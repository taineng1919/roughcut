from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from roughcut.adapters.fcp7_xml import write_fcp7_xml
from roughcut.domain.nle_handoff import NleHandoffError
from roughcut.domain.time import RationalRate
from nle_test_fixtures import build_mixed_rate_timeline, build_timeline


def test_fcp7_multicam_has_ordered_tracks_av_links_and_gaps(tmp_path: Path) -> None:
    timeline = build_timeline(tmp_path)
    first = write_fcp7_xml(timeline)
    second = write_fcp7_xml(timeline)
    root = ET.fromstring(first)
    sequence = root.find("sequence")
    assert sequence is not None
    assert root.attrib == {"version": "5"}
    assert first == second
    assert sequence.findtext("duration") == "750"

    video_tracks = sequence.findall("./media/video/track")
    audio_tracks = sequence.findall("./media/audio/track")
    assert len(video_tracks) == 2
    assert len(audio_tracks) == 2
    assert len(video_tracks[0].findall("clipitem")) == 3
    assert len(video_tracks[1].findall("clipitem")) == 3
    assert len(audio_tracks[0].findall("clipitem")) == 3
    assert len(audio_tracks[1].findall("clipitem")) == 3

    aux_video = video_tracks[1].findall("clipitem")
    assert [
        (int(item.findtext("start", "-1")), int(item.findtext("end", "-1"))) for item in aux_video
    ] == [
        (0, 250),
        (250, 375),
        (500, 750),
    ]
    assert all(item.find("file") is not None for item in aux_video)
    assert root.findall(".//generatoritem") == []
    assert b"black" not in first.lower()
    assert b"camera_" not in first.lower()
    assert b"mc-clip" not in first
    assert b"/renders/" not in first
    file_names = [
        item.findtext("name", "")
        for item in root.findall("./sequence/media/video/track/clipitem/file")
    ]
    assert not any(name.startswith("camera_") and name.endswith(".mp4") for name in file_names)
    assert b"parallel" not in first.lower()
    assert b"%20" in first
    assert b"%E5%8E%9F%E5%A7%8B%E7%B4%A0%E6%9D%90" in first

    video_items = {
        item.attrib["id"]: item for item in sequence.findall("./media/video/track/clipitem")
    }
    audio_items = {
        item.attrib["id"]: item for item in sequence.findall("./media/audio/track/clipitem")
    }
    for item in (*video_items.values(), *audio_items.values()):
        links = {link.findtext("linkclipref") for link in item.findall("link")}
        if item.attrib["id"].startswith("video-"):
            assert item.attrib["id"] in links
            assert item.attrib["id"].replace("video-", "audio-", 1) in links
        else:
            assert item.attrib["id"] in links
            assert item.attrib["id"].replace("audio-", "video-", 1) in links


def test_fcp7_single_camera_keeps_source_audio_video_relationship(tmp_path: Path) -> None:
    root = ET.fromstring(write_fcp7_xml(build_timeline(tmp_path, multicam=False)))
    assert len(root.findall("./sequence/media/video/track")) == 1
    assert len(root.findall("./sequence/media/audio/track")) == 1
    assert len(root.findall("./sequence/media/video/track/clipitem")) == 3
    assert len(root.findall("./sequence/media/audio/track/clipitem")) == 3
    assert root.findall(".//generatoritem") == []
    value = write_fcp7_xml(build_timeline(tmp_path, multicam=False))
    assert b"/renders/" not in value
    assert b"parallel" not in value.lower()
    assert b"mc-clip" not in value


@pytest.mark.parametrize(
    ("rate", "timebase", "ntsc"),
    [
        (RationalRate(25, 1), "25", "FALSE"),
        (RationalRate(24, 1), "24", "FALSE"),
        (RationalRate(30000, 1001), "30", "TRUE"),
        (RationalRate(24000, 1001), "24", "TRUE"),
    ],
)
def test_fcp7_rate_is_explicit_and_exact(
    tmp_path: Path,
    rate: RationalRate,
    timebase: str,
    ntsc: str,
) -> None:
    root = ET.fromstring(write_fcp7_xml(build_timeline(tmp_path, multicam=False, rate=rate)))
    sequence_rate = root.find("./sequence/rate")
    assert sequence_rate is not None
    assert sequence_rate.findtext("timebase") == timebase
    assert sequence_rate.findtext("ntsc") == ntsc


