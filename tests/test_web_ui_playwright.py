"""使用真实 Playwright Chromium 检验 Web 管理面板前端渲染与交互。"""

import os
import threading
import time
import unittest
import uvicorn
from playwright.sync_api import sync_playwright

from src.web.app import app


class WebUIPlaywrightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = 8099
        cls.config = uvicorn.Config(app, host="127.0.0.1", port=cls.port, log_level="warning")
        cls.server = uvicorn.Server(cls.config)
        cls.server_thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.server_thread.start()

        # 等待服务就绪
        deadline = time.monotonic() + 5.0
        while not cls.server.started and time.monotonic() < deadline:
            time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.server_thread.join(timeout=3.0)

    def test_web_ui_renders_dark_theme_and_tabs_switch(self):
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            # 1. 桌面端 1280x800 测试
            page.set_viewport_size({"width": 1280, "height": 800})
            page.goto(f"http://127.0.0.1:{self.port}/", wait_until="domcontentloaded")

            # 验证页面标题与品牌
            self.assertIn("雨课堂智能助手", page.title())
            title_text = page.locator(".brand-title").inner_text()
            self.assertEqual(title_text, "Rainclass Assistant")

            # 验证暗黑主题背景色 (#0f1419)
            body_bg = page.evaluate("() => window.getComputedStyle(document.body).backgroundColor")
            self.assertEqual(body_bg, "rgb(15, 20, 25)")  # #0f1419 的 RGB

            # 验证 5 个标签页按钮存在
            tabs = page.locator(".tab-btn")
            self.assertEqual(tabs.count(), 5)

            # 验证切换至参数配置 Tab
            page.locator('button[data-tab="tab-config"]').click()
            self.assertTrue(page.locator("#tab-config").is_visible())
            self.assertFalse(page.locator("#tab-dashboard").is_visible())

            # 验证切换至会话管理 Tab
            page.locator('button[data-tab="tab-session"]').click()
            self.assertTrue(page.locator("#tab-session").is_visible())
            self.assertTrue(page.locator("#dropzone").is_visible())

            # 验证切换至答题记录 Tab
            page.locator('button[data-tab="tab-records"]').click()
            self.assertTrue(page.locator("#tab-records").is_visible())
            self.assertTrue(page.locator("#records-tbody").is_visible())

            # 验证切换至控制台日志 Tab
            page.locator('button[data-tab="tab-logs"]').click()
            self.assertTrue(page.locator("#tab-logs").is_visible())
            self.assertTrue(page.locator("#terminal-box").is_visible())

            # 2. 移动端 375x667 响应式适配测试
            page.set_viewport_size({"width": 375, "height": 667})
            page.locator('button[data-tab="tab-dashboard"]').click()
            self.assertTrue(page.locator("#tab-dashboard").is_visible())

            # 验证主卡片在手机屏幕正常展示无溢出
            card_width = page.locator(".card").first.evaluate("el => el.getBoundingClientRect().width")
            self.assertLessEqual(card_width, 375)

            browser.close()


if __name__ == "__main__":
    unittest.main()
