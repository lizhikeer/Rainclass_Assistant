"""FastAPI 面板 API 路由与静态分发集成测试。"""

import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from src.web.app import app, manager
from src.config import Config


class WebAPITests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = self.temp_dir.name
        # 隔离 manager 数据目录
        manager.data_dir = self.data_dir
        manager.config_path = os.path.join(self.data_dir, "config.json")
        manager.session_path = os.path.join(self.data_dir, "browser_state.json")
        manager.health_path = os.path.join(self.data_dir, "health.json")
        manager.panel_state_path = os.path.join(self.data_dir, "panel_state.json")
        manager.records_db_path = os.path.join(self.data_dir, "records.db")

        # 初始化测试配置，写入一个敏感 key
        cfg = Config(manager.config_path)
        cfg.save({
            "doubao_api_key": "secret_doubao_key_12345",
            "custom_ai_api_key": "secret_custom_ai_key_99999",
            "ai_strategy": "fast_single",
            "submit_delay": 2,
        })

        self.client = TestClient(app)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_health_check_endpoint(self):
        resp = self.client.get("/api/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "healthy")
        self.assertIn("worker_running", data)

    def test_status_endpoint(self):
        resp = self.client.get("/api/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("worker_running", data)
        self.assertIn("desired_mode", data)
        self.assertIn("session", data)
        self.assertIn("config_dirty", data)

    def test_control_endpoint_validation_and_actions(self):
        # 1. 非法 action
        resp = self.client.post("/api/control", json={"action": "unknown_action"})
        self.assertEqual(resp.status_code, 400)

        # 2. 正常 stop
        with patch.object(manager, "stop", return_value=(True, "Worker 已停止")):
            resp = self.client.post("/api/control", json={"action": "stop"})
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json()["success"])

        # 3. 正常 start
        with patch.object(manager, "start", return_value=(True, "已成功启动 observe 模式")):
            resp = self.client.post("/api/control", json={"action": "start", "mode": "observe"})
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json()["success"])

    def test_config_masks_secrets_and_never_leaks(self):
        resp = self.client.get("/api/config")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        cfg = data["config"]
        flags = data["flags"]

        # 敏感 key 必须被掩码，绝不包含原明文
        self.assertNotIn("secret_doubao_key", json.dumps(data))
        self.assertNotIn("secret_custom_ai_key", json.dumps(data))
        self.assertIn("******", cfg["doubao_api_key"])
        self.assertIn("******", cfg["custom_ai_api_key"])
        self.assertTrue(flags["doubao_api_key_configured"])
        self.assertTrue(flags["custom_ai_api_key_configured"])

    def test_config_update_preserves_masked_key_unless_cleared(self):
        # 1. 提交更新时传入带星号的掩码值，应保留原 key
        update_payload = {
            "settings": {
                "submit_delay": 0,
                "doubao_api_key": "sec******345",
            }
        }
        resp = self.client.post("/api/config", json=update_payload)
        self.assertEqual(resp.status_code, 200)

        disk_cfg = Config(manager.config_path).to_dict()
        self.assertEqual(disk_cfg["submit_delay"], 0)
        self.assertEqual(disk_cfg["doubao_api_key"], "secret_doubao_key_12345")

        # 2. 提交更新并显式清空 key
        clear_payload = {
            "settings": {},
            "clear_keys": ["doubao_api_key"],
        }
        resp = self.client.post("/api/config", json=clear_payload)
        self.assertEqual(resp.status_code, 200)

        disk_cfg = Config(manager.config_path).to_dict()
        self.assertEqual(disk_cfg["doubao_api_key"], "")

    def test_session_upload_json_and_validation(self):
        # 1. 上传非法格式
        resp = self.client.post("/api/session", json="not a dict")
        self.assertEqual(resp.status_code, 400)

        # 2. 上传非法域名的会话（not-yuketang.example）
        fake_session = {
            "cookies": [
                {
                    "name": "sessionid",
                    "value": "fake",
                    "domain": "not-yuketang.example",
                    "path": "/",
                }
            ]
        }
        resp = self.client.post("/api/session", json=fake_session)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("校验失败", resp.json()["message"])

        # 3. 上传合法会话
        valid_session = {
            "cookies": [
                {
                    "name": "sessionid",
                    "value": "secret_cookie_val_xyz",
                    "domain": ".yuketang.cn",
                    "path": "/",
                    "expires": 253402300799,
                }
            ]
        }
        resp = self.client.post("/api/session", json=valid_session)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["success"])
        # 响应中绝不泄露具体 cookie 明文
        self.assertNotIn("secret_cookie_val_xyz", json.dumps(resp.json()))

    def test_records_and_logs_endpoints(self):
        # 1. 日志接口
        manager.log_buffer.append("测试 INFO 日志", level="INFO")
        manager.log_buffer.append("测试 WARN 日志", level="WARN")

        resp = self.client.get("/api/logs")
        self.assertEqual(resp.status_code, 200)
        lines = resp.json()["lines"]
        self.assertGreaterEqual(len(lines), 2)

        # 2. 答题记录接口
        resp = self.client.get("/api/records")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("records", resp.json())
        self.assertIn("stats", resp.json())

    def test_static_files_served(self):
        # 1. 首页 HTML
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Rainclass Assistant", resp.text)
        self.assertIn("style.css", resp.text)

        # 2. CSS 文件
        resp = self.client.get("/style.css")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("#0f1419", resp.text)

        # 3. JS 文件
        resp = self.client.get("/app.js")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("switchTab", resp.text)


if __name__ == "__main__":
    unittest.main()
