"""Run audalign 1.3.1 CorrelationRecognizer inside the isolated managed alignment venv.

The worker is executed with `-I` so only the pinned closure is importable.
It constructs a CorrelationRecognizer with the parent-provided canonical
configuration, verifies that the pinned config has not drifted, and returns
minimal validated offset evidence for one aux excerpt against one main reference, with
no GT, thresholds, or pairing.
"""

from __future__ import annotations

import argparse
import json
import math
import numbers
import sys

WORKER_TIMEOUT_SECONDS = 600.0
AUDALIGN_WORKER_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
WORKER_STATUS_KEY = "_roughcut_worker_status"
WORKER_CANDIDATE_COUNT_KEY = "candidate_count"
WORKER_CANDIDATE_LIMIT_KEY = "candidate_limit"
WORKER_STATUS_CANDIDATE_OVERFLOW = "candidate_overflow"
WORKER_STATUS_OUTPUT_OVERFLOW = "output_overflow"
_EXPECTED_RAW_RESULT_KEYS = frozenset({"match_time", "match_info", "rankings"})
_EXPECTED_MATCH_INFO_ENTRY_KEYS = frozenset(
    {
        "locality_samples",
        "offset_samples",
        "locality_seconds",
        "offset_seconds",
        "confidence",
        "sample_rate",
        "scaling_factor",
    }
)
_EXPECTED_RANKINGS_KEYS = frozenset({"match_info"})


class WorkerContractError(RuntimeError):
    """Raised when pinned Audalign returns a shape outside the raw contract."""

_EXPECTED_CONFIG_KEYS = frozenset(
    {
        "sample_rate",
        "fft_window_size",
        "filter_matches",
        "freq_threshold",
        "normalize",
        "multiprocessing",
        "num_processors",
        "locality",
        "max_lags",
        "match_len_filter",
        "close_seconds_filter",
        "locality_filter_prop",
        "plot",
        "start_end",
        "start_end_against",
        "passthrough_args",
        "fail_on_decode_error",
        "cant_read_extensions",
        "cant_write_extensions",
        "DEFAULT_OVERLAP_RATIO",
        "SCALING_16_BIT",
        "LOCALITY_OVERLAP_RATIO",
        "DEFAULT_LOCALITY_FILTER_PROP",
    }
)


def _candidate_count(payload: dict[str, object]) -> int | None:
    match_info = payload.get("match_info")
    if not isinstance(match_info, dict):
        return None
    offsets = match_info.get("offset_seconds")
    if not isinstance(offsets, list):
        return None
    return len(offsets)


