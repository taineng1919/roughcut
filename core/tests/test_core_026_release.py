from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from scripts.build_core_release import (
    CORE_VERSION,
    SOURCE_BUNDLE_NAME,
    SOURCE_BUNDLE_ROOT,
    WHEEL_NAME,
    _source_bundle_files,
    assemble,
)
from scripts.build_core_update import (
    CORE_DIRECTORY_NAME,
    CORE_PAYLOAD_NAMES,
    MANIFEST_NAME,
    ROOT_CHECKSUMS_NAME,
    ROOT_PAYLOAD_NAMES,
    UPDATE_FOLDER_NAME,
    ReleaseAssemblyError,
)
from scripts.build_core_update import (
    assemble as assemble_core_update,
)

ROOT = Path(__file__).resolve().parents[2]
BUILD_RELEASE = ROOT / "scripts" / "build_core_release.py"


@pytest.fixture(scope="module")
def release_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("core-release") / f"Core-{CORE_VERSION}"
    result = subprocess.run(
        [sys.executable, str(BUILD_RELEASE), "--output", str(output)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return output


def test_release_contains_only_the_standalone_core_unit_files(
    release_root: Path,
) -> None:
    assert {path.name for path in release_root.iterdir()} == {
        WHEEL_NAME,
        SOURCE_BUNDLE_NAME,
        "CORE-SHA256SUMS",
        "CORE-UPGRADE-AGENT.md",
    }


def test_upgrade_guide_uses_only_the_standalone_core_folder(
    release_root: Path,
) -> None:
    guide = (release_root / "CORE-UPGRADE-AGENT.md").read_text(encoding="utf-8")

    assert "set -eu" in guide
    assert f"cd /absolute/path/to/Core-{CORE_VERSION}" in guide
    assert 'CORE_PACKAGE_DIR="$PWD"' in guide
    assert f'CORE_WHEEL="$CORE_PACKAGE_DIR/{WHEEL_NAME}"' in guide
    assert f'tar -xzf "$CORE_PACKAGE_DIR/{SOURCE_BUNDLE_NAME}"' in guide
    assert f'CORE_ROOT="$CORE_TMP/{SOURCE_BUNDLE_ROOT}"' in guide
    assert 'python3.11 "$CORE_ROOT/scripts/bootstrap.py"' in guide
    assert '"$INSTALL_DIR/venv/bin/roughcut" health --json' in guide
    assert '"$INSTALL_DIR/venv/bin/roughcut" diagnostics --json' in guide
    assert 'MCP_COMMAND="$INSTALL_DIR/venv/bin/roughcut-mcp"' in guide
    assert (
        'python3.11 "$CORE_ROOT/scripts/build_host_package.py" \\\n  --host workbuddy'
        in guide
    )
    assert (
        'python3.11 "$CORE_ROOT/scripts/build_host_package.py" \\\n  --host codex'
        in guide
    )
    assert '--mcp-command "$MCP_COMMAND"' in guide
    assert "Large Components" not in guide
    assert "roughcut-macos-arm64-0.2.2-split-trial" not in guide


def test_wheel_contains_review_assets_catalog_and_entry_points(
    release_root: Path,
) -> None:
    with ZipFile(release_root / WHEEL_NAME) as archive:
        names = set(archive.namelist())
        dist_info = f"roughcut-{CORE_VERSION}.dist-info"
        metadata = archive.read(f"{dist_info}/METADATA").decode()
        wheel = archive.read(f"{dist_info}/WHEEL").decode()
        entry_points = archive.read(f"{dist_info}/entry_points.txt").decode()

    assert "Name: roughcut\n" in metadata
    assert f"Version: {CORE_VERSION}\n" in metadata
    assert "Root-Is-Purelib: true" in wheel
    assert "roughcut = roughcut.cli:main" in entry_points
    assert "roughcut-mcp = roughcut.mcp:main" in entry_points
    assert "roughcut/review/static/index.html" in names
    assert any(name.endswith(".js") for name in names if "/review/static/assets/" in name)
    assert any(name.endswith(".css") for name in names if "/review/static/assets/" in name)
    assert "roughcut/component_catalog/release-catalog.json" in names
    assert "roughcut/component_catalog/models.json" in names


def _assert_release_checksums(release_root: Path) -> None:
    entries = {}
    for line in (release_root / "CORE-SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, filename = line.split("  ", 1)
        entries[filename] = digest

    assert set(entries) == {SOURCE_BUNDLE_NAME, WHEEL_NAME}
    for filename, expected in entries.items():
        actual = hashlib.sha256((release_root / filename).read_bytes()).hexdigest()
        assert actual == expected


def test_core_checksums_cover_source_bundle_and_wheel(release_root: Path) -> None:
    _assert_release_checksums(release_root)


def test_source_bundle_contains_release_sources_and_no_development_payload(
    release_root: Path,
) -> None:
    with tarfile.open(release_root / SOURCE_BUNDLE_NAME, mode="r:gz") as archive:
        names = {member.name for member in archive.getmembers()}

    required = {
        f"{SOURCE_BUNDLE_ROOT}/scripts/bootstrap.py",
        f"{SOURCE_BUNDLE_ROOT}/scripts/build_host_package.py",
        f"{SOURCE_BUNDLE_ROOT}/scripts/build_core_update.py",
        f"{SOURCE_BUNDLE_ROOT}/scripts/release_asr_smoke.py",
        f"{SOURCE_BUNDLE_ROOT}/scripts/validate_agent_skills.py",
        f"{SOURCE_BUNDLE_ROOT}/agent-skill/skills/roughcut/SKILL.md",
        f"{SOURCE_BUNDLE_ROOT}/host-integrations/codex/.mcp.json",
        f"{SOURCE_BUNDLE_ROOT}/host-integrations/workbuddy/.mcp.json",
        f"{SOURCE_BUNDLE_ROOT}/core/pyproject.toml",
        f"{SOURCE_BUNDLE_ROOT}/core/src/roughcut/__init__.py",
        f"{SOURCE_BUNDLE_ROOT}/docs/agent-tool-contract.md",
        f"{SOURCE_BUNDLE_ROOT}/docs/installation.md",
        f"{SOURCE_BUNDLE_ROOT}/docs/release/PRODUCT_PACKAGING.md",
    }
    assert required <= names
    assert not any("/docs/release/macos-arm64-trial-" in name for name in names)
    forbidden = ("/tests/", "/build/", "/__pycache__/", ".pyc", ".egg-info/", "/.venv/")
    assert not any(any(marker in name for marker in forbidden) for name in names)
    assert not any(
        marker in name for name in names for marker in ("Large-Components", "component-cache")
    )


def test_source_bundle_excludes_ignored_virtualenv_with_symlinks(
    tmp_path: Path,
) -> None:
    """A gitignored ``core/.venv`` whose interpreter is a symlink must not
    break the source-bundle walk and must never enter the bundle."""
    source_root = tmp_path / "source"
    for directory in (
        "core/src/roughcut",
        "agent-skill",
        "host-integrations",
        "scripts",
        "docs/release",
    ):
        (source_root / directory).mkdir(parents=True, exist_ok=True)
    sentinel_files = [
        "README.md",
        "scripts/bootstrap.py",
        "scripts/build_core_release.py",
        "scripts/build_core_update.py",
        "scripts/build_host_package.py",
        "scripts/release_asr_smoke.py",
        "scripts/uninstall.py",
        "scripts/validate_agent_skills.py",
        "docs/agent-tool-contract.md",
        "docs/installation.md",
        "docs/release/PRODUCT_PACKAGING.md",
    ]
    for relative in sentinel_files + [
        "core/pyproject.toml",
        "core/src/roughcut/__init__.py",
    ]:
        (source_root / relative).write_text("sentinel\n", encoding="utf-8")

    venv_bin = source_root / "core" / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    real_interpreter = tmp_path / "real-python"
    real_interpreter.write_text("#!/bin/sh\n", encoding="utf-8")
    (venv_bin / "python").symlink_to(real_interpreter)
    (venv_bin / "activate").write_text("sentinel\n", encoding="utf-8")
    (source_root / "core" / ".venv" / "sentinel.txt").write_text(
        "sentinel\n", encoding="utf-8"
    )

    files = _source_bundle_files(source_root)

    assert not any(name.startswith("core/.venv") for name in files)
    assert f"{SOURCE_BUNDLE_ROOT}/core/src/roughcut/__init__.py" in {
        name for name in files
    } or "core/src/roughcut/__init__.py" in files


def test_core_release_assembly_is_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    assemble(ROOT / "core", first)
    assemble(ROOT / "core", second)

    for filename in (SOURCE_BUNDLE_NAME, WHEEL_NAME):
        assert (first / filename).read_bytes() == (second / filename).read_bytes()


def test_core_update_assembly_has_exact_root_and_manifest_facts(tmp_path: Path) -> None:
    output = tmp_path / UPDATE_FOLDER_NAME
    source_commit = "a" * 40
    assemble_core_update(ROOT / "core", output, source_commit=source_commit)

    assert {path.name for path in output.iterdir()} == {
        CORE_DIRECTORY_NAME,
        "INSTALL-AGENT.md",
        ROOT_CHECKSUMS_NAME,
        MANIFEST_NAME,
    }
    assert {path.name for path in (output / CORE_DIRECTORY_NAME).iterdir()} == set(
        CORE_PAYLOAD_NAMES
    )
    assert not (output / "README.md").exists()
    assert not (output / "Large-Components-macOS-arm64-r1").exists()
    assert not (output / "Core-0.2.3").exists()

    manifest = json.loads((output / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["release_schema"] == "roughcut.core-update-folder.v1"
    assert manifest["kind"] == "core-update-folder"
    assert manifest["offline_update"] is True
    assert manifest["release_name"] == UPDATE_FOLDER_NAME
    assert manifest["core"]["version"] == CORE_VERSION
    assert manifest["core"]["directory"] == CORE_DIRECTORY_NAME
    assert manifest["core"]["git_commit"] == source_commit
    assert manifest["core"]["offline_update"] is True
    assert manifest["large_components"] == {
        "included": False,
        "independent_source_required": True,
        "rebuilt": False,
    }
    assert {entry["path"] for entry in manifest["core"]["files"]} == set(
        ROOT_PAYLOAD_NAMES[: len(CORE_PAYLOAD_NAMES)]
    )
    assert manifest["root"]["payload_files"] == list(ROOT_PAYLOAD_NAMES)

    root_entries = {
        line.split("  ", 1)[1]
        for line in (output / ROOT_CHECKSUMS_NAME).read_text(encoding="utf-8").splitlines()
    }
    assert root_entries == set(ROOT_PAYLOAD_NAMES)
    assert ROOT_CHECKSUMS_NAME not in root_entries


def test_core_update_install_guide_is_core_only_and_has_no_guessed_large_path(
    tmp_path: Path,
) -> None:
    output = tmp_path / UPDATE_FOLDER_NAME
    assemble_core_update(ROOT / "core", output, source_commit="b" * 40)
    guide = (output / "INSTALL-AGENT.md").read_text(encoding="utf-8")

    for phrase in (
        "Core-only",
        "不联网",
        "不从 source 构建",
        "runtime.json",
        "components",
        "component-cache",
        "source-detachment",
        "WorkBuddy Host Package",
        "缺少组件时停止",
        "独立的离线 Large Components",
    ):
        assert phrase in guide
    assert "Large-Components-macOS-arm64-r1" not in guide
    assert "LARGE_DIR" not in guide
    assert "../" not in guide
    assert "--managed-root" not in guide
    assert "--component-cache" not in guide


def test_core_update_assembly_is_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    source_commit = "c" * 40
    assemble_core_update(ROOT / "core", first, source_commit=source_commit)
    assemble_core_update(ROOT / "core", second, source_commit=source_commit)

    def snapshot(root: Path) -> dict[str, bytes]:
        return {
            str(path.relative_to(root)): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    assert snapshot(first) == snapshot(second)


def test_core_update_assembly_rejects_non_empty_destination(tmp_path: Path) -> None:
    output = tmp_path / "non-empty"
    output.mkdir()
    sentinel = output / "sentinel"
    sentinel.write_bytes(b"keep")

    with pytest.raises(ReleaseAssemblyError, match="output directory must be empty"):
        assemble_core_update(ROOT / "core", output, source_commit="d" * 40)
    assert sentinel.read_bytes() == b"keep"


def _snapshot_files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _write_old_core_wheel(root: Path) -> Path:
    wheel = root / "roughcut-0.2.3-py3-none-any.whl"
    with ZipFile(wheel, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(
            "roughcut/__init__.py",
            '__version__ = "0.2.3"\n',
        )
        archive.writestr(
            "roughcut/cli.py",
            """
import json
import sys

def main():
    if sys.argv[1:2] == ["health"]:
        print(json.dumps({
            "schema_version": 1,
            "core_version": "0.2.3",
            "tool_schema_version": 27,
            "ok": True,
        }))
    else:
        raise SystemExit(2)
""",
        )
        archive.writestr(
            "roughcut/mcp.py",
            "def main():\n    raise SystemExit(2)\n",
        )
        archive.writestr(
            "roughcut-0.2.3.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: roughcut\nVersion: 0.2.3\n",
        )
        archive.writestr(
            "roughcut-0.2.3.dist-info/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(
            "roughcut-0.2.3.dist-info/entry_points.txt",
            "[console_scripts]\nroughcut = roughcut.cli:main\nroughcut-mcp = roughcut.mcp:main\n",
        )
        archive.writestr("roughcut-0.2.3.dist-info/RECORD", "")
    return wheel


def test_isolated_offline_e2e_updates_controlled_old_wheel_without_large_components(
    release_root: Path,
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "install-root"
    cache_dir = tmp_path / "empty-pip-cache"
    cache_dir.mkdir()
    old_wheel = _write_old_core_wheel(tmp_path)
    _assert_release_checksums(release_root)
    source_extract = tmp_path / "source-extract"
    source_extract.mkdir()
    with tarfile.open(release_root / SOURCE_BUNDLE_NAME, mode="r:gz") as archive:
        archive.extractall(source_extract)
    core_root = source_extract / SOURCE_BUNDLE_ROOT
    source_bootstrap = core_root / "scripts" / "bootstrap.py"
    assert source_bootstrap.is_file()
    assert core_root != ROOT
    assert source_bootstrap != ROOT / "scripts" / "bootstrap.py"
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME"}
    }
    env.update(
        {
            "PIP_NO_INDEX": "1",
            "PIP_CACHE_DIR": str(cache_dir),
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
        }
    )

    create_venv = subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(install_dir / "venv")],
        cwd=core_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert create_venv.returncode == 0, create_venv.stderr
    install_old = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "--python",
            str(install_dir / "venv"),
            "install",
            "--disable-pip-version-check",
            "--no-index",
            "--no-deps",
            str(old_wheel),
        ],
        cwd=core_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert install_old.returncode == 0, install_old.stderr

    # Create recognizable runtime.json and components fixture before Core-only update
    runtime_path = install_dir / "runtime.json"
    runtime_content = (
        json.dumps(
            {
                "schema_version": 2,
                "ffmpeg": "/tmp/fake-ffmpeg",
                "ffprobe": "/tmp/fake-ffprobe",
                "components": {"asr": "fixture"},
            },
            sort_keys=True,
        )
        + "\n"
    ).encode()
    runtime_path.write_bytes(runtime_content)
    components_root = install_dir / "components"
    (components_root / "models" / "asr").mkdir(parents=True)
    (components_root / "models" / "asr" / "weights.bin").write_bytes(b"fixture weights 0.2.3")
    (components_root / "component-manifest.json").write_text(
        '{"schema_version":1,"components":[]}\n', encoding="utf-8"
    )
    before_runtime = runtime_path.read_bytes()
    before_components = _snapshot_files(components_root)

    update = subprocess.run(
        [
            sys.executable,
            str(source_bootstrap),
            "--install-dir",
            str(install_dir),
            "--core-wheel",
            str(release_root / WHEEL_NAME),
            "--json",
        ],
        cwd=core_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert update.returncode == 0, update.stderr
    update_payload = json.loads(update.stdout)
    assert update_payload["ok"] is True
    assert update_payload["core_action"] == "updated"
    assert "media_components" not in update_payload
    # Core-only update must not modify runtime.json / components bytes
    assert runtime_path.read_bytes() == before_runtime
    assert _snapshot_files(components_root) == before_components

    # Build WorkBuddy Host Package from inside the assembled source bundle (as release doc requires)
    workbuddy_package = tmp_path / "workbuddy-host-package"
    workbuddy_build = subprocess.run(
        [
            sys.executable,
            str(core_root / "scripts" / "build_host_package.py"),
            "--output",
            str(workbuddy_package),
            "--host",
            "workbuddy",
            "--mcp-command",
            str(update_payload["mcp_command"]),
        ],
        cwd=core_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert workbuddy_build.returncode == 0, workbuddy_build.stderr
    assert (workbuddy_package / ".mcp.json").is_file()
    assert (workbuddy_package / "skills" / "roughcut" / "SKILL.md").is_file()
    assert (workbuddy_package / "skills" / "roughcut-basics" / "SKILL.md").is_file()
    # WorkBuddy package must bind the same absolute MCP command
    workbuddy_mcp = json.loads((workbuddy_package / ".mcp.json").read_text(encoding="utf-8"))
    assert workbuddy_mcp["mcpServers"]["roughcut"]["command"] == str(update_payload["mcp_command"])

    host_package = tmp_path / "codex-host-package"
    host_build = subprocess.run(
        [
            sys.executable,
            str(core_root / "scripts" / "build_host_package.py"),
            "--output",
            str(host_package),
            "--host",
            "codex",
            "--mcp-command",
            str(update_payload["mcp_command"]),
        ],
        cwd=core_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert host_build.returncode == 0, host_build.stderr
    assert (host_package / ".mcp.json").is_file()
    assert (host_package / ".codex-plugin" / "plugin.json").is_file()
    assert (host_package / "skills" / "roughcut" / "SKILL.md").is_file()

    roughcut = install_dir / "venv" / "bin" / "roughcut"
    health = subprocess.run(
        [str(roughcut), "health", "--json"],
        cwd=core_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    diagnostics = subprocess.run(
        [str(roughcut), "diagnostics", "--json"],
        cwd=core_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert health.returncode == 0, health.stderr
    assert diagnostics.returncode == 0, diagnostics.stderr
    assert json.loads(health.stdout)["core_version"] == CORE_VERSION
    assert json.loads(health.stdout)["tool_schema_version"] == 32
    assert json.loads(diagnostics.stdout)["ok"] is True
