"""左侧文件树

只读浏览项目目录。懒加载子目录（避免大项目卡顿）。
过滤框用 FileIndexer 全局搜索，不需要展开目录。
单击目录行即可展开，不用非得点小箭头。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from PySide6.QtCore import Qt, QFile, QMimeData, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QFrame, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox, QStackedWidget,
    QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from src.core.git_ops import (
    GIT_STATUS_ADDED, GIT_STATUS_CONFLICT, GIT_STATUS_DELETED,
    GIT_STATUS_MODIFIED, GIT_STATUS_UNTRACKED,
)
from src.core.file_actions import paste_paths
from src.core.tool_launchers import (
    CREATE_NO_WINDOW, open_in_cc_args, open_in_codex_args,
)
from src.ui.theme import (
    ACCENT_SUBTLE, BG_L2, BG_L4, FG_BRIGHT, FG_PRIMARY, GIT_ADD, GIT_CONFLICT,
    GIT_DEL, GIT_IGNORED, GIT_MODIFY, apply_search_style,
)
from src.util.editor import REVEAL_LABEL, reveal_in_explorer


# 永远隐藏：只藏操作系统垃圾文件。构建产物/依赖目录（build/dist/node_modules/.git 等）
# 都照常显示，跟资源管理器保持一致——目录树是懒加载的，显示成本几乎为零；
# 搜索性能由 FileIndexer 自己的 ignored_dirs 另行保护。
_ALWAYS_HIDDEN = {
    ".DS_Store", "Thumbs.db", "desktop.ini",
}


# QTreeView 的箭头/行样式：更大的分支指示器 + 稍高的行，点起来不费眼
# 注意：不要在 QTreeWidget::item 里写 color——QSS 的 ::item color 会覆盖
# QTreeWidgetItem.setForeground()，导致 git 染色无效。默认色由外层
# QTreeWidget { color } 兜底（theme.py 全局已设）。
def _tree_style() -> str:
    return f"""
QTreeWidget {{
    background:{BG_L2};
    border:none;
    outline:none;
}}
QTreeWidget::item {{
    height: 22px;
    padding: 1px 2px;
}}
QTreeWidget::item:hover {{ background:{BG_L4}; }}
QTreeWidget::item:selected {{ background:{ACCENT_SUBTLE}; color:{FG_BRIGHT}; }}
QTreeWidget::branch {{
    background:{BG_L2};
}}
QTreeWidget::branch:has-children:!has-siblings:closed,
QTreeWidget::branch:closed:has-children:has-siblings {{
    image: none;
    border-image: none;
    background: url(x);
}}
"""


# 右键菜单"▶ 运行"显示的可执行脚本类型
_RUNNABLE_SCRIPT_EXTS = {".bat", ".cmd", ".ps1", ".exe"}
_CUT_MIME = "application/x-mini-ide-cut"

# 节点级 git 染色缓存：(color_hex, strikethrough, tooltip)，
# 染色函数对比缓存值，相同则不调 setForeground/setFont/setToolTip，避免 viewport 误重绘
_RENDER_CACHE_ROLE = Qt.ItemDataRole.UserRole + 2


class _PasteWorker(QThread):
    done = Signal(str, int, list, bool)  # target_dir, changed_count, failed messages, move

    def __init__(self, sources: list[str], target_dir: str, move: bool = False, parent=None):
        super().__init__(parent)
        self._sources = sources
        self._target_dir = target_dir
        self._move = move

    def run(self) -> None:
        target_dir = Path(self._target_dir)
        changed, failed = paste_paths([Path(raw) for raw in self._sources], target_dir, self._move)
        self.done.emit(str(target_dir), changed, failed, self._move)


class _MultiSelectTree(QTreeWidget):
    """ExtendedSelection 下右键点击时保存 selection 快照。

    Qt 默认 mousePressEvent 会改写 selection（右键点未选中项 → 替换为单项；
    点已多选项理论上保留，但 PySide6 在 Windows 上观察到也被替换）。这里在 super
    调用之前先抓快照，customContextMenuRequested 触发时优先用快照而不是
    selectedItems() 的实时值，保证菜单操作的是用户右键瞬间的真实选区。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._right_press_snapshot: list = []

    def mousePressEvent(self, event):
        # 右键 press：在 super 改 selection 之前先抓快照
        # （super 还是要调，否则 contextMenuEvent / customContextMenuRequested 不会触发）
        if event.button() == Qt.MouseButton.RightButton:
            idx = self.indexAt(event.pos())
            item = self.itemFromIndex(idx) if idx.isValid() else None
            current = self.selectedItems()
            if item is not None and len(current) > 1:
                clicked_path = item.data(0, Qt.ItemDataRole.UserRole)
                selected_paths = {it.data(0, Qt.ItemDataRole.UserRole) for it in current}
                if clicked_path in selected_paths:
                    self._right_press_snapshot = list(current)
                else:
                    self._right_press_snapshot = []
            else:
                self._right_press_snapshot = []
        super().mousePressEvent(event)

    def take_right_press_snapshot(self) -> list:
        snap = self._right_press_snapshot
        self._right_press_snapshot = []
        return snap


