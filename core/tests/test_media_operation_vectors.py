from __future__ import annotations

import json
from pathlib import Path

from roughcut.domain.media_operation import (
    hash_approve_export_input,
    hash_approve_export_request,
    hash_proxy_input,
    hash_proxy_request,
    hash_transcription_input,
    hash_transcription_request,
)

ROOT = Path(__file__).resolve().parents[2]
VECTORS = (
    ROOT / "core" / "tests" / "fixtures" / "media-operation-record-vectors.json"
)

_OWNERS = {
    "MOR-001": (
        "test_transcription_operation_binds_exact_result_and_readback_skips_preflight",
    ),
    "MOR-002": (
        "test_proxy_operation_pair_publish_reuse_and_project_invariants",
    ),
    "MOR-003": (
        "test_media_operation_multisource_export_preserves_schema_two_three_pair",
    ),
    "MOR-004": (
        "test_live_writer_second_call_returns_running_without_starting_worker",
    ),
    "MOR-005": (
        "test_hard_exit_at_nonterminal_publication_converges_interrupted",
    ),
    "MOR-006": (
        "test_transcription_operation_binds_exact_result_and_readback_skips_preflight",
    ),
    "MOR-007": (
        "test_transcription_stale_revision_and_changed_request_fail_without_worker",
        "test_proxy_same_id_conflict_and_terminal_readback_ignore_current_basis",
    ),
    "MOR-008": (
        "test_transcription_stale_revision_and_changed_request_fail_without_worker",
    ),
    "MOR-009": (
        "test_media_operation_approve_export_stale_ref_fails_before_record",
    ),
    "MOR-010": (
        "test_proxy_project_change_after_encode_fails_before_pair_publish",
    ),
    "MOR-011": (
        "test_hard_exit_after_terminal_publish_reads_back_succeeded",
    ),
    "MOR-012": (
        "test_transcription_hard_exit_before_publish_is_not_adopted",
    ),
    "MOR-013": (
        "test_transcription_hard_exit_after_project_commit_keeps_binding_repairable",
    ),
    "MOR-014": (
        "test_manifest_publish_failure_removes_just_published_output",
        "test_hard_exit_at_nonterminal_publication_converges_interrupted",
    ),
    "MOR-015": (
        "test_media_operation_approve_export_claim_and_interrupt_preserve_authority",
        "test_failed_render_explicit_new_id_cleans_only_exact_tracked_orphan",
        "test_hard_exit_render_orphan_converges_then_explicit_new_id_reruns",
        "test_media_operation_multisource_run_published_failure_recovers_exact_before",
        "test_fwv_030_046_mp4_write_hard_exit_leaves_only_owned_staging",
    ),
    "MOR-016": (
        "test_media_store_rejects_symlink_record_directory_and_project_root",
        "test_media_store_rejects_hardlinked_record_and_writer_lock",
        "test_media_store_rejects_path_escape_and_static_windows_branch",
    ),
    "MOR-017": (
        "test_proxy_failure_record_is_closed_and_candidate_cleanup_is_preserved",
        "test_mor_017_asr_worker_failure_is_closed_and_does_not_leak_details",
        "test_mor_017_ffmpeg_proxy_failure_is_closed_and_cleans_candidate",
        "test_failed_render_explicit_new_id_cleans_only_exact_tracked_orphan",
        "test_media_operation_approve_export_claim_and_interrupt_preserve_authority",
        "test_proxy_project_change_after_encode_fails_before_pair_publish",
    ),
    "MOR-018": (
        "test_proxy_operation_pair_publish_reuse_and_project_invariants",
        "test_media_operation_approve_export_claim_and_interrupt_preserve_authority",
    ),
    "MOR-019": (
        "test_media_store_missing_read_is_side_effect_free_and_roundtrips",
        "test_status_missing_is_side_effect_free_and_terminal_is_pure",
    ),
    "MOR-020": (
        "test_media_operation_approve_export_uses_transaction_and_terminal_readback",
    ),
    "MOR-021": (
        "test_transcription_operation_binds_exact_result_and_readback_skips_preflight",
    ),
    "MOR-022": (
        "test_media_operation_approve_export_uses_transaction_and_terminal_readback",
    ),
    "MOR-023": (
        "test_transcription_operation_binds_exact_result_and_readback_skips_preflight",
    ),
    "MOR-024": (
        "test_proxy_same_id_conflict_and_terminal_readback_ignore_current_basis",
        "test_transcription_operation_binds_exact_result_and_readback_skips_preflight",
    ),
    "MOR-025": (
        "test_mor_025_transcription_rejects_each_changed_public_request_field",
        "test_mor_025_proxy_rejects_each_changed_public_request_field",
        "test_media_operation_approve_export_claim_and_interrupt_preserve_authority",
    ),
    "MOR-026": (
        "test_proxy_same_id_conflict_and_terminal_readback_ignore_current_basis",
    ),
    "MOR-027": (
        "test_media_operation_rejects_missing_unknown_or_wrong_typed_fields",
        "test_media_store_rejects_duplicate_keys_malformed_hash_and_cross_project",
    ),
}

