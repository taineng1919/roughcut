"""Minimal project and source-asset models."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from roughcut.domain.errors import ProjectError
from roughcut.domain.people import Person, SpeakerMap
from roughcut.domain.time import TICKS_PER_SECOND, RationalRate

DEFAULT_OUTPUT_WIDTH = 1920
DEFAULT_OUTPUT_HEIGHT = 1080
DEFAULT_AUDIO_SAMPLE_RATE = 48_000
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class ImportMode(str, Enum):
    COPIED = "copied"
    LINKED = "linked"


@dataclass(frozen=True)
class SourceFingerprint:
    size: int
    mtime_ns: int
    sha256_head_tail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "sha256_head_tail": self.sha256_head_tail,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SourceFingerprint:
        return cls(
            size=_integer(data, "size"),
            mtime_ns=_integer(data, "mtime_ns"),
            sha256_head_tail=_string(data, "sha256_head_tail"),
        )


@dataclass(frozen=True)
class MediaProbe:
    duration_ticks: int
    container_start_ticks: int
    first_content_ticks: int
    video_codec: str | None
    width: int | None
    height: int | None
    nominal_frame_rate: dict[str, int] | None
    is_vfr: bool
    audio_codec: str | None
    audio_sample_rate: int | None
    rotation_degrees: int

    def to_dict(self) -> dict[str, object]:
        return {
            "duration_ticks": self.duration_ticks,
            "container_start_ticks": self.container_start_ticks,
            "first_content_ticks": self.first_content_ticks,
            "video_codec": self.video_codec,
            "width": self.width,
            "height": self.height,
            "nominal_frame_rate": self.nominal_frame_rate,
            "is_vfr": self.is_vfr,
            "audio_codec": self.audio_codec,
            "audio_sample_rate": self.audio_sample_rate,
            "rotation_degrees": self.rotation_degrees,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MediaProbe:
        rate = data.get("nominal_frame_rate")
        parsed_rate = None
        if isinstance(rate, dict):
            parsed_rate = {
                "numerator": _integer(rate, "numerator"),
                "denominator": _integer(rate, "denominator"),
            }
        return cls(
            duration_ticks=_integer(data, "duration_ticks"),
            container_start_ticks=_integer(data, "container_start_ticks"),
            first_content_ticks=_integer(data, "first_content_ticks"),
            video_codec=_optional_string(data.get("video_codec")),
            width=_optional_integer(data.get("width")),
            height=_optional_integer(data.get("height")),
            nominal_frame_rate=parsed_rate,
            is_vfr=_boolean(data, "is_vfr"),
            audio_codec=_optional_string(data.get("audio_codec")),
            audio_sample_rate=_optional_integer(data.get("audio_sample_rate")),
            rotation_degrees=_integer(data, "rotation_degrees"),
        )


@dataclass(frozen=True)
class SourceAsset:
    source_id: str
    kind: str
    display_name: str
    import_mode: ImportMode
    locator: dict[str, str]
    fingerprint: SourceFingerprint
    probe: MediaProbe
    tags: tuple[str, ...] = ()
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "tags", normalize_tags(self.tags))
        if not isinstance(self.note, str):
            raise ProjectError("source note must be a string")

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "kind": self.kind,
            "display_name": self.display_name,
            "import_mode": self.import_mode.value,
            "locator": self.locator,
            "fingerprint": self.fingerprint.to_dict(),
            "probe": self.probe.to_dict(),
            "tags": list(self.tags),
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SourceAsset:
        locator = data.get("locator")
        fingerprint = data.get("fingerprint")
        probe = data.get("probe")
        if not isinstance(locator, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in locator.items()
        ):
            raise ProjectError("source locator must contain string keys and values")
        if not isinstance(fingerprint, dict) or not isinstance(probe, dict):
            raise ProjectError("source fingerprint and probe are required")
        try:
            import_mode = ImportMode(_string(data, "import_mode"))
        except ValueError as error:
            raise ProjectError("unsupported import mode") from error
        return cls(
            source_id=_string(data, "source_id"),
            kind=_string(data, "kind"),
            display_name=_string(data, "display_name"),
            import_mode=import_mode,
            locator=dict(locator),
            fingerprint=SourceFingerprint.from_dict(fingerprint),
            probe=MediaProbe.from_dict(probe),
            tags=_string_list(data.get("tags", []), "source tags"),
            note=_empty_string(data.get("note", ""), "source note"),
        )


@dataclass(frozen=True)
class Project:
    schema_version: int
    project_id: str
    revision: int
    name: str
    created_at: str
    updated_at: str
    settings: dict[str, object]
    sources: tuple[SourceAsset, ...]
    active_transcript_versions: dict[str, str]
    active_brief_id: str | None
    active_edit_version_id: str | None
    persons: tuple[Person, ...] = ()
    speaker_maps: tuple[SpeakerMap, ...] = ()
    edit_redo_stack: tuple[str, ...] = ()
    active_content_draft_id: str | None = None

    def __post_init__(self) -> None:
        person_ids = [person.person_id for person in self.persons]
        if len(person_ids) != len(set(person_ids)):
            raise ProjectError("project contains duplicate person ids")
        source_ids = {source.source_id for source in self.sources}
        person_id_set = set(person_ids)
        map_keys = [mapping.identity_key for mapping in self.speaker_maps]
        if len(map_keys) != len(set(map_keys)):
            raise ProjectError("project contains duplicate speaker map identities")
        for mapping in self.speaker_maps:
            if mapping.source_id not in source_ids:
                raise ProjectError("speaker map source is not part of the project")
            if mapping.person_id not in person_id_set:
                raise ProjectError("speaker map person is not part of the project")
        if len(self.edit_redo_stack) != len(set(self.edit_redo_stack)) or any(
            _SAFE_ID.fullmatch(edit_version_id) is None
            for edit_version_id in self.edit_redo_stack
        ):
            raise ProjectError("project edit redo stack is invalid")
        if self.active_content_draft_id is not None and _SAFE_ID.fullmatch(
            self.active_content_draft_id
        ) is None:
            raise ProjectError("project active content draft ID is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "revision": self.revision,
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "settings": self.settings,
            "sources": [source.to_dict() for source in self.sources],
            "active_transcript_versions": self.active_transcript_versions,
            "active_brief_id": self.active_brief_id,
            "active_edit_version_id": self.active_edit_version_id,
            "persons": [person.to_dict() for person in self.persons],
            "speaker_maps": [mapping.to_dict() for mapping in self.speaker_maps],
            "edit_redo_stack": list(self.edit_redo_stack),
            "active_content_draft_id": self.active_content_draft_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Project:
        settings = data.get("settings")
        sources = data.get("sources")
        persons = data.get("persons", [])
        speaker_maps = data.get("speaker_maps", [])
        edit_redo_stack = data.get("edit_redo_stack", [])
        active_transcripts = data.get("active_transcript_versions", {})
        if (
            not isinstance(settings, dict)
            or not isinstance(sources, list)
            or not isinstance(persons, list)
            or not isinstance(speaker_maps, list)
            or not isinstance(edit_redo_stack, list)
        ):
            raise ProjectError("project settings and sources are required")
        if not isinstance(active_transcripts, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in active_transcripts.items()
        ):
            raise ProjectError("active transcript versions must contain strings")
        if _integer(data, "schema_version") != 1:
            raise ProjectError("unsupported project schema version")
        parsed_sources: list[SourceAsset] = []
        for source in sources:
            if not isinstance(source, dict):
                raise ProjectError("project source must be an object")
            parsed_sources.append(SourceAsset.from_dict(source))
        parsed_persons: list[Person] = []
        for person in persons:
            if not isinstance(person, dict):
                raise ProjectError("project person must be an object")
            parsed_persons.append(Person.from_dict(person))
        parsed_maps: list[SpeakerMap] = []
        for mapping in speaker_maps:
            if not isinstance(mapping, dict):
                raise ProjectError("project speaker map must be an object")
            parsed_maps.append(SpeakerMap.from_dict(mapping))
        return cls(
            schema_version=1,
            project_id=_string(data, "project_id"),
            revision=_integer(data, "revision"),
            name=_string(data, "name"),
            created_at=_string(data, "created_at"),
            updated_at=_string(data, "updated_at"),
            settings=normalize_project_settings(settings),
            sources=tuple(parsed_sources),
            active_transcript_versions=dict(active_transcripts),
            active_brief_id=_optional_string(data.get("active_brief_id")),
            active_edit_version_id=_optional_string(data.get("active_edit_version_id")),
            persons=tuple(parsed_persons),
            speaker_maps=tuple(parsed_maps),
            edit_redo_stack=_string_list(edit_redo_stack, "edit redo stack"),
            active_content_draft_id=_optional_string(
                data.get("active_content_draft_id")
            ),
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


def _boolean(data: dict[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ProjectError(f"{key} must be a boolean")
    return value


def _optional_string(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise ProjectError("optional string field has an invalid value")


def _optional_integer(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProjectError("optional integer field has an invalid value")
    return value


def _empty_string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ProjectError(f"{name} must be a string")
    return value


def _string_list(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ProjectError(f"{name} must be a list of strings")
    return tuple(value)


def normalize_tags(tags: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    if not isinstance(tags, (tuple, list)) or not all(isinstance(tag, str) for tag in tags):
        raise ProjectError("source tags must contain strings")
    normalized: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        stripped = tag.strip()
        if stripped and stripped not in seen:
            seen.add(stripped)
            normalized.append(stripped)
    return tuple(normalized)


def normalize_project_settings(settings: dict[str, Any]) -> dict[str, object]:
    """Validate schema-v1 settings and fill only the documented dimension defaults."""
    timebase = settings.get("timebase")
    if isinstance(timebase, bool) or not isinstance(timebase, int) or timebase != TICKS_PER_SECOND:
        raise ProjectError("project timebase is invalid")
    rate_data = settings.get("frame_rate")
    if not isinstance(rate_data, dict):
        raise ProjectError("project frame rate is invalid")
    numerator = rate_data.get("numerator")
    denominator = rate_data.get("denominator")
    if (
        isinstance(numerator, bool)
        or not isinstance(numerator, int)
        or isinstance(denominator, bool)
        or not isinstance(denominator, int)
    ):
        raise ProjectError("project frame rate is invalid")
    try:
        _validated_ticks_per_frame = RationalRate(numerator, denominator).ticks_per_frame
    except ValueError as error:
        raise ProjectError("project frame rate is invalid") from error

    width = settings.get("width", DEFAULT_OUTPUT_WIDTH)
    height = settings.get("height", DEFAULT_OUTPUT_HEIGHT)
    for name, value in (("width", width), ("height", height)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value % 2:
            raise ProjectError(f"project output {name} must be a positive even integer")
    sample_rate = settings.get("audio_sample_rate")
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or sample_rate <= 0:
        raise ProjectError("project audio sample rate is invalid")

    normalized = dict(settings)
    normalized.update(
        {
            "timebase": timebase,
            "frame_rate": {"numerator": numerator, "denominator": denominator},
            "width": width,
            "height": height,
            "audio_sample_rate": sample_rate,
        }
    )
    return normalized
