from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from roughcut.adapters import component_environment
from roughcut.adapters.component_environment import (
    PROBE_FRAME_PREFIX,
    ComponentError,
    ComponentManifest,
    ComponentRecord,
    ComponentVerification,
    _parse_probe_frame,
    _probe_alignment_group,
    path_is_within,
)
from roughcut.adapters.ffmpeg_environment import (
    FFmpegRuntimeDriftError,
    diagnose_command,
    diagnose_ffmpeg,
    verify_runtime_pair,
)
from roughcut.adapters.funasr.runner import ASR_MODEL, VAD_MODEL
from roughcut.adapters.funasr_environment import diagnose_funasr, diagnose_model_cache
from roughcut.adapters.runtime_binding import (
    audalign_distribution_versions_for,
    audalign_distributions_for,
    bbc_audio_offset_finder_distribution_versions_for,
    bbc_audio_offset_finder_distributions_for,
)


def test_command_diagnostics_distinguish_available_missing_and_not_executable(tmp_path: Path) -> None:
    available = diagnose_command(sys.executable, version_args=("--version",))
    missing = diagnose_command("roughcut-command-that-does-not-exist")
    blocked = tmp_path / "blocked-command"
    blocked.write_text("#!/bin/sh\n", encoding="utf-8")
    blocked.chmod(0o644)

    assert available.status == "available"
    assert available.version is not None
    assert missing.status == "missing"
    expected_blocked_status = "unavailable" if sys.platform == "win32" else "not_executable"
    assert diagnose_command(str(blocked)).status == expected_blocked_status


def test_ffmpeg_and_funasr_diagnostics_report_independent_states(tmp_path: Path) -> None:
    cache_file = tmp_path / "model-cache-file"
    cache_file.write_text("not a directory", encoding="utf-8")
    ffmpeg = diagnose_ffmpeg(
        ffmpeg_command="roughcut-no-ffmpeg", ffprobe_command="roughcut-no-ffprobe"
    )
    funasr = diagnose_funasr(python_command=sys.executable, model_cache=cache_file)

    assert ffmpeg.ffmpeg.status == "missing"
    assert ffmpeg.ffprobe.status == "missing"
    assert funasr.python.status == "available"
    assert funasr.model_cache.status == "not_directory"
    assert diagnose_model_cache(tmp_path / "missing-cache").status == "missing"


def _ffmpeg_pair(tmp_path: Path) -> tuple[Path, Path]:
    ffmpeg = tmp_path / "ffmpeg"
    ffprobe = tmp_path / "ffprobe"
    for command in (ffmpeg, ffprobe):
        command.write_text("fixture", encoding="utf-8")
        command.chmod(0o755)
    return ffmpeg, ffprobe


def _compatibility_runner(
    ffmpeg_version: str,
    ffprobe_version: str,
    *,
    filter_support: bool = True,
    libx264: bool = True,
    aac: bool = True,
    smoke_failure: bool = False,
    probe_stdout: str | None = None,
    help_output: str | None = None,
    help_returncode: int = 0,
):
    calls: list[list[str]] = []

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1:] == ["-version"]:
            version = ffmpeg_version if Path(command[0]).name == "ffmpeg" else ffprobe_version
            return subprocess.CompletedProcess(command, 0, stdout=version + "\n", stderr="")
        if command[1:] == ["-hide_banner", "-h", "full"]:
            if help_output is not None:
                output = help_output
            elif filter_support:
                output = "-filter_complex <graph_description>\ndeprecated, use -/filter_complex instead"
            else:
                output = "no file option"
            return subprocess.CompletedProcess(command, help_returncode, stdout=output, stderr="")
        if command[1:] == ["-hide_banner", "-encoders"]:
            output = ""
            if libx264:
                output += " V....D libx264 fixture\n"
            if aac:
                output += " A....D aac fixture\n"
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")
        if Path(command[0]).name == "ffmpeg":
            if not smoke_failure:
                Path(command[-1]).write_bytes(b"mp4")
            return subprocess.CompletedProcess(command, 1 if smoke_failure else 0, stdout="", stderr="")
        payload = {
            "streams": [
                {"codec_type": "video", "codec_name": "h264"},
                {"codec_type": "audio", "codec_name": "aac"},
            ],
            "format": {"duration": "0.200000"},
            "programs": [],
            "stream_groups": [],
        }
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(payload) if probe_stdout is None else probe_stdout,
            stderr="",
        )

    return run, calls


