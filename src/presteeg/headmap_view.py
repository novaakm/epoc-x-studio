"""Headmap: live vector head map of EEG electrode connection and signal quality.

Can be instantiated inside prestEEG as an in-process window fed by a
``LineQueueStream``, or executed standalone from a terminal via ``presteeg-headmap``
(stdin CSV, ``--connect``, ``--lsl``, ``--demo``, or ``--png``).
"""
from __future__ import annotations

import argparse
import importlib
import itertools
import math
import random
import re
import sys
import threading
import time
from collections import deque
from types import SimpleNamespace
from typing import Callable, Optional

import numpy as np
from PyQt6 import QtCore, QtGui, QtWidgets

from .qt_common import (
    apply_dark_titlebar,
    ensure_qt_app,
    get_app_icon,
    install_sigint_handler,
    open_broker_stream,
    set_always_on_top,
)

CHANNELS = ["AF3", "F7", "F3", "FC5", "T7", "P7", "O1", "O2", "P8", "T8", "FC6", "F4", "F8", "AF4"]
LSL_ADC_TO_UV = 0.1275
SIZE = 180
GUI_SIZE = 240
_LEFT = {
    "AF3": (-0.25, 0.86),
    "F7": (-0.82, 0.55),
    "F3": (-0.40, 0.42),
    "FC5": (-0.72, 0.12),
    "T7": (-0.98, -0.25),
    "P7": (-0.72, -0.62),
    "O1": (-0.28, -0.90),
}
_MIRROR = {"AF4": "AF3", "F8": "F7", "F4": "F3", "FC6": "FC5", "T8": "T7", "P8": "P7", "O2": "O1"}
POS = dict(_LEFT, **{r: (-_LEFT[l][0], _LEFT[l][1]) for r, l in _MIRROR.items()})

GRAV_CH = ("MOT0", "MOT1", "MOT2")
MAG_CH = ("MOT3", "MOT4", "MOT5")
MOTION_CHANNELS = [f"MOT{i}" for i in range(7)]
EXTRA_CONTACTS: list[str] = []
ORIENT_RANGE = {"Y": 180.0, "P": 90.0, "R": 180.0}

LAYOUTS = {
    "epocx": dict(
        channels=list(CHANNELS),
        extra=[],
        pos=dict(POS),
        lsl_scale=LSL_ADC_TO_UV,
        grav=GRAV_CH,
        mag=MAG_CH,
        motion=list(MOTION_CHANNELS),
        estimator="QualityEstimator",
    ),
    "muse": dict(
        channels=["TP9", "AF7", "AF8", "TP10"],
        extra=["REF"],
        pos={
            "TP9": (-0.92, -0.45),
            "AF7": (-0.55, 0.72),
            "AF8": (0.55, 0.72),
            "TP10": (0.92, -0.45),
            "REF": (0.0, 0.90),
        },
        lsl_scale=1.0,
        grav=("ax", "ay", "az"),
        mag=(),
        motion=["ax", "ay", "az", "drl", "ref"],
        estimator="MuseQualityEstimator",
    ),
}
LAYOUT = "epocx"


def use_layout(name: str) -> None:
    global POS, LSL_ADC_TO_UV, GRAV_CH, MAG_CH, LAYOUT
    lay = LAYOUTS[name]
    CHANNELS[:] = lay["channels"]
    EXTRA_CONTACTS[:] = lay["extra"]
    MOTION_CHANNELS[:] = lay["motion"]
    POS, LSL_ADC_TO_UV, GRAV_CH, MAG_CH = lay["pos"], lay["lsl_scale"], lay["grav"], lay["mag"]
    LAYOUT = name


def orientation(mot):
    """{MOTn: value} -> (yaw, pitch, roll) in degrees, or None without motion data."""
    try:
        gx, gy, gz = (mot[c] for c in GRAV_CH)
    except KeyError:
        return None
    if not gx and not gy and not gz:
        return None
    roll = math.atan2(gy, gz)
    pitch = math.atan2(-gx, math.hypot(gy, gz))
    yaw = 0.0
    if MAG_CH and all(c in mot for c in MAG_CH):
        mx, my, mz = (mot[c] for c in MAG_CH)
        xh = mx * math.cos(pitch) + (my * math.sin(roll) + mz * math.cos(roll)) * math.sin(pitch)
        yh = my * math.cos(roll) - mz * math.sin(roll)
        yaw = math.atan2(-yh, xh)
    return tuple(math.degrees(a) for a in (yaw, pitch, roll))


def _wrap(a: float) -> float:
    return (a + 180.0) % 360.0 - 180.0


BG_COLOR = "#080d18"
HEAD_FILL = "#0f172a"
HEAD_FILL_INNER = "#162033"
HEAD_OUTLINE = "#334155"
GRID_STROKE = "#1a2740"
ELECTRODE_BG = "#162033"
TEXT_PRIMARY = "#f8fafc"
TEXT_MUTED = "#94a3b8"
ACCENT = "#38bdf8"

GREY, RED, ORANGE, GREEN = "#475569", "#ef4444", "#f59e0b", "#22c55e"
CONN_COLOURS = [GREY, RED, ORANGE, GREEN]


