"""冒烟测试：所有模块能 import + theme 之外没有 hex 颜色 hardcode

第二项（hex 扫描）是 CLAUDE.md 硬约束 15 的强制执行：
任何 widget 文件里出现 #abcdef 这种字面量都会让冒烟失败。
颜色必须来自 src/ui/theme.py 的 token。
"""
import re
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 不论从哪 cwd 跑，都把项目根加到 sys.path
sys.path.insert(0, str(ROOT))

modules = [
    "src.core.config",
    "src.core.project_detector",
    "src.core.runtime_units",
    "src.core.process_runner",
    "src.core.launch_tracker",
    "src.core.file_actions",
    "src.core.file_preview_model",
    "src.core.git_context",
    "src.core.path_utils",
    "src.core.target_resolver",
    "src.core.aggregate_workspace",
    "src.core.aggregate_workspace_manager",
    "src.core.development_workspace_service",
    "src.core.service_state",
    "src.core.tool_launchers",
    "src.core.workspace_manager",
    "src.core.log_classifier",
    "src.core.git_worker",
    "src.core.external_detector",
    "src.core.nginx_detector",
    "src.ui.theme",
    "src.ui.styles",
    "src.ui.log_widget",
    "src.ui.project_service_controller",
    "src.ui.settings_panel",
    "src.ui.service_panel",
    "src.ui.project_tab",
    "src.ui.empty_state",
    "src.ui.aggregate_project_dialog",
    "src.ui.aggregate_project_tab",
    "src.ui.development_workspace_dialog",
    "src.ui.workspace_review_dialog",
    "src.ui.file_tree",
    "src.ui.file_preview",
    "src.ui.syntax_highlighter",
    "src.core.file_index",
    "src.core.controller_index",
    "src.ui.quick_open",
    "src.ui.content_search",
    "src.core.git_ops",
    "src.ui.git_viewer",
    "src.ui.main_window",
    "src.ui.toast",
    "src.util.editor",
    "src.util.git_executable",
    "src.util.git_info",
    "src.util.notify",
]


def import_check() -> list[str]:
    failed: list[str] = []
    for m in modules:
        try:
            __import__(m)
            print(f"[OK]   {m}", flush=True)
        except Exception as e:
            print(f"[FAIL] {m}: {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()
            failed.append(m)
    return failed


def service_state_check() -> list[str]:
    """轻量验证服务状态模型的 CLI 兼容输出。"""
    from src.core.service_state import (
        KIND_MODULE, STATE_RUNNING_EXTERNAL, STATE_STOPPED,
        ServiceState, running_items,
    )

    failed: list[str] = []
    stopped = ServiceState(
        project="p", module="m", kind=KIND_MODULE, state=STATE_STOPPED,
    )
    external = ServiceState(
        project="p", module="m", kind=KIND_MODULE,
        state=STATE_RUNNING_EXTERNAL, pid=1234, port=8080, ports=[8080, 8443],
    )
    cli_stopped = stopped.to_cli_module()
    cli_external = external.to_cli_module()
    if cli_stopped["state"] != "stopped" or cli_stopped["external"]:
        failed.append("stopped service should keep legacy stopped output")
    if cli_external["state"] != "running" or not cli_external["external"]:
        failed.append("external service should keep legacy running output")
    if cli_external["ports"] != [8080, 8443]:
        failed.append("service state should expose all detected ports")
    if running_items([stopped, external]) != [external.to_running_item()]:
        failed.append("running_items should filter inactive services")

    if failed:
        for msg in failed:
            print(f"[FAIL] service_state: {msg}", flush=True)
    else:
        print("[OK]   service_state compatibility", flush=True)
    return failed


def nginx_detector_check() -> list[str]:
    """验证 nginx 目录识别和配置端口解析。"""
    import tempfile

    from src.core.nginx_detector import detect_nginx, is_nginx_dir
    from src.core.project_detector import detect_project

    failed: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "nginx.exe").write_bytes(b"")
        (root / "nginx.conf").write_text(
            "events {}\nhttp {\n"
            "  server { listen 9888; listen 0.0.0.0:9443 ssl; }\n"
            "}\n",
            encoding="utf-8",
        )
        if not is_nginx_dir(root):
            failed.append("nginx dir should require nginx.exe and nginx.conf")
        meta = detect_project(str(root))
        if meta.project_type != "nginx" or meta.display_type != "Nginx":
            failed.append(f"nginx dir should be detected as nginx, got {meta.project_type}")
        status = detect_nginx(root, {})
        if status.configured_ports != [9443, 9888]:
            failed.append(f"nginx configured ports mismatch: {status.configured_ports}")

    if failed:
        for msg in failed:
            print(f"[FAIL] nginx_detector: {msg}", flush=True)
    else:
        print("[OK]   nginx_detector rules", flush=True)
    return failed


def cli_parse_check() -> list[str]:
    """验证公开 CLI 的关键参数能解析成稳定 JSON 命令。"""
    from src.core.cli_client import _parse_args, _response_timeout_ms

    cases = [
        (["mini-ide.exe", "--status"], {"cmd": "status"}),
        (["mini-ide.exe", "--open", r"E:\demo"], {"cmd": "open", "path": r"E:\demo"}),
        (["mini-ide.exe", "--resolve-target", r"E:\demo"],
         {"cmd": "resolve-target", "path": r"E:\demo"}),
        (["mini-ide.exe", "--close", "server"], {"cmd": "close", "project": "server"}),
        (["mini-ide.exe", "--list-workspaces"], {"cmd": "list-workspaces"}),
        (["mini-ide.exe", "--list-runtimes", "webapp-lerna"],
         {"cmd": "list-runtimes", "project": "webapp-lerna"}),
        (["mini-ide.exe", "--open-workspace", "race", "dev"], {"cmd": "open-workspace", "target": "race dev"}),
        (["mini-ide.exe", "--close-workspace"], {"cmd": "close-workspace"}),
        (["mini-ide.exe", "--list-aggregates"], {"cmd": "list-aggregates"}),
        (["mini-ide.exe", "--open-aggregate", "race"],
         {"cmd": "open-aggregate", "target": "race"}),
        (["mini-ide.exe", "--create-development-workspace", "race", "certificate",
          "--projects", "server,webapp-tenant", "--description", "证书功能"],
         {"cmd": "create-development-workspace", "aggregate": "race",
          "name": "certificate", "projects": ["server", "webapp-tenant"],
          "description": "证书功能"}),
        (["mini-ide.exe", "--workspace-delete-check", "certificate"],
         {"cmd": "workspace-delete-check", "target": "certificate"}),
        (["mini-ide.exe", "--workspace-commit-push", "certificate"],
         {"cmd": "workspace-commit-push", "target": "certificate"}),
        (["mini-ide.exe", "--workspace-commit-push", "certificate", "--message", "backup"],
         {"cmd": "workspace-commit-push", "target": "certificate", "message": "backup"}),
        (["mini-ide.exe", "--workspace-sync", "certificate"],
         {"cmd": "workspace-sync", "target": "certificate"}),
        (["mini-ide.exe", "--workspace-sync", "certificate", "--fetch",
          "--keep-conflicts"],
         {"cmd": "workspace-sync", "target": "certificate",
          "fetch_remote": True, "keep_conflicts": True}),
        (["mini-ide.exe", "--start", "server", "gateway", "--wait", "--timeout", "90"],
         {"cmd": "start", "project": "server", "module": "gateway", "wait": True, "timeout": 90}),
        (["mini-ide.exe", "--ensure-running", "server", "gateway", "--timeout", "120"],
         {"cmd": "ensure-running", "project": "server", "module": "gateway", "timeout": 120}),
        (["mini-ide.exe", "--log", "server", "--all-modules", "--errors", "--tail", "200"],
         {"cmd": "log", "project": "server", "all_modules": True, "errors": True, "tail": 200}),
        (["mini-ide.exe", "--diagnose", "server", "gateway"],
         {"cmd": "diagnose", "project": "server", "module": "gateway", "tail": 120}),
        (["mini-ide.exe", "--git-diff", "server", "--full", "--max-chars", "12000"],
         {"cmd": "git-diff", "project": "server", "mode": "full", "max_chars": 12000}),
        (["mini-ide.exe", "--preflight-build"], {"cmd": "preflight-build"}),
        (["mini-ide.exe", "--preflight-build", "server"],
         {"cmd": "preflight-build", "project": "server"}),
        (["mini-ide.exe", "--can-quit"], {"cmd": "preflight-build"}),
    ]

    failed: list[str] = []
    for argv, expected in cases:
        actual = _parse_args(argv)
        if actual != expected:
            failed.append(f"{argv[1]} parsed as {actual!r}, expected {expected!r}")
    if _parse_args(["mini-ide.exe", "--unknown"]) is not None:
        failed.append("unknown command should fail parsing")
    if _parse_args(["mini-ide.exe", "--workspace-sync", "certificate", "--push"]) is not None:
        failed.append("workspace-sync must reject unknown flags")
    if _parse_args(["mini-ide.exe", "--workspace-sync"]) is not None:
        failed.append("workspace-sync must require a target")
    if _parse_args(["mini-ide.exe", "--workspace-commit-push"]) is not None:
        failed.append("workspace-commit-push must require a target")
    if _parse_args([
        "mini-ide.exe", "--workspace-commit-push", "certificate", "--message", "",
    ]) is not None:
        failed.append("workspace-commit-push must reject an empty message")
    if _parse_args(["mini-ide.exe", "--preflight-build", "server", "extra"]) is not None:
        failed.append("preflight-build must accept at most one project target")
    if _parse_args(["mini-ide.exe", "--resolve-target"]) is not None:
        failed.append("resolve-target must require a path")
    if _parse_args(["mini-ide.exe", "--resolve-target", "one", "two"]) is not None:
        failed.append("resolve-target must accept exactly one path")
    for action in (
        "create-development-workspace", "workspace-commit-push", "workspace-sync",
    ):
        if _response_timeout_ms({"cmd": action}) < 605000:
            failed.append(f"{action} should allow a complete workspace operation")
    if _parse_args(["mini-ide.exe", "--start-profile", "race"]) is not None:
        failed.append("removed runtime profile commands must not remain public")

    if failed:
        for msg in failed:
            print(f"[FAIL] cli_parse: {msg}", flush=True)
    else:
        print("[OK]   cli_parse compatibility", flush=True)
    return failed


def preflight_isolation_check() -> list[str]:
    """指定项目的构建预检不能被其它项目的运行服务阻塞。"""
    from types import SimpleNamespace
    from unittest.mock import patch

    from src.core import cli_server

    failed: list[str] = []
    target = SimpleNamespace(
        project_meta=SimpleNamespace(name="jl"),
        running_service_items=lambda **_kwargs: [],
    )
    unrelated = [{"project": "codex-desktop", "state": "running"}]
    with (
        patch.object(cli_server, "_find_project_tab", return_value=(target, None)),
        patch.object(cli_server, "_collect_running", return_value=unrelated),
    ):
        targeted = cli_server._cmd_preflight_build(object(), "jl")
        global_result = cli_server._cmd_preflight_build(object())

    if not targeted.get("ok") or not targeted.get("can_build"):
        failed.append(f"targeted preflight should ignore unrelated services: {targeted!r}")
    if global_result.get("ok") or global_result.get("can_build"):
        failed.append("global preflight must still block while any project is running")

    running_target = SimpleNamespace(
        project_meta=SimpleNamespace(name="jl"),
        running_service_items=lambda **_kwargs: [
            {"project": "jl", "state": "running"}
        ],
    )
    with patch.object(
        cli_server, "_find_project_tab", return_value=(running_target, None)
    ):
        blocked = cli_server._cmd_preflight_build(object(), "jl")
    if blocked.get("ok") or blocked.get("can_build"):
        failed.append("targeted preflight must block its own running services")

    if failed:
        for msg in failed:
            print(f"[FAIL] preflight_isolation: {msg}", flush=True)
    else:
        print("[OK]   targeted preflight isolation", flush=True)
    return failed


def cli_output_encoding_check() -> list[str]:
    """验证 CLI 输出中文时固定使用 UTF-8，并能兼容受限标准流。"""
    from src.core import cli_client

    class AsciiOnlyStream:
        def __init__(self) -> None:
            self.parts: list[str] = []
            self.fail_next = True

        def write(self, text: str) -> None:
            if self.fail_next:
                self.fail_next = False
                text.encode("cp1252")
            text.encode("ascii")
            self.parts.append(text)

        def flush(self) -> None:
            pass

    failed: list[str] = []

    class ReconfigurableStream:
        def __init__(self) -> None:
            self.encoding = "gbk"
            self.errors = "strict"
            self.data = bytearray()

        def reconfigure(self, *, encoding: str, errors: str) -> None:
            self.encoding = encoding
            self.errors = errors

        def write(self, text: str) -> None:
            self.data.extend(text.encode(self.encoding, errors=self.errors))

        def flush(self) -> None:
            pass

    utf8_stream = ReconfigurableStream()
    cli_client._configure_stream_utf8(utf8_stream)
    cli_client._safe_write(utf8_stream, '{"display_type": "Spring Boot (Gradle) · 多模块"}\n')
    try:
        utf8_output = bytes(utf8_stream.data).decode("utf-8")
    except UnicodeDecodeError as e:
        failed.append(f"configured CLI stream should emit UTF-8, got {e!r}")
    else:
        if "Spring Boot (Gradle) · 多模块" not in utf8_output:
            failed.append("configured CLI stream should preserve Chinese output")

    stream = AsciiOnlyStream()
    original_win_write = cli_client._win_write_std
    cli_client._win_write_std = lambda kind, text: False
    try:
        cli_client._safe_write(stream, '{"ok": true, "message": "中文✅"}\n')
    except UnicodeEncodeError as e:
        failed.append(f"_safe_write should swallow UnicodeEncodeError, got {e!r}")
    except Exception as e:
        failed.append(f"_safe_write should not raise, got {type(e).__name__}: {e}")
    finally:
        cli_client._win_write_std = original_win_write

    if not stream.parts:
        failed.append("_safe_write should fall back to escaped ASCII text")

    if failed:
        for msg in failed:
            print(f"[FAIL] cli_output_encoding: {msg}", flush=True)
    else:
        print("[OK]   cli_output_encoding fallback", flush=True)
    return failed


