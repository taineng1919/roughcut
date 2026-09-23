"""Read-only FunASR package and model-cache diagnostics."""

from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from roughcut.adapters.ffmpeg_environment import CommandDiagnostic, diagnose_command
from roughcut.adapters.funasr.runner import (
    ASR_MODEL,
    PUNC_MODEL,
    SPK_MODEL,
    VAD_MODEL,
    FunASRConfig,
)


@dataclass(frozen=True)
class ModelCacheDiagnostic:
    path: str
    status: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class FunASREnvironment:
    python: CommandDiagnostic
    package_status: str
    package_version: str | None
    model_cache: ModelCacheDiagnostic
    models: dict[str, ModelCacheDiagnostic]

    def to_dict(self) -> dict[str, object]:
        return {
            "python": self.python.to_dict(),
            "package_status": self.package_status,
            "package_version": self.package_version,
            "model_cache": self.model_cache.to_dict(),
            "models": {name: diagnostic.to_dict() for name, diagnostic in self.models.items()},
        }


def diagnose_model_cache(model_cache: Path) -> ModelCacheDiagnostic:
    if not model_cache.exists():
        return ModelCacheDiagnostic(str(model_cache), "missing")
    if not model_cache.is_dir():
        return ModelCacheDiagnostic(str(model_cache), "not_directory")
    return ModelCacheDiagnostic(str(model_cache), "available")


def diagnose_funasr(
    *, python_command: str | None = None, model_cache: Path | None = None
) -> FunASREnvironment:
    defaults = FunASRConfig()
    selected_python = python_command or str(defaults.python_path)
    cache = model_cache or defaults.model_root
    models = {
        "asr": diagnose_model_cache(cache / ASR_MODEL),
        "vad": diagnose_model_cache(cache / VAD_MODEL),
        "punc": diagnose_model_cache(cache / PUNC_MODEL),
        "speaker": diagnose_model_cache(cache / SPK_MODEL),
    }
    python = diagnose_command(selected_python, version_args=("--version",))
    if python.status != "available":
        return FunASREnvironment(
            python, "unavailable", None, diagnose_model_cache(cache), models
        )

    result = subprocess.run(
        [
            selected_python,
            "-c",
            "import importlib.metadata; print(importlib.metadata.version('funasr'))",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return FunASREnvironment(
            python, "package_missing", None, diagnose_model_cache(cache), models
        )
    return FunASREnvironment(
        python,
        "available",
        result.stdout.strip() or None,
        diagnose_model_cache(cache),
        models,
    )
