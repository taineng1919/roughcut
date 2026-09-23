"""Immutable Content Draft creation, confirmation, readback, and Proposal compilation."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeAlias
from uuid import uuid4

from roughcut.adapters.artifact_store import read_json_object, write_new_json
from roughcut.adapters.project_lock import project_write_lock
from roughcut.adapters.project_store import ProjectStore
from roughcut.application.agent_context import calculate_agent_context_hash
from roughcut.domain.bindings import SourceTranscriptBinding
from roughcut.domain.brief import EditBrief
from roughcut.domain.content_draft import (
    ContentDraft,
    ContentDraftBlock,
    ContentDraftRef,
    NarrationBlock,
    SectionTitleBlock,
    SourceExcerptBlock,
    legacy_section_block_id,
    project_schema1_to_schema2,
    split_display_text_for_canonical_parts,
)
from roughcut.domain.edit import (
    EditClip,
    EditDecision,
    EditProposal,
    MultiSourceEditDecision,
    MultiSourceEditProposal,
)
from roughcut.domain.project import Project, ProjectError, SourceAsset
from roughcut.domain.readable_transcript import (
    canonical_text_for_range,
    exact_fine_unit_spans,
    trusted_fine_unit_spans,
)
from roughcut.domain.transcript import TimedTranscript

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")

ProposalLike: TypeAlias = EditProposal | MultiSourceEditProposal
DURATION_ACCEPTANCE_TOLERANCE_PERCENT = 10


@dataclass(frozen=True)
class ContentDraftState:
    content_draft: ContentDraft
    project_revision: int
    status: str
    stale_reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "content_draft": self.content_draft.to_dict(),
            "project_revision": self.project_revision,
            "status": self.status,
            "stale_reasons": list(self.stale_reasons),
            "duration_acceptance": duration_acceptance_summary(self.content_draft),
        }


@dataclass(frozen=True)
class ContentDraftMutation:
    content_draft: ContentDraft
    project_revision: int
    status: str
    stale_reasons: tuple[str, ...]
    changed: bool

    def to_dict(self) -> dict[str, object]:
        payload = ContentDraftState(
            self.content_draft,
            self.project_revision,
            self.status,
            self.stale_reasons,
        ).to_dict()
        payload["changed"] = self.changed
        return payload


@dataclass(frozen=True)
class ScopedContentDraftRevision:
    content_draft: ContentDraft
    project_revision: int
    status: str
    stale_reasons: tuple[str, ...]
    changed: bool
    changed_block_ids: tuple[str, ...]
    unchanged_block_ids: tuple[str, ...]
    before_duration_ticks: int
    after_duration_ticks: int

    @property
    def duration_delta_ticks(self) -> int:
        return self.after_duration_ticks - self.before_duration_ticks

    def to_dict(self) -> dict[str, object]:
        payload = ContentDraftMutation(
            self.content_draft,
            self.project_revision,
            self.status,
            self.stale_reasons,
            self.changed,
        ).to_dict()
        payload["revision_summary"] = {
            "changed_block_ids": list(self.changed_block_ids),
            "unchanged_block_ids": list(self.unchanged_block_ids),
            "before_duration_ticks": self.before_duration_ticks,
            "after_duration_ticks": self.after_duration_ticks,
            "duration_delta_ticks": self.duration_delta_ticks,
        }
        return payload


@dataclass(frozen=True)
class ContentDraftProposalState:
    content_draft_id: str
    proposal_schema_version: int
    proposal: ProposalLike
    project_revision: int

    def to_dict(self) -> dict[str, object]:
        return {
            "content_draft_id": self.content_draft_id,
            "proposal_schema_version": self.proposal_schema_version,
            "proposal": self.proposal.to_dict(),
            "project_revision": self.project_revision,
        }


def create_content_draft(
    project_path: Path,
    *,
    parent_draft_id: str | None,
    display_title: str | None = None,
    source_bindings: list[dict[str, Any]],
    brief_id: str,
    context_hash: str,
    blocks: list[dict[str, Any]],
    expected_revision: int,
) -> ContentDraftMutation:
    _validate_revision(expected_revision)
    bindings = _parse_bindings(source_bindings)
    _validate_id(brief_id, "brief_id")
    if parent_draft_id is not None:
        _validate_id(parent_draft_id, "parent_draft_id")
    store = ProjectStore(project_path)
    with project_write_lock(store.project_path):
        project = store.load()
        if project.revision != expected_revision:
            raise ProjectError("project revision conflict")
        brief = _read_active_brief(store.project_path, project, brief_id)
        calculated_hash = calculate_agent_context_hash(
            store.project_path,
            project=project,
            bindings=bindings,
            brief=brief,
        )
        if context_hash != calculated_hash:
            raise ProjectError("content draft context hash is stale")
        _validate_decision_binding_scope(store.project_path, project, bindings)
        parent: ContentDraft | None = None
        if parent_draft_id is not None:
            parent = _read_draft(
                store.project_path, parent_draft_id, validate_parent=False
            )

        parsed_blocks = _parse_blocks(
            project=project,
            project_path=store.project_path,
            bindings=bindings,
            blocks=blocks,
            require_active=True,
            schema_version=2,
            allow_derived_canonical=True,
        )
        if parent is not None:
            parsed_blocks = _normalize_editor_child_blocks(
                project_schema1_to_schema2(parent),
                parsed_blocks,
                ensure_parent_headings=True,
            )
        draft = ContentDraft(
            content_draft_id=f"draft_{uuid4().hex}",
            parent_draft_id=parent_draft_id,
            base_project_revision=project.revision,
            confirmed_by_user=False,
            brief_snapshot=brief,
            source_bindings=bindings,
            context_hash=calculated_hash,
            blocks=parsed_blocks,
            display_title=display_title,
            schema_version=2,
        )
        draft_path = _draft_path(store.project_path, draft.content_draft_id)
        write_new_json(draft_path, draft.to_dict())
        latest = store.load()
        if latest.revision != project.revision:
            draft_path.unlink(missing_ok=True)
            raise ProjectError("project revision conflict")
        return ContentDraftMutation(draft, project.revision, "current", (), True)


def create_content_draft_editor_child(
    project_path: Path,
    *,
    parent: ContentDraft,
    blocks: tuple[ContentDraftBlock, ...],
    expected_revision: int,
) -> ContentDraftMutation:
    """Persist an editor-derived child against an already validated immutable basis."""
    _validate_revision(expected_revision)
    store = ProjectStore(project_path)
    with project_write_lock(store.project_path):
        project = store.load()
        _validate_cached_editor_basis(project, parent, expected_revision)
        child = prepare_content_draft_editor_child(
            parent=parent,
            blocks=blocks,
            child_id=f"draft_{uuid4().hex}",
        )
        child_path = _draft_path(store.project_path, child.content_draft_id)
        write_new_json(child_path, child.to_dict())
        latest = store.load()
        if latest.revision != expected_revision:
            child_path.unlink(missing_ok=True)
            raise ProjectError("project revision conflict")
        return ContentDraftMutation(child, expected_revision, "current", (), True)


def prepare_content_draft_editor_child(
    *,
    parent: ContentDraft,
    blocks: tuple[ContentDraftBlock, ...],
    child_id: str,
    display_ownership_resolved: bool = False,
) -> ContentDraft:
    """Construct one editor child without writing Project or artifact state."""

    projected_parent = project_schema1_to_schema2(parent)
    normalized_blocks = _normalize_editor_child_blocks(
        projected_parent,
        blocks,
        inherit_source_display=not display_ownership_resolved,
    )
    return ContentDraft(
        content_draft_id=child_id,
        parent_draft_id=parent.content_draft_id,
        base_project_revision=projected_parent.base_project_revision,
        confirmed_by_user=False,
        brief_snapshot=projected_parent.brief_snapshot,
        source_bindings=projected_parent.source_bindings,
        context_hash=projected_parent.context_hash,
        blocks=normalized_blocks,
        display_title=projected_parent.display_title,
        schema_version=2,
    )


def publish_prepared_content_draft_editor_child(
    project_path: Path,
    *,
    parent: ContentDraft,
    child: ContentDraft,
    expected_revision: int,
    display_ownership_resolved: bool = False,
) -> ContentDraftMutation:
    """Publish the exact child returned by the pure editor prepare seam."""

    _validate_revision(expected_revision)
    store = ProjectStore(project_path)
    with project_write_lock(store.project_path):
        project = store.load()
        _validate_cached_editor_basis(project, parent, expected_revision)
        expected = prepare_content_draft_editor_child(
            parent=parent,
            blocks=child.blocks,
            child_id=child.content_draft_id,
            display_ownership_resolved=display_ownership_resolved,
        )
        if child != expected:
            raise ProjectError("prepared draft editor child basis does not match")
        child_path = _draft_path(store.project_path, child.content_draft_id)
        write_new_json(child_path, child.to_dict())
        latest = store.load()
        if latest.revision != expected_revision:
            child_path.unlink(missing_ok=True)
            raise ProjectError("project revision conflict")
        return ContentDraftMutation(child, expected_revision, "current", (), True)


def read_content_draft_editor_child(
    project_path: Path,
    content_draft_id: str,
    *,
    parent: ContentDraft,
    expected_revision: int,
) -> ContentDraftState:
    """Read back one editor child without rebuilding immutable Transcript context."""
    _validate_revision(expected_revision)
    store = ProjectStore(project_path)
    project = store.load()
    _validate_cached_editor_basis(project, parent, expected_revision)
    child = _read_draft(
        store.project_path,
        content_draft_id,
        validate_parent=False,
    )
    if child.parent_draft_id != parent.content_draft_id:
        raise ProjectError("draft editor child parent does not match")
    if child.confirmed_by_user:
        raise ProjectError("draft editor child must be unconfirmed")
    if (
        child.base_project_revision != parent.base_project_revision
        or child.brief_snapshot != parent.brief_snapshot
        or child.source_bindings != parent.source_bindings
        or child.context_hash != parent.context_hash
        or child.display_title != parent.display_title
    ):
        raise ProjectError("draft editor child basis does not match")
    return ContentDraftState(child, project.revision, "current", ())


def revise_content_draft_scoped(
    project_path: Path,
    *,
    parent_draft_id: str,
    mutable_block_ids: list[str],
    blocks: list[dict[str, Any]],
    expected_revision: int,
) -> ScopedContentDraftRevision:
    _validate_revision(expected_revision)
    _validate_id(parent_draft_id, "parent_draft_id")
    if not isinstance(mutable_block_ids, list) or not mutable_block_ids:
        raise ProjectError("scoped revision requires mutable block IDs")
    if any(not isinstance(block_id, str) for block_id in mutable_block_ids):
        raise ProjectError("scoped revision mutable block ID is invalid")
    if len(mutable_block_ids) != len(set(mutable_block_ids)):
        raise ProjectError("scoped revision mutable block IDs must be unique")
    parent_state = read_content_draft(project_path, parent_draft_id)
    parent = parent_state.content_draft
    context_hash = parent.context_hash
    if parent_state.status != "current":
        project = ProjectStore(project_path).load()
        allowed_post_adoption_rebase = (
            project.revision == expected_revision
            and parent.confirmed_by_user
            and project.active_content_draft_id == parent.content_draft_id
            and project.active_edit_version_id is not None
            and project.revision == parent.base_project_revision + 1
            and set(parent_state.stale_reasons)
            <= {"project_revision", "context_hash"}
            and "project_revision" in parent_state.stale_reasons
        )
        if not allowed_post_adoption_rebase:
            raise ProjectError("scoped revision parent Content Draft is stale")
        _validate_decision_binding_scope(
            project_path,
            project,
            parent.source_bindings,
        )
        brief = _read_active_brief(
            project_path,
            project,
            parent.brief_snapshot.brief_id,
        )
        context_hash = calculate_agent_context_hash(
            project_path,
            project=project,
            bindings=parent.source_bindings,
            brief=brief,
        )
    # Compare schema-1 parents in their immutable schema-2 projection.  The
    # projection removes the legacy embedded title field from ordinary blocks;
    # treating that field as a user edit would make every unchanged block look
    # changed on the first scoped child.
    comparison_parent = project_schema1_to_schema2(parent)
    parent_by_id = {block.block_id: block for block in comparison_parent.blocks}
    unknown_mutable = set(mutable_block_ids) - set(parent_by_id)
    if unknown_mutable:
        raise ProjectError("scoped revision mutable block does not exist")

    revision_blocks = blocks
    if parent.schema_version == 1:
        input_schema = 1 if any(
            isinstance(item, dict) and "section_title" in item for item in blocks
        ) else 2
        parsed_input_blocks = _parse_blocks(
            project=ProjectStore(project_path).load(),
            project_path=project_path,
            bindings=parent.source_bindings,
            blocks=blocks,
            require_active=True,
            schema_version=input_schema,
            allow_derived_canonical=True,
        )
        projected_parent = comparison_parent
        revision_blocks = [
            {
                key: value
                for key, value in block.to_dict(schema_version=2).items()
                if key != "display_text"
            }
            for block in _normalize_editor_child_blocks(projected_parent, parsed_input_blocks)
        ]
    created = create_content_draft(
        project_path,
        parent_draft_id=parent_draft_id,
        display_title=parent.display_title,
        source_bindings=[binding.to_dict() for binding in parent.source_bindings],
        brief_id=parent.brief_snapshot.brief_id,
        context_hash=context_hash,
        blocks=revision_blocks,
        expected_revision=expected_revision,
    )
    child = created.content_draft
    child_by_id = {block.block_id: block for block in child.blocks}
    mutable = set(mutable_block_ids)
    unchanged_ids = tuple(
        block.block_id
        for block in comparison_parent.blocks
        if block.block_id not in mutable
    )
    try:
        for block_id in unchanged_ids:
            if child_by_id.get(block_id) != parent_by_id[block_id]:
                raise ProjectError(
                    "scoped revision unspecified blocks must remain unchanged"
                )
        child_unchanged_order = tuple(
            block.block_id for block in child.blocks if block.block_id in unchanged_ids
        )
        if child_unchanged_order != unchanged_ids:
            raise ProjectError(
                "scoped revision unspecified block order must remain unchanged"
            )
        parent_positions = {
            block.block_id: index
            for index, block in enumerate(comparison_parent.blocks)
        }
        child_positions = {
            block.block_id: index for index, block in enumerate(child.blocks)
        }
        changed_ids = tuple(
            block_id
            for block_id in mutable_block_ids
            if (
                child_by_id.get(block_id) != parent_by_id[block_id]
                or child_positions.get(block_id) != parent_positions[block_id]
            )
        ) + tuple(
            block.block_id
            for block in child.blocks
            if block.block_id not in parent_by_id
        )
        if not changed_ids:
            raise ProjectError("scoped revision does not change the parent Content Draft")
    except Exception:
        discard_failed_content_draft_child(
            project_path,
            child.content_draft_id,
            expected_parent_draft_id=parent_draft_id,
        )
        raise
    return ScopedContentDraftRevision(
        child,
        created.project_revision,
        created.status,
        created.stale_reasons,
        created.changed,
        changed_ids,
        unchanged_ids,
        _content_draft_duration(parent),
        _content_draft_duration(child),
    )


def read_content_draft(project_path: Path, content_draft_id: str) -> ContentDraftState:
    _validate_id(content_draft_id, "content_draft_id")
    store = ProjectStore(project_path)
    project = store.load()
    draft = _read_draft(store.project_path, content_draft_id, validate_parent=True)
    _validate_stored_blocks(project, store.project_path, draft)
    reasons = _stale_reasons(project, store.project_path, draft)
    return ContentDraftState(
        draft,
        project.revision,
        "stale" if reasons else "current",
        reasons,
    )


def discard_failed_content_draft_child(
    project_path: Path,
    content_draft_id: str,
    *,
    expected_parent_draft_id: str,
) -> None:
    """Remove only a newly created, inactive candidate after downstream failure."""
    _validate_id(content_draft_id, "content_draft_id")
    _validate_id(expected_parent_draft_id, "expected_parent_draft_id")
    store = ProjectStore(project_path)
    project = store.load()
    draft = _read_draft(store.project_path, content_draft_id, validate_parent=False)
    if (
        draft.parent_draft_id != expected_parent_draft_id
        or draft.confirmed_by_user
        or project.active_content_draft_id == content_draft_id
    ):
        raise ProjectError("content draft child is not safe to discard")
    _draft_path(store.project_path, content_draft_id).unlink(missing_ok=True)


def confirm_content_draft(
    project_path: Path,
    content_draft_id: str,
    *,
    expected_revision: int,
) -> ContentDraftMutation:
    _validate_revision(expected_revision)
    store = ProjectStore(project_path)
    with project_write_lock(store.project_path):
        project = store.load()
        if project.revision != expected_revision:
            raise ProjectError("project revision conflict")
        state = read_content_draft(store.project_path, content_draft_id)
        if state.status != "current":
            raise ProjectError("content draft is stale")
        draft = state.content_draft
        if draft.confirmed_by_user:
            if project.active_content_draft_id != draft.content_draft_id:
                raise ProjectError("confirmed content draft is not active")
            return ContentDraftMutation(draft, project.revision, "current", (), False)
        updated_project, child = prepare_confirmed_content_draft(
            store.project_path,
            project,
            draft,
            child_id=f"draft_{uuid4().hex}",
            updated_at=datetime.now(UTC).isoformat(),
        )
        child_path = _draft_path(store.project_path, child.content_draft_id)
        write_new_json(child_path, child.to_dict())
        try:
            store.save(updated_project, expected_revision=expected_revision)
        except Exception:
            child_path.unlink(missing_ok=True)
            raise
        return ContentDraftMutation(child, updated_project.revision, "current", (), True)


def prepare_confirmed_content_draft(
    project_path: Path,
    project: Project,
    draft: ContentDraft,
    *,
    child_id: str,
    updated_at: str,
) -> tuple[Project, ContentDraft]:
    """Prepare the exact confirmed child and Project image without publishing."""

    _validate_id(child_id, "content_draft_id")
    updated_project = replace(
        project,
        revision=project.revision + 1,
        updated_at=updated_at,
        active_content_draft_id=child_id,
    )
    context_hash = calculate_agent_context_hash(
        project_path,
        project=updated_project,
        bindings=draft.source_bindings,
        brief=draft.brief_snapshot,
    )
    projected = project_schema1_to_schema2(draft)
    blocks = tuple(
        replace(block, status="approved")
        if isinstance(block, NarrationBlock) and block.status == "draft"
        else block
        for block in projected.blocks
    )
    return updated_project, ContentDraft(
        content_draft_id=child_id,
        parent_draft_id=draft.content_draft_id,
        base_project_revision=updated_project.revision,
        confirmed_by_user=True,
        brief_snapshot=projected.brief_snapshot,
        source_bindings=projected.source_bindings,
        context_hash=context_hash,
        blocks=blocks,
        display_title=projected.display_title,
        schema_version=2,
    )


def prepare_content_draft_proposal(
    project_path: Path,
    project: Project,
    draft: ContentDraft,
    *,
    proposal_id: str,
    created_at: str,
) -> ProposalLike:
    """Compile one exact Proposal candidate without publishing it."""

    _validate_id(proposal_id, "proposal_id")
    if not draft.confirmed_by_user:
        raise ProjectError("content draft must be confirmed")
    if any(
        isinstance(block, NarrationBlock) and block.status != "recorded"
        for block in draft.blocks
    ):
        raise ProjectError("content draft narration is not recorded")
    clips: list[EditClip] = []
    occurrences: dict[tuple[str, str, str], int] = {}
    for block in draft.blocks:
        if isinstance(block, SectionTitleBlock):
            continue
        refs = block.refs if isinstance(block, SourceExcerptBlock) else block.recorded_refs
        reason = (
            "Content Draft source excerpt"
            if isinstance(block, SourceExcerptBlock)
            else "Recorded Content Draft narration"
        )
        canonical_parts = [
            _canonical_ref_text(
                project,
                project_path,
                draft.source_bindings,
                ref,
                require_active=True,
            )
            for ref in refs
        ]
        display_parts = (
            split_display_text_for_canonical_parts(
                block.display_text or block.canonical_text,
                canonical_parts,
            )
            if isinstance(block, SourceExcerptBlock)
            else tuple(canonical_parts)
        )
        for ref_index, ref in enumerate(refs):
            identity = (ref.source_id, ref.transcript_version_id, ref.segment_id)
            occurrence = occurrences.get(identity, 0) + 1
            occurrences[identity] = occurrence
            clips.append(
                EditClip(
                    clip_id=_stable_clip_id(ref, occurrence),
                    source_id=ref.source_id,
                    transcript_version_id=ref.transcript_version_id,
                    segment_id=ref.segment_id,
                    source_in_ticks=ref.start_ticks,
                    source_out_ticks=ref.end_ticks,
                    reason=reason,
                    display_text=display_parts[ref_index],
                )
            )
    if not clips:
        raise ProjectError("content draft has no media refs")
    total_duration_ticks = sum(clip.duration_ticks for clip in clips)
    if len(draft.source_bindings) == 1:
        binding = draft.source_bindings[0]
        return EditProposal(
            proposal_id=proposal_id,
            base_project_revision=project.revision,
            base_edit_version_id=project.active_edit_version_id,
            source_id=binding.source_id,
            transcript_version_id=binding.transcript_version_id,
            brief_snapshot=draft.brief_snapshot,
            context_hash=draft.context_hash,
            clips=tuple(clips),
            total_duration_ticks=total_duration_ticks,
            created_at=created_at,
        )
    return MultiSourceEditProposal(
        proposal_id=proposal_id,
        base_project_revision=project.revision,
        base_edit_version_id=project.active_edit_version_id,
        source_bindings=draft.source_bindings,
        brief_snapshot=draft.brief_snapshot,
        context_hash=draft.context_hash,
        clips=tuple(clips),
        total_duration_ticks=total_duration_ticks,
        created_at=created_at,
    )


def propose_content_draft(
    project_path: Path,
    content_draft_id: str,
    *,
    expected_revision: int,
) -> ContentDraftProposalState:
    _validate_revision(expected_revision)
    store = ProjectStore(project_path)
    project = store.load()
    if project.revision != expected_revision:
        raise ProjectError("project revision conflict")
    state = read_content_draft(store.project_path, content_draft_id)
    draft = state.content_draft
    if (
        state.status != "current"
        or not draft.confirmed_by_user
        or project.active_content_draft_id != draft.content_draft_id
    ):
        raise ProjectError("content draft must be active, current and confirmed")
    proposal = prepare_content_draft_proposal(
        store.project_path,
        project,
        draft,
        proposal_id=f"proposal_{uuid4().hex}",
        created_at=datetime.now(UTC).isoformat(),
    )
    proposal_path = (
        store.project_path / "proposals" / f"{proposal.proposal_id}.json"
    )
    write_new_json(proposal_path, proposal.to_dict())
    latest = store.load()
    if (
        latest.revision != project.revision
        or latest.active_edit_version_id != project.active_edit_version_id
    ):
        proposal_path.unlink(missing_ok=True)
        raise ProjectError("project revision conflict")
    return ContentDraftProposalState(
        draft.content_draft_id,
        1 if isinstance(proposal, EditProposal) else 2,
        proposal,
        project.revision,
    )


def _parse_bindings(value: object) -> tuple[SourceTranscriptBinding, ...]:
    if not isinstance(value, list) or not value:
        raise ProjectError("content draft requires source bindings")
    bindings: list[SourceTranscriptBinding] = []
    for item in value:
        if not isinstance(item, dict):
            raise ProjectError("content draft binding must be an object")
        bindings.append(SourceTranscriptBinding.from_dict(item))
    source_ids = [binding.source_id for binding in bindings]
    if len(source_ids) != len(set(source_ids)):
        raise ProjectError("content draft bindings require unique source IDs")
    return tuple(bindings)


def _parse_blocks(
    *,
    project: Project,
    project_path: Path,
    bindings: tuple[SourceTranscriptBinding, ...],
    blocks: object,
    require_active: bool,
    schema_version: int = 2,
    allow_display_text: bool = False,
    allow_derived_canonical: bool = False,
) -> tuple[ContentDraftBlock, ...]:
    if not isinstance(blocks, list) or not blocks:
        raise ProjectError("content draft requires blocks")
    parsed: list[ContentDraftBlock] = []
    for item in blocks:
        if not isinstance(item, dict):
            raise ProjectError("content draft block must be an object")
        kind = item.get("kind")
        if kind == "source_excerpt":
            if schema_version == 2:
                expected_fields = {"block_id", "kind", "refs"}
                if "canonical_text" in item:
                    expected_fields.add("canonical_text")
                elif not allow_derived_canonical:
                    raise ProjectError("schema 2 source block fields are invalid")
                if allow_display_text and "display_text" in item:
                    expected_fields.add("display_text")
                if set(item) != expected_fields:
                    raise ProjectError("schema 2 source block fields are invalid")
            elif "display_text" in item:
                raise ProjectError("schema 1 source block cannot contain display_text")
            has_canonical = "canonical_text" in item
            source_block = (
                SourceExcerptBlock.from_dict(item, schema_version=schema_version)
                if has_canonical
                else None
            )
            refs = (
                source_block.refs
                if source_block is not None
                else tuple(ContentDraftRef.from_dict(ref) for ref in item["refs"])
            )
            _validate_ref_sequence(
                project,
                project_path,
                bindings,
                refs,
                require_active=require_active,
            )
            canonical = "\n".join(
                _canonical_ref_text(
                    project,
                    project_path,
                    bindings,
                    ref,
                    require_active=require_active,
                )
                for ref in refs
            )
            if source_block is None:
                source_block = SourceExcerptBlock.from_dict(
                    {**item, "canonical_text": canonical},
                    schema_version=schema_version,
                )
            elif source_block.canonical_text != canonical:
                raise ProjectError("content draft source canonical text does not match refs")
            parsed.append(replace(source_block, canonical_text=canonical))
        elif kind == "narration":
            if schema_version == 2 and set(item) != {
                "block_id", "kind", "text", "status", "recorded_refs"
            }:
                raise ProjectError("schema 2 narration block fields are invalid")
            narration_block = NarrationBlock.from_dict(item, schema_version=schema_version)
            if narration_block.status == "recorded":
                _validate_ref_sequence(
                    project,
                    project_path,
                    bindings,
                    narration_block.recorded_refs,
                    require_active=require_active,
                )
                canonical = "\n".join(
                    _canonical_ref_text(
                        project,
                        project_path,
                        bindings,
                        ref,
                        require_active=require_active,
                    )
                    for ref in narration_block.recorded_refs
                )
                if narration_block.text != canonical:
                    raise ProjectError("recorded narration text does not match canonical refs")
            parsed.append(narration_block)
        elif kind == "section_title" and schema_version == 2:
            if set(item) != {"block_id", "kind", "title"}:
                raise ProjectError("content draft section title fields are invalid")
            parsed.append(SectionTitleBlock.from_dict(item))
        else:
            raise ProjectError("content draft block kind is invalid")
    block_ids = [block.block_id for block in parsed]
    if len(block_ids) != len(set(block_ids)):
        raise ProjectError("content draft contains duplicate block IDs")
    return tuple(parsed)


def _normalize_editor_child_blocks(
    parent: ContentDraft,
    blocks: tuple[ContentDraftBlock, ...],
    *,
    ensure_parent_headings: bool = False,
    inherit_source_display: bool = True,
) -> tuple[ContentDraftBlock, ...]:
    """Ensure every editor child has one schema 2 heading representation."""
    result: list[ContentDraftBlock] = []
    parent_heading_by_block: dict[str, SectionTitleBlock] = {}
    previous_heading: SectionTitleBlock | None = None
    for item in parent.blocks:
        if isinstance(item, SectionTitleBlock):
            previous_heading = item
        else:
            if previous_heading is not None:
                parent_heading_by_block[item.block_id] = previous_heading
    parent_indices = {
        item.block_id: index for index, item in enumerate(parent.blocks)
    }
    candidate_ids = {item.block_id for item in blocks}
    explicit_heading_ids = {
        item.block_id for item in blocks if isinstance(item, SectionTitleBlock)
    }
    for heading in parent_heading_by_block.values():
        if heading.block_id in candidate_ids and heading.block_id not in explicit_heading_ids:
            raise ProjectError("content draft editor child has a section identity collision")
    seen_ids: set[str] = set()
    for item in blocks:
        if isinstance(item, SectionTitleBlock):
            if item.block_id in seen_ids:
                raise ProjectError("content draft editor child has duplicate block IDs")
            result.append(item)
            seen_ids.add(item.block_id)
            continue
        if ensure_parent_headings:
            existing_heading = parent_heading_by_block.get(item.block_id)
            if existing_heading is not None and existing_heading.block_id not in explicit_heading_ids:
                if existing_heading.block_id in seen_ids:
                    raise ProjectError("content draft editor child has a section identity collision")
                result.append(existing_heading)
                seen_ids.add(existing_heading.block_id)
        title = getattr(item, "section_title", None)
        if title is not None:
            inherited_heading = parent_heading_by_block.get(item.block_id)
            inherited = inherited_heading is not None
            if inherited_heading is None:
                inherited_heading = SectionTitleBlock(
                    block_id=legacy_section_block_id(
                        parent.content_draft_id,
                        item.block_id,
                        parent_indices.get(item.block_id, len(parent_indices)),
                    ),
                    title=title,
                )
                if inherited_heading.block_id in candidate_ids:
                    raise ProjectError("content draft editor child has a section identity collision")
            if inherited_heading.block_id not in seen_ids:
                result.append(inherited_heading)
                seen_ids.add(inherited_heading.block_id)
            elif not inherited:
                raise ProjectError("content draft editor child has a section identity collision")
        if isinstance(item, SourceExcerptBlock):
            clean: ContentDraftBlock = SourceExcerptBlock(
                block_id=item.block_id,
                refs=item.refs,
                canonical_text=item.canonical_text,
                display_text=(
                    _inherited_source_display(parent, item)
                    if inherit_source_display
                    else item.display_text
                ),
            )
        elif isinstance(item, NarrationBlock):
            clean = NarrationBlock(
                block_id=item.block_id,
                text=item.text,
                status=item.status,
                recorded_refs=item.recorded_refs,
            )
        else:
            raise ProjectError("content draft editor child block kind is invalid")
        result.append(clean)
        seen_ids.add(clean.block_id)
    return tuple(result)


def _inherited_source_display(
    parent: ContentDraft,
    item: SourceExcerptBlock,
) -> str | None:
    """Carry parent punctuation only when the source mapping is unique."""
    if item.display_text is not None:
        return item.display_text
    parent_sources = [
        block for block in project_schema1_to_schema2(parent).blocks
        if isinstance(block, SourceExcerptBlock)
    ]
    exact = [
        block
        for block in parent_sources
        if block.block_id == item.block_id
        and block.refs == item.refs
        and block.canonical_text == item.canonical_text
    ]
    if len(exact) == 1:
        return exact[0].display_text
    same_block = [
        block
        for block in parent_sources
        if block.refs == item.refs and block.canonical_text == item.canonical_text
    ]
    if len(same_block) == 1:
        return same_block[0].display_text
    child_parts = tuple(item.canonical_text.split("\n"))
    if len(child_parts) != len(item.refs):
        raise ProjectError("content draft source display inheritance is ambiguous")
    relevant_blocks: list[tuple[SourceExcerptBlock, tuple[int, ...], tuple[str, ...]]] = []
    for block in parent_sources:
        if block.display_text is None:
            continue
        canonical_parts = [
            _parent_ref_canonical_part(block, ref_index)
            for ref_index in range(len(block.refs))
        ]
        display_parts = split_display_text_for_canonical_parts(
            block.display_text or block.canonical_text,
            canonical_parts,
        )
        candidate_indices: list[tuple[int, ...]] = []
        for ref in item.refs:
            candidates = tuple(
                index
                for index, parent_ref in enumerate(block.refs)
                if (
                    parent_ref.source_id == ref.source_id
                    and parent_ref.transcript_version_id == ref.transcript_version_id
                    and parent_ref.segment_id == ref.segment_id
                    and parent_ref.start_ticks <= ref.start_ticks
                    and ref.end_ticks <= parent_ref.end_ticks
                )
            )
            candidate_indices.append(candidates)
        if not any(candidate_indices):
            continue
        if any(len(candidates) != 1 for candidates in candidate_indices):
            raise ProjectError("content draft source display inheritance is ambiguous")
        indices = tuple(candidates[0] for candidates in candidate_indices)
        if indices != tuple(range(indices[0], indices[0] + len(indices))):
            raise ProjectError("content draft source display inheritance is ambiguous")
        relevant_blocks.append((block, indices, display_parts))
    if len(relevant_blocks) > 1 and any(
        block.display_text is not None for block, _indices, _display_parts in relevant_blocks
    ):
        raise ProjectError("content draft source display inheritance is ambiguous")
    matches: list[str] = []
    for block, indices, display_parts in relevant_blocks:
        if block.display_text is None:
            continue
        mapped_parts = tuple(
            _inherit_display_subrange(
                display_parts[index],
                canonical_parts[index],
                child_parts[child_index],
            )
            for child_index, index in enumerate(indices)
        )
        matches.append("\n".join(mapped_parts))
    if len(matches) > 1:
        raise ProjectError("content draft source display inheritance is ambiguous")
    return matches[0] if matches else None


def _inherit_display_subrange(
    display_text: str,
    parent_canonical: str,
    child_canonical: str,
) -> str:
    if child_canonical == parent_canonical:
        return display_text
    starts = tuple(
        index
        for index in range(len(parent_canonical) - len(child_canonical) + 1)
        if parent_canonical.startswith(child_canonical, index)
    )
    if len(starts) != 1:
        raise ProjectError("content draft source display inheritance is ambiguous")
    start = starts[0]
    end = start + len(child_canonical)
    pieces: list[str] = []
    if start:
        pieces.append(parent_canonical[:start])
    selected_index = len(pieces)
    pieces.append(child_canonical)
    if end < len(parent_canonical):
        pieces.append(parent_canonical[end:])
    display_pieces = split_display_text_for_canonical_parts(
        display_text,
        pieces,
        connection="",
    )
    return display_pieces[selected_index]


def _parent_ref_canonical_part(block: SourceExcerptBlock, ref_index: int) -> str:
    parts = block.canonical_text.split("\n")
    if len(parts) != len(block.refs):
        raise ProjectError("content draft source canonical connection is invalid")
    return parts[ref_index]


def _validate_ref_sequence(
    project: Project,
    project_path: Path,
    bindings: tuple[SourceTranscriptBinding, ...],
    refs: tuple[ContentDraftRef, ...],
    *,
    require_active: bool,
) -> None:
    identities = {(ref.source_id, ref.transcript_version_id) for ref in refs}
    if len(identities) != 1:
        raise ProjectError("content draft block refs must use one Transcript")
    source_id, transcript_version_id = next(iter(identities))
    if (source_id, transcript_version_id) not in {
        (binding.source_id, binding.transcript_version_id) for binding in bindings
    }:
        raise ProjectError("content draft ref is outside source bindings")
    if require_active and project.active_transcript_versions.get(source_id) != (
        transcript_version_id
    ):
        raise ProjectError("content draft ref Transcript is not active")

    transcript = _read_transcript(project_path, source_id, transcript_version_id)
    segments = {segment.segment_id: segment for segment in transcript.segments}
    positions = {
        segment.segment_id: index for index, segment in enumerate(transcript.segments)
    }
    ref_positions: list[int] = []
    for ref_index, ref in enumerate(refs):
        segment = segments.get(ref.segment_id)
        if segment is None:
            raise ProjectError("content draft ref segment does not exist")
        canonical_text_for_range(segment, ref.start_ticks, ref.end_ticks)
        if ref.start_ticks != segment.start_ticks or ref.end_ticks != segment.end_ticks:
            spans = exact_fine_unit_spans(segment) or trusted_fine_unit_spans(segment)
            start_index = (
                0
                if ref.start_ticks == segment.start_ticks
                else next(
                    (
                        index
                        for index, span in enumerate(spans or ())
                        if span.unit.start_ticks == ref.start_ticks
                    ),
                    None,
                )
            )
            end_index = (
                len(spans or ()) - 1
                if ref.end_ticks == segment.end_ticks
                else next(
                    (
                        index
                        for index, span in enumerate(spans or ())
                        if span.unit.end_ticks == ref.end_ticks
                    ),
                    None,
                )
            )
            if start_index is None or end_index is None or end_index < start_index:
                raise ProjectError(
                    "content draft partial ref requires trusted fine-unit boundaries"
                )
            if len(refs) > 1 and (
                (ref_index == 0 and ref.end_ticks != segment.end_ticks)
                or (ref_index == len(refs) - 1 and ref.start_ticks != segment.start_ticks)
                or (0 < ref_index < len(refs) - 1)
            ):
                raise ProjectError("content draft block refs must not skip segment text")
        ref_positions.append(positions[ref.segment_id])
    if ref_positions != list(
        range(ref_positions[0], ref_positions[0] + len(ref_positions))
    ):
        raise ProjectError("content draft block refs must be consecutive and ordered")


def _stable_clip_id(ref: ContentDraftRef, occurrence: int) -> str:
    identity = (
        f"{ref.source_id}\0{ref.transcript_version_id}\0{ref.segment_id}"
    ).encode()
    digest = hashlib.sha256(identity).hexdigest()[:20]
    return f"clip_ref_{digest}_{occurrence}"


def _content_draft_duration(draft: ContentDraft) -> int:
    return sum(
        ref.end_ticks - ref.start_ticks
        for block in draft.blocks
        if not isinstance(block, SectionTitleBlock)
        for ref in (
            block.refs
            if isinstance(block, SourceExcerptBlock)
            else block.recorded_refs
        )
    )


def duration_acceptance_summary(draft: ContentDraft) -> dict[str, object]:
    """Return deterministic duration acceptance facts for one immutable draft."""
    target_duration_ticks = draft.brief_snapshot.target_duration_ticks
    actual_duration_ticks = _content_draft_duration(draft)
    delta_ticks = actual_duration_ticks - target_duration_ticks
    tolerance_ticks = (
        target_duration_ticks * DURATION_ACCEPTANCE_TOLERANCE_PERCENT
    ) // 100
    accepted_upper_bound_ticks = target_duration_ticks + tolerance_ticks
    if actual_duration_ticks < target_duration_ticks:
        status = "under_target"
    elif actual_duration_ticks > accepted_upper_bound_ticks:
        status = "over_target"
    else:
        status = "within_target"
    return {
        "target_duration_ticks": target_duration_ticks,
        "actual_duration_ticks": actual_duration_ticks,
        "delta_ticks": delta_ticks,
        "status": status,
        "tolerance_ticks": tolerance_ticks,
        "accepted_upper_bound_ticks": accepted_upper_bound_ticks,
    }


def _validate_stored_blocks(project: Project, project_path: Path, draft: ContentDraft) -> None:
    _parse_blocks(
        project=project,
        project_path=project_path,
        bindings=draft.source_bindings,
        blocks=[block.to_dict(schema_version=draft.schema_version) for block in draft.blocks],
        require_active=False,
        schema_version=draft.schema_version,
        allow_display_text=True,
    )


def _canonical_ref_text(
    project: Project,
    project_path: Path,
    bindings: tuple[SourceTranscriptBinding, ...],
    ref: ContentDraftRef,
    *,
    require_active: bool,
) -> str:
    if (ref.source_id, ref.transcript_version_id) not in {
        (binding.source_id, binding.transcript_version_id) for binding in bindings
    }:
        raise ProjectError("content draft ref is outside source bindings")
    if require_active and project.active_transcript_versions.get(ref.source_id) != (
        ref.transcript_version_id
    ):
        raise ProjectError("content draft ref Transcript is not active")
    source = _find_source(project, ref.source_id)
    transcript = _read_transcript(project_path, ref.source_id, ref.transcript_version_id)
    segment = next(
        (item for item in transcript.segments if item.segment_id == ref.segment_id), None
    )
    if segment is None:
        raise ProjectError("content draft ref segment does not exist")
    if ref.end_ticks > source.probe.duration_ticks:
        raise ProjectError("content draft ref exceeds source duration")
    return canonical_text_for_range(segment, ref.start_ticks, ref.end_ticks)


def _stale_reasons(
    project: Project, project_path: Path, draft: ContentDraft
) -> tuple[str, ...]:
    reasons: list[str] = []
    if draft.base_project_revision != project.revision:
        reasons.append("project_revision")
    brief: EditBrief | None = None
    if project.active_brief_id != draft.brief_snapshot.brief_id:
        reasons.append("brief")
    else:
        try:
            brief = _read_active_brief(
                project_path, project, draft.brief_snapshot.brief_id
            )
            if brief != draft.brief_snapshot:
                reasons.append("brief")
        except ProjectError:
            reasons.append("brief")
    active_bindings = all(
        project.active_transcript_versions.get(binding.source_id)
        == binding.transcript_version_id
        for binding in draft.source_bindings
    )
    if not active_bindings:
        reasons.append("active_transcript")
    if brief is not None and active_bindings:
        try:
            current_hash = calculate_agent_context_hash(
                project_path,
                project=project,
                bindings=draft.source_bindings,
                brief=brief,
            )
            if current_hash != draft.context_hash:
                reasons.append("context_hash")
        except ProjectError:
            reasons.append("source_bindings")
    if draft.confirmed_by_user and project.active_content_draft_id != draft.content_draft_id:
        reasons.append("active_content_draft")
    return tuple(dict.fromkeys(reasons))


def _validate_cached_editor_basis(
    project: Project,
    parent: ContentDraft,
    expected_revision: int,
) -> None:
    if project.revision != expected_revision:
        raise ProjectError("project revision conflict")
    if parent.base_project_revision != expected_revision:
        raise ProjectError("draft editor parent revision is stale")
    if project.active_brief_id != parent.brief_snapshot.brief_id:
        raise ProjectError("draft editor Brief is stale")
    if any(
        project.active_transcript_versions.get(binding.source_id)
        != binding.transcript_version_id
        for binding in parent.source_bindings
    ):
        raise ProjectError("draft editor Transcript binding is stale")
    if (
        parent.confirmed_by_user
        and project.active_content_draft_id != parent.content_draft_id
    ):
        raise ProjectError("draft editor confirmed parent is not active")


def _validate_decision_binding_scope(
    project_path: Path,
    project: Project,
    bindings: tuple[SourceTranscriptBinding, ...],
) -> None:
    if project.active_edit_version_id is None:
        return
    data = read_json_object(
        project_path / "edits" / f"{project.active_edit_version_id}.json",
        description="active edit decision",
    )
    frozen: tuple[SourceTranscriptBinding, ...]
    if data.get("schema_version") == 1:
        single_decision = EditDecision.from_dict(data)
        proposal = single_decision.proposal_snapshot
        frozen = (
            SourceTranscriptBinding(proposal.source_id, proposal.transcript_version_id),
        )
    elif data.get("schema_version") == 2:
        multi_decision = MultiSourceEditDecision.from_dict(data)
        frozen = multi_decision.proposal_snapshot.source_bindings
    else:
        raise ProjectError("unsupported active edit decision schema")
    if frozen != bindings:
        raise ProjectError(
            "content draft cannot change bindings after an Edit Decision; create a rebaseline task"
        )


def _read_active_brief(
    project_path: Path, project: Project, brief_id: str
) -> EditBrief:
    if project.active_brief_id != brief_id:
        raise ProjectError("content draft Brief is not active")
    data = read_json_object(
        project_path / "briefs" / f"{brief_id}.json",
        description="content draft Brief",
    )
    brief = EditBrief.from_dict(data)
    if brief.brief_id != brief_id:
        raise ProjectError("content draft Brief identity mismatch")
    return brief


def _read_draft(
    project_path: Path,
    content_draft_id: str,
    *,
    validate_parent: bool,
) -> ContentDraft:
    _validate_id(content_draft_id, "content_draft_id")
    data = read_json_object(
        _draft_path(project_path, content_draft_id),
        description="content draft",
    )
    draft = ContentDraft.from_dict(data)
    if draft.content_draft_id != content_draft_id:
        raise ProjectError("content draft identity mismatch")
    if validate_parent and draft.parent_draft_id is not None:
        parent_data = read_json_object(
            _draft_path(project_path, draft.parent_draft_id),
            description="content draft parent",
        )
        parent = ContentDraft.from_dict(parent_data)
        if parent.content_draft_id != draft.parent_draft_id:
            raise ProjectError("content draft parent identity mismatch")
    return draft


def _read_transcript(
    project_path: Path, source_id: str, transcript_version_id: str
) -> TimedTranscript:
    data = read_json_object(
        project_path
        / "transcripts"
        / source_id
        / f"{transcript_version_id}.json",
        description="content draft Transcript",
    )
    transcript = TimedTranscript.from_dict(data)
    if (
        transcript.source_id != source_id
        or transcript.transcript_version_id != transcript_version_id
    ):
        raise ProjectError("content draft Transcript identity mismatch")
    return transcript


def _find_source(project: Project, source_id: str) -> SourceAsset:
    for source in project.sources:
        if source.source_id == source_id:
            return source
    raise ProjectError("content draft source is not part of the project")


def _draft_path(project_path: Path, content_draft_id: str) -> Path:
    return project_path / "content-drafts" / f"{content_draft_id}.json"


def _validate_id(value: str, name: str) -> None:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ProjectError(f"content draft {name} is invalid")


def _validate_revision(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProjectError("content draft expected revision is invalid")