def external_launcher_check() -> list[str]:
    """验证桌面应用入口保留目标目录。"""
    import os
    import tempfile
    from urllib.parse import parse_qs, urlsplit

    from src.core import tool_launchers

    failed: list[str] = []
    target = Path(r"E:\race workspace\中文 & certificate#1%")
    original_executable = os.environ.get("AGENTDESK_EXE")
    with tempfile.TemporaryDirectory() as temporary:
        executable = Path(temporary) / "AgentDesk.exe"
        executable.touch()
        os.environ["AGENTDESK_EXE"] = str(executable)
        try:
            agentdesk_args = tool_launchers.open_in_agentdesk_args(target)
            codex_args = tool_launchers.open_in_codex_args(target)
            cc_args = tool_launchers.open_in_cc_args(target)
        finally:
            if original_executable is None:
                os.environ.pop("AGENTDESK_EXE", None)
            else:
                os.environ["AGENTDESK_EXE"] = original_executable

    base = [str(executable), "--", "--cwd", str(target)]
    if agentdesk_args != base:
        failed.append("AgentDesk launcher must separate Electron arguments and preserve the target directory")
    link = urlsplit(codex_args[-1])
    if (
        Path(codex_args[0]).name.lower() != "rundll32.exe"
        or codex_args[1] != "url.dll,FileProtocolHandler"
        or (link.scheme, link.netloc, link.path) != ("codex", "threads", "/new")
        or parse_qs(link.query) != {"path": [str(target)]}
        or link.fragment
    ):
        failed.append("codex entry must match open gpt and preserve the target directory")
    if cc_args != [*base, "--provider=claude"]:
        failed.append("cc entry must launch the Claude provider in AgentDesk")
    if tool_launchers.CREATE_NO_WINDOW != 0x08000000:
        failed.append("AgentDesk launcher subprocess must keep CREATE_NO_WINDOW")

    if failed:
        for msg in failed:
            print(f"[FAIL] external_launcher: {msg}", flush=True)
    else:
        print("[OK]   Desktop external launchers", flush=True)
    return failed


def git_executable_resolution_check() -> list[str]:
    """Verify Git for Windows launcher paths resolve to the real binary."""
    import tempfile

    from src.util import git_executable

    failed: list[str] = []
    if sys.platform != "win32":
        print("[OK]   git executable resolution (non-Windows skip)", flush=True)
        return failed

    original_which = git_executable.shutil.which
    with tempfile.TemporaryDirectory() as tmp:
        install_root = Path(tmp) / "Git"
        wrapper = install_root / "cmd" / "git.exe"
        real_git = install_root / "mingw64" / "bin" / "git.exe"
        wrapper.parent.mkdir(parents=True)
        real_git.parent.mkdir(parents=True)
        wrapper.touch()
        real_git.touch()
        try:
            git_executable.shutil.which = lambda _name: str(wrapper)
            git_executable.resolve_git_executable.cache_clear()
            resolved = git_executable.resolve_git_executable()
        finally:
            git_executable.shutil.which = original_which
            git_executable.resolve_git_executable.cache_clear()

    if Path(resolved) != real_git:
        failed.append(f"Git wrapper should resolve to the real binary: {resolved}")

    if failed:
        for msg in failed:
            print(f"[FAIL] git_executable: {msg}", flush=True)
    else:
        print("[OK]   Git executable resolution", flush=True)
    return failed


def aggregate_workspace_model_check() -> list[str]:
    """验证聚合项目/开发工作区模型、迁移和路径安全边界。"""
    import json
    import subprocess
    import tempfile

    from src.core.aggregate_workspace import (
        AggregateConfigError, AggregateProject, DevelopmentWorkspace,
        load_aggregate_project, load_development_workspace,
        save_aggregate_project, save_development_workspace,
    )
    from src.core.path_utils import PathBoundaryError, normalized_path_key
    from src.util.git_executable import resolve_git_executable

    failed: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "race"
        for name in ("server", "nginx"):
            (root / name).mkdir(parents=True)

        config = {
            "id": "race",
            "name": "race",
            "kind": "aggregate",
            "workspaceDirectory": "workspace",
            "components": [
                {"id": "server", "path": "server", "type": "java"},
                {
                    "id": "nginx", "path": "nginx", "type": "nginx",
                    "shared": True,
                },
            ],
            "profiles": [{
                "id": "daily",
                "name": "日常开发",
                "startGroups": [["server"], ["nginx"]],
            }],
        }
        project = AggregateProject.from_dict(root, config)
        if project.schema_version != 1:
            failed.append("unversioned aggregate draft should migrate to schemaVersion 1")
        if project.component("SERVER") is None:
            failed.append("stable component id lookup should be case-insensitive")
        if project.profiles[0].start_groups != (("server",), ("nginx",)):
            failed.append("runtime profile groups should preserve stage boundaries")

        config_path = save_aggregate_project(project)
        saved = json.loads(config_path.read_text(encoding="utf-8"))
        if saved.get("schemaVersion") != 1:
            failed.append("saved aggregate config should contain schemaVersion 1")
        loaded = load_aggregate_project(root)
        if loaded.to_dict() != project.to_dict():
            failed.append("aggregate config should round-trip without semantic changes")

        creationflags = 0x08000000 if sys.platform == "win32" else 0
        git_exe = resolve_git_executable()

        def git(*args: str) -> str:
            result = subprocess.run(
                [git_exe, *args],
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                creationflags=creationflags,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip())
            return result.stdout.strip()

        source_repo = root / "server"
        (source_repo / "README.md").write_text("seed\n", encoding="utf-8")
        git("init", "-b", "main", str(source_repo))
        git("-C", str(source_repo), "config", "user.name", "mini-ide smoke")
        git("-C", str(source_repo), "config", "user.email", "smoke@mini-ide.local")
        git("-C", str(source_repo), "add", "README.md")
        git("-C", str(source_repo), "commit", "-m", "seed")
        base_commit = git("-C", str(source_repo), "rev-parse", "HEAD")

        duplicate = dict(config)
        duplicate["components"] = list(config["components"]) + [
            {"id": "SERVER", "path": "server", "type": "java"},
        ]
        try:
            AggregateProject.from_dict(root, duplicate)
            failed.append("component ids must be unique case-insensitively")
        except AggregateConfigError:
            pass

        unknown_profile = dict(config)
        unknown_profile["profiles"] = [{
            "id": "bad", "name": "bad", "startGroups": [["missing"]],
        }]
        try:
            AggregateProject.from_dict(root, unknown_profile)
            failed.append("runtime profiles must not reference unknown components")
        except AggregateConfigError:
            pass

        escaped = dict(config)
        escaped["components"] = [
            {"id": "server", "path": "../outside", "type": "java"},
        ]
        try:
            AggregateProject.from_dict(root, escaped)
            failed.append("aggregate component path must not escape the project root")
        except PathBoundaryError:
            pass

        workspace_root = root / "workspace" / "certificate"
        workspace_root.mkdir(parents=True)
        git(
            "-C", str(source_repo), "worktree", "add", "-b",
            "feature/certificate-server", str(workspace_root / "server"),
            base_commit,
        )
        workspace_data = {
            "schemaVersion": 1,
            "id": "certificate",
            "name": "certificate",
            "createdAt": "2026-07-29T18:00:00+08:00",
            "aggregateProjectPath": str(root),
            "status": "active",
            "components": [
                {
                    "id": "server",
                    "sourceRepositoryPath": str(root / "server"),
                    "worktreePath": "server",
                    "baseBranch": "main",
                    "baseCommit": base_commit,
                    "taskBranch": "feature/certificate-server",
                    "mode": "worktree",
                },
                {
                    "id": "nginx",
                    "sourceRepositoryPath": str(root / "nginx"),
                    "mode": "shared",
                },
            ],
            "lastProfileId": "daily",
            "runtimeStateRef": "race/certificate",
        }
        workspace = DevelopmentWorkspace.from_dict(
            workspace_root, workspace_data, aggregate_project=project,
        )
        server = next(item for item in workspace.components if item.id == "server")
        if normalized_path_key(server.worktree_path) != normalized_path_key(
            workspace_root / "server"
        ):
            failed.append("worktree path should be normalized inside the task root")
        if not (Path(server.worktree_path) / ".git").is_file():
            failed.append("temporary Git fixture should use a real linked Worktree")

        workspace_path = save_development_workspace(
            workspace, aggregate_project=project,
        )
        if not workspace_path.is_file():
            failed.append("development workspace config should be written atomically")
        reloaded_workspace = load_development_workspace(
            workspace_root, aggregate_project=project,
        )
        if reloaded_workspace.to_dict() != workspace.to_dict():
            failed.append("development workspace config should round-trip")

        escaped_workspace = dict(workspace_data)
        escaped_components = [dict(item) for item in workspace_data["components"]]
        escaped_components[0]["worktreePath"] = "../event/server"
        escaped_workspace["components"] = escaped_components
        try:
            DevelopmentWorkspace.from_dict(
                workspace_root, escaped_workspace, aggregate_project=project,
            )
            failed.append("worktree path must not escape the task root")
        except PathBoundaryError:
            pass

    if failed:
        for msg in failed:
            print(f"[FAIL] aggregate_workspace: {msg}", flush=True)
    else:
        print("[OK]   aggregate workspace models and path boundaries", flush=True)
    return failed


