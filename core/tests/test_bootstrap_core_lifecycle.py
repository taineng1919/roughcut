from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from scripts import bootstrap as bootstrap_script

CURRENT_VERSION = bootstrap_script.core_version()
CURRENT_HEALTH = {
    "schema_version": 1,
    "core_version": CURRENT_VERSION,
    "tool_schema_version": 32,
    "ok": True,
}


@pytest.fixture(autouse=True)
def unrecorded_source_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    # Existing lifecycle cases isolate version/schema behavior from Git discovery.
    # The staged-revision case below overrides this with explicit A/B identities.
    monkeypatch.setattr(bootstrap_script, "current_core_source_commit", lambda: None)


def _write_launchers(install_dir: Path) -> tuple[Path, Path]:
    bin_path = bootstrap_script.venv_bin(install_dir / "venv")
    bin_path.mkdir(parents=True, exist_ok=True)
    suffix = ".exe" if sys.platform == "win32" else ""
    roughcut = bin_path / f"roughcut{suffix}"
    mcp = bin_path / f"roughcut-mcp{suffix}"
    roughcut.write_text("fixture roughcut", encoding="utf-8")
    mcp.write_text("fixture roughcut mcp", encoding="utf-8")
    return roughcut, mcp


def _write_wheel(
    root: Path,
    *,
    filename: str = f"roughcut-{CURRENT_VERSION}-py3-none-any.whl",
    name: str = "roughcut",
    version: str = CURRENT_VERSION,
) -> Path:
    path = root / filename
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(
            f"roughcut-{CURRENT_VERSION}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        )
    return path


def _snapshot_files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _runner(
    install_dir: Path,
    health_results: list[object],
    *,
    pip_returncode: int = 0,
) -> tuple[Callable[..., subprocess.CompletedProcess[str]], list[list[str]]]:
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1:4] == ["-m", "venv", "--without-pip"]:
            _write_launchers(install_dir)
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[1:3] == ["-m", "pip"]:
            if pip_returncode == 0:
                _write_launchers(install_dir)
            return subprocess.CompletedProcess(
                command,
                pip_returncode,
                "",
                "fixture pip failure" if pip_returncode else "",
            )
        if command[1:] == ["health", "--json"]:
            result = health_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            if isinstance(result, str):
                return subprocess.CompletedProcess(command, 0, result, "")
            if isinstance(result, int):
                return subprocess.CompletedProcess(command, result, "", "fixture health failure")
            return subprocess.CompletedProcess(command, 0, json.dumps(result), "")
        raise AssertionError(f"unexpected subprocess command: {command}")

    return run, calls


def test_empty_install_creates_core_then_verifies_installed_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_dir = tmp_path / "roughcut install"
    run, calls = _runner(install_dir, [CURRENT_HEALTH])
    monkeypatch.setattr(bootstrap_script.subprocess, "run", run)

    result = bootstrap_script.bootstrap(install_dir)

    assert result["schema_version"] == 1
    assert result["core_version"] == CURRENT_VERSION
    assert result["core_action"] == "installed"
    assert result["installed"] is True
    assert calls[0][1:4] == ["-m", "venv", "--without-pip"]
    assert calls[1][1:3] == ["-m", "pip"]
    assert calls[-1][1:] == ["health", "--json"]


def test_core_wheel_install_uses_a_validated_offline_local_wheel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_dir = tmp_path / "roughcut install"
    wheel = _write_wheel(tmp_path)
    run, calls = _runner(install_dir, [CURRENT_HEALTH])
    monkeypatch.setattr(bootstrap_script.subprocess, "run", run)

    result = bootstrap_script.bootstrap(install_dir, core_wheel=wheel)

    assert result["core_action"] == "installed"
    pip_call = next(call for call in calls if call[1:3] == ["-m", "pip"])
    assert "--no-index" in pip_call
    assert "--disable-pip-version-check" in pip_call
    assert "--no-deps" in pip_call
    assert "--no-build-isolation" not in pip_call
    assert pip_call[-1] == str(wheel.resolve())
    assert str(bootstrap_script.CORE_PATH) not in pip_call


