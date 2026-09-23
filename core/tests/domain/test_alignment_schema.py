"""Schema-1 strictness: partition, summary recomputation, and closed errors."""

from __future__ import annotations

import copy
import json

import pytest

from roughcut.domain.alignment import (
    BBC_ALGORITHM_NAME,
    BBC_ALGORITHM_UPSTREAM,
    BBC_ALGORITHM_VERSION,
    BBC_PROFILE_NAME,
    BBC_PROFILE_VERSION,
    BBC_WRITER_PROFILE,
    AlignmentAlgorithm,
    AlignmentCamera,
    AlignmentCameraGroup,
    AlignmentError,
    AlignmentInterval,
    AlignmentPerCameraError,
    AlignmentSourceBasis,
    AlignmentSourceFingerprint,
    AlignmentSummary,
    AlignmentVerificationProfile,
    MulticamAlignmentArtifact,
    alignment_request_projection,
)

DURATION = 14_400_000


def test_bbc_writer_profile_binds_provider_refine_and_strict_verification() -> None:
    assert BBC_WRITER_PROFILE == {
        "name": BBC_PROFILE_NAME,
        "version": BBC_PROFILE_VERSION,
        "algorithm": {
            "name": BBC_ALGORITHM_NAME,
            "version": BBC_ALGORITHM_VERSION,
            "upstream": BBC_ALGORITHM_UPSTREAM,
            "evidence_family": "roughcut_bbc_offset_evidence_v1",
        },
        "provider": "bbc_audio_offset_finder",
        "provider_version": "0.5.5",
        "finder_parameters": "upstream_defaults_no_overrides",
        "roles": {
            "main": "selected_main_source",
            "auxiliary": "selected_auxiliary_source",
        },
        "mapping_model": "fixed_offset_equal_speed",
        "ticks_per_second": 120_000,
        "native_offset_semantics": "main_tick_equals_auxiliary_tick_plus_b",
        "seconds_to_ticks": "decimal_nearest_ties_away_from_zero",
        "short_window_derived_values_per_second": 250,
        "short_window_decode_sample_rate_hz": 8_000,
        "short_window_block_samples": 32,
        "window_ticks": 1_200_000,
        "window_count": 3,
        "maximum_refinement_lag_ticks": 360_000,
        "minimum_correlation": "0.35",
        "maximum_local_error_ticks": 12_000,
        "minimum_verified_overlap_ticks": 4_320_000,
        "verification_geometry": "recomputed_from_refined_b",
        "channel_schedule": "mono_then_left_then_right_only_after_failure",
    }


@pytest.mark.parametrize("name", [[], {}, True, None])
def test_algorithm_identity_rejects_non_string_name_as_alignment_error(
    name: object,
) -> None:
    payload = _algorithm().to_dict()
    payload["name"] = name
    with pytest.raises(AlignmentError) as error:
        AlignmentAlgorithm.from_dict(payload)
    assert error.value.code == "alignment_integrity_error"


def test_algorithm_identity_rejects_extra_fields_as_alignment_error() -> None:
    payload = _algorithm().to_dict()
    payload["extra"] = None
    with pytest.raises(AlignmentError) as error:
        AlignmentAlgorithm.from_dict(payload)
    assert error.value.code == "alignment_integrity_error"


def test_bbc_evidence_is_closed_and_roundtrips_without_coarse_fields() -> None:
    evidence = {
        "code": "fixed_offset_verified",
        "provider": "bbc_audio_offset_finder",
        "provider_version": "0.5.5",
        "channel": "mono",
        "native_offset_seconds": "-0.7",
        "initial_b_ticks": -84_000,
        "standard_score": "12.5",
        "refined_b_ticks": -82_560,
        "refined_correlation": "0.91",
        "refined_geometry": {
            "main_start_ticks": 0,
            "main_end_ticks": DURATION - 82_560,
            "auxiliary_start_ticks": 82_560,
            "auxiliary_end_ticks": DURATION,
        },
        "verification_windows": [
            {
                "label": label,
                "main_start_ticks": start,
                "main_end_ticks": start + 1_200_000,
                "auxiliary_start_ticks": start + 82_560,
                "auxiliary_end_ticks": start + 1_282_560,
                "b_ticks": -82_560,
                "local_error_ticks": 0,
                "correlation": "0.9",
            }
            for label, start in (
                ("opening", 0),
                ("middle", 6_558_720),
                ("ending", 13_117_440),
            )
        ],
        "verification_window_count": 3,
        "verification_profile": {
            "name": BBC_PROFILE_NAME,
            "version": BBC_PROFILE_VERSION,
        },
        "max_local_offset_error_ticks": 0,
        "conflicting_b_ticks": [],
    }
    interval = AlignmentInterval(
        interval_id="ali_bbc",
        auxiliary_camera_id="aux-1",
        classification="mapped",
        main={
            "source_id": "src_main_1",
            "start_ticks": 0,
            "end_ticks": DURATION - 82_560,
        },
        auxiliary={
            "source_id": "src_aux_1",
            "start_ticks": 82_560,
            "end_ticks": DURATION,
        },
        evidence=evidence,
    )
    assert AlignmentInterval.from_dict(interval.to_dict()) == interval
    assert not {
        "coarse_peak",
        "coarse_runner_up",
        "coarse_peak_runner_up_separation",
    } & evidence.keys()
    corrupted = copy.deepcopy(interval.to_dict())
    corrupted["evidence"]["coarse_peak"] = None
    with pytest.raises(AlignmentError):
        AlignmentInterval.from_dict(corrupted)


