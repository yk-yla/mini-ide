# 运行单元架构设计

2026-10-09 生命周期补充：正常 GUI/CLI 启停通过项目控制器的异步操作入口受理。同项目启停互斥，停止等待进程、进程树清理及端口释放后再返回/接续重启；依赖分组启动保留后续组调度，并检查本次启动代次。ProcessRunner.start 只受理启动，实际结果由 started/finished 信号报告。构建不能插入启停操作，停止超时不继续启动。关闭项目和环境切换的旧同步返回接口暂保留。

> 状态：核心方案首版已实现，后续扩展继续以本文为基线。
>
> 更新日期：2026-08-10
>
> 适用范围：mini-ide 的项目识别、服务生命周期、聚合目录、GUI、CLI、工作区和测试。

## 1. 这份文档解决什么问题

mini-ide 当前可以识别普通前端、Spring Boot、Python、Nginx 等项目，也可以在聚合目录中管理多个项目。但“多服务”目前主要通过 `spring_boot_modules` 表达，项目内部的运行单位没有统一模型：

- Spring Boot 多模块可以分别启动、停止和查看日志。
- 普通 Vue、Node、Python 项目通常只有一个启动配置。
- Lerna/npm workspace 形式的前端聚合项目会被识别成一个 Node/Vue 项目，不能自然展示教师端、学生端等角色。
- Python 的 API、Worker、Celery、定时任务等多个进程没有稳定的表达方式。
- UI 和 CLI 中存在 `project_type`、`_is_multi_module`、Nginx 特判，增加新技术栈会不断扩大特判范围。

长期目标不是让 IDE 记住更多技术栈，而是把所有项目统一描述为“项目中的运行单元”。技术栈只负责识别和生成默认配置，服务管理、日志、端口、健康检查、GUI 和 CLI 都消费统一模型。

## 2. 当前样本和已确认事实

本节记录在 `E:\whaty\project` 只读扫描得到的事实，防止设计脱离真实项目。

### 2.1 聚合目录

当前通过 mini-ide CLI 已登记且配置有效的聚合目录包括：

- `E:\whaty\project\xxpt`：4 个组件。
- `E:\whaty\project\live`：3 个组件。
- `E:\whaty\project\race`：5 个组件。

`xxpt/.mini-ide/project.json` 的直属组件是 `server`、`webapp`、`webapp-lerna`、`nginx`；`race` 还包含多个普通前端项目和后端项目。现有聚合模型已经能表达“一个聚合目录包含多个项目”，这部分应保留。

### 2.2 xxpt 后端

`xxpt/server` 是 Gradle Spring Boot 多模块项目。CLI 当前可以识别出 gateway、auth、manage、user、course、resource、task、thirdparty 等多个运行模块，证明现有 Spring 模块扫描和生命周期控制在这个样本上是可用的。

问题在于这些能力绑定在 `ProjectMeta.spring_boot_modules` 上，不能被其他技术栈复用。

### 2.3 xxpt 前端聚合

`xxpt/webapp-lerna/package.json` 使用 Yarn workspaces 和 Lerna：

- workspaces：`packages/mobile/*`、`packages/pc/*`、`packages/*`。
- 角色脚本：`dev-m-stu`、`dev-pc-stu`、`dev-pc-teacher`。
- 还有对应的 `build-*` 脚本。
- `lerna.json` 声明了 packages 和 Yarn 客户端。

当前 CLI 对该组件返回的结构是：

```json
{
  "id": "webapp-lerna",
  "type": "node",
  "modules": [
    {"name": "root", "state": "stopped"}
  ]
}
```

这说明当前识别器只看到了根目录的 Node 项目，没有把 Lerna workspace 的包和脚本转换成可运行单元。

### 2.4 Python 样本

`E:\whaty\project\hljzk` 和 `E:\whaty\project\jl` 都是 Poetry 项目。它们证明 Python 项目不应只按“有没有 `manage.py`”分类：

- 可以通过 Poetry script 暴露入口。
- 也可以由项目自身的 `src/main.py`、应用脚本或其他命令启动。
- 未来可能同时存在 API、Worker、采集任务和定时任务。

当前 Python 探测器选择一个入口并生成一个 `run` profile，适合单体 Python，但不够表达多进程 Python 项目。

## 3. 核心概念

### 3.1 聚合目录（Aggregate）