@pytest.mark.parametrize(
    ("version", "help_output"),
    [
        (
            "8.1.1",
            "-filter_complex <graph_description>\ndeprecated, use -/filter_complex instead",
        ),
        ("9.0", "-filter_complex <graph_description>"),
    ],
    ids=["8.1.1-deprecated-alias", "9.0-base-option-only"],
)
def test_ffmpeg_quick_help_oracle_accepts_independent_base_option(
    tmp_path: Path, version: str, help_output: str
) -> None:
    ffmpeg, ffprobe = _ffmpeg_pair(tmp_path)
    runner, _calls = _compatibility_runner(
        f"ffmpeg version {version}-vendor",
        f"ffprobe version {version}-vendor",
        help_output=help_output,
    )

    result = diagnose_ffmpeg(
        ffmpeg_command=str(ffmpeg),
        ffprobe_command=str(ffprobe),
        command_runner=runner,
    )

    assert result.ffmpeg.status == result.ffprobe.status == "available"


@pytest.mark.parametrize(
    "help_output",
    [
        "-filter_complex_threads <count>",
        "deprecated, use -/filter_complex instead",
        "-filter_complex_script <filename>",
        "other option only",
    ],
    ids=["threads-only", "deprecated-only", "script-only", "base-option-missing"],
)
def test_ffmpeg_quick_help_oracle_rejects_non_base_options(
    tmp_path: Path, help_output: str
) -> None:
    ffmpeg, ffprobe = _ffmpeg_pair(tmp_path)
    runner, _calls = _compatibility_runner(
        "ffmpeg version 8.1.1",
        "ffprobe version 8.1.1",
        help_output=help_output,
    )

    result = diagnose_ffmpeg(
        ffmpeg_command=str(ffmpeg),
        ffprobe_command=str(ffprobe),
        command_runner=runner,
    )

    assert result.ffmpeg.status == result.ffprobe.status == "unavailable"
    assert result.ffmpeg.detail == "FFmpeg lacks -/filter_complex support"


def test_ffmpeg_quick_help_oracle_rejects_nonzero_help_command(tmp_path: Path) -> None:
    ffmpeg, ffprobe = _ffmpeg_pair(tmp_path)
    runner, _calls = _compatibility_runner(
        "ffmpeg version 8.1.1",
        "ffprobe version 8.1.1",
        help_output="-filter_complex <graph_description>",
        help_returncode=1,
    )

    result = diagnose_ffmpeg(
        ffmpeg_command=str(ffmpeg),
        ffprobe_command=str(ffprobe),
        command_runner=runner,
    )

    assert result.ffmpeg.status == result.ffprobe.status == "unavailable"
    assert result.ffmpeg.detail == "FFmpeg lacks -/filter_complex support"


@pytest.mark.parametrize("version", ["8.1.1", "9.0"])
def test_ffmpeg_quick_accepts_frozen_version_window_and_matching_pair(
    tmp_path: Path, version: str
) -> None:
    ffmpeg, ffprobe = _ffmpeg_pair(tmp_path)
    runner, _calls = _compatibility_runner(
        f"ffmpeg version {version}-vendor",
        f"ffprobe version {version}-other-vendor",
    )

    result = diagnose_ffmpeg(
        ffmpeg_command=str(ffmpeg),
        ffprobe_command=str(ffprobe),
        command_runner=runner,
    )

    assert result.ffmpeg.status == result.ffprobe.status == "available"


