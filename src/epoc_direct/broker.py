"""Local CSV stream broker and session controller for EPOC-X Studio.

Owns a single active stream source (either live BLE via ``Recorder`` or
real-time CSV replay) and fans every CSV line out simultaneously to:
  * connected local TCP clients (``epoc-live-viewer --connect``,
    ``epoc-headmap --connect``, ``epoc-brain-map --connect``),
  * an optional on-disk CSV recording (toggled on/off mid-stream without
    interrupting BLE or open viewers),
  * an optional timed-notes CSV (``<recording>.notes.csv``).
"""
from __future__ import annotations

import asyncio
import bisect
import collections
import csv
import datetime as _dt
import io
import os
import queue
import socket
import threading
import time
from typing import Callable, Optional

from . import channels, decrypt
from .devices import DeviceProfile
from .paths import get_recordings_dir
from .record import Recorder

DEFAULT_BROKER_HOST = "127.0.0.1"
DEFAULT_BROKER_PORT = 18765

STANDARD_HEADER = ",".join(
    ["timestamp_ns", "timestamp_iso", "packet_size", "frame_counter"]
    + channels.CHANNELS
    + channels.MOTION_CHANNELS
    + ["status_a", "quality_a", "status_b", "quality_b"]
)


def _iso(ts_ns: int) -> str:
    return _dt.datetime.fromtimestamp(ts_ns / 1e9, _dt.timezone.utc).isoformat()


def format_note_line(start_ns: int, end_ns: int, text: str) -> str:
    """Format a timed note as an inline `#NOTE,...` broker stream line."""
    clean = " ".join(text.splitlines()).strip()
    buf = io.StringIO()
    csv.writer(buf, lineterminator="").writerow(["#NOTE", int(start_ns), int(end_ns), clean])
    return buf.getvalue()


def format_total_line(total_ms: Optional[float]) -> str:
    """Format an inline `#TOTAL,<duration_ms>` broker metadata line."""
    if total_ms is None:
        return "#TOTAL,"
    return f"#TOTAL,{float(total_ms):.3f}"


def format_mode_line(mode: str) -> str:
    """Format an inline `#MODE,<live|replay>` broker metadata line."""
    return f"#MODE,{mode}"


def format_bounds_line(first_ts_s: Optional[float], last_ts_s: Optional[float]) -> str:
    """Format an inline `#BOUNDS,<start_s>,<end_s>` broker metadata line."""
    if first_ts_s is None or last_ts_s is None:
        return "#BOUNDS,"
    return f"#BOUNDS,{float(first_ts_s):.6f},{float(last_ts_s):.6f}"


def _parse_csv_ts_ms(tok: str, is_iso: bool = False) -> Optional[float]:
    tok = tok.strip()
    if not tok:
        return None
    if is_iso or ("T" in tok and ":" in tok) or tok.endswith("Z"):
        try:
            t = tok[:-1] + "+00:00" if tok.endswith("Z") else tok
            return _dt.datetime.fromisoformat(t).timestamp() * 1000.0
        except ValueError:
            return None
    try:
        v = float(tok)
    except ValueError:
        return None
    a = abs(v)
    if a > 1e17:
        return v / 1e6
    if a > 1e14:
        return v / 1e3
    if a > 1e11:
        return v
    return v * 1000.0


def _detect_ts_column(fields: list[str]) -> Optional[tuple[int, bool]]:
    """Return `(col_index, is_iso)` if `fields` looks like a CSV header with timestamps."""
    low = [c.strip().lower() for c in fields]
    if not any(h in low for h in ("timestamp_ns", "timestamp", "time_ns", "timestamp_iso", "iso_time")):
        return None
    for idx, h in enumerate(low):
        if h in ("timestamp_ns", "timestamp", "time_ns"):
            return (idx, False)
        if h in ("timestamp_iso", "iso_time"):
            return (idx, True)
    return None


def _extract_csv_bounds_ms(csv_path: str) -> Optional[tuple[float, float]]:
    """Return `(first_ts_ms, last_ts_ms)` from a CSV file in O(1) I/O."""
    try:
        size = os.path.getsize(csv_path)
        if size <= 0:
            return None
        ts_col = 0
        ts_iso = False
        first_line: Optional[str] = None
        with open(csv_path, "r", encoding="utf-8", errors="replace") as f:
            for _ in range(32):
                raw = f.readline()
                if not raw:
                    break
                ln = raw.strip()
                if not ln or ln.startswith("#"):
                    continue
                fields = ln.split(",")
                detected = _detect_ts_column(fields)
                if detected is not None:
                    ts_col, ts_iso = detected
                    continue
                if first_line is None:
                    first_line = ln
                    break
        if first_line is None:
            return None

        last_line: Optional[str] = None
        with open(csv_path, "rb") as fb:
            if size > 16384:
                fb.seek(size - 16384)
                chunk = fb.read().decode("utf-8", errors="replace")
                tail_lines = chunk.splitlines()[1:]
            else:
                tail_lines = fb.read().decode("utf-8", errors="replace").splitlines()
        for raw in reversed(tail_lines):
            ln = raw.strip()
            if not ln or ln.startswith("#"):
                continue
            if ln.startswith("timestamp_ns,") or ln.startswith("timestamp,") or ln.startswith("timestamp_iso,"):
                continue
            last_line = ln
            break
        if last_line is None:
            return None

        f_fields = first_line.split(",")
        l_fields = last_line.split(",")
        if ts_col >= len(f_fields) or ts_col >= len(l_fields):
            return None
        first_ms = _parse_csv_ts_ms(f_fields[ts_col], is_iso=ts_iso)
        last_ms = _parse_csv_ts_ms(l_fields[ts_col], is_iso=ts_iso)
        if first_ms is None or last_ms is None or last_ms < first_ms:
            return None
        return (first_ms, last_ms)
    except OSError:
        return None


