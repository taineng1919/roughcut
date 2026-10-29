from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest
from nle_test_fixtures import build_timeline

from roughcut.adapters import fcp7_xml, fcpxml
from roughcut.adapters.nle_clip_organization import coalesce_nle_clips
from roughcut.domain.nle_handoff import (
    NleHandoffGap,
    NleHandoffTimeline,
)
from roughcut.domain.time import TICKS_PER_SECOND, RationalRate


def _timeline(tmp_path: Path, auxiliary_count: int = 0) -> NleHandoffTimeline:
    base = build_timeline(tmp_path, source_rate=RationalRate(50, 1))
    tracks = []
    for track_index in range(auxiliary_count + 1):
        template = base.tracks[bool(track_index)].clips[0]
        track_id = "main" if track_index == 0 else f"aux_{track_index}"
        source_start = (2 + track_index) * TICKS_PER_SECOND
        clips = tuple(
            replace(
                template,
                logical_clip_id=f"{track_id}_{index}",
                av_link_id=f"link_{track_id}_{index}",
                source_id=f"source_{track_id}",
                camera_id=track_id,
                track_id=track_id,
                source_in_ticks=source_start + index * TICKS_PER_SECOND,
                source_out_ticks=source_start + (index + 1) * TICKS_PER_SECOND,
                timeline_in_ticks=index * TICKS_PER_SECOND,
                timeline_out_ticks=(index + 1) * TICKS_PER_SECOND,
                source_timecode_origin_ticks=(track_index + 1) * 10 * TICKS_PER_SECOND,
            )
            for index in range(3)
        )
        tracks.append(replace(
            base.tracks[bool(track_index)],
            track_id=track_id, camera_id=track_id, items=clips,
        ))
    return replace(base, duration_ticks=3 * TICKS_PER_SECOND, tracks=tuple(tracks))


def _seconds(value: str) -> int:
    ticks = Fraction(value.removesuffix("s")) * TICKS_PER_SECOND
    assert ticks.denominator == 1
    return ticks.numerator


# (logical track, media type, Source ID, output in/out, file-local source in).
Span = tuple[int, str, str, int, int, int]


def _serialized_spans(payload: bytes, route: str) -> tuple[Span, ...]:
    root = ET.fromstring(payload)
    spans = []
    if route == "fcpxml":
        assets = {asset.attrib["id"]: asset for asset in root.findall("./resources/asset")}
        for parent in root.findall(".//sequence/spine/asset-clip"):
            parent_in = _seconds(parent.attrib["offset"])
            parent_start = _seconds(parent.attrib["start"])
            for item in (parent, *parent.findall("asset-clip")):
                asset = assets[item.attrib["ref"]]
                output_in = parent_in if item is parent else (
                    parent_in + _seconds(item.attrib["offset"]) - parent_start
                )
                for media in ("video", "audio"):
                    if asset.attrib[f"has{media.title()}"] == "1":
                        spans.append((
                            int(item.attrib.get("lane", "0")), media, asset.attrib["uid"],
                            output_in, output_in + _seconds(item.attrib["duration"]),
                            _seconds(item.attrib["start"]) - _seconds(asset.attrib["start"]),
                        ))
    else:
        files = {item.attrib["id"]: item for item in root.findall(".//file")
                 if item.find("rate") is not None}

        def frame_ticks(element: ET.Element) -> int:
            rate = int(element.findtext("rate/timebase", "0"))
            duration = Fraction(TICKS_PER_SECOND, rate)
            if element.findtext("rate/ntsc") == "TRUE":
                duration *= Fraction(1001, 1000)
            assert duration.denominator == 1
            return duration.numerator

        sequence = root.find("sequence")
        assert sequence is not None
        sequence_frame = frame_ticks(sequence)
        for media in ("video", "audio"):
            for track_index, track in enumerate(root.findall(f"./sequence/media/{media}/track")):
                for item in track.findall("clipitem"):
                    file = item.find("file")
                    assert file is not None
                    source = files[file.attrib["id"]]
                    source_frame = frame_ticks(source)
                    spans.append((
                        track_index, media, file.attrib["id"].removeprefix("file-"),
                        int(item.findtext("start", "0")) * sequence_frame,
                        int(item.findtext("end", "0")) * sequence_frame,
                        int(item.findtext("in", "0")) * sequence_frame
                        + int(item.findtext("mixedratesoffset", "0")) * source_frame,
                    ))
    return tuple(spans)