def mix(c1: str, c2: str, t: float) -> str:
    a = [int(c1[i : i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


def quality_colour(q: float) -> str:
    return mix(RED, ORANGE, q / 0.5) if q < 0.5 else mix(ORANGE, GREEN, (q - 0.5) / 0.5)


_FFT_CACHE: dict[tuple[int, float], tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}


def _get_fft_helpers(n: int, fs: float):
    key = (n, round(float(fs), 1))
    cached = _FFT_CACHE.get(key)
    if cached is not None:
        return cached
    if len(_FFT_CACHE) > 32:
        _FFT_CACHE.clear()
    win = np.hanning(n)
    f = np.fft.rfftfreq(n, 1.0 / max(1.0, float(fs)))
    m_tot = f >= 1.0
    m_eeg = m_tot & (f <= 30.0)
    m_line = (f >= 48.0) & (f <= 52.0)
    ramp = np.linspace(0.0, 1.0, n)
    entry = (win, m_tot, m_eeg, m_line, ramp)
    _FFT_CACHE[key] = entry
    return entry


class QualityEstimator:
    """Hand-written rules; values are in uV (DC offset around 4200)."""

    CONN_WINDOW_S = 1.5
    SIGNAL_WINDOW_S = 2.0
    CORR_OK = 0.40
    MOB_OK = 0.79
    FLAT_UV = 0.5
    SMOOTH = 0.12
    ONHEAD_VOTE = 0.30
    GREEN_VOTE = 0.40
    ORANGE_VOTE = 0.10

    def __init__(self):
        self._ema_global = 0.0
        self._ema = {ch: 0.0 for ch in CHANNELS}

    def connection(self, x, q):
        vals = [v for v in x if v is not None]
        if len(vals) < max(3, len(x) // 2):
            return 0
        lo, hi = min(vals), max(vals)
        mean = sum(vals) / len(vals)
        jumps = max((abs(b - a) for a, b in zip(vals, vals[1:])), default=0.0)
        if hi - lo < 0.5 or mean < 1000 or mean > 7500:
            level = 1
        elif hi - lo > 600 or jumps > 250:
            level = 2
        else:
            level = 3
        return min(level, q) if q is not None else level

    def connections(self, xs, q=None):
        cleaned = {ch: [v for v in xs.get(ch, ()) if v is not None] for ch in CHANNELS}
        n = min((len(v) for v in cleaned.values()), default=0)
        if n < 128:
            return {ch: 0 for ch in CHANNELS}
        M = np.array([cleaned[ch][-n:] for ch in CHANNELS], dtype=np.float64)
        M -= M.mean(axis=1, keepdims=True)
        sd = M.std(axis=1)
        cm = M.mean(axis=0)
        cm_c = cm - cm.mean()
        cm_sd = float(cm_c.std())
        if cm_sd > 1e-9:
            cor = np.where(sd > 1e-6, (M @ cm_c) / (n * sd * cm_sd + 1e-12), 0.0)
            cor = np.nan_to_num(cor)
        else:
            cor = np.zeros(len(CHANNELS), dtype=np.float64)
        mob = np.std(np.diff(M, axis=1), axis=1) / (sd + 1e-9)
        p2p = M.max(axis=1) - M.min(axis=1)
        good = (cor >= self.CORR_OK) & (mob <= self.MOB_OK)
        for i, ch in enumerate(CHANNELS):
            self._ema[ch] += self.SMOOTH * (float(good[i]) - self._ema[ch])
        g_vote = float((np.median(cor) >= self.CORR_OK) and (np.median(mob) <= self.MOB_OK))
        self._ema_global += self.SMOOTH * (g_vote - self._ema_global)
        onhead = self._ema_global >= self.ONHEAD_VOTE
        out = {}
        for i, ch in enumerate(CHANNELS):
            if p2p[i] < self.FLAT_UV or not onhead:
                lvl = 1
            elif self._ema[ch] >= self.GREEN_VOTE:
                lvl = 3
            elif self._ema[ch] >= self.ORANGE_VOTE:
                lvl = 2
            else:
                lvl = 1
            if q and q.get(ch) is not None:
                lvl = min(lvl, q[ch])
            out[ch] = lvl
        return out

    def signal(self, x, fs):
        if not x:
            return 0.0
        vals = np.array([v for v in x if v is not None], dtype=np.float64)
        n = len(vals)
        if n < 0.5 * len(x) or n < 32:
            return 0.0
        present = n / len(x)
        win, m_tot, m_eeg, m_line, ramp = _get_fft_helpers(n, fs)
        m0 = vals[:8].mean()
        m1 = vals[-8:].mean()
        v = vals - (m0 + (m1 - m0) * ramp)
        sd = float(v.std())
        if sd < 0.5:
            return 0.0
        amp = 1.0 if 2 <= sd <= 60 else max(0.0, 1 - abs(math.log(sd / (2 if sd < 2 else 60))) / 1.5)
        art = float(np.mean(np.abs(v - np.median(v)) > max(100.0, 6 * sd)))
        p = np.abs(np.fft.rfft(v * win)) ** 2
        tot = float(p[m_tot].sum()) + 1e-9
        eeg = float(p[m_eeg].sum()) / tot
        line = float(p[m_line].sum()) / tot
        spec = min(1.0, eeg / 0.8) * (1 - min(1.0, line / 0.5))
        return float(max(0.0, min(1.0, present * amp * (1 - min(1.0, art * 5)) * spec)))

    def signals(self, xs: dict, fs: float) -> dict[str, float]:
        """Vectorized batch signal quality calculation across all channels."""
        if not xs:
            return {}
        first_ch = CHANNELS[0] if CHANNELS else None
        if first_ch is None or first_ch not in xs:
            return {ch: self.signal(xs.get(ch, ()), fs) for ch in CHANNELS}
        raw_len = len(xs[first_ch])
        if raw_len < 32:
            return {ch: 0.0 for ch in CHANNELS}
        cleaned = []
        for ch in CHANNELS:
            seq = xs.get(ch, ())
            if len(seq) != raw_len:
                return {c: self.signal(xs.get(c, ()), fs) for c in CHANNELS}
            c_vals = [v for v in seq if v is not None]
            if len(c_vals) != raw_len:
                return {c: self.signal(xs.get(c, ()), fs) for c in CHANNELS}
            cleaned.append(c_vals)
        V = np.array(cleaned, dtype=np.float64)
        n = raw_len
        win, m_tot, m_eeg, m_line, ramp = _get_fft_helpers(n, fs)
        m0 = V[:, :8].mean(axis=1, keepdims=True)
        m1 = V[:, -8:].mean(axis=1, keepdims=True)
        V -= m0 + (m1 - m0) * ramp
        sds = V.std(axis=1)
        meds = np.median(V, axis=1, keepdims=True)
        thresh = np.maximum(100.0, 6.0 * sds)[:, None]
        arts = np.mean(np.abs(V - meds) > thresh, axis=1)
        P = np.abs(np.fft.rfft(V * win, axis=1)) ** 2
        tots = P[:, m_tot].sum(axis=1) + 1e-9
        eegs = P[:, m_eeg].sum(axis=1) / tots
        lines = P[:, m_line].sum(axis=1) / tots
        out: dict[str, float] = {}
        for i, ch in enumerate(CHANNELS):
            sd = float(sds[i])
            if sd < 0.5:
                out[ch] = 0.0
                continue
            amp = 1.0 if 2.0 <= sd <= 60.0 else max(0.0, 1.0 - abs(math.log(sd / (2.0 if sd < 2.0 else 60.0))) / 1.5)
            art = float(arts[i])
            spec = min(1.0, float(eegs[i]) / 0.8) * (1.0 - min(1.0, float(lines[i]) / 0.5))
            out[ch] = float(max(0.0, min(1.0, amp * (1.0 - min(1.0, art * 5.0)) * spec)))
        return out


class MuseQualityEstimator(QualityEstimator):
    FS = 256.0
    RAIL_P2P = 1500.0
    LINE_OK, LINE_BAD = 20.0, 70.0
    HF_OK, HF_BAD = 10.0, 35.0
    SLOW_OK = 45.0
    REF_OK = 300.0

    def __init__(self):
        super().__init__()
        self._feat = {}

    def forehead(self, latest):
        if not latest or "drl" not in latest or "ref" not in latest:
            return 0
        return 3 if abs(latest["drl"]) < self.REF_OK and abs(latest["ref"]) < self.REF_OK else 1

    def connections(self, xs, q=None, latest=None):
        import numpy as np

        ref = self.forehead(latest)
        out = {ch: 0 for ch in CHANNELS}
        out.update({c: ref for c in EXTRA_CONTACTS})
        n = min((sum(v is not None for v in xs.get(ch, [])) for ch in CHANNELS), default=0)
        if n < 128:
            return out
        w = np.hanning(n)
        f = np.fft.rfftfreq(n, 1 / self.FS)
        bands = {"slow": (f >= 1) & (f < 30), "line": (f >= 45) & (f < 65), "hf": f >= 65}
        for ch in CHANNELS:
            x = np.array([v for v in xs[ch] if v is not None][-n:], float)
            p2p = x.max() - x.min()
            if p2p < self.FLAT_UV or p2p > self.RAIL_P2P or ref == 1:
                self._feat.pop(ch, None)
                out[ch] = 1
                continue
            P = np.abs(np.fft.rfft((x - x.mean()) * w)) ** 2 * 2 / (n * (w**2).sum())
            feat = {k: math.log(math.sqrt(P[m].sum()) + 1e-3) for k, m in bands.items()}
            old = self._feat.get(ch)
            if old is not None:
                feat = {k: old[k] + self.SMOOTH * (v - old[k]) for k, v in feat.items()}
            self._feat[ch] = feat
            line, hf, slow = (math.exp(feat[k]) for k in ("line", "hf", "slow"))
            if line > self.LINE_BAD or hf > self.HF_BAD:
                out[ch] = 1
            elif line > self.LINE_OK or hf > self.HF_OK or slow > self.SLOW_OK:
                out[ch] = 2
            else:
                out[ch] = 3
        return out


_SHADOW_COLOR = QtGui.QColor(8, 13, 24, 195)
_QCOL_HEAD_OUTLINE = QtGui.QColor(HEAD_OUTLINE)
_QCOL_HEAD_FILL = QtGui.QColor(HEAD_FILL)
_QCOL_HEAD_FILL_INNER = QtGui.QColor(HEAD_FILL_INNER)
_QCOL_GRID_STROKE = QtGui.QColor(GRID_STROKE)
_QCOL_ELECTRODE_BG = QtGui.QColor(ELECTRODE_BG)
_QCOL_ACCENT = QtGui.QColor(ACCENT)
_QCOL_TEXT_MUTED = QtGui.QColor(TEXT_MUTED)
_QCOL_TEXT_PRIMARY = QtGui.QColor(TEXT_PRIMARY)
_QCOL_RED = QtGui.QColor(RED)
_QCOL_DEAD_BG = QtGui.QColor(8, 13, 24, 232)
_QCOL_CONN = [QtGui.QColor(c) for c in CONN_COLOURS]


def _draw_crisp_text(painter, rect, text, font, color, shadow=False):
    painter.setFont(font)
    flags = QtCore.Qt.AlignmentFlag.AlignCenter
    if shadow:
        painter.setPen(_SHADOW_COLOR)
        painter.drawText(rect.translated(0.6, 0.8), flags, text)
    painter.setPen(color)
    painter.drawText(rect, flags, text)


def paint_headmap(painter, rect, conn, sig, labels=True, name=None, ypr=None, dead=False):
    """Render the EEG head map as crisp anti-aliased vector graphics inside ``rect``."""
    painter.save()
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing, True)
    painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)

    side = min(rect.width(), rect.height())
    ox = rect.x() + (rect.width() - side) * 0.5
    oy = rect.y() + (rect.height() - side) * 0.5
    s = side / float(SIZE)

    cx = ox + 90.0 * s
    cy = oy + 94.0 * s
    R = 66.0 * s
    er = 12.6 * s
    ring = 3.4 * s
    stroke_w = max(1.0, 1.5 * s)

    outline_pen = QtGui.QPen(_QCOL_HEAD_OUTLINE, stroke_w)
    outline_pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
    outline_pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)

    nose = QtGui.QPainterPath()
    nose.moveTo(cx - 10.5 * s, cy - R + 3.0 * s)
    nose.quadTo(cx - 5.0 * s, cy - R - 7.5 * s, cx - 2.2 * s, cy - R - 11.5 * s)
    nose.quadTo(cx, cy - R - 14.0 * s, cx + 2.2 * s, cy - R - 11.5 * s)
    nose.quadTo(cx + 5.0 * s, cy - R - 7.5 * s, cx + 10.5 * s, cy - R + 3.0 * s)
    nose.closeSubpath()
    painter.setPen(outline_pen)
    painter.setBrush(_QCOL_HEAD_FILL)
    painter.drawPath(nose)

    for side_x in (-1.0, 1.0):
        ear_cx = cx + side_x * (R + 1.0 * s)
        ear = QtGui.QPainterPath()
        ear.moveTo(ear_cx - side_x * 2.0 * s, cy - 15.0 * s)
        ear.cubicTo(
            ear_cx + side_x * 9.0 * s,
            cy - 16.0 * s,
            ear_cx + side_x * 9.0 * s,
            cy + 16.0 * s,
            ear_cx - side_x * 2.0 * s,
            cy + 15.0 * s,
        )
        ear.closeSubpath()
        painter.setPen(outline_pen)
        painter.setBrush(_QCOL_HEAD_FILL)
        painter.drawPath(ear)

    head_grad = QtGui.QRadialGradient(QtCore.QPointF(cx, cy - R * 0.15), R * 1.05)
    head_grad.setColorAt(0.0, _QCOL_HEAD_FILL_INNER)
    head_grad.setColorAt(1.0, _QCOL_HEAD_FILL)
    painter.setPen(outline_pen)
    painter.setBrush(head_grad)
    painter.drawEllipse(QtCore.QPointF(cx, cy), R, R)

    grid_pen = QtGui.QPen(_QCOL_GRID_STROKE, max(0.8, 0.9 * s))
    painter.setPen(grid_pen)
    painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
    painter.drawEllipse(QtCore.QPointF(cx, cy), R * 0.58, R * 0.58)
    painter.drawLine(QtCore.QPointF(cx - R + 2.0 * s, cy), QtCore.QPointF(cx + R - 2.0 * s, cy))
    painter.drawLine(QtCore.QPointF(cx, cy - R + 2.0 * s), QtCore.QPointF(cx, cy + R - 2.0 * s))

    if ypr is not None:
        bx = cx + 4.0 * s
        hw = 18.0 * s
        bh = 6.0 * s
        ypr_font = QtGui.QFont("Consolas", 1)
        ypr_font.setStyleHint(QtGui.QFont.StyleHint.Monospace)
        ypr_font.setWeight(QtGui.QFont.Weight.Bold)
        ypr_font.setPointSizeF(max(5.0, 5.2 * s))
        track_pen = QtGui.QPen(_QCOL_HEAD_OUTLINE, max(0.8, 0.9 * s))
        center_pen = QtGui.QPen(_QCOL_TEXT_MUTED, max(1.0, 1.0 * s))
        for i, (lab, a) in enumerate(zip("YPR", ypr)):
            by = cy + 1.0 * s + i * 11.0 * s
            track_rect = QtCore.QRectF(bx - hw, by - bh * 0.5, hw * 2.0, bh)
            painter.setPen(track_pen)
            painter.setBrush(_QCOL_ELECTRODE_BG)
            painter.drawRoundedRect(track_rect, bh * 0.45, bh * 0.45)

            v = max(-1.0, min(1.0, a / ORIENT_RANGE[lab]))
            if abs(v * hw) >= 0.5:
                fx0 = min(bx, bx + v * hw)
                fx1 = max(bx, bx + v * hw)
                fill_rect = QtCore.QRectF(fx0, by - bh * 0.5 + 0.6 * s, fx1 - fx0, bh - 1.2 * s)
                painter.setPen(QtCore.Qt.PenStyle.NoPen)
                painter.setBrush(_QCOL_ACCENT)
                painter.drawRoundedRect(fill_rect, bh * 0.35, bh * 0.35)

            painter.setPen(center_pen)
            painter.drawLine(
                QtCore.QPointF(bx, by - bh * 0.5 - 1.2 * s),
                QtCore.QPointF(bx, by + bh * 0.5 + 1.2 * s),
            )

            lab_rect = QtCore.QRectF(bx - hw - 11.5 * s, by - 6.0 * s, 9.5 * s, 12.0 * s)
            _draw_crisp_text(painter, lab_rect, lab, ypr_font, _QCOL_TEXT_MUTED)

    ch_font = QtGui.QFont("Segoe UI", 1)
    ch_font.setWeight(QtGui.QFont.Weight.Bold)
    ch_font.setPointSizeF(max(4.5, 4.7 * s))
    conn_pens = [QtGui.QPen(col, ring) for col in _QCOL_CONN]

    for ch in CHANNELS + EXTRA_CONTACTS:
        ux, uy = POS[ch]
        ex = cx + ux * R * 0.93
        ey = cy - uy * R * 0.93
        c = conn.get(ch, 0)

        ring_r = er - ring * 0.35
        painter.setPen(conn_pens[c])
        painter.setBrush(_QCOL_ELECTRODE_BG)
        painter.drawEllipse(QtCore.QPointF(ex, ey), ring_r, ring_r)

        q = sig.get(ch)
        if q:
            dot_r = (er - ring * 0.9) * math.sqrt(q)
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QColor(quality_colour(q)))
            painter.drawEllipse(QtCore.QPointF(ex, ey), dot_r, dot_r)

        if labels:
            t_rect = QtCore.QRectF(ex - er, ey - er, er * 2.0, er * 2.0)
            _draw_crisp_text(painter, t_rect, ch, ch_font, _QCOL_TEXT_PRIMARY, shadow=True)

    if dead:
        badge_w = 70.0 * s
        badge_h = 20.0 * s
        badge_rect = QtCore.QRectF(cx - badge_w * 0.5, cy + 12.0 * s - badge_h * 0.5, badge_w, badge_h)
        painter.setPen(QtGui.QPen(_QCOL_RED, max(1.0, 1.3 * s)))
        painter.setBrush(_QCOL_DEAD_BG)
        painter.drawRoundedRect(badge_rect, 5.0 * s, 5.0 * s)

        dead_font = QtGui.QFont("Segoe UI", 1)
        dead_font.setWeight(QtGui.QFont.Weight.Bold)
        dead_font.setPointSizeF(max(6.5, 7.2 * s))
        _draw_crisp_text(painter, badge_rect, "NO SIGNAL", dead_font, _QCOL_RED)

    if name:
        name_font = QtGui.QFont("Segoe UI", 1)
        name_font.setWeight(QtGui.QFont.Weight.Medium)
        name_font.setPointSizeF(max(5.5, 6.0 * s))
        name_rect = QtCore.QRectF(ox + 6.0 * s, oy + (SIZE - 16.0) * s, side - 12.0 * s, 14.0 * s)
        _draw_crisp_text(painter, name_rect, name, name_font, _QCOL_TEXT_MUTED)

    painter.restore()


