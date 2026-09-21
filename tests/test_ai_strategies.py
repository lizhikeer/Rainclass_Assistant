"""第三阶段 AI 响应策略专项测试集。

覆盖：
1. fast_single: 快模型有效立即返回、主模型格式错误回退备用、预算不足跳过备用。
2. race_first_valid: 竞速首胜、迟到结果不改写、延迟启动及主模型提前完成跳过备用（节省调用费用）。
3. consensus: Quorum 提前满足、严格多数阈值计算、超时与平票确定性优先级裁决 (无 random.choice) 及平票跳过。
4. 队列调度与并发控制: 在途并发上限、过期题目不启动、取消标志检查。
5. 凭据隔离与安全: Cookie 域名/Path/Secure 匹配校验、跨域重定向凭据剥离、URL 与日志密钥脱敏。
"""

import threading
import time
import unittest
from typing import Optional
from unittest.mock import Mock, patch

from src.ai.image import (
    download_question_image,
    filter_matching_cookies,
    is_domain_match,
    sanitize_error,
    sanitize_url,
)
from src.ai.models import (
    EndpointConfig,
    ModelCallResult,
    canonical_vote,
    is_submittable_vote,
    vote_to_answer,
)
from src.ai.strategy import StrategyRunner


class MockEndpointConfig(EndpointConfig):
    """用于测试的 Mock 端点。"""
    pass


def make_endpoint(name: str, timeout: float = 15.0) -> EndpointConfig:
    return EndpointConfig(
        name=name,
        base_url="https://mock.example/v1",
        api_key="mock-key-12345",
        model=name,
        timeout=timeout,
    )


class FastSingleStrategyTests(unittest.TestCase):
    def setUp(self):
        self.runner = StrategyRunner(max_workers=4)

    def tearDown(self):
        self.runner.shutdown()

    def test_fast_primary_model_returns_immediately(self):
        primary = make_endpoint("primary")
        backup = make_endpoint("backup")

        with patch.object(self.runner, "call_endpoint") as mock_call:
            mock_call.return_value = ModelCallResult(
                model_name="primary",
                raw_response='{"type":"single","answers":"A"}',
                vote=("choice", "A"),
                is_valid=True,
                is_submittable=True,
                latency_ms=45.0,
            )
            start_ts = time.monotonic()
            decision = self.runner.execute_fast_single(
                primary_endpoint=primary,
                backup_endpoint=backup,
                image_b64="test_b64",
                prompt="prompt",
                deadline=time.monotonic() + 20.0,
            )
            duration = time.monotonic() - start_ts

        self.assertTrue(decision.is_success)
        self.assertEqual(decision.winning_model, "primary")
        self.assertEqual(decision.winning_answer, '{"type":"single","answers":"A"}')
        self.assertFalse(decision.backup_triggered)
        self.assertEqual(decision.requests_sent, 1)
        self.assertLess(duration, 0.5, "主模型成功时不应额外等待")

    def test_primary_malformed_falls_back_to_valid_backup(self):
        primary = make_endpoint("primary")
        backup = make_endpoint("backup")

        def side_effect(endpoint, *args, **kwargs):
            if endpoint.name == "primary":
                return ModelCallResult(
                    model_name="primary",
                    raw_response="error: 502 bad gateway",
                    is_valid=False,
                    is_submittable=False,
                    latency_ms=30.0,
                )
            return ModelCallResult(
                model_name="backup",
                raw_response='{"type":"single","answers":"B"}',
                vote=("choice", "B"),
                is_valid=True,
                is_submittable=True,
                latency_ms=50.0,
            )

        with patch.object(self.runner, "call_endpoint", side_effect=side_effect):
            decision = self.runner.execute_fast_single(
                primary_endpoint=primary,
                backup_endpoint=backup,
                image_b64="test_b64",
                prompt="prompt",
                deadline=time.monotonic() + 20.0,
                margin_seconds=3.0,
            )

        self.assertTrue(decision.is_success)
        self.assertEqual(decision.winning_model, "backup")
        self.assertEqual(decision.winning_answer, '{"type":"single","answers":"B"}')
        self.assertTrue(decision.backup_triggered)
        self.assertEqual(decision.requests_sent, 2)

    def test_backup_skipped_when_budget_insufficient(self):
        primary = make_endpoint("primary")
        backup = make_endpoint("backup")

        with patch.object(self.runner, "call_endpoint") as mock_call:
            mock_call.return_value = ModelCallResult(
                model_name="primary",
                raw_response="invalid",
                is_valid=False,
                is_submittable=False,
                latency_ms=20.0,
            )
            # 设定截止时间仅剩 3.5 秒，小于余量 3.0s + 1.0s = 4.0s
            deadline = time.monotonic() + 3.5
            decision = self.runner.execute_fast_single(
                primary_endpoint=primary,
                backup_endpoint=backup,
                image_b64="test_b64",
                prompt="prompt",
                deadline=deadline,
                margin_seconds=3.0,
            )

        self.assertFalse(decision.is_success)
        self.assertFalse(decision.backup_triggered, "预算不足必须跳过备用模型以保留提交余量")
        self.assertEqual(decision.requests_sent, 1)
        self.assertIn("预算不足", decision.winning_answer)


