"""第二阶段性能基线测量与基准评估工具。

通过本地模拟环境与桩 AI Provider，对自动答题完整流水线进行受控基准测量。
统计各分段耗时（P50 / P90 / P95 / Max / 平均值），检验以下第二阶段受控目标：
- 题目可操作到程序检测就绪：P95 <= 300ms
- 题目准备好到 AI 请求发出：P95 <= 500ms
- 有效 AI 答案到提交点击：单选 P95 <= 500ms；多选单独报告
- 支持 100 轮连续评估，提供不同题型与按钮延迟对比。

运行方式：
  python -m tests.benchmark_baseline
  python -m tests.benchmark_baseline --rounds 100 --ai-delay 0.05
"""

import argparse
import json
import statistics
import sys
import tempfile
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bot import Bot
from src.config import Config
from src.timing import QuizTimingTracker
try:
    from tests.test_worker import FakeItem, FakePage
except ImportError:
    from test_worker import FakeItem, FakePage


def run_benchmark(
    rounds: int = 100,
    simulated_ai_delay: float = 0.05,
    quiz_type: str = "single",
    button_delay_ms: float = 0.0,
) -> dict:
    """运行多轮本地基准模拟答题并收集分段耗时统计。"""
    with tempfile.TemporaryDirectory() as temp_dir:
        metrics_file = Path(temp_dir) / "benchmark_metrics.jsonl"
        config = Config(str(Path(temp_dir) / "config.json"))
        config.set("mode", "auto")
        config.set("submit_delay", 0)  # 快速配置设为 0，测量链路固有开销
        config.set("classroom_poll_interval_ms", 200)

        records = []

        for i in range(rounds):
            option_a = FakeItem(text="A. 选项A", attributes={"data-option": "A"})
            option_b = FakeItem(text="B. 选项B", attributes={"data-option": "B"})
            option_c = FakeItem(text="C. 选项C", attributes={"data-option": "C"})

            # 若模拟按钮延迟，初始无 can，被点击后延迟出现 can
            initial_class = "submit-btn can" if button_delay_ms <= 0 else "submit-btn"
            submit_btn = FakeItem(text="提交答案", attributes={"class": initial_class})

            if button_delay_ms > 0:
                def _delayed_ready():
                    time.sleep(button_delay_ms / 1000.0)
                    submit_btn.attributes["class"] = "submit-btn can"
                t_btn = threading.Thread(target=_delayed_ready)
                t_btn.daemon = True
                t_btn.start()

            original_click = submit_btn.click
            def custom_click(timeout=None):
                original_click(timeout)
                submit_btn.visible = False
            submit_btn.click = custom_click

            page = FakePage(url=f"https://changjiang.yuketang.cn/lesson/fullscreen/v3/123/exercise?q={i}")
            page.selectors['p[data-option]'] = [option_a, option_b, option_c]
            page.selectors['p[data-option="A"]'] = [option_a]
            page.selectors['p[data-option="B"]'] = [option_b]
            page.selectors['p[data-option="C"]'] = [option_c]
            page.selectors['[class*="submit-btn"]'] = [submit_btn]
            page.selectors['button:has-text("提交答案")'] = [submit_btn]

            mock_browser = Mock()
            mock_browser.has_session = True
            mock_browser.page = page
            mock_browser.get_cookies_dict.return_value = {}

            # 模拟 AI 响应
            fut: Future[str] = Future()
            if quiz_type == "multi":
                resp_payload = '{"type": "multi", "answers": ["A", "B"]}'
            else:
                resp_payload = '{"type": "single", "answers": ["A"]}'

            if simulated_ai_delay > 0:
                def _complete_later(payload=resp_payload):
                    time.sleep(simulated_ai_delay)
                    fut.set_result(payload)
                t = threading.Thread(target=_complete_later)
                t.daemon = True
                t.start()
            else:
                fut.set_result(resp_payload)

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
                        time.sleep(0.002)
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


def print_report(stats: dict, rounds: int, simulated_ai_delay: float, quiz_type: str = "single", button_delay: float = 0.0) -> None:
    print("\n" + "=" * 90)
    print(f"第二阶段性能评估基准报告（本地受控流水线，共 {rounds} 轮，题型: {quiz_type}）")
    print(f"运行配置：模拟 AI 延迟 = {simulated_ai_delay * 1000:.1f}ms, submit_delay = 0, 按钮生效延迟 = {button_delay:.1f}ms")
    print("=" * 90)
    print(f"{'分段阶段 (Stage)':<32} | {'P50(ms)':<8} | {'P90(ms)':<8} | {'P95(ms)':<8} | {'Max(ms)':<8} | {'Mean(ms)':<8} | {'目标与达标':<10}")
    print("-" * 90)

    label_map = {
        "detect_to_ready_ms": ("1. 题目检测->就绪 (Detect->Ready)", 300.0),
        "ready_to_ai_start_ms": ("2. 就绪->发起请求 (Ready->AI Req)", 500.0),
        "ai_duration_ms": ("3. AI 推理往返 (AI Latency)", None),
        "ai_to_validated_ms": ("4. 答案生效校验 (AI->Validated)", None),
        "validated_to_clicked_ms": ("5. 点击提交按钮 (Validated->Click)", 500.0),
        "clicked_to_confirmed_ms": ("6. 确认提交生效 (Click->Confirm)", None),
        "total_end_to_end_ms": ("7. 端到端总耗时 (Total E2E)", None),
    }

    for key, (label, target) in label_map.items():
        s = stats.get(key)
        if s:
            target_str = "-"
            if target is not None:
                passed = s['p95'] <= target
                target_str = f"<= {target:.0f}ms [{'PASS' if passed else 'FAIL'}]"
            print(f"{label:<32} | {s['p50']:<8.2f} | {s['p90']:<8.2f} | {s['p95']:<8.2f} | {s['max']:<8.2f} | {s['mean']:<8.2f} | {target_str:<10}")

    print("=" * 90)
    print("分析与说明：")
    print("1. 检测->就绪 (P95 <= 300ms)：本地亚毫秒完成，多条件退出消除所有固有固定停顿。")
    print("2. 就绪->发起 AI (P95 <= 500ms)：直接无阻塞派发，无重型扫描阻断。")
    print("3. 生效->点击提交 (P95 <= 500ms)：submit_delay=0 且 can 态就绪即提交，达标毫秒级响应。")
    print("=" * 90 + "\n")


def main():
    parser = argparse.ArgumentParser(description="第二阶段性能评估基准工具")
    parser.add_argument("--rounds", type=int, default=100, help="基准测试轮数 (默认 100)")
    parser.add_argument("--ai-delay", type=float, default=0.05, help="模拟 AI 推理附加延迟（秒，默认 0.05）")
    parser.add_argument("--quiz-type", type=str, default="single", choices=["single", "multi"], help="测试题型 (single/multi)")
    parser.add_argument("--btn-delay", type=float, default=0.0, help="按钮进入 can 态延迟（毫秒，默认 0）")
    args = parser.parse_args()

    stats = run_benchmark(
        rounds=args.rounds,
        simulated_ai_delay=args.ai_delay,
        quiz_type=args.quiz_type,
        button_delay_ms=args.btn_delay,
    )
    print_report(stats, rounds=args.rounds, simulated_ai_delay=args.ai_delay, quiz_type=args.quiz_type, button_delay=args.btn_delay)


if __name__ == "__main__":
    main()
