"""Closed installation OperationRecord schema 1."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, cast

from roughcut.domain.errors import WorkflowError
from roughcut.domain.workflow import (
    canonical_sha256_v1,
    validate_sha256,
    validate_timestamp,
)

OPERATION_SCHEMA_VERSION = 1
OPERATION_TYPE = "component_installation"
OPERATION_ID_PATTERN = re.compile(r"^op_[A-Za-z0-9_-]{1,125}$")
OPERATION_STATUSES = frozenset(
    {"pending", "running", "succeeded", "failed", "interrupted"}
)
RUNNING_PHASES = (
    "component_installation_preparing",
    "component_installation_downloading",
    "component_installation_installing",
    "component_installation_verifying",
    "component_installation_publishing_runtime",
)
TERMINAL_PHASES = {
    "succeeded": "component_installation_succeeded",
    "failed": "component_installation_failed",
    "interrupted": "component_installation_interrupted",
}
RESPONSIBILITIES = frozenset(
    {
        "roughcut_bootstrap",
        "roughcut_component_installer",
        "roughcut_runtime_binding",
        "python_https_runtime",
        "component_artifact",
        "user_input",
    }
)
FAILURE_ACTIONS = frozenset(
    {
        "install_components",
        "publish_runtime_binding",
        "download_component_artifact",
        "verify_component_artifact",
        "validate_approved_full_plan",
        "interrupt_component_installation",
        "recover_abandoned_component_installation",
    }
)
INSTALLATION_FAILURE_REASON_CODES = frozenset(
    {
        "runtime_publish_stale_plan",
        "runtime_publish_existing_binding_invalid",
        "runtime_publish_lock_failed",
        "runtime_publish_atomic_replace_failed",
        "runtime_publish_binding_validation_failed",
        "runtime_publish_failed",
    }
)

OperationStatus = Literal[
    "pending", "running", "succeeded", "failed", "interrupted"
]


class InstallationOperationError(RuntimeError):
    """Stable installation-operation failure with a closed error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _error(code: str, evidence: str) -> InstallationOperationError:
    return InstallationOperationError(
        code,
        f"Roughcut installation operation {evidence}",
    )


def validate_operation_id(value: object) -> str:
    if not isinstance(value, str) or OPERATION_ID_PATTERN.fullmatch(value) is None:
        raise _error("operation_integrity_error", "rejected an unsafe operation ID")
    return value


def _sha256(value: object, *, field: str) -> str:
    try:
        return validate_sha256(value, field=field)
    except WorkflowError as error:
        raise _error(
            "operation_integrity_error",
            f"rejected invalid {field}",
        ) from error


def _timestamp(value: object, *, field: str) -> str:
    try:
        return validate_timestamp(value, field=field)
    except WorkflowError as error:
        raise _error(
            "operation_integrity_error",
            f"rejected invalid {field}",
        ) from error


