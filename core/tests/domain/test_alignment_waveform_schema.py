from __future__ import annotations

import pytest

from roughcut.domain.alignment import (
    ALIGNMENT_MAPPING_MODEL,
    ALIGNMENT_TICKS_PER_SECOND,
    WAVEFORM_ALGORITHM_NAME,
    WAVEFORM_ALGORITHM_UPSTREAM,
    WAVEFORM_ALGORITHM_VERSION,
    WAVEFORM_PROFILE_NAME,
    WAVEFORM_PROFILE_VERSION,
    AlignmentAlgorithm,
    AlignmentCamera,
    AlignmentCameraGroup,
    AlignmentError,
    AlignmentInterval,
    AlignmentSourceBasis,
    AlignmentSourceFingerprint,
    AlignmentSummary,
    AlignmentVerificationProfile,
    MulticamAlignmentArtifact,
)

DURATION = 4_320_000


def _waveform_algorithm() -> AlignmentAlgorithm:
    return AlignmentAlgorithm(
        name=WAVEFORM_ALGORITHM_NAME,
        version=WAVEFORM_ALGORITHM_VERSION,
        upstream_commit=WAVEFORM_ALGORITHM_UPSTREAM,
        accuracy=None,
        num_processors=None,
        mapping_model=ALIGNMENT_MAPPING_MODEL,
        ticks_per_second=ALIGNMENT_TICKS_PER_SECOND,
        verification_profile=AlignmentVerificationProfile(
            name=WAVEFORM_PROFILE_NAME,
            version=WAVEFORM_PROFILE_VERSION,
        ),
    )


def _evidence(
    *, family: str = "waveform", code: str = "fixed_offset_verified"
) -> dict[str, object]:
    if family == "audalign":
        return {
            "code": "fixed_offset_verified",
            "raw_candidate_count": 1,
            "matching_fingerprint_counts": [1],
            "verification_window_count": 3,
            "verification_profile": {
                "name": "roughcut_audalign_fixed_offset",
                "version": 1,
            },
            "max_local_offset_error_ticks": 0,
        }
    return {
        "code": code,
        "coarse_peak": {"lag_ticks": 0, "score": "1"},
        "coarse_runner_up": {"lag_ticks": 360_000, "score": "0"},
        "coarse_peak_runner_up_separation": "1",
        "refined_offset_ticks": 0,
        "refined_correlation": "1",
        "verification_windows": [
            {"b_ticks": 0, "local_error_ticks": 0, "correlation": "1"},
            {"b_ticks": 0, "local_error_ticks": 0, "correlation": "1"},
        ],
        "verification_window_count": 3,
        "verification_profile": {
            "name": WAVEFORM_PROFILE_NAME,
            "version": WAVEFORM_PROFILE_VERSION,
        },
        "max_local_offset_error_ticks": 0,
        "conflicting_b_ticks": [],
    }


def _waveform_artifact() -> MulticamAlignmentArtifact:
    return MulticamAlignmentArtifact(
        alignment_id="aln_waveform",
        project_id="project_fixture",
        producer_operation_id="op_00000000000040008000000000000120",
        created_at="2026-08-21T00:00:00.000000Z",
        request_hash="a" * 64,
        input_hash="b" * 64,
        algorithm=_waveform_algorithm(),
        main_camera=AlignmentCameraGroup("main", ("main_1",)),
        auxiliary_cameras=(
            AlignmentCamera(
                camera_id="aux-1",
                ordered_source_ids=("aux_1",),
                status="complete",
                mapped_ticks=DURATION,
                missing_ticks=0,
                uncertain_ticks=0,
                conflict_ticks=0,
                errors=(),
            ),
        ),
        source_basis=(
            AlignmentSourceBasis(
                camera_id="aux-1",
                source_id="aux_1",
                fingerprint=AlignmentSourceFingerprint(
                    size=2, mtime_ns=2, sha256_head_tail="b" * 64
                ),
                duration_ticks=DURATION,
            ),
            AlignmentSourceBasis(
                camera_id="main",
                source_id="main_1",
                fingerprint=AlignmentSourceFingerprint(
                    size=1, mtime_ns=1, sha256_head_tail="a" * 64
                ),
                duration_ticks=DURATION,
            ),
        ),
        intervals=(
            AlignmentInterval(
                interval_id="ali_waveform_1",
                auxiliary_camera_id="aux-1",
                classification="mapped",
                main={"source_id": "main_1", "start_ticks": 0, "end_ticks": DURATION},
                auxiliary={"source_id": "aux_1", "start_ticks": 0, "end_ticks": DURATION},
                evidence=_evidence(),
            ),
        ),
        summary=AlignmentSummary(DURATION, 1, DURATION, 0, 0, 0),
    )


def test_waveform_artifact_roundtrips_and_keeps_family_identity() -> None:
    artifact = _waveform_artifact()
    parsed = MulticamAlignmentArtifact.from_dict(artifact.to_dict())
    assert parsed == artifact
    assert parsed.algorithm.name == WAVEFORM_ALGORITHM_NAME
    assert parsed.intervals[0].evidence["verification_profile"] == {
        "name": WAVEFORM_PROFILE_NAME,
        "version": WAVEFORM_PROFILE_VERSION,
    }
    assert "matching_fingerprint_counts" not in parsed.intervals[0].evidence


