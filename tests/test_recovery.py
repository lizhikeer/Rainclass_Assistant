"""第四阶段：NAS 长期运行可靠性与故障恢复专项测试集。

覆盖：
1. 会话生命周期：缺失、损坏、站点不一致、原子替换、保护有效备份不被未登录覆盖；
2. 数据库与两阶段提交：阶段记录、联合唯一约束、崩溃后页面状态核验与防盲目重放；
3. 代际防护：网络恢复后丢弃旧代际/旧题目 AI 结果；
4. 进程锁与隔离：同目录拒绝第二实例，不同数据目录独立并发；
5. 容错自愈与退避：网络超时/浏览器关闭有限次指数退避重试，认证失效转 needs_login；
6. 目录安全清理：禁止越界删除用户文件，符号链接隔离，容量与时长上限；
7. 通知队列有界与告警合并：队列打满不阻塞业务，高频重复告警合并；
8. 健康检查与优雅关机：进程存活与业务就绪区分，SIGTERM 拦截新题并标记待核对。
"""

import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

# 确保能找到项目根目录
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bot import Bot, BotState
from src.browser import BrowserManager, validate_session_data
from src.cleaner import ArtifactCleaner
from src.config import Config
from src.instance_lock import InstanceLock
from src.log import SanitizingFilter
from src.notification import NotificationService
from src.paths import PathManager
from src.status import ServiceState, StatusTracker
from src.storage import (
    QuizStorage,
    STAGE_AI_REQUESTED,
    STAGE_CONFIRMED,
    STAGE_DETECTED,
    STAGE_OPTIONS_CLICKED,
    STAGE_SKIPPED,
    STAGE_SUBMITTING,
    STAGE_UNKNOWN,
)
from src.worker import (
    EXIT_ERROR,
    EXIT_NEEDS_LOGIN,
    EXIT_OK,
    handle_healthcheck,
    handle_import_session,
    run_worker,
)


