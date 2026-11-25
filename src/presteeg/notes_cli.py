"""CLI entry point for ``presteeg-take-notes`` (timed free-text notes alongside recordings)."""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import os
import sys
import time

if os.name == "nt":
    import msvcrt
else:
    import codecs
    import select
    import termios
    import tty

COLS = ["start_ns", "start_iso", "end_ns", "end_iso", "note"]


def _iso(ts_ns: int) -> str:
    return _dt.datetime.fromtimestamp(ts_ns / 1e9, _dt.timezone.utc).isoformat()


def notes_path(rec: str) -> str:
    base = rec[:-4] if rec.endswith(".csv") else rec
    return base + ".notes.csv"


def read_notes_tty_win():
    """Windows interactive reader yielding (start_ns, end_ns, text)."""
    buf, start = [], None
    sys.stdout.write("> ")
    sys.stdout.flush()
    while True:
        try:
            ch = msvcrt.getwch()
        except KeyboardInterrupt:
            ch = "\x03"
        if ch in ("\x03", "\x04", "\x1a"):  # Ctrl-C / Ctrl-D / Ctrl-Z
            if buf:
                yield start, time.time_ns(), "".join(buf)
            sys.stdout.write("\n")
            return
        if ch in ("\r", "\n"):
            end = time.time_ns()
            sys.stdout.write("\n")
            if buf:
                yield start, end, "".join(buf)
            buf, start = [], None
            sys.stdout.write("> ")
        elif ch in ("\x08", "\x7f"):  # Backspace
            if buf:
                buf.pop()
                sys.stdout.write("\b \b")
        elif ch == "\x15":  # Ctrl-U: wipe line
            sys.stdout.write("\b \b" * len(buf))
            buf = []
        elif ch in ("\x00", "\xe0"):  # Arrow/function key prefix
            msvcrt.getwch()
        elif ord(ch) < 0x20:
            pass
        else:
            if start is None:
                start = time.time_ns()
            buf.append(ch)
            sys.stdout.write(ch)
        sys.stdout.flush()


def read_notes_tty():
    """Yield (start_ns, end_ns, text); start is taken at the first keypress."""
    if os.name == "nt":
        yield from read_notes_tty_win()
        return
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buf, start = [], None
    try:
        tty.setcbreak(fd)
        sys.stdout.write("> ")
        sys.stdout.flush()
        while True:
            try:
                b = os.read(fd, 1)
            except KeyboardInterrupt:
                b = b"\x04"
            if not b or b == b"\x04":
                if buf:
                    yield start, time.time_ns(), "".join(buf)
                sys.stdout.write("\n")
                return
            if b in (b"\r", b"\n"):
                end = time.time_ns()
                sys.stdout.write("\n")
                if buf:
                    yield start, end, "".join(buf)
                buf, start = [], None
                sys.stdout.write("> ")
            elif b in (b"\x7f", b"\x08"):
                if buf:
                    buf.pop()
                    sys.stdout.write("\b \b")
            elif b == b"\x15":
                sys.stdout.write("\b \b" * len(buf))
                buf = []
            elif b == b"\x1b":
                while select.select([fd], [], [], 0.01)[0]:
                    os.read(fd, 16)
            elif b[0] < 0x20:
                pass
            else:
                if start is None:
                    start = time.time_ns()
                ch = dec.decode(b)
                if ch:
                    buf.append(ch)
                    sys.stdout.write(ch)
            sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def read_notes_pipe():
    """Non-interactive fallback: one note per line, no keypress timing."""
    for line in sys.stdin:
        now = time.time_ns()
        if line.strip():
            yield now, now, line.rstrip("\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="presteeg-take-notes",
        description="Record timestamped event notes into <recording>.notes.csv.",
    )
    ap.add_argument("recording", help="Path to the EEG recording CSV (or prefix)")
    parsed = ap.parse_args(sys.argv[1:] if argv is None else argv)
    path = notes_path(parsed.recording)
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(COLS)
            f.flush()
        print(f"Appending notes to {path} (Enter saves, Ctrl-D quits)", file=sys.stderr)
        notes = read_notes_tty() if sys.stdin.isatty() else read_notes_pipe()
        for start, end, text in notes:
            w.writerow([start, _iso(start), end, _iso(end), text])
            f.flush()
            os.fsync(f.fileno())
    return 0
