"""题图下载与凭据安全模块。

处理题目图片下载、Cookie 域名/Path/Secure 匹配、重定向凭据隔离与日志脱敏。
"""

import logging
import os
import re
import urllib.parse
from datetime import datetime
from typing import Any, Optional, Tuple, Union

import requests

logger = logging.getLogger(__name__)

# 允许携带雨课堂会话凭据的主域名后缀
ALLOWED_CREDENTIAL_DOMAINS = (
    "yuketang.cn",
    "xuetangx.com",
)

# 敏感参数名正则（用于 URL 脱敏）
_SENSITIVE_PARAM_PATTERN = re.compile(
    r"(?i)(token|auth|key|apikey|secret|session|sessionid|password|sig|signature)=([^&]+)"
)


def sanitize_url(url: str) -> str:
    """对 URL 中的敏感 Query 参数进行脱敏替换。"""
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlparse(url)
        if not parsed.query:
            return url
        sanitized_query = _SENSITIVE_PARAM_PATTERN.sub(r"\1=***", parsed.query)
        return urllib.parse.urlunparse(parsed._replace(query=sanitized_query))
    except Exception:
        return _SENSITIVE_PARAM_PATTERN.sub(r"\1=***", url)


def sanitize_error(text: str) -> str:
    """脱敏错误字符串中可能包含的密钥、Token 与 URL。"""
    if not text:
        return ""
    # 替换标准 URL 中的敏感参数
    result = re.sub(
        r"https?://[^\s'\"]+",
        lambda m: sanitize_url(m.group(0)),
        str(text),
    )
    # 替换明显的常见 key/token 格式 (如 sk-..., ey...)
    result = re.sub(r"(sk-[a-zA-Z0-9_-]{10,})", r"sk-***", result)
    result = re.sub(r"(ey[a-zA-Z0-9_-]{20,}\.[a-zA-Z0-9_-]{20,})", r"ey***.***", result)
    return result


def is_domain_match(hostname: str, domain: str) -> bool:
    """RFC 6265 规范域名匹配。

    例如：
    - domain='.yuketang.cn' 匹配 'www.yuketang.cn' 和 'yuketang.cn'
    - domain='www.yuketang.cn' 匹配 'www.yuketang.cn'，不匹配 'api.yuketang.cn'
    """
    host = hostname.strip().lower()
    dom = domain.strip().lower().lstrip(".")
    if not host or not dom:
        return False
    return host == dom or host.endswith("." + dom)


def is_allowed_credential_host(hostname: str) -> bool:
    """检查目标主机是否为允许发送凭据的安全域名。"""
    host = hostname.strip().lower()
    return any(is_domain_match(host, allowed) for allowed in ALLOWED_CREDENTIAL_DOMAINS)


def filter_matching_cookies(
    target_url: str,
    cookies: Optional[Union[list[dict[str, Any]], dict[str, str]]],
) -> dict[str, str]:
    """根据目标 URL 严格匹配 Cookie（校验域名、路径、Secure 属性）。

    若目标主机不在雨课堂允许白名单内，则不传递任何会话 Cookie。
    """
    if not cookies or not target_url:
        return {}

    try:
        parsed = urllib.parse.urlparse(target_url)
    except Exception:
        return {}

    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    path = parsed.path or "/"

    if scheme not in ("http", "https") or not host:
        return {}

    # 若目标主机不是雨课堂/学堂在线官方域名，严禁发送任何鉴权凭据
    if not is_allowed_credential_host(host):
        return {}

    matched: dict[str, str] = {}

    # 情况 1: Playwright 标准格式 Cookie 列表 (包含 domain, path, secure 等)
    if isinstance(cookies, list):
        for c in cookies:
            if not isinstance(c, dict):
                continue
            name = c.get("name")
            val = c.get("value")
            if not name or val is None:
                continue

            c_domain = str(c.get("domain", "")).strip()
            c_path = str(c.get("path", "/")).strip() or "/"
            c_secure = bool(c.get("secure", False))

            # 校验域名
            if c_domain and not is_domain_match(host, c_domain):
                continue

            # 校验安全连接
            if c_secure and scheme != "https":
                continue

            # 校验路径 (URL 路径需匹配 cookie path)
            if not path.startswith(c_path):
                continue

            matched[name] = str(val)
        return matched

    # 情况 2: 扁平字典 dict[name, value]
    if isinstance(cookies, dict):
        # 仅在目标主机已通过 is_allowed_credential_host 白名单校验后才提供
        return {str(k): str(v) for k, v in cookies.items() if k and v is not None}

    return {}


def download_question_image(
    image_url: str,
    cookies: Optional[Union[list[dict[str, Any]], dict[str, str]]] = None,
    timeout: float = 10.0,
    save_dir: Optional[str] = None,
) -> Tuple[bytes, str]:
    """安全下载题目图片并保存至本地 data 目录。

    特性：
    1. 校验目标 URL 合法性（仅支持 http/https）。
    2. 严格按域名/path/secure 过滤待携带的 Cookie，跨站或离开白名单时自动剥离凭据。
    3. 重定向跟踪：遇到跨域/离开雨课堂白名单重定向时，不向第三方重定向地址透传 Cookie。
    4. 异常与日志自动进行 URL 及密钥脱敏，杜绝假密钥与 Token 泄漏。
    5. 返回 (image_bytes, saved_filepath)。
    """
    if not image_url:
        raise ValueError("图片 URL 不能为空")

    sanitized_original = sanitize_url(image_url)
    try:
        parsed = urllib.parse.urlparse(image_url)
        if parsed.scheme.lower() not in ("http", "https"):
            raise ValueError(f"不支持的 URL scheme: {parsed.scheme}")
    except Exception as exc:
        raise ValueError(f"图片 URL 非法：{sanitize_error(str(exc))}")

    session = requests.Session()
    current_url = image_url
    max_redirects = 3
    redirect_count = 0
    response: Optional[requests.Response] = None

    try:
        while redirect_count <= max_redirects:
            req_cookies = filter_matching_cookies(current_url, cookies)
            response = session.get(
                current_url,
                cookies=req_cookies,
                timeout=timeout,
                allow_redirects=False,
            )

            # 处理重定向
            if response.is_redirect or response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not location:
                    break
                next_url = urllib.parse.urljoin(current_url, location)
                redirect_count += 1
                current_url = next_url
                continue

            response.raise_for_status()
            break
        else:
            raise requests.TooManyRedirects(f"超过最大重定向次数 ({max_redirects})")

        if response is None:
            raise requests.RequestException("下载图片未能获得响应")

        response.raise_for_status()
        image_bytes = response.content
        if not image_bytes:
            raise ValueError("下载的图片内容为空")

    except Exception as exc:
        safe_msg = sanitize_error(str(exc))
        logger.warning("下载题图失败 [%s]：%s", sanitized_original, safe_msg)
        raise RuntimeError(f"题图下载失败: {safe_msg}") from None
    finally:
        session.close()

    # 存储到本地 data 目录
    now = datetime.now()
    if not save_dir:
        save_dir = os.path.join("data", now.strftime("%Y-%m-%d"))
    os.makedirs(save_dir, exist_ok=True)
    filepath = os.path.join(save_dir, f"{now.strftime('%H-%M-%S-%f')}.png")

    with open(filepath, "wb") as f:
        f.write(image_bytes)

    logger.debug("题目截图已保存：%s", filepath)
    return image_bytes, filepath
