"""Pure FCPXML 1.14 serialization for a validated NLE handoff timeline."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterable
from typing import cast

from roughcut.adapters.nle_time import (
    FcpxmlClipTiming,
    fcpxml_time,
    frame_duration,
    project_fcpxml_timing,
)
from roughcut.domain.nle_handoff import (
    FCPXML_EXPORT_PROFILE,
    NleHandoffClip,
    NleHandoffError,
    NleHandoffTimeline,
)
from roughcut.domain.time import RationalRate

FCPXML_VERSION = "1.14"
FCPXML_AUDIO_ONLY_FORMAT_ID = "format-audio-only"


def write_fcpxml(timeline: NleHandoffTimeline) -> bytes:
    """Return deterministic FCPXML 1.14 bytes without reading project state."""

    if FCPXML_EXPORT_PROFILE != "roughcut_fcpxml_1_14":
        raise ValueError("FCPXML export profile identity changed")
    sources = _source_catalog(timeline)
    timing = project_fcpxml_timing(timeline)
    project_format_id = _project_format_id(timeline)
    source_formats = _source_format_catalog(sources)
    root = ET.Element("fcpxml", {"version": FCPXML_VERSION})
    resources = ET.SubElement(root, "resources")
    ET.SubElement(
        resources,
        "format",
        {
            "id": project_format_id,
            "name": _format_name(timeline),
            "frameDuration": frame_duration(timeline.frame_rate),
            "width": str(timeline.video_width),
            "height": str(timeline.video_height),
            "colorSpace": "1-1-1 (Rec. 709)",
        },
    )
    for format_key in sorted(source_formats):
        source_width, source_height, numerator, denominator = format_key
        source_rate = RationalRate(numerator, denominator)
        ET.SubElement(
            resources,
            "format",
            {
                "id": source_formats[format_key],
                "name": _source_format_name(source_width, source_height, source_rate),
                "frameDuration": frame_duration(source_rate),
                "width": str(source_width),
                "height": str(source_height),
            },
        )
    if any(
        "audio" in clip.media_types and "video" not in clip.media_types for clip in sources.values()
    ):
        ET.SubElement(
            resources,
            "format",
            {"id": FCPXML_AUDIO_ONLY_FORMAT_ID, "name": "FFFrameRateUndefined"},
        )
    for source_id in sorted(sources):
        clip = sources[source_id]
        media_types = set(clip.media_types)
        attributes = {
            "id": f"asset-{source_id}",
            "name": clip.source_display_name,
            "uid": source_id,
            "start": "0s",
            "duration": fcpxml_time(clip.source_duration_ticks),
            "hasVideo": "1" if "video" in media_types else "0",
            "hasAudio": "1" if "audio" in media_types else "0",
        }
        if "video" in media_types:
            attributes["format"] = source_formats[_source_format_key(clip)]
        elif "audio" in media_types:
            attributes["format"] = FCPXML_AUDIO_ONLY_FORMAT_ID
        if "audio" in media_types and clip.source_audio_sample_rate is not None:
            attributes["audioRate"] = str(clip.source_audio_sample_rate)
        asset = ET.SubElement(resources, "asset", attributes)
        ET.SubElement(
            asset,
            "media-rep",
            {
                "kind": "original-media",
                "src": clip.source_locator.as_uri(),
            },
        )

    library = ET.SubElement(root, "library")
    event = ET.SubElement(library, "event", {"name": "Roughcut NLE handoff"})
    project = ET.SubElement(event, "project", {"name": "Roughcut editable handoff"})
    sequence = ET.SubElement(
        project,
        "sequence",
        {
            "format": project_format_id,
            "duration": fcpxml_time(timing.sequence_duration_ticks),
            "tcStart": "0s",
            "tcFormat": "NDF",
        },
    )
    spine = ET.SubElement(sequence, "spine")
    main_clips = timeline.main_track.clips
    for main_clip in main_clips:
        main_element = _asset_clip(
            main_clip,
            timing=timing.for_clip(main_clip),
            format_id=_clip_format_id(main_clip, source_formats),
            include_lane=False,
        )
        spine.append(main_element)
        for lane, track in enumerate(timeline.auxiliary_tracks, start=1):
            for auxiliary_clip in track.clips:
                if _contained_by(auxiliary_clip, main_clip):
                    main_element.append(
                        _asset_clip(
                            auxiliary_clip,
                            timing=timing.for_clip(auxiliary_clip),
                            format_id=_clip_format_id(auxiliary_clip, source_formats),
                            include_lane=True,
                            lane=lane,
                        )
                    )

    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="utf-8", short_empty_elements=True)
    return (
        b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n' + cast(bytes, body) + b"\n"
    )


def _asset_clip(
    clip: NleHandoffClip,
    *,
    timing: FcpxmlClipTiming,
    format_id: str,
    include_lane: bool,
    lane: int | None = None,
) -> ET.Element:
    attributes = {
        "name": f"{clip.logical_clip_id} | {clip.camera_id} | {clip.source_id}",
        "ref": f"asset-{clip.source_id}",
        "format": format_id,
        "offset": fcpxml_time(timing.offset_ticks),
        "start": fcpxml_time(timing.start_ticks),
        "duration": fcpxml_time(timing.duration_ticks),
    }
    if include_lane:
        if lane is None or lane < 1:
            raise ValueError("connected FCPXML clip requires a positive lane")
        attributes["lane"] = str(lane)
    if "audio" in clip.media_types:
        attributes["audioRole"] = "dialogue"
    return ET.Element("asset-clip", attributes)


def _source_catalog(timeline: NleHandoffTimeline) -> dict[str, NleHandoffClip]:
    catalog: dict[str, NleHandoffClip] = {}
    for clip in _all_clips(timeline):
        catalog.setdefault(clip.source_id, clip)
    return catalog


def _clip_format_id(
    clip: NleHandoffClip,
    source_formats: dict[tuple[int, int, int, int], str],
) -> str:
    if "video" in clip.media_types:
        return source_formats[_source_format_key(clip)]
    return FCPXML_AUDIO_ONLY_FORMAT_ID


def _source_format_catalog(
    sources: dict[str, NleHandoffClip],
) -> dict[tuple[int, int, int, int], str]:
    keys = {_source_format_key(clip) for clip in sources.values() if "video" in clip.media_types}
    return {key: _source_format_id(key) for key in sorted(keys)}


def _source_format_key(clip: NleHandoffClip) -> tuple[int, int, int, int]:
    if clip.source_is_vfr:
        raise _error("variable-frame-rate video Source is unsupported")
    if (
        clip.source_width is None
        or clip.source_height is None
        or clip.source_nominal_frame_rate is None
    ):
        raise _error("video Source format metadata is unavailable")
    return (
        clip.source_width,
        clip.source_height,
        clip.source_nominal_frame_rate.numerator,
        clip.source_nominal_frame_rate.denominator,
    )


def _source_format_id(key: tuple[int, int, int, int]) -> str:
    width, height, numerator, denominator = key
    return f"format-source-{width}x{height}-{numerator}-{denominator}"


def _source_format_name(width: int, height: int, rate: RationalRate) -> str:
    return f"RoughcutSourceFormat{width}x{height}-{rate.numerator}_{rate.denominator}"


def _error(message: str) -> NleHandoffError:
    return NleHandoffError("nle_export_rate_unsupported", f"Roughcut FCPXML {message}")


def _all_clips(timeline: NleHandoffTimeline) -> Iterable[NleHandoffClip]:
    for track in timeline.tracks:
        yield from track.clips


def _contained_by(clip: NleHandoffClip, parent: NleHandoffClip) -> bool:
    return (
        parent.timeline_in_ticks <= clip.timeline_in_ticks
        and clip.timeline_out_ticks <= parent.timeline_out_ticks
    )


def _project_format_id(timeline: NleHandoffTimeline) -> str:
    return (
        f"format-{timeline.video_width}x{timeline.video_height}-"
        f"{timeline.frame_rate.numerator}-{timeline.frame_rate.denominator}"
    )


def _format_name(timeline: NleHandoffTimeline) -> str:
    return (
        f"RoughcutFormat{timeline.video_width}x{timeline.video_height}-"
        f"{timeline.frame_rate.numerator}_{timeline.frame_rate.denominator}"
    )
