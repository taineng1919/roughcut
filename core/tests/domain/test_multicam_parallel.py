from __future__ import annotations

import pytest

from roughcut.domain.media_operation import (
    MediaOperationFailure,
    MediaOperationRecord,
    ParallelRenderOperationResult,
    ProjectOperationScope,
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
    validate_manifest,
    validate_parallel_prepare_pair,
)


def _summary() -> dict[str, object]:
    h = "a" * 64
    alignment = {
        "kind": "multicam_alignment",
        "alignment_id": "aln_a",
        "schema_version": 1,
        "content_hash": h,
    }
    slot = {
        "slot_id": "slot_000000",
        "decision_clip_ref": {
            "clip_id": "clip_a",
            "clip_ordinal": 0,
            "source_id": "src_main",
            "source_start_ticks": 0,
            "source_end_ticks": 120_000,
        },
        "classification": "mapped",
        "auxiliary_ref": {
            "source_id": "src_aux",
            "source_start_ticks": 0,
            "source_end_ticks": 120_000,
        },
        "output_start_ticks": 0,
        "output_end_ticks": 120_000,
        "video_frame_start": 0,
        "video_frame_end": 25,
        "video_frame_quota": 25,
        "audio_sample_start": 0,
        "audio_sample_end": 48_000,
        "audio_sample_quota": 48_000,
    }
    camera = {
        "camera_id": "aux_1",
        "alignment_status": "complete",
        "coverage_status": "complete",
        "render_status": "planned",
        "mapped_ticks": 120_000,
        "missing_ticks": 0,
        "uncertain_ticks": 0,
        "conflict_ticks": 0,
        "black_silence_ticks": 0,
        "planned_output": {"filename": "camera_aux_1.mp4", "relative_path": "camera_aux_1.mp4"},
        "slots": [slot],
    }
    return {
        "schema_version": 1,
        "prepare_id": "mpr_" + "b" * 32,
        "plan_basis_hash": h,
        "project_id": "project_a",
        "project_revision": 1,
        "decision": {
            "ref": {"kind": "decision", "edit_version_id": "edit_a", "schema_version": 1, "content_hash": h},
            "adoption": {
                "run_id": "run_a",
                "receipt_ref": {"action_id": "act_a", "receipt_schema_version": 1, "receipt_hash": h},
                "approval_ref": {"approval_id": "approval_a", "record_schema_version": 1, "record_hash": h},
            },
        },
        "alignment": {
            "ref": alignment,
            "producer": {"operation_id": "op_00000000000040008000000000000001", "operation_type": "align_multicam", "result_ref": alignment},
        },
        "output_profile": PARALLEL_OUTPUT_PROFILE,
        "output_settings": {"width": 1920, "height": 1080, "frame_rate": {"numerator": 25, "denominator": 1}, "audio_sample_rate": 48_000},
        "output_settings_hash": output_settings_hash({"width": 1920, "height": 1080, "frame_rate": {"numerator": 25, "denominator": 1}, "audio_sample_rate": 48_000}),
        "total_ticks": 120_000,
        "video_frame_quota": 25,
        "audio_sample_quota": 48_000,
        "estimated_temporary_disk_bytes": 1,
        "manifest": {"filename": "manifest.json", "relative_path": "manifest.json"},
        "cameras": [camera],
    }


def test_global_boundaries_and_output_settings_hash_are_frozen() -> None:
    assert frame_boundary(18_006, 25, 1) == 4
    assert sample_boundary(18_006, 48_000) == 7_202
    assert output_settings_hash(
        {"width": 1920, "height": 1080, "frame_rate": {"numerator": 25, "denominator": 1}, "audio_sample_rate": 48_000}
    ) == "b11ff679ae9b32ad4b5168003c50eda7a2f2550f0e0866223a6853241312116b"


def test_prepare_summary_is_recursive_closed_and_uses_global_quotas() -> None:
    summary = _summary()
    ParallelPrepareSummary(summary)
    mutated = dict(summary)
    mutated["unknown"] = True
    with pytest.raises(ParallelRenderError):
        ParallelPrepareSummary(mutated)
    camera = dict(summary["cameras"][0])
    slots = [dict(camera["slots"][0])]
    slots[0]["video_frame_end"] = 26
    camera["slots"] = slots
    mutated = dict(summary)
    mutated["cameras"] = [camera]
    with pytest.raises(ParallelRenderError):
        ParallelPrepareSummary(mutated)


def test_schema_two_parallel_operation_result_and_failure_mapping_are_closed() -> None:
    result = ParallelRenderOperationResult("mpr_" + "c" * 32, 1, "d" * 64)
    scope = ProjectOperationScope("project_a", "e" * 64)
    record = MediaOperationRecord(
        operation_id="op_00000000000040008000000000000002",
        scope=scope,
        operation_type="render_multicam_parallel",
        request_hash="f" * 64,
        input_hash="a" * 64,
        status="succeeded",
        phase_message_code="parallel_render_succeeded",
        created_at="2026-08-04T00:00:00.000000Z",
        started_at="2026-08-04T00:00:00.000000Z",
        updated_at="2026-08-04T00:00:00.000000Z",
        finished_at="2026-08-04T00:00:00.000000Z",
        result_ref=result,
        error=None,
        schema_version=2,
    )
    assert MediaOperationRecord.from_dict(record.to_dict()) == record
    failure = MediaOperationFailure(
        "parallel_render_all_cameras_failed",
        "roughcut_core",
        "render_parallel_cameras",
        "parallel_render_failed",
    )
    failure.validate_for("render_multicam_parallel", "failed")


