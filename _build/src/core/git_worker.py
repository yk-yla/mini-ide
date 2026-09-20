"""Git 网络操作的后台 QThread 包装。

mini-ide 只做静默 fetch 来刷新 ahead/behind 状态；
checkout / merge+push 走独立 worker 避免冻 UI。
"""
from __future__ import annotations

import logging
import time

from PySide6.QtCore import QThread, Signal


perf_log = logging.getLogger("mini-ide.performance")


class GitFetchWorker(QThread):
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


class GitBranchListWorker(QThread):
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


class GitDirtyCheckWorker(QThread):
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


class GitCheckoutWorker(QThread):
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


class GitCheckoutRemoteWorker(QThread):
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


class GitDeleteLocalBranchWorker(QThread):
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


class GitMergePushWorker(QThread):
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


class GitStatusWorker(QThread):
    """后台拉 git status / ignored / deleted + 分支信息，给文件树染色和状态栏用。

    主线程跑这些命令在大仓库上 200ms~1s+，每 3s 跑一次会让目录树滚动很卡。
    打包成一个 worker 后台跑，结果信号一次性回主线程刷颜色和状态栏分支/改动数。

    info 用 object 承载 GitInfo | None（非 git 仓库为 None），让状态栏的
    分支按钮、改动数按钮也由后台数据驱动，主线程不再同步调 git。

    done(statuses, ignored, deleted_by_parent, info)
    """

    done = Signal(dict, set, dict, object)

    def __init__(self, cwd: str, parent=None):
        super().__init__(parent)
        self.cwd = cwd

    def run(self) -> None:
        started = time.perf_counter()
        from src.core.git_ops import (
            file_status_map, list_changed_files, list_deleted_paths,
            list_ignored_files,
        )
        from src.util import git_info
        # 一轮刷新里 file_status_map 和 list_deleted_paths 都基于 git status
        # --porcelain，过去各自跑一次（每次 Windows 上 spawn 30-80ms）。这里只
        # 查一次再喂给两个解析函数，省掉一次重复的 git 子进程。
        changed = list_changed_files(self.cwd)
        statuses = file_status_map(self.cwd, files=changed)
        ignored = list_ignored_files(self.cwd)
        deleted = list_deleted_paths(self.cwd, files=changed)
        # 状态栏改动数必须复用本轮新查到的 git status 结果，避免短缓存让
        # 外层显示"有改动"而 GitViewer 重新查询后显示干净。
        info = git_info.get_info(self.cwd, changed_count=len(changed))
        perf_log.info(
            "perf op=git-status-worker duration_ms=%.1f files=%d status=done",
            (time.perf_counter() - started) * 1000, len(changed),
        )
        self.done.emit(statuses, ignored, deleted, info)
