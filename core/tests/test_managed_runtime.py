from __future__ import annotations

import json
import shutil
import sys
import venv
from dataclasses import replace
from pathlib import Path

import pytest

from roughcut.adapters.component_environment import (
    COMPONENT_MANIFEST_FILENAME,
    ComponentError,
    ComponentManifest,
    ComponentRecord,
    ComponentVerification,
    PythonRuntimeRecord,
    component_digest,
    current_architecture,
    current_platform,
    diagnose_components,
    load_component_manifest,
    uninstall_managed_components,
    verify_components,
    write_component_manifest,
)

PYTHON_VERSIONS = {
    "funasr": "1.3.14",
    "torch": "2.6.0",
    "torchaudio": "2.6.0",
}

TEST_PLATFORM = current_platform()
TEST_ARCHITECTURE = current_architecture()
RUNTIME_INTERPRETER = (
    "venv/Scripts/python.exe" if TEST_PLATFORM == "windows" else "venv/bin/python"
)
RUNTIME_LOCK = (
    "locks/windows-x64-py311.lock"
    if TEST_PLATFORM == "windows"
    else "locks/macos-arm64-py311.lock"
)


def _write_runtime_packages(
    root: Path,
    versions: dict[str, str],
    *,
    cuda_version: str | None = None,
) -> None:
    site_packages = (
        root / "venv/Lib/site-packages"
        if TEST_PLATFORM == "windows"
        else root / f"venv/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    )
    site_packages.mkdir(parents=True, exist_ok=True)
    for name, version in versions.items():
        package = site_packages / name
        package.mkdir(parents=True, exist_ok=True)
        if name == "torch":
            package_source = (
                f"class _Version:\n    cuda = {cuda_version!r}\n"
                "version = _Version()\n"
                "class _Cuda:\n"
                "    @staticmethod\n"
                "    def is_available():\n"
                "        return False\n"
                "cuda = _Cuda()\n"
            )
        else:
            package_source = "__all__ = []\n"
        (package / "__init__.py").write_text(package_source, encoding="utf-8")
        for old_metadata in site_packages.glob(f"{name}-*.dist-info"):
            shutil.rmtree(old_metadata)
        metadata = site_packages / f"{name}-{version}.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
            encoding="utf-8",
        )


def _write_managed_runtime(root: Path) -> ComponentManifest:
    venv.EnvBuilder(with_pip=False, symlinks=TEST_PLATFORM != "windows").create(
        root / "venv"
    )
    _write_runtime_packages(root, PYTHON_VERSIONS)
    lock = root / RUNTIME_LOCK
    lock.parent.mkdir(parents=True)
    lock.write_text("funasr==1.3.14 --hash=sha256:" + "a" * 64 + "\n", encoding="utf-8")

    records: list[ComponentRecord] = []
    for name, version in PYTHON_VERSIONS.items():
        receipt = root / f"packages/{name}/receipt.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({"name": name, "version": version}), encoding="utf-8")
        records.append(
            ComponentRecord(
                name=name,
                kind="python_package",
                source_type="managed",
                origin=f"https://pypi.org/project/{name}/{version}/",
                version=version,
                path=receipt.relative_to(root).as_posix(),
                platform=TEST_PLATFORM,
                architecture=TEST_ARCHITECTURE,
                license="fixture-only",
                verification=ComponentVerification("sha256", component_digest(receipt)),
            )
        )
    model = root / "models/model_asr"
    model.mkdir(parents=True)
    for required in (
        "configuration.json",
        "config.yaml",
        "model.pt",
        "tokens.json",
        "seg_dict",
        "am.mvn",
    ):
        (model / required).write_bytes(b"verified model fixture")
    records.append(
        ComponentRecord(
            name="model_asr",
            kind="model",
            source_type="managed",
            origin="https://modelscope.cn/models/iic/fixture",
            version="fixture-revision",
            path="models/model_asr",
            platform=TEST_PLATFORM,
            architecture=TEST_ARCHITECTURE,
            license="Apache-2.0",
            verification=ComponentVerification("sha256", component_digest(model)),
        )
    )
    manifest = ComponentManifest(
        components=tuple(records),
        platform=TEST_PLATFORM,
        architecture=TEST_ARCHITECTURE,
        managed_root=str(root.resolve()),
        schema_version=2,
        python_runtime=PythonRuntimeRecord(
            root="venv",
            interpreter=RUNTIME_INTERPRETER,
            dependency_lock=RUNTIME_LOCK,
            lock_origin="https://pypi.org/ and https://download.pytorch.org/whl/cpu",
            lock_verification=ComponentVerification("sha256", component_digest(lock)),
            device="cpu",
        ),
    )
    write_component_manifest(root / COMPONENT_MANIFEST_FILENAME, manifest)
    return manifest


def test_managed_python_is_available_only_after_isolated_runtime_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "managed runtime 中文"
    manifest = _write_managed_runtime(root)
    monkeypatch.setenv("PYTHONPATH", "/host/must/not/leak")
    monkeypatch.setenv("PYTHONHOME", "/host/python")
    monkeypatch.setenv("VIRTUAL_ENV", "/host/venv")

    diagnosis = diagnose_components(managed_manifest=manifest)

    for name in PYTHON_VERSIONS:
        component = diagnosis.components[name]
        assert component.status == "available"
        assert component.selected_source == "managed"
        assert component.path == str(root / RUNTIME_INTERPRETER)


