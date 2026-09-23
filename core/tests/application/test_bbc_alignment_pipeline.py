from __future__ import annotations

from pathlib import Path

import pytest

import roughcut.application.bbc_alignment as bbc
from roughcut.application.waveform_alignment import (
    WaveformDecodeError,
    WindowCorrelation,
    waveform_windows,
)
from roughcut.domain.alignment import (
    ALIGNMENT_MAXIMUM_LOCAL_ERROR_TICKS,
    WAVEFORM_MINIMUM_CORRELATION,
)


def _reader_calls() -> tuple[
    list[tuple[str, int, int, str]], object
]:
    calls: list[tuple[str, int, int, str]] = []

    def reader(path: Path, start: int, end: int, channel: str) -> list[float]:
        calls.append((path.name, start, end, channel))
        return [0.0, 1.0, 0.25, 0.75]

    return calls, reader


def test_refined_b_rebuilds_overlap_and_all_three_verification_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial_b = -360_000
    refined_b = 360_000
    main_duration = 8_400_000
    auxiliary_duration = 7_800_000
    correlations = iter(
        [
            WindowCorrelation(refined_b, 0.91),
            WindowCorrelation(refined_b, 0.92),
            WindowCorrelation(refined_b, 0.93),
            WindowCorrelation(refined_b, 0.94),
        ]
    )
    monkeypatch.setattr(
        bbc,
        "normalized_window_correlation",
        lambda *_args, **_kwargs: next(correlations),
    )
    calls, reader = _reader_calls()

    result = bbc.analyze_bbc_pair(
        main_path=Path("main.wav"),
        auxiliary_path=Path("aux.wav"),
        main_duration_ticks=main_duration,
        auxiliary_duration_ticks=auxiliary_duration,
        native_offset_seconds="3",
        initial_b_ticks=initial_b,
        standard_score="99.5",
        window_reader=reader,
    )

    assert result["classifications"] == "mapped"
    assert result["b_ticks"] == refined_b
    evidence = result["evidence"]
    assert evidence["native_offset_seconds"] == "3"
    assert evidence["initial_b_ticks"] == initial_b
    assert evidence["standard_score"] == "99.5"
    assert evidence["refined_b_ticks"] == refined_b
    assert evidence["refined_geometry"] == {
        "main_start_ticks": refined_b,
        "main_end_ticks": 8_160_000,
        "auxiliary_start_ticks": 0,
        "auxiliary_end_ticks": 7_800_000,
    }

    expected_windows = waveform_windows(refined_b, 8_160_000)
    assert expected_windows is not None
    # First two reads are the initial-geometry refinement pair. Every later
    # read must be rebuilt from refined geometry, including the middle window.
    verification_reads = calls[2:]
    assert len(verification_reads) == 6
    assert [item[1:3] for item in verification_reads[::2]] == list(
        expected_windows
    )
    assert [item[1] for item in verification_reads[1::2]] == [
        start - refined_b for start, _end in expected_windows
    ]
    assert [item["label"] for item in evidence["verification_windows"]] == [
        "opening",
        "middle",
        "ending",
    ]


@pytest.mark.parametrize(
    ("correlation", "local_error", "expected"),
    [
        (WAVEFORM_MINIMUM_CORRELATION, ALIGNMENT_MAXIMUM_LOCAL_ERROR_TICKS, "mapped"),
        (WAVEFORM_MINIMUM_CORRELATION - 0.0001, 0, "uncertain"),
        (0.9, ALIGNMENT_MAXIMUM_LOCAL_ERROR_TICKS + 1, "uncertain"),
    ],
)
def test_three_window_threshold_boundaries_are_closed(
    monkeypatch: pytest.MonkeyPatch,
    correlation: float,
    local_error: int,
    expected: str,
) -> None:
    refined_b = 240_000
    verification_b = refined_b + local_error
    channel_results = [
        WindowCorrelation(refined_b, 0.9),
        WindowCorrelation(verification_b, correlation),
        WindowCorrelation(refined_b, 0.9),
        WindowCorrelation(refined_b, 0.9),
    ]
    correlations = iter(
        channel_results
        if expected == "mapped"
        else [
            item
            for _channel in range(3)
            for item in channel_results[:2]
        ]
    )
    monkeypatch.setattr(
        bbc,
        "normalized_window_correlation",
        lambda *_args, **_kwargs: next(correlations),
    )
    _calls, reader = _reader_calls()
    result = bbc.analyze_bbc_pair(
        main_path=Path("main.wav"),
        auxiliary_path=Path("aux.wav"),
        main_duration_ticks=8_400_000,
        auxiliary_duration_ticks=8_400_000,
        native_offset_seconds="2",
        initial_b_ticks=refined_b,
        standard_score="1",
        window_reader=reader,
    )
    assert result["classifications"] == expected


