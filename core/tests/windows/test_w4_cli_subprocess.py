from __future__ import annotations

import os
from pathlib import Path

import pytest
from windows.w4_support import run_cli_raw


@pytest.mark.parametrize(
    "environment",
    (
        {"PYTHONUTF8": "0"},
        {"PYTHONUTF8": "0", "PYTHONIOENCODING": "cp1252"},
    ),
)
def test_cli_success_and_error_are_single_utf8_json_values(
    environment: dict[str, str],
) -> None:
    success = run_cli_raw(
        "fake-project-roundtrip",
        "--project-name",
        "中文 project with spaces",
        "--json",
        environment=environment,
    )
    assert success["ok"] is True
    assert success["project"] == {
        "schema_version": 1,
        "name": "中文 project with spaces",
    }

    error = run_cli_raw(
        "health",
        "--unknown",
        "--json",
        expected_returncode=2,
        environment=environment,
    )
    assert error["ok"] is False
    assert error["error"] == {"code": "invalid_arguments"}


def test_cli_preserves_unicode_and_space_bearing_project_path_across_restart(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "中文 project with spaces"
    created = run_cli_raw(
        "project-create",
        "--project",
        str(project_path),
        "--name",
        "中文 project name",
        "--json",
        environment={"PYTHONUTF8": "0", "PYTHONIOENCODING": "cp1252"},
    )
    assert created["ok"] is True
    assert created["project"]["name"] == "中文 project name"
    assert project_path.joinpath("project.json").is_file()

    reopened = run_cli_raw(
        "project-open",
        "--project",
        str(project_path),
        "--json",
        environment={"PYTHONUTF8": "0", "PYTHONIOENCODING": "cp1252"},
    )
    assert reopened["ok"] is True
    assert reopened["project"] == created["project"]


@pytest.mark.skipif(os.name != "nt", reason="requires a real Windows drive-letter path")
def test_cli_uses_the_real_windows_drive_path_without_rebinding(tmp_path: Path) -> None:
    project_path = tmp_path / "中文 drive project with spaces"
    assert project_path.drive
    assert ":" in str(project_path)

    created = run_cli_raw(
        "project-create",
        "--project",
        str(project_path),
        "--name",
        "Windows drive project",
        "--json",
    )
    assert created["ok"] is True

    started = run_cli_raw(
        "workflow-start",
        "--project",
        str(project_path),
        "--run-id",
        "wfr_drive",
        "--ordered-source-ids-json",
        "[]",
        "--json",
    )
    assert started["workflow_run"]["run_id"] == "wfr_drive"
    assert project_path.joinpath("workflow", "runs", "wfr_drive.json").is_file()

    status = run_cli_raw(
        "workflow-status",
        "--project",
        str(project_path),
        "--run-id",
        "wfr_drive",
        "--json",
    )
    assert status["status"]["workflow_run"]["run_id"] == "wfr_drive"
