"""CLI command descriptions, validation and machine-readable results."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

SCHEMA_VERSION = 1
SERVER_NAME = os.environ.get("MINI_IDE_SERVER_NAME", "mini-ide-single-instance")


def _command(summary, positional=(), options=(), effect="read", offline=False):
    return {"summary": summary, "positional": list(positional), "options": list(options),
            "effect": effect, "requires_gui": not offline}


COMMANDS = {
    "help": _command("Show help for all commands or one command", ("command?",), offline=True),
    "capabilities": _command("Describe commands, arguments and side effects", offline=True),
    "version": _command("Show this CLI package version", offline=True),
    "status": _command("Snapshot open projects and services"),
    "list-projects": _command("List open projects"),
    "list-aggregates": _command("List registered aggregates"),
    "list-workspaces": _command("List registered development workspaces"),
    "resolve-target": _command("Resolve a directory to its environment and component", ("path",)),
    "context": _command("Inspect a directory without opening or switching tabs", ("path",), ("full",)),
    "list-modules": _command("List observed runtime states", ("project",)),
    "list-runtimes": _command("Describe runtime units, including unopened projects", ("project",)),
    "list-actions": _command("List configured build/test/install/clean actions", ("project",)),
    "open": _command("Open the owning environment", ("path",), effect="ui"),
    "open-aggregate": _command("Open a configured aggregate path or a registered ID/name", ("target",), effect="ui"),
    "open-workspace": _command("Activate a workspace; running services block environment switching", ("target",), ("async", "request-id"), effect="ui"),
    "close": _command("Stop services and close the specified project", ("project",), effect="services"),
    "close-workspace": _command("Stop and exit the specified active workspace", ("target",), effect="services"),
    "create-development-workspace": _command("Create selected component worktrees", ("aggregate", "name"), ("projects", "description", "async", "request-id"), "workspace"),
    "workspace-review": _command("Inspect Git and update workspace metadata", ("target",), ("async", "request-id"), "workspace"),
    "workspace-commit-push": _command("Commit and push task branches", ("target",), ("message", "async", "request-id"), "git_remote"),
    "workspace-sync": _command("Merge base changes into task branches", ("target",), ("fetch", "keep-conflicts", "async", "request-id"), "git"),
    "workspace-sync-push": _command("Commit, fetch and merge base updates, then push all task branches", ("target",), ("message", "keep-conflicts", "async", "request-id"), "git_remote"),
    "workspace-merge": _command("Fast-forward task branches into their recorded base branches and push", ("target",), ("async", "request-id"), "git_remote"),
    "workspace-delete-check": _command("Fetch refs and inspect deletion prerequisites", ("target",), ("async", "request-id"), "git_remote"),
    "workspace-delete": _command("Delete an inactive merged workspace and its local/remote task branches", ("target",), ("async", "request-id"), "delete"),
    "start": _command("Start runtime units; --wait verifies startup readiness", ("project", "module?"), ("wait", "timeout", "async", "request-id"), "services"),
    "stop": _command("Stop specified runtime units and wait for cleanup", ("project", "module?"), ("wait", "timeout", "async", "request-id"), "services"),
    "restart": _command("Restart specified runtime units", ("project", "module?"), ("wait", "timeout", "async", "request-id"), "services"),
    "ensure-running": _command("Start if needed and verify startup readiness", ("project", "module?"), ("timeout", "async", "request-id"), "services"),
    "health": _command("Check startup readiness; --http also probes configured HTTP health", ("project", "module?"), ("timeout", "http")),
    "compile": _command("Run the configured compile action (may install dependencies)", ("project",), ("timeout", "async", "request-id"), "build"),
    "run-action": _command("Run one exact configured build/test/install/clean action", ("project", "action"), ("timeout", "async", "request-id"), "build"),
    "operation": _command("Read a previous operation or wait for its final result", ("operation_id",), ("wait-seconds",)),
    "log": _command("Read logs; --current-run avoids historical UI output", ("project", "module?"), ("tail", "errors", "all-modules", "current-run", "cursor", "run-id", "max-chars")),
    "diagnose": _command("Get runtime observations and complete error blocks", ("project", "module?"), ("tail", "current-run", "cursor", "run-id", "max-chars")),
    "git-status": _command("Read branch and changed files", ("project",)),
    "git-diff": _command("Read bounded diff", ("project",), ("full", "summary", "max-chars")),
    "git-ai-context": _command("Read bounded Git context", ("project",), ("full", "summary", "max-chars")),
    "preflight-build": _command("Check target project, or all projects when omitted", ("project?",)),
    "can-quit": _command("Check all running services before quit"),
    "quit": _command("Save files, stop all services and quit the IDE", effect="services"),
}

VALUE_OPTIONS = {"projects", "description", "message", "timeout", "wait-seconds", "tail", "cursor", "run-id", "max-chars", "request-id"}
INTEGER_OPTIONS = {"timeout": (1, 86400), "wait-seconds": (0, 3600), "tail": (1, 10000),
                   "cursor": (0, 2**63 - 1), "max-chars": (100, 1000000)}
TRACKED_COMMANDS = {name for name, spec in COMMANDS.items() if "async" in spec["options"]}


class ArgumentError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ArgumentError(message)


def is_path_target(value: str) -> bool:
    return value in (".", "..") or any(c in value for c in ("/", "\\", ":")) or value.startswith("~")


def parse_command(args: list[str]) -> dict:
    if not args:
        raise ArgumentError("a command is required; use --help")
    name = args[0].lstrip("-")
    if name not in COMMANDS:
        raise ArgumentError(f"unknown command: {args[0]}")
    spec = COMMANDS[name]
    parser = _Parser(prog=f"mini-ide-cli --{name}", add_help=False, allow_abbrev=False)
    for positional in spec["positional"]:
        optional = positional.endswith("?")
        kwargs = {"nargs": "+"} if name == "open-workspace" else ({"nargs": "?"} if optional else {})
        parser.add_argument(positional.rstrip("?"), **kwargs)
    for option in spec["options"]:
        kwargs = {"dest": option.replace("-", "_")}
        if option not in VALUE_OPTIONS:
            kwargs["action"] = "store_true"
        parser.add_argument("--" + option, **kwargs)
    parsed = {k: v for k, v in vars(parser.parse_args(args[1:])).items() if v is not None and v is not False}
    if name == "open-workspace":
        parsed["target"] = " ".join(parsed["target"]).strip()
    for field in ("path", "project", "target", "aggregate", "name", "action", "message", "operation_id"):
        if field in parsed and not parsed[field].strip():
            raise ArgumentError(f"{field} must not be empty")
    for key, (minimum, maximum) in INTEGER_OPTIONS.items():
        field = key.replace("-", "_")
        if field in parsed:
            try:
                value = int(parsed[field])
            except ValueError as exc:
                raise ArgumentError(f"--{key} requires an integer") from exc
            if not minimum <= value <= maximum:
                raise ArgumentError(f"--{key} must be between {minimum} and {maximum}")
            parsed[field] = value
    if parsed.pop("full", False):
        if parsed.pop("summary", False):
            raise ArgumentError("--full and --summary cannot be combined")
        parsed["mode"] = "full"
    elif parsed.pop("summary", False) or name in ("git-diff", "git-ai-context"):
        parsed["mode"] = "summary"
    if name == "context":
        parsed["full"] = parsed.pop("mode", "summary") == "full"
    if name == "create-development-workspace":
        selected = [x.strip() for x in parsed.get("projects", "").split(",") if x.strip()]
        if not selected:
            raise ArgumentError("--projects requires at least one component")
        parsed["projects"] = selected
    if "request_id" in parsed and not 1 <= len(parsed["request_id"]) <= 128:
        raise ArgumentError("--request-id must contain 1 to 128 characters")
    if name == "workspace-sync" and "fetch" in parsed:
        parsed["fetch_remote"] = parsed.pop("fetch")
    if name in ("health", "ensure-running") or name in ("start", "stop", "restart") and parsed.get("wait"):
        parsed.setdefault("timeout", 60)
    if name in ("compile", "run-action"):
        parsed.setdefault("timeout", 300)
    if name in ("log", "diagnose"):
        parsed.setdefault("tail", 50 if name == "log" else 120)
        if "cursor" in parsed or "run_id" in parsed:
            parsed["current_run"] = True
        if parsed.get("all_modules") and parsed.get("module"):
            raise ArgumentError("--all-modules cannot be combined with one module")
    return {"cmd": "preflight-build" if name == "can-quit" else name, **parsed}


def normalize_targets(command: dict, cwd: str | None = None) -> dict:
    result = dict(command)
    base = Path(cwd or os.getcwd())
    for field in ("path", "project", "target", "aggregate"):
        value = result.get(field)
        if value and (field == "path" or is_path_target(value)):
            path = Path(value).expanduser()
            result[field] = str((path if path.is_absolute() else base / path).resolve())
    return result


def capabilities() -> dict:
    from src.core.app_version import build_info
    return {"ok": True, "schema_version": SCHEMA_VERSION, "cli_version": build_info()["version"], "commands": COMMANDS,
            "global_options": ["--machine", "--auto-start"],
            "option_schema": {option: {"takes_value": option in VALUE_OPTIONS,
                "type": "integer" if option in INTEGER_OPTIONS else ("string" if option in VALUE_OPTIONS else "boolean"),
                **({"minimum": INTEGER_OPTIONS[option][0], "maximum": INTEGER_OPTIONS[option][1]} if option in INTEGER_OPTIONS else {})}
                for option in sorted({option for spec in COMMANDS.values() for option in spec["options"]})},
            "long_operations": {"async": "return an operation_id immediately", "query": "--operation ID --wait-seconds N",
                                "request_id": "deduplicate retries; reuse only for the same operation"},
            "target_rule": "Paths are relative to the caller; bare IDs must be unique. Prefer returned absolute project_target (nearest ordinary project root); if empty, select a component path from context.projects.",
            "health_rule": "Startup readiness is not business verification. HTTP is checked only with --http."}


def help_text(name: str | None = None) -> str:
    if name:
        name = name.lstrip("-")
        if name not in COMMANDS:
            raise ArgumentError(f"unknown command: {name}")
    lines = ["Usage: mini-ide-cli <command> [arguments] [--machine] [--auto-start]"]
    for key in ([name] if name else COMMANDS):
        spec = COMMANDS[key]
        arguments = " ".join((f"[{x[:-1]}]" if x.endswith("?") else f"<{x}>") for x in spec["positional"])
        flags = " ".join("[--" + x + (" VALUE" if x in VALUE_OPTIONS else "") + "]" for x in spec["options"])
        lines.append(f"  --{key} {arguments} {flags}\n      {spec['summary']} (effect: {spec['effect']})")
    return "\n".join(lines) + "\n"


def result_code(result) -> str:
    if not isinstance(result, dict) or result.get("ok") is not False:
        return "ok"
    if result.get("code"):
        return result["code"]
    text = str(result.get("error", "")).lower()
    for word, code in (("timeout", "wait_timeout"), ("mini-ide is not running", "ide_not_running"),
                       ("not found", "target_not_found"), ("ambiguous", "ambiguous_target"),
                       ("invalid", "invalid_arguments"), ("no compile", "action_not_found")):
        if word in text:
            return code
    return "operation_failed"


def machine_result(command: str, result) -> dict:
    ok = not isinstance(result, dict) or result.get("ok") is not False
    return {"schema_version": SCHEMA_VERSION, "command": command, "ok": ok,
            "data": result if ok else None,
            "error": None if ok else {"code": result_code(result), "message": result.get("error", "operation failed"),
                                        "details": {k: v for k, v in result.items() if k not in ("ok", "error", "code")}}}
