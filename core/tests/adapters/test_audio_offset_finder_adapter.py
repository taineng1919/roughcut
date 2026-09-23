"""Adapter contract tests for the BBC audio-offset-finder isolated worker."""

from __future__ import annotations

import inspect
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request as _std_urllib_gate
import wave
from decimal import Decimal
from pathlib import Path

import pytest

from roughcut.adapters.audio_offset_finder import (
    MAX_OUTPUT_BYTES,
    BbcAdapterError,
    BbcDecodeError,
    BbcDuplicateKeyError,
    BbcExtraFieldError,
    BbcFfmpegUnavailableError,
    BbcImportError,
    BbcInvalidOffsetError,
    BbcInvalidScoreError,
    BbcMalformedJsonError,
    BbcMemoryError,
    BbcNonZeroExitError,
    BbcNoOffsetError,
    BbcOutputTooLargeError,
    BbcProviderError,
    BbcProviderMismatchError,
    BbcProviderMissingError,
    BbcResultInvalidError,
    BbcTimeoutError,
    bbc_failure_code,
)
from roughcut.adapters.audio_offset_finder import (
    PROVIDER as BBC_PROVIDER,
)
from roughcut.adapters.audio_offset_finder import (
    VERSION as BBC_VERSION,
)
from roughcut.adapters.audio_offset_finder import (
    run_bbc_offset_finder as _run_bbc_offset_finder,
)
from roughcut.adapters.child_budget import (
    ChildBudget,
    ChildProcessBudgetError,
    ChildProcessMemoryBudgetError,
    ChildProcessTimeBudgetError,
)
from roughcut.adapters.runtime_binding import (
    BBC_AUDIO_OFFSET_FINDER_PROVIDER,
    RuntimeAlignmentPython,
)
from roughcut.domain.alignment import seconds_to_ticks

_real_stdlib_urlopen_gate = _std_urllib_gate.urlopen

CORE_ROOT = Path(__file__).resolve().parents[2]
WORKER_PATH = CORE_ROOT / "src/roughcut/adapters/audio_offset_finder/worker.py"


class _FakeDeadline:
    def __init__(self, remaining: float = 30.0) -> None:
        self._remaining = remaining

    def remaining(self) -> float:
        return self._remaining


def _budget(**overrides: object) -> ChildBudget:
    deadline = _FakeDeadline(float(overrides.get("remaining", 30.0)))  # type: ignore[arg-type]
    return ChildBudget(deadline, int(overrides.get("max_memory_bytes", 1024 * 1024 * 1024)))  # type: ignore[arg-type]


def _test_ffmpeg(workspace_root: Path) -> str:
    if os.environ.get("ROUGHCUT_RUN_REAL_BBC_GATE") == "1":
        command = shutil.which("ffmpeg")
        assert command is not None
        return str(Path(command).resolve())
    command = workspace_root.parent / "test-tools" / "ffmpeg"
    command.parent.mkdir(exist_ok=True)
    if not command.exists():
        command.write_text("#!/bin/sh\nexit 97\n")
        command.chmod(0o755)
    return str(command.resolve())


def run_bbc_offset_finder(*args: object, **kwargs: object) -> object:
    workspace_root = kwargs["workspace_root"]
    assert isinstance(workspace_root, Path)
    kwargs.setdefault("ffmpeg_command", _test_ffmpeg(workspace_root))
    return _run_bbc_offset_finder(*args, **kwargs)  # type: ignore[arg-type,return-value]


def _worker_ffmpeg(tmp_path: Path) -> Path:
    command = shutil.which("ffmpeg")
    assert command is not None
    return Path(command).resolve()


def _audalign_selection() -> RuntimeAlignmentPython:
    return RuntimeAlignmentPython(
        source_type="managed",
        ownership="roughcut_managed",
        interpreter="/tmp/fake-audalign/python",
        python_version="3.11",
        distributions=(),
        dependency_lock_receipt={"algorithm": "sha256", "value": "f" * 64},
        license_notice_receipt={"algorithm": "sha256", "value": "e" * 64},
        component_manifest_receipt={"algorithm": "sha256", "value": "d" * 64},
        provider="audalign",
        provider_version="1.3.1",
        upstream_commit="d5955ae8a85b1cd480dadd005c3f88986f4ebbef",
    )


def _bbc_selection(interpreter: str = "/tmp/fake-bbc/python") -> RuntimeAlignmentPython:
    from roughcut.adapters.runtime_binding import bbc_audio_offset_finder_distribution_versions_for

    try:
        versions = bbc_audio_offset_finder_distribution_versions_for("macos")
    except Exception:  # noqa: BLE001
        versions = {"audio-offset-finder": "0.5.5"}
    dists = tuple({"name": k, "version": v} for k, v in versions.items())
    return RuntimeAlignmentPython(
        source_type="managed",
        ownership="roughcut_managed",
        interpreter=interpreter,
        python_version="3.11",
        distributions=dists,
        dependency_lock_receipt={"algorithm": "sha256", "value": "f" * 64},
        license_notice_receipt={"algorithm": "sha256", "value": "e" * 64},
        component_manifest_receipt={"algorithm": "sha256", "value": "d" * 64},
        provider=BBC_AUDIO_OFFSET_FINDER_PROVIDER,
        provider_version="0.5.5",
        upstream_commit=None,
    )


# ---------------------------------------------------------------------------
# Input contract / provider checks
# ---------------------------------------------------------------------------