@pytest.mark.parametrize(
    (
        "project_rate",
        "source_rate",
        "project_timebase",
        "project_ntsc",
        "source_timebase",
        "source_ntsc",
    ),
    [
        (RationalRate(25, 1), RationalRate(50, 1), "25", "FALSE", "50", "FALSE"),
        (
            RationalRate(30_000, 1_001),
            RationalRate(60_000, 1_001),
            "30",
            "TRUE",
            "60",
            "TRUE",
        ),
    ],
)
def test_fcp7_mixed_rate_separates_sequence_and_source_coordinates(
    tmp_path: Path,
    project_rate: RationalRate,
    source_rate: RationalRate,
    project_timebase: str,
    project_ntsc: str,
    source_timebase: str,
    source_ntsc: str,
) -> None:
    root = ET.fromstring(
        write_fcp7_xml(
            build_mixed_rate_timeline(
                tmp_path,
                project_rate=project_rate,
                source_rate=source_rate,
            )
        )
    )
    sequence = root.find("./sequence")
    clipitem = root.find("./sequence/media/video/track/clipitem")
    assert sequence is not None and clipitem is not None
    sequence_rate = sequence.find("rate")
    clip_rate = clipitem.find("rate")
    source_file = clipitem.find("file")
    assert sequence_rate is not None and clip_rate is not None and source_file is not None
    source_file_rate = source_file.find("rate")
    source_video_rate = source_file.find("./media/video/samplecharacteristics/rate")
    assert source_file_rate is not None and source_video_rate is not None
    assert sequence_rate.findtext("timebase") == project_timebase
    assert sequence_rate.findtext("ntsc") == project_ntsc
    assert clip_rate.findtext("timebase") == project_timebase
    assert clip_rate.findtext("ntsc") == project_ntsc
    assert source_file_rate.findtext("timebase") == source_timebase
    assert source_file_rate.findtext("ntsc") == source_ntsc
    assert source_video_rate.findtext("timebase") == source_timebase
    assert source_video_rate.findtext("ntsc") == source_ntsc
    assert sequence.findtext("duration") == "50"
    assert clipitem.findtext("duration") == "50"
    assert clipitem.findtext("start") == "0"
    assert clipitem.findtext("end") == "50"
    assert clipitem.findtext("in") == "5"
    assert clipitem.findtext("out") == "55"
    assert clipitem.findtext("mixedratesoffset") == "1"
    mixed_rates_offset = int(clipitem.findtext("mixedratesoffset", "0"))
    source_frames_per_sequence_frame = Fraction(
        source_rate.numerator * project_rate.denominator,
        source_rate.denominator * project_rate.numerator,
    )
    assert (
        int(clipitem.findtext("in", "-1")) * source_frames_per_sequence_frame + mixed_rates_offset
        == 11
    )
    assert (
        int(clipitem.findtext("out", "-1")) * source_frames_per_sequence_frame + mixed_rates_offset
        == 111
    )
    assert source_file.findtext("duration") == "4000"
    assert source_file.findtext("./media/video/samplecharacteristics/width") == "3840"
    assert source_file.findtext("./media/video/samplecharacteristics/height") == "2160"
    assert source_file.find("./media/audio/samplecharacteristics/channelcount") is None
    assert source_file.find("./media/audio/samplecharacteristics/depth") is None


def test_fcp7_rejects_vfr_video(tmp_path: Path) -> None:
    base = build_timeline(tmp_path, multicam=False)
    vfr_clips = tuple(replace(clip, source_is_vfr=True) for clip in base.main_track.clips)
    timeline = replace(base, tracks=(replace(base.main_track, items=vfr_clips),))

    with pytest.raises(NleHandoffError, match="variable-frame-rate"):
        write_fcp7_xml(timeline)


@pytest.mark.parametrize(
    ("project_rate", "source_rate", "expected_message"),
    [
        (RationalRate(25, 1), RationalRate(50, 1), "not representable"),
        (RationalRate(25, 1), RationalRate(100, 1), "zero frames"),
    ],
)
def test_fcp7_rejects_unrepresentable_mixed_rate_duration(
    tmp_path: Path,
    project_rate: RationalRate,
    source_rate: RationalRate,
    expected_message: str,
) -> None:
    base = build_timeline(
        tmp_path,
        multicam=False,
        rate=project_rate,
        source_rate=source_rate,
    )
    source_frame_ticks = source_rate.ticks_per_frame
    clip = replace(
        base.main_track.clips[0],
        source_in_ticks=0,
        source_out_ticks=source_frame_ticks,
        timeline_in_ticks=0,
        timeline_out_ticks=source_frame_ticks,
    )
    track = replace(base.main_track, items=(clip,))
    timeline = replace(base, duration_ticks=source_frame_ticks, tracks=(track,))

    with pytest.raises(NleHandoffError, match=expected_message):
        write_fcp7_xml(timeline)


def test_fcp7_rejects_non_source_frame_aligned_boundary(tmp_path: Path) -> None:
    base = build_mixed_rate_timeline(
        tmp_path,
        project_rate=RationalRate(25, 1),
        source_rate=RationalRate(50, 1),
    )
    clip = base.main_track.clips[0]
    misaligned = replace(
        clip,
        source_in_ticks=clip.source_in_ticks + 1,
        source_out_ticks=clip.source_out_ticks + 1,
    )
    timeline = replace(
        base,
        tracks=(replace(base.main_track, items=(misaligned,)),),
    )

    with pytest.raises(NleHandoffError, match="not aligned to an exact source frame"):
        write_fcp7_xml(timeline)


def test_fcp7_rejects_unrepresentable_vendor_rate(tmp_path: Path) -> None:
    timeline = build_timeline(tmp_path, multicam=False, rate=RationalRate(100, 3))
    with pytest.raises(NleHandoffError, match="cannot represent frame rate"):
        write_fcp7_xml(timeline)
