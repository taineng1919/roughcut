from __future__ import annotations

import json
from pathlib import Path

import pytest

from roughcut import cli
from roughcut.application.health import TOOL_SCHEMA_VERSION
from roughcut.domain.workflow import WORKFLOW_ACTIONS
from roughcut.mcp import TOOLS

ROOT = Path(__file__).resolve().parents[2]


def test_phase3_vectors_have_the_frozen_37_22_15_owner_surface() -> None:
    vectors = json.loads(
        (ROOT / "core/tests/fixtures/multicam-alignment-vectors.json").read_text(
            encoding="utf-8"
        )
    )["vectors"]
    phase3 = [item for item in vectors if item["execution_owner"].startswith("phase3_")]
    phase2 = [item for item in vectors if item["execution_owner"].startswith("phase2_")]
    expected_phase3_owners = {
        "phase3_parallel_prepare_application",
        "phase3_parallel_operation_integration",
        "phase3_parallel_render_media_integration",
        "phase3_parallel_operation_process_integration",
        "phase3_parallel_store_concurrency_integration",
        "phase3_parallel_operation_store_integration",
        "phase3_parallel_operation_revalidation_integration",
        "phase3_parallel_operation_store_media_integration",
        "phase3_parallel_prepare_application_store_integration",
        "phase3_parallel_prepare_application_render_schedule_integration",
        "phase3_parallel_prepare_application_operation_store_integration",
        "phase3_parallel_start_application_store_integration",
    }
    assert len(vectors) == 37
    assert len(phase2) == 22
    assert len(phase3) == 15
    assert {item["execution_owner"] for item in phase3} == expected_phase3_owners
    assert len({item["id"] for item in phase3}) == 15


def test_schema26_adds_only_the_three_multicam_tools_and_reuses_status() -> None:
    assert TOOL_SCHEMA_VERSION == 32
    names = [tool["name"] for tool in TOOLS]
    added = [
        "align_multicam",
        "multicam_parallel_render_prepare",
        "multicam_parallel_render_start",
    ]
    assert len(names) == len(set(names))
    assert all(names.count(name) == 1 for name in added)
    assert "media_operation_status" in names
    assert len(WORKFLOW_ACTIONS) == 9
    for name in added:
        schema = next(tool["inputSchema"] for tool in TOOLS if tool["name"] == name)
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
    align_schema = next(
        tool["inputSchema"] for tool in TOOLS if tool["name"] == "align_multicam"
    )
    assert align_schema["properties"]["main_camera"]["properties"]["camera_id"] == {
        "const": "main"
    }
    auxiliary_camera_id = align_schema["properties"]["auxiliary_cameras"]["items"][
        "properties"
    ]["camera_id"]
    assert auxiliary_camera_id["type"] == "string"
    assert auxiliary_camera_id["pattern"] == "^[A-Za-z0-9_-]{1,128}$"
    assert auxiliary_camera_id["not"] == {"const": "main"}


def test_schema26_cli_routes_the_three_new_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    commands = (
        "align-multicam",
        "multicam-parallel-render-prepare",
        "multicam-parallel-render-start",
    )
    for command in commands:
        try:
            cli.main([command, "--project", "/tmp/roughcut-contract", "--json"])
        except SystemExit as error:
            assert error.code == 2
        else:
            raise AssertionError(f"{command} unexpectedly accepted incomplete input")
        assert '"code": "invalid_arguments"' in capsys.readouterr().out


def test_tool_contract_mirrors_are_byte_identical_to_the_source() -> None:
    source = ROOT / "docs/agent-tool-contract.md"
    mirrors = sorted(
        (ROOT / "agent-skill/skills").glob("*/references/tool-contract.md")
    )
    assert len(mirrors) == 5
    source_bytes = source.read_bytes()
    assert all(path.read_bytes() == source_bytes for path in mirrors)
