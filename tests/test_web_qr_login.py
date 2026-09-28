"""Stage 8 单元与集成测试：雨课堂网页扫码登录管理器、API 接口与状态机互斥流转。"""

import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from src.web.app import app
from src.web.login_manager import (
    DEFAULT_LOGIN_TIMEOUT,
    LOGIN_STATE_CANCELLED,
    LOGIN_STATE_EXPIRED,
    LOGIN_STATE_FAILED,
    LOGIN_STATE_IDLE,
    LOGIN_STATE_NEEDS_MANUAL,
    LOGIN_STATE_STARTING,
    LOGIN_STATE_SUCCESS,
    LOGIN_STATE_WAITING_SCAN,
    QRLoginManager,
)
from src.web.manager import LogRingBuffer, ProcessManager


class TestQRLoginManager(unittest.TestCase):
    """测试 QRLoginManager 的核心逻辑与状态机。"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.log_buffer = LogRingBuffer(capacity=100)
        self.process_manager = ProcessManager(self.temp_dir)
        self.login_manager = QRLoginManager(
            self.temp_dir,
            self.process_manager,
            self.log_buffer,
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_initial_status_idle(self):
        """初始状态应为空闲 (idle)。"""
        status = self.login_manager.get_status()
        self.assertFalse(status["active"])
        self.assertEqual(status["state"], LOGIN_STATE_IDLE)
        self.assertEqual(status["expires_in"], 0)
        self.assertIsNone(status["qr_image"])

    def test_invalid_server_rejected(self):
        """不支持的服务器名称应直接报错。"""
        ok, msg, data = self.login_manager.start_login("未知高校雨课堂")
        self.assertFalse(ok)
        self.assertIn("未知的雨课堂服务器", msg)

        ok2, msg2, _ = self.login_manager.start_login("https://evil.not-yuketang.com")
        self.assertFalse(ok2)
        self.assertIn("不支持的非雨课堂服务器域名", msg2)

    def test_worker_paused_and_restored_on_cancel(self):
        """若启动扫码登录时 Worker 正在运行，应先暂停 Worker 并在取消登录时恢复。"""
        # 模拟 Worker 处于运行中
        self.process_manager.is_running = MagicMock(return_value=True)
        self.process_manager.get_desired_mode = MagicMock(return_value="auto")
        self.process_manager.stop = MagicMock(return_value=(True, "ok"))
        self.process_manager.start = MagicMock(return_value=(True, "ok"))

        # 阻止真实 Playwright 线程启动
        with patch.object(self.login_manager, "_run_login_worker"):
            ok, msg, data = self.login_manager.start_login("雨课堂")
            self.assertTrue(ok)
            self.process_manager.stop.assert_called_once()

            status = self.login_manager.get_status()
            self.assertTrue(status["active"])
            self.assertEqual(status["state"], LOGIN_STATE_STARTING)

            # 取消登录
            cancel_ok, cancel_msg = self.login_manager.cancel_login()
            self.assertTrue(cancel_ok)
            self.process_manager.start.assert_called_once_with(mode="auto")

            post_status = self.login_manager.get_status()
            self.assertFalse(post_status["active"])
            self.assertEqual(post_status["state"], LOGIN_STATE_CANCELLED)

    def test_double_start_rejected(self):
        """已有正在进行的任务时，禁止再次启动。"""
        with patch.object(self.login_manager, "_run_login_worker"):
            ok1, _, _ = self.login_manager.start_login("雨课堂")
            self.assertTrue(ok1)

            ok2, msg2, _ = self.login_manager.start_login("长江雨课堂")
            self.assertFalse(ok2)
            self.assertIn("已有正在进行的扫码登录任务", msg2)

    def test_refresh_qr_flow(self):
        """处于 waiting_scan 状态时支持触发刷新。"""
        with self.login_manager._lock:
            refresh_event = threading.Event()
            self.login_manager._active_session = {
                "session_id": "test-sid",
                "state": LOGIN_STATE_WAITING_SCAN,
                "refresh_event": refresh_event,
                "expires_at": time.time() + 100,
                "message": "等待扫码",
            }

        ok, msg = self.login_manager.refresh_qr("test-sid")
        self.assertTrue(ok)
        self.assertTrue(refresh_event.is_set())

    def test_update_qr_image_extracts_base64_src(self):
        """页面 img.logma 具备 data:image 前缀时直接提取。"""
        mock_page = MagicMock()
        mock_locator = MagicMock()
        mock_page.locator.return_value = mock_locator
        mock_locator.count.return_value = 1
        fake_b64 = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAA..."
        mock_locator.first.get_attribute.return_value = fake_b64

        session = {"qr_image": None, "state": LOGIN_STATE_STARTING, "message": ""}
        self.login_manager._update_qr_image(mock_page, session)

        self.assertEqual(session["qr_image"], fake_b64)
        self.assertEqual(session["state"], LOGIN_STATE_WAITING_SCAN)

    def test_check_captcha_obstacles(self):
        """检测到验证码时应返回 True。"""
        mock_page = MagicMock()
        mock_locator = MagicMock()
        mock_page.locator.return_value = mock_locator

        # 模拟存在可见的验证码
        mock_locator.count.return_value = 1
        mock_el = MagicMock()
        mock_el.is_visible.return_value = True
        mock_locator.all.return_value = [mock_el]

        self.assertTrue(self.login_manager._check_captcha_obstacles(mock_page))


class TestQRLoginAPI(unittest.TestCase):
    """测试 FastAPI 扫码登录 RESTful API 契约与 HTTP 标头。"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.log_buffer = LogRingBuffer(capacity=100)
        self.process_manager = ProcessManager(self.temp_dir)
        self.login_manager = QRLoginManager(
            self.temp_dir,
            self.process_manager,
            self.log_buffer,
        )
        app.state.manager = self.process_manager
        app.state.login_manager = self.login_manager
        self.client = TestClient(app)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_get_qr_status_cache_control(self):
        """GET /api/login/qr/status 必须返回 Cache-Control: no-store。"""
        res = self.client.get("/api/login/qr/status")
        self.assertEqual(res.status_code, 200)
        self.assertIn("no-store", res.headers.get("Cache-Control", ""))
        data = res.json()
        self.assertIn("state", data)
        self.assertIn("active", data)
        self.assertIn("expires_in", data)

    def test_start_qr_login_api(self):
        """POST /api/login/qr/start 启动登录会话。"""
        with patch.object(self.login_manager, "_run_login_worker"):
            res = self.client.post("/api/login/qr/start", json={"server": "雨课堂"})
            self.assertEqual(res.status_code, 200)
            self.assertIn("no-store", res.headers.get("Cache-Control", ""))
            data = res.json()
            self.assertTrue(data["success"])
            self.assertIn("session_id", data["data"])

            # 重复启动返回 400
            res2 = self.client.post("/api/login/qr/start", json={"server": "雨课堂"})
            self.assertEqual(res2.status_code, 400)

            # 取消
            res3 = self.client.post("/api/login/qr/cancel")
            self.assertEqual(res3.status_code, 200)

            # 再次查询状态
            status_res = self.client.get("/api/login/qr/status")
            self.assertEqual(status_res.json()["state"], LOGIN_STATE_CANCELLED)


if __name__ == "__main__":
    unittest.main()
