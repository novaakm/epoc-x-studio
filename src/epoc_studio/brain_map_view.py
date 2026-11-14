"""3D Brain Map DSP engine, local HTTP/SSE server, and native desktop viewer window."""
from __future__ import annotations

import argparse
import bisect
import collections
import csv
import datetime
import http.server
import itertools
import json
import os
import queue
import socketserver
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from epoc_direct.paths import get_recordings_dir
from .qt_common import apply_dark_titlebar, asset_path, ensure_qt_app, ensure_stdio, install_sigint_handler, open_broker_stream

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

DEFAULT_PORT = 8080
DEFAULT_HOST = "127.0.0.1"
DEFAULT_WINDOW_SEC = 1.0
DEFAULT_FPS = 20.0
DEFAULT_FS = 128.0

EEG_CHANNELS = [
    "AF3", "F7", "F3", "FC5", "T7", "P7", "O1",
    "O2", "P8", "T8", "FC6", "F4", "F8", "AF4",
]

BANDS: Dict[str, Tuple[float, float]] = {
    "delta": (0.5, 4.0),
    "theta": (4.0, 8.0),
    "alpha": (8.0, 12.0),
    "beta": (12.0, 30.0),
    "gamma": (30.0, 45.0),
}

BAND_COLORS: Dict[str, str] = {
    "delta": "#ef4444",
    "theta": "#facc15",
    "alpha": "#a855f7",
    "beta": "#22c55e",
    "gamma": "#ec4899",
}

REGION_MAPPING: Dict[str, List[str]] = {
    "dorsal_frontal_left": ["AF3", "F3"],
    "dorsal_frontal_right": ["AF4", "F4"],
    "ventral_frontal_left": ["F7", "FC5"],
    "ventral_frontal_right": ["F8", "FC6"],
    "temporal_left": ["T7", "P7"],
    "temporal_right": ["T8", "P8"],
    "occipital_left": ["O1"],
    "occipital_right": ["O2"],
}

MACRO_LOBES: Dict[str, List[str]] = {
    "dorsal_frontal": ["dorsal_frontal_left", "dorsal_frontal_right"],
    "ventral_frontal": ["ventral_frontal_left", "ventral_frontal_right"],
    "temporal": ["temporal_left", "temporal_right"],
    "occipital": ["occipital_left", "occipital_right"],
}

WAKING_PRIORS: Dict[str, float] = {
    "delta": 0.10,
    "theta": 0.20,
    "alpha": 0.28,
    "beta": 0.28,
    "gamma": 0.14,
}


