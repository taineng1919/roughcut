"""Fail-closed guards for public writes owned by the finite workflow."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from roughcut.adapters.workflow_store import WorkflowStore
from roughcut.application.workflows import validate_workflow_proposal_ref
from roughcut.domain.errors import WorkflowError
from roughcut.domain.workflow import WorkflowRun

_PARTICIPANT_STAGES = {
    "brief_create": {"scope_review", "outline_review", "draft_review"},
    "content_draft_create": {"draft_review"},
    "content_draft_revise_scoped": {"draft_review"},
    "content_draft_confirm": {"draft_review"},
    "content_draft_propose": {"draft_review"},
    "proposal_create": {"draft_review"},
    "multi_source_proposal_create": {"draft_review"},
    "proposal_confirm": {"roughcut_review"},
    "multi_source_proposal_confirm": {"roughcut_review"},
    "render_roughcut": {"export_review"},
}


def _error(code: str, operation: str, evidence: str) -> WorkflowError:
    return WorkflowError(
        code,
        f"Roughcut workflow public-write gate rejected {operation}: {evidence}",
    )


@contextmanager
def protected_write(
    project_path: Path,
    operation: str,
    *,
    source_id: str | None = None,
    speaker_diarization: bool = False,
    proposal_id: str | None = None,
) -> Iterator[WorkflowRun]:
    """Authorize one fixed public write while holding the Project write lock."""

    store = WorkflowStore(project_path)
    with store.write_lock():
        store.recover_pending()
        run = store.active_run()
        if run is None:
            raise _error(
                "workflow_required",
                operation,
                "the Project has no active WorkflowRun",
            )
        if operation == "transcribe_source":
            _authorize_transcription(
                project_path,
                run,
                source_id=source_id,
                speaker_diarization=speaker_diarization,
            )
            yield run
            return
        if operation == "proposal_reject":
            if run.stage != "roughcut_review":
                raise _error(
                    "workflow_transition_not_allowed",
                    operation,
                    f"stage {run.stage!r} is not roughcut_review",
                )
            current = run.artifact_refs["proposal"]
            if current is None or current.artifact_id != proposal_id:
                raise _error(
                    "workflow_subject_mismatch",
                    operation,
                    "proposal_id is not the WorkflowRun current Proposal",
                )
            validate_workflow_proposal_ref(project_path, current)
            yield run
            return
        allowed_stages = _PARTICIPANT_STAGES.get(operation)
        if allowed_stages is None:
            raise _error(
                "workflow_action_invalid",
                operation,
                "the operation is not in the frozen protected-write table",
            )
        if run.stage not in allowed_stages:
            raise _error(
                "workflow_transition_not_allowed",
                operation,
                f"stage {run.stage!r} is not valid for this participant",
            )
        raise _error(
            "workflow_transition_not_allowed",
            operation,
            "this participant is callable only through its fixed workflow_action façade",
        )


def _authorize_transcription(
    project_path: Path,
    run: WorkflowRun,
    *,
    source_id: str | None,
    speaker_diarization: bool,
) -> None:
    operation = "transcribe_source"
    if run.stage != "scope_review":
        raise _error(
            "workflow_transition_not_allowed",
            operation,
            f"stage {run.stage!r} is not scope_review",
        )
    binding_ids = {binding.source_id for binding in run.ordered_bindings}
    if source_id is None or source_id not in binding_ids:
        raise _error(
            "workflow_subject_mismatch",
            operation,
            "source_id is outside the WorkflowRun ordered scope",
        )
    authorization = next(
        (
            item
            for item in run.scope_authorizations
            if item.source_id == source_id
        ),
        None,
    )
    if authorization is None or not authorization.transcribe:
        raise _error(
            "workflow_approval_required",
            operation,
            "the current scope does not authorize ASR for this source",
        )
    if speaker_diarization and not authorization.speaker_diarization:
        raise _error(
            "workflow_approval_required",
            operation,
            "the current scope does not authorize speaker diarization for this source",
        )
    from roughcut.application.workflows import workflow_status

    status = workflow_status(project_path, run.run_id)
    approval_status = status["approval_statuses"]["scope"]  # type: ignore[index]
    if approval_status == "missing":
        raise _error(
            "workflow_approval_required",
            operation,
            "the WorkflowRun has no scope approval",
        )
    if approval_status != "current":
        raise _error(
            "workflow_stale",
            operation,
            "the scope approval is stale",
        )
