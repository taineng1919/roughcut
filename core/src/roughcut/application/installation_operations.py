"""Installation-only OperationRecord coordination."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Generic, TypeVar
from uuid import uuid4

from roughcut.adapters.installation_operation_store import (
    InstallationOperationStore,
)
from roughcut.domain.installation_operation import (
    RUNNING_PHASES,
    InstallationOperationError,
    InstallationOperationFailure,
    InstallationOperationRecord,
    InstallationResultRef,
    InstallationScope,
    installation_input_hash,
    validate_operation_id,
)

T = TypeVar("T")


@dataclass(frozen=True)
class InstallationOperationOutcome(Generic[T]):
    record: InstallationOperationRecord
    result: T | None
    readback: bool


def new_installation_operation_id() -> str:
    return f"op_{uuid4().hex}"


def installation_operation_status(
    install_root: Path,
    operation_id: str,
) -> InstallationOperationRecord:
    """Read status and converge an abandoned nonterminal writer to interrupted."""

    validate_operation_id(operation_id)
    store = InstallationOperationStore(install_root)
    initial = store.read(operation_id, allow_writer_temp=True)
    if initial is None:
        raise InstallationOperationError(
            "operation_not_found",
            "Roughcut installation operation status did not find the requested record",
        )
    if initial.status in {"succeeded", "failed", "interrupted"}:
        return initial
    with store.writer(operation_id, create=False) as acquired:
        if not acquired:
            current = store.read(operation_id, allow_writer_temp=True)
            if current is None:
                raise InstallationOperationError(
                    "operation_integrity_error",
                    "Roughcut installation operation status lost the active record",
                )
            return current
        current = store.read(operation_id)
        if current is None:
            raise InstallationOperationError(
                "operation_integrity_error",
                "Roughcut installation operation status lost the abandoned record",
            )
        if current.status in {"succeeded", "failed", "interrupted"}:
            return current
        if current.status == "pending":
            current = _running_record(
                current,
                "component_installation_preparing",
            )
            store.write_locked(current)
        interrupted = _terminal_record(
            current,
            status="interrupted",
            failure=InstallationOperationFailure(
                code="component_installation_interrupted",
                responsibility="roughcut_bootstrap",
                action="recover_abandoned_component_installation",
                message_code="component_installation_interrupted",
            ),
        )
        return store.write_locked(interrupted)


def run_component_installation(
    install_root: Path,
    *,
    operation_id: str,
    approved_plan_hash: str,
    apply: Callable[[Callable[[str], None]], T],
    result_ref: Callable[[T], InstallationResultRef],
    preflight: Callable[[], object] | None = None,
    apply_preflight: Callable[[object, Callable[[str], None]], T] | None = None,
) -> InstallationOperationOutcome[T]:
    """Run or read back one exact approved component installation."""

    validate_operation_id(operation_id)
    input_hash = installation_input_hash(approved_plan_hash)
    store = InstallationOperationStore(install_root)
    existing = store.read(operation_id, allow_writer_temp=True)
    if existing is not None:
        _require_same_input(existing, input_hash)
        return InstallationOperationOutcome(existing, None, True)
    with store.writer(operation_id, create=True) as acquired:
        if not acquired:
            current = store.read(operation_id, allow_writer_temp=True)
            if current is None:
                raise InstallationOperationError(
                    "operation_integrity_error",
                    "Roughcut installation operation found a writer without a record",
                )
            _require_same_input(current, input_hash)
            return InstallationOperationOutcome(current, None, True)

        existing = store.read(operation_id)
        if existing is not None:
            _require_same_input(existing, input_hash)
            return InstallationOperationOutcome(existing, None, True)

        preflight_result: object | None = None
        if preflight is not None:
            preflight_result = preflight()

        created_at = _now()
        pending = InstallationOperationRecord(
            operation_id=operation_id,
            scope=InstallationScope(store.scope_hash),
            input_hash=input_hash,
            status="pending",
            phase_message_code="component_installation_preparing",
            created_at=created_at,
            started_at=None,
            updated_at=created_at,
            finished_at=None,
            result_ref=None,
            error=None,
        )
        store.write_locked(pending)
        running = _running_record(
            pending,
            "component_installation_preparing",
        )
        store.write_locked(running)
        active_record = running

        def update_phase(phase: str) -> None:
            nonlocal active_record
            if phase not in RUNNING_PHASES:
                raise InstallationOperationError(
                    "operation_transition_not_allowed",
                    "Roughcut installation operation rejected an unknown phase",
                )
            active_record = replace(
                active_record,
                phase_message_code=phase,
                updated_at=_now(),
            )
            store.write_locked(active_record)

        try:
            if apply_preflight is not None:
                if preflight_result is None:
                    raise InstallationOperationError(
                        "operation_integrity_error",
                        "Roughcut installation operation is missing its preflight result",
                    )
                applied = apply_preflight(preflight_result, update_phase)
            else:
                applied = apply(update_phase)
            succeeded = _terminal_record(
                active_record,
                status="succeeded",
                result=result_ref(applied),
            )
            return InstallationOperationOutcome(
                store.write_locked(succeeded),
                applied,
                False,
            )
        except (KeyboardInterrupt, SystemExit):
            interrupted = _terminal_record(
                active_record,
                status="interrupted",
                failure=InstallationOperationFailure(
                    code="component_installation_interrupted",
                    responsibility="roughcut_bootstrap",
                    action="interrupt_component_installation",
                    message_code="component_installation_interrupted",
                ),
            )
            store.write_locked(interrupted)
            raise
        except InstallationOperationError:
            raise
        except Exception as error:
            failure = classify_installation_failure(error)
            failed = _terminal_record(
                active_record,
                status="failed",
                failure=failure,
            )
            stored = store.write_locked(failed)
            raise InstallationOperationError(
                "component_installation_failed",
                "Roughcut installation operation failed; "
                f"responsibility={stored.error.responsibility if stored.error else 'unknown'}; "
                f"action={stored.error.action if stored.error else 'unknown'}",
            ) from error


def classify_installation_failure(
    error: BaseException,
) -> InstallationOperationFailure:
    responsibility = getattr(error, "failure_responsibility", None)
    action = getattr(error, "failure_action", None)
    if not isinstance(responsibility, str) or not isinstance(action, str):
        responsibility = "roughcut_component_installer"
        action = "install_components"
    reason_code = getattr(error, "failure_reason", None)
    if not isinstance(reason_code, str):
        reason_code = None
    return InstallationOperationFailure(
        code="component_installation_failed",
        responsibility=responsibility,
        action=action,
        message_code="component_installation_failed",
        reason_code=reason_code,
    )


def _require_same_input(
    record: InstallationOperationRecord,
    input_hash: str,
) -> None:
    if record.input_hash != input_hash:
        raise InstallationOperationError(
            "operation_input_conflict",
            "Roughcut installation operation refused the same ID with different input",
        )


def _running_record(
    record: InstallationOperationRecord,
    phase: str,
) -> InstallationOperationRecord:
    now = _now()
    return replace(
        record,
        status="running",
        phase_message_code=phase,
        started_at=record.started_at or now,
        updated_at=now,
    )


def _terminal_record(
    record: InstallationOperationRecord,
    *,
    status: str,
    result: InstallationResultRef | None = None,
    failure: InstallationOperationFailure | None = None,
) -> InstallationOperationRecord:
    if status not in {"succeeded", "failed", "interrupted"}:
        raise InstallationOperationError(
            "operation_transition_not_allowed",
            "Roughcut installation operation rejected an unknown terminal status",
        )
    now = _now()
    return replace(
        record,
        status=status,  # type: ignore[arg-type]
        phase_message_code=f"component_installation_{status}",
        updated_at=now,
        finished_at=now,
        result_ref=result,
        error=failure,
    )


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
