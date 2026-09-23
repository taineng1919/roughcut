from __future__ import annotations

import json

import pytest

from roughcut.domain.errors import WorkflowError
from roughcut.domain.project import Project
from roughcut.domain.workflow import (
    ActionReceipt,
    ApprovalRecord,
    ArtifactRef,
    MutationRef,
    OutputRef,
    ReceiptState,
    ScopeAuthorization,
    SubjectRef,
    TransactionMarker,
    WorkflowBinding,
    WorkflowRun,
    canonical_json_v1,
    canonical_sha256_v1,
    dependency_bundle_hash,
    load_closed_json,
    subject_content_hash,
    workflow_action_input_hash,
)

HASH_A = "a" * 64
HASH_B = "b" * 64
NOW = "2026-07-27T12:34:56.000001Z"


def _project_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "project_id": "proj_test",
        "revision": 7,
        "name": "Project",
        "created_at": NOW,
        "updated_at": NOW,
        "settings": {
            "timebase": 120000,
            "frame_rate": {"numerator": 25, "denominator": 1},
            "width": 1920,
            "height": 1080,
            "audio_sample_rate": 48000,
        },
        "sources": [],
        "active_transcript_versions": {},
        "active_brief_id": None,
        "active_edit_version_id": None,
        "persons": [],
        "speaker_maps": [],
        "edit_redo_stack": [],
        "active_content_draft_id": None,
    }


def _run_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": "wfr_test",
        "project_id": "proj_test",
        "stage": "scope_review",
        "lifecycle": "active",
        "created_at": NOW,
        "updated_at": NOW,
        "ordered_bindings": [
            {
                "source_id": "src_a",
                "transcript_version_id": None,
                "transcript_content_hash": None,
            }
        ],
        "scope_authorizations": [],
        "artifact_refs": {
            "brief": None,
            "outline": None,
            "content_draft": None,
            "proposal": None,
            "decision": None,
            "render": None,
        },
        "readiness_basis": {
            "scope_subject_hash": None,
            "brief_subject_hash": None,
            "required_transcripts": [],
            "speaker_resolution": {
                "mode": "not_ready",
                "refs": [],
                "waiver_subject_hash": None,
            },
            "blocking_operation_ids": [],
        },
        "approval_refs": {
            "scope": None,
            "brief": None,
            "outline": None,
            "draft": None,
            "roughcut": None,
            "export": None,
        },
        "last_receipt_ref": None,
    }


def _run_with_content_draft_ref(schema_version: int) -> dict[str, object]:
    payload = _run_payload()
    artifact_refs = payload["artifact_refs"]
    assert isinstance(artifact_refs, dict)
    artifact_refs["content_draft"] = {
        "artifact_id": "draft_test",
        "schema_version": schema_version,
        "content_hash": HASH_A,
    }
    return payload


def test_workflow_run_roundtrips_content_draft_schema1_ref() -> None:
    run = WorkflowRun.from_dict(_run_with_content_draft_ref(1))
    restored = WorkflowRun.from_dict(run.to_dict())
    assert restored.artifact_refs["content_draft"] is not None
    assert restored.artifact_refs["content_draft"].schema_version == 1


def test_workflow_run_roundtrips_content_draft_schema2_ref() -> None:
    run = WorkflowRun.from_dict(_run_with_content_draft_ref(2))
    restored = WorkflowRun.from_dict(run.to_dict())
    assert restored.artifact_refs["content_draft"] is not None
    assert restored.artifact_refs["content_draft"].schema_version == 2


def test_workflow_run_rejects_content_draft_schema3_ref() -> None:
    with pytest.raises(WorkflowError, match="content_draft artifact schema_version is unsupported"):
        WorkflowRun.from_dict(_run_with_content_draft_ref(3))


def _approval_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "approval_id": "appr_test",
        "run_id": "wfr_test",
        "project_id": "proj_test",
        "gate": "scope",
        "subject": {
            "kind": "scope_snapshot",
            "artifact_id": "scope_test",
            "schema_version": 1,
            "content_hash": HASH_A,
        },
        "dependency_hash": HASH_B,
        "issued_project_revision": 7,
        "issued_by_action_id": "act_test",
        "source": {
            "channel": "agent_conversation",
            "action": "approve_scope",
            "actor_assurance": "unverified_host_user_action",
        },
        "issued_at": NOW,
    }


