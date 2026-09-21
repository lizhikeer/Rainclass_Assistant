"""后台无 GUI 服务入口 - 面向 Linux Docker / 无 DISPLAY 环境。

支持命令：
  python -m src.worker --help
  python -m src.worker --mode observe
  python -m src.worker --mode auto --data-dir /app/state
  python -m src.worker --check
"""

import argparse
import logging
import os
import signal
import sys
import threading
import time
from typing import Optional

from src.ai import AIService
from src.bot import Bot, BotState
from src.browser import DEFAULT_SERVER, YUKETANG_SERVERS, BrowserManager
from src.config import Config, ConfigError
from src.instance_lock import InstanceLock
from src.log import setup_logging, stop_logging
from src.notification import NotificationService
from src.paths import PathManager

logger = logging.getLogger("src.worker")

# 退出码定义
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NEEDS_LOGIN = 2


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="python -m src.worker",
        description="雨课堂自动助手后台服务版（无 GUI、支持观察/自动答题模式、分段耗时分析）。",
    )
    parser.add_argument(
        "--config",
        dest="config_path",
        metavar="PATH",
        help="配置文件路径（默认：<data-dir>/config.json 或项目根目录 config.json）",
    )
    parser.add_argument(
        "--data-dir",
        dest="data_dir",
        metavar="DIR",
        help="可写数据根目录（默认读取 RAINCLASS_DATA_DIR 环境变量或当前目录 ./data）",
    )
    parser.add_argument(
        "--mode",
        choices=["observe", "auto"],
        help="运行模式：observe（观察模式，禁止签到、点击、提交和付费 AI 调用）/ auto（自动作答）",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="环境自检：校验配置、会话文件与锁状态后退出",
    )
    parser.add_argument(
        "--wait-for-session",
        action="store_true",
        help="会话缺失或失效时不退出进程，挂起等待用户导入 browser_state.json",
    )
    return parser


def run_worker(args: Optional[argparse.Namespace] = None) -> int:
    """Worker 主执行函数，返回进程退出码。"""
    if args is None:
        parser = build_parser()
        args = parser.parse_args()

    paths = PathManager(data_dir=args.data_dir, config_file=args.config_path)
    paths.ensure_dirs()

    # 初始化纯后台日志（输出到控制台与数据目录下的 log/bot.log）
    setup_logging(
        log_dir=paths.log_dir,
        enable_console=True,
        enable_file=True,
    )

    logger.info("=== 雨课堂后台 Worker 服务启动 ===")
    logger.info("数据根目录: %s", paths.data_dir)
    logger.info("配置文件路径: %s", paths.config_file)
    logger.info("会话文件路径: %s", paths.state_file)

    # 1. 严格加载配置
    try:
        config = Config.load_strict(paths.config_file)
    except ConfigError as err:
        logger.error("配置错误：\n%s", err)
        stop_logging()
        return EXIT_ERROR

    # 强制固定后台为无头模式
    config.set("headless_mode", True)

    # 命令行指定模式优先
    effective_mode = args.mode or config.get("mode", "observe")
    config.set("mode", effective_mode)
    logger.info("运行模式: %s", effective_mode)

    # 2. 单实例锁保护
    lock = InstanceLock(paths.lock_file)
    if not lock.acquire():
        logger.error(
            "无法获取单实例进程锁 (%s)。已有服务正在运行，同一数据目录下禁止多实例并发！",
            paths.lock_file,
        )
        stop_logging()
        return EXIT_ERROR

    stop_event = threading.Event()

    def _signal_handler(signum, frame):
        sig_name = "SIGINT" if signum == signal.SIGINT else "SIGTERM"
        logger.info("捕获信号 %s，正在优雅停止服务...", sig_name)
        stop_event.set()

    # 注册系统退出信号（Windows 支持 SIGINT/SIGTERM）
    try:
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)
    except Exception as e:
        logger.debug("注册信号处理异常：%s", e)

    server_name = config.get("yuketang_server", DEFAULT_SERVER)
    base_url = YUKETANG_SERVERS.get(server_name)

    # 3. 自检模式 (--check)
    if args.check:
        logger.info("--- 开始环境自检 ---")
        logger.info("配置格式: 正常通过")
        browser_mgr = BrowserManager(
            headless=True,
            state_file=str(paths.state_file),
            base_url=base_url,
            auto_install=False,
        )
        valid, msg = browser_mgr.validate_session()
        logger.info("会话状态检查: %s (%s)", "有效" if valid else "缺失/失效", msg)
        lock.release()
        stop_logging()
        return EXIT_OK if valid else EXIT_NEEDS_LOGIN

    exit_code = EXIT_OK

    try:
        while not stop_event.is_set():
            # 检查会话文件是否存在且有效
            browser_mgr = BrowserManager(
                headless=True,
                state_file=str(paths.state_file),
                base_url=base_url,
                auto_install=False,
            )

            has_session, session_msg = browser_mgr.validate_session()
            if not has_session:
                logger.warning("⚠ [NEEDS_LOGIN] 会话检查未通过: %s", session_msg)
                logger.info(
                    "修复指引: 请在本地或桌面环境登录雨课堂，将导出的 browser_state.json 存入: %s",
                    paths.state_file,
                )
                if not args.wait_for_session:
                    exit_code = EXIT_NEEDS_LOGIN
                    break
                logger.info("(--wait-for-session 开启) 正在等待会话文件导入，每 10 秒检测一次...")
                while not stop_event.is_set():
                    if stop_event.wait(10):
                        break
                    valid, _ = browser_mgr.validate_session()
                    if valid:
                        logger.info("检测到有效会话文件已导入，正在继续启动服务...")
                        break
                if stop_event.is_set():
                    break

            ai_service = AIService(config)
            notification = NotificationService(config.get("xxtui_api_key", ""))

            bot = Bot(
                config=config,
                browser=browser_mgr,
                ai_service=ai_service,
                notification=notification,
                stop_event=stop_event,
                mode=effective_mode,
                metrics_file=paths.metrics_file,
            )

            # 在当前主线程内执行 Bot 循环（严格遵守 Playwright 线程归属规则）
            bot.run()

            if bot.state == BotState.NEEDS_LOGIN:
                exit_code = EXIT_NEEDS_LOGIN
                if not args.wait_for_session:
                    break
                logger.info("会话失效，等待重新导入...")
                if stop_event.wait(10):
                    break
            elif bot.state == BotState.ERROR:
                exit_code = EXIT_ERROR
                break
            else:
                # 正常停止或退出循环
                exit_code = EXIT_OK
                break

    except Exception as e:
        logger.error("Worker 发生未捕获异常：%s", e, exc_info=True)
        exit_code = EXIT_ERROR
    finally:
        lock.release()
        logger.info("Worker 进程已收尾退出，退出码: %d", exit_code)
        stop_logging()

    return exit_code


def main() -> None:
    sys.exit(run_worker())


if __name__ == "__main__":
    main()
