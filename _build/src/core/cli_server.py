"""CLI 服务端：在已运行的 mini-ide 主进程中处理 CLI 命令请求。

接收 JSON 命令 → 路由到对应 ProjectTab → 返回 JSON 响应。
集成到 main.py 的 QLocalServer 连接处理流程中。
"""
from __future__ import annotations

from src.core.search_worker_lifecycle import BackgroundThread

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtNetwork import QLocalSocket

if TYPE_CHECKING:
    from src.ui.main_window import MainWindow

from src.core import git_context
from src.core.cli_contract import is_path_target, result_code
from src.core.aggregate_workspace import (
    load_aggregate_project, load_development_workspace,
)
from src.core.development_workspace_service import (
    build_workspace_creation_plan, commit_and_push_development_workspace,
    create_development_workspace, delete_development_workspace, merge_development_workspace,
    inspect_workspace_delete, review_development_workspace,
    sync_development_workspace, sync_commit_and_push_development_workspace,
)
from src.core.path_utils import normalized_path_key
from src.core.service_state import EnvironmentSwitchBlocked
from src.core.target_resolver import (
    TargetResolutionError, resolve_target_path,
)
from src.util import app_log
from src.core.app_version import build_info

log = app_log.get_logger("cli_server")


def _operations(window):
    from src.core.cli_operations import OperationStore
    from src.core.config import CONFIG_PATH
    if not hasattr(window, "_cli_operations"):
        window._cli_operations = OperationStore(CONFIG_PATH.parent / "cli-operations.json")
    return window._cli_operations


def _connected(sock):
    try:
        return sock.state() == QLocalSocket.LocalSocketState.ConnectedState
    except RuntimeError:
        return False


class _OperationChannel(QObject):
    def __init__(self, window, sock, command, receipt):
        super().__init__(window)
        self.sock = sock
        self.store = _operations(window)
        self.identifier = receipt["operation_id"]
        self.answered = bool(command.get("async"))
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.wait_expired)
        if self.answered:
            _send_response(sock, {"ok": True, "accepted": True, "operation_id": self.identifier, "operation": receipt})
        else:
            defaults = {"compile": 300, "run-action": 300, "start": 60, "stop": 60,
                        "restart": 60, "ensure-running": 60}
            self.timer.start(int(command.get("timeout", defaults.get(command["cmd"], 600))) * 1000)

    def state(self):
        record = self.store.records.get(self.identifier)
        return (QLocalSocket.LocalSocketState.ConnectedState if record and record["status"] == "running"
                else QLocalSocket.LocalSocketState.UnconnectedState)

    @Slot(str)
    def progress(self, line):
        self.store.progress(self.identifier, line)

    def wait_expired(self):
        if not self.answered and _connected(self.sock):
            _send_response(self.sock, {"ok": False, "code": "operation_wait_timeout",
                "error": "wait timed out; operation continues, query operation_id",
                "operation_id": self.identifier, "operation": self.store.get(self.identifier)})
        self.answered = True

    def finish(self, result):
        if self.state() != QLocalSocket.LocalSocketState.ConnectedState:
            return
        self.timer.stop()
        result = dict(result)
        result["operation_id"] = self.identifier
        if result.get("ok") is False:
            result.setdefault("code", result_code(result))
        record = self.store.complete(self.identifier, result)
        if not self.answered and _connected(self.sock):
            _send_response(self.sock, {**result, "operation_status": record["status"]})
        self.deleteLater()


def _cmd_operation_async(window, identifier, wait_seconds, sock, return_result=False):
    store = _operations(window)
    deadline = time.monotonic() + wait_seconds
    timer = QTimer(window)
    timer.setInterval(100)

    def check():
        if not _connected(sock):
            timer.stop()
            timer.deleteLater()
            return
        record = store.get(identifier)
        if record is None:
            result = {"ok": False, "code": "operation_not_found", "error": "operation not found", "operation_id": identifier}
        elif record["status"] != "running":
            result = ({**record["result"], "operation_id": identifier, "operation_status": record["status"]}
                      if return_result else {"ok": True, "operation": record, "operation_id": identifier})
        elif not wait_seconds:
            result = {"ok": True, "operation": record, "operation_id": identifier}
        elif time.monotonic() >= deadline:
            result = {"ok": False, "code": "operation_wait_timeout", "error": "operation is still running",
                      "operation": record, "operation_id": identifier}
        else:
            return
        _send_response(sock, result)
        timer.stop()
        timer.deleteLater()

    timer.timeout.connect(check)
    timer.start()
    check()


class _InspectionWorker(QObject):
    done = Signal(dict)

    def __init__(self, command, aggregates, observations):
        super().__init__()
        self.command, self.aggregates, self.observations = command, aggregates, observations

    @Slot()
    def run(self):
        from src.core.project_inspection import inspect_directory
        try:
            command = self.command
            context = inspect_directory(command.get("path") or command["project"], self.aggregates,
                                        self.observations, command.get("full", False))
            if command["cmd"] == "list-actions":
                result = {"ok": True, "projects": [{"path": item["path"], "actions": item["actions"]} for item in context["projects"]]}
            elif command["cmd"] == "list-runtimes":
                result = []
                for project in context["projects"]:
                    states = {state["name"]: state for state in project["states"]}
                    for unit in project["runtime_units"]:
                        result.append({**unit, "observed": project["observed"],
                            "state": states.get(unit["id"], {"detail_state": "unknown", "reason": "project is not open; not observed"})})
            else:
                result = context
        except Exception as exc:
            result = {"ok": False, "code": "inspection_failed", "error": str(exc)}
        self.done.emit({"_inspection_payload": result})


def _run_inspection_async(command, window, sock):
    observations = {str(Path(tab.project_meta.path).resolve()).casefold():
                    [state.to_cli_module() for state in tab.service_controller.states(refresh_external=False)]
                    for tab in _project_target_tabs(window)}
    thread = BackgroundThread()
    worker = _InspectionWorker(dict(command), tuple(window.config.aggregate_project_paths), observations)
    responder = _GitDiffResponder(sock, thread, worker)
    worker.moveToThread(thread)
    worker.done.connect(responder.receive, Qt.ConnectionType.QueuedConnection)
    worker.done.connect(thread.quit)
    thread.started.connect(worker.run)
    thread.finished.connect(worker.deleteLater)
    thread.finished.connect(responder.cleanup)
    _active_workers.append((thread, worker, responder))
    thread.start()


def _app_version() -> str:
    return build_info()["version"]


def _project_snapshot(tab) -> dict:
    """返回项目级快照，供 status / preflight / diagnose 复用。"""
    states = tab.service_states(refresh_external=True)
    modules = [s.to_cli_module() for s in states if s.kind != "script"]
    scripts = [s.to_running_item() for s in states if s.kind == "script" and s.is_active]
    return {
        "name": tab.project_meta.name,
        "path": tab.project_meta.path,
        "type": tab.project_meta.project_type,
        "display_type": tab.project_meta.display_type,
        "package_manager": tab.project_meta.package_manager,
        "default_port": tab.project_meta.default_port,
        "health_check_path": tab.project_meta.health_check_path,
        "runtime_units": [unit.to_dict() for unit in tab.project_meta.runtime_units],
        "modules": modules,
        "scripts": scripts,
        "running": [s.to_running_item() for s in states if s.is_active],
    }


def _all_project_tabs(window: "MainWindow") -> list:
    from src.ui.project_tab import ProjectTab
    tabs = []
    for i in range(window.tabs.count()):
        w = window.tabs.widget(i)
        if isinstance(w, ProjectTab):
            tabs.append(w)
    return tabs


def _all_environment_tabs(window: "MainWindow") -> list:
    from src.ui.aggregate_project_tab import AggregateProjectTab
    return [
        window.tabs.widget(index)
        for index in range(window.tabs.count())
        if isinstance(window.tabs.widget(index), AggregateProjectTab)
    ]


def _project_target_tabs(window: "MainWindow", *, include_components: bool = True) -> list:
    """返回 CLI 可操作项目；聚合目录内项目不参与顶层状态重复汇总。"""
    tabs = list(_all_project_tabs(window))
    if include_components:
        for environment in _all_environment_tabs(window):
            tabs.extend(environment.component_tabs.values())
    return list(dict.fromkeys(tabs))


