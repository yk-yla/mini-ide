"""mini-ide 入口"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))


def _is_cli_mode() -> bool:
    """sys.argv[1] 以 '--' 开头时走 CLI 模式。"""
    return len(sys.argv) > 1 and sys.argv[1].startswith("--")


if _is_cli_mode():
    try:
        from src.core.cli_client import run_cli
        sys.exit(run_cli(sys.argv))
    except SystemExit:
        raise
    except Exception as e:
        try:
            import json
            from src.core.cli_client import _safe_write

            _safe_write(
                sys.stderr,
                json.dumps(
                    {"ok": False, "error": f"cli failed: {type(e).__name__}: {e}"},
                    ensure_ascii=True,
                ) + "\n",
            )
        except Exception:
            pass
        sys.exit(1)


from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QLockFile, QStandardPaths, QThread, Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from src.util import app_log
from src.core.search_worker_lifecycle import stop_background_workers
from src.ui.main_window import MainWindow
from src.ui.theme import apply_theme
from src.core.config import AppConfig


SERVER_NAME = "mini-ide-single-instance"
_INSTANCE_LOCK: QLockFile | None = None


def _instance_lock_path() -> str:
    """返回当前用户范围内、跨 GUI/CLI 启动进程共享的锁文件路径。"""
    temp_dir = QStandardPaths.writableLocation(QStandardPaths.TempLocation)
    if not temp_dir:
        temp_dir = str(Path.cwd())
    return str(Path(temp_dir) / "whaty-mini-ide-single-instance.lock")


def _acquire_instance_lock() -> QLockFile | None:
    """串行化 GUI 冷启动，避免 IPC 尚未监听时重复创建窗口。"""
    global _INSTANCE_LOCK
    lock = QLockFile(_instance_lock_path())
    if not lock.tryLock(0):
        return None
    _INSTANCE_LOCK = lock
    return lock


def _forward_to_existing(path: str | None) -> bool:
    """尝试把 path 发给已运行的 mini-ide。连上了返回 True，本进程应退出。"""
    sock = QLocalSocket()
    sock.connectToServer(SERVER_NAME)
    if not sock.waitForConnected(500):
        return False
    payload = (path or "").encode("utf-8")
    sock.write(payload)
    sock.flush()
    sock.waitForBytesWritten(1000)
    sock.disconnectFromServer()
    return True


def _wait_for_existing(path: str | None, timeout_ms: int = 15000) -> bool:
    """等待现有实例完成冷启动，并在 IPC 就绪后转发一次请求。"""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if _forward_to_existing(path):
            return True
        QThread.msleep(100)
    return False


def _start_local_server(window: MainWindow) -> QLocalServer | None:
    """在本实例启动 IPC server 监听后续右键请求。"""
    server = QLocalServer(window)
    if not server.listen(SERVER_NAME):
        app_log.get_logger("main").warning(
            "QLocalServer 监听失败: %s", server.errorString()
        )
        return None

    def _on_new_connection():
        from src.core.cli_server import (
            _send_response, handle_async_cli_request, handle_cli_request,
        )
        import json

        sock = server.nextPendingConnection()
        if sock is None:
            return
        if sock.waitForReadyRead(1000):
            data = bytes(sock.readAll()).decode("utf-8", errors="replace").strip()
        else:
            data = ""

        # 尝试作为 CLI JSON 命令处理
        if data.startswith("{"):
            try:
                cmd = json.loads(data)
                if "cmd" in cmd:
                    # 异步命令（health / compile）需要保持连接
                    if handle_async_cli_request(cmd, sock, window):
                        return
                    # 同步命令
                    if handle_cli_request(data, sock, window):
                        return
            except json.JSONDecodeError:
                pass
            except Exception as exc:
                app_log.get_logger("main").exception("异步 CLI 命令执行异常")
                _send_response(sock, {
                    "ok": False,
                    "error": f"cli command failed: {type(exc).__name__}: {exc}",
                })
                return

        # 老协议：打开项目路径
        window.activate_and_open(data or None)
        sock.disconnectFromServer()

    server.newConnection.connect(_on_new_connection)
    return server


def _set_windows_app_id():
    """让 Windows 任务栏按 mini-ide 自己的 AppUserModelID 分组并显示我们的图标，
    而不是 pythonw.exe 的默认蓝色 logo。必须在任何 window 显示前调用。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("whaty.mini-ide")
    except Exception:
        pass


