"""Edit proposal validation and explicit user-confirmation services."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeAlias
from uuid import uuid4

from roughcut.adapters.artifact_store import read_json_object, write_new_json
from roughcut.adapters.project_store import ProjectStore
from roughcut.application.agent_context import (
    _find_source,
    _read_edit_decision,
    _read_transcript,
    _validate_id,
    read_agent_context,
    read_multi_source_agent_context,
)
from roughcut.domain.bindings import SourceTranscriptBinding, parse_source_bindings
from roughcut.domain.brief import EditBrief
from roughcut.domain.content_draft import punctuation_stripped
from roughcut.domain.edit import (
    EditClip,
    EditDecision,
    EditProposal,
    MultiSourceEditDecision,
    MultiSourceEditProposal,
)
from roughcut.domain.project import Project, ProjectError, SourceAsset
from roughcut.domain.readable_transcript import canonical_text_for_range
from roughcut.domain.time import TICKS_PER_SECOND
from roughcut.domain.transcript import TimedTranscript, TranscriptSegment

EditProposalLike: TypeAlias = EditProposal | MultiSourceEditProposal
EditDecisionLike: TypeAlias = EditDecision | MultiSourceEditDecision


@dataclass(frozen=True)
class ProposalState:
    proposal: EditProposal
    project_revision: int

    def to_dict(self) -> dict[str, object]:
        return {"proposal": self.proposal.to_dict(), "project_revision": self.project_revision}


@dataclass(frozen=True)
class DecisionState:
    decision: EditDecision
    project_revision: int

    def to_dict(self) -> dict[str, object]:
        return {"decision": self.decision.to_dict(), "project_revision": self.project_revision}


@dataclass(frozen=True)
class MultiSourceProposalState:
    proposal: MultiSourceEditProposal
    project_revision: int

    def to_dict(self) -> dict[str, object]:
        return {"proposal": self.proposal.to_dict(), "project_revision": self.project_revision}


@dataclass(frozen=True)
class MultiSourceDecisionState:
    decision: MultiSourceEditDecision
    project_revision: int

    def to_dict(self) -> dict[str, object]:
        return {"decision": self.decision.to_dict(), "project_revision": self.project_revision}


@dataclass(frozen=True)
class DecisionReadState:
    decision: EditDecisionLike
    project_revision: int

    def to_dict(self) -> dict[str, object]:
        decision_type = (
            "edit_decision"
            if isinstance(self.decision, EditDecision)
            else "multi_source_edit_decision"
        )
        return {
            "decision": {
                "type": decision_type,
                "schema_version": self.decision.schema_version,
                "edit_version_id": self.decision.edit_version_id,
                "payload": self.decision.to_dict(),
            },
            "project_revision": self.project_revision,
        }


@dataclass(frozen=True)
class ProposalRejection:
    proposal_id: str
    project_revision: int
    status: str = "rejected"

    def to_dict(self) -> dict[str, object]:
        return {
            "proposal_id": self.proposal_id,
            "project_revision": self.project_revision,
            "status": self.status,
        }


@dataclass(frozen=True)
class ProposalClipChange:
    clip_id: str
    changed_fields: tuple[str, ...]
    before: EditClip
    after: EditClip

    def to_dict(self) -> dict[str, object]:
        return {
            "clip_id": self.clip_id,
            "changed_fields": list(self.changed_fields),
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
        }


@dataclass(frozen=True)
class ProposalDiff:
    base_edit_version_id: str
    proposal_id: str
    schema_version: int
    before_clip_count: int
    after_clip_count: int
    before_total_duration_ticks: int
    after_total_duration_ticks: int
    duration_delta_ticks: int
    added: tuple[EditClip, ...]
    removed: tuple[EditClip, ...]
    changed: tuple[ProposalClipChange, ...]
    order_changed: bool
    before_order: tuple[str, ...]
    after_order: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "base_edit_version_id": self.base_edit_version_id,
            "proposal_id": self.proposal_id,
            "schema_version": self.schema_version,
            "before_clip_count": self.before_clip_count,
            "after_clip_count": self.after_clip_count,
            "before_total_duration_ticks": self.before_total_duration_ticks,
            "after_total_duration_ticks": self.after_total_duration_ticks,
            "duration_delta_ticks": self.duration_delta_ticks,
            "added": [clip.to_dict() for clip in self.added],
            "removed": [clip.to_dict() for clip in self.removed],
            "changed": [change.to_dict() for change in self.changed],
            "order_changed": self.order_changed,
            "before_order": list(self.before_order),
            "after_order": list(self.after_order),
        }


@dataclass(frozen=True)
class ProposalDiffState:
    proposal_diff: ProposalDiff
    project_revision: int

    def to_dict(self) -> dict[str, object]:
        return {
            "proposal_diff": self.proposal_diff.to_dict(),
            "project_revision": self.project_revision,
        }


def create_edit_proposal(
    project_path: Path,
    *,
    source_id: str,
    transcript_version_id: str,
    brief_id: str,
    context_hash: str,
    clips: list[dict[str, Any]],
    total_duration_ticks: int,
    expected_revision: int,
) -> ProposalState:
    parsed_clips = _parse_clips(clips)
    store = ProjectStore(project_path)
    project = store.load()
    if project.revision != expected_revision:
        raise ProjectError("project revision conflict")
    _validate_revision_scope(
        store.project_path,
        project,
        schema_version=1,
        source_bindings=(SourceTranscriptBinding(source_id, transcript_version_id),),
        clips=parsed_clips,
    )
    brief = _validate_snapshot(
        store.project_path,
        project,
        source_id=source_id,
        transcript_version_id=transcript_version_id,
        brief_id=brief_id,
        context_hash=context_hash,
        clips=parsed_clips,
        total_duration_ticks=total_duration_ticks,
        expected_revision=expected_revision,
    )
    proposal = EditProposal(
        proposal_id=f"proposal_{uuid4().hex}",
        base_project_revision=project.revision,
        base_edit_version_id=project.active_edit_version_id,
        source_id=source_id,
        transcript_version_id=transcript_version_id,
        brief_snapshot=brief,
        context_hash=context_hash,
        clips=parsed_clips,
        total_duration_ticks=total_duration_ticks,
        created_at=datetime.now(UTC).isoformat(),
    )
    proposal_path = store.project_path / "proposals" / f"{proposal.proposal_id}.json"
    write_new_json(proposal_path, proposal.to_dict())
    latest = store.load()
    if (
        latest.revision != project.revision
        or latest.active_edit_version_id != project.active_edit_version_id
    ):
        proposal_path.unlink(missing_ok=True)
        raise ProjectError("project revision conflict")
    return ProposalState(proposal=proposal, project_revision=project.revision)


def create_multi_source_edit_proposal(
    project_path: Path,
    *,
    source_bindings: list[dict[str, Any]],
    brief_id: str,
    context_hash: str,
    clips: list[dict[str, Any]],
    total_duration_ticks: int,
    expected_revision: int,
) -> MultiSourceProposalState:
    bindings = parse_source_bindings(source_bindings)
    parsed_clips = _parse_clips(clips)
    store = ProjectStore(project_path)
    project = store.load()
    if project.revision != expected_revision:
        raise ProjectError("project revision conflict")
    _validate_revision_scope(
        store.project_path,
        project,
        schema_version=2,
        source_bindings=bindings,
        clips=parsed_clips,
    )
    brief = _validate_multi_source_snapshot(
        store.project_path,
        project,
        source_bindings=bindings,
        brief_id=brief_id,
        context_hash=context_hash,
        clips=parsed_clips,
        total_duration_ticks=total_duration_ticks,
        expected_revision=expected_revision,
    )
    proposal = MultiSourceEditProposal(
        proposal_id=f"proposal_{uuid4().hex}",
        base_project_revision=project.revision,
        base_edit_version_id=project.active_edit_version_id,
        source_bindings=bindings,
        brief_snapshot=brief,
        context_hash=context_hash,
        clips=parsed_clips,
        total_duration_ticks=total_duration_ticks,
        created_at=datetime.now(UTC).isoformat(),
    )
    proposal_path = store.project_path / "proposals" / f"{proposal.proposal_id}.json"
    write_new_json(proposal_path, proposal.to_dict())
    latest = store.load()
    if (
        latest.revision != project.revision
        or latest.active_edit_version_id != project.active_edit_version_id
    ):
        proposal_path.unlink(missing_ok=True)
        raise ProjectError("project revision conflict")
    return MultiSourceProposalState(proposal=proposal, project_revision=project.revision)


def prepare_proposal_decision(
    project_path: Path,
    project: Project,
    proposal: EditProposalLike,
    *,
    created_at: str,
) -> tuple[Project, EditDecisionLike]:
    """Prepare one exact Decision and Project image without publishing."""

    edit_version_id = f"edit_{proposal.proposal_id.removeprefix('proposal_')}"
    if isinstance(proposal, EditProposal):
        decision: EditDecisionLike = EditDecision(
            edit_version_id=edit_version_id,
            proposal_snapshot=proposal,
            project_revision=project.revision + 1,
            created_at=created_at,
        )
    else:
        decision = MultiSourceEditDecision(
            edit_version_id=edit_version_id,
            proposal_snapshot=proposal,
            project_revision=project.revision + 1,
            created_at=created_at,
        )
    updated = replace(
        project,
        revision=project.revision + 1,
        updated_at=created_at,
        active_edit_version_id=edit_version_id,
        edit_redo_stack=(),
    )
    return updated, decision


def _edit_lineage_clips(
    project_path: Path,
    project: Project,
) -> tuple[EditClip, ...]:
    clips: list[EditClip] = []
    seen_edits: set[str] = set()
    current_id = project.active_edit_version_id
    while current_id is not None:
        if current_id in seen_edits:
            raise ProjectError("edit history contains a cycle")
        seen_edits.add(current_id)
        decision = _read_edit_decision(project_path, current_id)
        clips.extend(decision.proposal_snapshot.clips)
        current_id = decision.proposal_snapshot.base_edit_version_id
    return tuple(clips)


def _is_verified_edit_lineage_clips(
    clips: tuple[EditClip, ...],
    trusted_clips: tuple[EditClip, ...],
) -> bool:
    trusted_by_id: dict[str, EditClip] = {}
    for trusted in trusted_clips:
        trusted_by_id.setdefault(trusted.clip_id, trusted)
    seen: set[str] = set()
    for clip in clips:
        if clip.clip_id in seen:
            return False
        seen.add(clip.clip_id)
        trusted_clip = trusted_by_id.get(clip.clip_id)
        if trusted_clip is None or _clip_display_identity(clip) != _clip_display_identity(trusted_clip):
            return False
    return True


def _clip_display_identity(clip: EditClip) -> tuple[str, str, str, str]:
    return (clip.source_id, clip.transcript_version_id, clip.segment_id, clip.display_text)


def _compile_active_content_draft_proposal(
    project_path: Path,
    project: Project,
    *,
    proposal_id: str,
    created_at: str,
) -> EditProposalLike:
    content_draft_id = project.active_content_draft_id
    if content_draft_id is None:
        raise ProjectError("project has no active Content Draft")
    # Keep the Content Draft compiler private to this trust check.  The
    # public Proposal constructors remain canonical-only; recompiling the
    # immutable active draft is the only provenance available without
    # changing the Proposal schema.
    from roughcut.application.content_drafts import (
        prepare_content_draft_proposal,
        read_content_draft,
    )

    state = read_content_draft(project_path, content_draft_id)
    if (
        not state.content_draft.confirmed_by_user
        or state.content_draft.content_draft_id != content_draft_id
        or (
            state.status != "current"
            and (
                "project_revision" not in state.stale_reasons
                or not set(state.stale_reasons)
                <= {"project_revision", "context_hash"}
            )
        )
    ):
        raise ProjectError("active Content Draft is not current and confirmed")
    return prepare_content_draft_proposal(
        project_path,
        project,
        state.content_draft,
        proposal_id=proposal_id,
        created_at=created_at,
    )


def _is_verified_content_draft_clips(
    project_path: Path,
    project: Project,
    proposal: EditProposalLike,
    clips: tuple[EditClip, ...],
) -> bool:
    """Verify an edit candidate's displays against an active draft compilation."""

    try:
        compiled = _compile_active_content_draft_proposal(
            project_path,
            project,
            proposal_id="proposal_core_compiled",
            created_at="core-compiled",
        )
    except (OSError, ProjectError):
        return False
    if type(compiled) is not type(proposal):
        return False
    if isinstance(compiled, EditProposal) and isinstance(proposal, EditProposal):
        if (
            compiled.source_id != proposal.source_id
            or compiled.transcript_version_id != proposal.transcript_version_id
        ):
            return False
    elif isinstance(compiled, MultiSourceEditProposal) and isinstance(
        proposal, MultiSourceEditProposal
    ):
        if compiled.source_bindings != proposal.source_bindings:
            return False
    else:
        return False

    compiled_by_id = {clip.clip_id: clip for clip in compiled.clips}
    if len(compiled_by_id) != len(compiled.clips):
        return False
    seen: set[str] = set()
    for clip in clips:
        if clip.clip_id in seen:
            return False
        seen.add(clip.clip_id)
        expected = compiled_by_id.get(clip.clip_id)
        if expected is None:
            return False
        if (
            clip.source_id,
            clip.transcript_version_id,
            clip.segment_id,
            clip.display_text,
        ) != (
            expected.source_id,
            expected.transcript_version_id,
            expected.segment_id,
            expected.display_text,
        ):
            return False
    return True


