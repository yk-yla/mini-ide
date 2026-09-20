"""git 状态读取封装

除 git_fetch 外均不改变仓库状态。主要给状态栏和 GitViewer 用。
Windows 下加 CREATE_NO_WINDOW 避免弹 cmd 窗口。
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from src.util.git_executable import resolve_git_executable

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
_POPEN_KW = {"creationflags": _NO_WINDOW} if _NO_WINDOW else {}


@dataclass
class ChangedFile:
    status: str          # 两字符状态码：' M', 'M ', 'A ', 'D ', '??' 等
    path: str            # 相对仓库根
    is_staged: bool      # 是否已进入 staged
    is_unstaged: bool    # 是否有工作区改动


def _run(args: list[str], cwd: str, timeout: int = 10) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            [resolve_git_executable(), *args],
            cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout, **_POPEN_KW,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except (subprocess.SubprocessError, OSError) as e:
        return -1, "", str(e)


def is_git_repo(path: str) -> bool:
    rc, out, _ = _run(["rev-parse", "--show-toplevel"], path, timeout=3)
    return rc == 0 and bool(out.strip())


def has_git_metadata(path: str) -> bool:
    """主线程可用的轻量仓库判断，只检查当前目录及祖先的 .git。"""
    current = Path(path)
    return any((candidate / ".git").exists() for candidate in (current, *current.parents))


def repo_root(path: str) -> str:
    rc, out, _ = _run(["rev-parse", "--show-toplevel"], path, timeout=3)
    if rc != 0:
        return path
    return out.strip().replace("/", "\\") if sys.platform == "win32" else out.strip()


def list_changed_files(cwd: str) -> list[ChangedFile]:
    """等价于 git status --porcelain，分清楚 staged / unstaged"""
    rc, out, _ = _run(["status", "--porcelain=v1"], cwd)
    if rc != 0:
        return []
    results: list[ChangedFile] = []
    for raw in out.splitlines():
        if len(raw) < 3:
            continue
        staged_flag = raw[0]
        unstaged_flag = raw[1]
        path = raw[3:].strip()
        # 处理 rename "old -> new"
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        # 去除外层引号
        if path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        results.append(ChangedFile(
            status=f"{staged_flag}{unstaged_flag}",
            path=path,
            is_staged=staged_flag not in (" ", "?"),
            is_unstaged=unstaged_flag not in (" ",),
        ))
    return results


# 文件 git 状态枚举（给 file_tree 染色用）
GIT_STATUS_MODIFIED = "modified"   # 已被跟踪且有改动（含 staged + unstaged）
GIT_STATUS_ADDED = "added"         # staged 新增（A 状态）
GIT_STATUS_DELETED = "deleted"     # 删除（含 staged 和 unstaged）
GIT_STATUS_UNTRACKED = "untracked" # 新建但未 add（??）
GIT_STATUS_CONFLICT = "conflict"   # 合并冲突（U/AA/DD/AU/UA/DU/UD）


def file_status_map(cwd: str, files: list["ChangedFile"] | None = None) -> dict[str, str]:
    """返回 {相对仓库根的正斜杠路径: GIT_STATUS_*}。

    优先级：conflict > deleted > added > modified > untracked。
    rename 取目标路径的状态。

    files 可传入已查好的 list_changed_files 结果以复用，避免同一轮刷新里重复
    跑 git status（GitStatusWorker 用得上）；不传则自己查，保持向后兼容。
    """
    if files is None:
        files = list_changed_files(cwd)
    out: dict[str, str] = {}
    for f in files:
        s = f.status  # 两字符 XY
        # 冲突：任一字符为 U，或 AA/DD
        if "U" in s or s in ("AA", "DD"):
            kind = GIT_STATUS_CONFLICT
        elif "D" in s:
            kind = GIT_STATUS_DELETED
        elif s == "??":
            kind = GIT_STATUS_UNTRACKED
        elif "A" in s:
            kind = GIT_STATUS_ADDED
        else:
            kind = GIT_STATUS_MODIFIED
        # 路径用正斜杠（与 file_tree 内部约定一致）
        path = f.path.replace("\\", "/")
        out[path] = kind
    return out


def list_ignored_files(cwd: str) -> set[str]:
    """返回 .gitignore 命中的文件 / 目录相对路径集合（正斜杠）。

    用 git ls-files --others --ignored --exclude-standard --directory：
    --directory 让被整体忽略的目录折成单条（如 node_modules/），不展开里面 N 万个文件。
    """
    rc, out, _ = _run(
        ["ls-files", "--others", "--ignored", "--exclude-standard", "--directory"],
        cwd, timeout=15,
    )
    if rc != 0:
        return set()
    result: set[str] = set()
    for ln in out.splitlines():
        path = ln.strip()
        if not path:
            continue
        if path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        # 目录条目末尾带 /，去掉好与 file_tree 的 rel 字符串一致
        path = path.rstrip("/")
        result.add(path.replace("\\", "/"))
    return result


def list_deleted_paths(cwd: str, files: list["ChangedFile"] | None = None) -> dict[str, list[str]]:
    """返回被删除（磁盘已无）的文件，按父目录归组。

    返回 {parent_rel_posix: [filename, ...]}；parent_rel 为 "" 表示项目根。
    用来给 file_tree 在父目录下补"已删除"占位行——磁盘上 iterdir 看不到这些文件。

    files 同 file_status_map：传入则复用，不传则自查（向后兼容）。
    """
    if files is None:
        files = list_changed_files(cwd)
    out: dict[str, list[str]] = {}
    for f in files:
        if "D" not in f.status:
            continue
        path = f.path.replace("\\", "/")
        if "/" in path:
            parent, name = path.rsplit("/", 1)
        else:
            parent, name = "", path
        out.setdefault(parent, []).append(name)
    return out


def get_file_diff(cwd: str, path: str, staged: bool = False) -> str:
    """获取单个文件的 diff。staged=True 时显示已暂存的改动，否则显示未暂存的"""
    args = ["diff", "--no-color", "-U3"]
    if staged:
        args.append("--staged")
    args += ["--", path]
    rc, out, err = _run(args, cwd, timeout=15)
    if rc != 0 and not out:
        return f"(获取 diff 失败)\n{err}"
    return out


def diff_numstat(cwd: str) -> dict[str, tuple[int, int]]:
    """返回 {path: (additions, deletions)}，合并 staged + unstaged。"""
    result: dict[str, tuple[int, int]] = {}
    for extra in ([], ["--staged"]):
        rc, out, _ = _run(["diff", "--numstat"] + extra, cwd, timeout=10)
        if rc != 0:
            continue
        for ln in out.splitlines():
            parts = ln.split("\t", 2)
            if len(parts) < 3:
                continue
            add_s, del_s, path = parts
            if add_s == "-":
                continue
            try:
                adds, dels = int(add_s), int(del_s)
            except ValueError:
                continue
            if path in result:
                old = result[path]
                result[path] = (old[0] + adds, old[1] + dels)
            else:
                result[path] = (adds, dels)
    return result


def get_untracked_preview(cwd: str, path: str, max_lines: int = 200) -> str:
    """对未跟踪的新文件展示「全部添加」的伪 diff，方便看"""
    p = Path(cwd) / path
    if not p.exists() or p.is_dir():
        return f"(文件不存在或是目录) {path}"
    try:
        lines: list[str] = []
        truncated = False
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for idx, line in enumerate(f):
                if idx >= max_lines:
                    truncated = True
                    break
                lines.append(line.rstrip("\r\n"))
    except OSError as e:
        return f"(读取失败) {e}"
    header = (
        f"diff --git a/{path} b/{path}\n"
        f"new file mode 100644\n"
        f"--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
    )
    body = "\n".join("+" + ln for ln in lines)
    if truncated:
        body += f"\n\n(... 仅展示前 {max_lines} 行 ...)"
    return header + body


def current_branch(cwd: str) -> str:
    rc, out, _ = _run(["rev-parse", "--abbrev-ref", "HEAD"], cwd, timeout=3)
    return out.strip() if rc == 0 else ""


def list_branches(cwd: str, include_remote: bool = True) -> dict:
    """返回 {'current': str, 'local': [str], 'remote': [str]}。
    remote 已过滤掉 HEAD 软连接（如 origin/HEAD -> origin/main）。
    """
    cur = current_branch(cwd)
    rc, out, _ = _run(["branch", "--list", "--format=%(refname:short)"], cwd, timeout=5)
    local = [ln.strip() for ln in out.splitlines() if ln.strip()] if rc == 0 else []

    remote: list[str] = []
    if include_remote:
        rc2, out2, _ = _run(["branch", "-r", "--format=%(refname:short)"], cwd, timeout=5)
        if rc2 == 0:
            for ln in out2.splitlines():
                name = ln.strip()
                if not name or "->" in name:
                    continue
                remote.append(name)
    return {"current": cur, "local": local, "remote": remote}


def git_fetch(cwd: str) -> bool:
    """后台拉远程引用（不改本地分支）。失败静默返回 False。
    注意：此函数同步执行网络操作，调用方必须放后台线程，否则冻结 UI。
    """
    rc, _, _ = _run(["fetch", "--quiet", "--all"], cwd, timeout=60)
    return rc == 0


def has_upstream(cwd: str) -> bool:
    """当前分支是否配置了上游（@{u}）—— 没有上游 pull 会失败，菜单上用它来灰化按钮"""
    rc, _, _ = _run(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd, timeout=3)
    return rc == 0


def is_dirty(cwd: str) -> bool:
    rc, out, _ = _run(["status", "--porcelain"], cwd, timeout=5)
    return rc == 0 and bool(out.strip())


def git_checkout(cwd: str, branch: str) -> tuple[bool, str]:
    """切换分支。返回 (成功, 错误信息)。"""
    rc, out, err = _run(["checkout", branch], cwd, timeout=30)
    if rc == 0:
        return True, ""
    return False, (err or out).strip()


def _local_name_from_remote(remote_branch: str) -> str:
    if "/" not in remote_branch:
        return ""
    return remote_branch.split("/", 1)[1].strip()


def git_checkout_remote_branch(cwd: str, remote_branch: str) -> tuple[bool, str]:
    """切到远程分支：本地已有同名分支就切过去，否则创建跟踪分支。"""
    remote_branch = remote_branch.strip()
    local_branch = _local_name_from_remote(remote_branch)
    if not local_branch:
        return False, f"远程分支名不正确：{remote_branch}"

    data = list_branches(cwd)
    if remote_branch not in data["remote"]:
        return False, f"没有找到远程分支：{remote_branch}"

    if local_branch in data["local"]:
        return git_checkout(cwd, local_branch)

    rc, out, err = _run(["checkout", "--track", "-b", local_branch, remote_branch], cwd, timeout=30)
    if rc == 0:
        return True, ""
    return False, (err or out).strip()


def git_delete_local_branch(cwd: str, branch: str) -> tuple[bool, str]:
    """安全删除本地分支。使用 -d，不强删未合并分支。"""
    branch = branch.strip()
    if not branch:
        return False, "分支名为空。"

    cur = current_branch(cwd)
    if branch == cur:
        return False, "不能删除当前正在使用的分支。"

    data = list_branches(cwd, include_remote=False)
    if branch not in data["local"]:
        return False, f"没有找到本地分支：{branch}"

    rc, out, err = _run(["branch", "-d", branch], cwd, timeout=30)
    if rc == 0:
        return True, (out or f"已删除本地分支：{branch}").strip()
    return False, (err or out).strip()


def git_merge_remote_branch(cwd: str, remote_branch: str) -> tuple[bool, str]:
    """fetch + merge 远程分支到当前分支 + push。返回 (成功, 结果信息)。
    remote_branch 格式如 'origin/main'。
    """
    # fetch
    rc, _, err = _run(["fetch", "--quiet", "--all"], cwd, timeout=60)
    if rc != 0:
        return False, f"fetch 失败：{err.strip()}"

    # 记录 merge 前的 HEAD，用于统计变更
    _, head_before, _ = _run(["rev-parse", "HEAD"], cwd, timeout=3)
    head_before = head_before.strip()

    # merge
    rc, out, err = _run(["merge", remote_branch, "--no-edit"], cwd, timeout=60)
    if rc != 0:
        _run(["merge", "--abort"], cwd, timeout=10)
        msg = (err or out).strip()
        return False, f"合并冲突，已自动 abort：\n{msg}"

    # 统计变更
    _, head_after, _ = _run(["rev-parse", "HEAD"], cwd, timeout=3)
    head_after = head_after.strip()
    if head_before == head_after:
        summary = "已是最新，无需合并。"
    else:
        _, stat, _ = _run(["diff", "--stat", head_before, head_after], cwd, timeout=10)
        # stat 最后一行类似 "3 files changed, 10 insertions(+), 2 deletions(-)"
        lines = stat.strip().splitlines()
        summary = lines[-1].strip() if lines else "合并完成"

    # push
    rc, out, err = _run(["push"], cwd, timeout=60)
    if rc != 0:
        return False, f"合并成功但 push 失败：{(err or out).strip()}\n\n{summary}"

    return True, summary


def list_remote_branches(cwd: str) -> list[str]:
    """列出所有远程分支（含 origin/ 前缀），过滤 HEAD。"""
    rc, out, _ = _run(["branch", "-r", "--format=%(refname:short)"], cwd, timeout=5)
    if rc != 0:
        return []
    results = []
    for ln in out.splitlines():
        name = ln.strip()
        if not name or "->" in name:
            continue
        results.append(name)
    return results
