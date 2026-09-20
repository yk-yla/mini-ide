"""主窗口：Tab 容器 + 菜单栏 + 状态栏 + 空状态切换"""
from __future__ import annotations

import time
from pathlib import Path

import psutil
from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtGui import (
    QAction, QActionGroup, QCloseEvent, QDragEnterEvent, QDropEvent, QIcon,
    QKeySequence, QShortcut,
)
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QLabel, QMainWindow, QMessageBox,
    QMenu, QStackedWidget, QSystemTrayIcon, QTabWidget,
)

from src.core.config import AppConfig, ProjectEntry
from src.core.aggregate_workspace import (
    AggregateProject, aggregate_config_path, load_aggregate_project,
    load_development_workspace, save_aggregate_project,
)
from src.core.aggregate_workspace_manager import (
    scan_aggregate_components, suggest_stable_id,
)
from src.core.project_detector import detect_project
from src.core.path_utils import normalized_path_key
from src.core.target_resolver import (
    TargetResolutionError, resolve_target_path,
)
from src.core.service_state import (
    STATE_RUNNING_EXTERNAL, STATE_RUNNING_MANAGED,
)
from src.core.search_worker_lifecycle import stop_search_workers
from src.ui.empty_state import EmptyState
from src.ui.aggregate_project_tab import AggregateProjectTab
from src.ui.project_tab import ProjectTab
from src.util import app_log, notify
from src.util.editor import open_folder

log = app_log.get_logger("main_window")
perf_log = app_log.get_logger("performance")


def _restored_tab_index(entries: list[str], saved_index: int) -> int:
    """移除旧固定页后，把旧会话活动索引左移一位。"""
    if "workspace:management" in entries and saved_index > 0:
        return saved_index - 1
    return max(0, saved_index)


def _is_aggregate_directory(config: AppConfig, path: str | Path) -> bool:
    """目录定义本身优先；注册表只负责兼容没有定义的历史入口。"""
    return config.is_aggregate_project(str(path)) or aggregate_config_path(path).is_file()


def _normalized_tab_session(
    entries: list[str],
    saved_index: int,
    registered_aggregate_paths: list[str] | tuple[str, ...] = (),
) -> tuple[list[str], int]:
    """把历史组件 Tab 归并到所属聚合环境，并保留活动环境。"""
    groups: list[str] = []
    chosen: dict[str, str] = {}
    selected_group = ""

    for original_index, token in enumerate(entries):
        kind, _, raw_path = token.partition(":")
        if token == "workspace:management" or not raw_path:
            continue
        if kind not in {"project", "aggregate", "development"}:
            continue

        try:
            resolved = resolve_target_path(raw_path, registered_aggregate_paths)
            if resolved.kind in {"workspace", "workspace_component"}:
                canonical_token = f"development:{resolved.open_target}"
                aggregate_path = resolved.aggregate_project.root_path
                group = f"aggregate:{normalized_path_key(aggregate_path)}"
            elif resolved.kind in {"aggregate", "aggregate_component"}:
                canonical_token = f"aggregate:{resolved.open_target}"
                group = f"aggregate:{normalized_path_key(resolved.open_target)}"
            else:
                canonical_token = f"project:{resolved.open_target}"
                group = f"project:{normalized_path_key(resolved.open_target)}"
        except Exception:
            canonical_token = token
            group = f"{kind}:{normalized_path_key(raw_path)}"

        if group not in chosen:
            groups.append(group)
            chosen[group] = canonical_token
        if original_index == saved_index:
            chosen[group] = canonical_token
            selected_group = group

    normalized = [chosen[group] for group in groups]
    if selected_group in groups:
        target_index = groups.index(selected_group)
    else:
        target_index = min(_restored_tab_index(entries, saved_index), max(0, len(normalized) - 1))
    return normalized, target_index


