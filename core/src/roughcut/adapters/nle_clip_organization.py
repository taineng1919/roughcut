"""Remove redundant export cuts without changing canonical or serialized playback."""

from __future__ import annotations

from dataclasses import fields, replace

from roughcut.adapters.nle_time import fcp7_frame_range, project_fcpxml_timing
from roughcut.domain.nle_handoff import (
    NleHandoffClip,
    NleHandoffError,
    NleHandoffTimeline,
)

NLE_CLIP_ORGANIZATION_POLICY = "contiguous_source_mapping_v1"

# Actual serialized output in/out and file-local source start, all in ticks.
PlaybackRange = tuple[int, int, int]


def coalesce_nle_clips(
    timeline: NleHandoffTimeline, *, route: str
) -> tuple[NleHandoffTimeline, dict[str, tuple[str, ...]]]:
    """Organize writer input only; retain every original identity in export notes.

    A boundary can disappear only together on every logical track. Requiring
    full auxiliary coverage on both sides deliberately leaves partial/missing
    mappings alone. Validate the original clips before considering any merge,
    so organization cannot make an unsupported original export appear valid.
    """

    playback = _playback_ranges(timeline, route)
    members: dict[str, tuple[str, ...]] = {
        clip.logical_clip_id: (clip.logical_clip_id,)
        for track in timeline.tracks
        for clip in track.clips
    }
    index = 0
    while index + 1 < len(timeline.main_track.clips):
        left, right = timeline.main_track.clips[index : index + 2]
        pairs: list[tuple[NleHandoffClip, NleHandoffClip]] = []
        for track in timeline.tracks:
            before = [
                clip for clip in track.clips
                if (clip.timeline_in_ticks, clip.timeline_out_ticks)
                == (left.timeline_in_ticks, left.timeline_out_ticks)
            ]
            after = [
                clip for clip in track.clips
                if (clip.timeline_in_ticks, clip.timeline_out_ticks)
                == (right.timeline_in_ticks, right.timeline_out_ticks)
            ]
            if len(before) != 1 or len(after) != 1 or not _compatible(before[0], after[0]):
                break
            a, b = before[0], after[0]
            a_in, a_out, a_source = playback[a.logical_clip_id]
            b_in, _b_out, b_source = playback[b.logical_clip_id]
            if a_out != b_in or a_source + a_out - a_in != b_source:
                break
            pairs.append((a, b))
        if len(pairs) != len(timeline.tracks):
            index += 1
            continue

        merged = {
            a.logical_clip_id: replace(
                a, source_out_ticks=b.source_out_ticks, timeline_out_ticks=b.timeline_out_ticks
            )
            for a, b in pairs
        }
        removed = {b.logical_clip_id for _a, b in pairs}
        candidate = replace(
            timeline,
            tracks=tuple(
                replace(track, items=tuple(
                    merged.get(item.logical_clip_id, item)
                    if isinstance(item, NleHandoffClip) else item
                    for item in track.items
                    if not isinstance(item, NleHandoffClip) or item.logical_clip_id not in removed
                ))
                for track in timeline.tracks
            ),
        )
        try:
            candidate_playback = _playback_ranges(candidate, route)
        except NleHandoffError:
            # Reanchoring connected children may be unrepresentable even when
            # the original clips were supported. Keep that original boundary.
            index += 1
            continue
        expected = dict(playback)
        for a, b in pairs:
            a_in, _a_out, a_source = expected[a.logical_clip_id]
            _b_in, b_out, _b_source = expected.pop(b.logical_clip_id)
            expected[a.logical_clip_id] = (a_in, b_out, a_source)
        if candidate_playback != expected:
            index += 1
            continue
        for a, b in pairs:
            members[a.logical_clip_id] += members.pop(b.logical_clip_id)
        timeline, playback = candidate, candidate_playback
    return timeline, members


def _compatible(left: NleHandoffClip, right: NleHandoffClip) -> bool:
    if (
        left.source_out_ticks != right.source_in_ticks
        or left.timeline_out_ticks != right.timeline_in_ticks
    ):
        return False
    # Per-clip identities differ by construction. All playback/format/track
    # properties must match; future dataclass properties also fail closed here.
    identities_and_ranges = {
        "logical_clip_id", "av_link_id", "source_in_ticks", "source_out_ticks",
        "timeline_in_ticks", "timeline_out_ticks",
    }
    return all(
        getattr(left, field.name) == getattr(right, field.name)
        for field in fields(left) if field.name not in identities_and_ranges
    )


def _playback_ranges(timeline: NleHandoffTimeline, route: str) -> dict[str, PlaybackRange]:
    result: dict[str, PlaybackRange] = {}
    if route == "fcpxml":
        projection = project_fcpxml_timing(timeline)
        timings = {timing.logical_clip_id: timing for timing in projection.clip_timings}
        for track in timeline.tracks:
            for clip in track.clips:
                timing = timings[clip.logical_clip_id]
                start = timing.timeline_in_ticks
                if track.role == "auxiliary":
                    parent = next(
                        main for main in timeline.main_track.clips
                        if main.timeline_in_ticks <= clip.timeline_in_ticks
                        and clip.timeline_out_ticks <= main.timeline_out_ticks
                    )
                    parent_timing = timings[parent.logical_clip_id]
                    # Connected offsets are parent-source coordinates. Compare
                    # the actual XML placement, including its rounding shift.
                    start = (
                        parent_timing.timeline_in_ticks
                        + timing.offset_ticks - parent_timing.start_ticks
                    )
                result[clip.logical_clip_id] = (
                    start, start + timing.duration_ticks, timing.start_ticks
                )
        return result
    if route != "fcp7_xml":
        raise ValueError(f"unknown NLE organization route: {route}")
    for track in timeline.tracks:
        for clip in track.clips:
            if "video" in clip.media_types and clip.source_is_vfr:
                raise NleHandoffError(
                    "nle_export_rate_unsupported",
                    "Roughcut FCP7 XML variable-frame-rate video Source is unsupported",
                )
            frames = fcp7_frame_range(
                source_in_ticks=clip.source_in_ticks,
                source_out_ticks=clip.source_out_ticks,
                timeline_in_ticks=clip.timeline_in_ticks,
                timeline_out_ticks=clip.timeline_out_ticks,
                source_duration_ticks=clip.source_duration_ticks,
                sequence_rate=timeline.frame_rate,
                source_rate=clip.source_nominal_frame_rate or timeline.frame_rate,
            )
            result[clip.logical_clip_id] = (
                frames.timeline_in * timeline.frame_rate.ticks_per_frame,
                frames.timeline_out * timeline.frame_rate.ticks_per_frame,
                clip.source_in_ticks,
            )
    return result