def test_core_dependencies_remain_empty_and_adapter_is_isolated() -> None:
    text = (CORE_ROOT / "pyproject.toml").read_text()
    assert "dependencies = []" in text
    # Importing the adapter must not transitively import the third-party package
    import sys as _sys

    assert "audio_offset_finder" not in _sys.modules
    import roughcut.adapters.audio_offset_finder as _mod  # noqa: F401

    assert "audio_offset_finder" not in _sys.modules
    # Subprocess isolation: importing the adapter in a fresh interpreter must not import the package
    result = subprocess.run(
        [
            str(Path(_sys.executable)),
            "-c",
            "import roughcut.adapters.audio_offset_finder; import sys; print('audio_offset_finder' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "False"


def test_provider_missing_is_fail_closed(tmp_path: Path) -> None:
    main = tmp_path / "a.wav"
    aux = tmp_path / "b.wav"
    main.write_bytes(b"")
    aux.write_bytes(b"")
    with pytest.raises(BbcProviderMissingError):
        run_bbc_offset_finder(
            main, aux, selection=None, budget=_budget(), workspace_root=tmp_path
        )


def test_provider_mismatch_rejects_audalign(tmp_path: Path) -> None:
    main = tmp_path / "a.wav"
    aux = tmp_path / "b.wav"
    main.write_bytes(b"")
    aux.write_bytes(b"")
    with pytest.raises(BbcProviderMismatchError):
        run_bbc_offset_finder(
            main,
            aux,
            selection=_audalign_selection(),
            budget=_budget(),
            workspace_root=tmp_path,
        )


def test_stale_interpreter_is_provider_missing(tmp_path: Path) -> None:
    main = tmp_path / "a.wav"
    aux = tmp_path / "b.wav"
    main.write_bytes(b"")
    aux.write_bytes(b"")
    sel = _bbc_selection(interpreter="/nonexistent/python")
    with pytest.raises(BbcProviderMissingError):
        run_bbc_offset_finder(
            main, aux, selection=sel, budget=_budget(), workspace_root=tmp_path
        )


def test_paths_must_be_absolute(tmp_path: Path) -> None:
    sel = _bbc_selection()
    with pytest.raises(BbcAdapterError):
        run_bbc_offset_finder(
            Path("relative.wav"),
            tmp_path / "b.wav",
            selection=sel,
            budget=_budget(),
            workspace_root=tmp_path,
        )


def test_workspace_root_is_required_and_strictly_validated(tmp_path: Path) -> None:
    from roughcut.adapters.audio_offset_finder import _validate_workspace_root

    parameter = inspect.signature(_run_bbc_offset_finder).parameters["workspace_root"]
    assert parameter.default is inspect.Parameter.empty
    assert _validate_workspace_root(tmp_path) == tmp_path

    invalid_roots: list[object] = ["not-a-path", Path("relative"), tmp_path / "missing"]
    file_root = tmp_path / "file"
    file_root.write_text("not a directory")
    invalid_roots.append(file_root)
    symlink_root = tmp_path / "workspace-link"
    symlink_root.symlink_to(tmp_path, target_is_directory=True)
    invalid_roots.append(symlink_root)

    for invalid_root in invalid_roots:
        with pytest.raises(BbcAdapterError):
            _validate_workspace_root(invalid_root)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Child budget error mapping - via monkeypatching run_bounded_child_stream
# ---------------------------------------------------------------------------


def test_timeout_is_mapped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sel = _bbc_selection(interpreter=str(Path(tmp_path / "py").resolve()))
    Path(sel.interpreter).parent.mkdir(parents=True, exist_ok=True)
    Path(sel.interpreter).write_text("#!/bin/sh\nexit 0\n")
    Path(sel.interpreter).chmod(0o755)
    main = tmp_path / "a.wav"
    aux = tmp_path / "b.wav"
    # create dummy wavs so path validation passes
    _write_sine_wav(main, duration=0.5)
    _write_sine_wav(aux, duration=0.5)

    def _raise(*_a: object, **_k: object) -> None:
        raise ChildProcessTimeBudgetError("deadline exceeded")

    monkeypatch.setattr("roughcut.adapters.audio_offset_finder.run_bounded_child_stream", _raise)
    with pytest.raises(BbcTimeoutError):
        run_bbc_offset_finder(
            main, aux, selection=sel, budget=_budget(), workspace_root=tmp_path
        )


def test_memory_is_mapped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sel = _bbc_selection(interpreter=str(Path(tmp_path / "py2").resolve()))
    Path(sel.interpreter).parent.mkdir(parents=True, exist_ok=True)
    Path(sel.interpreter).write_text("#!/bin/sh\nexit 0\n")
    Path(sel.interpreter).chmod(0o755)
    main = tmp_path / "a2.wav"
    aux = tmp_path / "b2.wav"
    _write_sine_wav(main, duration=0.5)
    _write_sine_wav(aux, duration=0.5)

    def _raise(*_a: object, **_k: object) -> None:
        raise ChildProcessMemoryBudgetError("memory exceeded")

    monkeypatch.setattr("roughcut.adapters.audio_offset_finder.run_bounded_child_stream", _raise)
    with pytest.raises(BbcMemoryError):
        run_bbc_offset_finder(
            main, aux, selection=sel, budget=_budget(), workspace_root=tmp_path
        )


def test_nonzero_exit_is_mapped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sel = _bbc_selection(interpreter=str(Path(tmp_path / "py3").resolve()))
    Path(sel.interpreter).parent.mkdir(parents=True, exist_ok=True)
    Path(sel.interpreter).write_text("#!/bin/sh\nexit 0\n")
    Path(sel.interpreter).chmod(0o755)
    main = tmp_path / "a3.wav"
    aux = tmp_path / "b3.wav"
    _write_sine_wav(main, duration=0.5)
    _write_sine_wav(aux, duration=0.5)

    def _fake_run(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 2, "", "audio_offset_finder import failed")

    monkeypatch.setattr("roughcut.adapters.audio_offset_finder.run_bounded_child_stream", _fake_run)
    with pytest.raises(BbcNonZeroExitError):
        run_bbc_offset_finder(
            main, aux, selection=sel, budget=_budget(), workspace_root=tmp_path
        )


@pytest.mark.parametrize(
    ("returncode", "error_type", "evidence_code"),
    [
        (3, BbcImportError, "finder_import_failed"),
        (4, BbcFfmpegUnavailableError, "finder_ffmpeg_unavailable"),
        (5, BbcDecodeError, "finder_decode_failed"),
        (6, BbcProviderError, "finder_provider_failed"),
        (7, BbcResultInvalidError, "finder_result_invalid"),
    ],
)
def test_worker_failure_codes_map_to_closed_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    error_type: type[BbcAdapterError],
    evidence_code: str,
) -> None:
    sel = _bbc_selection(interpreter=str(Path(tmp_path / "python").resolve()))
    Path(sel.interpreter).write_text("#!/bin/sh\nexit 0\n")
    Path(sel.interpreter).chmod(0o755)
    main = tmp_path / "main.wav"
    auxiliary = tmp_path / "auxiliary.wav"
    _write_sine_wav(main, duration=0.5)
    _write_sine_wav(auxiliary, duration=0.5)
    monkeypatch.setattr(
        "roughcut.adapters.audio_offset_finder.run_bounded_child_stream",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, returncode, b"", b""),
    )
    with pytest.raises(error_type) as caught:
        run_bbc_offset_finder(
            main,
            auxiliary,
            selection=sel,
            budget=_budget(),
            workspace_root=tmp_path,
        )
    assert bbc_failure_code(caught.value) == evidence_code


def test_adapter_uses_only_verified_ffmpeg_directory_with_poisoned_parent_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sel = _bbc_selection(interpreter=str(Path(tmp_path / "python").resolve()))
    Path(sel.interpreter).write_text("#!/bin/sh\nexit 0\n")
    Path(sel.interpreter).chmod(0o755)
    main = tmp_path / "main.wav"
    auxiliary = tmp_path / "auxiliary.wav"
    _write_sine_wav(main, duration=0.5)
    _write_sine_wav(auxiliary, duration=0.5)
    poison = tmp_path / "poison" / "ffmpeg"
    poison.parent.mkdir()
    poison.write_text("#!/bin/sh\ntouch poison-used\nexit 91\n")
    poison.chmod(0o755)
    monkeypatch.setenv("PATH", str(poison.parent))

    def _fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        budget = kwargs["budget"]
        assert isinstance(budget, ChildBudget)
        assert budget.apply_tmpdir is not None
        environment = budget.apply_tmpdir({"PATH": str(poison.parent)})
        verified = Path(command[command.index("--ffmpeg-command") + 1])
        assert environment["PATH"] == str(verified.parent)
        output = Path(command[command.index("--output") + 1])
        output.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "provider": BBC_PROVIDER,
                    "provider_version": BBC_VERSION,
                    "native_offset_seconds": "0.25",
                    "standard_score": "2.0",
                    "analysis": {},
                }
            )
        )
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(
        "roughcut.adapters.audio_offset_finder.run_bounded_child_stream", _fake_run
    )
    result = run_bbc_offset_finder(
        main,
        auxiliary,
        selection=sel,
        budget=_budget(),
        workspace_root=tmp_path,
    )
    assert result.native_offset_seconds == "0.25"
    assert not (Path.cwd() / "poison-used").exists()


def test_import_error_is_mapped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sel = _bbc_selection(interpreter=str(Path(tmp_path / "py4").resolve()))
    Path(sel.interpreter).parent.mkdir(parents=True, exist_ok=True)
    Path(sel.interpreter).write_text("#!/bin/sh\nexit 0\n")
    Path(sel.interpreter).chmod(0o755)
    main = tmp_path / "a4.wav"
    aux = tmp_path / "b4.wav"
    _write_sine_wav(main, duration=0.5)
    _write_sine_wav(aux, duration=0.5)

    def _fake_run(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 3, "untrusted output", "untrusted error")

    monkeypatch.setattr("roughcut.adapters.audio_offset_finder.run_bounded_child_stream", _fake_run)
    with pytest.raises(BbcImportError):
        run_bbc_offset_finder(
            main, aux, selection=sel, budget=_budget(), workspace_root=tmp_path
        )


