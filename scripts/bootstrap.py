"""Install the roughcut core into a controlled Python environment."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from email.parser import Parser
from pathlib import Path, PurePath
from typing import Any, Callable, NoReturn, cast

ROOT = Path(__file__).resolve().parents[1]
CORE_PATH = ROOT / "core"


class BootstrapArgumentError(RuntimeError):
    """Raised so JSON callers receive argparse failures on stdout."""


class BootstrapArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise BootstrapArgumentError(message)


@dataclass(frozen=True)
class _ComponentInstallPreflight:
    plan: object
    catalog: object


def core_version() -> str:
    source_path = str(CORE_PATH / "src")
    if source_path in sys.path:
        sys.path.remove(source_path)
    sys.path.insert(0, source_path)
    from roughcut import __version__ as version

    if not isinstance(version, str):
        raise RuntimeError("core version is not a string")
    return version


def venv_bin(venv_path: Path) -> Path:
    return venv_path / ("Scripts" if sys.platform == "win32" else "bin")


def _parse_build_identity_literal(text: str) -> str | None:
    stripped = text.strip()
    if stripped == "None":
        return None
    if (
        len(stripped) == 42
        and stripped[0] == stripped[-1]
        and stripped[0] in {"'", '"'}
        and len(stripped[1:-1]) == 40
        and all(value in "0123456789abcdef" for value in stripped[1:-1])
    ):
        return stripped[1:-1]
    return None


def _source_commit_from_build_identity_file(path: Path) -> str | None:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in content.splitlines():
        code = line.split("#", 1)[0].strip()
        if not code.startswith("SOURCE_COMMIT"):
            continue
        _, _, literal = code.partition("=")
        commit = _parse_build_identity_literal(literal)
        # Only accept the first SOURCE_COMMIT assignment; anything else
        # (including an unparseable value) means dev/unrecorded identity.
        return commit
    return None


def current_core_source_commit() -> str | None:
    embedded = _source_commit_from_build_identity_file(
        CORE_PATH / "src" / "roughcut" / "_build_identity.py"
    )
    if embedded is not None:
        return embedded
    source_root = CORE_PATH.parent.resolve()
    repository = subprocess.run(
        ["git", "-C", str(source_root), "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
    )
    if repository.returncode != 0 or Path(repository.stdout.strip()).resolve() != source_root:
        return None
    status = subprocess.run(
        ["git", "-C", str(source_root), "status", "--porcelain", "--untracked-files=all"],
        check=False,
        capture_output=True,
        text=True,
    )
    if status.returncode != 0 or status.stdout:
        raise RuntimeError("Roughcut bootstrap requires a clean Git source checkout")
    head = subprocess.run(
        ["git", "-C", str(source_root), "rev-parse", "--verify", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    commit = head.stdout.strip()
    if head.returncode != 0 or _parse_build_identity_literal(f'"{commit}"') != commit:
        raise RuntimeError("Roughcut bootstrap could not identify Git source HEAD")
    return commit


def current_core_identity() -> dict[str, object]:
    source_path = str(CORE_PATH / "src")
    if source_path in sys.path:
        sys.path.remove(source_path)
    sys.path.insert(0, source_path)
    from roughcut.application.health import SCHEMA_VERSION, TOOL_SCHEMA_VERSION

    return {
        "schema_version": SCHEMA_VERSION,
        "core_version": core_version(),
        "tool_schema_version": TOOL_SCHEMA_VERSION,
        "source_commit": current_core_source_commit(),
    }


def _installed_core_health_error(
    roughcut_command: Path,
    expected_identity: dict[str, object],
) -> str | None:
    if not roughcut_command.is_file():
        return "installed roughcut launcher is missing"
    try:
        result = subprocess.run(
            [str(roughcut_command), "health", "--json"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        return str(error)
    if result.returncode != 0:
        return result.stderr.strip() or f"installed health exited with status {result.returncode}"
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return "installed health did not return JSON"
    if not isinstance(payload, dict):
        return "installed health did not return an object"
    if payload.get("ok") is not True:
        return "installed health did not report ok"
    if any(payload.get(key) != value for key, value in expected_identity.items()):
        return "installed health identity does not match the current core"
    return None


def _validate_core_wheel(
    core_wheel: Path,
    *,
    expected_identity: dict[str, object],
) -> Path:
    responsibility = "Roughcut bootstrap Core wheel validation"
    if not core_wheel.is_absolute():
        raise RuntimeError(
            f"{responsibility} failed: --core-wheel must be an absolute path"
        )
    try:
        details = os.lstat(core_wheel)
    except OSError as error:
        raise RuntimeError(
            f"{responsibility} failed: wheel is missing: {core_wheel}"
        ) from error
    if not stat.S_ISREG(details.st_mode):
        raise RuntimeError(
            f"{responsibility} failed: wheel is not a regular file: {core_wheel}"
        )

    expected_name = (
        f"roughcut-{expected_identity['core_version']}-py3-none-any.whl"
    )
    if core_wheel.name != expected_name:
        raise RuntimeError(
            f"{responsibility} failed: filename {core_wheel.name!r} does not match "
            f"expected {expected_name!r}"
        )

    try:
        with zipfile.ZipFile(core_wheel) as archive:
            metadata_names = [
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                raise RuntimeError(
                    f"{responsibility} failed: wheel must contain exactly one "
                    "dist-info/METADATA"
                )
            metadata_text = archive.read(metadata_names[0]).decode("utf-8")
            identity_names = [
                name
                for name in archive.namelist()
                if name == "roughcut/_build_identity.py"
            ]
            wheel_identity_text: str | None = None
            if len(identity_names) == 1:
                try:
                    wheel_identity_text = archive.read(identity_names[0]).decode(
                        "utf-8"
                    )
                except (UnicodeDecodeError, KeyError) as error:
                    raise RuntimeError(
                        f"{responsibility} failed: wheel build identity is unreadable"
                    ) from error
    except RuntimeError:
        raise
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile, KeyError) as error:
        raise RuntimeError(
            f"{responsibility} failed: wheel is not a readable zip archive"
        ) from error

    metadata = Parser().parsestr(metadata_text)
    expected_version = expected_identity["core_version"]
    if metadata.get("Name") != "roughcut":
        raise RuntimeError(
            f"{responsibility} failed: METADATA Name must be 'roughcut'"
        )
    if metadata.get("Version") != expected_version:
        raise RuntimeError(
            f"{responsibility} failed: METADATA Version must be {expected_version!r}"
        )
    expected_commit = expected_identity.get("source_commit")
    wheel_commit: str | None = None
    if wheel_identity_text is not None:
        for line in wheel_identity_text.splitlines():
            code = line.split("#", 1)[0].strip()
            if not code.startswith("SOURCE_COMMIT"):
                continue
            _, _, literal = code.partition("=")
            wheel_commit = _parse_build_identity_literal(literal)
            break
    if wheel_commit != expected_commit:
        raise RuntimeError(
            f"{responsibility} failed: wheel source_commit does not match "
            "the current core source"
        )
    return core_wheel.resolve()


def _install_current_core(
    venv_path: Path,
    *,
    action: str,
    core_wheel: Path | None = None,
    source_commit: str | None = None,
) -> None:
    command = [
        sys.executable,
        "-m",
        "pip",
        "--python",
        str(venv_path),
        "install",
    ]
    if core_wheel is not None:
        command.extend(["--disable-pip-version-check", "--no-index"])
    command.append("--no-deps")
    if action == "updated":
        command.extend(["--upgrade", "--force-reinstall"])
    if core_wheel is None and source_commit is not None and _source_commit_from_build_identity_file(
        CORE_PATH / "src" / "roughcut" / "_build_identity.py"
    ) is None:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from scripts.build_core_release import _stage_shared_source_with_identity

        with tempfile.TemporaryDirectory(prefix="roughcut-core-bootstrap-") as temporary:
            staged_root = Path(temporary) / "source"
            _stage_shared_source_with_identity(CORE_PATH.parent, staged_root, source_commit)
            if current_core_source_commit() != source_commit:
                raise RuntimeError("Roughcut source changed during Core staging")
            result = subprocess.run(
                [*command, str(staged_root / "core")],
                check=False,
                capture_output=True,
                text=True,
            )
    else:
        result = subprocess.run(
            [*command, str(core_wheel or CORE_PATH)],
            check=False,
            capture_output=True,
            text=True,
        )
    if result.returncode != 0:
        detail = result.stderr.strip() or "local core package installation failed"
        verb = "update" if action == "updated" else "install"
        raise RuntimeError(f"Roughcut bootstrap failed to {verb} the installed core: {detail}")


def bootstrap_core(
    install_dir: Path,
    *,
    core_wheel: Path | None = None,
) -> tuple[Path, str]:
    venv_path = install_dir / "venv"
    bin_path = venv_bin(venv_path)
    suffix = ".exe" if sys.platform == "win32" else ""
    roughcut_command = bin_path / f"roughcut{suffix}"
    mcp_command = bin_path / f"roughcut-mcp{suffix}"
    expected_identity = current_core_identity()
    validated_wheel = (
        _validate_core_wheel(core_wheel, expected_identity=expected_identity)
        if core_wheel is not None
        else None
    )
    if not venv_path.exists():
        create_environment = subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(venv_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        if create_environment.returncode != 0:
            detail = create_environment.stderr.strip() or "virtual environment creation failed"
            raise RuntimeError(f"Roughcut bootstrap failed to install the current core: {detail}")
        action = "installed"
    elif (
        mcp_command.is_file()
        and _installed_core_health_error(roughcut_command, expected_identity) is None
    ):
        return mcp_command, "reused"
    else:
        action = "updated"

    _install_current_core(
        venv_path,
        action=action,
        core_wheel=validated_wheel,
        source_commit=(
            expected_identity["source_commit"]
            if isinstance(expected_identity["source_commit"], str)
            else None
        ),
    )
    health_error = _installed_core_health_error(roughcut_command, expected_identity)
    if not mcp_command.is_file():
        health_error = "installed roughcut-mcp launcher is missing"
    if health_error is not None:
        verb = "update" if action == "updated" else "install"
        raise RuntimeError(f"Roughcut bootstrap failed to {verb} the installed core: {health_error}")
    return mcp_command, action


def bootstrap_media_components(
    managed_root: Path,
    *,
    external_manifest_path: Path | None = None,
    local_bundle_path: Path | None = None,
    verify_checksums: bool = False,
) -> dict[str, object]:
    (
        component_manifest_filename,
        diagnose_components,
        install_local_bundle,
        load_component_manifest,
        legacy_component_names,
    ) = _component_api()
    external = (
        load_component_manifest(external_manifest_path)
        if external_manifest_path is not None
        else None
    )
    bundle = (
        load_component_manifest(local_bundle_path)
        if local_bundle_path is not None
        else None
    )
    expected_versions = (
        {component.name: component.version for component in bundle.components}
        if bundle is not None
        else None
    )
    manifest_path = managed_root.resolve(strict=False) / component_manifest_filename
    managed = load_component_manifest(manifest_path) if manifest_path.is_file() else None
    diagnosis = diagnose_components(
        external_manifest=external,
        managed_manifest=managed,
        expected_versions=expected_versions,
        verify_checksums=verify_checksums or local_bundle_path is not None,
        component_names=legacy_component_names,
    )
    installation = None
    if local_bundle_path is not None and diagnosis.install_required and managed is None:
        installation = install_local_bundle(
            managed_root,
            local_bundle_path,
            component_names=diagnosis.install_required,
        )
        managed = installation.manifest
        diagnosis = diagnose_components(
            external_manifest=external,
            managed_manifest=managed,
            expected_versions=expected_versions,
            verify_checksums=verify_checksums or local_bundle_path is not None,
            component_names=legacy_component_names,
        )
    selected_managed = any(
        component.selected_source == "managed"
        for component in diagnosis.components.values()
    )
    return {
        "schema_version": 1,
        "managed_root": str(managed_root.resolve(strict=False)),
        "manifest_path": str(manifest_path),
        "installed": installation.installed if installation is not None else False,
        "reused": installation.reused if installation is not None else selected_managed,
        "diagnostics": diagnosis.to_dict(),
    }


def plan_media_components(
    managed_root: Path,
    cache_root: Path,
    *,
    install_root: Path | None = None,
    external_manifest_path: Path | None = None,
    external_python: Path | None = None,
    ffmpeg_command: str = "ffmpeg",
    ffprobe_command: str = "ffprobe",
    target_platform: str | None = None,
    target_architecture: str | None = None,
    verify_components: bool = False,
    include_audalign: bool = False,
    include_bbc_audio_offset_finder: bool = False,
) -> dict[str, object]:
    component_error, apply_install_plan, build_install_plan = _component_install_api()
    try:
        plan = build_install_plan(
            managed_root,
            cache_root,
            install_root=install_root,
            external_manifest_path=external_manifest_path,
            external_python=external_python,
            platform=target_platform,
            architecture=target_architecture,
            ffmpeg_command=ffmpeg_command,
            ffprobe_command=ffprobe_command,
            verify_components=verify_components,
            include_audalign=include_audalign,
            include_bbc_audio_offset_finder=include_bbc_audio_offset_finder,
        )
    except component_error as error:
        raise RuntimeError(str(error)) from error
    return cast(dict[str, object], plan.to_dict())


def apply_media_components(
    managed_root: Path,
    cache_root: Path,
    *,
    approved_plan_hash: str,
    operation_id: str,
    install_root: Path | None = None,
    external_manifest_path: Path | None = None,
    external_python: Path | None = None,
    ffmpeg_command: str = "ffmpeg",
    ffprobe_command: str = "ffprobe",
    verify_components: bool = False,
    include_audalign: bool = False,
    include_bbc_audio_offset_finder: bool = False,
) -> dict[str, object]:
    component_error, apply_install_plan, build_install_plan = _component_install_api()
    (
        installation_result_ref,
        run_component_installation,
        _status,
    ) = _installation_operation_api()
    selected_install_root = Path(
        os.path.abspath(install_root or Path.home() / ".roughcut")
    )

    def apply(phase_callback: Callable[[str], None]) -> Any:
        try:
            return apply_install_plan(
                managed_root,
                cache_root,
                install_root=selected_install_root,
                approved_plan_hash=approved_plan_hash,
                external_manifest_path=external_manifest_path,
                external_python=external_python,
                ffmpeg_command=ffmpeg_command,
                ffprobe_command=ffprobe_command,
                verify_components=verify_components,
                include_audalign=include_audalign,
                include_bbc_audio_offset_finder=include_bbc_audio_offset_finder,
                phase_callback=phase_callback,
            )
        except component_error:
            raise

    def preflight() -> object:
        if not callable(build_install_plan):
            raise component_error("component installation preflight is unavailable")
        from roughcut.adapters.component_installation import (
            load_release_catalog,
            validate_install_preflight,
        )

        catalog = load_release_catalog()
        plan = build_install_plan(
            managed_root,
            cache_root,
            catalog=catalog,
            install_root=selected_install_root,
            external_manifest_path=external_manifest_path,
            external_python=external_python,
            ffmpeg_command=ffmpeg_command,
            ffprobe_command=ffprobe_command,
            verify_components=verify_components,
            include_audalign=include_audalign,
            include_bbc_audio_offset_finder=include_bbc_audio_offset_finder,
        )
        validate_install_preflight(
            plan,
            approved_plan_hash,
            catalog=catalog,
        )
        return _ComponentInstallPreflight(plan=plan, catalog=catalog)

    def apply_preflight(
        preflight_result: object,
        phase_callback: Callable[[str], None],
    ) -> Any:
        if not isinstance(preflight_result, _ComponentInstallPreflight):
            raise component_error("component installation preflight result is invalid")
        try:
            return apply_install_plan(
                managed_root,
                cache_root,
                install_root=selected_install_root,
                approved_plan_hash=approved_plan_hash,
                external_manifest_path=external_manifest_path,
                external_python=external_python,
                ffmpeg_command=ffmpeg_command,
                ffprobe_command=ffprobe_command,
                verify_components=verify_components,
                include_audalign=include_audalign,
                include_bbc_audio_offset_finder=include_bbc_audio_offset_finder,
                phase_callback=phase_callback,
                catalog=preflight_result.catalog,
                preflight_plan=preflight_result.plan,
            )
        except component_error:
            raise

    def result_ref(result: Any) -> Any:
        binding_path = selected_install_root / "runtime.json"
        binding_hash = _safe_file_sha256(
            binding_path,
            responsibility="runtime binding",
        )
        manifest_path = (
            None
            if result.manifest_path is None
            else Path(result.manifest_path)
        )
        manifest_hash = (
            None
            if manifest_path is None
            else _safe_file_sha256(
                manifest_path,
                responsibility="component manifest",
            )
        )
        return installation_result_ref(
            approved_plan_hash=approved_plan_hash,
            runtime_binding_sha256=binding_hash,
            component_manifest_sha256=manifest_hash,
        )

    operation_kwargs: dict[str, object] = {
        "install_root": selected_install_root,
        "operation_id": operation_id,
        "approved_plan_hash": approved_plan_hash,
        "apply": apply,
        "result_ref": result_ref,
    }
    if callable(build_install_plan):
        operation_kwargs["preflight"] = preflight
        operation_kwargs["apply_preflight"] = apply_preflight
    outcome = run_component_installation(**operation_kwargs)
    return {
        "schema_version": 1,
        "kind": "roughcut_component_installation_operation",
        "operation": outcome.record.to_dict(),
        "readback": outcome.readback,
    }


def installation_operation_status(
    install_root: Path,
    operation_id: str,
) -> dict[str, object]:
    _installation_result_ref, _run, status = _installation_operation_api()
    record = status(Path(os.path.abspath(install_root)), operation_id)
    return {
        "schema_version": 1,
        "kind": "roughcut_component_installation_operation",
        "operation": record.to_dict(),
    }


def health_media_components(
    managed_root: Path,
    cache_root: Path,
    *,
    install_root: Path | None = None,
    external_manifest_path: Path | None = None,
    external_python: Path | None = None,
    ffmpeg_command: str = "ffmpeg",
    ffprobe_command: str = "ffprobe",
    verify_components: bool = False,
    include_audalign: bool = False,
    include_bbc_audio_offset_finder: bool = False,
) -> dict[str, object]:
    plan = plan_media_components(
        managed_root,
        cache_root,
        install_root=install_root,
        external_manifest_path=external_manifest_path,
        external_python=external_python,
        ffmpeg_command=ffmpeg_command,
        ffprobe_command=ffprobe_command,
        verify_components=verify_components,
        include_audalign=include_audalign,
        include_bbc_audio_offset_finder=include_bbc_audio_offset_finder,
    )
    result = {
        "schema_version": 1,
        "kind": "roughcut_component_health",
        "profile": plan["profile"],
        "target": plan["target"],
        "verification_mode": plan["verification_mode"],
        "components": plan["components"],
        "install_required": plan["missing_managed_groups"],
        "user_actions": plan["user_actions"],
        "reusable": not plan["missing_managed_groups"] and not plan["user_actions"],
    }
    if "runtime_binding" in plan:
        result["runtime_binding"] = plan["runtime_binding"]
    return result


def populate_cache_from_channel(
    cache_root: Path,
    *,
    filename: str,
    source_path: Path,
    source_tier: str,
    component: str | None = None,
    target_platform: str | None = None,
    target_architecture: str | None = None,
) -> dict[str, object]:
    """Scheme B Phase A: verify one channel-fetched file into the cache.

    The Agent fetches the exact frozen bytes through an allowed channel and
    passes the local file here. The core resolves the artifact from the
    frozen catalog by exact component + filename (component is optional for
    backward compatibility: a unique filename without component still
    resolves; an ambiguous filename without component fails closed with an
    actionable error), enforces exact size + SHA-256 from the frozen catalog,
    publishes through the existing cache layout/receipt path, and returns a
    sanitized provenance record. Callers must then rerun a full component
    PLAN (the hash changes) and obtain approval before APPLY.
    """

    core_source_path = str(CORE_PATH / "src")
    if core_source_path in sys.path:
        sys.path.remove(core_source_path)
    sys.path.insert(0, core_source_path)
    from roughcut.adapters.component_download import (
        POPULATE_SOURCE_TIERS,
        populate_cache_from_local_file,
    )
    from roughcut.adapters.component_environment import (
        current_architecture,
        current_platform,
    )
    from roughcut.adapters.component_installation import load_release_catalog

    if source_tier not in POPULATE_SOURCE_TIERS:
        raise RuntimeError("populate source tier is not closed")
    if not filename or "/" in filename or "\\" in filename or filename in {".", ".."}:
        raise RuntimeError("populate filename is unsafe")
    if component is not None and (
        not component or "/" in component or "\\" in component or component in {".", ".."}
    ):
        raise RuntimeError("populate component is unsafe")
    host_platform = current_platform()
    host_architecture = current_architecture()
    profile = load_release_catalog().profile_for(
        target_platform or host_platform, target_architecture or host_architecture
    )
    candidates = list(profile.runtime.artifacts)
    for model in profile.models.values():
        candidates.extend(model.artifacts)
    for group in profile.alignment_groups:
        candidates.extend(group.artifacts)
    if component is not None:
        allowed_components = {artifact.component for artifact in candidates}
        if component not in allowed_components:
            raise RuntimeError("populate component is unknown")
        matches = [
            artifact
            for artifact in candidates
            if artifact.filename == filename and artifact.component == component
        ]
        if not matches:
            raise RuntimeError(
                "populate filename does not match a catalog artifact for "
                "this component/platform/architecture"
            )
        if len(matches) > 1:
            raise RuntimeError(
                "populate component+filename is ambiguous in the catalog"
            )
    else:
        matches = [artifact for artifact in candidates if artifact.filename == filename]
        if not matches:
            raise RuntimeError(
                "populate filename does not match a catalog artifact for "
                "this platform/architecture"
            )
        if len(matches) > 1:
            raise RuntimeError(
                "artifact filename is ambiguous; specify --populate-component"
            )
    try:
        outcome = populate_cache_from_local_file(
            matches[0], source_path, source_tier, cache_root
        )
    except Exception as error:
        from roughcut.adapters.component_installation import ComponentInstallError

        if isinstance(error, ComponentInstallError):
            raise RuntimeError(str(error)) from error
        raise
    return {
        "schema_version": 1,
        "kind": "roughcut_component_cache_population",
        "component": matches[0].component,
        "filename": outcome.provenance["filename"],
        "tier": outcome.provenance["tier"],
        "sha256": outcome.provenance["sha256"],
        "size": outcome.provenance["size"],
        "reused": outcome.reused,
        "cache_path": str(outcome.path),
    }


def bootstrap(
    install_dir: Path,
    *,
    core_wheel: Path | None = None,
    managed_root: Path | None = None,
    external_manifest_path: Path | None = None,
    local_bundle_path: Path | None = None,
    verify_components: bool = False,
) -> dict[str, object]:
    if sys.version_info < (3, 11):
        raise RuntimeError("roughcut bootstrap requires Python 3.11 or newer")

    mcp_command, core_action = bootstrap_core(
        install_dir,
        core_wheel=core_wheel,
    )

    payload: dict[str, object] = {
        "schema_version": 1,
        "core_version": core_version(),
        "source_commit": current_core_source_commit(),
        "ok": True,
        "installed": core_action == "installed",
        "core_action": core_action,
        "mcp_command": str(mcp_command.resolve()),
    }
    runtime_binding_path, runtime_binding_status = _runtime_binding_api()
    binding_path = Path(runtime_binding_path(install_root=install_dir.resolve(strict=False)))
    payload["runtime_binding"] = runtime_binding_status(binding_path)
    if managed_root is not None:
        payload["media_components"] = bootstrap_media_components(
            managed_root,
            external_manifest_path=external_manifest_path,
            local_bundle_path=local_bundle_path,
            verify_checksums=verify_components,
        )
    return payload


def _component_api() -> tuple[
    str,
    Callable[..., Any],
    Callable[..., Any],
    Callable[..., Any],
    tuple[str, ...],
]:
    source_path = str(CORE_PATH / "src")
    if source_path in sys.path:
        sys.path.remove(source_path)
    sys.path.insert(0, source_path)
    from roughcut.adapters.component_environment import (
        COMPONENT_MANIFEST_FILENAME,
        diagnose_components,
        install_local_bundle,
        load_component_manifest,
        LEGACY_COMPONENT_NAMES,
    )
    return (
        COMPONENT_MANIFEST_FILENAME,
        diagnose_components,
        install_local_bundle,
        load_component_manifest,
        LEGACY_COMPONENT_NAMES,
    )


def _component_install_api() -> tuple[
    type[Exception],
    Callable[..., Any],
    Callable[..., Any],
]:
    source_path = str(CORE_PATH / "src")
    if source_path in sys.path:
        sys.path.remove(source_path)
    sys.path.insert(0, source_path)
    from roughcut.adapters.component_installation import (
        ComponentInstallError,
        apply_install_plan,
        build_install_plan,
    )

    return ComponentInstallError, apply_install_plan, build_install_plan


def _runtime_binding_api() -> tuple[
    Callable[..., str | PurePath],
    Callable[[Path], dict[str, object]],
]:
    source_path = str(CORE_PATH / "src")
    if source_path in sys.path:
        sys.path.remove(source_path)
    sys.path.insert(0, source_path)
    from roughcut.adapters.runtime_binding import (
        runtime_binding_path,
        runtime_binding_status,
    )

    return runtime_binding_path, runtime_binding_status


def _installation_operation_api() -> tuple[
    Callable[..., Any],
    Callable[..., Any],
    Callable[..., Any],
]:
    source_path = str(CORE_PATH / "src")
    if source_path in sys.path:
        sys.path.remove(source_path)
    sys.path.insert(0, source_path)
    from roughcut.application.installation_operations import (
        installation_operation_status,
        run_component_installation,
    )
    from roughcut.domain.installation_operation import InstallationResultRef

    return (
        InstallationResultRef,
        run_component_installation,
        installation_operation_status,
    )


def _new_installation_operation_id() -> str:
    source_path = str(CORE_PATH / "src")
    if source_path in sys.path:
        sys.path.remove(source_path)
    sys.path.insert(0, source_path)
    from roughcut.application.installation_operations import (
        new_installation_operation_id,
    )

    return new_installation_operation_id()


def _safe_file_sha256(path: Path, *, responsibility: str) -> str:
    try:
        details = os.lstat(path)
        if (
            stat.S_ISLNK(details.st_mode)
            or not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
        ):
            raise RuntimeError(
                f"Roughcut {responsibility} identity is unsafe"
            )
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as error:
        raise RuntimeError(
            f"Roughcut {responsibility} identity could not be read"
        ) from error


def _execute_bootstrap_args(args: argparse.Namespace) -> dict[str, object]:
    if args.operation_status is not None:
        if any(
            (
                args.managed_root is not None,
                args.core_wheel is not None,
                args.external_components is not None,
                args.external_funasr_python is not None,
                args.local_component_bundle is not None,
                args.component_cache is not None,
                args.apply_components,
                args.approved_plan_hash is not None,
                args.operation_id is not None,
                args.component_health,
                args.target_platform is not None,
                args.target_architecture is not None,
                args.verify_components,
                args.include_audalign,
                args.include_bbc_audio_offset_finder,
                getattr(args, "populate_cache", False),
                getattr(args, "populate_filename", None) is not None,
                getattr(args, "populate_source", None) is not None,
                getattr(args, "populate_tier", None) is not None,
                getattr(args, "populate_component", None) is not None,
            )
        ):
            raise RuntimeError(
                "--operation-status is a pure read and cannot be combined "
                "with component plan/apply options"
            )
        return {
            "schema_version": 1,
            "core_version": core_version(),
            "source_commit": current_core_source_commit(),
            "ok": True,
            "action": "operation_status",
            **installation_operation_status(
                args.install_dir,
                args.operation_status,
            ),
        }

    if getattr(args, "populate_cache", False):
        if any(
            (
                args.managed_root is not None,
                args.core_wheel is not None,
                args.external_components is not None,
                args.external_funasr_python is not None,
                args.local_component_bundle is not None,
                args.apply_components,
                args.approved_plan_hash is not None,
                args.operation_id is not None,
                args.component_health,
                args.verify_components,
                args.include_audalign,
                args.include_bbc_audio_offset_finder,
            )
        ):
            raise RuntimeError(
                "--populate-cache is cache-only and cannot be combined "
                "with core/component plan/apply/health options"
            )
        if args.component_cache is None:
            raise RuntimeError("--populate-cache requires --component-cache")
        if getattr(args, "populate_filename", None) is None:
            raise RuntimeError("--populate-cache requires --populate-filename")
        if getattr(args, "populate_source", None) is None:
            raise RuntimeError("--populate-cache requires --populate-source")
        if getattr(args, "populate_tier", None) is None:
            raise RuntimeError("--populate-cache requires --populate-tier")
        try:
            populated = populate_cache_from_channel(
                args.component_cache,
                filename=getattr(args, "populate_filename"),
                source_path=getattr(args, "populate_source"),
                source_tier=getattr(args, "populate_tier"),
                component=getattr(args, "populate_component", None),
                target_platform=args.target_platform,
                target_architecture=args.target_architecture,
            )
        except RuntimeError as error:
            raise RuntimeError(str(error)) from error
        return {
            "schema_version": 1,
            "core_version": core_version(),
            "source_commit": current_core_source_commit(),
            "ok": True,
            "action": "populate-cache",
            "populated": populated,
        }
    if any(
        (
            getattr(args, "populate_filename", None) is not None,
            getattr(args, "populate_source", None) is not None,
            getattr(args, "populate_tier", None) is not None,
            getattr(args, "populate_component", None) is not None,
        )
    ):
        raise RuntimeError(
            "--populate-filename/--populate-source/--populate-tier/--populate-component "
            "require --populate-cache"
        )

    if args.apply_components and args.component_health:
        raise RuntimeError(
            "--apply-components and --component-health are mutually exclusive"
        )
    if args.include_audalign and not args.verify_components:
        raise RuntimeError("--include-audalign requires --verify-components")
    if args.include_bbc_audio_offset_finder and not args.verify_components:
        raise RuntimeError(
            "--include-bbc-audio-offset-finder requires --verify-components"
        )
    if args.include_audalign and args.include_bbc_audio_offset_finder:
        raise RuntimeError(
            "--include-audalign and --include-bbc-audio-offset-finder are mutually exclusive"
        )
    if args.approved_plan_hash is not None and not args.apply_components:
        raise RuntimeError("--approved-plan-hash requires --apply-components")
    if args.operation_id is not None and not args.apply_components:
        raise RuntimeError("--operation-id requires --apply-components")
    if args.apply_components and args.approved_plan_hash is None:
        raise RuntimeError("--apply-components requires --approved-plan-hash")
    if args.apply_components and args.operation_id is None:
        raise RuntimeError("--apply-components requires --operation-id")
    if args.apply_components and not args.verify_components:
        raise RuntimeError(
            "--apply-components requires the approved full plan "
            "and --verify-components"
        )
    if (args.target_platform is None) != (args.target_architecture is None):
        raise RuntimeError(
            "--target-platform and --target-architecture must be provided together"
        )
    if (
        args.apply_components or args.component_health
    ) and args.target_platform is not None:
        raise RuntimeError("cross-target options are plan-only")
    if args.local_component_bundle is not None and args.managed_root is not None:
        raise RuntimeError(
            "--local-component-bundle cannot bypass plan approval; "
            "use plan then --apply-components"
        )
    if args.managed_root is None and (
        args.external_components is not None
        or args.external_funasr_python is not None
        or args.local_component_bundle is not None
        or args.component_cache is not None
        or args.apply_components
        or args.component_health
        or args.target_platform is not None
        or args.target_architecture is not None
        or args.verify_components
        or args.include_audalign
        or args.include_bbc_audio_offset_finder
    ):
        raise RuntimeError("--managed-root is required for media components")
    if args.managed_root is None:
        return bootstrap(args.install_dir, core_wheel=args.core_wheel)
    if args.core_wheel is not None:
        bootstrap_core(args.install_dir, core_wheel=args.core_wheel)
    if args.component_cache is None:
        raise RuntimeError("--component-cache is required for media components")
    if args.apply_components:
        media_components = apply_media_components(
            args.managed_root,
            args.component_cache,
            install_root=args.install_dir,
            approved_plan_hash=args.approved_plan_hash,
            operation_id=args.operation_id,
            external_manifest_path=args.external_components,
            external_python=args.external_funasr_python,
            ffmpeg_command=args.ffmpeg_command,
            ffprobe_command=args.ffprobe_command,
            verify_components=args.verify_components,
            include_audalign=args.include_audalign,
            include_bbc_audio_offset_finder=args.include_bbc_audio_offset_finder,
        )
        action = "apply"
    elif args.component_health:
        media_components = health_media_components(
            args.managed_root,
            args.component_cache,
            install_root=args.install_dir,
            external_manifest_path=args.external_components,
            external_python=args.external_funasr_python,
            ffmpeg_command=args.ffmpeg_command,
            ffprobe_command=args.ffprobe_command,
            verify_components=args.verify_components,
            include_audalign=args.include_audalign,
            include_bbc_audio_offset_finder=args.include_bbc_audio_offset_finder,
        )
        action = "health"
    else:
        media_components = plan_media_components(
            args.managed_root,
            args.component_cache,
            install_root=args.install_dir,
            external_manifest_path=args.external_components,
            external_python=args.external_funasr_python,
            ffmpeg_command=args.ffmpeg_command,
            ffprobe_command=args.ffprobe_command,
            target_platform=args.target_platform,
            target_architecture=args.target_architecture,
            verify_components=args.verify_components,
            include_audalign=args.include_audalign,
            include_bbc_audio_offset_finder=args.include_bbc_audio_offset_finder,
        )
        action = "plan"
    result: dict[str, object] = {
        "schema_version": 1,
        "core_version": core_version(),
        "source_commit": current_core_source_commit(),
        "ok": True,
        "action": action,
        "media_components": media_components,
    }
    if action == "plan" and args.verify_components:
        operation: dict[str, object] | None = None
        target = media_components.get("target")
        actions = media_components.get("user_actions")
        path_budget = media_components.get("path_budget")
        supported = isinstance(target, dict) and target.get(
            "apply_supported_on_this_host"
        ) is True
        no_blocking_actions = isinstance(actions, list) and not any(
            isinstance(item, dict) and item.get("blocking") is True
            for item in actions
        )
        path_budget_passes = isinstance(path_budget, dict) and not any(
            isinstance(item, dict) and item.get("status") == "blocking"
            for item in path_budget.values()
        )
        if supported and no_blocking_actions and path_budget_passes:
            operation = {
                "operation_id": _new_installation_operation_id(),
                "operation_type": "component_installation",
                "approved_plan_hash": media_components["plan_hash"],
            }
        result["installation_operation"] = operation
    return result


def _write_json_stdout(payload: object) -> None:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="strict")
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    sys.stdout.write("\n")
    sys.stdout.flush()


def main() -> None:
    parser = BootstrapArgumentParser()
    parser.add_argument("--install-dir", type=Path, default=Path.home() / ".roughcut")
    parser.add_argument("--core-wheel", type=Path)
    parser.add_argument("--managed-root", type=Path)
    parser.add_argument("--external-components", type=Path)
    parser.add_argument("--external-funasr-python", type=Path)
    parser.add_argument("--local-component-bundle", type=Path)
    parser.add_argument("--component-cache", type=Path)
    parser.add_argument("--apply-components", action="store_true")
    parser.add_argument("--approved-plan-hash")
    parser.add_argument("--operation-id")
    parser.add_argument("--operation-status")
    parser.add_argument("--component-health", action="store_true")
    parser.add_argument("--ffmpeg-command", default="ffmpeg")
    parser.add_argument("--ffprobe-command", default="ffprobe")
    parser.add_argument("--target-platform", choices=("macos", "windows"))
    parser.add_argument("--target-architecture", choices=("arm64", "x86_64"))
    parser.add_argument("--verify-components", action="store_true")
    parser.add_argument("--include-audalign", action="store_true")
    parser.add_argument("--include-bbc-audio-offset-finder", action="store_true")
    parser.add_argument("--populate-cache", action="store_true")
    parser.add_argument("--populate-component")
    parser.add_argument("--populate-filename")
    parser.add_argument("--populate-source", type=Path)
    parser.add_argument(
        "--populate-tier",
        choices=("local", "aliyun", "modelscope", "canonical", "agent-fallback"),
    )
    parser.add_argument("--json", action="store_true")
    json_output = "--json" in sys.argv[1:]

    try:
        args = parser.parse_args()
        result = _execute_bootstrap_args(args)
    except (BootstrapArgumentError, RuntimeError) as error:
        code = getattr(error, "code", "bootstrap_failed")
        responsibility = getattr(error, "failure_responsibility", None)
        action = getattr(error, "failure_action", None)
        if (
            responsibility == "user_input"
            and action == "validate_approved_full_plan"
        ):
            error_payload: dict[str, object] = {
                "code": "component_installation_failed",
                "responsibility": responsibility,
                "action": action,
                "message_code": "component_installation_failed",
            }
        else:
            error_payload = {"code": code, "message": str(error)}
        result = {
            "schema_version": 1,
            "ok": False,
            "error": error_payload,
        }
        if (
            "args" in locals()
            and args.operation_id is not None
            and args.apply_components
        ):
            try:
                result["installation_operation"] = installation_operation_status(
                    args.install_dir,
                    args.operation_id,
                )
            except RuntimeError:
                pass
        exit_code = 1
    except KeyboardInterrupt:
        result = {
            "schema_version": 1,
            "ok": False,
            "error": {
                "code": "component_installation_interrupted",
                "message": "Roughcut bootstrap component installation was interrupted",
            },
        }
        if "args" in locals() and args.operation_id is not None:
            try:
                result["installation_operation"] = installation_operation_status(
                    args.install_dir,
                    args.operation_id,
                )
            except RuntimeError:
                pass
        exit_code = 130
    else:
        exit_code = 0

    if json_output:
        _write_json_stdout(result)
    else:
        print(result)
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
