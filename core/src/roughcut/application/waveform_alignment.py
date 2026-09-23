"""Small, closed waveform alignment path for caller-declared source pairs.

The path keeps only one low-rate envelope per selected Source and extracts
three short windows around the already localized overlap. It never writes a
full-track PCM/WAV and never calls the historical Audalign adapter.
"""

from __future__ import annotations

import math
import operator
import sys
from array import array
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from roughcut.adapters.child_budget import ChildBudget, run_bounded_child_stream
from roughcut.domain.alignment import (
    ALIGNMENT_MAXIMUM_LOCAL_ERROR_TICKS,
    ALIGNMENT_MINIMUM_VERIFIED_OVERLAP_TICKS,
    ALIGNMENT_TICKS_PER_SECOND,
    WAVEFORM_COARSE_BIN_TICKS,
    WAVEFORM_COARSE_DECODE_SAMPLE_RATE_HZ,
    WAVEFORM_COARSE_MINIMUM_OVERLAP_BINS,
    WAVEFORM_COARSE_MINIMUM_SCORE,
    WAVEFORM_COARSE_MINIMUM_SEPARATION,
    WAVEFORM_COARSE_RUNNER_UP_EXCLUSION_BINS,
    WAVEFORM_MAX_REFINEMENT_LAG_TICKS,
    WAVEFORM_MINIMUM_CORRELATION,
    WAVEFORM_PROFILE_NAME,
    WAVEFORM_PROFILE_VERSION,
    WAVEFORM_SHORT_WINDOW_BLOCK_SAMPLES,
    WAVEFORM_SHORT_WINDOW_DECODE_SAMPLE_RATE_HZ,
    WAVEFORM_SHORT_WINDOW_DERIVED_SAMPLE_RATE_HZ,
    WAVEFORM_WINDOW_COUNT,
    WAVEFORM_WINDOW_TICKS,
    WAVEFORM_WRITER_PROFILE,
)

# The complete write identity for this path is frozen in the domain
# (``WAVEFORM_WRITER_PROFILE``); this module re-exports it so producer code
# and tests read one literal from one home.
WAVEFORM_PROFILE_CANONICAL = WAVEFORM_WRITER_PROFILE
# One coarse RMS bin covers exactly one second of decoded audio.
WAVEFORM_COARSE_BIN_SAMPLES = (
    WAVEFORM_COARSE_DECODE_SAMPLE_RATE_HZ
    * WAVEFORM_COARSE_BIN_TICKS
    // ALIGNMENT_TICKS_PER_SECOND
)
WAVEFORM_WORKSPACE_OVERHEAD_BYTES = 16 * 1024 * 1024
WAVEFORM_SHORT_WINDOW_MEMORY_BYTES = (
    2
    * (
        WAVEFORM_WINDOW_TICKS
        * WAVEFORM_SHORT_WINDOW_DERIVED_SAMPLE_RATE_HZ
        // ALIGNMENT_TICKS_PER_SECOND
    )
    * 8
)


class WaveformDecodeError(RuntimeError):
    """FFmpeg did not produce a bounded waveform stream."""


@dataclass(frozen=True)
class WaveformEnvelope:
    mono: tuple[float, ...]
    left: tuple[float, ...]
    right: tuple[float, ...]

    def channel(self, name: str) -> tuple[float, ...]:
        if name == "mono":
            return self.mono
        if name == "left":
            return self.left
        if name == "right":
            return self.right
        raise WaveformDecodeError("unknown waveform analysis channel")


@dataclass(frozen=True)
class CoarseLocalization:
    b_ticks: int
    peak_lag_bins: int
    peak_score: float
    runner_up_lag_bins: int | None
    runner_up_score: float | None
    separation: float
    accepted: bool


@dataclass(frozen=True)
class WindowCorrelation:
    b_ticks: int
    correlation: float
    local_error_ticks: int = 0


WindowReader = Callable[[Path, int, int, str], list[float]]


