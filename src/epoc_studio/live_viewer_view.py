"""Live Waveforms viewer (PyQtGraph) usable inside EPOC Studio or standalone."""
from __future__ import annotations

import argparse
import bisect
import collections
import csv
import datetime
import fnmatch
import math
import os
import re
import sys
import threading
import time
import warnings
from typing import Callable, Optional

import numpy as np
from PyQt6 import QtCore, QtGui, QtWidgets

from .qt_common import (
    apply_dark_titlebar,
    ensure_qt_app,
    get_app_icon,
    install_sigint_handler,
    open_broker_stream,
)

warnings.filterwarnings("ignore", message="All-NaN slice encountered")
warnings.filterwarnings("ignore", message="Mean of empty slice")

MAX_POINTS = 500_000
REDRAW_INTERVAL_MS = 33
X_TICK_MS = 1000

PALETTE = [
    (56, 189, 248),
    (34, 197, 94),
    (250, 204, 21),
    (168, 85, 247),
    (251, 113, 133),
    (251, 146, 60),
    (45, 212, 191),
    (129, 140, 248),
    (163, 230, 53),
    (244, 114, 182),
    (96, 165, 250),
    (226, 232, 240),
]


def parse_duration(text):
    """Parse a duration like '500ms', '3s', '2m', '1h' into milliseconds."""
    if text is None:
        return None
    t = text.strip().lower()
    m = re.fullmatch(r"([0-9]*\.?[0-9]+)\s*(ms|s|m|h)?", t)
    if not m:
        raise argparse.ArgumentTypeError("invalid duration: %r" % text)
    val = float(m.group(1))
    unit = m.group(2) or "s"
    factor = {"ms": 1.0, "s": 1000.0, "m": 60_000.0, "h": 3_600_000.0}[unit]
    return val * factor


def looks_like_number(tok):
    if tok is None:
        return None
    tok = tok.strip()
    if tok == "":
        return None
    try:
        float(tok)
        return True
    except ValueError:
        return False


def looks_like_iso(tok):
    if not tok:
        return False
    t = tok.strip()
    return ("T" in t and ":" in t and any(c.isdigit() for c in t)) or t.endswith("Z")


def is_header_row(fields):
    has_any = False
    for f in fields:
        s = f.strip()
        if not s:
            continue
        has_any = True
        if looks_like_number(s) or looks_like_iso(s):
            return False
    return has_any


class Structure:
    """Holds the guessed layout of the CSV stream."""

    def __init__(self, ncols, header=None):
        self.ncols = ncols
        self.header = header
        self.ts_col = None
        self.ts_is_iso = False
        self.channel_cols = []
        self.numeric_cols = []
        self.col_kind = ["?"] * ncols

    def column_name(self, idx):
        if self.header and idx < len(self.header) and self.header[idx].strip():
            return self.header[idx].strip()
        return "col%d" % (idx + 1)

    def describe(self):
        lines = ["Guessed CSV structure (%d columns):" % self.ncols]
        for i in range(self.ncols):
            lines.append("  [%2d] %-16s %s" % (i + 1, self.column_name(i), self.col_kind[i]))
        return "\n".join(lines)


def guess_structure(header, sample_rows):
    ncols = 0
    if header:
        ncols = len(header)
    for r in sample_rows:
        ncols = max(ncols, len(r))
    st = Structure(ncols, header)

    numeric_votes = [0] * ncols
    string_votes = [0] * ncols
    iso_votes = [0] * ncols
    seen = [0] * ncols
    col_values = [[] for _ in range(ncols)]

    for r in sample_rows:
        for i in range(ncols):
            tok = r[i] if i < len(r) else ""
            v = looks_like_number(tok)
            if v is None:
                continue
            seen[i] += 1
            if v:
                numeric_votes[i] += 1
                try:
                    col_values[i].append(float(tok))
                except ValueError:
                    pass
            else:
                string_votes[i] += 1
                if looks_like_iso(tok):
                    iso_votes[i] += 1

    def non_decreasing(vals):
        return all(vals[k] <= vals[k + 1] for k in range(len(vals) - 1))

    ts_candidate = None
    for i in range(ncols):
        if seen[i] == 0:
            continue
        if iso_votes[i] > 0 and iso_votes[i] == string_votes[i]:
            vals = [r[i] for r in sample_rows if i < len(r) and r[i].strip()]
            if len(vals) >= 2 and all(vals[k] <= vals[k + 1] for k in range(len(vals) - 1)):
                ts_candidate = (i, True)
                break
        if numeric_votes[i] > 0 and string_votes[i] == 0 and col_values[i]:
            v = col_values[i]
            if len(v) >= 2 and non_decreasing(v) and max(abs(x) for x in v) > 1e6:
                ts_candidate = (i, False)
                break

    if ts_candidate is None and header:
        for i, h in enumerate(header):
            hl = h.strip().lower()
            if hl in ("timestamp_ns", "timestamp", "time_ns"):
                ts_candidate = (i, False)
                break
            if hl in ("timestamp_iso", "iso_time"):
                ts_candidate = (i, True)
                break

    if ts_candidate is not None:
        st.ts_col, st.ts_is_iso = ts_candidate
        st.col_kind[st.ts_col] = "ts"

    for i in range(ncols):
        if i == st.ts_col:
            continue
        if seen[i] == 0:
            if header and i < len(header) and "iso" in header[i].strip().lower():
                st.col_kind[i] = "iso"
            else:
                st.numeric_cols.append(i)
                st.col_kind[i] = "num"
            continue
        if string_votes[i] > numeric_votes[i]:
            if iso_votes[i] > 0 and iso_votes[i] >= 0.5 * string_votes[i]:
                st.col_kind[i] = "iso"
            else:
                st.channel_cols.append(i)
                st.col_kind[i] = "chan"
        else:
            st.numeric_cols.append(i)
            st.col_kind[i] = "num"

    return st


def parse_ts_numeric(tok):
    try:
        v = float(tok)
    except (ValueError, TypeError):
        return None
    a = abs(v)
    if a > 1e17:
        return v / 1e6
    if a > 1e14:
        return v / 1e3
    if a > 1e11:
        return v
    return v * 1000.0


def parse_ts_iso(tok):
    try:
        t = tok.strip()
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        if "." in t:
            head, frac = t.split(".", 1)
            tz = ""
            for sep in ("+", "-"):
                idx = frac.find(sep)
                if idx > 0:
                    tz = frac[idx:]
                    frac = frac[:idx]
                    break
            frac6 = (frac + "000000")[:6]
            t = head + "." + frac6 + tz
        dt = datetime.datetime.fromisoformat(t)
        return dt.timestamp() * 1000.0
    except Exception:
        return None


def parse_note_line(line: str) -> Optional[tuple[float, float, str]]:
    """Parse an inline `#NOTE,<start_ns>,<end_ns>,<text>` stream message into `(start_ms, end_ms, text)`."""
    if not line.startswith("#NOTE,"):
        return None
    try:
        row = next(csv.reader([line]))
    except Exception:
        return None
    if len(row) < 4 or row[0].strip() != "#NOTE":
        return None
    start_ms = parse_ts_numeric(row[1].strip())
    if start_ms is None:
        return None
    end_ms = parse_ts_numeric(row[2].strip()) if row[2].strip() else start_ms
    if end_ms is None or end_ms < start_ms:
        end_ms = start_ms
    text = row[3].strip()
    if not text:
        return None
    return (start_ms, end_ms, text)


def parse_total_line(line: str) -> tuple[bool, Optional[float]]:
    """Parse an inline `#TOTAL,<duration_ms>` stream message."""
    if not line.startswith("#TOTAL,"):
        return (False, None)
    raw = line[7:].strip()
    if not raw:
        return (True, None)
    try:
        val = float(raw)
    except ValueError:
        return (True, None)
    if not math.isfinite(val) or val < 0.0:
        return (True, None)
    return (True, val)


def parse_mode_line(line: str) -> Optional[str]:
    """Parse an inline `#MODE,<live|replay|idle>` stream message."""
    if not line.startswith("#MODE,"):
        return None
    mode = line[6:].strip().lower()
    return mode if mode in ("live", "replay", "idle") else None


def parse_seek_line(line: str) -> Optional[float]:
    """Parse an inline `#SEEK,<elapsed_sec>` stream message."""
    if not line.startswith("#SEEK,"):
        return None
    raw = line[6:].strip()
    try:
        val = float(raw)
    except ValueError:
        return None
    return max(0.0, val) if math.isfinite(val) else None


class NotesTailer:
    """Incrementally tails a `<recording>.notes.csv` file without locking it."""

    def __init__(self, path: str):
        self.path = path
        self._offset = 0

    def poll(self) -> list[tuple[float, float, str]]:
        if not self.path or not os.path.exists(self.path):
            return []
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return []
        if size < self._offset:
            self._offset = 0
        if size == self._offset:
            return []
        try:
            with open(self.path, "rb") as f:
                f.seek(self._offset)
                raw = f.read()
        except OSError:
            return []
        last_nl = raw.rfind(b"\n")
        if last_nl < 0:
            return []
        chunk = raw[: last_nl + 1]
        self._offset += len(chunk)

        notes: list[tuple[float, float, str]] = []
        text_chunk = chunk.decode("utf-8", errors="replace")
        for row in csv.reader(text_chunk.splitlines()):
            if not row or len(row) < 5:
                continue
            if row[0].strip() == "start_ns":
                continue
            start_ms = parse_ts_numeric(row[0].strip())
            if start_ms is None:
                continue
            end_ms = parse_ts_numeric(row[2].strip()) if row[2].strip() else start_ms
            if end_ms is None or end_ms < start_ms:
                end_ms = start_ms
            note_text = row[4].strip()
            if note_text:
                notes.append((start_ms, end_ms, note_text))
        return notes


class GroupSpec:
    def __init__(self, raw):
        self.raw = raw
        self.columns = None
        self.pattern = None
        self.channel = None

        cols = self._try_parse_columns(raw)
        if cols is not None:
            self.columns = cols
        elif any(ch in raw for ch in "*?["):
            self.pattern = raw
        else:
            self.channel = raw

    @staticmethod
    def _try_parse_columns(raw):
        cols = []
        for p in raw.split(","):
            p = p.strip()
            if not p:
                continue
            if "-" in p:
                a, _, b = p.partition("-")
                if not (a.strip().isdigit() and b.strip().isdigit()):
                    return None
                lo, hi = int(a), int(b)
                if lo > hi:
                    lo, hi = hi, lo
                cols.extend(range(lo - 1, hi))
            else:
                if not p.isdigit():
                    return None
                cols.append(int(p) - 1)
        return cols if cols else None


class Channel:
    _INIT_CAP = 4096

    __slots__ = (
        "name",
        "col",
        "_cap",
        "_xs_owner",
        "xs",
        "ys",
        "present",
        "count",
        "vmin",
        "vmax",
        "last_val",
        "curve",
        "scatter",
        "dirty",
        "scatter_count",
        "has_nan",
        "curve_i0",
        "curve_i1",
        "curve_count",
        "curve_tail_x",
        "scatter_i0",
        "scatter_i1",
        "rendered_y_lo",
        "rendered_y_hi",
    )

    def __init__(self, name, col, xs_owner: Optional["Channel"] = None):
        self.name = name
        self.col = col
        cap = xs_owner._cap if xs_owner is not None else self._INIT_CAP
        self._cap = cap
        self._xs_owner = xs_owner
        self.xs = xs_owner.xs if xs_owner is not None else np.empty(cap, dtype=np.float64)
        self.ys = np.empty(cap, dtype=np.float64)
        self.present = np.empty(cap, dtype=bool)
        self.count = 0
        self.vmin = math.inf
        self.vmax = -math.inf
        self.last_val = math.nan
        self.curve = None
        self.scatter = None
        self.dirty = False
        self.scatter_count = -1
        self.has_nan = False
        self.curve_i0 = -1
        self.curve_i1 = -1
        self.curve_count = -1
        self.curve_tail_x = math.nan
        self.scatter_i0 = -1
        self.scatter_i1 = -1
        self.rendered_y_lo = math.nan
        self.rendered_y_hi = math.nan

    def reset(self):
        self.count = 0
        self.vmin = math.inf
        self.vmax = -math.inf
        self.last_val = math.nan
        self.scatter_count = -1
        self.has_nan = False
        self.curve_i0 = -1
        self.curve_i1 = -1
        self.curve_count = -1
        self.curve_tail_x = math.nan
        self.scatter_i0 = -1
        self.scatter_i1 = -1
        self.rendered_y_lo = math.nan
        self.rendered_y_hi = math.nan
        self.dirty = True
        if self._xs_owner is not None:
            self.xs = self._xs_owner.xs

    def ensure_capacity(self, min_cap: int) -> None:
        target = min(MAX_POINTS, max(self._cap, int(min_cap)))
        if target <= self._cap:
            if self._xs_owner is not None:
                self.xs = self._xs_owner.xs
            return
        n = self.count
        if self._xs_owner is None:
            new_xs = np.empty(target, dtype=np.float64)
            if n > 0:
                new_xs[:n] = self.xs[:n]
            self.xs = new_xs
        else:
            self.xs = self._xs_owner.xs
        new_ys = np.empty(target, dtype=np.float64)
        new_pr = np.empty(target, dtype=bool)
        if n > 0:
            new_ys[:n] = self.ys[:n]
            new_pr[:n] = self.present[:n]
        self.ys = new_ys
        self.present = new_pr
        self._cap = target

    def _grow(self):
        new_cap = min(MAX_POINTS, self._cap * 2)
        n = self.count
        if self._xs_owner is None:
            new_xs = np.empty(new_cap, dtype=np.float64)
            if n > 0:
                new_xs[:n] = self.xs[:n]
            self.xs = new_xs
        else:
            self.xs = self._xs_owner.xs
        new_ys = np.empty(new_cap, dtype=np.float64)
        new_pr = np.empty(new_cap, dtype=bool)
        if n > 0:
            new_ys[:n] = self.ys[:n]
            new_pr[:n] = self.present[:n]
        self.ys = new_ys
        self.present = new_pr
        self._cap = new_cap

    def append(self, x, val_or_none):
        i = self.count
        owner = self._xs_owner
        if i >= self._cap:
            if self._cap < MAX_POINTS:
                self._grow()
            else:
                keep = MAX_POINTS // 2
                if owner is None:
                    self.xs[:keep] = self.xs[MAX_POINTS - keep :]
                else:
                    self.xs = owner.xs
                self.ys[:keep] = self.ys[MAX_POINTS - keep :]
                self.present[:keep] = self.present[MAX_POINTS - keep :]
                self.count = keep
                self.curve_i0 = -1
                self.curve_i1 = -1
                self.curve_count = -1
                self.scatter_count = -1
                i = keep
        if owner is None:
            self.xs[i] = x
        else:
            self.xs = owner.xs
        if val_or_none is None:
            self.ys[i] = self.last_val
            self.present[i] = False
            if not math.isfinite(self.last_val):
                self.has_nan = True
        else:
            self.ys[i] = val_or_none
            self.present[i] = True
            self.last_val = val_or_none
            if val_or_none < self.vmin:
                self.vmin = val_or_none
            if val_or_none > self.vmax:
                self.vmax = val_or_none
        self.count += 1
        self.dirty = True

    def prune(self, x_lo):
        n = self.count
        if n == 0:
            return False
        owner = self._xs_owner
        if owner is not None and owner.xs is self.xs:
            if owner.count >= n:
                return False
            keep = owner.count
            drop = n - keep
        else:
            i0 = int(np.searchsorted(self.xs[:n], x_lo, side="left"))
            drop = i0 - 1
            if drop < 512 and drop < (n >> 2):
                return False
            keep = n - drop
            self.xs[:keep] = self.xs[drop:n]
        self.ys[:keep] = self.ys[drop:n]
        self.present[:keep] = self.present[drop:n]
        self.count = keep
        self.curve_i0 = -1
        self.curve_i1 = -1
        self.curve_count = -1
        self.scatter_count = -1
        if keep == 0:
            self.has_nan = False
            self.vmin = math.inf
            self.vmax = -math.inf
        elif not self.has_nan:
            seg = self.ys[:keep]
            self.vmin = float(seg.min())
            self.vmax = float(seg.max())
        else:
            self.has_nan = not bool(np.isfinite(self.ys[:keep]).all())
            pres = self.present[:keep]
            if pres.any():
                seg = self.ys[:keep][pres]
                self.vmin = float(seg.min())
                self.vmax = float(seg.max())
            else:
                self.vmin = math.inf
                self.vmax = -math.inf
        return True


