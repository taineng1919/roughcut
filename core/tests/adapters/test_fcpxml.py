from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

import pytest
from nle_test_fixtures import (
    FCPXML_FORENSIC_CASES,
    FcpxmlForensicCase,
    build_fcpxml_8789_offset_derivative_timeline,
    build_fcpxml_forensic_timeline,
    build_many_clip_timeline,
    build_mixed_rate_timeline,
    build_partial_connected_child_timeline,
    build_timeline,
)

from roughcut.adapters.fcp7_xml import write_fcp7_xml
from roughcut.adapters.fcpxml import write_fcpxml
from roughcut.adapters.nle_time import (
    fcpxml_time,
    frame_boundary_ticks,
    frame_duration,
    project_fcpxml_timing,
)
from roughcut.domain.nle_handoff import NleHandoffError
from roughcut.domain.time import TICKS_PER_SECOND, RationalRate


def test_fcpxml_single_camera_uses_original_sources_and_decision_order(tmp_path: Path) -> None:
    timeline = build_timeline(tmp_path, multicam=False)

    first = write_fcpxml(timeline)
    second = write_fcpxml(timeline)
    root = ET.fromstring(first)
    spine = root.find("./library/event/project/sequence/spine")

    assert first == second
    assert root.attrib == {"version": "1.14"}
    assert spine is not None
    assert spine.findall("spine") == []
    main = spine.findall("asset-clip")
    assert [item.attrib["name"].split(" | ", 1)[0] for item in main] == [
        "clip_1",
        "clip_2",
        "clip_3",
    ]
    assert [item.attrib["start"] for item in main] == ["20s", "50s", "35s"]
    assert [item.attrib["offset"] for item in main] == ["0s", "10s", "20s"]
    assert [item.attrib["duration"] for item in main] == ["10s", "10s", "10s"]
    source_url = timeline.main_track.clips[0].source_locator.as_uri().encode("utf-8")
    assert source_url in first
    assert b"%E5%8E%9F%E5%A7%8B%E7%B4%A0%E6%9D%90" in first
    assert b"%20" in first
    assert b"%26" in first
    assert b"generator" not in first
    assert b"black" not in first.lower()
    assert b"mc-clip" not in first
    assert b"conform-rate" not in first
    assert b"timeMap" not in first
    assert b"/renders/" not in first
    asset_sources = [
        item.attrib["src"].rsplit("/", 1)[-1]
        for item in root.findall("./resources/asset/media-rep")
    ]
    assert not any(name.startswith("camera_") and name.endswith(".mp4") for name in asset_sources)
    assert b"parallel" not in first.lower()
    assert b".mp4" in first


def test_fcpxml_multicam_uses_connected_lanes_and_gap_absence(tmp_path: Path) -> None:
    timeline = build_timeline(tmp_path)
    root = ET.fromstring(write_fcpxml(timeline))
    spine = root.find("./library/event/project/sequence/spine")
    assert spine is not None

    main = spine.findall("asset-clip")
    assert len(main) == 3
    connected = [child for item in main for child in item]
    assert len(connected) == 3
    assert all(child.tag == "asset-clip" for child in connected)
    assert all(child.attrib["lane"] == "1" for child in connected)
    assert [child.attrib["start"] for child in connected] == ["23s", "53s", "38s"]
    assert [child.attrib["offset"] for child in connected] == ["20s", "50s", "35s"]
    assert b"<spine" in write_fcpxml(timeline)
    assert b"<spine><spine" not in write_fcpxml(timeline)
    assert root.findall(".//gap") == []
    assert root.findall(".//generator") == []
    value = write_fcpxml(timeline)
    assert b"parallel" not in value.lower()
    assert b"/renders/" not in value
    assert b"mc-clip" not in value


@pytest.mark.parametrize(
    ("rate", "frame_duration"),
    [
        (RationalRate(25, 1), "1/25s"),
        (RationalRate(24, 1), "1/24s"),
        (RationalRate(30000, 1001), "1001/30000s"),
        (RationalRate(24000, 1001), "1001/24000s"),
    ],
)
def test_fcpxml_preserves_rational_project_rate(
    tmp_path: Path,
    rate: RationalRate,
    frame_duration: str,
) -> None:
    root = ET.fromstring(write_fcpxml(build_timeline(tmp_path, multicam=False, rate=rate)))
    format_element = root.find("./resources/format")
    assert format_element is not None
    assert format_element.attrib["frameDuration"] == frame_duration


