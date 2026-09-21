"""后台无 GUI 服务入口 - 面向 Linux Docker / NAS / 无 DISPLAY 环境。

支持命令：
  python -m src.worker --help
  python -m src.worker --mode observe
  python -m src.worker --mode auto --data-dir /app/state
  python -m src.worker --check
  python -m src.worker --login
  python -m src.worker --import-session /path/to/browser_state.json
  python -m src.worker --healthcheck [--strict-ready]
"""

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from src.ai import AIService
from src.bot import Bot, BotState
from src.browser import (
    DEFAULT_SERVER,
    YUKETANG_SERVERS,
    BrowserManager,
    validate_session_data,
)
from src.cleaner import ArtifactCleaner
from src.config import Config, ConfigError
from src.instance_lock import InstanceLock
from src.log import setup_logging, stop_logging
from src.notification import NotificationService
from src.paths import PathManager
from src.status import ServiceState, StatusTracker
from src.storage import QuizStorage

logger = logging.getLogger("src.worker")

# 退出码定义
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NEEDS_LOGIN = 2


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="python -m src.worker",
        description="雨课堂自动助手后台服务版（无 GUI、支持观察/自动答题模式、NAS 长期可靠运行与恢复）。",
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
    parser.add_argument(
        "--login",
        action="store_true",
        help="本地登录辅助：唤起图形浏览器完成雨课堂登录并原子保存会话",
    )
    parser.add_argument(
        "--import-session",
        dest="import_session_path",
        metavar="PATH",
        help="导入外部会话状态文件（校验 JSON 结构与雨课堂站点绑定后原子替换）",
    )
    parser.add_argument(
        "--healthcheck",
        action="store_true",
        help="健康检查命令：读取状态文件输出结构化 JSON 诊断信息并返回退出码",
    )
    parser.add_argument(
        "--strict-ready",
        action="store_true",
        help="与 --healthcheck 连用：仅在业务就绪时返回 0（若处于 needs_login 则返回退出码 2）",
    )
    return parser


