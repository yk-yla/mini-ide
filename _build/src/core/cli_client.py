"""CLI 客户端：连接已运行的 mini-ide 实例，发送 JSON 命令，等待响应后退出。

CLI 模式只用 QCoreApplication + QLocalSocket（不依赖 QtWidgets）。
"""
from __future__ import annotations

import json
import sys
import time

from PySide6.QtCore import QCoreApplication
from PySide6.QtNetwork import QLocalSocket


_STD_OUTPUT_HANDLE = -11
_STD_ERROR_HANDLE = -12
_FILE_TYPE_UNKNOWN = 0
_FILE_TYPE_CHAR = 0x0002


def _win_std_handle(kind: int):
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel32.GetStdHandle.restype = wintypes.HANDLE
        handle = kernel32.GetStdHandle(kind)
        invalid = ctypes.c_void_p(-1).value
        if handle in (None, 0, invalid):
            return None
        return handle
    except Exception:
        return None


def _win_std_handle_usable(kind: int) -> bool:
    handle = _win_std_handle(kind)
    if handle is None:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.GetFileType.argtypes = [wintypes.HANDLE]
        kernel32.GetFileType.restype = wintypes.DWORD
        return kernel32.GetFileType(handle) != _FILE_TYPE_UNKNOWN
    except Exception:
        return False


def _win_write_std(kind: int, text: str) -> bool:
    """GUI 子系统 exe 没有 Python stdout 时，直接写 Windows 标准句柄。"""
    handle = _win_std_handle(kind)
    if handle is None:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.GetFileType.argtypes = [wintypes.HANDLE]
        kernel32.GetFileType.restype = wintypes.DWORD
        file_type = kernel32.GetFileType(handle)

        written = wintypes.DWORD()
        if file_type == _FILE_TYPE_CHAR:
            kernel32.WriteConsoleW.argtypes = [
                wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
            ]
            kernel32.WriteConsoleW.restype = wintypes.BOOL
            if kernel32.WriteConsoleW(handle, text, len(text), ctypes.byref(written), None):
                return True

        data = text.encode("utf-8", errors="replace")
        buf = ctypes.create_string_buffer(data)
        kernel32.WriteFile.argtypes = [
            wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
        ]
        kernel32.WriteFile.restype = wintypes.BOOL
        return bool(kernel32.WriteFile(handle, buf, len(data), ctypes.byref(written), None))
    except Exception:
        return False


def _safe_write(stream, text: str) -> None:
    """安全写入——GUI exe 无控制台时 stream 可能无效。"""
    try:
        if stream and hasattr(stream, "write"):
            stream.write(text)
            stream.flush()
            return
    except Exception:
        pass
    kind = _STD_ERROR_HANDLE if stream is sys.stderr else _STD_OUTPUT_HANDLE
    if _win_write_std(kind, text):
        return
    try:
        if stream and hasattr(stream, "write"):
            safe_text = text.encode("ascii", errors="backslashreplace").decode("ascii")
            stream.write(safe_text)
            stream.flush()
    except Exception:
        pass


def _configure_stream_utf8(stream) -> None:
    """让 console CLI 的管道输出始终使用 UTF-8。"""
    try:
        if stream and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass


def _attach_console():
    """GUI exe 没有标准句柄时，CLI 模式附加到父控制台作为兜底。"""
    if sys.platform != "win32":
        return
    # 如果 stdout/stderr 已经是管道或控制台，不要 AttachConsole 后改写到 CONOUT$；
    # 否则 PowerShell / AI 工具捕获不到 stdout。
    if _win_std_handle_usable(_STD_OUTPUT_HANDLE) or _win_std_handle_usable(_STD_ERROR_HANDLE):
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # ATTACH_PARENT_PROCESS = -1
        kernel32.AttachConsole(-1)
    except Exception:
        pass


from src.core.cli_contract import SERVER_NAME

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_BAD_ARGS = 2
EXIT_NOT_RUNNING = 3
EXIT_TIMEOUT = 4


def _decode_response(response_buf: bytes):
    """解析服务端首行响应；空响应和坏 JSON 都必须是显式失败。"""
    if not response_buf.strip():
        return None, "empty response"
    line = response_buf.split(b"\n", 1)[0]
    try:
        return json.loads(line), None
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, "invalid response"


def _parse_args(argv: list[str]) -> dict | None:
    from src.core.cli_contract import ArgumentError, parse_command
    try:
        return parse_command(argv[1:])
    except ArgumentError:
        return None


def _response_timeout_ms(cmd: dict) -> int:
    """按命令生命周期设置 IPC 等待时间。"""
    action = cmd["cmd"]
    if cmd.get("async"):
        return 30000
    if action == "operation":
        return (int(cmd.get("wait_seconds", 0)) + 5) * 1000
    if action == "quit":
        return 120000
    if action in (
        "health", "compile", "run-action", "ensure-running",
        "open-workspace",
        "create-development-workspace", "workspace-review", "workspace-delete-check",
        "workspace-commit-push", "workspace-sync", "workspace-sync-push",
        "workspace-merge", "workspace-delete",
    ) or action in ("start", "stop", "restart"):
        defaults = {
            "start": 60, "stop": 60, "restart": 60,
            "create-development-workspace": 600,
            "open-workspace": 600,
            "workspace-commit-push": 600,
            "workspace-sync": 600,
            "workspace-sync-push": 600,
            "workspace-merge": 600,
            "workspace-delete": 600,
        }
        timeout = int(cmd.get("timeout", defaults.get(action, 120)))
        return (timeout + 5) * 1000
    if action in ("git-diff", "git-ai-context"):
        return 120000
    return 30000


