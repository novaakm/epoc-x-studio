"""Backwards-compatibility shim forwarding ``epoc_studio`` imports to ``presteeg``."""
from __future__ import annotations

from presteeg import brain_map_view, csv_compare_view, headmap_view, launcher, live_viewer_view, lsl_brain_server, qt_common

__all__ = [
    "qt_common",
    "headmap_view",
    "live_viewer_view",
    "brain_map_view",
    "lsl_brain_server",
    "csv_compare_view",
    "launcher",
]
