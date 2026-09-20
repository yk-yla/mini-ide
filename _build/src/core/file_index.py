"""项目文件索引

启动时扫描一次（后台线程），支持按文件名 fuzzy 搜索。
watchdog 订阅文件系统变化，自动增量更新。忽略常见的生成目录。
"""
from __future__ import annotations

import heapq
import logging
import os
import threading
import time
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QCoreApplication, QObject, QThread, Signal

from src.core.aggregate_workspace import (
    is_scan_path_excluded, scan_exclusion_roots,
)


IGNORED_DIRS = {
    ".git", ".idea", ".vscode", ".gradle", ".mvn", ".nuxt", ".next", ".output",
    "build", "target", "node_modules", "dist", "out", ".venv", "venv",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".cache", ".angular",
    "bin", "obj", ".parcel-cache",
}

IGNORED_EXTS = {
    ".class", ".jar", ".war", ".pyc", ".pyo", ".o", ".a", ".so", ".dll", ".exe",
    ".lock", ".sum",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".pdf",
    ".mp3", ".mp4", ".zip", ".7z", ".tar", ".gz",
}

MAX_FILES = 50_000           # 超过这个数只索引前 N 个，避免巨型单仓库炸内存
perf_log = logging.getLogger("mini-ide.performance")
_INDEX_SCAN_SLOT = threading.Semaphore(1)


@dataclass
class IndexedFile:
    rel_path: str            # 相对项目根
    abs_path: str
    name: str                # 小写文件名（用于匹配）
    name_original: str       # 原始大小写（用于显示）
    rel_path_lower: str = field(init=False, repr=False)
    stem: str = field(init=False, repr=False)
    initials: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.rel_path_lower = self.rel_path.lower()
        self.stem = self.name.rsplit(".", 1)[0] if "." in self.name else self.name
        self.initials = _camel_initials(self.name_original or self.name)

    def __lt__(self, other: "IndexedFile") -> bool:
        return self.rel_path < other.rel_path


