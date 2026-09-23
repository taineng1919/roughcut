"""Strict filesystem store for finite-workflow schema 1 objects."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeVar

from roughcut.adapters.project_lock import project_write_lock
from roughcut.adapters.project_store import ProjectStore
from roughcut.adapters.workflow_candidates import (
    workflow_candidate_temp_path,
    workflow_export_staging_id,
)
from roughcut.domain.brief import EditBrief
from roughcut.domain.content_draft import ContentDraft
from roughcut.domain.edit import (
    EditDecision,
    EditProposal,
    MultiSourceEditDecision,
    MultiSourceEditProposal,
)
from roughcut.domain.errors import WorkflowError
from roughcut.domain.project import Project, ProjectError
from roughcut.domain.render import parse_render_plan
from roughcut.domain.workflow import (
    ActionReceipt,
    ApprovalRecord,
    OutputRef,
    SubjectRef,
    TransactionMarker,
    WorkflowRun,
    canonical_json_v1,
    canonical_sha256_v1,
    load_closed_json,
    subject_content_hash,
    validate_safe_id,
)

_StoredObject = TypeVar(
    "_StoredObject", WorkflowRun, ApprovalRecord, ActionReceipt, TransactionMarker
)

_RECOVERY_MESSAGE = (
    "Roughcut core restored the previous interrupted workflow action; "
    "reconfirm that action before retrying"
)
_CANDIDATE_PATHS = {
    "brief": "briefs/{artifact_id}.json",
    "content_draft": "content-drafts/{artifact_id}.json",
    "proposal": "proposals/{artifact_id}.json",
    "decision": "edits/{artifact_id}.json",
    "render": "renders/{artifact_id}.plan.json",
    "mp4": "renders/{artifact_id}.mp4",
    "manifest": "renders/{artifact_id}.manifest.json",
}
_ACTION_CANDIDATE_KINDS = {
    "approve_scope": frozenset(),
    "confirm_brief": frozenset({"brief"}),
    "submit_outline": frozenset({"outline"}),
    "approve_outline": frozenset(),
    "submit_draft": frozenset({"content_draft"}),
    "approve_draft": frozenset({"content_draft", "proposal"}),
    "return_to_draft": frozenset(),
    "adopt_roughcut": frozenset({"decision"}),
    "approve_export": frozenset({"render", "mp4", "manifest"}),
    "workflow_cancel": frozenset(),
}
_APPROVAL_ACTIONS = frozenset(
    {
        "approve_scope",
        "confirm_brief",
        "approve_outline",
        "approve_draft",
        "adopt_roughcut",
        "approve_export",
    }
)


def _integrity_error(evidence: str) -> WorkflowError:
    return WorkflowError(
        "workflow_integrity_error",
        f"Roughcut workflow store rejected storage integrity: {evidence}",
    )


def _recovery_conflict(evidence: str) -> WorkflowError:
    return WorkflowError(
        "workflow_recovery_conflict",
        f"Roughcut core/store refused workflow recovery: {evidence}",
    )


def _candidate_identity_conflict(candidate_ref: OutputRef, evidence: str) -> WorkflowError:
    return WorkflowError(
        "workflow_recovery_conflict",
        "Roughcut core/store 拒绝删除内容身份不匹配的候选: "
        f"{candidate_ref.kind}/{candidate_ref.artifact_id}; {evidence}",
    )


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


@dataclass(frozen=True)
class WorkflowRecovery:
    """One deterministic outcome from reconciling a durable transaction marker."""

    action_id: str
    input_hash: str
    disposition: Literal["receipt_committed", "rolled_back"]
    message: str
    receipt: ActionReceipt | None


class WorkflowStore:
    """Project-scoped store; it does not execute workflow actions or business services."""

    def __init__(self, project_path: Path) -> None:
        self.project_path = project_path.resolve()
        self.workflow_path = self.project_path / "workflow"
        self.runs_path = self.workflow_path / "runs"
        self.approvals_path = self.workflow_path / "approvals"
        self.receipts_path = self.workflow_path / "receipts"
        self.transactions_path = self.workflow_path / "transactions"
        self.last_recovery: tuple[WorkflowRecovery, ...] = ()

    @contextmanager
    def write_lock(self) -> Iterator[None]:
        with project_write_lock(self.project_path):
            yield

    def recover_pending(self) -> tuple[WorkflowRecovery, ...]:
        """Reconcile a crashed fixed-action commit before exposing workflow state."""

        if not os.path.lexists(self.workflow_path):
            self.last_recovery = ()
            return ()
        with self.write_lock():
            self.last_recovery = self._recover_pending_locked()
            return self.last_recovery

    def list_runs(self) -> tuple[WorkflowRun, ...]:
        if not os.path.lexists(self.workflow_path):
            self.last_recovery = ()
            return ()
        with self.write_lock():
            self.last_recovery = self._recover_pending_locked()
            project_id = self._project_id()
            return self._list_runs_locked(project_id)

    def active_run(self) -> WorkflowRun | None:
        runs = self.list_runs()
        active = [run for run in runs if run.lifecycle == "active"]
        if len(active) > 1:
            raise WorkflowError(
                "workflow_run_conflict",
                "Roughcut workflow store found multiple active runs for one Project",
            )
        return active[0] if active else None

    def read_run(self, run_id: str) -> WorkflowRun:
        validate_safe_id(run_id, field="run_id")
        with self.write_lock():
            self.last_recovery = self._recover_pending_locked()
            project_id = self._project_id()
            self._validate_existing_tree()
            run = self._read_object(
                self.runs_path / f"{run_id}.json",
                WorkflowRun.from_dict,
                description="WorkflowRun",
            )
            self._validate_run_identity(run, run_id=run_id, project_id=project_id)
            self._validate_run_references_locked(run)
            return run

    def write_run(
        self, run: WorkflowRun, *, expected_run_hash: str | None = None
    ) -> WorkflowRun:
        with self.write_lock():
            run_hash = canonical_sha256_v1(run.to_dict())
            self._recover_before_unrelated_write_locked(
                lambda marker: (
                    marker.run_id == run.run_id
                    and marker.project_id == run.project_id
                    and marker.run_after_hash == run_hash
                )
            )
            project_id = self._project_id()
            if run.project_id != project_id:
                raise _integrity_error("WorkflowRun project_id does not match project.json")
            self._ensure_tree()
            path = self.runs_path / f"{run.run_id}.json"
            existing: WorkflowRun | None = None
            if os.path.lexists(path):
                existing = self._read_object(
                    path, WorkflowRun.from_dict, description="WorkflowRun"
                )
                self._validate_run_identity(existing, run_id=run.run_id, project_id=project_id)
                if existing.lifecycle != "active":
                    raise _integrity_error("completed/canceled WorkflowRun is immutable")
                if expected_run_hash is None:
                    raise WorkflowError(
                        "workflow_action_conflict",
                        "Roughcut workflow store requires the current WorkflowRun hash",
                    )
                if canonical_sha256_v1(existing.to_dict()) != expected_run_hash:
                    raise WorkflowError(
                        "workflow_action_conflict",
                        "Roughcut workflow store detected a WorkflowRun revision conflict",
                    )
            elif expected_run_hash is not None:
                raise WorkflowError(
                    "workflow_action_conflict",
                    "Roughcut workflow store expected an existing WorkflowRun",
                )
            elif run.lifecycle != "active" or run.stage != "scope_review":
                raise _integrity_error(
                    "a new WorkflowRun must start active at scope_review"
                )

            runs = self._list_runs_locked(project_id)
            active_runs = [item for item in runs if item.lifecycle == "active"]
            if len(active_runs) > 1:
                raise WorkflowError(
                    "workflow_run_conflict",
                    "Roughcut workflow store found multiple active runs for one Project",
                )
            other_active = [
                item
                for item in active_runs
                if item.lifecycle == "active" and item.run_id != run.run_id
            ]
            if run.lifecycle == "active" and other_active:
                raise WorkflowError(
                    "workflow_run_conflict",
                    "Roughcut workflow store permits only one active run per Project",
                )
            self._validate_run_references_locked(run)
            self._atomic_write(path, run.to_dict(), immutable=False)
            return run

    def write_approval(self, record: ApprovalRecord) -> ApprovalRecord:
        with self.write_lock():
            self._recover_before_unrelated_write_locked(
                lambda marker: (
                    record.approval_id in marker.approval_ids
                    and record.project_id == marker.project_id
                    and record.run_id == marker.run_id
                    and record.issued_by_action_id == marker.action_id
                    and record.source_action == marker.action
                )
            )
            project_id, active = self._active_identity_locked()
            self._validate_evidence_identity(
                record.project_id,
                record.run_id,
                project_id=project_id,
                active_run=active,
                description="ApprovalRecord",
            )
            self._ensure_tree()
            path = self.approvals_path / f"{record.approval_id}.json"
            self._atomic_write(path, record.to_dict(), immutable=True)
            return record

    def read_approval(self, approval_id: str, *, run_id: str) -> ApprovalRecord:
        validate_safe_id(approval_id, field="approval_id")
        validate_safe_id(run_id, field="run_id")
        with self.write_lock():
            self.last_recovery = self._recover_pending_locked()
            project_id = self._project_id()
            self._validate_existing_tree()
            run = self._read_run_locked(run_id, project_id)
            record = self._read_object(
                self.approvals_path / f"{approval_id}.json",
                ApprovalRecord.from_dict,
                description="ApprovalRecord",
            )
            if record.approval_id != approval_id:
                raise _integrity_error("ApprovalRecord ID does not match its path")
            self._validate_evidence_identity(
                record.project_id,
                record.run_id,
                project_id=project_id,
                active_run=run,
                description="ApprovalRecord",
            )
            return record

    def approval_status(
        self,
        approval_id: str,
        *,
        run_id: str,
        subject: SubjectRef,
        dependency_hash: str,
    ) -> str:
        record = self.read_approval(approval_id, run_id=run_id)
        return record.effective_status(subject, dependency_hash)

    def write_receipt(self, receipt: ActionReceipt) -> ActionReceipt:
        with self.write_lock():
            project = ProjectStore(self.project_path).load()
            project_id = project.project_id
            self._ensure_tree()
            path = self.receipts_path / f"{receipt.action_id}.json"
            marker_path = self.transactions_path / f"{receipt.action_id}.json"
            if os.path.lexists(path):
                if os.path.lexists(marker_path):
                    marker = self._read_object(
                        marker_path,
                        TransactionMarker.from_dict,
                        description="TransactionMarker",
                    )
                    if marker.input_hash != receipt.input_hash:
                        raise WorkflowError(
                            "workflow_action_conflict",
                            "Roughcut workflow store found the same action ID "
                            "with different input",
                        )
                    if (
                        marker.action_id != receipt.action_id
                        or marker.project_id != receipt.project_id
                        or marker.run_id != receipt.run_id
                        or marker.action != receipt.action
                    ):
                        raise _integrity_error(
                            "TransactionMarker identity does not match requested "
                            "ActionReceipt readback"
                        )
                    self.last_recovery = self._recover_pending_locked()
                    if (
                        len(self.last_recovery) != 1
                        or self.last_recovery[0].disposition != "receipt_committed"
                        or self.last_recovery[0].receipt is None
                    ):
                        raise _integrity_error(
                            "receipt reconciliation did not return its committed receipt"
                        )
                    return self.last_recovery[0].receipt
                self._recover_before_unrelated_write_locked(lambda _marker: False)
                existing = self._read_object(
                    path, ActionReceipt.from_dict, description="ActionReceipt"
                )
                self._validate_receipt_identity(
                    existing,
                    action_id=receipt.action_id,
                    project_id=project_id,
                    run_id=receipt.run_id,
                )
                if existing.input_hash != receipt.input_hash:
                    raise WorkflowError(
                        "workflow_action_conflict",
                        "Roughcut workflow store found the same action ID with different input",
                    )
                return existing

            self._recover_before_unrelated_write_locked(
                lambda marker: (
                    receipt.action_id == marker.action_id
                    and receipt.input_hash == marker.input_hash
                    and receipt.project_id == marker.project_id
                    and receipt.run_id == marker.run_id
                    and receipt.action == marker.action
                )
            )
            self._validate_output_refs(receipt)
            run = self._read_run_locked(receipt.run_id, project_id)
            if run.lifecycle == "canceled" and receipt.action != "workflow_cancel":
                raise _integrity_error("canceled WorkflowRun cannot publish a receipt")
            if (
                run.stage != receipt.after.stage
                or run.lifecycle != receipt.after.lifecycle
                or project.revision != receipt.after.project_revision
            ):
                raise _integrity_error(
                    "ActionReceipt after state does not match current Project and WorkflowRun"
                )
            if (
                run.last_receipt_ref is not None
                and run.last_receipt_ref.action_id == receipt.action_id
                and run.last_receipt_ref.receipt_hash
                != canonical_sha256_v1(receipt.to_dict())
            ):
                raise _integrity_error(
                    "ActionReceipt hash does not match pending WorkflowRun receipt ref"
                )
            self._validate_evidence_identity(
                receipt.project_id,
                receipt.run_id,
                project_id=project_id,
                active_run=run,
                description="ActionReceipt",
            )
            self._validate_receipt_approvals_locked(receipt, run)
            if os.path.lexists(marker_path):
                marker = self._read_object(
                    marker_path,
                    TransactionMarker.from_dict,
                    description="TransactionMarker",
                )
                if marker.input_hash != receipt.input_hash:
                    raise WorkflowError(
                        "workflow_action_conflict",
                        "Roughcut workflow store found an action marker with different input",
                    )
                if (
                    marker.action_id != receipt.action_id
                    or marker.project_id != receipt.project_id
                    or marker.run_id != receipt.run_id
                    or marker.action != receipt.action
                ):
                    raise _integrity_error(
                        "TransactionMarker identity does not match final ActionReceipt"
                    )
            self._atomic_write(path, receipt.to_dict(), immutable=True)
            return receipt

    def read_receipt(
        self, action_id: str, *, run_id: str, input_hash: str | None = None
    ) -> ActionReceipt:
        validate_safe_id(action_id, field="action_id")
        validate_safe_id(run_id, field="run_id")
        with self.write_lock():
            self.last_recovery = self._recover_pending_locked()
            project_id = self._project_id()
            self._validate_existing_tree()
            run = self._read_run_locked(run_id, project_id)
            receipt = self._read_object(
                self.receipts_path / f"{action_id}.json",
                ActionReceipt.from_dict,
                description="ActionReceipt",
            )
            self._validate_receipt_identity(
                receipt,
                action_id=action_id,
                project_id=project_id,
                run_id=run.run_id,
            )
            if input_hash is not None and receipt.input_hash != input_hash:
                raise WorkflowError(
                    "workflow_action_conflict",
                    "Roughcut workflow store found the same action ID with different input",
                )
            return receipt

    def write_transaction(self, marker: TransactionMarker) -> TransactionMarker:
        with self.write_lock():
            project = self._load_project_locked()
            project_id = project.project_id
            self._ensure_tree()
            path = self.transactions_path / f"{marker.action_id}.json"
            receipt_path = self.receipts_path / f"{marker.action_id}.json"
            existing: TransactionMarker | None = None
            if os.path.lexists(path):
                existing = self._read_object(
                    path, TransactionMarker.from_dict, description="TransactionMarker"
                )
                run = self._read_run_locked(existing.run_id, project_id)
                if run.lifecycle == "canceled" and existing.action != "workflow_cancel":
                    raise _integrity_error(
                        "canceled WorkflowRun cannot update a transaction marker"
                    )
                self._validate_evidence_identity(
                    existing.project_id,
                    existing.run_id,
                    project_id=project_id,
                    active_run=run,
                    description="TransactionMarker",
                )
                self._validate_evidence_identity(
                    marker.project_id,
                    marker.run_id,
                    project_id=project_id,
                    active_run=run,
                    description="TransactionMarker",
                )
            else:
                other_markers = [
                    item
                    for item in self._transaction_paths_locked()
                    if item != path
                ]
                if other_markers:
                    self.last_recovery = self._recover_pending_locked()
                    if self.last_recovery:
                        raise WorkflowError(
                            "workflow_action_conflict",
                            self.last_recovery[0].message,
                        )
                active_project_id, active = self._active_identity_locked()
                self._validate_evidence_identity(
                    marker.project_id,
                    marker.run_id,
                    project_id=active_project_id,
                    active_run=active,
                    description="TransactionMarker",
                )
                if (
                    canonical_sha256_v1(project.to_dict())
                    != marker.project_before_hash
                    or canonical_sha256_v1(active.to_dict()) != marker.run_before_hash
                    or project != marker.project_before
                    or active != marker.run_before
                ):
                    raise _integrity_error(
                        "TransactionMarker before images do not match current Project and run"
                    )
            self._validate_marker_ownership_locked(
                marker,
                require_absent=existing is None,
            )
            if os.path.lexists(receipt_path):
                receipt = self._read_object(
                    receipt_path, ActionReceipt.from_dict, description="ActionReceipt"
                )
                if (
                    receipt.action_id != marker.action_id
                    or receipt.project_id != marker.project_id
                    or receipt.run_id != marker.run_id
                    or receipt.action != marker.action
                ):
                    raise _integrity_error(
                        "final ActionReceipt identity does not match TransactionMarker"
                    )
                if receipt.input_hash != marker.input_hash:
                    raise WorkflowError(
                        "workflow_action_conflict",
                        "Roughcut workflow store found a final receipt with different input",
                    )
                raise _integrity_error("completed action cannot create a transaction marker")
            if existing is not None:
                if existing.input_hash != marker.input_hash:
                    raise WorkflowError(
                        "workflow_action_conflict",
                        "Roughcut workflow store found an action marker with different input",
                    )
                self._validate_transaction_update(existing, marker)
                if existing == marker:
                    return existing
            self._atomic_write(path, marker.to_dict(), immutable=False)
            return marker

    def read_transaction(self, action_id: str, *, run_id: str) -> TransactionMarker:
        validate_safe_id(action_id, field="action_id")
        validate_safe_id(run_id, field="run_id")
        with self.write_lock():
            project_id = self._project_id()
            self._validate_existing_tree()
            run = self._read_run_locked(run_id, project_id)
            marker = self._read_object(
                self.transactions_path / f"{action_id}.json",
                TransactionMarker.from_dict,
                description="TransactionMarker",
            )
            if marker.action_id != action_id:
                raise _integrity_error("TransactionMarker ID does not match its path")
            self._validate_evidence_identity(
                marker.project_id,
                marker.run_id,
                project_id=project_id,
                active_run=run,
                description="TransactionMarker",
            )
            return marker

    def delete_transaction(self, action_id: str, *, input_hash: str) -> None:
        validate_safe_id(action_id, field="action_id")
        with self.write_lock():
            project_id = self._project_id()
            self._validate_existing_tree()
            path = self.transactions_path / f"{action_id}.json"
            marker = self._read_object(
                path,
                TransactionMarker.from_dict,
                description="TransactionMarker",
            )
            if marker.action_id != action_id or marker.input_hash != input_hash:
                raise WorkflowError(
                    "workflow_action_conflict",
                    "Roughcut workflow store refused to delete a different action marker",
                )
            run = self._read_run_locked(marker.run_id, project_id)
            self._validate_evidence_identity(
                marker.project_id,
                marker.run_id,
                project_id=project_id,
                active_run=run,
                description="TransactionMarker",
            )
            receipt_path = self.receipts_path / f"{action_id}.json"
            if not os.path.lexists(receipt_path):
                raise _integrity_error(
                    "Roughcut core/store refused to delete TransactionMarker "
                    "before matching ActionReceipt publication"
                )
            self.last_recovery = self._recover_pending_locked()
            if (
                len(self.last_recovery) != 1
                or self.last_recovery[0].action_id != action_id
                or self.last_recovery[0].input_hash != input_hash
                or self.last_recovery[0].disposition != "receipt_committed"
            ):
                raise _integrity_error(
                    "TransactionMarker cleanup did not reconcile its committed receipt"
                )

    def _recover_before_unrelated_write_locked(
        self,
        participant_matches: Callable[[TransactionMarker], bool],
    ) -> None:
        if not os.path.lexists(self.transactions_path):
            return
        marker_paths = self._transaction_paths_locked()
        if not marker_paths:
            return
        if len(marker_paths) != 1:
            raise _recovery_conflict(
                "multiple transaction markers exist; the workflow write was rejected"
            )
        marker = self._read_object(
            marker_paths[0],
            TransactionMarker.from_dict,
            description="TransactionMarker",
        )
        if marker.action_id != marker_paths[0].stem:
            raise _integrity_error("TransactionMarker ID does not match its path")
        if participant_matches(marker):
            return
        self.last_recovery = self._recover_pending_locked()
        if self.last_recovery:
            raise WorkflowError(
                "workflow_action_conflict",
                self.last_recovery[0].message,
            )

    def _recover_pending_locked(self) -> tuple[WorkflowRecovery, ...]:
        self._validate_existing_tree()
        marker_paths = self._transaction_paths_locked()
        if not marker_paths:
            return ()
        if len(marker_paths) != 1:
            raise _recovery_conflict(
                "multiple transaction markers exist; no Project or run file was overwritten"
            )
        marker_path = marker_paths[0]
        marker = self._read_object(
            marker_path,
            TransactionMarker.from_dict,
            description="TransactionMarker",
        )
        if marker.action_id != marker_path.stem:
            raise _integrity_error("TransactionMarker ID does not match its path")
        project = self._load_project_locked()
        if marker.project_id != project.project_id:
            raise _integrity_error("TransactionMarker was copied from another Project")
        if (
            marker.project_before.project_id != project.project_id
            or marker.run_before.project_id != project.project_id
            or marker.run_before.run_id != marker.run_id
        ):
            raise _integrity_error(
                "TransactionMarker before images do not belong to its Project and run"
            )
        self._validate_marker_ownership_locked(marker, require_absent=False)

        receipt_path = self.receipts_path / f"{marker.action_id}.json"
        if os.path.lexists(receipt_path):
            receipt = self._read_object(
                receipt_path,
                ActionReceipt.from_dict,
                description="ActionReceipt",
            )
            if (
                receipt.action_id != marker.action_id
                or receipt.input_hash != marker.input_hash
                or receipt.project_id != marker.project_id
                or receipt.run_id != marker.run_id
                or receipt.action != marker.action
            ):
                raise _integrity_error(
                    "ActionReceipt identity, action, or input does not match "
                    "TransactionMarker"
                )
            committed_run = self._read_run_locked(marker.run_id, project.project_id)
            if (
                canonical_sha256_v1(project.to_dict()) != marker.project_after_hash
                or canonical_sha256_v1(committed_run.to_dict())
                != marker.run_after_hash
            ):
                raise _recovery_conflict(
                    "receipt exists but current Project/WorkflowRun is not the "
                    "marker after state; no marker was removed"
                )
            self._validate_receipt_approvals_locked(receipt, committed_run)
            try:
                self._cleanup_export_staging_locked(marker)
                self._delete_marker_locked(marker_path)
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as error:
                if isinstance(error, WorkflowError):
                    raise
                raise _integrity_error(
                    f"receipt recovery could not clean its marker: {error}"
                ) from error
            return (
                WorkflowRecovery(
                    action_id=marker.action_id,
                    input_hash=marker.input_hash,
                    disposition="receipt_committed",
                    message=(
                        "Roughcut core found the previous workflow action receipt; "
                        "the action succeeded and its receipt must be read back"
                    ),
                    receipt=receipt,
                ),
            )

        current_run = self._read_run_image_locked(marker.run_id, project.project_id)
        project_hash = canonical_sha256_v1(project.to_dict())
        run_hash = canonical_sha256_v1(current_run.to_dict())
        if project_hash not in {
            marker.project_before_hash,
            marker.project_after_hash,
        }:
            raise _recovery_conflict(
                "current Project hash is neither the marker before nor after hash; "
                "no file was overwritten"
            )
        if run_hash not in {marker.run_before_hash, marker.run_after_hash}:
            raise _recovery_conflict(
                "current WorkflowRun hash is neither the marker before nor after hash; "
                "no file was overwritten"
            )
        for candidate_ref in marker.candidate_refs:
            self._validate_owned_candidate_content_locked(marker, candidate_ref)

        try:
            if project_hash != marker.project_before_hash:
                ProjectStore(self.project_path).save(
                    marker.project_before,
                    expected_revision=project.revision,
                )
                restored_project = self._load_project_locked()
                if (
                    canonical_sha256_v1(restored_project.to_dict())
                    != marker.project_before_hash
                ):
                    raise _integrity_error(
                        "Project recovery did not publish the exact marker before image"
                    )
            if run_hash != marker.run_before_hash:
                run_path = self.runs_path / f"{marker.run_id}.json"
                self._atomic_write(
                    run_path,
                    marker.run_before.to_dict(),
                    immutable=False,
                )
                restored_run = self._read_run_image_locked(
                    marker.run_id,
                    marker.project_id,
                )
                if (
                    canonical_sha256_v1(restored_run.to_dict())
                    != marker.run_before_hash
                ):
                    raise _integrity_error(
                        "WorkflowRun recovery did not publish the exact marker before image"
                    )

            for candidate_ref in marker.candidate_refs:
                self._delete_owned_candidate_locked(marker, candidate_ref)
            for approval_id in marker.approval_ids:
                self._delete_owned_approval_locked(marker, approval_id)
            self._cleanup_export_staging_locked(marker)
            self._delete_marker_locked(marker_path)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as error:
            if isinstance(error, WorkflowError):
                raise
            raise _integrity_error(
                f"workflow recovery failed before marker cleanup: {error}"
            ) from error
        return (
            WorkflowRecovery(
                action_id=marker.action_id,
                input_hash=marker.input_hash,
                disposition="rolled_back",
                message=_RECOVERY_MESSAGE,
                receipt=None,
            ),
        )

    def _transaction_paths_locked(self) -> tuple[Path, ...]:
        if not os.path.lexists(self.transactions_path):
            return ()
        self._validate_directory(self.transactions_path)
        try:
            entries = sorted(self.transactions_path.iterdir(), key=lambda path: path.name)
        except OSError as error:
            raise _integrity_error(f"cannot enumerate transaction markers: {error}") from error
        paths: list[Path] = []
        for path in entries:
            if path.name.startswith(".") and path.name.endswith(".tmp"):
                raise _integrity_error(
                    f"orphan temporary transaction marker exists: {path.name}"
                )
            if path.suffix != ".json":
                raise _integrity_error(
                    f"unexpected transaction directory entry: {path.name}"
                )
            try:
                validate_safe_id(path.stem, field="transaction path ID")
            except WorkflowError as error:
                raise _integrity_error(
                    f"transaction marker path ID is invalid: {path.name}"
                ) from error
            self._validate_regular_file(path)
            paths.append(path)
        return tuple(paths)

    def _load_project_locked(self) -> Project:
        manifest = self.project_path / "project.json"
        self._validate_regular_file(manifest)
        try:
            return ProjectStore(self.project_path).load()
        except Exception as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise _integrity_error(f"project.json is invalid: {error}") from error

    def _read_run_image_locked(self, run_id: str, project_id: str) -> WorkflowRun:
        run = self._read_object(
            self.runs_path / f"{run_id}.json",
            WorkflowRun.from_dict,
            description="WorkflowRun",
        )
        self._validate_run_identity(run, run_id=run_id, project_id=project_id)
        return run

    def _validate_marker_ownership_locked(
        self,
        marker: TransactionMarker,
        *,
        require_absent: bool,
    ) -> None:
        allowed_kinds = _ACTION_CANDIDATE_KINDS[marker.action]
        owned_paths: set[Path] = set()
        for candidate_ref in marker.candidate_refs:
            if candidate_ref.kind not in allowed_kinds:
                raise _integrity_error(
                    "TransactionMarker candidate kind is not owned by its fixed action"
                )
            candidate_path = self._owned_candidate_path(marker, candidate_ref)
            if candidate_path is None:
                continue
            if candidate_path in owned_paths:
                raise _integrity_error(
                    "TransactionMarker candidate refs resolve to the same owned path"
                )
            owned_paths.add(candidate_path)
            temp_path = workflow_candidate_temp_path(
                candidate_path, marker.action_id
            )
            self._validate_contained(temp_path)
            if require_absent and os.path.lexists(candidate_path):
                raise _integrity_error(
                    "TransactionMarker candidate existed before marker publication"
                )
            if require_absent and os.path.lexists(temp_path):
                raise _integrity_error(
                    "TransactionMarker candidate temporary node existed before "
                    "marker publication"
                )
            if not require_absent:
                self._normalize_owned_candidate_publication_locked(
                    marker, candidate_ref, candidate_path, temp_path
                )
            self._validate_owned_candidate_components(candidate_path)

        expected_approval_count = 1 if marker.action in _APPROVAL_ACTIONS else 0
        if len(marker.approval_ids) != expected_approval_count:
            raise _integrity_error(
                "TransactionMarker approval IDs do not match its fixed action"
            )
        for approval_id in marker.approval_ids:
            approval_path = self.approvals_path / f"{approval_id}.json"
            self._validate_contained(approval_path)
            if require_absent and os.path.lexists(approval_path):
                raise _integrity_error(
                    "TransactionMarker approval existed before marker publication"
                )
            if not require_absent and os.path.lexists(approval_path):
                self._validate_owned_approval_record_locked(
                    marker,
                    approval_id,
                    approval_path,
                )

    def _owned_candidate_path(
        self,
        marker: TransactionMarker,
        candidate_ref: OutputRef,
    ) -> Path | None:
        kind = candidate_ref.kind
        if kind == "outline":
            if candidate_ref.project_relative_path is not None:
                raise _integrity_error(
                    "pathless outline candidate supplied a filesystem path"
                )
            return None
        template = _CANDIDATE_PATHS.get(kind)
        if template is None:
            raise _integrity_error(
                "TransactionMarker candidate kind has no controlled storage path"
            )
        relative_path = template.format(artifact_id=candidate_ref.artifact_id)
        if candidate_ref.project_relative_path not in {None, relative_path}:
            raise _integrity_error(
                "TransactionMarker candidate path does not match its controlled ID"
            )
        path = self.project_path.joinpath(*relative_path.split("/"))
        self._validate_contained(path)
        return path

    def _validate_owned_candidate_components(self, path: Path) -> None:
        current = self.project_path
        for index, part in enumerate(path.relative_to(self.project_path).parts):
            current = current / part
            if not os.path.lexists(current):
                return
            try:
                current_stat = os.lstat(current)
            except OSError as error:
                raise _integrity_error(
                    f"cannot inspect owned candidate path {path.name}: {error}"
                ) from error
            if stat.S_ISLNK(current_stat.st_mode):
                raise _integrity_error(
                    f"owned candidate path contains a symlink: {path.name}"
                )
            if index < len(path.relative_to(self.project_path).parts) - 1:
                if not stat.S_ISDIR(current_stat.st_mode):
                    raise _integrity_error(
                        f"owned candidate parent is not a directory: {path.name}"
                    )
            elif not stat.S_ISREG(current_stat.st_mode) or current_stat.st_nlink != 1:
                raise _integrity_error(
                    f"owned candidate is not a single-link regular file: {path.name}"
                )

    def _normalize_owned_candidate_publication_locked(
        self,
        marker: TransactionMarker,
        candidate_ref: OutputRef,
        candidate_path: Path,
        temp_path: Path,
    ) -> None:
        staged_path: Path | None = None
        if marker.action == "approve_export" and candidate_ref.kind == "mp4":
            staging = self._validated_export_staging_locked(marker)
            if staging is not None:
                staging_id = staging.name
                staged_path = staging / f"{staging_id}.mp4"

        if os.path.lexists(temp_path):
            temp_stat = self._validate_owned_publication_file(temp_path)
            if os.path.lexists(candidate_path):
                final_stat = self._validate_owned_publication_file(candidate_path)
                if (temp_stat.st_dev, temp_stat.st_ino) != (
                    final_stat.st_dev,
                    final_stat.st_ino,
                ):
                    raise _recovery_conflict(
                        "candidate final and owned temporary node have different "
                        "content identities"
                    )
            if staged_path is not None and os.path.lexists(staged_path):
                staged_stat = self._validate_owned_publication_file(staged_path)
                if (temp_stat.st_dev, temp_stat.st_ino) != (
                    staged_stat.st_dev,
                    staged_stat.st_ino,
                ):
                    raise _recovery_conflict(
                        "staged MP4 and owned candidate temporary node differ"
                    )
                staged_path.unlink()
                self._sync_directory(staged_path.parent)
            temp_path.unlink()
            self._sync_directory(temp_path.parent)

        if (
            staged_path is not None
            and os.path.lexists(staged_path)
            and os.path.lexists(candidate_path)
        ):
            staged_stat = self._validate_owned_publication_file(staged_path)
            final_stat = self._validate_owned_publication_file(candidate_path)
            if (staged_stat.st_dev, staged_stat.st_ino) != (
                final_stat.st_dev,
                final_stat.st_ino,
            ):
                raise _recovery_conflict(
                    "staged MP4 and formal candidate have different identities"
                )
            staged_path.unlink()
            self._sync_directory(staged_path.parent)

    def _validate_owned_publication_file(self, path: Path) -> os.stat_result:
        self._validate_contained(path)
        details = os.lstat(path)
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise _recovery_conflict(
                f"owned candidate publication node is unsafe: {path.name}"
            )
        return details

    def _validated_export_staging_locked(
        self, marker: TransactionMarker
    ) -> Path | None:
        if marker.action != "approve_export":
            return None
        staging_id = workflow_export_staging_id(
            marker.project_id,
            marker.run_id,
            marker.action_id,
            marker.input_hash,
        )
        directory = self.workflow_path / "export-staging" / staging_id
        self._validate_contained(directory)
        if not os.path.lexists(directory):
            return None
        directory_stat = os.lstat(directory)
        if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(
            directory_stat.st_mode
        ):
            raise _recovery_conflict("export staging is not a safe owned directory")
        owner_path = directory / "owner.json"
        try:
            self._validate_regular_file(owner_path)
            owner = self._read_candidate_json(owner_path)
        except Exception as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise _recovery_conflict(
                f"export staging owner is invalid: {error}"
            ) from error
        fields = {
            "schema_version",
            "project_id",
            "run_id",
            "action_id",
            "input_hash",
            "export_basis_id",
            "staging_id",
            "writing_kind",
            "completed_files",
            "created_at",
        }
        completed = owner.get("completed_files")
        basis_id = owner.get("export_basis_id")
        if (
            set(owner) != fields
            or owner.get("schema_version") != 1
            or owner.get("project_id") != marker.project_id
            or owner.get("run_id") != marker.run_id
            or owner.get("action_id") != marker.action_id
            or owner.get("input_hash") != marker.input_hash
            or owner.get("staging_id") != staging_id
            or owner.get("writing_kind") is not None
            or not isinstance(owner.get("created_at"), str)
            or not isinstance(basis_id, str)
            or not basis_id.startswith("wfb_export_")
            or len(basis_id) != len("wfb_export_") + 64
            or any(
                character not in "0123456789abcdef"
                for character in basis_id.removeprefix("wfb_export_")
            )
            or not isinstance(completed, list)
            or len(completed) != 3
        ):
            raise _recovery_conflict(
                "export staging owner identity or completed prefix is invalid"
            )
        expected = (
            ("plan", f"{staging_id}.plan.json"),
            ("mp4", f"{staging_id}.mp4"),
            ("manifest", f"{staging_id}.manifest.json"),
        )
        for entry, (kind, name) in zip(completed, expected, strict=True):
            if (
                not isinstance(entry, dict)
                or set(entry) != {"kind", "relative_name", "content_hash"}
                or entry.get("kind") != kind
                or entry.get("relative_name") != name
                or not isinstance(entry.get("content_hash"), str)
                or len(entry["content_hash"]) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in entry["content_hash"]
                )
            ):
                raise _recovery_conflict(
                    "export staging completed file identity is invalid"
                )
        fixed_names = {
            "owner.json",
            ".owner.json.tmp",
            *(entry["relative_name"] for entry in completed),
        }
        for child in directory.iterdir():
            if child.name not in fixed_names:
                raise _recovery_conflict(
                    f"export staging contains an unknown node: {child.name}"
                )
            self._validate_owned_publication_file(child)
        return directory

    def _cleanup_export_staging_locked(self, marker: TransactionMarker) -> None:
        directory = self._validated_export_staging_locked(marker)
        if directory is None:
            return
        refs = {candidate.kind: candidate for candidate in marker.candidate_refs}
        staging_id = directory.name
        paths = {
            "render": directory / f"{staging_id}.plan.json",
            "mp4": directory / f"{staging_id}.mp4",
            "manifest": directory / f"{staging_id}.manifest.json",
        }
        for kind, path in paths.items():
            if not os.path.lexists(path):
                continue
            candidate_ref = refs[kind]
            actual_hash = self._owned_candidate_content_hash(
                marker, candidate_ref, path
            )
            if actual_hash != candidate_ref.content_hash:
                raise _candidate_identity_conflict(
                    candidate_ref,
                    "export staging content hash does not match its marker ref",
                )
        for child in tuple(directory.iterdir()):
            child.unlink()
        directory.rmdir()
        self._sync_directory(directory.parent)

    def _validate_owned_candidate_content_locked(
        self,
        marker: TransactionMarker,
        candidate_ref: OutputRef,
    ) -> None:
        path = self._owned_candidate_path(marker, candidate_ref)
        if path is None:
            return
        self._validate_owned_candidate_components(path)
        if not os.path.lexists(path):
            return
        try:
            actual_hash = self._owned_candidate_content_hash(
                marker,
                candidate_ref,
                path,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as error:
            if isinstance(error, WorkflowError):
                raise
            raise _candidate_identity_conflict(
                candidate_ref,
                f"candidate could not be validated: {error}",
            ) from error
        if actual_hash != candidate_ref.content_hash:
            raise _candidate_identity_conflict(
                candidate_ref,
                "stored content hash does not match OutputRef.content_hash",
            )

    def _owned_candidate_content_hash(
        self,
        marker: TransactionMarker,
        candidate_ref: OutputRef,
        path: Path,
    ) -> str:
        if candidate_ref.kind == "mp4":
            digest = hashlib.sha256()
            with path.open("rb") as candidate_file:
                for chunk in iter(lambda: candidate_file.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest()

        payload = self._read_candidate_json(path)
        if candidate_ref.kind == "brief":
            brief = EditBrief.from_dict(payload)
            canonical = brief.to_dict()
            self._validate_exact_artifact_payload(payload, canonical)
            if (
                brief.brief_id != candidate_ref.artifact_id
                or brief.schema_version != candidate_ref.schema_version
            ):
                raise ProjectError("Brief identity does not match its candidate ref")
            return subject_content_hash("brief", brief.schema_version, canonical)
        if candidate_ref.kind == "content_draft":
            draft = ContentDraft.from_dict(payload)
            canonical = draft.to_dict()
            self._validate_exact_artifact_payload(payload, canonical)
            if (
                draft.content_draft_id != candidate_ref.artifact_id
                or draft.schema_version != candidate_ref.schema_version
            ):
                raise ProjectError(
                    "Content Draft identity does not match its candidate ref"
                )
            return subject_content_hash(
                "content_draft",
                draft.schema_version,
                canonical,
            )
        if candidate_ref.kind == "proposal":
            proposal_schema = payload.get("schema_version")
            if proposal_schema == 1:
                proposal: EditProposal | MultiSourceEditProposal = EditProposal.from_dict(
                    payload
                )
            elif proposal_schema == 2:
                proposal = MultiSourceEditProposal.from_dict(payload)
            else:
                raise ProjectError("unsupported Proposal schema version")
            canonical = proposal.to_dict()
            self._validate_exact_artifact_payload(payload, canonical)
            if (
                proposal.proposal_id != candidate_ref.artifact_id
                or proposal.schema_version != candidate_ref.schema_version
            ):
                raise ProjectError("Proposal identity does not match its candidate ref")
            projection = dict(canonical)
            projection.pop("created_at")
            return subject_content_hash(
                "proposal",
                proposal.schema_version,
                projection,
            )
        if candidate_ref.kind == "decision":
            decision_schema = payload.get("schema_version")
            if decision_schema == 1:
                decision: EditDecision | MultiSourceEditDecision = EditDecision.from_dict(
                    payload
                )
            elif decision_schema == 2:
                decision = MultiSourceEditDecision.from_dict(payload)
            else:
                raise ProjectError("unsupported Decision schema version")
            canonical = decision.to_dict()
            self._validate_exact_artifact_payload(payload, canonical)
            if (
                decision.edit_version_id != candidate_ref.artifact_id
                or decision.schema_version != candidate_ref.schema_version
            ):
                raise ProjectError("Decision identity does not match its candidate ref")
            projection = dict(canonical)
            projection.pop("created_at")
            return subject_content_hash(
                "decision",
                decision.schema_version,
                projection,
            )
        if candidate_ref.kind == "render":
            plan = parse_render_plan(payload)
            canonical = plan.to_dict()
            self._validate_exact_artifact_payload(payload, canonical)
            if (
                plan.render_id != candidate_ref.artifact_id
                or plan.project_id != marker.project_id
                or plan.schema_version != candidate_ref.schema_version
            ):
                raise ProjectError("Render Plan identity does not match its candidate ref")
            return canonical_sha256_v1(canonical)
        if candidate_ref.kind == "manifest":
            projection = self._render_manifest_projection(
                marker,
                candidate_ref,
                payload,
            )
            return subject_content_hash(
                "render_manifest",
                candidate_ref.schema_version,
                projection,
            )
        raise ProjectError("candidate kind has no fixed content hash rule")

    def _read_candidate_json(self, path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(
                path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise ProjectError("candidate JSON is missing or invalid") from error
        if not isinstance(payload, dict):
            raise ProjectError("candidate JSON must contain an object")
        return payload

    def _validate_exact_artifact_payload(
        self,
        payload: dict[str, Any],
        canonical: dict[str, object],
    ) -> None:
        if payload != canonical:
            raise ProjectError("candidate JSON does not match its closed artifact schema")

    def _render_manifest_projection(
        self,
        marker: TransactionMarker,
        candidate_ref: OutputRef,
        payload: dict[str, Any],
    ) -> dict[str, object]:
        common_fields = {
            "schema_version",
            "render_id",
            "project_id",
            "project_revision",
            "edit_version_id",
            "tools",
            "output_settings",
            "clips",
            "total_duration_ticks",
            "render_schedule",
            "command_summary",
            "performance",
            "output",
            "acceptance",
        }
        if candidate_ref.schema_version == 2:
            expected_fields = common_fields | {"input_source"}
            ordered_inputs = {"input_source": payload.get("input_source")}
        elif candidate_ref.schema_version == 3:
            expected_fields = common_fields | {"source_bindings", "input_sources"}
            ordered_inputs = {
                "source_bindings": payload.get("source_bindings"),
                "input_sources": payload.get("input_sources"),
            }
        else:
            raise ProjectError("unsupported Render manifest schema version")
        if set(payload) != expected_fields:
            raise ProjectError("Render manifest fields do not match its closed schema")
        if (
            payload.get("schema_version") != candidate_ref.schema_version
            or payload.get("render_id") != candidate_ref.artifact_id
            or payload.get("project_id") != marker.project_id
        ):
            raise ProjectError("Render manifest identity does not match its candidate ref")
        output = payload.get("output")
        if not isinstance(output, dict) or set(output) != {"mp4_path", "probe"}:
            raise ProjectError("Render manifest output is invalid")
        expected_output_path = f"renders/{candidate_ref.artifact_id}.mp4"
        if output.get("mp4_path") != expected_output_path:
            raise ProjectError("Render manifest output path does not match its identity")
        return {
            "render_id": payload["render_id"],
            "edit_version_id": payload["edit_version_id"],
            **ordered_inputs,
            "clips": payload["clips"],
            "output_settings": payload["output_settings"],
            "output": {"mp4_path": output["mp4_path"]},
            "acceptance": payload["acceptance"],
        }

    def _delete_owned_candidate_locked(
        self,
        marker: TransactionMarker,
        candidate_ref: OutputRef,
    ) -> None:
        path = self._owned_candidate_path(marker, candidate_ref)
        if path is None:
            return
        self._validate_owned_candidate_content_locked(marker, candidate_ref)
        if not os.path.lexists(path):
            return
        path.unlink()
        self._sync_directory(path.parent)

    def _delete_owned_approval_locked(
        self,
        marker: TransactionMarker,
        approval_id: str,
    ) -> None:
        path = self.approvals_path / f"{approval_id}.json"
        if not os.path.lexists(path):
            return
        self._validate_owned_approval_record_locked(marker, approval_id, path)
        path.unlink()
        self._sync_directory(self.approvals_path)

    def _validate_owned_approval_record_locked(
        self,
        marker: TransactionMarker,
        approval_id: str,
        path: Path,
    ) -> None:
        record = self._read_object(
            path,
            ApprovalRecord.from_dict,
            description="ApprovalRecord",
        )
        if (
            record.approval_id != approval_id
            or record.project_id != marker.project_id
            or record.run_id != marker.run_id
            or record.issued_by_action_id != marker.action_id
            or record.source_action != marker.action
        ):
            raise _integrity_error(
                "owned ApprovalRecord identity does not match TransactionMarker"
            )

    def _delete_marker_locked(self, path: Path) -> None:
        self._validate_regular_file(path)
        path.unlink()
        self._sync_directory(self.transactions_path)

    def _project_id(self) -> str:
        return self._load_project_locked().project_id

    def _active_identity_locked(self) -> tuple[str, WorkflowRun]:
        project_id = self._project_id()
        runs = self._list_runs_locked(project_id)
        active = [run for run in runs if run.lifecycle == "active"]
        if not active:
            raise WorkflowError(
                "workflow_required",
                "Roughcut workflow store requires an active WorkflowRun",
            )
        if len(active) > 1:
            raise WorkflowError(
                "workflow_run_conflict",
                "Roughcut workflow store found multiple active runs for one Project",
            )
        return project_id, active[0]

    def _read_run_locked(self, run_id: str, project_id: str) -> WorkflowRun:
        run = self._read_object(
            self.runs_path / f"{run_id}.json", WorkflowRun.from_dict, description="WorkflowRun"
        )
        self._validate_run_identity(run, run_id=run_id, project_id=project_id)
        self._validate_run_references_locked(run)
        return run

    def _list_runs_locked(self, project_id: str) -> tuple[WorkflowRun, ...]:
        if not os.path.lexists(self.runs_path):
            if os.path.lexists(self.workflow_path):
                self._validate_existing_tree()
            return ()
        self._validate_existing_tree()
        runs: list[WorkflowRun] = []
        try:
            entries = sorted(self.runs_path.iterdir(), key=lambda path: path.name)
        except OSError as error:
            raise _integrity_error(f"cannot enumerate runs: {error}") from error
        for path in entries:
            if path.name.startswith(".") and path.name.endswith(".tmp"):
                raise _integrity_error(f"orphan temporary run file exists: {path.name}")
            if path.suffix != ".json":
                raise _integrity_error(f"unexpected run directory entry: {path.name}")
            run_id = path.stem
            validate_safe_id(run_id, field="run path ID")
            run = self._read_object(path, WorkflowRun.from_dict, description="WorkflowRun")
            self._validate_run_identity(run, run_id=run_id, project_id=project_id)
            self._validate_run_references_locked(run)
            runs.append(run)
        return tuple(runs)

    def _validate_run_identity(
        self, run: WorkflowRun, *, run_id: str, project_id: str
    ) -> None:
        if run.run_id != run_id:
            raise _integrity_error("WorkflowRun ID does not match its path")
        if run.project_id != project_id:
            raise _integrity_error("WorkflowRun was copied from another Project")

    def _validate_receipt_identity(
        self,
        receipt: ActionReceipt,
        *,
        action_id: str,
        project_id: str,
        run_id: str,
    ) -> None:
        if receipt.action_id != action_id:
            raise _integrity_error("ActionReceipt ID does not match its path")
        self._validate_evidence_identity(
            receipt.project_id,
            receipt.run_id,
            project_id=project_id,
            active_run=self._read_run_locked(run_id, project_id),
            description="ActionReceipt",
        )

    def _validate_receipt_approvals_locked(
        self, receipt: ActionReceipt, run: WorkflowRun
    ) -> None:
        for approval_id in receipt.approval_ids:
            record = self._read_object(
                self.approvals_path / f"{approval_id}.json",
                ApprovalRecord.from_dict,
                description="ApprovalRecord",
            )
            if (
                record.approval_id != approval_id
                or record.project_id != receipt.project_id
                or record.run_id != receipt.run_id
                or record.issued_by_action_id != receipt.action_id
                or record.source_action != receipt.action
            ):
                raise _integrity_error(
                    "ActionReceipt approval identity does not match its action"
                )
            run_ref = run.approval_refs[record.gate]
            if (
                run_ref is None
                or run_ref.approval_id != approval_id
                or run_ref.record_hash != canonical_sha256_v1(record.to_dict())
            ):
                raise _integrity_error(
                    "ActionReceipt approval is not the current WorkflowRun approval"
                )

    def _validate_transaction_update(
        self, existing: TransactionMarker, updated: TransactionMarker
    ) -> None:
        existing_payload = existing.to_dict()
        updated_payload = updated.to_dict()
        existing_step = existing_payload.pop("commit_step")
        updated_step = updated_payload.pop("commit_step")
        if existing_payload != updated_payload:
            raise _integrity_error(
                "TransactionMarker immutable fields changed during commit"
            )
        steps = (
            "prepared",
            "candidates_published",
            "project_published",
            "run_published",
        )
        if steps.index(str(updated_step)) < steps.index(str(existing_step)):
            raise _integrity_error("TransactionMarker commit_step cannot move backward")

    def _validate_run_references_locked(self, run: WorkflowRun) -> None:
        for approval_ref in run.approval_refs.values():
            if approval_ref is None:
                continue
            record = self._read_object(
                self.approvals_path / f"{approval_ref.approval_id}.json",
                ApprovalRecord.from_dict,
                description="ApprovalRecord",
            )
            if (
                record.approval_id != approval_ref.approval_id
                or record.project_id != run.project_id
                or record.run_id != run.run_id
            ):
                raise _integrity_error("WorkflowRun approval ref identity does not match evidence")
            if canonical_sha256_v1(record.to_dict()) != approval_ref.record_hash:
                raise _integrity_error("WorkflowRun approval ref hash does not match evidence")
        receipt_ref = run.last_receipt_ref
        if receipt_ref is None:
            return
        receipt_path = self.receipts_path / f"{receipt_ref.action_id}.json"
        if not os.path.lexists(receipt_path):
            marker = self._read_object(
                self.transactions_path / f"{receipt_ref.action_id}.json",
                TransactionMarker.from_dict,
                description="TransactionMarker",
            )
            if (
                marker.action_id != receipt_ref.action_id
                or marker.project_id != run.project_id
                or marker.run_id != run.run_id
                or marker.run_after_hash != canonical_sha256_v1(run.to_dict())
            ):
                raise _integrity_error(
                    "pending WorkflowRun receipt ref does not match its transaction"
                )
            return
        receipt = self._read_object(
            receipt_path,
            ActionReceipt.from_dict,
            description="ActionReceipt",
        )
        if (
            receipt.action_id != receipt_ref.action_id
            or receipt.project_id != run.project_id
            or receipt.run_id != run.run_id
        ):
            raise _integrity_error("WorkflowRun receipt ref identity does not match evidence")
        if canonical_sha256_v1(receipt.to_dict()) != receipt_ref.receipt_hash:
            raise _integrity_error("WorkflowRun receipt ref hash does not match evidence")

    @staticmethod
    def _validate_evidence_identity(
        object_project_id: str,
        object_run_id: str,
        *,
        project_id: str,
        active_run: WorkflowRun,
        description: str,
    ) -> None:
        if object_project_id != project_id:
            raise _integrity_error(f"{description} was copied from another Project")
        if object_run_id != active_run.run_id:
            raise _integrity_error(f"{description} does not belong to the selected run")

    def _ensure_tree(self) -> None:
        self._validate_contained(self.workflow_path)
        for directory in (
            self.workflow_path,
            self.runs_path,
            self.approvals_path,
            self.receipts_path,
            self.transactions_path,
        ):
            if os.path.lexists(directory):
                self._validate_directory(directory)
            else:
                try:
                    directory.mkdir()
                    self._sync_directory(directory.parent)
                except OSError as error:
                    raise _integrity_error(
                        f"cannot create workflow directory {directory.name}: {error}"
                    ) from error

    def _validate_existing_tree(self) -> None:
        for directory in (
            self.workflow_path,
            self.runs_path,
            self.approvals_path,
            self.receipts_path,
            self.transactions_path,
        ):
            self._validate_contained(directory)
            if os.path.lexists(directory):
                self._validate_directory(directory)

    def _validate_directory(self, path: Path) -> None:
        try:
            path_stat = os.lstat(path)
        except OSError as error:
            raise _integrity_error(f"cannot inspect directory {path.name}: {error}") from error
        if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISDIR(path_stat.st_mode):
            raise _integrity_error(f"workflow path component is not a real directory: {path.name}")

    def _validate_contained(self, path: Path) -> None:
        try:
            path.relative_to(self.project_path)
        except ValueError as error:
            raise _integrity_error("workflow path escapes the Project root") from error

    def _validate_regular_file(self, path: Path) -> None:
        self._validate_contained(path)
        try:
            file_stat = os.lstat(path)
        except OSError as error:
            raise _integrity_error(f"cannot inspect {path.name}: {error}") from error
        if (
            stat.S_ISLNK(file_stat.st_mode)
            or not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_nlink != 1
        ):
            raise _integrity_error(f"workflow JSON is not a single-link regular file: {path.name}")

    def _validate_output_refs(self, receipt: ActionReceipt) -> None:
        for output_ref in receipt.output_refs:
            if output_ref.project_relative_path is None:
                continue
            current = self.project_path
            parts = Path(output_ref.project_relative_path).parts
            for index, part in enumerate(parts):
                current = current / part
                self._validate_contained(current)
                try:
                    current_stat = os.lstat(current)
                except OSError as error:
                    raise _integrity_error(
                        f"receipt output is missing: {output_ref.project_relative_path}"
                    ) from error
                if stat.S_ISLNK(current_stat.st_mode):
                    raise _integrity_error(
                        f"receipt output path contains a symlink: {output_ref.project_relative_path}"
                    )
                if index < len(parts) - 1:
                    if not stat.S_ISDIR(current_stat.st_mode):
                        raise _integrity_error(
                            f"receipt output parent is not a directory: "
                            f"{output_ref.project_relative_path}"
                        )
                elif not stat.S_ISREG(current_stat.st_mode) or current_stat.st_nlink != 1:
                    raise _integrity_error(
                        f"receipt output is not a single-link regular file: "
                        f"{output_ref.project_relative_path}"
                    )

    def _read_object(
        self,
        path: Path,
        parser: Callable[[object], _StoredObject],
        *,
        description: str,
    ) -> _StoredObject:
        self._validate_regular_file(path)
        try:
            payload = path.read_bytes()
        except OSError as error:
            raise _integrity_error(f"{description} is unreadable: {error}") from error
        try:
            return parser(load_closed_json(payload))
        except WorkflowError as error:
            raise _integrity_error(
                f"{description} failed strict parsing: {error}"
            ) from error
        except Exception as error:
            raise _integrity_error(f"{description} failed strict parsing: {error}") from error

    def _atomic_write(
        self, path: Path, payload: dict[str, object], *, immutable: bool
    ) -> None:
        self._validate_contained(path)
        self._validate_directory(path.parent)
        if os.path.lexists(path):
            self._validate_regular_file(path)
            if immutable:
                raise _integrity_error(f"immutable workflow object already exists: {path.name}")
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(canonical_json_v1(payload))
                temporary_file.write(b"\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
            self._sync_directory(path.parent)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _sync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