def render_png_image(width, height, conn, sig, labels=True, name=None, ypr=None, dead=False):
    img = QtGui.QImage(width, height, QtGui.QImage.Format.Format_ARGB32)
    img.fill(QtGui.QColor(BG_COLOR))
    painter = QtGui.QPainter(img)
    paint_headmap(
        painter,
        QtCore.QRectF(0.0, 0.0, float(width), float(height)),
        conn,
        sig,
        labels=labels,
        name=name,
        ypr=ypr,
        dead=dead,
    )
    painter.end()
    return img


class Sender:
    HEARTBEAT_S = 2.0

    def __init__(self, url, device, key=None):
        from urllib.parse import quote

        if "feed-status-image.php" not in url:
            url = url.rstrip("/") + "/feed-status-image.php"
        sep = "&" if "?" in url else "?"
        self.url = f"{url}{sep}device={quote(device)}" + (f"&key={quote(key)}" if key else "")
        self.cv = threading.Condition()
        self.png = None
        self.last = (None, 0.0)
        self.err = None
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, img):
        ba = QtCore.QByteArray()
        buf = QtCore.QBuffer(ba)
        buf.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG")
        buf.close()
        with self.cv:
            self.png = bytes(ba)
            self.cv.notify()

    def _run(self):
        import urllib.request

        while True:
            with self.cv:
                while self.png is None:
                    self.cv.wait()
                png, self.png = self.png, None
            if png == self.last[0] and time.time() - self.last[1] < self.HEARTBEAT_S:
                continue
            try:
                req = urllib.request.Request(self.url, data=png, headers={"Content-Type": "image/png"})
                urllib.request.urlopen(req, timeout=5).close()
                self.last = (png, time.time())
                if self.err:
                    print("# send-to: OK again", file=sys.stderr, flush=True)
                    self.err = None
            except Exception as e:
                if str(e) != self.err:
                    print(f"# send-to failed: {e}", file=sys.stderr, flush=True)
                self.err = str(e)
                time.sleep(1.0)