一组相关项目的容器，例如 `xxpt`、`live`、`race`。它解决“哪些项目属于同一套业务”和“需求工作区如何复制项目”的问题。

聚合目录可以有多个项目，但不应该直接承担每个项目的启动细节。

### 3.2 项目（Project）

一个代码边界，通常对应一个目录和一个 Git 仓库，也可以是一个共享目录。项目是文件、Git、编译和运行单元的归属对象。

例子：`xxpt/server`、`xxpt/webapp`、`xxpt/webapp-lerna`。

项目不等于进程。一个项目可以只有一个进程，也可以包含多个进程。

### 3.3 运行单元（Runtime Unit）

一个可以独立启动、停止、重启、查看日志和健康检查的目标。运行单元是未来服务管理的核心对象。

运行单元可以叫：

- 后端服务：gateway、auth、user。
- 前端角色：教师端、学生端、管理端。
- Python 进程：API、Worker、Celery、定时任务。
- 单体项目的默认进程：root、app 或项目名。

“模块”“角色”“服务”“进程”在界面上可以使用不同中文名称，但底层都使用 Runtime Unit。

### 3.4 运行预设（Runtime Profile）

多个运行单元的启动组合和顺序。例如：

- 本地开发：先启动 gateway，再并行启动教师端和学生端。
- 后端全量：按依赖顺序启动全部服务。
- 只调试学生端：只启动 gateway 和学生端。

现有聚合配置中的 `profiles.startGroups` 已经部分表达了“按组启动组件”的概念，未来应扩展为可以引用项目内部的运行单元。

## 4. 目标模型

统一层级如下：

```text
聚合目录
└─ 项目
   └─ 运行单元
      └─ 运行配置 / 状态 / 日志
```

示例：

```text
xxpt
├─ server
│  ├─ gateway
│  ├─ auth:auth-server
│  ├─ user:user-student:user-student-server
│  └─ user:user-teacher:user-teacher-server
├─ webapp
│  └─ app
├─ webapp-lerna
│  ├─ mobile-student
│  ├─ pc-student
│  └─ pc-teacher
└─ nginx
   └─ nginx
```

### 4.1 Runtime Unit 的最小字段

实现时建议增加独立的数据类，名称可以是 `RuntimeUnit`。字段语义如下：

| 字段 | 含义 |
| --- | --- |
| `id` | 项目内稳定 ID，CLI 精确匹配使用 |
| `name` | 用户可见名称 |
| `project_id` | 所属项目 ID |
| `kind` | `service`、`frontend`、`worker`、`task` 等显示分类，不承担生命周期分支 |
| `cwd` | 启动工作目录，必须位于项目目录内 |
| `start` | 启动命令的 argv 或受控启动器配置 |
| `stop` | 停止策略或命令，不能默认依赖强杀进程 |
| `profiles` | 运行、编译、测试、预览等动作 |
| `expected_port` | 可选预期端口 |
| `health` | 健康检查方式和超时 |
| `depends_on` | 启动前必须满足的运行单元 |
| `source` | `detected`、`configured` 或 `merged` |
| `metadata` | 技术栈、包名、入口类等识别信息，不参与通用控制流 |

`start`、`stop`、`health` 必须是结构化配置，不能为了省事把整段命令拼成一个 shell 字符串。Windows 后台启动继续遵守 `CREATE_NO_WINDOW` 和现有启动器规则。

### 4.2 单体项目如何表示

单体项目不是特殊分支，而是只有一个默认运行单元：

```text
webapp
└─ app
```

因此单体 Vue、普通 Node、Django、FastAPI、Go 等都走同一套界面和 CLI。类型适配器只负责生成这个单元的命令和健康检查。

### 4.3 多模块后端如何表示

现有 `spring_boot_modules` 中的每一项迁移为一个 `RuntimeUnit`：

- 模块名变成稳定 ID 和显示名。
- 模块路径变成 `cwd`。
- `bootRun` 变成该单元的 `start` profile。
- 模块端口变成 `expected_port`。
- 模块日志和外部进程匹配挂到该单元。

迁移完成后，Spring Boot 仍然可以有“全部启动”和“全部停止”，但这些操作只是对运行单元集合执行，不再由 Spring 专属字段驱动。

### 4.4 Lerna/npm workspace 如何表示

识别顺序：

