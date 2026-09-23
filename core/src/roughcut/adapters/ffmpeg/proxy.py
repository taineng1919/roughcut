"""Deterministic software FFmpeg proxy generation and verification."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Any

from roughcut.adapters.ffmpeg.render import (
    FFmpegRenderError,
    _rotation_filters,
    resolve_render_tools,
)
from roughcut.adapters.ffmpeg.verify import _has_faststart
from roughcut.adapters.media_operation_store import media_child_process_kwargs
from roughcut.domain.project import MediaProbe
from roughcut.domain.proxy import PROXY_AUDIO_SAMPLE_RATE, ProxyProfile
from roughcut.domain.render import ToolResolution
from roughcut.domain.time import TICKS_PER_SECOND


class FFmpegProxyError(RuntimeError):
    """Raised when a proxy process or output fails."""


class ProxyCancelled(FFmpegProxyError):
    """Raised when proxy generation is interrupted."""


class ProxyUnsupported(FFmpegProxyError):
    """Raised for deliberately unsupported source media."""


@dataclass(frozen=True)
class ProxyVerificationReport:
    duration_ticks: int
    checks: dict[str, bool]
    probe: dict[str, object]
    padded_video_frames: int
    padded_audio_samples: int
    leading_video_frames: int = 0
    leading_audio_samples: int = 0

    @property
    def accepted(self) -> bool:
        return bool(self.checks) and all(self.checks.values())


@dataclass(frozen=True)
class ProxySourceInspection:
    video_start_ticks: int | None
    video_duration_ticks: int | None
    audio_start_ticks: int | None
    audio_duration_ticks: int | None
    origin_ticks: int


ProcessRunner = Callable[..., subprocess.CompletedProcess[Any]]
_HDR_TRANSFERS = {"smpte2084", "arib-std-b67", "pq", "hlg"}
_QUICK_PROBE_TIMEOUT_SECONDS = 60
_FULL_VERIFY_TIMEOUT_SECONDS = 3600


def resolve_proxy_tools() -> tuple[ToolResolution, ToolResolution]:
    try:
        return resolve_render_tools()
    except FFmpegRenderError as error:
        raise FFmpegProxyError(str(error)) from error


def resolve_proxy_ffprobe() -> ToolResolution:
    try:
        _ffmpeg, ffprobe = resolve_render_tools()
        return ffprobe
    except FFmpegRenderError as error:
        raise FFmpegProxyError(str(error)) from error


def reject_unsupported_color(
    source_path: Path,
    ffprobe: ToolResolution,
    *,
    fallback_origin_ticks: int = 0,
    process_runner: ProcessRunner = subprocess.run,
) -> ProxySourceInspection:
    try:
        result = process_runner(
            [
                ffprobe.resolved_path,
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,start_time,duration,color_transfer,color_primaries,color_space",
                "-of",
                "json",
                str(source_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            **media_child_process_kwargs(),
        )
        data: Any = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        raise FFmpegProxyError("proxy source color metadata could not be read") from error
    if result.returncode != 0 or not isinstance(data, dict):
        raise FFmpegProxyError("proxy source color metadata could not be read")
    streams = data.get("streams", [])
    if not isinstance(streams, list):
        raise FFmpegProxyError("proxy source color metadata is invalid")
    typed_streams = [stream for stream in streams if isinstance(stream, dict)]
    for stream in typed_streams:
        transfer = stream.get("color_transfer")
        if isinstance(transfer, str) and transfer.lower() in _HDR_TRANSFERS:
            raise ProxyUnsupported("HDR/PQ/HLG proxy generation is not supported")
    video = next(
        (stream for stream in typed_streams if stream.get("codec_type") == "video"),
        None,
    )
    audio = next(
        (stream for stream in typed_streams if stream.get("codec_type") == "audio"),
        None,
    )
    video_start = _seconds_value_to_ticks(video.get("start_time")) if video is not None else None
    audio_start = _seconds_value_to_ticks(audio.get("start_time")) if audio is not None else None
    starts = [value for value in (video_start, audio_start) if value is not None]
    return ProxySourceInspection(
        video_start_ticks=video_start,
        video_duration_ticks=(
            _seconds_value_to_ticks(video.get("duration")) if video is not None else None
        ),
        audio_start_ticks=audio_start,
        audio_duration_ticks=(
            _seconds_value_to_ticks(audio.get("duration")) if audio is not None else None
        ),
        origin_ticks=min(starts) if starts else fallback_origin_ticks,
    )


def proxy_quotas(duration_ticks: int, profile: ProxyProfile) -> tuple[int, int | None]:
    frames = _ceil_fraction(
        Fraction(
            duration_ticks * profile.frame_rate.numerator,
            TICKS_PER_SECOND * profile.frame_rate.denominator,
        )
    )
    samples = (
        _ceil_fraction(Fraction(duration_ticks * PROXY_AUDIO_SAMPLE_RATE, TICKS_PER_SECOND))
        if profile.has_audio
        else None
    )
    return frames, samples


def build_proxy_command(
    source_path: Path,
    output_path: Path,
    probe: MediaProbe,
    profile: ProxyProfile,
    ffmpeg: ToolResolution,
    *,
    source_inspection: ProxySourceInspection | None = None,
) -> list[str]:
    frames, samples = proxy_quotas(probe.duration_ticks, profile)
    leading_video_frames, leading_audio_samples = _leading_padding(profile, source_inspection)
    rate = f"{profile.frame_rate.numerator}/{profile.frame_rate.denominator}"
    video_output = "[vout]"
    filters: list[str] = []
    if profile.has_video:
        try:
            rotation = _rotation_filters(probe.rotation_degrees)
        except FFmpegRenderError as error:
            raise FFmpegProxyError("proxy source rotation is unsupported") from error
        video_filters = [
            "setpts=PTS-STARTPTS",
            *rotation,
            (
                f"scale=w='min(iw,{profile.canvas_width})':"
                f"h='min(ih,{profile.canvas_height})':force_original_aspect_ratio=decrease"
            ),
            (f"pad={profile.canvas_width}:{profile.canvas_height}:(ow-iw)/2:(oh-ih)/2:color=black"),
            f"fps=fps={rate}:start_time=0:round=near:eof_action=pass",
        ]
        if leading_video_frames:
            video_filters.append(f"tpad=start={leading_video_frames}:start_mode=add:color=black")
        video_filters.extend(
            [
                f"tpad=stop_mode=clone:stop_duration={_seconds_text(probe.duration_ticks)}",
                f"trim=end_frame={frames}",
                "setpts=PTS-STARTPTS",
                "setsar=1",
                "format=yuv420p",
            ]
        )
        filters.append(f"[0:v:0]{','.join(video_filters)}{video_output}")
    else:
        filters.append(
            f"color=c=black:s={profile.canvas_width}x{profile.canvas_height}:r={rate},"
            f"trim=end_frame={frames},setpts=PTS-STARTPTS,setsar=1,format=yuv420p{video_output}"
        )
    if profile.has_audio:
        assert samples is not None
        audio_delay = f"adelay={leading_audio_samples}S:all=1," if leading_audio_samples else ""
        filters.append(
            "[0:a:0]asetpts=PTS-STARTPTS,"
            f"aresample={PROXY_AUDIO_SAMPLE_RATE}:first_pts=0,"
            f"aformat=sample_fmts=fltp:sample_rates={PROXY_AUDIO_SAMPLE_RATE}:"
            f"channel_layouts=stereo,{audio_delay}apad,"
            f"atrim=end_sample={samples},asetpts=PTS-STARTPTS[aout]"
        )
    command = [
        ffmpeg.resolved_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
    ]
    if profile.has_video:
        command.append("-noautorotate")
    command.extend(
        ["-i", str(source_path), "-filter_complex", ";".join(filters), "-map", video_output]
    )
    if profile.has_audio:
        command.extend(["-map", "[aout]"])
    command.extend(
        [
            "-map_metadata",
            "-1",
            "-c:v",
            profile.video_codec,
            "-preset",
            profile.preset,
            "-crf",
            str(profile.crf),
            "-pix_fmt",
            profile.pixel_format,
            "-r",
            rate,
            "-fps_mode",
            "cfr",
            "-g",
            str(profile.gop_frames),
            "-keyint_min",
            str(profile.gop_frames),
            "-frames:v",
            str(frames),
            "-metadata:s:v:0",
            "rotate=0",
        ]
    )
    if profile.has_audio:
        command.extend(["-c:a", "aac", "-profile:a", "aac_low", "-ar", "48000", "-ac", "2"])
    command.extend(["-movflags", "+faststart", "-f", "mp4", "-y", str(output_path)])
    return command


def transcode_proxy(
    source_path: Path,
    *,
    output_path: Path,
    probe: MediaProbe,
    profile: ProxyProfile,
    ffmpeg: ToolResolution,
    source_inspection: ProxySourceInspection | None = None,
    timeout_seconds: float = 14_400,
    process_runner: ProcessRunner = subprocess.run,
) -> None:
    command = build_proxy_command(
        source_path,
        output_path,
        probe,
        profile,
        ffmpeg,
        source_inspection=source_inspection,
    )
    try:
        result = process_runner(
            command,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            **media_child_process_kwargs(),
        )
    except KeyboardInterrupt as error:
        raise ProxyCancelled("proxy generation was interrupted") from error
    except subprocess.TimeoutExpired as error:
        raise FFmpegProxyError("proxy generation timed out") from error
    except OSError as error:
        raise FFmpegProxyError("FFmpeg proxy process could not start") from error
    if result.returncode != 0:
        raise FFmpegProxyError("FFmpeg proxy generation failed")


def verify_proxy_output(
    output_path: Path,
    probe: MediaProbe,
    profile: ProxyProfile,
    ffprobe: ToolResolution,
    *,
    ffmpeg: ToolResolution | None = None,
    source_inspection: ProxySourceInspection | None = None,
    decode: bool = False,
    process_runner: ProcessRunner = subprocess.run,
) -> ProxyVerificationReport:
    probe_command = [
        ffprobe.resolved_path,
        "-v",
        "error",
        "-show_format",
        "-show_streams",
    ]
    if decode:
        probe_command.append("-count_frames")
    probe_command.extend(["-of", "json", str(output_path)])
    probe_timeout = (
        _FULL_VERIFY_TIMEOUT_SECONDS if decode else _QUICK_PROBE_TIMEOUT_SECONDS
    )
    try:
        result = process_runner(
            probe_command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=probe_timeout,
            **media_child_process_kwargs(),
        )
        data: Any = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        raise FFmpegProxyError("proxy output could not be probed") from error
    if result.returncode != 0 or not isinstance(data, dict):
        raise FFmpegProxyError("proxy output could not be probed")
    raw_format = data.get("format")
    raw_streams = data.get("streams")
    format_data: dict[str, Any] = raw_format if isinstance(raw_format, dict) else {}
    streams: list[Any] = raw_streams if isinstance(raw_streams, list) else []
    typed: list[dict[str, Any]] = [item for item in streams if isinstance(item, dict)]
    videos = [item for item in typed if item.get("codec_type") == "video"]
    audios = [item for item in typed if item.get("codec_type") == "audio"]
    video = videos[0] if len(videos) == 1 else {}
    audio = audios[0] if len(audios) == 1 else {}
    duration_ticks = _duration_ticks(format_data, typed)
    frames, samples = proxy_quotas(probe.duration_ticks, profile)
    leading_video_frames, leading_audio_samples = _leading_padding(profile, source_inspection)
    actual_frames = _positive_integer(video.get("nb_read_frames") or video.get("nb_frames"))
    rate = _rate(video.get("avg_frame_rate"))
    expected_rate = Fraction(profile.frame_rate.numerator, profile.frame_rate.denominator)
    start_ticks = _seconds_value_to_ticks(video.get("start_time"))
    audio_start_ticks = _seconds_value_to_ticks(audio.get("start_time"))
    rotation = _rotation(video)
    checks = {
        "output_nonempty": output_path.is_file() and output_path.stat().st_size > 0,
        "container_mp4": isinstance(format_data.get("format_name"), str)
        and "mp4" in str(format_data.get("format_name")).split(","),
        "single_video_stream": len(videos) == 1,
        "video_codec_h264": video.get("codec_name") == "h264",
        "pixel_format_yuv420p": video.get("pix_fmt") == "yuv420p",
        "canvas_dimensions": video.get("width") == profile.canvas_width
        and video.get("height") == profile.canvas_height,
        "constant_frame_rate": rate == expected_rate,
        "starts_at_zero": start_ticks is not None
        and abs(start_ticks) <= profile.frame_rate.ticks_per_frame,
        "rotation_baked": rotation == 0,
        "audio_contract": (
            len(audios) == 1
            and audio.get("codec_name") == "aac"
            and _positive_integer(audio.get("sample_rate")) == PROXY_AUDIO_SAMPLE_RATE
            if profile.has_audio
            else len(audios) == 0
        ),
        "audio_starts_at_zero": (
            audio_start_ticks is not None and abs(audio_start_ticks) <= 2560
            if profile.has_audio
            else True
        ),
        "duration_within_tolerance": duration_ticks is not None
        and abs(duration_ticks - probe.duration_ticks)
        <= profile.frame_rate.ticks_per_frame + (2560 if profile.has_audio else 0),
        "faststart": _has_faststart(output_path),
    }
    if decode:
        checks["frame_quota"] = actual_frames == frames
    if decode:
        if ffmpeg is None:
            raise FFmpegProxyError("FFmpeg is required for proxy decode verification")
        try:
            decoded = process_runner(
                [
                    ffmpeg.resolved_path,
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
                timeout=_FULL_VERIFY_TIMEOUT_SECONDS,
                **media_child_process_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise FFmpegProxyError("proxy output decode verification failed") from error
        checks["decodes_from_start_to_end"] = decoded.returncode == 0
    report = ProxyVerificationReport(
        duration_ticks=duration_ticks or 0,
        checks=checks,
        probe={
            "format_name": format_data.get("format_name"),
            "duration_ticks": duration_ticks,
            "video": {
                "codec_name": video.get("codec_name"),
                "width": video.get("width"),
                "height": video.get("height"),
                "pix_fmt": video.get("pix_fmt"),
                "avg_frame_rate": video.get("avg_frame_rate"),
                "frame_count": actual_frames,
                "start_ticks": start_ticks,
                "rotation_degrees": rotation,
            },
            "audio": (
                {
                    "codec_name": audio.get("codec_name"),
                    "sample_rate": _positive_integer(audio.get("sample_rate")),
                    "start_ticks": audio_start_ticks,
                }
                if profile.has_audio
                else None
            ),
        },
        padded_video_frames=_video_padding_frames(
            frames,
            profile,
            source_inspection.video_duration_ticks if source_inspection is not None else None,
            leading_video_frames,
        ),
        padded_audio_samples=(
            _audio_padding_samples(
                samples,
                source_inspection.audio_duration_ticks if source_inspection is not None else None,
                leading_audio_samples,
            )
            if samples is not None
            else 0
        ),
        leading_video_frames=leading_video_frames,
        leading_audio_samples=leading_audio_samples,
    )
    if not report.accepted:
        raise FFmpegProxyError("proxy output verification failed")
    return report


def _seconds_text(ticks: int) -> str:
    return format(Decimal(ticks) / Decimal(TICKS_PER_SECOND), "f")


def _duration_ticks(format_data: dict[str, Any], streams: list[dict[str, Any]]) -> int | None:
    for value in [format_data.get("duration"), *[stream.get("duration") for stream in streams]]:
        try:
            parsed = (
                Decimal(value)
                if isinstance(value, (str, int)) and not isinstance(value, bool)
                else None
            )
        except InvalidOperation:
            parsed = None
        if parsed is not None and parsed > 0:
            return int((parsed * TICKS_PER_SECOND).to_integral_value(rounding=ROUND_HALF_UP))
    return None


def _seconds_value_to_ticks(value: object) -> int | None:
    try:
        parsed = (
            Decimal(value)
            if isinstance(value, (str, int)) and not isinstance(value, bool)
            else None
        )
    except InvalidOperation:
        return None
    return (
        int((parsed * TICKS_PER_SECOND).to_integral_value(rounding=ROUND_HALF_UP))
        if parsed is not None
        else None
    )


def _positive_integer(value: object) -> int | None:
    try:
        parsed = int(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else 0
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _rate(value: object) -> Fraction | None:
    if not isinstance(value, str) or "/" not in value:
        return None
    try:
        numerator, denominator = value.split("/", 1)
        return Fraction(int(numerator), int(denominator))
    except (ValueError, ZeroDivisionError):
        return None


def _rotation(video: dict[str, Any]) -> int:
    side_data = video.get("side_data_list")
    if isinstance(side_data, list):
        for item in side_data:
            if isinstance(item, dict) and isinstance(item.get("rotation"), int):
                return int(item["rotation"])
    tags = video.get("tags")
    if isinstance(tags, dict):
        try:
            return int(tags.get("rotate", 0))
        except (TypeError, ValueError):
            return 0
    return 0


def _ceil_fraction(value: Fraction) -> int:
    return -(-value.numerator // value.denominator)


def _video_padding_frames(
    output_frames: int,
    profile: ProxyProfile,
    source_duration_ticks: int | None,
    leading_frames: int,
) -> int:
    if source_duration_ticks is None:
        return 0
    source_frames = _ceil_fraction(
        Fraction(
            source_duration_ticks * profile.frame_rate.numerator,
            TICKS_PER_SECOND * profile.frame_rate.denominator,
        )
    )
    return max(0, output_frames - leading_frames - source_frames)


def _audio_padding_samples(
    output_samples: int,
    source_duration_ticks: int | None,
    leading_samples: int,
) -> int:
    if source_duration_ticks is None:
        return 0
    source_samples = _ceil_fraction(
        Fraction(source_duration_ticks * PROXY_AUDIO_SAMPLE_RATE, TICKS_PER_SECOND)
    )
    return max(0, output_samples - leading_samples - source_samples)


def _leading_padding(
    profile: ProxyProfile,
    inspection: ProxySourceInspection | None,
) -> tuple[int, int]:
    if inspection is None:
        return 0, 0
    video_start = (
        inspection.origin_ticks
        if inspection.video_start_ticks is None
        else inspection.video_start_ticks
    )
    audio_start = (
        inspection.origin_ticks
        if inspection.audio_start_ticks is None
        else inspection.audio_start_ticks
    )
    video_ticks = max(0, video_start - inspection.origin_ticks)
    audio_ticks = max(0, audio_start - inspection.origin_ticks)
    video_frames = _round_fraction(
        Fraction(
            video_ticks * profile.frame_rate.numerator,
            TICKS_PER_SECOND * profile.frame_rate.denominator,
        )
    )
    audio_samples = _round_fraction(
        Fraction(audio_ticks * PROXY_AUDIO_SAMPLE_RATE, TICKS_PER_SECOND)
    )
    return video_frames if profile.has_video else 0, audio_samples if profile.has_audio else 0


def _round_fraction(value: Fraction) -> int:
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)
