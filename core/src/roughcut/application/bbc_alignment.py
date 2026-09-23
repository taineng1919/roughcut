"""BBC initial B with Roughcut short-window refinement and verification."""

from __future__ import annotations

import math
from pathlib import Path
from typing import cast

from roughcut.application.waveform_alignment import (
    WaveformDecodeError,
    WindowReader,
    normalized_window_correlation,
    waveform_windows,
)
from roughcut.domain.alignment import (
    ALIGNMENT_MAXIMUM_LOCAL_ERROR_TICKS,
    BBC_PROFILE_NAME,
    BBC_PROFILE_VERSION,
    WAVEFORM_MAX_REFINEMENT_LAG_TICKS,
    WAVEFORM_MINIMUM_CORRELATION,
    WAVEFORM_SHORT_WINDOW_DERIVED_SAMPLE_RATE_HZ,
    WAVEFORM_WINDOW_TICKS,
)


def _number_text(value: float) -> str:
    return format(value, ".17g")


def _max_lag_samples(ticks: int) -> int:
    return max(
        1,
        math.ceil(
            ticks * WAVEFORM_SHORT_WINDOW_DERIVED_SAMPLE_RATE_HZ / 120_000
        ),
    )


def _geometry(
    b_ticks: int, main_duration_ticks: int, auxiliary_duration_ticks: int
) -> dict[str, int] | None:
    main_start = max(0, b_ticks)
    main_end = min(main_duration_ticks, auxiliary_duration_ticks + b_ticks)
    if waveform_windows(main_start, main_end) is None:
        return None
    return {
        "main_start_ticks": main_start,
        "main_end_ticks": main_end,
        "auxiliary_start_ticks": main_start - b_ticks,
        "auxiliary_end_ticks": main_end - b_ticks,
    }


def empty_bbc_evidence(
    code: str,
    *,
    native_offset_seconds: str | None = None,
    initial_b_ticks: int | None = None,
    standard_score: str | None = None,
) -> dict[str, object]:
    return {
        "code": code,
        "provider": "bbc_audio_offset_finder",
        "provider_version": "0.5.5",
        "channel": None,
        "native_offset_seconds": native_offset_seconds,
        "initial_b_ticks": initial_b_ticks,
        "standard_score": standard_score,
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


def analyze_bbc_pair(
    *,
    main_path: Path,
    auxiliary_path: Path,
    main_duration_ticks: int,
    auxiliary_duration_ticks: int,
    native_offset_seconds: str,
    initial_b_ticks: int,
    standard_score: str,
    window_reader: WindowReader,
) -> dict[str, object]:
    """Refine direct Roughcut B and verify three rebuilt windows."""
    initial_evidence = empty_bbc_evidence(
        "initial_overlap_insufficient",
        native_offset_seconds=native_offset_seconds,
        initial_b_ticks=initial_b_ticks,
        standard_score=standard_score,
    )
    initial_geometry = _geometry(
        initial_b_ticks, main_duration_ticks, auxiliary_duration_ticks
    )
    if initial_geometry is None:
        return {"classifications": "uncertain", "evidence": initial_evidence}
    initial_windows = waveform_windows(
        initial_geometry["main_start_ticks"], initial_geometry["main_end_ticks"]
    )
    assert initial_windows is not None

    last_evidence = initial_evidence
    for channel in ("mono", "left", "right"):
        evidence = empty_bbc_evidence(
            "refinement_failed",
            native_offset_seconds=native_offset_seconds,
            initial_b_ticks=initial_b_ticks,
            standard_score=standard_score,
        )
        evidence["channel"] = channel
        refinement_window = initial_windows[1]
        try:
            main_samples = window_reader(
                main_path, refinement_window[0], refinement_window[1], channel
            )
            auxiliary_start = refinement_window[0] - initial_b_ticks
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
                max_lag_samples=_max_lag_samples(
                    WAVEFORM_MAX_REFINEMENT_LAG_TICKS
                ),
            )
        except WaveformDecodeError:
            evidence["code"] = "decode_failed"
            last_evidence = evidence
            continue
        evidence["refined_b_ticks"] = refined.b_ticks
        evidence["refined_correlation"] = _number_text(refined.correlation)
        if refined.correlation < WAVEFORM_MINIMUM_CORRELATION:
            last_evidence = evidence
            continue

        refined_geometry = _geometry(
            refined.b_ticks, main_duration_ticks, auxiliary_duration_ticks
        )
        if refined_geometry is None:
            evidence["code"] = "refined_overlap_insufficient"
            last_evidence = evidence
            continue
        evidence["refined_geometry"] = refined_geometry
        refined_windows = waveform_windows(
            refined_geometry["main_start_ticks"],
            refined_geometry["main_end_ticks"],
        )
        assert refined_windows is not None
        records: list[dict[str, object]] = []
        failed = False
        decode_failed = False
        for label, window in zip(
            ("opening", "middle", "ending"), refined_windows, strict=True
        ):
            try:
                main_samples = window_reader(main_path, window[0], window[1], channel)
                auxiliary_start = window[0] - refined.b_ticks
                auxiliary_samples = window_reader(
                    auxiliary_path,
                    auxiliary_start,
                    auxiliary_start + WAVEFORM_WINDOW_TICKS,
                    channel,
                )
                verified = normalized_window_correlation(
                    main_samples,
                    auxiliary_samples,
                    main_start_ticks=window[0],
                    auxiliary_start_ticks=auxiliary_start,
                    sample_rate_hz=WAVEFORM_SHORT_WINDOW_DERIVED_SAMPLE_RATE_HZ,
                    max_lag_samples=_max_lag_samples(
                        ALIGNMENT_MAXIMUM_LOCAL_ERROR_TICKS
                    ),
                )
            except WaveformDecodeError:
                evidence["code"] = "decode_failed"
                evidence["verification_windows"] = records
                evidence["verification_window_count"] = len(records)
                evidence["max_local_offset_error_ticks"] = (
                    max(cast(int, item["local_error_ticks"]) for item in records)
                    if records
                    else None
                )
                last_evidence = evidence
                decode_failed = True
                break
            local_error = abs(verified.b_ticks - refined.b_ticks)
            records.append(
                {
                    "label": label,
                    "main_start_ticks": window[0],
                    "main_end_ticks": window[1],
                    "auxiliary_start_ticks": auxiliary_start,
                    "auxiliary_end_ticks": auxiliary_start + WAVEFORM_WINDOW_TICKS,
                    "b_ticks": verified.b_ticks,
                    "local_error_ticks": local_error,
                    "correlation": _number_text(verified.correlation),
                }
            )
            if (
                verified.correlation < WAVEFORM_MINIMUM_CORRELATION
                or local_error > ALIGNMENT_MAXIMUM_LOCAL_ERROR_TICKS
            ):
                failed = True
                break
        if decode_failed:
            continue
        evidence["verification_windows"] = records
        evidence["verification_window_count"] = len(records)
        evidence["max_local_offset_error_ticks"] = (
            max(cast(int, item["local_error_ticks"]) for item in records)
            if records
            else None
        )
        if failed or len(records) != 3:
            evidence["code"] = "verification_failed"
            last_evidence = evidence
            continue
        evidence["code"] = "fixed_offset_verified"
        return {
            "classifications": "mapped",
            "b_ticks": refined.b_ticks,
            "evidence": evidence,
        }
    return {"classifications": "uncertain", "evidence": last_evidence}
