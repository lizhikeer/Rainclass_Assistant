"""核心 Bot 逻辑 - 自动签到、答题、课程检测。

替代原 main.py 中 AutoClassBotApp 的业务逻辑部分，
使用 Playwright 替代 Selenium。
"""

import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
import traceback
from concurrent.futures import Future
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit

from src.browser import DEFAULT_SERVER, YUKETANG_SERVERS

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from src.ai.models import RoundDecision
from src.cleaner import ArtifactCleaner
from src.status import ServiceState, StatusTracker
from src.storage import (
    QuizStorage,
    STAGE_AI_REQUESTED,
    STAGE_CONFIRMED,
    STAGE_DETECTED,
    STAGE_FAILED,
    STAGE_OPTIONS_CLICKED,
    STAGE_SKIPPED,
    STAGE_SUBMITTING,
    STAGE_UNKNOWN,
)

logger = logging.getLogger(__name__)

# 重试间隔（秒）
RETRY_DELAY = 10

# 点击课程后，课堂页可能需要等待后端创建并完成首屏渲染。
# 60 秒：等待期间不产生新调用；超时后关闭本轮新开的标签页。
CLASSROOM_OPEN_TIMEOUT = 60

# 首页 DOM 刷新间隔（秒）
HOME_REFRESH_INTERVAL = 600

# 课程标签页数上限
MAX_ENTRY_TABS = 5

# 标签页超限警告的节流间隔（秒）。
TAB_LIMIT_WARN_INTERVAL = 300

# 雨课堂下课后会把该消息永久保留在课堂时间线中。
CLASS_ENDED_SELECTOR = '//div[@title="下课啦！" and contains(@class, "timeline__msg")]'

# Cookie 有效期（估算）
COOKIE_VALID_DAYS = 14

# 有效的选择题选项
VALID_OPTIONS = ["A", "B", "C", "D", "E", "F", "G"]

# 答题倒计时
#   <div class="time-box"><div class="timing timing--number">倒计时 14:36</div></div>
COUNTDOWN_SELECTOR = ".slide__cmp .time-box .timing"
# 分、秒均补零到两位；服务端推送到达前文本是「倒计时 --:--」。
COUNTDOWN_PATTERN = re.compile(r"倒计时\s*(\d{1,3}):([0-5]\d)")
# 倒计时不再显示数字时的已知文案：(文案, 日志说明)
COUNTDOWN_STATE_WORDS = (
    ("已完成", "题目已提交"),
    ("作答已结束", "作答时间已耗尽"),
    ("题目不限时", "该题不限时"),
    ("题目续时", "该题刚被续时"),
    ("老师可能会随时结束答题", "该题不限时，老师可随时收题"),
)

from src.timing import QuizTimingTracker


class BotState:
    """Bot 运行状态枚举（完全对齐长期运行规范并保持旧常量兼容）。"""
    STARTING = "starting"
    IDLE = "starting"
    NEEDS_LOGIN = "needs_login"
    WAITING_FOR_CLASS = "waiting_class"
    IN_CLASSROOM = "monitoring"
    MONITORING = "monitoring"
    ANSWERING = "answering"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


