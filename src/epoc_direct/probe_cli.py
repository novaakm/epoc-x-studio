"""CLI entry point for ``epoc-probe-device`` (extract key + whitening byte)."""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import subprocess
import sys

from . import decrypt
from .ble import BleConnection
from .paths import get_keys_path, get_repo_root
from .record import EEG_CHAR_UUID, STATUS_TRIGGER_CHAR_UUID

SERIAL_CHAR = "00002a25-0000-1000-8000-00805f9b34fb"
CORTEX_UNITS = ["cortex", "cortexsync"]


def _dump_swaes_path() -> str:
    root = get_repo_root()
    if root is not None:
        return str(root / "tools" / "dump_swaes.gdb")
    return os.path.join("tools", "dump_swaes.gdb")


def say(msg: str = "") -> None:
    print(msg, flush=True)


def step(title: str) -> None:
    say("\n" + "=" * 72)
    say(title)
    say("=" * 72)


def ask(msg: str) -> str:
    try:
        return input(msg)
    except EOFError:
        return ""


def pause(msg: str) -> None:
    ask(f"\n>>> {msg}\n    Press Enter to continue... ")


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, **kw)


def cortex_pid() -> str | None:
    try:
        out = subprocess.run(
            ["systemctl", "show", "-p", "MainPID", "--value", "cortex"],
            text=True,
            capture_output=True,
        ).stdout.strip()
        return out if out and out != "0" else None
    except Exception:
        return None


