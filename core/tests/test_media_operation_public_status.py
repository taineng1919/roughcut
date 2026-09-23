from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from roughcut.adapters.media_operation_store import MediaOperationStore
from roughcut.adapters.project_store import ProjectStore
from roughcut.application.health import TOOL_SCHEMA_VERSION
from roughcut.application.projects import create_project
from roughcut.domain.media_operation import (
    RUNNING_PHASES,
    MediaOperationRecord,
)
from roughcut.mcp import handle_request

ROOT = Path(__file__).resolve().parents[1]


def _running_record(
    store: MediaOperationStore,
    operation_id: str,
    operation_type: str,
) -> MediaOperationRecord:
    return MediaOperationRecord(
        operation_id=operation_id,
        scope=store.scope,
        operation_type=operation_type,
        request_hash="a" * 64,
        input_hash="b" * 64,
        status="running",
        phase_message_code=RUNNING_PHASES[operation_type][0],
        created_at="2026-07-29T08:00:00.000000Z",
        started_at="2026-07-29T08:00:01.000000Z",
        updated_at="2026-07-29T08:00:01.000000Z",
        finished_at=None,
        result_ref=None,
        error=None,
    )


def _cli_status(
    project: Path,
    operation_id: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "roughcut.cli",
            "media-operation-status",
            "--project",
            str(project),
            "--operation-id",
            operation_id,
            "--json",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _mcp_status(project: Path, operation_id: str) -> dict[str, object]:
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": operation_id,
            "method": "tools/call",
            "params": {
                "name": "media_operation_status",
                "arguments": {
                    "project_path": str(project),
                    "operation_id": operation_id,
                },
            },
        }
    )
    assert response is not None
    result = response["result"]
    assert isinstance(result, dict)
    payload = result["structuredContent"]
    assert isinstance(payload, dict)
    return payload


@pytest.mark.parametrize(
    "operation_type",
    ("transcribe_source", "proxy_create", "approve_export"),
)
def test_cli_and_mcp_read_the_same_live_media_operation(
    tmp_path: Path,
    operation_type: str,
) -> None:
    root = tmp_path / operation_type
    project = create_project(root, "Status")
    store = MediaOperationStore(root, project.project_id)
    operation_id = f"op_{operation_type}"
    record = _running_record(store, operation_id, operation_type)

    with store.writer(operation_id, create=True) as acquired:
        assert acquired
        store.write_locked(record)
        cli = _cli_status(root, operation_id)
        assert cli.returncode == 0
        assert cli.stderr == ""
        cli_payload = json.loads(cli.stdout)
        mcp_payload = _mcp_status(root, operation_id)

    assert cli_payload == mcp_payload
    assert cli_payload["tool_schema_version"] == TOOL_SCHEMA_VERSION == 32
    assert cli_payload["media_operation"] == record.to_dict()
    assert set(cli_payload) == {
        "schema_version",
        "tool_schema_version",
        "core_version",
        "source_commit",
        "platform",
        "ok",
        "media_operation",
    }


def test_public_status_converges_abandoned_writer_without_scanning_or_cleanup(
    tmp_path: Path,
) -> None:
    root = tmp_path / "abandoned"
    project = create_project(root, "Status")
    store = MediaOperationStore(root, project.project_id)
    operation_id = "op_abandoned_public"
    running = _running_record(
        store, operation_id, "approve_export"
    )
    with store.writer(operation_id, create=True) as acquired:
        assert acquired
        store.write_locked(running)

    staging = root / "workflow" / "export-staging" / "owned_staging"
    staging.mkdir(parents=True)
    (staging / "owner.json").write_bytes(b"uninspected-owner")
    receipt = root / "workflow" / "receipts" / "untrusted.json"
    receipt.parent.mkdir()
    receipt.write_bytes(b"uninspected-receipt")
    project_before = (root / "project.json").read_bytes()

    cli = _cli_status(root, operation_id)
    assert cli.returncode == 0
    payload = json.loads(cli.stdout)
    operation = payload["media_operation"]
    assert operation["status"] == "interrupted"
    assert operation["result_ref"] is None
    assert operation["error"] == {
        "code": "media_operation_interrupted",
        "responsibility": "roughcut_core",
        "action": "recover_abandoned_media_operation",
        "message_code": "render_interrupted",
    }
    assert (root / "project.json").read_bytes() == project_before
    assert (staging / "owner.json").read_bytes() == b"uninspected-owner"
    assert receipt.read_bytes() == b"uninspected-receipt"
    assert _mcp_status(root, operation_id)["media_operation"] == operation
    assert ProjectStore(root).load().revision == 0


def test_public_status_errors_and_mcp_schema_are_closed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "missing"
    create_project(root, "Status")

    missing = _cli_status(root, "op_missing")
    assert missing.returncode == 2
    assert json.loads(missing.stdout)["error"] == {
        "code": "operation_not_found"
    }
    assert not (root / "workflow").exists()

    unsafe = _cli_status(root, "../escape")
    assert unsafe.returncode == 2
    assert json.loads(unsafe.stdout)["error"] == {
        "code": "operation_integrity_error"
    }
    assert not (root / "workflow").exists()

    listed = handle_request(
        {
            "jsonrpc": "2.0",
            "id": "list",
            "method": "tools/list",
            "params": {},
        }
    )
    assert listed is not None
    result = listed["result"]
    assert isinstance(result, dict)
    assert result["toolSchemaVersion"] == TOOL_SCHEMA_VERSION == 32
    tool = next(
        item
        for item in result["tools"]
        if item["name"] == "media_operation_status"
    )
    assert tool["inputSchema"] == {
        "type": "object",
        "properties": {
            "project_path": {"type": "string", "minLength": 1},
            "operation_id": {
                "type": "string",
                "pattern": "^[A-Za-z0-9_-]{1,128}$",
            },
        },
        "required": ["project_path", "operation_id"],
        "additionalProperties": False,
    }

    unexpected = handle_request(
        {
            "jsonrpc": "2.0",
            "id": "unexpected",
            "method": "tools/call",
            "params": {
                "name": "media_operation_status",
                "arguments": {
                    "project_path": str(root),
                    "operation_id": "op_missing",
                    "payload": {},
                },
            },
        }
    )
    assert unexpected is not None
    unexpected_result = unexpected["result"]
    assert isinstance(unexpected_result, dict)
    assert unexpected_result["isError"] is True
    assert unexpected_result["structuredContent"]["error"] == {
        "code": "invalid_arguments"
    }