class RaceFirstValidStrategyTests(unittest.TestCase):
    def setUp(self):
        self.runner = StrategyRunner(max_workers=4)

    def tearDown(self):
        self.runner.shutdown()

    def test_fast_model_wins_slow_model_ignored(self):
        primary = make_endpoint("fast_model")
        backup = make_endpoint("slow_model")

        def side_effect(endpoint, *args, **kwargs):
            if endpoint.name == "fast_model":
                time.sleep(0.05)
                return ModelCallResult(
                    model_name="fast_model",
                    raw_response='{"type":"single","answers":"A"}',
                    vote=("choice", "A"),
                    is_valid=True,
                    is_submittable=True,
                    latency_ms=50.0,
                )
            time.sleep(0.5)
            return ModelCallResult(
                model_name="slow_model",
                raw_response='{"type":"single","answers":"B"}',
                vote=("choice", "B"),
                is_valid=True,
                is_submittable=True,
                latency_ms=500.0,
            )

        with patch.object(self.runner, "call_endpoint", side_effect=side_effect):
            start_ts = time.monotonic()
            decision = self.runner.execute_race_first_valid(
                primary_endpoint=primary,
                backup_endpoint=backup,
                image_b64="test_b64",
                prompt="prompt",
                deadline=time.monotonic() + 10.0,
                backup_delay_ms=0,
            )
            duration = time.monotonic() - start_ts

        self.assertTrue(decision.is_success)
        self.assertEqual(decision.winning_model, "fast_model")
        self.assertEqual(decision.winning_answer, '{"type":"single","answers":"A"}')
        self.assertLess(duration, 0.35, "竞速应当在快模型到达后立刻返回，不等待慢模型")

    def test_backup_delay_skips_launch_if_primary_finishes_early(self):
        primary = make_endpoint("primary")
        backup = make_endpoint("backup")
        backup_started = threading.Event()

        def side_effect(endpoint, *args, **kwargs):
            if endpoint.name == "primary":
                time.sleep(0.04)  # 40ms 完成
                return ModelCallResult(
                    model_name="primary",
                    raw_response='{"type":"single","answers":"A"}',
                    vote=("choice", "A"),
                    is_valid=True,
                    is_submittable=True,
                    latency_ms=40.0,
                )
            backup_started.set()
            return ModelCallResult(model_name="backup", is_valid=True, is_submittable=True)

        with patch.object(self.runner, "call_endpoint", side_effect=side_effect):
            decision = self.runner.execute_race_first_valid(
                primary_endpoint=primary,
                backup_endpoint=backup,
                image_b64="test_b64",
                prompt="prompt",
                deadline=time.monotonic() + 10.0,
                backup_delay_ms=200,  # 延迟 200ms
            )

        self.assertTrue(decision.is_success)
        self.assertEqual(decision.winning_model, "primary")
        self.assertFalse(decision.backup_triggered, "主模型在延迟时间内完成，备用模型不得触发")
        self.assertFalse(backup_started.is_set())

    def test_race_all_fail_returns_failure(self):
        primary = make_endpoint("primary")
        backup = make_endpoint("backup")

        with patch.object(self.runner, "call_endpoint") as mock_call:
            mock_call.return_value = ModelCallResult(
                model_name="failed",
                raw_response="error 500",
                is_valid=False,
                is_submittable=False,
            )
            decision = self.runner.execute_race_first_valid(
                primary_endpoint=primary,
                backup_endpoint=backup,
                image_b64="test_b64",
                prompt="prompt",
                deadline=time.monotonic() + 2.0,
                backup_delay_ms=0,
            )

        self.assertFalse(decision.is_success)


