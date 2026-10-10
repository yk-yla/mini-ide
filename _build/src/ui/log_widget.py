"""智能日志组件

负责：
- 按 classifier 的结果高亮着色
- 异常堆栈明细默认折叠
- 文件:行号 可点击跳转
- 搜索（Ctrl+F）+ 正则
- 错误计数、诊断提示浮层
- 日志落盘到 %APPDATA%/mini-ide/logs/
- 右键菜单：复制 / 跳转文件 / 清空 / 打开日志文件
"""
from __future__ import annotations

from collections import deque
import logging
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path


# ANSI 转义序列：CSI（颜色/格式）+ OSC（超链接等）
# loguru / rich / colorama 即便设置 NO_COLOR 也常常输出这些码，
# 进 QTextEdit 后显示成一堆 [32m[0m 的乱码，直接剥掉。
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\].*?(?:\x07|\x1b\\)|\x1b[=>]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)

from PySide6.QtCore import Qt, QTimer, Signal, QUrl, QRegularExpression
from PySide6.QtGui import (
    QAction, QColor, QDesktopServices, QFont, QTextCharFormat,
    QTextCursor, QTextDocument, QKeySequence, QMouseEvent, QTextBlockUserData, QTextLayout,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QHBoxLayout, QLabel, QLineEdit, QMenu,
    QPlainTextEdit, QToolTip, QVBoxLayout, QWidget, QToolButton,
)

from src.core import log_classifier as lc
from src.core.config import LOG_DIR
from src.ui.theme import (
    BG_DIAGNOSIS, BORDER_DIAGNOSIS,
    COLOR_BANNER, COLOR_DEBUG, COLOR_ERROR, COLOR_INFO, COLOR_LINK,
    COLOR_SQL, COLOR_SUCCESS, COLOR_WARN,
    FG_DIM, FG_PRIMARY, FG_SECONDARY, apply_search_style,
)


LOG_FLUSH_BATCH_SIZE = 500
LOG_FLUSH_BUDGET_SECONDS = 0.008
LOG_PENDING_CHARS = 2_000_000
LOG_LINE_CHARS = 20_000
SQL_FORMAT_CHARS = 4_096
perf_log = logging.getLogger("mini-ide.performance")


# ---- 每行附带的分类数据 ----

@dataclass
class LineMeta:
    kind: str = "plain"
    stream: str = "stdout"
    jumps: list[lc.FileJump] = field(default_factory=list)
    stack_group: str = ""
    diagnosis: str = ""


class LogBlockData(QTextBlockUserData):
    def __init__(self, meta: LineMeta):
        super().__init__()
        self.meta = meta
        self.theme_generation = -1


def _block_meta(block) -> LineMeta | None:
    data = block.userData()
    return data.meta if isinstance(data, LogBlockData) else None


class _LogTextEdit(QPlainTextEdit):
    """颜色属于视图：只给本次要画的 block 设置 layout 格式。"""

    def __init__(self, owner):
        super().__init__()
        self.owner = owner

    def paintEvent(self, event) -> None:
        block = self.firstVisibleBlock()
        bottom = self.viewport().height()
        while block.isValid():
            y = self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
            if y > bottom:
                break
            data = block.userData()
            if isinstance(data, LogBlockData) and data.theme_generation != self.owner._theme_generation:
                ranges = []
                base = QTextLayout.FormatRange()
                base.start, base.length = 0, block.length() - 1
                base.format = self.owner._fmt.get(data.meta.kind, self.owner._fmt["plain"])
                ranges.append(base)
                text = block.text()
                for jump in data.meta.jumps:
                    fmt = QTextLayout.FormatRange()
                    fmt.start = len(text[:jump.start].encode("utf-16-le")) // 2
                    fmt.length = len(text[jump.start:jump.end].encode("utf-16-le")) // 2
                    fmt.format = self.owner._fmt_link
                    ranges.append(fmt)
                block.layout().setFormats(ranges)
                data.theme_generation = self.owner._theme_generation
            block = block.next()
        super().paintEvent(event)


