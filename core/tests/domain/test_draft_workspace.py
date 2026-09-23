from __future__ import annotations

import json

import pytest

from roughcut.domain.draft_workspace import (
    DraftWorkspaceCheckpoint,
    DraftWorkspaceCheckpointRef,
    draft_workspace_input_hash,
)
from roughcut.domain.errors import WorkflowError
from roughcut.domain.workflow import load_closed_json


def _checkpoint_payload() -> dict[str, object]:
    current = {
        "artifact_id": "draft_current",
        "schema_version": 1,
        "content_hash": "c" * 64,
    }
    return {
        "schema_version": 1,
        "project_id": "proj_alpha",
        "workflow_run_id": "wfr_alpha",
        "generation": 3,
        "current_candidate_ref": current,
        "project_revision": 12,
        "ordered_bindings": [
            {
                "source_id": "src_a",
                "transcript_version_id": "tr_a_1",
                "transcript_content_hash": "a" * 64,
            }
        ],
        "context_hash": "b" * 64,
        "redo_candidate_refs": [
            {
                "artifact_id": "draft_redo",
                "schema_version": 1,
                "content_hash": "d" * 64,
            }
        ],
        "last_commit": {
            "operation_id": "dwop_2_0123456789abcdef0123456789abcdef",
            "expected_generation": 2,
            "operation": "undo",
            "input_hash": "e" * 64,
            "result_candidate_ref": current,
        },
        "audit_review_session_id": "review_session_alpha",
        "updated_at": "2026-07-29T00:00:00.000000Z",
    }


def test_checkpoint_schema_one_roundtrip_and_documented_hash() -> None:
    checkpoint = DraftWorkspaceCheckpoint.from_dict(_checkpoint_payload())

    assert checkpoint.to_dict() == _checkpoint_payload()
    assert checkpoint.checkpoint_hash == (
        "a5276921c5a000c54de851c6584b00f8418fad735e3646645eba5a66f83a6758"
    )
    assert DraftWorkspaceCheckpointRef.for_checkpoint(checkpoint).to_dict() == {
        "generation": 3,
        "checkpoint_hash": checkpoint.checkpoint_hash,
    }


@pytest.mark.parametrize(
    ("mutation", "evidence"),
    [
        (lambda value: value.pop("context_hash"), "missing"),
        (lambda value: value.__setitem__("unknown", True), "unknown"),
        (lambda value: value.__setitem__("generation", True), "type"),
        (
            lambda value: value["ordered_bindings"][0].__setitem__("unknown", True),
            "nested unknown",
        ),
        (
            lambda value: value["last_commit"].__setitem__(
                "operation_id", "dwop_1_0123456789abcdef0123456789abcdef"
            ),
            "generation mismatch",
        ),
    ],
)
def test_checkpoint_closed_schema_rejects_invalid_payload(
    mutation: object, evidence: str
) -> None:
    payload = _checkpoint_payload()
    mutation(payload)  # type: ignore[operator]

    with pytest.raises(WorkflowError, match="checkpoint|generation"):
        DraftWorkspaceCheckpoint.from_dict(payload)


def test_checkpoint_strict_loader_rejects_duplicate_key_and_float() -> None:
    with pytest.raises(WorkflowError, match="duplicate"):
        load_closed_json(b'{"schema_version":1,"schema_version":1}')
    with pytest.raises(WorkflowError, match="not an integer"):
        load_closed_json(b'{"generation":1.0}')


def test_checkpoint_input_hash_is_canonical_and_business_input_sensitive() -> None:
    payload = _checkpoint_payload()
    checkpoint = DraftWorkspaceCheckpoint.from_dict(payload)
    ref = DraftWorkspaceCheckpointRef.for_checkpoint(checkpoint)
    current = checkpoint.current_candidate_ref
    first = draft_workspace_input_hash(
        project_id=checkpoint.project_id,
        workflow_run_id=checkpoint.workflow_run_id,
        operation_id="dwop_3_0123456789abcdef0123456789abcdef",
        operation="narration_edit",
        expected_checkpoint_ref=ref,
        expected_current_candidate_ref=current,
        input_payload={"block_id": "block_a", "text": "新解说"},
    )
    reordered = json.loads(
        '{"text":"新解说","block_id":"block_a"}'
    )
    second = draft_workspace_input_hash(
        project_id=checkpoint.project_id,
        workflow_run_id=checkpoint.workflow_run_id,
        operation_id="dwop_3_0123456789abcdef0123456789abcdef",
        operation="narration_edit",
        expected_checkpoint_ref=ref,
        expected_current_candidate_ref=current,
        input_payload=reordered,
    )
    changed = draft_workspace_input_hash(
        project_id=checkpoint.project_id,
        workflow_run_id=checkpoint.workflow_run_id,
        operation_id="dwop_3_0123456789abcdef0123456789abcdef",
        operation="narration_edit",
        expected_checkpoint_ref=ref,
        expected_current_candidate_ref=current,
        input_payload={"block_id": "block_a", "text": "不同解说"},
    )

    assert first == second
    assert first != changed