def _are_trusted_editorial_clips(
    project_path: Path,
    project: Project,
    proposal: EditProposalLike,
    clips: tuple[EditClip, ...],
) -> bool:
    if _is_verified_edit_lineage_clips(
        clips, _edit_lineage_clips(project_path, project)
    ):
        return True
    return _is_verified_content_draft_clips(
        project_path,
        project,
        proposal,
        clips,
    )


def _validate_proposal_decision_basis(
    project_path: Path,
    project: Project,
    proposal: EditProposalLike,
) -> None:
    if proposal.base_project_revision != project.revision:
        raise ProjectError("proposal project revision is stale")
    if proposal.base_edit_version_id != project.active_edit_version_id:
        raise ProjectError("proposal edit base is stale")
    allow_editorial_display = _are_trusted_editorial_clips(
        project_path, project, proposal, proposal.clips
    )
    if isinstance(proposal, EditProposal):
        _validate_revision_scope(
            project_path,
            project,
            schema_version=1,
            source_bindings=(
                SourceTranscriptBinding(
                    proposal.source_id, proposal.transcript_version_id
                ),
            ),
            clips=proposal.clips,
        )
        brief = _validate_snapshot(
            project_path,
            project,
            source_id=proposal.source_id,
            transcript_version_id=proposal.transcript_version_id,
            brief_id=proposal.brief_snapshot.brief_id,
            context_hash=proposal.context_hash,
            clips=proposal.clips,
            total_duration_ticks=proposal.total_duration_ticks,
            expected_revision=project.revision,
            allow_editorial_display=allow_editorial_display,
        )
    else:
        _validate_revision_scope(
            project_path,
            project,
            schema_version=2,
            source_bindings=proposal.source_bindings,
            clips=proposal.clips,
        )
        brief = _validate_multi_source_snapshot(
            project_path,
            project,
            source_bindings=proposal.source_bindings,
            brief_id=proposal.brief_snapshot.brief_id,
            context_hash=proposal.context_hash,
            clips=proposal.clips,
            total_duration_ticks=proposal.total_duration_ticks,
            expected_revision=project.revision,
            allow_editorial_display=allow_editorial_display,
        )
    if brief != proposal.brief_snapshot:
        raise ProjectError("proposal brief snapshot is stale")


