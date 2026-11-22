"""本机用量统计。只接收预定义操作和简短的 CLI 目标摘要。"""
from __future__ import annotations

import json
import re
import threading
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Callable

from src.core.cli_contract import COMMANDS
from src.core.config import CONFIG_PATH

SCHEMA_VERSION = 1
RECENT_AI_LIMIT = 50
GUI_ACTIONS = frozenset({
    "shortcut:quick_open", "shortcut:content_search", "shortcut:endpoint_picker",
    "shortcut:recent_files", "file:open_preview", "file:unlock_edit", "git:open_viewer",
    "service:start", "service:stop", "service:restart", "workspace:enter",
    "workspace:create", "external:codex", "external:cc",
})
ERROR_CODES = frozenset({
    "action_failed", "action_not_found", "ambiguous_project", "ambiguous_target",
    "client_disconnected", "health_timeout", "http_health_failed", "inspection_failed",
    "invalid_aggregate_config", "invalid_arguments", "operation_failed",
    "operation_interrupted", "operation_not_found", "operation_request_conflict",
    "operation_wait_timeout", "project_not_found", "run_changed", "run_logs_unavailable",
    "service_operation_in_progress", "services_running", "target_not_found",
    "wait_timeout", "workspace_active", "workspace_delete_blocked",
})
_SHORT_NAME = re.compile(r"[\w][\w .-]{0,79}")


def short_target(value: str) -> str:
    if not isinstance(value, str) or "://" in value or value.startswith(("-", "~")):
        return ""
    name = PureWindowsPath(value).name.strip()
    return name if _SHORT_NAME.fullmatch(name) else ""


def _safe_code(code: str, ok: bool) -> str | None:
    if ok:
        return None
    return code if isinstance(code, str) and code in ERROR_CODES else "operation_failed"


def _valid_time(value) -> bool:
    if not isinstance(value, str) or len(value) > 40:
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


class UsageStats:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else CONFIG_PATH.with_name("usage-stats.json")
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._counters: dict[str, dict] = {"cli": {}, "gui": {}}
        self._recent_ai_events: deque[dict] = deque(maxlen=RECENT_AI_LIMIT)
        self._listeners: set[Callable[[dict], None]] = set()
        self._load()

    def _load(self) -> None:
        try:
            with self.path.open(encoding="utf-8") as stream:
                data = json.loads(stream.read(256 * 1024))
        except (OSError, ValueError, RecursionError):
            return
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
            return
        counters = data.get("counters")
        if isinstance(counters, dict):
            for group, accepted_keys in (("cli", COMMANDS), ("gui", GUI_ACTIONS)):
                entries = counters.get(group)
                if not isinstance(entries, dict):
                    continue
                for key, entry in entries.items():
                    if key not in accepted_keys or not isinstance(entry, dict):
                        continue
                    fields = ("count", "fail_count", "total_ms")
                    if (all(type(entry.get(field)) is int and entry[field] >= 0 for field in fields)
                            and entry["fail_count"] <= entry["count"]
                            and _valid_time(entry.get("last_used_at"))):
                        self._counters[group][key] = {
                            **{field: entry[field] for field in fields},
                            "last_used_at": entry["last_used_at"],
                        }
        events = data.get("recent_ai_events")
        if isinstance(events, list):
            for event in events[-RECENT_AI_LIMIT:]:
                if not isinstance(event, dict):
                    continue
                command = event.get("command")
                duration = event.get("duration_ms")
                ok = event.get("ok")
                if (isinstance(command, str) and command in COMMANDS and _valid_time(event.get("ts"))
                        and type(duration) is int and duration >= 0 and type(ok) is bool):
                    self._recent_ai_events.append({
                        "ts": event["ts"], "command": command,
                        "target": short_target(event.get("target", "")), "ok": ok,
                        "duration_ms": duration, "error_code": _safe_code(event.get("error_code"), ok),
                    })

    def add_listener(self, listener: Callable[[dict], None]) -> Callable[[], None]:
        """订阅新 AI 活动，返回取消订阅函数。回调在记录事件的线程执行。"""
        with self._lock:
            self._listeners.add(listener)

        def unsubscribe() -> None:
            with self._lock:
                self._listeners.discard(listener)

        return unsubscribe

    def record_cli(self, command: str, *, target: str = "", ok: bool = True,
                   duration_ms: float = 0, error_code: str | None = None) -> None:
        if not isinstance(command, str) or command not in COMMANDS:
            return
        event = None
        if command != "usage-stats":
            event = {"command": command, "target": short_target(target), "ok": ok,
                     "error_code": _safe_code(error_code, ok)}
        self._record("cli", command, ok, duration_ms, event)

    def record_gui(self, action: str, *, ok: bool = True, duration_ms: float = 0) -> None:
        if action in GUI_ACTIONS:
            self._record("gui", action, ok, duration_ms)

    def _record(self, group: str, key: str, ok: bool, duration_ms: float,
                event: dict | None = None) -> None:
        duration = max(0, round(duration_ms))
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        with self._lock:
            counter = self._counters[group].setdefault(
                key, {"count": 0, "fail_count": 0, "total_ms": 0},
            )
            counter["count"] += 1
            counter["fail_count"] += int(not ok)
            counter["total_ms"] += duration
            counter["last_used_at"] = timestamp
            if event is not None:
                event.update(ts=timestamp, duration_ms=duration)
                self._recent_ai_events.append(event)
            listeners = tuple(self._listeners) if event is not None else ()
            if self._timer is None:
                timer = threading.Timer(2, self.flush)
                timer.daemon = True
                try:
                    timer.start()
                except RuntimeError:
                    pass
                else:
                    self._timer = timer
        for listener in listeners:
            try:
                listener(dict(event))
            except Exception:
                continue

    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot()

    def _snapshot(self) -> dict:
        return {"schema_version": SCHEMA_VERSION, "counters": deepcopy(self._counters),
                "recent_ai_events": [dict(event) for event in self._recent_ai_events]}

    def flush(self) -> bool:
        """写入最新快照。文件读写失败时保留内存数据，后续仍可重试。"""
        with self._write_lock:
            with self._lock:
                if self._timer is not None:
                    self._timer.cancel()
                    self._timer = None
                data = self._snapshot()
            temporary = self.path.with_suffix(".json.tmp")
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                temporary.replace(self.path)
            except OSError:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
                return False
            return True


usage_stats = UsageStats()
