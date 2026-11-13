# 打包与版本

- 版本号以 git tag 为准，格式 `v主版本.次版本.补丁号`（例如 `v0.1.11`）；`_build/pyproject.toml` 不再记录版本。
- `scripts/build.bat` 开始时自动 `git fetch --tags`，取最大的 `vX.Y.Z` tag 准备下一补丁号（例如 `v0.1.11 → 0.1.12`）。取不到远程 tag 时中止打包，避免多台机器各自递增造成版本号分叉。
- GUI、CLI 和运行时全部生成成功后，才在当前 HEAD 打附注 tag 并推送到远程；推送失败会输出警告并提示手动 `git push origin vX.Y.Z`。失败不打 tag、不递增。同一完成步骤重试不会重复打 tag；打包期间出现新 tag（包括另一台机器先发布）会拒绝并提示重新打包。
- 工作区有未提交改动时仍可打包，但 tag 指向 HEAD，发布的包可能包含未提交内容，打包日志会给出警告。
- 随包的 `mini-ide-runtime/version.json` 固定本包版本和 UTC 构建时间。关于窗口、CLI 状态和两个 EXE 的 Windows 版本属性使用同一版本；发行包不读取外部源码的版本号。源码态（直接运行 `main.py`）显示仓库最新的 `vX.Y.Z` tag，git 不可用时显示 `unknown`。
- `mini-ide-cli.exe --version` 只读取该 CLI 本身的版本，不连接或启动 GUI；`--status` 返回正在运行的 GUI 版本及构建时间，二者用途不同。
- 正常 `scripts/build.bat` 更新根目录 GUI、CLI 和 runtime。`scripts/build.bat --package-only` 只生成 `_build/dist/release/` 完整包，不替换或退出运行中的 IDE；成功生成独立包同样打 tag 并递增。
- 安装现成独立包不重新打包，也不再递增。复制时需一起复制两个 EXE 和 runtime；正在运行的 IDE 必须先按授权正常退出后才能替换。
- 打包前执行 CLI 目标预检，交付前执行冒烟、版本专项、compileall 和 diff 检查。不要为了打包停止其它项目的服务。