def _receipt_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "action_id": "act_test",
        "input_hash": HASH_A,
        "run_id": "wfr_test",
        "project_id": "proj_test",
        "action": "approve_scope",
        "before": {
            "stage": "scope_review",
            "lifecycle": "active",
            "project_revision": 7,
        },
        "after": {
            "stage": "scope_review",
            "lifecycle": "active",
            "project_revision": 7,
        },
        "approval_ids": ["appr_test"],
        "mutation": None,
        "output_refs": [],
        "created_at": NOW,
    }


def _marker_payload() -> dict[str, object]:
    project = Project.from_dict(_project_payload())
    run = WorkflowRun.from_dict(_run_payload())
    return {
        "schema_version": 1,
        "action_id": "act_test",
        "input_hash": HASH_A,
        "run_id": "wfr_test",
        "project_id": "proj_test",
        "action": "approve_scope",
        "project_before_hash": canonical_sha256_v1(project.to_dict()),
        "project_after_hash": HASH_B,
        "run_before_hash": canonical_sha256_v1(run.to_dict()),
        "run_after_hash": HASH_B,
        "project_before": project.to_dict(),
        "run_before": run.to_dict(),
        "candidate_refs": [],
        "approval_ids": ["appr_test"],
        "commit_step": "prepared",
    }


def test_schema_one_objects_roundtrip() -> None:
    run = WorkflowRun.from_dict(_run_payload())
    approval = ApprovalRecord.from_dict(_approval_payload())
    receipt = ActionReceipt.from_dict(_receipt_payload())
    marker = TransactionMarker.from_dict(_marker_payload())

    assert WorkflowRun.from_dict(run.to_dict()) == run
    assert ApprovalRecord.from_dict(approval.to_dict()) == approval
    assert ActionReceipt.from_dict(receipt.to_dict()) == receipt
    assert TransactionMarker.from_dict(marker.to_dict()) == marker
    assert run.ordered_bindings == (WorkflowBinding("src_a", None, None),)
    assert run.scope_authorizations == ()
    assert receipt.before == ReceiptState("scope_review", "active", 7)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.pop("project_before"),
        lambda payload: payload.update({"unexpected": True}),
        lambda payload: payload.update({"schema_version": 2}),
        lambda payload: payload["project_before"].update({"schema_version": 2}),
        lambda payload: payload["project_before"].update({"unexpected": True}),
        lambda payload: payload["run_before"].update({"schema_version": 2}),
        lambda payload: payload.update({"project_before_hash": HASH_A}),
        lambda payload: payload.update({"run_before_hash": HASH_A}),
        lambda payload: payload.update({"approval_ids": ["appr_test", "appr_test"]}),
        lambda payload: payload.update({"approval_ids": ["../appr_test"]}),
        lambda payload: payload.update(
            {
                "candidate_refs": [
                    {
                        "kind": "brief",
                        "artifact_id": "brief_test",
                        "schema_version": 1,
                        "content_hash": HASH_A,
                        "project_relative_path": "../brief.json",
                    }
                ]
            }
        ),
    ],
)
def test_transaction_marker_rejects_invalid_before_images_and_closed_schema(
    mutation: object,
) -> None:
    payload = _marker_payload()
    mutation(payload)  # type: ignore[operator,union-attr]

    with pytest.raises(WorkflowError) as captured:
        TransactionMarker.from_dict(payload)

    assert captured.value.code == "workflow_integrity_error"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.pop("stage"),
        lambda payload: payload.update({"allowed_actions": []}),
        lambda payload: payload.update({"schema_version": 2}),
        lambda payload: payload.update({"run_id": "../escape"}),
    ],
)
def test_workflow_run_rejects_missing_extra_unknown_schema_and_unsafe_id(
    mutation: object,
) -> None:
    payload = _run_payload()
    mutation(payload)  # type: ignore[operator]

    with pytest.raises(WorkflowError) as captured:
        WorkflowRun.from_dict(payload)

    assert captured.value.code == "workflow_integrity_error"


