"""Run the release-only, fixture-based FunASR availability smoke check."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import tempfile
import wave
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CORE_SOURCE = ROOT / "core" / "src"
if str(CORE_SOURCE) not in sys.path:
    sys.path.insert(0, str(CORE_SOURCE))

from roughcut.adapters.funasr.runner import (  # noqa: E402
    FunASRConfig,
    FunASRRun,
    FunASRRunnerError,
    run_funasr,
)
from roughcut.adapters.runtime_binding import (  # noqa: E402
    RuntimeBindingError,
    load_runtime_binding,
)


FIXTURE_SAMPLE_RATE = 16_000
FIXTURE_FRAME_COUNT = 4_000
SMOKE_TIMEOUT_SECONDS = 300.0
OFFLINE_ENVIRONMENT = {
    "MODELSCOPE_OFFLINE": "1",
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
}


class ReleaseASRSmokeError(RuntimeError):
    """Raised when the release smoke result cannot be trusted."""


Runner = Callable[..., FunASRRun]


def run_smoke(runtime_binding_path: Path, *, runner: Runner = run_funasr) -> dict[str, object]:
    """Run FunASR against generated PCM and return a path-free JSON result."""

    binding = load_runtime_binding(runtime_binding_path)
    temporary_path: Path
    with tempfile.TemporaryDirectory(prefix="roughcut-release-asr-") as directory:
        temporary_path = Path(directory)
        model_cache = temporary_path / "model-cache"
        model_cache.mkdir()
        fixture_path = temporary_path / "fixture.wav"
        raw_output_path = temporary_path / "raw.json"
        _write_fixture(fixture_path)

        selected_config = FunASRConfig(
            python_path=Path(binding.python.interpreter),
            model_root=model_cache,
            asr_model_path=Path(binding.components["asr"].path),
            vad_model_path=Path(binding.components["vad"].path),
            punc_model_path=Path(binding.components["punc"].path),
            ffmpeg_command=binding.ffmpeg.command,
            ffmpeg_version=binding.ffmpeg.version,
            timeout_seconds=SMOKE_TIMEOUT_SECONDS,
        )
        completed = runner(
            fixture_path,
            raw_output_path,
            config=selected_config,
            process_runner=_run_worker_offline,
        )
        try:
            raw_result = json.loads(raw_output_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ReleaseASRSmokeError("FunASR raw output is not valid JSON") from error
        if not isinstance(raw_result, (dict, list)):
            raise ReleaseASRSmokeError("FunASR raw output JSON has an unexpected shape")

    if temporary_path.exists():
        raise ReleaseASRSmokeError("release ASR smoke temporary workspace was not removed")
    return {
        "schema_version": 1,
        "kind": "roughcut_release_asr_smoke",
        "ok": True,
        "runtime_binding": "persistent",
        "fixture": "generated_pcm_wav",
        "speaker_diarization": False,
        "raw_result": "valid_json",
        "funasr_version": completed.package_version,
        "worker_exit_status": completed.exit_status,
        "temporary_workspace_removed": True,
    }


def _write_fixture(path: Path) -> None:
    """Write a deterministic short tone; no user media or text oracle is used."""

    samples = bytearray()
    for index in range(FIXTURE_FRAME_COUNT):
        value = int(512 * math.sin(2 * math.pi * 440 * index / FIXTURE_SAMPLE_RATE))
        samples.extend(value.to_bytes(2, byteorder="little", signed=True))
    with wave.open(str(path), "wb") as fixture:
        fixture.setnchannels(1)
        fixture.setsampwidth(2)
        fixture.setframerate(FIXTURE_SAMPLE_RATE)
        fixture.writeframes(bytes(samples))


def _run_worker_offline(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    environment = dict(kwargs.get("env") or {})
    environment.update(OFFLINE_ENVIRONMENT)
    kwargs["env"] = environment
    return subprocess.run(command, **kwargs)


def _failure(error: BaseException) -> dict[str, object]:
    if isinstance(error, RuntimeBindingError):
        responsibility = "runtime_binding"
        action = "load_persistent_binding"
        message = "persistent runtime binding could not be loaded or validated"
    elif isinstance(error, FunASRRunnerError):
        responsibility = "asr_runtime"
        action = "run_local_funasr_worker"
        message = "installed FunASR runtime/model or local FFmpeg decode failed"
    else:
        responsibility = "release_asr_smoke"
        action = "validate_generated_fixture_result"
        message = "generated fixture smoke result could not be trusted"
    return {
        "schema_version": 1,
        "kind": "roughcut_release_asr_smoke",
        "ok": False,
        "error": {
            "code": "release_asr_smoke_failed",
            "responsibility": responsibility,
            "action": action,
            "message": message,
            "detail_type": type(error).__name__,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify the installed FunASR runtime with a generated local fixture."
    )
    parser.add_argument("--runtime-binding", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run_smoke(args.runtime_binding)
    except Exception as error:
        result = _failure(error)
        exit_code = 2
    else:
        exit_code = 0
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