@pytest.mark.parametrize(
    "installed_version", [
        "0.1.1",
        "0.1.9",
        "0.1.10",
        "0.1.11",
        "0.1.12",
        "0.1.13",
        "0.1.14",
        "0.1.15",
        "0.1.16",
        "0.2.0",
        "0.2.1",
        "0.2.2",
        "0.2.3",
        "0.2.4",
        "0.2.5",
        "0.2.6",
        "0.2.7",
        # 0.2.8 is a historical identity and must be updated, never reused.
        "0.2.8",
        "0.2.9",
    ]
)
def test_existing_old_core_is_updated_and_reverified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    installed_version: str,
) -> None:
    install_dir = tmp_path / "roughcut install"
    _write_launchers(install_dir)
    wheel = _write_wheel(tmp_path)
    old_health = {**CURRENT_HEALTH, "core_version": installed_version}
    run, calls = _runner(install_dir, [old_health, CURRENT_HEALTH])
    monkeypatch.setattr(bootstrap_script.subprocess, "run", run)

    result = bootstrap_script.bootstrap(install_dir, core_wheel=wheel)

    assert result["core_action"] == "updated"
    assert result["core_version"] == CURRENT_VERSION
    assert result["installed"] is False
    pip_call = next(call for call in calls if call[1:3] == ["-m", "pip"])
    assert "--no-deps" in pip_call
    assert "--no-index" in pip_call
    assert "--disable-pip-version-check" in pip_call
    assert "--upgrade" in pip_call
    assert "--force-reinstall" in pip_call
    assert pip_call[-1] == str(wheel.resolve())
    assert not any(call[1:4] == ["-m", "venv", "--without-pip"] for call in calls)
    assert calls[-1][1:] == ["health", "--json"]


def test_existing_schema_30_is_updated_and_reverified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_dir = tmp_path / "roughcut install"
    _write_launchers(install_dir)
    old_health = {**CURRENT_HEALTH, "tool_schema_version": 30}
    run, calls = _runner(install_dir, [old_health, CURRENT_HEALTH])
    monkeypatch.setattr(bootstrap_script.subprocess, "run", run)

    result = bootstrap_script.bootstrap(install_dir)

    assert result["core_action"] == "updated"
    assert result["installed"] is False
    pip_call = next(call for call in calls if call[1:3] == ["-m", "pip"])
    assert "--no-deps" in pip_call
    assert "--upgrade" in pip_call
    assert "--force-reinstall" in pip_call
    assert pip_call[-1] == str(bootstrap_script.CORE_PATH)
    assert calls[-1][1:] == ["health", "--json"]


def test_current_core_is_reused_without_pip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_dir = tmp_path / "roughcut install"
    _write_launchers(install_dir)
    wheel = _write_wheel(tmp_path)
    run, calls = _runner(install_dir, [CURRENT_HEALTH])
    monkeypatch.setattr(bootstrap_script.subprocess, "run", run)

    result = bootstrap_script.bootstrap(install_dir, core_wheel=wheel)

    assert result["core_action"] == "reused"
    assert result["installed"] is False
    assert not any(call[1:3] == ["-m", "pip"] for call in calls)


def test_staged_git_identity_installs_reuses_and_updates_same_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_dir = tmp_path / "install"
    runtime_path = install_dir / "runtime.json"
    components = tmp_path / "components" / "model.bin"
    revision = ["a" * 40]
    installed = [None]
    pip_calls: list[list[str]] = []
    identity = {**CURRENT_HEALTH, "source_commit": revision[0]}

    def current_identity() -> dict[str, object]:
        return {**identity, "source_commit": revision[0]}

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[1:4] == ["-m", "venv", "--without-pip"]:
            _write_launchers(install_dir)
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[1:3] == ["-m", "pip"]:
            pip_calls.append(command)
            staged_identity = Path(command[-1]) / "src/roughcut/_build_identity.py"
            installed[0] = bootstrap_script._source_commit_from_build_identity_file(
                staged_identity
            )
            assert installed[0] == revision[0]
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[1:] == ["health", "--json"]:
            return subprocess.CompletedProcess(
                command, 0, json.dumps({**current_identity(), "source_commit": installed[0]}), ""
            )
        raise AssertionError(f"unexpected subprocess command: {command}")

    monkeypatch.setattr(bootstrap_script, "current_core_identity", current_identity)
    monkeypatch.setattr(bootstrap_script, "current_core_source_commit", lambda: revision[0])
    monkeypatch.setattr(bootstrap_script.subprocess, "run", run)
    assert bootstrap_script.bootstrap_core(install_dir)[1] == "installed"
    assert installed[0] == "a" * 40
    assert bootstrap_script.bootstrap_core(install_dir)[1] == "reused"
    assert len(pip_calls) == 1

    runtime_path.write_bytes(b"existing runtime binding\n")
    components.parent.mkdir()
    components.write_bytes(b"existing model\n")
    revision[0] = "b" * 40
    assert bootstrap_script.bootstrap_core(install_dir)[1] == "updated"
    assert installed[0] == "b" * 40
    assert len(pip_calls) == 2
    assert "--force-reinstall" in pip_calls[-1]
    assert runtime_path.read_bytes() == b"existing runtime binding\n"
    assert components.read_bytes() == b"existing model\n"


