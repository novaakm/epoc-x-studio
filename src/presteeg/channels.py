"""Map decrypted EPOC-X payload bytes to the 14 EEG channel values.

LOCKED 2026-10-09 from a real decrypted capture (key + whitening solved; see
``decrypt.py``). Layout, verified by per-column variance and a continuous frame
counter over 900 frames:

A decrypted packet is a sequence of 32-byte **frames**; each frame is two
16-byte AES blocks:

    offset  0      : frame counter (increments +1 per frame, wraps at 256)
    offset  1      : 0x10   (block-0 marker)
    offset  2..15  : channels 0..6   (7 x little-endian uint16)   <- left block
    offset 16      : 0x00   (block-1 marker, ~always 0)
    offset 17      : 0x00
    offset 18..31  : channels 7..13  (7 x little-endian uint16)   <- right block

Samples are unsigned 16-bit ADC counts (~0x8000 midscale at rest). A 64-byte
packet carries 2 EEG frames; an 84-byte packet carries 2 EEG frames + 1 extra
16-byte block (motion/metadata) + the 4-byte cleartext trailer.

Channel order is Emotiv's standard 14-electrode order (left hemisphere first,
then right), which matches the observed 7+7 two-block split:
    AF3 F7 F3 FC5 T7 P7 O1 | O2 P8 T8 FC6 F4 F8 AF4
The left/right grouping is confirmed; the within-group electrode assignment is
the documented Emotiv order and can be spot-checked with a blink test (AF3/AF4
show the largest frontal deflection).
"""
from __future__ import annotations

import struct

import numpy as np

CHANNELS = ["AF3", "F7", "F3", "FC5", "T7", "P7", "O1",
            "O2", "P8", "T8", "FC6", "F4", "F8", "AF4"]

FRAME_BYTES = 32
# byte offset of each channel's little-endian uint16 within a 32-byte frame
_CH_OFFSETS = [2, 4, 6, 8, 10, 12, 14, 18, 20, 22, 24, 26, 28, 30]
_MIDSCALE = 0x8000  # at-rest ADC midpoint

# Motion / accelerometer block (bytes 64..79 of an 84-byte packet's plaintext;
# absent from 64-byte packets). Layout: byte0 = motion counter (0..31), byte1 =
# 0x20 marker, then 7 BIG-endian int16 motion values (the device's IMU uses the
# opposite byte order to the EEG ADC -- the noise sits in the low byte at the
# odd offsets, keeping values physically bounded). Exact physical axis labels
# (accel X/Y/Z vs magnetometer/markers) are provisional -- confirm with a tilt
# test (rotate the headset: the gravity axis swings ~full-scale). Named MOT0..6
# until then.
MOTION_CHANNELS = ["MOT0", "MOT1", "MOT2", "MOT3", "MOT4", "MOT5", "MOT6"]
MOTION_BLOCK_OFF = 64
_MOT_OFFSETS = [2, 4, 6, 8, 10, 12, 14]  # within the 16-byte motion block, BE

# Pre-compiled C struct unpackers for single-call frame & motion extraction.
_FRAME_STRUCT = struct.Struct("<Bx7Hxx7H")
_MOTION_STRUCT = struct.Struct(">Bx7h")


def raw_int16(plaintext: bytes, big_endian: bool = False) -> list[int]:
    """Decrypted bytes as raw int16 samples (little-endian is the hw format)."""
    dt = ">i2" if big_endian else "<i2"
    n = (len(plaintext) // 2) * 2
    return np.frombuffer(plaintext[:n], dt).tolist()


def iter_frames_tuples(plaintext: bytes):
    """Yield (counter, (ch0, ..., ch13)) as a flat tuple for each 32-byte frame."""
    limit = (len(plaintext) // FRAME_BYTES) * FRAME_BYTES
    for off in range(0, limit, FRAME_BYTES):
        unpacked = _FRAME_STRUCT.unpack_from(plaintext, off)
        yield unpacked[0], unpacked[1:]


def iter_frames(plaintext: bytes):
    """Yield (counter, {channel: count}) for each 32-byte EEG frame in a packet.

    The trailing 16-byte motion/metadata block of 84-byte packets (which leaves
    a non-multiple-of-32 remainder) is skipped.
    """
    limit = (len(plaintext) // FRAME_BYTES) * FRAME_BYTES
    for off in range(0, limit, FRAME_BYTES):
        unpacked = _FRAME_STRUCT.unpack_from(plaintext, off)
        yield unpacked[0], dict(zip(CHANNELS, unpacked[1:]))


def decode_eeg(plaintext: bytes) -> dict[str, int] | None:
    """Return {channel: ADC count} for the FIRST EEG frame of a packet.

    Kept for the simple one-row-per-packet path; the recorder uses
    ``iter_frames`` to emit every frame. Returns ``None`` if the payload is too
    short to hold a frame.
    """
    if len(plaintext) < FRAME_BYTES:
        return None
    unpacked = _FRAME_STRUCT.unpack_from(plaintext, 0)
    return dict(zip(CHANNELS, unpacked[1:]))


def centered(counts: dict[str, int]) -> dict[str, int]:
    """Channel counts re-centered on the at-rest midpoint (count - 0x8000)."""
    return {k: v - _MIDSCALE for k, v in counts.items()}


def decode_motion_tuple(plaintext: bytes) -> tuple[int, tuple[int, ...]] | None:
    """Return (motion_counter, (mot0, ..., mot6)) from a packet's motion block."""
    if len(plaintext) < MOTION_BLOCK_OFF + 16:
        return None
    unpacked = _MOTION_STRUCT.unpack_from(plaintext, MOTION_BLOCK_OFF)
    return unpacked[0], unpacked[1:]


def decode_motion(plaintext: bytes) -> tuple[int, dict[str, int]] | None:
    """Return (motion_counter, {MOTn: value}) from a packet's motion block.

    Present only in 84-byte packets (80-byte plaintext); returns ``None`` for
    64-byte EEG-only packets. Values are big-endian int16.
    """
    if len(plaintext) < MOTION_BLOCK_OFF + 16:
        return None
    unpacked = _MOTION_STRUCT.unpack_from(plaintext, MOTION_BLOCK_OFF)
    return unpacked[0], dict(zip(MOTION_CHANNELS, unpacked[1:]))
