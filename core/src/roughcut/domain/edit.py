"""Immutable edit proposal and confirmed decision models."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from roughcut.domain.bindings import SourceTranscriptBinding, parse_source_bindings
from roughcut.domain.brief import EditBrief
from roughcut.domain.project import ProjectError

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class EditClip:
    clip_id: str
    source_id: str
    transcript_version_id: str
    segment_id: str
    source_in_ticks: int
    source_out_ticks: int
    reason: str
    display_text: str

    def __post_init__(self) -> None:
        for name, value in (
            ("clip_id", self.clip_id),
            ("source_id", self.source_id),
            ("transcript_version_id", self.transcript_version_id),
            ("segment_id", self.segment_id),
        ):
            if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
                raise ProjectError(f"{name} is invalid")
        for tick_name, tick_value in (
            ("source_in_ticks", self.source_in_ticks),
            ("source_out_ticks", self.source_out_ticks),
        ):
            if isinstance(tick_value, bool) or not isinstance(tick_value, int):
                raise ProjectError(f"{tick_name} must be an integer")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ProjectError("clip reason is required")
        if not isinstance(self.display_text, str) or not self.display_text.strip():
            raise ProjectError("clip display_text is required")

    @property
    def duration_ticks(self) -> int:
        return self.source_out_ticks - self.source_in_ticks

    def to_dict(self) -> dict[str, object]:
        return {
            "clip_id": self.clip_id,
            "source_id": self.source_id,
            "transcript_version_id": self.transcript_version_id,
            "segment_id": self.segment_id,
            "source_in_ticks": self.source_in_ticks,
            "source_out_ticks": self.source_out_ticks,
            "reason": self.reason,
            "display_text": self.display_text,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EditClip:
        return cls(
            clip_id=_string(data, "clip_id"),
            source_id=_string(data, "source_id"),
            transcript_version_id=_string(data, "transcript_version_id"),
            segment_id=_string(data, "segment_id"),
            source_in_ticks=_integer(data, "source_in_ticks"),
            source_out_ticks=_integer(data, "source_out_ticks"),
            reason=_string(data, "reason"),
            display_text=_string(data, "display_text"),
        )


@dataclass(frozen=True)
class EditProposal:
    proposal_id: str
    base_project_revision: int
    base_edit_version_id: str | None
    source_id: str
    transcript_version_id: str
    brief_snapshot: EditBrief
    context_hash: str
    clips: tuple[EditClip, ...]
    total_duration_ticks: int
    created_at: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        for name, value in (
            ("proposal_id", self.proposal_id),
            ("source_id", self.source_id),
            ("transcript_version_id", self.transcript_version_id),
        ):
            if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
                raise ProjectError(f"{name} is invalid")
        if self.base_edit_version_id is not None and _SAFE_ID.fullmatch(
            self.base_edit_version_id
        ) is None:
            raise ProjectError("base_edit_version_id is invalid")
        if (
            isinstance(self.base_project_revision, bool)
            or not isinstance(self.base_project_revision, int)
            or self.base_project_revision < 0
        ):
            raise ProjectError("base project revision is invalid")
        if _SHA256.fullmatch(self.context_hash) is None:
            raise ProjectError("context hash is invalid")
        if not self.clips:
            raise ProjectError("proposal must contain at least one clip")
        if (
            isinstance(self.total_duration_ticks, bool)
            or not isinstance(self.total_duration_ticks, int)
            or self.total_duration_ticks <= 0
        ):
            raise ProjectError("proposal total duration must be positive")
        if not self.created_at:
            raise ProjectError("proposal created_at is required")
        if self.schema_version != 1:
            raise ProjectError("unsupported proposal schema version")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "proposal_id": self.proposal_id,
            "base_project_revision": self.base_project_revision,
            "base_edit_version_id": self.base_edit_version_id,
            "source_id": self.source_id,
            "transcript_version_id": self.transcript_version_id,
            "brief_snapshot": self.brief_snapshot.to_dict(),
            "context_hash": self.context_hash,
            "clips": [clip.to_dict() for clip in self.clips],
            "total_duration_ticks": self.total_duration_ticks,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EditProposal:
        brief = data.get("brief_snapshot")
        clips = data.get("clips")
        if not isinstance(brief, dict) or not isinstance(clips, list):
            raise ProjectError("proposal brief and clips are required")
        parsed_clips: list[EditClip] = []
        for clip in clips:
            if not isinstance(clip, dict):
                raise ProjectError("proposal clip must be an object")
            parsed_clips.append(EditClip.from_dict(clip))
        return cls(
            schema_version=_integer(data, "schema_version"),
            proposal_id=_string(data, "proposal_id"),
            base_project_revision=_integer(data, "base_project_revision"),
            base_edit_version_id=_optional_string(data.get("base_edit_version_id")),
            source_id=_string(data, "source_id"),
            transcript_version_id=_string(data, "transcript_version_id"),
            brief_snapshot=EditBrief.from_dict(brief),
            context_hash=_string(data, "context_hash"),
            clips=tuple(parsed_clips),
            total_duration_ticks=_integer(data, "total_duration_ticks"),
            created_at=_string(data, "created_at"),
        )


@dataclass(frozen=True)
class MultiSourceEditProposal:
    proposal_id: str
    base_project_revision: int
    base_edit_version_id: str | None
    source_bindings: tuple[SourceTranscriptBinding, ...]
    brief_snapshot: EditBrief
    context_hash: str
    clips: tuple[EditClip, ...]
    total_duration_ticks: int
    created_at: str
    schema_version: int = 2

    def __post_init__(self) -> None:
        if _SAFE_ID.fullmatch(self.proposal_id) is None:
            raise ProjectError("proposal_id is invalid")
        if self.base_edit_version_id is not None and _SAFE_ID.fullmatch(
            self.base_edit_version_id
        ) is None:
            raise ProjectError("base_edit_version_id is invalid")
        if (
            isinstance(self.base_project_revision, bool)
            or not isinstance(self.base_project_revision, int)
            or self.base_project_revision < 0
        ):
            raise ProjectError("base project revision is invalid")
        if len(self.source_bindings) < 2:
            raise ProjectError("multi-source proposal requires at least two source bindings")
        source_ids = [binding.source_id for binding in self.source_bindings]
        if len(source_ids) != len(set(source_ids)):
            raise ProjectError("multi-source proposal bindings must use unique source IDs")
        binding_keys = {
            (binding.source_id, binding.transcript_version_id)
            for binding in self.source_bindings
        }
        if any(
            (clip.source_id, clip.transcript_version_id) not in binding_keys
            for clip in self.clips
        ):
            raise ProjectError("proposal clip is outside its source bindings")
        if _SHA256.fullmatch(self.context_hash) is None:
            raise ProjectError("context hash is invalid")
        if not self.clips:
            raise ProjectError("proposal must contain at least one clip")
        if (
            isinstance(self.total_duration_ticks, bool)
            or not isinstance(self.total_duration_ticks, int)
            or self.total_duration_ticks <= 0
        ):
            raise ProjectError("proposal total duration must be positive")
        if not self.created_at:
            raise ProjectError("proposal created_at is required")
        if self.schema_version != 2:
            raise ProjectError("unsupported multi-source proposal schema version")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "proposal_id": self.proposal_id,
            "base_project_revision": self.base_project_revision,
            "base_edit_version_id": self.base_edit_version_id,
            "source_bindings": [binding.to_dict() for binding in self.source_bindings],
            "brief_snapshot": self.brief_snapshot.to_dict(),
            "context_hash": self.context_hash,
            "clips": [clip.to_dict() for clip in self.clips],
            "total_duration_ticks": self.total_duration_ticks,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MultiSourceEditProposal:
        brief = data.get("brief_snapshot")
        clips = data.get("clips")
        if not isinstance(brief, dict) or not isinstance(clips, list):
            raise ProjectError("proposal brief and clips are required")
        parsed_clips: list[EditClip] = []
        for clip in clips:
            if not isinstance(clip, dict):
                raise ProjectError("proposal clip must be an object")
            parsed_clips.append(EditClip.from_dict(clip))
        return cls(
            schema_version=_integer(data, "schema_version"),
            proposal_id=_string(data, "proposal_id"),
            base_project_revision=_integer(data, "base_project_revision"),
            base_edit_version_id=_optional_string(data.get("base_edit_version_id")),
            source_bindings=parse_source_bindings(data.get("source_bindings")),
            brief_snapshot=EditBrief.from_dict(brief),
            context_hash=_string(data, "context_hash"),
            clips=tuple(parsed_clips),
            total_duration_ticks=_integer(data, "total_duration_ticks"),
            created_at=_string(data, "created_at"),
        )


@dataclass(frozen=True)
class EditDecision:
    edit_version_id: str
    proposal_snapshot: EditProposal
    project_revision: int
    created_at: str
    created_by: str = "user"
    schema_version: int = 1

    def __post_init__(self) -> None:
        if _SAFE_ID.fullmatch(self.edit_version_id) is None:
            raise ProjectError("edit_version_id is invalid")
        if (
            isinstance(self.project_revision, bool)
            or not isinstance(self.project_revision, int)
            or self.project_revision < 1
        ):
            raise ProjectError("decision project revision is invalid")
        if self.created_by != "user":
            raise ProjectError("confirmed decision must be created by user")
        if not self.created_at:
            raise ProjectError("decision created_at is required")
        if self.schema_version != 1:
            raise ProjectError("unsupported decision schema version")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "edit_version_id": self.edit_version_id,
            "proposal_snapshot": self.proposal_snapshot.to_dict(),
            "project_revision": self.project_revision,
            "created_at": self.created_at,
            "created_by": self.created_by,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EditDecision:
        proposal = data.get("proposal_snapshot")
        if not isinstance(proposal, dict):
            raise ProjectError("decision proposal snapshot is required")
        return cls(
            schema_version=_integer(data, "schema_version"),
            edit_version_id=_string(data, "edit_version_id"),
            proposal_snapshot=EditProposal.from_dict(proposal),
            project_revision=_integer(data, "project_revision"),
            created_at=_string(data, "created_at"),
            created_by=_string(data, "created_by"),
        )


@dataclass(frozen=True)
class MultiSourceEditDecision:
    edit_version_id: str
    proposal_snapshot: MultiSourceEditProposal
    project_revision: int
    created_at: str
    created_by: str = "user"
    schema_version: int = 2

    def __post_init__(self) -> None:
        if _SAFE_ID.fullmatch(self.edit_version_id) is None:
            raise ProjectError("edit_version_id is invalid")
        if (
            isinstance(self.project_revision, bool)
            or not isinstance(self.project_revision, int)
            or self.project_revision < 1
        ):
            raise ProjectError("decision project revision is invalid")
        if self.created_by != "user":
            raise ProjectError("confirmed decision must be created by user")
        if not self.created_at:
            raise ProjectError("decision created_at is required")
        if self.schema_version != 2:
            raise ProjectError("unsupported multi-source decision schema version")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "edit_version_id": self.edit_version_id,
            "proposal_snapshot": self.proposal_snapshot.to_dict(),
            "project_revision": self.project_revision,
            "created_at": self.created_at,
            "created_by": self.created_by,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MultiSourceEditDecision:
        proposal = data.get("proposal_snapshot")
        if not isinstance(proposal, dict):
            raise ProjectError("decision proposal snapshot is required")
        return cls(
            schema_version=_integer(data, "schema_version"),
            edit_version_id=_string(data, "edit_version_id"),
            proposal_snapshot=MultiSourceEditProposal.from_dict(proposal),
            project_revision=_integer(data, "project_revision"),
            created_at=_string(data, "created_at"),
            created_by=_string(data, "created_by"),
        )


def _string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ProjectError(f"{key} must be a non-empty string")
    return value


def _integer(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProjectError(f"{key} must be an integer")
    return value


def _optional_string(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise ProjectError("optional edit string has an invalid value")
