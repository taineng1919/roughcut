"""Closed Draft Workspace Checkpoint schema 1."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from roughcut.domain.errors import WorkflowError
from roughcut.domain.workflow import (
    ArtifactRef,
    canonical_sha256_v1,
    validate_safe_id,
    validate_sha256,
    validate_timestamp,
)

_OPERATION_ID = re.compile(r"^dwop_(0|[1-9][0-9]*)_[a-f0-9]{32}$")
_OPERATIONS = frozenset(
    {
        "initialize",
        "edit_delete",
        "edit_move",
        "edit_insert",
        "punctuation_edit",
        "narration_edit",
        "section_reorder",
        "section_rename",
        "section_split",
        "section_merge",
        "section_delete",
        "candidate_select",
        "external_submit_advance",
        "undo",
        "redo",
        "return_to_draft_reset",
        "workflow_anchor_reset",
    }
)


def _error(evidence: str) -> WorkflowError:
    return WorkflowError(
        "draft_workspace_integrity_error",
        f"Roughcut Draft workspace rejected checkpoint integrity: {evidence}",
    )


def _closed(
    data: object, fields: set[str], *, description: str
) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) != fields:
        raise _error(f"{description} fields are invalid")
    return data


def _integer(value: object, *, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _error(f"{field} must be an integer >= {minimum}")
    return value


def _safe_id(value: object, *, field: str) -> str:
    try:
        return validate_safe_id(value, field=field)
    except WorkflowError as error:
        raise _error(f"{field} is invalid") from error


def _sha256(value: object, *, field: str) -> str:
    try:
        return validate_sha256(value, field=field)
    except WorkflowError as error:
        raise _error(f"{field} is invalid") from error


def _artifact_ref(value: object, *, field: str) -> ArtifactRef:
    try:
        ref = ArtifactRef.from_dict(value)
    except WorkflowError as error:
        raise _error(f"{field} is invalid") from error
    if ref.schema_version not in {1, 2} or ref.snapshot is not None:
        raise _error(f"{field} must be a Content Draft ArtifactRef without snapshot")
    return ref


@dataclass(frozen=True)
class DraftWorkspaceBinding:
    source_id: str
    transcript_version_id: str
    transcript_content_hash: str

    def __post_init__(self) -> None:
        _safe_id(self.source_id, field="source_id")
        _safe_id(self.transcript_version_id, field="transcript_version_id")
        _sha256(self.transcript_content_hash, field="transcript_content_hash")

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "transcript_version_id": self.transcript_version_id,
            "transcript_content_hash": self.transcript_content_hash,
        }

    @classmethod
    def from_dict(cls, data: object) -> DraftWorkspaceBinding:
        value = _closed(
            data,
            {"source_id", "transcript_version_id", "transcript_content_hash"},
            description="checkpoint binding",
        )
        return cls(
            source_id=_safe_id(value["source_id"], field="source_id"),
            transcript_version_id=_safe_id(
                value["transcript_version_id"], field="transcript_version_id"
            ),
            transcript_content_hash=_sha256(
                value["transcript_content_hash"], field="transcript_content_hash"
            ),
        )


@dataclass(frozen=True)
class DraftWorkspaceLastCommit:
    operation_id: str
    expected_generation: int
    operation: str
    input_hash: str
    result_candidate_ref: ArtifactRef

    def __post_init__(self) -> None:
        if (
            not isinstance(self.operation_id, str)
            or _OPERATION_ID.fullmatch(self.operation_id) is None
        ):
            raise _error("last_commit operation_id is invalid")
        expected = _integer(
            self.expected_generation, field="expected_generation", minimum=0
        )
        if int(self.operation_id.split("_", 2)[1]) != expected:
            raise _error("operation_id generation does not match expected_generation")
        if self.operation not in _OPERATIONS:
            raise _error("last_commit operation is unsupported")
        _sha256(self.input_hash, field="input_hash")
        if (
            self.result_candidate_ref.schema_version not in {1, 2}
            or self.result_candidate_ref.snapshot is not None
        ):
            raise _error("last_commit result_candidate_ref is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "expected_generation": self.expected_generation,
            "operation": self.operation,
            "input_hash": self.input_hash,
            "result_candidate_ref": self.result_candidate_ref.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: object) -> DraftWorkspaceLastCommit:
        value = _closed(
            data,
            {
                "operation_id",
                "expected_generation",
                "operation",
                "input_hash",
                "result_candidate_ref",
            },
            description="checkpoint last_commit",
        )
        operation = value["operation"]
        if not isinstance(operation, str):
            raise _error("last_commit operation must be a string")
        operation_id = value["operation_id"]
        if not isinstance(operation_id, str):
            raise _error("last_commit operation_id must be a string")
        return cls(
            operation_id=operation_id,
            expected_generation=_integer(
                value["expected_generation"],
                field="expected_generation",
                minimum=0,
            ),
            operation=operation,
            input_hash=_sha256(value["input_hash"], field="input_hash"),
            result_candidate_ref=_artifact_ref(
                value["result_candidate_ref"], field="result_candidate_ref"
            ),
        )


@dataclass(frozen=True)
class DraftWorkspaceCheckpoint:
    schema_version: int
    project_id: str
    workflow_run_id: str
    generation: int
    current_candidate_ref: ArtifactRef
    project_revision: int
    ordered_bindings: tuple[DraftWorkspaceBinding, ...]
    context_hash: str
    redo_candidate_refs: tuple[ArtifactRef, ...]
    last_commit: DraftWorkspaceLastCommit
    audit_review_session_id: str | None
    updated_at: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise _error("checkpoint schema_version is unsupported")
        _safe_id(self.project_id, field="project_id")
        _safe_id(self.workflow_run_id, field="workflow_run_id")
        generation = _integer(self.generation, field="generation", minimum=1)
        _integer(self.project_revision, field="project_revision", minimum=0)
        if (
            self.current_candidate_ref.schema_version not in {1, 2}
            or self.current_candidate_ref.snapshot is not None
        ):
            raise _error("current_candidate_ref is invalid")
        if not self.ordered_bindings:
            raise _error("checkpoint ordered_bindings must be non-empty")
        source_ids = [binding.source_id for binding in self.ordered_bindings]
        if len(source_ids) != len(set(source_ids)):
            raise _error("checkpoint ordered_bindings contain duplicate sources")
        _sha256(self.context_hash, field="context_hash")
        if self.last_commit.result_candidate_ref != self.current_candidate_ref:
            raise _error("last_commit result does not match current candidate")
        if self.last_commit.expected_generation != generation - 1:
            raise _error("last_commit expected_generation does not precede generation")
        redo_ids = [ref.artifact_id for ref in self.redo_candidate_refs]
        if len(redo_ids) != len(set(redo_ids)):
            raise _error("redo_candidate_refs contain duplicate artifacts")
        if self.current_candidate_ref.artifact_id in redo_ids:
            raise _error("current candidate cannot also be a redo candidate")
        for ref in self.redo_candidate_refs:
            if ref.schema_version not in {1, 2} or ref.snapshot is not None:
                raise _error("redo_candidate_ref is invalid")
        if self.audit_review_session_id is not None:
            _safe_id(
                self.audit_review_session_id, field="audit_review_session_id"
            )
        try:
            validate_timestamp(self.updated_at, field="updated_at")
        except WorkflowError as error:
            raise _error("updated_at is invalid") from error

    @property
    def checkpoint_hash(self) -> str:
        return canonical_sha256_v1(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "workflow_run_id": self.workflow_run_id,
            "generation": self.generation,
            "current_candidate_ref": self.current_candidate_ref.to_dict(),
            "project_revision": self.project_revision,
            "ordered_bindings": [
                binding.to_dict() for binding in self.ordered_bindings
            ],
            "context_hash": self.context_hash,
            "redo_candidate_refs": [
                ref.to_dict() for ref in self.redo_candidate_refs
            ],
            "last_commit": self.last_commit.to_dict(),
            "audit_review_session_id": self.audit_review_session_id,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: object) -> DraftWorkspaceCheckpoint:
        value = _closed(
            data,
            {
                "schema_version",
                "project_id",
                "workflow_run_id",
                "generation",
                "current_candidate_ref",
                "project_revision",
                "ordered_bindings",
                "context_hash",
                "redo_candidate_refs",
                "last_commit",
                "audit_review_session_id",
                "updated_at",
            },
            description="DraftWorkspaceCheckpoint",
        )
        bindings = value["ordered_bindings"]
        redo = value["redo_candidate_refs"]
        if not isinstance(bindings, list) or not isinstance(redo, list):
            raise _error("checkpoint bindings and redo refs must be arrays")
        session_id = value["audit_review_session_id"]
        if session_id is not None and not isinstance(session_id, str):
            raise _error("audit_review_session_id must be a string or null")
        updated_at = value["updated_at"]
        if not isinstance(updated_at, str):
            raise _error("updated_at must be a string")
        return cls(
            schema_version=_integer(
                value["schema_version"], field="schema_version", minimum=1
            ),
            project_id=_safe_id(value["project_id"], field="project_id"),
            workflow_run_id=_safe_id(
                value["workflow_run_id"], field="workflow_run_id"
            ),
            generation=_integer(
                value["generation"], field="generation", minimum=1
            ),
            current_candidate_ref=_artifact_ref(
                value["current_candidate_ref"], field="current_candidate_ref"
            ),
            project_revision=_integer(
                value["project_revision"], field="project_revision", minimum=0
            ),
            ordered_bindings=tuple(
                DraftWorkspaceBinding.from_dict(item) for item in bindings
            ),
            context_hash=_sha256(value["context_hash"], field="context_hash"),
            redo_candidate_refs=tuple(
                _artifact_ref(item, field="redo_candidate_ref") for item in redo
            ),
            last_commit=DraftWorkspaceLastCommit.from_dict(value["last_commit"]),
            audit_review_session_id=session_id,
            updated_at=updated_at,
        )


@dataclass(frozen=True)
class DraftWorkspaceCheckpointRef:
    generation: int
    checkpoint_hash: str

    def __post_init__(self) -> None:
        _integer(self.generation, field="generation", minimum=1)
        _sha256(self.checkpoint_hash, field="checkpoint_hash")

    def to_dict(self) -> dict[str, object]:
        return {
            "generation": self.generation,
            "checkpoint_hash": self.checkpoint_hash,
        }

    @classmethod
    def from_dict(cls, data: object) -> DraftWorkspaceCheckpointRef:
        value = _closed(
            data,
            {"generation", "checkpoint_hash"},
            description="checkpoint ref",
        )
        return cls(
            generation=_integer(value["generation"], field="generation", minimum=1),
            checkpoint_hash=_sha256(
                value["checkpoint_hash"], field="checkpoint_hash"
            ),
        )

    @classmethod
    def for_checkpoint(
        cls, checkpoint: DraftWorkspaceCheckpoint
    ) -> DraftWorkspaceCheckpointRef:
        return cls(checkpoint.generation, checkpoint.checkpoint_hash)


def draft_workspace_input_hash(
    *,
    project_id: str,
    workflow_run_id: str,
    operation_id: str,
    operation: str,
    expected_checkpoint_ref: DraftWorkspaceCheckpointRef | None,
    expected_current_candidate_ref: ArtifactRef | None,
    input_payload: dict[str, object],
) -> str:
    """Hash the frozen closed Review mutation envelope."""

    _safe_id(project_id, field="project_id")
    _safe_id(workflow_run_id, field="workflow_run_id")
    if operation not in _OPERATIONS:
        raise _error("operation is unsupported")
    if not isinstance(operation_id, str) or _OPERATION_ID.fullmatch(operation_id) is None:
        raise _error("operation_id is invalid")
    return canonical_sha256_v1(
        {
            "hash_schema": 1,
            "hash_kind": "draft_workspace_request",
            "project_id": project_id,
            "workflow_run_id": workflow_run_id,
            "operation_id": operation_id,
            "operation": operation,
            "expected_checkpoint_ref": (
                None
                if expected_checkpoint_ref is None
                else expected_checkpoint_ref.to_dict()
            ),
            "expected_current_candidate_ref": (
                None
                if expected_current_candidate_ref is None
                else expected_current_candidate_ref.to_dict()
            ),
            "input": input_payload,
        }
    )
