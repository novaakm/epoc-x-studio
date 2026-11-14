"""Device configuration store backed by KEYS.txt.

Reads and writes the human-readable stanza format produced by
``epoc-probe-device`` so keys and whitening bytes live in one git-ignored
local file rather than being hardcoded in launcher scripts.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

from .paths import get_keys_path

DEFAULT_KEYS_PATH = str(get_keys_path())


@dataclass
class DeviceProfile:
    name: str
    address: str
    serial: str
    aes_key: str
    whiten: int

    @property
    def display_label(self) -> str:
        addr = self.address if self.address and self.address not in ("?", "(auto)") else "auto-discover"
        return f"{self.name} [{addr}] (W=0x{self.whiten:02x})"

    @property
    def effective_address(self) -> Optional[str]:
        if not self.address or self.address in ("?", "(auto)", "auto"):
            return None
        return self.address

    @property
    def effective_name(self) -> Optional[str]:
        if not self.name or self.name in ("?", "EPOC-X"):
            return None
        return self.name


def _clean_field(val: str) -> str:
    return val.strip()


def load_devices(path: str = DEFAULT_KEYS_PATH) -> list[DeviceProfile]:
    """Parse all device profiles from ``KEYS.txt``."""
    if not os.path.exists(path):
        return []

    devices: list[DeviceProfile] = []
    cur: dict[str, str] = {}

    def _flush():
        key = cur.get("aes key", "").replace(" ", "").replace(":", "").lower()
        if not key:
            return
        whiten_raw = cur.get("whitening", "0x55")
        try:
            whiten = int(whiten_raw, 0)
        except ValueError:
            whiten = 0x55
        devices.append(
            DeviceProfile(
                name=cur.get("bt name") or "EPOC-X",
                address=cur.get("bt address") or "(auto)",
                serial=cur.get("serial") or "?",
                aes_key=key,
                whiten=whiten,
            )
        )

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("#"):
                if cur.get("aes key"):
                    _flush()
                    cur = {}
                continue
            if ":" in line:
                k, v = line.split(":", 1)
                k_norm = k.strip().lower()
                if k_norm == "bt name" and cur.get("aes key"):
                    _flush()
                    cur = {}
                cur[k_norm] = _clean_field(v)
    if cur.get("aes key"):
        _flush()
    return devices


def save_devices(devices: list[DeviceProfile], path: str = DEFAULT_KEYS_PATH) -> None:
    """Write device profiles back to ``KEYS.txt`` cleanly."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    lines = [
        "# Local EPOC-X device keys (git-ignored — never commit to version control).",
        "# Managed automatically by epoc_direct/devices.py, epoc-studio, and epoc-probe-device.",
        "",
    ]
    for d in devices:
        label = d.name or "EPOC-X"
        lines.extend([
            f"# --- {label} {'-' * max(4, 42 - len(label))}",
            f"BT name    : {d.name or 'EPOC-X'}",
            f"BT address : {d.address or '(auto)'}",
            f"serial     : {d.serial or '?'}",
            f"AES key    : {d.aes_key.lower()}",
            f"whitening  : 0x{d.whiten:02x}",
            "",
        ])
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def upsert_device(profile: DeviceProfile, path: str = DEFAULT_KEYS_PATH) -> list[DeviceProfile]:
    """Add or update a device profile (matched by address if explicit, else name)."""
    devices = load_devices(path)
    norm_addr = profile.address.strip().upper()
    has_explicit_addr = norm_addr not in ("", "?", "(AUTO)", "AUTO")
    replaced = False
    for i, existing in enumerate(devices):
        if has_explicit_addr and existing.address.strip().upper() == norm_addr:
            devices[i] = profile
            replaced = True
            break
        if not has_explicit_addr and existing.name == profile.name and existing.address == profile.address:
            devices[i] = profile
            replaced = True
            break
    if not replaced:
        devices.append(profile)
    save_devices(devices, path)
    return devices


def resolve_device(
    selector: Optional[str] = None,
    path: str = DEFAULT_KEYS_PATH,
) -> Optional[DeviceProfile]:
    """Find a device by name/address substring, or return the first saved device."""
    devices = load_devices(path)
    if not devices:
        return None
    if not selector:
        return devices[0]
    q = selector.strip().lower()
    for d in devices:
        if q == d.address.lower() or q == d.name.lower():
            return d
    for d in devices:
        if q in d.address.lower() or q in d.name.lower() or q in d.serial.lower():
            return d
    return None
