"""聚合目录工作台：一个顶层 Tab 管理自己的项目和需求工作区。"""
from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QHBoxLayout,
    QHeaderView, QLabel, QMessageBox, QPushButton, QSplitter, QStackedWidget,
    QStyle, QTabWidget, QToolButton, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget, QFrame, QScrollArea, QSizePolicy,
)

from src.core.aggregate_workspace import (
    AggregateProject, DevelopmentWorkspace, save_aggregate_project,
)
from src.core.aggregate_workspace_manager import build_workspace_dashboard_snapshot
from src.core.config import AppConfig, ProjectEntry
from src.core.development_workspace_service import (
    create_development_workspace, delete_development_workspace,
    inspect_workspace_delete, merge_development_workspace,
    sync_commit_and_push_development_workspace,
)
from src.core.git_ops import current_branch
from src.core.path_utils import normalized_path_key
from src.core.project_detector import detect_project
from src.core.service_state import (
    STATE_RUNNING_EXTERNAL, STATE_STARTING, STATE_STOPPING, STATE_UNKNOWN,
)
from src.core.tool_launchers import (
    CREATE_NO_WINDOW, open_in_cc_args, open_in_codex_args,
)
from src.ui.aggregate_project_dialog import AggregateProjectDialog
from src.ui.development_workspace_dialog import DevelopmentWorkspaceDialog
from src.ui.project_tab import ProjectTab
from src.ui.theme import (
    COLOR_ERROR, COLOR_SUCCESS, COLOR_WARN, FG_DIM, GAP_LG, GAP_MD, GAP_NONE,
    GAP_SM,
)

action_log = logging.getLogger("mini-ide.action")


class _PrepareProjectsWorker(QThread):
    done = Signal(object, int)

    def __init__(self, items: tuple[dict, ...], generation: int, parent=None):
        super().__init__(parent)
        self.items = items
        self.generation = generation

    def run(self) -> None:
        results = []
        for item in self.items:
            try:
                meta = detect_project(
                    item["path"], runtime_config=item.get("runtime"),
                )
                branch = item.get("branch", "")
                if not branch and not item.get("shared"):
                    branch = current_branch(item["path"])
                results.append({**item, "meta": meta, "branch": branch, "error": ""})
            except Exception as exc:
                results.append({**item, "meta": None, "branch": "", "error": str(exc)})
        self.done.emit(results, self.generation)


class _SaveProjectWorker(QThread):
    done = Signal(bool, str)

    def __init__(self, project: AggregateProject, parent=None):
        super().__init__(parent)
        self.project = project

    def run(self) -> None:
        try:
            self.done.emit(True, str(save_aggregate_project(self.project)))
        except Exception as exc:
            self.done.emit(False, str(exc))


class _WorkspaceSnapshotWorker(QThread):
    done = Signal(object, str)

    def __init__(self, aggregate_path: str, parent=None):
        super().__init__(parent)
        self.aggregate_path = aggregate_path

    def run(self) -> None:
        try:
            snapshot = build_workspace_dashboard_snapshot((self.aggregate_path,))
            self.done.emit(snapshot.development_workspaces, "")
        except Exception as exc:
            self.done.emit((), str(exc))


class _CreateWorkspaceWorker(QThread):
    done = Signal(object)

    def __init__(self, plan, parent=None):
        super().__init__(parent)
        self.plan = plan

    def run(self) -> None:
        self.done.emit(create_development_workspace(self.plan))


class _MergeWorkspaceWorker(QThread):
    done = Signal(object, str)

    def __init__(self, workspace: DevelopmentWorkspace, parent=None):
        super().__init__(parent)
        self.workspace = workspace

    def run(self) -> None:
        try:
            self.done.emit(merge_development_workspace(self.workspace), "")
        except Exception as exc:
            self.done.emit(None, str(exc))


class _SyncPushWorkspaceWorker(QThread):
    done = Signal(object, str)

    def __init__(
        self,
        workspace: DevelopmentWorkspace,
        message: str,
        *,
        keep_conflicts: bool,
        parent=None,
    ):
        super().__init__(parent)
        self.workspace = workspace
        self.message = message
        self.keep_conflicts = keep_conflicts

    def run(self) -> None:
        try:
            self.done.emit(
                sync_commit_and_push_development_workspace(
                    self.workspace,
                    self.message,
                    keep_conflicts=self.keep_conflicts,
                ),
                "",
            )
        except Exception as exc:
            self.done.emit(None, str(exc))


class _DeletePreflightWorker(QThread):
    done = Signal(object, str)

    def __init__(
        self, workspace: DevelopmentWorkspace, running_paths: tuple[str, ...], parent=None,
    ):
        super().__init__(parent)
        self.workspace = workspace
        self.running_paths = running_paths

    def run(self) -> None:
        try:
            self.done.emit(
                inspect_workspace_delete(self.workspace, self.running_paths), "",
            )
        except Exception as exc:
            self.done.emit(None, str(exc))


class _DeleteWorkspaceWorker(QThread):
    done = Signal(bool, object, str)

    def __init__(self, plan, confirm_unmerged: bool, parent=None):
        super().__init__(parent)
        self.plan = plan
        self.confirm_unmerged = confirm_unmerged

    def run(self) -> None:
        action_log.info("[GUI] 开始删除工作区 workspace=%s", self.plan.workspace.root_path)
        try:
            ok, kept, error = delete_development_workspace(
                self.plan, confirm_unmerged_backed_up=self.confirm_unmerged,
            )
        except Exception as exc:
            action_log.exception("[GUI] 删除工作区异常 workspace=%s", self.plan.workspace.root_path)
            ok, kept, error = False, (), f"删除未完成：{exc}"
        action_log.info(
            "[GUI] 工作区清理结果 workspace=%s ok=%s remaining=%s error=%s",
            self.plan.workspace.root_path, ok, kept, error,
        )
        self.done.emit(ok, kept, error)


