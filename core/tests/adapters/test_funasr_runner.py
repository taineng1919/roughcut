from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import types
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Self

import pytest

from roughcut.adapters.component_environment import current_architecture, current_platform
from roughcut.adapters.component_installation import load_release_catalog
from roughcut.adapters.ffmpeg.audio import FFmpegAudioError, PCMDecode
from roughcut.adapters.funasr import runner, worker
from roughcut.adapters.funasr.runner import (
    ASR_MODEL,
    PUNC_MODEL,
    VAD_MODEL,
    FunASRConfig,
    FunASRRunnerError,
    SpeakerModelResolution,
    _model_paths,
    default_funasr_python,
    run_funasr,
)


def _write_pcm(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as pcm:
        pcm.setnchannels(1)
        pcm.setsampwidth(2)
        pcm.setframerate(16_000)
        pcm.writeframes(b"\0\0" * 1600)


def _decode_result() -> PCMDecode:
    return PCMDecode(
        ffmpeg_path="/fixture/ffmpeg",
        ffmpeg_version="ffmpeg version fixture",
        audio_stream="0:a:0",
        sample_rate_hz=16_000,
        channels=1,
        sample_format="s16le",
        container_format="wav",
    )


def _managed_model_fixture(root: Path) -> tuple[dict[str, Path], dict[str, object]]:
    profile = load_release_catalog().profile_for(
        current_platform(), current_architecture()
    )
    expected: dict[str, Path] = {}
    components: list[dict[str, object]] = []
    for name, record_name in runner.MANAGED_MODEL_RECORDS.items():
        model = profile.models[record_name]
        path = root / runner.MANAGED_MODELS[name]
        path.mkdir(parents=True)
        (path / "model.pt").write_bytes(f"{name} model".encode())
        expected[name] = path
        components.append(
            {
                "name": record_name,
                "kind": "model",
                "source_type": "managed",
                "origin": f"https://modelscope.cn/models/iic/{record_name}",
                "version": model.revision,
                "path": runner.MANAGED_MODELS[name],
                "platform": current_platform(),
                "architecture": current_architecture(),
                "license": "Apache-2.0",
                "verification": {
                    "algorithm": "sha256",
                    "value": model.directory_sha256,
                },
            }
        )
    manifest: dict[str, object] = {
        "schema_version": 2,
        "platform": current_platform(),
        "architecture": current_architecture(),
        "managed_root": str(root.resolve()),
        "components": components,
    }
    return expected, manifest


def _write_manifest(root: Path, payload: dict[str, object]) -> None:
    (root / "component-manifest.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def test_default_config_is_roughcut_scoped_and_environment_configurable(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("ROUGHCUT_FUNASR_PYTHON", raising=False)
    monkeypatch.delenv("ROUGHCUT_FUNASR_MODEL_ROOT", raising=False)
    defaults = FunASRConfig()
    assert defaults.python_path == default_funasr_python()
    assert defaults.model_root == Path.home() / ".roughcut/models/funasr"

    configured_python = tmp_path / "runtime" / "python"
    configured_models = tmp_path / "models"
    monkeypatch.setenv("ROUGHCUT_FUNASR_PYTHON", str(configured_python))
    monkeypatch.setenv("ROUGHCUT_FUNASR_MODEL_ROOT", str(configured_models))
    configured = FunASRConfig()
    assert configured.python_path == configured_python
    assert configured.model_root == configured_models


def test_default_python_path_is_platform_specific(monkeypatch) -> None:
    monkeypatch.delenv("ROUGHCUT_FUNASR_PYTHON", raising=False)

    assert default_funasr_python(platform="posix") == (
        Path.home() / ".roughcut/toolchains/funasr/venv/bin/python"
    )
    assert default_funasr_python(platform="nt") == (
        Path.home() / ".roughcut/toolchains/funasr/venv/Scripts/python.exe"
    )


def test_runner_passes_explicit_managed_model_directories(tmp_path: Path) -> None:
    python_path = tmp_path / "managed/venv/bin/python"
    python_path.parent.mkdir(parents=True)
    python_path.write_text("#!/bin/sh\n", encoding="utf-8")
    python_path.chmod(0o755)
    model_paths = {
        "asr": tmp_path / "managed/models/model_asr",
        "vad": tmp_path / "managed/models/model_vad",
        "punc": tmp_path / "managed/models/model_punc",
    }
    for path in model_paths.values():
        path.mkdir(parents=True)
    source = tmp_path / "source.mp3"
    source.write_bytes(b"fixture")
    raw_path = tmp_path / "raw.json"
    captured: dict[str, object] = {}

    def decode(_source: Path, pcm: Path) -> PCMDecode:
        _write_pcm(pcm)
        return _decode_result()

    def record_process(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        raw_path.write_text("[]\n", encoding="utf-8")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "package_version": "1.3.14",
                    "torch_version": "2.6.0",
                    "torchaudio_version": "2.6.0",
                    "started_at": "2026-07-19T00:00:00+00:00",
                    "completed_at": "2026-07-19T00:00:01+00:00",
                }
            ),
            stderr="",
        )

    run_funasr(
        source,
        raw_path,
        config=FunASRConfig(
            python_path=python_path,
            model_root=tmp_path / "managed",
            asr_model_path=model_paths["asr"],
            vad_model_path=model_paths["vad"],
            punc_model_path=model_paths["punc"],
        ),
        process_runner=record_process,
        decoder=decode,
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert command[command.index("--asr-model") + 1] == str(model_paths["asr"])
    assert command[command.index("--vad-model") + 1] == str(model_paths["vad"])
    assert command[command.index("--punc-model") + 1] == str(model_paths["punc"])


def test_runner_selects_manifest_owned_component_installer_models(
    tmp_path: Path,
) -> None:
    managed_root = tmp_path / "managed components"
    expected, manifest = _managed_model_fixture(managed_root)
    _write_manifest(managed_root, manifest)
    assert _model_paths(FunASRConfig(model_root=managed_root)) == expected


def test_runner_does_not_infer_managed_models_from_directories_alone(
    tmp_path: Path,
) -> None:
    managed_root = tmp_path / "unowned model directories"
    for relative in runner.MANAGED_MODELS.values():
        path = managed_root / relative
        path.mkdir(parents=True)
        (path / "model.pt").write_bytes(b"unowned")

    selected = _model_paths(FunASRConfig(model_root=managed_root))

    assert selected == {
        "asr": managed_root / ASR_MODEL,
        "vad": managed_root / VAD_MODEL,
        "punc": managed_root / PUNC_MODEL,
    }


@pytest.mark.parametrize(
    "mutation",
    ["wrong_revision", "wrong_source_type", "path_escape", "missing_record"],
)
def test_runner_rejects_unbound_managed_model_records(
    tmp_path: Path, mutation: str
) -> None:
    managed_root = tmp_path / mutation
    _expected, manifest = _managed_model_fixture(managed_root)
    components = manifest["components"]
    assert isinstance(components, list)
    first = components[0]
    assert isinstance(first, dict)
    if mutation == "wrong_revision":
        first["version"] = "unapproved-revision"
    elif mutation == "wrong_source_type":
        first["source_type"] = "external"
        first["path"] = str((managed_root / runner.MANAGED_MODELS["asr"]).resolve())
    elif mutation == "path_escape":
        first["path"] = "../outside"
    else:
        components.pop(0)
    _write_manifest(managed_root, manifest)
    with pytest.raises(FunASRRunnerError, match="manifest is invalid"):
        _model_paths(FunASRConfig(model_root=managed_root))


def test_runner_rejects_managed_model_record_with_wrong_verification_digest(
    tmp_path: Path,
) -> None:
    managed_root = tmp_path / "wrong verification digest"
    _expected, manifest = _managed_model_fixture(managed_root)
    components = manifest["components"]
    assert isinstance(components, list)
    first = components[0]
    assert isinstance(first, dict)
    verification = first["verification"]
    assert isinstance(verification, dict)
    verification["value"] = "0" * 64
    _write_manifest(managed_root, manifest)

    with pytest.raises(FunASRRunnerError, match="verification digest does not match"):
        _model_paths(FunASRConfig(model_root=managed_root))


@pytest.mark.parametrize("mutation", ["wrong_owner", "wrong_platform", "wrong_architecture"])
def test_runner_rejects_managed_manifest_with_wrong_owner_or_target(
    tmp_path: Path, mutation: str
) -> None:
    managed_root = tmp_path / mutation
    _expected, manifest = _managed_model_fixture(managed_root)
    components = manifest["components"]
    assert isinstance(components, list)
    if mutation == "wrong_owner":
        manifest["managed_root"] = str((tmp_path / "different owner").resolve())
    elif mutation == "wrong_platform":
        wrong_platform = "macos" if current_platform() != "macos" else "windows"
        manifest["platform"] = wrong_platform
        for component in components:
            assert isinstance(component, dict)
            component["platform"] = wrong_platform
    else:
        wrong_architecture = "arm64" if current_architecture() != "arm64" else "x86_64"
        manifest["architecture"] = wrong_architecture
        for component in components:
            assert isinstance(component, dict)
            component["architecture"] = wrong_architecture
    _write_manifest(managed_root, manifest)
    with pytest.raises(FunASRRunnerError, match="manifest is invalid"):
        _model_paths(FunASRConfig(model_root=managed_root))


def test_runner_prefers_manifest_owned_models_when_legacy_cache_also_exists(
    tmp_path: Path,
) -> None:
    managed_root = tmp_path / "ambiguous root"
    expected, manifest = _managed_model_fixture(managed_root)
    for relative in (ASR_MODEL, VAD_MODEL, PUNC_MODEL):
        legacy = managed_root / relative
        legacy.mkdir(parents=True)
        (legacy / "model.pt").write_bytes(b"unbound legacy model")
    _write_manifest(managed_root, manifest)
    assert _model_paths(FunASRConfig(model_root=managed_root)) == expected


def test_runner_keeps_external_modelscope_layout_without_managed_manifest(
    tmp_path: Path,
) -> None:
    external_root = tmp_path / "external modelscope"
    expected = {
        "asr": external_root / ASR_MODEL,
        "vad": external_root / VAD_MODEL,
        "punc": external_root / PUNC_MODEL,
    }
    for path in expected.values():
        path.mkdir(parents=True)

    assert _model_paths(FunASRConfig(model_root=external_root)) == expected


def test_runner_keeps_three_explicit_model_paths_highest_priority(tmp_path: Path) -> None:
    explicit = {
        "asr": tmp_path / "explicit/asr",
        "vad": tmp_path / "explicit/vad",
        "punc": tmp_path / "explicit/punc",
    }
    for path in explicit.values():
        path.mkdir(parents=True)
    model_root = tmp_path / "invalid managed root"
    model_root.mkdir()
    (model_root / "component-manifest.json").write_text("not json", encoding="utf-8")

    assert _model_paths(
        FunASRConfig(
            model_root=model_root,
            asr_model_path=explicit["asr"],
            vad_model_path=explicit["vad"],
            punc_model_path=explicit["punc"],
        )
    ) == explicit


def test_runner_uses_isolated_mode_and_removes_host_python_environment(
    tmp_path: Path, monkeypatch
) -> None:
    python_path = tmp_path / "python"
    python_path.write_text("#!/bin/sh\n", encoding="utf-8")
    python_path.chmod(0o755)
    model_root = tmp_path / "models"
    for relative in (ASR_MODEL, VAD_MODEL, PUNC_MODEL):
        (model_root / relative).mkdir(parents=True)
    source = tmp_path / "source.wav"
    source.write_bytes(b"fixture")
    raw_path = tmp_path / "raw" / "run.json"
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "CONDA_PREFIX"):
        monkeypatch.setenv(name, f"host-{name.lower()}")

    captured: dict[str, object] = {}

    def decode(_source: Path, pcm_path: Path) -> PCMDecode:
        captured["pcm_path"] = pcm_path
        _write_pcm(pcm_path)
        return _decode_result()

    def record_process(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["env"] = kwargs["env"]
        raw_path.write_text('[{"text": "原始结果。"}]', encoding="utf-8")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "package_version": "1.3.8",
                    "torch_version": "2.12.0",
                    "torchaudio_version": "2.11.0",
                    "started_at": "2026-07-19T00:00:00Z",
                    "completed_at": "2026-07-19T00:00:01Z",
                }
            ),
            stderr="",
        )

    run = run_funasr(
        source,
        raw_path,
        config=FunASRConfig(python_path=python_path, model_root=model_root),
        process_runner=record_process,
        decoder=decode,
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert command[1] == "-I"
    assert "--pcm-input" in command
    assert str(source) not in command
    pcm_path = captured["pcm_path"]
    assert isinstance(pcm_path, Path)
    assert str(pcm_path) in command
    assert not pcm_path.exists()
    assert not pcm_path.parent.exists()
    worker_env = captured["env"]
    assert isinstance(worker_env, dict)
    assert worker_env["MODELSCOPE_CACHE"] == str(model_root)
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "CONDA_PREFIX"):
        assert name not in worker_env
    assert run.parameters["device"] == "cpu"
    assert run.parameters["input_type"] == "pcm_samples"
    assert run.parameters["decode"] == {
        "ffmpeg_path": "/fixture/ffmpeg",
        "ffmpeg_version": "ffmpeg version fixture",
        "audio_stream": "0:a:0",
        "sample_rate_hz": 16_000,
        "channels": 1,
        "sample_format": "s16le",
        "container_format": "wav",
    }
    assert run.parameters["runtime_versions"] == {
        "funasr": "1.3.8",
        "torch": "2.12.0",
        "torchaudio": "2.11.0",
    }
    assert run.parameters["speaker_diarization"] == {"enabled": False}


