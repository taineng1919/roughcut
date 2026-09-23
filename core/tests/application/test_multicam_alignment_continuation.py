from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_workflows import (
    _add_fixture_source,
    _advance_to_export_review,
    _submit_two_binding_draft,
    _workflow_project,
)

from roughcut import m2_7_public_capability
from roughcut.adapters.alignment_store import AlignmentStore
from roughcut.adapters.media_operation_store import MediaOperationStore
from roughcut.adapters.project_store import ProjectStore
from roughcut.adapters.workflow_store import WorkflowStore
from roughcut.application import multicam_continuation as continuation_module
from roughcut.application.agent_context import calculate_agent_context_hash
from roughcut.application.alignments import AlignmentOutcome
from roughcut.application.workflows import workflow_action, workflow_status
from roughcut.domain.alignment import (
    AUDALIGN_CORRELATION_ALGORITHM_NAME,
    AUDALIGN_CORRELATION_ALGORITHM_VERSION,
    AUDALIGN_CORRELATION_PROFILE_NAME,
    AUDALIGN_CORRELATION_PROFILE_VERSION,
    AUDALIGN_CORRELATION_RECOGNIZER,
    AUDALIGN_CORRELATION_UPSTREAM_COMMIT,
    AUDALIGN_CORRELATION_WRITER_PROFILE,
    AUDALIGN_CORRELATION_WRITER_PROFILE_SHA256,
    AUDALIGN_VERSION,
    BBC_PROFILE_NAME,
    BBC_PROFILE_VERSION,
    BBC_WRITER_PROFILE,
    AlignmentAlgorithm,
    AlignmentCamera,
    AlignmentCameraGroup,
    AlignmentInterval,
    AlignmentSourceBasis,
    AlignmentSourceFingerprint,
    AlignmentSummary,
    AlignmentVerificationProfile,
    MulticamAlignmentArtifact,
)
from roughcut.domain.bindings import SourceTranscriptBinding
from roughcut.domain.brief import EditBrief
from roughcut.domain.errors import WorkflowError
from roughcut.domain.media_operation import (
    AlignmentOperationResult,
    MediaOperationError,
    MediaOperationFailure,
    MediaOperationRecord,
)
from roughcut.domain.workflow import (
    MulticamAlignmentContinuation,
    WorkflowRun,
    canonical_json_v1,
    canonical_sha256_v1,
)
from roughcut.mcp import handle_request


def _fixture_alignment_artifact(
    root: Path,
    *,
    operation_id: str,
    alignment_id: str,
    request_hash: str,
    input_hash: str,
    partial: bool,
) -> MulticamAlignmentArtifact:
    duration = 14_400_000
    half = duration // 2
    profile = AlignmentVerificationProfile(
        AUDALIGN_CORRELATION_PROFILE_NAME,
        AUDALIGN_CORRELATION_PROFILE_VERSION,
    )
    algorithm = AlignmentAlgorithm(
        name=AUDALIGN_CORRELATION_ALGORITHM_NAME,
        version=AUDALIGN_CORRELATION_ALGORITHM_VERSION,
        upstream_commit=AUDALIGN_CORRELATION_UPSTREAM_COMMIT,
        accuracy=None,
        num_processors=None,
        mapping_model="fixed_offset_equal_speed",
        ticks_per_second=120_000,
        verification_profile=profile,
    )
    basis = (
        AlignmentSourceBasis(
            camera_id="aux",
            source_id="src_b",
            fingerprint=AlignmentSourceFingerprint(1, 1, "b" * 64),
            duration_ticks=duration,
        ),
        AlignmentSourceBasis(
            camera_id="main",
            source_id="src_a",
            fingerprint=AlignmentSourceFingerprint(1, 1, "a" * 64),
            duration_ticks=duration,
        ),
    )
    def evidence(code: str) -> dict[str, object]:
        if code == "no_candidate":
            return {
                "code": "no_candidate",
                "provider": "audalign",
                "provider_version": AUDALIGN_VERSION,
                "recognizer": AUDALIGN_CORRELATION_RECOGNIZER,
                "probe_records": [],
                "support_probe_count": 0,
                "cluster_spread_ticks": None,
                "representative_b_ticks": None,
                "conflicting_b_ticks": [],
                "verification_profile": profile.to_dict(),
            }
        return {
            "code": "fixed_offset_verified",
            "provider": "audalign",
            "provider_version": AUDALIGN_VERSION,
            "recognizer": AUDALIGN_CORRELATION_RECOGNIZER,
            "probe_records": [
                {
                    "percentage": percentage,
                    "auxiliary_start_ticks": start,
                    "auxiliary_end_ticks": start + 1_800_000,
                    "native_offset_seconds": native,
                    "derived_b_ticks": 0,
                }
                for percentage, start, native in (
                    (20, 2_880_000, "24.0"),
                    (50, 7_200_000, "60.0"),
                    (80, 11_520_000, "96.0"),
                )
            ],
            "support_probe_count": 3,
            "cluster_spread_ticks": 0,
            "representative_b_ticks": 0,
            "conflicting_b_ticks": [],
            "verification_profile": profile.to_dict(),
        }

    intervals = (
        AlignmentInterval(
            interval_id="aln_fixture_interval_1",
            auxiliary_camera_id="aux",
            classification="mapped",
            main={"source_id": "src_a", "start_ticks": 0, "end_ticks": half},
            auxiliary={"source_id": "src_b", "start_ticks": 0, "end_ticks": half},
            evidence=evidence("fixed_offset_verified"),
        ),
    )
    if partial:
        intervals = (
            *intervals,
            AlignmentInterval(
                interval_id="aln_fixture_interval_2",
                auxiliary_camera_id="aux",
                classification="uncertain",
                main={
                    "source_id": "src_a",
                    "start_ticks": half,
                    "end_ticks": duration,
                },
                auxiliary=None,
                evidence=evidence("no_candidate"),
            ),
        )
    else:
        intervals = (
            *intervals,
            AlignmentInterval(
                interval_id="aln_fixture_interval_2",
                auxiliary_camera_id="aux",
                classification="mapped",
                main={
                    "source_id": "src_a",
                    "start_ticks": half,
                    "end_ticks": duration,
                },
                auxiliary={
                    "source_id": "src_b",
                    "start_ticks": half,
                    "end_ticks": duration,
                },
                evidence=evidence("fixed_offset_verified"),
            ),
        )
    camera = AlignmentCamera(
        camera_id="aux",
        ordered_source_ids=("src_b",),
        status="partial" if partial else "complete",
        mapped_ticks=half if partial else duration,
        missing_ticks=0,
        uncertain_ticks=half if partial else 0,
        conflict_ticks=0,
        errors=(),
    )
    return MulticamAlignmentArtifact(
        alignment_id=alignment_id,
        project_id=ProjectStore(root).load().project_id,
        producer_operation_id=operation_id,
        created_at="2026-08-24T00:00:00.000000Z",
        request_hash=request_hash,
        input_hash=input_hash,
        algorithm=algorithm,
        main_camera=AlignmentCameraGroup(
            camera_id="main", ordered_source_ids=("src_a",)
        ),
        auxiliary_cameras=(camera,),
        source_basis=basis,
        intervals=intervals,
        summary=AlignmentSummary(
            total_main_ticks=duration,
            camera_count=1,
            mapped_ticks=half if partial else duration,
            missing_ticks=0,
            uncertain_ticks=half if partial else 0,
            conflict_ticks=0,
        ),
    )