def test_fcpxml_and_fcp7_normalize_to_the_same_semantics(tmp_path: Path) -> None:
    timeline = build_timeline(tmp_path)
    fcpxml_root = ET.fromstring(write_fcpxml(timeline))
    fcp7_root = ET.fromstring(write_fcp7_xml(timeline))

    fcpxml_spine = fcpxml_root.find("./library/event/project/sequence/spine")
    assert fcpxml_spine is not None
    fcpxml_main: list[tuple[str, str, int, int, int, int]] = []
    fcpxml_auxiliary: list[tuple[str, str, int, int, int, int]] = []
    for main in fcpxml_spine.findall("asset-clip"):
        main_name, _role, main_source_id = main.attrib["name"].split(" | ")
        main_source_start = _parse_seconds(main.attrib["start"])
        main_timeline_start = _parse_seconds(main.attrib["offset"])
        main_duration = _parse_seconds(main.attrib["duration"])
        fcpxml_main.append(
            (
                main_name,
                main_source_id,
                main_timeline_start,
                main_timeline_start + main_duration,
                main_source_start,
                main_source_start + main_duration,
            )
        )
        for auxiliary in main.findall("asset-clip"):
            aux_name, _aux_role, aux_source_id = auxiliary.attrib["name"].split(" | ")
            aux_source_start = _parse_seconds(auxiliary.attrib["start"])
            aux_timeline_start = (
                main_timeline_start + _parse_seconds(auxiliary.attrib["offset"]) - main_source_start
            )
            aux_duration = _parse_seconds(auxiliary.attrib["duration"])
            fcpxml_auxiliary.append(
                (
                    aux_name,
                    aux_source_id,
                    aux_timeline_start,
                    aux_timeline_start + aux_duration,
                    aux_source_start,
                    aux_source_start + aux_duration,
                )
            )

    def fcp7_items(track_index: int) -> list[tuple[str, str, int, int, int, int]]:
        items = fcp7_root.findall(f"./sequence/media/video/track[{track_index}]/clipitem")
        return [
            (
                item.findtext("name", "").split(" | ", 1)[0],
                item.find("file").attrib["id"].removeprefix("file-")
                if item.find("file") is not None
                else "",
                int(item.findtext("start", "0")) * 4_800,
                int(item.findtext("end", "0")) * 4_800,
                int(item.findtext("in", "0")) * 4_800,
                int(item.findtext("out", "0")) * 4_800,
            )
            for item in items
        ]

    assert fcpxml_main == fcp7_items(1)
    assert fcpxml_auxiliary == fcp7_items(2)
    assert [item[0:2] for item in fcpxml_auxiliary] == [
        ("aux_1", "source_b"),
        ("aux_2", "source_b"),
        ("aux_3", "source_b"),
    ]
    assert fcpxml_auxiliary[1][2:4] == (1_200_000, 1_800_000)
    assert fcpxml_auxiliary[1][4:6] == (6_360_000, 6_960_000)


def _parse_seconds(value: str) -> int:
    assert value.endswith("s")
    raw = value[:-1]
    if "/" in raw:
        numerator, denominator = raw.split("/", 1)
        return int(numerator) * 120_000 // int(denominator)
    return int(raw) * 120_000