def _extract_csv_duration_ms(csv_path: str) -> Optional[float]:
    """Return `(last_ts - first_ts)` in milliseconds from a CSV file in O(1) I/O."""
    bounds = _extract_csv_bounds_ms(csv_path)
    if bounds is None:
        return None
    return bounds[1] - bounds[0]


def load_notes_csv(notes_file: str) -> list[tuple[int, int, str]]:
    """Read `(start_ns, end_ns, text)` entries from a `.notes.csv` file."""
    notes: list[tuple[int, int, str]] = []
    if not notes_file or not os.path.exists(notes_file):
        return notes
    try:
        with open(notes_file, "r", encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.reader(f)
            for row in reader:
                if not row or len(row) < 5:
                    continue
                if row[0].strip() == "start_ns":
                    continue
                try:
                    s_ns = int(row[0].strip())
                    e_ns = int(row[2].strip()) if row[2].strip() else s_ns
                except ValueError:
                    continue
                text = row[4].strip()
                if text:
                    notes.append((s_ns, e_ns, text))
    except OSError:
        pass
    return notes


class LineQueueStream:
    """Thread-safe iterable/readline stream backed by a bounded queue.

    Used by in-process EPOC-X Studio windows so they consume the exact same
    CSV line format as ``CsvBrokerServer`` without opening loopback sockets.
    """

    def __init__(self, maxsize: int = 8192):
        self._q: queue.Queue[Optional[str]] = queue.Queue(maxsize=maxsize)
        self._closed = False

    def push(self, line: str) -> None:
        if self._closed:
            return
        try:
            self._q.put_nowait(line)
        except queue.Full:
            # Drop the oldest buffered sample so live displays stay current.
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(line)
            except queue.Full:
                pass

    def clear(self) -> None:
        """Drop any unread buffered lines from the queue."""
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if item is None:
                try:
                    self._q.put_nowait(None)
                except queue.Full:
                    pass
                break

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._q.put_nowait(None)
        except queue.Full:
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(None)
            except queue.Full:
                pass

    def __iter__(self):
        return self

    def __next__(self) -> str:
        item = self._q.get()
        if item is None:
            raise StopIteration
        return item

    def readline(self) -> str:
        item = self._q.get()
        if item is None:
            return ""
        return item + "\n"


class _LineSink(io.TextIOBase):
    """File-like object passed to ``Recorder(out=...)`` that emits complete lines."""

    def __init__(self, on_line: Callable[[str], None]):
        super().__init__()
        self._on_line = on_line
        self._buf = ""

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        if not s:
            return 0
        # Fast path for csv.writer.writerow (single complete line ending in \n or \r\n)
        if not self._buf and s.endswith("\n") and "\n" not in s[:-1]:
            line = s.rstrip("\r\n")
            if line:
                self._on_line(line)
            return len(s)
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip("\r")
            if line:
                self._on_line(line)
        return len(s)

    def flush(self) -> None:
        pass


class CsvBrokerServer:
    """Thread-safe local TCP server broadcasting CSV lines to attached viewers."""

    def __init__(self, host: str = DEFAULT_BROKER_HOST, port: int = DEFAULT_BROKER_PORT):
        self.host = host
        self.requested_port = port
        self.port: int = port
        self._srv_sock: Optional[socket.socket] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.Lock()
        self._clients: list[socket.socket] = []
        self._header_line: Optional[str] = STANDARD_HEADER
        self._meta_lines: list[str] = []
        self._notes_lines: list[str] = []

    def start(self) -> int:
        if self._running:
            return self.port
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name != "nt":
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((self.host, self.requested_port))
        except OSError:
            srv.bind((self.host, 0))
        srv.listen(16)
        srv.settimeout(0.5)
        self._srv_sock = srv
        self.port = srv.getsockname()[1]
        self._running = True
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()
        return self.port

    def stop(self) -> None:
        self._running = False
        if self._srv_sock is not None:
            try:
                self._srv_sock.close()
            except OSError:
                pass
            self._srv_sock = None
        with self._lock:
            for c in self._clients:
                try:
                    c.close()
                except OSError:
                    pass
            self._clients.clear()

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def set_header(self, header_line: str) -> None:
        header_line = header_line.strip()
        if not header_line:
            return
        with self._lock:
            self._header_line = header_line

    def set_meta(self, meta_lines: list[str]) -> None:
        with self._lock:
            self._meta_lines = list(meta_lines)

    def clear_meta(self) -> None:
        with self._lock:
            self._meta_lines.clear()

    def set_notes(self, note_lines: list[str]) -> None:
        with self._lock:
            self._notes_lines = list(note_lines)

    def add_note_line(self, note_line: str) -> None:
        with self._lock:
            self._notes_lines.append(note_line)

    def clear_notes(self) -> None:
        with self._lock:
            self._notes_lines.clear()

    def _accept_loop(self) -> None:
        while self._running and self._srv_sock is not None:
            try:
                conn, _addr = self._srv_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                conn.settimeout(0.05)
            except OSError:
                pass
            with self._lock:
                header = self._header_line
                initial_lines = (
                    ([header] if header else [])
                    + list(self._meta_lines)
                    + list(self._notes_lines)
                )
                if initial_lines:
                    try:
                        conn.sendall(("".join(ln + "\n" for ln in initial_lines)).encode("utf-8"))
                    except OSError:
                        try:
                            conn.close()
                        except OSError:
                            pass
                        continue
                self._clients.append(conn)

    def broadcast_line(self, line: str) -> None:
        if not self._clients:
            return
        payload = (line + "\n").encode("utf-8")
        dead: list[socket.socket] = []
        with self._lock:
            if not self._clients:
                return
            for c in self._clients:
                try:
                    c.sendall(payload)
                except OSError:
                    dead.append(c)
            if dead:
                for c in dead:
                    try:
                        c.close()
                    except OSError:
                        pass
                    if c in self._clients:
                        self._clients.remove(c)


class StreamController:
    """Coordinates source (BLE or CSV Replay), TCP broker, CSV recording, and notes."""

    def __init__(
        self,
        on_status: Optional[Callable[[str, str], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
    ):
        self.broker = CsvBrokerServer()
        self.broker.start()

        self._on_status = on_status
        self._on_log = on_log

        self._state_lock = threading.Lock()
        self.mode: str = "idle"  # idle | live | replay
        self.state: str = "disconnected"
        self.paused: bool = False
        self.sample_count: int = 0
        self.recorded_rows: int = 0
        self.recording_path: Optional[str] = None
        self.recording_start_time: Optional[float] = None

        self._rec_fp = None
        self._rec_header_written = False
        self._current_header: str = STANDARD_HEADER
        self._meta_lines: list[str] = []
        self._notes_lines: list[str] = []
        self._sub_lock = threading.Lock()
        self._subscribers: list[LineQueueStream] = []
        self._pause_lock = threading.Lock()
        self._pause_listeners: list[Callable[[bool], None]] = []
        self._state_listeners: list[Callable[[str, bool], None]] = []
        self._replay_listeners: list[Callable[[], None]] = []

        # Rate tracking
        self._rate_times: collections.deque[float] = collections.deque()
        self.sample_hz: float = 0.0

        # Active worker controls
        self._worker_thread: Optional[threading.Thread] = None
        self._ble_loop: Optional[asyncio.AbstractEventLoop] = None
        self._ble_stop_event: Optional[asyncio.Event] = None
        self._recorder: Optional[Recorder] = None
        self._replay_stop = threading.Event()
        self._replay_wake = threading.Event()
        self._replay_emit_lock = threading.Lock()
        self._replay_lock = threading.Lock()
        self._replay_header: str = ""
        self._replay_lines: list[str] = []
        self._replay_ts_ns: list[Optional[int]] = []
        self._replay_ts_s: list[float] = []
        self._replay_first_ts: Optional[float] = None
        self._replay_last_ts: Optional[float] = None
        self._replay_idx: int = 0
        self._replay_seeked: bool = False
        self._replay_speed: float = 1.0
        self._replay_loop: bool = True

    @property
    def connect_addr(self) -> str:
        return f"{self.broker.host}:{self.broker.port}"

    @property
    def client_count(self) -> int:
        with self._sub_lock:
            in_proc = len(self._subscribers)
        return self.broker.client_count + in_proc

    def subscribe(self) -> LineQueueStream:
        """Create and register an in-process CSV stream receiving the header first."""
        stream = LineQueueStream()
        with self._state_lock:
            header = self._current_header
            meta_lines = list(self._meta_lines)
            note_lines = list(self._notes_lines)
        if header:
            stream.push(header)
        for ml in meta_lines:
            stream.push(ml)
        for nl in note_lines:
            stream.push(nl)
        with self._sub_lock:
            self._subscribers.append(stream)
        return stream

    def unsubscribe(self, stream: Optional[LineQueueStream]) -> None:
        if stream is None:
            return
        with self._sub_lock:
            if stream in self._subscribers:
                self._subscribers.remove(stream)
        stream.close()

    def add_pause_listener(self, callback: Callable[[bool], None]) -> None:
        with self._pause_lock:
            if callback not in self._pause_listeners:
                self._pause_listeners.append(callback)

    def remove_pause_listener(self, callback: Optional[Callable[[bool], None]]) -> None:
        if callback is None:
            return
        with self._pause_lock:
            if callback in self._pause_listeners:
                self._pause_listeners.remove(callback)

    def _notify_pause_listeners(self, paused: bool) -> None:
        with self._pause_lock:
            listeners = list(self._pause_listeners)
        for cb in listeners:
            try:
                cb(paused)
            except Exception:
                pass

    def add_state_listener(self, callback: Callable[[str, bool], None]) -> None:
        with self._pause_lock:
            if callback not in self._state_listeners:
                self._state_listeners.append(callback)

    def remove_state_listener(self, callback: Optional[Callable[[str, bool], None]]) -> None:
        if callback is None:
            return
        with self._pause_lock:
            if callback in self._state_listeners:
                self._state_listeners.remove(callback)

    def _notify_state_listeners(self, mode: str, paused: bool) -> None:
        self._notify_pause_listeners(paused)
        with self._pause_lock:
            listeners = list(self._state_listeners)
        for cb in listeners:
            try:
                cb(mode, paused)
            except Exception:
                pass

    def add_replay_listener(self, callback: Callable[[], None]) -> None:
        with self._pause_lock:
            if callback not in self._replay_listeners:
                self._replay_listeners.append(callback)

    def remove_replay_listener(self, callback: Optional[Callable[[], None]]) -> None:
        if callback is None:
            return
        with self._pause_lock:
            if callback in self._replay_listeners:
                self._replay_listeners.remove(callback)

    def _notify_replay_listeners(self) -> None:
        with self._pause_lock:
            listeners = list(self._replay_listeners)
        for cb in listeners:
            try:
                cb()
            except Exception:
                pass

    def _emit_status(self, state: str, msg: str) -> None:
        self.state = state
        if self._on_status:
            try:
                self._on_status(state, msg)
            except Exception:
                pass

    def _log(self, msg: str) -> None:
        if self._on_log:
            try:
                self._on_log(msg)
            except Exception:
                pass

    # --- Recording & Notes ---

    def start_recording(self, path: str) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with self._state_lock:
            if self._rec_fp is not None:
                self._rec_fp.close()
            self._rec_fp = open(path, "w", newline="", encoding="utf-8", buffering=1)
            self._rec_header_written = False
            self.recording_path = path
            self.recorded_rows = 0
            self.recording_start_time = time.time()
            # Write current header immediately so notes/viewers know columns
            if self._current_header:
                self._rec_fp.write(self._current_header + "\n")
                self._rec_fp.flush()
                self._rec_header_written = True
        self._log(f"Recording started -> {path}")
        return path

    def stop_recording(self) -> Optional[str]:
        with self._state_lock:
            fp = self._rec_fp
            path = self.recording_path
            rows = self.recorded_rows
            self._rec_fp = None
            self.recording_path = None
            self.recording_start_time = None
            self._rec_header_written = False
        if fp is not None:
            try:
                fp.flush()
                fp.close()
            except OSError:
                pass
            self._log(f"Recording stopped ({rows} rows) -> {path}")
        return path

    def add_note(self, text: str, start_ns: Optional[int] = None,
                 end_ns: Optional[int] = None, fallback_dir: Optional[str] = None) -> str:
        """Append a timed note matching ``epoc-take-notes`` schema."""
        text = text.strip()
        if not text:
            return ""
        now_ns = time.time_ns()
        s_ns = start_ns or now_ns
        e_ns = end_ns or now_ns

        with self._state_lock:
            rec_path = self.recording_path
        if rec_path:
            base = rec_path[:-4] if rec_path.endswith(".csv") else rec_path
            notes_file = base + ".notes.csv"
        else:
            out_dir = fallback_dir or str(get_recordings_dir())
            os.makedirs(out_dir, exist_ok=True)
            stamp = _dt.datetime.now().strftime("%Y%m%d")
            notes_file = os.path.join(out_dir, f"session-{stamp}.notes.csv")

        is_new = not os.path.exists(notes_file) or os.path.getsize(notes_file) == 0
        with open(notes_file, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if is_new:
                w.writerow(["start_ns", "start_iso", "end_ns", "end_iso", "note"])
            w.writerow([s_ns, _iso(s_ns), e_ns, _iso(e_ns), text])
            f.flush()
        note_line = format_note_line(s_ns, e_ns, text)
        with self._state_lock:
            self._notes_lines.append(note_line)
        self.broker.add_note_line(note_line)
        self.broker.broadcast_line(note_line)
        self._broadcast_subscribers(note_line)
        self._log(f"Note saved to {os.path.basename(notes_file)}: {text}")
        return notes_file

    # --- Line handling ---

    def _handle_csv_line(self, line: str, is_header: Optional[bool] = None) -> None:
        if is_header is None:
            is_header = line.startswith("timestamp_ns,")
        if is_header:
            with self._state_lock:
                changed = line != self._current_header
                self._current_header = line
                self.broker.set_header(line)
                if self._rec_fp is not None and not self._rec_header_written:
                    self._rec_fp.write(line + "\n")
                    self._rec_fp.flush()
                    self._rec_header_written = True
            if changed:
                self.broker.broadcast_line(line)
                self._broadcast_subscribers(line)
            return

        now = time.monotonic()
        with self._state_lock:
            self.sample_count += 1
            self._rate_times.append(now)
            cutoff = now - 2.0
            while self._rate_times and self._rate_times[0] < cutoff:
                self._rate_times.popleft()
            if len(self._rate_times) >= 2:
                span = self._rate_times[-1] - self._rate_times[0]
                self.sample_hz = (len(self._rate_times) - 1) / span if span > 0 else 0.0
            if self._rec_fp is not None:
                if not self._rec_header_written and self._current_header:
                    self._rec_fp.write(self._current_header + "\n")
                    self._rec_header_written = True
                self._rec_fp.write(line + "\n")
                self.recorded_rows += 1
        self.broker.broadcast_line(line)
        self._broadcast_subscribers(line)

    def _broadcast_subscribers(self, line: str, exclude: Optional[LineQueueStream] = None) -> None:
        with self._sub_lock:
            subs = list(self._subscribers)
        for sub in subs:
            if sub is not exclude:
                sub.push(line)

    def _publish_meta_lines(self, meta_lines: list[str]) -> None:
        with self._state_lock:
            self._meta_lines = list(meta_lines)
        if meta_lines:
            self.broker.set_meta(meta_lines)
        else:
            self.broker.clear_meta()
        for ml in meta_lines:
            self.broker.broadcast_line(ml)
            self._broadcast_subscribers(ml)

    # --- Source lifecycle ---

    def stop_source(self) -> None:
        # Stop BLE if active
        if self._ble_loop is not None and self._ble_stop_event is not None:
            try:
                self._ble_loop.call_soon_threadsafe(self._ble_stop_event.set)
            except RuntimeError:
                pass
        # Stop replay if active
        self._replay_stop.set()
        self._replay_wake.set()

        t = self._worker_thread
        if t is not None and t.is_alive():
            t.join(timeout=4.0)
        self._worker_thread = None
        self._ble_loop = None
        self._ble_stop_event = None
        with self._replay_lock:
            self._replay_header = ""
            self._replay_lines = []
            self._replay_ts_ns = []
            self._replay_ts_s = []
            self._replay_first_ts = None
            self._replay_last_ts = None
            self._replay_idx = 0
            self._replay_seeked = False
        with self._state_lock:
            self._recorder = None
            self.mode = "idle"
            self.paused = False
            self.sample_hz = 0.0
            self._rate_times.clear()
            self._meta_lines.clear()
            self._notes_lines.clear()
        self.broker.clear_meta()
        self.broker.clear_notes()
        self._notify_state_listeners("idle", False)
        self._emit_status("disconnected", "Disconnected")

    def start_live(self, profile: DeviceProfile, use_lsl: bool = False,
                   lsl_name: str = "EPOCX") -> None:
        self.stop_source()
        decrypt.WHITEN_XOR = profile.whiten
        key = decrypt.load_key(profile.aes_key)

        with self._state_lock:
            self.mode = "live"
            self.paused = False
            self.sample_count = 0
            self._rate_times.clear()
        self._notify_state_listeners("live", False)
        self._publish_meta_lines([format_mode_line("live")])
        for clear_line in (format_total_line(None), format_bounds_line(None, None)):
            self.broker.broadcast_line(clear_line)
            self._broadcast_subscribers(clear_line)
        self._emit_status("connecting", f"Connecting to {profile.name}...")
        self._log(f"Starting BLE stream for {profile.name} (W=0x{profile.whiten:02x})")

        sink = _LineSink(self._handle_csv_line)
        rec = Recorder(
            key,
            out=sink,
            lsl=bool(use_lsl),
            lsl_name=lsl_name,
            also_csv=True,
        )
        self._recorder = rec

        def _worker():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            stop_ev = asyncio.Event()
            self._ble_loop = loop
            self._ble_stop_event = stop_ev

            def on_status(state: str, msg: str):
                if state == "streaming" and self.paused:
                    self._emit_status("paused", f"Paused ({msg})")
                else:
                    self._emit_status(state, msg)
                self._log(msg)

            async def _run():
                await rec.run_live(
                    address=profile.effective_address,
                    reconnect=True,
                    name=profile.effective_name,
                    stop_event=stop_ev,
                    on_status=on_status,
                )

            try:
                loop.run_until_complete(_run())
            except Exception as e:
                self._emit_status("error", str(e))
                self._log(f"BLE error: {e}")
            finally:
                loop.close()

        self._worker_thread = threading.Thread(target=_worker, daemon=True)
        self._worker_thread.start()

    def start_replay(
        self,
        csv_path: str,
        speed: float = 1.0,
        loop: bool = True,
        start_elapsed_sec: float = 0.0,
    ) -> None:
        self.stop_source()
        self._replay_stop.clear()
        self._replay_speed = max(0.1, float(speed))
        self._replay_loop = bool(loop)
        with self._state_lock:
            self.mode = "replay"
            self.paused = False
            self.sample_count = 0
            self._rate_times.clear()
        self._notify_state_listeners("replay", False)

        bounds_ms = _extract_csv_bounds_ms(csv_path)
        if bounds_ms is not None:
            first_ms, last_ms = bounds_ms
            self._publish_meta_lines([
                format_mode_line("replay"),
                format_total_line(last_ms - first_ms),
                format_bounds_line(first_ms / 1000.0, last_ms / 1000.0),
            ])
        else:
            self._publish_meta_lines([format_mode_line("replay")])
            for clear_line in (format_total_line(None), format_bounds_line(None, None)):
                self.broker.broadcast_line(clear_line)
                self._broadcast_subscribers(clear_line)

        base = csv_path[:-4] if csv_path.endswith(".csv") else csv_path
        notes_file = base + ".notes.csv"
        replay_notes = [
            format_note_line(s_ns, e_ns, txt)
            for s_ns, e_ns, txt in load_notes_csv(notes_file)
        ]
        with self._state_lock:
            self._notes_lines = list(replay_notes)
        self.broker.set_notes(replay_notes)
        for nl in replay_notes:
            self.broker.broadcast_line(nl)
            self._broadcast_subscribers(nl)
        if replay_notes:
            self._log(f"Loaded {len(replay_notes)} timed note(s) from {os.path.basename(notes_file)}")

        def _load_replay_csv() -> bool:
            header = ""
            lines: list[str] = []
            ts_ns_list: list[Optional[int]] = []
            ts_s_list: list[float] = []
            ts_col = 0
            ts_is_iso = False
            fallback_t = time.time()

            with open(csv_path, "r", encoding="utf-8", errors="replace") as f:
                for raw in f:
                    if self._replay_stop.is_set():
                        return False
                    ln = raw.strip()
                    if not ln or ln.startswith("#"):
                        continue
                    header = ln
                    detected = _detect_ts_column(header.split(","))
                    if detected is not None:
                        ts_col, ts_is_iso = detected
                    break
                if not header:
                    self._log("Replay error: empty CSV file")
                    return False

                fast_col0_num = (ts_col == 0 and not ts_is_iso)
                for raw in f:
                    if self._replay_stop.is_set():
                        return False
                    ln = raw.strip()
                    if not ln or ln.startswith("#"):
                        continue
                    if fast_col0_num:
                        cidx = ln.find(",")
                        tok = ln[:cidx] if cidx != -1 else ln
                        try:
                            val_ns = int(tok)
                            if abs(val_ns) > 100_000_000_000_000_000:
                                ts_ns = val_ns
                                ts_s = val_ns * 1e-9
                            else:
                                ts_ms = _parse_csv_ts_ms(tok, is_iso=False)
                                if ts_ms is not None:
                                    ts_s = ts_ms / 1000.0
                                    ts_ns = int(round(ts_ms * 1_000_000.0))
                                else:
                                    fallback_t += 1.0 / 128.0
                                    ts_s = fallback_t
                                    ts_ns = None
                        except ValueError:
                            ts_ms = _parse_csv_ts_ms(tok, is_iso=False)
                            if ts_ms is not None:
                                ts_s = ts_ms / 1000.0
                                ts_ns = int(round(ts_ms * 1_000_000.0))
                            else:
                                fallback_t += 1.0 / 128.0
                                ts_s = fallback_t
                                ts_ns = None
                    else:
                        fields = ln.split(",")
                        tok = fields[ts_col].strip() if ts_col < len(fields) else ""
                        ts_ms = _parse_csv_ts_ms(tok, is_iso=ts_is_iso)
                        ts_ns = None
                        if ts_ms is not None:
                            ts_s = ts_ms / 1000.0
                            ts_ns = int(round(ts_ms * 1_000_000.0))
                        else:
                            fallback_t += 1.0 / 128.0
                            ts_s = fallback_t
                    lines.append(ln)
                    ts_ns_list.append(ts_ns)
                    ts_s_list.append(ts_s)

            if not lines:
                self._log("Replay error: no data rows in CSV file")
                return False

            start_idx = 0
            if start_elapsed_sec > 0.0 and ts_s_list:
                first_s = ts_s_list[0]
                dur_s = max(0.0, ts_s_list[-1] - first_s)
                target_s = first_s + max(0.0, min(dur_s, float(start_elapsed_sec)))
                start_idx = bisect.bisect_left(ts_s_list, target_s)
                if start_idx >= len(lines):
                    start_idx = len(lines) - 1

            with self._replay_lock:
                self._replay_header = header
                self._replay_lines = lines
                self._replay_ts_ns = ts_ns_list
                self._replay_ts_s = ts_s_list
                self._replay_first_ts = ts_s_list[0]
                self._replay_last_ts = ts_s_list[-1]
                self._replay_idx = start_idx
                self._replay_seeked = False

            if bounds_ms is None:
                first_s = ts_s_list[0]
                last_s = max(first_s, ts_s_list[-1])
                self._publish_meta_lines([
                    format_mode_line("replay"),
                    format_total_line((last_s - first_s) * 1000.0),
                    format_bounds_line(first_s, last_s),
                ])
            self._notify_replay_listeners()
            return True

        def _worker():
            self._emit_status("replaying", f"Replaying {os.path.basename(csv_path)}")
            self._log(f"Replay started: {csv_path} ({self._replay_speed:g}x, loop={self._replay_loop})")
            try:
                if not _load_replay_csv():
                    return
            except Exception as e:
                self._emit_status("error", f"Replay error: {e}")
                self._log(f"Replay error: {e}")
                return

            while not self._replay_stop.is_set():
                try:
                    with self._replay_lock:
                        header = self._replay_header
                        lines = self._replay_lines
                        ts_ns_list = self._replay_ts_ns
                        ts_s_list = self._replay_ts_s
                        first_ts = self._replay_first_ts
                        start_pos = self._replay_idx
                    n_samples = len(lines)
                    self._handle_csv_line(header, is_header=True)
                    if start_pos > 0 and first_ts is not None and ts_s_list:
                        win_start = max(0, start_pos - 128)
                        actual_elapsed = round(max(0.0, ts_s_list[start_pos - 1] - first_ts), 2)
                        seek_line = f"#SEEK,{actual_elapsed:.3f}"
                        self.broker.broadcast_line(seek_line)
                        self._broadcast_subscribers(seek_line)
                        for ln in lines[win_start:start_pos]:
                            self.broker.broadcast_line(ln)
                            self._broadcast_subscribers(ln)

                    last_ts_ns: Optional[int] = None
                    last_wall = time.perf_counter()

                    while not self._replay_stop.is_set():
                        if self.paused:
                            self._replay_wake.clear()
                            if self.paused and not self._replay_stop.is_set():
                                self._replay_wake.wait(0.05)
                            last_ts_ns = None
                            last_wall = time.perf_counter()
                            continue

                        line: Optional[str] = None
                        ts_ns: Optional[int] = None
                        with self._replay_emit_lock:
                            with self._replay_lock:
                                if self._replay_seeked:
                                    self._replay_seeked = False
                                    last_ts_ns = None
                                    last_wall = time.perf_counter()
                                i = self._replay_idx
                                if i >= n_samples:
                                    line = None
                                else:
                                    self._replay_idx = i + 1
                                    line = lines[i]
                                    ts_ns = ts_ns_list[i]
                            if line is None:
                                break
                            self._handle_csv_line(line, is_header=False)

                        if ts_ns is not None and last_ts_ns is not None:
                            dt_sim = (ts_ns - last_ts_ns) / 1e9 / self._replay_speed
                            if 0 < dt_sim < 0.5:
                                now = time.perf_counter()
                                delay = dt_sim - (now - last_wall)
                                while delay > 0.001 and not self._replay_stop.is_set():
                                    self._replay_wake.clear()
                                    with self._replay_lock:
                                        seeked = self._replay_seeked
                                    if self.paused or seeked:
                                        last_wall = time.perf_counter()
                                        break
                                    step = min(delay, 0.02)
                                    self._replay_wake.wait(step)
                                    delay -= step
                                last_wall = time.perf_counter()
                        elif ts_ns is None:
                            self._replay_wake.clear()
                            with self._replay_lock:
                                seeked = self._replay_seeked
                            if not self.paused and not seeked and not self._replay_stop.is_set():
                                self._replay_wake.wait(1.0 / (128.0 * self._replay_speed))

                        if ts_ns is not None and ts_ns != last_ts_ns:
                            last_ts_ns = ts_ns
                except Exception as e:
                    self._emit_status("error", f"Replay error: {e}")
                    self._log(f"Replay error: {e}")
                    break

                if not self._replay_loop or self._replay_stop.is_set():
                    break
                with self._replay_lock:
                    self._replay_idx = 0
                    self._replay_seeked = False
                self._log("Replay reached EOF, looping...")

            if not self._replay_stop.is_set():
                with self._state_lock:
                    self.mode = "idle"
                    self.paused = False
                self._notify_state_listeners("idle", False)
                self._emit_status("disconnected", "Replay finished")

        self._worker_thread = threading.Thread(target=_worker, daemon=True)
        self._worker_thread.start()

    def seek_replay(
        self,
        target_elapsed_sec: float,
        window_samples: int = 128,
        exclude_stream: Optional[LineQueueStream] = None,
    ) -> Optional[tuple[float, str, list[str]]]:
        """Seek the active CSV replay to ``target_elapsed_sec`` and return ``(actual_elapsed, header, window_lines)``."""
        with self._replay_emit_lock:
            with self._replay_lock:
                if self.mode != "replay" or not self._replay_lines:
                    return None
                first_ts = self._replay_first_ts
                last_ts = self._replay_last_ts
                if first_ts is None or last_ts is None:
                    return None
                n_samples = len(self._replay_lines)
                duration = max(0.0, last_ts - first_ts)
                clamped = max(0.0, min(duration, float(target_elapsed_sec)))
                target_ts = first_ts + clamped
                target_idx = bisect.bisect_left(self._replay_ts_s, target_ts)
                if target_idx >= n_samples:
                    target_idx = n_samples - 1

                end_idx = min(n_samples, target_idx + 1)
                start_idx = max(0, end_idx - max(1, int(window_samples)))
                window_lines = list(self._replay_lines[start_idx:end_idx])
                header = self._replay_header
                actual_elapsed = round(max(0.0, self._replay_ts_s[end_idx - 1] - first_ts), 2)
                self._replay_idx = min(end_idx, n_samples - 1)
                self._replay_seeked = True
                self._replay_wake.set()

            if exclude_stream is not None:
                exclude_stream.clear()
            seek_line = f"#SEEK,{actual_elapsed:.3f}"
            self.broker.broadcast_line(seek_line)
            self._broadcast_subscribers(seek_line, exclude=exclude_stream)
            for ln in window_lines:
                self.broker.broadcast_line(ln)
                self._broadcast_subscribers(ln, exclude=exclude_stream)
            return (actual_elapsed, header, window_lines)

    def get_replay_snapshot(
        self, window_samples: int = 128
    ) -> Optional[tuple[float, float, str, list[str]]]:
        """Return ``(first_ts_s, last_ts_s, header, window_lines)`` for an active replay."""
        with self._replay_lock:
            if self.mode != "replay" or not self._replay_lines:
                return None
            if self._replay_first_ts is None or self._replay_last_ts is None:
                return None
            end_idx = max(1, min(len(self._replay_lines), self._replay_idx))
            start_idx = max(0, end_idx - max(1, int(window_samples)))
            return (
                self._replay_first_ts,
                self._replay_last_ts,
                self._replay_header,
                list(self._replay_lines[start_idx:end_idx]),
            )

    def get_replay_all(
        self,
    ) -> Optional[tuple[float, float, float, str, list[str]]]:
        """Return ``(first_ts_s, last_ts_s, current_elapsed_sec, header, all_lines)`` for an active replay."""
        with self._replay_lock:
            if self.mode != "replay" or not self._replay_lines:
                return None
            if self._replay_first_ts is None or self._replay_last_ts is None:
                return None
            idx = max(0, min(len(self._replay_lines) - 1, self._replay_idx - 1)) if self._replay_idx > 0 else 0
            elapsed = max(0.0, self._replay_ts_s[idx] - self._replay_first_ts)
            return (
                self._replay_first_ts,
                self._replay_last_ts,
                elapsed,
                self._replay_header,
                self._replay_lines,
            )

    def get_replay_progress(self) -> Optional[tuple[float, float]]:
        """Return ``(elapsed_sec, duration_sec)`` while a CSV replay is loaded."""
        with self._replay_lock:
            if self.mode != "replay" or not self._replay_lines:
                return None
            if self._replay_first_ts is None or self._replay_last_ts is None:
                return None
            duration = max(0.0, self._replay_last_ts - self._replay_first_ts)
            idx = max(0, min(len(self._replay_lines) - 1, self._replay_idx - 1)) if self._replay_idx > 0 else 0
            elapsed = max(0.0, min(duration, self._replay_ts_s[idx] - self._replay_first_ts))
            return (elapsed, duration)

    def set_paused(self, paused: Optional[bool] = None) -> bool:
        with self._state_lock:
            if self.mode == "idle":
                self.paused = False
                target = False
            else:
                target = (not self.paused) if paused is None else bool(paused)
                self.paused = target
            mode = self.mode
            rec = self._recorder
        if rec is not None:
            rec.paused = target
        if mode == "replay":
            self._replay_wake.set()
        if mode == "live":
            self._emit_status("paused" if target else "streaming",
                              "Paused" if target else "Streaming")
        elif mode == "replay":
            self._emit_status("paused" if target else "replaying",
                              "Replay paused" if target else "Replaying")
        self._notify_state_listeners(mode, target)
        return target

    def set_replay_speed(self, speed: float) -> None:
        self._replay_speed = max(0.1, float(speed))

    def shutdown(self) -> None:
        self.stop_source()
        self.stop_recording()
        with self._sub_lock:
            subs = list(self._subscribers)
            self._subscribers.clear()
        for sub in subs:
            sub.close()
        self.broker.stop()
