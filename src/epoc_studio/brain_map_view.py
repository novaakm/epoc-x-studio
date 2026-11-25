"""Compatibility shim forwarding ``epoc_studio.brain_map_view`` to ``presteeg.brain_map_view``."""
from __future__ import annotations

import sys

from presteeg.brain_map_view import *  # noqa: F401,F403
from presteeg.brain_map_view import main

if __name__ == "__main__":
    sys.exit(main())
