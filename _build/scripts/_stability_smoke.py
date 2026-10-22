"""Regression scenarios for shutdown, editing, log bursts and occupied worktrees.

All files, repositories and GUI sessions are isolated; no live IDE IPC is used.
"""
from __future__ import annotations

import ctypes
import faulthandler
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
SANDBOX = tempfile.TemporaryDirectory(prefix="mini-ide-stability-")
os.environ["APPDATA"] = SANDBOX.name
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QCoreApplication, QEvent, QThread, QTimer
from PySide6.QtNetwork import QLocalSocket
from PySide6.QtWidgets import QApplication, QWidget

from src.core import cli_server
from src.core.aggregate_workspace import DevelopmentWorkspace, WorkspaceComponent
from src.core.config import AppConfig
from src.core.development_workspace_service import (
    delete_development_workspace, inspect_workspace_delete,
    _workspace_delete_occupancy_error,
)
from src.core.file_index import FileIndexer
from src.core.process_runner import ProcessRunner, RunContext
from src.core.search_worker_lifecycle import (
    BackgroundThread, BackgroundShutdownWaiter, active_background_workers,
    stop_background_workers,
)
from src.ui.aggregate_project_tab import AggregateProjectTab
from src.ui.file_preview import FilePreviewPane, _MAX_READ_BYTES
from src.ui.main_window import MainWindow


class SlowWorker(BackgroundThread):
    def run(self):
        self.msleep(350)  # Represents a Git call that cannot stop halfway through.


def pump(app, predicate, seconds=5):
    deadline = time.perf_counter() + seconds
    while not predicate() and time.perf_counter() < deadline:
        app.processEvents()
        time.sleep(0.001)
    assert predicate(), "timed out waiting for test condition"
    app.processEvents()


def editing_checks(app):
    path = Path(SANDBOX.name) / "editable.txt"
    path.write_text("seed", encoding="utf-8")
    pane = FilePreviewPane(str(path))
    large = b"a" * (_MAX_READ_BYTES + 31) + b"TAIL-MUST-SURVIVE"
    path.write_bytes(large)
    pane._try_external_reload()
    assert pane.view.isReadOnly() and pane._truncated
    pane.view.setPlainText("cannot overwrite")
    pane.view.document().setModified(True)
    pane._autosave()
    assert path.read_bytes() == large
    pane.view.document().setModified(False)
    path.write_bytes("中文\r\n第二行".encode("utf-16"))
    pane._try_external_reload()
    assert not pane.view.isReadOnly() and pane._encoding.startswith("utf-16")
    pane.view.insertPlainText("修改")
    assert pane.flush_save()
    saved = path.read_bytes().decode("utf-16")
    assert "修改" in saved and "\r\n" in saved
    pane.view.insertPlainText("local change")
    path.write_text("external change", encoding="utf-8")
    assert not pane.flush_save() and pane.is_dirty()
    assert path.read_text(encoding="utf-8") == "external change"
    pane.stop_workers()
    pane.deleteLater()
    app.processEvents()
    print("[OK] external growth, encoding reload and overwrite conflict protection")


def log_checks(app):
    runner = ProcessRunner()
    lines = []
    runner.outputLine.connect(lambda stream, line: lines.append((stream, line)))
    runner._drain("_stdout_buf", b"one\r", "stdout")
    runner._drain("_stdout_buf", b"\ntwo\rthree\nfour", "stdout")
    runner._flush_remaining()
    pump(app, lambda: not runner._output_queue)
    assert [line for _, line in lines] == ["one", "two", "three", "four"]
    lines.clear()
    chunk = (b"x" * 118 + b"\n") * 20000
    started = time.perf_counter()
    runner._drain("_stdout_buf", chunk, "stdout")
    split_ms = (time.perf_counter() - started) * 1000
    assert len(lines) == 0 and split_ms < 200, split_ms
    runner._emit_output_batch()
    assert 0 < len(lines) <= 500
    pump(app, lambda: len(lines) == 20000)
    assert all(line == "x" * 118 for _, line in lines)
    lines.clear()
    finished = []
    runner.finished.connect(lambda code: finished.append((code, len(lines))))
    # Real isolated QProcess, including a final partial line and reuse.
    for text in ("tail-one", "tail-two"):
        assert runner.start([sys.executable, "-c", f"import sys; sys.stdout.write('{text}')"],
                            RunContext(SANDBOX.name))
        assert not runner.start([sys.executable], RunContext(SANDBOX.name))
        pump(app, lambda: runner.state() == "idle")
        assert ("stdout", text) in lines
        assert finished[-1][0] == 0 and finished[-1][1] == len(lines)
        assert lines[-1][0] == "meta"
        lines.clear()
    print(f"[OK] 20000 log lines: split_ms={split_ms:.1f}; bounded batches and finish ordering")


