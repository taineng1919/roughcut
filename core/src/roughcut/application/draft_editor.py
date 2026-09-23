"""Review-only initial-draft snapshots and immutable candidate editing."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

from roughcut.adapters.project_store import ProjectStore
from roughcut.application.agent_context import _read_transcript
from roughcut.application.content_drafts import (
    ContentDraftMutation,
    ContentDraftState,
    duration_acceptance_summary,
    prepare_content_draft_editor_child,
    publish_prepared_content_draft_editor_child,
)
from roughcut.application.preview import ReviewPlaybackSelection
from roughcut.application.readable_transcripts import (
    _ViewState,
    build_readable_transcript_view,
    insert_transcript_selection_at_caret,
    make_exact_sequence_caret,
    move_draft_selection_to_caret,
    numbered_readable_paragraphs,
    resolve_continuous_transcript_selection_from_view,
    resolve_transcript_caret_from_view,
)
from roughcut.application.workflow_review import (
    WorkflowReviewSnapshot,
    load_workflow_review_snapshot,
)
from roughcut.domain.content_draft import (
    ContentDraft,
    ContentDraftRef,
    NarrationBlock,
    SectionTitleBlock,
    SourceExcerptBlock,
    legacy_section_block_id,
    project_schema1_to_schema2,
    punctuation_stripped,
    split_display_text_for_canonical_parts,
    validate_punctuation_replacement,
)
from roughcut.domain.project import Project, ProjectError
from roughcut.domain.readable_transcript import (
    CaretNeighbor,
    CaretPosition,
    ContinuousSelectionResolution,
    ResolvedSelectionRef,
    canonical_text_for_range,
    codepoint_to_utf16_offset,
    exact_fine_unit_spans,
    trusted_fine_unit_spans,
    utf16_to_codepoint_offset,
)
from roughcut.domain.transcript import TimedTranscript, TranscriptSegment
from roughcut.domain.workflow import ArtifactRef, canonical_sha256_v1

DRAFT_EDITOR_SCHEMA_VERSION = 1
_MAX_PAGE_SIZE = 200
_MAX_SEARCH_LIMIT = 200
_EDIT_PARAGRAPH_BLOCK = re.compile(
    r"^block_edit_p_([0-9a-f]{24})_r_[0-9a-f]{16}$"
)


@dataclass(frozen=True)
class _SourceItem:
    block_id: str
    ref: ResolvedSelectionRef
    original_paragraph_id: str
    original_start_offset: int
    original_end_offset: int
    source_display_name: str
    person_key: tuple[str, ...]
    person_id: str | None
    person_name: str | None
    person_role: str | None
    local_speaker_id: str | None
    section_title: str | None
    section_origin: str | None
    paragraph_key: tuple[str, ...]
    display_section_title: bool
    display_text: str


@dataclass(frozen=True)
class _NarrationItem:
    block: NarrationBlock
    section_title: str | None = None
    section_origin: str | None = None
    paragraph_key: tuple[str, ...] = ()
    display_section_title: bool = False


_DraftItem = _SourceItem | _NarrationItem


@dataclass(frozen=True)
class _DraftSpan:
    paragraph_id: str
    start_offset: int
    end_offset: int
    item_index: int
    source: _SourceItem | None


@dataclass(frozen=True)
class DraftEditorCacheKey:
    project_revision: int
    source_bindings: tuple[tuple[str, str], ...]
    candidate_id: str


@dataclass(frozen=True)
class DraftEditorTranscriptBase:
    project_revision: int
    source_bindings: tuple[tuple[str, str], ...]
    transcript_paragraphs: tuple[dict[str, object], ...]
    view_hash: str
    transcript_view: _ViewState
    transcripts: dict[tuple[str, str], TimedTranscript]
    paragraph_index: dict[tuple[str, str, str], tuple[str, int]]
    project: Project
    source_display_names: dict[str, str]


@dataclass(frozen=True)
class DraftEditorSnapshot:
    workflow: WorkflowReviewSnapshot
    candidate: ContentDraft
    paragraphs: tuple[dict[str, object], ...]
    transcript_paragraphs: tuple[dict[str, object], ...]
    sources: tuple[dict[str, object], ...]
    _view_hash: str
    _transcript_view: _ViewState
    _items: tuple[_DraftItem, ...]
    _spans: tuple[_DraftSpan, ...]
    _transcript_base: DraftEditorTranscriptBase

    @property
    def cache_key(self) -> DraftEditorCacheKey:
        return DraftEditorCacheKey(
            self.workflow.project_revision,
            tuple(
                (binding.source_id, binding.transcript_version_id)
                for binding in self.workflow.source_bindings
            ),
            self.candidate.content_draft_id,
        )

    @property
    def authorized_source_ids(self) -> frozenset[str]:
        return self.workflow.authorized_source_ids

    @property
    def transcript_base(self) -> DraftEditorTranscriptBase:
        return self._transcript_base

    def playback_for(self, source_id: str) -> ReviewPlaybackSelection:
        return self.workflow.playback_for(source_id)

    def to_dict(self) -> dict[str, object]:
        brief = self.candidate.brief_snapshot
        return {
            "editor_schema_version": DRAFT_EDITOR_SCHEMA_VERSION,
            "review_mode": "draft_editor",
            "project": {"name": self.workflow.project_name},
            "brief": {
                "theme": brief.theme,
                "target_duration_ticks": brief.target_duration_ticks,
                "focus": list(brief.focus),
                "allow_reorder": brief.allow_reorder,
            },
            "duration_acceptance": duration_acceptance_summary(self.candidate),
            "candidate": {
                "candidate_id": self.candidate.content_draft_id,
                "parent_candidate_id": self.candidate.parent_draft_id,
                "display_title": self.candidate.display_title,
                "confirmed_by_user": self.candidate.confirmed_by_user,
                "has_unrecorded_narration": any(
                    isinstance(block, NarrationBlock) and block.status != "recorded"
                    for block in self.candidate.blocks
                ),
            },
            "paragraphs": list(self.paragraphs),
            "blocks": [
                block.to_dict(schema_version=self.candidate.schema_version)
                for block in self.candidate.blocks
            ],
            "sections": [
                {
                    "heading_block_id": block.block_id,
                    "title": block.title,
                }
                for block in self.candidate.blocks
                if isinstance(block, SectionTitleBlock)
            ],
            "sources": list(self.sources),
            "transcript_browser": {
                "read_endpoint": "/api/workflow/readable-transcript",
                "window_endpoint": "/api/workflow/draft-transcript-window",
                "search_endpoint": "/api/workflow/draft-search",
                "selection_endpoint": "/api/workflow/draft-selection-resolve",
                "pagination": {"offset_unit": "paragraph", "max_limit": _MAX_PAGE_SIZE},
            },
            "edit_endpoints": {
                "caret": "/api/workflow/draft-caret-resolve",
                "apply": "/api/workflow/draft-edit",
                "narration": "/api/workflow/draft-narration",
                "undo": "/api/workflow/draft-undo",
                "redo": "/api/workflow/draft-redo",
                "confirm": "/api/workflow/content-draft-confirm",
            },
        }


@dataclass(frozen=True)
class DraftEditorSelection:
    candidate_id: str
    surface: Literal["draft", "source"]
    resolution: ContinuousSelectionResolution | None
    item_start: int | None
    item_end: int | None
    person_key: tuple[str, ...]
    correspondence_groups: tuple[dict[str, object], ...]
    display_anchor: dict[str, object]
    display_focus: dict[str, object]
    narration_block_id: str | None = None
    narration_text: str | None = None
    narration_status: str | None = None
    narration_recorded_refs: tuple[ContentDraftRef, ...] = ()
    resolved_display_range: tuple[dict[str, object], dict[str, object]] | None = None

    def to_dict(self) -> dict[str, object]:
        resolution = self.resolution
        resolution_payload = (
            {
                "direction": "forward",
                "canonical_text": self.narration_text or "",
                "refs": [ref.to_dict() for ref in self.narration_recorded_refs],
                "start_caret": None,
                "end_caret": None,
                "adjusted": False,
                "degraded": False,
                "degradation_reasons": [],
            }
            if resolution is None
            else {
                "direction": resolution.direction,
                "canonical_text": resolution.canonical_text,
                "refs": [ref.to_dict() for ref in resolution.refs],
                "start_caret": _safe_source_caret(resolution.start_caret),
                "end_caret": _safe_source_caret(resolution.end_caret),
                "adjusted": resolution.adjusted,
                "degraded": resolution.degraded,
                "degradation_reasons": list(resolution.degradation_reasons),
            }
        )
        resolved_display_range = self.resolved_display_range or (
            self.display_anchor,
            self.display_focus,
        )
        return {
            "candidate_id": self.candidate_id,
            "surface": self.surface,
            "resolution": resolution_payload,
            "display_range": {
                "anchor": self.display_anchor,
                "focus": self.display_focus,
            },
            "resolved_display_range": {
                "anchor": resolved_display_range[0],
                "focus": resolved_display_range[1],
            },
            "correspondence_groups": list(self.correspondence_groups),
            "narration_block_id": self.narration_block_id,
            "narration_text": self.narration_text,
            "narration_status": self.narration_status,
        }


@dataclass(frozen=True)
class DraftEditorCaret:
    candidate_id: str
    paragraph_id: str
    character_offset: int
    utf16_offset: int
    boundary_id: str
    document_index: int
    source_caret: CaretPosition | None

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "paragraph_id": self.paragraph_id,
            "character_offset": self.character_offset,
            "utf16_offset": self.utf16_offset,
            "boundary_id": self.boundary_id,
            "degraded": (
                self.source_caret.degraded if self.source_caret is not None else False
            ),
            "degradation_reason": (
                self.source_caret.degradation_reason
                if self.source_caret is not None
                else None
            ),
        }


@dataclass(frozen=True)
class DraftSearchPage:
    surface: Literal["draft", "source"]
    query: str
    offset: int
    limit: int
    total: int
    next_cursor: int | None
    matches: tuple[dict[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "surface": self.surface,
            "query": self.query,
            "offset": self.offset,
            "limit": self.limit,
            "total": self.total,
            "next_cursor": self.next_cursor,
            "matches": list(self.matches),
        }


@dataclass(frozen=True)
class DraftTranscriptWindow:
    candidate_id: str
    source_id: str
    offset: int
    limit: int
    total: int
    next_cursor: int | None
    previous_cursor: int | None
    located_paragraph_id: str | None
    paragraphs: tuple[dict[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "source_id": self.source_id,
            "offset": self.offset,
            "limit": self.limit,
            "total": self.total,
            "next_cursor": self.next_cursor,
            "previous_cursor": self.previous_cursor,
            "located_paragraph_id": self.located_paragraph_id,
            "paragraphs": list(self.paragraphs),
        }


@dataclass(frozen=True)
class _WorkingAtom:
    ref: ResolvedSelectionRef | None
    narration: NarrationBlock | None
    origin_item_index: int
    section_title: str | None = None
    section_origin: str | None = None
    paragraph_key: tuple[str, ...] = ()
    display_text: str | None = None
    placement_marker: bool = False


@dataclass(frozen=True)
class DraftEditorPlacementResult:
    """Request-local identity for the exact source blocks being placed."""

    operation: Literal["move_selection", "insert_source_refs"]
    candidate_id: str
    block_ids: tuple[str, ...]
    display_range: tuple[dict[str, object], dict[str, object]] | None = None


@dataclass(frozen=True)
class DraftEditorPreparedChild:
    child: ContentDraft
    placement: DraftEditorPlacementResult | None


@dataclass(frozen=True)
class _TrustedBoundaryPair:
    left_end_ticks: int | None
    right_start_ticks: int | None


@dataclass(frozen=True)
class _CorrespondenceFragment:
    source_id: str
    source_display_name: str
    paragraph_id: str
    person_key: tuple[str, ...]
    start_offset: int
    end_offset: int


def load_draft_editor_snapshot(
    project_path: Path,
    *,
    source_bindings: list[dict[str, object]],
    content_draft_id: str,
    playback_selections: tuple[ReviewPlaybackSelection, ...] | None = None,
) -> DraftEditorSnapshot:
    workflow = load_workflow_review_snapshot(
        project_path,
        source_bindings=source_bindings,
        content_draft_id=content_draft_id,
        playback_selections=playback_selections,
    )
    return load_draft_editor_snapshot_from_workflow(
        workflow,
        content_draft_id=content_draft_id,
    )


def load_draft_editor_snapshot_from_workflow(
    workflow: WorkflowReviewSnapshot,
    *,
    content_draft_id: str,
    transcript_base: DraftEditorTranscriptBase | None = None,
) -> DraftEditorSnapshot:
    if workflow.brief is None or workflow.content_draft is None:
        raise ProjectError("draft editor requires a Brief and Content Draft")
    state = workflow.content_draft
    if state.status != "current":
        raise ProjectError("draft editor Content Draft is stale")
    if state.content_draft.content_draft_id != content_draft_id:
        raise ProjectError("draft editor Content Draft identity mismatch")

    base = transcript_base or load_draft_editor_transcript_base(workflow)
    expected_bindings = tuple(
        (binding.source_id, binding.transcript_version_id)
        for binding in workflow.source_bindings
    )
    if (
        base.project_revision != workflow.project_revision
        or base.source_bindings != expected_bindings
    ):
        raise ProjectError("draft editor transcript base is stale")
    items = _draft_items(state.content_draft, base)
    paragraphs, spans = _draft_paragraphs(state.content_draft, items)
    return DraftEditorSnapshot(
        workflow=workflow,
        candidate=state.content_draft,
        paragraphs=paragraphs,
        transcript_paragraphs=base.transcript_paragraphs,
        sources=workflow.sources,
        _view_hash=base.view_hash,
        _transcript_view=base.transcript_view,
        _items=items,
        _spans=spans,
        _transcript_base=base,
    )


def load_draft_editor_transcript_base(
    workflow: WorkflowReviewSnapshot,
) -> DraftEditorTranscriptBase:
    view = build_readable_transcript_view(
        workflow.project_path,
        source_bindings=[
            binding.to_dict() for binding in workflow.source_bindings
        ],
        expected_revision=workflow.project_revision,
    )
    transcript_paragraphs = tuple(
        paragraph.to_dict() for paragraph in numbered_readable_paragraphs(view)
    )
    transcripts = {
        (binding.source_id, binding.transcript_version_id): _read_transcript(
            workflow.project_path,
            binding.source_id,
            binding.transcript_version_id,
        )
        for binding in workflow.source_bindings
    }
    return DraftEditorTranscriptBase(
        project_revision=workflow.project_revision,
        source_bindings=tuple(
            (binding.source_id, binding.transcript_version_id)
            for binding in workflow.source_bindings
        ),
        transcript_paragraphs=transcript_paragraphs,
        view_hash=view.view_hash,
        transcript_view=view,
        transcripts=transcripts,
        paragraph_index=_original_paragraph_index(
            transcripts,
            transcript_paragraphs,
        ),
        project=ProjectStore(workflow.project_path).load(),
        source_display_names={
            str(source["source_id"]): str(source["display_name"])
            for source in workflow.sources
        },
    )


def resolve_draft_editor_selection(
    snapshot: DraftEditorSnapshot,
    *,
    surface: str,
    anchor: dict[str, object],
    focus: dict[str, object],
) -> DraftEditorSelection:
    _require_current(snapshot)
    if surface == "source":
        resolution = resolve_continuous_transcript_selection_from_view(
            snapshot._transcript_view,
            view_hash=snapshot._view_hash,
            anchor=anchor,
            focus=focus,
        )
        person_key = _single_person_key(snapshot, resolution.refs)
        return DraftEditorSelection(
            snapshot.candidate.content_draft_id,
            "source",
            resolution,
            None,
            None,
            person_key,
            (),
            _source_display_point(
                resolution.end_caret
                if resolution.direction == "backward"
                else resolution.start_caret
            ),
            _source_display_point(
                resolution.start_caret
                if resolution.direction == "backward"
                else resolution.end_caret
            ),
        )
    if surface != "draft":
        raise ProjectError("draft editor selection surface is invalid")

    anchor_point = _draft_request_point(snapshot, anchor)
    focus_point = _draft_request_point(snapshot, focus)
    if anchor_point[:2] == focus_point[:2]:
        raise ProjectError("draft editor selection must be non-empty")
    anchor_is_start = anchor_point[:2] < focus_point[:2]
    start_point, end_point = sorted((anchor_point, focus_point), key=lambda item: item[:2])
    selected_spans = _selected_draft_spans(snapshot, start_point, end_point)
    if not selected_spans:
        raise ProjectError("draft editor selection is empty")
    narration_spans = tuple(span for span in selected_spans if span.source is None)
    if narration_spans:
        if len(narration_spans) != len(selected_spans):
            raise ProjectError("draft editor selection cannot mix narration and source")
        narration_items_list: list[_NarrationItem] = []
        for span in narration_spans:
            item = snapshot._items[span.item_index]
            if isinstance(item, _NarrationItem):
                narration_items_list.append(item)
        narration_items = tuple(narration_items_list)
        if len(narration_items) != len(narration_spans):
            raise ProjectError("draft editor selection cannot include a section title")
        narration_ids = {
            item.block.block_id for item in narration_items
        }
        if len(narration_ids) != 1:
            raise ProjectError("draft editor selection cannot cross narration blocks")
        narration_id = next(iter(narration_ids))
        paragraph_id = narration_spans[0].paragraph_id
        paragraph = _find_draft_paragraph(snapshot, paragraph_id)
        full_text = str(paragraph["text"])
        if not full_text:
            raise ProjectError("draft editor narration selection is empty")
        narration_block = next(
            item.block for item in narration_items if item.block.block_id == narration_id
        )
        return DraftEditorSelection(
            snapshot.candidate.content_draft_id,
            "draft",
            None,
            narration_spans[0].item_index,
            narration_spans[-1].item_index + 1,
            ("narration", narration_id),
            (),
            {
                "paragraph_id": paragraph_id,
                "character_offset": 0,
                "utf16_offset": 0,
            },
            {
                "paragraph_id": paragraph_id,
                "character_offset": len(full_text),
                "utf16_offset": codepoint_to_utf16_offset(full_text, len(full_text)),
            },
            narration_id,
            narration_block.text,
            narration_block.status,
            narration_block.recorded_refs,
        )
    source_spans = tuple(
        (span, source)
        for span in selected_spans
        if (source := span.source) is not None
    )
    person_keys = {source.person_key for _, source in source_spans}
    resolution, correspondence_fragments = _resolve_compound_draft_selection(
        snapshot,
        selected_spans,
        start_point=start_point,
        end_point=end_point,
        direction="forward" if anchor_is_start else "backward",
    )
    # Keep the user's actual display range.  Reconstructing it from the
    # resolved canonical caret would silently absorb an adjacent editorial
    # punctuation mark, making an unselected mark move with the speech.
    display_start = _raw_display_point(snapshot, start_point[2], start_point[1])
    display_end = _raw_display_point(snapshot, end_point[2], end_point[1])
    resolved_start = _draft_display_point(
        snapshot,
        tuple(span for span, _ in source_spans),
        resolution.start_caret,
        bias="start",
    )
    resolved_end = _draft_display_point(
        snapshot,
        tuple(span for span, _ in source_spans),
        resolution.end_caret,
        bias="end",
    )
    paragraph_positions = {
        str(item["paragraph_id"]): index
        for index, item in enumerate(snapshot.paragraphs)
    }
    display_points = sorted(
        (display_start, display_end, resolved_start, resolved_end),
        key=lambda point: (
            paragraph_positions[str(point["paragraph_id"])],
            cast(int, point["character_offset"]),
        ),
    )
    resolved_start, resolved_end = display_points[0], display_points[-1]
    resolved_anchor = resolved_end if resolution.direction == "backward" else resolved_start
    resolved_focus = resolved_start if resolution.direction == "backward" else resolved_end
    return DraftEditorSelection(
        snapshot.candidate.content_draft_id,
        "draft",
        resolution,
        min(span.item_index for span, _ in source_spans),
        max(span.item_index for span, _ in source_spans) + 1,
        next(iter(person_keys)) if len(person_keys) == 1 else ("compound",),
        _logical_correspondence_groups(snapshot, correspondence_fragments),
        display_end if resolution.direction == "backward" else display_start,
        display_start if resolution.direction == "backward" else display_end,
        None,
        resolved_display_range=(resolved_anchor, resolved_focus),
    )


def resolve_draft_editor_caret(
    snapshot: DraftEditorSnapshot,
    *,
    paragraph_id: str,
    offset: int,
    offset_encoding: str = "codepoint",
) -> DraftEditorCaret:
    _require_current(snapshot)
    paragraph = _find_draft_paragraph(snapshot, paragraph_id)
    character_offset = _character_offset(
        str(paragraph["text"]),
        offset,
        offset_encoding,
    )
    spans = tuple(span for span in snapshot._spans if span.paragraph_id == paragraph_id)
    if not spans:
        raise ProjectError("draft editor paragraph has no caret positions")
    source_span = _span_for_boundary(spans, character_offset, bias="start")
    source_caret: CaretPosition | None = None
    if source_span.source is not None:
        original_offset = source_span.source.original_start_offset + _display_to_canonical_offset(
            source_span.source.display_text,
            source_span.source.ref.canonical_text,
            character_offset - source_span.start_offset,
            bias="start",
        )
        source_caret = resolve_transcript_caret_from_view(
            snapshot._transcript_view,
            view_hash=snapshot._view_hash,
            paragraph_id=source_span.source.original_paragraph_id,
            offset=original_offset,
            offset_encoding="codepoint",
        )
        character_offset = source_span.start_offset + _canonical_to_display_offset(
            source_span.source.display_text,
            source_span.source.ref.canonical_text,
            source_caret.character_offset - source_span.source.original_start_offset,
        )
    elif character_offset not in {0, len(str(paragraph["text"]))}:
        raise ProjectError("draft editor narration caret must be at a boundary")
    document_index = _document_boundary_index(
        spans,
        character_offset,
        len(snapshot._items),
    )
    boundary_id = "draft_caret_" + _hash_json(
        {
            "candidate_id": snapshot.candidate.content_draft_id,
            "paragraph_id": paragraph_id,
            "character_offset": character_offset,
            "document_index": document_index,
            "source_boundary_id": (
                source_caret.boundary_id if source_caret is not None else None
            ),
        }
    )[:32]
    return DraftEditorCaret(
        snapshot.candidate.content_draft_id,
        paragraph_id,
        character_offset,
        codepoint_to_utf16_offset(str(paragraph["text"]), character_offset),
        boundary_id,
        document_index,
        source_caret,
    )


def edit_draft_candidate(
    snapshot: DraftEditorSnapshot,
    *,
    operation: str,
    selection: DraftEditorSelection,
    caret: DraftEditorCaret | None,
    accept_degraded: bool,
) -> ContentDraftMutation:
    child = prepare_draft_candidate(
        snapshot,
        operation=operation,
        selection=selection,
        caret=caret,
        accept_degraded=accept_degraded,
        child_id=f"draft_{uuid4().hex}",
    )
    return publish_prepared_content_draft_editor_child(
        snapshot.workflow.project_path,
        parent=snapshot.candidate,
        child=child,
        expected_revision=snapshot.workflow.project_revision,
        display_ownership_resolved=True,
    )


def prepare_draft_candidate(
    snapshot: DraftEditorSnapshot,
    *,
    operation: str,
    selection: DraftEditorSelection,
    caret: DraftEditorCaret | None,
    accept_degraded: bool,
    child_id: str,
) -> ContentDraft:
    return prepare_draft_candidate_with_placement(
        snapshot,
        operation=operation,
        selection=selection,
        caret=caret,
        accept_degraded=accept_degraded,
        child_id=child_id,
    ).child


def prepare_draft_candidate_with_placement(
    snapshot: DraftEditorSnapshot,
    *,
    operation: str,
    selection: DraftEditorSelection,
    caret: DraftEditorCaret | None,
    accept_degraded: bool,
    child_id: str,
) -> DraftEditorPreparedChild:
    """Apply the existing exact-ref editor rules without publishing a child."""

    _require_current(snapshot)
    if selection.candidate_id != snapshot.candidate.content_draft_id:
        raise ProjectError("draft editor selection is stale")
    if caret is not None and caret.candidate_id != snapshot.candidate.content_draft_id:
        raise ProjectError("draft editor caret is stale")
    if not isinstance(accept_degraded, bool):
        raise ProjectError("draft editor degraded acceptance must be boolean")
    if selection.resolution is not None and selection.resolution.degraded and not accept_degraded:
        raise ProjectError("degraded selection must be explicitly accepted")
    if operation == "delete":
        if selection.surface != "draft" or caret is not None:
            raise ProjectError("draft delete requires only a draft selection")
    elif operation == "move":
        if selection.surface != "draft" or caret is None:
            raise ProjectError("draft move requires a draft selection and caret")
        if _caret_inside_selection(selection, caret):
            raise ProjectError("caret cannot be inside the moved selection")
    elif operation == "insert":
        if selection.surface != "source" or caret is None:
            raise ProjectError("draft insert requires a source selection and caret")
    else:
        raise ProjectError("draft editor operation is invalid")

    atoms = _working_atoms(snapshot)
    selected_indices: tuple[int, ...] = ()
    if selection.surface == "draft":
        atoms, selected_indices = _split_for_selection(
            snapshot,
            atoms,
            selection,
        )
    if operation == "move" and selection.narration_block_id is None:
        selected = set(selected_indices)
        atoms = tuple(
            replace(atom, placement_marker=index in selected)
            for index, atom in enumerate(atoms)
        )
    caret_index: int | None = None
    if caret is not None:
        atoms, caret_index = _split_for_caret(snapshot, atoms, caret)
        if selection.surface == "draft":
            selected_indices = _locate_selected_atoms(atoms, selection)

    if operation == "delete":
        changed_atoms = tuple(
            atom for index, atom in enumerate(atoms) if index not in selected_indices
        )
    elif operation == "move":
        assert caret is not None and caret_index is not None
        changed_atoms = _move_atoms(
            snapshot,
            atoms,
            selected_indices,
            selection,
            caret,
            caret_index,
            accept_degraded=accept_degraded,
        )
    else:
        assert caret is not None and caret_index is not None
        changed_atoms = _insert_atoms(
            snapshot,
            atoms,
            selection,
            caret,
            caret_index,
            accept_degraded=accept_degraded,
        )
    if changed_atoms == atoms:
        raise ProjectError("draft editor operation does not change the candidate")
    prepared = _prepare_candidate(
        snapshot,
        changed_atoms,
        operation=operation,
        child_id=child_id,
        placement_operation=(
            "move_selection"
            if operation == "move" and selection.narration_block_id is None
            else "insert_source_refs"
            if operation == "insert"
            else None
        ),
    )
    return prepared


def prepare_draft_punctuation(
    snapshot: DraftEditorSnapshot,
    *,
    paragraph_id: str,
    block_id: str,
    start_utf16_offset: int,
    end_utf16_offset: int,
    replacement: str,
    child_id: str,
) -> ContentDraft:
    """Prepare one display-only punctuation child without resolving media boundaries."""
    _require_current(snapshot)
    if not isinstance(paragraph_id, str) or not paragraph_id:
        raise ProjectError("draft punctuation paragraph ID is invalid")
    if not isinstance(block_id, str) or not block_id:
        raise ProjectError("draft punctuation block ID is invalid")
    paragraph = _find_draft_paragraph(snapshot, paragraph_id)
    if paragraph.get("kind") != "source_excerpt":
        raise ProjectError("draft punctuation requires a source paragraph")
    text = str(paragraph.get("text", ""))
    start = _character_offset(text, start_utf16_offset, "utf16")
    end = _character_offset(text, end_utf16_offset, "utf16")
    if end < start:
        raise ProjectError("draft punctuation range is reversed")
    runs = paragraph.get("source_runs")
    if not isinstance(runs, list):
        raise ProjectError("draft punctuation source runs are invalid")
    start_run = _source_run_for_punctuation_boundary(runs, start)
    end_run = _source_run_for_punctuation_range_end(runs, start, end)
    if start_run.get("block_id") != block_id or end_run.get("block_id") != block_id:
        raise ProjectError("draft punctuation range crosses source blocks")
    block = next(
        (
            item
            for item in snapshot.candidate.blocks
            if isinstance(item, SourceExcerptBlock) and item.block_id == block_id
        ),
        None,
    )
    if block is None:
        raise ProjectError("draft punctuation source block does not exist")
    block_start = _source_run_block_offset(snapshot, block, start_run)
    block_end = _source_run_block_offset(snapshot, block, end_run)
    local_start = block_start + start - cast(int, start_run["start_offset"])
    local_end = block_end + end - cast(int, end_run["start_offset"])
    current_display = block.display_text or block.canonical_text
    updated_display = validate_punctuation_replacement(
        block.canonical_text,
        current_display,
        local_start,
        local_end,
        replacement,
    )
    updated = tuple(
        replace(item, display_text=updated_display)
        if isinstance(item, SourceExcerptBlock) and item.block_id == block_id
        else item
        for item in project_schema1_to_schema2(snapshot.candidate).blocks
    )
    return prepare_content_draft_editor_child(
        parent=snapshot.candidate,
        blocks=updated,
        child_id=child_id,
    )


def _source_run_for_punctuation_boundary(
    runs: list[object],
    offset: int,
) -> dict[str, object]:
    parsed = [
        run
        for run in runs
        if isinstance(run, dict)
        and isinstance(run.get("start_offset"), int)
        and not isinstance(run.get("start_offset"), bool)
        and isinstance(run.get("end_offset"), int)
        and not isinstance(run.get("end_offset"), bool)
        and isinstance(run.get("block_id"), str)
    ]
    for run in parsed:
        start = int(run["start_offset"])
        end = int(run["end_offset"])
        if start <= offset < end:
            return run
    for index, run in enumerate(parsed):
        if int(run["end_offset"]) != offset:
            continue
        if index + 1 < len(parsed) and int(parsed[index + 1]["start_offset"]) == offset:
            return parsed[index + 1]
        return run
    raise ProjectError("draft punctuation boundary does not map to a source block")


def _source_run_for_punctuation_range_end(
    runs: list[object],
    start: int,
    end: int,
) -> dict[str, object]:
    """Keep a half-open range ending at a block edge owned by its left block."""
    if end > start:
        parsed = [
            run
            for run in runs
            if isinstance(run, dict)
            and isinstance(run.get("start_offset"), int)
            and not isinstance(run.get("start_offset"), bool)
            and isinstance(run.get("end_offset"), int)
            and not isinstance(run.get("end_offset"), bool)
            and isinstance(run.get("block_id"), str)
        ]
        for run in parsed:
            if int(run["end_offset"]) == end:
                return run
    return _source_run_for_punctuation_boundary(runs, end)


def _source_run_block_offset(
    snapshot: DraftEditorSnapshot,
    block: SourceExcerptBlock,
    target_run: dict[str, object],
) -> int:
    block_items = [
        item
        for item in snapshot._items
        if isinstance(item, _SourceItem) and item.block_id == block.block_id
    ]
    target_refs = target_run.get("refs")
    if not isinstance(target_refs, list) or len(target_refs) != 1:
        raise ProjectError("draft punctuation source run refs are invalid")
    target_ref = target_refs[0]
    if not isinstance(target_ref, dict):
        raise ProjectError("draft punctuation source run ref is invalid")
    target_indices = [
        index
        for index, item in enumerate(block_items)
        if _content_ref_dict(item.ref) == target_ref
    ]
    if len(target_indices) != 1:
        raise ProjectError("draft punctuation source run is stale")
    target_index = target_indices[0]
    # The persisted block display keeps the fixed newline between refs even
    # though the rendered paragraph joins the visible ref fragments.  Include
    # each preceding connection when converting paragraph coordinates back to
    # the block-local display coordinate.
    return sum(len(item.display_text) for item in block_items[:target_index]) + target_index


def update_draft_narration(
    snapshot: DraftEditorSnapshot,
    *,
    block_id: str,
    text: str,
) -> ContentDraftMutation:
    child = prepare_draft_narration(
        snapshot,
        block_id=block_id,
        text=text,
        child_id=f"draft_{uuid4().hex}",
    )
    return publish_prepared_content_draft_editor_child(
        snapshot.workflow.project_path,
        parent=snapshot.candidate,
        child=child,
        expected_revision=snapshot.workflow.project_revision,
        display_ownership_resolved=True,
    )


def prepare_draft_narration(
    snapshot: DraftEditorSnapshot,
    *,
    block_id: str,
    text: str,
    child_id: str,
) -> ContentDraft:
    """Apply narration validation without publishing a child."""

    _require_current(snapshot)
    if not isinstance(block_id, str) or not block_id:
        raise ProjectError("draft narration block ID is invalid")
    if not isinstance(text, str) or not text.strip():
        raise ProjectError("draft narration text is required")
    updated: list[_WorkingAtom] = []
    found = False
    for item_index, item in enumerate(snapshot._items):
        if isinstance(item, _NarrationItem) and item.block.block_id == block_id:
            found = True
            if item.block.text == text.strip() and item.block.status == "draft":
                raise ProjectError("draft narration text is unchanged")
            updated.append(
                _WorkingAtom(
                    None,
                    replace(
                        item.block,
                        text=text.strip(),
                        status="draft",
                        recorded_refs=(),
                    ),
                    item_index,
                    item.section_title,
                    item.section_origin,
                    item.paragraph_key,
                )
            )
        else:
            updated.append(_working_atom(item, item_index))
    if not found:
        raise ProjectError("draft narration block does not exist or is not narration")
    return _prepare_candidate(
        snapshot,
        tuple(updated),
        operation="narration",
        child_id=child_id,
    ).child


def search_draft_editor(
    snapshot: DraftEditorSnapshot,
    *,
    surface: str,
    query: str,
    offset: int,
    limit: int,
) -> DraftSearchPage:
    _require_current(snapshot)
    if surface not in {"draft", "source"}:
        raise ProjectError("draft search surface is invalid")
    if not isinstance(query, str) or not query.strip():
        raise ProjectError("draft search query is required")
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or offset < 0
        or isinstance(limit, bool)
        or not isinstance(limit, int)
        or limit < 1
        or limit > _MAX_SEARCH_LIMIT
    ):
        raise ProjectError("draft search pagination is invalid")
    paragraphs = (
        snapshot.paragraphs if surface == "draft" else snapshot.transcript_paragraphs
    )
    pattern = re.compile(re.escape(query.strip()), re.IGNORECASE)
    matches: list[dict[str, object]] = []
    for paragraph in paragraphs:
        text = str(paragraph["text"])
        paragraph_occurrence = 0
        for paragraph_occurrence, match in enumerate(pattern.finditer(text)):
            match_id = "match_" + _hash_json(
                {
                    "candidate_id": snapshot.candidate.content_draft_id,
                    "surface": surface,
                    "paragraph_id": paragraph["paragraph_id"],
                    "start": match.start(),
                    "end": match.end(),
                    "occurrence": paragraph_occurrence,
                }
            )[:32]
            matches.append(
                {
                    "match_id": match_id,
                    "paragraph_id": paragraph["paragraph_id"],
                    "occurrence": paragraph_occurrence,
                    "start_offset": match.start(),
                    "end_offset": match.end(),
                    "context": _search_context(text, match.start(), match.end()),
                    "source_id": paragraph.get("source_id"),
                    "source_display_name": paragraph.get("source_display_name"),
                    "person_name": (
                        paragraph.get("person_name")
                        if surface == "source"
                        else _nested_person_name(paragraph)
                    ),
                    "start_ticks": paragraph.get("start_ticks"),
                }
            )
            paragraph_occurrence += 1
    page = matches[offset : offset + limit]
    end = offset + len(page)
    return DraftSearchPage(
        surface=surface,  # type: ignore[arg-type]
        query=query.strip(),
        offset=offset,
        limit=limit,
        total=len(matches),
        next_cursor=end if end < len(matches) else None,
        matches=tuple(page),
    )


def read_draft_transcript_window(
    snapshot: DraftEditorSnapshot,
    *,
    source_id: str,
    offset: int,
    limit: int,
    paragraph_id: str | None = None,
) -> DraftTranscriptWindow:
    _require_current(snapshot)
    if (
        not isinstance(source_id, str)
        or not source_id
        or source_id not in snapshot.authorized_source_ids
    ):
        raise ProjectError("draft transcript window source is not authorized")
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or offset < 0
        or isinstance(limit, bool)
        or not isinstance(limit, int)
        or limit < 1
        or limit > _MAX_PAGE_SIZE
    ):
        raise ProjectError("draft transcript window pagination is invalid")
    paragraphs = tuple(
        item
        for item in snapshot.transcript_paragraphs
        if item.get("source_id") == source_id
    )
    total = len(paragraphs)
    actual_offset = offset
    if paragraph_id is not None:
        if not isinstance(paragraph_id, str) or not paragraph_id:
            raise ProjectError("draft transcript window paragraph is invalid")
        location = next(
            (
                index
                for index, paragraph in enumerate(paragraphs)
                if paragraph.get("paragraph_id") == paragraph_id
            ),
            None,
        )
        if location is None:
            raise ProjectError("draft transcript window paragraph does not exist")
        actual_offset = min(
            max(location - limit // 2, 0),
            max(total - limit, 0),
        )
    page = paragraphs[actual_offset : actual_offset + limit]
    end = actual_offset + len(page)
    return DraftTranscriptWindow(
        candidate_id=snapshot.candidate.content_draft_id,
        source_id=source_id,
        offset=actual_offset,
        limit=limit,
        total=total,
        next_cursor=end if end < total else None,
        previous_cursor=max(actual_offset - limit, 0) if actual_offset > 0 else None,
        located_paragraph_id=paragraph_id,
        paragraphs=page,
    )


def validate_draft_editor_snapshot(snapshot: DraftEditorSnapshot) -> None:
    """Run the cheap fail-closed checks required before reusing a session cache."""
    _require_current(snapshot)


def materialize_draft_editor_placement(
    snapshot: DraftEditorSnapshot,
    candidate: ContentDraft,
    placement: DraftEditorPlacementResult,
) -> DraftEditorPlacementResult:
    """Project and validate one request-local placement before publication."""

    if candidate.source_bindings != snapshot.candidate.source_bindings:
        raise ProjectError("draft editor prepared child bindings are stale")
    paragraphs, _ = _draft_paragraphs(candidate, _draft_items(candidate, snapshot.transcript_base))
    prospective = replace(snapshot, candidate=candidate, paragraphs=paragraphs)

    if candidate.content_draft_id != placement.candidate_id:
        raise ProjectError("draft editor placement candidate identity is invalid")
    if not placement.block_ids or len(set(placement.block_ids)) != len(placement.block_ids):
        raise ProjectError("draft editor placement occurrence identity is invalid")

    positions: dict[str, tuple[int, str, int, int]] = {}
    for paragraph_index, paragraph in enumerate(paragraphs):
        if paragraph["kind"] != "source_excerpt":
            continue
        for run in cast(list[dict[str, object]], paragraph["source_runs"]):
            block_id = cast(str, run["block_id"])
            if block_id not in placement.block_ids:
                continue
            if block_id in positions:
                raise ProjectError("draft editor placement occurrence is not unique")
            positions[block_id] = (
                paragraph_index,
                cast(str, paragraph["paragraph_id"]),
                cast(int, run["start_offset"]),
                cast(int, run["end_offset"]),
            )

    if tuple(positions) != placement.block_ids:
        raise ProjectError("draft editor placement range is not document-ordered")

    first_index, first_paragraph_id, first_start, _ = positions[placement.block_ids[0]]
    last_index, last_paragraph_id, _, last_end = positions[placement.block_ids[-1]]
    for paragraph_index in range(first_index, last_index + 1):
        paragraph = paragraphs[paragraph_index]
        if paragraph["kind"] != "source_excerpt":
            raise ProjectError("draft editor placement range includes a chapter title")
        for run in cast(list[dict[str, object]], paragraph["source_runs"]):
            block_id = cast(str, run["block_id"])
            start_offset = cast(int, run["start_offset"])
            end_offset = cast(int, run["end_offset"])
            outside_before = paragraph_index == first_index and end_offset <= first_start
            outside_after = paragraph_index == last_index and start_offset >= last_end
            if block_id not in placement.block_ids and not (outside_before or outside_after):
                raise ProjectError("draft editor placement range includes unrelated content")

    display_range = (
        _raw_display_point(prospective, first_paragraph_id, first_start),
        _raw_display_point(prospective, last_paragraph_id, last_end),
    )
    return replace(placement, display_range=display_range)


def prepare_draft_editor_result_selection(
    snapshot: DraftEditorSnapshot,
    *,
    child: ContentDraft,
    placement: DraftEditorPlacementResult,
    child_ref: ArtifactRef,
) -> dict[str, object]:
    """Build the private result_selection wire from the prepared child."""
    if placement.display_range is None:
        raise ProjectError("draft editor placement result is not verified")
    items = _draft_items(child, snapshot.transcript_base)
    paragraphs, spans = _draft_paragraphs(child, items)
    prospective = replace(
        snapshot,
        workflow=replace(
            snapshot.workflow,
            content_draft=ContentDraftState(
                child, snapshot.workflow.project_revision, "current", ()
            ),
        ),
        candidate=child,
        paragraphs=paragraphs,
        _items=items,
        _spans=spans,
    )
    anchor, focus = placement.display_range

    def request_point(point: dict[str, object]) -> dict[str, object]:
        return {
            "paragraph_id": point["paragraph_id"],
            "offset": point["utf16_offset"],
            "offset_encoding": "utf16",
        }

    selection = resolve_draft_editor_selection(
        prospective,
        surface="draft",
        anchor=request_point(anchor),
        focus=request_point(focus),
    )
    if selection.resolution is None:
        raise ProjectError("draft editor result selection resolved narration")
    block_refs = tuple(
        item.ref
        for block_id in placement.block_ids
        for item in items
        if isinstance(item, _SourceItem) and item.block_id == block_id
    )
    def identity(ref: ResolvedSelectionRef) -> tuple[object, ...]:
        return (
            ref.source_id,
            ref.transcript_version_id,
            ref.segment_id,
            ref.start_ticks,
            ref.end_ticks,
        )

    if [identity(ref) for ref in selection.resolution.refs] != [
        identity(ref) for ref in block_refs
    ]:
        raise ProjectError("draft editor result selection refs are stale")
    response = selection.to_dict()
    response["resolution_hash"] = _result_selection_hash(
        prospective,
        selection,
        child_ref,
    )
    resolved_anchor, resolved_focus = selection.resolved_display_range or (
        selection.display_anchor,
        selection.display_focus,
    )
    return {
        "surface": "draft",
        "request": {
            "anchor": request_point(resolved_anchor),
            "focus": request_point(resolved_focus),
        },
        "response": response,
        "accepted_degraded": True,
    }


def _result_selection_hash(
    prospective: DraftEditorSnapshot,
    selection: DraftEditorSelection,
    child_ref: ArtifactRef,
) -> str:
    """Hash the schema-2 source payload exactly like the resolve endpoint.

    The server re-computes `canonical_sha256_v1(dict(source) - resolution_hash
    + candidate_ref)` with the client-round-tripped S shape; the hash must
    match that shape or the next drop is rejected as stale.
    """
    if selection.resolution is None:
        raise ProjectError("draft editor result selection resolved narration")

    def display_point(
        point: dict[str, object],
    ) -> dict[str, object]:
        paragraph = _find_draft_paragraph(prospective, str(point["paragraph_id"]))
        character_offset = cast(int, point["character_offset"])
        runs = cast(list[dict[str, object]], paragraph.get("source_runs"))
        block_id: str | None = None
        for index, run in enumerate(runs):
            if not isinstance(run, dict):
                continue
            candidate = run.get("block_id")
            start = run.get("start_offset")
            end = run.get("end_offset")
            if (
                not isinstance(candidate, str)
                or isinstance(start, bool)
                or not isinstance(start, int)
                or isinstance(end, bool)
                or not isinstance(end, int)
            ):
                continue
            if start <= character_offset < end:
                block_id = candidate
                break
            if character_offset == end:
                next_run = runs[index + 1] if index + 1 < len(runs) else None
                next_start = next_run.get("start_offset") if isinstance(next_run, dict) else None
                if next_start != character_offset:
                    block_id = candidate
                    break
        if block_id is None:
            raise ProjectError("draft editor result selection display block is missing")
        text = str(paragraph["text"])
        return {
            "paragraph_id": str(point["paragraph_id"]),
            "block_id": block_id,
            "utf16_offset": codepoint_to_utf16_offset(text, character_offset),
        }

    anchor, focus = selection.resolved_display_range or (
        selection.display_anchor,
        selection.display_focus,
    )
    block_ids: list[str] = []
    if selection.item_start is not None and selection.item_end is not None:
        for item in prospective._items[selection.item_start : selection.item_end]:
            block_id = getattr(item, "block_id", None)
            if isinstance(block_id, str) and (not block_ids or block_ids[-1] != block_id):
                block_ids.append(block_id)
    source_payload = {
        "kind": "resolved_selection",
        "surface": "draft",
        "selection_kind": "source_excerpt",
        "display_range": {"anchor": display_point(anchor), "focus": display_point(focus)},
        "block_ids": block_ids,
        "refs": [
            {
                "source_id": ref.source_id,
                "transcript_version_id": ref.transcript_version_id,
                "segment_id": ref.segment_id,
                "start_ticks": ref.start_ticks,
                "end_ticks": ref.end_ticks,
            }
            for ref in selection.resolution.refs
        ],
        "canonical_text": selection.resolution.canonical_text,
        "degraded": selection.resolution.degraded,
    }
    return canonical_sha256_v1(
        {**source_payload, "candidate_ref": child_ref.to_dict()}
    )


def _require_current(snapshot: DraftEditorSnapshot) -> None:
    selected = snapshot.workflow.content_draft
    if (
        selected is None
        or selected.content_draft.content_draft_id
        != snapshot.candidate.content_draft_id
    ):
        raise ProjectError("draft editor candidate is stale")
    current = ProjectStore(snapshot.workflow.project_path).load()
    if current.revision != snapshot.workflow.project_revision:
        raise ProjectError("project revision conflict")
    if any(
        current.active_transcript_versions.get(binding.source_id)
        != binding.transcript_version_id
        for binding in snapshot.workflow.source_bindings
    ):
        raise ProjectError("draft editor Transcript binding is stale")


def _bindings_payload(snapshot: DraftEditorSnapshot) -> list[dict[str, object]]:
    return [binding.to_dict() for binding in snapshot.workflow.source_bindings]


def _draft_items(
    draft: ContentDraft,
    base: DraftEditorTranscriptBase,
) -> tuple[_DraftItem, ...]:
    items: list[_DraftItem] = []
    current_section_title: str | None = None
    current_section_id: str | None = None
    section_first_item_pending = False
    ordinary_run_active = False
    ordinary_run_section: str | None = None
    ordinary_run_person: tuple[str, ...] = ()
    ordinary_run_paragraph: tuple[str, ...] = ()
    for block_index, block in enumerate(draft.blocks):
        if isinstance(block, SectionTitleBlock):
            current_section_title = block.title
            current_section_id = block.block_id
            section_first_item_pending = True
            ordinary_run_active = False
            continue
        if isinstance(block, NarrationBlock):
            items.append(
                _NarrationItem(
                    block,
                    current_section_title,
                    current_section_id,
                    ("narration", block.block_id),
                    section_first_item_pending,
                )
            )
            section_first_item_pending = False
            ordinary_run_active = False
            continue
        persisted_paragraph = _persisted_source_paragraph_key(block.block_id)
        if persisted_paragraph is not None:
            ordinary_run_active = False
        canonical_parts: list[str] = []
        block_section_title = (
            current_section_title
            if current_section_title is not None
            else getattr(block, "section_title", None)
        )
        block_section_id = (
            current_section_id
            if current_section_id is not None
            else (
                legacy_section_block_id(
                    draft.content_draft_id,
                    block.block_id,
                    block_index,
                )
                if block_section_title is not None and draft.schema_version == 1
                else (block.block_id if block_section_title is not None else None)
            )
        )
        legacy_title_boundary = (
            draft.schema_version == 1
            and current_section_id is None
            and block_section_title is not None
        )
        display_parts = split_display_text_for_canonical_parts(
            block.display_text or block.canonical_text,
            [
                canonical_text_for_range(
                    _find_segment(
                        base.transcripts[(ref.source_id, ref.transcript_version_id)],
                        ref.segment_id,
                    ),
                    ref.start_ticks,
                    ref.end_ticks,
                )
                for ref in block.refs
            ],
        )
        for ref_index, ref in enumerate(block.refs):
            transcript = base.transcripts[(ref.source_id, ref.transcript_version_id)]
            segment = _find_segment(transcript, ref.segment_id)
            paragraph_id, segment_offset = base.paragraph_index[
                (ref.source_id, ref.transcript_version_id, ref.segment_id)
            ]
            local_start, local_end, fine_start, fine_end = _range_offsets(segment, ref)
            resolved = ResolvedSelectionRef(
                ref.source_id,
                ref.transcript_version_id,
                ref.segment_id,
                ref.start_ticks,
                ref.end_ticks,
                canonical_text_for_range(segment, ref.start_ticks, ref.end_ticks),
                fine_start,
                fine_end,
            )
            canonical_parts.append(resolved.canonical_text)
            person_key, person_id, person_name, person_role = _person_identity(
                base.project,
                ref.source_id,
                ref.transcript_version_id,
                segment,
            )
            if persisted_paragraph is not None:
                paragraph_key = persisted_paragraph
            elif (
                ordinary_run_active
                and ordinary_run_section == block_section_id
                and ordinary_run_person == person_key
            ):
                paragraph_key = ordinary_run_paragraph
            else:
                paragraph_key = _source_paragraph_key(
                    f"{block.block_id}:{ref_index}"
                )
                ordinary_run_active = True
                ordinary_run_section = block_section_id
                ordinary_run_person = person_key
                ordinary_run_paragraph = paragraph_key
            items.append(
                _SourceItem(
                    block.block_id,
                    resolved,
                    paragraph_id,
                    segment_offset + local_start,
                    segment_offset + local_end,
                    base.source_display_names[ref.source_id],
                    person_key,
                    person_id,
                    person_name,
                    person_role,
                    segment.local_speaker_id,
                    block_section_title,
                    block_section_id,
                    paragraph_key,
                    (
                        ref_index == 0
                        and block_section_title is not None
                        and (section_first_item_pending or legacy_title_boundary)
                    ),
                    display_parts[ref_index],
                )
            )
        if block.refs:
            section_first_item_pending = False
        if "\n".join(canonical_parts) != block.canonical_text:
            raise ProjectError("draft editor child canonical text does not match exact refs")
    return tuple(items)


def _source_paragraph_key(
    block_id: str,
) -> tuple[str, ...]:
    persisted = _persisted_source_paragraph_key(block_id)
    return (
        persisted
        if persisted is not None
        else ("source", _hash_json({"source_block": block_id})[:24])
    )


def _persisted_source_paragraph_key(
    block_id: str,
) -> tuple[str, ...] | None:
    generated = _EDIT_PARAGRAPH_BLOCK.fullmatch(block_id)
    return None if generated is None else ("source", generated.group(1))


def _original_paragraph_index(
    transcripts: dict[tuple[str, str], TimedTranscript],
    paragraphs: tuple[dict[str, object], ...],
) -> dict[tuple[str, str, str], tuple[str, int]]:
    result: dict[tuple[str, str, str], tuple[str, int]] = {}
    for paragraph in paragraphs:
        source_id = str(paragraph["source_id"])
        transcript_id = str(paragraph["transcript_version_id"])
        transcript = transcripts[(source_id, transcript_id)]
        segments = {segment.segment_id: segment for segment in transcript.segments}
        cursor = 0
        refs = paragraph["refs"]
        if not isinstance(refs, list):
            raise ProjectError("draft editor transcript refs are invalid")
        for index, ref in enumerate(refs):
            if not isinstance(ref, dict):
                raise ProjectError("draft editor transcript ref is invalid")
            segment_id = ref.get("segment_id")
            if not isinstance(segment_id, str) or segment_id not in segments:
                raise ProjectError("draft editor transcript segment is invalid")
            result[(source_id, transcript_id, segment_id)] = (
                str(paragraph["paragraph_id"]),
                cursor,
            )
            cursor += len(_effective_text(segments[segment_id]))
            if index + 1 < len(refs):
                cursor += 1
    return result


def _draft_paragraphs(
    draft: ContentDraft,
    items: tuple[_DraftItem, ...],
) -> tuple[tuple[dict[str, object], ...], tuple[_DraftSpan, ...]]:
    runs: list[list[tuple[int, _DraftItem]]] = []
    for item_index, item in enumerate(items):
        key = item.paragraph_key
        latest_key: tuple[str, ...] | None = None
        if runs:
            latest = runs[-1][0][1]
            latest_key = latest.paragraph_key
        if latest_key != key:
            runs.append([])
        runs[-1].append((item_index, item))

    paragraphs: list[dict[str, object]] = []
    paragraph_item_starts: list[int] = []
    spans: list[_DraftSpan] = []
    for run_index, run in enumerate(runs):
        paragraph_id = "draft_paragraph_" + _hash_json(
            {
                "candidate_id": draft.content_draft_id,
                "run_index": run_index,
                "items": [
                    (
                        item.block.block_id
                        if isinstance(item, _NarrationItem)
                        else item.ref.to_dict()
                    )
                    for _, item in run
                ],
            }
        )[:32]
        first = run[0][1]
        if isinstance(first, _NarrationItem):
            text = first.block.text
            paragraphs.append(
                {
                    "paragraph_id": paragraph_id,
                    "kind": "narration",
                    "person": {
                        "person_id": None,
                        "name": "解说",
                        "role": "解说",
                        "local_speaker_id": None,
                    },
                    "text": text,
                    "section_title": (
                        first.section_title
                        if first.display_section_title
                        else None
                    ),
                    "narration_status": first.block.status,
                    "block_id": first.block.block_id,
                    "source_runs": [],
                    "exact_refs": [],
                }
            )
            paragraph_item_starts.append(run[0][0])
            spans.append(_DraftSpan(paragraph_id, 0, len(text), run[0][0], None))
            continue
        source_items = [
            (item_index, item)
            for item_index, item in run
            if isinstance(item, _SourceItem)
        ]
        text_parts: list[str] = []
        source_runs: list[dict[str, object]] = []
        exact_refs: list[dict[str, object]] = []
        cursor = 0
        local_speakers: list[str] = []
        for item_index, item in source_items:
            text = item.display_text
            text_parts.append(text)
            source_runs.append(
                {
                    "block_id": item.block_id,
                    "source_id": item.ref.source_id,
                    "source_display_name": item.source_display_name,
                    "paragraph_id": item.original_paragraph_id,
                    "start_ticks": item.ref.start_ticks,
                    "end_ticks": item.ref.end_ticks,
                    "start_offset": cursor,
                    "end_offset": cursor + len(text),
                    "source_start_offset": item.original_start_offset,
                    "source_end_offset": item.original_end_offset,
                    "text": text,
                    "refs": [_content_ref_dict(item.ref)],
                }
            )
            exact_refs.append(_content_ref_dict(item.ref))
            spans.append(
                _DraftSpan(
                    paragraph_id,
                    cursor,
                    cursor + len(text),
                    item_index,
                    item,
                )
            )
            cursor += len(text)
            if item.local_speaker_id is not None:
                local_speakers.append(item.local_speaker_id)
        first_source = source_items[0][1]
        person_keys = {item.person_key for _, item in source_items}
        has_single_person = len(person_keys) == 1
        paragraphs.append(
            {
                "paragraph_id": paragraph_id,
                "kind": "source_excerpt",
                "person": {
                    "person_id": first_source.person_id if has_single_person else None,
                    "name": first_source.person_name if has_single_person else None,
                    "role": first_source.person_role if has_single_person else None,
                    "local_speaker_id": (
                        local_speakers[0]
                        if (
                            has_single_person
                            and len(set(local_speakers)) == 1
                            and local_speakers
                        )
                        else None
                    ),
                },
                "text": "".join(text_parts),
                "section_title": (
                    first_source.section_title
                    if first_source.display_section_title
                    else None
                ),
                "narration_status": None,
                "source_runs": source_runs,
                "exact_refs": exact_refs,
            }
        )
        paragraph_item_starts.append(run[0][0])
    emitted_heading_ids = {
        item.section_origin
        for item in items
        if (
            isinstance(item, _SourceItem) and item.section_origin is not None
        )
        or (
            isinstance(item, _NarrationItem) and item.section_origin is not None
        )
    }
    block_item_counts: dict[str, int] = {}
    for item in items:
        block_id = (
            item.block.block_id
            if isinstance(item, _NarrationItem)
            else item.block_id
        )
        block_item_counts[block_id] = block_item_counts.get(block_id, 0) + 1
    empty_heading_entries: list[tuple[SectionTitleBlock, int]] = []
    item_cursor = 0
    for block in draft.blocks:
        if isinstance(block, SectionTitleBlock):
            if block.block_id not in emitted_heading_ids:
                empty_heading_entries.append((block, item_cursor))
            continue
        item_cursor += block_item_counts.get(block.block_id, 0)
    for heading, insertion_item_index in reversed(empty_heading_entries):
        paragraph_id = "draft_section_paragraph_" + _hash_json(
            {"candidate_id": draft.content_draft_id, "heading": heading.block_id}
        )[:32]
        paragraph: dict[str, object] = {
            "paragraph_id": paragraph_id,
            "kind": "section_title",
            "person": {"person_id": None, "name": heading.title, "role": None, "local_speaker_id": None},
            "text": "",
            "section_title": heading.title,
            "narration_status": None,
            "block_id": heading.block_id,
            "source_runs": [],
            "exact_refs": [],
        }
        insert_at = next(
            (index for index, start in enumerate(paragraph_item_starts) if start >= insertion_item_index),
            len(paragraphs),
        )
        paragraphs.insert(insert_at, paragraph)
        paragraph_item_starts.insert(insert_at, insertion_item_index)
        spans.append(_DraftSpan(paragraph_id, 0, 0, insertion_item_index, None))
    return tuple(paragraphs), tuple(spans)


def _draft_request_point(
    snapshot: DraftEditorSnapshot,
    value: dict[str, object],
) -> tuple[int, int, str]:
    if not isinstance(value, dict):
        raise ProjectError("draft editor endpoint must be an object")
    paragraph_id = value.get("paragraph_id")
    offset = value.get("offset")
    encoding = value.get("offset_encoding", "codepoint")
    if not isinstance(paragraph_id, str):
        raise ProjectError("draft editor paragraph ID is invalid")
    paragraph = _find_draft_paragraph(snapshot, paragraph_id)
    character_offset = _character_offset(str(paragraph["text"]), offset, encoding)
    positions = {
        str(item["paragraph_id"]): index
        for index, item in enumerate(snapshot.paragraphs)
    }
    return positions[paragraph_id], character_offset, paragraph_id


def _character_offset(text: str, value: object, encoding: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProjectError("draft editor text offset must be an integer")
    if encoding == "codepoint":
        offset = value
    elif encoding == "utf16":
        offset = utf16_to_codepoint_offset(text, value)
    else:
        raise ProjectError("draft editor offset encoding is invalid")
    if offset < 0 or offset > len(text):
        raise ProjectError("draft editor text offset is out of bounds")
    return offset


def _selected_draft_spans(
    snapshot: DraftEditorSnapshot,
    start: tuple[int, int, str],
    end: tuple[int, int, str],
) -> tuple[_DraftSpan, ...]:
    paragraph_positions = {
        str(item["paragraph_id"]): index
        for index, item in enumerate(snapshot.paragraphs)
    }
    result = [
        span
        for span in snapshot._spans
        if (
            start[:2]
            < (
                paragraph_positions[span.paragraph_id],
                span.end_offset,
            )
            and (
                paragraph_positions[span.paragraph_id],
                span.start_offset,
            )
            < end[:2]
        )
    ]
    result.sort(
        key=lambda span: (
            paragraph_positions[span.paragraph_id],
            span.start_offset,
            span.item_index,
        )
    )
    return tuple(result)


def _source_endpoint(
    point: tuple[int, int, str],
    spans: Sequence[_DraftSpan],
    *,
    bias: Literal["start", "end"],
) -> dict[str, object]:
    candidates = [span for span in spans if span.paragraph_id == point[2]]
    span = _span_for_boundary(candidates, point[1], bias=bias)
    if span.source is None:
        raise ProjectError("draft editor selection cannot include narration")
    local = point[1] - span.start_offset
    original = span.source.original_start_offset + _display_to_canonical_offset(
        span.source.display_text,
        span.source.ref.canonical_text,
        local,
        bias=bias,
    )
    return {
        "paragraph_id": span.source.original_paragraph_id,
        "offset": original,
        "offset_encoding": "codepoint",
    }


def _resolve_compound_draft_selection(
    snapshot: DraftEditorSnapshot,
    selected_spans: Sequence[_DraftSpan],
    *,
    start_point: tuple[int, int, str],
    end_point: tuple[int, int, str],
    direction: Literal["forward", "backward"],
) -> tuple[ContinuousSelectionResolution, tuple[_CorrespondenceFragment, ...]]:
    refs: list[ResolvedSelectionRef] = []
    fragments: list[_CorrespondenceFragment] = []
    reasons: list[str] = []
    start_caret: CaretPosition | None = None
    end_caret: CaretPosition | None = None
    adjusted = False
    for span in selected_spans:
        if span.source is None:
            raise ProjectError("draft editor selection cannot include narration")
        local_start = (
            max(start_point[1], span.start_offset)
            if span.paragraph_id == start_point[2]
            else span.start_offset
        )
        local_end = (
            min(end_point[1], span.end_offset)
            if span.paragraph_id == end_point[2]
            else span.end_offset
        )
        if local_start >= local_end:
            continue
        original_start = span.source.original_start_offset + _display_to_canonical_offset(
            span.source.display_text,
            span.source.ref.canonical_text,
            local_start - span.start_offset,
            bias="start",
        )
        original_end = span.source.original_start_offset + _display_to_canonical_offset(
            span.source.display_text,
            span.source.ref.canonical_text,
            local_end - span.start_offset,
            bias="end",
        )
        part = resolve_continuous_transcript_selection_from_view(
            snapshot._transcript_view,
            view_hash=snapshot._view_hash,
            anchor={
                "paragraph_id": span.source.original_paragraph_id,
                "offset": original_start,
                "offset_encoding": "codepoint",
            },
            focus={
                "paragraph_id": span.source.original_paragraph_id,
                "offset": original_end,
                "offset_encoding": "codepoint",
            },
        )
        if (
            len(part.refs) != 1
            or not _same_segment(span.source.ref, part.refs[0])
            or part.refs[0].start_ticks < span.source.ref.start_ticks
            or part.refs[0].end_ticks > span.source.ref.end_ticks
        ):
            raise ProjectError(
                "draft editor compound selection escaped its candidate ref"
            )
        refs.extend(part.refs)
        fragments.append(
            _CorrespondenceFragment(
                source_id=span.source.ref.source_id,
                source_display_name=span.source.source_display_name,
                paragraph_id=span.source.original_paragraph_id,
                person_key=span.source.person_key,
                start_offset=part.start_caret.character_offset,
                end_offset=part.end_caret.character_offset,
            )
        )
        if start_caret is None:
            start_caret = part.start_caret
        end_caret = part.end_caret
        adjusted = adjusted or part.adjusted
        reasons.extend(part.degradation_reasons)
    if not refs or start_caret is None or end_caret is None:
        raise ProjectError("draft editor selection does not cover candidate refs")
    return (
        ContinuousSelectionResolution(
            view_hash=snapshot._view_hash,
            direction=direction,
            canonical_text="".join(ref.canonical_text for ref in refs),
            refs=tuple(refs),
            start_caret=start_caret,
            end_caret=end_caret,
            adjusted=adjusted,
            degraded=bool(reasons),
            degradation_reasons=tuple(dict.fromkeys(reasons)),
        ),
        tuple(fragments),
    )


def _logical_correspondence_groups(
    snapshot: DraftEditorSnapshot,
    fragments: Sequence[_CorrespondenceFragment],
) -> tuple[dict[str, object], ...]:
    paragraph_text = {
        str(paragraph["paragraph_id"]): str(paragraph["text"])
        for paragraph in snapshot.transcript_paragraphs
    }
    groups: list[dict[str, object]] = []
    previous_fragment: _CorrespondenceFragment | None = None
    for fragment in fragments:
        previous = groups[-1] if groups else None
        can_merge = (
            previous is not None
            and previous_fragment is not None
            and previous_fragment.source_id == fragment.source_id
            and previous_fragment.paragraph_id == fragment.paragraph_id
            and previous_fragment.person_key == fragment.person_key
            and previous_fragment.end_offset <= fragment.start_offset
            and _display_gap_is_non_speech(
                paragraph_text[fragment.paragraph_id][
                    previous_fragment.end_offset : fragment.start_offset
                ]
            )
        )
        if can_merge:
            assert previous is not None
            previous["end_offset"] = fragment.end_offset
        else:
            groups.append(
                {
                    "source_id": fragment.source_id,
                    "source_display_name": fragment.source_display_name,
                    "paragraph_id": fragment.paragraph_id,
                    "start_offset": fragment.start_offset,
                    "end_offset": fragment.end_offset,
                }
            )
        previous_fragment = fragment
    return tuple(groups)


def _display_gap_is_non_speech(text: str) -> bool:
    return all(
        character.isspace() or unicodedata.category(character).startswith("P")
        for character in text
    )


def _display_to_canonical_offset(
    display: str,
    canonical: str,
    offset: int,
    *,
    bias: Literal["start", "end"],
) -> int:
    if display == canonical:
        return offset
    if offset < 0 or offset > len(display):
        raise ProjectError("draft editor display offset is out of bounds")
    display_non_punctuation = [
        index
        for index, character in enumerate(display)
        if not unicodedata.category(character).startswith("P")
    ]
    canonical_non_punctuation = [
        index
        for index, character in enumerate(canonical)
        if not unicodedata.category(character).startswith("P")
    ]
    if len(display_non_punctuation) != len(canonical_non_punctuation):
        raise ProjectError("draft editor display does not match canonical text")
    count = sum(index < offset for index in display_non_punctuation)
    if bias == "start":
        return (
            canonical_non_punctuation[count]
            if count < len(canonical_non_punctuation)
            else len(canonical)
        )
    return canonical_non_punctuation[count - 1] + 1 if count > 0 else 0


def _canonical_to_display_offset(
    display: str,
    canonical: str,
    offset: int,
    *,
    bias: Literal["start", "end"] = "start",
) -> int:
    if display == canonical:
        return offset
    if offset < 0 or offset > len(canonical):
        raise ProjectError("draft editor canonical offset is out of bounds")
    canonical_non_punctuation = [
        index
        for index, character in enumerate(canonical)
        if not unicodedata.category(character).startswith("P")
    ]
    display_non_punctuation = [
        index
        for index, character in enumerate(display)
        if not unicodedata.category(character).startswith("P")
    ]
    if len(display_non_punctuation) != len(canonical_non_punctuation):
        raise ProjectError("draft editor display does not match canonical text")
    count = sum(index < offset for index in canonical_non_punctuation)
    if bias == "end":
        return display_non_punctuation[count - 1] + 1 if count > 0 else 0
    return display_non_punctuation[count] if count < len(display_non_punctuation) else len(display)


def _source_display_point(caret: CaretPosition) -> dict[str, object]:
    return {
        "paragraph_id": caret.paragraph_id,
        "character_offset": caret.character_offset,
        "utf16_offset": caret.utf16_offset,
    }


def _draft_display_point(
    snapshot: DraftEditorSnapshot,
    selected_spans: Sequence[_DraftSpan],
    caret: CaretPosition,
    *,
    bias: Literal["start", "end"],
) -> dict[str, object]:
    candidates = [
        span
        for span in selected_spans
        if span.source is not None
        and span.source.original_paragraph_id == caret.paragraph_id
        and span.source.original_start_offset
        <= caret.character_offset
        <= span.source.original_end_offset
    ]
    if not candidates:
        raise ProjectError("draft editor resolved selection is outside candidate content")
    if bias == "start":
        span = next(
            (
                item
                for item in candidates
                if item.source is not None
                and item.source.original_start_offset <= caret.character_offset
                < item.source.original_end_offset
            ),
            candidates[0],
        )
    else:
        span = next(
            (
                item
                for item in reversed(candidates)
                if item.source is not None
                and item.source.original_start_offset < caret.character_offset
                <= item.source.original_end_offset
            ),
            candidates[-1],
        )
    assert span.source is not None
    character_offset = span.start_offset + _canonical_to_display_offset(
        span.source.display_text,
        span.source.ref.canonical_text,
        caret.character_offset - span.source.original_start_offset,
        bias=bias,
    )
    paragraph = _find_draft_paragraph(snapshot, span.paragraph_id)
    return {
        "paragraph_id": span.paragraph_id,
        "character_offset": character_offset,
        "utf16_offset": codepoint_to_utf16_offset(
            str(paragraph["text"]),
            character_offset,
        ),
    }


def _span_for_boundary(
    spans: Sequence[_DraftSpan],
    offset: int,
    *,
    bias: Literal["start", "end"],
) -> _DraftSpan:
    containing = [
        span
        for span in spans
        if span.start_offset < offset < span.end_offset
    ]
    if containing:
        return containing[0]
    if bias == "start":
        following = [span for span in spans if span.start_offset == offset]
        if following:
            return following[0]
        preceding = [span for span in spans if span.end_offset == offset]
        if preceding:
            return preceding[-1]
    else:
        preceding = [span for span in spans if span.end_offset == offset]
        if preceding:
            return preceding[-1]
        following = [span for span in spans if span.start_offset == offset]
        if following:
            return following[0]
    raise ProjectError("draft editor endpoint does not map to candidate content")


def _document_boundary_index(
    spans: Sequence[_DraftSpan],
    offset: int,
    item_count: int,
) -> int:
    containing = [
        span.item_index
        for span in spans
        if span.start_offset < offset < span.end_offset
    ]
    if containing:
        return containing[0]
    following = [span.item_index for span in spans if span.start_offset >= offset]
    if following:
        return min(following)
    preceding = [span.item_index for span in spans if span.end_offset <= offset]
    return min(item_count, max(preceding, default=-1) + 1)


def _caret_inside_selection(
    selection: DraftEditorSelection,
    caret: DraftEditorCaret,
) -> bool:
    if selection.narration_block_id is not None:
        if selection.item_start is None or selection.item_end is None:
            return False
        return selection.item_start <= caret.document_index < selection.item_end
    source_caret = caret.source_caret
    if source_caret is None:
        return False
    resolution = cast(ContinuousSelectionResolution, selection.resolution)
    tick = (
        source_caret.right.start_ticks
        if source_caret.right is not None
        else source_caret.left.end_ticks  # type: ignore[union-attr]
    )

    def same_neighbor(ref: ResolvedSelectionRef, neighbor: CaretNeighbor) -> bool:
        return (
            ref.source_id,
            ref.transcript_version_id,
            ref.segment_id,
        ) == (
            neighbor.source_id,
            neighbor.transcript_version_id,
            neighbor.segment_id,
        )

    if any(
        same_neighbor(ref, neighbor)
        and ref.start_ticks < tick < ref.end_ticks
        for ref in resolution.refs
        for neighbor in (source_caret.left, source_caret.right)
        if neighbor is not None
    ):
        return True

    def contained(neighbor: CaretNeighbor | None) -> bool:
        return neighbor is not None and any(
            same_neighbor(ref, neighbor)
            and ref.start_ticks <= neighbor.start_ticks
            and neighbor.end_ticks <= ref.end_ticks
            for ref in resolution.refs
        )

    return contained(source_caret.left) and contained(source_caret.right)


def _single_person_key(
    snapshot: DraftEditorSnapshot,
    refs: Sequence[ResolvedSelectionRef],
) -> tuple[str, ...]:
    project = ProjectStore(snapshot.workflow.project_path).load()
    keys: set[tuple[str, ...]] = set()
    for ref in refs:
        transcript = _read_transcript(
            snapshot.workflow.project_path,
            ref.source_id,
            ref.transcript_version_id,
        )
        segment = _find_segment(transcript, ref.segment_id)
        keys.add(
            _person_identity(
                project,
                ref.source_id,
                ref.transcript_version_id,
                segment,
            )[0]
        )
    if len(keys) != 1:
        raise ProjectError("draft editor selection cannot cross a person")
    return next(iter(keys))


def _person_identity(
    project: Project,
    source_id: str,
    transcript_id: str,
    segment: TranscriptSegment,
) -> tuple[tuple[str, ...], str | None, str | None, str | None]:
    local = segment.local_speaker_id
    mapping = next(
        (
            item
            for item in project.speaker_maps
            if item.source_id == source_id
            and item.transcript_version_id == transcript_id
            and item.local_speaker_id == local
            and item.confirmed_by_user
        ),
        None,
    )
    if mapping is not None:
        person = next(
            (item for item in project.persons if item.person_id == mapping.person_id),
            None,
        )
        if person is None:
            raise ProjectError("draft editor Speaker Map person is missing")
        return ("person", person.person_id), person.person_id, person.name, person.role
    identity = local or segment.segment_id
    return ("local", source_id, transcript_id, identity), None, None, None


def _working_atoms(snapshot: DraftEditorSnapshot) -> tuple[_WorkingAtom, ...]:
    return tuple(
        _working_atom(item, index) for index, item in enumerate(snapshot._items)
    )


def _working_atom(item: _DraftItem, index: int) -> _WorkingAtom:
    if isinstance(item, _NarrationItem):
        return _WorkingAtom(
            None,
            item.block,
            index,
            item.section_title,
            item.section_origin,
            item.paragraph_key,
        )
    return _WorkingAtom(
        item.ref,
        None,
        index,
        item.section_title,
        item.section_origin,
        item.paragraph_key,
        item.display_text,
    )


def _raw_display_point(
    snapshot: DraftEditorSnapshot,
    paragraph_id: str,
    character_offset: int,
) -> dict[str, object]:
    paragraph = _find_draft_paragraph(snapshot, paragraph_id)
    text = str(paragraph["text"])
    if character_offset < 0 or character_offset > len(text):
        raise ProjectError("draft editor display point is out of bounds")
    return {
        "paragraph_id": paragraph_id,
        "character_offset": character_offset,
        "utf16_offset": codepoint_to_utf16_offset(text, character_offset),
    }


def _display_range_for_span(
    snapshot: DraftEditorSnapshot,
    selection: DraftEditorSelection,
    span: _DraftSpan,
) -> tuple[int, int]:
    positions = {
        str(item["paragraph_id"]): index
        for index, item in enumerate(snapshot.paragraphs)
    }

    def point(value: dict[str, object]) -> tuple[int, int]:
        paragraph_id = value.get("paragraph_id")
        offset = value.get("character_offset")
        if not isinstance(paragraph_id, str) or not isinstance(offset, int):
            raise ProjectError("draft editor display range is invalid")
        if paragraph_id not in positions:
            raise ProjectError("draft editor display range paragraph is stale")
        return positions[paragraph_id], offset

    start, end = sorted(
        (point(selection.display_anchor), point(selection.display_focus))
    )
    paragraph_position = positions.get(span.paragraph_id)
    if paragraph_position is None:
        raise ProjectError("draft editor source span paragraph is stale")
    span_start = (paragraph_position, span.start_offset)
    span_end = (paragraph_position, span.end_offset)
    if end <= span_start or start >= span_end:
        raise ProjectError("draft editor selection does not cover source span")
    local_start = (
        0
        if start <= span_start
        else start[1] - span.start_offset
        if start[0] == paragraph_position
        else span.start_offset
    )
    local_end = (
        span.end_offset - span.start_offset
        if end >= span_end
        else end[1] - span.start_offset
        if end[0] == paragraph_position
        else 0
    )
    if not 0 <= local_start <= local_end <= span.end_offset - span.start_offset:
        raise ProjectError("draft editor display range is disordered")
    return local_start, local_end


def _nth_non_punctuation_offset(value: str, nth: int) -> int | None:
    """Return the canonical offset of the nth non-punctuation character (1-based)."""
    count = 0
    for index, character in enumerate(value):
        if unicodedata.category(character).startswith("P"):
            continue
        count += 1
        if count == nth:
            return index
    return None


def _display_parts_for_selection(
    snapshot: DraftEditorSnapshot,
    selection: DraftEditorSelection,
    atom: _WorkingAtom,
    left_ref: ResolvedSelectionRef | None,
    selected_ref: ResolvedSelectionRef,
    right_ref: ResolvedSelectionRef | None,
) -> tuple[tuple[str | None, ...], str, str]:
    if atom.display_text is None:
        return (
            tuple(
                None
                for _ in (left_ref, selected_ref, right_ref)
                if _ is not None
            ),
            "",
            "",
        )
    span = next(
        (
            candidate
            for candidate in snapshot._spans
            if candidate.item_index == atom.origin_item_index
            and candidate.source is not None
        ),
        None,
    )
    if span is None:
        raise ProjectError("draft editor source display span is stale")
    local_start, local_end = _display_range_for_span(snapshot, selection, span)
    canonical_parts = tuple(
        ref.canonical_text
        for ref in (left_ref, selected_ref, right_ref)
        if ref is not None
    )
    if punctuation_stripped("".join(canonical_parts)) != punctuation_stripped(
        atom.ref.canonical_text  # type: ignore[union-attr]
    ):
        raise ProjectError("draft editor selection refs do not partition source text")
    canonical = atom.ref.canonical_text  # type: ignore[union-attr]
    left_count = (
        len(punctuation_stripped(left_ref.canonical_text))
        if left_ref is not None
        else 0
    )
    canonical_start = _nth_non_punctuation_offset(canonical, left_count + 1)
    if canonical_start is None:
        canonical_start = len(canonical)
    canonical_end = _nth_non_punctuation_offset(
        canonical,
        left_count + len(punctuation_stripped(selected_ref.canonical_text)) + 1,
    )
    if canonical_end is None:
        canonical_end = len(canonical)
    mapped_start = _canonical_to_display_offset(
        atom.display_text,
        atom.ref.canonical_text,  # type: ignore[union-attr]
        canonical_start,
    )
    mapped_end = _canonical_to_display_offset(
        atom.display_text,
        atom.ref.canonical_text,  # type: ignore[union-attr]
        canonical_end,
    )

    def non_punctuation_before(value: str, offset: int) -> int:
        return sum(
            not unicodedata.category(character).startswith("P")
            for character in value[:offset]
        )

    if non_punctuation_before(atom.display_text, local_start) != non_punctuation_before(
        atom.ref.canonical_text, canonical_start  # type: ignore[union-attr]
    ):
        local_start = mapped_start
    if non_punctuation_before(atom.display_text, local_end) != non_punctuation_before(
        atom.ref.canonical_text, canonical_end  # type: ignore[union-attr]
    ):
        local_end = mapped_end
    fragments = (
        atom.display_text[:local_start],
        atom.display_text[local_start:local_end],
        atom.display_text[local_end:],
    )
    expected = canonical_parts
    actual = tuple(
        fragment
        for index, fragment in enumerate(fragments)
        if (index == 0 and left_ref is not None)
        or (index == 1)
        or (index == 2 and right_ref is not None)
    )
    if len(actual) != len(expected) or any(
        punctuation_stripped(fragment) != punctuation_stripped(canonical)
        for fragment, canonical in zip(actual, expected)
    ):
        raise ProjectError("draft editor selection cannot uniquely retain display punctuation")
    prefix = fragments[0] if left_ref is None else ""
    suffix = fragments[2] if right_ref is None else ""
    if punctuation_stripped(prefix) or punctuation_stripped(suffix):
        raise ProjectError("draft editor selection cannot uniquely retain display punctuation")
    return actual, prefix, suffix


def _caret_boundary_from_ticks(
    snapshot: DraftEditorSnapshot,
    atom: _WorkingAtom,
    left_ref: ResolvedSelectionRef | None,
    right_ref: ResolvedSelectionRef | None,
) -> int:
    """Locate the caret cut inside the atom by fine-unit tick offsets.

    The right half's first fine unit starts at its span start offset in the
    segment display text; the cut in this atom's canonical is that offset
    relative to the atom's own first unit offset.  Left-boundary carets
    (no right half) use the left half's last unit end offset the same way.
    """
    basis = cast(ResolvedSelectionRef, atom.ref)
    segment = _find_segment(
        snapshot.transcript_base.transcripts[(basis.source_id, basis.transcript_version_id)],
        basis.segment_id,
    )
    spans = exact_fine_unit_spans(segment) or trusted_fine_unit_spans(segment)
    if spans is None:
        raise ProjectError("draft editor caret split lacks trusted fine units")
    basis_start = next(
        (
            span.start_offset
            for span in spans
            if span.unit.start_ticks == basis.start_ticks
        ),
        None,
    )
    if basis_start is None:
        raise ProjectError("draft editor caret atom start is not on a fine unit")
    if right_ref is not None:
        boundary_tick = right_ref.start_ticks
    elif left_ref is not None:
        boundary_tick = left_ref.end_ticks
    else:
        return 0
    boundary_offset = next(
        (
            span.start_offset
            for span in spans
            if span.unit.start_ticks == boundary_tick
        ),
        None,
    )
    if boundary_offset is None:
        # The right half starts at the atom's own end (caret at the very
        # end of the atom): the cut is the atom's full length.
        if right_ref is not None and right_ref.end_ticks == basis.end_ticks:
            return len(punctuation_stripped(basis.canonical_text))
        raise ProjectError("draft editor caret boundary is not on a fine unit")
    return boundary_offset - basis_start


def _display_parts_for_caret(
    snapshot: DraftEditorSnapshot,
    atom: _WorkingAtom,
    caret: DraftEditorCaret,
    left_ref: ResolvedSelectionRef | None,
    right_ref: ResolvedSelectionRef | None,
) -> tuple[str | None, ...]:
    if atom.display_text is None:
        return tuple(None for _ in (left_ref, right_ref) if _ is not None)
    span = next(
        (
            candidate
            for candidate in snapshot._spans
            if candidate.item_index == atom.origin_item_index
            and candidate.source is not None
        ),
        None,
    )
    if span is None:
        raise ProjectError("draft editor source display span is stale")
    if span.paragraph_id != caret.paragraph_id:
        raise ProjectError("draft editor caret paragraph does not match source span")
    canonical = atom.ref.canonical_text  # type: ignore[union-attr]
    # The caret boundary ticks come from the transcript, so left_ref/right_ref
    # are the left/right halves of THIS atom's canonical when the caret falls
    # inside the atom (both sides on the same segment), or the neighboring
    # atom's text otherwise.  Inside the atom the cut must be located by the
    # fine-unit ticks, never by text search: repeated speech makes the first
    # text match ambiguous (spec 5.2.6).  The right half's first fine unit
    # starts at its span start offset in the segment display text; the cut in
    # this atom's canonical is that offset relative to the atom's own start.
    same_segment = (
        left_ref is not None
        and right_ref is not None
        and left_ref.source_id == atom.ref.source_id  # type: ignore[union-attr]
        and left_ref.transcript_version_id == atom.ref.transcript_version_id  # type: ignore[union-attr]
        and left_ref.segment_id == atom.ref.segment_id  # type: ignore[union-attr]
        and right_ref.source_id == atom.ref.source_id  # type: ignore[union-attr]
        and right_ref.transcript_version_id == atom.ref.transcript_version_id  # type: ignore[union-attr]
        and right_ref.segment_id == atom.ref.segment_id  # type: ignore[union-attr]
    )
    if same_segment:
        canonical_boundary = _caret_boundary_from_ticks(
            snapshot,
            atom,
            left_ref,
            right_ref,
        )
    else:
        left_count = (
            len(punctuation_stripped(left_ref.canonical_text))
            if left_ref is not None
            else 0
        )
        candidate_boundary = _nth_non_punctuation_offset(canonical, left_count + 1)
        canonical_boundary = (
            len(canonical) if candidate_boundary is None else candidate_boundary
        )
    local = _canonical_to_display_offset(atom.display_text, canonical, canonical_boundary)
    if not 0 <= local <= len(atom.display_text):
        raise ProjectError("draft editor caret is outside source display span")
    if left_ref is None and local != 0:
        raise ProjectError("draft editor caret leaves a punctuation prefix orphaned")
    if right_ref is None and local != len(atom.display_text):
        raise ProjectError("draft editor caret leaves a punctuation suffix orphaned")
    fragments = (
        atom.display_text[:local],
        atom.display_text[local:],
    )
    # Fine-unit boundaries skip gap whitespace between units (e.g. the
    # space before a Latin word).  Inside this atom that whitespace is not
    # part of either caret half's canonical text, so trim it from the left
    # half's display fragment: the fragment must match the ref's canonical
    # text, which itself has no gap whitespace.
    if same_segment and left_ref is not None and fragments[0]:
        fragments = (fragments[0].rstrip(), fragments[1])
    expected = tuple(
        ref.canonical_text
        for ref in (left_ref, right_ref)
        if ref is not None
    )
    actual = tuple(
        fragment
        for index, fragment in enumerate(fragments)
        if (index == 0 and left_ref is not None)
        or (index == 1 and right_ref is not None)
    )
    if len(actual) != len(expected) or any(
        punctuation_stripped(fragment) != punctuation_stripped(canonical)
        for fragment, canonical in zip(actual, expected)
    ):
        raise ProjectError("draft editor caret cannot uniquely retain display punctuation")
    return actual


def _split_for_selection(
    snapshot: DraftEditorSnapshot,
    atoms: tuple[_WorkingAtom, ...],
    selection: DraftEditorSelection,
) -> tuple[tuple[_WorkingAtom, ...], tuple[int, ...]]:
    if selection.item_start is None or selection.item_end is None:
        raise ProjectError("draft selection location is missing")
    if selection.narration_block_id is not None:
        narration_indices = tuple(
            index
            for index, atom in enumerate(atoms)
            if atom.narration is not None
            and atom.narration.block_id == selection.narration_block_id
        )
        if not narration_indices:
            raise ProjectError("draft narration selection block is stale")
        return atoms, narration_indices
    if selection.resolution is None:
        raise ProjectError("draft source selection resolution is missing")
    selected_refs = selection.resolution.refs
    updated: list[_WorkingAtom] = []
    selected_indices: list[int] = []
    pending_punctuation: list[tuple[int, str, str]] = []
    for atom in atoms:
        if (
            atom.ref is None
            or atom.origin_item_index < selection.item_start
            or atom.origin_item_index >= selection.item_end
        ):
            updated.append(atom)
            continue
        overlaps = [
            ref
            for ref in selected_refs
            if _same_segment(atom.ref, ref)
            and ref.start_ticks < atom.ref.end_ticks
            and ref.end_ticks > atom.ref.start_ticks
        ]
        if not overlaps:
            updated.append(atom)
            continue
        if len(overlaps) != 1:
            raise ProjectError("draft selection refs are not continuous")
        selected = overlaps[0]
        if (
            selected.start_ticks < atom.ref.start_ticks
            or selected.end_ticks > atom.ref.end_ticks
        ):
            raise ProjectError("draft selection exceeds candidate refs")
        left_ref: ResolvedSelectionRef | None = None
        right_ref: ResolvedSelectionRef | None = None
        if (
            selected == selected_refs[0]
            and atom.ref.start_ticks < selected.start_ticks
        ):
            _trusted_selection_boundary(
                snapshot,
                selected,
                selection.resolution.start_caret,
                side="left",
            )
        if selected == selected_refs[-1] and selected.end_ticks < atom.ref.end_ticks:
            _trusted_selection_boundary(
                snapshot,
                selected,
                selection.resolution.end_caret,
                side="right",
            )
        left_ref = (
            _selection_remainder_ref(snapshot, atom.ref, selected, side="left")
            if atom.ref.start_ticks < selected.start_ticks
            else None
        )
        right_ref = (
            _selection_remainder_ref(snapshot, atom.ref, selected, side="right")
            if selected.end_ticks < atom.ref.end_ticks
            else None
        )
        display_parts, prefix, suffix = _display_parts_for_selection(
            snapshot,
            selection,
            atom,
            left_ref,
            selected,
            right_ref,
        )
        display_index = 0
        if left_ref is not None:
            updated.append(
                replace(
                    atom,
                    ref=left_ref,
                    display_text=display_parts[display_index],
                )
            )
            display_index += 1
        selected_index = len(updated)
        selected_indices.append(selected_index)
        updated.append(
            replace(
                atom,
                ref=selected,
                display_text=display_parts[display_index],
            )
        )
        display_index += 1
        if right_ref is not None:
            updated.append(
                replace(
                    atom,
                    ref=right_ref,
                    display_text=display_parts[display_index],
                )
            )
        if prefix or suffix:
            pending_punctuation.append((selected_index, prefix, suffix))
    actual = tuple(
        updated[index].ref for index in selected_indices if updated[index].ref is not None
    )
    if actual != selected_refs:
        raise ProjectError("draft selection refs do not match candidate occurrence")
    if selected_indices != list(
        range(selected_indices[0], selected_indices[0] + len(selected_indices))
    ):
        raise ProjectError("draft selection is not continuous")
    _attach_unselected_selection_punctuation(
        updated,
        tuple(selected_indices),
        tuple(pending_punctuation),
    )
    return tuple(updated), tuple(selected_indices)


def _selection_remainder_ref(
    snapshot: DraftEditorSnapshot,
    parent: ResolvedSelectionRef,
    selected: ResolvedSelectionRef,
    *,
    side: Literal["left", "right"],
) -> ResolvedSelectionRef | None:
    segment = _find_segment(
        snapshot.transcript_base.transcripts[
            (parent.source_id, parent.transcript_version_id)
        ],
        parent.segment_id,
    )
    spans = trusted_fine_unit_spans(segment)
    if spans is None:
        raise ProjectError("draft editor selection remainder lacks trusted fine units")
    remainder = [
        span
        for span in spans
        if parent.start_ticks <= span.unit.start_ticks
        and span.unit.end_ticks <= parent.end_ticks
        and (
            span.unit.end_ticks <= selected.start_ticks
            if side == "left"
            else selected.end_ticks <= span.unit.start_ticks
        )
    ]
    if not remainder:
        return None
    return _resolved_range(
        snapshot,
        parent,
        remainder[0].unit.start_ticks,
        remainder[-1].unit.end_ticks,
    )


def _attach_unselected_selection_punctuation(
    atoms: list[_WorkingAtom],
    selected_indices: tuple[int, ...],
    pending: tuple[tuple[int, str, str], ...],
) -> None:
    selected = set(selected_indices)
    for index, prefix, suffix in pending:
        for text, directions in ((suffix, (-1, 1)), (prefix, (1, -1))):
            if not text:
                continue
            target: int | None = None
            for step in directions:
                candidate = index + step
                while 0 <= candidate < len(atoms):
                    atom = atoms[candidate]
                    if (
                        atom.narration is not None
                        or atom.section_origin != atoms[index].section_origin
                        or (
                            atom.ref is not None
                            and atom.paragraph_key != atoms[index].paragraph_key
                        )
                    ):
                        break
                    if candidate in selected or atom.ref is None:
                        candidate += step
                        continue
                    target = candidate
                    break
                if target is not None:
                    break
            if target is None:
                raise ProjectError("draft editor selection leaves a punctuation orphan")
            atom = atoms[target]
            existing = atom.display_text or atom.ref.canonical_text  # type: ignore[union-attr]
            atoms[target] = replace(
                atom,
                display_text=(text + existing if target > index else existing + text),
            )


def _locate_selected_atoms(
    atoms: tuple[_WorkingAtom, ...],
    selection: DraftEditorSelection,
) -> tuple[int, ...]:
    if selection.item_start is None or selection.item_end is None:
        raise ProjectError("draft selection location is missing")
    if selection.narration_block_id is not None:
        result = tuple(
            index
            for index, atom in enumerate(atoms)
            if atom.narration is not None
            and atom.narration.block_id == selection.narration_block_id
        )
        if not result:
            raise ProjectError("draft narration selection block is stale")
        return result
    if selection.resolution is None:
        raise ProjectError("draft source selection resolution is missing")
    refs = selection.resolution.refs
    matches: list[tuple[int, ...]] = []
    for start in range(len(atoms) - len(refs) + 1):
        indices = tuple(range(start, start + len(refs)))
        selected = tuple(atoms[index] for index in indices)
        if (
            all(
                atom.ref is not None
                and selection.item_start <= atom.origin_item_index < selection.item_end
                for atom in selected
            )
            and tuple(atom.ref for atom in selected) == refs
        ):
            matches.append(indices)
    if len(matches) != 1:
        raise ProjectError("draft selection occurrence is no longer unique")
    return matches[0]


def _split_for_caret(
    snapshot: DraftEditorSnapshot,
    atoms: tuple[_WorkingAtom, ...],
    caret: DraftEditorCaret,
) -> tuple[tuple[_WorkingAtom, ...], int]:
    if caret.candidate_id != snapshot.candidate.content_draft_id:
        raise ProjectError("draft editor caret is stale")
    if caret.source_caret is None:
        boundary_index = next(
            (
                index
                for index, atom in enumerate(atoms)
                if atom.origin_item_index >= caret.document_index
            ),
            len(atoms),
        )
        return atoms, boundary_index
    neighbor = caret.source_caret.right or caret.source_caret.left
    if neighbor is None:
        raise ProjectError("draft editor caret has no source boundary")
    boundary_pair = (
        _trusted_boundary_pair(snapshot, caret.source_caret)
        if caret.source_caret.left is not None and caret.source_caret.right is not None
        else _TrustedBoundaryPair(
            None
            if caret.source_caret.left is None
            else caret.source_caret.left.end_ticks,
            None
            if caret.source_caret.right is None
            else caret.source_caret.right.start_ticks,
        )
    )
    tick = (
        boundary_pair.right_start_ticks
        if caret.source_caret.right is not None
        else boundary_pair.left_end_ticks
    )
    if tick is None:
        raise ProjectError("draft editor caret has no trusted source boundary")
    updated: list[_WorkingAtom] = []
    source_boundary: int | None = None
    for atom in atoms:
        if (
            atom.ref is not None
            and atom.origin_item_index == caret.document_index
            and atom.ref.source_id == neighbor.source_id
            and atom.ref.transcript_version_id == neighbor.transcript_version_id
            and atom.ref.segment_id == neighbor.segment_id
            and atom.ref.start_ticks <= tick <= atom.ref.end_ticks
        ):
            if caret.source_caret.left is not None and caret.source_caret.right is not None:
                left_end_ticks = boundary_pair.left_end_ticks
                right_start_ticks = boundary_pair.right_start_ticks
                if left_end_ticks is None or right_start_ticks is None:
                    raise ProjectError("draft editor caret boundary sides are incomplete")
                if left_end_ticks > right_start_ticks:
                    raise ProjectError("draft editor caret boundary sides are disordered")
            else:
                left_end_ticks = None
                right_start_ticks = boundary_pair.right_start_ticks
                if caret.source_caret.left is not None:
                    left_end_ticks = boundary_pair.left_end_ticks
                    right_start_ticks = None
            left_ref = (
                _resolved_range(
                    snapshot,
                    atom.ref,
                    atom.ref.start_ticks,
                    left_end_ticks,
                )
                if left_end_ticks is not None and atom.ref.start_ticks < left_end_ticks
                else None
            )
            right_ref = (
                _resolved_range(
                    snapshot,
                    atom.ref,
                    right_start_ticks,
                    atom.ref.end_ticks,
                )
                if right_start_ticks is not None and right_start_ticks < atom.ref.end_ticks
                else None
            )
            display_parts = _display_parts_for_caret(
                snapshot,
                atom,
                caret,
                left_ref,
                right_ref,
            )
            if left_ref is not None:
                updated.append(
                    replace(
                        atom,
                        ref=left_ref,
                        display_text=display_parts[0],
                    )
                )
            source_boundary = len(updated)
            if right_ref is not None:
                updated.append(
                    replace(
                        atom,
                        ref=right_ref,
                        display_text=display_parts[-1],
                    )
                )
        else:
            if (
                source_boundary is None
                and atom.origin_item_index >= caret.document_index
            ):
                source_boundary = len(updated)
            updated.append(atom)
    if source_boundary is None:
        source_boundary = len(updated)
    return tuple(updated), source_boundary


def _section_for_caret(
    snapshot: DraftEditorSnapshot,
    caret: DraftEditorCaret,
) -> tuple[str | None, str | None]:
    """Resolve the destination section from the displayed caret identity.

    Heading blocks are not editor atoms, so a caret at an empty heading needs
    the heading paragraph itself to carry the destination identity.  Ordinary
    paragraphs resolve through the span selected by the same start-biased
    boundary rule used by the core caret resolver.
    """

    paragraph = _find_draft_paragraph(snapshot, caret.paragraph_id)
    if paragraph.get("kind") == "section_title":
        heading_id = paragraph.get("block_id")
        if not isinstance(heading_id, str):
            raise ProjectError("draft editor section heading identity is missing")
        heading = next(
            (
                block
                for block in snapshot.candidate.blocks
                if isinstance(block, SectionTitleBlock)
                and block.block_id == heading_id
            ),
            None,
        )
        if heading is None:
            raise ProjectError("draft editor section heading is stale")
        return heading.block_id, heading.title
    spans = tuple(
        span for span in snapshot._spans if span.paragraph_id == caret.paragraph_id
    )
    if not spans:
        raise ProjectError("draft editor destination paragraph has no section")
    span = _span_for_boundary(spans, caret.character_offset, bias="start")
    item = snapshot._items[span.item_index]
    if isinstance(item, _SourceItem):
        return item.section_origin, item.section_title
    if isinstance(item, _NarrationItem):
        return item.section_origin, item.section_title
    raise ProjectError("draft editor destination section is invalid")


def _paragraph_for_caret(
    snapshot: DraftEditorSnapshot,
    caret: DraftEditorCaret,
) -> tuple[str, ...]:
    paragraph = _find_draft_paragraph(snapshot, caret.paragraph_id)
    if paragraph.get("kind") == "source_excerpt":
        spans = tuple(
            span for span in snapshot._spans if span.paragraph_id == caret.paragraph_id
        )
        if not spans:
            raise ProjectError("draft editor destination paragraph has no membership")
        span = _span_for_boundary(spans, caret.character_offset, bias="start")
        item = snapshot._items[span.item_index]
        if not isinstance(item, _SourceItem):
            raise ProjectError("draft editor destination paragraph membership is invalid")
        return item.paragraph_key
    return _source_paragraph_key(
        "block_edit_"
        + _hash_json(
            {
                "candidate": snapshot.candidate.content_draft_id,
                "paragraph": caret.paragraph_id,
                "boundary": caret.boundary_id,
            }
        )[:24]
    )


def _retag_atoms_for_destination(
    snapshot: DraftEditorSnapshot,
    atoms: tuple[_WorkingAtom, ...],
    selected_indices: tuple[int, ...],
    caret: DraftEditorCaret,
    caret_index: int,
) -> tuple[_WorkingAtom, ...]:
    section_origin, section_title = _section_for_caret(snapshot, caret)
    selected = set(selected_indices)
    selected_paragraphs: list[tuple[str, ...]] = []
    for index in selected_indices:
        key = atoms[index].paragraph_key
        if not selected_paragraphs or selected_paragraphs[-1] != key:
            selected_paragraphs.append(key)
    if len(selected_paragraphs) == 1:
        target_paragraph = _paragraph_for_caret(snapshot, caret)
        return tuple(
            replace(
                atom,
                section_origin=section_origin,
                section_title=section_title,
                paragraph_key=target_paragraph,
            )
            if index in selected
            else atom
            for index, atom in enumerate(atoms)
        )

    updated = list(atoms)
    target_paragraph = _paragraph_for_caret(snapshot, caret)
    target_before = any(
        index not in selected
        and index < caret_index
        and atom.paragraph_key == target_paragraph
        for index, atom in enumerate(atoms)
    )
    target_after = any(
        index not in selected
        and index >= caret_index
        and atom.paragraph_key == target_paragraph
        for index, atom in enumerate(atoms)
    )
    if target_before and target_after:
        suffix_key = _derived_move_paragraph_key(
            snapshot,
            caret,
            purpose="target_suffix",
            ordinal=0,
            original=target_paragraph,
            refs=tuple(
                atom.ref
                for index, atom in enumerate(atoms)
                if (
                    index not in selected
                    and index >= caret_index
                    and atom.paragraph_key == target_paragraph
                    and atom.ref is not None
                )
            ),
        )
        for index in range(caret_index, len(updated)):
            if index not in selected and updated[index].paragraph_key == target_paragraph:
                updated[index] = replace(updated[index], paragraph_key=suffix_key)

    for ordinal, paragraph_key in enumerate(selected_paragraphs):
        paragraph_indices = tuple(
            index
            for index in selected_indices
            if atoms[index].paragraph_key == paragraph_key
        )
        has_source_remainder = any(
            index not in selected and atom.paragraph_key == paragraph_key
            for index, atom in enumerate(atoms)
        )
        moved_key = (
            _derived_move_paragraph_key(
                snapshot,
                caret,
                purpose="selected_partial",
                ordinal=ordinal,
                original=paragraph_key,
                refs=tuple(
                    cast(ResolvedSelectionRef, atoms[index].ref)
                    for index in paragraph_indices
                    if atoms[index].ref is not None
                ),
            )
            if has_source_remainder
            else paragraph_key
        )
        for index in paragraph_indices:
            updated[index] = replace(
                updated[index],
                section_origin=section_origin,
                section_title=section_title,
                paragraph_key=moved_key,
            )
    return tuple(updated)


def _derived_move_paragraph_key(
    snapshot: DraftEditorSnapshot,
    caret: DraftEditorCaret,
    *,
    purpose: str,
    ordinal: int,
    original: tuple[str, ...],
    refs: tuple[ResolvedSelectionRef, ...],
) -> tuple[str, ...]:
    return (
        "source",
        _hash_json(
            {
                "candidate": snapshot.candidate.content_draft_id,
                "caret": caret.boundary_id,
                "purpose": purpose,
                "ordinal": ordinal,
                "original": original,
                "refs": [ref.to_dict() for ref in refs],
            }
        )[:24],
    )


def _move_atoms(
    snapshot: DraftEditorSnapshot,
    atoms: tuple[_WorkingAtom, ...],
    selected_indices: tuple[int, ...],
    selection: DraftEditorSelection,
    caret: DraftEditorCaret,
    caret_index: int,
    *,
    accept_degraded: bool,
) -> tuple[_WorkingAtom, ...]:
    if not selected_indices:
        raise ProjectError("draft move selection is empty")
    if selected_indices[0] < caret_index < selected_indices[-1] + 1:
        raise ProjectError("caret cannot be inside the moved selection")
    atoms = _retag_atoms_for_destination(
        snapshot,
        atoms,
        selected_indices,
        caret,
        caret_index,
    )
    if selection.narration_block_id is not None:
        selected = [atoms[index] for index in selected_indices]
        remaining = [
            atom for index, atom in enumerate(atoms) if index not in selected_indices
        ]
        adjusted = caret_index - sum(index < caret_index for index in selected_indices)
        moved = tuple(remaining[:adjusted] + selected + remaining[adjusted:])
        if moved == atoms:
            raise ProjectError("draft narration move does not change the candidate")
        return moved
    if selection.resolution is None:
        raise ProjectError("draft move selection resolution is missing")
    source_refs = tuple(atom.ref for atom in atoms if atom.ref is not None)
    selection_start = sum(
        1 for atom in atoms[: selected_indices[0]] if atom.ref is not None
    )
    source_caret_index = sum(1 for atom in atoms[:caret_index] if atom.ref is not None)
    exact_caret = make_exact_sequence_caret(
        selection.resolution.view_hash,
        caret.paragraph_id,
        source_refs,
        source_caret_index,
    )
    transformed = move_draft_selection_to_caret(
        source_refs,
        selection=selection.resolution,
        selection_start_index=selection_start,
        caret=exact_caret,
        accept_degraded=accept_degraded,
    )
    selected = [atoms[index] for index in selected_indices]
    remaining = [
        atom for index, atom in enumerate(atoms) if index not in selected_indices
    ]
    adjusted = caret_index - sum(index < caret_index for index in selected_indices)
    moved = tuple(remaining[:adjusted] + selected + remaining[adjusted:])
    if tuple(atom.ref for atom in moved if atom.ref is not None) != transformed.refs:
        raise ProjectError("draft move transform did not match exact refs")
    return moved


def _insert_atoms(
    snapshot: DraftEditorSnapshot,
    atoms: tuple[_WorkingAtom, ...],
    selection: DraftEditorSelection,
    caret: DraftEditorCaret,
    caret_index: int,
    *,
    accept_degraded: bool,
) -> tuple[_WorkingAtom, ...]:
    if selection.resolution is None:
        raise ProjectError("draft insert selection resolution is missing")
    source_refs = tuple(atom.ref for atom in atoms if atom.ref is not None)
    source_caret_index = sum(1 for atom in atoms[:caret_index] if atom.ref is not None)
    exact_caret = make_exact_sequence_caret(
        selection.resolution.view_hash,
        caret.paragraph_id,
        source_refs,
        source_caret_index,
    )
    transformed = insert_transcript_selection_at_caret(
        snapshot.workflow.project_path,
        source_bindings=_bindings_payload(snapshot),
        expected_revision=snapshot.workflow.project_revision,
        view_hash=selection.resolution.view_hash,
        draft_refs=source_refs,
        selection=selection.resolution,
        caret=exact_caret,
        accept_degraded=accept_degraded,
    )
    section_origin, section_title = _section_for_caret(snapshot, caret)
    paragraph_key = _paragraph_for_caret(snapshot, caret)
    inserted = [
        _WorkingAtom(
            ref,
            None,
            caret.document_index,
            section_title,
            section_origin,
            paragraph_key,
            placement_marker=True,
        )
        for ref in selection.resolution.refs
    ]
    result = (*atoms[:caret_index], *inserted, *atoms[caret_index:])
    if tuple(atom.ref for atom in result if atom.ref is not None) != transformed.refs:
        raise ProjectError("draft insert transform did not match exact refs")
    return result


def _prepare_candidate(
    snapshot: DraftEditorSnapshot,
    atoms: tuple[_WorkingAtom, ...],
    *,
    operation: str,
    child_id: str,
    placement_operation: Literal["move_selection", "insert_source_refs"] | None = None,
) -> DraftEditorPreparedChild:
    blocks: list[SourceExcerptBlock | NarrationBlock | SectionTitleBlock] = []
    placement_block_ids: list[str] = []
    emitted_sections: set[str] = set()
    emitted_paragraphs: set[tuple[str, ...]] = set()
    previous_paragraph: tuple[str, ...] | None = None
    for index, atom in enumerate(atoms):
        if (
            atom.section_origin is not None
            and atom.section_origin not in emitted_sections
        ):
            if atom.section_title is None:
                raise ProjectError("draft editor section title is missing")
            blocks.append(
                SectionTitleBlock(
                    block_id=atom.section_origin,
                    title=atom.section_title,
                )
            )
            emitted_sections.add(atom.section_origin)
        if atom.ref is not None:
            if (
                len(atom.paragraph_key) != 2
                or atom.paragraph_key[0] != "source"
                or re.fullmatch(r"[0-9a-f]{24}", atom.paragraph_key[1]) is None
            ):
                raise ProjectError("draft editor paragraph membership is invalid")
            if atom.paragraph_key != previous_paragraph:
                if atom.paragraph_key in emitted_paragraphs:
                    raise ProjectError("draft editor paragraph membership is discontiguous")
                emitted_paragraphs.add(atom.paragraph_key)
            previous_paragraph = atom.paragraph_key
            block_id = (
                f"block_edit_p_{atom.paragraph_key[1]}_r_"
                + _hash_json(
                    {
                        "parent": snapshot.candidate.content_draft_id,
                        "operation": operation,
                        "index": index,
                        "ref": atom.ref.to_dict(),
                    }
                )[:16]
            )
            if atom.placement_marker:
                placement_block_ids.append(block_id)
            blocks.append(
                SourceExcerptBlock(
                    block_id=block_id,
                    refs=(
                        ContentDraftRef(
                            atom.ref.source_id,
                            atom.ref.transcript_version_id,
                            atom.ref.segment_id,
                            atom.ref.start_ticks,
                            atom.ref.end_ticks,
                        ),
                    ),
                    canonical_text=atom.ref.canonical_text,
                    display_text=atom.display_text,
                )
            )
        elif atom.narration is not None:
            previous_paragraph = None
            blocks.append(atom.narration)
        else:
            raise ProjectError("draft editor atom is invalid")
    empty_headings: list[tuple[SectionTitleBlock, str | None]] = []
    for index, block in enumerate(snapshot.candidate.blocks):
        if not isinstance(block, SectionTitleBlock) or block.block_id in emitted_sections:
            continue
        next_emitted: str | None = None
        for following in snapshot.candidate.blocks[index + 1 :]:
            if isinstance(following, SectionTitleBlock) and following.block_id in emitted_sections:
                next_emitted = following.block_id
                break
        empty_headings.append((block, next_emitted))
    for heading, next_emitted in empty_headings:
        if next_emitted is None:
            blocks.append(heading)
        else:
            insert_at = next(
                index
                for index, item in enumerate(blocks)
                if isinstance(item, SectionTitleBlock) and item.block_id == next_emitted
            )
            blocks.insert(insert_at, heading)
    if not blocks:
        raise ProjectError("draft editor candidate cannot be empty")
    child = prepare_content_draft_editor_child(
        parent=snapshot.candidate,
        blocks=tuple(blocks),
        child_id=child_id,
        display_ownership_resolved=True,
    )
    placement = (
        DraftEditorPlacementResult(
            placement_operation,
            child.content_draft_id,
            tuple(placement_block_ids),
        )
        if placement_operation is not None
        else None
    )
    return DraftEditorPreparedChild(child, placement)


def _trusted_boundary_pair(
    snapshot: DraftEditorSnapshot,
    caret: CaretPosition,
    *,
    basis: ResolvedSelectionRef | ContentDraftRef | None = None,
) -> _TrustedBoundaryPair:
    if caret.view_hash != snapshot._view_hash or caret.degraded:
        raise ProjectError("draft editor caret is not trusted")
    left = caret.left
    right = caret.right
    if left is None and right is None:
        raise ProjectError("draft editor caret has no source neighbors")
    expected_key = (
        None
        if basis is None
        else (basis.source_id, basis.transcript_version_id, basis.segment_id)
    )
    for neighbor in (left, right):
        if neighbor is None:
            continue
        if (
            neighbor.source_id != caret.source_id
            or neighbor.transcript_version_id != caret.transcript_version_id
        ):
            raise ProjectError("draft editor caret source identity is invalid")
        neighbor_key = (
            neighbor.source_id,
            neighbor.transcript_version_id,
            neighbor.segment_id,
        )
        if expected_key is not None and neighbor_key != expected_key:
            raise ProjectError("draft editor caret source identity is invalid")
    if left is not None and right is not None and (
        left.source_id,
        left.transcript_version_id,
        left.segment_id,
    ) != (
        right.source_id,
        right.transcript_version_id,
        right.segment_id,
    ):
        raise ProjectError("draft editor caret source neighbors are not adjacent")
    source_neighbor = right or left
    assert source_neighbor is not None
    transcript = snapshot.transcript_base.transcripts[
        (source_neighbor.source_id, source_neighbor.transcript_version_id)
    ]
    segment = _find_segment(transcript, source_neighbor.segment_id)
    spans = trusted_fine_unit_spans(segment)
    if spans is None:
        raise ProjectError("draft editor caret does not have trusted fine units")

    def span_position(neighbor: CaretNeighbor) -> int:
        if neighbor.fine_unit_index is None:
            raise ProjectError("draft editor caret fine-unit identity is missing")
        matches = [
            index
            for index, span in enumerate(spans)
            if span.fine_unit_index == neighbor.fine_unit_index
        ]
        if len(matches) != 1:
            raise ProjectError("draft editor caret fine-unit identity is invalid")
        span = spans[matches[0]]
        if (
            span.unit.start_ticks != neighbor.start_ticks
            or span.unit.end_ticks != neighbor.end_ticks
        ):
            raise ProjectError("draft editor caret fine-unit ticks are invalid")
        return matches[0]

    left_position = None if left is None else span_position(left)
    right_position = None if right is None else span_position(right)
    if (
        left_position is not None
        and right_position is not None
        and right_position != left_position + 1
    ):
        raise ProjectError("draft editor caret neighbors are not adjacent trusted units")
    return _TrustedBoundaryPair(
        None if left is None else left.end_ticks,
        None if right is None else right.start_ticks,
    )


def _trusted_selection_boundary(
    snapshot: DraftEditorSnapshot,
    ref: ResolvedSelectionRef,
    caret: CaretPosition,
    *,
    side: Literal["left", "right"],
) -> int:
    boundary = _trusted_boundary_pair(snapshot, caret, basis=ref)
    if side == "left":
        if (
            caret.right is None
            or ref.fine_unit_start_index is None
            or caret.right.fine_unit_index != ref.fine_unit_start_index
            or boundary.right_start_ticks != ref.start_ticks
        ):
            raise ProjectError("draft selection left boundary identity is invalid")
        return ref.start_ticks
    if (
        caret.left is None
        or ref.fine_unit_end_index is None
        or caret.left.fine_unit_index != ref.fine_unit_end_index - 1
        or boundary.left_end_ticks != ref.end_ticks
    ):
        raise ProjectError("draft selection right boundary identity is invalid")
    return ref.end_ticks


def _resolved_range(
    snapshot: DraftEditorSnapshot,
    basis: ResolvedSelectionRef,
    start_ticks: int,
    end_ticks: int,
) -> ResolvedSelectionRef:
    transcript = snapshot.transcript_base.transcripts[
        (basis.source_id, basis.transcript_version_id)
    ]
    segment = _find_segment(transcript, basis.segment_id)
    spans = exact_fine_unit_spans(segment) or trusted_fine_unit_spans(segment)
    fine_start = next(
        (
            span.fine_unit_index
            for span in spans or ()
            if span.unit.start_ticks == start_ticks
        ),
        None,
    )
    fine_end_value = next(
        (
            span.fine_unit_index + 1
            for span in spans or ()
            if span.unit.end_ticks == end_ticks
        ),
        None,
    )
    if (
        (start_ticks != segment.start_ticks and fine_start is None)
        or (end_ticks != segment.end_ticks and fine_end_value is None)
    ):
        raise ProjectError("draft editor range is not on trusted fine-unit boundaries")
    return ResolvedSelectionRef(
        basis.source_id,
        basis.transcript_version_id,
        basis.segment_id,
        start_ticks,
        end_ticks,
        canonical_text_for_range(segment, start_ticks, end_ticks),
        fine_start,
        fine_end_value,
    )


def _range_offsets(
    segment: TranscriptSegment,
    ref: ContentDraftRef,
) -> tuple[int, int, int | None, int | None]:
    text = _effective_text(segment)
    if ref.start_ticks == segment.start_ticks and ref.end_ticks == segment.end_ticks:
        return 0, len(text), None, None
    spans = exact_fine_unit_spans(segment) or trusted_fine_unit_spans(segment)
    start = (
        None
        if ref.start_ticks == segment.start_ticks
        else next(
            (span for span in spans or () if span.unit.start_ticks == ref.start_ticks),
            None,
        )
    )
    end = (
        None
        if ref.end_ticks == segment.end_ticks
        else next(
            (span for span in spans or () if span.unit.end_ticks == ref.end_ticks),
            None,
        )
    )
    if (
        (ref.start_ticks != segment.start_ticks and start is None)
        or (ref.end_ticks != segment.end_ticks and end is None)
        or (
            start is not None
            and end is not None
            and end.fine_unit_index < start.fine_unit_index
        )
    ):
        raise ProjectError("draft editor partial ref lacks trusted fine-unit boundaries")
    return (
        0 if start is None else start.start_offset,
        len(text) if end is None else end.end_offset,
        None if start is None else start.fine_unit_index,
        None if end is None else end.fine_unit_index + 1,
    )


def _content_ref_dict(ref: ResolvedSelectionRef) -> dict[str, object]:
    return {
        "source_id": ref.source_id,
        "transcript_version_id": ref.transcript_version_id,
        "segment_id": ref.segment_id,
        "start_ticks": ref.start_ticks,
        "end_ticks": ref.end_ticks,
    }


def _same_segment(
    left: ResolvedSelectionRef,
    right: ResolvedSelectionRef,
) -> bool:
    return (
        left.source_id,
        left.transcript_version_id,
        left.segment_id,
    ) == (
        right.source_id,
        right.transcript_version_id,
        right.segment_id,
    )


def _find_draft_paragraph(
    snapshot: DraftEditorSnapshot,
    paragraph_id: str,
) -> dict[str, object]:
    match = next(
        (
            paragraph
            for paragraph in snapshot.paragraphs
            if paragraph["paragraph_id"] == paragraph_id
        ),
        None,
    )
    if match is None:
        raise ProjectError("draft editor paragraph does not exist")
    return match


def _find_segment(
    transcript: TimedTranscript,
    segment_id: str,
) -> TranscriptSegment:
    match = next(
        (segment for segment in transcript.segments if segment.segment_id == segment_id),
        None,
    )
    if match is None:
        raise ProjectError("draft editor segment does not exist")
    return match


def _effective_text(segment: TranscriptSegment) -> str:
    return segment.corrected_text or segment.original_text


def _search_context(text: str, start: int, end: int) -> str:
    left = max(0, start - 18)
    right = min(len(text), end + 24)
    return f"{'…' if left else ''}{text[left:right]}{'…' if right < len(text) else ''}"


def _nested_person_name(paragraph: dict[str, object]) -> object:
    person = paragraph.get("person")
    return person.get("name") if isinstance(person, dict) else None


def _safe_source_caret(caret: CaretPosition) -> dict[str, object]:
    return {
        "paragraph_id": caret.paragraph_id,
        "boundary_id": caret.boundary_id,
        "source_id": caret.source_id,
        "transcript_version_id": caret.transcript_version_id,
        "character_offset": caret.character_offset,
        "utf16_offset": caret.utf16_offset,
        "degraded": caret.degraded,
        "degradation_reason": caret.degradation_reason,
    }


def _hash_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
