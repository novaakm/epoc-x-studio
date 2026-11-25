"""Native desktop CSV Compare window powered by Qt WebEngine."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Callable, Optional

from PyQt6 import QtCore

from .qt_common import apply_dark_titlebar, asset_path, ensure_qt_app, install_sigint_handler
from .web_common import BaseWebWindow


def create_csv_compare_window(
    file_a: Optional[str] = None,
    file_b: Optional[str] = None,
    on_close: Optional[Callable[[], None]] = None,
    parent=None,
) -> BaseWebWindow:
    """Open ``simple-viewer.html`` inside a native dark-themed desktop window."""
    win = BaseWebWindow(
        title="prestEEG — CSV Compare",
        bg_hex="#080d18",
        width=1280,
        height=860,
        on_close=on_close,
        parent=parent,
    )

    pending: list[tuple[str, str]] = []
    if file_a:
        pending.append(("fileA", file_a))
    if file_b:
        pending.append(("fileB", file_b))

    def _on_load_finished(ok: bool):
        if not ok or not pending:
            return
        for which, path_str in pending:
            p = Path(path_str)
            if not p.is_file():
                continue
            file_url = QtCore.QUrl.fromLocalFile(str(p.resolve())).toString()
            js = f"window.loadCSVUrl({json.dumps(which)}, {json.dumps(file_url)}, {json.dumps(p.name)});"
            win.page.runJavaScript(js)
        pending.clear()

    win.view.loadFinished.connect(_on_load_finished)
    html_path = asset_path("simple-viewer.html")
    win.view.load(QtCore.QUrl.fromLocalFile(str(html_path)))
    return win


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="presteeg-csv-compare",
        description="Compare two EEG CSV recordings side-by-side inside a desktop window.",
    )
    ap.add_argument("file_a", nargs="?", help="Optional path to File A CSV")
    ap.add_argument("file_b", nargs="?", help="Optional path to File B CSV")
    args = ap.parse_args(argv)

    for label, path in (("File A", args.file_a), ("File B", args.file_b)):
        if path and not os.path.isfile(path):
            print(f"error: {label} not found: {path}", file=sys.stderr)
            return 1

    app = ensure_qt_app(sys.argv)
    win = create_csv_compare_window(file_a=args.file_a, file_b=args.file_b)
    install_sigint_handler(win, lambda: (win.shutdown(), app.quit()))
    win.show()
    apply_dark_titlebar(win)
    try:
        return app.exec()
    finally:
        win.shutdown()
