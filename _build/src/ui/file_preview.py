"""内嵌文件编辑/预览面板（tab 内容）

作为 ProjectTab 中心 QTabWidget 里的一个 tab 页内容。
- 默认直接进入编辑模式（无模式切换按钮）
- Pygments 语法高亮、行号、当前行高亮、跳行定位、搜索 Ctrl+F
- 每次输入后 3s 自动保存；Ctrl+S 立即保存
- 二进制 / >2MB / 读取失败：自动切只读并在状态条提示原因
- 没有"预览/渲染"视图；markdown 也按源码编辑（要看渲染去外部编辑器）

对外接口：
- FilePreviewPane(path, line=0, column=0, project_root, config, parent)
- get_path() -> str
- is_dirty() -> bool
- goto_line(line, col)
- flush_save()               外层关闭 tab 前调，立即触发 pending 自动保存
- saved = Signal(str)         保存成功时发出
- dirtyChanged = Signal(bool) dirty 状态变化时发出（外层可用来标 tab 星号）
"""
from __future__ import annotations

import math
import time
from pathlib import Path

from PySide6.QtCore import (
    Qt, QBuffer, QByteArray, QFileSystemWatcher, QIODevice, QRect, QSize, QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction, QColor, QFont, QImageReader, QKeySequence, QMovie, QPainter, QPixmap,
    QTextCursor, QTextDocument, QTextFormat,
)
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QTextEdit, QToolButton, QVBoxLayout, QWidget,
)

from src.core.file_preview_model import (
    BINARY_EXTS, detect_encoding, detect_newline, image_suffix_format, is_movie_path,
    line_comment_prefix, should_wrap_text,
)
from src.ui.syntax_highlighter import PygmentsHighlighter, get_lexer_for
from src.ui.theme import (
    BG_CODE, BG_L2, BG_L4, BORDER_SUBTLE, COLOR_ERROR, COLOR_SUCCESS, COLOR_WARN,
    FG_DIM, FG_SECONDARY, FONT_PT_CODE, RADIUS_SM, apply_search_style,
)
from src.util import app_log
from src.util.editor import REVEAL_LABEL, open_in_editor, reveal_in_explorer

log = app_log.get_logger("preview")

_MAX_READ_BYTES = 2 * 1024 * 1024   # 2MB 以上只读头部
_AUTOSAVE_DELAY_MS = 3000
_EXTRELOAD_DEBOUNCE_MS = 200   # 外部修改 debounce：避免 VSCode "删除-重命名" 写盘瞬间读到空文件
_FONT_PT = FONT_PT_CODE   # 编辑器/日志统一字号（走 theme token，改一处全生效）

def _image_format_for(path: Path) -> str:
    suffix_format = image_suffix_format(path)
    if suffix_format:
        return suffix_format
    try:
        fmt = QImageReader.imageFormat(str(path))
    except RuntimeError:
        return ""
    if not fmt:
        return ""
    return bytes(fmt).decode("ascii", errors="ignore").lower()


def _device_independent_size(size: QSize, dpr: float) -> QSize:
    if dpr <= 1.0:
        return size
    return QSize(
        max(1, math.ceil(size.width() / dpr)),
        max(1, math.ceil(size.height() / dpr)),
    )


def _load_pil_image(path: Path):
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    try:
        with Image.open(path) as img:
            return ImageOps.exif_transpose(img).convert("RGBA")
    except OSError:
        return None


def _scale_with_pillow(source, target_size: QSize) -> QPixmap:
    if source is None or not target_size.isValid() or target_size.isEmpty():
        return QPixmap()
    try:
        from PIL import Image, ImageFilter
        from PIL.ImageQt import ImageQt

        resized = source.resize(
            (target_size.width(), target_size.height()),
            Image.Resampling.LANCZOS,
        )
        resized = resized.filter(ImageFilter.UnsharpMask(radius=0.6, percent=160, threshold=2))
        return QPixmap.fromImage(ImageQt(resized))
    except Exception:
        return QPixmap()


class LineNumberArea(QWidget):
    def __init__(self, editor: "CodeView"):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self.editor.line_number_width(), 0)

    def paintEvent(self, event):
        self.editor._paint_line_numbers(event)


