"""CLI entry points for ``epoc-from-lsl`` and ``epoc-save-lsl``."""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import os
import re
import sys
import time

from .record import LSL_CHANNELS, LSL_STREAM_NAME, LSL_STREAM_TYPE


def _iso(ts_ns: int) -> str:
    return _dt.datetime.fromtimestamp(ts_ns / 1e9, _dt.timezone.utc).isoformat()


def _labels_from_info(info) -> list[str]:
    try:
        labels = []
        ch = info.desc().child("channels").child("channel")
        while not ch.empty():
            labels.append(ch.child_value("label"))
            ch = ch.next_sibling()
        if len(labels) == info.channel_count():
            return labels
    except Exception:
        pass
    return list(LSL_CHANNELS)


def _fmt(v: float) -> str:
    if v != v:
        return ""
    iv = int(v)
    return str(iv) if iv == v else repr(v)


def resolve_lsl(name: str, stype: str, timeout: float):
    from pylsl import resolve_byprop, resolve_streams

    streams = resolve_byprop("name", name, timeout=timeout)
    if not streams:
        streams = [s for s in resolve_streams(timeout) if s.type() == stype]
    return streams


def from_lsl_main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="epoc-from-lsl",
        description="Consume an EPOC-X Lab Streaming Layer stream and emit live-viewer CSV.",
    )
    ap.add_argument(
        "--name",
        default=LSL_STREAM_NAME,
        help=f"LSL stream name to resolve (default {LSL_STREAM_NAME})",
    )
    ap.add_argument(
        "--type",
        default=LSL_STREAM_TYPE,
        dest="stype",
        help=f"stream type if name is absent (default {LSL_STREAM_TYPE})",
    )
    ap.add_argument(
        "--resolve-timeout",
        type=float,
        default=5.0,
        help="seconds to wait for the stream to appear (default 5)",
    )
    args = ap.parse_args(argv)

    try:
        from pylsl import StreamInlet
    except ImportError as e:
        print(f"error: epoc-from-lsl needs pylsl (pip install pylsl): {e}", file=sys.stderr)
        return 2

    print(
        f"# resolving LSL stream name='{args.name}' type='{args.stype}' ...",
        file=sys.stderr,
        flush=True,
    )
    streams = resolve_lsl(args.name, args.stype, args.resolve_timeout)
    if not streams:
        print(
            "# no matching LSL stream found (is `epoc-direct-record --lsl` running?)",
            file=sys.stderr,
        )
        return 1
    if len(streams) > 1:
        print(
            f"# {len(streams)} streams matched; using the first ({streams[0].source_id()})",
            file=sys.stderr,
        )

    inlet = StreamInlet(streams[0], recover=True)
    info = inlet.info()
    labels = _labels_from_info(info)
    print(
        f"# connected to '{info.name()}' ({info.source_id()}), {info.channel_count()} ch",
        file=sys.stderr,
        flush=True,
    )

    writer = csv.writer(sys.stdout)
    writer.writerow(["timestamp_ns", "timestamp_iso"] + labels)
    sys.stdout.flush()

    try:
        while True:
            sample, ts = inlet.pull_sample(timeout=1.0)
            if sample is None:
                continue
            ts_ns = int(ts * 1e9) if ts and ts > 1e9 else time.time_ns()
            writer.writerow([ts_ns, _iso(ts_ns)] + [_fmt(v) for v in sample])
            sys.stdout.flush()
    except KeyboardInterrupt:
        print("# stopped", file=sys.stderr)
        return 130
    except BrokenPipeError:
        return 0


def device_tag(name: str) -> str:
    m = re.search(r"\(([^)]+)\)", name)
    tag = m.group(1) if m else name
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", tag).strip("_") or "device"


class LslSaver:
    def __init__(self, name: str, outdir: str, stamp: str):
        self.name = name
        self.path = os.path.join(outdir, f"{stamp}-{device_tag(name)}.log.csv")
        self.inlet = None
        self.file = None
        self.writer = None
        self.rows = 0
        self.codes = None
        self.code_col = None
        self.waiting = False

    def try_connect(self, timeout: float) -> bool:
        from pylsl import StreamInlet, resolve_byprop

        streams = resolve_byprop("name", self.name, timeout=timeout)
        if not streams:
            return False
        self.inlet = StreamInlet(streams[0], recover=True)
        info = self.inlet.info()
        labels = _labels_from_info(info)
        codes = info.desc().child_value("stream_codes")
        if codes and "stream" in labels:
            self.codes = codes.split(",")
            self.code_col = labels.index("stream")
        self.file = open(self.path, "w", newline="")
        self.writer = csv.writer(self.file)
        self.writer.writerow(["timestamp_ns", "timestamp_iso"] + labels)
        self.file.flush()
        print(
            f"# '{self.name}' ({info.source_id()}, {info.channel_count()} ch) -> {self.path}",
            file=sys.stderr,
            flush=True,
        )
        return True

    def pump(self) -> int:
        samples, stamps = self.inlet.pull_chunk(timeout=0.0)
        for sample, ts in zip(samples, stamps):
            ts_ns = int(ts * 1e9) if ts and ts > 1e9 else time.time_ns()
            row = [_fmt(v) for v in sample]
            if self.codes:
                c = sample[self.code_col]
                if c == c and 0 <= int(c) < len(self.codes):
                    row[self.code_col] = self.codes[int(c)]
            self.writer.writerow([ts_ns, _iso(ts_ns)] + row)
        self.rows += len(samples)
        return len(samples)

    def close(self):
        if self.file:
            self.file.close()
            print(f"# '{self.name}': {self.rows} rows in {self.path}", file=sys.stderr, flush=True)


def save_lsl_main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="epoc-save-lsl",
        description="Save one or more EPOC-X LSL streams to per-device CSV files.",
    )
    ap.add_argument("names", nargs="+", help="LSL stream names to save")
    ap.add_argument("--outdir", default=".", help="output directory")
    ap.add_argument(
        "--retry",
        type=float,
        default=1.0,
        help="seconds between re-resolving missing streams",
    )
    args = ap.parse_args(argv)

    try:
        import pylsl  # noqa: F401
    except ImportError as e:
        print(f"error: epoc-save-lsl needs pylsl (pip install pylsl): {e}", file=sys.stderr)
        return 2

    os.makedirs(args.outdir, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    savers = [LslSaver(n, args.outdir, stamp) for n in args.names]
    next_resolve = 0.0
    last_flush = time.monotonic()
    try:
        while True:
            now = time.monotonic()
            if now >= next_resolve:
                for s in savers:
                    if s.inlet is None and not s.try_connect(timeout=1.0) and not s.waiting:
                        s.waiting = True
                        print(
                            f"# waiting for LSL stream '{s.name}' ...",
                            file=sys.stderr,
                            flush=True,
                        )
                next_resolve = time.monotonic() + args.retry
            got = sum(s.pump() for s in savers if s.inlet is not None)
            if time.monotonic() - last_flush > 1.0:
                for s in savers:
                    if s.file:
                        s.file.flush()
                last_flush = time.monotonic()
            if not got:
                time.sleep(0.02)
    except KeyboardInterrupt:
        pass
    finally:
        for s in savers:
            s.close()
    return 0