class FileTree(QWidget):

    fileActivated = Signal(str)          # 双击文件时（带绝对路径）
    openInEditorRequested = Signal(str)  # 右键→用编辑器打开
    fileCreated = Signal(str)            # 右键→新建文件，参数=新文件绝对路径
    scriptRunRequested = Signal(str)     # 右键→运行脚本（.bat/.cmd/.ps1/.exe），参数=脚本绝对路径

    def __init__(self, root_path: str, indexer=None, project_type: str = "", parent=None):
        """indexer 是 FileIndexer 实例；有它时，过滤框使用全局索引搜索。
        project_type 保留给旧调用方兼容，目录树菜单不再按项目类型打开外部 IDE。
        """
        super().__init__(parent)
        self.root_path = Path(root_path)
        self.indexer = indexer
        self.project_type = project_type
        self._paste_workers: list[_PasteWorker] = []
        # git 状态：{rel_posix_path: status_kind}，status_kind 为 GIT_STATUS_* 之一
        self._git_status: dict[str, str] = {}
        self._git_ignored: set[str] = set()
        # ignored 前缀缓存：精确匹配走 set；前缀匹配按 rel 缓存"是否被某 ignored 目录覆盖"
        self._ignored_prefix_cache: dict[str, bool] = {}
        # 已删除文件按父目录分组：{parent_rel_posix: [filename, ...]}，"" 表示项目根
        self._git_deleted_by_parent: dict[str, list[str]] = {}
        # 预计算的「目录相对路径 → 冒泡状态」表，避免染色时反复扫 status dict
        self._git_dir_bubble: dict[str, str] = {}
        self.setMinimumWidth(240)
        self._build()
        self._reload()

    def focus_filter(self) -> None:
        """供外部（如 Ctrl+Shift+N）调用：聚焦过滤框并全选"""
        self.filter_input.setFocus()
        self.filter_input.selectAll()

    def update_git_status(
        self,
        statuses: dict[str, str],
        ignored: set[str],
        deleted_by_parent: dict[str, list[str]],
    ) -> None:
        """更新 git 状态并增量刷新颜色 / 已删除文件占位。

        statuses: {rel_posix_path: GIT_STATUS_*}
        ignored: 被 .gitignore 忽略的相对路径集合（含目录）
        deleted_by_parent: {parent_rel_posix: [filename, ...]}，已删除文件按父目录归组

        增量策略：
        - dict 直接比对（==），无变化则 short-circuit
        - 状态变了才一次性预计算 dir_bubble 表，染色时 O(1) 查表
        - 仅当"已删除文件清单"变化时刷受影响父目录（增删占位行）
        - 染色走节点级 _RENDER_CACHE_ROLE 缓存，值不变就 skip set 调用
        """
        # 1) 直接 dict 比对：状态没变就 short-circuit（绝大多数 3s 轮询命中这条）
        if (statuses == self._git_status
                and ignored == self._git_ignored
                and deleted_by_parent == self._git_deleted_by_parent):
            return

        old_deleted = self._git_deleted_by_parent
        ignored_changed = (ignored != self._git_ignored)
        self._git_status = statuses
        self._git_ignored = ignored
        self._git_deleted_by_parent = deleted_by_parent

        # 2) 预计算 dir_bubble + 清前缀缓存（只有 ignored 变了才需要清）
        self._git_dir_bubble = self._compute_dir_bubble(statuses)
        if ignored_changed:
            self._ignored_prefix_cache.clear()

        # 3) 已删除文件清单变化的父目录：只刷这些
        affected_parents: set[str] = set()
        for parent, names in deleted_by_parent.items():
            if old_deleted.get(parent) != names:
                affected_parents.add(parent)
        for parent in old_deleted:
            if parent not in deleted_by_parent:
                affected_parents.add(parent)

        for parent_rel in affected_parents:
            parent_path = self.root_path if not parent_rel else self.root_path / parent_rel
            node = self._find_node_by_path(self.tree.invisibleRootItem(), str(parent_path))
            if node is not None:
                if not (node.childCount() == 1
                        and node.child(0).text(0) == "(loading...)"):
                    self._refresh_dir_node(parent_path)

        # 4) 颜色：迭代刷整棵已加载子树（栈替代递归，避免 Python 函数调用开销）
        self._refresh_all_colors()

    @staticmethod
    def _compute_dir_bubble(statuses: dict[str, str]) -> dict[str, str]:
        """预计算每个祖先目录的冒泡状态。

        遍历每个改动文件，沿 path 一路往上爬给所有祖先打标，
        按 _BUBBLE_PRIORITY 取优先级最高的。返回 {dir_rel_posix: status_kind}。
        总耗时 O(改动数 × 平均路径深度)，远小于"每个目录扫整个 statuses 字典"。
        """
        # 优先级数字：越小越优先
        priority = {
            GIT_STATUS_CONFLICT: 0,
            GIT_STATUS_DELETED: 1,
            GIT_STATUS_MODIFIED: 2,
            GIT_STATUS_ADDED: 3,
            GIT_STATUS_UNTRACKED: 4,
        }
        out: dict[str, str] = {}
        for path, kind in statuses.items():
            kind_pri = priority.get(kind, 99)
            # 沿 path 往上拆出每一级祖先目录
            parts = path.split("/")
            # 不包括 path 本身，最后一段是文件名
            for i in range(1, len(parts)):
                ancestor = "/".join(parts[:i])
                old = out.get(ancestor)
                if old is None or priority.get(old, 99) > kind_pri:
                    out[ancestor] = kind
            # 项目根（"" 路径）也作为一个特殊键
            old_root = out.get("")
            if old_root is None or priority.get(old_root, 99) > kind_pri:
                out[""] = kind
        return out

    def reveal_path(self, file_path: str) -> None:
        """展开目录树并选中指定文件，滚动到可见位置。"""
        # 统一用小写比较（Windows 不区分大小写）
        norm_file = file_path.replace("/", "\\").lower()
        norm_root = str(self.root_path).replace("/", "\\").lower()
        if not norm_file.startswith(norm_root):
            return
        remainder = file_path[len(str(self.root_path)):]
        if remainder.startswith(("\\", "/")):
            remainder = remainder[1:]
        if not remainder:
            return
        rel_parts = Path(remainder).parts
        # 确保显示的是树视图而非搜索列表
        if self.stack.currentWidget() is not self.tree:
            self.filter_input.clear()
        # 树结构：invisibleRootItem → 项目根节点 → 子目录/文件
        # 从项目根节点开始搜索
        root_node = self.tree.topLevelItem(0)
        if root_node is None:
            return
        self.tree.expandItem(root_node)
        node = root_node
        for part in rel_parts:
            found = None
            part_lower = part.lower()
            for i in range(node.childCount()):
                child = node.child(i)
                child_path = child.data(0, Qt.ItemDataRole.UserRole)
                if child_path and Path(child_path).name.lower() == part_lower:
                    found = child
                    break
            if found is None:
                return
            # 触发懒加载
            if found.childCount() == 1 and found.child(0).text(0) == "(loading...)":
                found.removeChild(found.child(0))
                self._populate_children(found, Path(found.data(0, Qt.ItemDataRole.UserRole)))
            self.tree.expandItem(found)
            node = found
        self.tree.setCurrentItem(node)
        self.tree.scrollToItem(node)

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 顶部过滤
        top = QFrame()
        top.setObjectName("compact_toolbar")
        tl = QHBoxLayout(top)
        tl.setContentsMargins(6, 4, 6, 4)
        tl.setSpacing(4)
        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("过滤文件名（全项目）")
        apply_search_style(self.filter_input)
        self.filter_input.textChanged.connect(self._apply_filter)
        tl.addWidget(self.filter_input, 1)

        btn_refresh = QToolButton()
        btn_refresh.setText("↻")
        btn_refresh.setToolTip("刷新文件树")
        btn_refresh.clicked.connect(self._reload)
        tl.addWidget(btn_refresh)

        btn_expand = QToolButton()
        btn_expand.setText("＋")
        btn_expand.setToolTip("展开全部（谨慎，大项目可能慢）")
        btn_expand.clicked.connect(self._expand_all_safe)
        tl.addWidget(btn_expand)

        btn_collapse = QToolButton()
        btn_collapse.setText("－")
        btn_collapse.setToolTip("折叠全部")
        btn_collapse.clicked.connect(lambda: self.tree.collapseAll())
        tl.addWidget(btn_collapse)

        root.addWidget(top)

        # 主体：树 + 搜索结果列表切换
        self.stack = QStackedWidget()

        # 树
        self.tree = _MultiSelectTree()
        self.tree.setHeaderHidden(True)
        self.tree.setColumnCount(1)
        self.tree.setIndentation(18)
        self.tree.setAnimated(False)
        self.tree.setExpandsOnDoubleClick(False)
        # Ctrl/Shift 多选：批量删除/复制路径等
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setStyleSheet(_tree_style())
        self.tree.itemExpanded.connect(self._on_item_expanded)
        # 单击目录就展开/折叠，比点小箭头友好
        self.tree.itemClicked.connect(self._on_item_clicked)
        self.tree.itemDoubleClicked.connect(self._on_item_activated)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)
        self._shortcut_copy_tree = QShortcut(QKeySequence.StandardKey.Copy, self.tree)
        self._shortcut_copy_tree.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._shortcut_copy_tree.activated.connect(self._copy_tree_selection)
        self._shortcut_cut_tree = QShortcut(QKeySequence.StandardKey.Cut, self.tree)
        self._shortcut_cut_tree.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._shortcut_cut_tree.activated.connect(self._cut_tree_selection)
        self._shortcut_paste_tree = QShortcut(QKeySequence.StandardKey.Paste, self.tree)
        self._shortcut_paste_tree.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._shortcut_paste_tree.activated.connect(self._paste_to_tree_current)
        self.stack.addWidget(self.tree)

        # 搜索结果扁平列表
        self.search_list = QListWidget()
        self.search_list.setStyleSheet(
            f"QListWidget {{ background:{BG_L2}; color:{FG_PRIMARY}; border:none; outline:none; }}"
            f"QListWidget::item {{ padding: 6px 10px; }}"
            f"QListWidget::item:hover {{ background:{BG_L4}; }}"
            f"QListWidget::item:selected {{ background:{ACCENT_SUBTLE}; color:{FG_BRIGHT}; }}"
        )
        self.search_list.itemActivated.connect(self._on_search_item_activated)
        self.search_list.itemClicked.connect(self._on_search_item_activated)
        self.search_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.search_list.customContextMenuRequested.connect(self._on_search_context_menu)
        self._shortcut_copy_search = QShortcut(QKeySequence.StandardKey.Copy, self.search_list)
        self._shortcut_copy_search.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._shortcut_copy_search.activated.connect(self._copy_search_selection)
        self._shortcut_cut_search = QShortcut(QKeySequence.StandardKey.Cut, self.search_list)
        self._shortcut_cut_search.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._shortcut_cut_search.activated.connect(self._cut_search_selection)
        self._shortcut_paste_search = QShortcut(QKeySequence.StandardKey.Paste, self.search_list)
        self._shortcut_paste_search.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._shortcut_paste_search.activated.connect(self._paste_to_search_current)
        self.stack.addWidget(self.search_list)

        root.addWidget(self.stack, 1)

    def refresh_theme(self) -> None:
        self.tree.setStyleSheet(_tree_style())
        self._render_loaded_git_nodes()

    def _render_loaded_git_nodes(self) -> None:
        def walk(parent: QTreeWidgetItem) -> None:
            for index in range(parent.childCount()):
                node = parent.child(index)
                path = node.data(0, Qt.ItemDataRole.UserRole)
                kind = node.data(0, Qt.ItemDataRole.UserRole + 1)
                if path and kind in ("dir", "file"):
                    node.setData(0, _RENDER_CACHE_ROLE, None)
                    self._apply_git_color_to_node(node, Path(path))
                walk(node)
        walk(self.tree.invisibleRootItem())

    # ---- 加载 ----

    def _reload(self) -> None:
        # 保存展开状态 + 滚动位置，重建后恢复，避免每次刷新整个树都收起来
        expanded = self._collect_expanded(self.tree.invisibleRootItem())
        scroll_pos = self.tree.verticalScrollBar().value()

        self.tree.clear()
        root_item = QTreeWidgetItem([self.root_path.name])
        root_item.setData(0, Qt.ItemDataRole.UserRole, str(self.root_path))
        root_item.setData(0, Qt.ItemDataRole.UserRole + 1, "dir")
        self.tree.addTopLevelItem(root_item)
        self._populate_children(root_item, self.root_path)
        self.tree.expandItem(root_item)

        if expanded:
            self._apply_expanded(root_item, expanded)
            self.tree.verticalScrollBar().setValue(scroll_pos)

    def _collect_expanded(self, root_item: QTreeWidgetItem) -> set[str]:
        """收集当前所有处于展开状态的目录节点的绝对路径"""
        out: set[str] = set()
        def walk(item: QTreeWidgetItem) -> None:
            for i in range(item.childCount()):
                ch = item.child(i)
                if ch.data(0, Qt.ItemDataRole.UserRole + 1) == "dir" and ch.isExpanded():
                    p = ch.data(0, Qt.ItemDataRole.UserRole)
                    if p:
                        out.add(p)
                    walk(ch)
        walk(root_item)
        return out

    def _apply_expanded(self, root_item: QTreeWidgetItem, paths: set[str]) -> None:
        """对路径在 paths 集合里的目录节点逐个 expand（会触发懒加载）"""
        def walk(item: QTreeWidgetItem) -> None:
            for i in range(item.childCount()):
                ch = item.child(i)
                if ch.data(0, Qt.ItemDataRole.UserRole + 1) == "dir":
                    p = ch.data(0, Qt.ItemDataRole.UserRole)
                    if p in paths:
                        self.tree.expandItem(ch)
                        walk(ch)
        walk(root_item)

    def _populate_children(self, parent_item: QTreeWidgetItem, parent_path: Path) -> None:
        """加载直接子项。目录带懒加载占位符。"""
        try:
            entries = list(parent_path.iterdir())
        except OSError:
            return
        dirs = sorted(
            [p for p in entries if p.is_dir() and p.name not in _ALWAYS_HIDDEN],
            key=lambda p: p.name.lower(),
        )
        files = sorted(
            [p for p in entries if p.is_file() and p.name not in _ALWAYS_HIDDEN],
            key=lambda p: p.name.lower(),
        )
        # 预算 parent rel：避免对每个子项重复 relative_to
        try:
            parent_rel = str(parent_path.relative_to(self.root_path)).replace("\\", "/")
        except ValueError:
            parent_rel = ""
        if parent_rel == ".":
            parent_rel = ""
        prefix = (parent_rel + "/") if parent_rel else ""

        for d in dirs:
            node = QTreeWidgetItem(["📁  " + d.name])
            node.setData(0, Qt.ItemDataRole.UserRole, str(d))
            node.setData(0, Qt.ItemDataRole.UserRole + 1, "dir")
            node.addChild(QTreeWidgetItem(["(loading...)"]))
            self._apply_git_color_to_node(node, d, rel=prefix + d.name)
            parent_item.addChild(node)
        for f in files:
            node = QTreeWidgetItem(["  " + f.name])
            node.setData(0, Qt.ItemDataRole.UserRole, str(f))
            node.setData(0, Qt.ItemDataRole.UserRole + 1, "file")
            self._apply_git_color_to_node(node, f, rel=prefix + f.name)
            parent_item.addChild(node)
        # git 已删除文件：磁盘上读不到，需要在父目录下补占位行
        existing_names = {d.name.lower() for d in dirs} | {f.name.lower() for f in files}
        for name in self._git_deleted_by_parent.get(parent_rel, []):
            # 避免与磁盘上同名条目重复（理论上不会，但 rename 等极端情况兜底）
            if name.lower() in existing_names:
                continue
            node = QTreeWidgetItem(["  " + name])
            # 占位节点没有真实路径，但记下"原本应该在哪"以便 tooltip
            virtual_path = (parent_path / name)
            node.setData(0, Qt.ItemDataRole.UserRole, str(virtual_path))
            node.setData(0, Qt.ItemDataRole.UserRole + 1, "deleted")
            node.setForeground(0, QColor(str(GIT_DEL)))
            self._set_strikethrough(node, True)
            node.setToolTip(0, self._STATUS_TOOLTIPS[GIT_STATUS_DELETED])
            # 占位节点不可拖、不可改
            flags = node.flags()
            flags &= ~Qt.ItemFlag.ItemIsDragEnabled
            flags &= ~Qt.ItemFlag.ItemIsEditable
            node.setFlags(flags)
            parent_item.addChild(node)

    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        if item.childCount() == 1 and item.child(0).text(0) == "(loading...)":
            item.removeChild(item.child(0))
            path = Path(item.data(0, Qt.ItemDataRole.UserRole))
            self._populate_children(item, path)

    _STATUS_COLORS = {
        GIT_STATUS_CONFLICT: GIT_CONFLICT,
        GIT_STATUS_DELETED: GIT_DEL,
        GIT_STATUS_ADDED: GIT_ADD,
        GIT_STATUS_UNTRACKED: GIT_ADD,
        GIT_STATUS_MODIFIED: GIT_MODIFY,
    }
    # 目录冒泡颜色优先级：冲突 > 删除 > 修改 > 新增/未跟踪
    _BUBBLE_PRIORITY = (
        GIT_STATUS_CONFLICT,
        GIT_STATUS_DELETED,
        GIT_STATUS_MODIFIED,
        GIT_STATUS_ADDED,
        GIT_STATUS_UNTRACKED,
    )
    _STATUS_TOOLTIPS = {
        GIT_STATUS_CONFLICT: "git: 合并冲突",
        GIT_STATUS_DELETED: "git: 已删除",
        GIT_STATUS_ADDED: "git: 已添加（staged）",
        GIT_STATUS_UNTRACKED: "git: 未跟踪",
        GIT_STATUS_MODIFIED: "git: 已修改",
    }

    def _apply_git_color_to_node(self, node: QTreeWidgetItem, path: Path,
                                  rel: str | None = None) -> None:
        """根据 git 状态给节点染色 + 设置 tooltip。
        节点级缓存：上次染色后的 (color, strikethrough, tooltip) 存在 item.data，
        本轮值不变就完全 skip set 调用，避免 Qt 误判 dirty 触发 viewport 重绘。

        rel 由调用方预先算好可以省一次 relative_to。
        """
        if rel is None:
            try:
                rel = str(path.relative_to(self.root_path)).replace("\\", "/")
            except ValueError:
                return
            if rel == ".":
                rel = ""
        # 计算目标渲染态
        if self._is_ignored(rel):
            color, strike, tip = GIT_IGNORED, False, "被 .gitignore 忽略"
        else:
            own = self._git_status.get(rel)
            # 是否目录：读节点上已存的类型标记，不再 path.is_dir() 碰磁盘
            # （每个节点查一次磁盘在 Windows 大树上很拖，染色一轮可能上千次）
            is_dir = node.data(0, Qt.ItemDataRole.UserRole + 1) == "dir"
            bubble = self._git_dir_bubble.get(rel) if is_dir else None
            if own is not None and own in self._STATUS_COLORS:
                color = self._STATUS_COLORS[own]
                strike = (own == GIT_STATUS_DELETED)
                tip = self._STATUS_TOOLTIPS.get(own, "")
            elif bubble is not None and bubble in self._STATUS_COLORS:
                color = self._STATUS_COLORS[bubble]
                strike = False
                tip = f"包含 {self._STATUS_TOOLTIPS.get(bubble, '')} 的子项"
            else:
                color, strike, tip = FG_PRIMARY, False, ""
        # 与缓存比对：完全相同就 skip
        cache = node.data(0, _RENDER_CACHE_ROLE)
        target = (color, strike, tip)
        if cache == target:
            return
        node.setData(0, _RENDER_CACHE_ROLE, target)
        node.setForeground(0, QColor(str(color)))
        self._set_strikethrough(node, strike)
        node.setToolTip(0, tip)

    def _is_ignored(self, rel: str) -> bool:
        """rel 是否被 .gitignore 命中。带前缀缓存：每个 rel 第一次扫，后续 O(1)。"""
        if not self._git_ignored:
            return False
        cached = self._ignored_prefix_cache.get(rel)
        if cached is not None:
            return cached
        if rel in self._git_ignored:
            self._ignored_prefix_cache[rel] = True
            return True
        # 父目录被 ignored → 子节点也算
        for ig in self._git_ignored:
            if rel.startswith(ig + "/"):
                self._ignored_prefix_cache[rel] = True
                return True
        self._ignored_prefix_cache[rel] = False
        return False

    @staticmethod
    def _set_strikethrough(node: QTreeWidgetItem, enabled: bool) -> None:
        font = node.font(0)
        if font.strikeOut() == enabled:
            return
        font.setStrikeOut(enabled)
        node.setFont(0, font)

    def _refresh_all_colors(self) -> None:
        """迭代刷新整棵已加载子树的颜色。
        用栈替代递归减小 Python 函数调用开销；命中节点级缓存的节点 setForeground 等
        都不会触发，对 Qt 是零开销，整轮在大项目里稳定 < 5ms。
        """
        root_path_str = str(self.root_path)
        root_path_len = len(root_path_str)
        # 栈元素 (item, abs_path_str)，根节点先入栈
        top = self.tree.topLevelItem(0)
        if top is None:
            return
        stack: list[tuple[QTreeWidgetItem, str]] = [(top, root_path_str)]
        while stack:
            item, abs_str = stack.pop()
            kind = item.data(0, Qt.ItemDataRole.UserRole + 1)
            if kind == "deleted":
                # 删除占位节点的颜色在 _populate_children 时已经设好，无需刷
                continue
            # 计算 rel：剥离 root 前缀，比 Path.relative_to 快得多
            if abs_str == root_path_str:
                rel = ""
            elif abs_str.startswith(root_path_str):
                rel = abs_str[root_path_len:].lstrip("\\/").replace("\\", "/")
            else:
                continue
            self._apply_git_color_to_node(item, Path(abs_str), rel=rel)
            # 仅递归到已加载（非懒加载占位）的目录里
            cc = item.childCount()
            if cc == 0:
                continue
            if cc == 1 and item.child(0).text(0) == "(loading...)":
                continue
            for i in range(cc):
                child = item.child(i)
                child_kind = child.data(0, Qt.ItemDataRole.UserRole + 1)
                if child_kind not in ("dir", "file"):
                    continue
                child_abs = child.data(0, Qt.ItemDataRole.UserRole)
                if child_abs:
                    stack.append((child, child_abs))

    def _expand_all_safe(self) -> None:
        """展开全部前先把所有懒加载节点都加载一遍"""
        self._eagerly_load_all(self.tree.invisibleRootItem())
        self.tree.expandAll()

    def _eagerly_load_all(self, node: QTreeWidgetItem) -> None:
        # 触发当前节点的懒加载
        if node.childCount() == 1 and node.child(0).text(0) == "(loading...)":
            node.removeChild(node.child(0))
            path_str = node.data(0, Qt.ItemDataRole.UserRole)
            if path_str:
                self._populate_children(node, Path(path_str))
        for i in range(node.childCount()):
            child = node.child(i)
            if child.data(0, Qt.ItemDataRole.UserRole + 1) == "dir":
                self._eagerly_load_all(child)

    # ---- 过滤 ----

    def _apply_filter(self, text: str) -> None:
        text = text.strip()
        if not text:
            self.stack.setCurrentWidget(self.tree)
            return
        if self.indexer is None:
            # 无索引：回退到只过滤当前已加载节点（能力有限）
            self.stack.setCurrentWidget(self.tree)
            self._filter_tree(self.tree.invisibleRootItem(), text.lower())
            return
        # 有 FileIndexer：切换到扁平结果列表，全项目搜
        self.stack.setCurrentWidget(self.search_list)
        self.search_list.clear()
        hits = self.indexer.search(text, limit=300)
        if not hits:
            ph = QListWidgetItem(f"(无匹配)  共索引 {self.indexer.count()} 个文件")
            ph.setFlags(Qt.ItemFlag.NoItemFlags)
            ph.setForeground(Qt.GlobalColor.darkGray)
            self.search_list.addItem(ph)
            return
        for f in hits:
            item = QListWidgetItem(f"  {f.name_original}\n    {f.rel_path}")
            item.setData(Qt.ItemDataRole.UserRole, f.abs_path)
            self.search_list.addItem(item)

    def _filter_tree(self, item: QTreeWidgetItem, text: str) -> bool:
        any_visible = False
        for i in range(item.childCount()):
            child = item.child(i)
            kind = child.data(0, Qt.ItemDataRole.UserRole + 1)
            name = child.text(0).lower()
            if kind == "dir":
                child_match = self._filter_tree(child, text)
                visible = child_match or (not text) or (text in name)
                child.setHidden(not visible)
                any_visible = any_visible or visible
            else:
                visible = (not text) or (text in name)
                child.setHidden(not visible)
                any_visible = any_visible or visible
        return any_visible

    # ---- 事件 ----

    def _on_item_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        """单击目录 → 切换展开；单击文件什么都不做（双击打开）。
        Ctrl/Shift 按下时是在做多选——不要顺手把目录展开打断选择。
        """
        mods = QApplication.keyboardModifiers()
        if mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
            return
        kind = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if kind == "dir":
            item.setExpanded(not item.isExpanded())

    def _on_item_activated(self, item: QTreeWidgetItem, _col: int) -> None:
        kind = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if kind == "file":
            path = item.data(0, Qt.ItemDataRole.UserRole)
            self.fileActivated.emit(path)
        elif kind == "dir":
            item.setExpanded(not item.isExpanded())

    def _on_search_item_activated(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if path:
            self.fileActivated.emit(path)

    def _on_context_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if not item:
            self._show_blank_context_menu(self.tree.mapToGlobal(pos))
            return
        path = item.data(0, Qt.ItemDataRole.UserRole)
        kind = item.data(0, Qt.ItemDataRole.UserRole + 1)
        # 已删除占位节点：磁盘上无文件，所有文件操作都不适用——直接屏蔽右键
        if kind == "deleted":
            return
        # 多选场景：优先用 mousePressEvent 抓的快照（避免 super 改 selection 影响判断）；
        # 快照为空时回退到 selectedItems()。判断走 path 字符串，不依赖对象身份。
        snapshot = self.tree.take_right_press_snapshot()
        selected = snapshot if snapshot else self.tree.selectedItems()
        # 多选里若混入 deleted 占位节点，过滤掉——下面的批量操作只针对真实存在的项
        selected = [it for it in selected
                    if it.data(0, Qt.ItemDataRole.UserRole + 1) != "deleted"]
        selected_paths = {it.data(0, Qt.ItemDataRole.UserRole) for it in selected}
        if path in selected_paths and len(selected) > 1:
            batch = [(Path(it.data(0, Qt.ItemDataRole.UserRole)),
                      it.data(0, Qt.ItemDataRole.UserRole + 1), it)
                     for it in selected
                     if it.data(0, Qt.ItemDataRole.UserRole)]
        else:
            batch = [(Path(path), kind, item)]
        self._show_context_menu(self.tree.mapToGlobal(pos), path, kind,
                                tree_item=item, batch=batch)

    def _on_search_context_menu(self, pos) -> None:
        item = self.search_list.itemAt(pos)
        if not item:
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        if not path:
            return
        # 搜索列表项不是 QTreeWidgetItem，tree_item 传 None → 走磁盘扫描兜底
        self._show_context_menu(self.search_list.mapToGlobal(pos), path, "file",
                                tree_item=None, batch=[(Path(path), "file", None)])

    def _show_context_menu(self, global_pos, path: str, kind: str | None,
                           tree_item=None, batch=None) -> None:
        if not path:
            return
        p = Path(path)
        filename = p.name
        # 新建文件的目标目录：在目录上 → 该目录；在文件上 → 文件所在目录
        target_dir = p if kind == "dir" else p.parent

        menu = QMenu(self)

        # 顶置：可执行脚本（.bat/.cmd/.ps1/.exe）的运行入口——常用，要在最显眼的位置
        if kind == "file" and p.suffix.lower() in _RUNNABLE_SCRIPT_EXTS:
            menu.addAction(f"▶  运行 {filename}",
                           lambda fp=str(p): self.scriptRunRequested.emit(fp))
            menu.addSeparator()

        # 当前对象的文件操作：复制 / 粘贴 / 重命名先放一起，符合资源管理器习惯
        if batch and len(batch) > 1:
            copy_label = f"复制选中的 {len(batch)} 项"
        else:
            copy_label = "复制目录" if kind == "dir" else "复制文件"
        menu.addAction(copy_label,
                       lambda b=batch: self._copy_paths_to_clipboard([x[0] for x in b or []]))
        cut_action = menu.addAction("剪切",
                                    lambda b=batch: self._cut_paths_to_clipboard([x[0] for x in b or []]))
        if p.resolve() == self.root_path.resolve():
            cut_action.setEnabled(False)
        paste_label = "粘贴到此目录" if kind == "dir" else "粘贴到所在目录"
        paste_action = menu.addAction(paste_label,
                                      lambda d=target_dir: self._paste_clipboard_to(d))
        paste_action.setEnabled(bool(self._clipboard_file_paths()))
        menu.addAction("重命名…", lambda: self._rename_path(p, kind, tree_item))
        menu.addSeparator()

        # 新建文件（最常用：md）+ 新建目录
        menu.addAction("📄  新建 Markdown 文件…",
                       lambda d=target_dir: self._create_new_file(d, default_ext=".md"))
        menu.addAction("📄  新建文件…",
                       lambda d=target_dir: self._create_new_file(d, default_ext=""))
        menu.addAction("📁  新建目录…",
                       lambda d=target_dir: self._create_new_dir(d))
        menu.addSeparator()

        # 外部工具 / 系统定位
        menu.addAction("cc", lambda d=target_dir: self._open_cc(d))
        menu.addAction("codex", lambda d=target_dir: self._open_codex(d))
        menu.addAction(REVEAL_LABEL, lambda: reveal_in_explorer(path))
        menu.addSeparator()

        # 复制文本形式的路径信息
        name_label = "复制目录名" if kind == "dir" else "复制文件名"
        menu.addAction(name_label, lambda: QApplication.clipboard().setText(filename))
        if kind != "dir":
            menu.addAction("复制文件名（不含扩展名）",
                           lambda: QApplication.clipboard().setText(p.stem))
        menu.addAction("复制绝对路径", lambda: QApplication.clipboard().setText(path))
        try:
            rel = str(p.relative_to(self.root_path)).replace("\\", "/")
        except ValueError:
            rel = path
        menu.addAction("复制相对路径", lambda r=rel: QApplication.clipboard().setText(r))

        # 危险操作放到底部，避免误点
        menu.addSeparator()
        if batch and len(batch) > 1:
            del_label = f"🗑  删除选中的 {len(batch)} 项到回收站"
        else:
            del_label = "🗑  删除目录到回收站" if kind == "dir" else "🗑  删除到回收站"
        menu.addAction(del_label, lambda b=batch: self._delete_paths(b))

        menu.exec(global_pos)

    def _show_blank_context_menu(self, global_pos) -> None:
        menu = QMenu(self)
        paste_action = menu.addAction("粘贴到项目根目录",
                                      lambda: self._paste_clipboard_to(self.root_path))
        paste_action.setEnabled(bool(self._clipboard_file_paths()))
        menu.exec(global_pos)

    def _copy_tree_selection(self) -> None:
        items = self.tree.selectedItems()
        if not items and self.tree.currentItem() is not None:
            items = [self.tree.currentItem()]
        paths = [Path(it.data(0, Qt.ItemDataRole.UserRole)) for it in items
                 if it.data(0, Qt.ItemDataRole.UserRole)]
        self._copy_paths_to_clipboard(paths)

    def _cut_tree_selection(self) -> None:
        items = self.tree.selectedItems()
        if not items and self.tree.currentItem() is not None:
            items = [self.tree.currentItem()]
        paths = [Path(it.data(0, Qt.ItemDataRole.UserRole)) for it in items
                 if it.data(0, Qt.ItemDataRole.UserRole)]
        self._cut_paths_to_clipboard(paths)

    def _copy_search_selection(self) -> None:
        items = self.search_list.selectedItems()
        if not items and self.search_list.currentItem() is not None:
            items = [self.search_list.currentItem()]
        paths = [Path(it.data(Qt.ItemDataRole.UserRole)) for it in items
                 if it.data(Qt.ItemDataRole.UserRole)]
        self._copy_paths_to_clipboard(paths)

    def _cut_search_selection(self) -> None:
        items = self.search_list.selectedItems()
        if not items and self.search_list.currentItem() is not None:
            items = [self.search_list.currentItem()]
        paths = [Path(it.data(Qt.ItemDataRole.UserRole)) for it in items
                 if it.data(Qt.ItemDataRole.UserRole)]
        self._cut_paths_to_clipboard(paths)

    def _paste_to_tree_current(self) -> None:
        item = self.tree.currentItem()
        if item is None:
            self._paste_clipboard_to(self.root_path)
            return
        path = item.data(0, Qt.ItemDataRole.UserRole)
        kind = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if not path:
            self._paste_clipboard_to(self.root_path)
            return
        p = Path(path)
        self._paste_clipboard_to(p if kind == "dir" else p.parent)

    def _paste_to_search_current(self) -> None:
        item = self.search_list.currentItem()
        if item is None:
            self._paste_clipboard_to(self.root_path)
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        self._paste_clipboard_to(Path(path).parent if path else self.root_path)

    def _copy_paths_to_clipboard(self, paths: list[Path]) -> None:
        existing = [p for p in paths if p.exists()]
        if not existing:
            return
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(p)) for p in existing])
        mime.setText("\n".join(str(p) for p in existing))
        QApplication.clipboard().setMimeData(mime)

    def _cut_paths_to_clipboard(self, paths: list[Path]) -> None:
        existing = [p for p in paths
                    if p.exists() and p.resolve() != self.root_path.resolve()]
        if not existing:
            return
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(p)) for p in existing])
        mime.setText("\n".join(str(p) for p in existing))
        mime.setData(_CUT_MIME, b"1")
        QApplication.clipboard().setMimeData(mime)

    def _clipboard_is_cut(self) -> bool:
        mime = QApplication.clipboard().mimeData()
        return bool(mime and mime.hasFormat(_CUT_MIME))

    def _clipboard_file_paths(self) -> list[Path]:
        mime = QApplication.clipboard().mimeData()
        if mime is None:
            return []
        paths: list[Path] = []
        if mime.hasUrls():
            for url in mime.urls():
                if url.isLocalFile():
                    local = url.toLocalFile()
                    if local:
                        paths.append(Path(local))
        if not paths and mime.hasText():
            for line in mime.text().splitlines():
                text = line.strip().strip('"')
                if text:
                    paths.append(Path(text))
        out: list[Path] = []
        seen: set[str] = set()
        for p in paths:
            if not p.exists():
                continue
            try:
                key = str(p.resolve()).lower()
            except OSError:
                key = str(p).lower()
            if key not in seen:
                seen.add(key)
                out.append(p)
        return out

    def _paste_clipboard_to(self, target_dir: Path) -> None:
        sources = self._clipboard_file_paths()
        if not sources:
            QMessageBox.warning(self, "粘贴", "剪贴板里没有可粘贴的文件或目录")
            return
        move = self._clipboard_is_cut()
        worker = _PasteWorker([str(p) for p in sources], str(target_dir), move,
                              QApplication.instance())
        self._paste_workers.append(worker)
        worker.done.connect(self._on_paste_done)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_paste_done(self, target_dir: str, changed: int,
                       failed: list, move: bool) -> None:
        worker = self.sender()
        if worker in self._paste_workers:
            self._paste_workers.remove(worker)
        self._refresh_dir_node(Path(target_dir))
        if move and changed > 0:
            QApplication.clipboard().clear()
        if self.stack.currentWidget() is self.search_list:
            self._apply_filter(self.filter_input.text())
        if failed:
            shown = "\n\n".join(str(x) for x in failed[:5])
            more = f"\n\n还有 {len(failed) - 5} 项失败" if len(failed) > 5 else ""
            QMessageBox.warning(self, "部分粘贴失败", shown + more)

    def _rename_path(self, p: Path, kind: str | None, tree_item=None) -> None:
        # 不允许重命名项目根目录
        if p.resolve() == self.root_path.resolve():
            QMessageBox.warning(self, "重命名", "不能重命名项目根目录")
            return
        desc = "目录" if kind == "dir" else "文件"
        new_name, ok = QInputDialog.getText(
            self, f"重命名{desc}",
            f"输入新名称（当前：{p.name}）：",
            text=p.name,
        )
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name or new_name == p.name:
            return
        # 阻止跨目录注入
        if "/" in new_name or "\\" in new_name or new_name in (".", ".."):
            QMessageBox.warning(self, "重命名", "新名称不能包含路径分隔符")
            return
        new_path = p.with_name(new_name)
        # Windows 文件系统不区分大小写：仅改大小写时 exists() 仍为 True，要放行
        if new_name.lower() != p.name.lower() and new_path.exists():
            QMessageBox.warning(self, "重命名", f"目标已存在：{new_name}")
            return
        try:
            p.rename(new_path)
        except OSError as e:
            QMessageBox.warning(self, "重命名失败", f"{p}\n\n{e}")
            return
        # 重命名后排序位置可能变化，刷新父目录节点最稳
        self._refresh_dir_node(p.parent)
        # 搜索列表里也得重新过滤一遍
        if self.stack.currentWidget() is self.search_list:
            self._apply_filter(self.filter_input.text())

    def _delete_paths(self, batch) -> None:
        """直接删除（不弹二次确认），支持多选批量。
        batch: [(Path, kind, QTreeWidgetItem|None), ...]

        Windows 上 QFile.moveToTrash 走 shell IFileOperation：返回 ok=True 时
        文件可能还没真正从磁盘消失（shell 异步写回收站元数据，50-300ms 延迟），
        这时 Path.iterdir() 还能看到被删路径，依赖扫描刷新反而像"没刷"；此外
        路径字符串比对受 Windows 路径规范化（盘符大小写 / \\ vs /）影响并不可靠。
        用传进来的 QTreeWidgetItem 引用直接 removeChild 最稳。
        """
        # 过滤掉项目根目录
        items = [(p, k, ti) for (p, k, ti) in batch
                 if p.resolve() != self.root_path.resolve()]
        if not items:
            QMessageBox.warning(self, "删除", "不能删除项目根目录")
            return
        # 父先删（路径短的先）：父被 removeChild 后子的 tree_item.parent() 会是 None，
        # 同时子的磁盘路径也已经随父进了回收站，下面用 exists() + parent() 双重判空跳过。
        items.sort(key=lambda x: len(str(x[0])))
        failed = []
        affected_parents: set[Path] = set()
        for p, _kind, tree_item in items:
            if not p.exists():
                continue  # 已经随父一起进回收站了
            try:
                # PySide6 上 QFile.moveToTrash 返回单个 bool（不是 tuple）；
                # 老版本/Qt C++ 的 (bool, path) 二元组在这里不适用，不要解包。
                result = QFile.moveToTrash(str(p))
                ok = bool(result[0]) if isinstance(result, tuple) else bool(result)
            except (OSError, RuntimeError) as e:
                failed.append(f"{p}\n  {e}")
                continue
            if not ok:
                failed.append(str(p))
                continue
            affected_parents.add(p.parent)
            if tree_item is not None and tree_item.parent() is not None:
                tree_item.parent().removeChild(tree_item)
            elif tree_item is None:
                # 搜索列表删除路径——扫磁盘兜底；watchdog 事件跟上后就正确了
                self._refresh_dir_node(p.parent)
        if failed:
            QMessageBox.warning(self, "部分删除失败", "\n\n".join(failed[:5]))
        if affected_parents:
            QTimer.singleShot(400, lambda dirs=list(affected_parents): self._deferred_refresh(dirs))
        if self.stack.currentWidget() is self.search_list:
            self._apply_filter(self.filter_input.text())

    def _deferred_refresh(self, dirs: list[Path]) -> None:
        for d in dirs:
            self._refresh_dir_node(d)
        if self.stack.currentWidget() is self.search_list:
            self._apply_filter(self.filter_input.text())

    def _open_codex(self, target_dir: Path) -> None:
        """使用资源管理器 open gpt 的打开方式打开当前目录。"""
        command = open_in_codex_args(target_dir)
        try:
            subprocess.Popen(
                command,
                cwd=str(target_dir),
                creationflags=CREATE_NO_WINDOW,
                close_fds=True,
            )
        except OSError as e:
            QMessageBox.warning(self, "启动 open gpt 失败", f"{e}")

    def _open_cc(self, target_dir: Path) -> None:
        """在 AgentDesk 中为 cc 入口打开当前目录。"""
        command = open_in_cc_args(target_dir)
        if not command:
            QMessageBox.warning(
                self, "启动 AgentDesk 失败",
                "没有找到 AgentDesk，请先安装或配置 AGENTDESK_EXE。",
            )
            return
        try:
            subprocess.Popen(
                command,
                cwd=str(target_dir),
                creationflags=CREATE_NO_WINDOW,
                close_fds=True,
            )
        except OSError as e:
            QMessageBox.warning(self, "启动 AgentDesk 失败", f"{e}")

    def _create_new_file(self, target_dir: Path, default_ext: str = "") -> None:
        """在指定目录下新建文件。用户输入文件名，自动补 default_ext 后缀。"""
        try:
            rel_dir = target_dir.relative_to(self.root_path)
            rel_text = str(rel_dir).replace("\\", "/") if str(rel_dir) != "." else "/"
        except ValueError:
            rel_text = str(target_dir)
        prompt = f"在 {rel_text} 下新建文件名："
        default_name = "新文档" + default_ext if default_ext else ""
        name, ok = QInputDialog.getText(self, "新建文件", prompt, text=default_name)
        if not ok:
            return
        name = name.strip()
        if not name:
            return
        # 阻止跨目录注入（用户写了 ../xxx 或绝对路径都拒绝）
        if "/" in name or "\\" in name or name in (".", ".."):
            QMessageBox.warning(self, "新建文件", "文件名不能包含路径分隔符")
            return
        # 没写后缀且默认有就补上
        if default_ext and "." not in name:
            name += default_ext
        new_path = target_dir / name
        if new_path.exists():
            QMessageBox.warning(self, "新建文件", f"文件已存在：{new_path.name}")
            return
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            new_path.write_text("", encoding="utf-8")
        except OSError as e:
            QMessageBox.warning(self, "新建文件失败", str(e))
            return
        # 刷新树到新文件所在目录
        self._refresh_dir_node(target_dir)
        self.fileCreated.emit(str(new_path))

    def _create_new_dir(self, target_dir: Path) -> None:
        """在指定目录下新建子目录。"""
        try:
            rel_dir = target_dir.relative_to(self.root_path)
            rel_text = str(rel_dir).replace("\\", "/") if str(rel_dir) != "." else "/"
        except ValueError:
            rel_text = str(target_dir)
        prompt = f"在 {rel_text} 下新建目录名："
        name, ok = QInputDialog.getText(self, "新建目录", prompt, text="新目录")
        if not ok:
            return
        name = name.strip()
        if not name:
            return
        # 阻止跨目录注入
        if "/" in name or "\\" in name or name in (".", ".."):
            QMessageBox.warning(self, "新建目录", "目录名不能包含路径分隔符")
            return
        new_path = target_dir / name
        if new_path.exists():
            QMessageBox.warning(self, "新建目录", f"已存在：{new_path.name}")
            return
        try:
            new_path.mkdir(parents=True)
        except OSError as e:
            QMessageBox.warning(self, "新建目录失败", str(e))
            return
        self._refresh_dir_node(target_dir)

    def _refresh_dir_node(self, dir_path: Path) -> None:
        """重新加载某个目录节点的子项，并保留子树展开状态。
        找不到节点（路径不在已展开范围内）则整树刷新。
        """
        node = self._find_node_by_path(self.tree.invisibleRootItem(), str(dir_path))
        if node is None:
            self._reload()
            return
        # 保存子树展开状态
        sub_expanded = self._collect_expanded(node)
        # 清掉子项重新填充
        node.takeChildren()
        self._populate_children(node, dir_path)
        node.setExpanded(True)
        if sub_expanded:
            self._apply_expanded(node, sub_expanded)

    def _find_node_by_path(self, parent: QTreeWidgetItem, target: str) -> QTreeWidgetItem | None:
        for i in range(parent.childCount()):
            child = parent.child(i)
            data = child.data(0, Qt.ItemDataRole.UserRole)
            if data == target:
                return child
            if child.data(0, Qt.ItemDataRole.UserRole + 1) == "dir":
                # 仅递归到已加载（非懒加载占位）的目录里找
                if not (child.childCount() == 1 and child.child(0).text(0) == "(loading...)"):
                    found = self._find_node_by_path(child, target)
                    if found:
                        return found
        # 顶层根节点也要比对
        if parent is self.tree.invisibleRootItem() and self.tree.topLevelItemCount() > 0:
            top = self.tree.topLevelItem(0)
            if top.data(0, Qt.ItemDataRole.UserRole) == target:
                return top
        return None
