"""把任意目录确定性解析为普通项目、聚合目录或需求工作区目标。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from src.core.aggregate_workspace import (
    AggregateProject,
    DevelopmentWorkspace,
    aggregate_config_path,
    load_aggregate_project,
    load_development_workspace,
)
from src.core.path_utils import canonical_path, is_path_within, normalized_path_key


class TargetResolutionError(ValueError):
    """目标路径存在，但所属聚合目录或工作区无法安全解析。"""


@dataclass(frozen=True)
class ResolvedTarget:
    requested_path: str
    path: str
    kind: str
    open_command: str
    open_target: str
    project_target: str = ""
    aggregate_project: AggregateProject | None = None
    development_workspace: DevelopmentWorkspace | None = None
    component_id: str = ""
    component_name: str = ""
    component_path: str = ""

    def to_dict(self) -> dict:
        aggregate = self.aggregate_project
        workspace = self.development_workspace
        return {
            "ok": True,
            "requested_path": self.requested_path,
            "path": self.path,
            "kind": self.kind,
            "open_command": self.open_command,
            "open_target": self.open_target,
            "project_target": self.project_target,
            "aggregate": ({
                "id": aggregate.id,
                "name": aggregate.name,
                "path": aggregate.root_path,
            } if aggregate else None),
            "workspace": ({
                "id": workspace.id,
                "name": workspace.name,
                "path": workspace.root_path,
            } if workspace else None),
            "component": ({
                "id": self.component_id,
                "name": self.component_name or self.component_id,
                "path": self.component_path,
            } if self.component_id else None),
        }


def _ancestors(path: Path) -> tuple[Path, ...]:
    return (path, *path.parents)


def _normal_target(requested: str, path: Path, boundary: Path | None = None) -> ResolvedTarget:
    # 最近的项目标记（包括嵌套仓库）就是边界，不跨越聚合根寻找普通项目。
    markers = (
        "build.gradle", "build.gradle.kts", "pom.xml", "package.json",
        "pyproject.toml", "requirements.txt", "manage.py", "go.mod",
        ".mini-ide/runtime.json",
    )
    project = path
    for root in _ancestors(path):
        if root == boundary:
            break
        if ((root / ".git").exists()
                or any((root / marker).is_file() for marker in markers)
                or ((root / "nginx.exe").is_file() and (
                    (root / "nginx.conf").is_file() or (root / "conf/nginx.conf").is_file()))):
            project = root
            break
    return ResolvedTarget(
        requested_path=requested, path=str(path), kind="normal_project",
        open_command="open", open_target=str(project), project_target=str(project),
    )


def _workspace_component(
    path: Path,
    workspace: DevelopmentWorkspace,
    aggregate: AggregateProject,
) -> tuple[str, str, str] | None:
    matches: list[tuple[int, str, str, str]] = []
    for item in workspace.components:
        component_path = item.worktree_path or item.source_repository_path
        if not component_path or not is_path_within(path, component_path):
            continue
        definition = aggregate.component(item.id)
        name = definition.name if definition and definition.name else item.id
        depth = len(canonical_path(component_path).parts)
        matches.append((depth, item.id, name, str(canonical_path(component_path))))
    if not matches:
        return None
    _, component_id, name, component_path = max(matches, key=lambda item: item[0])
    return component_id, name, component_path


def _aggregate_component(
    path: Path,
    aggregate: AggregateProject,
) -> tuple[str, str, str] | None:
    matches: list[tuple[int, str, str, str]] = []
    for item in aggregate.components:
        component_path = item.absolute_path(aggregate.root_path)
        if not is_path_within(path, component_path):
            continue
        depth = len(component_path.parts)
        matches.append((
            depth, item.id, item.name or item.id, str(component_path),
        ))
    if not matches:
        return None
    _, component_id, name, component_path = max(matches, key=lambda item: item[0])
    return component_id, name, component_path


def _load_workspace_ancestor(
    path: Path,
) -> tuple[DevelopmentWorkspace, AggregateProject] | None:
    for root in _ancestors(path):
        if not (root / "workspace.json").is_file():
            continue
        try:
            draft = load_development_workspace(root)
            aggregate = load_aggregate_project(draft.aggregate_project_path)
            workspace = load_development_workspace(root, aggregate_project=aggregate)
        except Exception as exc:
            raise TargetResolutionError(
                f"invalid development workspace config: {root}: {exc}"
            ) from exc
        if is_path_within(path, workspace.root_path):
            return workspace, aggregate
    return None


def _aggregate_roots(
    path: Path,
    registered_aggregate_paths: Iterable[str | Path],
) -> tuple[Path, ...]:
    roots: list[Path] = []
    seen: set[str] = set()

    for ancestor in _ancestors(path):
        if not aggregate_config_path(ancestor).is_file():
            continue
        key = normalized_path_key(ancestor)
        if key not in seen:
            seen.add(key)
            roots.append(ancestor)

    registered = []
    for value in registered_aggregate_paths:
        root = canonical_path(value)
        if is_path_within(path, root):
            registered.append(root)
    registered.sort(key=lambda item: len(item.parts), reverse=True)
    for root in registered:
        key = normalized_path_key(root)
        if key not in seen:
            seen.add(key)
            roots.append(root)
    return tuple(roots)


def resolve_target_path(
    path: str | Path,
    registered_aggregate_paths: Iterable[str | Path] = (),
) -> ResolvedTarget:
    """优先解析工作区/聚合归属；普通目录向上找到最近的项目边界。"""
    requested = str(path)
    target = canonical_path(path)
    if not target.is_dir():
        raise TargetResolutionError(f"path is not a directory: {requested}")

    workspace_record = _load_workspace_ancestor(target)
    if workspace_record is not None:
        workspace, aggregate = workspace_record
        component = _workspace_component(target, workspace, aggregate)
        if component:
            component_id, component_name, component_path = component
            kind = "workspace_component"
        else:
            component_id = component_name = component_path = ""
            kind = "workspace"
        return ResolvedTarget(
            requested_path=requested,
            path=str(target),
            kind=kind,
            open_command="open-workspace",
            open_target=workspace.root_path,
            project_target=component_path,
            aggregate_project=aggregate,
            development_workspace=workspace,
            component_id=component_id,
            component_name=component_name,
            component_path=component_path,
        )

    roots = _aggregate_roots(target, registered_aggregate_paths)
    if roots:
        root = roots[0]
        try:
            aggregate = load_aggregate_project(root)
        except Exception as exc:
            raise TargetResolutionError(
                f"invalid aggregate project config: {root}: {exc}"
            ) from exc
        component = _aggregate_component(target, aggregate)
        if component:
            component_id, component_name, component_path = component
            kind = "aggregate_component"
        else:
            component_id = component_name = component_path = ""
            kind = "aggregate"
            if normalized_path_key(target) != normalized_path_key(aggregate.root_path):
                return _normal_target(requested, target, boundary=root)
        return ResolvedTarget(
            requested_path=requested,
            path=str(target),
            kind=kind,
            open_command="open-aggregate",
            open_target=aggregate.root_path,
            project_target=component_path,
            aggregate_project=aggregate,
            component_id=component_id,
            component_name=component_name,
            component_path=component_path,
        )

    return _normal_target(requested, target)
