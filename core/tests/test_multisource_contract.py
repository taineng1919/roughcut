from __future__ import annotations

import json
from pathlib import Path

import pytest

import roughcut.cli
import roughcut.mcp
from roughcut.application.health import TOOL_SCHEMA_VERSION
from roughcut.application.projects import create_project
from roughcut.application.proposals import MultiSourceProposalState
from roughcut.domain.bindings import SourceTranscriptBinding
from roughcut.domain.brief import EditBrief
from roughcut.domain.edit import EditClip, MultiSourceEditProposal
from roughcut.domain.project import ProjectError


def _proposal_state() -> MultiSourceProposalState:
    proposal = MultiSourceEditProposal(
        proposal_id="proposal_contract",
        base_project_revision=4,
        base_edit_version_id=None,
        source_bindings=(
            SourceTranscriptBinding("src_a", "tr_a"),
            SourceTranscriptBinding("src_b", "tr_b"),
        ),
        brief_snapshot=EditBrief("brief_contract", "主题", 240_000, ("重点",), True),
        context_hash="c" * 64,
        clips=(
            EditClip(
                "clip_a",
                "src_a",
                "tr_a",
                "seg_a",
                0,
                120_000,
                "A",
                "A",
            ),
            EditClip(
                "clip_b",
                "src_b",
                "tr_b",
                "seg_b",
                0,
                120_000,
                "B",
                "B",
            ),
        ),
        total_duration_ticks=240_000,
        created_at="fixture",
    )
    return MultiSourceProposalState(proposal=proposal, project_revision=4)


def _mcp_payload(name: str, arguments: dict[str, object]) -> dict[str, object]:
    response = roughcut.mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    assert response is not None
    result = response["result"]
    assert isinstance(result, dict)
    payload = result["structuredContent"]
    assert isinstance(payload, dict)
    return payload


def test_multi_source_tools_remain_listed_at_schema_version_twelve() -> None:
    assert TOOL_SCHEMA_VERSION == 32
    assert {tool["name"] for tool in roughcut.mcp.TOOLS} >= {
        "multi_source_context",
        "multi_source_proposal_create",
        "multi_source_proposal_confirm",
        "multi_source_edit_decision_read",
        "decision_read",
    }


def test_cli_and_mcp_multi_source_proposal_create_share_workflow_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    create_project(tmp_path / "project", "Contract")
    calls: list[dict[str, object]] = []

    def fake_create(project_path: Path, **arguments: object) -> MultiSourceProposalState:
        calls.append({"project_path": project_path, **arguments})
        return _proposal_state()

    bindings = [
        {"source_id": "src_a", "transcript_version_id": "tr_a"},
        {"source_id": "src_b", "transcript_version_id": "tr_b"},
    ]
    clips = [clip.to_dict() for clip in _proposal_state().proposal.clips]
    monkeypatch.setattr(roughcut.cli, "create_multi_source_edit_proposal", fake_create)
    with pytest.raises(SystemExit) as exit_info:
        roughcut.cli.main(
            [
                "multi-source-proposal-create",
                "--project",
                str(tmp_path / "project"),
                "--source-bindings-json",
                json.dumps(bindings),
                "--brief-id",
                "brief_contract",
                "--context-hash",
                "c" * 64,
                "--clips-json",
                json.dumps(clips),
                "--total-duration-ticks",
                "240000",
                "--expected-revision",
                "4",
                "--json",
            ]
        )
    assert exit_info.value.code == 2
    cli_payload = json.loads(capsys.readouterr().out)

    monkeypatch.setattr(roughcut.mcp, "create_multi_source_edit_proposal", fake_create)
    mcp_payload = _mcp_payload(
        "multi_source_proposal_create",
        {
            "project_path": str(tmp_path / "project"),
            "source_bindings": bindings,
            "brief_id": "brief_contract",
            "context_hash": "c" * 64,
            "clips": clips,
            "total_duration_ticks": 240_000,
            "expected_revision": 4,
        },
    )

    assert calls == []
    assert cli_payload == mcp_payload
    assert cli_payload["error"] == {"code": "workflow_required"}


def test_multi_source_cli_and_mcp_failures_keep_versioned_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    create_project(tmp_path / "project", "Contract")

    def fail(*_args: object, **_kwargs: object) -> MultiSourceProposalState:
        raise ProjectError("fixture")

    monkeypatch.setattr(roughcut.cli, "create_multi_source_edit_proposal", fail)
    with pytest.raises(SystemExit) as exit_info:
        roughcut.cli.main(
            [
                "multi-source-proposal-create",
                "--project",
                str(tmp_path / "project"),
                "--source-bindings-json",
                (
                    '[{"source_id":"src_a","transcript_version_id":"tr_a"},'
                    '{"source_id":"src_b","transcript_version_id":"tr_b"}]'
                ),
                "--brief-id",
                "brief_contract",
                "--context-hash",
                "c" * 64,
                "--clips-json",
                "[]",
                "--total-duration-ticks",
                "1",
                "--expected-revision",
                "4",
                "--json",
            ]
        )
    streams = capsys.readouterr()
    assert exit_info.value.code == 2
    assert streams.err == ""
    assert len(streams.out.splitlines()) == 1
    assert json.loads(streams.out)["error"] == {"code": "workflow_required"}

    monkeypatch.setattr(roughcut.mcp, "create_multi_source_edit_proposal", fail)
    payload = _mcp_payload(
        "multi_source_proposal_create",
        {
            "project_path": str(tmp_path / "project"),
            "source_bindings": [
                {"source_id": "src_a", "transcript_version_id": "tr_a"},
                {"source_id": "src_b", "transcript_version_id": "tr_b"},
            ],
            "brief_id": "brief_contract",
            "context_hash": "c" * 64,
            "clips": [],
            "total_duration_ticks": 1,
            "expected_revision": 4,
        },
    )
    assert payload["ok"] is False
    assert payload["error"] == {"code": "workflow_required"}


def test_multi_source_mcp_missing_arguments_keep_the_existing_versioned_error() -> None:
    payload = _mcp_payload(
        "multi_source_context",
        {"project_path": "fixture"},
    )

    assert payload["ok"] is False
    assert payload["error"] == {"code": "multi_source_operation_failed"}
