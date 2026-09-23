"""FFmpeg alignment decode adapter: L/R pan-filter command owner tests.

These tests run without an Audalign runtime: a fake process_runner records
the argv and produces a legal mono/44100 Hz/PCM16 WAV, so the decode path is
exercised end to end including the WAV contract validation.
"""

from __future__ import annotations

import subprocess
import wave
from pathlib import Path

import pytest

from roughcut.adapters.audalign.ffmpeg_audio import (
    ANALYSIS_AUDIO_STREAM,
    decode_alignment_audio,
)

VERSION_FIXTURE = "ffmpeg version fixture-7.1"


class _FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(
        self,
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(command))
        if command[1] == "-version":
            return subprocess.CompletedProcess(
                command, 0, stdout=VERSION_FIXTURE + "\n", stderr=""
            )
        # decode call: write a legal mono 44.1 kHz PCM16 WAV to the output
        # path given after the "-f wav" pair
        if command[1] == "-hide_banner":
            output = Path(command[command.index("wav") + 1])
            with wave.open(str(output), "wb") as pcm:
                pcm.setnchannels(1)
                pcm.setsampwidth(2)
                pcm.setframerate(44_100)
                pcm.writeframes(b"\x00\x00" * 44_100)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


@pytest.mark.parametrize(
    ("channel", "expected_filter"),
    [
        (None, None),
        ("left", "pan=mono|c0=c0"),
        ("right", "pan=mono|c0=c1"),
    ],
)
def test_lr_pan_filter_command_owner(
    tmp_path: Path,
    channel: str | None,
    expected_filter: str | None,
) -> None:
    """The L/R fallback keeps `-map 0:a:0` and adds the exact pan filter as
    separate argv entries; no `.c=` stream specifier ever appears."""
    runner = _FakeRunner()
    executable = tmp_path / "ffmpeg"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    source = tmp_path / "source.wav"
    source.write_bytes(b"fixture")
    output = tmp_path / "out.wav"
    kwargs: dict[str, object] = {"process_runner": runner}
    if channel is not None:
        kwargs["channel"] = channel
    result = decode_alignment_audio(
        source,
        output,
        ffmpeg_command=str(executable),
        **kwargs,
    )
    # the version probe ran first, then exactly one decode call
    assert len(runner.calls) == 2
    version_call, decode_call = runner.calls
    assert version_call[1] == "-version"
    assert "-map" in decode_call
    map_index = decode_call.index("-map")
    assert decode_call[map_index + 1] == ANALYSIS_AUDIO_STREAM == "0:a:0"
    if expected_filter is None:
        assert "-af" not in decode_call
    else:
        filter_index = decode_call.index("-af")
        assert decode_call[filter_index + 1] == expected_filter
        # the filter must sit before the final output path
        assert filter_index < len(decode_call) - 1
    # the final argv entry is exactly the output path
    assert decode_call[-1] == str(output)
    # no stream specifier with ".c=" may appear anywhere
    assert all(".c=" not in item for item in decode_call)
    # the returned decode contract keeps the full stream mapping
    assert result.audio_stream == "0:a:0"


def test_upstream_verified_version_skips_standalone_probe(tmp_path: Path) -> None:
    runner = _FakeRunner()
    executable = tmp_path / "ffmpeg"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    source = tmp_path / "source.wav"
    source.write_bytes(b"fixture")

    result = decode_alignment_audio(
        source,
        tmp_path / "out.wav",
        ffmpeg_command=str(executable),
        ffmpeg_version="ffmpeg version 8.1.1-bound",
        process_runner=runner,
    )

    assert len(runner.calls) == 1
    assert runner.calls[0][1] == "-hide_banner"
    assert result.ffmpeg_version == "ffmpeg version 8.1.1-bound"
    assert result.sample_rate_hz == 44_100
    assert result.channels == 1
    assert result.sample_format == "s16le"
    assert result.container_format == "wav"
