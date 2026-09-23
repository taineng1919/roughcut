"""Decode source audio to the audalign analysis WAV contract."""

from __future__ import annotations

import os
import subprocess
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from roughcut.adapters.child_budget import (
    ChildBudget,
    ChildProcessBudgetError,
    ChildProcessMemoryBudgetError,
    ChildProcessTimeBudgetError,
    run_bounded_child,
)
from roughcut.adapters.media_operation_store import media_child_process_kwargs

ANALYSIS_SAMPLE_RATE_HZ = 44_100
ANALYSIS_CHANNELS = 1
ANALYSIS_SAMPLE_FORMAT = "s16le"
ANALYSIS_SAMPLE_WIDTH_BYTES = 2
ANALYSIS_AUDIO_STREAM = "0:a:0"


class FFmpegAlignmentError(RuntimeError):
    """Raised when FFmpeg cannot prepare the audalign analysis WAV."""


class FFmpegAlignmentBudgetError(FFmpegAlignmentError):
    """Raised when FFmpeg exceeds the alignment operation budget."""


class FFmpegAlignmentTimeBudgetError(FFmpegAlignmentBudgetError):
    """Raised when FFmpeg exceeds the alignment wall-time budget."""


class FFmpegAlignmentMemoryBudgetError(FFmpegAlignmentBudgetError):
    """Raised when FFmpeg exceeds the alignment memory budget."""


@dataclass(frozen=True)
class AlignmentAudioDecode:
    ffmpeg_path: str
    ffmpeg_version: str
    audio_stream: str
    sample_rate_hz: int
    channels: int
    sample_format: str
    container_format: str


ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]


def decode_alignment_audio(
    source_path: Path,
    output_path: Path,
    *,
    ffmpeg_command: str,
    ffmpeg_version: str | None = None,
    channel: str | None = None,
    timeout_seconds: float = 600.0,
    memory_limit: int = 0,
    budget: ChildBudget | None = None,
    process_runner: ProcessRunner = subprocess.run,
) -> AlignmentAudioDecode:
    """Decode the first audio stream of one source to a mono 44.1 kHz WAV.

    `channel` is the bounded L/R fallback: `None` is the default mono mix,
    `"left"` keeps the full first audio stream mapped as `0:a:0` and adds the
    `pan=mono|c0=c0` filter, `"right"` uses `pan=mono|c0=c1`. The filter is
    passed as separate argv entries, never as a shell string. Every child
    shares the operation deadline and memory ceiling through `budget` when
    supplied.
    """
    executable = Path(ffmpeg_command)
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise FFmpegAlignmentError("FFmpeg command is missing or not executable")
    version = ffmpeg_version or _ffmpeg_version(
        executable,
        process_runner,
        timeout_seconds=timeout_seconds,
        budget=budget,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(executable),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(source_path),
        "-map",
        ANALYSIS_AUDIO_STREAM,
    ]
    if channel == "left":
        command.extend(["-af", "pan=mono|c0=c0"])
    elif channel == "right":
        command.extend(["-af", "pan=mono|c0=c1"])
    command.extend(
        [
            "-map_metadata",
            "-1",
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(ANALYSIS_SAMPLE_RATE_HZ),
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            str(output_path),
        ]
    )
    completed = False
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
                **media_child_process_kwargs(),
            )
        if result.returncode != 0:
            raise FFmpegAlignmentError(
                "FFmpeg could not decode the source audio"
            )
        _validate_wav(output_path)
        completed = True
    except (OSError, subprocess.TimeoutExpired) as error:
        raise FFmpegAlignmentError(
            "FFmpeg alignment decode could not complete"
        ) from error
    except ChildProcessTimeBudgetError as error:
        raise FFmpegAlignmentTimeBudgetError(str(error)) from error
    except ChildProcessMemoryBudgetError as error:
        raise FFmpegAlignmentMemoryBudgetError(str(error)) from error
    except ChildProcessBudgetError as error:
        raise FFmpegAlignmentBudgetError(str(error)) from error
    finally:
        if not completed:
            output_path.unlink(missing_ok=True)
    return AlignmentAudioDecode(
        ffmpeg_path=str(executable),
        ffmpeg_version=version,
        audio_stream=ANALYSIS_AUDIO_STREAM,
        sample_rate_hz=ANALYSIS_SAMPLE_RATE_HZ,
        channels=ANALYSIS_CHANNELS,
        sample_format=ANALYSIS_SAMPLE_FORMAT,
        container_format="wav",
    )