def _environment_snapshot(tab) -> dict:
    states = tab.service_states(refresh_external=True)
    return {
        "id": tab.stable_id,
        "name": tab.project_meta.name,
        "path": tab.path,
        "type": tab.project_meta.project_type,
        "aggregate_project_path": tab.aggregate_root_path,
        "workspace": bool(tab.workspace),
        "workspace_path": tab.workspace.root_path if tab.workspace else "",
        "projects": [
            {
                "id": component_id,
                "path": component_tab.project_meta.path,
                "type": component_tab.project_meta.project_type,
                "runtime_units": [
                    unit.to_dict() for unit in component_tab.project_meta.runtime_units
                ],
                "modules": [
                    state.to_cli_module()
                    for state in component_tab.service_states(refresh_external=False)
                    if state.kind != "script"
                ],
            }
            for component_id, component_tab in tab.component_tabs.items()
        ],
        "running": [state.to_running_item() for state in states if state.is_active],
    }


def _collect_running(window: "MainWindow") -> list[dict]:
    items: list[dict] = []
    for tab in _all_project_tabs(window):
        try:
            items.extend(tab.running_service_items(refresh_external=True))
        except Exception:
            log.exception("收集运行服务失败: %s", tab.project_meta.name)
    for tab in _all_environment_tabs(window):
        try:
            items.extend(tab.running_service_items(refresh_external=True))
        except Exception:
            log.exception("收集聚合环境服务失败: %s", tab.project_meta.name)
    return items


def _public_target_candidate(record: dict) -> dict:
    """歧义响应只暴露可 JSON 序列化的目标标识。"""
    return {
        "id": str(record.get("id", "")),
        "name": str(record.get("name", "")),
        "path": str(record.get("path", "")),
    }


def _match_target(records: list[dict], target: str, label: str) -> tuple[dict | None, dict | None]:
    key = str(target or "").strip()
    if not key:
        return None, {"ok": False, "error": f"missing {label} target"}
    path_matches = [
        item for item in records
        if is_path_target(key) and normalized_path_key(item["path"]) == normalized_path_key(key)
    ]
    exact = path_matches or [
        item for item in records
        if key.casefold() in {
            str(item.get("id", "")).casefold(),
            str(item.get("name", "")).casefold(),
            Path(item["path"]).name.casefold(),
        }
    ]
    if len(exact) == 1:
        return exact[0], None
    if len(exact) > 1:
        return None, {
            "ok": False, "code": f"ambiguous_{label}",
            "error": f"ambiguous {label}: {key}",
            "candidates": [_public_target_candidate(item) for item in exact],
        }
    folded = key.casefold()
    fuzzy = [
        item for item in records
        if any(folded in str(value).casefold() for value in (
            item.get("id", ""), item.get("name", ""), item.get("path", ""),
        ))
    ]
    if len(fuzzy) == 1:
        return fuzzy[0], None
    if len(fuzzy) > 1:
        return None, {
            "ok": False, "code": f"ambiguous_{label}",
            "error": f"ambiguous {label}: {key}",
            "candidates": [_public_target_candidate(item) for item in fuzzy],
        }
    return None, {
        "ok": False, "code": f"{label}_not_found",
        "error": f"{label} not found: {key}",
    }


def _aggregate_records(window: "MainWindow") -> list[dict]:
    records = []
    for path in window.config.aggregate_project_paths:
        try:
            project = load_aggregate_project(path)
            records.append({
                "id": project.id, "name": project.name, "path": project.root_path,
                "project_count": len(project.components), "error": "",
            })
        except Exception as exc:
            records.append({
                "id": "", "name": Path(path).name, "path": str(path),
                "project_count": 0, "error": str(exc),
            })
    return records


def _development_workspace_records(window: "MainWindow") -> list[dict]:
    """只读取聚合 Tab 的后台缓存，不在 GUI 主线程扫描目录。"""
    records = []
    for tab in _all_environment_tabs(window):
        for summary in getattr(tab, "_workspace_summaries", ()):
            workspace = summary.workspace
            records.append({
                "id": workspace.id if workspace else "",
                "name": summary.name,
                "path": summary.root_path,
                "aggregate": summary.aggregate_name,
                "projects": list(summary.component_ids),
                "branches": list(summary.task_branches),
                "status": summary.status,
                "git_change_count": summary.git_change_count,
                "error": summary.error,
            })
    return records


def _log_widget_for(tab, module: str | None):
    return tab.service_controller.log_widget(module)


def _iter_log_lines(log_widget, tail: int) -> list[str]:
    if log_widget is None:
        return []
    doc = log_widget.edit.document()
    total_blocks = doc.blockCount()
    start = max(0, total_blocks - max(0, tail))
    lines = []
    block = doc.findBlockByNumber(start)
    while block.isValid() and len(lines) < tail:
        text = block.text()
        if text:
            lines.append(text)
        block = block.next()
    return lines


def _validate_module(tab, module: str | None) -> dict | None:
    return tab.service_controller.validate_module(module)


def _looks_error_line(line: str) -> bool:
    low = line.lower()
    return any(x in low for x in (
        " error", "[error", "exception", "traceback", "failed", "failure",
        "caused by", "端口", "占用", "错误", "失败",
    ))


class _GitDiffWorker(QObject):
    done = Signal(dict)

    def __init__(self, root: str, mode: str, max_chars: int, parent=None):
        super().__init__(parent)
        self.root = root
        self.mode = mode
        self.max_chars = max_chars

    def run(self) -> None:
        try:
            summary = git_context.summary_payload(self.root, include_files=True)
            if not summary.get("ok"):
                self.done.emit(summary)
                return
            if self.mode != "full":
                summary["text"] = git_context.build_ai_text(summary)
                self.done.emit(summary)
                return
            repo = summary["repo"]
            diff, truncated = git_context.limited_diff_text(repo, max_chars=self.max_chars)
            summary["truncated"] = truncated
            summary["text"] = git_context.build_ai_text(summary, diff=diff)
            self.done.emit(summary)
        except Exception as e:
            log.exception("生成 CLI git diff 失败")
            self.done.emit({"ok": False, "error": str(e)})

    @Slot(dict)
    def dispose(self, _payload: dict) -> None:
        self.deleteLater()


