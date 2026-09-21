"""AI 模型端点基准评测 CLI 工具 (provider-benchmark)。

由用户主动运行，使用合成测试图片对配置的端点进行受限样本评测，
输出 P50/P95 耗时、格式有效率及 Token 消耗统计，绝不使用真实敏感课堂图片。
"""

import argparse
import base64
import configparser
import json
import logging
import math
import sys
import time
from pathlib import Path
from typing import Any, Optional

from src.ai.models import EndpointConfig, ModelCallResult, is_submittable_vote
from src.ai.strategy import StrategyRunner
from src.config import Config

# 预置 1x1 纯白合成 PNG 图片的 Base64（避免外部依赖与真实课堂图片泄露）
SYNTHETIC_TEST_CARD_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
)

BENCHMARK_PROMPT = (
    "请分析这张图片中的测试图案，返回题目类型和正确答案json，格式为："
    '{"type":"single","answers":"A"}'
)


def compute_percentile(data: list[float], percentile: float) -> float:
    """计算分位数 (0~100)。"""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    if len(sorted_data) == 1:
        return sorted_data[0]
    index = (percentile / 100.0) * (len(sorted_data) - 1)
    lower = int(math.floor(index))
    upper = int(math.ceil(index))
    if lower == upper:
        return sorted_data[lower]
    fraction = index - lower
    return sorted_data[lower] + (sorted_data[upper] - sorted_data[lower]) * fraction


def run_benchmark(
    endpoints: list[EndpointConfig],
    samples: int = 3,
    timeout: float = 15.0,
) -> list[dict[str, Any]]:
    """在指定端点上运行基准评测，返回结构化统计列表。"""
    samples = max(1, min(10, samples))  # 严格限制样本数，避免过度产生 API 账单
    runner = StrategyRunner(max_workers=min(4, len(endpoints) or 1))
    reports: list[dict[str, Any]] = []

    print(f"\n==================== 开始 AI 端点基准评测 ====================")
    print(f"评测模型数: {len(endpoints)} | 每模型样本数: {samples} | 单次超时: {timeout}s")
    print(f"测试素材: 合成测试卡 (非课堂敏感数据)")
    print(f"--------------------------------------------------------------")

    try:
        for ep in endpoints:
            print(f"\n正在评测端点: [{ep.name}] ({ep.model}) ...")
            latencies: list[float] = []
            valid_count = 0
            total_prompt_tokens = 0
            total_completion_tokens = 0
            has_token_usage = False

            for i in range(samples):
                sys.stdout.write(f"  样本 {i + 1}/{samples}: ")
                sys.stdout.flush()

                res: ModelCallResult = runner.call_endpoint(
                    endpoint=ep,
                    image_b64=SYNTHETIC_TEST_CARD_PNG_B64,
                    prompt=BENCHMARK_PROMPT,
                    timeout=timeout,
                    mime_type="image/png",
                )

                latencies.append(res.latency_ms)
                if res.is_submittable:
                    valid_count += 1
                    status = "有效答案"
                elif res.is_valid:
                    status = f"非可提交({res.vote[0] if res.vote else 'unknown'})"
                elif res.error:
                    status = f"失败({res.error[:30]})"
                else:
                    status = "格式非法"

                usage_info = ""
                if res.usage:
                    has_token_usage = True
                    p_tok = res.usage.get("prompt_tokens", 0)
                    c_tok = res.usage.get("completion_tokens", 0)
                    total_prompt_tokens += p_tok
                    total_completion_tokens += c_tok
                    usage_info = f", tokens={p_tok}+{c_tok}"

                print(f"{res.latency_ms:.1f}ms - {status}{usage_info}")
                time.sleep(0.1)  # 避免触发硬并发限流

            p50 = compute_percentile(latencies, 50)
            p95 = compute_percentile(latencies, 95)
            valid_rate = (valid_count / samples) * 100.0

            rep = {
                "name": ep.name,
                "model": ep.model,
                "samples": samples,
                "valid_count": valid_count,
                "valid_rate": round(valid_rate, 1),
                "p50_ms": round(p50, 1),
                "p95_ms": round(p95, 1),
                "avg_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
                "token_usage": (
                    f"in={total_prompt_tokens}, out={total_completion_tokens}"
                    if has_token_usage
                    else "unknown"
                ),
            }
            reports.append(rep)

    finally:
        runner.shutdown()

    # 打印最终对比表格
    print(f"\n==================== 评测结果报告 ====================")
    header = f"{'端点名称':<12} | {'模型标识':<22} | {'有效率':<7} | {'P50(ms)':<8} | {'P95(ms)':<8} | {'Token使用'}"
    print(header)
    print("-" * len(header))
    for r in reports:
        print(
            f"{r['name']:<12} | {r['model']:<22} | {r['valid_rate']:>5.1f}% | "
            f"{r['p50_ms']:>8.1f} | {r['p95_ms']:>8.1f} | {r['token_usage']}"
        )
    print(f"======================================================\n")

    return reports


