"""打包版本以 git tag 为准：先取远程最新 vX.Y.Z tag 递增，产物齐全才打 tag 并推送。"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
GIT_TIMEOUT = 120
PUSH_ATTEMPTS = 3


def _run_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=GIT_TIMEOUT,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
        raise ValueError(f"git {' '.join(args)} 失败: {detail}")
    return result


def latest_version_tag(repo: Path) -> str | None:
    lines = _run_git(repo, "tag", "--list", "v*", "--sort=-v:refname").stdout.splitlines()
    for line in lines:
        if TAG_RE.fullmatch(line.strip()):
            return line.strip()
    return None


def next_version(version: str) -> str:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError(f"expected major.minor.patch, got {version!r}")
    major, minor, patch = map(int, version.split("."))
    numbers = (major, minor, patch + 1)
    if any(n > 65535 for n in numbers):
        raise ValueError("version exceeds Windows version resource limit")
    return ".".join(map(str, numbers))


def version_resource(version: str, filename: str) -> str:
    numbers = tuple(map(int, version.split("."))) + (0,)
    strings = {
        "FileDescription": "mini-ide" if filename == "mini-ide.exe" else "mini-ide CLI",
        "FileVersion": version,
        "InternalName": filename.removesuffix(".exe"),
        "OriginalFilename": filename,
        "ProductName": "mini-ide",
        "ProductVersion": version,
    }
    entries = ",\n".join(f"        StringStruct({key!r}, {value!r})" for key, value in strings.items())
    return (
        "VSVersionInfo(\n"
        f"  ffi=FixedFileInfo(filevers={numbers!r}, prodvers={numbers!r}, "
        "mask=63, flags=0, OS=262148, fileType=1, subtype=0, date=(0, 0)),\n"
        "  kids=[StringFileInfo([StringTable('040904B0', [\n"
        + entries + "\n  ])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])])\n"
    )


def prepare(root: Path, output: Path) -> dict:
    repo = root.parent
    fetch = _run_git(repo, "fetch", "origin", "--tags", "--force", check=False)
    if fetch.returncode != 0:
        detail = (fetch.stderr or fetch.stdout).strip()
        raise ValueError(
            "无法获取远程 tag，为避免多台机器版本号分叉已中止打包，请检查网络后重试。"
            f"git fetch 输出: {detail}"
        )
    latest = latest_version_tag(repo)
    if latest is None:
        raise ValueError("仓库还没有 vX.Y.Z 格式的 tag，请先打一个基线 tag 再打包")
    status = _run_git(repo, "status", "--porcelain", check=False).stdout
    if status.strip():
        print("[build] WARNING: 工作区有未提交改动，tag 将指向 HEAD，发布的包可能包含未提交内容", flush=True)
    version = next_version(latest.removeprefix("v"))
    data = {"previous_tag": latest, "version": version,
            "build_time": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    output.mkdir(parents=True, exist_ok=True)
    (output / "version.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for filename in ("mini-ide.exe", "mini-ide-cli.exe"):
        (output / f"{filename}.version.txt").write_text(version_resource(version, filename), encoding="utf-8")
    return data


def push_tag(repo: Path, tag_name: str) -> tuple[bool, str]:
    last_error = ""
    for _ in range(PUSH_ATTEMPTS):
        result = _run_git(repo, "push", "origin", tag_name, check=False)
        if result.returncode == 0:
            return True, ""
        last_error = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
    return False, last_error


def commit(root: Path, output: Path, release: Path) -> str:
    repo = root.parent
    data = json.loads((output / "version.json").read_text(encoding="utf-8"))
    version = str(data.get("version", ""))
    if TAG_RE.fullmatch(f"v{version}") is None:
        raise ValueError(f"invalid prepared version: {version!r}")
    # 不允许把缺件或不同代次的目录记成已成功打包。
    for filename in ("mini-ide.exe", "mini-ide-cli.exe"):
        if not (release / filename).is_file() or not (release / filename).stat().st_size:
            raise ValueError(f"missing packaged executable: {filename}")
    bundled = release / "mini-ide-runtime" / "version.json"
    if json.loads(bundled.read_text(encoding="utf-8")) != data:
        raise ValueError("packaged version does not match prepared version")
    tag_name = f"v{version}"
    existing = _run_git(repo, "rev-parse", "--verify", f"{tag_name}^{{commit}}", check=False)
    if existing.returncode == 0:
        head = _run_git(repo, "rev-parse", "HEAD").stdout.strip()
        if existing.stdout.strip() == head:
            return version  # 同一完成步骤重试不重复打 tag。
        raise ValueError(f"tag {tag_name} 已存在且指向其他提交（可能另一台机器先发布），中止")
    # 打包期间（包括另一台机器）出现了新 tag 就重打，防止版本号被占用。
    if latest_version_tag(repo) != data.get("previous_tag"):
        raise ValueError("打包期间仓库出现了新的版本 tag，请重新打包")
    _run_git(repo, "tag", "-a", tag_name, "-m", f"mini-ide {tag_name}", "HEAD")
    pushed, detail = push_tag(repo, tag_name)
    if not pushed:
        print(
            f"[build] WARNING: tag 推送失败（{detail}）。"
            f"请手动执行 git push origin {tag_name}，否则其他机器打包会重复这个版本号",
            flush=True,
        )
    return version


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "commit"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--release", type=Path)
    args = parser.parse_args()
    if args.action == "prepare":
        data = prepare(args.root, args.output)
        print(f"[build] Version {data['previous_tag']} -> {data['version']}", flush=True)
    else:
        if args.release is None:
            parser.error("commit requires --release")
        print(f"[build] Packaged version: {commit(args.root, args.output, args.release)}", flush=True)


if __name__ == "__main__":
    main()