def mock():
    conn, sig = {c: 3 for c in EXTRA_CONTACTS}, {}
    for ch in CHANNELS:
        conn[ch] = random.choices([0, 1, 2, 3], [1, 1, 2, 8])[0]
        sig[ch] = None if conn[ch] == 0 else round(random.random() ** 0.5 * (conn[ch] / 3), 2)
    conn[CHANNELS[0]], sig[CHANNELS[0]] = 3, 1.0
    return conn, sig


def mock_ypr():
    return random.uniform(-120, 120), random.uniform(-45, 45), random.uniform(-60, 60)


class Buffers:
    def __init__(self, seconds=4.0):
        self.lock = threading.Lock()
        self.seconds = seconds
        self.t = deque()
        self.x = {ch: deque() for ch in CHANNELS}
        self.q = {ch: None for ch in CHANNELS}
        self.idx = None
        self.last_rx = 0.0
        self.mot = {}
        self.device_name = None
        self._stop = False

    def stop(self):
        self._stop = True

    def read(self, stream):
        for line in stream:
            if self._stop:
                break
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            f = stripped.split(",")
            if self.idx is None or f[0] == "timestamp_ns":
                if f[0] == "timestamp_ns":
                    self.idx = {name: i for i, name in enumerate(f)}
                    continue
                self.idx = {
                    name: i
                    for i, name in enumerate(["timestamp_ns", "timestamp_iso"] + CHANNELS + [c + "_q" for c in CHANNELS])
                }
            try:
                t = int(f[0]) / 1e9
            except (ValueError, IndexError):
                continue
            with self.lock:
                self.t.append(t)
                for ch in CHANNELS:
                    val = _num(f, self.idx.get(ch))
                    if val is not None and LAYOUT == "epocx" and val > 10000:
                        val *= LSL_ADC_TO_UV
                    self.x[ch].append(val)
                    qv = _num(f, self.idx.get(ch + "_q"))
                    if qv is not None:
                        self.q[ch] = int(qv)
                for m in MOTION_CHANNELS:
                    mv = _num(f, self.idx.get(m))
                    if mv is not None:
                        self.mot[m] = mv
                while self.t and self.t[0] < t - self.seconds:
                    self.t.popleft()
                    for ch in CHANNELS:
                        self.x[ch].popleft()
                self.last_rx = time.time()

    def read_lsl(self, name="EPOCX", scale=None):
        scale = LSL_ADC_TO_UV if scale is None else scale
        from pylsl import StreamInlet, resolve_byprop

        print(f"# resolving LSL stream '{name}' ...", file=sys.stderr, flush=True)
        while not self._stop and not (streams := resolve_byprop("name", name, timeout=2.0)):
            print(
                f"# no LSL stream '{name}' found yet (is presteeg-record --lsl running?); retrying",
                file=sys.stderr,
                flush=True,
            )
        if self._stop:
            return
        inlet = StreamInlet(streams[0], recover=True)
        info = inlet.info()
        labels, ch = [], info.desc().child("channels").child("channel")
        while not ch.empty():
            labels.append(ch.child_value("label"))
            ch = ch.next_sibling()
        idx = {lab: i for i, lab in enumerate(labels)}
        chan_idx = {c: idx.get(c) for c in CHANNELS}
        if all(i is None for i in chan_idx.values()):
            chan_idx = {c: 2 + i for i, c in enumerate(CHANNELS)}
        mot_idx = {m: idx[m] for m in MOTION_CHANNELS if m in idx}
        self.device_name = info.desc().child_value("device_name") or info.name()
        print(
            f"# connected to '{info.name()}' ({info.channel_count()} ch), device {self.device_name}",
            file=sys.stderr,
            flush=True,
        )
        while not self._stop:
            sample, ts = inlet.pull_sample(timeout=1.0)
            if sample is None:
                continue
            t = ts if ts and ts > 1e9 else time.time()
            vals = {}
            for c in CHANNELS:
                i = chan_idx.get(c)
                v = sample[i] if (i is not None and i < len(sample)) else None
                vals[c] = None if v is None or v != v else v * scale
            with self.lock:
                for m, i in mot_idx.items():
                    if i < len(sample) and sample[i] == sample[i]:
                        self.mot[m] = sample[i]
                self.last_rx = time.time()
                if all(v is None for v in vals.values()):
                    continue
                self.t.append(t)
                for c in CHANNELS:
                    self.x[c].append(vals[c])
                while self.t and self.t[0] < t - self.seconds:
                    self.t.popleft()
                    for c in CHANNELS:
                        self.x[c].popleft()
                self.last_rx = time.time()

    def window(self, seconds):
        with self.lock:
            if not self.t or time.time() - self.last_rx > 1.5:
                return None, 0.0, {}
            cutoff = self.t[-1] - seconds
            n = 0
            for t in reversed(self.t):
                if t >= cutoff:
                    n += 1
                else:
                    break
            total_len = len(self.t)
            span = self.t[-1] - self.t[0]
            fs = (total_len - 1) / span if span > 0 else 128.0
            start = max(0, total_len - n)
            return (
                {ch: list(itertools.islice(self.x[ch], start, total_len)) for ch in CHANNELS},
                fs,
                dict(self.q),
            )

    def motion(self):
        with self.lock:
            return dict(self.mot) if time.time() - self.last_rx <= 1.5 else {}