@pytest.mark.parametrize(
    ("project_rate", "source_rate", "source_frame_duration"),
    [
        (RationalRate(25, 1), RationalRate(50, 1), "1/50s"),
        (RationalRate(30_000, 1_001), RationalRate(60_000, 1_001), "1001/60000s"),
    ],
)
def test_fcpxml_mixed_rate_keeps_source_and_sequence_formats_separate(
    tmp_path: Path,
    project_rate: RationalRate,
    source_rate: RationalRate,
    source_frame_duration: str,
) -> None:
    root = ET.fromstring(
        write_fcpxml(
            build_mixed_rate_timeline(
                tmp_path,
                project_rate=project_rate,
                source_rate=source_rate,
            )
        )
    )
    formats = {item.attrib["id"]: item for item in root.findall("./resources/format")}
    sequence = root.find("./library/event/project/sequence")
    asset = root.find("./resources/asset")
    clip = root.find("./library/event/project/sequence/spine/asset-clip")
    assert sequence is not None and asset is not None and clip is not None
    project_format = formats[sequence.attrib["format"]]
    source_format = formats[asset.attrib["format"]]
    assert project_format.attrib["frameDuration"] == frame_duration(project_rate)
    assert project_format.attrib["width"] == "1920"
    assert project_format.attrib["height"] == "1080"
    assert source_format.attrib["frameDuration"] == source_frame_duration
    assert source_format.attrib["width"] == "3840"
    assert source_format.attrib["height"] == "2160"
    assert asset.attrib["format"] != sequence.attrib["format"]
    assert clip.attrib["format"] == asset.attrib["format"]
    assert clip.attrib["start"] == fcpxml_time(11 * source_rate.ticks_per_frame)
    assert clip.attrib["duration"] == fcpxml_time(100 * source_rate.ticks_per_frame)
    assert clip.attrib["offset"] == "0s"
    assert asset.attrib["duration"] == fcpxml_time(4_000 * source_rate.ticks_per_frame)
    assert "audioChannels" not in asset.attrib
    assert "audioSources" not in asset.attrib
    assert "videoSources" not in asset.attrib


def test_fcpxml_audio_only_uses_undefined_video_rate_without_invented_counts(
    tmp_path: Path,
) -> None:
    base = build_timeline(tmp_path, multicam=False)
    audio_clips = tuple(
        replace(
            clip,
            source_width=None,
            source_height=None,
            source_nominal_frame_rate=None,
            media_types=("audio",),
        )
        for clip in base.main_track.clips
    )
    audio_track = replace(base.main_track, media_types=("audio",), items=audio_clips)
    timeline = replace(base, tracks=(audio_track,))

    root = ET.fromstring(write_fcpxml(timeline))
    audio_format = root.find('./resources/format[@id="format-audio-only"]')
    asset = root.find("./resources/asset")
    clip = root.find("./library/event/project/sequence/spine/asset-clip")
    assert audio_format is not None
    assert audio_format.attrib == {"id": "format-audio-only", "name": "FFFrameRateUndefined"}
    assert asset is not None and clip is not None
    assert asset.attrib["format"] == "format-audio-only"
    assert "frameDuration" not in asset.attrib
    assert "audioChannels" not in asset.attrib
    assert "audioSources" not in asset.attrib
    assert "videoSources" not in asset.attrib
    assert clip.attrib["format"] == "format-audio-only"


def test_fcpxml_rejects_vfr_video(tmp_path: Path) -> None:
    base = build_timeline(tmp_path, multicam=False)
    vfr_clips = tuple(replace(clip, source_is_vfr=True) for clip in base.main_track.clips)
    timeline = replace(base, tracks=(replace(base.main_track, items=vfr_clips),))

    with pytest.raises(NleHandoffError, match="variable-frame-rate"):
        write_fcpxml(timeline)


