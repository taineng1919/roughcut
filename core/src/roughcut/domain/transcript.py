"""Timed transcript domain models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from roughcut.domain.project import ProjectError

FINE_UNIT_KINDS = {"word", "character", "token", "unknown"}
EDITORIAL_MARKS = {"unmarked", "include", "exclude", "maybe"}


@dataclass(frozen=True)
class FineUnit:
    kind: str
    text: str
    start_ticks: int
    end_ticks: int
    confidence: float | None

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "text": self.text,
            "start_ticks": self.start_ticks,
            "end_ticks": self.end_ticks,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FineUnit:
        kind = _string(data, "kind")
        if kind not in FINE_UNIT_KINDS:
            raise ProjectError("unsupported fine unit kind")
        return cls(
            kind=kind,
            text=_string(data, "text"),
            start_ticks=_integer(data, "start_ticks"),
            end_ticks=_integer(data, "end_ticks"),
            confidence=_optional_number(data.get("confidence")),
        )


@dataclass(frozen=True)
class TranscriptSegment:
    segment_id: str
    start_ticks: int
    end_ticks: int
    original_text: str
    corrected_text: str | None
    local_speaker_id: str | None
    person_id: str | None
    confidence: float | None
    fine_units: tuple[FineUnit, ...]
    editorial_mark: str

    def to_dict(self) -> dict[str, object]:
        return {
            "segment_id": self.segment_id,
            "start_ticks": self.start_ticks,
            "end_ticks": self.end_ticks,
            "original_text": self.original_text,
            "corrected_text": self.corrected_text,
            "local_speaker_id": self.local_speaker_id,
            "person_id": self.person_id,
            "confidence": self.confidence,
            "fine_units": [unit.to_dict() for unit in self.fine_units],
            "editorial_mark": self.editorial_mark,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TranscriptSegment:
        fine_units = data.get("fine_units")
        if not isinstance(fine_units, list):
            raise ProjectError("transcript fine_units must be a list")
        mark = _string(data, "editorial_mark")
        if mark not in EDITORIAL_MARKS:
            raise ProjectError("unsupported editorial mark")
        parsed_units = []
        for unit in fine_units:
            if not isinstance(unit, dict):
                raise ProjectError("transcript fine unit must be an object")
            parsed_units.append(FineUnit.from_dict(unit))
        return cls(
            segment_id=_string(data, "segment_id"),
            start_ticks=_integer(data, "start_ticks"),
            end_ticks=_integer(data, "end_ticks"),
            original_text=_string(data, "original_text"),
            corrected_text=_optional_string(data.get("corrected_text")),
            local_speaker_id=_optional_string(data.get("local_speaker_id")),
            person_id=_optional_string(data.get("person_id")),
            confidence=_optional_number(data.get("confidence")),
            fine_units=tuple(parsed_units),
            editorial_mark=mark,
        )


@dataclass(frozen=True)
class TranscriptProvenance:
    backend: str
    package_version: str
    models: dict[str, str]
    parameters: dict[str, object]
    raw_result_path: str
    started_at: str
    completed_at: str
    exit_status: int

    def to_dict(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "package_version": self.package_version,
            "models": self.models,
            "parameters": self.parameters,
            "raw_result_path": self.raw_result_path,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "exit_status": self.exit_status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TranscriptProvenance:
        models = data.get("models")
        parameters = data.get("parameters")
        if not isinstance(models, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in models.items()
        ):
            raise ProjectError("transcript models must contain strings")
        if not isinstance(parameters, dict):
            raise ProjectError("transcript parameters must be an object")
        return cls(
            backend=_string(data, "backend"),
            package_version=_string(data, "package_version"),
            models=dict(models),
            parameters=dict(parameters),
            raw_result_path=_string(data, "raw_result_path"),
            started_at=_string(data, "started_at"),
            completed_at=_string(data, "completed_at"),
            exit_status=_integer(data, "exit_status"),
        )


@dataclass(frozen=True)
class TimedTranscript:
    schema_version: int
    transcript_version_id: str
    source_id: str
    parent_version_id: str | None
    provenance: TranscriptProvenance
    language: str
    segments: tuple[TranscriptSegment, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "transcript_version_id": self.transcript_version_id,
            "source_id": self.source_id,
            "parent_version_id": self.parent_version_id,
            "provenance": self.provenance.to_dict(),
            "language": self.language,
            "segments": [segment.to_dict() for segment in self.segments],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TimedTranscript:
        provenance = data.get("provenance")
        segments = data.get("segments")
        if not isinstance(provenance, dict) or not isinstance(segments, list):
            raise ProjectError("transcript provenance and segments are required")
        if _integer(data, "schema_version") != 1:
            raise ProjectError("unsupported transcript schema version")
        parsed_segments = []
        for segment in segments:
            if not isinstance(segment, dict):
                raise ProjectError("transcript segment must be an object")
            parsed_segments.append(TranscriptSegment.from_dict(segment))
        return cls(
            schema_version=1,
            transcript_version_id=_string(data, "transcript_version_id"),
            source_id=_string(data, "source_id"),
            parent_version_id=_optional_string(data.get("parent_version_id")),
            provenance=TranscriptProvenance.from_dict(provenance),
            language=_string(data, "language"),
            segments=tuple(parsed_segments),
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
    raise ProjectError("optional transcript string has an invalid value")


def _optional_number(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProjectError("optional transcript number has an invalid value")
    return float(value)
