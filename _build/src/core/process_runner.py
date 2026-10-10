"""进程管理

用 QProcess 启动进程，psutil 递归杀子进程（Gradle/Maven 启动的 JVM 进程必须这么杀）。
实时发 outputLine 信号，UI 订阅后写入日志区。
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import socket
import sys
import threading
import time
from dataclasses import dataclass, field

import psutil
from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from src.util import app_log
from src.core.output_queue import OutputQueue

log = app_log.get_logger("runner")


STREAM_STDOUT = "stdout"
STREAM_STDERR = "stderr"
_LINE_BREAK = re.compile(rb"\r\n|\r|\n")


@dataclass
class RunContext:
    """一次运行的上下文"""
    cwd: str
    env: dict[str, str] = field(default_factory=dict)
    jvm_opts: str = ""
    spring_profile: str = ""
    extra_args: list[str] = field(default_factory=list)


class ProcessRunner(QObject):
    """单个子进程的生命周期管理。一个 ProjectTab 内串行复用一个 Runner。"""

    outputLine = Signal(str, str)        # (stream, line)
    started = Signal()
    finished = Signal(int)               # exit_code（-1 表示手动杀掉）
    stateChanged = Signal(str)           # idle | starting | running | stopping

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc: QProcess | None = None
        self._state = "idle"
        self._started_at: float = 0.0
        self._manually_stopped = False
        self._stdout_buf = b""
        self._stderr_buf = b""
        self._stop_thread: threading.Thread | None = None
        self._output_queue = OutputQueue()
        self.destroyed.connect(self._output_queue.close)
        self._output_timer = QTimer(self)
        self._output_timer.setInterval(1)
        self._output_timer.timeout.connect(self._emit_output_batch)
        self._pending_finish: int | None = None
        self._process_exit = None

    # ---- public API ----

    def is_running(self) -> bool:
        return self._state == "running"

    def state(self) -> str:
        return self._state

    def process_id(self) -> int | None:
        """返回本轮 QProcess 的根 PID；未启动或已退出时返回 None。"""
        if self._proc is None:
            return None
        try:
            pid = int(self._proc.processId())
        except (RuntimeError, TypeError, ValueError):
            return None
        return pid if pid > 0 else None

    def stop_cleanup_pending(self) -> bool:
        """后台进程树清理线程是否仍在收尾。"""
        t = self._stop_thread
        return bool(t and t.is_alive())

    def elapsed_seconds(self) -> float:
        if not self._started_at:
            return 0.0
        return time.time() - self._started_at

    def start(self, command: list[str], ctx: RunContext) -> bool:
        """受理启动；实际启动/失败由 started/finished 信号报告。"""
        # stopping 时 is_running() 已是 False，但旧进程树的后台清理还没结束。
        # 此时复用 runner 会覆盖 self._proc，并让旧、新进程的 finished 信号串台。
        # 只有完全回到 idle 且清理线程结束，才允许下一轮启动。
        if self._state != "idle" or self.stop_cleanup_pending() or self._output_queue:
            return False

        env = _build_child_env(ctx)

        # 立即占住 runner；启动完成由 QProcess.started 接续，不等待外部进程。
        self._set_state("starting")

        full_cmd = list(command) + list(ctx.extra_args or [])
        display = " ".join(shlex.quote(c) if " " in c else c for c in full_cmd)
        self.outputLine.emit("meta", f"$ {display}")
        self.outputLine.emit("meta", f"  cwd: {ctx.cwd}")
        if ctx.spring_profile:
            self.outputLine.emit("meta", f"  SPRING_PROFILES_ACTIVE={ctx.spring_profile}")
        if ctx.jvm_opts:
            self.outputLine.emit("meta", f"  JAVA_TOOL_OPTIONS={ctx.jvm_opts}")

        self._proc = QProcess(self)
        self._proc.setWorkingDirectory(ctx.cwd)
        self._proc.setProcessEnvironment(_env_to_qt(env))
        self._proc.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        _suppress_console(self._proc)
        self._proc.readyReadStandardOutput.connect(self._on_stdout)
        self._proc.readyReadStandardError.connect(self._on_stderr)
        self._proc.finished.connect(self._on_finished)
        self._proc.errorOccurred.connect(self._on_error)
        self._proc.started.connect(self._on_started)

        self._stdout_buf = b""
        self._stderr_buf = b""
        self._manually_stopped = False
        self._last_start_error = ""
        self._process_exit = None

        # Windows 下 .bat/.cmd 脚本必须通过 cmd /c 启动
        # 把已经清洗过的 env 传进去，用干净的 PATH 解析命令名，避免吃到 mini-ide 自己的 venv
        program, args = _split_program(full_cmd, env=env)
        log.info("启动进程: %s %s | cwd=%s | profile=%s | jvm=%s",
                 program, " ".join(args), ctx.cwd, ctx.spring_profile, ctx.jvm_opts)
        self._proc.start(program, args)
        self._started_at = time.time()
        return True

    def _on_started(self) -> None:
        if self._state == "stopping":
            self._proc.kill()
            return
        self._set_state("running")
        self.started.emit()

    def stop(self, timeout_ms: int = 5000) -> None:
        """优雅停止：先 SIGTERM 整棵进程树，超时则 SIGKILL。

        杀进程树（psutil terminate + wait_procs 最长 timeout_ms）放到后台线程跑，
        本方法立即返回——否则进程顽固不退时主线程会冻结到 5 秒。状态先置 stopping，
        is_running() 立刻转 False、UI 即时响应；进程真正退出后 QProcess 的 finished
        信号仍在主线程触发 _on_finished 收回 idle；controller 定时检查
        idle、后台清理和端口释放后，才确认停止或接续重启。
        """
        if not self._proc or self._state not in ("starting", "running"):
            return
        self._set_state("stopping")
        self._manually_stopped = True
        pid = int(self._proc.processId())
        if pid > 0:
            graceful = timeout_ms / 1000
            t = threading.Thread(
                target=_kill_tree, args=(pid, graceful),
                name=f"kill-tree-{pid}", daemon=True,
            )
            self._stop_thread = t
            t.start()
        else:
            self._proc.kill()

    # ---- internal ----

    def _set_state(self, s: str) -> None:
        self._state = s
        self.stateChanged.emit(s)

    def _drain(self, buf_attr: str, new_data: bytes, stream: str, *, final=False) -> None:
        """线性拆行；CRLF 跨读取批次时保留末尾 CR，输出由 timer 分批发送。"""
        buf = getattr(self, buf_attr) + new_data
        start = 0
        for match in _LINE_BREAK.finditer(buf):
            if not final and match.start() == len(buf) - 1 and buf.endswith(b"\r"):
                break
            self._output_queue.append((stream, buf[start:match.start()]))
            start = match.end()
        setattr(self, buf_attr, buf[start:])
        if self._output_queue and not self._output_timer.isActive():
            self._output_timer.start()

    def _emit_output_batch(self) -> None:
        stdout_pending = self._read_output(STREAM_STDOUT)
        stderr_pending = self._read_output(STREAM_STDERR)
        more_input = stdout_pending or stderr_pending
        if self._process_exit is not None and not more_input:
            code, elapsed, tag = self._process_exit
            self._process_exit = None
            self._flush_remaining()
            self._output_queue.append(("meta", f"[进程结束] {tag}  耗时 {elapsed:.1f}s".encode("utf-8")))
            self._pending_finish = code
        deadline = time.perf_counter() + 0.008
        for _ in range(500):
            if not self._output_queue:
                break
            stream, raw = self._output_queue.popleft()
            self.outputLine.emit(stream, raw.decode("utf-8", errors="replace"))
            if time.perf_counter() >= deadline:
                break
        if self._output_queue or more_input:
            return
        self._output_timer.stop()
        if self._pending_finish is not None:
            code = self._pending_finish
            self._pending_finish = None
            self._started_at = 0.0
            proc, self._proc = self._proc, None
            if proc is not None:
                proc.deleteLater()
            self._set_state("idle")
            self.finished.emit(code)

    def _flush_remaining(self) -> None:
        for attr, stream in (("_stdout_buf", STREAM_STDOUT), ("_stderr_buf", STREAM_STDERR)):
            self._drain(attr, b"", stream, final=True)
            remaining = getattr(self, attr)
            if remaining:
                self._output_queue.append((stream, remaining))
                setattr(self, attr, b"")
        if self._output_queue:
            self._output_timer.start()

    def _on_stdout(self) -> None:
        self._read_output(STREAM_STDOUT)

    def _on_stderr(self) -> None:
        self._read_output(STREAM_STDERR)

    def _read_output(self, stream: str) -> bool:
        if self._proc:
            channel = (QProcess.ProcessChannel.StandardOutput if stream == STREAM_STDOUT
                       else QProcess.ProcessChannel.StandardError)
            self._proc.setReadChannel(channel)
            data = bytes(self._proc.read(64 * 1024))
            if data:
                self._drain("_stdout_buf" if stream == STREAM_STDOUT else "_stderr_buf", data, stream)
            more = self._proc.bytesAvailable() > 0
            if more and not self._output_timer.isActive():
                self._output_timer.start()
            return more
        return False

    def _on_finished(self, exit_code: int, _status) -> None:
        code = -1 if self._manually_stopped else int(exit_code)
        elapsed = self.elapsed_seconds()
        tag = "已手动停止" if self._manually_stopped else (f"退出码 {exit_code}")
        log.info("进程结束: exit=%s manual=%s elapsed=%.1fs",
                 exit_code, self._manually_stopped, elapsed)
        self._process_exit = (code, elapsed, tag)
        self._output_timer.start()

    def _on_error(self, err) -> None:
        log.error("进程错误: %s", err)
        self.outputLine.emit(STREAM_STDERR, f"[进程错误] {err}")
        if err == QProcess.ProcessError.FailedToStart:
            self._last_start_error = self._proc.errorString() if self._proc else str(err)
            self._pending_finish = -2
            self._output_timer.start()


# ---------- helpers ----------

def _env_to_qt(env: dict[str, str]):
    from PySide6.QtCore import QProcessEnvironment
    qenv = QProcessEnvironment()
    for k, v in env.items():
        qenv.insert(k, v)
    return qenv


def _suppress_console(proc: QProcess) -> None:
    """Windows 下阻止 cmd.exe 弹出控制台窗口

    QProcess 默认会继承父进程的 console 设置，GUI 程序（pythonw/打包后的 exe）
    启动 cmd.exe 时会弹一个黑框。用 CREATE_NO_WINDOW 标志压掉。
    """
    if sys.platform != "win32":
        return
    try:
        CREATE_NO_WINDOW = 0x08000000

        def _modifier(args):
            args.flags |= CREATE_NO_WINDOW

        proc.setCreateProcessArgumentsModifier(_modifier)
    except AttributeError:
        # 老版 PySide6 没有这个 API，回退：什么都不做
        pass


def _split_program(cmd: list[str], env: dict[str, str] | None = None) -> tuple[str, list[str]]:
    """Windows 下启动策略

    - `.bat` / `.cmd` → cmd.exe /c
    - `python` / `pip` / `node` 这类命令名 **永远不做预解析**，直接交给 cmd.exe /c，
      让子进程用它自己（已经清掉 mini-ide venv）的 PATH 去找正确的 exe。
      否则 shutil.which 会用 mini-ide 的 PATH 找到 mini-ide 自己的 python.exe。
    - 其他命令名（npm/yarn/pnpm/poetry 等）用传入的 env['PATH'] 解析，
      找到真文件后再分 .cmd→cmd.exe /c 还是直接跑 .exe。
    - 绝对路径 + .exe 直接跑。
    """
    if not cmd:
        return "", []
    program = cmd[0]
    args = cmd[1:]
    if sys.platform != "win32":
        return program, args

    lower = program.lower()
    if lower.endswith((".bat", ".cmd")):
        return "cmd.exe", ["/c", program, *args]
    if lower.endswith(".exe") or os.path.isabs(program):
        return program, args

    # 这些命令必须用子进程干净 PATH 去找，不能让父进程 venv 污染
    _DEFER_TO_SHELL = {"python", "python3", "python.exe", "python3.exe",
                       "pip", "pip3", "pip.exe", "pip3.exe"}
    if lower in _DEFER_TO_SHELL:
        return "cmd.exe", ["/c", program, *args]

    # 其他命令名：用 child env 的 PATH 解析
    search_path = env.get("PATH") if env else None
    resolved = shutil.which(program, path=search_path)
    if resolved:
        if resolved.lower().endswith((".bat", ".cmd")):
            return "cmd.exe", ["/c", resolved, *args]
        return resolved, args

    # 兜底：交给 cmd.exe /c
    return "cmd.exe", ["/c", program, *args]


def _build_child_env(ctx: "RunContext") -> dict[str, str]:
    """构建干净的子进程环境

    **关键**：把 mini-ide 自己的 venv 从环境里摘掉，否则 poetry/python 会误用
    mini-ide 的 venv 而不是项目自己的 venv。
    """
    env = os.environ.copy()

    # 1. 摘除 mini-ide 自己的 venv 痕迹
    self_venv = os.environ.get("VIRTUAL_ENV", "")
    if self_venv and _is_mini_ide_venv(self_venv):
        env.pop("VIRTUAL_ENV", None)
        # 从 PATH 里删掉 <venv>/Scripts 或 <venv>/bin
        bad_parts = {os.path.join(self_venv, "Scripts"), os.path.join(self_venv, "bin")}
        path = env.get("PATH", "")
        sep = os.pathsep
        kept = [p for p in path.split(sep) if p and os.path.normpath(p) not in
                {os.path.normpath(b) for b in bad_parts}]
        env["PATH"] = sep.join(kept)
        # POETRY_ACTIVE 也得清，否则 poetry 以为当前已经在 venv 内
        env.pop("POETRY_ACTIVE", None)
        # 其他工具用的激活标记
        env.pop("PYTHONHOME", None)

    # 2. 读取项目根目录的 .env 文件（KEY=VALUE 格式，# 注释，不展开变量引用）
    _load_dotenv(ctx.cwd, env)

    # 3. 用户自定义 env 覆盖（优先级高于 .env）
    env.update(ctx.env or {})

    # 3. 强制子进程用 UTF-8 输出
    # 背景：Windows 上 Java/Python 子进程的 stdout/stderr 默认用系统活动代码页
    # （中文系统 = GBK / CP936），mini-ide 按 UTF-8 解码遇到 GBK 字节就替换成 ?，
    # 典型表现：IOException 本地化消息里的中文句号"。"变 "?"。
    #
    # Java：
    #   -Dfile.encoding=UTF-8     所有 Java 版本都认；Java 18+ 默认本就是 UTF-8 (JEP 400)
    #   -Dstdout.encoding=UTF-8   Java 19+ 精准生效（不动 file.encoding）
    #   -Dstderr.encoding=UTF-8   同上
    # 副作用：JVM 启动时会打一行 "Picked up JAVA_TOOL_OPTIONS: ..."，可接受
    java_utf8 = "-Dfile.encoding=UTF-8 -Dstdout.encoding=UTF-8 -Dstderr.encoding=UTF-8"
    existing_jto = env.get("JAVA_TOOL_OPTIONS", "")
    parts = [java_utf8]
    if existing_jto:
        parts.append(existing_jto)
    if ctx.jvm_opts:
        parts.append(ctx.jvm_opts)
    env["JAVA_TOOL_OPTIONS"] = " ".join(parts)

    if ctx.spring_profile:
        env.setdefault("SPRING_PROFILES_ACTIVE", ctx.spring_profile)

    # 4. 让输出实时、不带终端颜色控制符
    env.setdefault("PYTHONUNBUFFERED", "1")
    # Python 子进程：强制 stdout/stderr 用 UTF-8（同上，避免 GBK 默认）
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("FORCE_COLOR", "0")
    env.setdefault("NO_COLOR", "1")
    env.setdefault("TERM", "dumb")

    return env


def _load_dotenv(cwd: str, env: dict[str, str]) -> None:
    """读取 cwd/.env，把 KEY=VALUE 注入到 env（已有的 key 不覆盖）。"""
    dotenv = os.path.join(cwd, ".env")
    try:
        with open(dotenv, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    env.setdefault(key, value)
    except FileNotFoundError:
        pass
    except Exception:
        pass


def _is_mini_ide_venv(venv_path: str) -> bool:
    """判断某个 VIRTUAL_ENV 是不是 mini-ide 自己的 .venv"""
    try:
        norm = os.path.normpath(os.path.abspath(venv_path)).lower()
        # 简单启发：路径里含 "mini-ide" 且以 .venv / venv 结尾
        base = os.path.basename(norm)
        if base not in (".venv", "venv"):
            return False
        parent = os.path.basename(os.path.dirname(norm))
        return parent == "mini-ide"
    except Exception:
        return False


def _kill_tree(pid: int, graceful_timeout: float = 3.0) -> None:
    """递归终结进程树：先 terminate，超时则 kill"""
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    procs = [parent, *parent.children(recursive=True)]
    for p in procs:
        try:
            p.terminate()
        except psutil.Error:
            pass
    gone, alive = psutil.wait_procs(procs, timeout=graceful_timeout)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    if alive:
        psutil.wait_procs(alive, timeout=2.0)
    try:
        invalidate_port_cache()
    except Exception:
        pass


# ---------- 端口工具 ----------

# psutil.net_connections() 在 Windows 上要遍历所有 TCP 连接，一次调用 200-800ms，
# 直接在主线程扫会卡拖拽。我们用后台线程定期更新快照，UI 永远读缓存（非阻塞）。
_PORT_SNAPSHOT_INTERVAL = 3.0
_port_snapshot: dict[int, list[dict]] = {}
_port_lock = threading.Lock()
_port_thread_started = False


def _scan_once() -> dict[int, list[dict]]:
    port_map: dict[int, list[dict]] = {}
    try:
        conns = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, psutil.Error):
        return port_map
    pid_to_ports: dict[int, set[int]] = {}
    for conn in conns:
        if not conn.laddr or not conn.pid:
            continue
        # 只收 LISTEN：那才是服务对外提供的端口。ESTABLISHED 是临时连接
        # （nacos/数据库/HTTP 客户端连出去的高位端口），把它们算进来会让服务面板
        # 把某个随机高位端口误当成模块的服务端口显示（如 gateway 真实 19000 却显示 56720）。
        if conn.status not in (psutil.CONN_LISTEN, "LISTEN"):
            continue
        pid_to_ports.setdefault(conn.pid, set()).add(conn.laddr.port)
    for pid, ports in pid_to_ports.items():
        try:
            proc = psutil.Process(pid)
            name = proc.name()
            exe = proc.exe()
            # 全量 cmdline（截断到 2000 字符）：模块路径 / classpath / jar 名通常在靠后的参数里，
            # 只取前 5 段会丢掉用于匹配模块的关键信息（外部启动感知依赖它）。
            cmdline = " ".join(proc.cmdline())[:2000]
        except psutil.Error:
            name, exe, cmdline = "?", "", ""
        holder = {"pid": pid, "name": name, "exe": exe, "cmdline": cmdline}
        for p in ports:
            port_map.setdefault(p, []).append(holder)
    return port_map


def _port_scanner_loop() -> None:
    global _port_snapshot
    while True:
        try:
            snap = _scan_once()
            with _port_lock:
                _port_snapshot = snap
        except Exception:
            pass
        time.sleep(_PORT_SNAPSHOT_INTERVAL)


def _ensure_port_scanner() -> None:
    global _port_thread_started
    if _port_thread_started:
        return
    _port_thread_started = True
    t = threading.Thread(target=_port_scanner_loop, daemon=True, name="port-scanner")
    t.start()


def find_port_holder(port: int) -> list[dict]:
    """返回占用指定端口的进程列表（从后台快照读取，不阻塞）"""
    _ensure_port_scanner()
    with _port_lock:
        return list(_port_snapshot.get(port, []))


def port_snapshot() -> dict[int, list[dict]]:
    """返回完整端口→监听进程快照的浅拷贝（不阻塞）。

    给服务面板做「命令行匹配感知外部启动」用：一次拿到所有监听进程，
    在主线程里按 cmdline 匹配各模块，避免逐端口反复加锁。
    """
    _ensure_port_scanner()
    with _port_lock:
        # 值是 list[dict]，dict 本身只读不改，浅拷贝足够避免迭代时被后台线程整体替换
        return {p: list(holders) for p, holders in _port_snapshot.items()}


def invalidate_port_cache() -> None:
    """kill 端口后主动触发一次刷新（后台线程下一周期刷新，这里只清空）"""
    global _port_snapshot
    with _port_lock:
        _port_snapshot = {}


def is_port_listening(port: int) -> bool:
    """同步直查某端口当前是否仍有进程监听（不走后台快照，用于 kill 后确认释放）。

    后台快照每数秒才刷新一次、且 kill 后会被清空，等待端口释放时不能依赖它，
    否则会被「快照已清空 → 误判已释放」骗到。

    实现用 socket.connect_ex 探活（微秒级），而非 psutil.net_connections 全量
    扫描（Windows 上一次 200-800ms）。本函数曾被主线程的端口释放等待循环反复
    调用，全量扫描会把 UI 卡到肉眼可见——这正是项目反复强调「net_connections
    不上主线程」的红线。connect_ex 直连 127.0.0.1:port：连得上=仍在监听，连不上
    =已释放。本地 dev 服务几乎都监听 0.0.0.0/127.0.0.1，可靠。
    """
    for family, host in ((socket.AF_INET, "127.0.0.1"),
                         (socket.AF_INET6, "::1")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as s:
                s.settimeout(0.2)
                if s.connect_ex((host, port)) == 0:
                    return True
        except OSError:
            continue
    return False


def kill_pid(pid: int) -> bool:
    """一键杀掉指定 PID 及其子进程"""
    try:
        _kill_tree(pid, graceful_timeout=2.0)
        invalidate_port_cache()
        return True
    except psutil.Error:
        return False
