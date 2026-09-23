from __future__ import annotations

import json
import locale
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
import scripts.bootstrap as bootstrap_script
from scripts.bootstrap import bootstrap_media_components

from roughcut.adapters import component_environment
from roughcut.adapters.component_environment import (
    COMPONENT_MANIFEST_FILENAME,
    COMPONENT_NAMES,
    LEGACY_COMPONENT_NAMES,
    ComponentError,
    ComponentManifest,
    build_external_component,
    current_architecture,
    current_platform,
    diagnose_components,
    install_local_bundle,
    load_component_manifest,
    uninstall_managed_components,
    write_component_manifest,
)

ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = ROOT / "scripts" / "bootstrap.py"
UNINSTALL = ROOT / "scripts" / "uninstall.py"


def _subprocess_diagnostics(
    result: subprocess.CompletedProcess[str],
    argv: list[str],
    cwd: Path,
) -> str:
    return "\n".join(
        (
            "subprocess diagnostics:",
            f"argv={argv!r}",
            f"cwd={str(cwd)!r}",
            f"returncode={result.returncode!r}",
            f"stdout={result.stdout!r}",
            f"stderr={result.stderr!r}",
            f"sys.executable={sys.executable!r}",
            f"sys.version={sys.version!r}",
            f"platform.architecture={platform.architecture()!r}",
            f"platform.machine={platform.machine()!r}",
            f"os.name={os.name!r}",
            f"sys.platform={sys.platform!r}",
            f"locale.getencoding={locale.getencoding()!r}",
            f"sys.stdout.encoding={getattr(sys.stdout, 'encoding', None)!r}",
            f"sys.stderr.encoding={getattr(sys.stderr, 'encoding', None)!r}",
            "subprocess.capture_output=True",
            "subprocess.text=True",
            "subprocess.encoding='utf-8'",
            "subprocess.errors=None",
        )
    )


def _json_subprocess_payload(
    result: subprocess.CompletedProcess[str],
    argv: list[str],
    cwd: Path,
    *,
    expected_returncode: int,
) -> dict[str, object]:
    diagnostics = _subprocess_diagnostics(result, argv, cwd)
    assert result.returncode == expected_returncode, diagnostics
    assert result.stdout, diagnostics
    assert result.stdout.count("\n") == 1, diagnostics
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        pytest.fail(f"{diagnostics}\njson_decode_error={error!r}")
    assert isinstance(payload, dict), f"{diagnostics}\npayload_type={type(payload)!r}"
    return payload


def _snapshot_files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


VERSIONS = {
    "funasr": "1.3.8",
    "torch": "2.6.0",
    "torchaudio": "2.6.0",
    "model_asr": "fixture-asr-1",
    "model_vad": "fixture-vad-1",
    "model_punc": "fixture-punc-1",
    "model_spk": "fixture-spk-1",
    "ffmpeg": "8.1.1-fixture",
    "ffprobe": "8.1.1-fixture",
}


def _write_fake_bundle(root: Path) -> tuple[Path, ComponentManifest]:
    artifacts = root / "候选 组件"
    packages = artifacts / "wheels"
    packages.mkdir(parents=True)
    paths: dict[str, Path] = {}
    for name in ("funasr", "torch", "torchaudio"):
        path = packages / f"{name}-{VERSIONS[name]}-py3-none-any.whl"
        path.write_bytes(f"fake wheel for {name}\n".encode())
        paths[name] = path
    for name in ("model_asr", "model_vad", "model_punc", "model_spk"):
        path = artifacts / "模型" / name
        path.mkdir(parents=True)
        for required in component_environment.MODEL_RUNTIME_FILES[name]:
            if required.endswith(".json"):
                (path / required).write_text(
                    json.dumps({"name": name}, ensure_ascii=False), encoding="utf-8"
                )
            else:
                (path / required).write_bytes(f"fixture weights for {name} {required}".encode())
        paths[name] = path
    for name in ("ffmpeg", "ffprobe"):
        path = artifacts / "FFmpeg 工具" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"#!/bin/sh\necho {VERSIONS[name]}\n", encoding="utf-8")
        path.chmod(0o755)
        paths[name] = path

    records = tuple(
        build_external_component(
            name,
            paths[name],
            version=VERSIONS[name],
            origin=f"fixture://{name}",
            license="fixture-only",
        )
        for name in LEGACY_COMPONENT_NAMES
    )
    manifest = ComponentManifest(
        components=records,
        platform=current_platform(),
        architecture=current_architecture(),
    )
    manifest_path = root / "bundle manifest.json"
    write_component_manifest(manifest_path, manifest)
    return manifest_path, manifest


