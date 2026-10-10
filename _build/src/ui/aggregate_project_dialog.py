"""聚合项目定义确认与编辑对话框。"""
from __future__ import annotations

from src.core.search_worker_lifecycle import BackgroundThread

from pathlib import Path

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from src.core.aggregate_workspace import AggregateProject
from src.core.aggregate_workspace_manager import (
    component_definition_from_path, scan_aggregate_components, suggest_stable_id,
)
from src.core.path_utils import canonical_path, is_path_within, normalized_path_key
from src.ui.theme import (
    COLOR_WARN, GAP_LG, GAP_MD, GAP_SM, H_DIALOG_TALL, H_TABLE_ROW,
    W_DIALOG_WIDE,
)


class _ComponentDetectWorker(BackgroundThread):
    done = Signal(object, str)

    def __init__(
        self, aggregate_root: str, component_path: str, used_ids: set[str], parent=None,
    ):
        super().__init__(parent)
        self.aggregate_root = aggregate_root
        self.component_path = component_path
        self.used_ids = used_ids

    def run(self) -> None:
        try:
            component = component_definition_from_path(
                self.aggregate_root, self.component_path, self.used_ids,
            )
            self.done.emit(component, "")
        except Exception as exc:
            self.done.emit(None, str(exc))


class _AggregateScanWorker(BackgroundThread):
    done = Signal(object, str)

    def __init__(self, aggregate_root: str, workspace_directory: str, parent=None):
        super().__init__(parent)
        self.aggregate_root = aggregate_root
        self.workspace_directory = workspace_directory

    def run(self) -> None:
        try:
            self.done.emit(
                scan_aggregate_components(
                    self.aggregate_root, self.workspace_directory,
                ),
                "",
            )
        except Exception as exc:
            self.done.emit((), str(exc))


