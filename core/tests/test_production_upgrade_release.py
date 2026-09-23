from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import runpy
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest
from scripts import build_production_upgrade as production_builder
from scripts.build_core_release import SOURCE_BUNDLE_DIRECTORIES, SOURCE_BUNDLE_FILES
from scripts.build_core_update import (
    CORE_DIRECTORY_NAME,
    CORE_VERSION,
    SOURCE_BUNDLE_NAME,
    UPDATE_FOLDER_NAME,
    WHEEL_NAME,
)
from scripts.build_production_upgrade import (
    GUIDE_NAME,
    MANIFEST_NAME,
    PRODUCTION_FOLDER_PREFIX,
    ROOT_CHECKSUMS_NAME,
    ProductionUpgradeError,
    _guide,
    assemble,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
AUDALIGN_CACHE_ENV = "ROUGHCUT_AUDALIGN_CACHE_SOURCE"
EXTERNAL_COMPONENTS_ENV = "ROUGHCUT_EXTERNAL_COMPONENTS"
EXTERNAL_FUNASR_ENV = "ROUGHCUT_EXTERNAL_FUNASR_PYTHON"
FFMPEG_ENV = "ROUGHCUT_FFMPEG_COMMAND"
FFPROBE_ENV = "ROUGHCUT_FFPROBE_COMMAND"


def _audalign_cache_source() -> Path:
    value = os.environ.get(AUDALIGN_CACHE_ENV)
    if not value:
        pytest.skip(f"set {AUDALIGN_CACHE_ENV} for the verified macOS Audalign cache")
    source = Path(value)
    if not source.is_dir():
        pytest.skip(f"verified Audalign cache is unavailable: {source}")
    return source


def _resource_path(name: str, *, file: bool) -> Path:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"set {name} for the production-upgrade CLI E2E")
    path = Path(value)
    if (path.is_file() if file else path.is_dir()) is False:
        pytest.skip(f"production-upgrade E2E resource is unavailable: {path}")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_bootstrap_cli(
    script: Path,
    arguments: list[str],
) -> dict[str, Any]:
    old_argv = sys.argv
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        sys.argv = [str(script), *arguments]
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                runpy.run_path(str(script), run_name="__main__")
            except SystemExit as error:
                exit_code = error.code
            else:
                exit_code = 0
    finally:
        sys.argv = old_argv
    lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
    assert lines, stderr.getvalue()
    payload = json.loads(lines[-1])
    assert exit_code == 0, payload
    assert payload["ok"] is True, payload
    return payload


