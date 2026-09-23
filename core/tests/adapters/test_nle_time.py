from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from nle_test_fixtures import FCPXML_FORENSIC_CASES, build_fcpxml_forensic_timeline

from roughcut.adapters.nle_time import (
    Fcp7FrameRange,
    fcp7_frame_range,
    fcp7_rate,
    fcpxml_time,
    frame_boundary_ticks,
    project_fcpxml_timing,
    ticks_to_exact_frame,
    ticks_to_frame_boundary,
)
from roughcut.domain.nle_handoff import NleHandoffError
from roughcut.domain.time import RationalRate


def test_rational_time_is_exact_and_reduced() -> None:
    assert fcpxml_time(60_000) == "1/2s"
    assert fcpxml_time(120_000) == "1s"
    assert fcpxml_time(150_000) == "5/4s"


def test_frame_boundaries_use_absolute_ticks_without_accumulated_rounding() -> None:
    rate = RationalRate(25, 1)
    half_frame = 2_400
    assert [ticks_to_frame_boundary(value, rate) for value in (0, half_frame, 9_600, 14_400)] == [
        0,
        1,
        2,
        3,
    ]
    assert frame_boundary_ticks(73_800, rate) == 72_000
    assert fcp7_frame_range(
        source_in_ticks=rate.frames_to_ticks(1).ticks,
        source_out_ticks=rate.frames_to_ticks(2).ticks,
        timeline_in_ticks=rate.frames_to_ticks(1).ticks,
        timeline_out_ticks=rate.frames_to_ticks(2).ticks,
        source_duration_ticks=rate.frames_to_ticks(25).ticks,
        sequence_rate=rate,
        source_rate=rate,
    ) == Fcp7FrameRange(1, 2, 1, 2, None)


def test_source_boundaries_must_be_exact_source_frames() -> None:
    with pytest.raises(NleHandoffError, match="exact source frame"):
        ticks_to_exact_frame(2_400, RationalRate(25, 1), field="source in point")


def test_fcp7_mixed_rate_preserves_odd_source_frame_with_offset() -> None:
    sequence_rate = RationalRate(25, 1)
    source_rate = RationalRate(50, 1)
    result = fcp7_frame_range(
        source_in_ticks=11 * source_rate.ticks_per_frame,
        source_out_ticks=111 * source_rate.ticks_per_frame,
        timeline_in_ticks=0,
        timeline_out_ticks=50 * sequence_rate.ticks_per_frame,
        source_duration_ticks=400 * source_rate.ticks_per_frame,
        sequence_rate=sequence_rate,
        source_rate=source_rate,
    )
    assert result == Fcp7FrameRange(5, 55, 0, 50, 1)


def test_fcp7_ntsc_mixed_rate_preserves_odd_source_frame_with_offset() -> None:
    sequence_rate = RationalRate(30_000, 1_001)
    source_rate = RationalRate(60_000, 1_001)
    result = fcp7_frame_range(
        source_in_ticks=11 * source_rate.ticks_per_frame,
        source_out_ticks=111 * source_rate.ticks_per_frame,
        timeline_in_ticks=0,
        timeline_out_ticks=50 * sequence_rate.ticks_per_frame,
        source_duration_ticks=400 * source_rate.ticks_per_frame,
        sequence_rate=sequence_rate,
        source_rate=source_rate,
    )
    assert result == Fcp7FrameRange(5, 55, 0, 50, 1)


def test_fcp7_mixed_rate_rejects_fractional_sequence_duration() -> None:
    sequence_rate = RationalRate(25, 1)
    source_rate = RationalRate(50, 1)
    with pytest.raises(NleHandoffError, match="not representable"):
        fcp7_frame_range(
            source_in_ticks=11 * source_rate.ticks_per_frame,
            source_out_ticks=112 * source_rate.ticks_per_frame,
            timeline_in_ticks=0,
            timeline_out_ticks=50 * sequence_rate.ticks_per_frame,
            source_duration_ticks=400 * source_rate.ticks_per_frame,
            sequence_rate=sequence_rate,
            source_rate=source_rate,
        )


def test_fcp7_frame_range_rejects_zero_timeline_frames() -> None:
    rate = RationalRate(25, 1)
    with pytest.raises(NleHandoffError, match="zero frames"):
        fcp7_frame_range(
            source_in_ticks=0,
            source_out_ticks=rate.ticks_per_frame,
            timeline_in_ticks=0,
            timeline_out_ticks=1,
            source_duration_ticks=rate.frames_to_ticks(10).ticks,
            sequence_rate=rate,
            source_rate=rate,
        )


def test_fcpxml_projection_rejects_zero_frame_clip(tmp_path: Path) -> None:
    case = FCPXML_FORENSIC_CASES[0]
    timeline = build_fcpxml_forensic_timeline(tmp_path, case)
    main = timeline.main_track.clips[-1]
    auxiliary = timeline.auxiliary_tracks[0].clips[-1]
    collapsed = replace(
        timeline,
        duration_ticks=1,
        tracks=(
            replace(
                timeline.main_track,
                items=(
                    replace(main, source_out_ticks=main.source_in_ticks + 1, timeline_out_ticks=1),
                ),
            ),
            replace(
                timeline.auxiliary_tracks[0],
                items=(
                    replace(
                        auxiliary,
                        source_out_ticks=auxiliary.source_in_ticks + 1,
                        timeline_out_ticks=1,
                    ),
                ),
            ),
        ),
    )

    with pytest.raises(NleHandoffError, match="zero frames"):
        project_fcpxml_timing(collapsed)


@pytest.mark.parametrize(
    ("rate", "timebase", "ntsc"),
    [
        (RationalRate(25, 1), 25, False),
        (RationalRate(24, 1), 24, False),
        (RationalRate(30000, 1001), 30, True),
        (RationalRate(24000, 1001), 24, True),
    ],
)
def test_fcp7_rate_mapping_is_closed(
    rate: RationalRate,
    timebase: int,
    ntsc: bool,
) -> None:
    assert fcp7_rate(rate).timebase == timebase
    assert fcp7_rate(rate).ntsc is ntsc


def test_fcp7_rate_rejects_unknown_rate() -> None:
    with pytest.raises(NleHandoffError):
        fcp7_rate(RationalRate(100, 3))
