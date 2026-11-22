"""用隔离的示例数据渲染运行概览与需求工作区，生成 PNG。"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import replace
import logging
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch


BUILD = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD))


def _wait(app, ready, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if ready():
            return
        time.sleep(0.02)
    raise RuntimeError("界面加载超时")


def _sample_project(root):
    from src.core.aggregate_workspace import AggregateProject, save_aggregate_project

    server = root / "server"
    (server / "gateway" / "src").mkdir(parents=True)
    for module in ("auth", "manage", "course"):
        (server / module).mkdir()
    (server / "pom.xml").write_text("<project><artifactId>server</artifactId></project>", encoding="utf-8")
    (root / "webapp" / "src").mkdir(parents=True)
    (root / "webapp" / "package.json").write_text(
        '{"name":"webapp","scripts":{"dev":"vite"},"dependencies":{"vue":"3"}}', encoding="utf-8",
    )
    (root / "nginx").mkdir()
    (root / "nginx" / "nginx.exe").touch()
    (root / "nginx" / "nginx.conf").write_text(
        "events {} http { server { listen 80; } }", encoding="utf-8",
    )
    units = [
        {"id": name, "name": name, "cwd": name, "start": ["mvn", "spring-boot:run"],
         "expectedPort": port}
        for name, port in (("gateway", 8080), ("auth", 8081), ("manage", 8082), ("course", 8083))
    ]
    project = AggregateProject.from_dict(root, {
        "schemaVersion": 1,
        "id": "xxpt",
        "name": "xxpt",
        "kind": "aggregate",
        "workspaceDirectory": "workspace",
        "components": [
            {"id": "server", "path": "server", "type": "java", "runtime": {"units": units}},
            {"id": "webapp", "path": "webapp", "type": "frontend"},
            {"id": "nginx", "path": "nginx", "type": "nginx", "shared": True},
        ],
    })
    save_aggregate_project(project)


def _capture(tmp: Path, output: Path):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from src.core.aggregate_workspace import DevelopmentWorkspace
    from src.core.aggregate_workspace_manager import DevelopmentWorkspaceSummary
    from src.core.config import AppConfig
    from src.core.search_worker_lifecycle import stop_background_workers
    from src.core.service_state import (
        STATE_RUNNING_EXTERNAL, STATE_RUNNING_MANAGED, STATE_STARTING, STATE_STOPPED,
    )
    from src.core.usage_stats import usage_stats
    from src.ui.main_window import MainWindow
    from src.ui.theme import THEME_GITHUB_DARK, THEME_GITHUB_LIGHT, apply_theme

    root = tmp / "xxpt"
    _sample_project(root)
    app = QApplication([])
    config = AppConfig(theme=THEME_GITHUB_DARK)
    apply_theme(app, config.theme)
    window = MainWindow(config)
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    window.resize(1440, 900)
    window.show()
    aggregate = window.open_aggregate_project(str(root), quiet=True)
    if aggregate is None:
        raise RuntimeError("无法加载示例聚合目录")
    try:
        _wait(app, lambda: len(aggregate.component_tabs) == 3 and aggregate._workspace_worker is None)
        server = aggregate.component_tabs["server"]
        webapp = aggregate.component_tabs["webapp"]
        nginx = aggregate.component_tabs["nginx"]
        aggregate.select_component("server")
        for tab in aggregate.component_tabs.values():
            tab._poll_start_timer.stop()
            tab._poll_timer.stop()
        _wait(app, lambda: all(tab.indexer.is_ready() for tab in aggregate.component_tabs.values()))
        server_states = [
            replace(state, state=mode, port=port)
            for state, (mode, port) in zip(
                server.service_states(refresh_external=False),
                ((STATE_RUNNING_MANAGED, 8080), (STATE_RUNNING_MANAGED, 8181),
                 (STATE_STARTING, 8082), (STATE_STOPPED, 8083)),
                strict=True,
            )
        ]
        web_state = replace(webapp.service_states(False)[0], state=STATE_RUNNING_MANAGED, port=5173)
        nginx_state = replace(nginx.service_states(False)[0], state=STATE_RUNNING_EXTERNAL, port=80)
        for command, target, duration, error in (
            ("context", "server", 42, None),
            ("list-runtimes", "server", 28, None),
            ("start", "gateway", 2320, None),
            ("health", "gateway", 137, None),
            ("health", "auth", 1508, "health_timeout"),
            ("log", "auth", 63, None),
        ):
            usage_stats.record_cli(command, target=target, duration_ms=duration, ok=not error, error_code=error)

        output.mkdir(parents=True, exist_ok=True)

        def save(filename):
            app.processEvents()
            if not window.grab().save(str(output / filename)):
                raise OSError(f"无法保存 {filename}")
            print(output / filename)

        with ExitStack() as patches:
            for tab, states in ((server, server_states), (webapp, [web_state]), (nginx, [nginx_state])):
                patches.enter_context(patch.object(tab, "service_states", return_value=states))
                tab._refresh_cockpit_services()
            for state, panel_state in zip(server_states, ("running", "running", "starting", "idle")):
                server.service_panel.update_state(state.module, panel_state, state.port)
            server.lbl_elapsed.setText("已运行 2/4")
            aggregate.refresh_project_states()
            _wait(app, lambda: server.cockpit.event_rows.count() == 6)
            save("v2-cockpit-dark.png")

            window._switch_theme(THEME_GITHUB_LIGHT)
            _wait(app, lambda: not window._theme_refresh_queue)
            save("v2-cockpit-light.png")

            window._switch_theme(THEME_GITHUB_DARK)
            _wait(app, lambda: not window._theme_refresh_queue)
            summaries = []
            for key, name, components, changes, date in (
                ("course-report", "课程报表汇总", ("server", "webapp"), 5, "2026-11-21"),
                ("auth-refresh", "统一认证刷新", ("server",), 0, "2026-11-19"),
                ("student-search", "学员筛选优化", ("webapp", "nginx"), 0, "2026-11-18"),
            ):
                workspace_path = str(root / "workspace" / key)
                workspace = DevelopmentWorkspace(
                    root_path=workspace_path, id=key, name=name, created_at=date,
                    aggregate_project_path=str(root), status="active", components=(),
                )
                summaries.append(DevelopmentWorkspaceSummary(
                    workspace_path, name, "xxpt", components,
                    tuple(f"{component}: task/{key}" for component in components),
                    changes, (), "active", date, workspace=workspace,
                ))
            aggregate._workspaces_ready(summaries, "")
            aggregate.show_workspaces()
            save("v2-workspaces-dark.png")
    finally:
        aggregate.request_close(confirm_running=False, stop_running=False, quiet=True)
        window.hide()
        stop_background_workers()
        app.processEvents()
        usage_stats.flush()
        logging.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=BUILD.parent / "output" / "mockups")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="mini-ide-preview-") as directory:
        tmp = Path(directory)
        with patch.dict(os.environ, {"APPDATA": str(tmp / "appdata")}):
            _capture(tmp, args.output.resolve())


if __name__ == "__main__":
    main()
