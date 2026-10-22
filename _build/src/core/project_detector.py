"""项目类型自动识别

打开一个目录时，返回 ProjectMeta：
- 项目类型（Spring Boot Gradle/Maven、Vue/React、Python Poetry/Django/Flask ...）
- 可执行的命令集（RunProfile）
- 默认端口、健康检查路径、主类、spring profiles 等元信息

识别顺序：先看 build.gradle/pom.xml → package.json → pyproject.toml/requirements.txt
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from src.core.runtime_units import (
    RuntimeUnit, runtime_kind_for_project, runtime_start_groups,
)


# ---------- 数据结构 ----------

@dataclass
class RunProfile:
    """一个可执行的命令档位（编译/启动/测试/清理...）"""
    name: str                       # 唯一 key，如 "compile"、"bootRun"
    label: str                      # UI 按钮显示
    command: list[str]              # argv 数组，直接传 subprocess
    kind: str = "run"               # run | compile | build | clean | test
    icon: str = ""                  # 按钮图标
    description: str = ""           # tooltip
    primary: bool = False           # 是否首选按钮（如 bootRun）
    pre_compile: bool = False       # 执行前是否需要强制编译


@dataclass
class ProjectMeta:
    path: str
    name: str
    project_type: str                # 内部标识：spring-boot-gradle / vue / django ...
    display_type: str                # 用户可见："Spring Boot (Gradle)"
    icon: str                        # emoji
    profiles: list[RunProfile] = field(default_factory=list)
    package_manager: str = ""        # gradle / maven / npm / yarn / pnpm / poetry / pip
    default_port: int | None = None
    health_check_path: str = ""
    ignored_dirs: list[str] = field(default_factory=list)
    main_class: str = ""             # Spring Boot 主类（如能识别）
    spring_profiles: list[str] = field(default_factory=list)  # application-*.yml 扫描结果
    notes: list[str] = field(default_factory=list)            # 识别过程中的提示
    # Spring Boot 多模块场景下所有带 @SpringBootApplication 的子模块：
    # [(name, abs_path, port_or_None, main_class)]。
    # ProjectTab 用来渲染左侧服务面板；≥2 个才算多模块。
    # port 从该模块自己的 application*.yml 扫出，扫不到填 None（运行时仍能启动）。
    # main_class 是 @SpringBootApplication 主类全限定名，供外部进程感知按命令行匹配。
    spring_boot_modules: list[tuple[str, str, int | None, str]] = field(default_factory=list)
    # 统一运行单元。旧字段保留用于 CLI、外部进程感知和兼容旧调用方。
    runtime_units: list[RuntimeUnit] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.runtime_units:
            return
        primary = next((profile for profile in self.profiles if profile.primary), None)
        if primary is None:
            return
        profile_names = tuple(profile.name for profile in self.profiles)
        self.runtime_units.append(RuntimeUnit(
            id=self.name,
            name=self.name,
            kind=runtime_kind_for_project(self.project_type),
            cwd=self.path,
            start_profile=primary.name,
            profile_names=profile_names,
            expected_port=self.default_port,
            health_check_path=self.health_check_path,
            source="detected",
            metadata={"project_type": self.project_type},
        ))

    @property
    def has_multiple_runtimes(self) -> bool:
        return len(self.runtime_units) >= 2

    def runtime_unit(self, unit_id: str) -> RuntimeUnit | None:
        key = (unit_id or "").casefold()
        return next((unit for unit in self.runtime_units if unit.id.casefold() == key), None)

    def runtime_for_profile(self, profile_name: str) -> RuntimeUnit | None:
        """返回拥有该 profile 的运行单元。"""
        return next(
            (unit for unit in self.runtime_units if profile_name in unit.profile_names),
            None,
        )


# ---------- 工具函数 ----------

def _read(path: Path, limit: int = 500_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:limit]
    except OSError:
        return ""


def _file_exists(root: Path, *names: str) -> Path | None:
    for n in names:
        p = root / n
        if p.exists():
            return p
    return None


def _gradle_cmd(root: Path) -> str:
    """返回可用的 gradle 调用命令（优先项目自带 wrapper）"""
    if sys.platform == "win32":
        wrapper = root / "gradlew.bat"
        if wrapper.exists():
            return str(wrapper)
        if shutil.which("gradle"):
            return "gradle"
        return "gradlew.bat"
    wrapper = root / "gradlew"
    return str(wrapper) if wrapper.exists() else "gradle"


def _mvn_cmd(root: Path) -> str:
    if sys.platform == "win32":
        wrapper = root / "mvnw.cmd"
        if wrapper.exists():
            return str(wrapper)
        return "mvn.cmd" if shutil.which("mvn.cmd") else "mvn"
    wrapper = root / "mvnw"
    return str(wrapper) if wrapper.exists() else "mvn"


def _scan_spring_profiles(root: Path) -> tuple[list[str], int | None, str]:
    """扫描 application-*.yml/properties，返回 (profiles, port, context-path)"""
    profiles: list[str] = []
    port: int | None = None
    ctx = ""
    candidates: list[Path] = []
    # resources-local / resources-product：部分项目用非标准 profile 目录区分本地/生产
    # 配置（如 timing-service），不补进来会扫不到 port，服务面板只能靠运行时反查。
    # local 优先于 product——本地开发跑的是 local 那份。
    for sub in ("src/main/resources", "src/main/resources-local",
                "src/main/resources-product", "src/main/webapp/WEB-INF", "config"):
        base = root / sub
        if base.is_dir():
            candidates.extend(base.glob("application*.yml"))
            candidates.extend(base.glob("application*.yaml"))
            candidates.extend(base.glob("application*.properties"))
    for f in candidates:
        m = re.match(r"application-(.+)\.(yml|yaml|properties)$", f.name)
        if m:
            profiles.append(m.group(1))
        content = _read(f, limit=30_000)
        if port is None:
            # Only accept the explicit Spring server port. A loose `port:` match
            # can mistake Redis/Mongo ports for the HTTP service port.
            m_port = re.search(r"^\s*server\.port\s*[:=]\s*(\d+)", content, re.MULTILINE)
            if not m_port:
                server_indent = None
                for line in content.splitlines():
                    stripped = line.strip()
                    indent = len(line) - len(line.lstrip())
                    if stripped == "server:":
                        server_indent = indent
                        continue
                    if server_indent is None or not stripped or stripped.startswith("#"):
                        continue
                    if indent <= server_indent:
                        server_indent = None
                        continue
                    m_port = re.match(r"port\s*:\s*(\d+)", stripped)
                    if m_port:
                        break
            if m_port:
                port = int(m_port.group(1))
        if not ctx:
            m_ctx = re.search(r"context-path\s*[:=]\s*(\S+)", content)
            if m_ctx:
                ctx = m_ctx.group(1).strip('"\'').rstrip("/")
    return sorted(set(profiles)), port, ctx


def _detect_spring_boot_main(root: Path) -> str:
    """扫描 java 源码找 @SpringBootApplication 主类"""
    src = root / "src" / "main" / "java"
    if not src.is_dir():
        return ""
    for java in src.rglob("*.java"):
        try:
            content = java.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "@SpringBootApplication" in content:
            pkg_m = re.search(r"^\s*package\s+([\w.]+)\s*;", content, re.MULTILINE)
            cls_m = re.search(r"public\s+class\s+(\w+)", content)
            if pkg_m and cls_m:
                return f"{pkg_m.group(1)}.{cls_m.group(1)}"
            if cls_m:
                return cls_m.group(1)
    return ""


def _parse_gradle_subprojects(root: Path) -> list[tuple[str, Path]]:
    """解析 settings.gradle(.kts) 里的 include，返回 [(module_name, module_path), ...]"""
    settings = _file_exists(root, "settings.gradle", "settings.gradle.kts")
    if not settings:
        return []
    content = _read(settings)
    modules: list[tuple[str, Path]] = []
    seen: set[str] = set()
    # 匹配 include 'a', 'b', ":c" 等
    for m in re.finditer(r"""include\s*\(?\s*([^)\n]+)\)?""", content):
        entries = m.group(1)
        for tok in re.finditer(r"""['"]([^'"]+)['"]""", entries):
            name = tok.group(1).lstrip(":").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            # 默认模块目录 = 模块名（带 : 的转成 /）
            module_dir = root / name.replace(":", "/")
            if module_dir.is_dir():
                modules.append((name, module_dir))
            else:
                # 也可能在 settings 里用 project(':x').projectDir = ... 指定了，先按常规存
                modules.append((name, module_dir))
    return modules


def _find_spring_boot_modules(root: Path, subprojects: list[tuple[str, Path]]) -> list[tuple[str, Path, str]]:
    """在子模块里找哪些有 @SpringBootApplication。返回 [(module_name, path, main_class)]"""
    hits: list[tuple[str, Path, str]] = []
    for name, path in subprojects:
        if not path.is_dir():
            continue
        main_cls = _detect_spring_boot_main(path)
        if main_cls:
            hits.append((name, path, main_cls))
    return hits


def _workspace_patterns(data: dict, lerna_data: dict | None) -> list[str]:
    """返回 Node workspace/Lerna 的 package glob，保留配置顺序。"""
    patterns: list[str] = []
    raw_workspaces = data.get("workspaces", [])
    if isinstance(raw_workspaces, list):
        patterns.extend(str(item).strip() for item in raw_workspaces if str(item).strip())
    elif isinstance(raw_workspaces, dict):
        raw_packages = raw_workspaces.get("packages", [])
        if isinstance(raw_packages, list):
            patterns.extend(str(item).strip() for item in raw_packages if str(item).strip())
    if isinstance(lerna_data, dict):
        raw_packages = lerna_data.get("packages", [])
        if isinstance(raw_packages, list):
            patterns.extend(str(item).strip() for item in raw_packages if str(item).strip())
    result: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        key = pattern.casefold()
        if key not in seen:
            seen.add(key)
            result.append(pattern)
    return result


def _pnpm_workspace_patterns(root: Path) -> list[str]:
    """轻量读取 pnpm-workspace.yaml 的 packages 列表，不额外引入 YAML 依赖。"""
    content = _read(root / "pnpm-workspace.yaml", limit=50_000)
    if not content:
        return []
    patterns: list[str] = []
    in_packages = False
    base_indent = 0
    for line in content.splitlines():
        clean = line.split("#", 1)[0].rstrip()
        if not clean.strip():
            continue
        indent = len(clean) - len(clean.lstrip())
        stripped = clean.strip()
        if stripped == "packages:":
            in_packages = True
            base_indent = indent
            continue
        if in_packages and indent <= base_indent and not stripped.startswith("-"):
            break
        if in_packages:
            match = re.match(r"-\s*['\"]?([^'\"]+)['\"]?\s*$", stripped)
            if match and match.group(1).strip():
                patterns.append(match.group(1).strip())
    return patterns


def _workspace_packages(root: Path, patterns: list[str]) -> list[tuple[str, Path, dict]]:
    """展开 workspace package，返回 (package_name, path, package.json)。"""
    result: list[tuple[str, Path, dict]] = []
    seen: set[str] = set()
    for pattern in patterns:
        # pathlib.glob 只在根目录内展开；忽略 node_modules 和隐藏目录。
        try:
            candidates = sorted(root.glob(pattern))
        except (OSError, ValueError):
            continue
        for candidate in candidates:
            if not candidate.is_dir() or candidate.name.startswith("."):
                continue
            if any(part in {"node_modules", ".git", "dist", "build"} for part in candidate.parts):
                continue
            pkg_path = candidate / "package.json"
            if not pkg_path.is_file():
                continue
            try:
                package_data = json.loads(_read(pkg_path))
            except json.JSONDecodeError:
                continue
            package_name = str(package_data.get("name") or candidate.name).strip()
            key = str(candidate.resolve()).casefold()
            if not package_name or key in seen:
                continue
            seen.add(key)
            result.append((package_name, candidate, package_data))
    return result


def _workspace_script_name(scripts: dict, action: str, package_name: str) -> str | None:
    """查找根 package.json 中针对 package 的脚本。

    约定优先支持 `dev-pc-teacher`、`build-pc-teacher`，同时兼容
    `dev:pc-teacher` 和 `dev --scope=pc-teacher` 形式。
    """
    suffix = package_name.strip().lstrip("@").replace("/", "-")
    candidates = (
        f"{action}-{suffix}",
        f"{action}:{suffix}",
        f"{suffix}:{action}",
    )
    for name in candidates:
        if name in scripts:
            return name
    for name, command in scripts.items():
        if name.startswith(f"{action}-") or name.startswith(f"{action}:"):
            if re.search(rf"(?:^|[ =])(?:--scope(?:=|\s+))?{re.escape(package_name)}(?:$|\s)", str(command)):
                return name
    return None


def _workspace_runtime_units(
    root: Path,
    data: dict,
    scripts: dict,
    pm: str,
) -> tuple[list[RuntimeUnit], list[RunProfile]]:
    """把可独立启动的 workspace package 转换为运行单元。"""
    lerna_data: dict | None = None
    lerna_path = root / "lerna.json"
    if lerna_path.is_file():
        try:
            raw = json.loads(_read(lerna_path))
            lerna_data = raw if isinstance(raw, dict) else None
        except json.JSONDecodeError:
            lerna_data = None
    patterns = _workspace_patterns(data, lerna_data)
    for pattern in _pnpm_workspace_patterns(root):
        if pattern.casefold() not in {item.casefold() for item in patterns}:
            patterns.append(pattern)
    if not patterns:
        return [], []

    packages = _workspace_packages(root, patterns)
    units: list[RuntimeUnit] = []
    profiles: list[RunProfile] = []
    command_prefix = [pm, "run"]
    for package_name, package_path, package_data in packages:
        package_scripts = package_data.get("scripts", {}) or {}
        dev_script = "dev" if "dev" in package_scripts else None
        root_dev = _workspace_script_name(scripts, "dev", package_name)
        start_command: list[str] | None = None
        start_profile = ""
        cwd = str(package_path if dev_script and not root_dev else root)
        if root_dev:
            start_profile = f"runtime:{package_name}:dev"
            start_command = [*command_prefix, root_dev]
        elif dev_script:
            start_profile = f"runtime:{package_name}:dev"
            start_command = [*command_prefix, dev_script]
        if not start_command:
            # 没有独立 dev 脚本的共享库不是运行单元，但仍可被文件树访问。
            continue

        profile_names: list[str] = []
        profiles.append(RunProfile(
            name=start_profile,
            label=f"启动 {package_name}",
            command=start_command,
            kind="run",
            icon="▶",
            description=f"{pm} run {root_dev or dev_script} ({package_name})",
        ))
        profile_names.append(start_profile)

        root_build = _workspace_script_name(scripts, "build", package_name)
        package_build = "build" if "build" in package_scripts else None
        build_name = root_build or package_build
        if build_name:
            build_profile = f"runtime:{package_name}:build"
            profiles.append(RunProfile(
                name=build_profile,
                label=f"打包 {package_name}",
                command=[*command_prefix, build_name],
                kind="build",
                icon="🔒",
                description=f"{pm} run {build_name} ({package_name})",
            ))
            profile_names.append(build_profile)

        package_port = _detect_frontend_dev_port(package_path)
        unit_id = package_name.replace("/", "-").lstrip("@")
        units.append(RuntimeUnit(
            id=unit_id,
            name=package_name,
            kind="frontend",
            cwd=cwd,
            start_profile=start_profile,
            profile_names=tuple(profile_names),
            expected_port=package_port,
            source="detected",
            metadata={
                "adapter": "workspace",
                "package_name": package_name,
                "package_path": str(package_path),
            },
        ))
    return units, profiles


# ---------- 具体识别器 ----------

def _detect_gradle(root: Path) -> ProjectMeta | None:
    build = _file_exists(root, "build.gradle", "build.gradle.kts")
    if not build:
        return None
    settings = _file_exists(root, "settings.gradle", "settings.gradle.kts")
    content = _read(build)
    settings_content = _read(settings) if settings else ""

    name = root.name
    m_name = re.search(r"rootProject\.name\s*=\s*['\"]([^'\"]+)['\"]", settings_content)
    if m_name:
        name = m_name.group(1)

    is_spring = bool(
        re.search(r"org\.springframework\.boot", content) or
        re.search(r"spring-boot-starter", content)
    )

    gcmd = _gradle_cmd(root)
    # --no-daemon：禁用 Gradle 常驻守护进程。
    # 守护进程是独立于客户端长期驻留的进程，不挂在 mini-ide 启动的进程树下；
    # bootRun 真正 fork 出的服务进程由守护进程托管，于是「停止」顺着自己启动的
    # 进程线递归杀树时漏掉它——界面立刻显示「未启动」，但服务仍活着占着端口。
    # 下次启动撞端口、残留逐轮堆积，根因即此。改为 --no-daemon 后服务直接挂在
    # mini-ide 能管到的进程树下，停止即可连根清理。代价：每次启动少了守护进程
    # 预热复用，慢几秒，开发态可接受。
    project_flag = ["-p", str(root), "--no-daemon"]
    ignored = ["build", ".gradle", "out", ".idea"]

    if is_spring:
        # 多模块识别：settings.gradle 里有 include 时，bootRun 必须指定模块，
        # 否则每个 Spring Boot 模块都会被触发（包括无 main class 的共享库会失败）。
        subprojects = _parse_gradle_subprojects(root)
        boot_modules = _find_spring_boot_modules(root, subprojects)

        # 启动模块前缀：单模块为空；多模块选第一个带 @SpringBootApplication 的模块
        run_module = ""
        main_class = _detect_spring_boot_main(root)
        notes: list[str] = []
        # 四元组：(模块名, 绝对路径, port_or_None, 主类全限定名)。
        # 主类用于外部进程感知——gradle bootRun 把 classpath 塞进 Temp jar 后，
        # 进程命令行里没有项目路径，只剩主类名，靠它才能认出本项目的服务进程。
        module_list: list[tuple[str, str, int | None, str]] = []
        runtime_units: list[RuntimeUnit] = []
        if boot_modules:
            run_module = boot_modules[0][0]
            main_class = boot_modules[0][2]
            # 每个子模块独立扫它自己的 application.yml 取 port，填到服务面板上显示
            per_module_scan = [
                (name, path, cls, _scan_spring_profiles(path))
                for name, path, cls in boot_modules
            ]
            module_list = [(name, str(path), scan[1], cls)
                           for name, path, cls, scan in per_module_scan]
            if len(boot_modules) > 1:
                names = ", ".join(m[0] for m in boot_modules)
                notes.append(f"检测到多个 Spring Boot 模块: {names}")
            # 主启动模块的 profile 和 port 用来填 ProjectMeta 的默认值
            _, port, ctx = per_module_scan[0][3]
            profiles_list = per_module_scan[0][3][0]
        else:
            profiles_list, port, ctx = _scan_spring_profiles(root)

        prefix = f":{run_module}:" if run_module else ""

        def _with_module(task: str) -> str:
            return f"{prefix}{task}" if prefix else task

        run_profiles: list[RunProfile] = [
            RunProfile(
                name="compile",
                label="编译",
                command=[gcmd, *project_flag,
                         _with_module("compileJava"), _with_module("compileTestJava"),
                         "--rerun-tasks"],
                kind="compile",
                icon="🔨",
                description="强制全量编译（含测试代码，绕过缓存）",
                primary=False,
            ),
            RunProfile(
                name="bootRun",
                label="启动",
                command=[gcmd, *project_flag, _with_module("bootRun")],
                kind="run",
                icon="▶",
                description=f"启动 Spring Boot 应用" + (f"（模块 {run_module}）" if run_module else ""),
                primary=True,
                pre_compile=True,
            ),
            RunProfile(
                name="clean",
                label="Clean",
                command=[gcmd, *project_flag, "clean"],
                kind="clean",
                icon="🧹",
                description="清理 build 目录",
            ),
        ]

        # 多模块时给其他 Spring Boot 模块各加一个备选启动按钮
        for mod_name, _mod_path, _cls in boot_modules[1:]:
            run_profiles.append(RunProfile(
                name=f"bootRun:{mod_name}",
                label=f"启动 {mod_name}",
                command=[gcmd, *project_flag, f":{mod_name}:bootRun"],
                kind="run", icon="▶",
                description=f"启动模块 {mod_name}",
                pre_compile=True,
            ))

        # 只要发现 Spring Boot 子模块就暴露 RuntimeUnit。单子模块仍使用项目级
        # runner，但保留旧的子模块 ID，避免旧 CLI/脚本出现 module not found。
        if boot_modules:
            for index, (mod_name, mod_path, main_cls, scan) in enumerate(per_module_scan):
                profile_name = "bootRun" if index == 0 else f"bootRun:{mod_name}"
                runtime_units.append(RuntimeUnit(
                    id=mod_name,
                    name=mod_name.rsplit(":", 1)[-1],
                    kind="service",
                    cwd=str(root),
                    start_profile=profile_name,
                    profile_names=("compile", profile_name, "clean"),
                    expected_port=scan[1],
                    health_check_path=f"{scan[2]}/actuator/health" if scan[2] else "/actuator/health",
                    source="detected",
                    metadata={
                        "adapter": "spring-gradle",
                        "module_path": str(mod_path),
                        "main_class": main_cls,
                    },
                ))

        return ProjectMeta(
            path=str(root),
            name=name,
            project_type="spring-boot-gradle",
            display_type="Spring Boot (Gradle)" + (" · 多模块" if boot_modules else ""),
            icon="☕",
            profiles=run_profiles,
            package_manager="gradle",
            default_port=port or 8080,
            health_check_path=f"{ctx}/actuator/health" if ctx else "/actuator/health",
            ignored_dirs=ignored,
            main_class=main_class,
            spring_profiles=profiles_list,
            notes=notes,
            spring_boot_modules=module_list,
            runtime_units=runtime_units,
        )

    # 普通 Gradle Java
    return ProjectMeta(
        path=str(root),
        name=name,
        project_type="gradle-java",
        display_type="Java (Gradle)",
        icon="☕",
        package_manager="gradle",
        ignored_dirs=ignored,
        profiles=[
            RunProfile("compile", "编译", [gcmd, *project_flag, "compileJava", "compileTestJava", "--rerun-tasks"], kind="compile", icon="🔨"),
            RunProfile("run", "启动", [gcmd, *project_flag, "run"], kind="run", icon="▶", primary=True, pre_compile=True),
            RunProfile("clean", "Clean", [gcmd, *project_flag, "clean"], kind="clean", icon="🧹"),
        ],
    )


def _detect_maven(root: Path) -> ProjectMeta | None:
    pom = root / "pom.xml"
    if not pom.exists():
        return None
    content = _read(pom)
    is_spring = bool(
        re.search(r"<artifactId>\s*spring-boot-starter-parent\s*</artifactId>", content) or
        re.search(r"<groupId>\s*org\.springframework\.boot\s*</groupId>", content)
    )
    m_name = re.search(r"<artifactId>\s*([\w\-.]+)\s*</artifactId>", content)
    name = m_name.group(1) if m_name else root.name
    mvn = _mvn_cmd(root)
    ignored = ["target", ".idea"]

    if is_spring:
        profiles_list, port, ctx = _scan_spring_profiles(root)
        main_class = _detect_spring_boot_main(root)
        return ProjectMeta(
            path=str(root),
            name=name,
            project_type="spring-boot-maven",
            display_type="Spring Boot (Maven)",
            icon="☕",
            package_manager="maven",
            default_port=port or 8080,
            health_check_path=f"{ctx}/actuator/health" if ctx else "/actuator/health",
            ignored_dirs=ignored,
            main_class=main_class,
            spring_profiles=profiles_list,
            profiles=[
                RunProfile("compile", "编译", [mvn, "-f", str(pom), "compile", "test-compile"], kind="compile", icon="🔨"),
                RunProfile("bootRun", "启动", [mvn, "-f", str(pom), "spring-boot:run"], kind="run", icon="▶", primary=True, pre_compile=True),
                RunProfile("clean", "Clean", [mvn, "-f", str(pom), "clean"], kind="clean", icon="🧹"),
            ],
        )
    return ProjectMeta(
        path=str(root),
        name=name,
        project_type="maven-java",
        display_type="Java (Maven)",
        icon="☕",
        package_manager="maven",
        ignored_dirs=ignored,
        profiles=[
            RunProfile("compile", "编译", [mvn, "-f", str(pom), "compile", "test-compile"], kind="compile", icon="🔨"),
            RunProfile("exec", "启动", [mvn, "-f", str(pom), "exec:java"], kind="run", icon="▶", primary=True, pre_compile=True),
            RunProfile("clean", "Clean", [mvn, "-f", str(pom), "clean"], kind="clean", icon="🧹"),
        ],
    )


def _detect_node(root: Path) -> ProjectMeta | None:
    pkg = root / "package.json"
    if not pkg.exists():
        return None
    try:
        data = json.loads(_read(pkg))
    except json.JSONDecodeError:
        return None
    name = data.get("name") or root.name
    deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
    scripts = data.get("scripts", {}) or {}

    has_vue = "vue" in deps
    has_react = "react" in deps
    has_next = "next" in deps
    has_nuxt = "nuxt" in deps
    has_svelte = "svelte" in deps

    # 包管理器：优先看 lockfile
    if (root / "pnpm-lock.yaml").exists():
        pm = "pnpm"
    elif (root / "yarn.lock").exists():
        pm = "yarn"
    elif (root / "bun.lockb").exists():
        pm = "bun"
    else:
        pm = "npm"
    run_word = "run" if pm in ("npm", "yarn", "bun") else "run"
    # yarn 可省略 run，但统一写上兼容性更好

    configured_port = _detect_frontend_dev_port(root)
    if has_next:
        ptype, disp, icon, port = "next", "Next.js", "▲", configured_port or 3000
    elif has_nuxt:
        ptype, disp, icon, port = "nuxt", "Nuxt", "💚", configured_port or 3000
    elif has_vue:
        ptype, disp, icon, port = "vue", "Vue", "💚", configured_port or 5173
    elif has_react:
        ptype, disp, icon, port = "react", "React", "⚛", configured_port or 3000
    elif has_svelte:
        ptype, disp, icon, port = "svelte", "Svelte", "🔥", configured_port or 5173
    else:
        ptype, disp, icon, port = "node", "Node.js", "🟨", None

    # 从 scripts 里猜 dev/build/preview
    dev_script = next((s for s in ("dev", "serve", "start") if s in scripts), None)
    build_script = "build" if "build" in scripts else None
    preview_script = next((s for s in ("preview", "start:preview") if s in scripts), None)
    test_script = "test" if "test" in scripts else None

    profiles: list[RunProfile] = []
    profiles.append(RunProfile(
        name="install",
        label="安装依赖",
        command=[pm, "install"],
        kind="compile", icon="📦",
        description=f"使用 {pm} 安装依赖",
    ))
    if dev_script:
        profiles.append(RunProfile(
            name="dev",
            label="开发启动",
            command=[pm, run_word, dev_script] if pm != "npm" else [pm, "run", dev_script],
            kind="run", icon="▶", primary=True,
            description=f"{pm} run {dev_script}",
        ))
    if build_script:
        profiles.append(RunProfile(
            name="build",
            label="打包",
            command=[pm, "run", build_script] if pm == "npm" else [pm, run_word, build_script],
            kind="build", icon="🔒",
        ))
    if preview_script:
        profiles.append(RunProfile(
            name="preview",
            label="预览",
            command=[pm, "run", preview_script] if pm == "npm" else [pm, run_word, preview_script],
            kind="run", icon="👁",
        ))
    if test_script:
        profiles.append(RunProfile(
            name="test",
            label="测试",
            command=[pm, "run", test_script] if pm == "npm" else [pm, run_word, test_script],
            kind="test", icon="🧪",
        ))

    # 任何未识别的 script 也给个"更多命令"入口
    extras = [s for s in scripts if s not in {"dev", "serve", "start", "build", "preview", "test", "start:preview"}]
    for s in extras[:10]:
        action_kind = "test" if s.split(":")[0] in {"test", "unit", "e2e", "lint", "check", "typecheck"} else "run"
        profiles.append(RunProfile(
            name=f"script:{s}",
            label=f"▸ {s}",
            command=[pm, "run", s] if pm == "npm" else [pm, run_word, s],
            kind=action_kind, icon="",
            description=scripts.get(s, ""),
        ))

    runtime_units, runtime_profiles = _workspace_runtime_units(
        root, data, scripts, pm,
    )
    profiles.extend(runtime_profiles)
    if runtime_units:
        ptype = "frontend-workspace"
        disp = "前端聚合 (Lerna)" if (root / "lerna.json").is_file() else "前端工作区"
        icon = "🧩"
        port = None

    return ProjectMeta(
        path=str(root),
        name=name,
        project_type=ptype,
        display_type=disp,
        icon=icon,
        package_manager=pm,
        default_port=port,
        ignored_dirs=["node_modules", "dist", ".nuxt", ".next", ".output", "build"],
        profiles=profiles,
        runtime_units=runtime_units,
    )


def _detect_frontend_dev_port(root: Path) -> int | None:
    """读取常见前端开发服务器配置中的端口。"""
    candidates = (
        root / "config" / "index.js",
        root / "vue.config.js",
        root / "vite.config.js",
        root / "vite.config.ts",
        root / "webpack.config.js",
        root / "webpack.config.ts",
    )
    for path in candidates:
        content = _read(path, limit=50_000)
        if not content:
            continue
        for pattern in (
            r"(?:^|\n)\s*port\s*:\s*(\d+)",
            r"\bserver\.port\s*[:=]\s*(\d+)",
            r"--port\s+(\d+)",
            r"\bPORT\s*=\s*(\d+)",
        ):
            match = re.search(pattern, content)
            if match:
                return int(match.group(1))
    return None


def _detect_python(root: Path) -> ProjectMeta | None:
    pyproject = root / "pyproject.toml"
    requirements = root / "requirements.txt"
    manage_py = root / "manage.py"

    if not (pyproject.exists() or requirements.exists() or manage_py.exists()
            or (root / "setup.py").exists() or (root / "Pipfile").exists()):
        return None

    is_poetry = False
    name = root.name
    entry_script = ""
    pyproject_text = ""
    if pyproject.exists():
        pyproject_text = _read(pyproject)
        txt = pyproject_text
        if "[tool.poetry]" in txt:
            is_poetry = True
        m_name = re.search(r'name\s*=\s*["\']([^"\']+)["\']', txt)
        if m_name:
            name = m_name.group(1)
        m_entry = re.search(r"(?:scripts|entry-points)[\s\S]*?([\w_]+)\s*=\s*[\"']([\w\.]+):([\w_]+)", txt)
        if m_entry:
            entry_script = f"{m_entry.group(2)}:{m_entry.group(3)}"

    py_cmd = "poetry run python" if is_poetry else "python"
    pip_cmd = "poetry install" if is_poetry else "pip install -r requirements.txt"
    pm = "poetry" if is_poetry else "pip"

    # 识别 Django/Flask/FastAPI（所有 Python 项目统一用 🐍 图标便于一眼分辨）
    if manage_py.exists():
        ptype, disp, icon = "django", "Django", "🐍"
        run_cmd = py_cmd.split() + ["manage.py", "runserver"]
        port = 8000
    else:
        # 入口探测目录：先根目录，根目录没有再找 src/ 子目录
        # （部分仓库习惯把入口放在 src/main.py，根目录只留配置和文档）
        # prefix 用正斜杠拼到 entry_script 上（如 "src/main.py"）；
        # 它最终作为参数传给 python，cwd 仍是项目根目录，Python 会把脚本
        # 所在目录加入 sys.path，src 下的包导入照常可用。
        search_dirs = [(root, "")]
        if (root / "src").is_dir():
            search_dirs.append((root / "src", "src/"))
        for base, prefix in search_dirs:
            for candidate in ("main.py", "app.py", "run.py", "server.py", "wsgi.py", "web.py", "start.py"):
                if (base / candidate).exists():
                    entry_script = prefix + candidate
                    break
            if entry_script:
                break
        # 候选名都没命中 → 扫这些目录顶层 .py，挑第一个含 `if __name__ == "__main__":` 的脚本
        if not entry_script:
            try:
                for base, prefix in search_dirs:
                    for p in sorted(base.glob("*.py")):
                        if 'if __name__' in _read(p, limit=20_000):
                            entry_script = prefix + p.name
                            break
                    if entry_script:
                        break
            except OSError:
                pass
        content = ""
        if entry_script and not ":" in entry_script:
            content = _read(root / entry_script, limit=20_000)
        deps_blob = ""
        if requirements.exists():
            deps_blob += _read(requirements)
        if pyproject_text:
            deps_blob += "\n" + pyproject_text
        has_fastapi = "fastapi" in content.lower() or "fastapi" in deps_blob.lower()
        has_flask = "flask" in content.lower() or "flask" in deps_blob.lower()

        if has_fastapi:
            ptype, disp, icon = "fastapi", "FastAPI", "🐍"
            self_runner = None
            for candidate in ("run.py", "main.py", "app.py", "server.py", "start.py"):
                p = root / candidate
                if p.exists() and "uvicorn.run(" in _read(p, limit=20_000):
                    self_runner = candidate
                    break
            if self_runner:
                run_cmd = py_cmd.split() + [self_runner]
            else:
                # Windows 下 uvicorn 带 --reload 会强制用 SelectorEventLoop，无法启动子进程，
                # 导致 Playwright 等需要子进程的库抛 NotImplementedError；本机工具去掉 --reload。
                run_cmd = py_cmd.split() + ["-m", "uvicorn", f"{Path(entry_script).stem}:app" if entry_script else "main:app"]
            port = 8000
        elif has_flask:
            ptype, disp, icon = "flask", "Flask", "🐍"
            run_cmd = py_cmd.split() + [entry_script or "app.py"]
            port = 5000
        else:
            ptype, disp, icon = "python-poetry" if is_poetry else "python", "Python (Poetry)" if is_poetry else "Python", "🐍"
            run_cmd = py_cmd.split() + [entry_script or "main.py"]
            port = None

    profiles = [
        RunProfile("install", "安装依赖", pip_cmd.split(), kind="compile", icon="📦"),
        RunProfile("run", "启动", run_cmd, kind="run", icon="▶", primary=True),
    ]
    if manage_py.exists():
        profiles.append(RunProfile("migrate", "迁移", py_cmd.split() + ["manage.py", "migrate"], kind="build", icon="🗃"))
        profiles.append(RunProfile("shell", "Shell", py_cmd.split() + ["manage.py", "shell"], kind="run", icon="🐚"))

    return ProjectMeta(
        path=str(root),
        name=name,
        project_type=ptype,
        display_type=disp,
        icon=icon,
        package_manager=pm,
        default_port=port,
        ignored_dirs=["__pycache__", ".venv", "venv", ".pytest_cache", ".mypy_cache", "build", "dist", "*.egg-info"],
        profiles=profiles,
    )


def _detect_go(root: Path) -> ProjectMeta | None:
    go_mod = root / "go.mod"
    if not go_mod.exists():
        return None

    name = root.name
    txt = _read(go_mod, limit=20_000)
    m = re.search(r"^\s*module\s+(\S+)", txt, re.MULTILINE)
    if m:
        # module 路径常带域名前缀（github.com/foo/bar），取最后一段当项目名
        name = m.group(1).rstrip("/").split("/")[-1] or name

    # 找 main 包所在目录：优先根目录 main.go，其次 cmd/<name>/ 约定布局
    run_target = "."
    if not (root / "main.go").exists():
        cmd_dir = root / "cmd"
        if cmd_dir.is_dir():
            for sub in sorted(p for p in cmd_dir.iterdir() if p.is_dir()):
                if (sub / "main.go").exists():
                    run_target = f"./cmd/{sub.name}"
                    break

    go_cmd = "go"
    profiles = [
        RunProfile("install", "下载依赖", [go_cmd, "mod", "download"], kind="compile", icon="📦"),
        RunProfile("run", "启动", [go_cmd, "run", run_target], kind="run", icon="▶", primary=True),
        RunProfile("build", "编译", [go_cmd, "build", "-o", "bin/" + name, run_target], kind="build", icon="🔨"),
        RunProfile("test", "测试", [go_cmd, "test", "./..."], kind="test", icon="🧪"),
    ]

    return ProjectMeta(
        path=str(root),
        name=name,
        project_type="go",
        display_type="Go",
        icon="🐹",
        package_manager="go",
        default_port=None,
        ignored_dirs=["vendor", "bin", ".git", ".idea", ".vscode"],
        profiles=profiles,
    )


def _detect_nginx(root: Path) -> ProjectMeta | None:
    exe = root / "nginx.exe"
    conf = (root / "nginx.conf") if (root / "nginx.conf").is_file() else (root / "conf" / "nginx.conf")
    if not exe.is_file() or not conf.is_file():
        return None

    return ProjectMeta(
        path=str(root),
        name=root.name,
        project_type="nginx",
        display_type="Nginx",
        icon="📦",
        package_manager="nginx",
        default_port=None,
        ignored_dirs=["logs", "temp", ".git", ".idea", ".vscode"],
        notes=["检测到 nginx.exe 和 nginx.conf"],
        profiles=[
            RunProfile(
                "start",
                "启动",
                [str(exe), "-c", str(conf)],
                kind="run",
                icon="▶",
                primary=True,
                description="启动 Nginx",
            ),
            RunProfile(
                "stop",
                "停止",
                [str(exe), "-s", "stop", "-c", str(conf)],
                kind="run",
                icon="⏹",
                description="停止 Nginx",
            ),
        ],
    )


def _runtime_command(value, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} must be a non-empty argv array")
    command = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{label}[{index}] must be a non-empty string")
        command.append(item)
    return command


def _runtime_cwd(root: Path, value, label: str) -> str:
    text = str(value or ".").strip() or "."
    candidate = (root / text).resolve()
    base = root.resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"{label} must stay inside project root") from exc
    if not candidate.is_dir():
        raise ValueError(f"{label} directory not found: {text}")
    return str(candidate)


def _apply_runtime_config(meta: ProjectMeta, config: dict | None) -> ProjectMeta:
    """用显式配置覆盖自动识别的运行单元。"""
    if not config:
        return meta
    if not isinstance(config, dict):
        raise ValueError("runtime config must be an object")
    raw_units = config.get("units")
    if raw_units is None:
        return meta
    if not isinstance(raw_units, list) or not raw_units:
        raise ValueError("runtime units must be a non-empty array")

    root = Path(meta.path)
    units: list[RuntimeUnit] = []
    used: set[str] = set()
    configured_profiles: list[RunProfile] = []
    for index, raw in enumerate(raw_units):
        if not isinstance(raw, dict):
            raise ValueError(f"runtime units[{index}] must be an object")
        unit_id = str(raw.get("id") or "").strip()
        if not unit_id:
            raise ValueError(f"runtime units[{index}].id is required")
        key = unit_id.casefold()
        if key in used:
            raise ValueError(f"duplicate runtime unit id: {unit_id}")
        used.add(key)
        name = str(raw.get("name") or unit_id).strip() or unit_id
        cwd = _runtime_cwd(root, raw.get("cwd", "."), f"runtime {unit_id} cwd")
        start = _runtime_command(raw.get("start"), f"runtime {unit_id} start")
        start_profile = f"configured:{unit_id}:start"
        configured_profiles.append(RunProfile(
            name=start_profile,
            label=f"启动 {name}",
            command=start,
            kind="run",
            icon="▶",
            description=f"显式配置的运行单元 {name}",
            primary=len(raw_units) == 1,
        ))
        profile_names = [start_profile]
        raw_profiles = raw.get("profiles", {})
        if raw_profiles is not None and not isinstance(raw_profiles, dict):
            raise ValueError(f"runtime {unit_id} profiles must be an object")
        for action, command_value in (raw_profiles or {}).items():
            action_name = str(action or "").strip()
            if not action_name:
                raise ValueError(f"runtime {unit_id} profile name is required")
            command = _runtime_command(
                command_value, f"runtime {unit_id} profile {action_name}",
            )
            profile_name = f"configured:{unit_id}:{action_name}"
            kind = action_name if action_name in {"build", "clean", "test", "compile"} else "run"
            configured_profiles.append(RunProfile(
                name=profile_name,
                label=f"{action_name} {name}",
                command=command,
                kind=kind,
                icon="",
            ))
            profile_names.append(profile_name)
        expected_port = raw.get("expectedPort")
        if expected_port is not None and (
            not isinstance(expected_port, int) or isinstance(expected_port, bool)
            or not 1 <= expected_port <= 65535
        ):
            raise ValueError(f"runtime {unit_id} expectedPort must be 1..65535")
        raw_depends = raw.get("dependsOn", [])
        if not isinstance(raw_depends, list):
            raise ValueError(f"runtime {unit_id} dependsOn must be an array")
        depends_on = tuple(str(item).strip() for item in raw_depends if str(item).strip())
        stop = raw.get("stop")
        stop_command = _runtime_command(stop, f"runtime {unit_id} stop") if stop is not None else []
        units.append(RuntimeUnit(
            id=unit_id,
            name=name,
            kind=str(raw.get("kind") or runtime_kind_for_project(meta.project_type)),
            cwd=cwd,
            start_profile=start_profile,
            profile_names=tuple(profile_names),
            expected_port=expected_port,
            health_check_path=str(raw.get("healthCheckPath") or ""),
            stop_command=tuple(stop_command),
            depends_on=depends_on,
            source="configured",
            metadata={
                "adapter": str(config.get("detector") or "configured"),
                "stop_command": stop_command,
            },
        ))

    ids = {unit.id.casefold() for unit in units}
    for unit in units:
        for dependency in unit.depends_on:
            if dependency.casefold() not in ids:
                raise ValueError(
                    f"runtime {unit.id} depends on unknown unit: {dependency}"
                )
            if dependency.casefold() == unit.id.casefold():
                raise ValueError(f"runtime {unit.id} cannot depend on itself")
    # 显式配置拥有最高优先级，不能让旧探测器留下的 primary 抢先启动。
    for profile in meta.profiles:
        profile.primary = False
    meta.profiles.extend(configured_profiles)
    meta.runtime_units = units
    # 显式运行单元覆盖 Spring 自动模块，避免外部 JVM 探测混入旧模块 ID。
    meta.spring_boot_modules = []
    runtime_start_groups(units)
    if len(units) == 1:
        meta.default_port = units[0].expected_port
        meta.health_check_path = units[0].health_check_path
    return meta


def _local_runtime_config(root: Path) -> dict:
    path = root / ".mini-ide" / "runtime.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(_read(path))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid runtime config: {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"runtime config must be an object: {path}")
    return raw


# ---------- 对外入口 ----------

def detect_project(path: str, runtime_config: dict | None = None) -> ProjectMeta:
    """按优先级探测项目类型，识别不出时返回 generic。"""
    root = Path(path)

    for detector in (_detect_gradle, _detect_maven, _detect_node, _detect_python, _detect_go, _detect_nginx):
        meta = detector(root)
        if meta:
            merged = dict(runtime_config or {})
            merged.update(_local_runtime_config(root))
            return _apply_runtime_config(meta, merged)

    meta = ProjectMeta(
        path=str(root),
        name=root.name,
        project_type="generic",
        display_type="通用目录",
        icon="📦",
        ignored_dirs=[".git", ".idea", ".vscode", "node_modules", "build", "target", "__pycache__"],
        notes=["未识别出已知的项目类型，请手动配置运行命令"],
    )
    merged = dict(runtime_config or {})
    merged.update(_local_runtime_config(root))
    return _apply_runtime_config(meta, merged)
