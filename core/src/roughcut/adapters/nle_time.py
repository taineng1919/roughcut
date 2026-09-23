"""Exact time conversion helpers shared by the two NLE writers."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from itertools import pairwise

from roughcut.domain.nle_handoff import (
    NleHandoffClip,
    NleHandoffError,
    NleHandoffTimeline,
)
from roughcut.domain.time import TICKS_PER_SECOND, RationalRate


@dataclass(frozen=True)
class Fcp7Rate:
    timebase: int
    ntsc: bool


@dataclass(frozen=True)
class Fcp7FrameRange:
    """xmeml frame values split between source and sequence coordinates."""

    source_in: int
    source_out: int
    timeline_in: int
    timeline_out: int
    mixed_rates_offset: int | None


@dataclass(frozen=True)
class FcpxmlClipTiming:
    """One FCPXML target-grid timing tuple for a handoff clip.

    ``source_start_delta_ticks`` is the serialized source start minus the
    projection's desired source start. For connected children, the desired
    start is the Project-grid offset plus the canonical Alignment delta.
    """

    track_id: str
    logical_clip_id: str
    timeline_in_ticks: int
    timeline_out_ticks: int
    offset_ticks: int
    start_ticks: int
    duration_ticks: int
    source_start_delta_ticks: int
    canonical_alignment_delta_ticks: int | None = None
    serialized_alignment_delta_ticks: int | None = None
    sync_error_ticks: int | None = None


@dataclass(frozen=True)
class FcpxmlTimingProjection:
    """Deterministic FCPXML timing projection, separate from Core truth."""

    sequence_duration_ticks: int
    main_boundaries: tuple[int, ...]
    clip_timings: tuple[FcpxmlClipTiming, ...]
    max_source_start_quantization_error_ticks: int
    max_sync_error_ticks: int

    def for_clip(self, clip: NleHandoffClip) -> FcpxmlClipTiming:
        matches = tuple(
            timing
            for timing in self.clip_timings
            if timing.track_id == clip.track_id and timing.logical_clip_id == clip.logical_clip_id
        )
        if len(matches) != 1:
            raise _integrity_error(
                f"timing projection does not uniquely contain clip {clip.logical_clip_id!r}"
            )
        return matches[0]


def _error(message: str) -> NleHandoffError:
    return NleHandoffError("nle_export_rate_unsupported", f"Roughcut NLE time {message}")


def rational_seconds(ticks: int) -> Fraction:
    if isinstance(ticks, bool) or not isinstance(ticks, int) or ticks < 0:
        raise ValueError("ticks must be a non-negative integer")
    return Fraction(ticks, TICKS_PER_SECOND)


def fcpxml_time(ticks: int) -> str:
    value = rational_seconds(ticks)
    if value.denominator == 1:
        return f"{value.numerator}s"
    return f"{value.numerator}/{value.denominator}s"


def frame_duration(rate: RationalRate) -> str:
    value = Fraction(rate.denominator, rate.numerator)
    if value.denominator == 1:
        return f"{value.numerator}s"
    return f"{value.numerator}/{value.denominator}s"


def fcp7_rate(rate: RationalRate) -> Fcp7Rate:
    normalized = Fraction(rate.numerator, rate.denominator)
    if normalized.denominator == 1:
        return Fcp7Rate(normalized.numerator, False)
    if normalized.denominator == 1001 and normalized.numerator % 1000 == 0:
        return Fcp7Rate(normalized.numerator // 1000, True)
    raise _error(f"cannot represent frame rate {rate.numerator}/{rate.denominator}")


def _round_half_up(numerator: int, denominator: int) -> int:
    if numerator < 0 or denominator <= 0:
        raise ValueError("rounding inputs must be non-negative")
    quotient, remainder = divmod(numerator, denominator)
    return quotient + int(remainder * 2 >= denominator)


def ticks_to_frame_boundary(ticks: int, rate: RationalRate) -> int:
    try:
        ticks_per_frame = rate.ticks_per_frame
    except ValueError as error:
        raise _error(
            f"frame rate {rate.numerator}/{rate.denominator} is not tick-representable"
        ) from error
    return _round_half_up(ticks, ticks_per_frame)


def frame_boundary_ticks(ticks: int, rate: RationalRate) -> int:
    """Round absolute ticks to a frame boundary and return ticks again."""

    if isinstance(ticks, bool) or not isinstance(ticks, int) or ticks < 0:
        raise _error("frame-boundary input must be a non-negative integer")
    try:
        ticks_per_frame = rate.ticks_per_frame
    except ValueError as error:
        raise _error(
            f"frame rate {rate.numerator}/{rate.denominator} is not tick-representable"
        ) from error
    return ticks_to_frame_boundary(ticks, rate) * ticks_per_frame


def ticks_to_exact_frame(ticks: int, rate: RationalRate, *, field: str) -> int:
    """Convert a source boundary only when it names an exact source frame."""

    if isinstance(ticks, bool) or not isinstance(ticks, int) or ticks < 0:
        raise _error(f"{field} must be a non-negative integer tick value")
    try:
        ticks_per_frame = rate.ticks_per_frame
    except ValueError as error:
        raise _error(f"{field} uses a frame rate that is not tick-representable") from error
    frame, remainder = divmod(ticks, ticks_per_frame)
    if remainder:
        raise _error(f"{field} is not aligned to an exact source frame")
    return frame


def _integrity_error(message: str) -> NleHandoffError:
    return NleHandoffError("nle_export_integrity_error", f"Roughcut NLE time {message}")


def _source_rate_for_fcpxml(clip: NleHandoffClip) -> RationalRate | None:
    if "video" not in clip.media_types:
        return None
    if clip.source_is_vfr:
        raise _error("variable-frame-rate video Source is unsupported")
    if clip.source_nominal_frame_rate is None:
        raise _error("video Source nominal frame rate is unavailable")
    try:
        source_ticks_per_frame = clip.source_nominal_frame_rate.ticks_per_frame
    except ValueError as error:
        raise _error("video Source frame rate is not tick-representable") from error
    if source_ticks_per_frame <= 0:
        raise _error("video Source frame rate is not tick-representable")
    return clip.source_nominal_frame_rate


def _serialize_source_start(
    clip: NleHandoffClip,
    *,
    desired_start_ticks: int | None = None,
) -> tuple[int, int, RationalRate | None]:
    desired = clip.source_in_ticks if desired_start_ticks is None else desired_start_ticks
    if isinstance(desired, bool) or not isinstance(desired, int) or desired < 0:
        raise _error("desired serialized source start must be a non-negative integer tick value")
    source_rate = _source_rate_for_fcpxml(clip)
    if source_rate is None:
        return desired, 0, None
    serialized = frame_boundary_ticks(desired, source_rate)
    return serialized, serialized - desired, source_rate


def _validate_serialized_source_range(
    clip: NleHandoffClip,
    *,
    source_start_ticks: int,
    duration_ticks: int,
    source_rate: RationalRate | None,
) -> None:
    if source_start_ticks < 0:
        raise _integrity_error("serialized source start is negative")
    source_end_ticks = source_start_ticks + duration_ticks
    if source_end_ticks > clip.source_duration_ticks:
        raise _integrity_error(
            f"serialized source range exceeds original Source {clip.source_id!r} duration"
        )
    if source_rate is not None:
        ticks_per_frame = source_rate.ticks_per_frame
        if source_start_ticks % ticks_per_frame or source_end_ticks % ticks_per_frame:
            raise _error("serialized video source range is not aligned to its source frame grid")


def _make_fcpxml_clip_timing(
    clip: NleHandoffClip,
    *,
    timeline_in_ticks: int,
    timeline_out_ticks: int,
    offset_ticks: int,
    serialized_source: tuple[int, int, RationalRate | None] | None = None,
    canonical_alignment_delta_ticks: int | None = None,
    serialized_alignment_delta_ticks: int | None = None,
    sync_error_ticks: int | None = None,
) -> FcpxmlClipTiming:
    duration_ticks = timeline_out_ticks - timeline_in_ticks
    if duration_ticks <= 0:
        raise _error("a timeline clip quantizes to zero frames")
    source_start_ticks, source_delta_ticks, source_rate = (
        _serialize_source_start(clip) if serialized_source is None else serialized_source
    )
    _validate_serialized_source_range(
        clip,
        source_start_ticks=source_start_ticks,
        duration_ticks=duration_ticks,
        source_rate=source_rate,
    )
    return FcpxmlClipTiming(
        track_id=clip.track_id,
        logical_clip_id=clip.logical_clip_id,
        timeline_in_ticks=timeline_in_ticks,
        timeline_out_ticks=timeline_out_ticks,
        offset_ticks=offset_ticks,
        start_ticks=source_start_ticks,
        duration_ticks=duration_ticks,
        source_start_delta_ticks=source_delta_ticks,
        canonical_alignment_delta_ticks=canonical_alignment_delta_ticks,
        serialized_alignment_delta_ticks=serialized_alignment_delta_ticks,
        sync_error_ticks=sync_error_ticks,
    )


def project_fcpxml_timing(timeline: NleHandoffTimeline) -> FcpxmlTimingProjection:
    """Project canonical timing onto FCPXML's legal target frame grids.

    Main timeline boundaries are quantized once as an absolute vector. Every
    duration is then a difference of projected boundaries. Main source starts
    are quantized on their SourceAsset grid; connected children first project
    their offset onto the Project grid, then quantize ``offset + canonical
    alignment delta`` on the child SourceAsset grid.
    """

    main_clips = timeline.main_track.clips
    if not main_clips:
        raise _integrity_error("the main track has no clips")
    canonical_boundaries = [0]
    previous = 0
    for clip in main_clips:
        if clip.timeline_in_ticks != previous:
            raise _integrity_error("main timeline boundaries are not contiguous")
        canonical_boundaries.append(clip.timeline_out_ticks)
        previous = clip.timeline_out_ticks
    if previous != timeline.duration_ticks:
        raise _integrity_error("main timeline does not end at the sequence duration")

    projected_boundaries = tuple(
        frame_boundary_ticks(boundary, timeline.frame_rate) for boundary in canonical_boundaries
    )
    if projected_boundaries[0] != 0:
        raise _integrity_error("the projected sequence does not start at zero")
    if any(end <= start for start, end in pairwise(projected_boundaries)):
        raise _error("a timeline clip quantizes to zero frames")
    sequence_duration_ticks = projected_boundaries[-1]
    if sequence_duration_ticks <= 0:
        raise _error("the projected sequence duration is not positive")

    boundary_cache = dict(zip(canonical_boundaries, projected_boundaries))

    def projected_boundary(ticks: int) -> int:
        value = boundary_cache.get(ticks)
        if value is None:
            value = frame_boundary_ticks(ticks, timeline.frame_rate)
            boundary_cache[ticks] = value
        return value

    timings: list[FcpxmlClipTiming] = []
    main_timing_by_key: dict[tuple[str, str], FcpxmlClipTiming] = {}
    for index, clip in enumerate(main_clips):
        timing = _make_fcpxml_clip_timing(
            clip,
            timeline_in_ticks=projected_boundaries[index],
            timeline_out_ticks=projected_boundaries[index + 1],
            offset_ticks=projected_boundaries[index],
        )
        timings.append(timing)
        main_timing_by_key[(clip.track_id, clip.logical_clip_id)] = timing

    for track in timeline.auxiliary_tracks:
        previous_projected_end = 0
        for item in track.items:
            item_start = projected_boundary(item.timeline_in_ticks)
            item_end = projected_boundary(item.timeline_out_ticks)
            if item_start != previous_projected_end:
                raise _integrity_error(
                    f"auxiliary track {track.track_id!r} has a gap or overlap after target-grid projection"
                )
            if item_end < item_start:
                raise _error("an auxiliary timeline item has an inverted target range")
            if isinstance(item, NleHandoffClip) and item_end == item_start:
                raise _error("a timeline clip quantizes to zero frames")
            previous_projected_end = item_end
            if not isinstance(item, NleHandoffClip):
                continue

            parents = [
                parent
                for parent in main_clips
                if parent.timeline_in_ticks <= item.timeline_in_ticks
                and item.timeline_out_ticks <= parent.timeline_out_ticks
            ]
            if len(parents) != 1:
                raise _integrity_error(
                    f"auxiliary clip {item.logical_clip_id!r} has no unique main parent"
                )
            parent = parents[0]
            parent_timing = main_timing_by_key[(parent.track_id, parent.logical_clip_id)]
            if (
                item_start < parent_timing.timeline_in_ticks
                or item_end > parent_timing.timeline_out_ticks
            ):
                raise _integrity_error(
                    f"auxiliary clip {item.logical_clip_id!r} escapes its projected main parent"
                )
            # The parent-local anchor is a representation coordinate, not a
            # second alignment truth. Final Cut requires this connected offset
            # on the Project edit grid even when the parent asset is source-rate.
            raw_offset_ticks = (
                parent_timing.start_ticks + item_start - parent_timing.timeline_in_ticks
            )
            offset_ticks = frame_boundary_ticks(raw_offset_ticks, timeline.frame_rate)
            ticks_to_exact_frame(
                offset_ticks,
                timeline.frame_rate,
                field="connected child offset",
            )

            canonical_main_at_child = (
                parent.source_in_ticks + item.timeline_in_ticks - parent.timeline_in_ticks
            )
            canonical_delta = item.source_in_ticks - canonical_main_at_child
            # Keep the exact Alignment delta while solving the two serialized
            # coordinates together; rounding offset alone would move the child.
            desired_child_start = offset_ticks + canonical_delta
            serialized_source = _serialize_source_start(
                item,
                desired_start_ticks=desired_child_start,
            )
            serialized_delta = serialized_source[0] - offset_ticks
            sync_error = serialized_delta - canonical_delta
            source_rate = serialized_source[2]
            if source_rate is not None and abs(sync_error) * 2 > source_rate.ticks_per_frame:
                raise _error("connected child alignment error exceeds half a source frame")
            timing = _make_fcpxml_clip_timing(
                item,
                timeline_in_ticks=item_start,
                timeline_out_ticks=item_end,
                offset_ticks=offset_ticks,
                serialized_source=serialized_source,
                canonical_alignment_delta_ticks=canonical_delta,
                serialized_alignment_delta_ticks=serialized_delta,
                sync_error_ticks=sync_error,
            )
            timings.append(timing)

        if previous_projected_end != sequence_duration_ticks:
            raise _integrity_error(
                f"auxiliary track {track.track_id!r} does not cover the projected sequence"
            )

    return FcpxmlTimingProjection(
        sequence_duration_ticks=sequence_duration_ticks,
        main_boundaries=projected_boundaries,
        clip_timings=tuple(timings),
        max_source_start_quantization_error_ticks=max(
            (abs(timing.source_start_delta_ticks) for timing in timings),
            default=0,
        ),
        max_sync_error_ticks=max(
            (
                abs(timing.sync_error_ticks)
                for timing in timings
                if timing.sync_error_ticks is not None
            ),
            default=0,
        ),
    )


def fcp7_frame_range(
    *,
    source_in_ticks: int,
    source_out_ticks: int,
    timeline_in_ticks: int,
    timeline_out_ticks: int,
    source_duration_ticks: int,
    sequence_rate: RationalRate,
    source_rate: RationalRate,
) -> Fcp7FrameRange:
    """Map exact source boundaries to xmeml's sequence-rate in/out fields.

    Apple documents ``clipitem`` in/out points in sequence-rate units and uses
    ``mixedratesoffset`` to preserve the source-frame residual for mixed-rate
    footage.  Timeline start/end and clipitem duration stay on the sequence
    grid; the associated file's duration and rate stay on the source grid.
    """

    timeline_start = ticks_to_frame_boundary(timeline_in_ticks, sequence_rate)
    timeline_end = ticks_to_frame_boundary(timeline_out_ticks, sequence_rate)
    if timeline_end <= timeline_start:
        raise _error("a timeline clip quantizes to zero frames")

    source_start = ticks_to_exact_frame(
        source_in_ticks,
        source_rate,
        field="source in point",
    )
    source_end = ticks_to_exact_frame(
        source_out_ticks,
        source_rate,
        field="source out point",
    )
    source_duration = ticks_to_exact_frame(
        source_duration_ticks,
        source_rate,
        field="source duration",
    )
    if source_end <= source_start:
        raise _error("a source clip has a non-positive frame range")
    if source_end > source_duration:
        raise _error("a source clip exceeds the source duration")

    timeline_duration = timeline_end - timeline_start
    expected_timeline_duration = Fraction(
        (source_end - source_start) * sequence_rate.numerator * source_rate.denominator,
        source_rate.numerator * sequence_rate.denominator,
    )
    if (
        expected_timeline_duration.denominator != 1
        or expected_timeline_duration.numerator != timeline_duration
    ):
        raise _error("mixed-rate source duration is not representable on the sequence grid")

    source_frames_per_sequence_frame = Fraction(
        source_rate.numerator * sequence_rate.denominator,
        source_rate.denominator * sequence_rate.numerator,
    )
    sequence_source_position = Fraction(source_start, 1) / source_frames_per_sequence_frame
    sequence_source_start = (
        sequence_source_position.numerator // sequence_source_position.denominator
    )
    mixed_residual = (
        Fraction(source_start, 1) - sequence_source_start * source_frames_per_sequence_frame
    )
    if mixed_residual.denominator != 1:
        raise _error("mixed-rate source in point needs a fractional mixedratesoffset")
    sequence_source_end = sequence_source_start + timeline_duration
    if sequence_source_end * source_frames_per_sequence_frame + mixed_residual != source_end:
        raise _error("mixed-rate source range cannot preserve both source boundaries")

    rates_match = Fraction(source_rate.numerator, source_rate.denominator) == Fraction(
        sequence_rate.numerator,
        sequence_rate.denominator,
    )
    return Fcp7FrameRange(
        source_in=sequence_source_start,
        source_out=sequence_source_end,
        timeline_in=timeline_start,
        timeline_out=timeline_end,
        mixed_rates_offset=None if rates_match else mixed_residual.numerator,
    )