@pytest.mark.parametrize(
    ("ffmpeg_version", "ffprobe_version", "detail"),
    [
        ("8.1.1", "9.0", "differ"),
        ("8.0.9", "8.0.9", "outside >=8.1.0,<10.0.0"),
        ("10.0", "10.0", "outside >=8.1.0,<10.0.0"),
    ],
)
def test_ffmpeg_quick_rejects_pair_mismatch_and_out_of_window_versions(
    tmp_path: Path,
    ffmpeg_version: str,
    ffprobe_version: str,
    detail: str,
) -> None:
    ffmpeg, ffprobe = _ffmpeg_pair(tmp_path)
    runner, _calls = _compatibility_runner(
        f"ffmpeg version {ffmpeg_version}", f"ffprobe version {ffprobe_version}"
    )

    result = diagnose_ffmpeg(
        ffmpeg_command=str(ffmpeg),
        ffprobe_command=str(ffprobe),
        command_runner=runner,
    )

    assert result.ffmpeg.status == result.ffprobe.status == "unavailable"
    assert detail in str(result.ffmpeg.detail)


@pytest.mark.parametrize(
    ("filter_support", "libx264", "aac", "detail"),
    [
        (False, True, True, "-/filter_complex"),
        (True, False, True, "libx264"),
        (True, True, False, "AAC"),
    ],
)
def test_ffmpeg_quick_rejects_missing_static_capability(
    tmp_path: Path,
    filter_support: bool,
    libx264: bool,
    aac: bool,
    detail: str,
) -> None:
    ffmpeg, ffprobe = _ffmpeg_pair(tmp_path)
    runner, _calls = _compatibility_runner(
        "ffmpeg version 8.1.1", "ffprobe version 8.1.1",
        filter_support=filter_support, libx264=libx264, aac=aac,
    )

    result = diagnose_ffmpeg(
        ffmpeg_command=str(ffmpeg), ffprobe_command=str(ffprobe), command_runner=runner
    )

    assert result.ffmpeg.status == "unavailable"
    assert detail in str(result.ffmpeg.detail)


def test_ffmpeg_full_smoke_succeeds_and_failure_is_unavailable(tmp_path: Path) -> None:
    ffmpeg, ffprobe = _ffmpeg_pair(tmp_path)
    successful, successful_calls = _compatibility_runner(
        "ffmpeg version 8.1.1", "ffprobe version 8.1.1"
    )
    failed, _failed_calls = _compatibility_runner(
        "ffmpeg version 8.1.1", "ffprobe version 8.1.1", smoke_failure=True
    )

    available = diagnose_ffmpeg(
        ffmpeg_command=str(ffmpeg), ffprobe_command=str(ffprobe), full=True, command_runner=successful
    )
    unavailable = diagnose_ffmpeg(
        ffmpeg_command=str(ffmpeg), ffprobe_command=str(ffprobe), full=True, command_runner=failed
    )

    assert available.ffmpeg.status == "available"
    smoke_calls = [call for call in successful_calls if "-/filter_complex" in call]
    assert len(smoke_calls) == 1
    filter_index = smoke_calls[0].index("-/filter_complex")
    assert smoke_calls[0][filter_index + 1].endswith("filter.txt")
    assert smoke_calls[0][filter_index : filter_index + 2] == [
        "-/filter_complex",
        smoke_calls[0][filter_index + 1],
    ]
    assert unavailable.ffmpeg.status == "unavailable"
    assert unavailable.ffmpeg.detail == "FFmpeg full smoke encode failed"


