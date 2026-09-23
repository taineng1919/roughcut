"""Pure FCP7 XML/xmeml v5 serialization for a validated handoff timeline."""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from typing import cast

from roughcut.adapters.nle_time import (
    fcp7_frame_range,
    fcp7_rate,
    ticks_to_exact_frame,
    ticks_to_frame_boundary,
)
from roughcut.domain.nle_handoff import (
    FCP7_XML_EXPORT_PROFILE,
    NleHandoffClip,
    NleHandoffError,
    NleHandoffTimeline,
    NleHandoffTrack,
)
from roughcut.domain.time import RationalRate

FCP7_XML_VERSION = "5"


def write_fcp7_xml(timeline: NleHandoffTimeline) -> bytes:
    """Return deterministic xmeml v5 bytes without reading project state."""

    if FCP7_XML_EXPORT_PROFILE != "roughcut_fcp7_xml_xmeml_v5":
        raise ValueError("FCP7 XML export profile identity changed")
    rate = fcp7_rate(timeline.frame_rate)
    sources = _source_catalog(timeline)
    root = ET.Element("xmeml", {"version": FCP7_XML_VERSION})
    sequence = ET.SubElement(root, "sequence", {"id": f"sequence-{timeline.project_id}"})
    _text(sequence, "name", "Roughcut editable handoff")
    _text(
        sequence, "duration", ticks_to_frame_boundary(timeline.duration_ticks, timeline.frame_rate)
    )
    _add_rate(sequence, rate.timebase, rate.ntsc)
    timecode = ET.SubElement(sequence, "timecode")
    _add_rate(timecode, rate.timebase, rate.ntsc)
    _text(timecode, "string", "00:00:00:00")
    _text(timecode, "frame", 0)
    _text(timecode, "displayformat", "NDF")

    media = ET.SubElement(sequence, "media")
    video_tracks = [track for track in timeline.tracks if "video" in track.media_types]
    audio_tracks = [track for track in timeline.tracks if "audio" in track.media_types]
    video = ET.SubElement(media, "video") if video_tracks else None
    audio = ET.SubElement(media, "audio") if audio_tracks else None
    if video is not None:
        _add_video_format(video, timeline, rate.timebase, rate.ntsc)

    source_definitions: set[str] = set()
    group_indexes = _group_indexes(timeline)
    track_indexes = {
        "video": {track.track_id: index for index, track in enumerate(video_tracks, start=1)},
        "audio": {track.track_id: index for index, track in enumerate(audio_tracks, start=1)},
    }
    clip_indexes = _clip_indexes(timeline)
    if video is not None:
        for track in video_tracks:
            video_track = ET.SubElement(video, "track")
            _add_track_items(
                video_track,
                track,
                media_type="video",
                rate_timebase=rate.timebase,
                rate_ntsc=rate.ntsc,
                timeline=timeline,
                sources=sources,
                source_definitions=source_definitions,
                group_indexes=group_indexes,
                track_indexes=track_indexes,
                clip_indexes=clip_indexes,
            )
    if audio is not None:
        for track in audio_tracks:
            audio_track = ET.SubElement(audio, "track")
            _add_track_items(
                audio_track,
                track,
                media_type="audio",
                rate_timebase=rate.timebase,
                rate_ntsc=rate.ntsc,
                timeline=timeline,
                sources=sources,
                source_definitions=source_definitions,
                group_indexes=group_indexes,
                track_indexes=track_indexes,
                clip_indexes=clip_indexes,
            )

    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="utf-8", short_empty_elements=True)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n' + cast(bytes, body) + b"\n"


def _add_track_items(
    parent: ET.Element,
    track: NleHandoffTrack,
    *,
    media_type: str,
    rate_timebase: int,
    rate_ntsc: bool,
    timeline: NleHandoffTimeline,
    sources: dict[str, NleHandoffClip],
    source_definitions: set[str],
    group_indexes: dict[str, int],
    track_indexes: dict[str, dict[str, int]],
    clip_indexes: dict[tuple[str, str, str], int],
) -> None:
    for clip in track.clips:
        if media_type not in clip.media_types:
            continue
        frame_range = fcp7_frame_range(
            source_in_ticks=clip.source_in_ticks,
            source_out_ticks=clip.source_out_ticks,
            timeline_in_ticks=clip.timeline_in_ticks,
            timeline_out_ticks=clip.timeline_out_ticks,
            source_duration_ticks=clip.source_duration_ticks,
            sequence_rate=timeline.frame_rate,
            source_rate=_source_rate(clip, timeline),
        )
        item_id = _item_id(media_type, clip.logical_clip_id)
        item = ET.SubElement(parent, "clipitem", {"id": item_id})
        _text(item, "name", f"{clip.logical_clip_id} | {clip.camera_id} | {clip.source_id}")
        _text(item, "duration", frame_range.timeline_out - frame_range.timeline_in)
        _add_rate(item, rate_timebase, rate_ntsc)
        _text(item, "enabled", "TRUE")
        _text(item, "in", frame_range.source_in)
        _text(item, "out", frame_range.source_out)
        _text(item, "start", frame_range.timeline_in)
        _text(item, "end", frame_range.timeline_out)
        if frame_range.mixed_rates_offset is not None:
            _text(item, "mixedratesoffset", frame_range.mixed_rates_offset)
        if clip.source_id not in source_definitions:
            _add_file_definition(
                item,
                sources[clip.source_id],
                timeline,
            )
            source_definitions.add(clip.source_id)
        else:
            ET.SubElement(item, "file", {"id": f"file-{clip.source_id}"})
        sourcetrack = ET.SubElement(item, "sourcetrack")
        _text(sourcetrack, "mediatype", media_type)
        _text(sourcetrack, "trackindex", 1)
        _add_links(
            item,
            clip,
            group_index=group_indexes[clip.logical_clip_id],
            track_indexes=track_indexes,
            clip_indexes=clip_indexes,
        )