class BrainDSP:
    """Sliding-window spectral analyzer for 14-channel EEG."""

    def __init__(
        self,
        window_sec: float = DEFAULT_WINDOW_SEC,
        fs: float = DEFAULT_FS,
        ema_alpha: float = 0.25,
        equalize: bool = True,
        whitening_gamma: float = 1.0,
        delta_cutoff: float = 1.5,
        delta_weight: float = 1.0,
        shelf_knee_hz: float = 15.0,
        use_cmr: bool = True,
    ):
        self.fs = fs
        self.window_sec = window_sec
        self.window_len = max(32, int(round(window_sec * fs)))
        self.ema_alpha = ema_alpha

        self.equalize = equalize
        self.whitening_gamma = whitening_gamma
        self.delta_cutoff = delta_cutoff
        self.delta_weight = delta_weight
        self.shelf_knee_hz = shelf_knee_hz
        self.use_cmr = use_cmr
        self.baseline_locked = False
        self.reg_baselines: Dict[str, Dict[str, Optional[float]]] = {
            r: {b: None for b in BANDS} for r in REGION_MAPPING
        }

        self.lock = threading.Lock()
        self.buffer = collections.deque(maxlen=self.window_len * 4)
        self.timestamps = collections.deque(maxlen=self.window_len * 4)
        self._recompute_window()

        self.smoothed_regions: Dict[str, Dict] = {}
        self.smoothed_macro: Dict[str, Dict] = {}

        self.is_calibrating = False
        self.calibration_samples: List[Dict[str, Dict[str, float]]] = []
        self.calibration_baseline: Optional[Dict[str, Dict[str, Tuple[float, float]]]] = None

        self.last_artifact = False
        self.sample_count = 0

        self.is_replay = False
        self.paused = False
        self.stream_active = True
        self.stream_mode = "live"
        self.session_id = 0
        self.replay_start_ts: Optional[float] = None
        self.replay_end_ts: Optional[float] = None
        self.replay_duration_override: Optional[float] = None
        self.session_start_ts: Optional[float] = None
        self._last_frame: Optional[Dict] = None
        self._seek_callback: Optional[Callable[[float], Optional[float]]] = None

    def set_paused(self, paused: Optional[bool] = None) -> bool:
        with self.lock:
            self.paused = (not self.paused) if paused is None else bool(paused)
            return self.paused

    def set_stream_state(self, mode: str, paused: Optional[bool] = None) -> Tuple[bool, bool]:
        with self.lock:
            self.stream_mode = str(mode)
            self.stream_active = self.stream_mode != "idle"
            if self.stream_mode == "live":
                self.is_replay = False
                self.replay_start_ts = None
                self.replay_end_ts = None
                self.replay_duration_override = None
            elif self.stream_mode == "replay":
                self.is_replay = True
            if paused is not None:
                self.paused = bool(paused)
            return self.stream_active, self.paused

    def _reset_session_locked(self) -> None:
        self.buffer.clear()
        self.timestamps.clear()
        self.smoothed_regions.clear()
        self.smoothed_macro.clear()
        self.session_start_ts = None
        self._last_frame = None
        self.sample_count = 0
        self.last_artifact = False
        self.session_id += 1

    def reset_session(self) -> None:
        with self.lock:
            self._reset_session_locked()

    def set_equalize(self, state: Optional[bool] = None) -> bool:
        with self.lock:
            self.equalize = (not self.equalize) if state is None else bool(state)
            self.smoothed_regions.clear()
            self.smoothed_macro.clear()
            self.baseline_locked = False
            self.calibration_baseline = None
            self.reg_baselines = {r: {b: None for b in BANDS} for r in REGION_MAPPING}
            return self.equalize

    def reset_baseline(self) -> bool:
        with self.lock:
            self.baseline_locked = False
            self.calibration_baseline = None
            self.reg_baselines = {r: {b: None for b in BANDS} for r in REGION_MAPPING}
            self.smoothed_regions.clear()
            self.smoothed_macro.clear()
            return True

    def set_replay_bounds(self, start_ts: Optional[float], end_ts: Optional[float]):
        with self.lock:
            changed = (
                not self.is_replay
                or self.replay_start_ts != start_ts
                or self.replay_end_ts != end_ts
            )
            self.is_replay = True
            self.stream_active = True
            self.stream_mode = "replay"
            self.replay_start_ts = start_ts
            self.replay_end_ts = end_ts
            if start_ts is not None and end_ts is not None and end_ts >= start_ts:
                self.replay_duration_override = end_ts - start_ts
            if changed:
                self.smoothed_regions.clear()
                self.smoothed_macro.clear()
                self._last_frame = None

    def set_replay_duration(self, duration_sec: Optional[float]):
        if duration_sec is None:
            self.clear_replay_mode()
            return
        with self.lock:
            self.is_replay = True
            self.stream_mode = "replay"
            self.replay_duration_override = max(0.0, float(duration_sec))

    def clear_replay_mode(self):
        with self.lock:
            self.is_replay = False
            if self.stream_active:
                self.stream_mode = "live"
            self.replay_start_ts = None
            self.replay_end_ts = None
            self.replay_duration_override = None
            self._reset_session_locked()

    def reset_window(self):
        with self.lock:
            self.buffer.clear()
            self.timestamps.clear()
            self.smoothed_regions.clear()
            self.smoothed_macro.clear()
            self._last_frame = None

    def replace_window(self, samples: List[List[float]], timestamps: List[float]):
        with self.lock:
            self.buffer.clear()
            self.timestamps.clear()
            for s, t in zip(samples, timestamps):
                if self.session_start_ts is None:
                    self.session_start_ts = t
                self.buffer.append(s)
                self.timestamps.append(t)
            self.smoothed_regions.clear()
            self.smoothed_macro.clear()
            self._last_frame = None

    def set_seek_callback(self, callback: Callable[[float], Optional[float]]):
        with self.lock:
            self._seek_callback = callback

    def seek_to_elapsed(self, elapsed_sec: float) -> Optional[float]:
        with self.lock:
            cb = self._seek_callback
        if cb is not None:
            return cb(elapsed_sec)
        return None

    def _recompute_window(self):
        self.window_len = max(32, int(round(self.window_sec * self.fs)))
        self.hanning = np.hanning(self.window_len)[:, None]
        self.freqs = np.fft.rfftfreq(self.window_len, 1.0 / self.fs)
        t_centered = np.arange(self.window_len, dtype=np.float64) - 0.5 * (self.window_len - 1)
        self._t_centered = t_centered[:, None]
        denom = float(np.dot(t_centered, t_centered)) or 1.0
        self._t_norm = t_centered / denom
        self._shelf = np.minimum(1.0, (self.shelf_knee_hz / np.maximum(self.freqs, 1.0)) ** 2)[:, None]
        self._band_masks = {}
        for eq in (False, True):
            masks = {}
            for bname, (f_low, f_high) in BANDS.items():
                f_low_eff = max(f_low, self.delta_cutoff) if (eq and bname == "delta") else f_low
                mask = (self.freqs >= f_low_eff) & (self.freqs < f_high)
                if np.any(mask):
                    bw = (f_high - f_low_eff) if eq else 1.0
                    masks[bname] = (mask, bw)
            self._band_masks[eq] = masks

    def set_sampling_rate(self, new_fs: float):
        if abs(new_fs - self.fs) > 1.0 and 50.0 <= new_fs <= 1000.0:
            with self.lock:
                self.fs = new_fs
                self._recompute_window()

    def push_sample(self, sample_14: List[float], timestamp_s: float):
        with self.lock:
            if self.session_start_ts is None:
                self.session_start_ts = timestamp_s
            self.buffer.append(sample_14)
            self.timestamps.append(timestamp_s)
            self.sample_count += 1

    def push_batch(self, samples_batch: np.ndarray, timestamps_batch: List[float]):
        with self.lock:
            for s, t in zip(samples_batch, timestamps_batch):
                if self.session_start_ts is None:
                    self.session_start_ts = t
                self.buffer.append(s)
                self.timestamps.append(t)
                self.sample_count += 1

    def get_last_frame(self) -> Optional[Dict]:
        with self.lock:
            if self._last_frame is None:
                return None
            frame = dict(self._last_frame)
            frame["paused"] = self.paused
            frame["stream_active"] = self.stream_active
            frame["stream_mode"] = self.stream_mode
            frame["is_replay"] = bool(self.is_replay or self.stream_mode == "replay")
            frame["session_id"] = self.session_id
            return frame

    def compute_frame(self, force: bool = False) -> Optional[Dict]:
        with self.lock:
            if not force and (self.paused or not self.stream_active) and self._last_frame is not None:
                frozen = dict(self._last_frame)
                frozen["paused"] = self.paused
                frozen["stream_active"] = self.stream_active
                frozen["stream_mode"] = self.stream_mode
                frozen["is_replay"] = bool(self.is_replay or self.stream_mode == "replay")
                frozen["session_id"] = self.session_id
                return frozen

            if len(self.buffer) == 0:
                return None

            win_len = self.window_len
            hanning = self.hanning
            t_centered = self._t_centered
            t_norm = self._t_norm
            shelf = self._shelf
            fs = self.fs
            ema_alpha = self.ema_alpha
            equalize = self.equalize
            band_masks = self._band_masks[equalize]
            use_cmr = self.use_cmr
            delta_weight = self.delta_weight
            baseline_locked = self.baseline_locked
            reg_baselines = {r: dict(bmap) for r, bmap in self.reg_baselines.items()}
            prev_smoothed = {
                r: {"activity": v["activity"], "bands": dict(v["bands"])}
                for r, v in self.smoothed_regions.items()
            }

            buf_len = len(self.buffer)
            if buf_len >= win_len:
                buf_slice = list(itertools.islice(self.buffer, buf_len - win_len, buf_len))
            else:
                buf_list = list(self.buffer)
                pad_count = win_len - buf_len
                buf_slice = [buf_list[0]] * pad_count + buf_list
            raw_window = np.array(buf_slice, dtype=np.float64)
            current_time = self.timestamps[-1] if self.timestamps else time.time()
            is_paused = self.paused
            is_replay = self.is_replay
            stream_active = self.stream_active
            stream_mode = self.stream_mode
            session_id = self.session_id
            start_ref = (
                self.replay_start_ts
                if (self.is_replay and self.replay_start_ts is not None)
                else self.session_start_ts
            )
            end_ref = self.replay_end_ts if self.is_replay else None
            dur_override = self.replay_duration_override if self.is_replay else None
            can_seek = bool(is_replay and self._seek_callback is not None)
            sample_count = self.sample_count

        std_per_ch = np.std(raw_window, axis=0)
        is_artifact = bool(np.any(std_per_ch > 2500.0))

        # Closed-form linear detrend (equivalent to degree-1 polyfit without Vandermonde/SVD)
        win_mean = np.mean(raw_window, axis=0, keepdims=True)
        centered = raw_window - win_mean
        slope = t_norm @ centered
        detrended = centered - t_centered * slope[None, :]

        if equalize and use_cmr:
            detrended = detrended - np.median(detrended, axis=1, keepdims=True)

        if equalize:
            pre_emph = np.diff(detrended, axis=0, prepend=detrended[0:1])
            sig_for_fft = pre_emph - np.mean(pre_emph, axis=0, keepdims=True)
        else:
            sig_for_fft = detrended

        windowed = sig_for_fft * hanning
        fft_vals = np.fft.rfft(windowed, axis=0)
        psd = (np.abs(fft_vals) ** 2) / (fs * win_len)

        psd_proc = (psd * shelf) if equalize else psd

        ch_band_powers: Dict[str, Dict[str, float]] = {ch: {} for ch in EEG_CHANNELS}
        for bname, (mask, bw) in band_masks.items():
            bp = np.sum(psd_proc[mask, :], axis=0) / bw
            if equalize and bname == "delta":
                bp = bp * delta_weight
            for idx, ch in enumerate(EEG_CHANNELS):
                ch_band_powers[ch][bname] = float(bp[idx])

        region_results: Dict[str, Dict] = {}
        raw_region_powers: Dict[str, Dict[str, float]] = {}
        for rname, channels in REGION_MAPPING.items():
            r_powers = {b: 0.0 for b in BANDS}
            for ch in channels:
                for b in BANDS:
                    r_powers[b] += ch_band_powers[ch].get(b, 0.0)
            for b in BANDS:
                r_powers[b] /= len(channels)
            raw_region_powers[rname] = dict(r_powers)

            if equalize and baseline_locked and reg_baselines[rname]["alpha"] is not None:
                norm_p = {
                    b: WAKING_PRIORS[b] * (r_powers[b] / (reg_baselines[rname][b] + 1e-9))
                    for b in BANDS
                }
                total_power = sum(norm_p.values()) + 1e-9
                rel_powers = {b: norm_p[b] / total_power for b in BANDS}
            else:
                total_power = sum(r_powers.values()) + 1e-9
                rel_powers = {b: r_powers[b] / total_power for b in BANDS}

            dominant_band = max(rel_powers.keys(), key=lambda b: rel_powers[b])
            if equalize:
                raw_act = float(np.clip((rel_powers[dominant_band] - 0.15) / 0.30, 0.0, 1.0))
            else:
                raw_act = float(np.clip((rel_powers[dominant_band] - 0.15) / 0.5, 0.0, 1.0))

            prev_region = prev_smoothed.get(rname)
            if prev_region is not None:
                smooth_bands = {
                    b: (1.0 - ema_alpha) * prev_region["bands"][b] + ema_alpha * rel_powers[b]
                    for b in BANDS
                }
                smooth_act = (1.0 - ema_alpha) * prev_region["activity"] + ema_alpha * raw_act
                dominant = max(smooth_bands.keys(), key=lambda b: smooth_bands[b])
            else:
                smooth_bands = rel_powers
                smooth_act = raw_act
                dominant = dominant_band

            region_results[rname] = {
                "activity": round(float(smooth_act), 3),
                "dominant": dominant,
                "color": BAND_COLORS[dominant],
                "bands": {b: round(float(smooth_bands[b]), 3) for b in BANDS},
                "raw_power": {b: round(float(r_powers[b]), 2) for b in BANDS},
            }

        macro_results: Dict[str, Dict] = {}
        for mname, sub_regions in MACRO_LOBES.items():
            m_bands = {b: 0.0 for b in BANDS}
            m_act = 0.0
            for sr in sub_regions:
                sr_data = region_results[sr]
                m_act += sr_data["activity"]
                for b in BANDS:
                    m_bands[b] += sr_data["bands"][b]
            m_act /= len(sub_regions)
            for b in BANDS:
                m_bands[b] /= len(sub_regions)

            dominant = max(m_bands.keys(), key=lambda b: m_bands[b])
            macro_results[mname] = {
                "activity": round(float(m_act), 3),
                "dominant": dominant,
                "color": BAND_COLORS[dominant],
                "bands": {b: round(float(m_bands[b]), 3) for b in BANDS},
            }

        global_bands = {
            b: float(np.mean([region_results[r]["bands"][b] for r in REGION_MAPPING]))
            for b in BANDS
        }
        denom = global_bands["alpha"] + global_bands["theta"] + 1e-9
        engagement = float(np.clip(global_bands["beta"] / denom, 0.0, 3.0))

        alpha_prominence = float(
            np.mean(
                [
                    region_results["occipital_left"]["bands"]["alpha"],
                    region_results["occipital_right"]["bands"]["alpha"],
                    region_results["temporal_left"]["bands"]["alpha"],
                    region_results["temporal_right"]["bands"]["alpha"],
                ]
            )
        )

        try:
            dt = datetime.datetime.fromtimestamp(current_time)
            ts_iso = dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            ts_hms = dt.strftime("%H:%M:%S.%f")[:-4]
        except (ValueError, OSError, OverflowError):
            ts_iso = f"{current_time:.3f}s"
            ts_hms = f"{current_time:.2f}s"

        elapsed_sec = round(float(max(0.0, current_time - start_ref)), 2) if start_ref is not None else 0.0
        if is_replay and start_ref is not None and end_ref is not None and end_ref >= start_ref:
            duration_sec = round(float(max(0.0, end_ref - start_ref)), 2)
        elif is_replay and dur_override is not None and dur_override > 0:
            duration_sec = round(float(dur_override), 2)
        else:
            duration_sec = None

        frame_payload = {
            "timestamp": round(float(current_time), 3),
            "timestamp_iso": ts_iso,
            "time_hms": ts_hms,
            "elapsed_sec": elapsed_sec,
            "duration_sec": duration_sec,
            "is_replay": bool(is_replay or stream_mode == "replay"),
            "can_seek": can_seek,
            "stream_active": stream_active,
            "stream_mode": stream_mode,
            "session_id": session_id,
            "paused": is_paused,
            "sample_count": sample_count,
            "fs": round(float(fs), 1),
            "equalize": equalize,
            "baseline_locked": baseline_locked,
            "artifact": is_artifact,
            "engagement": round(engagement, 3),
            "alpha_prominence": round(alpha_prominence, 3),
            "global_dominant": max(global_bands.keys(), key=lambda b: global_bands[b]),
            "regions": region_results,
            "macro_regions": macro_results,
            "band_colors": BAND_COLORS,
        }
        with self.lock:
            self.last_artifact = is_artifact
            if self.equalize == equalize and self.baseline_locked == baseline_locked:
                self.smoothed_regions = region_results
                self.smoothed_macro = macro_results
            if self.is_calibrating:
                self.calibration_samples.append(raw_region_powers)
            self._last_frame = frame_payload
        return frame_payload

    def start_calibration(self):
        self.is_calibrating = True
        self.calibration_samples.clear()

    def finish_calibration(self):
        self.is_calibrating = False
        if not self.calibration_samples:
            return
        baseline: Dict[str, Dict[str, Tuple[float, float]]] = {}
        for rname in REGION_MAPPING:
            baseline[rname] = {}
            for b in BANDS:
                vals = [s[rname][b] for s in self.calibration_samples]
                mean_v = float(np.mean(vals))
                std_v = float(np.std(vals)) + 1e-6
                baseline[rname][b] = (mean_v, std_v)
                self.reg_baselines[rname][b] = mean_v
        self.calibration_baseline = baseline
        self.baseline_locked = True