def _assert_same_playback(before: bytes, after: bytes, route: str) -> None:
    old, new = _serialized_spans(before, route), _serialized_spans(after, route)
    # Check every source frame, plus both sides of every actual XML boundary.
    # These boundary partitions also prove equivalence between sampled frames:
    # both existing writers express slope-one source playback within a span.
    points = {point for span in (*old, *new) for point in (span[3], span[4] - 1)}
    points.update(range(0, 3 * TICKS_PER_SECOND, TICKS_PER_SECOND // 50))
    def at(spans: tuple[Span, ...], output: int) -> list[tuple[int, str, str, int]]:
        return sorted(
            (track, media, source, source_in + output - start)
            for track, media, source, start, end, source_in in spans
            if start <= output < end
        )

    for output in points:
        assert at(old, output) == at(new, output), output
    assert ET.fromstring(before).find(".//sequence").attrib.get("duration") == (
        ET.fromstring(after).find(".//sequence").attrib.get("duration")
    )
    if route == "fcp7_xml":
        assert ET.fromstring(before).findtext(".//sequence/duration") == (
            ET.fromstring(after).findtext(".//sequence/duration")
        )


def _raw_export(
    timeline: NleHandoffTimeline, route: str, monkeypatch: pytest.MonkeyPatch
) -> bytes:
    module = fcpxml if route == "fcpxml" else fcp7_xml
    with monkeypatch.context() as patch:
        patch.setattr(module, "coalesce_nle_clips", lambda value, **_kwargs: (
            value, {clip.logical_clip_id: (clip.logical_clip_id,)
                    for track in value.tracks for clip in track.clips},
        ))
        return module.write_fcpxml(timeline) if route == "fcpxml" else module.write_fcp7_xml(timeline)


@pytest.mark.parametrize("route", ["fcpxml", "fcp7_xml"])
@pytest.mark.parametrize("auxiliary_count", [0, 1, 2])
@pytest.mark.parametrize("main_rate,aux_rate", [(50, 50), (25, 50), (50, 25)])
def test_contiguous_export_coalesces_with_identical_playback_and_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str, auxiliary_count: int,
    main_rate: int, aux_rate: int,
) -> None:
    timeline = _timeline(tmp_path, auxiliary_count)
    timeline = replace(timeline, tracks=tuple(
        replace(track, items=tuple(
            replace(clip, source_nominal_frame_rate=RationalRate(
                main_rate if track.role == "main" else aux_rate, 1
            )) for clip in track.clips
        )) for track in timeline.tracks
    ))
    original = timeline.to_dict()
    before = _raw_export(timeline, route, monkeypatch)
    after = fcpxml.write_fcpxml(timeline) if route == "fcpxml" else fcp7_xml.write_fcp7_xml(timeline)
    _assert_same_playback(before, after, route)
    assert timeline.to_dict() == original
    organized, members = coalesce_nle_clips(timeline, route=route)
    assert [len(track.clips) for track in organized.tracks] == [1] * (auxiliary_count + 1)
    assert coalesce_nle_clips(organized, route=route)[0] == organized
    for track in timeline.tracks:
        ids = tuple(clip.logical_clip_id for clip in track.clips)
        assert members[ids[0]] == ids
        assert ",".join(ids).encode() in after
    if route == "fcp7_xml":
        xml = ET.fromstring(after)
        items = {item.attrib["id"]: item for item in xml.findall(".//clipitem")}
        for item_id, item in items.items():
            links = item.findall("link")
            assert len(links) == 2
            for link in links:
                target_id = link.findtext("linkclipref", "")
                assert target_id in items
                assert item_id in [other.findtext("linkclipref")
                                   for other in items[target_id].findall("link")]


@pytest.mark.parametrize("route", ["fcpxml", "fcp7_xml"])
@pytest.mark.parametrize("change", ["gap", "overlap", "repeat", "reorder", "source"])
def test_real_edits_never_coalesce(
    tmp_path: Path, route: str, change: str
) -> None:
    base = _timeline(tmp_path)
    first, second, _unused = base.main_track.clips
    shift = {"gap": 4_800, "overlap": -4_800, "repeat": -120_000,
             "reorder": -240_000, "source": 0}[change]
    second = replace(second, source_in_ticks=second.source_in_ticks + shift,
                     source_out_ticks=second.source_out_ticks + shift)
    if change == "source":
        second = replace(second, source_id="another_source")
    base = replace(base, tracks=(replace(base.main_track, items=(first, second)),),
                   duration_ticks=second.timeline_out_ticks)
    organized, _members = coalesce_nle_clips(base, route=route)
    assert organized == base


@pytest.mark.parametrize("route", ["fcpxml", "fcp7_xml"])
@pytest.mark.parametrize("change", ["missing", "partial", "mapping", "source", "audio"])
def test_auxiliary_changes_preserve_the_main_cut(
    tmp_path: Path, route: str, change: str
) -> None:
    base = _timeline(tmp_path, 2)
    track = base.tracks[2]
    first, second, third = track.clips
    items = [first, second, third]
    if change == "missing":
        items[1] = NleHandoffGap("missing", second.timeline_in_ticks,
                                 second.timeline_out_ticks, track.camera_id, track.track_id)
    elif change == "partial":
        items[1:2] = [replace(second, source_out_ticks=second.source_out_ticks - 4_800,
                              timeline_out_ticks=second.timeline_out_ticks - 4_800),
                      NleHandoffGap("missing", second.timeline_out_ticks - 4_800,
                                    second.timeline_out_ticks, track.camera_id, track.track_id)]
    elif change == "mapping":
        items[1] = replace(second, source_in_ticks=second.source_in_ticks + 4_800,
                           source_out_ticks=second.source_out_ticks + 4_800)
    else:
        items[1] = replace(second, source_id="changed_aux_source",
                           media_types=("video",) if change == "audio" else second.media_types)
    base = replace(base, tracks=(*base.tracks[:2], replace(track, items=tuple(items))))
    organized, _members = coalesce_nle_clips(base, route=route)
    assert organized == base


@pytest.mark.parametrize("source_in,duration", [(2_400, 6_000), (1_200, 8_400)])
def test_canonical_contiguity_cannot_hide_quantized_gap_or_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_in: int, duration: int
) -> None:
    base = _timeline(tmp_path)
    clips = tuple(replace(
        clip, source_in_ticks=source_in + index * duration,
        source_out_ticks=source_in + (index + 1) * duration,
        timeline_in_ticks=index * duration, timeline_out_ticks=(index + 1) * duration,
    ) for index, clip in enumerate(base.main_track.clips[:2]))
    base = replace(base, duration_ticks=2 * duration,
                   tracks=(replace(base.main_track, items=clips),))
    raw = _raw_export(base, "fcpxml", monkeypatch)
    spans = [span for span in _serialized_spans(raw, "fcpxml") if span[1] == "video"]
    assert clips[0].source_out_ticks == clips[1].source_in_ticks
    assert spans[0][5] + spans[0][4] - spans[0][3] != spans[1][5]
    assert coalesce_nle_clips(base, route="fcpxml")[0] == base
    assert fcpxml.write_fcpxml(base) == raw


