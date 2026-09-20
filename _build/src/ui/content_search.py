"""全项目内容搜索  (Ctrl+Shift+F)

启动时选择根目录，后台线程 grep 整个项目（忽略 build/node_modules/.git...），
结果流式展示。点击跳转到具体文件和行。
"""
from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import (
    QAbstractTextDocumentLayout, QColor, QFont, QPalette, QTextCharFormat,
    QTextCursor, QTextDocument,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QPushButton, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from src.core.aggregate_workspace import is_scan_path_excluded, scan_exclusion_roots
from src.core.file_index import FileIndexer, IGNORED_EXTS, prune_scan_dirnames
from src.core.search_worker_lifecycle import register_search_worker
from src.ui.theme import (
    FG_BRIGHT, FG_DIM, FG_PRIMARY, FG_SECONDARY, HIGHLIGHT_MATCH_BG,
    HIGHLIGHT_MATCH_FG, apply_search_style,
)


MAX_MATCHES = 1000
MAX_FILE_SIZE = 2 * 1024 * 1024    # 单文件超 2MB 不扫
MAX_LINE_LEN = 500                  # 匹配行显示截断
RENDER_BATCH_SIZE = 80
RENDER_INTERVAL_MS = 16
SEARCH_IO_WORKERS = 4
perf_log = logging.getLogger("mini-ide.performance")

# 默认隐藏的"噪音文件"：日志、压缩/生成产物、lock 等。
# 它们能被全文搜命中但绝大多数时候不是用户想找的源码，默认排除、
# 顶部勾选可放开（见 ContentSearchDialog 的"含日志等"复选框）。
NOISE_EXTS = {
    ".log",                          # 日志
    ".min.js", ".min.css",           # 压缩产物（注意是复合后缀，单独判定）
    ".map",                          # source map
    ".lock",                         # 锁文件
    ".snap",                         # 测试快照
    ".csv", ".tsv",                  # 大数据表（常是导出/样本数据）
}
# 文件名整体命中即视为噪音（无固定扩展名的 lock）
NOISE_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "composer.lock", "gemfile.lock", "cargo.lock",
}


def _is_noise_file(filename: str) -> bool:
    """文件名是否属于默认隐藏的噪音文件（日志 / 压缩产物 / lock 等）"""
    low = filename.lower()
    if low in NOISE_NAMES:
        return True
    # .min.js / .min.css 这类复合后缀单独判
    if low.endswith(".min.js") or low.endswith(".min.css"):
        return True
    ext = Path(filename).suffix.lower()
    return ext in NOISE_EXTS


def _compile_pattern(query: str, case: bool, whole_word: bool, regex: bool):
    """编译搜索 pattern。SearchWorker 跟 dialog 的 highlight delegate 共用同一份。"""
    q = query if regex else re.escape(query)
    if whole_word:
        q = r"\b" + q + r"\b"
    flags = 0 if case else re.IGNORECASE
    return re.compile(q, flags)


@dataclass
class ContentMatch:
    abs_path: str
    rel_path: str
    line_no: int
    line_text: str
    col_start: int
    col_end: int


