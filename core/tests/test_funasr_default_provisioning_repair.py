"""FunASR default provisioning repair: fixture-based Cases 1-5 + consistency.

Revised per PLAN BLOCK: the 79-line external 1.3.8 freeze is NOT the managed
target. The managed closure stays 82 entries (FunASR 1.3.14 / Torch 2.6.0 /
Torchaudio 2.6.0). These tests use temp dirs/fixtures/mocks only; no fresh
machine, no 2GB download, no real media.
"""

from __future__ import annotations

import sys
from pathlib import Path

from roughcut.adapters.component_environment import (
    MODEL_RUNTIME_FILES,
    ComponentManifest,
    ComponentRecord,
    ComponentVerification,
    PythonRuntimeRecord,
    component_digest,
    diagnose_components,
)
from roughcut.adapters.component_installation import (
    MODEL_COMPONENT_NAMES,
    _validate_runtime_dependency_lock,
    build_install_plan,
    load_release_catalog,
)


def _write_model_dir(base: Path, name: str) -> Path:
    model = base / name
    model.mkdir(parents=True, exist_ok=True)
    for required in MODEL_RUNTIME_FILES[name]:
        (model / required).write_bytes(f"{name}:{required}:fixture".encode())
    return model


def _model_record(name: str, path: Path, platform: str, arch: str) -> ComponentRecord:
    from roughcut.adapters.component_environment import current_architecture, current_platform

    return ComponentRecord(
        name=name,
        kind="model",
        source_type="managed",
        origin="fixture://model",
        version="fixture-revision",
        path=path.as_posix() if path.is_absolute() else str(path),
        platform=platform,
        architecture=arch,
        license="Apache-2.0",
        verification=ComponentVerification("sha256", component_digest(path)),
    )


def test_punc_runtime_table_contains_jieba_and_excludes_non_runtime() -> None:
    assert "jieba_usr_dict" in MODEL_RUNTIME_FILES["model_punc"]
    for name, files in MODEL_RUNTIME_FILES.items():
        assert "README.md" not in files
        assert not any(part.startswith("example") for part in files)
        assert not any(part.startswith("fig") for part in files)
        assert "configuration.json" in files
        assert "config.yaml" in files
    assert "campplus_cn_common.bin" in MODEL_RUNTIME_FILES["model_spk"]
    assert "model.pt" in MODEL_RUNTIME_FILES["model_asr"]


def test_case3_punc_missing_jieba_is_not_complete(tmp_path: Path) -> None:
    from roughcut.adapters.component_environment import current_architecture, current_platform

    platform = current_platform()
    arch = current_architecture()
    model = _write_model_dir(tmp_path / "models", "model_punc")
    (model / "jieba_usr_dict").unlink()
    record = ComponentRecord(
        name="model_punc",
        kind="model",
        source_type="managed",
        origin="fixture://model",
        version="fixture-revision",
        path="models/model_punc",
        platform=platform,
        architecture=arch,
        license="Apache-2.0",
        verification=ComponentVerification("sha256", component_digest(model)),
    )
    manifest = ComponentManifest(
        components=(record,),
        platform=platform,
        architecture=arch,
        managed_root=str((tmp_path).resolve()),
        schema_version=1,
    )
    diagnosis = diagnose_components(
        managed_manifest=manifest,
        managed_root_override=tmp_path,
    )
    assert diagnosis.components["model_punc"].status == "install_required"
    assert "jieba_usr_dict" in (diagnosis.components["model_punc"].attempts[0].detail or "")


def test_case4_full_fixture_is_complete(tmp_path: Path) -> None:
    from roughcut.adapters.component_environment import current_architecture, current_platform

    platform = current_platform()
    arch = current_architecture()
    records = []
    for name in MODEL_COMPONENT_NAMES:
        model = _write_model_dir(tmp_path / "models", name)
        records.append(
            ComponentRecord(
                name=name,
                kind="model",
                source_type="managed",
                origin="fixture://model",
                version="fixture-revision",
                path=f"models/{name}",
                platform=platform,
                architecture=arch,
                license="Apache-2.0",
                verification=ComponentVerification("sha256", component_digest(model)),
            )
        )
    manifest = ComponentManifest(
        components=tuple(records),
        platform=platform,
        architecture=arch,
        managed_root=str(tmp_path.resolve()),
        schema_version=1,
    )
    diagnosis = diagnose_components(
        managed_manifest=manifest,
        managed_root_override=tmp_path,
    )
    for name in MODEL_COMPONENT_NAMES:
        assert diagnosis.components[name].status == "available"


