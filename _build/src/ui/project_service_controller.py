"""项目服务生命周期控制器。

ProjectTab 负责界面和信号连接；这里集中承接启动、停止、重启、健康探测、
编译启动等服务生命周期动作，避免 CLI 直接触碰 ProjectTab 私有状态。
"""
from __future__ import annotations

import time

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from src.core.launch_tracker import LaunchTracker
from src.core.search_worker_lifecycle import BackgroundThread, background_tasks_suspended


class _ExternalStopWorker(BackgroundThread):
    done = Signal(bool)

    def __init__(self, pid):
        super().__init__()
        self.pid = pid

    def run(self):
        from src.core.process_runner import kill_pid
        self.done.emit(kill_pid(self.pid))


class _StoppedPortsWorker(BackgroundThread):
    done = Signal(bool)

    def __init__(self, ports):
        super().__init__()
        self.ports = ports

    def run(self):
        from src.core.process_runner import is_port_listening
        stopped = True
        for port in self.ports:
            if self.isInterruptionRequested():
                return
            stopped = not is_port_listening(port) and stopped
        self.done.emit(stopped)


class _ExternalStopResult(QObject):
    def __init__(self, controller, module, pid):
        super().__init__(controller.tab)
        self.controller, self.module, self.pid = controller, module, pid

    @Slot(bool)
    def completed(self, ok):
        c, tab, module, pid = self.controller, self.controller.tab, self.module, self.pid
        if ok:
            if tab._module_external_pids.get(module) == pid:
                tab._module_external_pids.pop(module, None)
            if module == "__nginx__":
                tab._nginx_status = None
        else:
            c._external_stop_error = f"failed to stop {module} (PID {pid})"
            tab.log.append_line("stderr", f"[停止失败] {module} (PID {pid})")

    @Slot()
    def finished(self):
        self.controller._external_stops.pop(self.module, None)
        self.controller.tab._refresh_status_row()
        self.deleteLater()


