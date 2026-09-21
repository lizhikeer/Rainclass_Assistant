"""通知模块 - 微信推送通知（有界队列、告警去重合并、状态可观察）。"""

import logging
import queue
import threading
import time
from concurrent.futures import Future
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

DEFAULT_QUEUE_SIZE = 50
DEFAULT_DEDUP_WINDOW_SECONDS = 300.0  # 5 分钟内相同告警合并
MAX_RETRY_COUNT = 2                   # 失败低频重试上限


class _NotificationTask:
    """包装单次通知发送任务。"""

    def __init__(self, title: str, content: str, future: Optional[Future] = None) -> None:
        self.title = title
        self.content = content
        self.future = future or Future()
        self.created_at = time.time()
        self.retries = 0


class NotificationService:
    """通过 xxtui 平台发送微信通知。

    特性：
    1. 有界队列与专属消费线程，队列满时不阻塞业务；
    2. 重复告警合并节流，避免网络抖动或刷题产生告警风暴；
    3. 区分“尝试发送”与“发送成功”，失败支持有限次低频退避重试；
    4. 绝不阻塞答题与浏览器页面主线程。
    """

    def __init__(
        self,
        api_key: str = "",
        queue_size: int = DEFAULT_QUEUE_SIZE,
        dedup_window: float = DEFAULT_DEDUP_WINDOW_SECONDS,
    ):
        self.api_key = api_key
        self.queue_size = queue_size
        self.dedup_window = dedup_window

        self._queue: queue.Queue[_NotificationTask] = queue.Queue(maxsize=queue_size)
        self._closed = False
        self._lock = threading.Lock()

        # 去重记录: (title, content) -> [last_sent_time, suppressed_count]
        self._dedup_history: dict[tuple[str, str], list[Any]] = {}

        # 统计指标
        self.attempted_count = 0
        self.success_count = 0
        self.failed_count = 0
        self.last_attempt_at: float = 0.0
        self.last_success_at: float = 0.0

        # 后台消费守护线程
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="notification_worker",
            daemon=True,
        )
        self._worker_thread.start()

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _should_suppress_and_aggregate(self, title: str, content: str) -> bool:
        """判断是否属于重复告警并增加合并计数。"""
        now = time.time()
        key = (title, content)

        with self._lock:
            # 清理过期的去重记录
            expired = [k for k, v in self._dedup_history.items() if (now - v[0]) > self.dedup_window]
            for k in expired:
                del self._dedup_history[k]

            if key in self._dedup_history:
                last_time, count = self._dedup_history[key]
                if now - last_time < self.dedup_window:
                    self._dedup_history[key][1] = count + 1
                    logger.info("告警合并节流：[%s] 短时间内重复触发，已压制并累计 (%d 次)", title, count + 1)
                    return True

            self._dedup_history[key] = [now, 1]
            return False

    def send(self, title: str, content: str) -> bool:
        """同步发送微信通知，返回是否成功（记录尝试与成功指标）。"""
        if not self.api_key:
            logger.warning("未设置微信提醒 API Key，通知功能已禁用。")
            return False

        with self._lock:
            self.attempted_count += 1
            self.last_attempt_at = time.time()

        api_url = f"https://www.xxtui.com/xxtui/{self.api_key}"
        headers = {"Content-Type": "application/json"}
        data = {
            "from": "课堂机器人",
            "title": title,
            "content": content,
            "channel": "WX_MP",
        }

        logger.info("正在向 xxtui 发送通知: %s", title)
        try:
            response = requests.post(api_url, headers=headers, json=data, timeout=5)
            response.raise_for_status()
            result = response.json()
            if result.get("code") == 0:
                logger.info("微信通知发送成功: %s", title)
                with self._lock:
                    self.success_count += 1
                    self.last_success_at = time.time()
                return True
            else:
                logger.warning(
                    "微信通知发送失败，错误码：%s，信息：%s",
                    result.get("code"),
                    result.get("message"),
                )
                with self._lock:
                    self.failed_count += 1
                return False
        except (requests.RequestException, ValueError, TypeError) as e:
            logger.error("发送微信通知时发生网络错误: %s", e)
            with self._lock:
                self.failed_count += 1
            return False

    def send_async(self, title: str, content: str) -> Optional[Future]:
        """后台异步发送通知；支持去重合并与有界队列防护。"""
        if self._closed:
            return None

        # 检查是否重复并合并
        if self._should_suppress_and_aggregate(title, content):
            fut: Future = Future()
            fut.set_result(True)  # 已合并，不视为失败
            return fut

        task = _NotificationTask(title, content)
        try:
            self._queue.put_nowait(task)
            return task.future
        except queue.Full:
            logger.warning("通知队列已满（上限 %d 条），丢弃新告警以防内存与响应阻塞: %s", self.queue_size, title)
            task.future.set_result(False)
            return task.future

    def _worker_loop(self) -> None:
        """后台专职通知发送循环。"""
        while not self._closed:
            try:
                task = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                # 检查合并的重复次数
                key = (task.title, task.content)
                count = 1
                with self._lock:
                    if key in self._dedup_history:
                        count = self._dedup_history[key][1]

                actual_content = task.content
                if count > 1:
                    actual_content += f"\n(该告警在近 {int(self.dedup_window)} 秒内累计发生 {count} 次)"

                success = self.send(task.title, actual_content)

                if success:
                    task.future.set_result(True)
                else:
                    # 失败低频重试（最多 2 次，间隔 1.0s，绝不阻塞前端）
                    if task.retries < MAX_RETRY_COUNT and not self._closed:
                        task.retries += 1
                        time.sleep(1.0 * task.retries)
                        try:
                            self._queue.put_nowait(task)
                            continue
                        except queue.Full:
                            pass
                    task.future.set_result(False)
            except Exception as e:
                logger.error("处理通知队列任务异常: %s", e)
                if not task.future.done():
                    task.future.set_result(False)
            finally:
                self._queue.task_done()

    def shutdown(self) -> None:
        """安全关闭通知服务。"""
        if self._closed:
            return
        self._closed = True
        # 清空剩余排队任务
        while not self._queue.empty():
            try:
                t = self._queue.get_nowait()
                if not t.future.done():
                    t.future.cancel()
                self._queue.task_done()
            except Exception:
                break
        self._worker_thread.join(timeout=2.0)
