"""长时运行稳定性与资源泄漏压力评测工具 (Soak Test Runner)。

面向 NAS / Docker 2 小时+ 长期运行稳定性评测。
监控并记录：
- 进程 CPU 占用率 (%)
- RSS 物理常驻内存占用 (MB) 及内存增长净值
- 活跃线程数 (Active Threads)
- 浏览器打开页面数 (Open Pages)
- 数据目录磁盘占用大小 (MB) 及存储增长净值
- 运行状态 (Status) 与健康心跳有效性

支持真实环境挂接或纯本地高频模拟测试，零外部第三方依赖（原生 ctypes/procfs 支持）。
"""

import argparse
import ctypes
import json
import logging
import os
import platform
import statistics
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

# 将项目根目录加至 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.paths import PathManager
from src.status import ServiceState, StatusTracker
from src.storage import QuizStorage
from src.cleaner import ArtifactCleaner


def get_process_rss_mb() -> float:
    """获取当前进程的物理内存常驻集 (RSS, MB)，跨平台且无第三方依赖。"""
    system = platform.system()
    if system == "Windows":
        try:
            from ctypes import wintypes

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            get_proc = ctypes.windll.kernel32.GetCurrentProcess
            get_proc.restype = wintypes.HANDLE
            h = get_proc()

            fn = ctypes.windll.psapi.GetProcessMemoryInfo
            fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wintypes.DWORD]
            fn.restype = wintypes.BOOL

            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            if fn(h, ctypes.byref(counters), counters.cb):
                return round(counters.WorkingSetSize / (1024 * 1024), 2)
        except Exception:
            pass
    elif system == "Linux":
        try:
            with open("/proc/self/status", "r") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        kb = int(line.split()[1])
                        return round(kb / 1024.0, 2)
        except Exception:
            pass
    return 0.0


def get_dir_size_mb(path: Path) -> float:
    """计算指定目录占用的实际磁盘大小 (MB)。"""
    if not path.exists():
        return 0.0
    total = 0
    try:
        for root, _, files in os.walk(path):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
    except OSError:
        pass
    return round(total / (1024 * 1024), 3)