def test_core_update_preserves_existing_runtime_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_dir = tmp_path / "roughcut install"
    _write_launchers(install_dir)
    runtime_path = install_dir / "runtime.json"
    stable_root = tmp_path / "external-tools/ffmpeg/9.0-martin-riedl-arm64/bin"
    runtime_bytes = (
        json.dumps(
            {
                "schema_version": 2,
                "ffmpeg": str(stable_root / "ffmpeg"),
                "ffprobe": str(stable_root / "ffprobe"),
            },
            sort_keys=True,
        )
        + "\n"
    ).encode()
    runtime_path.write_bytes(runtime_bytes)
    old_health = {**CURRENT_HEALTH, "core_version": "0.2.1"}
    run, _calls = _runner(install_dir, [old_health, CURRENT_HEALTH])
    monkeypatch.setattr(bootstrap_script.subprocess, "run", run)

    result = bootstrap_script.bootstrap(install_dir)

    assert result["core_action"] == "updated"
    assert runtime_path.read_bytes() == runtime_bytes


def test_core_wheel_update_preserves_runtime_and_component_tree_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_dir = tmp_path / "roughcut install"
    _write_launchers(install_dir)
    runtime_path = install_dir / "runtime.json"
    runtime_path.write_bytes(b'{"schema_version":2,"binding":"fixture"}\n')
    managed_root = tmp_path / "components"
    (managed_root / "models" / "asr").mkdir(parents=True)
    (managed_root / "models" / "asr" / "weights.bin").write_bytes(b"fixture weights")
    (managed_root / "component-manifest.json").write_text(
        '{"schema_version":1,"components":[]}\n',
        encoding="utf-8",
    )
    before_runtime = runtime_path.read_bytes()
    before_components = _snapshot_files(managed_root)
    wheel = _write_wheel(tmp_path)
    old_health = {**CURRENT_HEALTH, "core_version": "0.2.1"}
    run, _calls = _runner(install_dir, [old_health, CURRENT_HEALTH])
    monkeypatch.setattr(bootstrap_script.subprocess, "run", run)
    monkeypatch.setattr(
        bootstrap_script,
        "bootstrap_media_components",
        lambda *_args, **_kwargs: {"schema_version": 1, "installed": False},
    )

    bootstrap_script.bootstrap(
        install_dir,
        core_wheel=wheel,
        managed_root=managed_root,
    )

    assert runtime_path.read_bytes() == before_runtime
    assert _snapshot_files(managed_root) == before_components


def test_missing_mcp_launcher_updates_the_existing_core(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_dir = tmp_path / "roughcut install"
    _roughcut, mcp = _write_launchers(install_dir)
    mcp.unlink()
    run, calls = _runner(install_dir, [CURRENT_HEALTH])
    monkeypatch.setattr(bootstrap_script.subprocess, "run", run)

    result = bootstrap_script.bootstrap(install_dir)

    assert result["core_action"] == "updated"
    assert any(call[1:3] == ["-m", "pip"] for call in calls)


@pytest.mark.parametrize(
    "invalid_health",
    [
        1,
        "not JSON",
        {**CURRENT_HEALTH, "ok": False},
        OSError("fixture launcher is not executable"),
    ],
    ids=["nonzero", "non-json", "not-ok", "not-executable"],
)
def test_unusable_existing_core_is_updated_before_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_health: object,
) -> None:
    install_dir = tmp_path / "roughcut install"
    _write_launchers(install_dir)
    run, calls = _runner(install_dir, [invalid_health, CURRENT_HEALTH])
    monkeypatch.setattr(bootstrap_script.subprocess, "run", run)

    result = bootstrap_script.bootstrap(install_dir)

    assert result["core_action"] == "updated"
    assert any(call[1:3] == ["-m", "pip"] for call in calls)


