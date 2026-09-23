"""Build-injected Code source identity (never commit a SHA here).

The tracked file always keeps ``SOURCE_COMMIT = None`` (dev checkout).
The release builder copies the source to a staging directory, overwrites
this file in staging only with the exact 40-character committed HEAD SHA,
then builds the wheel and the source bundle from that same staging tree.
Both artifacts therefore share one injected identity without any Git
self-reference in tracked source.
"""

from __future__ import annotations

SOURCE_COMMIT: str | None = None