def _installed_cli_json(
    command: Path,
    runtime_path: Path,
    *arguments: str,
) -> dict[str, Any]:
    environment = os.environ.copy()
    environment["ROUGHCUT_RUNTIME_BINDING"] = str(runtime_path)
    for name in (
        "ROUGHCUT_FFMPEG_COMMAND",
        "ROUGHCUT_FFPROBE_COMMAND",
        "ROUGHCUT_FUNASR_PYTHON",
        "ROUGHCUT_FUNASR_MODEL_ROOT",
        "PYTHONPATH",
    ):
        environment.pop(name, None)
    result = subprocess.run(
        [str(command), *arguments, "--json"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout)
    assert payload["ok"] is True, payload
    return payload


def _extract_source_bundle(source_bundle: Path, destination: Path) -> Path:
    with tarfile.open(source_bundle, "r:gz") as stream:
        members = stream.getmembers()
        assert members
        for member in members:
            parts = Path(member.name).parts
            assert not Path(member.name).is_absolute()
            assert ".." not in parts
            assert not member.issym()
            assert not member.islnk()
        stream.extractall(destination)
    return destination / f"roughcut-core-{CORE_VERSION}"


def test_production_upgrade_guide_follows_the_current_core_identity() -> None:
    """The generated guide must not pin a stale Core identity.

    The Audalign-cache-gated assembly tests skip when the verified cache is
    absent, so the guide identity is asserted directly instead.
    """

    guide = _guide()
    core_directory = f"Core-{CORE_VERSION}"

    assert PRODUCTION_FOLDER_PREFIX == f"roughcut-macos-arm64-{CORE_VERSION}-production-upgrade"
    assert f'PACKAGE_ROOT="/absolute/path/to/{PRODUCTION_FOLDER_PREFIX}"' in guide
    assert f'CORE_PACKAGE_DIR="$PACKAGE_ROOT/core-update/{core_directory}"' in guide
    assert f'tar -xzf "$CORE_PACKAGE_DIR/{SOURCE_BUNDLE_NAME}"' in guide
    assert f'CORE_WHEEL="$CORE_PACKAGE_DIR/{WHEEL_NAME}"' in guide
    assert "Core-0.2.7" not in guide
    assert "roughcut-0.2.7-" not in guide


def test_production_upgrade_manifest_and_core_payload_use_current_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        production_builder,
        "_validate_cache",
        lambda *_args, **_kwargs: [],
    )
    output, _archive = assemble(
        REPOSITORY_ROOT / "core",
        tmp_path / "unused-cache",
        tmp_path / PRODUCTION_FOLDER_PREFIX,
        source_commit="9" * 40,
    )

    manifest = json.loads((output / MANIFEST_NAME).read_text(encoding="utf-8"))
    core_update = manifest["core_update"]
    assert output.name == PRODUCTION_FOLDER_PREFIX
    assert core_update["version"] == CORE_VERSION
    assert core_update["source_commit"] == "9" * 40
    assert core_update["wheel"]["path"] == (
        f"core-update/{CORE_DIRECTORY_NAME}/{WHEEL_NAME}"
    )
    assert core_update["source_bundle"]["path"] == (
        f"core-update/{CORE_DIRECTORY_NAME}/{SOURCE_BUNDLE_NAME}"
    )

    core_manifest = json.loads(
        (output / core_update["manifest"]["path"]).read_text(encoding="utf-8")
    )
    assert core_manifest["release_name"] == UPDATE_FOLDER_NAME
    assert core_manifest["core"]["version"] == CORE_VERSION
    assert core_manifest["core"]["directory"] == CORE_DIRECTORY_NAME

    with ZipFile(output / core_update["wheel"]["path"]) as wheel:
        metadata_name = next(
            name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = wheel.read(metadata_name).decode("utf-8")
    assert f"Version: {CORE_VERSION}\n" in metadata

    with tarfile.open(output / core_update["source_bundle"]["path"], "r:gz") as source:
        assert f"roughcut-core-{CORE_VERSION}" in {
            Path(member.name).parts[0] for member in source.getmembers()
        }


def test_production_upgrade_rejects_missing_explicit_cache(tmp_path: Path) -> None:
    with pytest.raises(ProductionUpgradeError, match="directory"):
        assemble(
            REPOSITORY_ROOT / "core",
            tmp_path / "missing-cache",
            tmp_path / "bundle",
            source_commit="5" * 40,
        )
    assert not (tmp_path / "bundle").exists()


def test_production_upgrade_uses_catalog_from_explicit_core_source(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "core-with-alt-catalog-root"
    for relative_directory in SOURCE_BUNDLE_DIRECTORIES:
        shutil.copytree(REPOSITORY_ROOT / relative_directory, source_root / relative_directory)
    for relative_file in SOURCE_BUNDLE_FILES:
        destination = source_root / relative_file
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPOSITORY_ROOT / relative_file, destination)

    core_source = source_root / "core"
    catalog_path = core_source / "src/roughcut/component_catalog/release-catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["catalog_version"] = "core-source-test-catalog"
    catalog_path.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    output, _archive = assemble(
        core_source,
        _audalign_cache_source(),
        tmp_path / PRODUCTION_FOLDER_PREFIX,
        source_commit="6" * 40,
    )

    manifest = json.loads((output / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["component_catalog"]["catalog_version"] == (
        "core-source-test-catalog"
    )
    assert (
        output / "component-catalog/release-catalog.json"
    ).read_bytes() == catalog_path.read_bytes()


def test_production_upgrade_is_offline_and_keeps_core_update_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _audalign_cache_source()

    def no_network(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("production upgrade builder must not access the network")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    output, archive = assemble(
        REPOSITORY_ROOT / "core",
        cache,
        tmp_path / PRODUCTION_FOLDER_PREFIX,
        source_commit="5" * 40,
    )

    manifest = json.loads((output / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["kind"] == "roughcut-production-upgrade-bundle"
    assert manifest["package_kind"] == "production-upgrade"
    assert manifest["core_dependencies"] == []
    assert manifest["offline"] is True
    assert manifest["source_commit"] == "5" * 40
    assert manifest["audalign_component_cache"]["provider"] == "audalign"
    assert manifest["audalign_component_cache"]["version"] == "1.3.1"
    assert manifest["audalign_component_cache"]["artifact_count"] == 16
    assert manifest["audalign_component_cache"]["expected_download_bytes_on_target"] == 0
    assert manifest["target_contract"]["runtime_binding_preseeded"] is False

    core_manifest = json.loads(
        (output / "core-update/release-manifest.json").read_text(encoding="utf-8")
    )
    assert core_manifest["kind"] == "core-update-folder"
    assert core_manifest["core"]["git_commit"] == "5" * 40
    assert (output / GUIDE_NAME).read_text(encoding="utf-8").count(
        "--include-audalign"
    ) == 3
    guide = (output / GUIDE_NAME).read_text(encoding="utf-8")
    assert guide.count('shasum -a 256 -c "$PACKAGE_ROOT/SHA256SUMS"') == 1
    assert guide.count('shasum -a 256 -c "$CORE_PACKAGE_DIR/CORE-SHA256SUMS"') == 1
    assert guide.count('--ffmpeg-command "$FFMPEG_COMMAND"') == 3
    assert guide.count('--ffprobe-command "$FFPROBE_COMMAND"') == 3
    assert guide.count('--operation-id "$OPERATION_ID"') == 1
    assert 'print(plan["media_components"]["plan_hash"])' in guide
    assert 'print(plan["installation_operation"]["operation_id"])' in guide
    assert (
        'plan["media_components"]["installation_operation"]["operation_id"]'
        not in guide
    )
    assert "--no-index" in guide
    assert "--approved-plan-hash" in guide
    assert "--apply-components" in guide
    assert "runtime.json" not in {
        path.name for path in output.rglob("*")
    }
    assert "Gate8" not in guide
    assert not any(path.name in {"Project", "media"} for path in output.rglob("*"))

    checksums = (output / ROOT_CHECKSUMS_NAME).read_text(encoding="utf-8")
    assert "production-upgrade-manifest.json" in checksums
    assert str(cache) not in checksums
    assert archive.is_file()
    with tarfile.open(archive, "r:gz") as stream:
        names = stream.getnames()
    assert names[0] == output.name
    assert all(".." not in Path(name).parts for name in names)


def test_production_upgrade_bundle_cli_e2e_is_offline_and_preserves_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the shipped bundle through canonical bootstrap CLI entrypoints."""
    audalign_cache = _resource_path(AUDALIGN_CACHE_ENV, file=False)
    external_components = _resource_path(EXTERNAL_COMPONENTS_ENV, file=True)
    external_funasr = _resource_path(EXTERNAL_FUNASR_ENV, file=True)
    ffmpeg = _resource_path(FFMPEG_ENV, file=True)
    ffprobe = _resource_path(FFPROBE_ENV, file=True)

    output, _archive = assemble(
        REPOSITORY_ROOT / "core",
        audalign_cache,
        tmp_path / PRODUCTION_FOLDER_PREFIX,
        source_commit="a" * 40,
    )
    core_directory = output / "core-update" / f"Core-{CORE_VERSION}"
    source_bundle = core_directory / SOURCE_BUNDLE_NAME
    core_wheel = core_directory / WHEEL_NAME
    source_root = _extract_source_bundle(source_bundle, tmp_path / "source")
    bootstrap = source_root / "scripts" / "bootstrap.py"

    install_root = tmp_path / "install"
    managed_root = tmp_path / "managed"
    runtime_path = install_root / "runtime.json"
    roughcut = install_root / "venv" / "bin" / "roughcut"
    common = [
        "--install-dir",
        str(install_root),
        "--managed-root",
        str(managed_root),
        "--external-components",
        str(external_components),
        "--external-funasr-python",
        str(external_funasr),
        "--ffmpeg-command",
        str(ffmpeg),
        "--ffprobe-command",
        str(ffprobe),
        "--component-cache",
        str(audalign_cache),
        "--verify-components",
        "--include-audalign",
        "--json",
    ]
    installed = _run_bootstrap_cli(
        bootstrap,
        ["--install-dir", str(install_root), "--core-wheel", str(core_wheel), "--json"],
    )
    assert installed["core_action"] == "installed"

    audalign_plan = _run_bootstrap_cli(bootstrap, common)
    audalign_media = audalign_plan["media_components"]
    audalign_operation = audalign_plan["installation_operation"]
    audalign_apply = _run_bootstrap_cli(
        bootstrap,
        [
            *common,
            "--apply-components",
            "--approved-plan-hash",
            audalign_media["plan_hash"],
            "--operation-id",
            audalign_operation["operation_id"],
        ],
    )
    assert audalign_apply["media_components"]["operation"]["status"] == "succeeded"
    assert audalign_apply["media_components"]["readback"] is False

    before_diagnostics = _installed_cli_json(
        roughcut,
        runtime_path,
        "diagnostics",
    )
    assert before_diagnostics["alignment"]["status"] == "available"
    assert before_diagnostics["alignment"]["configured_provider"] == "audalign"
    assert before_diagnostics["alignment"]["production_ready"] is True
    before_runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    manifest_path = managed_root / "component-manifest.json"
    before_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    before_external_manifest_sha256 = _sha256(external_components)
    before_component_records = {
        item["name"]: item for item in before_manifest["components"]
    }

    roughcut.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' '{\"ok\":true,\"schema_version\":0,"
        "\"core_version\":\"0.2.3\",\"tool_schema_version\":0}'\n",
        encoding="utf-8",
    )
    roughcut.chmod(0o755)
    upgraded = _run_bootstrap_cli(
        bootstrap,
        ["--install-dir", str(install_root), "--core-wheel", str(core_wheel), "--json"],
    )
    assert upgraded["core_action"] == "updated"
    current_health = _installed_cli_json(roughcut, runtime_path, "health")
    assert current_health["core_version"] == upgraded["core_version"]

    network_calls: list[str] = []

    def fail_network(url: object, *_args: object, **_kwargs: object) -> object:
        network_calls.append(str(url))
        raise AssertionError("production-upgrade CLI E2E attempted HTTP")

    monkeypatch.setattr(urllib.request, "urlopen", fail_network)
    cached_common = [
        "--install-dir",
        str(install_root),
        "--managed-root",
        str(managed_root),
        "--external-components",
        str(external_components),
        "--external-funasr-python",
        str(external_funasr),
        "--ffmpeg-command",
        str(ffmpeg),
        "--ffprobe-command",
        str(ffprobe),
        "--component-cache",
        str(output / "component-cache" / "audalign"),
        "--verify-components",
        "--include-audalign",
        "--json",
    ]
    cached_plan = _run_bootstrap_cli(bootstrap, cached_common)
    media_plan = cached_plan["media_components"]
    assert media_plan["download_bytes"] == 0
    operation = cached_plan["installation_operation"]
    assert operation["approved_plan_hash"] == media_plan["plan_hash"]

    cached_apply = _run_bootstrap_cli(
        bootstrap,
        [
            *cached_common,
            "--apply-components",
            "--approved-plan-hash",
            media_plan["plan_hash"],
            "--operation-id",
            operation["operation_id"],
        ],
    )
    applied_operation = cached_apply["media_components"]["operation"]
    assert applied_operation["status"] == "succeeded"
    assert applied_operation["operation_id"] == operation["operation_id"]
    assert cached_apply["media_components"]["readback"] is False

    operation_status = _run_bootstrap_cli(
        bootstrap,
        [
            "--install-dir",
            str(install_root),
            "--operation-status",
            operation["operation_id"],
            "--json",
        ],
    )
    assert operation_status["operation"] == applied_operation

    component_health = _run_bootstrap_cli(
        bootstrap,
        [*cached_common[:-1], "--component-health", "--json"],
    )
    assert component_health["media_components"]["reusable"] is True
    assert component_health["media_components"]["runtime_binding"]["configured"] is True
    after_diagnostics = _installed_cli_json(roughcut, runtime_path, "diagnostics")
    assert after_diagnostics["alignment"]["status"] == "available"
    assert after_diagnostics["alignment"]["configured_provider"] == "audalign"
    assert after_diagnostics["alignment"]["configured_provider_version"] == "1.3.1"
    assert after_diagnostics["alignment"]["production_ready"] is True
    assert after_diagnostics["runtime_binding"]["path"] == str(runtime_path)

    after_runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    after_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    after_component_records = {
        item["name"]: item for item in after_manifest["components"]
    }
    assert all(
        after_component_records[name] == record
        for name, record in before_component_records.items()
    )
    assert after_runtime["python"] == before_runtime["python"]
    assert after_runtime["components"] == before_runtime["components"]
    assert after_runtime["ffmpeg"] == before_runtime["ffmpeg"]
    assert after_runtime["ffprobe"] == before_runtime["ffprobe"]
    assert _sha256(external_components) == before_external_manifest_sha256
    result_ref = applied_operation["result_ref"]
    assert result_ref["approved_plan_hash"] == media_plan["plan_hash"]
    assert result_ref["runtime_binding_sha256"] == _sha256(runtime_path)
    assert result_ref["component_manifest_sha256"] == _sha256(manifest_path)
    assert network_calls == []

    alignment_interpreter = Path(after_diagnostics["alignment"]["interpreter"])
    import_check = subprocess.run(
        [
            str(alignment_interpreter),
            "-I",
            "-c",
            "import audalign",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert import_check.returncode == 0, import_check.stderr

def test_production_upgrade_archive_is_deterministic(tmp_path: Path) -> None:
    """Two independent builds from same HEAD must yield identical archive SHA256."""
    # Use the real Audalign cache.
    audalign_cache = Path.home() / ".roughcut" / "component-cache"
    if not (audalign_cache / "artifacts").is_dir():
        pytest.skip("audalign component-cache not available for determinism test")
    first = tmp_path / "first" / "roughcut-macos-arm64-0.2.7-production-upgrade"
    second = tmp_path / "second" / "roughcut-macos-arm64-0.2.7-production-upgrade"
    first.parent.mkdir(parents=True, exist_ok=True)
    second.parent.mkdir(parents=True, exist_ok=True)
    # Use the same source_commit for determinism
    commit = "a" * 40
    from scripts.build_production_upgrade import assemble as prod_assemble
    _out1, archive1 = prod_assemble(
        REPOSITORY_ROOT / "core",
        audalign_cache,
        first,
        source_commit=commit,
    )
    _out2, archive2 = prod_assemble(
        REPOSITORY_ROOT / "core",
        audalign_cache,
        second,
        source_commit=commit,
    )
    assert archive1.read_bytes() == archive2.read_bytes()
    assert _sha256(archive1) == _sha256(archive2)
    # Also verify that archive members are normalized (mtime 0, uid/gid 0, etc.)
    import tarfile
    with tarfile.open(archive1, "r:gz") as tf:
        for member in tf.getmembers():
            assert member.mtime == 0, f"member {member.name} mtime not normalized"
            assert member.uid == 0 and member.gid == 0, f"member {member.name} uid/gid not normalized"
            assert member.uname == "" and member.gname == "", f"member {member.name} uname/gname not normalized"