def test_output_too_large_via_stdout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sel = _bbc_selection(interpreter=str(Path(tmp_path / "py5").resolve()))
    Path(sel.interpreter).parent.mkdir(parents=True, exist_ok=True)
    Path(sel.interpreter).write_text("#!/bin/sh\nexit 0\n")
    Path(sel.interpreter).chmod(0o755)
    main = tmp_path / "a5.wav"
    aux = tmp_path / "b5.wav"
    _write_sine_wav(main, duration=0.5)
    _write_sine_wav(aux, duration=0.5)

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        on_stdout = kwargs.get("on_stdout")
        if callable(on_stdout):
            on_stdout(b"x" * 40000)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr("roughcut.adapters.audio_offset_finder.run_bounded_child_stream", _fake_run)
    with pytest.raises(BbcOutputTooLargeError):
        run_bbc_offset_finder(
            main, aux, selection=sel, budget=_budget(), workspace_root=tmp_path
        )


# ---------------------------------------------------------------------------
# JSON contract - malformed / duplicate / extra / invalid offset/score / no offset
# ---------------------------------------------------------------------------


def test_malformed_json_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sel = _bbc_selection(interpreter=str(Path(tmp_path / "py6").resolve()))
    Path(sel.interpreter).parent.mkdir(parents=True, exist_ok=True)
    Path(sel.interpreter).write_text("#!/bin/sh\nexit 0\n")
    Path(sel.interpreter).chmod(0o755)
    main = tmp_path / "a6.wav"
    aux = tmp_path / "b6.wav"
    _write_sine_wav(main, duration=0.5)
    _write_sine_wav(aux, duration=0.5)

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        # cmd contains --output <path>; write malformed json there
        out = Path(cmd[cmd.index("--output") + 1])
        out.write_text("{not json")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("roughcut.adapters.audio_offset_finder.run_bounded_child_stream", _fake_run)
    with pytest.raises(BbcMalformedJsonError):
        run_bbc_offset_finder(
            main, aux, selection=sel, budget=_budget(), workspace_root=tmp_path
        )


def test_duplicate_key_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sel = _bbc_selection(interpreter=str(Path(tmp_path / "py7").resolve()))
    Path(sel.interpreter).parent.mkdir(parents=True, exist_ok=True)
    Path(sel.interpreter).write_text("#!/bin/sh\nexit 0\n")
    Path(sel.interpreter).chmod(0o755)
    main = tmp_path / "a7.wav"
    aux = tmp_path / "b7.wav"
    _write_sine_wav(main, duration=0.5)
    _write_sine_wav(aux, duration=0.5)

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        out = Path(cmd[cmd.index("--output") + 1])
        out.write_text(
            '{"schema_version": 1, "schema_version": 1, "provider": "bbc_audio_offset_finder", "provider_version": "0.5.5", "native_offset_seconds": "0.1", "standard_score": "1.0", "analysis": {}}'
        )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("roughcut.adapters.audio_offset_finder.run_bounded_child_stream", _fake_run)
    with pytest.raises(BbcDuplicateKeyError):
        run_bbc_offset_finder(
            main, aux, selection=sel, budget=_budget(), workspace_root=tmp_path
        )


def test_extra_field_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sel = _bbc_selection(interpreter=str(Path(tmp_path / "py8").resolve()))
    Path(sel.interpreter).parent.mkdir(parents=True, exist_ok=True)
    Path(sel.interpreter).write_text("#!/bin/sh\nexit 0\n")
    Path(sel.interpreter).chmod(0o755)
    main = tmp_path / "a8.wav"
    aux = tmp_path / "b8.wav"
    _write_sine_wav(main, duration=0.5)
    _write_sine_wav(aux, duration=0.5)

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        out = Path(cmd[cmd.index("--output") + 1])
        payload = {
            "schema_version": 1,
            "provider": BBC_PROVIDER,
            "provider_version": BBC_VERSION,
            "native_offset_seconds": "0.1",
            "standard_score": "1.0",
            "analysis": {},
            "unexpected": 123,
        }
        out.write_text(json.dumps(payload))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("roughcut.adapters.audio_offset_finder.run_bounded_child_stream", _fake_run)
    with pytest.raises(BbcExtraFieldError):
        run_bbc_offset_finder(
            main, aux, selection=sel, budget=_budget(), workspace_root=tmp_path
        )


def test_invalid_offset_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sel = _bbc_selection(interpreter=str(Path(tmp_path / "py9").resolve()))
    Path(sel.interpreter).parent.mkdir(parents=True, exist_ok=True)
    Path(sel.interpreter).write_text("#!/bin/sh\nexit 0\n")
    Path(sel.interpreter).chmod(0o755)
    main = tmp_path / "a9.wav"
    aux = tmp_path / "b9.wav"
    _write_sine_wav(main, duration=0.5)
    _write_sine_wav(aux, duration=0.5)

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        out = Path(cmd[cmd.index("--output") + 1])
        payload = {
            "schema_version": 1,
            "provider": BBC_PROVIDER,
            "provider_version": BBC_VERSION,
            "native_offset_seconds": "not-a-number",
            "standard_score": "1.0",
            "analysis": {},
        }
        out.write_text(json.dumps(payload))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("roughcut.adapters.audio_offset_finder.run_bounded_child_stream", _fake_run)
    with pytest.raises(BbcInvalidOffsetError):
        run_bbc_offset_finder(
            main, aux, selection=sel, budget=_budget(), workspace_root=tmp_path
        )


def test_invalid_score_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sel = _bbc_selection(interpreter=str(Path(tmp_path / "py10").resolve()))
    Path(sel.interpreter).parent.mkdir(parents=True, exist_ok=True)
    Path(sel.interpreter).write_text("#!/bin/sh\nexit 0\n")
    Path(sel.interpreter).chmod(0o755)
    main = tmp_path / "a10.wav"
    aux = tmp_path / "b10.wav"
    _write_sine_wav(main, duration=0.5)
    _write_sine_wav(aux, duration=0.5)

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        out = Path(cmd[cmd.index("--output") + 1])
        payload = {
            "schema_version": 1,
            "provider": BBC_PROVIDER,
            "provider_version": BBC_VERSION,
            "native_offset_seconds": "0.1",
            "standard_score": "inf",
            "analysis": {},
        }
        out.write_text(json.dumps(payload))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("roughcut.adapters.audio_offset_finder.run_bounded_child_stream", _fake_run)
    with pytest.raises(BbcInvalidScoreError):
        run_bbc_offset_finder(
            main, aux, selection=sel, budget=_budget(), workspace_root=tmp_path
        )


def test_no_offset_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sel = _bbc_selection(interpreter=str(Path(tmp_path / "py11").resolve()))
    Path(sel.interpreter).parent.mkdir(parents=True, exist_ok=True)
    Path(sel.interpreter).write_text("#!/bin/sh\nexit 0\n")
    Path(sel.interpreter).chmod(0o755)
    main = tmp_path / "a11.wav"
    aux = tmp_path / "b11.wav"
    _write_sine_wav(main, duration=0.5)
    _write_sine_wav(aux, duration=0.5)

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        out = Path(cmd[cmd.index("--output") + 1])
        payload = {
            "schema_version": 1,
            "provider": BBC_PROVIDER,
            "provider_version": BBC_VERSION,
            "native_offset_seconds": None,
            "standard_score": None,
            "analysis": {"error": "insufficient_audio"},
        }
        out.write_text(json.dumps(payload))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("roughcut.adapters.audio_offset_finder.run_bounded_child_stream", _fake_run)
    with pytest.raises(BbcNoOffsetError):
        run_bbc_offset_finder(
            main, aux, selection=sel, budget=_budget(), workspace_root=tmp_path
        )


