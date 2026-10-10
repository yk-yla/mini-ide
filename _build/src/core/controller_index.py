"""Spring Controller 接口索引

扫描项目里所有 .java，解析类级 @RequestMapping 基路径 + 方法级
@GetMapping/@PostMapping/@RequestMapping(...) 拼出完整接口路径，
记下文件 + 行号。供 Ctrl+/ 浮窗按接口地址（如 /tenant/timer/common/
getCategoryList）模糊定位到 Controller 方法。

只用标准库 re + os.walk，后台线程扫描，不引新依赖。
"""
from __future__ import annotations

from src.core.search_worker_lifecycle import BackgroundThread, background_tasks_suspended

import os
import re
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from src.core.aggregate_workspace import scan_exclusion_roots
from src.core.file_index import prune_scan_dirnames


# 方法级映射注解 → 默认 HTTP 方法标签
_METHOD_ANNOS = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
    "RequestMapping": "",     # method 由 method= 属性定，默认空
}

# 类上的映射注解（拼基路径用），controller 标记注解（判定这是不是 Controller）
_CLASS_MAPPING_RE = re.compile(
    r"@RequestMapping\s*(?:\(\s*(?P<body>[^)]*)\))?", re.DOTALL)
_CONTROLLER_RE = re.compile(r"@(?:Rest)?Controller\b")
# 方法级映射注解：捕获注解名 + 括号内容（可能跨行，含 value=/path=/method=）
_METHOD_ANNO_RE = re.compile(
    r"@(GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping|RequestMapping)"
    r"\s*(?:\(\s*(?P<body>[^)]*)\))?",
    re.DOTALL)
# 从注解括号内容里抠出第一个字符串字面量（value="/x" / path="/x" / 直接 "/x"）
_PATH_LITERAL_RE = re.compile(r'(?:value|path)\s*=\s*"([^"]*)"|"([^"]*)"')
# method=RequestMethod.POST → POST
_METHOD_ATTR_RE = re.compile(r"method\s*=\s*\{?\s*RequestMethod\.(\w+)")


@dataclass
class Endpoint:
    http_method: str     # GET/POST/...，未知为 ""
    path: str            # 完整路径，如 /tenant/timer/common/getCategoryList
    class_name: str      # 所在 Controller 类名
    method_name: str     # Java 方法名
    abs_path: str        # 文件绝对路径
    line_no: int         # 方法映射注解所在行（1-based）

    @property
    def display(self) -> str:
        m = f"{self.http_method:6}" if self.http_method else "      "
        return f"{m} {self.path}"

    @property
    def subtitle(self) -> str:
        return f"{self.class_name}.{self.method_name}()"


def _join_path(base: str, sub: str) -> str:
    """拼接类级 base 与方法级 sub，规整斜杠。两者都可能带/不带前导斜杠。"""
    base = (base or "").strip()
    sub = (sub or "").strip()
    parts = [p for p in (base.strip("/"), sub.strip("/")) if p]
    return "/" + "/".join(parts) if parts else "/"


def _first_path_literal(body: str) -> str:
    """从注解括号内容里取第一个路径字面量；取不到返回空串。"""
    if not body:
        return ""
    m = _PATH_LITERAL_RE.search(body)
    if not m:
        return ""
    return m.group(1) if m.group(1) is not None else (m.group(2) or "")


