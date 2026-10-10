# 打包与版本

- `_build/pyproject.toml` 记录最后一次成功打包的版本，采用 `主版本.次版本.补丁号`。
- `scripts/build.bat` 每次准备下一补丁号，例如 `0.1.0 → 0.1.1 → 0.1.2`。GUI、CLI 和运行时全部生成成功后才回写版本；失败不递增。同一完成步骤重试不会再次递增。
- 随包的 `mini-ide-runtime/version.json` 固定本包版本和 UTC 构建时间。关于窗口、CLI 状态和两个 EXE 的 Windows 版本属性使用同一版本；发行包不读取外部源码的版本号。
- `mini-ide-cli.exe --version` 只读取该 CLI 本身的版本，不连接或启动 GUI；`--status` 返回正在运行的 GUI 版本及构建时间，二者用途不同。
- 正常 `scripts/build.bat` 更新根目录 GUI、CLI 和 runtime。`scripts/build.bat --package-only` 只生成 `_build/dist/release/` 完整包，不替换或退出运行中的 IDE；成功生成独立包同样递增版本。
- 安装现成独立包不重新打包，也不再递增。复制时需一起复制两个 EXE 和 runtime；正在运行的 IDE 必须先按授权正常退出后才能替换。
- 打包前执行 CLI 目标预检，交付前执行冒烟、版本专项、compileall 和 diff 检查。不要为了打包停止其它项目的服务。
