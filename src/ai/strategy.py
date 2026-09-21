"""AI 响应策略引擎模块。

实现三种响应决策策略：
1. fast_single: 主模型先行，失败且预算充足时按配置尝试一次备用模型。
2. race_first_valid: 最多两个配置模型竞速，首个有效答案胜出，备用模型可延迟启动并支持前置跳过。
3. consensus: 多模型投票，支持 Quorum 提前返回、严格多数计算、超时确定性优先级平票或跳过。
"""

import logging
import queue
import threading
import time
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Optional, Sequence

import requests
from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

from src.ai.image import sanitize_error
from src.ai.models import (
    ANSWER_MAX_TOKENS,
    EndpointConfig,
    ModelCallResult,
    RoundDecision,
    canonical_vote,
    is_submittable_vote,
    vote_to_answer,
)

logger = logging.getLogger(__name__)


class StrategyRunner:
    """AI 策略执行器，负责连接池复用、并发控制、超时与多模型协调决策。"""

    def __init__(
        self,
        max_workers: int = 4,
    ) -> None:
        self._max_workers = max(1, min(32, max_workers))
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix="ai-runner",
        )
        self._clients: dict[tuple[str, str], OpenAI] = {}
        self._clients_lock = threading.Lock()
        self._closed = False

    def shutdown(self) -> None:
        """关闭线程池及所有复用的 HTTP 客户端连接。"""
        if self._closed:
            return
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)
        with self._clients_lock:
            for client in self._clients.values():
                try:
                    client.close()
                except Exception:
                    pass
            self._clients.clear()
        logger.debug("StrategyRunner 线程池与客户端连接已完全释放。")

    def _get_or_create_client(self, base_url: str, api_key: str) -> OpenAI:
        """复用指定 base_url 与 api_key 的 OpenAI 客户端实例。"""
        cache_key = (base_url.strip(), api_key.strip())
        with self._clients_lock:
            if cache_key in self._clients:
                return self._clients[cache_key]
            client = OpenAI(
                base_url=cache_key[0],
                api_key=cache_key[1],
                max_retries=0,
            )
            self._clients[cache_key] = client
            return client

    # ==================== 单模型调用底层 ====================

    def call_endpoint(
        self,
        endpoint: EndpointConfig,
        image_b64: str,
        prompt: str,
        timeout: float,
        mime_type: str = "image/png",
    ) -> ModelCallResult:
        """执行单次模型 API 请求，收集状态、耗时与 Token 使用量。"""
        result = ModelCallResult(model_name=endpoint.name)
        if timeout <= 0:
            result.error = "超时时限已用尽"
            result.cancelled = True
            return result

        start_time = time.monotonic()
        try:
            if endpoint.provider_type == "gemini":
                text, usage = self._call_gemini(
                    endpoint=endpoint,
                    image_b64=image_b64,
                    prompt=prompt,
                    timeout=timeout,
                    mime_type=mime_type,
                )
            else:
                text, usage = self._call_openai_compatible(
                    endpoint=endpoint,
                    image_b64=image_b64,
                    prompt=prompt,
                    timeout=timeout,
                    mime_type=mime_type,
                )

            result.raw_response = text
            result.usage = usage
            result.vote = canonical_vote(text)
            result.is_valid = result.vote is not None
            result.is_submittable = is_submittable_vote(result.vote)

        except Exception as exc:
            err_text = sanitize_error(str(exc))
            result.error = err_text
            logger.warning("模型 [%s] 调用异常：%s", endpoint.name, err_text)
        finally:
            result.latency_ms = (time.monotonic() - start_time) * 1000.0

        return result

    def _call_openai_compatible(
        self,
        endpoint: EndpointConfig,
        image_b64: str,
        prompt: str,
        timeout: float,
        mime_type: str = "image/png",
    ) -> tuple[str, Optional[dict[str, int]]]:
        client = self._get_or_create_client(endpoint.base_url, endpoint.api_key)
        kwargs: dict[str, Any] = {
            "model": endpoint.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime_type};base64,{image_b64}"},
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            "timeout": timeout,
            "max_tokens": ANSWER_MAX_TOKENS,
        }
        if endpoint.extra_body:
            kwargs["extra_body"] = endpoint.extra_body

        response = client.chat.completions.create(**kwargs)
        content = (response.choices[0].message.content or "").strip()
        usage = None
        if getattr(response, "usage", None):
            usage = {
                "prompt_tokens": getattr(response.usage, "prompt_tokens", 0),
                "completion_tokens": getattr(response.usage, "completion_tokens", 0),
                "total_tokens": getattr(response.usage, "total_tokens", 0),
            }
        return content, usage

    def _call_gemini(
        self,
        endpoint: EndpointConfig,
        image_b64: str,
        prompt: str,
        timeout: float,
        mime_type: str = "image/png",
    ) -> tuple[str, Optional[dict[str, int]]]:
        api_url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{endpoint.model}:generateContent?key={endpoint.api_key}"
        )
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {"inlineData": {"mimeType": mime_type, "data": image_b64}},
                    ]
                }
            ],
            "generationConfig": {
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        response = requests.post(api_url, json=payload, timeout=timeout)
        response.raise_for_status()
        result = response.json()
        candidates = result.get("candidates", [])
        if not candidates:
            reason = result.get("promptFeedback", {}).get("finishReason") or "无返回内容"
            raise RuntimeError(f"Gemini 返回异常: {reason}")
        text = (
            candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
        )
        usage = None
        if "usageMetadata" in result:
            um = result["usageMetadata"]
            usage = {
                "prompt_tokens": um.get("promptTokenCount", 0),
                "completion_tokens": um.get("candidatesTokenCount", 0),
                "total_tokens": um.get("totalTokenCount", 0),
            }
        return text, usage

    # ==================== 策略 A: fast_single ====================

    def execute_fast_single(
        self,
        primary_endpoint: EndpointConfig,
        backup_endpoint: Optional[EndpointConfig],
        image_b64: str,
        prompt: str,
        deadline: float,
        margin_seconds: float = 3.0,
        mime_type: str = "image/png",
        check_cancelled: Optional[Callable[[], bool]] = None,
    ) -> RoundDecision:
        """执行 fast_single 策略：主模型先行，失败且预算足够时尝试备用模型。"""
        decision = RoundDecision(strategy="fast_single")
        start_ts = time.monotonic()

        # 检查是否已超时或被取消
        if check_cancelled and check_cancelled():
            decision.winning_answer = "答题请求已取消或过期"
            return decision

        primary_timeout = max(0.05, min(primary_endpoint.timeout, deadline - time.monotonic()))
        decision.requests_sent += 1
        logger.info("fast_single: 启动主模型 [%s] (超时 %.1fs)", primary_endpoint.name, primary_timeout)

        primary_res = self.call_endpoint(
            primary_endpoint, image_b64, prompt, primary_timeout, mime_type
        )
        decision.details.append(primary_res)
        decision.model_latencies[primary_endpoint.name] = primary_res.latency_ms

        if primary_res.usage:
            decision.token_usage[primary_endpoint.name] = primary_res.usage

        if primary_res.is_submittable:
            decision.winning_model = primary_endpoint.name
            decision.vote = primary_res.vote
            decision.winning_answer = vote_to_answer(primary_res.vote)  # type: ignore
            decision.is_success = True
            decision.valid_results_count = 1
            decision.total_duration_ms = (time.monotonic() - start_ts) * 1000.0
            logger.info(
                "fast_single: 主模型 [%s] 成功获取有效答案 (%.1fms)：%s",
                primary_endpoint.name,
                primary_res.latency_ms,
                decision.winning_answer,
            )
            return decision

        logger.warning(
            "fast_single: 主模型 [%s] 未获得有效答案 (耗时 %.1fms, 错误: %s)",
            primary_endpoint.name,
            primary_res.latency_ms,
            primary_res.error or primary_res.raw_response[:80],
        )

        # 主模型未成功，评估是否启用备用模型
        if not backup_endpoint:
            decision.winning_answer = primary_res.raw_response or (primary_res.error or "主模型无有效答案")
            decision.total_duration_ms = (time.monotonic() - start_ts) * 1000.0
            return decision

        remaining_budget = deadline - time.monotonic()
        # 仅在剩余时间满足安全余量 + 1秒启动开销时才尝试备用模型
        if remaining_budget <= (margin_seconds + 1.0):
            logger.warning(
                "fast_single: 题目剩余时间不足 (%.1fs <= 余量 %.1fs + 1s)，放弃尝试备用模型 [%s]。",
                remaining_budget,
                margin_seconds,
                backup_endpoint.name,
            )
            decision.timeout_count += 1
            decision.winning_answer = "题目预算不足，放弃备用模型尝试"
            decision.total_duration_ms = (time.monotonic() - start_ts) * 1000.0
            return decision

        # 启动备用模型
        decision.backup_triggered = True
        decision.requests_sent += 1
        backup_timeout = max(0.05, min(backup_endpoint.timeout, remaining_budget - margin_seconds))
        logger.info(
            "fast_single: 触发备用模型 [%s] (预算 %.1fs, 超时 %.1fs)",
            backup_endpoint.name,
            remaining_budget,
            backup_timeout,
        )

        backup_res = self.call_endpoint(
            backup_endpoint, image_b64, prompt, backup_timeout, mime_type
        )
        decision.details.append(backup_res)
        decision.model_latencies[backup_endpoint.name] = backup_res.latency_ms
        if backup_res.usage:
            decision.token_usage[backup_endpoint.name] = backup_res.usage

        if backup_res.is_submittable:
            decision.winning_model = backup_endpoint.name
            decision.vote = backup_res.vote
            decision.winning_answer = vote_to_answer(backup_res.vote)  # type: ignore
            decision.is_success = True
            decision.valid_results_count = 1
            logger.info(
                "fast_single: 备用模型 [%s] 成功获取有效答案 (%.1fms)：%s",
                backup_endpoint.name,
                backup_res.latency_ms,
                decision.winning_answer,
            )
        else:
            decision.winning_answer = backup_res.raw_response or (backup_res.error or "备用模型无有效答案")
            logger.warning("fast_single: 备用模型 [%s] 也未能获得有效答案。", backup_endpoint.name)

        decision.total_duration_ms = (time.monotonic() - start_ts) * 1000.0
        return decision

    # ==================== 策略 B: race_first_valid ====================

    def execute_race_first_valid(
        self,
        primary_endpoint: EndpointConfig,
        backup_endpoint: Optional[EndpointConfig],
        image_b64: str,
        prompt: str,
        deadline: float,
        backup_delay_ms: int = 0,
        margin_seconds: float = 3.0,
        mime_type: str = "image/png",
        check_cancelled: Optional[Callable[[], bool]] = None,
    ) -> RoundDecision:
        """执行 race_first_valid 竞速策略：最多两个模型竞速，首个有效答案胜出。"""
        decision = RoundDecision(strategy="race_first_valid")
        start_ts = time.monotonic()

        logger.info(
            "race_first_valid: 启动竞速策略 (主模型: %s, 备用: %s, 延迟: %dms)。"
            " 注意：竞速策略可能增加 API 调用费用。",
            primary_endpoint.name,
            backup_endpoint.name if backup_endpoint else "无",
            backup_delay_ms,
        )

        if not backup_endpoint:
            return self.execute_fast_single(
                primary_endpoint=primary_endpoint,
                backup_endpoint=None,
                image_b64=image_b64,
                prompt=prompt,
                deadline=deadline,
                margin_seconds=margin_seconds,
                mime_type=mime_type,
                check_cancelled=check_cancelled,
            )

        cancel_round = threading.Event()
        result_queue: queue.Queue[ModelCallResult] = queue.Queue()
        backup_started_event = threading.Event()
        lock = threading.Lock()
        state = {
            "decided": False,
            "requests_sent": 0,
            "backup_triggered": False,
        }

        def run_task(endpoint: EndpointConfig, timeout: float) -> None:
            if cancel_round.is_set() or (check_cancelled and check_cancelled()):
                return
            res = self.call_endpoint(endpoint, image_b64, prompt, timeout, mime_type)
            result_queue.put(res)

        # 1. 提交主模型任务
        primary_timeout = max(0.05, min(primary_endpoint.timeout, deadline - time.monotonic()))
        with lock:
            state["requests_sent"] += 1
        self._executor.submit(run_task, primary_endpoint, primary_timeout)

        # 2. 备用模型调度线程
        def schedule_backup() -> None:
            delay_sec = max(0.0, backup_delay_ms / 1000.0)
            if delay_sec > 0:
                # 检查是否在此期间主模型已完成
                if cancel_round.wait(delay_sec):
                    logger.debug("race: 主模型在备用延迟内完成，跳过备用模型启动。")
                    return

            if cancel_round.is_set() or (check_cancelled and check_cancelled()):
                return

            # 校验剩余预算
            remaining = deadline - time.monotonic()
            if remaining <= margin_seconds:
                logger.warning("race: 题目剩余时间不足 (%.1fs <= 余量 %.1fs)，跳过备用模型启动。", remaining, margin_seconds)
                return

            with lock:
                state["backup_triggered"] = True
                state["requests_sent"] += 1
            backup_started_event.set()
            backup_timeout = max(0.05, min(backup_endpoint.timeout, remaining - margin_seconds))
            logger.info("race: 启动备用模型 [%s] 竞速 (超时 %.1fs)", backup_endpoint.name, backup_timeout)
            self._executor.submit(run_task, backup_endpoint, backup_timeout)

        backup_scheduler = threading.Thread(
            target=schedule_backup, name="race-backup-scheduler", daemon=True
        )
        backup_scheduler.start()

        # 3. 收集并裁决首个有效结果
        winner_res: Optional[ModelCallResult] = None
        completed_results: list[ModelCallResult] = []

        while True:
            remaining_wait = deadline - time.monotonic()
            if remaining_wait <= 0:
                decision.timeout_count += 1
                break

            try:
                item = result_queue.get(timeout=min(0.05, remaining_wait))
            except queue.Empty:
                if time.monotonic() >= deadline:
                    decision.timeout_count += 1
                    break
                # 若两个模型都已返回或已放弃，提前退出等待
                with lock:
                    if len(completed_results) >= state["requests_sent"] and state["requests_sent"] >= 2:
                        break
                continue

            completed_results.append(item)
            decision.details.append(item)
            decision.model_latencies[item.model_name] = item.latency_ms
            if item.usage:
                decision.token_usage[item.model_name] = item.usage

            if item.is_submittable and not winner_res:
                # 赢家产生！首个完整且合法的答案立即锁定
                winner_res = item
                cancel_round.set()
                logger.info(
                    "race: 模型 [%s] 率先胜出 (耗时 %.1fms)：%s",
                    item.model_name,
                    item.latency_ms,
                    vote_to_answer(item.vote),  # type: ignore
                )
                break

            with lock:
                # 若已收到所有发出的请求结果，退出等待
                if len(completed_results) >= state["requests_sent"] and (
                    state["requests_sent"] >= 2 or not backup_scheduler.is_alive()
                ):
                    break

        cancel_round.set()
        with lock:
            decision.requests_sent = state["requests_sent"]
            decision.backup_triggered = state["backup_triggered"]

        if winner_res:
            decision.winning_model = winner_res.model_name
            decision.vote = winner_res.vote
            decision.winning_answer = vote_to_answer(winner_res.vote)  # type: ignore
            decision.is_success = True
            decision.valid_results_count = sum(1 for r in completed_results if r.is_submittable)
        else:
            decision.winning_answer = "竞速模型均未能在截止时间内获得有效答案"
            decision.is_success = False

        # 统计迟到结果
        decision.late_results_count = max(0, decision.requests_sent - len(completed_results))
        decision.total_duration_ms = (time.monotonic() - start_ts) * 1000.0
        return decision

    # ==================== 策略 C: consensus ====================

    def execute_consensus(
        self,
        endpoints: Sequence[EndpointConfig],
        image_b64: str,
        prompt: str,
        deadline: float,
        quorum: int = 2,
        mode: str = "quorum",  # quorum / strict_majority
        tie_breaker: str = "priority",  # priority / skip
        mime_type: str = "image/png",
        check_cancelled: Optional[Callable[[], bool]] = None,
        truncate_event: Optional[threading.Event] = None,
    ) -> RoundDecision:
        """执行共识决策策略：并发调用多个模型，支持法定票数 (quorum) 或严格多数提前决策。"""
        decision = RoundDecision(strategy="consensus")
        start_ts = time.monotonic()
        total_models = len(endpoints)

        if total_models == 0:
            decision.winning_answer = "没有可用的模型配置"
            return decision

        decision.requests_sent = total_models
        cancel_round = threading.Event()
        result_queue: queue.Queue[tuple[str, ModelCallResult]] = queue.Queue()

        # 严格多数阈值按当前轮计划模型总数计算: floor(N/2) + 1
        majority_threshold = (total_models // 2) + 1
        target_quorum = majority_threshold if mode == "strict_majority" else max(1, quorum)

        logger.info(
            "consensus: 启动 %d 个模型共识 (模式: %s, 目标阈值: %d 票, 平票策略: %s, 超时: %.1fs)",
            total_models,
            mode,
            target_quorum,
            tie_breaker,
            max(0.1, deadline - time.monotonic()),
        )

        def worker(ep: EndpointConfig, timeout: float) -> None:
            if cancel_round.is_set() or (check_cancelled and check_cancelled()):
                return
            res = self.call_endpoint(ep, image_b64, prompt, timeout, mime_type)
            result_queue.put((ep.name, res))

        # 并发提交各模型调用
        for endpoint in endpoints:
            ep_timeout = max(0.05, min(endpoint.timeout, deadline - time.monotonic()))
            self._executor.submit(worker, endpoint, ep_timeout)

        received_count = 0
        valid_votes: list[tuple[str, ...]] = []
        model_vote_map: dict[str, tuple[str, ...]] = {}
        completed_results: list[ModelCallResult] = []
        early_winner_vote: Optional[tuple[str, ...]] = None

        while received_count < total_models:
            remaining_wait = deadline - time.monotonic()
            if remaining_wait <= 0:
                decision.timeout_count += 1
                logger.warning("consensus: 整体等待已达截止时间，停止等待剩余模型。")
                break

            if truncate_event and truncate_event.is_set():
                logger.info("consensus: 收到外部截断请求，停止等待并就地裁决。")
                break

            try:
                ep_name, item = result_queue.get(timeout=min(0.05, remaining_wait))
            except queue.Empty:
                continue

            received_count += 1
            completed_results.append(item)
            decision.details.append(item)
            decision.model_latencies[ep_name] = item.latency_ms
            if item.usage:
                decision.token_usage[ep_name] = item.usage

            if item.is_submittable and item.vote:
                valid_votes.append(item.vote)
                model_vote_map[ep_name] = item.vote
                counts = Counter(valid_votes)
                current_vote_count = counts[item.vote]
                logger.info(
                    "consensus: 模型 [%s] 返回有效答案 (%.1fms, 当前累计 %d 票)：%s",
                    ep_name,
                    item.latency_ms,
                    current_vote_count,
                    vote_to_answer(item.vote),
                )

                # 检查是否满足提前返回条件
                if current_vote_count >= target_quorum:
                    early_winner_vote = item.vote
                    cancel_round.set()
                    logger.info(
                        "consensus: 达到 %s 目标条件 (%d/%d 票)，提前决策！",
                        mode,
                        current_vote_count,
                        target_quorum,
                    )
                    break
            else:
                logger.info(
                    "consensus: 模型 [%s] 返回无效或非可提交答案 (%.1fms)：%s",
                    ep_name,
                    item.latency_ms,
                    item.error or item.raw_response[:80],
                )

        cancel_round.set()
        decision.valid_results_count = len(valid_votes)
        decision.late_results_count = total_models - received_count

        # 结果裁决
        if early_winner_vote:
            winning_vote = early_winner_vote
            decision.is_success = True
        elif not valid_votes:
            decision.is_success = False
            decision.winning_answer = "多模型未能在截止时间内提供有效答案"
            decision.total_duration_ms = (time.monotonic() - start_ts) * 1000.0
            return decision
        else:
            # 截止后按得票汇总统计
            counts = Counter(valid_votes)
            highest_votes = max(counts.values())
            top_candidates = [v for v, c in counts.items() if c == highest_votes]

            if len(top_candidates) == 1:
                winning_vote = top_candidates[0]
                decision.is_success = True
            else:
                # 出现平票
                logger.warning(
                    "consensus: 存在 %d 个选项平票 (均得 %d 票)。",
                    len(top_candidates),
                    highest_votes,
                )
                if tie_breaker == "skip":
                    logger.warning("consensus: 平票策略为 skip，本次作答标记冲突跳过。")
                    decision.is_success = False
                    decision.winning_answer = "多AI投票平票且配置为跳过"
                    decision.total_duration_ms = (time.monotonic() - start_ts) * 1000.0
                    return decision
                else:
                    # priority 策略：按配置端点优先级确定性仲裁（第一个投出该票的模型优先），绝不用 random.choice
                    winning_vote = None
                    for ep in endpoints:
                        ep_vote = model_vote_map.get(ep.name)
                        if ep_vote and ep_vote in top_candidates:
                            winning_vote = ep_vote
                            logger.info(
                                "consensus: 平票按端点优先级裁决，采纳高优先级模型 [%s] 的答案。",
                                ep.name,
                            )
                            break
                    if winning_vote is None:
                        winning_vote = top_candidates[0]
                    decision.is_success = True

        decision.vote = winning_vote
        decision.winning_answer = vote_to_answer(winning_vote)
        # 寻找匹配该赢家答案的胜出模型名称
        for ep in endpoints:
            if model_vote_map.get(ep.name) == winning_vote:
                decision.winning_model = ep.name
                break

        decision.total_duration_ms = (time.monotonic() - start_ts) * 1000.0
        return decision
