"""Structured ffprobe adapter."""

from __future__ import annotations

import json
import shutil
import subprocess
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from roughcut.adapters.runtime_binding import (
    RuntimeBindingError,
    resolve_runtime_tool,
)
from roughcut.domain.project import MediaProbe, ProjectError
from roughcut.domain.time import TICKS_PER_SECOND


class ProbeError(ProjectError):
    """Raised when media cannot be probed."""


def probe_media(
    source_path: Path, *, ffprobe_command: str | None = None
) -> MediaProbe:
    try:
        selected = resolve_runtime_tool(
            "ffprobe", explicit_command=ffprobe_command
        ).command
    except RuntimeBindingError as error:
        raise ProbeError(str(error)) from error
    executable = shutil.which(selected)
    if executable is None:
        raise ProbeError("ffprobe command is missing")
    result = subprocess.run(
        [
            executable,
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(source_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise ProbeError("ffprobe could not read the source")
    try:
        data: Any = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ProbeError("ffprobe returned invalid JSON") from error
    if not isinstance(data, dict):
        raise ProbeError("ffprobe returned an invalid result")
    return parse_ffprobe_json(data)


def parse_ffprobe_json(data: dict[str, Any]) -> MediaProbe:
    format_data = data.get("format")
    streams = data.get("streams")
    if not isinstance(format_data, dict) or not isinstance(streams, list):
        raise ProbeError("ffprobe result has no format or streams")
    typed_streams = [stream for stream in streams if isinstance(stream, dict)]
    video = next((stream for stream in typed_streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in typed_streams if stream.get("codec_type") == "audio"), None)
    if video is None and audio is None:
        raise ProbeError("ffprobe result has no audio or video stream")

    duration = _first_decimal(format_data.get("duration"), *[s.get("duration") for s in typed_streams])
    if duration is None or duration <= 0:
        raise ProbeError("ffprobe result has no positive duration")
    container_start = _decimal_or_zero(format_data.get("start_time"))
    first_stream = video if video is not None else audio
    assert first_stream is not None
    first_content = _decimal_or_default(first_stream.get("start_time"), container_start)

    average_rate = _rate(video.get("avg_frame_rate")) if video is not None else None
    real_rate = _rate(video.get("r_frame_rate")) if video is not None else None
    nominal_rate = average_rate or real_rate
    rotation = _rotation(video) if video is not None else 0
    return MediaProbe(
        duration_ticks=_seconds_to_ticks(duration),
        container_start_ticks=_seconds_to_ticks(container_start),
        first_content_ticks=_seconds_to_ticks(first_content),
        video_codec=_optional_string(video, "codec_name"),
        width=_optional_int(video, "width"),
        height=_optional_int(video, "height"),
        nominal_frame_rate=nominal_rate,
        is_vfr=average_rate is not None and real_rate is not None and average_rate != real_rate,
        audio_codec=_optional_string(audio, "codec_name"),
        audio_sample_rate=_optional_int_string(audio, "sample_rate"),
        rotation_degrees=rotation,
    )


def _seconds_to_ticks(value: Decimal) -> int:
    ticks = value * TICKS_PER_SECOND
    return int(ticks.to_integral_value(rounding=ROUND_HALF_UP))


def _decimal(value: object) -> Decimal | None:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _first_decimal(*values: object) -> Decimal | None:
    for value in values:
        parsed = _decimal(value)
        if parsed is not None:
            return parsed
    return None


def _decimal_or_zero(value: object) -> Decimal:
    return _decimal(value) or Decimal(0)


def _decimal_or_default(value: object, default: Decimal) -> Decimal:
    parsed = _decimal(value)
    return parsed if parsed is not None else default


def _rate(value: object) -> dict[str, int] | None:
    if not isinstance(value, str) or "/" not in value:
        return None
    numerator_text, denominator_text = value.split("/", 1)
    try:
        numerator = int(numerator_text)
        denominator = int(denominator_text)
    except ValueError:
        return None
    if numerator <= 0 or denominator <= 0:
        return None
    return {"numerator": numerator, "denominator": denominator}


def _optional_string(stream: dict[str, Any] | None, key: str) -> str | None:
    if stream is None:
        return None
    value = stream.get(key)
    return value if isinstance(value, str) else None


def _optional_int(stream: dict[str, Any] | None, key: str) -> int | None:
    if stream is None:
        return None
    value = stream.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_int_string(stream: dict[str, Any] | None, key: str) -> int | None:
    value = _optional_string(stream, key)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _rotation(video: dict[str, Any]) -> int:
    side_data = video.get("side_data_list")
    if isinstance(side_data, list):
        for entry in side_data:
            if isinstance(entry, dict) and isinstance(entry.get("rotation"), int):
                return int(entry["rotation"])
    tags = video.get("tags")
    if isinstance(tags, dict):
        try:
            return int(tags.get("rotate", 0))
        except (TypeError, ValueError):
            pass
    return 0
