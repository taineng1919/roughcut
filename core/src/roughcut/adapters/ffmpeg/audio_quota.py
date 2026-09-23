"""Shared AAC sample quota contract for formal and parallel render verification."""

from __future__ import annotations

from roughcut.domain.media_quota import AAC_SAMPLE_QUOTA_TOLERANCE

__all__ = ("AAC_SAMPLE_QUOTA_TOLERANCE", "audio_sample_quota_matches")


def audio_sample_quota_matches(*, codec: object, expected: int, actual: int | None) -> bool:
    """AAC pads whole frames at the container boundary; one frame of drift is allowed."""
    if actual is None:
        return False
    if codec != "aac":
        return actual == expected
    return abs(actual - expected) <= AAC_SAMPLE_QUOTA_TOLERANCE
