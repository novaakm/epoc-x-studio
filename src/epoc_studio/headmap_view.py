"""Compatibility shim forwarding ``epoc_studio.headmap_view`` to ``presteeg.headmap_view``."""
from __future__ import annotations

import sys

from presteeg.headmap_view import *  # noqa: F401,F403
from presteeg.headmap_view import main

if __name__ == "__main__":
    sys.exit(main())
