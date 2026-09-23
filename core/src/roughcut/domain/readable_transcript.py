"""Deterministic readable-transcript view and selection result models."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from roughcut.domain.bindings import SourceTranscriptBinding
from roughcut.domain.project import ProjectError
from roughcut.domain.transcript import FineUnit, TranscriptSegment

VIEW_SCHEMA_VERSION = 1
PARAGRAPH_ALGORITHM_VERSION = 1
ADOPTION_STATUSES = {"adopted", "partial", "unadopted", "not_applicable"}


@dataclass(frozen=True)
class ReadableSegmentRef:
    source_id: str
    transcript_version_id: str
    segment_id: str
    start_ticks: int
    end_ticks: int

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "transcript_version_id": self.transcript_version_id,
            "segment_id": self.segment_id,
            "start_ticks": self.start_ticks,
            "end_ticks": self.end_ticks,
        }


@dataclass(frozen=True)
class ReadableParagraph:
    paragraph_id: str
    display_number: str
    source_id: str
    transcript_version_id: str
    source_display_name: str
    local_speaker_id: str | None
    local_speaker_ids: tuple[str, ...]
    person_id: str | None
    person_name: str | None
    text: str
    start_ticks: int
    end_ticks: int
    refs: tuple[ReadableSegmentRef, ...]
    adoption_status: str

    def to_dict(self) -> dict[str, object]:
        return {
            "paragraph_id": self.paragraph_id,
            "display_number": self.display_number,
            "source_id": self.source_id,
            "transcript_version_id": self.transcript_version_id,
            "source_display_name": self.source_display_name,
            "local_speaker_id": self.local_speaker_id,
            "local_speaker_ids": list(self.local_speaker_ids),
            "person_id": self.person_id,
            "person_name": self.person_name,
            "text": self.text,
            "start_ticks": self.start_ticks,
            "end_ticks": self.end_ticks,
            "refs": [ref.to_dict() for ref in self.refs],
            "adoption_status": self.adoption_status,
        }


@dataclass(frozen=True)
class ReadableTranscriptPage:
    view_schema_version: int
    algorithm_version: int
    view_hash: str
    project_id: str
    project_revision: int
    source_bindings: tuple[SourceTranscriptBinding, ...]
    offset: int
    limit: int
    total: int
    next_cursor: int | None
    paragraphs: tuple[ReadableParagraph, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "view_schema_version": self.view_schema_version,
            "algorithm_version": self.algorithm_version,
            "view_hash": self.view_hash,
            "project_id": self.project_id,
            "project_revision": self.project_revision,
            "source_bindings": [binding.to_dict() for binding in self.source_bindings],
            "offset": self.offset,
            "limit": self.limit,
            "total": self.total,
            "next_cursor": self.next_cursor,
            "paragraphs": [paragraph.to_dict() for paragraph in self.paragraphs],
        }


@dataclass(frozen=True)
class ResolvedSelectionRef:
    source_id: str
    transcript_version_id: str
    segment_id: str
    start_ticks: int
    end_ticks: int
    canonical_text: str
    fine_unit_start_index: int | None = None
    fine_unit_end_index: int | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "source_id": self.source_id,
            "transcript_version_id": self.transcript_version_id,
            "segment_id": self.segment_id,
            "start_ticks": self.start_ticks,
            "end_ticks": self.end_ticks,
            "canonical_text": self.canonical_text,
        }
        if self.fine_unit_start_index is not None:
            payload["fine_unit_start_index"] = self.fine_unit_start_index
        if self.fine_unit_end_index is not None:
            payload["fine_unit_end_index"] = self.fine_unit_end_index
        return payload


@dataclass(frozen=True)
class SelectionResolution:
    view_hash: str
    mode: str
    canonical_text: str
    refs: tuple[ResolvedSelectionRef, ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "view_hash": self.view_hash,
            "mode": self.mode,
            "canonical_text": self.canonical_text,
            "refs": [ref.to_dict() for ref in self.refs],
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class CaretNeighbor:
    source_id: str
    transcript_version_id: str
    segment_id: str
    fine_unit_index: int | None
    start_ticks: int
    end_ticks: int

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "transcript_version_id": self.transcript_version_id,
            "segment_id": self.segment_id,
            "fine_unit_index": self.fine_unit_index,
            "start_ticks": self.start_ticks,
            "end_ticks": self.end_ticks,
        }


@dataclass(frozen=True)
class CaretPosition:
    view_hash: str
    paragraph_id: str
    boundary_id: str
    source_id: str
    transcript_version_id: str
    character_offset: int
    utf16_offset: int
    left: CaretNeighbor | None
    right: CaretNeighbor | None
    degraded: bool
    degradation_reason: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "view_hash": self.view_hash,
            "paragraph_id": self.paragraph_id,
            "boundary_id": self.boundary_id,
            "source_id": self.source_id,
            "transcript_version_id": self.transcript_version_id,
            "character_offset": self.character_offset,
            "utf16_offset": self.utf16_offset,
            "left": self.left.to_dict() if self.left is not None else None,
            "right": self.right.to_dict() if self.right is not None else None,
            "degraded": self.degraded,
            "degradation_reason": self.degradation_reason,
        }


@dataclass(frozen=True)
class ContinuousSelectionResolution:
    view_hash: str
    direction: str
    canonical_text: str
    refs: tuple[ResolvedSelectionRef, ...]
    start_caret: CaretPosition
    end_caret: CaretPosition
    adjusted: bool
    degraded: bool
    degradation_reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "view_hash": self.view_hash,
            "direction": self.direction,
            "canonical_text": self.canonical_text,
            "refs": [ref.to_dict() for ref in self.refs],
            "start_caret": self.start_caret.to_dict(),
            "end_caret": self.end_caret.to_dict(),
            "adjusted": self.adjusted,
            "degraded": self.degraded,
            "degradation_reasons": list(self.degradation_reasons),
        }


@dataclass(frozen=True)
class ExactSequenceCaret:
    view_hash: str
    paragraph_id: str
    boundary_id: str
    sequence_index: int
    left_ref: ResolvedSelectionRef | None
    right_ref: ResolvedSelectionRef | None

    def to_dict(self) -> dict[str, object]:
        return {
            "view_hash": self.view_hash,
            "paragraph_id": self.paragraph_id,
            "boundary_id": self.boundary_id,
            "sequence_index": self.sequence_index,
            "left_ref": self.left_ref.to_dict() if self.left_ref is not None else None,
            "right_ref": self.right_ref.to_dict() if self.right_ref is not None else None,
        }


@dataclass(frozen=True)
class ExactRefTransform:
    operation: str
    refs: tuple[ResolvedSelectionRef, ...]
    caret: ExactSequenceCaret
    changed: bool
    reason: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "refs": [ref.to_dict() for ref in self.refs],
            "caret": self.caret.to_dict(),
            "changed": self.changed,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class MarkdownExportResult:
    basis: str
    markdown_path: str
    mapping_path: str
    content_hash: str

    def to_dict(self) -> dict[str, object]:
        return {
            "basis": self.basis,
            "markdown_path": self.markdown_path,
            "mapping_path": self.mapping_path,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class FineUnitTextSpan:
    unit: FineUnit
    fine_unit_index: int
    start_offset: int
    end_offset: int


def effective_text(segment: TranscriptSegment) -> str:
    return segment.corrected_text or segment.original_text


def trusted_fine_unit_spans(segment: TranscriptSegment) -> tuple[FineUnitTextSpan, ...] | None:
    """Align spoken fine units to display text without inventing punctuation timing."""

    return fine_unit_alignment(segment)[0]


def fine_unit_alignment(
    segment: TranscriptSegment,
) -> tuple[tuple[FineUnitTextSpan, ...] | None, str | None]:
    if not segment.fine_units:
        return None, "missing_fine_units"
    previous_end: int | None = None
    spoken_units: list[tuple[int, FineUnit, str]] = []
    for index, unit in enumerate(segment.fine_units):
        if not unit.text:
            return None, "empty_fine_unit_text"
        if unit.start_ticks < segment.start_ticks or unit.end_ticks > segment.end_ticks:
            return None, "fine_unit_out_of_bounds"
        if unit.end_ticks <= unit.start_ticks:
            return None, "fine_unit_range_invalid"
        if previous_end is not None and unit.start_ticks < previous_end:
            return None, "fine_units_overlap_or_disordered"
        previous_end = unit.end_ticks
        spoken = _spoken_text(unit.text)
        if spoken:
            spoken_units.append((index, unit, spoken))
    if not spoken_units:
        return None, "fine_units_contain_no_spoken_text"
    display = effective_text(segment)
    display_characters = [
        (offset, character)
        for offset, character in enumerate(display)
        if not _is_display_only(character)
    ]
    spoken_text = "".join(spoken for _, _, spoken in spoken_units)
    if "".join(character for _, character in display_characters) != spoken_text:
        return None, "spoken_text_mismatch"
    spans: list[FineUnitTextSpan] = []
    spoken_offset = 0
    starts: list[int] = []
    for _, _, spoken in spoken_units:
        starts.append(display_characters[spoken_offset][0])
        spoken_offset += len(spoken)
    for position, (fine_index, unit, _) in enumerate(spoken_units):
        start_offset = 0 if position == 0 else starts[position]
        end_offset = starts[position + 1] if position + 1 < len(starts) else len(display)
        spans.append(FineUnitTextSpan(unit, fine_index, start_offset, end_offset))
    return tuple(spans), None


def exact_fine_unit_spans(segment: TranscriptSegment) -> tuple[FineUnitTextSpan, ...] | None:
    """Preserve the legacy exact-concatenation path for existing callers."""

    if not segment.fine_units or "".join(
        unit.text for unit in segment.fine_units
    ) != effective_text(segment):
        return None
    spans: list[FineUnitTextSpan] = []
    text_offset = 0
    previous_end: int | None = None
    for index, unit in enumerate(segment.fine_units):
        if (
            not unit.text
            or unit.start_ticks < segment.start_ticks
            or unit.end_ticks > segment.end_ticks
            or unit.end_ticks <= unit.start_ticks
            or (previous_end is not None and unit.start_ticks < previous_end)
        ):
            return None
        next_offset = text_offset + len(unit.text)
        spans.append(FineUnitTextSpan(unit, index, text_offset, next_offset))
        text_offset = next_offset
        previous_end = unit.end_ticks
    return tuple(spans)


def canonical_text_for_range(segment: TranscriptSegment, start_ticks: int, end_ticks: int) -> str:
    """Derive the only truthful display text for one exact segment range."""

    if (
        isinstance(start_ticks, bool)
        or not isinstance(start_ticks, int)
        or isinstance(end_ticks, bool)
        or not isinstance(end_ticks, int)
        or start_ticks < segment.start_ticks
        or end_ticks > segment.end_ticks
        or end_ticks <= start_ticks
    ):
        raise ProjectError("range cannot derive canonical text")
    if start_ticks == segment.start_ticks and end_ticks == segment.end_ticks:
        return effective_text(segment)
    spans = exact_fine_unit_spans(segment) or trusted_fine_unit_spans(segment)
    if spans is None:
        return effective_text(segment)
    start_index = (
        0
        if start_ticks == segment.start_ticks
        else next(
            (
                index
                for index, span in enumerate(spans)
                if span.unit.start_ticks == start_ticks
            ),
            None,
        )
    )
    end_index = (
        len(spans) - 1
        if end_ticks == segment.end_ticks
        else next(
            (
                index
                for index, span in enumerate(spans)
                if span.unit.end_ticks == end_ticks
            ),
            None,
        )
    )
    if start_index is None or end_index is None or end_index < start_index:
        return effective_text(segment)
    return effective_text(segment)[spans[start_index].start_offset : spans[end_index].end_offset]


def codepoint_to_utf16_offset(text: str, offset: int) -> int:
    _validate_text_offset(text, offset, "code-point")
    return len(text[:offset].encode("utf-16-le")) // 2


def utf16_to_codepoint_offset(text: str, offset: int) -> int:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ProjectError("UTF-16 offset is invalid")
    consumed = 0
    for index, character in enumerate(text):
        if consumed == offset:
            return index
        width = 2 if ord(character) > 0xFFFF else 1
        if consumed < offset < consumed + width:
            raise ProjectError("UTF-16 offset splits a surrogate pair")
        consumed += width
    if consumed == offset:
        return len(text)
    raise ProjectError("UTF-16 offset is outside text")


def make_sequence_caret(
    view_hash: str,
    paragraph_id: str,
    refs: Sequence[ResolvedSelectionRef],
    sequence_index: int,
) -> ExactSequenceCaret:
    if (
        not isinstance(view_hash, str)
        or not isinstance(paragraph_id, str)
        or not paragraph_id
        or isinstance(sequence_index, bool)
        or not isinstance(sequence_index, int)
        or sequence_index < 0
        or sequence_index > len(refs)
    ):
        raise ProjectError("exact sequence caret is invalid")
    left = refs[sequence_index - 1] if sequence_index else None
    right = refs[sequence_index] if sequence_index < len(refs) else None
    payload = {
        "view_hash": view_hash,
        "paragraph_id": paragraph_id,
        "sequence_index": sequence_index,
        "left": left.to_dict() if left is not None else None,
        "right": right.to_dict() if right is not None else None,
    }
    boundary_id = (
        "boundary_"
        + hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()[:32]
    )
    return ExactSequenceCaret(
        view_hash,
        paragraph_id,
        boundary_id,
        sequence_index,
        left,
        right,
    )


def _spoken_text(value: str) -> str:
    return "".join(character for character in value if not _is_display_only(character))


def _is_display_only(character: str) -> bool:
    return character.isspace() or unicodedata.category(character).startswith("P")


def _validate_text_offset(text: str, offset: int, label: str) -> None:
    if (
        not isinstance(text, str)
        or isinstance(offset, bool)
        or not isinstance(offset, int)
        or offset < 0
        or offset > len(text)
    ):
        raise ProjectError(f"{label} offset is invalid")