class Bot:
    """课堂自动化 Bot。在独立线程或进程中运行主循环。"""

    def __init__(
        self,
        config: "Config",           # type: ignore
        browser: "BrowserManager",  # type: ignore
        ai_service: "AIService",    # type: ignore
        notification: "NotificationService",  # type: ignore
        stop_event: threading.Event,
        auto_answer: bool = True,
        mode: Optional[str] = None,
        metrics_file: Optional[str | os.PathLike] = None,
        status_tracker: Optional[StatusTracker] = None,
        storage: Optional[QuizStorage] = None,
        cleaner: Optional[ArtifactCleaner] = None,
    ):
        self.config = config
        self.browser = browser
        self.ai = ai_service
        self.notification = notification
        self.stop_event = stop_event
        self.status_tracker = status_tracker
        self.storage = storage
        self.cleaner = cleaner
        self.account_id = str(self.config.get("account", "default")) if self.config else "default"

        # 模式判定：优先级 mode 参数 > config.get("mode") > auto_answer 参数
        if mode is not None:
            self.mode = mode
            self.auto_answer = (mode == "auto")
        elif config is not None and config.get("mode") in ("auto", "observe"):
            self.mode = config.get("mode")
            self.auto_answer = (self.mode == "auto")
        else:
            self.auto_answer = auto_answer
            self.mode = "auto" if auto_answer else "observe"

        self.metrics_file = metrics_file
        self.state = BotState.IDLE
        self._consecutive_errors = 0
        self._last_clean_time = 0.0
        self._current_tracker: Optional[QuizTimingTracker] = None
        self._last_quiz_detected_time: Optional[float] = None
        self._last_quiz_detection_source: str = "exercise_page"

        self._skip_answer_logged = False
        self._question_states: dict[str, tuple[str, float, int]] = {}
        self._last_notify_time = 0.0
        self._signed_in = False
        self._last_classroom_url = ""
        self._last_sign_in_notify = 0.0
        self._answer_future: Optional[Future[str]] = None
        self._answer_question_id = ""
        self._answer_exercise_path = ""
        self._last_unidentified_log = 0.0
        # 自动截断：记录已触发过的题目，避免同一题重复请求；等待首个答案时按间隔提示。
        self._auto_truncated_question_id = ""
        self._last_auto_truncate_wait_log = 0.0
        self._exercise_html_saved = False
        self._ended_lesson_ids: set[str] = set()
        self._waiting_for_class_logged = False
        self._home_refreshed_at = 0.0  # 首页最近一次刷新/导航的时刻（monotonic）
        self._last_tab_limit_warn = 0.0  # 标签页超限警告节流
        self._request_generation: int = 0  # 题目请求代际号（切题/换题时递增）
        self._answer_generation: int = 0  # 当前在途 AI 请求所绑定的代际号
        self._last_processed_question_id: str = ""  # 最近处理的题目标识
        self._last_ended_check_time = 0.0  # 跨标签页下课深度扫描节流时间戳

    def log(self, message: str) -> None:
        """统一日志输出（只 emit 一次）。

        直接走 logging 根 logger 的 QueueHandler：
        QueueListener 会把同一条记录同时派发给文件处理器(TimedRotatingFileHandler，
        写入 log/bot.log，每天零点轮转为 log/bot_YYYY-MM-DD.log)
        与 GUI 处理器(_GuiLogHandler)，因此文件与界面都会各收到一次。
        切勿再二次 emit（例如同时走其他打印通道），否则同一条日志会打印两遍。
        """
        logger.info(message)

    def _update_state(self, state: str, reason: str = "", **kwargs) -> None:
        """更新 Bot 状态并同步上报给 StatusTracker。"""
        self.state = state
        if self.status_tracker is not None:
            try:
                svc_state = ServiceState(state)
            except ValueError:
                svc_state = ServiceState.MONITORING
            self.status_tracker.set_state(svc_state, reason=reason, **kwargs)

    def heartbeat(self) -> None:
        """刷新心跳。"""
        if self.status_tracker is not None:
            self.status_tracker.heartbeat()

    def _maybe_run_cleaner(self) -> None:
        """低频执行数据目录工件清理。"""
        if self.cleaner is None:
            return
        now = time.monotonic()
        if now - self._last_clean_time >= 3600.0:
            self._last_clean_time = now
            try:
                self.cleaner.clean()
            except Exception as e:
                logger.debug("执行工件清理异常: %s", e)

    def _int_setting(self, key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    def _notify(self, title: str, content: str) -> None:
        """通知是非关键旁路；支持异步实现且绝不阻断答题。"""
        try:
            send_async = getattr(self.notification, "send_async", None)
            if callable(send_async):
                send_async(title, content)
            else:
                self.notification.send(title, content)
        except Exception as e:
            self.log(f"通知发送失败：{e}")

    # ==================== 时间窗口 ====================

    @staticmethod
    def _parse_hhmm(value: str):
        """解析 'HH:MM' 为 (时, 分) 元组，失败返回 None。"""
        try:
            h, m = value.split(":")
            return int(h), int(m)
        except Exception:
            return None

    @staticmethod
    def _in_time_window(current: str, start: str, end: str) -> bool:
        """判断 current(HH:MM) 是否落在 [start, end] 内，支持跨午夜窗口。

        - start <= end：普通窗口，要求 start <= current <= end。
        - start > end（如 22:00 - 06:00）：跨午夜，要求 current >= start 或 current <= end。
        """
        ct = Bot._parse_hhmm(current)
        st = Bot._parse_hhmm(start)
        et = Bot._parse_hhmm(end)
        if ct is None or st is None or et is None:
            return False
        cur = ct[0] * 60 + ct[1]
        s = st[0] * 60 + st[1]
        e = et[0] * 60 + et[1]
        if s <= e:
            return s <= cur <= e
        return cur >= s or cur <= e

    # ==================== 主循环 ====================

    def run(self) -> None:
        """Bot 主循环（在后台线程或独立 Worker 中执行）。

        浏览器创建/导航/关闭全在本线程内完成，避免 Playwright 跨线程报错。
        """
        try:
            self._update_state(BotState.STARTING, "正在启动浏览器与检查会话")
            # 浏览器创建、使用和关闭都留在 Bot 线程内。
            if not self.browser.start():
                self._update_state(BotState.ERROR, "浏览器启动失败")
                self.log("浏览器启动失败，无法开始运行。")
                return
            self.log("浏览器已就绪。")

            if not getattr(self.browser, "has_session", False):
                self._update_state(BotState.NEEDS_LOGIN, "未检测到有效的登录会话")
                self.log("⚠ [NEEDS_LOGIN] 未检测到有效的登录会话（browser_state.json）。")
                self.log("修复指引：请在具有图形界面的环境登录雨课堂，将导出的 browser_state.json 存放到数据目录中，然后重启本服务。")
                return
            elif not (self.browser.navigate_to_class() and self.browser.is_logged_in()):
                self._update_state(BotState.NEEDS_LOGIN, "登录会话无效或已过期")
                self.log("⚠ [NEEDS_LOGIN] 登录会话无效或已过期（未能通过学生端页面登录校验）。")
                self.log("修复指引：请重新获取有效的 browser_state.json 并覆盖数据目录中的同名文件。")
                return

            self.log("登录会话有效。")
            # 适当时机原子保存有效会话（节流保护）
            self.browser.save_session(force=False)
            self._update_state(BotState.WAITING_FOR_CLASS, "登录会话有效，开始检索课程")

            while not self.stop_event.is_set():
                self.heartbeat()
                self._maybe_run_cleaner()
                check_interval = self._int_setting("check_interval", 60, 5, 3600)
                current_time = time.strftime("%H:%M", time.localtime())
                start_time = self.config.get("start_time", "07:00")
                end_time = self.config.get("end_time", "22:00")

                self._check_cookie_warning()

                if self._in_time_window(current_time, start_time, end_time):
                    # 具体检查日志由 _get_into_class 输出，避免每轮重复打印。
                    self._get_into_class()
                else:
                    self.log(
                        f"当前时间不在检查时间段内 ({start_time} - {end_time})，跳过检查。"
                    )

                # 每轮统一等待，无论是否在时间窗口内都按 check_interval 节奏休眠，
                # 避免忙等空转吃满 CPU（原代码此处被错误缩进到 while 之外）。
                self.stop_event.wait(check_interval)

        except Exception:
            self._update_state(BotState.ERROR, f"主循环发生意外错误: {traceback.format_exc()}")
            self.log(f"发生意外错误：{traceback.format_exc()}")
        finally:
            self.browser.stop()
            self.ai.shutdown()
            if self.notification:
                self.notification.shutdown()
            if self.state not in (BotState.NEEDS_LOGIN, BotState.ERROR):
                self._update_state(BotState.STOPPED, "服务已退出，浏览器已关闭")
            self.log("服务已退出，浏览器已关闭。")

    # ==================== 课程检测 ====================

    def _log_waiting_for_class_once(self) -> None:
        """每个等待课程阶段只输出一次状态，后台检测照常继续。"""
        if self._waiting_for_class_logged:
            return
        self._waiting_for_class_logged = True
        self.log("未找到可进入的课程")
        self.log("等待课程中……")

    def _get_into_class(self) -> None:
        """进入正在进行的课程——优先复用已有课堂标签页，没有才重新导航。"""
        while not self.stop_event.is_set():
            # 1. 先找已有课堂标签页
            classroom_page = self._find_existing_classroom()
            if classroom_page:
                self.log("检测到已有课堂标签页，直接进入。")
                self._run_classroom_loop(classroom_page)
                return

            # 2. 没有 → 导航到主页面找课
            if not self.browser.ensure_running():
                self.log(f"浏览器不可用，{RETRY_DELAY} 秒后重试...")
                self.stop_event.wait(RETRY_DELAY)
                continue

            # run() 启动时已经为登录校验导航过首页。避免紧接着重复刷新，
            # 否则第一次课程检测可能仍拿着刷新前的页面状态。
            try:
                on_home_page = self._is_home_page(self.browser.page)
            except Exception:
                on_home_page = False
            if not on_home_page:
                if not self.browser.navigate_to_class():
                    self.stop_event.wait(RETRY_DELAY)
                    continue
                self._home_refreshed_at = time.monotonic()
            elif time.monotonic() - self._home_refreshed_at >= HOME_REFRESH_INTERVAL:
                # 定时刷新首页：已渲染的 DOM 不会随下课自动更新（见常量注释）。
                # 刷新失败不阻塞本轮，下一轮会重试。
                if self.browser.refresh(self.browser.page):
                    self._home_refreshed_at = time.monotonic()

            page = self.browser.page
            self._debug_dump(page, "main-page")

            onlesson_selector = ".onlesson"

            try:
                page.wait_for_selector(onlesson_selector, timeout=10_000)

                # 首页加载期间，之前的进入请求可能已经创建并加载了课堂页。
                # 必须在再次点击课程前刷新标签页扫描结果，避免重复进入。
                classroom_page = self._find_classroom_in_pages(announce=False)
                if classroom_page:
                    self.log("课程列表加载期间检测到课堂页，取消重复进入。")
                    self._run_classroom_loop(classroom_page)
                    return

                # 标签页保险丝：点击课程条会真实打开新标签页，数量异常时
                # 先停止进入并告警，防止泄漏进一步扩大。
                if len(self.browser.pages) >= MAX_ENTRY_TABS:
                    self._warn_tab_limit()
                    return

                pages_before_click = self._snapshot_pages()
                if not self._click_active_class(page):
                    # click() 半途抛异常但页面可能已被打开，同样要做 diff 清理。
                    self._close_pages_opened_after(pages_before_click)
                    self._log_waiting_for_class_once()
                    return

                # 新标签页和同页跳转都轮询验证，不等待经常无法达到的 networkidle。
                classroom_page = self._wait_for_classroom_page(CLASSROOM_OPEN_TIMEOUT)
                self._debug_save_tabs()

                # 诊断：列出所有标签页
                try:
                    pages = self.browser.pages
                    self.log(f"当前共 {len(pages)} 个标签页：")
                    for i, p in enumerate(pages):
                        try:
                            self.log(f"  [{i}] {p.url[:120]}")
                        except Exception:
                            self.log(f"  [{i}] （无法读取 URL）")
                except Exception:
                    pass

                if classroom_page:
                    self.log(f"匹配到课堂页：{classroom_page.url[:100]}")
                    self._debug_dump(classroom_page, "classroom")
                    self._run_classroom_loop(classroom_page)
                    return

                # 首页不是课堂；关闭本轮新开的标签页后交给下一轮重新发现，
                # 不能在首页死循环，更不能让点击产生的标签页累积。
                self._close_pages_opened_after(pages_before_click)
                self._log_waiting_for_class_once()
                return

            except PlaywrightTimeout:
                self._log_waiting_for_class_once()
                return
            except Exception as e:
                self.log(f"课程检测发生错误：{e}，{RETRY_DELAY} 秒后重试...")
                self.stop_event.wait(RETRY_DELAY)
                continue

    def _server_host(self) -> str:
        """所配雨课堂服务器的主机名（小写），用于首页/课堂判定。"""
        name = self.config.get("yuketang_server")
        classroom_url = self.config.get("classroom_url", "")
        if not name or name == DEFAULT_SERVER:
            for s_name, s_url in YUKETANG_SERVERS.items():
                if s_url in classroom_url:
                    name = s_name
                    break
        name = name or DEFAULT_SERVER
        return urlsplit(
            YUKETANG_SERVERS.get(name, YUKETANG_SERVERS[DEFAULT_SERVER])
        ).netloc.lower()

    def _is_home_page(self, page: Page) -> bool:
        """是否处于所配服务器的首页（根路径或 v2/web 应用入口）。"""
        try:
            parts = urlsplit(page.url.strip())
            host = parts.netloc.lower()
            path = parts.path.lower().rstrip("/")
        except Exception:
            return False
        if host != self._server_host():
            return False
        return path in ("", "/v2/web", "/v2/web/index")

    def _wait_for_classroom_page(self, timeout: float) -> Optional[Page]:
        """等待课堂页，并持续处理 Playwright 的新页面事件。"""
        deadline = time.monotonic() + timeout
        while not self.stop_event.is_set() and time.monotonic() < deadline:
            classroom_page = self._find_classroom_in_pages(announce=False)
            if classroom_page:
                return classroom_page
            try:
                # context.pages 是事件驱动的本地快照；threading.Event.wait()
                # 不会泵送 Playwright 消息，新标签页会一直不可见到下一次 API 调用。
                self.browser.page.wait_for_timeout(100)
            except Exception:
                if self.stop_event.wait(0.1):
                    break
        return None

    # ------- 标签页查找 -------

    def _find_existing_classroom(self) -> Optional[Page]:
        """检查已有标签页中是否有课堂（优先 URL 匹配，其次 DOM 匹配）。"""
        return self._find_classroom_in_pages()

    def _find_classroom_in_pages(self, announce: bool = True) -> Optional[Page]:
        """选择通过强校验的课堂页，优先使用明确的 PPT 页面。"""
        candidates = []
        for index, page in enumerate(self.browser.pages):
            try:
                lesson_id = self._lesson_id_from_url(page.url)
                if lesson_id and lesson_id in self._ended_lesson_ids:
                    continue
                if self._is_classroom_page(page):
                    is_ppt = bool(re.search(r"/ppt/\d+(?:[/?#]|$)", page.url.lower()))
                    candidates.append((is_ppt, index, page))
            except Exception:
                continue

        if not candidates:
            return None
        page = max(candidates, key=lambda item: (item[0], item[1]))[2]
        self.browser.use_page(page)
        if announce:
            self.log(f"匹配到课堂页：{page.url[:100]}")
            self._debug_dump(page, "classroom")
        return page

    def _click_active_class(self, page: Page) -> bool:
        """点击具体的在课课程；多课程时先展开汇总栏。"""
        courses = page.locator(".onlesson .jump_lesson__bar")
        if self._click_first_visible(courses):
            return True

        summary = page.locator(".onlesson > .tipbar")
        if not self._click_first_visible(summary):
            return False
        self.stop_event.wait(0.5)
        return self._click_first_visible(page.locator(".onlesson .jump_lesson__bar"))

    @staticmethod
    def _click_first_visible(locator) -> bool:
        """点击 locator 中第一个可见元素。"""
        item = Bot._first_visible(locator)
        if item is None:
            return False
        try:
            item.click(timeout=5_000)
            return True
        except Exception:
            return False

    @staticmethod
    def _first_visible(locator):
        try:
            for index in range(locator.count()):
                item = locator.nth(index)
                if item.is_visible():
                    return item
        except Exception:
            return None
        return None

    @staticmethod
    def _last_visible(locator):
        """返回 locator 中最后一个可见元素，适配保留历史 slide 的页面。"""
        try:
            for index in range(locator.count() - 1, -1, -1):
                item = locator.nth(index)
                if not item.is_visible():
                    continue
                try:
                    in_viewport = item.evaluate(
                        """el => {
                            const r = el.getBoundingClientRect();
                            return r.width > 0 && r.height > 0 && r.bottom > 0 &&
                                r.right > 0 && r.top < window.innerHeight &&
                                r.left < window.innerWidth;
                        }"""
                    )
                    if not in_viewport:
                        continue
                except Exception:
                    pass
                return item
        except Exception:
            return None
        return None

    def _question_scope(self, page: Page):
        """定位当前可见题目容器；未知页面结构时保守回退到页面。"""
        for selector in (
            'section[class*="slide__cmp"]',
            '[class*="slide__cmp"]',
            '[data-question-id]',
            '[data-problem-id]',
            '.quiz-content',
        ):
            try:
                item = self._last_visible(page.locator(selector))
                if item is not None:
                    return item
            except Exception:
                continue
        return page

    def _is_classroom_page(self, page: Page) -> bool:
        """保守识别实时课堂，明确排除首页和普通课程页。"""
        try:
            if page.is_closed():
                return False
            url = page.url.lower()
        except Exception:
            return False

        # /v2/web/* 是 Web 应用页面（首页/课程页/考试页等），永远不是实时课堂；
        # 所配服务器的根路径同理
        try:
            parts = urlsplit(url)
        except Exception:
            parts = None
        if parts is not None and parts.path.startswith("/v2/web"):
            return False
        if (
            parts is not None
            and parts.netloc.lower() == self._server_host()
            and parts.path in ("", "/")
        ):
            return False

        has_timeline = self._has_any(page, [
            '[class*="timeline__"]',
        ])
        has_quiz_prompt = self._has_any(page, [
            'text=你有新的课堂习题',
        ])
        if has_timeline or has_quiz_prompt:
            return True

        url_hint = any(key in url for key in ("/lesson/", "/pro/lesson", "classroom"))
        has_classroom_content = self._has_any(page, [
            'section[class*="slide__cmp"]',
            '[class*="submit-btn"]',
        ])
        return url_hint and has_classroom_content

    @staticmethod
    def _has_any(page: Page, selectors: list[str]) -> bool:
        for selector in selectors:
            try:
                if Bot._first_visible(page.locator(selector)) is not None:
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _lesson_id_from_url(url: str) -> str:
        """从雨课堂课堂 URL 中提取 lesson ID。"""
        try:
            path = urlsplit(url).path.lower()
        except Exception:
            return ""
        match = re.search(r"/lesson/(?:fullscreen/v\d+/)?([a-zA-Z0-9_\-]+)(?:/|$)", path)
        return match.group(1) if match else ""

    def _pages_for_lesson(self, lesson_id: str, current_page: Page) -> list[Page]:
        """返回同一 lesson 的全部页面；无法提取 ID 时仅处理当前页。"""
        if not lesson_id:
            return [current_page]

        pages: list[Page] = []
        try:
            candidates = tuple(self.browser.pages)
        except Exception:
            candidates = ()
        for candidate in candidates:
            try:
                if self._lesson_id_from_url(candidate.url) == lesson_id:
                    pages.append(candidate)
            except Exception:
                continue
        if not any(candidate is current_page for candidate in pages):
            pages.append(current_page)
        return pages

    @staticmethod
    def _has_class_ended_signal(page: Page) -> bool:
        try:
            return page.locator(CLASS_ENDED_SELECTOR).count() > 0
        except Exception:
            return False

    def _handle_class_ended(self, current_page: Page) -> bool:
        """检测同 lesson 的下课消息，并让该课程在本进程内永久收敛。"""
        try:
            lesson_id = self._lesson_id_from_url(current_page.url)
        except Exception:
            lesson_id = ""
        lesson_pages = self._pages_for_lesson(lesson_id, current_page)
        if not any(self._has_class_ended_signal(page) for page in lesson_pages):
            return False

        # 必须先标记再关闭。即使某个 close() 失败，标签页发现流程也不会
        # 再次把这个已经结束的 lesson 当作正在进行的课堂。
        if lesson_id:
            self._ended_lesson_ids.add(lesson_id)
        if self._answer_future is not None:
            self._abandon_pending_answer("检测到下课")

        self.log("检测到下课啦！自动答题已停止。")
        closed = 0
        failed = 0
        for page in lesson_pages:
            try:
                page.close()
                closed += 1
            except Exception as exc:
                failed += 1
                logger.warning("关闭已结束课堂标签页失败：%s", exc)

        lesson_label = lesson_id or "当前课堂"
        self.log(f"已清理课程 {lesson_label} 的 {closed} 个标签页。")
        if failed:
            self.log(f"另有 {failed} 个课堂标签页关闭失败，后续将忽略这些页面。")
        # 根本手段：立即刷新主页，让课程条马上反映"课已结束"的服务器真相，
        # 否则上课期间渲染的首页 DOM 会残留课程条，主循环会反复点击它。
        self._refresh_home_after_class()
        return True

    def _snapshot_pages(self) -> list:
        """点击前快照现有标签页，用于之后识别本轮新开的页面。"""
        try:
            return list(self.browser.pages)
        except Exception:
            return []

    def _close_pages_opened_after(self, before: list) -> None:
        """关闭快照之后新打开且未被识别为课堂的标签页（防泄漏）。"""
        try:
            current = list(self.browser.pages)
        except Exception:
            return
        before_ids = {id(page) for page in before}
        opened = [page for page in current if id(page) not in before_ids]
        closed = 0
        for candidate in opened:
            try:
                if candidate.is_closed():
                    continue
                candidate.close()
                closed += 1
            except Exception as exc:
                logger.warning("关闭本轮新开标签页失败：%s", exc)
        if closed:
            self.log(f"未识别出课堂，已关闭本轮新打开的 {closed} 个标签页。")

    def _warn_tab_limit(self) -> None:
        """标签页达到上限时的节流警告；只警告不自动关闭。"""
        now = time.monotonic()
        if now - self._last_tab_limit_warn < TAB_LIMIT_WARN_INTERVAL:
            return
        self._last_tab_limit_warn = now
        self.log(
            f"标签页数量已达 {len(self.browser.pages)} 个"
            f"（上限 {MAX_ENTRY_TABS}），跳过进入课程以防泄漏，请检查浏览器。"
        )

    def _refresh_home_after_class(self) -> None:
        """下课关闭课堂标签页后立即刷新主页。

        没有存活的首页标签页时跳过：下一轮检测会新建页面并导航，
        天然是新鲜 DOM。
        """
        home_page = None
        try:
            for candidate in reversed(self.browser.pages):
                if not candidate.is_closed() and self._is_home_page(candidate):
                    home_page = candidate
                    break
        except Exception:
            home_page = None
        if home_page is None:
            return
        try:
            self.browser.use_page(home_page)
        except Exception:
            pass
        if self.browser.refresh(home_page):
            self._home_refreshed_at = time.monotonic()

    def _classroom_poll_interval(self) -> float:
        """返回课堂内高频检测间隔（秒）。

        优先读取毫秒配置 classroom_poll_interval_ms（范围 50~5000ms），
        未指定时回退到旧字段 quiz_refresh_interval（秒）。
        """
        ms_val = self.config.get("classroom_poll_interval_ms")
        if ms_val is not None:
            try:
                ms = int(ms_val)
                if 50 <= ms <= 5000:
                    return ms / 1000.0
            except (ValueError, TypeError):
                pass
        return float(self._int_setting("quiz_refresh_interval", 1, 1, 300))

    def _run_classroom_loop(self, page: Page) -> None:
        """在课堂页面内循环签到/答题直到下课。"""
        self._waiting_for_class_logged = False
        self.browser.use_page(page)
        self._last_classroom_url = ""
        self.log("我去上课啦！")
        lesson_id = ""
        try:
            lesson_id = self._lesson_id_from_url(page.url)
        except Exception:
            pass
        self._update_state(
            BotState.MONITORING,
            "已进入课堂，正在监控签到与习题",
            classroom_id=lesson_id,
            classroom_url=page.url,
        )

        invalid_checks = 0

        while not self.stop_event.is_set():
            self.heartbeat()
            poll_interval = self._classroom_poll_interval()
            try:
                # 页面关闭即刻退出
                if page.is_closed():
                    self.log("课堂标签页已关闭，返回课程发现流程。")
                    return

                # 若当前页直接出现下课标记，立即处理
                if self._has_class_ended_signal(page):
                    if self._handle_class_ended(page):
                        return

                # 跨标签页下课深度扫描节流：仅在路由变化或间隔 >= 2.0 秒时执行
                now = time.monotonic()
                cur = page.url
                if (cur != self._last_classroom_url) or (now - self._last_ended_check_time >= 2.0):
                    self._last_ended_check_time = now
                    if self._handle_class_ended(page):
                        return

                if not self._is_classroom_page(page):
                    replacement = self._find_classroom_in_pages(announce=False)
                    if replacement is None:
                        invalid_checks += 1
                        if invalid_checks >= 3:
                            self.log("课堂页面已失效，返回课程发现流程。")
                            return
                        self.stop_event.wait(min(poll_interval, 1.0))
                        continue
                    page = replacement
                    self.browser.use_page(page)
                    self._request_generation += 1
                invalid_checks = 0

                # 检查并处理新题提示
                pages_before_prompt = tuple(self.browser.pages)
                clicked_prompt = self._open_new_quiz(page)
                new_page = None
                if clicked_prompt:
                    # 多条件等待：新标签页、同页切换、同页渲染题目、页面关闭均可即刻退出
                    deadline = time.monotonic() + 3.0
                    while not self.stop_event.is_set() and time.monotonic() < deadline:
                        new_page = self._find_new_classroom_page(pages_before_prompt)
                        if new_page is not None:
                            break
                        if page.is_closed():
                            break
                        if self._is_exercise_page(page) or self._has_answerable_options(page):
                            self._last_quiz_detected_time = time.monotonic()
                            self._last_quiz_detection_source = "prompt_same_page"
                            break
                        self.stop_event.wait(0.04)

                if new_page is not None:
                    page = new_page
                    self.browser.use_page(page)
                    self._request_generation += 1
                    self.log(f"已跟随到新的课堂标签页：{page.url[:100]}")

                cur = page.url
                if cur and cur != self._last_classroom_url:
                    self._last_classroom_url = cur
                    self._request_generation += 1
                    self._signed_in = False
                    path = urlsplit(cur).path.lower()
                    if "exercise" in path:
                        self._last_quiz_detected_time = time.monotonic()
                        self._last_quiz_detection_source = "exercise_navigation"
                        self.log(f"进入新的习题页：{cur[:100]}")
                    elif re.search(r"/ppt(?:/|$)", path):
                        self.log(f"进入新的 PPT 页：{cur[:100]}")

                self._check_and_sign_in(page)

                if self.auto_answer or self.mode == "observe":
                    # AI 异步请求等待期加速：如果已有 Future 在等待，使用短片轮询检查
                    if self._answer_future is not None:
                        if self._answer_future.done():
                            self._complete_pending_answer(page)
                        else:
                            self._maybe_auto_truncate(page)
                            self.stop_event.wait(min(poll_interval, 0.04))
                            continue
                    else:
                        self._answer(page)
                elif not self._skip_answer_logged:
                    self._skip_answer_logged = True
                    self.log("调试模式：已禁用自动答题，课堂页面保持原样（不再重复提示）。")
            except Exception as e:
                # 区分登录失效与网络/页面临时故障
                try:
                    is_logged = self.browser.is_logged_in()
                except Exception:
                    is_logged = True
                if not is_logged:
                    self._update_state(BotState.NEEDS_LOGIN, "运行中登录会话失效（未能通过页面登录校验）")
                    self.log("运行中检测到雨课堂登录会话已失效，正在暂停答题并提醒登录。")
                    self._notify("登录失效提醒", "雨课堂登录已失效，请重新导入会话。")
                    return

                self._consecutive_errors += 1
                if self._consecutive_errors > 5:
                    self._update_state(BotState.ERROR, f"课堂监控连续异常达到上限 (5 次): {e}")
                    self.log(f"课堂监控连续异常达到上限 (5 次)，停止监控：{e}")
                    return

                backoff = min(60.0, 2.0 * (2 ** (self._consecutive_errors - 1)))
                self.log(f"课堂页面处理异常 (第 {self._consecutive_errors}/5 次)：{e}，将在 {backoff:.1f} 秒后退避恢复...")
                self._abandon_pending_answer("网络/页面异常退避恢复，丢弃在途 AI 请求")
                self._request_generation += 1
                if self.stop_event.wait(backoff):
                    return
                try:
                    self.browser.ensure_running()
                except Exception:
                    pass
                continue

            self._consecutive_errors = 0
            self.stop_event.wait(poll_interval)

    def _find_new_classroom_page(self, existing_pages: tuple[Page, ...]) -> Optional[Page]:
        """返回一次明确操作后真正新建并已加载为课堂的标签页。"""
        existing_ids = {id(page) for page in existing_pages}
        for candidate in reversed(self.browser.pages):
            if id(candidate) in existing_ids:
                continue
            try:
                lesson_id = self._lesson_id_from_url(candidate.url)
            except Exception:
                lesson_id = ""
            if lesson_id and lesson_id in self._ended_lesson_ids:
                continue
            if self._is_classroom_page(candidate):
                return candidate
        return None

    def _open_new_quiz(self, page: Page) -> bool:
        """消费雨课堂的新题提示，让页面切到最新题目。"""
        prompt = page.get_by_text("你有新的课堂习题", exact=False)
        if self._click_first_visible(prompt):
            self._last_quiz_detected_time = time.monotonic()
            self._last_quiz_detection_source = "prompt_click"
            self.log("发现新的课堂习题，已点击提示。")
            return True
        return False

    # ==================== 签到 ====================

    def _check_and_sign_in(self, page: Page) -> None:
        """检测并执行签到——同一课堂只签一次。

        精确匹配文本恰为「签到」的可点击元素，避免误匹配「已签到 / 签到记录」等
        静态文案；只有找不到签到按钮属正常情况（静默返回），真正的失败会记日志。
        """
        if self.mode == "observe":
            return
        if not self.config.get("auto_sign_in", True):
            return
        if self._signed_in:
            return
        sign_in_btn = page.get_by_text("签到", exact=True)
        # 先即时检查是否有可见签到按钮，避免无签到时固定等待 5 秒。
        try:
            if sign_in_btn.first.count() == 0 or not sign_in_btn.first.is_visible():
                return
        except Exception:
            return  # 无签到按钮属正常情况

        self.log("检测到签到。")
        now = time.time()
        if now - self._last_sign_in_notify > 60:
            try:
                self._notify("自动签到提醒", "课程有签到任务，正在自动签到。")
                self._last_sign_in_notify = now
            except Exception as e:
                self.log(f"签到通知发送失败：{e}")

        try:
            sign_in_btn.first.click()
            self._signed_in = True
            self.log("已成功自动签到。")
        except Exception as e:
            self.log(f"签到点击失败：{e}")

    # ==================== 答题 ====================

    def _answer(self, page: Page) -> None:
        """
        可答判定 → AI 答题 → 点击选项 → 提交 → 微信通知。

        不判断题型：exercise URL 是题目页的权威信号，提交按钮存在表示
        题目仍可作答，两者同时满足即发起 AI 请求（统一 JSON 提示词）。
        主观/填空题由 AI 返回的 type 识别，仅记录不点击。
        """
        if not self._is_exercise_page(page):
            self._exercise_html_saved = False
            if self._answer_future is not None:
                self._abandon_pending_answer("答题页面已关闭")
            return

        if not self._exercise_html_saved:
            self._exercise_html_saved = True
            self._save_exercise_html(page)

        if self.mode == "observe":
            if not self._skip_answer_logged:
                self._skip_answer_logged = True
                self.log("观察模式：已检测到习题页面，禁止调用 AI、点击选项与提交（页面保持原样）。")
            return

        if not self._has_submit_button(page):
            if self._answer_future is not None:
                self._finish_pending_as_completed("提交答案按钮已消失")
            return

        # AI 请求期间只核对廉价且稳定的路由和提交按钮状态。题目 DOM 会在
        # 渐进渲染时变化，不能用重新计算的内容哈希判断是否切题。
        if self._answer_future is not None:
            if self._answer_future.done():
                self._complete_pending_answer(page)
                return
            # 等待期间按剩余答题时间判断是否需要提前收尾（复用手动截断机制）。
            self._maybe_auto_truncate(page)
            return

        # 当前点击逻辑只支持 data-option 客观题；没有可操作选项时不调用 AI。
        if not self._has_answerable_options(page):
            return

        # --- 触发：exercise 页 + 提交按钮 + 可见选项同时满足才发起 ---
        self._debug_dump(page, "quiz-dom")
        self._handle_quiz(page)

    @staticmethod
    def _is_exercise_page(page: Page) -> bool:
        """URL 路径中包含 exercise 时才认为当前处于题目页。"""
        try:
            return "exercise" in urlsplit(page.url).path.lower()
        except Exception:
            return False

    def _save_exercise_html(self, page: Page) -> None:
        """保存 exercise 页面 HTML（默认关闭，需配置 save_exercise_html 或 debug_mode 开启）。"""
        if not (self.config.get("save_exercise_html", False) or self.config.get("debug_mode", False)):
            return
        now = datetime.now()
        data_dir = os.path.join("data", now.strftime("%Y-%m-%d"))
        path = os.path.join(
            data_dir,
            f"{now.strftime('%H-%M-%S-%f')}-exercise.html",
        )
        try:
            # 必须在 Playwright 线程读取 HTML
            content = page.content()
            os.makedirs(data_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as file:
                file.write(content)
            self.log(f"题目 HTML 已保存：{path}")
        except Exception as e:
            self.log(f"题目 HTML 保存失败：{e}")

    def _handle_quiz(self, page: Page) -> None:
        """发起当前题目的异步 AI 请求（统一 JSON 提示词，不判断题型）。"""
        image_url = self._capture_question_image(page)
        question_id = self._question_id(page, image_url)
        if not question_id:
            now = time.time()
            if now - self._last_unidentified_log > 30:
                self._last_unidentified_log = now
                self.log("无法稳定识别当前题目，跳过自动作答（30 秒内不再提示）。")
            return

        if self._answer_future is not None:
            if question_id != self._answer_question_id:
                self._abandon_pending_answer("检测到新的题目")
            elif self._answer_future.done():
                self._complete_pending_answer(page)
                return
            else:
                return
        if not self._begin_question(question_id):
            return

        # 题目真正变化时递增代际号
        if getattr(self, "_last_processed_question_id", "") != question_id:
            self._last_processed_question_id = question_id
            self._request_generation += 1

        lesson_id = ""
        try:
            lesson_id = self._lesson_id_from_url(page.url)
        except Exception:
            pass

        self._update_state(
            BotState.ANSWERING,
            f"正在作答题目: {question_id}",
            classroom_id=lesson_id,
            active_question_id=question_id,
        )

        detected_time = getattr(self, "_last_quiz_detected_time", None) or time.monotonic()
        detection_source = getattr(self, "_last_quiz_detection_source", "exercise_dom")

        if self.storage:
            # 1. 检查此前是否已经确认作答完毕
            if self.storage.has_confirmed(self.account_id, lesson_id, question_id):
                self.log("持久化记录显示该题目此前已确认提交，跳过重复作答。")
                self._update_state(BotState.MONITORING, "题目已作答，继续监控")
                return

            # 2. 检查此前是否在 submitting/unknown 崩溃
            prev = self.storage.get_latest_record(self.account_id, lesson_id, question_id)
            if prev and prev.get("stage") in (STAGE_SUBMITTING, STAGE_UNKNOWN):
                prev_gen = prev.get("request_generation", self._request_generation)
                if not self._has_submit_button(page):
                    self.storage.record_stage(
                        self.account_id, lesson_id, question_id, prev_gen,
                        stage=STAGE_CONFIRMED, submission_confirmed=1,
                        error_reason="崩溃恢复核对：页面提交按钮已消失",
                    )
                    self.log("恢复核对：页面提交按钮已消失，确认此前已提交成功。")
                    self._update_state(BotState.MONITORING, "恢复核对已提交，继续监控")
                    return
                else:
                    self.storage.record_stage(
                        self.account_id, lesson_id, question_id, prev_gen,
                        stage=STAGE_UNKNOWN, error_reason="崩溃恢复核对状态不明，跳过以防误提交",
                    )
                    self.log("恢复核对：崩溃前状态不明且页面按钮仍在，跳过自动作答以避免误提交。")
                    self._notify("答题恢复提醒", f"题目 {question_id} 崩溃前状态不明，已跳过避免误提交。")
                    self._update_state(BotState.MONITORING, "状态不明已跳过，继续监控")
                    return

            # 记录首次检测
            self.storage.record_stage(
                self.account_id, lesson_id, question_id, self._request_generation,
                stage=STAGE_DETECTED, detection_source=detection_source,
            )

        tracker = QuizTimingTracker(
            question_id=question_id,
            lesson_id=lesson_id,
            account_id="user",
            detection_source=detection_source,
            metrics_file=self.metrics_file,
        )
        tracker.mark("question_detected", detected_time)
        tracker.mark("question_ready")
        self._current_tracker = tracker

        self.log("检测到可作答题目，开始获取 AI 答案。")
        try:
            tracker.mark("ai_request_started")
            if time.time() - self._last_notify_time > 30:
                self._notify(
                    "自动答题提醒",
                    "课程检测到新题目，正在获取 AI 答案...",
                )
                self._last_notify_time = time.time()

            # 统一整题单调预算与截止时间
            rem_sec = self._read_remaining_seconds(page)
            margin = float(self.config.get("submit_time_margin_seconds", 3.0))
            if rem_sec is not None and rem_sec > 0:
                budget = max(0.5, float(rem_sec) - margin)
            else:
                budget = float(self.config.get("ai_total_budget_seconds", 20.0))
            deadline = time.monotonic() + budget

            # 统一准备一份完整题图（支持回退至截图，同轮模型复用）
            screenshot_b64 = self._prepare_question_image_b64(page, image_url=image_url, budget=budget)
            if not screenshot_b64:
                screenshot_b64 = "mock_image_base64"

            future = self.ai.submit_answer(
                image_b64=screenshot_b64,
                deadline=deadline,
                generation=self._request_generation,
            )
            if self.storage:
                self.storage.record_stage(
                    self.account_id, lesson_id, question_id, self._request_generation,
                    stage=STAGE_AI_REQUESTED,
                )
        except Exception as e:
            self.log(f"AI 答题请求启动失败：{e}")
            self._finish_question(question_id, False)
            return

        self._answer_future = future
        self._answer_question_id = question_id
        self._answer_exercise_path = self._exercise_path(page)
        self._answer_generation = self._request_generation

        # 测试桩或缓存结果可能立即完成。
        if future.done():
            self._complete_pending_answer(page)

    def _maybe_auto_truncate(self, page: Page) -> None:
        """按剩余答题时间自动截断多AI等待（复用与手动截断相同的机制）。

        当题目倒计时剩余秒数已不足 auto_truncate_seconds 时，提前结束等待、
        用已返回的答案投票；若此刻还没有任何有效答案，则保持等待，直到出现
        第一个有效答案再截断——否则会把空答案送进提交流程，等于白丢一题。

        与「多AI最大等待」的关系：后者是防止模型调用悬空，本项是防止剩余时间
        不够提交，两者各自独立判断、取先到者。由于本项的目的就是抢在收题前提交，
        其触发优先于最大等待（request_truncate 会让等待循环立即收尾）。

        判断顺序：先做纯内存检查（阈值、是否已截断、多AI是否仍在收集），
        全部通过后才读页面倒计时，避免单AI或本轮已收尾时每个轮询都打 DOM。
        """
        threshold = self._int_setting("auto_truncate_seconds", 0, 0, 3600)
        if threshold <= 0:
            return
        if (
            self._answer_question_id
            and self._auto_truncated_question_id == self._answer_question_id
        ):
            return  # 本题已经自动截断过，不重复请求

        # 截断与进度查询都是 AIService 的公开接口：前者与「手动截断」按钮共用，
        # 后者给出实时有效票数。非多AI作答或测试桩没有这些能力，直接跳过。
        progress_fn = getattr(self.ai, "multi_progress", None)
        truncate_fn = getattr(self.ai, "request_truncate", None)
        if not callable(progress_fn) or not callable(truncate_fn):
            return

        # 先确认多AI收集仍在进行（内存快照），再读倒计时（DOM）。
        progress = progress_fn()
        if not progress.get("active"):
            return  # 本轮收集已收尾，或当前不是多AI作答

        remaining = self._read_remaining_seconds(page)
        if remaining is None or remaining > threshold:
            return

        valid = int(progress.get("valid", 0) or 0)
        if valid <= 0:
            # 一个有效答案都还没有：继续等，交给后续轮询再判断（提示做节流）。
            now = time.time()
            if now - self._last_auto_truncate_wait_log > 30:
                self._last_auto_truncate_wait_log = now
                self.log(f"剩余 {remaining} 秒，但尚无有效答案，继续等待首个答案。")
            return

        truncate_fn()
        self._auto_truncated_question_id = self._answer_question_id
        self.log(
            f"剩余 {remaining} 秒，已自动截断：用已返回的 {valid} 个有效答案投票。"
        )

    def _complete_pending_answer(self, page: Page) -> None:
        future = self._answer_future
        question_id = self._answer_question_id
        answer_gen = getattr(self, "_answer_generation", 0)
        self._answer_future = None
        self._answer_question_id = ""
        if future is None or not question_id:
            return

        if self.stop_event.is_set() or self.state == BotState.STOPPING:
            self.log("服务正在停止退出，已中止新提交发起。")
            return

        lesson_id = ""
        try:
            lesson_id = self._lesson_id_from_url(page.url)
        except Exception:
            pass

        succeeded = False
        try:
            answer_text = future.result()
            if self._current_tracker:
                self._current_tracker.mark("ai_response_received")
                decision = getattr(self.ai, "last_decision", None)
                if isinstance(decision, RoundDecision):
                    self._current_tracker.set_ai_metrics(decision.to_metrics_dict())
            self.log(f"AI 返回：{answer_text}")
            if self._is_failed_answer(answer_text):
                self.log("AI 未返回有效答案，本题不再自动重试。")
                return
            qtype, letters, raw_answer = self._parse_ai_answer(answer_text)
            if qtype is None:
                self.log("AI 返回格式不符合约定，本题不再自动重试。")
                return
            if qtype == "unknown":
                self.log("AI 模型没有可用的视觉能力，已跳过本题。")
                if self.storage:
                    self.storage.record_stage(
                        self.account_id, lesson_id, question_id, answer_gen,
                        stage=STAGE_SKIPPED, error_reason="AI模型无可用视觉能力",
                    )
                succeeded = True
                return
            if qtype in ("fill", "sub"):
                self.log("AI 判定为主观/填空题，不自动作答，本题结束。")
                if self.storage:
                    self.storage.record_stage(
                        self.account_id, lesson_id, question_id, answer_gen,
                        stage=STAGE_SKIPPED, error_reason="主观/填空题跳过",
                    )
                succeeded = True
                return

            # 代际与页面状态核验
            if self._request_generation != answer_gen:
                self.log("AI 返回时题目代际已发生变更，已丢弃旧答案。")
                return
            if not self._is_exercise_page(page):
                self.log("AI 返回前答题页面已关闭，已丢弃旧答案。")
                return
            if self._exercise_path(page) != self._answer_exercise_path:
                self.log("AI 返回前题目路由已经变化，已丢弃旧答案。")
                return
            if not self._has_submit_button(page):
                self.log("AI 返回时提交答案按钮已消失，本题不再重复作答。")
                succeeded = True
                return

            if letters:
                clicked = self._click_options(page, letters)
            else:
                # 无字母：可能是判断题文案（对/错/正确/错误等），按选项文案映射
                clicked = self._click_judgment(page, raw_answer)
                if not clicked:
                    self.log("AI 未能提供有效选项，本题不再自动重试。")
                    self._notify_answer_failure(answer_text)
                    return
            if not clicked:
                return
            # 点击选项后确认选择已生效（按钮进入 can 态），否则提交会命中
            # 灰色按钮而不生效，还会误判为已作答。
            if not self._wait_submit_ready(page):
                self.log("选项点击未生效（提交按钮未进入可提交状态），本题不再自动重试。")
                return
            if self._current_tracker:
                self._current_tracker.mark("answer_validated")

            if self.storage:
                self.storage.record_stage(
                    self.account_id, lesson_id, question_id, answer_gen,
                    stage=STAGE_OPTIONS_CLICKED,
                    submitted_answer=str(letters or raw_answer),
                )

            # 提交前再次确认代际与提交按钮
            if self._request_generation != answer_gen:
                self.log("提交前题目代际已变更，已中止提交。")
                return
            if not self._has_submit_button(page):
                self.log("提交前提交按钮已消失，已中止提交。")
                return

            submit_delay = self._int_setting("submit_delay", 0, 0, 300)
            if submit_delay > 0 and self.stop_event.wait(submit_delay):
                return

            if self.storage:
                self.storage.record_stage(
                    self.account_id, lesson_id, question_id, answer_gen,
                    stage=STAGE_SUBMITTING,
                )

            if self._current_tracker:
                self._current_tracker.mark("submit_clicked")
            succeeded = self._submit_answer(page)
        except Exception as e:
            self.log(f"AI 答题结果处理失败：{e}")
        finally:
            self._finish_question(question_id, succeeded)

    def _finish_pending_as_completed(self, reason: str) -> None:
        """提交按钮消失时结束尚未完成的 AI 请求。"""
        future = self._answer_future
        question_id = self._answer_question_id
        self._answer_future = None
        self._answer_question_id = ""
        self._answer_exercise_path = ""
        self._answer_generation = 0
        if future is not None:
            future.cancel()
        if self._current_tracker is not None:
            self._current_tracker.finish(True, error_reason=reason)
            self._current_tracker = None
        if question_id:
            self.log(f"{reason}，本题已结束处理。")
            self._finish_question(question_id, True)

    def _abandon_pending_answer(self, reason: str) -> None:
        future = self._answer_future
        question_id = self._answer_question_id
        self._answer_future = None
        self._answer_question_id = ""
        self._answer_exercise_path = ""
        self._answer_generation = 0
        if future is not None:
            future.cancel()
        if self._current_tracker is not None:
            self._current_tracker.finish(False, error_reason=reason)
            self._current_tracker = None
        if question_id:
            self.log(f"{reason}，已丢弃旧题 AI 请求。")
            self._finish_question(question_id, False)

    def _begin_question(self, question_id: str) -> bool:
        """进入题目处理状态；同一道题只允许发起一次 AI 请求。"""
        state, _, attempts = self._question_states.get(
            question_id, ("pending", 0.0, 0)
        )
        if state in ("inflight", "completed", "failed"):
            return False
        self._set_question_state(question_id, "inflight", attempts + 1)
        return True

    def _finish_question(self, question_id: str, succeeded: bool) -> None:
        attempts = self._question_states.get(question_id, ("", 0.0, 1))[2]
        state = "completed" if succeeded else "failed"
        self._set_question_state(question_id, state, attempts)
        if self._current_tracker is not None:
            self._current_tracker.finish(succeeded)
            self._current_tracker = None

        if self.storage:
            lesson_id = ""
            try:
                lesson_id = self._lesson_id_from_url(self.browser.page.url)
            except Exception:
                pass
            gen = self._answer_generation or self._request_generation
            if succeeded:
                self.storage.record_stage(
                    self.account_id, lesson_id, question_id, gen,
                    stage=STAGE_CONFIRMED, submission_confirmed=1,
                )
            else:
                self.storage.record_stage(
                    self.account_id, lesson_id, question_id, gen,
                    stage=STAGE_FAILED,
                )

        if succeeded:
            self.log("当前题目已确认处理完成。")
        else:
            self.log("当前题目处理失败，本轮不再自动重试。")
        self._update_state(BotState.MONITORING, "题目处理结束，继续监控课堂")

    @staticmethod
    def _exercise_path(page: Page) -> str:
        """返回题目路由，用于校验异步答案仍属于原题页面。"""
        try:
            return urlsplit(page.url).path.lower()
        except Exception:
            return ""

    def _set_question_state(self, question_id: str, state: str, attempts: int) -> None:
        # 仅保留最近 200 道题；需要跨进程历史时再持久化。
        self._question_states.pop(question_id, None)
        self._question_states[question_id] = (state, time.time(), attempts)
        while len(self._question_states) > 200:
            self._question_states.pop(next(iter(self._question_states)))

    @staticmethod
    def _parse_ai_answer(text: str) -> tuple[Optional[str], list[str], str]:
        """解析统一提示词的 JSON 返回。

        约定格式（见 AIService.PROMPT_ANSWER）：
          {"type":"single","answers":"A"}
          {"type":"multi","answers":["A","B","D"]}
          {"type":"fill"} / {"type":"sub"} / {"type":"unknown"}
        返回 (题型, 选项字母列表, answers 原始字符串)；解析失败返回 (None, [], "")。
        客观题（single/multi）解析不出字母视为失败，交给上层重试。
        """
        if not text:
            return None, [], ""
        s = text.strip()
        # 容错：剥掉被提示词禁止但仍可能出现的代码块围栏
        s = re.sub(r"^```(?:json)?", "", s, flags=re.IGNORECASE).strip()
        s = re.sub(r"```$", "", s).strip()
        start, end = s.find("{"), s.rfind("}")
        if start == -1 or end <= start:
            return None, [], ""
        try:
            data = json.loads(s[start : end + 1])
        except json.JSONDecodeError:
            return None, [], ""
        if not isinstance(data, dict):
            return None, [], ""
        qtype = str(data.get("type", "")).strip().lower()
        if qtype not in ("single", "multi", "fill", "sub", "unknown"):
            return None, [], ""
        if qtype == "unknown":
            return "unknown", [], ""
        raw = data.get("answers", "")
        letters: list[str] = []
        if isinstance(raw, list):
            for item in raw:
                letters.extend(Bot._parse_options(str(item)))
        elif isinstance(raw, str):
            letters = Bot._parse_options(raw)
        if (
            qtype in ("single", "multi")
            and not letters
            and not (isinstance(raw, str) and Bot._parse_judgment(raw) is not None)
        ):
            # 客观题无字母且 answers 也不是判断题文案（对/错等）→ 解析失败重试
            return None, [], ""
        return qtype, letters, raw if isinstance(raw, str) else ""

    @staticmethod
    def _parse_options(text: str) -> list[str]:
        """严格解析 A-G，拒绝解释文本中的英文单词和错误码。"""
        if not text:
            return []
        match = re.fullmatch(
            r"\s*(?:(?:答案(?:是|为)?|ANSWER)\s*[:：]?\s*)?"
            r"([A-G](?:\s*[,，、/\s]\s*[A-G])*)\s*[。.]?\s*",
            text.upper(),
        )
        if not match:
            return []
        letters = re.findall(r"[A-G]", match.group(1))
        seen: set[str] = set()
        result: list[str] = []
        for letter in letters:
            if letter not in seen:
                seen.add(letter)
                result.append(letter)
        return result

    def _question_id(self, page: Page, image_url: Optional[str]) -> str:
        """优先使用稳定题目 ID，否则退化到题干、选项和图片摘要。"""
        try:
            data = page.evaluate(
                """() => {
                    const visible = el => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.display !== 'none' && style.visibility !== 'hidden' &&
                            style.opacity !== '0' && rect.width > 0 && rect.height > 0 &&
                            rect.bottom > 0 && rect.right > 0 &&
                            rect.top < window.innerHeight && rect.left < window.innerWidth;
                    };
                    const roots = [...document.querySelectorAll(
                        '[data-question-id], [data-problem-id], [data-slide-id], [class*="slide__cmp"], [class*="question"], .quiz-content'
                    )];
                    const visibleRoots = roots.filter(visible);
                    const idNames = ['data-question-id', 'data-problem-id', 'data-slide-id', 'data-id', 'id'];
                    const root = [...visibleRoots].reverse().find(el =>
                        idNames.some(name => el.getAttribute(name))
                    ) || visibleRoots[visibleRoots.length - 1];
                    if (!root) return null;
                    const id = idNames
                        .map(name => root.getAttribute(name)).find(Boolean) || '';
                    const clone = root.cloneNode(true);
                    const dynamicSelectors = [
                        '[class*="time-box"]',
                        '[class*="countdown"]',
                        '[class*="timing"]',
                        '[class*="submit-btn"]',
                        'button',
                        '.tips'
                    ];
                    dynamicSelectors.forEach(sel => {
                        clone.querySelectorAll(sel).forEach(el => el.remove());
                    });
                    const text = (clone.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 4000);
                    const options = [...root.querySelectorAll('[data-option]')]
                        .filter(visible)
                        .map(el => `${el.getAttribute('data-option') || ''}:${(el.innerText || '').replace(/\\s+/g, ' ').trim()}`);
                    const images = [...root.querySelectorAll('img')]
                        .filter(visible).map(img => img.currentSrc || img.src || '');
                    return {id, text, options, images};
                }"""
            )
        except Exception:
            data = None

        identity: list[str] = []
        if isinstance(data, dict):
            stable_id = str(data.get("id", "")).strip()
            if stable_id:
                identity = ["id", stable_id]
            else:
                options = "|".join(map(str, data.get("options", []) or []))
                images = "|".join(
                    self._stable_image_url(str(url))
                    for url in data.get("images", []) or []
                )
                text = str(data.get("text", "")).strip()
                identity = ["content", text, options, images]
        if not any(identity) and image_url:
            identity = ["image", self._stable_image_url(image_url)]
        if not any(identity):
            return ""

        try:
            page_url = page.url
        except Exception:
            page_url = ""
        scope = self._classroom_scope(page_url)
        digest = hashlib.sha256("\x1f".join(identity).encode("utf-8")).hexdigest()
        return f"{scope}:{digest}"

    @staticmethod
    def _stable_image_url(url: str) -> str:
        parsed = urlsplit(url)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}" if parsed.netloc else parsed.path

    @staticmethod
    def _classroom_scope(url: str) -> str:
        parsed = urlsplit(url)
        scope = f"{parsed.netloc}{parsed.path}".rstrip("/")
        stable_tokens = ("class", "lesson", "course", "session", "room", "live")
        query = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if any(token in key.lower() for token in stable_tokens)
        ]
        return f"{scope}?{urlencode(sorted(query))}" if query else scope

    @staticmethod
    def _is_failed_answer(answer: str) -> bool:
        if not answer or not answer.strip():
            return True
        lowered = answer.lower()
        return any(marker in lowered for marker in (
            "调用失败", "答题失败", "下载失败", "无法获取", "未设置", "error"
        ))

    # ------- 子方法 -------

    def _has_submit_button(self, page: Page) -> bool:
        """检查 exercise 页面中是否仍存在可见的提交答案按钮。"""
        return self._find_submit_button(page) is not None

    def _has_answerable_options(self, page: Page) -> bool:
        """当前题目至少有一个本程序能够点击的可见客观题选项。"""
        try:
            options = self._question_scope(page).locator("p[data-option]")
            return self._first_visible(options) is not None
        except Exception:
            return False

    def _read_remaining_seconds(self, page: Page) -> Optional[int]:
        """读取当前题目的倒计时剩余秒数；读不到有效值时返回 None。

        返回 None 的情形：页面已关闭、当前页没有可见的倒计时元素（非习题页，
        或 PPT 页里被隐藏的历史节点）、服务端尚未推送初始秒数（文本仍是
        「倒计时 --:--」）、题目已提交、作答时间已耗尽、或该题不限时。

        实现依据（2026-09-17 实测 + 前端源码核对）：
        - 倒计时是幻灯片右上角唯一的 time-box，文本形如「倒计时 14:36」，
          分与秒都补零到两位。归零后文本会被替换成文案而不再有数字，
          所以结束态按文案识别，而不是去匹配「00:00」。
        - 该值由前端收到服务端推送后自行每秒递减，服务端只在题目下发时推一次。
          因此标签页被浏览器节流时它会偏大，不能当作精确计时依据。
        - 页面可能保留历史 slide，故取最后一个可见节点。
        """
        try:
            if page.is_closed():
                return None
            node = self._last_visible(page.locator(COUNTDOWN_SELECTOR))
        except Exception as exc:
            logger.debug("读取倒计时失败：%s", exc)
            return None
        if node is None:
            return None

        try:
            text = (node.inner_text() or "").strip()
        except Exception as exc:
            logger.debug("读取倒计时文本失败：%s", exc)
            return None

        match = COUNTDOWN_PATTERN.search(text)
        if match:
            return int(match.group(1)) * 60 + int(match.group(2))

        for word, reason in COUNTDOWN_STATE_WORDS:
            if word in text:
                logger.debug("倒计时当前不可用（%s）：%r", reason, text)
                return None
        logger.debug("倒计时文本无法识别：%r", text)
        return None

    def _find_submit_button(self, page: Page):
        """返回可见提交按钮；按钮可能位于题目卡片外的页面操作栏。

        真实 DOM（2026-09-04 快照验证）：
        <div class="slide__shape submit-btn [can]">提交答案 <div class="tips">…</div></div>
        是 div 而非 button，故不能用 button 标签选择器作主匹配。
        """
        selectors = [
            'div.slide__shape.submit-btn',
            '[class*="submit-btn"]:has-text("提交答案")',
            'button:has-text("提交答案")',
            '[class*="submit-btn"]',
        ]
        for sel in selectors:
            try:
                button = self._first_visible(page.locator(sel))
                if button is not None:
                    return button
            except Exception:
                continue
        return None

    def _submit_ready(self, page: Page) -> bool:
        """提交按钮是否处于可提交态（class 含 can，即已选中至少一个选项）。

        真实 DOM 中按钮初始为灰色（无 can），选中选项后追加 can 类（蓝色）；
        点击灰色按钮不会生效，因此提交前须确认该状态。
        class 不可读或不存在时视为未知 DOM 变体，按旧行为放行，不阻塞提交。
        """
        button = self._find_submit_button(page)
        if button is None:
            return False
        try:
            class_attr = button.get_attribute("class")
        except Exception:
            return True
        if not class_attr:
            return True
        return "can" in class_attr.split()

    def _wait_submit_ready(self, page: Page, timeout: float = 2.5) -> bool:
        """轮询等待提交按钮进入可提交态（点击选项后 class 渲染可能略有延迟）。"""
        deadline = time.monotonic() + timeout
        while not self.stop_event.is_set() and time.monotonic() < deadline:
            if self._submit_ready(page):
                return True
            try:
                page.wait_for_timeout(40)
            except Exception:
                if self.stop_event.wait(0.04):
                    break
        return False

    def _capture_question_image(self, page: Page) -> Optional[str]:
        """获取题目图片的 URL。即时检查可见性和 src，避免无图时固定等待。"""
        try:
            scope = self._question_scope(page)
            image = self._first_visible(scope.locator('img[class*="cover"]'))
            if image is None:
                return None
            src = image.get_attribute("src")
            return src if src else None
        except Exception:
            return None

    def _prepare_question_image_b64(
        self,
        page: Page,
        image_url: Optional[str],
        budget: float = 20.0,
    ) -> str:
        """获取题目图片的 Base64 数据。若有 URL 则带安全 Cookie 下载；失败时回退至页面截图。"""
        if image_url:
            try:
                cookies = getattr(self.browser, "get_all_cookies", None)
                cookie_data = cookies() if callable(cookies) else self.browser.get_cookies_dict()
                dl_timeout = max(0.1, min(3.0, budget * 0.3))
                download_fn = getattr(self.ai, "_download_and_save", None)
                if callable(download_fn):
                    img_bytes, _ = download_fn(image_url, cookies=cookie_data, timeout=dl_timeout)
                    return base64.b64encode(img_bytes).decode("utf-8")
            except Exception as exc:
                self.log(f"题图下载失败，回退至页面直接截图：{exc}")

        try:
            scope = self._question_scope(page)
            return base64.b64encode(scope.screenshot(type="png")).decode("utf-8")
        except Exception as exc:
            self.log(f"页面截图失败：{exc}")
            return ""

    def _click_options(self, page: Page, letters: list[str]) -> bool:
        """点击 AI 返回的选项字母列表（不区分单选/多选，逐一点击）。

        - 字母来自统一 JSON 提示词的 answers 字段（已由 _parse_ai_answer 解析）。
        - 多字母点击中途失败时回滚已点击项，避免残留半选状态。
        - 全部失败时改微信通知报警，不默认选 A（避免误作答）。
        """
        scope = self._question_scope(page)
        if not letters:
            self.log("AI 未返回有效选项字母。")
            self._notify_answer_failure("")
            return False

        items = []
        for option in letters:
            item = self._first_visible(scope.locator(f'p[data-option="{option}"]'))
            if item is None:
                self.log(f"未找到可见选项 {option}。")
                self._notify_answer_failure(", ".join(letters))
                return False
            items.append((option, item))

        clicked = []
        for option, item in items:
            try:
                item.click(timeout=5_000)
                clicked.append(item)
            except Exception:
                self.log(f"未能点击选项 {option}。")
                if len(items) > 1:
                    for previous in reversed(clicked):
                        try:
                            previous.click(timeout=2_000)
                        except Exception:
                            pass
                self._notify_answer_failure(", ".join(letters))
                return False
        return True

    def _notify_answer_failure(self, predicted_str: str) -> None:
        try:
            self._notify(
                "自动答题失败",
                f"AI 返回：{predicted_str}，无法自动作答，请手动处理。",
            )
        except Exception as e:
            self.log(f"答题失败通知发送失败：{e}")

    def _click_judgment(self, page: Page, predicted_str: str) -> bool:
        """判断题作答：将 AI 返回的 对/错/正确/错误/T/F/是/否 映射到页面上的选项。

        雨课堂判断题通常是「单选题」形态，选项 data-option 为 A/B，文案为 对/错
        （或 正确/错误）。这里按页面实际文案匹配，而不是假设 A=对、B=错。
        仅当选项文案较短（<=4 字）时才视为判断题选项，避免误命中普通选择题长文本。
        """
        judgment = self._parse_judgment(predicted_str)
        if judgment is None:
            return False

        try:
            opts = self._question_scope(page).locator('p[data-option]')
            count = opts.count()
            if count == 0:
                return False
            for i in range(count):
                opt = opts.nth(i)
                try:
                    text = (opt.inner_text() or "").strip()
                except Exception:
                    continue
                if len(text) > 4:
                    continue  # 长文本不是判断题选项（如 "A. 关于对错的叙述"）
                option_value = self._parse_judgment(text)
                if option_value is judgment:
                    try:
                        opt.click(timeout=5_000)
                        self.log(f"判断题作答：{text}")
                        return True
                    except Exception:
                        self.log(f"未能点击选项 {text}。")
        except Exception as e:
            self.log(f"判断题解析失败：{e}")
        return False

    @staticmethod
    def _parse_judgment(text: str) -> Optional[bool]:
        normalized = re.sub(
            r"^(?:答案(?:是|为)?|ANSWER)\s*[:：]?\s*",
            "",
            text.strip().upper(),
        ).rstrip("。.")
        if normalized in ("对", "正确", "T", "TRUE", "是"):
            return True
        if normalized in ("错", "错误", "F", "FALSE", "否"):
            return False
        return None

    def _submit_answer(self, page: Page) -> bool:
        """提交答案。仅以提交按钮状态判断结果（准则：按钮存在 = 未作答）。

        True  = 已确认提交：点击后按钮消失，或已离开 exercise 页面。
        False = 仍未作答：未找到按钮、点击失败，或点击后按钮仍然存在。
        返回 False 的题在本轮运行中不会再次调用 AI。
        """
        try:
            submit_btn = self._find_submit_button(page)
            if submit_btn is None:
                self.log("未找到可见的提交答案按钮。")
                return False
            submit_btn.click(timeout=5_000)
        except Exception as e:
            self.log(f"无法提交答案：{e}")
            return False

        deadline = time.monotonic() + 10
        while not self.stop_event.is_set() and time.monotonic() < deadline:
            if not self._is_exercise_page(page) or not self._has_submit_button(page):
                if self._current_tracker:
                    self._current_tracker.mark("submit_confirmed")
                self.log("已提交答案。")
                return True
            try:
                page.wait_for_timeout(40)
            except Exception:
                if self.stop_event.wait(0.04):
                    break

        if self.stop_event.is_set():
            self.log("退出信号触发，提交确认未完成，记录为待核对状态而非成功。")
            if self.storage and self._answer_question_id:
                lesson_id = ""
                try:
                    lesson_id = self._lesson_id_from_url(page.url)
                except Exception:
                    pass
                self.storage.record_stage(
                    self.account_id, lesson_id, self._answer_question_id,
                    self._answer_generation or self._request_generation,
                    stage=STAGE_UNKNOWN, error_reason="关机中断待核对",
                )
            return False

        if self.storage and self._answer_question_id:
            lesson_id = ""
            try:
                lesson_id = self._lesson_id_from_url(page.url)
            except Exception:
                pass
            self.storage.record_stage(
                self.account_id, lesson_id, self._answer_question_id,
                self._answer_generation or self._request_generation,
                stage=STAGE_UNKNOWN, error_reason="已点击提交但超时后按钮仍存在",
            )
        self.log("已点击提交，但提交按钮仍存在，视为未作答，本题不再自动重试。")
        return False

    # ==================== Cookie 过期提醒 ====================

    def _check_cookie_warning(self) -> None:
        """检查 cookie 是否即将过期并发送微信提醒。"""
        last_update = self.config.get("last_cookie_update_time", "")
        if not last_update or not self.browser.has_session:
            return

        try:
            last_dt = datetime.strptime(last_update, "%Y-%m-%d %H:%M:%S")
            days_passed = (datetime.now() - last_dt).days
            remaining = COOKIE_VALID_DAYS - days_passed

            now = datetime.now()
            last_warn_date = self.config.get("last_cookie_warn_date", "")

            # 有效期不足 3 天且今天尚未提醒过即提醒（不再依赖恰好 8 点整）
            if (
                remaining <= 3
                and now.strftime("%Y-%m-%d") != last_warn_date
            ):
                self.log(f"Cookies 有效期不足 3 天，正在发送微信提醒。")
                try:
                    self._notify(
                        "雨课堂助手：Cookies 即将过期",
                        f"您的登录 Cookies 还剩约 {remaining:.1f} 天过期。请尽快更新 Cookies。",
                    )
                except Exception as notify_err:
                    self.log(f"Cookie 过期通知发送失败：{notify_err}")
                self.config.set("last_cookie_warn_date", now.strftime("%Y-%m-%d"))
                self.config.persist()
        except Exception as e:
            self.log(f"Cookie 过期检查异常：{e}")

    # ==================== 调试模式 ====================

    def _debug_dump(self, page: Page, label: str) -> None:
        """调试模式：保存页面 HTML + 截图到 debug/。"""
        if not self.config.get("debug_mode", False):
            return
        os.makedirs("debug", exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        try:
            path = f"debug/{ts}_{label}.html"
            with open(path, "w", encoding="utf-8") as f:
                f.write(page.content())
            self.log(f"[DEBUG] HTML: {path}")
        except Exception as e:
            self.log(f"[DEBUG] HTML 失败: {e}")
        try:
            path = f"debug/{ts}_{label}.png"
            page.screenshot(path=path, full_page=False)
            self.log(f"[DEBUG] 截图: {path}")
        except Exception as e:
            self.log(f"[DEBUG] 截图失败: {e}")

    def _debug_save_tabs(self) -> None:
        """调试模式：保存所有标签页 URL 到文件。"""
        if not self.config.get("debug_mode", False):
            return
        os.makedirs("debug", exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = f"debug/{ts}_tabs.txt"
        try:
            pages = self.browser.pages
            lines = [f"共 {len(pages)} 个标签页：\n"]
            for i, p in enumerate(pages):
                try:
                    lines.append(f"[{i}] closed={p.is_closed()} {p.url}\n")
                except Exception:
                    lines.append(f"[{i}] 无法读取\n")
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            self.log(f"[DEBUG] 标签页: {path}")
        except Exception as e:
            self.log(f"[DEBUG] 标签页失败: {e}")
