"""Route every Qt quit request through the same save and task-completion gate."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QApplication
from src.core.search_worker_lifecycle import active_background_workers


class ApplicationQuitGuard(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        QApplication.instance().installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:
        if watched is QApplication.instance() and event.type() == QEvent.Type.Quit:
            if not self.window._quit_prepared:
                self.window._request_quit()
                return True
            if active_background_workers():
                self.window.finish_application_quit()
                return True
        return super().eventFilter(watched, event)
