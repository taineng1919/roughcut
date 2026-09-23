"""Immutable direct Edit changes and persistent single-sequence history navigation."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeAlias
from uuid import uuid4

from roughcut.adapters.artifact_store import write_new_json
from roughcut.adapters.project_store import ProjectStore
from roughcut.application.agent_context import (
    _find_source,
    _read_transcript,
    read_agent_context,
    read_multi_source_agent_context,
)
from roughcut.application.proposals import (
    _are_trusted_editorial_clips,
    _is_verified_edit_lineage_clips,
    _read_decision_artifact,
    _validate_clips,
    _validate_multi_source_clips,
)
from roughcut.domain.agent import AgentContextPage, MultiSourceAgentContextPage
from roughcut.domain.edit import (
    EditClip,
    EditDecision,
    EditProposal,
    MultiSourceEditDecision,
    MultiSourceEditProposal,
)
from roughcut.domain.project import Project, ProjectError

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")

EditDecisionLike: TypeAlias = EditDecision | MultiSourceEditDecision
EditProposalLike: TypeAlias = EditProposal | MultiSourceEditProposal


@dataclass(frozen=True)
class EditChangeState:
    changed: bool
    operation_type: str
    project_revision: int
    decision: EditDecisionLike

    def to_dict(self) -> dict[str, object]:
        return {
            "changed": self.changed,
            "operation_type": self.operation_type,
            "project_revision": self.project_revision,
            "decision": self.decision.to_dict(),
        }


@dataclass(frozen=True)
class ReviewProposalChangeState:
    changed: bool
    operation_type: str
    project_revision: int
    proposal: EditProposalLike

    def to_dict(self) -> dict[str, object]:
        return {
            "changed": self.changed,
            "operation_type": self.operation_type,
            "project_revision": self.project_revision,
            "proposal": self.proposal.to_dict(),
        }


@dataclass(frozen=True)
class EditNavigationState:
    project_revision: int
    active_edit_version_id: str
    edit_redo_stack: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "project_revision": self.project_revision,
            "active_edit_version_id": self.active_edit_version_id,
            "edit_redo_stack": list(self.edit_redo_stack),
        }


@dataclass(frozen=True)
class EditHistoryEntry:
    edit_version_id: str
    parent_edit_version_id: str | None
    schema_version: int
    decision_project_revision: int
    clip_count: int
    total_duration_ticks: int
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "edit_version_id": self.edit_version_id,
            "parent_edit_version_id": self.parent_edit_version_id,
            "schema_version": self.schema_version,
            "decision_project_revision": self.decision_project_revision,
            "clip_count": self.clip_count,
            "total_duration_ticks": self.total_duration_ticks,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class EditHistoryState:
    project_revision: int
    active_edit_version_id: str | None
    active_schema_version: int | None
    can_undo: bool
    can_redo: bool
    ancestors: tuple[EditHistoryEntry, ...]
    redo_stack: tuple[str, ...]
    restorable_clips: tuple[dict[str, object], ...]
    current_clips: tuple[EditClip, ...]
    total_duration_ticks: int

    def to_dict(self) -> dict[str, object]:
        return {
            "project_revision": self.project_revision,
            "active_edit_version_id": self.active_edit_version_id,
            "active_schema_version": self.active_schema_version,
            "can_undo": self.can_undo,
            "can_redo": self.can_redo,
            "ancestors": [entry.to_dict() for entry in self.ancestors],
            "redo_stack": list(self.redo_stack),
            "restorable_clips": list(self.restorable_clips),
            "current_clips": [clip.to_dict() for clip in self.current_clips],
            "total_duration_ticks": self.total_duration_ticks,
        }


@dataclass(frozen=True)
class _ValidatedHistory:
    active: EditDecisionLike | None
    ancestors: tuple[EditDecisionLike, ...]
    redo: tuple[EditDecisionLike, ...]


def propose_review_edit_change(
    project_path: Path,
    *,
    proposal: EditProposalLike,
    operation: dict[str, Any],
    expected_revision: int,
    restoration_clips: tuple[EditClip, ...] = (),
) -> ReviewProposalChangeState:
    """Create an immutable Review candidate without adopting a new Decision."""

    operation_type = _operation_type(operation)
    store = ProjectStore(project_path)
    project = store.load()
    if project.revision != expected_revision:
        raise ProjectError("project revision conflict")
    history = _validate_history(store.project_path, project)
    _validate_review_proposal_basis(project, proposal, history)
    _validate_active_transcript_bindings(project, proposal)
    changed_clips = _apply_review_proposal_operation(
        operation_type,
        operation,
        proposal.clips,
        restoration_clips,
        history.ancestors,
    )
    if changed_clips == proposal.clips:
        return ReviewProposalChangeState(
            changed=False,
            operation_type=operation_type,
            project_revision=project.revision,
            proposal=proposal,
        )
    allow_editorial_display = _are_trusted_editorial_clips(
        store.project_path, project, proposal, changed_clips
    )
    _validate_changed_proposal_clips(
        store.project_path,
        project,
        proposal,
        changed_clips,
        allow_editorial_display=allow_editorial_display,
    )
    context = _current_review_context(store.project_path, project, proposal)
    now = datetime.now(UTC).isoformat()
    identifier = uuid4().hex
    changed_proposal = replace(
        proposal,
        proposal_id=f"proposal_review_{identifier}",
        base_project_revision=project.revision,
        base_edit_version_id=project.active_edit_version_id,
        brief_snapshot=context.brief,
        context_hash=context.context_hash,
        clips=changed_clips,
        total_duration_ticks=sum(clip.duration_ticks for clip in changed_clips),
        created_at=now,
    )
    proposal_path = (
        store.project_path / "proposals" / f"{changed_proposal.proposal_id}.json"
    )
    write_new_json(proposal_path, changed_proposal.to_dict())
    latest = store.load()
    if (
        latest.revision != project.revision
        or latest.active_edit_version_id != project.active_edit_version_id
    ):
        proposal_path.unlink(missing_ok=True)
        raise ProjectError("project revision conflict")
    return ReviewProposalChangeState(
        changed=True,
        operation_type=operation_type,
        project_revision=project.revision,
        proposal=changed_proposal,
    )


def read_review_restorable_clips(
    project_path: Path,
    *,
    proposal: EditProposalLike,
    restoration_clips: tuple[EditClip, ...] = (),
) -> tuple[EditClip, ...]:
    store = ProjectStore(project_path)
    project = store.load()
    history = _validate_history(store.project_path, project)
    _validate_review_proposal_basis(project, proposal, history)
    current_ids = {clip.clip_id for clip in proposal.clips}
    pool: dict[str, EditClip] = {}
    for clip in restoration_clips:
        pool.setdefault(clip.clip_id, clip)
    for decision in history.ancestors:
        for clip in decision.proposal_snapshot.clips:
            pool.setdefault(clip.clip_id, clip)
    return tuple(clip for clip_id, clip in pool.items() if clip_id not in current_ids)


def _validate_review_proposal_basis(
    project: Project,
    proposal: EditProposalLike,
    history: _ValidatedHistory,
) -> None:
    if history.active is not None and history.active.proposal_snapshot == proposal:
        if history.active.edit_version_id != project.active_edit_version_id:
            raise ProjectError("review Decision is not active")
        return
    if proposal.base_project_revision != project.revision:
        raise ProjectError("review proposal project revision is stale")
    if proposal.base_edit_version_id != project.active_edit_version_id:
        raise ProjectError("review proposal edit base is stale")


def _current_review_context(
    project_path: Path,
    project: Project,
    proposal: EditProposalLike,
) -> AgentContextPage | MultiSourceAgentContextPage:
    if isinstance(proposal, EditProposal):
        return read_agent_context(
            project_path,
            source_id=proposal.source_id,
            transcript_version_id=proposal.transcript_version_id,
            brief_id=proposal.brief_snapshot.brief_id,
            expected_revision=project.revision,
            offset=0,
            limit=1,
        )
    return read_multi_source_agent_context(
        project_path,
        source_bindings=[
            binding.to_dict() for binding in proposal.source_bindings
        ],
        brief_id=proposal.brief_snapshot.brief_id,
        expected_revision=project.revision,
        offset=0,
        limit=1,
    )


def change_edit(
    project_path: Path,
    *,
    operation: dict[str, Any],
    expected_revision: int,
    base_edit_version_id: str,
) -> EditChangeState:
    operation_type = _operation_type(operation)
    store, project = _current_project(
        project_path,
        expected_revision=expected_revision,
        base_edit_version_id=base_edit_version_id,
    )
    history = _validate_history(store.project_path, project)
    if history.active is None:
        raise ProjectError("direct edit requires an active Edit Decision")
    _validate_active_transcript_bindings(project, history.active.proposal_snapshot)
    clips = history.active.proposal_snapshot.clips
    changed_clips = _apply_operation(operation_type, operation, clips, history.ancestors)
    if changed_clips == clips:
        return EditChangeState(
            changed=False,
            operation_type=operation_type,
            project_revision=project.revision,
            decision=history.active,
        )
    trusted_clips = tuple(clip for item in history.ancestors for clip in item.proposal_snapshot.clips)
    _validate_changed_clips(
        store.project_path,
        project,
        history.active,
        changed_clips,
        allow_editorial_display=_is_verified_edit_lineage_clips(
            changed_clips, trusted_clips
        ),
    )
    now = datetime.now(UTC).isoformat()
    identifier = uuid4().hex
    decision_id = f"edit_{identifier}"
    if isinstance(history.active, EditDecision):
        single_proposal = replace(
            history.active.proposal_snapshot,
            proposal_id=f"proposal_direct_{identifier}",
            base_edit_version_id=history.active.edit_version_id,
            clips=changed_clips,
            total_duration_ticks=sum(clip.duration_ticks for clip in changed_clips),
            created_at=now,
        )
        decision: EditDecisionLike = EditDecision(
            edit_version_id=decision_id,
            proposal_snapshot=single_proposal,
            project_revision=project.revision + 1,
            created_at=now,
        )
    else:
        multi_proposal = replace(
            history.active.proposal_snapshot,
            proposal_id=f"proposal_direct_{identifier}",
            base_edit_version_id=history.active.edit_version_id,
            clips=changed_clips,
            total_duration_ticks=sum(clip.duration_ticks for clip in changed_clips),
            created_at=now,
        )
        decision = MultiSourceEditDecision(
            edit_version_id=decision_id,
            proposal_snapshot=multi_proposal,
            project_revision=project.revision + 1,
            created_at=now,
        )
    decision_path = store.project_path / "edits" / f"{decision_id}.json"
    write_new_json(decision_path, decision.to_dict())
    updated = replace(
        project,
        revision=project.revision + 1,
        updated_at=now,
        active_edit_version_id=decision_id,
        edit_redo_stack=(),
    )
    try:
        store.save(updated, expected_revision=expected_revision)
    except Exception:
        decision_path.unlink(missing_ok=True)
        raise
    return EditChangeState(
        changed=True,
        operation_type=operation_type,
        project_revision=updated.revision,
        decision=decision,
    )


def undo_edit(
    project_path: Path, *, expected_revision: int, base_edit_version_id: str
) -> EditNavigationState:
    store, project = _current_project(
        project_path,
        expected_revision=expected_revision,
        base_edit_version_id=base_edit_version_id,
    )
    history = _validate_history(store.project_path, project)
    if history.active is None:
        raise ProjectError("edit undo requires an active Edit Decision")
    parent_id = history.active.proposal_snapshot.base_edit_version_id
    if parent_id is None:
        raise ProjectError("root Edit Decision cannot be undone")
    _validate_active_transcript_bindings(project, history.active.proposal_snapshot)
    _validate_active_transcript_bindings(project, history.ancestors[1].proposal_snapshot)
    updated = replace(
        project,
        revision=project.revision + 1,
        updated_at=datetime.now(UTC).isoformat(),
        active_edit_version_id=parent_id,
        edit_redo_stack=(history.active.edit_version_id, *project.edit_redo_stack),
    )
    store.save(updated, expected_revision=expected_revision)
    return EditNavigationState(
        project_revision=updated.revision,
        active_edit_version_id=parent_id,
        edit_redo_stack=updated.edit_redo_stack,
    )


def redo_edit(
    project_path: Path, *, expected_revision: int, base_edit_version_id: str
) -> EditNavigationState:
    store, project = _current_project(
        project_path,
        expected_revision=expected_revision,
        base_edit_version_id=base_edit_version_id,
    )
    history = _validate_history(store.project_path, project)
    if history.active is None:
        raise ProjectError("edit redo requires an active Edit Decision")
    if not history.redo:
        raise ProjectError("edit redo stack is empty")
    next_decision = history.redo[0]
    _validate_active_transcript_bindings(project, history.active.proposal_snapshot)
    _validate_active_transcript_bindings(project, next_decision.proposal_snapshot)
    updated = replace(
        project,
        revision=project.revision + 1,
        updated_at=datetime.now(UTC).isoformat(),
        active_edit_version_id=next_decision.edit_version_id,
        edit_redo_stack=project.edit_redo_stack[1:],
    )
    store.save(updated, expected_revision=expected_revision)
    return EditNavigationState(
        project_revision=updated.revision,
        active_edit_version_id=next_decision.edit_version_id,
        edit_redo_stack=updated.edit_redo_stack,
    )


def read_edit_history(project_path: Path) -> EditHistoryState:
    store = ProjectStore(project_path)
    project = store.load()
    history = _validate_history(store.project_path, project)
    if history.active is None:
        return EditHistoryState(
            project_revision=project.revision,
            active_edit_version_id=None,
            active_schema_version=None,
            can_undo=False,
            can_redo=False,
            ancestors=(),
            redo_stack=(),
            restorable_clips=(),
            current_clips=(),
            total_duration_ticks=0,
        )
    active_clip_ids = {clip.clip_id for clip in history.active.proposal_snapshot.clips}
    restorable: list[dict[str, object]] = []
    discovered = set(active_clip_ids)
    for decision in history.ancestors[1:]:
        for clip in decision.proposal_snapshot.clips:
            if clip.clip_id in discovered:
                continue
            discovered.add(clip.clip_id)
            restorable.append(
                {
                    "clip_id": clip.clip_id,
                    "source_id": clip.source_id,
                    "transcript_version_id": clip.transcript_version_id,
                    "segment_id": clip.segment_id,
                    "source_in_ticks": clip.source_in_ticks,
                    "source_out_ticks": clip.source_out_ticks,
                    "from_edit_version_id": decision.edit_version_id,
                }
            )
    return EditHistoryState(
        project_revision=project.revision,
        active_edit_version_id=history.active.edit_version_id,
        active_schema_version=history.active.schema_version,
        can_undo=history.active.proposal_snapshot.base_edit_version_id is not None,
        can_redo=bool(history.redo),
        ancestors=tuple(_history_entry(decision) for decision in history.ancestors),
        redo_stack=project.edit_redo_stack,
        restorable_clips=tuple(restorable),
        current_clips=history.active.proposal_snapshot.clips,
        total_duration_ticks=history.active.proposal_snapshot.total_duration_ticks,
    )


def _current_project(
    project_path: Path, *, expected_revision: int, base_edit_version_id: str
) -> tuple[ProjectStore, Project]:
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise ProjectError("expected_revision must be a non-negative integer")
    _validate_id(base_edit_version_id, "base_edit_version_id")
    store = ProjectStore(project_path)
    project = store.load()
    if project.revision != expected_revision:
        raise ProjectError("project revision conflict")
    if project.active_edit_version_id != base_edit_version_id:
        raise ProjectError("edit base is stale")
    return store, project


def _validate_history(project_path: Path, project: Project) -> _ValidatedHistory:
    if project.active_edit_version_id is None:
        if project.edit_redo_stack:
            raise ProjectError("edit redo stack exists without an active Edit Decision")
        return _ValidatedHistory(None, (), ())
    ancestors: list[EditDecisionLike] = []
    visited: set[str] = set()
    current_id: str | None = project.active_edit_version_id
    schema: int | None = None
    while current_id is not None:
        if current_id in visited:
            raise ProjectError("edit history contains a cycle")
        visited.add(current_id)
        decision = _read_decision(project_path, current_id)
        if schema is None:
            schema = decision.schema_version
        elif decision.schema_version != schema:
            raise ProjectError("edit history mixes schema versions")
        ancestors.append(decision)
        current_id = decision.proposal_snapshot.base_edit_version_id

    redo: list[EditDecisionLike] = []
    parent_id = project.active_edit_version_id
    for redo_id in project.edit_redo_stack:
        if redo_id in visited:
            raise ProjectError("edit redo stack overlaps active history")
        visited.add(redo_id)
        decision = _read_decision(project_path, redo_id)
        if decision.schema_version != schema:
            raise ProjectError("edit redo stack mixes schema versions")
        if decision.proposal_snapshot.base_edit_version_id != parent_id:
            raise ProjectError("edit redo stack is not a continuous child chain")
        redo.append(decision)
        parent_id = decision.edit_version_id
    return _ValidatedHistory(ancestors[0], tuple(ancestors), tuple(redo))


def _read_decision(project_path: Path, edit_version_id: str) -> EditDecisionLike:
    return _read_decision_artifact(
        project_path,
        edit_version_id,
        description="edit history decision",
    )


def _operation_type(operation: dict[str, Any]) -> str:
    if not isinstance(operation, dict):
        raise ProjectError("edit operation must be an object")
    operation_type = operation.get("type")
    if operation_type not in {"delete", "restore", "reorder", "trim"}:
        raise ProjectError("unsupported direct edit operation")
    assert isinstance(operation_type, str)
    allowed = {
        "delete": {"type", "clip_id"},
        "restore": {"type", "clip_id", "insert_before_clip_id"},
        "reorder": {"type", "ordered_clip_ids"},
        "trim": {"type", "clip_id", "source_in_ticks", "source_out_ticks"},
    }[operation_type]
    required = allowed - ({"insert_before_clip_id"} if operation_type == "restore" else set())
    if set(operation) - allowed or not required.issubset(operation):
        raise ProjectError("direct edit operation fields are invalid")
    return operation_type


def _apply_operation(
    operation_type: str,
    operation: dict[str, Any],
    clips: tuple[EditClip, ...],
    ancestors: tuple[EditDecisionLike, ...],
) -> tuple[EditClip, ...]:
    clip_id: str | None = None
    if operation_type in {"delete", "restore", "trim"}:
        clip_id = _required_id(operation.get("clip_id"), "clip_id")
    if operation_type != "reorder":
        assert clip_id is not None
    positions = {clip.clip_id: index for index, clip in enumerate(clips)}
    if operation_type == "delete":
        if clip_id not in positions:
            raise ProjectError("delete references an unknown clip")
        if len(clips) == 1:
            raise ProjectError("delete cannot produce an empty sequence")
        return tuple(clip for clip in clips if clip.clip_id != clip_id)
    if operation_type == "restore":
        assert clip_id is not None
        if clip_id in positions:
            raise ProjectError("restore clip already exists in the active Decision")
        restored = _nearest_ancestor_clip(ancestors, clip_id)
        anchor = operation.get("insert_before_clip_id")
        if anchor is not None:
            anchor = _required_id(anchor, "insert_before_clip_id")
            if anchor not in positions:
                raise ProjectError("restore insertion anchor is unknown")
            index = positions[anchor]
        else:
            index = len(clips)
        return (*clips[:index], restored, *clips[index:])
    if operation_type == "reorder":
        ordered = operation.get("ordered_clip_ids")
        if not isinstance(ordered, list) or not all(isinstance(item, str) for item in ordered):
            raise ProjectError("ordered_clip_ids must be a string array")
        current_ids = [clip.clip_id for clip in clips]
        if len(ordered) != len(set(ordered)) or set(ordered) != set(current_ids):
            raise ProjectError("ordered_clip_ids must exactly permute current clip IDs")
        by_id = {clip.clip_id: clip for clip in clips}
        return tuple(by_id[ordered_id] for ordered_id in ordered)
    source_in = operation.get("source_in_ticks")
    source_out = operation.get("source_out_ticks")
    if (
        isinstance(source_in, bool)
        or not isinstance(source_in, int)
        or isinstance(source_out, bool)
        or not isinstance(source_out, int)
    ):
        raise ProjectError("trim bounds must be integers")
    assert clip_id is not None
    position = positions.get(clip_id)
    if position is None:
        raise ProjectError("trim references an unknown clip")
    trimmed = replace(clips[position], source_in_ticks=source_in, source_out_ticks=source_out)
    return (*clips[:position], trimmed, *clips[position + 1 :])


def _apply_review_proposal_operation(
    operation_type: str,
    operation: dict[str, Any],
    clips: tuple[EditClip, ...],
    restoration_clips: tuple[EditClip, ...],
    ancestors: tuple[EditDecisionLike, ...],
) -> tuple[EditClip, ...]:
    if operation_type != "restore":
        return _apply_operation(operation_type, operation, clips, ancestors)
    clip_id = _required_id(operation.get("clip_id"), "clip_id")
    positions = {clip.clip_id: index for index, clip in enumerate(clips)}
    if clip_id in positions:
        raise ProjectError("restore clip already exists in the Review candidate")
    restoration_pool: dict[str, EditClip] = {}
    for clip in restoration_clips:
        restoration_pool.setdefault(clip.clip_id, clip)
    for decision in ancestors:
        for clip in decision.proposal_snapshot.clips:
            restoration_pool.setdefault(clip.clip_id, clip)
    restored = restoration_pool.get(clip_id)
    if restored is None:
        raise ProjectError("restore clip is not part of the Review history")
    anchor = operation.get("insert_before_clip_id")
    if anchor is not None:
        anchor = _required_id(anchor, "insert_before_clip_id")
        if anchor not in positions:
            raise ProjectError("restore insertion anchor is unknown")
        index = positions[anchor]
    else:
        index = len(clips)
    return (*clips[:index], restored, *clips[index:])


def _nearest_ancestor_clip(ancestors: tuple[EditDecisionLike, ...], clip_id: str) -> EditClip:
    for decision in ancestors[1:]:
        for clip in decision.proposal_snapshot.clips:
            if clip.clip_id == clip_id:
                return clip
    raise ProjectError("restore clip does not exist in the active ancestor chain")


def _validate_active_transcript_bindings(project: Project, proposal: EditProposalLike) -> None:
    bindings: tuple[tuple[str, str], ...]
    if isinstance(proposal, EditProposal):
        bindings = ((proposal.source_id, proposal.transcript_version_id),)
    else:
        bindings = tuple(
            (binding.source_id, binding.transcript_version_id)
            for binding in proposal.source_bindings
        )
    for source_id, transcript_id in bindings:
        if project.active_transcript_versions.get(source_id) != transcript_id:
            raise ProjectError("Edit Decision has stale Transcript bindings")


def _validate_changed_clips(
    project_path: Path,
    project: Project,
    decision: EditDecisionLike,
    clips: tuple[EditClip, ...],
    *,
    allow_editorial_display: bool,
) -> None:
    proposal = decision.proposal_snapshot
    if isinstance(proposal, EditProposal):
        source = _find_source(project, proposal.source_id)
        transcript = _read_transcript(
            project_path, proposal.source_id, proposal.transcript_version_id
        )
        _validate_clips(
            source.probe.duration_ticks,
            transcript,
            proposal.brief_snapshot,
            clips,
            require_canonical_text=allow_editorial_display,
            allow_editorial_display=allow_editorial_display,
        )
        return
    sources = {
        binding.source_id: _find_source(project, binding.source_id)
        for binding in proposal.source_bindings
    }
    transcripts = {
        (binding.source_id, binding.transcript_version_id): _read_transcript(
            project_path, binding.source_id, binding.transcript_version_id
        )
        for binding in proposal.source_bindings
    }
    _validate_multi_source_clips(
        project,
        sources,
        transcripts,
        proposal.brief_snapshot,
        clips,
        require_canonical_text=allow_editorial_display,
        allow_editorial_display=allow_editorial_display,
    )


def _validate_changed_proposal_clips(
    project_path: Path,
    project: Project,
    proposal: EditProposalLike,
    clips: tuple[EditClip, ...],
    *,
    allow_editorial_display: bool,
) -> None:
    if isinstance(proposal, EditProposal):
        source = _find_source(project, proposal.source_id)
        transcript = _read_transcript(
            project_path,
            proposal.source_id,
            proposal.transcript_version_id,
        )
        _validate_clips(
            source.probe.duration_ticks,
            transcript,
            proposal.brief_snapshot,
            clips,
            require_canonical_text=True,
            allow_editorial_display=allow_editorial_display,
        )
        return
    sources = {
        binding.source_id: _find_source(project, binding.source_id)
        for binding in proposal.source_bindings
    }
    transcripts = {
        (binding.source_id, binding.transcript_version_id): _read_transcript(
            project_path,
            binding.source_id,
            binding.transcript_version_id,
        )
        for binding in proposal.source_bindings
    }
    _validate_multi_source_clips(
        project,
        sources,
        transcripts,
        proposal.brief_snapshot,
        clips,
        require_canonical_text=True,
        allow_editorial_display=allow_editorial_display,
    )


def _history_entry(decision: EditDecisionLike) -> EditHistoryEntry:
    return EditHistoryEntry(
        edit_version_id=decision.edit_version_id,
        parent_edit_version_id=decision.proposal_snapshot.base_edit_version_id,
        schema_version=decision.schema_version,
        decision_project_revision=decision.project_revision,
        clip_count=len(decision.proposal_snapshot.clips),
        total_duration_ticks=decision.proposal_snapshot.total_duration_ticks,
        created_at=decision.created_at,
    )


def _validate_id(value: object, name: str) -> None:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ProjectError(f"{name} is invalid")


def _required_id(value: object, name: str) -> str:
    _validate_id(value, name)
    assert isinstance(value, str)
    return value