def test_algorithm_and_evidence_families_cannot_be_crossed() -> None:
    with pytest.raises(AlignmentError):
        AlignmentAlgorithm(
            name=WAVEFORM_ALGORITHM_NAME,
            version=WAVEFORM_ALGORITHM_VERSION,
            upstream_commit=WAVEFORM_ALGORITHM_UPSTREAM,
            accuracy=None,
            num_processors=None,
            mapping_model=ALIGNMENT_MAPPING_MODEL,
            ticks_per_second=ALIGNMENT_TICKS_PER_SECOND,
            verification_profile=AlignmentVerificationProfile(
                name="roughcut_audalign_fixed_offset", version=1
            ),
        )
    crossed = _evidence(family="audalign")
    crossed["verification_profile"] = {
        "name": WAVEFORM_PROFILE_NAME,
        "version": WAVEFORM_PROFILE_VERSION,
    }
    with pytest.raises(AlignmentError):
        AlignmentInterval(
            interval_id="ali_cross_family",
            auxiliary_camera_id="aux-1",
            classification="mapped",
            main={"source_id": "main_1", "start_ticks": 0, "end_ticks": DURATION},
            auxiliary={"source_id": "aux_1", "start_ticks": 0, "end_ticks": DURATION},
            evidence=crossed,
        )


def test_waveform_algorithm_union_has_no_fake_audalign_semantics() -> None:
    """The waveform family serializes a closed six-field shape: it never
    carries Audalign accuracy/num_processors semantics, and its upstream
    literal is the explicit no-external-upstream marker."""
    algorithm = _waveform_algorithm()
    payload = algorithm.to_dict()
    assert set(payload) == {
        "name",
        "version",
        "upstream_commit",
        "mapping_model",
        "ticks_per_second",
        "verification_profile",
    }
    assert payload["upstream_commit"] == "roughcut-core"
    parsed = AlignmentAlgorithm.from_dict(payload)
    assert parsed == algorithm
    # an injected accuracy/num_processors pair is rejected on readback
    forged = dict(payload)
    forged["accuracy"] = 2
    forged["num_processors"] = 1
    with pytest.raises(AlignmentError):
        AlignmentAlgorithm.from_dict(forged)
    # fake Audalign semantics on the waveform name are rejected at build time
    with pytest.raises(AlignmentError):
        AlignmentAlgorithm(
            name=WAVEFORM_ALGORITHM_NAME,
            version=WAVEFORM_ALGORITHM_VERSION,
            upstream_commit=WAVEFORM_ALGORITHM_UPSTREAM,
            accuracy=2,
            num_processors=1,
            mapping_model=ALIGNMENT_MAPPING_MODEL,
            ticks_per_second=ALIGNMENT_TICKS_PER_SECOND,
            verification_profile=AlignmentVerificationProfile(
                name=WAVEFORM_PROFILE_NAME,
                version=WAVEFORM_PROFILE_VERSION,
            ),
        )


def test_historical_audalign_algorithm_still_roundtrips() -> None:
    historical = {
        "name": "audalign_fingerprint",
        "version": "1.3.1",
        "upstream_commit": "d5955ae8a85b1cd480dadd005c3f88986f4ebbef",
        "accuracy": 2,
        "num_processors": 1,
        "mapping_model": ALIGNMENT_MAPPING_MODEL,
        "ticks_per_second": ALIGNMENT_TICKS_PER_SECOND,
        "verification_profile": {
            "name": "roughcut_audalign_fixed_offset",
            "version": 2,
        },
    }
    parsed = AlignmentAlgorithm.from_dict(historical)
    assert parsed.accuracy == 2
    assert parsed.num_processors == 1
    assert parsed.to_dict() == historical
    # dropping a required Audalign field stays rejected
    incomplete = {key: value for key, value in historical.items() if key != "accuracy"}
    with pytest.raises(AlignmentError):
        AlignmentAlgorithm.from_dict(incomplete)


def _empty_waveform_evidence() -> dict[str, object]:
    return {
        "code": "no_candidate",
        "coarse_peak": None,
        "coarse_runner_up": None,
        "coarse_peak_runner_up_separation": None,
        "refined_offset_ticks": None,
        "refined_correlation": None,
        "verification_windows": [],
        "verification_window_count": 0,
        "verification_profile": {
            "name": WAVEFORM_PROFILE_NAME,
            "version": WAVEFORM_PROFILE_VERSION,
        },
        "max_local_offset_error_ticks": None,
        "conflicting_b_ticks": [],
    }


def _uncertain_interval(evidence: dict[str, object]) -> AlignmentInterval:
    return AlignmentInterval(
        interval_id="ali_uncertain_counter",
        auxiliary_camera_id="aux-1",
        classification="uncertain",
        main={"source_id": "main_1", "start_ticks": 0, "end_ticks": DURATION},
        auxiliary=None,
        evidence=evidence,
    )


