"""FFmpeg adapter for one verified parallel multicam camera output."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, cast

from roughcut.adapters.ffmpeg.audio_quota import (
    AAC_SAMPLE_QUOTA_TOLERANCE,
    audio_sample_quota_matches,
)
from roughcut.adapters.ffmpeg.render import _rotation_filters, ticks_to_seconds_text
from roughcut.adapters.media_operation_store import media_child_process_kwargs
from roughcut.domain.media_operation import (
    PARALLEL_STDERR_TAIL_MAX_BYTES,
    PARALLEL_STDERR_TAIL_MAX_LINES,
)
from roughcut.domain.project import SourceAsset
from roughcut.domain.render import ToolResolution


class ParallelCameraEncodeError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        return_code: int | None = None,
        stderr: str | bytes | None = None,
    ) -> None:
        super().__init__(message)
        self.return_code = return_code
        self.stderr = stderr


class ParallelCameraStagingError(RuntimeError):
    pass


class ParallelCameraVerifyError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        check: str = "verify_probe",
        expected: int | str | None = None,
        actual: int | str | None = None,
        tolerance: int | None = None,
    ) -> None:
        super().__init__(message)
        self.check = check
        self.expected = expected
        self.actual = actual
        self.tolerance = tolerance


@dataclass(frozen=True)
class ParallelCameraVerification:
    bytes: int
    content_hash: str
    frame_count: int
    audio_samples: int
    accepted: bool


_SAMPLES_RE = re.compile(r"Number of samples:\s*(\d+)\s*$", re.MULTILINE)
_SECRET_KEY = (
    r"(?:[A-Za-z0-9]+[_-])*?"
    r"(?:token|secret(?:[_-]?key)?|password|passwd|api[_-]?key|"
    r"access[_-]?key|authorization|key)"
)
_SECRET_RE = re.compile(
    rf"(?i)(\b{_SECRET_KEY}\b\s*[:=]\s*)(?:(?:Bearer)\s+)?[^\s,;]+"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_POSIX_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_])/(?:[^/\n]+/|[^/\n]+$)"
)
_DRIVE_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]")
_UNC_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])\\\\[^\n]+")
_CONTROL_TRANSLATION = {
    code: "\ufffd"
    for code in range(32)
    if code not in {9, 10}
}
_CONTROL_TRANSLATION[127] = "\ufffd"


def _redact_diagnostic_line(line: str) -> str:
    if (
        _POSIX_PATH_RE.search(line) is not None
        or _DRIVE_PATH_RE.search(line) is not None
        or _UNC_PATH_RE.search(line) is not None
    ):
        return "<redacted-path>"
    return _BEARER_RE.sub(
        "<redacted>", _SECRET_RE.sub(r"\1<redacted>", line)
    )


def bounded_stderr_tail(value: str | bytes | None) -> str:
    """Return a deterministic, redacted, bounded diagnostic tail."""
    if value is None:
        value = ""
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = value.encode("utf-8", errors="replace").decode("utf-8")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.translate(_CONTROL_TRANSLATION)
    lines = [_redact_diagnostic_line(line) for line in text.splitlines()]
    text = "\n".join(lines[-PARALLEL_STDERR_TAIL_MAX_LINES:])
    encoded = text.encode("utf-8")
    if len(encoded) > PARALLEL_STDERR_TAIL_MAX_BYTES:
        encoded = encoded[-PARALLEL_STDERR_TAIL_MAX_BYTES:]
        for _ in range(3):
            if encoded and 0x80 <= encoded[0] <= 0xBF:
                encoded = encoded[1:]
            else:
                break
        text = encoded.decode("utf-8")
    return text


def _safe_probe_text(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, str) or not value:
        return "missing"
    text = bounded_stderr_tail(value)
    return text if len(text) <= 256 else "<redacted>"


def render_parallel_camera(
    camera: dict[str, object],
    *,
    source_paths: dict[str, Path],
    sources: dict[str, SourceAsset],
    output_path: Path,
    settings: dict[str, object],
    ffmpeg: ToolResolution,
) -> None:
    """Encode exactly one camera plan into operation-owned staging."""
    slots = cast(list[dict[str, object]], camera["slots"])
    total_frames = cast(int, slots[-1]["video_frame_end"])
    total_samples = cast(int, slots[-1]["audio_sample_end"])
    filter_script = output_path.with_name(f".{output_path.name}.filter.txt")
    command = _build_command(
        slots,
        source_paths=source_paths,
        sources=sources,
        output_path=output_path,
        settings=settings,
        ffmpeg=ffmpeg,
        total_frames=total_frames,
        total_samples=total_samples,
        filter_script=filter_script,
    )
    primary_error: BaseException | None = None
    try:
        try:
            filter_script.write_text(
                _build_filter_script(
                    slots,
                    sources,
                    settings,
                    total_samples=total_samples,
                ),
                encoding="utf-8",
            )
        except OSError as error:
            raise ParallelCameraStagingError("FFmpeg filter-script write failed") from error
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **cast(dict[str, Any], media_child_process_kwargs()),
        )
        try:
            _stdout, stderr = process.communicate()
        except BaseException as error:
            primary_error = error
            deferred_hard_exit, control_error = _terminate_child(process)
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise error from None
            if deferred_hard_exit is not None:
                raise deferred_hard_exit from None
            if control_error is not None:
                raise ParallelCameraStagingError(
                    "FFmpeg child process control failed after confirmed reap"
                ) from control_error
            raise
        if process.returncode != 0:
            raise ParallelCameraEncodeError(
                "FFmpeg camera encode failed",
                return_code=process.returncode,
                stderr=bounded_stderr_tail(stderr),
            )
    except ParallelCameraStagingError as error:
        primary_error = error
        raise
    except ParallelCameraEncodeError as error:
        primary_error = error
        raise
    except (OSError, subprocess.SubprocessError) as error:
        primary_error = error
        raise ParallelCameraEncodeError("FFmpeg camera encode failed") from error
    except BaseException as error:
        primary_error = error
        raise
    finally:
        try:
            filter_script.unlink(missing_ok=True)
        except OSError as error:
            if primary_error is None:
                raise ParallelCameraStagingError(
                    "FFmpeg filter-script cleanup failed"
                ) from error


def verify_parallel_camera(
    camera: dict[str, object],
    *,
    output_path: Path,
    settings: dict[str, object],
    ffmpeg: ToolResolution,
    ffprobe: ToolResolution,
) -> ParallelCameraVerification:
    slots = cast(list[dict[str, object]], camera["slots"])
    expected_frames = cast(int, slots[-1]["video_frame_end"])
    expected_samples = cast(int, slots[-1]["audio_sample_end"])
    if not output_path.is_file() or output_path.stat().st_size <= 0:
        raise ParallelCameraVerifyError(
            "parallel camera output is empty",
            check="output_presence",
            expected="non_empty_file",
            actual="missing_or_empty",
        )
    try:
        probe = subprocess.run(
            [
                ffprobe.resolved_path,
                "-v", "error", "-count_frames", "-show_format", "-show_streams", "-of", "json", str(output_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3600,
            **cast(dict[str, Any], media_child_process_kwargs()),
        )
        data = json.loads(probe.stdout)
        decoded = subprocess.run(
            [ffmpeg.resolved_path, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(output_path), "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3600,
            **cast(dict[str, Any], media_child_process_kwargs()),
        )
        audio_probe = subprocess.run(
            [ffmpeg.resolved_path, "-hide_banner", "-loglevel", "info", "-nostats", "-nostdin", "-i", str(output_path), "-map", "0:a:0", "-af", "astats=metadata=0:reset=0", "-f", "null", "-"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3600,
            **cast(dict[str, Any], media_child_process_kwargs()),
        )
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        raise ParallelCameraVerifyError(
            "parallel camera output probe failed",
            check="verify_probe",
            expected="probe_success",
            actual="probe_exception",
        ) from error
    streams = data.get("streams") if isinstance(data, dict) else None
    if probe.returncode != 0 or not isinstance(streams, list):
        raise ParallelCameraVerifyError(
            "parallel camera output probe failed",
            check="verify_probe",
            expected="probe_success",
            actual="probe_failed",
        )
    videos = [item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"]
    audios = [item for item in streams if isinstance(item, dict) and item.get("codec_type") == "audio"]
    if len(videos) != 1 or len(audios) != 1:
        raise ParallelCameraVerifyError(
            "parallel camera output stream verification failed",
            check="video_stream",
            expected="one_video_one_audio",
            actual=f"{len(videos)}_video_{len(audios)}_audio",
        )
    if decoded.returncode != 0 or audio_probe.returncode != 0:
        raise ParallelCameraVerifyError(
            "parallel camera output stream verification failed",
            check="verify_probe",
            expected="decode_success",
            actual="decode_failed",
        )
    video = videos[0]
    audio = audios[0]
    actual_frames = _integer_text(video.get("nb_read_frames") or video.get("nb_frames"))
    timeline_samples = _timeline_samples(audio, cast(int, settings["audio_sample_rate"]))
    decoded_samples = _audio_samples(audio_probe.stderr)
    frame_rate = cast(dict[str, object], settings["frame_rate"])
    expected_rate = Fraction(cast(int, frame_rate["numerator"]), cast(int, frame_rate["denominator"]))
    actual_rate = _rate(video.get("avg_frame_rate"))
    if video.get("codec_name") != "h264":
        raise ParallelCameraVerifyError(
            "parallel camera video codec verification failed",
            check="video_codec",
            expected="h264",
            actual=_safe_probe_text(video.get("codec_name")),
        )
    expected_dimensions = f"{settings['width']}x{settings['height']}"
    actual_dimensions = f"{_safe_probe_text(video.get('width'))}x{_safe_probe_text(video.get('height'))}"
    if (
        video.get("width") != settings["width"]
        or video.get("height") != settings["height"]
    ):
        raise ParallelCameraVerifyError(
            "parallel camera video dimensions verification failed",
            check="video_dimensions",
            expected=expected_dimensions,
            actual=actual_dimensions,
        )
    if video.get("pix_fmt") != "yuv420p":
        raise ParallelCameraVerifyError(
            "parallel camera pixel format verification failed",
            check="video_pixel_format",
            expected="yuv420p",
            actual=_safe_probe_text(video.get("pix_fmt")),
        )
    if actual_rate != expected_rate:
        raise ParallelCameraVerifyError(
            "parallel camera frame rate verification failed",
            check="video_frame_rate",
            expected=f"{expected_rate.numerator}/{expected_rate.denominator}",
            actual=_safe_probe_text(video.get("avg_frame_rate")),
        )
    if actual_frames != expected_frames:
        raise ParallelCameraVerifyError(
            "parallel camera frame quota verification failed",
            check="video_frame_quota",
            expected=expected_frames,
            actual=actual_frames,
            tolerance=0,
        )
    if audio.get("codec_name") != "aac":
        raise ParallelCameraVerifyError(
            "parallel camera audio codec verification failed",
            check="audio_codec",
            expected="aac",
            actual=_safe_probe_text(audio.get("codec_name")),
        )
    expected_sample_rate = str(cast(int, settings["audio_sample_rate"]))
    if audio.get("sample_rate") != expected_sample_rate:
        raise ParallelCameraVerifyError(
            "parallel camera audio sample rate verification failed",
            check="audio_sample_rate",
            expected=expected_sample_rate,
            actual=_safe_probe_text(audio.get("sample_rate")),
        )
    if audio.get("channels") != 2:
        raise ParallelCameraVerifyError(
            "parallel camera audio channel verification failed",
            check="audio_channels",
            expected=2,
            actual=audio.get("channels")
            if isinstance(audio.get("channels"), int)
            else _safe_probe_text(audio.get("channels")),
        )
    if timeline_samples is None or not audio_sample_quota_matches(
        codec=audio.get("codec_name"),
        expected=expected_samples,
        actual=timeline_samples,
    ):
        raise ParallelCameraVerifyError(
            "parallel camera timeline sample quota verification failed",
            check="aac_timeline_quota",
            expected=expected_samples,
            actual=timeline_samples,
            tolerance=AAC_SAMPLE_QUOTA_TOLERANCE,
        )
    if decoded_samples is None or not audio_sample_quota_matches(
        codec=audio.get("codec_name"),
        expected=expected_samples,
        actual=decoded_samples,
    ):
        raise ParallelCameraVerifyError(
            "parallel camera decoded sample quota verification failed",
            check="aac_decoded_quota",
            expected=expected_samples,
            actual=decoded_samples,
            tolerance=AAC_SAMPLE_QUOTA_TOLERANCE,
        )
    return ParallelCameraVerification(
        bytes=output_path.stat().st_size,
        content_hash=_sha256_file(output_path),
        frame_count=actual_frames,
        audio_samples=decoded_samples,
        accepted=True,
    )


def _build_command(
    slots: list[dict[str, object]],
    *,
    source_paths: dict[str, Path],
    sources: dict[str, SourceAsset],
    output_path: Path,
    settings: dict[str, object],
    ffmpeg: ToolResolution,
    total_frames: int,
    total_samples: int,
    filter_script: Path,
) -> list[str]:
    command = [ffmpeg.resolved_path, "-hide_banner", "-loglevel", "error", "-nostdin"]
    input_index = 0
    for slot in slots:
        if slot["classification"] != "mapped":
            continue
        auxiliary = slot["auxiliary_ref"]
        assert isinstance(auxiliary, dict)
        source_id = str(auxiliary["source_id"])
        source = sources[source_id]
        source_path = source_paths[source_id]
        command.extend(["-ss", ticks_to_seconds_text(int(auxiliary["source_start_ticks"])), "-t", ticks_to_seconds_text(int(auxiliary["source_end_ticks"]) - int(auxiliary["source_start_ticks"]))])
        if source.probe.video_codec is not None:
            command.append("-noautorotate")
        command.extend(["-i", str(source_path)])
        input_index += 1
    frame_rate = cast(dict[str, object], settings["frame_rate"])
    rate = f"{frame_rate['numerator']}/{frame_rate['denominator']}"
    command.extend(["-/filter_complex", str(filter_script), "-map", "[vout]", "-map", "[aout]", "-frames:v", str(total_frames), "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-color_range", "tv", "-r", rate, "-fps_mode", "cfr", "-c:a", "aac", "-ar", str(settings["audio_sample_rate"]), "-ac", "2", "-map_metadata", "-1", "-movflags", "+faststart", "-f", "mp4", "-y", str(output_path)])
    del input_index, total_samples
    return command


def _build_filter_script(
    slots: list[dict[str, object]],
    sources: dict[str, SourceAsset],
    settings: dict[str, object],
    *,
    total_samples: int,
) -> str:
    frame_rate = cast(dict[str, object], settings["frame_rate"])
    rate = f"{frame_rate['numerator']}/{frame_rate['denominator']}"
    video_labels: list[str] = []
    audio_labels: list[str] = []
    input_index = 0
    lines: list[str] = []
    for slot_index, slot in enumerate(slots):
        frames = cast(int, slot["video_frame_quota"])
        samples = cast(int, slot["audio_sample_quota"])
        if slot["classification"] == "mapped":
            auxiliary = slot["auxiliary_ref"]
            assert isinstance(auxiliary, dict)
            source_id = str(auxiliary["source_id"])
            source = sources[source_id]
            if frames > 0 and source.probe.video_codec is not None:
                rotation_filters = _rotation_filters(source.probe.rotation_degrees)
                rotation_prefix = f"{','.join(rotation_filters)}," if rotation_filters else ""
                lines.append(
                    f"[{input_index}:v:0]setpts=PTS-STARTPTS,{rotation_prefix}scale={settings['width']}:{settings['height']}:force_original_aspect_ratio=decrease:in_range=auto:out_range=tv,pad={settings['width']}:{settings['height']}:(ow-iw)/2:(oh-ih)/2:color=black,fps=fps={rate}:start_time=0:round=near,trim=end_frame={frames},setpts=PTS-STARTPTS,setsar=1,format=yuv420p,setparams=range=limited[v_{slot_index}]"
                )
                video_labels.append(f"[v_{slot_index}]")
            elif frames > 0:
                raise ParallelCameraEncodeError("mapped camera source has no video stream")
            if samples > 0 and source.probe.audio_codec is not None:
                lines.append(f"[{input_index}:a:0]asetpts=PTS-STARTPTS,aresample={settings['audio_sample_rate']}:first_pts=0,aformat=sample_fmts=fltp:sample_rates={settings['audio_sample_rate']}:channel_layouts=stereo,atrim=end_sample={samples},asetpts=PTS-STARTPTS[a_{slot_index}]")
                audio_labels.append(f"[a_{slot_index}]")
            elif samples > 0:
                raise ParallelCameraEncodeError("mapped camera source has no audio stream")
            input_index += 1
        else:
            if frames > 0:
                lines.append(f"color=c=black:s={settings['width']}x{settings['height']}:r={rate},trim=end_frame={frames},setpts=PTS-STARTPTS,format=yuv420p,setparams=range=limited[v_{slot_index}]")
                video_labels.append(f"[v_{slot_index}]")
            if samples > 0:
                lines.append(f"anullsrc=r={settings['audio_sample_rate']}:cl=stereo,atrim=end_sample={samples},asetpts=PTS-STARTPTS[a_{slot_index}]")
                audio_labels.append(f"[a_{slot_index}]")
    if not video_labels or not audio_labels:
        raise ParallelCameraEncodeError("parallel camera plan has no encodable quota")
    lines.append("".join(video_labels) + f"concat=n={len(video_labels)}:v=1:a=0[vout]")
    lines.append(
        "".join(audio_labels)
        + f"concat=n={len(audio_labels)}:v=0:a=1[aout_concat]"
    )
    lines.append(
        f"[aout_concat]atrim=end_sample={total_samples},asetpts=PTS-STARTPTS[aout]"
    )
    return ";\n".join(lines) + "\n"


def _terminate_child(
    process: subprocess.Popen[str],
) -> tuple[KeyboardInterrupt | SystemExit | None, OSError | None]:
    deferred_hard_exit: KeyboardInterrupt | SystemExit | None = None
    control_error: OSError | None = None

    reaped, poll_hard_exit, poll_error = _poll_child_reaped(process)
    if poll_hard_exit is not None:
        deferred_hard_exit = poll_hard_exit
    if poll_error is not None:
        control_error = poll_error
    if not reaped:
        try:
            process.terminate()
        except (KeyboardInterrupt, SystemExit) as error:
            if deferred_hard_exit is None:
                deferred_hard_exit = error
        except OSError as error:
            if control_error is None:
                control_error = error
        try:
            process.wait(timeout=5)
        except (KeyboardInterrupt, SystemExit) as error:
            if deferred_hard_exit is None:
                deferred_hard_exit = error
        except subprocess.TimeoutExpired:
            pass
        except OSError as error:
            if control_error is None:
                control_error = error
        reaped, poll_hard_exit, poll_error = _poll_child_reaped(process)
        if poll_hard_exit is not None and deferred_hard_exit is None:
            deferred_hard_exit = poll_hard_exit
        if poll_error is not None and control_error is None:
            control_error = poll_error
    if not reaped:
        try:
            process.kill()
        except (KeyboardInterrupt, SystemExit) as error:
            if deferred_hard_exit is None:
                deferred_hard_exit = error
        except OSError as error:
            if control_error is None:
                control_error = error
        reaped, poll_hard_exit, poll_error = _poll_child_reaped(process)
        if poll_hard_exit is not None and deferred_hard_exit is None:
            deferred_hard_exit = poll_hard_exit
        if poll_error is not None and control_error is None:
            control_error = poll_error
    if not reaped or process.returncode is None:
        wait_hard_exit, wait_error = _blocking_wait_for_reap(process)
        if wait_hard_exit is not None and deferred_hard_exit is None:
            deferred_hard_exit = wait_hard_exit
        if wait_error is not None and control_error is None:
            control_error = wait_error
    return deferred_hard_exit, control_error


def _poll_child_reaped(
    process: subprocess.Popen[str],
) -> tuple[
    bool,
    KeyboardInterrupt | SystemExit | None,
    OSError | None,
]:
    if process.returncode is not None:
        return True, None, None
    try:
        return process.poll() is not None, None, None
    except (KeyboardInterrupt, SystemExit) as error:
        return False, error, None
    except OSError as error:
        return False, None, error


def _blocking_wait_for_reap(
    process: subprocess.Popen[str],
) -> tuple[KeyboardInterrupt | SystemExit | None, OSError | None]:
    deferred_hard_exit: KeyboardInterrupt | SystemExit | None = None
    control_error: OSError | None = None
    while process.returncode is None:
        try:
            process.wait()
        except (KeyboardInterrupt, SystemExit) as error:
            if deferred_hard_exit is None:
                deferred_hard_exit = error
        except OSError as error:
            if control_error is None:
                control_error = error
    return deferred_hard_exit, control_error


def _integer_text(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value >= 0 else None
    if not isinstance(value, str):
        return None
    try:
        result = int(value)
    except ValueError:
        return None
    return result if result >= 0 else None


def _rate(value: object) -> Fraction | None:
    if not isinstance(value, str) or "/" not in value:
        return None
    numerator, denominator = value.split("/", 1)
    try:
        result = Fraction(int(numerator), int(denominator))
    except (ValueError, ZeroDivisionError):
        return None
    return result if result > 0 else None


def _audio_samples(stderr: str) -> int | None:
    matches = _SAMPLES_RE.findall(stderr)
    return int(matches[-1]) if matches else None


def _timeline_samples(audio: dict[str, object], sample_rate: int) -> int | None:
    duration_ts = _integer_text(audio.get("duration_ts"))
    time_base = _rate(audio.get("time_base"))
    if duration_ts is None or time_base is None:
        return None
    value = Fraction(duration_ts) * time_base * sample_rate
    return value.numerator // value.denominator if value.denominator == 1 else None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