def test_fcpxml_exact_subsecond_values_are_rational(tmp_path: Path) -> None:
    timeline = build_timeline(tmp_path, multicam=False)
    clip = timeline.main_track.clips[0]
    # The fixture has exact integer-second boundaries; this checks the writer's
    # output remains rational rather than converting through float.
    assert clip.timeline_in_ticks % TICKS_PER_SECOND == 0
    value = write_fcpxml(timeline)
    assert b'duration="30s"' in value


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        (
            FCPXML_FORENSIC_CASES[0],
            {
                "sequence": 206_400,
                "timeline_in": 0,
                "timeline_out": 206_400,
                "duration": 206_400,
                "raw_aux_offset": 19_936_800,
                "main_start": 19_936_800,
                "aux_start": 19_826_400,
                "desired_aux_start": 19_826_685,
                "canonical_delta": -112_515,
                "serialized_delta": -112_800,
                "source_error": -285,
                "canonical_displacement": 2_115,
                "main_offset": 0,
                "aux_offset": 19_939_200,
                "sync_error": -285,
            },
        ),
        (
            FCPXML_FORENSIC_CASES[1],
            {
                "sequence": 278_400,
                "timeline_in": 206_400,
                "timeline_out": 278_400,
                "duration": 72_000,
                "raw_aux_offset": 20_143_200,
                "main_start": 20_143_200,
                "aux_start": 20_032_800,
                "desired_aux_start": 20_033_085,
                "canonical_delta": -112_515,
                "serialized_delta": -112_800,
                "source_error": -285,
                "canonical_displacement": 2_115,
                "main_offset": 206_400,
                "aux_offset": 20_145_600,
                "sync_error": -285,
            },
        ),
        (
            FCPXML_FORENSIC_CASES[2],
            {
                "sequence": 34_022_400,
                "timeline_in": 33_859_200,
                "timeline_out": 34_022_400,
                "duration": 163_200,
                "raw_aux_offset": 88_310_400,
                "main_start": 88_310_400,
                "aux_start": 88_358_400,
                "desired_aux_start": 88_357_380,
                "canonical_delta": 46_980,
                "serialized_delta": 48_000,
                "source_error": 1_020,
                "canonical_displacement": 1_020,
                "main_offset": 33_859_200,
                "aux_offset": 88_310_400,
                "sync_error": 1_020,
            },
        ),
    ],
    ids=lambda value: value.case_id if isinstance(value, FcpxmlForensicCase) else None,
)
def test_fcpxml_forensic_cases_use_one_target_grid_projection(
    tmp_path: Path,
    case: FcpxmlForensicCase,
    expected: dict[str, int],
) -> None:
    timeline = build_fcpxml_forensic_timeline(tmp_path, case)
    before = timeline.to_dict()
    projection = project_fcpxml_timing(timeline)
    main_clip = timeline.main_track.clips[-1]
    aux_clip = timeline.auxiliary_tracks[0].clips[-1]
    main_timing = projection.for_clip(main_clip)
    aux_timing = projection.for_clip(aux_clip)

    assert projection.sequence_duration_ticks == expected["sequence"]
    assert (main_timing.timeline_in_ticks, main_timing.timeline_out_ticks) == (
        expected["timeline_in"],
        expected["timeline_out"],
    )
    assert aux_timing.timeline_in_ticks == main_timing.timeline_in_ticks
    assert aux_timing.timeline_out_ticks == main_timing.timeline_out_ticks
    assert main_timing.duration_ticks == aux_timing.duration_ticks == expected["duration"]
    assert main_timing.start_ticks == expected["main_start"]
    assert aux_timing.start_ticks == expected["aux_start"]
    assert (
        main_timing.start_ticks + aux_timing.timeline_in_ticks - main_timing.timeline_in_ticks
        == expected["raw_aux_offset"]
    )
    assert aux_timing.start_ticks - aux_clip.source_in_ticks == expected["canonical_displacement"]
    assert (
        aux_timing.start_ticks - aux_timing.source_start_delta_ticks
        == expected["desired_aux_start"]
    )
    assert aux_timing.source_start_delta_ticks == expected["source_error"]
    assert aux_timing.canonical_alignment_delta_ticks == expected["canonical_delta"]
    assert aux_timing.serialized_alignment_delta_ticks == expected["serialized_delta"]
    assert main_timing.offset_ticks == expected["main_offset"]
    assert aux_timing.offset_ticks == expected["aux_offset"]
    assert projection.max_source_start_quantization_error_ticks == abs(expected["source_error"])
    assert projection.max_sync_error_ticks == abs(expected["sync_error"])
    assert (
        projection.max_source_start_quantization_error_ticks
        <= case.source_rate.ticks_per_frame // 2
    )
    assert projection.max_sync_error_ticks * 2 <= case.source_rate.ticks_per_frame
    assert timeline.to_dict() == before

    value = write_fcpxml(timeline)
    root = ET.fromstring(value)
    sequence = root.find("./library/event/project/sequence")
    spine = root.find("./library/event/project/sequence/spine")
    assert sequence is not None and spine is not None
    assert _parse_seconds(sequence.attrib["duration"]) == expected["sequence"]
    main_element = spine.findall("asset-clip")[-1]
    aux_element = main_element.findall("asset-clip")[-1]
    assert _parse_seconds(main_element.attrib["offset"]) == expected["main_offset"]
    assert _parse_seconds(main_element.attrib["start"]) == expected["main_start"]
    assert _parse_seconds(main_element.attrib["duration"]) == expected["duration"]
    assert _parse_seconds(aux_element.attrib["offset"]) == expected["aux_offset"]
    assert _parse_seconds(aux_element.attrib["start"]) == expected["aux_start"]
    assert _parse_seconds(aux_element.attrib["duration"]) == expected["duration"]

    source_frame_ticks = case.source_rate.ticks_per_frame
    project_frame_ticks = case.project_rate.ticks_per_frame
    for timing in projection.clip_timings:
        assert timing.timeline_in_ticks % project_frame_ticks == 0
        assert timing.timeline_out_ticks % project_frame_ticks == 0
        assert timing.duration_ticks % project_frame_ticks == 0
        assert timing.start_ticks % source_frame_ticks == 0
        assert (timing.start_ticks + timing.duration_ticks) % source_frame_ticks == 0
        assert timing.offset_ticks % project_frame_ticks == 0
        assert timing.offset_ticks not in {19_936_800, 20_143_200, 21_093_600}
        if timing.sync_error_ticks is not None:
            assert abs(timing.sync_error_ticks) * 2 <= source_frame_ticks
    assert (
        abs(projection.sequence_duration_ticks - timeline.duration_ticks)
        <= project_frame_ticks // 2
    )

    assets = {asset.attrib["id"]: asset for asset in root.findall("./resources/asset")}
    assert main_element.attrib["format"] == assets[main_element.attrib["ref"]].attrib["format"]
    assert aux_element.attrib["format"] == assets[aux_element.attrib["ref"]].attrib["format"]
    assert b"conform-rate" not in value
    assert b"timeMap" not in value
    assert b"mc-clip" not in value


