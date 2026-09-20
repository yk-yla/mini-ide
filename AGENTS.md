# mini-ide 协作规范

本文件只保留长期有效的协作规则、产品不变量、工程约束和发布安全要求。CLI 的完整参数见 [docs/cli.md](docs/cli.md)，工作区生命周期见 [docs/workspaces.md](docs/workspaces.md)，日志契约见 [docs/logging.md](docs/logging.md)。

## 回复与授权

- 所有回复使用简短、易懂的中文。
- 先判断用户是在提问/讨论，还是明确要求执行。只有用户明确说“改、修、实现、执行”等，才能修改文件、启动/停止服务或进行其他写操作。
- 用户只要求检查、分析、评估或解释时，默认只读检查，不顺手修改。
- 一条消息包含多个事项时，逐项判断授权范围，不能扩大到其他事项。
- 危险或不可逆操作先停下说明：删除数据、生产环境操作、强制推送/回滚、批量删除文件等必须得到明确确认。
- 同一问题连续失败两次时，停止原方向，分析原因并更换方案。
- 优先读取本地代码和文件；联网事实查询使用联网搜索工具；服务启停、日志、诊断和构建检查使用 mini-ide CLI。
- 保留用户已有改动，不使用 `git reset --hard`、`git checkout --` 等破坏性命令覆盖它们。

## 项目地图

- 技术栈：Python 3.11+、PySide6、Pygments、psutil、watchdog、sqlparse。
- 源码根目录：`_build/`。
- 逻辑层：`_build/src/core/`；界面层：`_build/src/ui/`；工具层：`_build/src/util/`。
- GUI 入口：`_build/main.py`；CLI 入口：`_build/cli_main.py`。
- 长期运行模型和多技术栈扩展基线见 [docs/runtime-units.md](docs/runtime-units.md)；涉及项目识别、服务生命周期、GUI 或 CLI 的改动先对照该文档。
- 性能优化基线、阶段计划和验收标准见 [docs/performance.md](docs/performance.md)；涉及扫描、索引、搜索、主线程或大数据量渲染的改动先对照该文档。
- 发布文件：根目录 `mini-ide.exe`、`mini-ide-cli.exe`、`mini-ide-runtime/`。
- 打包脚本：`_build\scripts\build.bat`。
- 冒烟测试：在 `_build` 目录运行 `poetry run python scripts/_smoke.py`。

## 产品行为

### 普通项目

普通项目占一个顶层 Tab。项目页提供服务启停、日志、文件、Git、诊断和编译；多模块项目省略模块名时，操作作用于该项目的全部模块。

### 常驻与单实例

- 同一时间只运行一个 GUI 实例；重复启动会向已有实例转发路径。
- 点击窗口关闭按钮只隐藏到系统托盘，不停止服务、不退出进程。
- 托盘单击/双击或托盘菜单“显示 mini-ide”会恢复窗口。
- 菜单“退出”、`Ctrl+Q`、托盘菜单“退出 mini-ide”以及 CLI `--quit` 才会执行真正退出前检查并结束进程。
- 隐藏后收到新的单实例打开请求时，窗口会自动唤回。
- 打包、调试和自动化需要真正退出时，使用 CLI `--can-quit`/`--quit`，不要强杀进程。

### 聚合目录

聚合目录只占一个顶层 Tab，内部有“项目”和“需求工作区”两个页面。定义文件为 `<聚合根目录>/.mini-ide/project.json`，必须有效且至少包含一个直属组件；组件路径必须位于聚合根目录内。

目录按聚合处理的条件：

1. 存在有效或待报告错误的 `.mini-ide/project.json`。
2. 已登记在 `AppConfig.aggregate_project_paths` 中。

配置文件存在但无效时，报告配置错误，不降级为普通项目。聚合配置记录稳定 ID、名称、组件、项目类型、共享标记、工作区目录和可选运行配置。

“文件 → 添加目录...” （`Ctrl+O`）允许选择普通项目或聚合目录。聚合目录扫描直属子目录，识别 `.git`、`build.gradle`、`build.gradle.kts`、`pom.xml`、`package.json`、`pyproject.toml`、`requirements.txt`、`manage.py`、`go.mod` 或 Nginx 配置，并排除隐藏目录和工作区目录。空目录或没有组件时不生成配置。选择普通项目只取消全局登记，不删除已有配置。

