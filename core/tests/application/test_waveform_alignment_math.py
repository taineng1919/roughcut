from __future__ import annotations

import math
from pathlib import Path

import roughcut.application.waveform_alignment as waveform
from roughcut.application.waveform_alignment import (
    WAVEFORM_COARSE_BIN_TICKS,
    CoarseLocalization,
    WaveformEnvelope,
    analyze_waveform_pair,
    localize_envelope,
    normalized_window_correlation,
)


def _base() -> list[float]:
    return [
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
    ]


def test_coarse_localization_preserves_positive_and_negative_b() -> None:
    base = _base()
    positive = localize_envelope(base, base[2:14])
    assert isinstance(positive, CoarseLocalization)
    assert positive.accepted is True
    assert positive.b_ticks == 2 * WAVEFORM_COARSE_BIN_TICKS

    negative = localize_envelope(base[2:14], base)
    assert isinstance(negative, CoarseLocalization)
    assert negative.accepted is True
    assert negative.b_ticks == -2 * WAVEFORM_COARSE_BIN_TICKS


def test_coarse_localization_marks_a_flat_peak_ambiguous() -> None:
    result = localize_envelope([1.0] * 8, [1.0] * 5)
    assert isinstance(result, CoarseLocalization)
    assert result.accepted is False
    assert result.separation == 0.0


def test_mono_failure_uses_shared_envelope_and_bounded_left_windows() -> None:
    coarse_main = [0.0] * 19
    coarse_main[3] = 1.0
    coarse_auxiliary = coarse_main[2:14]
    flat = (1.0,) * 19
    flat_auxiliary = flat[:12]
    main = WaveformEnvelope(
        mono=flat,
        left=tuple(coarse_main),
        right=flat,
    )
    auxiliary = WaveformEnvelope(
        mono=flat_auxiliary,
        left=tuple(coarse_auxiliary),
        right=flat_auxiliary,
    )
    calls: list[tuple[str, str]] = []
    samples = [((index * 37) % 101) / 100.0 for index in range(64)]

    def reader(path: Path, _start: int, _end: int, channel: str) -> list[float]:
        calls.append((path.name, channel))
        return samples

    result = analyze_waveform_pair(
        main_path=Path("main.wav"),
        auxiliary_path=Path("aux.wav"),
        main_envelope=main,
        auxiliary_envelope=auxiliary,
        main_duration_ticks=7_200_000,
        auxiliary_duration_ticks=7_200_000,
        window_reader=reader,
    )
    assert result["classifications"] == "mapped"
    assert result["b_ticks"] == 2 * WAVEFORM_COARSE_BIN_TICKS
    assert calls and {channel for _path, channel in calls} == {"left"}
    assert len(calls) == 6
    evidence = result["evidence"]
    assert isinstance(evidence, dict)
    assert isinstance(evidence["coarse_peak"]["score"], str)
    assert isinstance(evidence["refined_correlation"], str)


def test_three_window_inconsistency_closes_as_conflict(
    monkeypatch,
) -> None:
    coarse_main = [0.0] * 19
    coarse_main[3] = 1.0
    coarse_auxiliary = coarse_main[2:14]
    envelope = WaveformEnvelope(
        mono=tuple([1.0] * 19),
        left=tuple(coarse_main),
        right=tuple([1.0] * 19),
    )
    auxiliary = WaveformEnvelope(
        mono=tuple([1.0] * 12),
        left=tuple(coarse_auxiliary),
        right=tuple([1.0] * 12),
    )
    correlations = iter(
        (
            waveform.WindowCorrelation(240_000, 1.0),
            waveform.WindowCorrelation(240_000, 1.0),
            waveform.WindowCorrelation(480_000, 1.0),
        )
    )
    monkeypatch.setattr(
        waveform,
        "normalized_window_correlation",
        lambda *_args, **_kwargs: next(correlations),
    )
    result = waveform.analyze_waveform_pair(
        main_path=Path("main.wav"),
        auxiliary_path=Path("aux.wav"),
        main_envelope=envelope,
        auxiliary_envelope=auxiliary,
        main_duration_ticks=7_200_000,
        auxiliary_duration_ticks=7_200_000,
        window_reader=lambda *_args: [0.0, 1.0, 0.0, 0.5] * 16,
    )
    assert result["classifications"] == "conflict"
    evidence = result["evidence"]
    assert evidence["code"] == "fixed_offset_conflict"
    assert evidence["conflicting_b_ticks"] == [240_000, 480_000]