def parse_controller(text: str, abs_path: str) -> list[Endpoint]:
    """解析单个 .java 文本，返回其中所有接口。非 Controller 返回空。"""
    if not _CONTROLLER_RE.search(text):
        return []

    cls_m = re.search(r"\b(?:public\s+|final\s+|abstract\s+)*class\s+(\w+)", text)
    class_name = cls_m.group(1) if cls_m else "?"

    # 类级基路径：取类声明之前那段里的 @RequestMapping
    class_decl_pos = cls_m.start() if cls_m else len(text)
    base_path = ""
    cm = _CLASS_MAPPING_RE.search(text, 0, class_decl_pos)
    if cm:
        base_path = _first_path_literal(cm.group("body") or "")

    # 行起始偏移表，用于把字符位置换算成行号
    line_starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            line_starts.append(i + 1)

    def line_of(pos: int) -> int:
        # 二分找 pos 落在第几行
        lo, hi = 0, len(line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_starts[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    endpoints: list[Endpoint] = []
    for am in _METHOD_ANNO_RE.finditer(text):
        if am.start() < class_decl_pos:
            continue   # 类声明前的（含类级 @RequestMapping 本身）跳过
        anno = am.group(1)
        body = am.group("body") or ""
        sub_path = _first_path_literal(body)
        http = _METHOD_ANNOS.get(anno, "")
        if anno == "RequestMapping":
            mt = _METHOD_ATTR_RE.search(body)
            http = mt.group(1) if mt else ""
        # 注解之后紧跟的方法名：跳过可能的其他注解/修饰符，找第一个 "ret name("
        tail = text[am.end():am.end() + 400]
        nm = re.search(r"\b(\w+)\s*\(", tail)
        method_name = nm.group(1) if nm else "?"

        endpoints.append(Endpoint(
            http_method=http,
            path=_join_path(base_path, sub_path),
            class_name=class_name,
            method_name=method_name,
            abs_path=abs_path,
            line_no=line_of(am.start()),
        ))
    return endpoints


class ControllerIndexer(QObject):
    """一个项目对应一个实例。首次需要时 build_async 后台扫描，结果常驻。"""

    ready = Signal(int)     # 扫描完成，参数=接口总数

    def __init__(self, project_root: str, parent=None):
        super().__init__(parent)
        self.root = str(Path(project_root).resolve())
        self._endpoints: list[Endpoint] = []
        self._built = False
        self._worker: _ControllerWorker | None = None
        self._restart_requested = False
        self._stopping = False

    def is_built(self) -> bool:
        return self._built

    def is_building(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def build_async(self) -> None:
        if background_tasks_suspended():
            return
        self._stopping = False
        if self.is_building():
            if self._worker.isInterruptionRequested():
                self._restart_requested = True
            return
        if self._built:
            return
        self._worker = _ControllerWorker(self.root)
        self._worker.done.connect(self._on_done)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    def _on_worker_finished(self) -> None:
        if self.sender() is self._worker:
            self._worker = None
            if self._restart_requested and not self._stopping:
                self._restart_requested = False
                self.build_async()

    def _on_done(self, endpoints: list) -> None:
        if self._stopping:
            return
        self._endpoints = endpoints
        self._built = True
        self.ready.emit(len(endpoints))

    def count(self) -> int:
        return len(self._endpoints)

    def stop(self) -> None:
        self._stopping = True
        self._restart_requested = False
        if self._worker and self._worker.isRunning():
            self._worker.requestInterruption()

    def search(self, query: str, limit: int = 100) -> list[Endpoint]:
        """按路径模糊匹配。query 里的 / 和大小写都忽略，按子序列+子串打分。"""
        q = query.strip().lower()
        if not q:
            return self._endpoints[:limit]
        scored: list[tuple[int, Endpoint]] = []
        for ep in self._endpoints:
            s = _ep_score(ep, q)
            if s > 0:
                scored.append((s, ep))
        scored.sort(key=lambda p: (-p[0], p[1].path))
        return [e for _, e in scored[:limit]]


def _ep_score(ep: Endpoint, q: str) -> int:
    """接口路径匹配打分：完整子串最高，去斜杠子串次之，子序列兜底。"""
    path = ep.path.lower()
    if q == path:
        return 1000
    if q in path:
        return 800
    # 去掉斜杠再比（用户可能不打 /，或漏打中间段）
    flat = path.replace("/", "")
    qflat = q.replace("/", "")
    if qflat and qflat in flat:
        return 600
    # 方法名命中
    if q in ep.method_name.lower():
        return 400
    # 子序列兜底
    idx = 0
    for ch in qflat:
        pos = flat.find(ch, idx)
        if pos < 0:
            return 0
        idx = pos + 1
    return 100 if qflat else 0


class _ControllerWorker(BackgroundThread):
    done = Signal(list)

    def __init__(self, root: str):
        super().__init__()
        self.root = root
        self.excluded_roots = scan_exclusion_roots(root)

    def run(self) -> None:
        endpoints: list[Endpoint] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            if self.isInterruptionRequested():
                return
            prune_scan_dirnames(Path(dirpath), dirnames, self.excluded_roots)
            for fn in filenames:
                if self.isInterruptionRequested():
                    return
                if not fn.endswith(".java"):
                    continue
                abs_p = Path(dirpath) / fn
                try:
                    text = abs_p.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                if "Mapping" not in text:
                    continue   # 快速短路：没有任何映射注解
                try:
                    endpoints.extend(parse_controller(text, str(abs_p)))
                except Exception:
                    continue   # 单文件解析异常不影响整体
        endpoints.sort(key=lambda e: e.path)
        self.done.emit(endpoints)