def _num(f, i):
    try:
        return float(f[i]) if i is not None and f[i] != "" else None
    except (ValueError, IndexError):
        return None


def _apply_geometry(widget, geom_str):
    if not geom_str:
        return
    m = re.fullmatch(r"(?:(\d+)x(\d+))?(?:([+-]\d+)([+-]\d+))?", geom_str.strip())
    if not m:
        return
    w_s, h_s, x_s, y_s = m.groups()
    if w_s and h_s:
        widget.resize(int(w_s), int(h_s))
    if x_s is not None and y_s is not None:
        screen = widget.screen().availableGeometry()
        x = int(x_s)
        y = int(y_s)
        if x_s.startswith("-"):
            x = screen.right() - widget.width() + x
        else:
            x = screen.left() + x
        if y_s.startswith("-"):
            y = screen.bottom() - widget.height() + y
        else:
            y = screen.top() + y
        widget.move(x, y)


HEADMAP_QSS = """
QWidget#HeadmapRoot {
    background-color: #080d18;
    color: #e2e8f0;
    font-family: "Segoe UI", -apple-system, BlinkMacSystemFont, sans-serif;
}
QPushButton.HeaderBtn {
    background-color: #162033;
    color: #e2e8f0;
    border: 1px solid #2b3d5c;
    border-radius: 5px;
    padding: 2px 8px;
    font-size: 11px;
    font-weight: 600;
}
QPushButton.HeaderBtn:hover {
    background-color: #1e2d47;
    border-color: #38bdf8;
}
QPushButton.HeaderBtn[active="true"] {
    background-color: #0284c7;
    color: #f8fafc;
    border: 1px solid #38bdf8;
}
QPushButton.HeaderBtn[active="true"]:hover {
    background-color: #0369a1;
}
"""


