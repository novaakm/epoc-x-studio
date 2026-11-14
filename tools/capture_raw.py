#!/usr/bin/env python3
"""Quick raw-BLE capture for validating the decryption end-to-end.

Connects to the EPOC-X, subscribes to the EEG + STATUS characteristics, and
writes every raw notification payload (as hex, with a timestamp) to a CSV for
a few seconds. CortexService must NOT hold the headset (only one BLE central
can connect), so stop it first:  sudo systemctl stop cortex cortexsync

    python3 tools/capture_raw.py [seconds] [out.csv]
"""
import asyncio, csv, sys, time, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from epoc_direct.ble import BleConnection, EpocNotFoundError
from epoc_direct.record import EEG_CHAR_UUID, STATUS_TRIGGER_CHAR_UUID

async def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 6.0
    out = sys.argv[2] if len(sys.argv) > 2 else "/tmp/epocx_raw_capture.csv"
    rows = []
    def on_pkt(uuid, ts_ns, data):
        rows.append((ts_ns, uuid, len(data), bytes(data).hex()))
    conn = BleConnection()
    devs = await conn.discover()
    if not devs:
        raise EpocNotFoundError("no EPOC-X found (is CortexService still holding it?)")
    print(f"# connecting to {devs[0].name} ({devs[0].address})", file=sys.stderr)
    await conn.connect(devs[0])
    conn.on_notify(EEG_CHAR_UUID, on_pkt)
    conn.on_notify(STATUS_TRIGGER_CHAR_UUID, on_pkt)
    await conn.start_notify(EEG_CHAR_UUID)
    await conn.start_notify(STATUS_TRIGGER_CHAR_UUID)
    print(f"# capturing {secs}s ...", file=sys.stderr)
    await asyncio.sleep(secs)
    await conn.disconnect()
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp_ns", "uuid", "size", "hex"])
        w.writerows(rows)
    sizes = {}
    for r in rows: sizes[r[2]] = sizes.get(r[2], 0) + 1
    print(f"# wrote {len(rows)} packets to {out}  sizes={sizes}", file=sys.stderr)

if __name__ == "__main__":
    asyncio.run(main())
