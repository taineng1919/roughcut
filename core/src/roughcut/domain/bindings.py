"""Explicit source and active-transcript bindings for multi-source contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from roughcut.domain.project import ProjectError

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class SourceTranscriptBinding:
    source_id: str
    transcript_version_id: str

    def __post_init__(self) -> None:
        for name, value in (
            ("source_id", self.source_id),
            ("transcript_version_id", self.transcript_version_id),
        ):
            if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
                raise ProjectError(f"{name} is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "transcript_version_id": self.transcript_version_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SourceTranscriptBinding:
        source_id = data.get("source_id")
        transcript_id = data.get("transcript_version_id")
        if not isinstance(source_id, str) or not isinstance(transcript_id, str):
            raise ProjectError("source binding IDs must be strings")
        return cls(source_id=source_id, transcript_version_id=transcript_id)


def parse_source_bindings(value: object) -> tuple[SourceTranscriptBinding, ...]:
    if not isinstance(value, list) or len(value) < 2:
        raise ProjectError("multi-source contract requires at least two source bindings")
    bindings: list[SourceTranscriptBinding] = []
    for item in value:
        if not isinstance(item, dict):
            raise ProjectError("source binding must be an object")
        bindings.append(SourceTranscriptBinding.from_dict(item))
    source_ids = [binding.source_id for binding in bindings]
    if len(source_ids) != len(set(source_ids)):
        raise ProjectError("multi-source bindings must use unique source IDs")
    return tuple(bindings)
