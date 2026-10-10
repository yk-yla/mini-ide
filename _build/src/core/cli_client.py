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


SERVER_NAME = "mini-ide-single-instance"

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
    """解析 CLI 参数为 JSON 命令 dict。返回 None 表示参数错误。"""
    args = argv[1:]  # 跳过程序名
    if not args:
        return None

    cmd_name = args[0].lstrip("-")  # --list-projects → list-projects
    cmd_map = {
        "version": "version",
        "status": "status",
        "list-projects": "list-projects",
        "list-modules": "list-modules",
        "list-runtimes": "list-runtimes",
        "open": "open",
        "resolve-target": "resolve-target",
        "close": "close",
        "list-workspaces": "list-workspaces",
        "open-workspace": "open-workspace",
        "close-workspace": "close-workspace",
        "list-aggregates": "list-aggregates",
        "open-aggregate": "open-aggregate",
        "create-development-workspace": "create-development-workspace",
        "workspace-review": "workspace-review",
        "workspace-commit-push": "workspace-commit-push",
        "workspace-sync": "workspace-sync",
        "workspace-delete-check": "workspace-delete-check",
        "start": "start",
        "stop": "stop",
        "restart": "restart",
        "ensure-running": "ensure-running",
        "health": "health",
        "compile": "compile",
        "log": "log",
        "diagnose": "diagnose",
        "git-status": "git-status",
        "git-diff": "git-diff",
        "git-ai-context": "git-ai-context",
        "preflight-build": "preflight-build",
        "can-quit": "preflight-build",
        "quit": "quit",
    }
    if cmd_name not in cmd_map:
        return None

    result: dict = {"cmd": cmd_map[cmd_name]}
    rest = args[1:]

    if cmd_name == "version":
        return result if not rest else None

    if cmd_name in ("status", "list-projects", "list-workspaces", "close-workspace",
                    "list-aggregates", "can-quit"):
        return result

    if cmd_name == "preflight-build":
        if len(rest) > 1:
            return None
        if rest:
            result["project"] = rest[0]
        return result

    if cmd_name == "quit":
        return result

    if cmd_name == "open":
        if not rest:
            return None
        result["path"] = rest[0]
        return result

    if cmd_name == "close":
        if not rest:
            return None
        result["project"] = rest[0]
        return result

    if cmd_name == "open-workspace":
        if not rest:
            return None
        result["target"] = " ".join(rest).strip()
        return result

    if cmd_name in (
        "open-aggregate",
        "workspace-review", "workspace-delete-check",
    ):
        if not rest:
            return None
        result["target"] = rest[0]
        return result if len(rest) == 1 else None

    if cmd_name == "workspace-commit-push":
        if not rest:
            return None
        result["target"] = rest[0]
        index = 1
        while index < len(rest):
            if rest[index] == "--message" and index + 1 < len(rest):
                message = rest[index + 1].strip()
                if not message:
                    return None
                result["message"] = message
                index += 2
            else:
                return None
        return result

    if cmd_name == "resolve-target":
        if len(rest) != 1:
            return None
        result["path"] = rest[0]
        return result

    if cmd_name == "workspace-sync":
        if not rest:
            return None
        result["target"] = rest[0]
        index = 1
        while index < len(rest):
            if rest[index] == "--fetch":
                result["fetch_remote"] = True
                index += 1
            elif rest[index] == "--keep-conflicts":
                result["keep_conflicts"] = True
                index += 1
            else:
                return None
        return result

    if cmd_name == "create-development-workspace":
        if len(rest) < 2:
            return None
        result["aggregate"] = rest[0]
        result["name"] = rest[1]
        index = 2
        while index < len(rest):
            if rest[index] == "--projects" and index + 1 < len(rest):
                result["projects"] = [
                    item.strip() for item in rest[index + 1].split(",") if item.strip()
                ]
                index += 2
            elif rest[index] == "--description" and index + 1 < len(rest):
                result["description"] = rest[index + 1]
                index += 2
            else:
                return None
        if not result.get("projects"):
            return None
        return result

    if cmd_name in ("list-modules", "list-runtimes"):
        if not rest:
            return None
        result["project"] = rest[0]
        return result

    if cmd_name in ("start", "stop", "restart"):
        if not rest:
            return None
        result["project"] = rest[0]
        i = 1
        if i < len(rest) and not rest[i].startswith("--"):
            result["module"] = rest[i]
            i += 1
        while i < len(rest):
            if rest[i] == "--wait":
                result["wait"] = True
                i += 1
            elif rest[i] == "--timeout" and i + 1 < len(rest):
                try:
                    result["timeout"] = int(rest[i + 1])
                except ValueError:
                    return None
                i += 2
            else:
                return None
        if result.get("wait") and "timeout" not in result:
            result["timeout"] = 60
        return result

    if cmd_name == "ensure-running":
        if not rest:
            return None
        result["project"] = rest[0]
        i = 1
        if i < len(rest) and not rest[i].startswith("--"):
            result["module"] = rest[i]
            i += 1
        while i < len(rest):
            if rest[i] == "--timeout" and i + 1 < len(rest):
                try:
                    result["timeout"] = int(rest[i + 1])
                except ValueError:
                    return None
                i += 2
            else:
                return None
        if "timeout" not in result:
            result["timeout"] = 60
        return result

    if cmd_name == "health":
        if not rest:
            return None
        result["project"] = rest[0]
        i = 1
        if i < len(rest) and not rest[i].startswith("--"):
            result["module"] = rest[i]
            i += 1
        while i < len(rest):
            if rest[i] == "--timeout" and i + 1 < len(rest):
                try:
                    result["timeout"] = int(rest[i + 1])
                except ValueError:
                    return None
                i += 2
            else:
                return None
        if "timeout" not in result:
            result["timeout"] = 60
        return result

    if cmd_name == "compile":
        if not rest:
            return None
        result["project"] = rest[0]
        i = 1
        while i < len(rest):
            if rest[i] == "--timeout" and i + 1 < len(rest):
                try:
                    result["timeout"] = int(rest[i + 1])
                except ValueError:
                    return None
                i += 2
            else:
                return None
        if "timeout" not in result:
            # 冷编译 Gradle/Maven 动辄数十秒到几分钟，给宽松默认值
            result["timeout"] = 300
        return result

    if cmd_name == "log":
        if not rest:
            return None
        result["project"] = rest[0]
        i = 1
        if i < len(rest) and not rest[i].startswith("--"):
            result["module"] = rest[i]
            i += 1
        while i < len(rest):
            if rest[i] == "--tail" and i + 1 < len(rest):
                try:
                    result["tail"] = int(rest[i + 1])
                except ValueError:
                    return None
                i += 2
            elif rest[i] == "--errors":
                result["errors"] = True
                i += 1
            elif rest[i] == "--all-modules":
                result["all_modules"] = True
                i += 1
            else:
                return None
        if "tail" not in result:
            result["tail"] = 50
        return result

    if cmd_name == "diagnose":
        if not rest:
            return None
        result["project"] = rest[0]
        i = 1
        if i < len(rest) and not rest[i].startswith("--"):
            result["module"] = rest[i]
            i += 1
        while i < len(rest):
            if rest[i] == "--tail" and i + 1 < len(rest):
                try:
                    result["tail"] = int(rest[i + 1])
                except ValueError:
                    return None
                i += 2
            else:
                return None
        if "tail" not in result:
            result["tail"] = 120
        return result

    if cmd_name in ("git-status", "git-diff", "git-ai-context"):
        if not rest:
            return None
        result["project"] = rest[0]
        i = 1
        while i < len(rest):
            if rest[i] == "--full":
                result["mode"] = "full"
                i += 1
            elif rest[i] == "--summary":
                result["mode"] = "summary"
                i += 1
            elif rest[i] == "--max-chars" and i + 1 < len(rest):
                try:
                    result["max_chars"] = int(rest[i + 1])
                except ValueError:
                    return None
                i += 2
            else:
                return None
        if "mode" not in result:
            result["mode"] = "summary"
        return result

    return None