def test_fcpxml_synthetic_8789_offset_derivative_uses_project_grid_and_preserves_sync(
    tmp_path: Path,
) -> None:
    timeline = build_fcpxml_8789_offset_derivative_timeline(tmp_path)
    projection = project_fcpxml_timing(timeline)
    main = timeline.main_track.clips[-1]
    child = timeline.auxiliary_tracks[0].clips[-1]
    main_timing = projection.for_clip(main)
    child_timing = projection.for_clip(child)

    assert main_timing.start_ticks == 21_093_600
    assert (
        main_timing.start_ticks + child_timing.timeline_in_ticks - main_timing.timeline_in_ticks
        == 21_093_600
    )
    assert child_timing.offset_ticks == 21_096_000
    assert child_timing.offset_ticks % timeline.frame_rate.ticks_per_frame == 0
    assert child_timing.offset_ticks != 21_093_600
    assert child_timing.offset_ticks not in {19_936_800, 20_143_200, 21_093_600}
    assert child_timing.start_ticks == 20_983_200
    assert child_timing.start_ticks - child_timing.source_start_delta_ticks == 20_983_485
    assert child_timing.source_start_delta_ticks == -285
    assert child_timing.canonical_alignment_delta_ticks == -112_515
    assert child_timing.serialized_alignment_delta_ticks == -112_800
    assert child_timing.sync_error_ticks == -285
    assert abs(child_timing.sync_error_ticks) * 2 <= 2_400

    root = ET.fromstring(write_fcpxml(timeline))
    child_element = root.find("./library/event/project/sequence/spine/asset-clip/asset-clip")
    assert child_element is not None
    assert _parse_seconds(child_element.attrib["offset"]) == 21_096_000
    assert _parse_seconds(child_element.attrib["start"]) == 20_983_200


