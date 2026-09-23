"""Pure prepare and tracked operation coordinator for parallel multicam output."""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar, cast

from roughcut.adapters.alignment_store import AlignmentStore
from roughcut.adapters.ffmpeg.multicam_parallel import (
    ParallelCameraEncodeError,
    ParallelCameraStagingError,
    ParallelCameraVerifyError,
    bounded_stderr_tail,
    render_parallel_camera,
    verify_parallel_camera,
)
from roughcut.adapters.media_operation_store import MediaOperationStore
from roughcut.adapters.multicam_parallel_store import MulticamParallelStore
from roughcut.adapters.project_store import ProjectStore
from roughcut.adapters.runtime_binding import RuntimeBindingError
from roughcut.application.media_operations import (
    _load_persistent_runtime,
    _new_pending_record,
    _phase_record,
    _project_context,
    _running_record,
    _terminal_record,
    _validate_media_runtime,
)
from roughcut.application.renders import _resolve_source_path
from roughcut.application.sources import fingerprint_file
from roughcut.application.workflows import _roughcut_dependency
from roughcut.domain.alignment import (
    AlignmentCamera,
    AlignmentError,
    AlignmentInterval,
    MulticamAlignmentArtifact,
    project_alignment_intervals,
)
from roughcut.domain.edit import (
    EditClip,
    EditDecision,
    EditProposal,
    MultiSourceEditDecision,
    MultiSourceEditProposal,
)
from roughcut.domain.media_operation import (
    AlignmentOperationResult,
    MediaOperationError,
    MediaOperationFailure,
    MediaOperationRecord,
    ParallelFailureEvidence,
    ParallelRenderOperationResult,
    hash_parallel_render_request,
    parallel_render_request_projection,
    validate_media_operation_id,
)
from roughcut.domain.multicam_parallel import (
    PARALLEL_OUTPUT_PROFILE,
    ParallelPrepareRef,
    ParallelPrepareSummary,
    ParallelRenderError,
    build_prepare_ref,
    frame_boundary,
    output_settings_hash,
    parallel_render_id,
    sample_boundary,
    validate_output_settings,
    validate_parallel_prepare_pair,
)
from roughcut.domain.project import Project, ProjectError, SourceAsset
from roughcut.domain.time import TICKS_PER_SECOND
from roughcut.domain.workflow import (
    ActionReceipt,
    ApprovalRecord,
    ArtifactRef,
    SubjectRef,
    WorkflowRun,
    canonical_sha256_v1,
    load_closed_json,
    subject_content_hash,
)


@dataclass(frozen=True)
class ParallelPrepareOutcome:
    prepare_ref: dict[str, object]
    summary: dict[str, object]
    basis: dict[str, object]


@dataclass(frozen=True)
class ParallelRenderOutcome:
    record: MediaOperationRecord
    result: dict[str, object] | None
    readback: bool


_ResultT = TypeVar("_ResultT")


def _capture_exception(
    operation: Callable[[], _ResultT],
) -> tuple[_ResultT | None, Exception | None]:
    """Capture malformed-candidate errors without changing scan semantics."""
    try:
        return operation(), None
    except Exception as error:  # noqa: BLE001 - malformed evidence is skipped as one boundary
        return None, error


def prepare_multicam_parallel_render(
    project_path: Path,
    *,
    edit_version_id: str,
    alignment_ref: dict[str, object],
    auxiliary_camera_ids: list[str],
    expected_revision: int,
) -> ParallelPrepareOutcome:
    """Build a closed prepare ref and summary without writing any Project state."""
    _validate_prepare_request(
        edit_version_id=edit_version_id,
        alignment_ref=alignment_ref,
        auxiliary_camera_ids=auxiliary_camera_ids,
        expected_revision=expected_revision,
    )
    root, project, _media_store = _project_context(Path(project_path))
    if project.revision != expected_revision:
        raise ParallelRenderError(
            "parallel_render_prepare_revision_conflict",
            "expected Project revision differs from the current Project",
        )
    alignment, producer = _read_deliverable_alignment(root, project, alignment_ref)
    decision, decision_ref, adoption = _read_adopted_decision(root, project, edit_version_id)
    settings = validate_output_settings(
        {
            "width": project.settings["width"],
            "height": project.settings["height"],
            "frame_rate": project.settings["frame_rate"],
            "audio_sample_rate": project.settings["audio_sample_rate"],
        }
    )
    settings_hash = output_settings_hash(settings)
    clips = tuple(decision.proposal_snapshot.clips)
    total_ticks = sum(clip.duration_ticks for clip in clips)
    if total_ticks <= 0:
        raise ParallelRenderError(
            "parallel_render_decision_not_adopted",
            "adopted Decision has no positive output duration",
        )
    source_by_id = {source.source_id: source for source in project.sources}
    frame_rate = cast(dict[str, object], settings["frame_rate"])
    frame_rate_numerator = cast(int, frame_rate["numerator"])
    frame_rate_denominator = cast(int, frame_rate["denominator"])
    sample_rate = cast(int, settings["audio_sample_rate"])
    cameras: list[dict[str, object]] = []
    for camera_id in auxiliary_camera_ids:
        camera = _camera(alignment, camera_id)
        if camera.status not in {"complete", "partial"}:
            raise ParallelRenderError(
                "parallel_render_camera_not_deliverable",
                "selected camera is not deliverable",
            )
        slots = _project_camera_slots(camera, alignment.intervals, clips, settings)
        mapped = sum(cast(int, slot["output_end_ticks"]) - cast(int, slot["output_start_ticks"]) for slot in slots if slot["classification"] == "mapped")
        if mapped <= 0:
            raise ParallelRenderError(
                "parallel_render_camera_not_deliverable",
                "selected camera has no mapped ticks in the current Decision",
            )
        _assert_selected_sources_current(root, source_by_id, camera, slots)
        missing = sum(cast(int, slot["output_end_ticks"]) - cast(int, slot["output_start_ticks"]) for slot in slots if slot["classification"] == "missing")
        uncertain = sum(cast(int, slot["output_end_ticks"]) - cast(int, slot["output_start_ticks"]) for slot in slots if slot["classification"] == "uncertain")
        conflict = sum(cast(int, slot["output_end_ticks"]) - cast(int, slot["output_start_ticks"]) for slot in slots if slot["classification"] == "conflict")
        cameras.append(
            {
                "camera_id": camera_id,
                "alignment_status": camera.status,
                "coverage_status": "complete" if mapped == total_ticks else "partial",
                "render_status": "planned",
                "mapped_ticks": mapped,
                "missing_ticks": missing,
                "uncertain_ticks": uncertain,
                "conflict_ticks": conflict,
                "black_silence_ticks": missing + uncertain + conflict,
                "planned_output": {
                    "filename": f"camera_{camera_id}.mp4",
                    "relative_path": f"camera_{camera_id}.mp4",
                },
                "slots": slots,
            }
        )
    frame_quota = frame_boundary(
        total_ticks,
        frame_rate_numerator,
        frame_rate_denominator,
    )
    sample_quota = sample_boundary(total_ticks, sample_rate)
    estimate = _temporary_disk_estimate(
        total_ticks=total_ticks,
        frame_quota=frame_quota,
        sample_quota=sample_quota,
        camera_count=len(cameras),
        settings=settings,
    )
    basis: dict[str, object] = {
        "kind": "multicam_parallel_prepare_basis",
        "schema_version": 1,
        "project": {"project_id": project.project_id, "revision": project.revision},
        "decision": {"ref": decision_ref, "adoption": adoption},
        "alignment": {"ref": alignment_ref, "producer": producer},
        "auxiliary_camera_ids": list(auxiliary_camera_ids),
        "output_profile": PARALLEL_OUTPUT_PROFILE,
        "output_settings": settings,
        "output_settings_hash": settings_hash,
        "total_ticks": total_ticks,
        "video_frame_quota": frame_quota,
        "audio_sample_quota": sample_quota,
        "estimated_temporary_disk_bytes": estimate,
        "manifest": {"filename": "manifest.json", "relative_path": "manifest.json"},
        "cameras": cameras,
    }
    prepare_ref = build_prepare_ref(basis)
    summary = {
        "schema_version": 1,
        "prepare_id": prepare_ref["prepare_id"],
        "plan_basis_hash": prepare_ref["plan_basis_hash"],
        "project_id": project.project_id,
        "project_revision": project.revision,
        "decision": basis["decision"],
        "alignment": basis["alignment"],
        "output_profile": PARALLEL_OUTPUT_PROFILE,
        "output_settings": settings,
        "output_settings_hash": settings_hash,
        "total_ticks": total_ticks,
        "video_frame_quota": frame_quota,
        "audio_sample_quota": sample_quota,
        "estimated_temporary_disk_bytes": estimate,
        "manifest": basis["manifest"],
        "cameras": cameras,
    }
    # Domain validation is intentionally after derivation: callers cannot
    # inject a second summary or repair an identity by changing a field.
    ParallelPrepareSummary(summary)
    ParallelPrepareRef(prepare_ref)
    return ParallelPrepareOutcome(prepare_ref, summary, basis)


