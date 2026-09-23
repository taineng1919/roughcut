"""Read-only FFmpeg and ffprobe environment diagnostics."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class CommandDiagnostic:
    command: str
    status: str
    version: str | None
    detail: str | None

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


@dataclass(frozen=True)
class FFmpegEnvironment:
    ffmpeg: CommandDiagnostic
    ffprobe: CommandDiagnostic

    def to_dict(self) -> dict[str, dict[str, str | None]]:
        return {"ffmpeg": self.ffmpeg.to_dict(), "ffprobe": self.ffprobe.to_dict()}


class FFmpegRuntimeDriftError(RuntimeError):
    """Raised when a bound FFmpeg pair no longer has its exact identity."""


CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]
_VERSION_RE = re.compile(r"^(ffmpeg|ffprobe) version (\d+)\.(\d+)(?:\.(\d+))?")
_FILTER_COMPLEX_HELP_RE = re.compile(
    r"(?m)^[ \t]*-filter_complex[ \t]+<graph_description>(?=[ \t\r\n]|$)"
)
_MIN_VERSION = (8, 1, 0)
_MAX_VERSION = (10, 0, 0)


def _run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )


def diagnose_command(command: str, *, version_args: tuple[str, ...] = ("-version",)) -> CommandDiagnostic:
    executable, failure = _resolve_command(command)
    if failure is not None:
        return failure
    assert executable is not None
    try:
        result = _run_command([executable, *version_args])
    except PermissionError:
        return CommandDiagnostic(command, "not_executable", None, "permission denied")
    except (OSError, subprocess.TimeoutExpired) as error:
        return CommandDiagnostic(command, "unavailable", None, str(error))
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        return CommandDiagnostic(command, "unavailable", None, detail[0] if detail else "version command failed")
    output = (result.stdout or result.stderr).strip().splitlines()
    return CommandDiagnostic(command, "available", output[0] if output else None, None)


def diagnose_ffmpeg(
    *,
    ffmpeg_command: str = "ffmpeg",
    ffprobe_command: str = "ffprobe",
    full: bool = False,
    command_runner: CommandRunner = _run_command,
) -> FFmpegEnvironment:
    ffmpeg_path, ffmpeg_failure = _resolve_command(ffmpeg_command)
    ffprobe_path, ffprobe_failure = _resolve_command(ffprobe_command)
    if ffmpeg_failure is not None or ffprobe_failure is not None:
        return _incomplete_pair(
            ffmpeg_command,
            ffprobe_command,
            ffmpeg_failure,
            ffprobe_failure,
        )
    assert ffmpeg_path is not None and ffprobe_path is not None
    try:
        ffmpeg_version = _version_line(command_runner([ffmpeg_path, "-version"]), "ffmpeg")
        ffprobe_version = _version_line(command_runner([ffprobe_path, "-version"]), "ffprobe")
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        detail = _stable_failure_detail(error, "version check failed")
        return _unavailable_pair(ffmpeg_command, ffprobe_command, detail)
    ffmpeg_numeric = _numeric_version(ffmpeg_version, "ffmpeg")
    ffprobe_numeric = _numeric_version(ffprobe_version, "ffprobe")
    if ffmpeg_numeric is None or ffprobe_numeric is None:
        detail = (
            "ffmpeg numeric version could not be parsed"
            if ffmpeg_numeric is None
            else "ffprobe numeric version could not be parsed"
        )
        return _unavailable_pair(ffmpeg_command, ffprobe_command, detail)
    if not (_MIN_VERSION <= ffmpeg_numeric < _MAX_VERSION) or not (
        _MIN_VERSION <= ffprobe_numeric < _MAX_VERSION
    ):
        rejected = ffmpeg_numeric if not (_MIN_VERSION <= ffmpeg_numeric < _MAX_VERSION) else ffprobe_numeric
        return _unavailable_pair(
            ffmpeg_command,
            ffprobe_command,
            f"numeric version {_version_text(rejected)} is outside >=8.1.0,<10.0.0",
        )
    if ffmpeg_numeric != ffprobe_numeric:
        return _unavailable_pair(
            ffmpeg_command,
            ffprobe_command,
            "ffmpeg numeric version "
            f"{_version_text(ffmpeg_numeric)} and ffprobe numeric version "
            f"{_version_text(ffprobe_numeric)} differ",
        )
    try:
        help_result = command_runner([ffmpeg_path, "-hide_banner", "-h", "full"])
        encoder_result = command_runner([ffmpeg_path, "-hide_banner", "-encoders"])
    except (OSError, subprocess.TimeoutExpired) as error:
        return _unavailable_pair(
            ffmpeg_command,
            ffprobe_command,
            _stable_failure_detail(error, "static capability check failed"),
        )
    help_output = f"{help_result.stdout}\n{help_result.stderr}"
    encoder_output = f"{encoder_result.stdout}\n{encoder_result.stderr}"
    if help_result.returncode != 0 or _FILTER_COMPLEX_HELP_RE.search(help_output) is None:
        return _unavailable_pair(
            ffmpeg_command, ffprobe_command, "FFmpeg lacks -/filter_complex support"
        )
    if encoder_result.returncode != 0 or not re.search(r"^\s*V\S*\s+libx264\s", encoder_output, re.MULTILINE):
        return _unavailable_pair(ffmpeg_command, ffprobe_command, "FFmpeg lacks libx264 encoder")
    if not re.search(r"^\s*A\S*\s+aac\s", encoder_output, re.MULTILINE):
        return _unavailable_pair(ffmpeg_command, ffprobe_command, "FFmpeg lacks AAC encoder")
    if full:
        smoke_detail = _run_smoke(ffmpeg_path, ffprobe_path, command_runner)
        if smoke_detail is not None:
            return _unavailable_pair(ffmpeg_command, ffprobe_command, smoke_detail)
    return FFmpegEnvironment(
        CommandDiagnostic(ffmpeg_command, "available", ffmpeg_version, None),
        CommandDiagnostic(ffprobe_command, "available", ffprobe_version, None),
    )


def verify_runtime_pair(
    *,
    ffmpeg_command: str,
    ffmpeg_version: str,
    ffprobe_command: str,
    ffprobe_version: str,
    command_runner: CommandRunner = _run_command,
) -> tuple[str, str]:
    """Run each bound version command exactly once and compare exact lines."""
    ffmpeg_result = _attempt_runtime_version(
        command_runner, [ffmpeg_command, "-version"]
    )
    ffprobe_result = _attempt_runtime_version(
        command_runner, [ffprobe_command, "-version"]
    )
    if isinstance(ffmpeg_result, (OSError, subprocess.TimeoutExpired)):
        raise FFmpegRuntimeDriftError(
            "bound FFmpeg runtime is unavailable"
        ) from ffmpeg_result
    if isinstance(ffprobe_result, (OSError, subprocess.TimeoutExpired)):
        raise FFmpegRuntimeDriftError(
            "bound FFmpeg runtime is unavailable"
        ) from ffprobe_result
    try:
        current_ffmpeg = _version_line(ffmpeg_result, "ffmpeg")
        current_ffprobe = _version_line(ffprobe_result, "ffprobe")
    except ValueError as error:
        raise FFmpegRuntimeDriftError("bound FFmpeg runtime is unavailable") from error
    if current_ffmpeg != ffmpeg_version or current_ffprobe != ffprobe_version:
        raise FFmpegRuntimeDriftError("bound FFmpeg runtime identity changed")
    ffmpeg_numeric = _numeric_version(current_ffmpeg, "ffmpeg")
    ffprobe_numeric = _numeric_version(current_ffprobe, "ffprobe")
    if (
        ffmpeg_numeric is None
        or ffprobe_numeric is None
        or ffmpeg_numeric != ffprobe_numeric
        or not (_MIN_VERSION <= ffmpeg_numeric < _MAX_VERSION)
    ):
        raise FFmpegRuntimeDriftError("bound FFmpeg runtime pair is incompatible")
    return current_ffmpeg, current_ffprobe


def verify_runtime_ffmpeg(
    *,
    ffmpeg_command: str,
    ffmpeg_version: str,
    command_runner: CommandRunner = _run_command,
) -> str:
    """Verify exactly one bound tool, the FFmpeg selection.

    The Cloud transcription route binds ``ffmpeg_tool_selection_hash`` and
    nothing else about the persistent tool set, and the Cloud path never invokes
    ffprobe.  Checking the pair there would let a tool outside the closed Cloud
    execution identity decide whether an identical Cloud input runs at all, so
    the Cloud route verifies only the tool it actually depends on.
    """

    result = _attempt_runtime_version(command_runner, [ffmpeg_command, "-version"])
    if isinstance(result, (OSError, subprocess.TimeoutExpired)):
        raise FFmpegRuntimeDriftError("bound FFmpeg runtime is unavailable") from result
    try:
        current_ffmpeg = _version_line(result, "ffmpeg")
    except ValueError as error:
        raise FFmpegRuntimeDriftError("bound FFmpeg runtime is unavailable") from error
    if current_ffmpeg != ffmpeg_version:
        raise FFmpegRuntimeDriftError("bound FFmpeg runtime identity changed")
    ffmpeg_numeric = _numeric_version(current_ffmpeg, "ffmpeg")
    if ffmpeg_numeric is None or not (_MIN_VERSION <= ffmpeg_numeric < _MAX_VERSION):
        raise FFmpegRuntimeDriftError("bound FFmpeg runtime is incompatible")
    return current_ffmpeg


def _resolve_command(command: str) -> tuple[str | None, CommandDiagnostic | None]:
    requested_path = Path(command)
    if requested_path.parent != Path("."):
        if not requested_path.exists():
            return None, CommandDiagnostic(command, "missing", None, "command path does not exist")
        if not requested_path.is_file() or not os.access(requested_path, os.X_OK):
            return None, CommandDiagnostic(command, "not_executable", None, "command path is not executable")
        return str(requested_path), None
    resolved = shutil.which(command)
    if resolved is None:
        return None, CommandDiagnostic(command, "missing", None, "command is not on PATH")
    return resolved, None


def _attempt_runtime_version(
    command_runner: CommandRunner, command: list[str]
) -> subprocess.CompletedProcess[str] | OSError | subprocess.TimeoutExpired:
    try:
        return command_runner(command)
    except (OSError, subprocess.TimeoutExpired) as error:
        return error


def _version_line(result: subprocess.CompletedProcess[str], name: str) -> str:
    lines = (result.stdout or result.stderr).strip().splitlines()
    if result.returncode != 0 or not lines or not lines[0].startswith(f"{name} version "):
        raise ValueError(f"{name} version command failed")
    return lines[0]


def _numeric_version(version: str, name: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.match(version)
    if match is None or match.group(1) != name:
        return None
    return tuple(int(part or 0) for part in match.groups()[1:])  # type: ignore[return-value]


def _version_text(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def _unavailable_pair(ffmpeg_command: str, ffprobe_command: str, detail: str) -> FFmpegEnvironment:
    return FFmpegEnvironment(
        CommandDiagnostic(ffmpeg_command, "unavailable", None, detail),
        CommandDiagnostic(ffprobe_command, "unavailable", None, detail),
    )


def _incomplete_pair(
    ffmpeg_command: str,
    ffprobe_command: str,
    ffmpeg_failure: CommandDiagnostic | None,
    ffprobe_failure: CommandDiagnostic | None,
) -> FFmpegEnvironment:
    detail = "FFmpeg pair validation is incomplete"
    return FFmpegEnvironment(
        ffmpeg_failure
        or CommandDiagnostic(ffmpeg_command, "unavailable", None, detail),
        ffprobe_failure
        or CommandDiagnostic(ffprobe_command, "unavailable", None, detail),
    )


def _stable_failure_detail(error: BaseException, fallback: str) -> str:
    if isinstance(error, subprocess.TimeoutExpired):
        return f"{fallback}: command timed out"
    if isinstance(error, PermissionError):
        return f"{fallback}: permission denied"
    return fallback


def _run_smoke(ffmpeg: str, ffprobe: str, command_runner: CommandRunner) -> str | None:
    try:
        with tempfile.TemporaryDirectory(prefix="roughcut-ffmpeg-smoke-") as directory:
            root = Path(directory)
            filter_script = root / "filter.txt"
            output = root / "smoke.mp4"
            filter_script.write_text("[0:v]format=yuv420p[vout];[1:a]anull[aout]\n", encoding="utf-8")
            encoded = command_runner(
                [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "color=c=black:s=64x64:r=25:d=0.2", "-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=48000:duration=0.2", "-/filter_complex",
                    str(filter_script), "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", "0.2", "-f", "mp4", "-y", str(output),
                ]
            )
            if encoded.returncode != 0 or not output.is_file():
                return "FFmpeg full smoke encode failed"
            probed = command_runner(
                [ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name", "-of", "json", str(output)]
            )
            if probed.returncode != 0:
                return "FFmpeg full smoke probe failed"
            if not _valid_smoke_payload(probed.stdout):
                return "FFmpeg full smoke output failed verification"
    except (OSError, subprocess.TimeoutExpired):
        return "FFmpeg full smoke or cleanup failed"
    return None


def _valid_smoke_payload(raw: str) -> bool:
    try:
        payload = json.loads(raw, object_pairs_hook=_unique_json_object)
    except (json.JSONDecodeError, ValueError):
        return False
    if not isinstance(payload, dict) or set(payload) != {
        "streams",
        "format",
        "programs",
        "stream_groups",
    }:
        return False
    if payload["programs"] != [] or payload["stream_groups"] != []:
        return False
    streams = payload["streams"]
    if not isinstance(streams, list) or len(streams) != 2:
        return False
    codecs: set[tuple[str, str]] = set()
    for stream in streams:
        if not isinstance(stream, dict) or set(stream) != {"codec_type", "codec_name"}:
            return False
        codec_type = stream["codec_type"]
        codec_name = stream["codec_name"]
        if not isinstance(codec_type, str) or not isinstance(codec_name, str):
            return False
        codecs.add((codec_type, codec_name))
    if codecs != {("video", "h264"), ("audio", "aac")}:
        return False
    format_payload = payload["format"]
    if not isinstance(format_payload, dict) or set(format_payload) != {"duration"}:
        return False
    duration_raw = format_payload["duration"]
    if not isinstance(duration_raw, str):
        return False
    try:
        duration = float(duration_raw)
    except ValueError:
        return False
    return math.isfinite(duration) and 0 < duration <= 1


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result