def _bbc_empty_stage(code: str) -> dict[str, object]:
    return {
        "code": code,
        "provider": "bbc_audio_offset_finder",
        "provider_version": "0.5.5",
        "channel": None,
        "native_offset_seconds": None,
        "initial_b_ticks": None,
        "standard_score": None,
        "refined_b_ticks": None,
        "refined_correlation": None,
        "refined_geometry": None,
        "verification_windows": [],
        "verification_window_count": 0,
        "verification_profile": {
            "name": BBC_PROFILE_NAME,
            "version": BBC_PROFILE_VERSION,
        },
        "max_local_offset_error_ticks": None,
        "conflicting_b_ticks": [],
    }


def _uncertain_bbc_interval(evidence: dict[str, object]) -> AlignmentInterval:
    return AlignmentInterval(
        interval_id="ali_bbc_uncertain",
        auxiliary_camera_id="aux-1",
        classification="uncertain",
        main={"source_id": "src_main_1", "start_ticks": 0, "end_ticks": DURATION},
        auxiliary=None,
        evidence=evidence,
    )


def _bbc_verification_stage(
    code: str,
    correlations: tuple[str, ...],
) -> dict[str, object]:
    starts = (0, 6_600_000, 13_200_000)
    labels = ("opening", "middle", "ending")
    evidence = _bbc_empty_stage(code)
    evidence.update(
        {
            "native_offset_seconds": "0",
            "initial_b_ticks": 0,
            "standard_score": "1",
            "channel": "mono",
            "refined_b_ticks": 0,
            "refined_correlation": "0.9",
            "refined_geometry": {
                "main_start_ticks": 0,
                "main_end_ticks": DURATION,
                "auxiliary_start_ticks": 0,
                "auxiliary_end_ticks": DURATION,
            },
            "verification_windows": [
                {
                    "label": labels[index],
                    "main_start_ticks": starts[index],
                    "main_end_ticks": starts[index] + 1_200_000,
                    "auxiliary_start_ticks": starts[index],
                    "auxiliary_end_ticks": starts[index] + 1_200_000,
                    "b_ticks": 0,
                    "local_error_ticks": 0,
                    "correlation": correlation,
                }
                for index, correlation in enumerate(correlations)
            ],
            "verification_window_count": len(correlations),
            "max_local_offset_error_ticks": 0 if correlations else None,
        }
    )
    return evidence


@pytest.mark.parametrize(
    "code",
    [
        "finder_failed",
        "no_offset",
        "finder_import_failed",
        "finder_ffmpeg_unavailable",
        "finder_decode_failed",
        "finder_provider_failed",
        "finder_result_invalid",
        "finder_insufficient_audio",
    ],
)
def test_bbc_finder_failure_shapes_are_closed(code: str) -> None:
    evidence = _bbc_empty_stage(code)
    assert AlignmentInterval.from_dict(_uncertain_bbc_interval(evidence).to_dict())
    evidence["native_offset_seconds"] = "0"
    evidence["initial_b_ticks"] = 0
    evidence["standard_score"] = "1"
    with pytest.raises(AlignmentError):
        _uncertain_bbc_interval(evidence)


@pytest.mark.parametrize(
    ("code", "refined", "correlation"),
    [
        ("initial_overlap_insufficient", None, None),
        ("refinement_failed", 0, "0.3499"),
        ("refined_overlap_insufficient", 0, "0.35"),
        ("decode_failed", None, None),
    ],
)
def test_bbc_preverification_stage_shapes_roundtrip(
    code: str, refined: int | None, correlation: str | None
) -> None:
    evidence = _bbc_empty_stage(code)
    evidence.update(
        {
            "native_offset_seconds": "0",
            "initial_b_ticks": 0,
            "standard_score": "1",
            "channel": None if code == "initial_overlap_insufficient" else "mono",
            "refined_b_ticks": refined,
            "refined_correlation": correlation,
        }
    )
    assert AlignmentInterval.from_dict(_uncertain_bbc_interval(evidence).to_dict())


