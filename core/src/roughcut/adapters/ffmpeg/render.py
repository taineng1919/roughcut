"""FFmpeg filter-script and process boundary for one frozen render plan."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, cast

from roughcut.adapters.media_operation_store import media_child_process_kwargs
from roughcut.adapters.runtime_binding import (
    RuntimeBindingError,
    resolve_runtime_tool,
)
from roughcut.domain.render import (
    RenderPlan,
    RenderPlanLike,
    RenderSchedule,
    ToolResolution,
    derive_render_schedule,
    render_has_audio,
    source_for_render_clip,
)
from roughcut.domain.time import TICKS_PER_SECOND


class FFmpegRenderError(RuntimeError):
    """Raised when FFmpeg cannot produce a candidate render."""


class RenderCancelled(FFmpegRenderError):
    """Raised after a requested render cancellation has stopped FFmpeg."""


class ProcessLike(Protocol):
    returncode: int | None

    def communicate(self, timeout: float | None = None) -> tuple[str, str]: ...

    def terminate(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def kill(self) -> None: ...


ProcessFactory = Callable[..., ProcessLike]
CancelCheck = Callable[[], bool]


def _default_process_factory(command: list[str], **kwargs: Any) -> ProcessLike:
    return cast(ProcessLike, subprocess.Popen(command, **kwargs))


def resolve_render_tools(
    *, ffmpeg_command: str | None = None, ffprobe_command: str | None = None
) -> tuple[ToolResolution, ToolResolution]:
    try:
        selected_ffmpeg = resolve_runtime_tool(
            "ffmpeg", explicit_command=ffmpeg_command
        ).command
        selected_ffprobe = resolve_runtime_tool(
            "ffprobe", explicit_command=ffprobe_command
        ).command
    except RuntimeBindingError as error:
        raise FFmpegRenderError(str(error)) from error
    return (
        _resolve_tool(selected_ffmpeg, "ffmpeg version "),
        _resolve_tool(selected_ffprobe, "ffprobe version "),
    )


def build_filter_script(
    plan: RenderPlanLike,
    *,
    schedule: RenderSchedule | None = None,
) -> str:
    """Build the deterministic, path-free filter graph for a single source."""
    schedule = schedule or derive_render_schedule(plan)
    count = len(schedule.clips)
    settings = plan.output_settings
    rate = f"{settings.frame_rate.numerator}/{settings.frame_rate.denominator}"
    single_audio_only = (
        isinstance(plan, RenderPlan) and plan.source.probe.video_codec is None
    )
    has_audio = render_has_audio(plan)
    lines: list[str] = []

    if single_audio_only:
        lines.append(
            f"color=c=black:s={settings.width}x{settings.height}:r={rate},"
            f"trim=end_frame={schedule.total_frames},setpts=PTS-STARTPTS,"
            "setsar=1,format=yuv420p,setparams=range=limited[vout]"
        )

    for clip in schedule.clips:
        index = clip.input_index
        render_clip = plan.clips[index]
        source = source_for_render_clip(plan, render_clip)
        clip_has_video = source.probe.video_codec is not None
        clip_has_audio = source.probe.audio_codec is not None
        if clip_has_video:
            rotation = _rotation_filters(source.probe.rotation_degrees)
            video_filters = [
                "setpts=PTS-STARTPTS",
                *rotation,
                (
                    f"scale={settings.width}:{settings.height}:"
                    "force_original_aspect_ratio=decrease:in_range=auto:out_range=tv"
                ),
                (
                    f"pad={settings.width}:{settings.height}:"
                    "(ow-iw)/2:(oh-ih)/2:color=black"
                ),
                f"fps=fps={rate}:start_time=0:round=near:eof_action=pass",
                f"trim=end_frame={clip.frame_count}",
                "setpts=PTS-STARTPTS",
                "setsar=1",
                "format=yuv420p",
                "setparams=range=limited",
            ]
            lines.append(f"[{index}:v:0]{','.join(video_filters)}[v{index}]")
        elif not single_audio_only:
            lines.append(
                f"color=c=black:s={settings.width}x{settings.height}:r={rate},"
                f"trim=end_frame={clip.frame_count},setpts=PTS-STARTPTS,"
                f"setsar=1,format=yuv420p,setparams=range=limited[v{index}]"
            )
        if has_audio:
            if clip_has_audio:
                audio_filters = [
                    "asetpts=PTS-STARTPTS",
                    f"aresample={settings.audio_sample_rate}:first_pts=0",
                    (
                        "aformat=sample_fmts=fltp:"
                        f"sample_rates={settings.audio_sample_rate}:channel_layouts=stereo"
                    ),
                    f"atrim=end_sample={clip.sample_count}",
                    "asetpts=PTS-STARTPTS",
                ]
                lines.append(f"[{index}:a:0]{','.join(audio_filters)}[a{index}]")
            else:
                lines.append(
                    f"anullsrc=r={settings.audio_sample_rate}:cl=stereo,"
                    f"atrim=end_sample={clip.sample_count},asetpts=PTS-STARTPTS[a{index}]"
                )

    if not single_audio_only:
        inputs = "".join(f"[v{index}]" for index in range(count))
        lines.append(f"{inputs}concat=n={count}:v=1:a=0[vout]")
    if has_audio:
        inputs = "".join(f"[a{index}]" for index in range(count))
        lines.append(f"{inputs}concat=n={count}:v=0:a=1[aout]")
    return ";\n".join(lines) + "\n"


def build_ffmpeg_command(
    plan: RenderPlanLike,
    *,
    schedule: RenderSchedule | None = None,
    source_path: Path | None = None,
    source_paths: dict[str, Path] | None = None,
    filter_script_path: Path,
    output_path: Path,
) -> list[str]:
    schedule = schedule or derive_render_schedule(plan)
    settings = plan.output_settings
    rate = f"{settings.frame_rate.numerator}/{settings.frame_rate.denominator}"
    command = [
        plan.ffmpeg.resolved_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
    ]
    for clip in schedule.clips:
        render_clip = plan.clips[clip.input_index]
        source = source_for_render_clip(plan, render_clip)
        selected_path = (
            source_paths.get(source.source_id) if source_paths is not None else source_path
        )
        if selected_path is None:
            raise FFmpegRenderError("render source path is missing")
        command.extend(
            [
                "-ss",
                ticks_to_seconds_text(clip.access_start_ticks),
                "-t",
                ticks_to_seconds_text(clip.access_duration_ticks),
            ]
        )
        if source.probe.video_codec is not None:
            command.append("-noautorotate")
        command.extend(["-i", str(selected_path)])
    command.extend(["-/filter_complex", str(filter_script_path), "-map", "[vout]"])
    if render_has_audio(plan):
        command.extend(["-map", "[aout]"])
    command.extend(
        [
            "-map_metadata",
            "-1",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-color_range",
            "tv",
            "-r",
            rate,
            "-fps_mode",
            "cfr",
            "-frames:v",
            str(schedule.total_frames),
        ]
    )
    if render_has_audio(plan):
        command.extend(
            [
                "-c:a",
                "aac",
                "-ar",
                str(settings.audio_sample_rate),
                "-ac",
                "2",
            ]
        )
    command.extend(["-movflags", "+faststart", "-f", "mp4", "-y", str(output_path)])
    return command


def run_ffmpeg(
    command: list[str],
    *,
    cancel_requested: CancelCheck | None = None,
    process_factory: ProcessFactory = _default_process_factory,
) -> None:
    try:
        process = process_factory(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **media_child_process_kwargs(),
        )
    except OSError as error:
        raise FFmpegRenderError("FFmpeg process could not start") from error
    try:
        while True:
            try:
                _stdout, stderr = process.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                if cancel_requested is not None and cancel_requested():
                    _stop_process(process)
                    raise RenderCancelled("render was cancelled")
    except KeyboardInterrupt as error:
        _stop_process(process)
        raise RenderCancelled("render was interrupted") from error
    if process.returncode != 0:
        detail = stderr.strip().splitlines()
        message = detail[-1] if detail else "FFmpeg returned a non-zero exit status"
        raise FFmpegRenderError(message)


def ticks_to_seconds_text(ticks: int) -> str:
    sign = "-" if ticks < 0 else ""
    absolute = abs(ticks)
    whole, remainder = divmod(absolute, TICKS_PER_SECOND)
    if remainder == 0:
        return f"{sign}{whole}"
    decimal = Decimal(remainder) / Decimal(TICKS_PER_SECOND)
    fractional = format(decimal, ".12f").split(".", 1)[1].rstrip("0")
    return f"{sign}{whole}.{fractional}"


def _rotation_filters(rotation_degrees: int) -> list[str]:
    normalized = rotation_degrees % 360
    if normalized == 0:
        return []
    if normalized == 90:
        return ["transpose=clock"]
    if normalized == 180:
        return ["hflip", "vflip"]
    if normalized == 270:
        return ["transpose=cclock"]
    raise FFmpegRenderError("source rotation is not a right angle")


def _stop_process(process: ProcessLike) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _resolve_tool(command: str, version_prefix: str) -> ToolResolution:
    requested = Path(command)
    if requested.parent != Path("."):
        if not requested.is_file() or not os.access(requested, os.X_OK):
            raise FFmpegRenderError(f"{command} is missing or not executable")
        resolved = str(requested.resolve())
    else:
        found = shutil.which(command)
        if found is None:
            raise FFmpegRenderError(f"{command} is missing or not executable")
        resolved = found
    try:
        result = subprocess.run(
            [resolved, "-version"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise FFmpegRenderError(f"{command} version could not be read") from error
    lines = (result.stdout or result.stderr).strip().splitlines()
    if result.returncode != 0 or not lines or not lines[0].startswith(version_prefix):
        raise FFmpegRenderError(f"{command} version could not be read")
    return ToolResolution(command=command, resolved_path=resolved, version=lines[0])