class HeadmapCanvas(QtWidgets.QWidget):
    clicked = QtCore.pyqtSignal()

    def __init__(self, show_labels: bool = True, parent=None):
        super().__init__(parent)
        self._conn = {}
        self._sig = {}
        self._labels = show_labels
        self._name = None
        self._ypr = None
        self._dead = False
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Click headmap to zero orientation")

    def update_frame(self, conn, sig, name, ypr, dead):
        self._conn = conn
        self._sig = sig
        self._name = name
        self._ypr = ypr
        self._dead = dead
        self.update()

    def paintEvent(self, _event):
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), QtGui.QColor(BG_COLOR))
        r = QtCore.QRectF(4.0, 2.0, max(1.0, self.width() - 8.0), max(1.0, self.height() - 4.0))
        paint_headmap(
            painter,
            r,
            self._conn,
            self._sig,
            labels=self._labels,
            name=self._name,
            ypr=self._ypr,
            dead=self._dead,
        )

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)


class HeadmapWindow(QtWidgets.QWidget):
    """Independent Headmap window usable inside prestEEG or standalone."""

    closed = QtCore.pyqtSignal()

    def __init__(
        self,
        args=None,
        est=None,
        stream=None,
        on_close: Optional[Callable[[], None]] = None,
        parent=None,
    ):
        super().__init__(parent)
        if args is None:
            args = SimpleNamespace(
                demo=False,
                lsl=False,
                lsl_name="EPOCX",
                connect=None,
                scale=1.25,
                no_labels=False,
                float_above=False,
                geometry=None,
                name=None,
                no_orientation=False,
                send_to=None,
                device="epocx1",
                key=None,
            )
        self._args = args
        self._est = est or QualityEstimator()
        self._on_close = on_close
        self._cleaned_up = False
        self._sock = None

        s = max(0.5, float(getattr(args, "scale", 1.0) or 1.0))
        canvas_side = round(GUI_SIZE * s)

        self.setObjectName("HeadmapRoot")
        self.setStyleSheet(HEADMAP_QSS)
        self.setWindowTitle(
            args.name or (args.lsl_name if getattr(args, "lsl", False) else "prestEEG — Headmap")
        )
        icon = get_app_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)
        self.setMinimumSize(210, 210)
        self.resize(canvas_side, canvas_side)

        self._floating = bool(getattr(args, "float_above", False))
        if self._floating:
            self.setWindowFlag(QtCore.Qt.WindowType.WindowStaysOnTopHint, True)

        root_lay = QtWidgets.QVBoxLayout(self)
        root_lay.setContentsMargins(0, 0, 0, 0)
        root_lay.setSpacing(0)

        self.canvas = HeadmapCanvas(show_labels=not getattr(args, "no_labels", False))
        self.canvas.clicked.connect(self._rezero)
        root_lay.addWidget(self.canvas, 1)

        overlay_lay = QtWidgets.QVBoxLayout(self.canvas)
        overlay_lay.setContentsMargins(6, 6, 6, 6)
        overlay_lay.setSpacing(0)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.setSpacing(5)

        if not getattr(args, "no_orientation", False):
            self.zero_btn = QtWidgets.QPushButton("Zero")
            self.zero_btn.setProperty("class", "HeaderBtn")
            self.zero_btn.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
            self.zero_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            self.zero_btn.setToolTip("Zero head orientation (or click headmap)")
            self.zero_btn.clicked.connect(self._rezero)
            btn_row.addWidget(self.zero_btn)

        btn_row.addStretch(1)

        self.float_btn = QtWidgets.QPushButton("Float")
        self.float_btn.setProperty("class", "HeaderBtn")
        self.float_btn.setCheckable(True)
        self.float_btn.setChecked(self._floating)
        self.float_btn.setProperty("active", self._floating)
        self.float_btn.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.float_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.float_btn.setToolTip("Keep window always on top [F]")
        self.float_btn.clicked.connect(self._toggle_float)
        btn_row.addWidget(self.float_btn)

        overlay_lay.addLayout(btn_row)
        overlay_lay.addStretch(1)

        self._state = {
            "conn": {},
            "sig": {},
            "last_sig": 0.0,
            "ypr_raw": None,
            "zero": (0.0, 0.0, 0.0),
            "dead": False,
        }
        self._sender = Sender(args.send_to, args.device, args.key) if getattr(args, "send_to", None) else None
        self._buf: Optional[Buffers] = None

        if not getattr(args, "demo", False):
            self._buf = Buffers()
            if stream is not None:
                threading.Thread(target=self._buf.read, args=(stream,), daemon=True).start()
            elif getattr(args, "lsl", False):
                threading.Thread(target=self._buf.read_lsl, args=(args.lsl_name,), daemon=True).start()
            elif getattr(args, "connect", None):
                self._sock, sock_stream = open_broker_stream(args.connect)
                threading.Thread(target=self._buf.read, args=(sock_stream,), daemon=True).start()
            elif sys.stdin is not None:
                threading.Thread(target=self._buf.read, args=(sys.stdin,), daemon=True).start()

        _apply_geometry(self, getattr(args, "geometry", None))

        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000 if getattr(args, "demo", False) else 200)
        self._tick()

    def _rezero(self) -> None:
        raw = self._state["ypr_raw"]
        if raw:
            self._state["zero"] = raw
            print(
                f"# orientation zeroed at yaw/pitch/roll {raw[0]:.0f}/{raw[1]:.0f}/{raw[2]:.0f} deg",
                file=sys.stderr,
                flush=True,
            )

    def _toggle_float(self) -> None:
        self._floating = not self._floating
        set_always_on_top(self, self._floating)
        self.sync_float_btn()

    def _tick(self) -> None:
        args = self._args
        est = self._est
        state = self._state
        now = time.time()
        if getattr(args, "demo", False):
            state["conn"], state["sig"] = mock()
            state["ypr_raw"] = mock_ypr()
            state["dead"] = False
        else:
            latest = self._buf.motion()
            state["ypr_raw"] = orientation(latest)
            xs, fs, q = self._buf.window(est.CONN_WINDOW_S)
            state["dead"] = not xs
            if not xs:
                state["conn"] = {}
            else:
                if hasattr(est, "forehead"):
                    state["conn"] = est.connections(xs, q, latest=latest)
                elif hasattr(est, "connections"):
                    state["conn"] = est.connections(xs, q)
                else:
                    state["conn"] = {ch: est.connection(xs[ch], q.get(ch)) for ch in CHANNELS}
            if now - state["last_sig"] >= 0.5:
                state["last_sig"] = now
                xs, fs, _ = self._buf.window(est.SIGNAL_WINDOW_S)
                if not xs:
                    state["sig"] = {}
                elif hasattr(est, "signals"):
                    state["sig"] = est.signals(xs, fs)
                else:
                    state["sig"] = {ch: est.signal(xs[ch], fs) for ch in CHANNELS}

        raw = state["ypr_raw"]
        ypr = tuple(_wrap(a - z) for a, z in zip(raw, state["zero"])) if raw else None
        ypr = None if getattr(args, "no_orientation", False) else ypr
        name = args.name or (self._buf.device_name if self._buf else "demo") or getattr(args, "lsl_name", "EPOCX")
        dead = state.get("dead", False)

        self.canvas.update_frame(state["conn"], state["sig"], None, ypr, dead)

        if self._sender:
            img = render_png_image(
                SIZE,
                SIZE,
                state["conn"],
                state["sig"],
                labels=not getattr(args, "no_labels", False),
                name=name,
                ypr=ypr,
                dead=dead,
            )
            self._sender.submit(img)

    def sync_float_btn(self) -> None:
        self.float_btn.setChecked(self._floating)
        self.float_btn.setProperty("active", self._floating)
        self.float_btn.style().unpolish(self.float_btn)
        self.float_btn.style().polish(self.float_btn)

    def showEvent(self, event):
        super().showEvent(event)
        apply_dark_titlebar(self)

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key.Key_F:
            self._toggle_float()
            event.accept()
            return
        super().keyPressEvent(event)

    def shutdown(self) -> None:
        if self._cleaned_up:
            return
        self._cleaned_up = True
        self._timer.stop()
        if self._buf is not None:
            self._buf.stop()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._on_close is not None:
            try:
                self._on_close()
            except Exception:
                pass

    def closeEvent(self, event):
        self.shutdown()
        self.closed.emit()
        super().closeEvent(event)