class Reader(threading.Thread):
    def __init__(
        self,
        structure,
        plan,
        line_source,
        realtime_replay=False,
        first_ts_ms=None,
        total_duration_ms=None,
        initial_notes=None,
        source_mode: Optional[str] = None,
    ):
        super().__init__(daemon=True)
        self.st = structure
        self.plan = plan
        self.line_source = line_source
        self.lock = threading.Lock()
        self.stop_flag = False
        self.eof = False
        self._synth = 0
        self.realtime_replay = realtime_replay
        self._replay_first_x = None
        self._replay_start_wall = None
        self.paused = False
        self._pause_started_at = None
        self.first_ts_ms = first_ts_ms
        self.total_duration_ms = total_duration_ms
        self.latest_ts_ms = None
        self.playhead_ms: Optional[float] = first_ts_ms
        self.preloaded_recording: bool = False
        self._replay_timestamps_ms: list[float] = []
        self._seek_target_idx: Optional[int] = None
        self.source_mode: Optional[str] = "replay" if realtime_replay else source_mode
        self.stream_generation = 0
        self.reset_seq = 0
        self.seek_seq = 0
        self._notes: list[tuple[float, float, str]] = []
        self._note_keys: set[tuple[float, float, str]] = set()
        if initial_notes:
            for s_ms, e_ms, txt in initial_notes:
                self.add_note(s_ms, e_ms, txt)

    def add_note(self, start_ms: float, end_ms: float, text: str) -> None:
        key = (float(start_ms), float(end_ms), text)
        with self.lock:
            if key not in self._note_keys:
                self._note_keys.add(key)
                self._notes.append(key)

    def get_notes(self) -> list[tuple[float, float, str]]:
        with self.lock:
            return list(self._notes)

    def set_total_duration(self, total_duration_ms: Optional[float]) -> None:
        with self.lock:
            self.total_duration_ms = total_duration_ms

    def set_source_mode(self, mode: Optional[str]) -> None:
        with self.lock:
            self.source_mode = mode

    def set_playhead_ms(self, playhead_ms: Optional[float], bump_seek_seq: bool = False) -> None:
        with self.lock:
            self.playhead_ms = playhead_ms
            if bump_seek_seq:
                self.seek_seq += 1

    def _parse_fields_ts_locked(self, fields: list[str]) -> float:
        st = self.st
        ts_col = st.ts_col
        ts_iso = st.ts_is_iso
        if ts_col is not None and ts_col < len(fields):
            x = parse_ts_iso(fields[ts_col]) if ts_iso else parse_ts_numeric(fields[ts_col])
        else:
            x = None
        if x is None:
            self._synth += 1
            x = float(self._synth)
        return x

    def _dispatch_fields_locked(self, fields: list[str], x: float) -> None:
        if self.first_ts_ms is None:
            self.first_ts_ms = x
        self.latest_ts_ms = x
        for channels, match_col, match_val in self.plan:
            if match_col is not None:
                v = fields[match_col].strip() if match_col < len(fields) else ""
                if v != match_val:
                    continue
            for ch in channels:
                c = ch.col
                tok = fields[c].strip() if c < len(fields) else ""
                if tok == "":
                    ch.append(x, None)
                else:
                    try:
                        val = float(tok)
                    except ValueError:
                        ch.append(x, None)
                        continue
                    if val != val or val in (float("inf"), float("-inf")):
                        ch.append(x, None)
                    else:
                        ch.append(x, val)

    def load_recording_rows(
        self,
        first_ts_s: Optional[float],
        last_ts_s: Optional[float],
        current_elapsed_s: float,
        header: Optional[str],
        lines: list[str],
        bump_generation: bool = True,
    ) -> None:
        """Preload a full recording trace so users can scrub and scroll both backward and forward."""
        sig = (
            first_ts_s,
            last_ts_s,
            len(lines),
            lines[0] if lines else "",
            lines[-1] if lines else "",
        )
        with self.lock:
            if (
                self.preloaded_recording
                and getattr(self, "_loaded_replay_sig", None) == sig
                and self.first_ts_ms is not None
                and self.latest_ts_ms is not None
            ):
                self.playhead_ms = min(
                    self.latest_ts_ms,
                    max(self.first_ts_ms, self.first_ts_ms + max(0.0, float(current_elapsed_s)) * 1000.0),
                )
                self.seek_seq += 1
                return
            self._loaded_replay_sig = sig
            est_cap = min(MAX_POINTS, max(Channel._INIT_CAP, len(lines)))
            for channels, _, _ in self.plan:
                for ch in channels:
                    ch.reset()
                    ch.ensure_capacity(est_cap)
            self._synth = 0
            self.first_ts_ms = None
            self.latest_ts_ms = None
            ts_list: list[float] = []
            for raw in lines:
                ln = raw.rstrip("\n").rstrip("\r")
                if not ln or ln.startswith("#"):
                    continue
                fields = ln.split(",")
                if is_header_row(fields):
                    continue
                x = self._parse_fields_ts_locked(fields)
                self._dispatch_fields_locked(fields, x)
                ts_list.append(x)

            if self.first_ts_ms is None and first_ts_s is not None:
                self.first_ts_ms = float(first_ts_s) * 1000.0
            if self.latest_ts_ms is None and last_ts_s is not None:
                self.latest_ts_ms = float(last_ts_s) * 1000.0
            if self.first_ts_ms is not None and self.latest_ts_ms is not None:
                self.total_duration_ms = max(0.0, self.latest_ts_ms - self.first_ts_ms)
                self.playhead_ms = min(
                    self.latest_ts_ms,
                    max(self.first_ts_ms, self.first_ts_ms + max(0.0, float(current_elapsed_s)) * 1000.0),
                )
            else:
                self.playhead_ms = None

            self.source_mode = "replay"
            self.preloaded_recording = bool(ts_list)
            self._replay_timestamps_ms = ts_list
            self._seek_target_idx = None
            self._replay_first_x = None
            self._replay_start_wall = None
            if bump_generation:
                self.stream_generation += 1
            self.reset_seq += 1
            self.seek_seq += 1

    def seek_to_ms(self, target_ms: float) -> Optional[float]:
        """Seek local standalone replay pacing to ``target_ms``."""
        with self.lock:
            ts_list = self._replay_timestamps_ms
            if not ts_list:
                return None
            lo = ts_list[0]
            hi = ts_list[-1]
            clamped = min(hi, max(lo, float(target_ms)))
            idx = bisect.bisect_left(ts_list, clamped)
            if idx >= len(ts_list):
                idx = len(ts_list) - 1
            self._seek_target_idx = idx
            self._replay_first_x = None
            self._replay_start_wall = None
            self.playhead_ms = ts_list[idx]
            self.seek_seq += 1
            return self.playhead_ms

    def reset_for_new_stream(self, total_duration_ms: Optional[float]) -> None:
        with self.lock:
            for channels, _, _ in self.plan:
                for ch in channels:
                    ch.reset()
            self.first_ts_ms = None
            self.latest_ts_ms = None
            self.playhead_ms = None
            self.preloaded_recording = False
            self._loaded_replay_sig = None
            self._replay_timestamps_ms = []
            self._seek_target_idx = None
            self.total_duration_ms = total_duration_ms
            self._notes.clear()
            self._note_keys.clear()
            self.stream_generation += 1
            self.reset_seq += 1
            self.seek_seq += 1

    def set_paused(self, paused=None):
        with self.lock:
            target = (not self.paused) if paused is None else bool(paused)
            if target and not self.paused:
                self.paused = True
                self._pause_started_at = time.monotonic()
            elif not target and self.paused:
                now = time.monotonic()
                if self._pause_started_at is not None and self._replay_start_wall is not None:
                    self._replay_start_wall += now - self._pause_started_at
                self._pause_started_at = None
                self.paused = False
            return self.paused

    def run(self):
        if self.realtime_replay and self.preloaded_recording:
            self._run_preloaded_replay()
            return
        st = self.st
        ts_col = st.ts_col
        ts_iso = st.ts_is_iso
        for line in self.line_source:
            if self.stop_flag:
                break
            line = line.rstrip("\n").rstrip("\r")
            if not line:
                continue
            if line.startswith("#"):
                note = parse_note_line(line)
                if note is not None:
                    self.add_note(*note)
                    continue
                parsed_mode = parse_mode_line(line)
                if parsed_mode is not None:
                    self.set_source_mode(parsed_mode)
                    continue
                seek_sec = parse_seek_line(line)
                if seek_sec is not None:
                    with self.lock:
                        if self.first_ts_ms is not None:
                            target_ms = self.first_ts_ms + seek_sec * 1000.0
                            if self.latest_ts_ms is not None and self.preloaded_recording:
                                target_ms = min(self.latest_ts_ms, max(self.first_ts_ms, target_ms))
                            self.playhead_ms = target_ms
                            self.seek_seq += 1
                    continue
                has_total, parsed_total = parse_total_line(line)
                if has_total:
                    with self.lock:
                        if (
                            self.preloaded_recording
                            and parsed_total is not None
                            and self.total_duration_ms is not None
                            and abs(parsed_total - self.total_duration_ms) <= 250.0
                        ):
                            continue
                        has_existing_data = self.latest_ts_ms is not None
                    if has_existing_data:
                        self.reset_for_new_stream(parsed_total)
                    else:
                        self.set_total_duration(parsed_total)
                continue
            # Fast path when recording is already preloaded in memory: only extract timestamp
            if self.preloaded_recording and ts_col == 0 and not self.realtime_replay:
                cidx = line.find(",")
                tok = line[:cidx] if cidx != -1 else line
                x = parse_ts_iso(tok) if ts_iso else parse_ts_numeric(tok)
                if x is not None:
                    with self.lock:
                        if self.preloaded_recording:
                            self.playhead_ms = x
                            continue
            fields = line.split(",")
            if is_header_row(fields):
                continue
            with self.lock:
                if ts_col is not None and ts_col < len(fields):
                    x = parse_ts_iso(fields[ts_col]) if ts_iso else parse_ts_numeric(fields[ts_col])
                else:
                    x = None
                if x is None:
                    self._synth += 1
                    x = float(self._synth)
            if self.realtime_replay:
                self._pace(x)
            self._dispatch(fields, x)
        self.eof = True

    def _run_preloaded_replay(self):
        idx = 0
        while not self.stop_flag:
            while self.paused and not self.stop_flag:
                with self.lock:
                    if self._seek_target_idx is not None:
                        idx = self._seek_target_idx
                        self._seek_target_idx = None
                        self._replay_first_x = None
                        self._replay_start_wall = None
                time.sleep(0.02)
            if self.stop_flag:
                break
            with self.lock:
                if self._seek_target_idx is not None:
                    idx = self._seek_target_idx
                    self._seek_target_idx = None
                    self._replay_first_x = None
                    self._replay_start_wall = None
                n = len(self._replay_timestamps_ms)
                if idx >= n:
                    break
                x_ms = self._replay_timestamps_ms[idx]
            self._pace(x_ms)
            if self.stop_flag:
                break
            with self.lock:
                if self._seek_target_idx is not None:
                    continue
                self.playhead_ms = x_ms
            idx += 1
        self.eof = True

    def _pace(self, x_ms):
        while self.paused and not self.stop_flag:
            time.sleep(0.02)
        now = time.monotonic()
        if self._replay_first_x is None:
            self._replay_first_x = x_ms
            self._replay_start_wall = now
            return
        while not self.stop_flag:
            if self.paused:
                time.sleep(0.02)
                continue
            with self.lock:
                if self._seek_target_idx is not None:
                    return
            now = time.monotonic()
            target_wall = self._replay_start_wall + (x_ms - self._replay_first_x) / 1000.0
            delay = target_wall - now
            if delay <= 0:
                break
            time.sleep(min(delay, 0.02))

    def _dispatch(self, fields, x):
        with self.lock:
            if self.preloaded_recording:
                self.playhead_ms = x
                return
            # Handle backward time jumps cleanly (e.g., replay loop or switching files).
            if self.latest_ts_ms is not None and x < self.latest_ts_ms - 1000.0:
                for channels, _, _ in self.plan:
                    for ch in channels:
                        ch.reset()
                self.first_ts_ms = x
                self.reset_seq += 1
            self._dispatch_fields_locked(fields, x)
            self.playhead_ms = x


def build_groups(st, specs, sample_rows):
    groups = []
    titles = []
    plan = []

    if not specs:
        specs = [None]

    for gi, spec in enumerate(specs):
        channels = []
        match_col = None
        match_val = None
        title = None

        if spec is None:
            cols = list(st.numeric_cols)
            title = "all"
        elif spec.columns is not None:
            cols = [c for c in spec.columns if 0 <= c < st.ncols]
            title = spec.raw
        elif spec.pattern is not None:
            title = spec.raw
            cols = []
            for c in st.numeric_cols:
                name = st.header[c].strip() if (st.header and c < len(st.header)) else ""
                if name and fnmatch.fnmatch(name, spec.pattern):
                    cols.append(c)
        else:
            match_val = spec.channel
            title = spec.channel
            if st.channel_cols:
                match_col = st.channel_cols[0]
            cols = list(st.numeric_cols)
            if match_col is not None:
                nonempty = set()
                for r in sample_rows:
                    if match_col < len(r) and r[match_col].strip() == match_val:
                        for c in cols:
                            if c < len(r) and r[c].strip() != "":
                                nonempty.add(c)
                if nonempty:
                    cols = [c for c in cols if c in nonempty]

        have_header = bool(st.header)
        raw_names = []
        for c in cols:
            if have_header and c < len(st.header) and st.header[c].strip():
                raw_names.append(st.header[c].strip())
            else:
                raw_names.append(None)

        group_owner: Optional[Channel] = None
        if all(n is not None for n in raw_names) and raw_names:
            counts = collections.Counter(raw_names)
            seen = collections.Counter()
            for c, rn in zip(cols, raw_names):
                if counts[rn] > 1:
                    seen[rn] += 1
                    name = "%s-%d" % (rn, seen[rn])
                else:
                    name = rn
                ch_obj = Channel(name, c, xs_owner=group_owner)
                if group_owner is None:
                    group_owner = ch_obj
                channels.append(ch_obj)
        else:
            base = title if (title and title != "all") else "G"
            for idx, c in enumerate(cols, 1):
                ch_obj = Channel("%s-%d" % (base, idx), c, xs_owner=group_owner)
                if group_owner is None:
                    group_owner = ch_obj
                channels.append(ch_obj)

        groups.append(channels)
        titles.append(title or ("G%d" % (gi + 1)))
        plan.append((channels, match_col, match_val))

    return groups, titles, plan


def _visible_range(ch, x_lo, x_hi=None, i0: Optional[int] = None, i1: Optional[int] = None):
    n = ch.count
    if n == 0 or not math.isfinite(ch.vmin):
        return (None, None)
    if i0 is None or i1 is None:
        xs = ch.xs[:n]
        i0 = int(np.searchsorted(xs, x_lo, side="left"))
        i1 = n if x_hi is None else min(n, int(np.searchsorted(xs, x_hi, side="right")))
    lo = math.inf
    hi = -math.inf
    if i1 > i0:
        if not ch.has_nan:
            seg = ch.ys[i0:i1]
            lo = float(seg.min())
            hi = float(seg.max())
        else:
            pres = ch.present[i0:i1]
            if pres.any():
                seg = ch.ys[i0:i1][pres]
                lo = float(seg.min())
                hi = float(seg.max())
    if i0 > 0:
        held = ch.ys[i0 - 1]
        if math.isfinite(held):
            lo = min(lo, held)
            hi = max(hi, held)
    if not math.isfinite(lo):
        return (None, None)
    return (lo, hi)


def _normalize_y(arr, lo: float, hi: float):
    if hi <= lo:
        return arr + (0.5 - lo)
    return (arr - lo) * (1.0 / (hi - lo))


def _normalize_y_into(src: np.ndarray, lo: float, hi: float, out: np.ndarray) -> None:
    if hi <= lo:
        np.add(src, 0.5 - lo, out=out)
    else:
        np.subtract(src, lo, out=out)
        out *= 1.0 / (hi - lo)


_FIXED_X_FRACS_WIDE = (0.0, 0.25, 0.5, 0.75, 1.0)
_FIXED_X_FRACS_NARROW = (0.0, 0.5, 1.0)


def _fixed_div_fracs(px_w: float) -> tuple[float, ...]:
    return _FIXED_X_FRACS_WIDE if (not px_w or px_w >= 320.0) else _FIXED_X_FRACS_NARROW