def test_no_candidate_and_failed_must_be_completely_empty() -> None:
    base = _empty_waveform_evidence()
    interval = _uncertain_interval(dict(base))
    assert interval.evidence["code"] == "no_candidate"
    contradictions = [
        ("refined_offset_ticks", 120_000),
        ("coarse_peak", {"lag_ticks": 0, "score": "0.5"}),
        ("max_local_offset_error_ticks", 0),
    ]
    for field, value in contradictions:
        forged = dict(base)
        forged[field] = value
        if field == "refined_offset_ticks":
            forged["refined_correlation"] = "0.9"
            forged["verification_window_count"] = 1
        with pytest.raises(AlignmentError):
            _uncertain_interval(forged)
        with pytest.raises(AlignmentError):
            AlignmentInterval(
                interval_id="ali_missing_counter",
                auxiliary_camera_id="aux-1",
                classification="missing",
                main={
                    "source_id": "main_1",
                    "start_ticks": 0,
                    "end_ticks": DURATION,
                },
                auxiliary=None,
                evidence={**forged},
            )
    failed = dict(base)
    failed["code"] = "failed"
    assert _uncertain_interval(failed).evidence["code"] == "failed"
    failed_with_window = dict(failed)
    failed_with_window["verification_windows"] = [
        {"b_ticks": 0, "local_error_ticks": 0, "correlation": "1"}
    ]
    failed_with_window["verification_window_count"] = 1
    with pytest.raises(AlignmentError):
        _uncertain_interval(failed_with_window)


def test_uncertain_codes_carry_only_their_stage_facts() -> None:
    coarse_base = {
        **_empty_waveform_evidence(),
        "code": "coarse_ambiguous",
        "coarse_peak": {"lag_ticks": 0, "score": "0.5"},
        "coarse_peak_runner_up_separation": "0",
    }
    assert _uncertain_interval(coarse_base) is not None
    # coarse_ambiguous must not carry refinement facts
    with pytest.raises(AlignmentError):
        _uncertain_interval(
            {
                **coarse_base,
                "refined_offset_ticks": 0,
                "refined_correlation": "0.9",
                "verification_window_count": 1,
            }
        )
    # refinement_failed may carry the refined pair only with count 1
    refinement = {
        **coarse_base,
        "code": "refinement_failed",
        "refined_offset_ticks": 0,
        "refined_correlation": "0.2",
        "verification_window_count": 1,
    }
    assert _uncertain_interval(refinement) is not None
    with pytest.raises(AlignmentError):
        _uncertain_interval({**refinement, "verification_window_count": 0})
    with pytest.raises(AlignmentError):
        _uncertain_interval(
            {
                **refinement,
                "conflicting_b_ticks": [0],
            }
        )
    # verification_failed requires the refined offset and at least one window
    verification = {
        **coarse_base,
        "code": "verification_failed",
        "refined_offset_ticks": 0,
        "refined_correlation": "0.9",
        "verification_windows": [
            {"b_ticks": 0, "local_error_ticks": 0, "correlation": "0.2"}
        ],
        "verification_window_count": 2,
        "max_local_offset_error_ticks": 0,
    }
    assert _uncertain_interval(verification) is not None
    with pytest.raises(AlignmentError):
        _uncertain_interval({**verification, "refined_offset_ticks": None})


def test_waveform_score_and_separation_ranges_are_closed() -> None:
    good = {
        **_empty_waveform_evidence(),
        "code": "coarse_ambiguous",
        "coarse_peak": {"lag_ticks": 0, "score": "1"},
        "coarse_peak_runner_up_separation": "0",
    }
    assert _uncertain_interval(good) is not None
    for score in ("1.01", "-1.5"):
        with pytest.raises(AlignmentError):
            _uncertain_interval(
                {**good, "coarse_peak": {"lag_ticks": 0, "score": score}}
            )
    paired_base = {
        **good,
        "coarse_runner_up": {"lag_ticks": -360_000, "score": "-1"},
        "coarse_peak_runner_up_separation": "2",
    }
    assert _uncertain_interval(paired_base) is not None
    with pytest.raises(AlignmentError):
        _uncertain_interval(
            {**paired_base, "coarse_peak_runner_up_separation": "-0.1"}
        )
    with pytest.raises(AlignmentError):
        _uncertain_interval(
            {**paired_base, "coarse_peak_runner_up_separation": "2.1"}
        )
    # a separation without a runner-up candidate must be exactly zero
    with pytest.raises(AlignmentError):
        _uncertain_interval({**good, "coarse_peak_runner_up_separation": "0.5"})
    # a real runner-up pair stays readable
    assert _uncertain_interval(paired_base) is not None


def test_single_candidate_coarse_evidence_roundtrips() -> None:
    """The producer's legitimate runner-up-less coarse evidence stays valid."""
    single = {
        **_empty_waveform_evidence(),
        "code": "coarse_ambiguous",
        "coarse_peak": {"lag_ticks": 240_000, "score": "0.42"},
        "coarse_peak_runner_up_separation": "0",
    }
    interval = _uncertain_interval(single)
    parsed = AlignmentInterval.from_dict(interval.to_dict())
    assert parsed == interval
