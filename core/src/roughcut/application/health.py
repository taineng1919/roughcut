"""Environment-independent health information."""

from __future__ import annotations

import platform

from roughcut import __version__

SCHEMA_VERSION = 1
TOOL_SCHEMA_VERSION = 32


def source_commit() -> str | None:
    """Installed Code identity: staged release SHA, or None on dev checkouts."""
    try:
        from roughcut._build_identity import SOURCE_COMMIT
    except ImportError:
        return None
    if (
        isinstance(SOURCE_COMMIT, str)
        and len(SOURCE_COMMIT) == 40
        and all(value in "0123456789abcdef" for value in SOURCE_COMMIT)
    ):
        return SOURCE_COMMIT
    return None


def health() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "tool_schema_version": TOOL_SCHEMA_VERSION,
        "core_version": __version__,
        "source_commit": source_commit(),
        "platform": platform.platform(),
        "ok": True,
    }