def start_multicam_parallel_render(
    project_path: Path,
    *,
    operation_id: str,
    prepare_ref: dict[str, object],
) -> ParallelRenderOutcome:
    """Existing-first start of one exact prepare ref and one new operation ID."""
    validate_media_operation_id(operation_id)
    try:
        parsed_prepare = ParallelPrepareRef(dict(prepare_ref))
    except ParallelRenderError as error:
        raise ParallelRenderError(
            "parallel_render_prepare_stale", "start prepare ref is stale"
        ) from error
    root, project, store = _project_context(Path(project_path))
    request_projection = parallel_render_request_projection(
        store.scope,
        operation_id=operation_id,
        prepare_ref=parsed_prepare.to_dict(),
    )
    request_hash = hash_parallel_render_request(request_projection)
    existing = store.read(operation_id, allow_writer_temp=True)
    if existing is not None:
        if existing.operation_type != "render_multicam_parallel" or existing.request_hash != request_hash:
            raise MediaOperationError(
                "operation_input_conflict",
                "same operation ID was already used for a different parallel render request",
            )
        return ParallelRenderOutcome(existing, _parallel_result(existing), True)

    try:
        prepare_value = parsed_prepare.value
        assert isinstance(prepare_value, dict)
        prepared = prepare_multicam_parallel_render(
            root,
            edit_version_id=cast(str, cast(dict[str, object], prepare_value["decision_ref"])["edit_version_id"]),
            alignment_ref=cast(dict[str, object], prepare_value["alignment_ref"]),
            auxiliary_camera_ids=cast(list[str], prepare_value["auxiliary_camera_ids"]),
            expected_revision=cast(int, prepare_value["project_revision"]),
        )
        validate_parallel_prepare_pair(parsed_prepare.to_dict(), prepared.basis)
    except ParallelRenderError as error:
        if error.code in {"parallel_render_prepare_revision_conflict", "parallel_render_decision_not_adopted", "parallel_render_alignment_not_deliverable", "parallel_render_camera_not_deliverable", "parallel_render_source_stale"}:
            raise ParallelRenderError("parallel_render_prepare_stale", "start prepare ref is stale") from error
        raise
    runtime = _load_runtime_for_parallel()
    source_by_id = {source.source_id: source for source in project.sources}
    mapped_source_ids = _mapped_source_ids(cast(list[dict[str, object]], prepared.summary["cameras"]))
    source_paths = _source_paths(root, source_by_id, mapped_source_ids)
    input_hash = _parallel_input_hash(prepared.basis, project, source_by_id, mapped_source_ids, runtime)
    parallel_store = MulticamParallelStore(root)
    with store.writer(operation_id, create=True) as acquired:
        if not acquired:
            raced = store.read(operation_id, allow_writer_temp=True)
            if raced is None:
                raise MediaOperationError("operation_integrity_error", "parallel render writer disappeared without a record")
            if raced.request_hash != request_hash or raced.operation_type != "render_multicam_parallel":
                raise MediaOperationError("operation_input_conflict", "parallel render operation request conflicted")
            return ParallelRenderOutcome(raced, _parallel_result(raced), True)
        raced = store.read(operation_id)
        if raced is not None:
            if raced.request_hash != request_hash or raced.operation_type != "render_multicam_parallel":
                raise MediaOperationError("operation_input_conflict", "parallel render operation request conflicted")
            return ParallelRenderOutcome(raced, _parallel_result(raced), True)
        active = _running_record(
            store.write_locked(
                _new_pending_record(
                    operation_id,
                    store.scope,
                    "render_multicam_parallel",
                    request_hash,
                    input_hash,
                )
            ),
            "parallel_render_preparing",
        )
        active = store.write_locked(active)
        staging: Path | None = None
        published = False
        try:
            try:
                _validate_media_runtime(runtime)
            except RuntimeBindingError as error:
                raise ParallelRenderError(
                    "parallel_render_runtime_changed_during_run",
                    "persistent FFmpeg runtime is unavailable",
                ) from error
            if shutil.disk_usage(root).free < cast(int, prepared.basis["estimated_temporary_disk_bytes"]):
                failed = _write_parallel_failure_record(
                    store,
                    active,
                    _parallel_failure("parallel_render_disk_budget_exceeded"),
                )
                return ParallelRenderOutcome(failed, None, False)
            staging = parallel_store.create_staging(operation_id)
            active = store.write_locked(_phase_record(active, "parallel_render_encoding"))
            manifest_cameras: list[dict[str, object]] = []
            successes = 0
            first_camera_failure: ParallelFailureEvidence | None = None
            for camera in cast(list[dict[str, object]], prepared.summary["cameras"]):
                camera_id = cast(str, camera["camera_id"])
                temp_output = staging / f".{camera_id}.mp4.tmp"
                output_path = staging / f"camera_{camera_id}.mp4"
                camera_result = dict(camera)
                try:
                    render_parallel_camera(
                        camera,
                        source_paths=source_paths,
                        sources=source_by_id,
                        output_path=temp_output,
                        settings=cast(dict[str, object], prepared.summary["output_settings"]),
                        ffmpeg=runtime.ffmpeg,
                    )
                    active = store.write_locked(_phase_record(active, "parallel_render_verifying"))
                    verification = verify_parallel_camera(
                        camera,
                        output_path=temp_output,
                        settings=cast(dict[str, object], prepared.summary["output_settings"]),
                        ffmpeg=runtime.ffmpeg,
                        ffprobe=runtime.ffprobe,
                    )
                    os.rename(temp_output, output_path)
                    camera_result["render_status"] = "succeeded"
                    camera_result["output"] = {
                        "filename": output_path.name,
                        "relative_path": output_path.name,
                        "project_relative_path": f"renders/multicam/{parallel_render_id(parsed_prepare.to_dict(), operation_id)}/{output_path.name}",
                        "bytes": verification.bytes,
                        "content_hash": verification.content_hash,
                    }
                    camera_result["error"] = None
                    successes += 1
                except ParallelCameraVerifyError as error:
                    _remove_camera_temps(temp_output, output_path)
                    try:
                        evidence = _parallel_verify_evidence(camera_id, error)
                    except (MediaOperationError, TypeError, ValueError):
                        evidence = _parallel_evidence_fallback()
                    if first_camera_failure is None:
                        first_camera_failure = evidence
                    camera_result["render_status"] = "failed"
                    camera_result["output"] = None
                    camera_result["error"] = {"code": "parallel_camera_verify_failed"}
                except ParallelCameraStagingError:
                    raise
                except ParallelCameraEncodeError as error:
                    _remove_camera_temps(temp_output, output_path)
                    try:
                        evidence = _parallel_encode_evidence(camera_id, error)
                    except (MediaOperationError, TypeError, ValueError):
                        evidence = _parallel_evidence_fallback()
                    if first_camera_failure is None:
                        first_camera_failure = evidence
                    camera_result["render_status"] = "failed"
                    camera_result["output"] = None
                    camera_result["error"] = {"code": "parallel_camera_encode_failed"}
                except OSError as error:
                    raise ParallelCameraStagingError("parallel camera staging operation failed") from error
                camera_result.pop("planned_output", None)
                manifest_cameras.append(camera_result)
            if successes == 0:
                try:
                    _cleanup_worker_staging(parallel_store, operation_id)
                except ParallelRenderError:
                    # The all-failed semantic is already determined by the
                    # camera outcomes; cleanup failure must not replace it.
                    pass
                active = _write_parallel_failure_record(
                    store,
                    active,
                    _parallel_failure(
                        "parallel_render_all_cameras_failed",
                        evidence=first_camera_failure,
                    ),
                )
                return ParallelRenderOutcome(active, None, False)
            render_id = parallel_render_id(parsed_prepare.to_dict(), operation_id)
            manifest = _manifest(
                prepared.summary,
                parsed_prepare.to_dict(),
                operation_id=operation_id,
                parallel_id=render_id,
                cameras=manifest_cameras,
            )
            parallel_store.write_manifest(staging, manifest)
            active = store.write_locked(_phase_record(active, "parallel_render_revalidating_basis"))
            try:
                current_runtime = _load_runtime_for_parallel()
            except ParallelRenderError as error:
                raise ParallelRenderError(
                    "parallel_render_runtime_changed_during_run",
                    "persistent FFmpeg runtime became unavailable during revalidation",
                ) from error
            try:
                _revalidate_parallel_basis(
                    root,
                    prepared,
                    initial_runtime=runtime,
                    current_runtime=current_runtime,
                    sources=source_by_id,
                    mapped_ids=mapped_source_ids,
                )
            except ParallelRenderError as error:
                _cleanup_worker_staging(parallel_store, operation_id)
                code = "parallel_render_runtime_changed_during_run" if error.code == "parallel_render_runtime_changed_during_run" else "parallel_render_basis_changed_during_run"
                active = _write_parallel_failure_record(
                    store,
                    active,
                    _parallel_failure(code),
                )
                return ParallelRenderOutcome(active, None, False)
            active = store.write_locked(_phase_record(active, "parallel_render_publishing"))
            parallel_store.publish(staging, render_id, manifest)
            published = True
            result_ref = ParallelRenderOperationResult(
                parallel_render_id=render_id,
                schema_version=1,
                manifest_content_hash=canonical_sha256_v1(manifest),
                partial_failure_evidence=first_camera_failure,
            )
            active = _terminal_record(active, status="succeeded", result=result_ref)
            succeeded = store.write_locked(active)
            try:
                parallel_store.clear_publish_intent(
                    render_id,
                    operation_id,
                    result_ref.manifest_content_hash,
                )
            except ParallelRenderError:
                # Keep the durable success record; status can retry this exact cleanup.
                pass
            return ParallelRenderOutcome(succeeded, result_ref.to_dict(), False)
        except (KeyboardInterrupt, SystemExit):
            if not published:
                # Hard exit evidence keeps staging for status/readback; the
                # worker cannot claim ordinary cleanup after losing control.
                pass
            interrupted = _terminal_record(
                active,
                status="interrupted",
                failure=MediaOperationFailure(
                    code="parallel_render_interrupted",
                    responsibility="roughcut_core",
                    action="recover_abandoned_media_operation",
                    message_code="parallel_render_interrupted",
                ),
            )
            store.write_locked(interrupted)
            raise
        except ParallelRenderError as error:
            recovery_failed = error.recovery_failed
            if staging is not None and not published:
                try:
                    _cleanup_worker_staging(parallel_store, operation_id)
                except ParallelRenderError:
                    if error.code == "parallel_render_publish_failed":
                        recovery_failed = True
                    elif error.code not in {
                        "parallel_render_all_cameras_failed",
                        "parallel_render_basis_changed_during_run",
                        "parallel_render_runtime_changed_during_run",
                        "parallel_render_final_conflict",
                    }:
                        error = ParallelRenderError("parallel_render_staging_failed", "worker staging cleanup failed")
            failure_code = error.code if error.code in {"parallel_render_disk_budget_exceeded", "parallel_render_all_cameras_failed", "parallel_render_basis_changed_during_run", "parallel_render_runtime_changed_during_run", "parallel_render_staging_failed", "parallel_render_final_conflict", "parallel_render_publish_failed"} else "parallel_render_staging_failed"
            publish_evidence = (
                _parallel_publish_evidence(recovery_failed=recovery_failed)
                if failure_code
                in {"parallel_render_final_conflict", "parallel_render_publish_failed"}
                else None
            )
            failed = _write_parallel_failure_record(
                store,
                active,
                _parallel_failure(failure_code, evidence=publish_evidence),
            )
            return ParallelRenderOutcome(failed, None, False)
        except Exception:  # noqa: BLE001 - start failure must close staging and record failure
            if staging is not None and not published:
                try:
                    _cleanup_worker_staging(parallel_store, operation_id)
                except ParallelRenderError:
                    pass
            failed = _write_parallel_failure_record(
                store,
                active,
                _parallel_failure("parallel_render_staging_failed"),
            )
            return ParallelRenderOutcome(failed, None, False)


