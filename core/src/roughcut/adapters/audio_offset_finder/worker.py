"""BBC audio-offset-finder worker executed inside the managed venv."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
from pathlib import Path

PROVIDER = "bbc_audio_offset_finder"
VERSION = "0.5.5"
SCHEMA_VERSION = 1
MAX_OUTPUT_BYTES = 64 * 1024

EXIT_INVALID_REQUEST = 2
EXIT_IMPORT_FAILED = 3
EXIT_FFMPEG_UNAVAILABLE = 4
EXIT_DECODE_FAILED = 5
EXIT_PROVIDER_FAILED = 6
EXIT_RESULT_INVALID = 7


def _format_decimal(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not _is_finite_number(value):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    text = repr(number)
    text = text.removesuffix(".0")
    return text


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def _write_output(path: Path, encoded: bytes) -> bool:
    if not path.parent.is_dir():
        sys.stderr.write("output parent missing\n")
        return False
    try:
        path.write_bytes(encoded)
    except OSError:
        sys.stderr.write("output write failed\n")
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--main", required=True)
    parser.add_argument("--aux", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--provider-version", required=True)
    parser.add_argument("--ffmpeg-command", required=True)
    parser.add_argument("--max-output-bytes", type=int, default=MAX_OUTPUT_BYTES)
    args = parser.parse_args(argv)

    if args.provider != PROVIDER or args.provider_version != VERSION:
        sys.stderr.write("provider identity mismatch\n")
        return EXIT_INVALID_REQUEST

    max_bytes = args.max_output_bytes
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or max_bytes <= 0
        or max_bytes > 1024 * 1024
    ):
        sys.stderr.write("invalid max output bytes\n")
        return EXIT_INVALID_REQUEST

    main_path = Path(args.main)
    aux_path = Path(args.aux)
    output_path = Path(args.output)
    ffmpeg_command = Path(args.ffmpeg_command)

    # The managed worker must not leak tracebacks for input errors.
    # Validate existence strictly; parent already validated, but worker
    # remains fail-closed if invoked directly.
    if not main_path.is_absolute() or not aux_path.is_absolute() or not output_path.is_absolute():
        sys.stderr.write("paths must be absolute\n")
        return EXIT_INVALID_REQUEST
    if not main_path.is_file() or not aux_path.is_file():
        sys.stderr.write("media file missing\n")
        return EXIT_INVALID_REQUEST
    try:
        resolved_ffmpeg = Path(shutil.which("ffmpeg") or "").resolve(strict=True)
        expected_ffmpeg = ffmpeg_command.resolve(strict=True)
        if not ffmpeg_command.is_absolute() or resolved_ffmpeg != expected_ffmpeg:
            raise OSError
        if not os.access(expected_ffmpeg, os.X_OK):
            raise OSError
    except OSError:
        sys.stderr.write("finder_ffmpeg_unavailable\n")
        return EXIT_FFMPEG_UNAVAILABLE

    try:
        mod = __import__(
            "audio_offset_finder.audio_offset_finder",
            fromlist=["find_offset_between_files"],
        )
        find_offset_between_files = mod.find_offset_between_files
    except Exception:  # noqa: BLE001
        sys.stderr.write("finder_import_failed\n")
        return EXIT_IMPORT_FAILED

    try:
        result = find_offset_between_files(str(main_path), str(aux_path))
    except Exception as error:  # noqa: BLE001
        # InsufficientAudio is a known "no offset" case; treat as valid
        # bounded JSON with null offset rather than leaking exception.
        try:
            mod2 = __import__(
                "audio_offset_finder.audio_offset_finder", fromlist=["InsufficientAudioException"]
            )
            Insuf = mod2.InsufficientAudioException

            if isinstance(error, Insuf):
                payload: dict[str, object] = {
                    "schema_version": SCHEMA_VERSION,
                    "provider": PROVIDER,
                    "provider_version": VERSION,
                    "native_offset_seconds": None,
                    "standard_score": None,
                    "analysis": {"error": "insufficient_audio"},
                }
                encoded = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
                if len(encoded) > max_bytes:
                    sys.stderr.write("output too large\n")
                    return EXIT_RESULT_INVALID
                return 0 if _write_output(output_path, encoded) else EXIT_INVALID_REQUEST
        except ImportError:
            pass
        if isinstance(error, FileNotFoundError):
            sys.stderr.write("finder_ffmpeg_unavailable\n")
            return EXIT_FFMPEG_UNAVAILABLE
        if isinstance(error, (EOFError, ValueError)) or str(error).startswith("FFMpeg failed:"):
            sys.stderr.write("finder_decode_failed\n")
            return EXIT_DECODE_FAILED
        sys.stderr.write("finder_provider_failed\n")
        return EXIT_PROVIDER_FAILED

    if not isinstance(result, dict):
        sys.stderr.write("finder_result_invalid\n")
        return EXIT_RESULT_INVALID

    time_offset = result.get("time_offset")
    standard_score = result.get("standard_score")

    # time_offset must be finite number when present; None is allowed as "no offset"
    if time_offset is None:
        native_text = None
    else:
        native_text = _format_decimal(time_offset)
        if native_text is None:
            sys.stderr.write("finder_result_invalid\n")
            return EXIT_RESULT_INVALID

    if standard_score is None:
        score_text = None
    else:
        score_text = _format_decimal(standard_score)
        if score_text is None:
            sys.stderr.write("finder_result_invalid\n")
            return EXIT_RESULT_INVALID

    # Bounded analysis — never include the full correlation array.
    analysis: dict[str, object] = {}
    for key in ("frame_offset", "earliest_frame_offset", "latest_frame_offset"):
        value = result.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            analysis[key] = value
    time_scale = result.get("time_scale")
    if time_scale is not None:
        time_scale_text = _format_decimal(time_scale)
        if time_scale_text is None:
            sys.stderr.write("finder_result_invalid\n")
            return EXIT_RESULT_INVALID
        analysis["time_scale"] = time_scale_text
    correlation = result.get("correlation")
    if correlation is not None:
        try:
            length = len(correlation)
            analysis["correlation_length"] = int(length)
        except Exception:  # noqa: BLE001, S110
            pass

    payload = {
        "schema_version": SCHEMA_VERSION,
        "provider": PROVIDER,
        "provider_version": VERSION,
        "native_offset_seconds": native_text,
        "standard_score": score_text,
        "analysis": analysis,
    }

    encoded = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > max_bytes:
        sys.stderr.write("finder_result_invalid\n")
        return EXIT_RESULT_INVALID

    return 0 if _write_output(output_path, encoded) else EXIT_INVALID_REQUEST


if __name__ == "__main__":
    raise SystemExit(main())
