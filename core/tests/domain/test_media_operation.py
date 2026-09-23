from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from roughcut.domain.media_operation import (
    ArtifactIdentity,
    MediaOperationError,
    MediaOperationFailure,
    MediaOperationRecord,
    ProjectOperationScope,
    ProxyOperationResult,
    ReceiptIdentity,
    RenderOperationResult,
    RenderOutputIdentity,
    TranscriptOperationResult,
    hash_approve_export_input,
    hash_approve_export_request,
    hash_proxy_input,
    hash_proxy_request,
    hash_transcription_input,
    hash_transcription_request,
    validate_media_operation_transition,
)

ROOT = Path(__file__).resolve().parents[3]
VECTORS = ROOT / "core" / "tests" / "fixtures" / "media-operation-record-vectors.json"


def _result(operation_type: str) -> object:
    if operation_type == "transcribe_source":
        return TranscriptOperationResult(
            source_id="source_a",
            transcript_version_id="tr_fixture",
            schema_version=1,
            content_hash="1" * 64,
            project_revision=8,
        )
    if operation_type == "proxy_create":
        return ProxyOperationResult(
            source_id="source_a",
            cache_key="9" * 64,
            manifest_schema_version=1,
            manifest_content_hash="2" * 64,
            output_relative_path=f"proxies/source_a/{'9' * 64}/proxy.mp4",
            output_size=12,
            output_sha256_head_tail="3" * 64,
            project_revision=7,
        )
    return RenderOperationResult(
        run_id="run_fixture",
        render_plan_ref=ArtifactIdentity("render_fixture", 2, "a" * 64),
        mp4_ref=RenderOutputIdentity(
            "mp4",
            "render_fixture",
            1,
            "4" * 64,
            "renders/render_fixture.mp4",
        ),
        manifest_ref=RenderOutputIdentity(
            "manifest",
            "render_fixture",
            3,
            "5" * 64,
            "renders/render_fixture.manifest.json",
        ),
        approve_export_receipt_ref=ReceiptIdentity(
            "action_export_fixture", 1, "6" * 64
        ),
        project_revision=7,
    )


def _record(
    operation_type: str = "transcribe_source", status: str = "pending"
) -> MediaOperationRecord:
    phases = {
        "transcribe_source": "transcription_preparing",
        "proxy_create": "proxy_preparing",
        "approve_export": "render_preparing",
    }
    terminal = {
        "transcribe_source": "transcription",
        "proxy_create": "proxy",
        "approve_export": "render",
    }
    pending = MediaOperationRecord(
        operation_id="op_fixture",
        scope=ProjectOperationScope("project_fixture", "a" * 64),
        operation_type=operation_type,  # type: ignore[arg-type]
        request_hash="b" * 64,
        input_hash="c" * 64,
        status="pending",
        phase_message_code=phases[operation_type],
        created_at="2026-07-29T08:00:00.000000Z",
        started_at=None,
        updated_at="2026-07-29T08:00:00.000000Z",
        finished_at=None,
        result_ref=None,
        error=None,
    )
    if status == "pending":
        return pending
    running = replace(
        pending,
        status="running",
        started_at="2026-07-29T08:00:01.000000Z",
        updated_at="2026-07-29T08:00:01.000000Z",
    )
    if status == "running":
        return running
    if status == "succeeded":
        return replace(
            running,
            status="succeeded",
            phase_message_code=f"{terminal[operation_type]}_succeeded",
            updated_at="2026-07-29T08:00:02.000000Z",
            finished_at="2026-07-29T08:00:02.000000Z",
            result_ref=_result(operation_type),  # type: ignore[arg-type]
        )
    code = (
        "media_operation_interrupted"
        if status == "interrupted"
        else "media_operation_failed"
    )
    action = (
        "recover_abandoned_media_operation"
        if status == "interrupted"
        else {
            "transcribe_source": "publish_transcript",
            "proxy_create": "publish_proxy",
            "approve_export": "publish_export_transaction",
        }[operation_type]
    )
    return replace(
        running,
        status=status,  # type: ignore[arg-type]
        phase_message_code=f"{terminal[operation_type]}_{status}",
        updated_at="2026-07-29T08:00:02.000000Z",
        finished_at="2026-07-29T08:00:02.000000Z",
        error=MediaOperationFailure(
            code=code,
            responsibility="roughcut_core",
            action=action,
            message_code=f"{terminal[operation_type]}_{status}",
        ),
    )


