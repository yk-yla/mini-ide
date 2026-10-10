"""打包时准备下一版本，全部产物生成成功后才更新源码版本。"""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path


def read_version(root: Path) -> str:
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["tool"]["poetry"]["version"])


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
    previous = read_version(root)
    version = next_version(previous)
    data = {"previous_version": previous, "version": version,
            "build_time": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    output.mkdir(parents=True, exist_ok=True)
    (output / "version.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for filename in ("mini-ide.exe", "mini-ide-cli.exe"):
        (output / f"{filename}.version.txt").write_text(version_resource(version, filename), encoding="utf-8")
    return data


def commit(root: Path, output: Path, release: Path) -> str:
    data = json.loads((output / "version.json").read_text(encoding="utf-8"))
    version, previous = data["version"], data["previous_version"]
    if next_version(previous) != version:
        raise ValueError("invalid prepared version")
    # 不允许把缺件或不同代次的目录记成已成功打包。
    for filename in ("mini-ide.exe", "mini-ide-cli.exe"):
        if not (release / filename).is_file() or not (release / filename).stat().st_size:
            raise ValueError(f"missing packaged executable: {filename}")
    bundled = release / "mini-ide-runtime" / "version.json"
    if json.loads(bundled.read_text(encoding="utf-8")) != data:
        raise ValueError("packaged version does not match prepared version")
    path = root / "pyproject.toml"
    current = read_version(root)
    if current == version:
        return version  # 同一完成步骤重试不重复加号。
    if current != previous:
        raise ValueError("source version changed during packaging")
    original = path.read_bytes().decode("utf-8")
    section = re.search(r"(?ms)^\[tool\.poetry\]\r?\n.*?(?=^\[|\Z)", original)
    if section is None:
        raise ValueError("missing tool.poetry section")
    changed, count = re.subn(r'(?m)^(version\s*=\s*)[\'"]([^\'"\r\n]+)[\'"]',
                            lambda match: match.group(1) + '"' + version + '"', section.group(), count=1)
    if count != 1:
        raise ValueError("missing source version")
    updated = original[:section.start()] + changed + original[section.end():]
    # 原子替换，保留 CRLF 和其余配置；写入失败不截断已有 pyproject。
    fd, temporary = tempfile.mkstemp(prefix=".version-", suffix=".tmp", dir=root)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(updated.encode("utf-8"))
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
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
        print(f"[build] Version {data['previous_version']} -> {data['version']}", flush=True)
    else:
        if args.release is None:
            parser.error("commit requires --release")
        print(f"[build] Packaged version: {commit(args.root, args.output, args.release)}", flush=True)


if __name__ == "__main__":
    main()
