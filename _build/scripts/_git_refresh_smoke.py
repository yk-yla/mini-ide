"""Git 刷新回归：真实临时仓库和隔离 Qt，不连接现有 IDE 或业务服务。"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

SANDBOX = tempfile.TemporaryDirectory(prefix="mini-ide-git-refresh-")
os.environ["APPDATA"] = SANDBOX.name
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.environ.get("MINI_IDE_TEST_RUNTIME", str(Path(__file__).resolve().parents[1])))

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication
from src.core import git_ops
from src.core.config import AppConfig, ProjectEntry
from src.core.project_detector import ProjectMeta
from src.core.search_worker_lifecycle import stop_background_workers
from src.ui.project_tab import ProjectTab

app = QApplication.instance() or QApplication([])
root = Path(SANDBOX.name) / "repo"
root.mkdir()
gates = []


def git(*args):
    result = subprocess.run(
        [git_ops.resolve_git_executable(), "-c", "user.name=Refresh Test",
         "-c", "user.email=refresh@example.invalid", *args],
        cwd=root, capture_output=True, text=True, **git_ops._POPEN_KW,
    )
    assert result.returncode == 0, result.stderr


def commit():
    git("add", ".")
    git("commit", "-m", "temporary fixture")


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "Git refresh regression timed out"
        app.processEvents()
        time.sleep(0.002)
    app.processEvents()


def settle(tab):
    wait_until(lambda: tab._git_status_worker is None and tab._git_details_worker is None)


def block_query(original):
    gate = threading.Event()
    entered = threading.Event()
    gates.append(gate)

    def blocked(path):
        entered.set()
        assert gate.wait(8), "blocked test query was not released"
        return original(path)

    return blocked, entered, gate


def run():
    git("init", "-b", "main")
    for index in range(12):
        (root / f"file-{index}.txt").write_text("before\n", encoding="utf-8")
    commit()
    for index in range(12):
        (root / f"file-{index}.txt").write_text("after\n", encoding="utf-8")
    tab = ProjectTab(
        ProjectMeta(str(root), "fixture", "generic", "临时仓库", ""),
        ProjectEntry(str(root)), AppConfig(),
    )
    for timer in (tab._poll_start_timer, tab._fetch_start_timer):
        timer.stop()

    # 模拟文件树慢查询未结束时，用户在外部提交了全部 12 个文件。
    slow_ignored, ignored_entered, ignored_gate = block_query(git_ops.list_ignored_files)
    slow_stats, stats_entered, stats_gate = block_query(git_ops.diff_numstat)
    with patch.object(git_ops, "list_ignored_files", slow_ignored), patch.object(git_ops, "diff_numstat", slow_stats):
        tab.show()
        wait_until(lambda: "12 处改动" in tab.btn_changes.text() and ignored_entered.is_set())
        old_snapshot = tab._git_snapshot
        tab._git_status_pending = False
        commit()
        tab.open_git_viewer()
        viewer = tab._git_viewer
        viewer._refresh_timer.stop()
        wait_until(lambda: "无改动" in tab.btn_changes.text() and stats_entered.is_set(), 2)
        assert "工作区干净" in viewer.lbl_summary.text()
        assert tab.file_tree._git_status == {} and viewer.changes_list.count() == 0
        assert not ignored_gate.is_set() and not stats_gate.is_set()
        tab._on_git_snapshot_ready(old_snapshot)
        assert "无改动" in tab.btn_changes.text()
        ignored_gate.set()
        stats_gate.set()
        settle(tab)
        wait_until(lambda: viewer._overview_worker is None)
        assert "无改动" in tab.btn_changes.text() and tab.file_tree._git_status == {}
        viewer.close()
        wait_until(lambda: tab._git_viewer is None)
    print("[OK] external 12-file commit, popup synchronization, slow queries and stale results", flush=True)

    # 弹窗关闭后仍由原有三秒轮询发现外部编辑；切回应用主动刷新，不等下一轮。
    (root / "file-0.txt").write_text("polling\n", encoding="utf-8")
    tab._poll_timer.start()
    wait_until(lambda: "1 处改动" in tab.btn_changes.text(), 4.5)
    tab._poll_timer.stop()
    settle(tab)
    commit()
    app.applicationStateChanged.emit(Qt.ApplicationState.ApplicationActive)
    wait_until(lambda: "无改动" in tab.btn_changes.text(), 2)
    settle(tab)
    tab.hide()
    (root / "file-0.txt").write_text("tab switch\n", encoding="utf-8")
    tab.show()
    wait_until(lambda: "1 处改动" in tab.btn_changes.text(), 2)
    settle(tab)
    git("checkout", "-b", "other")
    tab._request_git_refresh()
    wait_until(lambda: "other" in tab.btn_branch.text(), 2)
    settle(tab)
    tab.open_git_viewer()
    viewer = tab._git_viewer
    viewer._refresh_timer.stop()
    wait_until(lambda: viewer.changes_list.count() == 1)
    wait_until(lambda: viewer._overview_worker is None)
    commit()
    tab._request_git_refresh()
    wait_until(lambda: "无改动" in tab.btn_changes.text(), 2)
    assert viewer.changes_list.count() == 0 and "工作区干净" in viewer.lbl_summary.text()
    viewer.close()
    wait_until(lambda: tab._git_viewer is None)
    settle(tab)
    print("[OK] polling, application focus, project visibility, branch changes and reverse synchronization", flush=True)

    # 主动请求撞上慢任务时只补一轮，避免请求丢失或堆积。
    (root / "file-0.txt").write_text("pending\n", encoding="utf-8")
    slow_ignored, entered, gate = block_query(git_ops.list_ignored_files)
    with patch.object(git_ops, "list_ignored_files", slow_ignored):
        tab._request_git_refresh()
        wait_until(lambda: "1 处改动" in tab.btn_changes.text() and entered.is_set())
        commit()
        for _ in range(5):
            app.applicationStateChanged.emit(Qt.ApplicationState.ApplicationActive)
        assert tab._git_status_pending
        wait_until(lambda: "无改动" in tab.btn_changes.text(), 2)
        assert not gate.is_set(), "slow details must not block the next local status query"
        gate.set()
        settle(tab)
    print("[OK] foreground refresh while a previous query is still running", flush=True)

    # 查询失败保留真实的上次状态，不能把失败伪装成零改动；下一轮能恢复。
    (root / "file-0.txt").write_text("failure\n", encoding="utf-8")
    tab._request_git_refresh()
    wait_until(lambda: "1 处改动" in tab.btn_changes.text())
    settle(tab)
    tab.open_git_viewer()
    viewer = tab._git_viewer
    viewer._refresh_timer.stop()
    wait_until(lambda: viewer._overview_worker is None)
    real_run = git_ops._run

    def failed_status(args, cwd, **kwargs):
        if args[0] == "status":
            return -1, "", "temporary status failure"
        return real_run(args, cwd, **kwargs)

    with patch.object(git_ops, "_run", failed_status):
        tab._request_git_refresh()
        viewer._reload(force=True)
        wait_until(lambda: "刷新失败" in tab.btn_changes.toolTip() and "刷新失败" in viewer.lbl_summary.text())
        assert "1 处改动" in tab.btn_changes.text() and viewer.changes_list.count() == 1
        assert git_ops.list_changed_files(str(root)) == []  # 旧调用保留原契约。
        settle(tab)
        wait_until(lambda: viewer._overview_worker is None)
    commit()
    viewer._reload(force=True)
    wait_until(lambda: "无改动" in tab.btn_changes.text() and "工作区干净" in viewer.lbl_summary.text())
    viewer.close()
    wait_until(lambda: tab._git_viewer is None)
    print("[OK] query failure preserves the last result and refresh recovers", flush=True)

    slow_ignored, entered, gate = block_query(git_ops.list_ignored_files)
    with patch.object(git_ops, "list_ignored_files", slow_ignored):
        tab._request_git_refresh()
        wait_until(entered.is_set)
        tab._request_git_refresh()
        assert tab.request_close(confirm_running=False, stop_running=False, quiet=True)
        gate.set()
        settle(tab)
        assert not tab._git_refresh_enabled and not tab._git_status_pending
    tab.hide()
    tab.deleteLater()
    print("[OK] closing with an unfinished refresh does not start another query", flush=True)


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    try:
        run()
    finally:
        for gate in gates:
            gate.set()
        assert stop_background_workers(15_000), "Git refresh workers did not stop"
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        logging.shutdown()
        SANDBOX.cleanup()
