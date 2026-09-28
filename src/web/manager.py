"""进程管理器与日志缓冲区 - 负责单实例 Worker 子进程生命周期、日志流式分发与配置/会话状态协调。"""

import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Generator, Optional

from src.browser import validate_session_data, YUKETANG_SERVERS
from src.config import Config, DEFAULTS
from src.storage import STAGE_CONFIRMED, STAGE_SKIPPED, STAGE_UNKNOWN, STAGE_FAILED


logger = logging.getLogger(__name__)

# 敏感字段脱敏正则
SENSITIVE_PATTERNS = [
    (re.compile(r'(?i)(api[_-]?key["\']?\s*[:=]\s*["\'])([^"\']{4})[^"\']+([^"\']{4}["\'])'), r'\1\2******\3'),
    (re.compile(r'(?i)(bearer\s+)([^"\s]{4})[^"\s]+([^"\s]{4})'), r'\1\2******\3'),
    (re.compile(r'(?i)(sessionid["\']?\s*[:=]\s*["\'])([^"\']{3})[^"\']+([^"\']{3}["\'])'), r'\1\2******\3'),
    (re.compile(r'(?i)(password["\']?\s*[:=]\s*["\'])([^"\']+)'), r'\1******'),
]

ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')


def sanitize_text(text: str) -> str:
    """过滤控制台 ANSI 颜色码与敏感密钥/Cookie。"""
    cleaned = ANSI_ESCAPE.sub('', text)
    for pattern, repl in SENSITIVE_PATTERNS:
        cleaned = pattern.sub(repl, cleaned)
    return cleaned