@pytest.mark.parametrize(
    ("code", "correlation"),
    [("refinement_failed", "0.35"), ("refined_overlap_insufficient", "0.3499")],
)
def test_bbc_preverification_threshold_shapes_reject_cross_stage_facts(
    code: str, correlation: str
) -> None:
    evidence = _bbc_empty_stage(code)
    evidence.update(
        {
            "native_offset_seconds": "0",
            "initial_b_ticks": 0,
            "standard_score": "1",
            "channel": "mono",
            "refined_b_ticks": 0,
            "refined_correlation": correlation,
        }
    )
    with pytest.raises(AlignmentError):
        _uncertain_bbc_interval(evidence)


def test_bbc_refinement_failed_rejects_missing_refinement_result() -> None:
    evidence = _bbc_empty_stage("refinement_failed")
    evidence.update(
        {
            "native_offset_seconds": "0",
            "initial_b_ticks": 0,
            "standard_score": "1",
            "channel": "mono",
        }
    )
    with pytest.raises(AlignmentError):
        AlignmentInterval.from_dict(_uncertain_bbc_interval(evidence).to_dict())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("refined_b_ticks", []),
        ("refined_correlation", {}),
        ("verification_windows", {}),
    ],
)
def test_bbc_malformed_stage_values_raise_alignment_error(
    field: str, value: object
) -> None:
    evidence = _bbc_empty_stage("refinement_failed")
    evidence.update(
        {
            "native_offset_seconds": "0",
            "initial_b_ticks": 0,
            "standard_score": "1",
            "channel": "mono",
            "refined_b_ticks": 0,
            "refined_correlation": "0.34",
            field: value,
        }
    )
    with pytest.raises(AlignmentError):
        AlignmentInterval.from_dict(_uncertain_bbc_interval(evidence).to_dict())


@pytest.mark.parametrize("window_count", [0, 1, 2])
def test_bbc_decode_failed_verification_stage_accepts_only_passed_prefix(
    window_count: int,
) -> None:
    evidence = _bbc_verification_stage(
        "decode_failed", tuple("0.9" for _ in range(window_count))
    )
    interval = _uncertain_bbc_interval(evidence)
    assert AlignmentInterval.from_dict(interval.to_dict()) == interval


def test_bbc_decode_failed_rejects_refinement_without_geometry() -> None:
    evidence = _bbc_empty_stage("decode_failed")
    evidence.update(
        {
            "native_offset_seconds": "0",
            "initial_b_ticks": 0,
            "standard_score": "1",
            "channel": "mono",
            "refined_b_ticks": 0,
            "refined_correlation": "0.9",
        }
    )
    with pytest.raises(AlignmentError):
        AlignmentInterval.from_dict(_uncertain_bbc_interval(evidence).to_dict())


def test_bbc_decode_failed_rejects_three_completed_windows() -> None:
    evidence = _bbc_verification_stage("decode_failed", ("0.9", "0.9", "0.9"))
    with pytest.raises(AlignmentError):
        AlignmentInterval.from_dict(_uncertain_bbc_interval(evidence).to_dict())


def test_bbc_verification_failed_requires_failed_strict_prefix() -> None:
    evidence = _bbc_empty_stage("verification_failed")
    evidence.update(
        {
            "native_offset_seconds": "0",
            "initial_b_ticks": 0,
            "standard_score": "1",
            "channel": "mono",
            "refined_b_ticks": 0,
            "refined_correlation": "0.9",
            "refined_geometry": {
                "main_start_ticks": 0,
                "main_end_ticks": DURATION,
                "auxiliary_start_ticks": 0,
                "auxiliary_end_ticks": DURATION,
            },
            "verification_windows": [
                {
                    "label": "opening",
                    "main_start_ticks": 0,
                    "main_end_ticks": 1_200_000,
                    "auxiliary_start_ticks": 0,
                    "auxiliary_end_ticks": 1_200_000,
                    "b_ticks": 0,
                    "local_error_ticks": 0,
                    "correlation": "0.34",
                }
            ],
            "verification_window_count": 1,
            "max_local_offset_error_ticks": 0,
        }
    )
    assert AlignmentInterval.from_dict(_uncertain_bbc_interval(evidence).to_dict())
    evidence["verification_windows"][0]["label"] = "middle"
    with pytest.raises(AlignmentError):
        _uncertain_bbc_interval(evidence)


@pytest.mark.parametrize(
    "correlations",
    [("0.34",), ("0.9", "0.34"), ("0.9", "0.9", "0.34")],
)
def test_bbc_verification_failed_accepts_pass_prefix_and_final_failure(
    correlations: tuple[str, ...],
) -> None:
    evidence = _bbc_verification_stage("verification_failed", correlations)
    interval = _uncertain_bbc_interval(evidence)
    assert AlignmentInterval.from_dict(interval.to_dict()) == interval