def _format_lookback_label(offset_ms: float) -> str:
    """Format lookback offset (<= 0 ms relative to right edge) for stationary X-axis ticks."""
    if abs(offset_ms) < 0.5:
        return "0s"
    sec = abs(offset_ms) / 1000.0
    sign = "-" if offset_ms < 0.0 else ""
    if round(sec, 2) < 60.0:
        txt = f"{sec:.2f}".rstrip("0").rstrip(".")
        return f"{sign}{txt}s"
    if sec < 3600.0:
        mins = int(sec // 60)
        rem = sec - mins * 60
        rem_rounded = round(rem, 1)
        if rem_rounded >= 60.0:
            return f"{sign}{mins + 1}m"
        if rem_rounded < 0.05:
            return f"{sign}{mins}m"
        if abs(rem_rounded - round(rem_rounded)) < 1e-6:
            return f"{sign}{mins}m{int(round(rem_rounded)):02d}s"
        return f"{sign}{mins}m{rem_rounded:04.1f}s"
    hours = int(sec // 3600)
    rem = sec - hours * 3600
    mins = int(round(rem / 60.0))
    if mins >= 60:
        hours += 1
        mins = 0
    return f"{sign}{hours}h" if mins == 0 else f"{sign}{hours}h{mins:02d}m"


def _make_dot_pixmap(color_rgb: tuple[int, int, int], size: int = 5) -> QtGui.QPixmap:
    pm = QtGui.QPixmap(size, size)
    pm.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(pm)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
    painter.setPen(QtCore.Qt.PenStyle.NoPen)
    painter.setBrush(QtGui.QColor(*color_rgb))
    painter.drawEllipse(QtCore.QRectF(0.25, 0.25, size - 0.5, size - 0.5))
    painter.end()
    return pm


def _make_fast_render_classes(pg):
    """Build lightweight direct-paint PyQtGraph items avoiding QPicture and ScatterPlot rebuild overhead."""
    _tick_font = QtGui.QFont("Segoe UI", 8)
    _tick_fm = QtGui.QFontMetricsF(_tick_font)
    _label_color = QtGui.QColor("#94a3b8")

    def _get_static_tick(cache: dict[str, tuple[QtGui.QStaticText, float]], text: str) -> tuple[QtGui.QStaticText, float]:
        entry = cache.get(text)
        if entry is not None:
            return entry
        if len(cache) > 512:
            cache.clear()
        st = QtGui.QStaticText(text)
        st.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        st.prepare(QtGui.QTransform(), _tick_font)
        w = float(_tick_fm.horizontalAdvance(text))
        entry = (st, w)
        cache[text] = entry
        return entry

    class _FastRelMsAxis(pg.AxisItem):
        """Stationary oscilloscope-division lookback axis rendered directly with native QStaticText."""

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._text_cache: dict[str, tuple[QtGui.QStaticText, float]] = {}

        def tickValues(self, minVal, maxVal, size):
            if not math.isfinite(minVal) or not math.isfinite(maxVal) or maxVal <= minVal:
                return []
            span = maxVal - minVal
            fracs = _fixed_div_fracs(size)
            return [(span / max(1, len(fracs) - 1), [minVal + f * span for f in fracs])]

        def tickStrings(self, values, scale, spacing):
            lv = self.linkedView()
            if lv is not None:
                x0, x1 = lv.viewRange()[0]
                if math.isfinite(x1):
                    return [_format_lookback_label(v - x1) for v in values]
            ref = values[-1] if values else 0.0
            return [_format_lookback_label(v - ref) for v in values]

        def paint(self, p, opt, widget):
            sz = self.size()
            w = float(sz.width())
            p.setPen(self.pen())
            y = 0.5
            p.drawLine(QtCore.QPointF(0.0, y), QtCore.QPointF(w, y))
            if not self.style.get("showValues", True):
                return
            lv = self.linkedView()
            if lv is None:
                return
            x0, x1 = lv.viewRange()[0]
            if not math.isfinite(x0) or not math.isfinite(x1) or x1 <= x0 or w <= 1.0:
                return
            span = x1 - x0
            fracs = _fixed_div_fracs(w)
            left = 0.0
            right = w
            tick_y1 = y + 5.0
            txt_y = y + 6.0
            pen = self.pen()
            tick_pts = []
            draw_items = []
            for f in fracs:
                if f <= 0.0:
                    px = left + 0.5
                elif f >= 1.0:
                    px = right - 0.5
                else:
                    px = left + w * f
                tick_pts.append(QtCore.QLineF(px, y, px, tick_y1))
                st, tw = _get_static_tick(self._text_cache, _format_lookback_label((f - 1.0) * span))
                if f <= 0.0:
                    tx = left + 2.0
                elif f >= 1.0:
                    tx = right - tw - 2.0
                else:
                    tx = px - tw * 0.5
                if tx >= left - 2.0 and tx + tw <= right + 2.0:
                    draw_items.append((tx, st))
            if tick_pts:
                p.setPen(pen)
                p.drawLines(tick_pts)
            if draw_items:
                p.setFont(_tick_font)
                p.setPen(_label_color)
                for tx, st in draw_items:
                    p.drawStaticText(QtCore.QPointF(tx, txt_y), st)

    class _FastLeftAxis(pg.AxisItem):
        """Left axis that skips QPicture overhead when numeric labels are hidden."""

        def __init__(self, show_values: bool, **kwargs):
            super().__init__(**kwargs)
            self._show_values = show_values

        def paint(self, p, opt, widget):
            if self._show_values:
                super().paint(p, opt, widget)
                return
            sz = self.size()
            p.setPen(self.pen())
            x = float(sz.width()) - 0.5
            p.drawLine(QtCore.QPointF(x, 0.0), QtCore.QPointF(x, float(sz.height())))

    class _FastOverlayGrid(pg.GraphicsWidget):
        """Viewport-aligned grid overlay parented to ViewBox to avoid per-frame prepareGeometryChange."""

        def __init__(self, vb, y_axis, is_shared: bool):
            super().__init__()
            self._vb = vb
            self._y_axis = y_axis
            self._is_shared = is_shared
            pen = QtGui.QPen(QtGui.QColor(16, 22, 35))
            pen.setCosmetic(True)
            pen.setWidthF(1.0)
            self._pen = pen
            self.setParentItem(vb)
            self.setZValue(-10)
            self._sync_size()
            vb.geometryChanged.connect(self._sync_size)

        def _sync_size(self):
            sz = self._vb.size()
            self.setGeometry(0.0, 0.0, max(1.0, float(sz.width())), max(1.0, float(sz.height())))

        def paint(self, p, opt, widget):
            sz = self._vb.size()
            w, h = float(sz.width()), float(sz.height())
            if w <= 1.0 or h <= 1.0:
                return
            xr, yr = self._vb.viewRange()
            x0, x1 = float(xr[0]), float(xr[1])
            y0, y1 = float(yr[0]), float(yr[1])
            if not (math.isfinite(x0) and math.isfinite(x1) and x1 > x0):
                return
            lines = []
            for f in _fixed_div_fracs(w):
                if 0.0 < f < 1.0:
                    px = w * f
                    lines.append(QtCore.QLineF(px, 0.0, px, h))
            if math.isfinite(y0) and math.isfinite(y1) and y1 > y0:
                inv_y = h / (y1 - y0)
                if self._is_shared:
                    levels = self._y_axis.tickValues(y0, y1, h)
                    if levels:
                        for y in levels[0][1]:
                            if y0 <= y <= y1:
                                py = (y1 - y) * inv_y
                                lines.append(QtCore.QLineF(0.0, py, w, py))
                else:
                    for y in (0.25, 0.5, 0.75):
                        if y0 <= y <= y1:
                            py = (y1 - y) * inv_y
                            lines.append(QtCore.QLineF(0.0, py, w, py))
            if lines:
                p.setPen(self._pen)
                p.drawLines(lines)

    class _FastWaveformItem(pg.PlotCurveItem):
        """Combined line + PixmapFragment dot item without per-frame dataBounds scans."""

        _WIDE_RECT = QtCore.QRectF(-1e8, -1e8, 2e8, 2e8)

        def __init__(self, draw_line: bool, dot_pm: Optional[QtGui.QPixmap], **kwargs):
            super().__init__(**kwargs)
            self.has_line = draw_line
            self.has_points = dot_pm is not None
            self._dot_pm = dot_pm
            self._dot_x = None
            self._dot_y = None
            self._has_y_norm = False
            self._y_scale = 1.0
            self._y_offset = 0.0
            self._y_bounds = None
            self._frags = (
                pg.Qt.internals.PrimitiveArray(QtGui.QPainter.PixmapFragment, 10)
                if dot_pm is not None
                else None
            )

        def setYBounds(self, lo: float, hi: float) -> None:
            if self._y_bounds is not None and self._y_bounds[0] == lo and self._y_bounds[1] == hi:
                return
            self._y_bounds = (lo, hi)
            self._has_y_norm = True
            if hi <= lo:
                self._y_scale = 1.0
                self._y_offset = 0.5 - lo
            else:
                self._y_scale = 1.0 / (hi - lo)
                self._y_offset = -lo

        def setFastCurve(
            self,
            x,
            y,
            connect: str = "all",
            skip_finite_check: bool = True,
        ) -> None:
            self.xData = x
            self.yData = y
            self.opts["connect"] = connect
            self.opts["skipFiniteCheck"] = skip_finite_check
            self.path = None
            self._lineSegmentsRendered = False
            self._mouseShape = None

        def setPoints(self, dot_x, dot_y, trigger_update: bool = False) -> None:
            self._dot_x = dot_x
            self._dot_y = dot_y
            if trigger_update:
                self.update()

        def clearPoints(self, trigger_update: bool = False) -> None:
            self._dot_x = None
            self._dot_y = None
            if trigger_update:
                self.update()

        def viewTransformChanged(self):
            pass

        def boundingRect(self):
            return self._WIDE_RECT

        def dataBounds(self, ax, frac=1.0, orthoRange=None):
            return (-1e8, 1e8)

        def paint(self, p, opt, widget):
            has_y_norm = self._has_y_norm
            if has_y_norm:
                p.save()
                p.scale(1.0, self._y_scale)
                p.translate(0.0, self._y_offset)
            try:
                if self.has_line:
                    super().paint(p, opt, widget)
                if (
                    self._dot_pm is not None
                    and self._dot_x is not None
                    and self._dot_y is not None
                    and len(self._dot_x) > 0
                ):
                    n = len(self._dot_x)
                    # Use world transform (excluding DPR deviceTransform) because
                    # drawPixmapFragments already applies the paint device's DPR.
                    tr = p.transform()
                    m11, m22 = tr.m11(), tr.m22()
                    dx, dy = tr.dx(), tr.dy()
                    self._frags.resize(n)
                    frags = self._frags.ndarray()
                    frags[:, 0] = self._dot_x * m11 + dx
                    frags[:, 1] = self._dot_y * m22 + dy
                    pm = self._dot_pm
                    frags[:, 2] = 0.0
                    frags[:, 3] = 0.0
                    frags[:, 4] = float(pm.width())
                    frags[:, 5] = float(pm.height())
                    frags[:, 6] = 1.0
                    frags[:, 7] = 1.0
                    frags[:, 8] = 0.0
                    frags[:, 9] = 1.0
                    p.save()
                    p.resetTransform()
                    p.drawPixmapFragments(*self._frags.drawargs(), pm)
                    p.restore()
            finally:
                if has_y_norm:
                    p.restore()

    return _FastRelMsAxis, _FastLeftAxis, _FastOverlayGrid, _FastWaveformItem


def _make_adaptive_legend_class(pg):
    from pyqtgraph.graphicsItems.LegendItem import ItemSample, drawSymbol, invisibleEye

    _aa_hint = QtGui.QPainter.RenderHint.Antialiasing

    class AdaptiveItemSample(ItemSample):
        def __init__(self, item, width=18, height=16):
            self._sw = width
            self._sh = height
            super().__init__(item)
            self.setSampleSize(width, height)

        def setSampleSize(self, width, height):
            if self._sw == width and self._sh == height:
                return
            self._sw = width
            self._sh = height
            self.setFixedWidth(width)
            self.setFixedHeight(height)
            self.updateGeometry()
            self.update()

        def boundingRect(self):
            return QtCore.QRectF(0, 0, self._sw, self._sh)

        def paint(self, p, *args):
            opts = self.item.opts
            if opts.get("antialias"):
                p.setRenderHint(_aa_hint)

            if not self.item.isVisible():
                side = max(8, int(min(self._sw, self._sh) - 2))
                x = int((self._sw - side) / 2)
                y = int((self._sh - side) / 2)
                p.drawPixmap(QtCore.QPoint(x, y), invisibleEye.qicon.pixmap(side, side))
                return

            has_line = getattr(self.item, "has_line", not isinstance(self.item, pg.ScatterPlotItem))
            has_points = getattr(self.item, "has_points", False)
            if has_line:
                p.setPen(pg.mkPen(opts["pen"]))
                cy = self._sh / 2.0
                p.drawLine(QtCore.QPointF(0.0, cy), QtCore.QPointF(float(self._sw), cy))

            symbol = opts.get("symbol", None)
            if symbol is not None:
                if isinstance(self.item, pg.PlotDataItem):
                    opts = self.item.scatter.opts
                p.translate(self._sw / 2.0, self._sh / 2.0)
                sym_size = min(opts["size"], max(3.0, self._sh * 0.5))
                drawSymbol(p, symbol, sym_size, pg.mkPen(opts["pen"]), pg.mkBrush(opts["brush"]))
            elif has_points and getattr(self.item, "_dot_pm", None) is not None:
                pm = self.item._dot_pm
                tx = int(round((self._sw - pm.width()) * 0.5))
                ty = int(round((self._sh - pm.height()) * 0.5))
                p.drawPixmap(tx, ty, pm)

        def mouseClickEvent(self, ev):
            super().mouseClickEvent(ev)
            parent = self.parentItem()
            if parent is not None:
                parent.update()

    class _ClipViewport(QtWidgets.QGraphicsWidget):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemClipsChildrenToShape, True)

        def paint(self, p, *args):
            pass

    class _InnerLegend(pg.LegendItem):
        def paint(self, p, *args):
            pass

        def mouseDragEvent(self, ev):
            ev.ignore()

    class AdaptiveLegendItem(pg.GraphicsWidget):
        _ITEM_TIER = ("9pt", 16, 15, 5, -2, 8, 4)

        def __init__(self, brush=(15, 23, 42, 255), pen=(30, 41, 59), labelTextColor="#cbd5e1", **kwargs):
            super().__init__()
            self.opts = {
                "brush": pg.mkBrush(brush),
                "pen": pg.mkPen(pen),
                "labelTextColor": labelTextColor,
            }
            self.frame = True
            self.columnCount = 1
            self._bound_plot = None
            self._bound_slot = None
            self._bound_view = None
            self._on_adapted = None
            self._content_w = 0.0
            self._content_h = 0.0
            self._pref_w = 0.0
            self._card_w: Optional[float] = None
            self._card_h = 0.0
            self._scroll_y = 0.0
            self._scrollbar_dragging = False
            self._scrollbar_drag_offset = 0.0

            self._clip_box = _ClipViewport(self)
            self._inner = _InnerLegend(
                colCount=1,
                offset=None,
                sampleType=AdaptiveItemSample,
                labelTextColor=labelTextColor,
            )
            self._inner.setParentItem(self._clip_box)
            pt, _sw, _sh, hs, vs, px, py = self._ITEM_TIER
            self._pt = pt
            self._inner.layout.setHorizontalSpacing(hs)
            self._inner.layout.setVerticalSpacing(vs)
            self._inner.layout.setContentsMargins(px, py, px, py)

            self.setAcceptedMouseButtons(QtCore.Qt.MouseButton.LeftButton)
            self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemClipsChildrenToShape, True)

        @property
        def items(self):
            return self._inner.items

        def _scrollbar_rects(self) -> Optional[tuple[QtCore.QRectF, QtCore.QRectF]]:
            max_scroll = self._max_scroll()
            if max_scroll <= 0.5 or not self.isVisible():
                return None
            r = self.boundingRect()
            card_w = float(r.width())
            card_h = float(r.height())
            if card_w <= 16.0 or card_h <= 20.0:
                return None
            track_w = 4.0
            track_x = card_w - track_w - 4.0
            track_y = 6.0
            track_h = max(12.0, card_h - 12.0)
            content_h = max(card_h, self._content_h + 4.0)
            thumb_h = max(18.0, min(track_h, track_h * (card_h / content_h)))
            frac = max(0.0, min(1.0, self._scroll_y / max(1e-3, max_scroll)))
            thumb_y = track_y + frac * max(0.0, track_h - thumb_h)
            return (
                QtCore.QRectF(track_x, track_y, track_w, track_h),
                QtCore.QRectF(track_x, thumb_y, track_w, thumb_h),
            )

        def paint(self, p, *args):
            if self.frame:
                p.save()
                p.setRenderHint(_aa_hint, True)
                p.setPen(self.opts["pen"])
                p.setBrush(self.opts["brush"])
                r = self.boundingRect().adjusted(0.5, 0.5, -0.5, -0.5)
                p.drawRoundedRect(r, 10.0, 10.0)
                sb = self._scrollbar_rects()
                if sb is not None:
                    track_r, thumb_r = sb
                    p.setPen(QtCore.Qt.PenStyle.NoPen)
                    p.setBrush(QtGui.QColor(30, 41, 59, 170))
                    p.drawRoundedRect(track_r, 2.0, 2.0)
                    thumb_col = QtGui.QColor(100, 116, 139, 230) if self._scrollbar_dragging else QtGui.QColor(71, 85, 105, 220)
                    p.setBrush(thumb_col)
                    p.drawRoundedRect(thumb_r, 2.0, 2.0)
                p.restore()

        def bindToPlot(self, plot_item, slot_widget, layout_widget, on_adapted=None):
            if self._bound_plot is not None:
                try:
                    self._bound_plot.vb.geometryChanged.disconnect(self.adaptToPlot)
                except Exception:
                    pass
                try:
                    self._bound_plot.geometryChanged.disconnect(self.adaptToPlot)
                except Exception:
                    pass
            self._bound_plot = plot_item
            self._bound_slot = slot_widget
            self._bound_view = layout_widget
            self._on_adapted = on_adapted
            if plot_item is not None:
                plot_item.vb.geometryChanged.connect(self.adaptToPlot)
                plot_item.geometryChanged.connect(self.adaptToPlot)

        def addItem(self, item, name):
            self._inner.addItem(item, name)
            _, sw, sh, *_ = self._ITEM_TIER
            if self._inner.items:
                sample, label = self._inner.items[-1]
                if hasattr(sample, "setSampleSize"):
                    sample.setSampleSize(sw, sh)
                label.setText(label.text, size=self._pt)
            self._refresh_content_metrics()

        def removeItem(self, item):
            self._inner.removeItem(item)
            self._refresh_content_metrics()

        def clear(self):
            self._inner.clear()
            self._scroll_y = 0.0
            self._refresh_content_metrics()

        def _refresh_content_metrics(self):
            self._inner.setColumnCount(1)
            self._inner.layout.activate()
            pref = self._inner.layout.preferredSize()
            self._content_w = float(pref.width())
            self._content_h = float(pref.height())
            self._pref_w = self._content_w + 10.0

        def _max_scroll(self) -> float:
            if not self.isVisible() or self._card_h <= 0.0:
                return 0.0
            clip_h = max(1.0, self._card_h - 4.0)
            return max(0.0, self._content_h - clip_h)

        def isScrollable(self) -> bool:
            return self.isVisible() and self._max_scroll() > 0.5

        def _apply_scroll_pos(self, resize_inner: bool = False) -> None:
            max_s = self._max_scroll()
            self._scroll_y = max(0.0, min(max_s, self._scroll_y))
            if resize_inner:
                inner_w = max(1.0, float(self.boundingRect().width()) - 10.0)
                self._inner.setGeometry(0.0, -self._scroll_y, inner_w, max(1.0, self._content_h))
            self._inner.setPos(0.0, -self._scroll_y)
            self.update()

        def scrollByPixels(self, delta_y: float) -> bool:
            if not self.isScrollable():
                return False
            old_y = self._scroll_y
            self._scroll_y = max(0.0, min(self._max_scroll(), self._scroll_y + float(delta_y)))
            if abs(self._scroll_y - old_y) < 1e-3:
                return False
            self._apply_scroll_pos()
            return True

        def wheelEvent(self, ev):
            if self.isScrollable():
                dy = float(ev.delta()) / 120.0 * 36.0
                self.scrollByPixels(-dy)
                ev.accept()
                return
            ev.ignore()

        def mousePressEvent(self, ev):
            if ev.button() == QtCore.Qt.MouseButton.LeftButton:
                sb = self._scrollbar_rects()
                if sb is not None:
                    track_r, thumb_r = sb
                    pos = ev.pos()
                    if pos.x() >= track_r.left() - 6.0:
                        if thumb_r.contains(pos):
                            self._scrollbar_dragging = True
                            self._scrollbar_drag_offset = float(pos.y()) - float(thumb_r.top())
                        else:
                            max_s = self._max_scroll()
                            denom = max(1.0, float(track_r.height() - thumb_r.height()))
                            frac = max(0.0, min(1.0, (float(pos.y()) - float(track_r.top()) - float(thumb_r.height()) * 0.5) / denom))
                            self._scroll_y = frac * max_s
                            self._scrollbar_dragging = True
                            self._scrollbar_drag_offset = float(thumb_r.height()) * 0.5
                            self._apply_scroll_pos()
                        self.update()
                        ev.accept()
                        return
            ev.ignore()

        def mouseMoveEvent(self, ev):
            if self._scrollbar_dragging:
                sb = self._scrollbar_rects()
                if sb is not None:
                    track_r, thumb_r = sb
                    max_s = self._max_scroll()
                    denom = max(1.0, float(track_r.height() - thumb_r.height()))
                    top_y = float(ev.pos().y()) - self._scrollbar_drag_offset
                    frac = max(0.0, min(1.0, (top_y - float(track_r.top())) / denom))
                    self._scroll_y = frac * max_s
                    self._apply_scroll_pos()
                ev.accept()
                return
            ev.ignore()

        def mouseReleaseEvent(self, ev):
            if self._scrollbar_dragging:
                self._scrollbar_dragging = False
                self.update()
                ev.accept()
                return
            ev.ignore()

        def mouseDragEvent(self, ev):
            ev.ignore()

        def preferredCardWidth(self) -> float:
            if not self.isVisible():
                return 0.0
            if self._pref_w <= 0.0 and self._inner.items:
                self._refresh_content_metrics()
            return self._pref_w

        def _update_card_geometry(self, width: float, height: float) -> None:
            w = max(1.0, float(width))
            h = max(1.0, float(height))
            self._card_h = h
            self.setGeometry(0.0, 0.0, w, h)
            self._clip_box.setGeometry(1.0, 2.0, max(1.0, w - 2.0), max(1.0, h - 4.0))
            self._apply_scroll_pos(resize_inner=True)

        def setCardWidth(self, width: float) -> None:
            self._card_w = float(width)
            if self.isVisible():
                target_w = max(self._pref_w, float(self._card_w))
                self._update_card_geometry(target_w, self._card_h if self._card_h > 0 else self._content_h + 4.0)
            self._sync_slot_geometry()

        def _sync_slot_geometry(self):
            if self._bound_slot is None or self._bound_plot is None:
                return
            w = float(self.boundingRect().width()) if self.isVisible() else 0.0
            self._bound_slot.setMinimumWidth(w)
            self._bound_slot.setMaximumWidth(w)
            self._bound_slot.setPreferredWidth(w)
            self._bound_slot.updateGeometry()
            vb_y = max(0.0, float(self._bound_plot.vb.pos().y()))
            self.setPos(0.0, vb_y)

        def adaptToPlot(self, notify=True):
            if self._bound_plot is None or not self._inner.items:
                return
            vb_h = float(self._bound_plot.vb.size().height())
            if vb_h <= 0:
                return
            if vb_h < 14.0:
                if self.isVisible():
                    self.setVisible(False)
                self._sync_slot_geometry()
                if notify and self._on_adapted is not None:
                    self._on_adapted()
                return
            if not self.isVisible():
                self.setVisible(True)
            if self._pref_w <= 0.0 or self._content_h <= 0.0:
                self._refresh_content_metrics()
            target_h = min(self._content_h + 4.0, vb_h)
            target_w = max(self._pref_w, float(self._card_w)) if self._card_w is not None else self._pref_w
            self._update_card_geometry(target_w, target_h)
            self._sync_slot_geometry()
            if notify and self._on_adapted is not None:
                self._on_adapted()

        def adaptToParent(self):
            self.adaptToPlot()

    return AdaptiveLegendItem


