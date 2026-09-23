"""Minimal edit brief model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from roughcut.domain.project import ProjectError


@dataclass(frozen=True)
class EditBrief:
    brief_id: str
    theme: str
    target_duration_ticks: int
    focus: tuple[str, ...]
    allow_reorder: bool
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not self.brief_id.strip():
            raise ProjectError("brief_id must be a non-empty string")
        if not self.theme.strip():
            raise ProjectError("brief theme is required")
        if (
            isinstance(self.target_duration_ticks, bool)
            or not isinstance(self.target_duration_ticks, int)
            or self.target_duration_ticks <= 0
        ):
            raise ProjectError("brief target duration must be a positive integer")
        if not isinstance(self.focus, tuple) or not self.focus:
            raise ProjectError("brief focus must be a non-empty tuple")
        if any(not isinstance(item, str) or not item.strip() for item in self.focus):
            raise ProjectError("brief focus entries must be non-empty strings")
        if not isinstance(self.allow_reorder, bool):
            raise ProjectError("brief allow_reorder must be a boolean")
        if self.schema_version != 1:
            raise ProjectError("unsupported brief schema version")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "brief_id": self.brief_id,
            "theme": self.theme,
            "target_duration_ticks": self.target_duration_ticks,
            "focus": list(self.focus),
            "allow_reorder": self.allow_reorder,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EditBrief:
        focus = data.get("focus")
        if not isinstance(focus, list) or not all(isinstance(item, str) for item in focus):
            raise ProjectError("brief focus must be a list of strings")
        return cls(
            schema_version=_integer(data, "schema_version"),
            brief_id=_string(data, "brief_id"),
            theme=_string(data, "theme"),
            target_duration_ticks=_integer(data, "target_duration_ticks"),
            focus=tuple(focus),
            allow_reorder=_boolean(data, "allow_reorder"),
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
