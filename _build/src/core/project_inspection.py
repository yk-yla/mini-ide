"""Read project facts on a worker, without opening tabs or starting services."""
from __future__ import annotations

from pathlib import Path

from src.core import git_context, git_ops
from src.core.path_utils import normalized_path_key
from src.core.project_detector import detect_project
from src.core.service_state import STATE_UNKNOWN
from src.core.target_resolver import ResolvedTarget, resolve_target_path

ACTION_KINDS = {"compile", "build", "test", "clean"}


def configured_actions(meta) -> list[dict]:
    return [{"id": profile.name, "label": profile.label, "kind": profile.kind,
             "description": profile.description, "requires_stopped_services": True}
            for profile in meta.profiles if profile.kind in ACTION_KINDS]


def _project_paths(resolved: ResolvedTarget) -> list[tuple[str, str, bool]]:
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
    return [item for item in components if not resolved.component_id or item[0] == resolved.component_id]


def inspect_directory(path: str, aggregate_paths=(), observations=None, full=False) -> dict:
    resolved = resolve_target_path(path, aggregate_paths)
    facts = resolved.to_dict()
    observations = observations or {}
    aggregate = resolved.aggregate_project
    workspace = resolved.development_workspace
    projects = []
    for identifier, project_path, shared in _project_paths(resolved):
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


def _snapshot_git(project_path: str) -> dict:
    repo = git_ops.repo_root(project_path)
    try:
        files = git_ops.list_changed_files(repo, strict=True)
    except RuntimeError as exc:
        return {"branch": "", "changed_count": None, "changed_files": [], "error": str(exc)}
    return {"branch": git_ops.current_branch(repo, refresh=True), "changed_count": len(files),
            "changed_files": [item.path for item in git_context.sort_changed_files(files)[:10]]}


def capture_snapshot(
    resolved: ResolvedTarget, observations: list[dict], recent_ai_events: list[dict],
    *, tail: int, max_chars: int,
) -> dict:
    target = resolved.to_dict()
    target.update(aggregate_name=target["aggregate"]["name"] if target["aggregate"] else "",
                  workspace_name=target["workspace"]["name"] if target["workspace"] else "")
    opened = {normalized_path_key(item["path"]): item for item in observations}
    aggregate = resolved.aggregate_project
    projects = []
    truncated = False
    remaining = max_chars
    for identifier, project_path, _ in _project_paths(resolved):
        observation = opened.get(normalized_path_key(project_path))
        definition = aggregate.component(identifier) if aggregate else None
        if observation is None:
            meta = detect_project(project_path, runtime_config=definition.runtime if definition else None)
            name, units = meta.name, meta.runtime_units
            default_port, health_path = meta.default_port, meta.health_check_path
            states, launches = {}, {}
        else:
            name, units = observation["name"], observation["units"]
            default_port, health_path = observation["default_port"], observation["health_path"]
            states, launches = observation["states"], observation["launches"]
        runtimes, errors = [], []
        for unit in units:
            state = states.get(unit.id)
            port = (state.port if state else None) or unit.expected_port or default_port
            ports = list(state.ports or []) if state else []
            health = unit.health_check_path or health_path
            url = f"http://127.0.0.1:{port}{health}" if port and health.startswith("/") and not health.startswith("//") else None
            runtimes.append({
                "id": unit.id, "name": unit.name, "state": state.state if state else STATE_UNKNOWN,
                "port": port, "ports": ports or ([port] if port else []),
                "pid": state.pid if state and state.pid and state.pid > 0 else None, "health_url": url,
            })
            launch = launches.get(unit.id)
            if launch is None:
                continue
            generation, output = launch
            block = output.latest_error_block(tail)
            if not block:
                continue
            text = "\n".join(block)
            shown = text[:remaining]
            clipped = len(shown) < len(text) or output.dropped
            remaining -= len(shown)
            errors.append({"id": unit.id, "name": unit.name, "generation": generation,
                           "run_id": output.run_id, "lines": shown.split("\n") if shown else [],
                           "truncated": clipped})
            truncated |= clipped
        projects.append({"id": identifier, "name": name, "path": project_path,
                         "observed": observation is not None, "runtimes": runtimes, "errors": errors,
                         "git": _snapshot_git(project_path)})
    return {"ok": True, "target": target, "projects": projects, "recent_ai_events": recent_ai_events,
            "truncated": truncated}
