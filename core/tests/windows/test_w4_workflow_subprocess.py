from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from windows.w4_support import (
    McpChild,
    run_cli_raw,
    seed_workflow_project,
    structured_payload,
    tool_call,
)


def _mcp_payload(child: McpChild, request_id: str, name: str, arguments: dict[str, object]) -> dict[str, Any]:
    child.send(tool_call(request_id, name, arguments))
    response = child.read_json()
    assert response["id"] == request_id
    return structured_payload(response)


def test_cli_and_mcp_share_workflow_truth_across_action_cancel_and_restart(
    tmp_path: Path,
) -> None:
    project_path = seed_workflow_project(
        tmp_path / "中文 workflow project with spaces"
    )
    if os.name == "nt":
        assert project_path.drive
        assert ":" in str(project_path)

    started = run_cli_raw(
        "workflow-start",
        "--project",
        str(project_path),
        "--run-id",
        "wfr_w4",
        "--ordered-source-ids-json",
        '["src_w4"]',
        "--json",
    )
    assert started["workflow_run"]["stage"] == "scope_review"
    assert started["status"]["next_action"] == "approve_scope"

    with McpChild.start() as child:
        child.send(
            {
                "jsonrpc": "2.0",
                "id": "initialize",
                "method": "initialize",
                "params": {},
            }
        )
        assert child.read_json()["id"] == "initialize"

        mcp_status = _mcp_payload(
            child,
            "status-before",
            "workflow_status",
            {"project_path": str(project_path), "run_id": "wfr_w4"},
        )
        cli_status = run_cli_raw(
            "workflow-status",
            "--project",
            str(project_path),
            "--run-id",
            "wfr_w4",
            "--json",
        )
        assert mcp_status == cli_status

        action_input = {
            "schema_version": 1,
            "confirmation_basis": cli_status["status"]["confirmation_bases"][
                "scope"
            ]["basis"],
            "source_authorizations": [
                {
                    "source_id": "src_w4",
                    "transcribe": False,
                    "speaker_diarization": False,
                }
            ],
        }
        mcp_action = _mcp_payload(
            child,
            "scope",
            "workflow_action",
            {
                "project_path": str(project_path),
                "run_id": "wfr_w4",
                "action_id": "act_scope_w4",
                "action": "approve_scope",
                "input": action_input,
            },
        )
        assert mcp_action["receipt"]["action"] == "approve_scope"
        assert mcp_action["status"]["workflow_run"]["stage"] == "scope_review"
        assert mcp_action["status"]["approval_statuses"]["scope"] == "current"

        cli_replay = run_cli_raw(
            "workflow-action",
            "--project",
            str(project_path),
            "--run-id",
            "wfr_w4",
            "--action-id",
            "act_scope_w4",
            "--action",
            "approve_scope",
            "--input-json",
            json.dumps(action_input, ensure_ascii=False, sort_keys=True),
            "--json",
        )
        assert cli_replay == mcp_action

        project_before_stale = project_path.joinpath("project.json").read_bytes()
        run_before_stale = project_path.joinpath(
            "workflow", "runs", "wfr_w4.json"
        ).read_bytes()
        updated = run_cli_raw(
            "source-metadata-update",
            "--project",
            str(project_path),
            "--source-id",
            "src_w4",
            "--display-name",
            "更新 source.wav",
            "--tags-json",
            '["更新"]',
            "--note",
            "stale basis fixture",
            "--expected-revision",
            "1",
            "--json",
        )
        assert updated["ok"] is True

        stale_cli = run_cli_raw(
            "workflow-action",
            "--project",
            str(project_path),
            "--run-id",
            "wfr_w4",
            "--action-id",
            "act_stale_cli",
            "--action",
            "approve_scope",
            "--input-json",
            json.dumps(action_input, ensure_ascii=False, sort_keys=True),
            "--json",
            expected_returncode=2,
        )
        assert stale_cli["error"] == {"code": "workflow_subject_mismatch"}
        stale_mcp = _mcp_payload(
            child,
            "stale-mcp",
            "workflow_action",
            {
                "project_path": str(project_path),
                "run_id": "wfr_w4",
                "action_id": "act_stale_mcp",
                "action": "approve_scope",
                "input": action_input,
            },
        )
        assert stale_mcp == stale_cli
        assert project_path.joinpath("project.json").read_bytes() != project_before_stale
        assert project_path.joinpath(
            "workflow", "runs", "wfr_w4.json"
        ).read_bytes() == run_before_stale

        mcp_cancel = _mcp_payload(
            child,
            "cancel",
            "workflow_cancel",
            {
                "project_path": str(project_path),
                "run_id": "wfr_w4",
                "action_id": "act_cancel_w4",
            },
        )
        assert mcp_cancel["workflow_run"]["lifecycle"] == "canceled"

        child.close_stdin()
        returncode, stderr = child.wait()
        assert returncode == 0
        assert stderr == b""

    cli_cancel_replay = run_cli_raw(
        "workflow-cancel",
        "--project",
        str(project_path),
        "--run-id",
        "wfr_w4",
        "--action-id",
        "act_cancel_w4",
        "--json",
    )
    assert cli_cancel_replay["receipt"] == mcp_cancel["receipt"]
    assert cli_cancel_replay["status"] == mcp_cancel["status"]

    with McpChild.start() as restarted:
        readback = _mcp_payload(
            restarted,
            "status-after-restart",
            "workflow_status",
            {"project_path": str(project_path), "run_id": "wfr_w4"},
        )
        assert readback["status"] == cli_cancel_replay["status"]
        assert readback["status"]["workflow_run"]["lifecycle"] == "canceled"
        restarted.close_stdin()
        returncode, stderr = restarted.wait()
        assert returncode == 0
        assert stderr == b""


def test_cli_and_mcp_return_the_same_no_active_workflow_error(tmp_path: Path) -> None:
    project_path = seed_workflow_project(
        tmp_path / "中文 no active project with spaces"
    )
    cli_error = run_cli_raw(
        "workflow-status",
        "--project",
        str(project_path),
        "--json",
        expected_returncode=2,
    )
    assert cli_error["error"] == {"code": "workflow_required"}

    with McpChild.start() as child:
        mcp_error = _mcp_payload(
            child,
            "missing",
            "workflow_status",
            {"project_path": str(project_path)},
        )
        assert mcp_error == cli_error
        child.close_stdin()
        returncode, stderr = child.wait()
        assert returncode == 0
        assert stderr == b""