def test_output_too_large_via_json_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sel = _bbc_selection(interpreter=str(Path(tmp_path / "py12").resolve()))
    Path(sel.interpreter).parent.mkdir(parents=True, exist_ok=True)
    Path(sel.interpreter).write_text("#!/bin/sh\nexit 0\n")
    Path(sel.interpreter).chmod(0o755)
    main = tmp_path / "a12.wav"
    aux = tmp_path / "b12.wav"
    _write_sine_wav(main, duration=0.5)
    _write_sine_wav(aux, duration=0.5)

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        out = Path(cmd[cmd.index("--output") + 1])
        # write a payload larger than max_output_bytes
        payload = {
            "schema_version": 1,
            "provider": BBC_PROVIDER,
            "provider_version": BBC_VERSION,
            "native_offset_seconds": "0.1",
            "standard_score": "1.0",
            "analysis": {"blob": "x" * 70000},
        }
        out.write_text(json.dumps(payload))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("roughcut.adapters.audio_offset_finder.run_bounded_child_stream", _fake_run)
    with pytest.raises(BbcOutputTooLargeError):
        run_bbc_offset_finder(
            main,
            aux,
            selection=sel,
            budget=_budget(),
            workspace_root=tmp_path,
            max_output_bytes=1024,
        )


def test_generic_budget_error_is_mapped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sel = _bbc_selection(interpreter=str(Path(tmp_path / "py_gen").resolve()))
    Path(sel.interpreter).parent.mkdir(parents=True, exist_ok=True)
    Path(sel.interpreter).write_text("#!/bin/sh\nexit 0\n")
    Path(sel.interpreter).chmod(0o755)
    main = tmp_path / "gen_main.wav"
    aux = tmp_path / "gen_aux.wav"
    _write_sine_wav(main, duration=0.5)
    _write_sine_wav(aux, duration=0.5)

    def _raise(*_a: object, **_k: object) -> None:
        raise ChildProcessBudgetError("generic budget")

    monkeypatch.setattr("roughcut.adapters.audio_offset_finder.run_bounded_child_stream", _raise)
    with pytest.raises(BbcAdapterError):
        run_bbc_offset_finder(
            main, aux, selection=sel, budget=_budget(), workspace_root=tmp_path
        )
    # ensure not misclassified as timeout/memory
    try:
        run_bbc_offset_finder(
            main, aux, selection=sel, budget=_budget(), workspace_root=tmp_path
        )
        assert False
    except BbcTimeoutError:
        assert False, "generic budget must not map to timeout"
    except BbcMemoryError:
        assert False, "generic budget must not map to memory"
    except BbcAdapterError:
        pass


def test_child_start_oserror_is_mapped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sel = _bbc_selection(interpreter=str(Path(tmp_path / "py_os").resolve()))
    Path(sel.interpreter).parent.mkdir(parents=True, exist_ok=True)
    Path(sel.interpreter).write_text("#!/bin/sh\nexit 0\n")
    Path(sel.interpreter).chmod(0o755)
    main = tmp_path / "os_main.wav"
    aux = tmp_path / "os_aux.wav"
    _write_sine_wav(main, duration=0.5)
    _write_sine_wav(aux, duration=0.5)

    def _raise_os(*_a: object, **_k: object) -> None:
        raise OSError("no exec")

    monkeypatch.setattr("roughcut.adapters.audio_offset_finder.run_bounded_child_stream", _raise_os)
    with pytest.raises(BbcAdapterError):
        run_bbc_offset_finder(
            main, aux, selection=sel, budget=_budget(), workspace_root=tmp_path
        )

    def _raise_val(*_a: object, **_k: object) -> None:
        raise ValueError("bad value")

    monkeypatch.setattr(
        "roughcut.adapters.audio_offset_finder.run_bounded_child_stream", _raise_val
    )
    with pytest.raises(BbcAdapterError):
        run_bbc_offset_finder(
            main, aux, selection=sel, budget=_budget(), workspace_root=tmp_path
        )


def test_worker_cli_provider_mismatch_has_fixed_code(tmp_path: Path) -> None:
    main = tmp_path / "w_main.wav"
    aux = tmp_path / "w_aux.wav"
    out = tmp_path / "w_out.json"
    _write_sine_wav(main, duration=0.5)
    _write_sine_wav(aux, duration=0.5)
    out.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            str(Path(sys.executable)),
            str(WORKER_PATH),
            "--main",
            str(main),
            "--aux",
            str(aux),
            "--output",
            str(out),
            "--provider",
            "wrong",
            "--provider-version",
            "0.5.5",
            "--ffmpeg-command",
            str(_worker_ffmpeg(tmp_path)),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "provider identity mismatch" in result.stderr
    assert "Traceback" not in result.stderr
    assert "Traceback" not in result.stdout


def test_worker_cli_output_parent_must_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from roughcut.adapters.audio_offset_finder import worker

    main = tmp_path / "w2_main.wav"
    aux = tmp_path / "w2_aux.wav"
    _write_sine_wav(main, duration=0.5)
    _write_sine_wav(aux, duration=0.5)
    out = tmp_path / "nonexistent_dir" / "out.json"
    finder_result = {"time_offset": 0.1, "standard_score": 3.0}

    class _FinderModule:
        @staticmethod
        def find_offset_between_files(_main: str, _aux: str) -> dict[str, object]:
            return finder_result

    monkeypatch.setattr(worker, "__import__", lambda *_args, **_kwargs: _FinderModule, raising=False)
    returncode = worker.main(
        [
            "--main",
            str(main),
            "--aux",
            str(aux),
            "--output",
            str(out),
            "--provider",
            BBC_PROVIDER,
            "--provider-version",
            BBC_VERSION,
            "--ffmpeg-command",
            str(_worker_ffmpeg(tmp_path)),
        ]
    )
    captured = capsys.readouterr()
    assert returncode == 2
    assert captured.err == "output parent missing\n"
    assert "Traceback" not in captured.err
    assert not out.exists()


