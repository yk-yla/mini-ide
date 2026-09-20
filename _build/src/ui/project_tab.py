"""单项目 Tab 面板

把 项目元信息 / 工具栏按钮 / 日志 / 设置面板 / 状态条 / 进程运行器 串起来。
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QMenu, QMessageBox, QPushButton,
    QSplitter, QTabBar, QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

from src.core.config import AppConfig, ProjectEntry
from src.core.file_index import FileIndexer
from src.core.git_worker import (
    GitBranchListWorker, GitCheckoutRemoteWorker, GitCheckoutWorker,
    GitDeleteLocalBranchWorker, GitDirtyCheckWorker, GitFetchWorker,
    GitMergePushWorker, GitStatusWorker,
)
from src.core.nginx_detector import detect_nginx
from src.core.process_runner import ProcessRunner, RunContext
from src.core.project_detector import ProjectMeta, RunProfile
from src.core.service_state import (
    KIND_MODULE, KIND_PROJECT, KIND_SCRIPT, SOURCE_EXTERNAL_DETECTOR,
    SOURCE_MANAGED_RUNNER, SOURCE_NONE, SOURCE_SCRIPT_RUNNER,
    STATE_RUNNING_EXTERNAL as SERVICE_RUNNING_EXTERNAL,
    STATE_RUNNING_MANAGED as SERVICE_RUNNING_MANAGED, STATE_STARTING as SERVICE_STARTING,
    STATE_STOPPED as SERVICE_STOPPED, STATE_STOPPING as SERVICE_STOPPING,
    ServiceState, running_items,
)
from src.ui.file_tree import FileTree
from src.ui.content_search import ContentSearchDialog
from src.ui.git_viewer import GitViewer
from src.ui.log_widget import LogWidget
from src.ui.project_service_controller import ProjectServiceController
from src.ui.quick_open import (
    PickerItem, show_recent_files,
)
from src.ui.service_panel import (
    STATE_IDLE, STATE_RUNNING, STATE_RUNNING_EXTERNAL, STATE_STARTING,
    STATE_STOPPING, ServicePanel,
)
from src.ui.settings_panel import SettingsPanel
from src.ui.theme import (
    BG_BTN, BG_BTN_HOVER, BG_BTN_PRESSED, BG_L1, BG_L4,
    BORDER_STRONG, BORDER_SUBTLE, COLOR_SUCCESS, COLOR_WARN,
    FG_DIM, FG_PRIMARY, FG_SECONDARY,
    FONT_PT_UI_SM, RADIUS_SM,
)
from src.util import git_info, notify
from src.util.editor import (
    open_in_editor, open_folder, reveal_in_explorer,
    is_office_doc, open_with_system_default,
)


# 顶层调谐常量（提到顶部方便统一调，避免散在各方法里成 magic number）
LEFT_PANEL_WIDTH = 240
STATUS_REFRESH_MS = 3000
GIT_FETCH_INTERVAL_MS = 5 * 60 * 1000
GIT_FETCH_INITIAL_DELAY_MS = 8000
MODULE_STOP_TIMEOUT_MS = 10000
RUNTIME_STOP_COMMAND_TIMEOUT_MS = 3000
RUNTIME_STOP_GRACE_MS = 3000
RESTART_GAP_MS = 1800
COMPILE_THEN_RUN_GAP_MS = 300

log = logging.getLogger("mini-ide")
action_log = logging.getLogger("mini-ide.action")  # 操作行为日志，统一写到同一日志文件
perf_log = logging.getLogger("mini-ide.performance")


# 状态栏分支/改动按钮保持紧凑，但必须有明确按钮边界。
def _status_btn_base_qss() -> str:
    return (
        f"QToolButton {{ background:{BG_BTN}; border:1px solid {BORDER_SUBTLE};"
        f" border-radius:{RADIUS_SM}px; padding:3px 8px;"
        f" min-height:20px; font-size:{FONT_PT_UI_SM}pt; }}"
        f"QToolButton:hover {{ background:{BG_BTN_HOVER};"
        f" border-color:{BORDER_STRONG}; border-radius:{RADIUS_SM}px; }}"
        f"QToolButton:pressed {{ background:{BG_BTN_PRESSED}; }}"
        f"QToolButton::menu-indicator {{ image:none; width:0; }}"
    )


class _BranchMenu(QMenu):
    """分支下拉菜单：点击本地分支切换，右键做分支操作。"""

    localDeleteRequested = Signal(str)
    remoteCheckoutRequested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_branch = ""

    def set_current_branch(self, name: str) -> None:
        self._current_branch = name or ""

    def contextMenuEvent(self, event):  # type: ignore[override]
        action = self.actionAt(event.pos())
        data = action.data() if action is not None else None
        kind = "copy"
        branch = ""
        if isinstance(data, tuple) and len(data) == 2:
            kind, branch = data
        elif isinstance(data, str):
            branch = data
        if not branch:
            super().contextMenuEvent(event)
            return
        ctx = QMenu(self)
        checkout_act = None
        delete_act = None
        if kind == "remote":
            checkout_act = ctx.addAction("切到这个分支")
            ctx.addSeparator()
        if kind == "local" and branch != self._current_branch:
            delete_act = ctx.addAction("删除本地分支")
            ctx.addSeparator()
        copy_act = ctx.addAction("复制分支名")
        chosen = ctx.exec(event.globalPos())
        if chosen is checkout_act:
            self.remoteCheckoutRequested.emit(branch)
        elif chosen is delete_act:
            self.localDeleteRequested.emit(branch)
        elif chosen is copy_act:
            QApplication.clipboard().setText(branch)
        event.accept()


# 前端项目类型白名单：这些类型的项目如果 package.json 带 build script，就在工具栏多一个「打包」按钮
_FRONTEND_TYPES = {"vue", "react", "next", "nuxt", "svelte", "node"}

# 多模块 Spring Boot：从子模块启动日志里抓真实监听端口，覆盖 application.yml 里的默认值
# 兼容 Spring Boot 2.x/3.x 的 Tomcat / Netty / Undertow / Jetty 启动行：
#   Tomcat started on port(s): 8080 (http)
#   Tomcat started on port 8080 (http)
#   Netty started on port(s): 8080
_RE_BOOT_LISTEN_PORT = re.compile(
    r"(?:Tomcat|Netty|Undertow|Jetty)\s+started\s+on\s+port(?:\(s\))?[:\s]+(\d+)",
    re.IGNORECASE,
)


class ProjectTab(QWidget):

    statusChanged = Signal()   # 状态变化时通知主窗口刷新状态栏

    def __init__(self, meta: ProjectMeta, entry: ProjectEntry, config: AppConfig, parent=None):
        super().__init__(parent)
        self.setObjectName("project_workbench")
        self.project_meta = meta
        self.entry = entry
        self.config = config
        self.path = meta.path

        # 多运行单元项目：Spring 服务、Lerna 角色等各有独立 runner。
        # 名称保留用于兼容 CLI 和旧测试，判断依据已改为通用运行单元。
        self._is_multi_module = bool(getattr(meta, "has_multiple_runtimes", False))

        # 是否 git 仓库：启动时判一次缓存住。非 git 项目不起染色/状态栏 worker，
        # 省得每 3s 白跑一堆注定失败的 git 子进程。
        from src.core.git_ops import has_git_metadata
        self._is_git_repo = has_git_metadata(meta.path)

        # 项目级 runner：单模块项目的启动用它；多模块项目用它跑编译/Clean 等全局 profile
        self.runner = ProcessRunner(self)
        self.runner.outputLine.connect(self._on_output)
        self.runner.stateChanged.connect(self._on_state)
        self.runner.finished.connect(self._on_finished)
        self.service_controller = ProjectServiceController(self)

        # 每个运行单元独立的 runner + 日志 widget（仅多运行单元时有内容）
        self.module_runners: dict[str, ProcessRunner] = {}
        self.module_logs: dict[str, LogWidget] = {}
        self._module_current: dict[str, RunProfile] = {}
        # 显式 stop 命令使用独立 runner，不能覆盖仍在运行的服务 runner。
        self._runtime_stop_runners: dict[str, ProcessRunner] = {}
        # 模块启动日志里抓到的真实监听端口（覆盖 application.yml 默认值）
        self._module_ports: dict[str, int] = {}
        # 用户关 tab 触发 stop 的模块集合；finished 时自动清理 tab
        self._closing_modules: set[str] = set()
        # 「外部启动感知」：非 mini-ide 拉起、但端口快照里按 cmdline 匹配到的模块 → pid。
        # 这些模块面板显示「运行中(外部)」，停止按钮走 kill_pid 而非 runner.stop()。
        self._module_external_pids: dict[str, int] = {}
        self._runtime_start_generation = 0
        self._nginx_status = None
        # 外部进程感知器：构造时一次性预算各模块的 match keys（模块列表不变），
        # 之后每轮刷新只做匹配。从 ProjectTab 抽到 core.external_detector 便于单测。
        from src.core.external_detector import ExternalProcessDetector
        self._external_detector = ExternalProcessDetector(
            meta.path, meta.spring_boot_modules,
        )

        # 文件树「▶ 运行脚本」启动的脚本：每个脚本绝对路径一个独立 runner + log tab。
        # 与 module_runners 完全解耦，不进 ServicePanel，不影响多模块聚合状态。
        # key 使用绝对路径（lower 后），同路径再次运行时会聚焦已有 tab 而不是再开一个
        self._script_runners: dict[str, ProcessRunner] = {}
        self._script_logs: dict[str, LogWidget] = {}
        self._closing_scripts: set[str] = set()

        self._current_profile: RunProfile | None = None
        self._pending_after_compile: RunProfile | None = None
        # 待重启的 profile：stop() 后不靠固定延时赌进程已退，而是在 _on_finished
        # （进程真正退出）里消费它再启动，避免慢停止导致重复弹框 / 定时器叠加。
        self._pending_restart: RunProfile | None = None
        self._git_branch_worker: GitBranchListWorker | None = None
        self._git_dirty_worker: GitDirtyCheckWorker | None = None
        self._pending_checkout: tuple[bool, str] | None = None
        self._branch_data: dict | None = None
        self._checkout_target = ""
        self._git_fetch_worker: GitFetchWorker | None = None
        self._git_checkout_worker: GitCheckoutWorker | None = None
        self._git_checkout_remote_worker: GitCheckoutRemoteWorker | None = None
        self._git_delete_branch_worker: GitDeleteLocalBranchWorker | None = None
        self._git_merge_worker: GitMergePushWorker | None = None
        self._git_status_worker: GitStatusWorker | None = None
        self._git_viewer = None
        self._merge_dialog = None
        # 启动完成标记：日志里看到 Started / ready in 等 marker 后，只给状态栏贴一次"✓ 启动完成"
        self._startup_phase_marked = False
        self._recent_files: list[str] = []     # 最近在预览里打开的文件

        self.indexer = FileIndexer(self.project_meta.path, self)
        self.indexer.start_async_scan()
        # 接口跳转索引：懒加载，首次按 Ctrl+/ 才扫（见 open_endpoint_picker）
        self._controller_indexer = None
        self._endpoint_dialog = None

        self._build_ui()
        self._register_shortcuts()

        # 状态轮询（运行时长、占用端口、内存）
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(STATUS_REFRESH_MS)
        self._poll_timer.timeout.connect(self._poll_status_if_visible)
        timer_offset = sum(ord(char) for char in self.project_meta.path) % 800
        self._poll_start_timer = QTimer(self)
        self._poll_start_timer.setSingleShot(True)
        self._poll_start_timer.timeout.connect(self._start_status_polling)
        self._poll_start_timer.start(250 + timer_offset)

        # 远程分支轮询：每 5 分钟 git fetch 一次，发现新提交时高亮分支按钮
        self._fetch_timer = QTimer(self)
        self._fetch_timer.setInterval(GIT_FETCH_INTERVAL_MS)
        self._fetch_timer.timeout.connect(self._start_remote_fetch)
        self._fetch_start_timer = QTimer(self)
        self._fetch_start_timer.setSingleShot(True)
        self._fetch_start_timer.timeout.connect(self._start_fetch_polling)
        self._fetch_start_timer.start(GIT_FETCH_INITIAL_DELAY_MS + timer_offset * 5)

    # ---- UI ----

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_toolbar())

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setHandleWidth(2)

        self.file_tree = FileTree(
            self.project_meta.path,
            indexer=self.indexer,
            project_type=self.project_meta.project_type,
        )
        self.file_tree.fileActivated.connect(self._on_file_activated)
        self.file_tree.fileCreated.connect(self._on_file_created)
        self.file_tree.scriptRunRequested.connect(self._run_script)

        # 多模块项目：左侧垂直 splitter，上是服务面板、下是文件树。
        # 单模块项目：左侧只有文件树（行为不变）。
        self.service_panel: ServicePanel | None = None
        if self._is_multi_module:
            left_wrap = QSplitter(Qt.Orientation.Vertical)
            left_wrap.setHandleWidth(2)
            unit_title = "角色" if self.project_meta.project_type == "frontend-workspace" else "服务"
            self.service_panel = ServicePanel(
                [
                    (unit.id, unit.name, unit.expected_port)
                    for unit in self.project_meta.runtime_units
                ],
                parent=self,
                title=unit_title,
            )
            self.service_panel.startRequested.connect(self._start_module)
            self.service_panel.stopRequested.connect(self._stop_module)
            self.service_panel.focusRequested.connect(self._focus_module_log)
            self.service_panel.startAllRequested.connect(self._start_all_modules)
            self.service_panel.stopAllRequested.connect(self._stop_all_modules)
            self.service_panel.clearLogsRequested.connect(self.clear_all_logs)
            left_wrap.addWidget(self.service_panel)
            left_wrap.addWidget(self.file_tree)
            left_wrap.setSizes([220, 500])
            self.splitter.addWidget(left_wrap)
        else:
            self.splitter.addWidget(self.file_tree)

        self.log = LogWidget()
        self.log.set_max_blocks(self.config.max_log_blocks)
        self.log.fileJumpRequested.connect(self._jump_to_file)

        # 中心 Tab 容器：tab 0 固定是日志（不可关），其它 tab 是文件面板
        self.center_tabs = QTabWidget()
        self.center_tabs.setTabsClosable(True)
        self.center_tabs.setMovable(True)
        self.center_tabs.setDocumentMode(True)
        self.center_tabs.tabCloseRequested.connect(self._on_tab_close_requested)
        self.center_tabs.currentChanged.connect(self._on_center_tab_changed)
        self.center_tabs.addTab(self.log, "📋 日志")
        # 锁定日志 tab 的关闭按钮（两侧都清空，兼容不同平台默认位置）
        _bar = self.center_tabs.tabBar()
        _bar.setTabButton(0, QTabBar.ButtonPosition.RightSide, None)
        _bar.setTabButton(0, QTabBar.ButtonPosition.LeftSide, None)
        # 多模块（微服务）项目：项目级日志平时是空的（各模块输出进各自 tab），
        # 默认隐藏这个常驻 tab；一旦有全局消息（整体编译输出 / 告警）写入就自动显示回来。
        # tab 始终留在 index 0，只切 visible，不动结构，避免别处「index 0 即日志」的假设错位。
        if self._is_multi_module:
            self.center_tabs.setTabVisible(0, False)
            self.log.contentAdded.connect(self._reveal_log_tab)
        # tab 右键菜单：关闭 / 关闭其它 / 关闭右侧 / 关闭左侧
        _bar.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        _bar.customContextMenuRequested.connect(self._on_tab_bar_context_menu)
        # 已打开文件去重：key = abs_path.lower() → FilePreviewPane
        self._file_panes: dict[str, object] = {}

        self.splitter.addWidget(self.center_tabs)
        self.splitter.setSizes([LEFT_PANEL_WIDTH, 1100])

        root.addWidget(self.splitter, 1)

    def _build_toolbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("top_toolbar")
        bar.setFrameShape(QFrame.Shape.NoFrame)
        bar.setStyleSheet(
            f"#top_toolbar {{ background:{BG_L1}; border-bottom:1px solid {BORDER_SUBTLE}; }}"
        )
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(6)

        type_lbl = QLabel(f"{self.project_meta.icon}  {self.project_meta.display_type}")
        type_lbl.setProperty("role", "title")
        lay.addWidget(type_lbl)

        name_lbl = QLabel(self.project_meta.name)
        name_lbl.setProperty("role", "subtitle")
        lay.addWidget(name_lbl)

        lay.addSpacing(20)

        # 工具栏保留项目最常用的运行控制；其它动作通过各自明确入口操作。
        # 多模块项目：启停走左侧服务面板，主按钮隐藏
        self._profile_buttons: dict[str, QPushButton] = {}
        primary_profiles = [p for p in self.project_meta.profiles if p.primary]
        self._primary_profile: RunProfile | None = primary_profiles[0] if primary_profiles else None

        self.btn_main = QPushButton()
        self.btn_main.setMinimumWidth(110)
        self.btn_main.clicked.connect(self._on_main_clicked)
        lay.addWidget(self.btn_main)
        # 多模块项目走左侧服务面板，主按钮隐藏；
        # 通用目录（未识别出可启动命令）也隐藏主按钮，不显示无意义的灰色「启动」
        if self._is_multi_module or self._primary_profile is None:
            self.btn_main.setVisible(False)
        else:
            self._update_main_button("idle")

        # 前端项目：package.json 里有 build script 时多一个「🔒 打包」按钮，
        # 走和主按钮同一套 _run_profile 逻辑（正在跑时会弹确认框先停再跑）
        self._build_profile: RunProfile | None = None
        if self.project_meta.project_type in _FRONTEND_TYPES:
            build_prof = next(
                (p for p in self.project_meta.profiles if p.name == "build"),
                None,
            )
            if build_prof:
                self._build_profile = build_prof
                self.btn_build = QPushButton(
                    f"{build_prof.icon}  {build_prof.label}" if build_prof.icon else build_prof.label
                )
                self.btn_build.setToolTip(
                    build_prof.description or " ".join(build_prof.command)
                )
                self.btn_build.clicked.connect(lambda: self._run_profile(build_prof))
                lay.addWidget(self.btn_build)

        lay.addStretch(1)

        # 运行计数 / 启动完成提示（原状态行，已并入本工具栏，靠右显示）
        self.lbl_elapsed = QLabel("")
        self.lbl_elapsed.setProperty("role", "subtitle")
        lay.addWidget(self.lbl_elapsed)

        self.lbl_phase = QLabel("")
        self.lbl_phase.setProperty("role", "subtitle")
        lay.addWidget(self.lbl_phase)

        # 分支按钮：点击弹出菜单复制分支名
        self.btn_branch = QToolButton()
        self.btn_branch.setText("")
        self.btn_branch.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_branch.setToolTip("点击复制分支名")
        # 默认色（无 git 信息时不显示，初始 QSS 用次级灰）
        self.btn_branch.setStyleSheet(
            _status_btn_base_qss() + f"QToolButton {{ color:{FG_SECONDARY}; }}"
        )
        self._branch_menu = _BranchMenu(self.btn_branch)
        self._branch_menu.aboutToShow.connect(self._populate_branch_menu)
        self._branch_menu.localDeleteRequested.connect(self._do_delete_local_branch)
        self._branch_menu.remoteCheckoutRequested.connect(self._do_checkout_remote_branch)
        self.btn_branch.setMenu(self._branch_menu)
        self.btn_branch.setVisible(False)   # 没有 git 信息时隐藏
        lay.addWidget(self.btn_branch)

        # 改动按钮：紧贴分支按钮，显示本地改动文件数，点击打开 GitViewer
        self.btn_changes = QToolButton()
        self.btn_changes.setText("")
        self.btn_changes.setToolTip("查看当前分支改动文件")
        self.btn_changes.clicked.connect(self.open_git_viewer)
        self.btn_changes.setVisible(False)
        lay.addWidget(self.btn_changes)

        return bar

    # ---- 动作 ----

    def _run_profile(self, prof: RunProfile) -> None:
        if self.service_controller.runner_busy(self.runner):
            ret = QMessageBox.question(
                self, "已有任务在运行",
                "当前已有任务在运行，需要先停止再执行吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
            # 事件驱动重启：记下待启动 profile，停止完成后由 _on_finished 接力，
            # 不赌固定延时——慢停止时既不会重复弹框，也不会叠加定时器。
            self._pending_restart = prof
            self._stop()
            return

        # 启动前强制编译
        if prof.pre_compile:
            compile_prof = self._find_compile_profile()
            if compile_prof and compile_prof.name != prof.name:
                self._pending_after_compile = prof
                if not self._start_profile(compile_prof):
                    self._pending_after_compile = None
                return
        self._start_profile(prof)

    def _find_compile_profile(self) -> RunProfile | None:
        for p in self.project_meta.profiles:
            if p.kind == "compile":
                return p
        return None

    def _start_profile(self, prof: RunProfile, enforce_guard: bool = True) -> bool:
        if prof is None:
            return False
        action_log.info("[GUI] 启动服务 project=%s profile=%s", self.project_meta.name, prof.name)
        if enforce_guard:
            conflict = self.service_controller.profile_start_error(prof)
            if conflict:
                message = conflict.get("error", "operation blocked")
                running = conflict.get("running_modules") or []
                if running:
                    message += f"：{', '.join(running)}"
                self.log.append_line("stderr", f"[已阻止] {message}")
                notify.notify_info("操作已阻止", message)
                return False
        self._current_profile = prof
        self.log.begin_run(f"{self.project_meta.name}-{prof.name}")
        runtime = self.project_meta.runtime_for_profile(prof.name)
        ctx = RunContext(
            cwd=runtime.cwd if runtime is not None else self.project_meta.path,
            env={},
            jvm_opts="-Xmx768m -Xms256m",
            spring_profile="",
            extra_args=[],
        )
        generation = self.service_controller.begin_launch(None) if prof.kind == "run" else None
        ok = self.runner.start(list(prof.command), ctx)
        if not ok:
            self.log.append_line("stderr", f"[启动失败] 无法启动 {prof.label}")
            if generation is not None:
                self.service_controller.launch_failed(None, generation)
            self.log.end_run()
            self._current_profile = None
            return False
        if generation is not None:
            self.service_controller.launch_started(None, generation, self.runner)
        return True

    def _stop(self) -> bool:
        action_log.info("[GUI] 停止服务 project=%s", self.project_meta.name)
        self._pending_after_compile = None
        runtime = None
        if self._current_profile is not None:
            runtime = self.project_meta.runtime_for_profile(self._current_profile.name)
        if runtime is None and len(self.project_meta.runtime_units) == 1:
            runtime = self.project_meta.runtime_units[0]
        if runtime is not None and runtime.stop_command:
            return self._start_runtime_stop(runtime, self.runner, None)
        if self.service_controller.runner_busy(self.runner):
            self.runner.stop()
            return True
        return False

    def _on_main_clicked(self) -> None:
        """主按钮点击：根据当前状态决定启动 or 停止"""
        if self.project_meta.project_type == "nginx":
            status = self._detect_nginx_status()
            if status.running or self.service_controller.runner_busy(self.runner):
                self._stop_nginx_external()
            else:
                self._start_nginx()
            self._refresh_status_row()
            return
        if self.service_controller.runner_busy(self.runner):
            self._stop()
        elif self._primary_profile:
            self._run_profile(self._primary_profile)

    def _update_main_button(self, state: str) -> None:
        btn = self.btn_main
        if state == "running":
            btn.setText("⏹  停止")
            btn.setProperty("role", "danger")
            btn.setToolTip("停止当前运行的进程（递归杀进程树）")
            btn.setEnabled(True)
        elif state == "starting":
            btn.setText("⏳  启动中...")
            btn.setToolTip("正在启动...")
            btn.setEnabled(False)
        elif state == "stopping":
            btn.setText("⏳  停止中...")
            btn.setToolTip("正在停止...")
            btn.setEnabled(False)
        else:   # idle
            p = self._primary_profile
            if p:
                btn.setText(f"{p.icon}  {p.label}" if p.icon else p.label)
                btn.setToolTip(p.description or " ".join(p.command))
            else:
                btn.setText("▶  启动")
                btn.setToolTip("(未识别出主启动命令)")
                btn.setEnabled(False)
                return
            btn.setProperty("role", "primary")
            btn.setEnabled(True)
        btn.style().unpolish(btn)
        btn.style().polish(btn)

    def _restart(self) -> None:
        action_log.info("[GUI] 重启服务 project=%s", self.project_meta.name)
        primary = next((p for p in self.project_meta.profiles if p.primary), None)
        if not primary:
            return
        if self.service_controller.runner_busy(self.runner):
            # 同 _run_profile：事件驱动，停止完成后由 _on_finished 接力启动
            self._pending_restart = primary
            self._stop()
        else:
            self._run_profile(primary)

    # ---- 多运行单元动作 ----

    def _find_module_run_profile(self, module: str) -> RunProfile | None:
        """运行单元 ID → 对应的启动 profile。"""
        unit = self.project_meta.runtime_unit(module)
        if unit is not None and unit.start_profile:
            profile = next(
                (item for item in self.project_meta.profiles
                 if item.name == unit.start_profile),
                None,
            )
            if profile is not None:
                return profile
        # 兼容旧 Spring 元数据。
        # 子模块：profile.name == f"bootRun:{module}"
        for p in self.project_meta.profiles:
            if p.name == f"bootRun:{module}":
                return p
        # 主模块：primary=True 且 command 里含 :{module}:bootRun
        marker = f":{module}:bootRun"
        for p in self.project_meta.profiles:
            if p.primary and p.kind == "run":
                if any(marker in arg for arg in p.command):
                    return p
        return None

    def _ensure_module_runner(self, module: str) -> ProcessRunner:
        r = self.module_runners.get(module)
        if r is not None:
            return r
        r = ProcessRunner(self)
        r.outputLine.connect(lambda s, l, m=module: self._on_module_output(m, s, l))
        r.stateChanged.connect(lambda st, m=module: self._on_module_state(m, st))
        r.finished.connect(lambda c, m=module: self._on_module_finished(m, c))
        self.module_runners[module] = r
        return r

    def _ensure_module_log_tab(self, module: str) -> LogWidget:
        """第一次启动该模块时创建独立 LogWidget 并挂到中心 Tab 容器"""
        lw = self.module_logs.get(module)
        if lw is not None:
            return lw
        lw = LogWidget()
        lw.set_max_blocks(self.config.max_log_blocks)
        lw.fileJumpRequested.connect(self._jump_to_file)
        self.module_logs[module] = lw
        idx = self.center_tabs.addTab(lw, f"📋 {module}")
        self.center_tabs.setCurrentIndex(idx)
        return lw

    def _start_module(self, module: str, silent: bool = False) -> bool:
        """启动单个模块。

        silent=True 时用于 CLI：不弹任何确认框。若该模块已被一个 mini-ide
        没在管的外部进程占着（典型：跨重启后靠端口感知到的旧进程），先按 PID
        把它停掉、等端口释放，再用 mini-ide 自己启动——这样起来的就是 mini-ide
        亲手管理的进程，界面正常显示「运行中」，不会再留外部标记。
        """
        action_log.info("[GUI] 启动模块 project=%s module=%s", self.project_meta.name, module)
        conflict = self.service_controller.service_start_error()
        if conflict:
            message = conflict.get("error", "operation blocked")
            self.log.append_line("stderr", f"[已阻止] {module}: {message}")
            if not silent:
                notify.notify_info("操作已阻止", message)
            return False
        prof = self._find_module_run_profile(module)
        if not prof:
            if not silent:
                QMessageBox.warning(self, "启动失败", f"未找到模块「{module}」的启动命令")
            return False
        runner = self._ensure_module_runner(module)
        if self.service_controller.runner_busy(runner):
            return False
        # 已有外部进程占着该模块
        if module in self._module_external_pids:
            pid = self._module_external_pids[module]
            if silent:
                # CLI：静默接管——杀掉旧的外部进程并等端口释放，避免新进程撞端口秒退
                if not self._takeover_external(module):
                    return False
            else:
                ret = QMessageBox.question(
                    self, "模块已在外部运行",
                    f"模块「{module}」似乎已在 mini-ide 之外运行（PID {pid}）。\n"
                    f"仍要再启动一个实例吗？（可能端口冲突）",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if ret != QMessageBox.StandardButton.Yes:
                    return False
                self._module_external_pids.pop(module, None)
        log_w = self._ensure_module_log_tab(module)
        log_w.begin_run(f"{self.project_meta.name}-{module}")
        unit = self.project_meta.runtime_unit(module)
        ctx = RunContext(
            cwd=unit.cwd if unit is not None else self.project_meta.path,
            env={},
            jvm_opts=(
                "-Xmx768m -Xms256m"
                if self.project_meta.project_type.startswith("spring-") else ""
            ),
            spring_profile="",
            extra_args=[],
        )
        # 上次运行抓到的端口失效，等本次启动日志里重新抓
        self._module_ports.pop(module, None)
        if self.service_panel:
            self.service_panel.update_state(module, STATE_STARTING)
        generation = self.service_controller.begin_launch(module)
        self._module_current[module] = prof
        ok = runner.start(list(prof.command), ctx)
        if not ok:
            log_w.append_line("stderr", f"[启动失败] 无法启动 {module}")
            self.service_controller.launch_failed(module, generation)
            self._module_current.pop(module, None)
            log_w.end_run()
            return False
        self.service_controller.launch_started(module, generation, runner)
        return True

    def _takeover_external(self, module: str) -> bool:
        """杀掉占着该模块的外部进程并等端口真正释放。返回端口是否已空出。

        给 CLI 静默启动/重启用：旧进程不是 mini-ide 亲手起的（只靠扫端口感知到），
        重启时若不先停掉它，新进程会撞端口/gradle 冲突，瞬间以错误码退出，结果旧的
        没停、新的没起，界面继续挂着外部标记。
        """
        from src.core.process_runner import kill_pid, is_port_listening
        port = self._module_ports.get(module)
        pid = self._module_external_pids.pop(module, None)
        if pid:
            kill_pid(pid)
        # 等端口真正释放（最多 ~8s）。用实时直查而非后台快照：kill 后快照会被清空，
        # 依赖它会被「清空 = 已释放」骗到。期间 processEvents 让 UI 不至于完全冻结。
        if port:
            import time
            from PySide6.QtCore import QCoreApplication, QEventLoop
            deadline = time.time() + 8.0
            while time.time() < deadline:
                if not is_port_listening(port):
                    return True
                QCoreApplication.processEvents(
                    QEventLoop.ProcessEventsFlag.AllEvents, 100
                )
            return not is_port_listening(port)
        return True


    def _stop_module(self, module: str, silent: bool = False) -> bool:
        action_log.info("[GUI] 停止模块 project=%s module=%s", self.project_meta.name, module)
        runner = self.module_runners.get(module)
        unit = self.project_meta.runtime_unit(module)
        if unit is not None and unit.stop_command:
            from src.core.process_runner import is_port_listening

            port_active = bool(unit.expected_port and is_port_listening(unit.expected_port))
            if (runner and self.service_controller.runner_busy(runner)) or port_active:
                return self._start_runtime_stop(unit, runner, module)
        if runner and self.service_controller.runner_busy(runner):
            runner.stop()
            return True
        # 外部进程（非 mini-ide 拉起）：按感知到的 pid 结束。
        # GUI 操作需用户确认避免误杀；CLI（silent）直接停。
        pid = self._module_external_pids.get(module)
        if pid is not None:
            if not silent:
                ret = QMessageBox.question(
                    self, "停止外部进程",
                    f"模块「{module}」是在 mini-ide 之外启动的（PID {pid}）。\n"
                    f"确定要结束该进程及其子进程吗？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if ret != QMessageBox.StandardButton.Yes:
                    return False
            from src.core.process_runner import kill_pid
            if self.service_panel:
                self.service_panel.update_state(module, STATE_STOPPING)
            ok = kill_pid(pid)
            if ok:
                self._module_external_pids.pop(module, None)
                if self.service_panel:
                    self.service_panel.update_state(module, STATE_IDLE)
            else:
                self.log.append_line("stderr", f"[停止失败] 无法结束 {module} (PID {pid})")
                # 刷新会重新感知真实状态
                self._refresh_status_row()
            return ok
        return False

    def _start_runtime_stop(
        self, unit, service_runner: ProcessRunner | None, module: str | None,
    ) -> bool:
        """先执行显式 stop；超时后只兜底停止本 IDE 托管的 runner。"""
        key = unit.id
        current = self._runtime_stop_runners.get(key)
        if current is not None and self.service_controller.runner_busy(current):
            return True

        stop_runner = ProcessRunner(self)
        self._runtime_stop_runners[key] = stop_runner
        stop_runner.outputLine.connect(
            lambda stream, line, m=module: self._append_runtime_stop_output(m, stream, line)
        )
        stop_runner.finished.connect(
            lambda code, u=unit, managed=service_runner, m=module, sr=stop_runner:
            self._on_runtime_stop_command_finished(u, managed, m, sr, code)
        )
        if module is not None and self.service_panel:
            self.service_panel.update_state(module, STATE_STOPPING)
        elif module is None:
            self._update_main_button("stopping")
        self._append_runtime_stop_output(module, "meta", f"[停止命令] {unit.name}")
        ok = stop_runner.start(
            list(unit.stop_command),
            RunContext(cwd=unit.cwd, env={}, extra_args=[]),
        )
        if not ok:
            self._runtime_stop_runners.pop(key, None)
            stop_runner.deleteLater()
            if service_runner is not None and self.service_controller.runner_busy(service_runner):
                service_runner.stop()
                return True
            self._append_runtime_stop_output(module, "stderr", "[停止失败] 无法启动停止命令")
            return False
        QTimer.singleShot(
            RUNTIME_STOP_COMMAND_TIMEOUT_MS,
            lambda k=key, sr=stop_runner: self._timeout_runtime_stop_command(k, sr),
        )
        return True

    def _append_runtime_stop_output(
        self, module: str | None, stream: str, line: str,
    ) -> None:
        target = self.module_logs.get(module) if module is not None else self.log
        (target or self.log).append_line(stream, line)

    def _timeout_runtime_stop_command(self, key: str, stop_runner: ProcessRunner) -> None:
        if self._runtime_stop_runners.get(key) is not stop_runner:
            return
        if self.service_controller.runner_busy(stop_runner):
            self.log.append_line("stderr", f"[停止命令超时] {key}，开始收尾")
            stop_runner.stop(timeout_ms=1000)

    def _on_runtime_stop_command_finished(
        self, unit, service_runner: ProcessRunner | None, module: str | None,
        stop_runner: ProcessRunner, exit_code: int,
    ) -> None:
        if self._runtime_stop_runners.get(unit.id) is stop_runner:
            self._runtime_stop_runners.pop(unit.id, None)
        stop_runner.deleteLater()
        if exit_code not in (0,):
            self._append_runtime_stop_output(
                module, "stderr", f"[停止命令失败] 退出码 {exit_code}，使用托管进程兜底",
            )
            if service_runner is not None and self.service_controller.runner_busy(service_runner):
                service_runner.stop()
            return
        deadline = time.time() + RUNTIME_STOP_GRACE_MS / 1000.0
        QTimer.singleShot(
            100,
            lambda u=unit, managed=service_runner, m=module, d=deadline:
            self._wait_runtime_stop_grace(u, managed, m, d),
        )

    def _wait_runtime_stop_grace(
        self, unit, service_runner: ProcessRunner | None,
        module: str | None, deadline: float,
    ) -> None:
        from src.core.process_runner import is_port_listening

        managed_active = bool(
            service_runner is not None
            and self.service_controller.runner_busy(service_runner)
        )
        port_active = bool(
            unit.expected_port and is_port_listening(unit.expected_port)
        )
        if not managed_active and not port_active:
            if module is not None and self.service_panel:
                self.service_panel.update_state(module, STATE_IDLE)
            elif module is None:
                self._update_main_button(self.runner.state())
            self._refresh_status_row()
            return
        if time.time() < deadline:
            QTimer.singleShot(
                100,
                lambda u=unit, managed=service_runner, m=module, d=deadline:
                self._wait_runtime_stop_grace(u, managed, m, d),
            )
            return
        if managed_active:
            self._append_runtime_stop_output(
                module, "meta", "[停止命令已执行] 服务未退出，停止托管进程树",
            )
            service_runner.stop()
        elif port_active:
            self._append_runtime_stop_output(
                module, "stderr",
                f"[停止未完成] 端口 {unit.expected_port} 仍被非托管进程占用，未执行强杀",
            )

    def _start_all_modules(self, silent: bool = False) -> bool:
        """按依赖分组启动所有未运行的运行单元。

        silent=True（CLI 全部启动）：对靠端口感知到的外部进程也接管——先杀旧的
        再用 mini-ide 启动，使其纳入本实例管理。界面操作时仍跳过外部进程不重复拉起。
        """
        conflict = self.service_controller.service_start_error()
        if conflict:
            message = conflict.get("error", "operation blocked")
            self.log.append_line("stderr", f"[已阻止] {message}")
            if not silent:
                notify.notify_info("操作已阻止", message)
            return False
        # Spring 服务可感知 IDE 外部启动的 JVM；其他适配器只管理本实例启动的进程。
        if self.project_meta.spring_boot_modules:
            self._detect_and_apply_external()
        from src.core.runtime_units import runtime_start_groups

        try:
            groups = runtime_start_groups(self.project_meta.runtime_units)
        except ValueError as exc:
            self.log.append_line("stderr", f"[启动失败] {exc}")
            return False
        if not groups:
            return False
        self._runtime_start_generation += 1
        return self._start_runtime_group(
            groups, 0, silent=silent, generation=self._runtime_start_generation,
        )

    def _start_runtime_group(
        self, groups, index: int, *, silent: bool, generation: int,
    ) -> bool:
        if generation != self._runtime_start_generation:
            return False
        group = groups[index]
        all_ok = True
        for unit in group:
            runner = self.module_runners.get(unit.id)
            if runner and self.service_controller.runner_busy(runner):
                continue
            if unit.id in self._module_external_pids and not silent:
                continue
            all_ok = self._start_module(unit.id, silent=silent) and all_ok
        if not all_ok:
            self.log.append_line("stderr", "[启动中止] 当前依赖组存在启动失败项")
            return False
        if index + 1 >= len(groups):
            return True
        launch_generations = self.service_controller.snapshot_launch_generations(None)
        deadline = time.time() + 90.0
        QTimer.singleShot(250, lambda: self._wait_runtime_group_ready(
            groups, index, silent=silent, generation=generation,
            launch_generations=launch_generations, deadline=deadline,
        ))
        return True

    def _wait_runtime_group_ready(
        self, groups, index: int, *, silent: bool, generation: int,
        launch_generations: dict[str, int | None], deadline: float,
    ) -> None:
        if generation != self._runtime_start_generation:
            return
        pending: list[str] = []
        for unit in groups[index]:
            ok, detail = self.service_controller.module_health_ok(
                unit.id, launch_generations.get(unit.id),
            )
            runner = self.module_runners.get(unit.id)
            # 无端口、无健康地址的 Worker/任务以托管进程稳定运行为就绪依据。
            if (
                not ok and unit.expected_port is None and not unit.health_check_path
                and runner is not None and runner.is_running()
            ):
                ok = True
            if not ok and detail.get("error"):
                self.log.append_line(
                    "stderr", f"[依赖启动失败] {unit.id}: {detail['error']}",
                )
                return
            if not ok:
                pending.append(unit.id)
        if not pending:
            self._start_runtime_group(
                groups, index + 1, silent=silent, generation=generation,
            )
            return
        if time.time() >= deadline:
            self.log.append_line(
                "stderr", f"[依赖启动超时] 等待就绪：{', '.join(pending)}",
            )
            return
        QTimer.singleShot(250, lambda: self._wait_runtime_group_ready(
            groups, index, silent=silent, generation=generation,
            launch_generations=launch_generations, deadline=deadline,
        ))

    def _stop_all_modules(self, silent: bool = False) -> int:
        """停止所有运行中的模块。

        两类进程都要停：
          1. mini-ide 自己拉起且尚未完全停止的 runner → runner.stop()
          2. CLI/AI/终端在外部起的（_module_external_pids）→ 按 PID kill_pid

        以前只停了第 1 类，导致「CLI 启动的服务点全部停止停不掉」。
        silent=True（CLI 调用）静默直接停；GUI 点按钮时若有外部进程，先弹一次
        汇总确认框（避免误杀无关进程），确认后一并 kill。
        """
        from src.core.process_runner import kill_pid
        self._runtime_start_generation += 1

        # 反向依赖顺序停止，避免先停依赖再停调用方。
        from src.core.runtime_units import runtime_start_groups
        try:
            stop_order = [
                unit.id
                for group in reversed(runtime_start_groups(self.project_meta.runtime_units))
                for unit in group
            ]
        except ValueError:
            stop_order = [unit.id for unit in reversed(self.project_meta.runtime_units)]
        stopped = 0
        for module in stop_order:
            runner = self.module_runners.get(module)
            unit = self.project_meta.runtime_unit(module)
            should_stop = bool(
                runner is not None and self.service_controller.runner_busy(runner)
            )
            if unit is not None and unit.stop_command and unit.expected_port:
                from src.core.process_runner import is_port_listening

                should_stop = should_stop or is_port_listening(unit.expected_port)
            if should_stop and self._stop_module(module, silent=silent):
                stopped += 1

        # 再处理外部进程
        external = {m: pid for m, pid in self._module_external_pids.items()
                    if pid is not None}
        if not external:
            return stopped

        if not silent:
            names = "、".join(sorted(external))
            ret = QMessageBox.question(
                self, "停止外部进程",
                f"以下模块是在 mini-ide 之外启动的（CLI / 终端 / IDE）：\n"
                f"{names}\n\n确定要一并结束这些进程及其子进程吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                return stopped

        for module, pid in external.items():
            if self.service_panel:
                self.service_panel.update_state(module, STATE_STOPPING)
            if kill_pid(pid):
                self._module_external_pids.pop(module, None)
                stopped += 1
                if self.service_panel:
                    self.service_panel.update_state(module, STATE_IDLE)
            else:
                self.log.append_line("stderr", f"[停止失败] 无法结束 {module} (PID {pid})")
        # 刷新一次，把真实状态同步回面板
        self._refresh_status_row()
        return stopped

    def clear_all_logs(self) -> None:
        """清空当前项目内所有控制台日志，不影响正在运行的进程。"""
        widgets = [self.log, *self.module_logs.values(), *self._script_logs.values()]
        seen: set[int] = set()
        cleared = 0
        for widget in widgets:
            marker = id(widget)
            if marker in seen:
                continue
            seen.add(marker)
            widget.clear()
            cleared += 1
        notify.notify_success("日志已清空", f"已清空 {cleared} 个控制台")

    def _focus_module_log(self, module: str) -> None:
        lw = self.module_logs.get(module)
        if lw is not None:
            idx = self.center_tabs.indexOf(lw)
            if idx >= 0:
                self.center_tabs.setCurrentIndex(idx)
                return
        pid = self._module_external_pids.get(module)
        if pid is not None:
            port = self._module_ports.get(module)
            port_text = f"，端口 {port}" if port else ""
            self.log.mark_external_running(module, pid=pid, port=port)
            self.log.append_line(
                "meta",
                f"[提示] {module} 正在 mini-ide 之外运行（PID {pid}{port_text}）。"
                "当前 IDE 没有这次启动的日志上下文；可停止后由 mini-ide 重新启动以接管日志。",
            )
            self._reveal_log_tab()
            self.center_tabs.setCurrentWidget(self.log)
            return
        # 模块还没启动过：提示用户先启动
        self.log.append_line(
            "meta",
                f"[提示] {module} 还未启动；点击其右侧的 ▶ 按钮。",
        )

    def focus_runtime_unit(self, unit_id: str) -> None:
        """供聚合项目树定位运行单元；详情仍复用完整 ProjectTab。"""
        if self.project_meta.runtime_unit(unit_id) is not None:
            self._focus_module_log(unit_id)

    def _on_module_output(self, module: str, stream: str, line: str) -> None:
        self.service_controller.record_service_output(module, line)
        lw = self.module_logs.get(module)
        if lw is None:
            return
        lw.append_line(stream, line)
        # 抓真实监听端口；只抓第一次命中（HTTP 端口通常先于 management 端口出现）
        if module not in self._module_ports:
            m = _RE_BOOT_LISTEN_PORT.search(line)
            if m:
                self._module_ports[module] = int(m.group(1))
                r = self.module_runners.get(module)
                # 必须确认 runner 仍在跑——_on_finished 里 flush 残留日志会
                # 在切 idle 前 emit 这行，没这层守卫会把 panel 刷回 RUNNING
                if r and r.is_running() and self.service_panel:
                    self.service_panel.update_state(
                        module, STATE_RUNNING,
                        port=self._module_ports[module],
                        elapsed_seconds=r.elapsed_seconds(),
                    )

    def _on_module_state(self, module: str, state: str) -> None:
        if self.service_panel:
            mapping = {
                "idle":     STATE_IDLE,
                "running":  STATE_RUNNING,
                "stopping": STATE_STOPPING,
            }
            panel_state = mapping.get(state, STATE_IDLE)
            r = self.module_runners.get(module)
            elapsed = r.elapsed_seconds() if r else 0.0
            self.service_panel.update_state(
                module, panel_state,
                port=self._module_ports.get(module),
                elapsed_seconds=elapsed,
            )
        self.statusChanged.emit()

    def _on_module_finished(self, module: str, exit_code: int) -> None:
        self.service_controller.launch_finished(module, exit_code)
        lw = self.module_logs.get(module)
        if lw is not None:
            lw.end_run()
        self._module_current.pop(module, None)
        if exit_code not in (0, -1):
            notify.notify_error(
                f"{self.project_meta.name} / {module} 异常停止",
                f"服务非正常退出（错误码 {exit_code}），点日志查看原因",
            )
        # 如果是"关 tab 触发的停止"，在这里把 tab 真正移除
        if module in self._closing_modules:
            self._closing_modules.discard(module)
            self._remove_module_tab(module)

    def _force_close_module_if_stuck(self, module: str) -> None:
        """关 tab 触发 stop 10 秒后的兜底：若还在 _closing_modules 说明进程没正常退
        （SIGTERM 被忽略 / QProcess.finished 丢失），强制清理 tab。"""
        if module not in self._closing_modules:
            return
        self._closing_modules.discard(module)
        self.log.append_line(
            "meta",
            f"[警告] 模块 {module} 停止超时（10s）；关闭 tab 但进程可能残留，"
            f"请用「🔌 端口占用查询」或任务管理器确认",
        )
        self._remove_module_tab(module)

    def _remove_module_tab(self, module: str) -> None:
        """把某模块的 tab + runner + log 全部清理掉"""
        lw = self.module_logs.pop(module, None)
        if lw is not None:
            idx = self.center_tabs.indexOf(lw)
            if idx >= 0:
                self.center_tabs.removeTab(idx)
            lw.deleteLater()
        runner = self.module_runners.pop(module, None)
        if runner is not None:
            runner.deleteLater()
        self._module_current.pop(module, None)
        self._module_ports.pop(module, None)
        if self.service_panel:
            self.service_panel.update_state(module, STATE_IDLE)

    def _module_of_tab(self, widget) -> str | None:
        for m, lw in self.module_logs.items():
            if lw is widget:
                return m
        return None

    def _any_module_running(self) -> bool:
        return any(self.service_controller.runner_busy(r) for r in self.module_runners.values())

    def service_states(self, refresh_external: bool = True) -> list[ServiceState]:
        """返回当前项目的统一服务状态快照。

        这是 GUI、CLI、退出检查共用的状态入口。旧的 running_service_items()
        保留给调用方兼容，但内部不再手写散落 dict。
        """
        if refresh_external and self._is_multi_module and self.project_meta.spring_boot_modules:
            try:
                self._detect_and_apply_external()
            except Exception:
                log.exception("刷新外部进程感知失败")
        nginx_status = None
        if refresh_external and self.project_meta.project_type == "nginx":
            try:
                nginx_status = self._detect_nginx_status()
            except Exception:
                log.exception("刷新 nginx 进程感知失败")
        elif self.project_meta.project_type == "nginx":
            nginx_status = self._nginx_status

        checked_at = int(time.time())
        states: list[ServiceState] = []
        single_unit = (
            self.project_meta.runtime_units[0]
            if not self._is_multi_module and self.project_meta.runtime_units else None
        )
        single_module = single_unit.id if single_unit is not None else self.project_meta.name
        single_port = (
            single_unit.expected_port if single_unit is not None
            else self.project_meta.default_port
        )

        if self.project_meta.project_type == "nginx" and nginx_status is not None and nginx_status.running:
            port = nginx_status.ports[0] if nginx_status.ports else None
            managed = self.service_controller.runner_busy(self.runner)
            pid = None
            if managed and self.runner._proc:
                pid = int(self.runner._proc.processId())
            states.append(ServiceState(
                project=self.project_meta.name,
                module=single_module,
                kind=KIND_PROJECT,
                state=SERVICE_RUNNING_MANAGED if managed else SERVICE_RUNNING_EXTERNAL,
                source=SOURCE_MANAGED_RUNNER if managed else SOURCE_EXTERNAL_DETECTOR,
                pid=(pid if pid and pid > 0 else nginx_status.master_pid),
                port=port,
                ports=nginx_status.ports,
                expected_port=None,
                log_attached=managed,
                checked_at=checked_at,
                reason="当前 mini-ide 启动，日志上下文完整" if managed else "检测到本目录的 nginx.exe 进程",
            ))
        elif self.runner.state() == "starting":
            states.append(ServiceState(
                project=self.project_meta.name,
                module=single_module,
                kind=KIND_PROJECT,
                state=SERVICE_STARTING,
                source=SOURCE_MANAGED_RUNNER,
                pid=self.runner.process_id(),
                port=single_port,
                expected_port=single_port,
                log_attached=True,
                checked_at=checked_at,
                reason="正在启动",
            ))
        elif self.runner.is_running():
            pid = None
            if self.runner._proc:
                pid = int(self.runner._proc.processId())
            states.append(ServiceState(
                project=self.project_meta.name,
                module=single_module,
                kind=KIND_PROJECT,
                state=SERVICE_RUNNING_MANAGED,
                source=SOURCE_MANAGED_RUNNER,
                pid=pid if pid and pid > 0 else None,
                port=single_port,
                expected_port=single_port,
                log_attached=True,
                checked_at=checked_at,
                reason="当前 mini-ide 启动，日志上下文完整",
            ))
        elif self.runner.state() == "stopping":
            states.append(ServiceState(
                project=self.project_meta.name,
                module=single_module,
                kind=KIND_PROJECT,
                state=SERVICE_STOPPING,
                source=SOURCE_MANAGED_RUNNER,
                port=single_port,
                expected_port=single_port,
                log_attached=True,
                checked_at=checked_at,
                reason="正在停止",
            ))
        elif not self._is_multi_module:
            states.append(ServiceState(
                project=self.project_meta.name,
                module=single_module,
                kind=KIND_PROJECT,
                state=SERVICE_STOPPED,
                source=SOURCE_NONE,
                port=single_port,
                expected_port=single_port,
                log_attached=False,
                checked_at=checked_at,
                reason="未检测到运行进程",
            ))

        for unit in self.project_meta.runtime_units if self._is_multi_module else ():
            mod_name = unit.id
            expected_port = unit.expected_port
            runner = self.module_runners.get(mod_name)
            port = self._module_ports.get(mod_name) or expected_port
            if runner and runner.state() == "starting":
                states.append(ServiceState(
                    project=self.project_meta.name,
                    module=mod_name,
                    kind=KIND_MODULE,
                    state=SERVICE_STARTING,
                    source=SOURCE_MANAGED_RUNNER,
                    pid=runner.process_id(),
                    port=port,
                    expected_port=expected_port,
                    log_attached=mod_name in self.module_logs,
                    checked_at=checked_at,
                    reason="正在启动",
                ))
            elif runner and runner.is_running():
                pid = int(runner._proc.processId()) if runner._proc else None
                states.append(ServiceState(
                    project=self.project_meta.name,
                    module=mod_name,
                    kind=KIND_MODULE,
                    state=SERVICE_RUNNING_MANAGED,
                    source=SOURCE_MANAGED_RUNNER,
                    pid=pid if pid and pid > 0 else None,
                    port=port,
                    expected_port=expected_port,
                    log_attached=mod_name in self.module_logs,
                    checked_at=checked_at,
                    reason="当前 mini-ide 启动，日志上下文完整",
                ))
            elif runner and runner.state() == "stopping":
                states.append(ServiceState(
                    project=self.project_meta.name,
                    module=mod_name,
                    kind=KIND_MODULE,
                    state=SERVICE_STOPPING,
                    source=SOURCE_MANAGED_RUNNER,
                    port=port,
                    expected_port=expected_port,
                    log_attached=mod_name in self.module_logs,
                    checked_at=checked_at,
                    reason="正在停止",
                ))
            elif mod_name in self._module_external_pids:
                pid = self._module_external_pids.get(mod_name)
                states.append(ServiceState(
                    project=self.project_meta.name,
                    module=mod_name,
                    kind=KIND_MODULE,
                    state=SERVICE_RUNNING_EXTERNAL,
                    source=SOURCE_EXTERNAL_DETECTOR,
                    pid=pid if pid and pid > 0 else None,
                    port=port,
                    expected_port=expected_port,
                    log_attached=False,
                    checked_at=checked_at,
                    reason="外部进程，当前 IDE 没有启动日志上下文",
                ))
            else:
                states.append(ServiceState(
                    project=self.project_meta.name,
                    module=mod_name,
                    kind=KIND_MODULE,
                    state=SERVICE_STOPPED,
                    source=SOURCE_NONE,
                    port=port,
                    expected_port=expected_port,
                    log_attached=mod_name in self.module_logs,
                    checked_at=checked_at,
                    reason="未检测到运行进程",
                ))

        for key, runner in self._script_runners.items():
            if runner.state() == "starting":
                states.append(ServiceState(
                    project=self.project_meta.name,
                    module=Path(key).name,
                    kind=KIND_SCRIPT,
                    state=SERVICE_STARTING,
                    source=SOURCE_SCRIPT_RUNNER,
                    pid=runner.process_id(),
                    checked_at=checked_at,
                    reason="脚本进程正在启动",
                ))
            elif runner.is_running():
                pid = int(runner._proc.processId()) if runner._proc else None
                states.append(ServiceState(
                    project=self.project_meta.name,
                    module=Path(key).name,
                    kind=KIND_SCRIPT,
                    state=SERVICE_RUNNING_MANAGED,
                    source=SOURCE_SCRIPT_RUNNER,
                    pid=pid if pid and pid > 0 else None,
                    checked_at=checked_at,
                    reason="当前 mini-ide 启动的脚本进程",
                ))
            elif runner.state() == "stopping":
                states.append(ServiceState(
                    project=self.project_meta.name,
                    module=Path(key).name,
                    kind=KIND_SCRIPT,
                    state=SERVICE_STOPPING,
                    source=SOURCE_SCRIPT_RUNNER,
                    checked_at=checked_at,
                    reason="脚本进程正在停止",
                ))
        return states

    def running_service_items(self, refresh_external: bool = True) -> list[dict]:
        """返回当前项目仍在运行/停止中的服务清单，供退出检查和 CLI quit 复用。"""
        return running_items(self.service_states(refresh_external=refresh_external))

    def stop_all_services(self, include_external: bool = True, silent: bool = True) -> int:
        """停止当前项目内所有运行服务/脚本，返回发起停止的数量。"""
        before = self.running_service_items(refresh_external=include_external)
        count = 0
        single_unit = (
            self.project_meta.runtime_units[0]
            if not self._is_multi_module and self.project_meta.runtime_units else None
        )
        detached_configured = False
        if single_unit is not None and single_unit.stop_command and single_unit.expected_port:
            from src.core.process_runner import is_port_listening

            detached_configured = is_port_listening(single_unit.expected_port)
        if self.service_controller.runner_busy(self.runner) or detached_configured:
            count += int(self._stop())
        if include_external and self.project_meta.project_type == "nginx":
            status = self._nginx_status
            if status is not None and status.running and self._stop_nginx_external():
                count += 1
        if self._is_multi_module:
            if include_external:
                count += self._stop_all_modules(silent=silent)
            else:
                for module, runner in list(self.module_runners.items()):
                    if self.service_controller.runner_busy(runner):
                        count += int(self._stop_module(module, silent=silent))
        for runner in list(self._script_runners.values()):
            if self.service_controller.runner_busy(runner):
                runner.stop()
                count += 1
        if include_external:
            count = max(count, len(before))
        return count

    def wait_services_stopped(self, timeout_ms: int = 12000) -> bool:
        """等待当前项目托管/外部服务停止；超时返回 False。"""
        import time
        from PySide6.QtCore import QCoreApplication, QEventLoop
        from src.core.process_runner import is_port_listening

        def runner_busy(runner: ProcessRunner) -> bool:
            if runner.state() != "idle":
                return True
            cleanup_pending = getattr(runner, "stop_cleanup_pending", None)
            return bool(cleanup_pending and cleanup_pending())

        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            active = False
            if runner_busy(self.runner):
                active = True
            if self.project_meta.default_port and is_port_listening(self.project_meta.default_port):
                active = True
            if any(runner_busy(r) for r in self.module_runners.values()):
                active = True
            if any(runner_busy(r) for r in self._runtime_stop_runners.values()):
                active = True
            for unit in self.project_meta.runtime_units:
                mod_name = unit.id
                expected_port = unit.expected_port
                port = self._module_ports.get(mod_name) or expected_port
                if port and is_port_listening(port):
                    active = True
                    break
            if any(runner_busy(r) for r in self._script_runners.values()):
                active = True
            if self.project_meta.spring_boot_modules:
                try:
                    self._detect_and_apply_external()
                except Exception:
                    log.exception("等待停止时刷新外部进程感知失败")
            if self._module_external_pids:
                active = True
            if self.project_meta.project_type == "nginx":
                try:
                    status = self._detect_nginx_status()
                    if status.running:
                        active = True
                except Exception:
                    log.exception("等待停止时刷新 nginx 进程感知失败")
            if not active:
                return True
            QCoreApplication.processEvents(
                QEventLoop.ProcessEventsFlag.AllEvents, 100,
            )
        return False

    # ---- 文件树「▶ 运行脚本」 ----

    def _script_key(self, path: str) -> str:
        """脚本 tab 的去重 key：用 resolve 后的小写绝对路径，跟 _file_panes 一致"""
        try:
            return str(Path(path).resolve()).lower()
        except OSError:
            return path.lower()

    def _script_of_tab(self, widget) -> str | None:
        for k, lw in self._script_logs.items():
            if lw is widget:
                return k
        return None

    def _run_script(self, path: str) -> None:
        """文件树右键「▶ 运行 xxx.bat」：在中心 Tab 容器里开一个独立 LogWidget 跑脚本，
        stdout/stderr 都灌进 tab。CREATE_NO_WINDOW 已在 process_runner 里贴上，不会弹黑窗。
        关 tab 等同于停进程（与模块日志 tab 同套机制）。
        """
        p = Path(path)
        if not p.is_file():
            QMessageBox.warning(self, "运行脚本", f"文件不存在：\n{path}")
            return

        key = self._script_key(path)
        existing = self._script_runners.get(key)
        if existing and self.service_controller.runner_busy(existing):
            # 同一脚本已有运行中 tab：聚焦它，不重复启动
            lw = self._script_logs.get(key)
            if lw is not None:
                idx = self.center_tabs.indexOf(lw)
                if idx >= 0:
                    self.center_tabs.setCurrentIndex(idx)
            return

        # 同路径上次运行已结束但 tab 还在：复用 LogWidget，新跑一轮
        lw = self._script_logs.get(key)
        if lw is None:
            lw = LogWidget()
            lw.set_max_blocks(self.config.max_log_blocks)
            lw.fileJumpRequested.connect(self._jump_to_file)
            self._script_logs[key] = lw
            idx = self.center_tabs.addTab(lw, f"▶ {p.name}")
        else:
            # 复用已有 LogWidget：保留上次输出，begin_run 会贴新 banner 区分两轮
            idx = self.center_tabs.indexOf(lw)
        self.center_tabs.setCurrentIndex(idx)

        runner = self._script_runners.get(key)
        if runner is None:
            runner = ProcessRunner(self)
            runner.outputLine.connect(lambda s, l, k=key: self._on_script_output(k, s, l))
            runner.finished.connect(lambda c, k=key: self._on_script_finished(k, c))
            self._script_runners[key] = runner

        # 命令构造：按脚本类型选解释器。
        # .ps1 → PowerShell
        # .bat/.cmd/.exe/其他 → 直接交给 process_runner._split_program 处理
        ext = p.suffix.lower()
        if ext == ".ps1":
            command = ["powershell", "-NoLogo", "-NoProfile",
                       "-ExecutionPolicy", "Bypass", "-File", str(p)]
        else:
            # .bat / .cmd / .exe 都直接交给 _split_program；它会按扩展名走 cmd /c 或直跑
            command = [str(p)]

        lw.begin_run(p.name)
        ctx = RunContext(
            cwd=str(p.parent),     # 用脚本所在目录作为 cwd——nginx 这类相对路径配置才能找到
            env={}, jvm_opts="", spring_profile="", extra_args=[],
        )
        ok = runner.start(command, ctx)
        if not ok:
            lw.append_line("stderr", f"[启动失败] 无法启动 {p.name}")

    def _on_script_output(self, key: str, stream: str, line: str) -> None:
        lw = self._script_logs.get(key)
        if lw is not None:
            lw.append_line(stream, line)

    def _on_script_finished(self, key: str, exit_code: int) -> None:
        lw = self._script_logs.get(key)
        if lw is not None:
            lw.end_run()
        # 关 tab 触发的停止：进程退出后真正清理 tab
        if key in self._closing_scripts:
            self._closing_scripts.discard(key)
            self._remove_script_tab(key)

    def _remove_script_tab(self, key: str) -> None:
        lw = self._script_logs.pop(key, None)
        if lw is not None:
            idx = self.center_tabs.indexOf(lw)
            if idx >= 0:
                self.center_tabs.removeTab(idx)
            lw.deleteLater()
        runner = self._script_runners.pop(key, None)
        if runner is not None:
            runner.deleteLater()

    def _force_close_script_if_stuck(self, key: str) -> None:
        if key not in self._closing_scripts:
            return
        self._closing_scripts.discard(key)
        self.log.append_line(
            "meta",
            f"[警告] 脚本 {key} 停止超时（10s）；关闭 tab 但进程可能残留，"
            f"请用「🔌 端口占用查询」或任务管理器确认",
        )
        self._remove_script_tab(key)

    def _any_script_running(self) -> bool:
        return any(self.service_controller.runner_busy(r) for r in self._script_runners.values())

    # ---- 事件回调 ----

    def _on_output(self, stream: str, line: str) -> None:
        self.service_controller.record_service_output(None, line)
        self.log.append_line(stream, line)
        # 日志里识别到启动完成 marker 后，状态栏贴一次"✓ 启动完成"（仅状态栏，不弹系统通知）
        if not self._startup_phase_marked and self._current_profile and self._current_profile.kind == "run":
            markers = ("Started ", "ready in ", "compiled successfully", "Netty started on port", "Tomcat started on port")
            if any(m in line for m in markers):
                self._startup_phase_marked = True
                self.lbl_phase.setText("✓ 启动完成")
                self.lbl_phase.setStyleSheet(f"color: {COLOR_SUCCESS};")

    def _on_state(self, state: str) -> None:
        # 顶部运行状态圆点/文字已移除；启动完成的 ✓ 提示仍由 lbl_phase 显示，
        # 运行/停止状态主要靠主按钮的形态表达。
        self._update_main_button(state)

        self.statusChanged.emit()

    def _on_finished(self, exit_code: int) -> None:
        prof = self._current_profile
        if prof and prof.kind == "run":
            self.service_controller.launch_finished(None, exit_code)
        self.log.end_run()
        self._current_profile = None

        # 事件驱动重启：进程已真正退出，现在安全地启动待重启的 profile。
        # 用 singleShot(0) 让本次 finished 回调先彻底结束（状态机收尾、UI 刷新），
        # 再在下一个事件循环里启动，避免在回调里重入 runner。
        if self._pending_restart is not None:
            nxt = self._pending_restart
            self._pending_restart = None
            QTimer.singleShot(RESTART_GAP_MS, lambda: self._run_profile(nxt))
            return

        # 启动前编译完成，接着执行主任务
        if prof and prof.kind == "compile" and self._pending_after_compile:
            if exit_code == 0:
                nxt = self._pending_after_compile
                self._pending_after_compile = None
                QTimer.singleShot(COMPILE_THEN_RUN_GAP_MS, lambda: self._start_profile(nxt))
            else:
                self._pending_after_compile = None
                self.log.append_line("stderr", "[已取消] 编译失败，跳过启动")
                notify.notify_error(
                    f"{self.project_meta.name} 编译失败",
                    "已取消启动，点日志查看错误详情",
                )
                return

        # 运行任务异常退出
        if prof and prof.kind == "run" and exit_code not in (0, -1):
            notify.notify_error(
                f"{self.project_meta.name} 异常停止",
                f"服务非正常退出（错误码 {exit_code}），点日志查看原因",
            )
        self._startup_phase_marked = False

    def _detect_external_modules(self) -> dict[str, tuple[int, int]]:
        """从后台端口快照里按命令行匹配各模块，返回 {module: (pid, port)}。

        感知非 mini-ide 启动的进程（AI / 终端 / IDE 起的、或跨重启遗留的孤儿）。
        实际匹配逻辑在 core.external_detector，这里只取端口快照并委托过去。
        只读后台快照，不阻塞主线程。
        """
        from src.core.process_runner import port_snapshot
        return self._external_detector.detect(port_snapshot())

    def _detect_nginx_status(self):
        from src.core.process_runner import port_snapshot
        self._nginx_status = detect_nginx(self.project_meta.path, port_snapshot())
        return self._nginx_status

    def _stop_nginx_external(self) -> bool:
        from src.core.process_runner import kill_pid
        from pathlib import Path
        import time
        status = self._nginx_status or self._detect_nginx_status()
        pid = status.master_pid if status and status.running else None
        if not pid:
            return False
        ok = kill_pid(pid)
        if ok:
            self._nginx_status = None
            deadline = time.time() + 5.0
            while time.time() < deadline:
                if not self._detect_nginx_status().running:
                    pid_file = Path(self.project_meta.path) / "running.pid"
                    try:
                        pid_file.unlink(missing_ok=True)
                    except OSError:
                        pass
                    return True
                QApplication.processEvents()
                time.sleep(0.2)
        return False

    def _start_nginx(self) -> bool:
        if self.project_meta.project_type != "nginx":
            return False
        status = self._detect_nginx_status()
        if status.running:
            return True
        primary = next((p for p in self.project_meta.profiles if p.primary), None)
        if not primary:
            return False
        self.log.begin_run(f"{self.project_meta.name}-nginx")
        ctx = RunContext(cwd=self.project_meta.path, env={}, jvm_opts="", spring_profile="", extra_args=[])
        ok = self.runner.start(list(primary.command), ctx)
        if not ok:
            self.log.append_line("stderr", "[启动失败] 无法启动 Nginx")
            return False
        return True

    def _detect_and_apply_external(self) -> None:
        """刷新 _module_external_pids（不碰面板，仅供批量启动前去重用）。

        同时把感知到的外部端口记入 _module_ports，供 _takeover_external 精确等待
        端口释放。
        """
        external = self._detect_external_modules()
        for mod_name, _path, _port, _cls in self.project_meta.spring_boot_modules:
            r = self.module_runners.get(mod_name)
            if r and self.service_controller.runner_busy(r):
                self._module_external_pids.pop(mod_name, None)
            elif mod_name in external:
                pid, port = external[mod_name]
                self._module_external_pids[mod_name] = pid
                if port:
                    self._module_ports[mod_name] = port
            else:
                self._module_external_pids.pop(mod_name, None)

    def _start_status_polling(self) -> None:
        if self.isVisible():
            self._refresh_status_row()
        self._poll_timer.start()

    def _poll_status_if_visible(self) -> None:
        if not self.isVisible():
            return
        started = time.perf_counter()
        self._refresh_status_row()
        duration_ms = (time.perf_counter() - started) * 1000
        if duration_ms >= 50:
            perf_log.warning(
                "perf op=status-poll duration_ms=%.1f status=slow",
                duration_ms,
            )

    def _start_fetch_polling(self) -> None:
        if not self._is_git_repo:
            return
        self._start_remote_fetch()
        self._fetch_timer.start()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self._poll_status_if_visible)

    def _refresh_status_row(self) -> None:
        if self._is_multi_module:
            # 多模块：顶部只显示 "N/M 运行中"；每行运行时长更新到 ServicePanel
            external = (
                self._detect_external_modules()
                if self.project_meta.spring_boot_modules else {}
            )
            running = 0
            for unit in self.project_meta.runtime_units:
                mod_name = unit.id
                r = self.module_runners.get(mod_name)
                if r and r.state() == "starting":
                    self._module_external_pids.pop(mod_name, None)
                    if self.service_panel:
                        self.service_panel.update_state(
                            mod_name, STATE_STARTING,
                            port=self._module_ports.get(mod_name),
                            elapsed_seconds=r.elapsed_seconds(),
                            reason="正在启动",
                        )
                    continue
                if r and r.is_running():
                    # mini-ide 亲手拉起的进程优先，覆盖外部感知
                    running += 1
                    self._module_external_pids.pop(mod_name, None)
                    if self.service_panel:
                        self.service_panel.update_state(
                            mod_name, STATE_RUNNING,
                            port=self._module_ports.get(mod_name),
                            elapsed_seconds=r.elapsed_seconds(),
                            reason="当前 mini-ide 启动",
                        )
                    continue
                # 非自管理：runner 处于 stopping 时不抢状态，交给状态机回调
                if r and r.state() == "stopping":
                    continue
                if mod_name in external:
                    # 外部进程（AI 或终端启动）——靠端口快照按命令行匹配到
                    running += 1
                    pid, port = external[mod_name]
                    self._module_external_pids[mod_name] = pid
                    if self.service_panel:
                        self.service_panel.update_state(
                            mod_name, STATE_RUNNING_EXTERNAL, port=port,
                            pid=pid, reason="外部进程，当前 IDE 没有启动日志上下文",
                        )
                    continue
                # 既非自管理也无外部进程：清掉外部标记，刷成 IDLE
                self._module_external_pids.pop(mod_name, None)
                if self.service_panel:
                    row = self.service_panel._rows.get(mod_name)
                    if row and row.current_state() in (
                        STATE_RUNNING, STATE_RUNNING_EXTERNAL, STATE_STARTING,
                    ):
                        # 自管理 runner 已停时一并清掉抓到的端口
                        if not r or r.state() == "idle":
                            self._module_ports.pop(mod_name, None)
                        self.service_panel.update_state(mod_name, STATE_IDLE)
            total = len(self.project_meta.runtime_units)
            self.lbl_elapsed.setText(f"已运行 {running}/{total}" if running else "")
        elif self.project_meta.project_type == "nginx":
            try:
                status = self._detect_nginx_status()
            except Exception:
                log.exception("刷新 nginx 状态展示失败")
                status = None
            if status and status.running:
                ports = " ".join(f":{p}" for p in status.ports)
                self.lbl_elapsed.setText(f"运行 {ports}".strip())
                tip = f"PID：{status.master_pid}" if status.master_pid else ""
                if status.conf_path:
                    tip = (tip + "\n" if tip else "") + f"配置：{status.conf_path}"
                self.lbl_elapsed.setToolTip(tip)
                self._update_main_button("running")
            else:
                self.lbl_elapsed.setText("")
                self.lbl_elapsed.setToolTip("")
                self._update_main_button("idle")
        elif self.runner.is_running():
            elapsed = self.runner.elapsed_seconds()
            m, s = divmod(int(elapsed), 60)
            h, m = divmod(m, 60)
            self.lbl_elapsed.setText(f"已运行 {h}:{m:02d}:{s:02d}" if h else f"已运行 {m}:{s:02d}")
        else:
            self.lbl_elapsed.setText("")

        # git 分支 / 改动数 / 文件树染色：全部交给后台 worker 查，
        # 主线程不再同步跑 git 命令（否则大仓库每 3s 冻 UI 几百 ms）。
        # worker 回来后在 _on_git_status_done 里一次性渲染状态栏 + 刷染色。
        self._update_file_tree_git_colors()

    def _render_git_status_row(self, info) -> None:
        """用后台查到的 GitInfo 渲染分支按钮 + 改动数按钮（在主线程回调里调）。"""
        text = git_info.format_status(info)
        if text:
            self.btn_branch.setText(text + "  ▾")
            base = _status_btn_base_qss()
            # behind > 0 → 橙色高亮提示远程有新提交，点开菜单复制分支名
            if info and info.behind > 0:
                self.btn_branch.setStyleSheet(base + f"QToolButton {{ color:{COLOR_WARN}; }}")
                self.btn_branch.setToolTip(
                    f"远程有 {info.behind} 个新提交；点击菜单可复制分支名给 AI"
                )
            else:
                self.btn_branch.setStyleSheet(base + f"QToolButton {{ color:{FG_SECONDARY}; }}")
                self.btn_branch.setToolTip("点击选择要复制的分支名")
            self.btn_branch.setVisible(True)
        else:
            self.btn_branch.setVisible(False)

        # 改动按钮：dirty 时高亮 + 显示数字；干净时灰色「无改动」
        if info and info.branch:
            n = info.changed
            base = _status_btn_base_qss()
            if n > 0:
                self.btn_changes.setText(f"📝 {n} 处改动")
                self.btn_changes.setStyleSheet(base + f"QToolButton {{ color:{COLOR_WARN}; }}")
            else:
                self.btn_changes.setText("📝 无改动")
                self.btn_changes.setStyleSheet(base + f"QToolButton {{ color:{FG_DIM}; }}")
            self.btn_changes.setVisible(True)
        else:
            self.btn_changes.setVisible(False)

    def _update_file_tree_git_colors(self) -> None:
        # 非 git 项目：状态栏隐藏分支/改动按钮，不起 worker
        if not self._is_git_repo:
            self.btn_branch.setVisible(False)
            self.btn_changes.setVisible(False)
            return
        # 防抖：上一轮 worker 还在跑就跳过这一轮（避免堆积）。
        # 回收策略：finished→deleteLater 负责销毁 C++ 对象，done 回调里只把
        # self._git_status_worker 置 None。下一轮看到 None 起新 worker，永远不再
        # 通过该引用访问已被 deleteLater 的旧对象，因此既不泄漏也不会空壳崩溃。
        # （旧实现为怕空壳崩溃而完全不回收，结果每 3s 漏一个 QThread，长开会耗尽句柄。）
        w = self._git_status_worker
        if w is not None:
            try:
                if w.isRunning():
                    return
            except RuntimeError:
                # 极端情况下对象已被回收，直接当作空闲，重新起一轮
                self._git_status_worker = None
        worker = GitStatusWorker(self.project_meta.path, parent=self)
        worker.done.connect(self._on_git_status_done)
        worker.finished.connect(worker.deleteLater)
        self._git_status_worker = worker
        worker.start()

    def _on_git_status_done(
        self, statuses: dict, ignored: set, deleted_by_parent: dict, info,
    ) -> None:
        started = time.perf_counter()
        log.debug(
            f"[git_colors] statuses={len(statuses)}, "
            f"ignored={len(ignored)}, deleted_groups={len(deleted_by_parent)}"
        )
        self._render_git_status_row(info)
        self.file_tree.update_git_status(statuses, ignored, deleted_by_parent)
        duration_ms = (time.perf_counter() - started) * 1000
        if duration_ms >= 20:
            perf_log.info(
                "perf op=git-status-render duration_ms=%.1f files=%d status=done",
                duration_ms, len(statuses),
            )
        # 本轮结束：只在仍是当前 worker 时清引用（避免极端时序下把刚起的新 worker 误置空）。
        # 对象本身由 finished→deleteLater 回收，这里不碰 C++ 生命周期。
        if self.sender() is self._git_status_worker:
            self._git_status_worker = None

    # ---- 文件跳转 ----

    def _jump_to_file(self, path: str, line: int, col: int) -> None:
        # 办公文档：交给系统默认关联程序打开（无行号语义）
        if is_office_doc(path) and open_with_system_default(path):
            return
        # file_open_mode=preview 总是用内置预览；auto 则先试外部编辑器
        if self.config.file_open_mode == "preview":
            self._show_preview(path, line, col)
            return
        ok = open_in_editor(
            path, line=line, column=col,
            editor_cmd=self.config.editor_cmd,
            project_root=self.project_meta.path,
        )
        if not ok:
            # 外部编辑器都不可用，回落到内置预览
            self._show_preview(path, line, col)

    def _show_preview(self, path: str, line: int, col: int) -> None:
        """在中心 tab 区打开文件：同文件已开则聚焦，否则新建 tab（默认编辑模式）"""
        from pathlib import Path
        # 办公文档不进内置预览，直接交给系统默认程序（git 改动/环境面板等入口也会走到这）
        if is_office_doc(path) and open_with_system_default(path):
            return
        p = Path(path)
        if not p.is_absolute():
            from src.util.editor import search_in_project
            found = search_in_project(Path(self.project_meta.path), p.name)
            if found:
                path = str(found)
        self._remember_recent(path)

        key = self._tab_key(path)
        existing = self._file_panes.get(key)
        if existing is not None:
            idx = self.center_tabs.indexOf(existing)
            if idx >= 0:
                self.center_tabs.setCurrentIndex(idx)
                if line > 0:
                    existing.goto_line(line, col)
                existing.setFocus()
                self.file_tree.reveal_path(path)
                return
            # 索引丢失（异常），从字典里清掉走新建分支
            self._file_panes.pop(key, None)

        from src.ui.file_preview import FilePreviewPane
        pane = FilePreviewPane(
            path=path, line=line, column=col,
            project_root=self.project_meta.path,
            config=self.config, parent=self.center_tabs,
        )
        pane.dirtyChanged.connect(
            lambda dirty, pn=pane: self._on_pane_dirty_changed(pn, dirty)
        )
        label = Path(path).name
        try:
            rel = str(Path(path).relative_to(self.project_meta.path))
        except ValueError:
            rel = path
        idx = self.center_tabs.addTab(pane, label)
        self.center_tabs.setTabToolTip(idx, rel)
        self._file_panes[key] = pane
        self.center_tabs.setCurrentIndex(idx)
        pane.setFocus()
        self.file_tree.reveal_path(path)

    def _on_file_created(self, path: str) -> None:
        """文件树新建文件后：直接在 tab 区打开（本来就是默认编辑模式）"""
        self._show_preview(path, 0, 0)

    def _tab_key(self, path: str) -> str:
        from pathlib import Path
        try:
            return str(Path(path).resolve()).lower()
        except OSError:
            return path.lower()

    def _on_pane_dirty_changed(self, pane, dirty: bool) -> None:
        from pathlib import Path
        idx = self.center_tabs.indexOf(pane)
        if idx <= 0:
            return
        name = Path(pane.get_path()).name
        self.center_tabs.setTabText(idx, f"{name} *" if dirty else name)

    def _on_center_tab_changed(self, index: int) -> None:
        if index <= 0:
            return
        widget = self.center_tabs.widget(index)
        if widget and hasattr(widget, "get_path"):
            path = widget.get_path()
            if path:
                self.file_tree.reveal_path(path)

    def _on_tab_close_requested(self, index: int) -> None:
        """× 按钮 / 中键 / Ctrl+W 触发。项目级日志 tab（index==0）不关。

        - 文件预览 tab：flush_save 后直接 removeTab
        - 模块日志 tab：如果进程在跑，弹确认 → 停进程；真正的 tab 移除推迟到 finished 回调里
        """
        if index == 0:
            return
        widget = self.center_tabs.widget(index)

        # 模块日志 tab
        module = self._module_of_tab(widget)
        if module is not None:
            runner = self.module_runners.get(module)
            if runner and self.service_controller.runner_busy(runner):
                ret = QMessageBox.question(
                    self, "关闭模块",
                    f"「{module}」正在运行。关闭此 tab 会停止该模块的进程，继续吗？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if ret != QMessageBox.StandardButton.Yes:
                    return
                self._closing_modules.add(module)
                self._stop_module(module)
                # 不立即 removeTab，等 _on_module_finished 触发清理。
                # 兜底：进程 10 秒还没退出就强制清理 tab（子进程可能卡在 SIGTERM，
                # 或 QProcess.finished 信号丢失；UI 不能永远留个关不掉的 tab）。
                QTimer.singleShot(MODULE_STOP_TIMEOUT_MS, lambda m=module: self._force_close_module_if_stuck(m))
            else:
                self._remove_module_tab(module)
            return

        # 脚本运行 tab（文件树「▶ 运行」启动的 .bat/.cmd/.ps1/.exe）
        script_key = self._script_of_tab(widget)
        if script_key is not None:
            runner = self._script_runners.get(script_key)
            if runner and self.service_controller.runner_busy(runner):
                ret = QMessageBox.question(
                    self, "关闭脚本",
                    f"脚本正在运行。关闭此 tab 会停止该进程，继续吗？\n\n{script_key}",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if ret != QMessageBox.StandardButton.Yes:
                    return
                self._closing_scripts.add(script_key)
                runner.stop()
                QTimer.singleShot(MODULE_STOP_TIMEOUT_MS, lambda k=script_key: self._force_close_script_if_stuck(k))
            else:
                self._remove_script_tab(script_key)
            return

        # 文件预览 tab
        from src.ui.file_preview import FilePreviewPane
        if isinstance(widget, FilePreviewPane):
            if not widget.flush_save():
                QMessageBox.warning(
                    self,
                    "保存失败",
                    "文件仍有未保存改动，已保留此 tab。\n\n"
                    f"{widget.get_path()}",
                )
                return
            key = self._tab_key(widget.get_path())
            self._file_panes.pop(key, None)
            widget.stop_workers()
        self.center_tabs.removeTab(index)
        widget.deleteLater()

    def _close_current_file_tab(self) -> None:
        """Ctrl+W：关闭当前 file tab（日志 tab 被忽略）"""
        idx = self.center_tabs.currentIndex()
        if idx <= 0:
            return
        self._on_tab_close_requested(idx)

    def _reveal_log_tab(self) -> None:
        """多模块项目里日志 tab 默认隐藏；有全局消息写入时显示回来（不抢焦点）。"""
        if not self.center_tabs.isTabVisible(0):
            self.center_tabs.setTabVisible(0, True)

    def _on_tab_bar_context_menu(self, pos) -> None:
        """tabBar 右键菜单：关闭 / 关闭其它 / 关闭右侧 / 关闭左侧。

        日志 tab（index 0）永不可关，所以在筛选目标时统一过滤掉 0；
        模块 / 脚本 tab 的关闭走 _on_tab_close_requested（已弹确认 + 异步停进程）。
        """
        bar = self.center_tabs.tabBar()
        index = bar.tabAt(pos)
        if index < 0:
            return
        total = self.center_tabs.count()

        others = [i for i in range(total) if i != index and i != 0]
        rights = [i for i in range(total) if i > index and i != 0]
        lefts = [i for i in range(total) if i < index and i != 0]

        menu = QMenu(self)
        act_close = menu.addAction("关闭")
        act_close.setEnabled(index != 0)
        menu.addSeparator()
        act_others = menu.addAction("关闭其它")
        act_others.setEnabled(bool(others))
        act_right = menu.addAction("关闭右侧")
        act_right.setEnabled(bool(rights))
        act_left = menu.addAction("关闭左侧")
        act_left.setEnabled(bool(lefts))

        chosen = menu.exec(bar.mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is act_close:
            self._on_tab_close_requested(index)
        elif chosen is act_others:
            self._close_tab_indices(others)
        elif chosen is act_right:
            self._close_tab_indices(rights)
        elif chosen is act_left:
            self._close_tab_indices(lefts)

    def _close_tab_indices(self, indices: list[int]) -> None:
        """批量关闭一组 tab。倒序关，避免中途 index 错位。

        模块 / 脚本 tab 走异步删除（finished 回调里 removeTab），
        但倒序处理保证我们处理某个 index 时它前面（更小 index）的元素位置不变；
        异步删除发生时被删的就是当前 index 自身，对剩余待处理的更小 index 没影响。
        """
        for i in sorted(indices, reverse=True):
            if i <= 0 or i >= self.center_tabs.count():
                continue
            self._on_tab_close_requested(i)

    def _remember_recent(self, path: str) -> None:
        if not path:
            return
        if path in self._recent_files:
            self._recent_files.remove(path)
        self._recent_files.insert(0, path)
        self._recent_files = self._recent_files[:30]

    # ---- 分支操作 ----

    def _populate_branch_menu(self) -> None:
        """菜单先显示加载态，分支列表由 worker 返回后再填充。"""
        menu = self._branch_menu
        menu.clear()
        if not self._is_git_repo:
            act = menu.addAction("（不是 git 仓库）")
            act.setEnabled(False)
            return
        act = menu.addAction("正在加载分支...")
        act.setEnabled(False)
        if self._git_branch_worker and self._git_branch_worker.isRunning():
            return
        worker = GitBranchListWorker(self.project_meta.path, parent=self)
        worker.done.connect(self._on_branch_list_done)
        worker.finished.connect(worker.deleteLater)
        self._git_branch_worker = worker
        worker.start()

    def _on_branch_list_done(self, data: dict) -> None:
        if self.sender() is not self._git_branch_worker:
            return
        self._git_branch_worker = None
        self._branch_data = data
        self._render_branch_menu(data)

    def _render_branch_menu(self, data: dict) -> None:
        menu = self._branch_menu
        menu.clear()
        cur = str(data.get("current", "") or "")
        local = list(data.get("local", []))
        remote = list(data.get("remote", []))
        menu.set_current_branch(cur)

        menu.addAction("🔀 合并远程分支到当前", self._show_merge_dialog)
        menu.addSeparator()

        if cur:
            act = menu.addAction(f"📋 复制当前分支：{cur}")
            act.setData(("copy", cur))
            act.triggered.connect(lambda _=False, br=cur: QApplication.clipboard().setText(br))
            menu.addSeparator()
        if local:
            head = menu.addAction("本地分支（点击切换 / 右键操作）")
            head.setEnabled(False)
            for b in local:
                label = ("● " if b == cur else "    ") + b
                act = menu.addAction(label)
                act.setData(("local", b))
                if b == cur:
                    act.setEnabled(False)
                else:
                    act.triggered.connect(lambda _=False, br=b: self._do_checkout(br))
        menu.addSeparator()
        menu.addAction("🔄 刷新远程分支", self._manual_fetch_remote)
        if remote:
            head = menu.addAction("远程分支（右键切换 / 复制）")
            head.setEnabled(False)
            for b in remote:
                act = menu.addAction("    " + b)
                act.setData(("remote", b))
                act.triggered.connect(lambda _=False, br=b: QApplication.clipboard().setText(br))

    def _manual_fetch_remote(self) -> None:
        """用户手动刷新远程分支：fetch 完成后重新弹出菜单。"""
        if not self._is_git_repo:
            return
        if self._git_fetch_worker and self._git_fetch_worker.isRunning():
            return
        notify.notify_info("刷新远程分支", "正在从远程仓库获取最新分支…")
        self._git_fetch_worker = GitFetchWorker(self.project_meta.path, parent=self)
        self._git_fetch_worker.done.connect(self._on_manual_fetch_done)
        self._git_fetch_worker.finished.connect(self._git_fetch_worker.deleteLater)
        self._git_fetch_worker.start()

    def _on_manual_fetch_done(self, ok: bool) -> None:
        if self.sender() is self._git_fetch_worker:
            self._git_fetch_worker = None
        if ok:
            git_info.invalidate(self.project_meta.path)
            self._refresh_status_row()
            notify.notify_success("刷新远程分支", "远程分支已更新")
            self._branch_menu.popup(QCursor.pos())
        else:
            notify.notify_error("刷新远程分支", "获取远程分支失败，请检查网络")

    def _start_remote_fetch(self) -> None:
        """后台静默 git fetch，刷新 ahead/behind 状态，发现远程领先时高亮分支按钮。"""
        if self._git_fetch_worker and self._git_fetch_worker.isRunning():
            return
        path = self.project_meta.path
        if not self._is_git_repo:
            return
        self._git_fetch_worker = GitFetchWorker(
            path, parent=self, require_upstream=True,
        )
        self._git_fetch_worker.done.connect(self._on_remote_fetch_done)
        self._git_fetch_worker.finished.connect(self._git_fetch_worker.deleteLater)
        self._git_fetch_worker.start()

    def _on_remote_fetch_done(self, ok: bool) -> None:
        if self.sender() is self._git_fetch_worker:
            self._git_fetch_worker = None
        if not ok:
            return
        git_info.invalidate(self.project_meta.path)
        self._refresh_status_row()

    def _do_checkout(self, branch: str) -> None:
        self._check_dirty_before_checkout(False, branch)

    def _do_checkout_remote_branch(self, remote_branch: str) -> None:
        self._check_dirty_before_checkout(True, remote_branch)

    def _check_dirty_before_checkout(self, remote: bool, branch: str) -> None:
        if ((self._git_dirty_worker and self._git_dirty_worker.isRunning())
                or (self._git_checkout_worker and self._git_checkout_worker.isRunning())
                or (self._git_checkout_remote_worker
                    and self._git_checkout_remote_worker.isRunning())):
            return
        self._pending_checkout = (remote, branch)
        worker = GitDirtyCheckWorker(self.project_meta.path, parent=self)
        worker.done.connect(self._on_checkout_dirty_checked)
        worker.finished.connect(worker.deleteLater)
        self._git_dirty_worker = worker
        worker.start()

    def _on_checkout_dirty_checked(self, dirty: bool) -> None:
        if self.sender() is not self._git_dirty_worker:
            return
        self._git_dirty_worker = None
        pending = self._pending_checkout
        self._pending_checkout = None
        if pending is None:
            return
        remote, branch = pending
        if dirty:
            ret = QMessageBox.question(
                self, "切换分支",
                f"本地有未提交的改动，仍要切换到 {branch} 吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
        self._start_checkout(remote, branch)

    def _start_checkout(self, remote: bool, branch: str) -> None:
        path = self.project_meta.path
        if remote:
            if (self._git_checkout_remote_worker
                    and self._git_checkout_remote_worker.isRunning()):
                return
            worker = GitCheckoutRemoteWorker(path, branch, parent=self)
            worker.done.connect(self._on_checkout_remote_done)
            worker.finished.connect(worker.deleteLater)
            self._git_checkout_remote_worker = worker
        else:
            if self._git_checkout_worker and self._git_checkout_worker.isRunning():
                return
            worker = GitCheckoutWorker(path, branch, parent=self)
            worker.done.connect(self._on_checkout_done)
            worker.finished.connect(worker.deleteLater)
            self._git_checkout_worker = worker
        self._checkout_target = branch
        worker.start()

    def _on_checkout_done(self, ok: bool, msg: str) -> None:
        if self.sender() is self._git_checkout_worker:
            self._git_checkout_worker = None
        git_info.invalidate(self.project_meta.path)
        self._refresh_status_row()
        if ok:
            target = self._checkout_target
            self._checkout_target = ""
            notify.notify_success("分支已切换", f"当前分支：{target}")
        else:
            self._checkout_target = ""
            QMessageBox.warning(self, "切换失败", msg)

    def _on_checkout_remote_done(self, ok: bool, msg: str) -> None:
        if self.sender() is self._git_checkout_remote_worker:
            self._git_checkout_remote_worker = None
        git_info.invalidate(self.project_meta.path)
        self._refresh_status_row()
        if ok:
            target = self._checkout_target
            if "/" in target:
                target = target.split("/", 1)[1]
            self._checkout_target = ""
            notify.notify_success("分支已切换", f"当前分支：{target}")
        else:
            self._checkout_target = ""
            QMessageBox.warning(self, "切换失败", msg)

    def _do_delete_local_branch(self, branch: str) -> None:
        ret = QMessageBox.question(
            self, "删除本地分支",
            f"确定删除本地分支 {branch} 吗？\n\n未合并的分支不会被强制删除。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        if self._git_delete_branch_worker and self._git_delete_branch_worker.isRunning():
            return
        path = self.project_meta.path
        self._git_delete_branch_worker = GitDeleteLocalBranchWorker(path, branch, parent=self)
        self._git_delete_branch_worker.done.connect(self._on_delete_local_branch_done)
        self._git_delete_branch_worker.finished.connect(self._git_delete_branch_worker.deleteLater)
        self._git_delete_branch_worker.start()

    def _on_delete_local_branch_done(self, ok: bool, msg: str) -> None:
        if self.sender() is self._git_delete_branch_worker:
            self._git_delete_branch_worker = None
        git_info.invalidate(self.project_meta.path)
        self._refresh_status_row()
        if ok:
            notify.notify_success("本地分支已删除", msg)
        else:
            QMessageBox.warning(self, "删除失败", msg)

    def _show_merge_dialog(self) -> None:
        data = self._branch_data or {}
        remotes = list(data.get("remote", []))
        if not remotes:
            QMessageBox.information(self, "合并", "没有找到远程分支。")
            return
        cur = str(data.get("current", "") or "")
        items = [PickerItem(title=b, subtitle="", data=b) for b in remotes]
        from src.ui.quick_open import PickerDialog
        dlg = PickerDialog(f"合并远程分支到当前（{cur}）", self)
        dlg.set_static_items(items)
        dlg.picked.connect(self._do_merge_push)
        # 持有引用，避免局部变量回收；菜单关闭瞬间会抢焦点导致浮窗"失焦自动关闭"，
        # 用 singleShot 等菜单彻底关掉后再弹，浮窗才能拿到焦点不被立即关闭。
        self._merge_dialog = dlg
        QTimer.singleShot(0, dlg.show)

    def _do_merge_push(self, remote_branch: str) -> None:
        if self._git_merge_worker and self._git_merge_worker.isRunning():
            return
        path = self.project_meta.path
        notify.notify_info("合并远程分支", f"正在合并 {remote_branch} 并推送…")
        self._git_merge_worker = GitMergePushWorker(path, remote_branch, parent=self)
        self._git_merge_worker.done.connect(self._on_merge_push_done)
        self._git_merge_worker.finished.connect(self._git_merge_worker.deleteLater)
        self._git_merge_worker.start()

    def _on_merge_push_done(self, ok: bool, msg: str) -> None:
        if self.sender() is self._git_merge_worker:
            self._git_merge_worker = None
        git_info.invalidate(self.project_meta.path)
        self._refresh_status_row()
        if ok:
            QMessageBox.information(self, "合并推送完成", msg)
        else:
            QMessageBox.warning(self, "合并失败", msg)

    # ---- 其他 ----

    def _on_file_activated(self, path: str) -> None:
        # 办公文档（docx/xlsx/pdf…）：交给系统默认关联程序打开（等价于资源管理器双击）
        if is_office_doc(path) and open_with_system_default(path):
            return
        if self.config.file_open_mode == "preview":
            self._show_preview(path, 0, 0)
            return
        ok = open_in_editor(path, editor_cmd=self.config.editor_cmd,
                            project_root=self.project_meta.path)
        if not ok:
            self._show_preview(path, 0, 0)

    def request_close(
        self,
        confirm_running: bool = True,
        stop_running: bool = True,
        quiet: bool = False,
    ) -> bool:
        running_items = self.running_service_items(refresh_external=True)
        any_running = bool(running_items)
        if any_running and confirm_running:
            ret = QMessageBox.question(
                self, "项目正在运行",
                f"{self.project_meta.name} 正在运行，关闭会停止所有进程。确认关闭？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                return False
        # 关闭前强制把所有 pending 自动保存刷盘。保存失败时保留项目，
        # 避免 tab 被关掉后用户误以为改动已经落盘。
        failed_panes = []
        for pane in list(self._file_panes.values()):
            try:
                if not pane.flush_save():
                    failed_panes.append(pane)
            except Exception:
                failed_panes.append(pane)
        if failed_panes:
            first = failed_panes[0]
            idx = self.center_tabs.indexOf(first)
            if idx >= 0:
                self.center_tabs.setCurrentIndex(idx)
            if not quiet:
                QMessageBox.warning(
                    self,
                    "保存失败",
                    f"有 {len(failed_panes)} 个文件未能保存，已取消关闭项目。",
                )
            return False
        if any_running and stop_running:
            self.stop_all_services(include_external=True, silent=True)
            if not self.wait_services_stopped():
                if not quiet:
                    QMessageBox.warning(
                        self,
                        "停止超时",
                        f"{self.project_meta.name} 仍有服务未完全停止，已取消关闭。",
                    )
                return False
        self._poll_timer.stop()
        self._fetch_timer.stop()
        self._poll_start_timer.stop()
        self._fetch_start_timer.stop()
        for pane in self._file_panes.values():
            pane.stop_workers()
        self.indexer.stop()
        self.file_tree.stop_workers()
        # Git 子进程可能受磁盘或网络影响。关闭 Tab 时把 worker 转交给应用回收，
        # 不在 UI 线程逐个 wait；接收者销毁后 Qt 会自动断开结果信号。
        for w in (self._git_branch_worker, self._git_dirty_worker,
                  self._git_status_worker, self._git_fetch_worker,
                  self._git_checkout_worker, self._git_checkout_remote_worker,
                  self._git_delete_branch_worker, self._git_merge_worker):
            if w is not None and w.isRunning():
                w.requestInterruption()
                w.setParent(QApplication.instance())
        return True

    # ---- 快捷键与导航 ----

    def _register_shortcuts(self) -> None:
        # 快捷键已上移到 MainWindow 统一注册（窗口级，焦点在哪都生效，
        # 多 tab 不冲突），由 MainWindow 路由到当前可见 tab 的下列 public 方法：
        #   Ctrl+Shift+N → open_file_picker      Ctrl+Shift+F → open_content_search
        #   Ctrl+E       → open_recent_files
        #   Ctrl+\\       → open_endpoint_picker   Ctrl+Shift+R → restart_project
        #   Ctrl+W       → close_current_file_tab
        # 这里留空，仅保留方法以兼容 __init__ 调用。
        pass

    # 供 MainWindow 窗口级快捷键路由调用的 public 别名
    def restart_project(self) -> None:
        self._restart()

    def close_current_file_tab(self) -> None:
        self._close_current_file_tab()


    def open_file_picker(self) -> None:
        """Ctrl+Shift+N：弹独立浮窗按文件名搜（IDEA 风格），回车在中心区打开"""
        from src.ui.quick_open import show_file_picker
        started = time.perf_counter()
        dlg = show_file_picker(
            self.indexer,
            on_pick=lambda p: self._show_preview(p, 0, 0),
            parent=self,
        )
        dlg.show()
        perf_log.info(
            "perf op=file-search-open duration_ms=%.1f files=%d status=done",
            (time.perf_counter() - started) * 1000, self.indexer.count(),
        )

    def open_content_search(self) -> None:
        """Ctrl+Shift+F：在当前项目内搜索文件内容。"""
        started = time.perf_counter()
        dlg = ContentSearchDialog(
            self.project_meta.path, parent=self, indexer=self.indexer,
        )
        dlg.open_requested.connect(lambda p, l, c: self._show_preview(p, l, c))
        dlg.show()
        perf_log.info(
            "perf op=content-search-open duration_ms=%.1f files=%d status=done",
            (time.perf_counter() - started) * 1000, self.indexer.count(),
        )

    def open_recent_files(self) -> None:
        if not self._recent_files:
            QMessageBox.information(self, "最近打开", "还没有打开过文件")
            return
        dlg = show_recent_files(
            self._recent_files, self.project_meta.path,
            on_pick=lambda p: self._show_preview(p, 0, 0),
            parent=self,
        )
        dlg.show()

    def open_endpoint_picker(self) -> None:
        """Ctrl+\\：按接口地址（如 /tenant/timer/common/getCategoryList）定位 Controller 方法。

        懒加载：首次调用才后台扫描全项目 Controller，扫完自动刷新候选列表。
        项目里没有 .java 时直接提示，不弹空浮窗。
        """
        from src.ui.quick_open import PickerDialog, PickerItem
        from src.core.controller_index import ControllerIndexer

        if self._controller_indexer is None:
            # 先粗检项目里有没有 java（用文件索引，零额外 IO）
            has_java = any(f.name.endswith(".java") for f in self.indexer.files())
            if not has_java and self.indexer.count() > 0:
                QMessageBox.information(self, "接口跳转", "当前项目里没有找到 Java 文件。")
                return
            self._controller_indexer = ControllerIndexer(self.project_meta.path, self)
            self._controller_indexer.build_async()

        ci = self._controller_indexer
        dlg = PickerDialog("按接口地址定位   (Ctrl+\\)", self)

        def fetch(query: str) -> list[PickerItem]:
            if not ci.is_built():
                return []
            return [
                PickerItem(title=ep.display, subtitle=ep.subtitle,
                           data=(ep.abs_path, ep.line_no))
                for ep in ci.search(query, limit=150)
            ]

        dlg.set_fetcher(fetch)
        dlg.picked.connect(lambda d: self._show_preview(d[0], d[1], 0))

        if ci.is_built():
            dlg.input.setPlaceholderText(f"已索引 {ci.count()} 个接口...")
        else:
            dlg.input.setPlaceholderText("正在扫描 Controller，请稍候...")
            # 扫完后刷新当前浮窗（若还开着）
            def on_ready(n: int):
                if dlg.isVisible():
                    dlg.input.setPlaceholderText(f"已索引 {n} 个接口...")
                    dlg._refresh(dlg.input.text())
            ci.ready.connect(on_ready)

        self._endpoint_dialog = dlg
        dlg.show()

    def open_git_viewer(self) -> None:
        if not self._is_git_repo:
            QMessageBox.information(self, "Git", "当前项目不是 git 仓库")
            return
        if self._git_viewer is not None:
            self._git_viewer.show()
            self._git_viewer.raise_()
            self._git_viewer.activateWindow()
            return
        dlg = GitViewer(self.project_meta.path, parent=self)
        self._git_viewer = dlg
        dlg.destroyed.connect(lambda: setattr(self, "_git_viewer", None))
        dlg.openFileRequested.connect(lambda p: self._show_preview(p, 0, 0))
        dlg.show()

    def open_project_info(self) -> None:
        """项目信息弹窗（路径/类型/包管理/主类，只读）"""
        from PySide6.QtWidgets import QDialog, QVBoxLayout
        dlg = QDialog(self)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.setWindowTitle(f"ℹ️ 项目信息 — {self.project_meta.name}")
        dlg.resize(440, 280)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(SettingsPanel(self.project_meta, self.entry, parent=dlg))
        dlg.show()