def test_bbc_verification_failed_accepts_final_local_error_failure() -> None:
    evidence = _bbc_verification_stage("verification_failed", ("0.9",))
    windows = evidence["verification_windows"]
    assert isinstance(windows, list)
    window = windows[0]
    assert isinstance(window, dict)
    window["b_ticks"] = 12_001
    window["local_error_ticks"] = 12_001
    evidence["max_local_offset_error_ticks"] = 12_001
    interval = _uncertain_bbc_interval(evidence)
    assert AlignmentInterval.from_dict(interval.to_dict()) == interval


def test_bbc_verification_failed_rejects_success_after_failed_middle() -> None:
    evidence = _bbc_verification_stage(
        "verification_failed", ("0.9", "0.34", "0.9")
    )
    with pytest.raises(AlignmentError):
        AlignmentInterval.from_dict(_uncertain_bbc_interval(evidence).to_dict())


def _profile() -> AlignmentVerificationProfile:
    return AlignmentVerificationProfile(
        name="roughcut_audalign_fixed_offset", version=1
    )


def _algorithm() -> AlignmentAlgorithm:
    return AlignmentAlgorithm(
        name="audalign_fingerprint",
        version="1.3.1",
        upstream_commit="d5955ae8a85b1cd480dadd005c3f88986f4ebbef",
        accuracy=2,
        num_processors=1,
        mapping_model="fixed_offset_equal_speed",
        ticks_per_second=120_000,
        verification_profile=_profile(),
    )


def _basis() -> tuple[AlignmentSourceBasis, ...]:
    return (
        AlignmentSourceBasis(
            camera_id="aux-1",
            source_id="src_aux_1",
            fingerprint=AlignmentSourceFingerprint(
                size=1, mtime_ns=1, sha256_head_tail="b" * 64
            ),
            duration_ticks=DURATION,
        ),
        AlignmentSourceBasis(
            camera_id="main",
            source_id="src_main_1",
            fingerprint=AlignmentSourceFingerprint(
                size=1, mtime_ns=1, sha256_head_tail="a" * 64
            ),
            duration_ticks=DURATION,
        ),
    )


def _mapped_interval(
    interval_id: str = "ali_1",
    *,
    main_start: int = 0,
    main_end: int = DURATION,
    aux_start: int = 0,
    aux_end: int = DURATION,
) -> AlignmentInterval:
    return AlignmentInterval(
        interval_id=interval_id,
        auxiliary_camera_id="aux-1",
        classification="mapped",
        main={"source_id": "src_main_1", "start_ticks": main_start, "end_ticks": main_end},
        auxiliary={
            "source_id": "src_aux_1",
            "start_ticks": aux_start,
            "end_ticks": aux_end,
        },
        evidence={
            "code": "fixed_offset_verified",
            "raw_candidate_count": 1,
            "matching_fingerprint_counts": [1],
            "verification_window_count": 3,
            "verification_profile": _profile().to_dict(),
            "max_local_offset_error_ticks": 0,
        },
    )


def _artifact(
    *,
    intervals: tuple[AlignmentInterval, ...] = (_mapped_interval(),),
    camera: AlignmentCamera | None = None,
    summary: AlignmentSummary | None = None,
) -> MulticamAlignmentArtifact:
    if camera is None:
        camera = AlignmentCamera(
            camera_id="aux-1",
            ordered_source_ids=("src_aux_1",),
            status="complete",
            mapped_ticks=DURATION,
            missing_ticks=0,
            uncertain_ticks=0,
            conflict_ticks=0,
            errors=(),
        )
    if summary is None:
        summary = AlignmentSummary(
            total_main_ticks=DURATION,
            camera_count=1,
            mapped_ticks=DURATION,
            missing_ticks=0,
            uncertain_ticks=0,
            conflict_ticks=0,
        )
    return MulticamAlignmentArtifact(
        alignment_id="aln_strict",
        project_id="project_fixture",
        producer_operation_id="op_00000000000040008000000000000001",
        created_at="2026-07-29T00:00:00.000000Z",
        request_hash="c" * 64,
        input_hash="d" * 64,
        algorithm=_algorithm(),
        main_camera=AlignmentCameraGroup(
            camera_id="main", ordered_source_ids=("src_main_1",)
        ),
        auxiliary_cameras=(camera,),
        source_basis=_basis(),
        intervals=intervals,
        summary=summary,
    )


def test_schema_one_has_no_camera_summaries_field() -> None:
    artifact = _artifact()
    payload = artifact.to_dict()
    assert "camera_summaries" not in payload
    assert set(payload) == {
        "schema_version",
        "alignment_id",
        "project_id",
        "producer_operation_id",
        "created_at",
        "request_hash",
        "input_hash",
        "algorithm",
        "main_camera",
        "auxiliary_cameras",
        "source_basis",
        "intervals",
        "summary",
    }
    # the per-camera status/ticks/errors live on the auxiliary item
    camera = payload["auxiliary_cameras"][0]
    assert set(camera) == {
        "camera_id",
        "ordered_source_ids",
        "status",
        "mapped_ticks",
        "missing_ticks",
        "uncertain_ticks",
        "conflict_ticks",
        "errors",
    }


