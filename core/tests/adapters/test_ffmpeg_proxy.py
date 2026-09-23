from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from roughcut.adapters.ffmpeg.proxy import (
    FFmpegProxyError,
    ProxyCancelled,
    ProxySourceInspection,
    ProxyUnsupported,
    build_proxy_command,
    proxy_quotas,
    reject_unsupported_color,
    transcode_proxy,
    verify_proxy_output,
)
from roughcut.domain.project import MediaProbe
from roughcut.domain.proxy import derive_proxy_profile
from roughcut.domain.render import ToolResolution


def _probe(**changes: object) -> MediaProbe:
    values: dict[str, object] = {
        "duration_ticks": 125_000,
        "container_start_ticks": 12_000,
        "first_content_ticks": 24_000,
        "video_codec": "h264",
        "width": 1080,
        "height": 1920,
        "nominal_frame_rate": {"numerator": 30, "denominator": 1},
        "is_vfr": True,
        "audio_codec": "aac",
        "audio_sample_rate": 44_100,
        "rotation_degrees": 90,
    }
    values.update(changes)
    return MediaProbe(**values)  # type: ignore[arg-type]


def _profile(probe: MediaProbe):  # type: ignore[no-untyped-def]
    return derive_proxy_profile(
        probe,
        {
            "frame_rate": {"numerator": 25, "denominator": 1},
            "width": 1920,
            "height": 1080,
        },
    )


def test_command_is_bounded_to_fixed_software_profile_and_primary_streams(tmp_path: Path) -> None:
    probe = _probe()
    profile = _profile(probe)
    ffmpeg = ToolResolution("ffmpeg", "/tools/ffmpeg", "ffmpeg version fixture")
    command = build_proxy_command(
        tmp_path / "中文 source.mp4", tmp_path / "proxy.mp4", probe, profile, ffmpeg
    )
    joined = " ".join(command)

    assert "-noautorotate" in command
    assert "[0:v:0]" in joined and "[0:a:0]" in joined
    assert "transpose=clock" in joined
    assert "fps=fps=25/1" in joined
    assert "apad" in joined and "atrim=end_sample" in joined
    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-preset") + 1] == "veryfast"
    assert command[command.index("-crf") + 1] == "23"
    assert command[command.index("-pix_fmt") + 1] == "yuv420p"
    assert command[command.index("-movflags") + 1] == "+faststart"
    assert "-hwaccel" not in command
    assert proxy_quotas(probe.duration_ticks, profile) == (27, 50_000)


def test_audio_only_gets_black_video_and_video_only_gets_no_fake_audio(tmp_path: Path) -> None:
    audio = _probe(video_codec=None, width=None, height=None, rotation_degrees=0)
    audio_command = build_proxy_command(
        tmp_path / "audio.wav",
        tmp_path / "audio.mp4",
        audio,
        _profile(audio),
        ToolResolution("ffmpeg", "/tools/ffmpeg", "ffmpeg version fixture"),
    )
    assert "color=c=black" in " ".join(audio_command)
    assert "[0:a:0]" in " ".join(audio_command)

    video = _probe(audio_codec=None, audio_sample_rate=None)
    video_command = build_proxy_command(
        tmp_path / "silent.mp4",
        tmp_path / "silent-proxy.mp4",
        video,
        _profile(video),
        ToolResolution("ffmpeg", "/tools/ffmpeg", "ffmpeg version fixture"),
    )
    assert "[0:a:0]" not in " ".join(video_command)
    assert "-c:a" not in video_command


def test_hdr_transfer_is_explicitly_unsupported() -> None:
    completed = subprocess.CompletedProcess(
        [], 0, '{"streams":[{"color_transfer":"smpte2084"}]}', ""
    )
    with pytest.raises(ProxyUnsupported):
        reject_unsupported_color(
            Path("fixture.mp4"),
            ToolResolution("ffprobe", "/tools/ffprobe", "ffprobe version fixture"),
            process_runner=lambda *_args, **_kwargs: completed,
        )


def test_source_inspection_returns_primary_stream_durations() -> None:
    completed = subprocess.CompletedProcess(
        [],
        0,
        (
            '{"streams":['
            '{"codec_type":"video","start_time":"1.00","duration":"0.72"},'
            '{"codec_type":"audio","start_time":"1.40","duration":"0.36"}]}'
        ),
        "",
    )
    inspection = reject_unsupported_color(
        Path("fixture.mp4"),
        ToolResolution("ffprobe", "/tools/ffprobe", "ffprobe version fixture"),
        process_runner=lambda *_args, **_kwargs: completed,
    )
    assert inspection.video_duration_ticks == 86_400
    assert inspection.audio_duration_ticks == 43_200
    assert inspection.video_start_ticks == 120_000
    assert inspection.audio_start_ticks == 168_000
    assert inspection.origin_ticks == 120_000


