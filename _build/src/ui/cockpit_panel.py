"""项目运行概览和本机 AI 操作时间线。"""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from src.core.service_state import (
    KIND_MODULE, KIND_SCRIPT, STATE_RUNNING_EXTERNAL, STATE_RUNNING_MANAGED,
    STATE_STARTING, STATE_STOPPED, STATE_STOPPING,
)
from src.core.usage_stats import usage_stats
from src.ui.icons import icon
from src.ui.theme import (
    COLOR_ERROR, COLOR_SUCCESS, COLOR_WARN, DOT_IDLE, FG_SECONDARY,
    GAP_MD, GAP_SM, GAP_XS, H_BTN_SM,
)


_STATE_TEXT = {
    STATE_RUNNING_MANAGED: ("运行中", COLOR_SUCCESS),
    STATE_RUNNING_EXTERNAL: ("外部运行", COLOR_WARN),
    STATE_STARTING: ("启动中", COLOR_WARN),
    STATE_STOPPING: ("停止中", COLOR_WARN),
    STATE_STOPPED: ("已停止", DOT_IDLE),
}


def _compact_button(text: str, *, icon_only: bool = False):
    if icon_only:
        button = QToolButton()
        button.setFixedSize(H_BTN_SM + GAP_XS, H_BTN_SM + GAP_XS)
        button.setAccessibleName(text)
    else:
        button = QPushButton(text)
    button.setProperty("compact", True)
    return button


class _RuntimeRow(QFrame):
    focus_requested = Signal(str)
    service_requested = Signal(str, object)

    def __init__(self, unit_id, parent=None):
        super().__init__(parent)
        self.unit_id = unit_id
        self._values = None
        self._url = ""
        self._action = ""
        self.setObjectName("cockpit_runtime_row")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("查看服务日志")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(GAP_SM, GAP_XS, GAP_SM, GAP_XS)
        layout.setSpacing(GAP_SM)
        self.state_label = QLabel()
        self.state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.state_label.setFixedWidth(H_BTN_SM)
        layout.addWidget(self.state_label)
        self.name_label = _compact_button("")
        self.name_label.setProperty("runtimeName", True)
        self.name_label.setToolTip("查看服务日志")
        self.name_label.clicked.connect(lambda: self.focus_requested.emit(unit_id or ""))
        layout.addWidget(self.name_label, 1)
        self.port_label = QLabel()
        self.port_label.setProperty("role", "subtitle")
        layout.addWidget(self.port_label)
        self.copy_button = _compact_button("复制地址", icon_only=True)
        self.copy_button.clicked.connect(lambda: QApplication.clipboard().setText(self._url))
        layout.addWidget(self.copy_button)
        self.open_button = _compact_button("浏览器打开", icon_only=True)
        self.open_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self._url)))
        layout.addWidget(self.open_button)
        self.service_button = _compact_button("启停服务", icon_only=True)
        self.service_button.clicked.connect(
            lambda: self.service_requested.emit(self._action, self.unit_id)
        )
        layout.addWidget(self.service_button)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.focus_requested.emit(self.unit_id or "")
        super().mousePressEvent(event)

    def update_runtime(self, name: str, state: str, ports: tuple[int, ...], can_run: bool):
        values = (name, state, ports, can_run)
        if values == self._values:
            return
        self._values = values
        self.name_label.setText(name)
        self.port_label.setText(" ".join(f":{port}" for port in ports) or "—")
        self._url = f"http://localhost:{ports[0]}" if ports else ""
        self.copy_button.setEnabled(bool(ports))
        self.open_button.setEnabled(bool(ports))
        self.copy_button.setToolTip(f"复制 {self._url}" if ports else "没有配置端口")
        self.open_button.setToolTip(f"打开 {self._url}" if ports else "没有配置端口")
        stopping = state in (STATE_RUNNING_EXTERNAL, STATE_RUNNING_MANAGED)
        self._action = "stop" if stopping else "start"
        self.service_button.setEnabled(can_run and (stopping or state == STATE_STOPPED))
        tooltip = "停止服务" if stopping else "启动服务"
        self.service_button.setToolTip(tooltip)
        self.service_button.setAccessibleName(tooltip)
        self._refresh_theme()

    def _refresh_theme(self):
        if self._values is None:
            return
        _, state, _, _ = self._values
        text, color = _STATE_TEXT.get(state, ("状态未知", COLOR_WARN))
        self.state_label.setText("●")
        self.state_label.setStyleSheet(f"color:{color};")
        self.state_label.setToolTip(text)
        self.state_label.setAccessibleName(text)
        self.copy_button.setIcon(icon("copy"))
        self.open_button.setIcon(icon("external_link"))
        glyph = "activity" if state in (STATE_STARTING, STATE_STOPPING) else (
            "stop" if self._action == "stop" else "play"
        )
        self.service_button.setIcon(icon(glyph, color if state != STATE_STOPPED else FG_SECONDARY))


