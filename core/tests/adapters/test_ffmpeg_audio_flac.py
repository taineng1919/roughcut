from __future__ import annotations

import json
import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from roughcut.adapters.ffmpeg.audio import (
    FLAC_CHANNELS,
    FLAC_SAMPLE_FORMAT,
    FLAC_SAMPLE_RATE_HZ,
    FFmpegAudioError,
    decode_audio_to_pcm,
    encode_audio_to_flac,
)

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def make_source(path: Path, *, sample_rate: int, channels: int) -> None:
    assert FFMPEG is not None
    result = subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate={sample_rate}:duration=0.5",
            "-c:a",
            "pcm_s16le",
            "-ac",
            str(channels),
            "-ar",
            str(sample_rate),
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def probe_streams(path: Path) -> dict[str, object]:
    assert FFPROBE is not None
    result = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            (
                "stream=codec_name,sample_rate,channels,sample_fmt,"
                "bits_per_raw_sample,duration"
            ),
            "-of",
            "json",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    streams = payload["streams"]
    assert len(streams) == 1
    return streams[0]


@pytest.mark.parametrize(("sample_rate", "channels"), [(48_000, 2), (44_100, 1)])
def test_flac_profile_is_16khz_mono_s16(
    tmp_path: Path,
    synthetic_media_runtime: tuple[str, str],
    sample_rate: int,
    channels: int,
) -> None:
    ffmpeg, _ = synthetic_media_runtime
    source = tmp_path / "source.wav"
    output = tmp_path / "cloud.flac"
    make_source(source, sample_rate=sample_rate, channels=channels)

    encoded = encode_audio_to_flac(source, output, ffmpeg_command=ffmpeg)

    assert encoded.sample_rate_hz == 16_000
    assert encoded.channels == 1
    assert encoded.sample_format == FLAC_SAMPLE_FORMAT == "s16"
    assert encoded.bits_per_sample == 16
    assert encoded.container_format == "flac"
    assert encoded.audio_stream == "0:a:0"
    assert encoded.ffmpeg_path == ffmpeg
    assert encoded.ffmpeg_version.startswith("ffmpeg version ")
    assert output.read_bytes().startswith(b"fLaC")

    stream = probe_streams(output)
    assert stream["codec_name"] == "flac"
    assert int(stream["sample_rate"]) == FLAC_SAMPLE_RATE_HZ == 16_000
    assert int(stream["channels"]) == FLAC_CHANNELS == 1
    assert stream["sample_fmt"] == FLAC_SAMPLE_FORMAT
    assert int(stream["bits_per_raw_sample"]) == 16


def test_existing_pcm_contract_is_unchanged(
    tmp_path: Path, synthetic_media_runtime: tuple[str, str]
) -> None:
    ffmpeg, _ = synthetic_media_runtime
    source = tmp_path / "source.wav"
    pcm_output = tmp_path / "asr.wav"
    flac_output = tmp_path / "cloud.flac"
    make_source(source, sample_rate=44_100, channels=2)

    decoded = decode_audio_to_pcm(source, pcm_output, ffmpeg_command=ffmpeg)
    encode_audio_to_flac(source, flac_output, ffmpeg_command=ffmpeg)

    with wave.open(str(pcm_output), "rb") as pcm:
        assert pcm.getframerate() == 16_000
        assert pcm.getnchannels() == 1
        assert pcm.getsampwidth() == 2
        assert pcm.getcomptype() == "NONE"
        assert pcm.getnframes() > 0
    assert decoded.container_format == "wav"
    assert decoded.sample_format == "s16le"
    assert decoded.audio_stream == "0:a:0"
    # The remote profile is a sibling container, not a change to the local one.
    assert pcm_output.read_bytes()[:4] == b"RIFF"
    assert flac_output.read_bytes()[:4] == b"fLaC"


def fake_ffmpeg(**kwargs: object) -> tuple[Path, str]:
    assert FFMPEG is not None
    return Path(FFMPEG), "ffmpeg version fake"


def test_incomplete_output_is_removed_when_ffmpeg_fails(tmp_path: Path) -> None:
    executable, version = fake_ffmpeg()
    source = tmp_path / "source.wav"
    source.write_bytes(b"synthetic")
    output = tmp_path / "cloud.flac"

    def failing_runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        output.write_bytes(b"fLaCpartial")
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")

    with pytest.raises(FFmpegAudioError):
        encode_audio_to_flac(
            source,
            output,
            ffmpeg_command=str(executable),
            ffmpeg_version=version,
            process_runner=failing_runner,
        )

    assert not output.exists()
    assert not list(tmp_path.glob(".cloud.flac.*"))