def test_case2_missing_runtime_requires_full_funasr_closure(tmp_path: Path) -> None:
    catalog = load_release_catalog()
    profile = catalog.profile_for("macos", "arm64")
    assert profile.runtime.versions == {
        "funasr": "1.3.14",
        "torch": "2.6.0",
        "torchaudio": "2.6.0",
    }
    plan = build_install_plan(
        tmp_path / "managed",
        tmp_path / "cache",
        catalog=catalog,
        platform="macos",
        architecture="arm64",
        ffmpeg_command="roughcut-missing-ffmpeg",
        ffprobe_command="roughcut-missing-ffprobe",
    )
    missing = plan.payload["missing_managed_groups"]
    assert isinstance(missing, list)
    assert "python_runtime" in missing
    for name in MODEL_COMPONENT_NAMES:
        assert name in missing
    # Never MLX-only sufficient.
    payload_text = str(plan.payload)
    assert "mlx" not in payload_text.lower()
    names = {str(a.get("name", "")) for a in plan.payload["artifacts"]}  # type: ignore[union-attr]
    assert "funasr" in names
    assert "torch" in names
    assert "torchaudio" in names
    destinations = {str(a.get("destination", "")) for a in plan.payload["artifacts"]}  # type: ignore[union-attr]
    assert "jieba_usr_dict" in destinations


def test_case5_lock_equals_provisioning_closure() -> None:
    catalog = load_release_catalog()
    for profile in catalog.profiles:
        if profile.id != "macos-arm64-py311":
            continue
        assert len(profile.runtime.artifacts) == 82
        _validate_runtime_dependency_lock(
            profile.runtime.dependency_lock, profile.runtime.artifacts
        )


def test_provisioning_consistency_default_asr_subset(tmp_path: Path) -> None:
    """DEFAULT_ASR_REQUIREMENTS subset PROVISIONED_RUNTIME on the real seam."""
    catalog = load_release_catalog()
    profile = catalog.profile_for("macos", "arm64")
    default_required = {"python_runtime", *MODEL_COMPONENT_NAMES}
    provisioned = {"python_runtime", *profile.models.keys()}
    assert default_required <= provisioned
    assert profile.runtime.versions["funasr"] == "1.3.14"
    plan = build_install_plan(
        tmp_path / "managed",
        tmp_path / "cache",
        catalog=catalog,
        platform="macos",
        architecture="arm64",
        ffmpeg_command="roughcut-missing-ffmpeg",
        ffprobe_command="roughcut-missing-ffprobe",
    )
    assert set(plan.payload["missing_managed_groups"]) >= default_required  # type: ignore[union-attr]


def test_runtime_lock_rejects_truncated_closure(tmp_path: Path) -> None:
    import pytest

    catalog = load_release_catalog()
    profile = catalog.profile_for("macos", "arm64")
    full = profile.runtime.dependency_lock.read_text(encoding="utf-8")
    # Drop the last distribution entry (keep header comments + all but last block).
    lines = full.splitlines(keepends=True)
    # Find last "name==version \" start and truncate before it.
    starts = [i for i, line in enumerate(lines) if "==" in line and line.strip().endswith("\\")]
    assert len(starts) == 82
    truncated = "".join(lines[: starts[-1]])
    candidate = tmp_path / "truncated.lock"
    candidate.write_text(truncated, encoding="utf-8")
    with pytest.raises(Exception):
        _validate_runtime_dependency_lock(candidate, profile.runtime.artifacts)


def test_python_runtime_probe_names_cover_default_asr() -> None:
    from roughcut.adapters.component_environment import PYTHON_COMPONENT_NAMES

    assert set(PYTHON_COMPONENT_NAMES) == {"funasr", "torch", "torchaudio"}
    assert sys.version_info[:2] >= (3, 11)


def test_code_only_trial_marks_fresh_install_pending_and_local_first(tmp_path: Path) -> None:
    import json
    import subprocess

    from scripts.build_macos_arm64_code_only_trial import (
        INSTALL_GUIDE_NAME,
        KNOWN_ISSUES_NAME,
        MANIFEST_NAME,
        PACKAGING_REPORT_NAME,
        assemble,
    )

    root = Path(__file__).resolve().parents[2]
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    out = tmp_path / "trial"
    assemble(root / "core", out, source_commit=head, require_clean=False)
    manifest = json.loads((out / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["real_user_fresh_install_validation"] == "PENDING"
    known = (out / KNOWN_ISSUES_NAME).read_text(encoding="utf-8")
    report = (out / PACKAGING_REPORT_NAME).read_text(encoding="utf-8")
    guide = (out / INSTALL_GUIDE_NAME).read_text(encoding="utf-8")
    assert "REAL_USER_FRESH_INSTALL_VALIDATION=PENDING" in known
    assert "REAL_USER_FRESH_INSTALL_VALIDATION=PENDING" in report
    # INSTALL-AGENT stays local-first and reflects the FunASR default closure.
    assert "REUSE" in guide
    assert "FunASR 1.3.14" in guide or "FunASR" in guide
    assert "jieba_usr_dict" in guide
    assert "MLX-only" not in guide or "不得以 MLX-only" in guide
    # Healthy approved external 1.3.8 profile must stay REUSE, not forced to reinstall.
    assert "1.3.8" in guide
    assert "REUSE" in guide and "重装" in guide
