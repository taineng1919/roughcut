from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VECTORS = (
    ROOT
    / "core"
    / "tests"
    / "fixtures"
    / "installation-operation-record-vectors.json"
)

_OWNERS = {
    "IOR-001": (
        "test_installation_operation_pending_running_succeeded_and_readback",
        "test_only_missing_speaker_is_installed_and_reused_without_copying_base_models",
    ),
    "IOR-002": ("test_installation_operation_failure_has_closed_responsibility",),
    "IOR-003": ("test_installation_operation_keyboard_interrupt_is_persisted",),
    "IOR-004": ("test_installation_operation_hard_exit_converges_to_interrupted",),
    "IOR-005": (
        "test_installation_operation_live_writer_remains_running",
        "test_installation_operation_live_subprocess_lock_remains_running",
    ),
    "IOR-006": ("test_installation_operation_transitions_only_move_forward",),
    "IOR-007": (
        "test_existing_first_readback_skips_plan_and_prerequisite_checks",
    ),
    "IOR-008": (
        "test_installation_operation_same_id_different_input_conflicts_without_apply",
    ),
    "IOR-009": ("test_bootstrap_apply_requires_operation_id_and_full_plan",),
    "IOR-010": (
        "test_apply_rejects_stale_hash_before_network_or_managed_write",
        "test_bootstrap_stale_full_plan_fails_before_download_or_publish",
    ),
    "IOR-011": (
        "test_bootstrap_component_apply_records_success_and_response_loss_readback",
    ),
    "IOR-012": ("test_bootstrap_runtime_publish_failure_matches_operation_record",),
    "IOR-013": (
        "test_installation_operation_rejects_invalid_closed_payloads",
        "test_installation_store_rejects_duplicate_keys_unknown_schema_and_changed_scope",
    ),
    "IOR-014": (
        "test_installation_store_rejects_symlink_record_and_directory",
        "test_installation_store_rejects_hardlinked_record_and_writer_lock",
        "test_installation_store_rejects_path_escape",
    ),
    "IOR-015": ("test_bootstrap_operation_status_is_pure_and_does_not_plan",),
    "IOR-016": (
        "test_installation_operation_live_subprocess_lock_remains_running",
        "test_installation_store_windows_lock_branch_has_static_fixture_coverage_only",
    ),
    "IOR-017": (
        "test_staged_full_verification_framing_failure_maps_to_component_installer",
    ),
    "IOR-018": ("test_new_operation_stale_preflight_has_no_operation_record",),
    "IOR-019": ("test_full_verification_text_does_not_change_typed_mapping",),
    "IOR-020": ("test_network_transport_failure_maps_to_https_runtime",),
}


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key: {key}")
        result[key] = value
    return result


def test_installation_operation_vectors_have_exact_executable_owners() -> None:
    payload = json.loads(
        VECTORS.read_text(encoding="utf-8"),
        object_pairs_hook=_strict_object,
    )
    assert set(payload) == {"schema_version", "kind", "vectors"}
    assert payload["schema_version"] == 1
    vectors = payload["vectors"]
    assert isinstance(vectors, list)
    ids = [vector["id"] for vector in vectors]
    assert len(ids) == 20
    assert len(set(ids)) == len(ids)
    assert set(ids) == set(_OWNERS)

    test_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            ROOT / "core/tests/domain/test_installation_operation.py",
            ROOT / "core/tests/adapters/test_installation_operation_store.py",
            ROOT / "core/tests/application/test_installation_operations.py",
            ROOT / "core/tests/test_bootstrap_installation_operation.py",
            ROOT / "core/tests/test_component_installation.py",
        )
    )
    for owners in _OWNERS.values():
        for owner in owners:
            assert f"def {owner}(" in test_sources
