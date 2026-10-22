"""隔离仓库验证工作区预检、线程回收、进度、取消、超时及创建回滚。"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

SANDBOX = tempfile.TemporaryDirectory(prefix="mini-ide-workspace-creation-")
os.environ["APPDATA"] = str(Path(SANDBOX.name) / "config")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.environ.get("MINI_IDE_TEST_RUNTIME", str(Path(__file__).resolve().parents[1])))

import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QTimer, Qt, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QMessageBox, QPushButton
from src.core import development_workspace_service as service
from src.core.aggregate_workspace import load_aggregate_project
from src.core.config import AppConfig
from src.core.search_worker_lifecycle import BackgroundThread, stop_background_workers
from src.ui import aggregate_project_tab as aggregate_ui
from src.ui import development_workspace_dialog as dialog_ui
from src.ui.theme import apply_theme

app = QApplication.instance() or QApplication([])
for font_name in ("segoeui.ttf", "msyh.ttc"):
    font = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / font_name
    if font.is_file():
        QFontDatabase.addApplicationFont(str(font))
apply_theme(app, "github-dark")
gates = []
root = Path(SANDBOX.name) / "suite"


def pump(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "workspace creation regression timed out"
        app.processEvents()
        time.sleep(0.002)
    app.processEvents()


def git(repo, *args):
    result = subprocess.run(
        [service.resolve_git_executable(), "-c", "user.name=Workspace Test",
         "-c", "user.email=workspace@example.invalid", *args],
        cwd=repo, capture_output=True, text=True, creationflags=service.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def fixture():
    root.mkdir()
    for name in ("server", "webapp"):
        repo = root / name
        repo.mkdir()
        git(repo, "init", "-b", "main")
        (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "temporary fixture")
        remote = Path(SANDBOX.name) / (name + "-origin.git")
        git(root, "init", "--bare", str(remote))
        git(repo, "remote", "add", "origin", str(remote))
        git(repo, "push", "--set-upstream", "origin", "main")
    (root / "nginx").mkdir()
    (root / ".mini-ide").mkdir()
    (root / ".mini-ide/project.json").write_text(json.dumps({
        "schemaVersion": 1, "id": "suite", "name": "suite", "kind": "aggregate",
        "workspaceDirectory": "workspace",
        "components": [{"id": "server", "path": "server", "type": "generic"},
                       {"id": "webapp", "path": "webapp", "type": "generic"},
                       {"id": "nginx", "path": "nginx", "type": "nginx", "shared": True}],
    }), encoding="utf-8")
    return load_aggregate_project(root)


def new_dialog(project, name="preflight"):
    dialog = dialog_ui.DevelopmentWorkspaceDialog(project)
    dialog.task_name.setText(name)
    dialog.description.setPlainText("preserve this description")
    for row in range(dialog.table.rowCount()):
        item = dialog.table.item(row, 0)
        if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
            item.setCheckState(Qt.CheckState.Checked)
    return dialog


def save_button(dialog):
    return dialog.buttons.button(QDialogButtonBox.StandardButton.Save)


def reclaim_before_results(worker):
    assert worker.wait(6000)
    QCoreApplication.sendPostedEvents(worker, QEvent.Type.MetaCall)
    QCoreApplication.sendPostedEvents(worker, QEvent.Type.DeferredDelete)
    assert not shiboken6.isValid(worker)


def lifecycle_check(project):
    # 旧代码的 sender() 身份检查，在发送者先回收时会忽略已完成的结果。
    class LegacyWorker(BackgroundThread):
        done = Signal()

        def run(self):
            self.done.emit()

    class Receiver(QObject):
        sender_was_lost = False

        def receive(self):
            self.sender_was_lost = self.sender() is None

    receiver = Receiver()
    legacy = LegacyWorker()
    legacy.done.connect(receiver.receive)
    legacy.finished.connect(legacy.deleteLater)
    legacy.start()
    reclaim_before_results(legacy)
    app.processEvents()
    assert receiver.sender_was_lost, "original result-loss ordering was not reproduced"

    dialog = new_dialog(project)
    confirmations = []
    with patch.object(QMessageBox, "question", lambda *a, **k: confirmations.append(True) or QMessageBox.StandardButton.No):
        dialog.accept()
        worker = dialog._worker
        assert not save_button(dialog).isEnabled()
        reclaim_before_results(worker)
        dialog._poll_plan()
        assert dialog._worker is None and save_button(dialog).isEnabled() and confirmations
        assert dialog.result_plan() is None
        assert "未创建" in dialog.progress_label.text()
    dialog.reject()
    dialog.deleteLater()
    modal = new_dialog(project, "modal-preflight")
    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
        QTimer.singleShot(0, save_button(modal).click)
        guard = QTimer(modal)
        guard.setSingleShot(True)
        guard.timeout.connect(modal.reject)
        guard.start(6000)
        assert modal.exec() == QDialog.DialogCode.Accepted
        guard.stop()
        assert modal.result_plan() is not None and not Path(modal.result_plan().root_path).exists()
    modal.deleteLater()
    print("[OK] reproduced lost sender; completed result survives native thread deletion", flush=True)


def recovery_checks(project):
    plan = service.build_workspace_creation_plan(project, "preflight", "", ("server", "webapp"))
    dialog = new_dialog(project)
    with patch.object(dialog_ui, "build_workspace_creation_plan", side_effect=RuntimeError("injected read failure")):
        dialog.accept()
        pump(lambda: dialog._worker is None)
        assert save_button(dialog).isEnabled() and "injected read failure" in dialog.progress_label.text()
        assert dialog.task_name.text() == "preflight" and "preserve" in dialog.description.toPlainText()
    gate = threading.Event()
    entered = threading.Event()
    gates.append(gate)

    def slow_plan(*args, **kwargs):
        kwargs["progress"](3, 17, "server：读取基准提交")
        entered.set()
        assert gate.wait(8)
        return plan

    with patch.object(dialog_ui, "build_workspace_creation_plan", slow_plan):
        dialog.show()
        dialog.accept()
        pump(lambda: entered.is_set() and "server：读取基准提交" in dialog.progress_label.text())
        old_worker = dialog._worker
        assert "已用" in dialog.progress_label.text()
        assert dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel).isEnabled()
        assert not dialog.task_name.isEnabled()
        screenshot_path = os.environ.get("MINI_IDE_PREFLIGHT_SCREENSHOT")
        if screenshot_path:
            assert dialog.grab().save(screenshot_path)
        dialog._started -= dialog_ui.PREFLIGHT_TIMEOUT_SECONDS + 1
        dialog._poll_plan()
        assert dialog._worker is None and save_button(dialog).isEnabled()
        assert "停止等待" in dialog.progress_label.text() and dialog.task_name.isEnabled()
        with patch.object(dialog_ui, "build_workspace_creation_plan", return_value=plan), patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            dialog.accept()
            pump(lambda: dialog._worker is None)
        final_label = dialog.progress_label.text()
        gate.set()
        pump(lambda: old_worker.completed)
        assert dialog.progress_label.text() == final_label and dialog.result_plan() is None
    assert not Path(plan.root_path).exists()
    with patch.object(dialog_ui._WorkspacePlanWorker, "start", side_effect=RuntimeError("thread start failure")):
        dialog.accept()
        assert dialog._worker is None and save_button(dialog).isEnabled()
        assert "thread start failure" in dialog.progress_label.text()
    with patch.object(dialog_ui._WorkspacePlanWorker, "run", lambda self: setattr(self, "completed", True)):
        dialog.accept()
        pump(lambda: dialog._worker is None)
        assert "未返回" in dialog.progress_label.text() and save_button(dialog).isEnabled()
    with patch.object(dialog_ui, "build_workspace_creation_plan", return_value=plan), patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
        dialog.accept()
        pump(lambda: dialog._worker is None)
        assert dialog.result() == QDialog.DialogCode.Accepted and dialog.result_plan() is plan
    assert not Path(plan.root_path).exists(), "preflight must never create a worktree"
    dialog.deleteLater()
    print("[OK] visible progress, timeout, retry, late result, failed start and missing result recovery", flush=True)

    for close_with_x in (False, True):
        gate = threading.Event()
        entered = threading.Event()
        gates.append(gate)

        def blocked(*args, **kwargs):
            entered.set()
            assert gate.wait(8)
            return plan

        dialog = new_dialog(project)
        dialog.show()
        with patch.object(dialog_ui, "build_workspace_creation_plan", blocked):
            dialog.accept()
            worker = dialog._worker
            pump(entered.is_set)
            started = time.monotonic()
            if close_with_x:
                dialog.close()
            else:
                dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel).click()
            assert time.monotonic() - started < 0.1
            assert dialog._worker is None and not dialog._poll_timer.isActive()
            dialog.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            gate.set()
            pump(lambda: worker.completed)
    print("[OK] cancel and close remain responsive; late workers survive the closed dialog safely", flush=True)


def deadline_checks(project):
    selected = ("server", "webapp")
    def slow_git(cwd, args, timeout=30):
        assert 0 < timeout <= 0.03
        time.sleep(0.05)
        return 0, "true", ""

    with patch.object(service, "_run_git", slow_git):
        try:
            service.build_workspace_creation_plan(project, "deadline", "", selected, timeout=0.02)
            raise AssertionError("deadline was ignored")
        except service.WorkspaceOperationError as error:
            assert "超时" in str(error)
    calls = []
    try:
        service.build_workspace_creation_plan(project, "cancelled", "", selected,
            progress=lambda n, total, message: calls.append(message), cancelled=lambda: len(calls) >= 3)
        raise AssertionError("cancellation was ignored")
    except service.WorkspaceOperationError as error:
        assert "取消" in str(error)
    assert not (root / "workspace").exists()
    print("[OK] one overall deadline bounds Git commands; cancellation stops before the next check", flush=True)


def creation_checks(project):
    tab = aggregate_ui.AggregateProjectTab(project, AppConfig())
    pump(lambda: tab._prepare_worker is None and tab._workspace_worker is None)
    tab._refresh_timer.stop()
    tab.view_tabs.setCurrentIndex(1)
    plans = [service.build_workspace_creation_plan(project, name, "", ("server", "webapp"))
             for name in ("created", "rolled-back", "unexpected")]
    real_run_git = service._run_git
    for index, plan in enumerate(plans):
        gate = threading.Event()
        entered = threading.Event()
        gates.append(gate)

        def controlled_git(cwd, args, timeout=30):
            if args[:2] == ["worktree", "add"] and str(args[4]).endswith("webapp"):
                entered.set()
                assert gate.wait(8)
                if index == 1:
                    return -1, "", "injected worktree timeout"
            return real_run_git(cwd, args, timeout)

        class AcceptedDialog:
            def __init__(self, *args):
                pass

            def exec(self):
                return QDialog.DialogCode.Accepted

            def result_plan(self):
                return plan

        warnings = []
        with patch.object(aggregate_ui, "DevelopmentWorkspaceDialog", AcceptedDialog), patch.object(QMessageBox, "warning", lambda *a, **k: warnings.append(a[2]) or QMessageBox.StandardButton.Ok), patch.object(service, "_run_git", controlled_git):
            if index == 2:
                with patch.object(aggregate_ui, "create_development_workspace", side_effect=RuntimeError("unexpected creation error")):
                    tab._create_workspace()
                    worker = tab._create_worker
                    reclaim_before_results(worker)
                    tab._poll_workspace_creation()
            else:
                tab._create_workspace()
                pump(lambda: entered.is_set() and "webapp" in tab.workspace_status.text())
                assert not tab.new_workspace_button.isEnabled() and "已用" in tab.workspace_status.text()
                gate.set()
                pump(lambda: tab._create_worker is None)
            pump(lambda: tab._workspace_worker is None)
            assert tab.new_workspace_button.isEnabled() and not tab._create_poll_timer.isActive()
            if index == 0:
                assert Path(plan.root_path, "workspace.json").is_file() and not warnings
                card = next(iter(tab._workspace_cards.values()))
                assert [button.text() for button in card.findChildren(QPushButton)] == ["进入工作区"]
                card.resize(1100, card.sizeHint().height())
                assert card.grab().save(str(Path(tempfile.gettempdir()) / "mini-ide-workspace-card.png"))
            elif index == 1:
                assert warnings and "timeout" in warnings[0] and not Path(plan.root_path).exists()
                assert "feature/rolled-back-server" not in git(root / "server", "branch", "--list")
            else:
                assert warnings and "unexpected creation error" in warnings[0]
    assert tab.request_close(quiet=True)
    tab.deleteLater()
    print("[OK] complete GUI creation, per-project progress, timeout rollback and unexpected exception recovery", flush=True)


def remote_creation_checks(project):
    original_git = service._run_git
    git(root / "webapp", "commit", "--allow-empty", "-m", "advance baseline")
    git(root / "webapp", "push")
    def no_origin(cwd, args, timeout=30):
        if args == ["remote", "get-url", "origin"]:
            return 1, "", "origin missing"
        return original_git(cwd, args, timeout)
    with patch.object(service, "_run_git", no_origin):
        try:
            service.build_workspace_creation_plan(project, "no-origin", "", ("server",))
            raise AssertionError("missing origin was accepted")
        except service.WorkspaceOperationError as error:
            assert "origin" in str(error)
    assert not (root / "workspace/no-origin").exists()
    stale = service.build_workspace_creation_plan(project, "remote-stale", "", ("server",))
    first = next(item for item in stale.components if item.mode == "worktree")
    git(root / "server", "push", "origin", f"{first.base_commit}:refs/heads/{first.task_branch}")
    rejected = service.create_development_workspace(stale)
    assert not rejected.ok and "远程任务分支已存在" in rejected.error and not Path(stale.root_path).exists()
    for name, race in (("publish-failed", False), ("publish-race", True)):
        plan = service.build_workspace_creation_plan(project, name, "", ("server", "webapp"))
        second = next(item for item in plan.components if item.id == "webapp")
        competing_commit = git(root / "webapp", "rev-parse", second.base_commit + "^").strip()
        def failing_publish(cwd, args, timeout=30):
            if args[0] == "push" and Path(cwd).name == "webapp":
                if race:
                    git(cwd, "push", "origin", f"{competing_commit}:refs/heads/{second.task_branch}")
                    return original_git(cwd, args, timeout)
                return -1, "", "injected push acknowledgement failure"
            return original_git(cwd, args, timeout)
        with patch.object(service, "_run_git", failing_publish):
            result = service.create_development_workspace(plan)
        assert not result.ok and result.workspace is not None, (name, result)
        assert result.pushed_components == ("server",) and result.pending_components == ("webapp",)
        assert Path(plan.root_path, "workspace.json").is_file() and "本地工作区已保留" in result.error
        if race:
            remote_ref = git(root / "webapp", "ls-remote", "--heads", "origin", f"refs/heads/{second.task_branch}")
            assert remote_ref.split()[0] == competing_commit
        else:
            retried = service.commit_and_push_development_workspace(result.workspace)
            assert retried.ok and all(item.pushed for item in retried.components)
    print("[OK] missing origin, live name collision, partial push recovery and concurrent branch protection", flush=True)


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    slot_errors = []
    original_excepthook = sys.excepthook

    def capture_slot_error(*error):
        slot_errors.append(error[1])
        original_excepthook(*error)

    sys.excepthook = capture_slot_error
    try:
        project = fixture()
        lifecycle_check(project)
        recovery_checks(project)
        deadline_checks(project)
        creation_checks(project)
        remote_creation_checks(project)
        assert not slot_errors, f"unhandled Qt callback errors: {slot_errors}"
    finally:
        for gate in gates:
            gate.set()
        assert stop_background_workers(15_000), "workspace workers did not stop"
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        logging.shutdown()
        SANDBOX.cleanup()
