"""EPOC-X BLE stream decryption.

Reverse-engineered from the native Linux ``CortexService``
(``Cortex::DataProcessing::EpocXDataDecryption``). Verified against live
ciphertext/plaintext pairs captured from the running service (see
``tools/dump_swaes.gdb``). Per raw notification payload on the EEG
characteristic ``81072f41-...``:

1. Whiten every byte with ``XOR W`` (a per-device byte, see ``WHITEN_XOR`` and
   ``autodetect_whiten``). CortexService fn ``0x16bd880`` does this XOR.
2. AES-**256**-decrypt per 16-byte block, **ECB** (the live-stream path): each
   16-byte block is independent, ``plaintext = AES256_decrypt(key, ct ^ W)``.

The decryption is NOT done with the CPU's AES-NI (that is BoringSSL, used only
for TLS); the EEG path uses a bundled software **T-table** AES (inverse S-box at
CortexService .rodata 0x354edc0, Td tables at 0x354eec0/f2c0/f6c0/fac0). Core:

* ``AES_decrypt(in=rdi, out=rsi, AES_KEY*=rdx)``        @ 0x2734820 (per block)
* ``AES_set_encrypt_key(userKey=rdi, bits=rsi, key=rdx)`` @ 0x27336b0
* ``AES_set_decrypt_key``                                @ 0x2734230

The 32-byte key is **per device** (and the whitening byte ``W`` is too): both
are provisioned per headset, stored inline in the decryptor object (key at
``obj+0x158``, ``W`` at ``obj+0x178``), constant per device with no per-session
nonce, and NOT derived from the serial. So this package ships **no** key -- you
must supply your own device's key via ``--key`` / ``$EPOCX_AES_KEY`` and its
whitening byte via ``$EPOCX_WHITEN`` (or let ``autodetect_whiten`` find W). See
``README.md`` / ``docs/FINDINGS.md`` ("Obtaining your device's key") for how to
extract them with ``tools/dump_swaes.gdb``.

Packet sizes: 64-byte packets are 4 full ciphertext blocks; 84-byte packets are
5 ciphertext blocks (80 bytes) + a 4-byte cleartext status trailer
(offsets 80..83: status_a, quality_a, status_b, quality_b).
"""
from __future__ import annotations

import os
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# XOR whitening byte applied to every ciphertext byte before AES. CortexService
# (fn 0x16bd880) uses 0x55 for old firmware but a PER-DEVICE byte stored at
# decryptor+0x178 for newer firmware (verifiable by brute force: the correct W
# is the only value that makes frames valid -- byte1==0x10 with an incrementing
# byte0 counter; see autodetect_whiten). 0x55 is the default; set $EPOCX_WHITEN
# to your device's byte, or let the recorder auto-detect it.
WHITEN_XOR = int(os.environ["EPOCX_WHITEN"], 0) if os.environ.get("EPOCX_WHITEN") else 0x55

# This package ships NO key: the AES-256 key is per device (see module docstring)
# and must be supplied via --key / $EPOCX_AES_KEY. Extract yours with
# tools/dump_swaes.gdb (see README.md "Obtaining your device's key").
KEY_HEX: str | None = None

# Legacy/experimental: two hardcoded IVs seen on a CBC "high-pass" code path.
# The live stream is ECB; these are kept only for experimentation and are NOT
# verified against the AES-256 cipher above.
IV_MODE2 = (0x0B7911DB14D880E2).to_bytes(8, "little") + (0x12C1318D633DBB43).to_bytes(8, "little")
IV_MODE6 = (0x153831E2BEBAFECA).to_bytes(8, "little") + (0x2312D6E9C25398E3).to_bytes(8, "little")


class DecryptError(RuntimeError):
    pass


def load_key(key_hex: str | None = None) -> bytes:
    """Resolve the 32-byte AES-256 key from arg, env, or the module default."""
    h = key_hex or os.environ.get("EPOCX_AES_KEY") or KEY_HEX
    if not h:
        raise DecryptError(
            "No AES key. Capture it from the live CortexService with "
            "tools/dump_swaes.gdb and pass --key / set EPOCX_AES_KEY."
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
    left to the caller). ``mode`` is "ecb" (default/live, verified) or
    "cbc2"/"cbc6" (unverified hardcoded-IV CBC variants) for experimentation.

    ``whiten`` XORs the ciphertext with ``WHITEN_XOR`` before AES. The verified
    live path applies this whitening (CortexService fn 0x16bd880), so the default
    is True. Set False only to inspect the raw AES-of-ciphertext.
    """
    n_cipher = (len(raw) // 16) * 16  # 64 -> 64, 84 -> 80
    if whiten:
        data = bytes(raw[:n_cipher]).translate(_WHITEN_TABLES[WHITEN_XOR & 0xFF])
    else:
        data = bytes(raw[:n_cipher])
    d = _get_cipher(key, mode).decryptor()
    return d.update(data) + d.finalize()


def autodetect_whiten(raw_packets, key: bytes) -> int | None:
    """Find the per-device whitening byte W from a few raw EEG packets + key.

    Tries all 256 values and returns the one whose decrypted frames are valid
    (byte1 == 0x10 and byte0 an incrementing counter), or ``None`` if none fits
    (wrong key, or not EEG packets). Lets the tool work given only the key.
    """
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
