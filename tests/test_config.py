import json
import os
import tempfile
import unittest

from src.config import Config


class ConfigPersistenceTests(unittest.TestCase):
    def test_multi_ai_defaults_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(os.path.join(directory, "config.json"))
            settings = config.to_dict()
            settings["ai_model"] = "多AI作答"

            self.assertEqual(settings["multi_ai_config_path"], "model_visible.ini")
            self.assertEqual(settings["multi_ai_timeout"], 20)
            self.assertEqual(config.save(settings), [])

            settings["multi_ai_timeout"] = 0
            self.assertIn("multi_ai_timeout 范围应为 1~300", config.save(settings))

    def test_yuketang_server_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(os.path.join(directory, "config.json"))
            settings = config.to_dict()

            settings["yuketang_server"] = "黄河雨课堂"
            self.assertEqual(config.save(settings), [])
            self.assertEqual(config.get("yuketang_server"), "黄河雨课堂")

            settings["yuketang_server"] = "火星雨课堂"
            self.assertIn(
                "雨课堂服务器必须是：雨课堂 / 荷塘雨课堂 / 长江雨课堂 / 黄河雨课堂",
                config.save(settings),
            )

    def test_yuketang_server_defaults_to_changjiang(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(os.path.join(directory, "config.json"))
            self.assertEqual(config.get("yuketang_server"), "长江雨课堂")

    def test_save_writes_valid_config_without_leaving_temp_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            config = Config(path)
            settings = config.to_dict()
            settings["quiz_refresh_interval"] = 2

            self.assertEqual(config.save(settings), [])
            with open(path, "r", encoding="utf-8") as file:
                saved = json.load(file)

            self.assertEqual(saved["quiz_refresh_interval"], 2)
            self.assertEqual(os.listdir(directory), ["config.json"])

    def test_persist_reports_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            config = Config(path)
            config.set("last_cookie_warn_date", "2026-09-01")

            self.assertTrue(config.persist())
            self.assertEqual(Config(path).get("last_cookie_warn_date"), "2026-09-01")

    def test_ai_strategy_defaults_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(os.path.join(directory, "config.json"))
            settings = config.to_dict()

            # 验证默认值
            self.assertEqual(settings["ai_strategy"], "fast_single")
            self.assertEqual(settings["ai_primary_model"], "豆包AI")
            self.assertEqual(settings["ai_consensus_quorum"], 2)
            self.assertEqual(settings["ai_total_budget_seconds"], 20.0)
            self.assertEqual(settings["submit_time_margin_seconds"], 3.0)
            self.assertEqual(settings["ai_max_concurrent_requests"], 4)

            # 策略名称非法
            settings["ai_strategy"] = "invalid_strategy"
            self.assertIn("ai_strategy 必须是 fast_single / race_first_valid / consensus", config.save(settings))

            # consensus 模式非法
            settings["ai_strategy"] = "consensus"
            settings["ai_consensus_mode"] = "majority_invalid"
            self.assertIn("ai_consensus_mode 必须是 quorum 或 strict_majority", config.save(settings))

            # 平票策略非法
            settings["ai_consensus_mode"] = "quorum"
            settings["ai_consensus_tie_breaker"] = "random"
            self.assertIn("ai_consensus_tie_breaker 必须是 priority 或 skip", config.save(settings))

            # 数值越界
            settings["ai_consensus_tie_breaker"] = "priority"
            settings["ai_backup_delay_ms"] = 999999
            self.assertIn("ai_backup_delay_ms 范围应为 0~60000", config.save(settings))

            # 合法配置保存成功
            settings["ai_backup_delay_ms"] = 500
            self.assertEqual(config.save(settings), [])
            self.assertEqual(config.get("ai_backup_delay_ms"), 500)


if __name__ == "__main__":
    unittest.main()