def test_fcpxml_many_clip_projection_has_no_per_clip_duration_drift(tmp_path: Path) -> None:
    timeline = build_many_clip_timeline(tmp_path)
    projection = project_fcpxml_timing(timeline)
    main_timings = tuple(timing for timing in projection.clip_timings if timing.track_id == "main")
    auxiliary_timings = tuple(
        timing for timing in projection.clip_timings if timing.track_id == "aux_aux1"
    )
    canonical_end = 159 * 73_800
    independent_rounding_end = 159 * frame_boundary_ticks(73_800, RationalRate(25, 1))
    project_frame_ticks = timeline.frame_rate.ticks_per_frame
    source_frame_ticks = timeline.main_track.clips[0].source_nominal_frame_rate.ticks_per_frame

    assert timeline.duration_ticks == canonical_end
    assert len(main_timings) == 159
    assert len(auxiliary_timings) == 159
    assert len(projection.clip_timings) == 318
    assert projection.sequence_duration_ticks == frame_boundary_ticks(
        canonical_end, RationalRate(25, 1)
    )
    assert (
        sum(timing.duration_ticks for timing in main_timings) == projection.sequence_duration_ticks
    )
    assert (
        sum(timing.duration_ticks for timing in auxiliary_timings)
        == projection.sequence_duration_ticks
    )
    assert projection.sequence_duration_ticks != independent_rounding_end
    assert projection.sequence_duration_ticks == 11_736_000
    assert independent_rounding_end == 11_448_000
    assert projection.max_source_start_quantization_error_ticks == 0
    assert projection.max_sync_error_ticks == 0
    for timing in projection.clip_timings:
        assert timing.offset_ticks % project_frame_ticks == 0
        assert timing.duration_ticks % project_frame_ticks == 0
        assert timing.start_ticks % source_frame_ticks == 0
        assert (timing.start_ticks + timing.duration_ticks) % source_frame_ticks == 0
        if timing.track_id == "aux_aux1":
            assert timing.sync_error_ticks is not None
            assert abs(timing.sync_error_ticks) * 2 <= source_frame_ticks

    root = ET.fromstring(write_fcpxml(timeline))
    sequence = root.find("./library/event/project/sequence")
    spine = root.find("./library/event/project/sequence/spine")
    assert sequence is not None and spine is not None
    assert _parse_seconds(sequence.attrib["duration"]) == projection.sequence_duration_ticks
    main_elements = spine.findall("asset-clip")
    auxiliary_elements = [
        child for main_element in main_elements for child in main_element.findall("asset-clip")
    ]
    assert len(main_elements) == 159
    assert len(auxiliary_elements) == 159
    for element in (*main_elements, *auxiliary_elements):
        offset = _parse_seconds(element.attrib["offset"])
        start = _parse_seconds(element.attrib["start"])
        duration = _parse_seconds(element.attrib["duration"])
        assert offset % project_frame_ticks == 0
        assert duration % project_frame_ticks == 0
        assert start % source_frame_ticks == 0
        assert (start + duration) % source_frame_ticks == 0


def test_fcpxml_connected_child_source_grid_half_up_tie_is_inclusive(
    tmp_path: Path,
) -> None:
    base = build_fcpxml_forensic_timeline(tmp_path, FCPXML_FORENSIC_CASES[0])
    child = base.auxiliary_tracks[0].clips[-1]
    tied_child = replace(
        child,
        source_in_ticks=19_825_200,
        source_out_ticks=20_031_600,
    )
    timeline = replace(
        base,
        tracks=(
            base.main_track,
            replace(base.auxiliary_tracks[0], items=(tied_child,)),
        ),
    )
    projection = project_fcpxml_timing(timeline)
    timing = projection.for_clip(tied_child)

    assert timing.offset_ticks == 19_939_200
    assert timing.canonical_alignment_delta_ticks == -111_600
    assert timing.start_ticks - timing.source_start_delta_ticks == 19_827_600
    assert timing.start_ticks == 19_828_800
    assert timing.source_start_delta_ticks == 1_200
    assert timing.serialized_alignment_delta_ticks == -110_400
    assert timing.sync_error_ticks == 1_200
    assert abs(timing.sync_error_ticks) * 2 == 2_400
    assert projection.max_sync_error_ticks == 1_200


def test_fcpxml_partial_connected_child_uses_projected_parent_local_offset(
    tmp_path: Path,
) -> None:
    timeline = build_partial_connected_child_timeline(tmp_path)
    projection = project_fcpxml_timing(timeline)
    parent = timeline.main_track.clips[-1]
    child = timeline.auxiliary_tracks[0].clips[-1]
    parent_timing = projection.for_clip(parent)
    timing = projection.for_clip(child)

    assert (timing.timeline_in_ticks, timing.timeline_out_ticks) == (4_800, 206_400)
    assert timing.duration_ticks == 201_600
    assert (
        parent_timing.start_ticks + timing.timeline_in_ticks - parent_timing.timeline_in_ticks
        == 19_941_600
    )
    assert timing.offset_ticks == 19_944_000
    assert timing.start_ticks == 19_944_000
    assert timing.start_ticks - timing.source_start_delta_ticks == 19_943_800
    assert timing.source_start_delta_ticks == 200
    assert timing.canonical_alignment_delta_ticks == -200
    assert timing.serialized_alignment_delta_ticks == 0
    assert timing.sync_error_ticks == 200
    assert projection.max_sync_error_ticks == 200
    assert abs(timing.sync_error_ticks) * 2 <= 2_400

    root = ET.fromstring(write_fcpxml(timeline))
    child_element = root.find("./library/event/project/sequence/spine/asset-clip/asset-clip")
    assert child_element is not None
    assert _parse_seconds(child_element.attrib["offset"]) == timing.offset_ticks
    assert _parse_seconds(child_element.attrib["duration"]) == timing.duration_ticks


