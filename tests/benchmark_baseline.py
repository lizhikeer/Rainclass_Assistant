"""第一阶段基线耗时测量与基准评估工具。

通过本地模拟环境与桩 AI Provider，对自动答题完整流水线进行受控基线测量。
统计各分段耗时（P50 / P90 / P95 / Max / 平均值），为后续阶段（如第二阶段消除轮询等待）
提供严格的性能对照基线。

运行方式：
  python -m tests.benchmark_baseline
  python -m tests.benchmark_baseline --rounds 100 --ai-delay 0.2
"""

import argparse
import json
import statistics
import tempfile
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import Mock, patch

from src.bot import Bot
from src.config import Config
from src.timing import QuizTimingTracker
from tests.test_worker import FakeItem, FakePage


def run_benchmark(rounds: int = 50, simulated_ai_delay: float = 0.0) -> dict:
    """运行多轮本地基准模拟答题并收集分段耗时统计。"""
    with tempfile.TemporaryDirectory() as temp_dir:
        metrics_file = Path(temp_dir) / "benchmark_metrics.jsonl"
        config = Config(str(Path(temp_dir) / "config.json"))
        config.set("mode", "auto")
        config.set("submit_delay", 0)  # 基线测试设为 0，测量纯链路耗时

        records = []

        for i in range(rounds):
            option_a = FakeItem(text="A. 选项A", attributes={"data-option": "A"})
            submit_btn = FakeItem(text="提交答案", attributes={"class": "submit-btn can"})

            original_click = submit_btn.click
            def custom_click(timeout=None):
                original_click(timeout)
                submit_btn.visible = False
            submit_btn.click = custom_click

            page = FakePage(url=f"https://changjiang.yuketang.cn/lesson/fullscreen/v3/123/exercise?q={i}")
            page.selectors['p[data-option]'] = [option_a]
            page.selectors['p[data-option="A"]'] = [option_a]
            page.selectors['[class*="submit-btn"]'] = [submit_btn]
            page.selectors['button:has-text("提交答案")'] = [submit_btn]

            mock_browser = Mock()
            mock_browser.has_session = True
            mock_browser.page = page
            mock_browser.get_cookies_dict.return_value = {}

            # 模拟 AI 响应
            fut: Future[str] = Future()
            if simulated_ai_delay > 0:
                def _complete_later():
                    time.sleep(simulated_ai_delay)
                    fut.set_result('{"type": "single", "answers": ["A"]}')
                t = threading.Thread(target=_complete_later)
                t.daemon = True
                t.start()
            else:
                fut.set_result('{"type": "single", "answers": ["A"]}')

            mock_ai = Mock()
            mock_ai.submit_answer.return_value = fut
            mock_notify = Mock()
            stop_event = threading.Event()

            bot = Bot(
                config=config,
                browser=mock_browser,
                ai_service=mock_ai,
                notification=mock_notify,
                stop_event=stop_event,
                mode="auto",
                metrics_file=metrics_file,
            )

            with patch.object(bot, "_capture_question_image", return_value=None):
                bot._answer(page)
                if simulated_ai_delay > 0:
                    while not fut.done():
                        time.sleep(0.005)
                    bot._answer(page)

        # 读取落盘的所有指标记录
        with open(metrics_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))

    # 计算各指标分布
    def _percentile(data: list[float], pct: float) -> float:
        if not data:
            return 0.0
        sorted_data = sorted(data)
        k = (len(sorted_data) - 1) * (pct / 100.0)
        f = int(k)
        c = min(f + 1, len(sorted_data) - 1)
        d0 = sorted_data[f] * (c - k)
        d1 = sorted_data[c] * (k - f)
        return round(d0 + d1, 2)

    keys = [
        "detect_to_ready_ms",
        "ready_to_ai_start_ms",
        "ai_duration_ms",
        "ai_to_validated_ms",
        "validated_to_clicked_ms",
        "clicked_to_confirmed_ms",
        "total_end_to_end_ms",
    ]

    stats = {}
    for key in keys:
        values = [r["durations_ms"][key] for r in records if key in r["durations_ms"]]
        if values:
            stats[key] = {
                "count": len(values),
                "mean": round(statistics.mean(values), 2),
                "min": round(min(values), 2),
                "p50": _percentile(values, 50),
                "p90": _percentile(values, 90),
                "p95": _percentile(values, 95),
                "max": round(max(values), 2),
            }

    return stats


def print_report(stats: dict, rounds: int, simulated_ai_delay: float) -> None:
    print("\n" + "=" * 80)
    print(f"第一阶段性能基线测量报告（本地模拟流水线，共 {rounds} 轮）")
    print(f"运行条件：模拟 AI 附加延迟 = {simulated_ai_delay * 1000:.1f}ms, submit_delay = 0")
    print("=" * 80)
    print(f"{'分段阶段 (Stage)':<26} | {'P50 (ms)':<9} | {'P90 (ms)':<9} | {'P95 (ms)':<9} | {'Max (ms)':<9} | {'Mean (ms)':<9}")
    print("-" * 80)

    label_map = {
        "detect_to_ready_ms": "1. 题目检测->就绪 (Detect->Ready)",
        "ready_to_ai_start_ms": "2. 就绪->发起请求 (Ready->AI Req)",
        "ai_duration_ms": "3. AI 推理往返 (AI Latency)",
        "ai_to_validated_ms": "4. 答案生效校验 (AI->Validated)",
        "validated_to_clicked_ms": "5. 点击提交按钮 (Validated->Click)",
        "clicked_to_confirmed_ms": "6. 确认提交生效 (Click->Confirm)",
        "total_end_to_end_ms": "7. 端到端总耗时 (Total E2E)",
    }

    for key, label in label_map.items():
        s = stats.get(key)
        if s:
            print(f"{label:<26} | {s['p50']:<9.2f} | {s['p90']:<9.2f} | {s['p95']:<9.2f} | {s['max']:<9.2f} | {s['mean']:<9.2f}")

    print("=" * 80)
    print("说明：")
    print("- 本测试为本地受控基准（受控 DOM 桩 + 模拟 Provider），排除了不可控的云端网络波动。")
    print("- 真实课堂耗时将额外叠加：雨课堂 WebSocket/轮询推送延迟、网络传输延迟以及远程 AI 视觉模型推理耗时。")
    print("=" * 80 + "\n")


def main():
    parser = argparse.ArgumentParser(description="第一阶段基线耗时评估工具")
    parser.add_argument("--rounds", type=int, default=50, help="基准测试轮数 (默认 50)")
    parser.add_argument("--ai-delay", type=float, default=0.0, help="模拟 AI 推理附加延迟（秒，默认 0.0）")
    args = parser.parse_args()

    stats = run_benchmark(rounds=args.rounds, simulated_ai_delay=args.ai_delay)
    print_report(stats, rounds=args.rounds, simulated_ai_delay=args.ai_delay)


if __name__ == "__main__":
    main()