def test_command_uses_one_origin_and_explicit_leading_video_or_audio_padding(
    tmp_path: Path,
) -> None:
    probe = _probe(duration_ticks=240_000, rotation_degrees=0)
    profile = _profile(probe)
    ffmpeg = ToolResolution("ffmpeg", "/tools/ffmpeg", "ffmpeg version fixture")

    audio_late = build_proxy_command(
        tmp_path / "audio-late.mp4",
        tmp_path / "proxy.mp4",
        probe,
        profile,
        ffmpeg,
        source_inspection=ProxySourceInspection(
            video_start_ticks=120_000,
            video_duration_ticks=120_000,
            audio_start_ticks=168_000,
            audio_duration_ticks=72_000,
            origin_ticks=120_000,
        ),
    )
    audio_graph = audio_late[audio_late.index("-filter_complex") + 1]
    assert "adelay=19200S:all=1" in audio_graph
    assert "tpad=start=" not in audio_graph

    video_late = build_proxy_command(
        tmp_path / "video-late.mp4",
        tmp_path / "proxy.mp4",
        probe,
        profile,
        ffmpeg,
        source_inspection=ProxySourceInspection(
            video_start_ticks=168_000,
            video_duration_ticks=72_000,
            audio_start_ticks=120_000,
            audio_duration_ticks=120_000,
            origin_ticks=120_000,
        ),
    )
    video_graph = video_late[video_late.index("-filter_complex") + 1]
    assert "tpad=start=10:start_mode=add:color=black" in video_graph
    assert "adelay=" not in video_graph


@pytest.mark.parametrize(
    ("failure", "error_type"),
    [
        (subprocess.TimeoutExpired(["ffmpeg"], 1), FFmpegProxyError),
        (KeyboardInterrupt(), ProxyCancelled),
    ],
)
def test_timeout_and_interrupt_are_stable_proxy_failures(
    tmp_path: Path, failure: BaseException, error_type: type[BaseException]
) -> None:
    def fail(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        raise failure

    probe = _probe()
    with pytest.raises(error_type):
        transcode_proxy(
            tmp_path / "source.mp4",
            output_path=tmp_path / "proxy.mp4",
            probe=probe,
            profile=_profile(probe),
            ffmpeg=ToolResolution("ffmpeg", "/tools/ffmpeg", "ffmpeg version fixture"),
            process_runner=fail,
        )


def test_ready_verification_does_not_count_frames_or_decode(tmp_path: Path) -> None:
    output = tmp_path / "ready.mp4"
    output.write_bytes(b"\x00\x00\x00\x08ftyp\x00\x00\x00\x08moov")
    probe = _probe(rotation_degrees=0)
    profile = _profile(probe)
    commands: list[list[str]] = []
    timeouts: list[float] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        timeouts.append(float(kwargs["timeout"]))
        payload = {
            "format": {"format_name": "mov,mp4", "duration": "1.041667"},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "pix_fmt": "yuv420p",
                    "width": profile.canvas_width,
                    "height": profile.canvas_height,
                    "avg_frame_rate": "25/1",
                    "start_time": "0",
                },
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "sample_rate": "48000",
                    "start_time": "0",
                },
            ],
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    report = verify_proxy_output(
        output,
        probe,
        profile,
        ToolResolution("ffprobe", "/tools/ffprobe", "ffprobe version fixture"),
        decode=False,
        process_runner=run,
    )

    assert report.accepted
    assert len(commands) == 1
    assert "-count_frames" not in commands[0]
    assert timeouts == [60]


def test_full_verification_uses_long_timeout_for_frame_count(tmp_path: Path) -> None:
    output = tmp_path / "candidate.mp4"
    output.write_bytes(b"\x00\x00\x00\x08ftyp\x00\x00\x00\x08moov")
    probe = _probe(rotation_degrees=0)
    profile = _profile(probe)
    captured: dict[str, object] = {}

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["timeout"] = kwargs["timeout"]
        return subprocess.CompletedProcess(command, 1, "{}", "")

    with pytest.raises(FFmpegProxyError, match="could not be probed"):
        verify_proxy_output(
            output,
            probe,
            profile,
            ToolResolution("ffprobe", "/tools/ffprobe", "ffprobe version fixture"),
            decode=True,
            process_runner=run,
        )

    command = captured["command"]
    assert isinstance(command, list)
    assert "-count_frames" in command
    assert captured["timeout"] == 3600
