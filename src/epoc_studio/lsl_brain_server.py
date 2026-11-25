"""Headless 3D Brain Map web backend that follows Lab Streaming Layer streams (no Qt, no BLE).

One process serves the brain map for several devices at once: for each device one LSL inlet (by stream name,
as published by ``epoc-direct-record --lsl --lsl-name NAME`` / ``muse-record --lsl --lsl-name NAME``), one
``BrainDSP`` and one SSE broadcaster. The viewer page chooses the device with ``?device=ID`` (``/stream``,
``/api/*``); ``GET /api/devices`` lists them. Streams that are not up yet are waited for, lost ones are
re-resolved, so devices may come and go.

  * EPOC-X: 14 channels at 128 Hz, all 8 cortical regions.
  * Muse: only TP9, AF7, AF8, TP10 at 256 Hz (the rows of the stream with the 'eeg' stream code). AF7/AF8 stand
    for both frontal regions of their side, TP9/TP10 for the temporal ones; occipital has no electrode and is
    shown dark; the parietal crown is interpolated from the rest (see MUSE_REGION_MAPPING).

    python -m epoc_studio.lsl_brain_server --port 8766 \\
        epocx1='EPOCX (E1234567)' epocx2='EPOCX (E7654321)' muse1=Muse

The page itself can be served by this process (``/``) or by another web server (it then talks to this one,
with CORS, on its own port: see brain_viewer.html, STUDIO_PORT).
"""
from __future__ import annotations

import argparse
import email.utils
import http.server
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import numpy as np

from .brain_map_view import (
    DEFAULT_FPS,
    EEG_CHANNELS,
    MUSE_ARTIFACT_STD,
    MUSE_CHANNELS,
    MUSE_FS,
    MUSE_REGION_MAPPING,
    BrainDSP,
    BrainServerHandler,
    SSEBroadcaster,
    _ThreadedHTTPServer,
    asset_path,
)

OFFLINE_AFTER_S = 3.0          # no sample for this long: the device is shown as idle
EPOC_FS = 128.0
EPOC_FALLBACK_COLUMNS = list(range(2, 16))   # 27-channel EPOC stream without labels: packet_size, frame_counter, 14 EEG...


def log(msg: str) -> None:
    print(f"# {msg}", file=sys.stderr, flush=True)


class Device:
    """One followed LSL stream with its DSP and SSE clients."""

    def __init__(self, dev_id: str, lsl_name: str):
        self.id = dev_id
        self.lsl_name = lsl_name
        self.kind = "muse" if lsl_name.lower().startswith("muse") else "epocx"
        if self.kind == "muse":
            self.dsp = BrainDSP(
                fs=MUSE_FS, channels=MUSE_CHANNELS, region_mapping=MUSE_REGION_MAPPING,
                artifact_std=MUSE_ARTIFACT_STD, use_cmr=False,   # CMR of just 4 channels would eat real signal
            )
        else:
            self.dsp = BrainDSP(fs=EPOC_FS)
        self.dsp.set_stream_state("idle", False)   # until the first samples arrive
        self.broadcaster = SSEBroadcaster()
        self.last_sample_mono = 0.0
        self.lsl_source_id = ""

    @property
    def online(self) -> bool:
        return self.last_sample_mono > 0 and time.monotonic() - self.last_sample_mono < OFFLINE_AFTER_S

    def set_active(self, active: bool) -> None:
        with self.dsp.lock:
            if self.dsp.stream_active == active:
                return
        if active:
            self.dsp.reset_window()          # drop the samples from before the gap
            self.dsp.set_stream_state("live", False)
        else:
            self.dsp.set_stream_state("idle", False)
        self.broadcaster.broadcast_event(
            "status", {"paused": False, "is_replay": False, "stream_active": active,
                       "stream_mode": "live" if active else "idle"})


def _labels(info) -> List[str]:
    labels = []
    try:
        ch = info.desc().child("channels").child("channel")
        while not ch.empty():
            labels.append(ch.child_value("label"))
            ch = ch.next_sibling()
    except Exception:
        return []
    return labels if len(labels) == info.channel_count() else []


