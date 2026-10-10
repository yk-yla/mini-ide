# CLI 说明

根目录 `mini-ide-cli.exe` 是唯一的自动化入口。它通过 IPC 调用已经运行的 GUI；GUI 未运行时，只有带 `--auto-start` 的普通命令会尝试启动同目录的 `mini-ide.exe`。

## 命令

~~~text
--status
--version
--list-projects
--list-modules <project>
--list-runtimes <project>
--open <path>
--resolve-target <path>
--close <project>

--start <project> [module] [--wait] [--timeout N]
--stop <project> [module]
--restart <project> [module] [--wait] [--timeout N]
--ensure-running <project> [module] [--timeout N]
--health <project> [module] [--timeout N]
--compile <project> [--timeout N]
--log <project> [module] [--tail N] [--errors] [--all-modules]
--diagnose <project> [module] [--tail N]

--git-status <project>
--git-diff <project> [--summary|--full] [--max-chars N]
--git-ai-context <project> [--summary|--full] [--max-chars N]

--list-aggregates
--open-aggregate <target>
--list-workspaces
--open-workspace <target>
--close-workspace
--create-development-workspace <aggregate> <name> --projects a,b [--description text]
--workspace-review <target>
--workspace-commit-push <target> [--message text]
--workspace-delete-check <target>
--workspace-sync <target> [--fetch] [--keep-conflicts]

--preflight-build [project]
--can-quit
--quit
~~~

`--status`、`--preflight-build`、`--can-quit` 和 `--quit` 不自动启动 GUI。`--can-quit` 是不退出的全局退出前检查；`--quit` 会停止所有运行中的服务并退出 GUI。窗口关闭按钮只隐藏到托盘，不能替代 `--quit`。

## 目标解析

`--version` 读取 CLI 本身的固定版本和构建时间，不连接或启动 GUI。`--status` 的 `version`、`build_time` 则属于当前运行的 GUI。打包版本规则见 [releasing.md](releasing.md)。

服务启停按项目互斥：另一个启停操作进行中时返回 `service_operation_in_progress`，构建也不能插入这段过程。`--stop` 在进程、后台进程树清理和监听端口均停止后返回；停止超时不会接续启动。`--start`/`--restart` 不带 `--wait` 时只确认进程已启动，带 `--wait` 时继续验证本次启动代次的就绪信号。异步等待使用 Qt 定时器和后台端口检查，不嵌套事件循环。

Git GUI 对大 diff 仅显示前 200,000 字符、2,000 行，单行最多 4,000 字符；复制当前 diff 同样只复制预览。日志界面的积压队列与显示文本均限 2,000,000 字符，单条显示最多 20,000 字符，完整运行日志仍写入日志文件；错误计数和就绪信号在显示淘汰之前识别。

项目、聚合目录和工作区目标按以下顺序匹配：

1. 规范化完整路径。
2. 稳定 ID 或目录名。
3. 唯一短名/模糊匹配。

多个候选时返回歧义错误和候选列表，不猜测执行。运行单元 ID 必须精确匹配；旧命令中的 `module` 是运行单元 ID 的兼容名称，省略表示目标项目的全部运行单元。

`--list-runtimes <project>` 返回运行单元 ID、显示名、分类、工作目录、启动档位、端口、健康地址、依赖、配置来源和当前状态。`--list-modules` 保留旧响应格式，已有自动化不需要立即迁移。

`--resolve-target <path>` 只解析目录归属并返回 JSON，不打开目录。`kind` 可能是 `normal_project`、`aggregate`、`aggregate_component`、`workspace` 或 `workspace_component`，同时返回所属聚合、工作区、组件、`open_command` 和 `open_target`。

`--open <path>` 使用 GUI 相同的目录识别逻辑，并自动向上查找所属聚合目录或需求工作区：打开聚合根或其任意组件/子目录只创建一个聚合 Tab，打开工作区根或其任意组件/子目录只进入所属工作区并在内部选中组件；普通项目才创建普通项目 Tab。组件路径不会再被拆成独立顶层 Tab。已有聚合配置或全局登记的目录直接按聚合打开；最近打开和 CLI 不会重复询问目录类型。未登记、无配置且不属于已知环境的目录通过 GUI“添加目录”或拖拽打开时，仍需要选择普通项目或聚合目录。

历史会话恢复也使用同一解析规则：旧的 `project:<组件路径>` 会归并为对应的 `aggregate:<聚合根>` 或 `development:<工作区根>`，同一环境只保留一个顶层 Tab。

## 聚合与工作区命令

- `--list-aggregates`、`--open-aggregate` 和 `--list-workspaces` 使用全局已登记的聚合路径；未登记但存在配置的目录可先用 `--open <path>` 登记。
- `--list-workspaces` 只列聚合目录中的开发工作区，不展示旧 `WorkspaceEntry`。
- `--open-workspace` 在所属聚合 Tab 内切换，不创建新的顶层 Tab。
- `--close-workspace` 停止当前工作区环境中的服务并返回聚合源目录。
- `--workspace-delete-check` 会刷新基准分支远程引用，只确认各任务分支已合并且基准分支已推送；不因工作区内未知文件或未提交文件阻止删除。真正删除由 GUI 操作执行。
- `--workspace-commit-push` 在任务 Worktree 提交改动并推送到 `origin`；没有新改动但已有未推送提交时也会推送，不执行强制推送。默认提交信息为工作区名称；某项目失败后停止后续项目。
- `--workspace-sync` 默认只使用本地基准分支；`--fetch` 才拉取上游；冲突默认回滚，`--keep-conflicts` 才保留冲突。

## 编译与退出检查

- `--compile <project>` 只与目标项目自己的服务互斥。编译前记录该项目原本运行的模块，编译后只恢复这些模块，不停止或启动其他项目。
- `--preflight-build <project>` 只检查目标项目；省略项目和 `--can-quit` 都执行全局退出前检查。
- `--close <project>` 会停止目标项目服务并关闭对应 Tab。
- `--quit` 会先刷盘未保存的文件，等待全部后台任务和文件监听清理完成，再停止所有项目服务、保存会话并真正退出 GUI。CLI 最长等待响应 120 秒；保存失败、线程或服务停止超时会返回 `ok=false` 并取消退出，窗口保持可用。不要强杀进程。

## 异步与超时

服务健康检查、等待启动、编译、Git diff、工作区创建/评审/同步/删除预检等长任务由 GUI 工作线程执行，CLI 会等待最终 JSON 响应。`--timeout` 是服务/编译等待时间；CLI IPC 还有少量响应缓冲时间。

返回码：

~~~text
0  成功
1  操作失败
2  参数错误
3  GUI 未运行
4  等待超时
~~~

完整 usage 由无效参数时输出的 usage 和 `_build/src/core/cli_client.py` 定义；当前 CLI 没有单独的 `--help` 命令。
