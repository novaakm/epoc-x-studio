"""Stream and record EPOC-X EEG + motion packets over BLE as CSV or LSL.

Live mode connects to the headset via ``ble``, decodes incoming notifications,
and streams CSV rows to stdout (and/or a file or Lab Streaming Layer outlet).
Offline mode replays a raw-packet CSV capture through the same decoder.

Pause/resume: send SIGUSR1 on POSIX systems to toggle pause. While paused, live
notifications are dropped and BLE stays connected; on a BLE disconnect the
recorder waits and reconnects automatically.
"""
from __future__ import annotations

import asyncio
import csv
import datetime as _dt
import signal
import sys
import time
from typing import Optional

from . import channels, decrypt

# EPOC-X GATT notification characteristics for EEG and status frames.
EEG_CHAR_UUID = "81072f41-9f3d-11e3-a9dc-0002a5d5c51b"
STATUS_TRIGGER_CHAR_UUID = "81072f42-9f3d-11e3-a9dc-0002a5d5c51b"

# --- Lab Streaming Layer (optional, --lsl) ---------------------------------
# One LSL stream carries one sample per 14-channel EEG frame. Its channels are
# every CSV column except the two timestamp columns (the sample's LSL timestamp
# holds the capture time), so ``presteeg-from-lsl`` can rebuild the exact viewer
# CSV. Blank cells (motion/status on frames that don't carry them) are sent as
# NaN. Nominal rate is the per-frame row rate (~128 Hz); it's informational.
LSL_STREAM_NAME = "EPOCX"
LSL_STREAM_TYPE = "EEG"
LSL_EEG_RATE = 128.0
LSL_CHANNELS = (["packet_size", "frame_counter"]
                + channels.CHANNELS + channels.MOTION_CHANNELS
                + ["status_a", "quality_a", "status_b", "quality_b"])
_NAN = float("nan")
_BLANK_TRAILER_CSV = ["", "", "", ""]
_BLANK_MOT_CSV = [""] * len(channels.MOTION_CHANNELS)
_BLANK_TRAILER_LSL = [_NAN, _NAN, _NAN, _NAN]
_BLANK_MOT_LSL = [_NAN] * len(channels.MOTION_CHANNELS)


def _iso(ts_ns: int) -> str:
    return _dt.datetime.fromtimestamp(ts_ns / 1e9, _dt.timezone.utc).isoformat()


