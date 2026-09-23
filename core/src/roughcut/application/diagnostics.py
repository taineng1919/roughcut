"""Application-level environment diagnostic payload."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from roughcut.adapters.audalign import (
    AudalignProviderMismatchError,
    AudalignProviderMissingError,
    validate_audalign_selection,
)
from roughcut.adapters.ffmpeg_environment import diagnose_command, diagnose_ffmpeg
from roughcut.adapters.funasr.runner import ASR_MODEL, PUNC_MODEL, SPK_MODEL, VAD_MODEL
from roughcut.adapters.runtime_binding import (
    AUDALIGN_PROVIDER,
    BBC_AUDIO_OFFSET_FINDER_PROVIDER,
    RuntimeAlignmentPython,
    RuntimeBinding,
    RuntimeBindingError,
    configured_runtime_path,
    load_runtime_binding,
    resolve_runtime_tool,
)
from roughcut.application.health import health


def diagnostics(*, runtime_path: Path | None = None) -> dict[str, object]:
    result = health()
    selected_path = configured_runtime_path(runtime_path)
    binding: RuntimeBinding | None = None
    binding_error: RuntimeBindingError | None = None
    try:
        binding = load_runtime_binding(selected_path)
    except RuntimeBindingError as error:
        binding_error = error
        status = (
            "unconfigured"
            if str(error) == "Roughcut runtime binding 未配置"
            else "invalid"
        )
        message = (
            "Roughcut runtime binding 未配置"
            if status == "unconfigured"
            else f"Roughcut CLI 未能读取持久运行时绑定: {error}"
        )
        result["runtime_binding"] = {
            "path": str(selected_path),
            "configured": False,
            "status": status,
            "source": "unconfigured",
            "message": message,
            "next_action": "component_plan",
        }
        result["ffmpeg"] = _unconfigured_environment()
        result["funasr"] = _unconfigured_environment()
    else:
        result["runtime_binding"] = {
            "path": str(selected_path),
            "configured": True,
            "status": "configured",
            "source": binding.source,
            "profile": binding.profile,
            "verification_mode": binding.verification_mode,
            "plan_hash": binding.evidence.plan_hash,
        }
        result["ffmpeg"] = _ffmpeg_payload(binding, runtime_path=selected_path)
        result["funasr"] = _funasr_payload(binding)
    result["alignment"] = _alignment_payload(
        selected_path=selected_path,
        binding=binding,
        binding_error=binding_error,
    )
    cli_name = "roughcut.exe" if os.name == "nt" else "roughcut"
    cli_path = Path(sys.executable).with_name(cli_name).absolute()
    result["launchers"] = {
        "cli": str(cli_path),
        "cli_status": (
            "available"
            if cli_path.is_file() and os.access(cli_path, os.X_OK)
            else "missing"
        ),
    }
    return result


def _alignment_payload(
    *,
    selected_path: Path,
    binding: RuntimeBinding | None,
    binding_error: RuntimeBindingError | None,
) -> dict[str, object]:
    """Report production Audalign correlation readiness from the normalized runtime binding."""

    normalized = binding
    if normalized is None:
        try:
            normalized = load_runtime_binding(
                selected_path,
                validate_filesystem=False,
            )
        except RuntimeBindingError:
            status = (
                "missing"
                if binding_error
                and str(binding_error) == "Roughcut runtime binding 未配置"
                else "invalid"
            )
            return _alignment_result(selection=None, status=status)

    selection = normalized.alignment_python
    if selection is None:
        return _alignment_result(selection=None, status="missing")
    if selection.provider == BBC_AUDIO_OFFSET_FINDER_PROVIDER:
        return _alignment_result(selection=selection, status="provider_mismatch")
    if selection.provider != AUDALIGN_PROVIDER:
        return _alignment_result(selection=selection, status="invalid")

    try:
        # reuse the same closed validation that the managed runtime enforces
        validate_audalign_selection(selection)
    except (AudalignProviderMismatchError, AudalignProviderMissingError, RuntimeBindingError):
        status = "invalid"
    else:
        status = "available" if binding_error is None else "invalid"
    return _alignment_result(selection=selection, status=status)


def _alignment_result(
    *,
    selection: RuntimeAlignmentPython | None,
    status: str,
) -> dict[str, object]:
    if status not in {"available", "missing", "provider_mismatch", "invalid"}:
        raise ValueError("alignment diagnostic status is not closed")
    return {
        "required_provider": AUDALIGN_PROVIDER,
        "required_provider_version": "1.3.1",
        "configured_provider": selection.provider if selection else None,
        "configured_provider_version": selection.provider_version if selection else None,
        "interpreter": selection.interpreter if selection else None,
        "status": status,
        "production_ready": status == "available",
    }


def _unconfigured_environment() -> dict[str, object]:
    return {
        "status": "unconfigured",
        "source": "unconfigured",
        "message": "Roughcut runtime binding 未配置",
        "next_action": "component_plan",
    }


def _ffmpeg_payload(
    binding: RuntimeBinding, *, runtime_path: Path
) -> dict[str, object]:
    ffmpeg = resolve_runtime_tool("ffmpeg", runtime_path=runtime_path)
    ffprobe = resolve_runtime_tool("ffprobe", runtime_path=runtime_path)
    environment = diagnose_ffmpeg(
        ffmpeg_command=ffmpeg.command,
        ffprobe_command=ffprobe.command,
    )
    ffmpeg_payload = environment.ffmpeg.to_dict()
    ffprobe_payload = environment.ffprobe.to_dict()
    ffmpeg_payload["source"] = ffmpeg.source
    ffprobe_payload["source"] = ffprobe.source
    return {
        "status": (
            "available"
            if ffmpeg_payload["status"] == ffprobe_payload["status"] == "available"
            else "incomplete"
        ),
        "source": (
            "explicit_override"
            if "explicit_override" in {ffmpeg.source, ffprobe.source}
            else "persistent_external"
        ),
        "ffmpeg": ffmpeg_payload,
        "ffprobe": ffprobe_payload,
    }


def _funasr_payload(binding: RuntimeBinding) -> dict[str, object]:
    python_override = os.environ.get("ROUGHCUT_FUNASR_PYTHON")
    model_override = os.environ.get("ROUGHCUT_FUNASR_MODEL_ROOT")
    if python_override or model_override:
        python_payload = (
            {
                **diagnose_command(
                    python_override, version_args=("--version",)
                ).to_dict(),
                "source": "explicit_override",
            }
            if python_override
            else {
                "command": binding.python.interpreter,
                "status": "available",
                "version": binding.python.versions["funasr"],
                "detail": None,
                "source": (
                    "persistent_external"
                    if binding.python.source_type == "external"
                    else "persistent_managed"
                ),
            }
        )
        models_payload: dict[str, object]
        if model_override:
            root = Path(model_override)
            model_paths = {
                "asr": root / ASR_MODEL,
                "vad": root / VAD_MODEL,
                "punc": root / PUNC_MODEL,
                "campp": root / SPK_MODEL,
            }
            models_payload = {
                key: {
                    "path": str(path),
                    "status": "available" if path.is_dir() else "missing",
                    "version": None,
                    "source": "explicit_override",
                }
                for key, path in model_paths.items()
            }
        else:
            models_payload = _binding_model_payload(binding)
        return {
            "status": "override_selected",
            "source": "explicit_override",
            "python": python_payload,
            "package_status": (
                "override_unverified" if python_override else "available"
            ),
            "package_version": (
                None if python_override else binding.python.versions["funasr"]
            ),
            "models": models_payload,
        }
    source = (
        "persistent_external"
        if binding.python.source_type == "external"
        and all(
            component.source_type == "external"
            for component in binding.components.values()
        )
        else "persistent_managed"
    )
    return {
        "status": "available",
        "source": source,
        "python": {
            "command": binding.python.interpreter,
            "status": "available",
            "version": binding.python.versions["funasr"],
            "detail": None,
            "source": (
                "persistent_external"
                if binding.python.source_type == "external"
                else "persistent_managed"
            ),
        },
        "package_status": "available",
        "package_version": binding.python.versions["funasr"],
        "models": _binding_model_payload(binding),
    }


def _binding_model_payload(binding: RuntimeBinding) -> dict[str, object]:
    return {
        key: {
            "path": component.path,
            "status": "available",
            "version": component.version,
            "source": (
                "persistent_external"
                if component.source_type == "external"
                else "persistent_managed"
            ),
        }
        for key, component in binding.components.items()
    }
