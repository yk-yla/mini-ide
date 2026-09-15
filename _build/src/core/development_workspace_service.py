"""临时开发工作区的创建、检查、合并和收尾删除。"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from src.core.aggregate_workspace import (
    AggregateProject, DevelopmentWorkspace, WorkspaceComponent,
    save_development_workspace, validate_stable_id,
)
from src.core.path_utils import (
    canonical_path, is_path_within, normalized_path_key, resolve_path_within,
)
from src.util.git_executable import resolve_git_executable
from src.util.pinyin import to_pinyin


CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_OPERATION_LOCK = threading.Lock()
_ACTIVE_OPERATIONS: set[str] = set()
_TASK_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


class WorkspaceOperationError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkspacePlanComponent:
    id: str
    source_path: str
    mode: str
    target_path: str = ""
    base_branch: str = ""
    base_commit: str = ""
    task_branch: str = ""
    remote: str = ""
    workspace_copy_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkspaceCreationPlan:
    aggregate_project: AggregateProject
    workspace_id: str
    name: str
    description: str
    root_path: str
    components: tuple[WorkspacePlanComponent, ...]


@dataclass(frozen=True)
class WorkspaceCreationResult:
    ok: bool
    workspace: DevelopmentWorkspace | None
    created_components: tuple[str, ...]
    rolled_back_components: tuple[str, ...]
    pending_components: tuple[str, ...]
    error: str = ""


@dataclass(frozen=True)
class WorkspaceReviewComponent:
    id: str
    base_branch: str
    base_commit: str
    task_branch: str
    changed_files: int
    commits: tuple[str, ...]
    diff_stat: str
    pushed: bool
    merged: bool
    error: str = ""
    compile_result: str = "未记录"
    health_result: str = "未记录"
    test_result: str = "未记录"


@dataclass(frozen=True)
class WorkspaceMergeComponent:
    id: str
    base_branch: str
    task_branch: str
    merged: bool
    error: str = ""
    pushed: bool = False


@dataclass(frozen=True)
class WorkspaceSyncComponent:
    id: str
    base_branch: str
    task_branch: str
    merge_ref: str = ""
    behind_count: int = 0
    synced: bool = False
    already_current: bool = False
    conflicted: bool = False
    conflict_files: tuple[str, ...] = ()
    rolled_back: bool = False
    new_base_commit: str = ""
    error: str = ""


@dataclass(frozen=True)
class WorkspaceSyncResult:
    ok: bool
    workspace: DevelopmentWorkspace
    components: tuple[WorkspaceSyncComponent, ...]
    error: str = ""

    @property
    def synced_ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.components if item.synced)

    @property
    def conflicted_ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.components if item.conflicted)

    @property
    def needs_conflict_confirmation(self) -> bool:
        """有冲突已安全回滚，等待用户确认是否保留冲突手工解决。"""
        return any(item.conflicted and item.rolled_back for item in self.components)


@dataclass(frozen=True)
class WorkspaceMergeResult:
    ok: bool
    workspace: DevelopmentWorkspace
    components: tuple[WorkspaceMergeComponent, ...]
    error: str = ""


@dataclass(frozen=True)
class WorkspaceCommitPushComponent:
    id: str
    task_branch: str
    committed: bool = False
    pushed: bool = False
    commit_id: str = ""
    error: str = ""


@dataclass(frozen=True)
class WorkspaceCommitPushResult:
    ok: bool
    workspace: DevelopmentWorkspace
    components: tuple[WorkspaceCommitPushComponent, ...]
    error: str = ""


@dataclass(frozen=True)
class WorkspaceSyncPushComponent:
    id: str
    base_branch: str
    task_branch: str
    committed: bool = False
    commit_id: str = ""
    merge_ref: str = ""
    synced: bool = False
    already_current: bool = False
    conflicted: bool = False
    conflict_files: tuple[str, ...] = ()
    rolled_back: bool = False
    pushed: bool = False
    error: str = ""


@dataclass(frozen=True)
class WorkspaceSyncPushResult:
    ok: bool
    workspace: DevelopmentWorkspace
    components: tuple[WorkspaceSyncPushComponent, ...]
    error: str = ""

    @property
    def needs_conflict_confirmation(self) -> bool:
        return any(item.conflicted and item.rolled_back for item in self.components)


@dataclass(frozen=True)
class WorkspaceDeleteComponent:
    id: str
    base_branch: str
    task_branch: str
    merged: bool
    base_pushed: bool
    base_remote_ref: str = ""
    error: str = ""


@dataclass(frozen=True)
class WorkspaceDeletePlan:
    workspace: DevelopmentWorkspace
    components: tuple[WorkspaceDeleteComponent, ...]
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    unknown_paths: tuple[str, ...]

    @property
    def can_delete(self) -> bool:
        return not self.blockers

    @property
    def needs_unmerged_confirmation(self) -> bool:
        return False


def _run_git(cwd: str | Path, args: list[str], timeout: int = 30) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            [resolve_git_executable(), *args],
            cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, "", str(exc)


def _git_value(cwd: str | Path, args: list[str], label: str) -> str:
    code, out, err = _run_git(cwd, args)
    if code != 0 or not out:
        raise WorkspaceOperationError(err or out or f"无法读取 {label}")
    return out.splitlines()[0].strip()


def workspace_id_from_name(name: str) -> str:
    raw = str(name or "").strip()
    if not raw or any(char in raw for char in ("/", "\\")):
        raise WorkspaceOperationError("任务名不能为空，也不能包含路径分隔符")
    pinyin, unsupported = to_pinyin(raw)
    if unsupported:
        raise WorkspaceOperationError(
            "任务名包含无法转为拼音的汉字：" + "、".join(dict.fromkeys(unsupported))
        )
    value = _TASK_SAFE_RE.sub("-", pinyin).strip("-._").lower()
    if not value:
        raise WorkspaceOperationError("任务名无法生成有效的拼音目录名")
    return validate_stable_id(value[:128], "workspace id")


def _remote_identity(repo: Path) -> str:
    code, out, _err = _run_git(repo, ["remote", "get-url", "origin"], timeout=5)
    return out if code == 0 else ""


def build_workspace_creation_plan(
    project: AggregateProject,
    name: str,
    description: str,
    selected_component_ids: list[str] | tuple[str, ...],
) -> WorkspaceCreationPlan:
    """先检查全部仓库，再返回不可变创建计划；不写文件。"""
    workspace_id = workspace_id_from_name(name)
    target_root = canonical_path(project.workspace_root / workspace_id)
    if not is_path_within(target_root, project.workspace_root, allow_equal=False):
        raise WorkspaceOperationError("临时工作区路径越界")
    if target_root.exists():
        raise WorkspaceOperationError(f"目标目录已存在：{target_root}")

    selected_keys = {str(item).casefold() for item in selected_component_ids}
    if not selected_keys:
        raise WorkspaceOperationError("至少选择一个会修改的 Git 项目")
    unknown = selected_keys - {item.id.casefold() for item in project.components}
    if unknown:
        raise WorkspaceOperationError("未知项目：" + ", ".join(sorted(unknown)))

    plan_components: list[WorkspacePlanComponent] = []
    seen_branches_by_remote: set[tuple[str, str]] = set()
    for component in project.components:
        source = canonical_path(component.absolute_path(project.root_path))
        if component.shared:
            plan_components.append(WorkspacePlanComponent(
                id=component.id, source_path=str(source), mode="shared",
            ))
            continue
        if component.id.casefold() not in selected_keys:
            continue
        if not source.is_dir():
            raise WorkspaceOperationError(f"项目目录不存在：{component.id}")
        code, out, _err = _run_git(source, ["rev-parse", "--is-inside-work-tree"], 5)
        if code != 0 or out != "true":
            raise WorkspaceOperationError(f"项目不是 Git 仓库：{component.id}")
        base_branch = _git_value(source, ["branch", "--show-current"], "当前分支")
        if not base_branch:
            raise WorkspaceOperationError(f"项目处于 detached HEAD：{component.id}")
        base_commit = _git_value(source, ["rev-parse", "HEAD"], "基准提交")
        task_branch = f"feature/{workspace_id}-{component.id}"
        code, _out, err = _run_git(source, ["check-ref-format", "--branch", task_branch], 5)
        if code != 0:
            raise WorkspaceOperationError(
                f"任务分支名无效：{task_branch} ({err})"
            )
        code, _out, _err = _run_git(
            source, ["show-ref", "--verify", "--quiet", f"refs/heads/{task_branch}"], 5,
        )
        if code == 0:
            raise WorkspaceOperationError(f"本地任务分支已存在：{task_branch}")
        code, remote_refs, err = _run_git(
            source, ["for-each-ref", "--format=%(refname)", "refs/remotes"], 10,
        )
        if code != 0:
            raise WorkspaceOperationError(err or f"无法检查远端分支缓存：{component.id}")
        remote_suffix = f"/{task_branch}".casefold()
        if any(
            ref.strip().casefold().endswith(remote_suffix)
            for ref in remote_refs.splitlines()
        ):
            raise WorkspaceOperationError(f"远端任务分支已存在：{task_branch}")
        remote = _remote_identity(source)
        remote_key = (remote.casefold(), task_branch.casefold())
        if remote and remote_key in seen_branches_by_remote:
            raise WorkspaceOperationError(f"同一远端任务分支重复：{task_branch}")
        seen_branches_by_remote.add(remote_key)
        code, worktrees, err = _run_git(source, ["worktree", "list", "--porcelain"], 10)
        if code != 0:
            raise WorkspaceOperationError(err or f"无法检查 Worktree：{component.id}")
        if f"branch refs/heads/{task_branch}" in worktrees:
            raise WorkspaceOperationError(f"任务分支已在其他 Worktree 检出：{task_branch}")
        plan_components.append(WorkspacePlanComponent(
            id=component.id,
            source_path=str(source),
            mode="worktree",
            target_path=str(target_root / component.id),
            base_branch=base_branch,
            base_commit=base_commit,
            task_branch=task_branch,
            remote=remote,
            workspace_copy_files=component.workspace_copy_files,
        ))

    selected_plans = [item for item in plan_components if item.mode == "worktree"]
    if len(selected_plans) != len(selected_keys):
        missing = selected_keys - {item.id.casefold() for item in selected_plans}
        raise WorkspaceOperationError(
            "选中的项目不能创建 Worktree：" + ", ".join(sorted(missing))
        )
    return WorkspaceCreationPlan(
        aggregate_project=project,
        workspace_id=workspace_id,
        name=str(name).strip(),
        description=str(description or "").strip(),
        root_path=str(target_root),
        components=tuple(plan_components),
    )


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    try:
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def _task_agents_text(plan: WorkspaceCreationPlan) -> str:
    project = plan.aggregate_project
    editable = [item for item in plan.components if item.mode == "worktree"]
    shared = [item for item in plan.components if item.mode == "shared"]
    included = {item.id.casefold() for item in plan.components}
    unselected = [
        item for item in project.components if item.id.casefold() not in included
    ]

    rows = []
    for item in editable:
        values = (
            item.id, item.source_path, item.base_branch, item.base_commit,
            item.task_branch, item.target_path, item.base_branch,
        )
        escaped = [str(value).replace("|", "\\|") for value in values]
        rows.append("| " + " | ".join(
            f"`{value}`" for value in escaped
        ) + " |")
    editable_table = (
        "| 项目 | 源仓库 | 基准分支 | 基准提交 | 任务分支 | 可修改 Worktree | 最终合并目标 |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        + "\n".join(rows)
    )

    forbidden = [
        f"- `{item.id}`：`{item.source_path}`（共享引用，只可读取和启动）"
        for item in shared
    ]
    forbidden.extend(
        f"- `{item.id}`：`{item.absolute_path(project.root_path)}`（本需求未选择）"
        for item in unselected
    )
    return (
        f"# {plan.name}\n\n"
        "## 任务\n\n"
        f"- 任务名称：{plan.name}\n"
        f"- 任务目标：{plan.description or '见 context.md'}\n"
        f"- 来源聚合目录：`{project.root_path}`\n"
        f"- 当前任务根目录：`{plan.root_path}`\n\n"
        "## 项目来源与合并目标\n\n"
        f"{editable_table}\n\n"
        "开发完成后，各项目代码分别合并回表中的最终合并目标；"
        "仅允许用户通过 mini-ide 的“合并代码”明确触发快进合并，"
        "禁止 AI 自行合并仓库或强删分支。\n\n"
        "## 修改边界\n\n"
        "只允许修改上表中的可修改 Worktree。以下目录禁止在本需求中修改：\n\n"
        + ("\n".join(forbidden) if forbidden else "- 无")
        + "\n\n"
        "必须继续遵守来源聚合目录及其上级的 `AGENTS.md`，尤其是：\n"
        f"- `{Path(project.root_path) / 'AGENTS.md'}`\n\n"
        "## mini-ide 约束\n\n"
        "- 服务启动、停止、健康检查、日志、诊断和编译必须走 mini-ide CLI 或 GUI。\n"
        "- 工作区创建、Review 和删除必须走 mini-ide；不要手工移动或递归删除 Worktree。\n"
        "- codex 和 cc 的工作目录必须保持为当前任务根目录。\n"
        "\n"
        "## 标准开发流程：mine-dev-flow\n\n"
        "本工作区以 `mine-dev-flow` Skill 作为需求开发的标准流程入口。进入当前任务根目录并开启新的 AI 会话后，"
        "AI 必须自动读取并主持该流程；用户只需提供需求和信息、判断理解是否正确、确认关键决策和执行人工验收，"
        "不需要主动输入 Skill 名称或阶段命令。\n\n"
        "该流程负责需求整理与对齐、全工作区影响分析及确认、独立影响复审、context 交接、开发计划、"
        "基线、实现、代码 Review、API 测试、人工验收和完成证据。缺少需求或需要确认时，AI 应主动向用户提问；"
        "用户确认后自动进入下一阶段。首次运行会在工作区根目录初始化或读取 `"
        ".mine-dev-flow/state.json`；流程状态和证据只写入 `.mine-dev-flow/`，不要修改 `workspace.json` 或写入组件仓库。\n\n"
        "提交、推送、同步、合并和删除仍需用户单独明确授权；服务、日志、诊断和编译继续遵守上面的 mini-ide 约束。\n"
    )


def _context_text(plan: WorkspaceCreationPlan) -> str:
    return (
        f"# {plan.name}\n\n"
        "## 需求\n\n"
        f"{plan.description or '待补充'}\n\n"
        "## 已确认决策\n\n- \n\n"
        "## 未解决问题\n\n- \n\n"
        "## Review 结论\n\n- \n"
    )


def _copy_workspace_files(item: WorkspacePlanComponent) -> list[Path]:
    source_root = canonical_path(item.source_path)
    target_root = canonical_path(item.target_path)
    copied: list[Path] = []
    try:
        for relative in item.workspace_copy_files:
            source = resolve_path_within(
                source_root, relative,
                label=f"{item.id} workspaceCopyFiles source",
            )
            target = resolve_path_within(
                target_root, relative,
                label=f"{item.id} workspaceCopyFiles target",
            )
            if source.is_symlink():
                raise WorkspaceOperationError(
                    f"{item.id} 白名单文件不能是软链接：{relative}"
                )
            if not source.is_file():
                raise WorkspaceOperationError(
                    f"{item.id} 白名单文件不存在或不是普通文件：{relative}"
                )
            if target.exists() or target.is_symlink():
                raise WorkspaceOperationError(
                    f"{item.id} 白名单文件目标已存在：{relative}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            copied.append(target)
            shutil.copy2(source, target)
        return copied
    except Exception:
        for target in reversed(copied):
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _unexpected_worktree_status(
    status: str,
    workspace_copy_files: tuple[str, ...],
) -> str:
    managed = {
        path.replace("\\", "/").casefold()
        for path in workspace_copy_files
    }
    unexpected: list[str] = []
    for line in status.splitlines():
        if line.startswith("?? "):
            relative = line[3:].replace("\\", "/").casefold()
            if relative in managed:
                continue
        unexpected.append(line)
    return "\n".join(unexpected)


def create_development_workspace(plan: WorkspaceCreationPlan) -> WorkspaceCreationResult:
    operation_key = normalized_path_key(plan.root_path)
    with _OPERATION_LOCK:
        if operation_key in _ACTIVE_OPERATIONS:
            return WorkspaceCreationResult(
                False, None, (), (), (), "同名工作区正在创建或删除",
            )
        _ACTIVE_OPERATIONS.add(operation_key)
    created: list[WorkspacePlanComponent] = []
    copied_files: dict[str, list[Path]] = {}
    rolled_back: list[str] = []
    try:
        root = Path(plan.root_path)
        root.mkdir(parents=True, exist_ok=False)
        worktree_items = [item for item in plan.components if item.mode == "worktree"]
        for item in worktree_items:
            code, out, err = _run_git(
                item.source_path,
                ["worktree", "add", "-b", item.task_branch, item.target_path, item.base_commit],
                timeout=120,
            )
            if code != 0:
                raise WorkspaceOperationError(err or out or f"创建 {item.id} 失败")
            created.append(item)
            if item.workspace_copy_files:
                copied_files[item.id] = _copy_workspace_files(item)

        workspace_components = [
            WorkspaceComponent(
                id=item.id,
                source_repository_path=item.source_path,
                mode=item.mode,
                worktree_path=item.target_path,
                base_branch=item.base_branch,
                base_commit=item.base_commit,
                task_branch=item.task_branch,
                workspace_copy_files=item.workspace_copy_files,
            )
            for item in plan.components
        ]
        workspace = DevelopmentWorkspace(
            root_path=plan.root_path,
            id=plan.workspace_id,
            name=plan.name,
            created_at=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            aggregate_project_path=plan.aggregate_project.root_path,
            status="active",
            components=tuple(workspace_components),
            description=plan.description,
            runtime_state_ref=f"{plan.aggregate_project.id}/{plan.workspace_id}",
        )
        _write_text_atomic(root / "AGENTS.md", _task_agents_text(plan))
        _write_text_atomic(root / "context.md", _context_text(plan))
        save_development_workspace(workspace, aggregate_project=plan.aggregate_project)
        return WorkspaceCreationResult(
            True, workspace, tuple(item.id for item in created), (), (), "",
        )
    except Exception as exc:
        for item in reversed(created):
            for copied in reversed(copied_files.get(item.id, [])):
                try:
                    copied.unlink(missing_ok=True)
                except OSError:
                    pass
            code, dirty, _err = _run_git(item.target_path, ["status", "--porcelain"], 10)
            if code != 0 or dirty:
                continue
            remove_code, _out, _err = _run_git(
                item.source_path, ["worktree", "remove", item.target_path], 60,
            )
            if remove_code == 0:
                _run_git(item.source_path, ["worktree", "prune"], 30)
                _run_git(item.source_path, ["branch", "-d", item.task_branch], 30)
                rolled_back.append(item.id)
        try:
            Path(plan.root_path).rmdir()
        except OSError:
            pass
        pending = [
            item.id for item in plan.components
            if item.mode == "worktree"
            and item.id not in {created_item.id for created_item in created}
        ]
        return WorkspaceCreationResult(
            False,
            None,
            tuple(item.id for item in created),
            tuple(rolled_back),
            tuple(pending),
            str(exc),
        )
    finally:
        with _OPERATION_LOCK:
            _ACTIVE_OPERATIONS.discard(operation_key)


def review_development_workspace(
    workspace: DevelopmentWorkspace,
) -> tuple[WorkspaceReviewComponent, ...]:
    results: list[WorkspaceReviewComponent] = []
    for item in workspace.components:
        if item.mode != "worktree":
            continue
        code, status, err = _run_git(item.worktree_path, ["status", "--porcelain"], 15)
        if code != 0:
            results.append(WorkspaceReviewComponent(
                item.id, item.base_branch, item.base_commit, item.task_branch,
                0, (), "", False, False, err or "无法读取 Git 状态",
            ))
            continue
        commits_code, commits_out, commits_err = _run_git(
            item.worktree_path,
            ["log", "--oneline", f"{item.base_commit}..HEAD"], 20,
        )
        diff_code, diff_out, diff_err = _run_git(
            item.worktree_path,
            ["diff", "--stat", item.base_commit, "HEAD"], 20,
        )
        pushed_code, pushed_out, _ = _run_git(
            item.source_repository_path,
            ["branch", "-r", "--contains", item.task_branch], 15,
        )
        merged_code, _out, _ = _run_git(
            item.source_repository_path,
            ["merge-base", "--is-ancestor", item.task_branch, item.base_branch], 15,
        )
        error = ""
        if commits_code != 0:
            error = commits_err
        elif diff_code != 0:
            error = diff_err
        results.append(WorkspaceReviewComponent(
            id=item.id,
            base_branch=item.base_branch,
            base_commit=item.base_commit,
            task_branch=item.task_branch,
            changed_files=len(_unexpected_worktree_status(
                status, item.workspace_copy_files,
            ).splitlines()) if status else 0,
            commits=tuple(line for line in commits_out.splitlines() if line),
            diff_stat=diff_out,
            pushed=pushed_code == 0 and bool(pushed_out),
            merged=merged_code == 0,
            error=error,
        ))
    review = tuple(results)
    has_task_commits = any(item.commits for item in review)
    fully_merged = bool(review) and all(
        not item.error and item.changed_files == 0 and item.merged for item in review
    )
    next_status = "merged" if has_task_commits and fully_merged else "reviewing"
    if workspace.status != next_status:
        save_development_workspace(replace(workspace, status=next_status))
    return review


def _merge_preflight_error(
    item: WorkspaceComponent,
    *,
    allow_worktree_changes: bool = False,
) -> str:
    code, status, err = _run_git(item.worktree_path, ["status", "--porcelain"], 15)
    if code != 0:
        return err or "无法读取工作区状态"
    worktree_changes = _unexpected_worktree_status(
        status, item.workspace_copy_files,
    )
    if not allow_worktree_changes and worktree_changes:
        return "工作区有未提交或未跟踪文件"
    code, branch, err = _run_git(
        item.worktree_path, ["branch", "--show-current"], 10,
    )
    if code != 0:
        return err or "无法读取任务分支"
    if branch != item.task_branch:
        return f"工作区当前分支不是 {item.task_branch}"

    code, source_branch, err = _run_git(
        item.source_repository_path, ["branch", "--show-current"], 10,
    )
    if code != 0:
        return err or "无法读取源目录分支"
    if source_branch != item.base_branch:
        return f"源目录当前分支不是 {item.base_branch}"
    code, source_status, err = _run_git(
        item.source_repository_path, ["status", "--porcelain"], 15,
    )
    if code != 0:
        return err or "无法读取源目录状态"
    if source_status:
        return "源目录有未提交或未跟踪文件"

    code, upstream, err = _run_git(
        item.source_repository_path,
        [
            "rev-parse", "--abbrev-ref", "--symbolic-full-name",
            f"{item.base_branch}@{{upstream}}",
        ],
        10,
    )
    if code != 0 or not upstream:
        detail = f"：{err}" if err else ""
        return f"基准分支 {item.base_branch} 未配置上游远程仓库{detail}"

    code, _out, err = _run_git(
        item.source_repository_path,
        ["merge-base", "--is-ancestor", item.base_branch, item.task_branch],
        15,
    )
    if code == 0:
        return ""
    already_merged, _out, merged_err = _run_git(
        item.source_repository_path,
        ["merge-base", "--is-ancestor", item.task_branch, item.base_branch],
        15,
    )
    if already_merged == 0:
        if allow_worktree_changes and worktree_changes:
            return "基准分支已有新提交，请先同步源分支"
        return ""
    return err or merged_err or "基准分支与任务分支已分叉，不能快进合并"


def _commit_worktree_changes(
    item: WorkspaceComponent,
    message: str,
) -> str:
    """提交任务 Worktree 的全部改动；受管理的复制文件仍按现有规则排除。"""
    code, status, err = _run_git(item.worktree_path, ["status", "--porcelain"], 15)
    if code != 0:
        return err or "无法读取工作区状态"
    if not _unexpected_worktree_status(status, item.workspace_copy_files):
        return ""

    managed = {
        path.replace("\\", "/").casefold(): path
        for path in item.workspace_copy_files
    }
    managed_untracked = [
        managed[line[3:].replace("\\", "/").casefold()]
        for line in status.splitlines()
        if line.startswith("?? ")
        and line[3:].replace("\\", "/").casefold() in managed
    ]
    code, out, err = _run_git(item.worktree_path, ["add", "--all", "--", "."], 60)
    if code != 0:
        return err or out or "自动暂存改动失败"
    for relative in managed_untracked:
        code, out, err = _run_git(
            item.worktree_path, ["reset", "--", relative], 30,
        )
        if code != 0:
            return err or out or f"排除工作区复制文件失败：{relative}"
    code, out, err = _run_git(
        item.worktree_path, ["commit", "-m", message], 120,
    )
    if code != 0:
        return err or out or "自动提交改动失败"
    return ""


def commit_and_push_development_workspace(
    workspace: DevelopmentWorkspace,
    message: str = "",
) -> WorkspaceCommitPushResult:
    """提交并推送任务 Worktree；不强推，失败后停止后续项目。"""
    operation_key = normalized_path_key(workspace.root_path)
    with _OPERATION_LOCK:
        if operation_key in _ACTIVE_OPERATIONS:
            return WorkspaceCommitPushResult(
                False, workspace, (), "工作区正在执行其它操作",
            )
        _ACTIVE_OPERATIONS.add(operation_key)
    try:
        editable = [item for item in workspace.components if item.mode == "worktree"]
        if not editable:
            return WorkspaceCommitPushResult(False, workspace, (), "工作区没有可提交的项目")
        commit_message = str(message or "").strip() or workspace.name.strip() or workspace.id
        results: list[WorkspaceCommitPushComponent] = []
        for index, item in enumerate(editable):
            code, branch, err = _run_git(
                item.worktree_path, ["branch", "--show-current"], 10,
            )
            if code != 0:
                error = err or "无法读取任务分支"
            elif branch != item.task_branch:
                error = f"工作区当前分支不是 {item.task_branch}"
            else:
                error = ""
            if not error:
                code, remote, err = _run_git(
                    item.worktree_path, ["remote", "get-url", "origin"], 10,
                )
                if code != 0 or not remote:
                    error = err or "未配置 origin 远程仓库"
            committed = False
            pushed = False
            commit_id = ""
            if not error:
                before_code, before_head, _before_err = _run_git(
                    item.worktree_path, ["rev-parse", "HEAD"], 10,
                )
                commit_error = _commit_worktree_changes(item, commit_message)
                if commit_error:
                    error = f"提交失败：{commit_error}"
                else:
                    code, commit_id, _err = _run_git(
                        item.worktree_path, ["rev-parse", "HEAD"], 10,
                    )
                    if code != 0:
                        commit_id = ""
                    committed = bool(
                        before_code == 0 and code == 0 and commit_id != before_head
                    )
            if not error:
                code, out, err = _run_git(
                    item.worktree_path,
                    ["push", "--set-upstream", "origin", item.task_branch],
                    300,
                )
                if code != 0:
                    error = err or out or "推送失败"
                else:
                    pushed = True
            results.append(WorkspaceCommitPushComponent(
                id=item.id,
                task_branch=item.task_branch,
                committed=committed,
                pushed=pushed,
                commit_id=commit_id,
                error=error,
            ))
            if error:
                results.extend(
                    WorkspaceCommitPushComponent(
                        id=pending.id,
                        task_branch=pending.task_branch,
                        error="前序项目提交或推送失败，未执行",
                    )
                    for pending in editable[index + 1:]
                )
                return WorkspaceCommitPushResult(
                    False, workspace, tuple(results), f"{item.id}：{error}",
                )
        return WorkspaceCommitPushResult(True, workspace, tuple(results))
    finally:
        with _OPERATION_LOCK:
            _ACTIVE_OPERATIONS.discard(operation_key)


def merge_development_workspace(
    workspace: DevelopmentWorkspace,
) -> WorkspaceMergeResult:
    operation_key = normalized_path_key(workspace.root_path)
    with _OPERATION_LOCK:
        if operation_key in _ACTIVE_OPERATIONS:
            return WorkspaceMergeResult(False, workspace, (), "工作区正在执行其它操作")
        _ACTIVE_OPERATIONS.add(operation_key)
    try:
        editable = [item for item in workspace.components if item.mode == "worktree"]
        if not editable:
            return WorkspaceMergeResult(False, workspace, (), "工作区没有可合并的项目")

        # 先校验源目录、分支和分叉关系；源目录不满足条件时不应先提交任务改动。
        checked = tuple(
            WorkspaceMergeComponent(
                item.id, item.base_branch, item.task_branch, False,
                _merge_preflight_error(
                    item, allow_worktree_changes=True,
                ),
            )
            for item in editable
        )
        blockers = [f"{item.id}：{item.error}" for item in checked if item.error]
        if blockers:
            return WorkspaceMergeResult(False, workspace, checked, "；".join(blockers))

        commit_message = workspace.name.strip() or workspace.id
        committed: list[WorkspaceMergeComponent] = []
        for index, item in enumerate(editable):
            commit_error = _commit_worktree_changes(item, commit_message)
            if commit_error:
                failed = WorkspaceMergeComponent(
                    item.id, item.base_branch, item.task_branch, False,
                    f"自动提交失败：{commit_error}",
                )
                committed.extend(
                    WorkspaceMergeComponent(
                        previous.id, previous.base_branch, previous.task_branch,
                        False, "已自动提交，尚未合并",
                    )
                    for previous in editable[:index]
                )
                committed.append(failed)
                committed.extend(
                    WorkspaceMergeComponent(
                        pending.id, pending.base_branch, pending.task_branch,
                        False, "前序项目自动提交失败，未执行",
                    )
                    for pending in editable[index + 1:]
                )
                return WorkspaceMergeResult(
                    False, workspace, tuple(committed),
                    f"{item.id}：自动提交失败：{commit_error}",
                )

        # 提交钩子可能产生额外改动，提交后再次执行完整预检。
        checked = tuple(
            WorkspaceMergeComponent(
                item.id, item.base_branch, item.task_branch, False,
                _merge_preflight_error(item),
            )
            for item in editable
        )
        blockers = [f"{item.id}：{item.error}" for item in checked if item.error]
        if blockers:
            return WorkspaceMergeResult(False, workspace, checked, "；".join(blockers))

        merged: list[WorkspaceMergeComponent] = []
        for index, item in enumerate(editable):
            code, out, err = _run_git(
                item.source_repository_path,
                ["merge", "--ff-only", item.task_branch],
                120,
            )
            if code != 0:
                message = err or out or "快进合并失败"
                merged.append(WorkspaceMergeComponent(
                    item.id, item.base_branch, item.task_branch,
                    False, message, False,
                ))
                merged.extend(
                    WorkspaceMergeComponent(
                        pending.id, pending.base_branch, pending.task_branch,
                        False, "前序项目合并失败，未执行", False,
                    )
                    for pending in editable[index + 1:]
                )
                return WorkspaceMergeResult(
                    False, workspace, tuple(merged), f"{item.id}：{message}",
                )

            code, out, err = _run_git(
                item.source_repository_path, ["push"], 300,
            )
            if code != 0:
                detail = err or out or "未知错误"
                message = f"推送基准分支失败：{detail}"
                merged.append(WorkspaceMergeComponent(
                    item.id, item.base_branch, item.task_branch,
                    True, message, False,
                ))
                merged.extend(
                    WorkspaceMergeComponent(
                        pending.id, pending.base_branch, pending.task_branch,
                        False, "前序项目推送失败，未执行", False,
                    )
                    for pending in editable[index + 1:]
                )
                return WorkspaceMergeResult(
                    False, workspace, tuple(merged), f"{item.id}：{message}",
                )
            merged.append(WorkspaceMergeComponent(
                item.id, item.base_branch, item.task_branch, True, "", True,
            ))

        merged_workspace = replace(workspace, status="merged")
        save_development_workspace(merged_workspace)
        return WorkspaceMergeResult(True, merged_workspace, tuple(merged))
    finally:
        with _OPERATION_LOCK:
            _ACTIVE_OPERATIONS.discard(operation_key)


def _resolve_sync_merge_ref(
    item: WorkspaceComponent,
    *,
    fetch_remote: bool,
    fetched: set[tuple[str, str]],
) -> tuple[str, str]:
    """返回 (要合并进任务分支的 ref, 错误)。只读源仓库 ref，不动源目录工作区。"""
    repo = item.source_repository_path
    code, _out, err = _run_git(
        repo, ["show-ref", "--verify", "--quiet", f"refs/heads/{item.base_branch}"], 10,
    )
    if code != 0:
        return "", err or f"源目录缺少基准分支：{item.base_branch}"
    if not fetch_remote:
        return item.base_branch, ""

    code, upstream, _err = _run_git(
        repo,
        [
            "rev-parse", "--abbrev-ref", "--symbolic-full-name",
            f"{item.base_branch}@{{upstream}}",
        ],
        10,
    )
    if code != 0 or not upstream:
        return item.base_branch, ""
    remote = upstream.split("/", 1)[0]
    fetch_key = (normalized_path_key(repo), remote.casefold())
    if fetch_key not in fetched:
        code, out, err = _run_git(repo, ["fetch", "--prune", remote], 300)
        if code != 0:
            return "", err or out or f"拉取远端失败：{remote}"
        fetched.add(fetch_key)

    local_ahead, _out, _err = _run_git(
        repo, ["merge-base", "--is-ancestor", upstream, item.base_branch], 15,
    )
    if local_ahead == 0:
        return item.base_branch, ""
    remote_ahead, _out, _err = _run_git(
        repo, ["merge-base", "--is-ancestor", item.base_branch, upstream], 15,
    )
    if remote_ahead == 0:
        return upstream, ""
    return "", f"源目录 {item.base_branch} 与 {upstream} 已分叉，请先在源目录处理"


def _sync_preflight_error(item: WorkspaceComponent) -> str:
    """同步只要求工作区干净、分支正确；不要求源目录干净或停在基准分支。"""
    code, status, err = _run_git(item.worktree_path, ["status", "--porcelain"], 15)
    if code != 0:
        return err or "无法读取工作区状态"
    if _unexpected_worktree_status(status, item.workspace_copy_files):
        return "工作区有未提交或未跟踪文件"
    code, branch, err = _run_git(item.worktree_path, ["branch", "--show-current"], 10)
    if code != 0:
        return err or "无法读取任务分支"
    if branch != item.task_branch:
        return f"工作区当前分支不是 {item.task_branch}"
    return ""


def workspace_behind_counts(workspace: DevelopmentWorkspace) -> dict[str, int]:
    """各任务分支落后本地基准分支多少提交；只读本地 ref，不联网。"""
    counts: dict[str, int] = {}
    for item in workspace.components:
        if item.mode != "worktree" or not item.task_branch or not item.base_branch:
            continue
        code, out, _err = _run_git(
            item.source_repository_path,
            ["rev-list", "--count", f"{item.task_branch}..{item.base_branch}"],
            15,
        )
        if code != 0:
            continue
        try:
            counts[item.id] = int(out.strip() or "0")
        except ValueError:
            continue
    return counts


def sync_development_workspace(
    workspace: DevelopmentWorkspace,
    *,
    fetch_remote: bool = False,
    keep_conflicts: bool = False,
) -> WorkspaceSyncResult:
    """把源目录基准分支的新提交合并进任务分支，只动 Worktree，不动源目录。"""
    operation_key = normalized_path_key(workspace.root_path)
    with _OPERATION_LOCK:
        if operation_key in _ACTIVE_OPERATIONS:
            return WorkspaceSyncResult(False, workspace, (), "工作区正在执行其它操作")
        _ACTIVE_OPERATIONS.add(operation_key)
    try:
        editable = [item for item in workspace.components if item.mode == "worktree"]
        if not editable:
            return WorkspaceSyncResult(False, workspace, (), "工作区没有可同步的项目")

        fetched: set[tuple[str, str]] = set()
        results: list[WorkspaceSyncComponent] = []
        new_commits: dict[str, str] = {}
        for item in editable:
            results.append(_sync_one_component(
                item, fetch_remote, keep_conflicts, fetched, new_commits,
            ))

        updated = workspace
        if new_commits:
            updated = replace(updated, components=tuple(
                replace(component, base_commit=new_commits[component.id])
                if component.id in new_commits else component
                for component in workspace.components
            ))
        if any(item.synced for item in results) and updated.status == "merged":
            updated = replace(updated, status="active")
        if updated is not workspace:
            save_development_workspace(updated)

        components = tuple(results)
        failures = [
            f"{item.id}：{item.error}"
            for item in components
            if item.error and not (keep_conflicts and item.conflicted)
        ]
        return WorkspaceSyncResult(
            not failures, updated, components, "；".join(failures),
        )
    finally:
        with _OPERATION_LOCK:
            _ACTIVE_OPERATIONS.discard(operation_key)


def _sync_one_component(
    item: WorkspaceComponent,
    fetch_remote: bool,
    keep_conflicts: bool,
    fetched: set[tuple[str, str]],
    new_commits: dict[str, str],
) -> WorkspaceSyncComponent:
    base = WorkspaceSyncComponent(item.id, item.base_branch, item.task_branch)
    error = _sync_preflight_error(item)
    if error:
        return replace(base, error=error)

    merge_ref, error = _resolve_sync_merge_ref(
        item, fetch_remote=fetch_remote, fetched=fetched,
    )
    if error:
        return replace(base, error=error)
    base = replace(base, merge_ref=merge_ref)

    code, target, err = _run_git(
        item.source_repository_path, ["rev-parse", merge_ref], 15,
    )
    if code != 0 or not target:
        return replace(base, error=err or f"无法解析 {merge_ref}")
    target_commit = target.splitlines()[0].strip()

    code, _out, _err = _run_git(
        item.worktree_path, ["merge-base", "--is-ancestor", target_commit, "HEAD"], 15,
    )
    if code == 0:
        # 基准内容已全部包含在任务分支里，顺手修正过期的基准提交记录。
        if target_commit != item.base_commit:
            new_commits[item.id] = target_commit
        return replace(base, already_current=True, new_base_commit=target_commit)

    code, behind, _err = _run_git(
        item.worktree_path, ["rev-list", "--count", f"HEAD..{target_commit}"], 20,
    )
    base = replace(
        base, behind_count=int(behind) if code == 0 and behind.isdigit() else 0,
    )

    code, out, err = _run_git(
        item.worktree_path, ["merge", "--no-edit", target_commit], 180,
    )
    if code == 0:
        new_commits[item.id] = target_commit
        return replace(base, synced=True, new_base_commit=target_commit)

    unmerged_code, unmerged, _err = _run_git(
        item.worktree_path, ["diff", "--name-only", "--diff-filter=U"], 20,
    )
    conflict_files = tuple(
        line.strip() for line in unmerged.splitlines() if line.strip()
    ) if unmerged_code == 0 else ()
    if not conflict_files:
        return replace(base, error=err or out or "合并失败")
    if keep_conflicts:
        return replace(
            base, conflicted=True, conflict_files=conflict_files,
            error="存在冲突，已保留在工作区等待手工解决",
        )
    abort_code, _out, abort_err = _run_git(item.worktree_path, ["merge", "--abort"], 60)
    if abort_code != 0:
        return replace(
            base, conflicted=True, conflict_files=conflict_files,
            error=f"存在冲突且回滚失败，需要手工处理：{abort_err}",
        )
    return replace(
        base, conflicted=True, rolled_back=True, conflict_files=conflict_files,
        error="存在冲突，已回滚到同步前状态",
    )


def inspect_workspace_delete(
    workspace: DevelopmentWorkspace,
    running_paths: tuple[str, ...] | list[str] = (),
) -> WorkspaceDeletePlan:
    # 删除工作区代表需求已经收尾。目录内容、Worktree 脏状态和运行状态不再作为
    # 删除限制；这里只确认任务分支已经进入基准分支，且基准分支已同步到远端。
    _ = running_paths
    blockers: list[str] = []
    fetched: set[tuple[str, str]] = set()
    components: list[WorkspaceDeleteComponent] = []
    for item in workspace.components:
        if item.mode != "worktree":
            continue
        merged_code, _out, merged_err = _run_git(
            item.source_repository_path,
            ["merge-base", "--is-ancestor", item.task_branch, item.base_branch], 15,
        )
        merged = merged_code == 0
        base_remote_ref, base_error = _workspace_base_remote_ref(item, fetched)
        base_pushed = bool(base_remote_ref) and not base_error
        errors = [value for value in (merged_err if not merged else "", base_error) if value]
        if not merged:
            blockers.append(
                f"{item.id} 任务分支 {item.task_branch} 尚未合并到 {item.base_branch}"
            )
        if not base_pushed:
            blockers.append(
                f"{item.id} 基准分支 {item.base_branch} 尚未确认已推送到远程仓库"
            )
        components.append(WorkspaceDeleteComponent(
            id=item.id,
            base_branch=item.base_branch,
            task_branch=item.task_branch,
            merged=merged,
            base_pushed=base_pushed,
            base_remote_ref=base_remote_ref,
            error="；".join(dict.fromkeys(errors)),
        ))
    return WorkspaceDeletePlan(
        workspace=workspace,
        components=tuple(components),
        blockers=tuple(dict.fromkeys(blockers)),
        warnings=(),
        unknown_paths=(),
    )


def _workspace_base_remote_ref(
    item: WorkspaceComponent,
    fetched: set[tuple[str, str]],
) -> tuple[str, str]:
    repo = item.source_repository_path
    code, _out, err = _run_git(
        repo, ["show-ref", "--verify", "--quiet", f"refs/heads/{item.base_branch}"], 10,
    )
    if code != 0:
        return "", err or f"本地缺少基准分支 {item.base_branch}"

    code, upstream, _err = _run_git(
        repo,
        [
            "rev-parse", "--abbrev-ref", "--symbolic-full-name",
            f"{item.base_branch}@{{upstream}}",
        ],
        10,
    )
    if code != 0 or not upstream:
        code, _out, _err = _run_git(repo, ["remote", "get-url", "origin"], 10)
        if code != 0:
            return "", f"基准分支 {item.base_branch} 未配置上游，且仓库没有 origin"
        upstream = f"origin/{item.base_branch}"

    remote = upstream.split("/", 1)[0]
    fetch_key = (normalized_path_key(repo), remote.casefold())
    if fetch_key not in fetched:
        code, out, err = _run_git(repo, ["fetch", "--prune", remote], 300)
        if code != 0:
            return "", err or out or f"拉取远程仓库失败：{remote}"
        fetched.add(fetch_key)

    code, _out, err = _run_git(
        repo, ["show-ref", "--verify", "--quiet", f"refs/remotes/{upstream}"], 10,
    )
    if code != 0:
        return "", err or f"远程缺少基准分支 {upstream}"
    code, _out, err = _run_git(
        repo, ["merge-base", "--is-ancestor", item.base_branch, upstream], 15,
    )
    if code != 0:
        return "", err or f"本地 {item.base_branch} 还有未推送到 {upstream} 的提交"
    return upstream, ""


def _remove_readonly_path(function, path: str, _error_info) -> None:
    os.chmod(path, stat.S_IWRITE)
    function(path)


def _workspace_delete_root_error(workspace: DevelopmentWorkspace) -> str:
    root = canonical_path(workspace.root_path)
    aggregate_root = canonical_path(workspace.aggregate_project_path)
    if not is_path_within(root, aggregate_root, allow_equal=False):
        return "工作区目录不在聚合目录内，拒绝递归删除"
    if root.name.casefold() != workspace.id.casefold():
        return "工作区目录名与工作区 ID 不一致，拒绝递归删除"
    for item in workspace.components:
        if item.mode == "worktree" and not is_path_within(
            item.worktree_path, root, allow_equal=False,
        ):
            return f"{item.id} Worktree 不在工作区目录内，拒绝递归删除"
    return ""


def _delete_remote_task_branches(
    item: WorkspaceComponent,
    base_remote_ref: str,
) -> tuple[list[str], list[str]]:
    repo = item.source_repository_path
    remotes: list[str] = []
    code, _out, _err = _run_git(repo, ["remote", "get-url", "origin"], 10)
    if code == 0:
        remotes.append("origin")
    if base_remote_ref and "/" in base_remote_ref:
        remotes.append(base_remote_ref.split("/", 1)[0])

    remaining: list[str] = []
    errors: list[str] = []
    for remote in dict.fromkeys(remotes):
        code, out, err = _run_git(
            repo,
            ["ls-remote", "--heads", remote, f"refs/heads/{item.task_branch}"],
            60,
        )
        if code != 0:
            remaining.append(f"{remote}/{item.task_branch}")
            errors.append(err or out or f"无法检查远程分支 {remote}/{item.task_branch}")
            continue
        if not out:
            continue
        code, out, err = _run_git(
            repo, ["push", remote, "--delete", item.task_branch], 120,
        )
        if code != 0:
            remaining.append(f"{remote}/{item.task_branch}")
            errors.append(err or out or f"删除远程分支失败：{remote}/{item.task_branch}")
    return remaining, errors


def delete_development_workspace(
    plan: WorkspaceDeletePlan,
    *,
    confirm_unmerged_backed_up: bool = False,
) -> tuple[bool, tuple[str, ...], str]:
    _ = confirm_unmerged_backed_up
    fresh_plan = inspect_workspace_delete(plan.workspace)
    if not fresh_plan.can_delete:
        return False, (), "；".join(fresh_plan.blockers)
    safety_error = _workspace_delete_root_error(fresh_plan.workspace)
    if safety_error:
        return False, (), safety_error
    operation_key = normalized_path_key(plan.workspace.root_path)
    with _OPERATION_LOCK:
        if operation_key in _ACTIVE_OPERATIONS:
            return False, (), "同名工作区正在创建或删除"
        _ACTIVE_OPERATIONS.add(operation_key)
    remaining_branches: list[str] = []
    try:
        root = canonical_path(fresh_plan.workspace.root_path)
        errors: list[str] = []
        # Detach each owned Worktree before removing files. Git otherwise refuses
        # to delete its checked-out branch when an occupied directory survives.
        for item in fresh_plan.workspace.components:
            if item.mode != "worktree":
                continue
            code, branch, _err = _run_git(
                item.worktree_path, ["symbolic-ref", "--quiet", "HEAD"], 10,
            )
            if code == 0 and branch == f"refs/heads/{item.task_branch}":
                code, commit, err = _run_git(
                    item.worktree_path, ["rev-parse", "--verify", "HEAD"], 10,
                )
                if code == 0:
                    code, _out, err = _run_git(
                        item.worktree_path,
                        ["update-ref", "--no-deref", "HEAD", commit], 10,
                    )
                if code != 0:
                    errors.append(f"{item.id} 解除目录与分支的关联失败：{err}")
        directory_error = ""
        try:
            if root.exists():
                shutil.rmtree(root, onerror=_remove_readonly_path)
        except OSError as exc:
            directory_error = f"工作区目录未删完：{root}；{exc}"
            errors.append(directory_error)

        statuses = {item.id: item for item in fresh_plan.components}
        pruned_repositories: set[str] = set()
        for item in fresh_plan.workspace.components:
            if item.mode != "worktree":
                continue
            repo_key = normalized_path_key(item.source_repository_path)
            if repo_key not in pruned_repositories:
                code, out, err = _run_git(
                    item.source_repository_path,
                    ["worktree", "prune", "--expire", "now"],
                    30,
                )
                if code != 0:
                    errors.append(err or out or f"清理 {item.id} Worktree 记录失败")
                pruned_repositories.add(repo_key)

            code, _out, ref_error = _run_git(
                item.source_repository_path,
                ["show-ref", "--verify", "--quiet", f"refs/heads/{item.task_branch}"],
                10,
            )
            if code == 0:
                delete_code, out, err = _run_git(
                    item.source_repository_path, ["branch", "-D", item.task_branch], 30,
                )
                if delete_code != 0:
                    remaining_branches.append(item.task_branch)
                    errors.append(f"本地分支 {item.task_branch} 删除失败：{err or out}")
            elif code != 1:
                remaining_branches.append(item.task_branch)
                errors.append(f"本地分支 {item.task_branch} 检查失败：{ref_error}")

            status = statuses[item.id]
            remote_remaining, remote_errors = _delete_remote_task_branches(
                item, status.base_remote_ref,
            )
            remaining_branches.extend(remote_remaining)
            errors.extend(remote_errors)

        if errors:
            return (
                False,
                tuple(dict.fromkeys(remaining_branches)),
                ("目录清理未完成。" if directory_error else "工作区目录已删除。")
                + ("部分分支未删完。" if remaining_branches else "本地和远程任务分支均已删除。")
                + "\n" + "；".join(dict.fromkeys(errors)),
            )
        return True, (), ""
    finally:
        with _OPERATION_LOCK:
            _ACTIVE_OPERATIONS.discard(operation_key)


def sync_commit_and_push_development_workspace(
    workspace: DevelopmentWorkspace,
    message: str = "",
    *,
    keep_conflicts: bool = False,
) -> WorkspaceSyncPushResult:
    """先保存任务改动、获取并同步远端基准分支，再推送任务分支。"""
    operation_key = normalized_path_key(workspace.root_path)
    with _OPERATION_LOCK:
        if operation_key in _ACTIVE_OPERATIONS:
            return WorkspaceSyncPushResult(
                False, workspace, (), "工作区正在执行其它操作",
            )
        _ACTIVE_OPERATIONS.add(operation_key)
    try:
        editable = [item for item in workspace.components if item.mode == "worktree"]
        if not editable:
            return WorkspaceSyncPushResult(
                False, workspace, (), "工作区没有可同步并推送的项目",
            )

        states = {
            item.id: WorkspaceSyncPushComponent(
                item.id, item.base_branch, item.task_branch,
            )
            for item in editable
        }

        # 在产生任何本地提交前统一校验任务分支和远程，避免明显错误造成部分改动。
        preflight_errors: list[str] = []
        for item in editable:
            code, branch, err = _run_git(
                item.worktree_path, ["branch", "--show-current"], 10,
            )
            error = ""
            if code != 0:
                error = err or "无法读取任务分支"
            elif branch != item.task_branch:
                error = f"工作区当前分支不是 {item.task_branch}"
            if not error:
                code, conflicts, err = _run_git(
                    item.worktree_path,
                    ["diff", "--name-only", "--diff-filter=U"],
                    15,
                )
                if code != 0:
                    error = err or "无法检查工作区冲突状态"
                elif conflicts:
                    error = "工作区仍有未解决冲突，请解决并暂存后重试"
            if not error:
                code, remote, err = _run_git(
                    item.worktree_path, ["remote", "get-url", "origin"], 10,
                )
                if code != 0 or not remote:
                    error = err or "未配置 origin 远程仓库"
            if error:
                states[item.id] = replace(states[item.id], error=error)
                preflight_errors.append(f"{item.id}：{error}")
        if preflight_errors:
            for item in editable:
                if not states[item.id].error:
                    states[item.id] = replace(
                        states[item.id], error="其它项目预检失败，未执行",
                    )
            return WorkspaceSyncPushResult(
                False, workspace, tuple(states[item.id] for item in editable),
                "；".join(preflight_errors),
            )

        commit_message = str(message or "").strip() or workspace.name.strip() or workspace.id
        for index, item in enumerate(editable):
            before_code, before_head, _before_err = _run_git(
                item.worktree_path, ["rev-parse", "HEAD"], 10,
            )
            commit_error = _commit_worktree_changes(item, commit_message)
            if commit_error:
                error = f"提交失败：{commit_error}"
                states[item.id] = replace(states[item.id], error=error)
                for pending in editable[index + 1:]:
                    states[pending.id] = replace(
                        states[pending.id], error="前序项目提交失败，未执行",
                    )
                return WorkspaceSyncPushResult(
                    False, workspace, tuple(states[value.id] for value in editable),
                    f"{item.id}：{error}",
                )
            code, commit_id, _err = _run_git(
                item.worktree_path, ["rev-parse", "HEAD"], 10,
            )
            if code != 0:
                commit_id = ""
            states[item.id] = replace(
                states[item.id],
                committed=bool(
                    before_code == 0 and code == 0 and commit_id != before_head
                ),
                commit_id=commit_id,
            )

        fetched: set[tuple[str, str]] = set()
        sync_results: list[WorkspaceSyncComponent] = []
        new_commits: dict[str, str] = {}
        for item in editable:
            sync_item = _sync_one_component(
                item, True, keep_conflicts, fetched, new_commits,
            )
            sync_results.append(sync_item)
            states[item.id] = replace(
                states[item.id],
                merge_ref=sync_item.merge_ref,
                synced=sync_item.synced,
                already_current=sync_item.already_current,
                conflicted=sync_item.conflicted,
                conflict_files=sync_item.conflict_files,
                rolled_back=sync_item.rolled_back,
                error=sync_item.error,
            )

        updated = workspace
        if new_commits:
            updated = replace(updated, components=tuple(
                replace(component, base_commit=new_commits[component.id])
                if component.id in new_commits else component
                for component in workspace.components
            ))
        if any(item.synced for item in sync_results) and updated.status == "merged":
            updated = replace(updated, status="active")
        if updated is not workspace:
            save_development_workspace(updated)

        sync_failures = [
            f"{item.id}：{item.error}" for item in sync_results if item.error
        ]
        if sync_failures:
            return WorkspaceSyncPushResult(
                False, updated, tuple(states[item.id] for item in editable),
                "；".join(sync_failures),
            )

        for index, item in enumerate(editable):
            code, out, err = _run_git(
                item.worktree_path,
                ["push", "--set-upstream", "origin", item.task_branch],
                300,
            )
            if code != 0:
                error = err or out or "推送失败"
                states[item.id] = replace(states[item.id], error=error)
                for pending in editable[index + 1:]:
                    states[pending.id] = replace(
                        states[pending.id], error="前序项目推送失败，未执行",
                    )
                return WorkspaceSyncPushResult(
                    False, updated, tuple(states[value.id] for value in editable),
                    f"{item.id}：{error}",
                )
            states[item.id] = replace(states[item.id], pushed=True)

        return WorkspaceSyncPushResult(
            True, updated, tuple(states[item.id] for item in editable),
        )
    finally:
        with _OPERATION_LOCK:
            _ACTIVE_OPERATIONS.discard(operation_key)