def confirm_edit_proposal(
    project_path: Path, proposal_id: str, *, expected_revision: int
) -> DecisionState:
    store = ProjectStore(project_path)
    project = store.load()
    if project.revision != expected_revision:
        raise ProjectError("project revision conflict")
    proposal = _read_proposal(store.project_path, proposal_id)
    _validate_proposal_decision_basis(store.project_path, project, proposal)
    updated, prepared = prepare_proposal_decision(
        store.project_path,
        project,
        proposal,
        created_at=datetime.now(UTC).isoformat(),
    )
    assert isinstance(prepared, EditDecision)
    decision_path = store.project_path / "edits" / f"{prepared.edit_version_id}.json"
    write_new_json(decision_path, prepared.to_dict())
    try:
        store.save(updated, expected_revision=expected_revision)
    except Exception:
        decision_path.unlink(missing_ok=True)
        raise
    return DecisionState(decision=prepared, project_revision=updated.revision)


def confirm_multi_source_edit_proposal(
    project_path: Path, proposal_id: str, *, expected_revision: int
) -> MultiSourceDecisionState:
    store = ProjectStore(project_path)
    project = store.load()
    if project.revision != expected_revision:
        raise ProjectError("project revision conflict")
    proposal = _read_multi_source_proposal(store.project_path, proposal_id)
    _validate_proposal_decision_basis(store.project_path, project, proposal)
    updated, prepared = prepare_proposal_decision(
        store.project_path,
        project,
        proposal,
        created_at=datetime.now(UTC).isoformat(),
    )
    assert isinstance(prepared, MultiSourceEditDecision)
    decision_path = store.project_path / "edits" / f"{prepared.edit_version_id}.json"
    write_new_json(decision_path, prepared.to_dict())
    try:
        store.save(updated, expected_revision=expected_revision)
    except Exception:
        decision_path.unlink(missing_ok=True)
        raise
    return MultiSourceDecisionState(
        decision=prepared, project_revision=updated.revision
    )