@pytest.mark.parametrize("initial_b", [-240_000, 240_000])
def test_native_b_sign_is_not_inverted(
    monkeypatch: pytest.MonkeyPatch, initial_b: int
) -> None:
    monkeypatch.setattr(
        bbc,
        "normalized_window_correlation",
        lambda *_args, **_kwargs: WindowCorrelation(initial_b, 1.0),
    )
    _calls, reader = _reader_calls()
    result = bbc.analyze_bbc_pair(
        main_path=Path("main.wav"),
        auxiliary_path=Path("aux.wav"),
        main_duration_ticks=8_400_000,
        auxiliary_duration_ticks=8_400_000,
        native_offset_seconds="-2" if initial_b < 0 else "2",
        initial_b_ticks=initial_b,
        standard_score="5",
        window_reader=reader,
    )
    assert result["classifications"] == "mapped"
    assert result["b_ticks"] == initial_b
    assert result["evidence"]["initial_b_ticks"] == initial_b


def test_non_identical_stereo_falls_back_from_mono_to_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    correlations = iter(
        [
            WindowCorrelation(0, WAVEFORM_MINIMUM_CORRELATION - 0.01),
            WindowCorrelation(0, 0.9),
            WindowCorrelation(0, 0.91),
            WindowCorrelation(0, 0.92),
            WindowCorrelation(0, 0.93),
        ]
    )
    monkeypatch.setattr(
        bbc,
        "normalized_window_correlation",
        lambda *_args, **_kwargs: next(correlations),
    )
    calls, reader = _reader_calls()
    result = bbc.analyze_bbc_pair(
        main_path=Path("main.wav"),
        auxiliary_path=Path("aux.wav"),
        main_duration_ticks=8_400_000,
        auxiliary_duration_ticks=8_400_000,
        native_offset_seconds="0",
        initial_b_ticks=0,
        standard_score="-100",
        window_reader=reader,
    )
    assert result["classifications"] == "mapped"
    assert {call[3] for call in calls} == {"mono", "left"}
    assert all(call[3] != "right" for call in calls)


@pytest.mark.parametrize("failed_window", [0, 1, 2])
def test_verification_failure_falls_back_to_left_without_mixing_evidence(
    monkeypatch: pytest.MonkeyPatch, failed_window: int
) -> None:
    mono_verification = [WindowCorrelation(0, 0.9) for _ in range(3)]
    mono_verification[failed_window] = WindowCorrelation(0, 0.34)
    correlations = iter(
        [WindowCorrelation(0, 0.9), *mono_verification]
        + [WindowCorrelation(0, 0.91)] * 4
    )
    monkeypatch.setattr(
        bbc,
        "normalized_window_correlation",
        lambda *_args, **_kwargs: next(correlations),
    )
    calls, reader = _reader_calls()
    result = bbc.analyze_bbc_pair(
        main_path=Path("main.wav"),
        auxiliary_path=Path("aux.wav"),
        main_duration_ticks=8_400_000,
        auxiliary_duration_ticks=8_400_000,
        native_offset_seconds="0",
        initial_b_ticks=0,
        standard_score="1",
        window_reader=reader,
    )
    assert result["classifications"] == "mapped"
    assert result["evidence"]["channel"] == "left"
    assert len(result["evidence"]["verification_windows"]) == 3
    assert {call[3] for call in calls} == {"mono", "left"}


def test_refined_overlap_failure_falls_back_to_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    correlations = iter(
        [WindowCorrelation(4_200_001, 0.9)]
        + [WindowCorrelation(0, 0.91)] * 4
    )
    monkeypatch.setattr(
        bbc,
        "normalized_window_correlation",
        lambda *_args, **_kwargs: next(correlations),
    )
    calls, reader = _reader_calls()
    result = bbc.analyze_bbc_pair(
        main_path=Path("main.wav"),
        auxiliary_path=Path("aux.wav"),
        main_duration_ticks=8_400_000,
        auxiliary_duration_ticks=8_400_000,
        native_offset_seconds="0",
        initial_b_ticks=0,
        standard_score="1",
        window_reader=reader,
    )
    assert result["classifications"] == "mapped"
    assert result["evidence"]["channel"] == "left"
    assert {call[3] for call in calls} == {"mono", "left"}


def test_mono_refinement_read_failure_falls_back_to_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bbc,
        "normalized_window_correlation",
        lambda *_args, **_kwargs: WindowCorrelation(0, 0.9),
    )

    def reader(_path: Path, _start: int, _end: int, channel: str) -> list[float]:
        if channel == "mono":
            raise WaveformDecodeError("deterministic decode failure")
        return [0.0, 1.0, 0.25, 0.75]

    result = bbc.analyze_bbc_pair(
        main_path=Path("main.wav"),
        auxiliary_path=Path("aux.wav"),
        main_duration_ticks=8_400_000,
        auxiliary_duration_ticks=8_400_000,
        native_offset_seconds="0",
        initial_b_ticks=0,
        standard_score="1",
        window_reader=reader,
    )
    assert result["classifications"] == "mapped"
    assert result["evidence"]["channel"] == "left"
    assert result["evidence"]["code"] == "fixed_offset_verified"