class AggregateProjectDialog(QDialog):
    def __init__(
        self,
        project: AggregateProject | None,
        *,
        title: str,
        root_path: str = "",
        omitted_candidates: tuple[str, ...] = (),
        missing_paths: tuple[str, ...] = (),
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(W_DIALOG_WIDE, H_DIALOG_TALL)
        self._aggregate_root = str(canonical_path(
            project.root_path if project is not None else root_path
        ))
        self._source_profiles = project.profiles if project is not None else ()
        self._schema_version = project.schema_version if project is not None else 1
        self._result_project: AggregateProject | None = None
        self._detect_worker: _ComponentDetectWorker | None = None
        self._scan_worker: _AggregateScanWorker | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(GAP_LG, GAP_LG, GAP_LG, GAP_LG)
        root.setSpacing(GAP_MD)

        form = QFormLayout()
        form.setHorizontalSpacing(GAP_LG)
        form.setVerticalSpacing(GAP_SM)
        self.root_path = QLineEdit(self._aggregate_root)
        self.root_path.setReadOnly(True)
        default_id = project.id if project is not None else suggest_stable_id(
            Path(self._aggregate_root).name
        )
        self.project_id = QLineEdit(default_id)
        self.project_name = QLineEdit(
            project.name if project is not None else Path(self._aggregate_root).name
        )
        self.workspace_directory = QLineEdit(
            project.workspace_directory if project is not None else "workspace"
        )
        form.addRow("根目录：", self.root_path)
        form.addRow("稳定 ID：", self.project_id)
        form.addRow("名称：", self.project_name)
        form.addRow("临时工作区目录：", self.workspace_directory)
        root.addLayout(form)

        warning_lines = []
        if omitted_candidates:
            names = ", ".join(Path(path).name for path in omitted_candidates)
            warning_lines.append(f"可能遗漏：{names}")
        if missing_paths:
            warning_lines.append(
                "旧路径不存在：" + ", ".join(Path(path).name for path in missing_paths)
            )
        if warning_lines:
            warning = QLabel("\n".join(warning_lines))
            warning.setWordWrap(True)
            warning.setStyleSheet(f"color: {COLOR_WARN};")
            warning.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            root.addWidget(warning)

        component_header = QHBoxLayout()
        component_header.setSpacing(GAP_SM)
        component_title = QLabel("项目")
        font = component_title.font()
        font.setBold(True)
        component_title.setFont(font)
        component_header.addWidget(component_title)
        component_header.addStretch(1)
        self.add_button = QPushButton("添加项目")
        self.add_button.clicked.connect(self._choose_component)
        component_header.addWidget(self.add_button)
        self.scan_button = QPushButton("扫描候选")
        self.scan_button.clicked.connect(self._scan_components)
        component_header.addWidget(self.scan_button)
        self.remove_button = QPushButton("移除项目")
        self.remove_button.clicked.connect(self._remove_selected_component)
        component_header.addWidget(self.remove_button)
        root.addLayout(component_header)

        self.component_table = QTableWidget(0, 6)
        self.component_table.setHorizontalHeaderLabels(
            ["稳定 ID", "名称", "相对路径", "类型", "需求工作区", "状态"]
        )
        self.component_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.component_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.component_table.verticalHeader().setVisible(False)
        self.component_table.verticalHeader().setDefaultSectionSize(H_TABLE_ROW)
        header = self.component_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.component_table, 1)
        for component in project.components if project is not None else ():
            self._append_component(component)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def result_project(self) -> AggregateProject | None:
        return self._result_project

    def _append_component(self, component) -> None:
        row = self.component_table.rowCount()
        self.component_table.insertRow(row)
        values = [component.id, component.name, component.path, component.type]
        for column, value in enumerate(values):
            self.component_table.setItem(row, column, QTableWidgetItem(value))

        shared = QTableWidgetItem(
            "仅使用源目录" if component.shared else "可创建 Worktree"
        )
        shared.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        shared.setData(Qt.ItemDataRole.UserRole, component.shared)
        self.component_table.setItem(row, 4, shared)

        path = component.absolute_path(self._aggregate_root)
        state = QTableWidgetItem("可用" if path.is_dir() else "路径不存在")
        state.setFlags(state.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.component_table.setItem(row, 5, state)

    def _choose_component(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self, "选择项目目录", self._aggregate_root,
        )
        if not selected:
            return
        root = canonical_path(self._aggregate_root)
        path = canonical_path(selected)
        if not is_path_within(path, root, allow_equal=False):
            QMessageBox.warning(self, "项目路径无效", "项目必须位于聚合目录内。")
            return
        existing = {
            normalized_path_key(root / self._cell_text(row, 2))
            for row in range(self.component_table.rowCount())
        }
        if normalized_path_key(path) in existing:
            QMessageBox.information(self, "项目已存在", f"已包含项目：{path.name}")
            return
        if self._detect_worker and self._detect_worker.isRunning():
            return
        used_ids = {
            self._cell_text(row, 0).casefold()
            for row in range(self.component_table.rowCount())
        }
        self.add_button.setEnabled(False)
        worker = _ComponentDetectWorker(
            str(root), str(path), used_ids, QApplication.instance(),
        )
        self._detect_worker = worker
        worker.done.connect(self._component_detected)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _component_detected(self, component, error: str) -> None:
        if self.sender() is not self._detect_worker:
            return
        self._detect_worker = None
        self.add_button.setEnabled(True)
        if error:
            QMessageBox.warning(self, "项目识别失败", error)
            return
        self._append_component(component)

    def _scan_components(self) -> None:
        if self._scan_worker or self._detect_worker:
            return
        self.scan_button.setEnabled(False)
        worker = _AggregateScanWorker(
            self._aggregate_root,
            self.workspace_directory.text().strip() or "workspace",
            QApplication.instance(),
        )
        self._scan_worker = worker
        worker.done.connect(self._components_scanned)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _components_scanned(self, components, error: str) -> None:
        if self.sender() is not self._scan_worker:
            return
        self._scan_worker = None
        self.scan_button.setEnabled(True)
        if error:
            QMessageBox.warning(self, "扫描候选失败", error)
            return
        existing = {
            normalized_path_key(
                canonical_path(self._aggregate_root) / self._cell_text(row, 2)
            )
            for row in range(self.component_table.rowCount())
        }
        added = 0
        for component in components:
            path_key = normalized_path_key(
                component.absolute_path(self._aggregate_root)
            )
            if path_key in existing:
                continue
            self._append_component(component)
            existing.add(path_key)
            added += 1
        QMessageBox.information(
            self, "扫描完成", f"发现 {len(components)} 个候选，新增 {added} 个到确认表。",
        )

    def _remove_selected_component(self) -> None:
        rows = self.component_table.selectionModel().selectedRows()
        if rows:
            self.component_table.removeRow(rows[0].row())

    def _cell_text(self, row: int, column: int) -> str:
        item = self.component_table.item(row, column)
        return item.text().strip() if item else ""

    def accept(self) -> None:
        if (
            (self._detect_worker and self._detect_worker.isRunning())
            or (self._scan_worker and self._scan_worker.isRunning())
        ):
            QMessageBox.information(self, "项目识别中", "请等待当前项目识别完成。")
            return
        components = []
        for row in range(self.component_table.rowCount()):
            shared_item = self.component_table.item(row, 4)
            components.append({
                "id": self._cell_text(row, 0),
                "name": self._cell_text(row, 1),
                "path": self._cell_text(row, 2),
                "type": self._cell_text(row, 3),
                "shared": bool(
                    shared_item and shared_item.data(Qt.ItemDataRole.UserRole)
                ),
            })
        try:
            self._result_project = AggregateProject.from_dict(
                self._aggregate_root,
                {
                    "schemaVersion": self._schema_version,
                    "id": self.project_id.text().strip(),
                    "name": self.project_name.text().strip(),
                    "kind": "aggregate",
                    "workspaceDirectory": self.workspace_directory.text().strip(),
                    "components": components,
                    "profiles": [
                        profile.to_dict() for profile in self._source_profiles
                    ],
                },
            )
        except Exception as exc:
            QMessageBox.warning(self, "聚合项目定义无效", str(exc))
            return
        super().accept()
