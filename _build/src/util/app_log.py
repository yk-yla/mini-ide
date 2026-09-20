"""mini-ide 自身的运行日志

用途：
- 记录 启动/关闭、打开项目、运行器状态变化等关键事件
- 捕获未处理异常（Python 和 Qt 两个层面）
- 主线程卡死检测（watchdog 另起线程，超过 3 秒无心跳就 dump 主线程栈）

日志落盘到 %APPDATA%/mini-ide/logs/mini-ide-YYYYMMDD.log，按天切分，
保留最近 14 天。用户可以从"帮助 → 打开 mini-ide 日志"快速打开。
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path


LOG_FORMAT = "%(asctime)s.%(msecs)03d [%(levelname)-5s] [%(threadName)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _log_dir() -> Path:
    from src.core.config import LOG_DIR
    return LOG_DIR


def _today_log_file() -> Path:
    return _log_dir() / f"mini-ide-{datetime.now():%Y%m%d}.log"


_initialized = False
_logger: logging.Logger | None = None
_heartbeat = 0.0
_freeze_watchdog_started = False
_performance_tick = 0.0
_performance_last_warned = 0.0


def init() -> logging.Logger:
    """在 main() 最早的时候调用一次。多次调用幂等。"""
    global _initialized, _logger
    if _initialized and _logger:
        return _logger

    logger = logging.getLogger("mini-ide")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    # 避免重复装 handler
    for h in list(logger.handlers):
        logger.removeHandler(h)

    fmt = logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    log_file = _today_log_file()
    try:
        fh = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=3,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        fh.setLevel(logging.DEBUG)
        logger.addHandler(fh)
    except OSError as e:
        print(f"[app_log] 打开日志文件失败: {e}", file=sys.stderr)

    # 保留控制台输出（pythonw 下没有控制台，无害；run.bat 模式可看）
    ch = logging.StreamHandler(sys.stderr)
    ch.setFormatter(fmt)
    ch.setLevel(logging.INFO)
    logger.addHandler(ch)

    _prune_old_logs(keep_days=14)

    _install_excepthook(logger)
    _install_qt_message_handler(logger)
    _start_freeze_watchdog(logger)

    logger.info("=" * 60)
    logger.info("mini-ide 启动 | python=%s | platform=%s", sys.version.split()[0], sys.platform)
    logger.info("日志文件: %s", log_file)

    _logger = logger
    _initialized = True
    return logger


def get_logger(name: str = "") -> logging.Logger:
    base = init()
    if not name:
        return base
    return base.getChild(name)


def heartbeat() -> None:
    """主线程心跳：卡死检测器用这个判断主线程是否还在响应"""
    global _heartbeat
    _heartbeat = time.time()


def performance_heartbeat(interval_ms: int = 100, stall_ms: int = 250) -> None:
    """记录亚秒级 UI 事件循环延迟，不采集用户输入或文件内容。"""
    global _performance_tick, _performance_last_warned
    now = time.perf_counter()
    if _performance_tick:
        delay_ms = max(0.0, (now - _performance_tick) * 1000 - interval_ms)
        if delay_ms >= stall_ms and now - _performance_last_warned >= 2.0:
            _performance_last_warned = now
            logging.getLogger("mini-ide.performance").warning(
                "perf op=ui-stall duration_ms=%.1f status=warn", delay_ms,
            )
    _performance_tick = now


def log_path_today() -> Path:
    return _today_log_file()


# ---- 内部 ----

def _prune_old_logs(keep_days: int) -> None:
    cutoff = datetime.now() - timedelta(days=keep_days)
    for f in _log_dir().glob("mini-ide-*.log*"):
        try:
            if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                f.unlink(missing_ok=True)
        except OSError:
            pass


def _install_excepthook(logger: logging.Logger) -> None:
    """Python 未捕获异常 → 写入日志"""
    default = sys.excepthook

    def hook(exc_type, exc, tb):
        logger.critical(
            "未捕获异常:\n%s",
            "".join(traceback.format_exception(exc_type, exc, tb)),
        )
        default(exc_type, exc, tb)

    sys.excepthook = hook

    # threading 异常也记录（Python 3.8+）
    if hasattr(threading, "excepthook"):
        def thook(args):
            logger.error(
                "线程 %s 未捕获异常:\n%s",
                args.thread.name if args.thread else "?",
                "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)),
            )
        threading.excepthook = thook


def _install_qt_message_handler(logger: logging.Logger) -> None:
    """接管 Qt 自己发的警告/严重错误（否则只会打到 stderr，pythonw 下看不到）"""
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except ImportError:
        return

    level_map = {
        QtMsgType.QtDebugMsg:    logging.DEBUG,
        QtMsgType.QtInfoMsg:     logging.INFO,
        QtMsgType.QtWarningMsg:  logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg:    logging.CRITICAL,
    }

    qt_logger = logger.getChild("qt")

    def handler(msg_type, context, message):
        level = level_map.get(msg_type, logging.INFO)
        ctx = ""
        if context and context.file:
            ctx = f" ({context.file}:{context.line})"
        qt_logger.log(level, "%s%s", message, ctx)

    qInstallMessageHandler(handler)


def _start_freeze_watchdog(logger: logging.Logger) -> None:
    """主线程卡死检测

    主线程每 1s 由 QTimer 调用 heartbeat()。后台线程每 1s 检查
    _heartbeat 是否超过 3s 没更新，若是则 dump 所有线程栈到日志。
    """
    global _freeze_watchdog_started
    if _freeze_watchdog_started:
        return
    _freeze_watchdog_started = True
    heartbeat()

    watchdog_logger = logger.getChild("watchdog")

    def loop():
        last_warned = 0.0
        while True:
            time.sleep(1.0)
            gap = time.time() - _heartbeat
            if gap > 3.0 and time.time() - last_warned > 10.0:
                last_warned = time.time()
                frames = []
                for tid, frame in sys._current_frames().items():
                    name = _thread_name(tid)
                    stack = "".join(traceback.format_stack(frame))
                    frames.append(f"--- {name} (tid={tid}) ---\n{stack}")
                watchdog_logger.warning(
                    "主线程疑似卡死 %.1fs，栈快照：\n%s",
                    gap, "\n".join(frames),
                )

    t = threading.Thread(target=loop, daemon=True, name="freeze-watchdog")
    t.start()


def _thread_name(tid: int) -> str:
    for t in threading.enumerate():
        if t.ident == tid:
            return t.name
    return "?"