def test_runner_enables_only_explicit_local_speaker_model(
    tmp_path: Path, monkeypatch
) -> None:
    python_path = tmp_path / "python"
    python_path.write_text("#!/bin/sh\n", encoding="utf-8")
    python_path.chmod(0o755)
    model_root = tmp_path / "base models"
    for relative in (ASR_MODEL, VAD_MODEL, PUNC_MODEL):
        (model_root / relative).mkdir(parents=True)
    speaker_path = tmp_path / "managed speaker/models/model_spk"
    speaker_path.mkdir(parents=True)
    (speaker_path / "campplus_cn_common.bin").write_bytes(b"speaker")
    source = tmp_path / "source.wav"
    source.write_bytes(b"fixture")
    raw_path = tmp_path / "raw/run.json"
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        runner,
        "_resolve_speaker_model",
        lambda _config: SpeakerModelResolution(
            path=speaker_path,
            source="managed",
            revision="v2.0.2",
        ),
    )

    def decode(_source: Path, pcm_path: Path) -> PCMDecode:
        _write_pcm(pcm_path)
        return _decode_result()

    def record_process(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text('[]\n', encoding="utf-8")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "package_version": "1.3.8",
                    "torch_version": "2.12.0",
                    "torchaudio_version": "2.11.0",
                    "started_at": "2026-07-21T00:00:00Z",
                    "completed_at": "2026-07-21T00:00:01Z",
                }
            ),
            stderr="",
        )

    result = run_funasr(
        source,
        raw_path,
        config=FunASRConfig(
            python_path=python_path,
            model_root=model_root,
            speaker_diarization=True,
            speaker_model_path=speaker_path,
        ),
        process_runner=record_process,
        decoder=decode,
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert command[command.index("--spk-model") + 1] == str(speaker_path)
    assert result.models["spk"] == "model_spk"
    assert result.parameters["speaker_diarization"] == {
        "enabled": True,
        "model": "model_spk",
        "revision": "v2.0.2",
        "source": "managed",
        "spk_mode": "punc_segment",
    }


def _speaker_catalog(monkeypatch, payload: bytes):
    payload_digest = hashlib.sha256(payload).hexdigest()
    directory_digest = hashlib.sha256(b"roughcut-component-directory-v1\0")
    directory_digest.update(b"F\0campplus_cn_common.bin\0")
    directory_digest.update(bytes.fromhex(payload_digest))
    artifact = SimpleNamespace(
        destination="campplus_cn_common.bin",
        size=len(payload),
        sha256=payload_digest,
    )
    model = SimpleNamespace(
        revision="v2.0.2",
        directory_sha256=directory_digest.hexdigest(),
        artifacts=(artifact,),
    )
    profile = SimpleNamespace(models={"model_spk": model})
    catalog = SimpleNamespace(profile_for=lambda _platform, _architecture: profile)
    monkeypatch.setattr(runner, "load_release_catalog", lambda: catalog)
    return model


def test_speaker_resolver_verifies_explicit_external_payload_and_revision(
    tmp_path: Path, monkeypatch
) -> None:
    payload = b"verified speaker payload"
    _speaker_catalog(monkeypatch, payload)
    speaker = tmp_path / "共享 CAM++"
    speaker.mkdir()
    (speaker / "campplus_cn_common.bin").write_bytes(payload)
    (speaker / ".mv").write_text("Revision:v2.0.2,CreatedAt:fixture", encoding="utf-8")
    (speaker / ".msc").write_bytes(b"client metadata")

    resolved = runner._resolve_speaker_model(
        FunASRConfig(speaker_diarization=True, speaker_model_path=speaker.resolve())
    )

    assert resolved == SpeakerModelResolution(
        path=speaker.resolve(), source="external", revision="v2.0.2"
    )


def test_speaker_resolver_uses_manifest_owned_managed_component(
    tmp_path: Path, monkeypatch
) -> None:
    payload = b"managed speaker payload"
    model = _speaker_catalog(monkeypatch, payload)
    managed = tmp_path / "managed speaker"
    speaker = managed / "models/model_spk"
    speaker.mkdir(parents=True)
    (speaker / "campplus_cn_common.bin").write_bytes(payload)
    _write_manifest(
        managed,
        {
            "schema_version": 2,
            "platform": current_platform(),
            "architecture": current_architecture(),
            "managed_root": str(managed.resolve()),
            "components": [
                {
                    "name": "model_spk",
                    "kind": "model",
                    "source_type": "managed",
                    "origin": "https://modelscope.cn/models/iic/speech_campplus_sv_zh-cn_16k-common/files",
                    "version": "v2.0.2",
                    "path": "models/model_spk",
                    "platform": current_platform(),
                    "architecture": current_architecture(),
                    "license": "Apache-2.0",
                    "verification": {
                        "algorithm": "sha256",
                        "value": model.directory_sha256,
                    },
                }
            ],
        },
    )

    resolved = runner._resolve_speaker_model(
        FunASRConfig(model_root=managed, speaker_diarization=True)
    )

    assert resolved.source == "managed"
    assert resolved.path == speaker.resolve()


@pytest.mark.parametrize(
    "mutation", ["revision", "digest", "missing", "symlink", "unexpected"]
)
def test_speaker_resolver_rejects_untrusted_external_payload(
    tmp_path: Path, monkeypatch, mutation: str
) -> None:
    payload = b"verified speaker payload"
    _speaker_catalog(monkeypatch, payload)
    speaker = tmp_path / mutation
    speaker.mkdir()
    primary = speaker / "campplus_cn_common.bin"
    primary.write_bytes(payload)
    (speaker / ".mv").write_text("Revision:v2.0.2", encoding="utf-8")
    if mutation == "revision":
        (speaker / ".mv").write_text("Revision:master", encoding="utf-8")
    elif mutation == "digest":
        primary.write_bytes(b"tampered speaker payload")
    elif mutation == "missing":
        primary.unlink()
    elif mutation == "symlink":
        target = tmp_path / "outside.bin"
        target.write_bytes(payload)
        primary.unlink()
        try:
            primary.symlink_to(target)
        except OSError:
            pytest.skip("symlink creation is unavailable")
    elif mutation == "unexpected":
        (speaker / "unapproved.bin").write_bytes(b"not in the catalog")

    with pytest.raises(FunASRRunnerError, match="speaker model is invalid"):
        runner._resolve_speaker_model(
            FunASRConfig(speaker_diarization=True, speaker_model_path=speaker.resolve())
        )


def test_worker_preserves_raw_json_and_uses_local_models(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    model_root = tmp_path / "models"
    pcm_path = tmp_path / "中文 worker source.wav"
    _write_pcm(pcm_path)
    raw_path = tmp_path / "raw-asr" / "run.json"
    captured: dict[str, object] = {}

    class FakeAutoModel:
        def __init__(self, **kwargs: object) -> None:
            captured["model"] = kwargs

        def generate(self, **kwargs: object) -> list[dict[str, object]]:
            captured["generate"] = kwargs
            return [{"text": "隔离 worker。", "start": 0, "end": 800}]

    fake_funasr = types.ModuleType("funasr")
    fake_funasr.AutoModel = FakeAutoModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "funasr", fake_funasr)

    class FakeSamples:
        dtype = types.SimpleNamespace(name="float32")
        shape = (1600,)

        def __itruediv__(self, _value: float) -> Self:
            return self

    class FakeIntegers:
        def astype(self, _dtype: object) -> FakeSamples:
            return FakeSamples()

    fake_numpy = types.ModuleType("numpy")
    fake_numpy.float32 = object()  # type: ignore[attr-defined]
    fake_numpy.frombuffer = lambda payload, dtype: FakeIntegers()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "numpy", fake_numpy)

    versions = {"funasr": "1.3.8", "torch": "2.12.0", "torchaudio": "2.11.0"}
    monkeypatch.setattr(worker.importlib.metadata, "version", versions.__getitem__)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "worker.py",
            "--pcm-input",
            str(pcm_path),
            "--raw-output",
            str(raw_path),
            "--model-root",
            str(model_root),
        ],
    )
    worker.main()

    summary = json.loads(capsys.readouterr().out)
    assert summary["package_version"] == "1.3.8"
    assert summary["torch_version"] == "2.12.0"
    assert summary["torchaudio_version"] == "2.11.0"
    assert json.loads(raw_path.read_text(encoding="utf-8"))[0]["text"] == "隔离 worker。"
    assert captured["model"] == {
        "model": str(model_root / ASR_MODEL),
        "vad_model": str(model_root / VAD_MODEL),
        "punc_model": str(model_root / PUNC_MODEL),
        "disable_update": True,
        "device": "cpu",
    }
    generate = captured["generate"]
    assert isinstance(generate, dict)
    assert not isinstance(generate["input"], (str, Path))
    assert generate["input"].dtype.name == "float32"  # type: ignore[union-attr]
    assert generate["input"].shape == (1600,)  # type: ignore[union-attr]
    assert generate["fs"] == 16_000
    assert generate["batch_size_s"] == 300
    assert generate["sentence_timestamp"] is True
    assert generate["return_raw_text"] is True