def test_profile_two_artifact_reads_while_unknown_profile_stays_closed() -> None:
    payload = copy.deepcopy(_artifact().to_dict())
    algorithm = payload["algorithm"]
    assert isinstance(algorithm, dict)
    verification_profile = algorithm["verification_profile"]
    assert isinstance(verification_profile, dict)
    verification_profile["version"] = 2
    interval = payload["intervals"][0]
    assert isinstance(interval, dict)
    evidence = interval["evidence"]
    assert isinstance(evidence, dict)
    interval_profile = evidence["verification_profile"]
    assert isinstance(interval_profile, dict)
    interval_profile["version"] = 2
    parsed = MulticamAlignmentArtifact.from_dict(payload)
    assert parsed.algorithm.verification_profile.version == 2
    assert parsed.intervals[0].evidence["verification_profile"] == {
        "name": "roughcut_audalign_fixed_offset",
        "version": 2,
    }
    verification_profile["version"] = 3
    with pytest.raises(AlignmentError):
        MulticamAlignmentArtifact.from_dict(payload)


@pytest.mark.parametrize(
    ("algorithm_version", "evidence_version"),
    [(1, 2), (2, 1)],
)
def test_algorithm_and_interval_profile_versions_must_match(
    algorithm_version: int, evidence_version: int
) -> None:
    payload = copy.deepcopy(_artifact().to_dict())
    algorithm = payload["algorithm"]
    interval = payload["intervals"][0]
    assert isinstance(algorithm, dict)
    assert isinstance(interval, dict)
    algorithm_profile = algorithm["verification_profile"]
    evidence = interval["evidence"]
    assert isinstance(algorithm_profile, dict)
    assert isinstance(evidence, dict)
    evidence_profile = evidence["verification_profile"]
    assert isinstance(evidence_profile, dict)
    algorithm_profile["version"] = algorithm_version
    evidence_profile["version"] = evidence_version
    with pytest.raises(AlignmentError):
        MulticamAlignmentArtifact.from_dict(payload)


def test_nullable_error_source_id_roundtrips() -> None:
    error = AlignmentPerCameraError(
        code="auxiliary_recognition_failed", source_id=None
    )
    assert error.to_dict() == {"code": "auxiliary_recognition_failed", "source_id": None}
    parsed = AlignmentPerCameraError.from_dict(error.to_dict())
    assert parsed.source_id is None
    with pytest.raises(AlignmentError):
        AlignmentPerCameraError.from_dict(
            {"code": "auxiliary_decode_failed", "source_id": ""}
        )
    with pytest.raises(AlignmentError):
        AlignmentPerCameraError.from_dict(
            {"code": "auxiliary_decode_failed", "source_id": 3}
        )


def test_corrupt_artifact_is_rejected() -> None:
    artifact = _artifact()
    data = artifact.to_dict()
    data["unknown_field"] = True
    with pytest.raises(AlignmentError):
        MulticamAlignmentArtifact.from_dict(data)
    del data["unknown_field"]
    data["intervals"] = "not-a-list"
    with pytest.raises(AlignmentError):
        MulticamAlignmentArtifact.from_dict(data)


def test_partition_gap_is_rejected() -> None:
    # interval covers [0, 5s] only; the camera claims full coverage
    half = _mapped_interval(main_end=6_000_000, aux_end=6_000_000)
    camera = AlignmentCamera(
        camera_id="aux-1",
        ordered_source_ids=("src_aux_1",),
        status="partial",
        mapped_ticks=6_000_000,
        missing_ticks=0,
        uncertain_ticks=0,
        conflict_ticks=0,
        errors=(),
    )
    summary = AlignmentSummary(
        total_main_ticks=DURATION,
        camera_count=1,
        mapped_ticks=6_000_000,
        missing_ticks=8_400_000,
        uncertain_ticks=0,
        conflict_ticks=0,
    )
    # the summary equation holds but the intervals do not cover the timeline
    with pytest.raises(AlignmentError):
        _artifact(intervals=(half,), camera=camera, summary=summary)


def test_partition_overlap_is_rejected() -> None:
    first = _mapped_interval(
        "ali_1", main_end=9_600_000, aux_end=9_600_000
    )
    second = _mapped_interval(
        "ali_2", main_start=4_800_000, aux_start=4_800_000
    )
    camera = AlignmentCamera(
        camera_id="aux-1",
        ordered_source_ids=("src_aux_1",),
        status="complete",
        mapped_ticks=DURATION,
        missing_ticks=0,
        uncertain_ticks=0,
        conflict_ticks=0,
        errors=(),
    )
    with pytest.raises(AlignmentError):
        _artifact(intervals=(first, second), camera=camera)