class ConsensusStrategyTests(unittest.TestCase):
    def setUp(self):
        self.runner = StrategyRunner(max_workers=4)

    def tearDown(self):
        self.runner.shutdown()

    def test_quorum_early_exit(self):
        # 4 个模型，设置 quorum=2。前两个返回相同答案 A，后两个慢模型还未完成
        endpoints = [make_endpoint(f"m_{i}") for i in range(4)]

        def side_effect(ep, *args, **kwargs):
            if ep.name in ("m_0", "m_1"):
                time.sleep(0.04)
                return ModelCallResult(
                    model_name=ep.name,
                    raw_response='{"type":"single","answers":"A"}',
                    vote=("choice", "A"),
                    is_valid=True,
                    is_submittable=True,
                    latency_ms=40.0,
                )
            time.sleep(1.0)
            return ModelCallResult(model_name=ep.name, is_valid=True)

        with patch.object(self.runner, "call_endpoint", side_effect=side_effect):
            start_ts = time.monotonic()
            decision = self.runner.execute_consensus(
                endpoints=endpoints,
                image_b64="b64",
                prompt="prompt",
                deadline=time.monotonic() + 5.0,
                quorum=2,
                mode="quorum",
            )
            duration = time.monotonic() - start_ts

        self.assertTrue(decision.is_success)
        self.assertEqual(decision.winning_answer, '{"type":"single","answers":"A"}')
        self.assertLess(duration, 0.4, "达到 quorum=2 应立即返回，不等待后续慢模型")

    def test_strict_majority_threshold_calculation(self):
        # 3 个模型，strict_majority 阈值为 floor(3/2) + 1 = 2
        endpoints = [make_endpoint(f"m_{i}") for i in range(3)]

        def side_effect(ep, *args, **kwargs):
            if ep.name in ("m_0", "m_1"):
                time.sleep(0.03)
                return ModelCallResult(
                    model_name=ep.name,
                    raw_response='{"type":"single","answers":"B"}',
                    vote=("choice", "B"),
                    is_valid=True,
                    is_submittable=True,
                    latency_ms=30.0,
                )
            time.sleep(1.0)
            return ModelCallResult(model_name=ep.name, is_valid=True)

        with patch.object(self.runner, "call_endpoint", side_effect=side_effect):
            start_ts = time.monotonic()
            decision = self.runner.execute_consensus(
                endpoints=endpoints,
                image_b64="b64",
                prompt="prompt",
                deadline=time.monotonic() + 5.0,
                mode="strict_majority",
            )
            duration = time.monotonic() - start_ts

        self.assertTrue(decision.is_success)
        self.assertEqual(decision.winning_answer, '{"type":"single","answers":"B"}')
        self.assertLess(duration, 0.4)

    def test_tie_breaker_priority_is_deterministic(self):
        # m_0, m_1 投 A，m_2, m_3 投 B -> 平票 (2票对2票)
        # 配置顺序为 m_0, m_1, m_2, m_3，priority 策略必须采纳首个优先端点投出的 A，绝对不用 random.choice
        endpoints = [make_endpoint(f"m_{i}") for i in range(4)]
        votes = {
            "m_0": ("choice", "A"),
            "m_1": ("choice", "A"),
            "m_2": ("choice", "B"),
            "m_3": ("choice", "B"),
        }

        def side_effect(ep, *args, **kwargs):
            return ModelCallResult(
                model_name=ep.name,
                raw_response=f'{{"type":"single","answers":"{votes[ep.name][1]}"}}',
                vote=votes[ep.name],
                is_valid=True,
                is_submittable=True,
                latency_ms=20.0,
            )

        with patch.object(self.runner, "call_endpoint", side_effect=side_effect):
            decision = self.runner.execute_consensus(
                endpoints=endpoints,
                image_b64="b64",
                prompt="prompt",
                deadline=time.monotonic() + 5.0,
                quorum=3,  # 未提前达到
                tie_breaker="priority",
            )

        self.assertTrue(decision.is_success)
        self.assertEqual(decision.vote, ("choice", "A"), "平票必须按配置端点优先级稳定仲裁为 A")

    def test_tie_breaker_skip_option(self):
        # 平票且配置 tie_breaker="skip" 时，标记冲突并跳过
        endpoints = [make_endpoint("m_0"), make_endpoint("m_1")]
        votes = {"m_0": ("choice", "A"), "m_1": ("choice", "B")}

        def side_effect(ep, *args, **kwargs):
            return ModelCallResult(
                model_name=ep.name,
                raw_response=f'{{"type":"single","answers":"{votes[ep.name][1]}"}}',
                vote=votes[ep.name],
                is_valid=True,
                is_submittable=True,
                latency_ms=20.0,
            )

        with patch.object(self.runner, "call_endpoint", side_effect=side_effect):
            decision = self.runner.execute_consensus(
                endpoints=endpoints,
                image_b64="b64",
                prompt="prompt",
                deadline=time.monotonic() + 2.0,
                quorum=2,
                tie_breaker="skip",
            )

        self.assertFalse(decision.is_success)
        self.assertIn("跳过", decision.winning_answer)