def _validate_prepare_request(*, edit_version_id: str, alignment_ref: dict[str, object], auxiliary_camera_ids: list[str], expected_revision: int) -> None:
    if not isinstance(edit_version_id, str) or not edit_version_id or not isinstance(alignment_ref, dict) or not isinstance(auxiliary_camera_ids, list) or not auxiliary_camera_ids or any(not isinstance(item, str) or not item for item in auxiliary_camera_ids) or isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
        raise ParallelRenderError("parallel_render_prepare_revision_conflict", "parallel prepare request is not closed")
    if len(auxiliary_camera_ids) != len(set(auxiliary_camera_ids)):
        raise ParallelRenderError("parallel_render_prepare_revision_conflict", "parallel prepare request has duplicate cameras")


def _read_deliverable_alignment(root: Path, project: Project, alignment_ref: dict[str, object]) -> tuple[MulticamAlignmentArtifact, dict[str, object]]:
    if set(alignment_ref) != {"kind", "alignment_id", "schema_version", "content_hash"} or alignment_ref.get("kind") != "multicam_alignment" or alignment_ref.get("schema_version") != 1:
        raise ParallelRenderError("parallel_render_alignment_not_deliverable", "alignment ref is not the exact closed result ref")
    alignment_id = cast(str, alignment_ref["alignment_id"])
    try:
        artifact = AlignmentStore(root).read(alignment_id)
    except (AlignmentError, ValueError) as error:
        raise ParallelRenderError("parallel_render_alignment_not_deliverable", "alignment artifact identity is invalid") from error
    if artifact is None or artifact.project_id != project.project_id or artifact.content_hash != alignment_ref["content_hash"]:
        raise ParallelRenderError("parallel_render_alignment_not_deliverable", "alignment artifact is missing or has a different identity")
    media_store = MediaOperationStore(root, project.project_id)
    producer_record = media_store.read(artifact.producer_operation_id)
    if producer_record is None or producer_record.schema_version != 2 or producer_record.operation_type != "align_multicam" or producer_record.status != "succeeded" or producer_record.error is not None or not isinstance(producer_record.result_ref, AlignmentOperationResult) or producer_record.result_ref.to_dict() != alignment_ref or artifact.producer_operation_id != producer_record.operation_id:
        raise ParallelRenderError("parallel_render_alignment_not_deliverable", "alignment producer record is not an exact succeeded delivery")
    producer = {
        "operation_id": producer_record.operation_id,
        "operation_type": "align_multicam",
        "result_ref": dict(alignment_ref),
    }
    return artifact, cast(dict[str, object], producer)


