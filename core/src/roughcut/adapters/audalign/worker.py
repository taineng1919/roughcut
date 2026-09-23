"""Run audalign 1.3.1 public API inside the isolated managed alignment venv.

The worker is executed with `-I` so only the pinned closure is importable.
It accepts exact WAV file pairs plus extraction offsets and returns the raw
`match_info` evidence for one auxiliary excerpt against one main reference,
with no ground truth, thresholds, or window verdicts applied here.
"""

from __future__ import annotations

import argparse
import json
import sys

WORKER_TIMEOUT_SECONDS = 600.0
AUDALIGN_WORKER_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
WORKER_STATUS_KEY = "_roughcut_worker_status"
WORKER_CANDIDATE_COUNT_KEY = "candidate_count"
WORKER_CANDIDATE_LIMIT_KEY = "candidate_limit"
WORKER_STATUS_CANDIDATE_OVERFLOW = "candidate_overflow"
WORKER_STATUS_OUTPUT_OVERFLOW = "output_overflow"


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
    """Return a complete result or a small, explicit non-result marker.

    The worker never slices a candidate list.  Candidate admission and output
    size limits therefore cannot turn a partial JSON document into evidence.
    """
    if max_output_bytes <= 0:
        raise ValueError("max_output_bytes must be positive")
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


def _build_recognizer():
    import audalign  # type: ignore[import-not-found]

    recognizer = audalign.FingerprintRecognizer()
    recognizer.config.set_accuracy(2)
    recognizer.config.num_processors = 1
    return recognizer


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
    args = parser.parse_args()

    recognizer = _build_recognizer()
    result = audalign.recognize(
        args.target_wav, args.against_wav, recognizer=recognizer
    )
    payload: dict[str, object]
    if result is None:
        payload = {"match_info": None}
    else:
        match_info = result.get("match_info")
        if not isinstance(match_info, dict):
            payload = {"match_info": None}
        else:
            against_key = args.against_wav
            entry = match_info.get(against_key)
            if entry is None:
                # audalign keys by basename; find the single against entry
                candidates = [
                    value
                    for key, value in match_info.items()
                    if key and value is not None
                ]
                entry = candidates[0] if len(candidates) == 1 else None
            if not isinstance(entry, dict):
                payload = {"match_info": None}
            else:
                payload = {
                    "match_info": {
                        "offset_seconds": entry.get("offset_seconds"),
                        "confidence": entry.get("confidence"),
                        "locality_seconds": entry.get("locality_seconds"),
                    }
                }
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