_EXPECT_KEYS = {
    "MOR-001": {
        "binding",
        "project_revision",
        "result_kind",
        "status",
        "workflow_stage_changed",
    },
    "MOR-002": {
        "pair_published",
        "project_revision",
        "result_kind",
        "status",
        "workflow_stage_changed",
    },
    "MOR-003": {
        "decision_schema_version",
        "manifest_schema_version",
        "operation_record_advances_stage",
        "receipt_required",
        "render_plan_schema_version",
        "result_kind",
        "single_render",
        "status",
    },
    "MOR-004": {"record_mutated", "status", "worker_restarted"},
    "MOR-005": {
        "action",
        "artifact_inferred",
        "responsibility",
        "status",
    },
    "MOR-006": {
        "current_revision_checked",
        "current_stage_checked",
        "historical_input_reconstructed",
        "new_result_count",
        "same_record",
        "worker_invocations",
    },
    "MOR-007": {
        "all_cases_conflict",
        "error_code",
        "mutation",
        "worker_invocations",
    },
    "MOR-008": {
        "asr_invocations",
        "error_owner",
        "mutation",
        "record_created",
    },
    "MOR-009": {
        "error_owner",
        "mutation",
        "record_created",
        "renderer_invocations",
    },
    "MOR-010": {
        "action",
        "project_overwritten",
        "ready_proxy",
        "responsibility",
        "status",
    },
    "MOR-011": {
        "publish_invocations",
        "same_result_ref",
        "worker_invocations",
    },
    "MOR-012": {
        "active_transcript_changed",
        "raw_asr_adopted",
        "result_ref",
        "status",
    },
    "MOR-013": {
        "binding_repaired_by_existing_synchronizer",
        "operation_infers_success",
        "operation_status",
        "project_and_transcript_preserved",
    },
    "MOR-014": {
        "partial_adopted",
        "result_ref",
        "status",
        "status_cleanup",
    },
    "MOR-015": {
        "operation_advances_stage",
        "operation_status",
        "owner_and_marker_remain_authoritative",
        "project_lock_released_before_media_writer",
        "publish_lock_order",
        "status_cleanup",
    },
    "MOR-016": {
        "error_code",
        "evidence_preserved",
        "worker_invocations",
    },
    "MOR-017": {
        "absolute_path_leaked",
        "internal_exception_leaked",
        "responsibilities",
    },
    "MOR-018": {
        "approval_created_by_record",
        "checkpoint_changed",
        "project_schema_changed",
        "workflow_stage_equal_before",
    },
    "MOR-019": {
        "first_authorized_operation_creates_controlled_storage",
        "missing_status_creates_directory",
        "project_json_changed",
        "project_schema_changed",
    },
    "MOR-020": {
        "manifest_schema_version",
        "new_render_schema_created",
        "operation_record_advances_stage",
        "render_plan_schema_version",
    },
    "MOR-021": {
        "asr_invocations",
        "current_revision_revalidated",
        "publish_invocations",
        "same_result_ref",
        "status",
    },
    "MOR-022": {
        "current_run_record_hash_required",
        "current_stage_revalidated",
        "publish_invocations",
        "renderer_invocations",
        "same_result_ref",
        "status",
    },
    "MOR-023": {
        "asr_invocations",
        "historical_input_hash_reconstructed",
        "request_hash_matches_record",
        "same_result_ref",
        "status",
    },
    "MOR-024": {
        "current_source_or_project_revalidated",
        "request_hash_matches_record",
        "same_result_ref",
        "worker_invocations",
    },
    "MOR-025": {
        "all_cases_error_code",
        "mutation",
        "worker_invocations",
    },
    "MOR-026": {
        "input_hash_recomputed",
        "new_record_created",
        "preflight_invocations",
        "same_record",
        "worker_invocations",
    },
    "MOR-027": {
        "all_cases_error_code",
        "mutation",
        "record_reinterpreted_as_legacy_schema",
        "worker_invocations",
    },
}


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key: {key}")
        result[key] = value
    return result


def test_media_operation_vectors_are_strict_unique_and_canonical() -> None:
    payload = json.loads(
        VECTORS.read_text(encoding="utf-8"),
        object_pairs_hook=_strict_object,
    )
    assert set(payload) == {"schema_version", "kind", "vectors"}
    assert payload["schema_version"] == 1
    assert payload["kind"] == "roughcut_project_media_operation_vectors"
    vectors = payload["vectors"]
    assert isinstance(vectors, list)
    ids = [vector["id"] for vector in vectors]
    assert ids == [f"MOR-{index:03d}" for index in range(1, 28)]
    assert len(set(ids)) == 27

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


def test_media_operation_vector_expectations_have_semantic_traceability() -> None:
    payload = json.loads(
        VECTORS.read_text(encoding="utf-8"),
        object_pairs_hook=_strict_object,
    )
    assert {vector["id"] for vector in payload["vectors"]} == set(_OWNERS)
    assert set(_EXPECT_KEYS) == set(_OWNERS)
    for vector in payload["vectors"]:
        vector_id = vector["id"]
        assert set(vector["expect"]) == _EXPECT_KEYS[vector_id]
        assert _OWNERS[vector_id]
        assert all(owner.startswith("test_") for owner in _OWNERS[vector_id])
