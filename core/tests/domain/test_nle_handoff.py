from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from roughcut.domain.nle_handoff import (
    NleHandoffClip,
    NleHandoffError,
    NleHandoffGap,
    NleHandoffTimeline,
    NleHandoffTrack,
)
from roughcut.domain.time import TICKS_PER_SECOND, RationalRate


SOURCE_FIXTURE = Path(__file__).resolve().parent / "source.mp4"


def _clip(
    logical_id: str = "clip_1",
    *,
    timeline_in: int = 0,
    timeline_out: int = TICKS_PER_SECOND,
    track_id: str = "main",
    camera_id: str = "main",
) -> NleHandoffClip:
    return NleHandoffClip(
        logical_clip_id=logical_id,
        source_id="source_a",
        source_locator=SOURCE_FIXTURE,
        source_duration_ticks=10 * TICKS_PER_SECOND,
        source_display_name="source_a",
        source_width=1920,
        source_height=1080,
        source_nominal_frame_rate=RationalRate(25, 1),
        source_is_vfr=False,
        source_audio_sample_rate=48_000,
        source_in_ticks=0,
        source_out_ticks=timeline_out - timeline_in,
        timeline_in_ticks=timeline_in,
        timeline_out_ticks=timeline_out,
        camera_id=camera_id,
        track_id=track_id,
        media_types=("video", "audio"),
        av_link_id=f"link_{logical_id}",
    )


def test_track_rejects_gaps_or_overlaps_in_item_partition() -> None:
    first = _clip("one", timeline_out=TICKS_PER_SECOND)
    second = _clip("two", timeline_in=2 * TICKS_PER_SECOND, timeline_out=3 * TICKS_PER_SECOND)
    with pytest.raises(NleHandoffError, match="gap or overlap"):
        NleHandoffTrack("main", "main", "main", ("video", "audio"), (first, second))


def test_timeline_rejects_duplicate_tracks_and_non_full_auxiliary_cover() -> None:
    main = _clip("main_clip", track_id="main", camera_id="main")
    duplicate = _clip("duplicate", track_id="main", camera_id="aux")
    with pytest.raises(NleHandoffError, match="duplicate logical tracks"):
        NleHandoffTimeline(
            "project",
            TICKS_PER_SECOND,
            RationalRate(25, 1),
            1920,
            1080,
            48_000,
            TICKS_PER_SECOND,
            (
                NleHandoffTrack("main", "main", "main", ("video", "audio"), (main,)),
                NleHandoffTrack("main", "aux", "auxiliary", ("video", "audio"), (duplicate,)),
            ),
        )


def test_timeline_rejects_duplicate_item_identities() -> None:
    first = _clip("same")
    second = _clip("same", timeline_in=TICKS_PER_SECOND, timeline_out=2 * TICKS_PER_SECOND)
    with pytest.raises(NleHandoffError, match="duplicate timeline item identities"):
        NleHandoffTimeline(
            "project",
            TICKS_PER_SECOND,
            RationalRate(25, 1),
            1920,
            1080,
            48_000,
            2 * TICKS_PER_SECOND,
            (NleHandoffTrack("main", "main", "main", ("video", "audio"), (first, second)),),
        )


def test_gap_has_no_media_fields_and_timeline_is_deterministic() -> None:
    main = _clip("main_clip", track_id="main", camera_id="main")
    gap = NleHandoffGap("gap", 0, TICKS_PER_SECOND, "aux", "aux_track")
    auxiliary = NleHandoffTrack("aux_track", "aux", "auxiliary", ("video", "audio"), (gap,))
    first = NleHandoffTimeline(
        "project",
        TICKS_PER_SECOND,
        RationalRate(25, 1),
        1920,
        1080,
        48_000,
        TICKS_PER_SECOND,
        (NleHandoffTrack("main", "main", "main", ("video", "audio"), (main,)), auxiliary),
    )
    second = NleHandoffTimeline(
        "project",
        TICKS_PER_SECOND,
        RationalRate(25, 1),
        1920,
        1080,
        48_000,
        TICKS_PER_SECOND,
        (NleHandoffTrack("main", "main", "main", ("video", "audio"), (main,)), auxiliary),
    )
    assert first.to_dict() == second.to_dict()
    assert first.auxiliary_tracks[0].gaps == (gap,)
    assert "source_locator" not in gap.to_dict()


def test_timeline_rejects_repeated_source_metadata_mismatch() -> None:
    first = _clip("first")
    second = replace(
        _clip("second", timeline_in=TICKS_PER_SECOND, timeline_out=2 * TICKS_PER_SECOND),
        source_width=3840,
    )
    with pytest.raises(NleHandoffError, match="inconsistent metadata"):
        NleHandoffTimeline(
            "project",
            TICKS_PER_SECOND,
            RationalRate(25, 1),
            1920,
            1080,
            48_000,
            2 * TICKS_PER_SECOND,
            (NleHandoffTrack("main", "main", "main", ("video", "audio"), (first, second)),),
        )


@pytest.mark.parametrize(
    ("field", "expected_message"),
    [
        ("source_width", "without source dimensions"),
        ("source_height", "without source dimensions"),
        ("source_nominal_frame_rate", "without source nominal frame rate"),
    ],
)
def test_video_clip_requires_source_media_metadata(field: str, expected_message: str) -> None:
    with pytest.raises(NleHandoffError, match=expected_message):
        replace(_clip(), **{field: None})


def test_clip_rejects_source_timeline_duration_mismatch() -> None:
    with pytest.raises(NleHandoffError, match="durations differ"):
        NleHandoffClip(
            logical_clip_id="clip_bad",
            source_id="source_a",
            source_locator=SOURCE_FIXTURE,
            source_duration_ticks=10 * TICKS_PER_SECOND,
            source_display_name="source_a",
            source_width=1920,
            source_height=1080,
            source_nominal_frame_rate=RationalRate(25, 1),
            source_is_vfr=False,
            source_audio_sample_rate=48_000,
            source_in_ticks=0,
            source_out_ticks=TICKS_PER_SECOND,
            timeline_in_ticks=0,
            timeline_out_ticks=2 * TICKS_PER_SECOND,
            camera_id="main",
            track_id="main",
            media_types=("video", "audio"),
            av_link_id="link_bad",
        )