def handle_healthcheck(paths: PathManager, strict_ready: bool = False) -> int:
    """执行轻量级健康检查（无需启动 Web 服务）。"""
    health_file = paths.health_file
    if not health_file.exists():
        payload = {
            "status": "unknown",
            "alive": False,
            "ready": False,
            "reason": f"未找到健康状态文件: {health_file}",
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_ERROR

    try:
        with open(health_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        payload = {
            "status": "error",
            "alive": False,
            "ready": False,
            "reason": f"无法读取健康状态文件: {e}",
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_ERROR

    # 校验心跳新鲜度（超过 120 秒未产生有效心跳判定为挂死/僵尸进程）
    heartbeat_age = float(data.get("heartbeat_age_seconds", 999.0))
    last_hb_str = data.get("last_heartbeat_at", "")
    if last_hb_str:
        try:
            hb_t = time.mktime(time.strptime(last_hb_str, "%Y-%m-%d %H:%M:%S"))
            heartbeat_age = max(0.0, time.time() - hb_t)
            data["heartbeat_age_seconds"] = round(heartbeat_age, 1)
        except Exception:
            pass

    if heartbeat_age > 120.0 and data.get("status") not in ("stopped", "error"):
        data["alive"] = False
        data["ready"] = False
        data["reason"] = f"心跳超时 (距离上次心跳已过去 {int(heartbeat_age)} 秒，进程可能已卡死)"

    print(json.dumps(data, ensure_ascii=False, indent=2))

    if not data.get("alive"):
        return EXIT_ERROR

    if strict_ready:
        if data.get("ready"):
            return EXIT_OK
        if data.get("status") == ServiceState.NEEDS_LOGIN.value:
            return EXIT_NEEDS_LOGIN
        return EXIT_ERROR

    # 默认模式：进程存活即返回 0（避免容器因等待登录而无限重启）
    return EXIT_OK


def handle_import_session(paths: PathManager, import_path: str, base_url: str) -> int:
    """导入外部会话文件：校验格式与站点，临时文件原子替换，严禁泄漏凭据。"""
    src_file = Path(import_path).resolve()
    if not src_file.exists():
        print(f"错误：待导入的会话文件不存在: {src_file}", file=sys.stderr)
        return EXIT_ERROR

    try:
        with open(src_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"错误：会话文件无法解析为合法 JSON: {e}", file=sys.stderr)
        return EXIT_ERROR

    valid, msg = validate_session_data(data, expected_base_url=base_url)
    if not valid:
        print(f"错误：会话文件校验未通过: {msg}", file=sys.stderr)
        return EXIT_ERROR

    try:
        paths.state_file.parent.mkdir(parents=True, exist_ok=True)
        temp_file = paths.state_file.with_suffix(".tmp")
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_file, paths.state_file)
        print(f"成功：会话文件已原子导入至 {paths.state_file}")
        return EXIT_OK
    except Exception as e:
        print(f"错误：保存会话文件失败: {e}", file=sys.stderr)
        return EXIT_ERROR


def handle_login(paths: PathManager, base_url: str) -> int:
    """本地交互式登录辅助：唤起图形浏览器供用户登录雨课堂。"""
    lock = InstanceLock(paths.lock_file)
    if not lock.acquire():
        print(f"错误：无法获取单实例锁 ({paths.lock_file})。已有服务正在运行！", file=sys.stderr)
        return EXIT_ERROR
    try:
        browser_mgr = BrowserManager(
            headless=False,
            state_file=str(paths.state_file),
            base_url=base_url,
            auto_install=True,
        )
        print("正在唤起图形化 Chromium 浏览器供用户登录雨课堂（最长等待 180 秒）...")
        success = browser_mgr.get_cookies(timeout_ms=180_000)
        if success:
            print(f"成功：登录会话已原子保存至 {paths.state_file}")
            return EXIT_OK
        else:
            print("错误：登录未完成或超时。", file=sys.stderr)
            return EXIT_ERROR
    finally:
        lock.release()


def run_worker(args: Optional[argparse.Namespace] = None) -> int:
    """Worker 主执行函数，返回进程退出码。"""
    if args is None:
        parser = build_parser()
        args = parser.parse_args()

    paths = PathManager(data_dir=args.data_dir, config_file=args.config_path)
    paths.ensure_dirs()

    # 优先处理健康检查命令（无需加锁，独立轻量执行）
    if getattr(args, "healthcheck", False):
        return handle_healthcheck(paths, strict_ready=getattr(args, "strict_ready", False))

    # 预加载配置以获取雨课堂站点
    try:
        config = Config.load_strict(paths.config_file)
    except ConfigError as err:
        # 自检或普通启动均报错
        print(f"配置错误：\n{err}", file=sys.stderr)
        return EXIT_ERROR

    server_name = config.get("yuketang_server")
    classroom_url = config.get("classroom_url", "")
    if not server_name or server_name == DEFAULT_SERVER:
        for s_name, s_url in YUKETANG_SERVERS.items():
            if s_url in classroom_url:
                server_name = s_name
                break
    server_name = server_name or DEFAULT_SERVER
    base_url = YUKETANG_SERVERS.get(server_name, YUKETANG_SERVERS[DEFAULT_SERVER])

    # 优先处理会话导入命令
    if getattr(args, "import_session_path", None):
        return handle_import_session(paths, args.import_session_path, base_url)

    # 优先处理本地登录命令
    if getattr(args, "login", False):
        return handle_login(paths, base_url)

    # 初始化纯后台日志（输出到控制台与数据目录下的 log/bot.log，带脱敏与截断保护）
    setup_logging(
        log_dir=paths.log_dir,
        enable_console=True,
        enable_file=True,
    )

    logger.info("=== 雨课堂后台 Worker 服务启动 ===")
    logger.info("数据根目录: %s", paths.data_dir)
    logger.info("配置文件路径: %s", paths.config_file)
    logger.info("会话文件路径: %s", paths.state_file)
    logger.info("状态健康文件: %s", paths.health_file)
    logger.info("数据库路径: %s", paths.db_file)

    # 强制固定后台为无头模式
    config.set("headless_mode", True)

    # 运行模式解析：命令行参数 > 环境变量 (RAINCLASS_MODE / WORKER_MODE) > 配置文件 > 默认 observe
    env_mode = (os.getenv("RAINCLASS_MODE") or os.getenv("WORKER_MODE") or "").strip().lower()
    valid_env_mode = env_mode if env_mode in ("observe", "auto") else None
    effective_mode = getattr(args, "mode", None) or valid_env_mode or config.get("mode", "observe")
    config.set("mode", effective_mode)
    logger.info("运行模式: %s (来源: %s)", effective_mode, "命令行" if getattr(args, "mode", None) else ("环境变量" if valid_env_mode else "配置文件"))

    # 单实例锁保护（按数据目录隔离不同实例）
    lock = InstanceLock(paths.lock_file)
    if not lock.acquire():
        logger.error(
            "无法获取单实例进程锁 (%s)。已有服务正在该目录运行，同一数据目录下禁止多实例并发！",
            paths.lock_file,
        )
        stop_logging()
        return EXIT_ERROR

    stop_event = threading.Event()
    status_tracker = StatusTracker(
        health_file=paths.health_file,
        account_id=config.get("account", "default"),
        server_name=server_name,
    )
    status_tracker.set_state(ServiceState.STARTING, "服务启动与配置初始化中")

    storage = QuizStorage(paths.db_file)
    cleaner = ArtifactCleaner(paths.data_dir)

    def _signal_handler(signum, frame):
        sig_name = "SIGINT" if signum == signal.SIGINT else "SIGTERM"
        logger.info("捕获信号 %s，正在优雅停止服务...", sig_name)
        status_tracker.set_state(ServiceState.STOPPING, f"捕获信号 {sig_name}，正在退出")
        stop_event.set()

    # 注册系统退出信号（Windows 支持 SIGINT/SIGTERM）
    try:
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)
    except Exception as e:
        logger.debug("注册信号处理异常：%s", e)

    # 环境自检模式 (--check)
    if getattr(args, "check", False):
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
        if valid:
            status_tracker.set_state(ServiceState.WAITING_CLASS, "环境自检通过")
        else:
            status_tracker.set_state(ServiceState.NEEDS_LOGIN, f"环境自检未通过: {msg}")
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
                status_tracker.set_state(
                    ServiceState.NEEDS_LOGIN,
                    f"会话检查未通过: {session_msg}",
                )
                logger.warning("⚠ [NEEDS_LOGIN] 会话检查未通过: %s", session_msg)
                logger.info(
                    "修复指引: 请在本地运行 python -m src.worker --login，或将有效的 browser_state.json 导入: %s",
                    paths.state_file,
                )
                if not getattr(args, "wait_for_session", False):
                    exit_code = EXIT_NEEDS_LOGIN
                    break
                logger.info("(--wait-for-session 开启) 正在等待会话文件导入，每 10 秒检测一次...")
                while not stop_event.is_set():
                    status_tracker.heartbeat()
                    if stop_event.wait(10):
                        break
                    valid, _ = browser_mgr.validate_session()
                    if valid:
                        logger.info("检测到有效会话文件已导入，正在继续启动服务...")
                        status_tracker.set_state(ServiceState.STARTING, "有效会话已导入，准备就绪")
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
                status_tracker=status_tracker,
                storage=storage,
                cleaner=cleaner,
            )

            # 在当前主线程内执行 Bot 循环（严格遵守 Playwright 线程归属规则）
            bot.run()

            if bot.state == BotState.NEEDS_LOGIN:
                status_tracker.set_state(ServiceState.NEEDS_LOGIN, "运行中会话失效")
                exit_code = EXIT_NEEDS_LOGIN
                if not getattr(args, "wait_for_session", False):
                    break
                logger.info("会话失效，等待重新导入...")
                if stop_event.wait(10):
                    break
            elif bot.state == BotState.ERROR:
                status_tracker.set_state(ServiceState.ERROR, "Bot 发生严重错误退出")
                exit_code = EXIT_ERROR
                break
            else:
                # 正常停止或退出循环
                exit_code = EXIT_OK
                break

    except Exception as e:
        logger.error("Worker 发生未捕获异常：%s", e, exc_info=True)
        status_tracker.set_state(ServiceState.ERROR, f"未捕获异常: {e}")
        exit_code = EXIT_ERROR
    finally:
        if exit_code == EXIT_OK:
            status_tracker.set_state(ServiceState.STOPPED, "Worker 进程已正常退出")
        elif exit_code == EXIT_NEEDS_LOGIN:
            status_tracker.set_state(ServiceState.NEEDS_LOGIN, "Worker 等待登录会话退出")
        else:
            status_tracker.set_state(ServiceState.ERROR, f"Worker 异常退出，退出码: {exit_code}")

        lock.release()
        logger.info("Worker 进程已收尾退出，退出码: %d", exit_code)
        stop_logging()

    return exit_code


def main() -> None:
    sys.exit(run_worker())


if __name__ == "__main__":
    main()