def load_benchmark_endpoints(
    config_path: str = "config.json",
    ini_path: str = "model_visible.ini",
) -> list[EndpointConfig]:
    """从配置或 INI 文件加载待评测端点列表。"""
    endpoints: list[EndpointConfig] = []

    # 1. 尝试从 model_visible.ini 读取多端点
    p_ini = Path(ini_path)
    if p_ini.exists():
        parser = configparser.ConfigParser(interpolation=None)
        try:
            with open(p_ini, "r", encoding="utf-8-sig") as f:
                parser.read_file(f)
            for sec in parser.sections():
                base_url = parser.get(sec, "base_url", fallback="").strip()
                key = parser.get(sec, "key", fallback="").strip()
                model = parser.get(sec, "model", fallback="").strip()
                if base_url and key and model:
                    endpoints.append(
                        EndpointConfig(
                            name=sec,
                            base_url=base_url,
                            api_key=key,
                            model=model,
                            description=parser.get(sec, "description", fallback="").strip(),
                        )
                    )
        except Exception as e:
            logger.warning("读取 INI 文件失败：%s", e)

    # 2. 如果 INI 没有有效端点，读取 config.json 中的单模型配置
    if not endpoints and Path(config_path).exists():
        cfg = Config(config_path)
        d_key = cfg.get("doubao_api_key", "")
        if d_key:
            endpoints.append(
                EndpointConfig(
                    name="豆包AI",
                    base_url="https://ark.cn-beijing.volces.com/api/v3",
                    api_key=d_key,
                    model="doubao-seed-1-6-250615",
                    extra_body={"enable_thinking": False},
                )
            )
        g_key = cfg.get("gemini_api_key", "")
        if g_key:
            endpoints.append(
                EndpointConfig(
                    name="Gemini AI",
                    base_url="https://generativelanguage.googleapis.com/v1beta",
                    api_key=g_key,
                    model="gemini-2.5-flash",
                    provider_type="gemini",
                )
            )
        c_url = cfg.get("custom_ai_base_url", "")
        c_key = cfg.get("custom_ai_api_key", "")
        c_mod = cfg.get("custom_ai_model", "")
        if c_url and c_key and c_mod:
            endpoints.append(
                EndpointConfig(
                    name="自定义",
                    base_url=c_url,
                    api_key=c_key,
                    model=c_mod,
                    extra_body={"enable_thinking": False},
                )
            )

    return endpoints


def main() -> None:
    """CLI 入口点。"""
    parser = argparse.ArgumentParser(description="雨课堂 AI Provider 性能与有效率基准评测")
    parser.add_argument("--config", default="config.json", help="config.json 配置文件路径")
    parser.add_argument("--models-ini", default="model_visible.ini", help="多模型 INI 路径")
    parser.add_argument("--samples", type=int, default=3, help="每个端点测试样本数 (1~10)")
    parser.add_argument("--timeout", type=float, default=15.0, help="单次请求超时秒数")

    args = parser.parse_args()
    endpoints = load_benchmark_endpoints(config_path=args.config, ini_path=args.models_ini)

    if not endpoints:
        print("未找到任何已配置 API Key 或有效 base_url 的 AI 端点。请检查 config.json 或 model_visible.ini。")
        sys.exit(1)

    run_benchmark(endpoints, samples=args.samples, timeout=args.timeout)


if __name__ == "__main__":
    main()
