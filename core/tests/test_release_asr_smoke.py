from __future__ import annotations

import json
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts import release_asr_smoke

from roughcut.adapters.runtime_binding import RuntimeBindingError


def _binding(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        python=SimpleNamespace(interpreter=str(tmp_path / "venv/bin/python")),
        components={
            name: SimpleNamespace(path=str(tmp_path / "components" / name))
            for name in ("asr", "vad", "punc")
        },
        ffmpeg=SimpleNamespace(
            command=str(tmp_path / "ffmpeg"),
            version="ffmpeg version 9.0-fixture",
        ),
    )


def test_smoke_generates_pcm_uses_persistent_selection_and_cleans_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    binding = _binding(tmp_path)
    monkeypatch.setattr(release_asr_smoke, "load_runtime_binding", lambda _path: binding)

    def fake_runner(
        source: Path,
        raw_output: Path,
        *,
        config: object,
        process_runner: object,
    ) -> SimpleNamespace:
        with wave.open(str(source), "rb") as fixture:
            captured["format"] = (
                fixture.getnchannels(),
                fixture.getsampwidth(),
                fixture.getframerate(),
                fixture.getnframes(),
            )
        captured["source"] = source
        captured["raw_output"] = raw_output
        captured["config"] = config
        captured["process_runner"] = process_runner
        raw_output.write_text("[]\n", encoding="utf-8")
        return SimpleNamespace(package_version="1.3.14", exit_status=0)

    result = release_asr_smoke.run_smoke(tmp_path / "runtime.json", runner=fake_runner)

    assert result["ok"] is True
    assert result["fixture"] == "generated_pcm_wav"
    assert captured["format"] == (1, 2, 16_000, 4_000)
    config = captured["config"]
    assert isinstance(config, release_asr_smoke.FunASRConfig)
    assert config.asr_model_path == Path(binding.components["asr"].path)
    assert config.vad_model_path == Path(binding.components["vad"].path)
    assert config.punc_model_path == Path(binding.components["punc"].path)
    assert config.ffmpeg_command == binding.ffmpeg.command
    assert config.model_root.parent == Path(captured["source"]).parent
    assert not Path(captured["source"]).exists()
    assert not Path(captured["raw_output"]).exists()
    assert not Path(captured["source"]).parent.exists()


def test_main_returns_path_free_json_for_binding_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        release_asr_smoke,
        "load_runtime_binding",
        lambda _path: (_ for _ in ()).throw(RuntimeBindingError("fixture path")),
    )

    exit_code = release_asr_smoke.main(
        ["--runtime-binding", str(tmp_path / "runtime.json"), "--json"]
    )

    assert exit_code == 2
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["ok"] is False
    assert payload["error"]["responsibility"] == "runtime_binding"
    assert payload["error"]["action"] == "load_persistent_binding"
    assert "fixture path" not in output
