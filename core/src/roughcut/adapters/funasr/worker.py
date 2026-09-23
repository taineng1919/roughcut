"""Standalone FunASR worker; imports no roughcut modules."""

from __future__ import annotations

import argparse
import contextlib
import importlib.metadata
import io
import json
import os
import sys
import tempfile
import wave
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ASR_MODEL = "models/iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
VAD_MODEL = "models/iic/speech_fsmn_vad_zh-cn-16k-common-pytorch"
PUNC_MODEL = "models/iic/punc_ct-transformer_cn-en-common-vocab471067-large"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pcm-input", type=Path, required=True)
    parser.add_argument("--raw-output", type=Path, required=True)
    parser.add_argument("--model-root", type=Path, required=True)
    parser.add_argument("--asr-model", type=Path)
    parser.add_argument("--vad-model", type=Path)
    parser.add_argument("--punc-model", type=Path)
    parser.add_argument("--spk-model", type=Path)
    args = parser.parse_args()
    started_at = datetime.now(UTC).isoformat()
    logs = io.StringIO()
    try:
        with (
            contextlib.redirect_stdout(logs),
            contextlib.redirect_stderr(logs),
            _suppress_native_output(),
        ):
            from funasr import AutoModel  # type: ignore[import-not-found]

            samples = _read_pcm_samples(args.pcm_input)
            model_options: dict[str, object] = {
                "model": str(args.asr_model or args.model_root / ASR_MODEL),
                "vad_model": str(args.vad_model or args.model_root / VAD_MODEL),
                "punc_model": str(args.punc_model or args.model_root / PUNC_MODEL),
                "disable_update": True,
                "device": "cpu",
            }
            if args.spk_model is not None:
                if not args.spk_model.is_absolute():
                    raise ValueError("speaker model path must be absolute")
                model_options.update(
                    spk_model=str(args.spk_model),
                    spk_mode="punc_segment",
                )
            model = AutoModel(**model_options)
            generate_options: dict[str, object] = {
                "input": samples,
                "fs": 16_000,
                "batch_size_s": 300,
                "sentence_timestamp": True,
                "return_raw_text": True,
            }
            if args.spk_model is not None:
                generate_options["return_spk_res"] = True
            result = model.generate(**generate_options)
        _write_raw_json(args.raw_output, result)
        summary = {
            "package_version": importlib.metadata.version("funasr"),
            "torch_version": importlib.metadata.version("torch"),
            "torchaudio_version": importlib.metadata.version("torchaudio"),
            "started_at": started_at,
            "completed_at": datetime.now(UTC).isoformat(),
        }
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    except Exception as error:
        print(f"FunASR worker failed: {error.__class__.__name__}", file=sys.stderr)
        raise SystemExit(1) from error


@contextmanager
def _suppress_native_output() -> Iterator[None]:
    """Keep native dependency diagnostics off the worker's JSON stdout."""

    sys.stdout.flush()
    sys.stderr.flush()
    saved_stdout: int | None = None
    saved_stderr: int | None = None
    try:
        saved_stdout = os.dup(1)
        saved_stderr = os.dup(2)
        with open(os.devnull, "wb") as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
            yield
    finally:
        restore_errors: list[BaseException] = []
        for target, saved in ((1, saved_stdout), (2, saved_stderr)):
            if saved is None:
                continue
            try:
                os.dup2(saved, target)
            except OSError as error:
                restore_errors.append(error)
            finally:
                os.close(saved)
        if restore_errors:
            raise restore_errors[0]


def _read_pcm_samples(path: Path) -> Any:
    with wave.open(str(path), "rb") as pcm:
        if (
            pcm.getframerate() != 16_000
            or pcm.getnchannels() != 1
            or pcm.getsampwidth() != 2
            or pcm.getcomptype() != "NONE"
            or pcm.getnframes() <= 0
        ):
            raise ValueError("PCM input does not match the worker contract")
        frame_count = pcm.getnframes()
        payload = pcm.readframes(frame_count)
    if len(payload) != frame_count * 2:
        raise ValueError("PCM input is truncated")
    import numpy as np  # type: ignore[import-not-found]

    samples = np.frombuffer(payload, dtype="<i2").astype(np.float32)
    samples /= 32768.0
    return samples


def _write_raw_json(path: Path, result: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(result, temporary_file, ensure_ascii=False, indent=2)
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
