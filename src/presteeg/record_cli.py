"""CLI entry point for ``presteeg-record``."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from . import decrypt, devices
from .record import Recorder


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="presteeg-record",
        description="Stream and record EPOC-X BLE EEG packets as CSV or LSL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--key",
        help="this device's AES-256 key as 64 hex chars (else $PRESTEEG_AES_KEY or KEYS.txt)",
    )
    ap.add_argument(
        "--device",
        help="saved device name/address in KEYS.txt to load key/whiten from",
    )
    ap.add_argument(
        "--offline",
        metavar="CAPTURE.csv",
        help="replay a raw-packet sniff CSV instead of live BLE",
    )
    ap.add_argument("--address", help="BLE address to prefer (live mode)")
    ap.add_argument(
        "--name",
        help="Bluetooth name of the headset to use, e.g. 'EPOCX (E5021270)' (live mode)",
    )
    ap.add_argument(
        "--mode",
        default="ecb",
        choices=["ecb", "cbc2", "cbc6"],
        help="cipher mode (default ecb = the live stream path)",
    )
    ap.add_argument(
        "--little-endian",
        action="store_true",
        help="decode int16 samples little-endian (default big)",
    )
    ap.add_argument("--raw", action="store_true", help="also emit the ciphertext hex column")
    ap.add_argument(
        "--lsl",
        action="store_true",
        help="publish to a Lab Streaming Layer outlet ('EPOCX') instead of writing CSV",
    )
    ap.add_argument(
        "--lsl-name",
        default="EPOCX",
        help="LSL stream name (default EPOCX); give each headset its own when streaming several",
    )
    ap.add_argument("--whiten", help="XOR whitening byte, e.g. 0x14 (else $PRESTEEG_WHITEN or 0x55)")
    ap.add_argument(
        "--csv",
        metavar="FILE.csv",
        help="also save the stream to this CSV file while streaming on stdout",
    )
    ap.add_argument(
        "--no-reconnect",
        action="store_true",
        help="exit on BLE disconnect instead of waiting/retrying",
    )
    args = ap.parse_args(argv)

    has_env_key = bool(os.environ.get("PRESTEEG_AES_KEY") or os.environ.get("EPOCX_AES_KEY"))
    has_env_whiten = bool(os.environ.get("PRESTEEG_WHITEN") or os.environ.get("EPOCX_WHITEN"))
    if not args.key and not has_env_key:
        selector = args.device or args.address or args.name
        profile = devices.resolve_device(selector)
        if profile is not None:
            args.key = profile.aes_key
            if not args.whiten and not has_env_whiten:
                decrypt.WHITEN_XOR = profile.whiten
            if not args.address and profile.effective_address:
                args.address = profile.effective_address
            if not args.name and profile.effective_name:
                args.name = profile.effective_name

    if args.whiten:
        decrypt.WHITEN_XOR = int(args.whiten, 0)

    try:
        key = decrypt.load_key(args.key)
    except decrypt.DecryptError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    rec = Recorder(
        key,
        mode=args.mode,
        include_raw=args.raw,
        big_endian=not args.little_endian,
        lsl=args.lsl,
        lsl_name=args.lsl_name,
        csv_path=args.csv,
    )

    if args.offline:
        rec.run_offline(args.offline)
        return 0

    rec.install_pause_handler()
    try:
        asyncio.run(
            rec.run_live(
                address=args.address,
                reconnect=not args.no_reconnect,
                name=args.name,
            )
        )
    except KeyboardInterrupt:
        print("# stopped", file=sys.stderr)
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            sys.stdout.flush()
        except Exception:
            try:
                sys.stdout = open(os.devnull, "w")
            except Exception:
                pass
    return 0
