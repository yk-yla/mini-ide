# CLI 说明

根目录 `mini-ide-cli.exe` 是唯一的自动化入口。它通过 IPC 调用已经运行的 GUI；GUI 未运行时，只有带 `--auto-start` 的普通命令会尝试启动同目录的 `mini-ide.exe`。

## AI 调用约定

- `--help [command]` 和 `--capabilities` 不连接 GUI，分别返回帮助与机器可读命令清单。能力清单标明参数、操作影响和是否需要 GUI。
- 相对路径在 CLI 调用进程内转换为绝对路径；`server` 等裸 ID 只按 ID/名称匹配，不按 GUI 的工作目录猜路径。重名返回候选，优先复用 `--resolve-target` 返回的绝对 `project_target`。
- 未知参数、空目标、非法数值会直接失败。`--close-workspace <target>` 必须显式指定工作区，不使用界面当前选择。
- 默认保留已有 JSON 对象/数组输出；加 `--machine` 返回统一的 `schema_version`、`command`、`ok`、`data`、`error`。机器模式错误只在 stdout 返回一个 JSON，详细错误位于 `error.code/message/details`。退出码仍需检查。
- `--context <path> [--full]` 一次返回目录归属、组件路径、共享/可写标记、运行单元、已配置动作、Git 摘要及已观察状态。它和绝对路径形式的 `--list-runtimes` / `--list-actions` 不要求先打开项目；未打开项目标记 `observed=false`，不把未知状态写成已停止。默认不返回完整 diff。

## 构建、测试和操作结果

~~~text
--list-actions <project>
--run-action <project> <action> [--timeout N] [--async] [--request-id ID]
--operation <operation_id> [--wait-seconds N]
~~~

`--run-action` 只接受已配置的 compile/build/test/clean 动作；服务继续走 start/ensure-running，不能用任意 shell 命令替代已配置动作。项目级互斥与原有编译入口一致。返回 `action.id/kind/label` 和真实 `exit_code`；Node 项目的旧 `--compile` 可能执行的是 `install`，不能当作 build/test 已通过。

启停、编译、动作执行和工作区长操作均生成 `operation_id`。默认等待最终结果；`--async` 立即返回接收回执，`accepted=true` 只表示接收。编译或工作区操作等待超时不会取消后台执行，超时响应携带编号，可以随后查询；启停/就绪本身的超时仍是该操作失败，响应保留实际观察状态。

`--operation` 成功表示查询成功，实际执行结论看 `operation.status` 和 `operation.result`：running/succeeded/failed/interrupted。`--wait-seconds` 到期返回等待超时而不是执行失败。重试相同操作时沿用 `--request-id`，不会再次执行；同一 ID 改用不同动作或目标会拒绝。

最多保留 64 个操作回执；运行中记录不淘汰。回执保存于当前 APPDATA 配置目录的 `cli-operations.json`，日志输出尾部有界。IDE 重启后未结束记录标为 interrupted，必须先核对实际进程、Git 或文件结果，不能根据回执重放写操作。

## 本次运行日志和分层健康检查

~~~text
--log <project> [module] --current-run [--cursor N] [--run-id ID] [--errors] [--tail N] [--max-chars N]
--diagnose <project> [module] --current-run [--cursor N] [--run-id ID]
--health <project> [module] [--timeout N] [--http]
~~~

本次运行日志从启动代次的原始输出读取，不依赖界面缓冲，返回 run_id、generation、next_cursor、latest_cursor 和 truncated。下一次调用同时传 run-id 与 cursor；重启后旧 run-id 返回 run_changed，不会混入历史日志。`--errors` 保留异常栈上下文；单次输出和每个运行单元缓冲有界，truncated=true 表示存在截断/淘汰，完整历史仍查原始日志文件。外部进程没有托管启动日志时明确返回 unavailable。多运行单元项目必须指定模块。

默认 health 只验证启动就绪，返回进程、TCP、启动标记和 HTTP 各层的检查状态；没有做过的检查显示 not_checked。`--http` 才对配置的本地健康路径实际 GET，不携带登录凭据、不跟随重定向；401/403 标记 auth_required，未配置或请求失败不会冒充通过。超时附最后观察结果。健康通过不代表业务功能测试通过。

