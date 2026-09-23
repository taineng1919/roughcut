from __future__ import annotations

from dataclasses import replace

import pytest

from roughcut.domain.installation_operation import (
    InstallationOperationError,
    InstallationOperationFailure,
    InstallationOperationRecord,
    InstallationResultRef,
    InstallationScope,
    installation_input_hash,
    validate_installation_transition,
)


def _record(status: str = "pending") -> InstallationOperationRecord:
    base = InstallationOperationRecord(
        operation_id="op_fixture",
        scope=InstallationScope("a" * 64),
        input_hash=installation_input_hash("b" * 64),
        status="pending",
        phase_message_code="component_installation_preparing",
        created_at="2026-07-29T08:00:00.000000Z",
        started_at=None,
        updated_at="2026-07-29T08:00:00.000000Z",
        finished_at=None,
        result_ref=None,
        error=None,
    )
    if status == "pending":
        return base
    running = replace(
        base,
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
            phase_message_code="component_installation_succeeded",
            updated_at="2026-07-29T08:00:02.000000Z",
            finished_at="2026-07-29T08:00:02.000000Z",
            result_ref=InstallationResultRef(
                approved_plan_hash="b" * 64,
                runtime_binding_sha256="c" * 64,
                component_manifest_sha256=None,
            ),
        )
    code = (
        "component_installation_interrupted"
        if status == "interrupted"
        else "component_installation_failed"
    )
    return replace(
        running,
        status=status,  # type: ignore[arg-type]
        phase_message_code=code,
        updated_at="2026-07-29T08:00:02.000000Z",
        finished_at="2026-07-29T08:00:02.000000Z",
        error=InstallationOperationFailure(
            code=code,
            responsibility="roughcut_component_installer",
            action="install_components",
            message_code=code,
        ),
    )


def test_installation_operation_roundtrip_and_hash_are_closed() -> None:
    for status in ("pending", "running", "succeeded", "failed", "interrupted"):
        record = _record(status)
        assert InstallationOperationRecord.from_dict(record.to_dict()) == record
        assert len(record.record_hash) == 64


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: {key: value for key, value in payload.items() if key != "input_hash"},
        lambda payload: {**payload, "unknown": True},
        lambda payload: {**payload, "schema_version": 2},
        lambda payload: {**payload, "operation_id": "../escape"},
        lambda payload: {**payload, "status": "queued"},
        lambda payload: {
            **payload,
            "scope": {**payload["scope"], "path": "/private/install"},
        },
    ],
)
def test_installation_operation_rejects_invalid_closed_payloads(mutation) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(InstallationOperationError) as raised:
        InstallationOperationRecord.from_dict(mutation(_record().to_dict()))
    assert raised.value.code == "operation_integrity_error"


def test_installation_operation_transitions_only_move_forward() -> None:
    pending = _record()
    running = _record("running")
    succeeded = _record("succeeded")
    validate_installation_transition(pending, running)
    validate_installation_transition(running, succeeded)

    with pytest.raises(InstallationOperationError) as raised:
        validate_installation_transition(succeeded, running)
    assert raised.value.code == "operation_transition_not_allowed"

    regressed = replace(
        running,
        phase_message_code="component_installation_preparing",
    )
    downloading = replace(
        running,
        phase_message_code="component_installation_downloading",
    )
    validate_installation_transition(regressed, downloading)
    with pytest.raises(InstallationOperationError):
        validate_installation_transition(downloading, regressed)