def _add_links(
    parent: ET.Element,
    clip: NleHandoffClip,
    *,
    group_index: int,
    track_indexes: dict[str, dict[str, int]],
    clip_indexes: dict[tuple[str, str, str], int],
) -> None:
    linked_types = [item for item in ("video", "audio") if item in clip.media_types]
    for linked_type in linked_types:
        track_index = track_indexes[linked_type].get(clip.track_id)
        if track_index is None:
            raise ValueError("A/V link refers to a missing logical media track")
        link = ET.SubElement(parent, "link")
        _text(link, "linkclipref", _item_id(linked_type, clip.logical_clip_id))
        _text(link, "mediatype", linked_type)
        _text(link, "trackindex", track_index)
        _text(link, "clipindex", clip_indexes[(clip.track_id, linked_type, clip.logical_clip_id)])
        _text(link, "groupindex", group_index)


def _add_file_definition(
    parent: ET.Element,
    clip: NleHandoffClip,
    timeline: NleHandoffTimeline,
) -> None:
    file_element = ET.SubElement(parent, "file", {"id": f"file-{clip.source_id}"})
    _text(file_element, "name", clip.source_display_name)
    source_rate = _source_rate(clip, timeline)
    source_fcp7_rate = fcp7_rate(source_rate)
    _add_rate(file_element, source_fcp7_rate.timebase, source_fcp7_rate.ntsc)
    _text(
        file_element,
        "duration",
        ticks_to_exact_frame(
            clip.source_duration_ticks,
            source_rate,
            field="source duration",
        ),
    )
    _text(file_element, "pathurl", clip.source_locator.as_uri())
    media = ET.SubElement(file_element, "media")
    if "video" in clip.media_types:
        video = ET.SubElement(media, "video")
        characteristics = ET.SubElement(video, "samplecharacteristics")
        if clip.source_width is None or clip.source_height is None:
            raise _error("video Source dimensions are unavailable")
        _text(characteristics, "width", clip.source_width)
        _text(characteristics, "height", clip.source_height)
        _text(characteristics, "pixelaspectratio", "square")
        _text(characteristics, "fielddominance", "none")
        _add_rate(characteristics, source_fcp7_rate.timebase, source_fcp7_rate.ntsc)
    if "audio" in clip.media_types:
        audio = ET.SubElement(media, "audio")
        characteristics = ET.SubElement(audio, "samplecharacteristics")
        if clip.source_audio_sample_rate is not None:
            _text(characteristics, "samplerate", clip.source_audio_sample_rate)


def _add_video_format(
    parent: ET.Element,
    timeline: NleHandoffTimeline,
    rate_timebase: int,
    rate_ntsc: bool,
) -> None:
    format_element = ET.SubElement(parent, "format")
    characteristics = ET.SubElement(format_element, "samplecharacteristics")
    _text(characteristics, "width", timeline.video_width)
    _text(characteristics, "height", timeline.video_height)
    _text(characteristics, "pixelaspectratio", "square")
    _text(characteristics, "fielddominance", "none")
    _add_rate(characteristics, rate_timebase, rate_ntsc)


def _add_rate(parent: ET.Element, timebase: int, ntsc: bool) -> None:
    rate = ET.SubElement(parent, "rate")
    _text(rate, "timebase", timebase)
    _text(rate, "ntsc", "TRUE" if ntsc else "FALSE")


def _text(parent: ET.Element, tag: str, value: object) -> None:
    child = ET.SubElement(parent, tag)
    child.text = str(value)


def _source_rate(clip: NleHandoffClip, timeline: NleHandoffTimeline) -> RationalRate:
    if "video" in clip.media_types:
        if clip.source_is_vfr:
            raise _error("variable-frame-rate video Source is unsupported")
        if clip.source_nominal_frame_rate is None:
            raise _error("video Source nominal frame rate is unavailable")
        return clip.source_nominal_frame_rate
    return clip.source_nominal_frame_rate or timeline.frame_rate


def _error(message: str) -> NleHandoffError:
    return NleHandoffError("nle_export_rate_unsupported", f"Roughcut FCP7 XML {message}")


def _source_catalog(timeline: NleHandoffTimeline) -> dict[str, NleHandoffClip]:
    catalog: dict[str, NleHandoffClip] = {}
    for clip in _all_clips(timeline):
        catalog.setdefault(clip.source_id, clip)
    return catalog


def _all_clips(timeline: NleHandoffTimeline) -> Iterable[NleHandoffClip]:
    for track in timeline.tracks:
        yield from track.clips


def _group_indexes(timeline: NleHandoffTimeline) -> dict[str, int]:
    indexes: dict[str, int] = {}
    for index, clip in enumerate(_all_clips(timeline), start=1):
        indexes.setdefault(clip.logical_clip_id, index)
    return indexes


def _clip_indexes(timeline: NleHandoffTimeline) -> dict[tuple[str, str, str], int]:
    indexes: dict[tuple[str, str, str], int] = {}
    for track in timeline.tracks:
        for media_type in ("video", "audio"):
            index = 0
            for clip in track.clips:
                if media_type not in clip.media_types:
                    continue
                index += 1
                indexes[(track.track_id, media_type, clip.logical_clip_id)] = index
    return indexes


def _item_id(media_type: str, logical_clip_id: str) -> str:
    value = f"{media_type}-{logical_clip_id}"
    if len(value) <= 128:
        return value
    digest = hashlib.sha256(logical_clip_id.encode("utf-8")).hexdigest()[:32]
    return f"{media_type}-{digest}"
