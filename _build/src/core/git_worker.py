"""Git 网络操作的后台 QThread 包装。

mini-ide 只做静默 fetch 来刷新 ahead/behind 状态；
checkout / merge+push 走独立 worker 避免冻 UI。
"""
from __future__ import annotations

from src.core.search_worker_lifecycle import BackgroundThread

import logging
import time

from PySide6.QtCore import QThread, Signal


perf_log = logging.getLogger("mini-ide.performance")


class GitFetchWorker(BackgroundThread):
    """git fetch 后台执行（不改本地）。done(ok)"""

    done = Signal(bool)

    def __init__(self, cwd: str, parent=None, require_upstream: bool = False):
        super().__init__(parent)
        self.cwd = cwd
        self.require_upstream = require_upstream

    def run(self) -> None:
        from src.core.git_ops import git_fetch, has_upstream
        if self.require_upstream and not has_upstream(self.cwd):
            self.done.emit(False)
            return
        ok = git_fetch(self.cwd)
        self.done.emit(ok)


class GitBranchListWorker(BackgroundThread):
    """后台读取分支列表，避免打开分支菜单时同步启动多个 Git 子进程。"""

    done = Signal(dict)

    def __init__(self, cwd: str, parent=None):
        super().__init__(parent)
        self.cwd = cwd

    def run(self) -> None:
        started = time.perf_counter()
        from src.core.git_ops import list_branches
        data = list_branches(self.cwd)
        perf_log.info(
            "perf op=git-branch-worker duration_ms=%.1f files=%d status=done",
            (time.perf_counter() - started) * 1000,
            len(data.get("local", [])) + len(data.get("remote", [])),
        )
        self.done.emit(data)


class GitDirtyCheckWorker(BackgroundThread):
    """后台检查工作区是否有未提交改动。"""

    done = Signal(bool)

    def __init__(self, cwd: str, parent=None):
        super().__init__(parent)
        self.cwd = cwd

    def run(self) -> None:
        started = time.perf_counter()
        from src.core.git_ops import is_dirty
        dirty = is_dirty(self.cwd)
        perf_log.info(
            "perf op=git-dirty-worker duration_ms=%.1f files=0 matches=%d status=done",
            (time.perf_counter() - started) * 1000,
            int(dirty),
        )
        self.done.emit(dirty)


class GitCheckoutWorker(BackgroundThread):
    """后台切换分支。done(ok, error_msg)"""

    done = Signal(bool, str)

    def __init__(self, cwd: str, branch: str, parent=None):
        super().__init__(parent)
        self.cwd = cwd
        self.branch = branch

    def run(self) -> None:
        from src.core.git_ops import git_checkout
        ok, msg = git_checkout(self.cwd, self.branch)
        self.done.emit(ok, msg)


class GitCheckoutRemoteWorker(BackgroundThread):
    """后台切到远程分支。done(ok, error_msg)"""

    done = Signal(bool, str)

    def __init__(self, cwd: str, remote_branch: str, parent=None):
        super().__init__(parent)
        self.cwd = cwd
        self.remote_branch = remote_branch

    def run(self) -> None:
        from src.core.git_ops import git_checkout_remote_branch
        ok, msg = git_checkout_remote_branch(self.cwd, self.remote_branch)
        self.done.emit(ok, msg)


class GitDeleteLocalBranchWorker(BackgroundThread):
    """后台安全删除本地分支。done(ok, msg)"""

    done = Signal(bool, str)

    def __init__(self, cwd: str, branch: str, parent=None):
        super().__init__(parent)
        self.cwd = cwd
        self.branch = branch

    def run(self) -> None:
        from src.core.git_ops import git_delete_local_branch
        ok, msg = git_delete_local_branch(self.cwd, self.branch)
        self.done.emit(ok, msg)


class GitMergePushWorker(BackgroundThread):
    """后台执行 fetch + merge 远程分支 + push。done(ok, msg)"""

    done = Signal(bool, str)

    def __init__(self, cwd: str, remote_branch: str, parent=None):
        super().__init__(parent)
        self.cwd = cwd
        self.remote_branch = remote_branch

    def run(self) -> None:
        from src.core.git_ops import git_merge_remote_branch
        ok, msg = git_merge_remote_branch(self.cwd, self.remote_branch)
        self.done.emit(ok, msg)


class GitStatusWorker(BackgroundThread):
    """只读取本地状态，慢查询由独立任务补充，不占用下一轮刷新。"""

    done = Signal(object)
    failed = Signal(object, str)

    def __init__(self, cwd: str, parent=None):
        super().__init__(parent)
        self.cwd = cwd

    def run(self) -> None:
        started = time.perf_counter()
        started_ns = time.monotonic_ns()
        from src.core.git_ops import read_status_snapshot
        try:
            snapshot = read_status_snapshot(self.cwd)
            if self.isInterruptionRequested():
                return
            self.done.emit(snapshot)
            perf_log.info(
                "perf op=git-status-worker duration_ms=%.1f files=%d status=done",
                (time.perf_counter() - started) * 1000, len(snapshot.files),
            )
        except Exception as error:
            perf_log.exception("Git 状态刷新失败: %s", self.cwd)
            self.failed.emit(started_ns, str(error))


class GitStatusDetailsWorker(BackgroundThread):
    """补充忽略文件、上游提交数；每个项目最多一轮。"""

    done = Signal(object, set, object)

    def __init__(self, cwd: str, snapshot, parent=None):
        super().__init__(parent)
        self.cwd = cwd
        self.snapshot = snapshot

    def run(self) -> None:
        from src.core.git_ops import list_ignored_files
        from src.util import git_info

        try:
            ignored = list_ignored_files(self.cwd)
            if self.isInterruptionRequested():
                return
            info = git_info.get_info(self.cwd, changed_count=len(self.snapshot.files))
            self.done.emit(self.snapshot, ignored, info)
        except Exception:
            perf_log.exception("Git 补充信息刷新失败: %s", self.cwd)