@pytest.mark.parametrize("route", ["fcpxml", "fcp7_xml"])
def test_organization_continues_after_a_real_deleted_interval(tmp_path: Path, route: str) -> None:
    base = _timeline(tmp_path)
    first, second, third = base.main_track.clips
    second = replace(second, source_in_ticks=second.source_in_ticks + TICKS_PER_SECOND,
                     source_out_ticks=second.source_out_ticks + TICKS_PER_SECOND)
    third = replace(third, source_in_ticks=third.source_in_ticks + TICKS_PER_SECOND,
                    source_out_ticks=third.source_out_ticks + TICKS_PER_SECOND)
    base = replace(base, tracks=(replace(base.main_track, items=(first, second, third)),))
    organized, members = coalesce_nle_clips(base, route=route)
    assert len(organized.main_track.clips) == 2
    assert organized.main_track.clips[0] == first
    assert organized.main_track.clips[1].source_in_ticks == second.source_in_ticks
    assert organized.main_track.clips[1].source_out_ticks == third.source_out_ticks
    assert members[second.logical_clip_id] == (second.logical_clip_id, third.logical_clip_id)


@pytest.mark.parametrize("route", ["fcpxml", "fcp7_xml"])
def test_merging_cannot_hide_an_unsupported_original_clip(tmp_path: Path, route: str) -> None:
    from roughcut.domain.nle_handoff import NleHandoffError

    base = _timeline(tmp_path)
    clips = tuple(replace(
        clip, source_in_ticks=index * 2_400, source_out_ticks=(index + 1) * 2_400,
        timeline_in_ticks=index * 2_400, timeline_out_ticks=(index + 1) * 2_400,
    ) for index, clip in enumerate(base.main_track.clips[:2]))
    base = replace(base, duration_ticks=4_800, tracks=(replace(base.main_track, items=clips),))
    with pytest.raises(NleHandoffError):
        coalesce_nle_clips(base, route=route)
    hull = replace(clips[0], source_out_ticks=4_800, timeline_out_ticks=4_800)
    hull_timeline = replace(base, tracks=(replace(base.main_track, items=(hull,)),))
    assert coalesce_nle_clips(hull_timeline, route=route)[0] == hull_timeline


