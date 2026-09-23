from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

import roughcut.cli
import roughcut.mcp
from roughcut.adapters.ffmpeg.render import (
    FFmpegRenderError,
    RenderCancelled,
)
from roughcut.adapters.ffmpeg.verify import RenderVerificationError
from roughcut.adapters.runtime_binding import RuntimeBindingError

_FAILURE_KEYS = {
    "schema_version",
    "tool_schema_version",
    "core_version",
    "source_commit",
    "platform",
    "ok",
    "error",
}
_SECRET = "/private/source.mp4 stderr token=secret"
_EXPORT_INPUT = {
    "schema_version": 1,
    "export_ref": {
        "artifact_id": "edit_failure",
        "schema_version": 1,
        "content_hash": "a" * 64,
    },
}


def _cli_arguments(start: str, project_path: Path) -> list[str]:
    if start == "transcribe_source":
        return [
            "transcribe-source",
            "--project",
            str(project_path),
            "--operation-id",
            "op_transcription_failure",
            "--source-id",
            "src_failure",
            "--expected-revision",
            "0",
            "--json",
        ]
    if start == "proxy_create":
        return [
            "proxy-create",
            "--project",
            str(project_path),
            "--operation-id",
            "op_proxy_failure",
            "--source-id",
            "src_failure",
            "--expected-revision",
            "0",
            "--json",
        ]
    return [
        "workflow-action",
        "--project",
        str(project_path),
        "--run-id",
        "wfr_failure",
        "--action-id",
        "act_export_failure",
        "--action",
        "approve_export",
        "--input-json",
        json.dumps(_EXPORT_INPUT),
        "--json",
    ]


def _mcp_arguments(start: str, project_path: Path) -> dict[str, object]:
    if start == "transcribe_source":
        return {
            "project_path": str(project_path),
            "operation_id": "op_transcription_failure",
            "source_id": "src_failure",
            "expected_revision": 0,
        }
    if start == "proxy_create":
        return {
            "project_path": str(project_path),
            "operation_id": "op_proxy_failure",
            "source_id": "src_failure",
            "expected_revision": 0,
        }
    return {
        "project_path": str(project_path),
        "run_id": "wfr_failure",
        "action_id": "act_export_failure",
        "action": "approve_export",
        "input": _EXPORT_INPUT,
    }


def _coordinator_name(start: str) -> str:
    return {
        "transcribe_source": "run_transcription_operation",
        "proxy_create": "run_proxy_operation",
        "approve_export": "run_approve_export_operation",
    }[start]


@pytest.mark.parametrize(
    ("start", "failure", "code"),
    (
        (
            "transcribe_source",
            RuntimeBindingError(_SECRET),
            "transcription_failed",
        ),
        (
            "transcribe_source",
            RuntimeError(_SECRET),
            "transcription_failed",
        ),
        (
            "transcribe_source",
            LookupError(_SECRET),
            "transcription_failed",
        ),
        (
            "proxy_create",
            RuntimeBindingError(_SECRET),
            "proxy_operation_failed",
        ),
        (
            "proxy_create",
            RuntimeError(_SECRET),
            "proxy_operation_failed",
        ),
        (
            "proxy_create",
            LookupError(_SECRET),
            "proxy_operation_failed",
        ),
        (
            "approve_export",
            RuntimeBindingError(_SECRET),
            "render_operation_failed",
        ),
        (
            "approve_export",
            FFmpegRenderError(_SECRET),
            "render_operation_failed",
        ),
        (
            "approve_export",
            RenderVerificationError(),
            "render_operation_failed",
        ),
        (
            "approve_export",
            RuntimeError(_SECRET),
            "render_operation_failed",
        ),
        (
            "approve_export",
            LookupError(_SECRET),
            "render_operation_failed",
        ),
        (
            "approve_export",
            RenderCancelled(_SECRET),
            "render_cancelled",
        ),
    ),
)
def test_tracked_cli_start_returns_closed_failure_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    start: str,
    failure: Exception,
    code: str,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> Any:
        raise failure

    monkeypatch.setattr(roughcut.cli, _coordinator_name(start), fail)
    with pytest.raises(SystemExit) as exit_info:
        roughcut.cli.main(_cli_arguments(start, tmp_path))

    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert exit_info.value.code == 2
    assert output.err == ""
    assert set(payload) == _FAILURE_KEYS
    assert payload["ok"] is False
    assert payload["error"] == {"code": code}
    assert "/private/" not in output.out
    assert "stderr" not in output.out
    assert "token=secret" not in output.out