def reject_edit_proposal(
    project_path: Path, proposal_id: str, *, expected_revision: int
) -> ProposalRejection:
    store = ProjectStore(project_path)
    project = store.load()
    if project.revision != expected_revision:
        raise ProjectError("project revision conflict")
    proposal = _read_proposal(store.project_path, proposal_id)
    return ProposalRejection(
        proposal_id=proposal.proposal_id,
        project_revision=project.revision,
    )


def reject_multi_source_edit_proposal(
    project_path: Path, proposal_id: str, *, expected_revision: int
) -> ProposalRejection:
    store = ProjectStore(project_path)
    project = store.load()
    if project.revision != expected_revision:
        raise ProjectError("project revision conflict")
    proposal = _read_multi_source_proposal(store.project_path, proposal_id)
    return ProposalRejection(
        proposal_id=proposal.proposal_id,
        project_revision=project.revision,
    )


def read_edit_decision(project_path: Path, edit_version_id: str) -> DecisionState:
    decision, project_revision = _read_decision_state(project_path, edit_version_id)
    if not isinstance(decision, EditDecision):
        raise ProjectError("edit decision schema does not match legacy reader")
    return DecisionState(decision=decision, project_revision=project_revision)


def read_multi_source_edit_decision(
    project_path: Path, edit_version_id: str
) -> MultiSourceDecisionState:
    decision, project_revision = _read_decision_state(project_path, edit_version_id)
    if not isinstance(decision, MultiSourceEditDecision):
        raise ProjectError("multi-source edit decision schema does not match legacy reader")
    return MultiSourceDecisionState(decision=decision, project_revision=project_revision)


