from __future__ import annotations

from copy import deepcopy

import pytest

from roughcut.domain.errors import WorkflowError
from roughcut.domain.workflow import (
    MulticamSetup,
    MulticamSetupDeclaration,
    canonical_sha256_v1,
)


def _source_snapshot(source_id: str) -> dict[str, object]:
    return {
        "source_id": source_id,
        "import_mode": "linked",
        "fingerprint": {
            "size": 100,
            "mtime_ns": 1,
            "sha256_head_tail": f"fixture-{source_id}",
        },
        "display_name": f"{source_id}.wav",
        "tags": [],
        "note": "",
    }


def _setup_payload(*, no_aux: bool = False) -> dict[str, object]:
    source_ids = ["src_main", "src_aux"] if not no_aux else ["src_main"]
    auxiliary = [] if no_aux else [{"camera_id": "aux_a", "ordered_source_ids": ["src_aux"]}]
    pairs = [] if no_aux else [{"main_source_id": "src_main", "auxiliary_source_id": "src_aux"}]
    snapshots = [_source_snapshot(source_id) for source_id in source_ids]
    body: dict[str, object] = {
        "schema_version": 1,
        "project_id": "proj_test",
        "workflow_run_id": "wfr_test",
        "main_camera": {"camera_id": "main", "ordered_source_ids": ["src_main"]},
        "auxiliary_cameras": auxiliary,
        "source_pairs": pairs,
        "asr_scope": [
            {
                "source_id": "src_main",
                "transcribe": False,
                "speaker_diarization": False,
            }
        ],
        "source_snapshots": snapshots,
        "source_snapshot_hash": canonical_sha256_v1(snapshots),
    }
    body["setup_id"] = f"mcs_{canonical_sha256_v1(body)[:32]}"
    return body


def test_multicam_setup_roundtrip_preserves_exact_order_and_no_aux() -> None:
    payload = _setup_payload(no_aux=True)
    setup = MulticamSetup.from_dict(payload)

    assert setup.to_dict() == payload
    assert setup.auxiliary_cameras == ()
    assert setup.source_pairs == ()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.pop("source_pairs"),
        lambda value: value.update({"unexpected": True}),
        lambda value: value.update({"auxiliary_cameras": "not-an-array"}),
        lambda value: value.update({"source_snapshot_hash": "bad"}),
        lambda value: value["source_snapshots"][0].update({"import_mode": []}),
        lambda value: value["source_snapshots"][0].update({1: "not-a-field"}),
    ],
)
def test_multicam_setup_rejects_missing_unknown_and_malformed_fields(mutation) -> None:
    payload = _setup_payload()
    mutation(payload)

    with pytest.raises(WorkflowError):
        MulticamSetup.from_dict(payload)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["auxiliary_cameras"].append(
            {"camera_id": "aux_a", "ordered_source_ids": ["src_aux"]}
        ),
        lambda value: value["main_camera"].update(
            {"ordered_source_ids": ["src_main", "src_main"]}
        ),
        lambda value: value["auxiliary_cameras"].__setitem__(
            0, {"camera_id": "main", "ordered_source_ids": ["src_aux"]}
        ),
    ],
)
def test_multicam_setup_rejects_duplicate_or_cross_role_camera_sources(mutation) -> None:
    payload = _setup_payload()
    mutation(payload)

    with pytest.raises(WorkflowError):
        MulticamSetup.from_dict(payload)


def test_multicam_setup_rejects_pair_outside_declared_group() -> None:
    payload = _setup_payload()
    payload["source_pairs"] = [
        {"main_source_id": "src_main", "auxiliary_source_id": "src_missing"}
    ]

    with pytest.raises(WorkflowError):
        MulticamSetup.from_dict(payload)


def test_multicam_setup_rejects_duplicate_pair_and_snapshot_mismatch() -> None:
    payload = _setup_payload()
    payload["source_pairs"] = [
        {"main_source_id": "src_main", "auxiliary_source_id": "src_aux"},
        {"main_source_id": "src_main", "auxiliary_source_id": "src_aux"},
    ]
    with pytest.raises(WorkflowError):
        MulticamSetup.from_dict(payload)

    payload = _setup_payload()
    changed = deepcopy(payload["source_snapshots"])
    assert isinstance(changed, list)
    changed[0]["display_name"] = "changed.wav"
    payload["source_snapshots"] = changed
    with pytest.raises(WorkflowError):
        MulticamSetup.from_dict(payload)


def _declaration_payload(*, no_aux: bool = False) -> dict[str, object]:
    return {
        "schema_version": 1,
        "main_camera": {"camera_id": "main", "ordered_source_ids": ["src_main"]},
        "auxiliary_cameras": (
            []
            if no_aux
            else [{"camera_id": "aux_a", "ordered_source_ids": ["src_aux"]}]
        ),
        "source_pairs": (
            []
            if no_aux
            else [{"main_source_id": "src_main", "auxiliary_source_id": "src_aux"}]
        ),
    }


def test_multicam_setup_declaration_roundtrip_is_user_facts_only() -> None:
    payload = _declaration_payload()
    declaration = MulticamSetupDeclaration.from_dict(payload)

    assert declaration.to_dict() == payload
    assert set(payload) == {
        "schema_version",
        "main_camera",
        "auxiliary_cameras",
        "source_pairs",
    }


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.pop("main_camera"),
        lambda value: value.update({"setup_id": "mcs_injected"}),
        lambda value: value.update({"project_id": "proj_injected"}),
        lambda value: value.update({"source_snapshots": []}),
        lambda value: value.update({"source_snapshot_hash": "a" * 64}),
        lambda value: value.update({"workflow_run_id": "wfr_injected"}),
        lambda value: value.update({"auxiliary_cameras": "not-an-array"}),
    ],
)
def test_multicam_setup_declaration_rejects_persisted_identity_fields(mutation) -> None:
    payload = _declaration_payload()
    mutation(payload)

    with pytest.raises(WorkflowError):
        MulticamSetupDeclaration.from_dict(payload)


def test_multicam_setup_declaration_rejects_pair_outside_camera_groups() -> None:
    payload = _declaration_payload()
    payload["source_pairs"] = [
        {"main_source_id": "src_missing", "auxiliary_source_id": "src_aux"}
    ]

    with pytest.raises(WorkflowError):
        MulticamSetupDeclaration.from_dict(payload)