def test_worker_cli_import_failure_has_stable_code(tmp_path: Path) -> None:
    main = tmp_path / "w3_main.wav"
    aux = tmp_path / "w3_aux.wav"
    _write_sine_wav(main, duration=0.5)
    _write_sine_wav(aux, duration=0.5)
    out = tmp_path / "w3_out.json"
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    env["PYTHONPATH"] = ""
    result = subprocess.run(
        [
            str(Path(sys.executable)),
            "-I",
            str(WORKER_PATH),
            "--main",
            str(main),
            "--aux",
            str(aux),
            "--output",
            str(out),
            "--provider",
            BBC_PROVIDER,
            "--provider-version",
            BBC_VERSION,
            "--ffmpeg-command",
            str(_worker_ffmpeg(tmp_path)),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    # In core venv audio_offset_finder is not installed, so it must fail with code 3 and fixed message
    assert result.returncode == 3
    assert result.stderr == "finder_import_failed\n"
    assert "Traceback" not in result.stderr


def test_worker_cli_insufficient_audio_is_no_offset_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from roughcut.adapters.audio_offset_finder import worker

    main = tmp_path / "short_main.wav"
    aux = tmp_path / "short_aux.wav"
    _write_sine_wav(main, duration=0.15)
    _write_sine_wav(aux, duration=0.15)
    out = tmp_path / "short_out.json"

    class _InsufficientAudioException(Exception):
        pass

    class _FinderModule:
        InsufficientAudioException = _InsufficientAudioException

        @staticmethod
        def find_offset_between_files(_main: str, _aux: str) -> dict[str, object]:
            raise _InsufficientAudioException

    monkeypatch.setattr(worker, "__import__", lambda *_args, **_kwargs: _FinderModule, raising=False)
    returncode = worker.main(
        [
            "--main",
            str(main),
            "--aux",
            str(aux),
            "--output",
            str(out),
            "--provider",
            BBC_PROVIDER,
            "--provider-version",
            BBC_VERSION,
            "--ffmpeg-command",
            str(_worker_ffmpeg(tmp_path)),
        ]
    )
    captured = capsys.readouterr()
    assert returncode == 0
    assert captured.err == ""
    payload = json.loads(out.read_text())
    assert payload["native_offset_seconds"] is None
    assert payload["analysis"]["error"] == "insufficient_audio"


@pytest.mark.parametrize(
    ("failure", "returncode", "stderr"),
    [
        (OSError("private native runtime path"), 6, "finder_provider_failed\n"),
        (RuntimeError("FFMpeg failed: private media path"), 5, "finder_decode_failed\n"),
        (FileNotFoundError("private ffmpeg path"), 4, "finder_ffmpeg_unavailable\n"),
        (ValueError("private malformed WAV"), 5, "finder_decode_failed\n"),
        (RuntimeError("private provider value"), 6, "finder_provider_failed\n"),
    ],
)
def test_worker_provider_failures_are_closed_bounded_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: Exception,
    returncode: int,
    stderr: str,
) -> None:
    from roughcut.adapters.audio_offset_finder import worker

    main = tmp_path / "main.wav"
    auxiliary = tmp_path / "auxiliary.wav"
    main.write_bytes(b"main")
    auxiliary.write_bytes(b"auxiliary")

    class _FinderModule:
        class InsufficientAudioException(Exception):
            pass

        @staticmethod
        def find_offset_between_files(_main: str, _auxiliary: str) -> object:
            raise failure

    monkeypatch.setattr(
        worker, "__import__", lambda *_args, **_kwargs: _FinderModule, raising=False
    )
    result = worker.main(
        [
            "--main",
            str(main),
            "--aux",
            str(auxiliary),
            "--output",
            str(tmp_path / "output.json"),
            "--provider",
            BBC_PROVIDER,
            "--provider-version",
            BBC_VERSION,
            "--ffmpeg-command",
            str(_worker_ffmpeg(tmp_path)),
        ]
    )
    captured = capsys.readouterr()
    assert result == returncode
    assert captured.err == stderr
    assert "private" not in captured.err
    assert "Traceback" not in captured.err


def test_worker_rejects_path_ffmpeg_that_is_not_the_verified_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from roughcut.adapters.audio_offset_finder import worker

    main = tmp_path / "main.wav"
    auxiliary = tmp_path / "auxiliary.wav"
    main.write_bytes(b"main")
    auxiliary.write_bytes(b"auxiliary")
    poison = tmp_path / "poison" / "ffmpeg"
    poison.parent.mkdir()
    poison.write_text("#!/bin/sh\nexit 91\n")
    poison.chmod(0o755)
    verified = _worker_ffmpeg(tmp_path)
    monkeypatch.setenv("PATH", str(poison.parent))
    result = worker.main(
        [
            "--main",
            str(main),
            "--aux",
            str(auxiliary),
            "--output",
            str(tmp_path / "output.json"),
            "--provider",
            BBC_PROVIDER,
            "--provider-version",
            BBC_VERSION,
            "--ffmpeg-command",
            str(verified),
        ]
    )
    captured = capsys.readouterr()
    assert result == 4
    assert captured.err == "finder_ffmpeg_unavailable\n"


@pytest.mark.parametrize(
    ("field", "value", "expected_stderr"),
    [
        ("time_offset", 10**1000, "finder_result_invalid\n"),
        ("time_offset", float("nan"), "finder_result_invalid\n"),
        ("time_offset", float("inf"), "finder_result_invalid\n"),
        ("standard_score", 10**1000, "finder_result_invalid\n"),
        ("standard_score", float("nan"), "finder_result_invalid\n"),
        ("standard_score", float("inf"), "finder_result_invalid\n"),
        ("time_scale", 10**1000, "finder_result_invalid\n"),
        ("time_scale", float("nan"), "finder_result_invalid\n"),
        ("time_scale", float("inf"), "finder_result_invalid\n"),
        (None, None, "finder_result_invalid\n"),
    ],
)
def test_worker_invalid_provider_numbers_fail_closed_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    field: str | None,
    value: object,
    expected_stderr: str,
) -> None:
    from roughcut.adapters.audio_offset_finder import worker

    main = tmp_path / "invalid_main.wav"
    aux = tmp_path / "invalid_aux.wav"
    main.write_bytes(b"main")
    aux.write_bytes(b"aux")
    out = tmp_path / "invalid_out.json"
    finder_result: object = {
        "time_offset": 0.1,
        "standard_score": 2.0,
        "time_scale": 1.0,
    }
    if field is None:
        finder_result = []
    else:
        assert isinstance(finder_result, dict)
        finder_result[field] = value

    class _FinderModule:
        @staticmethod
        def find_offset_between_files(_main: str, _aux: str) -> object:
            return finder_result

    monkeypatch.setattr(worker, "__import__", lambda *_args, **_kwargs: _FinderModule, raising=False)
    returncode = worker.main(
        [
            "--main",
            str(main),
            "--aux",
            str(aux),
            "--output",
            str(out),
            "--provider",
            BBC_PROVIDER,
            "--provider-version",
            BBC_VERSION,
            "--ffmpeg-command",
            str(_worker_ffmpeg(tmp_path)),
        ]
    )
    captured = capsys.readouterr()
    assert returncode == 7
    assert captured.err == expected_stderr
    assert captured.out == ""
    assert "Traceback" not in captured.err
    assert not out.exists()