def read_sample(stream, nrows=400, quick_on_header=False):
    lines = []
    if quick_on_header:
        while True:
            first = stream.readline()
            if not first:
                return lines
            s = first.rstrip("\n").rstrip("\r")
            if not s:
                continue
            lines.append(s)
            if s.startswith("#"):
                continue
            if is_header_row(s.split(",")):
                return lines
            break
    for _ in range(nrows - len(lines)):
        line = stream.readline()
        if not line:
            break
        lines.append(line.rstrip("\n").rstrip("\r"))
    return lines


class PrereadStdin:
    def __init__(self, buffered_lines, real):
        self._buf = buffered_lines
        self._real = real

    def __iter__(self):
        for l in self._buf:
            yield l
        for l in self._real:
            yield l


def _extract_recording_bounds(st, lines):
    ts_col = st.ts_col
    ts_iso = st.ts_is_iso
    if ts_col is None or not lines:
        return None, None
    first_ts = None
    last_ts = None
    for line in lines:
        fields = line.split(",")
        if ts_col < len(fields):
            x = parse_ts_iso(fields[ts_col]) if ts_iso else parse_ts_numeric(fields[ts_col])
            if x is not None:
                first_ts = x
                break
    for line in reversed(lines):
        fields = line.split(",")
        if ts_col < len(fields):
            x = parse_ts_iso(fields[ts_col]) if ts_iso else parse_ts_numeric(fields[ts_col])
            if x is not None:
                last_ts = x
                break
    return first_ts, last_ts


