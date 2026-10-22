"""需求工作区创建向导：选择会修改的项目并预览创建计划。"""
from __future__ import annotations

from src.core.search_worker_lifecycle import BackgroundThread

import logging
import time

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFormLayout, QHeaderView, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar, QTableWidget, QTableWidgetItem,
    QVBoxLayout,
)

from src.core.aggregate_workspace import AggregateProject
from src.core.development_workspace_service import build_workspace_creation_plan
from src.ui.theme import GAP_LG, GAP_MD, GAP_SM, H_DIALOG_TALL, H_TABLE_ROW, W_DIALOG_WIDE


PREFLIGHT_TIMEOUT_SECONDS = 30
action_log = logging.getLogger("mini-ide.action")


class _WorkspacePlanWorker(BackgroundThread):
    def __init__(
        self, project: AggregateProject, name: str, description: str,
        selected: tuple[str, ...], parent=None,
    ):
        super().__init__(parent)
        self.project = project
        self.name = name
        self.description = description
        self.selected = selected
        self.plan = None
        self.error = ""
        self.completed = False
        self.progress = (0, 1, "准备预检")

    def _report_progress(self, current: int, total: int, message: str) -> None:
        self.progress = (current, total, message)
        action_log.info("[GUI] 工作区预检进度 aggregate=%s step=%d/%d item=%s",
                        self.project.id, current, total, message)

    def run(self) -> None:
        try:
            self.plan = build_workspace_creation_plan(
                self.project, self.name, self.description, self.selected,
                progress=self._report_progress,
                cancelled=self.isInterruptionRequested,
                timeout=PREFLIGHT_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            self.error = str(exc)
        finally:
            # 结果随 Python 对象保留，不能依赖 QThread 已销毁后的 sender()。
            self.completed = True


class DevelopmentWorkspaceDialog(QDialog):
    def __init__(self, project: AggregateProject, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"新建需求工作区 - {project.name}")
        self.resize(W_DIALOG_WIDE, H_DIALOG_TALL)
        self.project = project
        self._plan = None
        self._worker: _WorkspacePlanWorker | None = None
        self._started = 0.0
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(100)
        self._poll_timer.timeout.connect(self._poll_plan)

        root = QVBoxLayout(self)
        root.setContentsMargins(GAP_LG, GAP_LG, GAP_LG, GAP_LG)
        root.setSpacing(GAP_MD)
        form = QFormLayout()
        form.setHorizontalSpacing(GAP_LG)
        form.setVerticalSpacing(GAP_SM)
        self.task_name = QLineEdit()
        self.task_name.setPlaceholderText("例如 直播系统对接")
        self.description = QPlainTextEdit()
        self.description.setPlaceholderText("任务目标和范围")
        form.addRow("任务名：", self.task_name)
        form.addRow("任务说明：", self.description)
        root.addLayout(form)

        hint = QLabel("只勾选本需求会修改的 Git 项目；未选择项目不会创建 Worktree。")
        hint.setProperty("role", "subtitle")
        root.addWidget(hint)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["修改", "项目", "类型", "来源目录"])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(H_TABLE_ROW)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.table, 1)
        for component in project.components:
            row = self.table.rowCount()
            self.table.insertRow(row)
            select = QTableWidgetItem("非 Git 项目" if component.shared else "")
            if component.shared:
                select.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            else:
                select.setFlags(
                    Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                    | Qt.ItemFlag.ItemIsUserCheckable
                )
                select.setCheckState(Qt.CheckState.Unchecked)
            select.setData(Qt.ItemDataRole.UserRole, component.id)
            self.table.setItem(row, 0, select)
            self.table.setItem(row, 1, QTableWidgetItem(component.id))
            self.table.setItem(row, 2, QTableWidgetItem(component.type))
            self.table.setItem(row, 3, QTableWidgetItem(str(
                component.absolute_path(project.root_path)
            )))

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        save = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        save.setText("预检并创建")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.progress_label = QLabel("")
        self.progress_label.setTextFormat(Qt.TextFormat.PlainText)
        self.progress_label.setWordWrap(True)
        self.progress_bar = QProgressBar()
        self.progress_label.hide()
        self.progress_bar.hide()
        root.addWidget(self.progress_label)
        root.addWidget(self.progress_bar)
        root.addWidget(self.buttons)

    def result_plan(self):
        return self._plan

    def _selected_components(self) -> tuple[str, ...]:
        selected = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                if item.checkState() == Qt.CheckState.Checked:
                    selected.append(str(item.data(Qt.ItemDataRole.UserRole)))
        return tuple(selected)

    def accept(self) -> None:
        if self._worker:
            return
        name = self.task_name.text().strip()
        selected = self._selected_components()
        if not name:
            QMessageBox.warning(self, "任务名不能为空", "请输入任务名。")
            return
        if not selected:
            QMessageBox.warning(self, "未选择项目", "至少选择一个会修改的 Git 项目。")
            return
        self._set_busy(True)
        self.progress_label.setText("准备预检…")
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_label.show()
        self.progress_bar.show()
        self._started = time.monotonic()
        worker = _WorkspacePlanWorker(
            self.project, name, self.description.toPlainText().strip(), selected,
            QApplication.instance(),
        )
        self._worker = worker
        worker.finished.connect(worker.deleteLater)
        self._poll_timer.start()
        action_log.info("[GUI] 工作区预检开始 aggregate=%s selected=%s",
                        self.project.id, ",".join(selected))
        try:
            worker.start()
        except Exception as exc:
            self._release_worker()
            worker.deleteLater()
            self._show_error(f"无法开始预检：{exc}")

    def _set_busy(self, busy: bool) -> None:
        self.task_name.setEnabled(not busy)
        self.description.setEnabled(not busy)
        self.table.setEnabled(not busy)
        save = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        save.setEnabled(not busy)
        save.setText("正在预检…" if busy else "预检并创建")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setEnabled(True)

    def _release_worker(self) -> None:
        worker, self._worker = self._worker, None
        self._poll_timer.stop()
        self._set_busy(False)
        if worker is not None and not worker.completed:
            try:
                worker.requestInterruption()
            except RuntimeError:
                pass

    def _show_error(self, error: str) -> None:
        self.progress_label.setText(error)
        action_log.warning("[GUI] 工作区预检失败 aggregate=%s error=%s", self.project.id, error)

    def _poll_plan(self) -> None:
        worker = self._worker
        if worker is None:
            return
        elapsed = time.monotonic() - self._started
        current, total, message = worker.progress
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(current)
        self.progress_label.setText(f"{message}（{current}/{total}） · 已用 {elapsed:.1f} 秒")
        if worker.completed:
            plan, error = worker.plan, worker.error
            self._release_worker()
            self._plan_ready(plan, error)
        elif elapsed >= PREFLIGHT_TIMEOUT_SECONDS:
            self._release_worker()
            self._show_error(f"预检超过 {PREFLIGHT_TIMEOUT_SECONDS} 秒，已停止等待。可修改后重试，或取消关闭。")

    def _plan_ready(self, plan, error: str) -> None:
        if error or plan is None:
            self._show_error(error or "后台检查已结束，但未返回预检结果，请重试。")
            return
        self.progress_label.setText("预检完成，等待确认创建计划。")
        self.progress_bar.setValue(self.progress_bar.maximum())
        action_log.info("[GUI] 工作区预检完成 aggregate=%s elapsed_ms=%.1f",
                        self.project.id, (time.monotonic() - self._started) * 1000)
        lines = [f"任务目录：{plan.root_path}", "", "将执行："]
        for item in plan.components:
            if item.mode == "shared":
                lines.append(f"- {item.id}：使用源目录，只读 {item.source_path}")
            else:
                lines.extend([
                    f"- {item.id}",
                    f"  基准：{item.base_branch} @ {item.base_commit[:12]}",
                    f"  分支：{item.task_branch}",
                    f"  目录：{item.target_path}",
                    "  远程：创建后推送任务分支到 origin 并设置上游",
                ])
        answer = QMessageBox.question(
            self, "确认创建计划", "\n".join(lines),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self.progress_label.setText("未创建工作区，可修改任务信息后重新预检。")
            return
        self._plan = plan
        super().accept()

    def reject(self) -> None:
        if self._worker is not None:
            action_log.info("[GUI] 取消工作区预检 aggregate=%s", self.project.id)
        self._release_worker()
        super().reject()