@pytest.mark.parametrize("schema_version", (1, 2))
def test_workflow_run_render_plan_schema_one_and_two_roundtrip(
    schema_version: int,
) -> None:
    payload = _run_payload()
    artifact_refs = payload["artifact_refs"]
    assert isinstance(artifact_refs, dict)
    artifact_refs["render"] = {
        "artifact_id": "render_test",
        "schema_version": schema_version,
        "content_hash": HASH_A,
    }

    run = WorkflowRun.from_dict(payload)

    assert run.artifact_refs["render"] == ArtifactRef(
        "render_test", schema_version, HASH_A
    )
    assert WorkflowRun.from_dict(run.to_dict()) == run


def test_workflow_run_render_plan_schema_three_remains_rejected() -> None:
    payload = _run_payload()
    artifact_refs = payload["artifact_refs"]
    assert isinstance(artifact_refs, dict)
    artifact_refs["render"] = {
        "artifact_id": "render_test",
        "schema_version": 3,
        "content_hash": HASH_A,
    }

    with pytest.raises(WorkflowError, match="render artifact schema_version"):
        WorkflowRun.from_dict(payload)


@pytest.mark.parametrize(
    ("parser", "payload"),
    [
        (ApprovalRecord.from_dict, _approval_payload()),
        (ActionReceipt.from_dict, _receipt_payload()),
    ],
)
def test_approval_and_receipt_are_closed_schema(parser: object, payload: dict[str, object]) -> None:
    payload["unexpected"] = True
    with pytest.raises(WorkflowError, match="fields are invalid"):
        parser(payload)  # type: ignore[operator]


@pytest.mark.parametrize(
    ("parser", "payload"),
    [
        (ApprovalRecord.from_dict, _approval_payload()),
        (ActionReceipt.from_dict, _receipt_payload()),
    ],
)
def test_approval_and_receipt_reject_unknown_schema(
    parser: object, payload: dict[str, object]
) -> None:
    payload["schema_version"] = 2
    with pytest.raises(WorkflowError, match="unsupported"):
        parser(payload)  # type: ignore[operator]


def test_duplicate_json_key_and_normalized_duplicate_key_are_rejected() -> None:
    with pytest.raises(WorkflowError, match="duplicate key"):
        load_closed_json('{"schema_version":1,"schema_version":1}')

    with pytest.raises(WorkflowError, match="duplicate normalized"):
        canonical_json_v1({"é": 1, "e\u0301": 2})

    with pytest.raises(WorkflowError, match="BOM"):
        load_closed_json(b"\xef\xbb\xbf{}")


def test_canonical_json_normalizes_newlines_unicode_and_key_order() -> None:
    first = {"z": "Cafe\u0301\r\nline\rnext", "a": 1, "truth": True}
    second = {"truth": True, "a": 1, "z": "Café\nline\nnext"}

    assert canonical_json_v1(first) == canonical_json_v1(second)
    assert canonical_json_v1(first) == '{"a":1,"truth":true,"z":"Café\\nline\\nnext"}'.encode()
    assert not canonical_json_v1(first).endswith(b"\n")


@pytest.mark.parametrize("value", [1.0, float("nan"), float("inf"), b"bytes"])
def test_canonical_json_rejects_float_and_binary(value: object) -> None:
    with pytest.raises(WorkflowError):
        canonical_json_v1({"value": value})


def test_canonical_hash_envelopes_are_stable_and_distinct() -> None:
    content = {"value": 1}
    assert canonical_sha256_v1(content) == canonical_sha256_v1(json.loads('{"value":1}'))
    assert subject_content_hash("brief", 1, content) != dependency_bundle_hash(
        "brief", content
    )
    assert len(workflow_action_input_hash("wfr_test", "act_test", "approve_scope", {})) == 64


def test_approval_current_or_stale_is_purely_derived() -> None:
    record = ApprovalRecord.from_dict(_approval_payload())
    same_subject = SubjectRef.from_dict(_approval_payload()["subject"])
    changed_subject = SubjectRef(
        kind=same_subject.kind,
        artifact_id=same_subject.artifact_id,
        schema_version=same_subject.schema_version,
        content_hash=HASH_B,
    )

    assert record.effective_status(same_subject, HASH_B) == "current"
    assert record.effective_status(changed_subject, HASH_B) == "stale"
    assert record.effective_status(same_subject, HASH_A) == "stale"