def aggregate_close_checks(app):
    tab = AggregateProjectTab.__new__(AggregateProjectTab)
    QWidget.__init__(tab)
    tab._project_tabs = {}
    tab._refresh_timer = QTimer(tab)
    tab._create_poll_timer = QTimer(tab)
    for name in ("_save_worker", "_create_worker", "_merge_worker", "_sync_push_worker",
                 "_delete_worker", "_prepare_worker", "_delete_preflight_worker"):
        setattr(tab, name, None)
    worker = SlowWorker(tab)
    tab._workspace_worker = worker
    worker.start()
    assert worker.parent() is app and worker in active_background_workers()
    start = time.perf_counter()
    assert tab.request_close(quiet=True)
    assert (time.perf_counter() - start) < 0.1 and worker.isRunning()
    tab._save_worker = worker
    assert not tab.request_close(quiet=True)
    tab.deleteLater()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert worker.isRunning()
    pump(app, lambda: not worker.isRunning())
    worker.deleteLater()
    print("[OK] aggregate closes without waiting; writes block close; live worker survives page deletion")


def index_resume_checks(app):
    root = Path(SANDBOX.name) / "indexed"
    root.mkdir()
    (root / "first.py").write_text("pass")
    index = FileIndexer(str(root))
    index.start_async_scan()
    pump(app, lambda: index.is_ready() and index._observer is not None)
    index.stop()
    (root / "second.py").write_text("pass")
    index.resume()
    pump(app, lambda: index.count() == 2 and index._observer is not None)
    index.stop()
    pump(app, lambda: not active_background_workers())
    index.deleteLater()
    print("[OK] index/watchdog resume refreshes changes made while suspended")


