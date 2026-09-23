"""Media-free project roundtrip used to prove the host tool contract."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from roughcut import __version__
from roughcut.application.health import (
    SCHEMA_VERSION,
    TOOL_SCHEMA_VERSION,
    source_commit,
)


def fake_project_roundtrip(
    project_name: str, *, temporary_parent: Path | None = None
) -> dict[str, object]:
    """Create, read and remove a deliberately minimal temporary project."""
    if not project_name:
        raise ValueError("project_name is required")

    with TemporaryDirectory(
        prefix="roughcut-fake-project-", dir=temporary_parent
    ) as temporary_directory:
        project_path = Path(temporary_directory) / "project.json"
        original = {"schema_version": SCHEMA_VERSION, "name": project_name}
        project_path.write_text(json.dumps(original), encoding="utf-8")
        read_back = json.loads(project_path.read_text(encoding="utf-8"))

    return {
        "schema_version": SCHEMA_VERSION,
        "tool_schema_version": TOOL_SCHEMA_VERSION,
        "core_version": __version__,
        "source_commit": source_commit(),
        "ok": True,
        "project": read_back,
    }
