"""发行包读取随包固定的版本信息；源码态读取仓库最新的 vX.Y.Z tag。"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def source_version(repo: Path) -> str:
    """读取仓库最新的 vX.Y.Z tag 并去掉 v 前缀；git 不可用或无 tag 时返回 unknown。"""
    try:
        from src.util.git_executable import resolve_git_executable
        result = subprocess.run(
            [resolve_git_executable(), "-C", str(repo), "tag", "--list", "v*", "--sort=-v:refname"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if TAG_RE.fullmatch(line.strip()):
                    return line.strip().removeprefix("v")
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def build_info() -> dict[str, str]:
    try:
        if getattr(sys, "frozen", False):
            root = Path(sys._MEIPASS)
            data = json.loads((root / "version.json").read_text(encoding="utf-8"))
            return {"version": str(data["version"]), "build_time": str(data["build_time"])}
        repo = Path(__file__).resolve().parents[3]
        return {"version": source_version(repo), "build_time": ""}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {"version": "unknown", "build_time": ""}
