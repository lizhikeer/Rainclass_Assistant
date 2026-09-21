"""Worker 独立入口、配置校验、会话管理与观察模式核心测试。"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import Mock, patch

from src.bot import Bot, BotState
from src.config import Config, ConfigError, DEFAULTS
from src.instance_lock import InstanceLock
from src.paths import PathManager
from src.timing import QuizTimingTracker
from src.worker import EXIT_ERROR, EXIT_NEEDS_LOGIN, EXIT_OK, run_worker


class FakeLocator:
    def __init__(self, items=None):
        self.items = items or []

    @property
    def first(self):
        return self.items[0] if self.items else FakeItem(visible=False)

    def count(self):
        return len(self.items)

    def nth(self, idx):
        return self.items[idx]


class FakeItem:
    def __init__(self, text="", visible=True, attributes=None, selectors=None):
        self.text = text
        self.visible = visible
        self.attributes = attributes or {}
        self.selectors = selectors or {}
        self.click_count = 0

    def is_visible(self):
        return self.visible

    def inner_text(self):
        return self.text

    def get_attribute(self, name):
        return self.attributes.get(name)

    def click(self, timeout=None):
        self.click_count += 1

    def screenshot(self, type="png"):
        return b"fake_png_data"

    def locator(self, selector):
        return FakeLocator(self.selectors.get(selector, []))


class FakePage:
    def __init__(self, url="https://changjiang.yuketang.cn/lesson/fullscreen/v3/123/exercise"):
        self.url = url
        self.closed = False
        self.selectors = {}
        self.by_text_selectors = {}

    def is_closed(self):
        return self.closed

    def locator(self, selector):
        return FakeLocator(self.selectors.get(selector, []))

    def get_by_text(self, text, exact=False):
        return FakeLocator(self.by_text_selectors.get(text, []))

    def evaluate(self, script):
        return {"id": "q1", "text": "协议", "options": ["A:IP"], "images": []}

    def screenshot(self, type="png"):
        return b"fake_screenshot"

    def wait_for_timeout(self, ms):
        pass

    def content(self):
        return "<html>exercise</html>"


class WorkerTests(unittest.TestCase):
    def test_worker_help_no_gui_dependencies(self):
        """验证 python -m src.worker --help 能在无 GUI 环境正常运行且不加载 tkinter。"""
        result = subprocess.run(
            [sys.executable, "-m", "src.worker", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("雨课堂自动助手后台服务版", result.stdout)

        # 验证当前进程导入 src.worker 时不依赖 tkinter / customtkinter
        modules_before = set(sys.modules.keys())
        import src.worker
        modules_after = set(sys.modules.keys())
        new_modules = modules_after - modules_before
        self.assertNotIn("tkinter", new_modules)
        self.assertNotIn("customtkinter", new_modules)

    def test_worker_missing_config_fails_cleanly(self):
        """验证配置文件缺失时立即报告错误并以 EXIT_ERROR 退出。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            args = argparse.Namespace(
                data_dir=temp_dir,
                config_path=os.path.join(temp_dir, "non_existent.json"),
                mode="observe",
                check=False,
                wait_for_session=False,
            )
            code = run_worker(args)
            self.assertEqual(code, EXIT_ERROR)

    def test_worker_corrupt_config_fails_cleanly(self):
        """验证配置文件为损坏 JSON 时立即报错退出。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg_path = os.path.join(temp_dir, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as f:
                f.write("{ invalid json syntax ...")

            args = argparse.Namespace(
                data_dir=temp_dir,
                config_path=cfg_path,
                mode="observe",
                check=False,
                wait_for_session=False,
            )
            code = run_worker(args)
            self.assertEqual(code, EXIT_ERROR)

    def test_worker_invalid_config_type_fails_cleanly(self):
        """验证配置字段类型或数值非法时报错退出。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg_path = os.path.join(temp_dir, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump({"submit_delay": -100}, f)

            args = argparse.Namespace(
                data_dir=temp_dir,
                config_path=cfg_path,
                mode="observe",
                check=False,
                wait_for_session=False,
            )
            code = run_worker(args)
            self.assertEqual(code, EXIT_ERROR)

    def test_worker_missing_session_enters_needs_login(self):
        """验证缺少 browser_state.json 时返回 EXIT_NEEDS_LOGIN (2)。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg_path = os.path.join(temp_dir, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump({}, f)

            args = argparse.Namespace(
                data_dir=temp_dir,
                config_path=cfg_path,
                mode="observe",
                check=False,
                wait_for_session=False,
            )
            code = run_worker(args)
            self.assertEqual(code, EXIT_NEEDS_LOGIN)

    def test_worker_corrupt_session_enters_needs_login(self):
        """验证会话文件为空或非字典时返回 EXIT_NEEDS_LOGIN (2)。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg_path = os.path.join(temp_dir, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump({}, f)

            state_path = os.path.join(temp_dir, "browser_state.json")
            with open(state_path, "w", encoding="utf-8") as f:
                f.write("")  # 空文件

            args = argparse.Namespace(
                data_dir=temp_dir,
                config_path=cfg_path,
                mode="observe",
                check=False,
                wait_for_session=False,
            )
            code = run_worker(args)
            self.assertEqual(code, EXIT_NEEDS_LOGIN)

    def test_observe_mode_strictly_prohibits_actions(self):
        """验证 observe 模式下签到、点击选项、提交和 AI 答题均为零。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Config(os.path.join(temp_dir, "config.json"))
            config.set("mode", "observe")

            sign_btn = FakeItem(text="签到")
            submit_btn = FakeItem(text="提交答案", attributes={"class": "submit-btn can"})
            option_a = FakeItem(text="A. 选项A", attributes={"data-option": "A"})

            page = FakePage()
            page.by_text_selectors["签到"] = [sign_btn]
            page.selectors['[class*="submit-btn"]'] = [submit_btn]
            page.selectors['p[data-option]'] = [option_a]

            mock_browser = Mock()
            mock_browser.has_session = True
            mock_browser.page = page

            mock_ai = Mock()
            mock_notify = Mock()
            stop_event = threading.Event()

            bot = Bot(
                config=config,
                browser=mock_browser,
                ai_service=mock_ai,
                notification=mock_notify,
                stop_event=stop_event,
                mode="observe",
            )

            # 1. 验证签到被完全跳过
            bot._check_and_sign_in(page)
            self.assertEqual(sign_btn.click_count, 0)
            self.assertFalse(bot._signed_in)

            # 2. 验证题目作答被拦截
            bot._answer(page)
            self.assertEqual(option_a.click_count, 0)
            self.assertEqual(submit_btn.click_count, 0)
            self.assertEqual(mock_ai.submit_answer.call_count, 0)

    def test_auto_mode_records_full_timing_stages(self):
        """验证 auto 模式下各阶段单调时钟打点正常并落盘指标。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            metrics_file = Path(temp_dir) / "quiz_timings.jsonl"
            config = Config(os.path.join(temp_dir, "config.json"))
            config.set("mode", "auto")
            config.set("submit_delay", 0)

            option_a = FakeItem(text="A. 选项A", attributes={"data-option": "A"})
            submit_btn = FakeItem(text="提交答案", attributes={"class": "submit-btn can"})

            def on_submit_click():
                submit_btn.visible = False

            # 点击提交后隐藏按钮以触发 submit_confirmed
            original_click = submit_btn.click
            def custom_click(timeout=None):
                original_click(timeout)
                submit_btn.visible = False
            submit_btn.click = custom_click

            page = FakePage(url="https://changjiang.yuketang.cn/lesson/fullscreen/v3/123/exercise?lesson_id=456")
            page.selectors['p[data-option]'] = [option_a]
            page.selectors['p[data-option="A"]'] = [option_a]
            page.selectors['[class*="submit-btn"]'] = [submit_btn]
            page.selectors['button:has-text("提交答案")'] = [submit_btn]

            mock_browser = Mock()
            mock_browser.has_session = True
            mock_browser.page = page
            mock_browser.get_cookies_dict.return_value = {}

            # 模拟即时完成的 AI 响应
            fut = Future()
            fut.set_result('{"type": "single", "answers": ["A"]}')
            mock_ai = Mock()
            mock_ai.submit_answer.return_value = fut

            mock_notify = Mock()
            stop_event = threading.Event()

            bot = Bot(
                config=config,
                browser=mock_browser,
                ai_service=mock_ai,
                notification=mock_notify,
                stop_event=stop_event,
                mode="auto",
                metrics_file=metrics_file,
            )

            # 触发答题
            with patch.object(bot, "_capture_question_image", return_value=None):
                bot._answer(page)

            # 验证选项与提交按钮被点击
            self.assertGreaterEqual(option_a.click_count, 1)
            self.assertGreaterEqual(submit_btn.click_count, 1)

            # 验证 metrics 文件落盘
            self.assertTrue(metrics_file.exists())
            with open(metrics_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
            self.assertEqual(len(lines), 1)

            record = json.loads(lines[0])
            self.assertTrue(record["success"])
            durations = record["durations_ms"]
            self.assertIn("detect_to_ready_ms", durations)
            self.assertIn("ai_duration_ms", durations)
            self.assertIn("ai_to_validated_ms", durations)
            self.assertIn("total_end_to_end_ms", durations)


if __name__ == "__main__":
    unittest.main()
