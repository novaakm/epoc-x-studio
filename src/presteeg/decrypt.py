"""EPOC-X BLE packet decryption using user-supplied per-device credentials.

Each raw BLE notification payload on the EEG GATT characteristic carries
16-byte AES-256 blocks XOR-whitened with a per-device byte ``W``:

1. Whiten every ciphertext byte with ``XOR W`` (default ``0x55`` or configured
   per headset via ``KEYS.txt`` / ``$PRESTEEG_WHITEN``).
2. Decrypt each 16-byte block independently with AES-256 in ECB mode.

No keys or whitening bytes are shipped with this package; supply your own
device's credentials via ``KEYS.txt``, ``--key`` / ``--whiten``, or the
``PRESTEEG_AES_KEY`` / ``PRESTEEG_WHITEN`` environment variables.

Packet sizes: 64-byte packets contain 4 ciphertext blocks; 84-byte packets
contain 5 ciphertext blocks (80 bytes) plus a 4-byte cleartext status trailer
(offsets 80..83: status_a, quality_a, status_b, quality_b).
"""
from __future__ import annotations

import os
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_ENV_WHITEN = os.environ.get("PRESTEEG_WHITEN") or os.environ.get("EPOCX_WHITEN")
WHITEN_XOR = int(_ENV_WHITEN, 0) if _ENV_WHITEN else 0x55

KEY_HEX: str | None = None

IV_MODE2 = (0x0B7911DB14D880E2).to_bytes(8, "little") + (0x12C1318D633DBB43).to_bytes(8, "little")
IV_MODE6 = (0x153831E2BEBAFECA).to_bytes(8, "little") + (0x2312D6E9C25398E3).to_bytes(8, "little")


class DecryptError(RuntimeError):
    pass


def load_key(key_hex: str | None = None) -> bytes:
    """Resolve the 16- or 32-byte AES key from argument, environment, or module default."""
    h = (
        key_hex
        or os.environ.get("PRESTEEG_AES_KEY")
        or os.environ.get("EPOCX_AES_KEY")
        or KEY_HEX
    )
    if not h:
        raise DecryptError(
            "No AES key configured. Add a device profile in prestEEG (KEYS.txt) "
            "or pass --key / set PRESTEEG_AES_KEY."
        )
    h = h.replace(" ", "").replace(":", "")
    key = bytes.fromhex(h)
    if len(key) not in (16, 32):
        raise DecryptError(f"AES key must be 16 or 32 bytes, got {len(key)}")
    return key


# Precomputed 256-byte translation tables for C-speed XOR whitening.
_IDENTITY_BYTES = bytes(range(256))
_WHITEN_TABLES: list[bytes] = [
    bytes.maketrans(_IDENTITY_BYTES, bytes(b ^ w for b in range(256)))
    for w in range(256)
]
_CIPHER_CACHE: dict[tuple[bytes, str], Cipher] = {}


def _get_cipher(key: bytes, mode: str) -> Cipher:
    cache_key = (key, mode)
    cipher = _CIPHER_CACHE.get(cache_key)
    if cipher is None:
        if mode == "ecb":
            cipher = Cipher(algorithms.AES(key), modes.ECB())
        elif mode == "cbc2":
            cipher = Cipher(algorithms.AES(key), modes.CBC(IV_MODE2))
        elif mode == "cbc6":
            cipher = Cipher(algorithms.AES(key), modes.CBC(IV_MODE6))
        else:
            raise DecryptError(f"unknown mode {mode!r}")
        _CIPHER_CACHE[cache_key] = cipher
    return cipher


def decrypt_payload(raw: bytes, key: bytes, mode: str = "ecb",
                    whiten: bool = True) -> bytes:
    """Decrypt one raw BLE notification payload to plaintext bytes.

    Returns the decrypted ciphertext region only: for a 64-byte packet all 64
    bytes; for an 84-byte packet the first 80 (the 4-byte cleartext trailer is
    left to the caller).
    """
    n_cipher = (len(raw) // 16) * 16  # 64 -> 64, 84 -> 80
    if whiten:
        data = bytes(raw[:n_cipher]).translate(_WHITEN_TABLES[WHITEN_XOR & 0xFF])
    else:
        data = bytes(raw[:n_cipher])
    d = _get_cipher(key, mode).decryptor()
    return d.update(data) + d.finalize()


def autodetect_whiten(raw_packets, key: bytes) -> int | None:
    """Find the per-device whitening byte W from sample raw EEG packets + key."""
    firsts = [bytes(p)[:16] for p in raw_packets if len(p) >= 16]
    if not firsts:
        return None
    n_pkts = len(firsts)
    joined_ct = b"".join(firsts)
    cipher = _get_cipher(key, "ecb")
    best, best_score = None, 0
    for W in range(256):
        whitened = joined_ct.translate(_WHITEN_TABLES[W])
        d = cipher.decryptor()
        pt = d.update(whitened) + d.finalize()
        b1ok = 0
        for idx in range(n_pkts):
            if pt[idx * 16 + 1] == 0x10:
                b1ok += 1
        if b1ok != n_pkts:
            continue
        incr = 0
        prev_b0 = pt[0]
        for idx in range(1, n_pkts):
            cur_b0 = pt[idx * 16]
            if (cur_b0 - prev_b0) & 0xFF == 1:
                incr += 1
            prev_b0 = cur_b0
        score = b1ok * 2 + incr
        if score > best_score:
            best, best_score = W, score
    return best


def cleartext_trailer(raw: bytes) -> dict | None:
    """The 4-byte cleartext status trailer of an 84-byte packet, if present."""
    if len(raw) != 84:
        return None
    return {
        "status_a": raw[80], "quality_a": raw[81],
        "status_b": raw[82], "quality_b": raw[83],
    }
