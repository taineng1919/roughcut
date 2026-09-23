"""Code build identity: source_commit REUSE/UPDATE without Git self-reference."""

from __future__ import annotations

import json
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts import bootstrap as bootstrap_script
from scripts.build_core_release import assemble as assemble_core

ROOT = Path(__file__).resolve().parents[2]
CORE_SRC = ROOT / "core" / "src"


def _read_health_source_commit() -> object:
    sys.path.insert(0, str(CORE_SRC))
    try:
        from roughcut.application.health import health

        return health()["source_commit"]
    finally:
        try:
            sys.path.remove(str(CORE_SRC))
        except ValueError:
            pass


def test_tracked_source_reports_dev_identity() -> None:
    # Tracked source must never contain a SHA (no Git self-reference).
    text = (CORE_SRC / "roughcut" / "_build_identity.py").read_text(encoding="utf-8")
    assert "SOURCE_COMMIT: str | None = None" in text
    assert _read_health_source_commit() is None


def test_bootstrap_identity_requires_source_commit_equality() -> None:
    base = bootstrap_script.current_core_identity()
    assert "source_commit" in base
    # Same version + different commit must not compare equal (UPDATE).
    other = dict(base)
    other["source_commit"] = "b" * 40 if base["source_commit"] != "b" * 40 else "c" * 40
    assert base != other


def test_build_identity_file_parsing(tmp_path: Path) -> None:
    sha_file = tmp_path / "_build_identity.py"
    sha_file.write_text('SOURCE_COMMIT: str | None = "' + "e" * 40 + '"\n', encoding="utf-8")
    assert (
        bootstrap_script._source_commit_from_build_identity_file(sha_file) == "e" * 40
    )
    dev_file = tmp_path / "dev_identity.py"
    dev_file.write_text("SOURCE_COMMIT: str | None = None\n", encoding="utf-8")
    assert bootstrap_script._source_commit_from_build_identity_file(dev_file) is None


def test_installed_health_reuse_and_update_on_source_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    import subprocess

    expected = {
        "schema_version": 1,
        "core_version": "0.2.8",
        "tool_schema_version": 32,
        "source_commit": "a" * 40,
    }
    launcher = tmp_path / "roughcut"
    launcher.write_text("fixture", encoding="utf-8")

    def run_matching(command: list[str], **_kwargs: object):
        assert command[1:] == ["health", "--json"]
        payload = {**expected, "ok": True}
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr(bootstrap_script.subprocess, "run", run_matching)
    assert (
        bootstrap_script._installed_core_health_error(launcher, expected) is None
    )

    def run_stale(command: list[str], **_kwargs: object):
        payload = {**expected, "source_commit": "b" * 40, "ok": True}
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr(bootstrap_script.subprocess, "run", run_stale)
    assert (
        bootstrap_script._installed_core_health_error(launcher, expected)
        == "installed health identity does not match the current core"
    )


def test_bootstrap_json_responses_carry_source_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse
    import subprocess

    cache = tmp_path / "cache"
    cache.mkdir()
    source = tmp_path / "artifact.whl"
    source.write_bytes(b"0123456789")
    provenance = {
        "schema_version": 1,
        "kind": "roughcut_component_cache_population",
        "filename": "artifact.whl",
        "tier": "local",
        "sha256": "x",
        "size": 10,
        "reused": False,
        "cache_path": str(cache),
    }
    monkeypatch.setattr(
        bootstrap_script, "populate_cache_from_channel", lambda *a, **k: provenance
    )
    args = argparse.Namespace(
        operation_status=None,
        managed_root=None,
        core_wheel=None,
        external_components=None,
        external_funasr_python=None,
        local_component_bundle=None,
        component_cache=cache,
        apply_components=False,
        approved_plan_hash=None,
        operation_id=None,
        component_health=False,
        target_platform=None,
        target_architecture=None,
        verify_components=False,
        include_audalign=False,
        include_bbc_audio_offset_finder=False,
        populate_cache=True,
        populate_filename="artifact.whl",
        populate_source=source,
        populate_tier="local",
    )
    result = bootstrap_script._execute_bootstrap_args(args)
    assert result["ok"] is True
    assert result["action"] == "populate-cache"
    assert "source_commit" in result
    assert result["populated"] == provenance


def test_bootstrap_wheel_validation_enforces_source_commit(tmp_path: Path) -> None:
    expected = {
        "schema_version": 1,
        "core_version": "0.2.8",
        "tool_schema_version": 32,
        "source_commit": "a" * 40,
    }
    wheel_dir = tmp_path / "wheels"
    wheel_dir.mkdir()
    good = wheel_dir / "roughcut-0.2.8-py3-none-any.whl"
    with ZipFile(good, "w") as archive:
        archive.writestr(
            "roughcut-0.2.8.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: roughcut\nVersion: 0.2.8\n",
        )
        archive.writestr(
            "roughcut/_build_identity.py",
            'SOURCE_COMMIT: str | None = "' + "a" * 40 + '"\n',
        )
    assert bootstrap_script._validate_core_wheel(
        good.absolute(), expected_identity=expected
    ).is_file()

    bad = wheel_dir / "good-name-but-checked-separately.whl"
    # Reuse the good wheel bytes under a second path with mismatched expectation.
    mismatched = dict(expected)
    mismatched["source_commit"] = "b" * 40
    try:
        bootstrap_script._validate_core_wheel(
            good.absolute(), expected_identity=mismatched
        )
    except RuntimeError as error:
        assert "source_commit" in str(error)
    else:
        raise AssertionError("mismatched source_commit wheel must fail validation")


def test_shared_staging_shares_one_identity(tmp_path: Path) -> None:
    out = tmp_path / "code"
    sha = "d" * 40
    source_bundle, wheel = assemble_core(ROOT / "core", out, source_commit=sha)
    with ZipFile(wheel) as archive:
        wheel_text = archive.read("roughcut/_build_identity.py").decode("utf-8")
    with tarfile.open(source_bundle, mode="r:gz") as bundle:
        names = [
            member.name
            for member in bundle.getmembers()
            if member.name.endswith("roughcut/_build_identity.py")
        ]
        assert len(names) == 1
        extracted = bundle.extractfile(names[0])
        assert extracted is not None
        bundle_text = extracted.read().decode("utf-8")
    assert f'"{sha}"' in wheel_text
    assert f'"{sha}"' in bundle_text


def test_release_rejects_invalid_source_commit(tmp_path: Path) -> None:
    from scripts.build_core_release import ReleaseAssemblyError

    try:
        assemble_core(ROOT / "core", tmp_path / "out", source_commit="not-a-sha")
    except ReleaseAssemblyError as error:
        assert "source_commit" in str(error)
    else:
        raise AssertionError("invalid source_commit must fail closed")
