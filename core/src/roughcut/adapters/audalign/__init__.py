"""Audalign 1.3.1 adapter: subprocess worker and offset evidence parsing."""

from __future__ import annotations

import json
import math
import os
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from roughcut.adapters.audalign.worker import (
    AUDALIGN_WORKER_MAX_OUTPUT_BYTES,
    WORKER_STATUS_CANDIDATE_OVERFLOW,
    WORKER_STATUS_KEY,
    WORKER_STATUS_OUTPUT_OVERFLOW,
    WORKER_TIMEOUT_SECONDS,
)
from roughcut.adapters.child_budget import (
    ChildBudget,
    ChildProcessBudgetError,
    ChildProcessMemoryBudgetError,
    ChildProcessTimeBudgetError,
    run_bounded_child,
)
from roughcut.adapters.component_environment import current_platform
from roughcut.adapters.media_operation_store import media_child_process_kwargs
from roughcut.adapters.runtime_binding import (
    AUDALIGN_PROVIDER,
    RuntimeAlignmentPython,
    RuntimeBindingError,
)
from roughcut.domain.alignment import (
    AUDALIGN_CORRELATION_MAX_RAW_CANDIDATES_PER_PROBE,
    AUDALIGN_CORRELATION_SAMPLE_RATE_HZ,
    AUDALIGN_UPSTREAM_COMMIT,
    AUDALIGN_VERSION,
    AlignmentError,
    audalign_correlation_typed_config,
    seconds_to_ticks,
)


class AudalignAdapterError(RuntimeError):
    """Raised when the pinned audalign worker cannot produce evidence."""


class AudalignCandidateLimitError(AudalignAdapterError):
    """Raised when the worker refuses to emit more than the profile cap."""


class AudalignOutputOverflowError(AudalignAdapterError):
    """Raised when the complete worker JSON exceeds its internal bound."""


class AudalignBudgetError(AudalignAdapterError):
    """Raised when the audalign worker exceeds the operation budget."""


class AudalignTimeBudgetError(AudalignBudgetError):
    """Raised when the audalign worker exceeds the wall-time budget."""


class AudalignMemoryBudgetError(AudalignBudgetError):
    """Raised when the audalign worker exceeds the memory budget."""


@dataclass(frozen=True)
class AudalignCandidate:
    offset_seconds: str
    confidence: int
    offset_ticks: int


@dataclass(frozen=True)
class AudalignMatchResult:
    candidates: tuple[AudalignCandidate, ...]
    raw_matching_fingerprint_counts: tuple[int, ...]


ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]


