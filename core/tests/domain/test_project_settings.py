from __future__ import annotations

import json
from pathlib import Path

import pytest

from roughcut.application.projects import create_project, open_project
from roughcut.domain.project import Project, ProjectError


def _project_data(settings: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "project_id": "proj_settings",
        "revision": 0,
        "name": "Settings",
        "created_at": "fixture",
        "updated_at": "fixture",
        "settings": settings,
        "sources": [],
        "active_transcript_versions": {},
        "active_brief_id": None,
        "active_edit_version_id": None,
    }


def test_new_projects_store_complete_default_output_settings(tmp_path: Path) -> None:
    project = create_project(tmp_path / "project", "Output defaults")

    assert project.settings == {
        "timebase": 120_000,
        "frame_rate": {"numerator": 25, "denominator": 1},
        "width": 1920,
        "height": 1080,
        "audio_sample_rate": 48_000,
    }


def test_schema_v1_projects_without_dimensions_read_with_compatible_defaults(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "legacy"
    project_path.mkdir()
    data = _project_data(
        {
            "timebase": 120_000,
            "frame_rate": {"numerator": 25, "denominator": 1},
            "audio_sample_rate": 48_000,
        }
    )
    (project_path / "project.json").write_text(json.dumps(data), encoding="utf-8")

    project = open_project(project_path)

    assert project.settings["width"] == 1920
    assert project.settings["height"] == 1080
    assert Project.from_dict(project.to_dict()) == project


@pytest.mark.parametrize(
    "settings",
    [
        {"timebase": 120_000, "frame_rate": {"numerator": 25, "denominator": 1}, "width": 0, "height": 1080, "audio_sample_rate": 48_000},
        {"timebase": 120_000, "frame_rate": {"numerator": 25, "denominator": 1}, "width": 1919, "height": 1080, "audio_sample_rate": 48_000},
        {"timebase": 120_000, "frame_rate": {"numerator": 0, "denominator": 1}, "width": 1920, "height": 1080, "audio_sample_rate": 48_000},
        {"timebase": 120_000, "frame_rate": {"numerator": 25, "denominator": 0}, "width": 1920, "height": 1080, "audio_sample_rate": 48_000},
        {"timebase": 120_000, "frame_rate": {"numerator": 25, "denominator": 1}, "width": 1920, "height": 1080, "audio_sample_rate": 0},
        {"timebase": 1_000, "frame_rate": {"numerator": 25, "denominator": 1}, "width": 1920, "height": 1080, "audio_sample_rate": 48_000},
    ],
)
def test_project_rejects_invalid_output_settings(settings: dict[str, object]) -> None:
    with pytest.raises(ProjectError):
        Project.from_dict(_project_data(settings))
