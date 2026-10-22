"""统一服务运行状态模型。

这个模块只放纯数据结构，不依赖 Qt UI。GUI、CLI、退出检查都应该从这里
拿同一套状态语义，避免各自拼 dict 后出现展示不一致。
"""
from __future__ import annotations

from dataclasses import dataclass
import time


STATE_STOPPED = "stopped"
STATE_STARTING = "starting"
STATE_RUNNING_MANAGED = "running_managed"
STATE_RUNNING_EXTERNAL = "running_external"
STATE_STOPPING = "stopping"
STATE_UNKNOWN = "unknown"

SOURCE_NONE = "none"
SOURCE_MANAGED_RUNNER = "managed_runner"
SOURCE_EXTERNAL_DETECTOR = "external_detector"
SOURCE_SCRIPT_RUNNER = "script_runner"

class EnvironmentSwitchBlocked(RuntimeError):
    """无人值守切换环境时，保留服务现场并向调用端报告阻塞。"""

    def __init__(self, running: list[dict], source_path: str, target_path: str):
        super().__init__("environment has running services; stop the listed services before switching")
        self.result = {
            "ok": False, "code": "services_running", "error": str(self),
            "running": running, "source_path": source_path, "target_path": target_path,
        }


KIND_PROJECT = "project"
KIND_MODULE = "module"
KIND_SCRIPT = "script"


@dataclass(frozen=True)
class ServiceState:
    project: str
    module: str
    kind: str
    state: str
    source: str = SOURCE_NONE
    pid: int | None = None
    port: int | None = None
    ports: list[int] | None = None
    expected_port: int | None = None
    log_attached: bool = False
    checked_at: int = 0
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.checked_at:
            object.__setattr__(self, "checked_at", int(time.time()))

    @property
    def is_running(self) -> bool:
        return self.state in {STATE_RUNNING_MANAGED, STATE_RUNNING_EXTERNAL}

    @property
    def is_active(self) -> bool:
        return self.state in {
            STATE_STARTING,
            STATE_RUNNING_MANAGED,
            STATE_RUNNING_EXTERNAL,
            STATE_STOPPING,
        }

    @property
    def external(self) -> bool:
        return self.state == STATE_RUNNING_EXTERNAL

    @property
    def legacy_state(self) -> str:
        return "running" if self.is_running else "stopped"

    def to_running_item(self) -> dict:
        """关闭检查和 quit 审计用，只返回仍活跃的服务信息。"""
        return {
            "project": self.project,
            "module": self.module,
            "kind": self.kind,
            "state": self.state,
            "source": self.source,
            "pid": self.pid if self.pid and self.pid > 0 else None,
            "port": self.port,
            "ports": list(self.ports or []),
            "expected_port": self.expected_port,
            "log_attached": self.log_attached,
            "checked_at": self.checked_at,
            "reason": self.reason,
        }

    def to_cli_module(self) -> dict:
        """兼容旧 CLI 字段，同时暴露新的状态细节。"""
        return {
            "name": self.module,
            "state": self.legacy_state,
            "detail_state": self.state,
            "source": self.source,
            "external": self.external,
            "pid": self.pid if self.pid and self.pid > 0 else None,
            "port": self.port,
            "ports": list(self.ports or []),
            "expected_port": self.expected_port,
            "log_attached": self.log_attached,
            "checked_at": self.checked_at,
            "reason": self.reason,
        }


def running_items(states: list[ServiceState]) -> list[dict]:
    return [s.to_running_item() for s in states if s.is_active]
