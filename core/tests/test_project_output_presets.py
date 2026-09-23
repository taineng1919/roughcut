from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from roughcut.application.health import TOOL_SCHEMA_VERSION
from roughcut.application.projects import create_project, open_project
from roughcut.domain.project import (
    ImportMode,
    MediaProbe,
    ProjectError,
    SourceAsset,
    SourceFingerprint,
)
from roughcut.domain.proxy import derive_proxy_profile
from roughcut.domain.render import OutputSettings
from roughcut.mcp import TOOLS, handle_request


def _project_payload(response: dict[str, object]) -> dict[str, object]:
    result = response["result"]
    assert isinstance(result, dict)
    payload = result["structuredContent"]
    assert isinstance(payload, dict)
    project = payload["project"]
    assert isinstance(project, dict)
    return project


def _horizontal_source() -> SourceAsset:
    return SourceAsset(
        source_id="src_horizontal",
        kind="video",
        display_name="横版素材.mp4",
        import_mode=ImportMode.LINKED,
        locator={"absolute_path": "/fixture/horizontal.mp4"},
        fingerprint=SourceFingerprint(1, 1, "fixture"),
        probe=MediaProbe(
            duration_ticks=120_000,
            container_start_ticks=0,
            first_content_ticks=0,
            video_codec="h264",
            width=1920,
            height=1080,
            nominal_frame_rate={"numerator": 25, "denominator": 1},
            is_vfr=False,
            audio_codec="aac",
            audio_sample_rate=48_000,
            rotation_degrees=0,
        ),
    )


def test_project_create_defaults_to_landscape_and_accepts_portrait_preset(tmp_path: Path) -> None:
    landscape = create_project(tmp_path / "landscape", "横版")
    explicit_landscape = create_project(
        tmp_path / "explicit-landscape",
        "明确横版",
        output_preset="landscape_1080p",
    )
    portrait = create_project(
        tmp_path / "portrait",
        "竖版",
        output_preset="portrait_1080p",
    )

    assert landscape.settings == {
        "timebase": 120_000,
        "frame_rate": {"numerator": 25, "denominator": 1},
        "width": 1920,
        "height": 1080,
        "audio_sample_rate": 48_000,
    }
    assert explicit_landscape.settings == landscape.settings
    assert portrait.settings == {
        "timebase": 120_000,
        "frame_rate": {"numerator": 25, "denominator": 1},
        "width": 1080,
        "height": 1920,
        "audio_sample_rate": 48_000,
    }
    assert open_project(tmp_path / "portrait") == portrait


@pytest.mark.parametrize("preset", ["", "square", "landscape", "portrait_4k", "1080x1920"])
def test_project_create_rejects_any_preset_outside_the_public_pair(
    tmp_path: Path,
    preset: str,
) -> None:
    with pytest.raises(ProjectError, match="output preset"):
        create_project(tmp_path / f"invalid-{preset or 'empty'}", "无效", output_preset=preset)


def test_cli_and_mcp_project_create_share_portrait_preset_and_current_schema(
    tmp_path: Path,
) -> None:
    cli_path = tmp_path / "CLI 竖版"
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "roughcut.cli",
            "project-create",
            "--project",
            str(cli_path),
            "--name",
            "CLI 竖版",
            "--output-preset",
            "portrait_1080p",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0
    assert process.stderr == ""
    cli_payload = json.loads(process.stdout)

    mcp_path = tmp_path / "MCP 竖版"
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": "portrait",
            "method": "tools/call",
            "params": {
                "name": "project_create",
                "arguments": {
                    "project_path": str(mcp_path),
                    "name": "MCP 竖版",
                    "output_preset": "portrait_1080p",
                },
            },
        }
    )
    assert response is not None
    mcp_project = _project_payload(response)
    assert cli_payload["tool_schema_version"] == TOOL_SCHEMA_VERSION == 32
    assert mcp_project["settings"] == cli_payload["project"]["settings"]
    project_tool = next(tool for tool in TOOLS if tool["name"] == "project_create")
    assert project_tool["inputSchema"]["properties"]["output_preset"] == {
        "type": "string",
        "enum": ["landscape_1080p", "portrait_1080p"],
    }


@pytest.mark.parametrize("argument", ["width", "height", "frame_rate", "codec"])
def test_project_create_rejects_arbitrary_output_settings_for_cli_and_mcp(
    tmp_path: Path,
    argument: str,
) -> None:
    cli_path = tmp_path / f"CLI {argument}"
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "roughcut.cli",
            "project-create",
            "--project",
            str(cli_path),
            "--name",
            "不应创建",
            f"--{argument.replace('_', '-')}",
            "123",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert process.returncode == 2
    assert process.stderr == ""
    assert json.loads(process.stdout)["ok"] is False
    assert not cli_path.exists()

    mcp_path = tmp_path / f"MCP {argument}"
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": argument,
            "method": "tools/call",
            "params": {
                "name": "project_create",
                "arguments": {
                    "project_path": str(mcp_path),
                    "name": "不应创建",
                    argument: 123,
                },
            },
        }
    )
    assert response is not None
    result = response["result"]
    assert isinstance(result, dict)
    payload = result["structuredContent"]
    assert isinstance(payload, dict)
    assert payload["ok"] is False
    assert payload["error"] == {"code": "project_operation_failed"}
    assert not mcp_path.exists()


def test_portrait_project_uses_one_canvas_for_proxy_and_render_settings(tmp_path: Path) -> None:
    project = create_project(tmp_path / "vertical", "竖版", output_preset="portrait_1080p")
    profile = derive_proxy_profile(_horizontal_source().probe, project.settings)
    render_settings = OutputSettings.from_dict(project.settings)

    assert (profile.canvas_width, profile.canvas_height) == (404, 720)
    assert profile.canvas_width < profile.canvas_height
    assert render_settings.to_dict() == {
        "width": 1080,
        "height": 1920,
        "frame_rate": {"numerator": 25, "denominator": 1},
        "audio_sample_rate": 48_000,
    }
