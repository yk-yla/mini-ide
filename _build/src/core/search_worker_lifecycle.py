"""Track background threads so application shutdown cannot destroy live QThreads."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable

from PySide6.QtCore import QThread


log = logging.getLogger("mini-ide.search_lifecycle")
_active_workers: dict[QThread, Callable[[], None]] = {}


def register_background_worker(worker: QThread, stop: Callable[[], None]) -> None:
    """Register a worker that must be stopped before QApplication teardown."""
    _active_workers[worker] = stop
    worker.finished.connect(lambda worker=worker: _active_workers.pop(worker, None))


def stop_background_workers(timeout_ms: int = 5000) -> bool:
    """Stop and join all registered workers before QApplication is destroyed."""
    workers = [worker for worker in _active_workers if worker.isRunning()]
    for worker in workers:
        try:
            _active_workers[worker]()
        except (RuntimeError, TypeError):
            pass

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