def run_audalign_recognize(
    alignment_python: Path,
    target_wav: Path,
    against_wav: Path,
    output_path: Path,
    *,
    process_runner: ProcessRunner = subprocess.run,
    timeout_seconds: float = WORKER_TIMEOUT_SECONDS,
    memory_limit: int = 0,
    budget: ChildBudget | None = None,
    max_output_bytes: int = AUDALIGN_WORKER_MAX_OUTPUT_BYTES,
    max_raw_candidates: int | None = None,
) -> AudalignMatchResult:
    """Run one public audalign.recognize(target, against) and parse evidence.

    The child shares the operation deadline and memory ceiling through the
    bounded runner when `budget` is supplied; otherwise it keeps the module
    defaults (used by non-alignment callers).
    """
    worker = Path(__file__).with_name("worker.py")
    if (
        isinstance(max_output_bytes, bool)
        or not isinstance(max_output_bytes, int)
        or max_output_bytes <= 0
    ):
        raise AudalignAdapterError("audalign worker output limit is invalid")
    if max_raw_candidates is not None and (
        isinstance(max_raw_candidates, bool)
        or not isinstance(max_raw_candidates, int)
        or max_raw_candidates <= 0
    ):
        raise AudalignAdapterError("audalign worker candidate limit is invalid")
    if not alignment_python.is_file() or not os.access(alignment_python, os.X_OK):
        raise AudalignAdapterError(
            "alignment Python interpreter is missing or not executable"
        )
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("PYTHON")
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(alignment_python),
        "-I",
        str(worker),
        "--target-wav",
        str(target_wav),
        "--against-wav",
        str(against_wav),
        "--json-output",
        str(output_path),
        "--max-output-bytes",
        str(max_output_bytes),
    ]
    if max_raw_candidates is not None:
        command.extend(["--max-raw-candidates", str(max_raw_candidates)])
    try:
        if budget is not None:
            result = run_bounded_child(
                command,
                budget=budget,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
            )
        else:
            result = process_runner(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                env=environment,
                **media_child_process_kwargs(),
            )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AudalignAdapterError("audalign worker could not complete") from error
    except ChildProcessTimeBudgetError as error:
        raise AudalignTimeBudgetError(str(error)) from error
    except ChildProcessMemoryBudgetError as error:
        raise AudalignMemoryBudgetError(str(error)) from error
    except ChildProcessBudgetError as error:
        raise AudalignBudgetError(str(error)) from error
    if result.returncode != 0:
        raise AudalignAdapterError(
            f"audalign worker exited with status {result.returncode}"
        )
    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AudalignAdapterError("audalign worker returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise AudalignAdapterError("audalign worker returned invalid JSON object")
    worker_status = payload.get(WORKER_STATUS_KEY)
    if worker_status == WORKER_STATUS_CANDIDATE_OVERFLOW:
        _remove_successful_output(output_path)
        raise AudalignCandidateLimitError(
            "audalign worker candidate limit exceeded"
        )
    if worker_status == WORKER_STATUS_OUTPUT_OVERFLOW:
        _remove_successful_output(output_path)
        raise AudalignOutputOverflowError(
            "audalign worker output limit exceeded"
        )
    if worker_status is not None:
        raise AudalignAdapterError("audalign worker returned an unknown status")
    match_info = payload.get("match_info")
    if match_info is None:
        _remove_successful_output(output_path)
        return AudalignMatchResult((), ())
    if not isinstance(match_info, dict):
        raise AudalignAdapterError("audalign worker returned invalid match info")
    offsets = match_info.get("offset_seconds")
    confidences = match_info.get("confidence")
    if not isinstance(offsets, list) or not isinstance(confidences, list):
        raise AudalignAdapterError("audalign worker returned incomplete match info")
    if len(offsets) != len(confidences):
        raise AudalignAdapterError("audalign worker returned mismatched evidence")
    candidates: list[AudalignCandidate] = []
    counts: list[int] = []
    for offset, confidence in zip(offsets, confidences):
        if isinstance(offset, bool) or not isinstance(offset, (int, float)):
            raise AudalignAdapterError("audalign worker returned a non-numeric offset")
        if isinstance(confidence, bool) or not isinstance(confidence, int) or confidence < 0:
            raise AudalignAdapterError("audalign worker returned an invalid confidence")
        text = _decimal_text(offset)
        candidates.append(
            AudalignCandidate(
                offset_seconds=text,
                confidence=confidence,
                offset_ticks=seconds_to_ticks(text),
            )
        )
        counts.append(confidence)
    _remove_successful_output(output_path)
    return AudalignMatchResult(tuple(candidates), tuple(counts))


def _remove_successful_output(output_path: Path) -> None:
    """Release one completed worker response before the next serial child."""
    try:
        output_path.unlink()
    except OSError:
        # The workspace recheck at the caller remains authoritative if a
        # response cannot be removed.
        pass



class AudalignProviderMismatchError(AudalignAdapterError):
    """Raised when the Audalign provider identity does not match production."""


class AudalignProviderMissingError(AudalignAdapterError):
    """Raised when the Audalign provider is missing or invalid."""


def validate_audalign_selection(
    selection: RuntimeAlignmentPython | None,
) -> RuntimeAlignmentPython:
    """Validate the exact persistent Audalign 1.3.1 selection without running media."""
    if not isinstance(selection, RuntimeAlignmentPython):
        raise AudalignProviderMissingError("audalign provider is missing")
    if selection.provider != AUDALIGN_PROVIDER:
        raise AudalignProviderMismatchError(
            f"audalign provider mismatch: expected {AUDALIGN_PROVIDER}, got {selection.provider}"
        )
    if selection.provider_version != AUDALIGN_VERSION:
        raise AudalignProviderMismatchError(
            f"audalign version mismatch: expected {AUDALIGN_VERSION}, got {selection.provider_version}"
        )
    if selection.upstream_commit != AUDALIGN_UPSTREAM_COMMIT:
        raise AudalignProviderMismatchError(
            f"audalign upstream mismatch: expected {AUDALIGN_UPSTREAM_COMMIT}, got {selection.upstream_commit}"
        )
    if not isinstance(selection.interpreter, str) or not selection.interpreter:
        raise AudalignProviderMissingError("audalign managed interpreter is invalid")
    try:
        interpreter = Path(selection.interpreter)
    except (TypeError, ValueError) as error:
        raise AudalignProviderMissingError(
            "audalign managed interpreter is invalid"
        ) from error
    try:
        details = interpreter.lstat()
        if (
            not interpreter.is_absolute()
            or not stat.S_ISREG(details.st_mode)
            or stat.S_ISLNK(details.st_mode)
            or not os.access(interpreter, os.X_OK)
        ):
            raise AudalignProviderMissingError(
                "audalign managed interpreter is not an absolute executable file"
            )
    except OSError as error:
        raise AudalignProviderMissingError(
            "audalign managed interpreter could not be checked"
        ) from error
    try:
        selection.validate(platform=current_platform())
    except RuntimeBindingError as error:
        raise AudalignProviderMissingError(str(error)) from error
    return selection


def _decimal_text(value: float) -> str:
    """Render the audalign float offset as its shortest exact decimal text."""
    try:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("offset must be finite")
        text = str(value) if isinstance(value, int) else repr(value)
    except (OverflowError, ValueError) as error:
        raise AudalignAdapterError("audalign worker returned a non-finite offset") from error
    text = text.removesuffix(".0")
    return text


@dataclass(frozen=True)
class AudalignCorrelationCandidate:
    offset_seconds: str
    offset_ticks: int


@dataclass(frozen=True)
class AudalignCorrelationResult:
    candidates: tuple[AudalignCorrelationCandidate, ...]


def run_audalign_correlation(
    alignment_python: Path,
    target_wav: Path,
    against_wav: Path,
    output_path: Path,
    *,
    process_runner: ProcessRunner = subprocess.run,
    timeout_seconds: float = WORKER_TIMEOUT_SECONDS,
    budget: ChildBudget | None = None,
    max_output_bytes: int = AUDALIGN_WORKER_MAX_OUTPUT_BYTES,
    max_raw_candidates: int = AUDALIGN_CORRELATION_MAX_RAW_CANDIDATES_PER_PROBE,
) -> AudalignCorrelationResult:
    """Run one CorrelationRecognizer recognize and parse its minimal payload.

    Uses the dedicated correlation_worker.py which verifies the frozen
    CorrelationConfig identity and emits only offset_seconds/sample_rate.
    Correlation scores are not part of the Roughcut admission or evidence
    contract; admission uses only the fixed-offset probe cluster.
    """
    worker = Path(__file__).with_name("correlation_worker.py")
    if (
        isinstance(max_output_bytes, bool)
        or not isinstance(max_output_bytes, int)
        or max_output_bytes <= 0
    ):
        raise AudalignAdapterError("audalign worker output limit is invalid")
    if (
        isinstance(max_raw_candidates, bool)
        or not isinstance(max_raw_candidates, int)
        or max_raw_candidates <= 0
    ):
        raise AudalignAdapterError("audalign worker candidate limit is invalid")
    if not alignment_python.is_file() or not os.access(alignment_python, os.X_OK):
        raise AudalignAdapterError("alignment Python interpreter is missing or not executable")
    try:
        output_path.unlink(missing_ok=True)
    except OSError as error:
        raise AudalignAdapterError("audalign worker output could not be cleared") from error
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("PYTHON")
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(alignment_python),
        "-I",
        str(worker),
        "--target-wav",
        str(target_wav),
        "--against-wav",
        str(against_wav),
        "--json-output",
        str(output_path),
        "--max-output-bytes",
        str(max_output_bytes),
        "--expected-config",
        json.dumps(
            audalign_correlation_typed_config(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ),
    ]
    command.extend(["--max-raw-candidates", str(max_raw_candidates)])
    try:
        try:
            if budget is not None:
                result = run_bounded_child(
                    command,
                    budget=budget,
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=environment,
                )
            else:
                result = process_runner(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout_seconds,
                    env=environment,
                    **media_child_process_kwargs(),
                )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise AudalignAdapterError("audalign worker could not complete") from error
        except ChildProcessTimeBudgetError as error:
            raise AudalignTimeBudgetError(str(error)) from error
        except ChildProcessMemoryBudgetError as error:
            raise AudalignMemoryBudgetError(str(error)) from error
        except ChildProcessBudgetError as error:
            raise AudalignBudgetError(str(error)) from error
        if result.returncode != 0:
            raise AudalignAdapterError(
                f"audalign worker exited with status {result.returncode}"
            )
        try:
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise AudalignAdapterError("audalign worker returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise AudalignAdapterError("audalign worker returned invalid JSON object")
        worker_status = payload.get(WORKER_STATUS_KEY)
        if worker_status == WORKER_STATUS_CANDIDATE_OVERFLOW:
            if set(payload) != {
                WORKER_STATUS_KEY,
                "candidate_count",
                "candidate_limit",
            }:
                raise AudalignAdapterError("audalign worker returned an invalid status")
            raise AudalignCandidateLimitError("audalign worker candidate limit exceeded")
        if worker_status == WORKER_STATUS_OUTPUT_OVERFLOW:
            if set(payload) != {WORKER_STATUS_KEY, "candidate_count"}:
                raise AudalignAdapterError("audalign worker returned an invalid status")
            raise AudalignOutputOverflowError("audalign worker output limit exceeded")
        if worker_status is not None:
            raise AudalignAdapterError("audalign worker returned an unknown status")
        if set(payload) != {"match_info"}:
            raise AudalignAdapterError("audalign worker returned non-closed JSON")
        match_info = payload["match_info"]
        if match_info is None:
            return AudalignCorrelationResult(())
        if not isinstance(match_info, dict) or set(match_info) != {
            "offset_seconds",
            "sample_rate",
        }:
            raise AudalignAdapterError("audalign worker returned invalid match info")
        sample_rate = match_info["sample_rate"]
        if (
            isinstance(sample_rate, bool)
            or not isinstance(sample_rate, int)
            or sample_rate != AUDALIGN_CORRELATION_SAMPLE_RATE_HZ
        ):
            raise AudalignAdapterError("audalign worker returned an invalid sample rate")
        offsets = match_info["offset_seconds"]
        if not isinstance(offsets, list):
            raise AudalignAdapterError("audalign worker returned incomplete match info")
        if not offsets:
            raise AudalignAdapterError("audalign worker returned an empty match")
        if len(offsets) > max_raw_candidates:
            raise AudalignCandidateLimitError("audalign worker candidate limit exceeded")
        candidates: list[AudalignCorrelationCandidate] = []
        for offset in offsets:
            if isinstance(offset, bool) or not isinstance(offset, (int, float)):
                raise AudalignAdapterError(
                    "audalign worker returned a non-numeric offset"
                )
            text = _decimal_text(offset)
            try:
                offset_ticks = seconds_to_ticks(text)
            except AlignmentError as error:
                raise AudalignAdapterError(
                    "audalign worker returned an invalid offset"
                ) from error
            candidates.append(
                AudalignCorrelationCandidate(
                    offset_seconds=text,
                    offset_ticks=offset_ticks,
                )
            )
        return AudalignCorrelationResult(tuple(candidates))
    finally:
        _remove_successful_output(output_path)