class FileIndexer(QObject):
    """一个项目对应一个 FileIndexer 实例"""

    indexed = Signal(int)        # 扫描完成，参数=文件总数
    file_added = Signal(str)     # abs_path
    file_removed = Signal(str)

    def __init__(self, project_root: str, parent=None):
        super().__init__(parent)
        self.root = Path(project_root).resolve()
        self._excluded_roots = scan_exclusion_roots(self.root)
        self._lock = threading.RLock()
        self._files: list[IndexedFile] = []
        self._abs_set: set[str] = set()
        self._observer = None
        self._worker: _IndexWorker | None = None
        self._watchdog_worker: _WatchdogStartWorker | None = None
        self._stopping = False
        self._ready = False
        self._complete = False
        self._scan_started = 0.0

    # ---- public ----

    def start_async_scan(self) -> None:
        if self._worker and self._worker.isRunning():
            return
        self._ready = False
        self._complete = False
        self._stopping = False
        self._scan_started = time.perf_counter()
        self._worker = _IndexWorker(str(self.root), self._excluded_roots)
        self._worker.done.connect(self._apply_snapshot)
        self._worker.start()

    def files(self) -> list[IndexedFile]:
        with self._lock:
            return list(self._files)

    def count(self) -> int:
        with self._lock:
            return len(self._files)

    def is_ready(self) -> bool:
        with self._lock:
            return self._ready

    def has_complete_snapshot(self) -> bool:
        with self._lock:
            return self._ready and self._complete

    def search(self, query: str, limit: int = 100) -> list[IndexedFile]:
        """按文件名 fuzzy 搜索"""
        with self._lock:
            files = list(self._files)
        return search_indexed_files(files, query, limit)

    def stop(self) -> None:
        self._stopping = True
        if self._worker and self._worker.isRunning():
            self._worker.requestInterruption()
        if self._watchdog_worker and self._watchdog_worker.isRunning():
            self._watchdog_worker.requestInterruption()
        if self._observer:
            try:
                self._observer.stop()
                self._observer.join(timeout=1)
            except Exception:
                pass
            self._observer = None

    # ---- 内部 ----

    def _apply_snapshot(self, files: list[IndexedFile]) -> None:
        worker = self.sender()
        with self._lock:
            self._files = files
            self._abs_set = {f.abs_path for f in files}
            self._ready = True
            self._complete = not bool(getattr(worker, "truncated", False))
        duration_ms = (time.perf_counter() - self._scan_started) * 1000
        perf_log.info(
            "perf op=file-index-build duration_ms=%.1f queue_ms=%.1f files=%d status=done",
            duration_ms, float(getattr(worker, "queue_ms", 0.0)), len(files),
        )
        self.indexed.emit(len(files))
        self._start_watchdog_async()

    def _start_watchdog_async(self) -> None:
        if self._observer or (self._watchdog_worker and self._watchdog_worker.isRunning()):
            return
        if self._stopping:
            return
        owner_ref = weakref.ref(self)

        def on_change(path: str, added: bool) -> None:
            owner = owner_ref()
            if owner is not None:
                owner._on_fs_change(path, added)

        worker = _WatchdogStartWorker(
            str(self.root), on_change, parent=QCoreApplication.instance(),
        )
        self._watchdog_worker = worker
        worker.ready.connect(self._on_watchdog_started)
        worker.finished.connect(self._on_watchdog_worker_finished)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_watchdog_started(self, observer) -> None:
        if observer is None:
            return
        if self._stopping:
            try:
                observer.stop()
                observer.join(timeout=1)
            except Exception:
                pass
            return
        self._observer = observer

    def _on_watchdog_worker_finished(self) -> None:
        worker = self.sender()
        if worker is self._watchdog_worker:
            self._watchdog_worker = None

    def _start_watchdog(self) -> None:
        """兼容旧调用方；实际启动始终异步。"""
        self._start_watchdog_async()

    def _on_fs_change(self, path: str, added: bool) -> None:
        try:
            p = Path(path).resolve()
        except OSError:
            return
        if not p.is_relative_to(self.root):
            return
        if _should_ignore(p, self.root, self._excluded_roots):
            return
        key = str(p)
        with self._lock:
            if added:
                if key in self._abs_set:
                    return
                rel = str(p.relative_to(self.root)).replace("\\", "/")
                entry = IndexedFile(
                    rel_path=rel, abs_path=key,
                    name=p.name.lower(), name_original=p.name,
                )
                # 插入排序位置（保持 _files 有序）
                from bisect import insort
                insort(self._files, entry)
                self._abs_set.add(key)
                self.file_added.emit(key)
            else:
                if key not in self._abs_set:
                    return
                self._files = [f for f in self._files if f.abs_path != key]
                self._abs_set.discard(key)
                self.file_removed.emit(key)


class _WatchdogStartWorker(QThread):
    ready = Signal(object)

    def __init__(self, root: str, on_change: Callable[[str, bool], None], parent=None):
        super().__init__(parent)
        self._root = root
        self._on_change = on_change

    def run(self) -> None:
        started = time.perf_counter()
        observer = None
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            perf_log.info(
                "perf op=file-watchdog-start duration_ms=%.1f files=0 status=unavailable",
                (time.perf_counter() - started) * 1000,
            )
            self.ready.emit(None)
            return

        callback = self._on_change

        class Handler(FileSystemEventHandler):
            def on_created(self, event):
                if not event.is_directory:
                    callback(event.src_path, True)

            def on_deleted(self, event):
                if not event.is_directory:
                    callback(event.src_path, False)

            def on_moved(self, event):
                if not event.is_directory:
                    callback(event.src_path, False)
                    callback(event.dest_path, True)

        if self.isInterruptionRequested():
            return
        try:
            observer = Observer()
            observer.schedule(Handler(), self._root, recursive=True)
            observer.daemon = True
            observer.start()
            if self.isInterruptionRequested():
                observer.stop()
                observer.join(timeout=1)
                return
            perf_log.info(
                "perf op=file-watchdog-start duration_ms=%.1f files=0 status=done",
                (time.perf_counter() - started) * 1000,
            )
            self.ready.emit(observer)
        except Exception:
            perf_log.exception("watchdog 启动失败")
            if observer is not None:
                try:
                    observer.stop()
                    observer.join(timeout=1)
                except Exception:
                    pass
            self.ready.emit(None)

