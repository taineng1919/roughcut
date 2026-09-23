from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import scripts.bootstrap as bootstrap_module

from roughcut.adapters import component_download, component_environment, component_installation
from roughcut.adapters.component_installation import (
    ComponentInstallError,
    RuntimePublicationError,
    StaleApprovedPlanError,
)
from roughcut.application.installation_operations import (
    installation_operation_status,
    run_component_installation,
)
from roughcut.domain.installation_operation import (
    InstallationOperationError,
    InstallationResultRef,
)

ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = ROOT / "scripts" / "bootstrap.py"


class _FixtureComponentError(RuntimeError):
    pass


def _args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "install_dir": Path("/unused/install"),
        "managed_root": None,
        "external_components": None,
        "external_funasr_python": None,
        "local_component_bundle": None,
        "component_cache": None,
        "apply_components": False,
        "approved_plan_hash": None,
        "operation_id": None,
        "operation_status": None,
        "component_health": False,
        "ffmpeg_command": "ffmpeg",
        "ffprobe_command": "ffprobe",
        "target_platform": None,
        "target_architecture": None,
        "verify_components": False,
        "include_audalign": False,
        "include_bbc_audio_offset_finder": False,
        "core_wheel": None,
        "json": True,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_bootstrap_full_plan_generates_operation_id_but_quick_plan_does_not(
    tmp_path: Path,
) -> None:
    common = [
        sys.executable,
        str(BOOTSTRAP),
        "--install-dir",
        str(tmp_path / "install"),
        "--managed-root",
        str(tmp_path / "managed"),
        "--component-cache",
        str(tmp_path / "cache"),
        "--ffmpeg-command",
        "roughcut-missing-ffmpeg",
        "--ffprobe-command",
        "roughcut-missing-ffprobe",
        "--json",
    ]
    quick = subprocess.run(
        common,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    full = subprocess.run(
        [*common[:-1], "--verify-components", "--json"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    quick_payload = json.loads(quick.stdout)
    full_payload = json.loads(full.stdout)
    assert quick.returncode == full.returncode == 0
    assert quick_payload["media_components"]["verification_mode"] == "quick"
    assert "audalign_fingerprint" not in quick_payload["media_components"]
    assert "audalign_fingerprint" not in quick_payload["media_components"][
        "missing_managed_groups"
    ]
    assert "installation_operation" not in quick_payload
    assert full_payload["media_components"]["verification_mode"] == "full"
    assert "audalign_fingerprint" not in full_payload["media_components"]
    assert "audalign_fingerprint" not in full_payload["media_components"][
        "missing_managed_groups"
    ]
    operation = full_payload["installation_operation"]
    assert operation["operation_id"].startswith("op_")
    assert operation["approved_plan_hash"] == full_payload["media_components"][
        "plan_hash"
    ]
    assert not (tmp_path / "install").exists()
    assert not (tmp_path / "managed").exists()
    assert not (tmp_path / "cache").exists()


def test_cross_target_windows_full_plan_has_no_operation_or_writes(
    tmp_path: Path,
) -> None:
    result = bootstrap_module._execute_bootstrap_args(
        _args(
            install_dir=tmp_path / "install",
            managed_root=tmp_path / "managed",
            component_cache=tmp_path / "cache",
            target_platform="windows",
            target_architecture="x86_64",
            verify_components=True,
            ffmpeg_command="roughcut-missing-ffmpeg",
            ffprobe_command="roughcut-missing-ffprobe",
        )
    )

    plan = result["media_components"]
    assert isinstance(plan, dict)
    assert plan["target"]["apply_supported_on_this_host"] is False
    assert plan["prerequisites"]["windows_vc_runtime_x64"]["status"] == (
        "unverifiable_cross_target"
    )
    assert all(
        budget["status"] == "not_applicable" for budget in plan["path_budget"].values()
    )
    assert result["installation_operation"] is None
    assert not (tmp_path / "install").exists()
    assert not (tmp_path / "managed").exists()
    assert not (tmp_path / "cache").exists()


def test_bootstrap_apply_requires_operation_id_and_full_plan(
    tmp_path: Path,
) -> None:
    base = [
        sys.executable,
        str(BOOTSTRAP),
        "--install-dir",
        str(tmp_path / "install"),
        "--managed-root",
        str(tmp_path / "managed"),
        "--component-cache",
        str(tmp_path / "cache"),
        "--apply-components",
        "--approved-plan-hash",
        "a" * 64,
        "--json",
    ]
    missing_operation = subprocess.run(
        [*base[:-1], "--verify-components", "--json"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    quick_apply = subprocess.run(
        [*base[:-1], "--operation-id", "op_fixture", "--json"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert missing_operation.returncode == quick_apply.returncode == 1
    assert "operation-id" in json.loads(missing_operation.stdout)["error"]["message"]
    assert "full plan" in json.loads(quick_apply.stdout)["error"]["message"]
    assert not (tmp_path / "install").exists()
    assert not (tmp_path / "managed").exists()
    assert not (tmp_path / "cache").exists()


def test_bootstrap_component_apply_records_success_and_response_loss_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_root = tmp_path / "install"
    managed_root = tmp_path / "managed"
    cache_root = tmp_path / "cache"
    calls = 0

    def fake_apply(
        _managed_root: Path,
        _cache_root: Path,
        **kwargs: object,
    ) -> object:
        nonlocal calls
        calls += 1
        assert kwargs["include_audalign"] is True
        update = kwargs["phase_callback"]
        assert callable(update)
        for phase in (
            "component_installation_preparing",
            "component_installation_downloading",
            "component_installation_installing",
            "component_installation_verifying",
            "component_installation_publishing_runtime",
        ):
            update(phase)
        install_root.mkdir(parents=True, exist_ok=True)
        (install_root / "runtime.json").write_text(
            '{"schema_version":1}\n',
            encoding="utf-8",
        )
        managed_root.mkdir(parents=True, exist_ok=True)
        manifest = managed_root / "component-manifest.json"
        manifest.write_text('{"schema_version":2}\n', encoding="utf-8")
        return SimpleNamespace(manifest_path=str(manifest))

    monkeypatch.setattr(
        bootstrap_module,
        "_component_install_api",
        lambda: (_FixtureComponentError, fake_apply, object()),
    )
    first = bootstrap_module.apply_media_components(
        managed_root,
        cache_root,
        install_root=install_root,
        approved_plan_hash="a" * 64,
        operation_id="op_bootstrap_success",
        verify_components=True,
        include_audalign=True,
    )
    repeated = bootstrap_module.apply_media_components(
        managed_root,
        cache_root,
        install_root=install_root,
        approved_plan_hash="a" * 64,
        operation_id="op_bootstrap_success",
        verify_components=True,
        include_audalign=True,
    )
    assert first["operation"]["status"] == "succeeded"  # type: ignore[index]
    assert repeated["operation"] == first["operation"]
    assert repeated["readback"] is True
    assert calls == 1
    serialized = json.dumps(repeated, ensure_ascii=False)
    assert str(install_root) not in serialized
    assert str(managed_root) not in serialized


def test_bootstrap_plan_wrapper_passes_include_audalign_to_install_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: dict[str, object] = {}

    def fake_build(
        _managed_root: Path,
        _cache_root: Path,
        **kwargs: object,
    ) -> object:
        received.update(kwargs)
        return SimpleNamespace(to_dict=lambda: {"plan_hash": "a" * 64})

    monkeypatch.setattr(
        bootstrap_module,
        "_component_install_api",
        lambda: (_FixtureComponentError, object(), fake_build),
    )

    result = bootstrap_module.plan_media_components(
        tmp_path / "managed",
        tmp_path / "cache",
        verify_components=True,
        include_audalign=True,
    )

    assert result["plan_hash"] == "a" * 64
    assert received["include_audalign"] is True


def test_bootstrap_health_wrapper_passes_include_audalign_to_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: dict[str, object] = {}

    def fake_plan(
        _managed_root: Path,
        _cache_root: Path,
        **kwargs: object,
    ) -> dict[str, object]:
        received.update(kwargs)
        return {
            "profile": "fixture",
            "target": {},
            "verification_mode": "full",
            "components": {},
            "missing_managed_groups": [],
            "user_actions": [],
        }

    monkeypatch.setattr(bootstrap_module, "plan_media_components", fake_plan)

    result = bootstrap_module.health_media_components(
        tmp_path / "managed",
        tmp_path / "cache",
        verify_components=True,
        include_audalign=True,
    )

    assert result["reusable"] is True
    assert received["include_audalign"] is True


def test_execute_bootstrap_public_paths_pass_same_include_audalign_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    def fake_plan(
        _managed_root: Path,
        _cache_root: Path,
        **kwargs: object,
    ) -> dict[str, object]:
        calls.append(("plan", kwargs["include_audalign"]))
        return {"plan_hash": "a" * 64}

    def fake_apply(
        _managed_root: Path,
        _cache_root: Path,
        **kwargs: object,
    ) -> dict[str, object]:
        calls.append(("apply", kwargs["include_audalign"]))
        return {}

    def fake_health(
        _managed_root: Path,
        _cache_root: Path,
        **kwargs: object,
    ) -> dict[str, object]:
        calls.append(("health", kwargs["include_audalign"]))
        return {}

    monkeypatch.setattr(bootstrap_module, "plan_media_components", fake_plan)
    monkeypatch.setattr(bootstrap_module, "apply_media_components", fake_apply)
    monkeypatch.setattr(bootstrap_module, "health_media_components", fake_health)
    common = {
        "install_dir": tmp_path / "install",
        "managed_root": tmp_path / "managed",
        "component_cache": tmp_path / "cache",
        "verify_components": True,
        "include_audalign": True,
    }

    plan = bootstrap_module._execute_bootstrap_args(_args(**common))
    apply = bootstrap_module._execute_bootstrap_args(
        _args(
            **common,
            apply_components=True,
            approved_plan_hash="a" * 64,
            operation_id="op_fixture",
        )
    )
    health = bootstrap_module._execute_bootstrap_args(
        _args(**common, component_health=True)
    )

    assert [plan["action"], apply["action"], health["action"]] == [
        "plan",
        "apply",
        "health",
    ]
    assert calls == [("plan", True), ("apply", True), ("health", True)]


def test_include_audalign_requires_full_verification_before_planning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def fail_plan(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal called
        called = True
        raise AssertionError("plan must not run")

    monkeypatch.setattr(bootstrap_module, "plan_media_components", fail_plan)

    with pytest.raises(RuntimeError, match="--include-audalign requires"):
        bootstrap_module._execute_bootstrap_args(
            _args(
                managed_root=tmp_path / "managed",
                component_cache=tmp_path / "cache",
                include_audalign=True,
            )
        )

    assert called is False
    assert not (tmp_path / "managed").exists()
    assert not (tmp_path / "cache").exists()


def test_include_bbc_requires_full_verification_before_planning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def fail_plan(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal called
        called = True
        raise AssertionError("plan must not run")

    monkeypatch.setattr(bootstrap_module, "plan_media_components", fail_plan)

    with pytest.raises(RuntimeError, match="--include-bbc-audio-offset-finder requires"):
        bootstrap_module._execute_bootstrap_args(
            _args(
                managed_root=tmp_path / "managed",
                component_cache=tmp_path / "cache",
                include_bbc_audio_offset_finder=True,
            )
        )

    assert called is False
    assert not (tmp_path / "managed").exists()
    assert not (tmp_path / "cache").exists()


def test_bootstrap_cli_parses_include_audalign_and_rejects_quick_plan(
    tmp_path: Path,
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(BOOTSTRAP),
            "--install-dir",
            str(tmp_path / "install"),
            "--managed-root",
            str(tmp_path / "managed"),
            "--component-cache",
            str(tmp_path / "cache"),
            "--include-audalign",
            "--json",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 1
    assert "--include-audalign requires --verify-components" in json.loads(
        result.stdout
    )["error"]["message"]
    assert not (tmp_path / "install").exists()
    assert not (tmp_path / "managed").exists()
    assert not (tmp_path / "cache").exists()


def test_operation_status_rejects_include_audalign_as_non_status_option(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def fail_status(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal called
        called = True
        raise AssertionError("status must not run")

    monkeypatch.setattr(
        bootstrap_module,
        "installation_operation_status",
        fail_status,
    )

    with pytest.raises(RuntimeError, match="pure read"):
        bootstrap_module._execute_bootstrap_args(
            _args(
                install_dir=tmp_path / "install",
                operation_status="op_fixture",
                include_audalign=True,
            )
        )

    assert called is False
    assert not (tmp_path / "install").exists()


def test_public_apply_without_approved_audalign_selection_is_stale_before_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed_root = tmp_path / "managed"
    cache_root = tmp_path / "cache"
    install_root = tmp_path / "install"
    plan = bootstrap_module._execute_bootstrap_args(
        _args(
            install_dir=install_root,
            managed_root=managed_root,
            component_cache=cache_root,
            target_platform="macos",
            target_architecture="arm64",
            verify_components=True,
            include_audalign=True,
            ffmpeg_command="roughcut-missing-ffmpeg",
            ffprobe_command="roughcut-missing-ffprobe",
        )
    )
    media_plan = plan["media_components"]
    operation = plan["installation_operation"]
    assert isinstance(media_plan, dict)
    assert isinstance(operation, dict)
    assert "audalign_fingerprint" in media_plan["missing_managed_groups"]
    assert "audalign_fingerprint" in media_plan
    download_calls = 0
    install_calls = 0

    def fail_download(*_args: object, **_kwargs: object) -> None:
        nonlocal download_calls
        download_calls += 1
        raise AssertionError("download must not run")

    def fail_install(*_args: object, **_kwargs: object) -> object:
        nonlocal install_calls
        install_calls += 1
        raise AssertionError("install must not run")

    monkeypatch.setattr(component_download, "download_artifact", fail_download)
    monkeypatch.setattr(
        component_installation,
        "_install_staged_components",
        fail_install,
    )

    with pytest.raises(StaleApprovedPlanError) as raised:
        bootstrap_module._execute_bootstrap_args(
            _args(
                install_dir=install_root,
                managed_root=managed_root,
                component_cache=cache_root,
                apply_components=True,
                approved_plan_hash=operation["approved_plan_hash"],
                operation_id=operation["operation_id"],
                verify_components=True,
            )
        )

    assert "stale component plan" in str(raised.value)
    with pytest.raises(InstallationOperationError) as missing:
        installation_operation_status(
            install_root,
            str(operation["operation_id"]),
        )
    assert missing.value.code == "operation_not_found"
    assert download_calls == 0
    assert install_calls == 0
    assert not managed_root.exists()
    assert not cache_root.exists()
    assert not (install_root / "runtime.json").exists()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink unavailable")
def test_bootstrap_apply_rejects_symlink_install_root_before_component_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "install-target"
    target.mkdir()
    install_root = tmp_path / "install-link"
    install_root.symlink_to(target, target_is_directory=True)
    calls = 0

    def fake_apply(
        _managed_root: Path,
        _cache_root: Path,
        **_kwargs: object,
    ) -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("component apply must not run")

    monkeypatch.setattr(
        bootstrap_module,
        "_component_install_api",
        lambda: (_FixtureComponentError, fake_apply, object()),
    )

    with pytest.raises(InstallationOperationError) as raised:
        bootstrap_module.apply_media_components(
            tmp_path / "managed",
            tmp_path / "cache",
            install_root=install_root,
            approved_plan_hash="a" * 64,
            operation_id="op_symlink_apply",
            verify_components=True,
        )

    assert raised.value.code == "operation_integrity_error"
    assert calls == 0
    assert list(target.iterdir()) == []
    assert not (tmp_path / "managed").exists()
    assert not (tmp_path / "cache").exists()


def test_bootstrap_runtime_publish_failure_matches_operation_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_apply(
        _managed_root: Path,
        _cache_root: Path,
        **_kwargs: object,
    ) -> object:
        raise RuntimePublicationError(
            "Roughcut runtime binding publisher failed at /private/runtime.json",
            reason_code="runtime_publish_atomic_replace_failed",
        )

    monkeypatch.setattr(
        bootstrap_module,
        "_component_install_api",
        lambda: (_FixtureComponentError, fail_apply, object()),
    )
    with pytest.raises(InstallationOperationError):
        bootstrap_module.apply_media_components(
            tmp_path / "managed",
            tmp_path / "cache",
            install_root=tmp_path / "install",
            approved_plan_hash="a" * 64,
            operation_id="op_runtime_failure",
            verify_components=True,
        )
    record = installation_operation_status(
        tmp_path / "install",
        "op_runtime_failure",
    )
    assert record.status == "failed"
    assert record.error is not None
    assert record.error.responsibility == "roughcut_runtime_binding"
    assert record.error.action == "publish_runtime_binding"
    assert record.error.reason_code == "runtime_publish_atomic_replace_failed"
    assert "/private" not in json.dumps(record.to_dict())


def test_bootstrap_preflight_adopts_exact_catalog_without_reloading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_root = tmp_path / "install"
    managed_root = tmp_path / "managed"
    cache_root = tmp_path / "cache"
    catalog = component_installation.load_release_catalog()
    ffmpeg = "roughcut-missing-ffmpeg"
    ffprobe = "roughcut-missing-ffprobe"
    approved = component_installation.build_install_plan(
        managed_root,
        cache_root,
        install_root=install_root,
        catalog=catalog,
        ffmpeg_command=ffmpeg,
        ffprobe_command=ffprobe,
        verify_components=True,
    )
    target = approved.payload["target"]
    assert isinstance(target, dict)
    profile = catalog.profile_for(
        str(target["platform"]),
        str(target["architecture"]),
    )
    replacement_artifact = replace(
        profile.runtime.artifacts[0],
        url="https://example.invalid/catalog-changed-after-preflight.whl",
    )
    replacement_profile = replace(
        profile,
        runtime=replace(
            profile.runtime,
            artifacts=(replacement_artifact, *profile.runtime.artifacts[1:]),
        ),
    )
    changed_catalog = replace(
        catalog,
        digest="f" * 64,
        profiles=tuple(
            replacement_profile if item.id == profile.id else item
            for item in catalog.profiles
        ),
    )
    catalog_loads = 0

    def changing_loader() -> object:
        nonlocal catalog_loads
        catalog_loads += 1
        return catalog if catalog_loads == 1 else changed_catalog

    real_apply = component_installation.apply_install_plan

    def apply_after_catalog_change(
        managed: Path,
        cache: Path,
        **kwargs: object,
    ) -> object:
        assert catalog_loads == 1
        assert component_installation.load_release_catalog() is changed_catalog
        assert kwargs["catalog"] is catalog
        return real_apply(managed, cache, **kwargs)

    selected_artifacts: list[object] = []

    def stop_after_selection(artifact: object, _cache: Path) -> None:
        selected_artifacts.append(artifact)
        raise ComponentInstallError("fixture stopped after exact artifact selection")

    monkeypatch.setattr(
        component_installation,
        "load_release_catalog",
        changing_loader,
    )
    monkeypatch.setattr(
        bootstrap_module,
        "_component_install_api",
        lambda: (
            ComponentInstallError,
            apply_after_catalog_change,
            component_installation.build_install_plan,
        ),
    )
    monkeypatch.setattr(component_download, "download_artifact", stop_after_selection)
    monkeypatch.setattr(
        component_installation,
        "_available_bytes",
        lambda _path: 10**15,
    )

    with pytest.raises(InstallationOperationError):
        bootstrap_module.apply_media_components(
            managed_root,
            cache_root,
            install_root=install_root,
            approved_plan_hash=approved.plan_hash,
            operation_id="op_exact_catalog_adoption",
            ffmpeg_command=ffmpeg,
            ffprobe_command=ffprobe,
            verify_components=True,
        )

    assert catalog_loads == 2
    assert selected_artifacts == [profile.runtime.artifacts[0]]
    assert selected_artifacts[0] != replacement_artifact


def test_bootstrap_stale_full_plan_fails_before_download_or_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_root = tmp_path / "install"
    managed_root = tmp_path / "managed"
    cache_root = tmp_path / "cache"
    catalog = component_installation.load_release_catalog()
    approved = component_installation.build_install_plan(
        managed_root,
        cache_root,
        install_root=install_root,
        catalog=catalog,
        ffmpeg_command="roughcut-missing-ffmpeg",
        ffprobe_command="roughcut-missing-ffprobe",
        verify_components=True,
    )
    changed_catalog = replace(catalog, digest="f" * 64)
    build_calls = 0
    apply_calls = 0

    def return_other_catalog_plan(
        _managed_root: Path,
        _cache_root: Path,
        **_kwargs: object,
    ) -> object:
        nonlocal build_calls
        build_calls += 1
        return approved

    def must_not_apply(
        _managed_root: Path,
        _cache_root: Path,
        **_kwargs: object,
    ) -> object:
        nonlocal apply_calls
        apply_calls += 1
        raise AssertionError("stale preflight must not apply")

    monkeypatch.setattr(
        bootstrap_module,
        "_component_install_api",
        lambda: (ComponentInstallError, must_not_apply, return_other_catalog_plan),
    )
    monkeypatch.setattr(
        component_installation,
        "load_release_catalog",
        lambda: changed_catalog,
    )
    with pytest.raises(StaleApprovedPlanError):
        bootstrap_module.apply_media_components(
            managed_root,
            cache_root,
            install_root=install_root,
            approved_plan_hash=approved.plan_hash,
            operation_id="op_stale_plan",
            ffmpeg_command="roughcut-missing-ffmpeg",
            ffprobe_command="roughcut-missing-ffprobe",
            verify_components=True,
        )

    with pytest.raises(InstallationOperationError) as missing:
        installation_operation_status(install_root, "op_stale_plan")
    assert missing.value.code == "operation_not_found"
    assert build_calls == 1
    assert apply_calls == 0
    assert not managed_root.exists()
    assert not cache_root.exists()
    assert not (install_root / "runtime.json").exists()


def test_public_stale_preflight_failure_is_closed_and_has_no_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install_root = tmp_path / "install"

    def reject_stale(*_args: object, **_kwargs: object) -> object:
        raise StaleApprovedPlanError("fixture stale approved plan")

    monkeypatch.setattr(bootstrap_module, "apply_media_components", reject_stale)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bootstrap.py",
            "--install-dir",
            str(install_root),
            "--managed-root",
            str(tmp_path / "managed"),
            "--component-cache",
            str(tmp_path / "cache"),
            "--apply-components",
            "--approved-plan-hash",
            "a" * 64,
            "--operation-id",
            "op_public_stale",
            "--verify-components",
            "--json",
        ],
    )

    with pytest.raises(SystemExit) as exited:
        bootstrap_module.main()

    payload = json.loads(capsys.readouterr().out)
    assert exited.value.code == 1
    assert payload["error"] == {
        "code": "component_installation_failed",
        "responsibility": "user_input",
        "action": "validate_approved_full_plan",
        "message_code": "component_installation_failed",
    }
    assert "installation_operation" not in payload
    assert not install_root.exists()


def test_public_pre_pending_keyboard_interrupt_ignores_missing_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install_root = tmp_path / "install"

    def interrupt(*_args: object, **_kwargs: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(bootstrap_module, "apply_media_components", interrupt)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bootstrap.py",
            "--install-dir",
            str(install_root),
            "--managed-root",
            str(tmp_path / "managed"),
            "--component-cache",
            str(tmp_path / "cache"),
            "--apply-components",
            "--approved-plan-hash",
            "a" * 64,
            "--operation-id",
            "op_pre_pending_interrupt",
            "--verify-components",
            "--json",
        ],
    )

    with pytest.raises(SystemExit) as exited:
        bootstrap_module.main()

    payload = json.loads(capsys.readouterr().out)
    assert exited.value.code == 130
    assert payload["error"]["code"] == "component_installation_interrupted"
    assert "installation_operation" not in payload
    assert not install_root.exists()


def test_bootstrap_operation_status_is_pure_and_does_not_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_root = tmp_path / "install"
    called = False

    def fail_plan(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("plan must not run")

    monkeypatch.setattr(bootstrap_module, "plan_media_components", fail_plan)
    with pytest.raises(InstallationOperationError) as raised:
        bootstrap_module._execute_bootstrap_args(
            _args(
                install_dir=install_root,
                operation_status="op_missing",
            )
        )
    assert raised.value.code == "operation_not_found"
    assert called is False
    assert not install_root.exists()


def test_bootstrap_cli_operation_status_reads_terminal_without_apply(
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "install"
    run_component_installation(
        install_root,
        operation_id="op_cli_status",
        approved_plan_hash="a" * 64,
        apply=lambda _update: object(),
        result_ref=lambda _result: InstallationResultRef(
            approved_plan_hash="a" * 64,
            runtime_binding_sha256="b" * 64,
            component_manifest_sha256=None,
        ),
    )
    result = subprocess.run(
        [
            sys.executable,
            str(BOOTSTRAP),
            "--install-dir",
            str(install_root),
            "--operation-status",
            "op_cli_status",
            "--json",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    payload = json.loads(result.stdout)
    assert result.returncode == 0
    assert payload["action"] == "operation_status"
    assert payload["operation"]["status"] == "succeeded"
    assert payload["operation"]["result_ref"]["runtime_binding_sha256"] == "b" * 64


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink unavailable")
def test_bootstrap_cli_operation_status_rejects_symlink_install_root(
    tmp_path: Path,
) -> None:
    target = tmp_path / "install-target"
    run_component_installation(
        target,
        operation_id="op_symlink_status",
        approved_plan_hash="a" * 64,
        apply=lambda _update: object(),
        result_ref=lambda _result: InstallationResultRef(
            approved_plan_hash="a" * 64,
            runtime_binding_sha256="b" * 64,
            component_manifest_sha256=None,
        ),
    )
    record_path = (
        target
        / "operations"
        / "component-installation"
        / "op_symlink_status.json"
    )
    record_before = record_path.read_bytes()
    install_root = tmp_path / "install-link"
    install_root.symlink_to(target, target_is_directory=True)

    result = subprocess.run(
        [
            sys.executable,
            str(BOOTSTRAP),
            "--install-dir",
            str(install_root),
            "--operation-status",
            "op_symlink_status",
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
    assert payload["error"]["code"] == "operation_integrity_error"
    assert record_path.read_bytes() == record_before


@pytest.mark.parametrize(
    ("status", "registry", "dll"),
    [
        ("missing", None, None),
        ("outdated", "14.50.0.0", "14.50.0.0"),
        ("registry_dll_mismatch", "14.51.36247.0", "14.51.36246.0"),
    ],
)
def test_bootstrap_windows_vc_prerequisite_blocks_before_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    registry: str | None,
    dll: str | None,
) -> None:
    """A non-ready Windows VC++ host blocks apply before any side effect.

    The frozen contract requires missing/outdated/registry_dll_mismatch to be
    blocking, and apply must fail closed before download, cache write,
    OperationRecord creation, managed staging and runtime publication.
    """
    install_root = tmp_path / "install"
    managed_root = tmp_path / "managed"
    cache_root = tmp_path / "cache"
    monkeypatch.setattr(component_environment, "current_platform", lambda: "windows")
    monkeypatch.setattr(
        component_environment, "current_architecture", lambda: "x86_64"
    )
    monkeypatch.setattr(
        component_environment,
        "_read_windows_registry_version",
        lambda: registry,
    )
    monkeypatch.setattr(
        component_environment,
        "_read_windows_dll_version",
        lambda _name: dll,
    )
    plan = component_installation.build_install_plan(
        managed_root,
        cache_root,
        install_root=install_root,
        ffmpeg_command="roughcut-missing-ffmpeg",
        ffprobe_command="roughcut-missing-ffprobe",
        verify_components=True,
    )
    prereq = plan.payload["prerequisites"]["windows_vc_runtime_x64"]
    assert prereq["status"] == status
    assert prereq["action"]["blocking"] is True

    downloads = 0

    def must_not_download(*_args: object, **_kwargs: object) -> None:
        nonlocal downloads
        downloads += 1
        raise AssertionError("blocked apply must not download artifacts")

    monkeypatch.setattr(component_download, "download_artifact", must_not_download)

    with pytest.raises(ComponentInstallError, match="blocking component prerequisite"):
        bootstrap_module.apply_media_components(
            managed_root,
            cache_root,
            install_root=install_root,
            approved_plan_hash=plan.plan_hash,
            operation_id=f"op_vc_{status}",
            ffmpeg_command="roughcut-missing-ffmpeg",
            ffprobe_command="roughcut-missing-ffprobe",
            verify_components=True,
        )

    assert downloads == 0
    assert not managed_root.exists()
    assert not cache_root.exists()
    assert not (install_root / "runtime.json").exists()
    assert not any(managed_root.parent.glob(".roughcut-stage-*"))
    with pytest.raises(InstallationOperationError) as missing:
        installation_operation_status(install_root, f"op_vc_{status}")
    assert missing.value.code == "operation_not_found"
