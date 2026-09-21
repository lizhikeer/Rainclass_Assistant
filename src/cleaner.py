"""持久化文件与调试工件清理模块。

为日志文件、截图和调试 HTML 提供容量与时长上限控制。
安全准则：
1. 清理范围必须严格限制在应用自身的可写数据目录下；
2. 禁止越界跟随符号链接或相对路径误删用户文件；
3. 保留关键配置、会话与数据库凭据。
"""

import logging
import os
import time
from pathlib import Path
from typing import Optional, Set

logger = logging.getLogger(__name__)

# 绝对禁止删除的关键核心文件
PROTECTED_FILENAMES: Set[str] = {
    "config.json",
    "browser_state.json",
    ".rainclass-assistant.lock",
    "records.db",
    "records.db-wal",
    "records.db-shm",
    "health.json",
    "quiz_timings.jsonl",
}


class ArtifactCleaner:
    """数据目录工件清理器。"""

    def __init__(
        self,
        data_dir: Path | str,
        max_age_days: int = 7,
        max_total_mb: float = 100.0,
        max_file_count: int = 500,
    ) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.max_age_seconds = max(1, max_age_days) * 86400.0
        self.max_total_bytes = int(max(0.0001, max_total_mb) * 1024 * 1024)
        self.max_file_count = max(1, max_file_count)

    def is_safe_path(self, path: Path) -> bool:
        """检查目标路径是否严格位于 data_dir 内部且非指向外部的符号链接。"""
        try:
            resolved = path.resolve()
            # 必须以 data_dir 绝对路径为前缀
            if not resolved.is_relative_to(self.data_dir):
                return False
            # 禁止跟随符号链接
            if path.is_symlink():
                target = path.readlink()
                target_resolved = (path.parent / target).resolve()
                if not target_resolved.is_relative_to(self.data_dir):
                    return False
            return True
        except (ValueError, RuntimeError, OSError):
            return False

    def clean(self) -> dict[str, int]:
        """执行独立低频清理，返回清理统计。"""
        if not self.data_dir.exists():
            return {"deleted_files": 0, "freed_bytes": 0}

        now = time.time()
        deleted_count = 0
        freed_bytes = 0

        # 只清理 log/ 与 debug/ 以及根目录临时保存的 html/png 工件
        candidate_dirs = [
            self.data_dir / "debug",
            self.data_dir / "log",
        ]

        file_infos: list[tuple[Path, os.stat_result]] = []

        for c_dir in candidate_dirs:
            if not c_dir.exists():
                continue
            for root, dirs, files in os.walk(c_dir):
                root_path = Path(root)
                if not self.is_safe_path(root_path):
                    logger.warning("发现越界或不安全路径，跳过清理: %s", root_path)
                    continue

                for fname in files:
                    if fname in PROTECTED_FILENAMES:
                        continue
                    file_path = root_path / fname
                    if not self.is_safe_path(file_path):
                        continue

                    try:
                        stat = file_path.lstat()
                    except OSError:
                        continue

                    # 1. 超过最大保留时间直接清理
                    if (now - stat.st_mtime) > self.max_age_seconds:
                        try:
                            file_path.unlink()
                            deleted_count += 1
                            freed_bytes += stat.st_size
                            continue
                        except OSError as e:
                            logger.debug("清理过期文件失败 %s: %s", file_path, e)

                    file_infos.append((file_path, stat))

        # 2. 检查总容量与文件数量上限（按 mtime 从旧到新删除）
        total_size = sum(st.st_size for _, st in file_infos)
        total_files = len(file_infos)

        if total_size > self.max_total_bytes or total_files > self.max_file_count:
            # 最旧的优先清理
            file_infos.sort(key=lambda item: item[1].st_mtime)
            for file_path, stat in file_infos:
                if total_size <= self.max_total_bytes and total_files <= self.max_file_count:
                    break
                try:
                    file_path.unlink()
                    deleted_count += 1
                    freed_bytes += stat.st_size
                    total_size -= stat.st_size
                    total_files -= 1
                except OSError as e:
                    logger.debug("按容量清理文件失败 %s: %s", file_path, e)

        # 3. 清理空子目录（排除根可写目录）
        for c_dir in candidate_dirs:
            if not c_dir.exists():
                continue
            for root, dirs, files in os.walk(c_dir, topdown=False):
                r_path = Path(root)
                if r_path != c_dir and self.is_safe_path(r_path):
                    try:
                        r_path.rmdir()
                    except OSError:
                        pass

        if deleted_count > 0:
            logger.info("数据工件清理完成：已删除 %d 个文件，释放 %.2f MB 空间。", deleted_count, freed_bytes / 1024 / 1024)

        return {"deleted_files": deleted_count, "freed_bytes": freed_bytes}
