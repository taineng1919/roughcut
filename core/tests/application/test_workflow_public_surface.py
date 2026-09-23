from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import roughcut.cli
from roughcut.adapters.artifact_store import write_new_json
from roughcut.adapters.project_store import ProjectStore
from roughcut.application.projects import create_project
from roughcut.application.proposals import ProposalRejection
from roughcut.domain.asr import ASR_CLOUD_TAG
from roughcut.domain.project import (
    ImportMode,
    MediaProbe,
    SourceAsset,
    SourceFingerprint,
)
from roughcut.domain.transcript import (
    TimedTranscript,
    TranscriptProvenance,
    TranscriptSegment,
)
from roughcut.mcp import TOOLS, handle_request


def _project(tmp_path: Path, *, with_source: bool) -> Path:
    root = tmp_path / "public-workflow"
    project = create_project(root, "Public workflow")
    if not with_source:
        return root
    source = SourceAsset(
        source_id="src_public",
        kind="audio",
        display_name="public.wav",
        import_mode=ImportMode.LINKED,
        locator={"absolute_path": "/fixture/public.wav"},
        fingerprint=SourceFingerprint(100, 1, "fixture-public"),
        probe=MediaProbe(
            duration_ticks=120_000,
            container_start_ticks=0,
            first_content_ticks=0,
            video_codec=None,
            width=None,
            height=None,
            nominal_frame_rate=None,
            is_vfr=False,
            audio_codec="pcm_s16le",
            audio_sample_rate=16_000,
            rotation_degrees=0,
        ),
    )
    transcript = TimedTranscript(
        schema_version=1,
        transcript_version_id="tr_public",
        source_id=source.source_id,
        parent_version_id=None,
        provenance=TranscriptProvenance(
            backend="fixture",
            package_version="fixture",
            models={},
            parameters={},
            raw_result_path="raw-asr/src_public/fixture.json",
            started_at="fixture",
            completed_at="fixture",
            exit_status=0,
        ),
        language="zh-CN",
        segments=(
            TranscriptSegment(
                segment_id="seg_public",
                start_ticks=0,
                end_ticks=120_000,
                original_text="公开工作流。",
                corrected_text=None,
                local_speaker_id=None,
                person_id=None,
                confidence=None,
                fine_units=(),
                editorial_mark="unmarked",
            ),
        ),
    )
    write_new_json(
        root / "transcripts" / source.source_id / "tr_public.json",
        transcript.to_dict(),
    )
    ProjectStore(root).save(
        replace(
            project,
            revision=1,
            sources=(source,),
            active_transcript_versions={source.source_id: "tr_public"},
        ),
        expected_revision=0,
    )
    return root