def delete_checks():
    if os.name != "nt":
        return
    root = Path(SANDBOX.name) / "aggregate"
    repo, remote, work = root / "repo", root / "remote.git", root / "workspace" / "task"
    repo.mkdir(parents=True)
    work.parent.mkdir()
    def git(*args):
        result = subprocess.run(["git", *map(str, args)], capture_output=True, text=True,
                                timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()
    git("init", "-b", "main", repo)
    git("-C", repo, "config", "user.name", "stability")
    git("-C", repo, "config", "user.email", "stability@mini-ide.local")
    (repo / "file.txt").write_text("must survive")
    git("-C", repo, "add", ".")
    git("-C", repo, "commit", "-m", "seed")
    git("init", "--bare", remote)
    git("-C", repo, "remote", "add", "origin", remote)
    git("-C", repo, "push", "-u", "origin", "main")
    work.mkdir()
    git("-C", repo, "worktree", "add", "-b", "feature/task", work / "repo", "main")
    git("-C", repo, "push", "origin", "feature/task")
    component = WorkspaceComponent("repo", str(repo), "worktree", str(work / "repo"),
                                   "main", git("-C", repo, "rev-parse", "HEAD"), "feature/task")
    workspace = DevelopmentWorkspace(str(work), "task", "task", "today", str(root),
                                     "active", (component,))
    before = {str(p.relative_to(work)): p.read_bytes() for p in work.rglob("*") if p.is_file()}
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    from ctypes import wintypes
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    for locked in (work, work / "repo" / "file.txt"):
        handle = kernel.CreateFileW(str(locked), 0x80000000, 3, None, 3, 0x02000000, None)
        assert handle != ctypes.c_void_p(-1).value
        try:
            assert _workspace_delete_occupancy_error(work)
            plan = inspect_workspace_delete(workspace)
            assert plan.can_delete, plan.blockers
            ok, remaining, error = delete_development_workspace(plan)
            assert not ok and not remaining and "保留目录内容" in error, error
            after = {str(p.relative_to(work)): p.read_bytes() for p in work.rglob("*") if p.is_file()}
            assert before == after
            assert not git("-C", repo, "branch", "--list", "feature/task")
            assert not git("-C", repo, "ls-remote", "--heads", "origin", "feature/task")
        finally:
            kernel.CloseHandle(handle)
    plan = inspect_workspace_delete(workspace)
    assert plan.can_delete, plan.blockers
    assert delete_development_workspace(plan) == (True, (), "")
    assert not work.exists()
    print("[OK] root/file locks preserve all files; branches clean independently; detached retry succeeds")


def quit_child(mode):
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    config = AppConfig(startup_restore_mode="none", restore_tabs_on_startup=False)
    window = MainWindow(config)
    root = Path(SANDBOX.name) / "project"
    root.mkdir()
    path = root / "file.txt"
    path.write_text("seed")
    tab = window.open_project(str(root), quiet=True)
    assert tab is not None
    tab._show_preview(str(path), 0, 0)
    pane = next(iter(tab._file_panes.values()))
    pane.view.insertPlainText("edited")
    worker = SlowWorker(tab)
    results, at_quit = [], []
    app.aboutToQuit.connect(lambda: at_quit.append(not active_background_workers()))
    def start():
        worker.start()
        if mode == "timeout":
            def done(result):
                results.append(result)
                assert not result["ok"] and window.isEnabled()
                assert not window._shutdown_in_progress
                assert not app.property("mini_ide_shutting_down")
                assert tab._poll_timer.isActive() or tab._poll_start_timer.isActive()
                QTimer.singleShot(450, window._request_quit)
            window.prepare_application_quit(done, timeout_ms=20)
        elif mode == "save-fail":
            path.write_text("external change")
            window.prepare_application_quit(results.append)
            assert results and not results[0]["ok"] and window.isEnabled()
            assert pane.is_dirty() and path.read_text() == "external change"
            # Explicitly discard only this test edit, then verify another quit works.
            pane.view.document().setModified(False)
            pane._try_external_reload()
            window._request_quit()
        elif mode == "cli":
            class Socket:
                def state(self):
                    return QLocalSocket.LocalSocketState.ConnectedState
            cli_server._send_response = lambda sock, result: results.append(result)
            assert cli_server.handle_async_cli_request({"cmd": "quit"}, Socket(), window)
        elif mode == "qt":
            app.quit()
        else:
            window._request_quit()
    QTimer.singleShot(150, start)
    QTimer.singleShot(6000, lambda: app.exit(99))
    result = app.exec()
    # Clean up safely even if a quit-regression assertion fails.
    stop_background_workers(5000)
    assert result == 0 and window._quit_prepared and at_quit == [True], (result, at_quit)
    if mode != "save-fail":
        assert "edited" in path.read_text(), "pending autosave was lost"
    if mode == "cli":
        assert len(results) == 1 and results[0]["ok"], results
    print(f"[OK] quit {mode}: saves files and waits for workers; cancellation remains usable")


def main():
    faulthandler.dump_traceback_later(12, repeat=True)
    if len(sys.argv) > 1:
        quit_child(sys.argv[1])
        return
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    editing_checks(app)
    log_checks(app)
    aggregate_close_checks(app)
    index_resume_checks(app)
    delete_checks()
    faulthandler.cancel_dump_traceback_later()
    for mode in ("menu", "cli", "qt", "timeout", "save-fail"):
        result = subprocess.run([sys.executable, str(Path(__file__)), mode],
                                capture_output=True, encoding="utf-8", errors="replace",
                                timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert result.returncode == 0, result.stdout + result.stderr
        assert "QThread: Destroyed" not in result.stderr, result.stderr
        print(next(line for line in result.stdout.splitlines() if line.startswith("[OK] quit")))
    pump(app, lambda: not active_background_workers())


if __name__ == "__main__":
    try:
        main()
    finally:
        logging.shutdown()
        SANDBOX.cleanup()
