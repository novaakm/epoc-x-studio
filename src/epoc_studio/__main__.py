"""Compatibility entry point forwarding ``python -m epoc_studio`` to ``presteeg``."""
from __future__ import annotations

import sys

from presteeg.launcher import main

if __name__ == "__main__":
    sys.exit(main())
