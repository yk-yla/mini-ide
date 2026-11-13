"""版本专项：tag 来源、失败不打 tag、完成幂等、tag 冲突拒绝及显示一致性。"""
from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = tempfile.TemporaryDirectory(prefix="mini-ide-version-config-")
os.environ["APPDATA"] = SANDBOX.name
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import release_version as release
from src.core import app_version, cli_client, cli_server
from src.ui.main_window import MainWindow


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


class VersionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mini-ide-version-")
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.bare = base / "origin.git"
        self.work = base / "work"
        self.root = self.work / "_build"
        self.output = base / "metadata"
        self.package = base / "package"
        self.root.mkdir(parents=True)
        _git(self.bare.parent, "init", "--bare", "-b", "main", self.bare.name)
        _git(self.work, "init", "-b", "main", ".")
        _git(self.work, "config", "user.email", "smoke@example.com")
        _git(self.work, "config", "user.name", "smoke")
        (self.work / "README.md").write_text("seed", encoding="utf-8")
        _git(self.work, "add", ".")
        _git(self.work, "commit", "-m", "seed")
        _git(self.work, "tag", "-a", "v0.1.0", "-m", "seed")
        _git(self.work, "remote", "add", "origin", str(self.bare))
        _git(self.work, "push", "-q", "origin", "main", "v0.1.0")

    def complete_package(self):
        self.package.mkdir()
        for filename in ("mini-ide.exe", "mini-ide-cli.exe"):
            (self.package / filename).write_bytes(b"test-executable")
        runtime = self.package / "mini-ide-runtime"
        runtime.mkdir()
        shutil.copy2(self.output / "version.json", runtime / "version.json")

    def test_prepare_requires_remote_tags(self):
        _git(self.work, "remote", "remove", "origin")
        with self.assertRaises(ValueError):
            release.prepare(self.root, self.output)
        _git(self.work, "remote", "add", "origin", str(self.bare))
        _git(self.work, "push", "-q", "origin", "--delete", "v0.1.0")
        _git(self.work, "tag", "-d", "v0.1.0")
        with self.assertRaises(ValueError):
            release.prepare(self.root, self.output)

    def test_failed_or_incomplete_build_does_not_tag(self):
        first = release.prepare(self.root, self.output)
        second = release.prepare(self.root, self.output)
        self.assertEqual(first["version"], "0.1.1")
        self.assertEqual(first["version"], second["version"])
        with self.assertRaises(ValueError):
            release.commit(self.root, self.output, self.package)
        self.assertEqual(_git(self.work, "tag", "--list", "v0.1.1").stdout.strip(), "")

    def test_success_creates_and_pushes_tag(self):
        release.prepare(self.root, self.output)
        self.complete_package()
        self.assertEqual(release.commit(self.root, self.output, self.package), "0.1.1")
        head = _git(self.work, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(_git(self.work, "rev-parse", "v0.1.1^{commit}").stdout.strip(), head)
        self.assertIn("v0.1.1", _git(self.bare, "tag", "--list", "v0.1.1").stdout)
        self.assertEqual(release.commit(self.root, self.output, self.package), "0.1.1")
        self.assertEqual(release.prepare(self.root, self.output)["version"], "0.1.2")

    def test_newer_tag_during_packaging_blocks_commit(self):
        release.prepare(self.root, self.output)
        self.complete_package()
        _git(self.work, "tag", "-a", "v0.1.2", "-m", "newer from other machine", "HEAD")
        with self.assertRaises(ValueError):
            release.commit(self.root, self.output, self.package)

    def test_existing_tag_from_other_commit_rejected(self):
        data = release.prepare(self.root, self.output)
        self.complete_package()
        (self.work / "README.md").write_text("other machine", encoding="utf-8")
        _git(self.work, "add", ".")
        _git(self.work, "commit", "-m", "elsewhere")
        _git(self.work, "tag", "-a", f"v{data['version']}", "-m", "taken", "HEAD")
        _git(self.work, "reset", "-q", "--hard", "HEAD~1")
        with self.assertRaises(ValueError):
            release.commit(self.root, self.output, self.package)

    def test_push_failure_warns_but_keeps_local_tag(self):
        release.prepare(self.root, self.output)
        self.complete_package()
        with patch.object(release, "push_tag", return_value=(False, "network down")):
            self.assertEqual(release.commit(self.root, self.output, self.package), "0.1.1")
        self.assertIn("v0.1.1", _git(self.work, "tag", "--list", "v0.1.1").stdout)

    def test_frozen_gui_and_cli_share_bundled_version(self):
        release.prepare(self.root, self.output)
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "_MEIPASS", str(self.output), create=True):
            info = app_version.build_info()
            self.assertEqual(info["version"], "0.1.1")
            self.assertEqual(cli_server._app_version(), info["version"])
            captured = io.StringIO()
            with patch.object(sys, "stdout", captured), patch.object(cli_client, "QLocalSocket") as socket:
                self.assertEqual(cli_client.run_cli(["mini-ide-cli.exe", "--auto-start", "--version"]), 0)
                socket.assert_not_called()
            self.assertEqual(json.loads(captured.getvalue()), {"ok": True, **info})
            with patch("src.ui.main_window.QMessageBox.about") as about:
                MainWindow._about(object())
                html = about.call_args.args[2]
                self.assertIn("v0.1.1", html)
                self.assertIn(info["build_time"], html)
        self.assertIsNone(cli_client._parse_args(["mini-ide-cli.exe", "--version", "extra"]))

    def test_source_version_reads_latest_tag(self):
        self.assertEqual(app_version.source_version(self.work), "0.1.0")
        _git(self.work, "tag", "-a", "v0.1.5", "-m", "later")
        self.assertEqual(app_version.source_version(self.work), "0.1.5")
        self.assertEqual(app_version.source_version(Path(self.temp.name)), "unknown")

    def test_windows_resource_versions(self):
        data = release.prepare(self.root, self.output)
        if os.name == "nt":
            from PyInstaller.utils.win32.versioninfo import load_version_info_from_text_file
            for filename in ("mini-ide.exe", "mini-ide-cli.exe"):
                resource = load_version_info_from_text_file(str(self.output / f"{filename}.version.txt"))
                self.assertEqual(resource.ffi.fileVersionLS, 65536)
                self.assertIn(data["version"], str(resource))
                self.assertIn(filename, str(resource))
        with self.assertRaises(ValueError):
            release.next_version("invalid")


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        logging.shutdown()
        SANDBOX.cleanup()
