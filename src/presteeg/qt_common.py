"""Shared Qt helpers, Windows taskbar/shortcut integration, and asset resolution."""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

from .paths import get_user_data_dir

APP_ID = "prestEEG.Desktop"
APP_DISPLAY_NAME = "prestEEG"

_ASSETS_DIR = Path(__file__).resolve().parent / "assets"
_APP_ID_INITIALIZED = False


def asset_path(*parts: str) -> Path:
    """Return the path to a static asset bundled inside ``presteeg/assets``."""
    return _ASSETS_DIR.joinpath(*parts)


def init_windows_app_id() -> None:
    """Register an explicit AppUserModelID so Windows groups prestEEG windows cleanly."""
    global _APP_ID_INITIALIZED
    if _APP_ID_INITIALIZED or sys.platform != "win32":
        return
    _APP_ID_INITIALIZED = True
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass


def get_app_icon():
    from PyQt6 import QtGui

    ico = asset_path("presteeg.ico")
    if ico.is_file():
        icon = QtGui.QIcon(str(ico))
        if not icon.isNull():
            return icon
    png = asset_path("presteeg.png")
    if png.is_file():
        return QtGui.QIcon(str(png))
    return QtGui.QIcon()


_DEVNULL_STREAMS: list = []


def ensure_stdio() -> None:
    """Ensure ``sys.stdout`` and ``sys.stderr`` are writable under GUI subsystems (e.g. ``pythonw.exe``)."""
    for attr in ("stdout", "stderr"):
        if getattr(sys, attr, None) is None:
            stream = open(os.devnull, "w", encoding="utf-8", errors="replace")
            _DEVNULL_STREAMS.append(stream)
            setattr(sys, attr, stream)


def ensure_qt_app(argv: Optional[list[str]] = None):
    """Initialize (or reuse) the process-wide ``QApplication`` with consistent branding."""
    from PyQt6 import QtCore, QtWidgets

    ensure_stdio()
    init_windows_app_id()
    app = QtWidgets.QApplication.instance()
    if app is None:
        QtCore.QCoreApplication.setAttribute(QtCore.Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
        app = QtWidgets.QApplication(sys.argv if argv is None else argv)
    app.setStyle("Fusion")
    app.setApplicationName(APP_DISPLAY_NAME)
    app.setApplicationDisplayName(APP_DISPLAY_NAME)
    icon = get_app_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)
    return app


def apply_dark_titlebar(widget) -> None:
    """Apply immersive dark title bar on Windows 10/11."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        hwnd = int(widget.winId())
        dwmapi = ctypes.windll.dwmapi
        val = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE
            dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val))
    except Exception:
        pass


def set_always_on_top(widget, on: bool) -> None:
    from PyQt6 import QtCore

    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            hwnd = int(widget.winId())
            HWND_TOPMOST = -1
            HWND_NOTOPMOST = -2
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOACTIVATE = 0x0010
            set_window_pos = ctypes.windll.user32.SetWindowPos
            set_window_pos.argtypes = [
                wintypes.HWND,
                wintypes.HWND,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint,
            ]
            set_window_pos.restype = wintypes.BOOL
            if set_window_pos(
                hwnd,
                HWND_TOPMOST if on else HWND_NOTOPMOST,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
            ):
                return
        except Exception:
            pass
    pos = widget.pos()
    widget.setWindowFlag(QtCore.Qt.WindowType.WindowStaysOnTopHint, on)
    widget.move(pos)
    widget.show()
    apply_dark_titlebar(widget)


def open_broker_stream(addr: str):
    """Connect to a local prestEEG TCP CSV broker and return ``(socket, text_stream)``."""
    if ":" in addr:
        host, port_s = addr.rsplit(":", 1)
        host = host or "127.0.0.1"
        port = int(port_s)
    else:
        host, port = "127.0.0.1", int(addr)
    sock = socket.create_connection((host, port), timeout=5.0)
    sock.settimeout(None)
    return sock, sock.makefile("r", encoding="utf-8", errors="replace")


def install_sigint_handler(widget, on_quit: Callable[[], None]) -> None:
    """Allow Ctrl-C in a terminal to cleanly close a standalone Qt window."""
    from PyQt6 import QtCore

    def _sigint(_sig, _frm):
        on_quit()

    signal.signal(signal.SIGINT, _sigint)
    try:
        signal.signal(signal.SIGTERM, _sigint)
    except Exception:
        pass

    timer = QtCore.QTimer(widget)
    timer.timeout.connect(lambda: None)
    timer.start(100)


def _find_app_launch_target() -> tuple[str, str]:
    """Return ``(executable, arguments)`` for launching prestEEG GUI cleanly."""
    py_exe = Path(sys.executable)
    scripts_dir = py_exe.parent
    app_exe = scripts_dir / "presteeg.exe"
    if app_exe.is_file():
        return str(app_exe), ""
    pythonw = scripts_dir / "pythonw.exe"
    if pythonw.is_file():
        return str(pythonw), "-m presteeg"
    return str(py_exe), "-m presteeg"


def install_windows_shortcuts(desktop: bool = True) -> dict[str, Path]:
    """Create Windows Start Menu (and optional Desktop) shortcuts for prestEEG."""
    if sys.platform != "win32":
        raise RuntimeError("Windows shortcuts are only supported on Windows.")

    target_exe, target_args = _find_app_launch_target()
    icon_file = str(asset_path("presteeg.ico"))
    work_dir = str(get_user_data_dir())
    os.makedirs(work_dir, exist_ok=True)

    start_menu_dir = (
        Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / APP_DISPLAY_NAME
    )
    start_menu_dir.mkdir(parents=True, exist_ok=True)

    destinations: dict[str, Path] = {
        "start_menu": start_menu_dir / f"{APP_DISPLAY_NAME}.lnk",
    }
    if desktop:
        desk_dir = Path.home() / "Desktop"
        if desk_dir.is_dir():
            destinations["desktop"] = desk_dir / f"{APP_DISPLAY_NAME}.lnk"

    def _ps_quote(s: str) -> str:
        return "'" + s.replace("'", "''") + "'"

    ps_lines = ["$ws = New-Object -ComObject WScript.Shell"]
    for lnk_path in destinations.values():
        ps_lines.extend(
            [
                f"$s = $ws.CreateShortcut({_ps_quote(str(lnk_path))})",
                f"$s.TargetPath = {_ps_quote(target_exe)}",
                f"$s.Arguments = {_ps_quote(target_args)}",
                f"$s.WorkingDirectory = {_ps_quote(work_dir)}",
                f"$s.IconLocation = {_ps_quote(icon_file + ',0')}",
                f"$s.Description = {_ps_quote('prestEEG - EEG Stream Hub & Visualizations')}",
                "$s.Save()",
            ]
        )

    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", "; ".join(ps_lines)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return destinations