def replay_csv_worker(
    csv_path: str,
    dsp: BrainDSP,
    loop: bool = True,
    speed: float = 1.0,
    stop_event: Optional[threading.Event] = None,
):
    print(f"[*] Starting Replay Worker from '{csv_path}' (loop={loop}, speed={speed}x)...")
    stop_ev = stop_event or threading.Event()

    while not stop_ev.is_set():
        try:
            timestamps_ns: List[Optional[int]] = []
            timestamps_s: List[float] = []
            samples_list: List[List[float]] = []

            with open(csv_path, "r", encoding="utf-8", errors="ignore") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                if not header:
                    print(f"[!] Empty CSV: {csv_path}")
                    stop_ev.wait(1.0)
                    continue

                col_indices = []
                for ch in EEG_CHANNELS:
                    if ch in header:
                        col_indices.append(header.index(ch))
                    else:
                        print(f"[!] Channel {ch} not found in header!")
                        return

                max_col = max(col_indices)
                fallback_t = time.time()
                for row in reader:
                    if stop_ev.is_set():
                        return
                    if not row or len(row) <= max_col:
                        continue
                    try:
                        vals = [float(row[idx]) for idx in col_indices]
                    except ValueError:
                        continue
                    try:
                        ts_ns = int(row[0])
                        ts_s = ts_ns / 1e9
                    except (ValueError, IndexError):
                        ts_ns = None
                        fallback_t += 1.0 / dsp.fs
                        ts_s = fallback_t
                    timestamps_ns.append(ts_ns)
                    timestamps_s.append(ts_s)
                    samples_list.append(vals)

            n_samples = len(samples_list)
            if n_samples == 0:
                print(f"[!] No valid EEG samples in {csv_path}")
                stop_ev.wait(1.0)
                continue

            first_ts = timestamps_s[0]
            last_ts = timestamps_s[-1]
            dsp.set_replay_bounds(first_ts, last_ts)

            state_lock = threading.Lock()
            state = {"idx": 0, "seeked": False}

            def handle_seek(target_elapsed_sec: float) -> Optional[float]:
                duration = max(0.0, last_ts - first_ts)
                clamped = max(0.0, min(duration, float(target_elapsed_sec)))
                target_ts = first_ts + clamped
                target_idx = bisect.bisect_left(timestamps_s, target_ts)
                if target_idx >= n_samples:
                    target_idx = n_samples - 1

                with dsp.lock:
                    win_len = dsp.window_len
                end_idx = min(n_samples, target_idx + 1)
                start_idx = max(0, end_idx - win_len)
                dsp.replace_window(samples_list[start_idx:end_idx], timestamps_s[start_idx:end_idx])

                with state_lock:
                    state["idx"] = min(end_idx, n_samples - 1)
                    state["seeked"] = True

                return round(timestamps_s[end_idx - 1] - first_ts, 2)

            dsp.set_seek_callback(handle_seek)

            while not stop_ev.is_set():
                last_ts_ns = None
                last_wall = time.perf_counter()

                while not stop_ev.is_set():
                    while dsp.paused and not stop_ev.is_set():
                        with state_lock:
                            if state["seeked"]:
                                state["seeked"] = False
                                last_ts_ns = None
                                last_wall = time.perf_counter()
                        stop_ev.wait(0.02)
                        last_wall = time.perf_counter()
                    if stop_ev.is_set():
                        return

                    with state_lock:
                        if state["seeked"]:
                            state["seeked"] = False
                            last_ts_ns = None
                            last_wall = time.perf_counter()
                        i = state["idx"]
                        if i >= n_samples:
                            break
                        state["idx"] = i + 1

                    ts_ns = timestamps_ns[i]
                    ts_s = timestamps_s[i]
                    vals = samples_list[i]
                    dsp.push_sample(vals, ts_s)

                    if ts_ns is not None and last_ts_ns is not None:
                        dt_sim = (ts_ns - last_ts_ns) / 1e9 / speed
                        if 0 < dt_sim < 0.5:
                            now = time.perf_counter()
                            delay = dt_sim - (now - last_wall)
                            while delay > 0.001 and not stop_ev.is_set():
                                if dsp.paused or state["seeked"]:
                                    last_wall = time.perf_counter()
                                    break
                                step = min(delay, 0.02)
                                stop_ev.wait(step)
                                delay -= step
                            last_wall = time.perf_counter()
                    else:
                        stop_ev.wait(1.0 / (dsp.fs * speed))

                    if ts_ns is not None and ts_ns != last_ts_ns:
                        last_ts_ns = ts_ns

                if not loop or stop_ev.is_set():
                    return
                with state_lock:
                    state["idx"] = 0

        except Exception as e:
            print(f"[!] Replay error: {e}")
            stop_ev.wait(1.0)