class LogWidget(QWidget):
    """日志区：顶部一条工具条 + 中间日志 + 底部搜索条"""

    fileJumpRequested = Signal(str, int, int)       # path, line, col
    contentAdded = Signal()                         # 有新内容写入（多模块时用来把隐藏的日志 tab 显示回来）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ctx = lc.LineContext()
        self._theme_generation = 0
        self._error_count = 0
        self._warn_count = 0
        self._log_file: Path | None = None
        self._persistent_fh = None
        self._last_diagnosis = ""
        self._run_label = ""
        self._run_started_at: float = 0.0
        self._run_ended_at: float = 0.0
        self._first_error = ""
        self._ready_seen = False
        self._external_running = False
        self._new_run = False

        # 批量刷新：高吞吐日志（如 Gradle 初扫）会飙到 1000+行/秒，
        # 单行 insert 会卡主线程。把接收到的行先塞到队列，每 50ms 集中写入一次。
        self._pending: deque[tuple[str, LineMeta]] = deque()
        self._pending_chars = 0
        self._display_skipped = 0
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(50)
        self._flush_timer.timeout.connect(self._flush_pending)

        self._hide_stack = True

        self._build_ui()
        self._apply_formats()

    # ---- UI 构建 ----

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.toolbar = QWidget()
        self.toolbar.setObjectName("compact_toolbar")
        tb = QHBoxLayout(self.toolbar)
        tb.setContentsMargins(6, 4, 6, 4)
        tb.setSpacing(6)

        self.lbl_counts = QLabel("")
        self.lbl_counts.setProperty("role", "subtitle")
        tb.addWidget(self.lbl_counts, 1)

        self.btn_clear = QToolButton()
        self.btn_clear.setText("🗑")
        self.btn_clear.setToolTip("清空日志")
        self.btn_clear.clicked.connect(self.clear)
        tb.addWidget(self.btn_clear)

        root.addWidget(self.toolbar)

        self.edit = _LogTextEdit(self)
        self.edit.setReadOnly(True)
        self.edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        # 默认 10000 行，可通过 AppConfig.max_log_blocks 调整（LogWidget 不直接依赖 config，
        # 由上层项目 Tab 在构造后调 set_max_blocks）
        self.edit.setMaximumBlockCount(10_000)
        self.edit.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.edit.customContextMenuRequested.connect(self._on_context_menu)
        self.edit.mouseDoubleClickEvent = self._on_double_click   # type: ignore[assignment]
        root.addWidget(self.edit, 1)

        # 诊断条：文本 + 可选端口按钮
        self.diagnosis_bar = QWidget()
        self.diagnosis_bar.setVisible(False)
        self.diagnosis_bar.setStyleSheet(
            f"background: {BG_DIAGNOSIS}; border-top: 1px solid {BORDER_DIAGNOSIS};"
        )
        db_layout = QHBoxLayout(self.diagnosis_bar)
        db_layout.setContentsMargins(10, 6, 10, 6)
        db_layout.setSpacing(8)
        self.diagnosis_text = QLabel("")
        self.diagnosis_text.setWordWrap(True)
        self.diagnosis_text.setStyleSheet(f"color: {COLOR_WARN}; background: transparent;")
        db_layout.addWidget(self.diagnosis_text, 1)
        root.addWidget(self.diagnosis_bar)

        # 搜索栏
        self.search_bar = QWidget()
        self.search_bar.setObjectName("compact_toolbar")
        sb = QHBoxLayout(self.search_bar)
        sb.setContentsMargins(6, 4, 6, 4)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("搜索日志，按回车跳转下一个")
        apply_search_style(self.search_input)
        self.search_input.returnPressed.connect(self._find_next)
        sb.addWidget(self.search_input, 1)
        self.chk_regex = QCheckBox("正则")
        sb.addWidget(self.chk_regex)
        self.chk_case = QCheckBox("大小写")
        sb.addWidget(self.chk_case)
        btn_prev = QToolButton(); btn_prev.setText("↑"); btn_prev.clicked.connect(self._find_prev)
        btn_next = QToolButton(); btn_next.setText("↓"); btn_next.clicked.connect(self._find_next)
        btn_close = QToolButton(); btn_close.setText("×"); btn_close.clicked.connect(self._toggle_search)
        sb.addWidget(btn_prev); sb.addWidget(btn_next); sb.addWidget(btn_close)
        self.search_bar.setVisible(False)
        root.addWidget(self.search_bar)

        act_find = QAction(self)
        act_find.setShortcut(QKeySequence("Ctrl+F"))
        act_find.triggered.connect(self._toggle_search)
        self.addAction(act_find)
        act_find.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)

    def _apply_formats(self) -> None:
        def mk(color: str, bold: bool = False, italic: bool = False) -> QTextCharFormat:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(str(color)))
            if bold:
                f = QFont(); f.setBold(True)
                fmt.setFontWeight(QFont.Weight.Bold)
            if italic:
                fmt.setFontItalic(True)
            return fmt
        self._fmt = {
            "error":        mk(COLOR_ERROR, bold=True),
            "caused_by":    mk(COLOR_ERROR, bold=True),
            "stack":        mk(FG_DIM, italic=True),
            "warn":         mk(COLOR_WARN),
            "info":         mk(FG_PRIMARY),
            "debug":        mk(COLOR_DEBUG, italic=True),
            "trace":        mk(COLOR_DEBUG, italic=True),
            "sql":          mk(COLOR_SQL),
            "sql_continuation": mk(COLOR_SQL),
            "startup_ready": mk(COLOR_SUCCESS, bold=True),
            "startup_banner": mk(COLOR_BANNER),
            "meta":         mk(FG_SECONDARY, italic=True),
            "plain":        mk(FG_PRIMARY),
        }
        self._fmt_link = QTextCharFormat()
        self._fmt_link.setForeground(QColor(str(COLOR_LINK)))
        self._fmt_link.setUnderlineStyle(QTextCharFormat.UnderlineStyle.SingleUnderline)

    def refresh_theme(self) -> None:
        self._apply_formats()
        self._theme_generation += 1
        self.edit.viewport().update()
        self._update_counts(None)

    # ---- 对外 API ----

    def set_max_blocks(self, n: int) -> None:
        self.edit.setMaximumBlockCount(max(500, n))

    def begin_run(self, label: str) -> None:
        """一次运行开始：重置堆栈上下文并打开日志文件"""
        self.contentAdded.emit()
        self._ctx = lc.LineContext()
        self._error_count = 0
        self._warn_count = 0
        self._last_diagnosis = ""
        self._run_label = label
        self._run_started_at = time.time()
        self._run_ended_at = 0.0
        self._first_error = ""
        self._ready_seen = False
        self._external_running = False
        self._new_run = True          # 新运行开始，下次 flush 强制滚到底部
        self.diagnosis_bar.setVisible(False)
        self._close_log_file()
        ts = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000_000:09d}"
        safe_label = re.sub(r"[^\w\-.]+", "_", label)
        self._log_file = LOG_DIR / f"{safe_label}_{ts}.log"
        try:
            self._persistent_fh = self._log_file.open("w", encoding="utf-8")
        except OSError:
            self._persistent_fh = None

    def end_run(self) -> None:
        self._run_ended_at = time.time()
        self._update_counts(None)
        self._close_log_file()

    def mark_external_running(self, label: str, pid: int | None = None, port: int | None = None) -> None:
        self._ctx = lc.LineContext()
        self._error_count = 0
        self._warn_count = 0
        self._run_label = label
        self._run_started_at = 0.0
        self._run_ended_at = 0.0
        self._first_error = ""
        self._ready_seen = False
        self._external_running = True
        detail = f"{label} 正在 mini-ide 之外运行"
        if pid:
            detail += f"（PID {pid}"
            if port:
                detail += f"，端口 {port}"
            detail += "）"
        elif port:
            detail += f"（端口 {port}）"
        detail += "；当前 IDE 没有这次启动日志上下文。"
        self._last_diagnosis = detail
        self._show_diagnosis(detail)
        self._update_counts(None)

    def diagnosis_summary(self, max_chars: int = 4000) -> str:
        status = "运行中" if self._run_started_at and not self._run_ended_at else "已结束"
        if self._ready_seen:
            status = "已启动"
        if not self._run_started_at:
            status = "无本次运行日志"
        if self._external_running:
            status = "外部运行（无当前 IDE 日志）"
        lines = [
            f"服务：{self._run_label or '(未知)'}",
            f"状态：{status}",
            f"错误数：{self._error_count}",
            f"警告数：{self._warn_count}",
        ]
        if self._run_started_at:
            lines.append(f"开始时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self._run_started_at))}")
        if self._run_ended_at:
            lines.append(f"结束时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self._run_ended_at))}")
        if self._first_error:
            lines.append(f"首条错误：{self._first_error}")
        if self._last_diagnosis:
            lines.append(f"诊断：{self._last_diagnosis}")
        snippets = self.error_snippets(max_chars=max_chars)
        if snippets:
            lines.append("")
            lines.append("关键日志：")
            lines.append(snippets)
        return "\n".join(lines)

    def _close_log_file(self) -> None:
        if self._persistent_fh:
            try:
                self._persistent_fh.close()
            except OSError:
                pass
            self._persistent_fh = None

    def append_line(self, stream: str, line: str) -> None:
        """追加一行日志。实际写入由 _flush_pending 批量完成，避免高频抖动"""
        self.contentAdded.emit()
        if "\x1b" in line:
            line = _strip_ansi(line)
        # 原始内容不受视图容量限制；分类也在淘汰显示之前完成。
        if self._persistent_fh:
            try:
                self._persistent_fh.write(line + "\n")
            except OSError:
                pass
        cls = lc.classify(line, self._ctx)
        meta = LineMeta(cls.kind, stream, cls.jumps, cls.stack_group_id, cls.diagnosis)
        if cls.kind in ("error", "caused_by"):
            self._error_count += 1
            if not self._first_error:
                self._first_error = line[:LOG_LINE_CHARS]
        elif cls.kind == "warn":
            self._warn_count += 1
        if cls.kind == "startup_ready":
            self._ready_seen = True
        if cls.diagnosis and cls.diagnosis != self._last_diagnosis:
            self._last_diagnosis = cls.diagnosis
            self._show_diagnosis(cls.diagnosis)
        display = line
        if len(display) > LOG_LINE_CHARS:
            display = display[:LOG_LINE_CHARS] + " …（显示已截断，完整内容见日志文件）"
            meta = replace(meta, jumps=[j for j in meta.jumps if j.end <= LOG_LINE_CHARS])
        self._pending.append((display, meta))
        self._pending_chars += len(display)
        limit = self.edit.maximumBlockCount()
        while len(self._pending) > limit or self._pending_chars > LOG_PENDING_CHARS:
            skipped, _ = self._pending.popleft()
            self._pending_chars -= len(skipped)
            self._display_skipped += 1
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def _flush_pending(self) -> None:
        if not self._pending:
            self._flush_timer.stop()
            return
        started = time.perf_counter()
        sb = self.edit.verticalScrollBar()
        was_at_bottom = sb.value() >= sb.maximum() - 4
        self.edit.setUpdatesEnabled(False)
        rendered = 0
        try:
            while self._pending and rendered < LOG_FLUSH_BATCH_SIZE:
                line, meta = self._pending.popleft()
                self._pending_chars -= len(line)
                display = _format_sql(line) if meta.kind == "sql" and ";" in line else line
                self._insert_block(display, meta)
                rendered += 1
                if time.perf_counter() - started >= LOG_FLUSH_BUDGET_SECONDS:
                    break
            # 同时限制行数和字符数，避免少量巨大行占满内存。
            doc = self.edit.document()
            if doc.characterCount() > LOG_PENDING_CHARS:
                boundary = doc.findBlock(doc.characterCount() - LOG_PENDING_CHARS)
                cursor = QTextCursor(doc)
                cursor.setPosition(boundary.position())
                cursor.movePosition(QTextCursor.MoveOperation.Start, QTextCursor.MoveMode.KeepAnchor)
                cursor.removeSelectedText()
        finally:
            self.edit.setUpdatesEnabled(True)
        self._update_counts(None)
        if self._new_run or was_at_bottom:
            self._new_run = False
            sb.setValue(sb.maximum())
        if self._persistent_fh:
            try:
                self._persistent_fh.flush()
            except OSError:
                pass
        duration_ms = (time.perf_counter() - started) * 1000
        if duration_ms >= 100:
            perf_log.warning("perf op=log-render duration_ms=%.1f files=%d status=slow", duration_ms, rendered)

    def clear(self) -> None:
        if self._flush_timer.isActive():
            self._flush_timer.stop()
        self._pending.clear()
        self._ctx = lc.LineContext()
        self.edit.clear()
        self._pending_chars = 0
        self._display_skipped = 0
        self._error_count = 0
        self._warn_count = 0
        self._last_diagnosis = ""
        self._first_error = ""
        self._external_running = False
        self._new_run = False
        self.diagnosis_bar.setVisible(False)
        self._update_counts(None)

    def error_snippets(self, max_chars: int = 8000) -> str:
        """收集 error/caused_by 行及其紧随的 stack 行，供右键复制用"""
        doc = self.edit.document()
        out_lines: list[str] = []
        block = doc.firstBlock()
        idx = 0
        total_chars = 0
        in_stack = False
        current_group = ""
        while block.isValid():
            meta = _block_meta(block)
            if meta:
                if meta.kind in ("error", "caused_by"):
                    out_lines.append(block.text())
                    total_chars += len(block.text())
                    in_stack = True
                    current_group = meta.stack_group
                elif meta.kind == "stack" and in_stack and meta.stack_group == current_group:
                    out_lines.append(block.text())
                    total_chars += len(block.text())
                elif meta.kind == "warn" and not in_stack:
                    out_lines.append(block.text())
                    total_chars += len(block.text())
                else:
                    if in_stack and meta.kind not in ("stack", "caused_by"):
                        in_stack = False
                        out_lines.append("")
            if total_chars > max_chars:
                out_lines.append("...（已截断）")
                break
            block = block.next()
            idx += 1
        return "\n".join(out_lines)

    def log_file_path(self) -> Path | None:
        return self._log_file

    # ---- 内部：插入与着色 ----

    def _insert_block(self, line: str, meta: LineMeta, cls=None) -> None:
        cursor = QTextCursor(self.edit.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        offset = 0
        for part in re.split(r"[\n\r\u2028\u2029]", line):
            if cursor.block().userData() is not None or self.edit.document().characterCount() > 1:
                cursor.insertBlock()
            jumps = [replace(j, start=j.start-offset, end=j.end-offset) for j in meta.jumps
                     if offset <= j.start and j.end <= offset + len(part)]
            block_meta = replace(meta, jumps=jumps)
            cursor.insertText(part)
            block = cursor.block()
            block.setUserData(LogBlockData(block_meta))
            block.setUserState(_kind_to_state(meta.kind))
            if self._hide_stack and meta.kind == "stack" and meta.stack_group:
                block.setVisible(False)
                block.setLineCount(0)
            offset += len(part) + 1

    def _update_counts(self, cls: lc.Classification | None) -> None:
        # 计数已在 _flush_pending 里累加，这里只更新 UI 标签
        parts = []
        if self._error_count:
            parts.append(f"<span style='color:{COLOR_ERROR};'>✗ {self._error_count} errors</span>")
        if self._warn_count:
            parts.append(f"<span style='color:{COLOR_WARN};'>⚠ {self._warn_count} warnings</span>")
        if not parts:
            parts.append(f"<span style='color:{FG_SECONDARY};'>0 errors</span>")
        if self._display_skipped:
            parts.append(f"显示跳过 {self._display_skipped} 条积压；完整内容见日志文件")
        self.lbl_counts.setText("  ".join(parts))

    def _show_diagnosis(self, text: str) -> None:
        """在诊断栏显示一行提示。"""
        self.diagnosis_text.setText("💡  " + text)
        self.diagnosis_bar.setVisible(True)

    # ---- 事件：点击跳转、右键、搜索 ----

    def _on_double_click(self, event: QMouseEvent) -> None:
        cursor = self.edit.cursorForPosition(event.position().toPoint())
        meta = _block_meta(cursor.block())
        if meta and meta.jumps:
            # 取最近的 jump
            col = len(cursor.block().text().encode("utf-16-le")[:cursor.columnNumber() * 2].decode("utf-16-le", errors="ignore"))
            jumps = meta.jumps
            target = min(jumps, key=lambda j: abs(((j.start + j.end) // 2) - col))
            self.fileJumpRequested.emit(target.path, target.line, target.column)
            return
        # 双击空白处也允许选中默认行为
        QPlainTextEdit.mouseDoubleClickEvent(self.edit, event)

    def _on_context_menu(self, pos) -> None:
        cursor = self.edit.cursorForPosition(pos)
        meta = _block_meta(cursor.block())

        menu = QMenu(self)
        menu.addAction("复制当前行", lambda: self._copy_line(cursor))
        menu.addAction("复制选中/当前异常", self._copy_error_context)
        menu.addSeparator()
        if meta and meta.jumps:
            jumps_submenu = menu.addMenu("跳转到文件")
            for j in meta.jumps:
                jumps_submenu.addAction(
                    f"{j.path}:{j.line}",
                    lambda p=j.path, ln=j.line, c=j.column: self.fileJumpRequested.emit(p, ln, c)
                )
        menu.addSeparator()
        menu.addAction("复制诊断给 AI", self._copy_diagnosis)
        menu.addAction("打开日志文件所在目录", self._open_log_dir)
        if self._log_file:
            menu.addAction("在默认编辑器打开完整日志", self._open_log_file)
        menu.addSeparator()
        menu.addAction("清空日志", self.clear)
        menu.exec(self.edit.mapToGlobal(pos))

    def _copy_line(self, cursor: QTextCursor) -> None:
        QApplication.clipboard().setText(cursor.block().text())

    def _copy_error_context(self) -> None:
        txt = self.edit.textCursor().selectedText()
        if not txt:
            txt = self.error_snippets(max_chars=2000)
        QApplication.clipboard().setText(txt.replace("\u2029", "\n"))

    def _copy_diagnosis(self) -> None:
        QApplication.clipboard().setText(self.diagnosis_summary(max_chars=4000))

    def _open_log_dir(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(LOG_DIR)))

    def _open_log_file(self) -> None:
        if self._log_file:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._log_file)))

    # ---- 搜索 ----

    def _toggle_search(self) -> None:
        visible = not self.search_bar.isVisible()
        self.search_bar.setVisible(visible)
        if visible:
            self.search_input.setFocus()
            self.search_input.selectAll()

    def _find_next(self) -> None:
        self._find(forward=True)

    def _find_prev(self) -> None:
        self._find(forward=False)

    def _set_search_status(self, msg: str) -> None:
        """在搜索框旁弹个气泡提示（如正则语法错误），不打断输入。"""
        QToolTip.showText(self.search_input.mapToGlobal(self.search_input.rect().bottomLeft()), msg, self.search_input)

    def _find(self, forward: bool) -> None:
        q = self.search_input.text()
        if not q:
            return
        flags = QTextDocument.FindFlag(0)
        if not forward:
            flags |= QTextDocument.FindFlag.FindBackward
        if self.chk_case.isChecked():
            flags |= QTextDocument.FindFlag.FindCaseSensitively

        # 正则模式用 QRegularExpression（QPlainTextEdit.find 不接受 Python 的 re.Pattern）；
        # 大小写靠 QRegularExpression 的 option 控制，flags 里的 FindCaseSensitively 对正则无效。
        needle: "str | QRegularExpression"
        if self.chk_regex.isChecked():
            opts = QRegularExpression.PatternOption.NoPatternOption
            if not self.chk_case.isChecked():
                opts = QRegularExpression.PatternOption.CaseInsensitiveOption
            rx = QRegularExpression(q, opts)
            if not rx.isValid():
                self._set_search_status(f"正则语法错误：{rx.errorString()}")
                return
            needle = rx
        else:
            needle = q

        found = self.edit.find(needle, flags)
        if not found:
            # 回绕一次
            cur = self.edit.textCursor()
            cur.movePosition(
                QTextCursor.MoveOperation.End if not forward else QTextCursor.MoveOperation.Start
            )
            self.edit.setTextCursor(cur)
            self.edit.find(needle, flags)


