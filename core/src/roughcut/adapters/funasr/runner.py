"""Run FunASR in its isolated Python environment."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from roughcut.adapters.component_environment import (
    COMPONENT_MANIFEST_FILENAME,
    ComponentError,
    component_record_digest,
    current_architecture,
    current_platform,
    load_component_manifest,
)
from roughcut.adapters.component_installation import (
    ComponentInstallError,
    load_release_catalog,
)
from roughcut.adapters.ffmpeg.audio import FFmpegAudioError, PCMDecode, decode_audio_to_pcm
from roughcut.adapters.media_operation_store import media_child_process_kwargs
from roughcut.adapters.runtime_binding import (
    RuntimeBindingError,
    resolve_funasr_selection,
)

ASR_MODEL = "models/iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
VAD_MODEL = "models/iic/speech_fsmn_vad_zh-cn-16k-common-pytorch"
PUNC_MODEL = "models/iic/punc_ct-transformer_cn-en-common-vocab471067-large"
SPK_MODEL = "models/iic/speech_campplus_sv_zh-cn_16k-common"
SPK_COMPONENT = "model_spk"
SPK_MANAGED_PATH = "models/model_spk"
SPK_PRIMARY_PAYLOAD = "campplus_cn_common.bin"
MANAGED_MODELS = {
    "asr": "models/model_asr",
    "vad": "models/model_vad",
    "punc": "models/model_punc",
}
MANAGED_MODEL_RECORDS = {
    "asr": "model_asr",
    "vad": "model_vad",
    "punc": "model_punc",
}
VIRTUAL_ENVIRONMENT_VARIABLES = {
    "CONDA_DEFAULT_ENV",
    "CONDA_PREFIX",
    "VIRTUAL_ENV",
    "_OLD_VIRTUAL_PATH",
    "__PYVENV_LAUNCHER__",
}


def default_funasr_python(*, platform: str | None = None) -> Path:
    configured = os.environ.get("ROUGHCUT_FUNASR_PYTHON")
    if configured:
        return Path(configured)
    runtime = Path.home() / ".roughcut" / "toolchains" / "funasr" / "venv"
    if (platform or os.name) == "nt":
        return runtime / "Scripts" / "python.exe"
    return runtime / "bin" / "python"


def default_model_root() -> Path:
    configured = os.environ.get("ROUGHCUT_FUNASR_MODEL_ROOT")
    if configured:
        return Path(configured)
    return Path.home() / ".roughcut" / "models" / "funasr"


class FunASRRunnerError(RuntimeError):
    """Raised when the isolated FunASR worker cannot produce raw JSON."""


@dataclass(frozen=True)
class FunASRConfig:
    python_path: Path = field(default_factory=default_funasr_python)
    model_root: Path = field(default_factory=default_model_root)
    asr_model_path: Path | None = None
    vad_model_path: Path | None = None
    punc_model_path: Path | None = None
    speaker_diarization: bool = False
    speaker_model_path: Path | None = None
    ffmpeg_command: str = "ffmpeg"
    ffmpeg_version: str | None = None
    timeout_seconds: float = 3600.0


@dataclass(frozen=True)
class FunASRRun:
    package_version: str
    models: dict[str, str]
    parameters: dict[str, object]
    started_at: str
    completed_at: str
    exit_status: int


@dataclass(frozen=True)
class SpeakerModelResolution:
    path: Path
    source: str
    revision: str


ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]
AudioDecoder = Callable[[Path, Path], PCMDecode]


def run_funasr(
    source_path: Path,
    raw_output_path: Path,
    *,
    config: FunASRConfig | None = None,
    process_runner: ProcessRunner = subprocess.run,
    decoder: AudioDecoder | None = None,
    phase_callback: Callable[[str], None] | None = None,
) -> FunASRRun:
    try:
        selected = config or configured_funasr_config()
    except RuntimeBindingError as error:
        raise FunASRRunnerError(str(error)) from error
    if not selected.python_path.is_file() or not os.access(selected.python_path, os.X_OK):
        raise FunASRRunnerError("FunASR Python is missing or not executable")
    model_paths = _model_paths(selected)
    for model_path in model_paths.values():
        if not model_path.is_dir():
            raise FunASRRunnerError("a required FunASR model directory is missing")
    speaker = _resolve_speaker_model(selected) if selected.speaker_diarization else None

    worker_environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("PYTHON") and name not in VIRTUAL_ENVIRONMENT_VARIABLES
    }
    worker_environment["MODELSCOPE_CACHE"] = str(selected.model_root)
    raw_output_path.parent.mkdir(parents=True, exist_ok=True)
    worker_path = Path(__file__).with_name("worker.py")
    selected_decoder = decoder or (
        lambda media, pcm: decode_audio_to_pcm(
            media,
            pcm,
            ffmpeg_command=selected.ffmpeg_command,
            ffmpeg_version=selected.ffmpeg_version,
            timeout_seconds=selected.timeout_seconds,
        )
    )
    with tempfile.TemporaryDirectory(prefix="roughcut-pcm-") as temporary_directory:
        pcm_path = Path(temporary_directory) / "audio.wav"
        try:
            if phase_callback is not None:
                phase_callback("transcription_decoding_audio")
            decode = selected_decoder(source_path, pcm_path)
        except FFmpegAudioError as error:
            raise FunASRRunnerError("FFmpeg could not prepare FunASR PCM") from error
        command = [
            str(selected.python_path),
            "-I",
            str(worker_path),
            "--pcm-input",
            str(pcm_path),
            "--raw-output",
            str(raw_output_path),
            "--model-root",
            str(selected.model_root),
            "--asr-model",
            str(model_paths["asr"]),
            "--vad-model",
            str(model_paths["vad"]),
            "--punc-model",
            str(model_paths["punc"]),
        ]
        if speaker is not None:
            command.extend(["--spk-model", str(speaker.path)])
        try:
            if phase_callback is not None:
                phase_callback("transcription_running_asr")
            result = process_runner(
                command,
                check=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=selected.timeout_seconds,
                env=worker_environment,
                **media_child_process_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise FunASRRunnerError("FunASR worker could not complete") from error
    if result.returncode != 0:
        raise FunASRRunnerError(f"FunASR worker exited with status {result.returncode}")
    try:
        summary: Any = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise FunASRRunnerError("FunASR worker returned invalid summary JSON") from error
    if not isinstance(summary, dict) or not raw_output_path.is_file():
        raise FunASRRunnerError("FunASR worker did not preserve raw JSON")
    try:
        json.loads(raw_output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FunASRRunnerError("FunASR worker raw JSON is unreadable") from error

    package_version = _summary_string(summary, "package_version")
    models = {name: path.name for name, path in model_paths.items()}
    speaker_parameters: dict[str, object] = {"enabled": False}
    if speaker is not None:
        models["spk"] = SPK_COMPONENT
        speaker_parameters = {
            "enabled": True,
            "model": SPK_COMPONENT,
            "revision": speaker.revision,
            "source": speaker.source,
            "spk_mode": "punc_segment",
        }
    return FunASRRun(
        package_version=package_version,
        models=models,
        parameters={
            "batch_size_s": 300,
            "sentence_timestamp": True,
            "return_raw_text": True,
            "device": "cpu",
            "input_type": "pcm_samples",
            "speaker_diarization": speaker_parameters,
            "decode": decode.to_dict(),
            "runtime_versions": {
                "funasr": package_version,
                "torch": _summary_string(summary, "torch_version"),
                "torchaudio": _summary_string(summary, "torchaudio_version"),
            },
        },
        started_at=_summary_string(summary, "started_at"),
        completed_at=_summary_string(summary, "completed_at"),
        exit_status=result.returncode,
    )


def configured_funasr_config(
    *,
    python_path: Path | None = None,
    model_root: Path | None = None,
    speaker_diarization: bool = False,
    speaker_model_path: Path | None = None,
) -> FunASRConfig:
    selected = resolve_funasr_selection(
        explicit_python=python_path,
        explicit_model_root=model_root,
        explicit_speaker_model=speaker_model_path,
    )
    model_paths = selected.model_paths or {}
    return FunASRConfig(
        python_path=selected.python_path,
        model_root=selected.model_root,
        asr_model_path=model_paths.get("asr"),
        vad_model_path=model_paths.get("vad"),
        punc_model_path=model_paths.get("punc"),
        speaker_diarization=speaker_diarization,
        speaker_model_path=(
            speaker_model_path or selected.speaker_model_path
            if speaker_diarization
            else None
        ),
        ffmpeg_command=selected.ffmpeg_command,
    )


def _model_paths(config: FunASRConfig) -> dict[str, Path]:
    explicit = {
        "asr": config.asr_model_path,
        "vad": config.vad_model_path,
        "punc": config.punc_model_path,
    }
    if all(path is not None for path in explicit.values()):
        return {name: path for name, path in explicit.items() if path is not None}

    modelscope = {
        "asr": config.model_root / ASR_MODEL,
        "vad": config.model_root / VAD_MODEL,
        "punc": config.model_root / PUNC_MODEL,
    }
    manifest_path = config.model_root / COMPONENT_MANIFEST_FILENAME
    defaults = (
        _managed_model_paths(config.model_root)
        if os.path.lexists(manifest_path)
        else modelscope
    )
    return {name: explicit[name] or defaults[name] for name in defaults}


def _managed_model_paths(model_root: Path) -> dict[str, Path]:
    try:
        manifest = load_component_manifest(model_root / COMPONENT_MANIFEST_FILENAME)
        resolved_root = model_root.resolve(strict=False)
        if (
            manifest.managed_root is None
            or Path(manifest.managed_root).resolve(strict=False) != resolved_root
        ):
            raise ComponentError("component manifest belongs to a different managed root")
        target_platform = current_platform()
        target_architecture = current_architecture()
        if manifest.platform != target_platform:
            raise ComponentError("component manifest platform does not match")
        if manifest.architecture != target_architecture:
            raise ComponentError("component manifest architecture does not match")

        expected_locks = _pinned_model_locks()
        records = {record.name: record for record in manifest.components}
        resolved: dict[str, Path] = {}
        for name, record_name in MANAGED_MODEL_RECORDS.items():
            record = records.get(record_name)
            if record is None:
                raise ComponentError(f"managed model record {record_name} is missing")
            if record.source_type != "managed":
                raise ComponentError(f"managed model record {record_name} has wrong source_type")
            expected_path = MANAGED_MODELS[name]
            if record.path != expected_path:
                raise ComponentError(f"managed model record {record_name} path is not canonical")
            expected_revision, expected_directory_sha256 = expected_locks[record_name]
            if record.version != expected_revision:
                raise ComponentError(f"managed model record {record_name} revision does not match")
            if record.verification.algorithm != "sha256":
                raise ComponentError(
                    f"managed model record {record_name} verification algorithm does not match"
                )
            if record.verification.value != expected_directory_sha256:
                raise ComponentError(
                    f"managed model record {record_name} verification digest does not match"
                )
            model_path = model_root / expected_path
            model_payload = model_path / "model.pt"
            if (
                not model_path.is_dir()
                or model_path.is_symlink()
                or not model_payload.is_file()
                or model_payload.is_symlink()
            ):
                raise ComponentError(
                    f"managed model record {record_name} payload is missing or unsafe"
                )
            resolved[name] = model_path
        return resolved
    except (ComponentError, ComponentInstallError, OSError) as error:
        raise FunASRRunnerError(
            f"managed FunASR model manifest is invalid: {error}"
        ) from error


def _pinned_model_locks() -> dict[str, tuple[str, str]]:
    profile = load_release_catalog().profile_for(
        current_platform(), current_architecture()
    )
    return {
        record_name: (
            profile.models[record_name].revision,
            profile.models[record_name].directory_sha256,
        )
        for record_name in MANAGED_MODEL_RECORDS.values()
    }


def _resolve_speaker_model(config: FunASRConfig) -> SpeakerModelResolution:
    explicit = config.speaker_model_path
    if explicit is not None and not explicit.is_absolute():
        raise FunASRRunnerError("speaker model path must be absolute")
    manifest_path = config.model_root / COMPONENT_MANIFEST_FILENAME
    if explicit is None:
        if os.path.lexists(manifest_path):
            path = config.model_root / SPK_MANAGED_PATH
            source = "managed"
        else:
            path = config.model_root / SPK_MODEL
            source = "external"
    else:
        path = explicit
        source = "managed" if _is_manifest_owned_speaker(path) else "external"

    try:
        profile = load_release_catalog().profile_for(
            current_platform(), current_architecture()
        )
        model = profile.models[SPK_COMPONENT]
        if source == "managed":
            _validate_managed_speaker(path, model.revision, model.directory_sha256)
        _verify_speaker_payload(path, model)
    except (ComponentError, ComponentInstallError, OSError) as error:
        raise FunASRRunnerError(f"speaker model is invalid: {error}") from error
    return SpeakerModelResolution(path=path.resolve(strict=True), source=source, revision=model.revision)


def _is_manifest_owned_speaker(path: Path) -> bool:
    if path.name != SPK_COMPONENT or path.parent.name != "models":
        return False
    root = path.parent.parent
    return os.path.lexists(root / COMPONENT_MANIFEST_FILENAME)


def _validate_managed_speaker(path: Path, revision: str, digest: str) -> None:
    if path.name != SPK_COMPONENT or path.parent.name != "models":
        raise ComponentError("managed speaker model path is not canonical")
    root = path.parent.parent.resolve(strict=False)
    manifest = load_component_manifest(root / COMPONENT_MANIFEST_FILENAME)
    if manifest.managed_root is None or Path(manifest.managed_root).resolve(strict=False) != root:
        raise ComponentError("component manifest belongs to a different managed root")
    if manifest.platform != current_platform() or manifest.architecture != current_architecture():
        raise ComponentError("managed speaker model target does not match")
    records = {record.name: record for record in manifest.components}
    record = records.get(SPK_COMPONENT)
    if record is None or record.source_type != "managed":
        raise ComponentError("managed speaker model record is missing")
    if record.path != SPK_MANAGED_PATH:
        raise ComponentError("managed speaker model path is not canonical")
    if record.version != revision:
        raise ComponentError("managed speaker model revision does not match")
    if record.verification.value != digest:
        raise ComponentError("managed speaker model verification digest does not match")


def _verify_speaker_payload(path: Path, model: Any) -> None:
    if not path.is_dir() or path.is_symlink():
        raise ComponentError("speaker model directory is missing or unsafe")
    primary = path / SPK_PRIMARY_PAYLOAD
    if not primary.is_file() or primary.is_symlink():
        raise ComponentError("speaker model primary payload is missing or unsafe")
    metadata = path / ".mv"
    if metadata.exists() and (
        metadata.is_symlink()
        or f"Revision:{model.revision}" not in metadata.read_text(encoding="utf-8")
    ):
        raise ComponentError("speaker model revision metadata does not match")
    for artifact in model.artifacts:
        if artifact.destination is None:
            raise ComponentError("speaker model catalog destination is missing")
        target = path / artifact.destination
        if not target.is_file() or target.is_symlink():
            raise ComponentError("speaker model payload is missing or unsafe")
        if target.stat().st_size != artifact.size or _file_sha256(target) != artifact.sha256:
            raise ComponentError("speaker model payload checksum differs")
    if component_record_digest(SPK_COMPONENT, path) != model.directory_sha256:
        raise ComponentError("speaker model directory checksum differs")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _summary_string(summary: dict[str, Any], key: str) -> str:
    value = summary.get(key)
    if not isinstance(value, str) or not value:
        raise FunASRRunnerError("FunASR worker summary is incomplete")
    return value