def test_output_that_is_not_the_flac_profile_is_removed(tmp_path: Path) -> None:
    executable, version = fake_ffmpeg()
    source = tmp_path / "source.wav"
    source.write_bytes(b"synthetic")
    output = tmp_path / "cloud.flac"
    output.write_bytes(b"stale-output")

    def wrong_output_runner(
        *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        output.write_bytes(b"RIFFnot-flac")
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    with pytest.raises(FFmpegAudioError):
        encode_audio_to_flac(
            source,
            output,
            ffmpeg_command=str(executable),
            ffmpeg_version=version,
            process_runner=wrong_output_runner,
        )

    assert not output.exists()


def test_flac_stream_with_unexpected_profile_is_rejected(tmp_path: Path) -> None:
    executable, version = fake_ffmpeg()
    source = tmp_path / "source.wav"
    source.write_bytes(b"synthetic")
    output = tmp_path / "cloud.flac"
    # A structurally valid FLAC header that declares 44.1 kHz stereo 24 bit.
    stream_info = bytearray(b"fLaC" + bytes([0x00, 0x00, 0x00, 0x22]))
    stream_info += bytes(10)
    packed = (44_100 << 44) | ((2 - 1) << 41) | ((24 - 1) << 36) | 44_100
    stream_info += packed.to_bytes(8, "big")

    def wrong_profile_runner(
        *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        output.write_bytes(bytes(stream_info))
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    with pytest.raises(FFmpegAudioError):
        encode_audio_to_flac(
            source,
            output,
            ffmpeg_command=str(executable),
            ffmpeg_version=version,
            process_runner=wrong_profile_runner,
        )

    assert not output.exists()


def test_timeout_removes_the_partial_output(tmp_path: Path) -> None:
    executable, version = fake_ffmpeg()
    source = tmp_path / "source.wav"
    source.write_bytes(b"synthetic")
    output = tmp_path / "cloud.flac"

    def timing_out_runner(*args: object, **kwargs: object) -> object:
        output.write_bytes(b"fLaCpartial")
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=1)

    with pytest.raises(FFmpegAudioError):
        encode_audio_to_flac(
            source,
            output,
            ffmpeg_command=str(executable),
            ffmpeg_version=version,
            process_runner=timing_out_runner,
        )

    assert not output.exists()


def test_missing_ffmpeg_executable_fails_before_writing(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    source.write_bytes(b"synthetic")
    output = tmp_path / "nested" / "cloud.flac"

    with pytest.raises(FFmpegAudioError):
        encode_audio_to_flac(
            source, output, ffmpeg_command=str(tmp_path / "missing-ffmpeg")
        )

    assert not output.exists()


def test_output_parent_directory_is_created(tmp_path: Path) -> None:
    executable, version = fake_ffmpeg()
    source = tmp_path / "source.wav"
    source.write_bytes(b"synthetic")
    output = tmp_path / "nested" / "cloud.flac"

    def valid_runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        stream_info = bytearray(b"fLaC" + bytes([0x00, 0x00, 0x00, 0x22]))
        stream_info += bytes(10)
        packed = (16_000 << 44) | (0 << 41) | (15 << 36) | 16_000
        stream_info += packed.to_bytes(8, "big")
        output.write_bytes(bytes(stream_info))
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    encode_audio_to_flac(
        source,
        output,
        ffmpeg_command=str(executable),
        ffmpeg_version=version,
        process_runner=valid_runner,
    )

    assert output.exists()


def test_flac_stream_with_no_samples_is_rejected(tmp_path: Path) -> None:
    """`total_samples == 0` is not a usable cloud profile."""

    executable, version = fake_ffmpeg()
    source = tmp_path / "source.wav"
    source.write_bytes(b"synthetic")
    output = tmp_path / "cloud.flac"

    def empty_runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        stream_info = bytearray(b"fLaC" + bytes([0x00, 0x00, 0x00, 0x22]))
        stream_info += bytes(10)
        packed = (16_000 << 44) | (0 << 41) | (15 << 36) | 0
        stream_info += packed.to_bytes(8, "big")
        output.write_bytes(bytes(stream_info))
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    with pytest.raises(FFmpegAudioError):
        encode_audio_to_flac(
            source,
            output,
            ffmpeg_command=str(executable),
            ffmpeg_version=version,
            process_runner=empty_runner,
        )

    assert not output.exists()