# ---- 工具函数 ----

def _kind_to_state(kind: str) -> int:
    mapping = {
        "error": 1, "caused_by": 1,
        "warn": 2,
        "info": 3,
        "stack": 4,
        "debug": 5, "trace": 5,
        "sql": 6, "sql_continuation": 6,
        "startup_ready": 7,
        "startup_banner": 8,
        "meta": 9,
        "plain": 0,
    }
    return mapping.get(kind, 0)


def _format_sql(line: str) -> str:
    """简单 SQL 美化：Hibernate 的一行 SQL 变成多行缩进"""
    if len(line) > SQL_FORMAT_CHARS:
        return line
    try:
        import sqlparse  # type: ignore
    except ImportError:
        return line
    m = re.match(r"^(Hibernate|SQL)\s*:\s*(.+)$", line, re.IGNORECASE | re.DOTALL)
    if not m:
        return line
    prefix, sql = m.group(1), m.group(2)
    # 只做词法换行，不构造 SQL 分组树；字符串和注释 token 原样保留。
    if len(sql) > LOG_LINE_CHARS:
        return line
    from sqlparse import tokens as T
    out = []
    depth = 0
    for token, value in sqlparse.lexer.tokenize(sql):
        if token in T.Keyword:
            value = value.upper()
            if depth == 0 and value in {"FROM", "WHERE", "GROUP BY", "ORDER BY", "HAVING", "LIMIT", "VALUES", "SET"}:
                out.append("\n    ")
        if token in T.Punctuation:
            if value == "(":
                depth += 1
            elif value == ")":
                depth = max(0, depth - 1)
            elif value == "," and depth == 0:
                out.append(",\n    ")
                continue
        out.append(value)
    return f"{prefix}: " + "".join(out)