def test_approval_subject_kind_must_match_its_gate() -> None:
    payload = _approval_payload()
    subject = payload["subject"]
    assert isinstance(subject, dict)
    subject["kind"] = "proposal"

    with pytest.raises(WorkflowError, match="subject kind"):
        ApprovalRecord.from_dict(payload)


def test_scope_authorizations_roundtrip_and_match_binding_order() -> None:
    payload = _run_payload()
    payload["scope_authorizations"] = [
        {
            "source_id": "src_a",
            "transcribe": True,
            "speaker_diarization": True,
        }
    ]

    run = WorkflowRun.from_dict(payload)

    assert run.scope_authorizations == (
        ScopeAuthorization("src_a", True, True),
    )
    assert WorkflowRun.from_dict(run.to_dict()) == run


def test_scope_authorizations_reject_wrong_order_or_diarization_without_asr() -> None:
    payload = _run_payload()
    payload["scope_authorizations"] = [
        {
            "source_id": "src_other",
            "transcribe": True,
            "speaker_diarization": False,
        }
    ]
    with pytest.raises(WorkflowError, match="match ordered bindings"):
        WorkflowRun.from_dict(payload)

    payload["scope_authorizations"] = [
        {
            "source_id": "src_a",
            "transcribe": False,
            "speaker_diarization": True,
        }
    ]
    with pytest.raises(WorkflowError, match="requires transcription"):
        WorkflowRun.from_dict(payload)


def test_approved_scope_requires_persisted_authorizations() -> None:
    payload = _run_payload()
    approvals = payload["approval_refs"]
    assert isinstance(approvals, dict)
    approvals["scope"] = {
        "approval_id": "appr_scope",
        "record_schema_version": 1,
        "record_hash": HASH_A,
    }

    with pytest.raises(WorkflowError, match="approved scope requires"):
        WorkflowRun.from_dict(payload)


def test_receipt_supports_fixed_compound_approve_draft_outputs() -> None:
    receipt = ActionReceipt(
        schema_version=1,
        action_id="act_approve_draft",
        input_hash=HASH_A,
        run_id="wfr_test",
        project_id="proj_test",
        action="approve_draft",
        before=ReceiptState("draft_review", "active", 4),
        after=ReceiptState("roughcut_review", "active", 5),
        approval_ids=("appr_draft",),
        mutation=MutationRef("content_draft", "draft_confirmed", 1, HASH_A, True),
        output_refs=(
            OutputRef("proposal", "proposal_test", 2, HASH_B, None),
        ),
        created_at=NOW,
    )

    assert ActionReceipt.from_dict(receipt.to_dict()) == receipt
    assert receipt.output_refs[0].project_relative_path is None


def test_receipt_rejects_mutation_or_outputs_for_the_wrong_action() -> None:
    with pytest.raises(WorkflowError, match="mutation does not match"):
        ActionReceipt(
            schema_version=1,
            action_id="act_scope",
            input_hash=HASH_A,
            run_id="wfr_test",
            project_id="proj_test",
            action="approve_scope",
            before=ReceiptState("scope_review", "active", 1),
            after=ReceiptState("scope_review", "active", 1),
            approval_ids=("appr_scope",),
            mutation=MutationRef("decision", "edit_wrong", 1, HASH_A, True),
            output_refs=(),
            created_at=NOW,
        )


def test_receipt_rejects_wrong_project_revision_delta() -> None:
    payload = _receipt_payload()
    after = payload["after"]
    assert isinstance(after, dict)
    after["project_revision"] = 8

    with pytest.raises(WorkflowError, match="revision delta"):
        ActionReceipt.from_dict(payload)


def test_cancel_receipt_is_separate_from_business_action_enum() -> None:
    receipt = ActionReceipt(
        schema_version=1,
        action_id="act_cancel",
        input_hash=workflow_action_input_hash(
            "wfr_test", "act_cancel", "workflow_cancel", {}
        ),
        run_id="wfr_test",
        project_id="proj_test",
        action="workflow_cancel",
        before=ReceiptState("draft_review", "active", 4),
        after=ReceiptState("draft_review", "canceled", 4),
        approval_ids=(),
        mutation=None,
        output_refs=(),
        created_at=NOW,
    )

    assert ActionReceipt.from_dict(receipt.to_dict()) == receipt
    with pytest.raises(WorkflowError, match="lifecycle"):
        ActionReceipt(
            **{
                **receipt.__dict__,
                "after": ReceiptState("draft_review", "active", 4),
            }
        )