class _IndexWorker(QThread):
    done = Signal(list)

    def __init__(
        self,
        root: str,
        excluded_roots: tuple[str | Path, ...] | None = None,
    ):
        super().__init__()
        self.root = root
        self.excluded_roots = (
            tuple(Path(path) for path in excluded_roots)
            if excluded_roots is not None
            else scan_exclusion_roots(root)
        )
        self.truncated = False

    def run(self) -> None:
        queued_at = time.perf_counter()
        while not _INDEX_SCAN_SLOT.acquire(timeout=0.1):
            if self.isInterruptionRequested():
                return
        self.queue_ms = (time.perf_counter() - queued_at) * 1000
        try:
            self._scan()
        finally:
            _INDEX_SCAN_SLOT.release()

    def _scan(self) -> None:
        files: list[IndexedFile] = []
        root_path = Path(self.root)
        try:
            for dirpath, dirnames, filenames in os.walk(self.root):
                if self.isInterruptionRequested():
                    return
                prune_scan_dirnames(
                    Path(dirpath), dirnames, self.excluded_roots, allow_env=True,
                )
                for fn in filenames:
                    if self.isInterruptionRequested():
                        return
                    ext = Path(fn).suffix.lower()
                    if ext in IGNORED_EXTS:
                        continue
                    abs_p = Path(dirpath) / fn
                    try:
                        rel = str(abs_p.relative_to(root_path)).replace("\\", "/")
                    except ValueError:
                        continue
                    files.append(IndexedFile(
                        rel_path=rel,
                        abs_path=str(abs_p),
                        name=fn.lower(),
                        name_original=fn,
                    ))
                    if len(files) >= MAX_FILES:
                        self.truncated = True
                        files.sort()
                        self.done.emit(files)
                        return
        except OSError:
            pass
        files.sort()
        self.done.emit(files)


class FileSearchWorker(QThread):
    """后台执行文件名模糊匹配；generation 用于丢弃过期结果。"""

    result = Signal(int, list, float)

    def __init__(self, files: list[IndexedFile], query: str, limit: int,
                 generation: int, parent=None):
        super().__init__(parent)
        self.files = files
        self.query = query
        self.limit = limit
        self.generation = generation

    def run(self) -> None:
        started = time.perf_counter()
        hits = search_indexed_files(
            self.files, self.query, self.limit, self.isInterruptionRequested,
        )
        duration_ms = (time.perf_counter() - started) * 1000
        if self.isInterruptionRequested():
            perf_log.debug(
                "perf op=file-search duration_ms=%.1f files=%d query_len=%d status=stopped",
                duration_ms, len(self.files), len(self.query),
            )
            return
        perf_log.info(
            "perf op=file-search duration_ms=%.1f files=%d matches=%d query_len=%d status=done",
            duration_ms, len(self.files), len(hits), len(self.query),
        )
        self.result.emit(self.generation, hits, duration_ms)


def prune_scan_dirnames(
    dirpath: Path,
    dirnames: list[str],
    excluded_roots: tuple[str | Path, ...] | list[str | Path],
    *,
    allow_env: bool = False,
) -> None:
    """原地裁剪 os.walk 目录，统一应用噪音目录和聚合工作区排除。"""
    dirnames[:] = [
        name for name in dirnames
        if (
            (name not in IGNORED_DIRS and not name.startswith("."))
            or (allow_env and name == ".env")
        )
        and not is_scan_path_excluded(dirpath / name, excluded_roots)
    ]


def _should_ignore(
    p: Path,
    root: Path,
    excluded_roots: tuple[str | Path, ...] | list[str | Path] = (),
) -> bool:
    if is_scan_path_excluded(p, excluded_roots):
        return True
    try:
        rel = p.relative_to(root)
    except ValueError:
        return True
    parts = rel.parts
    for part in parts[:-1]:
        if part in IGNORED_DIRS:
            return True
        if part.startswith(".") and part not in (".env",):
            return True
    if p.suffix.lower() in IGNORED_EXTS:
        return True
    return False


def search_indexed_files(
    files: list[IndexedFile],
    query: str,
    limit: int = 100,
    should_stop: Callable[[], bool] | None = None,
) -> list[IndexedFile]:
    """有界 Top-K 文件搜索，可供同步调用或可中断 worker 复用。"""
    query = query.strip().lower()
    if limit <= 0:
        return []
    if not query:
        return files[:limit]

    stopped = False

    def ranked():
        nonlocal stopped
        for index, item in enumerate(files):
            if index % 256 == 0:
                if should_stop is not None and should_stop():
                    stopped = True
                    return
                if index:
                    time.sleep(0)
            score = _indexed_fuzzy_score(item, query)
            if score > 0:
                yield score, item

    matches = heapq.nsmallest(
        limit,
        ranked(),
        key=lambda pair: (-pair[0], len(pair[1].name), pair[1].rel_path),
    )
    if stopped:
        return []
    return [item for _score, item in matches]