def _fake_success_runner(root: Path, *, partial: bool):
    def run(*args: object, **kwargs: object) -> AlignmentOutcome:
        operation_id = str(kwargs["operation_id"])
        alignment_id = str(kwargs["alignment_id"])
        run = WorkflowStore(root).read_run("wfr_test")
        continuation = run.multicam_alignment_continuation
        setup = run.multicam_setup
        assert continuation is not None and continuation.request_hash is not None
        assert setup is not None
        _request, operation_request_hash = continuation_module._request_facts(
            root,
            run,
            continuation,
            setup,
            AUDALIGN_CORRELATION_WRITER_PROFILE,
        )
        assert operation_request_hash == continuation.request_hash
        input_hash = "e" * 64
        artifact = _fixture_alignment_artifact(
            root,
            operation_id=operation_id,
            alignment_id=alignment_id,
            request_hash=continuation.request_hash,
            input_hash=input_hash,
            partial=partial,
        )
        AlignmentStore(root).publish(alignment_id, artifact)
        project = ProjectStore(root).load()
        store = MediaOperationStore(root, project.project_id)
        result = AlignmentOperationResult(
            alignment_id=alignment_id,
            schema_version=1,
            content_hash=artifact.content_hash,
        )
        record = MediaOperationRecord(
            operation_id=operation_id,
            scope=store.scope,
            operation_type="align_multicam",
            request_hash=operation_request_hash,
            input_hash=input_hash,
            status="succeeded",
            phase_message_code="alignment_succeeded",
            created_at="2026-08-24T00:00:00.000000Z",
            started_at="2026-08-24T00:00:01.000000Z",
            updated_at="2026-08-24T00:00:02.000000Z",
            finished_at="2026-08-24T00:00:02.000000Z",
            result_ref=result,
            error=None,
            schema_version=2,
        )
        with store.writer(operation_id, create=True) as acquired:
            assert acquired
            store.write_locked(record)
        return AlignmentOutcome(record, artifact, False)

    return run


def _two_camera_setup() -> dict[str, object]:
    return {
        "schema_version": 1,
        "main_camera": {"camera_id": "main", "ordered_source_ids": ["src_a"]},
        "auxiliary_cameras": [
            {"camera_id": "aux", "ordered_source_ids": ["src_b"]}
        ],
        "source_pairs": [
            {"main_source_id": "src_a", "auxiliary_source_id": "src_b"}
        ],
    }


def _adopt_required_setup(root: Path):
    submitted = _submit_two_binding_draft(
        root,
        multicam_setup=_two_camera_setup(),
    )
    mutation = submitted.receipt.mutation
    assert mutation is not None
    approved = workflow_action(
        root,
        "wfr_test",
        "compat_approve_draft",
        "approve_draft",
        {
            "schema_version": 1,
            "content_draft_ref": {
                "artifact_id": mutation.artifact_id,
                "schema_version": mutation.schema_version,
                "content_hash": mutation.content_hash,
            },
        },
    )
    proposal = approved.workflow_run.artifact_refs["proposal"]
    assert proposal is not None
    return workflow_action(
        root,
        "wfr_test",
        "compat_adopt",
        "adopt_roughcut",
        {"schema_version": 1, "proposal_ref": proposal.to_dict()},
    )