@pytest.mark.parametrize(
    "probe_stdout",
    [
        "[]",
        '{"streams":[],"streams":[],"format":{"duration":"0.2"},"programs":[],"stream_groups":[]}',
        '{"streams":{},"format":{"duration":"0.2"},"programs":[],"stream_groups":[]}',
        '{"streams":["bad",{"codec_type":"audio","codec_name":"aac"}],"format":{"duration":"0.2"},"programs":[],"stream_groups":[]}',
        '{"streams":[{"codec_type":"video"},{"codec_type":"audio","codec_name":"aac"}],"format":{"duration":"0.2"},"programs":[],"stream_groups":[]}',
        '{"streams":[{"codec_type":"video","codec_name":"h264","extra":1},{"codec_type":"audio","codec_name":"aac"}],"format":{"duration":"0.2"},"programs":[],"stream_groups":[]}',
        '{"streams":[{"codec_type":"video","codec_name":"h264"},{"codec_type":"audio","codec_name":"aac"}],"format":[],"programs":[],"stream_groups":[]}',
        '{"streams":[{"codec_type":"video","codec_name":"h264"},{"codec_type":"audio","codec_name":"aac"}],"format":{"duration":0.2},"programs":[],"stream_groups":[]}',
        '{"streams":[{"codec_type":"video","codec_name":"h264"},{"codec_type":"audio","codec_name":"aac"}],"format":{"duration":"0.2","extra":1},"programs":[],"stream_groups":[]}',
        '{"streams":[{"codec_type":"video","codec_name":"h264"},{"codec_type":"audio","codec_name":"aac"}],"programs":[],"stream_groups":[]}',
        '{"streams":[{"codec_type":"video","codec_name":"h264"},{"codec_type":"audio","codec_name":"aac"}],"format":{"duration":"0.2"},"programs":[],"stream_groups":[],"extra":1}',
        '{"streams":[{"codec_type":"video","codec_name":"h264"},{"codec_type":"audio","codec_name":"aac"}],"format":{"duration":"0.2"},"programs":{},"stream_groups":[]}',
    ],
)
def test_ffmpeg_full_smoke_rejects_non_closed_probe_json(
    tmp_path: Path, probe_stdout: str
) -> None:
    ffmpeg, ffprobe = _ffmpeg_pair(tmp_path)
    runner, _calls = _compatibility_runner(
        "ffmpeg version 8.1.1",
        "ffprobe version 8.1.1",
        probe_stdout=probe_stdout,
    )

    result = diagnose_ffmpeg(
        ffmpeg_command=str(ffmpeg),
        ffprobe_command=str(ffprobe),
        full=True,
        command_runner=runner,
    )

    assert result.ffmpeg.status == result.ffprobe.status == "unavailable"
    assert result.ffmpeg.detail == "FFmpeg full smoke output failed verification"
    assert str(tmp_path) not in result.ffmpeg.detail


@pytest.mark.parametrize("failure", ["missing", "not_executable"])
def test_ffmpeg_partial_pair_preserves_failure_and_companion_is_not_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    ffmpeg = tmp_path / "ffmpeg"
    if failure == "not_executable":
        ffmpeg.write_text("fixture", encoding="utf-8")
        ffmpeg.chmod(0o644)
    ffprobe = tmp_path / "ffprobe"
    ffprobe.write_text("fixture", encoding="utf-8")
    ffprobe.chmod(0o755)
    if failure == "not_executable":
        real_access = os.access
        monkeypatch.setattr(
            "roughcut.adapters.ffmpeg_environment.os.access",
            lambda path, mode: False if Path(path) == ffmpeg else real_access(path, mode),
        )
    calls: list[list[str]] = []

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        raise AssertionError("partial pair started a child")

    result = diagnose_ffmpeg(
        ffmpeg_command=str(ffmpeg),
        ffprobe_command=str(ffprobe),
        full=True,
        command_runner=runner,
    )

    assert result.ffmpeg.status == failure
    assert result.ffprobe.status == "unavailable"
    assert "available" not in {result.ffmpeg.status, result.ffprobe.status}
    assert calls == []