def _read_adopted_decision(root: Path, project: Project, edit_version_id: str) -> tuple[EditDecision | MultiSourceEditDecision, dict[str, object], dict[str, object]]:
    if project.active_edit_version_id != edit_version_id:
        raise ParallelRenderError("parallel_render_decision_not_adopted", "requested Decision is not the active Decision")
    workflow_root = root / "workflow"
    runs_root = workflow_root / "runs"
    receipts_root = workflow_root / "receipts"
    approvals_root = workflow_root / "approvals"
    transactions_root = workflow_root / "transactions"
    if not _safe_evidence_directory(runs_root) or not _safe_evidence_directory(receipts_root) or not _safe_evidence_directory(approvals_root):
        raise ParallelRenderError("parallel_render_decision_not_adopted", "Decision adoption evidence is missing")
    if os.path.lexists(transactions_root) and not _safe_evidence_directory(transactions_root):
        raise ParallelRenderError("parallel_render_decision_not_adopted", "Decision adoption evidence is unsafe")
    candidates: list[tuple[WorkflowRun, ActionReceipt, ApprovalRecord, ArtifactRef, EditDecision | MultiSourceEditDecision]] = []
    try:
        run_paths = sorted(path for path in runs_root.iterdir() if path.name.endswith(".json"))
    except OSError as error:
        raise ParallelRenderError("parallel_render_decision_not_adopted", "Decision adoption evidence is unreadable") from error
    for path in run_paths:
        run_payload = _read_evidence_json(path)
        def parse_run(run_payload: dict[str, object] = run_payload) -> WorkflowRun:
            return WorkflowRun.from_dict(run_payload)

        run, run_error = _capture_exception(parse_run)
        if run_error is not None or run is None:
            continue
        if path.name != f"{run.run_id}.json":
            raise ParallelRenderError(
                "parallel_render_decision_not_adopted",
                "WorkflowRun path does not match its object identity",
            )
        if (
            run.project_id != project.project_id
            or (run.lifecycle, run.stage)
            not in {("active", "export_review"), ("completed", "exporting")}
            or run.artifact_refs["decision"] is None
        ):
            continue
        decision_ref = run.artifact_refs["decision"]
        assert decision_ref is not None
        if decision_ref.artifact_id != edit_version_id:
            continue
        def read_decision(
            decision_ref: ArtifactRef = decision_ref,
        ) -> EditDecision | MultiSourceEditDecision:
            return _read_decision_evidence(root, decision_ref)

        decision, decision_error = _capture_exception(read_decision)
        if decision_error is not None or decision is None:
            continue
        roughcut_ref = run.approval_refs.get("roughcut")
        proposal_ref = run.artifact_refs.get("proposal")
        if roughcut_ref is None or proposal_ref is None:
            continue
        approval_path = approvals_root / f"{roughcut_ref.approval_id}.json"
        approval_payload = _read_evidence_json(approval_path)
        def parse_approval(
            approval_payload: dict[str, object] = approval_payload,
        ) -> ApprovalRecord:
            return ApprovalRecord.from_dict(approval_payload)

        approval, approval_error = _capture_exception(parse_approval)
        if approval_error is not None or approval is None:
            continue
        if approval_path.name != f"{approval.approval_id}.json":
            raise ParallelRenderError(
                "parallel_render_decision_not_adopted",
                "Approval path does not match its object identity",
            )
        if (
            approval.project_id != project.project_id
            or approval.run_id != run.run_id
            or approval.gate != "roughcut"
            or approval.issued_project_revision >= project.revision
            or canonical_sha256_v1(approval.to_dict()) != roughcut_ref.record_hash
        ):
            continue
        receipt_path = receipts_root / f"{approval.issued_by_action_id}.json"
        receipt_payload = _read_evidence_json(receipt_path)
        def parse_receipt(
            receipt_payload: dict[str, object] = receipt_payload,
        ) -> ActionReceipt:
            return ActionReceipt.from_dict(receipt_payload)

        receipt, receipt_error = _capture_exception(parse_receipt)
        if receipt_error is not None or receipt is None:
            continue
        if receipt_path.name != f"{receipt.action_id}.json":
            raise ParallelRenderError(
                "parallel_render_decision_not_adopted",
                "receipt path does not match its object identity",
            )
        if _pending_transaction_marker(transactions_root, approval.issued_by_action_id):
            raise ParallelRenderError("parallel_render_decision_not_adopted", "Decision adoption has a pending transaction marker")
        if not _exact_adoption_receipt(receipt, approval, run, decision_ref, project):
            continue
        def validate_proposal(
            run=run, proposal_ref=proposal_ref, approval=approval
        ) -> bool:
            _read_proposal_evidence(root, proposal_ref)
            expected_subject = SubjectRef(
                "proposal",
                proposal_ref.artifact_id,
                proposal_ref.schema_version,
                proposal_ref.content_hash,
            )
            return (
                approval.subject == expected_subject
                and approval.dependency_hash == _roughcut_dependency(run, proposal_ref)
            )
        proposal_is_valid, proposal_error = _capture_exception(validate_proposal)
        if proposal_error is not None or not proposal_is_valid:
            continue
        candidates.append((run, receipt, approval, decision_ref, decision))
    if len(candidates) != 1:
        raise ParallelRenderError("parallel_render_decision_not_adopted", "there is not one exact adopted Decision chain")
    run, receipt, approval, decision_ref, decision = candidates[0]
    return decision, {
        "kind": "decision",
        "edit_version_id": decision_ref.artifact_id,
        "schema_version": decision_ref.schema_version,
        "content_hash": decision_ref.content_hash,
    }, {
        "run_id": run.run_id,
        "receipt_ref": {"action_id": receipt.action_id, "receipt_schema_version": 1, "receipt_hash": canonical_sha256_v1(receipt.to_dict())},
        "approval_ref": {"approval_id": approval.approval_id, "record_schema_version": 1, "record_hash": canonical_sha256_v1(approval.to_dict())},
    }


