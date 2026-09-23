"""Pure virtual timeline mapping derived from frozen edit snapshots."""

from __future__ import annotations

from dataclasses import dataclass

from roughcut.domain.edit import (
    EditDecision,
    EditProposal,
    MultiSourceEditDecision,
    MultiSourceEditProposal,
)
from roughcut.domain.project import ProjectError


@dataclass(frozen=True)
class TimelineSpan:
    clip_id: str
    source_id: str
    source_in_ticks: int
    source_out_ticks: int
    output_in_ticks: int
    output_out_ticks: int

    def __post_init__(self) -> None:
        if self.source_out_ticks <= self.source_in_ticks:
            raise ProjectError("timeline source range must be non-empty")
        if self.output_out_ticks <= self.output_in_ticks:
            raise ProjectError("timeline output range must be non-empty")
        if self.source_out_ticks - self.source_in_ticks != self.output_out_ticks - self.output_in_ticks:
            raise ProjectError("timeline source and output durations must match")

    def to_dict(self) -> dict[str, object]:
        return {
            "clip_id": self.clip_id,
            "source_id": self.source_id,
            "source_in_ticks": self.source_in_ticks,
            "source_out_ticks": self.source_out_ticks,
            "output_in_ticks": self.output_in_ticks,
            "output_out_ticks": self.output_out_ticks,
        }


@dataclass(frozen=True)
class TimeMapping:
    clip_id: str
    source_id: str
    output_ticks: int
    source_ticks: int

    def to_dict(self) -> dict[str, object]:
        return {
            "clip_id": self.clip_id,
            "source_id": self.source_id,
            "output_ticks": self.output_ticks,
            "source_ticks": self.source_ticks,
        }


@dataclass(frozen=True)
class VirtualTimeline:
    spans: tuple[TimelineSpan, ...]

    def __post_init__(self) -> None:
        if not self.spans:
            raise ProjectError("timeline requires at least one clip")
        expected_output_in = 0
        seen: set[str] = set()
        for span in self.spans:
            if span.clip_id in seen:
                raise ProjectError("timeline clip IDs must be unique")
            seen.add(span.clip_id)
            if span.output_in_ticks != expected_output_in:
                raise ProjectError("timeline output ranges must be contiguous")
            expected_output_in = span.output_out_ticks

    @classmethod
    def from_proposal(
        cls, proposal: EditProposal | MultiSourceEditProposal
    ) -> VirtualTimeline:
        output_in = 0
        spans: list[TimelineSpan] = []
        for clip in proposal.clips:
            output_out = output_in + clip.duration_ticks
            spans.append(
                TimelineSpan(
                    clip_id=clip.clip_id,
                    source_id=clip.source_id,
                    source_in_ticks=clip.source_in_ticks,
                    source_out_ticks=clip.source_out_ticks,
                    output_in_ticks=output_in,
                    output_out_ticks=output_out,
                )
            )
            output_in = output_out
        timeline = cls(tuple(spans))
        if timeline.total_duration_ticks != proposal.total_duration_ticks:
            raise ProjectError("timeline total duration does not match proposal")
        return timeline

    @classmethod
    def from_decision(
        cls, decision: EditDecision | MultiSourceEditDecision
    ) -> VirtualTimeline:
        return cls.from_proposal(decision.proposal_snapshot)

    @property
    def total_duration_ticks(self) -> int:
        return self.spans[-1].output_out_ticks

    def map_output_time(self, output_ticks: int) -> TimeMapping:
        if isinstance(output_ticks, bool) or not isinstance(output_ticks, int):
            raise ProjectError("output time must be an integer")
        for span in self.spans:
            if span.output_in_ticks <= output_ticks < span.output_out_ticks:
                return TimeMapping(
                    clip_id=span.clip_id,
                    source_id=span.source_id,
                    output_ticks=output_ticks,
                    source_ticks=span.source_in_ticks + output_ticks - span.output_in_ticks,
                )
        raise ProjectError("output time is outside the timeline")

    def map_source_time(self, clip_id: str, source_ticks: int) -> TimeMapping:
        for span in self.spans:
            if span.clip_id == clip_id:
                if not span.source_in_ticks <= source_ticks < span.source_out_ticks:
                    raise ProjectError("source time is outside the clip")
                return TimeMapping(
                    clip_id=span.clip_id,
                    source_id=span.source_id,
                    output_ticks=span.output_in_ticks + source_ticks - span.source_in_ticks,
                    source_ticks=source_ticks,
                )
        raise ProjectError("unknown clip in timeline")

    def to_dict(self) -> dict[str, object]:
        return {
            "total_duration_ticks": self.total_duration_ticks,
            "spans": [span.to_dict() for span in self.spans],
        }