def test_ffmpeg_full_cleanup_failure_is_unavailable_without_temp_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ffmpeg, ffprobe = _ffmpeg_pair(tmp_path)
    runner, _calls = _compatibility_runner(
        "ffmpeg version 8.1.1", "ffprobe version 8.1.1"
    )

    class CleanupFailure:
        def __enter__(self) -> str:
            self.path = tmp_path / "random-smoke-path"
            self.path.mkdir()
            return str(self.path)

        def __exit__(self, *_args: object) -> None:
            raise OSError("random-smoke-path cleanup failed")

    monkeypatch.setattr(
        "roughcut.adapters.ffmpeg_environment.tempfile.TemporaryDirectory",
        lambda **_kwargs: CleanupFailure(),
    )
    result = diagnose_ffmpeg(
        ffmpeg_command=str(ffmpeg), ffprobe_command=str(ffprobe), full=True, command_runner=runner
    )

    assert result.ffmpeg.status == "unavailable"
    assert result.ffmpeg.detail == "FFmpeg full smoke or cleanup failed"
    assert "random-smoke-path" not in result.ffmpeg.detail


def test_runtime_pair_drift_check_calls_each_version_once_and_compares_exact_text() -> None:
    calls: list[list[str]] = []

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        name = Path(command[0]).name
        return subprocess.CompletedProcess(
            command, 0, stdout=f"{name} version 8.1.1-vendor\n", stderr=""
        )

    assert verify_runtime_pair(
        ffmpeg_command="/fixture/ffmpeg",
        ffmpeg_version="ffmpeg version 8.1.1-vendor",
        ffprobe_command="/fixture/ffprobe",
        ffprobe_version="ffprobe version 8.1.1-vendor",
        command_runner=runner,
    ) == (
        "ffmpeg version 8.1.1-vendor",
        "ffprobe version 8.1.1-vendor",
    )
    assert calls == [["/fixture/ffmpeg", "-version"], ["/fixture/ffprobe", "-version"]]

    with pytest.raises(FFmpegRuntimeDriftError):
        verify_runtime_pair(
            ffmpeg_command="/fixture/ffmpeg",
            ffmpeg_version="ffmpeg version 8.1.1-other",
            ffprobe_command="/fixture/ffprobe",
            ffprobe_version="ffprobe version 8.1.1-vendor",
            command_runner=runner,
        )


@pytest.mark.parametrize(
    "first_failure",
    [
        subprocess.CompletedProcess(["ffmpeg", "-version"], 1, stdout="", stderr="failed"),
        OSError("fixture unavailable"),
        subprocess.TimeoutExpired(["ffmpeg", "-version"], 10),
    ],
)
def test_runtime_pair_safe_first_failure_still_attempts_each_child_once(
    first_failure: subprocess.CompletedProcess[str] | OSError | subprocess.TimeoutExpired,
) -> None:
    calls: list[list[str]] = []

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if len(calls) == 1:
            if isinstance(first_failure, BaseException):
                raise first_failure
            return first_failure
        return subprocess.CompletedProcess(
            command, 0, stdout="ffprobe version 8.1.1-vendor\n", stderr=""
        )

    with pytest.raises(FFmpegRuntimeDriftError):
        verify_runtime_pair(
            ffmpeg_command="/fixture/ffmpeg",
            ffmpeg_version="ffmpeg version 8.1.1-vendor",
            ffprobe_command="/fixture/ffprobe",
            ffprobe_version="ffprobe version 8.1.1-vendor",
            command_runner=runner,
        )

    assert calls == [["/fixture/ffmpeg", "-version"], ["/fixture/ffprobe", "-version"]]


def test_runtime_pair_unknown_first_failure_propagates_without_starting_second_child() -> None:
    calls: list[list[str]] = []

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        raise RuntimeError("fixture unsafe child-control failure")

    with pytest.raises(RuntimeError, match="unsafe child-control failure"):
        verify_runtime_pair(
            ffmpeg_command="/fixture/ffmpeg",
            ffmpeg_version="ffmpeg version 8.1.1-vendor",
            ffprobe_command="/fixture/ffprobe",
            ffprobe_version="ffprobe version 8.1.1-vendor",
            command_runner=runner,
        )

    assert calls == [["/fixture/ffmpeg", "-version"]]