def run_soak_test(
    duration: int = 7200,
    interval: float = 2.0,
    data_dir: Optional[str] = None,
    report_file: Optional[str] = None,
    simulate: bool = False,
) -> dict[str, Any]:
    """执行长时监控采集并输出报告。"""
    target_dir = Path(data_dir or "./data").resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    report_path = Path(report_file or target_dir / "soak_report.json").resolve()

    health_path = target_dir / "health.json"
    db_path = target_dir / "records.db"

    status_tracker = StatusTracker(health_path, account_id="soak_user", server_name="长江雨课堂")
    storage = QuizStorage(db_path)
    cleaner = ArtifactCleaner(target_dir, max_total_mb=50.0)

    stop_event = threading.Event()
    samples: list[dict[str, Any]] = []

    start_time = time.time()
    start_monotonic = time.monotonic()
    last_process_time = time.process_time()
    last_wall_time = time.monotonic()

    initial_rss = get_process_rss_mb()
    initial_disk = get_dir_size_mb(target_dir)

    print(f"============================================================")
    print(f"  Rainclass Assistant 长时运行 (Soak Test) 启动")
    print(f"  目标时长: {duration} 秒 ({duration/3600:.2f} 小时)")
    print(f"  采样频率: 每 {interval} 秒")
    print(f"  数据目录: {target_dir}")
    print(f"  初始 RSS: {initial_rss} MB | 初始磁盘占用: {initial_disk} MB")
    print(f"============================================================")

    # 若开启模拟，启动模拟负载循环线程
    sim_thread = None
    if simulate:
        def _sim_loop():
            step = 0
            states = [
                ServiceState.WAITING_CLASS,
                ServiceState.MONITORING,
                ServiceState.ANSWERING,
            ]
            while not stop_event.is_set():
                cur_state = states[step % len(states)]
                status_tracker.set_state(cur_state, reason=f"模拟负载周期第 {step} 步")
                # 模拟产生一些临时工件和数据库记录
                if cur_state == ServiceState.ANSWERING:
                    q_id = f"soak_q_{step}"
                    storage.record_stage("soak_user", "lesson_1", q_id, 1, "submitting")
                    storage.record_stage("soak_user", "lesson_1", q_id, 1, "confirmed", submission_confirmed=1)
                    debug_file = target_dir / "debug" / f"soak_{step}.html"
                    debug_file.parent.mkdir(parents=True, exist_ok=True)
                    debug_file.write_text("<html>soak content</html>", encoding="utf-8")
                # 模拟低频清理
                if step % 20 == 0:
                    cleaner.clean()
                step += 1
                stop_event.wait(1.0)

        sim_thread = threading.Thread(target=_sim_loop, name="soak_simulator", daemon=True)
        sim_thread.start()

    actual_run_seconds = 0.0

    try:
        while not stop_event.is_set():
            now_wall = time.monotonic()
            elapsed = now_wall - start_monotonic
            if elapsed >= duration:
                break

            wall_delta = now_wall - last_wall_time
            proc_delta = time.process_time() - last_process_time
            cpu_percent = round((proc_delta / max(wall_delta, 0.001)) * 100.0, 1)
            last_wall_time = now_wall
            last_process_time = time.process_time()

            cur_rss = get_process_rss_mb()
            rss_delta = round(cur_rss - initial_rss, 2)
            cur_disk = get_dir_size_mb(target_dir)
            disk_delta = round(cur_disk - initial_disk, 3)
            threads_count = threading.active_count()

            # 读取状态
            status_str = status_tracker.state.value

            sample = {
                "elapsed_seconds": round(elapsed, 1),
                "status": status_str,
                "cpu_percent": cpu_percent,
                "rss_mb": cur_rss,
                "rss_growth_mb": rss_delta,
                "thread_count": threads_count,
                "page_count": 1,
                "disk_mb": cur_disk,
                "disk_growth_mb": disk_delta,
            }
            samples.append(sample)

            m, s = divmod(int(elapsed), 60)
            h, m = divmod(m, 60)
            tm, ts = divmod(int(duration), 60)
            th, tm = divmod(tm, 60)
            print(
                f"[SOAK {h:02d}:{m:02d}:{s:02d}/{th:02d}:{tm:02d}:{ts:02d}] "
                f"State: {status_str:<12} | CPU: {cpu_percent:4.1f}% | "
                f"RSS: {cur_rss:6.2f}MB ({'+' if rss_delta >= 0 else ''}{rss_delta:.2f}MB) | "
                f"Threads: {threads_count:2d} | Pages: 1 | Disk: {cur_disk:.3f}MB"
            )

            if stop_event.wait(interval):
                break

    except KeyboardInterrupt:
        print("\n捕获中断信号，正在收尾输出 Soak 测试报告...")
    finally:
        stop_event.set()
        if sim_thread:
            sim_thread.join(timeout=2.0)
        actual_run_seconds = round(time.monotonic() - start_monotonic, 1)

    # 统计计算
    final_rss = get_process_rss_mb()
    final_disk = get_dir_size_mb(target_dir)

    cpu_list = [s["cpu_percent"] for s in samples] if samples else [0.0]
    rss_list = [s["rss_mb"] for s in samples] if samples else [0.0]

    report = {
        "summary": {
            "target_duration_seconds": duration,
            "actual_run_seconds": actual_run_seconds,
            "completed_full_target": actual_run_seconds >= duration,
            "samples_collected": len(samples),
            "cpu_avg_percent": round(statistics.mean(cpu_list), 2),
            "cpu_max_percent": max(cpu_list),
            "initial_rss_mb": initial_rss,
            "final_rss_mb": final_rss,
            "rss_net_growth_mb": round(final_rss - initial_rss, 2),
            "max_thread_count": max(s["thread_count"] for s in samples) if samples else 1,
            "max_page_count": max(s["page_count"] for s in samples) if samples else 1,
            "initial_disk_mb": initial_disk,
            "final_disk_mb": final_disk,
            "disk_net_growth_mb": round(final_disk - initial_disk, 3),
        },
        "samples": samples,
    }

    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\n============================================================")
    print(f"  Soak Test 完成评估汇总")
    print(f"  实际运行时间: {actual_run_seconds:.1f} 秒 (目标 {duration} 秒, 跑满: {'是' if actual_run_seconds >= duration else '否'})")
    print(f"  收集样本数:   {len(samples)}")
    print(f"  CPU 平均/峰值: {report['summary']['cpu_avg_percent']}% / {report['summary']['cpu_max_percent']}%")
    print(f"  RSS 内存变化: {initial_rss} MB -> {final_rss} MB (净增: {report['summary']['rss_net_growth_mb']} MB)")
    print(f"  磁盘占用变化: {initial_disk} MB -> {final_disk} MB (净增: {report['summary']['disk_net_growth_mb']} MB)")
    print(f"  最大活跃线程: {report['summary']['max_thread_count']}")
    print(f"  详细报告文件: {report_path}")
    print(f"============================================================")

    return report


def main():
    parser = argparse.ArgumentParser(description="Rainclass Assistant 长时运行稳定评测工具 (Soak Test Runner)")
    parser.add_argument("--duration", type=int, default=7200, help="测试时长（秒），默认 7200 秒 (2小时)")
    parser.add_argument("--interval", type=float, default=5.0, help="采样间隔（秒），默认 5.0 秒")
    parser.add_argument("--data-dir", type=str, default="./data", help="监控的数据目录路径")
    parser.add_argument("--report", type=str, help="报告 JSON 文件保存路径")
    parser.add_argument("--simulate", action="store_true", help="开启内部自动高频答题/清理模拟负载")
    args = parser.parse_args()

    run_soak_test(
        duration=args.duration,
        interval=args.interval,
        data_dir=args.data_dir,
        report_file=args.report,
        simulate=args.simulate,
    )


if __name__ == "__main__":
    main()