def test_mono_refinement_correlation_failure_falls_back_to_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def correlation(*_args: object, **_kwargs: object) -> WindowCorrelation:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise WaveformDecodeError("invalid mono correlation input")
        return WindowCorrelation(0, 0.9)

    monkeypatch.setattr(bbc, "normalized_window_correlation", correlation)
    _calls, reader = _reader_calls()
    result = bbc.analyze_bbc_pair(
        main_path=Path("main.wav"),
        auxiliary_path=Path("aux.wav"),
        main_duration_ticks=8_400_000,
        auxiliary_duration_ticks=8_400_000,
        native_offset_seconds="0",
        initial_b_ticks=0,
        standard_score="1",
        window_reader=reader,
    )
    assert result["classifications"] == "mapped"
    assert result["evidence"]["channel"] == "left"


def test_all_channel_refinement_decode_failures_keep_only_right_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reader(_path: Path, _start: int, _end: int, _channel: str) -> list[float]:
        raise WaveformDecodeError("deterministic decode failure")

    result = bbc.analyze_bbc_pair(
        main_path=Path("main.wav"),
        auxiliary_path=Path("aux.wav"),
        main_duration_ticks=8_400_000,
        auxiliary_duration_ticks=8_400_000,
        native_offset_seconds="0",
        initial_b_ticks=0,
        standard_score="1",
        window_reader=reader,
    )
    assert result["classifications"] == "uncertain"
    evidence = result["evidence"]
    assert evidence["code"] == "decode_failed"
    assert evidence["channel"] == "right"
    assert evidence["refined_b_ticks"] is None
    assert evidence["verification_windows"] == []


def test_verification_correlation_decode_failure_falls_back_to_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def correlation(*_args: object, **_kwargs: object) -> WindowCorrelation:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise WaveformDecodeError("invalid mono verification input")
        return WindowCorrelation(0, 0.9)

    monkeypatch.setattr(bbc, "normalized_window_correlation", correlation)
    _calls, reader = _reader_calls()
    result = bbc.analyze_bbc_pair(
        main_path=Path("main.wav"),
        auxiliary_path=Path("aux.wav"),
        main_duration_ticks=8_400_000,
        auxiliary_duration_ticks=8_400_000,
        native_offset_seconds="0",
        initial_b_ticks=0,
        standard_score="1",
        window_reader=reader,
    )
    assert result["classifications"] == "mapped"
    assert result["evidence"]["channel"] == "left"


def test_mono_verification_read_failure_falls_back_to_left_without_mixing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bbc,
        "normalized_window_correlation",
        lambda *_args, **_kwargs: WindowCorrelation(0, 0.9),
    )
    mono_reads = 0

    def reader(_path: Path, _start: int, _end: int, channel: str) -> list[float]:
        nonlocal mono_reads
        if channel == "mono":
            mono_reads += 1
            if mono_reads == 5:
                raise WaveformDecodeError("middle verification decode failure")
        return [0.0, 1.0, 0.25, 0.75]

    result = bbc.analyze_bbc_pair(
        main_path=Path("main.wav"),
        auxiliary_path=Path("aux.wav"),
        main_duration_ticks=8_400_000,
        auxiliary_duration_ticks=8_400_000,
        native_offset_seconds="0",
        initial_b_ticks=0,
        standard_score="1",
        window_reader=reader,
    )
    assert result["classifications"] == "mapped"
    assert result["evidence"]["channel"] == "left"
    assert len(result["evidence"]["verification_windows"]) == 3


def test_all_channel_verification_correlation_failures_keep_final_stage_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def correlation(*_args: object, **_kwargs: object) -> WindowCorrelation:
        nonlocal calls
        calls += 1
        if calls % 2 == 0:
            raise WaveformDecodeError("invalid verification input")
        return WindowCorrelation(0, 0.9)

    monkeypatch.setattr(bbc, "normalized_window_correlation", correlation)
    _calls, reader = _reader_calls()
    result = bbc.analyze_bbc_pair(
        main_path=Path("main.wav"),
        auxiliary_path=Path("aux.wav"),
        main_duration_ticks=8_400_000,
        auxiliary_duration_ticks=8_400_000,
        native_offset_seconds="0",
        initial_b_ticks=0,
        standard_score="1",
        window_reader=reader,
    )
    assert result["classifications"] == "uncertain"
    evidence = result["evidence"]
    assert evidence["code"] == "decode_failed"
    assert evidence["channel"] == "right"
    assert evidence["refined_b_ticks"] == 0
    assert evidence["refined_geometry"] is not None
    assert evidence["verification_windows"] == []


@pytest.mark.parametrize(
    ("duration", "expected"),
    [(4_320_000, "mapped"), (4_319_999, "uncertain")],
)
def test_minimum_overlap_boundary(
    monkeypatch: pytest.MonkeyPatch, duration: int, expected: str
) -> None:
    monkeypatch.setattr(
        bbc,
        "normalized_window_correlation",
        lambda *_args, **_kwargs: WindowCorrelation(0, 1.0),
    )
    _calls, reader = _reader_calls()
    result = bbc.analyze_bbc_pair(
        main_path=Path("main.wav"),
        auxiliary_path=Path("aux.wav"),
        main_duration_ticks=duration,
        auxiliary_duration_ticks=duration,
        native_offset_seconds="0",
        initial_b_ticks=0,
        standard_score="-100",
        window_reader=reader,
    )
    assert result["classifications"] == expected
