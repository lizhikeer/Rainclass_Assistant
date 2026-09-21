"""第二阶段：新题检测与提交提速核心功能与边界测试。

覆盖：
1. classroom_poll_interval_ms 配置校验与优先级
2. 新题提示点击后同页/新页极速退出（彻底消除 3 秒死等）
3. 真实 Chromium 下本地 HTML 夹具：倒计时跳变与选项选中态不改变 question_id
4. 题干或选项真正变化时生成不同 question_id
5. 题目代际号 (Request Generation) 与旧答案丢弃防护
6. 多选全部选项预检与点击失败逆序回滚
7. submit_delay=0 与提交就绪确认
8. 默认关闭 HTML 完整保存与开启验证
9. 观察模式与停止信号的即时响应
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import Mock, patch

# 确保各执行环境下均能找到项目根目录与 tests 模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

from src.bot import Bot
from src.config import Config, ConfigError, DEFAULTS
try:
    from tests.test_worker import FakeItem, FakeLocator, FakePage
except ImportError:
    from test_worker import FakeItem, FakeLocator, FakePage


class FastPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sync_playwright is None:
            raise unittest.SkipTest("当前环境未安装 playwright，跳过真实 Chromium 测试")
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch(headless=True)
        except Exception as e:
            cls.pw.stop()
            raise unittest.SkipTest(f"无法启动 Chromium: {e}")
        fixture_path = Path(__file__).parent / "fixtures" / "exercise_single.html"
        cls.fixture_url = fixture_path.resolve().as_uri()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "browser"):
            try:
                cls.browser.close()
            except Exception:
                pass
        if hasattr(cls, "pw"):
            try:
                cls.pw.stop()
            except Exception:
                pass

    def test_classroom_poll_interval_config_and_priority(self):
        """验证 classroom_poll_interval_ms 优先级高于 quiz_refresh_interval，以及校验边界。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg_path = Path(temp_dir) / "config.json"
            cfg = Config(str(cfg_path))

            # 默认 200ms -> 0.2s
            self.assertEqual(cfg.get("classroom_poll_interval_ms"), 200)

            mock_browser = Mock()
            bot = Bot(config=cfg, browser=mock_browser, ai_service=Mock(), notification=Mock(), stop_event=threading.Event())
            self.assertAlmostEqual(bot._classroom_poll_interval(), 0.2)

            # 修改为 100ms -> 0.1s
            cfg.set("classroom_poll_interval_ms", 100)
            self.assertAlmostEqual(bot._classroom_poll_interval(), 0.1)

            # 若未设置 classroom_poll_interval_ms，回退到 quiz_refresh_interval (秒)
            cfg.set("classroom_poll_interval_ms", None)
            cfg.set("quiz_refresh_interval", 2)
            self.assertAlmostEqual(bot._classroom_poll_interval(), 2.0)

            # 边界校验：必须在 50~5000ms 之间
            s = cfg.to_dict()
            s["classroom_poll_interval_ms"] = 30
            errors = cfg.save(s)
            self.assertTrue(any("classroom_poll_interval_ms" in e for e in errors))

            s["classroom_poll_interval_ms"] = 6000
            errors = cfg.save(s)
            self.assertTrue(any("classroom_poll_interval_ms" in e for e in errors))

            s["classroom_poll_interval_ms"] = 500
            errors = cfg.save(s)
            self.assertEqual(errors, [])

    def test_prompt_click_same_page_fast_exit_no_3s_delay(self):
        """点击新题提示后，若题目在同页出现，必须在几十毫秒内退出等待，绝不等满 3 秒。"""
        page = FakePage("https://changjiang.yuketang.cn/lesson/1/exercise")
        option_a = FakeItem(text="A. 选项A", attributes={"data-option": "A"})
        submit_btn = FakeItem(text="提交答案", attributes={"class": "submit-btn can"})
        page.selectors['p[data-option]'] = [option_a]
        page.selectors['[class*="submit-btn"]'] = [submit_btn]

        mock_browser = Mock()
        mock_browser.pages = [page]
        mock_browser.page = page

        stop_event = threading.Event()
        bot = Bot(
            config=Config(),
            browser=mock_browser,
            ai_service=Mock(),
            notification=Mock(),
            stop_event=stop_event,
            mode="observe",
        )

        with patch.object(bot, "_open_new_quiz", return_value=True):
            with patch.object(bot, "_find_new_classroom_page", return_value=None):
                # 仅运行一次循环体，测量点击提示后的等待耗时
                start = time.monotonic()
                pages_before_prompt = tuple(bot.browser.pages)
                clicked_prompt = bot._open_new_quiz(page)
                self.assertTrue(clicked_prompt)

                # 执行多条件判定等待
                deadline = time.monotonic() + 3.0
                while not bot.stop_event.is_set() and time.monotonic() < deadline:
                    new_page = bot._find_new_classroom_page(pages_before_prompt)
                    if new_page is not None:
                        break
                    if page.is_closed():
                        break
                    if bot._is_exercise_page(page) or bot._has_answerable_options(page):
                        break
                    bot.stop_event.wait(0.04)
                duration = time.monotonic() - start

                # 必须立即退出，远小于 3 秒（受控环境预期 < 0.15s）
                self.assertLess(duration, 0.2, f"同页新题等待耗时过长: {duration:.3f}s")

    def test_prompt_click_new_page_fast_exit(self):
        """点击新题提示后，新标签页一旦产生，立刻退出并跟随。"""
        old_page = FakePage("https://changjiang.yuketang.cn/lesson/1/ppt/1")
        new_page = FakePage("https://changjiang.yuketang.cn/lesson/1/exercise")
        new_page.selectors['[class*="submit-btn"]'] = [FakeItem(text="提交答案")]

        mock_browser = Mock()
        mock_browser.pages = [old_page]

        bot = Bot(
            config=Config(),
            browser=mock_browser,
            ai_service=Mock(),
            notification=Mock(),
            stop_event=threading.Event(),
        )

        pages_before = tuple(mock_browser.pages)
        # 模拟 50ms 后新标签页加入
        def _add_page():
            time.sleep(0.05)
            mock_browser.pages = [old_page, new_page]
        t = threading.Thread(target=_add_page)
        t.start()

        start = time.monotonic()
        deadline = time.monotonic() + 3.0
        found = None
        while time.monotonic() < deadline:
            found = bot._find_new_classroom_page(pages_before)
            if found is not None:
                break
            time.sleep(0.02)
        t.join()
        duration = time.monotonic() - start

        self.assertIsNotNone(found)
        self.assertLess(duration, 0.3)

    def test_countdown_ticks_do_not_mutate_question_id_in_real_browser(self):
        """真实 Chromium 下加载 fixture，验证倒计时跳变不改变 question_id。"""
        page = self.browser.new_page()
        try:
            page.goto(self.fixture_url)
            bot = Bot(
                config=Config(),
                browser=Mock(),
                ai_service=Mock(),
                notification=Mock(),
                stop_event=threading.Event(),
            )

            id_1 = bot._question_id(page, None)
            self.assertTrue(id_1)

            # 模拟倒计时从 05:00 跳变为 04:59
            page.evaluate("document.querySelector('.time-box .timing').innerText = '倒计时 04:59'")
            id_2 = bot._question_id(page, None)

            # 再次跳变到 00:01
            page.evaluate("document.querySelector('.time-box .timing').innerText = '倒计时 00:01'")
            id_3 = bot._question_id(page, None)

            self.assertEqual(id_1, id_2, "倒计时变动不应改变题目语义标识")
            self.assertEqual(id_2, id_3, "倒计时变动不应改变题目语义标识")
        finally:
            page.close()

    def test_option_selection_does_not_mutate_question_id_in_real_browser(self):
        """真实 Chromium 下选中选项和按钮进入 can 态不改变 question_id。"""
        page = self.browser.new_page()
        try:
            page.goto(self.fixture_url)
            bot = Bot(
                config=Config(),
                browser=Mock(),
                ai_service=Mock(),
                notification=Mock(),
                stop_event=threading.Event(),
            )

            id_before = bot._question_id(page, None)

            # 模拟用户/程序选中选项 A，选项追加 active，提交按钮追加 can 类
            page.evaluate("""() => {
                document.querySelector('[data-option="A"]').classList.add('active');
                document.querySelector('.btn-submit').classList.add('can');
            }""")

            id_after = bot._question_id(page, None)
            self.assertEqual(id_before, id_after, "选项选中态变动不应改变题目语义标识")
        finally:
            page.close()

    def test_different_stem_or_options_produce_different_question_id(self):
        """题干替换时产生不同的 question_id，确保真切题能够被识别。"""
        page = self.browser.new_page()
        try:
            page.goto(self.fixture_url)
            bot = Bot(
                config=Config(),
                browser=Mock(),
                ai_service=Mock(),
                notification=Mock(),
                stop_event=threading.Event(),
            )

            id_first = bot._question_id(page, None)

            # 题干被替换
            page.evaluate("document.querySelector('.quiz-stem').innerText = '2. 下列工作在传输层的是？'")
            id_second = bot._question_id(page, None)

            self.assertNotEqual(id_first, id_second, "题干改变必须产生不同的题目标识")
        finally:
            page.close()

    def test_stale_answer_discarded_on_generation_mismatch(self):
        """AI 异步计算期间发生代际递增（如切题/换题），返回的旧答案必须被丢弃。"""
        page = FakePage("https://changjiang.yuketang.cn/lesson/1/exercise")
        option_a = FakeItem(text="A. 选项A", attributes={"data-option": "A"})
        submit_btn = FakeItem(text="提交答案", attributes={"class": "submit-btn can"})
        page.selectors['p[data-option="A"]'] = [option_a]
        page.selectors['[class*="submit-btn"]'] = [submit_btn]

        mock_browser = Mock()
        mock_browser.get_cookies_dict.return_value = {}

        fut = Future()
        mock_ai = Mock()
        mock_ai.submit_answer.return_value = fut

        bot = Bot(
            config=Config(),
            browser=mock_browser,
            ai_service=mock_ai,
            notification=Mock(),
            stop_event=threading.Event(),
            mode="auto",
        )

        with patch.object(bot, "_capture_question_image", return_value=None):
            bot._handle_quiz(page)
            orig_gen = bot._answer_generation

            # 模拟页面切题，代际号递增
            bot._request_generation += 1

            # AI 结果在切题后姗姗来迟
            fut.set_result('{"type":"single","answers":"A"}')
            bot._complete_pending_answer(page)

            # 旧答案被丢弃，不得点击选项或提交
            self.assertEqual(option_a.click_count, 0)
            self.assertEqual(submit_btn.click_count, 0)

    def test_multichoice_click_failure_rollback(self):
        """多选题中途点击某选项失败时，必须逆序回滚已选选项，且不触发提交。"""
        page = FakePage("https://changjiang.yuketang.cn/lesson/1/exercise")
        option_a = FakeItem(text="A. 选项A", attributes={"data-option": "A"})
        option_b = FakeItem(text="B. 选项B", attributes={"data-option": "B"})

        def _fail_click(timeout=None):
            raise RuntimeError("DOM 临时不可操作")
        option_b.click = _fail_click

        page.selectors['p[data-option="A"]'] = [option_a]
        page.selectors['p[data-option="B"]'] = [option_b]

        bot = Bot(
            config=Config(),
            browser=Mock(),
            ai_service=Mock(),
            notification=Mock(),
            stop_event=threading.Event(),
            mode="auto",
        )

        # 点击 A 和 B，B 失败时应回滚 A
        result = bot._click_options(page, ["A", "B"])
        self.assertFalse(result)
        # option_a 应该被点击了 2 次（初次选中 + 回滚反选）
        self.assertEqual(option_a.click_count, 2)

    def test_multichoice_missing_option_prevents_clicks(self):
        """多选题缺少某个选项时，预检失败，不点击任何一个选项。"""
        page = FakePage("https://changjiang.yuketang.cn/lesson/1/exercise")
        option_a = FakeItem(text="A. 选项A", attributes={"data-option": "A"})
        page.selectors['p[data-option="A"]'] = [option_a]

        bot = Bot(
            config=Config(),
            browser=Mock(),
            ai_service=Mock(),
            notification=Mock(),
            stop_event=threading.Event(),
            mode="auto",
        )

        # 请求选 A 和 B，但 B 不在 DOM 中
        result = bot._click_options(page, ["A", "B"])
        self.assertFalse(result)
        self.assertEqual(option_a.click_count, 0)

    def test_submit_delay_zero_executes_immediately(self):
        """当 submit_delay=0 时，验证通过后立即提交，不产生额外等待。"""
        page = FakePage("https://changjiang.yuketang.cn/lesson/1/exercise")
        option_a = FakeItem(text="A. 选项A", attributes={"data-option": "A"})
        submit_btn = FakeItem(text="提交答案", attributes={"class": "submit-btn can"})
        page.selectors['p[data-option="A"]'] = [option_a]
        page.selectors['[class*="submit-btn"]'] = [submit_btn]

        cfg = Config()
        cfg.set("submit_delay", 0)

        bot = Bot(
            config=cfg,
            browser=Mock(),
            ai_service=Mock(),
            notification=Mock(),
            stop_event=threading.Event(),
            mode="auto",
        )

        fut = Future()
        fut.set_result('{"type":"single","answers":"A"}')
        bot._answer_future = fut
        bot._answer_question_id = "test_q"
        bot._answer_exercise_path = bot._exercise_path(page)
        bot._answer_generation = bot._request_generation

        with patch.object(bot, "_submit_answer", return_value=True) as mock_submit:
            start = time.monotonic()
            bot._complete_pending_answer(page)
            duration = time.monotonic() - start

            mock_submit.assert_called_once()
            self.assertLess(duration, 0.1, f"submit_delay=0 时不应产生停顿: {duration:.3f}s")

    def test_html_save_disabled_by_default(self):
        """验证默认关闭 HTML 保存；开启配置时正常落盘。"""
        page = FakePage("https://changjiang.yuketang.cn/lesson/1/exercise")
        page_mock = Mock()
        page_mock.content.return_value = "<html>test</html>"

        # 1. 默认配置（save_exercise_html: False, debug_mode: False）
        cfg = Config()
        self.assertFalse(cfg.get("save_exercise_html"))
        bot = Bot(config=cfg, browser=Mock(), ai_service=Mock(), notification=Mock(), stop_event=threading.Event())
        bot._save_exercise_html(page_mock)
        page_mock.content.assert_not_called()

        # 2. 开启 save_exercise_html
        cfg.set("save_exercise_html", True)
        with patch("src.bot.os.makedirs"), patch("builtins.open", unittest.mock.mock_open()):
            bot._save_exercise_html(page_mock)
            page_mock.content.assert_called_once()

    def test_observe_mode_passive_integrity_under_fast_poll(self):
        """即使课堂内轮询缩短到毫秒级，观察模式依然严格保持零动作。"""
        page = FakePage("https://changjiang.yuketang.cn/lesson/1/exercise")
        option_a = FakeItem(text="A. 选项A", attributes={"data-option": "A"})
        submit_btn = FakeItem(text="提交答案", attributes={"class": "submit-btn can"})
        page.selectors['p[data-option]'] = [option_a]
        page.selectors['[class*="submit-btn"]'] = [submit_btn]

        mock_browser = Mock()
        mock_browser.pages = [page]
        mock_browser.page = page

        mock_ai = Mock()
        cfg = Config()
        cfg.set("classroom_poll_interval_ms", 50)
        bot = Bot(config=cfg, browser=mock_browser, ai_service=mock_ai, notification=Mock(), stop_event=threading.Event(), mode="observe")

        # 模拟在观察模式下运行 _answer
        bot._answer(page)

        # 校验绝对零操作
        self.assertEqual(option_a.click_count, 0)
        self.assertEqual(submit_btn.click_count, 0)
        self.assertEqual(mock_ai.submit_answer.call_count, 0)


if __name__ == "__main__":
    unittest.main()