class _WorkspaceCard(QFrame):
    selected = Signal(object)
    activated = Signal(object)
    action_requested = Signal(object, str)

    def __init__(self, summary, current: bool, parent=None):
        super().__init__(parent)
        self.summary = summary
        self.setObjectName("workspace_card")
        self.setProperty("current", current)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        name = QLabel(summary.name)
        name.setObjectName("workspace_card_title")
        head.addWidget(name)
        if current:
            current_label = QLabel("当前工作区")
            current_label.setObjectName("workspace_current")
            head.addWidget(current_label)
        head.addStretch(1)
        if summary.error:
            self._add_status(head, "配置错误", "error")
        else:
            self._add_status(
                head,
                f"{summary.git_change_count} 个未提交改动"
                if summary.git_change_count else "工作区干净",
                "warn" if summary.git_change_count else "ok",
            )
        root.addLayout(head)

        divider = QFrame()
        divider.setObjectName("workspace_card_divider")
        divider.setFixedHeight(1)
        root.addWidget(divider)

        details = QVBoxLayout()
        details.setSpacing(4)
        project_row = QHBoxLayout()
        project_row.setSpacing(7)
        label = QLabel("修改项目")
        label.setObjectName("workspace_card_label")
        label.setFixedWidth(64)
        project_row.addWidget(label)
        for project_id in summary.component_ids:
            chip = QLabel(project_id)
            chip.setObjectName("workspace_project_chip")
            project_row.addWidget(chip)
        project_row.addStretch(1)
        details.addLayout(project_row)

        branch_row = QHBoxLayout()
        branch_row.setSpacing(7)
        branch_label = QLabel("任务分支")
        branch_label.setObjectName("workspace_card_label")
        branch_label.setFixedWidth(64)
        branch_row.addWidget(branch_label)
        branch = QLabel(
            f'<a href="branches">{len(summary.task_branches)} 个任务分支 · 点击查看</a>'
            if summary.task_branches else "无任务分支"
        )
        branch.setObjectName("workspace_branch_link")
        branch.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse)
        branch.linkActivated.connect(lambda _link: self._show_branches())
        branch_row.addWidget(branch)
        created = QLabel(f"创建于 {summary.created_at or '-'}")
        created.setObjectName("workspace_card_meta")
        branch_row.addWidget(created)
        branch_row.addStretch(1)
        details.addLayout(branch_row)
        root.addLayout(details)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self._add_button(actions, "进入工作区", "activate", primary=True)
        self._add_button(actions, "同步并推送", "sync-push")
        self._add_button(actions, "合并代码", "merge")
        self._add_button(actions, "在 codex 中打开", "codex")
        self._add_button(actions, "在 cc 中打开", "cc")
        self._add_button(actions, "删除工作区", "delete", danger=True)
        root.addLayout(actions)

    def _add_status(self, layout, text: str, tone: str) -> None:
        status = QLabel(text)
        status.setProperty("role", f"workspace-status-{tone}")
        layout.addWidget(status)

    def _show_branches(self) -> None:
        QMessageBox.information(
            self,
            f"{self.summary.name} · 任务分支",
            "\n".join(f"- {branch}" for branch in self.summary.task_branches),
        )

    def _add_button(
        self,
        layout,
        text: str,
        action: str,
        primary: bool = False,
        danger: bool = False,
    ) -> None:
        button = QPushButton(text)
        if primary:
            button.setProperty("role", "primary")
        elif danger:
            button.setProperty("role", "danger")
        button.clicked.connect(lambda _checked=False, a=action: self.action_requested.emit(self.summary, a))
        layout.addWidget(button)

    def mousePressEvent(self, event) -> None:
        self.selected.emit(self.summary)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        self.activated.emit(self.summary)
        super().mouseDoubleClickEvent(event)