@pytest.mark.parametrize(
    ("start", "failure", "code"),
    (
        (
            "transcribe_source",
            RuntimeBindingError(_SECRET),
            "transcription_failed",
        ),
        (
            "transcribe_source",
            RuntimeError(_SECRET),
            "transcription_failed",
        ),
        (
            "transcribe_source",
            LookupError(_SECRET),
            "transcription_failed",
        ),
        (
            "proxy_create",
            RuntimeBindingError(_SECRET),
            "proxy_operation_failed",
        ),
        (
            "proxy_create",
            RuntimeError(_SECRET),
            "proxy_operation_failed",
        ),
        (
            "proxy_create",
            LookupError(_SECRET),
            "proxy_operation_failed",
        ),
        (
            "approve_export",
            RuntimeBindingError(_SECRET),
            "render_operation_failed",
        ),
        (
            "approve_export",
            FFmpegRenderError(_SECRET),
            "render_operation_failed",
        ),
        (
            "approve_export",
            RenderVerificationError(),
            "render_operation_failed",
        ),
        (
            "approve_export",
            RuntimeError(_SECRET),
            "render_operation_failed",
        ),
        (
            "approve_export",
            LookupError(_SECRET),
            "render_operation_failed",
        ),
        (
            "approve_export",
            RenderCancelled(_SECRET),
            "render_cancelled",
        ),
    ),
)
def test_tracked_mcp_start_returns_closed_failure_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    start: str,
    failure: Exception,
    code: str,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> Any:
        raise failure

    monkeypatch.setattr(roughcut.mcp, _coordinator_name(start), fail)
    request = {
        "jsonrpc": "2.0",
        "id": "tracked-failure",
        "method": "tools/call",
        "params": {
            "name": start if start != "approve_export" else "workflow_action",
            "arguments": _mcp_arguments(start, tmp_path),
        },
    }
    monkeypatch.setattr(
        roughcut.mcp.sys,
        "stdin",
        io.StringIO(f"{json.dumps(request)}\n"),
    )
    roughcut.mcp.main()
    output = capsys.readouterr()
    response = json.loads(output.out)
    result = response["result"]
    assert isinstance(result, dict)
    assert result["isError"] is True
    payload = result["structuredContent"]
    assert isinstance(payload, dict)
    assert set(payload) == _FAILURE_KEYS
    assert payload["ok"] is False
    assert payload["error"] == {"code": code}
    encoded = json.dumps(response)
    assert output.err == ""
    assert "/private/" not in encoded
    assert "stderr" not in encoded
    assert "token=secret" not in encoded


@pytest.mark.parametrize(
    "start",
    ("transcribe_source", "proxy_create", "approve_export"),
)
def test_tracked_start_does_not_catch_system_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    start: str,
) -> None:
    def exit_process(*_args: object, **_kwargs: object) -> Any:
        raise SystemExit(73)

    coordinator = _coordinator_name(start)
    monkeypatch.setattr(roughcut.cli, coordinator, exit_process)
    with pytest.raises(SystemExit) as cli_exit:
        roughcut.cli.main(_cli_arguments(start, tmp_path))
    assert cli_exit.value.code == 73
    assert capsys.readouterr() == ("", "")

    monkeypatch.setattr(roughcut.mcp, coordinator, exit_process)
    with pytest.raises(SystemExit) as mcp_exit:
        roughcut.mcp.handle_request(
            {
                "jsonrpc": "2.0",
                "id": "tracked-system-exit",
                "method": "tools/call",
                "params": {
                    "name": (
                        start
                        if start != "approve_export"
                        else "workflow_action"
                    ),
                    "arguments": _mcp_arguments(start, tmp_path),
                },
            }
        )
    assert mcp_exit.value.code == 73
    assert capsys.readouterr() == ("", "")
