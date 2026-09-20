"""Git 改动查看器（只读）

只做一件事：实时展示当前分支有哪些文件被改动。
提交、拉取、合并等 Git 操作交给外部 AI / 终端处理。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QTextCharFormat, QSyntaxHighlighter
from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMenu,
    QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from src.core import git_context, git_ops
from src.ui.theme import (
    FG_SECONDARY, FONT_PT_DIFF, GIT_ADD, GIT_DEL, GIT_FILE_HEAD, GIT_HUNK, GIT_META,
    GIT_MODIFY,
)
from src.util.editor import REVEAL_LABEL, reveal_in_explorer

perf_log = logging.getLogger("mini-ide.performance")


def _overview_signature(
    root: str, branch: str, files: list[git_ops.ChangedFile],
) -> str:
    parts = [branch]
    for changed in files:
        abs_path = Path(root) / changed.path
        try:
            st = abs_path.stat()
            stamp = f"{st.st_mtime_ns}:{st.st_size}"
        except OSError:
            stamp = "-"
        parts.append(f"{changed.status}:{changed.path}:{stamp}")
    return "\n".join(parts)


class _OverviewWorker(QThread):
    done = Signal(str, str, list, dict, str)  # root, branch, files, numstat, signature

    def __init__(self, root: str, parent=None):
        super().__init__(parent)
        self.root = root

    def run(self) -> None:
        started = time.perf_counter()
        root = git_ops.repo_root(self.root)
        branch = git_ops.current_branch(root)
        files = git_ops.list_changed_files(root)
        stats = git_ops.diff_numstat(root)
        signature = _overview_signature(root, branch, files)
        perf_log.info(
            "perf op=git-overview-worker duration_ms=%.1f files=%d status=done",
            (time.perf_counter() - started) * 1000, len(files),
        )
        self.done.emit(root, branch, files, stats, signature)


class _FileDiffWorker(QThread):
    done = Signal(str, str)  # path, diff text

    def __init__(self, root: str, changed_file: git_ops.ChangedFile, parent=None):
        super().__init__(parent)
        self.root = root
        self.changed_file = changed_file

    def run(self) -> None:
        started = time.perf_counter()
        f = self.changed_file
        text = git_context.file_diff_text(self.root, f)
        perf_log.info(
            "perf op=git-diff-worker duration_ms=%.1f files=1 status=done",
            (time.perf_counter() - started) * 1000,
        )
        self.done.emit(f.path, text)


class DiffHighlighter(QSyntaxHighlighter):
    """对 diff 输出上色：+绿、-红、@蓝、index 灰"""

    def __init__(self, document):
        super().__init__(document)
        self._fmt_add = self._mk(GIT_ADD)
        self._fmt_del = self._mk(GIT_DEL)
        self._fmt_hunk = self._mk(GIT_HUNK, bold=True)
        self._fmt_meta = self._mk(GIT_META, italic=True)
        self._fmt_file = self._mk(GIT_FILE_HEAD, bold=True)

    @staticmethod
    def _mk(color: str, bold: bool = False, italic: bool = False) -> QTextCharFormat:
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(str(color)))
        if bold:
            fmt.setFontWeight(QFont.Weight.DemiBold)
        if italic:
            fmt.setFontItalic(True)
        return fmt

    def highlightBlock(self, text: str) -> None:
        if not text:
            return
        if text.startswith("diff --git") or text.startswith("+++ ") or text.startswith("--- "):
            self.setFormat(0, len(text), self._fmt_file)
        elif text.startswith("@@"):
            self.setFormat(0, len(text), self._fmt_hunk)
        elif text.startswith("+") and not text.startswith("+++"):
            self.setFormat(0, len(text), self._fmt_add)
        elif text.startswith("-") and not text.startswith("---"):
            self.setFormat(0, len(text), self._fmt_del)
        elif text.startswith("index ") or text.startswith("new file") or text.startswith("deleted file"):
            self.setFormat(0, len(text), self._fmt_meta)


class GitViewer(QDialog):

    # 用户选择「跳转到文件」：参数是绝对路径。ProjectTab 连接这里打开内嵌 tab
    openFileRequested = Signal(str)

    def __init__(self, project_root: str, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.root = project_root
        self.setWindowTitle(f"Git 改动 — {self.root}")
        self.resize(1120, 720)
        self._overview_worker: _OverviewWorker | None = None
        self._diff_worker: _FileDiffWorker | None = None
        self._numstat: dict[str, tuple[int, int]] = {}
        self._last_signature = ""
        self._selected_path = ""
        self._current_branch = ""
        self._current_files: list[git_ops.ChangedFile] = []
        self._closing = False
        self._active_workers: list[QThread] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        header = QHBoxLayout()
        self.lbl_branch = QLabel("")
        self.lbl_branch.setTextFormat(Qt.TextFormat.RichText)
        header.addWidget(self.lbl_branch, 1)
        btn_refresh = QPushButton("刷新")
        btn_refresh.clicked.connect(lambda: self._reload(force=True))
        header.addWidget(btn_refresh)
        root.addLayout(header)

        self.lbl_summary = QLabel("")
        self.lbl_summary.setStyleSheet(f"color:{FG_SECONDARY};")
        root.addWidget(self.lbl_summary)

        root.addWidget(self._build_changes_view(), 1)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(1500)
        self._refresh_timer.timeout.connect(self._reload)
        self._refresh_timer.start()

        self._reload(force=True)

    # ---- 改动视图 ----

    def _build_changes_view(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        split = QSplitter(Qt.Orientation.Horizontal)

        self.changes_list = QListWidget()
        self.changes_list.setMinimumWidth(260)
        self.changes_list.currentItemChanged.connect(self._on_change_selected)
        self.changes_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.changes_list.customContextMenuRequested.connect(self._on_changes_context_menu)
        split.addWidget(self.changes_list)

        self.diff_view = QPlainTextEdit()
        self.diff_view.setReadOnly(True)
        self.diff_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        f = QFont()
        f.setFamilies(["Cascadia Mono", "Consolas"])
        f.setPointSize(FONT_PT_DIFF)
        self.diff_view.setFont(f)
        self.diff_highlighter = DiffHighlighter(self.diff_view.document())
        split.addWidget(self.diff_view)

        split.setSizes([280, 900])
        lay.addWidget(split)
        return w

    def refresh_theme(self) -> None:
        self.lbl_summary.setStyleSheet(f"color:{FG_SECONDARY};")
        self._update_header(self._current_branch, self._current_files)
        for index in range(self.changes_list.count()):
            item = self.changes_list.item(index)
            changed = item.data(Qt.ItemDataRole.UserRole)
            if changed is not None:
                item.setForeground(QColor(str(self._status_color(changed))))
        self.diff_highlighter = DiffHighlighter(self.diff_view.document())
        self.diff_highlighter.rehighlight()

    def _status_label(self, f: git_ops.ChangedFile) -> str:
        return git_context.status_label(f)

    def _status_color(self, f: git_ops.ChangedFile) -> str:
        label = self._status_label(f)
        if label in ("新增", "未跟踪"):
            return GIT_ADD
        if label == "删除":
            return GIT_DEL
        if label == "冲突":
            return GIT_DEL
        return GIT_MODIFY

    def _sort_files(self, files: list[git_ops.ChangedFile]) -> list[git_ops.ChangedFile]:
        return git_context.sort_changed_files(files)

    def _update_header(self, branch: str, files: list[git_ops.ChangedFile]) -> None:
        self._current_branch = branch
        self._current_files = list(files)
        self.lbl_branch.setText(
            f"🌿 {branch or '(无分支)'} "
            f"<span style='color:{FG_SECONDARY};'>@ {self.root}</span>"
        )
        counts: dict[str, int] = {}
        for f in self._sort_files(files):
            label = self._status_label(f)
            counts[label] = counts.get(label, 0) + 1
        if not files:
            text = f"工作区干净 · {time.strftime('%H:%M:%S')}"
        else:
            order = ["冲突", "修改", "暂存+修改", "已暂存", "新增", "未跟踪", "删除"]
            parts = [f"{len(files)} 个改动"]
            parts.extend(f"{k} {counts[k]}" for k in order if counts.get(k))
            text = " · ".join(parts) + f" · {time.strftime('%H:%M:%S')}"
        self.lbl_summary.setText(text)

    def _load_changes(self, files: list[git_ops.ChangedFile]) -> None:
        current_path = self._selected_path
        self.changes_list.setUpdatesEnabled(False)
        self.changes_list.clear()
        if not files:
            self.changes_list.setUpdatesEnabled(True)
            self.diff_view.setPlainText("(工作区很干净，无改动)")
            self._selected_path = ""
            return
        target_row = 0
        for f in files:
            label = self._status_label(f)
            stat = self._numstat.get(f.path)
            if stat:
                stat_text = f"  +{stat[0]} -{stat[1]}"
            else:
                stat_text = ""
            item = QListWidgetItem(f"[{label}]  {f.path}{stat_text}")
            item.setData(Qt.ItemDataRole.UserRole, f)
            item.setToolTip(f"{f.status}  {f.path}")
            item.setForeground(QColor(str(self._status_color(f))))
            self.changes_list.addItem(item)
            if f.path == current_path:
                target_row = self.changes_list.count() - 1
        self.changes_list.setUpdatesEnabled(True)
        self.changes_list.setCurrentRow(target_row)

    def _on_change_selected(self, current, _prev) -> None:
        if self._closing:
            return
        if not current:
            self.diff_view.setPlainText("")
            return
        f: git_ops.ChangedFile = current.data(Qt.ItemDataRole.UserRole)
        self._selected_path = f.path
        self.diff_view.setPlainText("正在加载 diff...")
        worker = _FileDiffWorker(self.root, f, parent=QApplication.instance())
        self._diff_worker = worker
        worker.done.connect(self._on_file_diff_loaded)
        self._track_worker(worker)
        worker.start()

    def _on_file_diff_loaded(self, path: str, text: str) -> None:
        if self._closing:
            return
        if self.sender() is not self._diff_worker:
            return
        current = self.changes_list.currentItem()
        if current:
            f: git_ops.ChangedFile = current.data(Qt.ItemDataRole.UserRole)
            if f.path == path:
                self.diff_view.setPlainText(text)
        self._diff_worker = None

    def _on_changes_context_menu(self, pos) -> None:
        item = self.changes_list.itemAt(pos)
        if item is None:
            return
        f: git_ops.ChangedFile = item.data(Qt.ItemDataRole.UserRole)
        abs_path = Path(self.root) / f.path
        file_exists = abs_path.exists() and abs_path.is_file()

        menu = QMenu(self.changes_list)
        act_jump = menu.addAction("打开文件", lambda: self._jump_to_file(f))
        if not file_exists:
            act_jump.setEnabled(False)
            act_jump.setText("打开文件（已删除）")
        act_reveal = menu.addAction(REVEAL_LABEL, lambda: reveal_in_explorer(str(abs_path)))
        act_reveal.setEnabled(abs_path.exists())
        menu.addSeparator()
        menu.addAction("复制相对路径", lambda: QApplication.clipboard().setText(f.path))
        menu.addAction("复制绝对路径", lambda: QApplication.clipboard().setText(str(abs_path)))
        menu.addAction("复制当前 diff", lambda: QApplication.clipboard().setText(self.diff_view.toPlainText()))
        menu.exec(self.changes_list.viewport().mapToGlobal(pos))

    def _jump_to_file(self, f: git_ops.ChangedFile) -> None:
        abs_path = Path(self.root) / f.path
        if not abs_path.is_file():
            QMessageBox.information(
                self, "跳转失败",
                f"文件不存在或不可打开：\n\n{abs_path}",
            )
            return
        self.openFileRequested.emit(str(abs_path))

    def _reload(self, force: bool = False) -> None:
        if self._closing:
            return
        if self._overview_worker and self._overview_worker.isRunning():
            return
        if force and not self._last_signature:
            self.diff_view.setPlainText("正在加载本地改动...")
        worker = _OverviewWorker(self.root, parent=QApplication.instance())
        self._overview_worker = worker
        worker.done.connect(self._on_overview_loaded)
        self._track_worker(worker)
        worker.start()

    def _on_overview_loaded(
        self, root: str, branch: str, files: list[git_ops.ChangedFile],
        stats: dict, signature: str,
    ) -> None:
        if self._closing:
            return
        if self.sender() is not self._overview_worker:
            return
        started = time.perf_counter()
        self._overview_worker = None
        self.root = root
        self.setWindowTitle(f"Git 改动 — {self.root}")
        self._numstat = stats
        self._update_header(branch, files)
        if signature != self._last_signature:
            self._last_signature = signature
            self._load_changes(files)
        duration_ms = (time.perf_counter() - started) * 1000
        if duration_ms >= 20:
            perf_log.info(
                "perf op=git-overview-render duration_ms=%.1f files=%d status=done",
                duration_ms, len(files),
            )

    def _track_worker(self, worker: QThread) -> None:
        self._active_workers.append(worker)

        def cleanup(w=worker) -> None:
            try:
                self._active_workers.remove(w)
            except ValueError:
                pass

        worker.finished.connect(cleanup)
        worker.finished.connect(worker.deleteLater)

    def closeEvent(self, event) -> None:
        self._closing = True
        self._refresh_timer.stop()
        for worker in list(self._active_workers):
            if worker.isRunning():
                worker.requestInterruption()
        super().closeEvent(event)
