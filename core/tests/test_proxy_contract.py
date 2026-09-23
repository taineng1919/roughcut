from __future__ import annotations

import json
from pathlib import Path

import pytest

import roughcut.cli
import roughcut.mcp
from roughcut.adapters.ffmpeg.proxy import ProxyCancelled, ProxyUnsupported
from roughcut.application.health import TOOL_SCHEMA_VERSION
from roughcut.application.media_operations import MediaOperationOutcome
from roughcut.application.proxies import ProxyResult, ProxyState
from roughcut.domain.media_operation import (
    MediaOperationError,
    MediaOperationRecord,
    ProjectOperationScope,
)
from roughcut.domain.project import ProjectError


def _state(*, reused: bool = True) -> ProxyState:
    return ProxyState(
        source_id="src_fixture",
        cache_key="a" * 64,
        status="ready",
        reason=None,
        project_revision=7,
        reused=reused,
        proxy_relative_path=f"proxies/src_fixture/{'a' * 64}/proxy.mp4",
        manifest_relative_path=f"proxies/src_fixture/{'a' * 64}/manifest.json",
        summary={"duration_ticks": 120_000},
    )


def _mcp(name: str, arguments: dict[str, object]) -> dict[str, object]:
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


def _outcome() -> MediaOperationOutcome[ProxyResult]:
    return MediaOperationOutcome(
        MediaOperationRecord(
            operation_id="op_proxy_contract",
            scope=ProjectOperationScope("project", "d" * 64),
            operation_type="proxy_create",
            request_hash="a" * 64,
            input_hash="b" * 64,
            status="pending",
            phase_message_code="proxy_preparing",
            created_at="2026-07-29T00:00:00.000000Z",
            started_at=None,
            updated_at="2026-07-29T00:00:00.000000Z",
            finished_at=None,
            result_ref=None,
            error=None,
        ),
        None,
        True,
    )


def test_proxy_tools_increment_schema_and_cli_mcp_share_application_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert TOOL_SCHEMA_VERSION == 32
    assert {tool["name"] for tool in roughcut.mcp.TOOLS} >= {"proxy_create", "proxy_read"}
    captured: list[tuple[Path, str, str, int | None]] = []

    def create(
        project_path: Path,
        *,
        operation_id: str,
        source_id: str,
        expected_project_revision: int,
    ) -> MediaOperationOutcome[ProxyResult]:
        captured.append(
            (
                project_path,
                operation_id,
                source_id,
                expected_project_revision,
            )
        )
        return _outcome()

    def read(project_path: Path, *, source_id: str, expected_revision: int) -> ProxyState:
        captured.append((project_path, source_id, expected_revision))
        return _state()

    monkeypatch.setattr(roughcut.cli, "run_proxy_operation", create)
    monkeypatch.setattr(roughcut.cli, "read_proxy", read)
    roughcut.cli.main(
        [
            "proxy-create",
            "--project",
            str(tmp_path),
            "--operation-id",
            "op_proxy_contract",
            "--source-id",
            "src_fixture",
            "--expected-revision",
            "7",
            "--json",
        ]
    )
    cli_create = json.loads(capsys.readouterr().out)
    roughcut.cli.main(
        [
            "proxy-read",
            "--project",
            str(tmp_path),
            "--source-id",
            "src_fixture",
            "--expected-revision",
            "7",
            "--json",
        ]
    )
    cli_read = json.loads(capsys.readouterr().out)

    monkeypatch.setattr(roughcut.mcp, "run_proxy_operation", create)
    monkeypatch.setattr(roughcut.mcp, "read_proxy", read)
    mcp_create = _mcp(
        "proxy_create",
        {
            "project_path": str(tmp_path),
            "operation_id": "op_proxy_contract",
            "source_id": "src_fixture",
            "expected_revision": 7,
        },
    )
    mcp_read = _mcp(
        "proxy_read",
        {"project_path": str(tmp_path), "source_id": "src_fixture", "expected_revision": 7},
    )

    assert cli_create == mcp_create
    assert cli_read == mcp_read
    assert cli_create["proxy"] is None
    assert cli_create["operation_readback"] is True
    assert cli_read["proxy"]["status"] == "ready"
    assert cli_read["proxy"]["reused"] is True
    assert captured == [
        (tmp_path, "op_proxy_contract", "src_fixture", 7),
        (tmp_path, "src_fixture", 7),
        (tmp_path, "op_proxy_contract", "src_fixture", 7),
        (tmp_path, "src_fixture", 7),
    ]


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (
            MediaOperationError(
                "operation_input_conflict",
                "fixture conflict",
            ),
            "operation_input_conflict",
        ),
        (ProjectError("fixture"), "proxy_operation_failed"),
        (ProxyCancelled("fixture"), "proxy_cancelled"),
        (ProxyUnsupported("fixture"), "proxy_unsupported"),
    ],
)
def test_proxy_cli_and_mcp_use_stable_versioned_error_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: Exception,
    code: str,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> MediaOperationOutcome[ProxyResult]:
        raise failure

    monkeypatch.setattr(roughcut.cli, "run_proxy_operation", fail)
    with pytest.raises(SystemExit) as exit_info:
        roughcut.cli.main(
            [
                "proxy-create",
                "--project",
                str(tmp_path),
                "--operation-id",
                "op_proxy_contract",
                "--source-id",
                "src_fixture",
                "--expected-revision",
                "7",
                "--json",
            ]
        )
    output = capsys.readouterr()
    assert exit_info.value.code == 2
    assert output.err == ""
    assert json.loads(output.out)["error"] == {"code": code}

    monkeypatch.setattr(roughcut.mcp, "run_proxy_operation", fail)
    assert _mcp(
        "proxy_create",
        {
            "project_path": str(tmp_path),
            "operation_id": "op_proxy_contract",
            "source_id": "src_fixture",
            "expected_revision": 7,
        },
    )["error"] == {"code": code}


def test_proxy_missing_arguments_are_invalid_arguments_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        roughcut.cli.main(["proxy-create", "--json"])
    output = capsys.readouterr()
    assert exit_info.value.code == 2
    assert output.err == ""
    assert json.loads(output.out)["error"] == {"code": "invalid_arguments"}

    assert _mcp("proxy_create", {})["error"] == {"code": "invalid_arguments"}
    assert _mcp(
        "proxy_read",
        {
            "project_path": "/fixture",
            "source_id": "src_fixture",
            "expected_revision": "7",
        },
    )["error"] == {"code": "invalid_arguments"}
