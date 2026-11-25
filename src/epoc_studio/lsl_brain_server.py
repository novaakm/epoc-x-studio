"""Compatibility shim forwarding ``epoc_studio.lsl_brain_server`` to ``presteeg.lsl_brain_server``."""
from __future__ import annotations

import sys

from presteeg.lsl_brain_server import *  # noqa: F401,F403
from presteeg.lsl_brain_server import main

if __name__ == "__main__":
    sys.exit(main())
