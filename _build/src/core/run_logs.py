"""Bounded, cursor-addressable output for one actual service launch."""
from __future__ import annotations

from collections import deque
import re
import uuid


def is_error(line: str) -> bool:
    return bool(re.search(r"error|exception|traceback|failed|failure|caused by|错误|失败", line, re.I))


def is_continuation(line: str) -> bool:
    return bool(line[:1].isspace() or re.match(r"(?:Caused by:|Suppressed:|Traceback|\.\.\. \d+ more|\w*(?:Error|Exception):)", line))


def error_blocks(lines: list[str]) -> list[str]:
    selected = []
    active = False
    for line in lines:
        if is_error(line):
            active = True
            selected.append(line)
        elif active and is_continuation(line):
            selected.append(line)
        else:
            active = False
    return selected


class RunLog:
    def __init__(self, max_chars=256000, max_lines=4000):
        self.run_id = uuid.uuid4().hex
        self.rows = deque()
        self.sequence = 0
        self.characters = 0
        self.max_chars, self.max_lines = max_chars, max_lines
        self.dropped = False

    def append(self, line):
        self.sequence += 1
        if len(line) > 20000:
            line = line[:20000] + " [line truncated]"
            self.dropped = True
        self.rows.append((self.sequence, line))
        self.characters += len(line)
        while self.rows and (self.characters > self.max_chars or len(self.rows) > self.max_lines):
            self.characters -= len(self.rows.popleft()[1])
            self.dropped = True

    def read(self, *, tail=50, cursor=0, errors=False, max_chars=30000, run_id=None):
        if run_id and run_id != self.run_id:
            return {"ok": False, "code": "run_changed", "error": "requested run is no longer current",
                    "run_id": self.run_id}
        rows = list(self.rows)
        gap = self.dropped or bool(cursor and rows and cursor < rows[0][0] - 1)
        if cursor:
            first = next((index for index, row in enumerate(rows) if row[0] > cursor), len(rows))
            if errors and first < len(rows):
                while first > 0 and is_continuation(rows[first][1]):
                    first -= 1
            rows = rows[first:]
        else:
            first = max(0, len(rows) - tail)
            while first > 0 and is_continuation(rows[first][1]):
                first -= 1
            rows = rows[first:]
        result = []
        used = 0
        next_cursor = cursor
        active_error = False
        for sequence, text in rows:
            include = not errors or is_error(text) or active_error and is_continuation(text)
            active_error = is_error(text) or active_error and is_continuation(text)
            if include and used + len(text) > max_chars:
                remaining = max_chars - used
                if remaining:
                    result.append(text[:remaining])
                    next_cursor = max(cursor, sequence)
                gap = True
                break
            next_cursor = max(cursor, sequence)
            if include:
                result.append(text)
                used += len(text)
        return {"ok": True, "run_id": self.run_id, "lines": result, "next_cursor": next_cursor,
                "latest_cursor": self.sequence, "truncated": gap, "source": "launch_output"}