class _CreateAggregateProjectWorker(QThread):
    done = Signal(object, str)

    def __init__(self, root_path: str, parent=None):
        super().__init__(parent)
        self.root_path = root_path

    def run(self) -> None:
        try:
            root = Path(self.root_path).resolve()
            components = scan_aggregate_components(root)
            project = AggregateProject.from_dict(root, {
                "schemaVersion": 1,
                "id": suggest_stable_id(root.name),
                "name": root.name,
                "kind": "aggregate",
                "workspaceDirectory": "workspace",
                "components": [item.to_dict() for item in components],
                "profiles": [],
            })
            save_aggregate_project(project)
            self.done.emit(project, "")
        except Exception as exc:
            self.done.emit(None, str(exc))


class MainWindow(QMainWindow):
    def __init__(self, config: AppConfig):
        super().__init__()
        self.config = config
        self._quit_requested = False
        self.setWindowTitle("mini-ide")
        self.resize(1480, 900)
        self.setMinimumSize(960, 600)
        self.setAcceptDrops(True)

        res_dir = Path(__file__).resolve().parents[2] / "src" / "resources"
        icon_path = res_dir / "icon.ico"
        if not icon_path.exists():
            icon_path = res_dir / "icon.png"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        self._restore_geometry()

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        notify.install(self.stack)

        self.empty = EmptyState(config)
        self.empty.projectRequested.connect(self._open_or_classify_directory)
        self.stack.addWidget(self.empty)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("main_tabs")
        self.tabs.setDocumentMode(True)
        self._restoring_tabs = False
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.tabBar().tabMoved.connect(self._on_top_tab_moved)
        self.tabs.currentChanged.connect(lambda _index: self._save_tab_session())
        self.stack.addWidget(self.tabs)
        self._aggregate_create_worker: _CreateAggregateProjectWorker | None = None

        self._build_menu()
        self._setup_tray()
        self._build_statusbar()
        self._register_global_shortcuts()

        self._mem_timer = QTimer(self)
        self._mem_timer.setInterval(2000)
        self._mem_timer.timeout.connect(self._refresh_status)
        self._mem_timer.start()

        self._switch_view()

        # 命令行传入的初始项目路径（由 main.py 或单实例 IPC 写入）
        self.pending_initial_project: str | None = None

        # 延迟到事件循环起来后统一收尾：先恢复会话，再处理 pending 初始项目
        QTimer.singleShot(100, self._startup_finalize)

    # ---- 菜单 ----

    def _register_global_shortcuts(self) -> None:
        """窗口级快捷键统一在这里注册一次，触发时路由到当前可见的项目 tab。

        放在主窗口（而非每个 ProjectTab）注册的原因：
        - WindowShortcut 作用域 → 焦点在工具栏/标签栏/任意子组件都能触发，
          不用先点一下内容区（这正是之前"得先聚焦才生效"的根因）
        - 若每个 tab 各自注册窗口级快捷键，多 tab 时同窗口内同一序列重复，
          Qt 会判定歧义导致全部失效；集中注册一份就没有这个问题
        """
        # (序列, 调用当前 tab 的哪个方法名)
        bindings = [
            ("Ctrl+Shift+N", "open_file_picker"),
            ("Ctrl+Shift+F", "open_content_search"),
            ("Ctrl+E", "open_recent_files"),
            ("Ctrl+\\", "open_endpoint_picker"),
            ("Ctrl+Shift+R", "restart_project"),
            ("Ctrl+W", "close_current_file_tab"),
        ]
        for seq, method in bindings:
            sc = QShortcut(QKeySequence(seq), self)
            sc.activated.connect(lambda m=method: self._dispatch_to_current_tab(m))

    def _dispatch_to_current_tab(self, method_name: str) -> None:
        """把快捷键动作转发给当前可见的项目 tab。没有打开项目时静默忽略。"""
        tab = self.tabs.currentWidget()
        if isinstance(tab, (ProjectTab, AggregateProjectTab)):
            fn = getattr(tab, method_name, None)
            if callable(fn):
                fn()

    def _build_menu(self) -> None:
        mb = self.menuBar()

        self.file_menu = mb.addMenu("文件(&F)")
        open_act = QAction("添加目录...", self)
        open_act.setShortcut("Ctrl+O")
        open_act.triggered.connect(self._open_dialog)
        self.file_menu.addAction(open_act)

        self.recent_menu = self.file_menu.addMenu("最近打开")
        self._rebuild_recent_menu()

        self.file_menu.addSeparator()
        quit_act = QAction("退出", self)
        quit_act.setShortcut("Ctrl+Q")
        quit_act.triggered.connect(self._request_quit)
        self.file_menu.addAction(quit_act)

        view_menu = mb.addMenu("视图(&V)")
        theme_menu = view_menu.addMenu("主题")
        self.theme_action_group = QActionGroup(self)
        self.theme_action_group.setExclusive(True)
        self.theme_actions: dict[str, QAction] = {}
        from src.ui.theme import THEME_NAMES, normalize_theme_name
        selected_theme = normalize_theme_name(self.config.theme)
        for theme_id, label in THEME_NAMES.items():
            action = QAction(label, self)
            action.setCheckable(True)
            action.setChecked(theme_id == selected_theme)
            action.triggered.connect(
                lambda checked=False, name=theme_id: checked and self._switch_theme(name)
            )
            self.theme_action_group.addAction(action)
            theme_menu.addAction(action)
            self.theme_actions[theme_id] = action

        help_menu = mb.addMenu("帮助(&H)")
        open_log_act = QAction("打开 mini-ide 日志", self)
        open_log_act.triggered.connect(self._open_app_log)
        help_menu.addAction(open_log_act)
        open_log_dir_act = QAction("打开日志目录", self)
        open_log_dir_act.triggered.connect(self._open_log_dir)
        help_menu.addAction(open_log_dir_act)
        help_menu.addSeparator()
        about = QAction("关于", self)
        about.triggered.connect(self._about)
        help_menu.addAction(about)

    def _switch_theme(self, theme_name: str) -> None:
        from src.ui.theme import apply_theme

        app = QApplication.instance()
        if app is None or theme_name == self.config.theme:
            return
        self.config.theme = apply_theme(app, theme_name)
        action = self.theme_actions.get(self.config.theme)
        if action is not None:
            action.setChecked(True)
        self.config.save()
        self._theme_refresh_queue = list(app.allWidgets())
        QTimer.singleShot(0, self._refresh_theme_batch)
        log.info("切换主题: %s", self.config.theme)

    def _refresh_theme_batch(self) -> None:
        from src.ui.theme import apply_search_style

        queue = getattr(self, "_theme_refresh_queue", [])
        batch, self._theme_refresh_queue = queue[:80], queue[80:]
        for widget in batch:
            if widget.property("role") == "search":
                apply_search_style(widget)
            refresh = getattr(widget, "refresh_theme", None)
            if callable(refresh):
                refresh()
        if self._theme_refresh_queue:
            QTimer.singleShot(0, self._refresh_theme_batch)

    def _setup_tray(self) -> None:
        """创建托盘入口；窗口关闭后仍由托盘菜单负责唤回或退出。"""
        icon = self.windowIcon()
        if icon.isNull():
            icon = QApplication.instance().windowIcon()
        self._tray_icon = QSystemTrayIcon(icon, self)
        self._tray_icon.setToolTip("mini-ide")

        menu = QMenu(self)
        show_action = QAction("显示 mini-ide", self)
        show_action.triggered.connect(self._show_from_tray)
        menu.addAction(show_action)
        menu.addSeparator()
        quit_action = QAction("退出 mini-ide", self)
        quit_action.triggered.connect(self._request_quit)
        menu.addAction(quit_action)
        self._tray_icon.setContextMenu(menu)
        self._tray_icon.activated.connect(self._on_tray_activated)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._tray_icon.show()
        else:
            log.warning("系统托盘不可用，关闭窗口后只能通过 CLI --quit 退出")

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        }:
            self._show_from_tray()

    def _show_from_tray(self) -> None:
        from PySide6.QtCore import Qt

        self.show()
        if self.isMinimized():
            self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.raise_()
        self.activateWindow()

    def _request_quit(self) -> None:
        """托盘/菜单的显式退出，复用 closeEvent 中的服务停止检查。"""
        self._quit_requested = True
        self.close()

    def _rebuild_recent_menu(self) -> None:
        self.recent_menu.clear()
        for entry in self.config.recent_projects[:15]:
            act = QAction(f"{entry.name or Path(entry.path).name}  -  {entry.path}", self)
            act.triggered.connect(lambda _, p=entry.path: self.open_project(p))
            self.recent_menu.addAction(act)
        if not self.config.recent_projects:
            empty_act = QAction("(无)", self)
            empty_act.setEnabled(False)
            self.recent_menu.addAction(empty_act)

    def _rebuild_workspace_menu(self) -> None:
        """旧调用兼容入口；全局工作区菜单已经移除。"""

    # ---- 状态栏 ----

    def _build_statusbar(self) -> None:
        sb = self.statusBar()
        sb.setSizeGripEnabled(False)
        self.lbl_project = QLabel("")
        self.lbl_project.setProperty("role", "hint")
        self.lbl_mem = QLabel("")
        self.lbl_mem.setProperty("role", "hint")
        sb.addWidget(self.lbl_project, 1)
        sb.addPermanentWidget(self.lbl_mem)

    def _refresh_status(self) -> None:
        if self.config.show_memory_usage:
            try:
                rss_mb = psutil.Process().memory_info().rss / 1024 / 1024
                self.lbl_mem.setText(f"mini-ide: {rss_mb:.0f} MB")
            except psutil.Error:
                self.lbl_mem.setText("")

        tab = self.tabs.currentWidget()
        if tab and hasattr(tab, "project_meta"):
            meta = tab.project_meta
            self.lbl_project.setText(f"{meta.project_type}  |  {meta.path}")
        else:
            self.lbl_project.setText("")

    # ---- Tab 管理 ----

    def _on_top_tab_moved(self, from_index: int, to_index: int) -> None:
        """顶层目录 Tab 可自由排序。"""
        self._save_tab_session()

    def _switch_view(self) -> None:
        self.stack.setCurrentIndex(1 if self.tabs.count() > 0 else 0)

    def _open_dialog(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "选择要添加的目录", self._default_dialog_dir()
        )
        if path:
            self._choose_directory_type(path)

    def _choose_directory_type(self, path: str) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("添加目录")
        box.setText(f"这个目录按哪种方式管理？\n\n{path}")
        normal_button = box.addButton("普通项目", QMessageBox.ButtonRole.AcceptRole)
        aggregate_button = box.addButton("聚合目录", QMessageBox.ButtonRole.ActionRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is normal_button:
            self.config.unregister_aggregate_project(path)
            self.config.save()
            self.open_project(path)
        elif box.clickedButton() is aggregate_button:
            self._add_aggregate_directory(path)

    def _open_or_classify_directory(self, path: str) -> None:
        try:
            resolved = resolve_target_path(path, self.config.aggregate_project_paths)
        except TargetResolutionError:
            self.open_project(path)
            return
        if (
            resolved.kind != "normal_project"
            or self.config.find_project(path)
            or _is_aggregate_directory(self.config, path)
        ):
            self.open_project(path)
        else:
            self._choose_directory_type(path)

    def _add_aggregate_directory(self, path: str) -> None:
        if self._aggregate_create_worker:
            QMessageBox.information(self, "正在添加", "请等待当前聚合目录识别完成。")
            return
        if aggregate_config_path(path).is_file():
            self.config.register_aggregate_project(path)
            self.config.save()
            self.open_aggregate_project(path)
            return
        worker = _CreateAggregateProjectWorker(path, QApplication.instance())
        self._aggregate_create_worker = worker
        worker.done.connect(self._aggregate_directory_created)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _aggregate_directory_created(self, project, error: str) -> None:
        if self.sender() is not self._aggregate_create_worker:
            return
        self._aggregate_create_worker = None
        if error or project is None:
            QMessageBox.warning(self, "添加聚合目录失败", error or "未知错误")
            return
        self.config.register_aggregate_project(project.root_path)
        self.config.save()
        self.open_aggregate_project(project.root_path)

    def _default_dialog_dir(self) -> str:
        """对话框默认落脚点：最近项目的父目录 > 配置的 default_project_dir > 家目录"""
        if self.config.recent_projects:
            parent = str(Path(self.config.recent_projects[0].path).parent)
            if Path(parent).is_dir():
                return parent
        if self.config.default_project_dir and Path(self.config.default_project_dir).is_dir():
            return self.config.default_project_dir
        return str(Path.home())

    def open_project(
        self, path: str, quiet: bool = False,
    ) -> ProjectTab | AggregateProjectTab | None:
        try:
            resolved = resolve_target_path(path, self.config.aggregate_project_paths)
        except TargetResolutionError as exc:
            log.warning("打开目录解析失败: %s | %s", path, exc)
            if not quiet:
                QMessageBox.warning(self, "打开目录失败", str(exc))
            return None

        if resolved.kind in {"workspace", "workspace_component"}:
            tab = self.open_development_workspace(resolved.open_target, quiet=quiet)
            if tab is not None and resolved.component_id:
                tab.select_component(resolved.component_id)
            return tab
        if resolved.kind in {"aggregate", "aggregate_component"}:
            tab = self.open_aggregate_project(resolved.open_target, quiet=quiet)
            if tab is not None and resolved.component_id:
                tab.select_component(resolved.component_id)
            return tab

        p = Path(resolved.path)

        existing = self.find_tab_by_path(str(p))
        if existing:
            self.tabs.setCurrentWidget(existing)
            return existing

        try:
            meta = detect_project(str(p))
        except Exception:
            log.exception("项目识别失败: %s", p)
            if not quiet:
                QMessageBox.warning(self, "识别失败", f"无法识别项目类型\n{p}")
            return None
        log.info("打开项目: %s | 类型=%s | port=%s | profiles=%s",
                 p, meta.project_type, meta.default_port, [x.name for x in meta.profiles])

        entry = self.config.find_project(str(p)) or ProjectEntry(path=str(p))
        entry.name = meta.name
        entry.project_type = meta.project_type
        entry.last_opened_at = time.time()
        self.config.touch_project(entry)
        self.config.save()

        tab = ProjectTab(meta, entry, self.config)
        tab.path = str(p)
        idx = self.tabs.addTab(tab, f"{meta.icon}  {meta.name}")
        self.tabs.setTabToolTip(idx, str(p))
        self.tabs.setCurrentIndex(idx)
        self._rebuild_recent_menu()
        self._rebuild_workspace_menu()
        self._switch_view()
        self._save_tab_session()
        return tab

    def open_aggregate_project(
        self, path: str, quiet: bool = False,
    ) -> AggregateProjectTab | None:
        try:
            project = load_aggregate_project(path)
        except Exception as exc:
            log.exception("打开聚合项目失败: %s", path)
            if not quiet:
                QMessageBox.warning(self, "打开聚合项目失败", str(exc))
            return None
        entry = self.config.find_project(project.root_path) or ProjectEntry(
            path=project.root_path,
        )
        entry.name = project.name
        entry.project_type = "aggregate"
        entry.last_opened_at = time.time()
        self.config.touch_project(entry)
        self.config.register_aggregate_project(project.root_path)
        self.config.save()
        self._rebuild_recent_menu()
        existing = self.find_tab_by_path(project.root_path)
        if isinstance(existing, AggregateProjectTab):
            if existing.workspace is not None:
                if not existing.activate_workspace(None, interactive=not quiet):
                    return None
            self.focus_tab(existing)
            return existing
        if isinstance(existing, ProjectTab):
            if not self.close_project_tab(existing, quiet=quiet):
                if not quiet:
                    QMessageBox.warning(
                        self, "打开聚合项目失败", "聚合根目录的旧项目 Tab 未能安全关闭。",
                    )
                return None
        component_keys = {
            normalized_path_key(item.absolute_path(project.root_path))
            for item in project.components
        }
        adopted: dict[str, ProjectTab] = {}
        for index in range(self.tabs.count() - 1, -1, -1):
            widget = self.tabs.widget(index)
            if not isinstance(widget, ProjectTab):
                continue
            key = normalized_path_key(widget.project_meta.path)
            if key in component_keys:
                adopted[key] = widget
                self.tabs.removeTab(index)
        tab = AggregateProjectTab(
            project, self.config,
            environment_guard=self._ensure_aggregate_environment,
            existing_tabs=adopted,
            parent=self.tabs,
        )
        index = self.tabs.addTab(tab, project.name)
        self.tabs.setTabToolTip(index, project.root_path)
        self.tabs.setCurrentIndex(index)
        self._rebuild_workspace_menu()
        self._switch_view()
        self._save_tab_session()
        return tab

    def open_development_workspace(
        self, path: str, quiet: bool = False,
    ) -> AggregateProjectTab | None:
        try:
            workspace = load_development_workspace(path)
            project = load_aggregate_project(workspace.aggregate_project_path)
            workspace = load_development_workspace(path, aggregate_project=project)
        except Exception as exc:
            log.exception("打开开发工作区失败: %s", path)
            if not quiet:
                QMessageBox.warning(self, "打开开发工作区失败", str(exc))
            return None
        existing = self.find_tab_by_path(project.root_path)
        if isinstance(existing, AggregateProjectTab):
            tab = existing
            self.focus_tab(tab)
        else:
            tab = self.open_aggregate_project(project.root_path, quiet=quiet)
        if tab is None:
            return None
        if not tab.activate_workspace(workspace, interactive=not quiet):
            return None
        self.focus_tab(tab)
        tab.show_projects()
        self._save_tab_session()
        return tab

    def _ensure_aggregate_environment(
        self, requester: AggregateProjectTab, allow_switch: bool, interactive: bool,
    ) -> bool:
        conflicts = []
        for index in range(self.tabs.count()):
            tab = self.tabs.widget(index)
            if not isinstance(tab, AggregateProjectTab) or tab is requester:
                continue
            if normalized_path_key(tab.aggregate_root_path) != normalized_path_key(
                requester.aggregate_root_path
            ):
                continue
            if tab.running_service_items(refresh_external=True):
                conflicts.append(tab)
        if not conflicts:
            return True
        if not allow_switch:
            return False
        if interactive:
            names = "、".join(tab.project_meta.name for tab in conflicts)
            answer = QMessageBox.question(
                self, "切换运行环境",
                f"同一聚合项目当前由 {names} 占用运行环境。\n\n"
                "是否先停止旧环境，再启动当前环境？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return False
        for tab in conflicts:
            tab.stop_all_services(include_external=True, silent=True)
            if not tab.wait_services_stopped(20000):
                QMessageBox.warning(self, "切换失败", f"{tab.project_meta.name} 未能完全停止。")
                return False
        return True

    def find_tab_by_path(self, path: str):
        key = normalized_path_key(path)
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            candidate = getattr(w, "path", None)
            if candidate and normalized_path_key(candidate) == key:
                return w
        return None

    def focus_tab(self, tab) -> None:
        idx = self.tabs.indexOf(tab)
        if idx >= 0:
            self.tabs.setCurrentIndex(idx)

    def _close_tab(self, index: int) -> None:
        w = self.tabs.widget(index)
        if w and hasattr(w, "request_close"):
            if not w.request_close():
                return
        self.tabs.removeTab(index)
        self._switch_view()
        self._save_tab_session()

    def close_project_tab(self, tab, quiet: bool = False) -> bool:
        """CLI/GUI 共用的项目关闭入口；quiet=True 时不弹消息框。"""
        idx = self.tabs.indexOf(tab)
        if idx < 0:
            return False
        if not tab.request_close(confirm_running=not quiet, quiet=quiet):
            return False
        self.tabs.removeTab(idx)
        self._switch_view()
        self._save_tab_session()
        return True

    # ---- 其他 ----

    def _about(self) -> None:
        from src.ui.theme import FG_SECONDARY
        QMessageBox.about(
            self,
            "关于 mini-ide",
            "<h3>mini-ide</h3>"
            "<p>AI 时代的开发指挥台</p>"
            "<p>Spring Boot / Vue / Python 项目的轻量启动器</p>"
            f"<p style='color:{FG_SECONDARY};'>v0.1.0</p>",
        )

    def _open_app_log(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        path = app_log.log_path_today()
        if path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        else:
            QMessageBox.information(self, "日志", f"当前日志文件尚未生成:\n{path}")

    def _open_log_dir(self) -> None:
        from src.core.config import LOG_DIR
        open_folder(str(LOG_DIR))

    def _restore_geometry(self) -> None:
        g = self.config.window_geometry
        # 首次启动（无任何记录）：默认最大化，撑满屏幕
        if not g:
            self.showMaximized()
            return
        # 上次是最大化关闭：继续最大化
        if g.get("maximized"):
            self.showMaximized()
            return
        # 否则按上次的尺寸/位置恢复
        if g.get("w") and g.get("h"):
            self.resize(g["w"], g["h"])
            if "x" in g and "y" in g:
                self.move(g["x"], g["y"])

    def _persist_geometry(self) -> None:
        # 最大化状态下 geometry() 返回的是恢复后的尺寸，不存为常规尺寸
        if self.isMaximized():
            self.config.window_geometry = {"maximized": True}
            return
        g = self.geometry()
        self.config.window_geometry = {
            "x": g.x(), "y": g.y(), "w": g.width(), "h": g.height(),
            "maximized": False,
        }

    def dragEnterEvent(self, e: QDragEnterEvent) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e: QDropEvent) -> None:
        for url in e.mimeData().urls():
            path = url.toLocalFile()
            if path and Path(path).is_dir():
                self._open_or_classify_directory(path)
                break

    def closeEvent(self, e: QCloseEvent) -> None:
        if not self._quit_requested:
            self._persist_geometry()
            self._persist_tabs()
            self.config.save()
            self.hide()
            e.ignore()
            log.info("关闭窗口：隐藏到系统托盘")
            return

        running = self._collect_running_services()
        stop_running = True
        if running:
            choice = self._confirm_close_with_services(running)
            if choice == "cancel":
                self._quit_requested = False
                e.ignore()
                return
            stop_running = choice == "stop"

        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if w and hasattr(w, "request_close"):
                if not w.request_close(confirm_running=False, stop_running=stop_running):
                    self._quit_requested = False
                    e.ignore()
                    return
        if not stop_search_workers():
            self._quit_requested = False
            e.ignore()
            log.error("搜索线程未能在退出前停止，已取消退出")
            return
        self._persist_geometry()
        self._persist_tabs()
        self.config.save()
        if running:
            kept = [
                x for x in running
                if not stop_running and x.get("state") == STATE_RUNNING_EXTERNAL
            ]
            log.info(
                "退出前服务处理：running=%d stop=%s kept_external=%d",
                len(running), stop_running, len(kept),
            )
        super().closeEvent(e)
        # 显式退出时结束事件循环；普通关闭已在上面隐藏到托盘。
        QApplication.quit()

    def _collect_running_services(self) -> list[dict]:
        items: list[dict] = []
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, (ProjectTab, AggregateProjectTab)):
                try:
                    items.extend(w.running_service_items(refresh_external=True))
                except Exception:
                    log.exception("收集运行服务失败: %s", w.project_meta.name)
        return items

    def _confirm_close_with_services(self, items: list[dict]) -> str:
        """返回 stop / keep_external / cancel。"""
        managed = [x for x in items if x.get("state") == STATE_RUNNING_MANAGED]
        external = [x for x in items if x.get("state") == STATE_RUNNING_EXTERNAL]
        lines = []
        for item in items[:12]:
            port = f":{item.get('port')}" if item.get("port") else ""
            pid = f" PID {item.get('pid')}" if item.get("pid") else ""
            label = "外部" if item.get("state") == STATE_RUNNING_EXTERNAL else "托管"
            lines.append(
                f"- [{label}] {item.get('project')} / {item.get('module')}{port}{pid}"
            )
        if len(items) > 12:
            lines.append(f"... 还有 {len(items) - 12} 个")
        msg = (
            "当前仍有服务在运行：\n\n"
            + "\n".join(lines)
            + "\n\n选择「全部停止后退出」会先停止这些服务；"
        )
        if external and not managed:
            msg += "也可以选择「保留外部服务退出」。"
        elif external and managed:
            msg += "托管服务必须停止；外部服务如需保留，请先手动停止托管服务后再退出。"
        else:
            msg += "当前没有可保留的外部服务。"

        box = QMessageBox(self)
        box.setWindowTitle("退出前检查")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(msg)
        stop_btn = box.addButton("全部停止后退出", QMessageBox.ButtonRole.AcceptRole)
        keep_btn = None
        if external and not managed:
            keep_btn = box.addButton("保留外部服务退出", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(stop_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is stop_btn:
            return "stop"
        if keep_btn is not None and clicked is keep_btn:
            return "keep_external"
        if clicked is cancel_btn:
            return "cancel"
        return "cancel"

    # ---- Tab 会话持久化 ----

    def _persist_tabs(self) -> None:
        """把当前 Tab 列表记下来，下次启动恢复"""
        active: list[str] = []
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, AggregateProjectTab):
                kind = "development" if w.workspace else "aggregate"
                path = w.workspace.root_path if w.workspace else w.aggregate_root_path
                active.append(f"{kind}:{path}")
            elif isinstance(w, ProjectTab):
                active.append(f"project:{w.project_meta.path}")
        self.config.active_tabs = active
        self.config.active_tab_index = max(0, self.tabs.currentIndex())
        log.info("保存 Tab 会话: %d 个 (current=%d)", len(active), self.config.active_tab_index)

    def _save_tab_session(self) -> None:
        """Tab 增删、切换、拖动后立即保存，避免异常退出时恢复旧会话。"""
        if self._restoring_tabs:
            return
        self._persist_tabs()
        self.config.save()

    def _startup_finalize(self) -> None:
        """事件循环起来后的收尾：先恢复上次会话，再打开命令行传入的初始项目。"""
        if (self.config.startup_restore_mode in {"workspace", "last_session"}
              and self.config.restore_tabs_on_startup and self.config.active_tabs):
            self._restore_tabs(self._finish_startup)
            return
        self._finish_startup()

    def _finish_startup(self) -> None:
        if self.pending_initial_project:
            self.open_project(self.pending_initial_project)
            self.pending_initial_project = None

    def activate_and_open(self, path: str | None) -> None:
        """单实例 IPC 入口：老实例收到新请求后的响应。"""
        from PySide6.QtCore import Qt
        if path:
            self.open_project(path)
        if not self.isVisible():
            self.show()
        # 只清掉「最小化」标志把窗口唤回前台，不能无脑 showNormal——
        # 那会把最大化的窗口一并降级成普通大小（用户从空目录右键 open ide
        # 时窗口突然缩小就是这么来的）。最大化/普通状态原样保留。
        if self.isMinimized():
            self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.raise_()
        self.activateWindow()

    def _restore_tabs(self, on_done=None) -> None:
        entries, target_index = _normalized_tab_session(
            list(self.config.active_tabs),
            self.config.active_tab_index,
            self.config.aggregate_project_paths,
        )
        if not entries:
            if on_done:
                on_done()
            return
        log.info("恢复 Tab 会话: %d 个", len(entries))
        started = time.perf_counter()
        self._restoring_tabs = True
        next_index = 0
        opened = 0

        def finish() -> None:
            if opened and target_index < self.tabs.count():
                self.tabs.setCurrentIndex(target_index)
            self._restoring_tabs = False
            self._save_tab_session()
            perf_log.info(
                "perf op=session-restore duration_ms=%.1f files=%d status=done",
                (time.perf_counter() - started) * 1000, opened,
            )
            if on_done:
                on_done()

        def restore_next() -> None:
            nonlocal next_index, opened
            if next_index >= len(entries):
                finish()
                return
            token = entries[next_index]
            next_index += 1
            item_started = time.perf_counter()
            kind, _, key = token.partition(":")
            if not key or kind not in {"project", "aggregate", "development"}:
                status = "skipped"
            else:
                status = "done"
                try:
                    if Path(key).is_dir():
                        if kind == "aggregate":
                            tab = self.open_aggregate_project(key)
                        elif kind == "development":
                            tab = self.open_development_workspace(key)
                        else:
                            tab = self.open_project(key)
                        if tab is not None:
                            opened += 1
                    else:
                        status = "skipped"
                        log.warning("跳过不存在的项目: %s", key)
                except Exception:
                    status = "failed"
                    log.exception("恢复 Tab 失败: %s", token)
            perf_log.info(
                "perf op=session-restore-item duration_ms=%.1f files=1 status=%s",
                (time.perf_counter() - item_started) * 1000, status,
            )
            QTimer.singleShot(10, restore_next)

        QTimer.singleShot(0, restore_next)
