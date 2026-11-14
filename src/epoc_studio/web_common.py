"""Shared Qt WebEngine helpers for in-app 3D Brain Map and CSV Compare windows."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional

from PyQt6 import QtCore, QtGui, QtWebEngineCore, QtWebEngineWidgets, QtWidgets

from epoc_direct.paths import get_recordings_dir
from .qt_common import apply_dark_titlebar, get_app_icon


class StudioWebPage(QtWebEngineCore.QWebEnginePage):
    """Custom QWebEnginePage with native recordings-folder file dialogs & fullscreen."""

    def __init__(
        self,
        profile: Optional[QtWebEngineCore.QWebEngineProfile] = None,
        default_dir: Optional[Path] = None,
        parent=None,
    ):
        if profile is not None:
            super().__init__(profile, parent)
        else:
            super().__init__(parent)
        self._default_dir = default_dir or get_recordings_dir()

    def chooseFiles(self, mode, oldFiles, acceptedMimeTypes):
        start_dir = str(self._default_dir if self._default_dir.exists() else self._default_dir.parent)
        if oldFiles:
            first_parent = os.path.dirname(oldFiles[0])
            if first_parent and os.path.isdir(first_parent):
                start_dir = first_parent

        parent_widget = self.parent() if isinstance(self.parent(), QtWidgets.QWidget) else None
        filter_str = "CSV Files (*.csv *.txt);;All Files (*)"
        if mode == QtWebEngineCore.QWebEnginePage.FileSelectionMode.FileSelectOpenMultiple:
            files, _ = QtWidgets.QFileDialog.getOpenFileNames(
                parent_widget, "Select File(s)", start_dir, filter_str
            )
            return files
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(
            parent_widget, "Select File", start_dir, filter_str
        )
        return [file_path] if file_path else []

    def javaScriptConsoleMessage(self, level, message, lineNumber, sourceID):
        if level == QtWebEngineCore.QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
            print(f"[WebEngine JS Error] {sourceID}:{lineNumber}: {message}", flush=True)


class BaseWebWindow(QtWidgets.QWidget):
    """Frameless-flash, dark-titlebar desktop window hosting a ``QWebEngineView``."""

    closed = QtCore.pyqtSignal()

    def __init__(
        self,
        title: str,
        bg_hex: str = "#080d18",
        width: int = 1280,
        height: int = 840,
        on_close: Optional[Callable[[], None]] = None,
        parent=None,
    ):
        super().__init__(parent)
        self._on_close = on_close
        self._cleaned_up = False
        self._was_maximized = False

        self.setWindowTitle(title)
        icon = get_app_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)
        self.setStyleSheet(f"background-color: {bg_hex};")
        self.resize(width, height)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.view = QtWebEngineWidgets.QWebEngineView(self)
        self.page = StudioWebPage(parent=self.view)
        self.page.setBackgroundColor(QtGui.QColor(bg_hex))

        settings = self.page.settings()
        settings.setAttribute(QtWebEngineCore.QWebEngineSettings.WebAttribute.WebGLEnabled, True)
        # Qt 6 Widgets QWebEngineView composites 2D canvases through its backing store,
        # making Accelerated2dCanvasEnabled ~8-9x slower than software 2D rasterization on Windows.
        settings.setAttribute(
            QtWebEngineCore.QWebEngineSettings.WebAttribute.Accelerated2dCanvasEnabled, False
        )
        settings.setAttribute(
            QtWebEngineCore.QWebEngineSettings.WebAttribute.FullScreenSupportEnabled, True
        )
        settings.setAttribute(
            QtWebEngineCore.QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True
        )

        self.page.fullScreenRequested.connect(self._on_fullscreen_requested)
        self.view.setPage(self.page)
        self.setFocusProxy(self.view)
        layout.addWidget(self.view)

    def _on_fullscreen_requested(self, request: QtWebEngineCore.QWebEngineFullScreenRequest):
        request.accept()
        if request.toggleOn():
            self._was_maximized = self.isMaximized()
            self.showFullScreen()
        else:
            if self._was_maximized:
                self.showMaximized()
            else:
                self.showNormal()
            apply_dark_titlebar(self)

    def showEvent(self, event):
        super().showEvent(event)
        apply_dark_titlebar(self)

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key.Key_Escape and self.isFullScreen():
            self.page.triggerAction(QtWebEngineCore.QWebEnginePage.WebAction.ExitFullScreen)
            if self.isFullScreen():
                if self._was_maximized:
                    self.showMaximized()
                else:
                    self.showNormal()
                apply_dark_titlebar(self)
                self.page.runJavaScript("if (typeof setFullscreenUI === 'function') setFullscreenUI(false);")
            event.accept()
            return
        if event.key() == QtCore.Qt.Key.Key_F11 and not self.isFullScreen():
            self._was_maximized = self.isMaximized()
            self.showFullScreen()
            self.page.runJavaScript("if (typeof setFullscreenUI === 'function') setFullscreenUI(true);")
            event.accept()
            return
        super().keyPressEvent(event)

    def shutdown(self) -> None:
        if self._cleaned_up:
            return
        self._cleaned_up = True
        try:
            self.view.stop()
            self.view.setHtml("")
        except Exception:
            pass
        if self._on_close is not None:
            try:
                self._on_close()
            except Exception:
                pass

    def closeEvent(self, event):
        self.shutdown()
        self.closed.emit()
        super().closeEvent(event)
