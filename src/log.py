"""公共日志模块 - 解除 GUI 依赖，支持控制台、文件与 GUI 队列输出。"""

import logging
import os
import queue
import sys
from logging.handlers import (
    QueueHandler,
    QueueListener,
    TimedRotatingFileHandler,
)
from pathlib import Path
from typing import Optional

# 默认日志格式
LOG_FORMAT = logging.Formatter(
    "%(asctime)s  %(message)s", datefmt="%H:%M:%S"
)

DEFAULT_LOG_DIR = Path("log")
DEFAULT_LOG_FILE = DEFAULT_LOG_DIR / "bot.log"
LOG_BACKUP_DAYS = 365

_log_queue: queue.Queue = queue.Queue()
_log_listener: Optional[QueueListener] = None


import re

SENSITIVE_PATTERNS = [
    (re.compile(r'(sessionid[=:][\s"\']*)([a-zA-Z0-9_\-]{8,})', re.IGNORECASE), r'\1***REDACTED***'),
    (re.compile(r'(api[-_]?key[=:][\s"\']*)([^\s"\'&,;]{6,})', re.IGNORECASE), r'\1***REDACTED***'),
    (re.compile(r'(token[=:][\s"\']*)([^\s"\'&,;]{6,})', re.IGNORECASE), r'\1***REDACTED***'),
    (re.compile(r'(password[=:][\s"\']*)([^\s"\'&,;]+)', re.IGNORECASE), r'\1***REDACTED***'),
    (re.compile(r'(bearer\s+)([a-zA-Z0-9_\-\.]{8,})', re.IGNORECASE), r'\1***REDACTED***'),
    (re.compile(r'(sk-[a-zA-Z0-9]{16,})', re.IGNORECASE), r'sk-***REDACTED***'),
]


class SanitizingFilter(logging.Filter):
    """日志脱敏过滤器：自动遮蔽日志记录中的敏感凭据。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            for pattern, repl in SENSITIVE_PATTERNS:
                message = pattern.sub(repl, message)
            record.msg = message
            record.args = None
        except Exception:
            pass
        return True


class _MaxLogLengthFilter(logging.Filter):
    """日志长度闸门：超长记录截断后再落盘/上屏。

    模型报错、SDK 调试日志可能携带整页 HTML 或 base64 数据（曾出现
    单条 315KB 的日志），会在控制台/GUI 刷屏并撑爆日志文件。
    正常业务日志远低于该上限，不受影响。
    """

    def __init__(self, limit: int = 1000) -> None:
        super().__init__()
        self.limit = limit

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        if len(message) > self.limit:
            record.msg = (
                f"{message[:self.limit]}...(日志过长已截断，原始 {len(message)} 字符)"
            )
            record.args = None
        return True


def _bot_log_namer(default_name: str) -> str:
    """把 TimedRotating 默认名 bot.log.YYYY-MM-DD 改成 bot_YYYY-MM-DD.log。"""
    path = Path(default_name)
    name = path.name
    if not name.startswith("bot.log."):
        return default_name
    date_part = name[len("bot.log.") :]
    if len(date_part) == 10 and date_part[4] == "-" and date_part[7] == "-":
        return str(path.with_name(f"bot_{date_part}.log"))
    return default_name


def setup_logging(
    log_dir: Path | str = DEFAULT_LOG_DIR,
    is_debug: Optional[bool] = None,
    enable_console: bool = False,
    enable_file: bool = True,
    extra_handlers: Optional[list[logging.Handler]] = None,
) -> None:
    """配置全局日志系统，通过 QueueHandler + QueueListener 异步分发日志。"""
    global _log_listener
    if _log_listener is not None:
        stop_logging()

    root_logger = logging.getLogger()
    if is_debug is None:
        is_debug = bool(os.environ.get("RAINCLASS_DEBUG"))
    root_logger.setLevel(logging.DEBUG if is_debug else logging.INFO)
    root_logger.handlers.clear()

    # 静音第三方高噪日志
    for noisy_logger in ("openai", "httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    qh = QueueHandler(_log_queue)
    root_logger.addHandler(qh)

    handlers: list[logging.Handler] = []
    length_gate = _MaxLogLengthFilter()
    sanitizer = SanitizingFilter()

    if enable_file:
        dir_path = Path(log_dir)
        dir_path.mkdir(parents=True, exist_ok=True)
        file_path = dir_path / "bot.log"
        fh = TimedRotatingFileHandler(
            file_path,
            when="midnight",
            backupCount=LOG_BACKUP_DAYS,
            encoding="utf-8",
        )
        fh.namer = _bot_log_namer
        fh.setFormatter(LOG_FORMAT)
        fh.addFilter(length_gate)
        fh.addFilter(sanitizer)
        handlers.append(fh)

    if enable_console:
        try:
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(errors="replace")
        except Exception:
            pass
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(LOG_FORMAT)
        ch.addFilter(length_gate)
        ch.addFilter(sanitizer)
        handlers.append(ch)

    if extra_handlers:
        handlers.extend(extra_handlers)

    _log_listener = QueueListener(_log_queue, *handlers, respect_handler_level=True)
    _log_listener.start()


def stop_logging() -> None:
    """停止日志监听器并刷新关闭所有处理器。"""
    global _log_listener
    if _log_listener is not None:
        listener = _log_listener
        listener.stop()
        for handler in listener.handlers:
            try:
                handler.close()
            except Exception:
                pass
        _log_listener = None
