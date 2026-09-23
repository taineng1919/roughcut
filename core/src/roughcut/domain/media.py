"""Media probe values that keep container and normalized source time separate."""

from __future__ import annotations

from dataclasses import dataclass

from roughcut.domain.time import MediaTime


@dataclass(frozen=True)
class SourceProbe:
    container_start: MediaTime
    first_content_time: MediaTime
    content_duration: MediaTime

    def __post_init__(self) -> None:
        if self.content_duration.ticks <= 0:
            raise ValueError("content duration must be positive")

    def normalize_container_time(self, container_time: MediaTime) -> MediaTime:
        normalized = container_time.ticks - self.first_content_time.ticks
        if normalized < 0:
            raise ValueError("container time precedes the first effective picture")
        return MediaTime(normalized)

    def container_time_for_source(self, source_time: MediaTime) -> MediaTime:
        if source_time.ticks < 0:
            raise ValueError("normalized source time cannot be negative")
        return MediaTime(self.first_content_time.ticks + source_time.ticks)
