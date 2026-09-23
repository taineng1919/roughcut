"""Direct tests for the current Correlation auxiliary decode contract."""

from __future__ import annotations

import subprocess
import wave
from pathlib import Path

import pytest

from roughcut.application import alignments
from roughcut.domain.alignment import AlignmentError
from roughcut.domain.render import ToolResolution


def _write_wave(
    path: Path,
    *,
    sample_rate: int = 44_100,
    channels: int = 1,
    sample_width: int = 2,
    frames: int = 661_500,
) -> None:
    with wave.open(str(path), "wb") as output:
        output.setframerate(sample_rate)
        output.setnchannels(channels)
        output.setsampwidth(sample_width)
        output.writeframes(b"\0" * frames * channels * sample_width)


class _WorkspaceBudget:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir()

    def reserve_decode(self, *, duration_ticks: int) -> None:
        assert duration_ticks == 1_800_000

    def recheck(self) -> None:
        return None


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param("nonzero", id="ffmpeg-nonzero"),
        pytest.param("unreadable", id="unreadable-wav"),
        pytest.param("wrong-stream", id="wrong-stream"),
        pytest.param("channels", id="wrong-channels"),
        pytest.param("rate", id="wrong-rate"),
        pytest.param("width", id="wrong-width"),
        pytest.param("length", id="wrong-length"),
    ],
)
def test_auxiliary_decode_failures_are_pair_decode_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    source = tmp_path / "source.wav"
    source.write_bytes(b"source fixture")
    output = tmp_path / "workspace" / "probe.wav"
    budget = _WorkspaceBudget(tmp_path / "workspace")
    seen_command: list[str] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        seen_command.extend(command)
        if failure in {"nonzero", "wrong-stream"}:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="decode")
        if failure == "unreadable":
            output.write_bytes(b"not a wav")
        else:
            settings = {
                "channels": {"channels": 2},
                "rate": {"sample_rate": 48_000},
                "width": {"sample_width": 1},
                "length": {"frames": 661_499},
            }.get(failure, {})
            _write_wave(output, **settings)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(alignments, "run_bounded_child", run)

    with pytest.raises(AlignmentError) as raised:
        alignments._extract_aux_excerpt(
            source,
            output,
            1_234_567,
            1_234_567 + 1_800_000,
            ToolResolution("ffmpeg", "ffmpeg", "fixture"),
            object(),  # type: ignore[arg-type]
            budget,  # type: ignore[arg-type]
        )

    assert raised.value.code == "auxiliary_decode_failed"
    assert "alignment_main_decode_failed" not in str(raised.value)
    assert seen_command[seen_command.index("-ss") + 1] == (
        "10.28805833333333333333333333"
    )
    assert seen_command[seen_command.index("-t") + 1] == "15"
    assert seen_command[seen_command.index("-map") + 1] == "0:a:0"
    assert seen_command[seen_command.index("-ac") + 1] == "1"
    assert seen_command[seen_command.index("-ar") + 1] == "44100"
    assert seen_command[seen_command.index("-c:a") + 1] == "pcm_s16le"