def _cli(*arguments: str) -> tuple[int, dict[str, Any]]:
    result = subprocess.run(
        [sys.executable, "-m", "roughcut.cli", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.stderr == ""
    return result.returncode, json.loads(result.stdout)


def _mcp(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": "public-contract",
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


def _normalized(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = json.loads(json.dumps(payload))

    def replace_times(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key.endswith("_at"):
                    value[key] = "<time>"
                else:
                    replace_times(item)
        elif isinstance(value, list):
            for item in value:
                replace_times(item)

    replace_times(normalized)
    return normalized


def _business_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and path.name not in {".project.lock", ".claim.lock"}
    }


def test_four_public_facades_share_exact_cli_mcp_payloads(tmp_path: Path) -> None:
    root = _project(tmp_path, with_source=True)
    mcp_root = tmp_path / "public-workflow-mcp"
    shutil.copytree(root, mcp_root)
    exit_code, started = _cli(
        "workflow-start",
        "--project",
        str(root),
        "--run-id",
        "wfr_public",
        "--ordered-source-ids-json",
        '["src_public"]',
        "--json",
    )
    assert exit_code == 0
    assert started["workflow_run"]["run_id"] == "wfr_public"
    mcp_started = _mcp(
        "workflow_start",
        {
            "project_path": str(mcp_root),
            "run_id": "wfr_public",
            "ordered_source_ids": ["src_public"],
        },
    )
    assert _normalized(started) == _normalized(mcp_started)

    exit_code, cli_status = _cli(
        "workflow-status",
        "--project",
        str(root),
        "--run-id",
        "wfr_public",
        "--json",
    )
    assert exit_code == 0
    assert cli_status == _mcp(
        "workflow_status",
        {"project_path": str(root), "run_id": "wfr_public"},
    )

    scope_basis = cli_status["status"]["confirmation_bases"]["scope"]["basis"]
    action_input = {
        "schema_version": 1,
        "confirmation_basis": scope_basis,
        "source_authorizations": [
            {
                "source_id": "src_public",
                "transcribe": False,
                "speaker_diarization": False,
            }
        ],
    }
    action_arguments = {
        "project_path": str(root),
        "run_id": "wfr_public",
        "action_id": "act_public_scope",
        "action": "approve_scope",
        "input": action_input,
    }
    mcp_action = _mcp("workflow_action", action_arguments)
    exit_code, cli_action = _cli(
        "workflow-action",
        "--project",
        str(root),
        "--run-id",
        "wfr_public",
        "--action-id",
        "act_public_scope",
        "--action",
        "approve_scope",
        "--input-json",
        json.dumps(action_input),
        "--json",
    )
    assert exit_code == 0
    assert cli_action == mcp_action
    assert cli_action["receipt"]["action"] == "approve_scope"

    exit_code, cli_cancel = _cli(
        "workflow-cancel",
        "--project",
        str(root),
        "--run-id",
        "wfr_public",
        "--action-id",
        "act_public_cancel",
        "--json",
    )
    assert exit_code == 0
    mcp_cancel = _mcp(
        "workflow_cancel",
        {
            "project_path": str(root),
            "run_id": "wfr_public",
            "action_id": "act_public_cancel",
        },
    )
    assert cli_cancel == mcp_cancel
    assert cli_cancel["workflow_run"]["lifecycle"] == "canceled"


def test_workflow_start_empty_scope_matches_cli_mcp_and_advertised_schema(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path, with_source=False)
    mcp_root = tmp_path / "empty-scope-mcp"
    shutil.copytree(root, mcp_root)
    exit_code, cli_payload = _cli(
        "workflow-start",
        "--project",
        str(root),
        "--run-id",
        "wfr_empty",
        "--ordered-source-ids-json",
        "[]",
        "--json",
    )
    assert exit_code == 0
    mcp_payload = _mcp(
        "workflow_start",
        {
            "project_path": str(mcp_root),
            "run_id": "wfr_empty",
            "ordered_source_ids": [],
        },
    )
    assert _normalized(cli_payload) == _normalized(mcp_payload)
    assert cli_payload["workflow_run"]["ordered_bindings"] == []

    schema = next(tool for tool in TOOLS if tool["name"] == "workflow_start")[
        "inputSchema"
    ]
    ordered = schema["properties"]["ordered_source_ids"]
    assert ordered["type"] == "array"
    assert "minItems" not in ordered


@pytest.mark.parametrize(
    ("cli_command", "mcp_tool"),
    (
        ("workflow-start", "workflow_start"),
        ("workflow-status", "workflow_status"),
        ("workflow-action", "workflow_action"),
        ("workflow-cancel", "workflow_cancel"),
    ),
)
def test_four_public_facades_share_exact_invalid_argument_payload(
    cli_command: str,
    mcp_tool: str,
) -> None:
    exit_code, cli_payload = _cli(cli_command, "--json")
    assert exit_code == 2
    assert cli_payload == _mcp(mcp_tool, {})
    assert cli_payload["error"]["code"] == "invalid_arguments"


def test_public_workflow_inputs_are_closed_and_reject_ambiguous_actions(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path, with_source=True)
    _mcp(
        "workflow_start",
        {
            "project_path": str(root),
            "run_id": "wfr_closed",
            "ordered_source_ids": ["src_public"],
        },
    )
    for action in ("继续", "approve_scope,confirm_brief"):
        payload = _mcp(
            "workflow_action",
            {
                "project_path": str(root),
                "run_id": "wfr_closed",
                "action_id": f"act_{len(action)}",
                "action": action,
                "input": {},
            },
        )
        assert payload["error"]["code"] == "workflow_action_invalid"
    payload = _mcp(
        "workflow_action",
        {
            "project_path": str(root),
            "run_id": "wfr_closed",
            "action_id": "act_approval_token",
            "action": "approve_scope",
            "input": {},
            "approval_id": "not-a-capability",
        },
    )
    assert payload["error"]["code"] == "invalid_arguments"


def test_stage_five_fake_agent_old_schema_and_wrong_order_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path, with_source=True)
    started = _mcp(
        "workflow_start",
        {
            "project_path": str(root),
            "run_id": "wfr_adversarial",
            "ordered_source_ids": ["src_public"],
        },
    )
    assert started["ok"] is True
    health = _mcp("health", {})
    assert health["tool_schema_version"] == 32
    before = _business_snapshot(root)
    render_calls = 0
    transcribe_calls = 0

    def forbidden_render(*args: object, **kwargs: object) -> object:
        nonlocal render_calls
        del args, kwargs
        render_calls += 1
        raise AssertionError("Roughcut renderer must not start for an early-stage request")

    monkeypatch.setattr("roughcut.mcp.render_roughcut", forbidden_render)

    def forbidden_transcribe(*args: object, **kwargs: object) -> object:
        nonlocal transcribe_calls
        del args, kwargs
        transcribe_calls += 1
        raise AssertionError("Roughcut ASR must not start before scope approval")

    monkeypatch.setattr(
        "roughcut.application.media_operations.transcribe_source",
        forbidden_transcribe,
    )
    errors: list[dict[str, Any]] = []

    exit_code, ambiguous = _cli(
        "workflow-action",
        "--project",
        str(root),
        "--run-id",
        "wfr_adversarial",
        "--action-id",
        "act_ambiguous",
        "--action",
        "继续",
        "--input-json",
        "{}",
        "--json",
    )
    errors.append(ambiguous)
    assert exit_code == 2
    assert ambiguous["error"]["code"] == "workflow_action_invalid"

    unknown = _mcp(
        "workflow_action",
        {
            "project_path": str(root),
            "run_id": "wfr_adversarial",
            "action_id": "act_unknown",
            "action": "workflow_continue",
            "input": {},
        },
    )
    errors.append(unknown)
    assert unknown["error"]["code"] == "workflow_action_invalid"

    for tool_name, arguments in (
        (
            "transcribe_source",
            {
                "operation_id": "op_adversarial",
                "source_id": "src_public",
                "expected_revision": 1,
            },
        ),
        (
            "brief_create",
            {
                "theme": "越级",
                "target_duration_ticks": 120_000,
                "focus": ["越级"],
                "allow_reorder": False,
                "expected_revision": 1,
            },
        ),
        (
            "content_draft_confirm",
            {"content_draft_id": "draft_none", "expected_revision": 1},
        ),
        (
            "proposal_confirm",
            {"proposal_id": "proposal_none", "expected_revision": 1},
        ),
        (
            "render_roughcut",
            {"edit_version_id": "edit_none", "expected_revision": 1},
        ),
    ):
        rejected = _mcp(tool_name, {"project_path": str(root), **arguments})
        errors.append(rejected)
        expected_code = (
            "workflow_approval_required"
            if tool_name == "transcribe_source"
            else "workflow_transition_not_allowed"
        )
        assert rejected["error"]["code"] == expected_code

    for action, action_input in (
        (
            "adopt_roughcut",
            {
                "schema_version": 1,
                "proposal_ref": {
                    "artifact_id": "proposal_none",
                    "schema_version": 1,
                    "content_hash": "0" * 64,
                },
            },
        ),
        (
            "approve_export",
            {
                "schema_version": 1,
                "export_ref": {
                    "artifact_id": "edit_none",
                    "schema_version": 1,
                    "content_hash": "0" * 64,
                },
            },
        ),
    ):
        rejected = _mcp(
            "workflow_action",
            {
                "project_path": str(root),
                "run_id": "wfr_adversarial",
                "action_id": f"act_early_{action}",
                "action": action,
                "input": action_input,
            },
        )
        errors.append(rejected)
        assert rejected["error"]["code"] == "workflow_transition_not_allowed"

    serialized = json.dumps(errors, ensure_ascii=False)
    assert str(root) not in serialized
    assert "Traceback" not in serialized
    assert "token" not in serialized.lower()
    assert render_calls == 0
    assert transcribe_calls == 0
    assert _business_snapshot(root) == before


def test_stage_five_public_receipt_readback_and_action_id_conflict(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path, with_source=True)
    _mcp(
        "workflow_start",
        {
            "project_path": str(root),
            "run_id": "wfr_readback",
            "ordered_source_ids": ["src_public"],
        },
    )
    status = _mcp(
        "workflow_status",
        {"project_path": str(root), "run_id": "wfr_readback"},
    )
    action_input = {
        "source_authorizations": [
            {
                "speaker_diarization": False,
                "transcribe": False,
                "source_id": "src_public",
            }
        ],
        "confirmation_basis": status["status"]["confirmation_bases"]["scope"][
            "basis"
        ],
        "schema_version": 1,
    }
    first = _mcp(
        "workflow_action",
        {
            "project_path": str(root),
            "run_id": "wfr_readback",
            "action_id": "act_response_lost",
            "action": "approve_scope",
            "input": action_input,
        },
    )
    assert first["ok"] is True
    committed = _business_snapshot(root)

    exit_code, readback = _cli(
        "workflow-action",
        "--project",
        str(root),
        "--run-id",
        "wfr_readback",
        "--action-id",
        "act_response_lost",
        "--action",
        "approve_scope",
        "--input-json",
        json.dumps(action_input, ensure_ascii=False, sort_keys=True),
        "--json",
    )
    assert exit_code == 0
    assert readback["receipt"] == first["receipt"]
    assert _business_snapshot(root) == committed

    changed = json.loads(json.dumps(action_input))
    changed["source_authorizations"][0]["transcribe"] = True
    conflict = _mcp(
        "workflow_action",
        {
            "project_path": str(root),
            "run_id": "wfr_readback",
            "action_id": "act_response_lost",
            "action": "approve_scope",
            "input": changed,
        },
    )
    assert conflict["error"]["code"] == "workflow_action_conflict"
    assert _business_snapshot(root) == committed
    assert len(list((root / "workflow" / "receipts").glob("*.json"))) == 1


def test_stage_five_public_concurrent_scope_gate_commits_once(tmp_path: Path) -> None:
    root = _project(tmp_path, with_source=True)
    _mcp(
        "workflow_start",
        {
            "project_path": str(root),
            "run_id": "wfr_concurrent_scope",
            "ordered_source_ids": ["src_public"],
        },
    )
    status = _mcp(
        "workflow_status",
        {"project_path": str(root), "run_id": "wfr_concurrent_scope"},
    )
    action_input = {
        "schema_version": 1,
        "confirmation_basis": status["status"]["confirmation_bases"]["scope"][
            "basis"
        ],
        "source_authorizations": [
            {
                "source_id": "src_public",
                "transcribe": False,
                "speaker_diarization": False,
            }
        ],
    }
    start = threading.Barrier(2)

    def approve(action_id: str) -> dict[str, Any]:
        start.wait(timeout=5)
        return _mcp(
            "workflow_action",
            {
                "project_path": str(root),
                "run_id": "wfr_concurrent_scope",
                "action_id": action_id,
                "action": "approve_scope",
                "input": action_input,
            },
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(approve, ("act_scope_one", "act_scope_two"))
        )
    assert sum(result["ok"] is True for result in results) == 1
    rejected = next(result for result in results if result["ok"] is False)
    assert rejected["error"]["code"] == "workflow_subject_mismatch"
    assert len(list((root / "workflow" / "receipts").glob("*.json"))) == 1
    assert len(list((root / "workflow" / "approvals").glob("*.json"))) == 1


def test_stage_five_cross_project_basis_and_copied_run_fail_closed(
    tmp_path: Path,
) -> None:
    source_root = _project(tmp_path / "source", with_source=True)
    target_root = _project(tmp_path / "target", with_source=True)
    _mcp(
        "workflow_start",
        {
            "project_path": str(source_root),
            "run_id": "wfr_source",
            "ordered_source_ids": ["src_public"],
        },
    )
    _mcp(
        "workflow_start",
        {
            "project_path": str(target_root),
            "run_id": "wfr_target",
            "ordered_source_ids": ["src_public"],
        },
    )
    source_status = _mcp(
        "workflow_status",
        {"project_path": str(source_root), "run_id": "wfr_source"},
    )
    target_before = _business_snapshot(target_root)
    cross_basis = _mcp(
        "workflow_action",
        {
            "project_path": str(target_root),
            "run_id": "wfr_target",
            "action_id": "act_cross_basis",
            "action": "approve_scope",
            "input": {
                "schema_version": 1,
                "confirmation_basis": source_status["status"][
                    "confirmation_bases"
                ]["scope"]["basis"],
                "source_authorizations": [
                    {
                        "source_id": "src_public",
                        "transcribe": False,
                        "speaker_diarization": False,
                    }
                ],
            },
        },
    )
    assert cross_basis["error"]["code"] == "workflow_subject_mismatch"
    assert _business_snapshot(target_root) == target_before

    copied_run = target_root / "workflow" / "runs" / "wfr_source.json"
    shutil.copy2(
        source_root / "workflow" / "runs" / "wfr_source.json",
        copied_run,
    )
    copied_before = copied_run.read_bytes()
    copied = _mcp(
        "workflow_status",
        {"project_path": str(target_root), "run_id": "wfr_source"},
    )
    assert copied["error"]["code"] == "workflow_integrity_error"
    assert copied_run.read_bytes() == copied_before


def test_stage_five_public_stale_revision_and_scope_subject_are_zero_write(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path, with_source=True)
    _mcp(
        "workflow_start",
        {
            "project_path": str(root),
            "run_id": "wfr_stale_public",
            "ordered_source_ids": ["src_public"],
        },
    )
    status = _mcp(
        "workflow_status",
        {"project_path": str(root), "run_id": "wfr_stale_public"},
    )
    stale_basis = status["status"]["confirmation_bases"]["scope"]["basis"]
    updated = _mcp(
        "source_metadata_update",
        {
            "project_path": str(root),
            "source_id": "src_public",
            "display_name": "updated.wav",
            "tags": [ASR_CLOUD_TAG],
            "note": "updated",
            "expected_revision": 1,
        },
    )
    assert updated["ok"] is True
    assert ProjectStore(root).load().sources[0].tags == (ASR_CLOUD_TAG,)
    after_update = _business_snapshot(root)

    stale_subject = _mcp(
        "workflow_action",
        {
            "project_path": str(root),
            "run_id": "wfr_stale_public",
            "action_id": "act_stale_scope",
            "action": "approve_scope",
            "input": {
                "schema_version": 1,
                "confirmation_basis": stale_basis,
                "source_authorizations": [
                    {
                        "source_id": "src_public",
                        "transcribe": False,
                        "speaker_diarization": False,
                    }
                ],
            },
        },
    )
    assert stale_subject["error"]["code"] == "workflow_subject_mismatch"
    assert _business_snapshot(root) == after_update

    stale_revision = _mcp(
        "source_metadata_update",
        {
            "project_path": str(root),
            "source_id": "src_public",
            "display_name": "stale.wav",
            "tags": ["stale"],
            "note": "stale",
            "expected_revision": 1,
        },
    )
    assert stale_revision["error"]["code"] == "people_operation_failed"
    assert _business_snapshot(root) == after_update


def test_workflow_action_advertises_nine_action_specific_closed_schemas() -> None:
    tool: Any = next(item for item in TOOLS if item["name"] == "workflow_action")
    schema = tool["inputSchema"]
    expected_actions = {
        "approve_scope",
        "confirm_brief",
        "submit_outline",
        "approve_outline",
        "submit_draft",
        "approve_draft",
        "return_to_draft",
        "adopt_roughcut",
        "approve_export",
    }
    assert schema["type"] == "object"
    assert set(schema["properties"]) == {
        "project_path",
        "run_id",
        "action_id",
        "action",
        "input",
    }
    assert schema["required"] == [
        "project_path",
        "run_id",
        "action_id",
        "action",
        "input",
    ]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]["action"]["enum"]) == expected_actions
    branches = schema["oneOf"]
    assert len(branches) == 9
    assert {
        branch["properties"]["action"]["const"] for branch in branches
    } == expected_actions
    for branch in branches:
        assert branch["additionalProperties"] is False
        assert branch["properties"]["input"]["additionalProperties"] is False


def test_submit_outline_schema_advertises_non_empty_unbounded_sections() -> None:
    tool: Any = next(item for item in TOOLS if item["name"] == "workflow_action")
    branch = next(
        branch
        for branch in tool["inputSchema"]["oneOf"]
        if branch["properties"]["action"]["const"] == "submit_outline"
    )
    sections = branch["properties"]["input"]["properties"]["sections"]

    assert sections["type"] == "array"
    assert sections["minItems"] == 1
    assert "maxItems" not in sections


def test_media_start_tools_advertise_schema_25_closed_inputs() -> None:
    tools: dict[str, Any] = {
        tool["name"]: tool["inputSchema"] for tool in TOOLS
    }
    transcribe = tools["transcribe_source"]
    assert transcribe["required"] == [
        "project_path",
        "operation_id",
        "source_id",
        "expected_revision",
    ]
    assert set(transcribe["properties"]) == {
        "project_path",
        "operation_id",
        "source_id",
        "expected_revision",
        "speaker_diarization",
    }
    assert transcribe["additionalProperties"] is False

    proxy = tools["proxy_create"]
    assert proxy["required"] == [
        "project_path",
        "operation_id",
        "source_id",
        "expected_revision",
    ]
    assert set(proxy["properties"]) == set(proxy["required"])
    assert proxy["additionalProperties"] is False


def test_cli_approve_export_routes_action_id_to_tracked_coordinator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}
    sentinel = object()

    def run(
        project_path: Path,
        *,
        run_id: str,
        action_id: str,
        action_input: object,
    ) -> object:
        captured.update(
            {
                "project_path": project_path,
                "run_id": run_id,
                "action_id": action_id,
                "action_input": action_input,
            }
        )
        return sentinel

    monkeypatch.setattr(roughcut.cli, "run_approve_export_operation", run)
    monkeypatch.setattr(
        roughcut.cli,
        "_approve_export_operation_payload",
        lambda outcome: {"ok": outcome is sentinel},
    )
    root = tmp_path / "route"
    action_input = {
        "schema_version": 1,
        "export_ref": {
            "artifact_id": "edit_route",
            "schema_version": 1,
            "content_hash": "a" * 64,
        },
    }
    roughcut.cli.main(
        [
            "workflow-action",
            "--project",
            str(root),
            "--run-id",
            "wfr_route",
            "--action-id",
            "act_route",
            "--action",
            "approve_export",
            "--input-json",
            json.dumps(action_input),
            "--json",
        ]
    )

    assert json.loads(capsys.readouterr().out) == {"ok": True}
    assert captured == {
        "project_path": root,
        "run_id": "wfr_route",
        "action_id": "act_route",
        "action_input": action_input,
    }