1. 读取根目录 `package.json` 的 `workspaces`。
2. 读取 `lerna.json` 的 `packages` 和 `useWorkspaces`。
3. 展开匹配到的 package 目录，读取每个 package 的 `name`、`scripts`、框架依赖和端口配置。
4. 对根脚本按 `--scope` 或 workspace 参数建立角色与脚本的映射。
5. 只有明确能独立启动的 package 才生成运行单元；`core`、共享组件等库默认标记为 library，不显示启动按钮。

`xxpt/webapp-lerna` 的目标结果类似：

```text
webapp-lerna
├─ mobile-student  -> yarn run dev-m-stu / build-m-stu
├─ pc-student      -> yarn run dev-pc-stu / build-pc-stu
└─ pc-teacher      -> yarn run dev-pc-teacher / build-pc-teacher
```

如果角色实际都由一个根脚本并行拉起，也要明确标记为一个 composite unit，而不是假装有多个独立停止目标。能否独立停止必须以真实启动进程和脚本语义为准。

### 4.5 Python 如何表示

Python 适配器分三层：

1. 自动识别项目环境：Poetry、pip、uv 或系统 Python。
2. 自动识别常见入口：Django、FastAPI、Flask、Poetry scripts、`main.py`、`app.py` 等。
3. 对无法可靠推断的多个进程提供配置入口，不通过猜文件名无限扩展特判。

例如：

```text
data-service
├─ api       -> poetry run uvicorn src.api:app --port 8000
├─ worker    -> poetry run celery -A src.worker worker
└─ scheduler -> poetry run python -m src.scheduler
```

Django 的 `migrate`、`shell` 仍然可以保留，但它们属于项目动作 profile，不应被误认为长期运行单元。

## 5. 配置来源和优先级

自动识别不可能覆盖所有公司项目，因此必须允许配置覆盖。

配置优先级：

```text
项目明确配置 > 聚合组件 overrides > 技术栈适配器自动识别 > generic 默认值
```

现有 `<aggregate>/.mini-ide/project.json` 继续作为聚合定义和工作区定义。建议向下兼容地增加组件运行配置，例如：

```json
{
  "id": "webapp-lerna",
  "path": "webapp-lerna",
  "type": "frontend",
  "runtime": {
    "detector": "lerna",
    "units": [
      {
        "id": "pc-teacher",
        "name": "教师端",
        "cwd": "packages/pc/teacher",
        "start": ["yarn", "run", "dev-pc-teacher"],
        "profiles": {
          "build": ["yarn", "run", "build-pc-teacher"]
        },
        "expectedPort": 5175
      }
    ]
  }
}
```

最终字段名以实现时的 schema 迁移方案为准，但必须满足以下约束：

- 所有路径都必须位于项目或聚合根目录内。
- ID 在项目内唯一，大小写不敏感匹配。
- 配置错误要明确报告，不能静默降级成普通项目。
- 自动扫描结果可以预览和编辑，不能直接覆盖用户明确配置。
- 缺少端口不能阻止启动，只影响健康检查和展示。
- 停止命令缺失时使用该适配器的安全策略，不能默认执行全局进程杀除。

## 6. 识别器和运行器的分工

### 6.1 识别器（Detector / Adapter）

识别器只负责把目录和配置转换为统一描述：

- Gradle/Maven：生成 Java 项目和 Spring 运行单元。
- Node/Vue/React：生成单体前端运行单元。
- Lerna/npm/pnpm/yarn workspace：生成多个 package 运行单元。
- Python：生成单体入口或读取显式运行单元配置。
- Nginx：生成受控的外部服务运行单元。
- Generic：只提供文件、Git 和手动命令配置。

识别器可以知道技术栈；UI、CLI 和生命周期控制器不应再根据技术栈写分支。

### 6.2 运行器（Runtime Controller）

运行器只处理统一动作：

- validate
- start one
- stop one
- restart one
- start all
- stop all
- wait health
- read log
- diagnose
- build conflict check

运行器处理的是 `RuntimeUnit`，而不是 `spring_boot_modules` 或某一种项目类型。

### 6.3 技术栈特有逻辑应该放哪里

技术栈特有逻辑允许存在，但必须收口在适配器中：

- Spring 的主类探测、Gradle task 拼接。
- Lerna 的 scope、workspace package 展开。
- Django 的 migrate 和 manage.py。
- Nginx 的信号停止和外部进程感知。

