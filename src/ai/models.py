"""AI 数据结构、答案校验与归一化模块。

定义 Endpoint 配置、模型单次调用指标、全轮决策结果，以及符合题型规范的答案解析器。
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 单次答案生成的 Token 上限
ANSWER_MAX_TOKENS = 128


@dataclass(frozen=True)
class EndpointConfig:
    """单个 AI 模型的配置参数。"""
    name: str
    base_url: str
    api_key: str
    model: str
    timeout: float = 15.0
    description: str = ""
    provider_type: str = "openai_compatible"  # doubao / gemini / openai_compatible
    extra_body: Optional[dict[str, Any]] = None


@dataclass
class ModelCallResult:
    """单次模型请求的调用结果与耗时指标。"""
    model_name: str
    raw_response: str = ""
    vote: Optional[tuple[str, ...]] = None
    is_valid: bool = False
    is_submittable: bool = False
    latency_ms: float = 0.0
    usage: Optional[dict[str, int]] = None  # prompt_tokens, completion_tokens, total_tokens
    error: Optional[str] = None
    cancelled: bool = False
    late: bool = False


@dataclass
class RoundDecision:
    """当前题目的整体 AI 决策结果与综合统计。"""
    strategy: str
    winning_model: str = ""
    winning_answer: str = ""
    vote: Optional[tuple[str, ...]] = None
    is_success: bool = False
    total_duration_ms: float = 0.0
    model_latencies: dict[str, float] = field(default_factory=dict)
    requests_sent: int = 0
    valid_results_count: int = 0
    backup_triggered: bool = False
    timeout_count: int = 0
    late_results_count: int = 0
    token_usage: dict[str, Any] = field(default_factory=dict)
    details: list[ModelCallResult] = field(default_factory=list)

    @property
    def valid_rate(self) -> float:
        """有效结果率（注意：是格式与内容可提交率，不是答题正确率）。"""
        if self.requests_sent <= 0:
            return 0.0
        return round(self.valid_results_count / self.requests_sent, 4)

    def to_metrics_dict(self) -> dict[str, Any]:
        """导出适合写入 metrics.jsonl 的结构化字典。"""
        return {
            "strategy": self.strategy,
            "winning_model": self.winning_model,
            "is_success": self.is_success,
            "total_duration_ms": round(self.total_duration_ms, 2),
            "model_latencies": {k: round(v, 2) for k, v in self.model_latencies.items()},
            "requests_sent": self.requests_sent,
            "valid_results_count": self.valid_results_count,
            "valid_rate": self.valid_rate,
            "backup_triggered": self.backup_triggered,
            "timeout_count": self.timeout_count,
            "late_results_count": self.late_results_count,
            "token_usage": self.token_usage if self.token_usage else "unknown",
        }


def vote_letters(value: Any, *, allow_compact: bool = False) -> tuple[str, ...]:
    """提取答案中的大写选项字母（A-G）。"""
    if isinstance(value, list):
        text = ",".join(str(item) for item in value)
    else:
        text = str(value or "")
    compact = text.strip().upper()
    if allow_compact and re.fullmatch(r"[A-G]{1,7}", compact):
        return tuple(sorted(set(compact)))
    match = re.fullmatch(
        r"\s*(?:(?:答案(?:是|为)?|ANSWER)\s*[:：]?\s*)?"
        r"([A-G](?:\s*[,，、/\s]\s*[A-G])*)\s*[。.]?\s*",
        text.upper(),
    )
    if not match:
        return ()
    return tuple(sorted(set(re.findall(r"[A-G]", match.group(1)))))


def extract_answer_json(value: str) -> Optional[dict]:
    """从包含思维链或代码块的文本中提取包含答题字段的首个 JSON 对象。"""
    decoder = json.JSONDecoder()
    fallback = None
    for match in re.finditer(r"\{", value):
        try:
            candidate, _ = decoder.raw_decode(value[match.start():])
        except json.JSONDecodeError:
            continue
        if not isinstance(candidate, dict):
            continue
        if "type" in candidate or "answers" in candidate:
            return candidate
        if fallback is None:
            fallback = candidate
    return fallback


def is_failed_text(text: str) -> bool:
    """检测文本是否为明确的错误提示或调用失败。"""
    if not text or not text.strip():
        return True
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "调用失败",
            "答题失败",
            "下载失败",
            "无法获取",
            "未设置",
            "error",
            "timeout",
            "insufficient balance",
            "<html",
            "<!doctype",
        )
    )


def canonical_vote(text: str) -> Optional[tuple[str, ...]]:
    """归一化模型返回结果为可计票/判断键。

    返回值形式：
    - 单选/多选: ("choice", "A") 或 ("choice", "A", "B", "C")
    - 判断题: ("judgment", "true") 或 ("judgment", "false")
    - 填空/主观题: ("type", "fill") 或 ("type", "sub")
    - 视觉缺失: ("type", "unknown") 或 None
    - 无效/错误返回: None
    """
    if not text or not text.strip():
        return None
    value = text.strip()
    value = re.sub(r"^```(?:json)?", "", value, flags=re.IGNORECASE).strip()
    value = re.sub(r"```$", "", value).strip()

    data = extract_answer_json(value)

    if data is None:
        if is_failed_text(value):
            return None
        letters = vote_letters(value)
        if letters:
            return ("choice", *letters)
        judgment = value.upper().rstrip("。.")
        if judgment in ("对", "正确", "T", "TRUE", "是"):
            return ("judgment", "true")
        if judgment in ("错", "错误", "F", "FALSE", "否"):
            return ("judgment", "false")
        return None

    qtype = str(data.get("type", "")).strip().lower()
    if qtype == "unknown":
        return ("type", "unknown")

    raw_answer = data.get("answers", "")
    letters = vote_letters(raw_answer, allow_compact=True)
    if letters:
        # 如果声称为 single 但包含多个选项，依然记录 choice，但由 is_submittable_vote 做严格检查
        return ("choice", *letters)

    judgment = str(raw_answer).strip().upper().rstrip("。.")
    if judgment in ("对", "正确", "T", "TRUE", "是"):
        return ("judgment", "true")
    if judgment in ("错", "错误", "F", "FALSE", "否"):
        return ("judgment", "false")
    if qtype in ("fill", "sub"):
        return ("type", qtype)
    return None


def is_submittable_vote(vote: Optional[tuple[str, ...]]) -> bool:
    """检查 vote 是否属于可自动提交的有效答案。

    按照规范：
    - 单选/多选/判断题属于有效可提交。
    - 主观题(sub)、填空题(fill)、缺失视觉能力(unknown)、解析错误或空则不可提交。
    """
    if vote is None or not vote:
        return False
    kind = vote[0]
    if kind == "judgment":
        return len(vote) == 2 and vote[1] in ("true", "false")
    if kind == "choice":
        # 必须有至少一个有效大写字母 A-G
        letters = vote[1:]
        return len(letters) >= 1 and all(re.fullmatch(r"[A-G]", l) for l in letters)
    return False


def vote_to_answer(vote: tuple[str, ...]) -> str:
    """将归一化的 vote 转换回标准 JSON 字符串供提交。"""
    kind = vote[0]
    if kind == "choice":
        letters = list(vote[1:])
        payload = {
            "type": "single" if len(letters) == 1 else "multi",
            "answers": letters[0] if len(letters) == 1 else letters,
        }
    elif kind == "judgment":
        payload = {
            "type": "single",
            "answers": "对" if vote[1] == "true" else "错",
        }
    else:
        payload = {"type": vote[1]}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