隔离验证可为 GUI 与 CLI 设置同一个 `MINI_IDE_SERVER_NAME`，并给子进程设置独立 APPDATA；IPC 和启动锁随实例名隔离。日常使用保持默认值。不要把新 CLI 连接到不支持对应能力的旧 GUI 后声称新版功能已生效。

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
--close-workspace <target>
--create-development-workspace <aggregate> <name> --projects a,b [--description text]
--workspace-review <target>
--workspace-commit-push <target> [--message text]
--workspace-delete-check <target>
--workspace-sync <target> [--fetch] [--keep-conflicts]
--workspace-sync-push <target> [--message text] [--keep-conflicts] [--async] [--request-id ID]
--workspace-merge <target> [--async] [--request-id ID]
--workspace-delete <target> [--async] [--request-id ID]

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

普通项目子目录向上选择最近的项目标记：Git 根（含 Worktree 的 `.git` 文件）、Gradle/Maven/Node/Python/Go 清单、`.mini-ide/runtime.json` 或 Nginx 安装目录。嵌套项目取最近边界；已配置的聚合/工作区组件归属优先，不跨聚合根寻找普通项目。没有标记时保留传入目录。`path` 是规范化后的输入目录，`project_target/open_target` 才是项目根；`--context` 和 `--open` 使用该项目根。

聚合根和工作区根的 `project_target` 为空是正常结果；它们包含多个项目。先读取 `--context <open_target>`，根据任务选择 `projects[].path` 作为服务、日志、Git、构建命令的项目目标，不默认选择第一个组件。

`--open <path>` 使用 GUI 相同的目录识别逻辑，并自动向上查找所属聚合目录或需求工作区：打开聚合根或其任意组件/子目录只创建一个聚合 Tab，打开工作区根或其任意组件/子目录只进入所属工作区并在内部选中组件；普通项目才创建普通项目 Tab。组件路径不会再被拆成独立顶层 Tab。已有聚合配置或全局登记的目录直接按聚合打开；最近打开和 CLI 不会重复询问目录类型。未登记、无配置且不属于已知环境的目录通过 GUI“添加目录”或拖拽打开时，仍需要选择普通项目或聚合目录。

历史会话恢复也使用同一解析规则：旧的 `project:<组件路径>` 会归并为对应的 `aggregate:<聚合根>` 或 `development:<工作区根>`，同一环境只保留一个顶层 Tab。

## 聚合与工作区命令

- `--list-aggregates` 和 `--list-workspaces` 使用全局已登记的聚合路径。`--open-aggregate <绝对路径>` 可直接打开并登记有效配置的聚合目录；ID/名称匹配仍限已登记目录。无效配置返回 `invalid_aggregate_config`，不会降级或猜测其它目标。
- `--list-workspaces` 只列聚合目录中的开发工作区，不展示旧 `WorkspaceEntry`。
- `--open-workspace` 在所属聚合 Tab 内切换，不创建新的顶层 Tab。
- CLI 的 `--open-workspace`、`--open-aggregate` 和自动路由的 `--open` 不会自动停止前一环境服务。切换受阻时返回 `services_running`，包含 `running[].project_path/module/pid`、`source_path/target_path`；只有已获停止授权才停止列出的服务，确认停净后重试。异步打开的失败记录在 `operation.result`，排除阻塞后用新 request-id；同一环境重复打开不要求停服务。
- `--close-workspace <target>` 停止明确指定的已激活工作区服务并返回其聚合源目录，不依赖当前选中 Tab。
- `--workspace-delete-check` 会刷新基准分支远程引用，只确认各任务分支已合并且基准分支已推送；不因工作区内未知文件或未提交文件阻止删除。
- `--workspace-delete` 在用户授权后删除工作区全部内容、Worktree 记录、本地及远程任务分支；内部重新预检，不允许未合并的任务提交。目标激活时返回 workspace_active，先显式 close-workspace；运行中服务会阻止删除，命令不自动停止服务。返回 directory_deleted、remaining_branches 和预检项目结果。
- `--create-development-workspace` 在创建 Worktree 后立即推送任务分支到 origin 并设置上游；要求所选仓库有 origin，全部推送成功才算成功。推送失败时保留本地工作区，返回 local_workspace_preserved、pushed、pending；核对远程后继续推送，不重复创建。
- `--workspace-sync-push` 自动提交任务改动，拉取并合入最新基准分支；全部同步成功后才推送任务分支。同步冲突默认回滚，返回各项目 conflicted/rolled_back/pushed，用户要求保留冲突时才使用 keep-conflicts。
- `--workspace-merge` 将任务分支快进合回创建时记录的基准分支并推送上游；源目录必须干净且停在基准分支。返回各项目 merged/pushed，部分推送失败保留结果并停止后续项目。同步后重新验证与最终合并由 AI 分步骤执行。
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

完整 usage 和能力描述来自 `_build/src/core/cli_contract.py` 的同一份命令定义，可使用 `--help` 或 `--capabilities` 获取。