def test_forged_summary_is_rejected() -> None:
    # the intervals say 10s mapped + 10s missing; the summary lies
    half = _mapped_interval(main_end=6_000_000, aux_end=6_000_000)
    from roughcut.domain.alignment import AlignmentInterval as AI

    missing = AI(
        interval_id="ali_miss",
        auxiliary_camera_id="aux-1",
        classification="missing",
        main={"source_id": "src_main_1", "start_ticks": 6_000_000, "end_ticks": DURATION},
        auxiliary=None,
        evidence={
            "code": "no_candidate",
            "raw_candidate_count": 0,
            "matching_fingerprint_counts": [],
            "verification_window_count": 0,
            "verification_profile": _profile().to_dict(),
            "max_local_offset_error_ticks": None,
        },
    )
    camera = AlignmentCamera(
        camera_id="aux-1",
        ordered_source_ids=("src_aux_1",),
        status="partial",
        mapped_ticks=6_000_000,
        missing_ticks=8_400_000,
        uncertain_ticks=0,
        conflict_ticks=0,
        errors=(),
    )
    summary = AlignmentSummary(
        total_main_ticks=DURATION,
        camera_count=1,
        mapped_ticks=14_400_000,  # forged: intervals say 6M mapped
        missing_ticks=0,
        uncertain_ticks=0,
        conflict_ticks=0,
    )
    with pytest.raises(AlignmentError):
        _artifact(intervals=(half, missing), camera=camera, summary=summary)


def test_forged_camera_status_is_rejected() -> None:
    # camera says complete but has missing ticks
    half = _mapped_interval(main_end=6_000_000, aux_end=6_000_000)
    from roughcut.domain.alignment import AlignmentInterval as AI

    missing = AI(
        interval_id="ali_miss",
        auxiliary_camera_id="aux-1",
        classification="missing",
        main={"source_id": "src_main_1", "start_ticks": 6_000_000, "end_ticks": DURATION},
        auxiliary=None,
        evidence={
            "code": "no_candidate",
            "raw_candidate_count": 0,
            "matching_fingerprint_counts": [],
            "verification_window_count": 0,
            "verification_profile": _profile().to_dict(),
            "max_local_offset_error_ticks": None,
        },
    )
    camera = AlignmentCamera(
        camera_id="aux-1",
        ordered_source_ids=("src_aux_1",),
        status="complete",  # forged: partial is correct
        mapped_ticks=6_000_000,
        missing_ticks=8_400_000,
        uncertain_ticks=0,
        conflict_ticks=0,
        errors=(),
    )
    summary = AlignmentSummary(
        total_main_ticks=DURATION,
        camera_count=1,
        mapped_ticks=6_000_000,
        missing_ticks=8_400_000,
        uncertain_ticks=0,
        conflict_ticks=0,
    )
    with pytest.raises(AlignmentError):
        _artifact(intervals=(half, missing), camera=camera, summary=summary)


def test_unsorted_extra_basis_is_rejected() -> None:
    artifact = _artifact()
    data = artifact.to_dict()
    # duplicate main entry
    data["source_basis"].append(copy.deepcopy(data["source_basis"][1]))
    with pytest.raises(AlignmentError):
        MulticamAlignmentArtifact.from_dict(data)
    # unsorted order
    data = artifact.to_dict()
    data["source_basis"] = [data["source_basis"][1], data["source_basis"][0]]
    with pytest.raises(AlignmentError):
        MulticamAlignmentArtifact.from_dict(data)


def test_mapped_foreign_aux_source_is_rejected() -> None:
    artifact = _artifact()
    data = artifact.to_dict()
    data["intervals"][0]["auxiliary"]["source_id"] = "src_other"
    with pytest.raises(AlignmentError):
        MulticamAlignmentArtifact.from_dict(data)


def test_mapped_unequal_duration_is_rejected() -> None:
    artifact = _artifact()
    data = artifact.to_dict()
    data["intervals"][0]["auxiliary"]["end_ticks"] = DURATION + 120_000
    with pytest.raises(AlignmentError):
        MulticamAlignmentArtifact.from_dict(data)


