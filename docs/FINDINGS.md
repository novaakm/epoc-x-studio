# EPOC-X direct-decryption: reverse-engineering findings

Development unit: EPOC-X, FW `0x0730`, EEG 128 Hz / 16-bit, motion 32 Hz /
16-bit (from CortexService logs). The findings are device-model-general; the
per-device key and whitening byte are not included (see "Obtaining your
device's key").

**Status: SOLVED END-TO-END.** The headset's EEG is decrypted directly from BLE
with no CortexService, no Emotiv license, and no connect handshake. The complete
live pipeline, per BLE notification payload, is:

```
plaintext = AES256_ECB_decrypt(KEY, ciphertext XOR W)   # KEY and W are per device
```

`epoc-direct-record` streams one CSV row per 14-channel EEG frame. Verified
offline against a real capture: 900 frames, 14 channels, frame counter +1 across
892/899 (the 7 breaks are BLE packet drops).

## Where the decryption lives

Not in `EmotivPRO.exe` (that is only a WebSocket client of Cortex on
`tcp/6868`). The decryption is in the **native Linux**
`/usr/local/emotivapps/bin/CortexService` (ELF, stripped, RTTI intact), class
`Cortex::DataProcessing::EpocXDataDecryption`.

## The cipher (verified)

Per EEG payload the plaintext is recovered by **AES-256 in ECB mode**, per
16-byte block:

```
plaintext_block = AES256_decrypt(KEY, ciphertext_block)
```

Confirmed by breakpointing the live decrypt and checking
`AES256_ECB_decrypt(KEY, in) == out` on real captured blocks (all matched).

- **Block cipher**: AES-256 (14 rounds), ECB on the live stream. Each 16-byte
  block is independent; no IV, no chaining.
- **Implementation**: a bundled **software T-table AES** (decryption-only:
  inverse S-box at `.rodata 0x354edc0`, inverse T-tables `Td0..Td3` at
  `0x354eec0 / 0x354f2c0 / 0x354f6c0 / 0x354fac0`). This is **separate** from the
  CPU AES-NI code (that belongs to BoringSSL and is used only for TLS — which is
  why every AES-NI breakpoint missed the EEG stream).
- **Key funcs** (PIE offsets, add LOAD BASE):
  - `AES_decrypt(in=rdi, out=rsi, AES_KEY*=rdx)` @ `0x2734820` — per block,
    `rounds` at `[rdx+0xf0]` (=14).
  - `AES_set_encrypt_key(userKey=rdi, bits=rsi=256, AES_KEY*=rdx)` @ `0x27336b0`.
  - `AES_set_decrypt_key` @ `0x2734230`; ECB block-loop wrapper @ `0x2734bd0`.
- 64-byte packets = 4 ciphertext blocks. 84-byte packets = 80 ciphertext bytes
  + 4-byte cleartext status trailer (`status_a, quality_a, status_b, quality_b`).

### Whitening (per-device XOR byte)

Before AES, CortexService XOR-whitens every ciphertext byte with a constant `W`
(function `0x16bd880`, called from the EEG path `0x16ec338`). `W` is `0x55` for
old firmware, or a **per-device byte at `decryptor+0x178`** when a FW-version
check (`> 0x73e`) passes. The per-device `W` is recovered by brute-forcing all
256 values against the known frame structure (the correct value is the only one
that yields `byte1 == 0x10` with a clean incrementing counter — see
`tools/find_whiten.py` / `decrypt.autodetect_whiten`). Supply it via
`$EPOCX_WHITEN`, or let the tooling auto-detect it.

### The live decrypt function

`0x16bda10` is the real per-packet AES-256-ECB decrypt (`mov $0x100,%esi` →
`AES_set_decrypt_key`, key = 4th arg = `decryptor+0x158`), called from the
`EpocXDataDecryption` method at `0x16ec3ad`. The caller strips the 84-byte
packet's trailer (`input.mid(0, floor(size/16)*16)`), whitens, then decrypts.
Dead ends: `0x16bd590` (AES-128 CBC, hardcoded IVs = "modes" 2/6) and `0x169b79d`
(AES-128 debug) are **not** on the live path.

### No connect handshake

Captured every GATT write CortexService makes on a fresh connect
(`tools/dump_writes.gdb`): just `discoverDetails` + two `writeDescriptor 0x0100`
(notification enables) — **zero `writeCharacteristic`**. So a bare bleak
`start_notify` on the EEG + status characteristics is sufficient; there is no
command/challenge handshake to replay.

## Obtaining your device's key (and whitening byte)

The 32-byte AES key is **per device**: provisioned per headset, cached in the
decryptor object at `obj+0x158`, constant per device, and **not** derived from
the serial (serial-derived candidates were tried and all failed). The whitening
byte `W` (`obj+0x178`) is per device too. **This repo ships neither** — each
user extracts their own device's values once, locally:

1. **Key** — with CortexService running and the headset streaming, attach the
   capture script (needs sudo; adjust for your install):

   ```
   sudo gdb -q -batch -p "$(systemctl show -p MainPID --value cortex)" \
     -x tools/dump_swaes.gdb 2>&1 | tee key.log
   ```

   `tools/dump_swaes.gdb` breakpoints `AES_decrypt` and `AES_set_encrypt_key`,
   prints the 32-byte key (`RAWKEY=...`, bits=256) and grabs matched
   (ciphertext → plaintext) pairs so you can verify it offline. The key is
   constant across reboots for a given device/pairing.

2. **Whitening byte `W`** — stop CortexService, capture a few seconds of raw
   packets, and brute-force `W` with your key:

   ```
   sudo systemctl stop cortex cortexsync
   python3 tools/capture_raw.py 5 capture.csv
   python3 tools/find_whiten.py capture.csv <YOUR_KEY_HEX>
   ```

3. **Run** with your values (never commit them):

   ```
   EPOCX_AES_KEY=<YOUR_KEY_HEX> EPOCX_WHITEN=0x<W> epoc-direct-record
   ```

## What was tried and ruled out (historical)

- AES-NI single-block (`aes_hw_decrypt` @ `0x2827440`) and the multi-buffer /
  CBC bulk routines never fire for EEG — all TLS. The EEG path is software AES.
- The `0x16bd590` family (AES-128, key@`obj+0x148`, XOR 0x55, CBC IVs
  `e2 80 d8 14…` and `ca fe ba be…`) is a separate path not used by the live
  stream; it misled the earlier analysis.
- Brute-forcing serial-derived AES-128 keys and all logged AES-NI keys: moot
  (wrong cipher/wrong size).

## Frame layout (locked)

A decrypted packet is a sequence of 32-byte **frames**; each frame = two AES
blocks, one 14-channel EEG sample:

```
byte  0      frame counter (+1 per frame, wraps 256)
byte  1      0x10
byte  2..15  channels 0..6   (7 x little-endian uint16)   # left block
byte 16..17  0x00 0x00
byte 18..31  channels 7..13  (7 x little-endian uint16)   # right block
```

64-byte packet = 2 EEG frames. 84-byte packet = 2 EEG frames + 1 extra 16-byte
block (motion/metadata) + the 4-byte cleartext trailer. Samples are unsigned
16-bit ADC counts (~`0x8000` at rest). Channel order (Emotiv standard, left 7 /
right 7 matches the two-block split): `AF3 F7 F3 FC5 T7 P7 O1 | O2 P8 T8 FC6 F4
F8 AF4`.
