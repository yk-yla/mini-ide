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
    QTextCursor, QTextDocument, QKeySequence, QMouseEvent,
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
perf_log = logging.getLogger("mini-ide.performance")


# ---- 每行附带的分类数据 ----

@dataclass
class LineMeta:
    kind: str = "plain"
    stream: str = "stdout"
    jumps: list[lc.FileJump] = field(default_factory=list)
    stack_group: str = ""
    diagnosis: str = ""


class LogWidget(QWidget):
    """日志区：顶部一条工具条 + 中间日志 + 底部搜索条"""

    fileJumpRequested = Signal(str, int, int)       # path, line, col
    contentAdded = Signal()                         # 有新内容写入（多模块时用来把隐藏的日志 tab 显示回来）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ctx = lc.LineContext()
        self._lines: list[LineMeta] = []       # 每一块的元数据（与文档 block 一一对应）
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
        self._pending: deque[tuple[str, str]] = deque()
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

        self.edit = QPlainTextEdit()
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
        document = self.edit.document()
        block = document.firstBlock()
        while block.isValid():
            index = block.blockNumber()
            meta = self._lines[index] if index < len(self._lines) else None
            cursor = QTextCursor(block)
            cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
            cursor.setCharFormat(self._fmt.get(meta.kind if meta else "plain", self._fmt["plain"]))
            if meta:
                for jump in meta.jumps:
                    link_cursor = QTextCursor(document)
                    link_cursor.setPosition(block.position() + jump.start)
                    link_cursor.setPosition(
                        block.position() + jump.end,
                        QTextCursor.MoveMode.KeepAnchor,
                    )
                    link_cursor.setCharFormat(self._fmt_link)
            block = block.next()
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
        ts = time.strftime("%Y%m%d-%H%M%S")
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
        self._pending.append((stream, line))
        if self._persistent_fh:
            try:
                self._persistent_fh.write(line + "\n")
            except OSError:
                pass
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def _flush_pending(self) -> None:
        if not self._pending:
            self._flush_timer.stop()
            return
        started = time.perf_counter()
        batch = [
            self._pending.popleft()
            for _ in range(min(LOG_FLUSH_BATCH_SIZE, len(self._pending)))
        ]

        # 插入前采样：用户当前是否贴在底部。只有贴底时才在 flush 后自动跟随到
        # 最新——否则用户往上翻看历史会被每 50ms 一次的 flush 反复拽回底部。
        sb = self.edit.verticalScrollBar()
        was_at_bottom = sb.value() >= sb.maximum() - 4

        self.edit.setUpdatesEnabled(False)
        try:
            for stream, line in batch:
                cls = lc.classify(line, self._ctx)
                meta = LineMeta(
                    kind=cls.kind,
                    stream=stream,
                    jumps=cls.jumps,
                    stack_group=cls.stack_group_id,
                    diagnosis=cls.diagnosis,
                )
                display_line = line
                if cls.kind == "sql" and ";" in line:
                    display_line = _format_sql(line)

                self._insert_block(display_line, meta, cls)
                if cls.kind == "error" or cls.kind == "caused_by":
                    self._error_count += 1
                    if not self._first_error:
                        self._first_error = line
                elif cls.kind == "warn":
                    self._warn_count += 1
                if cls.kind == "startup_ready":
                    self._ready_seen = True
                if cls.diagnosis and cls.diagnosis != self._last_diagnosis:
                    self._last_diagnosis = cls.diagnosis
                    self._show_diagnosis(cls.diagnosis)
                if self._hide_stack and cls.kind == "stack" and bool(meta.stack_group):
                    self.edit.document().lastBlock().setVisible(False)
        finally:
            self.edit.setUpdatesEnabled(True)

        # 批次末尾统一刷新一次计数；新运行开始或用户贴底时跟随滚动到最新
        self._update_counts(None)
        if self._new_run or was_at_bottom:
            self._new_run = False
            sb.setValue(sb.maximum())

        # Qt 文档超限时会自动淘汰头部旧块，同步截断 _lines 保持一致
        max_blocks = self.edit.maximumBlockCount()
        if max_blocks > 0 and len(self._lines) > max_blocks:
            self._lines = self._lines[-max_blocks:]

        if self._persistent_fh:
            try:
                self._persistent_fh.flush()
            except OSError:
                pass
        duration_ms = (time.perf_counter() - started) * 1000
        if duration_ms >= 100:
            perf_log.warning(
                "perf op=log-render duration_ms=%.1f files=%d status=slow",
                duration_ms, len(batch),
            )

    def clear(self) -> None:
        if self._flush_timer.isActive():
            self._flush_timer.stop()
        self._pending.clear()
        self._ctx = lc.LineContext()
        self.edit.clear()
        self._lines.clear()
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
            meta = self._lines[idx] if idx < len(self._lines) else None
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

    def _insert_block(self, line: str, meta: LineMeta, cls: lc.Classification) -> None:
        cursor = self.edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        # 新段
        if self.edit.document().blockCount() > 0 and self.edit.document().characterCount() > 1:
            cursor.insertBlock()

        base_fmt = self._fmt.get(meta.kind, self._fmt["plain"])

        # 记录插入前的 block 数：line 可能含 \n（如 SQL 被美化成多行），
        # insertText 会把它拆成多个文档 block。下面用实际增量补齐 _lines，
        # 保证 _lines 与文档 block 严格一一对应——否则之后所有按 block 索引的
        # 双击跳转 / 异常上下文收集都会从这条多行日志起整体错位。
        blocks_before = self.edit.document().blockCount()

        # 分段插入：把 jump 部分用 link 样式，其他用基础样式
        if meta.jumps:
            last_end = 0
            for j in sorted(meta.jumps, key=lambda x: x.start):
                if j.start > last_end:
                    cursor.insertText(line[last_end:j.start], base_fmt)
                cursor.insertText(line[j.start:j.end], self._fmt_link)
                last_end = j.end
            if last_end < len(line):
                cursor.insertText(line[last_end:], base_fmt)
        else:
            cursor.insertText(line, base_fmt)

        # 块用户数据
        block = cursor.block()
        block.setUserState(_kind_to_state(meta.kind))
        # 与文档 block 一一对应：首块挂真实 meta，多出来的续块挂无 jump 的
        # 副本（续行的 jump 偏移是按整行算的，对拆分后的子行无意义，置空避免误跳）。
        added = self.edit.document().blockCount() - blocks_before
        self._lines.append(meta)
        if added > 0:
            cont_meta = replace(meta, jumps=[]) if meta.jumps else meta
            for _ in range(added):
                self._lines.append(cont_meta)

    def _update_counts(self, cls: lc.Classification | None) -> None:
        # 计数已在 _flush_pending 里累加，这里只更新 UI 标签
        parts = []
        if self._error_count:
            parts.append(f"<span style='color:{COLOR_ERROR};'>✗ {self._error_count} errors</span>")
        if self._warn_count:
            parts.append(f"<span style='color:{COLOR_WARN};'>⚠ {self._warn_count} warnings</span>")
        if not parts:
            parts.append(f"<span style='color:{FG_SECONDARY};'>0 errors</span>")
        self.lbl_counts.setText("  ".join(parts))

    def _show_diagnosis(self, text: str) -> None:
        """在诊断栏显示一行提示。"""
        self.diagnosis_text.setText("💡  " + text)
        self.diagnosis_bar.setVisible(True)

    # ---- 事件：点击跳转、右键、搜索 ----

    def _on_double_click(self, event: QMouseEvent) -> None:
        cursor = self.edit.cursorForPosition(event.position().toPoint())
        block_num = cursor.block().blockNumber()
        if block_num < len(self._lines) and self._lines[block_num].jumps:
            # 取最近的 jump
            col = cursor.columnNumber()
            jumps = self._lines[block_num].jumps
            target = min(jumps, key=lambda j: abs(((j.start + j.end) // 2) - col))
            self.fileJumpRequested.emit(target.path, target.line, target.column)
            return
        # 双击空白处也允许选中默认行为
        QPlainTextEdit.mouseDoubleClickEvent(self.edit, event)

    def _on_context_menu(self, pos) -> None:
        cursor = self.edit.cursorForPosition(pos)
        block_num = cursor.block().blockNumber()
        meta = self._lines[block_num] if block_num < len(self._lines) else None

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
    try:
        import sqlparse  # type: ignore
    except ImportError:
        return line
    m = re.match(r"^(Hibernate|SQL)\s*:\s*(.+)$", line, re.IGNORECASE | re.DOTALL)
    if not m:
        return line
    prefix, sql = m.group(1), m.group(2)
    formatted = sqlparse.format(
        sql, reindent=True, keyword_case="upper", indent_width=2, use_space_around_operators=True
    )
    lines = formatted.splitlines() or [sql]
    head, tail = lines[0], lines[1:]
    return f"{prefix}: {head}" + ("\n    " + "\n    ".join(tail) if tail else "")