def _safe_evidence_directory(path: Path) -> bool:
    try:
        details = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(details.st_mode) and not stat.S_ISLNK(details.st_mode)


def _require_safe_evidence_file(path: Path) -> None:
    try:
        details = os.lstat(path)
    except OSError as error:
        raise ParallelRenderError("parallel_render_decision_not_adopted", "Decision adoption evidence is unreadable") from error
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
        raise ParallelRenderError("parallel_render_decision_not_adopted", "Decision adoption evidence is unsafe")


def _read_evidence_json(path: Path) -> dict[str, Any]:
    descriptor = -1
    try:
        path_details = os.lstat(path)
        if (
            stat.S_ISLNK(path_details.st_mode)
            or not stat.S_ISREG(path_details.st_mode)
            or path_details.st_nlink != 1
        ):
            raise ParallelRenderError(
                "parallel_render_decision_not_adopted",
                "Decision adoption evidence is unsafe",
            )
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(path, flags)
        descriptor_details = os.fstat(descriptor)
        current_details = os.lstat(path)
        identities = {
            (path_details.st_dev, path_details.st_ino),
            (descriptor_details.st_dev, descriptor_details.st_ino),
            (current_details.st_dev, current_details.st_ino),
        }
        if (
            len(identities) != 1
            or stat.S_ISLNK(current_details.st_mode)
            or not stat.S_ISREG(descriptor_details.st_mode)
            or not stat.S_ISREG(current_details.st_mode)
            or descriptor_details.st_nlink != 1
            or current_details.st_nlink != 1
        ):
            raise ParallelRenderError(
                "parallel_render_decision_not_adopted",
                "Decision adoption evidence changed identity while being read",
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        final_descriptor_details = os.fstat(descriptor)
        final_path_details = os.lstat(path)
        if (
            (final_descriptor_details.st_dev, final_descriptor_details.st_ino)
            not in identities
            or (final_path_details.st_dev, final_path_details.st_ino)
            not in identities
            or not stat.S_ISREG(final_descriptor_details.st_mode)
            or not stat.S_ISREG(final_path_details.st_mode)
            or final_descriptor_details.st_nlink != 1
            or final_path_details.st_nlink != 1
        ):
            raise ParallelRenderError(
                "parallel_render_decision_not_adopted",
                "Decision adoption evidence changed identity while being read",
            )
        return load_closed_json(b"".join(chunks))
    except ParallelRenderError:
        raise
    except Exception as error:
        raise ParallelRenderError("parallel_render_decision_not_adopted", "Decision adoption evidence is invalid") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_decision_evidence(root: Path, ref: ArtifactRef) -> EditDecision | MultiSourceEditDecision:
    payload = _read_evidence_json(root / "edits" / f"{ref.artifact_id}.json")
    if payload.get("schema_version") == 1:
        decision: EditDecision | MultiSourceEditDecision = EditDecision.from_dict(payload)
    elif payload.get("schema_version") == 2:
        decision = MultiSourceEditDecision.from_dict(payload)
    else:
        raise ParallelRenderError("parallel_render_decision_not_adopted", "Decision schema is invalid")
    canonical = dict(decision.to_dict())
    canonical.pop("created_at", None)
    if decision.edit_version_id != ref.artifact_id or decision.schema_version != ref.schema_version or subject_content_hash("decision", decision.schema_version, canonical) != ref.content_hash:
        raise ParallelRenderError("parallel_render_decision_not_adopted", "Decision ref does not match its immutable evidence")
    return decision


def _read_proposal_evidence(root: Path, ref: ArtifactRef) -> EditProposal | MultiSourceEditProposal:
    payload = _read_evidence_json(root / "proposals" / f"{ref.artifact_id}.json")
    if payload.get("schema_version") == 1:
        proposal: EditProposal | MultiSourceEditProposal = EditProposal.from_dict(payload)
    elif payload.get("schema_version") == 2:
        proposal = MultiSourceEditProposal.from_dict(payload)
    else:
        raise ParallelRenderError("parallel_render_decision_not_adopted", "Proposal schema is invalid")
    canonical = dict(proposal.to_dict())
    canonical.pop("created_at", None)
    if proposal.proposal_id != ref.artifact_id or proposal.schema_version != ref.schema_version or subject_content_hash("proposal", proposal.schema_version, canonical) != ref.content_hash:
        raise ParallelRenderError("parallel_render_decision_not_adopted", "Proposal ref does not match its immutable evidence")
    return proposal


def _pending_transaction_marker(transactions_root: Path, action_id: str) -> bool:
    if not os.path.lexists(transactions_root):
        return False
    marker = transactions_root / f"{action_id}.json"
    if not os.path.lexists(marker):
        return False
    _require_safe_evidence_file(marker)
    return True


def _exact_adoption_receipt(
    receipt: ActionReceipt,
    approval: ApprovalRecord,
    run: WorkflowRun,
    decision_ref: ArtifactRef,
    project: Project,
) -> bool:
    mutation = receipt.mutation
    return not (
        receipt.action_id != approval.issued_by_action_id
        or receipt.project_id != project.project_id
        or receipt.run_id != run.run_id
        or receipt.action != "adopt_roughcut"
        or receipt.approval_ids != (approval.approval_id,)
        or mutation is None
        or mutation.kind != "decision"
        or mutation.artifact_id != decision_ref.artifact_id
        or mutation.schema_version != decision_ref.schema_version
        or mutation.content_hash != decision_ref.content_hash
        or not mutation.changed
        or receipt.before.stage != "roughcut_review"
        or receipt.before.lifecycle != "active"
        or receipt.after.stage != "export_review"
        or receipt.after.lifecycle != "active"
        or receipt.after.project_revision != receipt.before.project_revision + 1
        or approval.issued_project_revision != receipt.before.project_revision
        or (
            run.lifecycle == "active"
            and (
                run.stage != "export_review"
                or project.revision != receipt.after.project_revision
            )
        )
        or (
            run.lifecycle == "completed"
            and (
                run.stage != "exporting"
                or project.revision < receipt.after.project_revision
            )
        )
        or run.lifecycle not in {"active", "completed"}
    )


def _camera(artifact: MulticamAlignmentArtifact, camera_id: str) -> AlignmentCamera:
    for camera in artifact.auxiliary_cameras:
        if camera.camera_id == camera_id:
            return camera
    raise ParallelRenderError("parallel_render_camera_not_deliverable", "camera is not part of the alignment artifact")


def _project_camera_slots(
    camera: AlignmentCamera,
    all_intervals: tuple[AlignmentInterval, ...],
    clips: tuple[EditClip, ...],
    settings: dict[str, object],
) -> list[dict[str, object]]:
    frame_rate = cast(dict[str, object], settings["frame_rate"])
    frame_rate_numerator = cast(int, frame_rate["numerator"])
    frame_rate_denominator = cast(int, frame_rate["denominator"])
    sample_rate = cast(int, settings["audio_sample_rate"])
    output_cursor = 0
    slots: list[dict[str, object]] = []
    for ordinal, clip in enumerate(clips):
        try:
            projected = project_alignment_intervals(
                camera_id=camera.camera_id,
                intervals=all_intervals,
                main_source_id=clip.source_id,
                main_start_ticks=clip.source_in_ticks,
                main_end_ticks=clip.source_out_ticks,
                timeline_start_ticks=output_cursor,
            )
        except AlignmentError as error:
            raise ParallelRenderError(
                "parallel_render_camera_not_deliverable",
                "alignment partition does not cover a Decision clip",
            ) from error
        for segment in projected:
            auxiliary = None
            if segment.classification == "mapped":
                assert segment.auxiliary_source_id is not None
                assert segment.auxiliary_start_ticks is not None
                assert segment.auxiliary_end_ticks is not None
                auxiliary = {
                    "source_id": segment.auxiliary_source_id,
                    "source_start_ticks": segment.auxiliary_start_ticks,
                    "source_end_ticks": segment.auxiliary_end_ticks,
                }
            frame_start = frame_boundary(
                segment.timeline_start_ticks,
                frame_rate_numerator,
                frame_rate_denominator,
            )
            frame_end = frame_boundary(
                segment.timeline_end_ticks,
                frame_rate_numerator,
                frame_rate_denominator,
            )
            sample_start = sample_boundary(segment.timeline_start_ticks, sample_rate)
            sample_end = sample_boundary(segment.timeline_end_ticks, sample_rate)
            slots.append(
                {
                    "slot_id": f"slot_{len(slots):06d}",
                    "decision_clip_ref": {
                        "clip_id": clip.clip_id,
                        "clip_ordinal": ordinal,
                        "source_id": clip.source_id,
                        "source_start_ticks": segment.main_start_ticks,
                        "source_end_ticks": segment.main_end_ticks,
                    },
                    "classification": segment.classification,
                    "auxiliary_ref": auxiliary,
                    "output_start_ticks": segment.timeline_start_ticks,
                    "output_end_ticks": segment.timeline_end_ticks,
                    "video_frame_start": frame_start,
                    "video_frame_end": frame_end,
                    "video_frame_quota": frame_end - frame_start,
                    "audio_sample_start": sample_start,
                    "audio_sample_end": sample_end,
                    "audio_sample_quota": sample_end - sample_start,
                }
            )
        output_cursor += clip.duration_ticks
    if not slots or cast(int, slots[-1]["output_end_ticks"]) != output_cursor:
        raise ParallelRenderError(
            "parallel_render_camera_not_deliverable",
            "camera slot plan does not cover the Decision output",
        )
    return slots


def _assert_selected_sources_current(root: Path, sources: dict[str, SourceAsset], camera: AlignmentCamera, slots: list[dict[str, object]]) -> None:
    for source_id in _mapped_source_ids([{"slots": slots}]):
        source = sources.get(source_id)
        if source is None:
            raise ParallelRenderError("parallel_render_source_stale", "mapped Source is not current")
        try:
            if fingerprint_file(_resolve_source_path(root, source)) != source.fingerprint:
                raise ParallelRenderError("parallel_render_source_stale", "mapped Source fingerprint changed")
        except (OSError, ProjectError):
            raise ParallelRenderError("parallel_render_source_stale", "mapped Source is unreadable") from None
    del camera


def _mapped_source_ids(cameras: list[dict[str, object]]) -> tuple[str, ...]:
    result: list[str] = []
    for camera in cameras:
        for slot in cast(list[dict[str, object]], camera["slots"]):
            if slot["classification"] == "mapped":
                aux = slot["auxiliary_ref"]
                assert isinstance(aux, dict)
                source_id = cast(str, aux["source_id"])
                if source_id not in result:
                    result.append(source_id)
    return tuple(result)


def _source_paths(root: Path, sources: dict[str, SourceAsset], ids: tuple[str, ...]) -> dict[str, Path]:
    try:
        return {source_id: _resolve_source_path(root, sources[source_id]) for source_id in ids}
    except (KeyError, OSError, ProjectError) as error:
        raise ParallelRenderError("parallel_render_source_stale", "mapped Source path is unavailable") from error


def _temporary_disk_estimate(*, total_ticks: int, frame_quota: int, sample_quota: int, camera_count: int, settings: dict[str, object]) -> int:
    pixels = cast(int, settings["width"]) * cast(int, settings["height"])
    per_camera = max(1, frame_quota * pixels // 8 + sample_quota * 2 * 2 + 4 * 1024 * 1024)
    return max(1, per_camera * camera_count + total_ticks // TICKS_PER_SECOND * 1024)


def _runtime_identity(runtime: Any) -> dict[str, object]:
    return {
        "runtime_binding_sha256": runtime.runtime_binding_sha256,
        "python_receipt_hash": runtime.python_receipt_hash,
        "ffmpeg_tool_selection_hash": runtime.ffmpeg_tool_selection_hash,
        "ffprobe_tool_selection_hash": runtime.ffprobe_tool_selection_hash,
        "ffmpeg_version": runtime.ffmpeg.version,
        "ffprobe_version": runtime.ffprobe.version,
    }


def _load_runtime_for_parallel() -> Any:
    try:
        return _load_persistent_runtime()
    except Exception as error:
        raise ParallelRenderError("parallel_render_runtime_unavailable", "persistent FFmpeg runtime is unavailable") from error


def _parallel_input_hash(basis: dict[str, object], project: Project, sources: dict[str, SourceAsset], mapped_ids: tuple[str, ...], runtime: Any) -> str:
    return canonical_sha256_v1(
        {
            "input_schema_version": 1,
            "operation_type": "render_multicam_parallel",
            "basis": basis,
            "mapped_sources": [
                {"source_id": source_id, "source": sources[source_id].to_dict()}
                for source_id in mapped_ids
            ],
            "project_id": project.project_id,
            "runtime": _runtime_identity(runtime),
        }
    )


def _revalidate_parallel_basis(
    root: Path,
    prepared: ParallelPrepareOutcome,
    *,
    initial_runtime: Any,
    current_runtime: Any,
    sources: dict[str, SourceAsset],
    mapped_ids: tuple[str, ...],
) -> None:
    if _runtime_identity(initial_runtime) != _runtime_identity(current_runtime):
        raise ParallelRenderError(
            "parallel_render_runtime_changed_during_run",
            "persistent FFmpeg runtime identity changed",
        )
    current_project = ProjectStore(root).load()
    basis_project = cast(dict[str, object], prepared.basis["project"])
    if current_project.revision != cast(int, basis_project["revision"]):
        raise ParallelRenderError("parallel_render_basis_changed_during_run", "Project revision changed")
    try:
        for source_id in mapped_ids:
            if fingerprint_file(_resolve_source_path(root, sources[source_id])) != sources[source_id].fingerprint:
                raise ParallelRenderError("parallel_render_basis_changed_during_run", "mapped Source changed")
    except (OSError, ProjectError, KeyError) as error:
        raise ParallelRenderError("parallel_render_basis_changed_during_run", "mapped Source became stale") from error
    basis_decision = cast(dict[str, object], prepared.basis["decision"])
    basis_alignment = cast(dict[str, object], prepared.basis["alignment"])
    basis_project = cast(dict[str, object], prepared.basis["project"])
    current = prepare_multicam_parallel_render(
        root,
        edit_version_id=cast(str, cast(dict[str, object], basis_decision["ref"])["edit_version_id"]),
        alignment_ref=cast(dict[str, object], basis_alignment["ref"]),
        auxiliary_camera_ids=cast(list[str], prepared.basis["auxiliary_camera_ids"]),
        expected_revision=cast(int, basis_project["revision"]),
    )
    if current.basis != prepared.basis:
        raise ParallelRenderError("parallel_render_basis_changed_during_run", "Decision/alignment/output basis changed")


def _manifest(summary: dict[str, object], prepare_ref: dict[str, object], *, operation_id: str, parallel_id: str, cameras: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "parallel_render_id": parallel_id,
        "project_id": summary["project_id"],
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "producer": {"operation_id": operation_id, "operation_type": "render_multicam_parallel"},
        "prepare_ref": prepare_ref,
        "decision": summary["decision"],
        "alignment": summary["alignment"],
        "delivery_status": "complete" if all(camera["render_status"] == "succeeded" for camera in cameras) else "partial",
        "output_profile": summary["output_profile"],
        "output_settings": summary["output_settings"],
        "output_settings_hash": summary["output_settings_hash"],
        "total_ticks": summary["total_ticks"],
        "video_frame_quota": summary["video_frame_quota"],
        "audio_sample_quota": summary["audio_sample_quota"],
        "manifest_file": {"filename": "manifest.json", "relative_path": "manifest.json", "project_relative_path": f"renders/multicam/{parallel_id}/manifest.json"},
        "cameras": cameras,
    }


def _parallel_failure(
    code: str,
    *,
    evidence: ParallelFailureEvidence | None = None,
) -> MediaOperationFailure:
    action = {
        "parallel_render_disk_budget_exceeded": "validate_parallel_render_basis",
        "parallel_render_all_cameras_failed": "render_parallel_cameras",
        "parallel_render_basis_changed_during_run": "revalidate_parallel_render_basis",
        "parallel_render_runtime_changed_during_run": "revalidate_parallel_render_basis",
        "parallel_render_staging_failed": "manage_parallel_render_staging",
        "parallel_render_final_conflict": "publish_parallel_render",
        "parallel_render_publish_failed": "publish_parallel_render",
    }[code]
    return MediaOperationFailure(
        code,
        "roughcut_core",
        action,
        "parallel_render_failed",
        evidence,
    )


def _parallel_encode_evidence(
    camera_id: str, error: ParallelCameraEncodeError
) -> ParallelFailureEvidence:
    return ParallelFailureEvidence(
        code="parallel_camera_encode_failed",
        camera_id=camera_id,
        check="ffmpeg_encode",
        expected=None,
        actual=None,
        delta=None,
        tolerance=None,
        return_code=(
            error.return_code
            if isinstance(error.return_code, int) and not isinstance(error.return_code, bool)
            else -1
        ),
        stderr_tail=bounded_stderr_tail(
            error.stderr if error.stderr is not None else str(error)
        ),
    )


def _parallel_verify_evidence(
    camera_id: str, error: ParallelCameraVerifyError
) -> ParallelFailureEvidence:
    expected = error.expected
    actual = error.actual
    delta = (
        actual - expected
        if isinstance(expected, int)
        and not isinstance(expected, bool)
        and isinstance(actual, int)
        and not isinstance(actual, bool)
        else None
    )
    return ParallelFailureEvidence(
        code="parallel_camera_verify_failed",
        camera_id=camera_id,
        check=error.check,
        expected=expected,
        actual=actual,
        delta=delta,
        tolerance=error.tolerance,
        return_code=None,
        stderr_tail=None,
    )


def _parallel_publish_evidence(*, recovery_failed: bool = False) -> ParallelFailureEvidence:
    return ParallelFailureEvidence(
        code="parallel_manifest_publish_failed",
        camera_id=None,
        check="manifest_publish_recovery" if recovery_failed else "manifest_publish",
        expected="ambiguous_final_removed" if recovery_failed else "published_verified_manifest",
        actual="recovery_failed" if recovery_failed else "publish_failed",
        delta=None,
        tolerance=None,
        return_code=None,
        stderr_tail=None,
    )


def _parallel_evidence_fallback() -> ParallelFailureEvidence:
    return ParallelFailureEvidence(
        code="parallel_failure_evidence_unavailable",
        camera_id=None,
        check="failure_evidence_persistence",
        expected="primary_failure_preserved",
        actual="evidence_unavailable",
        delta=None,
        tolerance=None,
        return_code=None,
        stderr_tail=None,
    )


def _write_parallel_failure_record(
    store: MediaOperationStore,
    active: MediaOperationRecord,
    failure: MediaOperationFailure,
) -> MediaOperationRecord:
    terminal = _terminal_record(active, status="failed", failure=failure)
    try:
        return store.write_locked(terminal)
    except (MediaOperationError, OSError, TypeError, ValueError) as primary_error:
        fallback = _terminal_record(
            active,
            status="failed",
            failure=replace(failure, evidence=_parallel_evidence_fallback()),
        )
        try:
            return store.write_locked(fallback)
        except (MediaOperationError, OSError, TypeError, ValueError):
            raise primary_error


def _parallel_result(record: MediaOperationRecord) -> dict[str, object] | None:
    if not isinstance(record.result_ref, ParallelRenderOperationResult):
        return None
    return record.result_ref.to_dict()


def _cleanup_worker_staging(store: MulticamParallelStore, operation_id: str) -> None:
    store.remove_staging(operation_id)


def _remove_camera_temps(temp_output: Path, output_path: Path) -> None:
    try:
        temp_output.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)
    except OSError as error:
        raise ParallelCameraStagingError("could not clean up a camera staging output") from error