_PROTECTED_ARGUMENTS: dict[str, dict[str, Any]] = {
    "transcribe_source": {
        "operation_id": "op_public_surface",
        "source_id": "src_none",
        "expected_revision": 0,
    },
    "brief_create": {
        "theme": "theme",
        "target_duration_ticks": 120_000,
        "focus": ["focus"],
        "allow_reorder": False,
        "expected_revision": 0,
    },
    "content_draft_create": {
        "parent_draft_id": None,
        "display_title": "draft",
        "source_bindings": [
            {"source_id": "src_none", "transcript_version_id": "tr_none"}
        ],
        "brief_id": "brief_none",
        "context_hash": "0" * 64,
        "blocks": [{}],
        "expected_revision": 0,
    },
    "content_draft_revise_scoped": {
        "parent_draft_id": "draft_none",
        "mutable_block_ids": ["block_none"],
        "blocks": [{}],
        "expected_revision": 0,
    },
    "content_draft_confirm": {
        "content_draft_id": "draft_none",
        "expected_revision": 0,
    },
    "content_draft_propose": {
        "content_draft_id": "draft_none",
        "expected_revision": 0,
    },
    "proposal_create": {
        "source_id": "src_none",
        "transcript_version_id": "tr_none",
        "brief_id": "brief_none",
        "context_hash": "0" * 64,
        "clips": [{}],
        "total_duration_ticks": 1,
        "expected_revision": 0,
    },
    "multi_source_proposal_create": {
        "source_bindings": [
            {"source_id": "src_none", "transcript_version_id": "tr_none"},
            {"source_id": "src_other", "transcript_version_id": "tr_other"},
        ],
        "brief_id": "brief_none",
        "context_hash": "0" * 64,
        "clips": [{}],
        "total_duration_ticks": 1,
        "expected_revision": 0,
    },
    "proposal_confirm": {"proposal_id": "proposal_none", "expected_revision": 0},
    "multi_source_proposal_confirm": {
        "proposal_id": "proposal_none",
        "expected_revision": 0,
    },
    "proposal_reject": {"proposal_id": "proposal_none", "expected_revision": 0},
    "render_roughcut": {"edit_version_id": "edit_none", "expected_revision": 0},
}


