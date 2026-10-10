"""源码以 pyproject.toml 为准；发行包读取随包固定的版本信息。"""
from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path


def build_info() -> dict[str, str]:
    try:
        if getattr(sys, "frozen", False):
            root = Path(sys._MEIPASS)
            data = json.loads((root / "version.json").read_text(encoding="utf-8"))
            return {"version": str(data["version"]), "build_time": str(data["build_time"])}
        root = Path(__file__).resolve().parents[2]
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        return {"version": str(data["tool"]["poetry"]["version"]), "build_time": ""}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {"version": "unknown", "build_time": ""}