def _adopt_without_alignment_runner(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> WorkflowRun:
    monkeypatch.setattr(
        m2_7_public_capability.platform,
        "system",
        lambda: "Darwin",
    )
    monkeypatch.setattr(
        continuation_module,
        "run_align_multicam",
        lambda *args, **kwargs: None,
    )
    adopted = _adopt_required_setup(root)
    assert adopted.status["multicam_alignment"]["status"] == "pending"
    return adopted.workflow_run


def _prepare_legacy_bbc_state(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[WorkflowRun, str, str, Path, Path]:
    run = _adopt_without_alignment_runner(root, monkeypatch)
    continuation = run.multicam_alignment_continuation
    setup = run.multicam_setup
    decision_ref = run.artifact_refs["decision"]
    assert continuation is not None
    assert setup is not None
    assert decision_ref is not None
    setup_hash = canonical_sha256_v1(setup.to_dict())
    legacy_operation_id, legacy_alignment_id = continuation_module._continuation_ids(
        run,
        decision_ref,
        setup,
        setup_hash,
        writer_profile_name=BBC_PROFILE_NAME,
        writer_profile_version=BBC_PROFILE_VERSION,
        writer_profile_hash=canonical_sha256_v1(BBC_WRITER_PROFILE),
    )
    legacy_payload = continuation.to_dict()
    legacy_payload.update(
        {
            "operation_id": legacy_operation_id,
            "alignment_id": legacy_alignment_id,
            "writer_profile_name": BBC_PROFILE_NAME,
            "writer_profile_version": BBC_PROFILE_VERSION,
            "writer_profile_hash": canonical_sha256_v1(BBC_WRITER_PROFILE),
        }
    )
    legacy_candidate = MulticamAlignmentContinuation.from_dict(legacy_payload)
    _legacy_facts, legacy_hash = continuation_module._request_facts(
        root,
        run,
        legacy_candidate,
        setup,
        BBC_WRITER_PROFILE,
    )
    correlation_facts, correlation_hash = continuation_module._request_facts(
        root,
        run,
        legacy_candidate,
        setup,
        AUDALIGN_CORRELATION_WRITER_PROFILE,
    )
    assert continuation_module._request_facts_without_writer_profile(
        correlation_facts
    ) == continuation_module._request_facts_without_writer_profile(_legacy_facts)
    legacy_payload["request_hash"] = legacy_hash
    run_payload = run.to_dict()
    run_payload["multicam_alignment_continuation"] = legacy_payload
    run_path = root / "workflow" / "runs" / "wfr_test.json"
    run_path.write_bytes(canonical_json_v1(run_payload) + b"\n")
    legacy_run = WorkflowStore(root).read_run("wfr_test")
    legacy_continuation = legacy_run.multicam_alignment_continuation
    assert legacy_continuation is not None
    input_hash = "e" * 64
    artifact = _fixture_alignment_artifact(
        root,
        operation_id=legacy_operation_id,
        alignment_id=legacy_alignment_id,
        request_hash=correlation_hash,
        input_hash=input_hash,
        partial=False,
    )
    AlignmentStore(root).publish(legacy_alignment_id, artifact)
    store = MediaOperationStore(root, legacy_run.project_id)
    result = AlignmentOperationResult(
        alignment_id=legacy_alignment_id,
        schema_version=1,
        content_hash=artifact.content_hash,
    )
    record = MediaOperationRecord(
        operation_id=legacy_operation_id,
        scope=store.scope,
        operation_type="align_multicam",
        request_hash=correlation_hash,
        input_hash=input_hash,
        status="succeeded",
        phase_message_code="alignment_succeeded",
        created_at="2026-08-24T00:00:00.000000Z",
        started_at="2026-08-24T00:00:01.000000Z",
        updated_at="2026-08-24T00:00:02.000000Z",
        finished_at="2026-08-24T00:00:02.000000Z",
        result_ref=result,
        error=None,
        schema_version=2,
    )
    with store.writer(legacy_operation_id, create=True) as acquired:
        assert acquired
        store.write_locked(record)
    operation_path = root / "workflow" / "operations" / "media" / f"{legacy_operation_id}.json"
    artifact_path = AlignmentStore(root)._artifact_path(legacy_alignment_id)
    return legacy_run, legacy_hash, correlation_hash, operation_path, artifact_path


@pytest.fixture(autouse=True)
def _default_m2_7_release_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep existing automatic-continuation tests on a released platform."""
    monkeypatch.setattr(
        m2_7_public_capability.platform,
        "system",
        lambda: "Darwin",
    )


def _public_call(tool: str, arguments: dict[str, object]) -> dict[str, object]:
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": f"gate6-{tool}",
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


def test_adopt_without_confirmed_aux_is_durable_not_required_and_does_not_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = _workflow_project(tmp_path)
    calls: list[object] = []

    def unexpected_runner(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        raise AssertionError("alignment must not run without an auxiliary setup")

    monkeypatch.setattr(continuation_module, "run_align_multicam", unexpected_runner)

    adopted = _advance_to_export_review(root)

    assert calls == []
    assert adopted.status["multicam_alignment"] == {
        "schema_version": 1,
        "status": "not_required",
        "operation_id": None,
        "alignment_ref": None,
        "failure": None,
    }
    assert workflow_status(root, "wfr_test")["multicam_alignment"] == adopted.status[
        "multicam_alignment"
    ]


def test_adopt_with_confirmed_no_aux_setup_is_not_required(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = _workflow_project(tmp_path)
    calls: list[object] = []

    def unexpected_runner(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        raise AssertionError("alignment must not run for a no-aux setup")

    monkeypatch.setattr(continuation_module, "run_align_multicam", unexpected_runner)
    adopted = _advance_to_export_review(
        root,
        multicam_setup={
            "schema_version": 1,
            "main_camera": {
                "camera_id": "main",
                "ordered_source_ids": ["src_a"],
            },
            "auxiliary_cameras": [],
            "source_pairs": [],
        },
    )
    assert calls == []
    assert adopted.workflow_run.multicam_alignment_continuation is not None
    assert (
        adopted.workflow_run.multicam_alignment_continuation.requirement
        == "not_required"
    )
    assert adopted.status["multicam_alignment"]["status"] == "not_required"


def test_windows_without_multicam_continuation_remains_not_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(m2_7_public_capability.platform, "system", lambda: "Windows")
    calls: list[object] = []

    def unexpected_runner(*args: object, **kwargs: object) -> None:
        calls.append((args, kwargs))
        raise AssertionError("alignment must not run without a required continuation")

    monkeypatch.setattr(continuation_module, "run_align_multicam", unexpected_runner)
    root = _workflow_project(tmp_path / "windows-no-aux")
    adopted = _advance_to_export_review(
        root,
        multicam_setup={
            "schema_version": 1,
            "main_camera": {
                "camera_id": "main",
                "ordered_source_ids": ["src_a"],
            },
            "auxiliary_cameras": [],
            "source_pairs": [],
        },
    )

    assert adopted.receipt is not None
    assert adopted.receipt.action == "adopt_roughcut"
    assert adopted.status["multicam_alignment"]["status"] == "not_required"
    assert calls == []
    media_root = root / "workflow" / "operations" / "media"
    assert not media_root.exists() or not list(media_root.glob("*.json"))


def test_windows_required_adopt_publishes_durable_identity_without_alignment_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(m2_7_public_capability.platform, "system", lambda: "Windows")
    calls: list[object] = []

    def unexpected_runner(*args: object, **kwargs: object) -> None:
        calls.append((args, kwargs))
        raise AssertionError("Windows deferred continuation must not start alignment")

    monkeypatch.setattr(continuation_module, "run_align_multicam", unexpected_runner)

    def forbidden_alignment_path(*args: object, **kwargs: object) -> object:
        raise AssertionError("Windows guard was reached after durable continuation reads")

    for name in (
        "_require_continuation_profile",
        "_validate_continuation_identity",
        "_existing_record",
    ):
        monkeypatch.setattr(continuation_module, name, forbidden_alignment_path)

    root = _workflow_project(tmp_path / "windows-required-adopt")
    adopted = _adopt_required_setup(root)
    continuation = adopted.workflow_run.multicam_alignment_continuation
    assert continuation is not None
    stored_run = WorkflowStore(root).read_run("wfr_test")
    stored_continuation = stored_run.multicam_alignment_continuation
    assert stored_continuation == continuation
    assert continuation.requirement == "required"
    assert continuation.operation_id is not None
    assert continuation.alignment_id is not None
    assert continuation.request_hash is not None
    assert continuation.writer_profile_name == AUDALIGN_CORRELATION_PROFILE_NAME
    assert continuation.writer_profile_version == AUDALIGN_CORRELATION_PROFILE_VERSION
    assert continuation.writer_profile_hash == AUDALIGN_CORRELATION_WRITER_PROFILE_SHA256
    assert adopted.receipt is not None
    assert adopted.receipt.action == "adopt_roughcut"
    assert adopted.status["multicam_alignment"]["status"] == "failed"
    assert adopted.status["multicam_alignment"]["failure"]["code"] == (
        "alignment_runtime_unavailable"
    )
    assert calls == []
    media_root = root / "workflow" / "operations" / "media"
    assert not media_root.exists() or not list(media_root.glob("*.json"))


def test_windows_required_workflow_status_is_deterministic_without_operational_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(m2_7_public_capability.platform, "system", lambda: "Windows")
    root = _workflow_project(tmp_path / "windows-required-status")
    calls: list[object] = []

    def unexpected_runner(*args: object, **kwargs: object) -> None:
        calls.append((args, kwargs))
        raise AssertionError("Windows deferred continuation must not start alignment")

    monkeypatch.setattr(continuation_module, "run_align_multicam", unexpected_runner)
    adopted = _adopt_required_setup(root)
    continuation = adopted.workflow_run.multicam_alignment_continuation
    assert continuation is not None
    assert continuation.operation_id is not None

    class ForbiddenMediaOperationStore:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("status must guard before MediaOperationStore")

    class ForbiddenAlignmentStore:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("status must guard before alignment artifacts")

    def forbidden_operational_path(*args: object, **kwargs: object) -> object:
        raise AssertionError("status must guard before continuation revalidation")

    monkeypatch.setattr(
        continuation_module,
        "MediaOperationStore",
        ForbiddenMediaOperationStore,
    )
    monkeypatch.setattr(continuation_module, "AlignmentStore", ForbiddenAlignmentStore)
    for name in ("_request_facts", "_validate_continuation_identity", "_existing_record"):
        monkeypatch.setattr(continuation_module, name, forbidden_operational_path)

    first = workflow_status(root, "wfr_test")
    second = workflow_status(root, "wfr_test")
    expected = {
        "schema_version": 1,
        "status": "failed",
        "operation_id": continuation.operation_id,
        "alignment_ref": None,
        "failure": {
            "code": "alignment_runtime_unavailable",
            "responsibility": "roughcut_core",
            "action": "validate_alignment_basis",
            "message_code": "alignment_failed",
        },
    }
    assert first["multicam_alignment"] == expected
    assert second == first
    assert calls == []
    media_root = root / "workflow" / "operations" / "media"
    assert not media_root.exists() or not list(media_root.glob("*.json"))


def test_abandoned_pending_continuation_is_read_as_interrupted(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = _workflow_project(tmp_path)
    setup = {
        "schema_version": 1,
        "main_camera": {"camera_id": "main", "ordered_source_ids": ["src_a"]},
        "auxiliary_cameras": [
            {"camera_id": "aux", "ordered_source_ids": ["src_b"]}
        ],
        "source_pairs": [
            {"main_source_id": "src_a", "auxiliary_source_id": "src_b"}
        ],
    }
    monkeypatch.setattr(continuation_module, "run_align_multicam", lambda *args, **kwargs: None)
    submitted = _submit_two_binding_draft(root, multicam_setup=setup)
    mutation = submitted.receipt.mutation
    assert mutation is not None
    approved = workflow_action(
        root,
        "wfr_test",
        "act_pending_approve",
        "approve_draft",
        {
            "schema_version": 1,
            "content_draft_ref": {
                "artifact_id": mutation.artifact_id,
                "schema_version": mutation.schema_version,
                "content_hash": mutation.content_hash,
            },
        },
    )
    proposal = approved.workflow_run.artifact_refs["proposal"]
    assert proposal is not None
    adopted = workflow_action(
        root,
        "wfr_test",
        "act_pending_adopt",
        "adopt_roughcut",
        {"schema_version": 1, "proposal_ref": proposal.to_dict()},
    )
    assert adopted.status["multicam_alignment"]["status"] == "pending"
    continuation = adopted.workflow_run.multicam_alignment_continuation
    assert continuation is not None
    assert continuation.operation_id is not None
    project = ProjectStore(root).load()
    store = MediaOperationStore(root, project.project_id)
    record = MediaOperationRecord(
        operation_id=continuation.operation_id,
        scope=store.scope,
        operation_type="align_multicam",
        request_hash=continuation.request_hash or "a" * 64,
        input_hash="b" * 64,
        status="pending",
        phase_message_code="alignment_preparing",
        created_at="2026-08-24T00:00:00.000000Z",
        started_at=None,
        updated_at="2026-08-24T00:00:00.000000Z",
        finished_at=None,
        result_ref=None,
        error=None,
        schema_version=2,
    )
    with store.writer(continuation.operation_id, create=True) as acquired:
        assert acquired
        store.write_locked(record)
    status = workflow_status(root, "wfr_test")
    assert status["multicam_alignment"]["status"] == "interrupted"
    assert status["multicam_alignment"]["failure"]["code"] == "alignment_interrupted"


def test_mismatched_operation_record_is_rejected_without_status_guessing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = _workflow_project(tmp_path)
    setup = {
        "schema_version": 1,
        "main_camera": {"camera_id": "main", "ordered_source_ids": ["src_a"]},
        "auxiliary_cameras": [
            {"camera_id": "aux", "ordered_source_ids": ["src_b"]}
        ],
        "source_pairs": [
            {"main_source_id": "src_a", "auxiliary_source_id": "src_b"}
        ],
    }
    monkeypatch.setattr(continuation_module, "run_align_multicam", lambda *args, **kwargs: None)
    submitted = _submit_two_binding_draft(root, multicam_setup=setup)
    mutation = submitted.receipt.mutation
    assert mutation is not None
    approved = workflow_action(
        root,
        "wfr_test",
        "act_mismatch_approve",
        "approve_draft",
        {
            "schema_version": 1,
            "content_draft_ref": {
                "artifact_id": mutation.artifact_id,
                "schema_version": mutation.schema_version,
                "content_hash": mutation.content_hash,
            },
        },
    )
    proposal = approved.workflow_run.artifact_refs["proposal"]
    assert proposal is not None
    adopted = workflow_action(
        root,
        "wfr_test",
        "act_mismatch_adopt",
        "adopt_roughcut",
        {"schema_version": 1, "proposal_ref": proposal.to_dict()},
    )
    continuation = adopted.workflow_run.multicam_alignment_continuation
    assert continuation is not None
    assert continuation.operation_id is not None
    project = ProjectStore(root).load()
    store = MediaOperationStore(root, project.project_id)
    mismatched = MediaOperationRecord(
        operation_id=continuation.operation_id,
        scope=store.scope,
        operation_type="align_multicam",
        request_hash="f" * 64,
        input_hash="e" * 64,
        status="failed",
        phase_message_code="alignment_failed",
        created_at="2026-08-24T00:00:00.000000Z",
        started_at="2026-08-24T00:00:01.000000Z",
        updated_at="2026-08-24T00:00:02.000000Z",
        finished_at="2026-08-24T00:00:02.000000Z",
        result_ref=None,
        error=MediaOperationFailure(
            code="alignment_input_stale",
            responsibility="roughcut_core",
            action="validate_alignment_basis",
            message_code="alignment_failed",
        ),
        schema_version=2,
    )
    with store.writer(continuation.operation_id, create=True) as acquired:
        assert acquired
        store.write_locked(mismatched)
    with pytest.raises(WorkflowError) as rejected:
        workflow_status(root, "wfr_test")
    assert rejected.value.code == "workflow_integrity_error"


def test_published_alignment_artifact_loss_is_closed_after_adopt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = _workflow_project(tmp_path)
    setup = {
        "schema_version": 1,
        "main_camera": {"camera_id": "main", "ordered_source_ids": ["src_a"]},
        "auxiliary_cameras": [
            {"camera_id": "aux", "ordered_source_ids": ["src_b"]}
        ],
        "source_pairs": [
            {"main_source_id": "src_a", "auxiliary_source_id": "src_b"}
        ],
    }
    monkeypatch.setattr(
        continuation_module,
        "run_align_multicam",
        _fake_success_runner(root, partial=False),
    )
    submitted = _submit_two_binding_draft(root, multicam_setup=setup)
    mutation = submitted.receipt.mutation
    assert mutation is not None
    approved = workflow_action(
        root,
        "wfr_test",
        "act_corrupt_approve",
        "approve_draft",
        {
            "schema_version": 1,
            "content_draft_ref": {
                "artifact_id": mutation.artifact_id,
                "schema_version": mutation.schema_version,
                "content_hash": mutation.content_hash,
            },
        },
    )
    proposal = approved.workflow_run.artifact_refs["proposal"]
    assert proposal is not None
    adopted = workflow_action(
        root,
        "wfr_test",
        "act_corrupt_adopt",
        "adopt_roughcut",
        {"schema_version": 1, "proposal_ref": proposal.to_dict()},
    )
    continuation = adopted.workflow_run.multicam_alignment_continuation
    assert continuation is not None
    assert continuation.alignment_id is not None
    artifact_path = AlignmentStore(root)._artifact_path(continuation.alignment_id)
    artifact_path.unlink()
    with pytest.raises(WorkflowError) as rejected:
        workflow_status(root, "wfr_test")
    assert rejected.value.code == "workflow_integrity_error"


def test_adopt_with_aux_runs_exact_confirmed_request_and_durably_closes_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = _workflow_project(tmp_path)
    calls: list[dict[str, object]] = []
    setup = {
        "schema_version": 1,
        "main_camera": {"camera_id": "main", "ordered_source_ids": ["src_a"]},
        "auxiliary_cameras": [
            {"camera_id": "aux", "ordered_source_ids": ["src_b"]}
        ],
        "source_pairs": [
            {"main_source_id": "src_a", "auxiliary_source_id": "src_b"}
        ],
    }

    def failed_runner(*args: object, **kwargs: object) -> object:
        calls.append(kwargs)
        raise MediaOperationError(
            "alignment_runtime_unavailable", "fixture runtime is unavailable"
        )

    monkeypatch.setattr(continuation_module, "run_align_multicam", failed_runner)
    submitted = _submit_two_binding_draft(root, multicam_setup=setup)
    mutation = submitted.receipt.mutation
    assert mutation is not None
    approved = workflow_action(
        root,
        "wfr_test",
        "act_aux_approve",
        "approve_draft",
        {
            "schema_version": 1,
            "content_draft_ref": {
                "artifact_id": mutation.artifact_id,
                "schema_version": mutation.schema_version,
                "content_hash": mutation.content_hash,
            },
        },
    )
    proposal = approved.workflow_run.artifact_refs["proposal"]
    assert proposal is not None

    adopted = workflow_action(
        root,
        "wfr_test",
        "act_aux_adopt",
        "adopt_roughcut",
        {"schema_version": 1, "proposal_ref": proposal.to_dict()},
    )

    assert len(calls) == 1
    assert calls[0]["main_camera"] == setup["main_camera"]
    assert calls[0]["auxiliary_cameras"] == [
        {
            **setup["auxiliary_cameras"][0],
            "source_pairs": setup["source_pairs"],
        }
    ]
    assert calls[0]["main_audio_stable"] is True
    continuation = adopted.workflow_run.multicam_alignment_continuation
    assert continuation is not None
    assert continuation.requirement == "required"
    assert adopted.status["multicam_alignment"]["status"] == "failed"
    failure = adopted.status["multicam_alignment"]["failure"]
    assert failure["code"] == "alignment_runtime_unavailable"
    assert "approve_export" in adopted.status["allowed_actions"]

    retried = workflow_action(
        root,
        "wfr_test",
        "act_aux_adopt",
        "adopt_roughcut",
        {"schema_version": 1, "proposal_ref": proposal.to_dict()},
    )
    assert retried.receipt == adopted.receipt
    assert len(calls) == 1
    assert retried.status["multicam_alignment"] == adopted.status["multicam_alignment"]


@pytest.mark.parametrize("partial, expected_status", [(False, "succeeded"), (True, "partial")])
def test_adopt_reads_exact_published_alignment_and_reuses_terminal_operation(
    tmp_path: Path,
    monkeypatch,
    partial: bool,
    expected_status: str,
) -> None:
    root = _workflow_project(tmp_path)
    setup = {
        "schema_version": 1,
        "main_camera": {"camera_id": "main", "ordered_source_ids": ["src_a"]},
        "auxiliary_cameras": [
            {"camera_id": "aux", "ordered_source_ids": ["src_b"]}
        ],
        "source_pairs": [
            {"main_source_id": "src_a", "auxiliary_source_id": "src_b"}
        ],
    }
    calls: list[object] = []
    fake = _fake_success_runner(root, partial=partial)

    def counting_runner(*args: object, **kwargs: object) -> AlignmentOutcome:
        calls.append((args, kwargs))
        return fake(*args, **kwargs)

    monkeypatch.setattr(continuation_module, "run_align_multicam", counting_runner)
    submitted = _submit_two_binding_draft(root, multicam_setup=setup)
    mutation = submitted.receipt.mutation
    assert mutation is not None
    approved = workflow_action(
        root,
        "wfr_test",
        "act_terminal_approve",
        "approve_draft",
        {
            "schema_version": 1,
            "content_draft_ref": {
                "artifact_id": mutation.artifact_id,
                "schema_version": mutation.schema_version,
                "content_hash": mutation.content_hash,
            },
        },
    )
    proposal = approved.workflow_run.artifact_refs["proposal"]
    assert proposal is not None
    adopted = workflow_action(
        root,
        "wfr_test",
        "act_terminal_adopt",
        "adopt_roughcut",
        {"schema_version": 1, "proposal_ref": proposal.to_dict()},
    )
    assert len(calls) == 1
    continuation = adopted.workflow_run.multicam_alignment_continuation
    assert continuation is not None
    assert continuation.writer_profile_name == AUDALIGN_CORRELATION_PROFILE_NAME
    assert continuation.writer_profile_version == AUDALIGN_CORRELATION_PROFILE_VERSION
    assert continuation.writer_profile_hash == AUDALIGN_CORRELATION_WRITER_PROFILE_SHA256
    assert continuation.operation_id is not None
    operation = MediaOperationStore(root, adopted.workflow_run.project_id).read(
        continuation.operation_id
    )
    assert operation is not None
    assert operation.request_hash == continuation.request_hash
    assert adopted.status["multicam_alignment"]["status"] == expected_status, adopted.status
    alignment_ref = adopted.status["multicam_alignment"]["alignment_ref"]
    assert alignment_ref["alignment_id"].startswith("aln_mc_")
    before_run = (root / "workflow" / "runs" / "wfr_test.json").read_bytes()
    before_operation = next(
        (root / "workflow" / "operations" / "media").glob("*.json")
    ).read_bytes()

    repeated = workflow_action(
        root,
        "wfr_test",
        "act_terminal_adopt",
        "adopt_roughcut",
        {"schema_version": 1, "proposal_ref": proposal.to_dict()},
    )
    assert len(calls) == 1
    assert repeated.receipt == adopted.receipt
    assert repeated.status["multicam_alignment"] == adopted.status["multicam_alignment"]
    assert (root / "workflow" / "runs" / "wfr_test.json").read_bytes() == before_run
    assert next(
        (root / "workflow" / "operations" / "media").glob("*.json")
    ).read_bytes() == before_operation


def test_exact_026_bbc_continuation_reads_correlation_operation_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _workflow_project(tmp_path)
    (
        legacy_run,
        legacy_hash,
        correlation_hash,
        operation_path,
        artifact_path,
    ) = _prepare_legacy_bbc_state(root, monkeypatch)
    run_path = root / "workflow" / "runs" / "wfr_test.json"
    before = {
        path: path.read_bytes()
        for path in (run_path, operation_path, artifact_path)
    }
    continuation = legacy_run.multicam_alignment_continuation
    assert continuation is not None
    assert continuation.writer_profile_name == BBC_PROFILE_NAME
    assert continuation.writer_profile_version == BBC_PROFILE_VERSION
    assert continuation.writer_profile_hash == canonical_sha256_v1(BBC_WRITER_PROFILE)
    assert continuation.request_hash == legacy_hash
    assert legacy_hash != correlation_hash

    status = workflow_status(root, "wfr_test")
    operation = MediaOperationStore(root, legacy_run.project_id).read(
        continuation.operation_id or ""
    )
    assert operation is not None

    assert status["multicam_alignment"]["status"] == "succeeded"
    assert status["multicam_alignment"]["alignment_ref"]["alignment_id"] == continuation.alignment_id
    assert operation.request_hash == correlation_hash
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize(
    "target, field",
    [
        ("continuation", "request_hash"),
        ("continuation", "writer_profile_name"),
        ("continuation", "writer_profile_version"),
        ("continuation", "writer_profile_hash"),
        ("continuation", "setup_id"),
        ("continuation", "setup_hash"),
        ("continuation", "operation_id"),
        ("continuation", "alignment_id"),
        ("continuation", "expected_revision"),
        ("continuation", "main_audio_stable"),
        ("operation", "operation_id"),
        ("operation", "scope"),
        ("operation", "request_hash"),
        ("result", "alignment_id"),
        ("result", "content_hash"),
        ("artifact", "alignment_id"),
        ("artifact", "producer_operation_id"),
        ("artifact", "request_hash"),
        ("artifact", "input_hash"),
        ("artifact", "created_at"),
    ],
)
def test_exact_026_bbc_compatibility_rejects_forged_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    field: str,
) -> None:
    root = _workflow_project(tmp_path)
    legacy_run, _legacy_hash, _correlation_hash, operation_path, artifact_path = (
        _prepare_legacy_bbc_state(root, monkeypatch)
    )
    run_path = root / "workflow" / "runs" / "wfr_test.json"
    if target == "continuation":
        payload = json.loads(run_path.read_text(encoding="utf-8"))
        continuation_payload = payload["multicam_alignment_continuation"]
        legacy_continuation = legacy_run.multicam_alignment_continuation
        assert legacy_continuation is not None
        if field == "expected_revision":
            continuation_payload[field] = legacy_continuation.expected_revision + 1
        elif field == "main_audio_stable":
            continuation_payload[field] = False
        elif field == "writer_profile_version":
            continuation_payload[field] = BBC_PROFILE_VERSION + 1
        elif field == "writer_profile_name":
            continuation_payload[field] = "forged_bbc_profile"
        elif field in {"setup_id", "operation_id", "alignment_id"}:
            continuation_payload[field] = {
                "setup_id": "mcs_forged",
                "operation_id": "op_mc_forged",
                "alignment_id": "aln_mc_forged",
            }[field]
        else:
            continuation_payload[field] = "f" * 64
        path = run_path
    elif target == "operation":
        payload = json.loads(operation_path.read_text(encoding="utf-8"))
        if field == "operation_id":
            payload[field] = "op_mc_forged"
        elif field == "scope":
            payload[field]["project_root_hash"] = "f" * 64
        else:
            payload[field] = "f" * 64
        path = operation_path
    elif target == "result":
        payload = json.loads(operation_path.read_text(encoding="utf-8"))
        payload["result_ref"][field] = (
            "aln_mc_forged" if field == "alignment_id" else "f" * 64
        )
        path = operation_path
    else:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        payload[field] = (
            "aln_mc_forged"
            if field == "alignment_id"
            else "2026-08-24T00:00:03.000000Z"
            if field == "created_at"
            else "op_mc_forged"
            if field == "producer_operation_id"
            else "f" * 64
        )
        path = artifact_path
    path.write_bytes(canonical_json_v1(payload) + b"\n")

    with pytest.raises(WorkflowError) as rejected:
        workflow_status(root, "wfr_test")
    assert rejected.value.code == "workflow_integrity_error"


def test_gate6_public_status_and_new_process_readback_do_not_rerun_alignment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = _workflow_project(tmp_path)
    setup = {
        "schema_version": 1,
        "main_camera": {"camera_id": "main", "ordered_source_ids": ["src_a"]},
        "auxiliary_cameras": [
            {"camera_id": "aux", "ordered_source_ids": ["src_b"]}
        ],
        "source_pairs": [
            {"main_source_id": "src_a", "auxiliary_source_id": "src_b"}
        ],
    }
    calls: list[object] = []
    fake = _fake_success_runner(root, partial=False)

    def counting_runner(*args: object, **kwargs: object) -> AlignmentOutcome:
        calls.append((args, kwargs))
        return fake(*args, **kwargs)

    monkeypatch.setattr(continuation_module, "run_align_multicam", counting_runner)
    submitted = _submit_two_binding_draft(root, multicam_setup=setup)
    mutation = submitted.receipt.mutation
    assert mutation is not None
    approved = workflow_action(
        root,
        "wfr_test",
        "act_gate6_approve",
        "approve_draft",
        {
            "schema_version": 1,
            "content_draft_ref": {
                "artifact_id": mutation.artifact_id,
                "schema_version": mutation.schema_version,
                "content_hash": mutation.content_hash,
            },
        },
    )
    proposal = approved.workflow_run.artifact_refs["proposal"]
    assert proposal is not None
    adopted = workflow_action(
        root,
        "wfr_test",
        "act_gate6_adopt",
        "adopt_roughcut",
        {"schema_version": 1, "proposal_ref": proposal.to_dict()},
    )
    public = handle_request(
        {
            "jsonrpc": "2.0",
            "id": "gate6",
            "method": "tools/call",
            "params": {
                "name": "workflow_status",
                "arguments": {"project_path": str(root), "run_id": "wfr_test"},
            },
        }
    )
    assert public is not None
    public_status = public["result"]["structuredContent"]["status"]
    assert public_status["multicam_alignment"] == adopted.status["multicam_alignment"]
    environment = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).parents[2] / "src"),
    }
    reopened = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, sys; "
                "from pathlib import Path; "
                "from roughcut import m2_7_public_capability; "
                "m2_7_public_capability.platform.system = lambda: 'Darwin'; "
                "from roughcut.application.workflows import workflow_status; "
                "print(json.dumps(workflow_status(Path(sys.argv[1]), 'wfr_test'), sort_keys=True))"
            ),
            str(root),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert reopened.returncode == 0, reopened.stderr
    assert json.loads(reopened.stdout)["multicam_alignment"]["status"] == "succeeded"
    assert len(calls) == 1


def test_gate6_public_end_to_end_declaration_to_alignment_readback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = _workflow_project(tmp_path)
    _add_fixture_source(root, "src_b")
    setup = {
        "schema_version": 1,
        "main_camera": {"camera_id": "main", "ordered_source_ids": ["src_a"]},
        "auxiliary_cameras": [
            {"camera_id": "aux", "ordered_source_ids": ["src_b"]}
        ],
        "source_pairs": [
            {"main_source_id": "src_a", "auxiliary_source_id": "src_b"}
        ],
    }
    calls: list[object] = []
    fake = _fake_success_runner(root, partial=False)

    def counting_runner(*args: object, **kwargs: object) -> AlignmentOutcome:
        calls.append((args, kwargs))
        return fake(*args, **kwargs)

    monkeypatch.setattr(continuation_module, "run_align_multicam", counting_runner)
    started = _public_call(
        "workflow_start",
        {
            "project_path": str(root),
            "run_id": "wfr_test",
            "ordered_source_ids": ["src_a"],
        },
    )
    assert started["status"]["multicam_alignment"] is None
    scope_input = {
        "schema_version": 1,
        "confirmation_basis": started["status"]["confirmation_bases"]["scope"][
            "basis"
        ],
        "source_authorizations": [
            {
                "source_id": "src_a",
                "transcribe": False,
                "speaker_diarization": False,
            }
        ],
        "multicam_setup": setup,
    }
    scoped = _public_call(
        "workflow_action",
        {
            "project_path": str(root),
            "run_id": "wfr_test",
            "action_id": "gate6_scope",
            "action": "approve_scope",
            "input": scope_input,
        },
    )
    _public_call(
        "workflow_action",
        {
            "project_path": str(root),
            "run_id": "wfr_test",
            "action_id": "gate6_brief",
            "action": "confirm_brief",
            "input": {
                "schema_version": 1,
                "confirmation_basis": scoped["status"]["confirmation_bases"][
                    "brief"
                ]["basis"],
                "theme": "双机位",
                "target_duration_ticks": 240_000,
                "focus": ["绑定"],
                "allow_reorder": False,
                "speaker_resolution_waivers": [],
            },
        },
    )
    outline = {
        "schema_version": 1,
        "title": "双机位",
        "opening": "A",
        "sections": [
            {
                "section_id": f"section_{index}",
                "title": title,
                "summary": title,
                "target_duration_ticks": 120_000,
            }
            for index, title in enumerate(("A", "B", "C", "D"), start=1)
        ],
        "ending": "B",
        "required_content_coverage": [],
        "narration_status": "none",
    }
    outlined = _public_call(
        "workflow_action",
        {
            "project_path": str(root),
            "run_id": "wfr_test",
            "action_id": "gate6_outline",
            "action": "submit_outline",
            "input": outline,
        },
    )
    outline_ref = outlined["status"]["presented_subjects"]["outline_ref"]
    approved_outline = _public_call(
        "workflow_action",
        {
            "project_path": str(root),
            "run_id": "wfr_test",
            "action_id": "gate6_outline_approve",
            "action": "approve_outline",
            "input": {"schema_version": 1, "outline_ref": outline_ref},
        },
    )
    workflow_run = approved_outline["workflow_run"]
    assert isinstance(workflow_run, dict)
    brief_ref = workflow_run["artifact_refs"]["brief"]
    assert isinstance(brief_ref, dict)
    brief = EditBrief.from_dict(
        json.loads(
            (root / "briefs" / f"{brief_ref['artifact_id']}.json").read_text(
                encoding="utf-8"
            )
        )
    )
    context_hash = calculate_agent_context_hash(
        root,
        project=ProjectStore(root).load(),
        bindings=(SourceTranscriptBinding("src_a", "tr_a"),),
        brief=brief,
    )
    submitted = _public_call(
        "workflow_action",
        {
            "project_path": str(root),
            "run_id": "wfr_test",
            "action_id": "gate6_draft",
            "action": "submit_draft",
            "input": {
                "schema_version": 1,
                "parent_draft_ref": None,
                "display_title": "双机位",
                "source_bindings": [
                    {"source_id": "src_a", "transcript_version_id": "tr_a"},
                ],
                "brief_ref": brief_ref,
                "context_hash": context_hash,
                "blocks": [
                    {
                        "block_id": "block_src_a",
                        "kind": "source_excerpt",
                        "refs": [
                            {
                                "source_id": "src_a",
                                "transcript_version_id": "tr_a",
                                "segment_id": "seg_1",
                                "start_ticks": 0,
                                "end_ticks": 120_000,
                            }
                        ],
                        "canonical_text": "开场。",
                    },
                ],
                "scoped_mutable_block_ids": [],
            },
        },
    )
    mutation = submitted["receipt"]["mutation"]
    assert isinstance(mutation, dict)
    approved_draft = _public_call(
        "workflow_action",
        {
            "project_path": str(root),
            "run_id": "wfr_test",
            "action_id": "gate6_draft_approve",
            "action": "approve_draft",
            "input": {
                "schema_version": 1,
                "content_draft_ref": {
                    key: mutation[key]
                    for key in ("artifact_id", "schema_version", "content_hash")
                },
            },
        },
    )
    proposal_ref = approved_draft["workflow_run"]["artifact_refs"]["proposal"]
    adopted = _public_call(
        "workflow_action",
        {
            "project_path": str(root),
            "run_id": "wfr_test",
            "action_id": "gate6_adopt",
            "action": "adopt_roughcut",
            "input": {"schema_version": 1, "proposal_ref": proposal_ref},
        },
    )
    assert adopted["status"]["multicam_alignment"]["status"] == "succeeded"
    assert adopted["workflow_run"]["multicam_alignment_continuation"][
        "main_audio_stable"
    ] is True
    assert len(calls) == 1
    reopened = _public_call(
        "workflow_status",
        {"project_path": str(root), "run_id": "wfr_test"},
    )
    assert reopened["status"]["multicam_alignment"] == adopted["status"][
        "multicam_alignment"
    ]
    repeated = _public_call(
        "workflow_action",
        {
            "project_path": str(root),
            "run_id": "wfr_test",
            "action_id": "gate6_adopt",
            "action": "adopt_roughcut",
            "input": {"schema_version": 1, "proposal_ref": proposal_ref},
        },
    )
    assert repeated["receipt"] == adopted["receipt"]
    assert len(calls) == 1
