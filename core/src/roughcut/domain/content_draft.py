"""Immutable Content Draft schema between an Edit Brief and Proposal."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, TypeAlias

from roughcut.domain.bindings import SourceTranscriptBinding
from roughcut.domain.brief import EditBrief
from roughcut.domain.project import ProjectError

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
NARRATION_STATUSES = {"draft", "approved", "recorded"}
_MAX_DISPLAY_TITLE_LENGTH = 80
MAX_EDITORIAL_PUNCTUATION_RUN = 8


def is_unicode_punctuation(character: str) -> bool:
    """Return whether one complete Unicode code point is in General Category P*."""
    return len(character) == 1 and unicodedata.category(character).startswith("P")


def punctuation_stripped(value: str) -> str:
    """Remove only Unicode P* code points; preserve every other code point verbatim."""
    return "".join(character for character in value if not is_unicode_punctuation(character))


def validate_source_display_text(
    canonical_text: str,
    display_text: str | None,
) -> str | None:
    """Validate and canonicalize the optional schema 2 editorial display field."""
    if display_text is None or display_text == canonical_text:
        return None
    if not isinstance(display_text, str) or not display_text.strip():
        raise ProjectError("content draft source display text is invalid")
    if punctuation_stripped(display_text) != punctuation_stripped(canonical_text):
        raise ProjectError(
            "content draft source display text must differ from canonical text only by punctuation"
        )
    return display_text


def validate_punctuation_replacement(
    canonical_text: str,
    current_display_text: str,
    start_offset: int,
    end_offset: int,
    replacement: str,
    *,
    max_run: int = MAX_EDITORIAL_PUNCTUATION_RUN,
) -> str:
    """Validate one code-point range replacement and return the next display text."""
    if (
        isinstance(start_offset, bool)
        or not isinstance(start_offset, int)
        or isinstance(end_offset, bool)
        or not isinstance(end_offset, int)
        or start_offset < 0
        or end_offset < start_offset
        or end_offset > len(current_display_text)
    ):
        raise ProjectError("content draft punctuation range is invalid")
    if not isinstance(replacement, str) or any(
        not is_unicode_punctuation(character) for character in replacement
    ):
        raise ProjectError("content draft punctuation replacement must contain only punctuation")
    if any(
        not is_unicode_punctuation(character)
        for character in current_display_text[start_offset:end_offset]
    ):
        raise ProjectError("content draft punctuation range must contain only punctuation")
    if len(replacement) > max_run:
        raise ProjectError("content draft punctuation replacement exceeds 8 characters")
    next_display_text = (
        current_display_text[:start_offset]
        + replacement
        + current_display_text[end_offset:]
    )
    if next_display_text == current_display_text:
        raise ProjectError("content draft punctuation edit is unchanged")
    validated = validate_source_display_text(canonical_text, next_display_text)
    if validated is None:
        validated = canonical_text
    if not validated.strip():
        raise ProjectError("content draft source display text cannot be empty")
    replacement_start = start_offset
    replacement_end = start_offset + len(replacement)
    for run_start, run_end in _punctuation_runs(next_display_text):
        touches_change = (
            run_start <= replacement_end and replacement_start <= run_end
        )
        if touches_change and run_end - run_start > max_run:
            raise ProjectError("content draft punctuation run exceeds 8 characters")
    return validated


def split_display_text_for_canonical_parts(
    display_text: str,
    canonical_parts: tuple[str, ...] | list[str],
    *,
    connection: str = "\n",
) -> tuple[str, ...]:
    """Split one display at canonical part boundaries without duplicating text."""
    parts = tuple(canonical_parts)
    if not parts:
        raise ProjectError("content draft display split requires canonical parts")
    canonical = connection.join(parts)
    if punctuation_stripped(display_text) != punctuation_stripped(canonical):
        raise ProjectError("content draft display cannot be split against canonical refs")
    if len(parts) == 1:
        return (display_text,)

    display_non_punctuation_positions = [
        index
        for index, character in enumerate(display_text)
        if not is_unicode_punctuation(character)
    ]
    canonical_non_punctuation = punctuation_stripped(canonical)
    if len(display_non_punctuation_positions) != len(canonical_non_punctuation):
        raise ProjectError("content draft display split has an invalid code-point mapping")
    if connection == "":
        result_without_connection: list[str] = []
        left = 0
        cursor = 0
        for index, part in enumerate(parts):
            part_count = len(punctuation_stripped(part))
            cursor += part_count
            right = (
                display_non_punctuation_positions[cursor]
                if index + 1 < len(parts)
                else len(display_text)
            )
            fragment = display_text[left:right]
            if punctuation_stripped(fragment) != punctuation_stripped(part):
                raise ProjectError("content draft display split is ambiguous")
            result_without_connection.append(fragment)
            left = right
        return tuple(result_without_connection)

    separator_positions: list[int] = []
    cursor = 0
    for index, part in enumerate(parts[:-1]):
        cursor += len(punctuation_stripped(part))
        if canonical_non_punctuation[cursor] != "\n":
            raise ProjectError("content draft source refs have an invalid fixed connection")
        separator_positions.append(display_non_punctuation_positions[cursor])
        cursor += 1

    result: list[str] = []
    left = 0
    for index, part in enumerate(parts):
        right = separator_positions[index] if index < len(separator_positions) else len(display_text)
        fragment = display_text[left:right]
        if punctuation_stripped(fragment) != punctuation_stripped(part):
            raise ProjectError("content draft display split is ambiguous")
        result.append(fragment)
        left = right + 1 if index < len(separator_positions) else right
    return tuple(result)


def _punctuation_runs(value: str) -> tuple[tuple[int, int], ...]:
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, character in enumerate(value):
        if is_unicode_punctuation(character):
            start = index if start is None else start
            continue
        if start is not None:
            runs.append((start, index))
            start = None
    if start is not None:
        runs.append((start, len(value)))
    return tuple(runs)


@dataclass(frozen=True)
class ContentDraftRef:
    source_id: str
    transcript_version_id: str
    segment_id: str
    start_ticks: int
    end_ticks: int

    def __post_init__(self) -> None:
        for name, value in (
            ("source_id", self.source_id),
            ("transcript_version_id", self.transcript_version_id),
            ("segment_id", self.segment_id),
        ):
            if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
                raise ProjectError(f"content draft {name} is invalid")
        if (
            isinstance(self.start_ticks, bool)
            or not isinstance(self.start_ticks, int)
            or isinstance(self.end_ticks, bool)
            or not isinstance(self.end_ticks, int)
            or self.start_ticks < 0
            or self.end_ticks <= self.start_ticks
        ):
            raise ProjectError("content draft ref range is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "transcript_version_id": self.transcript_version_id,
            "segment_id": self.segment_id,
            "start_ticks": self.start_ticks,
            "end_ticks": self.end_ticks,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContentDraftRef:
        return cls(
            source_id=_string(data, "source_id"),
            transcript_version_id=_string(data, "transcript_version_id"),
            segment_id=_string(data, "segment_id"),
            start_ticks=_integer(data, "start_ticks"),
            end_ticks=_integer(data, "end_ticks"),
        )


@dataclass(frozen=True)
class SourceExcerptBlock:
    block_id: str
    refs: tuple[ContentDraftRef, ...]
    canonical_text: str
    section_title: str | None = None
    kind: str = "source_excerpt"
    display_text: str | None = None

    def __post_init__(self) -> None:
        _validate_id(self.block_id, "block_id")
        if self.kind != "source_excerpt":
            raise ProjectError("content draft source block kind is invalid")
        if not self.refs:
            raise ProjectError("content draft source block requires refs")
        if not isinstance(self.canonical_text, str) or not self.canonical_text.strip():
            raise ProjectError("content draft source canonical text is required")
        object.__setattr__(
            self,
            "display_text",
            validate_source_display_text(self.canonical_text, self.display_text),
        )
        object.__setattr__(
            self,
            "section_title",
            _normalized_optional_title(self.section_title, "section title"),
        )

    def to_dict(self, *, schema_version: int = 1) -> dict[str, object]:
        payload: dict[str, object] = {
            "block_id": self.block_id,
            "kind": self.kind,
            "refs": [ref.to_dict() for ref in self.refs],
            "canonical_text": self.canonical_text,
        }
        if schema_version == 1:
            if self.display_text is not None:
                raise ProjectError("schema 1 source block cannot contain display_text")
            payload["section_title"] = self.section_title
        elif schema_version == 2 and self.display_text is not None:
            payload["display_text"] = self.display_text
        elif schema_version != 2:
            raise ProjectError("unsupported source block schema version")
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, schema_version: int = 1) -> SourceExcerptBlock:
        if schema_version == 2 and "section_title" in data:
            raise ProjectError("schema 2 source block cannot contain section_title")
        if schema_version == 1 and "display_text" in data:
            raise ProjectError("schema 1 source block cannot contain display_text")
        if schema_version == 2 and "display_text" in data and not isinstance(
            data["display_text"], str
        ):
            raise ProjectError("content draft source display text is invalid")
        return cls(
            block_id=_string(data, "block_id"),
            kind=_string(data, "kind"),
            refs=_refs(data.get("refs"), "source block refs"),
            canonical_text=_string(data, "canonical_text"),
            display_text=(
                data.get("display_text") if schema_version == 2 else None
            ),
            section_title=(
                None
                if schema_version == 2
                else _optional_title(data.get("section_title"), "section title")
            ),
        )


@dataclass(frozen=True)
class NarrationBlock:
    block_id: str
    text: str
    status: str
    recorded_refs: tuple[ContentDraftRef, ...]
    section_title: str | None = None
    kind: str = "narration"

    def __post_init__(self) -> None:
        _validate_id(self.block_id, "block_id")
        if self.kind != "narration":
            raise ProjectError("content draft narration kind is invalid")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ProjectError("content draft narration text is required")
        object.__setattr__(self, "text", self.text.strip())
        if self.status not in NARRATION_STATUSES:
            raise ProjectError("content draft narration status is invalid")
        if self.status == "recorded" and not self.recorded_refs:
            raise ProjectError("recorded narration requires refs")
        if self.status != "recorded" and self.recorded_refs:
            raise ProjectError("unrecorded narration cannot contain refs")
        object.__setattr__(
            self,
            "section_title",
            _normalized_optional_title(self.section_title, "section title"),
        )

    def to_dict(self, *, schema_version: int = 1) -> dict[str, object]:
        payload: dict[str, object] = {
            "block_id": self.block_id,
            "kind": self.kind,
            "text": self.text,
            "status": self.status,
            "recorded_refs": [ref.to_dict() for ref in self.recorded_refs],
        }
        if schema_version == 1:
            payload["section_title"] = self.section_title
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, schema_version: int = 1) -> NarrationBlock:
        if schema_version == 2 and "section_title" in data:
            raise ProjectError("schema 2 narration cannot contain section_title")
        return cls(
            block_id=_string(data, "block_id"),
            kind=_string(data, "kind"),
            text=_string(data, "text"),
            status=_string(data, "status"),
            recorded_refs=_refs(data.get("recorded_refs"), "recorded narration refs"),
            section_title=(
                None
                if schema_version == 2
                else _optional_title(data.get("section_title"), "section title")
            ),
        )


@dataclass(frozen=True)
class SectionTitleBlock:
    """An independent schema 2 heading; it has no media or timing payload."""

    block_id: str
    title: str
    kind: str = "section_title"

    def __post_init__(self) -> None:
        _validate_id(self.block_id, "block_id")
        if self.kind != "section_title":
            raise ProjectError("content draft section title kind is invalid")
        normalized = _normalized_optional_title(self.title, "section title")
        if normalized is None:
            raise ProjectError("content draft section title is required")
        object.__setattr__(self, "title", normalized)

    def to_dict(self, *, schema_version: int = 2) -> dict[str, object]:
        if schema_version != 2:
            raise ProjectError("section title is only valid in schema 2")
        return {
            "block_id": self.block_id,
            "kind": self.kind,
            "title": self.title,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SectionTitleBlock:
        return cls(
            block_id=_string(data, "block_id"),
            kind=_string(data, "kind"),
            title=_string(data, "title"),
        )


ContentDraftBlock: TypeAlias = SourceExcerptBlock | NarrationBlock | SectionTitleBlock
# The architecture uses both names in prose; keep one concrete type and one
# discoverable alias so callers do not invent a second heading representation.
SectionHeadingBlock = SectionTitleBlock


def legacy_section_block_id(content_draft_id: str, block_id: str, block_index: int) -> str:
    """Return the deterministic schema 1 projection ID required by the contract."""
    _validate_id(content_draft_id, "content_draft_id")
    _validate_id(block_id, "block_id")
    if isinstance(block_index, bool) or not isinstance(block_index, int) or block_index < 0:
        raise ProjectError("legacy section block index is invalid")
    material = (
        content_draft_id.encode("utf-8")
        + b"\x00"
        + block_id.encode("utf-8")
        + b"\x00"
        + str(block_index).encode("ascii")
    )
    return "legacy_section_" + hashlib.sha256(material).hexdigest()[:32]


def project_schema1_to_schema2(parent: ContentDraft) -> ContentDraft:
    """Project an immutable schema 1 artifact for its first editor child.

    The parent is never changed.  Every legacy embedded title becomes an
    independent heading immediately before its block.  Any identity collision
    is an integrity failure rather than an aliasing opportunity.
    """
    if parent.schema_version != 1:
        return parent
    persistent_ids = {block.block_id for block in parent.blocks}
    projected: list[ContentDraftBlock] = []
    projected_ids: set[str] = set()
    for index, block in enumerate(parent.blocks):
        title = getattr(block, "section_title", None)
        if title is not None:
            heading_id = legacy_section_block_id(
                parent.content_draft_id, block.block_id, index
            )
            if (
                heading_id in persistent_ids
                or heading_id in projected_ids
                or heading_id in {item.block_id for item in projected}
            ):
                raise ProjectError("schema 1 section projection has an identity collision")
            projected.append(SectionTitleBlock(block_id=heading_id, title=title))
            projected_ids.add(heading_id)
        if isinstance(block, SourceExcerptBlock):
            projected.append(
                SourceExcerptBlock(
                    block_id=block.block_id,
                    refs=block.refs,
                    canonical_text=block.canonical_text,
                    display_text=block.display_text,
                )
            )
        elif isinstance(block, NarrationBlock):
            projected.append(
                NarrationBlock(
                    block_id=block.block_id,
                    text=block.text,
                    status=block.status,
                    recorded_refs=block.recorded_refs,
                )
            )
        else:  # schema 1 cannot contain headings
            raise ProjectError("schema 1 contains an unsupported block")
    return ContentDraft(
        content_draft_id=parent.content_draft_id,
        parent_draft_id=parent.parent_draft_id,
        base_project_revision=parent.base_project_revision,
        confirmed_by_user=parent.confirmed_by_user,
        brief_snapshot=parent.brief_snapshot,
        source_bindings=parent.source_bindings,
        context_hash=parent.context_hash,
        blocks=tuple(projected),
        display_title=parent.display_title,
        schema_version=2,
    )


@dataclass(frozen=True)
class ContentDraft:
    content_draft_id: str
    parent_draft_id: str | None
    base_project_revision: int
    confirmed_by_user: bool
    brief_snapshot: EditBrief
    source_bindings: tuple[SourceTranscriptBinding, ...]
    context_hash: str
    blocks: tuple[ContentDraftBlock, ...]
    display_title: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        _validate_id(self.content_draft_id, "content_draft_id")
        if self.parent_draft_id is not None:
            _validate_id(self.parent_draft_id, "parent_draft_id")
            if self.parent_draft_id == self.content_draft_id:
                raise ProjectError("content draft parent cannot reference itself")
        if (
            isinstance(self.base_project_revision, bool)
            or not isinstance(self.base_project_revision, int)
            or self.base_project_revision < 0
        ):
            raise ProjectError("content draft base project revision is invalid")
        if not isinstance(self.confirmed_by_user, bool):
            raise ProjectError("content draft confirmed flag is invalid")
        if self.schema_version not in {1, 2}:
            raise ProjectError("unsupported content draft schema version")
        if not self.source_bindings:
            raise ProjectError("content draft requires source bindings")
        source_ids = [binding.source_id for binding in self.source_bindings]
        if len(source_ids) != len(set(source_ids)):
            raise ProjectError("content draft bindings require unique source IDs")
        if not isinstance(self.context_hash, str) or _SHA256.fullmatch(self.context_hash) is None:
            raise ProjectError("content draft context hash is invalid")
        if not self.blocks:
            raise ProjectError("content draft requires blocks")
        object.__setattr__(
            self,
            "display_title",
            _normalized_optional_title(self.display_title, "display title"),
        )
        block_ids = [block.block_id for block in self.blocks]
        if len(block_ids) != len(set(block_ids)):
            raise ProjectError("content draft contains duplicate block IDs")
        binding_keys = {
            (binding.source_id, binding.transcript_version_id)
            for binding in self.source_bindings
        }
        if self.schema_version == 1 and any(
            isinstance(block, SectionTitleBlock) for block in self.blocks
        ):
            raise ProjectError("schema 1 cannot contain section title blocks")
        if self.schema_version == 2 and any(
            getattr(block, "section_title", None) is not None
            for block in self.blocks
            if isinstance(block, (SourceExcerptBlock, NarrationBlock))
        ):
            raise ProjectError("schema 2 blocks cannot contain section_title")
        for block in self.blocks:
            if isinstance(block, SectionTitleBlock):
                continue
            refs = block.refs if isinstance(block, SourceExcerptBlock) else block.recorded_refs
            if any((ref.source_id, ref.transcript_version_id) not in binding_keys for ref in refs):
                raise ProjectError("content draft ref is outside source bindings")
        if self.confirmed_by_user and any(
            isinstance(block, NarrationBlock) and block.status == "draft"
            for block in self.blocks
        ):
            raise ProjectError("confirmed content draft cannot contain draft narration")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "content_draft_id": self.content_draft_id,
            "parent_draft_id": self.parent_draft_id,
            "base_project_revision": self.base_project_revision,
            "confirmed_by_user": self.confirmed_by_user,
            "brief_snapshot": self.brief_snapshot.to_dict(),
            "source_bindings": [binding.to_dict() for binding in self.source_bindings],
            "context_hash": self.context_hash,
            "blocks": [block.to_dict(schema_version=self.schema_version) for block in self.blocks],
            "display_title": self.display_title,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContentDraft:
        brief = data.get("brief_snapshot")
        bindings = data.get("source_bindings")
        blocks = data.get("blocks")
        if not isinstance(brief, dict) or not isinstance(bindings, list) or not isinstance(
            blocks, list
        ):
            raise ProjectError("content draft brief, bindings and blocks are required")
        parsed_bindings: list[SourceTranscriptBinding] = []
        for binding in bindings:
            if not isinstance(binding, dict):
                raise ProjectError("content draft binding must be an object")
            parsed_bindings.append(SourceTranscriptBinding.from_dict(binding))
        schema_version = _integer(data, "schema_version")
        if schema_version not in {1, 2}:
            raise ProjectError("unsupported content draft schema version")
        parsed_blocks: list[ContentDraftBlock] = []
        for block in blocks:
            if not isinstance(block, dict):
                raise ProjectError("content draft block must be an object")
            kind = block.get("kind")
            if kind == "source_excerpt":
                if schema_version == 1:
                    expected_fields = {
                        "block_id",
                        "kind",
                        "refs",
                        "canonical_text",
                    }
                    if set(block) not in (
                        expected_fields,
                        expected_fields | {"section_title"},
                    ):
                        raise ProjectError("content draft source block fields are invalid")
                    expected_fields = set(block)
                elif "display_text" in block:
                    expected_fields = {
                        "block_id",
                        "kind",
                        "refs",
                        "canonical_text",
                        "display_text",
                    }
                else:
                    expected_fields = {
                        "block_id",
                        "kind",
                        "refs",
                        "canonical_text",
                    }
                if set(block) != expected_fields:
                    raise ProjectError("content draft source block fields are invalid")
                if schema_version == 2 and "display_text" in block and not isinstance(
                    block["display_text"], str
                ):
                    raise ProjectError("content draft source display text is invalid")
                parsed_blocks.append(
                    SourceExcerptBlock.from_dict(block, schema_version=schema_version)
                )
            elif kind == "narration":
                if schema_version == 2 and set(block) != {
                    "block_id", "kind", "text", "status", "recorded_refs"
                }:
                    raise ProjectError("schema 2 narration block fields are invalid")
                parsed_blocks.append(
                    NarrationBlock.from_dict(block, schema_version=schema_version)
                )
            elif kind == "section_title" and schema_version == 2:
                if set(block) != {"block_id", "kind", "title"}:
                    raise ProjectError("schema 2 section title fields are invalid")
                parsed_blocks.append(SectionTitleBlock.from_dict(block))
            else:
                raise ProjectError("content draft block kind is invalid")
        return cls(
            schema_version=schema_version,
            content_draft_id=_string(data, "content_draft_id"),
            parent_draft_id=_optional_string(data.get("parent_draft_id")),
            base_project_revision=_integer(data, "base_project_revision"),
            confirmed_by_user=_boolean(data, "confirmed_by_user"),
            brief_snapshot=EditBrief.from_dict(brief),
            source_bindings=tuple(parsed_bindings),
            context_hash=_string(data, "context_hash"),
            blocks=tuple(parsed_blocks),
            display_title=_optional_title(data.get("display_title"), "display title"),
        )


def _refs(value: object, name: str) -> tuple[ContentDraftRef, ...]:
    if not isinstance(value, list):
        raise ProjectError(f"{name} must be an array")
    result: list[ContentDraftRef] = []
    for item in value:
        if not isinstance(item, dict):
            raise ProjectError(f"{name} must contain objects")
        result.append(ContentDraftRef.from_dict(item))
    return tuple(result)


def _validate_id(value: str, name: str) -> None:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ProjectError(f"content draft {name} is invalid")


def _string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ProjectError(f"content draft {key} must be a non-empty string")
    return value


def _integer(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProjectError(f"content draft {key} must be an integer")
    return value


def _boolean(data: dict[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ProjectError(f"content draft {key} must be a boolean")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProjectError("content draft optional ID is invalid")
    return value


def _optional_title(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProjectError(f"content draft {name} is invalid")
    return value


def _normalized_optional_title(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProjectError(f"content draft {name} is invalid")
    normalized = value.strip()
    if not normalized or len(normalized) > _MAX_DISPLAY_TITLE_LENGTH:
        raise ProjectError(f"content draft {name} is invalid")
    return normalized