def main():
    logger = app_log.init()
    try:
        _set_windows_app_id()
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
        app = QApplication(sys.argv)
        # CLI --quit calls QApplication.quit() directly, so cleanup must also
        # run from aboutToQuit instead of relying only on MainWindow.closeEvent.
        app.aboutToQuit.connect(stop_background_workers)
        app.setApplicationName("mini-ide")
        app.setOrganizationName("whaty")
        # 主窗口关闭时会隐藏到系统托盘，不能因最后一个窗口隐藏而结束事件循环。
        app.setQuitOnLastWindowClosed(False)

        # 全局基准字体：用 setFont 直接定基准字号（比 QSS font-size 优先级更稳，
        # 不会被个别 widget 的局部样式或平台默认覆盖）。具体字号取 theme 的主 UI token。
        from src.ui.theme import FONT_PT_UI
        _base_font = app.font()
        _base_font.setPointSize(FONT_PT_UI)
        app.setFont(_base_font)

        initial_project = sys.argv[1] if len(sys.argv) > 1 else None

        # 单实例：先拿启动锁，避免第一个实例尚未监听 IPC 时第二个实例继续启动。
        # 已有实例拿着锁时，等待它完成冷启动后转发路径并退出。
        if _acquire_instance_lock() is None:
            if _wait_for_existing(initial_project):
                logger.info("检测到正在启动的 mini-ide，已等待并转发路径=%r 后退出", initial_project)
                return
            logger.error("已有 mini-ide 正在启动，但 IPC 在等待窗口内仍未就绪")
            return

        # 兼容锁文件不存在但旧实例已经在运行的情况。
        if _forward_to_existing(initial_project):
            logger.info("检测到已运行的 mini-ide，已转发路径=%r 后退出", initial_project)
            return

        # 图标：优先 ICO（Windows 任务栏更清晰），回落 PNG。
        for name in ("icon.ico", "icon.png"):
            icon_path = ROOT / "src" / "resources" / name
            if icon_path.exists():
                app.setWindowIcon(QIcon(str(icon_path)))
                break

        config = AppConfig.load()
        config.theme = apply_theme(app, config.theme)
        window = MainWindow(config)
        window.pending_initial_project = initial_project

        # 先确认 IPC 已成功监听，再显示窗口；失败时不启动一个无法接收请求的副本。
        window._local_server = _start_local_server(window)
        if window._local_server is None:
            if _wait_for_existing(initial_project, timeout_ms=3000):
                logger.info("检测到另一个 mini-ide 已监听 IPC，已转发路径=%r 后退出", initial_project)
            else:
                logger.error("mini-ide IPC 监听失败，取消本次窗口启动")
            window.close()
            return
        window.show()

        # 主线程心跳，给 freeze-watchdog 用
        hb_timer = QTimer()
        hb_timer.setInterval(1000)
        hb_timer.timeout.connect(app_log.heartbeat)
        hb_timer.start()

        perf_timer = QTimer()
        perf_timer.setInterval(100)
        perf_timer.timeout.connect(app_log.performance_heartbeat)
        perf_timer.start()

        logger.info("进入事件循环")
        rc = app.exec()
        logger.info("mini-ide 正常退出 (rc=%d)", rc)
        sys.exit(rc)
    except Exception:
        logger.exception("main() 抛出异常")
        raise


if __name__ == "__main__":
    main()
