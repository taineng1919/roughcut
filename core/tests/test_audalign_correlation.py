"""Tests for Audalign Correlation bounded-probe production path (0.2.5)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from roughcut.application.alignment_profile import (
    correlation_admission,
    correlation_probe_starts,
)
from roughcut.domain.alignment import (
    ALIGNMENT_TICKS_PER_SECOND,
    AUDALIGN_CORRELATION_ALGORITHM_NAME,
    AUDALIGN_CORRELATION_ALGORITHM_VERSION,
    AUDALIGN_CORRELATION_PROBE_TICKS,
    AUDALIGN_CORRELATION_PROFILE_NAME,
    AUDALIGN_CORRELATION_PROFILE_VERSION,
    AUDALIGN_CORRELATION_WRITER_PROFILE,
    AUDALIGN_UPSTREAM_COMMIT,
    AUDALIGN_VERSION,
    AlignmentAlgorithm,
    AlignmentError,
    AlignmentInterval,
    AlignmentVerificationProfile,
    audalign_correlation_typed_config,
)
from roughcut.domain.media_operation import MediaOperationError


def test_correlation_writer_profile_is_canonical():
    assert AUDALIGN_CORRELATION_WRITER_PROFILE["name"] == AUDALIGN_CORRELATION_PROFILE_NAME
    assert AUDALIGN_CORRELATION_WRITER_PROFILE["version"] == AUDALIGN_CORRELATION_PROFILE_VERSION
    assert AUDALIGN_CORRELATION_WRITER_PROFILE["algorithm"]["name"] == AUDALIGN_CORRELATION_ALGORITHM_NAME
    assert AUDALIGN_CORRELATION_WRITER_PROFILE["algorithm"]["version"] == AUDALIGN_CORRELATION_ALGORITHM_VERSION
    assert AUDALIGN_CORRELATION_WRITER_PROFILE["provider"] == "audalign"
    assert AUDALIGN_CORRELATION_WRITER_PROFILE["provider_version"] == AUDALIGN_VERSION
    assert AUDALIGN_CORRELATION_WRITER_PROFILE["upstream_commit"] == AUDALIGN_UPSTREAM_COMMIT
    assert AUDALIGN_CORRELATION_WRITER_PROFILE["recognizer"] == "CorrelationRecognizer"
    assert AUDALIGN_CORRELATION_WRITER_PROFILE["ticks_per_second"] == 120000
    assert AUDALIGN_CORRELATION_WRITER_PROFILE["decode"]["sample_rate_hz"] == 44100
    assert AUDALIGN_CORRELATION_WRITER_PROFILE["probe"]["excerpt_ticks"] == 1800000
    assert AUDALIGN_CORRELATION_WRITER_PROFILE["probe"]["percentages"] == [20, 50, 80]
    cfg = AUDALIGN_CORRELATION_WRITER_PROFILE["recognizer_config"]
    assert cfg["sample_rate"] == 8000
    assert cfg["fft_window_size"] == 4096
    assert cfg["filter_matches"] == "0.0"


def test_correlation_frozen_config_matches_worker():
    config = audalign_correlation_typed_config()
    assert config["sample_rate"] == 8000
    assert config["fft_window_size"] == 4096
    assert config["filter_matches"] == 0.0
    assert config["freq_threshold"] == 200
    assert config["normalize"] is True


def _call_correlation_adapter(
    tmp_path: Path,
    payload: object,
    *,
    returncode: int = 0,
    runner_error: Exception | None = None,
    budget: object | None = None,
    max_output_bytes: int = 8 * 1024 * 1024,
    max_raw_candidates: int | None = None,
):
    from roughcut.adapters import audalign as audalign_adapter

    output = tmp_path / "correlation-response.json"
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if runner_error is not None:
            raise runner_error
        output.write_text(
            json.dumps(payload, allow_nan=True),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            command,
            returncode,
            stdout="",
            stderr="",
        )

    kwargs: dict[str, object] = {
        "process_runner": fake_run,
        "max_output_bytes": max_output_bytes,
    }
    if budget is not None:
        kwargs["budget"] = budget
    if max_raw_candidates is not None:
        kwargs["max_raw_candidates"] = max_raw_candidates
    result = audalign_adapter.run_audalign_correlation(
        Path(sys.executable).resolve(),
        tmp_path / "target.wav",
        tmp_path / "against.wav",
        output,
        **kwargs,
    )
    return result, calls, output


def test_correlation_adapter_accepts_closed_payload_and_passes_typed_config(
    tmp_path: Path,
) -> None:

    result, calls, output = _call_correlation_adapter(
        tmp_path,
        {
            "match_info": {
                "offset_seconds": [0, 0.125, -0.25],
                "sample_rate": 8000,
            }
        },
    )
    assert [candidate.offset_ticks for candidate in result.candidates] == [
        0,
        15_000,
        -30_000,
    ]
    assert not hasattr(result.candidates[0], "confidence")
    assert not output.exists()
    command = calls[0][0]
    expected_config = json.loads(
        command[command.index("--expected-config") + 1]
    )
    assert expected_config == audalign_correlation_typed_config()
    assert command[command.index("--target-wav") + 1].endswith("target.wav")
    assert command[command.index("--against-wav") + 1].endswith("against.wav")


@pytest.mark.parametrize(
    "payload",
    [
        {"other": None},
        {"match_info": None, "extra": True},
        {"match_info": {"offset_seconds": [0]}},
        {
            "match_info": {
                "offset_seconds": [0],
                "sample_rate": 8000,
                "extra": None,
            }
        },
        {
            "match_info": {
                "offset_seconds": 0,
                "sample_rate": 8000,
            }
        },
        {
            "match_info": {
                "offset_seconds": [0, "bad"],
                "sample_rate": 8000,
            }
        },
        {
            "match_info": {
                "offset_seconds": [],
                "sample_rate": 8000,
            }
        },
        {
            "match_info": {
                "offset_seconds": [0],
                "sample_rate": 44100,
            }
        },
        {
            "match_info": {
                "offset_seconds": [0],
                "sample_rate": 8000.0,
            }
        },
        {
            "match_info": {
                "offset_seconds": [float("nan")],
                "sample_rate": 8000,
            }
        },
        {
            "match_info": {
                "offset_seconds": [float("inf")],
                "sample_rate": 8000,
            }
        },
        {"_roughcut_worker_status": "unknown"},
        {
            "_roughcut_worker_status": "output_overflow",
            "candidate_count": 1,
            "extra": True,
        },
    ],
)
def test_correlation_adapter_rejects_non_closed_or_nonfinite_payloads(
    tmp_path: Path,
    payload: object,
) -> None:
    from roughcut.adapters.audalign import AudalignAdapterError

    with pytest.raises(AudalignAdapterError):
        _call_correlation_adapter(tmp_path, payload)
    assert not (tmp_path / "correlation-response.json").exists()


def test_correlation_adapter_no_candidate_cleans_response(tmp_path: Path) -> None:
    result, _calls, output = _call_correlation_adapter(
        tmp_path,
        {"match_info": None},
    )
    assert result.candidates == ()
    assert not output.exists()


def test_correlation_adapter_candidate_bound_is_explicit(tmp_path: Path) -> None:
    from roughcut.adapters.audalign import AudalignCandidateLimitError

    with pytest.raises(AudalignCandidateLimitError):
        _call_correlation_adapter(
            tmp_path,
            {
                "match_info": {
                    "offset_seconds": [0, 1],
                    "sample_rate": 8000,
                }
            },
            max_raw_candidates=1,
        )
    assert not (tmp_path / "correlation-response.json").exists()


@pytest.mark.parametrize(
    "payload",
    [
        {
            "_roughcut_worker_status": "candidate_overflow",
            "candidate_count": 2,
            "candidate_limit": 1,
        },
        {
            "_roughcut_worker_status": "output_overflow",
            "candidate_count": 2,
        },
    ],
)
def test_correlation_adapter_status_bounds_are_explicit(
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    from roughcut.adapters.audalign import (
        AudalignCandidateLimitError,
        AudalignOutputOverflowError,
    )

    expected = (
        AudalignCandidateLimitError
        if payload["_roughcut_worker_status"] == "candidate_overflow"
        else AudalignOutputOverflowError
    )
    with pytest.raises(expected):
        _call_correlation_adapter(tmp_path, payload)
    assert not (tmp_path / "correlation-response.json").exists()


def test_correlation_adapter_nonzero_and_output_limit_cleanup(
    tmp_path: Path,
) -> None:
    from roughcut.adapters.audalign import AudalignAdapterError

    with pytest.raises(AudalignAdapterError):
        _call_correlation_adapter(
            tmp_path / "nonzero",
            {"match_info": None},
            returncode=7,
        )
    assert not (tmp_path / "nonzero" / "correlation-response.json").exists()
    with pytest.raises(AudalignAdapterError):
        _call_correlation_adapter(
            tmp_path / "invalid-limit",
            {"match_info": None},
            max_output_bytes=0,
        )
    assert not (tmp_path / "invalid-limit" / "correlation-response.json").exists()


@pytest.mark.parametrize(
    ("child_error", "adapter_error"),
    [
        (
            subprocess.TimeoutExpired(["worker"], 1),
            "AudalignAdapterError",
        ),
    ],
)
def test_correlation_adapter_process_timeout_cleanup(
    tmp_path: Path,
    child_error: Exception,
    adapter_error: str,
) -> None:
    from roughcut.adapters.audalign import AudalignAdapterError

    del adapter_error
    with pytest.raises(AudalignAdapterError):
        _call_correlation_adapter(
            tmp_path,
            {"match_info": None},
            runner_error=child_error,
        )
    assert not (tmp_path / "correlation-response.json").exists()


@pytest.mark.parametrize(
    ("child_error", "expected"),
    [
        pytest.param(
            "time",
            "AudalignTimeBudgetError",
            id="timeout",
        ),
        pytest.param(
            "memory",
            "AudalignMemoryBudgetError",
            id="memory",
        ),
        pytest.param(
            "generic",
            "AudalignBudgetError",
            id="generic-budget",
        ),
    ],
)
def test_correlation_adapter_budget_errors_are_typed_and_cleaned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    child_error: str,
    expected: str,
) -> None:
    from roughcut.adapters import audalign as audalign_adapter
    from roughcut.adapters.child_budget import (
        ChildProcessBudgetError,
        ChildProcessMemoryBudgetError,
        ChildProcessTimeBudgetError,
    )

    error_type = {
        "time": ChildProcessTimeBudgetError,
        "memory": ChildProcessMemoryBudgetError,
        "generic": ChildProcessBudgetError,
    }[child_error]

    def fail(*_args: object, **_kwargs: object):
        raise error_type("fixture budget failure")

    monkeypatch.setattr(audalign_adapter, "run_bounded_child", fail)
    expected_type = {
        "AudalignTimeBudgetError": audalign_adapter.AudalignTimeBudgetError,
        "AudalignMemoryBudgetError": audalign_adapter.AudalignMemoryBudgetError,
        "AudalignBudgetError": audalign_adapter.AudalignBudgetError,
    }[expected]
    with pytest.raises(expected_type):
        _call_correlation_adapter(
            tmp_path,
            {"match_info": None},
            budget=object(),
        )
    assert not (tmp_path / "correlation-response.json").exists()


def test_correlation_probe_schedule_integer_and_clamp():
    # aux < excerpt => insufficient
    assert correlation_probe_starts(1_799_999) == ()
    # D == L -> all three clamp to 0 => duplicate => fail closed ()
    assert correlation_probe_starts(1_800_000) == ()
    # 30s aux (3_600_000) -> 20% 720k, 50% 1_800_000, 80% clamp to 1_800_000 duplicate => fail closed ()
    assert correlation_probe_starts(3_600_000) == ()
    # 60s aux (7_200_000) -> distinct
    starts = correlation_probe_starts(7_200_000)
    assert starts == (1440000, 3600000, 5400000)
    # Check integer ticks and start+excerpt <= aux duration
    for s in starts:
        assert isinstance(s, int)
        assert s + AUDALIGN_CORRELATION_PROBE_TICKS <= 7_200_000


def test_correlation_probe_duplicate_not_counted_as_support():
    # Ensure duplicate starts are rejected, not counted as two supports
    # 30s case already fails
    assert correlation_probe_starts(3_600_000) == ()
    # Admission with duplicate B values from distinct probes should still be evaluated
    # If two probes have same derived B due to same content, they would be considered separate supports
    # but our admission counts distinct probes; if they are same B, spread 0, still 2 supports
    starts = (0, 900000, 1800000)
    # Simulate two probes with same B (duplicate offset)
    code, rep, spread, support = correlation_admission((1000, 1000, 20000), starts)
    # 1000 and 1000 are same B, spread 0, support should be 2 (indices 0,1) or maybe 3? Let's see: valid Bs are 1000,1000,20000 => qualifying cluster of 1000,1000 spread 0 => support 2
    assert code == "fixed_offset_verified"
    assert rep == 1000
    assert spread == 0
    assert support == (0, 1)


def test_correlation_admission_cases():
    starts = (0, 500000, 1000000)
    # 3/3 within tolerance
    code, rep, spread, support = correlation_admission((0, 5000, 6000), starts)
    assert code == "fixed_offset_verified"
    assert rep == 0  # earliest in 20->50->80
    assert spread == 6000
    assert support == (0, 1, 2)
    # 2/3 + outlier
    code, rep, spread, support = correlation_admission((0, 5000, 50000), starts)
    assert code == "fixed_offset_verified"
    assert support == (0, 1)
    # 1/3 -> no candidate, while the two-point inconsistency cases below stay
    # distinct from a probe that simply produced no result.
    code, rep, spread, support = correlation_admission((0, None, None), starts)
    assert code == "no_candidate"
    # No cluster
    code, rep, spread, support = correlation_admission((0, 20000, 40000), starts)
    assert code == "probe_inconsistent"
    # Ambiguous two clusters: 0,11900,23800 -> two separate qualifying pairs
    code, rep, spread, support = correlation_admission((0, 11900, 23800), (0, 1000000, 2000000))
    assert code == "probe_inconsistent"
    # Non-chaining: 0,11900,23800 should not be considered single cluster via chaining
    # Already tested as probe_inconsistent due to ambiguous
    # Earliest fixed-order representative: if cluster is 5000,6000 from probes 50 and 80, representative should be 5000 (50)
    code, rep, spread, support = correlation_admission((None, 5000, 6000), starts)
    assert code == "fixed_offset_verified"
    assert rep == 5000
    assert support == (1, 2)


def test_correlation_evidence_closed_shapes():
    # Mapped evidence must be closed and not contain fingerprint fields; mapped requires exactly 3 probes per spec
    evidence = {
        "code": "fixed_offset_verified",
        "provider": "audalign",
        "provider_version": "1.3.1",
        "recognizer": "CorrelationRecognizer",
        "probe_records": [
            {
                "percentage": 20,
                "auxiliary_start_ticks": 0,
                "auxiliary_end_ticks": 1_800_000,
                "native_offset_seconds": "0.0",
                "derived_b_ticks": 0,
            },
            {
                "percentage": 50,
                "auxiliary_start_ticks": 500000,
                "auxiliary_end_ticks": 2_300_000,
                "native_offset_seconds": "4.166666666666667",
                "derived_b_ticks": 0,  # -500000 + ticks(4.166...)=0
            },
            {
                "percentage": 80,
                "auxiliary_start_ticks": 1000000,
                "auxiliary_end_ticks": 2_800_000,
                "native_offset_seconds": "8.333333333333334",
                "derived_b_ticks": 0,  # -1000000 + 1000000 =0
            },
        ],
        "support_probe_count": 3,
        "cluster_spread_ticks": 0,
        "representative_b_ticks": 0,
        "conflicting_b_ticks": [],
        "verification_profile": {"name": AUDALIGN_CORRELATION_PROFILE_NAME, "version": 1},
    }
    # Fix derived for second record: compute ticks
    # 4.166666... *120000 = 500000, so -500000+500000=0
    interval = AlignmentInterval(
        interval_id="ali_test_mapped",
        auxiliary_camera_id="B",
        classification="mapped",
        main={"source_id": "main1", "start_ticks": 0, "end_ticks": 1000000},
        auxiliary={"source_id": "aux1", "start_ticks": 0, "end_ticks": 1000000},
        evidence=evidence,
    )
    assert interval.evidence["code"] == "fixed_offset_verified"
    # Uncertain evidence uses the closed insufficient-probes shape.
    evidence_uncertain = dict(evidence)
    evidence_uncertain["code"] = "insufficient_probes"
    evidence_uncertain["probe_records"] = []
    evidence_uncertain["support_probe_count"] = 0
    evidence_uncertain["cluster_spread_ticks"] = None
    evidence_uncertain["representative_b_ticks"] = None
    interval2 = AlignmentInterval(
        interval_id="ali_test_uncertain",
        auxiliary_camera_id="B",
        classification="uncertain",
        main={"source_id": "main1", "start_ticks": 0, "end_ticks": 1000000},
        auxiliary=None,
        evidence=evidence_uncertain,
    )
    assert interval2.classification == "uncertain"


def _correlation_probe_record(
    percentage: int, start: int, native: str | None, derived: int | None
) -> dict[str, object]:
    return {
        "percentage": percentage,
        "auxiliary_start_ticks": start,
        "auxiliary_end_ticks": start + 1_800_000,
        "native_offset_seconds": native,
        "derived_b_ticks": derived,
    }


def _correlation_evidence(
    code: str,
    records: list[dict[str, object]],
    *,
    classification: str,
    support: int,
    spread: int | None,
    representative: int | None,
    conflicts: list[int] | None = None,
) -> AlignmentInterval:
    return AlignmentInterval(
        interval_id=f"ali_shape_{code}_{len(records)}_{classification}",
        auxiliary_camera_id="B",
        classification=classification,  # type: ignore[arg-type]
        main={"source_id": "main1", "start_ticks": 0, "end_ticks": 1_000_000},
        auxiliary=(
            {"source_id": "aux1", "start_ticks": 0, "end_ticks": 1_000_000}
            if classification == "mapped"
            else None
        ),
        evidence={
            "code": code,
            "provider": "audalign",
            "provider_version": "1.3.1",
            "recognizer": "CorrelationRecognizer",
            "probe_records": records,
            "support_probe_count": support,
            "cluster_spread_ticks": spread,
            "representative_b_ticks": representative,
            "conflicting_b_ticks": list(conflicts or []),
            "verification_profile": {
                "name": AUDALIGN_CORRELATION_PROFILE_NAME,
                "version": 1,
            },
        },
    )


def test_correlation_mapped_and_conflict_require_all_three_probe_records() -> None:
    mapped_records = [
        _correlation_probe_record(20, 0, "0", 0),
        _correlation_probe_record(
            50, 500_000, "4.208333333333333333333333333", 5_000
        ),
        _correlation_probe_record(80, 1_000_000, None, None),
    ]
    interval = _correlation_evidence(
        "fixed_offset_verified",
        mapped_records,
        classification="mapped",
        support=2,
        spread=5_000,
        representative=0,
    )
    assert interval.evidence["support_probe_count"] == 2

    with pytest.raises(AlignmentError, match="without all probes"):
        _correlation_evidence(
            "fixed_offset_verified",
            mapped_records[:2],
            classification="mapped",
            support=2,
            spread=5_000,
            representative=0,
        )

    conflict_records = [
        _correlation_probe_record(20, 0, "0", 0),
        _correlation_probe_record(
            50, 500_000, "4.166666666666666666666666667", 0
        ),
        _correlation_probe_record(80, 1_000_000, "8.75", 50_000),
    ]
    conflict = _correlation_evidence(
        "fixed_offset_conflict",
        conflict_records,
        classification="conflict",
        support=2,
        spread=0,
        representative=0,
        conflicts=[0, 50_000],
    )
    assert conflict.classification == "conflict"

    with pytest.raises(AlignmentError, match="without all probes"):
        _correlation_evidence(
            "fixed_offset_conflict",
            conflict_records[:2],
            classification="conflict",
            support=2,
            spread=0,
            representative=0,
            conflicts=[0, 50_000],
        )


def test_correlation_probe_inconsistent_closed_shapes() -> None:
    valid = _correlation_evidence(
        "probe_inconsistent",
        [
            _correlation_probe_record(20, 0, "0", 0),
            _correlation_probe_record(
                50, 500_000, "4.333333333333333333333333333", 20_000
            ),
            _correlation_probe_record(
                80, 1_000_000, "8.666666666666666666666666667", 40_000
            ),
        ],
        classification="uncertain",
        support=0,
        spread=None,
        representative=None,
    )
    assert valid.evidence["code"] == "probe_inconsistent"

    with pytest.raises(AlignmentError, match="invalid inconsistent"):
        _correlation_evidence(
            "probe_inconsistent",
            [_correlation_probe_record(20, 0, "0", 0)],
            classification="uncertain",
            support=0,
            spread=None,
            representative=None,
        )


def test_auxiliary_decode_failed_requires_a_failed_probe_record() -> None:
    complete_records = [
        _correlation_probe_record(20, 0, "0", 0),
        _correlation_probe_record(
            50, 500_000, "4.166666666666666666666666667", 0
        ),
        _correlation_probe_record(80, 1_000_000, "8.333333333333334", 0),
    ]
    with pytest.raises(AlignmentError, match="without a failed probe"):
        _correlation_evidence(
            "auxiliary_decode_failed",
            complete_records,
            classification="uncertain",
            support=0,
            spread=None,
            representative=None,
        )

    failed_records = [
        _correlation_probe_record(20, 0, None, None),
        *complete_records[1:],
    ]
    accepted = _correlation_evidence(
        "auxiliary_decode_failed",
        failed_records,
        classification="uncertain",
        support=0,
        spread=None,
        representative=None,
    )
    assert accepted.evidence["code"] == "auxiliary_decode_failed"


def test_correlation_and_fingerprint_mutual_exclusion():
    # Ensure correlation evidence does not contain fingerprint fields and vice versa
    from roughcut.domain.alignment import (
        _AUDALIGN_CORRELATION_EVIDENCE_FIELDS,
        _AUDALIGN_EVIDENCE_FIELDS,
    )
    assert "matching_fingerprint_counts" not in _AUDALIGN_CORRELATION_EVIDENCE_FIELDS
    assert "probe_records" not in _AUDALIGN_EVIDENCE_FIELDS
    assert "raw_candidate_count" not in _AUDALIGN_CORRELATION_EVIDENCE_FIELDS


def test_correlation_algorithm_identity():
    algo = AlignmentAlgorithm(
        name=AUDALIGN_CORRELATION_ALGORITHM_NAME,
        version=AUDALIGN_CORRELATION_ALGORITHM_VERSION,
        upstream_commit=AUDALIGN_UPSTREAM_COMMIT,
        accuracy=None,
        num_processors=None,
        mapping_model="fixed_offset_equal_speed",
        ticks_per_second=120000,
        verification_profile=AlignmentVerificationProfile(name=AUDALIGN_CORRELATION_PROFILE_NAME, version=1),
    )
    d = algo.to_dict()
    assert "accuracy" not in d
    assert "num_processors" not in d
    # Roundtrip
    algo2 = AlignmentAlgorithm.from_dict(d)
    assert algo2.name == AUDALIGN_CORRELATION_ALGORITHM_NAME
    # Fingerprint should still have accuracy
    from roughcut.domain.alignment import AUDALIGN_UPSTREAM_COMMIT as AUD_COMMIT
    from roughcut.domain.alignment import AUDALIGN_VERSION
    fp_algo = AlignmentAlgorithm(
        name="audalign_fingerprint",
        version=AUDALIGN_VERSION,
        upstream_commit=AUD_COMMIT,
        accuracy=2,
        num_processors=1,
        mapping_model="fixed_offset_equal_speed",
        ticks_per_second=120000,
        verification_profile=AlignmentVerificationProfile(name="roughcut_audalign_fixed_offset", version=2),
    )
    assert fp_algo.to_dict()["accuracy"] == 2


def test_timeline_correlation_conflict_via_partition():
    from roughcut.application.alignments import (
        _correlation_empty_evidence,
        _waveform_partition_camera_timeline,
    )

    # Two mapped owners with divergent B >12000 should become conflict
    # Simulate main duration 10s = 1_200_000 ticks, two aux sources with different B
    main_duration = 10 * ALIGNMENT_TICKS_PER_SECOND
    evidence1 = {
        "code": "fixed_offset_verified",
        "provider": "audalign",
        "provider_version": "1.3.1",
        "recognizer": "CorrelationRecognizer",
        "probe_records": [
            {"percentage": 20, "auxiliary_start_ticks": 0, "auxiliary_end_ticks": 1_800_000, "native_offset_seconds": "0.0", "derived_b_ticks": 0},
            {"percentage": 50, "auxiliary_start_ticks": 500000, "auxiliary_end_ticks": 2_300_000, "native_offset_seconds": "4.166666666666667", "derived_b_ticks": 0},
            {"percentage": 80, "auxiliary_start_ticks": 1000000, "auxiliary_end_ticks": 2_800_000, "native_offset_seconds": "8.75", "derived_b_ticks": 50000},
        ],
        "support_probe_count": 2,
        "cluster_spread_ticks": 0,
        "representative_b_ticks": 0,
        "conflicting_b_ticks": [],
        "verification_profile": {"name": AUDALIGN_CORRELATION_PROFILE_NAME, "version": 1},
    }
    evidence2 = {
        "code": "fixed_offset_verified",
        "provider": "audalign",
        "provider_version": "1.3.1",
        "recognizer": "CorrelationRecognizer",
        "probe_records": [
            {"percentage": 20, "auxiliary_start_ticks": 0, "auxiliary_end_ticks": 1_800_000, "native_offset_seconds": "0.4166666666666667", "derived_b_ticks": 50000},
            {"percentage": 50, "auxiliary_start_ticks": 500000, "auxiliary_end_ticks": 2_300_000, "native_offset_seconds": "4.583333333333333", "derived_b_ticks": 50000},
            {"percentage": 80, "auxiliary_start_ticks": 1000000, "auxiliary_end_ticks": 2_800_000, "native_offset_seconds": "9.583333333333334", "derived_b_ticks": 100000},
        ],
        "support_probe_count": 2,
        "cluster_spread_ticks": 0,
        "representative_b_ticks": 50000,  # diff 50000 >12000
        "conflicting_b_ticks": [],
        "verification_profile": {"name": AUDALIGN_CORRELATION_PROFILE_NAME, "version": 1},
    }
    intervals: list[AlignmentInterval] = []
    mapped_segments = [
        (0, main_duration, "aux1", 0, main_duration, evidence1),
        (0, main_duration, "aux2", 0, main_duration, evidence2),
    ]
    # This should produce a conflict due to divergent B
    _mapped, _missing, _uncertain, conflict = _waveform_partition_camera_timeline(
        main_duration=main_duration,
        camera_id="B",
        main_source_id="main1",
        mapped_segments=mapped_segments,
        unverifiable_segments=[],
        intervals=intervals,
        empty_evidence=lambda: _correlation_empty_evidence("no_candidate"),
    )
    assert conflict == main_duration
    assert intervals[0].classification == "conflict"
    assert intervals[0].evidence["code"] == "fixed_offset_conflict"
    assert len(intervals[0].evidence["conflicting_b_ticks"]) == 2


def test_source_pairs_no_cartesian():
    from roughcut.application.alignments import _build_pair_groups, _pair_source_ids
    from roughcut.domain.alignment import (
        AlignmentCameraGroup,
        AlignmentSourcePair,
    )

    main = AlignmentCameraGroup(camera_id="main", ordered_source_ids=("main1", "main2"))
    aux = AlignmentCameraGroup(camera_id="B", ordered_source_ids=("aux1", "aux2"))
    # With explicit pairs, only those pairs are selected, no cartesian
    from roughcut.domain.alignment import AlignmentRequestAuxiliaryGroup

    req_groups = (
        AlignmentRequestAuxiliaryGroup(
            camera=aux,
            source_pairs=(
                AlignmentSourcePair(main_source_id="main1", auxiliary_source_id="aux1"),
            ),
        ),
    )
    selected_main, pair_groups = _build_pair_groups(main, req_groups)
    assert selected_main.ordered_source_ids == ("main1",)
    assert pair_groups[0].camera.ordered_source_ids == ("aux1",)
    # Ensure unpaired sources not in selected set
    selected_ids = _pair_source_ids(selected_main, pair_groups)
    assert "main2" not in selected_ids
    assert "aux2" not in selected_ids
    # Missing pairs for multi-source should fail closed
    aux2 = AlignmentCameraGroup(camera_id="C", ordered_source_ids=("aux2", "aux3"))
    req_groups2 = (AlignmentRequestAuxiliaryGroup(camera=aux2, source_pairs=None),)
    with pytest.raises(MediaOperationError, match="explicit source_pairs are required"):
        _build_pair_groups(main, req_groups2)
