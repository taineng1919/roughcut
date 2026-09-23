from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import wave
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from roughcut.adapters.audalign import (
    AUDALIGN_WORKER_MAX_OUTPUT_BYTES,
    AudalignAdapterError,
    AudalignCandidateLimitError,
    AudalignMemoryBudgetError,
    AudalignOutputOverflowError,
    AudalignTimeBudgetError,
)
from roughcut.application import alignments
from roughcut.application.alignment_profile import (
    PROFILE2_CANONICAL,
    AlignmentProfileError,
    CandidateOffset,
    FixedOffsetVerifier,
    group_profile2_hypotheses,
    profile2_call_plan,
    profile2_probe_starts,
    source_relation_b,
)
from roughcut.application.sources import fingerprint_file
from roughcut.domain.alignment import (
    ALIGNMENT_VERIFICATION_WINDOW_TICKS,
    AlignmentAlgorithm,
    AlignmentCamera,
    AlignmentCameraGroup,
    AlignmentError,
    AlignmentInterval,
    AlignmentSourceBasis,
    AlignmentSourceFingerprint,
    AlignmentSummary,
    AlignmentVerificationProfile,
    MulticamAlignmentArtifact,
    seconds_to_ticks,
)
from roughcut.domain.project import ImportMode
from roughcut.domain.render import ToolResolution
from roughcut.domain.workflow import canonical_sha256_v1


class _Deadline:
    def remaining(self) -> float:
        return 120.0


class _MarkerArmedDeadline:
    def __init__(
        self,
        marker: Path,
        timeout_seconds: float,
        *,
        startup_seconds: float = 5.0,
    ) -> None:
        now = time.monotonic()
        self._marker = marker
        self._timeout_seconds = timeout_seconds
        self._startup_expires_at = now + startup_seconds
        self._timeout_expires_at: float | None = None

    def remaining(self) -> float:
        now = time.monotonic()
        if self._timeout_expires_at is None:
            if self._marker.is_file():
                self._timeout_expires_at = now + self._timeout_seconds
            elif now >= self._startup_expires_at:
                raise AssertionError("child did not publish readiness marker")
            else:
                return self._startup_expires_at - now
        return self._timeout_expires_at - now


def _process_is_running(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    import ctypes
    from ctypes import wintypes

    synchronize = 0x00100000
    process_query_limited_information = 0x1000
    wait_object_0 = 0x00000000
    wait_timeout = 0x00000102
    error_invalid_parameter = 87
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.OpenProcess.argtypes = (
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    )
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
    )
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(
        synchronize | process_query_limited_information,
        False,
        pid,
    )
    if not handle:
        error = ctypes.get_last_error()
        if error == error_invalid_parameter:
            return False
        raise ctypes.WinError(error)  # type: ignore[attr-defined]
    state = kernel32.WaitForSingleObject(handle, 0)
    wait_error = ctypes.get_last_error()
    if not kernel32.CloseHandle(handle):
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
    if state == wait_object_0:
        return False
    if state == wait_timeout:
        return True
    raise ctypes.WinError(wait_error)  # type: ignore[attr-defined]


def _write_workspace_wav(path: Path, *, seconds: int = 15) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(44_100)
        output.writeframes(b"\0" * (44_100 * 2 * seconds))


class _RecordingWorkspaceBudget:
    def __init__(self, events: list[str], reservations: list[int]) -> None:
        self.events = events
        self.reservations = reservations
        self.output_reservations: list[int] = []

    def reserve_decode(self, *, duration_ticks: int) -> None:
        self.reservations.append(duration_ticks)
        self.events.append("reserve")

    def reserve_bytes(self, *, size_bytes: int) -> None:
        self.output_reservations.append(size_bytes)
        self.events.append("reserve-output")

    def recheck(self) -> None:
        self.events.append("recheck")


def _run_recorded_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[str], list[int], list[int]]:
    import roughcut.adapters.audalign.ffmpeg_audio as ffmpeg_audio_module
    from roughcut.application import alignment_profile as profile_module

    events: list[str] = []
    reservations: list[int] = []
    budget = _RecordingWorkspaceBudget(events, reservations)
    workspace = tmp_path / "verification-workspace"
    workspace.mkdir()

    def fake_extract(_source, output, **_kwargs):
        events.append("extract")
        Path(output).write_bytes(b"window")

    def fake_recognize(*_args, **kwargs):
        events.append("recognize")
        Path(kwargs.get("output_path", _args[3])).write_text(
            "{}", encoding="utf-8"
        )
        return SimpleNamespace(
            candidates=(SimpleNamespace(offset_ticks=0),),
        )

    monkeypatch.setattr(
        ffmpeg_audio_module, "extract_wav_window", fake_extract
    )
    monkeypatch.setattr(profile_module, "run_audalign_recognize", fake_recognize)
    verifier = FixedOffsetVerifier(
        alignment_python=tmp_path / "alignment-python",
        ffmpeg_command="ffmpeg",
        workspace=workspace,
        deadline=_Deadline(),
        workspace_budget=budget,
    )
    result = verifier.verify(
        tmp_path / "main.wav",
        tmp_path / "aux.wav",
        candidate_b_ticks=0,
        main_overlap_start_ticks=0,
        main_overlap_end_ticks=3 * ALIGNMENT_VERIFICATION_WINDOW_TICKS,
        auxiliary_overlap_start_ticks=0,
        auxiliary_overlap_end_ticks=3 * ALIGNMENT_VERIFICATION_WINDOW_TICKS,
    )
    assert result["passed"] is True
    return events, reservations, budget.output_reservations


