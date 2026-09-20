"""Search performance and lifecycle regression checks."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QPlainTextEdit

from src.core.file_index import (
    FileIndexer, FileSearchWorker, IndexedFile, _fuzzy_score,
    search_indexed_files,
)
from src.core.git_worker import GitBranchListWorker, GitDirtyCheckWorker
from src.core.resource_guard import _get_node_processes
from src.core.search_worker_lifecycle import (
    register_search_worker, stop_search_workers,
)
from src.ui.content_search import ContentMatch, ContentSearchDialog, SearchWorker
from src.ui.file_tree import FileTree
from src.ui.git_viewer import GitViewer
from src.ui.log_widget import LogWidget
from src.ui.quick_open import PickerDialog, PickerItem, show_file_picker
from src.ui.syntax_highlighter import (
    MAX_HIGHLIGHT_BYTES, PygmentsHighlighter, get_lexer_for,
)


FILE_COUNT = 50_000
QUERY_LIMIT_MS = 200.0
OPEN_LIMIT_MS = 100.0
SCHEDULE_LIMIT_MS = 20.0
DIRECTORY_RENDER_LIMIT_MS = 200.0
HIGHLIGHT_APPLY_LIMIT_MS = 200.0


def _files() -> list[IndexedFile]:
    result = []
    for index in range(FILE_COUNT):
        name = f"UserAccountController{index}.java"
        rel_path = f"module{index % 20}/src/main/java/example/{name}"
        result.append(IndexedFile(rel_path, rel_path, name.lower(), name))
    return result


def _wait_workers(app: QApplication, workers: list, timeout: float = 5.0) -> None:
    deadline = time.perf_counter() + timeout
    while workers and time.perf_counter() < deadline:
        app.processEvents()
        time.sleep(0.001)
    if workers:
        raise AssertionError("file search worker did not finish")


def _wait_until(app: QApplication, predicate, message: str, timeout: float = 5.0) -> None:
    deadline = time.perf_counter() + timeout
    while not predicate() and time.perf_counter() < deadline:
        app.processEvents()
        time.sleep(0.001)
    if not predicate():
        raise AssertionError(message)


def main() -> int:
    app = QApplication.instance() or QApplication([])
    files = _files()

    samples = [
        IndexedFile("src/AppEquipment.java", "1", "appequipment.java", "AppEquipment.java"),
        IndexedFile("src/AppEquipmentController.java", "2", "appequipmentcontroller.java", "AppEquipmentController.java"),
        IndexedFile("src/GetCategoryList.java", "3", "getcategorylist.java", "GetCategoryList.java"),
        IndexedFile("config/application.yml", "4", "application.yml", "application.yml"),
    ]
    for query in ("appequipment", "gcl", "application", "config"):
        legacy = []
        for item in samples:
            score = _fuzzy_score(
                item.name, query, item.rel_path_lower, item.name_original,
            )
            if score > 0:
                legacy.append((score, item))
        legacy.sort(key=lambda pair: (-pair[0], len(pair[1].name), pair[1].rel_path))
        expected = [item.abs_path for _score, item in legacy[:3]]
        actual = [item.abs_path for item in search_indexed_files(samples, query, 3)]
        if actual != expected:
            raise AssertionError(f"file search ordering changed for {query}: {actual}")

    query_times = []
    for query in ("user", "uac", "controller", "xyz-not-found"):
        started = time.perf_counter()
        hits = search_indexed_files(files, query, 150)
        duration_ms = (time.perf_counter() - started) * 1000
        query_times.append(duration_ms)
        expected = 0 if query == "xyz-not-found" else 150
        if len(hits) != expected:
            raise AssertionError(f"unexpected hit count for {query}: {len(hits)}")
        if duration_ms > QUERY_LIMIT_MS:
            raise AssertionError(f"query too slow: {query} {duration_ms:.1f}ms")

    indexer = FileIndexer(os.getcwd())
    indexer._files = files
    indexer._abs_set = {item.abs_path for item in files}
    indexer._ready = True
    indexer._complete = True

    with tempfile.TemporaryDirectory() as tmp:
        watch_indexer = FileIndexer(tmp)
        watch_indexer._ready = True
        started = time.perf_counter()
        watch_indexer._start_watchdog()
        watchdog_dispatch_ms = (time.perf_counter() - started) * 1000
        if watchdog_dispatch_ms > SCHEDULE_LIMIT_MS:
            raise AssertionError(
                f"watchdog startup scheduling blocked UI: {watchdog_dispatch_ms:.1f}ms"
            )
        _wait_until(
            app,
            lambda: watch_indexer._watchdog_worker is None,
            "watchdog startup worker did not finish",
        )
        if watch_indexer._observer is None:
            raise AssertionError("watchdog observer did not start")
        watched_file = Path(tmp) / "watchdog-event.txt"
        watched_file.touch()
        _wait_until(
            app,
            lambda: str(watched_file) in watch_indexer._abs_set,
            "watchdog create event did not update the file index",
        )
        watch_indexer.stop()

    resource_done: list[tuple[int, int]] = []
    resource_heartbeat_delays: list[float] = []
    resource_heartbeat_last = [time.perf_counter()]
    resource_heartbeat = QTimer()
    resource_heartbeat.setInterval(20)

    def on_resource_heartbeat() -> None:
        now = time.perf_counter()
        resource_heartbeat_delays.append(
            (now - resource_heartbeat_last[0]) * 1000
        )
        resource_heartbeat_last[0] = now

    def scan_node_processes() -> None:
        managed, unmanaged = _get_node_processes()
        resource_done.append((len(managed), len(unmanaged)))

    resource_heartbeat.timeout.connect(on_resource_heartbeat)
    resource_heartbeat.start()
    threading.Thread(target=scan_node_processes, daemon=True).start()
    _wait_until(
        app, lambda: bool(resource_done),
        "resource guard process scan did not finish", timeout=10.0,
    )
    resource_heartbeat.stop()
    resource_heartbeat_max_ms = max(resource_heartbeat_delays or [0])
    if resource_heartbeat_max_ms > 250:
        raise AssertionError(
            f"resource guard stalled UI heartbeat: {resource_heartbeat_max_ms:.1f}ms"
        )

    started = time.perf_counter()
    picker = show_file_picker(indexer, lambda _path: None)
    open_ms = (time.perf_counter() - started) * 1000
    if open_ms > OPEN_LIMIT_MS:
        raise AssertionError(f"file picker open too slow: {open_ms:.1f}ms")

    started = time.perf_counter()
    picker.input.setText("uac")
    picker._do_refresh()
    schedule_ms = (time.perf_counter() - started) * 1000
    if schedule_ms > SCHEDULE_LIMIT_MS:
        raise AssertionError(f"file query scheduling blocked UI: {schedule_ms:.1f}ms")
    _wait_workers(app, picker._search_workers)
    if picker.list.count() != 150:
        raise AssertionError(f"async picker result count: {picker.list.count()}")
    picker.input.setText("xyz-not-found")
    picker._do_refresh()
    _wait_workers(app, picker._search_workers)
    if picker.list.count() != 0:
        raise AssertionError("stale file search result replaced the newest query")
    picker.close()

    layout_picker = PickerDialog("layout")
    layout_picker.set_static_items([
        PickerItem(
            "WeimobApiRequest.java",
            "server/application/src/main/java/com/example/support/http/thirdparty/"
            "weimob/WeimobApiRequest.java",
        ),
        PickerItem(
            "WeimobSystemOrder.java",
            "server/application/WeimobSystemOrder.java",
        ),
    ])
    layout_picker.show()
    app.processEvents()
    first_rect = layout_picker.list.visualItemRect(layout_picker.list.item(0))
    second_rect = layout_picker.list.visualItemRect(layout_picker.list.item(1))
    if (
        first_rect.height() != second_rect.height()
        or first_rect.bottom() >= second_rect.top()
    ):
        raise AssertionError(
            f"file picker rows overlap or have unstable heights: "
            f"{first_rect}, {second_rect}"
        )
    layout_picker.close()

    worker = FileSearchWorker(files, "uac", 150, 99, app)
    register_search_worker(worker, worker.requestInterruption)
    worker.start()
    if not stop_search_workers(2000):
        raise AssertionError("application shutdown did not stop file search workers")
    app.processEvents()
    if worker.isRunning():
        raise AssertionError("cancelled file search worker is still running")
    if search_indexed_files(files, "uac", 150, lambda: True):
        raise AssertionError("cancelled file search returned partial results")

    with tempfile.TemporaryDirectory() as tmp:
        search_root = Path(tmp)
        content_files = []
        filler = ("ordinary source line\n" * 100).encode()
        for index in range(1000):
            path = search_root / f"source_{index:04d}.txt"
            path.write_bytes(filler)
            content_files.append(str(path))
        target = search_root / "target.txt"
        target.write_text("prefix target phrase suffix\n", encoding="utf-8")
        content_files.append(str(target))
        search_done: list[tuple[int, int]] = []
        search_worker = SearchWorker(
            tmp, "target phrase", False, False, False, [],
            file_list=content_files, parent=app,
        )
        search_worker.done.connect(lambda scanned, total: search_done.append((scanned, total)))
        register_search_worker(search_worker, search_worker.stop)
        heartbeat_delays: list[float] = []
        heartbeat_last = [time.perf_counter()]
        heartbeat = QTimer()
        heartbeat.setInterval(20)

        def on_heartbeat() -> None:
            now = time.perf_counter()
            heartbeat_delays.append((now - heartbeat_last[0]) * 1000)
            heartbeat_last[0] = now

        heartbeat.timeout.connect(on_heartbeat)
        heartbeat.start()
        started = time.perf_counter()
        search_worker.start()
        _wait_until(
            app, lambda: bool(search_done),
            "buffered content search did not finish", timeout=5.0,
        )
        content_search_ms = (time.perf_counter() - started) * 1000
        heartbeat.stop()
        if search_done != [(1001, 1)]:
            raise AssertionError(f"buffered content search result: {search_done}")
        content_heartbeat_max_ms = max(heartbeat_delays or [0])
        if content_heartbeat_max_ms > 250:
            raise AssertionError(
                f"content search stalled UI heartbeat: {content_heartbeat_max_ms:.1f}ms"
            )

        regex_target = search_root / "regex-target.txt"
        regex_target.write_text("prefix\ntarget phrase\n", encoding="utf-8")
        regex_worker = SearchWorker(
            tmp, "^target phrase$", False, False, True, [],
            file_list=[str(regex_target)], parent=app,
        )
        regex_done: list[tuple[int, int]] = []
        regex_worker.done.connect(lambda scanned, total: regex_done.append((scanned, total)))
        register_search_worker(regex_worker, regex_worker.stop)
        regex_worker.start()
        _wait_until(
            app, lambda: bool(regex_done),
            "line-based regex content search did not finish",
        )
        if regex_done != [(1, 1)]:
            raise AssertionError(f"line-based regex semantics changed: {regex_done}")

        stop_worker = SearchWorker(
            tmp, "not present", False, False, False, [],
            file_list=content_files, parent=app,
        )
        register_search_worker(stop_worker, stop_worker.stop)
        stop_worker.start()
        started = time.perf_counter()
        if not stop_search_workers(2000):
            raise AssertionError("application shutdown did not stop content search workers")
        content_stop_ms = (time.perf_counter() - started) * 1000
        if content_stop_ms > 500:
            raise AssertionError(f"content search stopped too slowly: {content_stop_ms:.1f}ms")
        app.processEvents()

    content = ContentSearchDialog(os.getcwd(), indexer=indexer)
    candidates = content._candidate_files()
    if candidates is None or len(candidates) != FILE_COUNT:
        raise AssertionError("content search did not reuse the ready file index")
    indexer._complete = False
    if content._candidate_files() is not None:
        raise AssertionError("content search reused a truncated file index")
    indexer._complete = True
    content._file_items = {}
    content._file_counts = {}
    content._file_dirs = {}
    content._search_started = time.perf_counter()
    matches = []
    for index in range(1000):
        file_index = index // 5
        path = f"src/example/{file_index}.py"
        matches.append(ContentMatch(
            path, path, index + 1, "example matching line text", 8, 16,
        ))
    started = time.perf_counter()
    content._on_matches(matches)
    while content._render_queue:
        content._flush_matches()
    render_ms = (time.perf_counter() - started) * 1000
    rendered = sum(
        content.tree.topLevelItem(index).childCount()
        for index in range(content.tree.topLevelItemCount())
    )
    if rendered != 1000:
        raise AssertionError(f"content result render count: {rendered}")
    content.close()
    app.processEvents()

    # Git 菜单查询必须走 worker；直接调用 run 验证信号契约，不启动真实 Git。
    from src.core import git_ops
    old_list_branches = git_ops.list_branches
    old_is_dirty = git_ops.is_dirty
    branch_results: list[dict] = []
    dirty_results: list[bool] = []
    try:
        git_ops.list_branches = lambda _cwd: {
            "current": "main", "local": ["main"], "remote": ["origin/main"],
        }
        git_ops.is_dirty = lambda _cwd: True
        branch_worker = GitBranchListWorker(os.getcwd())
        branch_worker.done.connect(branch_results.append)
        branch_worker.run()
        dirty_worker = GitDirtyCheckWorker(os.getcwd())
        dirty_worker.done.connect(dirty_results.append)
        dirty_worker.run()
    finally:
        git_ops.list_branches = old_list_branches
        git_ops.is_dirty = old_is_dirty
    if not branch_results or branch_results[0].get("current") != "main":
        raise AssertionError("git branch worker did not return branch data")
    if dirty_results != [True]:
        raise AssertionError("git dirty worker did not return dirty state")

    # 仓库解析故意阻塞 250ms，GitViewer 构造和关闭仍必须立即返回。
    old_repo_root = git_ops.repo_root
    old_current_branch = git_ops.current_branch
    old_changed_files = git_ops.list_changed_files
    old_numstat = git_ops.diff_numstat
    with tempfile.TemporaryDirectory() as tmp:
        try:
            def slow_repo_root(root: str) -> str:
                time.sleep(0.25)
                return root

            git_ops.repo_root = slow_repo_root
            git_ops.current_branch = lambda _root: "main"
            git_ops.list_changed_files = lambda _root: []
            git_ops.diff_numstat = lambda _root: {}
            started = time.perf_counter()
            viewer = GitViewer(tmp)
            viewer.show()
            app.processEvents()
            git_view_open_ms = (time.perf_counter() - started) * 1000
            started = time.perf_counter()
            viewer.close()
            git_view_close_ms = (time.perf_counter() - started) * 1000
            overview_worker = viewer._overview_worker
            if overview_worker and overview_worker.isRunning():
                overview_worker.wait(2000)
            app.processEvents()
        finally:
            git_ops.repo_root = old_repo_root
            git_ops.current_branch = old_current_branch
            git_ops.list_changed_files = old_changed_files
            git_ops.diff_numstat = old_numstat
    if git_view_open_ms > OPEN_LIMIT_MS:
        raise AssertionError(f"git viewer open blocked UI: {git_view_open_ms:.1f}ms")
    if git_view_close_ms > OPEN_LIMIT_MS:
        raise AssertionError(f"git viewer close blocked UI: {git_view_close_ms:.1f}ms")

    java_text = "".join(
        f"public int method{index:05d}() {{ return {index}; }}\n"
        for index in range(2_000)
    )
    java_bytes = len(java_text.encode("utf-8"))
    if not 70_000 <= java_bytes <= MAX_HIGHLIGHT_BYTES:
        raise AssertionError(f"syntax fixture size: {java_bytes}")
    syntax_editor = QPlainTextEdit()
    syntax_editor.setPlainText(java_text)
    syntax_heartbeat_delays: list[float] = []
    syntax_heartbeat_last = [time.perf_counter()]
    syntax_heartbeat = QTimer()
    syntax_heartbeat.setInterval(20)

    def on_syntax_heartbeat() -> None:
        now = time.perf_counter()
        syntax_heartbeat_delays.append((now - syntax_heartbeat_last[0]) * 1000)
        syntax_heartbeat_last[0] = now

    syntax_heartbeat.timeout.connect(on_syntax_heartbeat)
    syntax_heartbeat.start()
    lexer = get_lexer_for("PerformanceFixture.java")
    if lexer is None:
        raise AssertionError("java lexer unavailable")
    started = time.perf_counter()
    highlighter = PygmentsHighlighter(syntax_editor.document(), lexer)
    syntax_dispatch_ms = (time.perf_counter() - started) * 1000
    if syntax_dispatch_ms > 50:
        raise AssertionError(
            f"syntax highlight scheduling blocked UI: {syntax_dispatch_ms:.1f}ms"
        )
    _wait_until(
        app,
        lambda: highlighter._worker is None and highlighter._applied_generation > 0,
        "large syntax highlight did not finish", timeout=10.0,
    )
    syntax_heartbeat.stop()
    syntax_heartbeat_max_ms = max(syntax_heartbeat_delays or [0])
    if syntax_heartbeat_max_ms > 250:
        raise AssertionError(
            f"syntax tokenize stalled UI heartbeat: {syntax_heartbeat_max_ms:.1f}ms"
        )
    if highlighter._last_apply_ms > HIGHLIGHT_APPLY_LIMIT_MS:
        raise AssertionError(
            f"syntax highlight apply too slow: {highlighter._last_apply_ms:.1f}ms"
        )
    if not highlighter._block_formats:
        raise AssertionError("large syntax highlight produced no formats")

    python_editor = QPlainTextEdit()
    python_text = "".join(
        f"def method_{index:05d}(value): return value + {index}\n"
        for index in range(2_000)
    )
    python_editor.setPlainText(python_text)
    python_delays: list[float] = []
    python_last = [time.perf_counter()]
    python_heartbeat = QTimer()
    python_heartbeat.setInterval(20)

    def on_python_heartbeat() -> None:
        now = time.perf_counter()
        python_delays.append((now - python_last[0]) * 1000)
        python_last[0] = now

    python_heartbeat.timeout.connect(on_python_heartbeat)
    python_heartbeat.start()
    python_lexer = get_lexer_for("PerformanceFixture.py")
    if python_lexer is None:
        raise AssertionError("python lexer unavailable")
    python_highlighter = PygmentsHighlighter(
        python_editor.document(), python_lexer,
    )
    _wait_until(
        app,
        lambda: python_highlighter._worker is None and python_highlighter._applied_generation > 0,
        "python syntax highlight did not finish", timeout=10.0,
    )
    python_heartbeat.stop()
    python_heartbeat_max_ms = max(python_delays or [0])
    if python_heartbeat_max_ms > 250:
        raise AssertionError(
            f"python syntax tokenize stalled UI heartbeat: {python_heartbeat_max_ms:.1f}ms"
        )
    if python_highlighter._last_apply_ms > HIGHLIGHT_APPLY_LIMIT_MS:
        raise AssertionError(
            f"python syntax highlight apply too slow: {python_highlighter._last_apply_ms:.1f}ms"
        )

    # 连续两次请求必须中断旧任务，最终只应用最新 generation。
    highlighter.retokenize()
    highlighter.retokenize()
    _wait_until(
        app,
        lambda: highlighter._worker is None and highlighter._applied_generation == highlighter._generation,
        "latest syntax generation was not applied", timeout=10.0,
    )

    oversized_editor = QPlainTextEdit()
    oversized_editor.setPlainText("x" * 450_000)
    oversized = PygmentsHighlighter(oversized_editor.document(), lexer)
    if oversized._worker is not None or oversized._block_formats:
        raise AssertionError("oversized syntax document should disable highlighting")

    # 模拟关闭文件和应用退出：运行中的分词线程必须可中断并完成回收。
    highlighter.retokenize()
    highlighter.stop()
    if not stop_search_workers(5000):
        raise AssertionError("application shutdown did not stop syntax workers")
    app.processEvents()
    if highlighter._worker and highlighter._worker.isRunning():
        raise AssertionError("cancelled syntax worker is still running")
    oversized.stop()
    python_highlighter.stop()
    syntax_editor.close()
    python_editor.close()
    oversized_editor.close()

    log_widget = LogWidget()
    log_widget.edit.setMaximumBlockCount(20_000)
    for index in range(10_000):
        log_widget.append_line(
            "stdout", f"2026-08-11 INFO request completed item={index}",
        )
    log_batch_times = []
    while log_widget._pending:
        started = time.perf_counter()
        log_widget._flush_pending()
        log_batch_times.append((time.perf_counter() - started) * 1000)
    log_render_max_ms = max(log_batch_times)
    if log_widget.edit.blockCount() != 10_000:
        raise AssertionError(f"log render count: {log_widget.edit.blockCount()}")
    if log_render_max_ms > 100:
        raise AssertionError(f"log render batch too slow: {log_render_max_ms:.1f}ms")
    log_widget.search_input.setText("item=9999")
    started = time.perf_counter()
    log_widget._find_next()
    log_search_ms = (time.perf_counter() - started) * 1000
    if log_widget.edit.textCursor().selectedText() != "item=9999":
        raise AssertionError("log search did not select the expected match")
    if log_search_ms > 100:
        raise AssertionError(f"log search too slow: {log_search_ms:.1f}ms")
    log_widget.close()

    # 真实扫描 5,000 项，并覆盖异步文件定位和展开全部的续跑流程。
    with tempfile.TemporaryDirectory() as tmp:
        root_path = Path(tmp)
        for index in range(5000):
            (root_path / f"file_{index:05d}.txt").touch()
        target = root_path / "nested" / "deeper" / "target.txt"
        target.parent.mkdir(parents=True)
        target.touch()
        started = time.perf_counter()
        file_tree = FileTree(tmp)
        directory_dispatch_ms = (time.perf_counter() - started) * 1000
        root_item = file_tree.tree.topLevelItem(0)
        _wait_until(
            app,
            lambda: not file_tree._has_loading_placeholder(root_item),
            "directory worker did not render the root directory",
        )
        if root_item.childCount() != 5001:
            raise AssertionError(f"directory render count: {root_item.childCount()}")

        file_tree.reveal_path(str(target))
        _wait_until(
            app,
            lambda: (
                file_tree.tree.currentItem() is not None
                and file_tree.tree.currentItem().data(0, 256) == str(target)
            ),
            "async directory reveal did not select the target file",
        )
        file_tree._expand_all_safe()
        _wait_until(
            app,
            lambda: not file_tree._expand_all_pending,
            "async expand-all did not finish",
        )

        entries = [
            ("file", f"file_{index:05d}.txt", str(root_path / f"file_{index:05d}.txt"))
            for index in range(5000)
        ]
        root_item.takeChildren()
        started = time.perf_counter()
        file_tree._render_directory_entries(root_item, root_path, entries)
        directory_render_ms = (time.perf_counter() - started) * 1000
        if root_item.childCount() != 5000:
            raise AssertionError(f"directory batch render count: {root_item.childCount()}")
        if directory_render_ms > DIRECTORY_RENDER_LIMIT_MS:
            raise AssertionError(
                f"directory batch render too slow: {directory_render_ms:.1f}ms"
            )
        file_tree.stop_workers()
        _wait_workers(app, file_tree._directory_workers)
        file_tree.close()
        app.processEvents()

    print(
        "[OK] search performance "
        f"max_query_ms={max(query_times):.1f} "
        f"open_ms={open_ms:.1f} schedule_ms={schedule_ms:.1f} "
        f"watchdog_dispatch_ms={watchdog_dispatch_ms:.1f} "
        f"resource_heartbeat_max_ms={resource_heartbeat_max_ms:.1f} "
        f"content_search_ms={content_search_ms:.1f} "
        f"content_heartbeat_max_ms={content_heartbeat_max_ms:.1f} "
        f"content_stop_ms={content_stop_ms:.1f} "
        f"render_1000_ms={render_ms:.1f} "
        f"git_view_open_ms={git_view_open_ms:.1f} "
        f"git_view_close_ms={git_view_close_ms:.1f} "
        f"syntax_dispatch_ms={syntax_dispatch_ms:.1f} "
        f"syntax_tokenize_ms={highlighter._last_tokenize_ms:.1f} "
        f"syntax_apply_ms={highlighter._last_apply_ms:.1f} "
        f"syntax_heartbeat_max_ms={syntax_heartbeat_max_ms:.1f} "
        f"python_syntax_heartbeat_max_ms={python_heartbeat_max_ms:.1f} "
        f"log_render_max_ms={log_render_max_ms:.1f} "
        f"log_search_ms={log_search_ms:.1f} "
        f"directory_dispatch_ms={directory_dispatch_ms:.1f} "
        f"directory_render_5000_ms={directory_render_ms:.1f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
