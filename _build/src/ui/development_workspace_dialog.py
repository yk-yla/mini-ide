"""需求工作区创建向导：选择会修改的项目并预览创建计划。"""
from __future__ import annotations

from src.core.search_worker_lifecycle import BackgroundThread

from pathlib import Path

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFormLayout, QHeaderView, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QTableWidget, QTableWidgetItem,
    QVBoxLayout,
)

from src.core.aggregate_workspace import AggregateProject
from src.core.development_workspace_service import build_workspace_creation_plan
from src.ui.theme import GAP_LG, GAP_MD, GAP_SM, H_DIALOG_TALL, H_TABLE_ROW, W_DIALOG_WIDE


class _WorkspacePlanWorker(BackgroundThread):
    done = Signal(object, str)

    def __init__(
        self, project: AggregateProject, name: str, description: str,
        selected: tuple[str, ...], parent=None,
    ):
        super().__init__(parent)
        self.project = project
        self.name = name
        self.description = description
        self.selected = selected

    def run(self) -> None:
        try:
            plan = build_workspace_creation_plan(
                self.project, self.name, self.description, self.selected,
            )
            self.done.emit(plan, "")
        except Exception as exc:
            self.done.emit(None, str(exc))


class DevelopmentWorkspaceDialog(QDialog):
    def __init__(self, project: AggregateProject, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"新建需求工作区 - {project.name}")
        self.resize(W_DIALOG_WIDE, H_DIALOG_TALL)
        self.project = project
        self._plan = None
        self._worker: _WorkspacePlanWorker | None = None

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
        self.buttons.setEnabled(False)
        worker = _WorkspacePlanWorker(
            self.project, name, self.description.toPlainText().strip(), selected,
            QApplication.instance(),
        )
        self._worker = worker
        worker.done.connect(self._plan_ready)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _plan_ready(self, plan, error: str) -> None:
        if self.sender() is not self._worker:
            return
        self._worker = None
        self.buttons.setEnabled(True)
        if error:
            QMessageBox.warning(self, "创建预检失败", error)
            return
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
                ])
        answer = QMessageBox.question(
            self, "确认创建计划", "\n".join(lines),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._plan = plan
        super().accept()

    def reject(self) -> None:
        if self._worker and self._worker.isRunning():
            return
        super().reject()