def _columns(dev: Device, info) -> Optional[Dict]:
    """How to cut this stream's samples into the DSP's channel vectors, or None if it does not fit."""
    labels = _labels(info)
    if dev.kind == "muse":
        if not labels or "stream" not in labels or any(c not in labels for c in MUSE_CHANNELS):
            return None
        codes = info.desc().child_value("stream_codes").split(",") or ["eeg"]
        eeg_code = codes.index("eeg") if "eeg" in codes else 0
        return {"cols": [labels.index(c) for c in MUSE_CHANNELS], "code_col": labels.index("stream"), "code": eeg_code}
    if labels and all(c in labels for c in EEG_CHANNELS):
        return {"cols": [labels.index(c) for c in EEG_CHANNELS], "code_col": None, "code": None}
    if info.channel_count() == 27:
        return {"cols": EPOC_FALLBACK_COLUMNS, "code_col": None, "code": None}
    return None


def lsl_reader(dev: Device, stop: threading.Event) -> None:
    from pylsl import StreamInlet, resolve_byprop

    waiting_logged = False
    while not stop.is_set():
        infos = resolve_byprop("name", dev.lsl_name, timeout=2.0)
        if not infos:
            dev.set_active(False)
            if not waiting_logged:
                log(f"[{dev.id}] waiting for LSL stream '{dev.lsl_name}'")
                waiting_logged = True
            stop.wait(1.0)
            continue
        waiting_logged = False
        inlet = StreamInlet(infos[0], max_buflen=10, recover=True)
        info = inlet.info()
        layout = _columns(dev, info)
        if layout is None:
            log(f"[{dev.id}] stream '{dev.lsl_name}' has no usable EEG channels, ignoring it")
            stop.wait(5.0)
            continue
        dev.lsl_source_id = info.source_id()
        log(f"[{dev.id}] following '{dev.lsl_name}' ({info.source_id()}, {info.channel_count()} ch)")
        cols = layout["cols"]
        while not stop.is_set():
            samples, stamps = inlet.pull_chunk(timeout=0.25, max_samples=512)
            if stamps:
                arr = np.asarray(samples, dtype=np.float64)
                if layout["code_col"] is not None:   # Muse: rows of all sensors are interleaved
                    keep = arr[:, layout["code_col"]] == layout["code"]
                    arr = arr[keep]
                    stamps = [t for t, k in zip(stamps, keep) if k]
                if len(arr):
                    vec = arr[:, cols]
                    ok = np.isfinite(vec).all(axis=1)     # contact lost: NaN rows are skipped
                    if ok.any():
                        now = time.time()
                        ts = [t if t > 1e9 else now for t, k in zip(stamps, ok) if k]
                        dev.dsp.push_batch(vec[ok], ts)
                        dev.last_sample_mono = time.monotonic()
                        dev.set_active(True)
            if not dev.online:
                dev.set_active(False)


def dsp_loop(dev: Device, fps: float, stop: threading.Event) -> None:
    interval = 1.0 / max(1.0, fps)
    while not stop.is_set():
        if not dev.broadcaster.clients:   # nobody watching this device: no need to compute
            stop.wait(0.25)
            continue
        t0 = time.perf_counter()
        try:
            frame = dev.dsp.compute_frame()
            if frame is not None:
                dev.broadcaster.broadcast(frame)
        except Exception as e:  # keep serving
            print(f"[!] DSP error ({dev.id}): {e}", file=sys.stderr, flush=True)
        stop.wait(max(0.0, interval - (time.perf_counter() - t0)))


