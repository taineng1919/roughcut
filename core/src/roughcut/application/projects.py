"""Project creation and opening services."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from roughcut.adapters.project_lock import LOCK_FILENAME
from roughcut.adapters.project_store import ProjectStore
from roughcut.domain.project import Project, ProjectError
from roughcut.domain.time import TICKS_PER_SECOND

OUTPUT_PRESETS = {
    "landscape_1080p": (1920, 1080),
    "portrait_1080p": (1080, 1920),
}


def create_project(
    project_path: Path,
    name: str,
    *,
    output_preset: str | None = None,
) -> Project:
    if not name.strip():
        raise ProjectError("project name is required")
    preset = "landscape_1080p" if output_preset is None else output_preset
    if preset not in OUTPUT_PRESETS:
        raise ProjectError("output preset is unsupported")
    width, height = OUTPUT_PRESETS[preset]
    resolved = project_path.resolve()
    resolved.mkdir(parents=True, exist_ok=False)
    (resolved / "sources").mkdir()
    now = datetime.now(UTC).isoformat()
    project = Project(
        schema_version=1,
        project_id=f"proj_{uuid4().hex}",
        revision=0,
        name=name,
        created_at=now,
        updated_at=now,
        settings={
            "timebase": TICKS_PER_SECOND,
            "frame_rate": {"numerator": 25, "denominator": 1},
            "width": width,
            "height": height,
            "audio_sample_rate": 48_000,
        },
        sources=(),
        active_transcript_versions={},
        active_brief_id=None,
        active_edit_version_id=None,
    )
    try:
        ProjectStore(resolved).save(project, expected_revision=None)
    except Exception:
        (resolved / LOCK_FILENAME).unlink(missing_ok=True)
        (resolved / "sources").rmdir()
        resolved.rmdir()
        raise
    return project


def open_project(project_path: Path) -> Project:
    return ProjectStore(project_path).load()