class _AiEventRow(QFrame):
    activated = Signal(str)

    def __init__(self, event, clickable):
        super().__init__()
        self.target = event["target"]
        self.setObjectName("cockpit_event_row")
        self.setProperty("clickable", clickable)
        if clickable:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setToolTip(f"点击查看 {self.target} 日志")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(GAP_SM, GAP_XS, GAP_SM, GAP_XS)
        layout.setSpacing(GAP_MD)
        timestamp = datetime.fromisoformat(event["ts"]).astimezone().strftime("%H:%M:%S")
        milliseconds = event["duration_ms"]
        duration = f"{milliseconds}ms" if milliseconds < 1000 else f"{milliseconds / 1000:.1f}s"
        status = "成功" if event["ok"] else f"失败 · {event['error_code']}"
        self.labels = []
        for column, text in enumerate((timestamp, event["command"], self.target or "—", duration, status)):
            label = QLabel(text)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            if column == 4:
                label.setProperty("role", "cockpit-event-ok" if event["ok"] else "cockpit-event-error")
            elif column != 1:
                label.setProperty("role", "subtitle")
            if column == 2:
                label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            layout.addWidget(label, int(column == 2))
            self.labels.append(label)

    def mousePressEvent(self, event):
        if self.property("clickable") and event.button() == Qt.MouseButton.LeftButton:
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if (self.property("clickable") and event.button() == Qt.MouseButton.LeftButton
                and self.rect().contains(event.position().toPoint())):
            self.activated.emit(self.target)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class ProjectCockpitPanel(QScrollArea):
    file_search_requested = Signal()
    content_search_requested = Signal()
    endpoint_search_requested = Signal()
    log_requested = Signal(str)
    event_log_requested = Signal(str)
    service_requested = Signal(str, object)
    _ai_event = Signal(dict)

    def __init__(self, meta, *, has_endpoints=False, stats=None, resolve_event_log=None, parent=None):
        super().__init__(parent)
        self._stats = stats if stats is not None else usage_stats
        self._unsubscribe = None
        self._displayed_events = None
        self._resolve_event_log = resolve_event_log
        self._icons = []
        self._rows = {}
        self._units = {unit.id: unit for unit in meta.runtime_units}
        self._primary = next((profile for profile in meta.profiles if profile.primary), None)
        self._meta = meta

        self.setObjectName("project_cockpit")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(True)
        content = QWidget()
        self.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(GAP_MD, GAP_MD, GAP_MD, GAP_MD)
        layout.setSpacing(GAP_MD)

        overview = self._section(layout, "cockpit_overview")
        name_row = QHBoxLayout()
        name_row.setSpacing(GAP_SM)
        name = QLabel(meta.name)
        name.setProperty("role", "cockpit-title")
        name.setTextFormat(Qt.TextFormat.PlainText)
        name_row.addWidget(name)
        chip = QLabel(meta.display_type)
        chip.setProperty("role", "cockpit-chip")
        name_row.addWidget(chip)
        name_row.addStretch(1)
        overview.addLayout(name_row)

        path_row = QHBoxLayout()
        path_row.setSpacing(GAP_SM)
        path = QLabel(meta.path)
        path.setTextFormat(Qt.TextFormat.PlainText)
        path.setProperty("role", "subtitle")
        path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        path.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        path.setToolTip(meta.path)
        path_row.addWidget(path, 1)
        copy = self._button("复制路径", "copy", path_row, icon_only=True)
        copy.setToolTip("复制项目路径")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(meta.path))
        overview.addLayout(path_row)

        actions = QHBoxLayout()
        actions.setSpacing(GAP_SM)
        buttons = [
            ("搜文件  Ctrl+Shift+N", "file", self.file_search_requested),
            ("搜内容  Ctrl+Shift+F", "search", self.content_search_requested),
        ]
        if has_endpoints:
            buttons.append(("搜接口  Ctrl+\\", "code", self.endpoint_search_requested))
        for title, glyph, signal in buttons:
            self._button(title, glyph, actions).clicked.connect(signal.emit)
        actions.addStretch(1)
        overview.addLayout(actions)

        radar = self._section(layout, "cockpit_section")
        self._heading(radar, "服务与端口", "layers")
        self._runtime_layout = QVBoxLayout()
        self._runtime_layout.setSpacing(GAP_XS)
        radar.addLayout(self._runtime_layout)
        self._no_runtimes = QLabel("当前目录没有运行配置")
        self._no_runtimes.setProperty("role", "subtitle")
        self._runtime_layout.addWidget(self._no_runtimes)

        activity = self._section(layout, "cockpit_section")
        self._heading(activity, "最近 AI 活动", "activity")
        self.event_rows = QVBoxLayout()
        self.event_rows.setSpacing(GAP_XS)
        activity.addLayout(self.event_rows)
        hint = QLabel("Ctrl+E  最近文件    ·    Ctrl+Shift+R  重启项目")
        hint.setProperty("role", "subtitle")
        layout.addWidget(hint)
        layout.addStretch(1)

        self._ai_event.connect(self._refresh_events, Qt.ConnectionType.QueuedConnection)
        self._subscribe()
        self.refresh_theme()

    def _section(self, layout, name):
        panel = QFrame()
        panel.setObjectName(name)
        layout.addWidget(panel)
        section = QVBoxLayout(panel)
        section.setContentsMargins(GAP_MD, GAP_MD, GAP_MD, GAP_MD)
        section.setSpacing(GAP_SM)
        return section

    def _heading(self, layout, title, glyph):
        row = QHBoxLayout()
        row.setSpacing(GAP_SM)
        image = QLabel()
        self._icons.append((image, glyph))
        row.addWidget(image)
        label = QLabel(title)
        label.setProperty("role", "title")
        row.addWidget(label)
        row.addStretch(1)
        layout.addLayout(row)

    def _button(self, text, glyph, layout, *, icon_only=False):
        button = _compact_button(text, icon_only=icon_only)
        self._icons.append((button, glyph))
        layout.addWidget(button)
        return button

    def update_services(self, states):
        for snapshot in states:
            if snapshot.kind == KIND_SCRIPT:
                continue
            module = snapshot.module if snapshot.kind == KIND_MODULE else None
            if self._meta.has_multiple_runtimes and module is None:
                continue
            unit = self._units.get(snapshot.module)
            if not unit and not self._primary:
                continue
            row = self._rows.get(module)
            if row is None:
                row = _RuntimeRow(module)
                row.focus_requested.connect(self.log_requested)
                row.service_requested.connect(self.service_requested)
                self._rows[module] = row
                self._runtime_layout.addWidget(row)
            port = snapshot.port or snapshot.expected_port
            ports = tuple(p for p in (snapshot.ports or [port]) if p and 0 < p <= 65535)
            name = unit.name if unit else self._meta.name
            if module is None and self._primary:
                name = f"{name} · {self._primary.label}"
            row.update_runtime(name, snapshot.state, ports, bool(unit or self._primary))
        self._no_runtimes.setHidden(bool(self._rows))

    def _subscribe(self):
        if self._unsubscribe is None:
            self._unsubscribe = self._stats.add_listener(self._ai_event.emit)
            self.destroyed.connect(self._unsubscribe)
        self._refresh_events()

    def stop(self):
        if self._unsubscribe is not None:
            self.destroyed.disconnect(self._unsubscribe)
            self._unsubscribe()
            self._unsubscribe = None

    def closeEvent(self, event):
        self.stop()
        super().closeEvent(event)

    def showEvent(self, event):
        if self._unsubscribe is None:
            self._subscribe()
        super().showEvent(event)

    def refresh_events(self):
        self._refresh_events()

    def _refresh_events(self, _event=None):
        if self._unsubscribe is None:
            return
        events = self._stats.snapshot()["recent_ai_events"][-12:][::-1]
        entries = [
            (event, self._resolve_event_log is not None and self._resolve_event_log(event["target"]) is not None)
            for event in events
        ]
        if entries == self._displayed_events:
            return
        self._displayed_events = entries
        while self.event_rows.count():
            widget = self.event_rows.takeAt(0).widget()
            widget.hide()
            widget.deleteLater()
        if not entries:
            empty = QLabel("暂无 AI CLI 活动")
            empty.setProperty("role", "subtitle")
            self.event_rows.addWidget(empty)
            return
        rows = [_AiEventRow(event, clickable) for event, clickable in entries]
        for row in rows:
            row.activated.connect(self.event_log_requested)
            self.event_rows.addWidget(row)
            row.ensurePolished()
        for column in (0, 1, 3, 4):
            width = max(row.labels[column].sizeHint().width() for row in rows)
            for row in rows:
                row.labels[column].setFixedWidth(width)

    def refresh_theme(self):
        for widget, glyph in self._icons:
            if isinstance(widget, QLabel):
                widget.setPixmap(icon(glyph).pixmap(16, 16))
            else:
                widget.setIcon(icon(glyph))
        for row in self._rows.values():
            row._refresh_theme()