class ProjectServiceController:
    def __init__(self, tab):
        self.tab = tab
        self._launches = LaunchTracker(tab.project_meta.project_type)
        # prepare_compile() 和真正 runner.start() 之间虽然都在 GUI 主线程，仍用显式
        # reservation 表达不变量，避免 processEvents 或未来重构重新引入启停竞态。
        self._compile_reserved = False
        self._operation = None
        self._operation_invoking = False
        self._external_stops = {}
        self._external_stop_error = ""

    def operation_error(self) -> dict | None:
        if self._operation is not None and not self._operation_invoking:
            return {"ok": False, "code": "service_operation_in_progress", "error": "another service operation is in progress"}
        return None

    def request(self, action: str, module=None, on_done=None, timeout_ms=None) -> dict:
        """GUI/CLI 共用异步入口；回调报告最终结果，pending 仅表示受理。"""
        error = self.operation_error() or self.validate_module(module)
        if background_tasks_suspended():
            error = {"ok": False, "error": "IDE is preparing to quit"}
        if action not in ("start", "stop", "restart"):
            error = {"ok": False, "error": "invalid service action"}
        if not error and action != "stop":
            error = self.service_start_error()
        if error:
            if on_done:
                on_done(error)
            return error
        if timeout_ms is None:
            timeout_ms = 15000 if action == "stop" else 90000
        operation = _ServiceOperation(self, action, module, on_done, timeout_ms)
        self._operation = operation
        QTimer.singleShot(0, self.tab, operation.begin)
        return {"ok": True, "pending": True}

    def request_gui(self, action: str, module=None) -> dict:
        from PySide6.QtWidgets import QMessageBox
        tab = self.tab
        if tab.project_meta.spring_boot_modules:
            tab._detect_and_apply_external()
        external = [name for name in tab._module_external_pids if not module or name == module]
        if external:
            names = "、".join(external)
            answer = QMessageBox.question(tab, "服务在外部运行",
                f"{names} 在 mini-ide 之外运行，本次操作将先停止这些进程。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return {"ok": False, "error": "cancelled"}
        def completed(result):
            if not result.get("ok") and result.get("error") not in ("already running", "not running"):
                self.tab.log.append_line("stderr", f"[服务操作失败] {result.get('error', '')}")
            self.tab._refresh_status_row()
        return self.request(action, module, completed)

    def stop_external(self, module: str, pid: int) -> bool:
        if module in self._external_stops:
            return True
        worker = _ExternalStopWorker(pid)
        self._external_stops[module] = worker
        receiver = _ExternalStopResult(self, module, pid)
        # 接收对象是 Tab，销毁后自动断连；worker 仍由应用持有。
        worker.done.connect(receiver.completed)
        worker.finished.connect(receiver.finished)
        worker.finished.connect(worker.deleteLater)
        worker.start()
        return True

    def validate_module(self, module: str | None) -> dict | None:
        if module:
            names = self.module_names()
            aliases = names + (
                [self.tab.project_meta.name] if not self.tab._is_multi_module else []
            )
            if module not in aliases:
                return {"ok": False, "error": f"module not found: {module}"}
        return None

    def module_names(self) -> list[str]:
        units = getattr(self.tab.project_meta, "runtime_units", ())
        if units:
            return [unit.id for unit in units]
        return [item[0] for item in getattr(
            self.tab.project_meta, "spring_boot_modules", (),
        )]

    def log_widget(self, module: str | None):
        if self.tab._is_multi_module and module:
            return self.tab.module_logs.get(module)
        return self.tab.log

    def states(self, refresh_external: bool = True):
        return self.tab.service_states(refresh_external=refresh_external)

    def running_items(self, refresh_external: bool = True) -> list[dict]:
        return self.tab.running_service_items(refresh_external=refresh_external)

    def stop_all(self, include_external: bool = True, silent: bool = True) -> int:
        return self.tab.stop_all_services(include_external=include_external, silent=silent)

    def wait_stopped(self, timeout_ms: int = 12000) -> bool:
        return self.tab.wait_services_stopped(timeout_ms=timeout_ms)

    # ---- 项目级构建 / 服务互斥 ----

    def running_service_names_for_build(self) -> list[str]:
        """返回会与构建目录冲突的本项目服务。

        多模块项目的编译 runner 与模块 runner 是两套对象，必须在这里统一判断；
        否则 Gradle 编译覆盖 build/classes 时，运行中的 JVM 仍可能懒加载到半套 class。
        """
        from src.core.process_runner import is_port_listening

        tab = self.tab
        running: list[str] = []
        if tab._is_multi_module:
            for name, runner in tab.module_runners.items():
                if self.runner_busy(runner):
                    running.append(name)
            if tab.project_meta.spring_boot_modules:
                try:
                    tab._detect_and_apply_external()
                except Exception:
                    pass
            running.extend(tab._module_external_pids.keys())
            # 外部感知依赖 3 秒端口快照；编译门闩不能容忍这段缓存空窗，
            # 再对每个已知端口做一次同步直连确认。
            for name in self.module_names():
                port = self.module_port(name)
                if port and is_port_listening(port):
                    running.append(name)
            # GUI 仍可能用项目级 runner 启动主 profile，也要算服务。
            current = getattr(tab, "_current_profile", None)
            if self.runner_busy(tab.runner) and current and current.kind == "run":
                running.append(tab.project_meta.name)
        else:
            current = getattr(tab, "_current_profile", None)
            if self.runner_busy(tab.runner) and current and current.kind == "run":
                running.append(tab.project_meta.name)
            port = tab.project_meta.default_port
            if port and is_port_listening(port):
                running.append(tab.project_meta.name)
        return list(dict.fromkeys(sorted(running)))

    def build_start_error(self) -> dict | None:
        error = self.operation_error()
        if error or self._external_stops:
            return error or {"ok": False, "error": "service stop is still in progress"}
        running = self.running_service_names_for_build()
        if running:
            return {
                "ok": False,
                "code": "services_running",
                "error": "services are running; stop them before compile",
                "running_modules": running,
            }
        if self._compile_reserved or self.runner_busy(self.tab.runner):
            return {
                "ok": False,
                "code": "project_task_running",
                "error": "another project task is running",
            }
        return None

    def service_start_error(self) -> dict | None:
        error = self.operation_error()
        if error:
            return error
        if getattr(self.tab, "_runtime_start_pending", False) and not self._operation_invoking:
            return {"ok": False, "error": "runtime dependency startup is in progress"}
        if self.tab.project_meta.project_type == "nginx" and self.runner_busy(self.tab.runner):
            return {"ok": False, "error": "nginx command is still in progress"}
        if self._external_stops or any(self.runner_busy(r) for r in getattr(self.tab, "_runtime_stop_runners", {}).values()):
            return {"ok": False, "error": "service stop is still in progress"}
        tab = self.tab
        if self._compile_reserved:
            return {
                "ok": False,
                "code": "build_in_progress",
                "error": "project build is being prepared; start/restart is blocked",
            }
        if self.runner_busy(tab.runner):
            current = getattr(tab, "_current_profile", None)
            # 单模块项目的 runner 本身就是当前服务；start/restart 的原有逻辑会处理
            # already running / stop→start。其它情况都是项目级构建任务冲突。
            if tab._is_multi_module or current is None or current.kind != "run":
                return {
                    "ok": False,
                    "code": "build_in_progress",
                    "error": "project build/task is running; start/restart is blocked",
                }
        return None

    def profile_start_error(self, profile) -> dict | None:
        if profile.kind == "run":
            return self.service_start_error()
        # compile/build/clean/test 都可能写入或删除构建输出；统一使用同一门闩。
        if profile.kind in ("compile", "build", "clean", "test"):
            return self.build_start_error()
        if self.runner_busy(self.tab.runner):
            return {
                "ok": False,
                "code": "project_task_running",
                "error": "another project task is running",
            }
        return None

    # ---- 本轮启动代次 ----

    def launch_target(self, module: str | None) -> str:
        if module:
            return module
        units = getattr(self.tab.project_meta, "runtime_units", ())
        if not self.tab._is_multi_module and units:
            return units[0].id
        return self.tab.project_meta.name

    def begin_launch(self, module: str | None) -> int:
        return self._launches.begin(self.launch_target(module))

    def launch_started(self, module: str | None, generation: int, runner) -> None:
        pid_getter = getattr(runner, "process_id", None)
        pid = pid_getter() if callable(pid_getter) else None
        self._launches.started(self.launch_target(module), generation, pid)
        if hasattr(runner, "started") and runner.state() == "starting":
            previous = getattr(runner, "_mini_ide_pid_hook", None)
            if previous is not None:
                runner.started.disconnect(previous)
            target = self.launch_target(module)
            def update_pid():
                self._launches.started(target, generation, runner.process_id())
                runner.started.disconnect(update_pid)
                runner._mini_ide_pid_hook = None
            runner._mini_ide_pid_hook = update_pid
            runner.started.connect(update_pid)

    def launch_failed(self, module: str | None, generation: int) -> None:
        self._launches.failed_to_start(self.launch_target(module), generation)

    def launch_finished(self, module: str | None, exit_code: int) -> None:
        self._launches.finished(self.launch_target(module), exit_code)

    def record_service_output(self, module: str | None, line: str) -> None:
        self._launches.record_output(self.launch_target(module), line)

    def snapshot_launch_generations(self, module: str | None) -> dict[str, int | None]:
        return {
            target: self._launches.generation(target)
            for target in self.target_modules(module)
        }

    def start(self, module: str | None = None) -> dict:
        tab = self.tab
        if tab.project_meta.project_type == "nginx":
            if module and module != tab.project_meta.name:
                return {"ok": False, "error": f"module not found: {module}"}
            if tab._detect_nginx_status().running:
                return {"ok": False, "error": "already running"}
            return {"ok": bool(tab._start_nginx())}

        invalid = self.validate_module(module)
        if invalid:
            return invalid

        conflict = self.service_start_error()
        if conflict:
            return conflict

        if tab._is_multi_module:
            if module:
                invalid = self.validate_module(module)
                if invalid:
                    return invalid
                runner = tab.module_runners.get(module)
                if runner and self.runner_busy(runner):
                    return {"ok": False, "error": "already running"}
                if tab.project_meta.spring_boot_modules:
                    tab._detect_and_apply_external()
                ok = bool(tab._start_module(module, silent=True))
                return {"ok": True} if ok else {"ok": False, "error": "failed to start module"}
            ok = bool(tab._start_all_modules(silent=True))
            return {"ok": True} if ok else {"ok": False, "error": "failed to start one or more modules"}

        if self.runner_busy(tab.runner):
            return {"ok": False, "error": "already running"}
        primary = next((p for p in tab.project_meta.profiles if p.primary), None)
        if not primary:
            return {"ok": False, "error": "no run profile found"}
        ok = bool(tab._start_profile(primary))
        return {"ok": True} if ok else {"ok": False, "error": "failed to start project"}

    def stop(self, module: str | None = None) -> dict:
        return self.request("stop", module)

    def restart(self, module: str | None = None) -> dict:
        return self.request("restart", module)

    def runner_busy(self, runner) -> bool:
        if runner.state() != "idle":
            return True
        cleanup_pending = getattr(runner, "stop_cleanup_pending", None)
        return bool(cleanup_pending and cleanup_pending())

    def module_port(self, module: str) -> int | None:
        if module in self.tab._module_ports:
            return self.tab._module_ports.get(module)
        resolver = getattr(self.tab.project_meta, "runtime_unit", None)
        unit = resolver(module) if callable(resolver) else None
        if unit is not None:
            return unit.expected_port
        for name, _path, port, _main_class in getattr(
            self.tab.project_meta, "spring_boot_modules", (),
        ):
            if name == module:
                return port
        return None

    def target_modules(self, module: str | None) -> list[str]:
        if self.tab._is_multi_module:
            names = self.module_names()
            return [module] if module else names
        units = getattr(self.tab.project_meta, "runtime_units", ())
        unit = units[0] if units else None
        return [unit.id if unit is not None else self.tab.project_meta.name]

    def active_modules(self, module: str | None) -> list[str]:
        active = []
        for state in self.states(refresh_external=True):
            if state.kind == "script":
                continue
            if module and state.module != module:
                continue
            if state.is_running:
                active.append(state.module)
        return active

    def module_health_ok(
        self,
        module: str,
        generation: int | None = None,
    ) -> tuple[bool, dict]:
        tab = self.tab
        if tab.project_meta.project_type == "nginx":
            status = tab._detect_nginx_status()
            if status.running and status.ports:
                return True, {
                    "module": module,
                    "source": "nginx",
                    "pid": status.master_pid,
                    "ports": status.ports,
                }
            return False, {"module": module, "source": "pending"}

        if self._launches.is_superseded(module, generation):
            return False, {
                "module": module,
                "source": "superseded",
                "error": "launch was superseded by a newer start/restart",
            }

        runner = tab.module_runners.get(module)
        launch = self._launches.state(module, generation)
        if runner and runner.is_running():
            if launch and launch.ready:
                return True, {
                    "module": module,
                    "source": "log",
                    "generation": launch.generation,
                    "pid": launch.runner_pid,
                }
            port = self.module_port(module)
            holder_pid = self._module_port_holder_since(module, port, launch)
            if holder_pid is not None:
                return True, {
                    "module": module,
                    "source": "port",
                    "port": port,
                    "pid": holder_pid,
                    "generation": launch.generation if launch else None,
                }
            return False, {
                "module": module,
                "source": "pending",
                "generation": launch.generation if launch else None,
            }

        # 一旦本轮启动有明确代次，就绝不能退回旧端口/旧外部进程作为成功依据。
        if launch is not None:
            return False, {
                "module": module,
                "source": "process_exited",
                "error": "process exited before becoming ready",
                "exit_code": launch.exit_code,
                "generation": launch.generation,
            }

        try:
            external = tab._detect_external_modules()
        except Exception:
            external = {}
        if module in external:
            pid, port = external[module]
            return True, {
                "module": module,
                "source": "external",
                "pid": pid,
                "port": port,
            }
        return False, {"module": module, "source": "pending"}

    def single_project_health_ok(
        self,
        generation: int | None = None,
    ) -> tuple[bool, dict]:
        from src.core.process_runner import find_port_holder

        tab = self.tab
        if tab.project_meta.project_type == "nginx":
            status = tab._detect_nginx_status()
            if status.running and status.ports:
                return True, {"source": "nginx", "pid": status.master_pid, "ports": status.ports}
            return False, {"source": "pending"}

        target = self.launch_target(None)
        if self._launches.is_superseded(target, generation):
            return False, {
                "source": "superseded",
                "error": "launch was superseded by a newer start/restart",
            }
        launch = self._launches.state(target, generation)
        if tab.runner.is_running():
            if launch and launch.ready:
                return True, {
                    "source": "log",
                    "generation": launch.generation,
                    "pid": launch.runner_pid,
                }
            default_port = tab.project_meta.default_port
            if default_port:
                holders = find_port_holder(default_port)
                holder_pid = self._new_holder_pid(holders, launch)
                if holder_pid is not None:
                    return True, {
                        "source": "port",
                        "port": default_port,
                        "pid": holder_pid,
                        "generation": launch.generation if launch else None,
                    }
            return False, {
                "source": "pending",
                "generation": launch.generation if launch else None,
            }

        if launch is not None:
            return False, {
                "source": "process_exited",
                "error": "process exited before becoming ready",
                "exit_code": launch.exit_code,
                "generation": launch.generation,
            }
        default_port = tab.project_meta.default_port
        holders = find_port_holder(default_port) if default_port else []
        if holders:
            return True, {
                "source": "external",
                "port": default_port,
                "pid": holders[0].get("pid"),
            }
        return False, {"source": "pending"}

    def _module_port_holder_since(self, module: str, port: int | None, launch) -> int | None:
        if not port:
            return None
        from src.core.process_runner import find_port_holder

        # 运行单元已经声明了端口，不应再经过 Spring 专用的命令行匹配器。
        # 本轮启动代次仍由 _new_holder_pid 校验，旧监听进程不能冒充启动成功。
        return self._new_holder_pid(find_port_holder(port), launch)

    @staticmethod
    def _new_holder_pid(holders: list[dict], launch) -> int | None:
        """只接受本轮启动之后创建的监听进程，避免旧端口冒充新服务。"""
        if launch is None:
            return holders[0].get("pid") if holders else None
        import psutil

        for holder in holders:
            pid = holder.get("pid")
            if not pid:
                continue
            try:
                process = psutil.Process(pid)
                if launch.runner_pid and (
                    pid == launch.runner_pid
                    or any(parent.pid == launch.runner_pid for parent in process.parents())
                ):
                    return int(pid)
                # Gradle daemon 可能早于 QProcess 存在，应用 JVM 不一定是 wrapper
                # 的后代；这类情况再用本轮开始后的创建时间确认。Windows 时间戳
                # 留 0.5 秒容差，既覆盖时钟精度，又不会接受真正的旧服务。
                if process.create_time() >= launch.started_at - 0.5:
                    return int(pid)
            except (psutil.Error, OSError, ValueError, TypeError):
                continue
        return None

    def prepare_compile(self) -> dict:
        """校验并预留一次编译；调用方须先挂 finished，再 start_prepared_compile。"""
        tab = self.tab
        compile_prof = tab._find_compile_profile()
        if not compile_prof:
            return {"ok": False, "error": "no compile profile found"}
        conflict = self.build_start_error()
        if conflict:
            return conflict
        self._compile_reserved = True
        return {
            "ok": True,
            "runner": tab.runner,
            "profile": compile_prof,
        }

    def prepare_action(self, action: str) -> dict:
        from src.core.project_inspection import ACTION_KINDS
        profile = next((item for item in self.tab.project_meta.profiles if item.name == action), None)
        if profile is None:
            return {"ok": False, "code": "action_not_found", "error": f"configured action not found: {action}"}
        if profile.kind not in ACTION_KINDS:
            return {"ok": False, "code": "unsupported_action", "error": "service profiles use start/ensure-running; run-action only accepts build/test/install/clean"}
        conflict = self.build_start_error()
        if conflict:
            return conflict
        self._compile_reserved = True
        return {"ok": True, "runner": self.tab.runner, "profile": profile}

    def start_prepared_compile(self, prepared: dict) -> dict:
        if not self._compile_reserved:
            return {"ok": False, "error": "compile reservation is no longer valid"}
        profile = prepared.get("profile")
        try:
            ok = bool(self.tab._start_profile(profile, enforce_guard=False))
        finally:
            self._compile_reserved = False
        if not ok:
            return {"ok": False, "error": "failed to start compile process"}
        return {"ok": True}

    def abort_prepared_compile(self) -> None:
        self._compile_reserved = False

    def begin_compile(self) -> dict:
        """兼容内部旧调用；CLI 应使用 prepare→挂信号→start，避免丢 finished。"""
        prepared = self.prepare_compile()
        if not prepared.get("ok"):
            return prepared
        started = self.start_prepared_compile(prepared)
        if not started.get("ok"):
            return started
        return {"ok": True, "runner": prepared["runner"]}


class _ServiceOperation(QObject):
    def __init__(self, controller, action, module, on_done, timeout_ms):
        super().__init__(controller.tab)
        self.controller, self.action, self.module = controller, action, module
        self.on_done = on_done
        self.deadline = time.monotonic() + max(0, timeout_ms) / 1000
        self.stop_deadline = min(self.deadline, time.monotonic() + 15)
        self.phase = "stop"
        self.ports = []
        self.probe = None
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.poll)
        self.result_sent = False
        self.stop_targets = None
        controller.tab.destroyed.connect(self.tab_destroyed)

    def tab_destroyed(self):
        if not self.result_sent:
            self.result_sent = True
            if self.on_done:
                self.on_done({"ok": False, "error": "project was closed during service operation"})
        if self.probe is not None:
            self.probe.requestInterruption()

    def begin(self):
        c, tab = self.controller, self.controller.tab
        if background_tasks_suspended():
            self.finish({"ok": False, "error": "IDE is preparing to quit"})
            return
        c._external_stop_error = ""
        if tab._is_multi_module and tab.project_meta.spring_boot_modules:
            tab._detect_and_apply_external()
        if tab._is_multi_module:
            names = [self.module] if self.module else c.module_names()
            self.ports = [c.module_port(name) for name in names if c.module_port(name)]
        else:
            unit = tab.project_meta.runtime_units[0] if tab.project_meta.runtime_units else None
            port = unit.expected_port if unit else tab.project_meta.default_port
            self.ports = [port] if port else []
        nginx = tab.project_meta.project_type == "nginx"
        status = tab._detect_nginx_status() if nginx else None
        if status and status.running:
            self.ports = list(status.ports)
        takeover = self.action == "start" and bool(tab._module_external_pids) and (not self.module or self.module in tab._module_external_pids)
        if takeover:
            self.stop_targets = [name for name in tab._module_external_pids if not self.module or name == self.module]
            self.ports = [c.module_port(name) for name in self.stop_targets if c.module_port(name)]
        if self.action == "start" and not takeover:
            self.launch()
            return
        active = bool(c.active_modules(self.module)) or bool(status and status.running)
        if self.action == "stop" and not active and not self.ports and not (tab._is_multi_module and not self.module):
            self.finish({"ok": False, "error": "not running"})
            return
        c._operation_invoking = True
        try:
            if takeover:
                for name in self.stop_targets:
                    if not tab._stop_module(name, silent=True):
                        self.finish({"ok": False, "error": f"failed to stop external module: {name}"})
                        return
            elif nginx:
                if status and status.running:
                    tab._stop_nginx_external()
                if c.runner_busy(tab.runner):
                    tab.runner.stop()
            elif tab._is_multi_module:
                if self.module:
                    runner = tab.module_runners.get(self.module)
                    if active or self.ports or (runner and c.runner_busy(runner)):
                        if not tab._stop_module(self.module, silent=True):
                            self.finish({"ok": False, "error": "failed to stop module"})
                            return
                else:
                    tab._stop_all_modules(silent=True)
                    if c.runner_busy(tab.runner):
                        tab._stop()
            elif active or self.ports or c.runner_busy(tab.runner):
                if not tab._stop():
                    self.finish({"ok": False, "error": "failed to stop project"})
                    return
        except Exception as exc:
            self.finish({"ok": False, "error": str(exc)})
            return
        finally:
            c._operation_invoking = False
        self.timer.start()
        self.poll()

    def runners(self):
        tab = self.controller.tab
        if tab._is_multi_module:
            names = self.stop_targets if self.phase == "stop" and self.stop_targets is not None else ([self.module] if self.module else self.controller.module_names())
            return [tab.module_runners[name] for name in names if name in tab.module_runners]
        return [tab.runner]

    def poll(self):
        if self.result_sent:
            return
        c, tab = self.controller, self.controller.tab
        deadline = self.stop_deadline if self.phase == "stop" else self.deadline
        if time.monotonic() >= deadline:
            self.finish({"ok": False, "error": f"{self.phase} timeout"})
            return
        if self.phase == "start":
            if getattr(tab, "_runtime_start_pending", False):
                return
            if getattr(tab, "_runtime_start_error", ""):
                self.finish({"ok": False, "error": tab._runtime_start_error})
                return
            if any(r.state() == "starting" for r in self.runners()):
                return
            errors = [getattr(r, "_last_start_error", "") for r in self.runners()]
            error = next((error for error in errors if error), None)
            self.finish({"ok": False, "error": error} if error else {"ok": True})
            return
        if c._external_stop_error:
            self.finish({"ok": False, "error": c._external_stop_error})
            return
        if any(c.runner_busy(r) for r in self.runners()) or c._external_stops:
            return
        if any(c.runner_busy(r) for r in tab._runtime_stop_runners.values()):
            return
        if self.probe is None:
            worker = _StoppedPortsWorker(self.ports)
            self.probe = worker
            worker.done.connect(self.ports_checked)
            worker.finished.connect(self.probe_finished)
            worker.finished.connect(worker.deleteLater)
            worker.start()

    def probe_finished(self):
        self.probe = None

    def ports_checked(self, stopped):
        if self.result_sent or not stopped:
            return
        if self.action == "stop":
            self.finish({"ok": True})
        else:
            self.launch()

    def launch(self):
        c = self.controller
        self.phase = "start"
        if self.module:
            c.tab._runtime_start_error = ""
        c._operation_invoking = True
        try:
            result = c.start(self.module)
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        finally:
            c._operation_invoking = False
        if not result.get("ok"):
            self.finish(result)
            return
        self.timer.start()
        self.poll()

    def finish(self, result):
        if self.result_sent:
            return
        self.result_sent = True
        if not result.get("ok") and getattr(self.controller.tab, "_runtime_start_pending", False):
            self.controller.tab._runtime_start_generation += 1
            self.controller.tab._runtime_start_pending = False
        self.timer.stop()
        if self.probe is not None:
            self.probe.requestInterruption()
        if self.controller._operation is self:
            self.controller._operation = None
        if self.on_done:
            self.on_done(result)
        self.deleteLater()
