"""生命周期与状态管理模块 - 面向 NAS / 容器长期稳定运行。

提供服务状态枚举、心跳维护、状态切换原因记录与 health.json 原子持久化。
支持健康检查区分“进程存活(alive)”与“业务就绪(ready)”。
"""

import json
import logging
import os
import tempfile
import time
from enum import Enum
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class ServiceState(str, Enum):
    """服务运行状态枚举。"""
    STARTING = "starting"          # 正在初始化配置、浏览器与组件
    NEEDS_LOGIN = "needs_login"    # 会话缺失或失效，等待导入/重新登录
    WAITING_CLASS = "waiting_class"# 正常运行中，暂无正在进行的课堂（健康状态，非故障）
    MONITORING = "monitoring"      # 已进入课堂，正在监控签到与习题
    ANSWERING = "answering"        # 正在处理题目（图片分析、AI决策或提交）
    STOPPING = "stopping"          # 收到停止信号，正在安全收尾与刷盘
    STOPPED = "stopped"            # 服务已正常退出
    ERROR = "error"                # 发生不可恢复的致命异常退出


class StatusTracker:
    """运行状态跟踪与健康信息落盘器。

    特性：
    1. 记录当前状态与状态切换的具体原因（reason）；
    2. 维护单调心跳时间与挂钟时间（last_heartbeat_at）；
    3. 支持附加账号、当前课堂、活跃题目等上下文；
    4. 原子写出 health.json（临时文件 + os.replace），供外部 CLI / Docker healthcheck 读取；
    5. 区分进程存活(alive)与业务就绪(ready)。
    """

    def __init__(
        self,
        health_file: Optional[Path | str] = None,
        account_id: str = "default",
        server_name: str = "",
    ) -> None:
        self.health_file = Path(health_file).resolve() if health_file else None
        self.account_id = account_id
        self.server_name = server_name

        self._state: ServiceState = ServiceState.STARTING
        self._reason: str = "服务初始化中"
        self._last_heartbeat_time: float = time.time()
        self._last_heartbeat_monotonic: float = time.monotonic()
        self._start_time: float = time.time()

        self._classroom_id: str = ""
        self._classroom_url: str = ""
        self._active_question_id: str = ""
        self._error_message: str = ""
        self._retry_count: int = 0

    @property
    def state(self) -> ServiceState:
        return self._state

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def is_alive(self) -> bool:
        """进程存活判断：只要不是已停止状态，均属于进程存活。"""
        return self._state not in (ServiceState.STOPPED, ServiceState.ERROR)

    @property
    def is_ready(self) -> bool:
        """业务就绪判断：能够执行或准备执行监控答题（waiting_class/monitoring/answering）。"""
        return self._state in (
            ServiceState.WAITING_CLASS,
            ServiceState.MONITORING,
            ServiceState.ANSWERING,
        )

    def set_state(
        self,
        state: ServiceState,
        reason: str = "",
        classroom_id: Optional[str] = None,
        classroom_url: Optional[str] = None,
        active_question_id: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """变更运行状态并记录原因。"""
        prev_state = self._state
        self._state = state
        if reason:
            self._reason = reason

        if classroom_id is not None:
            self._classroom_id = classroom_id
        if classroom_url is not None:
            self._classroom_url = classroom_url
        if active_question_id is not None:
            self._active_question_id = active_question_id
        if error_message is not None:
            self._error_message = error_message

        self.heartbeat()

        if prev_state != state:
            logger.info("服务状态变更: [%s] -> [%s] (原因: %s)", prev_state.value, state.value, self._reason)

    def heartbeat(self) -> None:
        """刷新心跳并原子落盘 health.json。"""
        self._last_heartbeat_time = time.time()
        self._last_heartbeat_monotonic = time.monotonic()
        self._dump_health_file()

    def set_error(self, message: str) -> None:
        """记录错误并切换至 ERROR 状态。"""
        self._error_message = message
        self.set_state(ServiceState.ERROR, reason=message, error_message=message)

    def to_dict(self) -> dict[str, Any]:
        """构建健康检查可导出的字典结构。"""
        now = time.time()
        heartbeat_age = max(0.0, round(now - self._last_heartbeat_time, 1))
        uptime = max(0.0, round(now - self._start_time, 1))

        return {
            "status": self._state.value,
            "alive": self.is_alive,
            "ready": self.is_ready,
            "reason": self._reason,
            "account_id": self.account_id,
            "server_name": self.server_name,
            "classroom_id": self._classroom_id,
            "classroom_url": self._classroom_url,
            "active_question_id": self._active_question_id,
            "error_message": self._error_message,
            "uptime_seconds": uptime,
            "heartbeat_age_seconds": heartbeat_age,
            "last_heartbeat_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._last_heartbeat_time)),
        }

    def _dump_health_file(self) -> None:
        """原子写入 health.json。"""
        if self.health_file is None:
            return

        try:
            self.health_file.parent.mkdir(parents=True, exist_ok=True)
            data = self.to_dict()
            payload = json.dumps(data, ensure_ascii=False, indent=2)

            # 临时文件同目录写入后原子替换
            temp_path = self.health_file.with_suffix(".tmp")
            with open(temp_path, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())

            os.replace(temp_path, self.health_file)
        except Exception as e:
            logger.debug("写入 health.json 失败: %s", e)