@pytest.mark.parametrize("route", ["fcpxml", "fcp7_xml"])
@pytest.mark.parametrize("case", ["audio_only", "ntsc"])
def test_contiguous_audio_only_and_ntsc_exports_preserve_playback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str, case: str
) -> None:
    base = _timeline(tmp_path)
    if case == "audio_only":
        clips = tuple(replace(
            clip, media_types=("audio",), source_width=None, source_height=None,
            source_nominal_frame_rate=None,
        ) for clip in base.main_track.clips)
        base = replace(base, tracks=(replace(
            base.main_track, items=clips, media_types=("audio",)
        ),))
    else:
        sequence_rate, source_rate = RationalRate(30_000, 1001), RationalRate(60_000, 1001)
        duration = 30 * sequence_rate.ticks_per_frame
        clips = tuple(replace(
            clip, source_nominal_frame_rate=source_rate,
            source_duration_ticks=4000 * source_rate.ticks_per_frame,
            source_in_ticks=(index + 2) * duration, source_out_ticks=(index + 3) * duration,
            timeline_in_ticks=index * duration, timeline_out_ticks=(index + 1) * duration,
        ) for index, clip in enumerate(base.main_track.clips))
        base = replace(base, frame_rate=sequence_rate, duration_ticks=3 * duration,
                       tracks=(replace(base.main_track, items=clips),))
    raw = _raw_export(base, route, monkeypatch)
    after = fcpxml.write_fcpxml(base) if route == "fcpxml" else fcp7_xml.write_fcp7_xml(base)
    _assert_same_playback(raw, after, route)
    assert len(coalesce_nle_clips(base, route=route)[0].main_track.clips) == 1