class Recorder:
    def __init__(self, key: bytes, mode: str = "ecb", include_raw: bool = False,
                 big_endian: bool = True, out=sys.stdout, lsl: bool = False,
                 lsl_name: str = LSL_STREAM_NAME, csv_path: Optional[str] = None,
                 also_csv: bool = False):
        self.key = key
        self.mode = mode
        self.include_raw = include_raw
        self.big_endian = big_endian
        self.lsl = lsl
        self.also_csv = also_csv
        self.lsl_name = lsl_name
        self.address = None          # set by run_live; used for the LSL source_id
        self.device_name = None      # BT name, set by run_live; in the LSL desc
        self.paused = False
        self._writer = csv.writer(out) if (not lsl or also_csv) else None
        self._header_written = False
        self._out = out
        self._csv_fp = None
        self._file_writer = None
        if csv_path:
            self._csv_fp = open(csv_path, "w", newline="", buffering=1)
            self._file_writer = csv.writer(self._csv_fp)
        self._outlet = None

    # --- CSV ---

    def _write_header(self, n_int16: int):
        cols = ["timestamp_ns", "timestamp_iso", "packet_size", "frame_counter"]
        if self._use_channels:
            cols += channels.CHANNELS + channels.MOTION_CHANNELS
        else:
            cols += [f"i{i}" for i in range(n_int16)]
        cols += ["status_a", "quality_a", "status_b", "quality_b"]
        if self.include_raw:
            cols += ["cipher_hex"]
        if self._writer:
            self._writer.writerow(cols)
        if self._file_writer:
            self._file_writer.writerow(cols)
        self._header_written = True

    def _ensure_outlet(self):
        """Create the LSL outlet (lazily, once the first packet has arrived)."""
        if self._outlet is not None:
            return
        try:
            from pylsl import StreamInfo, StreamOutlet
        except ImportError as e:  # pragma: no cover - env-dependent
            raise RuntimeError(
                "--lsl needs pylsl (pip install pylsl). " + str(e)) from e
        source_id = f"epocx-{self.address or 'direct'}"
        info = StreamInfo(self.lsl_name, LSL_STREAM_TYPE, len(LSL_CHANNELS),
                          LSL_EEG_RATE, "float32", source_id)
        desc = info.desc()
        desc.append_child_value("manufacturer", "Emotiv")
        desc.append_child_value("device_name", self.device_name or "")
        desc.append_child_value("device_address", self.address or "")
        chns = desc.append_child("channels")
        for label in LSL_CHANNELS:
            ch = chns.append_child("channel")
            ch.append_child_value("label", label)
            if label in channels.CHANNELS:
                ch.append_child_value("type", "EEG")
                ch.append_child_value("unit", "ADC")
            elif label in channels.MOTION_CHANNELS:
                ch.append_child_value("type", "MOT")
            else:
                ch.append_child_value("type", "misc")
        self._outlet = StreamOutlet(info)
        print(f"# LSL outlet '{self.lsl_name}' ({LSL_STREAM_TYPE}, "
              f"{len(LSL_CHANNELS)} ch @ {LSL_EEG_RATE:g} Hz nominal) open; "
              f"source_id={source_id}", file=sys.stderr, flush=True)

    def emit(self, ts_ns: int, raw: bytes):
        plain = decrypt.decrypt_payload(raw, self.key, self.mode)
        frames = list(channels.iter_frames_tuples(plain))
        if not self._header_written:
            self._use_channels = bool(frames)
            if self.lsl:
                self._ensure_outlet()
            if not self.lsl or self.also_csv or self._file_writer:
                self._write_header(len(channels.raw_int16(plain, self.big_endian)))
            self._header_written = True
        if self.lsl:
            self._emit_lsl(ts_ns, raw, plain, frames)
        if not self.lsl or self.also_csv or self._file_writer:
            self._emit_csv(ts_ns, raw, plain, frames)

    def _emit_lsl(self, ts_ns: int, raw: bytes, plain: bytes, frames):
        # one LSL sample per EEG frame; mirrors the CSV row layout minus the two
        # timestamp columns (carried by the sample's LSL timestamp instead).
        if not frames:
            return  # LSL only carries decoded channel frames
        status = (
            [float(raw[80]), float(raw[81]), float(raw[82]), float(raw[83])]
            if len(raw) == 84 else _BLANK_TRAILER_LSL
        )
        mot = channels.decode_motion_tuple(plain)
        mot_vals = [float(v) for v in mot[1]] if mot else _BLANK_MOT_LSL
        ts = ts_ns / 1e9  # unix seconds; presteeg-from-lsl recovers ts_ns = ts * 1e9
        pkt_len_f = float(len(raw))
        for i, (counter, chan_vals) in enumerate(frames):
            sample = [pkt_len_f, float(counter)]
            sample.extend(float(v) for v in chan_vals)
            sample.extend(mot_vals if i == 0 else _BLANK_MOT_LSL)
            sample.extend(status)
            self._outlet.push_sample(sample, ts)

    def _emit_csv(self, ts_ns: int, raw: bytes, plain: bytes, frames):
        trailer = (
            [raw[80], raw[81], raw[82], raw[83]]
            if len(raw) == 84 else _BLANK_TRAILER_CSV
        )
        rawhex = raw[: (len(raw) // 16) * 16].hex() if self.include_raw else None
        iso_str = _iso(ts_ns)
        pkt_len = len(raw)

        if self._use_channels:
            # motion arrives once per 84-byte packet (~32 Hz); attach it to that
            # packet's first EEG-frame row, blanks elsewhere (viewer holds last).
            mot = channels.decode_motion_tuple(plain)
            mot_vals = list(mot[1]) if mot else _BLANK_MOT_CSV
            # one CSV row per 14-channel EEG frame (a packet carries 2)
            for i, (counter, chan_vals) in enumerate(frames):
                row = [ts_ns, iso_str, pkt_len, counter, *chan_vals]
                row.extend(mot_vals if i == 0 else _BLANK_MOT_CSV)
                row.extend(trailer)
                if rawhex is not None:
                    row.append(rawhex)
                if self._writer:
                    self._writer.writerow(row)
                if self._file_writer:
                    self._file_writer.writerow(row)
        else:
            row = [ts_ns, iso_str, pkt_len, ""]
            row.extend(channels.raw_int16(plain, self.big_endian))
            row.extend(trailer)
            if rawhex is not None:
                row.append(rawhex)
            if self._writer:
                self._writer.writerow(row)
            if self._file_writer:
                self._file_writer.writerow(row)
        if self._out:
            self._out.flush()
        if self._csv_fp:
            self._csv_fp.flush()

    # --- offline ---

    def run_offline(self, capture_csv: str):
        for r in csv.DictReader(open(capture_csv)):
            raw = bytes.fromhex(r["hex"])
            ts = int(r.get("timestamp_ns") or 0)
            self.emit(ts, raw)

    # --- live ---

    async def run_live(self, address: Optional[str] = None,
                       reconnect: bool = True, name: Optional[str] = None,
                       stop_event: Optional[asyncio.Event] = None,
                       on_status=None):
        from .ble import BleConnection, EpocNotFoundError
        if stop_event is None:
            stop_event = asyncio.Event()

        def _status(state: str, msg: str):
            if on_status is not None:
                try:
                    on_status(state, msg)
                except Exception:
                    pass

        def on_packet(_uuid: str, ts_ns: int, data: bytearray):
            if self.paused or stop_event.is_set():
                return
            try:
                self.emit(ts_ns, bytes(data))
            except (BrokenPipeError, OSError) as e:
                if isinstance(e, BrokenPipeError) or getattr(e, "errno", None) in (22, 32):
                    stop_event.set()
                    return
                print(f"# decode error: {e}", file=sys.stderr, flush=True)
            except Exception as e:  # never kill the stream on one bad packet
                print(f"# decode error: {e}", file=sys.stderr, flush=True)

        # Back off while the headset is missing (5 s doubling to 60 s): every
        # attempt scans/connects, which competes for the adapter with other
        # headsets that are streaming. Reset once a connection has streamed.
        delay = 5
        while not stop_event.is_set():
            conn = BleConnection()
            try:
                _status("scanning", "Searching for EPOC-X headset...")
                devices = await conn.discover()
                if address or name:
                    def wanted(d):
                        d_addr = (d.address or "").replace("-", ":").upper()
                        req_addr = (address or "").replace("-", ":").upper()
                        addr_ok = not address or d_addr == req_addr

                        d_name = (d.name or "").lower()
                        req_name = (name or "").lower()
                        name_ok = (
                            not name
                            or req_name == d_name
                            or req_name in d_name
                            or d_name in req_name
                        )
                        return addr_ok and name_ok
                    match = [d for d in devices if wanted(d)]
                    if not match:
                        # discover() returns bonded devices first and skips
                        # scanning; a requested, non-bonded device won't be in
                        # it, so scan explicitly for it. Never silently fall
                        # back to a different (e.g. bonded) headset.
                        scanned = await BleConnection._scan(10.0)
                        match = [d for d in scanned if wanted(d)]
                    if not match:
                        what = " ".join(x for x in (name, address) if x)
                        raise EpocNotFoundError(
                            f"EPOC-X {what} not found (powered on / advertising?)")
                    devices = match
                if not devices:
                    raise EpocNotFoundError("no EPOC-X device found")
                await conn.connect(devices[0])
                self.address = devices[0].address  # for the LSL source_id
                self.device_name = devices[0].name
                conn.on_notify(EEG_CHAR_UUID, on_packet)
                # STATUS_TRIGGER must be subscribed for 84-byte status packets
                await conn.start_notify(EEG_CHAR_UUID)
                await conn.start_notify(STATUS_TRIGGER_CHAR_UUID)
                msg = f"connected to {devices[0].name} ({devices[0].address})"
                _status("streaming", msg)
                print(f"# {msg}; streaming. SIGUSR1 toggles pause.", file=sys.stderr, flush=True)
                while conn.is_connected and not stop_event.is_set():
                    await asyncio.sleep(0.25)
                delay = 5
            except Exception as e:
                _status("error", str(e))
                print(f"# connection error: {e}", file=sys.stderr, flush=True)
            finally:
                await conn.disconnect()
            if not reconnect or stop_event.is_set():
                _status("disconnected", "Disconnected")
                return
            _status("reconnecting", f"Disconnected; retrying in {delay}s")
            print(f"# disconnected; retrying in {delay}s (device charging?)",
                  file=sys.stderr, flush=True)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
                _status("disconnected", "Disconnected")
                return
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, 60)

    def install_pause_handler(self):
        def toggle(_sig, _frm):
            self.paused = not self.paused
            print(f"# {'PAUSED' if self.paused else 'RESUMED'}",
                  file=sys.stderr, flush=True)
        if hasattr(signal, "SIGUSR1"):
            signal.signal(signal.SIGUSR1, toggle)
