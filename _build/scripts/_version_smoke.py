"""版本专项：失败不递增、完成幂等、包版本独立及显示一致性。"""
from __future__ import annotations

import io
import json
import logging
import os
import shutil
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


class VersionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mini-ide-version-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "pyproject.toml"
        self.original = b'[tool.poetry]\r\nname = "mini-ide"\r\nversion = "0.1.0"\r\n\r\n[other]\r\nversion = "keep"\r\n'
        self.source.write_bytes(self.original)
        self.output = self.root / "metadata"
        self.package = self.root / "package"

    def complete_package(self):
        self.package.mkdir()
        for filename in ("mini-ide.exe", "mini-ide-cli.exe"):
            (self.package / filename).write_bytes(b"test-executable")
        runtime = self.package / "mini-ide-runtime"
        runtime.mkdir()
        shutil.copy2(self.output / "version.json", runtime / "version.json")

    def test_failed_or_incomplete_build_does_not_increment(self):
        first = release.prepare(self.root, self.output)
        second = release.prepare(self.root, self.output)
        self.assertEqual(first["version"], "0.1.1")
        self.assertEqual(first["version"], second["version"])
        self.assertEqual(self.source.read_bytes(), self.original)
        with self.assertRaises(ValueError):
            release.commit(self.root, self.output, self.package)
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_success_increments_once_and_preserves_other_config(self):
        release.prepare(self.root, self.output)
        self.complete_package()
        self.assertEqual(release.commit(self.root, self.output, self.package), "0.1.1")
        self.assertEqual(release.commit(self.root, self.output, self.package), "0.1.1")
        self.assertEqual(self.source.read_bytes(), self.original.replace(b'"0.1.0"', b'"0.1.1"'))
        self.assertEqual(release.prepare(self.root, self.output)["version"], "0.1.2")

    def test_metadata_mismatch_or_source_drift_blocks_commit(self):
        release.prepare(self.root, self.output)
        self.complete_package()
        bundled = self.package / "mini-ide-runtime" / "version.json"
        saved = bundled.read_bytes()
        bundled.write_text('{"version":"9.9.9"}', encoding="utf-8")
        with self.assertRaises(ValueError):
            release.commit(self.root, self.output, self.package)
        bundled.write_bytes(saved)
        self.source.write_bytes(self.original.replace(b'"0.1.0"', b'"0.2.0"'))
        with self.assertRaises(ValueError):
            release.commit(self.root, self.output, self.package)

    def test_write_failure_preserves_source(self):
        release.prepare(self.root, self.output)
        self.complete_package()
        with patch.object(release.os, "replace", side_effect=OSError("simulated disk failure")):
            with self.assertRaises(OSError):
                release.commit(self.root, self.output, self.package)
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertFalse(list(self.root.glob(".version-*.tmp")))

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
