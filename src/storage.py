"""SQLite 答题流水线状态持久化模块。

实现两阶段提交状态管理、崩溃恢复幂等性核对与必要指标记录。
严格要求：
1. 唯一联合约束防重复：UNIQUE(account_id, classroom_id, question_id, request_generation)；
2. 连接所有权明确，WAL 模式并发安全，写入事务尽量短；
3. 严禁把 API Key、Token、Cookie 或完整 HTML 写入数据库。
"""

import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator, Optional

logger = logging.getLogger(__name__)

# 中间处理阶段定义
STAGE_DETECTED = "detected"            # 首次检测到题目
STAGE_AI_REQUESTED = "ai_requested"    # 已向 AI 发起请求
STAGE_OPTIONS_CLICKED = "options_clicked"  # 已在浏览器中点击选项（尚未点击提交）
STAGE_SUBMITTING = "submitting"        # 点击提交中/已发点击但尚未确认（中间态）
STAGE_CONFIRMED = "confirmed"          # 已确认提交成功（提交按钮已消失/页面离开）
STAGE_UNKNOWN = "unknown"              # 提交状态不明（如点击超时/崩溃后按钮状态异常）
STAGE_FAILED = "failed"                # 明确作答失败（如无有效选项、模型无法处理）
STAGE_SKIPPED = "skipped"              # 判定为主观题/观察模式/状态不明跳过

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS quiz_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    classroom_id TEXT NOT NULL,
    question_id TEXT NOT NULL,
    request_generation INTEGER NOT NULL,
    stage TEXT NOT NULL,
    submitted_answer TEXT,
    submission_confirmed INTEGER DEFAULT 0,
    detection_source TEXT,
    latency_ms REAL,
    ai_strategy TEXT,
    winning_model TEXT,
    error_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(account_id, classroom_id, question_id, request_generation)
);

CREATE INDEX IF NOT EXISTS idx_quiz_records_lookup 
ON quiz_records(account_id, classroom_id, question_id);
"""


class QuizStorage:
    """SQLite 答题流水线持久化。"""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path).resolve()
        self._init_db()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA_SQL)

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        """建立带超时与 WAL 模式的短连接。"""
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=10.0,
            isolation_level=None,  # 自动提交模式，手动管理事务
        )
        conn.row_factory = sqlite3.Row
        try:
            # 开启 WAL 与普通同步，兼顾极速与崩溃安全
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            yield conn
        finally:
            conn.close()

    def record_stage(
        self,
        account_id: str,
        classroom_id: str,
        question_id: str,
        request_generation: int,
        stage: str,
        submitted_answer: Optional[str] = None,
        submission_confirmed: Optional[int] = None,
        detection_source: Optional[str] = None,
        latency_ms: Optional[float] = None,
        ai_strategy: Optional[str] = None,
        winning_model: Optional[str] = None,
        error_reason: Optional[str] = None,
    ) -> None:
        """记录或更新指定代际题目的处理阶段。

        使用 UPSERT 确保联合约束幂等更新。
        """
        now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        account_id = account_id or "default"
        classroom_id = classroom_id or "default"

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                # 检查是否存在
                cursor = conn.execute(
                    """
                    SELECT id, submitted_answer, submission_confirmed, detection_source, 
                           latency_ms, ai_strategy, winning_model, error_reason
                    FROM quiz_records
                    WHERE account_id = ? AND classroom_id = ? AND question_id = ? AND request_generation = ?
                    """,
                    (account_id, classroom_id, question_id, request_generation),
                )
                row = cursor.fetchone()
                if row:
                    # 更新
                    new_answer = submitted_answer if submitted_answer is not None else row["submitted_answer"]
                    new_confirmed = submission_confirmed if submission_confirmed is not None else row["submission_confirmed"]
                    new_source = detection_source if detection_source is not None else row["detection_source"]
                    new_latency = latency_ms if latency_ms is not None else row["latency_ms"]
                    new_strategy = ai_strategy if ai_strategy is not None else row["ai_strategy"]
                    new_model = winning_model if winning_model is not None else row["winning_model"]
                    new_reason = error_reason if error_reason is not None else row["error_reason"]

                    conn.execute(
                        """
                        UPDATE quiz_records
                        SET stage = ?, submitted_answer = ?, submission_confirmed = ?,
                            detection_source = ?, latency_ms = ?, ai_strategy = ?,
                            winning_model = ?, error_reason = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            stage,
                            new_answer,
                            new_confirmed,
                            new_source,
                            new_latency,
                            new_strategy,
                            new_model,
                            new_reason,
                            now_str,
                            row["id"],
                        ),
                    )
                else:
                    # 插入
                    conn.execute(
                        """
                        INSERT INTO quiz_records (
                            account_id, classroom_id, question_id, request_generation,
                            stage, submitted_answer, submission_confirmed, detection_source,
                            latency_ms, ai_strategy, winning_model, error_reason,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            account_id,
                            classroom_id,
                            question_id,
                            request_generation,
                            stage,
                            submitted_answer,
                            submission_confirmed or 0,
                            detection_source,
                            latency_ms,
                            ai_strategy,
                            winning_model,
                            error_reason,
                            now_str,
                            now_str,
                        ),
                    )
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def get_record(
        self,
        account_id: str,
        classroom_id: str,
        question_id: str,
        request_generation: int,
    ) -> Optional[dict[str, Any]]:
        """获取特定代际记录。"""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM quiz_records
                WHERE account_id = ? AND classroom_id = ? AND question_id = ? AND request_generation = ?
                """,
                (account_id or "default", classroom_id or "default", question_id, request_generation),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_latest_record(
        self,
        account_id: str,
        classroom_id: str,
        question_id: str,
    ) -> Optional[dict[str, Any]]:
        """获取某题最近一次处理记录（按 request_generation 倒序）。"""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM quiz_records
                WHERE account_id = ? AND classroom_id = ? AND question_id = ?
                ORDER BY request_generation DESC, id DESC
                LIMIT 1
                """,
                (account_id or "default", classroom_id or "default", question_id),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def has_confirmed(
        self,
        account_id: str,
        classroom_id: str,
        question_id: str,
    ) -> bool:
        """检查题目是否曾被确认提交过（任一代际）。"""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT 1 FROM quiz_records
                WHERE account_id = ? AND classroom_id = ? AND question_id = ? AND submission_confirmed = 1
                LIMIT 1
                """,
                (account_id or "default", classroom_id or "default", question_id),
            )
            return cursor.fetchone() is not None

    def list_unconfirmed_submitting(self, account_id: str) -> list[dict[str, Any]]:
        """查询留在 submitting 或 unknown 状态的未决记录（崩溃重启后核对）。"""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM quiz_records
                WHERE account_id = ? AND stage IN ('submitting', 'unknown') AND submission_confirmed = 0
                ORDER BY id DESC
                """,
                (account_id or "default",),
            )
            return [dict(r) for r in cursor.fetchall()]
