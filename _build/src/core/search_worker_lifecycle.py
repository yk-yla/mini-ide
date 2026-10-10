"""Track background threads so application shutdown cannot destroy live QThreads."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable

from PySide6.QtCore import QCoreApplication, QObject, QThread, QTimer


log = logging.getLogger("mini-ide.search_lifecycle")
_active_workers: dict[QThread, Callable[[], None]] = {}


def background_tasks_suspended() -> bool:
    app = QCoreApplication.instance()
    return bool(app and app.property("mini_ide_shutting_down"))


class BackgroundThread(QThread):
    """Every IDE task stays owned and visible to the application until finished."""

    def __init__(self, parent=None):
        # Pages may be destroyed between a result signal and thread.finished.
        # Python references cannot protect a QThread from its C++ parent's deletion.
        super().__init__(QCoreApplication.instance() or parent)

    def start(self, priority=QThread.Priority.InheritPriority) -> None:
        if self not in _active_workers:
            register_background_worker(self, self.requestInterruption)
        try:
            super().start(priority)
        except Exception:
            _active_workers.pop(self, None)
            raise


def register_background_worker(worker: QThread, stop: Callable[[], None]) -> None:
    """Register a worker that must be stopped before QApplication teardown."""
    first_registration = worker not in _active_workers
    _active_workers[worker] = stop
    if first_registration:
        worker.finished.connect(lambda worker=worker: _active_workers.pop(worker, None))
        worker.destroyed.connect(lambda _obj=None, worker=worker: _active_workers.pop(worker, None))


def active_background_workers() -> list[QThread]:
    result = []
    for worker in list(_active_workers):
        try:
            if worker.isRunning():
                result.append(worker)
        except RuntimeError:
            _active_workers.pop(worker, None)
    return result


def request_background_stop() -> None:
    for worker in active_background_workers():
        try:
            _active_workers[worker]()
            # QObject tasks finish their current slot before their event loop exits.
            worker.quit()
        except (RuntimeError, TypeError, KeyError):
            pass


class BackgroundShutdownWaiter(QObject):
    """Keep the GUI event loop alive while tasks finish; a timeout cancels exit."""

    def __init__(self, on_done: Callable[[bool], None], timeout_ms=10000, parent=None):
        super().__init__(parent)
        self._on_done = on_done
        self._deadline = time.monotonic() + max(0, timeout_ms) / 1000
        self._timer = QTimer(self)
        self._timer.setInterval(25)
        self._timer.timeout.connect(self._poll)
        self._empty_polls = 0

    def start(self) -> None:
        request_background_stop()
        self._timer.start()
        self._poll()

    def _poll(self) -> None:
        workers = active_background_workers()
        if not workers:
            # Let queued result/finished slots run before accepting shutdown. Some
            # slots start another worker (e.g. disposing a newly started observer).
            self._empty_polls += 1
            if self._empty_polls < 2:
                return
        else:
            self._empty_polls = 0
        if workers and time.monotonic() < self._deadline:
            request_background_stop()
            return
        self._timer.stop()
        log.info("后台任务退出检查: remaining=%d stopped=%s", len(workers), not workers)
        callback = self._on_done
        self._on_done = lambda _ok: None
        callback(not workers)
        self.deleteLater()


def stop_background_workers(timeout_ms: int = 5000) -> bool:
    """Stop and join all registered workers before QApplication is destroyed."""
    workers = active_background_workers()
    request_background_stop()

    deadline = time.monotonic() + max(0, timeout_ms) / 1000
    all_stopped = True
    for worker in workers:
        remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
        try:
            if worker.isRunning() and not worker.wait(remaining_ms):
                all_stopped = False
        except RuntimeError:
            continue
        if not worker.isRunning():
            _active_workers.pop(worker, None)

    if workers:
        log.info(
            "后台线程退出清理: workers=%d stopped=%s",
            len(workers), all_stopped,
        )
    return all_stopped


# Existing search callers keep their stable API while other UI workers can use
# the same application-level shutdown guard.
register_search_worker = register_background_worker
stop_search_workers = stop_background_workers