class SearchWorker(QThread):
    match_found = Signal(list)   # list[ContentMatch]
    progress = Signal(int, int)  # files_scanned, matches_found
    done = Signal(int, int)      # total_files_scanned, total_matches
    stopped = Signal()
    files_collected = Signal(list)  # list[str]：本次 walk 收集到的候选文件清单（供 dialog 缓存）

    def __init__(self, root: str, query: str, case_sensitive: bool,
                 whole_word: bool, use_regex: bool, include_exts: list[str],
                 file_list: list[str] | None = None, include_noise: bool = False,
                 parent=None):
        super().__init__(parent)
        self.root = root
        self.excluded_roots = scan_exclusion_roots(root)
        self.query = query
        self.case = case_sensitive
        self.whole_word = whole_word
        self.regex = use_regex
        self.include_exts = {e.lower() for e in include_exts if e}
        # 是否把日志/压缩产物/lock 等噪音文件纳入搜索（默认 False，顶部勾选放开）
        self.include_noise = include_noise
        # 复用上一次扫描缓存的候选文件清单：非 None 时跳过 os.walk + stat，直接 grep。
        # 大仓库下省掉的目录遍历和 stat 调用是"输入即搜"流畅度的关键。
        # 注意：清单里含噪音文件，是否搜它们由 include_noise 在 grep 阶段决定，
        # 这样切换"含日志"开关无需重新 walk。
        self.file_list = file_list
        self._stop = False
        self._literal_needle: bytes | None = None
        if not use_regex:
            # 大小写敏感文本可直接按 UTF-8 预筛；中文、数字等没有大小写
            # 的查询在 IGNORECASE 下也可安全预筛。其余情况保留完整解码语义。
            has_cased_char = any(ch.lower() != ch.upper() for ch in query)
            if case_sensitive or not has_cased_char:
                self._literal_needle = query.encode("utf-8")

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        try:
            pattern = _compile_pattern(self.query, self.case, self.whole_word, self.regex)
        except re.error:
            self.done.emit(0, 0)
            return

        if self.file_list is not None:
            self._run_cached(pattern)
        else:
            self._run_walk(pattern)

    def _grep_file(self, abs_p: Path, pattern, batch: list, scanned: int,
                   total: int) -> tuple[int, bool]:
        """grep 单个文件，命中追加到 batch。返回 (新增命中数, 是否达上限)。"""
        rel = str(abs_p.relative_to(self.root)).replace("\\", "/")
        added = 0
        try:
            with abs_p.open("r", encoding="utf-8", errors="replace") as f:
                for ln, line in enumerate(f, 1):
                    if self._stop:
                        return added, False
                    raw = line.rstrip("\r\n")
                    for m in pattern.finditer(raw):
                        text = raw if len(raw) <= MAX_LINE_LEN else raw[:MAX_LINE_LEN] + "…"
                        batch.append(ContentMatch(
                            abs_path=str(abs_p), rel_path=rel,
                            line_no=ln, line_text=text,
                            col_start=m.start(), col_end=m.end(),
                        ))
                        added += 1
                        if total + added >= MAX_MATCHES:
                            return added, True
                        break   # 一行只记一次，减少噪音
        except OSError:
            pass
        return added, False

    def _grep_file_buffered(self, path: str, pattern) -> list[ContentMatch]:
        """Read one file once; reject non-matches before splitting it into lines."""
        if self._stop:
            return []
        abs_p = Path(path)
        try:
            data = abs_p.read_bytes()
        except OSError:
            return []
        if self._stop:
            return []
        if self._literal_needle is not None and self._literal_needle not in data:
            return []
        text = data.decode("utf-8", errors="replace")
        # 普通文本/整词可先整文件排除。正则必须保持原有逐行语义，
        # 例如 ^foo 应能匹配任意一行的开头，不能用整文件 search 预判。
        if not self.regex and pattern.search(text) is None:
            return []
        try:
            rel = str(abs_p.relative_to(self.root)).replace("\\", "/")
        except ValueError:
            return []
        matches: list[ContentMatch] = []
        for line_no, line in enumerate(text.splitlines(), 1):
            if self._stop:
                return []
            match = pattern.search(line)
            if match is None:
                continue
            shown = line if len(line) <= MAX_LINE_LEN else line[:MAX_LINE_LEN] + "…"
            matches.append(ContentMatch(
                abs_path=str(abs_p), rel_path=rel,
                line_no=line_no, line_text=shown,
                col_start=match.start(), col_end=match.end(),
            ))
        return matches

    def _run_cached(self, pattern) -> None:
        """走缓存清单：无 os.walk / stat，逐个文件 grep。"""
        scanned = 0
        total = 0
        batch: list[ContentMatch] = []
        candidates: list[str] = []
        for path in self.file_list:
            if self._stop:
                self.stopped.emit()
                return
            if is_scan_path_excluded(path, self.excluded_roots):
                continue
            fn = Path(path).name
            if not self.include_noise and _is_noise_file(fn):
                continue
            ext = Path(path).suffix.lower()
            if self.include_exts and ext not in self.include_exts:
                continue
            abs_p = Path(path)
            try:
                if abs_p.stat().st_size > MAX_FILE_SIZE:
                    continue
            except OSError:
                continue
            candidates.append(path)

        with ThreadPoolExecutor(
            max_workers=SEARCH_IO_WORKERS, thread_name_prefix="content-search",
        ) as executor:
            for matches in executor.map(
                lambda path: self._grep_file_buffered(path, pattern), candidates,
                chunksize=8,
            ):
                if self._stop:
                    executor.shutdown(wait=True, cancel_futures=True)
                    self.stopped.emit()
                    return
                scanned += 1
                remaining = MAX_MATCHES - total
                if matches:
                    batch.extend(matches[:remaining])
                    total += min(len(matches), remaining)
                if total >= MAX_MATCHES:
                    if batch:
                        self.match_found.emit(batch)
                    self.done.emit(scanned, total)
                    return
                if len(batch) >= 30:
                    self.match_found.emit(batch)
                    batch = []
                if scanned % 100 == 0:
                    self.progress.emit(scanned, total)
        if batch:
            self.match_found.emit(batch)
        self.done.emit(scanned, total)

    def _run_walk(self, pattern) -> None:
        collected: list[str] = []
        scanned = 0
        total = 0
        batch: list[ContentMatch] = []

        for dirpath, dirnames, filenames in os.walk(self.root):
            if self._stop:
                self.stopped.emit()
                return
            prune_scan_dirnames(
                Path(dirpath), dirnames, self.excluded_roots, allow_env=True,
            )
            for fn in filenames:
                if self._stop:
                    self.stopped.emit()
                    return
                ext = Path(fn).suffix.lower()
                if ext in IGNORED_EXTS:
                    continue
                abs_p = Path(dirpath) / fn
                try:
                    if abs_p.stat().st_size > MAX_FILE_SIZE:
                        continue
                except OSError:
                    continue
                # 收集进缓存清单（含所有文本文件，含噪音文件；ext_filter 与噪音
                # 过滤都在 grep 阶段才做，这样换扩展名/切换"含日志"无需重新 walk）
                collected.append(str(abs_p))
                if not self.include_noise and _is_noise_file(fn):
                    continue
                if self.include_exts and ext not in self.include_exts:
                    continue
                scanned += 1
                added, capped = self._grep_file(abs_p, pattern, batch, scanned, total)
                total += added
                if capped:
                    self.match_found.emit(batch)
                    self.done.emit(scanned, total)
                    return

                if len(batch) >= 30:
                    self.match_found.emit(batch)
                    batch = []
                    self.progress.emit(scanned, total)
            if len(batch) >= 10:
                self.match_found.emit(batch)
                batch = []
                self.progress.emit(scanned, total)

        if batch:
            self.match_found.emit(batch)
        self.files_collected.emit(collected)
        self.done.emit(scanned, total)



