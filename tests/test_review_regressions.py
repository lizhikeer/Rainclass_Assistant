"""Regression tests covering issues identified in review-20260928 probes."""
import json
import time
import unittest
from unittest.mock import Mock, patch

from tests.test_bot_pages import FakeAI, FakeItem, FakePage, make_bot
from src.browser import validate_session_data
from src.ai.strategy import StrategyRunner
from src.ai.models import EndpointConfig, ModelCallResult
from src.storage import STAGE_SKIPPED, STAGE_CONFIRMED, STAGE_UNKNOWN, STAGE_FAILED


class ReviewRegressionTests(unittest.TestCase):
    def test_same_route_new_question_does_not_click_old_answer(self):
        """When teacher changes question on the same route, pending answer for old question must be discarded."""
        option = FakeItem()
        button = FakeItem()
        url = "https://changjiang.yuketang.cn/lesson/fullscreen/v3/123/exercise/1"
        page = FakePage(
            url,
            {
                'p[data-option]': [option],
                'p[data-option="A"]': [option],
                '[class*="submit-btn"]': [button],
            },
            evaluated={"id": "question-one", "text": "First question", "options": ["A:first"], "images": []},
        )
        ai = FakeAI(answer='{"type":"single","answers":"A"}', complete=False)
        bot = make_bot(pages=[page], ai=ai)

        with patch.object(bot, "_capture_question_image", return_value=None), \
             patch.object(bot, "_prepare_question_image_b64", return_value="valid_image_base64"):
            bot._handle_quiz(page)

        # Question changes on page before AI completes
        page.evaluated = {"id": "question-two", "text": "Different question", "options": ["A:other"], "images": []}
        ai.last_future.set_result('{"type":"single","answers":"A"}')

        with patch.object(bot, "_submit_answer", return_value=True) as submit:
            bot._answer(page)
            # Old option must NOT be clicked, and submit must NOT be called for the new question
            self.assertFalse(option.clicked, "Old answer option was clicked on new question!")
            self.assertFalse(submit.called, "Submit was called on new question with old answer!")

    def test_unknown_and_skipped_answer_not_overwritten_to_confirmed(self):
        """When AI returns unknown (no vision) or fill/sub, status must be recorded as skipped and NEVER confirmed."""
        option = FakeItem()
        button = FakeItem()
        url = "https://changjiang.yuketang.cn/lesson/fullscreen/v3/123/exercise/1"
        page = FakePage(
            url,
            {
                'p[data-option]': [option],
                'p[data-option="A"]': [option],
                '[class*="submit-btn"]': [button],
            },
            evaluated={"id": "question-one", "text": "First question", "options": ["A:first"], "images": []},
        )
        ai = FakeAI(answer='{"type":"unknown"}', complete=False)
        bot = make_bot(pages=[page], ai=ai)
        bot.storage = Mock()
        bot.storage.has_confirmed.return_value = False
        bot.storage.get_latest_record.return_value = None

        with patch.object(bot, "_capture_question_image", return_value=None), \
             patch.object(bot, "_prepare_question_image_b64", return_value="valid_image_base64"):
            bot._handle_quiz(page)

        ai.last_future.set_result('{"type":"unknown"}')
        bot._complete_pending_answer(page)

        recorded_stages = [call.kwargs.get("stage") for call in bot.storage.record_stage.call_args_list]
        self.assertIn(STAGE_SKIPPED, recorded_stages, "STAGE_SKIPPED was not recorded for unknown AI answer")
        self.assertNotIn(STAGE_CONFIRMED, recorded_stages, "STAGE_CONFIRMED was incorrectly recorded for skipped answer!")

        for call in bot.storage.record_stage.call_args_list:
            self.assertNotEqual(
                call.kwargs.get("submission_confirmed"), 1,
                "submission_confirmed=1 was set for skipped answer!",
            )

    def test_pending_identity_preserved_during_unconfirmed_submit(self):
        """When submission is unconfirmed (e.g. shutdown/timeout), task identity must be retained and marked as unknown."""
        option = FakeItem()
        button = FakeItem()
        url = "https://changjiang.yuketang.cn/lesson/fullscreen/v3/123/exercise/1"
        page = FakePage(
            url,
            {
                'p[data-option]': [option],
                'p[data-option="A"]': [option],
                '[class*="submit-btn"]': [button],
            },
            evaluated={"id": "question-one", "text": "First question", "options": ["A:first"], "images": []},
        )
        ai = FakeAI(answer='{"type":"single","answers":"A"}', complete=False)
        bot = make_bot(pages=[page], ai=ai)
        bot.storage = Mock()
        bot.storage.has_confirmed.return_value = False
        bot.storage.get_latest_record.return_value = None

        with patch.object(bot, "_capture_question_image", return_value=None), \
             patch.object(bot, "_prepare_question_image_b64", return_value="valid_image_base64"):
            bot._handle_quiz(page)

        ai.last_future.set_result('{"type":"single","answers":"A"}')

        # Simulate shutdown or uncertain submit
        def uncertain_submit(p, **kwargs):
            return False

        with patch.object(bot, "_submit_answer", side_effect=uncertain_submit):
            bot._complete_pending_answer(page)

        recorded_stages = [call.kwargs.get("stage") for call in bot.storage.record_stage.call_args_list]
        # Should record UNKNOWN or not claim FAILED as a definitive terminal state for unconfirmed in-flight submit
        self.assertNotIn(STAGE_CONFIRMED, recorded_stages)
        self.assertIn(STAGE_UNKNOWN, recorded_stages)

    def test_empty_screenshot_records_image_failed_without_ai_call(self):
        """When image preparation returns empty, zero requests must be sent to AI and no mock_image_base64 used."""
        url = "https://changjiang.yuketang.cn/lesson/fullscreen/v3/123/exercise/1"
        page = FakePage(
            url,
            evaluated={"id": "question-one", "text": "First question", "options": ["A:first"], "images": []},
        )
        bot = make_bot(pages=[page], ai=FakeAI(complete=False))

        with patch.object(bot, "_capture_question_image", return_value=None), \
             patch.object(bot, "_prepare_question_image_b64", return_value=""), \
             patch.object(bot.ai, "submit_answer") as mock_submit:
            bot._handle_quiz(page)
            # AI must not be called when no valid image could be prepared
            mock_submit.assert_not_called()

    def test_response_after_deadline_is_rejected(self):
        """When model response arrives after the absolute deadline, fast_single must reject it."""
        runner = StrategyRunner(max_workers=1)
        try:
            endpoint = EndpointConfig(
                name="fake",
                base_url="https://example.invalid/v1",
                api_key="FAKE",
                model="fake",
                timeout=0.01,
            )

            def slow_result(*args, **kwargs):
                time.sleep(0.02)
                return ModelCallResult(
                    model_name="fake",
                    vote=("single", "A"),
                    is_valid=True,
                    is_submittable=True,
                )

            with patch.object(runner, "call_endpoint", side_effect=slow_result):
                deadline = time.monotonic() + 0.005
                decision = runner.execute_fast_single(endpoint, None, "fake", "fake", deadline)
                self.assertFalse(decision.is_success, "Result arriving after deadline was accepted as success!")
        finally:
            runner.shutdown()

    def test_invalid_session_domain_rejected(self):
        """validate_session_data must reject domains that only contain 'yuketang' as a substring."""
        invalid_cookie_data = {
            "cookies": [
                {
                    "name": "fake",
                    "value": "FAKE",
                    "domain": "not-yuketang.example",
                    "path": "/",
                }
            ],
            "origins": [],
        }
        valid, msg = validate_session_data(invalid_cookie_data, "https://www.yuketang.cn")
        self.assertFalse(valid, "Invalid domain not-yuketang.example was accepted!")
        self.assertIn("不匹配", msg)


if __name__ == "__main__":
    unittest.main()