def phase_a_key() -> str:
    step("PHASE A  -  extract the AES-256 key (CortexService + gdb)")
    say("You'll need sudo. Priming it now (enter your password if prompted):")
    run(["sudo", "-v"])

    say("\n1. Open EmotivPRO and connect ONLY the new headset (unplug/forget any")
    say("   other EPOC so we capture the right device's key).")
    say("2. Make sure CortexService is running (EmotivPRO starts it). If needed:")
    say("     sudo systemctl start cortex cortexsync")
    pid = cortex_pid()
    if not pid:
        pause("CortexService not running. Start EmotivPRO (or the cortex unit), then continue.")
        pid = cortex_pid()
    if not pid:
        say("!! Still can't find CortexService's PID. Start it and re-run. Aborting.")
        sys.exit(1)
    say(f"   CortexService PID = {pid}")

    say("\n3. In EmotivPRO, make sure the live EEG view is STOPPED/closed for a")
    say("   moment (so the key gets set FRESH when you start it under gdb).")
    pause("Stop the live EEG view now.")

    dump_swaes = _dump_swaes_path()
    for attempt in range(1, 4):
        say(f"\n[attempt {attempt}/3] Attaching gdb to CortexService (PID {pid})...")
        say(">>> As soon as it says 'armed', START the live EEG view in EmotivPRO")
        say("    (open the EEG / contact-quality screen). The key fires on start.")
        proc = subprocess.run(
            ["sudo", "gdb", "-q", "-batch", "-p", pid, "-x", dump_swaes],
            text=True,
            capture_output=True,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        m = re.search(r"RAWKEY=([0-9a-fA-F]{64})", out)
        bits = re.search(r"bits=(\d+)", out)
        if m:
            key_hex = m.group(1).lower()
            say(f"\n  ✔ Captured key (bits={bits.group(1) if bits else '?'}):")
            say(f"    {key_hex}")
            decrypt.load_key(key_hex)
            return key_hex
        say("  ✗ No RAWKEY in this run (the decryptor wasn't re-initialised while")
        say("    gdb was attached).")
        if attempt < 3:
            pause("STOP the live EEG view again; I'll re-attach, then START it once more.")
    say("\n!! Could not capture the key after 3 tries. Tips:")
    say("   - Ensure the stream was STOPPED before each attach and STARTED after.")
    say("   - Or disconnect+reconnect the headset in EmotivPRO while gdb is attached.")
    say(f"   - You can also run it by hand and watch for RAWKEY=:")
    say(f"       sudo gdb -q -batch -p {pid} -x {dump_swaes} 2>&1 | tee key.log")
    sys.exit(1)


async def _capture(address: str | None, secs: float):
    rows = []
    conn = BleConnection()
    devs = await conn.discover(timeout=10.0)
    if not devs:
        raise RuntimeError("No EPOC-X advertising. Power-cycle the headset and retry.")
    if address:
        devs = [d for d in devs if d.address == address] or devs
    if len(devs) > 1 and not address:
        say("\nMultiple EPOC-X devices found:")
        for i, d in enumerate(devs):
            say(f"  [{i}] {d.name}   {d.address}")
        sel = ask("Pick the NEW device by number: ").strip()
        devs = [devs[int(sel)]] if sel.isdigit() and int(sel) < len(devs) else devs[:1]
    dev = devs[0]
    say(f"\nConnecting to {dev.name} ({dev.address}) ...")
    last = None
    for a in range(4):
        try:
            await conn.connect(dev)
            last = None
            break
        except Exception as e:
            last = e
            say(f"  connect attempt {a + 1} failed, retrying...")
            await asyncio.sleep(2)
    if last:
        raise last
    serial = None
    try:
        serial = (await conn.read(SERIAL_CHAR)).decode(errors="replace").strip()
    except Exception:
        pass
    conn.on_notify(EEG_CHAR_UUID, lambda u, t, d: rows.append((str(u), bytes(d))))
    conn.on_notify(STATUS_TRIGGER_CHAR_UUID, lambda u, t, d: rows.append((str(u), bytes(d))))
    await conn.start_notify(EEG_CHAR_UUID)
    await conn.start_notify(STATUS_TRIGGER_CHAR_UUID)
    say(f"Capturing {secs:.0f}s of raw packets ...")
    await asyncio.sleep(secs)
    await conn.disconnect()
    eeg = [d for (u, d) in rows if u.lower().startswith("81072f41")]
    return dev.name, dev.address, serial, eeg


def phase_b_whiten(key_hex: str, address: str | None):
    step("PHASE B  -  find the whitening byte W (CortexService stopped)")
    say("I'll stop CortexService so we can grab the headset over Bluetooth.")
    run(["sudo", "systemctl", "stop", *CORTEX_UNITS])
    say("Close the EmotivPRO window too, so it doesn't re-grab the headset.")
    pause("Close EmotivPRO (or at least stop its streaming), then continue.")

    key = decrypt.load_key(key_hex)
    name = addr = serial = None
    eeg = []
    for attempt in range(1, 4):
        try:
            name, addr, serial, eeg = asyncio.run(_capture(address, secs=5.0))
            if eeg:
                break
        except Exception as e:
            say(f"  capture attempt {attempt}/3 failed: {e}")
            if attempt < 3:
                pause("If it won't connect, power-cycle the headset, then continue.")
    if not eeg:
        say("\n!! No EEG packets captured. Aborting Phase B.")
        sys.exit(1)

    say(f"Captured {len(eeg)} EEG packets. Brute-forcing W over 0..255 ...")
    W = decrypt.autodetect_whiten(eeg[:80], key)
    if W is None:
        say("\n!! No whitening byte makes the frames valid. Most likely the KEY is")
        say("   wrong for THIS device (did you capture the key for a different")
        say("   headset?). Re-run Phase A for the correct device.")
        sys.exit(1)
    say(f"\n  ✔ Whitening byte W = 0x{W:02x}")
    return name, addr, serial, W


def append_keys(name, addr, serial, key_hex, W):
    keys_txt = str(get_keys_path())
    os.makedirs(os.path.dirname(os.path.abspath(keys_txt)), exist_ok=True)
    record = (
        f"\n# --- {name or 'EPOC-X'} (added by probe-new-device) "
        f"{'-' * max(0, 30 - len(name or ''))}\n"
        f"BT name    : {name or '?'}\n"
        f"BT address : {addr or '?'}\n"
        f"serial     : {serial or '? (not read)'}\n"
        f"AES key    : {key_hex}\n"
        f"whitening  : 0x{W:02x}\n"
    )
    existing = ""
    if os.path.exists(keys_txt):
        with open(keys_txt, encoding="utf-8", errors="replace") as f:
            existing = f.read()
    if addr and addr in existing:
        say(
            f"\nNote: {addr} already appears in {keys_txt} -- appending anyway "
            "(remove the old record if stale)."
        )
    with open(keys_txt, "a", encoding="utf-8") as f:
        f.write(record)
    say(f"\nAppended to {keys_txt} (local only).")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="epoc-probe-device",
        description="Interactive helper to extract AES key and whitening byte for a new EPOC-X headset.",
    )
    ap.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        step("EPOC-X new-device probe  (AES key + whitening byte)")
        say("This takes ~5 minutes and needs: EmotivPRO, the headset, and sudo.")
        addr = ask("Known BLE address of the NEW device (blank = auto-detect): ").strip() or None

        key_hex = phase_a_key()
        name, got_addr, serial, W = phase_b_whiten(key_hex, addr)

        step("RESULT")
        say(f"  BT name    : {name}")
        say(f"  BT address : {got_addr}")
        say(f"  serial     : {serial or '(not read)'}")
        say(f"  AES key    : {key_hex}")
        say(f"  whitening  : 0x{W:02x}")
        say("\nRun this device with:")
        say(f"  EPOCX_AES_KEY={key_hex} \\")
        say(f"  EPOCX_WHITEN=0x{W:02x} epoc-direct-record --address {got_addr}")

        if ask("\nAppend this to KEYS.txt? [Y/n] ").strip().lower() in ("", "y", "yes"):
            append_keys(name, got_addr, serial, key_hex, W)
        say("\nDone. (Restart EmotivPRO/CortexService normally when you're finished.)")
        return 0
    except KeyboardInterrupt:
        say("\n(interrupted)")
        return 130