def _response_timeout_ms(cmd: dict) -> int:
    """按命令生命周期设置 IPC 等待时间。"""
    action = cmd["cmd"]
    if action == "quit":
        return 120000
    if action in (
        "health", "compile", "ensure-running",
        "create-development-workspace", "workspace-review", "workspace-delete-check",
        "workspace-commit-push", "workspace-sync",
    ) or action in ("start", "stop", "restart"):
        defaults = {
            "start": 60, "stop": 60, "restart": 60,
            "create-development-workspace": 600,
            "workspace-commit-push": 600,
            "workspace-sync": 600,
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

    cmd = _parse_args(filtered_argv)
    if cmd is None:
        _safe_write(sys.stderr, json.dumps({"ok": False, "error": "invalid arguments"}) + "\n")
        _print_usage()
        return EXIT_BAD_ARGS

    if cmd["cmd"] == "version":
        from src.core.app_version import build_info
        _safe_write(sys.stdout, json.dumps({"ok": True, **build_info()}, ensure_ascii=False) + "\n")
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
                _safe_write(sys.stderr, json.dumps({"ok": False, "error": "mini-ide started but IPC not ready"}) + "\n")
                return EXIT_NOT_RUNNING
        else:
            _safe_write(sys.stderr, json.dumps({"ok": False, "error": "mini-ide is not running"}) + "\n")
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
            _safe_write(sys.stderr, json.dumps({"ok": False, "error": "timeout waiting for response"}) + "\n")
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
        _safe_write(
            sys.stderr,
            json.dumps({"ok": False, "error": response_error}) + "\n",
        )
        return EXIT_FAIL

    _safe_write(sys.stdout, json.dumps(result, ensure_ascii=False) + "\n")

    if isinstance(result, dict) and result.get("ok") is False:
        error = result.get("error", "")
        if "timeout" in error:
            return EXIT_TIMEOUT
        if "not found" in error:
            return EXIT_BAD_ARGS
        return EXIT_FAIL
    return EXIT_OK


def _print_usage():
    _safe_write(
        sys.stderr,
        "Usage: mini-ide <command> [args...]\n"
        "Commands:\n"
        "  --status                     Snapshot IDE/projects/services/workspaces\n"
        "  --version                    Show this CLI package version without connecting to GUI\n"
        "  --list-projects              List all open projects\n"
        "  --list-modules <project>     List modules of a project\n"
        "  --list-runtimes <project>    List runtime units of a project\n"
        "  --open <path>                Open a project path in the running IDE\n"
        "  --resolve-target <path>      Resolve a path to its owning environment/component\n"
        "  --close <project>            Stop services and close a project tab\n"
        "  --list-workspaces            List aggregate development workspaces\n"
        "  --open-workspace <target>    Open one workspace inside its aggregate tab\n"
        "  --close-workspace            Return the current aggregate tab to source projects\n"
        "  --list-aggregates            List aggregate project definitions\n"
        "  --open-aggregate <target>    Open one aggregate project tab\n"
        "  --create-development-workspace <aggregate> <name> --projects a,b [--description text]\n"
        "  --workspace-review <target>\n"
        "  --workspace-commit-push <target> [--message text]\n"
        "                               Commit changes and push task branches to origin\n"
        "  --workspace-sync <target> [--fetch] [--keep-conflicts]\n"
        "                               Merge the source base branch into task branches\n"
        "  --workspace-delete-check <target>\n"
        "  --start <project> [module]   Start a module\n"
        "  --stop <project> [module]    Stop a module\n"
        "  --restart <project> [module] Restart a module\n"
        "  --ensure-running <project> [module] [--timeout N]  Start if needed and wait\n"
        "  --health <project> [module] [--timeout N]  Wait for startup\n"
        "  --compile <project>          Trigger compilation\n"
        "  --log <project> [module] [--tail N] [--errors] [--all-modules]\n"
        "  --diagnose <project> [module] [--tail N]  Get service/log diagnosis\n"
        "  --git-status <project>       Get git branch and changed files\n"
        "  --git-diff <project> [--summary|--full] [--max-chars N]\n"
        "  --git-ai-context <project> [--summary|--full] [--max-chars N]\n"
        "  --preflight-build [project]  Check target project, or all projects if omitted\n"
        "  --can-quit                   Check all running services before quitting\n"
        "  --quit                       Stop all running services and exit mini-ide\n"
        "\nOptions:\n"
        "  --auto-start                 If mini-ide is not running, launch it automatically\n"
        "                               (ignored for --status / --preflight-build / --quit)\n"
    )