class MultiDeviceHandler(BrainServerHandler):
    """BrainServerHandler with the device chosen per request (?device=ID) and CORS for a page served elsewhere."""

    devices: Dict[str, Device]
    default_device: str
    logger_port: Optional[int] = None    # a web app on this host to link back to (e.g. an activity logger)
    headmap_dir: Optional[Path] = None   # where the head map feeder's status-image-<device>.png files are

    def _send_headmap(self) -> None:
        """The live head map image of the chosen device (drawn by an external head map feeder):
        with ETag / Last-Modified / Date so the page can tell a fresh frame from a dead feeder."""
        file = self.headmap_dir / f"status-image-{self._cur.id}.png" if self.headmap_dir else None
        try:
            st = file.stat() if file else None
        except OSError:
            st = None
        if st is None:
            self._send_json({"error": "no head map"}, 404)
            return
        etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
        last_mod = email.utils.formatdate(st.st_mtime, usegmt=True)
        same = self.headers.get("If-None-Match") == etag
        self.send_response(304 if same else 200)
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", last_mod)
        self.send_header("Cache-Control", "no-cache")
        self._cors()
        self.send_header("Access-Control-Expose-Headers", "ETag, Date, Last-Modified")   # readable by the page
        if same:
            self.end_headers()
            return
        data = file.read_bytes()
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _pick(self) -> Device:
        q = parse_qs(urlparse(self.path).query)
        dev_id = (q.get("device") or [self.default_device])[0]
        return self.devices.get(dev_id) or self.devices[self.default_device]

    @property
    def dsp(self) -> BrainDSP:               # type: ignore[override]
        return self._cur.dsp

    @property
    def broadcaster(self) -> SSEBroadcaster:  # type: ignore[override]
        return self._cur.broadcaster

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self._cur = self._pick()
        if self.path.split("?")[0] == "/api/headmap":
            self._send_headmap()
            return
        if self.path.split("?")[0] == "/api/devices":
            self._send_json({
                "live_only": True,
                "default": self.default_device,
                "logger_port": self.logger_port,
                "devices": [
                    {"id": d.id, "name": d.lsl_name, "kind": d.kind, "online": d.online,
                     "channels": d.dsp.channels, "fs": d.dsp.fs}
                    for d in self.devices.values()
                ],
            })
            return
        super().do_GET()

    def do_POST(self):
        self._cur = self._pick()
        self.path = self.path.split("?")[0]   # the base class matches the bare path
        super().do_POST()

    def log_message(self, fmt, *args):
        first = str(args[0]) if args else ""
        if "/stream" in first or "/api/" in first or "/vendor/" in first or ".glb" in first:
            return
        super().log_message(fmt, *args)


class _Server(_ThreadedHTTPServer):
    request_queue_size = 128   # many phones connecting at once


def parse_devices(specs: List[str]) -> Dict[str, Device]:
    devices: Dict[str, Device] = {}
    for spec in specs:
        if "=" not in spec:
            raise SystemExit(f"device must be ID=LSL_STREAM_NAME, not '{spec}'")
        dev_id, name = spec.split("=", 1)
        devices[dev_id.strip()] = Device(dev_id.strip(), name.strip())
    return devices


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("devices", nargs="+", metavar="ID=LSL_NAME",
                    help="devices to follow; the first is the default, e.g. epocx1='EPOCX (E1234567)' muse1=Muse")
    ap.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1; 0.0.0.0 = all)")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--fps", type=float, default=DEFAULT_FPS)
    ap.add_argument("--headmap-dir", metavar="DIR",
                    help="directory of the live head map images status-image-<device>.png (written by the feeder)")
    ap.add_argument("--logger-port", type=int, metavar="PORT",
                    help="port, on this host, of a web app the page links back to (\"← Logger\")")
    ap.add_argument("--exit-with", type=int, metavar="PID",
                    help="stop when this process is gone (so the server cannot outlive its launcher)")
    args = ap.parse_args(argv)

    devices = parse_devices(args.devices)
    stop = threading.Event()

    class Handler(MultiDeviceHandler):
        pass

    Handler.devices = devices
    Handler.default_device = next(iter(devices))
    Handler.assets_dir = asset_path()
    Handler.stop_event = stop
    Handler.logger_port = args.logger_port
    Handler.headmap_dir = Path(args.headmap_dir) if args.headmap_dir else None
    Handler.on_pause_request = None
    Handler.on_start_request = None

    server = _Server((args.host, args.port), Handler)
    threads = [threading.Thread(target=server.serve_forever, daemon=True)]
    for dev in devices.values():
        threads.append(threading.Thread(target=lsl_reader, args=(dev, stop), daemon=True))
        threads.append(threading.Thread(target=dsp_loop, args=(dev, args.fps, stop), daemon=True))
    for t in threads:
        t.start()
    log(f"brain map for {', '.join(f'{d.id}={d.lsl_name}' for d in devices.values())} at "
        f"http://{args.host}:{args.port}/")

    def _stop(*_a):
        stop.set()
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    try:
        while not stop.is_set():
            if args.exit_with is not None:
                try:
                    os.kill(args.exit_with, 0)
                except ProcessLookupError:
                    log(f"process {args.exit_with} is gone, stopping")
                    break
                except PermissionError:
                    pass
            stop.wait(1.0)
    finally:
        stop.set()
        for dev in devices.values():
            dev.broadcaster.close_all()
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