最近打开和 CLI 直接打开会按现有配置/登记状态直接识别；拖拽或“添加目录”打开未登记、无配置目录时会先询问普通项目或聚合目录。

聚合项目表中的项目独立提供启动、停止、日志、文件、Git、诊断和编译，不提供跨项目批量启停或运行预设。codex/cc 在源目录模式打开聚合根目录，在工作区模式打开当前任务根目录。

### 需求工作区

需求工作区的详细创建、评审、同步、合并和删除规则见 [docs/workspaces.md](docs/workspaces.md)。界面只展示当前聚合目录的工作区，不提供 Review 按钮；Review 通过 CLI/API 执行。

## CLI 契约

- 外部自动化和 AI 工具只能调用根目录 `mini-ide-cli.exe`，不能用 `mini-ide.exe` 代替。
- CLI 通过 IPC 控制 GUI；成功结果通常为 JSON，错误可能写入 stderr。
- `--resolve-target <path>` 是目录归属的权威只读查询；先用它判断任意当前目录属于普通项目、聚合目录、聚合组件、需求工作区或工作区组件，再选择 `--open`、`--open-aggregate` 或 `--open-workspace`。
- `--open <path>` 会自动向上识别所属聚合/工作区；聚合或工作区组件路径必须归入已有环境 Tab，禁止逐个打开组件创建多个顶层 Tab。
- 项目、聚合目录和工作区目标按规范化完整路径、稳定 ID、唯一短名或唯一模糊匹配解析；歧义时拒绝执行并返回候选。
- 模块名必须精确匹配；省略模块表示该项目的全部模块。
- `--auto-start` 可在 GUI 未运行时启动同目录 GUI；`--status`、`--preflight-build`、`--can-quit`、`--quit` 不自动拉起 GUI。
- 退出码：`0` 成功，`1` 操作失败，`2` 参数错误，`3` GUI 未运行，`4` 超时。
- 完整命令、参数、异步命令和输出约定见 [docs/cli.md](docs/cli.md)；实现源文件为 `_build/src/core/cli_client.py` 和 `_build/src/core/cli_server.py`。

## 工程约束

- 可能耗时的扫描、Git、文件批处理、工作区生命周期和服务操作必须放到 `QThread` 或 CLI 工作线程；轻量 UI 查询可以同步执行，不能阻塞主线程。
- UI 颜色、圆角和间距使用 `_build/src/ui/theme.py` 的 token，禁止散落硬编码颜色。
- Windows 后台子进程使用 `CREATE_NO_WINDOW`；需要打开可见终端或 GUI 工具时，使用项目现有的启动器。
- 不引入新依赖，除非用户明确同意。
- 服务启停、日志、诊断、编译和工作区生命周期必须经过 mini-ide CLI 或 GUI；不要直接运行业务项目的 Gradle、Maven、npm 或 Python 启动命令。
- 不要把业务项目的服务、Git 或工作区操作放进 Qt 主线程等待。

## 发布与安全

- 修改源码后运行 `_build\scripts\build.bat`，更新根目录 GUI、CLI 和 `mini-ide-runtime/`。
- 打包前对目标项目执行 `mini-ide-cli.exe --preflight-build <project>` 并确认 `ok=true`。
- 根目录 EXE 被运行中的 IDE 锁定时，必须先获得用户同意，再通过 `--status`/`--can-quit` 确认状态并使用 `--quit` 正常退出；不能强杀或绕过锁定。
- 打包完成后重新启动根目录 `mini-ide.exe`，用 CLI 验证 IPC；常驻模式下不要把窗口关闭按钮当成退出操作。
- 交付前运行冒烟测试、必要的 `compileall` 和 `git diff --check`；失败必须先修复。
- 修改 CLI、服务生命周期、日志、诊断、编译或构建预检行为后，检查 `C:\Users\Administrator\.claude\skills\mini-ide\SKILL.md` 是否需要同步。

## 日志入口

日志契约、轮转策略、当前 GUI/CLI 行为日志覆盖范围和查询示例见 [docs/logging.md](docs/logging.md)。不要假定所有普通 `mini-ide.*` 日志都带 `[GUI]` 前缀；筛选行为日志时使用 `[CLI]`、`[GUI]`，排查启动/异常/托盘问题时同时查看基础 logger。
