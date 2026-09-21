"""结构化事件计时与耗时分析模块。

使用 Python 单调时钟 (time.monotonic) 精确追踪答题全流程中关键节点的事件耗时：
1. question_detected: 观察到新题
2. question_ready: 页面和选项已就绪可调用 AI
3. ai_request_started: AI 请求发出
4. ai_response_received: 收到 AI 响应
5. answer_validated: 选项点击完毕并确认可提交
6. submit_clicked: 点击提交按钮
7. submit_confirmed: 提交确认完成
"""

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

STAGES = (
    "question_detected",
    "question_ready",
    "ai_request_started",
    "ai_response_received",
    "answer_validated",
    "submit_clicked",
    "submit_confirmed",
)


class QuizTimingTracker:
    """管理单道题目的事件分段计时与指标落盘。"""

    def __init__(
        self,
        question_id: str,
        lesson_id: str = "",
        account_id: str = "default_account",
        detection_source: str = "unknown",
        metrics_file: Optional[Path | str] = None,
    ) -> None:
        self.question_id = question_id
        self.lesson_id = lesson_id
        self.account_id = account_id
        self.detection_source = detection_source
        self.metrics_file = Path(metrics_file) if metrics_file else None
        self.timestamps: dict[str, float] = {}
        self.success: bool = False
        self.error_reason: str = ""
        self.ai_metrics: Optional[dict[str, Any]] = None

    def set_ai_metrics(self, metrics: dict[str, Any]) -> None:
        """设置本题关联的 AI 响应策略与模型统计指标。"""
        self.ai_metrics = metrics

    def mark(self, stage: str, timestamp: Optional[float] = None) -> None:
        """记录指定阶段的时间戳（单位：秒，来自 monotonic）。"""
        self.timestamps[stage] = (
            timestamp if timestamp is not None else time.monotonic()
        )

    def finish(self, success: bool, error_reason: str = "") -> dict[str, Any]:
        """完成计时统计，计算各分段毫秒耗时，记录指标日志并追加至 metrics 文件。"""
        self.success = success
        self.error_reason = error_reason

        def _diff(start_stage: str, end_stage: str) -> Optional[float]:
            if start_stage in self.timestamps and end_stage in self.timestamps:
                diff_ms = (self.timestamps[end_stage] - self.timestamps[start_stage]) * 1000.0
                return round(diff_ms, 2)
            return None

        durations_ms: dict[str, Optional[float]] = {
            "detect_to_ready_ms": _diff("question_detected", "question_ready"),
            "ready_to_ai_start_ms": _diff("question_ready", "ai_request_started"),
            "ai_duration_ms": _diff("ai_request_started", "ai_response_received"),
            "ai_to_validated_ms": _diff("ai_response_received", "answer_validated"),
            "validated_to_clicked_ms": _diff("answer_validated", "submit_clicked"),
            "clicked_to_confirmed_ms": _diff("submit_clicked", "submit_confirmed"),
            "total_end_to_end_ms": _diff("question_detected", "submit_confirmed"),
        }

        record: dict[str, Any] = {
            "question_id": self._anonymize(self.question_id),
            "lesson_id": self._anonymize(self.lesson_id),
            "account_id": self._anonymize(self.account_id),
            "detection_source": self.detection_source,
            "timestamps": {k: round(v, 4) for k, v in self.timestamps.items()},
            "durations_ms": {k: v for k, v in durations_ms.items() if v is not None},
            "success": self.success,
            "error_reason": self.error_reason,
        }

        if isinstance(self.ai_metrics, dict):
            record["ai_metrics"] = self.ai_metrics

        # 追加写入 metrics 文件（同步单行追加，不影响热路径高频落盘）
        if self.metrics_file:
            try:
                self.metrics_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self.metrics_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            except Exception as e:
                logger.warning("写入指标记录文件失败：%s", e)

        # 结构化输出一行可读耗时日志
        summary_items = [
            f"detect->ready: {durations_ms.get('detect_to_ready_ms', '-')}ms",
            f"AI推理: {durations_ms.get('ai_duration_ms', '-')}ms",
            f"点击校验: {durations_ms.get('ai_to_validated_ms', '-')}ms",
            f"确认提交: {durations_ms.get('clicked_to_confirmed_ms', '-')}ms",
            f"端到端总计: {durations_ms.get('total_end_to_end_ms', '-')}ms",
        ]
        if isinstance(self.ai_metrics, dict):
            strat = str(self.ai_metrics.get("strategy", "-"))
            winner = str(self.ai_metrics.get("winning_model", "-"))
            try:
                vrate = float(self.ai_metrics.get("valid_rate", 0.0) or 0.0)
                summary_items.append(f"AI策略: {strat}({winner}, 有效比:{int(vrate*100)}%)")
            except (TypeError, ValueError):
                summary_items.append(f"AI策略: {strat}({winner})")
        logger.info(
            "[TIMING] 题目 %s 耗时 (来源: %s, 成功: %s): %s",
            record["question_id"],
            self.detection_source,
            self.success,
            ", ".join(summary_items),
        )

        return record

    @staticmethod
    def _anonymize(val: str) -> str:
        """非敏感化标识符（截断哈希）。"""
        if not val:
            return "unknown"
        return hashlib.sha256(val.encode("utf-8")).hexdigest()[:8]
