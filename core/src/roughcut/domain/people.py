"""Project people and user-confirmed local speaker mappings."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from roughcut.domain.errors import ProjectError

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def validate_safe_id(value: str, name: str) -> None:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ProjectError(f"{name} is invalid")


@dataclass(frozen=True)
class Person:
    person_id: str
    name: str
    role: str
    note: str

    def __post_init__(self) -> None:
        validate_safe_id(self.person_id, "person_id")
        for name, value in (("name", self.name), ("role", self.role)):
            if not isinstance(value, str) or not value.strip():
                raise ProjectError(f"person {name} is required")
        if not isinstance(self.note, str):
            raise ProjectError("person note must be a string")

    def to_dict(self) -> dict[str, object]:
        return {
            "person_id": self.person_id,
            "name": self.name,
            "role": self.role,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Person:
        return cls(
            person_id=_string(data, "person_id"),
            name=_string(data, "name"),
            role=_string(data, "role"),
            note=_string(data, "note", allow_empty=True),
        )


@dataclass(frozen=True)
class SpeakerMap:
    source_id: str
    transcript_version_id: str
    local_speaker_id: str
    person_id: str
    confirmed_by_user: bool

    def __post_init__(self) -> None:
        for name, value in (
            ("source_id", self.source_id),
            ("transcript_version_id", self.transcript_version_id),
            ("local_speaker_id", self.local_speaker_id),
            ("person_id", self.person_id),
        ):
            validate_safe_id(value, name)
        if self.confirmed_by_user is not True:
            raise ProjectError("speaker map must be confirmed by the user")

    @property
    def identity_key(self) -> tuple[str, str, str]:
        return (self.source_id, self.transcript_version_id, self.local_speaker_id)

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "transcript_version_id": self.transcript_version_id,
            "local_speaker_id": self.local_speaker_id,
            "person_id": self.person_id,
            "confirmed_by_user": self.confirmed_by_user,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SpeakerMap:
        confirmed = data.get("confirmed_by_user")
        if not isinstance(confirmed, bool):
            raise ProjectError("confirmed_by_user must be a boolean")
        return cls(
            source_id=_string(data, "source_id"),
            transcript_version_id=_string(data, "transcript_version_id"),
            local_speaker_id=_string(data, "local_speaker_id"),
            person_id=_string(data, "person_id"),
            confirmed_by_user=confirmed,
        )


def _string(data: dict[str, Any], key: str, *, allow_empty: bool = False) -> str:
    value = data.get(key)
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ProjectError(f"{key} must be a string")
    return value