def test_managed_python_version_or_cuda_mismatch_is_not_available(tmp_path: Path) -> None:
    root = tmp_path / "managed"
    manifest = _write_managed_runtime(root)
    _write_runtime_packages(
        root,
        {**PYTHON_VERSIONS, "funasr": "9.9.9"},
        cuda_version="12.4",
    )

    diagnosis = diagnose_components(managed_manifest=manifest)

    assert diagnosis.components["funasr"].status == "install_required"
    assert diagnosis.components["funasr"].attempts[0].detail == "managed Python runtime versions differ"
    assert diagnosis.components["torch"].status == "install_required"
    assert diagnosis.components["torch"].attempts[0].detail == "managed Python runtime is not CPU-only"


def test_missing_managed_interpreter_reports_runtime_failure_not_version_drift(
    tmp_path: Path,
) -> None:
    root = tmp_path / "managed"
    manifest = _write_managed_runtime(root)
    (root / RUNTIME_INTERPRETER).unlink()

    diagnosis = diagnose_components(managed_manifest=manifest)

    assert diagnosis.components["funasr"].attempts[0].detail == (
        "managed Python interpreter is missing or not executable"
    )


def test_fast_diagnostics_avoids_model_hash_but_explicit_verify_detects_drift(
    tmp_path: Path,
) -> None:
    root = tmp_path / "managed"
    manifest = _write_managed_runtime(root)
    (root / "models/model_asr/model.pt").write_bytes(b"changed")

    quick = diagnose_components(managed_manifest=manifest)
    verified = verify_components(managed_manifest=manifest)

    assert quick.verification_mode == "quick"
    assert quick.components["model_asr"].status == "available"
    assert verified.verification_mode == "full"
    assert verified.components["model_asr"].status == "install_required"
    assert verified.components["model_asr"].attempts[0].detail == "component checksum differs"


def test_fast_diagnostics_rejects_a_missing_model_payload(tmp_path: Path) -> None:
    root = tmp_path / "managed"
    manifest = _write_managed_runtime(root)
    (root / "models/model_asr/model.pt").unlink()

    diagnosis = diagnose_components(managed_manifest=manifest)

    assert diagnosis.components["model_asr"].status == "install_required"
    assert diagnosis.components["model_asr"].attempts[0].detail == (
        "model runtime file is missing or unsafe: model.pt"
    )


def test_schema_one_managed_wheel_is_not_a_runnable_python_component(tmp_path: Path) -> None:
    wheel = tmp_path / "managed/packages/funasr/funasr.whl"
    wheel.parent.mkdir(parents=True)
    wheel.write_bytes(b"wheel is only a supply input")
    record = ComponentRecord(
        name="funasr",
        kind="python_package",
        source_type="managed",
        origin="https://pypi.org/project/funasr/1.3.14/",
        version="1.3.14",
        path="packages/funasr/funasr.whl",
        platform=TEST_PLATFORM,
        architecture=TEST_ARCHITECTURE,
        license="MIT",
        verification=ComponentVerification("sha256", component_digest(wheel)),
    )
    manifest = ComponentManifest(
        components=(record,),
        platform=TEST_PLATFORM,
        architecture=TEST_ARCHITECTURE,
        managed_root=str(wheel.parents[2]),
    )

    diagnosis = diagnose_components(managed_manifest=manifest)

    assert diagnosis.components["funasr"].status == "install_required"
    assert diagnosis.components["funasr"].attempts[0].detail == "managed Python runtime manifest is missing"


def test_runtime_manifest_has_platform_specific_paths_and_rejects_escape() -> None:
    windows = PythonRuntimeRecord(
        root="venv",
        interpreter="venv/Scripts/python.exe",
        dependency_lock="locks/windows-x64-py311.lock",
        lock_origin="https://download.pytorch.org/whl/cpu",
        lock_verification=ComponentVerification("sha256", "a" * 64),
        device="cpu",
    )
    ComponentManifest(
        components=(),
        platform="windows",
        architecture="x86_64",
        managed_root=r"C:\Users\示例 用户\AppData\Local\Roughcut Managed",
        schema_version=2,
        python_runtime=windows,
    )

    with pytest.raises(ComponentError, match="interpreter path"):
        ComponentManifest(
            components=(),
            platform="windows",
            architecture="x86_64",
            managed_root=r"C:\Users\示例 用户\AppData\Local\Roughcut Managed",
            schema_version=2,
            python_runtime=replace(windows, interpreter="venv/bin/python"),
        )
    with pytest.raises(ComponentError, match="escapes managed root"):
        replace(windows, dependency_lock="../outside.lock")


def test_uninstall_removes_runtime_owned_paths_and_keeps_unowned_file(tmp_path: Path) -> None:
    root = tmp_path / "managed"
    _write_managed_runtime(root)
    unowned = root / "keep-user-file.txt"
    unowned.write_text("keep", encoding="utf-8")

    result = uninstall_managed_components(root)

    assert result.uninstalled is True
    assert not (root / "venv").exists()
    assert not (root / RUNTIME_LOCK).exists()
    assert unowned.read_text(encoding="utf-8") == "keep"
    assert not (root / COMPONENT_MANIFEST_FILENAME).exists()


def test_runtime_manifest_round_trip_preserves_lock_provenance(tmp_path: Path) -> None:
    root = tmp_path / "managed"
    expected = _write_managed_runtime(root)

    actual = load_component_manifest(root / COMPONENT_MANIFEST_FILENAME)

    assert actual == expected
    assert actual.python_runtime is not None
    assert actual.python_runtime.lock_origin.endswith("download.pytorch.org/whl/cpu")
    assert actual.python_runtime.lock_verification.algorithm == "sha256"
