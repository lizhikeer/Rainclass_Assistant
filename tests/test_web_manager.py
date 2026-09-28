"""ProcessManager 与 LogRingBuffer 单元测试。"""

import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.web.manager import LogRingBuffer, ProcessManager, sanitize_text


class WebManagerTests(unittest.TestCase):
    def test_sanitize_text_strips_ansi_and_masks_secrets(self):
        # 1. ANSI 颜色转义字符过滤
        colored = "\x1b[32m[INFO]\x1b[0m 登录成功"
        self.assertEqual(sanitize_text(colored), "[INFO] 登录成功")

        # 2. API Key 脱敏
        key_log = 'api_key="sk-1234567890abcdef"'
        clean = sanitize_text(key_log)
        self.assertNotIn("1234567890", clean)
        self.assertIn("sk-1******cdef", clean)

        # 3. Bearer Token 脱敏
        auth_log = "Authorization: Bearer my_secret_token_12345"
        clean_auth = sanitize_text(auth_log)
        self.assertNotIn("secret_token", clean_auth)
        self.assertIn("my_s******2345", clean_auth)

        # 4. SessionID 脱敏
        sess_log = 'sessionid="sess_abc123xyz_val"'
        clean_sess = sanitize_text(sess_log)
        self.assertNotIn("abc123xyz", clean_sess)
        self.assertIn("ses******val", clean_sess)

    def test_log_ring_buffer_capacity_and_subscriptions(self):
        buf = LogRingBuffer(capacity=5)
        received = []

        unsub = buf.subscribe(lambda e: received.append(e["message"]))

        for i in range(7):
            buf.append(f"log line {i}")

        # 容量截断到 5
        lines = buf.get_lines(limit=10)
        self.assertEqual(len(lines), 5)
        self.assertEqual(lines[0]["message"], "log line 2")
        self.assertEqual(lines[-1]["message"], "log line 6")

        # 订阅回调接收了全部 7 条
        self.assertEqual(len(received), 7)

        # 取消订阅后不再接收
        unsub()
        buf.append("log line 7")
        self.assertEqual(len(received), 7)

    def test_desired_mode_persistence(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = ProcessManager(td)
            self.assertEqual(mgr._desired_mode, "stopped")

            mgr._save_desired_mode("observe")
            self.assertEqual(mgr._desired_mode, "observe")

            # 新实例读取相同持久化文件
            mgr2 = ProcessManager(td)
            self.assertEqual(mgr2._desired_mode, "observe")

            mgr._save_desired_mode("auto")
            self.assertEqual(ProcessManager(td)._desired_mode, "auto")

    def test_process_manager_idempotent_start_and_stop(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = ProcessManager(td)

            fake_proc = Mock()
            fake_proc.poll.return_value = None  # 进程运行中
            fake_proc.stdout = None

            with patch("subprocess.Popen", return_value=fake_proc) as mock_popen:
                # 首次启动
                ok, msg = mgr.start("observe")
                self.assertTrue(ok)
                self.assertEqual(mock_popen.call_count, 1)

                # 重复启动相同模式：应幂等返回成功，不创建新进程
                ok, msg = mgr.start("observe")
                self.assertTrue(ok)
                self.assertEqual(mock_popen.call_count, 1)

                # 停止 Worker
                ok, msg = mgr.stop()
                self.assertTrue(ok)
                self.assertTrue(fake_proc.terminate.called)

                # 重复停止：应幂等返回成功
                fake_proc.poll.return_value = 0
                ok, msg = mgr.stop()
                self.assertTrue(ok)

    def test_session_import_and_validation(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = ProcessManager(td)

            # 1. 尝试导入非法会话（非 yuketang.cn 域名）
            invalid_session = {
                "cookies": [
                    {
                        "name": "sessionid",
                        "value": "fake",
                        "domain": "not-yuketang.example",
                        "path": "/",
                    }
                ]
            }
            ok, msg = mgr.import_session(invalid_session)
            self.assertFalse(ok)
            self.assertIn("校验失败", msg)
            self.assertFalse(os.path.exists(mgr.session_path))

            # 2. 导入合法雨课堂会话
            valid_session = {
                "cookies": [
                    {
                        "name": "sessionid",
                        "value": "valid_token",
                        "domain": ".yuketang.cn",
                        "path": "/",
                        "expires": 253402300799,
                    }
                ]
            }
            ok, msg = mgr.import_session(valid_session)
            self.assertTrue(ok)
            self.assertTrue(os.path.exists(mgr.session_path))

            # 3. 校验元数据读取不泄露明文
            info = mgr.get_session_info()
            self.assertTrue(info["exists"])
            self.assertTrue(info["valid"])
            self.assertEqual(info["cookie_count"], 1)
            self.assertIn(".yuketang.cn", info["domains"])
            self.assertNotIn("valid_token", json.dumps(info))

    def test_get_records_and_percentiles(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = ProcessManager(td)

            # 创建测试 SQLite 数据库与记录
            conn = sqlite3.connect(mgr.records_db_path)
            conn.execute(
                """
                CREATE TABLE quiz_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT,
                    lesson_id TEXT,
                    question_id TEXT,
                    request_generation INTEGER,
                    stage TEXT,
                    detection_source TEXT,
                    ai_model TEXT,
                    submitted_answer TEXT,
                    submission_confirmed INTEGER,
                    error_reason TEXT,
                    detect_to_ready_ms REAL,
                    ready_to_ai_start_ms REAL,
                    ai_duration_ms REAL,
                    ai_to_validated_ms REAL,
                    validated_to_clicked_ms REAL,
                    clicked_to_confirmed_ms REAL,
                    total_end_to_end_ms REAL,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )
            # 插入 4 条测试记录
            conn.execute(
                """
                INSERT INTO quiz_records (question_id, stage, total_end_to_end_ms, created_at)
                VALUES ('q1', 'confirmed', 200.0, '2026-09-28 12:00:00'),
                       ('q2', 'confirmed', 300.0, '2026-09-28 12:01:00'),
                       ('q3', 'skipped', 50.0, '2026-09-28 12:02:00'),
                       ('q4', 'failed', NULL, '2026-09-28 12:03:00')
                """
            )
            conn.commit()
            conn.close()

            data = mgr.get_records()
            self.assertEqual(data["total"], 4)
            self.assertEqual(len(data["records"]), 4)
            stats = data["stats"]
            self.assertEqual(stats["confirmed"], 2)
            self.assertEqual(stats["skipped"], 1)
            self.assertEqual(stats["failed"], 1)
            self.assertIn("p50_ms", stats)
            self.assertIn("p95_ms", stats)


if __name__ == "__main__":
    unittest.main()