不能把这些逻辑继续散落到 `ProjectTab`、CLI server、服务控制器和状态刷新代码中。

## 7. 状态、日志和依赖

### 7.1 状态统一

每个运行单元统一使用：

```text
unknown -> starting -> running -> stopping -> stopped
                         └──────> failed
```

状态必须区分：

- mini-ide 自己启动的进程。
- 外部已存在但被识别接管的进程。
- 端口占用但无法确认归属的进程。

父项目状态由子运行单元汇总：

- 全部停止：已停止。
- 部分运行：部分运行，例如 `2/3 运行中`。
- 全部运行：运行中。
- 任一失败：显示失败数量和可进入的具体日志。

不能因为某个运行单元状态变化就重新排序项目列表。

### 7.2 日志归属

日志必须绑定到 `project_id + runtime_unit_id`。项目级构建、安装和全局错误可以单独作为项目日志，不应把多个角色的输出混在一个默认日志里。

CLI 的 `--log`、`--diagnose`、`--health` 继续支持省略运行单元表示全部；指定运行单元时必须精确匹配。

### 7.3 依赖和启动组

运行单元可以声明 `depends_on`。启动规则：

- 依赖先启动并通过健康检查，当前单元才启动。
- 同一依赖层的单元可以并行启动。
- 依赖失败时，不启动后续单元，并返回明确的阻塞链。
- 停止按反向依赖顺序执行。

聚合目录已有的 `startGroups` 可以逐步迁移为引用 `project_id/runtime_unit_id`，兼容旧配置中的项目级引用。

## 8. GUI 设计原则

### 8.1 左侧导航

左侧不是压缩版表格，而是项目树：

```text
项目 4
├─ server                         3/16 运行中
├─ webapp                         已停止
└─ webapp-lerna                   2/3 运行中
   ├─ 教师端                      运行中  :5175
   ├─ 学生端                      运行中  :5174
   └─ 移动端学生                  已停止
```

设计规则：

- 项目和运行单元使用两级层次，不把角色伪装成独立项目。
- 默认只展开当前选中的多运行单元项目。
- 每行固定高度，状态更新不改变布局。
- 项目显示汇总状态，运行单元显示自己的状态和端口。
- 启动/停止按钮固定在右侧，位置不因悬浮变化。
- 侧栏默认约 320px，可拖动并记忆宽度。
- 项目较少时不显示搜索框；超过 8 个再显示。
- 项目顺序按配置稳定排序，不按运行状态自动跳动。

### 8.2 右侧详情

- 选中项目：显示项目概览、全部运行单元和项目级操作。
- 选中运行单元：显示该单元的文件、日志、Git 和代码。
- 多运行单元项目提供“全部启动/全部停止”，但必须显示具体数量和失败项。
- 安装、Clean、迁移、测试等动作使用项目已有的明确操作入口；codex/cc 在工作区卡片或项目页直接打开。
- 文件树、服务面板和编辑器继续使用现有 `ProjectTab`，先通过适配层接入统一运行单元。

### 8.3 聚合目录和工作区

聚合目录仍然是一个顶层 Tab，内部继续有“项目”和“需求工作区”两个页面。变化只在项目页面内部：

```text
聚合目录
├─ 项目树
│  └─ 项目详情 / 运行单元详情
└─ 需求工作区
```

需求工作区复制的是项目代码边界；进入工作区后，每个项目内部的运行单元重新从 Worktree 或共享目录生成。不能把工作区中的某个角色误认为独立 Git 项目。

## 9. CLI 兼容方案

现有命令不能因为内部重构而失效。

### 9.1 兼容旧参数

- `--start <project>`：单体项目启动唯一运行单元，多运行单元项目启动全部单元。
- `--start <project> <module>`：`module` 作为运行单元 ID 的兼容别名。
- `--stop`、`--restart`、`--health`、`--log`、`--diagnose` 同理。
- `--list-modules` 保留，响应中的 `modules` 逐步由通用运行单元生成。
- 旧的 Spring 模块 ID 保持不变，避免脚本和 AI 工具失效。

### 9.2 新增能力

后续可以增加：

- `--list-runtimes <project>`：列出统一运行单元。
- `--start <project> --all`：显式启动全部运行单元。
- `--start-profile <aggregate> <profile>`：按运行预设启动。
- `--runtime-context <project> <runtime>`：返回 cwd、命令、端口、依赖和状态。