@pytest.mark.parametrize("tool_name", sorted(_PROTECTED_ARGUMENTS))
def test_every_protected_public_write_requires_active_run(
    tmp_path: Path,
    tool_name: str,
) -> None:
    root = _project(tmp_path, with_source=False)
    project_before = (root / "project.json").read_bytes()
    payload = _mcp(
        tool_name,
        {"project_path": str(root), **_PROTECTED_ARGUMENTS[tool_name]},
    )
    assert payload["error"]["code"] == "workflow_required"
    assert (root / "project.json").read_bytes() == project_before
    assert not (root / "workflow").exists()


def test_fwv_001_direct_render_has_no_mutation_or_renderer_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path, with_source=False)
    project_before = (root / "project.json").read_bytes()
    calls = 0

    def forbidden_render(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("render_roughcut must not be called")

    monkeypatch.setattr("roughcut.mcp.render_roughcut", forbidden_render)
    payload = _mcp(
        "render_roughcut",
        {
            "project_path": str(root),
            "edit_version_id": "edit_historical",
            "expected_revision": 0,
        },
    )
    assert payload["error"]["code"] == "workflow_required"
    assert calls == 0
    assert (root / "project.json").read_bytes() == project_before
    assert not (root / "workflow").exists()


def test_active_run_rejects_wrong_stage_and_unauthorized_transcription(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path, with_source=True)
    _mcp(
        "workflow_start",
        {
            "project_path": str(root),
            "run_id": "wfr_guard",
            "ordered_source_ids": ["src_public"],
        },
    )
    wrong_stage = _mcp(
        "proposal_confirm",
        {
            "project_path": str(root),
            "proposal_id": "proposal_none",
            "expected_revision": 1,
        },
    )
    assert wrong_stage["error"]["code"] == "workflow_transition_not_allowed"
    unauthorized = _mcp(
        "transcribe_source",
        {
            "project_path": str(root),
            "operation_id": "op_public_unauthorized",
            "source_id": "src_public",
            "expected_revision": 1,
        },
    )
    assert unauthorized["error"]["code"] == "workflow_approval_required"
    outside_scope = _mcp(
        "transcribe_source",
        {
            "project_path": str(root),
            "operation_id": "op_public_outside_scope",
            "source_id": "src_other",
            "expected_revision": 1,
        },
    )
    assert outside_scope["error"]["code"] == "workflow_subject_mismatch"


@pytest.mark.parametrize(
    ("schema_version", "selected_name"),
    ((1, "single"), (2, "multi")),
)
def test_public_proposal_reject_selects_current_schema_service(
    monkeypatch: pytest.MonkeyPatch,
    schema_version: int,
    selected_name: str,
) -> None:
    calls: list[str] = []

    @contextmanager
    def authorized(*_args: object, **_kwargs: object):
        yield SimpleNamespace(
            artifact_refs={
                "proposal": SimpleNamespace(schema_version=schema_version)
            }
        )

    def reject_single(*_args: object, **_kwargs: object) -> ProposalRejection:
        calls.append("single")
        return ProposalRejection("proposal_current", 7)

    def reject_multi(*_args: object, **_kwargs: object) -> ProposalRejection:
        calls.append("multi")
        return ProposalRejection("proposal_current", 7)

    monkeypatch.setattr("roughcut.mcp.protected_write", authorized)
    monkeypatch.setattr("roughcut.mcp.reject_edit_proposal", reject_single)
    monkeypatch.setattr(
        "roughcut.mcp.reject_multi_source_edit_proposal", reject_multi
    )
    payload = _mcp(
        "proposal_reject",
        {
            "project_path": "/fixture/project",
            "proposal_id": "proposal_current",
            "expected_revision": 7,
        },
    )
    assert calls == [selected_name]
    assert payload["proposal_rejection"]["status"] == "rejected"
