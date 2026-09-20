"""快速导航工具

- Ctrl+Shift+N 文件名搜索（FilePicker）
- Ctrl+E      最近打开的文件

都基于同一个 PickerDialog 基类：输入框 + 候选列表 + 上下箭头选择 + Enter 执行。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import Qt, QEvent, QSize, QTimer, Signal
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QIcon, QKeyEvent, QPalette,
)
from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
    QVBoxLayout, QWidget,
)

from src.core.file_index import FileSearchWorker
from src.core.search_worker_lifecycle import register_search_worker
from src.ui.theme import (
    ACCENT, ACCENT_SUBTLE, BG_CODE, BG_L1, BG_L2, BG_L4,
    BORDER_STRONG, BORDER_SUBTLE, FG_BRIGHT, FG_DIM, FG_PRIMARY, FG_SECONDARY,
    FONT_PT_UI, FONT_PT_UI_LG, FONT_PT_UI_SM, RADIUS_SM, TREE_ITEM_PAD_V,
    apply_search_style,
)


class _PickerItemDelegate(QStyledItemDelegate):
    """单行绘制标题和路径，长文本省略显示，不允许换行挤压相邻项。"""

    _LINE_GAP = TREE_ITEM_PAD_V

    @staticmethod
    def _item(index):
        it = index.data(Qt.ItemDataRole.UserRole)
        return it if isinstance(it, PickerItem) else None

    @staticmethod
    def _fonts(base_font: QFont) -> tuple[QFont, QFont]:
        title_font = QFont(base_font)
        title_font.setPointSizeF(FONT_PT_UI)
        title_font.setWeight(QFont.Weight.DemiBold)
        subtitle_font = QFont(base_font)
        subtitle_font.setPointSizeF(FONT_PT_UI_SM)
        return title_font, subtitle_font

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        item = self._item(index)
        if item is None:
            super().paint(painter, option, index)
            return

        opt.text = ""
        widget = opt.widget
        style = widget.style() if widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)
        text_rect = style.subElementRect(
            QStyle.SubElement.SE_ItemViewItemText, opt, widget)

        title_font, subtitle_font = self._fonts(opt.font)
        title_metrics = QFontMetrics(title_font)
        subtitle_metrics = QFontMetrics(subtitle_font)
        content_height = title_metrics.height()
        if item.subtitle:
            content_height += self._LINE_GAP + subtitle_metrics.height()
        top = text_rect.top() + max(0, (text_rect.height() - content_height) // 2)
        width = max(0, text_rect.width())

        painter.save()
        painter.setClipRect(text_rect)
        painter.setFont(title_font)
        painter.setPen(QColor(FG_BRIGHT if selected else FG_PRIMARY))
        painter.drawText(
            text_rect.left(), top + title_metrics.ascent(),
            title_metrics.elidedText(item.title, Qt.TextElideMode.ElideRight, width),
        )
        if item.subtitle:
            subtitle_top = top + title_metrics.height() + self._LINE_GAP
            painter.setFont(subtitle_font)
            painter.setPen(QColor(FG_SECONDARY if selected else FG_DIM))
            painter.drawText(
                text_rect.left(), subtitle_top + subtitle_metrics.ascent(),
                subtitle_metrics.elidedText(
                    item.subtitle, Qt.TextElideMode.ElideMiddle, width,
                ),
            )
        painter.restore()

    def sizeHint(self, option, index):
        if self._item(index) is None:
            return super().sizeHint(option, index)
        title_font, subtitle_font = self._fonts(option.font)
        item = self._item(index)
        height = QFontMetrics(title_font).height() + TREE_ITEM_PAD_V * 2
        if item.subtitle:
            height += self._LINE_GAP + QFontMetrics(subtitle_font).height()
        return QSize(0, height)


@dataclass
class PickerItem:
    title: str                      # 显示的主文本（带路径）
    subtitle: str = ""              # 灰色辅助文本（右侧/下方）
    data: object = None             # 回调用的数据


class PickerDialog(QDialog):
    """通用快速选择浮窗"""

    picked = Signal(object)    # PickerItem.data

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(680, 480)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QWidget()
        header.setStyleSheet(
            f"background:{BG_L2}; border:1px solid {BORDER_STRONG}; border-bottom:none;"
        )
        hl = QVBoxLayout(header)
        hl.setContentsMargins(14, 12, 14, 6)
        self.title_lbl = QLabel(title)
        self.title_lbl.setStyleSheet(f"color:{FG_SECONDARY}; font-size:{FONT_PT_UI_SM}pt;")
        hl.addWidget(self.title_lbl)
        self.input = QLineEdit()
        # 统一搜索框样式（白字 + 等宽 + 加大 + 提亮 placeholder）
        apply_search_style(self.input)
        self.input.textChanged.connect(self._on_query_changed)
        self.input.installEventFilter(self)
        hl.addWidget(self.input)
        root.addWidget(header)

        self.list = QListWidget()
        self.list.setStyleSheet(
            f"QListWidget {{ background:{BG_L1}; color:{FG_PRIMARY};"
            f" border:1px solid {BORDER_STRONG}; border-top:none; outline:none; }}"
            f"QListWidget::item {{ padding:0 12px; border:none; }}"
            f"QListWidget::item:hover {{ background:{BG_L4}; }}"
            f"QListWidget::item:selected {{ background:{ACCENT_SUBTLE};"
            f" color:{FG_BRIGHT}; }}"
        )
        self.list.itemActivated.connect(self._on_activated)
        self.list.installEventFilter(self)
        # 标题和路径各占一行，长文本省略，避免换行挤压相邻项。
        self.list.setItemDelegate(_PickerItemDelegate(self.list))
        root.addWidget(self.list, 1)

        self._fetcher: Callable[[str], list[PickerItem]] | None = None
        self._all_items: list[PickerItem] = []
        self._async_indexer = None
        self._search_generation = 0
        self._search_workers: list[FileSearchWorker] = []

        # 输入去抖：大仓库下 fetcher 是全表 O(n) 模糊打分，每个按键都跑会卡顿。
        # textChanged 只重置定时器，停顿 120ms 后才真正刷新候选列表。
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(120)
        self._debounce.timeout.connect(self._do_refresh)
        self._pending_query = ""

    def set_fetcher(self, fetcher: Callable[[str], list[PickerItem]]) -> None:
        """fetcher(query) -> items；query 为空时返回默认列表"""
        self._async_indexer = None
        self._fetcher = fetcher
        self._refresh("")

    def set_file_indexer(self, indexer) -> None:
        """文件名搜索使用后台 worker，空查询只展示已有索引切片。"""
        self._fetcher = None
        self._async_indexer = indexer
        self._refresh("")

    def set_static_items(self, items: list[PickerItem]) -> None:
        self._all_items = items
        self.set_fetcher(self._default_fetch)

    def _default_fetch(self, query: str) -> list[PickerItem]:
        q = query.strip().lower()
        if not q:
            return self._all_items
        return [it for it in self._all_items if q in it.title.lower() or q in it.subtitle.lower()]

    def _on_query_changed(self, text: str) -> None:
        # 走去抖：仅记录最新 query 并重启定时器，停顿后由 _do_refresh 真正刷新
        self._pending_query = text
        if self._async_indexer is not None:
            self._cancel_search_workers()
        self._debounce.start()

    def _do_refresh(self) -> None:
        self._refresh(self._pending_query)

    def _refresh(self, query: str) -> None:
        if self._async_indexer is not None:
            if query.strip():
                self._start_async_refresh(query)
            else:
                self._cancel_search_workers()
                self._set_items([
                    PickerItem(
                        title=item.name_original,
                        subtitle=item.rel_path,
                        data=item.abs_path,
                    )
                    for item in self._async_indexer.files()[:150]
                ])
            return
        items = self._fetcher(query) if self._fetcher else []
        self._set_items(items)

    def _set_items(self, items: list[PickerItem]) -> None:
        self.list.setUpdatesEnabled(False)
        self.list.clear()
        try:
            for it in items[:200]:
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, it)
                self.list.addItem(item)
            if self.list.count() > 0:
                self.list.setCurrentRow(0)
        finally:
            self.list.setUpdatesEnabled(True)

    def _start_async_refresh(self, query: str) -> None:
        self._cancel_search_workers()
        self._search_generation += 1
        generation = self._search_generation
        worker = FileSearchWorker(
            self._async_indexer.files(), query, 150, generation,
            parent=QApplication.instance(),
        )
        self._search_workers.append(worker)
        register_search_worker(worker, worker.requestInterruption)
        worker.result.connect(self._on_async_result)
        worker.finished.connect(self._on_search_worker_finished)
        worker.finished.connect(worker.deleteLater)
        self.list.clear()
        waiting = QListWidgetItem("搜索中...")
        waiting.setFlags(Qt.ItemFlag.NoItemFlags)
        self.list.addItem(waiting)
        worker.start()

    def _cancel_search_workers(self) -> None:
        self._search_generation += 1
        for worker in self._search_workers:
            if worker.isRunning():
                worker.requestInterruption()

    def _on_async_result(self, generation: int, hits: list, _duration_ms: float) -> None:
        if generation != self._search_generation:
            return
        self._set_items([
            PickerItem(
                title=item.name_original,
                subtitle=item.rel_path,
                data=item.abs_path,
            )
            for item in hits
        ])

    def _on_search_worker_finished(self) -> None:
        worker = self.sender()
        if worker in self._search_workers:
            self._search_workers.remove(worker)

    def _on_activated(self, item: QListWidgetItem) -> None:
        picker = item.data(Qt.ItemDataRole.UserRole)
        if picker is not None:
            self.picked.emit(picker.data)
        self.accept()

    def keyPressEvent(self, e: QKeyEvent) -> None:
        if e.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(e)

    def showEvent(self, e):
        # 无边框 dialog 在 Windows 上 show() 后默认拿不到键盘焦点，必须显式激活
        super().showEvent(e)
        self.activateWindow()
        self.raise_()
        self.input.setFocus()

    def changeEvent(self, e):
        # 失焦自动关闭（点击外部 / 切到其他窗口）
        if e.type() == QEvent.Type.ActivationChange and not self.isActiveWindow():
            self.reject()
            return
        super().changeEvent(e)

    def closeEvent(self, e):
        self._debounce.stop()
        self._cancel_search_workers()
        super().closeEvent(e)

    def eventFilter(self, obj, e):
        # 方向键 / Enter / ESC 在 input 或 list 上都能用
        if e.type() == e.Type.KeyPress and isinstance(e, QKeyEvent):
            k = e.key()
            if k == Qt.Key.Key_Escape:
                self.reject()
                return True
            if obj is self.input:
                if k == Qt.Key.Key_Down:
                    row = min(self.list.currentRow() + 1, self.list.count() - 1)
                    self.list.setCurrentRow(row)
                    return True
                if k == Qt.Key.Key_Up:
                    row = max(self.list.currentRow() - 1, 0)
                    self.list.setCurrentRow(row)
                    return True
                if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    item = self.list.currentItem()
                    if item:
                        self._on_activated(item)
                    return True
        return super().eventFilter(obj, e)


# ---- 具体实现 ----

def show_file_picker(indexer, on_pick: Callable[[str], None], parent=None) -> PickerDialog:
    """Ctrl+Shift+N: 按文件名搜索"""
    dlg = PickerDialog("按文件名搜索   (Ctrl+Shift+N)", parent)
    dlg.input.setPlaceholderText(f"已索引 {indexer.count()} 个文件...")

    dlg.set_file_indexer(indexer)
    dlg.picked.connect(on_pick)
    return dlg


def show_recent_files(recent_abs_paths: list[str], project_root: str,
                      on_pick: Callable[[str], None], parent=None) -> PickerDialog:
    """Ctrl+E: 最近打开的文件"""
    dlg = PickerDialog("最近打开   (Ctrl+E)", parent)
    from pathlib import Path
    items: list[PickerItem] = []
    for p in recent_abs_paths:
        pp = Path(p)
        try:
            rel = str(pp.relative_to(project_root)).replace("\\", "/")
        except Exception:
            rel = p
        items.append(PickerItem(title=pp.name, subtitle=rel, data=p))
    dlg.set_static_items(items)
    dlg.picked.connect(on_pick)
    return dlg