新命令必须继续通过根目录 `mini-ide-cli.exe`，不能让外部自动化直接运行 Gradle、Maven、npm 或业务 Python。

## 10. 配置迁移和实现顺序

必须先改模型，再改 UI。不要先把当前表格改成树，然后继续把旧特判藏在树后面。

### 阶段 0：基线和回归保护

- 为现有 `ProjectMeta`、`RunProfile`、Spring 模块状态和 CLI 响应补充快照测试。
- 固化 `xxpt`、`race`、`live` 的识别结果。
- 明确当前用户改动的 `project_detector.py` 不被覆盖，并补充其端口识别回归测试。

### 阶段 1：引入 RuntimeUnit

- 新增通用 `RuntimeUnit`、`RuntimeState`、`RuntimeSnapshot` 数据结构。
- 将现有 Spring `spring_boot_modules` 映射成运行单元。
- 让 `ProjectServiceController` 只处理运行单元集合。
- 保留旧字段和旧 CLI 输出作为兼容层，不一次性删除。

### 阶段 2：收口技术栈适配器

- 把 Gradle、Maven、Node、Python、Nginx 的识别逻辑拆成适配器。
- `ProjectTab` 不再使用 `project_type` 直接决定是否显示多模块面板。
- `LaunchTracker`、外部进程探测、健康检查改为读取运行单元的声明。

### 阶段 3：支持前端聚合

- 检测 `lerna.json`、package workspaces、pnpm/yarn workspace。
- 展开 package 并识别独立启动脚本。
- 支持 `--scope`、package 目录和根脚本之间的映射。
- 对共享库只提供文件和构建能力，不显示启动按钮。
- 首先用 `xxpt/webapp-lerna` 验证教师端、学生端和移动端。

### 阶段 4：支持 Python 多运行单元

- 保持 Poetry、Django、FastAPI、Flask 单体识别行为。
- 增加项目配置中的显式 Python 运行单元。
- 支持 API、Worker、Celery、定时任务的独立日志和状态。
- 不根据文件名无限猜测危险的后台任务。

### 阶段 5：改 GUI 和 CLI

- 聚合项目页改成左侧项目树、右侧详情。
- 项目和运行单元都支持选择、启动、停止、日志和诊断。
- CLI 先接入兼容层，再发布新的运行单元命令。
- 补充窗口重启、工作区切换、外部进程接管和关闭检查回归。

### 阶段 6：清理旧特判

只有在所有适配器和回归测试通过后，才删除或弃用：

- `spring_boot_modules` 作为核心控制字段。
- `_is_multi_module` 作为 UI 分支条件。
- 业务 UI 中直接判断 `project_type` 的生命周期逻辑。

## 11. 测试和验收矩阵

### 11.1 识别测试

| 样本 | 期望项目层 | 期望运行单元 |
| --- | --- | --- |
| `xxpt/webapp` | frontend | 1 个单体前端 |
| `xxpt/webapp-lerna` | frontend aggregate | pc-student、pc-teacher、mobile-student 等 |
| `xxpt/server` | Java aggregate | 当前可识别的全部 Spring 服务 |
| `race/server` | Java aggregate | application、gateway、timing 等服务 |
| `hljzk` | Python Poetry | 1 个 Python 入口 |
| `jl` | Python Poetry | 默认 1 个入口，后续可配置更多任务 |
| `xxpt/nginx` | nginx | 1 个 nginx 运行单元 |

### 11.2 生命周期测试

每种项目至少验证：

- 列出项目和运行单元。
- 启动单个运行单元。
- 等待健康检查成功或准确返回失败原因。
- 查看该单元日志，不混入其他单元日志。
- 停止单个运行单元。
- 启动全部、部分失败和依赖阻塞时返回明确结果。
- 外部启动进程被识别时不误杀其他项目。
- 构建时只检查和停止目标项目的运行单元。

### 11.3 工作区测试

- 从 `xxpt` 创建工作区，确认组件复制边界不变。
- 工作区中运行单元的路径指向 Worktree 或共享源目录。
- 切换源目录与工作区时，所有目标运行单元先按现有规则停止。
- 合并、同步、删除规则不因新增运行单元改变。

### 11.4 GUI 测试

