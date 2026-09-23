from __future__ import annotations

import pytest

from roughcut.domain.alignment import (
    AUDALIGN_CORRELATION_PROFILE_NAME,
    AUDALIGN_CORRELATION_PROFILE_VERSION,
    AUDALIGN_CORRELATION_RECOGNIZER,
    AUDALIGN_VERSION,
    AlignmentError,
    AlignmentInterval,
    AlignmentVerificationProfile,
    project_alignment_intervals,
)


def _evidence(code: str) -> dict[str, object]:
    profile = AlignmentVerificationProfile(
        AUDALIGN_CORRELATION_PROFILE_NAME,
        AUDALIGN_CORRELATION_PROFILE_VERSION,
    )
    if code == "no_candidate":
        return {
            "code": code,
            "provider": "audalign",
            "provider_version": AUDALIGN_VERSION,
            "recognizer": AUDALIGN_CORRELATION_RECOGNIZER,
            "probe_records": [],
            "support_probe_count": 0,
            "cluster_spread_ticks": None,
            "representative_b_ticks": None,
            "conflicting_b_ticks": [],
            "verification_profile": profile.to_dict(),
        }
    if code == "fixed_offset_conflict":
        probes = ((20, "0", 0), (50, "15", 0), (80, "30.2", 24_000))
    else:
        probes = ((20, "0", 0), (50, "15", 0), (80, "30", 0))
    return {
        "code": code,
        "provider": "audalign",
        "provider_version": AUDALIGN_VERSION,
        "recognizer": AUDALIGN_CORRELATION_RECOGNIZER,
        "probe_records": [
            {
                "percentage": percentage,
                "auxiliary_start_ticks": index * 1_800_000,
                "auxiliary_end_ticks": (index + 1) * 1_800_000,
                "native_offset_seconds": native,
                "derived_b_ticks": value,
            }
            for index, (percentage, native, value) in enumerate(probes)
        ],
        "support_probe_count": 2 if code == "fixed_offset_conflict" else 3,
        "cluster_spread_ticks": 0,
        "representative_b_ticks": 0,
        "conflicting_b_ticks": [0, 24_000] if code == "fixed_offset_conflict" else [],
        "verification_profile": profile.to_dict(),
    }


def _interval(
    interval_id: str,
    classification: str,
    start: int,
    end: int,
    *,
    auxiliary_start: int | None = None,
) -> AlignmentInterval:
    return AlignmentInterval(
        interval_id=interval_id,
        auxiliary_camera_id="aux",
        classification=classification,
        main={"source_id": "main_source", "start_ticks": start, "end_ticks": end},
        auxiliary=(
            None
            if auxiliary_start is None
            else {
                "source_id": "aux_source",
                "start_ticks": auxiliary_start,
                "end_ticks": auxiliary_start + end - start,
            }
        ),
        evidence=_evidence(
            "fixed_offset_verified"
            if classification == "mapped"
            else "fixed_offset_conflict"
            if classification == "conflict"
            else "no_candidate"
        ),
    )


def test_projection_reuses_exact_mapping_for_all_classifications_and_truncates_clip() -> None:
    intervals = (
        _interval("mapped", "mapped", 0, 4 * 120_000, auxiliary_start=10 * 120_000),
        _interval("uncertain", "uncertain", 4 * 120_000, 6 * 120_000),
        _interval("conflict", "conflict", 6 * 120_000, 8 * 120_000),
        _interval("missing", "missing", 8 * 120_000, 10 * 120_000),
    )

    projected = project_alignment_intervals(
        camera_id="aux",
        intervals=intervals,
        main_source_id="main_source",
        main_start_ticks=2 * 120_000,
        main_end_ticks=9 * 120_000,
        timeline_start_ticks=100,
    )

    assert [item.classification for item in projected] == [
        "mapped",
        "uncertain",
        "conflict",
        "missing",
    ]
    assert [
        (
            item.main_start_ticks,
            item.main_end_ticks,
            item.timeline_start_ticks,
            item.timeline_end_ticks,
        )
        for item in projected
    ] == [
        (2 * 120_000, 4 * 120_000, 100, 2 * 120_000 + 100),
        (4 * 120_000, 6 * 120_000, 2 * 120_000 + 100, 4 * 120_000 + 100),
        (6 * 120_000, 8 * 120_000, 4 * 120_000 + 100, 6 * 120_000 + 100),
        (8 * 120_000, 9 * 120_000, 6 * 120_000 + 100, 7 * 120_000 + 100),
    ]
    assert (projected[0].auxiliary_start_ticks, projected[0].auxiliary_end_ticks) == (
        12 * 120_000,
        14 * 120_000,
    )
    assert all(item.auxiliary_source_id is None for item in projected[1:])


def test_projection_rejects_an_uncovered_alignment_partition() -> None:
    intervals = (
        _interval("first", "mapped", 0, 4 * 120_000, auxiliary_start=0),
        _interval("last", "missing", 6 * 120_000, 10 * 120_000),
    )
    with pytest.raises(AlignmentError, match="gap or overlap"):
        project_alignment_intervals(
            camera_id="aux",
            intervals=intervals,
            main_source_id="main_source",
            main_start_ticks=0,
            main_end_ticks=10 * 120_000,
            timeline_start_ticks=0,
        )
