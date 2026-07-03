"""Console output that never crashes on a narrow terminal encoding.

Alert bodies can contain emoji or arrows; a Windows cp1252 console would raise
``UnicodeEncodeError`` on those. This helper degrades gracefully instead.
"""

from __future__ import annotations

import sys


def safe_print(msg: str) -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "ascii"
        print(msg.encode(enc, errors="replace").decode(enc), flush=True)