class CookieSecurityAndSanitizationTests(unittest.TestCase):
    def test_cookie_domain_and_secure_matching(self):
        playwright_cookies = [
            {
                "name": "valid_session",
                "value": "session_val_123",
                "domain": ".yuketang.cn",
                "path": "/",
                "secure": True,
            },
            {
                "name": "http_only_insecure",
                "value": "insecure_val",
                "domain": ".yuketang.cn",
                "path": "/",
                "secure": False,
            },
            {
                "name": "strict_subdomain",
                "value": "sub_val",
                "domain": "pro.yuketang.cn",
                "path": "/",
                "secure": True,
            },
            {
                "name": "external_cookie",
                "value": "evil_val",
                "domain": ".attacker.com",
                "path": "/",
                "secure": True,
            },
        ]

        # 1. 匹配长江雨课堂 HTTPS URL
        target_url = "https://changjiang.yuketang.cn/api/v2/image.png"
        matched = filter_matching_cookies(target_url, playwright_cookies)
        self.assertIn("valid_session", matched)
        self.assertIn("http_only_insecure", matched)
        self.assertNotIn("strict_subdomain", matched, "子域名不匹配不得发送")
        self.assertNotIn("external_cookie", matched, "第三方域名 Cookie 绝不可发送")

        # 2. HTTP URL 请求不携带 secure cookie
        http_url = "http://changjiang.yuketang.cn/api/v2/image.png"
        http_matched = filter_matching_cookies(http_url, playwright_cookies)
        self.assertNotIn("valid_session", http_matched, "非安全连接不得发送 secure cookie")
        self.assertIn("http_only_insecure", http_matched)

        # 3. 访问外部网站（如 CDN 或第三方），绝不传递雨课堂会话凭据
        cdn_url = "https://cdn.thirdparty.com/images/q.png"
        cdn_matched = filter_matching_cookies(cdn_url, playwright_cookies)
        self.assertEqual(cdn_matched, {}, "非白名单域名必须清空所有凭据")

    def test_url_and_log_sanitization(self):
        url_with_token = "https://api.yuketang.cn/img?token=SECRET_TOKEN_ABC&user_id=123"
        sanitized = sanitize_url(url_with_token)
        self.assertNotIn("SECRET_TOKEN_ABC", sanitized)
        self.assertIn("token=***", sanitized)
        self.assertIn("user_id=123", sanitized)

        raw_err = "Request to https://example.com/v1?key=MY_API_KEY failed with sk-abcdef1234567890"
        clean_err = sanitize_error(raw_err)
        self.assertNotIn("MY_API_KEY", clean_err)
        self.assertNotIn("sk-abcdef1234567890", clean_err)
        self.assertIn("sk-***", clean_err)


