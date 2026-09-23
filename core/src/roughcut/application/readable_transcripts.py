"""Readable Transcript derivation, exact selection resolution, and Markdown export."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, replace
from itertools import pairwise
from pathlib import Path
from typing import Any, TypeAlias

from roughcut.adapters.artifact_store import read_json_object
from roughcut.adapters.project_store import ProjectStore
from roughcut.domain.bindings import SourceTranscriptBinding
from roughcut.domain.edit import (
    EditClip,
    EditDecision,
    EditProposal,
    MultiSourceEditDecision,
    MultiSourceEditProposal,
)
from roughcut.domain.people import Person, SpeakerMap
from roughcut.domain.project import Project, ProjectError, SourceAsset
from roughcut.domain.readable_transcript import (
    ADOPTION_STATUSES,
    PARAGRAPH_ALGORITHM_VERSION,
    VIEW_SCHEMA_VERSION,
    CaretNeighbor,
    CaretPosition,
    ContinuousSelectionResolution,
    ExactRefTransform,
    ExactSequenceCaret,
    MarkdownExportResult,
    ReadableParagraph,
    ReadableSegmentRef,
    ReadableTranscriptPage,
    ResolvedSelectionRef,
    SelectionResolution,
    canonical_text_for_range,
    codepoint_to_utf16_offset,
    effective_text,
    exact_fine_unit_spans,
    fine_unit_alignment,
    make_sequence_caret,
    trusted_fine_unit_spans,
    utf16_to_codepoint_offset,
)
from roughcut.domain.transcript import TimedTranscript, TranscriptSegment

_MAX_PAGE_SIZE = 200
_MAX_GAP_TICKS = 2 * 120_000
_SOFT_DURATION_TICKS = 45 * 120_000
_SOFT_CHARACTER_LIMIT = 300
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")

ProposalLike: TypeAlias = EditProposal | MultiSourceEditProposal
DecisionLike: TypeAlias = EditDecision | MultiSourceEditDecision


@dataclass(frozen=True)
class _ViewState:
    project: Project
    bindings: tuple[SourceTranscriptBinding, ...]
    transcripts: dict[tuple[str, str], TimedTranscript]
    paragraphs: tuple[ReadableParagraph, ...]
    view_hash: str


@dataclass
class _ParagraphBuilder:
    source: SourceAsset
    binding: SourceTranscriptBinding
    segments: list[TranscriptSegment]
    person_id: str | None
    person_name: str | None
    speaker_identity: tuple[str, str | None]


@dataclass(frozen=True)
class _Overlay:
    basis: str
    artifact_id: str
    artifact_hash: str
    clips: tuple[EditClip, ...]
    bindings: tuple[SourceTranscriptBinding, ...]
    payload: dict[str, object]


@dataclass(frozen=True)
class _ParagraphSegmentSpan:
    segment: TranscriptSegment
    start_offset: int
    end_offset: int


def read_readable_transcript(
    project_path: Path,
    *,
    source_bindings: list[dict[str, Any]],
    expected_revision: int,
    offset: int,
    limit: int,
    filters: dict[str, object] | None = None,
    overlay: dict[str, object] | None = None,
) -> ReadableTranscriptPage:
    _validate_pagination(offset, limit)
    state = _build_view(
        project_path,
        source_bindings=source_bindings,
        expected_revision=expected_revision,
        overlay=overlay,
    )
    filtered = _filter_paragraphs(state.paragraphs, filters)
    page = filtered[offset : offset + limit]
    numbered = tuple(
        _with_display_number(paragraph, offset + index + 1) for index, paragraph in enumerate(page)
    )
    end = offset + len(page)
    return ReadableTranscriptPage(
        view_schema_version=VIEW_SCHEMA_VERSION,
        algorithm_version=PARAGRAPH_ALGORITHM_VERSION,
        view_hash=state.view_hash,
        project_id=state.project.project_id,
        project_revision=state.project.revision,
        source_bindings=state.bindings,
        offset=offset,
        limit=limit,
        total=len(filtered),
        next_cursor=end if end < len(filtered) else None,
        paragraphs=numbered,
    )


def resolve_transcript_selection(
    project_path: Path,
    *,
    source_bindings: list[dict[str, Any]],
    expected_revision: int,
    view_hash: str,
    selections: list[dict[str, object]],
    overlay: dict[str, object] | None = None,
) -> SelectionResolution:
    if not isinstance(view_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", view_hash):
        raise ProjectError("view hash is invalid")
    if not isinstance(selections, list) or not selections:
        raise ProjectError("selection must contain at least one item")
    state = _build_view(
        project_path,
        source_bindings=source_bindings,
        expected_revision=expected_revision,
        overlay=overlay,
    )
    if state.view_hash != view_hash:
        raise ProjectError("view hash is stale")
    paragraph_positions = {
        paragraph.paragraph_id: index for index, paragraph in enumerate(state.paragraphs)
    }
    parsed: list[tuple[ReadableParagraph, dict[str, object], bool]] = []
    seen: set[str] = set()
    for selection in selections:
        if not isinstance(selection, dict):
            raise ProjectError("selection item must be an object")
        paragraph_id = selection.get("paragraph_id")
        if not isinstance(paragraph_id, str) or paragraph_id in seen:
            raise ProjectError("selection paragraph_id is invalid or duplicated")
        paragraph = next(
            (item for item in state.paragraphs if item.paragraph_id == paragraph_id), None
        )
        if paragraph is None:
            raise ProjectError("selection paragraph does not exist")
        seen.add(paragraph_id)
        partial = any(
            key in selection for key in ("start_offset", "end_offset", "quote", "occurrence")
        )
        parsed.append((paragraph, selection, partial))
    if len(parsed) > 1 and any(item[2] for item in parsed):
        raise ProjectError("partial cross-paragraph selection is not supported")
    parsed.sort(key=lambda item: paragraph_positions[item[0].paragraph_id])

    refs: list[ResolvedSelectionRef] = []
    warnings: list[str] = []
    expanded = False
    for paragraph, selection, partial in parsed:
        if not partial:
            refs.extend(_whole_paragraph_refs(state, paragraph))
            continue
        partial_refs, was_expanded = _resolve_partial(state, paragraph, selection)
        refs.extend(partial_refs)
        if was_expanded:
            expanded = True
            warnings.append(
                "selection expanded to complete segment because trusted fine units were unavailable"
            )
    return SelectionResolution(
        view_hash=state.view_hash,
        mode="expanded_to_segments" if expanded else "exact",
        canonical_text="\n".join(ref.canonical_text for ref in refs),
        refs=tuple(refs),
        warnings=tuple(warnings),
    )


def resolve_transcript_caret(
    project_path: Path,
    *,
    source_bindings: list[dict[str, Any]],
    expected_revision: int,
    view_hash: str,
    paragraph_id: str,
    offset: int,
    offset_encoding: str = "codepoint",
    overlay: dict[str, object] | None = None,
) -> CaretPosition:
    state = _checked_view(
        project_path,
        source_bindings=source_bindings,
        expected_revision=expected_revision,
        view_hash=view_hash,
        overlay=overlay,
    )
    return resolve_transcript_caret_from_view(
        state,
        view_hash=view_hash,
        paragraph_id=paragraph_id,
        offset=offset,
        offset_encoding=offset_encoding,
    )


def resolve_transcript_caret_from_view(
    state: _ViewState,
    *,
    view_hash: str,
    paragraph_id: str,
    offset: int,
    offset_encoding: str = "codepoint",
) -> CaretPosition:
    _require_view_hash(state, view_hash)
    paragraph = _find_paragraph(state, paragraph_id)
    character_offset = _endpoint_character_offset(paragraph.text, offset, offset_encoding)
    return _snap_caret(state, paragraph, character_offset, bias="nearest")


def resolve_continuous_transcript_selection(
    project_path: Path,
    *,
    source_bindings: list[dict[str, Any]],
    expected_revision: int,
    view_hash: str,
    anchor: dict[str, object],
    focus: dict[str, object],
    overlay: dict[str, object] | None = None,
) -> ContinuousSelectionResolution:
    state = _checked_view(
        project_path,
        source_bindings=source_bindings,
        expected_revision=expected_revision,
        view_hash=view_hash,
        overlay=overlay,
    )
    return resolve_continuous_transcript_selection_from_view(
        state,
        view_hash=view_hash,
        anchor=anchor,
        focus=focus,
    )


def resolve_continuous_transcript_selection_from_view(
    state: _ViewState,
    *,
    view_hash: str,
    anchor: dict[str, object],
    focus: dict[str, object],
) -> ContinuousSelectionResolution:
    _require_view_hash(state, view_hash)
    positions = {paragraph.paragraph_id: index for index, paragraph in enumerate(state.paragraphs)}
    anchor_value = _parse_endpoint(state, positions, anchor)
    focus_value = _parse_endpoint(state, positions, focus)
    if anchor_value[:2] == focus_value[:2]:
        raise ProjectError("continuous selection must be non-empty")
    direction = "forward" if anchor_value[:2] < focus_value[:2] else "backward"
    start_value, end_value = sorted((anchor_value, focus_value), key=lambda item: item[:2])
    start_index, start_offset, start_paragraph = start_value
    end_index, end_offset, end_paragraph = end_value
    if (
        start_paragraph.source_id != end_paragraph.source_id
        or start_paragraph.transcript_version_id != end_paragraph.transcript_version_id
    ):
        raise ProjectError("continuous selection cannot cross a source binding")

    refs: list[ResolvedSelectionRef] = []
    degradation_reasons: list[str] = []
    snapped_start: CaretPosition | None = None
    snapped_end: CaretPosition | None = None
    adjusted = False
    for paragraph_index in range(start_index, end_index + 1):
        paragraph = state.paragraphs[paragraph_index]
        requested_start = start_offset if paragraph_index == start_index else 0
        requested_end = end_offset if paragraph_index == end_index else len(paragraph.text)
        if requested_start == requested_end:
            boundary = _snap_caret(
                state,
                paragraph,
                requested_start,
                bias="start" if paragraph_index == start_index else "end",
            )
            if paragraph_index == start_index:
                snapped_start = boundary
            if paragraph_index == end_index:
                snapped_end = boundary
            if boundary.degradation_reason is not None:
                degradation_reasons.append(boundary.degradation_reason)
            adjusted = adjusted or boundary.character_offset != requested_start
            continue
        paragraph_start, paragraph_end = _selection_carets(
            state,
            paragraph,
            requested_start,
            requested_end,
        )
        if paragraph_index == start_index:
            snapped_start = paragraph_start
        if paragraph_index == end_index:
            snapped_end = paragraph_end
        adjusted = adjusted or (
            paragraph_start.character_offset != requested_start
            or paragraph_end.character_offset != requested_end
        )
        for caret in (paragraph_start, paragraph_end):
            if caret.degradation_reason is not None:
                degradation_reasons.append(caret.degradation_reason)
        refs.extend(
            _refs_for_character_range(
                state,
                paragraph,
                paragraph_start.character_offset,
                paragraph_end.character_offset,
            )
        )
    if snapped_start is None or snapped_end is None or not refs:
        raise ProjectError("continuous selection does not cover transcript text")
    return ContinuousSelectionResolution(
        view_hash=state.view_hash,
        direction=direction,
        canonical_text="\n".join(ref.canonical_text for ref in refs),
        refs=tuple(refs),
        start_caret=snapped_start,
        end_caret=snapped_end,
        adjusted=adjusted,
        degraded=bool(degradation_reasons),
        degradation_reasons=tuple(dict.fromkeys(degradation_reasons)),
    )


def build_readable_transcript_view(
    project_path: Path,
    *,
    source_bindings: list[dict[str, Any]],
    expected_revision: int,
) -> _ViewState:
    """Build one validated unfiltered transcript view for a Review session."""
    return _build_view(
        project_path,
        source_bindings=source_bindings,
        expected_revision=expected_revision,
        overlay=None,
    )


def numbered_readable_paragraphs(
    state: _ViewState,
) -> tuple[ReadableParagraph, ...]:
    return tuple(
        _with_display_number(paragraph, index)
        for index, paragraph in enumerate(state.paragraphs, start=1)
    )


def make_exact_sequence_caret(
    view_hash: str,
    paragraph_id: str,
    refs: Sequence[ResolvedSelectionRef],
    sequence_index: int,
) -> ExactSequenceCaret:
    return make_sequence_caret(view_hash, paragraph_id, refs, sequence_index)


def move_draft_selection_to_caret(
    draft_refs: Sequence[ResolvedSelectionRef],
    *,
    selection: ContinuousSelectionResolution,
    selection_start_index: int,
    caret: ExactSequenceCaret,
    accept_degraded: bool = False,
) -> ExactRefTransform:
    refs = tuple(draft_refs)
    if not isinstance(accept_degraded, bool):
        raise ProjectError("degraded selection acceptance must be boolean")
    if selection.view_hash != caret.view_hash:
        raise ProjectError("selection and caret view hashes do not match")
    if selection.degraded and not accept_degraded:
        raise ProjectError("degraded selection must be accepted before moving")
    _validate_sequence_caret(refs, caret)
    if (
        isinstance(selection_start_index, bool)
        or not isinstance(selection_start_index, int)
        or selection_start_index < 0
    ):
        raise ProjectError("selection start index is invalid")
    selection_end_index = selection_start_index + len(selection.refs)
    if refs[selection_start_index:selection_end_index] != selection.refs:
        raise ProjectError("selection refs do not match the draft sequence")
    if selection_start_index < caret.sequence_index < selection_end_index:
        raise ProjectError("caret cannot be inside the moved selection")
    if caret.sequence_index in {selection_start_index, selection_end_index}:
        return ExactRefTransform("move_to_caret", refs, caret, False, "caret_is_adjacent")

    remaining = refs[:selection_start_index] + refs[selection_end_index:]
    insertion_index = caret.sequence_index
    if insertion_index > selection_end_index:
        insertion_index -= len(selection.refs)
    moved = remaining[:insertion_index] + selection.refs + remaining[insertion_index:]
    result_caret = make_sequence_caret(
        caret.view_hash,
        caret.paragraph_id,
        moved,
        insertion_index + len(selection.refs),
    )
    return ExactRefTransform("move_to_caret", moved, result_caret, True, None)


def insert_transcript_selection_at_caret(
    project_path: Path,
    *,
    source_bindings: list[dict[str, Any]],
    expected_revision: int,
    view_hash: str,
    draft_refs: Sequence[ResolvedSelectionRef],
    selection: ContinuousSelectionResolution,
    caret: ExactSequenceCaret,
    accept_degraded: bool = False,
    overlay: dict[str, object] | None = None,
) -> ExactRefTransform:
    state = _checked_view(
        project_path,
        source_bindings=source_bindings,
        expected_revision=expected_revision,
        view_hash=view_hash,
        overlay=overlay,
    )
    refs = tuple(draft_refs)
    if not isinstance(accept_degraded, bool):
        raise ProjectError("degraded selection acceptance must be boolean")
    if selection.view_hash != state.view_hash or caret.view_hash != state.view_hash:
        raise ProjectError("selection or caret view hash is stale")
    _validate_sequence_caret(refs, caret)
    if selection.degraded and not accept_degraded:
        raise ProjectError("degraded selection must be accepted before insertion")
    _validate_inserted_selection(state, selection.refs)
    inserted = refs[: caret.sequence_index] + selection.refs + refs[caret.sequence_index :]
    result_caret = make_sequence_caret(
        caret.view_hash,
        caret.paragraph_id,
        inserted,
        caret.sequence_index + len(selection.refs),
    )
    return ExactRefTransform("insert_at_caret", inserted, result_caret, True, None)


def export_markdown(
    project_path: Path,
    *,
    basis: str,
    output_path: Path,
    expected_revision: int,
    source_bindings: list[dict[str, Any]] | None = None,
    artifact_id: str | None = None,
) -> MarkdownExportResult:
    if basis == "transcript":
        if source_bindings is None or artifact_id is not None:
            raise ProjectError("transcript export requires source bindings only")
        state = _build_view(
            project_path,
            source_bindings=source_bindings,
            expected_revision=expected_revision,
            overlay=None,
        )
        markdown, mapping = _transcript_markdown(state)
    elif basis in {"proposal", "decision"}:
        if source_bindings is not None or artifact_id is None:
            raise ProjectError("artifact export requires only an artifact ID")
        markdown, mapping = _artifact_markdown(
            project_path,
            basis=basis,
            artifact_id=artifact_id,
            expected_revision=expected_revision,
        )
    else:
        raise ProjectError("unsupported Markdown export basis")
    if output_path.suffix.lower() != ".md":
        raise ProjectError("Markdown output path must end in .md")
    markdown_bytes = markdown.encode("utf-8")
    mapping_bytes = (
        json.dumps(mapping, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    mapping_path = output_path.with_suffix(".map.json")
    _publish_pair(output_path, markdown_bytes, mapping_path, mapping_bytes)
    content_hash = hashlib.sha256(markdown_bytes + b"\0" + mapping_bytes).hexdigest()
    return MarkdownExportResult(
        basis=basis,
        markdown_path=str(output_path),
        mapping_path=str(mapping_path),
        content_hash=content_hash,
    )


def _build_view(
    project_path: Path,
    *,
    source_bindings: list[dict[str, Any]],
    expected_revision: int,
    overlay: dict[str, object] | None,
) -> _ViewState:
    _validate_expected_revision(expected_revision)
    bindings = _parse_bindings(source_bindings)
    store = ProjectStore(project_path)
    project = store.load()
    if project.revision != expected_revision:
        raise ProjectError("project revision conflict")
    sources: dict[str, SourceAsset] = {}
    transcripts: dict[tuple[str, str], TimedTranscript] = {}
    for binding in bindings:
        if (
            project.active_transcript_versions.get(binding.source_id)
            != binding.transcript_version_id
        ):
            raise ProjectError("readable transcript binding is not active")
        source = _find_source(project, binding.source_id)
        transcript = _read_transcript(
            store.project_path, binding.source_id, binding.transcript_version_id
        )
        sources[binding.source_id] = source
        transcripts[(binding.source_id, binding.transcript_version_id)] = transcript
    parsed_overlay = _read_overlay(store.project_path, overlay)
    if parsed_overlay is not None and parsed_overlay.bindings != bindings:
        raise ProjectError("overlay source bindings do not match readable transcript")
    adoption = _adoption_by_segment(parsed_overlay, transcripts)
    paragraphs = _build_paragraphs(project, bindings, sources, transcripts, adoption)
    safe_sources = [
        {
            "source_id": sources[binding.source_id].source_id,
            "display_name": sources[binding.source_id].display_name,
            "kind": sources[binding.source_id].kind,
            "duration_ticks": sources[binding.source_id].probe.duration_ticks,
            "tags": list(sources[binding.source_id].tags),
            "note": sources[binding.source_id].note,
        }
        for binding in bindings
    ]
    relevant_maps = [
        mapping.to_dict()
        for binding in bindings
        for mapping in project.speaker_maps
        if mapping.source_id == binding.source_id
        and mapping.transcript_version_id == binding.transcript_version_id
    ]
    hash_payload = {
        "view_schema_version": VIEW_SCHEMA_VERSION,
        "algorithm_version": PARAGRAPH_ALGORITHM_VERSION,
        "project_id": project.project_id,
        "project_revision": project.revision,
        "source_bindings": [binding.to_dict() for binding in bindings],
        "sources": safe_sources,
        "persons": [person.to_dict() for person in project.persons],
        "speaker_maps": relevant_maps,
        "transcripts": [
            transcripts[(b.source_id, b.transcript_version_id)].to_dict() for b in bindings
        ],
        "overlay": parsed_overlay.payload if parsed_overlay is not None else None,
    }
    view_hash = _hash_json(hash_payload)
    return _ViewState(project, bindings, transcripts, paragraphs, view_hash)


def _build_paragraphs(
    project: Project,
    bindings: tuple[SourceTranscriptBinding, ...],
    sources: dict[str, SourceAsset],
    transcripts: dict[tuple[str, str], TimedTranscript],
    adoption: dict[tuple[str, str, str], str],
) -> tuple[ReadableParagraph, ...]:
    people = {person.person_id: person for person in project.persons}
    maps = {
        (mapping.source_id, mapping.transcript_version_id, mapping.local_speaker_id): mapping
        for mapping in project.speaker_maps
    }
    result: list[ReadableParagraph] = []
    for binding in bindings:
        source = sources[binding.source_id]
        transcript = transcripts[(binding.source_id, binding.transcript_version_id)]
        seen_segments: set[str] = set()
        current: _ParagraphBuilder | None = None
        for segment in transcript.segments:
            if segment.segment_id in seen_segments:
                raise ProjectError("transcript segment IDs must be unique")
            seen_segments.add(segment.segment_id)
            if segment.end_ticks <= segment.start_ticks:
                raise ProjectError("transcript segment range must be non-empty")
            person_id, person_name = _resolved_person(binding, segment, maps, people)
            identity = (
                ("person", person_id)
                if person_id is not None
                else ("local", segment.local_speaker_id)
            )
            if current is None or _starts_new_paragraph(current, segment, identity):
                if current is not None:
                    result.append(_finish_paragraph(current, adoption))
                current = _ParagraphBuilder(
                    source=source,
                    binding=binding,
                    segments=[segment],
                    person_id=person_id,
                    person_name=person_name,
                    speaker_identity=identity,
                )
            else:
                current.segments.append(segment)
        if current is not None:
            result.append(_finish_paragraph(current, adoption))
    return tuple(result)


def _starts_new_paragraph(
    current: _ParagraphBuilder,
    segment: TranscriptSegment,
    identity: tuple[str, str | None],
) -> bool:
    previous = current.segments[-1]
    current_text = "\n".join(effective_text(item) for item in current.segments)
    return (
        identity != current.speaker_identity
        or segment.start_ticks < previous.end_ticks
        or segment.start_ticks - previous.end_ticks > _MAX_GAP_TICKS
        or previous.end_ticks - current.segments[0].start_ticks >= _SOFT_DURATION_TICKS
        or len(current_text) >= _SOFT_CHARACTER_LIMIT
    )


def _finish_paragraph(
    builder: _ParagraphBuilder,
    adoption: dict[tuple[str, str, str], str],
) -> ReadableParagraph:
    segment_ids = [segment.segment_id for segment in builder.segments]
    digest = hashlib.sha256(
        json.dumps(
            [
                PARAGRAPH_ALGORITHM_VERSION,
                builder.binding.source_id,
                builder.binding.transcript_version_id,
                segment_ids,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    statuses = [
        adoption.get(
            (
                builder.binding.source_id,
                builder.binding.transcript_version_id,
                segment.segment_id,
            ),
            "not_applicable",
        )
        for segment in builder.segments
    ]
    status = _paragraph_adoption(statuses)
    local_ids = tuple(
        dict.fromkeys(
            segment.local_speaker_id
            for segment in builder.segments
            if segment.local_speaker_id is not None
        )
    )
    return ReadableParagraph(
        paragraph_id=f"paragraph_{digest[:32]}",
        display_number="",
        source_id=builder.binding.source_id,
        transcript_version_id=builder.binding.transcript_version_id,
        source_display_name=builder.source.display_name,
        local_speaker_id=local_ids[0] if len(local_ids) == 1 else None,
        local_speaker_ids=local_ids,
        person_id=builder.person_id,
        person_name=builder.person_name,
        text="\n".join(effective_text(segment) for segment in builder.segments),
        start_ticks=builder.segments[0].start_ticks,
        end_ticks=builder.segments[-1].end_ticks,
        refs=tuple(
            ReadableSegmentRef(
                builder.binding.source_id,
                builder.binding.transcript_version_id,
                segment.segment_id,
                segment.start_ticks,
                segment.end_ticks,
            )
            for segment in builder.segments
        ),
        adoption_status=status,
    )


def _resolved_person(
    binding: SourceTranscriptBinding,
    segment: TranscriptSegment,
    mappings: dict[tuple[str, str, str], SpeakerMap],
    people: dict[str, Person],
) -> tuple[str | None, str | None]:
    if segment.local_speaker_id is None:
        return None, None
    mapping = mappings.get(
        (binding.source_id, binding.transcript_version_id, segment.local_speaker_id)
    )
    if mapping is None:
        return None, None
    person = people.get(mapping.person_id)
    if person is None:
        raise ProjectError("speaker map person is not part of the project")
    return person.person_id, person.name


def _read_overlay(project_path: Path, value: dict[str, object] | None) -> _Overlay | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"basis", "artifact_id"}:
        raise ProjectError("overlay must contain basis and artifact_id")
    basis = value.get("basis")
    artifact_id = value.get("artifact_id")
    if basis not in {"proposal", "decision"} or not isinstance(artifact_id, str):
        raise ProjectError("overlay basis or artifact_id is invalid")
    _validate_id(artifact_id, "artifact_id")
    if basis == "proposal":
        data = read_json_object(
            project_path / "proposals" / f"{artifact_id}.json", description="proposal overlay"
        )
        artifact = _parse_proposal(data)
        clips = artifact.clips
        bindings = _proposal_bindings(artifact)
    else:
        data = read_json_object(
            project_path / "edits" / f"{artifact_id}.json", description="decision overlay"
        )
        decision = _parse_decision(data)
        artifact = decision.proposal_snapshot
        clips = artifact.clips
        bindings = _proposal_bindings(artifact)
    payload: dict[str, object] = {
        "basis": basis,
        "artifact_id": artifact_id,
        "artifact_hash": _hash_json(data),
    }
    return _Overlay(basis, artifact_id, str(payload["artifact_hash"]), clips, bindings, payload)


def _adoption_by_segment(
    overlay: _Overlay | None,
    transcripts: dict[tuple[str, str], TimedTranscript],
) -> dict[tuple[str, str, str], str]:
    if overlay is None:
        return {}
    segments = {
        (source_id, transcript_id, segment.segment_id): segment
        for (source_id, transcript_id), transcript in transcripts.items()
        for segment in transcript.segments
    }
    ranges: dict[tuple[str, str, str], list[tuple[int, int]]] = {}
    for clip in overlay.clips:
        key = (clip.source_id, clip.transcript_version_id, clip.segment_id)
        segment = segments.get(key)
        if segment is None:
            raise ProjectError("overlay clip is outside readable transcript")
        if (
            clip.source_in_ticks < segment.start_ticks
            or clip.source_out_ticks > segment.end_ticks
            or clip.source_out_ticks <= clip.source_in_ticks
        ):
            raise ProjectError("overlay clip range is invalid")
        ranges.setdefault(key, []).append((clip.source_in_ticks, clip.source_out_ticks))
    result: dict[tuple[str, str, str], str] = {}
    for key, segment in segments.items():
        intervals = sorted(ranges.get(key, []))
        if not intervals:
            result[key] = "unadopted"
            continue
        merged: list[list[int]] = []
        for start, end in intervals:
            if not merged or start > merged[-1][1]:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        result[key] = (
            "adopted"
            if len(merged) == 1
            and merged[0][0] == segment.start_ticks
            and merged[0][1] == segment.end_ticks
            else "partial"
        )
    return result


def _paragraph_adoption(statuses: Sequence[str]) -> str:
    unique = set(statuses)
    if unique == {"not_applicable"}:
        return "not_applicable"
    if unique == {"adopted"}:
        return "adopted"
    if unique == {"unadopted"}:
        return "unadopted"
    return "partial"


def _filter_paragraphs(
    paragraphs: tuple[ReadableParagraph, ...], filters: dict[str, object] | None
) -> tuple[ReadableParagraph, ...]:
    if filters is None:
        return paragraphs
    if not isinstance(filters, dict):
        raise ProjectError("readable transcript filters must be an object")
    allowed = {"source_ids", "person_ids", "adoption_statuses", "keyword"}
    if set(filters) - allowed:
        raise ProjectError("readable transcript filter is unsupported")
    source_ids = _optional_string_set(filters, "source_ids")
    person_ids = _optional_string_set(filters, "person_ids")
    adoption_statuses = _optional_string_set(filters, "adoption_statuses")
    if adoption_statuses is not None and not adoption_statuses <= ADOPTION_STATUSES:
        raise ProjectError("adoption status filter is invalid")
    keyword = filters.get("keyword")
    if keyword is not None and (not isinstance(keyword, str) or not keyword):
        raise ProjectError("keyword filter must be a non-empty string")
    return tuple(
        paragraph
        for paragraph in paragraphs
        if (source_ids is None or paragraph.source_id in source_ids)
        and (person_ids is None or paragraph.person_id in person_ids)
        and (adoption_statuses is None or paragraph.adoption_status in adoption_statuses)
        and (keyword is None or keyword.casefold() in paragraph.text.casefold())
    )


def _optional_string_set(filters: dict[str, object], key: str) -> set[str] | None:
    value = filters.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise ProjectError(f"{key} filter must be a non-empty string array")
    return set(value)


def _whole_paragraph_refs(
    state: _ViewState, paragraph: ReadableParagraph
) -> list[ResolvedSelectionRef]:
    segments = _paragraph_segments(state, paragraph)
    return [
        ResolvedSelectionRef(
            paragraph.source_id,
            paragraph.transcript_version_id,
            segment.segment_id,
            segment.start_ticks,
            segment.end_ticks,
            effective_text(segment),
        )
        for segment in segments
    ]


def _resolve_partial(
    state: _ViewState,
    paragraph: ReadableParagraph,
    selection: dict[str, object],
) -> tuple[list[ResolvedSelectionRef], bool]:
    start = selection.get("start_offset")
    end = selection.get("end_offset")
    quote = selection.get("quote")
    occurrence = selection.get("occurrence")
    if (
        isinstance(start, bool)
        or not isinstance(start, int)
        or isinstance(end, bool)
        or not isinstance(end, int)
        or not isinstance(quote, str)
        or start < 0
        or end <= start
        or end > len(paragraph.text)
        or paragraph.text[start:end] != quote
    ):
        raise ProjectError("selection offsets and quote do not match the paragraph")
    occurrences = _occurrences(paragraph.text, quote)
    if len(occurrences) > 1 and occurrence is None:
        raise ProjectError("selection occurrence is required for repeated text")
    if occurrence is not None and (
        isinstance(occurrence, bool)
        or not isinstance(occurrence, int)
        or occurrence < 0
        or occurrence >= len(occurrences)
        or occurrences[occurrence] != start
    ):
        raise ProjectError("selection occurrence does not match its offset")

    segments = _paragraph_segments(state, paragraph)
    spans: list[tuple[TranscriptSegment, int, int]] = []
    cursor = 0
    for index, segment in enumerate(segments):
        if index:
            cursor += 1
        segment_start = cursor
        cursor += len(effective_text(segment))
        spans.append((segment, segment_start, cursor))
    resolved: list[ResolvedSelectionRef] = []
    expanded = False
    for segment, text_start, text_end in spans:
        overlap_start = max(start, text_start)
        overlap_end = min(end, text_end)
        if overlap_end <= overlap_start:
            continue
        local_start = overlap_start - text_start
        local_end = overlap_end - text_start
        segment_text = effective_text(segment)
        range_start = segment.start_ticks
        range_end = segment.end_ticks
        canonical = segment_text
        if local_start != 0 or local_end != len(segment_text):
            fine_spans = exact_fine_unit_spans(segment) or trusted_fine_unit_spans(segment)
            start_span = next(
                (span for span in fine_spans or () if span.start_offset == local_start), None
            )
            end_span = next(
                (span for span in fine_spans or () if span.end_offset == local_end), None
            )
            if (
                start_span is None
                or end_span is None
                or end_span.end_offset <= start_span.start_offset
            ):
                expanded = True
            else:
                range_start = start_span.unit.start_ticks
                range_end = end_span.unit.end_ticks
                canonical = canonical_text_for_range(segment, range_start, range_end)
        resolved.append(
            ResolvedSelectionRef(
                paragraph.source_id,
                paragraph.transcript_version_id,
                segment.segment_id,
                range_start,
                range_end,
                canonical,
            )
        )
    if not resolved:
        raise ProjectError("selection does not cover transcript text")
    return resolved, expanded


def _paragraph_segments(
    state: _ViewState, paragraph: ReadableParagraph
) -> tuple[TranscriptSegment, ...]:
    transcript = state.transcripts[(paragraph.source_id, paragraph.transcript_version_id)]
    by_id = {segment.segment_id: segment for segment in transcript.segments}
    return tuple(by_id[ref.segment_id] for ref in paragraph.refs)


def _occurrences(text: str, quote: str) -> list[int]:
    positions: list[int] = []
    cursor = 0
    while True:
        position = text.find(quote, cursor)
        if position < 0:
            return positions
        positions.append(position)
        cursor = position + 1


def _checked_view(
    project_path: Path,
    *,
    source_bindings: list[dict[str, Any]],
    expected_revision: int,
    view_hash: str,
    overlay: dict[str, object] | None,
) -> _ViewState:
    if not isinstance(view_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", view_hash):
        raise ProjectError("view hash is invalid")
    state = _build_view(
        project_path,
        source_bindings=source_bindings,
        expected_revision=expected_revision,
        overlay=overlay,
    )
    if state.view_hash != view_hash:
        raise ProjectError("view hash is stale")
    return state


def _require_view_hash(state: _ViewState, view_hash: str) -> None:
    if (
        not isinstance(view_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", view_hash) is None
        or state.view_hash != view_hash
    ):
        raise ProjectError("view hash is stale")


def _find_paragraph(state: _ViewState, paragraph_id: str) -> ReadableParagraph:
    if not isinstance(paragraph_id, str):
        raise ProjectError("paragraph identity is invalid")
    paragraph = next(
        (item for item in state.paragraphs if item.paragraph_id == paragraph_id),
        None,
    )
    if paragraph is None:
        raise ProjectError("paragraph does not exist")
    return paragraph


def _parse_endpoint(
    state: _ViewState,
    positions: dict[str, int],
    value: dict[str, object],
) -> tuple[int, int, ReadableParagraph]:
    if not isinstance(value, dict) or set(value) - {
        "paragraph_id",
        "offset",
        "offset_encoding",
    }:
        raise ProjectError("selection endpoint is invalid")
    paragraph_id = value.get("paragraph_id")
    offset = value.get("offset")
    encoding = value.get("offset_encoding", "codepoint")
    paragraph = _find_paragraph(state, paragraph_id)  # type: ignore[arg-type]
    character_offset = _endpoint_character_offset(paragraph.text, offset, encoding)
    return positions[paragraph.paragraph_id], character_offset, paragraph


def _endpoint_character_offset(text: str, offset: object, encoding: object) -> int:
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise ProjectError("selection endpoint offset is invalid")
    if encoding == "codepoint":
        if offset < 0 or offset > len(text):
            raise ProjectError("selection endpoint offset is outside paragraph")
        return offset
    if encoding == "utf16":
        return utf16_to_codepoint_offset(text, offset)
    raise ProjectError("selection endpoint offset encoding is invalid")


def _paragraph_segment_spans(
    state: _ViewState,
    paragraph: ReadableParagraph,
) -> tuple[_ParagraphSegmentSpan, ...]:
    result: list[_ParagraphSegmentSpan] = []
    cursor = 0
    for index, segment in enumerate(_paragraph_segments(state, paragraph)):
        if index:
            cursor += 1
        start = cursor
        cursor += len(effective_text(segment))
        result.append(_ParagraphSegmentSpan(segment, start, cursor))
    return tuple(result)


def _display_only(character: str) -> bool:
    return character.isspace() or unicodedata.category(character).startswith("P")


def _caret_candidates(
    state: _ViewState,
    paragraph: ReadableParagraph,
) -> tuple[CaretPosition, ...]:
    candidates: list[CaretPosition] = []
    for segment_span in _paragraph_segment_spans(state, paragraph):
        segment = segment_span.segment
        spans, degradation_reason = fine_unit_alignment(segment)
        if spans is None:
            segment_neighbor = CaretNeighbor(
                paragraph.source_id,
                paragraph.transcript_version_id,
                segment.segment_id,
                None,
                segment.start_ticks,
                segment.end_ticks,
            )
            candidates.extend(
                (
                    _make_caret(
                        paragraph,
                        state.view_hash,
                        segment_span.start_offset,
                        None,
                        segment_neighbor,
                        degraded=True,
                        reason=degradation_reason,
                    ),
                    _make_caret(
                        paragraph,
                        state.view_hash,
                        segment_span.end_offset,
                        segment_neighbor,
                        None,
                        degraded=True,
                        reason=degradation_reason,
                    ),
                )
            )
            continue
        neighbors = tuple(
            CaretNeighbor(
                paragraph.source_id,
                paragraph.transcript_version_id,
                segment.segment_id,
                span.fine_unit_index,
                span.unit.start_ticks,
                span.unit.end_ticks,
            )
            for span in spans
        )
        segment_text = effective_text(segment)
        for boundary_index in range(len(spans) + 1):
            local_offset = (
                spans[0].start_offset
                if boundary_index == 0
                else spans[boundary_index - 1].end_offset
            )
            candidates.append(
                _make_caret(
                    paragraph,
                    state.view_hash,
                    segment_span.start_offset + local_offset,
                    neighbors[boundary_index - 1] if boundary_index else None,
                    neighbors[boundary_index] if boundary_index < len(neighbors) else None,
                    degraded=False,
                    reason=None,
                )
            )
        # Display-only punctuation/whitespace absorbed into a spoken span
        # (说='说：'‘' with 说=[3,6)) forms a visual gap inside the span:
        # emit a caret at every code-point boundary inside the gap so a drag
        # ending there keeps the exact character the user included.  Each such
        # caret carries the gap-side tick pair (contract 87-88): left = the
        # preceding spoken unit end, right = the following spoken unit start.
        # Only gaps whose characters carry exact fine-unit ticks participate:
        # characters without ticks (spaces/punctuation absent from the unit
        # list) keep the legacy snap-over-gap behavior.
        exact_spans = exact_fine_unit_spans(segment)
        gap_offsets = (
            {
                span.start_offset
                for span in exact_spans
                if span.unit.end_ticks > span.unit.start_ticks
            }
            if exact_spans is not None
            else set()
        )
        for boundary_index, span in enumerate(spans):
            spoken_length = len(span.unit.text)
            gap_start = span.start_offset + spoken_length
            gap_end = span.end_offset
            if gap_start >= gap_end:
                continue
            if not all(_display_only(segment_text[offset]) for offset in range(gap_start, gap_end)):
                continue
            for offset in range(gap_start, gap_end):
                if offset not in gap_offsets:
                    continue
                candidates.append(
                    _make_caret(
                        paragraph,
                        state.view_hash,
                        segment_span.start_offset + offset,
                        neighbors[boundary_index],
                        (
                            neighbors[boundary_index + 1]
                            if boundary_index + 1 < len(neighbors)
                            else None
                        ),
                        degraded=False,
                        reason=None,
                    )
                )
    unique: dict[str, CaretPosition] = {}
    for candidate in candidates:
        unique[candidate.boundary_id] = candidate
    return tuple(
        sorted(
            unique.values(),
            key=lambda item: (item.character_offset, item.boundary_id),
        )
    )


def _make_caret(
    paragraph: ReadableParagraph,
    view_hash: str,
    character_offset: int,
    left: CaretNeighbor | None,
    right: CaretNeighbor | None,
    *,
    degraded: bool,
    reason: str | None,
) -> CaretPosition:
    identity: dict[str, object] = {
        "view_hash": view_hash,
        "paragraph_id": paragraph.paragraph_id,
        "source_id": paragraph.source_id,
        "transcript_version_id": paragraph.transcript_version_id,
        "character_offset": character_offset,
        "left": left.to_dict() if left is not None else None,
        "right": right.to_dict() if right is not None else None,
    }
    boundary_id = "caret_" + _hash_json(identity)[:32]
    return CaretPosition(
        view_hash,
        paragraph.paragraph_id,
        boundary_id,
        paragraph.source_id,
        paragraph.transcript_version_id,
        character_offset,
        codepoint_to_utf16_offset(paragraph.text, character_offset),
        left,
        right,
        degraded,
        reason,
    )


def _snap_caret(
    state: _ViewState,
    paragraph: ReadableParagraph,
    character_offset: int,
    *,
    bias: str,
) -> CaretPosition:
    candidates = _caret_candidates(state, paragraph)
    if not candidates:
        raise ProjectError("paragraph has no legal caret boundaries")

    def distance_key(item: CaretPosition) -> tuple[int, int]:
        tie_breaker = -item.character_offset if bias == "end" else item.character_offset
        return abs(item.character_offset - character_offset), tie_breaker

    return min(candidates, key=distance_key)


def _selection_carets(
    state: _ViewState,
    paragraph: ReadableParagraph,
    requested_start: int,
    requested_end: int,
) -> tuple[CaretPosition, CaretPosition]:
    if (
        requested_start < 0
        or requested_end > len(paragraph.text)
        or requested_end <= requested_start
    ):
        raise ProjectError("selection range is invalid")
    candidates = _caret_candidates(state, paragraph)
    start = _snap_caret(state, paragraph, requested_start, bias="start")
    end = _snap_caret(state, paragraph, requested_end, bias="end")
    if start.character_offset >= end.character_offset:
        before = [item for item in candidates if item.character_offset <= requested_start]
        after = [item for item in candidates if item.character_offset >= requested_end]
        if not before or not after:
            raise ProjectError("selection cannot expand to legal caret boundaries")
        start = max(before, key=lambda item: item.character_offset)
        end = min(after, key=lambda item: item.character_offset)
    if start.character_offset >= end.character_offset:
        raise ProjectError("selection does not contain a spoken fine unit")
    # A selection covering only display-only punctuation (no spoken unit)
    # must not form a media selection: snap back to the spoken span that
    # absorbed the punctuation, matching the legacy snap-over-gap behavior.
    text = paragraph.text
    if not any(
        not _display_only(character)
        for character in text[start.character_offset : end.character_offset]
    ):
        for segment_span in _paragraph_segment_spans(state, paragraph):
            if not (
                segment_span.start_offset <= start.character_offset
                and end.character_offset <= segment_span.end_offset
            ):
                continue
            spans = trusted_fine_unit_spans(segment_span.segment)
            if spans is None:
                continue
            for span in spans:
                if (
                    span.start_offset <= start.character_offset
                    and end.character_offset <= span.end_offset
                    and start.character_offset >= span.start_offset + len(span.unit.text)
                ):
                    start = _snap_caret(state, paragraph, span.start_offset, bias="start")
                    end = _snap_caret(state, paragraph, span.end_offset, bias="end")
                    if start.character_offset < end.character_offset:
                        return start, end
        raise ProjectError("selection does not contain a spoken fine unit")
    return start, end


def _refs_for_character_range(
    state: _ViewState,
    paragraph: ReadableParagraph,
    start_offset: int,
    end_offset: int,
) -> list[ResolvedSelectionRef]:
    result: list[ResolvedSelectionRef] = []
    for segment_span in _paragraph_segment_spans(state, paragraph):
        overlap_start = max(start_offset, segment_span.start_offset)
        overlap_end = min(end_offset, segment_span.end_offset)
        if overlap_end <= overlap_start:
            continue
        segment = segment_span.segment
        segment_text = effective_text(segment)
        local_start = overlap_start - segment_span.start_offset
        local_end = overlap_end - segment_span.start_offset
        if local_start == 0 and local_end == len(segment_text):
            result.append(
                ResolvedSelectionRef(
                    paragraph.source_id,
                    paragraph.transcript_version_id,
                    segment.segment_id,
                    segment.start_ticks,
                    segment.end_ticks,
                    segment_text,
                )
            )
            continue
        spans = trusted_fine_unit_spans(segment)
        if spans is None:
            raise ProjectError("partial selection requires trusted fine units")

        def span_index_at(
            offset: int,
            *,
            side: str,
            spans=spans,
            segment_text=segment_text,
        ) -> int | None:
            """Map a display offset to a spoken span boundary.

            Offsets on a span boundary resolve to that span; offsets inside a
            span's trailing display-only gap resolve to the same span so a
            selection ending in the gap keeps the exact characters included
            (the gap has no ticks of its own — the ref uses the span's unit
            boundary instead).
            """
            for index, span in enumerate(spans):
                if span.start_offset == offset and side == "start":
                    return index
                if span.end_offset == offset and side == "end":
                    return index
                if span.start_offset < offset < span.end_offset:
                    gap_start = span.start_offset + len(span.unit.text)
                    if offset >= gap_start and all(
                        _display_only(segment_text[position])
                        for position in range(gap_start, offset + 1)
                    ):
                        return index
            return None

        start_index = span_index_at(local_start, side="start")
        end_index = span_index_at(local_end, side="end")
        if start_index is None or end_index is None or end_index < start_index:
            raise ProjectError("selection is not aligned to fine-unit boundaries")
        first = spans[start_index]
        last = spans[end_index]
        selected_text = segment_text[local_start:local_end]
        exact = exact_fine_unit_spans(segment)
        exact_ticks = (
            {
                span.start_offset
                for span in exact
                if span.unit.end_ticks > span.unit.start_ticks
            }
            if exact is not None
            else set()
        )
        explicit_gap = (
            local_start < last.start_offset
            and any(
                _display_only(character)
                and local_start + index in exact_ticks
                for index, character in enumerate(selected_text)
            )
        )
        if explicit_gap:
            # The selection starts before the final spoken span and explicitly
            # includes display-only characters that carry exact fine-unit
            # ticks: those punctuation characters move with the content
            # (spec: explicitly included punctuation follows the selection).
            # The ref covers the exact spans of the first and last selected
            # character, so a selection ending on a span boundary still keeps
            # the punctuation it spans.  A selection that starts exactly at
            # the final span's spoken boundary keeps the legacy trusted-span
            # derivation (punctuation absorbed into the span stays behind).
            if exact is None:
                raise ProjectError("selection is not aligned to fine-unit boundaries")
            first_exact = next(
                (
                    span
                    for span in exact
                    if span.start_offset <= local_start < span.end_offset
                ),
                None,
            )
            last_exact = next(
                (
                    span
                    for span in exact
                    if span.start_offset <= local_end - 1 < span.end_offset
                ),
                None,
            )
            if first_exact is None or last_exact is None:
                raise ProjectError("selection is not aligned to fine-unit boundaries")
            result.append(
                ResolvedSelectionRef(
                    paragraph.source_id,
                    paragraph.transcript_version_id,
                    segment.segment_id,
                    first_exact.unit.start_ticks,
                    last_exact.unit.end_ticks,
                    canonical_text_for_range(
                        segment,
                        first_exact.unit.start_ticks,
                        last_exact.unit.end_ticks,
                    ),
                    first_exact.fine_unit_index,
                    last_exact.fine_unit_index + 1,
                )
            )
            continue
        result.append(
            ResolvedSelectionRef(
                paragraph.source_id,
                paragraph.transcript_version_id,
                segment.segment_id,
                first.unit.start_ticks,
                last.unit.end_ticks,
                canonical_text_for_range(
                    segment,
                    first.unit.start_ticks,
                    last.unit.end_ticks,
                ),
                first.fine_unit_index,
                last.fine_unit_index + 1,
            )
        )
    return result


def _validate_sequence_caret(
    refs: tuple[ResolvedSelectionRef, ...],
    caret: ExactSequenceCaret,
) -> None:
    expected = make_sequence_caret(
        caret.view_hash,
        caret.paragraph_id,
        refs,
        caret.sequence_index,
    )
    if expected != caret:
        raise ProjectError("exact sequence caret is stale")


def _validate_inserted_selection(
    state: _ViewState,
    refs: tuple[ResolvedSelectionRef, ...],
) -> None:
    if not refs:
        raise ProjectError("inserted selection requires exact refs")
    binding_keys = {
        (binding.source_id, binding.transcript_version_id) for binding in state.bindings
    }
    identities = {(ref.source_id, ref.transcript_version_id) for ref in refs}
    if len(identities) != 1 or not identities <= binding_keys:
        raise ProjectError("inserted selection crosses or escapes source bindings")
    source_id, transcript_version_id = next(iter(identities))
    transcript = state.transcripts[(source_id, transcript_version_id)]
    segments = {segment.segment_id: segment for segment in transcript.segments}
    positions = {segment.segment_id: index for index, segment in enumerate(transcript.segments)}
    maps = {
        (mapping.source_id, mapping.transcript_version_id, mapping.local_speaker_id): mapping
        for mapping in state.project.speaker_maps
    }
    people = {person.person_id: person for person in state.project.persons}
    binding = next(
        item
        for item in state.bindings
        if item.source_id == source_id and item.transcript_version_id == transcript_version_id
    )
    speaker_identities: set[tuple[str, str | None]] = set()
    ref_ranges: list[tuple[int, int, int, int]] = []
    for ref in refs:
        segment = segments.get(ref.segment_id)
        if segment is None:
            raise ProjectError("inserted selection contains an unknown ref")
        span_start, span_end, span_count = _validated_ref_span(segment, ref)
        canonical = canonical_text_for_range(segment, ref.start_ticks, ref.end_ticks)
        if canonical != ref.canonical_text:
            raise ProjectError("inserted selection canonical text does not match its ref")
        person_id, _ = _resolved_person(binding, segment, maps, people)
        speaker_identities.add(
            ("person", person_id) if person_id is not None else ("local", segment.local_speaker_id)
        )
        ref_ranges.append((positions[segment.segment_id], span_start, span_end, span_count))
    if len(speaker_identities) != 1:
        raise ProjectError("inserted selection cannot cross speaker identities")
    for previous, current in pairwise(ref_ranges):
        previous_position, _, previous_end, previous_count = previous
        current_position, current_start, _, _ = current
        next_segment = current_position == previous_position + 1
        if next_segment and previous_end == previous_count and current_start == 0:
            continue
        raise ProjectError("inserted selection refs are not continuous")


def _validated_ref_span(
    segment: TranscriptSegment,
    ref: ResolvedSelectionRef,
) -> tuple[int, int, int]:
    spans = exact_fine_unit_spans(segment) or trusted_fine_unit_spans(segment)
    if ref.start_ticks == segment.start_ticks and ref.end_ticks == segment.end_ticks:
        if ref.fine_unit_start_index is not None or ref.fine_unit_end_index is not None:
            raise ProjectError("full-segment ref must not claim partial fine units")
        return 0, len(spans or ()), len(spans or ())
    if spans is None:
        raise ProjectError("partial inserted ref requires trusted fine units")
    start_position = next(
        (index for index, span in enumerate(spans) if span.unit.start_ticks == ref.start_ticks),
        None,
    )
    end_position = next(
        (index + 1 for index, span in enumerate(spans) if span.unit.end_ticks == ref.end_ticks),
        None,
    )
    if start_position is None or end_position is None or end_position <= start_position:
        raise ProjectError("partial inserted ref is not on fine-unit boundaries")
    first = spans[start_position]
    last = spans[end_position - 1]
    if (
        ref.fine_unit_start_index != first.fine_unit_index
        or ref.fine_unit_end_index != last.fine_unit_index + 1
    ):
        raise ProjectError("inserted ref fine-unit identity is invalid")
    return start_position, end_position, len(spans)


def _transcript_markdown(state: _ViewState) -> tuple[str, dict[str, object]]:
    lines = ["# Readable Transcript", "", f"View hash: `{state.view_hash}`", ""]
    entries: list[dict[str, object]] = []
    for index, paragraph in enumerate(state.paragraphs, start=1):
        number = f"P{index:03d}"
        speaker = paragraph.person_name or paragraph.local_speaker_id or "未映射人物"
        lines.extend(
            [
                f"## {number} · {paragraph.source_display_name} · {speaker}",
                "",
                f"{_format_ticks(paragraph.start_ticks)}–{_format_ticks(paragraph.end_ticks)}",
                "",
                paragraph.text,
                "",
            ]
        )
        entries.append(
            {
                "paragraph_id": paragraph.paragraph_id,
                "display_number": number,
                "refs": [ref.to_dict() for ref in paragraph.refs],
            }
        )
    mapping: dict[str, object] = {
        "schema_version": 1,
        "basis": "transcript",
        "view_schema_version": VIEW_SCHEMA_VERSION,
        "algorithm_version": PARAGRAPH_ALGORITHM_VERSION,
        "view_hash": state.view_hash,
        "source_bindings": [binding.to_dict() for binding in state.bindings],
        "entries": entries,
    }
    return "\n".join(lines), mapping


def _artifact_markdown(
    project_path: Path,
    *,
    basis: str,
    artifact_id: str,
    expected_revision: int,
) -> tuple[str, dict[str, object]]:
    _validate_expected_revision(expected_revision)
    _validate_id(artifact_id, "artifact_id")
    store = ProjectStore(project_path)
    project = store.load()
    if project.revision != expected_revision:
        raise ProjectError("project revision conflict")
    if basis == "proposal":
        data = read_json_object(
            store.project_path / "proposals" / f"{artifact_id}.json",
            description="Markdown proposal",
        )
        proposal = _parse_proposal(data)
    else:
        data = read_json_object(
            store.project_path / "edits" / f"{artifact_id}.json",
            description="Markdown decision",
        )
        proposal = _parse_decision(data).proposal_snapshot
    artifact_hash = _hash_json(data)
    bindings = _proposal_bindings(proposal)
    sources = {binding.source_id: _find_source(project, binding.source_id) for binding in bindings}
    lines = [f"# {basis.title()} Script", "", f"Artifact hash: `{artifact_hash}`", ""]
    entries: list[dict[str, object]] = []
    for index, clip in enumerate(proposal.clips, start=1):
        source = sources[clip.source_id]
        number = f"C{index:03d}"
        lines.extend(
            [
                f"## {number} · {source.display_name}",
                "",
                f"{_format_ticks(clip.source_in_ticks)}–{_format_ticks(clip.source_out_ticks)}",
                "",
                clip.display_text,
                "",
                f"Reason: {clip.reason}",
                "",
            ]
        )
        entries.append(
            {
                "clip_id": clip.clip_id,
                "display_number": number,
                "refs": [
                    {
                        "source_id": clip.source_id,
                        "transcript_version_id": clip.transcript_version_id,
                        "segment_id": clip.segment_id,
                        "start_ticks": clip.source_in_ticks,
                        "end_ticks": clip.source_out_ticks,
                    }
                ],
            }
        )
    mapping: dict[str, object] = {
        "schema_version": 1,
        "basis": basis,
        "artifact_id": artifact_id,
        "artifact_hash": artifact_hash,
        "source_bindings": [binding.to_dict() for binding in bindings],
        "entries": entries,
    }
    return "\n".join(lines), mapping


def _publish_pair(
    markdown_path: Path,
    markdown_bytes: bytes,
    mapping_path: Path,
    mapping_bytes: bytes,
) -> None:
    if markdown_path.exists() or mapping_path.exists():
        raise ProjectError("Markdown export target already exists")
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    if mapping_path.parent != markdown_path.parent:
        raise ProjectError("Markdown and mapping must share a directory")
    markdown_temp = _write_temp(markdown_path.parent, markdown_bytes)
    try:
        mapping_temp = _write_temp(mapping_path.parent, mapping_bytes)
    except Exception:
        markdown_temp.unlink(missing_ok=True)
        raise
    markdown_published = False
    try:
        os.replace(markdown_temp, markdown_path)
        markdown_published = True
        os.replace(mapping_temp, mapping_path)
    except Exception:
        if markdown_published:
            markdown_path.unlink(missing_ok=True)
        mapping_path.unlink(missing_ok=True)
        raise
    finally:
        markdown_temp.unlink(missing_ok=True)
        mapping_temp.unlink(missing_ok=True)


def _write_temp(directory: Path, content: bytes) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=".roughcut-export-", suffix=".tmp", dir=directory)
    path = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def _parse_bindings(value: object) -> tuple[SourceTranscriptBinding, ...]:
    if not isinstance(value, list) or not value:
        raise ProjectError("readable transcript requires source bindings")
    bindings: list[SourceTranscriptBinding] = []
    for item in value:
        if not isinstance(item, dict):
            raise ProjectError("source binding must be an object")
        bindings.append(SourceTranscriptBinding.from_dict(item))
    source_ids = [binding.source_id for binding in bindings]
    if len(source_ids) != len(set(source_ids)):
        raise ProjectError("readable transcript bindings require unique source IDs")
    return tuple(bindings)


def _proposal_bindings(proposal: ProposalLike) -> tuple[SourceTranscriptBinding, ...]:
    if isinstance(proposal, EditProposal):
        return (SourceTranscriptBinding(proposal.source_id, proposal.transcript_version_id),)
    return proposal.source_bindings


def _parse_proposal(data: dict[str, Any]) -> ProposalLike:
    if data.get("schema_version") == 1:
        return EditProposal.from_dict(data)
    if data.get("schema_version") == 2:
        return MultiSourceEditProposal.from_dict(data)
    raise ProjectError("unsupported proposal schema")


def _parse_decision(data: dict[str, Any]) -> DecisionLike:
    if data.get("schema_version") == 1:
        return EditDecision.from_dict(data)
    if data.get("schema_version") == 2:
        return MultiSourceEditDecision.from_dict(data)
    raise ProjectError("unsupported decision schema")


def _read_transcript(project_path: Path, source_id: str, transcript_id: str) -> TimedTranscript:
    data = read_json_object(
        project_path / "transcripts" / source_id / f"{transcript_id}.json",
        description="readable timed transcript",
    )
    transcript = TimedTranscript.from_dict(data)
    if transcript.source_id != source_id or transcript.transcript_version_id != transcript_id:
        raise ProjectError("transcript identity mismatch")
    return transcript


def _find_source(project: Project, source_id: str) -> SourceAsset:
    for source in project.sources:
        if source.source_id == source_id:
            return source
    raise ProjectError("source is not part of the project")


def _with_display_number(paragraph: ReadableParagraph, number: int) -> ReadableParagraph:
    return replace(paragraph, display_number=f"P{number:03d}")


def _hash_json(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _format_ticks(ticks: int) -> str:
    milliseconds = ticks * 1000 // 120_000
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def _validate_expected_revision(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProjectError("expected_revision must be a non-negative integer")


def _validate_pagination(offset: int, limit: int) -> None:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ProjectError("readable transcript offset must be non-negative")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_PAGE_SIZE:
        raise ProjectError("readable transcript limit must be between 1 and 200")


def _validate_id(value: str, name: str) -> None:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ProjectError(f"{name} is invalid")
