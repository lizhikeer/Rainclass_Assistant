"""雨课堂网页扫码登录管理器 (QRLoginManager)

负责管理专用的临时无头浏览器实例、雨课堂扫码登录页面交互、二维码安全提取、
超时与刷新控制、人机验证码感知、以及登录成功后的原子会话落盘与 Worker 自动恢复。
"""

import base64
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

from src.browser import (
    YUKETANG_SERVERS,
    _ensure_playwright_browsers,
    validate_session_data,
)
from src.web.manager import LogRingBuffer, ProcessManager

logger = logging.getLogger(__name__)

# 登录状态常量
LOGIN_STATE_IDLE = "idle"
LOGIN_STATE_STARTING = "starting"
LOGIN_STATE_WAITING_SCAN = "waiting_scan"
LOGIN_STATE_SCANNED = "scanned"
LOGIN_STATE_SUCCESS = "success"
LOGIN_STATE_EXPIRED = "expired"
LOGIN_STATE_CANCELLED = "cancelled"
LOGIN_STATE_FAILED = "failed"
LOGIN_STATE_NEEDS_MANUAL = "needs_manual"

# 默认超时时长（秒）
DEFAULT_LOGIN_TIMEOUT = 180


class QRLoginManager:
    """雨课堂网页扫码登录生命周期管理器。"""

    def __init__(
        self,
        data_dir: str,
        process_manager: ProcessManager,
        log_buffer: LogRingBuffer,
        playwright_launcher: Optional[Callable] = None,
    ):
        self.data_dir = os.path.abspath(data_dir)
        self.process_manager = process_manager
        self.log_buffer = log_buffer
        self._launcher = playwright_launcher or sync_playwright

        self._lock = threading.RLock()
        self._active_session: Optional[dict[str, Any]] = None

    def get_status(self) -> dict[str, Any]:
        """获取当前扫码登录任务状态与二维码图像（Cache-Control: no-store）。"""
        with self._lock:
            if not self._active_session:
                return {
                    "active": False,
                    "state": LOGIN_STATE_IDLE,
                    "message": "空闲",
                    "error": "",
                    "session_id": "",
                    "expires_in": 0,
                    "has_qr": False,
                    "qr_image": None,
                    "server": "",
                }

            now = time.time()
            expires_in = max(0, int(self._active_session["expires_at"] - now))
            state = self._active_session["state"]
            has_qr = bool(self._active_session.get("qr_image"))

            return {
                "active": state in (LOGIN_STATE_STARTING, LOGIN_STATE_WAITING_SCAN, LOGIN_STATE_SCANNED),
                "state": state,
                "message": self._active_session.get("message", ""),
                "error": self._active_session.get("error", ""),
                "session_id": self._active_session.get("session_id", ""),
                "expires_in": expires_in,
                "has_qr": has_qr,
                "qr_image": self._active_session.get("qr_image"),
                "server": self._active_session.get("server", ""),
            }

    def start_login(self, server_name: str = "雨课堂") -> tuple[bool, str, dict[str, Any]]:
        """启动网页扫码登录流程。
        
        安全规则：
        1. 仅允许单一活动登录会话，快速重复调用予以阻断；
        2. 与主 Worker 互斥：若 Worker 正在运行，先停止 Worker 并记住其模式，登录完成后恢复；
        3. 仅从官方白名单域名中挑选目标服务器。
        """
        base_url = YUKETANG_SERVERS.get(server_name)
        if not base_url:
            # 允许传入合法 URL
            if server_name.startswith("http://") or server_name.startswith("https://"):
                host = urlsplit(server_name).hostname or ""
                if not host.endswith("yuketang.cn"):
                    return False, f"不支持的非雨课堂服务器域名: {host}", {}
                base_url = server_name.rstrip("/")
            else:
                return False, f"未知的雨课堂服务器: {server_name}", {}

        with self._lock:
            if self._active_session and self._active_session["state"] in (
                LOGIN_STATE_STARTING,
                LOGIN_STATE_WAITING_SCAN,
                LOGIN_STATE_SCANNED,
            ):
                return False, "已有正在进行的扫码登录任务，请先完成或取消当前任务", self.get_status()

            # 检查 Worker 状态并处理互斥
            prev_mode = "stopped"
            if self.process_manager.is_running():
                if callable(getattr(self.process_manager, "get_desired_mode", None)):
                    prev_mode = self.process_manager.get_desired_mode()
                else:
                    prev_mode = getattr(self.process_manager, "desired_mode", "observe")
                self.log_buffer.append("【扫码登录】检测到 Worker 正在运行，为避免会话冲突，正在暂停 Worker...")
                self.process_manager.stop()

            session_id = uuid.uuid4().hex
            cancel_event = threading.Event()
            refresh_event = threading.Event()

            session_data = {
                "session_id": session_id,
                "server": server_name,
                "base_url": base_url,
                "state": LOGIN_STATE_STARTING,
                "message": "正在启动隔离无头浏览器并加载登录页面...",
                "error": "",
                "qr_image": None,
                "created_at": time.time(),
                "expires_at": time.time() + DEFAULT_LOGIN_TIMEOUT,
                "previous_mode": prev_mode,
                "cancel_event": cancel_event,
                "refresh_event": refresh_event,
            }
            self._active_session = session_data

            worker_thread = threading.Thread(
                target=self._run_login_worker,
                args=(session_data,),
                daemon=True,
                name=f"qr-login-{session_id[:8]}",
            )
            session_data["thread"] = worker_thread
            worker_thread.start()

            self.log_buffer.append(f"【扫码登录】已创建新登录任务 ({session_id[:8]}...)，目标站点: {server_name}")
            return True, "扫码登录任务已启动", {"session_id": session_id, "state": LOGIN_STATE_STARTING}

    def cancel_login(self, session_id: Optional[str] = None) -> tuple[bool, str]:
        """取消当前登录任务，释放浏览器资源，并根据期望模式恢复 Worker。"""
        with self._lock:
            if not self._active_session:
                return False, "当前无活跃的扫码登录任务"
            if session_id and self._active_session.get("session_id") != session_id:
                return False, "会话 ID 不匹配"

            state = self._active_session["state"]
            if state not in (LOGIN_STATE_STARTING, LOGIN_STATE_WAITING_SCAN, LOGIN_STATE_SCANNED):
                return False, f"任务已处于终态 ({state})，无需取消"

            self._active_session["cancel_event"].set()
            self._active_session["state"] = LOGIN_STATE_CANCELLED
            self._active_session["message"] = "用户已取消扫码登录"
            self._active_session["qr_image"] = None
            prev_mode = self._active_session.get("previous_mode", "stopped")

        self.log_buffer.append("【扫码登录】用户取消了登录流程。")
        # 恢复 Worker
        if prev_mode and prev_mode != "stopped":
            self.log_buffer.append(f"【扫码登录】正在恢复 Worker 至 {prev_mode} 模式...")
            self.process_manager.start(mode=prev_mode)

        return True, "已取消登录任务"

    def refresh_qr(self, session_id: Optional[str] = None) -> tuple[bool, str]:
        """刷新二维码。"""
        with self._lock:
            if not self._active_session:
                return False, "当前无活跃的扫码登录任务"
            if session_id and self._active_session.get("session_id") != session_id:
                return False, "会话 ID 不匹配"

            state = self._active_session["state"]
            if state not in (LOGIN_STATE_WAITING_SCAN, LOGIN_STATE_EXPIRED):
                return False, f"当前状态 ({state}) 无法刷新二维码"

            self._active_session["refresh_event"].set()
            self._active_session["message"] = "正在刷新二维码..."
            self._active_session["expires_at"] = time.time() + DEFAULT_LOGIN_TIMEOUT

        return True, "已触发二维码刷新"

    def _run_login_worker(self, session: dict[str, Any]) -> None:
        """运行在独立线程中的 Playwright 浏览器自动化流程。"""
        session_id = session["session_id"]
        base_url = session["base_url"]
        cancel_event: threading.Event = session["cancel_event"]
        refresh_event: threading.Event = session["refresh_event"]
        prev_mode = session.get("previous_mode", "stopped")

        pw = None
        browser = None
        context = None
        page = None

        try:
            _ensure_playwright_browsers(auto_install=True)
            pw = self._launcher().start()
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )
            page = context.new_page()

            login_url = f"{base_url}/web/"
            logger.info("QRLogin: 正在导航至登录页面 %s", login_url)
            page.goto(login_url, timeout=30_000)

            # 等待二维码出现
            qr_locator = page.locator("#qrcode-box img.logma, img.logma")
            try:
                qr_locator.wait_for(state="visible", timeout=15_000)
            except Exception:
                pass

            # 提取二维码并更新会话
            self._update_qr_image(page, session)

            if cancel_event.is_set():
                session["state"] = LOGIN_STATE_CANCELLED
                return

            # 主轮询循环：等待用户微信或雨课堂 APP 扫码完成
            poll_interval = 1.0
            while not cancel_event.is_set():
                now = time.time()
                if now > session["expires_at"]:
                    session["state"] = LOGIN_STATE_EXPIRED
                    session["message"] = "二维码已过期，请点击刷新重新获取"
                    session["qr_image"] = None
                    self.log_buffer.append("【扫码登录】二维码已超时过期。")
                    break

                # 响应刷新事件
                if refresh_event.is_set():
                    refresh_event.clear()
                    try:
                        reload_btn = page.locator(".load-fail .fetch-btn, .fetch-btn")
                        if reload_btn.count() > 0 and reload_btn.is_visible():
                            reload_btn.click()
                        else:
                            page.reload(timeout=15_000)
                        page.wait_for_timeout(1000)
                        self._update_qr_image(page, session)
                        session["state"] = LOGIN_STATE_WAITING_SCAN
                        session["message"] = "二维码已刷新，请使用微信或雨课堂 APP 扫码"
                    except Exception as err:
                        logger.warning("QRLogin: 刷新二维码异常: %s", err)

                # 1. 检查人机验证码障碍（例如 hCaptcha 或腾讯滑块）
                if self._check_captcha_obstacles(page):
                    session["state"] = LOGIN_STATE_NEEDS_MANUAL
                    session["error"] = "雨课堂触发了安全人机验证码，请在本地电脑运行命令行完成首次登录后导入会话。"
                    session["message"] = "触发人机验证码，无法在纯无头模式继续"
                    session["qr_image"] = None
                    self.log_buffer.append("【扫码登录】⚠️ 雨课堂触发安全人机验证，请使用本地 CLI 登录并导入。")
                    break

                # 2. 检查页面二维码是否在 DOM 中失效
                load_fail = page.locator(".load-fail")
                if load_fail.count() > 0 and load_fail.is_visible():
                    if session["state"] != LOGIN_STATE_EXPIRED:
                        session["state"] = LOGIN_STATE_EXPIRED
                        session["message"] = "二维码已过期，请点击刷新重新获取"
                        self.log_buffer.append("【扫码登录】页面提示二维码已失效。")

                # 3. 检查登录完成标志
                if self._is_login_successful(page, context):
                    session["state"] = LOGIN_STATE_SCANNED
                    session["message"] = "检测到扫码成功，正在持久化登录凭据..."
                    self.log_buffer.append("【扫码登录】检测到用户扫码成功！正在保存会话...")

                    # 给予少量时间确保所有凭证写完
                    time.sleep(1.0)
                    storage = context.storage_state()
                    valid, msg = validate_session_data(storage, expected_base_url=base_url)
                    if not valid:
                        session["state"] = LOGIN_STATE_FAILED
                        session["error"] = f"会话校验未通过: {msg}"
                        session["message"] = "获取到的会话凭据无效"
                        self.log_buffer.append(f"【扫码登录】❌ 会话校验失败: {msg}")
                        break

                    # 原子写入 browser_state.json
                    target_file = Path(self.data_dir) / "browser_state.json"
                    temp_file = target_file.with_suffix(".tmp")
                    with open(temp_file, "w", encoding="utf-8") as f:
                        json.dump(storage, f, ensure_ascii=False, indent=2)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(temp_file, target_file)

                    session["state"] = LOGIN_STATE_SUCCESS
                    session["message"] = "雨课堂账号登录成功！凭据已原子保存并生效。"
                    session["qr_image"] = None
                    self.log_buffer.append(f"【扫码登录】✅ 成功保存登录会话至 {target_file}")

                    # 恢复 Worker
                    if prev_mode and prev_mode != "stopped":
                        self.log_buffer.append(f"【扫码登录】正在自动恢复 Worker 至 {prev_mode} 模式...")
                        self.process_manager.start(mode=prev_mode)
                    break

                time.sleep(poll_interval)

        except Exception as exc:
            logger.error("QRLogin: 执行异常: %s", exc, exc_info=True)
            session["state"] = LOGIN_STATE_FAILED
            session["error"] = f"扫码登录异常: {exc}"
            session["message"] = "自动化浏览器执行失败"
            session["qr_image"] = None
            self.log_buffer.append(f"【扫码登录】❌ 登录异常退出: {exc}")
        finally:
            if context:
                try:
                    context.close()
                except Exception:
                    pass
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            if pw:
                try:
                    pw.stop()
                except Exception:
                    pass

    def _update_qr_image(self, page: Any, session: dict[str, Any]) -> None:
        """从页面提取二维码并保存在内存中。"""
        try:
            qr_locator = page.locator("#qrcode-box img.logma, img.logma")
            if qr_locator.count() > 0:
                src = qr_locator.first.get_attribute("src")
                if src and src.startswith("data:image/"):
                    session["qr_image"] = src
                    session["state"] = LOGIN_STATE_WAITING_SCAN
                    session["message"] = "请使用微信或雨课堂 APP 扫描二维码登录"
                    return

                # 若不是 base64 data url，则对二维码区域进行内存截图
                png_bytes = qr_locator.first.screenshot()
                b64 = base64.b64encode(png_bytes).decode("ascii")
                session["qr_image"] = f"data:image/png;base64,{b64}"
                session["state"] = LOGIN_STATE_WAITING_SCAN
                session["message"] = "请使用微信或雨课堂 APP 扫描二维码登录"
                return
        except Exception as e:
            logger.debug("QRLogin: 提取二维码失败或暂不可见: %s", e)

    def _check_captcha_obstacles(self, page: Any) -> bool:
        """检查是否有不可无头绕过的验证码。"""
        try:
            if page.locator("iframe[src*='hcaptcha'], iframe[src*='gtimg'], .turing-verify").count() > 0:
                for el in page.locator("iframe[src*='hcaptcha'], iframe[src*='gtimg'], .turing-verify").all():
                    if el.is_visible():
                        return True
        except Exception:
            pass
        return False

    def _is_login_successful(self, page: Any, context: Any) -> bool:
        """通过页面元素、重定向 URL 与 Cookie 综合确认是否已成功登录。"""
        try:
            # 1. 检查页面学生端元素
            if page.locator("#tab-student, .user-name, .avatar, .header-user").count() > 0:
                return True

            # 2. 检查是否发生重定向到应用主页
            cur_url = page.url.lower()
            if "/v2/web/" in cur_url or "/studentlog/" in cur_url or "/index" in cur_url:
                if "/web/" not in cur_url or cur_url.endswith("/v2/web/") or "/v2/web/index" in cur_url:
                    return True

            # 3. 检查 Cookie 中是否已经包含有效 sessionid
            cookies = context.cookies()
            for c in cookies:
                name = c.get("name", "")
                val = c.get("value", "")
                domain = c.get("domain", "")
                if name in ("sessionid", "login_token") and val and "yuketang.cn" in domain:
                    return True
        except Exception:
            pass
        return False
