"""路径管理模块 - 统一可写数据根目录与静态资源路径解析。"""

import os
from pathlib import Path
from typing import Optional

DEFAULT_DATA_DIR_NAME = "data"


class PathManager:
    """管理系统可写数据目录与静态资源路径解析。

    集中管理：
    - data_dir: 统一数据根目录 (可通过 RAINCLASS_DATA_DIR 覆盖)
    - config_file: config.json
    - state_file: browser_state.json
    - lock_file: .rainclass-assistant.lock
    - log_dir: log/
    - debug_dir: debug/
    - metrics_file: metrics/quiz_timings.jsonl
    """

    def __init__(
        self,
        data_dir: Optional[Path | str] = None,
        config_file: Optional[Path | str] = None,
    ) -> None:
        if data_dir is not None:
            self.data_dir = Path(data_dir).resolve()
        elif os.environ.get("RAINCLASS_DATA_DIR"):
            self.data_dir = Path(os.environ["RAINCLASS_DATA_DIR"]).resolve()
        else:
            self.data_dir = Path.cwd() / DEFAULT_DATA_DIR_NAME

        if config_file is not None:
            self.config_file = Path(config_file).resolve()
        else:
            # 优先检查数据目录下的 config.json；若不存在但工作目录根部存在 config.json，则兼容回退
            candidate = self.data_dir / "config.json"
            fallback = Path.cwd() / "config.json"
            if not candidate.exists() and fallback.exists():
                self.config_file = fallback.resolve()
            else:
                self.config_file = candidate

    @property
    def project_root(self) -> Path:
        """项目源码根目录。"""
        return Path(__file__).resolve().parent.parent

    @property
    def state_file(self) -> Path:
        """Playwright storage_state 会话状态文件。"""
        candidate = self.data_dir / "browser_state.json"
        fallback = Path.cwd() / "browser_state.json"
        if not candidate.exists() and fallback.exists():
            return fallback.resolve()
        return candidate

    @property
    def lock_file(self) -> Path:
        """单实例进程锁。"""
        return self.data_dir / ".rainclass-assistant.lock"

    @property
    def log_dir(self) -> Path:
        """日志目录。"""
        return self.data_dir / "log"

    @property
    def debug_dir(self) -> Path:
        """调试目录。"""
        return self.data_dir / "debug"

    @property
    def db_file(self) -> Path:
        """SQLite 答题流水线持久化数据库。"""
        return self.data_dir / "records.db"

    @property
    def health_file(self) -> Path:
        """运行状态与健康检查状态文件。"""
        return self.data_dir / "health.json"

    @property
    def metrics_file(self) -> Path:
        """结构化指标记录文件。"""
        return self.data_dir / "metrics" / "quiz_timings.jsonl"

    def ensure_dirs(self) -> None:
        """确保运行时所需的各可写目录已创建。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        self.metrics_file.parent.mkdir(parents=True, exist_ok=True)
        self.db_file.parent.mkdir(parents=True, exist_ok=True)

    def resolve_resource(self, filename: str) -> Path:
        """解析只读或模板资源文件路径。

        按顺序检索：
        1. data_dir
        2. 当前工作目录
        3. 项目根目录
        """
        candidates = [
            self.data_dir / filename,
            Path.cwd() / filename,
            self.project_root / filename,
        ]
        for p in candidates:
            if p.exists():
                return p.resolve()
        return candidates[-1].resolve()
