"""Versioned, path-free Agent context model."""

from __future__ import annotations

from dataclasses import dataclass

from roughcut.domain.bindings import SourceTranscriptBinding
from roughcut.domain.brief import EditBrief
from roughcut.domain.edit import EditClip


@dataclass(frozen=True)
class AgentContextPage:
    context_hash: str
    project_id: str
    project_revision: int
    source: dict[str, object]
    transcript_version_id: str
    brief: EditBrief
    settings: dict[str, object]
    allowed_operations: tuple[str, ...]
    offset: int
    limit: int
    total: int
    next_offset: int | None
    persons: tuple[dict[str, object], ...]
    speaker_maps: tuple[dict[str, object], ...]
    segments: tuple[dict[str, object], ...]
    schema_version: int = 1

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "context_hash": self.context_hash,
            "project_id": self.project_id,
            "project_revision": self.project_revision,
            "source": self.source,
            "transcript_version_id": self.transcript_version_id,
            "brief": self.brief.to_dict(),
            "settings": self.settings,
            "allowed_operations": list(self.allowed_operations),
            "offset": self.offset,
            "limit": self.limit,
            "total": self.total,
            "next_offset": self.next_offset,
            "persons": list(self.persons),
            "speaker_maps": list(self.speaker_maps),
            "segments": list(self.segments),
        }


@dataclass(frozen=True)
class MultiSourceAgentContextPage:
    context_hash: str
    project_id: str
    project_revision: int
    source_bindings: tuple[SourceTranscriptBinding, ...]
    sources: tuple[dict[str, object], ...]
    brief: EditBrief
    settings: dict[str, object]
    allowed_operations: tuple[str, ...]
    offset: int
    limit: int
    total: int
    next_offset: int | None
    persons: tuple[dict[str, object], ...]
    speaker_maps: tuple[dict[str, object], ...]
    segments: tuple[dict[str, object], ...]
    schema_version: int = 2

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "context_hash": self.context_hash,
            "project_id": self.project_id,
            "project_revision": self.project_revision,
            "source_bindings": [binding.to_dict() for binding in self.source_bindings],
            "sources": list(self.sources),
            "brief": self.brief.to_dict(),
            "settings": self.settings,
            "allowed_operations": list(self.allowed_operations),
            "offset": self.offset,
            "limit": self.limit,
            "total": self.total,
            "next_offset": self.next_offset,
            "persons": list(self.persons),
            "speaker_maps": list(self.speaker_maps),
            "segments": list(self.segments),
        }


@dataclass(frozen=True)
class RevisionContextPage:
    context_hash: str
    project_id: str
    project_revision: int
    base_edit_version_id: str
    edit_schema_version: int
    source_bindings: tuple[SourceTranscriptBinding, ...]
    sources: tuple[dict[str, object], ...]
    base_clips: tuple[EditClip, ...]
    base_total_duration_ticks: int
    brief: EditBrief
    settings: dict[str, object]
    allowed_operations: tuple[str, ...]
    offset: int
    limit: int
    total: int
    next_offset: int | None
    persons: tuple[dict[str, object], ...]
    speaker_maps: tuple[dict[str, object], ...]
    segments: tuple[dict[str, object], ...]
    schema_version: int = 1

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "context_hash": self.context_hash,
            "project_id": self.project_id,
            "project_revision": self.project_revision,
            "base_edit_version_id": self.base_edit_version_id,
            "edit_schema_version": self.edit_schema_version,
            "source_bindings": [binding.to_dict() for binding in self.source_bindings],
            "sources": list(self.sources),
            "base_clips": [clip.to_dict() for clip in self.base_clips],
            "base_total_duration_ticks": self.base_total_duration_ticks,
            "brief": self.brief.to_dict(),
            "settings": self.settings,
            "allowed_operations": list(self.allowed_operations),
            "offset": self.offset,
            "limit": self.limit,
            "total": self.total,
            "next_offset": self.next_offset,
            "persons": list(self.persons),
            "speaker_maps": list(self.speaker_maps),
            "segments": list(self.segments),
        }