def read_decision(project_path: Path, edit_version_id: str) -> DecisionReadState:
    decision, project_revision = _read_decision_state(project_path, edit_version_id)
    return DecisionReadState(decision=decision, project_revision=project_revision)


def _read_decision_state(
    project_path: Path, edit_version_id: str
) -> tuple[EditDecisionLike, int]:
    _validate_id(edit_version_id, "edit_version_id")
    store = ProjectStore(project_path)
    project = store.load()
    decision = _read_decision_artifact(
        store.project_path,
        edit_version_id,
        description="edit decision",
    )
    return decision, project.revision


def _read_decision_artifact(
    project_path: Path,
    edit_version_id: str,
    *,
    description: str,
) -> EditDecisionLike:
    _validate_id(edit_version_id, "edit_version_id")
    data = read_json_object(
        project_path / "edits" / f"{edit_version_id}.json",
        description=description,
    )
    schema = data.get("schema_version")
    if schema == 1:
        decision: EditDecisionLike = EditDecision.from_dict(data)
    elif schema == 2:
        decision = MultiSourceEditDecision.from_dict(data)
    else:
        raise ProjectError("unsupported edit decision schema")
    if decision.edit_version_id != edit_version_id:
        raise ProjectError("edit decision identity mismatch")
    return decision


def read_edit_proposal(project_path: Path, proposal_id: str) -> ProposalState:
    store = ProjectStore(project_path)
    project = store.load()
    return ProposalState(
        proposal=_read_proposal(store.project_path, proposal_id),
        project_revision=project.revision,
    )


def read_multi_source_edit_proposal(
    project_path: Path, proposal_id: str
) -> MultiSourceProposalState:
    store = ProjectStore(project_path)
    project = store.load()
    return MultiSourceProposalState(
        proposal=_read_multi_source_proposal(store.project_path, proposal_id),
        project_revision=project.revision,
    )


def read_proposal_diff(
    project_path: Path,
    proposal_id: str,
    *,
    expected_revision: int,
) -> ProposalDiffState:
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise ProjectError("expected revision must be a non-negative integer")
    store = ProjectStore(project_path)
    project = store.load()
    if project.revision != expected_revision:
        raise ProjectError("project revision conflict")
    proposal = _read_proposal_any(store.project_path, proposal_id)
    if proposal.base_project_revision != project.revision:
        raise ProjectError("proposal project revision is stale")
    if project.active_edit_version_id is None:
        raise ProjectError("proposal edit base is stale")
    if proposal.base_edit_version_id != project.active_edit_version_id:
        raise ProjectError("proposal edit base is stale")
    allow_editorial_display = _are_trusted_editorial_clips(
        store.project_path, project, proposal, proposal.clips
    )
    if isinstance(proposal, EditProposal):
        bindings = (SourceTranscriptBinding(proposal.source_id, proposal.transcript_version_id),)
        base = _validate_revision_scope(
            store.project_path,
            project,
            schema_version=1,
            source_bindings=bindings,
            clips=proposal.clips,
        )
        brief = _validate_snapshot(
            store.project_path,
            project,
            source_id=proposal.source_id,
            transcript_version_id=proposal.transcript_version_id,
            brief_id=proposal.brief_snapshot.brief_id,
            context_hash=proposal.context_hash,
            clips=proposal.clips,
            total_duration_ticks=proposal.total_duration_ticks,
            expected_revision=expected_revision,
            allow_editorial_display=allow_editorial_display,
        )
    else:
        base = _validate_revision_scope(
            store.project_path,
            project,
            schema_version=2,
            source_bindings=proposal.source_bindings,
            clips=proposal.clips,
        )
        brief = _validate_multi_source_snapshot(
            store.project_path,
            project,
            source_bindings=proposal.source_bindings,
            brief_id=proposal.brief_snapshot.brief_id,
            context_hash=proposal.context_hash,
            clips=proposal.clips,
            total_duration_ticks=proposal.total_duration_ticks,
            expected_revision=expected_revision,
            allow_editorial_display=allow_editorial_display,
        )
    if brief != proposal.brief_snapshot:
        raise ProjectError("proposal brief snapshot is stale")
    if base is None:
        raise ProjectError("proposal edit base is stale")
    return ProposalDiffState(
        proposal_diff=_proposal_diff(base, proposal),
        project_revision=project.revision,
    )