def _prepare_basis() -> dict[str, object]:
    summary = _summary()
    return {
        "kind": "multicam_parallel_prepare_basis",
        "schema_version": 1,
        "project": {
            "project_id": summary["project_id"],
            "revision": summary["project_revision"],
        },
        "decision": summary["decision"],
        "alignment": summary["alignment"],
        "auxiliary_camera_ids": ["aux_1"],
        "output_profile": summary["output_profile"],
        "output_settings": summary["output_settings"],
        "output_settings_hash": summary["output_settings_hash"],
        "total_ticks": summary["total_ticks"],
        "video_frame_quota": summary["video_frame_quota"],
        "audio_sample_quota": summary["audio_sample_quota"],
        "estimated_temporary_disk_bytes": summary["estimated_temporary_disk_bytes"],
        "manifest": summary["manifest"],
        "cameras": summary["cameras"],
    }


def test_pre_feature_prepare_basis_keeps_its_exact_plan_basis_and_prepare_id() -> None:
    """Golden values captured from the pre-placeholder revision of this module.

    The old basis never carried ``placeholder_ref``/``placeholder_ticks``; the
    new code must still accept it and derive byte-identical identities.
    """

    basis = _prepare_basis()
    ref = build_prepare_ref(basis)
    assert ref["plan_basis_hash"] == (
        "fd3d93e2bbe0bd95e898f9dc7135f36d05235d4965de790c95ec0eb7d4abd5d8"
    )
    assert ref["prepare_id"] == "mpr_bcc59e2cdfe9125802085041a433deec"
    validate_parallel_prepare_pair(ref, basis)
    ParallelPrepareRef(ref)


def test_camera_and_slot_validation_accepts_both_pre_feature_and_placeholder_shapes() -> None:
    summary = _summary()
    ParallelPrepareSummary(summary)
    camera = dict(summary["cameras"][0])
    slot = dict(camera["slots"][0])
    slot["placeholder_ref"] = None
    camera["slots"] = [slot]
    camera["placeholder_ticks"] = 0
    expanded = dict(summary)
    expanded["cameras"] = [camera]
    ParallelPrepareSummary(expanded)
    with pytest.raises(ParallelRenderError):
        # A narration placeholder slot still requires its exact media ref.
        placeholder_slot = {
            **slot,
            "classification": "narration_placeholder",
            "auxiliary_ref": None,
            "decision_clip_ref": {
                **slot["decision_clip_ref"],
                "source_id": "phm_aaa",
            },
        }
        broken = dict(summary)
        broken["cameras"] = [{**camera, "slots": [placeholder_slot]}]
        ParallelPrepareSummary(broken)


def test_manifest_validation_accepts_pre_feature_camera_plans() -> None:
    summary = _summary()
    ref = build_prepare_ref(_prepare_basis())
    operation_id = "op_" + "a" * 32
    render_id = parallel_render_id(ref, operation_id)
    manifest = {
        "schema_version": 1,
        "parallel_render_id": render_id,
        "project_id": summary["project_id"],
        "created_at": "2026-10-16T00:00:00.000000Z",
        "producer": {
            "operation_id": operation_id,
            "operation_type": "render_multicam_parallel",
        },
        "prepare_ref": ref,
        "decision": summary["decision"],
        "alignment": summary["alignment"],
        "delivery_status": "complete",
        "output_profile": summary["output_profile"],
        "output_settings": summary["output_settings"],
        "output_settings_hash": summary["output_settings_hash"],
        "total_ticks": summary["total_ticks"],
        "video_frame_quota": summary["video_frame_quota"],
        "audio_sample_quota": summary["audio_sample_quota"],
        "manifest_file": {
            "filename": "manifest.json",
            "relative_path": "manifest.json",
            "project_relative_path": f"renders/multicam/{render_id}/manifest.json",
        },
        "cameras": [
            {
                "camera_id": "aux_1",
                "alignment_status": "complete",
                "coverage_status": "complete",
                "render_status": "succeeded",
                "mapped_ticks": summary["cameras"][0]["mapped_ticks"],
                "missing_ticks": 0,
                "uncertain_ticks": 0,
                "conflict_ticks": 0,
                "black_silence_ticks": 0,
                "output": {
                    "filename": "camera_aux_1.mp4",
                    "relative_path": "camera_aux_1.mp4",
                    "project_relative_path": f"renders/multicam/{render_id}/camera_aux_1.mp4",
                    "bytes": 1,
                    "content_hash": "d" * 64,
                },
                "error": None,
                "slots": summary["cameras"][0]["slots"],
            }
        ],
    }
    validate_manifest(manifest)
