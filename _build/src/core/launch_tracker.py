"""服务启动代次跟踪。

健康检查不能只在日志尾部搜索 ``Started``：日志区会保留上一次启动内容，
重启后的检查很容易被旧标记骗过。本模块给每次真实启动分配递增代次，只接收
该代次开始之后的输出，因此与 QTextDocument 的裁剪、历史日志多少都无关。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from src.core.run_logs import RunLog


def health_markers(project_type: str) -> tuple[str, ...]:
    """按项目类型返回稳定的就绪日志标记。"""
    if project_type.startswith("spring-boot") or project_type in (
        "gradle-java", "maven-java",
    ):
        return (
            "Started ", "Netty started on port", "Tomcat started on port",
            "Undertow started on port", "Jetty started on port",
        )
    if project_type in ("vue", "react", "next", "nuxt", "svelte", "node"):
        return (
            "Compiled successfully", "compiled successfully", "ready in ",
            "running here", "Local:", "VITE", "webpack compiled",
        )
    if project_type in ("fastapi", "django", "flask", "python", "python-poetry"):
        return (
            "Uvicorn running", "Application startup complete",
            "Starting development server", "Running on http",
        )
    return (
        "Started ", "ready in ", "compiled successfully",
        "Compiled successfully", "running here", "Local:",
        "Uvicorn running", "Application startup complete",
    )


@dataclass
class LaunchState:
    generation: int
    started_at: float
    runner_pid: int | None = None
    ready: bool = False
    ready_line: str = ""
    finished: bool = False
    exit_code: int | None = None
    output: RunLog = field(default_factory=RunLog)


class LaunchTracker:
    """记录每个模块（或单模块项目）最近一次启动的状态。"""

    def __init__(self, project_type: str):
        self._markers = health_markers(project_type)
        self._next_generation = 0
        self._states: dict[str, LaunchState] = {}

    @property
    def markers(self) -> tuple[str, ...]:
        return self._markers

    def begin(self, target: str) -> int:
        self._next_generation += 1
        generation = self._next_generation
        self._states[target] = LaunchState(
            generation=generation,
            started_at=time.time(),
        )
        return generation

    def started(self, target: str, generation: int, runner_pid: int | None) -> None:
        state = self._matching_state(target, generation)
        if state is not None:
            state.runner_pid = runner_pid if runner_pid and runner_pid > 0 else None

    def failed_to_start(self, target: str, generation: int) -> None:
        state = self._matching_state(target, generation)
        if state is not None:
            state.finished = True
            state.exit_code = None

    def record_output(self, target: str, line: str) -> None:
        state = self._states.get(target)
        if state is None or state.finished:
            return
        state.output.append(line)
        if state.ready:
            return
        if any(marker in line for marker in self._markers):
            state.ready = True
            state.ready_line = line

    def finished(self, target: str, exit_code: int) -> None:
        state = self._states.get(target)
        if state is not None:
            state.finished = True
            state.exit_code = exit_code

    def generation(self, target: str) -> int | None:
        state = self._states.get(target)
        return state.generation if state is not None else None

    def state(self, target: str, generation: int | None = None) -> LaunchState | None:
        state = self._states.get(target)
        if state is None:
            return None
        if generation is not None and state.generation != generation:
            return None
        return state

    def is_superseded(self, target: str, generation: int | None) -> bool:
        if generation is None:
            return False
        state = self._states.get(target)
        return state is not None and state.generation != generation

    def _matching_state(self, target: str, generation: int) -> LaunchState | None:
        state = self._states.get(target)
        if state is None or state.generation != generation:
            return None
        return state