class CodeView(QPlainTextEdit):
    """带行号 + 当前行高亮的等宽编辑器"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        # 当前文件的行注释前缀（# / // / -- 等），由 FilePreviewPane 在加载后设置；
        # None 表示该文件类型不支持行注释（HTML/XML 等需要块注释，暂不实现）
        self.comment_prefix: str | None = None
        f = QFont()
        f.setFamilies(["Cascadia Mono", "Consolas", "Courier New"])
        f.setStyleHint(QFont.StyleHint.Monospace)
        f.setPointSize(_FONT_PT)
        self.setFont(f)
        # 必须：styles.py 里的全局 QSS 给 QPlainTextEdit 设了 font-size: 12px，
        # 会覆盖 setFont 的 pointSize。实例级 setStyleSheet 优先级更高，强制 _FONT_PT 生效。
        self.setStyleSheet(f"QPlainTextEdit {{ font-size: {_FONT_PT}pt; }}")
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)

        self._ln_area = LineNumberArea(self)
        self.blockCountChanged.connect(self._update_ln_area_width)
        self.updateRequest.connect(self._update_ln_area)
        self.cursorPositionChanged.connect(self._highlight_current_line)
        self._update_ln_area_width(0)
        self._highlight_current_line()

    def line_number_width(self) -> int:
        digits = max(3, len(str(max(1, self.blockCount()))))
        return 10 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_ln_area_width(self, _count: int) -> None:
        self.setViewportMargins(self.line_number_width(), 0, 0, 0)

    def _update_ln_area(self, rect, dy: int) -> None:
        if dy:
            self._ln_area.scroll(0, dy)
        else:
            self._ln_area.update(0, rect.y(), self._ln_area.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_ln_area_width(0)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        cr = self.contentsRect()
        self._ln_area.setGeometry(QRect(cr.left(), cr.top(), self.line_number_width(), cr.height()))

    def _paint_line_numbers(self, event) -> None:
        painter = QPainter(self._ln_area)
        painter.fillRect(event.rect(), QColor(str(BG_L2)))

        block = self.firstVisibleBlock()
        number = block.blockNumber()
        top = self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
        bottom = top + self.blockBoundingRect(block).height()

        painter.setPen(QColor(str(FG_DIM)))
        painter.setFont(self.font())
        h = self.fontMetrics().height()

        current_block = self.textCursor().blockNumber()
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                text = str(number + 1)
                painter.setPen(
                    QColor(str(FG_SECONDARY)) if number == current_block
                    else QColor(str(FG_DIM))
                )
                painter.drawText(0, int(top), self._ln_area.width() - 4, h,
                                 Qt.AlignmentFlag.AlignRight, text)
            block = block.next()
            top = bottom
            bottom = top + self.blockBoundingRect(block).height()
            number += 1

    def _highlight_current_line(self) -> None:
        sel = QTextEdit.ExtraSelection()
        sel.format.setBackground(QColor(str(BG_L4)))
        sel.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
        sel.cursor = self.textCursor()
        sel.cursor.clearSelection()
        self.setExtraSelections([sel])
        self._ln_area.update()

    def refresh_theme(self) -> None:
        self._highlight_current_line()
        self.viewport().update()
        self._ln_area.update()

    def goto_line(self, line: int, column: int = 0) -> None:
        if line <= 0:
            return
        block = self.document().findBlockByNumber(line - 1)
        if not block.isValid():
            return
        cur = QTextCursor(block)
        if column > 0:
            cur.movePosition(QTextCursor.MoveOperation.Right,
                             QTextCursor.MoveMode.MoveAnchor, min(column - 1, block.length() - 1))
        self.setTextCursor(cur)
        self.centerCursor()

    def keyPressEvent(self, e):
        # Ctrl+/ 行注释切换。直接在 keyPressEvent 拦截最稳——QAction.setShortcut("Ctrl+/")
        # 在 QPlainTextEdit 焦点下偶发被吞（"加注释成功，再按一次没反应"），改这里彻底解决
        if (e.modifiers() == Qt.KeyboardModifier.ControlModifier
                and e.key() == Qt.Key.Key_Slash
                and not self.isReadOnly()
                and self.comment_prefix):
            self.toggle_line_comment(self.comment_prefix)
            e.accept()
            return
        super().keyPressEvent(e)

    def toggle_line_comment(self, prefix: str) -> None:
        """Ctrl+/ 行注释切换（IntelliJ / 通用 IDE 行为）。

        - 没选区：处理光标所在行
        - 有选区：处理选中区涉及的所有行（选区结尾正好在下一行行首时不算下一行）
        - 模式判定：所有非空行 lstrip 后都以 prefix 起头 → 取消注释；否则全部加注释
        - 加注释：在每行**自己**的非空白起始位置插入 `prefix + ' '`（保留缩进）
        - 取消注释：去掉每行 lstrip 后开头的 prefix（再吃掉紧跟一个空格）
        - 空行始终不动；全程 beginEditBlock，撤销视为单步

        注：不用"选区最浅缩进作统一插入位置"的 VSCode 严格行为——nginx.conf
        / Java 等场景里选区常含缩进 0 的 `}` 行，会把所有 `#` 顶到最左。
        改成每行独立缩进位置，视觉更一致。
        """
        if not prefix:
            return

        cursor = self.textCursor()
        sel_start = cursor.selectionStart()
        sel_end = cursor.selectionEnd()
        doc = self.document()
        start_block = doc.findBlock(sel_start)
        end_block = doc.findBlock(sel_end)
        # 选区扩到下一行行首时（如三击选行），不要把空的下一行也算进来
        if (sel_end > sel_start and end_block.position() == sel_end
                and end_block != start_block):
            end_block = end_block.previous()

        # 收集 [start_block, end_block] 范围内所有 block
        blocks = []
        b = start_block
        while b.isValid():
            blocks.append(b)
            if b.blockNumber() >= end_block.blockNumber():
                break
            b = b.next()
        if not blocks:
            return

        # 判定：所有非空行 lstrip 后是否都以 prefix 起头
        all_commented = True
        has_non_empty = False
        for b in blocks:
            stripped = b.text().lstrip(" \t")
            if not stripped:
                continue
            has_non_empty = True
            if not stripped.startswith(prefix):
                all_commented = False
                break
        if not has_non_empty:
            return  # 全是空行

        cursor.beginEditBlock()
        try:
            # 从后往前改，避免前面插入/删除导致后面 block 的 position 偏移
            for b in reversed(blocks):
                text = b.text()
                if not text.strip():
                    continue   # 空行不处理
                # 每行独立缩进位置：紧贴该行自己的非空白起始字符
                own_indent = len(text) - len(text.lstrip(" \t"))
                block_pos = b.position()
                cur = QTextCursor(doc)
                cur.setPosition(block_pos + own_indent)
                if all_commented:
                    # 删 prefix（从 own_indent 开始）
                    cur.movePosition(QTextCursor.MoveOperation.Right,
                                     QTextCursor.MoveMode.KeepAnchor, len(prefix))
                    if cur.selectedText() == prefix:
                        cur.removeSelectedText()
                        # 紧跟一个空格也吃掉（注释字符后通常会有一个空格）
                        cur.movePosition(QTextCursor.MoveOperation.Right,
                                         QTextCursor.MoveMode.KeepAnchor, 1)
                        if cur.selectedText() == " ":
                            cur.removeSelectedText()
                else:
                    cur.insertText(prefix + " ")
        finally:
            cursor.endEditBlock()


class FilePreviewPane(QWidget):
    """tab 内嵌的文件编辑/预览面板"""

    saved = Signal(str)             # 保存成功，发射 path
    dirtyChanged = Signal(bool)     # dirty 状态变化，外层刷 tab 标题用

    def __init__(self, path: str, line: int = 0, column: int = 0,
                 project_root: str = "", config=None, parent=None):
        super().__init__(parent)
        self.path = path
        self.project_root = project_root
        self.config = config
        self._initial_line = line
        self._initial_col = column
        self._truncated = False
        self._binary = False
        self._not_found = False
        self._dirty = False
        self._read_only = False
        # 读文件时探测出的原始编码与行尾，保存时按原样回写，避免：
        #   1. 把 LF 文件整体改写成 CRLF（污染整文件 git diff）
        #   2. 把 GBK 等非 UTF-8 文件以 UTF-8 写回（内容不可逆损坏）
        self._encoding = "utf-8"
        self._newline = ""  # "" = 保持文本原有的 \n（写入时不转换）
        # 搜索状态去抖：_update_search_status 每次全文 toPlainText+lower+线性扫描，
        # 大文件下挂在 view.textChanged（编辑每字符触发）会卡。停顿 150ms 再算。
        self._search_status_timer = QTimer(self)
        self._search_status_timer.setSingleShot(True)
        self._search_status_timer.setInterval(150)
        self._search_status_timer.timeout.connect(self._update_search_status)
        self._highlighter: PygmentsHighlighter | None = None
        self._image_scroll: QScrollArea | None = None
        self._image_label: QLabel | None = None
        self._image_pixmap: QPixmap | None = None
        self._image_movie: QMovie | None = None
        self._image_movie_data: QByteArray | None = None
        self._image_movie_buffer: QBuffer | None = None
        self._image_pil = None
        self._image_scaled_cache: tuple[tuple[int, int, float], QPixmap] | None = None
        self._image_source_size = QSize()

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        # 顶栏：相对路径 | 状态 | 外部编辑器 | 在资源管理器中显示
        top = QHBoxLayout()
        self.lbl_path = QLabel(self._display_path())
        self.lbl_path.setStyleSheet(f"color:{FG_SECONDARY};")
        self.lbl_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        top.addWidget(self.lbl_path, 1)

        self.lbl_status = QLabel("编辑中")
        self.lbl_status.setStyleSheet(f"color:{FG_SECONDARY}; padding:0 8px;")
        top.addWidget(self.lbl_status)

        btn_external = QPushButton("外部编辑器")
        btn_external.setToolTip("用 VS Code / IDEA / Notepad++ 打开")
        btn_external.clicked.connect(self._open_external)
        top.addWidget(btn_external)

        btn_reveal = QToolButton()
        btn_reveal.setText("📁")
        btn_reveal.setToolTip(REVEAL_LABEL)
        btn_reveal.clicked.connect(lambda: reveal_in_explorer(self.path))
        top.addWidget(btn_reveal)

        root.addLayout(top)

        # 搜索栏
        self.search_widget = QWidget()
        search_row = QHBoxLayout(self.search_widget)
        search_row.setContentsMargins(0, 0, 0, 0)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("搜索（Ctrl+F）")
        apply_search_style(self.search_input)
        self.search_input.returnPressed.connect(self._find_next)
        self.search_input.textChanged.connect(self._schedule_search_status)
        search_row.addWidget(self.search_input, 1)
        self.chk_search_case = QCheckBox("大小写")
        self.chk_search_case.toggled.connect(self._update_search_status)
        search_row.addWidget(self.chk_search_case)
        self.lbl_search_status = QLabel("")
        self.lbl_search_status.setStyleSheet(f"color:{FG_SECONDARY};")
        search_row.addWidget(self.lbl_search_status)
        btn_prev = QToolButton(); btn_prev.setText("↑"); btn_prev.clicked.connect(self._find_prev)
        btn_next = QToolButton(); btn_next.setText("↓"); btn_next.clicked.connect(self._find_next)
        search_row.addWidget(btn_prev); search_row.addWidget(btn_next)
        root.addWidget(self.search_widget)

        # 编辑器
        self.view = CodeView()
        self.view.document().modificationChanged.connect(self._on_modified)
        self.view.textChanged.connect(self._on_text_changed)
        self.view.textChanged.connect(self._schedule_search_status)
        root.addWidget(self.view, 1)

        # 自动保存
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.timeout.connect(self._autosave)

        # 外部修改监听（AI / 外部编辑器改了文件时自动重载）
        # 自保存触发的 fileChanged 用 _ignore_external_until 时间戳过滤
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._on_external_changed)
        self._extreload_timer = QTimer(self)
        self._extreload_timer.setSingleShot(True)
        self._extreload_timer.timeout.connect(self._try_external_reload)
        self._ignore_external_until = 0.0

        # 快捷键：Ctrl+F 搜索、Ctrl+S 保存。
        # Ctrl+W 交给外层 ProjectTab 处理，不在此注册，避免冲突
        self._register_shortcuts()

        self._load()
        if line > 0 and not self._read_only:
            self.view.goto_line(line, column)
        elif line > 0:
            self.view.goto_line(line, column)

    # ---- 对外接口 ----

    def get_path(self) -> str:
        return self.path

    def refresh_theme(self) -> None:
        self.view.refresh_theme()
        if self._highlighter is not None:
            self._highlighter.refresh_theme()

    def is_dirty(self) -> bool:
        return self._dirty

    def goto_line(self, line: int, column: int = 0) -> None:
        self.view.goto_line(line, column)

    def flush_save(self) -> bool:
        """关闭 tab 前调：pending 自动保存立刻落盘。
        返回 True 表示已干净或成功保存；False 表示仍 dirty（保存失败）
        """
        if self._autosave_timer.isActive():
            self._autosave_timer.stop()
            self._autosave()
        return not self._dirty

    def stop_workers(self) -> None:
        if self._highlighter is not None:
            self._highlighter.stop()

    # ---- 加载 ----

    def _display_path(self) -> str:
        if self.project_root:
            try:
                return str(Path(self.path).relative_to(self.project_root))
            except ValueError:
                return self.path
        return self.path

    def _load(self) -> None:
        p = Path(self.path)
        if not p.exists():
            self._not_found = True
            self._set_readonly_reason(f"(文件不存在) {self.path}", f"文件不存在\n\n{self.path}")
            return
        image_format = _image_format_for(p)
        if image_format:
            self._load_image(p, image_format)
            return
        if p.suffix.lower() in BINARY_EXTS:
            self._binary = True
            self._set_readonly_reason(
                f"二进制，不可编辑（{_human_size(p.stat().st_size)})",
                f"(二进制文件，不预览)\n\n{p.name}\n{p.stat().st_size} bytes",
            )
            return

        try:
            size = p.stat().st_size
            with p.open("rb") as f:
                raw = f.read(_MAX_READ_BYTES)
            self._truncated = size > _MAX_READ_BYTES
            text, self._encoding = detect_encoding(raw)
            # 记录原始行尾，保存时按原样回写；编辑器内部统一用 \n
            self._newline = detect_newline(text)
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            self.view.setPlainText(text)
            self.view.document().setModified(False)
            self.lbl_path.setText(f"{self._display_path()}  ({_human_size(size)})")
            if self._truncated:
                self._set_readonly_reason(
                    f"文件过大（>{_MAX_READ_BYTES // (1024*1024)}MB），只读显示前部",
                    None,
                )
        except OSError as e:
            self._set_readonly_reason(f"读取失败：{e}", f"(读取失败)\n\n{e}")
            log.error("预览读取失败: %s | %s", self.path, e)
            return

        # 语法高亮（非二进制/非截断才挂）
        if not self._binary and not self._truncated:
            lexer = get_lexer_for(self.path)
            if lexer:
                self._highlighter = PygmentsHighlighter(self.view.document(), lexer)

        # Ctrl+/ 行注释支持：根据文件名/扩展名挑注释字符（独立于语法高亮）
        # 不支持的文件类型（HTML/XML/CSS 等）prefix=None，view 内部不会触发 toggle
        self.view.comment_prefix = line_comment_prefix(self.path)

        # Markdown / 纯文本按窗口宽度软换行；其他文件保持横向滚动
        if should_wrap_text(p):
            self.view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)

        # 注册外部修改监听（只监听存在、可编辑的常规文本文件）
        if not self._not_found and not self._binary:
            if self.path not in self._watcher.files():
                self._watcher.addPath(self.path)

    def _load_image(self, p: Path, image_format: str) -> None:
        self._binary = True

        size = p.stat().st_size
        reader = QImageReader(str(p))
        reader.setAutoTransform(True)
        source_size = reader.size()
        if source_size.isValid():
            self._image_source_size = source_size

        self.lbl_path.setText(f"{self._display_path()}  ({_human_size(size)})")

        if is_movie_path(p):
            try:
                movie = self._make_movie_from_memory(p, image_format)
            except OSError as e:
                movie = QMovie(self)
                log.warning("动图读取失败: %s | %s", self.path, e)
            if movie.isValid():
                self._read_only = True
                self.search_widget.setVisible(False)
                self.view.setVisible(False)
                self._build_image_area()
                self._image_movie = movie
                self._image_label.setMovie(movie)
                movie.start()
                if self._image_source_size.isEmpty():
                    self._image_source_size = movie.currentImage().size()
                self._set_image_status("动图预览", image_format)
                QTimer.singleShot(0, self._fit_image_to_view)
                return
            self._image_movie_buffer = None
            self._image_movie_data = None

        image = reader.read()
        if image.isNull():
            self._set_readonly_reason(
                f"图片预览失败：{reader.errorString()}",
                f"(图片预览失败)\n\n{p.name}\n{reader.errorString()}",
            )
            log.warning("图片预览失败: %s | %s", self.path, reader.errorString())
            return

        self._read_only = True
        self.search_widget.setVisible(False)
        self.view.setVisible(False)
        self._build_image_area()
        self._image_pixmap = QPixmap.fromImage(image)
        self._image_source_size = self._image_pixmap.size()
        self._image_pil = _load_pil_image(p)
        self._set_image_status("图片预览", image_format)
        QTimer.singleShot(0, self._fit_image_to_view)

    def _make_movie_from_memory(self, p: Path, image_format: str) -> QMovie:
        self._image_movie_data = QByteArray(p.read_bytes())
        self._image_movie_buffer = QBuffer(self)
        self._image_movie_buffer.setData(self._image_movie_data)
        self._image_movie_buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        return QMovie(self._image_movie_buffer, image_format.encode("ascii"), self)

    def _build_image_area(self) -> None:
        self._image_label = QLabel()
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setStyleSheet(f"background:{BG_CODE}; color:{FG_SECONDARY};")

        self._image_scroll = QScrollArea()
        self._image_scroll.setWidgetResizable(False)
        self._image_scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_scroll.setWidget(self._image_label)
        self._image_scroll.setStyleSheet(
            f"QScrollArea {{ background:{BG_CODE}; border:1px solid {BORDER_SUBTLE}; "
            f"border-radius:{RADIUS_SM}px; }}"
        )
        self.layout().addWidget(self._image_scroll, 1)

    def _set_image_status(self, prefix: str, image_format: str) -> None:
        if self._image_source_size.isValid() and not self._image_source_size.isEmpty():
            detail = f"{self._image_source_size.width()}x{self._image_source_size.height()}"
            self.lbl_status.setText(f"{prefix} {detail} {image_format.upper()}")
        else:
            self.lbl_status.setText(f"{prefix} {image_format.upper()}")
        self.lbl_status.setStyleSheet(f"color:{FG_SECONDARY}; padding:0 8px;")

    def _fit_image_to_view(self) -> None:
        if self._image_label is None or self._image_scroll is None:
            return
        if not self._image_source_size.isValid() or self._image_source_size.isEmpty():
            return

        dpr = self._image_device_pixel_ratio()
        viewport = self._image_scroll.viewport().size()
        if not viewport.isValid() or viewport.isEmpty():
            return

        max_physical = QSize(
            max(1, math.floor(viewport.width() * dpr)),
            max(1, math.floor(viewport.height() * dpr)),
        )
        target_physical = self._image_source_size.scaled(
            max_physical, Qt.AspectRatioMode.KeepAspectRatio
        )
        if self._image_pixmap is not None:
            display_pixmap = self._scaled_preview_pixmap(target_physical, dpr)
            display_pixmap.setDevicePixelRatio(dpr)
            self._image_label.setPixmap(display_pixmap)
            self._image_label.setFixedSize(_device_independent_size(display_pixmap.size(), dpr))
            return
        if self._image_movie is not None and self._image_source_size.isValid():
            target_logical = _device_independent_size(target_physical, dpr)
            if self._image_movie.scaledSize() != target_logical:
                self._image_movie.setScaledSize(target_logical)
            self._image_label.setFixedSize(target_logical)

    def _image_device_pixel_ratio(self) -> float:
        handle = self.window().windowHandle() if self.window() else None
        screen = handle.screen() if handle is not None else self.screen()
        if screen is not None:
            return max(1.0, float(screen.devicePixelRatio()))
        return max(1.0, float(self.devicePixelRatioF()))

    def _scaled_preview_pixmap(self, target_physical: QSize, dpr: float) -> QPixmap:
        if self._image_pixmap is None:
            return QPixmap()
        if target_physical == self._image_pixmap.size():
            return QPixmap(self._image_pixmap)

        key = (target_physical.width(), target_physical.height(), dpr)
        if self._image_scaled_cache is not None and self._image_scaled_cache[0] == key:
            return QPixmap(self._image_scaled_cache[1])

        pixmap = _scale_with_pillow(self._image_pil, target_physical)
        if pixmap.isNull():
            pixmap = self._image_pixmap.scaled(
                target_physical,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        self._image_scaled_cache = (key, QPixmap(pixmap))
        return pixmap

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_image_to_view()

    def _set_readonly_reason(self, reason: str, body_text: str | None) -> None:
        self._read_only = True
        self.view.setReadOnly(True)
        if body_text is not None:
            self.view.setPlainText(body_text)
        self.lbl_status.setText(reason)
        self.lbl_status.setStyleSheet(f"color:{COLOR_WARN}; padding:0 8px;")

    # ---- 编辑 / 保存 ----

    def _on_modified(self, dirty: bool) -> None:
        if dirty == self._dirty:
            return
        self._dirty = dirty
        self.dirtyChanged.emit(dirty)

    def _on_text_changed(self) -> None:
        if self._read_only or self._truncated or self._binary:
            return
        self._autosave_timer.start(_AUTOSAVE_DELAY_MS)

    def _write_text_preserving(self, target: Path, text: str) -> None:
        """按文件原始编码与行尾写回。

        编辑器内部行尾统一是 \\n；这里用 open(newline=self._newline) 让 Python 在
        写入时把 \\n 翻译回原行尾（newline="" 表示不翻译，保持 LF）。编码用读取时
        探测到的 self._encoding，避免把非 UTF-8 文件以 UTF-8 覆盖。
        """
        with open(target, "w", encoding=self._encoding,
                  newline=self._newline) as f:
            f.write(text)

    def _autosave(self) -> None:
        if not self._dirty or self._read_only:
            return
        try:
            p = Path(self.path)
            tmp = p.with_suffix(p.suffix + ".minitmp")
            self._write_text_preserving(tmp, self.view.toPlainText())
            # 设忽略窗口：watcher 接下来 1 秒内的 fileChanged 是我们自己写的，不要触发重载
            self._ignore_external_until = time.monotonic() + 1.0
            tmp.replace(p)
            self.view.document().setModified(False)
            self._dirty = False
            self.dirtyChanged.emit(False)
            self.lbl_status.setText(f"已自动保存 {time.strftime('%H:%M:%S')}")
            self.lbl_status.setStyleSheet(f"color:{COLOR_SUCCESS}; padding:0 8px;")
            self.saved.emit(self.path)
            log.debug("自动保存: %s", self.path)
            QTimer.singleShot(1500, self._restore_status_label)
        except OSError as e:
            log.warning("自动保存失败: %s | %s", self.path, e)
            self.lbl_status.setText("自动保存失败")
            self.lbl_status.setStyleSheet(f"color:{COLOR_ERROR}; padding:0 8px;")

    def _restore_status_label(self) -> None:
        if not self._read_only:
            self.lbl_status.setText("编辑中")
            self.lbl_status.setStyleSheet(f"color:{FG_SECONDARY}; padding:0 8px;")

    def _save(self) -> None:
        if self._read_only or not self._dirty:
            return
        p = Path(self.path)
        try:
            tmp = p.with_suffix(p.suffix + ".minitmp")
            self._write_text_preserving(tmp, self.view.toPlainText())
            self._ignore_external_until = time.monotonic() + 1.0
            tmp.replace(p)
            self.view.document().setModified(False)
            self._dirty = False
            self.dirtyChanged.emit(False)
            self.saved.emit(self.path)
            log.info("保存文件: %s", self.path)
        except OSError as e:
            QMessageBox.warning(self, "保存失败", f"{self.path}\n\n{e}")
            log.error("保存失败: %s | %s", self.path, e)

    # ---- 外部修改自动重载（方案 B：dirty 时不重载，仅提示） ----

    def _on_external_changed(self, path: str) -> None:
        """QFileSystemWatcher 回调。debounce 200ms 后再读，避免 VSCode
        '删除-重命名'保存路径瞬间读到空文件。
        """
        if time.monotonic() < self._ignore_external_until:
            return
        self._extreload_timer.start(_EXTRELOAD_DEBOUNCE_MS)

    def _try_external_reload(self) -> None:
        if self._read_only and not self._not_found:
            return  # 只读（二进制 / 过大 / 读取失败）不参与重载
        p = Path(self.path)
        if not p.exists():
            # 某些编辑器是"删除-重命名"，文件可能正处于中间态，再 debounce 一次
            self._extreload_timer.start(_EXTRELOAD_DEBOUNCE_MS)
            return

        # 部分编辑器写盘后 inode 变了，watcher 会丢路径，重新挂上
        if self.path not in self._watcher.files():
            self._watcher.addPath(self.path)

        if self._dirty:
            # 方案 B：本地有未保存改动，不覆盖；状态条提示用户自己处理
            self.lbl_status.setText("⚠ 外部已修改，本地有未保存改动")
            self.lbl_status.setStyleSheet(f"color:{COLOR_WARN}; padding:0 8px;")
            log.info("外部修改但本地 dirty，跳过自动重载: %s", self.path)
            return

        # 干净状态：重读文件，保留滚动位置和光标位置
        try:
            with p.open("rb") as f:
                raw = f.read(_MAX_READ_BYTES)
            text = raw.decode("utf-8", errors="replace")
        except OSError as e:
            log.warning("外部修改后重读失败: %s | %s", self.path, e)
            return

        if text == self.view.toPlainText():
            return  # 内容没变（可能是 mtime 触发但内容相同），不动

        scroll_v = self.view.verticalScrollBar().value()
        scroll_h = self.view.horizontalScrollBar().value()
        cursor_pos = self.view.textCursor().position()

        self.view.setPlainText(text)
        self.view.document().setModified(False)
        self._dirty = False
        self.dirtyChanged.emit(False)

        # 恢复滚动位置；光标位置 clamp 到新文档长度内
        cur = self.view.textCursor()
        cur.setPosition(min(cursor_pos, len(text)))
        self.view.setTextCursor(cur)
        self.view.verticalScrollBar().setValue(scroll_v)
        self.view.horizontalScrollBar().setValue(scroll_h)

        self.lbl_status.setText(f"已自动重载 {time.strftime('%H:%M:%S')}")
        self.lbl_status.setStyleSheet(f"color:{COLOR_SUCCESS}; padding:0 8px;")
        QTimer.singleShot(1500, self._restore_status_label)
        log.debug("外部修改自动重载: %s", self.path)

    def _open_external(self) -> None:
        ok = open_in_editor(self.path, line=self._initial_line, column=self._initial_col,
                            editor_cmd="", project_root=self.project_root)
        if not ok:
            QMessageBox.information(
                self, "未找到外部编辑器",
                "没检测到 VS Code / IDEA / WebStorm / Notepad++ / Sublime。\n"
                "可以在 config.json 的 editor_cmd 里指向你喜欢的编辑器。",
            )

    # ---- 搜索 ----

    def _find_next(self) -> None:
        self._find(True)

    def _find_prev(self) -> None:
        self._find(False)

    def _find(self, forward: bool) -> None:
        q = self.search_input.text()
        if not q:
            self._update_search_status()
            return
        flags = QTextDocument.FindFlag(0)
        if not forward:
            flags |= QTextDocument.FindFlag.FindBackward
        if self.chk_search_case.isChecked():
            flags |= QTextDocument.FindFlag.FindCaseSensitively
        if not self.view.find(q, flags):
            cur = self.view.textCursor()
            cur.movePosition(
                QTextCursor.MoveOperation.Start if forward else QTextCursor.MoveOperation.End
            )
            self.view.setTextCursor(cur)
            self.view.find(q, flags)
        self._update_search_status()

    def _search_positions(self, query: str) -> list[int]:
        if not query:
            return []
        text = self.view.toPlainText()
        haystack = text if self.chk_search_case.isChecked() else text.lower()
        needle = query if self.chk_search_case.isChecked() else query.lower()
        positions: list[int] = []
        start = 0
        while True:
            pos = haystack.find(needle, start)
            if pos < 0:
                break
            positions.append(pos)
            start = pos + max(1, len(needle))
        return positions

    def _schedule_search_status(self) -> None:
        # 去抖入口：搜索框输入 / 正文编辑都先重置定时器，停顿后才真正算匹配数
        self._search_status_timer.start()

    def _update_search_status(self) -> None:
        q = self.search_input.text()
        if not q:
            self.lbl_search_status.setText("")
            return
        positions = self._search_positions(q)
        if not positions:
            self.lbl_search_status.setText("无匹配")
            return
        sel_start = self.view.textCursor().selectionStart()
        current = 1
        for i, pos in enumerate(positions, 1):
            if pos >= sel_start:
                current = i
                break
        else:
            current = len(positions)
        if sel_start in positions:
            current = positions.index(sel_start) + 1
        self.lbl_search_status.setText(f"{current}/{len(positions)}")

    # ---- 快捷键 ----

    def _register_shortcuts(self) -> None:
        act_find = QAction(self)
        act_find.setShortcut(QKeySequence("Ctrl+F"))
        act_find.triggered.connect(lambda: self.search_input.setFocus())
        act_find.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.addAction(act_find)

        act_save = QAction(self)
        act_save.setShortcut(QKeySequence("Ctrl+S"))
        act_save.triggered.connect(self._save)
        act_save.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.addAction(act_save)

        # Ctrl+/ 不在这里注册——CodeView.keyPressEvent 里直接拦截，最稳
        # （QAction 在 QPlainTextEdit 焦点下偶发吞键，反复 toggle 会失灵）


def _human_size(n: int) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"