class _WorkspaceCommandWorker(QObject):
    done = Signal(dict)
    progress = Signal(str)

    def __init__(self, command: dict, aggregate_paths: tuple[str, ...], running_paths: tuple[str, ...]):
        super().__init__()
        self.command = command
        self.aggregate_paths = aggregate_paths
        self.running_paths = running_paths

    def _aggregates(self) -> list[dict]:
        records = []
        for path in self.aggregate_paths:
            try:
                project = load_aggregate_project(path)
                records.append({
                    "id": project.id, "name": project.name,
                    "path": project.root_path, "project": project,
                })
            except Exception:
                continue
        return records

    def _workspaces(self) -> list[dict]:
        records = []
        for aggregate in self._aggregates():
            project = aggregate["project"]
            root = project.workspace_root
            if not root.is_dir():
                continue
            try:
                children = tuple(root.iterdir())
            except OSError:
                continue
            for path in children:
                if not path.is_dir() or not (path / "workspace.json").is_file():
                    continue
                try:
                    workspace = load_development_workspace(path, aggregate_project=project)
                except Exception:
                    continue
                records.append({
                    "id": workspace.id, "name": workspace.name,
                    "path": workspace.root_path, "workspace": workspace,
                    "project": project,
                })
        return records

    @Slot()
    def run(self) -> None:
        try:
            action = self.command["cmd"]
            self.progress.emit(f"{action}: inspecting target")
            if action == "list-workspaces":
                records = []
                active_path = normalized_path_key(
                    self.command.get("active_workspace_path", "")
                ) if self.command.get("active_workspace_path") else ""
                for record in self._workspaces():
                    workspace = record["workspace"]
                    records.append({
                        "id": workspace.id,
                        "name": workspace.name,
                        "path": workspace.root_path,
                        "aggregate": record["project"].name,
                        "projects": [item.id for item in workspace.components],
                        "branches": [
                            item.task_branch for item in workspace.components
                            if item.task_branch
                        ],
                        "status": workspace.status,
                        "active": bool(
                            active_path
                            and normalized_path_key(workspace.root_path) == active_path
                        ),
                    })
                records.sort(key=lambda item: (
                    item["aggregate"].casefold(), item["name"].casefold(),
                ))
                self.done.emit({
                    "ok": True,
                    "active_workspace_name": next((
                        item["name"] for item in records if item["active"]
                    ), ""),
                    "workspaces": records,
                })
                return
            if action == "create-development-workspace":
                record, error = _match_target(
                    self._aggregates(), self.command.get("aggregate", ""), "aggregate",
                )
                if error:
                    self.done.emit(error)
                    return
                plan = build_workspace_creation_plan(
                    record["project"], self.command.get("name", ""),
                    self.command.get("description", ""),
                    self.command.get("projects", []),
                    progress=lambda current, total, message: self.progress.emit(f"{current}/{total}: {message}"),
                )
                result = create_development_workspace(plan, progress=lambda current, total, message: self.progress.emit(f"{current}/{total}: {message}"))
                self.done.emit({
                    "ok": result.ok,
                    "error": result.error,
                    "workspace": ({
                        "id": result.workspace.id,
                        "name": result.workspace.name,
                        "path": result.workspace.root_path,
                        "projects": [item.to_dict() for item in result.workspace.components],
                    } if result.workspace else None),
                    "created": list(result.created_components),
                    "rolled_back": list(result.rolled_back_components),
                    "pending": list(result.pending_components),
                    "pushed": list(result.pushed_components),
                    "local_workspace_preserved": not result.ok and result.workspace is not None,
                })
                return
            record, error = _match_target(
                self._workspaces(), self.command.get("target", ""),
                "development_workspace",
            )
            if error:
                self.done.emit(error)
                return
            workspace = record["workspace"]
            if action == "workspace-delete":
                active = self.command.get("active_workspace_paths", ())
                if normalized_path_key(workspace.root_path) in active:
                    self.done.emit({"ok": False, "code": "workspace_active",
                        "error": "close the specified workspace with --close-workspace before deleting it",
                        "workspace_path": workspace.root_path})
                    return
                plan = inspect_workspace_delete(workspace, self.running_paths)
                if not plan.can_delete:
                    self.done.emit({"ok": False, "code": "workspace_delete_blocked",
                        "error": "workspace deletion preflight failed", "blockers": list(plan.blockers)})
                    return
                self.progress.emit("deleting workspace directory and owned task branches")
                ok, remaining, error = delete_development_workspace(plan)
                self.done.emit({"ok": ok, "error": error,
                    "workspace_path": workspace.root_path,
                    "directory_deleted": not Path(workspace.root_path).exists(),
                    "remaining_branches": list(remaining),
                    "projects": [asdict(item) for item in plan.components]})
                return
            if action in ("workspace-merge", "workspace-sync-push"):
                self.progress.emit("merging and pushing base branches" if action == "workspace-merge"
                                   else "committing, synchronizing base changes and pushing task branches")
                if action == "workspace-merge":
                    result = merge_development_workspace(workspace)
                else:
                    result = sync_commit_and_push_development_workspace(
                        workspace, self.command.get("message", ""),
                        keep_conflicts=bool(self.command.get("keep_conflicts")))
                self.done.emit({"ok": result.ok, "error": result.error,
                    "workspace": {"id": result.workspace.id, "name": result.workspace.name,
                                  "path": result.workspace.root_path, "status": result.workspace.status},
                    "projects": [asdict(item) for item in result.components],
                    "needs_conflict_confirmation": getattr(result, "needs_conflict_confirmation", False)})
                return
            if action == "open-workspace":
                self.done.emit({
                    "ok": True,
                    "workspace": {
                        "id": workspace.id,
                        "name": workspace.name,
                        "path": workspace.root_path,
                    },
                    "_open_path": workspace.root_path,
                })
                return
            if action == "workspace-review":
                projects = review_development_workspace(workspace)
                workspace = load_development_workspace(
                    workspace.root_path, aggregate_project=record["project"],
                )
                self.done.emit({
                    "ok": True,
                    "workspace": {
                        "id": workspace.id, "name": workspace.name,
                        "path": workspace.root_path, "status": workspace.status,
                    },
                    "projects": [
                        {
                            "id": item.id,
                            "base_branch": item.base_branch,
                            "base_commit": item.base_commit,
                            "task_branch": item.task_branch,
                            "changed_files": item.changed_files,
                            "commits": list(item.commits),
                            "diff_stat": item.diff_stat,
                            "pushed": item.pushed,
                            "merged": item.merged,
                            "compile_result": item.compile_result,
                            "health_result": item.health_result,
                            "test_result": item.test_result,
                            "error": item.error,
                        }
                        for item in projects
                    ],
                })
                return
            if action == "workspace-commit-push":
                result = commit_and_push_development_workspace(
                    workspace, self.command.get("message", ""),
                )
                self.done.emit({
                    "ok": result.ok,
                    "error": result.error,
                    "workspace": {
                        "id": result.workspace.id,
                        "name": result.workspace.name,
                        "path": result.workspace.root_path,
                        "status": result.workspace.status,
                    },
                    "projects": [
                        {
                            "id": item.id,
                            "task_branch": item.task_branch,
                            "committed": item.committed,
                            "pushed": item.pushed,
                            "commit_id": item.commit_id,
                            "error": item.error,
                        }
                        for item in result.components
                    ],
                })
                return
            if action == "workspace-sync":
                result = sync_development_workspace(
                    workspace,
                    fetch_remote=bool(self.command.get("fetch_remote")),
                    keep_conflicts=bool(self.command.get("keep_conflicts")),
                )
                self.done.emit({
                    "ok": result.ok,
                    "error": result.error,
                    "workspace": {
                        "id": result.workspace.id, "name": result.workspace.name,
                        "path": result.workspace.root_path,
                        "status": result.workspace.status,
                    },
                    "synced": list(result.synced_ids),
                    "conflicted": list(result.conflicted_ids),
                    "needs_conflict_confirmation": result.needs_conflict_confirmation,
                    "projects": [
                        {
                            "id": item.id,
                            "base_branch": item.base_branch,
                            "task_branch": item.task_branch,
                            "merge_ref": item.merge_ref,
                            "behind_count": item.behind_count,
                            "synced": item.synced,
                            "already_current": item.already_current,
                            "conflicted": item.conflicted,
                            "rolled_back": item.rolled_back,
                            "conflict_files": list(item.conflict_files),
                            "new_base_commit": item.new_base_commit,
                            "error": item.error,
                        }
                        for item in result.components
                    ],
                })
                return
            if action == "workspace-delete-check":
                plan = inspect_workspace_delete(workspace, self.running_paths)
                self.done.emit({
                    "ok": True,
                    "workspace": {"id": workspace.id, "name": workspace.name, "path": workspace.root_path},
                    "can_delete": plan.can_delete,
                    "requires_confirmation": plan.needs_unmerged_confirmation,
                    "blockers": list(plan.blockers),
                    "warnings": list(plan.warnings),
                    "unknown_paths": list(plan.unknown_paths),
                    "projects": [
                        {
                            "id": item.id,
                            "base_branch": item.base_branch,
                            "task_branch": item.task_branch,
                            "merged": item.merged,
                            "pushed": item.base_pushed,
                            "base_pushed": item.base_pushed,
                            "base_remote_ref": item.base_remote_ref,
                            "dirty": False,
                            "branch_will_be_kept": False,
                            "error": item.error,
                        }
                        for item in plan.components
                    ],
                })
                return
            self.done.emit({"ok": False, "error": f"unsupported workspace command: {action}"})
        except Exception as exc:
            log.exception("CLI 开发工作区命令失败")
            self.done.emit({"ok": False, "error": str(exc)})

    @Slot(dict)
    def dispose(self, _payload: dict) -> None:
        self.deleteLater()