def test_worker_adds_speaker_options_only_when_local_path_is_explicit(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    model_root = tmp_path / "models"
    speaker_path = tmp_path / "speaker model"
    speaker_path.mkdir()
    pcm_path = tmp_path / "source.wav"
    _write_pcm(pcm_path)
    raw_path = tmp_path / "raw/run.json"
    captured: dict[str, object] = {}

    class FakeAutoModel:
        def __init__(self, **kwargs: object) -> None:
            captured["model"] = kwargs

        def generate(self, **kwargs: object) -> list[dict[str, object]]:
            captured["generate"] = kwargs
            return [{"text": "两人对话。", "start": 0, "end": 800, "spk": 0}]

    fake_funasr = types.ModuleType("funasr")
    fake_funasr.AutoModel = FakeAutoModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "funasr", fake_funasr)

    class FakeSamples:
        dtype = types.SimpleNamespace(name="float32")
        shape = (1600,)

        def __itruediv__(self, _value: float) -> Self:
            return self

    class FakeIntegers:
        def astype(self, _dtype: object) -> FakeSamples:
            return FakeSamples()

    fake_numpy = types.ModuleType("numpy")
    fake_numpy.float32 = object()  # type: ignore[attr-defined]
    fake_numpy.frombuffer = lambda payload, dtype: FakeIntegers()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "numpy", fake_numpy)
    versions = {"funasr": "1.3.8", "torch": "2.12.0", "torchaudio": "2.11.0"}
    monkeypatch.setattr(worker.importlib.metadata, "version", versions.__getitem__)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "worker.py",
            "--pcm-input",
            str(pcm_path),
            "--raw-output",
            str(raw_path),
            "--model-root",
            str(model_root),
            "--spk-model",
            str(speaker_path),
        ],
    )

    worker.main()
    capsys.readouterr()

    model_options = captured["model"]
    generate_options = captured["generate"]
    assert isinstance(model_options, dict)
    assert isinstance(generate_options, dict)
    assert model_options["spk_model"] == str(speaker_path)
    assert model_options["spk_mode"] == "punc_segment"
    assert generate_options["return_spk_res"] is True