class AggregateProjectTab(QWidget):
    """聚合目录、直属项目和该目录需求工作区的唯一顶层工作台。"""

    def __init__(
        self,
        project: AggregateProject,
        config: AppConfig,
        *,
        workspace: DevelopmentWorkspace | None = None,
        environment_guard=None,
        existing_tabs: dict[str, ProjectTab] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.aggregate_project = project
        self.workspace = workspace
        self.config = config
        self.environment_guard = environment_guard
        self.path = project.root_path
        self.stable_id = project.id
        self.aggregate_root_path = project.root_path
        self.project_meta = SimpleNamespace(
            path=project.root_path,
            name=project.name,
            project_type="aggregate",
            display_type="聚合目录",
            package_manager="",
            default_port=None,
            health_check_path="",
        )
        self._project_tabs: dict[str, ProjectTab] = {}
        self._project_nodes: dict[str, QTreeWidgetItem] = {}
        self._metadata: dict[str, dict] = {}
        self._existing_tabs = existing_tabs or {}
        self._configured_existing_tabs: dict[str, ProjectTab] = {}
        self._workspace_summaries = ()
        self._workspace_cards: dict[str, _WorkspaceCard] = {}
        self._selected_workspace = None
        self._pending_workspace_path = ""
        self._pending_component_id = ""
        self._prepare_worker: _PrepareProjectsWorker | None = None
        self._prepare_generation = 0
        self._save_worker: _SaveProjectWorker | None = None
        self._workspace_worker: _WorkspaceSnapshotWorker | None = None
        self._create_worker: _CreateWorkspaceWorker | None = None
        self._merge_worker: _MergeWorkspaceWorker | None = None
        self._sync_push_worker: _SyncPushWorkspaceWorker | None = None
        self._delete_preflight_worker: _DeletePreflightWorker | None = None
        self._delete_worker: _DeleteWorkspaceWorker | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(GAP_MD, GAP_MD, GAP_MD, GAP_MD)
        root.setSpacing(GAP_SM)

        header = QHBoxLayout()
        header.setSpacing(GAP_SM)
        self.title_label = QLabel(project.name)
        self.title_label.setProperty("role", "title")
        header.addWidget(self.title_label)
        self.context_label = QLabel()
        self.context_label.setProperty("role", "subtitle")
        header.addWidget(self.context_label)
        self.exit_workspace_button = QPushButton("退出工作区")
        self.exit_workspace_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowBack)
        )
        self.exit_workspace_button.setToolTip("退出当前需求工作区，返回聚合目录")
        self.exit_workspace_button.clicked.connect(
            lambda: self.activate_workspace(None)
        )
        header.addWidget(self.exit_workspace_button)
        header.addStretch(1)
        self.edit_button = QPushButton("编辑项目列表")
        self.edit_button.clicked.connect(self.edit_project_definition)
        header.addWidget(self.edit_button)
        root.addLayout(header)

        self.view_tabs = QTabWidget()
        self.view_tabs.setObjectName("aggregate_tabs")
        self.view_tabs.setDocumentMode(True)
        self.projects_page = self._build_projects_page()
        self.workspaces_page = self._build_workspaces_page()
        self.view_tabs.addTab(self.projects_page, "项目")
        self.view_tabs.addTab(self.workspaces_page, "需求工作区")
        root.addWidget(self.view_tabs, 1)

        self._set_context_label()
        self._start_prepare()
        self.refresh_workspaces()
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(2000)
        self._refresh_timer.timeout.connect(self.refresh_project_states)
        self._refresh_timer.start()

    def _build_projects_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(GAP_NONE, GAP_SM, GAP_NONE, GAP_NONE)
        layout.setSpacing(GAP_SM)
        self.project_progress = QLabel("正在识别项目...")
        self.project_progress.setProperty("role", "hint")
        progress_row = QHBoxLayout()
        progress_row.setSpacing(GAP_SM)
        progress_row.addWidget(self.project_progress, 1)
        self.project_codex_button = QPushButton("codex")
        self.project_codex_button.setObjectName("external_tool_button")
        self.project_codex_button.setMinimumWidth(58)
        self.project_codex_button.setToolTip("使用 open gpt 打开当前代码环境")
        self.project_codex_button.clicked.connect(self._open_current_in_codex)
        progress_row.addWidget(self.project_codex_button)
        self.project_cc_button = QPushButton("cc")
        self.project_cc_button.setObjectName("external_tool_button")
        self.project_cc_button.setMinimumWidth(42)
        self.project_cc_button.setToolTip("在 AgentDesk 中打开当前代码环境")
        self.project_cc_button.clicked.connect(self._open_current_in_cc)
        progress_row.addWidget(self.project_cc_button)
        layout.addLayout(progress_row)

        self.project_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.project_splitter.setChildrenCollapsible(False)
        self.project_splitter.setHandleWidth(3)

        sidebar = QFrame()
        sidebar.setObjectName("aggregate_project_sidebar")
        sidebar.setMinimumWidth(220)
        sidebar.setMaximumWidth(300)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(GAP_MD, GAP_MD, GAP_MD, GAP_MD)
        sidebar_layout.setSpacing(GAP_SM)
        self.project_tree_label = QLabel("项目")
        self.project_tree_label.setProperty("role", "title")
        sidebar_layout.addWidget(self.project_tree_label)
        self.project_tree = QTreeWidget()
        self.project_tree.setObjectName("aggregate_project_tree")
        self.project_tree.setColumnCount(2)
        self.project_tree.setHeaderHidden(True)
        self.project_tree.setRootIsDecorated(False)
        self.project_tree.setUniformRowHeights(True)
        self.project_tree.setIndentation(0)
        self.project_tree.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.project_tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tree_header = self.project_tree.header()
        tree_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        tree_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.project_tree.itemSelectionChanged.connect(self._show_selected_project)
        sidebar_layout.addWidget(self.project_tree, 1)
        self.project_splitter.addWidget(sidebar)

        detail = QFrame()
        detail.setObjectName("aggregate_project_detail")
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(GAP_NONE, GAP_NONE, GAP_NONE, GAP_NONE)
        detail_layout.setSpacing(GAP_NONE)
        self.detail_stack = QStackedWidget()
        self.placeholder = QLabel(
            "项目加载完成后，在上方选择项目查看日志、文件和 Git。"
        )
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setStyleSheet(f"color: {FG_DIM};")
        self.detail_stack.addWidget(self.placeholder)
        detail_layout.addWidget(self.detail_stack)
        self.project_splitter.addWidget(detail)
        self.project_splitter.setStretchFactor(0, 0)
        self.project_splitter.setStretchFactor(1, 1)
        try:
            sidebar_width = int(self.config.aggregate_sidebar_width)
        except (TypeError, ValueError):
            sidebar_width = 280
        sidebar_width = max(220, min(300, sidebar_width))
        self.project_splitter.setSizes([sidebar_width, 1000])
        self._sidebar_save_timer = QTimer(self)
        self._sidebar_save_timer.setSingleShot(True)
        self._sidebar_save_timer.setInterval(350)
        self._sidebar_save_timer.timeout.connect(self._save_sidebar_width)
        self.project_splitter.splitterMoved.connect(self._sidebar_moved)
        layout.addWidget(self.project_splitter, 1)
        return page

    def _build_workspaces_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(GAP_NONE, GAP_MD, GAP_NONE, GAP_NONE)
        layout.setSpacing(GAP_MD)

        actions = QHBoxLayout()
        actions.setSpacing(GAP_SM)
        self.new_workspace_button = QPushButton("新建需求工作区")
        self.new_workspace_button.setProperty("role", "primary")
        self.new_workspace_button.clicked.connect(self._create_workspace)
        actions.addWidget(self.new_workspace_button)
        actions.addStretch(1)
        self.refresh_workspace_button = QToolButton()
        self.refresh_workspace_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload)
        )
        self.refresh_workspace_button.setToolTip("刷新需求工作区")
        self.refresh_workspace_button.clicked.connect(self.refresh_workspaces)
        actions.addWidget(self.refresh_workspace_button)
        layout.addLayout(actions)

        self.workspace_status = QLabel("正在读取需求工作区...")
        self.workspace_status.setProperty("role", "subtitle")
        layout.addWidget(self.workspace_status)
        self.workspace_scroll = QScrollArea()
        self.workspace_scroll.setObjectName("workspace_scroll")
        self.workspace_scroll.setWidgetResizable(True)
        self.workspace_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.workspace_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.workspace_cards_host = QWidget()
        self.workspace_cards_layout = QVBoxLayout(self.workspace_cards_host)
        self.workspace_cards_layout.setContentsMargins(0, 2, 0, 2)
        self.workspace_cards_layout.setSpacing(12)
        self.workspace_cards_layout.addStretch(1)
        self.workspace_scroll.setWidget(self.workspace_cards_host)
        layout.addWidget(self.workspace_scroll, 1)
        self._update_workspace_buttons()
        return page

    @property
    def component_tabs(self) -> dict[str, ProjectTab]:
        """兼容现有 CLI 的命名；界面统一称为“项目”。"""
        return dict(self._project_tabs)

    def _project_items(self) -> tuple[dict, ...]:
        if self.workspace is not None:
            workspace_map = {item.id.casefold(): item for item in self.workspace.components}
            items = []
            for definition in self.aggregate_project.components:
                workspace_item = workspace_map.get(definition.id.casefold())
                if workspace_item is None:
                    continue
                is_worktree = workspace_item.mode == "worktree"
                items.append({
                    "id": definition.id,
                    "name": definition.name or definition.id,
                    "path": (
                        workspace_item.worktree_path
                        or workspace_item.source_repository_path
                    ),
                    "shared": not is_worktree,
                    "branch": workspace_item.task_branch if is_worktree else "源目录",
                    "runtime": definition.runtime,
                })
            return tuple(items)
        return tuple({
            "id": item.id,
            "name": item.name or item.id,
            "path": str(item.absolute_path(self.aggregate_project.root_path)),
            "shared": item.shared,
            "branch": "共享目录" if item.shared else "",
            "runtime": item.runtime,
        } for item in self.aggregate_project.components)

    def _start_prepare(self) -> None:
        self._prepare_generation += 1
        generation = self._prepare_generation
        self.project_progress.setText("正在识别项目...")
        pending = []
        for item in self._project_items():
            path_key = normalized_path_key(item["path"])
            existing = self._existing_tabs.get(path_key)
            if existing is not None and not item.get("runtime"):
                self._install_project({
                    **item,
                    "meta": existing.project_meta,
                    "branch": item.get("branch") or "Git 仓库",
                    "error": "",
                }, existing)
            else:
                pending.append(item)
                if existing is not None:
                    self._configured_existing_tabs[path_key] = existing
        self._existing_tabs = {}
        if not pending:
            self._prepare_worker = None
            self._projects_prepared([], generation)
            return
        worker = _PrepareProjectsWorker(
            tuple(pending), generation, QApplication.instance(),
        )
        self._prepare_worker = worker
        worker.done.connect(self._projects_prepared)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _projects_prepared(self, results: list[dict], generation: int) -> None:
        if generation != self._prepare_generation:
            return
        self._prepare_worker = None
        for item in results:
            existing = self._configured_existing_tabs.pop(
                normalized_path_key(item["path"]), None,
            )
            if existing is not None:
                if not existing.request_close(
                    confirm_running=False, stop_running=True, quiet=True,
                ):
                    self._install_project({
                        **item,
                        "meta": existing.project_meta,
                        "error": "旧项目仍在运行，无法应用聚合运行配置",
                    }, existing)
                    continue
                existing.deleteLater()
            self._install_project(item)
        ready = len(self._project_tabs)
        errors = sum(1 for item in self._metadata.values() if item.get("error"))
        text = f"{ready} 个项目可用"
        if errors:
            text += f"，{errors} 个错误"
        self.project_progress.setText(text)
        self.project_tree_label.setText(f"项目 {self.project_tree.topLevelItemCount()}")
        pending_key = self._pending_component_id.casefold()
        pending_node = next((
            node for project_id, node in self._project_nodes.items()
            if project_id.casefold() == pending_key
        ), None) if pending_key else None
        if pending_node is not None:
            self.project_tree.setCurrentItem(pending_node)
            self._pending_component_id = ""
        elif self.project_tree.topLevelItemCount() and not self.project_tree.selectedItems():
            self.project_tree.setCurrentItem(self.project_tree.topLevelItem(0))
        self.refresh_project_states()

    def _install_project(self, item: dict, existing: ProjectTab | None = None) -> None:
        project_id = item["id"]
        self._metadata[project_id] = item
        meta = item.get("meta")
        node = QTreeWidgetItem([
            item.get("name") or project_id,
            "配置错误" if item.get("error") else "已停止",
        ])
        node.setData(0, Qt.ItemDataRole.UserRole, project_id)
        node.setToolTip(0, item.get("error", "") or (
            f"{meta.display_type if meta else '识别失败'} · "
            f"{item.get('branch') or ('共享目录' if item.get('shared') else '-') }"
        ))
        self.project_tree.addTopLevelItem(node)
        self._project_nodes[project_id] = node
        if meta is None:
            return
        runtime_count = len(meta.runtime_units)
        runtime_text = f" · {runtime_count} 个运行单元" if runtime_count > 1 else ""
        node.setToolTip(
            0,
            f"{meta.display_type}{runtime_text}\n"
            f"{item.get('branch') or ('共享目录' if item.get('shared') else item['path'])}",
        )
        tab = existing
        if tab is None:
            entry = self.config.find_project(item["path"]) or ProjectEntry(path=item["path"])
            entry.name = meta.name
            entry.project_type = meta.project_type
            tab = ProjectTab(meta, entry, self.config)
            tab.path = item["path"]
        tab.component_id = project_id
        self._project_tabs[project_id] = tab
        self.detail_stack.addWidget(tab)

    def _show_selected_project(self) -> None:
        items = self.project_tree.selectedItems()
        if not items:
            return
        item = items[0]
        project_id = item.data(0, Qt.ItemDataRole.UserRole) or ""
        tab = self._project_tabs.get(project_id)
        if tab is not None:
            self.detail_stack.setCurrentWidget(tab)

    def _sidebar_moved(self, position: int, _index: int) -> None:
        self._sidebar_save_timer.start()

    def _save_sidebar_width(self) -> None:
        width = self.project_splitter.sizes()[0] if self.project_splitter.sizes() else 280
        self.config.aggregate_sidebar_width = max(220, min(300, int(width)))
        self.config.save()

    def show_projects(self) -> None:
        self.view_tabs.setCurrentWidget(self.projects_page)

    def select_component(self, component_id: str) -> bool:
        """选中聚合内部项目；识别尚未完成时延迟到本轮结果安装后执行。"""
        key = str(component_id or "").strip().casefold()
        if not key:
            return False
        actual_id = next((
            item["id"] for item in self._project_items()
            if item["id"].casefold() == key
        ), "")
        if not actual_id:
            return False
        self.show_projects()
        node = self._project_nodes.get(actual_id)
        if node is not None:
            self.project_tree.setCurrentItem(node)
            self._pending_component_id = ""
        else:
            self._pending_component_id = actual_id
        return True

    def show_workspaces(self) -> None:
        self.view_tabs.setCurrentWidget(self.workspaces_page)

    def refresh_theme(self) -> None:
        self.refresh_project_states()

    def _set_context_label(self) -> None:
        if self.workspace is None:
            self.context_label.setText(f"源目录：{self.aggregate_project.root_path}")
        else:
            self.context_label.setText(f"当前需求：{self.workspace.name}")
        self.exit_workspace_button.setVisible(self.workspace is not None)

    def activate_workspace(
        self, workspace: DevelopmentWorkspace | None, *, interactive: bool = True,
    ) -> bool:
        old_path = self.workspace.root_path if self.workspace else ""
        new_path = workspace.root_path if workspace else ""
        if old_path and new_path and normalized_path_key(old_path) == normalized_path_key(new_path):
            self.show_projects()
            return True
        if not old_path and not new_path:
            return True

        running = self.running_service_items(refresh_external=True)
        if running:
            if not interactive:
                return False
            answer = QMessageBox.question(
                self,
                "切换代码目录",
                "当前仍有项目在运行。切换需求工作区前需要停止这些项目，是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return False
        for tab in self._project_tabs.values():
            if not tab.request_close(
                confirm_running=False, stop_running=True, quiet=not interactive,
            ):
                return False
        if running and not self.wait_services_stopped(20000):
            QMessageBox.warning(self, "切换失败", "当前项目未能完全停止。")
            return False

        for tab in self._project_tabs.values():
            self.detail_stack.removeWidget(tab)
            tab.deleteLater()
        self._project_tabs.clear()
        self._project_nodes.clear()
        self._metadata.clear()
        self.project_tree.clear()
        self.project_tree_label.setText("项目")
        self.detail_stack.setCurrentWidget(self.placeholder)
        self.workspace = workspace
        self._set_context_label()
        self._start_prepare()
        self._update_workspace_buttons()
        self.show_projects()
        return True

    def _selected_workspace_summary(self):
        return self._selected_workspace

    def _workspace_busy(self) -> bool:
        return bool(
            self._workspace_worker or self._create_worker or self._merge_worker
            or self._sync_push_worker or self._delete_preflight_worker
            or self._delete_worker
        )

    def _update_workspace_buttons(self) -> None:
        summary = self._selected_workspace_summary()
        busy = self._workspace_busy()
        for card in self._workspace_cards.values():
            card.setEnabled(not busy and card.summary.workspace is not None)
        self.new_workspace_button.setEnabled(not busy)
        self.refresh_workspace_button.setEnabled(not busy)
        self.exit_workspace_button.setEnabled(self.workspace is not None and not busy)

    def refresh_workspaces(self) -> None:
        if self._workspace_worker:
            return
        self.workspace_status.setText("正在读取需求工作区...")
        worker = _WorkspaceSnapshotWorker(
            self.aggregate_project.root_path, QApplication.instance(),
        )
        self._workspace_worker = worker
        self._update_workspace_buttons()
        worker.done.connect(self._workspaces_ready)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _workspaces_ready(self, summaries, error: str) -> None:
        if self.sender() is not self._workspace_worker:
            return
        self._workspace_worker = None
        self._workspace_summaries = tuple(summaries)
        while self.workspace_cards_layout.count() > 1:
            item = self.workspace_cards_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        self._workspace_cards.clear()
        self._selected_workspace = None
        selected_row = -1
        for row, summary in enumerate(summaries):
            current = bool(self.workspace and normalized_path_key(self.workspace.root_path) == normalized_path_key(summary.root_path))
            card = _WorkspaceCard(summary, current, self.workspace_cards_host)
            if summary.error:
                card.setToolTip(summary.error)
            card.selected.connect(self._select_workspace_card)
            card.activated.connect(self._activate_workspace_summary)
            card.action_requested.connect(self._workspace_card_action)
            self.workspace_cards_layout.insertWidget(self.workspace_cards_layout.count() - 1, card)
            self._workspace_cards[normalized_path_key(summary.root_path)] = card
            if self._pending_workspace_path and normalized_path_key(summary.root_path) == normalized_path_key(
                self._pending_workspace_path
            ):
                selected_row = row
        self._pending_workspace_path = ""
        if error:
            self.workspace_status.setText(f"读取失败：{error}")
        else:
            self.workspace_status.setText(f"{len(summaries)} 个需求工作区")
        if summaries:
            self._select_workspace_card(summaries[selected_row if selected_row >= 0 else 0])
        self._update_workspace_buttons()

    def _select_workspace_card(self, summary) -> None:
        self._selected_workspace = summary
        for key, card in self._workspace_cards.items():
            card.setProperty("selected", card.summary is summary)
            card.style().unpolish(card)
            card.style().polish(card)
        self._update_workspace_buttons()

    def _activate_workspace_summary(self, summary) -> None:
        self._select_workspace_card(summary)
        self._activate_selected_workspace()

    def _workspace_card_action(self, summary, action: str) -> None:
        self._select_workspace_card(summary)
        handlers = {
            "activate": self._activate_selected_workspace,
            "codex": self._open_selected_in_codex,
            "cc": self._open_selected_in_cc,
            "sync-push": self._sync_push_selected_workspace,
            "merge": self._merge_selected_workspace,
            "delete": self._delete_selected_workspace,
        }
        handler = handlers.get(action)
        if handler:
            handler()

    def _activate_selected_workspace(self) -> None:
        summary = self._selected_workspace_summary()
        if summary and summary.workspace is not None:
            action_log.info("[GUI] 进入工作区 aggregate=%s workspace=%s",
                            self.aggregate_project.name, summary.name)
            self.activate_workspace(summary.workspace)

    def _create_workspace(self) -> None:
        if self._workspace_busy():
            return
        dialog = DevelopmentWorkspaceDialog(self.aggregate_project, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        plan = dialog.result_plan()
        if plan is None:
            return
        self.workspace_status.setText("正在创建所选项目的 Worktree...")
        worker = _CreateWorkspaceWorker(plan, QApplication.instance())
        self._create_worker = worker
        self._update_workspace_buttons()
        worker.done.connect(self._workspace_created)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _workspace_created(self, result) -> None:
        if self.sender() is not self._create_worker:
            return
        self._create_worker = None
        if not result.ok or result.workspace is None:
            action_log.warning("[GUI] 创建工作区失败 error=%s", result.error)
            details = [result.error]
            if result.created_components:
                details.append("已创建：" + ", ".join(result.created_components))
            if result.rolled_back_components:
                details.append("已安全回滚：" + ", ".join(result.rolled_back_components))
            if result.pending_components:
                details.append("未处理：" + ", ".join(result.pending_components))
            QMessageBox.warning(self, "创建需求工作区失败", "\n".join(details))
        else:
            action_log.info("[GUI] 创建工作区成功 workspace=%s", result.workspace.root_path)
            self._pending_workspace_path = result.workspace.root_path
            self.workspace_status.setText("需求工作区已创建。")
        self._update_workspace_buttons()
        self.refresh_workspaces()

    def _current_environment_root(self) -> Path:
        return Path(
            self.workspace.root_path
            if self.workspace is not None
            else self.aggregate_project.root_path
        )

    def _open_external_tool(self, target: Path, command: list[str], tool_name: str = "AgentDesk") -> None:
        if not command:
            QMessageBox.warning(
                self, "启动 AgentDesk 失败",
                "没有找到 AgentDesk，请先安装或配置 AGENTDESK_EXE。",
            )
            return
        try:
            subprocess.Popen(
                command, cwd=str(target), creationflags=CREATE_NO_WINDOW, close_fds=True,
            )
        except OSError as exc:
            QMessageBox.warning(self, f"启动 {tool_name} 失败", str(exc))

    def _open_current_in_codex(self) -> None:
        target = self._current_environment_root()
        self._open_external_tool(target, open_in_codex_args(target), "open gpt")

    def _open_current_in_cc(self) -> None:
        target = self._current_environment_root()
        self._open_external_tool(target, open_in_cc_args(target))

    def _open_selected_in_codex(self) -> None:
        summary = self._selected_workspace_summary()
        if summary and summary.workspace is not None:
            target = Path(summary.workspace.root_path)
            self._open_external_tool(target, open_in_codex_args(target), "open gpt")

    def _open_selected_in_cc(self) -> None:
        summary = self._selected_workspace_summary()
        if summary and summary.workspace is not None:
            target = Path(summary.workspace.root_path)
            self._open_external_tool(target, open_in_cc_args(target))

    def _sync_push_selected_workspace(self) -> None:
        summary = self._selected_workspace_summary()
        if not summary or summary.workspace is None or self._workspace_busy():
            return
        editable = [
            item for item in summary.workspace.components if item.mode == "worktree"
        ]
        commit_message = summary.workspace.name.strip() or summary.workspace.id
        lines = ["将同步并推送以下任务分支："]
        lines.extend(
            f"- {item.id}: {item.base_branch} -> {item.task_branch}"
            for item in editable
        )
        lines.append(
            f"\n将先用“{commit_message}”提交工作区本地改动（暂不推送），"
            "再从远端获取源分支更新并合并进任务分支；"
            "全部同步成功后才推送到 origin。"
            "同步冲突会回滚合并并停止推送，本地提交会保留。确认继续？"
        )
        box = QMessageBox(self)
        box.setWindowTitle("确认同步并推送")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("\n".join(lines))
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.exec()
        if box.standardButton(box.clickedButton()) != QMessageBox.StandardButton.Yes:
            return
        self._start_workspace_sync_push(
            summary.workspace,
            commit_message,
            keep_conflicts=False,
        )

    def _start_workspace_sync_push(
        self,
        workspace: DevelopmentWorkspace,
        commit_message: str,
        *,
        keep_conflicts: bool,
    ) -> None:
        action_log.info(
            "[GUI] 同步并推送工作区 aggregate=%s workspace=%s "
            "fetch_remote=true keep_conflicts=%s",
            self.aggregate_project.name, workspace.name, keep_conflicts,
        )
        self.workspace_status.setText("正在提交本地改动、同步源分支并推送...")
        worker = _SyncPushWorkspaceWorker(
            workspace,
            commit_message,
            keep_conflicts=keep_conflicts,
            parent=QApplication.instance(),
        )
        self._sync_push_worker = worker
        self._update_workspace_buttons()
        worker.done.connect(self._workspace_sync_push_ready)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    @staticmethod
    def _conflict_lines(items) -> list[str]:
        lines = []
        for item in items:
            files = "、".join(item.conflict_files[:5])
            if len(item.conflict_files) > 5:
                files += f" 等 {len(item.conflict_files)} 个文件"
            lines.append(f"- {item.id}: {files or '有冲突'}")
        return lines

    def _workspace_sync_push_ready(self, result, error: str) -> None:
        if self.sender() is not self._sync_push_worker:
            return
        self._sync_push_worker = None
        self._update_workspace_buttons()
        if error or result is None:
            message = error or "未知错误"
            action_log.warning("[GUI] 同步并推送工作区失败 error=%s", message)
            QMessageBox.warning(self, "同步并推送失败", message)
            self.refresh_workspaces()
            return

        committed = [item.id for item in result.components if item.committed]
        synced = [item.id for item in result.components if item.synced]
        current = [item.id for item in result.components if item.already_current]
        pushed = [item.id for item in result.components if item.pushed]
        others = [
            f"- {item.id}: {item.error}"
            for item in result.components
            if item.error and not item.conflicted
        ]
        if result.needs_conflict_confirmation:
            conflicted = [
                item for item in result.components
                if item.conflicted and item.rolled_back
            ]
            lines = ["以下项目同步时有冲突，已回滚到同步前状态："]
            lines.extend(self._conflict_lines(conflicted))
            if synced:
                lines.append(f"\n已成功同步：{'、'.join(synced)}")
            if committed:
                lines.append(f"\n已保留本地提交：{'、'.join(committed)}")
            if others:
                lines.append("\n其它未同步项目：")
                lines.extend(others)
            lines.append(
                "\n本次没有推送。是否重新同步并把冲突保留在工作区，"
                "由你或 AI 手工解决？"
                "\n解决冲突后再次点击“同步并推送”即可完成提交和推送。"
            )
            answer = QMessageBox.question(
                self, "同步并推送存在冲突", "\n".join(lines),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                commit_message = result.workspace.name.strip() or result.workspace.id
                self._start_workspace_sync_push(
                    result.workspace,
                    commit_message,
                    keep_conflicts=True,
                )
                return
            action_log.warning(
                "[GUI] 同步并推送存在冲突并已回滚 projects=%s",
                "、".join(item.id for item in conflicted),
            )
            self.workspace_status.setText("同步存在冲突，已回滚且未推送。")
            self.refresh_workspaces()
            return

        if not result.ok:
            kept = [
                item for item in result.components
                if item.conflicted and not item.rolled_back
            ]
            blocks = []
            if committed:
                blocks.append("已保留本地提交：" + "、".join(committed))
            if synced:
                blocks.append("已同步：" + "、".join(synced))
            if kept:
                blocks.append(
                    "冲突已保留，尚未推送：\n"
                    + "\n".join(self._conflict_lines(kept))
                )
            if pushed:
                blocks.append("已推送：" + "、".join(pushed))
            if others:
                blocks.append("未完成项目：\n" + "\n".join(others))
            if result.error and not kept:
                blocks.append(result.error)
            action_log.warning("[GUI] 同步并推送未完成 error=%s", result.error)
            QMessageBox.warning(
                self, "同步并推送未完成", "\n\n".join(blocks) or result.error,
            )
            self.refresh_workspaces()
            return

        blocks = []
        if committed:
            blocks.append("已提交本地改动：" + "、".join(committed))
        if synced:
            blocks.append("已同步源分支：" + "、".join(synced))
        if current:
            blocks.append("已是最新：" + "、".join(current))
        blocks.append("已推送：" + "、".join(pushed))
        action_log.info(
            "[GUI] 同步并推送工作区成功 synced=%s pushed=%s",
            "、".join(synced) or "无", "、".join(pushed) or "无",
        )
        QMessageBox.information(
            self, "同步并推送完成", "\n\n".join(blocks),
        )
        self.refresh_workspaces()

    def _merge_selected_workspace(self) -> None:
        summary = self._selected_workspace_summary()
        if not summary or summary.workspace is None or self._workspace_busy():
            return
        editable = [
            item for item in summary.workspace.components if item.mode == "worktree"
        ]
        lines = ["将把以下任务分支快进合并到基准分支，并推送基准分支上游："]
        lines.extend(
            f"- {item.id}: {item.task_branch} -> {item.base_branch}"
            for item in editable
        )
        commit_message = summary.workspace.name.strip() or summary.workspace.id
        lines.append(
            f"\n将先自动提交各工作区的全部改动，提交信息为“{commit_message}”，"
            "再合并到基准分支并推送。源目录必须无未提交改动，"
            "分支分叉时不会自动合并；基准分支上已有的未推送提交也会一并推送。"
            "确认继续？"
        )
        answer = QMessageBox.question(
            self,
            "确认合并代码",
            "\n".join(lines),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.workspace_status.setText("正在提交、合并并推送代码...")
        worker = _MergeWorkspaceWorker(summary.workspace, QApplication.instance())
        self._merge_worker = worker
        self._update_workspace_buttons()
        worker.done.connect(self._workspace_merge_ready)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _workspace_merge_ready(self, result, error: str) -> None:
        if self.sender() is not self._merge_worker:
            return
        self._merge_worker = None
        self._update_workspace_buttons()
        if error or result is None or not result.ok:
            message = error or (result.error if result is not None else "未知错误")
            if result is not None and result.components:
                details = [
                    f"- {item.id}: {item.error}"
                    for item in result.components if item.error
                ]
                pushed = [item.id for item in result.components if item.pushed]
                if pushed:
                    details.insert(0, "已推送基准分支：" + "、".join(pushed))
                if details:
                    message = message + "\n\n" + "\n".join(details)
            action_log.warning("[GUI] 合并工作区失败 error=%s", message)
            QMessageBox.warning(self, "合并并推送失败", message)
        else:
            projects = "、".join(item.id for item in result.components if item.merged)
            action_log.info("[GUI] 合并并推送工作区成功 projects=%s", projects)
            QMessageBox.information(
                self, "合并并推送完成",
                f"已合并并推送基准分支：{projects}",
            )
        self.refresh_workspaces()

    def _running_project_paths(self) -> tuple[str, ...]:
        paths = []
        for tab in self._project_tabs.values():
            if tab.running_service_items(refresh_external=True):
                paths.append(tab.project_meta.path)
        return tuple(paths)

    def _delete_selected_workspace(self) -> None:
        summary = self._selected_workspace_summary()
        if not summary or summary.workspace is None or self._workspace_busy():
            return
        self.workspace_status.setText("正在执行删除预检...")
        worker = _DeletePreflightWorker(
            summary.workspace, self._running_project_paths(), QApplication.instance(),
        )
        self._delete_preflight_worker = worker
        self._update_workspace_buttons()
        worker.done.connect(self._delete_preflight_ready)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _delete_preflight_ready(self, plan, error: str) -> None:
        if self.sender() is not self._delete_preflight_worker:
            return
        self._delete_preflight_worker = None
        self._update_workspace_buttons()
        if error or plan is None:
            QMessageBox.warning(self, "删除预检失败", error or "未知错误")
            self.refresh_workspaces()
            return
        if plan.blockers:
            details = "\n".join(f"- {item}" for item in plan.blockers)
            QMessageBox.warning(self, "当前不能删除", details)
            self.refresh_workspaces()
            return
        lines = ["已确认以下项目的任务分支已合并，基准分支已推送远程："]
        for item in plan.components:
            lines.append(
                f"- {item.id}: {item.task_branch} → {item.base_branch} "
                f"→ {item.base_remote_ref}"
            )
        lines.extend([
            "",
            "继续后将分别清理以下内容，一项失败不阻止其他项：",
            f"1. 递归删除整个工作区目录：{plan.workspace.root_path}",
            "2. 删除为该工作区创建的本地任务分支",
            "3. 删除远程仓库中的同名任务分支",
            "",
            "目录内所有文件都会删除且无法恢复。确认继续？",
        ])
        answer = QMessageBox.question(
            self,
            "确认删除需求工作区",
            "\n".join(lines),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self.refresh_workspaces()
            return
        if self.workspace and normalized_path_key(self.workspace.root_path) == normalized_path_key(
            plan.workspace.root_path
        ):
            if not self.activate_workspace(None):
                QMessageBox.warning(self, "删除已取消", "当前工作区未能安全退出。")
                return
        worker = _DeleteWorkspaceWorker(
            plan, plan.needs_unmerged_confirmation, QApplication.instance(),
        )
        self._delete_worker = worker
        self._update_workspace_buttons()
        self.workspace_status.setText("正在删除工作区目录和任务分支...")
        worker.done.connect(self._workspace_deleted)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _workspace_deleted(self, ok: bool, kept_branches, error: str) -> None:
        if self.sender() is not self._delete_worker:
            return
        self._delete_worker = None
        self._update_workspace_buttons()
        if not ok:
            action_log.warning("[GUI] 删除工作区失败 error=%s", error)
            QMessageBox.warning(self, "删除未完成", error)
        else:
            action_log.info("[GUI] 删除工作区成功 kept_branches=%s", kept_branches)
            message = "需求工作区目录、本地任务分支和远程任务分支均已删除。"
            QMessageBox.information(self, "删除完成", message)
        self.refresh_workspaces()

    def edit_project_definition(self) -> None:
        if self._save_worker or self._prepare_worker:
            return
        if self.workspace is not None and not self.activate_workspace(None):
            return
        dialog = AggregateProjectDialog(
            self.aggregate_project,
            title=f"编辑 {self.aggregate_project.name} 的项目列表",
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        project = dialog.result_project()
        if project is None:
            return
        self.edit_button.setEnabled(False)
        worker = _SaveProjectWorker(project, QApplication.instance())
        self._save_worker = worker
        worker.done.connect(self._project_definition_saved)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _project_definition_saved(self, ok: bool, result: str) -> None:
        if self.sender() is not self._save_worker:
            return
        project = self._save_worker.project
        self._save_worker = None
        self.edit_button.setEnabled(True)
        if not ok:
            QMessageBox.warning(self, "保存项目列表失败", result)
            return
        self.aggregate_project = project
        self.stable_id = project.id
        self.project_meta.name = project.name
        self.title_label.setText(project.name)
        parent = self.parentWidget()
        if isinstance(parent, QTabWidget):
            index = parent.indexOf(self)
            if index >= 0:
                parent.setTabText(index, project.name)
        entry = self.config.find_project(project.root_path) or ProjectEntry(
            path=project.root_path,
        )
        entry.name = project.name
        entry.project_type = "aggregate"
        self.config.touch_project(entry)
        self.config.register_aggregate_project(project.root_path)
        self.config.save()
        self._rebuild_projects()
        self.refresh_workspaces()

    def _rebuild_projects(self) -> None:
        for tab in self._project_tabs.values():
            tab.request_close(confirm_running=False, stop_running=True, quiet=True)
            self.detail_stack.removeWidget(tab)
            tab.deleteLater()
        self._project_tabs.clear()
        self._project_nodes.clear()
        self._metadata.clear()
        self.project_tree.clear()
        self.project_tree_label.setText("项目")
        self.detail_stack.setCurrentWidget(self.placeholder)
        self._start_prepare()

    def start_component(self, component_id: str) -> dict:
        tab = self._project_tabs.get(component_id)
        if tab is None:
            return {"ok": False, "error": f"project not available: {component_id}"}
        states = [item for item in tab.service_states(refresh_external=True) if item.kind != "script"]
        if states and all(item.is_running for item in states):
            return {"ok": True, "already_running": True}
        result = tab.service_controller.start()
        self.refresh_project_states()
        return result

    def start_runtime(self, component_id: str, runtime_id: str | None) -> dict:
        if runtime_id is None:
            return self.start_component(component_id)
        tab = self._project_tabs.get(component_id)
        if tab is None:
            return {"ok": False, "error": f"project not available: {component_id}"}
        result = tab.service_controller.start(runtime_id)
        self.refresh_project_states()
        return result

    def stop_component(self, component_id: str) -> dict:
        tab = self._project_tabs.get(component_id)
        if tab is None:
            return {"ok": False, "error": f"project not available: {component_id}"}
        if not tab.running_service_items(refresh_external=True):
            return {"ok": True, "already_stopped": True}
        result = tab.service_controller.stop()
        self.refresh_project_states()
        return result

    def stop_runtime(self, component_id: str, runtime_id: str | None) -> dict:
        if runtime_id is None:
            return self.stop_component(component_id)
        tab = self._project_tabs.get(component_id)
        if tab is None:
            return {"ok": False, "error": f"project not available: {component_id}"}
        states = tab.service_states(refresh_external=True)
        if not any(item.module == runtime_id and item.is_active for item in states):
            return {"ok": True, "already_stopped": True}
        result = tab.service_controller.stop(runtime_id)
        self.refresh_project_states()
        return result

    def refresh_project_states(self) -> None:
        for project_id, tab in self._project_tabs.items():
            node = self._project_nodes.get(project_id)
            if node is None:
                continue
            try:
                states = [
                    item for item in tab.service_states(refresh_external=False)
                    if item.kind != "script"
                ]
            except Exception:
                states = []
            unknown = [item for item in states if item.state == STATE_UNKNOWN]
            external = [item for item in states if item.state == STATE_RUNNING_EXTERNAL]
            changing = [
                item for item in states if item.state in (STATE_STARTING, STATE_STOPPING)
            ]
            running_count = sum(1 for item in states if item.is_running)
            total = len(getattr(tab.project_meta, "runtime_units", ())) or len(states)
            if unknown:
                status, color = "状态不确定", COLOR_ERROR
            elif changing:
                status, color = "处理中", COLOR_WARN
            elif total and running_count == total and external:
                status, color = "外部运行", COLOR_WARN
            elif total and running_count == total:
                status, color = "运行中", COLOR_SUCCESS
            elif running_count:
                status, color = f"{running_count}/{total} 运行中", COLOR_SUCCESS
            else:
                status, color = "已停止", FG_DIM
            node.setText(1, status)
            node.setForeground(1, QColor(str(color)))
            node.setToolTip(1, "\n".join(item.reason for item in states if item.reason))


    def service_states(self, refresh_external: bool = True):
        states = []
        for tab in self._project_tabs.values():
            states.extend(tab.service_states(refresh_external=refresh_external))
        return states

    def running_service_items(self, refresh_external: bool = True) -> list[dict]:
        return [
            item.to_running_item()
            for item in self.service_states(refresh_external)
            if item.is_active
        ]

    def stop_all_services(self, include_external: bool = True, silent: bool = True) -> int:
        return sum(
            tab.stop_all_services(include_external=include_external, silent=silent)
            for tab in self._project_tabs.values()
        )

    def wait_services_stopped(self, timeout_ms: int = 12000) -> bool:
        deadline = time.time() + timeout_ms / 1000.0
        for tab in self._project_tabs.values():
            remaining = max(0, int((deadline - time.time()) * 1000))
            if remaining <= 0 or not tab.wait_services_stopped(remaining):
                return False
        return True

    def runtime_paths(self) -> tuple[str, ...]:
        return tuple(tab.project_meta.path for tab in self._project_tabs.values())

    def request_close(
        self, confirm_running: bool = True, stop_running: bool = True, quiet: bool = False,
    ) -> bool:
        running = self.running_service_items(refresh_external=True)
        if running and confirm_running:
            result = QMessageBox.question(
                self,
                "项目正在运行",
                "关闭会停止该目录内正在运行的项目，确认关闭？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if result != QMessageBox.StandardButton.Yes:
                return False
        for tab in self._project_tabs.values():
            if not tab.request_close(
                confirm_running=False, stop_running=stop_running, quiet=quiet,
            ):
                return False
        self._refresh_timer.stop()
        for worker in (
            self._prepare_worker, self._save_worker, self._workspace_worker,
            self._create_worker, self._merge_worker, self._sync_push_worker,
            self._delete_preflight_worker, self._delete_worker,
        ):
            if worker and worker.isRunning():
                worker.wait(3000)
        return True

    def _current_project_tab(self) -> ProjectTab | None:
        tab = self.detail_stack.currentWidget()
        return tab if isinstance(tab, ProjectTab) else None

    def open_file_picker(self) -> None:
        tab = self._current_project_tab()
        if tab:
            tab.open_file_picker()

    def open_content_search(self) -> None:
        tab = self._current_project_tab()
        if tab:
            tab.open_content_search()

    def open_recent_files(self) -> None:
        tab = self._current_project_tab()
        if tab:
            tab.open_recent_files()

    def open_endpoint_picker(self) -> None:
        tab = self._current_project_tab()
        if tab:
            tab.open_endpoint_picker()

    def restart_project(self) -> None:
        tab = self._current_project_tab()
        if tab:
            tab.restart_project()

    def close_current_file_tab(self) -> None:
        tab = self._current_project_tab()
        if tab:
            tab.close_current_file_tab()
