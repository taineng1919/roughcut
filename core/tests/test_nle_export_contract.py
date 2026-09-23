from __future__ import annotations

import json
from pathlib import Path

import pytest

import roughcut.cli
import roughcut.mcp
from roughcut.mcp import handle_request


class _FakeOutcome:
    def to_dict(self) -> dict[str, object]:
        return {
            "nle_export": {
                "route": "fcpxml",
                "exporter_profile": "roughcut_fcpxml_1_14",
                "readback": False,
            },
            "receipt": {"action_id": "act_nle"},
        }


def _mcp(tool: str, arguments: dict[str, object]) -> dict[str, object]:
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": "nle-contract",
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
    )
    assert response is not None
    result = response["result"]
    assert isinstance(result, dict)
    payload = result["structuredContent"]
    assert isinstance(payload, dict)
    return payload


def test_nle_tool_schema_is_closed_and_fixed() -> None:
    tool = next(item for item in roughcut.mcp.TOOLS if item["name"] == "approve_nle_export")
    schema = tool["inputSchema"]
    assert isinstance(schema, dict)
    assert schema["required"] == [
        "project_path",
        "run_id",
        "action_id",
        "edit_version_id",
        "expected_revision",
        "route",
        "destination",
        "alignment_artifact_id",
    ]
    assert schema["additionalProperties"] is False
    assert roughcut.mcp.TOOL_SCHEMA_VERSION == 32


def test_cli_and_mcp_route_the_same_explicit_nle_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[dict[str, object]] = []

    def fake(_project_path: Path, **kwargs: object) -> _FakeOutcome:
        calls.append(kwargs)
        return _FakeOutcome()

    monkeypatch.setattr(roughcut.cli, "approve_nle_export", fake)
    roughcut.cli.main(
        [
            "approve-nle-export",
            "--project",
            str(tmp_path),
            "--run-id",
            "run_nle",
            "--action-id",
            "act_nle",
            "--edit-version-id",
            "edit_nle",
            "--expected-revision",
            "7",
            "--route",
            "fcpxml",
            "--destination",
            str(tmp_path / "handoff.fcpxml"),
            "--json",
        ]
    )
    cli_payload = json.loads(capsys.readouterr().out)
    assert cli_payload["nle_export"]["route"] == "fcpxml"
    assert calls[-1]["alignment_artifact_id"] is None

    monkeypatch.setattr(roughcut.mcp, "approve_nle_export", fake)
    mcp_payload = _mcp(
        "approve_nle_export",
        {
            "project_path": str(tmp_path),
            "run_id": "run_nle",
            "action_id": "act_nle",
            "edit_version_id": "edit_nle",
            "expected_revision": 7,
            "route": "fcpxml",
            "destination": str(tmp_path / "handoff.fcpxml"),
            "alignment_artifact_id": None,
        },
    )
    assert mcp_payload["nle_export"]["exporter_profile"] == "roughcut_fcpxml_1_14"
    assert calls[-1]["alignment_artifact_id"] is None


def test_mcp_requires_explicit_null_or_exact_alignment_id() -> None:
    missing = _mcp(
        "approve_nle_export",
        {
            "project_path": "/tmp/project",
            "run_id": "run_nle",
            "action_id": "act_nle_missing",
            "edit_version_id": "edit_nle",
            "expected_revision": 1,
            "route": "fcpxml",
            "destination": "/tmp/handoff.fcpxml",
        },
    )
    assert missing["error"]["code"] == "invalid_arguments"  # type: ignore[index]

    unexpected = _mcp(
        "approve_nle_export",
        {
            "project_path": "/tmp/project",
            "run_id": "run_nle",
            "action_id": "act_nle_unexpected",
            "edit_version_id": "edit_nle",
            "expected_revision": 1,
            "route": "fcpxml",
            "destination": "/tmp/handoff.fcpxml",
            "alignment_artifact_id": None,
            "profile": "other",
        },
    )
    assert unexpected["error"]["code"] == "invalid_arguments"  # type: ignore[index]