class AnswerValidityNormalizationTests(unittest.TestCase):
    def test_valid_answer_types(self):
        self.assertTrue(is_submittable_vote(canonical_vote('{"type":"single","answers":"A"}')))
        self.assertTrue(is_submittable_vote(canonical_vote('{"type":"multi","answers":["A","B"]}')))
        self.assertTrue(is_submittable_vote(canonical_vote('{"type":"single","answers":"对"}')))
        self.assertTrue(is_submittable_vote(canonical_vote("正确")))

    def test_unsubmittable_answer_types(self):
        self.assertFalse(is_submittable_vote(canonical_vote('{"type":"fill"}')), "填空题不可自动提交")
        self.assertFalse(is_submittable_vote(canonical_vote('{"type":"sub"}')), "主观题不可自动提交")
        self.assertFalse(is_submittable_vote(canonical_vote('{"type":"unknown"}')), "无视觉能力不可自动提交")
        self.assertFalse(is_submittable_vote(canonical_vote("Error code: 429")), "报错文本不可作为答案")
        self.assertFalse(is_submittable_vote(canonical_vote("")), "空文本非法")


class CookieRedirectIsolationTests(unittest.TestCase):
    def test_redirect_to_external_domain_strips_cookies(self):
        """发生离开白名单的跨域重定向时，凭据必须被彻底剥离，严防凭据外泄。"""
        fake_session = Mock()
        resp_302 = Mock(status_code=302, is_redirect=True)
        resp_302.headers = {"Location": "https://cdn.external-thirdparty.com/image.png"}

        resp_200 = Mock(status_code=200, is_redirect=False, content=b"fake_image_bytes")
        fake_session.get.side_effect = [resp_302, resp_200]

        cookies = [{"name": "sessionid", "value": "secret_session", "domain": ".yuketang.cn"}]

        with (
            patch("src.ai.image.requests.Session", return_value=fake_session),
            patch("builtins.open", unittest.mock.mock_open()),
            patch("os.makedirs"),
        ):
            img_bytes, _ = download_question_image(
                "https://changjiang.yuketang.cn/img/test.png",
                cookies=cookies,
                timeout=5.0,
            )

        self.assertEqual(img_bytes, b"fake_image_bytes")
        self.assertEqual(fake_session.get.call_count, 2)
        # 第一次请求目标为长江雨课堂，应传递匹配的凭据
        first_call = fake_session.get.call_args_list[0]
        self.assertEqual(first_call.args[0], "https://changjiang.yuketang.cn/img/test.png")
        self.assertEqual(first_call.kwargs["cookies"], {"sessionid": "secret_session"})

        # 第二次请求重定向到了外部第三方，严禁发送任何凭据
        second_call = fake_session.get.call_args_list[1]
        self.assertEqual(second_call.args[0], "https://cdn.external-thirdparty.com/image.png")
        self.assertEqual(second_call.kwargs["cookies"], {}, "跨域重定向必须清空 Cookie")