def test_media_operation_closed_roundtrip_and_forward_transitions() -> None:
    for operation_type in (
        "transcribe_source",
        "proxy_create",
        "approve_export",
    ):
        for status in (
            "pending",
            "running",
            "succeeded",
            "failed",
            "interrupted",
        ):
            record = _record(operation_type, status)
            assert MediaOperationRecord.from_dict(record.to_dict()) == record
            assert len(record.record_hash) == 64
        pending = _record(operation_type)
        running = _record(operation_type, "running")
        succeeded = _record(operation_type, "succeeded")
        validate_media_operation_transition(pending, running)
        validate_media_operation_transition(running, succeeded)
        with pytest.raises(MediaOperationError) as raised:
            validate_media_operation_transition(succeeded, running)
        assert raised.value.code == "operation_transition_not_allowed"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: {
            key: value for key, value in payload.items() if key != "request_hash"
        },
        lambda payload: {**payload, "unknown": True},
        lambda payload: {**payload, "schema_version": 2},
        lambda payload: {**payload, "operation_id": "../escape"},
        lambda payload: {**payload, "request_hash": "A" * 64},
        lambda payload: {**payload, "request_hash": 1},
        lambda payload: {**payload, "status": "queued"},
        lambda payload: {
            **payload,
            "scope": {**payload["scope"], "project_path": "/private/project"},
        },
    ],
)
def test_media_operation_rejects_missing_unknown_or_wrong_typed_fields(
    mutation,
) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(MediaOperationError) as raised:
        MediaOperationRecord.from_dict(
            mutation(_record("transcribe_source", "succeeded").to_dict())
        )
    assert raised.value.code == "operation_integrity_error"


def test_media_operation_rejects_cross_type_phase_result_and_error() -> None:
    succeeded = _record("transcribe_source", "succeeded").to_dict()
    succeeded["phase_message_code"] = "proxy_succeeded"
    with pytest.raises(MediaOperationError):
        MediaOperationRecord.from_dict(succeeded)

    wrong_result = _record("transcribe_source", "succeeded").to_dict()
    wrong_result["result_ref"] = _result("proxy_create").to_dict()  # type: ignore[union-attr]
    with pytest.raises(MediaOperationError):
        MediaOperationRecord.from_dict(wrong_result)

    wrong_error = _record("proxy_create", "failed").to_dict()
    wrong_error["error"] = {
        "code": "media_operation_failed",
        "responsibility": "ffmpeg_render",
        "action": "encode_render",
        "message_code": "proxy_failed",
    }
    with pytest.raises(MediaOperationError):
        MediaOperationRecord.from_dict(wrong_error)


def test_media_operation_vectors_have_canonical_request_and_input_hashes() -> None:
    payload = json.loads(VECTORS.read_text(encoding="utf-8"))
    vectors = payload["vectors"]
    hashers = {
        "transcribe_source": (
            hash_transcription_request,
            hash_transcription_input,
        ),
        "proxy_create": (hash_proxy_request, hash_proxy_input),
        "approve_export": (
            hash_approve_export_request,
            hash_approve_export_input,
        ),
    }
    for vector in vectors[:3]:
        request_hasher, input_hasher = hashers[vector["operation_type"]]
        assert request_hasher(vector["request_projection"]) == vector["request_hash"]
        assert input_hasher(vector["input_projection"]) == vector["input_hash"]


@pytest.mark.parametrize(
    ("operation_type", "field_path"),
    [
        ("transcribe_source", ("source_id",)),
        ("transcribe_source", ("transcription_request", "speaker_diarization")),
        ("proxy_create", ("expected_project_revision",)),
        ("approve_export", ("workflow_action", "input_hash")),
    ],
)
def test_media_request_hash_rejects_missing_unknown_and_wrong_types(
    operation_type: str, field_path: tuple[str, ...]
) -> None:
    vector = next(
        vector
        for vector in json.loads(VECTORS.read_text(encoding="utf-8"))["vectors"]
        if vector["operation_type"] == operation_type
    )
    hashers = {
        "transcribe_source": hash_transcription_request,
        "proxy_create": hash_proxy_request,
        "approve_export": hash_approve_export_request,
    }
    missing = copy.deepcopy(vector["request_projection"])
    parent = missing
    for key in field_path[:-1]:
        parent = parent[key]
    del parent[field_path[-1]]
    with pytest.raises(MediaOperationError):
        hashers[operation_type](missing)
    with pytest.raises(MediaOperationError):
        hashers[operation_type](
            {**vector["request_projection"], "unknown": True}
        )
    wrong = copy.deepcopy(vector["request_projection"])
    parent = wrong
    for key in field_path[:-1]:
        parent = parent[key]
    parent[field_path[-1]] = 1.5
    with pytest.raises(MediaOperationError):
        hashers[operation_type](wrong)