def _start_gui_and_wait(timeout_sec: int = 15) -> bool:
    """启动 mini-ide.exe，轮询等待 IPC socket 可连接，成功返回 True。"""
    import subprocess
    from pathlib import Path

    gui_exe = Path(sys.argv[0]).resolve().parent / "mini-ide.exe"
    if not gui_exe.exists():
        return False
    try:
        subprocess.Popen(
            [str(gui_exe)],
            creationflags=0x00000008,  # DETACHED_PROCESS，独立运行不继承父进程控制台
        )
    except OSError:
        return False

    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        probe = QLocalSocket()
        probe.connectToServer(SERVER_NAME)
        if probe.waitForConnected(500):
            probe.disconnectFromServer()
            return True
        time.sleep(0.3)
    return False


def run_cli(argv: list[str]) -> int:
    """CLI 模式主入口。返回退出码。"""
    _attach_console()
    _configure_stream_utf8(sys.stdout)
    _configure_stream_utf8(sys.stderr)

    # 摘出 --auto-start flag，不传给命令解析和 Qt
    auto_start = "--auto-start" in argv
    filtered_argv = [a for a in argv if a != "--auto-start"]

    from src.core.cli_contract import (
        ArgumentError, capabilities, help_text, machine_result, normalize_targets,
        parse_command, result_code,
    )
    machine = "--machine" in filtered_argv
    filtered_argv = [arg for arg in filtered_argv if arg != "--machine"]

    def emit(result, *, failure=False, command=""):
        payload = machine_result(command or cmd.get("cmd", ""), result) if machine else result
        _safe_write(sys.stdout if machine or not failure else sys.stderr,
                    json.dumps(payload, ensure_ascii=False) + "\n")

    cmd = {}
    try:
        cmd = normalize_targets(parse_command(filtered_argv[1:]))
    except ArgumentError as exc:
        emit({"ok": False, "code": "invalid_arguments", "error": str(exc)}, failure=True)
        if not machine:
            _safe_write(sys.stderr, help_text())
        return EXIT_BAD_ARGS

    if cmd["cmd"] == "help":
        try:
            content = help_text(cmd.get("command"))
        except ArgumentError as exc:
            emit({"ok": False, "code": "invalid_arguments", "error": str(exc)}, failure=True)
            return EXIT_BAD_ARGS
        if machine:
            emit({"ok": True, "help": content})
        else:
            _safe_write(sys.stdout, content)
        return EXIT_OK
    if cmd["cmd"] == "capabilities":
        emit(capabilities())
        return EXIT_OK
    if cmd["cmd"] == "version":
        from src.core.app_version import build_info
        emit({"ok": True, **build_info()})
        return EXIT_OK

    # quit / preflight-build / status 不触发自动启动
    if cmd["cmd"] in {"quit", "preflight-build", "status"}:
        auto_start = False

    app = QCoreApplication(filtered_argv)
    sock = QLocalSocket()
    sock.connectToServer(SERVER_NAME)
    if not sock.waitForConnected(2000):
        if auto_start and _start_gui_and_wait():
            sock = QLocalSocket()
            sock.connectToServer(SERVER_NAME)
            if not sock.waitForConnected(5000):
                emit({"ok": False, "code": "ide_not_running", "error": "mini-ide started but IPC not ready"}, failure=True)
                return EXIT_NOT_RUNNING
        else:
            emit({"ok": False, "code": "ide_not_running", "error": "mini-ide is not running"}, failure=True)
            return EXIT_NOT_RUNNING

    payload = json.dumps(cmd, ensure_ascii=False) + "\n"
    sock.write(payload.encode("utf-8"))
    sock.flush()

    timeout_ms = _response_timeout_ms(cmd)
    response_buf = b""
    deadline = time.time() + timeout_ms / 1000.0

    while True:
        remaining = int((deadline - time.time()) * 1000)
        if remaining <= 0:
            emit({"ok": False, "code": "transport_timeout", "error": "timeout waiting for response; query the operation or retry with the same request-id"}, failure=True)
            sock.disconnectFromServer()
            return EXIT_TIMEOUT
        if sock.waitForReadyRead(min(remaining, 500)):
            response_buf += bytes(sock.readAll())
            if b"\n" in response_buf:
                break
        if sock.state() != QLocalSocket.LocalSocketState.ConnectedState:
            break

    sock.disconnectFromServer()

    result, response_error = _decode_response(response_buf)
    if response_error:
        emit({"ok": False, "code": "invalid_response", "error": response_error}, failure=True)
        return EXIT_FAIL

    if isinstance(result, dict) and result.get("ok") is False:
        result.setdefault("code", result_code(result))
    emit(result)
    code = result_code(result)
    if code == "ok":
        return EXIT_OK
    if "timeout" in code:
        return EXIT_TIMEOUT
    if code == "ide_not_running":
        return EXIT_NOT_RUNNING
    if code in ("invalid_arguments", "target_not_found", "project_not_found", "action_not_found", "operation_not_found"):
        return EXIT_BAD_ARGS
    return EXIT_FAIL


def _print_usage():
    from src.core.cli_contract import help_text
    _safe_write(sys.stderr, help_text())