def test_worker_fails_closed_when_pcm_cannot_be_read(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    pcm_path = tmp_path / "invalid.wav"
    pcm_path.write_bytes(b"not a wave file")
    raw_path = tmp_path / "raw-asr" / "run.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "worker.py",
            "--pcm-input",
            str(pcm_path),
            "--raw-output",
            str(raw_path),
            "--model-root",
            str(tmp_path / "models"),
        ],
    )

    with pytest.raises(SystemExit) as error:
        worker.main()

    assert error.value.code == 1
    assert "FunASR worker failed" in capsys.readouterr().err
    assert not raw_path.exists()


@pytest.mark.parametrize("failure", ["ffmpeg", "funasr", "cancel"])
def test_runner_cleans_temporary_pcm_on_failure_and_cancel(
    tmp_path: Path, failure: str
) -> None:
    python_path = tmp_path / "python"
    python_path.write_text("#!/bin/sh\n", encoding="utf-8")
    python_path.chmod(0o755)
    model_root = tmp_path / "models"
    for relative in (ASR_MODEL, VAD_MODEL, PUNC_MODEL):
        (model_root / relative).mkdir(parents=True)
    source = tmp_path / "source.mp3"
    source.write_bytes(b"fixture")
    raw_path = tmp_path / "raw" / "run.json"
    captured: dict[str, Path] = {}

    def decode(_source: Path, pcm_path: Path) -> PCMDecode:
        captured["pcm_path"] = pcm_path
        _write_pcm(pcm_path)
        if failure == "ffmpeg":
            raise FFmpegAudioError("ffmpeg decode failed")
        return _decode_result()

    def run_worker(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if failure == "cancel":
            raise KeyboardInterrupt
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="worker failed")

    expected_error = KeyboardInterrupt if failure == "cancel" else FunASRRunnerError
    with pytest.raises(expected_error):
        run_funasr(
            source,
            raw_path,
            config=FunASRConfig(python_path=python_path, model_root=model_root),
            process_runner=run_worker,
            decoder=decode,
        )

    pcm_path = captured["pcm_path"]
    assert not pcm_path.exists()
    assert not pcm_path.parent.exists()