def extract_wav_window(
    wav_path: Path,
    output_path: Path,
    *,
    start_ticks: int,
    end_ticks: int,
    ffmpeg_command: str,
    timeout_seconds: float = 300.0,
    memory_limit: int = 0,
    budget: ChildBudget | None = None,
    process_runner: ProcessRunner = subprocess.run,
) -> None:
    """Cut one exact source-local window from an analysis WAV in ticks."""
    executable = Path(ffmpeg_command)
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise FFmpegAlignmentError("FFmpeg command is missing or not executable")
    if end_ticks <= start_ticks or start_ticks < 0:
        raise FFmpegAlignmentError("rejected an invalid window range")
    start_seconds = start_ticks / 120_000
    duration_ticks = end_ticks - start_ticks
    duration_seconds = duration_ticks / 120_000
    output_path.parent.mkdir(parents=True, exist_ok=True)
    completed = False
    try:
        if budget is not None:
            result = run_bounded_child(
                [
                    str(executable),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-y",
                    "-i",
                    str(wav_path),
                    "-ss",
                    _seconds_text(start_seconds),
                    "-t",
                    _seconds_text(duration_seconds),
                    "-map_metadata",
                    "-1",
                    "-ac",
                    "1",
                    "-ar",
                    str(ANALYSIS_SAMPLE_RATE_HZ),
                    "-c:a",
                    "pcm_s16le",
                    "-f",
                    "wav",
                    str(output_path),
                ],
                budget=budget,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        else:
            result = process_runner(
                [
                    str(executable),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-y",
                    "-i",
                    str(wav_path),
                    "-ss",
                    _seconds_text(start_seconds),
                    "-t",
                    _seconds_text(duration_seconds),
                    "-map_metadata",
                    "-1",
                    "-ac",
                    "1",
                    "-ar",
                    str(ANALYSIS_SAMPLE_RATE_HZ),
                    "-c:a",
                    "pcm_s16le",
                    "-f",
                    "wav",
                    str(output_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                **media_child_process_kwargs(),
            )
        if result.returncode != 0:
            raise FFmpegAlignmentError("FFmpeg could not cut the analysis window")
        _validate_wav(output_path)
        completed = True
    except (OSError, subprocess.TimeoutExpired) as error:
        raise FFmpegAlignmentError(
            "FFmpeg alignment window cut could not complete"
        ) from error
    except ChildProcessTimeBudgetError as error:
        raise FFmpegAlignmentTimeBudgetError(str(error)) from error
    except ChildProcessMemoryBudgetError as error:
        raise FFmpegAlignmentMemoryBudgetError(str(error)) from error
    except ChildProcessBudgetError as error:
        raise FFmpegAlignmentBudgetError(str(error)) from error
    finally:
        if not completed:
            output_path.unlink(missing_ok=True)


def _seconds_text(seconds: float) -> str:
    text = repr(float(seconds))
    text = text.removesuffix(".0")
    return text


def _ffmpeg_version(
    executable: Path,
    process_runner: ProcessRunner,
    *,
    timeout_seconds: float,
    budget: ChildBudget | None,
) -> str:
    try:
        if budget is not None:
            result = run_bounded_child(
                [str(executable), "-version"],
                budget=budget,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        else:
            result = process_runner(
                [str(executable), "-version"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                **media_child_process_kwargs(),
            )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise FFmpegAlignmentError("FFmpeg version could not be read") from error
    except ChildProcessTimeBudgetError as error:
        raise FFmpegAlignmentTimeBudgetError(str(error)) from error
    except ChildProcessMemoryBudgetError as error:
        raise FFmpegAlignmentMemoryBudgetError(str(error)) from error
    except ChildProcessBudgetError as error:
        raise FFmpegAlignmentBudgetError(str(error)) from error
    lines = (result.stdout or result.stderr).strip().splitlines()
    if result.returncode != 0 or not lines or not lines[0].startswith("ffmpeg version "):
        raise FFmpegAlignmentError("FFmpeg version could not be read")
    return lines[0]


def _validate_wav(path: Path) -> None:
    try:
        with wave.open(str(path), "rb") as pcm:
            valid = (
                pcm.getframerate() == ANALYSIS_SAMPLE_RATE_HZ
                and pcm.getnchannels() == ANALYSIS_CHANNELS
                and pcm.getsampwidth() == ANALYSIS_SAMPLE_WIDTH_BYTES
                and pcm.getcomptype() == "NONE"
                and pcm.getnframes() > 0
            )
    except (OSError, EOFError, wave.Error) as error:
        raise FFmpegAlignmentError("FFmpeg output is not readable PCM") from error
    if not valid:
        raise FFmpegAlignmentError("FFmpeg output does not match the WAV contract")