def test_funasr_diagnostics_report_each_controlled_model_directory(tmp_path: Path) -> None:
    model_root = tmp_path / "models"
    for relative_path in (ASR_MODEL, VAD_MODEL):
        (model_root / relative_path).mkdir(parents=True)

    funasr = diagnose_funasr(python_command=sys.executable, model_cache=model_root)

    assert funasr.model_cache.status == "available"
    assert funasr.models["asr"].status == "available"
    assert funasr.models["vad"].status == "available"
    assert funasr.models["punc"].status == "missing"
    assert funasr.models["speaker"].status == "missing"


def test_diagnostics_cli_returns_versioned_json() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "roughcut.cli", "diagnostics", "--json"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    payload = json.loads(result.stdout)
    assert result.returncode == 0
    assert payload["ok"] is True
    assert "ffmpeg" in payload
    assert "funasr" in payload
    assert payload["launchers"]["cli_status"] in {"available", "missing"}


def test_component_path_containment_supports_posix_spaces_and_chinese() -> None:
    root = "/Users/示例 用户/Library/Application Support/Roughcut"

    assert path_is_within(
        "/Users/示例 用户/Library/Application Support/Roughcut/models/中文 模型",
        root,
        platform="posix",
    )
    assert not path_is_within(
        "/Users/示例 用户/Library/Application Support/Roughcut/../Other/model",
        root,
        platform="posix",
    )


def test_component_path_containment_supports_windows_drives_and_rejects_escape() -> None:
    root = r"C:\Users\示例 用户\AppData\Local\Roughcut Managed"

    assert path_is_within(
        r"C:\Users\示例 用户\AppData\Local\Roughcut Managed\bin\ffmpeg.exe",
        root,
        platform="windows",
    )
    assert not path_is_within(
        r"C:\Users\示例 用户\AppData\Local\Roughcut Managed\..\Other\ffmpeg.exe",
        root,
        platform="windows",
    )
    assert not path_is_within(
        r"D:\Roughcut Managed\bin\ffmpeg.exe",
        root,
        platform="windows",
    )


def test_probe_frame_ignores_arbitrary_outer_bytes_but_parses_one_closed_frame() -> None:
    frame = PROBE_FRAME_PREFIX + b'{"value":"ok"}\n'
    assert _parse_probe_frame(b"\xff notice\n" + frame + b"\x80 tail", frozenset({"value"})) == {
        "value": "ok"
    }


@pytest.mark.parametrize(
    "stdout",
    [
        b"",
        PROBE_FRAME_PREFIX + b'{"value":"ok"}\n' + PROBE_FRAME_PREFIX + b'{"value":"ok"}\n',
        PROBE_FRAME_PREFIX + b"\xff\n",
        PROBE_FRAME_PREFIX + b'{"value":"ok"}\r\n',
        PROBE_FRAME_PREFIX + b'{"value":"ok","extra":1}\n',
        PROBE_FRAME_PREFIX + b'{"missing":1}\n',
    ],
)
def test_probe_frame_rejects_zero_multiple_corrupt_and_non_closed_frames(
    stdout: bytes,
) -> None:
    with pytest.raises(ComponentError):
        _parse_probe_frame(stdout, frozenset({"value"}))