def _closed(
    value: object, fields: set[str], *, description: str
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise _error(
            "operation_integrity_error",
            f"rejected non-closed {description}",
        )
    return cast(dict[str, Any], value)


def _required_string(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise _error(
            "operation_integrity_error",
            f"rejected invalid {field}",
        )
    return value


@dataclass(frozen=True)
class InstallationScope:
    install_root_hash: str
    kind: str = "installation"

    def __post_init__(self) -> None:
        if self.kind != "installation":
            raise _error(
                "operation_integrity_error",
                "rejected an invalid installation scope kind",
            )
        _sha256(self.install_root_hash, field="install_root_hash")

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "install_root_hash": self.install_root_hash,
        }

    @classmethod
    def from_dict(cls, value: object) -> InstallationScope:
        data = _closed(
            value,
            {"kind", "install_root_hash"},
            description="installation scope",
        )
        return cls(
            kind=_required_string(data["kind"], field="scope.kind"),
            install_root_hash=_sha256(
                data["install_root_hash"],
                field="scope.install_root_hash",
            ),
        )


@dataclass(frozen=True)
class InstallationResultRef:
    approved_plan_hash: str
    runtime_binding_sha256: str
    component_manifest_sha256: str | None
    kind: str = "runtime_binding"
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.kind != "runtime_binding" or self.schema_version != 1:
            raise _error(
                "operation_integrity_error",
                "rejected an invalid installation result identity",
            )
        _sha256(self.approved_plan_hash, field="approved_plan_hash")
        _sha256(
            self.runtime_binding_sha256,
            field="runtime_binding_sha256",
        )
        if self.component_manifest_sha256 is not None:
            _sha256(
                self.component_manifest_sha256,
                field="component_manifest_sha256",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "schema_version": self.schema_version,
            "approved_plan_hash": self.approved_plan_hash,
            "runtime_binding_sha256": self.runtime_binding_sha256,
            "component_manifest_sha256": self.component_manifest_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> InstallationResultRef:
        data = _closed(
            value,
            {
                "kind",
                "schema_version",
                "approved_plan_hash",
                "runtime_binding_sha256",
                "component_manifest_sha256",
            },
            description="installation result ref",
        )
        schema_version = data["schema_version"]
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise _error(
                "operation_integrity_error",
                "rejected an invalid result schema version",
            )
        manifest_hash = data["component_manifest_sha256"]
        if manifest_hash is not None and not isinstance(manifest_hash, str):
            raise _error(
                "operation_integrity_error",
                "rejected an invalid component manifest hash",
            )
        return cls(
            kind=_required_string(data["kind"], field="result_ref.kind"),
            schema_version=schema_version,
            approved_plan_hash=_sha256(
                data["approved_plan_hash"],
                field="result_ref.approved_plan_hash",
            ),
            runtime_binding_sha256=_sha256(
                data["runtime_binding_sha256"],
                field="result_ref.runtime_binding_sha256",
            ),
            component_manifest_sha256=(
                None
                if manifest_hash is None
                else _sha256(
                    manifest_hash,
                    field="result_ref.component_manifest_sha256",
                )
            ),
        )


@dataclass(frozen=True)
class InstallationOperationFailure:
    code: str
    responsibility: str
    action: str
    message_code: str
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.code not in {
            "component_installation_failed",
            "component_installation_interrupted",
        }:
            raise _error(
                "operation_integrity_error",
                "rejected an unknown installation failure code",
            )
        if self.responsibility not in RESPONSIBILITIES:
            raise _error(
                "operation_integrity_error",
                "rejected an unknown installation responsibility",
            )
        if self.action not in FAILURE_ACTIONS or self.message_code != self.code:
            raise _error(
                "operation_integrity_error",
                "rejected an invalid installation failure action",
            )
        if (
            self.reason_code is not None
            and self.reason_code not in INSTALLATION_FAILURE_REASON_CODES
        ):
            raise _error(
                "operation_integrity_error",
                "rejected an unknown installation failure reason",
            )

    def to_dict(self) -> dict[str, str]:
        result = {
            "code": self.code,
            "responsibility": self.responsibility,
            "action": self.action,
            "message_code": self.message_code,
        }
        if self.reason_code is not None:
            result["reason_code"] = self.reason_code
        return result

    @classmethod
    def from_dict(cls, value: object) -> InstallationOperationFailure:
        fields = {"code", "responsibility", "action", "message_code"}
        with_reason = fields | {"reason_code"}
        if not isinstance(value, dict) or set(value) not in (fields, with_reason):
            raise _error(
                "operation_integrity_error",
                "rejected non-closed installation error",
            )
        data = cast(dict[str, Any], value)
        reason = data.get("reason_code")
        return cls(
            code=_required_string(data["code"], field="error.code"),
            responsibility=_required_string(
                data["responsibility"],
                field="error.responsibility",
            ),
            action=_required_string(data["action"], field="error.action"),
            message_code=_required_string(
                data["message_code"],
                field="error.message_code",
            ),
            reason_code=(
                None
                if reason is None
                else _required_string(reason, field="error.reason_code")
            ),
        )


@dataclass(frozen=True)
class InstallationOperationRecord:
    operation_id: str
    scope: InstallationScope
    input_hash: str
    status: OperationStatus
    phase_message_code: str
    created_at: str
    started_at: str | None
    updated_at: str
    finished_at: str | None
    result_ref: InstallationResultRef | None
    error: InstallationOperationFailure | None
    operation_type: str = OPERATION_TYPE
    schema_version: int = OPERATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        validate_operation_id(self.operation_id)
        if (
            self.schema_version != OPERATION_SCHEMA_VERSION
            or self.operation_type != OPERATION_TYPE
        ):
            raise _error(
                "operation_integrity_error",
                "rejected an unknown operation schema or type",
            )
        _sha256(self.input_hash, field="input_hash")
        if self.status not in OPERATION_STATUSES:
            raise _error(
                "operation_integrity_error",
                "rejected an unknown operation status",
            )
        _timestamp(self.created_at, field="created_at")
        _timestamp(self.updated_at, field="updated_at")
        if self.updated_at < self.created_at:
            raise _error(
                "operation_integrity_error",
                "rejected an operation whose updated time moved backward",
            )
        self._validate_state_shape()

    def _validate_state_shape(self) -> None:
        if self.status == "pending":
            valid = (
                self.phase_message_code == "component_installation_preparing"
                and self.started_at is None
                and self.finished_at is None
                and self.result_ref is None
                and self.error is None
            )
        elif self.status == "running":
            valid = (
                self.phase_message_code in RUNNING_PHASES
                and self.started_at is not None
                and self.finished_at is None
                and self.result_ref is None
                and self.error is None
            )
        elif self.status == "succeeded":
            valid = (
                self.phase_message_code == TERMINAL_PHASES["succeeded"]
                and self.started_at is not None
                and self.finished_at is not None
                and self.result_ref is not None
                and self.error is None
            )
        else:
            valid = (
                self.phase_message_code == TERMINAL_PHASES[self.status]
                and self.started_at is not None
                and self.finished_at is not None
                and self.result_ref is None
                and self.error is not None
                and self.error.code
                == (
                    "component_installation_interrupted"
                    if self.status == "interrupted"
                    else "component_installation_failed"
                )
            )
        if not valid:
            raise _error(
                "operation_integrity_error",
                "rejected an inconsistent operation status payload",
            )
        for field, value in (
            ("started_at", self.started_at),
            ("finished_at", self.finished_at),
        ):
            if value is not None:
                _timestamp(value, field=field)
                if value < self.created_at:
                    raise _error(
                        "operation_integrity_error",
                        f"rejected an operation whose {field} moved backward",
                    )
        if self.started_at is not None and self.started_at > self.updated_at:
            raise _error(
                "operation_integrity_error",
                "rejected an operation updated before it started",
            )
        if self.finished_at is not None and self.finished_at != self.updated_at:
            raise _error(
                "operation_integrity_error",
                "rejected a terminal operation with inconsistent finish time",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "operation_id": self.operation_id,
            "scope": self.scope.to_dict(),
            "operation_type": self.operation_type,
            "input_hash": self.input_hash,
            "status": self.status,
            "phase_message_code": self.phase_message_code,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "result_ref": (
                None if self.result_ref is None else self.result_ref.to_dict()
            ),
            "error": None if self.error is None else self.error.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: object) -> InstallationOperationRecord:
        data = _closed(
            value,
            {
                "schema_version",
                "operation_id",
                "scope",
                "operation_type",
                "input_hash",
                "status",
                "phase_message_code",
                "created_at",
                "started_at",
                "updated_at",
                "finished_at",
                "result_ref",
                "error",
            },
            description="installation OperationRecord",
        )
        schema_version = data["schema_version"]
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise _error(
                "operation_integrity_error",
                "rejected an invalid operation schema version",
            )
        status = data["status"]
        if not isinstance(status, str) or status not in OPERATION_STATUSES:
            raise _error(
                "operation_integrity_error",
                "rejected an unknown operation status",
            )
        for field in ("started_at", "finished_at"):
            if data[field] is not None and not isinstance(data[field], str):
                raise _error(
                    "operation_integrity_error",
                    f"rejected invalid {field}",
                )
        return cls(
            schema_version=schema_version,
            operation_id=validate_operation_id(data["operation_id"]),
            scope=InstallationScope.from_dict(data["scope"]),
            operation_type=_required_string(
                data["operation_type"],
                field="operation_type",
            ),
            input_hash=_sha256(data["input_hash"], field="input_hash"),
            status=cast(OperationStatus, status),
            phase_message_code=_required_string(
                data["phase_message_code"],
                field="phase_message_code",
            ),
            created_at=_timestamp(data["created_at"], field="created_at"),
            started_at=cast(str | None, data["started_at"]),
            updated_at=_timestamp(data["updated_at"], field="updated_at"),
            finished_at=cast(str | None, data["finished_at"]),
            result_ref=(
                None
                if data["result_ref"] is None
                else InstallationResultRef.from_dict(data["result_ref"])
            ),
            error=(
                None
                if data["error"] is None
                else InstallationOperationFailure.from_dict(data["error"])
            ),
        )

    @property
    def record_hash(self) -> str:
        return canonical_sha256_v1(self.to_dict())


def installation_scope_hash(install_root: str) -> str:
    return canonical_sha256_v1(
        {
            "hash_schema": 1,
            "hash_kind": "installation_scope",
            "install_root": install_root,
        }
    )


def installation_input_hash(approved_plan_hash: str) -> str:
    _sha256(approved_plan_hash, field="approved_plan_hash")
    return canonical_sha256_v1(
        {
            "hash_schema": 1,
            "hash_kind": "component_installation_input",
            "operation_type": OPERATION_TYPE,
            "approved_plan_hash": approved_plan_hash,
        }
    )


def validate_installation_transition(
    before: InstallationOperationRecord,
    after: InstallationOperationRecord,
) -> None:
    if (
        before.operation_id,
        before.scope,
        before.operation_type,
        before.input_hash,
        before.created_at,
    ) != (
        after.operation_id,
        after.scope,
        after.operation_type,
        after.input_hash,
        after.created_at,
    ):
        raise _error(
            "operation_transition_not_allowed",
            "refused to change immutable operation identity",
        )
    allowed = {
        "pending": {"running"},
        "running": {"running", "succeeded", "failed", "interrupted"},
        "succeeded": set(),
        "failed": set(),
        "interrupted": set(),
    }
    if after.status not in allowed[before.status]:
        raise _error(
            "operation_transition_not_allowed",
            f"refused transition {before.status}->{after.status}",
        )
    if after.updated_at < before.updated_at:
        raise _error(
            "operation_transition_not_allowed",
            "refused an updated time that moved backward",
        )
    if before.status == after.status == "running":
        before_index = RUNNING_PHASES.index(before.phase_message_code)
        after_index = RUNNING_PHASES.index(after.phase_message_code)
        if after_index < before_index:
            raise _error(
                "operation_transition_not_allowed",
                "refused a running phase that moved backward",
            )
