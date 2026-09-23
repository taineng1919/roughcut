from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import roughcut.cli
import roughcut.mcp
from roughcut.adapters.ffmpeg.render import FFmpegRenderError, RenderCancelled
from roughcut.application.health import TOOL_SCHEMA_VERSION
from roughcut.application.projects import create_project
from roughcut.application.renders import RenderResult
from roughcut.domain.project import ProjectError


def _result() -> RenderResult:
    return RenderResult(
        render_id="render_fixture",
        mp4_path="renders/render_fixture.mp4",
        manifest_path="renders/render_fixture.manifest.json",
        acceptance={"verified": True},
    )


def test_cli_render_requires_workflow_before_application_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_path = tmp_path / "project"
    create_project(project_path, "Render")
    captured: dict[str, object] = {}

    def fake_render(
        project_path: Path,
        *,
        edit_version_id: str,
        expected_revision: int,
    ) -> RenderResult:
        captured.update(
            project_path=project_path,
            edit_version_id=edit_version_id,
            expected_revision=expected_revision,
        )
        return _result()

    monkeypatch.setattr(roughcut.cli, "render_roughcut", fake_render)

    with pytest.raises(SystemExit) as exit_info:
        roughcut.cli.main(
            [
                "render-roughcut",
                "--project",
                str(project_path),
                "--edit-version-id",
                "edit_fixture",
                "--expected-revision",
                "0",
                "--json",
            ]
        )

    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert exit_info.value.code == 2
    assert output.err == ""
    assert captured == {}
    assert payload["error"] == {"code": "workflow_required"}
    assert payload["tool_schema_version"] == TOOL_SCHEMA_VERSION


def test_mcp_render_requires_workflow_before_application_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_path = tmp_path / "project"
    create_project(project_path, "Render")
    captured: dict[str, object] = {}

    def fake_render(
        project_path: Path,
        *,
        edit_version_id: str,
        expected_revision: int,
    ) -> RenderResult:
        captured.update(
            project_path=project_path,
            edit_version_id=edit_version_id,
            expected_revision=expected_revision,
        )
        return _result()

    monkeypatch.setattr(roughcut.mcp, "render_roughcut", fake_render)
    response = roughcut.mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "render_roughcut",
                "arguments": {
                    "project_path": str(project_path),
                    "edit_version_id": "edit_fixture",
                    "expected_revision": 0,
                },
            },
        }
    )

    assert response is not None
    result = response["result"]
    assert isinstance(result, dict)
    assert result["structuredContent"]["error"] == {"code": "workflow_required"}
    assert captured == {}


def test_render_service_failures_cannot_bypass_public_workflow_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_path = tmp_path / "project"
    create_project(project_path, "Render")

    def fail(*_args: object, **_kwargs: object) -> RenderResult:
        raise ProjectError("fixture")

    monkeypatch.setattr(roughcut.cli, "render_roughcut", fail)
    with pytest.raises(SystemExit) as exit_info:
        roughcut.cli.main(
            [
                "render-roughcut",
                "--project",
                str(project_path),
                "--edit-version-id",
                "edit_fixture",
                "--expected-revision",
                "0",
                "--json",
            ]
        )
    output = capsys.readouterr()
    assert exit_info.value.code == 2
    assert output.err == ""
    assert json.loads(output.out)["error"] == {"code": "workflow_required"}

    monkeypatch.setattr(roughcut.mcp, "render_roughcut", fail)
    response = roughcut.mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "render_roughcut",
                "arguments": {
                    "project_path": str(project_path),
                    "edit_version_id": "edit_fixture",
                    "expected_revision": 0,
                },
            },
        }
    )
    assert response is not None
    result = response["result"]
    assert isinstance(result, dict)
    assert result["isError"] is True
    assert result["structuredContent"]["error"] == {"code": "workflow_required"}


def test_render_cli_subprocess_rejects_missing_input_as_pure_json() -> None:
    process = subprocess.run(
        [sys.executable, "-m", "roughcut.cli", "render-roughcut", "--json"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert process.returncode == 2
    assert process.stderr == ""
    assert len(process.stdout.splitlines()) == 1
    assert json.loads(process.stdout)["error"] == {"code": "invalid_input"}


@pytest.mark.parametrize(
    ("failure", "_expected_code"),
    [
        (FFmpegRenderError("fixture"), "render_operation_failed"),
        (RenderCancelled("fixture"), "render_cancelled"),
    ],
)
def test_cli_and_mcp_gate_before_render_failure_or_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: Exception,
    _expected_code: str,
) -> None:
    project_path = tmp_path / "project"
    create_project(project_path, "Render")

    def fail(*_args: object, **_kwargs: object) -> RenderResult:
        raise failure

    arguments = {
        "project_path": str(project_path),
        "edit_version_id": "edit_multi",
        "expected_revision": 0,
    }
    monkeypatch.setattr(roughcut.cli, "render_roughcut", fail)
    with pytest.raises(SystemExit) as exit_info:
        roughcut.cli.main(
            [
                "render-roughcut",
                "--project",
                arguments["project_path"],
                "--edit-version-id",
                arguments["edit_version_id"],
                "--expected-revision",
                str(arguments["expected_revision"]),
                "--json",
            ]
        )
    cli_output = capsys.readouterr()
    assert exit_info.value.code == 2
    assert cli_output.err == ""
    assert json.loads(cli_output.out)["error"] == {"code": "workflow_required"}

    monkeypatch.setattr(roughcut.mcp, "render_roughcut", fail)
    response = roughcut.mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "render_roughcut", "arguments": arguments},
        }
    )
    assert response is not None
    result = response["result"]
    assert isinstance(result, dict)
    assert result["structuredContent"]["error"] == {"code": "workflow_required"}
