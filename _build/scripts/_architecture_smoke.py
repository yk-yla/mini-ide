"""长期运行与组合压力回归：隔离 APPDATA，不连接现有 IDE，不操作业务服务。"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SANDBOX = tempfile.TemporaryDirectory(prefix="mini-ide-architecture-")
os.environ["APPDATA"] = SANDBOX.name
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QTimer, Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from src.core import cli_server, git_ops, process_runner
from src.core.process_runner import ProcessRunner, RunContext
from src.core.output_queue import OutputQueue
from src.core.search_worker_lifecycle import stop_background_workers
from src.ui.file_preview import FilePreviewPane
from src.ui.file_tree import FileTree
from src.ui.git_viewer import GitViewer, _bounded_diff_preview
from src.ui.log_widget import LogWidget, _block_meta, _format_sql, LOG_PENDING_CHARS
from src.ui.project_service_controller import ProjectServiceController
from src.ui.theme import apply_theme
from src.util import app_log

app = QApplication.instance() or QApplication([])
metrics = {}
watchdog_timer = QTimer()
watchdog_timer.setInterval(100)
watchdog_timer.timeout.connect(app_log.heartbeat)
watchdog_timer.start()


def wait_until(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "isolated regression timed out"
        app.processEvents()
        time.sleep(0.001)
    app.processEvents()


class Heartbeat:
    def __init__(self):
        self.last = time.perf_counter()
        self.delays = []
        self.timer = QTimer()
        self.timer.setInterval(20)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    def tick(self):
        now = time.perf_counter()
        self.delays.append((now - self.last) * 1000)
        self.last = now

    def finish(self, name):
        self.timer.stop()
        value = max(self.delays or [0])
        metrics[name] = round(value, 1)
        assert value < 250, (name, value)


def logs_check():
    widget = LogWidget()
    widget.resize(900, 350)
    widget.show()
    widget.set_max_blocks(500)
    widget.begin_run("raw-completeness")
    widget.append_line("stdout", "Started App in 1.2 seconds")
    widget.append_line("stderr", "ERROR first error preserved in statistics")
    for index in range(30_000):
        widget.append_line("stdout", f"INFO record-{index}")
    widget.append_line("stderr", "ERROR sentinel-last-error")
    sql = "Hibernate: select " + ",".join(f"t.column_{i}" for i in range(8)) + " from t;"
    widget.append_line("stdout", sql)
    assert len(widget._pending) <= 500 and widget._pending_chars <= LOG_PENDING_CHARS
    assert widget._ready_seen and widget._error_count == 2
    assert widget._display_skipped > 0
    widget.end_run()
    raw = widget.log_file_path().read_text(encoding="utf-8")
    assert len(raw.splitlines()) == 30_004 and sql in raw
    while widget._pending:
        widget._flush_pending()
    assert widget.edit.blockCount() == 500
    assert "sentinel-last-error" in widget.error_snippets()
    block = widget.edit.document().lastBlock()
    assert _block_meta(block).kind == "sql"
    widget.append_line("stderr", r"E:\fixture\Foo.java:42: error: cannot find symbol")
    widget._flush_pending()
    assert _block_meta(widget.edit.document().lastBlock()).jumps[0].line == 42
    before = widget.edit.toPlainText()
    revision = widget.edit.document().revision()
    started = time.perf_counter()
    apply_theme(app, "github-light")
    widget.refresh_theme()
    metrics["log_theme_dispatch_ms"] = round((time.perf_counter() - started) * 1000, 1)
    app.processEvents()
    assert before == widget.edit.toPlainText() and revision == widget.edit.document().revision()
    visible = widget.edit.firstVisibleBlock()
    assert visible.layout().formats(), "visible log colors were not drawn"
    widget.edit.verticalScrollBar().setValue(0)
    app.processEvents()
    assert widget.edit.firstVisibleBlock().userData().theme_generation == widget._theme_generation
    quoted = "Hibernate: select 'a,b FROM x', f(1,2) from t where s='x; y'; -- keep\n"
    formatted = _format_sql(quoted)
    assert "'a,b FROM x'" in formatted and "f(1,2)" in formatted and "'x; y'" in formatted
    large_sql = "Hibernate: select " + ",".join(f"t.long_column_{i}" for i in range(1000)) + " from t;"
    started = time.perf_counter()
    _format_sql(large_sql)
    metrics["sql_1000_columns_ms"] = round((time.perf_counter() - started) * 1000, 1)
    assert metrics["sql_1000_columns_ms"] < 100
    old_path = widget.log_file_path()
    widget.begin_run("raw-completeness")
    assert widget.log_file_path() != old_path and old_path.read_text(encoding="utf-8") == raw
    widget.end_run()
    widget.search_input.setText("record-")
    widget._find_next()
    assert "record-" in widget.edit.textCursor().selectedText()
    widget.close()
    return widget


def search_check():
    path = Path(SANDBOX.name) / "million.txt"
    path.write_text(("a" * 99 + "\n") * 20_000, encoding="utf-8")
    pane = FilePreviewPane(str(path))
    pane.search_input.setText("a")
    heartbeat = Heartbeat()
    pane._update_search_status()
    wait_until(lambda: pane._search_cache_key is not None and pane._search_worker is None)
    assert len(pane._search_cache) == 1_980_000
    started = time.perf_counter()
    pane._find_next()
    metrics["cached_search_next_ms"] = round((time.perf_counter() - started) * 1000, 1)
    assert metrics["cached_search_next_ms"] < 100
    pane.search_input.setText("aa")
    pane._update_search_status()
    pane.search_input.setText("missing")
    pane._update_search_status()
    wait_until(lambda: pane._search_cache_key is not None and pane._search_cache_key[1] == "missing")
    assert len(pane._search_cache) == 0
    heartbeat.finish("million_search_heartbeat_ms")
    pane.view.setPlainText("😀 needle\nneedle")
    pane.search_input.setText("needle")
    pane._update_search_status()
    wait_until(lambda: pane._search_cache_key[1] == "needle")
    assert list(pane._search_cache) == [3, 10], list(pane._search_cache)
    pane.stop_workers()
    pane.close()


def tree_and_git_check():
    root = Path(SANDBOX.name) / "tree"
    root.mkdir()
    tree = FileTree(str(root))
    wait_until(lambda: not tree._directory_loading)
    item = tree.tree.topLevelItem(0)
    entries = [("file", f"f-{i}.txt", str(root / f"f-{i}.txt")) for i in range(20_000)]
    heartbeat = Heartbeat()
    tree._render_directory_entries(item, root, entries)
    wait_until(lambda: not tree._render_tokens)
    assert item.childCount() == 20_000
    tree.update_git_status({f"f-{i}.txt": "modified" for i in range(20_000)}, set(), {})
    tree.refresh_theme()
    # 留足时间让分批染色与其它任务一起运行；下一轮刷新淘汰旧批次。
    done = []
    QTimer.singleShot(500, lambda: done.append(True))
    wait_until(lambda: done)
    heartbeat.finish("tree_20000_heartbeat_ms")
    tree.stop_workers()
    tree.close()
    large = ("+" + "x" * 99 + "\n") * 100_000
    preview = _bounded_diff_preview(large)
    assert len(preview) < 201_000 and len(preview.splitlines()) < 2_010
    assert "仅显示前部" in preview and "复制" in preview
    with patch.object(git_ops, "repo_root", lambda p: p), patch.object(git_ops, "current_branch", lambda p: "main"), patch.object(git_ops, "list_changed_files", lambda p, **kwargs: []), patch.object(git_ops, "diff_numstat", lambda p: {}):
        viewer = GitViewer(str(root))
        wait_until(lambda: not viewer._active_workers)
        viewer._refresh_timer.stop()
        started = time.perf_counter()
        viewer.diff_view.setPlainText(preview)
        viewer.refresh_theme()
        metrics["diff_10mb_preview_ms"] = round((time.perf_counter() - started) * 1000, 1)
        assert metrics["diff_10mb_preview_ms"] < 200
        viewer.close()


class FakeRunner:
    def __init__(self):
        self.value = "running"
        self._last_start_error = ""

    def state(self):
        return self.value

    def stop_cleanup_pending(self):
        return False


def services_check():
    from PySide6.QtWidgets import QWidget
    class Tab(QWidget):
        def __init__(self):
            super().__init__()
            self.project_meta = SimpleNamespace(project_type="python", name="fake", default_port=None,
                runtime_units=[], profiles=[SimpleNamespace(primary=True, kind="run")])
            self._is_multi_module = False
            self.runner = FakeRunner()
            self._runtime_stop_runners = {}
            self._module_external_pids = {}
            self._current_profile = SimpleNamespace(kind="run")
            self.module_runners = {}
            self._module_ports = {}
            self.starts = 0
            self.log = LogWidget()

        def _stop(self):
            self.runner.value = "stopping"
            QTimer.singleShot(400, self, lambda: setattr(self.runner, "value", "idle"))
            return True

        def _start_profile(self, profile):
            assert self.runner.value == "idle", "restart overlapped the old process"
            self.starts += 1
            self.runner.value = "starting"
            QTimer.singleShot(40, self, lambda: setattr(self.runner, "value", "running"))
            return True

        def _refresh_status_row(self):
            pass

    tab = Tab()
    controller = ProjectServiceController(tab)
    controller.active_modules = lambda module: ["fake"] if tab.runner.value != "idle" else []
    results = []
    heartbeat = Heartbeat()
    assert controller.request("restart", on_done=results.append)["pending"]
    conflict = controller.request("start")
    assert conflict["code"] == "service_operation_in_progress"
    assert controller.build_start_error()["code"] == "service_operation_in_progress"
    wait_until(lambda: results)
    assert results == [{"ok": True}] and tab.starts == 1
    failed = []
    controller.request("stop", on_done=failed.append, timeout_ms=80)
    wait_until(lambda: failed)
    assert failed[0] == {"ok": False, "error": "stop timeout"}
    wait_until(lambda: tab.runner.value == "idle")
    with patch.object(process_runner, "kill_pid", lambda pid: time.sleep(0.25) or False):
        tab._module_external_pids["mock-external"] = 12345
        controller.stop_external("mock-external", 12345)
        wait_until(lambda: not controller._external_stops)
        assert controller._external_stop_error
    # 即使 runner 已 idle，端口仍监听也不能提前启动下一轮。
    tab.runner.value = "idle"
    tab.project_meta.default_port = 12345
    port_checks = []
    def fake_listening(port):
        port_checks.append(port)
        time.sleep(0.12)
        return len(port_checks) < 3
    port_result = []
    with patch.object(process_runner, "is_port_listening", fake_listening):
        controller.request("restart", on_done=port_result.append)
        wait_until(lambda: port_result)
    assert port_result == [{"ok": True}] and len(port_checks) >= 3 and tab.starts == 2
    heartbeat.finish("slow_stop_heartbeat_ms")
    tab.project_meta.default_port = None
    interrupted = []
    controller.request("restart", on_done=interrupted.append)
    tab.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert interrupted and not interrupted[0]["ok"], interrupted

    # 仅启动本脚本创建的临时输出程序；不向真实 IDE 发 IPC。
    fixture = Path(SANDBOX.name) / "output.py"
    fixture.write_text("for i in range(30000): print('INFO record-' + str(i))\nprint('ERROR final-error')\n", encoding="utf-8")
    runner = ProcessRunner()
    log_widget = LogWidget()
    log_widget.set_max_blocks(500)
    log_widget.begin_run("temporary-process")
    output_count = []
    runner.outputLine.connect(log_widget.append_line)
    runner.outputLine.connect(lambda stream, text: output_count.append(text))
    finished = []
    runner.finished.connect(finished.append)
    heartbeat = Heartbeat()
    assert runner.start([sys.executable, str(fixture)], RunContext(cwd=SANDBOX.name))
    wait_until(lambda: finished)
    log_widget.end_run()
    wait_until(lambda: not log_widget._pending)
    assert finished == [0] and len([s for s in output_count if s.startswith("INFO record-")]) == 30_000
    assert "final-error" in log_widget.error_snippets()
    heartbeat.finish("process_log_30000_heartbeat_ms")
    bad = ProcessRunner()
    failed_start = []
    bad.finished.connect(failed_start.append)
    assert bad.start([str(Path(SANDBOX.name) / "missing.exe")], RunContext(cwd=SANDBOX.name))
    wait_until(lambda: failed_start)
    assert failed_start == [-2] and bad.state() == "idle"


def queue_check():
    queue = OutputQueue(max_bytes=4096, max_records=500)
    expected = [("stderr" if i % 2 else "stdout", (f"{i}" + "x" * 300).encode()) for i in range(10_000)]
    for record in expected:
        queue.append(record)
    assert queue.memory_bytes <= 4096 and queue._spilled > 0
    consumed = [queue.popleft() for _ in range(6000)]
    tail = [("meta", f"new-{i}".encode()) for i in range(500)]
    for record in tail:
        queue.append(record)
    consumed.extend(queue.popleft() for _ in range(len(queue)))
    assert consumed == expected + tail and queue._spool is None


def ipc_check():
    # 独立 socket 名称，不连接正在运行的 IDE。
    server = QLocalServer()
    name = "architecture-test-" + Path(SANDBOX.name).name
    assert server.listen(name)
    client = QLocalSocket()
    client.connectToServer(name)
    wait_until(server.hasPendingConnections)
    accepted = server.nextPendingConnection()
    payload = {"ok": True, "value": "x" * 1_000_000}
    cli_server._send_response(accepted, payload)
    received = bytearray()
    def complete():
        received.extend(bytes(client.readAll()))
        return received.endswith(b"\n")
    wait_until(complete)
    assert json.loads(received) == payload, "nonblocking IPC truncated the response"
    client.close()
    server.close()


def combination_check():
    root = Path(SANDBOX.name) / "combined"
    root.mkdir()
    path = root / "search.txt"
    path.write_text(("a" * 99 + "\n") * 15_000, encoding="utf-8")
    pane = FilePreviewPane(str(path))
    tree = FileTree(str(root))
    wait_until(lambda: not tree._directory_loading)
    logs = [LogWidget() for _ in range(4)]
    for i, widget in enumerate(logs):
        widget.begin_run(f"combination-{i}")
        widget.set_max_blocks(500)
    heartbeat = Heartbeat()
    entries = [("file", f"f-{i}.txt", str(root / f"f-{i}.txt")) for i in range(20_000)]
    tree._render_directory_entries(tree.tree.topLevelItem(0), root, entries)
    pane.search_input.setText("a")
    pane._update_search_status()
    count = [0]
    timer = QTimer()
    timer.setInterval(1)
    def produce():
        for _ in range(100):
            if count[0] >= 30_000:
                timer.stop()
                return
            index = count[0]
            logs[index % 4].append_line("stdout", f"INFO combined-{index}")
            count[0] += 1
        if count[0] == 2_000:
            apply_theme(app, "github-dark")
            for widget in logs:
                widget.refresh_theme()
            tree.refresh_theme()
    timer.timeout.connect(produce)
    timer.start()
    wait_until(lambda: count[0] == 30_000 and not any(w._pending for w in logs)
               and not tree._render_tokens and pane._search_worker is None)
    assert len(pane._search_cache) == 1_485_000
    for i, widget in enumerate(logs):
        widget.end_run()
        assert len(widget.log_file_path().read_text(encoding="utf-8").splitlines()) == 7_500
        assert widget.edit.blockCount() <= 500
        widget.close()
    heartbeat.finish("combined_heartbeat_ms")
    pane.stop_workers()
    tree.stop_workers()
    pane.close()
    tree.close()


if __name__ == "__main__":
    try:
        logs_check()
        search_check()
        tree_and_git_check()
        queue_check()
        services_check()
        ipc_check()
        combination_check()
        print("[OK] architecture pressure " + json.dumps(metrics, ensure_ascii=False), flush=True)
    finally:
        assert stop_background_workers(15_000), "isolated background workers did not stop"
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        logging.shutdown()
        SANDBOX.cleanup()