@pytest.mark.parametrize("path", ["C:/outside.mp4", ".", "../outside.mp4"])
def test_output_ref_rejects_non_portable_relative_paths(path: str) -> None:
    with pytest.raises(WorkflowError, match="unsafe"):
        OutputRef("mp4", "render_test", 1, HASH_A, path)


@pytest.mark.parametrize(
    ("mode", "refs", "match"),
    [
        (
            "no_speakers",
            [
                {
                    "source_id": "src_a",
                    "transcript_version_id": "tr_a",
                    "local_speaker_id": "spk_0",
                    "resolution": "mapped",
                    "person_id": "person_a",
                }
            ],
            "no_speakers",
        ),
        (
            "all_mapped",
            [],
            "all_mapped",
        ),
        (
            "not_ready",
            [
                {
                    "source_id": "src_other",
                    "transcript_version_id": "tr_other",
                    "local_speaker_id": "spk_0",
                    "resolution": "mapped",
                    "person_id": "person_a",
                }
            ],
            "outside ordered bindings",
        ),
    ],
)
def test_workflow_run_rejects_inconsistent_speaker_readiness(
    mode: str, refs: list[dict[str, object]], match: str
) -> None:
    payload = _run_payload()
    payload["ordered_bindings"] = [
        {
            "source_id": "src_a",
            "transcript_version_id": "tr_a",
            "transcript_content_hash": HASH_A,
        }
    ]
    readiness = payload["readiness_basis"]
    assert isinstance(readiness, dict)
    readiness["required_transcripts"] = [
        {
            "source_id": "src_a",
            "transcript_version_id": "tr_a",
            "schema_version": 1,
            "content_hash": HASH_A,
        }
    ]
    readiness["speaker_resolution"] = {
        "mode": mode,
        "refs": refs,
        "waiver_subject_hash": None,
    }

    with pytest.raises(WorkflowError, match=match):
        WorkflowRun.from_dict(payload)


def _outline_snapshot(section_count: int) -> dict[str, object]:
    return {
        "schema_version": 1,
        "title": "Title",
        "opening": "Opening",
        "sections": [
            {
                "section_id": f"section_{index}",
                "title": f"Section {index}",
                "summary": "Summary",
                "target_duration_ticks": 100,
            }
            for index in range(section_count)
        ],
        "ending": "Ending",
        "required_content_coverage": [],
        "narration_status": "none",
    }


@pytest.mark.parametrize("section_count", (1, 3, 4, 7, 8, 12))
def test_outline_snapshot_roundtrips_any_non_empty_section_count(
    section_count: int,
) -> None:
    snapshot = _outline_snapshot(section_count)
    content_hash = subject_content_hash("outline_snapshot", 1, snapshot)
    ref = ArtifactRef(
        f"outline_{content_hash[:16]}", 1, content_hash, snapshot
    )

    restored = ArtifactRef.from_dict(ref.to_dict(), outline=True)

    assert restored == ref
    assert len(restored.snapshot["sections"]) == section_count  # type: ignore[index]


def test_outline_snapshot_rejects_empty_sections() -> None:
    snapshot = _outline_snapshot(0)
    content_hash = subject_content_hash("outline_snapshot", 1, snapshot)

    with pytest.raises(WorkflowError, match="non-empty sections array"):
        ArtifactRef(f"outline_{content_hash[:16]}", 1, content_hash, snapshot)


def test_existing_four_section_outline_readback_is_byte_identical() -> None:
    snapshot = _outline_snapshot(4)
    content_hash = subject_content_hash("outline_snapshot", 1, snapshot)
    ref = ArtifactRef(
        f"outline_{content_hash[:16]}", 1, content_hash, snapshot
    )
    original_bytes = canonical_json_v1(ref.to_dict())

    restored = ArtifactRef.from_dict(ref.to_dict(), outline=True)

    assert canonical_json_v1(restored.to_dict()) == original_bytes


def test_outline_ref_requires_deterministic_id() -> None:
    snapshot = _outline_snapshot(4)
    content_hash = subject_content_hash("outline_snapshot", 1, snapshot)

    with pytest.raises(WorkflowError, match="outline artifact ID"):
        ArtifactRef("outline_wrong", 1, content_hash, snapshot)