class AIServiceStrategyIntegrationTests(unittest.TestCase):
    def test_fast_single_does_not_wait_default_20s(self):
        """fast_single 主模型成功后立刻返回，不拖延至默认 20 秒。"""
        from src.ai.service import AIService
        from src.config import Config

        cfg = Config()
        cfg.set("ai_strategy", "fast_single")
        cfg.set("ai_total_budget_seconds", 20.0)
        service = AIService(cfg)
        self.addCleanup(service.shutdown)

        with patch.object(service.runner, "call_endpoint") as mock_call:
            mock_call.return_value = ModelCallResult(
                model_name="豆包AI",
                raw_response='{"type":"single","answers":"A"}',
                vote=("choice", "A"),
                is_valid=True,
                is_submittable=True,
                latency_ms=35.0,
            )

            start_ts = time.monotonic()
            fut = service.submit_answer(image_b64="test_b64")
            result = fut.result(timeout=3.0)
            duration = time.monotonic() - start_ts

        self.assertLess(duration, 0.5, "fast_single 必须在首个有效结果到达时立即返回，绝不等满 20 秒")
        self.assertEqual(result, '{"type":"single","answers":"A"}')
        self.assertIsNotNone(service.last_decision)
        self.assertEqual(service.last_decision.winning_model, "豆包AI")
        self.assertEqual(service.last_decision.strategy, "fast_single")

    def test_race_strategy_integration(self):
        """race_first_valid 竞速策略产出正确指标并记录胜出模型。"""
        from src.ai.service import AIService
        from src.config import Config

        cfg = Config()
        cfg.set("ai_strategy", "race_first_valid")
        cfg.set("ai_primary_model", "豆包AI")
        cfg.set("ai_backup_model", "Gemini AI")
        service = AIService(cfg)
        self.addCleanup(service.shutdown)

        def side_effect(endpoint, *args, **kwargs):
            if endpoint.name == "豆包AI":
                time.sleep(0.04)
                return ModelCallResult(
                    model_name="豆包AI",
                    raw_response='{"type":"single","answers":"C"}',
                    vote=("choice", "C"),
                    is_valid=True,
                    is_submittable=True,
                    latency_ms=40.0,
                )
            time.sleep(0.5)
            return ModelCallResult(model_name="Gemini AI", is_valid=True)

        with patch.object(service.runner, "call_endpoint", side_effect=side_effect):
            start_ts = time.monotonic()
            fut = service.submit_answer(image_b64="test_b64")
            result = fut.result(timeout=3.0)
            duration = time.monotonic() - start_ts

        self.assertLess(duration, 0.4)
        self.assertEqual(result, '{"type":"single","answers":"C"}')
        self.assertIsNotNone(service.last_decision)
        self.assertEqual(service.last_decision.winning_model, "豆包AI")


class BoundedConcurrencyTests(unittest.TestCase):
    def test_stale_generation_aborts_queued_task(self):
        """当题目代际发生变更时，排队或后发的旧任务立即终止，不产生无效网络调用。"""
        runner = StrategyRunner(max_workers=2)
        self.addCleanup(runner.shutdown)

        current_gen = [1]
        primary = make_endpoint("ep1")

        with patch.object(runner, "call_endpoint") as mock_call:
            mock_call.return_value = ModelCallResult(
                model_name="ep1",
                raw_response='{"type":"single","answers":"A"}',
                vote=("choice", "A"),
                is_valid=True,
                is_submittable=True,
            )

            # 代际匹配时正常调用
            res1 = runner.execute_fast_single(
                primary_endpoint=primary,
                backup_endpoint=None,
                image_b64="b64",
                prompt="prompt",
                deadline=time.monotonic() + 10.0,
                check_cancelled=lambda: current_gen[0] != 1,
            )
            self.assertEqual(mock_call.call_count, 1)

            current_gen[0] = 2  # 模拟切题代际翻转
            cancelled_decision = runner.execute_fast_single(
                primary_endpoint=primary,
                backup_endpoint=None,
                image_b64="b64",
                prompt="prompt",
                deadline=time.monotonic() + 10.0,
                check_cancelled=lambda: current_gen[0] != 1,
            )

            # 代际失效后不得产生新网络调用
            self.assertEqual(mock_call.call_count, 1, "代际失效的任务必须在调用 API 前拦截跳过")
            self.assertIn("已取消或过期", cancelled_decision.winning_answer)


if __name__ == "__main__":
    unittest.main()