def _round_ratio_to_ticks(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        raise WaveformDecodeError("waveform sample rate is invalid")
    sign = -1 if numerator < 0 else 1
    magnitude = abs(numerator)
    quotient, remainder = divmod(magnitude, denominator)
    if remainder * 2 >= denominator:
        quotient += 1
    return sign * quotient


def _samples_to_ticks(samples: int, sample_rate_hz: int) -> int:
    return _round_ratio_to_ticks(
        samples * ALIGNMENT_TICKS_PER_SECOND,
        sample_rate_hz,
    )


def _number_text(value: float) -> str:
    return format(value, ".17g")


def _ticks_text(ticks: int) -> str:
    """Exact seconds text for FFmpeg seek/duration options.

    Only trailing zeros AFTER a decimal point are removed; integer seconds
    keep their significant zeros (``10`` stays ``10``, never ``1``).
    """
    value = Decimal(ticks) / Decimal(ALIGNMENT_TICKS_PER_SECOND)
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _float_values(raw: bytes) -> array[Any]:
    if len(raw) % 4 != 0:
        raise WaveformDecodeError("FFmpeg returned an incomplete float sample")
    values = array("f")
    values.frombytes(raw)
    if sys.byteorder != "little":
        values.byteswap()
    return values


class _EnvelopeAccumulator:
    """Full-speech-band streaming RMS envelope.

    FFmpeg decodes one stereo stream at the frozen coarse decode rate
    (``WAVEFORM_COARSE_DECODE_SAMPLE_RATE_HZ``); this accumulator folds each
    ``WAVEFORM_COARSE_BIN_SAMPLES``-frame second into three RMS values
    (mono/left/right) and keeps only the bins. The interleaved PCM tail is
    bounded to a single bin, so neither a full-track PCM nor a WAV file is
    ever resident or written.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        # interleaved stereo floats awaiting a complete coarse bin
        self._tail: array[Any] = array("f")
        self._mono: list[float] = []
        self._left: list[float] = []
        self._right: list[float] = []

    def feed(self, raw: bytes) -> None:
        self._buffer.extend(raw)
        usable = len(self._buffer) - (len(self._buffer) % 4)
        if usable <= 0:
            return
        values = _float_values(bytes(self._buffer[:usable]))
        del self._buffer[:usable]
        self._tail.extend(values)
        bin_samples = 2 * WAVEFORM_COARSE_BIN_SAMPLES
        while len(self._tail) >= bin_samples:
            block = self._tail[:bin_samples]
            del self._tail[:bin_samples]
            self._append_bin(block)

    def _append_bin(self, block: array[Any]) -> None:
        left = block[0::2]
        right = block[1::2]
        count = float(len(left))
        left_energy = float(sum(map(operator.mul, left, left)))
        right_energy = float(sum(map(operator.mul, right, right)))
        cross = float(sum(map(operator.mul, left, right)))
        mono_square = 0.25 * (left_energy + right_energy + 2.0 * cross)
        self._left.append(math.sqrt(left_energy / count))
        self._right.append(math.sqrt(right_energy / count))
        self._mono.append(math.sqrt(max(0.0, mono_square) / count))

    def finish(self) -> WaveformEnvelope:
        if self._buffer:
            # A complete FFmpeg f32le sample is required. The stream runner
            # has already drained the child, so this is a closed decode error.
            raise WaveformDecodeError("FFmpeg returned a partial stereo frame")
        if len(self._tail) >= 2:
            # The final partial second still contributes its own RMS bin,
            # matching the historical ceil(duration) bin count.
            self._append_bin(self._tail)
        elif self._tail:
            raise WaveformDecodeError("FFmpeg returned a partial stereo frame")
        if not self._mono:
            raise WaveformDecodeError("FFmpeg returned no audio envelope")
        return WaveformEnvelope(
            mono=tuple(self._mono),
            left=tuple(self._left),
            right=tuple(self._right),
        )


class _WindowEnvelopeAccumulator:
    """Full-band window decoder folded into bounded block-RMS values.

    FFmpeg streams the short window as full-speech-band mono PCM at
    ``WAVEFORM_SHORT_WINDOW_DECODE_SAMPLE_RATE_HZ``; this accumulator folds
    every ``WAVEFORM_SHORT_WINDOW_BLOCK_SAMPLES`` samples into one RMS value
    and keeps only the derived values plus a sub-block tail, so no raw PCM is
    ever fully resident.
    """

    def __init__(self, *, maximum_values: int) -> None:
        self._buffer = bytearray()
        self._tail: array[Any] = array("f")
        self._values: list[float] = []
        self._maximum_values = maximum_values

    def feed(self, raw: bytes) -> None:
        self._buffer.extend(raw)
        usable = len(self._buffer) - (len(self._buffer) % 4)
        if usable <= 0:
            return
        values = _float_values(bytes(self._buffer[:usable]))
        del self._buffer[:usable]
        self._tail.extend(values)
        block = WAVEFORM_SHORT_WINDOW_BLOCK_SAMPLES
        while len(self._tail) >= block:
            chunk = self._tail[:block]
            del self._tail[:block]
            energy = float(sum(map(operator.mul, chunk, chunk)))
            self._values.append(math.sqrt(energy / block))
            if len(self._values) > self._maximum_values:
                raise WaveformDecodeError("FFmpeg short window exceeded its bound")

    def finish(self) -> list[float]:
        if self._buffer:
            raise WaveformDecodeError("FFmpeg returned a partial window sample")
        if self._tail:
            # The final partial block still contributes its own RMS value,
            # matching ceil(duration) value counts at the derived rate.
            energy = float(sum(map(operator.mul, self._tail, self._tail)))
            self._values.append(math.sqrt(energy / len(self._tail)))
            if len(self._values) > self._maximum_values:
                raise WaveformDecodeError("FFmpeg short window exceeded its bound")
        if not self._values:
            raise WaveformDecodeError("FFmpeg returned an empty short window")
        return self._values


def stream_source_envelope(
    source_path: Path,
    *,
    ffmpeg_command: str,
    budget: ChildBudget,
) -> WaveformEnvelope:
    """Stream one Source once as a full-speech-band stereo envelope."""
    accumulator = _EnvelopeAccumulator()
    command = [
        ffmpeg_command,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        str(source_path),
        "-map",
        "0:a:0",
        "-map_metadata",
        "-1",
        "-vn",
        "-ac",
        "2",
        "-ar",
        str(WAVEFORM_COARSE_DECODE_SAMPLE_RATE_HZ),
        "-c:a",
        "pcm_f32le",
        "-f",
        "f32le",
        "pipe:1",
    ]
    result = run_bounded_child_stream(
        command,
        budget=budget,
        on_stdout=accumulator.feed,
    )
    if result.returncode != 0:
        raise WaveformDecodeError("FFmpeg could not stream the source envelope")
    return accumulator.finish()


def read_source_window(
    source_path: Path,
    *,
    start_ticks: int,
    end_ticks: int,
    channel: str,
    ffmpeg_command: str,
    budget: ChildBudget,
) -> list[float]:
    """Read one bounded mono short window as a full-band block-RMS sequence.

    The window is decoded full-band at
    ``WAVEFORM_SHORT_WINDOW_DECODE_SAMPLE_RATE_HZ`` and folded into
    ``WAVEFORM_SHORT_WINDOW_BLOCK_SAMPLES``-sample RMS values, so the shared
    speech-band content survives instead of being low-passed away by a raw
    low-rate resample.
    """
    if start_ticks < 0 or end_ticks <= start_ticks:
        raise WaveformDecodeError("waveform short window range is invalid")
    duration_ticks = end_ticks - start_ticks
    expected_values = -(
        -duration_ticks * WAVEFORM_SHORT_WINDOW_DERIVED_SAMPLE_RATE_HZ
        // ALIGNMENT_TICKS_PER_SECOND
    )
    accumulator = _WindowEnvelopeAccumulator(
        maximum_values=expected_values
        + WAVEFORM_SHORT_WINDOW_DERIVED_SAMPLE_RATE_HZ
    )
    command = [
        ffmpeg_command,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-ss",
        _ticks_text(start_ticks),
        "-i",
        str(source_path),
        "-t",
        _ticks_text(duration_ticks),
        "-map",
        "0:a:0",
    ]
    if channel == "left":
        command.extend(["-af", "pan=mono|c0=c0"])
    elif channel == "right":
        command.extend(["-af", "pan=mono|c0=c1"])
    elif channel != "mono":
        raise WaveformDecodeError("waveform short window channel is invalid")
    command.extend(
        [
            "-map_metadata",
            "-1",
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(WAVEFORM_SHORT_WINDOW_DECODE_SAMPLE_RATE_HZ),
            "-c:a",
            "pcm_f32le",
            "-f",
            "f32le",
            "pipe:1",
        ]
    )
    result = run_bounded_child_stream(
        command,
        budget=budget,
        on_stdout=accumulator.feed,
    )
    if result.returncode != 0:
        raise WaveformDecodeError("FFmpeg could not read a waveform short window")
    samples = accumulator.finish()
    return samples[:expected_values]


def _normalized_lag_score(
    main: Sequence[float],
    auxiliary: Sequence[float],
    lag: int,
) -> tuple[float, int]:
    """One centered normalized correlation at a single envelope lag.

    Centering matches the refinement pass: a raw non-centered cosine on
    positive RMS envelopes approaches 1 for every lag whenever the mean
    dominates the fluctuations, which would make the global peak
    indistinguishable from arbitrary lags.
    """
    auxiliary_start = max(0, -lag)
    main_start = max(0, lag)
    length = min(
        len(auxiliary) - auxiliary_start,
        len(main) - main_start,
    )
    if length <= 0:
        return 0.0, 0
    auxiliary_values = auxiliary[auxiliary_start : auxiliary_start + length]
    main_values = main[main_start : main_start + length]
    auxiliary_mean = sum(auxiliary_values) / length
    main_mean = sum(main_values) / length
    centered_auxiliary = [value - auxiliary_mean for value in auxiliary_values]
    centered_main = [value - main_mean for value in main_values]
    dot = float(sum(map(operator.mul, centered_auxiliary, centered_main)))
    auxiliary_energy = float(
        sum(map(operator.mul, centered_auxiliary, centered_auxiliary))
    )
    main_energy = float(sum(map(operator.mul, centered_main, centered_main)))
    denominator = math.sqrt(auxiliary_energy * main_energy)
    if denominator <= 0.0:
        return 0.0, length
    return dot / denominator, length


def localize_envelope(
    main: Sequence[float],
    auxiliary: Sequence[float],
) -> CoarseLocalization | None:
    """Run the one global coarse pass and retain real peak separation."""
    if not main or not auxiliary:
        return None
    # Candidate support rule: every competing lag must rest on at least half
    # of the smaller side's bins. Edge alignments with a handful of bins can
    # reach a perfect cosine regardless of content and must never become the
    # peak or the runner-up.
    support_floor = max(
        WAVEFORM_COARSE_MINIMUM_OVERLAP_BINS,
        min(len(main), len(auxiliary)) // 2,
    )
    scores: list[tuple[int, float, int]] = []
    for lag in range(-(len(auxiliary) - 1), len(main)):
        score, overlap = _normalized_lag_score(main, auxiliary, lag)
        if overlap >= support_floor:
            scores.append((lag, score, overlap))
    if not scores:
        return None
    peak_lag, peak_score, _peak_overlap = max(
        scores,
        key=lambda item: (item[1], -abs(item[0]), -item[0]),
    )
    runner_candidates = [
        item
        for item in scores
        if item[0] != peak_lag
        and abs(item[0] - peak_lag) >= WAVEFORM_COARSE_RUNNER_UP_EXCLUSION_BINS
    ]
    if not runner_candidates:
        return CoarseLocalization(
            b_ticks=peak_lag * WAVEFORM_COARSE_BIN_TICKS,
            peak_lag_bins=peak_lag,
            peak_score=peak_score,
            runner_up_lag_bins=None,
            runner_up_score=None,
            separation=0.0,
            accepted=False,
        )
    runner_lag, runner_score, _runner_overlap = max(
        runner_candidates,
        key=lambda item: (item[1], -abs(item[0]), -item[0]),
    )
    separation = max(0.0, peak_score - runner_score)
    accepted = (
        peak_score >= WAVEFORM_COARSE_MINIMUM_SCORE
        and separation >= WAVEFORM_COARSE_MINIMUM_SEPARATION
    )
    return CoarseLocalization(
        b_ticks=peak_lag * WAVEFORM_COARSE_BIN_TICKS,
        peak_lag_bins=peak_lag,
        peak_score=peak_score,
        runner_up_lag_bins=runner_lag,
        runner_up_score=runner_score,
        separation=separation,
        accepted=accepted,
    )


def normalized_window_correlation(
    main: Sequence[float],
    auxiliary: Sequence[float],
    *,
    main_start_ticks: int,
    auxiliary_start_ticks: int,
    sample_rate_hz: int,
    max_lag_samples: int,
) -> WindowCorrelation:
    """Find one bounded normalized waveform correlation peak.

    The lag convention is `main_sample = auxiliary_sample + lag`; therefore
    `B = M0 - A0 + Δ` is preserved exactly in the returned source-global B.
    """
    if not main or not auxiliary or sample_rate_hz <= 0 or max_lag_samples < 0:
        raise WaveformDecodeError("waveform correlation input is invalid")
    main_mean = sum(main) / len(main)
    auxiliary_mean = sum(auxiliary) / len(auxiliary)
    centered_main = [value - main_mean for value in main]
    centered_auxiliary = [value - auxiliary_mean for value in auxiliary]
    minimum_overlap = max(2, min(len(main), len(auxiliary)) // 2)
    candidates: list[tuple[int, float]] = []
    for lag in range(-max_lag_samples, max_lag_samples + 1):
        auxiliary_start = max(0, -lag)
        main_start = max(0, lag)
        length = min(
            len(centered_auxiliary) - auxiliary_start,
            len(centered_main) - main_start,
        )
        if length < minimum_overlap:
            continue
        auxiliary_values = centered_auxiliary[
            auxiliary_start : auxiliary_start + length
        ]
        main_values = centered_main[main_start : main_start + length]
        dot = float(sum(map(operator.mul, auxiliary_values, main_values)))
        auxiliary_energy = float(
            sum(map(operator.mul, auxiliary_values, auxiliary_values))
        )
        main_energy = float(sum(map(operator.mul, main_values, main_values)))
        denominator = math.sqrt(auxiliary_energy * main_energy)
        correlation = 0.0 if denominator <= 0.0 else dot / denominator
        candidates.append((lag, correlation))
    if not candidates:
        raise WaveformDecodeError("waveform correlation has insufficient overlap")
    lag, correlation = max(
        candidates,
        key=lambda item: (item[1], -abs(item[0]), -item[0]),
    )
    b_ticks = (
        main_start_ticks
        - auxiliary_start_ticks
        + _samples_to_ticks(lag, sample_rate_hz)
    )
    return WindowCorrelation(b_ticks=b_ticks, correlation=correlation)


def waveform_windows(
    overlap_start_ticks: int,
    overlap_end_ticks: int,
) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int]] | None:
    """Return opening/middle/ending short windows over one overlap."""
    if overlap_end_ticks <= overlap_start_ticks:
        return None
    if overlap_end_ticks - overlap_start_ticks < max(
        ALIGNMENT_MINIMUM_VERIFIED_OVERLAP_TICKS,
        WAVEFORM_WINDOW_TICKS * WAVEFORM_WINDOW_COUNT,
    ):
        return None
    first = (overlap_start_ticks, overlap_start_ticks + WAVEFORM_WINDOW_TICKS)
    last = (overlap_end_ticks - WAVEFORM_WINDOW_TICKS, overlap_end_ticks)
    middle_start = (
        overlap_start_ticks
        + overlap_end_ticks
        - WAVEFORM_WINDOW_TICKS
    ) // 2
    middle = (middle_start, middle_start + WAVEFORM_WINDOW_TICKS)
    if not (first[1] <= middle[0] and middle[1] <= last[0]):
        return None
    return first, middle, last


def _empty_evidence(code: str = "no_candidate") -> dict[str, object]:
    return {
        "code": code,
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


def _coarse_evidence(coarse: CoarseLocalization) -> dict[str, object]:
    return {
        "code": "coarse_ambiguous",
        "coarse_peak": {
            "lag_ticks": coarse.b_ticks,
            "score": _number_text(coarse.peak_score),
        },
        "coarse_runner_up": (
            None
            if coarse.runner_up_lag_bins is None
            else {
                "lag_ticks": coarse.runner_up_lag_bins * WAVEFORM_COARSE_BIN_TICKS,
                "score": _number_text(
                    float(cast(float, coarse.runner_up_score))
                ),
            }
        ),
        "coarse_peak_runner_up_separation": _number_text(coarse.separation),
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


def _with_coarse(
    evidence: dict[str, object],
    coarse: CoarseLocalization,
) -> dict[str, object]:
    evidence = dict(evidence)
    evidence["coarse_peak"] = {
        "lag_ticks": coarse.b_ticks,
        "score": _number_text(coarse.peak_score),
    }
    evidence["coarse_runner_up"] = (
        None
        if coarse.runner_up_lag_bins is None
        else {
            "lag_ticks": coarse.runner_up_lag_bins * WAVEFORM_COARSE_BIN_TICKS,
            "score": _number_text(
                float(cast(float, coarse.runner_up_score))
            ),
        }
    )
    evidence["coarse_peak_runner_up_separation"] = _number_text(coarse.separation)
    return evidence


def _max_lag_samples() -> int:
    return max(
        1,
        math.ceil(
            WAVEFORM_MAX_REFINEMENT_LAG_TICKS
            * WAVEFORM_SHORT_WINDOW_DERIVED_SAMPLE_RATE_HZ
            / ALIGNMENT_TICKS_PER_SECOND
        ),
    )


def _verification_max_lag_samples() -> int:
    """Confirmation-only search bound for opening/ending windows.

    Verification happens after ``refined.b_ticks`` exists: the auxiliary
    window is anchored exactly there and the correlation may only wander
    within the frozen ``ALIGNMENT_MAXIMUM_LOCAL_ERROR_TICKS`` local-error
    gate. A stronger distant peak can therefore never re-localize the pair;
    it can only fail the confirmation.
    """
    return max(
        1,
        math.ceil(
            ALIGNMENT_MAXIMUM_LOCAL_ERROR_TICKS
            * WAVEFORM_SHORT_WINDOW_DERIVED_SAMPLE_RATE_HZ
            / ALIGNMENT_TICKS_PER_SECOND
        ),
    )


def analyze_waveform_pair(
    *,
    main_path: Path,
    auxiliary_path: Path,
    main_envelope: WaveformEnvelope,
    auxiliary_envelope: WaveformEnvelope,
    main_duration_ticks: int,
    auxiliary_duration_ticks: int,
    window_reader: WindowReader,
) -> dict[str, object]:
    """Analyze one already caller-selected pair without any pairing search."""
    last_evidence: dict[str, object] = _empty_evidence()
    overlap_start = 0
    overlap_end = 0
    channels = ("mono", "left", "right")
    for channel in channels:
        coarse = localize_envelope(
            main_envelope.channel(channel),
            auxiliary_envelope.channel(channel),
        )
        if coarse is None:
            continue
        last_evidence = _coarse_evidence(coarse)
        if not coarse.accepted:
            continue
        b_ticks = coarse.b_ticks
        overlap_start = max(0, b_ticks)
        overlap_end = min(main_duration_ticks, auxiliary_duration_ticks + b_ticks)
        windows = waveform_windows(overlap_start, overlap_end)
        if windows is None:
            last_evidence = _with_coarse(_empty_evidence("refinement_failed"), coarse)
            continue
        refinement_window = windows[1]
        try:
            main_samples = window_reader(
                main_path,
                refinement_window[0],
                refinement_window[1],
                channel,
            )
            auxiliary_start = refinement_window[0] - b_ticks
            auxiliary_samples = window_reader(
                auxiliary_path,
                auxiliary_start,
                auxiliary_start + WAVEFORM_WINDOW_TICKS,
                channel,
            )
            refined = normalized_window_correlation(
                main_samples,
                auxiliary_samples,
                main_start_ticks=refinement_window[0],
                auxiliary_start_ticks=auxiliary_start,
                sample_rate_hz=WAVEFORM_SHORT_WINDOW_DERIVED_SAMPLE_RATE_HZ,
                max_lag_samples=_max_lag_samples(),
            )
        except WaveformDecodeError:
            last_evidence = _with_coarse(_empty_evidence("refinement_failed"), coarse)
            continue
        if refined.correlation < WAVEFORM_MINIMUM_CORRELATION:
            last_evidence = _with_coarse(_empty_evidence("refinement_failed"), coarse)
            last_evidence["refined_offset_ticks"] = refined.b_ticks
            last_evidence["refined_correlation"] = _number_text(refined.correlation)
            last_evidence["verification_window_count"] = 1
            continue
        verification_records: list[WindowCorrelation] = []
        verification_failed = False
        for verification_window in (windows[0], windows[2]):
            try:
                main_samples = window_reader(
                    main_path,
                    verification_window[0],
                    verification_window[1],
                    channel,
                )
                # confirmatory anchoring: the auxiliary window is placed by
                # the refined offset, not the coarse one, and the correlation
                # may only move within the local-error gate around it
                auxiliary_start = verification_window[0] - refined.b_ticks
                auxiliary_samples = window_reader(
                    auxiliary_path,
                    auxiliary_start,
                    auxiliary_start + WAVEFORM_WINDOW_TICKS,
                    channel,
                )
                verified = normalized_window_correlation(
                    main_samples,
                    auxiliary_samples,
                    main_start_ticks=verification_window[0],
                    auxiliary_start_ticks=auxiliary_start,
                    sample_rate_hz=WAVEFORM_SHORT_WINDOW_DERIVED_SAMPLE_RATE_HZ,
                    max_lag_samples=_verification_max_lag_samples(),
                )
            except WaveformDecodeError:
                verification_failed = True
                break
            verified = replace(
                verified,
                local_error_ticks=abs(verified.b_ticks - refined.b_ticks),
            )
            verification_records.append(verified)
            if verified.correlation < WAVEFORM_MINIMUM_CORRELATION:
                verification_failed = True
                break
        evidence = _with_coarse(_empty_evidence(), coarse)
        evidence["refined_offset_ticks"] = refined.b_ticks
        evidence["refined_correlation"] = _number_text(refined.correlation)
        evidence["verification_windows"] = [
            {
                "b_ticks": item.b_ticks,
                "local_error_ticks": item.local_error_ticks,
                "correlation": _number_text(item.correlation),
            }
            for item in verification_records
        ]
        evidence["verification_window_count"] = 1 + len(verification_records)
        if verification_records:
            evidence["max_local_offset_error_ticks"] = max(
                item.local_error_ticks for item in verification_records
            )
        if verification_failed or len(verification_records) != 2:
            evidence["code"] = "verification_failed"
            last_evidence = evidence
            continue
        max_error = int(cast(int, evidence["max_local_offset_error_ticks"]))
        if max_error > ALIGNMENT_MAXIMUM_LOCAL_ERROR_TICKS:
            evidence["code"] = "fixed_offset_conflict"
            evidence["conflicting_b_ticks"] = list(
                dict.fromkeys(
                    [
                        refined.b_ticks,
                        *(item.b_ticks for item in verification_records),
                    ]
                )
            )
            return {
                "classifications": "conflict",
                "b_ticks": refined.b_ticks,
                "evidence": evidence,
                "max_local_offset_error_ticks": max_error,
            }
        evidence["code"] = "fixed_offset_verified"
        return {
            "classifications": "mapped",
            "b_ticks": refined.b_ticks,
            "evidence": evidence,
            "max_local_offset_error_ticks": max_error,
        }
    return {
        "classifications": "uncertain",
        "evidence": last_evidence,
    }


def estimate_waveform_workspace_bytes(source_durations_ticks: Sequence[int]) -> int:
    """Bounded input estimate: envelopes and three short windows, not PCM."""
    envelope_bytes = sum(
        max(1, math.ceil(duration / ALIGNMENT_TICKS_PER_SECOND))
        * 3
        * 8
        for duration in source_durations_ticks
    )
    return WAVEFORM_WORKSPACE_OVERHEAD_BYTES + envelope_bytes + WAVEFORM_SHORT_WINDOW_MEMORY_BYTES