def test_fcpxml_rejects_negative_paired_child_start(tmp_path: Path) -> None:
    base = build_fcpxml_forensic_timeline(tmp_path, FCPXML_FORENSIC_CASES[0])
    parent = base.main_track.clips[-1]
    child = base.auxiliary_tracks[0].clips[-1]
    adjusted_parent = replace(parent, source_in_ticks=1, source_out_ticks=206_401)
    adjusted_child = replace(child, source_in_ticks=0, source_out_ticks=206_400)
    adjusted = replace(
        base,
        tracks=(
            replace(base.main_track, items=(adjusted_parent,)),
            replace(base.auxiliary_tracks[0], items=(adjusted_child,)),
        ),
    )

    with pytest.raises(NleHandoffError, match="desired serialized source start"):
        project_fcpxml_timing(adjusted)


def test_fcpxml_paired_connected_start_overflow_fails_closed(tmp_path: Path) -> None:
    case = FCPXML_FORENSIC_CASES[0]
    timeline = build_fcpxml_forensic_timeline(tmp_path, case)
    child = timeline.auxiliary_tracks[0].clips[-1]
    projected_duration = frame_boundary_ticks(timeline.duration_ticks, timeline.frame_rate)
    independently_quantized_start = frame_boundary_ticks(child.source_in_ticks, case.source_rate)
    paired_start = 19_826_400
    constrained_source_duration = 20_031_600

    assert independently_quantized_start + projected_duration <= constrained_source_duration
    assert paired_start + projected_duration > constrained_source_duration

    auxiliary_track = timeline.auxiliary_tracks[0]
    constrained_items = tuple(
        replace(item, source_duration_ticks=constrained_source_duration)
        for item in auxiliary_track.items
    )
    constrained = replace(
        timeline,
        tracks=(timeline.main_track, replace(auxiliary_track, items=constrained_items)),
    )

    with pytest.raises(NleHandoffError, match="exceeds original Source"):
        write_fcpxml(constrained)


def test_fcpxml_rejects_project_duration_not_on_source_grid(tmp_path: Path) -> None:
    base = build_mixed_rate_timeline(
        tmp_path,
        project_rate=RationalRate(25, 1),
        source_rate=RationalRate(50, 1),
    )
    clip = base.main_track.clips[0]
    one_project_frame = 5_000
    invalid_clip = replace(
        clip,
        source_out_ticks=clip.source_in_ticks + one_project_frame,
        timeline_in_ticks=0,
        timeline_out_ticks=one_project_frame,
    )
    timeline = replace(
        base,
        frame_rate=RationalRate(24, 1),
        duration_ticks=one_project_frame,
        tracks=(replace(base.main_track, items=(invalid_clip,)),),
    )

    with pytest.raises(NleHandoffError, match="source frame grid"):
        write_fcpxml(timeline)


def test_fcpxml_source_range_overrun_fails_closed(tmp_path: Path) -> None:
    timeline = build_fcpxml_forensic_timeline(tmp_path, FCPXML_FORENSIC_CASES[2])
    auxiliary_track = timeline.auxiliary_tracks[0]
    constrained_items = tuple(
        replace(item, source_duration_ticks=88_520_000) for item in auxiliary_track.items
    )
    constrained = replace(
        timeline,
        tracks=(timeline.main_track, replace(auxiliary_track, items=constrained_items)),
    )

    with pytest.raises(NleHandoffError, match="exceeds original Source"):
        write_fcpxml(constrained)