class _GitDiffResponder(QObject):
    def __init__(self, sock: QLocalSocket, thread: QThread, worker: QObject, parent=None):
        super().__init__(parent)
        self.sock = sock
        self.thread = thread
        self.worker = worker

    @Slot(dict)
    def receive(self, payload: dict) -> None:
        # PySide 对被子类覆盖的 Python slot 可能绕过 receiver 的线程归属。
        # 信号固定连接到这个未覆盖的入口，再由 GUI 线程分派具体处理。
        self.finish(payload.get("_inspection_payload", payload))

    @Slot(dict)
    def finish(self, payload: dict) -> None:
        if self.sock.state() == QLocalSocket.LocalSocketState.ConnectedState:
            _send_response(self.sock, payload)
        self.thread.quit()

    @Slot()
    def cleanup(self) -> None:
        try:
            _active_workers.remove((self.thread, self.worker, self))
        except ValueError:
            pass
        self.thread.deleteLater()
        self.deleteLater()


class _WorkspaceCommandResponder(_GitDiffResponder):
    def __init__(self, window: "MainWindow", *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.window = window
        self.command_name = getattr(self.worker, "command", {}).get("cmd", "")

    @Slot(dict)
    def finish(self, payload: dict) -> None:
        result = dict(payload)
        open_path = result.pop("_open_path", "")
        if result.get("ok") and open_path:
            if QThread.currentThread() != self.window.thread():
                log.critical("CLI 打开工作区结果未切回 GUI 主线程: %s", open_path)
                result = {
                    "ok": False,
                    "error": "open workspace callback is not on the GUI thread",
                }
            else:
                try:
                    tab = self.window.open_development_workspace(open_path, quiet=True)
                    if tab is None:
                        result = {
                            "ok": False,
                            "error": f"open workspace failed: {open_path}",
                        }
                    else:
                        result["environment"] = _environment_snapshot(tab)
                except EnvironmentSwitchBlocked as exc:
                    result = exc.result
                except Exception as exc:
                    log.exception("CLI 打开工作区失败: %s", open_path)
                    result = {"ok": False, "error": str(exc)}
        super().finish(result)
        if self.command_name in (
            "create-development-workspace", "workspace-sync", "workspace-sync-push",
            "workspace-commit-push", "workspace-merge", "workspace-delete",
        ):
            for tab in _all_environment_tabs(self.window):
                tab.refresh_workspaces()


_active_workers: list[tuple[QThread, QObject, QObject]] = []


def _connect_async_worker(worker: QObject, responder: _GitDiffResponder) -> None:
    """后台结果必须排队回 responder 所属的 GUI 线程。"""
    worker.done.connect(responder.receive, Qt.ConnectionType.QueuedConnection)
    worker.done.connect(worker.dispose, Qt.ConnectionType.DirectConnection)
    responder.thread.finished.connect(
        responder.cleanup, Qt.ConnectionType.QueuedConnection,
    )


def _run_git_diff_async(tab, mode: str, max_chars: int, sock: QLocalSocket) -> None:
    thread = BackgroundThread()
    worker = _GitDiffWorker(tab.project_meta.path, mode, max_chars)
    responder = _GitDiffResponder(sock, thread, worker)
    worker.moveToThread(thread)

    _connect_async_worker(worker, responder)
    thread.started.connect(worker.run)
    _active_workers.append((thread, worker, responder))
    thread.start()


def _run_workspace_command_async(cmd: dict, window: "MainWindow", sock: QLocalSocket) -> None:
    thread = BackgroundThread()
    running_paths = tuple(
        path
        for tab in _all_environment_tabs(window)
        if tab.running_service_items(refresh_external=True)
        for path in tab.runtime_paths()
    )
    command = dict(cmd)
    command["active_workspace_paths"] = tuple(
        normalized_path_key(tab.workspace.root_path)
        for tab in _all_environment_tabs(window) if tab.workspace is not None)
    current = window.tabs.currentWidget()
    active_workspace = getattr(current, "workspace", None)
    command["active_workspace_path"] = (
        active_workspace.root_path if active_workspace else ""
    )
    worker = _WorkspaceCommandWorker(
        command, tuple(window.config.aggregate_project_paths), running_paths,
    )
    if isinstance(sock, _OperationChannel):
        worker.progress.connect(sock.progress, Qt.ConnectionType.QueuedConnection)
    responder = _WorkspaceCommandResponder(window, sock, thread, worker)
    worker.moveToThread(thread)
    _connect_async_worker(worker, responder)
    thread.started.connect(worker.run)
    _active_workers.append((thread, worker, responder))
    thread.start()


def handle_cli_request(data: str, sock: QLocalSocket, window: "MainWindow") -> bool:
    """尝试处理 CLI JSON 请求。如果 data 不是 JSON 命令则返回 False（走老协议）。"""
    stripped = data.strip()
    if not stripped.startswith("{"):
        return False

    try:
        cmd = json.loads(stripped)
    except json.JSONDecodeError:
        return False

    if "cmd" not in cmd:
        return False

    log.info("[CLI] %s params=%s", cmd.get("cmd", "?"), {k: v for k, v in cmd.items() if k != "cmd"})

    if getattr(window, "_shutdown_in_progress", False) and cmd.get("cmd") not in (
        "status", "preflight-build", "quit",
    ):
        _send_response(sock, {"ok": False, "error": "IDE is preparing to quit"})
        return True

    try:
        result = _dispatch(cmd, window)
    except EnvironmentSwitchBlocked as exc:
        result = exc.result
    except Exception as e:
        log.exception("[CLI] %s EXCEPTION", cmd.get("cmd", "?"))
        result = {"ok": False, "error": str(e)}

    ok_flag = result.get("ok", True) if isinstance(result, dict) else True
    if ok_flag:
        log.info("[CLI] %s ok", cmd.get("cmd", "?"))
    else:
        log.warning("[CLI] %s fail: %s", cmd.get("cmd", "?"),
                    result.get("error", "") if isinstance(result, dict) else "")

    _send_response(sock, result)
    return True


def _send_response(sock: QLocalSocket, result: dict) -> None:
    """发送 JSON 响应并关闭连接。"""
    if isinstance(sock, _OperationChannel):
        sock.finish(result)
        return
    if isinstance(result, dict) and result.get("ok") is False:
        result.setdefault("code", result_code(result))
    payload = json.dumps(result, ensure_ascii=False) + "\n"
    sock.write(payload.encode("utf-8"))
    sock.flush()
    sock.disconnectFromServer()


def _dispatch(cmd: dict, window: "MainWindow") -> dict:
    """根据 cmd 类型路由到对应处理函数。"""
    action = cmd["cmd"]

    if action == "status":
        return _cmd_status(window)

    if action == "list-projects":
        return _cmd_list_projects(window)

    if action == "open":
        return _cmd_open(window, cmd.get("path", ""))

    if action == "resolve-target":
        return _cmd_resolve_target(window, cmd.get("path", ""))

    if action in ("list-workspaces", "open-workspace"):
        return {"ok": False, "error": f"{action} command uses async handler"}

    if action == "list-aggregates":
        return {"ok": True, "aggregates": _aggregate_records(window)}

    if action == "open-aggregate":
        return _cmd_open_aggregate(window, cmd.get("target", ""))

    if action in (
        "create-development-workspace", "workspace-review",
        "workspace-commit-push", "workspace-sync", "workspace-delete-check",
        "workspace-sync-push", "workspace-merge", "workspace-delete",
    ):
        return {"ok": False, "error": f"{action} command uses async handler"}

    if action == "close-workspace":
        return _cmd_close_workspace(window, cmd.get("target", ""))

    if action == "preflight-build":
        return _cmd_preflight_build(window, cmd.get("project", ""))

    if action == "quit":
        return _cmd_quit(window)

    # 以下命令都需要 project 参数
    project_key = cmd.get("project")
    if not project_key:
        return {"ok": False, "error": "missing 'project' argument"}

    tab, match_error = _find_project_tab(
        window, project_key, include_components=action != "close",
    )
    if match_error:
        return match_error

    if action == "list-modules":
        return _cmd_list_modules(tab)
    elif action == "list-runtimes":
        return _cmd_list_runtimes(tab)
    elif action == "close":
        return _cmd_close_project(window, tab)
    elif action == "start":
        return _cmd_start(tab, cmd.get("module"))
    elif action == "stop":
        return _cmd_stop(tab, cmd.get("module"))
    elif action == "restart":
        return _cmd_restart(tab, cmd.get("module"))
    elif action == "health":
        return {"ok": False, "error": "health command uses async handler"}
    elif action == "compile":
        return {"ok": False, "error": "compile command uses async handler"}
    elif action == "log":
        return _cmd_log(
            tab,
            cmd.get("module"),
            cmd.get("tail", 50),
            errors=bool(cmd.get("errors")),
            all_modules=bool(cmd.get("all_modules")),
            current_run=bool(cmd.get("current_run")), cursor=cmd.get("cursor", 0),
            run_id=cmd.get("run_id"), max_chars=cmd.get("max_chars", 30000),
        )
    elif action == "diagnose":
        return _cmd_diagnose(tab, cmd.get("module"), cmd.get("tail", 120),
            current_run=bool(cmd.get("current_run")), cursor=cmd.get("cursor", 0),
            run_id=cmd.get("run_id"), max_chars=cmd.get("max_chars", 30000))
    elif action == "git-status":
        return _cmd_git_status(tab)
    elif action in ("git-diff", "git-ai-context"):
        return {"ok": False, "error": f"{action} command uses async handler"}
    else:
        return {"ok": False, "error": f"unknown command: {action}"}


def handle_async_cli_request(
    cmd: dict, sock: QLocalSocket, window: "MainWindow"
) -> bool:
    from src.core.cli_contract import TRACKED_COMMANDS
    if cmd.get("cmd") == "operation":
        _cmd_operation_async(window, cmd["operation_id"], cmd.get("wait_seconds", 0), sock)
        return True
    if cmd.get("cmd") in TRACKED_COMMANDS:
        try:
            receipt, created = _operations(window).begin(cmd)
        except ValueError as exc:
            _send_response(sock, {"ok": False, "code": "operation_request_conflict", "error": str(exc)})
            return True
        if not created:
            if cmd.get("async"):
                _send_response(sock, {"ok": True, "accepted": receipt["status"] == "running", "operation_id": receipt["operation_id"], "operation": receipt})
            else:
                _cmd_operation_async(window, receipt["operation_id"], cmd.get("timeout", 600), sock, return_result=True)
            return True
        channel = _OperationChannel(window, sock, cmd, receipt)
        try:
            _handle_async_cli_request(cmd, channel, window)
        except Exception as exc:
            _send_response(channel, {"ok": False, "error": str(exc)})
        return True
    return _handle_async_cli_request(cmd, sock, window)


def _handle_async_cli_request(cmd, sock, window) -> bool:
    """处理需要异步等待的命令。返回 True 表示已接管。"""
    action = cmd.get("cmd")
    if getattr(window, "_shutdown_in_progress", False) and action not in (
        "status", "preflight-build", "quit",
    ):
        _send_response(sock, {"ok": False, "error": "IDE is preparing to quit"})
        return True
    if action == "quit":
        _cmd_quit_async(window, sock)
        return True
    if action == "context":
        _run_inspection_async(cmd, window, sock)
        return True
    if action in ("list-actions", "list-runtimes"):
        tab, match_error = _find_project_tab(window, cmd.get("project", ""))
        if tab is not None:
            if action == "list-runtimes":
                _send_response(sock, _cmd_list_runtimes(tab))
            else:
                from src.core.project_inspection import configured_actions
                _send_response(sock, {"ok": True, "projects": [{"path": tab.project_meta.path, "actions": configured_actions(tab.project_meta)}]})
        elif is_path_target(cmd.get("project", "")):
            _run_inspection_async(cmd, window, sock)
        else:
            _send_response(sock, match_error)
        return True
    if action in (
        "list-workspaces", "open-workspace", "create-development-workspace",
        "workspace-review", "workspace-commit-push", "workspace-sync",
        "workspace-delete-check", "workspace-sync-push", "workspace-merge", "workspace-delete",
    ):
        _run_workspace_command_async(cmd, window, sock)
        return True
    if action not in (
        "health", "compile", "run-action", "ensure-running", "start", "stop", "restart",
        "git-diff", "git-ai-context",
    ):
        return False

    project_key = cmd.get("project")
    if not project_key:
        _send_response(sock, {"ok": False, "error": "missing 'project' argument"})
        return True

    tab, match_error = _find_project_tab(window, project_key)
    if match_error:
        _send_response(sock, match_error)
        return True

    if action == "health":
        _cmd_health_async(tab, cmd.get("module"), cmd.get("timeout", 60), sock, http=bool(cmd.get("http")))
        return True
    elif action in ("compile", "run-action"):
        _cmd_compile_async(tab, sock, cmd.get("timeout", 300), cmd.get("action"))
        return True
    elif action == "ensure-running":
        _cmd_ensure_running_async(tab, cmd.get("module"), cmd.get("timeout", 60), sock)
        return True
    elif action in ("start", "stop", "restart"):
        _cmd_service_async(tab, action, cmd.get("module"), cmd.get("timeout", 60), sock, bool(cmd.get("wait")))
        return True
    elif action in ("git-diff", "git-ai-context"):
        mode = cmd.get("mode", "summary")
        max_chars = int(cmd.get("max_chars", 30000))
        _run_git_diff_async(tab, mode, max_chars, sock)
        return True
    return False


# ---- 项目查找 ----

def _find_project_tab(
    window: "MainWindow", key: str, *, include_components: bool = True,
):
    """确定性匹配项目，返回 (tab, error_payload)。"""
    tabs = _project_target_tabs(window, include_components=include_components)
    key = key.strip()
    key_folded = key.casefold()

    def normalized_path(value: str) -> str:
        return normalized_path_key(value)

    def stable_id(tab) -> str:
        meta = tab.project_meta
        return str(
            getattr(tab, "component_id", "")
            or getattr(tab, "stable_id", "")
            or getattr(meta, "component_id", "")
            or getattr(meta, "stable_id", "")
            or ""
        )

    def candidate(tab) -> dict:
        path = str(tab.project_meta.path)
        return {
            "id": stable_id(tab) or Path(path).name,
            "name": tab.project_meta.name,
            "path": path,
            "type": tab.project_meta.project_type,
        }

    def resolved(matches: list, match_kind: str):
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            candidates = sorted(
                (candidate(tab) for tab in matches),
                key=lambda item: normalized_path(item["path"]),
            )
            return None, {
                "ok": False,
                "code": "ambiguous_project",
                "error": f"ambiguous project: {key}",
                "match": match_kind,
                "candidates": candidates,
            }
        return None

    exact_path = [
        tab for tab in tabs
        if is_path_target(key) and normalized_path(tab.project_meta.path) == normalized_path(key)
    ]
    result = resolved(exact_path, "path")
    if result:
        return result

    exact_id_or_dir = []
    for tab in tabs:
        path = str(tab.project_meta.path)
        identifiers = {Path(path).name.casefold()}
        tab_stable_id = stable_id(tab)
        if tab_stable_id:
            identifiers.add(tab_stable_id.casefold())
        if key_folded in identifiers:
            exact_id_or_dir.append(tab)
    result = resolved(exact_id_or_dir, "id")
    if result:
        return result

    fuzzy = []
    for tab in tabs:
        path = str(tab.project_meta.path)
        values = {
            normalized_path(path),
            Path(path).name.casefold(),
            str(tab.project_meta.name).casefold(),
        }
        tab_stable_id = stable_id(tab)
        if tab_stable_id:
            values.add(tab_stable_id.casefold())
        if any(key_folded in value for value in values):
            fuzzy.append(tab)
    result = resolved(fuzzy, "fuzzy")
    if result:
        return result

    return None, {
        "ok": False,
        "code": "project_not_found",
        "error": f"project not found: {key}",
    }


def _cmd_open_aggregate(window: "MainWindow", target: str) -> dict:
    if is_path_target(target):
        try:
            path = load_aggregate_project(target).root_path
        except Exception as exc:
            return {"ok": False, "code": "invalid_aggregate_config", "error": str(exc)}
    else:
        record, error = _match_target(_aggregate_records(window), target, "aggregate")
        if error:
            return error
        if record.get("error"):
            return {"ok": False, "code": "invalid_aggregate_config", "error": record["error"]}
        path = record["path"]
    tab = window.open_aggregate_project(path, quiet=True)
    if tab is None:
        return {"ok": False, "error": f"open aggregate failed: {target}"}
    return {"ok": True, "environment": _environment_snapshot(tab)}


# ---- 命令实现 ----

def _stop_tab_all(tab) -> tuple[int, bool]:
    """停掉一个 ProjectTab 下所有运行中的服务，返回 (服务数, 是否全部停净)。

    覆盖三类进程，与关闭窗口时的清理保持一致：
      1. mini-ide 自己拉起的（单模块主 runner / 多模块 module_runners / 脚本 runner）
      2. 多模块里被外部（CLI/终端/IDE）启动、靠端口快照感知到的孤儿进程 → 按 PID 杀
    silent=True 全程不弹确认框（CLI 场景无人值守）。
    """
    from src.ui.project_tab import ProjectTab
    tab: ProjectTab
    count = tab.stop_all_services(include_external=True, silent=True)
    stopped = True
    if count:
        stopped = tab.wait_services_stopped(timeout_ms=15000)
    return count, stopped


def _cmd_status(window: "MainWindow") -> dict:
    from src.ui.aggregate_project_tab import AggregateProjectTab

    current = window.tabs.currentWidget()
    current_project = None
    if current is not None and hasattr(current, "project_meta"):
        current_project = {
            "name": current.project_meta.name,
            "path": current.project_meta.path,
        }
    projects = [_project_snapshot(tab) for tab in _all_project_tabs(window)]
    environments = [_environment_snapshot(tab) for tab in _all_environment_tabs(window)]
    active_workspace = (
        current.workspace if isinstance(current, AggregateProjectTab) else None
    )
    workspaces = _development_workspace_records(window)
    running = []
    for p in projects:
        running.extend(p.get("running", []))
    for environment in environments:
        running.extend(environment.get("running", []))
    return {
        "ok": True,
        "ide": "mini-ide",
        "version": _app_version(),
        "build_time": build_info()["build_time"],
        "project_count": len(projects) + len(environments),
        "current_project": current_project,
        "active_workspace_name": active_workspace.name if active_workspace else "",
        "startup_restore_mode": window.config.startup_restore_mode,
        "projects": projects,
        "environments": environments,
        "aggregates": _aggregate_records(window),
        "running": running,
        "workspaces": workspaces,
    }


def _cmd_open(window: "MainWindow", path: str) -> dict:
    if not path:
        return {"ok": False, "error": "missing 'path' argument"}
    try:
        resolved = resolve_target_path(path, window.config.aggregate_project_paths)
    except TargetResolutionError as exc:
        return {"ok": False, "error": str(exc)}
    tab = window.open_project(resolved.path, quiet=True)
    if tab is None:
        return {"ok": False, "error": f"open project failed: {path}"}
    if tab in _all_environment_tabs(window):
        result = {"ok": True, "environment": _environment_snapshot(tab)}
        result["target"] = resolved.to_dict()
        return result
    return {"ok": True, "project": _project_snapshot(tab), "target": resolved.to_dict()}


def _cmd_resolve_target(window: "MainWindow", path: str) -> dict:
    if not path:
        return {"ok": False, "error": "missing 'path' argument"}
    try:
        return resolve_target_path(
            path, window.config.aggregate_project_paths,
        ).to_dict()
    except TargetResolutionError as exc:
        return {"ok": False, "error": str(exc)}


def _cmd_close_project(window: "MainWindow", tab) -> dict:
    running_before = tab.running_service_items(refresh_external=True)
    name = tab.project_meta.name
    path = tab.project_meta.path
    ok = window.close_project_tab(tab, quiet=True)
    if not ok:
        return {
            "ok": False,
            "error": "close project failed",
            "project": {"name": name, "path": path},
            "running": running_before,
        }
    return {
        "ok": True,
        "project": {"name": name, "path": path},
        "stopped": len(running_before),
        "services": running_before,
    }


def _cmd_close_workspace(window: "MainWindow", target: str) -> dict:
    from src.ui.aggregate_project_tab import AggregateProjectTab

    records = [
        {"id": tab.workspace.id, "name": tab.workspace.name, "path": tab.workspace.root_path, "tab": tab}
        for tab in _all_environment_tabs(window) if tab.workspace is not None
    ]
    record, error = _match_target(records, target, "workspace")
    if error:
        return error
    tab = record["tab"]
    workspace = tab.workspace
    running = tab.running_service_items(refresh_external=True)
    stopped_count = tab.stop_all_services(include_external=True, silent=True)
    if stopped_count and not tab.wait_services_stopped(20000):
        return {
            "ok": False, "error": "stop timeout", "services": running,
        }
    if not tab.activate_workspace(None, interactive=False):
        return {"ok": False, "error": "workspace could not return to source projects"}
    window._save_tab_session()
    return {
        "ok": True,
        "workspace": {"id": workspace.id, "name": workspace.name, "path": workspace.root_path},
        "stopped": stopped_count,
        "services": running,
    }


def _cmd_preflight_build(window: "MainWindow", project_key: str = "") -> dict:
    """检查构建目标；不指定项目时保留退出前的全局检查语义。"""

    if project_key:
        tab, match_error = _find_project_tab(window, project_key)
        if match_error:
            return match_error
        try:
            running = tab.running_service_items(refresh_external=True)
        except Exception:
            log.exception("收集目标项目运行服务失败: %s", tab.project_meta.name)
            return {
                "ok": False,
                "error": "failed to inspect target project services",
                "can_build": False,
                "running": [],
            }
        error = "target project services must be stopped before build"
    else:
        running = _collect_running(window)
        error = "running services must be stopped before build/quit"
    if running:
        return {
            "ok": False,
            "error": error,
            "can_build": False,
            "running": running,
        }
    return {"ok": True, "can_build": True, "running": []}


def _cmd_quit_async(window: "MainWindow", sock: QLocalSocket) -> None:
    from PySide6.QtCore import QTimer

    log.info("[CLI] quit: 开始保存文件与安全退出检查")

    def complete(result: dict) -> None:
        log.info("[CLI] quit %s", "ok" if result.get("ok") else result.get("error", "fail"))
        try:
            if sock.state() == QLocalSocket.LocalSocketState.ConnectedState:
                _send_response(sock, result)
        except RuntimeError:
            pass
        if result.get("ok"):
            QTimer.singleShot(300, window.finish_application_quit)

    window.prepare_application_quit(complete, stop_running=True, quiet=True)


def _cmd_quit(window: "MainWindow") -> dict:
    return {"ok": False, "error": "quit command uses async handler"}


def _cmd_list_projects(window: "MainWindow") -> list:
    projects = []
    for w in _project_target_tabs(window):
        modules = [unit.id for unit in w.project_meta.runtime_units]
        projects.append({
            "name": w.project_meta.name,
            "path": w.project_meta.path,
            "type": w.project_meta.project_type,
            "modules": modules,
            "runtime_units": [unit.to_dict() for unit in w.project_meta.runtime_units],
        })
    return projects


def _cmd_list_modules(tab) -> list:
    from src.ui.project_tab import ProjectTab
    tab: ProjectTab
    states = tab.service_controller.states(refresh_external=True)
    # 对 CLI 来说，脚本运行不是“模块”，避免 list-modules 混入右键脚本进程。
    modules = [s.to_cli_module() for s in states if s.kind != "script"]
    return modules


def _cmd_list_runtimes(tab) -> list:
    states = {
        state.module: state.to_cli_module()
        for state in tab.service_controller.states(refresh_external=True)
        if state.kind != "script"
    }
    result = []
    for unit in tab.project_meta.runtime_units:
        item = unit.to_dict()
        item["state"] = states.get(unit.id, {"name": unit.id, "state": "stopped"})
        result.append(item)
    return result


def _cmd_start(tab, module: str | None) -> dict:
    return tab.service_controller.start(module)


def _cmd_stop(tab, module: str | None) -> dict:
    result = tab.service_controller.stop(module)
    if result.get("ok") is False and result.get("error") not in ("not running",):
        log.warning("CLI stop failed: %s", result)
    return result


def _cmd_restart(tab, module: str | None) -> dict:
    return tab.service_controller.restart(module)


def _cmd_log(
    tab,
    module: str | None,
    tail: int,
    errors: bool = False,
    all_modules: bool = False,
    current_run: bool = False,
    cursor: int = 0,
    run_id: str | None = None,
    max_chars: int = 30000,
) -> dict:
    """获取最近 N 行日志。支持错误过滤和多模块汇总。"""
    from src.ui.project_tab import ProjectTab
    tab: ProjectTab

    if current_run or run_id is not None or cursor:
        if all_modules:
            return {"ok": False, "code": "invalid_arguments", "error": "current-run cursors require one explicit runtime, not --all-modules"}
        invalid = _validate_module(tab, module)
        if invalid:
            return invalid
        target = tab.service_controller.launch_target(module)
        if tab._is_multi_module and not module:
            return {"ok": False, "code": "invalid_arguments", "error": "--current-run requires a module for multi-runtime projects"}
        launch = tab.service_controller._launches.state(target)
        if launch is None:
            return {"ok": False, "code": "run_logs_unavailable", "error": "no managed launch output; external services do not have current-run logs"}
        result = launch.output.read(tail=tail, cursor=cursor, errors=errors, max_chars=max_chars, run_id=run_id)
        result.update(generation=launch.generation, module=target, project=tab.project_meta.path)
        return result

    from src.core.run_logs import error_blocks

    if all_modules and tab._is_multi_module:
        logs: dict[str, list[str]] = {}
        for unit in tab.project_meta.runtime_units:
            mod_name = unit.id
            lines = _iter_log_lines(tab.module_logs.get(mod_name), tail)
            if errors:
                lines = error_blocks(lines)
            logs[mod_name] = lines
        project_lines = _iter_log_lines(tab.log, tail)
        if errors:
            project_lines = error_blocks(project_lines)
        return {"ok": True, "logs": logs, "project_lines": project_lines}

    invalid = _validate_module(tab, module)
    if invalid:
        return invalid
    lines = _iter_log_lines(_log_widget_for(tab, module), tail)
    if errors:
        lines = error_blocks(lines)
    return {"ok": True, "lines": lines}


def _cmd_diagnose(tab, module: str | None, tail: int, **log_options) -> dict:
    invalid = _validate_module(tab, module)
    if invalid:
        return invalid
    snapshot = _project_snapshot(tab)
    target_states = snapshot["modules"]
    if module:
        target_states = [s for s in target_states if s.get("name") == module]
    log_widget = _log_widget_for(tab, module)
    scoped = None
    if log_options.get("current_run") or log_options.get("cursor") or log_options.get("run_id"):
        scoped = _cmd_log(tab, module, tail, **log_options)
        if not scoped.get("ok"):
            return scoped
    lines = scoped["lines"] if scoped else _iter_log_lines(log_widget, tail)
    from src.core.run_logs import error_blocks
    error_lines = error_blocks(lines)
    diagnosis = ""
    if not scoped and log_widget is not None and hasattr(log_widget, "diagnosis_summary"):
        diagnosis = log_widget.diagnosis_summary(max_chars=4000)
    hints = []
    for state in target_states:
        if state.get("detail_state") == "running_external":
            hints.append("服务由 mini-ide 外部进程占用，当前 IDE 没有完整启动日志；建议停止后由 mini-ide 重新启动。")
        elif state.get("detail_state") == "stopped" and not lines:
            hints.append("当前没有运行进程，也没有本次日志上下文。")
        elif error_lines:
            hints.append("近期日志包含错误关键字，请优先查看 error_lines。")
    result = {
        "ok": True,
        "project": {
            "name": tab.project_meta.name,
            "path": tab.project_meta.path,
            "type": tab.project_meta.project_type,
            "default_port": tab.project_meta.default_port,
        },
        "module": module,
        "states": target_states,
        "diagnosis": diagnosis,
        "error_lines": error_lines,
        "recent_lines": lines[-tail:],
        "hints": list(dict.fromkeys(hints)),
    }
    if scoped:
        result["log_context"] = {key: value for key, value in scoped.items() if key not in ("ok", "lines")}
    return result


def _cmd_git_status(tab) -> dict:
    return git_context.summary_payload(tab.project_meta.path, include_files=True)


def _get_health_markers(project_type: str) -> tuple[str, ...]:
    """兼容旧内部调用；标记的唯一来源在 launch_tracker。"""
    from src.core.launch_tracker import health_markers
    return health_markers(project_type)


def _health_checks(detail):
    source = detail.get("source", "pending")
    return {"process": {"status": "passed" if detail.get("pid") else "not_observed", "pid": detail.get("pid")},
            "tcp": {"status": "passed" if source in ("port", "external", "nginx") else "not_checked",
                    "port": detail.get("port"), "ports": detail.get("ports", [])},
            "startup": {"status": "passed" if source in ("log", "port", "external", "nginx") else "pending", "evidence": source},
            "http": {"status": "not_checked"}}


class _HttpHealthWorker(QObject):
    done = Signal(dict)

    def __init__(self, targets, payload, timeout):
        super().__init__()
        self.targets, self.payload, self.timeout = targets, payload, timeout

    @Slot()
    def run(self):
        import urllib.request
        import urllib.error
        observations = []
        deadline = time.monotonic() + self.timeout
        for module, port, path in self.targets:
            observation = {"module": module, "status": "not_configured"}
            if port and path and path.startswith("/") and not path.startswith("//"):
                url = f"http://127.0.0.1:{port}{path}"
                try:
                    request = urllib.request.Request(url, headers={"User-Agent": "mini-ide-health"})
                    class NoRedirect(urllib.request.HTTPRedirectHandler):
                        def redirect_request(self, *args, **kwargs):
                            return None
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
                    with opener.open(request, timeout=max(0.1, deadline - time.monotonic())) as response:
                        observation.update(status="passed" if 200 <= response.status < 300 else "failed", status_code=response.status)
                        try:
                            body = json.loads(response.read(4096))
                            if isinstance(body, dict) and body.get("status") in ("DOWN", "OUT_OF_SERVICE"):
                                observation["status"] = "failed"
                        except (ValueError, UnicodeDecodeError):
                            pass
                except urllib.error.HTTPError as exc:
                    observation.update(status="auth_required" if exc.code in (401, 403) else "failed", status_code=exc.code)
                except (OSError, ValueError) as exc:
                    observation.update(status="failed", reason=str(exc))
            observations.append(observation)
        result = dict(self.payload)
        passed = bool(observations) and all(item["status"] == "passed" for item in observations)
        result["checks"] = {**result.get("checks", {}), "http": {"status": "passed" if passed else "failed", "observations": observations}}
        if not passed:
            result.update(ok=False, code="http_health_failed", error="configured HTTP health check did not pass")
        self.done.emit(result)


def _http_health_async(tab, module, payload, timeout, sock):
    targets = [(unit.id, unit.expected_port or tab.project_meta.default_port,
                unit.health_check_path or tab.project_meta.health_check_path)
               for unit in tab.project_meta.runtime_units if not module or unit.id == module]
    thread = BackgroundThread()
    worker = _HttpHealthWorker(targets, payload, timeout)
    responder = _GitDiffResponder(sock, thread, worker)
    worker.moveToThread(thread)
    worker.done.connect(responder.receive, Qt.ConnectionType.QueuedConnection)
    worker.done.connect(thread.quit)
    thread.started.connect(worker.run)
    thread.finished.connect(worker.deleteLater)
    thread.finished.connect(responder.cleanup)
    _active_workers.append((thread, worker, responder))
    thread.start()


def _cmd_health_async(
    tab,
    module: str | None,
    timeout: int,
    sock: QLocalSocket,
    generations: dict[str, int | None] | None = None,
    http: bool = False,
) -> None:
    """异步等待模块启动完成，检测日志中的启动标记或端口监听。"""
    from src.ui.project_tab import ProjectTab
    tab: ProjectTab

    start_time = time.time()
    deadline = start_time + timeout
    last_observation = {}

    def finish(payload):
        timer.stop()
        details = payload.get("modules")
        if details:
            payload["modules"] = [{**item, "checks": _health_checks(item)} for item in details]
            payload["checks"] = {"http": {"status": "not_checked"}}
        else:
            payload["checks"] = _health_checks(payload if "source" in payload else last_observation)
        payload["verification"] = "startup_readiness"
        if http and payload.get("ok"):
            _http_health_async(tab, module, payload, max(0.1, deadline - time.time()), sock)
        else:
            _send_response(sock, payload)

    if generations is None:
        generations = tab.service_controller.snapshot_launch_generations(module)

    def _generation(target: str) -> int | None:
        return generations.get(target) if generations else None

    def _terminal_error(detail: dict) -> bool:
        error = detail.get("error")
        if not error:
            return False
        payload = {"ok": False, "error": error}
        payload.update(detail)
        finish(payload)
        timer.stop()
        timer.deleteLater()
        return True

    def _check():
        now = time.time()
        # 客户端已断开就别再空转 / 往死 socket 写
        if sock.state() != QLocalSocket.LocalSocketState.ConnectedState:
            timer.stop()
            timer.deleteLater()
            return
        if now >= deadline:
            finish({"ok": False, "code": "health_timeout", "error": "startup readiness timeout",
                    "last_observation": dict(last_observation), "elapsed_ms": int((now-start_time)*1000)})
            timer.stop()
            timer.deleteLater()
            return

        if tab._is_multi_module:
            if module:
                ok, detail = tab.service_controller.module_health_ok(
                    module, _generation(module),
                )
                last_observation.update(detail)
                if ok:
                    elapsed_ms = int((time.time() - start_time) * 1000)
                    payload = {"ok": True, "elapsed_ms": elapsed_ms}
                    payload.update(detail)
                    finish(payload)
                    timer.stop()
                    timer.deleteLater()
                    return
                if _terminal_error(detail):
                    return
            else:
                details = []
                all_ok = True
                for unit in tab.project_meta.runtime_units:
                    mod_name = unit.id
                    ok, detail = tab.service_controller.module_health_ok(
                        mod_name, _generation(mod_name),
                    )
                    details.append(detail)
                    last_observation[mod_name] = detail
                    all_ok = all_ok and ok
                    if not ok and detail.get("error"):
                        finish({
                            "ok": False,
                            "error": detail["error"],
                            "module": mod_name,
                            "modules": details,
                        })
                        timer.stop()
                        timer.deleteLater()
                        return
                if details and all_ok:
                    elapsed_ms = int((time.time() - start_time) * 1000)
                    finish({
                        "ok": True,
                        "elapsed_ms": elapsed_ms,
                        "modules": details,
                    })
                    timer.stop()
                    timer.deleteLater()
                    return
            return

        target = tab.service_controller.target_modules(None)[0]
        ok, detail = tab.service_controller.single_project_health_ok(
            _generation(target),
        )
        last_observation.update(detail)
        if ok:
            elapsed_ms = int((time.time() - start_time) * 1000)
            payload = {"ok": True, "elapsed_ms": elapsed_ms}
            payload.update(detail)
            finish(payload)
            timer.stop()
            timer.deleteLater()
            return
        if _terminal_error(detail):
            return

    timer = QTimer()
    timer.setInterval(100)
    timer.timeout.connect(_check)
    timer.start()
    # 立即检查一次
    _check()


def _active_modules(tab, module: str | None) -> list[str]:
    return tab.service_controller.active_modules(module)


def _target_modules(tab, module: str | None) -> list[str]:
    return tab.service_controller.target_modules(module)


def _cmd_ensure_running_async(
    tab,
    module: str | None,
    timeout: int,
    sock: QLocalSocket,
) -> None:
    invalid = _validate_module(tab, module)
    if invalid:
        _send_response(sock, invalid)
        return
    targets = _target_modules(tab, module)
    active = set(_active_modules(tab, module))
    already = all(name in active for name in targets)
    if not already:
        _cmd_service_async(tab, "start", module, timeout, sock, True, allow_already=True)
        return
    generations = tab.service_controller.snapshot_launch_generations(module)
    _cmd_health_async(tab, module, timeout, sock, generations)


def _cmd_service_async(tab, action, module, timeout, sock, wait=False, allow_already=False):
    started_at = time.monotonic()
    def completed(result):
        try:
            connected = sock.state() == QLocalSocket.LocalSocketState.ConnectedState
        except RuntimeError:
            return
        if not connected:
            return
        if not result.get("ok") and not (allow_already and result.get("error") == "already running"):
            _send_response(sock, result)
            return
        if wait and action != "stop":
            remaining = max(0, timeout - (time.monotonic() - started_at))
            generations = tab.service_controller.snapshot_launch_generations(module)
            _cmd_health_async(tab, module, remaining, sock, generations)
        else:
            _send_response(sock, result)
    budget_ms = max(0, timeout) * 1000
    tab.service_controller.request(action, module, completed, min(15000, budget_ms) if action == "stop" else budget_ms)


def _cmd_start_or_restart_wait_async(tab, action, module, timeout, sock):
    _cmd_service_async(tab, action, module, timeout, sock, True, allow_already=True)


def _cmd_compile_async(tab, sock: QLocalSocket, timeout: int = 300, action: str | None = None) -> None:
    """触发编译并等待完成。"""
    from src.ui.project_tab import ProjectTab
    tab: ProjectTab

    prepared = (tab.service_controller.prepare_action(action) if action
                else tab.service_controller.prepare_compile())
    if not prepared.get("ok"):
        _send_response(sock, prepared)
        return
    runner = prepared["runner"]

    # finished/output 信号必须在 runner.start 之前连接。极快命令可能在 start 返回前
    # 就结束，先启动再 connect 会永远收不到 finished，只能错误地等到超时。
    state = {"done": False}
    output_lines: list[str] = []

    def _collect_output() -> str:
        return "\n".join(output_lines[-200:])

    def _on_output(_stream: str, line: str) -> None:
        if line:
            if isinstance(sock, _OperationChannel):
                sock.progress(line)
            output_lines.append(line)
            if len(output_lines) > 200:
                del output_lines[:-200]

    def _finish(payload: dict) -> None:
        if state["done"]:
            return
        state["done"] = True
        deadline_timer.stop()
        deadline_timer.deleteLater()
        try:
            runner.finished.disconnect(_on_finished)
        except (RuntimeError, TypeError):
            pass
        try:
            runner.outputLine.disconnect(_on_output)
        except (RuntimeError, TypeError):
            pass
        if sock.state() == QLocalSocket.LocalSocketState.ConnectedState:
            _send_response(sock, payload)

    def _on_finished(exit_code: int):
        output = _collect_output()
        ok = exit_code == 0
        profile = prepared.get("profile")
        result = {"ok": ok, "exit_code": exit_code, "output": output,
                  "action": {"id": getattr(profile, "name", "compile"),
                             "kind": getattr(profile, "kind", "compile"),
                             "label": getattr(profile, "label", "")}}
        if not ok:
            result.update(code="action_failed", error="configured action exited unsuccessfully")
        _finish(result)

    def _on_deadline():
        # 超时：编译还在跑，返回超时（不强停编译，让它在 GUI 里继续）
        _finish({"ok": False, "error": "compile timeout", "output": _collect_output()})

    deadline_timer = QTimer()
    deadline_timer.setSingleShot(True)
    deadline_timer.setInterval(max(1, timeout) * 1000)
    deadline_timer.timeout.connect(_on_deadline)
    if not isinstance(sock, _OperationChannel):
        deadline_timer.start()

    runner.outputLine.connect(_on_output)
    runner.finished.connect(_on_finished)
    try:
        started = tab.service_controller.start_prepared_compile(prepared)
    except Exception as exc:
        tab.service_controller.abort_prepared_compile()
        log.exception("CLI compile start failed")
        _finish({"ok": False, "error": f"failed to start compile: {exc}"})
        return
    if not started.get("ok"):
        _finish(started)