def _parse_timestamp_sec(token: str) -> Optional[float]:
    tok = token.strip()
    if not tok:
        return None
    try:
        v = float(tok)
    except ValueError:
        if ("T" in tok and ":" in tok) or tok.endswith("Z"):
            try:
                t = tok[:-1] + "+00:00" if tok.endswith("Z") else tok
                return datetime.datetime.fromisoformat(t).timestamp()
            except ValueError:
                return None
        return None
    a = abs(v)
    if a > 1e17:
        return v / 1e9
    if a > 1e14:
        return v / 1e6
    if a > 1e11:
        return v / 1e3
    return v


def parse_csv_lines_for_dsp(
    header_line: str,
    lines: List[str],
    default_fs: float = DEFAULT_FS,
) -> Tuple[List[List[float]], List[float]]:
    """Parse a CSV header and data lines into ``(samples_14ch, timestamps_s)``."""
    header = [p.strip() for p in header_line.split(",")]
    col_indices = [header.index(ch) if ch in header else -1 for ch in EEG_CHANNELS]
    if all(i < 0 for i in col_indices):
        return [], []
    max_idx = max(col_indices)
    samples: List[List[float]] = []
    timestamps: List[float] = []
    fallback_t = time.time()
    step = 1.0 / max(1.0, default_fs)

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) <= max_idx:
            continue
        try:
            vals = [float(parts[idx]) if idx >= 0 else 0.0 for idx in col_indices]
        except ValueError:
            continue
        ts_s = _parse_timestamp_sec(parts[0])
        if ts_s is None:
            fallback_t += step
            ts_s = fallback_t
        else:
            fallback_t = ts_s
        samples.append(vals)
        timestamps.append(ts_s)
    return samples, timestamps