def test_short_window_correlation_returns_real_offset_and_normalized_score() -> None:
    main = [0.0, 0.2, 1.0, 0.1, 0.0, 0.4, 0.8, 0.0]
    auxiliary = [1.0, 0.1, 0.0, 0.4, 0.8, 0.0]
    result = normalized_window_correlation(
        main,
        auxiliary,
        main_start_ticks=2_000,
        auxiliary_start_ticks=0,
        sample_rate_hz=100,
        max_lag_samples=300,
    )
    # M0 - A0 + Δ: the matching main sample is two 100 Hz samples later.
    assert result.b_ticks == 4_400
    assert result.correlation > 0.98
    assert abs(result.local_error_ticks) <= 12_000


def _noise(seed: int, count: int) -> list[float]:
    state = (seed * 2654435761 + 12345) % (1 << 31)
    values: list[float] = []
    for _ in range(count):
        state = (state * 1103515245 + 12345) % (1 << 31)
        values.append(state / (1 << 31))
    return values


def test_device_noise_localizes_known_positive_and_negative_lag() -> None:
    """Shared content observed through independent device noise must
    localize to the true lag in both directions."""
    for seed in (4, 8):
        shared = _noise(seed, 600)
        device_main = _noise(seed + 101, 600)
        device_auxiliary = _noise(seed + 202, 600)
        main_envelope = [
            sample + 0.5 * noise
            for sample, noise in zip(shared, device_main)
        ]
        auxiliary_envelope = [
            sample + 0.5 * noise
            for sample, noise in zip(shared[40:], device_auxiliary[:560])
        ]
        positive = localize_envelope(main_envelope, auxiliary_envelope)
        assert isinstance(positive, CoarseLocalization)
        assert positive.accepted is True
        assert positive.peak_lag_bins == 40

        negative = localize_envelope(auxiliary_envelope, main_envelope)
        assert isinstance(negative, CoarseLocalization)
        assert negative.accepted is True
        assert negative.peak_lag_bins == -40


def test_edge_small_overlap_candidates_cannot_become_the_peak() -> None:
    """A perfect few-bin motif match at an extreme lag never competes:
    without the support rule this alignment scored .91-.95 and was falsely
    accepted instead of the true interior offset."""
    for seed in (4, 8):
        motif = [0.9, 0.1, 0.95, 0.05, 0.9, 0.1]
        main_envelope = _noise(seed, 400)
        main_envelope[-6:] = motif
        auxiliary_envelope = _noise(seed + 77, 300)
        auxiliary_envelope[:6] = motif
        result = localize_envelope(main_envelope, auxiliary_envelope)
        assert isinstance(result, CoarseLocalization)
        # the motif-only alignment sits at lag len(main)-6 with six bins of
        # overlap; it sits below the candidate support floor and must never
        # be selected, let alone accepted
        motif_lag_bins = len(main_envelope) - 6
        assert result.peak_lag_bins != motif_lag_bins
        if result.accepted:
            # whatever supported interior lag wins, separation stays real
            assert result.separation >= 0.05
            assert result.peak_score >= 0.20


def test_minimally_supported_true_offset_still_wins_the_peak() -> None:
    """A five-bin auxiliary against a longer main keeps its true-lag
    candidate: the support floor is half of the smaller side. With so few
    supporting bins the separation gate cannot be demonstrated, so the
    result stays fail-closed even though localization picked the truth."""
    main_envelope = _noise(31, 40)
    true_lag_bins = 17
    auxiliary_envelope = main_envelope[
        true_lag_bins : true_lag_bins + 5
    ]
    result = localize_envelope(main_envelope, auxiliary_envelope)
    assert isinstance(result, CoarseLocalization)
    assert result.peak_lag_bins == true_lag_bins
    assert result.b_ticks == true_lag_bins * WAVEFORM_COARSE_BIN_TICKS


def _sequence(seed: int, count: int) -> list[float]:
    state = (seed * 2654435761 + 12345) % (1 << 31)
    values: list[float] = []
    for _ in range(count):
        state = (state * 1103515245 + 12345) % (1 << 31)
        values.append(state / (1 << 31))
    return values