@pytest.mark.parametrize(
    "outer",
    [
        b"\xc2\x8f notice from a third-party tool\n",
        b"\x9d\x8c\x9d notice\n",
        b"\xba\xbb\xcc\xdd notice\n",
        b"\xff\xfe\x00\x00 notice\n",
    ],
    ids=["cp1252-like", "cp1252-symbols", "gbk-like", "utf16-le-encoded-nonsense"],
)
def test_probe_frame_outer_bytes_are_locale_independent(outer: bytes) -> None:
    """Third-party stdout not in the parent locale never corrupts the frame.

    The parent captures raw bytes and only UTF-8 decodes the JSON suffix after
    the exact byte prefix; Windows cp1252/cp936-style surrounding bytes must not
    affect framing or decoding.
    """
    frame = PROBE_FRAME_PREFIX + b'{"value":"ok"}\n'
    assert _parse_probe_frame(outer + frame + b"\x80\x81 tail", frozenset({"value"})) == {
        "value": "ok"
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("funasr", 123),
        ("torch", ["2.6.0"]),
        ("python_version", {"3": 11}),
        ("cuda_available", "false"),
    ],
    ids=["funasr-int", "torch-list", "python-version-dict", "cuda-available-string"],
)
def test_probe_frame_wrong_field_types_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    """Payload fields with the wrong JSON type fail closed."""
    from roughcut.adapters.component_environment import probe_python_runtime

    interpreter = tmp_path / "interpreter"
    interpreter.write_text("fixture", encoding="utf-8")
    interpreter.chmod(0o755)

    def return_frame(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        payload = {
            "python_version": "3.11",
            "funasr": "1.3.14",
            "torch": "2.6.0+cpu",
            "torchaudio": "2.6.0+cpu",
            "cuda_version": None,
            "cuda_available": False,
        }
        payload[field] = value
        return subprocess.CompletedProcess(
            command,
            0,
            PROBE_FRAME_PREFIX
            + json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
            + b"\n",
            b"",
        )

    monkeypatch.setattr(component_environment.subprocess, "run", return_frame)
    with pytest.raises(ComponentError):
        probe_python_runtime(interpreter)


def test_probe_child_nonzero_precedes_a_valid_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid frame does not mask a non-zero child exit."""
    import roughcut.adapters.component_environment as environment_module
    from roughcut.adapters.component_environment import probe_python_runtime

    interpreter = tmp_path / "interpreter"
    interpreter.write_text("fixture", encoding="utf-8")
    interpreter.chmod(0o755)

    def return_with_exit(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        payload = {
            "python_version": "3.11",
            "funasr": "1.3.14",
            "torch": "2.6.0+cpu",
            "torchaudio": "2.6.0+cpu",
            "cuda_version": None,
            "cuda_available": False,
        }
        return subprocess.CompletedProcess(
            command,
            23,
            PROBE_FRAME_PREFIX
            + json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
            + b"\n",
            b"",
        )

    monkeypatch.setattr(environment_module.subprocess, "run", return_with_exit)
    with pytest.raises(ComponentError, match="non-zero exit"):
        probe_python_runtime(interpreter)


@pytest.mark.parametrize("platform", ["macos", "windows"])
def test_live_audalign_probe_uses_the_platform_specific_closed_distribution_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
) -> None:
    root = tmp_path / "managed"
    venv = root / "audalign" / "venv"
    interpreter = (
        venv / "Scripts" / "python.exe"
        if platform == "windows"
        else venv / "bin" / "python"
    )
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("fixture", encoding="utf-8")
    interpreter.chmod(0o755)
    receipt = root / "audalign" / "venv-receipt.json"
    receipt.write_text("{}\n", encoding="utf-8")
    record = ComponentRecord(
        name="audalign",
        kind="python_package",
        source_type="managed",
        origin="fixture://audalign",
        version="1.3.1",
        path="audalign/venv-receipt.json",
        platform=platform,
        architecture="x86_64" if platform == "windows" else "arm64",
        license="MIT",
        verification=ComponentVerification(
            "sha256", hashlib.sha256(receipt.read_bytes()).hexdigest()
        ),
    )
    manifest = ComponentManifest(
        components=(record,),
        platform=platform,
        architecture=record.architecture,
        managed_root=(r"C:\Roughcut Managed" if platform == "windows" else str(root)),
    )
    expected = [
        [name, audalign_distribution_versions_for(platform)[name]]
        for name in audalign_distributions_for(platform)
    ]
    monkeypatch.setattr(
        "roughcut.adapters.component_environment.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=(
                b"ROUGHCUT-PROBE/1 "
                + json.dumps({"python_version": "3.11", "distributions": expected}).encode()
                + b"\n"
            ),
        ),
    )

    assert _probe_alignment_group(
        record,
        manifest,
        managed_root_override=root,
        managed_dir="audalign",
        provider="audalign",
    ) is None
    if platform == "windows":
        macos_only = [[name, version] for name, version in expected if name != "colorama"]
        monkeypatch.setattr(
            "roughcut.adapters.component_environment.subprocess.run",
            lambda *args, **kwargs: SimpleNamespace(
                returncode=0,
                stdout=(
                    b"ROUGHCUT-PROBE/1 "
                    + json.dumps(
                        {"python_version": "3.11", "distributions": macos_only}
                    ).encode()
                    + b"\n"
                ),
            ),
        )
        assert (
            _probe_alignment_group(record, manifest, managed_root_override=root, managed_dir="audalign", provider="audalign")
            == "audalign distribution closure differs"
        )
    else:
        windows = [*expected, ["colorama", "0.4.6"]]
        monkeypatch.setattr(
            "roughcut.adapters.component_environment.subprocess.run",
            lambda *args, **kwargs: SimpleNamespace(
                returncode=0,
                stdout=(
                    b"ROUGHCUT-PROBE/1 "
                    + json.dumps(
                        {"python_version": "3.11", "distributions": windows}
                    ).encode()
                    + b"\n"
                ),
            ),
        )
        assert (
            _probe_alignment_group(record, manifest, managed_root_override=root, managed_dir="audalign", provider="audalign")
            == "audalign distribution closure differs"
        )


@pytest.mark.parametrize("bundled_library", [True, False])
def test_live_bbc_probe_requires_imports_and_bundled_libsndfile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bundled_library: bool,
) -> None:
    root = tmp_path / "managed"
    venv = root / "bbc_audio_offset_finder" / "venv"
    interpreter = venv / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("fixture", encoding="utf-8")
    interpreter.chmod(0o755)
    receipt = root / "bbc_audio_offset_finder" / "venv-receipt.json"
    receipt.write_text("{}\n", encoding="utf-8")
    record = ComponentRecord(
        name="bbc_audio_offset_finder",
        kind="python_package",
        source_type="managed",
        origin="fixture://bbc",
        version="0.5.5",
        path="bbc_audio_offset_finder/venv-receipt.json",
        platform="macos",
        architecture="arm64",
        license="Apache-2.0",
        verification=ComponentVerification(
            "sha256", hashlib.sha256(receipt.read_bytes()).hexdigest()
        ),
    )
    manifest = ComponentManifest(
        components=(record,),
        platform="macos",
        architecture="arm64",
        managed_root=str(root),
    )
    expected = [
        [name, bbc_audio_offset_finder_distribution_versions_for("macos")[name]]
        for name in bbc_audio_offset_finder_distributions_for("macos")
    ]
    monkeypatch.setattr(
        "roughcut.adapters.component_environment.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=(
                b"ROUGHCUT-PROBE/1 "
                + json.dumps(
                    {
                        "python_version": "3.11",
                        "distributions": expected,
                        "audio_offset_finder_imported": True,
                        "soundfile_imported": True,
                        "soundfile_bundled_library": bundled_library,
                    }
                ).encode()
                + b"\n"
            ),
        ),
    )

    reason = _probe_alignment_group(
        record,
        manifest,
        managed_root_override=root,
        managed_dir="bbc_audio_offset_finder",
        provider="bbc_audio_offset_finder",
    )
    assert reason is None if bundled_library else reason == (
        "bbc_audio_offset_finder bundled libsndfile is unavailable"
    )
