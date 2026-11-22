"""主题感知的单色界面图标。"""
from functools import lru_cache

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from src.ui.theme import FG_SECONDARY


_SHAPES = {
    "play": '<path d="m8 5 12 7-12 7Z"/>',
    "stop": '<rect x="5" y="5" width="14" height="14" rx="2"/>',
    "restart": '<path d="M3 12a9 9 0 1 1 3 6.7M3 5v7h7"/>',
    "clear": '<path d="M3 6h18M9 6V3h6v3M5 6l1 14h12l1-14M10 10v6m4-6v6"/>',
    "layers": '<path d="m12 3 9 5-9 5-9-5 9-5Zm-9 9 9 5 9-5M3 16l9 5 9-5"/>',
    "terminal": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 8 4 4-4 4m7 0h3"/>',
    "search": '<circle cx="10.5" cy="10.5" r="6"/><path d="m15 15 5 5"/>',
    "git_branch": '<path d="M6 8v8m0 0c1-6 12-2 12-8"/><circle cx="6" cy="6" r="2"/><circle cx="6" cy="18" r="2"/><circle cx="18" cy="6" r="2"/>',
    "git_diff": '<path d="M7 3h7l5 5v13H5V3h2Zm7 0v5h5M8 13h8m-4-3v6m-4 2h8"/>',
    "lock": '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 1 1 8 0v3m-4 5v2"/>',
    "edit": '<path d="m14 6 4 4M4 20l4-1 12-12-4-4L4 15l-1 5Z"/>',
    "external_link": '<path d="M13 5h6v6m0-6-8 8m6 0v6H5V7h6"/>',
    "copy": '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
    "refresh": '<path d="M21 12a9 9 0 1 1-2.64-6.36L21 8"/><path d="M21 3v5h-5"/>',
    "globe": '<circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18"/>',
    "code": '<path d="m8 6-6 6 6 6m8-12 6 6-6 6m-3-14-2 16"/>',
    "coffee": '<path d="M4 9h14v5a5 5 0 0 1-5 5H9a5 5 0 0 1-5-5V9Zm14 1h1a3 3 0 1 1 0 6h-1M3 21h17M8 3v3m5-3v3"/>',
    "folder": '<path d="M3 6a2 2 0 0 1 2-2h4l2 3h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/>',
    "folder_open": '<path d="M3 19V6a2 2 0 0 1 2-2h4l2 3h7a2 2 0 0 1 2 2v1m-17 9 3-9h15l-3 9Z"/>',
    "file": '<path d="M6 3h8l5 5v13H6Zm8 0v5h5"/>',
    "activity": '<path d="M3 12h4l3-7 4 14 3-7h4"/>',
    "arrow_left": '<path d="m12 5-7 7 7 7M5 12h15"/>',
    "chevron_right": '<path d="m9 6 6 6-6 6"/>',
    "dashboard": '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
}

_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
    'fill="none" stroke="currentColor" stroke-width="1.75" '
    'stroke-linecap="round" stroke-linejoin="round">{shape}</svg>'
)


def icon(name: str, color=FG_SECONDARY, size: int = 16) -> QIcon:
    return _render_icon(name, str(color), size)


@lru_cache(maxsize=256)
def _render_icon(name: str, color: str, size: int) -> QIcon:
    renderer = QSvgRenderer(QByteArray(
        _SVG.format(shape=_SHAPES[name]).replace("currentColor", color).encode("utf-8")
    ))
    result = QIcon()
    for ratio in (1, 2):
        pixmap = QPixmap(size * ratio, size * ratio)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        pixmap.setDevicePixelRatio(ratio)
        result.addPixmap(pixmap)
    return result


def project_icon_name(project_type: str) -> str:
    if any(kind in project_type for kind in ("spring", "java")):
        return "coffee"
    if project_type in {"nginx", "node", "vue", "react", "frontend-workspace"}:
        return "globe"
    return "folder" if project_type == "generic" else "code"