def stdin_reader_worker(
    dsp: BrainDSP,
    stream=None,
    stop_event: Optional[threading.Event] = None,
):
    print("[*] Stream Reader active. Awaiting EEG CSV stream...")
    header = None
    col_indices = None
    ts_history = collections.deque(maxlen=128)
    last_ts_s: Optional[float] = None
    src = stream if stream is not None else sys.stdin
    stop_ev = stop_event or threading.Event()
    if src is None:
        return

    for line in src:
        if stop_ev.is_set():
            break
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            if line.startswith("#TOTAL,"):
                raw_total = line[7:].strip()
                if not raw_total:
                    dsp.clear_replay_mode()
                    ts_history.clear()
                    last_ts_s = None
                else:
                    try:
                        total_ms = float(raw_total)
                        if total_ms >= 0.0:
                            if dsp.sample_count > 0 or dsp.session_start_ts is not None:
                                dsp.reset_session()
                                ts_history.clear()
                                last_ts_s = None
                            dsp.set_replay_duration(total_ms / 1000.0)
                    except ValueError:
                        pass
            elif line.startswith("#BOUNDS,"):
                raw_bounds = line[8:].strip()
                if not raw_bounds:
                    with dsp.lock:
                        dsp.replay_start_ts = None
                        dsp.replay_end_ts = None
                else:
                    bparts = [p.strip() for p in raw_bounds.split(",")]
                    if len(bparts) >= 2:
                        try:
                            s_ts = float(bparts[0])
                            e_ts = float(bparts[1])
                            dsp.set_replay_bounds(s_ts, e_ts)
                            ts_history.clear()
                            last_ts_s = None
                        except ValueError:
                            pass
            elif line.startswith("#SEEK,"):
                dsp.reset_window()
                ts_history.clear()
                last_ts_s = None
            continue

        parts = [p.strip() for p in line.split(",")]
        if any(ch in parts for ch in EEG_CHANNELS):
            new_indices = [parts.index(ch) if ch in parts else -1 for ch in EEG_CHANNELS]
            if parts != header:
                header = parts
                col_indices = new_indices
                ts_history.clear()
                present = sum(1 for i in col_indices if i >= 0)
                print(f"[*] Detected CSV Header with {present}/14 EEG channels.")
            continue

        if not col_indices:
            continue
        max_idx = max(col_indices)
        if len(parts) <= max_idx:
            continue

        ts_s = _parse_timestamp_sec(parts[0])
        if ts_s is None:
            ts_s = time.time()

        try:
            vals = [float(parts[idx]) if idx >= 0 else 0.0 for idx in col_indices]
        except ValueError:
            continue

        if last_ts_s is not None and ts_s < last_ts_s - 1.0:
            ts_history.clear()
            with dsp.lock:
                if dsp.is_replay and dsp.replay_start_ts is None:
                    dsp.session_start_ts = ts_s
        last_ts_s = ts_s

        dsp.push_sample(vals, ts_s)
        ts_history.append(ts_s)
        if len(ts_history) >= 64:
            span = ts_history[-1] - ts_history[0]
            if span > 0.1:
                dsp.set_sampling_rate((len(ts_history) - 1) / span)


class SSEBroadcaster:
    """Manages active SSE client connections and broadcasts frames."""

    def __init__(self):
        self.clients: List[queue.Queue] = []
        self.lock = threading.Lock()
        self._closed = False

    def add_client(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=4)
        with self.lock:
            if self._closed:
                q.put_nowait(None)
                return q
            self.clients.append(q)
        return q

    def remove_client(self, q: queue.Queue):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    def _enqueue_all(self, data: bytes) -> None:
        with self.lock:
            clients_snapshot = list(self.clients)
        for q in clients_snapshot:
            if q.full():
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
            try:
                q.put_nowait(data)
            except queue.Full:
                pass

    def broadcast(self, payload: Dict):
        data = f"data: {json.dumps(payload)}\n\n".encode("utf-8")
        self._enqueue_all(data)

    def broadcast_event(self, event_name: str, payload: Dict):
        data = f"event: {event_name}\ndata: {json.dumps(payload)}\n\n".encode("utf-8")
        self._enqueue_all(data)

    def close_all(self):
        with self.lock:
            self._closed = True
            clients_snapshot = list(self.clients)
        for q in clients_snapshot:
            try:
                while not q.empty():
                    q.get_nowait()
            except queue.Empty:
                pass
            try:
                q.put_nowait(None)
            except queue.Full:
                pass


_ASSET_BYTES_CACHE: Dict[Path, Tuple[int, bytes]] = {}
_ASSET_CACHE_LOCK = threading.Lock()