def development_workspace_lifecycle_check() -> list[str]:
    """用真实临时 Git 仓库验证创建、失败回滚、Review 和收尾删除。"""
    from dataclasses import replace
    import subprocess
    import tempfile

    from src.core.aggregate_workspace import AggregateProject, load_development_workspace
    from src.core import development_workspace_service as service
    from src.util.git_executable import resolve_git_executable

    failed: list[str] = []
    creationflags = 0x08000000 if sys.platform == "win32" else 0
    git_exe = resolve_git_executable()

    def git(*args: str) -> str:
        result = subprocess.run(
            [git_exe, *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", creationflags=creationflags,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        return result.stdout.strip()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "suite"
        repo_one = root / "server"
        repo_two = root / "webapp"
        shared = root / "nginx"
        bare_remote = Path(tmp) / "server-origin.git"
        for repo in (repo_one, repo_two):
            repo.mkdir(parents=True)
            (repo / "README.md").write_text(f"{repo.name}\n", encoding="utf-8")
            git("init", "-b", "main", str(repo))
            git("-C", str(repo), "config", "user.name", "mini-ide smoke")
            git("-C", str(repo), "config", "user.email", "smoke@mini-ide.local")
            git("-C", str(repo), "add", "README.md")
            git("-C", str(repo), "commit", "-m", "seed")
        (repo_one / ".gitignore").write_text(".env\n", encoding="utf-8")
        (repo_one / ".env").write_text("DEV_TAG=smoke\n", encoding="utf-8")
        git("-C", str(repo_one), "add", ".gitignore")
        git("-C", str(repo_one), "commit", "-m", "ignore local env")
        git("init", "--bare", str(bare_remote))
        git("-C", str(repo_one), "remote", "add", "origin", str(bare_remote))
        git("-C", str(repo_one), "push", "--set-upstream", "origin", "main")
        shared.mkdir(parents=True)
        project = AggregateProject.from_dict(root, {
            "schemaVersion": 1,
            "id": "suite",
            "name": "suite",
            "kind": "aggregate",
            "workspaceDirectory": "workspace",
            "components": [
                {
                    "id": "server", "path": "server", "type": "java",
                    "workspaceCopyFiles": [".env"],
                },
                {
                    "id": "webapp", "path": "webapp", "type": "frontend",
                    "runtime": {"units": [
                        {"id": "teacher", "name": "教师端", "cwd": ".",
                         "start": ["tool", "teacher"], "expectedPort": 8881},
                        {"id": "student", "name": "学生端", "cwd": ".",
                         "start": ["tool", "student"], "expectedPort": 8882},
                    ]},
                },
                {"id": "nginx", "path": "nginx", "type": "nginx", "shared": True},
            ],
            "profiles": [{
                "id": "daily", "name": "日常开发",
                "startGroups": [["server", "webapp"], ["nginx"]],
            }],
        })
        try:
            AggregateProject.from_dict(root, {
                "schemaVersion": 1,
                "id": "invalid-suite",
                "name": "invalid-suite",
                "kind": "aggregate",
                "workspaceDirectory": "workspace",
                "components": [{
                    "id": "server", "path": "server", "type": "java",
                    "workspaceCopyFiles": ["../outside"],
                }],
                "profiles": [],
            })
            failed.append("workspaceCopyFiles must reject paths outside the component")
        except ValueError:
            pass

        remote_task_branch = "feature/remote-conflict-server"
        git(
            "-C", str(repo_one), "update-ref",
            f"refs/remotes/origin/{remote_task_branch}", "HEAD",
        )
        try:
            service.build_workspace_creation_plan(
                project, "remote-conflict", "远端分支冲突", ["server"],
            )
            failed.append("cached remote task branch must block workspace creation")
        except service.WorkspaceOperationError:
            pass
        finally:
            git(
                "-C", str(repo_one), "update-ref", "-d",
                f"refs/remotes/origin/{remote_task_branch}",
            )

        if service.workspace_id_from_name("直播系统对接") != "zhiboxitongduijie":
            failed.append("Chinese workspace name should become a readable pinyin id")
        if service.workspace_id_from_name("Live 2.0") != "live-2.0":
            failed.append("ASCII workspace name should remain readable and normalized")
        try:
            service.workspace_id_from_name("😀")
            failed.append("workspace name without a usable id must be rejected")
        except service.WorkspaceOperationError:
            pass

        # 主工作目录有未提交内容不阻止从明确 HEAD 创建 Worktree。
        (repo_one / "LOCAL_ONLY.txt").write_text("dirty source\n", encoding="utf-8")
        plan = service.build_workspace_creation_plan(
            project, "证书任务", "证书任务", ["server"],
        )
        if plan.workspace_id != "zhengshurenwu":
            failed.append(f"workspace plan should use pinyin id: {plan.workspace_id}")
        if plan.components[0].task_branch != "feature/zhengshurenwu-server":
            failed.append(
                f"task branch should use pinyin id: {plan.components[0].task_branch}"
            )
        result = service.create_development_workspace(plan)
        if not result.ok or result.workspace is None:
            failed.append(f"workspace creation should succeed: {result.error}")
        else:
            workspace = result.workspace
            if not (Path(workspace.root_path) / "server" / ".git").is_file():
                failed.append("selected Git component should be a linked Worktree")
            copied_env = Path(workspace.root_path) / "server" / ".env"
            if not copied_env.is_file() or copied_env.read_text(encoding="utf-8") != "DEV_TAG=smoke\n":
                failed.append("workspaceCopyFiles should copy the configured local file")
            loaded_server = next(
                item for item in load_development_workspace(
                    workspace.root_path, aggregate_project=project,
                ).components if item.id == "server"
            )
            if loaded_server.workspace_copy_files != (".env",):
                failed.append("workspaceCopyFiles should round-trip in workspace metadata")
            if (Path(workspace.root_path) / "webapp").exists():
                failed.append("unselected repository must not be copied")
            if (Path(workspace.root_path) / "nginx").exists():
                failed.append("shared component must not be copied")
            agents = (Path(workspace.root_path) / "AGENTS.md").read_text(encoding="utf-8")
            required_agents_text = (
                "证书任务", project.root_path, plan.components[0].source_path,
                plan.components[0].base_branch,
                plan.components[0].base_commit, plan.components[0].task_branch,
                plan.components[0].target_path, "webapp", "nginx", "AGENTS.md", "mini-ide",
                "mine-dev-flow", "AI 必须自动读取并主持", "不需要主动输入 Skill 名称",
                ".mine-dev-flow/state.json",
            )
            if any(text not in agents for text in required_agents_text):
                failed.append(
                    "task AGENTS.md should preserve source, base, branch, merge and edit boundaries"
                )
            if workspace.description != "证书任务":
                failed.append("workspace description should round-trip")

            untracked = Path(workspace.root_path) / "server" / "UNTRACKED.txt"
            untracked.write_text("block delete\n", encoding="utf-8")
            delete_plan = service.inspect_workspace_delete(workspace)
            if not delete_plan.can_delete:
                failed.append(
                    "dirty Worktree must not block deletion after merge/push checks: "
                    f"{delete_plan.blockers!r}"
                )
            untracked.unlink()

            worktree = Path(workspace.root_path) / "server"
            (worktree / "README.md").write_text("feature\n", encoding="utf-8")
            git("-C", str(worktree), "add", "README.md")
            git("-C", str(worktree), "commit", "-m", "feature")
            review = service.review_development_workspace(workspace)
            if len(review) != 1 or not review[0].commits:
                failed.append("workspace Review should include task commits by component")
            elif any(
                value != "未记录"
                for value in (
                    review[0].compile_result,
                    review[0].health_result,
                    review[0].test_result,
                )
            ):
                failed.append("unrecorded verification results must be explicit")

            push_calls = []
            original_run_git = service._run_git

            def fail_first_push(cwd, args, timeout=30):
                if args and args[0] == "push":
                    push_calls.append(str(cwd))
                    return 1, "", "injected push failure"
                return original_run_git(cwd, args, timeout)

            two_component_workspace = replace(
                workspace,
                components=(
                    workspace.components[0],
                    replace(workspace.components[0], id="later"),
                ),
            )
            service._run_git = fail_first_push
            try:
                stopped_push = service.commit_and_push_development_workspace(
                    two_component_workspace,
                )
            finally:
                service._run_git = original_run_git
            if stopped_push.ok or len(push_calls) != 1:
                failed.append("push failure must stop later workspace projects")
            elif len(stopped_push.components) != 2 or "未执行" not in stopped_push.components[1].error:
                failed.append("stopped workspace projects should report that they were not run")

            # 已有本地提交、没有新改动时也应推送并建立 upstream。
            pushed_result = service.commit_and_push_development_workspace(workspace)
            if not pushed_result.ok or not pushed_result.components[0].pushed:
                failed.append(
                    "workspace commit and push should back up local commits: "
                    f"{pushed_result.error}"
                )
            elif pushed_result.components[0].committed:
                failed.append("commit and push should not create an empty commit")
            upstream = git(
                "-C", str(worktree), "rev-parse", "--abbrev-ref",
                "--symbolic-full-name", "@{upstream}",
            )
            if upstream != f"origin/{workspace.components[0].task_branch}":
                failed.append(f"workspace push should set upstream, got {upstream!r}")

            pushed_file = worktree / "PUSHED.txt"
            pushed_file.write_text("backup\n", encoding="utf-8")
            committed_push = service.commit_and_push_development_workspace(
                workspace, "backup workspace",
            )
            if not committed_push.ok or not committed_push.components[0].committed:
                failed.append(
                    "workspace commit and push should commit dirty files: "
                    f"{committed_push.error}"
                )
            elif git("-C", str(worktree), "log", "-1", "--format=%s") != "backup workspace":
                failed.append("workspace commit and push should use the requested message")
            remote_head = git(
                "--git-dir", str(bare_remote), "rev-parse",
                f"refs/heads/{workspace.components[0].task_branch}",
            )
            if remote_head != git("-C", str(worktree), "rev-parse", "HEAD"):
                failed.append("workspace remote branch should match the local task branch")
            reviewed_workspace = load_development_workspace(
                workspace.root_path, aggregate_project=project,
            )
            if reviewed_workspace.status != "reviewing":
                failed.append("Review should move an active workspace to reviewing")

            # 源基准分支前进后，任务分支必须能安全同步回来。
            (repo_one / "BASE.md").write_text("base update\n", encoding="utf-8")
            git("-C", str(repo_one), "add", "BASE.md")
            git("-C", str(repo_one), "commit", "-m", "base update")
            behind = service.workspace_behind_counts(workspace)
            if behind.get("server") != 1:
                failed.append(f"behind count should follow the base branch: {behind!r}")
            sync = service.sync_development_workspace(workspace)
            if not sync.ok or "server" not in sync.synced_ids:
                failed.append(f"clean sync should merge base into task branch: {sync.error}")
            if not (worktree / "BASE.md").is_file():
                failed.append("sync should bring base branch files into the Worktree")
            if not (repo_one / "LOCAL_ONLY.txt").is_file():
                failed.append("sync must not touch the dirty source checkout")
            synced_workspace = load_development_workspace(
                workspace.root_path, aggregate_project=project,
            )
            base_main = git("-C", str(repo_one), "rev-parse", "main")
            synced_server = next(
                item for item in synced_workspace.components if item.id == "server"
            )
            if synced_server.base_commit != base_main:
                failed.append("sync should record the new base commit")
            if service.workspace_behind_counts(synced_workspace).get("server") != 0:
                failed.append("synced task branch should not stay behind")

            # GUI 组合按钮应先保留本地改动、固定获取远端基准分支，再统一推送。
            combined_local = worktree / "COMBINED_LOCAL.txt"
            combined_local.write_text("local change\n", encoding="utf-8")
            combined_base = repo_one / "COMBINED_BASE.txt"
            combined_base.write_text("base change\n", encoding="utf-8")
            git("-C", str(repo_one), "add", "COMBINED_BASE.txt")
            git("-C", str(repo_one), "commit", "-m", "combined base update")
            git("-C", str(repo_one), "push", "origin", "main")
            remote_writer = Path(tmp) / "server-upstream"
            git("clone", "--branch", "main", str(bare_remote), str(remote_writer))
            git("-C", str(remote_writer), "config", "user.name", "mini-ide smoke")
            git(
                "-C", str(remote_writer), "config", "user.email",
                "smoke@mini-ide.local",
            )
            (remote_writer / "REMOTE_BASE.txt").write_text(
                "remote base update\n", encoding="utf-8",
            )
            git("-C", str(remote_writer), "add", "REMOTE_BASE.txt")
            git("-C", str(remote_writer), "commit", "-m", "remote base update")
            git("-C", str(remote_writer), "push", "origin", "main")
            combined = service.sync_commit_and_push_development_workspace(
                synced_workspace, "combined workspace",
            )
            if not combined.ok:
                failed.append(f"combined sync and push should succeed: {combined.error}")
            elif not (
                combined.components[0].committed
                and combined.components[0].synced
                and combined.components[0].pushed
            ):
                failed.append(
                    "combined sync and push should commit, sync and push the component"
                )
            if not all((
                combined_local.is_file(),
                (worktree / "COMBINED_BASE.txt").is_file(),
                (worktree / "REMOTE_BASE.txt").is_file(),
            )):
                failed.append(
                    "combined sync and push should keep local changes and fetch remote base"
                )
            combined_remote_head = git(
                "--git-dir", str(bare_remote), "rev-parse",
                f"refs/heads/{workspace.components[0].task_branch}",
            )
            if combined_remote_head != git("-C", str(worktree), "rev-parse", "HEAD"):
                failed.append("combined sync and push should update the remote task branch")
            synced_workspace = load_development_workspace(
                workspace.root_path, aggregate_project=project,
            )
            git("-C", str(repo_one), "merge", "--ff-only", "origin/main")

            # 冲突默认回滚；用户确认后才保留冲突。
            (repo_one / "README.md").write_text("server v2\n", encoding="utf-8")
            git("-C", str(repo_one), "add", "README.md")
            git("-C", str(repo_one), "commit", "-m", "base conflict")
            remote_before_conflict = git(
                "--git-dir", str(bare_remote), "rev-parse",
                f"refs/heads/{workspace.components[0].task_branch}",
            )
            combined_conflict = service.sync_commit_and_push_development_workspace(
                synced_workspace,
            )
            if combined_conflict.ok or not combined_conflict.needs_conflict_confirmation:
                failed.append("combined sync conflict must ask before keeping conflicts")
            if any(item.pushed for item in combined_conflict.components):
                failed.append("combined sync conflict must stop all pushes")
            remote_after_conflict = git(
                "--git-dir", str(bare_remote), "rev-parse",
                f"refs/heads/{workspace.components[0].task_branch}",
            )
            if remote_after_conflict != remote_before_conflict:
                failed.append("combined sync conflict must not move the remote task branch")
            declined = service.sync_development_workspace(synced_workspace)
            if declined.ok or not declined.needs_conflict_confirmation:
                failed.append("conflicting sync must ask before keeping conflicts")
            if not all(item.rolled_back for item in declined.components if item.conflicted):
                failed.append("declined conflict sync should roll back the Worktree")
            if service._run_git(str(worktree), ["status", "--porcelain"], 15)[1]:
                failed.append("rolled-back sync must leave the Worktree clean")
            kept = service.sync_development_workspace(
                synced_workspace, keep_conflicts=True,
            )
            if not kept.ok or "server" not in kept.conflicted_ids:
                failed.append(f"keep-conflicts sync should hold conflicts: {kept.error}")
            if service._run_git(
                str(worktree), ["rev-parse", "--verify", "MERGE_HEAD"], 10,
            )[0] != 0:
                failed.append("kept conflicts should leave the merge in progress")
            blocked_conflict_push = (
                service.sync_commit_and_push_development_workspace(synced_workspace)
            )
            if (
                blocked_conflict_push.ok
                or "未解决冲突" not in blocked_conflict_push.error
                or any(item.pushed for item in blocked_conflict_push.components)
            ):
                failed.append(
                    "combined sync and push must block unresolved kept conflicts"
                )
            (worktree / "README.md").write_text("resolved\n", encoding="utf-8")
            git("-C", str(worktree), "add", "README.md")
            git("-C", str(worktree), "commit", "--no-edit")
            healed = service.sync_development_workspace(synced_workspace)
            if not healed.ok or not all(
                item.already_current for item in healed.components
            ):
                failed.append("sync after manual resolution should report already current")
            healed_server = next(
                item for item in load_development_workspace(
                    workspace.root_path, aggregate_project=project,
                ).components if item.id == "server"
            )
            if healed_server.base_commit != git("-C", str(repo_one), "rev-parse", "main"):
                failed.append("sync should heal a stale base commit after resolution")
            workspace = load_development_workspace(
                workspace.root_path, aggregate_project=project,
            )

            merge_dirty = worktree / "MERGE_DIRTY.txt"
            merge_dirty.write_text("auto commit\n", encoding="utf-8")
            if not (repo_one / "LOCAL_ONLY.txt").is_file():
                failed.append("workspace creation and Review must preserve source dirty files")
            base_head = git("-C", str(repo_one), "rev-parse", "HEAD")
            task_head = git("-C", str(worktree), "rev-parse", "HEAD")
            blocked_merge = service.merge_development_workspace(workspace)
            if blocked_merge.ok or "源目录有未提交" not in blocked_merge.error:
                failed.append("workspace merge must block a dirty source checkout")
            if git("-C", str(repo_one), "rev-parse", "HEAD") != base_head:
                failed.append("blocked workspace merge must not move the base branch")
            if git("-C", str(worktree), "rev-parse", "HEAD") != task_head:
                failed.append("source preflight failure must not commit task changes")
            (repo_one / "LOCAL_ONLY.txt").unlink()

            git("-C", str(repo_one), "branch", "--unset-upstream", "main")
            missing_upstream = service.merge_development_workspace(workspace)
            if missing_upstream.ok or "未配置上游" not in missing_upstream.error:
                failed.append("workspace merge must require a base branch upstream")
            if git("-C", str(repo_one), "rev-parse", "HEAD") != base_head:
                failed.append("missing upstream must not move the base branch")
            if git("-C", str(worktree), "rev-parse", "HEAD") != task_head:
                failed.append("missing upstream must not commit task changes")
            git(
                "-C", str(repo_one), "branch", "--set-upstream-to",
                "origin/main", "main",
            )

            original_run_git = service._run_git

            def fail_auto_commit(cwd, args, timeout=30):
                if args[:2] == ["commit", "-m"]:
                    return 1, "", "injected commit failure"
                return original_run_git(cwd, args, timeout)

            service._run_git = fail_auto_commit
            try:
                commit_failed = service.merge_development_workspace(workspace)
            finally:
                service._run_git = original_run_git
            if commit_failed.ok or "自动提交失败" not in commit_failed.error:
                failed.append("auto-commit failure must stop workspace merge")
            if git("-C", str(repo_one), "rev-parse", "HEAD") != base_head:
                failed.append("auto-commit failure must not move the base branch")

            original_run_git = service._run_git

            def fail_merge_push(cwd, args, timeout=30):
                if args and args[0] == "push":
                    return 1, "", "injected base push failure"
                return original_run_git(cwd, args, timeout)

            service._run_git = fail_merge_push
            try:
                merge_push_failed = service.merge_development_workspace(workspace)
            finally:
                service._run_git = original_run_git
            if merge_push_failed.ok or "推送" not in merge_push_failed.error:
                failed.append("base push failure must make workspace merge incomplete")
            elif not merge_push_failed.components[0].merged or merge_push_failed.components[0].pushed:
                failed.append("base push failure should retain local merge without marking pushed")
            if git("-C", str(repo_one), "rev-parse", "HEAD") == base_head:
                failed.append("base push failure should retain the local base merge")

            merge_result = service.merge_development_workspace(workspace)
            if not merge_result.ok or not all(item.merged for item in merge_result.components):
                failed.append(f"dirty workspace should auto-commit and merge: {merge_result.error}")
            elif git("-C", str(worktree), "log", "-1", "--format=%s") != workspace.name:
                failed.append("workspace merge should use the workspace name as commit message")
            elif git("-C", str(worktree), "status", "--porcelain"):
                failed.append("auto-committed Worktree should be clean")
            merged_workspace = load_development_workspace(
                workspace.root_path, aggregate_project=project,
            )
            if merged_workspace.status != "merged":
                failed.append("successful workspace merge should mark the workspace merged")
            if not merge_result.components or not all(
                item.merged and item.pushed for item in merge_result.components
            ):
                failed.append("successful workspace merge should push every base branch")
            delete_plan = service.inspect_workspace_delete(workspace)
            if not delete_plan.can_delete:
                failed.append(
                    "merged and pushed workspace should pass deletion preflight: "
                    f"{delete_plan.blockers}"
                )

            unknown = Path(workspace.root_path) / ".mine-dev-flow" / "reviews"
            unknown.mkdir(parents=True)
            (unknown / "report.md").write_text("keep no more\n", encoding="utf-8")
            delete_plan = service.inspect_workspace_delete(workspace)
            if not delete_plan.can_delete:
                failed.append(f"merged clean workspace should be deletable: {delete_plan.blockers!r}")
            else:
                # An occupied directory must not prevent local or remote branch
                # cleanup, even while the Worktree files still exist.
                from unittest.mock import patch
                with patch.object(service.shutil, "rmtree", side_effect=PermissionError("occupied directory")):
                    occupied_ok, occupied_kept, occupied_error = service.delete_development_workspace(delete_plan)
                if occupied_ok or occupied_kept or not Path(workspace.root_path).exists():
                    failed.append(f"occupied directory result incorrect: {occupied_error}; {occupied_kept!r}")
                for component in workspace.components:
                    if component.mode != "worktree":
                        continue
                    if service._run_git(component.source_repository_path,
                        ["show-ref", "--verify", "--quiet", f"refs/heads/{component.task_branch}"], 10)[0] != 1:
                        failed.append(f"occupied directory retained local branch: {component.task_branch}")
                    if service._run_git(component.source_repository_path,
                        ["ls-remote", "--heads", "origin", f"refs/heads/{component.task_branch}"], 30)[1]:
                        failed.append(f"occupied directory retained remote branch: {component.task_branch}")
                    # Restore only the temporary fixture's branches for the
                    # normal deletion case below; the Worktree is now detached.
                    for args in (["branch", component.task_branch, "HEAD"],
                                 ["push", "origin", component.task_branch]):
                        restore_code, restore_out, restore_err = service._run_git(component.worktree_path, args, 30)
                        if restore_code:
                            failed.append(f"delete fixture restoration failed: {restore_err or restore_out}")
                ok, kept, error = service.delete_development_workspace(delete_plan)
                if not ok or error or Path(workspace.root_path).exists():
                    failed.append(f"safe workspace deletion failed: {error}; kept={kept!r}")
                elif copied_env.exists():
                    failed.append("workspace deletion should remove managed copied files with the Worktree")
                elif service._run_git(
                    repo_one,
                    ["show-ref", "--verify", "--quiet", f"refs/heads/{workspace.components[0].task_branch}"],
                    10,
                )[0] == 0:
                    failed.append("workspace deletion should remove the local task branch")
                elif service._run_git(
                    repo_one,
                    ["ls-remote", "--heads", "origin", f"refs/heads/{workspace.components[0].task_branch}"],
                    30,
                )[1]:
                    failed.append("workspace deletion should remove the remote task branch")
        # 第二个仓库创建失败时，只回滚本次已创建且仍干净的 Worktree。
        partial_plan = service.build_workspace_creation_plan(
            project, "partial", "失败回滚", ["server", "webapp"],
        )
        original_run_git = service._run_git

        def fail_second(cwd, args, timeout=30):
            if args[:2] == ["worktree", "add"] and str(args[4]).endswith("webapp"):
                return 1, "", "injected failure"
            return original_run_git(cwd, args, timeout)

        service._run_git = fail_second
        try:
            partial = service.create_development_workspace(partial_plan)
        finally:
            service._run_git = original_run_git
        if partial.ok or "server" not in partial.rolled_back_components:
            failed.append(f"partial failure should roll back the first clean Worktree: {partial!r}")
        if Path(partial_plan.root_path).exists():
            failed.append("fully rolled-back partial workspace root should be removed")

    if failed:
        for msg in failed:
            print(f"[FAIL] development_workspace_lifecycle: {msg}", flush=True)
    else:
        print("[OK]   development workspace create/review/delete lifecycle", flush=True)
    return failed


def aggregate_runtime_ui_check() -> list[str]:
    """验证一个聚合目录 Tab 内含项目和需求工作区，且项目单独启停。"""
    import tempfile
    import time
    from types import SimpleNamespace

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QFrame, QPushButton, QScrollArea

    from src.core.aggregate_workspace import AggregateProject
    from src.core.cli_server import _find_project_tab
    from src.core.config import AppConfig, ProjectEntry
    from src.core.path_utils import normalized_path_key
    from src.core.project_detector import detect_project
    from src.ui.aggregate_project_tab import AggregateProjectTab, _WorkspaceCard
    from src.ui.project_tab import ProjectTab

    failed: list[str] = []
    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "suite"
        (root / "server").mkdir(parents=True)
        (root / "webapp").mkdir(parents=True)
        project = AggregateProject.from_dict(root, {
            "schemaVersion": 1,
            "id": "suite",
            "name": "suite",
            "kind": "aggregate",
            "workspaceDirectory": "workspace",
            "components": [
                {"id": "server", "path": "server", "type": "java"},
                {
                    "id": "webapp", "path": "webapp", "type": "frontend",
                    "runtime": {"units": [
                        {"id": "teacher", "name": "教师端", "cwd": ".",
                         "start": ["tool", "teacher"], "expectedPort": 8881},
                        {"id": "student", "name": "学生端", "cwd": ".",
                         "start": ["tool", "student"], "expectedPort": 8882},
                    ]},
                },
            ],
            "profiles": [],
        })
        old_meta = detect_project(str(root / "webapp"))
        old_webapp_tab = ProjectTab(
            old_meta, ProjectEntry(path=old_meta.path), AppConfig(),
        )
        tab = AggregateProjectTab(
            project, AppConfig(),
            existing_tabs={
                normalized_path_key(root / "webapp"): old_webapp_tab,
            },
        )
        deadline = time.time() + 5
        while tab._prepare_worker is not None and time.time() < deadline:
            worker = tab._prepare_worker
            app.processEvents()
            if worker is not None:
                worker.wait(50)
        app.processEvents()
        if tab.project_meta.project_type != "aggregate":
            failed.append("aggregate root must open as aggregate environment, not generic project")
        if tab.project_tree.topLevelItemCount() != 2 or set(tab.component_tabs) != {"server", "webapp"}:
            failed.append("aggregate tab must keep all projects inside one top-level workbench")
        configured_webapp = tab.component_tabs.get("webapp")
        if configured_webapp is old_webapp_tab:
            failed.append("configured aggregate component must not reuse stale project metadata")
        elif configured_webapp is not None and [
            unit.id for unit in configured_webapp.project_meta.runtime_units
        ] != ["teacher", "student"]:
            failed.append("aggregate runtime config must override reused tab detection")
        original_rows = tab.project_tree.topLevelItemCount()
        original_components = set(tab.component_tabs)
        tab._projects_prepared(
            [dict(tab._metadata["server"])], tab._prepare_generation - 1,
        )
        if (
            tab.project_tree.topLevelItemCount() != original_rows
            or set(tab.component_tabs) != original_components
        ):
            failed.append("stale project discovery must not append rows after switching environments")
        webapp_node = tab._project_nodes.get("webapp")
        if webapp_node is None or webapp_node.childCount() != 0:
            failed.append("aggregate project navigation must stay flat and omit duplicate runtime rows")
        else:
            tab.project_tree.setCurrentItem(webapp_node)
            app.processEvents()
            if tab.detail_stack.currentWidget() is not configured_webapp:
                failed.append("selecting a project must show its internal runtime controls")
        if tab.project_splitter.orientation() != Qt.Orientation.Horizontal:
            failed.append("aggregate projects must use a horizontal sidebar/detail layout")
        if tab.view_tabs.count() != 2 or tab.view_tabs.tabText(1) != "需求工作区":
            failed.append("aggregate tab must contain its own workspace panel")
        if tab.detail_stack.count() != 3:
            failed.append("project details should remain internal to the aggregate tab")
        if (
            tab.project_codex_button.text() != "codex"
            or tab.project_cc_button.text() != "cc"
        ):
            failed.append("project codex and cc entries must keep their labels short")
        if not all(
            button.toolTip()
            for button in (
                tab.project_codex_button, tab.project_cc_button,
            )
        ):
            failed.append("project codex and cc labels must keep an explanatory tooltip")
        if tab._current_environment_root() != Path(project.root_path):
            failed.append("project tools must open the aggregate root in source mode")
        if hasattr(tab, "enter_workspace_button") or hasattr(tab, "review_button"):
            failed.append("workspace toolbar must rely on double-click and omit Review")
        if not isinstance(tab.workspace_scroll, QScrollArea):
            failed.append("workspace panel must use a scrollable card layout")
        if hasattr(tab, "workspace_table"):
            failed.append("workspace panel must no longer use the old table")
        if tab.workspace_cards_layout.count() < 1:
            failed.append("workspace card container must preserve its trailing stretch")
        card_summary = SimpleNamespace(
            name="certificate",
            error="",
            git_change_count=0,
            component_ids=("server",),
            task_branches=("feature/certificate-server",),
            created_at="2026-09-02",
        )
        card = _WorkspaceCard(card_summary, False)
        card_buttons = [button.text() for button in card.findChildren(QPushButton)]
        if "同步并推送" not in card_buttons:
            failed.append("workspace card must expose the combined sync and push action")
        if "提交并推送" in card_buttons or "同步源分支" in card_buttons:
            failed.append("workspace card must remove the two separate legacy actions")
        card.deleteLater()
        double_clicks: list[bool] = []
        tab._activate_selected_workspace = lambda: double_clicks.append(True)
        fake_summary = SimpleNamespace(
            root_path="workspace", workspace=SimpleNamespace(root_path="workspace"),
        )
        tab._activate_workspace_summary(fake_summary)
        if double_clicks != [True]:
            failed.append("activating a workspace card must enter that workspace")
        if not tab.exit_workspace_button.isHidden():
            failed.append("exit workspace action must stay hidden in aggregate source mode")
        workspace_root = root / "workspace" / "certificate"
        tab.workspace = SimpleNamespace(
            root_path=str(workspace_root),
            name="certificate",
        )
        tab._set_context_label()
        if tab.exit_workspace_button.isHidden():
            failed.append("active workspace must expose its exit action in the header")
        if "当前需求：certificate" != tab.context_label.text():
            failed.append("aggregate header must identify the active workspace")
        if tab._current_environment_root() != workspace_root:
            failed.append("project tools must open the active workspace root")
        tab.workspace = None
        tab._set_context_label()

        class TopTabs:
            def count(self):
                return 1

            def widget(self, _index):
                return tab

        matched, match_error = _find_project_tab(
            SimpleNamespace(tabs=TopTabs()), "server",
        )
        if matched is not tab._project_tabs.get("server") or match_error is not None:
            failed.append("project CLI must resolve projects inside aggregate tabs")

        # 单独启动一个项目时不能连带启动其它项目。
        original_tabs = tab._project_tabs
        tab._refresh_timer.stop()
        attempts: list[str] = []

        class FakeController:
            def __init__(self, component_id: str):
                self.component_id = component_id

            def start(self):
                attempts.append(self.component_id)
                return {"ok": True}

        class FakeTab:
            _is_multi_module = False

            def __init__(self, component_id: str):
                self.project_meta = SimpleNamespace(name=component_id)
                self.service_controller = FakeController(component_id)

            def service_states(self, refresh_external=True):
                return []

        tab._project_tabs = {
            "server": FakeTab("server"),
            "webapp": FakeTab("webapp"),
        }
        tab.start_component("server")
        if attempts != ["server"]:
            failed.append(f"starting one project must not start another project: {attempts!r}")
        tab._project_tabs = original_tabs
        if not tab.request_close(confirm_running=False, stop_running=True, quiet=True):
            failed.append("aggregate tab should close all internal component tabs safely")
        tab.deleteLater()
        app.processEvents()

    if failed:
        for msg in failed:
            print(f"[FAIL] aggregate_runtime_ui: {msg}", flush=True)
    else:
        print("[OK]   aggregate single-tab runtime workbench", flush=True)
    return failed


def aggregate_dashboard_check() -> list[str]:
    """验证固定管理页快照会保留有效项、临时工作区和配置错误。"""
    import tempfile

    from src.core.aggregate_workspace import (
        AggregateProject, DevelopmentWorkspace, save_aggregate_project,
        save_development_workspace,
    )
    from src.core.aggregate_workspace_manager import build_workspace_dashboard_snapshot
    from src.core.config import AppConfig

    failed: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "race"
        nginx = root / "nginx"
        nginx.mkdir(parents=True)
        project = AggregateProject.from_dict(root, {
            "schemaVersion": 1,
            "id": "race",
            "name": "race",
            "kind": "aggregate",
            "workspaceDirectory": "workspace",
            "components": [{
                "id": "nginx", "path": "nginx", "type": "nginx", "shared": True,
            }],
            "profiles": [],
        })
        save_aggregate_project(project)

        task_root = project.workspace_root / "certificate"
        task_root.mkdir(parents=True)
        workspace = DevelopmentWorkspace.from_dict(task_root, {
            "schemaVersion": 1,
            "id": "certificate",
            "name": "certificate",
            "createdAt": "2026-07-30T10:00:00+08:00",
            "aggregateProjectPath": str(root),
            "status": "created",
            "components": [{
                "id": "nginx",
                "sourceRepositoryPath": str(nginx),
                "mode": "shared",
            }],
        }, aggregate_project=project)
        save_development_workspace(workspace, aggregate_project=project)

        config = AppConfig()
        config.register_aggregate_project(str(root))
        config.register_aggregate_project(str(root))
        if len(config.aggregate_project_paths) != 1:
            failed.append("aggregate project registry must deduplicate normalized paths")

        missing = root / "missing"
        snapshot = build_workspace_dashboard_snapshot([
            *config.aggregate_project_paths, str(missing),
        ])
        valid = [item for item in snapshot.aggregate_projects if item.project is not None]
        invalid = [item for item in snapshot.aggregate_projects if item.error]
        if len(valid) != 1 or valid[0].component_count != 1:
            failed.append("dashboard must load registered aggregate definitions")
        if len(invalid) != 1 or invalid[0].root_path != str(missing):
            failed.append("dashboard must surface invalid registered definitions")
        if len(snapshot.development_workspaces) != 1:
            failed.append("dashboard must discover workspace.json under workspaceDirectory")
        elif snapshot.development_workspaces[0].component_ids != ("nginx",):
            failed.append("development workspace summary must preserve component ids")

    if failed:
        for msg in failed:
            print(f"[FAIL] aggregate_dashboard: {msg}", flush=True)
    else:
        print("[OK]   aggregate workspace dashboard snapshot", flush=True)
    return failed


def legacy_workspace_migration_check() -> list[str]:
    """验证旧 WorkspaceEntry 显式迁移不会改写原记录，并提示遗漏目录。"""
    import tempfile

    from src.core.aggregate_workspace import save_aggregate_project
    from src.core.aggregate_workspace_manager import (
        build_legacy_workspace_candidate, build_workspace_dashboard_snapshot,
    )
    from src.core.config import AppConfig, WorkspaceEntry

    failed: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "race"
        server = root / "server"
        webapp = root / "webapp"
        (server / ".git").mkdir(parents=True)
        webapp.mkdir(parents=True)
        (webapp / "package.json").write_text(
            '{"dependencies":{"vue":"3"}}', encoding="utf-8",
        )
        legacy = WorkspaceEntry(name="race", paths=[str(server)])
        original_paths = list(legacy.paths)

        candidate = build_legacy_workspace_candidate(legacy)
        if candidate.error or candidate.project is None:
            failed.append(f"legacy workspace should produce a migration candidate: {candidate.error}")
        elif [item.id for item in candidate.project.components] != ["server"]:
            failed.append("legacy paths must map to stable component ids without additions")
        if str(webapp.resolve()) not in candidate.omitted_candidates:
            failed.append("sibling project omitted by legacy data must be surfaced")
        if legacy.paths != original_paths:
            failed.append("candidate creation must not mutate WorkspaceEntry paths")

        config = AppConfig(workspaces=[legacy])
        if candidate.project is not None:
            save_aggregate_project(candidate.project)
            config.register_aggregate_project(candidate.project.root_path)
            config.mark_legacy_workspace_migrated("race", candidate.project.root_path)
        if config.workspaces != [legacy]:
            failed.append("migration records must not remove legacy WorkspaceEntry")

        snapshot = build_workspace_dashboard_snapshot(
            config.aggregate_project_paths,
            config.workspaces,
            config.legacy_workspace_migrations,
        )
        if len(snapshot.legacy_workspaces) != 1:
            failed.append("dashboard must list every legacy workspace")
        elif snapshot.legacy_workspaces[0].status != "migrated":
            failed.append("confirmed migration should remain auditable in the dashboard")

    if failed:
        for msg in failed:
            print(f"[FAIL] legacy_workspace_migration: {msg}", flush=True)
    else:
        print("[OK]   legacy WorkspaceEntry migration preservation", flush=True)
    return failed


def aggregate_definition_management_check() -> list[str]:
    """验证新建、注册和编辑聚合项目定义的纯逻辑往返。"""
    import tempfile

    from src.core.aggregate_workspace import (
        AggregateProject, load_aggregate_project, save_aggregate_project,
    )
    from src.core.aggregate_workspace_manager import (
        component_definition_from_path, suggest_stable_id,
    )
    from src.core.config import AppConfig
    from src.ui.main_window import _is_aggregate_directory

    failed: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "race suite"
        component_root = root / "webapp-tenant"
        component_root.mkdir(parents=True)
        (component_root / "package.json").write_text(
            '{"dependencies":{"vue":"3"}}', encoding="utf-8",
        )
        if suggest_stable_id(root.name) != "race-suite":
            failed.append("new aggregate definitions need a valid suggested stable id")

        component = component_definition_from_path(root, component_root)
        if component.id != "webapp-tenant" or component.type != "frontend":
            failed.append("selected component detection must preserve id and semantic type")
        project = AggregateProject.from_dict(root, {
            "schemaVersion": 1,
            "id": suggest_stable_id(root.name),
            "name": "race suite",
            "kind": "aggregate",
            "workspaceDirectory": "workspace",
            "components": [component.to_dict()],
            "profiles": [],
        })
        save_aggregate_project(project)

        edited = AggregateProject.from_dict(root, {
            **project.to_dict(),
            "name": "race suite edited",
        })
        save_aggregate_project(edited)
        loaded = load_aggregate_project(root)
        if loaded.name != "race suite edited" or loaded.components != edited.components:
            failed.append("edited aggregate definition must round-trip without data loss")

        config = AppConfig()
        config.register_aggregate_project(str(root))
        if config.aggregate_project_paths != [str(root.resolve())]:
            failed.append("opening a definition must register its canonical root path")
        if not config.is_aggregate_project(str(root)):
            failed.append("aggregate classification must come from the explicit registry")
        config.unregister_aggregate_project(str(root))
        if config.is_aggregate_project(str(root)) or not loaded.config_path.is_file():
            failed.append("ordinary classification must preserve the existing aggregate definition")
        if not _is_aggregate_directory(config, root):
            failed.append("a valid on-disk aggregate definition must override stale ordinary history")

    if failed:
        for msg in failed:
            print(f"[FAIL] aggregate_definition_management: {msg}", flush=True)
    else:
        print("[OK]   aggregate definition create/open/edit", flush=True)
    return failed


def workspace_tab_session_check() -> list[str]:
    """验证移除固定管理页后兼容旧活动 Tab 索引。"""
    from src.ui.main_window import _normalized_tab_session, _restored_tab_index

    failed: list[str] = []
    old_entries = [r"project:E:\whaty\project\race\server"]
    new_entries = ["workspace:management", *old_entries]
    if _restored_tab_index(old_entries, 0) != 0:
        failed.append("project-only session index must stay unchanged")
    if _restored_tab_index(new_entries, 1) != 0:
        failed.append("old fixed workspace tab must be removed from the saved index")

    import tempfile
    from src.core.aggregate_workspace import AggregateProject, save_aggregate_project
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "suite"
        (root / "server").mkdir(parents=True)
        project = AggregateProject.from_dict(root, {
            "schemaVersion": 1, "id": "suite", "name": "suite",
            "kind": "aggregate", "workspaceDirectory": "workspace",
            "components": [{"id": "server", "path": "server", "type": "java"}],
            "profiles": [],
        })
        save_aggregate_project(project)
        normalized, index = _normalized_tab_session([
            f"project:{root / 'server'}", f"aggregate:{root}",
        ], 0, [str(root)])
        if normalized != [f"aggregate:{root.resolve()}"] or index != 0:
            failed.append("legacy aggregate component tabs must restore as one aggregate tab")

    if failed:
        for msg in failed:
            print(f"[FAIL] workspace_tab_session: {msg}", flush=True)
    else:
        print("[OK]   removed workspace tab session compatibility", flush=True)
    return failed


def cli_project_match_check() -> list[str]:
    """验证 CLI 项目匹配不会把 webapp 错配到 webapp-tenant。"""
    import json
    from types import SimpleNamespace

    from src.core.cli_server import _find_project_tab, _match_target
    from src.ui import project_tab as project_tab_module

    failed: list[str] = []

    class FakeProjectTab:
        pass

    def tab(path: str, component_id: str = ""):
        item = FakeProjectTab()
        item.component_id = component_id
        item.project_meta = SimpleNamespace(
            path=path,
            name="time-track-webapp",
            project_type="vue",
        )
        return item

    webapp = tab(r"E:\whaty\project\race\webapp")
    tenant = tab(r"E:\whaty\project\race\webapp-tenant", "tenant-ui")
    mobile = tab(r"E:\whaty\project\race\webapp-m")
    items = [tenant, webapp, mobile]

    class FakeTabs:
        def count(self) -> int:
            return len(items)

        def widget(self, index: int):
            return items[index]

    window = SimpleNamespace(tabs=FakeTabs())

    original_project_tab = project_tab_module.ProjectTab
    project_tab_module.ProjectTab = FakeProjectTab
    try:
        matched, error = _find_project_tab(window, "webapp")
        if matched is not webapp or error is not None:
            failed.append("exact directory name must win over earlier fuzzy matches")

        matched, error = _find_project_tab(
            window, r"e:/whaty/project/race/WEBAPP-TENANT/",
        )
        if matched is not tenant or error is not None:
            failed.append("normalized full path should match case-insensitively")

        matched, error = _find_project_tab(window, "tenant-ui")
        if matched is not tenant or error is not None:
            failed.append("stable component id should match exactly")

        matched, error = _find_project_tab(window, "webapp-")
        if matched is not None or not error or error.get("code") != "ambiguous_project":
            failed.append("non-unique fuzzy match must return an ambiguity error")
        elif len(error.get("candidates", [])) != 2:
            failed.append("ambiguity error must include the complete candidate list")

        matched, error = _find_project_tab(window, "missing")
        if matched is not None or not error or error.get("code") != "project_not_found":
            failed.append("unknown target must return project_not_found")

        matched, error = _match_target([
            {"id": "race-a", "name": "race", "path": r"E:\race-a", "project": object()},
            {"id": "race-b", "name": "race", "path": r"E:\race-b", "project": object()},
        ], "race", "aggregate")
        if matched is not None or not error or len(error.get("candidates", [])) != 2:
            failed.append("aggregate ambiguity must preserve the candidate list")
        else:
            try:
                json.dumps(error)
            except TypeError:
                failed.append("ambiguity candidates must not expose internal model objects")
    finally:
        project_tab_module.ProjectTab = original_project_tab

    if failed:
        for msg in failed:
            print(f"[FAIL] cli_project_match: {msg}", flush=True)
    else:
        print("[OK]   cli_project_match determinism", flush=True)
    return failed


def file_action_check() -> list[str]:
    """验证文件树粘贴命名和安全边界的纯逻辑。"""
    import tempfile

    from src.core.file_actions import copy_destination, is_same_or_child, paste_paths

    failed: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "target"
        target.mkdir()
        src_file = root / "note.md"
        src_file.write_text("a", encoding="utf-8")
        existing = target / "note.md"
        existing.write_text("b", encoding="utf-8")
        if copy_destination(target, src_file).name != "note - 副本.md":
            failed.append("copy_destination should use Explorer-like duplicate name")

        src_dir = root / "dir"
        child_dir = src_dir / "child"
        child_dir.mkdir(parents=True)
        if not is_same_or_child(child_dir, src_dir):
            failed.append("is_same_or_child should identify child directory")
        changed, errors = paste_paths([src_dir], child_dir, move=False)
        if changed != 0 or not errors:
            failed.append("paste_paths should block copying a directory into its child")

    if failed:
        for msg in failed:
            print(f"[FAIL] file_actions: {msg}", flush=True)
    else:
        print("[OK]   file_actions rules", flush=True)
    return failed


def file_preview_model_check() -> list[str]:
    """验证预览核心判断：编码、行尾、注释前缀。"""
    from src.core.file_preview_model import (
        detect_encoding, detect_newline, image_suffix_format, line_comment_prefix,
        should_wrap_text,
    )

    failed: list[str] = []
    text, enc = detect_encoding("中文".encode("gbk"))
    if text != "中文" or enc != "gbk":
        failed.append("detect_encoding should preserve GBK text")
    if detect_newline("a\r\nb\r\n") != "\r\n":
        failed.append("detect_newline should detect CRLF")
    if line_comment_prefix("Demo.java") != "//":
        failed.append("line_comment_prefix should map Java to //")
    if line_comment_prefix("Dockerfile") != "#":
        failed.append("line_comment_prefix should map Dockerfile to #")
    if not should_wrap_text("readme.md"):
        failed.append("markdown should use soft wrap")
    if image_suffix_format("icon.PNG") != "png":
        failed.append("image_suffix_format should normalize image suffix")

    if failed:
        for msg in failed:
            print(f"[FAIL] file_preview_model: {msg}", flush=True)
    else:
        print("[OK]   file_preview_model rules", flush=True)
    return failed


def launch_and_operation_guard_check() -> list[str]:
    """验证旧启动日志隔离、代次切换以及项目级编译/启停互斥。"""
    from types import SimpleNamespace

    from src.core.launch_tracker import LaunchTracker
    from src.core.process_runner import ProcessRunner, RunContext
    from src.ui.project_service_controller import ProjectServiceController

    failed: list[str] = []
    tracker = LaunchTracker("spring-boot-gradle")
    tracker.record_output("application", "Started OldApplication in 1.0 seconds")
    first = tracker.begin("application")
    state = tracker.state("application", first)
    if state is None or state.ready:
        failed.append("a marker emitted before begin must not satisfy a new launch")
    tracker.record_output("application", "preparing context")
    if tracker.state("application", first).ready:
        failed.append("non-ready output must not mark launch ready")
    tracker.record_output("application", "Started NewApplication in 2.0 seconds")
    if not tracker.state("application", first).ready:
        failed.append("a marker emitted after begin should satisfy that generation")
    second = tracker.begin("application")
    if not tracker.is_superseded("application", first):
        failed.append("a newer restart must supersede the previous generation")
    if tracker.state("application", second).ready:
        failed.append("new generation must not inherit the previous ready marker")
    gateway = tracker.begin("gateway")
    tracker.record_output("application", "Started NewerApplication")
    if tracker.state("gateway", gateway).ready:
        failed.append("module launch readiness must be independent")

    real_runner = ProcessRunner()
    for busy_state in ("starting", "running", "stopping"):
        real_runner._state = busy_state
        if real_runner.start(["must-not-run"], RunContext(cwd=str(ROOT))):
            failed.append(f"ProcessRunner must reject reentry while {busy_state}")
    real_runner._state = "idle"

    class FakeRunner:
        def __init__(self, state: str = "idle"):
            self._state = state

        def state(self) -> str:
            return self._state

        def is_running(self) -> bool:
            return self._state == "running"

        def stop_cleanup_pending(self) -> bool:
            return False

    profile = SimpleNamespace(kind="compile", name="compile")

    class FakeTab:
        _is_multi_module = True
        project_meta = SimpleNamespace(
            project_type="spring-boot-gradle",
            name="demo",
            spring_boot_modules=[("application", "", None, "")],
        )
        runner = FakeRunner()
        module_runners = {"application": FakeRunner("running")}
        _module_ports: dict[str, int] = {}
        _module_external_pids: dict[str, int] = {}
        _current_profile = None

        def _detect_and_apply_external(self) -> None:
            pass

        def _find_compile_profile(self):
            return profile

    tab = FakeTab()
    controller = ProjectServiceController(tab)
    conflict = controller.prepare_compile()
    if conflict.get("code") != "services_running":
        failed.append(f"compile must reject a managed running module, got {conflict!r}")

    tab.module_runners["application"]._state = "idle"
    tab._module_external_pids["application"] = 1234
    conflict = controller.prepare_compile()
    if conflict.get("code") != "services_running":
        failed.append(f"compile must reject an external running module, got {conflict!r}")

    tab._module_external_pids.clear()
    prepared = controller.prepare_compile()
    if not prepared.get("ok"):
        failed.append(f"compile should reserve when all services are stopped: {prepared!r}")
    start_conflict = controller.service_start_error()
    if start_conflict is None or start_conflict.get("code") != "build_in_progress":
        failed.append("start/restart must be blocked while compile is reserved")
    controller.abort_prepared_compile()

    tab.runner._state = "running"
    tab._current_profile = profile
    start_conflict = controller.service_start_error()
    if start_conflict is None or start_conflict.get("code") != "build_in_progress":
        failed.append("start/restart must be blocked while project build runner is active")

    import os
    from src.core import process_runner
    from src.core.runtime_units import RuntimeUnit

    api_unit = RuntimeUnit(
        id="api", name="api", kind="python", cwd=str(ROOT),
        start_profile="api:start", profile_names=("api:start",),
        expected_port=9000,
    )

    class HealthRunner(FakeRunner):
        def process_id(self):
            return os.getpid()

    class HealthTab:
        _is_multi_module = True
        project_meta = SimpleNamespace(
            project_type="python", name="health-demo", runtime_units=[api_unit],
            spring_boot_modules=[],
            runtime_unit=lambda unit_id: api_unit if unit_id == "api" else None,
        )
        runner = FakeRunner()
        module_runners = {"api": HealthRunner("running")}
        _module_ports: dict[str, int] = {}
        _module_external_pids: dict[str, int] = {}

        def _detect_external_modules(self):
            raise AssertionError("non-Spring port health must not use Spring detection")

    health_controller = ProjectServiceController(HealthTab())
    generation = health_controller.begin_launch("api")
    health_controller.launch_started(
        "api", generation, health_controller.tab.module_runners["api"],
    )
    original_find_port_holder = process_runner.find_port_holder
    process_runner.find_port_holder = lambda port: (
        [{"pid": os.getpid()}] if port == 9000 else []
    )
    try:
        healthy, detail = health_controller.module_health_ok("api", generation)
    finally:
        process_runner.find_port_holder = original_find_port_holder
    if not healthy or detail.get("source") != "port":
        failed.append(f"declared non-Spring port must satisfy health: {detail!r}")

    if failed:
        for msg in failed:
            print(f"[FAIL] launch_guard: {msg}", flush=True)
    else:
        print("[OK]   launch generation and operation guard", flush=True)
    return failed


def compile_wait_check() -> list[str]:
    """验证 compile 在启动进程前挂监听，且只有真实 finished 才响应成功。"""
    from types import SimpleNamespace

    from PySide6.QtNetwork import QLocalSocket

    from src.core import cli_server

    failed: list[str] = []

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback) -> None:
            self.callbacks.append(callback)

        def disconnect(self, callback) -> None:
            if callback not in self.callbacks:
                raise TypeError("not connected")
            self.callbacks.remove(callback)

        def emit(self, *args) -> None:
            for callback in list(self.callbacks):
                callback(*args)

    class FakeTimer:
        def __init__(self):
            self.timeout = FakeSignal()
            self.running = False

        def setSingleShot(self, _value) -> None:
            pass

        def setInterval(self, _value) -> None:
            pass

        def start(self) -> None:
            self.running = True

        def stop(self) -> None:
            self.running = False

        def deleteLater(self) -> None:
            pass

    class FakeRunner:
        def __init__(self):
            self.outputLine = FakeSignal()
            self.finished = FakeSignal()

    class FakeSocket:
        def state(self):
            return QLocalSocket.LocalSocketState.ConnectedState

    class FakeController:
        def __init__(self, runner, finish_inside_start: bool):
            self.runner = runner
            self.finish_inside_start = finish_inside_start
            self.listener_was_ready = False

        def prepare_compile(self):
            return {"ok": True, "runner": self.runner, "profile": object()}

        def start_prepared_compile(self, _prepared):
            self.listener_was_ready = bool(self.runner.finished.callbacks)
            self.runner.outputLine.emit("stdout", "BUILD SUCCESSFUL")
            if self.finish_inside_start:
                self.runner.finished.emit(0)
            return {"ok": True}

        def abort_prepared_compile(self) -> None:
            pass

    original_timer = cli_server.QTimer
    original_send = cli_server._send_response
    sent: list[dict] = []
    cli_server.QTimer = FakeTimer
    cli_server._send_response = lambda _sock, payload: sent.append(payload)
    try:
        runner = FakeRunner()
        controller = FakeController(runner, finish_inside_start=True)
        tab = SimpleNamespace(service_controller=controller)
        cli_server._cmd_compile_async(tab, FakeSocket(), timeout=30)
        if not controller.listener_was_ready:
            failed.append("finished listener must be connected before compile starts")
        if len(sent) != 1 or not sent[0].get("ok") or "BUILD SUCCESSFUL" not in sent[0].get("output", ""):
            failed.append(f"synchronous finish should return one success response: {sent!r}")

        sent.clear()
        runner = FakeRunner()
        controller = FakeController(runner, finish_inside_start=False)
        tab = SimpleNamespace(service_controller=controller)
        cli_server._cmd_compile_async(tab, FakeSocket(), timeout=30)
        if sent:
            failed.append("compile must not respond before runner.finished")
        runner.finished.emit(1)
        if len(sent) != 1 or sent[0].get("ok") is not False or sent[0].get("exit_code") != 1:
            failed.append(f"non-zero finished must return one failure response: {sent!r}")
    finally:
        cli_server.QTimer = original_timer
        cli_server._send_response = original_send

    if failed:
        for msg in failed:
            print(f"[FAIL] compile_wait: {msg}", flush=True)
    else:
        print("[OK]   compile waits for the current runner.finished", flush=True)
    return failed


def workspace_cli_thread_check() -> list[str]:
    """验证工作区 CLI 的 UI 回调始终排队回 GUI 主线程。"""
    import time

    from PySide6.QtCore import QObject, QThread, Signal, Slot
    from PySide6.QtNetwork import QLocalSocket
    from PySide6.QtWidgets import QApplication

    from src.core import cli_server

    failed: list[str] = []
    app = QApplication.instance() or QApplication([])
    gui_thread = app.thread()
    callback_threads = []
    thread_finished = []

    class Worker(QObject):
        done = Signal(dict)

        @Slot()
        def run(self) -> None:
            self.done.emit({"ok": True, "_open_path": "smoke-workspace"})

        @Slot(dict)
        def dispose(self, _payload: dict) -> None:
            self.deleteLater()

    class FakeSocket:
        def state(self):
            return QLocalSocket.LocalSocketState.UnconnectedState

    class FakeWindow(QObject):
        def open_development_workspace(self, _path: str, quiet: bool = False):
            callback_threads.append(QThread.currentThread())
            return object()

    thread = QThread()
    worker = Worker()
    window = FakeWindow()
    responder = cli_server._WorkspaceCommandResponder(
        window, FakeSocket(), thread, worker,
    )
    original_snapshot = cli_server._environment_snapshot
    cli_server._environment_snapshot = lambda _tab: {"id": "smoke"}
    worker.moveToThread(thread)
    cli_server._connect_async_worker(worker, responder)
    thread.started.connect(worker.run)
    thread.finished.connect(lambda: thread_finished.append(True))
    tracked = (thread, worker, responder)
    cli_server._active_workers.append(tracked)
    try:
        thread.start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and (not thread_finished or not callback_threads):
            app.processEvents()
            time.sleep(0.01)
        app.processEvents()
        if callback_threads != [gui_thread]:
            failed.append("workspace UI callback must run exactly once on the GUI thread")
        if not thread_finished:
            failed.append("workspace CLI worker thread must stop after responding")
    finally:
        cli_server._environment_snapshot = original_snapshot
        if not thread_finished:
            try:
                thread.quit()
                thread.wait(1000)
            except RuntimeError:
                pass
        if tracked in cli_server._active_workers:
            cli_server._active_workers.remove(tracked)

    if failed:
        for msg in failed:
            print(f"[FAIL] workspace_cli_thread: {msg}", flush=True)
    else:
        print("[OK]   workspace CLI callback stays on the GUI thread", flush=True)
    return failed


def cli_response_and_packaging_check() -> list[str]:
    """验证空响应失败语义和独立 console CLI 的打包约束。"""
    from src.core.cli_client import _decode_response

    failed: list[str] = []
    result, error = _decode_response(b"")
    if result is not None or error != "empty response":
        failed.append("empty CLI response must be an explicit failure")
    result, error = _decode_response(b"not-json\n")
    if result is not None or error != "invalid response":
        failed.append("invalid CLI response must be an explicit failure")
    result, error = _decode_response(b'{"ok": true}\n')
    if result != {"ok": True} or error is not None:
        failed.append("valid CLI response should decode normally")

    build_text = (ROOT / "scripts" / "build.bat").read_text(encoding="utf-8")
    if "--name mini-ide-cli" not in build_text or "--console" not in build_text:
        failed.append("build must produce a console-subsystem mini-ide-cli.exe")
    if not (ROOT / "cli_main.py").is_file():
        failed.append("console CLI entrypoint is missing")

    if failed:
        for msg in failed:
            print(f"[FAIL] cli_reliability: {msg}", flush=True)
    else:
        print("[OK]   CLI response and console packaging reliability", flush=True)
    return failed


def aggregate_scan_isolation_check() -> list[str]:
    """验证聚合根扫描、缓存搜索、监听和旧候选都排除 workspaceDirectory。"""
    import tempfile

    from src.core.aggregate_workspace import (
        AggregateProject, is_aggregate_workspace_path, scan_exclusion_roots,
        save_aggregate_project,
    )
    from src.core.aggregate_workspace_manager import scan_aggregate_components
    from src.core.config import AppConfig, ProjectEntry
    from src.core.controller_index import _ControllerWorker
    from src.core.file_index import _IndexWorker, _should_ignore
    from src.core.path_utils import normalized_path_key
    from src.core.workspace_manager import ensure_workspace_candidates
    from src.ui.content_search import SearchWorker

    failed: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "race"
        source_root = root / "server"
        workspace_server = root / "workspace" / "certificate" / "server"
        workspace_webapp = root / "workspace" / "certificate" / "webapp"
        source_root.mkdir(parents=True)
        (source_root / ".git").mkdir()
        workspace_server.mkdir(parents=True)
        workspace_webapp.mkdir(parents=True)

        project = AggregateProject.from_dict(root, {
            "schemaVersion": 1,
            "id": "race",
            "name": "race",
            "kind": "aggregate",
            "workspaceDirectory": "workspace",
            "components": [
                {"id": "server", "path": "server", "type": "java"},
            ],
            "profiles": [],
        })
        save_aggregate_project(project)

        source_file = source_root / "SourceController.java"
        duplicate_file = workspace_server / "SourceController.java"
        java_text = (
            '@RestController\n@RequestMapping("/needle")\n'
            'class SourceController { @GetMapping("/one") void one() {} }\n'
        )
        source_file.write_text(java_text, encoding="utf-8")
        duplicate_file.write_text(java_text, encoding="utf-8")

        exclusions = scan_exclusion_roots(root)
        if len(exclusions) != 1 or normalized_path_key(exclusions[0]) != normalized_path_key(
            root / "workspace"
        ):
            failed.append(f"aggregate scan exclusion mismatch: {exclusions!r}")
        if not is_aggregate_workspace_path(duplicate_file):
            failed.append("nested Worktree file should be recognized as aggregate workspace data")
        if _should_ignore(source_file, root, exclusions):
            failed.append("source component files must remain visible to the index")
        if not _should_ignore(duplicate_file, root, exclusions):
            failed.append("watchdog events from workspaceDirectory must be ignored")

        indexed: list = []
        index_worker = _IndexWorker(str(root))
        index_worker.done.connect(lambda files: indexed.extend(files))
        index_worker.run()
        indexed_paths = {item.abs_path for item in indexed}
        if str(source_file) not in indexed_paths or str(duplicate_file) in indexed_paths:
            failed.append("file index must include source components and exclude Worktrees")

        walked_matches: list = []
        collected: list[str] = []
        walk_search = SearchWorker(
            root=str(root), query="needle", case_sensitive=False,
            whole_word=False, use_regex=False, include_exts=[],
        )
        walk_search.match_found.connect(lambda batch: walked_matches.extend(batch))
        walk_search.files_collected.connect(lambda files: collected.extend(files))
        walk_search.run()
        if len(walked_matches) != 1 or str(duplicate_file) in collected:
            failed.append("walk search must not read or cache files under workspaceDirectory")

        cached_matches: list = []
        cached_search = SearchWorker(
            root=str(root), query="needle", case_sensitive=False,
            whole_word=False, use_regex=False, include_exts=[],
            file_list=[str(source_file), str(duplicate_file)],
        )
        cached_search.match_found.connect(lambda batch: cached_matches.extend(batch))
        cached_search.run()
        if len(cached_matches) != 1 or cached_matches[0].abs_path != str(source_file):
            failed.append("cached search must discard stale workspaceDirectory entries")

        endpoints: list = []
        controller_worker = _ControllerWorker(str(root))
        controller_worker.done.connect(lambda items: endpoints.extend(items))
        controller_worker.run()
        if len(endpoints) != 1 or endpoints[0].abs_path != str(source_file):
            failed.append("controller scan must not duplicate endpoints from Worktrees")

        config = AppConfig(recent_projects=[
            ProjectEntry(path=str(workspace_server)),
            ProjectEntry(path=str(workspace_webapp)),
        ])
        if ensure_workspace_candidates(config) or config.workspaces:
            failed.append("legacy workspace discovery must ignore temporary Worktree paths")

        candidates = scan_aggregate_components(root)
        if [item.id for item in candidates] != ["server"]:
            failed.append(
                "aggregate component scan must inspect direct candidates and exclude workspaceDirectory"
            )

    if failed:
        for msg in failed:
            print(f"[FAIL] aggregate_scan: {msg}", flush=True)
    else:
        print("[OK]   aggregate workspace scan isolation", flush=True)
    return failed


def target_resolution_check() -> list[str]:
    """任意目录路径都应归入唯一的聚合/需求工作区环境。"""
    import tempfile

    from src.core.aggregate_workspace import (
        AggregateProject, DevelopmentWorkspace, load_aggregate_project,
        save_aggregate_project, save_development_workspace,
    )
    from src.core.target_resolver import resolve_target_path

    failed: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "paike"
        server = root / "server"
        webapp = root / "webapp"
        workspace_root = root / "workspace" / "task"
        (server / "src").mkdir(parents=True)
        webapp.mkdir(parents=True)
        (workspace_root / "server" / "src").mkdir(parents=True)
        project_data = {
            "schemaVersion": 1, "id": "paike", "name": "paike",
            "kind": "aggregate", "workspaceDirectory": "workspace",
            "components": [
                {"id": "server", "path": "server", "type": "java"},
                {"id": "webapp", "path": "webapp", "type": "frontend", "shared": True},
            ], "profiles": [],
        }
        save_aggregate_project(AggregateProject.from_dict(root, project_data))
        aggregate = load_aggregate_project(root)
        workspace_data = {
            "schemaVersion": 1, "id": "task", "name": "task",
            "createdAt": "2026-09-03T00:00:00Z",
            "aggregateProjectPath": str(root), "status": "active",
            "components": [
                {"id": "server", "sourceRepositoryPath": str(server),
                 "mode": "worktree", "worktreePath": str(workspace_root / "server"),
                 "baseBranch": "main", "baseCommit": "abc", "taskBranch": "feature/task-server"},
                {"id": "webapp", "sourceRepositoryPath": str(webapp),
                 "mode": "shared"},
            ],
        }
        save_development_workspace(
            DevelopmentWorkspace.from_dict(
                workspace_root, workspace_data, aggregate_project=aggregate,
            ), aggregate_project=aggregate,
        )
        cases = [
            (root, "aggregate"),
            (server, "aggregate_component"),
            (server / "src", "aggregate_component"),
            (workspace_root, "workspace"),
            (workspace_root / "server", "workspace_component"),
            (workspace_root / "server" / "src", "workspace_component"),
            (root / "unrelated", "normal_project"),
        ]
        (root / "unrelated").mkdir()
        for path, expected in cases:
            result = resolve_target_path(path, [str(root)])
            if result.kind != expected:
                failed.append(f"{path} resolved as {result.kind}, expected {expected}")
        result = resolve_target_path(workspace_root / "server", [str(root)])
        if result.open_command != "open-workspace" or result.component_id != "server":
            failed.append("workspace component should route to its workspace and component")
        result = resolve_target_path(webapp, [str(root)])
        if result.open_command != "open-aggregate" or result.component_id != "webapp":
            failed.append("aggregate component should route to aggregate and component")

    if failed:
        for msg in failed:
            print(f"[FAIL] target_resolution: {msg}", flush=True)
    else:
        print("[OK]   target path resolution", flush=True)
    return failed


def theme_switch_check() -> list[str]:
    """验证 GitHub Light / Dark 可在同一进程即时切换，且不写真实配置。"""
    from PySide6.QtWidgets import QApplication

    from src.core.config import AppConfig
    from src.ui.main_window import MainWindow
    from src.ui.theme import (
        BG_L1, FG_PRIMARY, THEME_GITHUB_DARK, THEME_GITHUB_LIGHT,
        apply_theme, current_theme,
    )

    failed: list[str] = []
    app = QApplication.instance() or QApplication([])
    config = AppConfig(theme=THEME_GITHUB_DARK, restore_tabs_on_startup=False)
    config.save = lambda: None
    apply_theme(app, config.theme)
    window = MainWindow(config)
    window._switch_theme(THEME_GITHUB_LIGHT)
    if (
        current_theme() != THEME_GITHUB_LIGHT
        or str(BG_L1) != "#F6F8FA"
        or str(FG_PRIMARY) != "#1F2328"
        or not window.theme_actions[THEME_GITHUB_LIGHT].isChecked()
    ):
        failed.append("GitHub Light must apply immediately and update the checked menu action")
    window._switch_theme(THEME_GITHUB_DARK)
    if (
        current_theme() != THEME_GITHUB_DARK
        or str(BG_L1) != "#161B22"
        or not window.theme_actions[THEME_GITHUB_DARK].isChecked()
    ):
        failed.append("GitHub Dark must restore immediately in the same window")
    window._mem_timer.stop()
    if failed:
        for message in failed:
            print(f"[FAIL] theme_switch: {message}", flush=True)
    else:
        print("[OK]   GitHub Light / Dark live theme switching", flush=True)
    return failed


def runtime_stop_execution_check() -> list[str]:
    """验证显式 stop 会执行，并且失败时只兜底托管 runner。"""
    from PySide6.QtWidgets import QApplication

    from src.core.runtime_units import RuntimeUnit
    from src.ui import project_tab as project_tab_module
    from src.ui.project_tab import ProjectTab

    failed: list[str] = []
    _app = QApplication.instance() or QApplication([])

    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self, *args):
            for callback in list(self.callbacks):
                callback(*args)

    class FakeStopRunner:
        instances = []

        def __init__(self, _parent=None):
            self.outputLine = FakeSignal()
            self.finished = FakeSignal()
            self._state = "idle"
            self.command = []
            self.cwd = ""
            self.deleted = False
            self.__class__.instances.append(self)

        def start(self, command, ctx):
            self.command = list(command)
            self.cwd = ctx.cwd
            self._state = "running"
            return True

        def state(self):
            return self._state

        def is_running(self):
            return self._state == "running"

        def stop_cleanup_pending(self):
            return False

        def deleteLater(self):
            self.deleted = True

    class ManagedRunner:
        def __init__(self):
            self._state = "running"
            self.stopped = False

        def state(self):
            return self._state

        def stop_cleanup_pending(self):
            return False

        def stop(self):
            self.stopped = True
            self._state = "stopping"

    class FakeLog:
        def __init__(self):
            self.lines = []

        def append_line(self, stream, line):
            self.lines.append((stream, line))

    class FakeController:
        @staticmethod
        def runner_busy(runner):
            return runner.state() != "idle"

    root = ROOT / "runtime-stop-cwd"
    unit = RuntimeUnit(
        id="worker", name="worker", kind="python", cwd=str(root),
        start_profile="worker:start", profile_names=("worker:start",),
        stop_command=("tool", "stop-worker"),
    )
    managed = ManagedRunner()
    log = FakeLog()
    class RuntimeStopTab:
        _append_runtime_stop_output = ProjectTab._append_runtime_stop_output
        _timeout_runtime_stop_command = ProjectTab._timeout_runtime_stop_command
        _on_runtime_stop_command_finished = ProjectTab._on_runtime_stop_command_finished

        def __init__(self):
            self._runtime_stop_runners = {}
            self.service_controller = FakeController()
            self.module_logs = {"worker": log}
            self.log = log
            self.service_panel = None

        def _update_main_button(self, _state):
            pass

    fake_tab = RuntimeStopTab()
    original_runner = project_tab_module.ProcessRunner
    project_tab_module.ProcessRunner = FakeStopRunner
    try:
        started = ProjectTab._start_runtime_stop(fake_tab, unit, managed, "worker")
        stop_runner = FakeStopRunner.instances[-1]
        if not started or stop_runner.command != ["tool", "stop-worker"]:
            failed.append("configured stop argv must be executed by an independent runner")
        if stop_runner.cwd != str(root):
            failed.append(f"configured stop must use runtime cwd: {stop_runner.cwd!r}")
        stop_runner._state = "idle"
        stop_runner.finished.emit(7)
        if not managed.stopped:
            failed.append("failed stop command must fall back to the managed runner")
    finally:
        project_tab_module.ProcessRunner = original_runner

    if failed:
        for msg in failed:
            print(f"[FAIL] runtime_stop: {msg}", flush=True)
    else:
        print("[OK]   configured runtime stop execution", flush=True)
    return failed