@pytest.mark.parametrize(
    "post_update_health",
    [
        {**CURRENT_HEALTH, "tool_schema_version": 21},
        "not JSON",
    ],
    ids=["identity-mismatch", "non-json"],
)
def test_failed_core_update_stops_before_runtime_or_component_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    post_update_health: object,
) -> None:
    install_dir = tmp_path / "roughcut install"
    _write_launchers(install_dir)
    runtime_path = install_dir / "runtime.json"
    runtime_path.write_text("existing binding", encoding="utf-8")
    old_health = {**CURRENT_HEALTH, "tool_schema_version": 21}
    run, _calls = _runner(install_dir, [old_health, post_update_health])
    monkeypatch.setattr(bootstrap_script.subprocess, "run", run)
    monkeypatch.setattr(
        bootstrap_script,
        "bootstrap_media_components",
        lambda *_args, **_kwargs: pytest.fail("components must not run after core failure"),
    )

    with pytest.raises(
        RuntimeError,
        match="Roughcut bootstrap failed to update the installed core:",
    ):
        bootstrap_script.bootstrap(install_dir, managed_root=tmp_path / "managed")

    assert runtime_path.read_text(encoding="utf-8") == "existing binding"


def test_pip_update_failure_stops_before_runtime_or_component_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_dir = tmp_path / "roughcut install"
    _write_launchers(install_dir)
    runtime_path = install_dir / "runtime.json"
    runtime_path.write_text("existing binding", encoding="utf-8")
    old_health = {**CURRENT_HEALTH, "tool_schema_version": 21}
    run, _calls = _runner(install_dir, [old_health], pip_returncode=1)
    monkeypatch.setattr(bootstrap_script.subprocess, "run", run)
    monkeypatch.setattr(
        bootstrap_script,
        "bootstrap_media_components",
        lambda *_args, **_kwargs: pytest.fail("components must not run after core failure"),
    )

    with pytest.raises(
        RuntimeError,
        match="Roughcut bootstrap failed to update the installed core: fixture pip failure",
    ):
        bootstrap_script.bootstrap(install_dir, managed_root=tmp_path / "managed")

    assert runtime_path.read_text(encoding="utf-8") == "existing binding"


@pytest.mark.parametrize(
    "kind",
    ["missing", "directory", "wrong-filename", "wrong-name", "wrong-version", "corrupt"],
)
def test_invalid_core_wheel_fails_closed_before_environment_or_component_work(
    tmp_path: Path,
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_dir = tmp_path / "roughcut install"
    wheel = tmp_path / f"roughcut-{CURRENT_VERSION}-py3-none-any.whl"
    if kind == "directory":
        wheel.mkdir()
    elif kind == "wrong-filename":
        wheel = _write_wheel(
            tmp_path,
            filename=f"other-{CURRENT_VERSION}-py3-none-any.whl",
        )
    elif kind == "wrong-name":
        _write_wheel(tmp_path, name="other")
    elif kind == "wrong-version":
        _write_wheel(tmp_path, version="0.2.3")
    elif kind == "corrupt":
        wheel.write_bytes(b"not a wheel")

    monkeypatch.setattr(
        bootstrap_script,
        "bootstrap_media_components",
        lambda *_args, **_kwargs: pytest.fail(
            "components must not run after wheel validation failure"
        ),
    )
    with pytest.raises(
        RuntimeError,
        match="Roughcut bootstrap Core wheel validation failed",
    ):
        bootstrap_script.bootstrap(
            install_dir,
            core_wheel=wheel,
            managed_root=tmp_path / "managed",
        )
    assert not install_dir.exists()


def test_venv_bin_uses_platform_launcher_location(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bootstrap_script.sys, "platform", "win32")
    assert bootstrap_script.venv_bin(tmp_path / "venv") == tmp_path / "venv" / "Scripts"

    monkeypatch.setattr(bootstrap_script.sys, "platform", "darwin")
    assert bootstrap_script.venv_bin(tmp_path / "venv") == tmp_path / "venv" / "bin"