class BrainServerHandler(http.server.BaseHTTPRequestHandler):
    """Handles bundled static assets, REST API endpoints, and SSE live stream."""

    dsp: BrainDSP
    broadcaster: SSEBroadcaster
    assets_dir: Path
    stop_event: threading.Event
    on_pause_request: Optional[Callable[[Optional[bool]], bool]] = None
    on_start_request: Optional[Callable[[], bool]] = None

    def _serve_file(self, file_path: Path, content_type: str, cache_control: str = "no-cache") -> bool:
        try:
            st = file_path.stat()
        except OSError:
            return False
        mtime_ns = st.st_mtime_ns
        with _ASSET_CACHE_LOCK:
            cached = _ASSET_BYTES_CACHE.get(file_path)
        if cached is not None and cached[0] == mtime_ns:
            data = cached[1]
        else:
            try:
                data = file_path.read_bytes()
            except OSError:
                return False
            with _ASSET_CACHE_LOCK:
                if len(_ASSET_BYTES_CACHE) > 32:
                    _ASSET_BYTES_CACHE.clear()
                _ASSET_BYTES_CACHE[file_path] = (mtime_ns, data)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)
        return True

    def _send_json(self, obj: Dict, status: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]

        if path in ("/stream", "/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            q = self.broadcaster.add_client()
            try:
                last_frame = self.dsp.get_last_frame()
                if last_frame is not None:
                    self.wfile.write(f"data: {json.dumps(last_frame)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                else:
                    with self.dsp.lock:
                        status_payload = {
                            "paused": self.dsp.paused,
                            "is_replay": bool(self.dsp.is_replay or self.dsp.stream_mode == "replay"),
                            "stream_active": self.dsp.stream_active,
                            "stream_mode": self.dsp.stream_mode,
                        }
                    self.wfile.write(f"event: status\ndata: {json.dumps(status_payload)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                while not self.stop_event.is_set():
                    try:
                        data = q.get(timeout=0.5)
                    except queue.Empty:
                        data = b": keepalive\n\n"
                    if data is None:
                        break
                    self.wfile.write(data)
                    self.wfile.flush()
            except (
                BrokenPipeError,
                ConnectionResetError,
                ConnectionAbortedError,
                OSError,
                ValueError,
                KeyboardInterrupt,
            ):
                pass
            finally:
                self.broadcaster.remove_client(q)
            return

        if path == "/api/status":
            with self.dsp.lock:
                status_obj = {
                    "sample_count": self.dsp.sample_count,
                    "fs": round(self.dsp.fs, 1),
                    "window_sec": self.dsp.window_sec,
                    "clients": len(self.broadcaster.clients),
                    "is_calibrating": self.dsp.is_calibrating,
                    "has_baseline": self.dsp.calibration_baseline is not None,
                    "last_artifact": self.dsp.last_artifact,
                    "is_replay": bool(self.dsp.is_replay or self.dsp.stream_mode == "replay"),
                    "stream_active": self.dsp.stream_active,
                    "stream_mode": self.dsp.stream_mode,
                    "paused": self.dsp.paused,
                }
            self._send_json(status_obj)
            return

        if path in ("/", "/index.html", "/brain_viewer.html"):
            if self._serve_file(
                self.assets_dir / "brain_viewer.html",
                "text/html; charset=utf-8",
                "no-store, no-cache, must-revalidate, max-age=0",
            ):
                return

        if path == "/brain_lobes.glb":
            if self._serve_file(self.assets_dir / "brain_lobes.glb", "model/gltf-binary", "public, max-age=86400"):
                return

        if path in ("/compare", "/simple-viewer.html"):
            if self._serve_file(
                self.assets_dir / "simple-viewer.html",
                "text/html; charset=utf-8",
                "no-store, no-cache, must-revalidate, max-age=0",
            ):
                return

        if path == "/favicon.ico":
            if self._serve_file(self.assets_dir / "epoc_studio.ico", "image/x-icon", "public, max-age=86400"):
                return

        if path.startswith("/vendor/three/"):
            rel = path[len("/vendor/three/") :]
            if "/" not in rel and "\\" not in rel and ".." not in rel and rel.endswith(".js"):
                if self._serve_file(
                    self.assets_dir / "vendor" / "three" / rel,
                    "application/javascript; charset=utf-8",
                    "public, max-age=86400",
                ):
                    return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if self.path == "/api/pause":
            length = int(self.headers.get("Content-Length", "0") or 0)
            desired_paused: Optional[bool] = None
            action: Optional[str] = None
            if length > 0:
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    if isinstance(payload, dict):
                        if "paused" in payload and payload["paused"] is not None:
                            desired_paused = bool(payload["paused"])
                        if isinstance(payload.get("action"), str):
                            action = payload["action"].strip().lower()
                except Exception:
                    pass

            with self.dsp.lock:
                currently_active = self.dsp.stream_active

            if not currently_active:
                start_fn = self.on_start_request
                if start_fn is not None and (action == "start" or desired_paused is False or desired_paused is None):
                    start_fn()
                with self.dsp.lock:
                    is_paused = self.dsp.paused
                    is_active = self.dsp.stream_active
                    mode = self.dsp.stream_mode
                self._send_json(
                    {
                        "status": "ok",
                        "paused": is_paused,
                        "stream_active": is_active,
                        "stream_mode": mode,
                    }
                )
                return

            if action == "start":
                desired_paused = False

            pause_fn = self.on_pause_request
            if pause_fn is not None:
                is_paused = bool(pause_fn(desired_paused))
            else:
                is_paused = self.dsp.set_paused(desired_paused)
                frame = self.dsp.compute_frame()
                if frame is not None:
                    self.broadcaster.broadcast(frame)
                else:
                    with self.dsp.lock:
                        mode = self.dsp.stream_mode
                        is_active = self.dsp.stream_active
                    self.broadcaster.broadcast_event(
                        "status",
                        {
                            "paused": is_paused,
                            "is_replay": bool(self.dsp.is_replay or mode == "replay"),
                            "stream_active": is_active,
                            "stream_mode": mode,
                        },
                    )
            with self.dsp.lock:
                is_paused = self.dsp.paused
                is_active = self.dsp.stream_active
                mode = self.dsp.stream_mode
            self._send_json(
                {
                    "status": "ok",
                    "paused": is_paused,
                    "stream_active": is_active,
                    "stream_mode": mode,
                }
            )
            return

        if self.path == "/api/seek":
            length = int(self.headers.get("Content-Length", "0") or 0)
            target_elapsed: Optional[float] = None
            if length > 0:
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    if isinstance(payload, dict):
                        if "elapsed_sec" in payload:
                            target_elapsed = float(payload["elapsed_sec"])
                        elif "progress" in payload:
                            with self.dsp.lock:
                                s_ts = self.dsp.replay_start_ts
                                e_ts = self.dsp.replay_end_ts
                                dur_ov = self.dsp.replay_duration_override
                            if s_ts is not None and e_ts is not None and e_ts > s_ts:
                                target_elapsed = float(payload["progress"]) * (e_ts - s_ts)
                            elif dur_ov is not None and dur_ov > 0:
                                target_elapsed = float(payload["progress"]) * dur_ov
                except Exception:
                    pass

            actual_elapsed = None
            frame = None
            if target_elapsed is not None:
                actual_elapsed = self.dsp.seek_to_elapsed(target_elapsed)
                frame = self.dsp.compute_frame(force=True)
                if frame is not None:
                    self.broadcaster.broadcast(frame)
            self._send_json({"status": "ok", "elapsed_sec": actual_elapsed, "frame": frame})
            return

        if self.path == "/api/calibrate":
            length = int(self.headers.get("Content-Length", "0") or 0)
            is_reset = False
            if length > 0:
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    if isinstance(payload, dict) and payload.get("reset"):
                        is_reset = True
                except Exception:
                    pass

            if is_reset:
                self.dsp.reset_baseline()
                self._send_json({"status": "reset", "baseline_locked": False})
            else:
                self.dsp.start_calibration()
                stop_ev = self.stop_event

                def _finish():
                    if not stop_ev.wait(5.0):
                        self.dsp.finish_calibration()
                        print("[*] Baseline calibration complete (LOCKED).")

                threading.Thread(target=_finish, daemon=True).start()
                self._send_json({"status": "calibrating", "duration_sec": 5.0})
            return

        if self.path == "/api/equalize":
            length = int(self.headers.get("Content-Length", "0") or 0)
            target_eq = None
            if length > 0:
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    if isinstance(payload, dict) and "equalize" in payload:
                        target_eq = bool(payload["equalize"])
                except Exception:
                    pass
            new_eq = self.dsp.set_equalize(target_eq)
            self._send_json({"status": "ok", "equalize": new_eq})
            return

        self.send_response(404)
        self.end_headers()

    def address_string(self):
        # Avoid reverse DNS lookups on localhost static and SSE requests.
        return self.client_address[0]

    def log_message(self, fmt, *args):
        first = str(args[0]) if args else ""
        if "GET /stream" in first or "GET /events" in first or "GET /api/" in first:
            return
        if sys.stderr is None:
            return
        super().log_message(fmt, *args)


def dsp_broadcast_loop(
    dsp: BrainDSP,
    broadcaster: SSEBroadcaster,
    fps: float,
    stop_event: Optional[threading.Event] = None,
):
    interval = 1.0 / max(1.0, fps)
    stop_ev = stop_event or threading.Event()
    while not stop_ev.is_set():
        t_start = time.perf_counter()
        try:
            frame = dsp.compute_frame()
            if frame is not None:
                broadcaster.broadcast(frame)
        except Exception as e:
            print(f"[!] DSP Broadcast error: {e}", file=sys.stderr)
        elapsed = time.perf_counter() - t_start
        sleep_dur = interval - elapsed
        if sleep_dur > 0:
            stop_ev.wait(sleep_dur)


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def handle_error(self, request, client_address):
        _, exc, _ = sys.exc_info()
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        if isinstance(exc, OSError) and getattr(exc, "winerror", None) in (10053, 10054):
            return
        super().handle_error(request, client_address)


class BrainServerController:
    """Manages a lifecycle-safe BrainDSP + local HTTP/SSE server."""

    def __init__(
        self,
        dsp: BrainDSP,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        fps: float = DEFAULT_FPS,
        allow_ephemeral_fallback: bool = True,
        stream=None,
        replay_file: Optional[str] = None,
        replay_loop: bool = True,
        replay_speed: float = 1.0,
        on_close: Optional[Callable[[], None]] = None,
        on_pause_request: Optional[Callable[[Optional[bool]], bool]] = None,
        on_start_request: Optional[Callable[[], bool]] = None,
        on_seek_request: Optional[Callable[[float], Optional[float]]] = None,
    ):
        self.dsp = dsp
        self.host = host
        self.requested_port = port
        self.port = port
        self.fps = fps
        self.allow_ephemeral_fallback = allow_ephemeral_fallback
        self._stream = stream
        self._replay_file = replay_file
        self._replay_loop = replay_loop
        self._replay_speed = replay_speed
        self._on_close = on_close
        self._on_pause_request = on_pause_request
        self._on_start_request = on_start_request
        self._on_seek_request = on_seek_request

        self.broadcaster = SSEBroadcaster()
        self.stop_event = threading.Event()
        self._server: Optional[_ThreadedHTTPServer] = None
        self._threads: list[threading.Thread] = []
        self._stopped = False

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _broadcast_state_update(self, is_active: bool, is_paused: bool, mode: str) -> None:
        frame = self.dsp.compute_frame()
        if frame is not None:
            self.broadcaster.broadcast(frame)
        self.broadcaster.broadcast_event(
            "status",
            {
                "paused": is_paused,
                "is_replay": bool(self.dsp.is_replay or mode == "replay"),
                "stream_active": is_active,
                "stream_mode": mode,
            },
        )

    def set_paused(self, paused: Optional[bool] = None) -> bool:
        """Synchronize pause state onto this brain DSP and push an immediate SSE update."""
        if self._stopped:
            return self.dsp.paused
        is_paused = self.dsp.set_paused(paused)
        with self.dsp.lock:
            is_active = self.dsp.stream_active
            mode = self.dsp.stream_mode
        self._broadcast_state_update(is_active, is_paused, mode)
        return is_paused

    def set_stream_state(self, mode: str, paused: bool) -> Tuple[bool, bool]:
        """Synchronize stream mode and pause state onto this brain DSP and push an SSE update."""
        if self._stopped:
            return self.dsp.stream_active, self.dsp.paused
        is_active, is_paused = self.dsp.set_stream_state(mode, paused)
        self._broadcast_state_update(is_active, is_paused, mode)
        return is_active, is_paused

    def start(self) -> str:
        class BoundHandler(BrainServerHandler):
            pass

        BoundHandler.dsp = self.dsp
        BoundHandler.broadcaster = self.broadcaster
        BoundHandler.assets_dir = asset_path()
        BoundHandler.stop_event = self.stop_event
        BoundHandler.on_pause_request = staticmethod(self._on_pause_request) if self._on_pause_request else None
        BoundHandler.on_start_request = staticmethod(self._on_start_request) if self._on_start_request else None
        if self._on_seek_request is not None:
            self.dsp.set_seek_callback(self._on_seek_request)

        try:
            self._server = _ThreadedHTTPServer((self.host, self.requested_port), BoundHandler)
        except OSError:
            if not self.allow_ephemeral_fallback or self.requested_port == 0:
                raise
            self._server = _ThreadedHTTPServer((self.host, 0), BoundHandler)

        self.port = self._server.server_address[1]

        if self._replay_file:
            t_feed = threading.Thread(
                target=replay_csv_worker,
                args=(self._replay_file, self.dsp, self._replay_loop, self._replay_speed, self.stop_event),
                daemon=True,
            )
        else:
            t_feed = threading.Thread(
                target=stdin_reader_worker,
                args=(self.dsp, self._stream, self.stop_event),
                daemon=True,
            )
        t_dsp = threading.Thread(
            target=dsp_broadcast_loop,
            args=(self.dsp, self.broadcaster, self.fps, self.stop_event),
            daemon=True,
        )
        t_srv = threading.Thread(target=self._server.serve_forever, daemon=True)

        self._threads = [t_feed, t_dsp, t_srv]
        for t in self._threads:
            t.start()

        return self.url

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self.stop_event.set()
        self.broadcaster.close_all()
        if self._on_close is not None:
            try:
                self._on_close()
            except Exception:
                pass
        if self._server is not None:
            srv = self._server
            self._server = None
            try:
                srv.shutdown()
            except Exception:
                pass
            try:
                srv.server_close()
            except Exception:
                pass


def create_brain_map_window(
    dsp: Optional[BrainDSP] = None,
    stream=None,
    replay_file: Optional[str] = None,
    replay_loop: bool = True,
    replay_speed: float = 1.0,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    fps: float = DEFAULT_FPS,
    allow_ephemeral_fallback: bool = True,
    on_close: Optional[Callable[[], None]] = None,
    on_pause_request: Optional[Callable[[Optional[bool]], bool]] = None,
    on_start_request: Optional[Callable[[], bool]] = None,
    on_seek_request: Optional[Callable[[float], Optional[float]]] = None,
    parent=None,
):
    from PyQt6 import QtCore
    from .web_common import BaseWebWindow

    dsp_obj = dsp or BrainDSP()
    controller = BrainServerController(
        dsp=dsp_obj,
        host=host,
        port=port,
        fps=fps,
        allow_ephemeral_fallback=allow_ephemeral_fallback,
        stream=stream,
        replay_file=replay_file,
        replay_loop=replay_loop,
        replay_speed=replay_speed,
        on_close=on_close,
        on_pause_request=on_pause_request,
        on_start_request=on_start_request,
        on_seek_request=on_seek_request,
    )
    url = controller.start()

    win = BaseWebWindow(
        title="EPOC-X 3D Brain Map",
        bg_hex="#030508",
        width=1360,
        height=860,
        on_close=controller.stop,
        parent=parent,
    )
    win.server_controller = controller  # type: ignore[attr-defined]
    win.view.load(QtCore.QUrl(url))
    return win


def main(argv: Optional[list[str]] = None) -> int:
    ensure_stdio()
    parser = argparse.ArgumentParser(
        prog="epoc-brain-map",
        description="EPOC-X 3D Brain Activity Viewer & Streaming Server",
    )
    parser.add_argument("--replay", type=str, default=None, help="Path to recorded EEG CSV to replay")
    parser.add_argument(
        "--connect",
        type=str,
        default=None,
        metavar="HOST:PORT",
        help="Connect to local EPOC-X stream broker instead of stdin",
    )
    parser.add_argument("--no-loop", action="store_true", help="Don't loop CSV replay at EOF")
    parser.add_argument("--speed", type=float, default=1.0, help="Replay playback speed multiplier")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"HTTP/SSE port (default: {DEFAULT_PORT})")
    parser.add_argument("--host", type=str, default=DEFAULT_HOST, help=f"Host interface (default: {DEFAULT_HOST})")
    parser.add_argument("--fps", type=float, default=DEFAULT_FPS, help=f"Update rate in Hz (default: {DEFAULT_FPS})")
    parser.add_argument("--window", type=float, default=DEFAULT_WINDOW_SEC, help="FFT window in seconds")
    parser.add_argument("--smooth", type=float, default=0.25, help="EMA smoothing factor 0.0-1.0")
    parser.add_argument("--raw-spectrum", action="store_true", help="Disable 1/f spectral equalization")
    parser.add_argument("--whitening-gamma", type=float, default=1.0, help="Exponent for 1/f spectral whitening")
    parser.add_argument("--delta-cutoff", type=float, default=1.5, help="Delta band lower cutoff in Hz")
    parser.add_argument("--delta-weight", type=float, default=1.0, help="Delta scaling weight")
    parser.add_argument("--shelf-knee", type=float, default=15.0, help="High-frequency shelf knee in Hz")
    parser.add_argument("--no-cmr", action="store_true", help="Disable Spatial Common Median Reference (CMR)")
    parser.add_argument(
        "--server-only",
        "--headless",
        action="store_true",
        dest="server_only",
        help="Run only the local HTTP/SSE server without opening a desktop window",
    )
    args = parser.parse_args(argv)

    replay_file = args.replay
    connect_stream = None
    connect_sock = None
    if args.connect:
        connect_sock, connect_stream = open_broker_stream(args.connect)
    elif not replay_file and sys.stdin is not None and not sys.stdin.isatty():
        pass
    elif not replay_file:
        default_csv = get_recordings_dir() / "eeg_recording.csv"
        if default_csv.is_file():
            print(f"[*] No live input detected. Defaulting to replay: {default_csv}")
            replay_file = str(default_csv)

    dsp = BrainDSP(
        window_sec=args.window,
        fs=DEFAULT_FS,
        ema_alpha=args.smooth,
        equalize=not args.raw_spectrum,
        whitening_gamma=args.whitening_gamma,
        delta_cutoff=args.delta_cutoff,
        delta_weight=args.delta_weight,
        shelf_knee_hz=args.shelf_knee,
        use_cmr=not args.no_cmr,
    )

    def _cleanup():
        if connect_sock is not None:
            try:
                connect_sock.close()
            except OSError:
                pass

    if args.server_only:
        ctrl = BrainServerController(
            dsp=dsp,
            host=args.host,
            port=args.port,
            fps=args.fps,
            allow_ephemeral_fallback=False,
            stream=connect_stream,
            replay_file=replay_file,
            replay_loop=not args.no_loop,
            replay_speed=args.speed,
            on_close=_cleanup,
        )
        url = ctrl.start()
        source_label = (
            f"Replay: {replay_file}"
            if replay_file
            else (f"Broker: {args.connect}" if args.connect else "Live stdin")
        )
        print("=" * 64)
        print("  [BRAIN-3D] EPOC-X 3D CORTICAL ACTIVITY STREAMING SERVER")
        print(f"  Web Viewer & Stream:  {url}")
        print(f"  DSP Update Rate:      {args.fps} Hz | FFT Window: {args.window}s")
        print(f"  Input Source:         {source_label}")
        print("  Press Ctrl+C to stop.")
        print("=" * 64)
        try:
            while not ctrl.stop_event.is_set():
                time.sleep(0.25)
        except KeyboardInterrupt:
            print("\n[*] Shutting down server...")
        finally:
            ctrl.stop()
        return 0

    app = ensure_qt_app(sys.argv)
    win = create_brain_map_window(
        dsp=dsp,
        stream=connect_stream,
        replay_file=replay_file,
        replay_loop=not args.no_loop,
        replay_speed=args.speed,
        host=args.host,
        port=args.port,
        fps=args.fps,
        allow_ephemeral_fallback=True,
        on_close=_cleanup,
    )
    install_sigint_handler(win, lambda: (win.shutdown(), app.quit()))
    win.show()
    apply_dark_titlebar(win)
    try:
        return app.exec()
    finally:
        win.shutdown()
