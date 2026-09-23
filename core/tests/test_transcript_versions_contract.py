from __future__ import annotations

import json
from pathlib import Path

import pytest

import roughcut.cli
import roughcut.mcp
from roughcut.application.health import TOOL_SCHEMA_VERSION
from roughcut.application.transcripts import (
    EditReferenceStatus,
    TranscriptMutation,
    TranscriptVersionActivation,
    TranscriptVersionsState,
)
from roughcut.domain.project import ProjectError
from roughcut.domain.transcript import TimedTranscript, TranscriptProvenance


def _transcript() -> TimedTranscript:
    return TimedTranscript(
        schema_version=1,
        transcript_version_id="tr_child",
        source_id="src_fixture",
        parent_version_id="tr_parent",
        provenance=TranscriptProvenance(
            backend="fixture",
            package_version="fixture",
            models={},
            parameters={},
            raw_result_path="raw-asr/src_fixture/run.json",
            started_at="fixture",
            completed_at="fixture",
            exit_status=0,
        ),
        language="zh-CN",
        segments=(),
    )


def _mcp_call(name: str, arguments: dict[str, object]) -> dict[str, object]:
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


def test_transcript_version_tools_remain_listed_at_schema_version_eleven() -> None:
    assert TOOL_SCHEMA_VERSION == 32
    assert {tool["name"] for tool in roughcut.mcp.TOOLS} >= {
        "transcript_correct",
        "transcript_version_activate",
        "transcript_versions_read",
    }


def test_cli_and_mcp_correction_use_identical_payload_and_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[dict[str, object]] = []
    result = TranscriptMutation(True, 4, _transcript())

    def fake_correct(project_path: Path, **arguments: object) -> TranscriptMutation:
        calls.append({"project_path": project_path, **arguments})
        return result

    common = {
        "source_id": "src_fixture",
        "parent_transcript_version_id": "tr_parent",
        "corrections": [{"segment_id": "seg_1", "corrected_text": "校正"}],
        "expected_revision": 3,
    }
    monkeypatch.setattr(roughcut.cli, "correct_transcript", fake_correct)
    roughcut.cli.main(
        [
            "transcript-correct",
            "--project",
            str(tmp_path / "project"),
            "--source-id",
            "src_fixture",
            "--parent-transcript-id",
            "tr_parent",
            "--corrections-json",
            '[{"segment_id":"seg_1","corrected_text":"校正"}]',
            "--expected-revision",
            "3",
            "--json",
        ]
    )
    cli_payload = json.loads(capsys.readouterr().out)

    monkeypatch.setattr(roughcut.mcp, "correct_transcript", fake_correct)
    mcp_payload = _mcp_call(
        "transcript_correct",
        {"project_path": str(tmp_path / "project"), **common},
    )
    assert calls[0] == calls[1]
    assert cli_payload == mcp_payload
    assert cli_payload["transcript_mutation"]["changed"] is True


def test_cli_and_mcp_activation_and_read_are_equivalent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    activation = TranscriptVersionActivation(
        source_id="src_fixture",
        transcript_version_id="tr_child",
        project_revision=5,
        changed=True,
    )
    versions = TranscriptVersionsState(
        source_id="src_fixture",
        active_transcript_version_id="tr_child",
        project_revision=5,
        versions=(),
        edit_reference_status=EditReferenceStatus("none", ()),
    )
    monkeypatch.setattr(roughcut.cli, "activate_transcript_version", lambda *_a, **_k: activation)
    roughcut.cli.main(
        [
            "transcript-version-activate",
            "--project",
            str(tmp_path / "project"),
            "--source-id",
            "src_fixture",
            "--transcript-id",
            "tr_child",
            "--expected-revision",
            "4",
            "--json",
        ]
    )
    cli_activation = json.loads(capsys.readouterr().out)
    monkeypatch.setattr(roughcut.mcp, "activate_transcript_version", lambda *_a, **_k: activation)
    mcp_activation = _mcp_call(
        "transcript_version_activate",
        {
            "project_path": str(tmp_path / "project"),
            "source_id": "src_fixture",
            "transcript_version_id": "tr_child",
            "expected_revision": 4,
        },
    )
    assert cli_activation == mcp_activation

    monkeypatch.setattr(roughcut.cli, "read_transcript_versions", lambda *_a, **_k: versions)
    roughcut.cli.main(
        [
            "transcript-versions-read",
            "--project",
            str(tmp_path / "project"),
            "--source-id",
            "src_fixture",
            "--json",
        ]
    )
    cli_versions = json.loads(capsys.readouterr().out)
    monkeypatch.setattr(roughcut.mcp, "read_transcript_versions", lambda *_a, **_k: versions)
    mcp_versions = _mcp_call(
        "transcript_versions_read",
        {"project_path": str(tmp_path / "project"), "source_id": "src_fixture"},
    )
    assert cli_versions == mcp_versions


def test_cli_and_mcp_failures_are_versioned_and_quiet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(*_args: object, **_kwargs: object) -> TranscriptMutation:
        raise ProjectError("fixture failure")

    monkeypatch.setattr(roughcut.cli, "correct_transcript", fail)
    with pytest.raises(SystemExit) as error:
        roughcut.cli.main(
            [
                "transcript-correct",
                "--project",
                str(tmp_path / "project"),
                "--source-id",
                "src_fixture",
                "--parent-transcript-id",
                "tr_parent",
                "--corrections-json",
                "[]",
                "--expected-revision",
                "0",
                "--json",
            ]
        )
    streams = capsys.readouterr()
    assert error.value.code == 2
    assert streams.err == ""
    assert len(streams.out.splitlines()) == 1
    payload = json.loads(streams.out)
    assert payload["tool_schema_version"] == 32
    assert payload["error"] == {"code": "transcript_version_operation_failed"}

    monkeypatch.setattr(roughcut.mcp, "correct_transcript", fail)
    mcp = _mcp_call(
        "transcript_correct",
        {
            "project_path": str(tmp_path / "project"),
            "source_id": "src_fixture",
            "parent_transcript_version_id": "tr_parent",
            "corrections": [],
            "expected_revision": 0,
        },
    )
    assert mcp["ok"] is False
    assert mcp["error"] == {"code": "transcript_version_operation_failed"}


def test_cli_and_mcp_missing_arguments_share_invalid_input(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        roughcut.cli.main(
            [
                "transcript-correct",
                "--project",
                str(tmp_path / "project"),
                "--source-id",
                "src_fixture",
                "--parent-transcript-id",
                "tr_parent",
                "--expected-revision",
                "0",
                "--json",
            ]
        )
    streams = capsys.readouterr()
    assert error.value.code == 2
    assert streams.err == ""
    cli_payload = json.loads(streams.out)
    mcp_payload = _mcp_call(
        "transcript_correct",
        {
            "project_path": str(tmp_path / "project"),
            "source_id": "src_fixture",
            "parent_transcript_version_id": "tr_parent",
            "expected_revision": 0,
        },
    )
    assert cli_payload["error"] == mcp_payload["error"] == {"code": "invalid_input"}