def test_local_bundle_is_atomically_installed_then_reused(tmp_path: Path) -> None:
    bundle_path, bundle = _write_fake_bundle(tmp_path)
    managed_root = tmp_path / "用户 数据" / "Roughcut Managed"

    first = install_local_bundle(managed_root, bundle_path)
    second = install_local_bundle(managed_root, bundle_path)

    assert first.installed is True
    assert second.installed is False
    assert second.reused is False
    assert second.install_required == ("funasr", "torch", "torchaudio")
    manifest_path = managed_root / COMPONENT_MANIFEST_FILENAME
    installed = load_component_manifest(manifest_path)
    assert installed.schema_version == 1
    assert installed.managed_root == str(managed_root.resolve())
    assert {component.name for component in installed.components} == set(LEGACY_COMPONENT_NAMES)
    for component in installed.components:
        assert component.source_type == "managed"
        assert component.version == VERSIONS[component.name]
        assert component.origin == f"fixture://{component.name}"
        assert component.license == "fixture-only"
        assert component.platform == current_platform()
        assert component.architecture == current_architecture()
        assert component.verification.algorithm == "sha256"
        assert len(component.verification.value) == 64
        assert not Path(component.path).is_absolute()

    diagnosis = diagnose_components(
        managed_manifest=installed,
        component_names=LEGACY_COMPONENT_NAMES,
    )
    assert diagnosis.install_required == ("funasr", "torch", "torchaudio")
    assert all(
        diagnosis.components[name].selected_source == "managed"
        for name in set(LEGACY_COMPONENT_NAMES) - {"funasr", "torch", "torchaudio"}
    )
    assert bundle == load_component_manifest(bundle_path)


def test_bootstrap_uses_explicit_managed_root_and_never_downloads(tmp_path: Path) -> None:
    bundle_path, _bundle = _write_fake_bundle(tmp_path)
    managed_root = tmp_path / "隔离 用户" / "media components"

    first = bootstrap_media_components(managed_root, local_bundle_path=bundle_path)
    second = bootstrap_media_components(managed_root, local_bundle_path=bundle_path)

    assert first["installed"] is True
    assert second["installed"] is False
    assert second["reused"] is True
    assert first["managed_root"] == str(managed_root.resolve())
    diagnostics = first["diagnostics"]
    assert isinstance(diagnostics, dict)
    assert diagnostics["install_required"] == ["funasr", "torch", "torchaudio"]
    assert "audalign" not in diagnostics["components"]


def test_bootstrap_without_local_bundle_only_reports_missing_components(tmp_path: Path) -> None:
    managed_root = tmp_path / "not-created"

    result = bootstrap_media_components(managed_root)

    diagnostics = result["diagnostics"]
    assert isinstance(diagnostics, dict)
    assert diagnostics["install_required"] == list(LEGACY_COMPONENT_NAMES)
    assert result["installed"] is False
    assert not managed_root.exists()