def test_analysis_16k_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sel = _bbc_selection(interpreter=str(Path(tmp_path / "py_16k").resolve()))
    Path(sel.interpreter).parent.mkdir(parents=True, exist_ok=True)
    Path(sel.interpreter).write_text("#!/bin/sh\nexit 0\n")
    Path(sel.interpreter).chmod(0o755)
    main = tmp_path / "16k_main.wav"
    aux = tmp_path / "16k_aux.wav"
    _write_sine_wav(main, duration=0.5)
    _write_sine_wav(aux, duration=0.5)

    # exactly 16 KiB analysis should pass — use allowed time_scale decimal
    analysis_ok = {"frame_offset": 1, "time_scale": "0." + "1" * (14 * 1024)}
    ok_payload = {
        "schema_version": 1,
        "provider": BBC_PROVIDER,
        "provider_version": BBC_VERSION,
        "native_offset_seconds": "0.1",
        "standard_score": "1.0",
        "analysis": analysis_ok,
    }
    from roughcut.adapters.audio_offset_finder import _validate_payload

    # Should not raise — analysis ~14k <16k
    _validate_payload({**ok_payload, "analysis": analysis_ok})

    # 16k +1 should fail — time_scale huge
    analysis_big = {"frame_offset": 1, "time_scale": "0." + "1" * (17 * 1024)}
    big_payload = {
        "schema_version": 1,
        "provider": BBC_PROVIDER,
        "provider_version": BBC_VERSION,
        "native_offset_seconds": "0.1",
        "standard_score": "1.0",
        "analysis": analysis_big,
    }

    def _fake_big(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        out = Path(cmd[cmd.index("--output") + 1])
        out.write_text(json.dumps(big_payload))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("roughcut.adapters.audio_offset_finder.run_bounded_child_stream", _fake_big)
    with pytest.raises(BbcOutputTooLargeError):
        run_bbc_offset_finder(
            main, aux, selection=sel, budget=_budget(), workspace_root=tmp_path
        )


def test_missing_keys_and_null_score_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sel = _bbc_selection(interpreter=str(Path(tmp_path / "py_miss").resolve()))
    Path(sel.interpreter).parent.mkdir(parents=True, exist_ok=True)
    Path(sel.interpreter).write_text("#!/bin/sh\nexit 0\n")
    Path(sel.interpreter).chmod(0o755)
    main = tmp_path / "miss_main.wav"
    aux = tmp_path / "miss_aux.wav"
    _write_sine_wav(main, duration=0.5)
    _write_sine_wav(aux, duration=0.5)

    # missing native_offset_seconds
    def _fake_missing(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        out = Path(cmd[cmd.index("--output") + 1])
        payload = {
            "schema_version": 1,
            "provider": BBC_PROVIDER,
            "provider_version": BBC_VERSION,
            # "native_offset_seconds" missing
            "standard_score": "1.0",
            "analysis": {},
        }
        out.write_text(json.dumps(payload))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(
        "roughcut.adapters.audio_offset_finder.run_bounded_child_stream", _fake_missing
    )
    with pytest.raises(BbcMalformedJsonError):
        run_bbc_offset_finder(
            main, aux, selection=sel, budget=_budget(), workspace_root=tmp_path
        )

    # null score with offset present
    def _fake_null_score(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        out = Path(cmd[cmd.index("--output") + 1])
        payload = {
            "schema_version": 1,
            "provider": BBC_PROVIDER,
            "provider_version": BBC_VERSION,
            "native_offset_seconds": "0.1",
            "standard_score": None,
            "analysis": {},
        }
        out.write_text(json.dumps(payload))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(
        "roughcut.adapters.audio_offset_finder.run_bounded_child_stream", _fake_null_score
    )
    with pytest.raises(BbcInvalidScoreError):
        run_bbc_offset_finder(
            main, aux, selection=sel, budget=_budget(), workspace_root=tmp_path
        )


def test_json_nan_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sel = _bbc_selection(interpreter=str(Path(tmp_path / "py_nan").resolve()))
    Path(sel.interpreter).parent.mkdir(parents=True, exist_ok=True)
    Path(sel.interpreter).write_text("#!/bin/sh\nexit 0\n")
    Path(sel.interpreter).chmod(0o755)
    main = tmp_path / "nan_main.wav"
    aux = tmp_path / "nan_aux.wav"
    _write_sine_wav(main, duration=0.5)
    _write_sine_wav(aux, duration=0.5)

    def _fake_nan(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        out = Path(cmd[cmd.index("--output") + 1])
        out.write_text(
            '{"schema_version": 1, "provider": "bbc_audio_offset_finder", "provider_version": "0.5.5", "native_offset_seconds": NaN, "standard_score": "1.0", "analysis": {}}'
        )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("roughcut.adapters.audio_offset_finder.run_bounded_child_stream", _fake_nan)
    with pytest.raises(BbcMalformedJsonError):
        run_bbc_offset_finder(
            main, aux, selection=sel, budget=_budget(), workspace_root=tmp_path
        )


def test_output_symlink_and_oversize_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sel = _bbc_selection(interpreter=str(Path(tmp_path / "py_sym").resolve()))
    Path(sel.interpreter).parent.mkdir(parents=True, exist_ok=True)
    Path(sel.interpreter).write_text("#!/bin/sh\nexit 0\n")
    Path(sel.interpreter).chmod(0o755)
    main = tmp_path / "sym_main.wav"
    aux = tmp_path / "sym_aux.wav"
    _write_sine_wav(main, duration=0.5)
    _write_sine_wav(aux, duration=0.5)

    # symlink
    real_out = tmp_path / "real.json"
    real_out.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "provider": BBC_PROVIDER,
                "provider_version": BBC_VERSION,
                "native_offset_seconds": "0.1",
                "standard_score": "1.0",
                "analysis": {},
            }
        )
    )

    def _fake_symlink(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        out = Path(cmd[cmd.index("--output") + 1])
        # output_path is tmp_dir/output.json; we replace it with symlink to real
        if out.exists():
            out.unlink()
        out.symlink_to(real_out)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(
        "roughcut.adapters.audio_offset_finder.run_bounded_child_stream", _fake_symlink
    )
    with pytest.raises(BbcMalformedJsonError):
        run_bbc_offset_finder(
            main, aux, selection=sel, budget=_budget(), workspace_root=tmp_path
        )

    # oversize before read: create a file larger than max via symlink target size?
    # Instead test lstat size check by having fake write oversized file
    def _fake_oversize(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        out = Path(cmd[cmd.index("--output") + 1])
        out.write_bytes(b"x" * (MAX_OUTPUT_BYTES + 1))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(
        "roughcut.adapters.audio_offset_finder.run_bounded_child_stream", _fake_oversize
    )
    with pytest.raises(BbcOutputTooLargeError):
        run_bbc_offset_finder(
            main, aux, selection=sel, budget=_budget(), workspace_root=tmp_path
        )


def test_workspace_is_controlled_and_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sel = _bbc_selection(interpreter=str(Path(tmp_path / "py_ws").resolve()))
    Path(sel.interpreter).parent.mkdir(parents=True, exist_ok=True)
    Path(sel.interpreter).write_text("#!/bin/sh\nexit 0\n")
    Path(sel.interpreter).chmod(0o755)
    main = tmp_path / "ws_main.wav"
    aux = tmp_path / "ws_aux.wav"
    _write_sine_wav(main, duration=0.5)
    _write_sine_wav(aux, duration=0.5)
    ws = tmp_path / "workspace"
    ws.mkdir()
    external_before = (
        set(Path(tempfile.gettempdir()).iterdir())
        if Path(tempfile.gettempdir()).is_dir()
        else set()
    )

    def _fake_ok(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        out = Path(cmd[cmd.index("--output") + 1])
        assert str(out).startswith(str(ws))
        nested = out.parent / "nested" / "provider-cache"
        nested.parent.mkdir(parents=True)
        nested.write_text("temporary")
        payload = {
            "schema_version": 1,
            "provider": BBC_PROVIDER,
            "provider_version": BBC_VERSION,
            "native_offset_seconds": "0.2",
            "standard_score": "2.0",
            "analysis": {},
        }
        out.write_text(json.dumps(payload))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("roughcut.adapters.audio_offset_finder.run_bounded_child_stream", _fake_ok)
    result = run_bbc_offset_finder(main, aux, selection=sel, budget=_budget(), workspace_root=ws)
    assert result.native_offset_seconds == "0.2"
    # workspace should be empty after cleanup (tmp_dir removed)
    assert list(ws.iterdir()) == []
    # external tmp should not have leaked file
    external_after = (
        set(Path(tempfile.gettempdir()).iterdir())
        if Path(tempfile.gettempdir()).is_dir()
        else set()
    )
    assert external_after == external_before

    # failure path also cleans
    def _fake_fail(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        out = Path(cmd[cmd.index("--output") + 1])
        assert str(out).startswith(str(ws))
        nested = out.parent / "nested" / "provider-cache"
        nested.parent.mkdir(parents=True)
        nested.write_text("temporary")
        out.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "provider": BBC_PROVIDER,
                    "provider_version": BBC_VERSION,
                    "native_offset_seconds": "0.3",
                    "standard_score": "3.0",
                    "analysis": {},
                }
            )
        )
        return subprocess.CompletedProcess(cmd, 1, "", "error")

    monkeypatch.setattr(
        "roughcut.adapters.audio_offset_finder.run_bounded_child_stream", _fake_fail
    )
    with pytest.raises(BbcNonZeroExitError):
        run_bbc_offset_finder(main, aux, selection=sel, budget=_budget(), workspace_root=ws)
    assert list(ws.iterdir()) == []


# ---------------------------------------------------------------------------
# Ticks conversion - Decimal, not float
# ---------------------------------------------------------------------------


def test_decimal_conversion_negative_6_144() -> None:
    assert seconds_to_ticks("-6.144") == -737280
    assert seconds_to_ticks("-6.144000") == -737280


def test_decimal_conversion_positive_offsets() -> None:
    assert seconds_to_ticks("0.288") == 34560
    assert seconds_to_ticks("0.320") == 38400
    assert seconds_to_ticks("0.276") == 33120


def test_decimal_conversion_rejects_nonfinite() -> None:
    with pytest.raises(Exception):  # noqa: B017
        seconds_to_ticks("inf")
    with pytest.raises(Exception):  # noqa: B017
        seconds_to_ticks("nan")


def test_positive_offset_string_is_preserved_via_mocked_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sel = _bbc_selection(interpreter=str(Path(tmp_path / "py_pos").resolve()))
    Path(sel.interpreter).parent.mkdir(parents=True, exist_ok=True)
    Path(sel.interpreter).write_text("#!/bin/sh\nexit 0\n")
    Path(sel.interpreter).chmod(0o755)
    main = tmp_path / "pos_main.wav"
    aux = tmp_path / "pos_aux.wav"
    _write_sine_wav(main, duration=0.5)
    _write_sine_wav(aux, duration=0.5)

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        out = Path(cmd[cmd.index("--output") + 1])
        payload = {
            "schema_version": 1,
            "provider": BBC_PROVIDER,
            "provider_version": BBC_VERSION,
            "native_offset_seconds": "0.5",
            "standard_score": "12.34",
            "analysis": {"frame_offset": 31},
        }
        out.write_text(json.dumps(payload))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("roughcut.adapters.audio_offset_finder.run_bounded_child_stream", _fake_run)
    result = run_bbc_offset_finder(
        main, aux, selection=sel, budget=_budget(), workspace_root=tmp_path
    )
    assert result.native_offset_seconds == "0.5"
    assert Decimal(result.native_offset_seconds) > 0
    assert seconds_to_ticks(result.native_offset_seconds) == 60000


def test_negative_offset_string_is_preserved_via_mocked_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sel = _bbc_selection(interpreter=str(Path(tmp_path / "py_neg").resolve()))
    Path(sel.interpreter).parent.mkdir(parents=True, exist_ok=True)
    Path(sel.interpreter).write_text("#!/bin/sh\nexit 0\n")
    Path(sel.interpreter).chmod(0o755)
    main = tmp_path / "neg_main.wav"
    aux = tmp_path / "neg_aux.wav"
    _write_sine_wav(main, duration=0.5)
    _write_sine_wav(aux, duration=0.5)

    def _fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        out = Path(cmd[cmd.index("--output") + 1])
        payload = {
            "schema_version": 1,
            "provider": BBC_PROVIDER,
            "provider_version": BBC_VERSION,
            "native_offset_seconds": "-0.5",
            "standard_score": "9.87",
            "analysis": {"frame_offset": -31},
        }
        out.write_text(json.dumps(payload))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("roughcut.adapters.audio_offset_finder.run_bounded_child_stream", _fake_run)
    result = run_bbc_offset_finder(
        main, aux, selection=sel, budget=_budget(), workspace_root=tmp_path
    )
    assert result.native_offset_seconds == "-0.5"
    assert Decimal(result.native_offset_seconds) < 0
    assert seconds_to_ticks(result.native_offset_seconds) == -60000


def _write_sine_wav(
    path: Path, duration: float = 2.0, sample_rate: int = 16000, freq: float = 440.0
) -> None:
    n = int(duration * sample_rate)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        for i in range(n):
            sample = int(
                16000
                * 0.5
                * __import__("math").sin(2 * __import__("math").pi * freq * i / sample_rate)
            )
            wf.writeframes(struct.pack("<h", sample))


def _write_shifted_wav(
    src: Path, dst: Path, shift_seconds: float, sample_rate: int = 16000
) -> None:
    # read src, prepend silence for positive shift (aux delayed), trim to same length
    import wave as _wave

    with _wave.open(str(src), "rb") as r:
        frames = r.readframes(r.getnframes())
    # 16-bit mono
    fmt = f"<{len(frames) // 2}h"
    samples = list(struct.unpack(fmt, frames))
    shift_samples = round(shift_seconds * sample_rate)
    if shift_samples >= 0:
        shifted = [0] * shift_samples + samples
        shifted = shifted[: len(samples)]
    else:
        shifted = samples[abs(shift_samples) :]
        shifted = shifted + [0] * (len(samples) - len(shifted))
    with _wave.open(str(dst), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(struct.pack(fmt, *shifted))


def _write_rich_wav(
    path: Path, duration: float = 4.0, sample_rate: int = 16000, seed: int = 0
) -> None:
    import math as _math
    import random as _random

    _random.seed(seed)
    n = int(duration * sample_rate)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        for i in range(n):
            t = i / sample_rate
            # Rich: chirp + AM noise + multi-tone
            chirp = _math.sin(2 * _math.pi * (220 + 180 * t / duration) * t)
            tone2 = 0.5 * _math.sin(2 * _math.pi * 880 * t)
            am = 0.3 * _math.sin(2 * _math.pi * 5 * t) * _random.uniform(-1, 1)
            sample = int(12000 * (chirp + tone2 + am))
            sample = max(-32768, min(32767, sample))
            wf.writeframes(struct.pack("<h", sample))


@pytest.mark.skipif(
    os.environ.get("ROUGHCUT_RUN_REAL_BBC_GATE") != "1",
    reason="set ROUGHCUT_RUN_REAL_BBC_GATE=1 to run the isolated real BBC gate",
)
def test_gate_real_synthetic_rich_waveform_with_isolated_bbc_install(tmp_path: Path) -> None:
    """Gate: isolated BBC managed install + rich waveform positive/negative shifts."""
    import time as _time

    # Isolated managed install using official pinned catalog
    managed = tmp_path / "gate_managed"
    cache = tmp_path / "gate_cache"
    install = tmp_path / "gate_install"
    external_root = tmp_path / "gate_external"
    for p in (managed, cache, install, external_root):
        p.mkdir(parents=True, exist_ok=True)

    # Use the real release catalog; external python/models are faked via helpers
    # to keep runtime/models external and only BBC as managed missing (114 MB)
    import importlib.util as _ilu_gate

    _spec_gate = _ilu_gate.spec_from_file_location(
        "tci_gate", str(Path(__file__).parent.parent / "test_component_installation.py")
    )
    assert _spec_gate is not None and _spec_gate.loader is not None
    _tci_gate = _ilu_gate.module_from_spec(_spec_gate)
    _spec_gate.loader.exec_module(_tci_gate)
    _wep_gate = _tci_gate._write_external_python
    _wlem_gate = _tci_gate._write_legacy_external_models
    _epr_gate = _tci_gate._external_probe_result
    _LEMD_gate = _tci_gate.LEGACY_EXTERNAL_MODEL_DIGESTS
    from unittest.mock import patch as _patch_gate

    import roughcut.adapters.component_environment as _ce_gate
    import roughcut.adapters.component_installation as _ci_gate

    catalog_gate = _ci_gate.load_release_catalog()
    expected_digests_gate = {
        **_LEMD_gate,
        "model_spk": catalog_gate.profile_for("macos", "arm64")
        .models["model_spk"]
        .directory_sha256,
    }

    orig_digest_gate = _ce_gate.component_record_digest

    def _patched_digest_gate(name: str, path: Path) -> str:
        if name in expected_digests_gate:
            return expected_digests_gate[name]
        return orig_digest_gate(name, path)

    external_python_gate = _wep_gate(external_root / "bin/python")
    external_models_gate = _wlem_gate(external_root / "external models")
    ffmpeg_gate_raw = shutil.which("ffmpeg")
    ffprobe_gate_raw = shutil.which("ffprobe")
    assert ffmpeg_gate_raw is not None and ffprobe_gate_raw is not None
    ffmpeg_gate = Path(ffmpeg_gate_raw).resolve()
    ffprobe_gate = Path(ffprobe_gate_raw).resolve()

    t_gate_start = _time.time()
    with (
        _patch_gate.object(_std_urllib_gate, "urlopen", _real_stdlib_urlopen_gate),
        _patch_gate.object(_ci_gate, "probe_external_python", lambda _p: _epr_gate()),
        _patch_gate.object(_ce_gate, "component_record_digest", _patched_digest_gate),
    ):
        plan_gate = _ci_gate.build_install_plan(
            managed,
            cache,
            install_root=install,
            external_manifest_path=external_models_gate,
            external_python=external_python_gate,
            platform="macos",
            architecture="arm64",
            ffmpeg_command=str(ffmpeg_gate),
            ffprobe_command=str(ffprobe_gate),
            verify_components=True,
            include_bbc_audio_offset_finder=True,
        )
        assert plan_gate.payload["missing_managed_groups"] == ["bbc_audio_offset_finder"]
        t_plan_gate = _time.time() - t_gate_start
        applied_gate = _ci_gate.apply_install_plan(
            managed,
            cache,
            approved_plan_hash=plan_gate.plan_hash,
            catalog=catalog_gate,
            install_root=install,
            external_manifest_path=external_models_gate,
            external_python=external_python_gate,
            ffmpeg_command=str(ffmpeg_gate),
            ffprobe_command=str(ffprobe_gate),
            python_executable=Path(sys.executable),
            verify_components=True,
            include_bbc_audio_offset_finder=True,
        )
        assert applied_gate.installed_groups == ("bbc_audio_offset_finder",)
        assert not applied_gate.reused
        t_apply_gate = _time.time() - t_gate_start - t_plan_gate
        # Verify manifest/receipt/binding via formal path
        receipt_gate = json.loads(
            (managed / "bbc_audio_offset_finder/venv-receipt.json").read_text()
        )
        assert receipt_gate["provider"] == "bbc_audio_offset_finder"
        from roughcut.adapters.runtime_binding import load_runtime_binding as _load_gate

        binding_gate = _load_gate(install / "runtime.json")
        sel_gate = binding_gate.alignment_python
        assert sel_gate is not None and sel_gate.provider == "bbc_audio_offset_finder"

        # Rich waveform positive/negative
        main_rich = tmp_path / "rich_main.wav"
        aux_pos_rich = tmp_path / "rich_pos.wav"
        aux_neg_rich = tmp_path / "rich_neg.wav"
        _write_rich_wav(main_rich, duration=4.0, sample_rate=16000, seed=42)
        _write_shifted_wav(main_rich, aux_pos_rich, shift_seconds=0.7, sample_rate=16000)
        _write_shifted_wav(main_rich, aux_neg_rich, shift_seconds=-0.7, sample_rate=16000)

        ws_gate = tmp_path / "ws_gate"
        ws_gate.mkdir()
        budget_pos_gate = _budget()
        t_pos_start = _time.time()
        res_pos = run_bbc_offset_finder(
            main_rich,
            aux_pos_rich,
            selection=sel_gate,
            budget=budget_pos_gate,
            workspace_root=ws_gate,
        )
        t_pos = _time.time() - t_pos_start
        print(
            f"GATE rich pos native={res_pos.native_offset_seconds} score={res_pos.standard_score} time={t_pos:.2f}s"
        )
        assert Decimal(res_pos.native_offset_seconds) < 0
        assert abs(Decimal(res_pos.native_offset_seconds) - Decimal("-0.7")) < Decimal("0.08")
        assert res_pos.offset_ticks < 0
        assert abs(res_pos.offset_ticks - seconds_to_ticks("-0.7")) <= seconds_to_ticks("0.08")

        budget_neg_gate = _budget()
        t_neg_start = _time.time()
        res_neg = run_bbc_offset_finder(
            main_rich,
            aux_neg_rich,
            selection=sel_gate,
            budget=budget_neg_gate,
            workspace_root=ws_gate,
        )
        t_neg = _time.time() - t_neg_start
        print(
            f"GATE rich neg native={res_neg.native_offset_seconds} score={res_neg.standard_score} time={t_neg:.2f}s"
        )
        assert Decimal(res_neg.native_offset_seconds) > 0
        assert abs(Decimal(res_neg.native_offset_seconds) - Decimal("0.7")) < Decimal("0.08")
        assert res_neg.offset_ticks > 0
        assert abs(res_neg.offset_ticks - seconds_to_ticks("0.7")) <= seconds_to_ticks("0.08")

        # Non-identical stereo
        aux_stereo_gate = tmp_path / "rich_stereo.wav"
        tmp_mono_gate = tmp_path / "rich_tmp_mono.wav"
        _write_shifted_wav(main_rich, tmp_mono_gate, shift_seconds=0.4, sample_rate=16000)
        import wave as _wave_gate

        with _wave_gate.open(str(tmp_mono_gate), "rb") as r_gate:
            frames_gate = r_gate.readframes(r_gate.getnframes())
        mono_samples_gate = struct.unpack(f"<{len(frames_gate) // 2}h", frames_gate)
        stereo_frames_gate = b"".join(struct.pack("<hh", s, s) for s in mono_samples_gate)
        with _wave_gate.open(str(aux_stereo_gate), "wb") as w_gate:
            w_gate.setnchannels(2)
            w_gate.setsampwidth(2)
            w_gate.setframerate(16000)
            w_gate.writeframes(stereo_frames_gate)
        budget_stereo_gate = _budget()
        res_stereo = run_bbc_offset_finder(
            main_rich,
            aux_stereo_gate,
            selection=sel_gate,
            budget=budget_stereo_gate,
            workspace_root=ws_gate,
        )
        print(
            f"GATE stereo native={res_stereo.native_offset_seconds} score={res_stereo.standard_score}"
        )
        assert abs(Decimal(res_stereo.native_offset_seconds) - Decimal("-0.4")) < Decimal("0.08")
        assert res_stereo.offset_ticks < 0
        assert abs(res_stereo.offset_ticks - seconds_to_ticks("-0.4")) <= seconds_to_ticks("0.08")

        plan2_gate = _ci_gate.build_install_plan(
            managed,
            cache,
            install_root=install,
            external_manifest_path=external_models_gate,
            external_python=external_python_gate,
            platform="macos",
            architecture="arm64",
            ffmpeg_command=str(ffmpeg_gate),
            ffprobe_command=str(ffprobe_gate),
            verify_components=True,
            include_bbc_audio_offset_finder=True,
        )
        assert plan2_gate.payload["missing_managed_groups"] == []
        assert plan2_gate.payload["bbc_audio_offset_finder"]["available"] is True
        reused_gate = _ci_gate.apply_install_plan(
            managed,
            cache,
            approved_plan_hash=plan2_gate.plan_hash,
            catalog=catalog_gate,
            install_root=install,
            external_manifest_path=external_models_gate,
            external_python=external_python_gate,
            ffmpeg_command=str(ffmpeg_gate),
            ffprobe_command=str(ffprobe_gate),
            python_executable=Path(sys.executable),
            verify_components=True,
            include_bbc_audio_offset_finder=True,
        )
        assert reused_gate.reused is True
        print(
            f"GATE total wall {time.time() - t_gate_start:.1f}s plan {t_plan_gate:.1f}s apply {t_apply_gate:.1f}s reuse {reused_gate.reused}"
        )
        assert list(ws_gate.iterdir()) == []
