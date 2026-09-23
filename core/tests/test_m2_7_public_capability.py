from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from roughcut import cli, m2_7_public_capability, mcp

PUBLIC_CASES = (
    (
        "align-multicam",
        "align_multicam",
        "run_align_multicam",
        "alignment_runtime_unavailable",
    ),
    (
        "multicam-parallel-render-prepare",
        "multicam_parallel_render_prepare",
        "prepare_multicam_parallel_render",
        "parallel_render_runtime_unavailable",
    ),
    (
        "multicam-parallel-render-start",
        "multicam_parallel_render_start",
        "start_multicam_parallel_render",
        "parallel_render_runtime_unavailable",
    ),
)


def _cli_arguments(command: str, project_path: Path) -> list[str]:
    common = [command, "--project", str(project_path)]
    if command == "align-multicam":
        return common + [
            "--operation-id",
            "op_platform_guard_alignment",
            "--alignment-id",
            "aln_platform_guard",
            "--expected-revision",
            "1",
            "--main-camera-json",
            '{"camera_id":"main","ordered_source_ids":["src_main"]}',
            "--auxiliary-cameras-json",
            '[{"camera_id":"aux","ordered_source_ids":["src_aux"]}]',
            "--main-audio-stable",
            "true",
            "--max-temporary-disk-bytes",
            "1",
            "--max-analysis-memory-bytes",
            "1",
            "--max-runtime-seconds",
            "1",
            "--json",
        ]
    if command == "multicam-parallel-render-prepare":
        return common + [
            "--edit-version-id",
            "edit_platform_guard",
            "--alignment-ref-json",
            '{"kind":"multicam_alignment"}',
            "--auxiliary-camera-ids-json",
            '["aux"]',
            "--expected-revision",
            "1",
            "--json",
        ]
    return common + [
        "--operation-id",
        "op_platform_guard_parallel",
        "--prepare-ref-json",
        '{"kind":"multicam_parallel_prepare"}',
        "--json",
    ]


def _mcp_arguments(tool_name: str, project_path: Path) -> dict[str, Any]:
    common: dict[str, Any] = {"project_path": str(project_path)}
    if tool_name == "align_multicam":
        return {
            **common,
            "operation_id": "op_platform_guard_alignment",
            "alignment_id": "aln_platform_guard",
            "expected_revision": 1,
            "main_camera": {
                "camera_id": "main",
                "ordered_source_ids": ["src_main"],
            },
            "auxiliary_cameras": [
                {"camera_id": "aux", "ordered_source_ids": ["src_aux"]}
            ],
            "main_audio_stable": True,
            "max_temporary_disk_bytes": 1,
            "max_analysis_memory_bytes": 1,
            "max_runtime_seconds": 1,
        }
    if tool_name == "multicam_parallel_render_prepare":
        return {
            **common,
            "edit_version_id": "edit_platform_guard",
            "alignment_ref": {"kind": "multicam_alignment"},
            "auxiliary_camera_ids": ["aux"],
            "expected_revision": 1,
        }
    return {
        **common,
        "operation_id": "op_platform_guard_parallel",
        "prepare_ref": {"kind": "multicam_parallel_prepare"},
    }


def _mcp_request(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": f"{tool_name}-platform-guard",
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }


def _replace_cli_option(arguments: list[str], option: str, value: str) -> list[str]:
    updated = list(arguments)
    updated[updated.index(option) + 1] = value
    return updated


def _invalid_cli_type(command: str, project_path: Path) -> list[str]:
    option = {
        "align-multicam": "--main-camera-json",
        "multicam-parallel-render-prepare": "--alignment-ref-json",
        "multicam-parallel-render-start": "--prepare-ref-json",
    }[command]
    return _replace_cli_option(_cli_arguments(command, project_path), option, "[]")


def _invalid_mcp_type(tool_name: str, project_path: Path) -> dict[str, Any]:
    arguments = _mcp_arguments(tool_name, project_path)
    key = {
        "align_multicam": "main_camera",
        "multicam_parallel_render_prepare": "alignment_ref",
        "multicam_parallel_render_start": "prepare_ref",
    }[tool_name]
    arguments[key] = []
    return arguments


def _expected_error(code: str) -> dict[str, object]:
    payload = cli.health()
    payload["ok"] = False
    payload["error"] = {"code": code}
    return payload


