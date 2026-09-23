from __future__ import annotations

import shutil
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from roughcut.adapters.ffmpeg.audio import FFmpegAudioError, decode_audio_to_pcm

FFMPEG = shutil.which("ffmpeg")


def _make_audio(path: Path, *, sample_rate: int, channels: int) -> None:
    if FFMPEG is None:
        pytest.skip("ffmpeg is required for synthetic audio tests")
    command = [
        FFMPEG,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=800:sample_rate={sample_rate}:duration=0.4",
    ]
    if path.suffix == ".mp4":
        command.extend(
            [
                "-f",
                "lavfi",
                "-i",
                "color=size=64x64:rate=25:duration=0.4",
                "-shortest",
                "-c:v",
                "mpeg4",
                "-c:a",
                "aac",
            ]
        )
    elif path.suffix == ".mp3":
        command.extend(["-c:a", "libmp3lame"])
    else:
        command.extend(["-c:a", "pcm_s16le"])
    command.extend(["-ac", str(channels), "-ar", str(sample_rate), str(path)])
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("suffix", "sample_rate", "channels"),
    [(".mp3", 44_100, 1), (".mp4", 32_000, 2), (".wav", 48_000, 2)],
)
def test_decodes_supported_media_to_the_same_pcm_contract(
    tmp_path: Path, suffix: str, sample_rate: int, channels: int
) -> None:
    source = tmp_path / f"source{suffix}"
    _make_audio(source, sample_rate=sample_rate, channels=channels)
    output = tmp_path / "decoded.wav"

    assert FFMPEG is not None
    decoded = decode_audio_to_pcm(source, output, ffmpeg_command=FFMPEG)

    with wave.open(str(output), "rb") as pcm:
        assert pcm.getframerate() == 16_000
        assert pcm.getnchannels() == 1
        assert pcm.getsampwidth() == 2
        assert pcm.getcomptype() == "NONE"
        assert pcm.getnframes() > 0
    assert decoded.sample_rate_hz == 16_000
    assert decoded.channels == 1
    assert decoded.sample_format == "s16le"
    assert decoded.container_format == "wav"
    assert decoded.audio_stream == "0:a:0"
    assert decoded.ffmpeg_path == FFMPEG
    assert decoded.ffmpeg_version.startswith("ffmpeg version ")


def test_decode_does_not_preserve_container_pts_as_leading_pcm_time(tmp_path: Path) -> None:
    if FFMPEG is None:
        pytest.skip("ffmpeg is required for synthetic audio tests")
    source_wave = tmp_path / "source.wav"
    _make_audio(source_wave, sample_rate=48_000, channels=1)
    delayed = tmp_path / "delayed.m4a"
    result = subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-itsoffset",
            "2",
            "-i",
            str(source_wave),
            "-c:a",
            "aac",
            str(delayed),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    output = tmp_path / "decoded.wav"

    decode_audio_to_pcm(delayed, output, ffmpeg_command=FFMPEG)

    with wave.open(str(output), "rb") as pcm:
        duration = pcm.getnframes() / pcm.getframerate()
    assert 0.3 <= duration <= 0.6


def test_ffmpeg_failure_removes_partial_pcm(tmp_path: Path) -> None:
    source = tmp_path / "source.mp3"
    source.write_bytes(b"fixture")
    output = tmp_path / "decoded.wav"

    def fail_decode(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if "-version" in command:
            return subprocess.CompletedProcess(
                command, 0, stdout="ffmpeg version fixture\n", stderr=""
            )
        output.write_bytes(b"partial")
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="decode failed")

    with pytest.raises(FFmpegAudioError, match="decode"):
        decode_audio_to_pcm(
            source,
            output,
            ffmpeg_command=sys.executable,
            process_runner=fail_decode,
        )

    assert not output.exists()
