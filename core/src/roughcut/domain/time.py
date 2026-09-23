"""Integer media-time primitives shared by all media adapters."""

from __future__ import annotations

from dataclasses import dataclass

TICKS_PER_SECOND = 120_000
MIN_TICKS = -(2**63)
MAX_TICKS = 2**63 - 1


@dataclass(frozen=True)
class MediaTime:
    ticks: int

    def __post_init__(self) -> None:
        if isinstance(self.ticks, bool) or not isinstance(self.ticks, int):
            raise TypeError("ticks must be an integer")
        if not MIN_TICKS <= self.ticks <= MAX_TICKS:
            raise ValueError("ticks must fit in a signed 64-bit integer")

    @classmethod
    def from_milliseconds(cls, milliseconds: int) -> MediaTime:
        return cls(milliseconds * 120)


@dataclass(frozen=True)
class RationalRate:
    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        if self.numerator <= 0 or self.denominator <= 0:
            raise ValueError("frame-rate numerator and denominator must be positive")

    @property
    def ticks_per_frame(self) -> int:
        ticks, remainder = divmod(TICKS_PER_SECOND * self.denominator, self.numerator)
        if remainder:
            raise ValueError("frame duration is not representable in whole ticks")
        return ticks

    def frames_to_ticks(self, frames: int) -> MediaTime:
        return MediaTime(frames * self.ticks_per_frame)


@dataclass(frozen=True)
class SourceRange:
    start: MediaTime
    end: MediaTime

    def __post_init__(self) -> None:
        if self.end.ticks <= self.start.ticks:
            raise ValueError("source ranges use [start, end) with end after start")

    @property
    def duration(self) -> MediaTime:
        return MediaTime(self.end.ticks - self.start.ticks)