class _HighlightDelegate(QStyledItemDelegate):
    """搜索结果第二列的命中高亮 delegate。

    只对子节点（命中行内容）做高亮：把命中段染琥珀底+亮琥珀字+加粗，让眼睛
    一眼定位到 query 在长行里的位置。父节点（路径 / 命中数）走默认绘制。

    实现：用 QTextDocument 渲染 RichText，避免手算字宽位置；selection / hover
    背景由 style.drawControl 走默认主题逻辑，保持视觉一致。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pattern = None

    def set_pattern(self, pattern) -> None:
        self._pattern = pattern

    def paint(self, painter, option, index):
        # 只对"子节点 + 第二列 + 有 pattern"的情况启用高亮，其余走默认
        if (self._pattern is None
                or index.column() != 1
                or not index.parent().isValid()):
            super().paint(painter, option, index)
            return

        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        if not text:
            super().paint(painter, option, index)
            return

        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        # 不让默认绘制画文字（我们自己用 QTextDocument 画带高亮的版本）
        opt.text = ""
        widget = opt.widget
        style = widget.style() if widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)

        doc = QTextDocument()
        doc.setDefaultFont(opt.font)
        doc.setDocumentMargin(0)
        doc.setPlainText(text)

        # 高亮命中段
        fmt = QTextCharFormat()
        fmt.setBackground(QColor(str(HIGHLIGHT_MATCH_BG)))
        fmt.setForeground(QColor(str(HIGHLIGHT_MATCH_FG)))
        fmt.setFontWeight(700)
        cursor = QTextCursor(doc)
        try:
            for m in self._pattern.finditer(text):
                if m.start() == m.end():
                    continue   # 空匹配（如 ".*"）跳过，否则死循环式无意义高亮
                cursor.setPosition(m.start())
                cursor.setPosition(m.end(), QTextCursor.MoveMode.KeepAnchor)
                cursor.mergeCharFormat(fmt)
        except Exception:
            pass   # 任何异常都退化为不高亮，至少不能崩

        text_rect = style.subElementRect(
            QStyle.SubElement.SE_ItemViewItemText, opt, widget)
        painter.save()
        painter.translate(text_rect.topLeft())
        painter.setClipRect(0, 0, text_rect.width(), text_rect.height())
        ctx = QAbstractTextDocumentLayout.PaintContext()
        # 默认（非高亮段）文字色：选中态用 FG_BRIGHT，正常态用 FG_PRIMARY
        if option.state & QStyle.StateFlag.State_Selected:
            ctx.palette.setColor(QPalette.ColorRole.Text, QColor(str(FG_BRIGHT)))
        else:
            ctx.palette.setColor(QPalette.ColorRole.Text, QColor(str(FG_PRIMARY)))
        doc.documentLayout().draw(painter, ctx)
        painter.restore()


class ContentSearchDialog(QDialog):
    """全项目内容搜索对话框"""

    open_requested = Signal(str, int, int)   # abs_path, line, col

    def __init__(self, project_root: str, parent=None,
                 indexer: FileIndexer | None = None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.root = project_root
        self.indexer = indexer
        self.setWindowTitle(f"在项目中搜索   (Ctrl+Shift+F)   —   {project_root}")
        self.resize(1100, 700)

        self._worker: SearchWorker | None = None
        self._pending_restart = False
        # 整项目候选文件清单缓存：首次搜索 walk 时填充，之后同会话内复用，
        # 避免每次输入都重新遍历目录树 + stat（大仓库下这是卡顿主因）
        self._file_cache: list[str] | None = None
        self._render_queue: deque[ContentMatch] = deque()
        self._pending_terminal: tuple[str, int, int] | None = None
        self._discard_results = False
        self._search_started = 0.0
        self._first_batch_logged = False

        self._render_timer = QTimer(self)
        self._render_timer.setInterval(RENDER_INTERVAL_MS)
        self._render_timer.timeout.connect(self._flush_matches)

        # 输入即搜去抖：停顿 250ms 才真正触发，避免每个按键都起一个 worker
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self._start_search)

        root_lay = QVBoxLayout(self)
        root_lay.setContentsMargins(10, 10, 10, 10)

        # 查询输入
        top = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("输入文本或正则（停顿即搜）...")
        self.input.returnPressed.connect(self._start_search)
        self.input.textChanged.connect(self._on_query_changed)
        top.addWidget(self.input, 1)

        self.ext_filter = QLineEdit()
        self.ext_filter.setPlaceholderText("文件扩展名，逗号分隔，如 .java,.yml（留空=所有文本文件）")
        self.ext_filter.setFixedWidth(360)
        self.ext_filter.textChanged.connect(self._on_query_changed)
        top.addWidget(self.ext_filter)

        # 搜索框统一样式（theme.apply_search_style）：白字 + 等宽 + 加大 + 提亮 placeholder
        apply_search_style(self.input)
        apply_search_style(self.ext_filter)

        root_lay.addLayout(top)

        # 选项
        opts = QHBoxLayout()
        self.chk_case = QCheckBox("大小写")
        self.chk_word = QCheckBox("整词")
        self.chk_regex = QCheckBox("正则")
        # 默认隐藏日志/压缩产物/lock 等噪音文件，勾上才纳入搜索
        self.chk_noise = QCheckBox("含日志等")
        self.chk_noise.setToolTip("默认隐藏 .log / .min.js / source map / lock 等噪音文件；勾选后一并搜索")
        # 勾选项变化也立即重搜（去抖统一走 _on_query_changed）
        self.chk_case.toggled.connect(self._on_query_changed)
        self.chk_word.toggled.connect(self._on_query_changed)
        self.chk_regex.toggled.connect(self._on_query_changed)
        self.chk_noise.toggled.connect(self._on_query_changed)
        opts.addWidget(self.chk_case)
        opts.addWidget(self.chk_word)
        opts.addWidget(self.chk_regex)
        opts.addWidget(self.chk_noise)
        opts.addStretch(1)

        self.btn_search = QPushButton("搜索")
        self.btn_search.setProperty("role", "primary")
        self.btn_search.clicked.connect(self._start_search)
        opts.addWidget(self.btn_search)

        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop_search)
        opts.addWidget(self.btn_stop)

        root_lay.addLayout(opts)

        self.status = QLabel("就绪")
        self.status.setStyleSheet(f"color:{FG_SECONDARY};")
        root_lay.addWidget(self.status)

        # 结果
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["文件 / 行号", "目录 / 内容"])
        self.tree.itemActivated.connect(self._on_activate)
        self.tree.setIndentation(20)
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(False)
        self.tree.setUniformRowHeights(True)
        # 第一列只放"文件名 / 行号"（短），自适应内容；第二列吃掉剩余空间放路径 / 行内容
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setStretchLastSection(True)
        # 默认 branch 三角箭头颜色取自 palette.Text，深底上的黑三角几乎看不见；
        # 把 Text role 改成 FG_SECONDARY，三角同步变浅蓝灰，可见度大幅提升
        tree_pal = self.tree.palette()
        tree_pal.setColor(QPalette.ColorRole.Text, QColor(str(FG_SECONDARY)))
        self.tree.setPalette(tree_pal)
        # 第二列装高亮 delegate（命中行内容里的 query 关键词色块标记）
        self._delegate = _HighlightDelegate(self.tree)
        self.tree.setItemDelegateForColumn(1, self._delegate)
        root_lay.addWidget(self.tree, 1)

    def refresh_theme(self) -> None:
        self.status.setStyleSheet(f"color:{FG_SECONDARY};")
        palette = self.tree.palette()
        palette.setColor(QPalette.ColorRole.Text, QColor(str(FG_SECONDARY)))
        self.tree.setPalette(palette)
        for index in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(index)
            parent.setForeground(0, QColor(str(FG_PRIMARY)))
            parent.setForeground(1, QColor(str(FG_SECONDARY)))
            for child_index in range(parent.childCount()):
                child = parent.child(child_index)
                child.setForeground(0, QColor(str(FG_DIM)))
                child.setForeground(1, QColor(str(FG_PRIMARY)))
        self.tree.viewport().update()

    # ---- 搜索控制 ----

    def _on_query_changed(self, _text: str = "") -> None:
        """输入框 / 扩展名变化：重启去抖定时器，停顿后自动搜。

        query 为空时不搜，并清空结果与"搜索中"状态。
        """
        if not self.input.text().strip():
            self._debounce.stop()
            self._stop_search()
            self.tree.clear()
            self.status.setText("就绪")
            return
        self._debounce.start()

    def _candidate_files(self) -> list[str] | None:
        if self.indexer is not None and self.indexer.has_complete_snapshot():
            return [item.abs_path for item in self.indexer.files()]
        return self._file_cache

    def _start_search(self) -> None:
        self._debounce.stop()
        query = self.input.text().strip()
        if not query:
            return
        if self._worker and self._worker.isRunning():
            self._stop_search(restart=True)
            return

        exts = [e.strip() if e.strip().startswith(".") else ("." + e.strip())
                for e in self.ext_filter.text().split(",") if e.strip()]

        self._render_timer.stop()
        self._render_queue.clear()
        self._pending_terminal = None
        self._discard_results = False
        self._search_started = time.perf_counter()
        self._first_batch_logged = False
        self.tree.setUpdatesEnabled(False)
        self.tree.clear()
        self.tree.setUpdatesEnabled(True)
        self._file_items: dict[str, QTreeWidgetItem] = {}
        self._file_counts: dict[str, int] = {}
        self._file_dirs: dict[str, str] = {}
        self.status.setText("搜索中...")
        self.btn_search.setEnabled(False)
        self.btn_stop.setEnabled(True)

        # 把 pattern 也给 delegate 一份做命中高亮（与 worker 内编译同源同语义）
        try:
            hi_pattern = _compile_pattern(
                query, self.chk_case.isChecked(),
                self.chk_word.isChecked(), self.chk_regex.isChecked())
        except re.error:
            hi_pattern = None
        self._delegate.set_pattern(hi_pattern)

        file_list = self._candidate_files()
        perf_log.info(
            "perf op=content-search-start files=%d query_len=%d status=start",
            len(file_list) if file_list is not None else -1, len(query),
        )
        self._worker = SearchWorker(
            root=self.root, query=query,
            case_sensitive=self.chk_case.isChecked(),
            whole_word=self.chk_word.isChecked(),
            use_regex=self.chk_regex.isChecked(),
            include_exts=exts, file_list=file_list,
            include_noise=self.chk_noise.isChecked(),
            parent=QApplication.instance(),
        )
        self._worker.match_found.connect(self._on_matches)
        self._worker.progress.connect(self._on_progress)
        self._worker.done.connect(self._on_done)
        self._worker.stopped.connect(self._on_stopped)
        self._worker.files_collected.connect(self._on_files_collected)
        register_search_worker(self._worker, self._worker.stop)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    def _on_files_collected(self, files: list) -> None:
        if self.sender() is not self._worker:
            return
        # 仅首次 walk 的 worker 会发这个信号；缓存住供后续输入复用
        self._file_cache = files

    def _stop_search(self, restart: bool = False) -> None:
        if self._worker and self._worker.isRunning():
            self._pending_restart = restart
            self._discard_results = True
            self._render_timer.stop()
            self._render_queue.clear()
            self._pending_terminal = None
            self._worker.stop()
            self.status.setText("正在停止...")
            self.btn_search.setEnabled(False)
            self.btn_stop.setEnabled(False)
        elif not restart:
            self._pending_restart = False

    def _on_matches(self, batch: list[ContentMatch]) -> None:
        if self.sender() is not self._worker or self._discard_results:
            return
        if batch and not self._first_batch_logged:
            self._first_batch_logged = True
            perf_log.info(
                "perf op=content-search-first-batch duration_ms=%.1f matches=%d status=done",
                (time.perf_counter() - self._search_started) * 1000, len(batch),
            )
        self._render_queue.extend(batch)
        if not self._render_timer.isActive():
            self._render_timer.start()

    def _flush_matches(self) -> None:
        batch: list[ContentMatch] = []
        while self._render_queue and len(batch) < RENDER_BATCH_SIZE:
            batch.append(self._render_queue.popleft())
        if batch:
            self._render_match_batch(batch)
        if self._render_queue:
            return
        self._render_timer.stop()
        if self._pending_terminal is not None:
            self._finish_terminal()

    def _render_match_batch(self, batch: list[ContentMatch]) -> None:
        touched: set[str] = set()
        self.tree.setUpdatesEnabled(False)
        try:
            for match in batch:
                parent = self._file_items.get(match.rel_path)
                if parent is None:
                    path = Path(match.rel_path)
                    parent_dir = str(path.parent).replace("\\", "/")
                    if parent_dir == ".":
                        parent_dir = ""
                    parent = QTreeWidgetItem([f"📄  {path.name}", parent_dir])
                    bold = QFont(parent.font(0))
                    bold.setBold(True)
                    bold.setPointSize(bold.pointSize() + 1)
                    parent.setFont(0, bold)
                    parent.setForeground(0, QColor(str(FG_PRIMARY)))
                    parent.setForeground(1, QColor(str(FG_SECONDARY)))
                    parent.setToolTip(0, match.abs_path)
                    parent.setToolTip(1, match.rel_path)
                    self.tree.addTopLevelItem(parent)
                    parent.setExpanded(True)
                    self._file_items[match.rel_path] = parent
                    self._file_counts[match.rel_path] = 0
                    self._file_dirs[match.rel_path] = parent_dir
                child = QTreeWidgetItem([f"   L {match.line_no}", match.line_text])
                child.setData(
                    0, Qt.ItemDataRole.UserRole,
                    (match.abs_path, match.line_no, match.col_start),
                )
                child.setForeground(0, QColor(str(FG_DIM)))
                child.setForeground(1, QColor(str(FG_PRIMARY)))
                parent.addChild(child)
                self._file_counts[match.rel_path] += 1
                touched.add(match.rel_path)
            for rel_path in touched:
                parent = self._file_items[rel_path]
                directory = self._file_dirs[rel_path]
                count = self._file_counts[rel_path]
                parent.setText(
                    1,
                    f"{directory}    ·    {count} 处命中"
                    if directory else f"{count} 处命中",
                )
        finally:
            self.tree.setUpdatesEnabled(True)
            self.tree.viewport().update()

    def _on_progress(self, scanned: int, total: int) -> None:
        if self.sender() is not self._worker:
            return
        self.status.setText(f"已扫 {scanned} 文件 | 命中 {total}")

    def _on_done(self, scanned: int, total: int) -> None:
        if self.sender() is not self._worker:
            return
        self._worker = None
        state = "done" if self.input.text().strip() else "cleared"
        self._pending_terminal = (state, scanned, total)
        if self._render_queue:
            if not self._render_timer.isActive():
                self._render_timer.start()
        else:
            self._finish_terminal()

    def _on_stopped(self) -> None:
        if self.sender() is not self._worker:
            return
        self._render_timer.stop()
        self._render_queue.clear()
        self._worker = None
        state = "stopped" if self.input.text().strip() else "cleared"
        self._pending_terminal = (state, 0, 0)
        self._finish_terminal()

    def _finish_terminal(self) -> None:
        if self._pending_terminal is None:
            return
        state, scanned, total = self._pending_terminal
        self._pending_terminal = None
        duration_ms = (time.perf_counter() - self._search_started) * 1000
        if state == "done":
            self.status.setText(
                f"完成：扫描 {scanned} 文件 | 命中 {total}" +
                (f"  (达到上限 {MAX_MATCHES})" if total >= MAX_MATCHES else "")
            )
        elif state == "stopped":
            self.status.setText("已停止")
        else:
            self.status.setText("就绪")
        perf_log.info(
            "perf op=content-search duration_ms=%.1f files=%d matches=%d status=%s",
            duration_ms, scanned, total, state,
        )
        self.btn_search.setEnabled(True)
        self.btn_stop.setEnabled(False)
        if self._pending_restart:
            self._pending_restart = False
            QTimer.singleShot(0, self._start_search)

    def _on_activate(self, item: QTreeWidgetItem, _col: int) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data:
            path, line, col = data
            self.open_requested.emit(path, line, col)

    def closeEvent(self, e):
        self._pending_restart = False
        self._stop_search()
        super().closeEvent(e)
