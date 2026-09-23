"""Probe and decode verification for an unpublished MP4 candidate."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Any

from roughcut.adapters.ffmpeg.audio_quota import audio_sample_quota_matches
from roughcut.adapters.media_operation_store import media_child_process_kwargs
from roughcut.domain.render import (
    MultiSourceRenderPlan,
    RenderPlanLike,
    RenderSchedule,
    derive_render_schedule,
    render_has_audio,
)
from roughcut.domain.time import TICKS_PER_SECOND


@dataclass(frozen=True)
class VerificationReport:
    accepted: bool
    duration_ticks: int | None
    checks: dict[str, bool]
    probe: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "duration_ticks": self.duration_ticks,
            "checks": self.checks,
            "probe": self.probe,
        }


class RenderVerificationError(RuntimeError):
    """Raised when a candidate output does not meet the frozen plan."""

    def __init__(self, report: VerificationReport | None = None) -> None:
        super().__init__("render output verification failed")
        self.report = report


ProcessRunner = Callable[..., subprocess.CompletedProcess[Any]]
_AUDIO_SAMPLE_COUNT = re.compile(r"Number of samples:\s*(\d+)\s*$", re.MULTILINE)


def verify_render_output(
    output_path: Path,
    plan: RenderPlanLike,
    *,
    schedule: RenderSchedule | None = None,
    process_runner: ProcessRunner = subprocess.run,
) -> VerificationReport:
    schedule = schedule or derive_render_schedule(plan)
    try:
        probe_result = process_runner(
            [
                plan.ffprobe.resolved_path,
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-count_frames",
                "-of",
                "json",
                str(output_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            **media_child_process_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RenderVerificationError() from error
    if probe_result.returncode != 0:
        raise RenderVerificationError()
    try:
        probe_data: Any = json.loads(probe_result.stdout)
    except json.JSONDecodeError as error:
        raise RenderVerificationError() from error
    if not isinstance(probe_data, dict):
        raise RenderVerificationError()

    try:
        decode_result = process_runner(
            [
                plan.ffmpeg.resolved_path,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-i",
                str(output_path),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-f",
                "null",
                "-",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3600,
            **media_child_process_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RenderVerificationError() from error
    decoded_audio_samples: int | None = None
    has_audio = render_has_audio(plan)
    if has_audio:
        try:
            audio_decode_result = process_runner(
                [
                    plan.ffmpeg.resolved_path,
                    "-hide_banner",
                    "-loglevel",
                    "info",
                    "-nostats",
                    "-nostdin",
                    "-i",
                    str(output_path),
                    "-map",
                    "0:a:0",
                    "-af",
                    "astats=metadata=0:reset=0",
                    "-f",
                    "null",
                    "-",
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=3600,
                **media_child_process_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RenderVerificationError() from error
        if audio_decode_result.returncode == 0:
            decoded_audio_samples = _decoded_audio_sample_count(audio_decode_result.stderr)
    report = evaluate_output_probe(
        plan,
        probe_data,
        schedule=schedule,
        output_size=output_path.stat().st_size if output_path.is_file() else 0,
        decode_succeeded=(
            decode_result.returncode == 0
            and (not has_audio or decoded_audio_samples is not None)
        ),
        decoded_audio_samples=decoded_audio_samples,
        faststart=_has_faststart(output_path),
    )
    if not report.accepted:
        raise RenderVerificationError(report)
    return report


def evaluate_output_probe(
    plan: RenderPlanLike,
    data: dict[str, Any],
    *,
    schedule: RenderSchedule | None = None,
    output_size: int,
    decode_succeeded: bool,
    decoded_audio_samples: int | None = None,
    faststart: bool,
) -> VerificationReport:
    schedule = schedule or derive_render_schedule(plan)
    format_data = data.get("format")
    streams = data.get("streams")
    typed_format = format_data if isinstance(format_data, dict) else {}
    typed_streams = [item for item in streams if isinstance(item, dict)] if isinstance(streams, list) else []
    videos = [item for item in typed_streams if item.get("codec_type") == "video"]
    audios = [item for item in typed_streams if item.get("codec_type") == "audio"]
    video = videos[0] if len(videos) == 1 else {}
    audio = audios[0] if len(audios) == 1 else {}
    duration_ticks = _duration_ticks(typed_format, typed_streams)
    start_ticks = _start_ticks(typed_format, typed_streams)
    expected_rate = Fraction(
        plan.output_settings.frame_rate.numerator,
        plan.output_settings.frame_rate.denominator,
    )
    actual_rate = _rate(video.get("avg_frame_rate"))
    video_frame_count = _positive_int_text(
        video.get("nb_read_frames") or video.get("nb_frames")
    )
    has_audio = render_has_audio(plan)
    audio_contract = (
        len(audios) == 1 and audio.get("codec_name") == "aac"
        if has_audio
        else len(audios) == 0
    )
    audio_rate = _positive_int_text(audio.get("sample_rate")) if has_audio else None
    audio_channels = audio.get("channels") if has_audio else None
    audio_timeline_samples = (
        _timeline_sample_count(audio, audio_rate)
        if has_audio and audio_rate is not None
        else None
    )
    if has_audio and decoded_audio_samples is None:
        decoded_audio_samples = audio_timeline_samples
    tolerance = plan.output_settings.frame_rate.ticks_per_frame
    if has_audio:
        tolerance += _ceil_fraction(
            Fraction(1024 * TICKS_PER_SECOND, plan.output_settings.audio_sample_rate)
        )
    duration_ok = (
        duration_ticks is not None
        and abs(duration_ticks - plan.total_duration_ticks) <= tolerance
    )
    format_name = typed_format.get("format_name")
    checks = {
        "output_nonempty": output_size > 0,
        "container_mp4": isinstance(format_name, str) and "mp4" in format_name.split(","),
        "single_video_stream": len(videos) == 1,
        "video_codec_h264": video.get("codec_name") == "h264",
        "output_dimensions": (
            video.get("width") == plan.output_settings.width
            and video.get("height") == plan.output_settings.height
        ),
        "output_frame_rate": actual_rate == expected_rate,
        "output_frame_quota": video_frame_count == schedule.total_frames,
        "pixel_format_yuv420p": video.get("pix_fmt") == "yuv420p",
        "audio_stream_contract": audio_contract,
        "audio_sample_rate": (
            audio_rate == plan.output_settings.audio_sample_rate if has_audio else True
        ),
        "audio_channels_stereo": audio_channels == 2 if has_audio else True,
        "audio_sample_quota": (
            audio_timeline_samples is not None
            and audio_sample_quota_matches(
                codec=audio.get("codec_name"),
                expected=schedule.total_samples,
                actual=audio_timeline_samples,
            )
            if has_audio
            else True
        ),
        "decoded_audio_padding_within_aac_frame": (
            decoded_audio_samples is not None
            and audio_sample_quota_matches(
                codec=audio.get("codec_name"),
                expected=schedule.total_samples,
                actual=decoded_audio_samples,
            )
            if has_audio
            else True
        ),
        "decodes_from_start_to_end": decode_succeeded,
        "duration_within_tolerance": duration_ok,
        "faststart": faststart,
    }
    if isinstance(plan, MultiSourceRenderPlan):
        checks["starts_at_zero"] = start_ticks == 0
    sanitized_format: dict[str, object] = {
        "format_name": format_name if isinstance(format_name, str) else None,
        "duration_ticks": duration_ticks,
    }
    if isinstance(plan, MultiSourceRenderPlan):
        sanitized_format["start_ticks"] = start_ticks
    sanitized_probe: dict[str, object] = {
        "format": sanitized_format,
        "video": {
            "codec_name": video.get("codec_name"),
            "width": video.get("width"),
            "height": video.get("height"),
            "avg_frame_rate": video.get("avg_frame_rate"),
            "pix_fmt": video.get("pix_fmt"),
            "frame_count": video_frame_count,
        },
        "audio": (
            {
                "codec_name": audio.get("codec_name"),
                "sample_rate": audio_rate,
                "channels": audio_channels,
                "timeline_sample_count": audio_timeline_samples,
                "decoded_sample_count": decoded_audio_samples,
            }
            if has_audio
            else None
        ),
    }
    return VerificationReport(
        accepted=all(checks.values()),
        duration_ticks=duration_ticks,
        checks=checks,
        probe=sanitized_probe,
    )


def _duration_ticks(format_data: dict[str, Any], streams: list[dict[str, Any]]) -> int | None:
    values = [format_data.get("duration"), *[stream.get("duration") for stream in streams]]
    for value in values:
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            continue
        try:
            duration = Decimal(value)
        except InvalidOperation:
            continue
        if duration > 0:
            return int(
                (duration * TICKS_PER_SECOND).to_integral_value(rounding=ROUND_HALF_UP)
            )
    return None


def _start_ticks(format_data: dict[str, Any], streams: list[dict[str, Any]]) -> int | None:
    values = [format_data.get("start_time"), *[stream.get("start_time") for stream in streams]]
    parsed: list[Decimal] = []
    for value in values:
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            continue
        try:
            parsed.append(Decimal(value))
        except InvalidOperation:
            continue
    if not parsed:
        return None
    return int((min(parsed) * TICKS_PER_SECOND).to_integral_value(rounding=ROUND_HALF_UP))


def _rate(value: object) -> Fraction | None:
    if not isinstance(value, str) or "/" not in value:
        return None
    numerator, denominator = value.split("/", 1)
    try:
        rate = Fraction(int(numerator), int(denominator))
    except (ValueError, ZeroDivisionError):
        return None
    return rate if rate > 0 else None


def _positive_int_text(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _timeline_sample_count(audio: dict[str, Any], sample_rate: int) -> int | None:
    duration_ticks = audio.get("duration_ts")
    time_base = audio.get("time_base")
    if (
        isinstance(duration_ticks, bool)
        or not isinstance(duration_ticks, (str, int))
        or not isinstance(time_base, str)
        or "/" not in time_base
    ):
        return None
    numerator, denominator = time_base.split("/", 1)
    try:
        duration = int(duration_ticks)
        scaled_numerator = duration * int(numerator) * sample_rate
        scaled_denominator = int(denominator)
        if scaled_denominator <= 0:
            return None
        samples, remainder = divmod(scaled_numerator, scaled_denominator)
    except ValueError:
        return None
    return samples if samples > 0 and remainder == 0 else None


def _decoded_audio_sample_count(stderr: object) -> int | None:
    if not isinstance(stderr, str):
        return None
    matches = _AUDIO_SAMPLE_COUNT.findall(stderr)
    if not matches:
        return None
    samples = int(matches[-1])
    return samples if samples > 0 else None


def _ceil_fraction(value: Fraction) -> int:
    return -(-value.numerator // value.denominator)


def _has_faststart(path: Path) -> bool:
    try:
        size = path.stat().st_size
        with path.open("rb") as media:
            offset = 0
            while offset + 8 <= size:
                media.seek(offset)
                header = media.read(8)
                if len(header) != 8:
                    return False
                box_size = int.from_bytes(header[:4], "big")
                box_type = header[4:8]
                header_size = 8
                if box_size == 1:
                    extended = media.read(8)
                    if len(extended) != 8:
                        return False
                    box_size = int.from_bytes(extended, "big")
                    header_size = 16
                elif box_size == 0:
                    box_size = size - offset
                if box_size < header_size or offset + box_size > size:
                    return False
                if box_type == b"moov":
                    return True
                if box_type == b"mdat":
                    return False
                offset += box_size
    except OSError:
        return False
    return False