def test_verification_confirms_refined_offset_and_ignores_far_peaks(
    monkeypatch,
) -> None:
    """Opening/ending windows are anchored at refined.b_ticks and may only
    search within the frozen 12000-tick local-error gate: a stronger far
    peak inside the historical +/-3 s verification range stays unreachable,
    while a genuine local match passes."""
    from roughcut.application.waveform_alignment import (
        ALIGNMENT_MAXIMUM_LOCAL_ERROR_TICKS,
        _max_lag_samples,
        _verification_max_lag_samples,
    )

    assert _max_lag_samples() == 750
    assert _verification_max_lag_samples() == math.ceil(
        ALIGNMENT_MAXIMUM_LOCAL_ERROR_TICKS * 250 / 120_000
    ) == 25

    monkeypatch.setattr(
        waveform,
        "localize_envelope",
        lambda _main, _auxiliary: CoarseLocalization(
            b_ticks=0,
            peak_lag_bins=0,
            peak_score=0.9,
            runner_up_lag_bins=None,
            runner_up_score=None,
            separation=0.0,
            accepted=True,
        ),
    )

    calls: list[dict[str, int]] = []
    real_correlation = waveform.normalized_window_correlation

    def recording_correlation(
        main,
        auxiliary,
        *,
        main_start_ticks,
        auxiliary_start_ticks,
        sample_rate_hz,
        max_lag_samples,
    ):
        calls.append(
            {
                "main_start": main_start_ticks,
                "aux_start": auxiliary_start_ticks,
                "max_lag": max_lag_samples,
            }
        )
        return real_correlation(
            main,
            auxiliary,
            main_start_ticks=main_start_ticks,
            auxiliary_start_ticks=auxiliary_start_ticks,
            sample_rate_hz=sample_rate_hz,
            max_lag_samples=max_lag_samples,
        )

    monkeypatch.setattr(
        waveform, "normalized_window_correlation", recording_correlation
    )

    # refinement (middle window): clean local match two derived bins later
    main_middle = _sequence(51, 800)
    auxiliary_middle = [0.9 * value for value in main_middle[2:]] + [0.0, 0.0]

    # verification data: a real local match at the refined offset PLUS a
    # stronger decoy block 300 bins (1.2 s) away that the historical free
    # search would lock onto
    def poisoned_pair() -> tuple[list[float], list[float]]:
        main_window = _sequence(77, 800)
        auxiliary_window = []
        for index in range(800):
            value = 0.75 * main_window[index]
            if index + 300 < len(main_window):
                value += 0.80 * main_window[index + 300]
            auxiliary_window.append(value)
        return main_window, auxiliary_window

    envelope_values = (1.0,) * 40
    envelope = WaveformEnvelope(
        mono=envelope_values, left=envelope_values, right=envelope_values
    )

    main_opening, opening_auxiliary = poisoned_pair()
    main_ending, ending_auxiliary = poisoned_pair()

    # fixture validity: with the historical free range the decoy genuinely
    # wins and lands far outside the local-error gate
    unbounded = real_correlation(
        main_opening,
        opening_auxiliary,
        main_start_ticks=0,
        auxiliary_start_ticks=0,
        sample_rate_hz=250,
        max_lag_samples=_max_lag_samples(),
    )
    assert abs(unbounded.b_ticks) > 10 * ALIGNMENT_MAXIMUM_LOCAL_ERROR_TICKS

    def reader(path: Path, start_ticks: int, _end_ticks: int, _channel: str):
        if start_ticks == 3_000_000:
            return (
                main_middle if path.name == "main.wav" else auxiliary_middle
            )
        if start_ticks == 0:
            return (
                main_opening
                if path.name == "main.wav"
                else opening_auxiliary
            )
        return (
            main_ending if path.name == "main.wav" else ending_auxiliary
        )

    result = analyze_waveform_pair(
        main_path=Path("main.wav"),
        auxiliary_path=Path("aux.wav"),
        main_envelope=envelope,
        auxiliary_envelope=envelope,
        main_duration_ticks=7_200_000,
        auxiliary_duration_ticks=7_200_000,
        window_reader=reader,
    )
    assert result["classifications"] == "mapped"
    refined_b = int(result["b_ticks"])
    assert abs(refined_b - 960) <= 480
    evidence = result["evidence"]
    assert evidence["code"] == "fixed_offset_verified"
    windows = evidence["verification_windows"]
    assert len(windows) == 2
    for window in windows:
        assert abs(window["b_ticks"] - refined_b) <= (
            ALIGNMENT_MAXIMUM_LOCAL_ERROR_TICKS
        )
        # the far decoy sits 1.2 s away and must not appear anywhere
        assert abs(window["b_ticks"]) < ALIGNMENT_MAXIMUM_LOCAL_ERROR_TICKS

    # wiring proof: one full-range refinement call followed by two
    # gate-bounded confirmation calls anchored on the refined offset
    assert len(calls) == 3
    assert calls[0]["max_lag"] == 750
    for call in calls[1:]:
        assert call["max_lag"] == 25
        expected_anchor = call["main_start"] - refined_b
        assert call["aux_start"] == expected_anchor
