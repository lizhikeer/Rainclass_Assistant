"""基于真实 Playwright Chromium 的性能基准评测工具。

在真实无头 Chromium 浏览器环境下加载本地 exercise_single.html 习题测试桩，
通过模拟真实 DOM 交互（页面导航、题图截图、选项点击、提交按钮消失），
测量并统计自动答题各分段耗时（P50 / P90 / P95 / P99 / Max / Min / 平均值）：
1. 题目检测与就绪 (detect_to_ready_ms)
2. 截图与题图准备 (ready_to_ai_start_ms)
3. AI 等待响应 (ai_duration_ms)
4. 选项点击与按钮就绪 (ai_to_validated_ms)
5. 提交点击与确认完成 (clicked_to_confirmed_ms)
6. 全链路端到端耗时 (total_end_to_end_ms)

运行方式：
  python -m tests.benchmark_real_browser --rounds 100 --ai-delay 0.05
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
from typing import Any, Optional
from unittest.mock import Mock

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bot import Bot
from src.config import Config


FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "exercise_single.html"


def calculate_stats(values: list[float]) -> dict[str, float]:
    """计算分位数与统计值。"""
    if not values:
        return {"count": 0, "mean": 0.0, "p50": 0.0, "p90": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "min": 0.0}
    sorted_vals = sorted(values)
    n = len(sorted_vals)

    def percentile(p: float) -> float:
        k = (n - 1) * p
        f = int(k)
        c = f + 1
        if c < n:
            return sorted_vals[f] + (k - f) * (sorted_vals[c] - sorted_vals[f])
        return sorted_vals[f]

    return {
        "count": n,
        "mean": round(statistics.mean(sorted_vals), 2),
        "p50": round(percentile(0.50), 2),
        "p90": round(percentile(0.90), 2),
        "p95": round(percentile(0.95), 2),
        "p99": round(percentile(0.99), 2),
        "max": round(max(sorted_vals), 2),
        "min": round(min(sorted_vals), 2),
    }


def run_real_browser_benchmark(
    rounds: int = 100,
    simulated_ai_delay: float = 0.05,
    headless: bool = True,
) -> dict[str, Any]:
    """在真实 Chromium 中执行指定轮数的答题流水线基准测试。"""
    if not FIXTURE_PATH.exists():
        raise FileNotFoundError(f"Fixture not found: {FIXTURE_PATH}")

    fixture_html = FIXTURE_PATH.read_text(encoding="utf-8")
    # 注入交互行为：点击提交按钮后移除该按钮以模拟提交完成
    injected_html = fixture_html.replace(
        "</body>",
        """<script>
        document.addEventListener('click', function(e) {
            var btn = e.target.closest('.btn-submit, [class*="submit-btn"]');
            if (btn) {
                btn.remove();
            }
        });
        </script></body>""",
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        metrics_file = temp_path / "real_browser_metrics.jsonl"
        config = Config(str(temp_path / "config.json"))
        config.set("mode", "auto")
        config.set("submit_delay", 0)  # 设为 0，测量链路固有开销
        config.set("classroom_poll_interval_ms", 200)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=headless)
            context = browser.new_context()

            # 拦截长江雨课堂习题页面路由，返回测试 HTML
            context.route(
                "https://changjiang.yuketang.cn/**",
                lambda route: route.fulfill(
                    status=200,
                    content_type="text/html",
                    body=injected_html,
                ),
            )

            page = context.new_page()

            mock_browser_mgr = Mock()
            mock_browser_mgr.page = page
            mock_browser_mgr.pages = [page]
            mock_browser_mgr.has_session = True
            mock_browser_mgr.is_logged_in.return_value = True

            records: list[dict[str, Any]] = []

            for i in range(rounds):
                # 重新导航到习题页面，生成带唯一 query 的 URL
                exercise_url = f"https://changjiang.yuketang.cn/lesson/fullscreen/v3/123/exercise?round={i}"
                page.goto(exercise_url, wait_until="domcontentloaded")

                # 创建受控 AI 桩，按设定延时返回正确选项
                ai_mock = Mock()
                def make_submit_answer():
                    fut: Future[str] = Future()
                    def _delayed_answer():
                        if simulated_ai_delay > 0:
                            time.sleep(simulated_ai_delay)
                        fut.set_result('{"type": "single", "answers": "A"}')
                    t = threading.Thread(target=_delayed_answer)
                    t.daemon = True
                    t.start()
                    return fut

                ai_mock.submit_answer.side_effect = lambda **kwargs: make_submit_answer()
                ai_mock.last_decision = None

                bot = Bot(
                    config=config,
                    browser=mock_browser_mgr,
                    ai_service=ai_mock,
                    notification=Mock(),
                    stop_event=threading.Event(),
                    metrics_file=metrics_file,
                    mode="auto",
                )

                # 模拟课堂轮询触发答题
                bot._handle_quiz(page)

                # 等待 AI 完成并在主线程执行选项点击与提交
                deadline = time.monotonic() + 10.0
                while not bot._answer_future.done() and time.monotonic() < deadline:
                    page.wait_for_timeout(10)

                bot._complete_pending_answer(page)

            browser.close()

        # 读取落盘 metrics
        if metrics_file.exists():
            with open(metrics_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        records.append(json.loads(line))

    # 提取分段耗时并计算统计指标
    durations: dict[str, list[float]] = {
        "detect_to_ready_ms": [],
        "ready_to_ai_start_ms": [],
        "ai_duration_ms": [],
        "ai_to_validated_ms": [],
        "clicked_to_confirmed_ms": [],
        "total_end_to_end_ms": [],
    }

    success_count = 0
    for r in records:
        if r.get("success"):
            success_count += 1
        d = r.get("durations_ms", {})
        for key in durations:
            if key in d and d[key] is not None:
                durations[key].append(d[key])

    report = {
        "rounds": rounds,
        "success_count": success_count,
        "success_rate": round(success_count / rounds * 100, 2) if rounds else 0.0,
        "simulated_ai_delay_ms": round(simulated_ai_delay * 1000, 2),
        "stages": {k: calculate_stats(v) for k, v in durations.items()},
    }
    return report


def print_markdown_report(report: dict[str, Any]) -> None:
    """输出美观的 Markdown 性能评估报告。"""
    stages = report.get("stages", {})
    rounds = report.get("rounds", 0)
    success_rate = report.get("success_rate", 0.0)
    ai_delay = report.get("simulated_ai_delay_ms", 0.0)

    stage_names = {
        "detect_to_ready_ms": "题目检测与就绪 (detect -> ready)",
        "ready_to_ai_start_ms": "截图/题图准备 (ready -> ai_start)",
        "ai_duration_ms": f"AI 模型推理 (ai_start -> ai_resp, 桩延时 {ai_delay:.0f}ms)",
        "ai_to_validated_ms": "选项匹配与点击 (ai_resp -> validated)",
        "clicked_to_confirmed_ms": "提交点击与确认 (clicked -> confirmed)",
        "total_end_to_end_ms": "全流程端到端 (total end-to-end)",
    }

    print("\n" + "=" * 80)
    print("真实 Playwright Chromium 性能基准测试报告 (Real Browser Benchmark)")
    print("=" * 80)
    print(f"评估轮数: {rounds} 轮 | 成功率: {success_rate}% | 模拟 AI 延时: {ai_delay:.1f}ms\n")

    header = "| 阶段名称 | 样本数 | 平均耗时 (ms) | P50 (ms) | P90 (ms) | P95 (ms) | P99 (ms) | Max (ms) |"
    divider = "| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |"
    print(header)
    print(divider)

    for k, name in stage_names.items():
        s = stages.get(k, {})
        if not s or s.get("count", 0) == 0:
            continue
        print(f"| {name} | {s['count']} | {s['mean']:.1f} | {s['p50']:.1f} | {s['p90']:.1f} | {s['p95']:.1f} | {s['p99']:.1f} | {s['max']:.1f} |")

    print("\n" + "=" * 80)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Real Browser Benchmark for Rainclass Assistant")
    parser.add_argument("--rounds", type=int, default=100, help="Number of benchmark rounds (default: 100)")
    parser.add_argument("--ai-delay", type=float, default=0.05, help="Simulated AI delay in seconds (default: 0.05)")
    parser.add_argument("--no-headless", action="store_true", help="Run browser in non-headless mode")

    args = parser.parse_args()
    report_data = run_real_browser_benchmark(
        rounds=args.rounds,
        simulated_ai_delay=args.ai_delay,
        headless=not args.no_headless,
    )
    print_markdown_report(report_data)
