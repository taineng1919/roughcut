"""Host-neutral roughcut core."""

from __future__ import annotations

import sys

__version__ = "0.2.10"


def _ensure_supported_python(version: tuple[int, int]) -> None:
    if version < (3, 11):
        raise RuntimeError("roughcut requires Python 3.11 or newer")


_ensure_supported_python(sys.version_info[:2])
