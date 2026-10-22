"""Read project facts on a worker, without opening tabs or starting services."""
from __future__ import annotations

from pathlib import Path

from src.core import git_context
from src.core.project_detector import detect_project
from src.core.target_resolver import resolve_target_path

ACTION_KINDS = {"compile", "build", "test", "clean"}


def configured_actions(meta) -> list[dict]:
    return [{"id": profile.name, "label": profile.label, "kind": profile.kind,
             "description": profile.description, "requires_stopped_services": True}
            for profile in meta.profiles if profile.kind in ACTION_KINDS]


def inspect_directory(path: str, aggregate_paths=(), observations=None, full=False) -> dict:
    resolved = resolve_target_path(path, aggregate_paths)
    facts = resolved.to_dict()
    observations = observations or {}
    workspace = resolved.development_workspace
    aggregate = resolved.aggregate_project
    components = []
    if workspace:
        for component in workspace.components:
            components.append((component.id, component.worktree_path or component.source_repository_path, component.mode == "shared"))
    elif aggregate:
        components = [(component.id, str(Path(aggregate.root_path) / component.path), component.shared) for component in aggregate.components]
    else:
        components = [(Path(resolved.project_target).name, resolved.project_target, False)]
    if resolved.component_id:
        components = [item for item in components if item[0] == resolved.component_id]
    projects = []
    for identifier, project_path, shared in components:
        definition = aggregate.component(identifier) if aggregate else None
        meta = detect_project(project_path, runtime_config=definition.runtime if definition else None)
        observed = observations.get(str(Path(project_path).resolve()).casefold())
        git = git_context.summary_payload(project_path, include_files=full)
        # Summary context does not repeat full diff/file lists or global IDE state.
        item = {"id": identifier, "path": project_path, "type": meta.project_type,
                "shared": shared, "writable": not bool(workspace and shared),
                "observed": observed is not None, "states": observed or [],
                "actions": configured_actions(meta), "git": git,
                "runtime_units": [unit.to_dict() for unit in meta.runtime_units]}
        projects.append(item)
    return {"ok": True, "target": facts, "projects": projects,
            "observation_rule": "Unopened projects have no observed runtime state; absence is not stopped."}
