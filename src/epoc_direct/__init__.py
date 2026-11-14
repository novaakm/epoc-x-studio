"""epoc-direct-record: decrypt the EPOC-X BLE stream directly (no Cortex, no
screen-scraping), the way the native CortexService does internally.

See decrypt.py for the reverse-engineered cipher and README.md for status.
"""
__all__ = ["ble", "decrypt", "channels", "record", "devices"]