def _validate_revision_scope(
    project_path: Path,
    project: Project,
    *,
    schema_version: int,
    source_bindings: tuple[SourceTranscriptBinding, ...],
    clips: tuple[EditClip, ...],
) -> EditDecisionLike | None:
    active_id = project.active_edit_version_id
    if active_id is None:
        return None
    decision = _read_edit_decision(project_path, active_id)
    if decision.schema_version != schema_version:
        raise ProjectError("revision proposal schema does not match active Decision")
    active_bindings: tuple[SourceTranscriptBinding, ...]
    if isinstance(decision, EditDecision):
        single_active_proposal = decision.proposal_snapshot
        active_bindings = (
            SourceTranscriptBinding(
                single_active_proposal.source_id,
                single_active_proposal.transcript_version_id,
            ),
        )
        active_clips = single_active_proposal.clips
    else:
        multi_active_proposal = decision.proposal_snapshot
        active_bindings = multi_active_proposal.source_bindings
        active_clips = multi_active_proposal.clips
    if source_bindings != active_bindings:
        raise ProjectError("revision proposal source bindings do not match active Decision")
    base_by_id = {clip.clip_id: clip for clip in active_clips}
    for clip in clips:
        base_clip = base_by_id.get(clip.clip_id)
        if base_clip is None:
            continue
        if (
            clip.source_id,
            clip.transcript_version_id,
            clip.segment_id,
        ) != (
            base_clip.source_id,
            base_clip.transcript_version_id,
            base_clip.segment_id,
        ):
            raise ProjectError("revision proposal reused clip ID with a different identity")
    return decision


def _proposal_diff(
    base: EditDecisionLike,
    proposal: EditProposalLike,
) -> ProposalDiff:
    before = base.proposal_snapshot.clips
    after = proposal.clips
    before_by_id = {clip.clip_id: clip for clip in before}
    after_by_id = {clip.clip_id: clip for clip in after}
    added = tuple(clip for clip in after if clip.clip_id not in before_by_id)
    removed = tuple(clip for clip in before if clip.clip_id not in after_by_id)
    change_fields = (
        "source_in_ticks",
        "source_out_ticks",
        "display_text",
        "reason",
    )
    changed: list[ProposalClipChange] = []
    for clip in after:
        base_clip = before_by_id.get(clip.clip_id)
        if base_clip is None:
            continue
        fields = tuple(
            field for field in change_fields if getattr(base_clip, field) != getattr(clip, field)
        )
        if fields:
            changed.append(
                ProposalClipChange(
                    clip_id=clip.clip_id,
                    changed_fields=fields,
                    before=base_clip,
                    after=clip,
                )
            )
    before_order = tuple(clip.clip_id for clip in before)
    after_order = tuple(clip.clip_id for clip in after)
    before_shared = tuple(clip_id for clip_id in before_order if clip_id in after_by_id)
    after_shared = tuple(clip_id for clip_id in after_order if clip_id in before_by_id)
    return ProposalDiff(
        base_edit_version_id=base.edit_version_id,
        proposal_id=proposal.proposal_id,
        schema_version=proposal.schema_version,
        before_clip_count=len(before),
        after_clip_count=len(after),
        before_total_duration_ticks=base.proposal_snapshot.total_duration_ticks,
        after_total_duration_ticks=proposal.total_duration_ticks,
        duration_delta_ticks=(
            proposal.total_duration_ticks - base.proposal_snapshot.total_duration_ticks
        ),
        added=added,
        removed=removed,
        changed=tuple(changed),
        order_changed=before_shared != after_shared,
        before_order=before_order,
        after_order=after_order,
    )