def run_gui(args, est) -> int:
    app = ensure_qt_app(sys.argv)
    win = HeadmapWindow(args=args, est=est)
    install_sigint_handler(win, app.quit)
    win.show()
    apply_dark_titlebar(win)
    try:
        return app.exec()
    finally:
        win.shutdown()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="presteeg-headmap",
        description="Live vector head map of EEG electrode connection and signal quality.",
    )
    ap.add_argument("--demo", action="store_true", help="random mock qualities instead of stdin")
    ap.add_argument(
        "--lsl",
        action="store_true",
        help="read the Lab Streaming Layer stream instead of stdin CSV (needs pylsl)",
    )
    ap.add_argument("--lsl-name", default="EPOCX", help="LSL stream name to resolve (default EPOCX)")
    ap.add_argument(
        "--connect",
        metavar="HOST:PORT",
        help="connect to a local prestEEG stream broker TCP socket instead of reading stdin",
    )
    ap.add_argument("--png", help="render one mock frame to this PNG and exit")
    ap.add_argument("--scale", type=float, default=1.0, help="UI / PNG scale factor")
    ap.add_argument("--seed", type=int, default=4)
    ap.add_argument("--no-labels", action="store_true", help="do not print electrode names")
    ap.add_argument(
        "--float-above",
        action="store_true",
        help="start with the window floating above other windows; toggle with 'Float' button or 'f'",
    )
    ap.add_argument("--geometry", help="window position, e.g. +0+0 or -0+200")
    ap.add_argument("--name", help="device name shown at the bottom and as window title")
    ap.add_argument("--no-orientation", action="store_true", help="do not draw the yaw/pitch/roll bars")
    ap.add_argument("--send-to", metavar="URL", help="also POST each frame as PNG to mental-state-logger")
    ap.add_argument(
        "--device",
        default="epocx1",
        help="device id reported with --send-to: epocx1, epocx2, muse1 (default epocx1)",
    )
    ap.add_argument(
        "--layout",
        choices=sorted(LAYOUTS),
        help="electrode layout (default: muse for --device muse*, else epocx)",
    )
    ap.add_argument("--dead", action="store_true", help="with --png: draw the NO SIGNAL frame")
    ap.add_argument("--key", help="shared secret for --send-to")
    ap.add_argument("--estimator", help="module:Class replacing QualityEstimator")
    a = ap.parse_args(argv)
    random.seed(a.seed)
    use_layout(a.layout or ("muse" if a.device.startswith("muse") else "epocx"))
    if a.png:
        _app = ensure_qt_app(sys.argv)
        dim = round(SIZE * a.scale)
        conn, sig = ({}, {}) if a.dead else mock()
        img = render_png_image(
            dim,
            dim,
            conn,
            sig,
            labels=not a.no_labels,
            name=a.name or "EPOCX (E5021270)",
            ypr=None if a.no_orientation or a.dead else mock_ypr(),
            dead=a.dead,
        )
        img.save(a.png, "PNG")
        return 0
    est = globals()[LAYOUTS[LAYOUT]["estimator"]]()
    if a.estimator:
        mod, cls = a.estimator.split(":")
        est = getattr(importlib.import_module(mod), cls)()
    return run_gui(a, est)