def _bounded_output_bytes(
    payload: dict[str, object],
    *,
    max_output_bytes: int,
    max_raw_candidates: int | None = None,
) -> bytes:
    if max_output_bytes <= 0:
        raise ValueError("max_output_bytes must be positive")
    if (
        max_raw_candidates is not None
        and (
            isinstance(max_raw_candidates, bool)
            or not isinstance(max_raw_candidates, int)
            or max_raw_candidates <= 0
        )
    ):
        raise ValueError("max_raw_candidates must be positive")
    count = _candidate_count(payload)
    if (
        max_raw_candidates is not None
        and count is not None
        and count > max_raw_candidates
    ):
        marker: dict[str, object] = {
            WORKER_STATUS_KEY: WORKER_STATUS_CANDIDATE_OVERFLOW,
            WORKER_CANDIDATE_COUNT_KEY: count,
            WORKER_CANDIDATE_LIMIT_KEY: max_raw_candidates,
        }
    else:
        marker = payload

    encoded = (json.dumps(marker, sort_keys=True) + "\n").encode("utf-8")
    if marker is not payload or len(encoded) <= max_output_bytes:
        if len(encoded) > max_output_bytes:
            raise ValueError("candidate overflow marker exceeds output limit")
        return encoded

    overflow = {
        WORKER_STATUS_KEY: WORKER_STATUS_OUTPUT_OVERFLOW,
        WORKER_CANDIDATE_COUNT_KEY: count,
    }
    encoded = (json.dumps(overflow, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > max_output_bytes:
        raise ValueError("output overflow marker exceeds output limit")
    return encoded


def _build_recognizer(expected_config: dict[str, object]):
    from audalign.recognizers.correcognize import (  # type: ignore[import-not-found]
        CorrelationRecognizer,
    )

    if set(expected_config) != _EXPECTED_CONFIG_KEYS:
        raise RuntimeError("CorrelationRecognizer expected config is not closed")
    recognizer = CorrelationRecognizer()
    cfg = recognizer.config
    for key, expected in expected_config.items():
        actual = getattr(cfg, key, None)
        if type(actual) is not type(expected) or actual != expected:
            raise RuntimeError(
                f"CorrelationRecognizer config drift: {key} expected {expected!r} got {actual!r}"
            )
    return recognizer


def _against_basename(path_text: str) -> str:
    normalized = path_text.replace("\\", "/")
    basename = normalized.rsplit("/", 1)[-1]
    if not basename or basename in {".", ".."}:
        raise RuntimeError("against path has no safe basename")
    return basename


def _finite_number(value: object, *, field: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise WorkerContractError(f"Audalign returned an invalid {field}")
    try:
        numeric = float(value)
    except (OverflowError, ValueError):
        raise WorkerContractError(f"Audalign returned an invalid {field}") from None
    if not math.isfinite(numeric):
        raise WorkerContractError(f"Audalign returned a non-finite {field}")
    if isinstance(value, numbers.Integral):
        return int(value)
    return numeric


def _finite_number_list(value: object, *, field: str) -> list[int | float]:
    if not isinstance(value, list):
        raise WorkerContractError(f"Audalign returned malformed {field}")
    return [_finite_number(item, field=field) for item in value]


def _integer_list(value: object, *, field: str) -> list[int]:
    if not isinstance(value, list):
        raise WorkerContractError(f"Audalign returned malformed {field}")
    result: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, numbers.Integral):
            raise WorkerContractError(f"Audalign returned an invalid {field}")
        result.append(int(item))
    return result


def _none_list(value: object, *, field: str, length: int) -> list[None]:
    if not isinstance(value, list) or len(value) != length or any(
        item is not None for item in value
    ):
        raise RuntimeError(f"Audalign returned an invalid {field}")
    return [None] * length


def _minimal_payload_from_raw(
    result: object,
    *,
    against_path: str,
    expected_sample_rate: int,
) -> dict[str, object]:
    """Validate the pinned recognize shape, then discard non-contract fields."""
    if result is None:
        return {"match_info": None}
    if not isinstance(result, dict) or set(result) != _EXPECTED_RAW_RESULT_KEYS:
        raise RuntimeError("Audalign returned a non-closed result")
    _finite_number(result["match_time"], field="match time")

    expected_key = _against_basename(against_path)
    match_info = result["match_info"]
    if not isinstance(match_info, dict) or set(match_info) != {expected_key}:
        raise RuntimeError("Audalign match info has an unknown or multiple against key")
    entry = match_info[expected_key]
    if not isinstance(entry, dict) or set(entry) != _EXPECTED_MATCH_INFO_ENTRY_KEYS:
        raise RuntimeError("Audalign against match info is not closed")

    offsets = _finite_number_list(entry["offset_seconds"], field="offset seconds")
    if not offsets:
        raise RuntimeError("Audalign returned an empty match")
    offset_samples = _integer_list(entry["offset_samples"], field="offset samples")
    confidence = _finite_number_list(entry["confidence"], field="confidence")
    _none_list(
        entry["locality_samples"], field="locality samples", length=len(offsets)
    )
    _none_list(
        entry["locality_seconds"], field="locality seconds", length=len(offsets)
    )
    if len(offset_samples) != len(offsets) or len(confidence) != len(offsets):
        raise RuntimeError("Audalign match info fields have mismatched lengths")
    sample_rate = entry["sample_rate"]
    if (
        isinstance(sample_rate, bool)
        or not isinstance(sample_rate, numbers.Integral)
        or int(sample_rate) != expected_sample_rate
    ):
        raise RuntimeError("Audalign returned an invalid sample rate")
    _finite_number(entry["scaling_factor"], field="scaling factor")

    rankings = result["rankings"]
    if not isinstance(rankings, dict) or set(rankings) != _EXPECTED_RANKINGS_KEYS:
        raise RuntimeError("Audalign returned malformed rankings")
    ranking_info = rankings["match_info"]
    if not isinstance(ranking_info, dict) or set(ranking_info) != {expected_key}:
        raise RuntimeError("Audalign rankings have an unknown or multiple against key")
    rank = ranking_info[expected_key]
    if isinstance(rank, bool) or not isinstance(rank, numbers.Integral) or not 1 <= int(rank) <= 10:
        raise RuntimeError("Audalign rankings contain an invalid rank")

    return {
        "match_info": {
            "offset_seconds": [
                int(item) if isinstance(item, int) else float(item)
                for item in offsets
            ],
            "sample_rate": int(sample_rate),
        }
    }


def main() -> int:
    import audalign  # type: ignore[import-not-found]

    parser = argparse.ArgumentParser()
    parser.add_argument("--target-wav", required=True)
    parser.add_argument("--against-wav", required=True)
    parser.add_argument("--json-output", required=True)
    parser.add_argument(
        "--max-output-bytes",
        type=int,
        default=AUDALIGN_WORKER_MAX_OUTPUT_BYTES,
    )
    parser.add_argument("--max-raw-candidates", type=int)
    parser.add_argument("--expected-config", required=True)
    args = parser.parse_args()

    try:
        expected_config = json.loads(args.expected_config)
    except json.JSONDecodeError as error:
        raise RuntimeError("CorrelationRecognizer expected config is invalid JSON") from error
    if not isinstance(expected_config, dict):
        raise TypeError("CorrelationRecognizer expected config is not an object")
    recognizer = _build_recognizer(expected_config)
    result = audalign.recognize(
        args.target_wav, args.against_wav, recognizer=recognizer
    )
    payload: dict[str, object]
    if result is None:
        payload = {"match_info": None}
    elif not isinstance(result, dict) or set(result) != _EXPECTED_RAW_RESULT_KEYS:
        raise RuntimeError("Audalign returned a non-object result")
    else:
        payload = _minimal_payload_from_raw(
            result,
            against_path=args.against_wav,
            expected_sample_rate=expected_config["sample_rate"],
        )
    output_bytes = _bounded_output_bytes(
        payload,
        max_output_bytes=args.max_output_bytes,
        max_raw_candidates=args.max_raw_candidates,
    )
    with open(args.json_output, "wb") as output:
        output.write(output_bytes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