def runtime_unit_check() -> list[str]:
    """验证单体、Spring、Lerna 和显式运行配置使用同一模型。"""
    import json
    import tempfile

    from src.core.project_detector import detect_project
    from src.core.runtime_units import RuntimeUnit, runtime_start_groups

    failed: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        single = root / "single-vue"
        single.mkdir()
        (single / "package.json").write_text(json.dumps({
            "name": "single-vue",
            "scripts": {"dev": "vite"},
            "dependencies": {"vue": "3.0.0"},
        }), encoding="utf-8")
        (single / "vite.config.js").write_text(
            "export default {\n  server: {\n    port: 8881\n  }\n}\n",
            encoding="utf-8",
        )
        single_meta = detect_project(str(single))
        if len(single_meta.runtime_units) != 1:
            failed.append("single project must expose one default runtime unit")
        elif single_meta.runtime_units[0].expected_port != 8881:
            failed.append("frontend server.port must flow into the default runtime unit")

        spring = root / "spring-suite"
        spring.mkdir()
        (spring / "build.gradle").write_text(
            "plugins { id 'org.springframework.boot' version '3.2.0' }\n",
            encoding="utf-8",
        )
        (spring / "settings.gradle").write_text(
            "rootProject.name = 'spring-suite'\ninclude 'gateway', 'auth'\n",
            encoding="utf-8",
        )
        for name, port in (("gateway", 8080), ("auth", 8081)):
            module = spring / name
            java_dir = module / "src" / "main" / "java" / "com" / "example"
            resources = module / "src" / "main" / "resources"
            java_dir.mkdir(parents=True)
            resources.mkdir(parents=True)
            (java_dir / f"{name.title()}Application.java").write_text(
                "package com.example;\n@SpringBootApplication\n"
                f"public class {name.title()}Application {{}}\n",
                encoding="utf-8",
            )
            (resources / "application.yml").write_text(
                "spring:\n  data:\n    redis:\n      port: 6379\n"
                f"server:\n  port: {port}\n",
                encoding="utf-8",
            )
        spring_meta = detect_project(str(spring))
        spring_ids = [unit.id for unit in spring_meta.runtime_units]
        legacy_ids = [item[0] for item in spring_meta.spring_boot_modules]
        if spring_ids != ["gateway", "auth"] or spring_ids != legacy_ids:
            failed.append(f"Spring runtime IDs must preserve legacy module IDs: {spring_ids}")
        elif [unit.expected_port for unit in spring_meta.runtime_units] != [8080, 8081]:
            failed.append("Spring ports must ignore nested Redis/Mongo port values")

        single_spring = root / "single-spring"
        application = single_spring / "application"
        java_dir = application / "src" / "main" / "java" / "com" / "example"
        resources = application / "src" / "main" / "resources"
        java_dir.mkdir(parents=True)
        resources.mkdir(parents=True)
        (single_spring / "build.gradle").write_text(
            "plugins { id 'org.springframework.boot' version '3.2.0' }\n",
            encoding="utf-8",
        )
        (single_spring / "settings.gradle").write_text(
            "rootProject.name = 'single-spring'\ninclude 'application'\n",
            encoding="utf-8",
        )
        (java_dir / "Application.java").write_text(
            "package com.example;\n@SpringBootApplication\npublic class Application {}\n",
            encoding="utf-8",
        )
        (resources / "application.yml").write_text(
            "server:\n  port: 8989\n", encoding="utf-8",
        )
        single_spring_meta = detect_project(str(single_spring))
        if [unit.id for unit in single_spring_meta.runtime_units] != ["application"]:
            failed.append("single Spring submodule must preserve its legacy runtime ID")
        if [item[0] for item in single_spring_meta.spring_boot_modules] != ["application"]:
            failed.append("single Spring legacy module metadata must stay aligned")

        workspace = root / "webapp-lerna"
        packages = workspace / "packages"
        for name in ("teacher", "student", "shared"):
            (packages / name).mkdir(parents=True)
        (workspace / "package.json").write_text(json.dumps({
            "name": "webapp-lerna",
            "workspaces": ["packages/*"],
            "scripts": {
                "dev-teacher": "lerna run dev --scope=teacher",
                "build-teacher": "lerna run build --scope=teacher",
            },
        }), encoding="utf-8")
        (workspace / "lerna.json").write_text(json.dumps({
            "packages": ["packages/*"], "useWorkspaces": True,
        }), encoding="utf-8")
        for name, scripts in (
            ("teacher", {"dev": "vite", "build": "vite build"}),
            ("student", {"dev": "vite"}),
            ("shared", {"build": "vite build"}),
        ):
            (packages / name / "package.json").write_text(json.dumps({
                "name": name, "scripts": scripts,
            }), encoding="utf-8")
        (packages / "teacher" / "vite.config.js").write_text(
            "export default {\n  server: {\n    port: 8881\n  }\n}\n",
            encoding="utf-8",
        )
        (packages / "student" / "vite.config.js").write_text(
            "export default {\n  server: {\n    port: 8882\n  }\n}\n",
            encoding="utf-8",
        )
        workspace_meta = detect_project(str(workspace))
        workspace_ids = [unit.id for unit in workspace_meta.runtime_units]
        if workspace_meta.project_type != "frontend-workspace":
            failed.append("Lerna workspace must use the frontend-workspace adapter")
        if workspace_ids != ["student", "teacher"]:
            failed.append(f"workspace runnable packages mismatch: {workspace_ids}")
        if "shared" in workspace_ids:
            failed.append("workspace library without dev script must not become a runtime unit")
        ports = {unit.id: unit.expected_port for unit in workspace_meta.runtime_units}
        if ports != {"student": 8882, "teacher": 8881}:
            failed.append(f"workspace runtime ports mismatch: {ports}")

        pnpm_workspace = root / "pnpm-suite"
        pnpm_app = pnpm_workspace / "apps" / "portal"
        pnpm_app.mkdir(parents=True)
        (pnpm_workspace / "package.json").write_text(json.dumps({
            "name": "pnpm-suite", "scripts": {},
        }), encoding="utf-8")
        (pnpm_workspace / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n", encoding="utf-8")
        (pnpm_workspace / "pnpm-workspace.yaml").write_text(
            "packages:\n  - 'apps/*'\n", encoding="utf-8",
        )
        (pnpm_app / "package.json").write_text(json.dumps({
            "name": "portal", "scripts": {"dev": "vite"},
        }), encoding="utf-8")
        pnpm_meta = detect_project(str(pnpm_workspace))
        if [unit.id for unit in pnpm_meta.runtime_units] != ["portal"]:
            failed.append("pnpm-workspace.yaml packages must become runtime units")

        configured = root / "configured"
        (configured / "api").mkdir(parents=True)
        (configured / "worker").mkdir()
        runtime_config = {
            "units": [
                {"id": "api", "cwd": "api", "start": ["tool", "api"],
                 "expectedPort": 9000, "profiles": {
                     "build": ["tool", "build-api"],
                     "test": ["tool", "test-api"],
                     "compile": ["tool", "compile-api"],
                 }},
                {"id": "worker", "cwd": "worker", "start": ["tool", "worker"],
                 "dependsOn": ["api"], "stop": ["tool", "stop-worker"]},
            ],
        }
        configured_meta = detect_project(str(configured), runtime_config=runtime_config)
        groups = runtime_start_groups(configured_meta.runtime_units)
        if [[unit.id for unit in group] for group in groups] != [["api"], ["worker"]]:
            failed.append("configured runtime dependency order is unstable")
        if configured_meta.runtime_units[1].stop_command != ("tool", "stop-worker"):
            failed.append("configured stop argv must remain structured")
        api_unit = configured_meta.runtime_unit("api")
        api_profiles = {
            profile.name: profile for profile in configured_meta.profiles
            if profile.name in api_unit.profile_names
        }
        if len(api_profiles) != 4:
            failed.append(f"configured runtime profiles are incomplete: {list(api_profiles)}")
        for profile_name in api_profiles:
            owner = configured_meta.runtime_for_profile(profile_name)
            if owner is not api_unit:
                failed.append(f"profile owner lookup failed: {profile_name}")

        from types import SimpleNamespace
        from src.ui.project_tab import ProjectTab

        class ProfileRunner:
            def __init__(self):
                self.contexts = []

            def start(self, _command, context):
                self.contexts.append(context)
                return True

        class ProfileLog:
            def begin_run(self, _name):
                pass

            def append_line(self, _stream, _line):
                pass

            def end_run(self):
                pass

        class ProfileController:
            def profile_start_error(self, _profile):
                return None

            def begin_launch(self, _module):
                return 1

            def launch_started(self, _module, _generation, _runner):
                pass

            def launch_failed(self, _module, _generation):
                pass

        profile_runner = ProfileRunner()
        profile_tab = SimpleNamespace(
            project_meta=configured_meta,
            service_controller=ProfileController(),
            log=ProfileLog(), runner=profile_runner, _current_profile=None,
        )
        for profile in api_profiles.values():
            if not ProjectTab._start_profile(profile_tab, profile, enforce_guard=False):
                failed.append(f"configured profile failed to start: {profile.name}")
        expected_api_cwd = (configured / "api").resolve()
        if any(Path(context.cwd).resolve() != expected_api_cwd for context in profile_runner.contexts):
            failed.append("every runtime profile must use its unit cwd")

        invalid_configs = (
            ({"units": [
                {"id": "api", "start": ["tool"]},
                {"id": "API", "start": ["tool"]},
            ]}, "duplicate runtime unit ID"),
            ({"units": [
                {"id": "api", "start": ["tool"], "dependsOn": ["missing"]},
            ]}, "unknown runtime dependency"),
            ({"units": [
                {"id": "a", "start": ["tool"], "dependsOn": ["b"]},
                {"id": "b", "start": ["tool"], "dependsOn": ["a"]},
            ]}, "runtime dependency cycle"),
            ({"units": [
                {"id": "escape", "cwd": "..", "start": ["tool"]},
            ]}, "runtime cwd boundary"),
        )
        for config, label in invalid_configs:
            try:
                detect_project(str(configured), runtime_config=config)
            except ValueError:
                pass
            else:
                failed.append(f"{label} must fail during detection")

    real_workspace = ROOT.parents[2] / "xxpt" / "webapp-lerna"
    if real_workspace.is_dir():
        real_meta = detect_project(str(real_workspace))
        real_units = {
            unit.id: unit.expected_port for unit in real_meta.runtime_units
        }
        expected = {"m-stu": 8890, "pc-stu": 5174, "pc-teacher": 8881}
        if real_units != expected:
            failed.append(f"real xxpt/webapp-lerna runtimes mismatch: {real_units}")

    real_single_spring = ROOT.parents[2] / "framework-admin"
    if real_single_spring.is_dir():
        real_spring_meta = detect_project(str(real_single_spring))
        if [unit.id for unit in real_spring_meta.runtime_units] != ["application"]:
            failed.append(
                "real framework-admin must keep legacy runtime ID 'application'"
            )

    if failed:
        for msg in failed:
            print(f"[FAIL] runtime_units: {msg}", flush=True)
    else:
        print("[OK]   runtime unit detection and configuration", flush=True)
    return failed


def git_context_check() -> list[str]:
    """验证 Git AI 上下文的标签、排序和摘要格式。"""
    from src.core.git_context import build_ai_text, sort_changed_files, status_label, summary_from_changes
    from src.core.git_ops import ChangedFile

    failed: list[str] = []
    files = [
        ChangedFile("??", "new.txt", False, True),
        ChangedFile("M ", "src/app.py", True, False),
        ChangedFile(" M", "README.md", False, True),
    ]
    labels = [status_label(f) for f in files]
    if labels != ["未跟踪", "已暂存", "修改"]:
        failed.append(f"unexpected labels: {labels}")
    sorted_paths = [f.path for f in sort_changed_files(files)]
    if sorted_paths[0] != "README.md":
        failed.append(f"unexpected git sort order: {sorted_paths}")
    summary = summary_from_changes(
        repo=r"E:\repo",
        branch="main",
        files=files,
        stats={"src/app.py": (3, 1)},
        include_files=True,
    )
    text = build_ai_text(summary)
    if "影响目录：" not in text or "- [已暂存] src/app.py +3 -1" not in text:
        failed.append("git summary text should include groups and numstat")

    if failed:
        for msg in failed:
            print(f"[FAIL] git_context: {msg}", flush=True)
    else:
        print("[OK]   git_context rules", flush=True)
    return failed


# 允许出现 hex 字面量的文件（语义独立的"色板"，不属于 UI 主题范畴）
_HEX_WHITELIST = {
    "src/ui/theme.py",          # 主题 token 的唯一来源
    "src/ui/styles.py",          # 兼容外壳，转发到 theme
    "src/ui/syntax_highlighter.py",  # Pygments 代码语法色板（OneDark 风格）
}

# 抓任何位置的 hex 颜色字面量（包括 inline QSS 字符串内部，如 "background:#abc;"）
# \b 收尾保证只匹配 6 位（不抓 #abcdef00 之类的 8 位带 alpha 颜色，本项目不用）
_HEX_RE = re.compile(r"#[0-9a-fA-F]{6}\b")
# Python 行注释起始：行首是 # 或前面只有空白 + #
_COMMENT_RE = re.compile(r"^\s*#")


def hex_scan() -> list[tuple[str, int, str]]:
    """扫描 src/ 下所有 .py，返回 [(rel_path, lineno, line)] 表示违规命中"""
    hits: list[tuple[str, int, str]] = []
    src_dir = ROOT / "src"
    for py in src_dir.rglob("*.py"):
        rel = py.relative_to(ROOT).as_posix()
        if rel in _HEX_WHITELIST:
            continue
        try:
            text = py.read_text(encoding="utf-8")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if _COMMENT_RE.match(line):
                continue   # 整行注释里写颜色不算 hardcode
            if _HEX_RE.search(line):
                hits.append((rel, i, line.rstrip()))
    return hits


if __name__ == "__main__":
    failed = import_check()
    theme_failed = theme_switch_check()
    model_failed = service_state_check()
    runtime_unit_failed = runtime_unit_check()
    cli_failed = cli_parse_check()
    preflight_failed = preflight_isolation_check()
    cli_encoding_failed = cli_output_encoding_check()
    launcher_failed = external_launcher_check()
    git_executable_failed = git_executable_resolution_check()
    cli_match_failed = cli_project_match_check()
    launch_guard_failed = launch_and_operation_guard_check()
    runtime_stop_failed = runtime_stop_execution_check()
    compile_wait_failed = compile_wait_check()
    workspace_cli_thread_failed = workspace_cli_thread_check()
    cli_reliability_failed = cli_response_and_packaging_check()
    file_failed = file_action_check()
    preview_failed = file_preview_model_check()
    aggregate_scan_failed = aggregate_scan_isolation_check()
    git_failed = git_context_check()
    nginx_failed = nginx_detector_check()
    aggregate_failed = aggregate_workspace_model_check()
    development_lifecycle_failed = development_workspace_lifecycle_check()
    aggregate_runtime_failed = aggregate_runtime_ui_check()
    aggregate_dashboard_failed = aggregate_dashboard_check()
    legacy_migration_failed = legacy_workspace_migration_check()
    aggregate_definition_failed = aggregate_definition_management_check()
    workspace_session_failed = workspace_tab_session_check()
    target_resolution_failed = target_resolution_check()

    print()
    print("Hex hardcode scan (CLAUDE.md hard rule 15):")
    hex_hits = hex_scan()
    if not hex_hits:
        print("[OK]   no hex literals outside theme/syntax")
    else:
        # Windows console 默认 GBK，行内容里的 emoji 会让 print 抛 UnicodeEncodeError，
        # 用 errors='replace' 兜底——把不能编码的字符替成 '?' 不影响诊断
        enc = (sys.stdout.encoding or "utf-8")
        for rel, ln, line in hex_hits:
            safe = line.encode(enc, errors="replace").decode(enc, errors="replace")
            print(f"[HEX]  {rel}:{ln}: {safe}")

    total_fail = (
        len(failed) + len(theme_failed) + len(model_failed) + len(runtime_unit_failed)
        + len(cli_failed) + len(preflight_failed)
        + len(cli_encoding_failed) + len(launcher_failed)
        + len(git_executable_failed)
        + len(cli_match_failed) + len(launch_guard_failed)
        + len(runtime_stop_failed)
        + len(compile_wait_failed) + len(workspace_cli_thread_failed)
        + len(cli_reliability_failed)
        + len(file_failed) + len(preview_failed)
        + len(aggregate_scan_failed)
        + len(git_failed) + len(nginx_failed)
        + len(aggregate_failed) + len(development_lifecycle_failed)
        + len(aggregate_runtime_failed) + len(aggregate_dashboard_failed)
        + len(legacy_migration_failed) + len(aggregate_definition_failed)
        + len(workspace_session_failed) + len(hex_hits)
        + len(target_resolution_failed)
    )
    print()
    print(f"Result: imports {len(modules) - len(failed)}/{len(modules)}, "
          f"hex hits {len(hex_hits)}", flush=True)
    sys.exit(1 if total_fail else 0)