- 普通单体项目只有一个叶子运行单元。
- `webapp-lerna` 展开后角色可分别选中和启停。
- `server` 显示服务汇总，不把十几个服务挤成横向表格。
- 侧栏宽度可拖动、可收起、重启后保留。
- 状态刷新不重新排序、不导致选中项跳动。
- 右侧目录、日志和代码获得完整纵向空间。

## 12. 首版实现记录

2026-08-10 已完成以下实现：

- 新增 `RuntimeUnit`，所有可运行的单体项目自动生成一个默认运行单元。
- Gradle Spring Boot 多服务映射为运行单元，同时保留旧模块 ID 和 `spring_boot_modules` 兼容层。
- 支持 `package.json.workspaces`、`lerna.json` 和 `pnpm-workspace.yaml`；没有独立开发脚本的共享库不生成运行单元。
- 支持项目内 `.mini-ide/runtime.json` 和聚合组件 `runtime` 配置；项目内配置优先。
- 显式配置校验目录越界、空命令、重复 ID、无效端口、未知依赖、自依赖和依赖环。
- 多运行单元按依赖分组启动，依赖组就绪后再启动下一组；停止使用反向依赖顺序。
- 声明 `expectedPort` 的运行单元直接按端口持有者判断本轮启动就绪，不依赖 Spring 专用探测。
- 显式 `stop` 使用运行单元自己的 `cwd` 和独立 runner 执行；失败或超时只兜底停止 mini-ide 托管的进程，不按端口强杀陌生进程。
- 单子模块 Spring Boot 保留原子模块 ID；运行单元的启动、构建、测试、编译 profile 都使用所属单元的 `cwd`。
- 聚合项目页改为横向分栏：左侧两级项目树，右侧完整项目详情；侧栏宽度可拖动并持久化。
- CLI 新增 `--list-runtimes`；旧 `--list-modules` 和 `<module>` 参数继续兼容。

显式配置的实际格式如下：

```json
{
  "units": [
    {
      "id": "api",
      "name": "API",
      "kind": "python",
      "cwd": ".",
      "start": ["poetry", "run", "uvicorn", "src.api:app", "--port", "8000"],
      "stop": ["poetry", "run", "python", "scripts/stop_api.py"],
      "expectedPort": 8000,
      "healthCheckPath": "/health",
      "dependsOn": [],
      "profiles": {
        "test": ["poetry", "run", "pytest"]
      }
    }
  ]
}
```

聚合配置把同一个对象放在组件的 `runtime` 字段中。命令始终是 argv 数组，不接受 shell 字符串；`cwd` 必须位于项目根目录内。

真实目录只读验收结果：

| 路径 | 类型 | 运行单元 |
| --- | --- | --- |
| `framework-admin` | `spring-boot-gradle` | 1 个，端口 8989 |
| `app-learning` | `spring-boot-gradle` | 5 个服务 |
| `race/server` | `spring-boot-gradle` | 6 个服务，端口识别正常 |
| `xxpt/server` | `spring-boot-gradle` | 16 个服务，旧 ID 保持不变 |
| `xxpt/webapp` | `vue` | 1 个，端口 8980 |
| `xxpt/webapp-lerna` | `frontend-workspace` | `m-stu:8890`、`pc-stu:5174`、`pc-teacher:8881` |
| `hljzk`、`jl` | `python-poetry` | 各 1 个默认运行单元 |
| `race/py-certificate` | `fastapi` | 1 个，端口 8000 |
| `xxpt/nginx` | `nginx` | 1 个 Nginx 运行单元 |

兼容边界：Python 多进程和无法可靠自动推断的技术栈使用显式配置；Maven 多模块若不能自动识别，也使用同一配置，不继续在 UI 中增加特判。Nginx 和 Spring 外部 JVM 感知仍保留适配器专用实现，但 GUI、CLI 状态和日志入口已消费统一运行单元。

## 13. 开发时必须遵守的判断标准

遇到新项目类型时，先问：

1. 它是新的项目边界，还是现有项目中的新运行单元？
2. 每个运行单元能否独立启动、停止和查看日志？
3. 它是否有明确的工作目录、端口、健康检查和依赖？
4. 自动识别失败时，用户能否通过配置修正，而不是要求修改 IDE 源码？
5. GUI 和 CLI 是否只依赖通用运行单元，而没有新增类型特判？

如果答案是否定的，应先补充运行模型或配置契约，不要直接在界面里添加一次性按钮。
