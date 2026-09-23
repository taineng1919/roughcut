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
    ParallelPrepareSummary,
    ParallelRenderError,
    frame_boundary,
    output_settings_hash,
    sample_boundary,
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
