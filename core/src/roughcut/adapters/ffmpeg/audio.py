"""Decode source audio to the controlled FunASR PCM contract.

This module owns two sibling preparation profiles behind one FFmpeg resolution
boundary: the existing local FunASR ``16 kHz / mono / PCM s16le / WAV`` profile
and the cloud ``16 kHz / mono / FLAC s16`` profile.  The profiles are kept
separate on purpose; neither one changes the other's contract.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import wave
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from roughcut.adapters.media_operation_store import media_child_process_kwargs
from roughcut.adapters.runtime_binding import (
    RuntimeBindingError,
    resolve_runtime_tool,
)

PCM_SAMPLE_RATE_HZ = 16_000
PCM_CHANNELS = 1
PCM_SAMPLE_FORMAT = "s16le"
PCM_CONTAINER_FORMAT = "wav"
PCM_SAMPLE_WIDTH_BYTES = 2
PCM_AUDIO_STREAM = "0:a:0"

FLAC_SAMPLE_RATE_HZ = 16_000
FLAC_CHANNELS = 1
FLAC_SAMPLE_FORMAT = "s16"
FLAC_BITS_PER_SAMPLE = 16
FLAC_CODEC = "flac"
FLAC_CONTAINER_FORMAT = "flac"
FLAC_AUDIO_STREAM = "0:a:0"
FLAC_MAGIC = b"fLaC"
FLAC_STREAMINFO_TYPE = 0
FLAC_STREAMINFO_BYTES = 34
_FLAC_STREAMINFO_PREFIX_BYTES = 8 + 18


class FFmpegAudioError(RuntimeError):
    """Raised when FFmpeg cannot produce a controlled audio profile."""


@dataclass(frozen=True)
class PCMDecode:
    ffmpeg_path: str
    ffmpeg_version: str
    audio_stream: str
    sample_rate_hz: int
    channels: int
    sample_format: str
    container_format: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]


def decode_audio_to_pcm(
    source_path: Path,
    output_path: Path,
    *,
    ffmpeg_command: str | None = None,
    ffmpeg_version: str | None = None,
    timeout_seconds: float = 3600.0,
    process_runner: ProcessRunner = subprocess.run,
) -> PCMDecode:
    try:
        selected = resolve_runtime_tool(
            "ffmpeg", explicit_command=ffmpeg_command
        ).command
    except RuntimeBindingError as error:
        raise FFmpegAudioError(str(error)) from error
    executable = _resolve_executable(selected)
    version = ffmpeg_version or _ffmpeg_version(executable, process_runner)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    completed = False
    try:
        result = process_runner(
            [
                executable,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-i",
                str(source_path),
                "-map",
                PCM_AUDIO_STREAM,
                "-map_metadata",
                "-1",
                "-vn",
                "-ac",
                str(PCM_CHANNELS),
                "-ar",
                str(PCM_SAMPLE_RATE_HZ),
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
            raise FFmpegAudioError("FFmpeg could not decode the source audio")
        _validate_pcm(output_path)
        completed = True
    except (OSError, subprocess.TimeoutExpired) as error:
        raise FFmpegAudioError("FFmpeg audio decode could not complete") from error
    finally:
        if not completed:
            output_path.unlink(missing_ok=True)
    return PCMDecode(
        ffmpeg_path=executable,
        ffmpeg_version=version,
        audio_stream=PCM_AUDIO_STREAM,
        sample_rate_hz=PCM_SAMPLE_RATE_HZ,
        channels=PCM_CHANNELS,
        sample_format=PCM_SAMPLE_FORMAT,
        container_format=PCM_CONTAINER_FORMAT,
    )


@dataclass(frozen=True)
class FLACEncode:
    ffmpeg_path: str
    ffmpeg_version: str
    audio_stream: str
    sample_rate_hz: int
    channels: int
    sample_format: str
    bits_per_sample: int
    container_format: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def encode_audio_to_flac(
    source_path: Path,
    output_path: Path,
    *,
    ffmpeg_command: str | None = None,
    ffmpeg_version: str | None = None,
    timeout_seconds: float = 3600.0,
    process_runner: ProcessRunner = subprocess.run,
) -> FLACEncode:
    """Prepare the cloud 16 kHz mono FLAC s16 profile in one FFmpeg pass."""

    try:
        selected = resolve_runtime_tool(
            "ffmpeg", explicit_command=ffmpeg_command
        ).command
    except RuntimeBindingError as error:
        raise FFmpegAudioError(str(error)) from error
    executable = _resolve_executable(selected)
    version = ffmpeg_version or _ffmpeg_version(executable, process_runner)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    completed = False
    try:
        stream_info = _run_flac_encode(
            executable, source_path, output_path, timeout_seconds, process_runner
        )
        completed = True
    except (OSError, subprocess.TimeoutExpired) as error:
        raise FFmpegAudioError("FFmpeg audio preparation could not complete") from error
    finally:
        if not completed:
            output_path.unlink(missing_ok=True)
    return FLACEncode(
        ffmpeg_path=executable,
        ffmpeg_version=version,
        audio_stream=FLAC_AUDIO_STREAM,
        sample_rate_hz=stream_info.sample_rate_hz,
        channels=stream_info.channels,
        sample_format=FLAC_SAMPLE_FORMAT,
        bits_per_sample=stream_info.bits_per_sample,
        container_format=FLAC_CONTAINER_FORMAT,
    )


@dataclass(frozen=True)
class _FLACStreamInfo:
    sample_rate_hz: int
    channels: int
    bits_per_sample: int
    total_samples: int


def _run_flac_encode(
    executable: str,
    source_path: Path,
    output_path: Path,
    timeout_seconds: float,
    process_runner: ProcessRunner,
) -> _FLACStreamInfo:
    result = process_runner(
        [
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(source_path),
            "-map",
            FLAC_AUDIO_STREAM,
            "-map_metadata",
            "-1",
            "-vn",
            "-ac",
            str(FLAC_CHANNELS),
            "-ar",
            str(FLAC_SAMPLE_RATE_HZ),
            "-sample_fmt",
            FLAC_SAMPLE_FORMAT,
            "-c:a",
            FLAC_CODEC,
            "-f",
            FLAC_CONTAINER_FORMAT,
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
        raise FFmpegAudioError("FFmpeg could not prepare the cloud audio profile")
    stream_info = _read_flac_stream_info(output_path)
    _validate_flac_stream_info(stream_info)
    return stream_info


def _read_flac_stream_info(path: Path) -> _FLACStreamInfo:
    """Read the fixed FLAC STREAMINFO header without an extra process."""

    try:
        with path.open("rb") as flac:
            prefix = flac.read(_FLAC_STREAMINFO_PREFIX_BYTES)
    except OSError as error:
        raise FFmpegAudioError("FFmpeg output is not readable FLAC") from error
    if len(prefix) < _FLAC_STREAMINFO_PREFIX_BYTES or not prefix.startswith(FLAC_MAGIC):
        raise FFmpegAudioError("FFmpeg output is not a FLAC stream")
    block_type = prefix[4] & 0x7F
    block_length = int.from_bytes(prefix[5:8], "big")
    if block_type != FLAC_STREAMINFO_TYPE or block_length != FLAC_STREAMINFO_BYTES:
        raise FFmpegAudioError("FFmpeg output does not start with FLAC stream info")
    packed = int.from_bytes(prefix[18:26], "big")
    sample_rate_hz = packed >> 44
    channels = ((packed >> 41) & 0x07) + 1
    bits_per_sample = ((packed >> 36) & 0x1F) + 1
    total_samples = packed & ((1 << 36) - 1)
    return _FLACStreamInfo(
        sample_rate_hz=sample_rate_hz,
        channels=channels,
        bits_per_sample=bits_per_sample,
        total_samples=total_samples,
    )


def _validate_flac_stream_info(stream_info: _FLACStreamInfo) -> None:
    valid = (
        stream_info.sample_rate_hz == FLAC_SAMPLE_RATE_HZ
        and stream_info.channels == FLAC_CHANNELS
        and stream_info.bits_per_sample == FLAC_BITS_PER_SAMPLE
        and stream_info.total_samples > 0
    )
    if not valid:
        raise FFmpegAudioError("FFmpeg output does not match the FLAC contract")


def _resolve_executable(command: str) -> str:
    requested = Path(command)
    if requested.parent != Path("."):
        if not requested.is_file() or not os.access(requested, os.X_OK):
            raise FFmpegAudioError("FFmpeg command is missing or not executable")
        return str(requested)
    resolved = shutil.which(command)
    if resolved is None:
        raise FFmpegAudioError("FFmpeg command is missing or not executable")
    return resolved


def _ffmpeg_version(executable: str, process_runner: ProcessRunner) -> str:
    try:
        result = process_runner(
            [executable, "-version"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            **media_child_process_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise FFmpegAudioError("FFmpeg version could not be read") from error
    lines = (result.stdout or result.stderr).strip().splitlines()
    if result.returncode != 0 or not lines or not lines[0].startswith("ffmpeg version "):
        raise FFmpegAudioError("FFmpeg version could not be read")
    return lines[0]


def _validate_pcm(path: Path) -> None:
    try:
        with wave.open(str(path), "rb") as pcm:
            valid = (
                pcm.getframerate() == PCM_SAMPLE_RATE_HZ
                and pcm.getnchannels() == PCM_CHANNELS
                and pcm.getsampwidth() == PCM_SAMPLE_WIDTH_BYTES
                and pcm.getcomptype() == "NONE"
                and pcm.getnframes() > 0
            )
    except (OSError, EOFError, wave.Error) as error:
        raise FFmpegAudioError("FFmpeg output is not readable PCM") from error
    if not valid:
        raise FFmpegAudioError("FFmpeg output does not match the PCM contract")