def test_same_aux_source_in_two_aux_cameras_is_rejected() -> None:
    artifact = _artifact()
    data = artifact.to_dict()
    data["auxiliary_cameras"].append(
        {
            "camera_id": "aux-2",
            "ordered_source_ids": ["src_aux_1"],
            "status": "omitted",
            "mapped_ticks": 0,
            "missing_ticks": 0,
            "uncertain_ticks": DURATION,
            "conflict_ticks": 0,
            "errors": [],
        }
    )
    data["source_basis"].append(
        {
            "camera_id": "aux-2",
            "source_id": "src_aux_1",
            "fingerprint": {"size": 1, "mtime_ns": 1, "sha256_head_tail": "b" * 64},
            "duration_ticks": DURATION,
        }
    )
    data["intervals"].append(
        {
            "interval_id": "ali_aux2",
            "auxiliary_camera_id": "aux-2",
            "classification": "uncertain",
            "main": {"source_id": "src_main_1", "start_ticks": 0, "end_ticks": DURATION},
            "auxiliary": None,
            "evidence": {
                "code": "no_candidate",
                "raw_candidate_count": 0,
                "matching_fingerprint_counts": [],
                "verification_window_count": 0,
                "verification_profile": _profile().to_dict(),
                "max_local_offset_error_ticks": None,
            },
        }
    )
    # the same aux source now belongs to aux-1 and aux-2: must be rejected
    with pytest.raises(AlignmentError):
        MulticamAlignmentArtifact.from_dict(data)


def test_cross_camera_error_source_id_is_rejected() -> None:
    artifact = _artifact()
    data = artifact.to_dict()
    # an error on aux-1 that names a source owned by no camera at all
    data["auxiliary_cameras"][0]["errors"] = [
        {"code": "auxiliary_decode_failed", "source_id": "src_other"}
    ]
    with pytest.raises(AlignmentError):
        MulticamAlignmentArtifact.from_dict(data)


def test_mapped_with_zero_raw_candidates_is_rejected() -> None:
    artifact = _artifact()
    data = artifact.to_dict()
    interval = data["intervals"][0]
    interval["evidence"]["raw_candidate_count"] = 0
    interval["evidence"]["matching_fingerprint_counts"] = []
    with pytest.raises(AlignmentError):
        MulticamAlignmentArtifact.from_dict(data)


def test_reversed_serialized_partition_is_rejected() -> None:
    first = _mapped_interval(
        "ali_1", main_end=7_200_000, aux_end=7_200_000
    )
    second = _mapped_interval(
        "ali_2", main_start=7_200_000, aux_start=7_200_000
    )
    camera = AlignmentCamera(
        camera_id="aux-1",
        ordered_source_ids=("src_aux_1",),
        status="complete",
        mapped_ticks=DURATION,
        missing_ticks=0,
        uncertain_ticks=0,
        conflict_ticks=0,
        errors=(),
    )
    # serialized order [ali_2, ali_1] covers the timeline but is not sorted:
    # the persisted order itself must be a gapless ascending partition
    with pytest.raises(AlignmentError):
        _artifact(intervals=(second, first), camera=camera)


def test_mismatched_raw_and_matching_counts_is_rejected() -> None:
    artifact = _artifact()
    data = artifact.to_dict()
    interval = data["intervals"][0]
    interval["evidence"]["raw_candidate_count"] = 2
    with pytest.raises(AlignmentError):
        MulticamAlignmentArtifact.from_dict(data)


def test_json_roundtrip_preserves_nullable_error() -> None:
    from roughcut.domain.alignment import AlignmentInterval as AI

    unc = AI(
        interval_id="ali_unc",
        auxiliary_camera_id="aux-1",
        classification="uncertain",
        main={"source_id": "src_main_1", "start_ticks": 0, "end_ticks": DURATION},
        auxiliary=None,
        evidence={
            "code": "no_candidate",
            "raw_candidate_count": 0,
            "matching_fingerprint_counts": [],
            "verification_window_count": 0,
            "verification_profile": _profile().to_dict(),
            "max_local_offset_error_ticks": None,
        },
    )
    summary = AlignmentSummary(
        total_main_ticks=DURATION,
        camera_count=1,
        mapped_ticks=0,
        missing_ticks=0,
        uncertain_ticks=DURATION,
        conflict_ticks=0,
    )
    camera_with_ticks = AlignmentCamera(
        camera_id="aux-1",
        ordered_source_ids=("src_aux_1",),
        status="failed",
        mapped_ticks=0,
        missing_ticks=0,
        uncertain_ticks=DURATION,
        conflict_ticks=0,
        errors=(
            AlignmentPerCameraError(
                code="auxiliary_recognition_failed", source_id=None
            ),
        ),
    )
    artifact = _artifact(
        intervals=(unc,), camera=camera_with_ticks, summary=summary
    )
    payload = json.loads(
        json.dumps(artifact.to_dict())
    )
    assert payload["auxiliary_cameras"][0]["errors"][0]["source_id"] is None
    parsed = MulticamAlignmentArtifact.from_dict(payload)
    assert parsed.auxiliary_cameras[0].errors[0].source_id is None
    assert parsed.auxiliary_cameras[0].status == "failed"


# ---------------------------------------------------------------------------
# OperationRecord closed counterexamples
# ---------------------------------------------------------------------------


