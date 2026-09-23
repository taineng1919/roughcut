"""Roughcut-owned BBC audio-offset-finder adapter.

Core never imports the third-party package; the worker executes inside the
managed venv that carries the frozen closure.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path

from roughcut.adapters.child_budget import (
    ChildBudget,
    ChildProcessBudgetError,
    ChildProcessMemoryBudgetError,
    ChildProcessTimeBudgetError,
    run_bounded_child_stream,
)
from roughcut.adapters.runtime_binding import (
    BBC_AUDIO_OFFSET_FINDER_PROVIDER,
    BBC_AUDIO_OFFSET_FINDER_VERSION,
    RuntimeAlignmentPython,
    RuntimeBindingError,
)
from roughcut.domain.alignment import seconds_to_ticks  # re-export for tests

PROVIDER = BBC_AUDIO_OFFSET_FINDER_PROVIDER
VERSION = BBC_AUDIO_OFFSET_FINDER_VERSION

MAX_OUTPUT_BYTES = 64 * 1024
MAX_STDOUT_BYTES = 32 * 1024
SCHEMA_VERSION = 1

ALLOWED_PAYLOAD_KEYS = frozenset(
    {
        "schema_version",
        "provider",
        "provider_version",
        "native_offset_seconds",
        "standard_score",
        "analysis",
    }
)
ALLOWED_ANALYSIS_KEYS = frozenset(
    {
        "frame_offset",
        "time_scale",
        "correlation_length",
        "earliest_frame_offset",
        "latest_frame_offset",
        "error",
    }
)


class BbcAdapterError(RuntimeError):
    """Base for all BBC adapter failures."""


class BbcProviderMissingError(BbcAdapterError):
    pass


class BbcProviderMismatchError(BbcAdapterError):
    pass


class BbcTimeoutError(BbcAdapterError):
    pass


class BbcMemoryError(BbcAdapterError):
    pass


class BbcOutputTooLargeError(BbcAdapterError):
    pass


class BbcNonZeroExitError(BbcAdapterError):
    pass


class BbcMalformedJsonError(BbcAdapterError):
    pass


class BbcDuplicateKeyError(BbcAdapterError):
    pass


class BbcExtraFieldError(BbcAdapterError):
    pass


class BbcInvalidOffsetError(BbcAdapterError):
    pass


class BbcInvalidScoreError(BbcAdapterError):
    pass


class BbcNoOffsetError(BbcAdapterError):
    pass


class BbcImportError(BbcAdapterError):
    pass


class BbcFfmpegUnavailableError(BbcAdapterError):
    pass


class BbcDecodeError(BbcAdapterError):
    pass


class BbcProviderError(BbcAdapterError):
    pass


class BbcResultInvalidError(BbcAdapterError):
    pass


def bbc_failure_code(error: BbcAdapterError) -> str:
    """Map adapter failures to the closed, path-free artifact vocabulary."""
    if isinstance(error, BbcImportError):
        return "finder_import_failed"
    if isinstance(error, BbcFfmpegUnavailableError):
        return "finder_ffmpeg_unavailable"
    if isinstance(error, BbcDecodeError):
        return "finder_decode_failed"
    if isinstance(error, BbcProviderError):
        return "finder_provider_failed"
    if isinstance(error, BbcNoOffsetError):
        return "finder_insufficient_audio"
    return "finder_result_invalid"


@dataclass(frozen=True)
class BbcOffsetAnalysis:
    frame_offset: int | None = None
    time_scale: str | None = None
    correlation_length: int | None = None
    earliest_frame_offset: int | None = None
    latest_frame_offset: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class BbcOffsetResult:
    native_offset_seconds: str
    standard_score: str | None
    analysis: BbcOffsetAnalysis
    provider: str = PROVIDER
    provider_version: str = VERSION

    @property
    def offset_ticks(self) -> int:
        return seconds_to_ticks(self.native_offset_seconds)


def _validate_paths(main_path: Path, aux_path: Path) -> None:
    for name, path in (("main", main_path), ("aux", aux_path)):
        if not isinstance(path, Path):
            raise BbcAdapterError(f"{name} path must be a Path")
        if not path.is_absolute():
            raise BbcAdapterError(f"{name} path must be absolute")
        if not path.is_file():
            raise BbcAdapterError(f"{name} file is missing")
        try:
            if path.is_symlink():
                raise BbcAdapterError(f"{name} path must not be a symlink")
        except OSError as error:
            raise BbcAdapterError(f"{name} path could not be checked") from error


def _validate_workspace_root(workspace_root: Path) -> Path:
    if not isinstance(workspace_root, Path):
        raise BbcAdapterError("workspace_root must be a Path")
    if not workspace_root.is_absolute():
        raise BbcAdapterError("workspace_root must be absolute")
    try:
        if not workspace_root.exists() or not workspace_root.is_dir():
            raise BbcAdapterError("workspace_root must be an existing directory")
        if workspace_root.is_symlink():
            raise BbcAdapterError("workspace_root must not be a symlink")
    except OSError as error:
        raise BbcAdapterError("workspace_root could not be checked") from error
    return workspace_root


def _validate_ffmpeg_command(ffmpeg_command: str) -> Path:
    if not isinstance(ffmpeg_command, str) or not ffmpeg_command:
        raise BbcFfmpegUnavailableError("verified ffmpeg command is missing")
    command = Path(ffmpeg_command)
    try:
        if not command.is_absolute() or not command.is_file() or command.is_symlink():
            raise BbcFfmpegUnavailableError("verified ffmpeg command is unavailable")
        if not os.access(command, os.X_OK):
            raise BbcFfmpegUnavailableError("verified ffmpeg command is unavailable")
    except OSError as error:
        raise BbcFfmpegUnavailableError("verified ffmpeg command could not be checked") from error
    return command.resolve()


def _validate_selection(selection: RuntimeAlignmentPython | None) -> RuntimeAlignmentPython:
    if selection is None:
        raise BbcProviderMissingError("bbc provider is missing")
    if selection.provider != PROVIDER:
        raise BbcProviderMismatchError(
            f"bbc provider mismatch: expected {PROVIDER}, got {selection.provider}"
        )
    if selection.provider_version != VERSION:
        raise BbcProviderMismatchError(
            f"bbc version mismatch: expected {VERSION}, got {selection.provider_version}"
        )
    interpreter = Path(selection.interpreter)
    try:
        if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
            raise BbcProviderMissingError("bbc managed interpreter is missing or stale")
        if interpreter.is_symlink():
            raise BbcProviderMissingError("bbc managed interpreter must not be a symlink")
    except OSError as error:
        raise BbcProviderMissingError("bbc managed interpreter could not be checked") from error
    # Full managed closure validation
    try:
        from roughcut.adapters.component_environment import current_platform

        selection.validate(platform=current_platform())
    except RuntimeBindingError as error:
        msg = str(error).lower()
        if "provider" in msg:
            raise BbcProviderMismatchError(str(error)) from error
        raise BbcProviderMissingError(str(error)) from error
    return selection


def validate_bbc_selection(
    selection: RuntimeAlignmentPython | None,
) -> RuntimeAlignmentPython:
    """Validate the exact persistent BBC selection without running media."""
    return _validate_selection(selection)


def _check_decimal_string(text: str, *, field: str) -> None:
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError, AttributeError) as error:
        if field == "native_offset_seconds":
            raise BbcInvalidOffsetError(f"invalid {field}: {text!r}") from error
        raise BbcInvalidScoreError(f"invalid {field}: {text!r}") from error
    if not value.is_finite():
        if field == "native_offset_seconds":
            raise BbcInvalidOffsetError(f"non-finite {field}")
        raise BbcInvalidScoreError(f"non-finite {field}")


def _parse_json_strict(raw: bytes, *, max_bytes: int) -> dict[str, object]:
    if len(raw) > max_bytes:
        raise BbcOutputTooLargeError("output JSON exceeds byte limit")
    if not raw:
        raise BbcMalformedJsonError("output JSON is empty")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BbcMalformedJsonError("output JSON is not utf-8") from error
    stripped = text.strip()
    if not stripped:
        raise BbcMalformedJsonError("output JSON is empty")

    def _pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        seen: set[str] = set()
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in seen:
                raise BbcDuplicateKeyError(f"duplicate key: {key!r}")
            seen.add(key)
            result[key] = value
        return result

    def _reject_constant(constant: str) -> object:
        raise BbcMalformedJsonError(f"JSON constant not allowed: {constant}")

    try:
        payload = json.loads(
            stripped, object_pairs_hook=_pairs_hook, parse_constant=_reject_constant
        )
    except BbcDuplicateKeyError:
        raise
    except BbcMalformedJsonError:
        raise
    except json.JSONDecodeError as error:
        raise BbcMalformedJsonError("output JSON is malformed") from error
    except BbcAdapterError:
        raise
    except Exception as error:
        raise BbcMalformedJsonError("output JSON could not be parsed") from error

    if not isinstance(payload, dict):
        raise BbcMalformedJsonError("output JSON must be an object")
    return payload


def _validate_payload(payload: dict[str, object]) -> dict[str, object]:
    # Strict closed set: missing and extra both rejected
    if set(payload.keys()) != ALLOWED_PAYLOAD_KEYS:
        extra = set(payload.keys()) - ALLOWED_PAYLOAD_KEYS
        missing = ALLOWED_PAYLOAD_KEYS - set(payload.keys())
        if extra:
            raise BbcExtraFieldError(f"unexpected fields: {sorted(extra)}")
        raise BbcMalformedJsonError(f"missing fields: {sorted(missing)}")

    if payload.get("schema_version") != SCHEMA_VERSION:
        raise BbcMalformedJsonError("schema_version must be 1")
    if payload.get("provider") != PROVIDER:
        raise BbcMalformedJsonError(f"provider must be {PROVIDER}")
    if payload.get("provider_version") != VERSION:
        raise BbcMalformedJsonError(f"provider_version must be {VERSION}")

    analysis = payload.get("analysis")
    if not isinstance(analysis, dict):
        raise BbcMalformedJsonError("analysis must be an object")
    extra_analysis = set(analysis.keys()) - ALLOWED_ANALYSIS_KEYS
    if extra_analysis:
        raise BbcExtraFieldError(f"unexpected analysis fields: {sorted(extra_analysis)}")

    # Typed analysis validation
    for key in (
        "frame_offset",
        "earliest_frame_offset",
        "latest_frame_offset",
        "correlation_length",
    ):
        if key in analysis:
            val = analysis[key]
            if not isinstance(val, int) or isinstance(val, bool):
                raise BbcMalformedJsonError(f"analysis.{key} must be int")
            if key == "correlation_length" and val <= 0:
                raise BbcMalformedJsonError("correlation_length must be positive")

    if "time_scale" in analysis:
        ts = analysis["time_scale"]
        if not isinstance(ts, str):
            raise BbcMalformedJsonError("analysis.time_scale must be string")
        _check_decimal_string(ts, field="time_scale")

    if "error" in analysis:
        err = analysis["error"]
        if err != "insufficient_audio":
            raise BbcMalformedJsonError("analysis.error must be 'insufficient_audio'")

    # Score / offset contract
    native = payload.get("native_offset_seconds")
    score = payload.get("standard_score")

    if native is None:
        # No-offset marker: score must be null and analysis must contain error
        if score is not None:
            raise BbcInvalidScoreError("score must be null when native is null")
        if analysis.get("error") != "insufficient_audio":
            raise BbcMalformedJsonError("no-offset payload must carry error=insufficient_audio")
    else:
        if not isinstance(native, str):
            raise BbcInvalidOffsetError("native_offset_seconds must be a string")
        _check_decimal_string(native, field="native_offset_seconds")
        if "error" in analysis:
            raise BbcMalformedJsonError("success payload must not contain error")
        if score is None:
            raise BbcInvalidScoreError("standard_score must be present when offset exists")
        if not isinstance(score, str):
            raise BbcInvalidScoreError("standard_score must be a string")
        _check_decimal_string(score, field="standard_score")

    # Bounded analysis re-encoding check
    try:
        encoded_analysis = json.dumps(analysis, sort_keys=True).encode("utf-8")
    except Exception as error:
        raise BbcMalformedJsonError("analysis could not be encoded") from error
    if len(encoded_analysis) > 16 * 1024:
        raise BbcOutputTooLargeError("analysis exceeds byte limit")

    return payload


def _bounded_read(path: Path, *, max_bytes: int) -> bytes:
    # lstat first: reject symlink / non-regular, check size
    try:
        st = path.lstat()
    except OSError as error:
        raise BbcMalformedJsonError("output JSON could not be read") from error
    if stat.S_ISLNK(st.st_mode):
        raise BbcMalformedJsonError("output JSON must not be a symlink")
    if not stat.S_ISREG(st.st_mode):
        raise BbcMalformedJsonError("output JSON must be a regular file")
    if st.st_size > max_bytes:
        raise BbcOutputTooLargeError("output JSON exceeds byte limit")
    # Bounded read: max+1 to detect overflow without unbounded allocation
    try:
        with path.open("rb") as f:
            data = f.read(max_bytes + 1)
    except OSError as error:
        raise BbcMalformedJsonError("output JSON could not be read") from error
    if len(data) > max_bytes:
        raise BbcOutputTooLargeError("output JSON exceeds byte limit")
    return data


def run_bbc_offset_finder(
    main_path: Path,
    aux_path: Path,
    *,
    selection: RuntimeAlignmentPython | None,
    budget: ChildBudget,
    workspace_root: Path,
    ffmpeg_command: str,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
) -> BbcOffsetResult:
    """Run one BBC offset discovery under the shared child budget."""
    _validate_paths(main_path, aux_path)
    validated = _validate_selection(selection)
    validated_ffmpeg = _validate_ffmpeg_command(ffmpeg_command)

    if (
        isinstance(max_output_bytes, bool)
        or not isinstance(max_output_bytes, int)
        or max_output_bytes <= 0
        or max_output_bytes > 1024 * 1024
    ):
        raise BbcAdapterError("max_output_bytes is invalid")
    if not isinstance(budget, ChildBudget):
        raise BbcAdapterError("budget must be a ChildBudget")

    interpreter = Path(validated.interpreter)
    worker = Path(__file__).with_name("worker.py")
    if not worker.is_file():
        raise BbcAdapterError("bbc worker is missing")

    validated_root = _validate_workspace_root(workspace_root)
    temporary = tempfile.TemporaryDirectory(dir=validated_root, prefix="bbc-")
    tmp_dir = Path(temporary.name)
    output_path = tmp_dir / "output.json"

    command: list[str] = [
        str(interpreter),
        "-I",
        str(worker),
        "--main",
        str(main_path),
        "--aux",
        str(aux_path),
        "--output",
        str(output_path),
        "--provider",
        PROVIDER,
        "--provider-version",
        VERSION,
        "--ffmpeg-command",
        str(validated_ffmpeg),
        "--max-output-bytes",
        str(max_output_bytes),
    ]

    try:
        original_apply_tmpdir = budget.apply_tmpdir

        def _controlled_bbc_environment(environment: dict[str, str]) -> dict[str, str]:
            controlled = (
                original_apply_tmpdir(environment)
                if original_apply_tmpdir is not None
                else dict(environment)
            )
            controlled["PATH"] = str(validated_ffmpeg.parent)
            return controlled

        worker_budget = replace(budget, apply_tmpdir=_controlled_bbc_environment)
        # Stream stdout bounded; stderr is DEVNULL (0 bytes bounded).
        total_stdout = 0

        def _on_stdout(chunk: bytes) -> None:
            nonlocal total_stdout
            total_stdout += len(chunk)
            if total_stdout > MAX_STDOUT_BYTES:
                raise BbcOutputTooLargeError("stdout exceeds byte limit")

        try:
            result = run_bounded_child_stream(
                command,
                budget=worker_budget,
                on_stdout=_on_stdout,
            )
        except ChildProcessTimeBudgetError as error:
            raise BbcTimeoutError(str(error)) from error
        except ChildProcessMemoryBudgetError as error:
            raise BbcMemoryError(str(error)) from error
        except ChildProcessBudgetError as error:
            raise BbcAdapterError(str(error)) from error
        except (OSError, ValueError) as error:
            raise BbcAdapterError("bbc worker could not start") from error

        if result.returncode != 0:
            if result.returncode == 3:
                raise BbcImportError("bbc provider import failed")
            if result.returncode == 4:
                raise BbcFfmpegUnavailableError("bbc ffmpeg unavailable")
            if result.returncode == 5:
                raise BbcDecodeError("bbc media decode failed")
            if result.returncode == 6:
                raise BbcProviderError("bbc provider failed")
            if result.returncode == 7:
                raise BbcResultInvalidError("bbc provider result invalid")
            raise BbcNonZeroExitError(f"bbc worker exited with status {result.returncode}")

        # Bounded file read
        raw = _bounded_read(output_path, max_bytes=max_output_bytes)
        payload = _parse_json_strict(raw, max_bytes=max_output_bytes)
        validated_payload = _validate_payload(payload)

        native = validated_payload.get("native_offset_seconds")
        score = validated_payload.get("standard_score")
        analysis_raw = validated_payload.get("analysis")
        assert isinstance(analysis_raw, dict)

        if native is None:
            raise BbcNoOffsetError("bbc finder produced no offset")
        assert isinstance(native, str)
        # score already validated to be str when native is not None
        assert isinstance(score, str)

        # Build typed analysis
        analysis = BbcOffsetAnalysis(
            frame_offset=analysis_raw.get("frame_offset"),
            time_scale=analysis_raw.get("time_scale"),
            correlation_length=analysis_raw.get("correlation_length"),
            earliest_frame_offset=analysis_raw.get("earliest_frame_offset"),
            latest_frame_offset=analysis_raw.get("latest_frame_offset"),
            error=analysis_raw.get("error"),
        )

        return BbcOffsetResult(
            native_offset_seconds=native,
            standard_score=score,
            analysis=analysis,
        )
    finally:
        temporary.cleanup()