def test_audalign_worker_output_and_tmpdir_stay_in_operation_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from roughcut.adapters import audalign as audalign_adapter
    from roughcut.adapters.child_budget import ChildBudget

    operation_workspace = tmp_path / "operation-workspace"
    output = operation_workspace / "pair-main-aux" / "recognize.json"
    observed: dict[str, str] = {}

    def apply_tmpdir(environment: dict[str, str]) -> dict[str, str]:
        controlled = dict(environment)
        controlled["TMPDIR"] = str(operation_workspace)
        return controlled

    budget = ChildBudget(
        _Deadline(),
        4_294_967_296,
        apply_tmpdir=apply_tmpdir,
    )

    def fake_run(command, *, budget, env, **_kwargs):
        controlled = budget.apply_tmpdir(env)
        observed["tmpdir"] = controlled["TMPDIR"]
        output_argument = Path(command[command.index("--json-output") + 1])
        observed["output"] = str(output_argument)
        output_argument.parent.mkdir(parents=True, exist_ok=True)
        output_argument.write_text('{"match_info": null}', encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(audalign_adapter, "run_bounded_child", fake_run)
    result = audalign_adapter.run_audalign_recognize(
        Path(sys.executable),
        tmp_path / "target.wav",
        tmp_path / "against.wav",
        output,
        budget=budget,
    )

    assert result.candidates == ()
    assert observed == {
        "tmpdir": str(operation_workspace),
        "output": str(output),
    }
    assert not output.exists()


def test_audalign_worker_candidate_overflow_is_explicit_and_not_truncated() -> None:
    from roughcut.adapters.audalign.worker import (
        WORKER_STATUS_CANDIDATE_OVERFLOW,
        WORKER_STATUS_KEY,
        _bounded_output_bytes,
    )

    encoded = _bounded_output_bytes(
        {
            "match_info": {
                "offset_seconds": list(range(513)),
                "confidence": [1] * 513,
                "locality_seconds": [None] * 513,
            }
        },
        max_output_bytes=256,
        max_raw_candidates=512,
    )

    payload = json.loads(encoded)
    assert len(encoded) <= 256
    assert payload == {
        WORKER_STATUS_KEY: WORKER_STATUS_CANDIDATE_OVERFLOW,
        "candidate_count": 513,
        "candidate_limit": 512,
    }


def test_audalign_worker_output_overflow_is_explicit_and_bounded() -> None:
    from roughcut.adapters.audalign.worker import (
        WORKER_STATUS_KEY,
        WORKER_STATUS_OUTPUT_OVERFLOW,
        _bounded_output_bytes,
    )

    encoded = _bounded_output_bytes(
        {
            "match_info": {
                "offset_seconds": [0],
                "confidence": [1],
                "locality_seconds": ["x" * 2_048],
            }
        },
        max_output_bytes=128,
    )

    payload = json.loads(encoded)
    assert len(encoded) <= 128
    assert payload[WORKER_STATUS_KEY] == WORKER_STATUS_OUTPUT_OVERFLOW
    assert "match_info" not in payload


def test_audalign_adapter_rejects_candidate_overflow_marker(
    tmp_path: Path,
) -> None:
    from roughcut.adapters import audalign as audalign_adapter
    from roughcut.adapters.audalign.worker import _bounded_output_bytes

    output = tmp_path / "overflow.json"

    def fake_run(command, **_kwargs):
        output.write_bytes(
            _bounded_output_bytes(
                {
                    "match_info": {
                        "offset_seconds": list(range(513)),
                        "confidence": [1] * 513,
                    }
                },
                max_output_bytes=256,
                max_raw_candidates=512,
            )
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    with pytest.raises(AudalignCandidateLimitError):
        audalign_adapter.run_audalign_recognize(
            Path(sys.executable),
            tmp_path / "target.wav",
            tmp_path / "against.wav",
            output,
            process_runner=fake_run,
            max_raw_candidates=512,
            max_output_bytes=256,
        )
    assert not output.exists()


def test_audalign_adapter_rejects_output_overflow_marker(
    tmp_path: Path,
) -> None:
    from roughcut.adapters import audalign as audalign_adapter
    from roughcut.adapters.audalign.worker import _bounded_output_bytes

    output = tmp_path / "output-overflow.json"

    def fake_run(command, **_kwargs):
        output.write_bytes(
            _bounded_output_bytes(
                {
                    "match_info": {
                        "offset_seconds": [0],
                        "confidence": [1],
                        "locality_seconds": ["x" * 2_048],
                    }
                },
                max_output_bytes=128,
            )
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    with pytest.raises(AudalignOutputOverflowError):
        audalign_adapter.run_audalign_recognize(
            Path(sys.executable),
            tmp_path / "target.wav",
            tmp_path / "against.wav",
            output,
            process_runner=fake_run,
            max_output_bytes=128,
        )
    assert not output.exists()


PROFILE2_VECTOR_PATH = (
    Path(__file__).resolve().parents[3]
    / "core"
    / "tests"
    / "fixtures"
    / "multicam-alignment-vectors.json"
)
PROFILE2_VECTOR_IDS = tuple(
    f"MAV2-{index:03d}-{suffix}"
    for index, suffix in (
        (1, "profile1-profile2-readback-compatibility"),
        (2, "source-global-b-positive-negative-offset"),
        (3, "exact-schedule-candidate-admission"),
        (4, "multiple-probes-same-B-merge"),
        (5, "different-verified-B-conflict"),
        (6, "all-probes-zero-candidate-uncertain"),
        (7, "probe-schedule-and-call-ceiling-boundaries"),
        (8, "mono-two-groups-fail-bounded-lr"),
        (9, "multifile-pair-order-direct-source-ticks"),
        (10, "deadline-memory-overrun-zero-artifact"),
        (11, "repeated-fingerprint-fixture-bounded"),
        (12, "mono-worker-error-no-lr"),
        (13, "windows-hard-memory-enforcement-attribution-boundary"),
    )
)
PROFILE2_VECTOR_OWNERS = {
    "phase4_profile2_alignment_domain_compatibility",
    "phase4_profile2_historical_readback_input_projection_compatibility",
    "phase4_profile2_alignment_schedule_unit",
    "phase4_profile2_alignment_media_integration",
    "phase4_profile2_alignment_operation_process_integration",
}


def _profile2_vectors() -> dict[str, dict[str, object]]:
    payload = json.loads(PROFILE2_VECTOR_PATH.read_text(encoding="utf-8"))
    return {item["id"]: item for item in payload["profile2_vectors"]}


def _candidate(offset_seconds: str, *, upstream_index: int = 0):
    return SimpleNamespace(
        offset_seconds=offset_seconds,
        confidence=90 - upstream_index,
    )


def _match(candidates: tuple[object, ...], counts: tuple[int, ...] | None = None):
    return SimpleNamespace(
        candidates=candidates,
        raw_matching_fingerprint_counts=(
            tuple(range(1, len(candidates) + 1))
            if counts is None
            else counts
        ),
    )


def _run_profile2_pair(
    tmp_path: Path,
    monkeypatch,
    responses: list[object],
    *,
    main_duration: int = 120_000_000,
    auxiliary_duration: int = 98_668_800,
    verify_passes: bool = True,
    verify_error_ticks: int = 0,
    disk_limit: int = 536_870_912,
):
    extraction_spans: list[tuple[int, int]] = []
    verification_b: list[int] = []

    def fake_extract(_source, output, *, start_ticks, end_ticks, **_kwargs):
        extraction_spans.append((start_ticks, end_ticks))
        _write_workspace_wav(output)

    def fake_recognize(*_args, **_kwargs):
        return responses.pop(0)

    def fake_verify(_self, _main, _aux, **kwargs):
        verification_b.append(int(kwargs["candidate_b_ticks"]))
        return {
            "passed": verify_passes,
            "local_errors_ticks": [0, 0, 0],
            "max_local_offset_error_ticks": verify_error_ticks,
        }

    monkeypatch.setattr(alignments, "extract_wav_window", fake_extract)
    monkeypatch.setattr(alignments, "run_audalign_recognize", fake_recognize)
    monkeypatch.setattr(alignments.FixedOffsetVerifier, "verify", fake_verify)
    result = alignments._align_one_pair(
        "main",
        main_duration,
        tmp_path / "main.wav",
        "aux",
        auxiliary_duration,
        tmp_path / "aux.wav",
        "aux-1",
        tmp_path / "python",
        ToolResolution("ffmpeg", "ffmpeg", "fixture"),
        tmp_path,
        _Deadline(),
        4_294_967_296,
        None,
        profile2=True,
        workspace_budget=alignments._WorkspaceBudget(tmp_path, disk_limit),
    )
    return result, extraction_spans, verification_b


def _fake_asset(path: Path, duration_ticks: int):
    fingerprint = fingerprint_file(path)
    return SimpleNamespace(
        import_mode=ImportMode.LINKED,
        locator={"absolute_path": str(path)},
        fingerprint=SimpleNamespace(
            size=fingerprint.size,
            mtime_ns=fingerprint.mtime_ns,
            sha256_head_tail=fingerprint.sha256_head_tail,
        ),
        probe=SimpleNamespace(
            duration_ticks=duration_ticks,
            audio_codec="pcm_s16le",
            audio_sample_rate=44_100,
        ),
    )


def _frozen_asset(asset, source_id: str) -> dict[str, object]:
    path = Path(asset.locator["absolute_path"])
    return {
        "import_mode": asset.import_mode.value,
        "locator_identity_hash": canonical_sha256_v1({"locator": asset.locator}),
        "fingerprint": {
            "size": asset.fingerprint.size,
            "mtime_ns": asset.fingerprint.mtime_ns,
            "sha256_head_tail": asset.fingerprint.sha256_head_tail,
        },
        "identity": alignments._source_identity_evidence(path),
        "probe": {
            "duration_ticks": asset.probe.duration_ticks,
            "audio_codec": asset.probe.audio_codec,
            "audio_sample_rate": asset.probe.audio_sample_rate,
        },
    }


def _run_mono_first(tmp_path: Path, monkeypatch, recognize, verify, *, decode=None):
    main_duration = 120_000_000
    auxiliary_duration = 98_668_800
    main_path = tmp_path / "mono-main.wav"
    auxiliary_path = tmp_path / "mono-aux.wav"
    _write_workspace_wav(main_path, seconds=60)
    _write_workspace_wav(auxiliary_path, seconds=60)
    main_asset = _fake_asset(main_path, main_duration)
    auxiliary_asset = _fake_asset(auxiliary_path, auxiliary_duration)
    frozen = {
        "main": _frozen_asset(main_asset, "main"),
        "aux": _frozen_asset(auxiliary_asset, "aux"),
    }

    def fake_decode(_source, output, **_kwargs):
        _write_workspace_wav(output, seconds=60)

    def fake_extract(_source, output, **_kwargs):
        _write_workspace_wav(output)

    monkeypatch.setattr(
        alignments,
        "decode_alignment_audio",
        fake_decode if decode is None else decode,
    )
    monkeypatch.setattr(alignments, "extract_wav_window", fake_extract)
    monkeypatch.setattr(alignments, "run_audalign_recognize", recognize)
    monkeypatch.setattr(alignments.FixedOffsetVerifier, "verify", verify)
    result = alignments._align_one_pair_mono_first(
        "main",
        main_duration,
        main_path,
        main_path,
        "aux",
        auxiliary_duration,
        auxiliary_path,
        main_asset,
        auxiliary_asset,
        "aux-1",
        tmp_path / "mono-python",
        ToolResolution("ffmpeg", "ffmpeg", "fixture"),
        tmp_path,
        _Deadline(),
        4_294_967_296,
        None,
        alignments._WorkspaceBudget(tmp_path, 536_870_912),
        frozen,
    )
    return result


def _profile2_artifact(version: int) -> MulticamAlignmentArtifact:
    duration = 14_400_000
    profile = AlignmentVerificationProfile(
        name="roughcut_audalign_fixed_offset", version=version
    )
    algorithm = AlignmentAlgorithm(
        name="audalign_fingerprint",
        version="1.3.1",
        upstream_commit="d5955ae8a85b1cd480dadd005c3f88986f4ebbef",
        accuracy=2,
        num_processors=1,
        mapping_model="fixed_offset_equal_speed",
        ticks_per_second=120_000,
        verification_profile=profile,
    )
    interval = AlignmentInterval(
        interval_id="ali_profile2",
        auxiliary_camera_id="aux-1",
        classification="mapped",
        main={"source_id": "src_main_1", "start_ticks": 0, "end_ticks": duration},
        auxiliary={"source_id": "src_aux_1", "start_ticks": 0, "end_ticks": duration},
        evidence={
            "code": "fixed_offset_verified",
            "raw_candidate_count": 1,
            "matching_fingerprint_counts": [1],
            "verification_window_count": 3,
            "verification_profile": profile.to_dict(),
            "max_local_offset_error_ticks": 0,
        },
    )
    basis = (
        AlignmentSourceBasis(
            camera_id="aux-1",
            source_id="src_aux_1",
            fingerprint=AlignmentSourceFingerprint(1, 1, "b" * 64),
            duration_ticks=duration,
        ),
        AlignmentSourceBasis(
            camera_id="main",
            source_id="src_main_1",
            fingerprint=AlignmentSourceFingerprint(1, 1, "a" * 64),
            duration_ticks=duration,
        ),
    )
    return MulticamAlignmentArtifact(
        alignment_id="aln_profile2",
        project_id="project_profile2",
        producer_operation_id="op_00000000000040008000000000000002",
        created_at="2026-08-05T00:00:00.000000Z",
        request_hash="c" * 64,
        input_hash="d" * 64,
        algorithm=algorithm,
        main_camera=AlignmentCameraGroup("main", ("src_main_1",)),
        auxiliary_cameras=(
            AlignmentCamera(
                "aux-1", ("src_aux_1",), "complete", duration, 0, 0, 0, ()
            ),
        ),
        source_basis=basis,
        intervals=(interval,),
        summary=AlignmentSummary(duration, 1, duration, 0, 0, 0),
    )


def _operation_fixture(tmp_path: Path, monkeypatch):
    """Build one real Project/runtime basis for production operation seams."""
    from roughcut.adapters.runtime_binding import (
        AUDALIGN_PROVIDER,
        AUDALIGN_UPSTREAM_COMMIT,
        RuntimeAlignmentPython,
    )
    from roughcut.application.media_operations import _PersistentMediaRuntime
    from roughcut.application.projects import create_project
    from roughcut.application.sources import add_source

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    assert ffmpeg is not None and ffprobe is not None
    ffmpeg_version = _read_bound_version(ffmpeg, "ffmpeg")
    ffprobe_version = _read_bound_version(ffprobe, "ffprobe")
    monkeypatch.setenv("ROUGHCUT_FFMPEG_COMMAND", ffmpeg)
    monkeypatch.setenv("ROUGHCUT_FFPROBE_COMMAND", ffprobe)
    project_root = tmp_path / "operation-project"
    project = create_project(project_root, "Profile2 vector operation")
    main_path = tmp_path / "main.wav"
    aux_path = tmp_path / "aux.wav"
    _write_workspace_wav(main_path, seconds=40)
    _write_workspace_wav(aux_path, seconds=40)
    project = add_source(
        project_root,
        main_path,
        ImportMode.LINKED,
        expected_revision=project.revision,
    )
    project = add_source(
        project_root,
        aux_path,
        ImportMode.LINKED,
        expected_revision=project.revision,
    )
    from roughcut.adapters.runtime_binding import (
        audalign_distribution_versions_for,
        audalign_distributions_for,
    )

    dists = audalign_distributions_for("macos")
    vers = audalign_distribution_versions_for("macos")
    distributions = tuple({"name": n, "version": vers[n]} for n in dists)
    alignment_python = RuntimeAlignmentPython(
        source_type="managed",
        ownership="roughcut_managed",
        interpreter=str(Path(sys.executable).resolve()),
        python_version="3.11",
        distributions=distributions,
        dependency_lock_receipt={"algorithm": "sha256", "value": "f" * 64},
        license_notice_receipt={"algorithm": "sha256", "value": "e" * 64},
        component_manifest_receipt={"algorithm": "sha256", "value": "d" * 64},
        provider=AUDALIGN_PROVIDER,
        provider_version="1.3.1",
        upstream_commit=AUDALIGN_UPSTREAM_COMMIT,
    )
    runtime = _PersistentMediaRuntime(
        binding=SimpleNamespace(alignment_python=alignment_python),
        runtime_binding_sha256="a" * 64,
        python_receipt_hash="b" * 64,
        ffmpeg_tool_selection_hash="c" * 64,
        ffprobe_tool_selection_hash="d" * 64,
        ffmpeg=ToolResolution(ffmpeg, ffmpeg, ffmpeg_version),
        ffprobe=ToolResolution(ffprobe, ffprobe, ffprobe_version),
    )
    monkeypatch.setattr(alignments, "_load_persistent_runtime", lambda: runtime)
    return project_root, project, main_path, aux_path, runtime


def _read_bound_version(command: str, executable_name: str) -> str:
    result = subprocess.run(
        [command, "-version"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    lines = f"{result.stdout}\n{result.stderr}".splitlines()
    assert lines and lines[0].startswith(f"{executable_name} version ")
    return lines[0]


def _operation_request(
    project,
    main_id: str,
    aux_id: str,
    operation_id: str,
    alignment_id: str,
    *,
    disk_bytes: int = 536_870_912,
) -> dict[str, object]:
    return {
        "operation_id": operation_id,
        "alignment_id": alignment_id,
        "expected_revision": project.revision,
        "main_camera": {"camera_id": "main", "ordered_source_ids": [main_id]},
        "auxiliary_cameras": [
            {"camera_id": "aux-1", "ordered_source_ids": [aux_id]}
        ],
        "main_audio_stable": True,
        "max_temporary_disk_bytes": disk_bytes,
        "max_analysis_memory_bytes": 4_294_967_296,
        "max_runtime_seconds": 1_800,
    }


def test_profile2_schedule_grouping_and_call_ceiling() -> None:
    starts = profile2_probe_starts(98_668_800)
    assert starts == (0, 48_434_400, 96_868_800)
    assert profile2_probe_starts(1_799_999) == ()
    candidates = (
        CandidateOffset(100_000, "", 0, 0, 0),
        CandidateOffset(105_000, "", 0, 0, 1),
        CandidateOffset(300_000, "", 0, 0, 2),
    )
    groups = group_profile2_hypotheses(candidates)
    assert [[item.b_ticks for item in group] for group in groups] == [
        [100_000, 105_000],
        [300_000],
    ]
    assert groups[0][0].probe_index == 0
    profile2_call_plan(recall_calls=9, verification_calls=27)
    try:
        profile2_call_plan(recall_calls=10, verification_calls=27)
    except AlignmentProfileError as error:
        assert "ceiling" in str(error)
    else:
        raise AssertionError("the 37th planned call must be rejected")


def test_verifier_rechecks_after_every_window_extraction_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events, reservations, output_reservations = _run_recorded_verification(
        tmp_path, monkeypatch
    )

    for index, event in enumerate(events[:-1]):
        if event == "extract":
            assert events[index + 1] == "recheck"
    assert events.count("extract") == 6
    assert events.count("recheck") == 9
    assert reservations == [
        ALIGNMENT_VERIFICATION_WINDOW_TICKS
    ] * 6
    assert output_reservations == [AUDALIGN_WORKER_MAX_OUTPUT_BYTES] * 3


def test_verifier_rechecks_after_every_audalign_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events, _reservations, output_reservations = _run_recorded_verification(
        tmp_path, monkeypatch
    )

    for index, event in enumerate(events[:-1]):
        if event == "recognize":
            assert events[index + 1] == "recheck"
    assert events.count("recognize") == 3
    assert output_reservations == [AUDALIGN_WORKER_MAX_OUTPUT_BYTES] * 3


def test_verifier_reserves_window_before_child_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import roughcut.adapters.audalign.ffmpeg_audio as ffmpeg_audio_module

    child_calls: list[str] = []

    def fake_extract(*_args, **_kwargs):
        child_calls.append("extract")

    monkeypatch.setattr(
        ffmpeg_audio_module, "extract_wav_window", fake_extract
    )
    workspace = tmp_path / "verification-workspace"
    budget = alignments._WorkspaceBudget(workspace, max_disk_bytes=1)
    verifier = FixedOffsetVerifier(
        alignment_python=tmp_path / "alignment-python",
        ffmpeg_command="ffmpeg",
        workspace=workspace,
        deadline=_Deadline(),
        workspace_budget=budget,
    )

    with pytest.raises(AlignmentError) as exc:
        verifier.verify(
            tmp_path / "main.wav",
            tmp_path / "aux.wav",
            candidate_b_ticks=0,
            main_overlap_start_ticks=0,
            main_overlap_end_ticks=3 * ALIGNMENT_VERIFICATION_WINDOW_TICKS,
            auxiliary_overlap_start_ticks=0,
            auxiliary_overlap_end_ticks=3 * ALIGNMENT_VERIFICATION_WINDOW_TICKS,
        )

    assert exc.value.code == "alignment_disk_budget_exceeded"
    assert child_calls == []


def test_verifier_reserves_audalign_output_before_child_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import roughcut.adapters.audalign.ffmpeg_audio as ffmpeg_audio_module

    recognize_calls: list[str] = []

    def fake_extract(_source, output, **_kwargs):
        Path(output).write_bytes(b"window")

    def fake_recognize(*_args, **_kwargs):
        recognize_calls.append("recognize")
        raise AssertionError("output reservation must reject before audalign")

    monkeypatch.setattr(ffmpeg_audio_module, "extract_wav_window", fake_extract)
    from roughcut.application import alignment_profile as profile_module

    monkeypatch.setattr(profile_module, "run_audalign_recognize", fake_recognize)
    workspace = tmp_path / "verification-workspace"
    workspace.mkdir()
    budget = alignments._WorkspaceBudget(
        workspace, max_disk_bytes=AUDALIGN_WORKER_MAX_OUTPUT_BYTES - 1
    )
    verifier = FixedOffsetVerifier(
        alignment_python=tmp_path / "alignment-python",
        ffmpeg_command="ffmpeg",
        workspace=workspace,
        deadline=_Deadline(),
        workspace_budget=budget,
    )

    with pytest.raises(AlignmentError) as exc:
        verifier.verify(
            tmp_path / "main.wav",
            tmp_path / "aux.wav",
            candidate_b_ticks=0,
            main_overlap_start_ticks=0,
            main_overlap_end_ticks=3 * ALIGNMENT_VERIFICATION_WINDOW_TICKS,
            auxiliary_overlap_start_ticks=0,
            auxiliary_overlap_end_ticks=3 * ALIGNMENT_VERIFICATION_WINDOW_TICKS,
        )

    assert exc.value.code == "alignment_disk_budget_exceeded"
    assert recognize_calls == []


def test_verifier_failed_child_over_budget_is_disk_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import roughcut.adapters.audalign.ffmpeg_audio as ffmpeg_audio_module
    from roughcut.application import alignment_profile as profile_module

    workspace = tmp_path / "verification-workspace"
    workspace.mkdir()
    window_reservation = (12 * 44_100 * 2) + 4_096
    disk_limit = (
        AUDALIGN_WORKER_MAX_OUTPUT_BYTES + window_reservation * 2 + 100
    )

    def fake_extract(_source, output, **_kwargs):
        Path(output).write_bytes(b"window")

    def failed_recognize(*_args, **_kwargs):
        output = Path(_args[3])
        output.write_bytes(b"x" * (disk_limit + 1))
        raise AudalignAdapterError("recognition failed")

    monkeypatch.setattr(
        ffmpeg_audio_module, "extract_wav_window", fake_extract
    )
    monkeypatch.setattr(
        profile_module, "run_audalign_recognize", failed_recognize
    )
    verifier = FixedOffsetVerifier(
        alignment_python=tmp_path / "alignment-python",
        ffmpeg_command="ffmpeg",
        workspace=workspace,
        deadline=_Deadline(),
        workspace_budget=alignments._WorkspaceBudget(
            workspace, max_disk_bytes=disk_limit
        ),
    )

    with pytest.raises(AlignmentError) as exc:
        verifier.verify(
            tmp_path / "main.wav",
            tmp_path / "aux.wav",
            candidate_b_ticks=0,
            main_overlap_start_ticks=0,
            main_overlap_end_ticks=3 * ALIGNMENT_VERIFICATION_WINDOW_TICKS,
            auxiliary_overlap_start_ticks=0,
            auxiliary_overlap_end_ticks=3 * ALIGNMENT_VERIFICATION_WINDOW_TICKS,
        )

    assert exc.value.code == "alignment_disk_budget_exceeded"
    assert isinstance(exc.value.__cause__, AudalignAdapterError)


def test_verifier_failed_child_within_budget_preserves_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import roughcut.adapters.audalign.ffmpeg_audio as ffmpeg_audio_module
    from roughcut.application import alignment_profile as profile_module

    workspace = tmp_path / "verification-workspace"
    workspace.mkdir()
    window_reservation = (12 * 44_100 * 2) + 4_096
    disk_limit = (
        AUDALIGN_WORKER_MAX_OUTPUT_BYTES + window_reservation * 2 + 100
    )

    def fake_extract(_source, output, **_kwargs):
        Path(output).write_bytes(b"window")

    def failed_recognize(*_args, **_kwargs):
        Path(_args[3]).write_text("{}", encoding="utf-8")
        raise AudalignAdapterError("recognition failed")

    monkeypatch.setattr(
        ffmpeg_audio_module, "extract_wav_window", fake_extract
    )
    monkeypatch.setattr(
        profile_module, "run_audalign_recognize", failed_recognize
    )
    verifier = FixedOffsetVerifier(
        alignment_python=tmp_path / "alignment-python",
        ffmpeg_command="ffmpeg",
        workspace=workspace,
        deadline=_Deadline(),
        workspace_budget=alignments._WorkspaceBudget(
            workspace, max_disk_bytes=disk_limit
        ),
    )

    with pytest.raises(AudalignAdapterError, match="recognition failed"):
        verifier.verify(
            tmp_path / "main.wav",
            tmp_path / "aux.wav",
            candidate_b_ticks=0,
            main_overlap_start_ticks=0,
            main_overlap_end_ticks=3 * ALIGNMENT_VERIFICATION_WINDOW_TICKS,
            auxiliary_overlap_start_ticks=0,
            auxiliary_overlap_end_ticks=3 * ALIGNMENT_VERIFICATION_WINDOW_TICKS,
        )


def test_workspace_estimate_allows_one_26_minute_camera_pair_under_ceiling() -> None:
    duration = 26 * 60 * 120_000
    estimate = alignments._estimate_workspace_bytes((duration,), (duration,))

    assert estimate == 446_330_432
    assert estimate < 536_870_912


def test_audalign_output_and_window_scratch_fit_32_mib_overhead() -> None:
    wav_bytes_per_second = 44_100 * 2
    recall_scratch = 3 * (15 * wav_bytes_per_second + 4_096)
    verification_scratch = 6 * (12 * wav_bytes_per_second + 4_096)

    assert (
        AUDALIGN_WORKER_MAX_OUTPUT_BYTES
        + recall_scratch
        + verification_scratch
        < alignments.ANALYSIS_WORKSPACE_OVERHEAD_BYTES
    )


def test_runtime_validation_rechecks_after_each_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from roughcut.adapters.child_budget import ChildBudget

    events: list[str] = []
    runtime = SimpleNamespace(
        ffmpeg=SimpleNamespace(command="ffmpeg", version="ffmpeg version 8.1.0"),
        ffprobe=SimpleNamespace(command="ffprobe", version="ffprobe version 8.1.0"),
    )

    def fake_run(command, **_kwargs):
        events.append(f"probe:{command[0]}")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"{command[0]} version 8.1.0\n",
            stderr="",
        )

    monkeypatch.setattr(alignments, "run_bounded_child", fake_run)
    budget = _RecordingWorkspaceBudget(events, [])
    alignments._validate_alignment_runtime(
        runtime,
        ChildBudget(_Deadline(), 4_294_967_296),
        budget,
    )

    assert events == ["probe:ffmpeg", "recheck", "probe:ffprobe", "recheck"]


def test_runtime_validation_rechecks_exception_and_disk_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from roughcut.adapters.child_budget import (
        ChildBudget,
        ChildProcessTimeBudgetError,
    )

    runtime = SimpleNamespace(
        ffmpeg=SimpleNamespace(command="ffmpeg", version="ffmpeg version 8.1.0"),
        ffprobe=SimpleNamespace(command="ffprobe", version="ffprobe version 8.1.0"),
    )
    events: list[str] = []

    def failed_run(command, **_kwargs):
        events.append(f"probe:{command[0]}")
        raise ChildProcessTimeBudgetError("fixture timeout")

    monkeypatch.setattr(alignments, "run_bounded_child", failed_run)
    normal_budget = _RecordingWorkspaceBudget(events, [])
    with pytest.raises(AlignmentError) as normal_error:
        alignments._validate_alignment_runtime(
            runtime,
            ChildBudget(_Deadline(), 4_294_967_296),
            normal_budget,
        )
    assert normal_error.value.code == "alignment_time_budget_exceeded"
    assert events == ["probe:ffmpeg", "recheck"]

    class DiskBudget(_RecordingWorkspaceBudget):
        def recheck(self) -> None:
            self.events.append("recheck")
            raise AlignmentError(
                "alignment_disk_budget_exceeded", "fixture scratch overflow"
            )

    events.clear()
    disk_budget = DiskBudget(events, [])
    with pytest.raises(AlignmentError) as disk_error:
        alignments._validate_alignment_runtime(
            runtime,
            ChildBudget(_Deadline(), 4_294_967_296),
            disk_budget,
        )
    assert disk_error.value.code == "alignment_disk_budget_exceeded"
    assert isinstance(disk_error.value.__cause__, ChildProcessTimeBudgetError)
    assert events == ["probe:ffmpeg", "recheck"]


def test_26_minute_pair_releases_mono_and_left_scratch_before_next_decode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    duration = 26 * 60 * 120_000
    source_root = tmp_path / "sources"
    workspace = tmp_path / "workspace"
    source_root.mkdir()
    workspace.mkdir()
    main_source = source_root / "main.wav"
    auxiliary_source = source_root / "aux.wav"
    _write_workspace_wav(main_source)
    _write_workspace_wav(auxiliary_source)
    main_asset = _fake_asset(main_source, duration)
    auxiliary_asset = _fake_asset(auxiliary_source, duration)
    frozen = {
        "main": _frozen_asset(main_asset, "main"),
        "aux": _frozen_asset(auxiliary_asset, "aux"),
    }
    main_wav = workspace / "main-main.wav"
    _write_workspace_wav(main_wav, seconds=1)

    def fake_decode(_source, output, **_kwargs):
        _write_workspace_wav(Path(output), seconds=1)

    channel_calls: list[str | None] = []

    def fake_pair(*_args, channel=None, **_kwargs):
        channel_calls.append(channel)
        pair_workspace = workspace / "pair-main-aux"
        if channel == "right":
            assert not (pair_workspace / "scratch-left").exists()
        pair_workspace.mkdir(parents=True, exist_ok=True)
        (pair_workspace / f"scratch-{channel or 'mono'}").write_bytes(b"x")
        if channel is None:
            return {"classifications": "uncertain"}
        return {
            "classifications": "mapped",
            "b_ticks": 0,
            "raw_candidate_count": 1,
            "matching_fingerprint_counts": [1],
            "max_local_offset_error_ticks": 0,
        }

    monkeypatch.setattr(alignments, "decode_alignment_audio", fake_decode)
    monkeypatch.setattr(alignments, "_align_one_pair", fake_pair)
    budget = alignments._WorkspaceBudget(workspace, 536_870_912)

    result = alignments._align_one_pair_mono_first(
        "main",
        duration,
        main_wav,
        main_source,
        "aux",
        duration,
        auxiliary_source,
        main_asset,
        auxiliary_asset,
        "aux-1",
        tmp_path / "python",
        ToolResolution("ffmpeg", "ffmpeg", "fixture"),
        workspace,
        _Deadline(),
        4_294_967_296,
        None,
        budget,
        frozen,
    )

    assert result["classifications"] == "mapped"
    assert channel_calls == [None, "left", "right"]
    assert budget._observed_disk <= 536_870_912
    assert main_wav.exists()
    assert not (workspace / "pair-main-aux").exists()


def test_profile2_admission_uses_source_global_b_and_only_first_candidate(
    tmp_path: Path, monkeypatch
) -> None:
    starts = (0, 48_434_400, 96_868_800)
    expected_b = (68_584_315, -1_416_779, -1_228_594)
    extraction_starts: list[int] = []
    verification_b: list[int] = []

    class Candidate:
        def __init__(self, offset_seconds: str) -> None:
            self.offset_seconds = offset_seconds
            self.confidence = 99

    matches = []
    for index, (start, b) in enumerate(zip(starts, expected_b, strict=True)):
        delta_ticks = b + start
        delta = str(Decimal(delta_ticks) / Decimal(120_000))
        matches.append(
            type(
                "Match",
                (),
                {
                    "candidates": (
                        Candidate(delta),
                        Candidate("999"),
                    ),
                    "raw_matching_fingerprint_counts": (index + 1, index + 2),
                },
            )()
        )

    def fake_extract(_source, output, *, start_ticks, **_kwargs):
        extraction_starts.append(start_ticks)
        _write_workspace_wav(output)

    def fake_recognize(*_args, **_kwargs):
        return matches[len(extraction_starts) - 1]

    def fake_verify(_self, _main, _aux, **kwargs):
        verification_b.append(kwargs["candidate_b_ticks"])
        return {"passed": False, "reason": "fixture_gate_only"}

    monkeypatch.setattr(alignments, "extract_wav_window", fake_extract)
    monkeypatch.setattr(alignments, "run_audalign_recognize", fake_recognize)
    monkeypatch.setattr(alignments.FixedOffsetVerifier, "verify", fake_verify)

    result = alignments._align_one_pair(
        "main",
        120_000_000,
        tmp_path / "main.wav",
        "aux",
        98_668_800,
        tmp_path / "aux.wav",
        "aux-1",
        tmp_path / "python",
        ToolResolution("ffmpeg", "ffmpeg", "fixture"),
        tmp_path,
        _Deadline(),
        4_294_967_296,
        None,
        profile2=True,
        workspace_budget=alignments._WorkspaceBudget(tmp_path, 536_870_912),
    )

    assert PROFILE2_CANONICAL["max_selected_candidates_per_probe"] == 1
    assert extraction_starts == list(starts)
    assert verification_b == list(expected_b)
    assert result == {"classifications": "uncertain"}
    assert seconds_to_ticks("-10.25125") == -1_230_150


def test_profile2_rechecks_after_each_recall_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    events: list[str] = []
    reservations: list[int] = []
    budget = _RecordingWorkspaceBudget(events, reservations)

    def fake_extract(_source, output, **_kwargs):
        events.append("extract")
        Path(output).write_bytes(b"window")

    def fake_recognize(*args, **_kwargs):
        events.append("recognize")
        Path(args[3]).write_text("{}", encoding="utf-8")
        return SimpleNamespace(candidates=(), raw_matching_fingerprint_counts=())

    monkeypatch.setattr(alignments, "extract_wav_window", fake_extract)
    monkeypatch.setattr(alignments, "run_audalign_recognize", fake_recognize)
    result = alignments._align_one_pair(
        "main",
        120_000_000,
        tmp_path / "main.wav",
        "aux",
        98_668_800,
        tmp_path / "aux.wav",
        "aux-1",
        tmp_path / "python",
        ToolResolution("ffmpeg", "ffmpeg", "fixture"),
        tmp_path,
        _Deadline(),
        4_294_967_296,
        None,
        profile2=True,
        workspace_budget=budget,
    )

    assert result == {"classifications": "uncertain"}
    assert events == [
        "reserve",
        "extract",
        "recheck",
        "reserve-output",
        "recognize",
        "recheck",
    ] * 3
    assert reservations == [1_800_000] * 3


@pytest.mark.parametrize(
    "overflow_error",
    [AudalignCandidateLimitError, AudalignOutputOverflowError],
)
def test_profile2_worker_overflow_is_uncertain_without_truncated_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overflow_error: type[AudalignAdapterError],
) -> None:
    calls: list[dict[str, object]] = []

    def fake_extract(_source, output, **_kwargs):
        Path(output).write_bytes(b"window")

    def overflow(*_args, **kwargs):
        calls.append(kwargs)
        raise overflow_error("worker result is not complete")

    def fake_verify(*_args, **_kwargs):
        raise AssertionError("candidate overflow must not start verification")

    monkeypatch.setattr(alignments, "extract_wav_window", fake_extract)
    monkeypatch.setattr(alignments, "run_audalign_recognize", overflow)
    monkeypatch.setattr(alignments.FixedOffsetVerifier, "verify", fake_verify)

    result = alignments._align_one_pair(
        "main",
        120_000_000,
        tmp_path / "main.wav",
        "aux",
        98_668_800,
        tmp_path / "aux.wav",
        "aux-1",
        tmp_path / "python",
        ToolResolution("ffmpeg", "ffmpeg", "fixture"),
        tmp_path,
        _Deadline(),
        4_294_967_296,
        None,
        profile2=True,
        workspace_budget=alignments._WorkspaceBudget(tmp_path, 536_870_912),
    )

    assert result == {"classifications": "uncertain"}
    assert len(calls) == 1
    assert calls[0]["max_raw_candidates"] == 512


@pytest.mark.parametrize(
    "overflow_error",
    [AudalignCandidateLimitError, AudalignOutputOverflowError],
)
def test_profile2_verification_worker_overflow_is_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overflow_error: type[AudalignAdapterError],
) -> None:
    import roughcut.adapters.audalign.ffmpeg_audio as ffmpeg_audio_module
    from roughcut.application import alignment_profile as profile_module

    recognize_calls: list[int] = []

    def fake_extract(_source, output, **_kwargs):
        Path(output).write_bytes(b"window")

    def fake_recognize(*_args, **_kwargs):
        recognize_calls.append(len(recognize_calls) + 1)
        if len(recognize_calls) == 1:
            return _match((_candidate("0"),))
        if len(recognize_calls) <= 3:
            return _match(())
        raise overflow_error("verification evidence is not complete")

    monkeypatch.setattr(
        ffmpeg_audio_module, "extract_wav_window", fake_extract
    )
    monkeypatch.setattr(alignments, "extract_wav_window", fake_extract)
    monkeypatch.setattr(alignments, "run_audalign_recognize", fake_recognize)
    monkeypatch.setattr(
        profile_module, "run_audalign_recognize", fake_recognize
    )

    result = alignments._align_one_pair(
        "main",
        120_000_000,
        tmp_path / "main.wav",
        "aux",
        98_668_800,
        tmp_path / "aux.wav",
        "aux-1",
        tmp_path / "python",
        ToolResolution("ffmpeg", "ffmpeg", "fixture"),
        tmp_path,
        _Deadline(),
        4_294_967_296,
        None,
        profile2=True,
        workspace_budget=alignments._WorkspaceBudget(tmp_path, 536_870_912),
    )

    assert result == {"classifications": "uncertain"}
    assert recognize_calls == [1, 2, 3, 4]


@pytest.mark.parametrize(
    "overflow_error",
    [AudalignCandidateLimitError, AudalignOutputOverflowError],
)
def test_profile1_raw_worker_overflow_is_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overflow_error: type[AudalignAdapterError],
) -> None:
    def overflow(*_args, **_kwargs):
        raise overflow_error("raw evidence is not complete")

    monkeypatch.setattr(alignments, "run_audalign_recognize", overflow)
    result = alignments._align_one_pair(
        "main",
        120_000_000,
        tmp_path / "main.wav",
        "aux",
        98_668_800,
        tmp_path / "aux.wav",
        "aux-1",
        tmp_path / "python",
        ToolResolution("ffmpeg", "ffmpeg", "fixture"),
        tmp_path,
        _Deadline(),
        4_294_967_296,
        None,
        workspace_budget=alignments._WorkspaceBudget(tmp_path, 536_870_912),
    )

    assert result == {"classifications": "uncertain"}


def _execute_mav2_001(vector: dict[str, object], tmp_path: Path, monkeypatch) -> None:
    del monkeypatch
    input_data = vector["input"]
    expected = vector["expected"]
    assert isinstance(input_data, dict)
    assert isinstance(expected, dict)
    from roughcut.adapters.alignment_store import AlignmentStore

    # Profile 1/2 are historical read-only identities after the BBC writer
    # switch. Preserve their exact schema/canonical hash behavior without
    # asking the production coordinator to create a new profile-2 artifact.
    historical_versions: list[int] = []
    historical_schema_versions: list[int] = []
    historical_request_hashes: list[str] = []
    historical_input_hashes: list[str] = []
    profile_inputs = input_data["historical_alignment_profiles"]
    assert isinstance(profile_inputs, list)
    versions_to_read: list[int] = []
    for profile_input in profile_inputs:
        assert isinstance(profile_input, dict)
        assert profile_input["name"] == "roughcut_audalign_fixed_offset"
        version = profile_input["version"]
        assert isinstance(version, int)
        versions_to_read.append(version)
    for version in versions_to_read:
        historical = _profile2_artifact(version)
        assert MulticamAlignmentArtifact.from_dict(historical.to_dict()) == historical
        historical_root = tmp_path / f"historical-profile{version}"
        historical_root.mkdir()
        historical_store = AlignmentStore(historical_root)
        assert (
            historical_store.publish(historical.alignment_id, historical)
            == historical
        )
        readback = historical_store.read(historical.alignment_id)
        assert readback == historical
        assert readback is not None
        historical_versions.append(readback.algorithm.verification_profile.version)
        historical_schema_versions.append(readback.schema_version)
        historical_request_hashes.append(readback.request_hash)
        historical_input_hashes.append(readback.input_hash)
        assert all(
            interval.evidence["verification_profile"]
            == readback.algorithm.verification_profile.to_dict()
            for interval in readback.intervals
        )
    assert historical_versions == expected["historical_profile_versions"]
    assert historical_schema_versions == [
        input_data["artifact_schema_version_must_remain"]
    ] * len(versions_to_read)
    assert historical_schema_versions == expected["historical_schema_versions"]
    assert historical_request_hashes == expected["historical_request_hashes"]
    assert historical_input_hashes == expected["historical_input_hashes"]

    profile2_projection = alignments._alignment_input_projection(
        root=tmp_path,
        project=SimpleNamespace(project_id="project_profile2", revision=7),
        request_projection={
            "request_schema_version": 2,
            "operation_type": "align_multicam",
        },
        sources={},
        runtime=SimpleNamespace(
            binding=SimpleNamespace(alignment_python=None),
            ffmpeg_tool_selection_hash="c" * 64,
            ffprobe_tool_selection_hash="d" * 64,
        ),
        workspace_estimate=123_456,
        frozen_identity={},
        profile=deepcopy(PROFILE2_CANONICAL),
    )
    assert profile2_projection["profile"] == PROFILE2_CANONICAL
    profile2_input_hash = canonical_sha256_v1(profile2_projection)
    assert profile2_input_hash == expected["profile2_input_projection_hash"]

    changed_projection = deepcopy(profile2_projection)
    changed_profile = changed_projection["profile"]
    assert isinstance(changed_profile, dict)
    changed_profile["max_audalign_calls_per_source_pair"] = 35
    changed_input_hash = canonical_sha256_v1(changed_projection)
    assert changed_input_hash == expected["mutated_profile2_input_projection_hash"]
    assert changed_input_hash != profile2_input_hash


def _execute_mav2_002(vector: dict[str, object], _tmp_path: Path, _monkeypatch) -> None:
    input_data = vector["input"]
    expected = vector["expected"]
    assert isinstance(input_data, dict)
    assert isinstance(expected, dict)
    actual = []
    for case in input_data["cases"]:  # type: ignore[index]
        actual.append(
            source_relation_b(
                case["M0_ticks"],  # type: ignore[index]
                case["A0_ticks"],  # type: ignore[index]
                case["delta_seconds"],  # type: ignore[index]
            )
        )
    assert actual == expected["source_global_B_ticks"]
    assert expected["window_local_offset_not_used_as_B"] is True


def _execute_mav2_003(vector: dict[str, object], tmp_path: Path, monkeypatch) -> None:
    input_data = vector["input"]
    expected = vector["expected"]
    assert isinstance(input_data, dict)
    assert isinstance(expected, dict)
    responses = []
    for probe in input_data["probes"]:  # type: ignore[index]
        raw = []
        for item in probe["raw_candidates"]:  # type: ignore[index]
            raw.append(
                _candidate(
                    item.get("delta_seconds", "999"),  # type: ignore[union-attr]
                    upstream_index=item["upstream_index"],  # type: ignore[index]
                )
            )
        responses.append(_match(tuple(raw)))
    result, _spans, verification_b = _run_profile2_pair(
        tmp_path, monkeypatch, responses, verify_passes=False
    )
    assert result == {"classifications": "uncertain"}
    selected = expected["selected_hypothesis_B_ticks"]
    assert verification_b == selected
    assert len(verification_b) * 3 == expected["planned_verification_call_count"]
    assert expected["selected_upstream_indices"] == [0, 0, 0]
    assert expected["ending_correct_group_selected"] is True
    assert expected["result"] == "candidate_admission_gate_passed_not_mapped"


def _execute_mav2_004(vector: dict[str, object], _tmp_path: Path, _monkeypatch) -> None:
    input_data = vector["input"]
    expected = vector["expected"]
    assert isinstance(input_data, dict)
    assert isinstance(expected, dict)
    candidates = tuple(
        CandidateOffset(
            item["B_ticks"],  # type: ignore[index]
            "",
            item["confidence"],  # type: ignore[index]
            item["upstream_index"],  # type: ignore[index]
            input_data["probe_order"].index(item["probe"]),  # type: ignore[index]
        )
        for item in input_data["candidates"]  # type: ignore[index]
    )
    groups = group_profile2_hypotheses(candidates)
    assert len(groups) == expected["deduplicated_verified_B_count"]
    assert len(groups[0]) == expected["same_B_probe_support_count"]
    assert groups[0][0].probe_index == input_data["probe_order"].index(expected["representative_probe"])
    assert expected["conflict"] is False
    assert expected["saved_relation_is_source_global"] is True


def _execute_mav2_005(vector: dict[str, object], tmp_path: Path, monkeypatch) -> None:
    input_data = vector["input"]
    expected = vector["expected"]
    assert isinstance(input_data, dict)
    assert isinstance(expected, dict)
    starts = profile2_probe_starts(98_668_800)
    b_values = tuple(group["B_ticks"] for group in input_data["candidate_groups"])
    responses = [
        _match(
            (
                _candidate(
                    str(Decimal(b_values[index % len(b_values)] + start) / Decimal(120_000))
                ),
            )
        )
        for index, start in enumerate(starts)
    ]
    result, _spans, verification_b = _run_profile2_pair(
        tmp_path,
        monkeypatch,
        responses,
        verify_passes=True,
        verify_error_ticks=input_data["max_local_error_ticks"],
    )
    assert result["classifications"] == expected["classification"]
    assert verification_b == list(expected["verified_B_ticks"])
    assert len(verification_b) * input_data["each_group_verification_windows"] == expected["verification_call_count"]
    assert result["verification_window_count"] == input_data["each_group_verification_windows"]
    assert result["raw_candidate_count"] == len(result["matching_fingerprint_counts"])
    assert result["raw_candidate_count"] == 3
    assert result["max_local_offset_error_ticks"] == input_data["max_local_error_ticks"]
    assert expected["verified_B_count"] == len(verification_b)
    assert expected["both_verified_evidences_preserved"] is True
    assert expected["lr_override_attempted"] is False


def _execute_mav2_006(vector: dict[str, object], tmp_path: Path, monkeypatch) -> None:
    input_data = vector["input"]
    expected = vector["expected"]
    assert isinstance(input_data, dict)
    assert isinstance(expected, dict)
    calls: list[str] = []

    def fake_recognize(*args, **_kwargs):
        calls.append("recognize")
        return _match(())

    def fake_verify(*_args, **_kwargs):
        raise AssertionError("zero-candidate fallback must not verify")

    result = _run_mono_first(tmp_path, monkeypatch, fake_recognize, fake_verify)
    assert result["classifications"] == expected["pair_classification"]
    assert len(calls) == expected["recall_call_count"]
    assert expected["channel_classifications"] == ["uncertain", "uncertain", "uncertain"]
    assert expected["verification_call_count"] == 0
    assert expected["missing_interval_count"] == 0


def _execute_mav2_007(vector: dict[str, object], tmp_path: Path, monkeypatch) -> None:
    input_data = vector["input"]
    expected = vector["expected"]
    assert isinstance(input_data, dict)
    assert isinstance(expected, dict)
    duration_cases = input_data["duration_cases"]
    assert isinstance(duration_cases, list)
    for case in duration_cases:
        assert list(profile2_probe_starts(case["duration_ticks"])) == case["expected_probe_starts"]  # type: ignore[index]
    full_duration = input_data["duration_cases"][1]["duration_ticks"]
    starts = tuple(profile2_probe_starts(full_duration))
    b_values = (0, 2_000_000, 4_000_000)
    responses = [
        _match(
            (
                _candidate(
                    str(
                        (Decimal(b_values[index]) + Decimal(start))
                        / Decimal(120_000)
                    )
                ),
            )
        )
        for index, start in enumerate(starts)
    ]
    result, _spans, verification_b = _run_profile2_pair(
        tmp_path, monkeypatch, responses, verify_passes=True
    )
    assert result["classifications"] == "conflict"
    assert verification_b == list(b_values)
    assert len(verification_b) * 3 == expected["three_group_verification_call_count"]
    assert expected["three_group_admitted_for_verification"] is True

    profile2_call_plan(recall_calls=9, verification_calls=27)
    try:
        profile2_call_plan(recall_calls=9, verification_calls=28)
    except AlignmentProfileError:
        pass
    else:
        raise AssertionError("the 37th call plan must be rejected")

    too_many = _match(tuple(_candidate("0") for _ in range(513)))
    result, _spans, verification_b = _run_profile2_pair(
        tmp_path, monkeypatch, [too_many]
    )
    assert result["classifications"] == expected["raw_candidate_count_513_channel_classification"]
    assert verification_b == []
    assert expected["raw_candidate_count_513_truncated"] is False
    assert expected["raw_candidate_count_513_verification_call_count"] == 0

    original_group = alignments.group_profile2_hypotheses
    fourth_group = tuple(
        (CandidateOffset(index * 2_000_000, "", 1, 0, index),)
        for index in range(4)
    )
    monkeypatch.setattr(
        alignments,
        "group_profile2_hypotheses",
        lambda _candidates: fourth_group,
    )
    four_result, _spans, four_verification_b = _run_profile2_pair(
        tmp_path,
        monkeypatch,
        [_match((_candidate("0"),)), _match((_candidate("0"),)), _match((_candidate("0"),))],
    )
    assert four_result["classifications"] == expected["four_group_channel_classification"]
    assert four_verification_b == []
    monkeypatch.setattr(alignments, "group_profile2_hypotheses", original_group)
    assert expected["planned_call_count_37_result"] == "rejected"


def _execute_mav2_008(vector: dict[str, object], tmp_path: Path, monkeypatch) -> None:
    input_data = vector["input"]
    expected = vector["expected"]
    assert isinstance(input_data, dict)
    assert isinstance(expected, dict)
    starts = tuple(input_data["probe_starts_ticks"])
    b_values = tuple(
        group["B_ticks"] for group in input_data["mono"]["verification_group_results"]  # type: ignore[index]
    )
    recall_channels: list[str] = []
    verification_channels: list[str] = []

    def fake_recognize(_python, target, _main, _output, **_kwargs):
        name = Path(target).name
        channel = name.split("-")[1]
        probe_index = int(name.rsplit("-", 1)[-1].split(".", 1)[0])
        recall_channels.append(channel)
        if channel == "mono":
            selected_b = b_values[probe_index % len(b_values)]
            other_b = b_values[(probe_index + 1) % len(b_values)]
            candidates = tuple(
                _candidate(str(Decimal(b + starts[probe_index]) / Decimal(120_000)))
                for b in (selected_b, other_b)
            )
        else:
            b = input_data["left"]["verified_B_ticks"]  # type: ignore[index]
            candidates = (
                _candidate(
                    str(Decimal(b + starts[probe_index]) / Decimal(120_000))
                ),
            )
        return _match(candidates)

    def fake_verify(_self, _main, auxiliary_wav, **kwargs):
        channel = Path(auxiliary_wav).stem.split("-")[-1]
        verification_channels.append(channel)
        return {
            "passed": channel != "mono",
            "local_errors_ticks": [0, 0, 0],
            "max_local_offset_error_ticks": 0,
        }

    result = _run_mono_first(tmp_path, monkeypatch, fake_recognize, fake_verify)
    assert result["classifications"] == expected["final_pair_classification"]
    assert recall_channels == ["mono"] * 3 + ["left"] * 3 + ["right"] * 3
    assert verification_channels == ["mono", "mono", "left", "right"]
    assert len(verification_channels[:2]) * 3 == expected["mono_verification_calls"]
    assert len(verification_channels) * 3 == expected["total_verification_calls"]
    assert expected["mono_triggers_fallback"] is True
    assert expected["same_B_channels_merge"] is True
    assert not (tmp_path / "pair-main-aux").exists()


def _execute_mav2_009(vector: dict[str, object], tmp_path: Path, monkeypatch) -> None:
    input_data = vector["input"]
    expected = vector["expected"]
    assert isinstance(input_data, dict)
    assert isinstance(expected, dict)
    main_duration = 120_000_000
    pair_inputs = input_data["ordered_pairs"]
    assert isinstance(pair_inputs, list)
    source_wav = tmp_path / "ordered-source.wav"
    _write_workspace_wav(source_wav, seconds=60)
    sources = {
        f"src_main_{index}": _fake_asset(source_wav, main_duration)
        for index in (1, 2)
    }
    for pair in pair_inputs:
        auxiliary_duration = max(pair["A0_ticks"] + 1_800_000, 6_000_000)  # type: ignore[index]
        sources[pair["aux_source_id"]] = _fake_asset(  # type: ignore[index]
            source_wav, auxiliary_duration
        )
    frozen_identity = {
        source_id: _frozen_asset(asset, source_id)
        for source_id, asset in sources.items()
    }
    expected_b_by_pair = {
        (pair["main_source_id"], pair["aux_source_id"]): pair["expected_B_ticks"]  # type: ignore[index]
        for pair in pair_inputs
    }
    adapter_pair_order: list[str] = []
    adapter_b_by_pair: dict[tuple[str, str], list[int]] = {}
    recall_counts: dict[tuple[str, str], int] = {}
    seen_pairs: set[tuple[str, str]] = set()

    def fake_decode(_source, output, **_kwargs):
        _write_workspace_wav(output, seconds=60)

    def fake_extract(_source, output, **_kwargs):
        _write_workspace_wav(output)

    def fake_recognize(_python, target, _main, _output, **_kwargs):
        pair_name = Path(target).parent.name.removeprefix("pair-")
        main_id, aux_id = pair_name.split("-", 1)
        pair_key = (main_id, aux_id)
        if pair_key not in seen_pairs:
            seen_pairs.add(pair_key)
            adapter_pair_order.append(pair_name.replace("-", "↔", 1))
        recall_counts[pair_key] = recall_counts.get(pair_key, 0) + 1
        probe_index = int(Path(target).stem.rsplit("-", 1)[-1])
        start = profile2_probe_starts(
            sources[aux_id].probe.duration_ticks  # type: ignore[attr-defined]
        )[probe_index]
        b_ticks = expected_b_by_pair[pair_key]
        return _match(
            (
                _candidate(str(Decimal(b_ticks + start) / Decimal(120_000))),
            )
        )

    def fake_verify(_self, _main, auxiliary_wav, **kwargs):
        pair_name = Path(auxiliary_wav).parent.name.removeprefix("pair-")
        main_id, aux_id = pair_name.split("-", 1)
        pair_key = (main_id, aux_id)
        adapter_b_by_pair.setdefault(pair_key, []).append(
            int(kwargs["candidate_b_ticks"])
        )
        return {
            "passed": True,
            "local_errors_ticks": [0, 0, 0],
            "max_local_offset_error_ticks": 0,
        }

    monkeypatch.setattr(alignments, "decode_alignment_audio", fake_decode)
    monkeypatch.setattr(alignments, "extract_wav_window", fake_extract)
    monkeypatch.setattr(alignments, "run_audalign_recognize", fake_recognize)
    monkeypatch.setattr(alignments.FixedOffsetVerifier, "verify", fake_verify)
    workspace = tmp_path / "ordered-pair-workspace"
    for pair in pair_inputs:
        main_id = pair["main_source_id"]  # type: ignore[index]
        aux_id = pair["aux_source_id"]  # type: ignore[index]
        intervals, mapped, missing, uncertain, conflict, errors = (
            alignments._align_main_source_to_auxiliaries(
                main_id,
                main_duration,
                source_wav,
                AlignmentCameraGroup("aux-1", (aux_id,)),
                sources,
                tmp_path,
                source_wav,
                ToolResolution("ffmpeg", "ffmpeg", "fixture"),
                workspace,
                _Deadline(),
                4_294_967_296,
                None,
                alignments._WorkspaceBudget(workspace, 536_870_912),
                frozen_identity,
                failed_sources={},
            )
        )
        assert intervals
        assert mapped > 0
        assert mapped + missing == main_duration
        assert uncertain == conflict == 0
        assert errors == ()
    actual_b = [
        adapter_b_by_pair[(pair["main_source_id"], pair["aux_source_id"])]  # type: ignore[index]
        [0]
        for pair in pair_inputs
    ]
    assert adapter_pair_order == expected["pair_execution_order"]
    assert actual_b == expected["B_ticks"]
    assert all(count == 3 for count in recall_counts.values())
    assert sum(recall_counts.values()) <= expected["max_planned_recall_calls"]
    assert expected["chain_truth"] is False


def _execute_mav2_010(vector: dict[str, object], tmp_path: Path, monkeypatch) -> None:
    input_data = vector["input"]
    expected = vector["expected"]
    assert isinstance(input_data, dict)
    assert isinstance(expected, dict)
    from roughcut.adapters import child_budget as child_budget_module
    from roughcut.adapters.child_budget import (
        ChildBudget,
        ChildProcessTimeBudgetError,
        run_bounded_child,
    )
    from roughcut.adapters.media_operation_store import MediaOperationStore
    from roughcut.application.alignments import run_align_multicam

    project_root, project, _main_path, _aux_path, _runtime = _operation_fixture(
        tmp_path, monkeypatch
    )
    main_id, aux_id = project.sources[0].source_id, project.sources[1].source_id
    failure_kind: str | None = None
    disk_output_ready = False
    disk_budget = alignments._estimate_correlation_workspace_bytes(
        (project.sources[0].probe.duration_ticks,),
        (project.sources[1].probe.duration_ticks,),
    )

    def fake_decode(source, output, *, channel=None, **_kwargs):
        nonlocal disk_output_ready
        del source, channel
        if failure_kind == "disk":
            disk_output_ready = True
        _write_workspace_wav(Path(output), seconds=40)

    original_used_bytes = alignments._WorkspaceBudget._used_bytes

    def fake_used_bytes(budget):
        if failure_kind == "disk" and disk_output_ready:
            return budget.max_disk_bytes + 1
        return original_used_bytes(budget)

    monkeypatch.setattr(alignments._WorkspaceBudget, "_used_bytes", fake_used_bytes)

    def fake_extract(_source, output, *_args, **_kwargs):
        _write_workspace_wav(Path(output))

    monkeypatch.setattr(alignments, "decode_alignment_audio", fake_decode)
    monkeypatch.setattr(alignments, "_extract_aux_excerpt", fake_extract)
    def correlation_fault(*_args, **_kwargs):
        if failure_kind == "time":
            raise AudalignTimeBudgetError("fixture budget fault")
        if failure_kind == "memory":
            raise AudalignMemoryBudgetError("fixture budget fault")
        raise AssertionError("disk fault must be detected by workspace recheck")

    monkeypatch.setattr(alignments, "run_audalign_correlation", correlation_fault)

    child_pid_file = tmp_path / "bounded-child-descendant.pid"
    long_running_script = (
        "import pathlib, subprocess, sys, time;"
        "descendant = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
        "pathlib.Path(sys.argv[1]).write_text(str(descendant.pid), encoding='ascii');"
        "time.sleep(60)"
    )
    captured_processes = []
    original_popen = child_budget_module.subprocess.Popen

    def capture_popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        captured_processes.append(process)
        return process

    monkeypatch.setattr(child_budget_module.subprocess, "Popen", capture_popen)
    with pytest.raises(ChildProcessTimeBudgetError):
        run_bounded_child(
            [sys.executable, "-u", "-c", long_running_script, str(child_pid_file)],
            budget=ChildBudget(
                _MarkerArmedDeadline(child_pid_file, 0.3),
                4_294_967_296,
            ),
        )
    assert len(captured_processes) == 1
    child_process = captured_processes[0]
    assert child_process.returncode is not None
    assert child_process.wait(timeout=0) == child_process.returncode
    assert not _process_is_running(child_process.pid)
    assert child_pid_file.exists()
    descendant_pid = int(child_pid_file.read_text(encoding="ascii"))
    assert not _process_is_running(descendant_pid)

    for index, kind in enumerate(("time", "memory", "disk"), start=1):
        failure_kind = kind
        operation_id = f"op_00000000000040008000000000000{110 + index:03d}"
        alignment_id = f"aln_profile2_budget_{kind}"
        request = _operation_request(
            project,
            main_id,
            aux_id,
            operation_id,
            alignment_id,
            disk_bytes=(
                input_data["operation_budgets"]["max_temporary_disk_bytes"]
                if kind != "disk"
                else disk_budget
            ),
        )
        try:
            run_align_multicam(project_root, **request)
        except AlignmentError as error:
            expected_code = {
                "time": "alignment_time_budget_exceeded",
                "memory": "alignment_memory_budget_exceeded",
                "disk": "alignment_disk_budget_exceeded",
            }[kind]
            assert error.code == expected_code
        else:
            raise AssertionError("operation budget fault must fail")
        record = MediaOperationStore(project_root, project.project_id).read(operation_id)
        assert record is not None
        assert record.status == expected["operation_status"]
        assert record.result_ref is None
        assert not (project_root / "artifacts" / "multicam-alignments" / f"{alignment_id}.json").exists()
    assert input_data["artifact_schema_version"] == 1
    assert expected["operation_status"] == "failed"
    assert expected["artifact_publish_count"] == 0
    assert expected["per_camera_partial"] is False
    assert child_process.returncode is not None
    assert expected["all_child_processes_waited"] is True


def _execute_mav2_011(vector: dict[str, object], tmp_path: Path, monkeypatch) -> None:
    input_data = vector["input"]
    expected = vector["expected"]
    assert isinstance(input_data, dict)
    assert isinstance(expected, dict)
    starts = tuple(input_data["probe_starts_ticks"])
    responses = []
    for start in starts:
        candidates = [_candidate(str(Decimal(start) / Decimal(120_000)))]
        candidates.extend(_candidate("999", upstream_index=index) for index in range(1, 512))
        responses.append(_match(tuple(candidates), tuple(1 for _ in range(512))))
    result, spans, _verification = _run_profile2_pair(tmp_path, monkeypatch, responses)
    assert result["classifications"] == "mapped"
    assert len(spans) == expected["mono_recall_call_upper_bound"]
    assert all(end - start == expected["target_window_length_ticks"] for start, end in ((item[0], item[1]) for item in spans))
    profile2_call_plan(recall_calls=9, verification_calls=27)
    assert expected["full_length_target_window_count"] == 0
    assert expected["internal_fanout_resource_bound_proven"] is False
    assert expected["planned_call_count_37_result"] == "rejected"


def _execute_mav2_012(vector: dict[str, object], tmp_path: Path, monkeypatch) -> None:
    input_data = vector["input"]
    expected = vector["expected"]
    assert isinstance(input_data, dict)
    assert isinstance(expected, dict)
    calls: list[str] = []

    def fake_recognize(*_args, **_kwargs):
        calls.append("mono")
        if len(calls) == input_data["mono"]["recall_call_count"]:  # type: ignore[index]
            raise AudalignAdapterError("fixture worker error")
        return _match(())

    def fake_verify(*_args, **_kwargs):
        raise AssertionError("mono worker error must not verify or enter LR")

    result = _run_mono_first(tmp_path, monkeypatch, fake_recognize, fake_verify)
    assert result["classifications"] == expected["final_pair_classification"]
    assert result["error_code"] == expected["final_error_code"]
    assert len(calls) == expected["total_recall_calls"]
    assert expected["left_recall_calls"] == 0
    assert expected["right_recall_calls"] == 0
    assert expected["verification_call_count"] == 0


def _mav2_013_memory_operation_code(
    case: dict[str, object],
    tmp_path: Path,
    monkeypatch,
    *,
    operation_suffix: int,
    completion_query_failure: bool = False,
    peak_query_failure: bool = False,
) -> str:
    """Run one positive memory fact through runner, adapter and record."""
    from roughcut.adapters import audalign as audalign_adapter
    from roughcut.adapters import child_budget
    from roughcut.adapters.media_operation_store import MediaOperationStore
    from roughcut.application.alignments import run_align_multicam

    ceiling = 4_294_967_296
    case_id = case["case_id"]
    assert isinstance(case_id, str)
    with monkeypatch.context() as evidence_patch:
        project_root, project, _main, _aux, _runtime = _operation_fixture(
            tmp_path / case_id,
            evidence_patch,
        )
        main_id, aux_id = project.sources[0].source_id, project.sources[1].source_id

        def fake_decode(_source, output, **_kwargs):
            _write_workspace_wav(Path(output), seconds=40)

        def fake_extract(_source, output, **_kwargs):
            _write_workspace_wav(Path(output))

        evidence_patch.setattr(alignments, "decode_alignment_audio", fake_decode)
        evidence_patch.setattr(alignments, "extract_wav_window", fake_extract)
        runner_cleanup_events: list[str] | None = None
        evidence_query_events: list[str] | None = None

        if case_id == "typed_error_observed":

            def raise_typed_memory(*_args, **_kwargs):
                raise child_budget.ChildProcessMemoryBudgetError(
                    "typed child memory evidence"
                )

            evidence_patch.setattr(
                audalign_adapter,
                "run_bounded_child",
                raise_typed_memory,
            )
        else:
            process_holder: dict[str, object] = {}
            runner_cleanup_events = []
            evidence_query_events = []

            class _EvidenceProcess:
                pid = 81

                def __init__(self, args: list[str]) -> None:
                    self.args = args
                    self.running = True
                    self.returncode: int | None = None

                def communicate(self, *, timeout: float):
                    raise AssertionError("positive memory evidence precedes communicate")

                def poll(self) -> int | None:
                    return None if self.running else self.returncode

                def wait(self) -> int:
                    assert runner_cleanup_events is not None
                    runner_cleanup_events.append("root_popen_wait")
                    assert not self.running
                    if self.returncode is None:
                        self.returncode = 1
                    return self.returncode

                def kill(self) -> None:
                    self.running = False
                    self.returncode = 1

            class _EvidenceJob(child_budget._WindowsJob):
                def __init__(self, memory_limit: int) -> None:
                    assert memory_limit == ceiling
                    self._assigned = False
                    self.peak = case["peak"]
                    self.message_received = case["message_received"]

                @property
                def assigned(self) -> bool:
                    return self._assigned

                def assign(self, pid: int) -> None:
                    self._assigned = True

                def resume_initial_thread(self, pid: int) -> None:
                    return None

                def drain_notifications(self) -> bool:
                    assert evidence_query_events is not None
                    evidence_query_events.append("completion")
                    if completion_query_failure:
                        raise child_budget.ChildProcessBudgetError(
                            "completion evidence query failed"
                        )
                    assert isinstance(self.message_received, bool)
                    return self.message_received

                def peak_job_memory_used(self) -> int:
                    assert evidence_query_events is not None
                    evidence_query_events.append("peak")
                    if peak_query_failure:
                        raise child_budget.ChildProcessBudgetError(
                            "peak evidence query failed"
                        )
                    assert isinstance(self.peak, int)
                    return self.peak

                def active_processes(self) -> int:
                    process = process_holder["process"]
                    assert isinstance(process, _EvidenceProcess)
                    assert runner_cleanup_events is not None
                    runner_cleanup_events.append(
                        "class1_discovery"
                        if process.running
                        else "class1_active_processes_zero"
                    )
                    return 1 if process.running else 0

                def terminate(self) -> None:
                    process = process_holder["process"]
                    assert isinstance(process, _EvidenceProcess)
                    assert runner_cleanup_events is not None
                    runner_cleanup_events.append("terminate_job")
                    process.running = False

                def verify_empty(self) -> None:
                    assert self.active_processes() == 0

                def close_job_handle(self) -> None:
                    assert runner_cleanup_events is not None
                    runner_cleanup_events.append("close_job_handle")

                def close_job_for_emergency(self) -> None:
                    raise AssertionError("memory-positive cleanup must drain normally")

                def close_remaining_handles(self) -> None:
                    assert runner_cleanup_events is not None
                    runner_cleanup_events.extend(
                        (
                            "close_completion_port_handle",
                            "close_initial_thread_or_gate_handles",
                        )
                    )

            def fake_popen(command, **kwargs):
                assert kwargs["creationflags"] & 0x00000004
                process = _EvidenceProcess(command)
                process_holder["process"] = process
                return process

            evidence_patch.setattr(child_budget.sys, "platform", "win32")
            evidence_patch.setattr(child_budget, "_WindowsJob", _EvidenceJob)
            evidence_patch.setattr(child_budget.subprocess, "Popen", fake_popen)
            evidence_patch.setattr(
                audalign_adapter,
                "run_bounded_child",
                child_budget.run_bounded_child,
            )

        def correlation_with_production_adapter(
            alignment_python,
            target_wav,
            against_wav,
            output_path,
            *,
            budget,
            **_kwargs,
        ):
            return audalign_adapter.run_audalign_correlation(
                Path(alignment_python),
                Path(target_wav),
                Path(against_wav),
                Path(output_path),
                budget=budget,
            )

        evidence_patch.setattr(
            alignments,
            "run_audalign_correlation",
            correlation_with_production_adapter,
        )

        operation_id = f"op_00000000000040008000000000000{operation_suffix:03d}"
        alignment_id = f"aln_mav2_013_{operation_suffix}"
        request = _operation_request(
            project,
            main_id,
            aux_id,
            operation_id,
            alignment_id,
        )
        with pytest.raises(AlignmentError) as exc:
            run_align_multicam(project_root, **request)
        record = MediaOperationStore(project_root, project.project_id).read(operation_id)
        assert record is not None
        assert record.status == "failed"
        assert record.error is not None
        assert record.error.code == exc.value.code
        assert record.result_ref is None
        assert not (
            project_root
            / "artifacts"
            / "multicam-alignments"
            / f"{alignment_id}.json"
        ).exists()
        if runner_cleanup_events is not None:
            assert runner_cleanup_events == [
                "class1_discovery",
                "terminate_job",
                "root_popen_wait",
                "class1_active_processes_zero",
                "close_job_handle",
                "close_completion_port_handle",
                "close_initial_thread_or_gate_handles",
            ]
        if evidence_query_events is not None:
            assert evidence_query_events == ["completion", "peak"]
        return exc.value.code


def _mav2_013_cleanup_mapping(
    cleanup_cases: list[dict[str, object]],
    expected: dict[str, object],
    monkeypatch,
) -> dict[str, str]:
    """Drive real runner cleanup from raw observations, then classify output."""
    import ctypes

    from roughcut.adapters import child_budget

    observed: dict[str, str] = {}
    for raw in cleanup_cases:
        case_id = raw["case_id"]
        assert isinstance(case_id, str)
        events: list[str] = ["writer_acquire"]
        process_holder: dict[str, object] = {}
        job_holder: dict[str, object] = {}
        primary = child_budget.ChildProcessTimeBudgetError(
            f"primary cleanup failure for {case_id}"
        )

        class _CleanupProcess:
            pid = 82

            def __init__(self, args: list[str]) -> None:
                self.args = args
                self.running = bool(raw["root_or_descendant_alive_before_cleanup"])  # noqa: B023
                self.returncode: int | None = None if self.running else 0

            def communicate(self, *, timeout: float):
                assert not self.running
                self.returncode = 0
                return "", ""

            def poll(self) -> int | None:
                return None if self.running else self.returncode

            def kill(self) -> None:
                events.append("root_kill_after_job_close_failure")  # noqa: B023
                self.running = False
                self.returncode = 1

            def wait(self) -> int:
                events.append("root_popen_wait")  # noqa: B023
                if raw["root_wait_completed"] is not True:  # noqa: B023
                    raise child_budget.ChildProcessBudgetError(
                        "raw root wait observation failed"
                    )
                assert not self.running
                if self.returncode is None:
                    self.returncode = 1
                return self.returncode

        class _CleanupKernel:
            def __init__(self) -> None:
                self.query_count = 0

            def QueryInformationJobObject(
                self,
                _job,
                information_class,
                buffer,
                size,
                _returned,
            ):
                assert information_class == 1
                assert size == ctypes.sizeof(child_budget._WindowsBasicAccounting)
                self.query_count += 1
                events.append("class1_query_active_processes")  # noqa: B023
                if self.query_count > 1 and not raw["class1_query_succeeded"]:  # noqa: B023
                    return 0
                target = ctypes.cast(
                    buffer,
                    ctypes.POINTER(child_budget._WindowsBasicAccounting),
                ).contents
                process = process_holder["process"]  # noqa: B023
                assert isinstance(process, _CleanupProcess)
                if self.query_count == 1:
                    target.ActiveProcesses = int(
                        bool(raw["root_or_descendant_alive_before_cleanup"])  # noqa: B023
                    )
                else:
                    active_processes = raw["active_processes"]  # noqa: B023
                    assert isinstance(active_processes, int)
                    target.ActiveProcesses = active_processes
                return 1

            def TerminateJobObject(self, _job, _code):
                events.append("terminate_job")  # noqa: B023
                assert raw["terminate_called"] is True  # noqa: B023
                if raw["terminate_succeeded"] is not True:  # noqa: B023
                    return 0
                process = process_holder["process"]  # noqa: B023
                assert isinstance(process, _CleanupProcess)
                process.running = False
                return 1

            def CloseHandle(self, handle):
                process = process_holder["process"]  # noqa: B023
                assert isinstance(process, _CleanupProcess)
                if handle == 3:
                    events.append("close_job_handle")  # noqa: B023
                    succeeded = raw["job_close_succeeded"] is True  # noqa: B023
                    if succeeded:
                        process.running = False
                    return int(succeeded)
                remaining = raw["remaining_handle_close_results"]  # noqa: B023
                assert isinstance(remaining, dict)
                if handle == 4:
                    events.append("close_completion_port_handle")  # noqa: B023
                    result = remaining["completion_port"]
                else:
                    assert handle == 5
                    events.append("close_initial_thread_or_gate_handles")  # noqa: B023
                    result = remaining["initial_thread_or_gate"]
                return int(result is not False)

        kernel = _CleanupKernel()

        class _CleanupJob(child_budget._WindowsJob):
            def __init__(self, memory_limit: int) -> None:
                assert memory_limit == 4_294_967_296
                self._ctypes = ctypes
                self._kernel32 = kernel  # noqa: B023
                self._job = 3
                self._completion_port = 4
                self._completion_key = 3
                self._thread = 5
                self._assigned = False
                self._emergency_containment_occurred = False
                job_holder["job"] = self  # noqa: B023

            def assign(self, pid: int) -> None:
                self._assigned = True

            def resume_initial_thread(self, pid: int) -> None:
                return None

            def memory_limit_exceeded(self, limit: int) -> bool:
                return False

        class _CleanupDeadline:
            def remaining(self) -> float:
                if raw["root_or_descendant_alive_before_cleanup"]:  # noqa: B023
                    raise primary  # noqa: B023
                return 60.0

        def fake_popen(command, **kwargs):
            assert kwargs["creationflags"] & 0x00000004
            process = _CleanupProcess(command)
            process_holder["process"] = process  # noqa: B023
            return process

        caught: BaseException | None = None
        try:
            with monkeypatch.context() as cleanup_patch:
                cleanup_patch.setattr(child_budget.sys, "platform", "win32")
                cleanup_patch.setattr(child_budget, "_WindowsJob", _CleanupJob)
                cleanup_patch.setattr(child_budget.subprocess, "Popen", fake_popen)
                child_budget.run_bounded_child(
                    ["fake-child"],
                    budget=child_budget.ChildBudget(
                        _CleanupDeadline(),
                        4_294_967_296,
                    ),
                )
        except BaseException as error:  # noqa: BLE001
            caught = error
        finally:
            events.append("writer_release")
        events.append("runner_result_observed")

        job = job_holder["job"]
        assert isinstance(job, _CleanupJob)
        if job.emergency_containment_occurred:
            observed[case_id] = (
                "emergency_containment"
                if caught is primary
                else "job_containment_succeeded_remaining_handle_cleanup_failed"
            )
        elif caught is not None and caught is not primary:
            observed[case_id] = "job_containment_failed"
        else:
            observed[case_id] = "verified_normal_drain"

        assert events[-2:] == ["writer_release", "runner_result_observed"]
        if case_id == "cleanup_observation_e":
            assert events[events.index("terminate_job") : -1] == [
                "terminate_job",
                "root_popen_wait",
                "class1_query_active_processes",
                "close_job_handle",
                "close_completion_port_handle",
                "close_initial_thread_or_gate_handles",
                "writer_release",
            ]
        if case_id == "cleanup_observation_f":
            emergency_start = events.index("close_job_handle")
            assert events[emergency_start:-1] == [
                "close_job_handle",
                "root_popen_wait",
                "close_completion_port_handle",
                "close_initial_thread_or_gate_handles",
                "writer_release",
            ]
            assert job.emergency_containment_occurred is True
            assert caught is not primary

    normal = expected["windows_native_verified_normal_drain"]
    emergency = expected["deterministic_kernel_control_emergency_containment"]
    assert isinstance(normal, dict) and isinstance(emergency, dict)
    assert normal["ordered_cleanup_steps"] == [
        "terminate_job_if_required_and_require_success",
        "root_popen_wait",
        "class1_query_active_processes_zero",
        "close_job_handle",
        "close_completion_port_handle",
        "close_initial_thread_or_gate_handles",
        "release_writer_lock",
    ]
    assert emergency["ordered_cleanup_steps"] == [
        "close_last_job_handle_with_kill_on_job_close",
        "root_popen_wait",
        "close_all_remaining_non_job_handles",
        "release_writer_lock",
        "return_existing_failure_if_remaining_handle_closes_succeeded",
    ]
    return observed


def _execute_mav2_013(vector: dict[str, object], tmp_path: Path, monkeypatch) -> None:
    """Execute deterministic ABI, attribution and cleanup on every platform."""
    from roughcut.adapters import child_budget
    from roughcut.adapters.audalign.ffmpeg_audio import FFmpegAlignmentError

    input_data = vector["input"]
    expected = vector["expected"]
    assert isinstance(input_data, dict) and isinstance(expected, dict)
    cases = input_data["cases"]
    cleanup_cases = input_data["cleanup_cases"]
    assert isinstance(cases, list) and isinstance(cleanup_cases, list)
    by_id = {case["case_id"]: case for case in cases}
    assert input_data["operation_budgets"] == {
        "max_runtime_seconds": 1_800,
        "max_analysis_memory_bytes": 4_294_967_296,
        "max_temporary_disk_bytes": 536_870_912,
    }

    assert child_budget._WindowsJob._INFO_BASIC_ACCOUNTING == 1
    assert child_budget._WindowsJob._INFO_EXTENDED_LIMIT == 9
    assert child_budget._WindowsBasicAccounting.ActiveProcesses.offset >= 0
    assert child_budget._WindowsExtendedLimit.PeakJobMemoryUsed.offset >= 0

    observed = {
        case_id: _mav2_013_memory_operation_code(
            by_id[case_id],
            tmp_path,
            monkeypatch,
            operation_suffix=suffix,
        )
        for case_id, suffix in (
            ("peak_equals_ceiling_message_absent", 131),
            ("message_observed_peak_below_ceiling", 132),
            ("typed_error_observed", 133),
        )
    }
    observed.update(
        {
            "main_decode_nonzero_text_and_intent": alignments._classify_worker_error(
                FFmpegAlignmentError("ordinary nonzero child"),
                "alignment_decoding_main",
            ),
            "main_index_nonzero": alignments._classify_worker_error(
                AudalignAdapterError("ordinary nonzero child"),
                "alignment_indexing_main",
            ),
        }
    )

    def raise_decode(*_args, **_kwargs):
        raise FFmpegAlignmentError("ordinary nonzero child")

    with monkeypatch.context() as decode_patch:
        decode_result = _run_mono_first(
            tmp_path / "auxiliary-decode",
            decode_patch,
            lambda *_args, **_kwargs: _match(()),
            lambda *_args, **_kwargs: None,
            decode=raise_decode,
        )
    observed["auxiliary_decode_nonzero_text_and_intent"] = decode_result["error_code"]

    def raise_recognition(*_args, **_kwargs):
        raise AudalignAdapterError("ordinary nonzero child")

    with monkeypatch.context() as recognition_patch:
        recognition_result = _run_mono_first(
            tmp_path / "auxiliary-recognition",
            recognition_patch,
            raise_recognition,
            lambda *_args, **_kwargs: None,
        )
    observed["auxiliary_recognition_nonzero"] = recognition_result["error_code"]

    def raise_verification(*_args, **_kwargs):
        raise AudalignAdapterError("ordinary nonzero child")

    with monkeypatch.context() as verification_patch:
        verification_result = _run_mono_first(
            tmp_path / "auxiliary-verification",
            verification_patch,
            lambda *_args, **_kwargs: _match((_candidate("0"),)),
            raise_verification,
        )
    observed["auxiliary_verification_nonzero"] = verification_result["error_code"]

    deterministic = expected["deterministic_abi_fault_injection"]
    assert isinstance(deterministic, dict)
    assert observed == deterministic["case_error_mapping"]
    cleanup_observed = _mav2_013_cleanup_mapping(
        cleanup_cases,  # type: ignore[arg-type]
        expected,
        monkeypatch,
    )
    emergency = expected["deterministic_kernel_control_emergency_containment"]
    assert isinstance(emergency, dict)
    assert cleanup_observed == emergency["cleanup_case_mapping"]


@pytest.mark.parametrize(
    (
        "case_id",
        "message_received",
        "peak",
        "completion_query_failure",
        "peak_query_failure",
        "operation_suffix",
    ),
    [
        pytest.param(
            "message-positive-peak-query-failure",
            True,
            4_294_967_295,
            False,
            True,
            134,
            id="message-positive-peak-query-failure",
        ),
        pytest.param(
            "completion-query-failure-peak-exact",
            False,
            4_294_967_296,
            True,
            False,
            135,
            id="completion-query-failure-peak-exact",
        ),
    ],
)
def test_mav2_013_cross_query_positive_evidence_reaches_failed_operation(
    tmp_path: Path,
    monkeypatch,
    case_id: str,
    message_received: bool,
    peak: int,
    completion_query_failure: bool,
    peak_query_failure: bool,
    operation_suffix: int,
) -> None:
    """Each positive source survives failure of the other evidence query."""
    code = _mav2_013_memory_operation_code(
        {
            "case_id": case_id,
            "message_received": message_received,
            "peak": peak,
        },
        tmp_path,
        monkeypatch,
        operation_suffix=operation_suffix,
        completion_query_failure=completion_query_failure,
        peak_query_failure=peak_query_failure,
    )
    assert code == "alignment_memory_budget_exceeded"


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="MAV2-013 native Windows Job evidence requires Windows",
)
def test_mav2_013_windows_native_job_process_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Native-only hard ceiling, suspended assignment and tree drain owner."""
    from roughcut.adapters import child_budget

    vector = _profile2_vectors()[PROFILE2_VECTOR_IDS[12]]
    input_data = vector["input"]
    assert isinstance(input_data, dict)
    ceiling = input_data["operation_budgets"]["max_analysis_memory_bytes"]  # type: ignore[index]
    assert ceiling == 4_294_967_296
    root_marker = tmp_path / "native-root.pid"
    descendant_marker = tmp_path / "native-descendant.pid"
    descendant_script = (
        "import os, pathlib, sys, time;"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='ascii');"
        "time.sleep(60)"
    )
    root_script = (
        "import os, pathlib, subprocess, sys, time;"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='ascii');"
        f"subprocess.Popen([sys.executable, '-c', {descendant_script!r}, sys.argv[2]]);"
        "time.sleep(60)"
    )
    native_events: list[str] = []
    native_jobs: list[object] = []
    native_processes = []
    real_job = child_budget._WindowsJob
    real_popen = child_budget.subprocess.Popen

    class _AuditedWindowsJob(real_job):
        def __init__(self, memory_limit: int) -> None:
            assert memory_limit == 4_294_967_296
            super().__init__(memory_limit)
            native_jobs.append(self)
            self.drained_to_zero = False

        def assign(self, pid: int) -> None:
            super().assign(pid)
            native_events.append("assigned")

        def resume_initial_thread(self, pid: int) -> None:
            native_events.append("resume")
            super().resume_initial_thread(pid)

        def verify_empty(self) -> None:
            super().verify_empty()
            self.drained_to_zero = True

    def capture_native_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        native_processes.append(process)
        return process

    with monkeypatch.context() as native_patch:
        native_patch.setattr(child_budget, "_WindowsJob", _AuditedWindowsJob)
        native_patch.setattr(child_budget.subprocess, "Popen", capture_native_popen)
        with pytest.raises(child_budget.ChildProcessTimeBudgetError):
            child_budget.run_bounded_child(
                [
                    sys.executable,
                    "-c",
                    root_script,
                    str(root_marker),
                    str(descendant_marker),
                ],
                budget=child_budget.ChildBudget(
                    _MarkerArmedDeadline(descendant_marker, 0.3),
                    ceiling,
                ),
            )

    assert native_events[:2] == ["assigned", "resume"]
    assert len(native_processes) == 1
    root_process = native_processes[0]
    assert root_process.returncode is not None
    assert root_process.wait(timeout=0) == root_process.returncode
    assert root_marker.exists() and descendant_marker.exists()
    root_pid = int(root_marker.read_text(encoding="ascii"))
    descendant_pid = int(descendant_marker.read_text(encoding="ascii"))
    assert not _process_is_running(root_pid)
    assert not _process_is_running(descendant_pid)
    assert native_jobs and all(job.drained_to_zero for job in native_jobs)  # type: ignore[attr-defined]

    from roughcut.adapters import audalign as audalign_adapter

    rejected_output = tmp_path / "native-nonzero-output.json"
    with pytest.raises(AudalignAdapterError) as nonzero_exc:
        audalign_adapter.run_audalign_recognize(
            Path(sys.executable),
            tmp_path / "missing-target.wav",
            tmp_path / "missing-against.wav",
            rejected_output,
            budget=child_budget.ChildBudget(_Deadline(), ceiling),
        )
    assert not isinstance(nonzero_exc.value, AudalignMemoryBudgetError)
    assert not rejected_output.exists()


_PROFILE2_VECTOR_EXECUTORS = {
    PROFILE2_VECTOR_IDS[0]: _execute_mav2_001,
    PROFILE2_VECTOR_IDS[1]: _execute_mav2_002,
    PROFILE2_VECTOR_IDS[2]: _execute_mav2_003,
    PROFILE2_VECTOR_IDS[3]: _execute_mav2_004,
    PROFILE2_VECTOR_IDS[4]: _execute_mav2_005,
    PROFILE2_VECTOR_IDS[5]: _execute_mav2_006,
    PROFILE2_VECTOR_IDS[6]: _execute_mav2_007,
    PROFILE2_VECTOR_IDS[7]: _execute_mav2_008,
    PROFILE2_VECTOR_IDS[8]: _execute_mav2_009,
    PROFILE2_VECTOR_IDS[9]: _execute_mav2_010,
    PROFILE2_VECTOR_IDS[10]: _execute_mav2_011,
    PROFILE2_VECTOR_IDS[11]: _execute_mav2_012,
    PROFILE2_VECTOR_IDS[12]: _execute_mav2_013,
}


def test_profile2_vector_ids_owners_and_contract_executions_are_closed() -> None:
    vectors = _profile2_vectors()
    assert tuple(vectors) == PROFILE2_VECTOR_IDS
    assert len(tuple(vectors)) == len(set(vectors)) == 13
    assert {vector["execution_owner"] for vector in vectors.values()} == PROFILE2_VECTOR_OWNERS
    assert set(vectors) == set(_PROFILE2_VECTOR_EXECUTORS)
    assert len(_PROFILE2_VECTOR_EXECUTORS) == 13


@pytest.mark.parametrize("vector_id", PROFILE2_VECTOR_IDS)
def test_profile2_vector_has_one_contract_execution(
    vector_id: str, tmp_path: Path, monkeypatch
) -> None:
    vector = _profile2_vectors()[vector_id]
    _PROFILE2_VECTOR_EXECUTORS[vector_id](vector, tmp_path, monkeypatch)