def _validate_snapshot(
    project_path: Path,
    project: Project,
    *,
    source_id: str,
    transcript_version_id: str,
    brief_id: str,
    context_hash: str,
    clips: tuple[EditClip, ...],
    total_duration_ticks: int,
    expected_revision: int,
    allow_editorial_display: bool = False,
) -> EditBrief:
    if project.revision != expected_revision:
        raise ProjectError("project revision conflict")
    context = read_agent_context(
        project_path,
        source_id=source_id,
        transcript_version_id=transcript_version_id,
        brief_id=brief_id,
        expected_revision=expected_revision,
        offset=0,
        limit=1,
    )
    if context.context_hash != context_hash:
        raise ProjectError("proposal context hash is stale")
    source = _find_source(project, source_id)
    transcript = _read_transcript(project_path, source_id, transcript_version_id)
    _validate_clips(
        source.probe.duration_ticks,
        transcript,
        context.brief,
        clips,
        allow_editorial_display=allow_editorial_display,
    )
    if (
        isinstance(total_duration_ticks, bool)
        or not isinstance(total_duration_ticks, int)
        or total_duration_ticks <= 0
    ):
        raise ProjectError("proposal total duration must be positive")
    calculated_duration = sum(clip.duration_ticks for clip in clips)
    if total_duration_ticks != calculated_duration:
        raise ProjectError("proposal total duration does not match clips")
    tolerance = max(5 * TICKS_PER_SECOND, context.brief.target_duration_ticks // 10)
    if total_duration_ticks > context.brief.target_duration_ticks + tolerance:
        raise ProjectError("proposal exceeds the reasonable target duration")
    return context.brief


def _validate_multi_source_snapshot(
    project_path: Path,
    project: Project,
    *,
    source_bindings: tuple[SourceTranscriptBinding, ...],
    brief_id: str,
    context_hash: str,
    clips: tuple[EditClip, ...],
    total_duration_ticks: int,
    expected_revision: int,
    allow_editorial_display: bool = False,
) -> EditBrief:
    if project.revision != expected_revision:
        raise ProjectError("project revision conflict")
    context = read_multi_source_agent_context(
        project_path,
        source_bindings=[binding.to_dict() for binding in source_bindings],
        brief_id=brief_id,
        expected_revision=expected_revision,
        offset=0,
        limit=1,
    )
    if context.context_hash != context_hash:
        raise ProjectError("proposal context hash is stale")
    sources = {
        binding.source_id: _find_source(project, binding.source_id) for binding in source_bindings
    }
    transcripts = {
        (binding.source_id, binding.transcript_version_id): _read_transcript(
            project_path, binding.source_id, binding.transcript_version_id
        )
        for binding in source_bindings
    }
    _validate_multi_source_clips(
        project,
        sources,
        transcripts,
        context.brief,
        clips,
        allow_editorial_display=allow_editorial_display,
    )
    if (
        isinstance(total_duration_ticks, bool)
        or not isinstance(total_duration_ticks, int)
        or total_duration_ticks <= 0
    ):
        raise ProjectError("proposal total duration must be positive")
    calculated_duration = sum(clip.duration_ticks for clip in clips)
    if total_duration_ticks != calculated_duration:
        raise ProjectError("proposal total duration does not match clips")
    tolerance = max(5 * TICKS_PER_SECOND, context.brief.target_duration_ticks // 10)
    if total_duration_ticks > context.brief.target_duration_ticks + tolerance:
        raise ProjectError("proposal exceeds the reasonable target duration")
    return context.brief


def _validate_multi_source_clips(
    project: Project,
    sources: dict[str, SourceAsset],
    transcripts: dict[tuple[str, str], TimedTranscript],
    brief: EditBrief,
    clips: tuple[EditClip, ...],
    *,
    require_canonical_text: bool = True,
    allow_editorial_display: bool = False,
) -> None:
    if not clips:
        raise ProjectError("proposal must contain at least one clip")
    source_positions = {source.source_id: index for index, source in enumerate(project.sources)}
    segment_positions = {
        key: {segment.segment_id: index for index, segment in enumerate(transcript.segments)}
        for key, transcript in transcripts.items()
    }
    segments = {
        key: {segment.segment_id: segment for segment in transcript.segments}
        for key, transcript in transcripts.items()
    }
    seen_clip_ids: set[str] = set()
    order: list[tuple[int, int, int]] = []
    for clip in clips:
        if clip.clip_id in seen_clip_ids:
            raise ProjectError("proposal clip IDs must be unique")
        seen_clip_ids.add(clip.clip_id)
        source = sources.get(clip.source_id)
        transcript_key = (clip.source_id, clip.transcript_version_id)
        transcript_segments = segments.get(transcript_key)
        if source is None:
            raise ProjectError("proposal clip references an unknown source")
        if transcript_segments is None:
            raise ProjectError("proposal clip references an unknown transcript")
        segment = transcript_segments.get(clip.segment_id)
        if segment is None:
            raise ProjectError("proposal clip references an unknown segment")
        _validate_clip_range(clip, segment, source.probe.duration_ticks)
        if require_canonical_text:
            canonical_text = canonical_text_for_range(
                segment, clip.source_in_ticks, clip.source_out_ticks
            )
            if allow_editorial_display:
                valid_display = (
                    isinstance(clip.display_text, str)
                    and punctuation_stripped(clip.display_text)
                    in {
                        punctuation_stripped(canonical_text),
                        punctuation_stripped(segment.corrected_text or segment.original_text),
                    }
                )
            else:
                valid_display = clip.display_text == canonical_text
        else:
            valid_display = clip.display_text in (segment.corrected_text or segment.original_text)
        if not valid_display:
            raise ProjectError("proposal clip display_text does not match canonical text")
        order.append(
            (
                source_positions[clip.source_id],
                segment_positions[transcript_key][clip.segment_id],
                clip.source_in_ticks,
            )
        )
    if not brief.allow_reorder and order != sorted(order):
        raise ProjectError("brief does not allow clip reorder")


def _validate_clips(
    source_duration_ticks: int,
    transcript: TimedTranscript,
    brief: EditBrief,
    clips: tuple[EditClip, ...],
    *,
    require_canonical_text: bool = True,
    allow_editorial_display: bool = False,
) -> None:
    if not clips:
        raise ProjectError("proposal must contain at least one clip")
    segment_positions = {
        segment.segment_id: index for index, segment in enumerate(transcript.segments)
    }
    segments = {segment.segment_id: segment for segment in transcript.segments}
    seen_clip_ids: set[str] = set()
    order: list[tuple[int, int]] = []
    for clip in clips:
        if clip.clip_id in seen_clip_ids:
            raise ProjectError("proposal clip IDs must be unique")
        seen_clip_ids.add(clip.clip_id)
        if clip.source_id != transcript.source_id:
            raise ProjectError("proposal clip references an unknown source")
        if clip.transcript_version_id != transcript.transcript_version_id:
            raise ProjectError("proposal clip references an unknown transcript")
        segment = segments.get(clip.segment_id)
        if segment is None:
            raise ProjectError("proposal clip references an unknown segment")
        _validate_clip_range(clip, segment, source_duration_ticks)
        if require_canonical_text:
            canonical_text = canonical_text_for_range(
                segment, clip.source_in_ticks, clip.source_out_ticks
            )
            if allow_editorial_display:
                valid_display = (
                    isinstance(clip.display_text, str)
                    and punctuation_stripped(clip.display_text)
                    in {
                        punctuation_stripped(canonical_text),
                        punctuation_stripped(segment.corrected_text or segment.original_text),
                    }
                )
            else:
                valid_display = clip.display_text == canonical_text
        else:
            valid_display = clip.display_text in (segment.corrected_text or segment.original_text)
        if not valid_display:
            raise ProjectError("proposal clip display_text does not match canonical text")
        order.append((segment_positions[clip.segment_id], clip.source_in_ticks))
    if not brief.allow_reorder and order != sorted(order):
        raise ProjectError("brief does not allow clip reorder")


def _validate_clip_range(
    clip: EditClip, segment: TranscriptSegment, source_duration_ticks: int
) -> None:
    if clip.source_in_ticks < 0 or clip.source_out_ticks > source_duration_ticks:
        raise ProjectError("proposal clip is outside source bounds")
    if clip.source_out_ticks <= clip.source_in_ticks:
        raise ProjectError("proposal clip range must be non-empty and ordered")
    if clip.source_in_ticks < segment.start_ticks or clip.source_out_ticks > segment.end_ticks:
        raise ProjectError("proposal clip is outside its transcript segment")


def _parse_clips(clips: list[dict[str, Any]]) -> tuple[EditClip, ...]:
    if not isinstance(clips, list):
        raise ProjectError("proposal clips must be a list")
    parsed: list[EditClip] = []
    for clip in clips:
        if not isinstance(clip, dict):
            raise ProjectError("proposal clip must be an object")
        parsed.append(EditClip.from_dict(clip))
    return tuple(parsed)


def _read_proposal(project_path: Path, proposal_id: str) -> EditProposal:
    _validate_id(proposal_id, "proposal_id")
    data = read_json_object(
        project_path / "proposals" / f"{proposal_id}.json", description="edit proposal"
    )
    proposal = EditProposal.from_dict(data)
    if proposal.proposal_id != proposal_id:
        raise ProjectError("edit proposal identity mismatch")
    return proposal


def _read_multi_source_proposal(project_path: Path, proposal_id: str) -> MultiSourceEditProposal:
    _validate_id(proposal_id, "proposal_id")
    data = read_json_object(
        project_path / "proposals" / f"{proposal_id}.json",
        description="multi-source edit proposal",
    )
    proposal = MultiSourceEditProposal.from_dict(data)
    if proposal.proposal_id != proposal_id:
        raise ProjectError("edit proposal identity mismatch")
    return proposal


def _read_proposal_any(project_path: Path, proposal_id: str) -> EditProposalLike:
    _validate_id(proposal_id, "proposal_id")
    data = read_json_object(
        project_path / "proposals" / f"{proposal_id}.json",
        description="edit proposal",
    )
    schema = data.get("schema_version")
    if schema == 1:
        proposal: EditProposalLike = EditProposal.from_dict(data)
    elif schema == 2:
        proposal = MultiSourceEditProposal.from_dict(data)
    else:
        raise ProjectError("unsupported edit proposal schema")
    if proposal.proposal_id != proposal_id:
        raise ProjectError("edit proposal identity mismatch")
    return proposal