def _camel_initials(name: str) -> str:
    """提取驼峰/分隔符首字母缩写：UserController.java → ucj，get_category → gc。

    规则：每个"词"的首字母入选。词边界 = 字符串开头、大写字母（驼峰）、
    分隔符（_ - . 空格）之后。全部转小写返回，供缩写匹配（如 uc→UserController）。
    """
    out: list[str] = []
    prev_boundary = True
    for ch in name:
        if ch in "_-. ":
            prev_boundary = True
            continue
        is_upper = ch.isupper()
        if prev_boundary or is_upper:
            out.append(ch.lower())
        prev_boundary = False
    return "".join(out)


def _indexed_fuzzy_score(item: IndexedFile, query: str) -> int:
    """使用建索引时预计算的字段打分，语义与 _fuzzy_score 保持一致。"""
    if query == item.name or query == item.stem:
        return 1000
    if item.name.startswith(query):
        return 900 - min(len(item.stem) - len(query), 99)
    if query in item.name:
        return 600
    if query == item.initials:
        return 720
    if item.initials.startswith(query):
        return 560
    sub = _subsequence_score(item.name_original or item.name, query)
    if sub > 0:
        return sub
    if query in item.rel_path_lower:
        return 120
    return 0


def _fuzzy_score(name: str, query: str, rel_path: str, name_original: str = "") -> int:
    """文件名模糊打分。分数越高越靠前。

    name 是小写文件名（子串/前缀匹配用），name_original 是原始大小写
    （驼峰首字母缩写用，识别 UserController 这种大写词边界）。
    覆盖几类匹配，按"用户意图明显程度"给分：
    - 完全相等 / 前缀 / 子串：最强信号
    - 驼峰首字母缩写（uc → UserController、gcl → getCategoryList）：很常用
    - 子序列匹配：兜底，且按"连续程度 + 是否贴着词首"加权，
      让 gcl→getCategoryList 这种贴词首的子序列排在松散匹配前面
    """
    if not query:
        return 1
    # 去扩展名后的主干名（AppEquipment.java → appequipment），用于"完整匹配"判定
    stem = name.rsplit(".", 1)[0] if "." in name else name
    if query == name or query == stem:
        return 1000          # 文件名（含/不含扩展名）正好就是 query → 最高，必置顶
    if name.startswith(query):
        # 前缀匹配：越接近"完整等于"分越高。多余字符越少越优先，
        # 这样 appequipment 命中时 AppEquipment > AppEquipmentController
        # > AppEquipmentAssignmentController，不再被路径字母序打乱。
        extra = len(stem) - len(query)
        return 900 - min(extra, 99)
    if query in name:
        return 600

    # 驼峰/分隔首字母缩写：gcl == getCategoryList 的首字母串
    initials = _camel_initials(name_original or name)
    if query == initials:
        return 720
    if initials.startswith(query):
        return 560

    # 子序列匹配 + 连续度/词首加权
    sub = _subsequence_score(name_original or name, query)
    if sub > 0:
        return sub
    # 文件名都不沾边，再看相对路径子串（弱信号）
    if query in rel_path:
        return 120
    return 0


def _subsequence_score(name: str, query: str) -> int:
    """query 的字符按序出现在 name 里则算命中，按连续段和词首奖励打分。

    name 用原始大小写（识别驼峰词首）；query 已小写，比较时按位转小写。
    基础分 100；每个紧跟前一字符的连续命中 +12；每个落在词首
    （开头 / 大写 / 分隔符后）的命中 +8。不命中返回 0。
    """
    low = name.lower()
    idx = 0
    score = 100
    prev_pos = -2
    for ch in query:
        pos = low.find(ch, idx)
        if pos < 0:
            return 0
        if pos == prev_pos + 1:
            score += 12   # 连续
        is_word_start = (pos == 0 or name[pos - 1] in "_-. "
                         or (name[pos].isupper() and not name[pos - 1].isupper()))
        if is_word_start:
            score += 8
        prev_pos = pos
        idx = pos + 1
    return score