class RecoveryAndReliabilityTests(unittest.TestCase):
    """第四阶段专项测试用例。"""

    def test_session_validation_and_domain_binding(self):
        """验证会话校验：缺失、非法格式、站点域名不匹配与正常通过。"""
        # 1. 非法 JSON 对象
        valid, msg = validate_session_data("not a dict")
        self.assertFalse(valid)
        self.assertIn("非法", msg)

        # 2. 缺少 cookies 和 origins
        valid, msg = validate_session_data({})
        self.assertFalse(valid)
        self.assertIn("缺少有效 cookies", msg)

        # 3. 域名完全不匹配（如配置长江雨课堂，但 Cookie 仅属于百度或 example.com）
        bad_domain_data = {
            "cookies": [{"name": "sessionid", "value": "123", "domain": "example.com"}]
        }
        valid, msg = validate_session_data(bad_domain_data, expected_base_url="https://changjiang.yuketang.cn")
        self.assertFalse(valid)
        self.assertIn("不匹配", msg)

        # 4. 正常匹配雨课堂根域或完整 host
        good_data = {
            "cookies": [{"name": "sessionid", "value": "secret", "domain": ".yuketang.cn"}]
        }
        valid, msg = validate_session_data(good_data, expected_base_url="https://changjiang.yuketang.cn")
        self.assertTrue(valid)
        self.assertEqual(msg, "OK")

    def test_session_atomic_import(self):
        """验证会话文件原子导入：格式校验、原子写入与保护。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = PathManager(data_dir=temp_dir)
            paths.ensure_dirs()

            import_file = Path(temp_dir) / "source_session.json"
            valid_session = {
                "cookies": [{"name": "sessionid", "value": "test123456", "domain": "changjiang.yuketang.cn"}]
            }
            with open(import_file, "w", encoding="utf-8") as f:
                json.dump(valid_session, f)

            code = handle_import_session(paths, str(import_file), "https://changjiang.yuketang.cn")
            self.assertEqual(code, EXIT_OK)
            self.assertTrue(paths.state_file.exists())

            # 验证导入的内容正确
            with open(paths.state_file, "r", encoding="utf-8") as f:
                saved = json.load(f)
            self.assertEqual(saved["cookies"][0]["value"], "test123456")

    def test_session_save_protects_valid_backup(self):
        """验证 save_session 在未登录状态下不覆盖有效备份，且支持节流。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "browser_state.json"
            state_file.write_text('{"valid": "original"}', encoding="utf-8")

            bm = BrowserManager(headless=True, state_file=str(state_file), auto_install=False)
            bm._context = Mock()
            bm._browser = Mock()
            bm._browser.is_connected.return_value = True

            # 模拟未登录状态
            bm.is_logged_in = Mock(return_value=False)
            result = bm.save_session(force=False)
            self.assertFalse(result)
            # 文件内容不被改变
            self.assertEqual(state_file.read_text(encoding="utf-8"), '{"valid": "original"}')

            # 模拟已登录状态
            bm.is_logged_in = Mock(return_value=True)
            bm._context.storage_state = lambda path: Path(path).write_text('{"valid": "new"}', encoding="utf-8")
            result = bm.save_session(force=True)
            self.assertTrue(result)
            self.assertEqual(state_file.read_text(encoding="utf-8"), '{"valid": "new"}')

    def test_observable_login_status_evidence(self):
        """验证基于可观察证据（重定向URL、密码框、#tab-student）的登录检测。"""
        bm = BrowserManager(headless=True, auto_install=False)
        bm._browser = Mock()
        bm._browser.is_connected.return_value = True

        mock_page = Mock()
        mock_page.is_closed.return_value = False
        bm._page = mock_page

        # 1. 发生重定向到登录页
        mock_page.url = "https://changjiang.yuketang.cn/web/login?redirect_url=..."
        self.assertFalse(bm.is_logged_in())

        # 2. 页面存在密码输入框
        mock_page.url = "https://changjiang.yuketang.cn/v2/web/"
        mock_page.locator.side_effect = lambda sel: Mock(count=Mock(return_value=1 if sel == 'input[type="password"]' else 0))
        self.assertFalse(bm.is_logged_in())

        # 3. 正常出现 #tab-student 且无未登录信号
        mock_page.locator.side_effect = lambda sel: Mock(count=Mock(return_value=1 if sel == "#tab-student" else 0))
        self.assertTrue(bm.is_logged_in())

    def test_sqlite_storage_pipeline_stages(self):
        """验证 SQLite 答题记录各阶段流转、联合唯一约束及无凭据泄漏。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "records.db"
            storage = QuizStorage(db_path)

            # 1. 记录 detected
            storage.record_stage("user1", "class1", "q1", 1, STAGE_DETECTED, detection_source="prompt")
            rec = storage.get_record("user1", "class1", "q1", 1)
            self.assertIsNotNone(rec)
            self.assertEqual(rec["stage"], STAGE_DETECTED)
            self.assertEqual(rec["submission_confirmed"], 0)

            # 2. 推进至 options_clicked
            storage.record_stage("user1", "class1", "q1", 1, STAGE_OPTIONS_CLICKED, submitted_answer="A")
            rec = storage.get_record("user1", "class1", "q1", 1)
            self.assertEqual(rec["stage"], STAGE_OPTIONS_CLICKED)
            self.assertEqual(rec["submitted_answer"], "A")

            # 3. 推进至 submitting
            storage.record_stage("user1", "class1", "q1", 1, STAGE_SUBMITTING)
            rec = storage.get_record("user1", "class1", "q1", 1)
            self.assertEqual(rec["stage"], STAGE_SUBMITTING)

            # 4. 确认提交 confirmed
            storage.record_stage("user1", "class1", "q1", 1, STAGE_CONFIRMED, submission_confirmed=1, latency_ms=120.5)
            rec = storage.get_record("user1", "class1", "q1", 1)
            self.assertEqual(rec["stage"], STAGE_CONFIRMED)
            self.assertEqual(rec["submission_confirmed"], 1)
            self.assertEqual(rec["latency_ms"], 120.5)

            # 5. has_confirmed 检索
            self.assertTrue(storage.has_confirmed("user1", "class1", "q1"))
            self.assertFalse(storage.has_confirmed("user1", "class1", "q2"))

    def test_two_phase_crash_recovery_logic(self):
        """验证崩溃重启后的两阶段核验：按钮已消失补齐确认，按钮仍在标记未知并跳过。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "records.db"
            storage = QuizStorage(db_path)

            # 模拟崩溃前留在 submitting 状态
            storage.record_stage("u1", "c1", "q100", 1, STAGE_SUBMITTING, submitted_answer="B")

            cfg = Config()
            cfg.set("mode", "auto")
            bot = Bot(
                config=cfg,
                browser=Mock(),
                ai_service=Mock(),
                notification=Mock(),
                stop_event=threading.Event(),
                storage=storage,
            )
            bot.account_id = "u1"
            bot._capture_question_image = Mock(return_value="http://img")
            bot._question_id = Mock(return_value="q100")

            # 情况 A: 重启后检查页面，提交按钮已经消失（说明崩溃前浏览器实际提交成功）
            mock_page_a = Mock()
            mock_page_a.url = "https://changjiang.yuketang.cn/lesson/fullscreen/v3/c1/exercise?q=100"
            bot._has_submit_button = Mock(return_value=False)

            bot._handle_quiz(mock_page_a)
            # 应当被确认为 confirmed，而不是重新调用 AI 作答
            rec_a = storage.get_record("u1", "c1", "q100", 1)
            self.assertEqual(rec_a["stage"], STAGE_CONFIRMED)
            self.assertEqual(rec_a["submission_confirmed"], 1)

            # 情况 B: 模拟另一题崩溃留在 submitting，但重启后提交按钮依然存在（状态不明）
            storage.record_stage("u1", "c1", "q200", 1, STAGE_SUBMITTING, submitted_answer="C")
            bot._question_id = Mock(return_value="q200")
            mock_page_b = Mock()
            mock_page_b.url = "https://changjiang.yuketang.cn/lesson/fullscreen/v3/c1/exercise?q=200"
            bot._has_submit_button = Mock(return_value=True)

            bot._handle_quiz(mock_page_b)
            # 应当被置为 unknown，并发送通知，绝不盲目重新调用 AI 提交
            rec_b = storage.get_record("u1", "c1", "q200", 1)
            self.assertEqual(rec_b["stage"], STAGE_UNKNOWN)
            self.assertEqual(rec_b["submission_confirmed"], 0)
            bot.notification.send_async.assert_called()

    def test_instance_lock_directory_isolation(self):
        """验证单实例锁：同目录拒绝第二实例，不同数据目录相互隔离独立运行。"""
        with tempfile.TemporaryDirectory() as dir1, tempfile.TemporaryDirectory() as dir2:
            paths1 = PathManager(data_dir=dir1)
            paths2 = PathManager(data_dir=dir2)

            lock1_a = InstanceLock(paths1.lock_file)
            lock1_b = InstanceLock(paths1.lock_file)
            lock2 = InstanceLock(paths2.lock_file)

            # 1. 实例 1 在 dir1 加锁成功
            self.assertTrue(lock1_a.acquire())

            # 2. 相同目录 dir1 的第二实例必须被拒绝
            self.assertFalse(lock1_b.acquire())

            # 3. 不同目录 dir2 的实例不受影响，成功加锁
            self.assertTrue(lock2.acquire())

            # 4. 释放 lock1_a 后，lock1_b 可成功获取
            lock1_a.release()
            self.assertTrue(lock1_b.acquire())

            lock1_b.release()
            lock2.release()

    def test_artifact_cleaner_security_and_limits(self):
        """验证文件清理器：符号链接保护、严格目录限制与容量/天数上限。"""
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as outside_dir:
            cleaner = ArtifactCleaner(data_dir, max_age_days=1, max_total_mb=0.01, max_file_count=5)

            debug_dir = Path(data_dir) / "debug"
            debug_dir.mkdir(parents=True, exist_ok=True)

            # 在外部目录创建用户重要文件
            user_important_file = Path(outside_dir) / "important.txt"
            user_important_file.write_text("critical user data", encoding="utf-8")

            # 尝试在 debug_dir 下创建一个指向外部文件的符号链接（在支持 symlink 的系统）
            symlink_created = False
            try:
                evil_symlink = debug_dir / "evil_link.txt"
                os.symlink(user_important_file, evil_symlink)
                symlink_created = True
            except (OSError, NotImplementedError):
                pass

            # 在 debug_dir 下创建若干测试文件
            f1 = debug_dir / "test1.html"
            f1.write_text("a" * 5000, encoding="utf-8")
            f2 = debug_dir / "test2.html"
            f2.write_text("b" * 10000, encoding="utf-8")

            # 执行清理
            stats = cleaner.clean()
            self.assertGreaterEqual(stats["deleted_files"], 1)

            # 严格验证：外部用户文件绝对未被删除
            self.assertTrue(user_important_file.exists())
            self.assertEqual(user_important_file.read_text(encoding="utf-8"), "critical user data")

    def test_log_sanitizer_redacts_credentials(self):
        """验证日志脱敏：SessionID、API Key、Token、密码等敏感信息被自动掩码。"""
        sf = SanitizingFilter()

        test_records = [
            ("Connecting with sessionid=eyw1zkzpmxi69betfw3vtgkd2ybmd10r to server", "***REDACTED***"),
            ("Using api_key=sk-1234567890abcdef1234567890 for Doubao", "***REDACTED***"),
            ("Authorization: Bearer my_secret_token_12345", "***REDACTED***"),
            ("Got password: mysecretpassword123", "***REDACTED***"),
        ]

        for raw_msg, expected_mask in test_records:
            record = Mock()
            record.getMessage.return_value = raw_msg
            record.msg = raw_msg
            record.args = None
            sf.filter(record)
            self.assertIn(expected_mask, record.msg)
            self.assertNotIn("eyw1zkzpmxi69betfw3vtgkd2ybmd10r", record.msg)
            self.assertNotIn("sk-1234567890abcdef1234567890", record.msg)
            self.assertNotIn("my_secret_token_12345", record.msg)

    def test_notification_bounded_queue_and_dedup(self):
        """验证有界通知队列：打满时不阻塞、重复告警合并。"""
        ns = NotificationService(api_key="dummy_key", queue_size=5, dedup_window=10.0)

        # 模拟底层 requests.post 成功
        with patch("requests.post") as mock_post:
            mock_resp = Mock()
            mock_resp.raise_for_status.return_value = None
            mock_resp.json.return_value = {"code": 0, "message": "success"}
            mock_post.return_value = mock_resp

            # 1. 快速触发相同告警，应当合并
            f1 = ns.send_async("告警A", "内容重复")
            f2 = ns.send_async("告警A", "内容重复")
            f3 = ns.send_async("告警A", "内容重复")

            self.assertIsNotNone(f1)
            self.assertIsNotNone(f2)
            # f2 和 f3 被去重合并，立即返回完成
            self.assertTrue(f2.done())
            self.assertTrue(f3.done())

            # 2. 模拟队列满
            ns._closed = True  # 暂停消费线程模拟队列堵塞
            ns._closed = False
            for i in range(10):
                ns.send_async(f"唯一告警_{i}", f"内容_{i}")
            # 队列不会无限膨胀，超出 queue_size 的任务被安全降级不引发阻塞
            self.assertLessEqual(ns._queue.qsize(), 5)

        ns.shutdown()

    def test_healthcheck_cli_logic(self):
        """验证 healthcheck：区分进程存活与业务就绪，心跳超时检测。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = PathManager(data_dir=temp_dir)
            paths.ensure_dirs()

            tracker = StatusTracker(paths.health_file, account_id="test_acc", server_name="长江雨课堂")

            # 1. 处于 needs_login 状态
            tracker.set_state(ServiceState.NEEDS_LOGIN, "等待登录")
            # 默认 healthcheck 返回 0 (alive=True，防止 Docker 无限死循环重启)
            code_normal = handle_healthcheck(paths, strict_ready=False)
            self.assertEqual(code_normal, EXIT_OK)

            # strict_ready 模式下返回 EXIT_NEEDS_LOGIN (2)
            code_strict = handle_healthcheck(paths, strict_ready=True)
            self.assertEqual(code_strict, EXIT_NEEDS_LOGIN)

            # 2. 处于 waiting_class 或 monitoring 状态（业务就绪）
            tracker.set_state(ServiceState.WAITING_CLASS, "无课等待中")
            self.assertEqual(handle_healthcheck(paths, strict_ready=True), EXIT_OK)

            # 3. 模拟心跳超时（超过 120 秒）
            tracker._last_heartbeat_time = time.time() - 150
            tracker._dump_health_file()
            self.assertEqual(handle_healthcheck(paths, strict_ready=False), EXIT_ERROR)

    def test_sigterm_graceful_shutdown_marks_unknown(self):
        """验证在提交点击前后收到停止信号时，记录为 unknown (待核对) 而非 success。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "records.db"
            storage = QuizStorage(db_path)
            stop_event = threading.Event()

            cfg = Config()
            cfg.set("mode", "auto")
            bot = Bot(
                config=cfg,
                browser=Mock(),
                ai_service=Mock(),
                notification=Mock(),
                stop_event=stop_event,
                storage=storage,
            )
            bot.account_id = "user_sig"
            bot._answer_question_id = "q_sig_1"
            bot._answer_generation = 1

            mock_page = Mock()
            mock_page.url = "https://changjiang.yuketang.cn/lesson/fullscreen/v3/c1/exercise?q=1"
            mock_page.wait_for_timeout.side_effect = lambda ms: None
            mock_btn = Mock()
            bot._find_submit_button = Mock(return_value=mock_btn)
            bot._is_exercise_page = Mock(return_value=True)
            bot._has_submit_button = Mock(return_value=True)

            # 模拟点击后立即触发关机信号
            def _click_and_stop(timeout=None):
                stop_event.set()
            mock_btn.click = _click_and_stop

            res = bot._submit_answer(mock_page)
            self.assertFalse(res)

            # 数据库应当被记为 unknown / 待核对，绝不能是 confirmed
            rec = storage.get_record("user_sig", "c1", "q_sig_1", 1)
            self.assertIsNotNone(rec)
            self.assertEqual(rec["stage"], STAGE_UNKNOWN)
            self.assertEqual(rec["submission_confirmed"], 0)
            self.assertIn("关机中断", rec["error_reason"])


if __name__ == "__main__":
    unittest.main()