class LogRingBuffer:
    """线程安全的内存日志环形缓冲区，支持全量脱敏与多客户端 SSE 订阅。"""

    def __init__(self, capacity: int = 1000):
        self.capacity = capacity
        self._lines: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._listeners: list[Callable[[dict[str, Any]], None]] = []

    def append(self, text: str, level: str = "INFO") -> None:
        clean = sanitize_text(text.strip())
        if not clean:
            return
        entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "level": level,
            "message": clean,
        }
        with self._lock:
            self._lines.append(entry)
            listeners = list(self._listeners)

        for callback in listeners:
            try:
                callback(entry)
            except Exception:
                pass

    def get_lines(self, limit: int = 200, level: Optional[str] = None, keyword: Optional[str] = None) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._lines)
        if level:
            lvl = level.upper()
            items = [item for item in items if item.get("level") == lvl]
        if keyword:
            kw = keyword.lower()
            items = [item for item in items if kw in item.get("message", "").lower()]
        return items[-limit:]

    def subscribe(self, callback: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(callback)

        def unsubscribe():
            with self._lock:
                if callback in self._listeners:
                    self._listeners.remove(callback)

        return unsubscribe

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()


class ProcessManager:
    """单一进程管理器：控制独立的 Worker 子进程，管理配置与会话状态。"""

    def __init__(self, data_dir: str = "data"):
        self.data_dir = os.path.abspath(data_dir)
        os.makedirs(self.data_dir, exist_ok=True)

        self.config_path = os.path.join(self.data_dir, "config.json")
        self.session_path = os.path.join(self.data_dir, "browser_state.json")
        self.health_path = os.path.join(self.data_dir, "health.json")
        self.panel_state_path = os.path.join(self.data_dir, "panel_state.json")
        self.records_db_path = os.path.join(self.data_dir, "records.db")

        self.log_buffer = LogRingBuffer(capacity=1000)
        self._lock = threading.Lock()
        self._process: Optional[subprocess.Popen] = None
        self._log_reader_thread: Optional[threading.Thread] = None

        self._desired_mode: str = self._load_desired_mode()
        self._last_started_config: Optional[dict[str, Any]] = None
        self._start_error: str = ""

    def _load_desired_mode(self) -> str:
        """加载用户期望模式：observe / auto / stopped。"""
        if os.path.exists(self.panel_state_path):
            try:
                with open(self.panel_state_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    mode = data.get("desired_mode", "stopped")
                    if mode in ("observe", "auto", "stopped"):
                        return mode
            except Exception:
                pass
        return "stopped"

    def _save_desired_mode(self, mode: str) -> None:
        self._desired_mode = mode
        try:
            temp_file = f"{self.panel_state_path}.tmp"
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump({"desired_mode": mode, "updated_at": datetime.now().isoformat()}, f, indent=2)
            os.replace(temp_file, self.panel_state_path)
        except Exception as e:
            logger.warning("无法持久化 panel_state.json: %s", e)

    def is_running(self) -> bool:
        with self._lock:
            if self._process is None:
                return False
            return self._process.poll() is None

    def start(self, mode: str = "observe") -> tuple[bool, str]:
        """幂等启动指定模式的 Worker。"""
        if mode not in ("observe", "auto"):
            return False, f"不支持的运行模式: {mode}"

        with self._lock:
            # 若正在运行且模式一致，直接返回成功
            if self._process is not None and self._process.poll() is None:
                if self._desired_mode == mode:
                    return True, f"Worker 已在以 {mode} 模式运行中"
                # 模式不同，需要先停止后重启
                self._stop_locked()

            self._save_desired_mode(mode)
            self._start_error = ""

            # 记录启动时的配置快照，用于判断配置是否被修改但未生效
            cfg = Config(self.config_path)
            self._last_started_config = cfg.to_dict()

            cmd = [
                sys.executable,
                "-m",
                "src.worker",
                "--data-dir",
                self.data_dir,
                "--config",
                self.config_path,
                "--mode",
                mode,
            ]

            env = dict(os.environ)
            env["PYTHONUNBUFFERED"] = "1"
            env["WORKER_MODE"] = mode
            env["RAINCLASS_MODE"] = mode

            try:
                self.log_buffer.append(f"正在拉起 Worker 子进程 (模式: {mode})...", level="INFO")
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env=env,
                    cwd=os.getcwd(),
                )
                self._process = proc

                # 启动后台读取线程，向环形队列填充日志
                self._log_reader_thread = threading.Thread(
                    target=self._pipe_logs,
                    args=(proc,),
                    daemon=True,
                )
                self._log_reader_thread.start()
                return True, f"已成功启动 {mode} 模式"
            except Exception as e:
                self._start_error = str(e)
                self.log_buffer.append(f"启动 Worker 失败: {e}", level="ERROR")
                return False, f"启动 Worker 失败: {e}"

    def stop(self) -> tuple[bool, str]:
        """停止当前 Worker 子进程。"""
        with self._lock:
            self._save_desired_mode("stopped")
            return self._stop_locked()

    def _stop_locked(self) -> tuple[bool, str]:
        proc = self._process
        if proc is None or proc.poll() is not None:
            self._process = None
            return True, "Worker 未在运行"

        self.log_buffer.append("正在向 Worker 发送优雅停机信号 (SIGTERM)...", level="INFO")
        try:
            proc.terminate()
        except Exception:
            pass

        # 等待最多 5 秒让其优雅收尾
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.1)

        if proc.poll() is None:
            self.log_buffer.append("Worker 超时未退出，发送强制终止 (SIGKILL)...", level="WARN")
            try:
                proc.kill()
                proc.wait(timeout=2.0)
            except Exception:
                pass

        self._process = None
        self.log_buffer.append("Worker 已成功停止", level="INFO")
        return True, "Worker 已停止"

    def restart(self) -> tuple[bool, str]:
        """重启当前 Worker，沿用当前期望模式（若停止中则默认使用配置的 mode 或 observe）。"""
        mode = self._desired_mode if self._desired_mode in ("observe", "auto") else "observe"
        self.stop()
        time.sleep(0.3)
        return self.start(mode)

    def _pipe_logs(self, proc: subprocess.Popen) -> None:
        """从子进程管道持续抓取控制台输出并投递至环形队列。"""
        try:
            if proc.stdout:
                for line in proc.stdout:
                    if line:
                        level = "INFO"
                        if "ERROR" in line or "Traceback" in line or "失败" in line or "异常" in line:
                            level = "ERROR"
                        elif "WARN" in line or "警告" in line or "跳过" in line:
                            level = "WARN"
                        self.log_buffer.append(line, level=level)
        except Exception:
            pass
        finally:
            exit_code = proc.poll()
            if exit_code is not None:
                self.log_buffer.append(f"Worker 子进程退出，退出码: {exit_code}", level="INFO" if exit_code == 0 else "ERROR")

    def get_status(self) -> dict[str, Any]:
        """返回完整的系统状态视图。"""
        is_alive = self.is_running()
        health_data = {}
        if os.path.exists(self.health_path):
            try:
                with open(self.health_path, "r", encoding="utf-8") as f:
                    health_data = json.load(f)
            except Exception:
                pass

        # 计算心跳新鲜度
        heartbeat_age = None
        last_hb = health_data.get("last_heartbeat_at")
        if last_hb:
            try:
                hb_dt = datetime.strptime(last_hb, "%Y-%m-%d %H:%M:%S")
                heartbeat_age = max(0.0, round((datetime.now() - hb_dt).total_seconds(), 1))
            except Exception:
                pass

        # 检查配置脏状态（配置已在磁盘修改但尚未重启 Worker 生效）
        is_dirty = False
        dirty_fields = []
        if is_alive and self._last_started_config is not None:
            try:
                current_cfg = Config(self.config_path).to_dict()
                for k, v in current_cfg.items():
                    # 忽略频繁波动的只读状态字段
                    if k in ("last_cookie_warn_date", "last_cookie_update_time"):
                        continue
                    if self._last_started_config.get(k) != v:
                        is_dirty = True
                        dirty_fields.append(k)
            except Exception:
                pass

        # 确定实际状态
        if not is_alive:
            status = "stopped" if self._desired_mode == "stopped" else "inactive"
            reason = self._start_error or ("服务已由用户停止" if self._desired_mode == "stopped" else "Worker 未在运行")
        else:
            status = health_data.get("status", "running")
            reason = health_data.get("reason", "正在运行")

        session_info = self.get_session_info()

        return {
            "worker_running": is_alive,
            "desired_mode": self._desired_mode,
            "actual_mode": health_data.get("server_name") and health_data.get("status") and (self._last_started_config or {}).get("mode", self._desired_mode),
            "status": status,
            "reason": reason,
            "uptime_seconds": health_data.get("uptime_seconds", 0.0),
            "heartbeat_age_seconds": heartbeat_age,
            "last_heartbeat_at": last_hb,
            "server_name": health_data.get("server_name", "雨课堂"),
            "classroom_id": health_data.get("classroom_id", ""),
            "classroom_url": health_data.get("classroom_url", ""),
            "active_question_id": health_data.get("active_question_id", ""),
            "error_message": health_data.get("error_message", ""),
            "config_dirty": is_dirty,
            "config_dirty_fields": dirty_fields,
            "session": session_info,
        }

    def _get_server_url(self) -> str:
        try:
            cfg = Config(self.config_path)
            server_name = cfg.get("yuketang_server", "雨课堂")
            return YUKETANG_SERVERS.get(server_name, "https://www.yuketang.cn")
        except Exception:
            return "https://www.yuketang.cn"

    def get_session_info(self) -> dict[str, Any]:
        """读取会话元数据（绝不回显具体 Cookie 明文）。"""
        if not os.path.exists(self.session_path):
            return {"exists": False, "valid": False, "cookie_count": 0, "mtime": None, "domains": []}

        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(self.session_path)).strftime("%Y-%m-%d %H:%M:%S")
            with open(self.session_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            server_url = self._get_server_url()
            valid, reason = validate_session_data(data, expected_base_url=server_url)
            cookies = data.get("cookies", []) if isinstance(data, dict) else []
            domains = sorted(list({c.get("domain", "") for c in cookies if isinstance(c, dict) and c.get("domain")}))

            return {
                "exists": True,
                "valid": valid,
                "validation_reason": reason,
                "cookie_count": len(cookies),
                "mtime": mtime,
                "domains": domains,
            }
        except Exception as e:
            return {"exists": True, "valid": False, "validation_reason": f"解析异常: {e}", "cookie_count": 0, "mtime": None, "domains": []}

    def import_session(self, session_data: dict[str, Any]) -> tuple[bool, str]:
        """导入并校验会话凭证；校验不通过绝不覆写现有会话。"""
        server_url = self._get_server_url()
        valid, reason = validate_session_data(session_data, expected_base_url=server_url)
        if not valid:
            return False, f"会话数据校验失败: {reason}"

        with self._lock:
            was_running = (self._process is not None and self._process.poll() is None)
            current_mode = self._desired_mode
            if was_running:
                self.log_buffer.append("正在更新会话，暂停当前运行的 Worker...", level="INFO")
                self._stop_locked()

            try:
                temp_file = f"{self.session_path}.tmp"
                with open(temp_file, "w", encoding="utf-8") as f:
                    json.dump(session_data, f, ensure_ascii=False, indent=2)
                os.replace(temp_file, self.session_path)
                self.log_buffer.append("新登录会话已成功原子写入磁盘", level="INFO")

                if was_running and current_mode in ("observe", "auto"):
                    self.log_buffer.append(f"正在以原模式 ({current_mode}) 重新启动 Worker...", level="INFO")
                    self.start(current_mode)

                return True, "会话导入并保存成功"
            except Exception as e:
                self.log_buffer.append(f"会话保存异常: {e}", level="ERROR")
                return False, f"保存会话文件异常: {e}"

    def get_records(self, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """安全读取 records.db，返回答题历史列表与聚合性能分位数。"""
        if not os.path.exists(self.records_db_path):
            return {"total": 0, "records": [], "stats": {}}

        try:
            conn = sqlite3.connect(f"file:{self.records_db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # 检查表是否存在
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='quiz_records'")
            if not cursor.fetchone():
                conn.close()
                return {"total": 0, "records": [], "stats": {}}

            # 获取总数
            cursor.execute("SELECT COUNT(*) FROM quiz_records")
            total = cursor.fetchone()[0]

            # 获取表字段清单以保持向下兼容
            cursor.execute("PRAGMA table_info(quiz_records)")
            col_names = {row[1] for row in cursor.fetchall()}

            has_classroom = "classroom_id" in col_names
            has_lesson = "lesson_id" in col_names
            has_winning = "winning_model" in col_names
            has_latency = "latency_ms" in col_names
            has_total = "total_end_to_end_ms" in col_names

            fields = ["id", "account_id", "question_id", "stage", "detection_source", "submitted_answer", "submission_confirmed", "error_reason", "created_at", "updated_at"]
            if has_classroom:
                fields.append("classroom_id")
            elif has_lesson:
                fields.append("lesson_id as classroom_id")

            if has_winning:
                fields.append("winning_model as ai_model")
            elif "ai_model" in col_names:
                fields.append("ai_model")

            if has_latency:
                fields.append("latency_ms as total_end_to_end_ms")
            elif has_total:
                fields.append("total_end_to_end_ms")

            for timing_col in ("detect_to_ready_ms", "ready_to_ai_start_ms", "ai_duration_ms", "ai_to_validated_ms", "validated_to_clicked_ms", "clicked_to_confirmed_ms"):
                if timing_col in col_names:
                    fields.append(timing_col)

            sql = f"SELECT {', '.join(fields)} FROM quiz_records ORDER BY id DESC LIMIT ? OFFSET ?"
            cursor.execute(sql, (limit, offset))
            rows = [dict(row) for row in cursor.fetchall()]

            # 聚合状态统计
            cursor.execute("SELECT stage, COUNT(*) FROM quiz_records GROUP BY stage")
            stage_counts = dict(cursor.fetchall())

            # 耗时统计（样本数 >= 1 时才计算分位数）
            duration_col = "latency_ms" if has_latency else ("total_end_to_end_ms" if has_total else None)
            all_durations = []
            if duration_col:
                cursor.execute(f"SELECT {duration_col} FROM quiz_records WHERE {duration_col} IS NOT NULL AND {duration_col} > 0")
                all_durations = [r[0] for r in cursor.fetchall()]

            stats = {
                "confirmed": stage_counts.get(STAGE_CONFIRMED, 0),
                "skipped": stage_counts.get(STAGE_SKIPPED, 0),
                "failed": stage_counts.get(STAGE_FAILED, 0),
                "unknown": stage_counts.get(STAGE_UNKNOWN, 0),
            }
            if all_durations:
                sorted_d = sorted(all_durations)
                n = len(sorted_d)
                stats["p50_ms"] = round(sorted_d[int(n * 0.50)], 1)
                stats["p90_ms"] = round(sorted_d[min(n - 1, int(n * 0.90))], 1)
                stats["p95_ms"] = round(sorted_d[min(n - 1, int(n * 0.95))], 1)
                stats["mean_ms"] = round(sum(sorted_d) / n, 1)

            conn.close()
            return {"total": total, "records": rows, "stats": stats}
        except Exception as e:
            logger.warning("读取 records.db 异常: %s", e)
            return {"total": 0, "records": [], "stats": {}, "error": str(e)}