@pytest.mark.parametrize(
    ("command", "tool_name", "dispatcher_name", "error_code"), PUBLIC_CASES
)
def test_windows_cli_and_mcp_fail_closed_before_application_or_project_touch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    tool_name: str,
    dispatcher_name: str,
    error_code: str,
) -> None:
    monkeypatch.setattr(m2_7_public_capability.platform, "system", lambda: "Windows")
    application_calls: list[str] = []

    def forbidden_application(*_args: object, **_kwargs: object) -> object:
        application_calls.append(dispatcher_name)
        raise AssertionError("public guard must reject before application dispatch")

    monkeypatch.setattr(cli, dispatcher_name, forbidden_application)
    monkeypatch.setattr(mcp, dispatcher_name, forbidden_application)
    untouched_project = tmp_path / "project-must-not-be-created"

    with pytest.raises(SystemExit) as cli_exit:
        cli.main(_cli_arguments(command, untouched_project))
    assert cli_exit.value.code == 2
    cli_payload = json.loads(capsys.readouterr().out)
    expected = _expected_error(error_code)
    assert cli_payload == expected

    response = mcp.handle_request(
        _mcp_request(tool_name, _mcp_arguments(tool_name, untouched_project))
    )
    assert response == {
        "jsonrpc": "2.0",
        "id": f"{tool_name}-platform-guard",
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(expected, ensure_ascii=False),
                }
            ],
            "structuredContent": expected,
            "isError": True,
        },
    }
    assert application_calls == []
    assert not untouched_project.exists()


@pytest.mark.parametrize(
    ("command", "tool_name", "_dispatcher_name", "_error_code"), PUBLIC_CASES
)
def test_invalid_cli_and_mcp_requests_precede_the_windows_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    tool_name: str,
    _dispatcher_name: str,
    _error_code: str,
) -> None:
    monkeypatch.setattr(m2_7_public_capability.platform, "system", lambda: "Windows")
    project_path = tmp_path / "invalid-request-project"
    invalid_cli_shape = _cli_arguments(command, project_path) + [
        "--source-id",
        "unexpected",
    ]
    for invalid_cli in (_invalid_cli_type(command, project_path), invalid_cli_shape):
        with pytest.raises(SystemExit) as cli_exit:
            cli.main(invalid_cli)
        assert cli_exit.value.code == 2
        assert json.loads(capsys.readouterr().out) == _expected_error(
            "invalid_arguments"
        )

    invalid_mcp_shape = _mcp_arguments(tool_name, project_path)
    invalid_mcp_shape["unexpected"] = True
    for invalid_mcp in (_invalid_mcp_type(tool_name, project_path), invalid_mcp_shape):
        response = mcp.handle_request(_mcp_request(tool_name, invalid_mcp))
        assert response is not None
        assert response["result"]["structuredContent"] == _expected_error(
            "invalid_arguments"
        )
    assert not project_path.exists()


class ApplicationReached(RuntimeError):
    pass


@pytest.mark.parametrize(
    ("command", "tool_name", "dispatcher_name", "_error_code"), PUBLIC_CASES
)
def test_macos_cli_and_mcp_reach_the_existing_application_dispatchers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    tool_name: str,
    dispatcher_name: str,
    _error_code: str,
) -> None:
    monkeypatch.setattr(m2_7_public_capability.platform, "system", lambda: "Darwin")

    def reached(label: str) -> Callable[..., object]:
        def dispatch(*_args: object, **_kwargs: object) -> object:
            raise ApplicationReached(label)

        return dispatch

    project_path = tmp_path / "macos-dispatch-project"
    monkeypatch.setattr(cli, dispatcher_name, reached("cli"))
    with pytest.raises(ApplicationReached, match="cli"):
        cli.main(_cli_arguments(command, project_path))

    monkeypatch.setattr(mcp, dispatcher_name, reached("mcp"))
    with pytest.raises(ApplicationReached, match="mcp"):
        mcp.handle_request(_mcp_request(tool_name, _mcp_arguments(tool_name, project_path)))


def test_guard_rejects_unknown_entry_without_platform_or_environment_fallback() -> None:
    with pytest.raises(ValueError, match="unknown M2.7 public entry"):
        m2_7_public_capability.require_m2_7_public_capability("unknown")