def test_component_bootstrap_cli_defaults_to_read_only_versioned_json_plan(
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "自定义 安装"
    managed_root = tmp_path / "must not be created/managed 中文"
    cache_root = tmp_path / "must not be created/cache 中文"

    argv = [
        sys.executable,
        str(BOOTSTRAP),
        "--install-dir",
        str(install_root),
        "--managed-root",
        str(managed_root),
        "--component-cache",
        str(cache_root),
        "--ffmpeg-command",
        "roughcut-missing-ffmpeg",
        "--ffprobe-command",
        "roughcut-missing-ffprobe",
        "--json",
    ]
    result = subprocess.run(
        argv,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    payload = _json_subprocess_payload(
        result,
        argv,
        ROOT,
        expected_returncode=0,
    )
    assert result.stderr == "", _subprocess_diagnostics(result, argv, ROOT)
    assert payload["schema_version"] == 1
    assert payload["ok"] is True
    assert payload["action"] == "plan"
    assert len(payload["media_components"]["plan_hash"]) == 64
    assert payload["media_components"]["runtime_binding"] == {
        "path": str(install_root.resolve() / "runtime.json"),
        "configured": False,
        "status": "unconfigured",
    }
    assert not install_root.exists()
    assert not managed_root.exists()
    assert not cache_root.exists()


def test_component_bootstrap_cli_requires_explicit_approved_hash_for_apply(
    tmp_path: Path,
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(BOOTSTRAP),
            "--managed-root",
            str(tmp_path / "managed"),
            "--component-cache",
            str(tmp_path / "cache"),
            "--apply-components",
            "--json",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    payload = json.loads(result.stdout)
    assert result.returncode == 1
    assert result.stderr == ""
    assert payload["ok"] is False
    assert payload["error"]["code"] == "bootstrap_failed"
    assert "approved-plan-hash" in payload["error"]["message"]


def test_component_bootstrap_argparse_failure_is_json_only(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(BOOTSTRAP),
            "--managed-root",
            str(tmp_path / "managed"),
            "--component-cache",
            str(tmp_path / "cache"),
            "--unknown-component-option",
            "--json",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    payload = json.loads(result.stdout)
    assert result.returncode == 1
    assert result.stderr == ""
    assert payload["ok"] is False
    assert payload["error"]["code"] == "bootstrap_failed"


def test_bootstrap_exposes_explicit_full_component_verification(tmp_path: Path) -> None:
    bundle_path, _bundle = _write_fake_bundle(tmp_path)
    managed_root = tmp_path / "managed"
    install_local_bundle(managed_root, bundle_path)
    model = managed_root / "models/model_asr/model.pt"
    model.write_bytes(b"changed")

    quick = bootstrap_media_components(managed_root)
    verified = bootstrap_media_components(managed_root, verify_checksums=True)

    assert quick["diagnostics"]["verification_mode"] == "quick"  # type: ignore[index]
    assert quick["diagnostics"]["components"]["model_asr"]["status"] == "available"  # type: ignore[index]
    assert verified["diagnostics"]["verification_mode"] == "full"  # type: ignore[index]
    assert verified["diagnostics"]["components"]["model_asr"]["status"] == "install_required"  # type: ignore[index]


def test_bootstrap_with_bundle_full_verifies_existing_managed_components(
    tmp_path: Path,
) -> None:
    bundle_path, _bundle = _write_fake_bundle(tmp_path)
    managed_root = tmp_path / "managed"
    install_local_bundle(managed_root, bundle_path)
    (managed_root / "models/model_asr/model.pt").write_bytes(b"changed")

    result = bootstrap_media_components(managed_root, local_bundle_path=bundle_path)

    diagnostics = result["diagnostics"]
    assert isinstance(diagnostics, dict)
    assert diagnostics["verification_mode"] == "full"
    assert diagnostics["components"]["model_asr"]["status"] == "install_required"  # type: ignore[index]


def test_bootstrap_installs_only_components_missing_after_external_resolution(
    tmp_path: Path,
) -> None:
    bundle_path, bundle = _write_fake_bundle(tmp_path)
    external_record = next(item for item in bundle.components if item.name == "funasr")
    external_manifest = ComponentManifest(
        components=(external_record,),
        platform=bundle.platform,
        architecture=bundle.architecture,
    )
    external_manifest_path = tmp_path / "explicit external.json"
    write_component_manifest(external_manifest_path, external_manifest)
    external_path = Path(external_record.path)
    before = external_path.read_bytes()
    managed_root = tmp_path / "managed"

    result = bootstrap_media_components(
        managed_root,
        external_manifest_path=external_manifest_path,
        local_bundle_path=bundle_path,
    )

    installed = load_component_manifest(managed_root / COMPONENT_MANIFEST_FILENAME)
    assert {component.name for component in installed.components} == set(LEGACY_COMPONENT_NAMES) - {
        "funasr"
    }
    diagnostics = result["diagnostics"]
    assert isinstance(diagnostics, dict)
    component_diagnostics = diagnostics["components"]
    assert isinstance(component_diagnostics, dict)
    assert component_diagnostics["funasr"]["selected_source"] == "external"
    assert component_diagnostics["torch"]["selected_source"] is None
    assert component_diagnostics["model_asr"]["selected_source"] == "managed"
    assert external_path.read_bytes() == before


def test_bundle_version_constraint_rejects_different_external_version(
    tmp_path: Path,
) -> None:
    bundle_path, bundle = _write_fake_bundle(tmp_path)
    funasr = next(item for item in bundle.components if item.name == "funasr")
    external = ComponentManifest(
        components=(replace(funasr, version="9.9.9"),),
        platform=bundle.platform,
        architecture=bundle.architecture,
    )
    external_manifest_path = tmp_path / "different version.json"
    write_component_manifest(external_manifest_path, external)
    managed_root = tmp_path / "managed"

    result = bootstrap_media_components(
        managed_root,
        external_manifest_path=external_manifest_path,
        local_bundle_path=bundle_path,
    )

    diagnostics = result["diagnostics"]
    assert isinstance(diagnostics, dict)
    component_diagnostics = diagnostics["components"]
    assert isinstance(component_diagnostics, dict)
    assert component_diagnostics["funasr"]["selected_source"] is None
    assert component_diagnostics["funasr"]["attempts"][0] == {
        "source_type": "external",
        "status": "incompatible",
        "detail": "version does not match",
    }
    assert component_diagnostics["funasr"]["attempts"][1] == {
        "source_type": "managed",
        "status": "incompatible",
        "detail": "managed Python runtime manifest is missing",
    }


def test_external_precedes_managed_without_being_modified(tmp_path: Path) -> None:
    bundle_path, bundle = _write_fake_bundle(tmp_path)
    managed_root = tmp_path / "managed"
    install_local_bundle(managed_root, bundle_path)
    managed = load_component_manifest(managed_root / COMPONENT_MANIFEST_FILENAME)
    external = ComponentManifest(
        components=(next(item for item in bundle.components if item.name == "funasr"),),
        platform=current_platform(),
        architecture=current_architecture(),
    )
    external_path = Path(external.components[0].path)
    before_stat = external_path.stat()
    before_bytes = external_path.read_bytes()

    diagnosis = diagnose_components(
        external_manifest=external,
        managed_manifest=managed,
        component_names=LEGACY_COMPONENT_NAMES,
    )

    assert diagnosis.components["funasr"].selected_source == "external"
    assert diagnosis.components["torch"].selected_source is None
    assert diagnosis.components["model_asr"].selected_source == "managed"
    after_stat = external_path.stat()
    assert external_path.read_bytes() == before_bytes
    assert (
        after_stat.st_mode,
        after_stat.st_ino,
        after_stat.st_dev,
        after_stat.st_nlink,
        after_stat.st_size,
        after_stat.st_mtime_ns,
        after_stat.st_ctime_ns,
    ) == (
        before_stat.st_mode,
        before_stat.st_ino,
        before_stat.st_dev,
        before_stat.st_nlink,
        before_stat.st_size,
        before_stat.st_mtime_ns,
        before_stat.st_ctime_ns,
    )


def test_invalid_external_falls_back_to_compatible_managed(tmp_path: Path) -> None:
    bundle_path, bundle = _write_fake_bundle(tmp_path)
    managed_root = tmp_path / "managed"
    install_local_bundle(managed_root, bundle_path)
    managed = load_component_manifest(managed_root / COMPONENT_MANIFEST_FILENAME)
    external_record = next(item for item in bundle.components if item.name == "funasr")
    Path(external_record.path).write_text("changed after manifest", encoding="utf-8")
    external = ComponentManifest(
        components=(external_record,),
        platform=current_platform(),
        architecture=current_architecture(),
    )

    diagnosis = diagnose_components(
        external_manifest=external,
        managed_manifest=managed,
        component_names=LEGACY_COMPONENT_NAMES,
    )

    selected = diagnosis.components["funasr"]
    assert selected.selected_source is None
    assert selected.attempts[0].source_type == "external"
    assert selected.attempts[0].status == "incompatible"
    assert selected.attempts[1].detail == "managed Python runtime manifest is missing"


def test_missing_components_only_report_install_requirement(tmp_path: Path) -> None:
    managed_root = tmp_path / "must-not-be-created"

    diagnosis = diagnose_components()

    assert diagnosis.install_required == COMPONENT_NAMES
    assert set(diagnosis.components) == set(COMPONENT_NAMES)
    assert "audalign" in diagnosis.components
    assert all(item.action == "install_managed" for item in diagnosis.components.values())
    assert not managed_root.exists()


def test_failed_install_leaves_no_final_or_staging_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle_path, _bundle = _write_fake_bundle(tmp_path)
    managed_root = tmp_path / "managed failure"

    def fail_copy(_source: Path, _destination: Path) -> None:
        raise OSError("injected copy failure")

    monkeypatch.setattr(component_environment, "_copy_file", fail_copy)

    with pytest.raises(ComponentError, match="could not be installed"):
        install_local_bundle(managed_root, bundle_path)

    assert not managed_root.exists()
    assert not list(tmp_path.glob(f".{managed_root.name}.install-*"))


def test_failed_atomic_publish_leaves_no_final_or_staging_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle_path, _bundle = _write_fake_bundle(tmp_path)
    managed_root = tmp_path / "managed publish failure"
    real_replace = component_environment.os.replace

    def fail_final_publish(source: Path, destination: Path) -> None:
        if Path(destination) == managed_root.resolve():
            raise OSError("injected atomic publish failure")
        real_replace(source, destination)

    monkeypatch.setattr(component_environment.os, "replace", fail_final_publish)

    with pytest.raises(ComponentError, match="could not be installed"):
        install_local_bundle(managed_root, bundle_path)

    assert not managed_root.exists()
    assert not list(tmp_path.glob(f".{managed_root.name}.install-*"))


def test_cancelled_install_leaves_no_final_or_staging_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle_path, _bundle = _write_fake_bundle(tmp_path)
    managed_root = tmp_path / "managed cancelled"

    def cancel_copy(_source: Path, _destination: Path) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(component_environment, "_copy_file", cancel_copy)

    with pytest.raises(KeyboardInterrupt):
        install_local_bundle(managed_root, bundle_path)

    assert not managed_root.exists()
    assert not list(tmp_path.glob(f".{managed_root.name}.install-*"))


def test_uninstall_removes_only_manifest_owned_managed_paths(tmp_path: Path) -> None:
    bundle_path, bundle = _write_fake_bundle(tmp_path)
    managed_root = tmp_path / "managed"
    install_local_bundle(managed_root, bundle_path)
    manifest_path = managed_root / COMPONENT_MANIFEST_FILENAME
    external = next(item for item in bundle.components if item.name == "funasr")
    external_path = Path(external.path)
    external_bytes = external_path.read_bytes()
    unowned = managed_root / "keep-user-file.txt"
    unowned.write_text("keep", encoding="utf-8")

    result = uninstall_managed_components(managed_root)

    assert result.uninstalled is True
    assert set(result.removed_components) == set(LEGACY_COMPONENT_NAMES)
    assert external_path.read_bytes() == external_bytes
    assert unowned.read_text(encoding="utf-8") == "keep"
    assert not manifest_path.exists()


def test_uninstall_script_returns_versioned_json(tmp_path: Path) -> None:
    bundle_path, _bundle = _write_fake_bundle(tmp_path)
    managed_root = tmp_path / "managed root 中文"
    install_local_bundle(managed_root, bundle_path)

    argv = [
        sys.executable,
        str(UNINSTALL),
        "--managed-root",
        str(managed_root),
        "--json",
    ]
    result = subprocess.run(
        argv,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    payload = _json_subprocess_payload(
        result,
        argv,
        ROOT,
        expected_returncode=0,
    )
    assert result.stderr == "", _subprocess_diagnostics(result, argv, ROOT)
    assert payload["schema_version"] == 1
    assert payload["ok"] is True
    assert set(payload["removed_components"]) == set(LEGACY_COMPONENT_NAMES)
    assert not managed_root.exists()


def test_uninstall_argparse_failures_are_versioned_json(tmp_path: Path) -> None:
    commands = [
        [sys.executable, str(UNINSTALL), "--json"],
        [
            sys.executable,
            str(UNINSTALL),
            "--managed-root",
            str(tmp_path / "managed root 中文"),
            "--unknown-option",
            "--json",
        ],
    ]

    for argv in commands:
        result = subprocess.run(
            argv,
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        payload = _json_subprocess_payload(
            result,
            argv,
            ROOT,
            expected_returncode=1,
        )
        assert result.stderr == "", _subprocess_diagnostics(result, argv, ROOT)
        assert payload["schema_version"] == 1
        assert payload["ok"] is False
        assert payload["error"]["code"] == "uninstall_failed"


def test_uninstall_cli_is_idempotent_and_preserves_unowned_external_data(
    tmp_path: Path,
) -> None:
    bundle_path, bundle = _write_fake_bundle(tmp_path)
    managed_root = tmp_path / "managed root 中文 with spaces"
    install_local_bundle(managed_root, bundle_path)
    unowned = managed_root / "keep-user-file.txt"
    unowned.write_text("keep", encoding="utf-8")
    external = next(item for item in bundle.components if item.name == "funasr")
    external_path = Path(external.path)
    external_bytes = external_path.read_bytes()
    argv = [
        sys.executable,
        str(UNINSTALL),
        "--managed-root",
        str(managed_root),
        "--json",
    ]

    first_result = subprocess.run(
        argv,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    first = _json_subprocess_payload(
        first_result,
        argv,
        ROOT,
        expected_returncode=0,
    )
    assert first_result.stderr == "", _subprocess_diagnostics(first_result, argv, ROOT)
    assert first["uninstalled"] is True
    assert set(first["removed_components"]) == set(LEGACY_COMPONENT_NAMES)
    assert managed_root.exists()
    assert unowned.read_text(encoding="utf-8") == "keep"
    assert external_path.read_bytes() == external_bytes

    second_result = subprocess.run(
        argv,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    second = _json_subprocess_payload(
        second_result,
        argv,
        ROOT,
        expected_returncode=0,
    )
    assert second_result.stderr == "", _subprocess_diagnostics(second_result, argv, ROOT)
    assert second["uninstalled"] is False
    assert second["removed_components"] == []
    assert managed_root.exists()
    assert unowned.read_text(encoding="utf-8") == "keep"
    assert external_path.read_bytes() == external_bytes


def test_uninstall_cli_returns_versioned_error_without_deleting_on_path_escape(
    tmp_path: Path,
) -> None:
    bundle_path, _bundle = _write_fake_bundle(tmp_path)
    managed_root = tmp_path / "managed root 中文"
    install_local_bundle(managed_root, bundle_path)
    manifest_path = managed_root / COMPONENT_MANIFEST_FILENAME
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    first_owned = managed_root / raw["components"][0]["path"]
    outside = tmp_path / "outside.txt"
    outside.write_text("do not delete", encoding="utf-8")
    raw["components"][0]["path"] = "../outside.txt"
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")
    argv = [
        sys.executable,
        str(UNINSTALL),
        "--managed-root",
        str(managed_root),
        "--json",
    ]

    result = subprocess.run(
        argv,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    payload = _json_subprocess_payload(
        result,
        argv,
        ROOT,
        expected_returncode=1,
    )

    assert result.stderr == "", _subprocess_diagnostics(result, argv, ROOT)
    assert payload["schema_version"] == 1
    assert payload["ok"] is False
    assert payload["error"]["code"] == "uninstall_failed"
    assert outside.read_text(encoding="utf-8") == "do not delete"
    assert first_owned.exists()
    assert manifest_path.exists()


def test_uninstall_rejects_manifest_path_escape_before_deleting(tmp_path: Path) -> None:
    bundle_path, _bundle = _write_fake_bundle(tmp_path)
    managed_root = tmp_path / "managed"
    install_local_bundle(managed_root, bundle_path)
    manifest_path = managed_root / COMPONENT_MANIFEST_FILENAME
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    first_owned = managed_root / raw["components"][0]["path"]
    outside = tmp_path / "outside.txt"
    outside.write_text("do not delete", encoding="utf-8")
    raw["components"][0]["path"] = "../outside.txt"
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ComponentError, match="escapes managed root"):
        uninstall_managed_components(managed_root)

    assert outside.read_text(encoding="utf-8") == "do not delete"
    assert first_owned.exists()


def test_uninstall_rejects_a_tampered_broad_component_directory(
    tmp_path: Path,
) -> None:
    bundle_path, _bundle = _write_fake_bundle(tmp_path)
    managed_root = tmp_path / "managed"
    install_local_bundle(managed_root, bundle_path)
    manifest_path = managed_root / COMPONENT_MANIFEST_FILENAME
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    model = next(item for item in raw["components"] if item["name"] == "model_asr")
    model["path"] = "models"
    unowned = managed_root / "models" / "keep-user-file.txt"
    unowned.write_text("keep", encoding="utf-8")
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ComponentError, match="not canonical"):
        uninstall_managed_components(managed_root)

    assert unowned.read_text(encoding="utf-8") == "keep"
    assert (managed_root / "models" / "model_asr").is_dir()


def test_component_lifecycle_rejects_filesystem_root_as_managed_root() -> None:
    with pytest.raises(ComponentError, match="dedicated subdirectory"):
        uninstall_managed_components(Path(Path.cwd().anchor))


def test_uninstall_script_prefers_repository_core_over_ambient_package(
    tmp_path: Path,
) -> None:
    ambient = tmp_path / "ambient"
    (ambient / "roughcut" / "adapters").mkdir(parents=True)
    (ambient / "roughcut" / "__init__.py").write_text("", encoding="utf-8")
    (ambient / "roughcut" / "adapters" / "__init__.py").write_text(
        "", encoding="utf-8"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ambient)
    result = subprocess.run(
        [
            sys.executable,
            str(UNINSTALL),
            "--managed-root",
            str(tmp_path / "dedicated-managed-root"),
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == {
        "managed_root": str((tmp_path / "dedicated-managed-root").resolve()),
        "ok": True,
        "schema_version": 1,
        "uninstalled": False,
        "removed_components": [],
    }


def test_bootstrap_cli_installs_and_reuses_core_from_local_wheel(
    tmp_path: Path,
) -> None:
    wheel_dir = tmp_path / "本地 wheel with spaces"
    wheel_dir.mkdir()
    core_source = tmp_path / "临时 core source with spaces"
    shutil.copytree(
        ROOT / "core",
        core_source,
        ignore=shutil.ignore_patterns("build", "dist", "*.egg-info", "__pycache__"),
    )
    build_argv = [
        sys.executable,
        "-m",
        "build",
        "--wheel",
        "--no-isolation",
        "--outdir",
        str(wheel_dir),
        str(core_source),
    ]
    build_result = subprocess.run(
        build_argv,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert build_result.returncode == 0, _subprocess_diagnostics(
        build_result,
        build_argv,
        ROOT,
    )
    wheel = wheel_dir / "roughcut-0.2.8-py3-none-any.whl"
    assert wheel.is_file()

    install_dir = tmp_path / "安装 根 with spaces"
    managed_root = tmp_path / "must not be created managed"
    cache_root = tmp_path / "must not be created cache"
    bootstrap_argv = [
        sys.executable,
        str(BOOTSTRAP),
        "--install-dir",
        str(install_dir),
        "--core-wheel",
        str(wheel),
        "--json",
    ]

    def run_bootstrap() -> dict[str, object]:
        result = subprocess.run(
            bootstrap_argv,
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        payload = _json_subprocess_payload(
            result,
            bootstrap_argv,
            ROOT,
            expected_returncode=0,
        )
        assert result.stderr == "", _subprocess_diagnostics(result, bootstrap_argv, ROOT)
        return payload

    first = run_bootstrap()
    assert first["schema_version"] == 1
    assert first["ok"] is True
    assert first["core_version"] == "0.2.8"
    assert first["core_action"] == "installed"
    assert first["installed"] is True

    launcher_dir = bootstrap_script.venv_bin(install_dir / "venv")
    suffix = ".exe" if sys.platform == "win32" else ""
    roughcut = (launcher_dir / f"roughcut{suffix}").resolve()
    mcp = (launcher_dir / f"roughcut-mcp{suffix}").resolve()
    assert roughcut.is_file()
    assert mcp.is_file()
    assert Path(str(first["mcp_command"])) == mcp
    if sys.platform == "win32":
        assert launcher_dir.name == "Scripts"
        assert roughcut.drive
        assert mcp.drive
    else:
        assert launcher_dir.name == "bin"

    health_argv = [str(roughcut), "health", "--json"]
    health_result = subprocess.run(
        health_argv,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    health_payload = _json_subprocess_payload(
        health_result,
        health_argv,
        ROOT,
        expected_returncode=0,
    )
    assert health_result.stderr == "", _subprocess_diagnostics(
        health_result,
        health_argv,
        ROOT,
    )
    assert health_payload["schema_version"] == 1
    assert health_payload["core_version"] == "0.2.8"
    assert health_payload["tool_schema_version"] == 32
    assert health_payload["ok"] is True

    install_snapshot = _snapshot_files(install_dir)
    second = run_bootstrap()
    assert second["core_action"] == "reused"
    assert second["installed"] is False
    assert _snapshot_files(install_dir) == install_snapshot
    assert not (install_dir / "runtime.json").exists()
    assert not managed_root.exists()
    assert not cache_root.exists()
