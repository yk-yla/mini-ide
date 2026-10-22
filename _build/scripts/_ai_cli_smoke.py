"""Exercise the actual IPC/CLI against isolated projects and disposable services."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

ROOT = Path(os.environ.get("MINI_IDE_TEST_RUNTIME", Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(ROOT))


def backend(folder):
    from PySide6.QtWidgets import QApplication
    from src.core.config import AppConfig, ProjectEntry
    from src.core.project_detector import ProjectMeta, RunProfile
    from src.core.runtime_units import RuntimeUnit
    from src.ui.main_window import MainWindow
    from src.ui.project_tab import ProjectTab
    import importlib.util
    previous_argv = sys.argv
    sys.argv = ["ai-cli-test"]
    spec = importlib.util.spec_from_file_location("isolated_miniide_main", str(Path(__file__).resolve().parent.parent / "main.py"))
    main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main)
    sys.argv = previous_argv
    app = QApplication(["ai-cli-test"])
    app.setQuitOnLastWindowClosed(False)
    config = AppConfig(startup_restore_mode="none", restore_tabs_on_startup=False)
    window = MainWindow(config)
    while window.tabs.count():
        window.tabs.removeTab(0)
    port = int((folder / "port").read_text())
    for label in ("a", "b"):
        project = folder / label / "server"
        script = str(folder / "service.py")
        build = "import pathlib,time; p=pathlib.Path('build-count'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1'); print('BUILD_START',flush=True); time.sleep(2); print('BUILD_OK',flush=True)"
        profiles = [RunProfile("install", "Install dependencies", [sys.executable, "-c", "print('INSTALL_OK')"], kind="compile"),
                    RunProfile("build", "Build output", [sys.executable, "-u", "-c", build], kind="build"),
                    RunProfile("test", "Run failing tests", [sys.executable, "-c", "print('TEST_FAILED'); raise SystemExit(3)"], kind="test"),
                    RunProfile("dev", "Start service", [sys.executable, "-u", script, str(port)], kind="run", primary=True)]
        unit = RuntimeUnit("app", label, "api", str(project), "dev", tuple(p.name for p in profiles), port, "/health")
        meta = ProjectMeta(str(project), label, "python", "Python", "", profiles=profiles,
                           default_port=port, health_check_path="/health", runtime_units=[unit])
        tab = ProjectTab(meta, ProjectEntry(str(project)), config, window)
        window.tabs.addTab(tab, label)
    source_project = str(Path(__file__).resolve().parent.parent.parent)
    self_meta = ProjectMeta(source_project, "mini-ide", "generic", "mini-ide", "")
    self_tab = ProjectTab(self_meta, ProjectEntry(source_project), config, window)
    window.tabs.addTab(self_tab, "mini-ide")
    window.tabs.setCurrentIndex(1)
    os.chdir(folder / "b")
    window._local_server = main._start_local_server(window)
    if window._local_server is None:
        raise RuntimeError("isolated IPC did not start")
    (folder / "ready").write_text("ready")
    app.exec()


def run():
    with tempfile.TemporaryDirectory(prefix="miniide-ai-cli-") as directory:
        folder = Path(directory).resolve()
        from src.util.git_executable import resolve_git_executable
        def git(repo, *args):
            result = subprocess.run([resolve_git_executable(), *args], cwd=repo,
                capture_output=True, text=True, encoding="utf-8", timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            assert result.returncode == 0, (args, result.stderr)
            return result.stdout.strip()
        suite = folder / "suite"
        suite.mkdir()
        for label in ("server", "webapp"):
            repo = suite / label
            repo.mkdir()
            git(repo, "init", "-b", "develop")
            git(repo, "config", "user.name", "CLI test")
            git(repo, "config", "user.email", "cli@example.invalid")
            (repo / "seed.txt").write_text("seed\n")
            (repo / ".gitignore").write_text("launch-count\n")
            git(repo, "add", ".")
            git(repo, "commit", "-m", "seed")
            remote = folder / (label + "-origin.git")
            git(folder, "init", "--bare", str(remote))
            git(repo, "remote", "add", "origin", str(remote))
            git(repo, "push", "-u", "origin", "develop")
        (suite / ".mini-ide").mkdir()
        (suite / ".mini-ide/project.json").write_text(json.dumps({
            "schemaVersion": 1, "id": "suite", "name": "suite", "kind": "aggregate",
            "workspaceDirectory": "workspace", "components": [
                {"id": label, "path": label, "type": "generic"} for label in ("server", "webapp")]}))
        for label in ("a", "b", "unopened"):
            (folder / label / "server").mkdir(parents=True)
        unopened = folder / "unopened" / "server"
        (unopened / "package.json").write_text(json.dumps({"name": "unopened", "scripts": {"dev": "echo dev", "build": "echo build", "test": "echo test"}}))
        (unopened / "src").mkdir()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        (folder / "port").write_text(str(port))
        aggregate_config_path = suite / ".mini-ide/project.json"
        aggregate_config = json.loads(aggregate_config_path.read_text())
        aggregate_config["components"][0]["runtime"] = {"units": [{
            "id": "app", "name": "app", "cwd": ".", "kind": "api",
            "start": [sys.executable, "-u", str(folder / "service.py"), str(port)],
            "expectedPort": port, "healthCheckPath": "/health",
        }]}
        aggregate_config_path.write_text(json.dumps(aggregate_config))
        (folder / "service.py").write_text('''import http.server,sys,pathlib
p=pathlib.Path('launch-count')
n=int(p.read_text())+1 if p.exists() else 1
p.write_text(str(n))
class Handler(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  if self.path=='/emit':
   print('Traceback (most recent call last):\\n  File "demo.py", line 7\\n    raise ValueError()\\nValueError: scoped demo',flush=True)
  status=401 if pathlib.Path('deny-health').exists() else 200
  self.send_response(status); self.end_headers(); self.wfile.write(b'{"status":"UP"}')
print('LAUNCH',n,flush=True)
print('Application startup complete',flush=True)
http.server.HTTPServer(('127.0.0.1',int(sys.argv[1])),Handler).serve_forever()
''', encoding="utf-8")
        env = {**os.environ, "APPDATA": str(folder / "appdata"), "QT_QPA_PLATFORM": "offscreen",
               "MINI_IDE_SERVER_NAME": "miniide-ai-cli-" + uuid.uuid4().hex}
        packaged = os.environ.get("MINI_IDE_TEST_CLI")
        prefix = [packaged] if packaged else [sys.executable, "-X", "utf8", str(ROOT / "cli_main.py")]
        process_log = (folder / "backend.log").open("w", encoding="utf-8")
        process = subprocess.Popen([sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "serve", str(folder)],
                                   env=env, stdout=process_log, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        checks = []

        def check(condition, name):
            if not condition:
                raise AssertionError(name)
            checks.append(name)
            print("[OK] " + name, flush=True)

        def cli(*arguments, cwd=None, expected=0):
            result = subprocess.run([*prefix, *arguments], cwd=cwd or folder, env=env, capture_output=True,
                                    text=True, encoding="utf-8", timeout=25,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode != expected:
                raise AssertionError(f"{arguments}: exit {result.returncode}, expected {expected}: {result.stdout} {result.stderr}")
            return json.loads(result.stdout) if result.stdout.strip() else None

        try:
            deadline = time.monotonic() + 20
            while not (folder / "ready").exists():
                if process.poll() is not None or time.monotonic() > deadline:
                    raise AssertionError("backend failed to start")
                time.sleep(0.05)
            a = str(folder / "a" / "server")
            resolved = cli("--resolve-target", ".", cwd=a)
            check(resolved["path"] == a, "relative paths use caller directory despite different GUI cwd")
            preflight = cli("--preflight-build", str(Path(__file__).resolve().parent.parent.parent))
            check(preflight["can_build"], "target project build preflight passes in isolated GUI")
            ambiguous = cli("--list-runtimes", "server", expected=1)
            check(ambiguous["code"] == "ambiguous_project", "duplicate server IDs reject instead of selecting GUI cwd")
            invalid = cli("--list-projects", "--unknown", "--machine", expected=2)
            check(invalid["error"]["code"] == "invalid_arguments", "unknown arguments produce one structured error")
            cli("--close-workspace", "--machine", expected=2)
            invalid_timeout = cli("--health", a, "--timeout", "-1", "--machine", expected=2)
            check(invalid_timeout["error"]["code"] == "invalid_arguments", "negative time budgets fail before starting work")
            check("run-action" in cli("--capabilities")["commands"], "offline capability discovery")
            help_run = subprocess.run([*prefix, "--help", "run-action"], env=env, capture_output=True, text=True, encoding="utf-8")
            check(help_run.returncode == 0 and "--request-id" in help_run.stdout, "normal help describes action and retry options")
            context = cli("--context", str(unopened))
            check(context["projects"][0]["observed"] is False, "unopened context does not pretend services are stopped")
            child = cli("--resolve-target", ".", cwd=unopened / "src")
            child_context = cli("--context", str(unopened / "src"))
            check(child["project_target"] == str(unopened)
                  and child_context["projects"][0]["actions"] == context["projects"][0]["actions"],
                  "ordinary child resolves and inspects the same project actions")
            opened_child = cli("--open", str(unopened / "src"))
            check(opened_child["project"]["path"] == str(unopened), "opening an ordinary child opens its project root")
            cli("--close", str(unopened))
            aggregate_target = cli("--resolve-target", str(suite))
            check(not aggregate_target["project_target"] and not cli("--list-aggregates")["aggregates"],
                  "unregistered aggregate root is a container without an implicit project")
            opened_aggregate = cli("--" + aggregate_target["open_command"], aggregate_target["open_target"])
            check(opened_aggregate["ok"] and len(cli("--list-aggregates")["aggregates"]) == 1,
                  "resolver recommended command directly opens and registers a configured aggregate")
            check(cli("--list-runtimes", str(unopened))[0]["state"]["detail_state"] == "unknown", "unopened runtime inspection requires no GUI activation")
            actions = cli("--list-actions", a)["projects"][0]["actions"]
            check({x["id"] for x in actions} == {"install", "build", "test"}, "finite actions are discoverable separately from services")
            install = cli("--compile", a)
            check(install["action"]["id"] == "install" and "INSTALL_OK" in install["output"], "legacy compile reports the actual install action")
            timed = cli("--run-action", a, "build", "--timeout", "1", "--request-id", "build-once", expected=4)
            check(timed["operation"]["status"] == "running", "wait timeout leaves the actual build running")
            receipt = cli("--operation", timed["operation_id"], "--wait-seconds", "8")["operation"]
            check(receipt["status"] == "succeeded" and receipt["result"]["exit_code"] == 0, "final result remains queryable after caller timeout")
            duplicate = cli("--run-action", a, "build", "--async", "--request-id", "build-once")
            check(duplicate["operation_id"] == timed["operation_id"] and (Path(a) / "build-count").read_text() == "1", "request retry never executes the build twice")
            conflict = cli("--run-action", a, "test", "--request-id", "build-once", expected=1)
            check(conflict["code"] == "operation_request_conflict", "same request ID cannot silently change action")
            failed = cli("--run-action", a, "test", "--request-id", "failed-test", expected=1)
            check(failed["exit_code"] == 3 and failed["action"]["id"] == "test", "test failure keeps actual exit status and action")
            repeated_failure = cli("--run-action", a, "test", "--request-id", "failed-test", expected=1)
            check(repeated_failure["operation_id"] == failed["operation_id"], "retry of failed action preserves its failure and receipt")
            started = cli("--start", a, "--wait", "--timeout", "10")
            check(started["ok"] and started["checks"]["http"]["status"] == "not_checked", "startup readiness does not claim HTTP verification")
            healthy = cli("--health", a, "--http", "--timeout", "5")
            check(healthy["checks"]["http"]["status"] == "passed", "configured HTTP health is actually requested")
            initial = cli("--log", a, "--current-run")
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(f"http://127.0.0.1:{port}/emit").close()
            output = cli("--log", a, "--current-run", "--errors", "--cursor", str(initial["next_cursor"]), "--run-id", initial["run_id"])
            check(any('File "demo.py"' in line for line in output["lines"]) and any("raise ValueError" in line for line in output["lines"]), "incremental errors retain traceback context")
            (Path(a) / "deny-health").touch()
            denied = cli("--health", a, "--http", "--timeout", "5", expected=1)
            check(denied["checks"]["http"]["observations"][0]["status"] == "auth_required", "401 health is identified and never treated as success")
            (Path(a) / "deny-health").unlink()
            cli("--restart", a, "--wait", "--timeout", "10")
            new_logs = cli("--log", a, "--current-run")
            check(new_logs["run_id"] != initial["run_id"] and not any("LAUNCH 1" in line for line in new_logs["lines"]), "restart logs exclude the prior launch")
            changed = cli("--log", a, "--current-run", "--run-id", initial["run_id"], expected=1)
            check(changed["code"] == "run_changed", "stale run IDs are rejected explicitly")
            cli("--stop", a)
            timeout = cli("--health", a, "--timeout", "1", expected=1)
            check(timeout["ok"] is False and "checks" in timeout, "stopped process health includes failure observations")
            pending = cli("--health", str(folder / "b" / "server"), "--timeout", "1", expected=4)
            check(pending["code"] == "health_timeout" and "last_observation" in pending, "readiness timeout retains last observation")
            machine = cli("--list-projects", "--machine")
            check(machine["ok"] and isinstance(machine["data"], list), "machine envelope preserves legacy list data")
            created = cli("--create-development-workspace", "suite", "delivery", "--projects", "server,webapp", "--request-id", "create-delivery")
            workspace = Path(created["workspace"]["path"])
            check(set(created["pushed"]) == {"server", "webapp"} and all(
                git(suite / label, "ls-remote", "--heads", "origin", f"refs/heads/feature/delivery-{label}")
                for label in ("server", "webapp")), "workspace creation actually publishes both task branches")
            source_server = str(suite / "server")
            cli("--ensure-running", source_server, "app", "--timeout", "10")
            blocked_open = cli("--open-workspace", str(workspace), "--request-id", "blocked-switch", expected=1)
            routed_open = cli("--open", str(workspace / "server"), expected=1)
            pending_open = cli("--open-workspace", str(workspace), "--async", "--request-id", "blocked-async-switch")
            failed_open = cli("--operation", pending_open["operation_id"], "--wait-seconds", "10")["operation"]
            check(all(result["code"] == "services_running" and result["running"][0]["project_path"] == source_server
                      and result["running"][0]["module"] == "app" and result["running"][0]["pid"]
                      for result in (blocked_open, routed_open, failed_open["result"]))
                  and failed_open["status"] == "failed", "direct, routed and async switching retain actionable service blockers")
            check(cli("--health", source_server, "app", "--timeout", "2")["ok"],
                  "blocked workspace switching leaves source service available")
            cli("--stop", source_server)
            replay = cli("--open-workspace", str(workspace), "--request-id", "blocked-switch", expected=1)
            check(replay["operation_id"] == blocked_open["operation_id"], "same request ID preserves failed receipt")
            cli("--open-workspace", str(workspace), "--request-id", "switch-after-stop")
            cli("--ensure-running", str(workspace / "server"), "app", "--timeout", "10")
            back = cli("--open-aggregate", str(suite), expected=1)
            back_routed = cli("--open", source_server, expected=1)
            same = cli("--open-workspace", str(workspace))
            check(back["code"] == back_routed["code"] == "services_running" and same["ok"],
                  "return to source has the same guard while reopening active workspace succeeds")
            cli("--close-workspace", str(workspace))
            for label in ("server", "webapp"):
                repo = suite / label
                (repo / "base-update.txt").write_text("base advanced\n")
                git(repo, "add", ".")
                git(repo, "commit", "-m", "base update")
                git(repo, "push")
                (workspace / label / "task.txt").write_text("task work\n")
            synced = cli("--workspace-sync-push", str(workspace), "--request-id", "sync-delivery")
            check(all(item["synced"] and item["pushed"] for item in synced["projects"])
                  and all((workspace / label / "base-update.txt").exists() and not (suite / label / "task.txt").exists()
                          for label in ("server", "webapp")), "sync-push commits tasks and brings base changes into task branches only")
            blocked = cli("--workspace-delete", str(workspace), expected=1)
            check(blocked["code"] == "workspace_delete_blocked" and workspace.is_dir(), "unmerged task commits cannot be deleted")
            merged = cli("--workspace-merge", str(workspace), "--request-id", "merge-delivery")
            check(all(item["base_branch"] == "develop" and item["merged"] and item["pushed"] for item in merged["projects"])
                  and all((suite / label / "task.txt").exists() for label in ("server", "webapp")), "merge CLI publishes into recorded develop branches")
            cli("--open-workspace", str(workspace))
            active = cli("--workspace-delete", str(workspace), expected=1)
            check(active["code"] == "workspace_active", "active workspace must be closed explicitly before deletion")
            cli("--close-workspace", str(workspace))
            removed = cli("--workspace-delete", str(workspace), "--request-id", "delete-delivery")
            check(removed["directory_deleted"] and not workspace.exists() and all(
                not git(suite / label, "branch", "--list", f"feature/delivery-{label}") and
                not git(suite / label, "ls-remote", "--heads", "origin", f"refs/heads/feature/delivery-{label}")
                for label in ("server", "webapp")), "delete CLI removes directory and all owned local/remote branches")
            replay = cli("--workspace-delete", str(workspace), "--request-id", "delete-delivery")
            check(replay["operation_id"] == removed["operation_id"], "delete retries return the original receipt after metadata is gone")
            cli("--quit")
            process.wait(timeout=15)
            check(process.returncode == 0, "isolated GUI exits normally with all sample services stopped")
            from src.core.cli_operations import OperationStore
            receipts_path = folder / "receipts.json"
            store = OperationStore(receipts_path)
            unfinished, _ = store.begin({"cmd": "compile", "project": "sample", "request_id": "persist"})
            restored = OperationStore(receipts_path).get(unfinished["operation_id"])
            check(restored["status"] == "interrupted", "IDE restart labels unfinished operations instead of replaying writes")
            store.complete(unfinished["operation_id"], {"ok": True, "exit_code": 0})
            check(OperationStore(receipts_path).get(unfinished["operation_id"])["result"]["exit_code"] == 0, "completed result survives receipt reload")
            from src.core.run_logs import RunLog
            bounded = RunLog(max_lines=2)
            for line in ("old", "Traceback", "    " + "x" * 400):
                bounded.append(line)
            bounded_result = bounded.read(cursor=1, max_chars=100)
            check(bounded_result["truncated"] and bounded_result["next_cursor"] > 1, "bounded log cursors advance even when a long line is clipped")
            from types import SimpleNamespace
            from unittest.mock import patch
            from src.core import cli_server
            closed = []
            def workspace_tab(identifier):
                return SimpleNamespace(workspace=SimpleNamespace(id=identifier, name="same-name", root_path=str(folder / identifier)),
                    running_service_items=lambda **kwargs: [], stop_all_services=lambda **kwargs: 0,
                    activate_workspace=lambda *args, **kwargs: closed.append(identifier) or True)
            tabs = [workspace_tab("first"), workspace_tab("second")]
            fake_window = SimpleNamespace(_save_tab_session=lambda: None)
            with patch.object(cli_server, "_all_environment_tabs", return_value=tabs):
                close_result = cli_server._cmd_close_workspace(fake_window, str(folder / "first"))
                ambiguous_close = cli_server._cmd_close_workspace(fake_window, "same-name")
            check(close_result["ok"] and closed == ["first"] and ambiguous_close["code"] == "ambiguous_workspace", "workspace close uses explicit identity and rejects duplicate names")
            print(f"Result: {len(checks)} checks passed", flush=True)
        except Exception:
            process_log.flush()
            print((folder / "backend.log").read_text(encoding="utf-8", errors="replace")[-18000:], file=sys.stderr)
            raise
        finally:
            if process.poll() is None:
                try:
                    cli("--stop", str(folder / "a" / "server"))
                    cli("--quit")
                    process.wait(timeout=15)
                except Exception as exc:
                    print("Isolated stop cleanup: " + str(exc), file=sys.stderr)
                    try:
                        cli("--quit")
                        process.wait(timeout=15)
                    except Exception as quit_error:
                        print("Isolated test process did not exit; inspect PID " + str(process.pid) + ": " + str(quit_error), file=sys.stderr)
            process_log.close()


def packaged_gui_check():
    executable = Path(os.environ["MINI_IDE_TEST_CLI"])
    gui = executable.with_name("mini-ide.exe")
    with tempfile.TemporaryDirectory(prefix="miniide-ai-frozen-") as directory:
        folder = Path(directory).resolve()
        from src.util.git_executable import resolve_git_executable
        suite = folder / "suite"
        repo = suite / "server"
        repo.mkdir(parents=True)
        def git(*args):
            completed = subprocess.run([resolve_git_executable(), *args], cwd=repo,
                capture_output=True, text=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            assert completed.returncode == 0, completed.stderr
        git("init", "-b", "develop")
        git("config", "user.name", "Frozen test")
        git("config", "user.email", "frozen@example.invalid")
        (repo / "seed.txt").write_text("frozen fixture\n")
        git("add", ".")
        git("commit", "-m", "seed")
        remote = folder / "origin.git"
        git("init", "--bare", str(remote))
        git("remote", "add", "origin", str(remote))
        git("push", "-u", "origin", "develop")
        (suite / ".mini-ide").mkdir()
        (suite / ".mini-ide/project.json").write_text(json.dumps({
            "schemaVersion": 1, "id": "frozen", "name": "frozen", "kind": "aggregate",
            "workspaceDirectory": "workspace", "components": [{"id": "server", "path": "server", "type": "generic"}]}))
        config = folder / "mini-ide" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"startup_restore_mode": "none", "restore_tabs_on_startup": False,
                                      "aggregate_project_paths": [str(suite)]}))
        env = {**os.environ, "APPDATA": str(folder), "QT_QPA_PLATFORM": "offscreen",
               "MINI_IDE_SERVER_NAME": "miniide-ai-frozen-" + uuid.uuid4().hex}
        out = (folder / "gui-output.log").open("w", encoding="utf-8")
        process = subprocess.Popen([str(gui)], env=env, stdout=out, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        def call(*args):
            response = subprocess.run([str(executable), *args], env=env, cwd=folder, capture_output=True,
                                      text=True, encoding="utf-8", timeout=15,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return response.returncode, json.loads(response.stdout) if response.stdout.strip() else None
        try:
            deadline = time.monotonic() + 25
            while True:
                code, status = call("--status")
                if code == 0:
                    break
                if process.poll() is not None or time.monotonic() > deadline:
                    raise AssertionError("frozen GUI failed to expose isolated IPC")
                time.sleep(0.1)
            code, version = call("--version")
            assert code == 0 and status["version"] == version["version"] and status["project_count"] == 0
            code, inspected = call("--context", str(folder), "--machine")
            assert code == 0 and inspected["ok"] and inspected["data"]["target"]["path"] == str(folder)
            code, created = call("--create-development-workspace", "frozen", "frozen-task", "--projects", "server")
            assert code == 0 and created["pushed"] == ["server"], created
            workspace = created["workspace"]["path"]
            for action in ("--workspace-sync-push", "--workspace-merge", "--workspace-delete"):
                code, action_result = call(action, workspace)
                assert code == 0 and action_result["ok"], (action, action_result)
            assert not Path(workspace).exists()
            code, stopped = call("--quit")
            assert code == 0 and stopped["ok"]
            process.wait(timeout=15)
            assert process.returncode == 0
            print("[OK] frozen GUI/CLI version, isolated IPC, context, complete workspace lifecycle and normal exit", flush=True)
        finally:
            if process.poll() is None:
                call("--quit")
                process.wait(timeout=20)
            out.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        backend(Path(sys.argv[2]))
    else:
        run()
        if os.environ.get("MINI_IDE_TEST_CLI"):
            packaged_gui_check()
