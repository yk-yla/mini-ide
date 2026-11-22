"""左侧运行单元面板。

每行：模块名 端口 启停按钮
（运行/未启动等状态只用右侧按钮的图标表达，不再显示文字，也不再有左侧独立状态图标）

交互：
- 左键行主体 → 切到该模块的日志 tab
- 右侧小按钮 → 启停（▶ 启动 / ⏹ 停止，启动中/停止中时禁用并显示 ◐）
- 顶部"全部启动 / 全部停止"按钮

对外信号：
- focusRequested(module_name)
- startRequested / stopRequested(module_name)
- startAllRequested / stopAllRequested
- clearLogsRequested
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QToolButton,
    QVBoxLayout, QWidget,
)

from src.ui.icons import icon
from src.ui.theme import (
    ACCENT, BG_L4, BORDER_SUBTLE, COLOR_SUCCESS, COLOR_WARN,
    FG_DIM, FG_ON_ACCENT, FG_PRIMARY, FG_SECONDARY, FONT_PT_UI, FONT_PT_UI_SM, H_BTN_SM,
)


STATE_IDLE = "idle"
STATE_STARTING = "starting"
STATE_RUNNING = "running"
STATE_RUNNING_EXTERNAL = "running_external"   # 非 mini-ide 启动、靠端口/命令行感知到的运行中
STATE_STOPPING = "stopping"


def _row_btn_qss(border_color: str, fg_color: str) -> str:
    return (
        f"QPushButton {{ background:transparent; border:1px solid {border_color};"
        f" border-radius:3px; color:{fg_color}; font-size:{FONT_PT_UI_SM}pt;"
        f" padding:0; }}"
        f"QPushButton:hover:enabled {{ border-color:{ACCENT}; color:{FG_PRIMARY}; }}"
        f"QPushButton:disabled {{ background:transparent; }}"
    )


class _ServiceRow(QFrame):
    focusRequested = Signal(str)
    startRequested = Signal(str)
    stopRequested = Signal(str)

    def __init__(
        self, module_name: str, default_port: int | None = None,
        display_name: str | None = None, parent=None,
    ):
        super().__init__(parent)
        self.module_name = module_name
        self.display_name = display_name or module_name
        self.default_port = default_port
        self._state = STATE_IDLE

        self.setObjectName("service_row")
        self._display = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 5, 6, 5)
        lay.setSpacing(8)

        self.lbl_name = QLabel(self.display_name)
        if self.display_name != module_name:
            self.lbl_name.setToolTip(module_name)
        self.lbl_name.setStyleSheet(f"color:{FG_PRIMARY}; font-size:{FONT_PT_UI}pt;")
        lay.addWidget(self.lbl_name, 1)

        self.lbl_info = QLabel("")
        self.lbl_info.setStyleSheet(f"color:{FG_SECONDARY}; font-size:{FONT_PT_UI_SM}pt;")
        lay.addWidget(self.lbl_info)

        self.btn = QPushButton()
        self.btn.setFixedSize(H_BTN_SM, H_BTN_SM)
        self.btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn.clicked.connect(self._on_btn)
        lay.addWidget(self.btn)

        self.set_state(STATE_IDLE)

    def mousePressEvent(self, event):
        # 点到按钮时 Qt 已派给子 widget，这里只在点行空白处触发
        if event.button() == Qt.MouseButton.LeftButton:
            self.focusRequested.emit(self.module_name)
        super().mousePressEvent(event)

    def _on_btn(self):
        if self._state in (STATE_RUNNING, STATE_RUNNING_EXTERNAL):
            self.stopRequested.emit(self.module_name)
        elif self._state == STATE_IDLE:
            self.startRequested.emit(self.module_name)

    def set_state(
        self, state: str, port: int | None = None, elapsed_seconds: float = 0.0,
        pid: int | None = None, reason: str = "", ports: list[int] | None = None,
    ):
        self._state = state
        display = (state, port, pid, reason, tuple(ports or ()))
        if display == self._display:
            return
        self._display = display
        # port 优先用本次传入的（启动后从日志抓的真实端口），否则用 application.yml 的默认值
        effective_port = port if port is not None else self.default_port
        if ports:
            port_str = " ".join(f":{p}" for p in ports)
        else:
            port_str = f":{effective_port}" if effective_port else ""
        detail = reason or ""
        if state == STATE_IDLE:
            self.lbl_info.setText(port_str)
            self.lbl_info.setStyleSheet(f"color:{FG_SECONDARY}; font-size:{FONT_PT_UI_SM}pt;")
            self.btn.setIcon(icon("play", FG_SECONDARY))
            self.btn.setToolTip(f"启动 {self.display_name}")
            self.btn.setEnabled(True)
            self.btn.setStyleSheet(_row_btn_qss(BORDER_SUBTLE, FG_DIM))
        elif state == STATE_STARTING:
            self.lbl_info.setText(f"启动中 {port_str}".strip())
            self.lbl_info.setStyleSheet(f"color:{COLOR_WARN}; font-size:{FONT_PT_UI_SM}pt;")
            self.btn.setIcon(icon("activity", COLOR_WARN))
            self.btn.setToolTip(f"{self.display_name} 正在启动")
            self.btn.setEnabled(False)
            self.btn.setStyleSheet(_row_btn_qss(COLOR_WARN, COLOR_WARN))
        elif state == STATE_RUNNING:
            self.lbl_info.setText(f"运行 {port_str}".strip())
            self.lbl_info.setStyleSheet(f"color:{COLOR_SUCCESS}; font-size:{FONT_PT_UI_SM}pt;")
            self.btn.setIcon(icon("stop", COLOR_SUCCESS))
            tip = f"{self.display_name} 由当前 mini-ide 启动，日志上下文完整。"
            if effective_port:
                tip += f"\n端口：{effective_port}"
            self.btn.setToolTip(tip + "\n点击停止")
            self.btn.setEnabled(True)
            self.btn.setStyleSheet(_row_btn_qss(COLOR_SUCCESS, COLOR_SUCCESS))
        elif state == STATE_RUNNING_EXTERNAL:
            self.lbl_info.setText(f"外部 {port_str}".strip())
            self.lbl_info.setStyleSheet(f"color:{COLOR_WARN}; font-size:{FONT_PT_UI_SM}pt;")
            self.btn.setIcon(icon("stop", COLOR_WARN))
            tip = f"{self.display_name} 正在 mini-ide 之外运行，当前 IDE 没有启动日志上下文。"
            if ports:
                tip += f"\n端口：{', '.join(str(p) for p in ports)}"
            elif effective_port:
                tip += f"\n端口：{effective_port}"
            if pid:
                tip += f"\nPID：{pid}"
            if detail:
                tip += f"\n{detail}"
            self.btn.setToolTip(tip + "\n点击停止外部进程")
            self.btn.setEnabled(True)
            self.btn.setStyleSheet(_row_btn_qss(COLOR_WARN, COLOR_WARN))
        elif state == STATE_STOPPING:
            self.lbl_info.setText(f"停止中 {port_str}".strip())
            self.lbl_info.setStyleSheet(f"color:{COLOR_WARN}; font-size:{FONT_PT_UI_SM}pt;")
            self.btn.setIcon(icon("activity", COLOR_WARN))
            self.btn.setToolTip(f"{self.display_name} 正在停止")
            self.btn.setEnabled(False)
            self.btn.setStyleSheet(_row_btn_qss(COLOR_WARN, COLOR_WARN))
        self.btn.setAccessibleName(self.btn.toolTip())

    def _refresh_theme(self) -> None:
        self.lbl_name.setStyleSheet(f"color:{FG_PRIMARY}; font-size:{FONT_PT_UI}pt;")
        state, port, pid, reason, ports = self._display
        self._display = None
        self.set_state(state, port, pid=pid, reason=reason, ports=ports)

    def current_state(self) -> str:
        return self._state


class ServicePanel(QWidget):
    """多模块项目的左上侧服务面板"""

    focusRequested = Signal(str)
    startRequested = Signal(str)
    stopRequested = Signal(str)
    startAllRequested = Signal()
    stopAllRequested = Signal()
    clearLogsRequested = Signal()

    def __init__(
        self, modules: list[tuple], parent=None, *, title: str = "服务",
    ):
        """modules: [(id, port)] 或 [(id, display_name, port)]。"""
        super().__init__(parent)
        self.setObjectName("service_panel")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QFrame()
        header.setObjectName("service_header")
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(10, 6, 6, 6)
        h_lay.setSpacing(6)

        self.title_icon = QLabel()
        h_lay.addWidget(self.title_icon)
        title_label = QLabel(f"{title} ({len(modules)})")
        h_lay.addWidget(title_label)
        h_lay.addStretch(1)

        self.btn_clear_logs = QToolButton()
        self.btn_clear_logs.setAccessibleName("清空日志")
        self.btn_clear_logs.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_clear_logs.setToolTip("清空当前项目所有服务控制台日志")
        self.btn_clear_logs.clicked.connect(self.clearLogsRequested.emit)
        h_lay.addWidget(self.btn_clear_logs)

        self.btn_start_all = QToolButton()
        self.btn_start_all.setProperty("role", "primary")
        self.btn_start_all.setAccessibleName("全部启动")
        self.btn_start_all.setToolTip("全部启动")
        self.btn_start_all.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_start_all.clicked.connect(self.startAllRequested.emit)
        h_lay.addWidget(self.btn_start_all)

        self.btn_stop_all = QToolButton()
        self.btn_stop_all.setProperty("role", "danger")
        self.btn_stop_all.setAccessibleName("全部停止")
        self.btn_stop_all.setToolTip("全部停止")
        self.btn_stop_all.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_stop_all.clicked.connect(self.stopAllRequested.emit)
        self.btn_stop_all.setEnabled(False)
        h_lay.addWidget(self.btn_stop_all)

        root.addWidget(header)

        rows_wrap = QFrame()
        rlay = QVBoxLayout(rows_wrap)
        rlay.setContentsMargins(0, 0, 0, 0)
        rlay.setSpacing(0)

        self._rows: dict[str, _ServiceRow] = {}
        for item in modules:
            if len(item) == 3:
                name, display_name, port = item
            else:
                name, port = item
                display_name = name
            row = _ServiceRow(
                name, default_port=port, display_name=display_name, parent=rows_wrap,
            )
            row.focusRequested.connect(self.focusRequested.emit)
            row.startRequested.connect(self.startRequested.emit)
            row.stopRequested.connect(self.stopRequested.emit)
            self._rows[name] = row
            rlay.addWidget(row)
        rlay.addStretch(1)

        self.rows_scroll = QScrollArea()
        self.rows_scroll.setObjectName("service_rows_scroll")
        self.rows_scroll.setWidgetResizable(True)
        self.rows_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.rows_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.rows_scroll.setWidget(rows_wrap)
        root.addWidget(self.rows_scroll, 1)
        self.refresh_theme()

    def refresh_theme(self) -> None:
        self.title_icon.setPixmap(icon("layers").pixmap(16, 16))
        self.btn_clear_logs.setIcon(icon("clear"))
        self.btn_start_all.setIcon(icon("play", FG_ON_ACCENT))
        self.btn_stop_all.setIcon(icon("stop", COLOR_WARN))
        for row in self._rows.values():
            row._refresh_theme()

    def update_state(
        self, module: str, state: str,
        port: int | None = None, elapsed_seconds: float = 0.0,
        pid: int | None = None, reason: str = "", ports: list[int] | None = None,
    ) -> None:
        row = self._rows.get(module)
        if row:
            row.set_state(state, port, elapsed_seconds, pid, reason, ports)
        # 任意模块在 RUNNING/STARTING/外部运行 就允许「全部停止」
        any_active = any(
            r.current_state() in (STATE_RUNNING, STATE_RUNNING_EXTERNAL, STATE_STARTING)
            for r in self._rows.values()
        )
        self.btn_stop_all.setEnabled(any_active)

    def running_count(self) -> int:
        return sum(
            1 for r in self._rows.values()
            if r.current_state() in (STATE_RUNNING, STATE_RUNNING_EXTERNAL)
        )

    def total(self) -> int:
        return len(self._rows)