def _scope() -> object:
    from roughcut.domain.media_operation import ProjectOperationScope

    return ProjectOperationScope(
        project_id="project_fixture",
        project_root_hash="a" * 64,
    )


def _record(
    *,
    status: str,
    code: str,
    action: str,
    responsibility: str,
) -> object:
    from roughcut.domain.media_operation import (
        MediaOperationFailure,
        MediaOperationRecord,
    )

    scope = _scope()
    return MediaOperationRecord(
        schema_version=2,
        operation_id="op_00000000000040008000000000000060",
        scope=scope,  # type: ignore[arg-type]
        operation_type="align_multicam",
        request_hash="b" * 64,
        input_hash="c" * 64,
        status=status,  # type: ignore[arg-type]
        phase_message_code=(
            "alignment_failed"
            if status == "failed"
            else "alignment_interrupted"
        ),
        created_at="2026-07-29T00:00:00.000000Z",
        started_at="2026-07-29T00:00:01.000000Z",
        updated_at="2026-07-29T00:00:02.000000Z",
        finished_at="2026-07-29T00:00:02.000000Z",
        result_ref=None,
        error=MediaOperationFailure(
            code=code,
            responsibility=responsibility,  # type: ignore[arg-type]
            action=action,
            message_code=(
                "alignment_failed"
                if status == "failed"
                else "alignment_interrupted"
            ),
        ),
    )


def test_failed_record_rejects_interrupted_code() -> None:
    from roughcut.domain.alignment import AlignmentError
    from roughcut.domain.media_operation import MediaOperationError

    with pytest.raises((AlignmentError, MediaOperationError)):
        _record(
            status="failed",
            code="alignment_interrupted",
            action="revalidate_alignment_basis",
            responsibility="roughcut_core",
        )


def test_interrupted_record_requires_interrupted_code() -> None:
    from roughcut.domain.media_operation import MediaOperationError

    # interrupted with a plain failure code is rejected
    with pytest.raises(MediaOperationError):
        _record(
            status="interrupted",
            code="alignment_basis_changed_during_run",
            action="recover_abandoned_media_operation",
            responsibility="roughcut_core",
        )
    # interrupted with a non-interruption action is rejected
    with pytest.raises(MediaOperationError):
        _record(
            status="interrupted",
            code="alignment_interrupted",
            action="publish_alignment_artifact",
            responsibility="roughcut_core",
        )
    # interrupted with a host responsibility via recover action is rejected
    with pytest.raises(MediaOperationError):
        _record(
            status="interrupted",
            code="alignment_interrupted",
            action="recover_abandoned_media_operation",
            responsibility="host",
        )


def test_failed_record_rejects_unknown_code_and_mismatched_action() -> None:
    from roughcut.domain.media_operation import MediaOperationError

    with pytest.raises(MediaOperationError):
        _record(
            status="failed",
            code="alignment_unknown_code",
            action="revalidate_alignment_basis",
            responsibility="roughcut_core",
        )
    with pytest.raises(MediaOperationError):
        _record(
            status="failed",
            code="alignment_basis_changed_during_run",
            action="encode_render",
            responsibility="roughcut_core",
        )
    with pytest.raises(MediaOperationError):
        _record(
            status="failed",
            code="alignment_basis_changed_during_run",
            action="validate_alignment_basis",
            responsibility="asr_worker",
        )


def test_request_projection_rejects_non_main_main_and_main_named_aux() -> None:
    scope = {
        "project_id": "proj_demo",
        "project_root_hash": "0" * 64,
        "kind": "project",
    }
    base = {
        "operation_id": "op_demo",
        "alignment_id": "aln_demo",
        "expected_revision": 1,
        "main_camera": {
            "camera_id": "main",
            "ordered_source_ids": ["src_main"],
        },
        "auxiliary_cameras": [
            {"camera_id": "aux", "ordered_source_ids": ["src_aux"]}
        ],
        "main_audio_stable": True,
        "max_temporary_disk_bytes": 1,
        "max_analysis_memory_bytes": 1,
        "max_runtime_seconds": 1,
    }
    alignment_request_projection(scope=scope, **base)

    other_main = dict(base)
    other_main["main_camera"] = {
        "camera_id": "primary",
        "ordered_source_ids": ["src_main"],
    }
    with pytest.raises(AlignmentError) as exc:
        alignment_request_projection(scope=scope, **other_main)
    assert exc.value.code == "alignment_integrity_error"

    aux_named_main = dict(base)
    aux_named_main["auxiliary_cameras"] = [
        {"camera_id": "main", "ordered_source_ids": ["src_aux"]}
    ]
    with pytest.raises(AlignmentError) as exc:
        alignment_request_projection(scope=scope, **aux_named_main)
    assert exc.value.code == "alignment_integrity_error"
