from __future__ import annotations

from roughcut.domain.media import SourceProbe
from roughcut.domain.time import MediaTime, RationalRate, SourceRange


def test_supported_frame_rates_are_exact_integer_ticks() -> None:
    assert RationalRate(25, 1).ticks_per_frame == 4_800
    assert RationalRate(30, 1).ticks_per_frame == 4_000
    rate = RationalRate(30_000, 1_001)
    assert rate.ticks_per_frame == 4_004
    assert rate.frames_to_ticks(30_000) == MediaTime(120_120_000)


def test_source_ranges_are_half_open_and_non_empty() -> None:
    source_range = SourceRange(MediaTime(0), MediaTime(120))

    assert source_range.duration == MediaTime(120)
    try:
        SourceRange(MediaTime(120), MediaTime(120))
    except ValueError as error:
        assert "[start, end)" in str(error)
    else:
        raise AssertionError("empty source ranges must be rejected")


def test_source_time_normalizes_from_the_first_effective_picture() -> None:
    probe = SourceProbe(
        container_start=MediaTime(10_800),
        first_content_time=MediaTime(12_000),
        content_duration=MediaTime(120_000),
    )

    assert probe.container_start == MediaTime(10_800)
    assert probe.normalize_container_time(MediaTime(12_000)) == MediaTime(0)
    assert probe.container_time_for_source(MediaTime(0)) == MediaTime(12_000)
