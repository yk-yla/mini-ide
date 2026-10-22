"""Bounded operation receipts; a disconnected client never owns the operation."""
from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid
from pathlib import Path


class OperationStore:
    def __init__(self, path: Path | None = None, limit: int = 64):
        self.path = path
        self.limit = limit
        self.records: dict[str, dict] = {}
        if path and path.exists():
            try:
                for item in json.loads(path.read_text(encoding="utf-8"))[-limit:]:
                    if item["status"] == "running":
                        item.update(status="interrupted", finished_at=time.time(), result={
                            "ok": False, "code": "operation_interrupted",
                            "error": "IDE restarted; reconcile actual state before starting another operation",
                        })
                    self.records[item["operation_id"]] = item
            except (OSError, ValueError, KeyError, TypeError):
                self.records = {}

    def begin(self, command: dict) -> tuple[dict, bool]:
        request_id = command.get("request_id", "")
        intent = {k: v for k, v in command.items() if k not in ("request_id", "async", "timeout")}
        fingerprint = hashlib.sha256(json.dumps(intent, sort_keys=True).encode()).hexdigest()
        if request_id:
            for record in self.records.values():
                if record["request_id"] == request_id:
                    if record["fingerprint"] != fingerprint:
                        raise ValueError("request-id is already bound to a different operation")
                    return copy.deepcopy(record), False
        if len(self.records) >= self.limit:
            finished = next((key for key, item in self.records.items() if item["status"] != "running"), None)
            if finished:
                del self.records[finished]
            else:
                raise ValueError("too many running operations")
        identifier = uuid.uuid4().hex
        record = {"operation_id": identifier, "request_id": request_id, "fingerprint": fingerprint,
                  "command": command["cmd"], "target": command.get("project") or command.get("target") or command.get("aggregate", ""),
                  "status": "running", "started_at": time.time(), "finished_at": None,
                  "progress": "accepted", "output_tail": "", "result": None}
        self.records[identifier] = record
        self._save()
        return copy.deepcopy(record), True

    def get(self, identifier: str) -> dict | None:
        record = self.records.get(identifier)
        return copy.deepcopy(record) if record else None

    def progress(self, identifier: str, text: str) -> None:
        record = self.records[identifier]
        if record["status"] == "running":
            record["progress"] = "executing"
            record["output_tail"] = (record["output_tail"] + text + "\n")[-12000:]

    def complete(self, identifier: str, result: dict) -> dict:
        record = self.records[identifier]
        record.update(status="succeeded" if result.get("ok") else "failed",
                      finished_at=time.time(), progress="finished", result=copy.deepcopy(result))
        self._save()
        return self.get(identifier)

    def _save(self) -> None:
        if self.path is None:
            return
        # Receipts are small and written only on accept/finish, never for each log line.
        snapshots = copy.deepcopy(list(self.records.values()))
        for item in snapshots:
            result = item.get("result")
            if isinstance(result, dict) and isinstance(result.get("output"), str):
                if len(result["output"]) > 12000:
                    result["output"] = result["output"][-12000:]
                    result["output_truncated"] = True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(snapshots, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.path)
        except OSError:
            # The running in-memory operation must still finish if receipt storage fails.
            for record in self.records.values():
                record["persistence_error"] = "operation receipt could not be saved"
