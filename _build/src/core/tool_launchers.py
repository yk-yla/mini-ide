"""Launch command builders for external coding tools."""
from __future__ import annotations

import os
import shutil
import winreg
from pathlib import Path
from urllib.parse import quote

CREATE_NO_WINDOW = 0x08000000

_AGENTDESK_APP_PATHS = [
    (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\App Paths\AgentDesk.exe"),
    (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\App Paths\AgentDesk.exe"),
]

_AGENTDESK_UNINSTALL_ROOTS = [
    (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
]


def _agentdesk_candidate(value: str) -> Path | None:
    raw = os.path.expandvars((value or "").strip())
    if not raw:
        return None
    if raw.startswith('"'):
        end = raw.find('"', 1)
        raw = raw[1:end] if end > 1 else raw.strip('"')
    raw = raw.rsplit(",", 1)[0].strip()
    candidate = Path(raw)
    return candidate if candidate.is_file() else None


def find_agentdesk() -> Path | None:
    """Locate AgentDesk from portable Windows installation metadata."""
    configured = _agentdesk_candidate(os.environ.get("AGENTDESK_EXE", ""))
    if configured:
        return configured

    on_path = shutil.which("AgentDesk.exe") or shutil.which("agentdesk")
    candidate = _agentdesk_candidate(on_path or "")
    if candidate:
        return candidate

    for hive, subkey in _AGENTDESK_APP_PATHS:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                value, _kind = winreg.QueryValueEx(key, "")
        except OSError:
            continue
        candidate = _agentdesk_candidate(str(value))
        if candidate:
            return candidate

    for hive, root in _AGENTDESK_UNINSTALL_ROOTS:
        try:
            with winreg.OpenKey(hive, root) as uninstall_root:
                subkey_count = winreg.QueryInfoKey(uninstall_root)[0]
                subkeys = [winreg.EnumKey(uninstall_root, index) for index in range(subkey_count)]
        except OSError:
            continue
        for name in subkeys:
            try:
                with winreg.OpenKey(hive, f"{root}\\{name}") as key:
                    display_name = str(winreg.QueryValueEx(key, "DisplayName")[0])
                    if not display_name.lower().startswith("agentdesk"):
                        continue
                    display_icon = str(winreg.QueryValueEx(key, "DisplayIcon")[0])
            except OSError:
                continue
            candidate = _agentdesk_candidate(display_icon)
            if candidate:
                return candidate
    return None


def open_in_agentdesk_args(target_dir: Path, provider: str = "") -> list[str]:
    """Open a new AgentDesk session for target_dir."""
    executable = find_agentdesk()
    if not executable:
        return []
    command = [str(executable), "--", "--cwd", str(target_dir)]
    if provider:
        command.append(f"--provider={provider}")
    return command


def open_in_codex_args(target_dir: Path) -> list[str]:
    """Use the same desktop deep link as Explorer's open gpt action."""
    return [
        str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "rundll32.exe"),
        "url.dll,FileProtocolHandler",
        f"codex://threads/new?path={quote(str(target_dir), safe='')}",
    ]


def open_in_cc_args(target_dir: Path) -> list[str]:
    """Open the cc entry in AgentDesk."""
    return open_in_agentdesk_args(target_dir, "claude")