class LiveViewerWindow(QtWidgets.QWidget):
    """Independent Live Waveforms window usable inside EPOC Studio or standalone."""

    closed = QtCore.pyqtSignal()
    _external_pause_signal = QtCore.pyqtSignal(bool)
    _external_state_signal = QtCore.pyqtSignal(str, bool)
    _external_replay_data_signal = QtCore.pyqtSignal(object)

    def __init__(
        self,
        structure,
        groups,
        titles,
        reader: Reader,
        shared_groups=(),
        style: str = "linespoints",
        width_ms: Optional[float] = None,
        window_title: str = "EPOC-X Live Waveforms",
        notes_path: Optional[str] = None,
        on_close: Optional[Callable[[], None]] = None,
        on_pause_request: Optional[Callable[[Optional[bool]], Optional[bool]]] = None,
        initial_active: bool = True,
        initial_mode: str = "live",
        on_start_request: Optional[Callable[[], bool]] = None,
        on_seek_request: Optional[Callable[[float], Optional[float]]] = None,
        parent=None,
    ):
        super().__init__(parent)
        import pyqtgraph as pg

        self._pg = pg
        self.reader = reader
        self._groups = groups
        self._shared = set(shared_groups)
        self._width_ms = width_ms
        self._initial_width_ms = width_ms
        self._pan_x_hi: Optional[float] = None
        self._opened_at = time.monotonic()
        self._last_native_zoom_at = 0.0
        self._rendered_reset_seq = reader.reset_seq
        self._rendered_seek_seq = reader.seek_seq
        self._last_was_paused = bool(reader.paused)
        self._cursor_dragging = False
        self._cursor_ms: Optional[float] = None
        self._cursor_vp_pos: Optional[QtCore.QPointF] = None
        self._edge_scroll_dir = 0
        self._edge_scroll_speed = 0.0
        self._last_edge_tick = 0.0
        self._edge_scroll_timer = QtCore.QTimer(self)
        self._edge_scroll_timer.setInterval(16)
        self._edge_scroll_timer.setTimerType(QtCore.Qt.TimerType.PreciseTimer)
        self._edge_scroll_timer.timeout.connect(self._on_edge_scroll_tick)
        self._on_close = on_close
        self._on_pause_request = on_pause_request
        self._on_start_request = on_start_request
        self._on_seek_request = on_seek_request
        self._source_active = bool(initial_active)
        self._source_mode = str(initial_mode)
        self._studio_mode: Optional[str] = (
            str(initial_mode)
            if (on_pause_request is not None or on_start_request is not None or str(initial_mode) != "live")
            else None
        )
        self._cleaned_up = False
        self._external_pause_signal.connect(self._on_external_pause)
        self._external_state_signal.connect(self._on_external_state)
        self._external_replay_data_signal.connect(self._on_external_replay_data)
        self._notes_tailer = NotesTailer(notes_path) if notes_path else None
        self._last_notes_poll = 0.0
        self._rendered_notes: list[dict] = []
        self._note_vb = None
        self._note_strip_active = False

        pg.setConfigOptions(antialias=False, useOpenGL=False, background="#080d18", foreground="#cbd5e1")
        AdaptiveLegendItem = _make_adaptive_legend_class(pg)
        FastRelMsAxis, FastLeftAxis, FastOverlayGrid, FastWaveformItem = _make_fast_render_classes(pg)

        self.setWindowTitle(window_title)
        icon = get_app_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)
        self.setStyleSheet("QWidget { background-color: #080d18; color: #e2e8f0; }")
        self.resize(1200, 240 + 260 * len(groups))

        self._ts_val_lbl = QtWidgets.QLabel("--")
        self._win_val_lbl = QtWidgets.QLabel("--")
        total_str = (
            self._fmt_rel_time(reader.total_duration_ms)
            if reader.total_duration_ms is not None
            else "--"
        )
        self._total_val_lbl = QtWidgets.QLabel(total_str)
        self._ts_val_lbl.setMinimumWidth(165)
        self._win_val_lbl.setMinimumWidth(145)
        self._total_val_lbl.setMinimumWidth(75)
        self._last_hdr_update = 0.0
        self._zoom_dirty = False
        self._reset_view_btn = QtWidgets.QPushButton("Reset View")
        self._reset_view_btn.setToolTip(
            "Reset time window size and jump back to the current playhead\n"
            "(Zoom: pinch or Ctrl+wheel; When paused: ←/→ or scroll to pan, click or drag plot to scrub playhead)"
        )
        self._reset_view_btn.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self._reset_view_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self._reset_view_btn.clicked.connect(self._reset_view)
        self._reset_view_btn.setStyleSheet(
            "QPushButton { background: #162033; color: #e2e8f0; border: 1px solid #2b3d5c; "
            "border-radius: 6px; padding: 5px 12px; font-weight: 600; font-size: 12px; } "
            "QPushButton:hover { background: #1e2d47; border-color: #38bdf8; color: #38bdf8; }"
        )

        self._pause_btn = QtWidgets.QPushButton("⏸  Pause  [Space]")
        self._pause_btn.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self._pause_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self._pause_btn.clicked.connect(self._toggle_pause)
        self._rendered_toolbar_state = None
        self._rendered_generation = reader.stream_generation
        self._last_wb = None
        self._last_playhead_ms: Optional[float] = None
        self._last_total_override = reader.total_duration_ms
        self._x_origin: Optional[float] = reader.first_ts_ms
        self._row_ratios: list[float] = [1.0 / max(1, len(groups))] * max(1, len(groups))
        self._syncing_legends = False
        self._resizing_h_idx: Optional[int] = None
        self._hover_h_idx: Optional[int] = None
        self._update_toolbar_status()

        root_layout = QtWidgets.QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        header_frame = QtWidgets.QFrame()
        header_frame.setStyleSheet("QFrame { background-color: #0f172a; border-bottom: 1px solid #1e293b; }")
        header_layout = QtWidgets.QHBoxLayout(header_frame)
        header_layout.setContentsMargins(16, 8, 16, 8)
        header_layout.setSpacing(24)

        header_layout.addWidget(self._make_field("TIMESTAMP:", self._ts_val_lbl, "#e2e8f0"))
        header_layout.addWidget(self._make_field("WINDOW:", self._win_val_lbl, "#38bdf8"))
        header_layout.addWidget(self._make_field("TOTAL:", self._total_val_lbl, "#94a3b8"))
        header_layout.addStretch(1)
        header_layout.addWidget(self._reset_view_btn)
        header_layout.addWidget(self._pause_btn)

        self._plot_widget = pg.GraphicsLayoutWidget(show=False)
        self._plot_widget.setViewportUpdateMode(QtWidgets.QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self._plot_widget.setOptimizationFlag(QtWidgets.QGraphicsView.OptimizationFlag.DontAdjustForAntialiasing, True)
        self._plot_widget.viewport().setAttribute(QtCore.Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        if self._plot_widget.scene() is not None:
            self._plot_widget.scene().setItemIndexMethod(QtWidgets.QGraphicsScene.ItemIndexMethod.NoIndex)
        root_layout.addWidget(header_frame)
        root_layout.addWidget(self._plot_widget, 1)

        style_l = style.lower()
        self._draw_line = style_l in ("lines", "linespoints")
        self._draw_points = style_l in ("points", "linespoints")

        grid_lay = self._plot_widget.ci.layout
        grid_lay.setContentsMargins(10, 8, 10, 8)
        grid_lay.setHorizontalSpacing(10)
        grid_lay.setVerticalSpacing(10)
        grid_lay.setColumnStretchFactor(0, 0)
        grid_lay.setColumnStretchFactor(1, 1)

        self._plots = []
        self._legend_slots = []
        mixed_shared = 0 < len(self._shared) < len(groups)
        for gi, (channels, _title) in enumerate(zip(groups, titles)):
            is_shared = gi in self._shared
            grid_lay.setRowStretchFactor(gi, 0)
            legend_slot = pg.GraphicsWidget()
            legend_slot.setMinimumHeight(0)
            legend_slot.setPreferredHeight(0)
            legend_slot.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Fixed,
                QtWidgets.QSizePolicy.Policy.Ignored,
            )
            self._plot_widget.addItem(legend_slot, row=gi, col=0)
            self._legend_slots.append(legend_slot)

            p = self._plot_widget.addPlot(
                row=gi,
                col=1,
                axisItems={
                    "bottom": FastRelMsAxis(orientation="bottom"),
                    "left": FastLeftAxis(is_shared, orientation="left"),
                },
            )
            p.layout.setContentsMargins(0, 0, 0, 0)
            p.layout.setSpacing(0)
            p.layout.removeItem(p.titleLabel)
            p.titleLabel.setVisible(False)
            p.showGrid(x=False, y=False)
            p.disableAutoRange()
            p.hideButtons()
            p.setAutoVisible(x=False, y=False)
            p.getAxis("bottom").setPen(pg.mkPen("#334155"))
            p.getAxis("bottom").setTextPen(pg.mkPen("#94a3b8"))
            if gi < len(groups) - 1:
                bottom_ax = p.getAxis("bottom")
                bottom_ax.setStyle(showValues=False, tickLength=0)
                bottom_ax.setHeight(2)
            else:
                p.getAxis("bottom").setHeight(24)
            p.getAxis("left").setPen(pg.mkPen("#334155"))
            p.getAxis("left").setTextPen(pg.mkPen("#94a3b8"))
            p.setMouseEnabled(x=False, y=False)
            if not is_shared:
                p.getAxis("left").setStyle(showValues=False)
                p.getAxis("left").setWidth(56 if mixed_shared else 1)
                p.setYRange(0.0, 1.0, padding=0.02)
            elif mixed_shared:
                p.getAxis("left").setWidth(56)
            FastOverlayGrid(p.vb, p.getAxis("left"), is_shared)
            if gi == 0:
                note_vb = pg.ViewBox(enableMouse=False, enableMenu=False)
                note_vb.setFixedHeight(0)
                note_vb.setXLink(p)
                note_vb.setYRange(0.0, 1.0, padding=0)
                p.layout.addItem(note_vb, 0, 1)
                self._note_vb = note_vb
            legend = AdaptiveLegendItem(
                brush=(15, 23, 42, 255),
                pen=(30, 41, 59),
                labelTextColor="#cbd5e1",
            )
            p.legend = legend
            legend.setParentItem(legend_slot)
            legend.bindToPlot(p, legend_slot, self._plot_widget, on_adapted=self._on_legend_adapted)
            for ci, ch in enumerate(channels):
                color = PALETTE[ci % len(PALETTE)]
                dot_pm = _make_dot_pixmap(color, size=5) if self._draw_points else None
                pen = pg.mkPen(color=color, width=1) if self._draw_line else None
                ch.curve = FastWaveformItem(
                    draw_line=self._draw_line,
                    dot_pm=dot_pm,
                    x=[],
                    y=[],
                    pen=pen,
                    connect="finite",
                )
                ch.scatter = None
                p.addItem(ch.curve, ignoreBounds=True)
                p.legend.addItem(ch.curve, ch.name)
            self._plots.append(p)

        self._h_divider_hit_rects: list[Optional[tuple[float, float, float]]] = [
            None for _ in range(max(0, len(self._plots) - 1))
        ]

        self._cursor_line = QtWidgets.QGraphicsLineItem()
        cursor_pen = QtGui.QPen(QtGui.QColor("#f59e0b"), 1.6)
        cursor_pen.setCosmetic(True)
        self._cursor_line.setPen(cursor_pen)
        self._cursor_line.setZValue(1000)
        self._cursor_line.setVisible(False)
        if self._plot_widget.scene() is not None:
            self._plot_widget.scene().addItem(self._cursor_line)
        if self._plots:
            self._plots[0].vb.geometryChanged.connect(self._update_cursor_line)
            if len(self._plots) > 1:
                self._plots[-1].vb.geometryChanged.connect(self._update_cursor_line)

        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setTimerType(QtCore.Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._on_redraw_tick)

        self._paint_fallback_timer = QtCore.QTimer(self)
        self._paint_fallback_timer.setSingleShot(True)
        self._paint_fallback_timer.timeout.connect(self._on_paint_done)
        self._waiting_for_paint = False
        self._frame_t0 = time.monotonic()

        vp = self._plot_widget.viewport()
        vp.setMouseTracking(True)
        vp.installEventFilter(self)
        vp.grabGesture(QtCore.Qt.GestureType.PinchGesture)
        self._plot_widget.installEventFilter(self)
        self._timer.start(REDRAW_INTERVAL_MS)

    @staticmethod
    def _make_field(caption: str, value_widget: QtWidgets.QLabel, val_color: str) -> QtWidgets.QWidget:
        box = QtWidgets.QWidget()
        box.setStyleSheet("border: none; background: transparent;")
        lay = QtWidgets.QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        cap = QtWidgets.QLabel(caption)
        cap.setStyleSheet(
            "color: #64748b; font-size: 10px; font-weight: 700; "
            "letter-spacing: 0.6px; border: none; background: transparent;"
        )
        value_widget.setStyleSheet(
            f"color: {val_color}; font-family: Consolas, 'Courier New', monospace; "
            "font-size: 12px; font-weight: 700; border: none; background: transparent;"
        )
        lay.addWidget(cap)
        lay.addWidget(value_widget)
        return box

    @staticmethod
    def _fmt_full_ts(ms_val):
        try:
            dt = datetime.datetime.fromtimestamp(ms_val / 1000.0)
            return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
        except (ValueError, OSError, OverflowError):
            return f"{ms_val:.1f} ms"

    @staticmethod
    def _fmt_rel_time(ms_delta):
        sec = max(0.0, ms_delta / 1000.0)
        hours = int(sec // 3600)
        mins = int((sec % 3600) // 60)
        rem = sec - hours * 3600 - mins * 60
        if hours > 0:
            return f"{hours:d}:{mins:02d}:{rem:05.2f}"
        return f"{mins:02d}:{rem:05.2f}"

    def _update_toolbar_status(self):
        paused = bool(self.reader.paused)
        active = bool(self._source_active)
        state_key = (active, paused)
        if state_key == self._rendered_toolbar_state:
            return
        self._rendered_toolbar_state = state_key
        if not active:
            self._pause_btn.setText("▶  Play  [Space]")
            self._pause_btn.setStyleSheet(
                "QPushButton { background: rgba(34, 197, 94, 0.16); color: #4ade80; "
                "border: 1px solid rgba(34, 197, 94, 0.55); border-radius: 6px; "
                "padding: 5px 14px; font-weight: 600; font-size: 12px; } "
                "QPushButton:hover { background: rgba(34, 197, 94, 0.28); }"
            )
        elif paused:
            self._pause_btn.setText("▶  Resume  [Space]")
            self._pause_btn.setStyleSheet(
                "QPushButton { background: rgba(234, 179, 8, 0.16); color: #facc15; "
                "border: 1px solid rgba(234, 179, 8, 0.55); border-radius: 6px; "
                "padding: 5px 14px; font-weight: 600; font-size: 12px; } "
                "QPushButton:hover { background: rgba(234, 179, 8, 0.28); }"
            )
        else:
            self._pause_btn.setText("⏸  Pause  [Space]")
            self._pause_btn.setStyleSheet(
                "QPushButton { background: #162033; color: #e2e8f0; border: 1px solid #2b3d5c; "
                "border-radius: 6px; padding: 5px 14px; font-weight: 600; font-size: 12px; } "
                "QPushButton:hover { background: #1e2d47; border-color: #38bdf8; }"
            )

    def set_paused(self, paused: bool) -> None:
        """Thread-safe pause state sync from an external controller (e.g. Studio)."""
        self.reader.set_paused(bool(paused))
        if not self._cleaned_up:
            self._external_pause_signal.emit(bool(paused))

    def set_stream_state(self, mode: str, paused: bool) -> None:
        """Thread-safe stream mode and pause state sync from an external controller."""
        self.reader.set_paused(bool(paused))
        if not self._cleaned_up:
            self._external_state_signal.emit(str(mode), bool(paused))

    def load_replay_data(
        self,
        first_ts_s: Optional[float],
        last_ts_s: Optional[float],
        current_elapsed_s: float,
        header: Optional[str],
        lines: list[str],
    ) -> None:
        """Thread-safe delivery of a full replay trace for bidirectional scrolling & seeking."""
        if not self._cleaned_up:
            self._external_replay_data_signal.emit(
                (first_ts_s, last_ts_s, current_elapsed_s, header, lines if isinstance(lines, list) else list(lines))
            )

    def _on_external_replay_data(self, payload) -> None:
        if self._cleaned_up:
            return
        first_ts_s, last_ts_s, current_elapsed_s, header, lines = payload
        self.reader.load_recording_rows(
            first_ts_s,
            last_ts_s,
            current_elapsed_s,
            header,
            lines,
            bump_generation=True,
        )
        self._pan_x_hi = None
        self._schedule_redraw_now()

    def _on_external_pause(self, paused: bool) -> None:
        if self._cleaned_up:
            return
        if not paused and self._is_recording_mode():
            self._pan_x_hi = None
        if not paused and self._cursor_dragging:
            self._cancel_cursor_drag()
        self._update_toolbar_status()
        self._schedule_redraw_now()

    def _on_external_state(self, mode: str, paused: bool) -> None:
        if self._cleaned_up:
            return
        self._source_mode = mode
        self._source_active = mode != "idle"
        if mode in ("live", "replay"):
            self._studio_mode = mode
        if not paused and mode == "replay":
            self._pan_x_hi = None
        if (not paused or mode != "replay") and self._cursor_dragging:
            self._cancel_cursor_drag()
        self._update_toolbar_status()
        self._schedule_redraw_now()

    def _toggle_pause(self):
        if self._cursor_dragging:
            self._cancel_cursor_drag()
        if not self._source_active:
            if self._on_start_request is not None:
                started = bool(self._on_start_request())
                if started:
                    self._source_active = True
                    self.reader.set_paused(False)
                    if self._is_recording_mode():
                        self._pan_x_hi = None
            self._update_toolbar_status()
            self._schedule_redraw_now()
            return
        desired = not bool(self.reader.paused)
        if self._on_pause_request is not None:
            actual = self._on_pause_request(desired)
            if actual is not None:
                self.reader.set_paused(bool(actual))
        else:
            self.reader.set_paused(desired)
        if not self.reader.paused and self._is_recording_mode():
            self._pan_x_hi = None
        self._update_toolbar_status()
        self._schedule_redraw_now()

    def _is_recording_mode(self) -> bool:
        if self.reader.realtime_replay or self.reader.preloaded_recording:
            return True
        with self.reader.lock:
            src_mode = self.reader.source_mode
            has_total = self.reader.total_duration_ms is not None
        if src_mode == "replay" or self._source_mode == "replay":
            return True
        if has_total:
            return True
        if src_mode == "live" or self._studio_mode == "live":
            return False
        return bool(self.reader.eof)

    def _is_playback_running(self) -> bool:
        if not self._is_recording_mode():
            return False
        if self.reader.paused:
            return False
        if self.reader.realtime_replay:
            return not self.reader.eof
        return bool(self._source_active) and not self.reader.eof

    def _can_seek(self) -> bool:
        return (
            self._is_recording_mode()
            and not self._is_playback_running()
            and (self._on_seek_request is not None or self.reader.realtime_replay or self.reader.eof)
        )

    def _should_prune(self) -> bool:
        if self._width_ms is None or self._is_recording_mode():
            return False
        with self.reader.lock:
            src_mode = self.reader.source_mode
        if src_mode == "live" or self._studio_mode == "live":
            return True
        return (time.monotonic() - self._opened_at) >= 10.0

    def _data_bounds(self) -> Optional[tuple[float, float]]:
        xmin = math.inf
        xmax = -math.inf
        with self.reader.lock:
            for channels in self._groups:
                for ch in channels:
                    if ch._xs_owner is not None:
                        continue
                    n = ch.count
                    if n:
                        x0 = float(ch.xs[0])
                        x1 = float(ch.xs[n - 1])
                        if x0 < xmin:
                            xmin = x0
                        if x1 > xmax:
                            xmax = x1
        if not math.isfinite(xmax):
            return None
        if not math.isfinite(xmin):
            xmin = xmax
        return (xmin, xmax)

    @staticmethod
    def _clamp_window_hi(target_hi: float, width: float, bounds: tuple[float, float]) -> float:
        xmin, xmax = bounds
        if (xmax - xmin) <= width:
            return xmax
        return min(xmax, max(xmin + width, target_hi))

    def _window_bounds(self, playhead_ms: Optional[float] = None, use_given_playhead: bool = False):
        bounds = self._data_bounds()
        if bounds is None:
            return None
        xmin, xmax = bounds
        if self._width_ms is None:
            return (xmin, xmax)
        recording = self._is_recording_mode()
        if self._pan_x_hi is not None and recording:
            x_hi = min(xmax, max(xmin, self._pan_x_hi))
            self._pan_x_hi = x_hi
            return (x_hi - self._width_ms, x_hi)
        if recording:
            ph = playhead_ms if use_given_playhead else None
            if not use_given_playhead:
                with self.reader.lock:
                    ph = self.reader.playhead_ms
            if ph is not None and math.isfinite(ph):
                x_hi = min(xmax, max(xmin, ph))
                return (x_hi - self._width_ms, x_hi)
        return (xmax - self._width_ms, xmax)

    @staticmethod
    def _set_y_transform(item, lo, hi):
        item.setYBounds(lo, hi)

    def _schedule_redraw_now(self) -> None:
        self._zoom_dirty = True
        if not self._waiting_for_paint:
            self._timer.start(0)

    def _cancel_cursor_drag(self) -> None:
        self._edge_scroll_timer.stop()
        self._edge_scroll_dir = 0
        self._edge_scroll_speed = 0.0
        self._cursor_dragging = False
        self._cursor_ms = None
        self._cursor_vp_pos = None
        self._update_cursor_line()

    def _reset_view(self) -> None:
        if self._cleaned_up:
            return
        self._cancel_cursor_drag()
        self._width_ms = self._initial_width_ms
        self._pan_x_hi = None
        self._schedule_redraw_now()

    def _adjust_time_window(self, factor: float, anchor_ratio: Optional[float] = None) -> None:
        if self._cleaned_up or not math.isfinite(factor) or factor <= 0.0:
            return
        cur_w = self._width_ms
        if cur_w is None:
            if self._last_wb is not None and self._last_wb[1] > self._last_wb[0]:
                cur_w = self._last_wb[1] - self._last_wb[0]
            else:
                cur_w = 5000.0
        bounds = self._data_bounds()
        recording = self._is_recording_mode()
        avail = (bounds[1] - bounds[0]) if bounds is not None else 0.0
        max_w = max(300_000.0, avail) if recording else 300_000.0
        new_w = min(max_w, max(250.0, float(cur_w) * factor))
        if self._width_ms is not None and abs(new_w - self._width_ms) < 1e-3:
            return

        wb = self._last_wb if self._last_wb is not None else (self._window_bounds() if bounds is not None else None)
        self._width_ms = new_w
        if recording and wb is not None and bounds is not None:
            following_playhead = self._pan_x_hi is None and (
                (not self.reader.paused and not self.reader.eof) or anchor_ratio is None
            )
            if not following_playhead:
                r = 0.5 if anchor_ratio is None else max(0.0, min(1.0, float(anchor_ratio)))
                wb_lo, wb_hi = wb
                span = max(1e-3, wb_hi - wb_lo)
                anchor_x = wb_lo + r * span
                target_hi = anchor_x + (1.0 - r) * new_w
                clamped_hi = min(bounds[1], max(bounds[0], target_hi))
                if clamped_hi >= bounds[1] - 1e-3 and not self.reader.paused and not self.reader.eof:
                    self._pan_x_hi = None
                else:
                    self._pan_x_hi = clamped_hi
        elif not recording:
            self._pan_x_hi = None
        self._schedule_redraw_now()

    def _pan_time_window(self, delta_ms: float) -> None:
        if self._cleaned_up or not self._is_recording_mode() or self._is_playback_running():
            return
        bounds = self._data_bounds()
        if bounds is None:
            return
        xmin, xmax = bounds
        cur_w = self._width_ms
        if cur_w is None or (xmax - xmin) <= cur_w:
            return
        if self._pan_x_hi is not None:
            raw_hi = self._pan_x_hi
        elif self._last_wb is not None:
            raw_hi = self._last_wb[1]
        else:
            wb = self._window_bounds()
            raw_hi = wb[1] if wb is not None else xmax
        cur_hi = min(xmax, max(xmin, raw_hi))
        lo_lim = min(cur_hi, xmin + cur_w) if (xmax - xmin) > cur_w else min(cur_hi, xmax)
        new_hi = min(xmax, max(lo_lim, cur_hi + delta_ms))
        if abs(new_hi - cur_hi) < 1e-3:
            return
        self._pan_x_hi = new_hi
        self._schedule_redraw_now()

    def _on_legend_adapted(self) -> None:
        if self._cleaned_up or self._syncing_legends:
            return
        self._sync_legend_column_width()

    def _adapt_all_legends(self) -> None:
        if self._cleaned_up or not self._plots:
            return
        self._syncing_legends = True
        try:
            for p in self._plots:
                if p.legend is not None and hasattr(p.legend, "adaptToPlot"):
                    p.legend.adaptToPlot(notify=False)
        finally:
            self._syncing_legends = False
        self._sync_legend_column_width()

    def _sync_legend_column_width(self) -> None:
        if self._cleaned_up or self._syncing_legends or not self._plots:
            return
        legends = [p.legend for p in self._plots if p.legend is not None]
        if not legends:
            return
        self._syncing_legends = True
        try:
            shared_w = max((leg.preferredCardWidth() for leg in legends), default=120.0)
            shared_w = max(80.0, shared_w)
            for leg in legends:
                leg.setCardWidth(shared_w)
            for slot in self._legend_slots:
                slot.setMinimumWidth(shared_w)
                slot.setMaximumWidth(shared_w)
                slot.setPreferredWidth(shared_w)
                slot.updateGeometry()
            self._plot_widget.ci.layout.activate()
        finally:
            self._syncing_legends = False
        self._update_divider_hit_rects()
        self._update_cursor_line()

    def _plot_row_overhead(self, gi: int) -> float:
        p = self._plots[gi]
        note_ov = 28.0 if (gi == 0 and self._note_vb is not None and self._note_strip_active) else 0.0
        bot_ax = p.getAxis("bottom")
        bot_ov = float(bot_ax.height()) if bot_ax is not None and bot_ax.isVisible() else 0.0
        return note_ov + bot_ov

    def _apply_layout_split(self) -> None:
        if self._cleaned_up or not self._plots:
            return
        vp_h = float(self._plot_widget.viewport().height())
        if vp_h <= 40.0:
            return
        grid_lay = self._plot_widget.ci.layout
        n = len(self._plots)
        _, top_m, _, bot_m = grid_lay.getContentsMargins()
        v_gap = float(grid_lay.verticalSpacing())
        total_grid_h = max(60.0 * n, vp_h - float(top_m) - float(bot_m) - v_gap * max(0, n - 1))
        overheads = [self._plot_row_overhead(i) for i in range(n)]
        avail_vb_h = max(40.0 * n, total_grid_h - sum(overheads))
        min_vb = min(60.0, avail_vb_h / max(1, n))

        if len(self._row_ratios) != n or sum(self._row_ratios) <= 0:
            self._row_ratios = [1.0 / n] * n
        vb_heights = [max(min_vb, avail_vb_h * r) for r in self._row_ratios]
        extra = sum(vb_heights) - avail_vb_h
        if abs(extra) > 1e-3:
            adjustable = [i for i, h in enumerate(vb_heights) if h > min_vb + 1e-3]
            if adjustable:
                slack_sum = sum(vb_heights[i] - min_vb for i in adjustable)
                if slack_sum > 1e-3 and extra > 0:
                    scale = max(0.0, (slack_sum - extra) / slack_sum)
                    for i in adjustable:
                        vb_heights[i] = min_vb + (vb_heights[i] - min_vb) * scale
                elif extra < 0:
                    for i in range(n):
                        vb_heights[i] += (-extra) / n
        if avail_vb_h > 0:
            self._row_ratios = [h / avail_vb_h for h in vb_heights]

        for gi in range(n):
            row_h = float(int(round(vb_heights[gi] + overheads[gi])))
            grid_lay.setRowMinimumHeight(gi, row_h)
            grid_lay.setRowPreferredHeight(gi, row_h)
            grid_lay.setRowMaximumHeight(gi, row_h)
        grid_lay.activate()
        self._adapt_all_legends()

    def _drag_horizontal_split(self, i: int, scene_y: float) -> None:
        if i < 0 or i + 1 >= len(self._plots):
            return
        top_y = float(self._plots[i].sceneBoundingRect().top())
        note_ov = 28.0 if (i == 0 and self._note_vb is not None and self._note_strip_active) else 0.0
        vb_i = float(self._plots[i].vb.size().height())
        vb_next = float(self._plots[i + 1].vb.size().height())
        pair_vb = vb_i + vb_next
        if pair_vb <= 0:
            return
        min_vb = min(60.0, pair_vb * 0.45)
        new_vb_i = max(min_vb, min(pair_vb - min_vb, (scene_y - 0.5) - top_y - note_ov))
        new_vb_next = pair_vb - new_vb_i
        total_vb = sum(max(1.0, float(p.vb.size().height())) for p in self._plots)
        if total_vb <= 0:
            return
        ratios = [max(1.0, float(p.vb.size().height())) / total_vb for p in self._plots]
        ratios[i] = new_vb_i / total_vb
        ratios[i + 1] = new_vb_next / total_vb
        s = sum(ratios)
        if s > 0:
            self._row_ratios = [r / s for r in ratios]
        self._apply_layout_split()

    def _update_divider_hit_rects(self) -> None:
        if self._cleaned_up or not self._plots or not hasattr(self, "_h_divider_hit_rects"):
            return
        for i in range(len(self._h_divider_hit_rects)):
            if i + 1 >= len(self._plots):
                self._h_divider_hit_rects[i] = None
                continue
            p_above = self._plots[i]
            vb = p_above.vb
            vb_sz = vb.size()
            if vb_sz.width() <= 0 or vb_sz.height() <= 0:
                self._h_divider_hit_rects[i] = None
                continue
            vb_r = vb.mapRectToScene(QtCore.QRectF(0.0, 0.0, float(vb_sz.width()), float(vb_sz.height())))
            x_left = float(vb_r.left())
            x_right = float(vb_r.right())
            h_y = float(vb_r.bottom()) + 0.5
            self._h_divider_hit_rects[i] = (x_left, x_right, h_y)

    def _hit_test_divider(self, scene_pos: QtCore.QPointF) -> Optional[int]:
        if not self._plots or not hasattr(self, "_h_divider_hit_rects"):
            return None
        tol = 6.0
        sx = float(scene_pos.x())
        sy = float(scene_pos.y())
        for i, hit in enumerate(self._h_divider_hit_rects):
            if hit is not None:
                x1, x2, hy = hit
                if abs(sy - hy) <= tol and (x1 - tol) <= sx <= (x2 + tol):
                    return i
        return None

    def _plot_x_span(self) -> Optional[tuple[float, float, float, float]]:
        if not self._plots:
            return None
        top_vb = self._plots[0].vb
        bot_vb = self._plots[-1].vb
        top_sz = top_vb.size()
        bot_sz = bot_vb.size()
        if top_sz.width() <= 0 or top_sz.height() <= 0 or bot_sz.height() <= 0:
            return None
        top_rect = top_vb.mapRectToScene(
            QtCore.QRectF(0.0, 0.0, float(top_sz.width()), float(top_sz.height()))
        )
        bot_rect = bot_vb.mapRectToScene(
            QtCore.QRectF(0.0, 0.0, float(bot_sz.width()), float(bot_sz.height()))
        )
        if top_rect.width() <= 0 or bot_rect.height() <= 0:
            return None
        return (top_rect.left(), top_rect.right(), top_rect.top(), bot_rect.bottom())

    def _cursor_ms_from_scene_x(
        self, scene_x: float, wb_override: Optional[tuple[float, float]] = None
    ) -> Optional[float]:
        if not self._plots:
            return None
        wb = wb_override if wb_override is not None else (
            self._last_wb if self._last_wb is not None else self._window_bounds()
        )
        bounds = self._data_bounds()
        span_rect = self._plot_x_span()
        if wb is None or bounds is None or span_rect is None:
            return None
        vb_left, vb_right, _, _ = span_rect
        vb_w = vb_right - vb_left
        if vb_w <= 0:
            return None
        rel = max(0.0, min(1.0, float((scene_x - vb_left) / vb_w)))
        raw_ms = wb[0] + rel * (wb[1] - wb[0])
        return min(bounds[1], max(bounds[0], raw_ms))

    def _update_cursor_line(
        self,
        wb: Optional[tuple[float, float]] = None,
        playhead_ms: Optional[float] = None,
        use_given_playhead: bool = False,
    ) -> None:
        if self._cleaned_up or not self._plots or not hasattr(self, "_cursor_line"):
            return
        if wb is None:
            wb = self._last_wb if self._last_wb is not None else self._window_bounds()
        if self._cursor_dragging and self._cursor_ms is not None:
            show = True
            cursor_ms = self._cursor_ms
        else:
            recording = self._is_recording_mode()
            if use_given_playhead:
                ph = playhead_ms
            elif self._last_playhead_ms is not None:
                ph = self._last_playhead_ms
            else:
                with self.reader.lock:
                    ph = self.reader.playhead_ms
            show = bool(recording and ph is not None and math.isfinite(ph))
            if show and self._width_ms is not None and self._pan_x_hi is None and wb is not None:
                cursor_ms = wb[1]
            else:
                cursor_ms = ph
        if not show or wb is None or cursor_ms is None or not math.isfinite(cursor_ms):
            self._cursor_line.setVisible(False)
            return
        x_lo, x_hi = wb
        span = max(1e-3, x_hi - x_lo)
        frac = (cursor_ms - x_lo) / span
        if frac < -1e-3 or frac > 1.0 + 1e-3:
            self._cursor_line.setVisible(False)
            return
        frac = max(0.0, min(1.0, frac))
        span_rect = self._plot_x_span()
        if span_rect is None:
            self._cursor_line.setVisible(False)
            return
        vb_left, vb_right, top_y, bot_y = span_rect
        x_px = vb_left + frac * (vb_right - vb_left)
        self._cursor_line.setLine(x_px, top_y, x_px, bot_y)
        self._cursor_line.setVisible(True)

    def _update_edge_scroll_state(self, scene_x: float) -> None:
        if not self._plots or not self._cursor_dragging:
            self._edge_scroll_dir = 0
            self._edge_scroll_speed = 0.0
            self._edge_scroll_timer.stop()
            return
        span_rect = self._plot_x_span()
        if span_rect is None:
            self._edge_scroll_dir = 0
            self._edge_scroll_speed = 0.0
            self._edge_scroll_timer.stop()
            return
        vb_left, vb_right, _, _ = span_rect
        w = float(vb_right - vb_left)
        if w <= 0:
            self._edge_scroll_dir = 0
            self._edge_scroll_speed = 0.0
            self._edge_scroll_timer.stop()
            return
        edge_zone = min(32.0, w * 0.06)
        cur_w = self._width_ms
        if cur_w is None:
            cur_w = (self._last_wb[1] - self._last_wb[0]) if self._last_wb is not None else 5000.0
        cur_w = max(100.0, float(cur_w))
        if scene_x <= vb_left + edge_zone:
            depth = (vb_left + edge_zone) - scene_x
            norm = depth / max(1.0, edge_zone)
            self._edge_scroll_dir = -1
            self._edge_scroll_speed = cur_w * (0.25 + 1.0 * min(3.5, norm ** 1.35))
        elif scene_x >= vb_right - edge_zone:
            depth = scene_x - (vb_right - edge_zone)
            norm = depth / max(1.0, edge_zone)
            self._edge_scroll_dir = 1
            self._edge_scroll_speed = cur_w * (0.25 + 1.0 * min(3.5, norm ** 1.35))
        else:
            self._edge_scroll_dir = 0
            self._edge_scroll_speed = 0.0

        if self._edge_scroll_dir != 0:
            if not self._edge_scroll_timer.isActive():
                self._last_edge_tick = time.monotonic()
                self._edge_scroll_timer.start()
        else:
            self._edge_scroll_timer.stop()

    def _on_edge_scroll_tick(self) -> None:
        if self._cleaned_up or not self._cursor_dragging or self._edge_scroll_dir == 0:
            self._edge_scroll_timer.stop()
            return
        bounds = self._data_bounds()
        cur_w = self._width_ms
        if bounds is None or cur_w is None:
            return
        xmin, xmax = bounds
        now = time.monotonic()
        dt = min(0.1, max(0.001, now - self._last_edge_tick))
        self._last_edge_tick = now
        delta_ms = self._edge_scroll_dir * self._edge_scroll_speed * dt
        cur_hi = (
            self._pan_x_hi
            if self._pan_x_hi is not None
            else (self._last_wb[1] if self._last_wb is not None else xmax)
        )
        cur_hi = min(xmax, max(xmin, cur_hi))
        lo_lim = min(cur_hi, xmin + cur_w) if (xmax - xmin) > cur_w else min(cur_hi, xmin)
        new_hi = min(xmax, max(lo_lim, cur_hi + delta_ms))
        moved = abs(new_hi - cur_hi) >= 1e-3
        if moved:
            self._pan_x_hi = new_hi
            wb_next = (new_hi - cur_w, new_hi)
            if self._cursor_vp_pos is not None:
                scene_pos = self._plot_widget.mapToScene(self._cursor_vp_pos.toPoint())
                target_ms = self._cursor_ms_from_scene_x(scene_pos.x(), wb_override=wb_next)
                if target_ms is not None:
                    self._cursor_ms = target_ms
                    self._ts_val_lbl.setText(self._fmt_full_ts(target_ms))
            self._schedule_redraw_now()
            self._update_cursor_line(wb=wb_next)

    def _seek_to_ms(self, target_ms: float, keep_camera: bool = True) -> None:
        if self._cleaned_up or not self._can_seek():
            return
        bounds = self._data_bounds()
        if bounds is None:
            return
        xmin, xmax = bounds
        clamped_ms = min(xmax, max(xmin, float(target_ms)))
        with self.reader.lock:
            ref_first = self.reader.first_ts_ms if self.reader.first_ts_ms is not None else xmin
        actual_ms = clamped_ms
        if self._on_seek_request is not None:
            elapsed_sec = max(0.0, (clamped_ms - ref_first) / 1000.0)
            res = self._on_seek_request(elapsed_sec)
            if res is not None and math.isfinite(res):
                actual_ms = min(xmax, max(xmin, ref_first + float(res) * 1000.0))
        elif self.reader.realtime_replay:
            s_ms = self.reader.seek_to_ms(clamped_ms)
            if s_ms is not None and math.isfinite(s_ms):
                actual_ms = s_ms
        self.reader.set_playhead_ms(actual_ms, bump_seek_seq=True)
        if not keep_camera:
            self._pan_x_hi = None
        self._schedule_redraw_now()

    def _plot_vb_rect(self, vp_pos: Optional[QtCore.QPointF] = None) -> Optional[QtCore.QRectF]:
        if not self._plots:
            return None
        if vp_pos is not None:
            scene_pos = self._plot_widget.mapToScene(vp_pos.toPoint())
            for p in self._plots:
                rect = p.vb.sceneBoundingRect()
                if rect.top() <= scene_pos.y() <= rect.bottom() and rect.width() > 0:
                    return rect
        rect = self._plots[0].vb.sceneBoundingRect()
        return rect if rect.width() > 0 else None

    def _plot_px_width(self) -> float:
        rect = self._plot_vb_rect()
        if rect is not None and rect.width() > 0:
            return float(rect.width())
        return float(max(200, self._plot_widget.viewport().width()))

    def _anchor_ratio_from_pos(self, vp_pos: Optional[QtCore.QPointF]) -> Optional[float]:
        if vp_pos is None:
            return None
        rect = self._plot_vb_rect(vp_pos)
        if rect is None or rect.width() <= 0:
            return None
        scene_pos = self._plot_widget.mapToScene(vp_pos.toPoint())
        rel = (scene_pos.x() - rect.left()) / rect.width()
        return max(0.0, min(1.0, float(rel)))

    def _map_event_pos_to_viewport(self, obj, event) -> Optional[QtCore.QPointF]:
        vp = self._plot_widget.viewport()
        if hasattr(event, "position"):
            pos = event.position()
            if obj is vp:
                return pos
            if isinstance(obj, QtWidgets.QWidget):
                return QtCore.QPointF(obj.mapTo(vp, pos.toPoint()))
        if hasattr(event, "globalPosition"):
            return QtCore.QPointF(vp.mapFromGlobal(event.globalPosition().toPoint()))
        return None

    def eventFilter(self, obj, event):
        vp = self._plot_widget.viewport()
        if (obj is vp or obj is self._plot_widget) and not self._cleaned_up:
            etype = event.type()
            if obj is vp and etype == QtCore.QEvent.Type.Resize:
                self._apply_layout_split()
                return super().eventFilter(obj, event)
            if (
                obj is vp
                and etype in (QtCore.QEvent.Type.MouseButtonPress, QtCore.QEvent.Type.MouseButtonDblClick)
                and event.button() == QtCore.Qt.MouseButton.LeftButton
            ):
                scene_pos = self._plot_widget.mapToScene(event.position().toPoint())
                hit_h = self._hit_test_divider(scene_pos)
                if hit_h is not None:
                    self._resizing_h_idx = hit_h
                    self._hover_h_idx = hit_h
                    vp.setCursor(QtCore.Qt.CursorShape.SplitVCursor)
                    event.accept()
                    return True
                if self._can_seek():
                    span_rect = self._plot_x_span()
                    if (
                        span_rect is not None
                        and span_rect[0] <= scene_pos.x() <= span_rect[1]
                        and span_rect[2] <= scene_pos.y() <= span_rect[3]
                    ):
                        target_ms = self._cursor_ms_from_scene_x(scene_pos.x())
                        if target_ms is not None:
                            self._cursor_dragging = True
                            self._cursor_vp_pos = QtCore.QPointF(event.position())
                            self._cursor_ms = target_ms
                            if self._pan_x_hi is None and self._last_wb is not None:
                                self._pan_x_hi = self._last_wb[1]
                            self._update_edge_scroll_state(scene_pos.x())
                            self._update_cursor_line()
                            self._ts_val_lbl.setText(self._fmt_full_ts(target_ms))
                            event.accept()
                            return True
            elif obj is vp and etype == QtCore.QEvent.Type.MouseMove:
                scene_pos = self._plot_widget.mapToScene(event.position().toPoint())
                if self._resizing_h_idx is not None:
                    self._drag_horizontal_split(self._resizing_h_idx, float(scene_pos.y()))
                    event.accept()
                    return True
                if self._cursor_dragging:
                    if not self._can_seek():
                        self._cancel_cursor_drag()
                        return super().eventFilter(obj, event)
                    self._cursor_vp_pos = QtCore.QPointF(event.position())
                    target_ms = self._cursor_ms_from_scene_x(scene_pos.x())
                    if target_ms is not None:
                        self._cursor_ms = target_ms
                        self._ts_val_lbl.setText(self._fmt_full_ts(target_ms))
                    self._update_edge_scroll_state(scene_pos.x())
                    self._update_cursor_line()
                    event.accept()
                    return True
                hit_h = self._hit_test_divider(scene_pos)
                if hit_h != self._hover_h_idx:
                    self._hover_h_idx = hit_h
                    if hit_h is not None:
                        vp.setCursor(QtCore.Qt.CursorShape.SplitVCursor)
                    else:
                        vp.unsetCursor()
            elif (
                obj is vp
                and etype == QtCore.QEvent.Type.MouseButtonRelease
                and event.button() == QtCore.Qt.MouseButton.LeftButton
            ):
                if self._resizing_h_idx is not None:
                    self._resizing_h_idx = None
                    scene_pos = self._plot_widget.mapToScene(event.position().toPoint())
                    hit_h = self._hit_test_divider(scene_pos)
                    self._hover_h_idx = hit_h
                    if hit_h is not None:
                        vp.setCursor(QtCore.Qt.CursorShape.SplitVCursor)
                    else:
                        vp.unsetCursor()
                    event.accept()
                    return True
                if self._cursor_dragging:
                    self._edge_scroll_timer.stop()
                    self._edge_scroll_dir = 0
                    self._edge_scroll_speed = 0.0
                    scene_pos = self._plot_widget.mapToScene(event.position().toPoint())
                    target_ms = self._cursor_ms_from_scene_x(scene_pos.x())
                    if target_ms is None:
                        target_ms = self._cursor_ms
                    self._cursor_dragging = False
                    self._cursor_ms = None
                    self._cursor_vp_pos = None
                    if target_ms is not None and self._can_seek():
                        self._pan_x_hi = None
                        self._seek_to_ms(target_ms, keep_camera=False)
                    else:
                        self._update_cursor_line()
                    event.accept()
                    return True
            elif obj is vp and etype == QtCore.QEvent.Type.Leave:
                if self._resizing_h_idx is None and self._hover_h_idx is not None:
                    self._hover_h_idx = None
                    vp.unsetCursor()
            elif etype == QtCore.QEvent.Type.NativeGesture:
                zoom_type = getattr(QtCore.Qt.NativeGestureType, "ZoomNativeGesture", None)
                if zoom_type is not None and event.gestureType() == zoom_type:
                    self._last_native_zoom_at = time.monotonic()
                    val = max(-1.0, min(1.0, float(event.value())))
                    pos = self._map_event_pos_to_viewport(obj, event)
                    self._adjust_time_window(math.exp(-val), anchor_ratio=self._anchor_ratio_from_pos(pos))
                    event.accept()
                    return True
            elif etype == QtCore.QEvent.Type.Gesture:
                pinch = event.gesture(QtCore.Qt.GestureType.PinchGesture)
                if pinch is not None:
                    if (time.monotonic() - self._last_native_zoom_at) >= 0.1:
                        sf = float(pinch.scaleFactor())
                        if math.isfinite(sf) and sf > 0.0 and abs(sf - 1.0) > 1e-4:
                            pos = QtCore.QPointF(vp.mapFromGlobal(pinch.centerPoint().toPoint()))
                            self._adjust_time_window(1.0 / sf, anchor_ratio=self._anchor_ratio_from_pos(pos))
                    event.accept(QtCore.Qt.GestureType.PinchGesture)
                    return True
            elif etype == QtCore.QEvent.Type.Wheel:
                ad = event.angleDelta()
                pd = event.pixelDelta()
                if not (event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier):
                    pos = self._map_event_pos_to_viewport(obj, event)
                    if pos is not None:
                        scene_pos = self._plot_widget.mapToScene(pos.toPoint())
                        for p in self._plots:
                            leg = p.legend
                            if leg is not None and leg.isVisible() and leg.sceneBoundingRect().contains(scene_pos):
                                if hasattr(leg, "isScrollable") and leg.isScrollable():
                                    dy = float(pd.y()) if pd.y() != 0 else (float(ad.y()) / 120.0 * 36.0)
                                    if dy != 0.0:
                                        leg.scrollByPixels(-dy)
                                event.accept()
                                return True
                if event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier:
                    delta_a = ad.y() if ad.y() != 0 else ad.x()
                    delta_p = pd.y() if pd.y() != 0 else pd.x()
                    if delta_a != 0:
                        steps = float(delta_a) / 120.0
                    elif delta_p != 0:
                        steps = max(-2.0, min(2.0, float(delta_p) / 40.0))
                    else:
                        steps = 0.0
                    if steps != 0.0:
                        pos = self._map_event_pos_to_viewport(obj, event)
                        self._adjust_time_window(0.8 ** steps, anchor_ratio=self._anchor_ratio_from_pos(pos))
                elif self._is_recording_mode() and not self._is_playback_running():
                    px = pd.x() if abs(pd.x()) >= abs(pd.y()) else pd.y()
                    ad_dom = ad.x() if abs(ad.x()) >= abs(ad.y()) else ad.y()
                    cur_w = self._width_ms
                    if cur_w is not None and (px != 0 or ad_dom != 0):
                        if px != 0:
                            delta_ms = -(float(px) / max(200.0, self._plot_px_width())) * cur_w
                        else:
                            delta_ms = -(float(ad_dom) / 120.0) * (cur_w * 0.20)
                        self._pan_time_window(delta_ms)
                event.accept()
                return True
            elif obj is vp and etype == QtCore.QEvent.Type.Paint and self._waiting_for_paint:
                # Queue completion strictly after the current Paint finishes on the viewport.
                QtCore.QTimer.singleShot(0, self._on_paint_done)
        return super().eventFilter(obj, event)

    def _on_paint_done(self):
        if not self._waiting_for_paint:
            return
        self._waiting_for_paint = False
        self._paint_fallback_timer.stop()
        if not self._cleaned_up and not self._timer.isActive():
            elapsed_ms = max(0.0, (time.monotonic() - self._frame_t0) * 1000.0)
            cooldown = 0 if self._zoom_dirty else max(0, REDRAW_INTERVAL_MS - int(elapsed_ms))
            self._timer.start(cooldown)

    def _on_redraw_tick(self):
        if self._cleaned_up:
            return
        self._frame_t0 = time.monotonic()
        drew = False
        try:
            drew = bool(self._redraw())
        finally:
            if not self._cleaned_up:
                if drew and self.isVisible() and not self.isMinimized():
                    self._waiting_for_paint = True
                    self._paint_fallback_timer.start(250)
                else:
                    self._timer.start(REDRAW_INTERVAL_MS)

    def _poll_notes_file(self) -> None:
        if self._notes_tailer is None:
            return
        now = time.monotonic()
        if now - self._last_notes_poll < 0.5:
            return
        self._last_notes_poll = now
        for s_ms, e_ms, txt in self._notes_tailer.poll():
            self.reader.add_note(s_ms, e_ms, txt)

    def _sync_notes(
        self, x_lo: float, x_hi: float, x_origin: float, origin_changed: bool = False
    ) -> bool:
        notes = self.reader.get_notes()
        changed = len(notes) > len(self._rendered_notes)
        if changed and not self._note_strip_active and self._note_vb is not None:
            self._note_strip_active = True
            self._note_vb.setFixedHeight(28)
            self._apply_layout_split()

        if origin_changed:
            for entry in self._rendered_notes:
                s_rel = entry["start"] - x_origin
                e_rel = entry["end"] - x_origin
                for item in entry["plot_items"]:
                    if hasattr(item, "setRegion"):
                        item.setRegion((s_rel, e_rel))
                    else:
                        item.setValue(s_rel)
                for item in entry["strip_items"]:
                    if item is not entry["label"]:
                        item.setValue(s_rel)

        if changed:
            pg = self._pg
            line_pen = pg.mkPen((245, 158, 11, 200), width=1.2, style=QtCore.Qt.PenStyle.DashLine)
            tick_pen = pg.mkPen((245, 158, 11, 190), width=1.2)
            region_brush = pg.mkBrush(245, 158, 11, 26)
            region_pen = pg.mkPen(245, 158, 11, 80)
            label_border = pg.mkPen((245, 158, 11, 175), width=1)
            label_fill = pg.mkBrush(15, 23, 42, 230)
            label_font = QtGui.QFont("Segoe UI", 8)

            for idx in range(len(self._rendered_notes), len(notes)):
                s_ms, e_ms, txt = notes[idx]
                s_rel = s_ms - x_origin
                e_rel = e_ms - x_origin
                has_span = (e_ms - s_ms) >= 100.0
                plot_items = []
                plot_pairs = []
                for p in self._plots:
                    ln = pg.InfiniteLine(pos=s_rel, angle=90, movable=False, pen=line_pen)
                    ln.setZValue(5)
                    p.addItem(ln, ignoreBounds=True)
                    plot_items.append(ln)
                    plot_pairs.append((p, ln))
                    if has_span:
                        reg = pg.LinearRegionItem(
                            values=(s_rel, e_rel),
                            movable=False,
                            brush=region_brush,
                            pen=region_pen,
                        )
                        reg.setZValue(-10)
                        reg.setAcceptHoverEvents(False)
                        p.addItem(reg, ignoreBounds=True)
                        plot_items.append(reg)
                        plot_pairs.append((p, reg))

                strip_items = []
                label_item = None
                if self._note_vb is not None:
                    tick = pg.InfiniteLine(pos=s_rel, angle=90, movable=False, pen=tick_pen)
                    tick.setZValue(1)
                    self._note_vb.addItem(tick, ignoreBounds=True)
                    strip_items.append(tick)

                    short_txt = txt if len(txt) <= 36 else (txt[:35] + "…")
                    label_item = pg.TextItem(
                        text=short_txt,
                        color="#fde68a",
                        anchor=(0.0, 0.5),
                        border=label_border,
                        fill=label_fill,
                    )
                    label_item.setFont(label_font)
                    label_item.setPos(s_rel, 0.5)
                    label_item.setZValue(2)
                    self._note_vb.addItem(label_item, ignoreBounds=True)
                    strip_items.append(label_item)

                self._rendered_notes.append(
                    {
                        "start": s_ms,
                        "end": e_ms,
                        "plot_items": plot_items,
                        "plot_pairs": plot_pairs,
                        "strip_items": strip_items,
                        "label": label_item,
                    }
                )

        span = max(1.0, x_hi - x_lo)
        margin = span * 0.25
        for entry in self._rendered_notes:
            s_ms = entry["start"]
            e_ms = entry["end"]
            in_view = bool((e_ms >= x_lo - margin) and (s_ms <= x_hi + margin))
            for item in entry["plot_items"]:
                if item.isVisible() != in_view:
                    item.setVisible(in_view)
            for item in entry["strip_items"]:
                if item.isVisible() != in_view:
                    item.setVisible(in_view)
            lbl = entry["label"]
            if lbl is not None and in_view:
                if e_ms > s_ms and s_ms < x_lo <= e_ms:
                    lbl.setPos(x_lo - x_origin, 0.5)
                else:
                    lbl.setPos(s_ms - x_origin, 0.5)
        return changed

    def _clear_rendered_notes(self) -> None:
        for entry in self._rendered_notes:
            for p, item in entry.get("plot_pairs", ()):
                try:
                    p.removeItem(item)
                except Exception:
                    pass
            if self._note_vb is not None:
                for item in entry.get("strip_items", ()):
                    try:
                        self._note_vb.removeItem(item)
                    except Exception:
                        pass
        self._rendered_notes.clear()
        if self._note_strip_active and self._note_vb is not None:
            self._note_strip_active = False
            self._note_vb.setFixedHeight(0)
            self._apply_layout_split()

    def _redraw(self) -> bool:
        self._update_toolbar_status()
        with self.reader.lock:
            gen = self.reader.stream_generation
            reset_seq = self.reader.reset_seq
            seek_seq = self.reader.seek_seq
            playhead_ms = self.reader.playhead_ms
            total_override = self.reader.total_duration_ms
            latest_ts = self.reader.latest_ts_ms
        cleared_for_new_stream = False
        gen_changed = gen != self._rendered_generation
        if gen_changed:
            self._rendered_generation = gen
            self._rendered_reset_seq = reset_seq
            self._rendered_seek_seq = seek_seq
            self._last_wb = None
            self._x_origin = None
            self._pan_x_hi = None
            self._last_total_override = total_override
            self._clear_rendered_notes()
            for channels in self._groups:
                for ch in channels:
                    if ch.curve is not None:
                        if self._draw_line:
                            ch.curve.setFastCurve(np.empty(0), np.empty(0))
                        if self._draw_points:
                            ch.curve.clearPoints(trigger_update=not self._draw_line)
                        if not self._draw_line and not self._draw_points:
                            ch.curve.update()
            self._ts_val_lbl.setText("--")
            self._win_val_lbl.setText("--")
            self._total_val_lbl.setText(self._fmt_rel_time(total_override) if total_override is not None else "--")
            cleared_for_new_stream = True

        if reset_seq != self._rendered_reset_seq:
            self._rendered_reset_seq = reset_seq
            self._pan_x_hi = None

        seek_changed = seek_seq != self._rendered_seek_seq
        if seek_changed:
            self._rendered_seek_seq = seek_seq
            if not self.reader.paused:
                self._pan_x_hi = None

        paused_now = bool(self.reader.paused)
        if paused_now != self._last_was_paused:
            self._last_was_paused = paused_now
            if not paused_now and self._is_recording_mode():
                self._pan_x_hi = None
            self._zoom_dirty = True

        self._poll_notes_file()
        notes_pending = len(self.reader.get_notes()) > len(self._rendered_notes)
        zoom_dirty = self._zoom_dirty
        now = time.monotonic()
        if self.reader.paused and not self.reader.realtime_replay:
            if zoom_dirty or seek_changed or gen_changed:
                wb = self._window_bounds(playhead_ms=playhead_ms, use_given_playhead=True)
                if wb is None:
                    self._last_playhead_ms = playhead_ms
                    self._update_cursor_line(playhead_ms=playhead_ms, use_given_playhead=True)
                    return cleared_for_new_stream
            else:
                if notes_pending and self._last_wb is not None:
                    orig = self._x_origin if self._x_origin is not None else self._last_wb[0]
                    self._sync_notes(
                        self._last_wb[0],
                        max(self._last_wb[1], self._last_wb[0] + 1.0),
                        orig,
                        False,
                    )
                    return True
                return cleared_for_new_stream
        else:
            wb = self._window_bounds(playhead_ms=playhead_ms, use_given_playhead=True)
            if wb is None:
                self._last_playhead_ms = playhead_ms
                self._update_cursor_line(playhead_ms=playhead_ms, use_given_playhead=True)
                return cleared_for_new_stream
        total_override = self.reader.total_duration_ms
        if (
            wb == self._last_wb
            and not zoom_dirty
            and not seek_changed
            and not gen_changed
            and not notes_pending
            and total_override == self._last_total_override
            and not any(ch.dirty for channels in self._groups for ch in channels)
        ):
            if playhead_ms != self._last_playhead_ms:
                self._last_playhead_ms = playhead_ms
                self._update_cursor_line(wb=wb, playhead_ms=playhead_ms, use_given_playhead=True)
            return False
        self._zoom_dirty = False
        self._last_wb = wb
        self._last_playhead_ms = playhead_ms
        self._last_total_override = total_override
        x_lo, x_hi = wb
        if x_hi <= x_lo:
            x_hi = x_lo + 1.0
        origin = self.reader.first_ts_ms if self.reader.first_ts_ms is not None else x_lo
        origin_changed = (self._x_origin is None) or (abs(origin - self._x_origin) > 1e-3)
        if origin_changed:
            self._x_origin = origin
            for channels in self._groups:
                for ch in channels:
                    ch.curve_count = -1
                    ch.scatter_count = -1
        x_origin = self._x_origin
        x_lo_rel = x_lo - x_origin
        x_hi_rel = x_hi - x_origin

        self._sync_notes(x_lo, x_hi, x_origin, origin_changed)

        recording = self._is_recording_mode()
        show_playhead = recording and playhead_ms is not None and math.isfinite(playhead_ms)

        first_ts = self.reader.first_ts_ms if self.reader.first_ts_ms is not None else x_lo
        rel_lo = max(0.0, x_lo - first_ts)
        rel_hi = max(0.0, x_hi - first_ts)
        total_ms = (
            total_override
            if total_override is not None
            else (max(rel_hi, latest_ts - first_ts) if latest_ts is not None else rel_hi)
        )
        if zoom_dirty or seek_changed or gen_changed or (now - self._last_hdr_update) >= 0.08:
            self._last_hdr_update = now
            if self._cursor_dragging and self._cursor_ms is not None:
                ts_display = self._cursor_ms
            elif show_playhead and playhead_ms is not None:
                ts_display = playhead_ms
            else:
                ts_display = x_hi
            self._ts_val_lbl.setText(self._fmt_full_ts(ts_display))
            self._win_val_lbl.setText(f"{self._fmt_rel_time(rel_lo)} – {self._fmt_rel_time(rel_hi)}")
            self._total_val_lbl.setText(self._fmt_rel_time(total_ms))

        should_prune = self._should_prune()
        for gi, (channels, p) in enumerate(zip(self._groups, self._plots)):
            use_shared = gi in self._shared
            group_min = math.inf
            group_max = -math.inf
            pending_curve_ops = []
            pending_point_ops = []

            with self.reader.lock:
                if should_prune:
                    prune_before = x_hi - max(self._width_ms * 4.0, 60_000.0)
                    for ch in channels:
                        ch.prune(prune_before)

                cached_xs_ref = None
                cached_n = -1
                cached_i0 = 0
                cached_i1 = 0
                ch_bounds: list[Optional[tuple[int, int]]] = []
                for ch in channels:
                    n = ch.count
                    if n == 0:
                        ch_bounds.append(None)
                        continue
                    if ch.xs is cached_xs_ref and n == cached_n:
                        i0, i1 = cached_i0, cached_i1
                    else:
                        xs_view = ch.xs[:n]
                        i0 = int(np.searchsorted(xs_view, x_lo, side="left"))
                        i1 = min(n, int(np.searchsorted(xs_view, x_hi, side="right")))
                        cached_xs_ref = ch.xs
                        cached_n = n
                        cached_i0 = i0
                        cached_i1 = i1
                    ch_bounds.append((i0, i1))
                    if use_shared:
                        lo, hi = _visible_range(ch, x_lo, x_hi, i0=i0, i1=i1)
                        if lo is not None:
                            group_min = min(group_min, lo)
                            group_max = max(group_max, hi)

                for ch, bounds_pair in zip(channels, ch_bounds):
                    if bounds_pair is None:
                        continue
                    n = ch.count
                    i0, i1 = bounds_pair
                    xs = ch.xs[:n]
                    start = max(0, i0 - 1)
                    has_left = i0 > 0 and math.isfinite(ch.ys[i0 - 1])

                    if i1 <= i0 and not has_left:
                        if self._draw_line and ch.curve_count != 0:
                            pending_curve_ops.append((ch.curve, np.empty(0), np.empty(0), "all", True))
                            ch.curve_i0 = -1
                            ch.curve_i1 = -1
                            ch.curve_count = 0
                            ch.curve_tail_x = math.nan
                        if self._draw_points and ch.scatter_count != 0:
                            pending_point_ops.append((ch.curve, None, None, not self._draw_line))
                            ch.scatter_i0 = -1
                            ch.scatter_i1 = -1
                            ch.scatter_count = 0
                        ch.dirty = False
                        continue

                    s_lo = math.nan
                    s_hi = math.nan
                    y_changed = False
                    if not use_shared:
                        lo, hi = _visible_range(ch, x_lo, x_hi, i0=i0, i1=i1)
                        if lo is None:
                            if math.isfinite(ch.vmin):
                                lo, hi = ch.vmin, ch.vmax
                            else:
                                ch.dirty = False
                                continue
                        s_lo, s_hi = lo, hi
                        y_changed = (ch.rendered_y_lo != s_lo) or (ch.rendered_y_hi != s_hi)

                    xw = None
                    yw = None
                    if self._draw_line:
                        end = min(n, i1 + 1)
                        last_x = float(xs[n - 1])
                        last_y = float(ch.ys[n - 1])
                        need_right = (end == n) and math.isfinite(last_y) and last_x < x_hi
                        need_curve_update = (
                            origin_changed
                            or ch.dirty
                            or y_changed
                            or ch.curve_count != n
                            or ch.curve_i0 < 0
                            or start < ch.curve_i0
                            or end > ch.curve_i1
                            or need_right
                            or math.isfinite(ch.curve_tail_x)
                        )
                        if need_curve_update:
                            win_len = end - start
                            if need_right:
                                xf = np.empty(win_len + 1, dtype=np.float64)
                                np.subtract(xs[start:end], x_origin, out=xf[:win_len])
                                xf[win_len] = x_hi_rel
                                yf = np.empty(win_len + 1, dtype=np.float64)
                                if not use_shared:
                                    _normalize_y_into(ch.ys[start:end], s_lo, s_hi, out=yf[:win_len])
                                    yf[win_len] = float(_normalize_y(last_y, s_lo, s_hi))
                                else:
                                    yf[:win_len] = ch.ys[start:end]
                                    yf[win_len] = last_y
                                xw = xf[:win_len]
                                yw = yf[:win_len]
                                ch.curve_tail_x = x_hi
                            else:
                                xw = xs[start:end] - x_origin
                                yw = ch.ys[start:end]
                                if not use_shared:
                                    yw = _normalize_y(yw, s_lo, s_hi)
                                else:
                                    yw = yw.copy()
                                xf, yf = xw, yw
                                ch.curve_tail_x = math.nan
                            if ch.has_nan:
                                pending_curve_ops.append((ch.curve, xf, yf, "finite", False))
                            else:
                                pending_curve_ops.append((ch.curve, xf, yf, "all", True))
                            ch.curve_i0 = start
                            ch.curve_i1 = end
                            ch.curve_count = n

                    if self._draw_points:
                        if (
                            origin_changed
                            or ch.dirty
                            or y_changed
                            or ch.count != ch.scatter_count
                            or ch.scatter_i0 != i0
                            or ch.scatter_i1 != i1
                        ):
                            if not ch.has_nan:
                                if i1 > i0:
                                    if xw is not None and yw is not None:
                                        off = i0 - start
                                        end_off = i1 - start
                                        dx = xw[off:end_off]
                                        dy = yw[off:end_off]
                                    else:
                                        dx = xs[i0:i1] - x_origin
                                        dy = ch.ys[i0:i1]
                                        if not use_shared:
                                            dy = _normalize_y(dy, s_lo, s_hi)
                                        else:
                                            dy = dy.copy()
                                    pending_point_ops.append((ch.curve, dx, dy, not self._draw_line))
                                else:
                                    pending_point_ops.append((ch.curve, None, None, not self._draw_line))
                            else:
                                pres = ch.present[i0:i1]
                                if pres.any():
                                    dx = xs[i0:i1][pres] - x_origin
                                    dy = ch.ys[i0:i1][pres]
                                    if not use_shared:
                                        dy = _normalize_y(dy, s_lo, s_hi)
                                    pending_point_ops.append((ch.curve, dx, dy, not self._draw_line))
                                else:
                                    pending_point_ops.append((ch.curve, None, None, not self._draw_line))
                            ch.scatter_i0 = i0
                            ch.scatter_i1 = i1
                            ch.scatter_count = ch.count

                    if not use_shared:
                        ch.rendered_y_lo = s_lo
                        ch.rendered_y_hi = s_hi

                    ch.dirty = False

            for curve_item, xf, yf, conn_mode, skip_fin in pending_curve_ops:
                curve_item.setFastCurve(xf, yf, connect=conn_mode, skip_finite_check=skip_fin)
            for curve_item, dx, dy, trig in pending_point_ops:
                if dx is None:
                    curve_item.clearPoints(trigger_update=trig)
                else:
                    curve_item.setPoints(dx, dy, trigger_update=trig)

            if use_shared and math.isfinite(group_min) and math.isfinite(group_max):
                s_lo, s_hi = group_min, group_max
                if s_hi <= s_lo:
                    s_hi = s_lo + 1.0
                pad = (s_hi - s_lo) * 0.05
                p.setYRange(s_lo - pad, s_hi + pad, padding=0)

            p.setXRange(x_lo_rel, x_hi_rel, padding=0)
        self._update_cursor_line(wb=wb, playhead_ms=playhead_ms, use_given_playhead=True)
        return True

    def showEvent(self, event):
        super().showEvent(event)
        apply_dark_titlebar(self)
        self._apply_layout_split()

    def keyPressEvent(self, event):
        key = event.key()
        if key == QtCore.Qt.Key.Key_Space:
            self._toggle_pause()
            event.accept()
            return
        if key in (QtCore.Qt.Key.Key_Left, QtCore.Qt.Key.Key_Right):
            if self._is_recording_mode() and not self._is_playback_running() and self._width_ms is not None:
                step = self._width_ms * 0.20
                self._pan_time_window(-step if key == QtCore.Qt.Key.Key_Left else step)
            event.accept()
            return
        if key in (QtCore.Qt.Key.Key_Plus, QtCore.Qt.Key.Key_Equal):
            self._adjust_time_window(0.8)
            event.accept()
            return
        if key in (QtCore.Qt.Key.Key_Minus, QtCore.Qt.Key.Key_Underscore):
            self._adjust_time_window(1.25)
            event.accept()
            return
        super().keyPressEvent(event)

    def shutdown(self):
        if self._cleaned_up:
            return
        self._cleaned_up = True
        self._edge_scroll_timer.stop()
        self._timer.stop()
        self._paint_fallback_timer.stop()
        self.reader.stop_flag = True
        if self._on_close is not None:
            try:
                self._on_close()
            except Exception:
                pass

    def closeEvent(self, event):
        self.shutdown()
        self.closed.emit()
        super().closeEvent(event)


def create_viewer_window(
    input_stream,
    group_specs=(),
    shared_range_groups="",
    style="linespoints",
    width_ms=None,
    realtime_replay=False,
    quick_on_header=False,
    window_title="EPOC-X Live Waveforms",
    notes_path: Optional[str] = None,
    on_close: Optional[Callable[[], None]] = None,
    initial_paused: bool = False,
    on_pause_request: Optional[Callable[[Optional[bool]], Optional[bool]]] = None,
    initial_active: bool = True,
    initial_mode: str = "live",
    on_start_request: Optional[Callable[[], bool]] = None,
    on_seek_request: Optional[Callable[[float], Optional[float]]] = None,
    replay_data: Optional[tuple[float, float, float, str, list[str]]] = None,
) -> Optional[LiveViewerWindow]:
    """Read initial CSV header/sample from ``input_stream`` and construct a ``LiveViewerWindow``."""
    if realtime_replay:
        raw_lines = [l.rstrip("\n").rstrip("\r") for l in input_stream if l.strip()]
    else:
        raw_lines = read_sample(input_stream, quick_on_header=quick_on_header)

    initial_notes: list[tuple[float, float, str]] = []
    total_duration_ms: Optional[float] = None
    stream_source_mode: Optional[str] = None
    filtered_lines: list[str] = []
    for ln in raw_lines:
        if ln.startswith("#"):
            note = parse_note_line(ln)
            if note is not None:
                initial_notes.append(note)
                continue
            parsed_mode = parse_mode_line(ln)
            if parsed_mode is not None:
                stream_source_mode = parsed_mode
                continue
            has_total, parsed_total = parse_total_line(ln)
            if has_total:
                total_duration_ms = parsed_total
            continue
        filtered_lines.append(ln)

    if not filtered_lines:
        return None

    all_lines = filtered_lines if realtime_replay else None
    sample_lines = filtered_lines[:400] if realtime_replay else filtered_lines

    split_rows = [l.split(",") for l in sample_lines]
    header = None
    data_lines = all_lines if all_lines is not None else sample_lines
    data_rows = split_rows
    if is_header_row(split_rows[0]):
        header = [h.strip() for h in split_rows[0]]
        data_rows = split_rows[1:]
        data_lines = data_lines[1:]

    st = guess_structure(header, data_rows)
    specs = [GroupSpec(g) for g in group_specs]
    groups, titles, plan = build_groups(st, specs, data_rows)

    shared = []
    if shared_range_groups.strip():
        for tok in shared_range_groups.split(","):
            tok = tok.strip()
            if tok.isdigit():
                shared.append(int(tok) - 1)

    first_ts_ms = None
    if all_lines is not None:
        first_ts_ms, last_ts_ms = _extract_recording_bounds(st, data_lines)
        if first_ts_ms is not None and last_ts_ms is not None and last_ts_ms >= first_ts_ms:
            total_duration_ms = last_ts_ms - first_ts_ms
        line_source = list(data_lines)
    else:
        line_source = PrereadStdin(list(data_lines), input_stream)

    reader = Reader(
        st,
        plan,
        line_source,
        realtime_replay=realtime_replay,
        first_ts_ms=first_ts_ms,
        total_duration_ms=total_duration_ms,
        initial_notes=initial_notes,
        source_mode=stream_source_mode,
    )
    if replay_data is not None:
        reader.load_recording_rows(*replay_data, bump_generation=False)
    elif realtime_replay and data_lines:
        reader.load_recording_rows(
            first_ts_ms / 1000.0 if first_ts_ms is not None else None,
            None,
            0.0,
            ",".join(header) if header else None,
            data_lines,
            bump_generation=False,
        )
    if initial_paused:
        reader.set_paused(True)
    reader.start()

    return LiveViewerWindow(
        st,
        groups,
        titles,
        reader,
        shared_groups=shared,
        style=style,
        width_ms=width_ms,
        window_title=window_title,
        notes_path=notes_path,
        on_close=on_close,
        on_pause_request=on_pause_request,
        initial_active=initial_active,
        initial_mode=initial_mode,
        on_start_request=on_start_request,
        on_seek_request=on_seek_request,
    )


def run_gui(
    structure,
    groups,
    titles,
    reader,
    shared_groups,
    style,
    width_ms,
    window_title="EPOC-X Live Viewer",
):
    app = ensure_qt_app(sys.argv)
    win = LiveViewerWindow(
        structure,
        groups,
        titles,
        reader,
        shared_groups=shared_groups,
        style=style,
        width_ms=width_ms,
        window_title=window_title,
    )
    install_sigint_handler(win, lambda: (setattr(reader, "stop_flag", True), app.quit()))
    win.show()
    apply_dark_titlebar(win)
    try:
        app.exec()
    finally:
        win.shutdown()


def parse_args(argv):
    ap = argparse.ArgumentParser(
        prog="epoc-live-viewer",
        description="Fast live viewer for CSV values arriving on stdin or broker socket.",
    )
    ap.add_argument(
        "--style",
        choices=["lines", "points", "linespoints"],
        default="linespoints",
        help="how to draw each channel (default: linespoints)",
    )
    ap.add_argument(
        "--group",
        action="append",
        default=[],
        help="define a plot group (column numbers/ranges, wildcard, or channel token)",
    )
    ap.add_argument(
        "--shared-range-groups",
        default="",
        help="comma-separated 1-based indices of groups sharing min/max",
    )
    ap.add_argument(
        "--width",
        type=parse_duration,
        default=None,
        help="width of the rolling time window (e.g. 500ms, 3s, 2m, 1h)",
    )
    ap.add_argument(
        "--realtime-replay",
        action="store_true",
        help="pace stdin according to its own timestamp column",
    )
    ap.add_argument(
        "--connect",
        metavar="HOST:PORT",
        help="connect to a local EPOC-X stream broker TCP socket instead of reading stdin",
    )
    ap.add_argument(
        "--notes",
        metavar="NOTES.csv",
        help="timed notes CSV file to overlay and tail live",
    )
    ap.add_argument("--title", default="EPOC-X Live Viewer", help="window title")
    return ap.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    sock = None
    if args.connect:
        sock, input_stream = open_broker_stream(args.connect)
    else:
        input_stream = sys.stdin

    def _cleanup():
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    app = ensure_qt_app(sys.argv)
    if input_stream is None:
        sys.stderr.write("live-viewer: no input on stream\n")
        _cleanup()
        return 1
    win = create_viewer_window(
        input_stream,
        group_specs=args.group,
        shared_range_groups=args.shared_range_groups,
        style=args.style,
        width_ms=args.width,
        realtime_replay=args.realtime_replay,
        quick_on_header=bool(args.connect),
        window_title=args.title,
        notes_path=args.notes,
        on_close=_cleanup,
    )
    if win is None:
        sys.stderr.write("live-viewer: no input on stream\n")
        _cleanup()
        return 1

    sys.stderr.write(win.reader.st.describe() + "\n")
    if win.reader.st.ts_col is not None:
        sys.stderr.write(
            "Timestamp column: %d (%s)\n"
            % (win.reader.st.ts_col + 1, "ISO" if win.reader.st.ts_is_iso else "numeric")
        )
    sys.stderr.flush()

    install_sigint_handler(win, lambda: (win.shutdown(), app.quit()))
    win.show()
    apply_dark_titlebar(win)
    try:
        app.exec()
    finally:
        win.shutdown()
    return 0
