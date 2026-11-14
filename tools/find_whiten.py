#!/usr/bin/env python3
"""Recover a device's XOR whitening byte W from a raw capture + its AES key.

The EPOC-X whitens each ciphertext byte with a per-device byte W before AES
(0x55 on old firmware; a provisioned byte on newer firmware). Given the device
key and a short raw-packet capture (e.g. from tools/capture_raw.py), this
brute-forces W -- the only value that yields valid frames (byte1==0x10 with an
incrementing counter).

    # 1) capture a few seconds of raw packets (CortexService must be stopped):
    python3 tools/capture_raw.py 5 capture.csv
    # 2) recover W for your device, using your device's key:
    python3 tools/find_whiten.py capture.csv <AES_KEY_HEX>
    # 3) use it:
    EPOCX_WHITEN=0x<W> epoc-direct-record --key <AES_KEY_HEX>
"""
import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from epoc_direct import decrypt


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    capture_csv, key_hex = sys.argv[1], sys.argv[2]
    key = decrypt.load_key(key_hex)
    pkts = []
    with open(capture_csv) as f:
        for r in csv.DictReader(f):
            # accept capture_raw.py CSVs (uuid,size,hex) -- keep EEG char only
            uuid = (r.get("uuid") or "").lower()
            if uuid and not uuid.startswith("81072f41"):
                continue
            pkts.append(bytes.fromhex(r["hex"]))
    W = decrypt.autodetect_whiten(pkts, key)
    if W is None:
        print("No whitening byte makes the frames valid. Wrong key, or the CSV "
              "has no EEG packets.", file=sys.stderr)
        return 1
    print(f"W = 0x{W:02x}   ->   run with:  EPOCX_WHITEN=0x{W:02x}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
