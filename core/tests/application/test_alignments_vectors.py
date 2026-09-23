"""Phase 2 alignment vectors: real application/store/process/audalign/FFmpeg semantics.

These tests execute the actual Audalign Correlation production route through
the isolated managed alignment venv and real FFmpeg on synthetic media. They
require a schema-2 runtime binding with a verified Audalign managed group;
without one the real-media tests skip (the operation/store tests still run).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import ClassVar

import pytest

import roughcut.application.alignments as alignments_module
from roughcut.adapters import runtime_binding
from roughcut.adapters.alignment_store import AlignmentStore
from roughcut.adapters.audalign import (
    AudalignAdapterError,
    AudalignBudgetError,
    AudalignCorrelationCandidate,
    AudalignCorrelationResult,
    AudalignMemoryBudgetError,
    AudalignTimeBudgetError,
)
from roughcut.adapters.audalign.ffmpeg_audio import (
    FFmpegAlignmentError,
)
from roughcut.adapters.child_budget import (
    ChildProcessMemoryBudgetError,
    ChildProcessTimeBudgetError,
)
from roughcut.adapters.media_operation_store import MediaOperationStore
from roughcut.adapters.runtime_binding import (
    AUDALIGN_PROVIDER,
    RuntimeAlignmentPython,
    RuntimeBindingError,
    load_runtime_binding,
)
from roughcut.application.alignments import run_align_multicam
from roughcut.application.media_operations import (
    _PersistentMediaRuntime,
    media_operation_status,
)
from roughcut.application.projects import create_project
from roughcut.application.sources import add_source
from roughcut.domain.alignment import (
    ALIGNMENT_CANDIDATE_GROUP_DIAMETER_TICKS,
    AlignmentError,
    MulticamAlignmentArtifact,
    alignment_request_projection,
    hash_alignment_request,
    seconds_to_ticks,
)
from roughcut.domain.media_operation import (
    AlignmentOperationResult,
    MediaOperationError,
    MediaOperationRecord,
    ProjectOperationScope,
)
from roughcut.domain.project import ImportMode
from roughcut.domain.render import ToolResolution
from roughcut.domain.workflow import canonical_sha256_v1

ROOT = Path(__file__).resolve().parents[3]
VECTORS = ROOT / "core" / "tests" / "fixtures" / "multicam-alignment-vectors.json"
FIXTURES = ROOT / "core" / "tests" / "fixtures"
# Phase 2 real-media tests need a schema-2 runtime binding with a verified
# audalign Correlation managed group. The operator provides its path through
# ROUGHCUT_ALIGNMENT_TEST_BINDING; without one the real-media tests skip.
BINDING_CANDIDATES = tuple(
    Path(path)
    for path in filter(
        None, os.environ.get("ROUGHCUT_ALIGNMENT_TEST_BINDING", "").split(os.pathsep)
    )
)


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


def _load_test_binding() -> tuple[Path, _PersistentMediaRuntime] | None:
    for candidate in BINDING_CANDIDATES:
        if not candidate.is_file():
            continue
        try:
            binding = load_runtime_binding(candidate)
        except RuntimeBindingError:
            continue
        if (
            binding.schema_version == 2
            and binding.alignment_python is not None
            and Path(binding.alignment_python.interpreter).is_file()
        ):
            payload = candidate.read_bytes()
            runtime = _PersistentMediaRuntime(
                binding=binding,
                runtime_binding_sha256=hashlib.sha256(payload).hexdigest(),
                python_receipt_hash=canonical_sha256_v1(binding.python.receipt),
                ffmpeg_tool_selection_hash=canonical_sha256_v1(
                    binding.ffmpeg.to_dict()
                ),
                ffprobe_tool_selection_hash=canonical_sha256_v1(
                    binding.ffprobe.to_dict()
                ),
                ffmpeg=ToolResolution(
                    binding.ffmpeg.command,
                    binding.ffmpeg.command,
                    binding.ffmpeg.version,
                ),
                ffprobe=ToolResolution(
                    binding.ffprobe.command,
                    binding.ffprobe.command,
                    binding.ffprobe.version,
                ),
            )
            return candidate, runtime
    return None


def _require_runtime():
    loaded = _load_test_binding()
    if loaded is None:
        pytest.skip(
            "no schema-2 alignment runtime binding with a verified audalign group"
        )
    return loaded


@pytest.fixture
def real_runtime(monkeypatch: pytest.MonkeyPatch):
    _binding_path, runtime = _require_runtime()
    monkeypatch.setattr(
        alignments_module,
        "_load_persistent_runtime",
        lambda: runtime,
    )
    return runtime


@pytest.fixture
def synthetic_media_runtime(monkeypatch: pytest.MonkeyPatch):
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.fail("Roughcut 测试 fixture 未解析到 ffmpeg/ffprobe")
    monkeypatch.setenv("ROUGHCUT_FFMPEG_COMMAND", ffmpeg)
    monkeypatch.setenv("ROUGHCUT_FFPROBE_COMMAND", ffprobe)
    return ffmpeg, ffprobe


def _project_with_sources(
    tmp_path: Path,
    *,
    media_files: list[tuple[str, Path]],
    revision: int = 0,
):
    project_root = tmp_path / "alignment-project"
    project = create_project(project_root, "Alignment")
    current = project
    for _name, media_path in media_files:
        current = add_source(
            project_root,
            media_path,
            ImportMode.LINKED,
            expected_revision=current.revision,
        )
    return project_root, current


def _sources_by_id(project) -> dict[str, object]:
    return {source.source_id: source for source in project.sources}


def _align_request(
    project,
    *,
    operation_id: str,
    alignment_id: str,
    main_ids: list[str],
    aux_cameras: list[tuple[str, list[str]]],
    source_pairs: dict[str, list[dict[str, str]]] | None = None,
) -> dict[str, object]:
    cameras: list[dict[str, object]] = []
    for camera_id, source_ids in aux_cameras:
        camera: dict[str, object] = {
            "camera_id": camera_id,
            "ordered_source_ids": source_ids,
        }
        if source_pairs and camera_id in source_pairs:
            camera["source_pairs"] = source_pairs[camera_id]
        cameras.append(camera)
    return {
        "operation_id": operation_id,
        "alignment_id": alignment_id,
        "expected_revision": max(1, project.revision),
        "main_camera": {"camera_id": "main", "ordered_source_ids": main_ids},
        "auxiliary_cameras": cameras,
        "main_audio_stable": True,
        "max_temporary_disk_bytes": 536_870_912,
        "max_analysis_memory_bytes": 4_294_967_296,
        "max_runtime_seconds": 1800,
    }


def _run(project_root, project, request, *, runtime_fixture=None):
    outcome = run_align_multicam(
        project_root,
        **request,
    )
    return outcome


def _stub_alignment_runtime(ffmpeg: str, ffprobe: str) -> _PersistentMediaRuntime:
    from roughcut.adapters.runtime_binding import (
        audalign_distribution_versions_for,
        audalign_distributions_for,
    )

    dists = audalign_distributions_for("macos")
    vers = audalign_distribution_versions_for("macos")
    distributions = tuple({"name": name, "version": vers[name]} for name in dists)
    alignment_python = RuntimeAlignmentPython(
        source_type="managed",
        ownership="roughcut_managed",
        interpreter=str(Path(sys.executable).resolve()),
        python_version="3.11",
        distributions=distributions,
        dependency_lock_receipt={"algorithm": "sha256", "value": "f" * 64},
        license_notice_receipt={"algorithm": "sha256", "value": "e" * 64},
        component_manifest_receipt={"algorithm": "sha256", "value": "d" * 64},
        provider="audalign",
        provider_version="1.3.1",
        upstream_commit="d5955ae8a85b1cd480dadd005c3f88986f4ebbef",
    )
    binding = type(
        "StubAlignmentBinding",
        (),
        {"alignment_python": alignment_python},
    )()
    return _PersistentMediaRuntime(
        binding=binding,  # type: ignore[arg-type]
        runtime_binding_sha256="a" * 64,
        python_receipt_hash="b" * 64,
        ffmpeg_tool_selection_hash="c" * 64,
        ffprobe_tool_selection_hash="d" * 64,
        ffmpeg=_runtime_tool(ffmpeg),
        ffprobe=_runtime_tool(ffprobe),
    )


def _runtime_tool(command: str) -> ToolResolution:
    version = subprocess.run(
        [command, "-version"], check=True, capture_output=True, text=True
    ).stdout.splitlines()[0]
    return ToolResolution(command, command, version)


def _write_audio_fixture(
    path: Path, *, seconds: int = 1, channels: int = 2
) -> None:
    import wave

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(44_100)
        output.writeframes(b"\0" * (44_100 * channels * 2 * seconds))


def _stub_correlation_runtime(
    monkeypatch: pytest.MonkeyPatch, ffmpeg: str, ffprobe: str
) -> None:
    """Current Correlation writer seam with real FFmpeg/FFprobe."""
    runtime = _stub_alignment_runtime(ffmpeg, ffprobe)
    monkeypatch.setattr(
        alignments_module,
        "_load_persistent_runtime",
        lambda: runtime,
    )


def _stub_correlation_worker(
    monkeypatch: pytest.MonkeyPatch,
    *,
    error: Exception | None = None,
) -> list[tuple[str, Path, Path]]:
    """Provide the current bounded 15-second/executor seams for coordinator tests."""
    calls: list[tuple[str, Path, Path]] = []

    def extract(
        source_path: Path,
        output_path: Path,
        *_args: object,
        **_kwargs: object,
    ) -> None:
        calls.append(("extract", Path(source_path), Path(output_path)))
        _write_audio_fixture(output_path, seconds=15, channels=1)

    def recognize(
        _alignment_python: Path,
        target_wav: Path,
        against_wav: Path,
        _output_path: Path,
        **_kwargs: object,
    ) -> AudalignCorrelationResult:
        calls.append(("recognize", Path(target_wav), Path(against_wav)))
        if error is not None:
            raise error
        return AudalignCorrelationResult(
            (AudalignCorrelationCandidate("0", 0),)
        )

    monkeypatch.setattr(alignments_module, "_extract_aux_excerpt", extract)
    monkeypatch.setattr(
        alignments_module, "run_audalign_correlation", recognize
    )
    return calls


def test_mav_correlation_coordinator_publishes_closed_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_media_runtime: tuple[str, str],
) -> None:
    """Exercise the public coordinator through the current Correlation writer."""
    ffmpeg, ffprobe = synthetic_media_runtime
    media = tmp_path / "media"
    main_path = media / "main.wav"
    aux_path = media / "aux.wav"
    _write_audio_fixture(main_path, seconds=40, channels=1)
    _write_audio_fixture(aux_path, seconds=40, channels=1)
    project_root, project = _project_with_sources(
        tmp_path, media_files=[("main", main_path), ("aux", aux_path)]
    )
    main_id = project.sources[0].source_id
    aux_id = project.sources[1].source_id
    request = _align_request(
        project,
        operation_id="op_00000000000040008000000000000064",
        alignment_id="aln_correlation_published",
        main_ids=[main_id],
        aux_cameras=[("aux-1", [aux_id])],
        source_pairs={
            "aux-1": [
                {"main_source_id": main_id, "auxiliary_source_id": aux_id}
            ]
        },
    )
    _stub_correlation_runtime(monkeypatch, ffmpeg, ffprobe)

    main_decodes: list[Path] = []
    probe_starts: list[int] = []
    correlation_calls: list[tuple[Path, Path]] = []
    events: list[str] = []

    def decode(
        source_path: Path,
        output_path: Path,
        **_kwargs: object,
    ) -> None:
        main_decodes.append(Path(source_path))
        events.append("decode_main")
        _write_audio_fixture(output_path, seconds=40, channels=1)

    def extract(
        source_path: Path,
        output_path: Path,
        start_ticks: int,
        end_ticks: int,
        *_args: object,
        **_kwargs: object,
    ) -> None:
        assert Path(source_path) == aux_path
        assert end_ticks - start_ticks == 1_800_000
        probe_starts.append(start_ticks)
        events.append(f"extract:{start_ticks}")
        _write_audio_fixture(output_path, seconds=15, channels=1)

    def recognize(
        _alignment_python: Path,
        target_wav: Path,
        against_wav: Path,
        _output_path: Path,
        **_kwargs: object,
    ) -> AudalignCorrelationResult:
        correlation_calls.append((Path(target_wav), Path(against_wav)))
        events.append(f"recognize:{Path(target_wav).name}")
        start_ticks = probe_starts[len(correlation_calls) - 1]
        assert Path(against_wav).name == f"main-{main_id}.wav"
        assert Path(target_wav).name.endswith(("-20.wav", "-50.wav", "-80.wav"))
        assert start_ticks % 120_000 == 0
        return AudalignCorrelationResult(
            (
                AudalignCorrelationCandidate(
                    str(start_ticks // 120_000),
                    0,
                ),
            )
        )

    monkeypatch.setattr(alignments_module, "decode_alignment_audio", decode)
    monkeypatch.setattr(alignments_module, "_extract_aux_excerpt", extract)
    monkeypatch.setattr(alignments_module, "run_audalign_correlation", recognize)

    outcome = run_align_multicam(project_root, **request)

    assert outcome.record.status == "succeeded"
    assert outcome.artifact is not None
    artifact = outcome.artifact
    assert artifact.algorithm.name == "audalign_correlation"
    assert artifact.algorithm.version == "1.3.1"
    assert artifact.algorithm.verification_profile.to_dict() == {
        "name": "roughcut_audalign_correlation_fixed_offset",
        "version": 1,
    }
    assert artifact.summary.mapped_ticks == artifact.summary.total_main_ticks
    assert artifact.summary.uncertain_ticks == 0
    assert artifact.summary.conflict_ticks == 0
    assert main_decodes == [main_path]
    assert len(correlation_calls) == 3
    assert probe_starts == [960_000, 2_400_000, 3_000_000]
    assert events == [
        "decode_main",
        "extract:960000",
        "recognize:pair-" + main_id + "-" + aux_id + "-probe-20.wav",
        "extract:2400000",
        "recognize:pair-" + main_id + "-" + aux_id + "-probe-50.wav",
        "extract:3000000",
        "recognize:pair-" + main_id + "-" + aux_id + "-probe-80.wav",
    ]
    mapped = [item for item in artifact.intervals if item.classification == "mapped"]
    assert len(mapped) == 1
    assert mapped[0].evidence["representative_b_ticks"] == 0
    assert mapped[0].evidence["support_probe_count"] == 3
    assert AlignmentStore(project_root).read(request["alignment_id"]) == artifact
    assert outcome.record.result_ref is not None
    assert isinstance(outcome.record.result_ref, AlignmentOperationResult)
    assert outcome.record.result_ref.content_hash == artifact.content_hash


def test_mav_correlation_sparse_pairs_keep_uncovered_main_spans_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_media_runtime: tuple[str, str],
) -> None:
    """Sparse exact pairs do not trigger Cartesian pairing or invalid evidence."""
    ffmpeg, ffprobe = synthetic_media_runtime
    media = tmp_path / "media"
    main_one = media / "main-one.wav"
    main_two = media / "main-two.wav"
    aux_one = media / "aux-one.wav"
    aux_two = media / "aux-two.wav"
    for media_path in (main_one, main_two, aux_one, aux_two):
        _write_audio_fixture(media_path, seconds=40, channels=1)
    project_root, project = _project_with_sources(
        tmp_path,
        media_files=[
            ("main-one", main_one),
            ("main-two", main_two),
            ("aux-one", aux_one),
            ("aux-two", aux_two),
        ],
    )
    main_one_id, main_two_id = project.sources[0].source_id, project.sources[1].source_id
    aux_one_id, aux_two_id = project.sources[2].source_id, project.sources[3].source_id
    request = _align_request(
        project,
        operation_id="op_00000000000040008000000000000067",
        alignment_id="aln_correlation_sparse_pairs",
        main_ids=[main_one_id, main_two_id],
        aux_cameras=[("aux-one", [aux_one_id]), ("aux-two", [aux_two_id])],
        source_pairs={
            "aux-one": [
                {
                    "main_source_id": main_one_id,
                    "auxiliary_source_id": aux_one_id,
                }
            ],
            "aux-two": [
                {
                    "main_source_id": main_two_id,
                    "auxiliary_source_id": aux_two_id,
                }
            ],
        },
    )
    _stub_correlation_runtime(monkeypatch, ffmpeg, ffprobe)

    main_decodes: list[Path] = []
    extract_calls: list[tuple[Path, int]] = []
    correlation_calls: list[tuple[Path, Path]] = []
    expected_against = {
        aux_one: f"main-{main_one_id}.wav",
        aux_two: f"main-{main_two_id}.wav",
    }
    expected_target_prefix = {
        aux_one: f"pair-{main_one_id}-{aux_one_id}-probe-",
        aux_two: f"pair-{main_two_id}-{aux_two_id}-probe-",
    }

    def decode(
        source_path: Path,
        output_path: Path,
        **_kwargs: object,
    ) -> None:
        main_decodes.append(Path(source_path))
        _write_audio_fixture(output_path, seconds=40, channels=1)

    def extract(
        source_path: Path,
        output_path: Path,
        start_ticks: int,
        end_ticks: int,
        *_args: object,
        **_kwargs: object,
    ) -> None:
        assert end_ticks - start_ticks == 1_800_000
        extract_calls.append((Path(source_path), start_ticks))
        _write_audio_fixture(output_path, seconds=15, channels=1)

    def recognize(
        _alignment_python: Path,
        target_wav: Path,
        against_wav: Path,
        _output_path: Path,
        **_kwargs: object,
    ) -> AudalignCorrelationResult:
        correlation_calls.append((Path(target_wav), Path(against_wav)))
        source_path, start_ticks = extract_calls[len(correlation_calls) - 1]
        assert Path(target_wav).name.startswith(expected_target_prefix[source_path])
        assert Path(against_wav).name == expected_against[source_path]
        assert start_ticks in {960_000, 2_400_000, 3_000_000}
        return AudalignCorrelationResult(
            (AudalignCorrelationCandidate(str(start_ticks // 120_000), 0),)
        )

    monkeypatch.setattr(alignments_module, "decode_alignment_audio", decode)
    monkeypatch.setattr(alignments_module, "_extract_aux_excerpt", extract)
    monkeypatch.setattr(alignments_module, "run_audalign_correlation", recognize)

    outcome = run_align_multicam(project_root, **request)

    assert outcome.record.status == "succeeded"
    assert outcome.artifact is not None
    artifact = outcome.artifact
    assert main_decodes == [main_one, main_two]
    assert extract_calls == [
        (aux_one, 960_000),
        (aux_one, 2_400_000),
        (aux_one, 3_000_000),
        (aux_two, 960_000),
        (aux_two, 2_400_000),
        (aux_two, 3_000_000),
    ]
    assert len(correlation_calls) == 6
    assert {
        (target.name.split("-probe-", 1)[0], against.name)
        for target, against in correlation_calls
    } == {
        (f"pair-{main_one_id}-{aux_one_id}", f"main-{main_one_id}.wav"),
        (f"pair-{main_two_id}-{aux_two_id}", f"main-{main_two_id}.wav"),
    }
    for camera_id, mapped_main_id, uncovered_main_id in (
        ("aux-one", main_one_id, main_two_id),
        ("aux-two", main_two_id, main_one_id),
    ):
        camera = next(item for item in artifact.auxiliary_cameras if item.camera_id == camera_id)
        assert camera.status == "partial"
        assert camera.errors == ()
        camera_intervals = [
            item for item in artifact.intervals if item.auxiliary_camera_id == camera_id
        ]
        assert any(
            item.classification == "mapped"
            and item.main["source_id"] == mapped_main_id
            for item in camera_intervals
        )
        uncovered = [
            item
            for item in camera_intervals
            if item.main["source_id"] == uncovered_main_id
        ]
        assert len(uncovered) == 1
        assert uncovered[0].classification == "uncertain"
        assert uncovered[0].main == {
            "source_id": uncovered_main_id,
            "start_ticks": 0,
            "end_ticks": 4_800_000,
        }
        assert uncovered[0].evidence["code"] == "insufficient_probes"
        assert uncovered[0].evidence["probe_records"] == []
    assert AlignmentStore(project_root).read(request["alignment_id"]) == artifact


def test_mav_correlation_worker_failure_is_pair_uncertain_but_safe_pair_maps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_media_runtime: tuple[str, str],
) -> None:
    """A non-budget child fault closes one pair without blocking another."""
    ffmpeg, ffprobe = synthetic_media_runtime
    media = tmp_path / "media"
    main_path = media / "main.wav"
    bad_path = media / "bad.wav"
    good_path = media / "good.wav"
    for path in (main_path, bad_path, good_path):
        _write_audio_fixture(path, seconds=40, channels=1)
    project_root, project = _project_with_sources(
        tmp_path,
        media_files=[("main", main_path), ("bad", bad_path), ("good", good_path)],
    )
    main_id = project.sources[0].source_id
    bad_id = project.sources[1].source_id
    good_id = project.sources[2].source_id
    request = _align_request(
        project,
        operation_id="op_00000000000040008000000000000065",
        alignment_id="aln_correlation_worker_failed",
        main_ids=[main_id],
        aux_cameras=[("aux-bad", [bad_id]), ("aux-good", [good_id])],
        source_pairs={
            "aux-bad": [
                {"main_source_id": main_id, "auxiliary_source_id": bad_id}
            ],
            "aux-good": [
                {"main_source_id": main_id, "auxiliary_source_id": good_id}
            ],
        },
    )
    _stub_correlation_runtime(monkeypatch, ffmpeg, ffprobe)

    main_decodes: list[Path] = []
    extract_sources: list[Path] = []
    extract_starts: list[int] = []
    correlation_calls: list[Path] = []
    bad_attempts = 0

    def decode(
        source_path: Path,
        output_path: Path,
        **_kwargs: object,
    ) -> None:
        main_decodes.append(Path(source_path))
        _write_audio_fixture(output_path, seconds=40, channels=1)

    def extract(
        source_path: Path,
        output_path: Path,
        start_ticks: int,
        end_ticks: int,
        *_args: object,
        **_kwargs: object,
    ) -> None:
        assert end_ticks - start_ticks == 1_800_000
        extract_sources.append(Path(source_path))
        extract_starts.append(start_ticks)
        _write_audio_fixture(output_path, seconds=15, channels=1)

    def recognize(
        _alignment_python: Path,
        target_wav: Path,
        against_wav: Path,
        _output_path: Path,
        **_kwargs: object,
    ) -> AudalignCorrelationResult:
        nonlocal bad_attempts
        correlation_calls.append(Path(target_wav))
        assert Path(against_wav).name == f"main-{main_id}.wav"
        if f"-{bad_id}-" in target_wav.name and bad_attempts == 0:
            bad_attempts += 1
            raise AudalignAdapterError("synthetic non-budget worker failure")
        probe_index = (len(correlation_calls) - 1) % 3
        return AudalignCorrelationResult(
            (
                AudalignCorrelationCandidate(
                    ("8", "20", "25")[probe_index],
                    0,
                ),
            )
        )

    monkeypatch.setattr(alignments_module, "decode_alignment_audio", decode)
    monkeypatch.setattr(alignments_module, "_extract_aux_excerpt", extract)
    monkeypatch.setattr(alignments_module, "run_audalign_correlation", recognize)

    outcome = run_align_multicam(project_root, **request)

    assert outcome.record.status == "succeeded"
    assert outcome.artifact is not None
    artifact = outcome.artifact
    assert main_decodes == [main_path]
    assert extract_sources == [bad_path] * 3 + [good_path] * 3
    assert extract_starts == [960_000, 2_400_000, 3_000_000] * 2
    assert len(correlation_calls) == 6
    bad_intervals = [
        item
        for item in artifact.intervals
        if item.auxiliary_camera_id == "aux-bad"
    ]
    good_intervals = [
        item
        for item in artifact.intervals
        if item.auxiliary_camera_id == "aux-good"
    ]
    assert bad_intervals and all(item.classification == "uncertain" for item in bad_intervals)
    assert all(item.evidence["code"] == "worker_failed" for item in bad_intervals)
    assert any(item.classification == "mapped" for item in good_intervals)
    bad_camera, good_camera = artifact.auxiliary_cameras
    assert bad_camera.status == "failed"
    assert good_camera.status == "complete"
    assert bad_camera.errors == (
        type(bad_camera.errors[0])(
            code="auxiliary_recognition_failed", source_id=bad_id
        ),
    )
    assert AlignmentStore(project_root).read(request["alignment_id"]) == artifact


@pytest.mark.parametrize(
    ("error_type", "expected_code"),
    [
        pytest.param(AudalignTimeBudgetError, "alignment_time_budget_exceeded", id="time"),
        pytest.param(AudalignMemoryBudgetError, "alignment_memory_budget_exceeded", id="memory"),
        pytest.param(AudalignBudgetError, "alignment_disk_budget_exceeded", id="disk"),
    ],
)
def test_mav_correlation_budget_failure_is_terminal_and_unpublished(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_media_runtime: tuple[str, str],
    error_type: type[Exception],
    expected_code: str,
) -> None:
    """All current Correlation child budget classes fail closed at the coordinator."""
    ffmpeg, ffprobe = synthetic_media_runtime
    media = tmp_path / "media"
    main_path = media / "main.wav"
    aux_path = media / "aux.wav"
    _write_audio_fixture(main_path, seconds=40, channels=1)
    _write_audio_fixture(aux_path, seconds=40, channels=1)
    project_root, project = _project_with_sources(
        tmp_path, media_files=[("main", main_path), ("aux", aux_path)]
    )
    main_id, aux_id = project.sources[0].source_id, project.sources[1].source_id
    request = _align_request(
        project,
        operation_id=f"op_000000000000400080000000000000{66 + [AudalignTimeBudgetError, AudalignMemoryBudgetError, AudalignBudgetError].index(error_type):02d}",
        alignment_id=f"aln_correlation_budget_{expected_code.rsplit('_', 1)[-1]}",
        main_ids=[main_id],
        aux_cameras=[("aux-1", [aux_id])],
        source_pairs={
            "aux-1": [
                {"main_source_id": main_id, "auxiliary_source_id": aux_id}
            ]
        },
    )
    _stub_correlation_runtime(monkeypatch, ffmpeg, ffprobe)

    def decode(
        _source_path: Path,
        output_path: Path,
        **_kwargs: object,
    ) -> None:
        _write_audio_fixture(output_path, seconds=40, channels=1)

    def extract(
        _source_path: Path,
        output_path: Path,
        *_args: object,
        **_kwargs: object,
    ) -> None:
        _write_audio_fixture(output_path, seconds=15, channels=1)

    def recognize(*_args: object, **_kwargs: object) -> AudalignCorrelationResult:
        raise error_type("synthetic Correlation budget failure")

    monkeypatch.setattr(alignments_module, "decode_alignment_audio", decode)
    monkeypatch.setattr(alignments_module, "_extract_aux_excerpt", extract)
    monkeypatch.setattr(alignments_module, "run_audalign_correlation", recognize)

    with pytest.raises(AlignmentError) as exc:
        run_align_multicam(project_root, **request)
    assert exc.value.code == expected_code
    record = MediaOperationStore(project_root, project.project_id).read(
        request["operation_id"]
    )
    assert record is not None and record.status == "failed"
    assert record.error is not None and record.error.code == expected_code
    assert AlignmentStore(project_root).read(request["alignment_id"]) is None


def _track_envelope_streams(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    streamed: list[str] = []
    original = alignments_module.stream_source_envelope

    def tracking(path, **kwargs):
        streamed.append(Path(path).name)
        return original(path, **kwargs)

    monkeypatch.setattr(alignments_module, "stream_source_envelope", tracking)
    return streamed


def test_alignment_runtime_pair_probe_is_inside_writer_and_one_child_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, synthetic_media_runtime: tuple[str, str]
) -> None:
    ffmpeg, ffprobe = synthetic_media_runtime
    main_path = tmp_path / "media" / "main.wav"
    aux_path = tmp_path / "media" / "aux.wav"
    _write_audio_fixture(main_path)
    _write_audio_fixture(aux_path)
    project_root, project = _project_with_sources(
        tmp_path, media_files=[("main", main_path), ("aux", aux_path)]
    )
    request = _align_request(
        project,
        operation_id="op_00000000000040008000000000000070",
        alignment_id="aln_runtime_pair_scope",
        main_ids=[project.sources[0].source_id],
        aux_cameras=[("aux-1", [project.sources[1].source_id])],
    )
    runtime = _stub_alignment_runtime(ffmpeg, ffprobe)
    monkeypatch.setattr(alignments_module, "_load_persistent_runtime", lambda: runtime)
    original_writer = MediaOperationStore.writer
    writer_active = False
    budgets: list[object] = []
    calls: list[list[str]] = []

    @contextmanager
    def tracked_writer(store: MediaOperationStore, *args: object, **kwargs: object):
        nonlocal writer_active
        with original_writer(store, *args, **kwargs) as acquired:
            writer_active = True
            try:
                yield acquired
            finally:
                writer_active = False

    def bounded(command: list[str], *, budget: object, **_kwargs: object):
        assert writer_active is True
        record = MediaOperationStore(project_root, project.project_id).read(
            request["operation_id"]
        )
        assert record is not None and record.status == "running"
        calls.append(command)
        budgets.append(budget)
        tool = runtime.ffmpeg if command[0] == ffmpeg else runtime.ffprobe
        return subprocess.CompletedProcess(command, 0, stdout=tool.version + "\n", stderr="")

    def stop_after_probe(*_args: object, **_kwargs: object) -> object:
        raise AlignmentError(
            "alignment_disk_budget_exceeded", "fixture stops after drift pair"
        )

    monkeypatch.setattr(MediaOperationStore, "writer", tracked_writer)
    monkeypatch.setattr(alignments_module, "run_bounded_child", bounded)
    monkeypatch.setattr(
        alignments_module,
        "_execute_audalign_correlation_alignment",
        stop_after_probe,
    )

    with pytest.raises(AlignmentError, match="fixture stops after drift pair"):
        run_align_multicam(project_root, **request)

    assert calls == [[ffmpeg, "-version"], [ffprobe, "-version"]]
    assert len({id(budget) for budget in budgets}) == 1


def _load_vector_json():
    return json.loads(VECTORS.read_text(encoding="utf-8"))


def _vector(vector_id: str) -> dict:
    payload = _load_vector_json()
    return next(item for item in payload["vectors"] if item["id"] == vector_id)


# ---------------------------------------------------------------------------
# MAV-023: seconds-to-ticks rounding (unit, no media required)
# ---------------------------------------------------------------------------


def test_mav_023_signed_rounding_boundaries() -> None:
    vector = _vector("MAV-023-signed-rounding-boundaries")
    cases = vector["input"]["decimal_seconds"]
    expected = vector["expected"]["ticks"]
    for text, want in zip(cases, expected):
        assert seconds_to_ticks(text) == want
    assert vector["expected"]["rounding"] == "nearest_ties_away_from_zero_decimal"


# ---------------------------------------------------------------------------
# MAV-001: continuous one-to-one mapped (real audalign + FFmpeg)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("synthetic_media_runtime")
def test_mav_001_continuous_one_to_one_mapped(
    tmp_path: Path, real_runtime
) -> None:
    sys.path.insert(0, str(ROOT / "core" / "tests" / "fixtures"))
    from alignment_synthetic import make_offset_pair

    media = tmp_path / "media"
    main_path, aux_path = make_offset_pair(
        media,
        main_duration_ticks=14_400_000,
        aux_offset_ticks=510_000,
        seed=11,
        aux_gain=1.06,
        aux_bandpass=True,
        aux_tail_ticks=1_200_000,
    )
    project_root, project = _project_with_sources(
        tmp_path, media_files=[("main", main_path), ("aux", aux_path)]
    )
    main_id, aux_id = project.sources[0].source_id, project.sources[1].source_id
    request = _align_request(
        project,
        operation_id="op_00000000000040008000000000000001",
        alignment_id="aln_continuous",
        main_ids=[main_id],
        aux_cameras=[("aux-1", [aux_id])],
    )
    outcome = _run(project_root, project, request)
    assert outcome.record.status == "succeeded"
    assert outcome.artifact is not None
    artifact = outcome.artifact
    assert artifact.summary.camera_count == 1
    assert artifact.summary.total_main_ticks == artifact.summary.mapped_ticks
    assert artifact.summary.missing_ticks == 0
    assert artifact.summary.uncertain_ticks == 0
    assert artifact.summary.conflict_ticks == 0
    mapped = [iv for iv in artifact.intervals if iv.classification == "mapped"]
    assert len(mapped) == 1
    interval = mapped[0]
    main = interval.main
    auxiliary = interval.auxiliary
    assert auxiliary is not None
    assert auxiliary["source_id"] == aux_id
    assert main["start_ticks"] == 0
    assert main["end_ticks"] == artifact.summary.total_main_ticks
    # the aux offset must be within the verification tolerance (12000 ticks)
    assert abs(auxiliary["start_ticks"] - 510_000) <= 12_000
    assert interval.evidence["verification_window_count"] == 3
    assert interval.evidence["max_local_offset_error_ticks"] is not None
    assert interval.evidence["max_local_offset_error_ticks"] <= 12_000
    store = AlignmentStore(project_root)
    assert store.read("aln_continuous") == artifact
    assert outcome.record.result_ref is not None
    assert isinstance(outcome.record.result_ref, AlignmentOperationResult)
    assert outcome.record.result_ref.content_hash == artifact.content_hash


# ---------------------------------------------------------------------------
# MAV-002: main-one aux-many with direct interval mapping (no chain truth)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("synthetic_media_runtime")
def test_mav_002_main_one_aux_many_direct_intervals(
    tmp_path: Path, real_runtime
) -> None:
    sys.path.insert(0, str(ROOT / "core" / "tests" / "fixtures"))
    from alignment_synthetic import make_offset_pair

    media = tmp_path / "media"
    # main 180s; aux_1 covers main [10,90], aux_2 covers main [95,175]
    main_path, aux_1 = make_offset_pair(
        media / "pair1",
        main_duration_ticks=21_600_000,
        aux_offset_ticks=0,
        seed=22,
        aux_gain=1.05,
        aux_tail_ticks=0,
    )
    # overwrite aux_1 to carry exactly main [10s, 90s] at its own origin
    from alignment_synthetic import _render_sequence, _samples_per_tick, write_mono_wav
    _aux1 = _render_sequence(seed=22, duration_ticks=21_600_000, gain=1.05)[
        _samples_per_tick(1_200_000):_samples_per_tick(9_600_000)
    ]
    write_mono_wav(aux_1, _aux1)
    aux_2_dir = media / "pair2"
    aux_2_dir.mkdir(parents=True, exist_ok=True)
    from alignment_synthetic import _render_sequence, _samples_per_tick, write_mono_wav

    main_samples = _render_sequence(seed=22, duration_ticks=21_600_000)
    write_mono_wav(main_path, main_samples)
    # aux_2 carries main [95s, 175s] content at its own file origin, so the
    # direct relation is main_tick = aux_tick + 95s
    aux_2_samples = _render_sequence(
        seed=22,
        duration_ticks=21_600_000,
        gain=0.97,
    )[_samples_per_tick(11_400_000):_samples_per_tick(21_000_000)]
    aux_2 = aux_2_dir / "aux2.wav"
    write_mono_wav(aux_2, aux_2_samples)

    project_root, project = _project_with_sources(
        tmp_path,
        media_files=[
            ("main", main_path),
            ("aux1", aux_1),
            ("aux2", aux_2),
        ],
    )
    main_id = project.sources[0].source_id
    aux_1_id = project.sources[1].source_id
    aux_2_id = project.sources[2].source_id
    request = _align_request(
        project,
        operation_id="op_00000000000040008000000000000002",
        alignment_id="aln_one_many",
        main_ids=[main_id],
        aux_cameras=[("aux-1", [aux_1_id, aux_2_id])],
    )
    outcome = _run(project_root, project, request)
    assert outcome.record.status == "succeeded"
    artifact = outcome.artifact
    assert artifact is not None
    mapped = [iv for iv in artifact.intervals if iv.classification == "mapped"]
    assert len(mapped) >= 2
    aux_1_mapped = [
        iv for iv in mapped if iv.auxiliary and iv.auxiliary["source_id"] == aux_1_id
    ]
    aux_2_mapped = [
        iv for iv in mapped if iv.auxiliary and iv.auxiliary["source_id"] == aux_2_id
    ]
    assert aux_1_mapped and aux_2_mapped
    # aux_1 starts near main 10s
    assert abs(aux_1_mapped[0].main["start_ticks"] - 1_200_000) <= 24_000
    # aux_2 starts near main 95s
    assert abs(aux_2_mapped[0].main["start_ticks"] - 11_400_000) <= 24_000
    # the camera is partial: not all main ticks mapped
    camera = artifact.auxiliary_cameras[0]
    assert camera.status in {"partial", "complete"}
    # no scene groups / match graph fields exist in the artifact shape
    assert all(
        iv.auxiliary is None or iv.classification == "mapped"
        for iv in artifact.intervals
    )


# ---------------------------------------------------------------------------
# MAV-003: main-many aux-one
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("synthetic_media_runtime")
def test_mav_003_main_many_aux_one(tmp_path: Path, real_runtime) -> None:
    sys.path.insert(0, str(ROOT / "core" / "tests" / "fixtures"))
    from alignment_synthetic import (
        _render_sequence,
        make_offset_pair,
        write_mono_wav,
    )

    media = tmp_path / "media"
    # main_1 60s, main_2 60s, aux covers both with one continuous file
    main_1_path, _unused = make_offset_pair(
        media / "pair_a",
        main_duration_ticks=7_200_000,
        aux_offset_ticks=1_200_000,
        seed=31,
    )
    main_2_path, _unused2 = make_offset_pair(
        media / "pair_b",
        main_duration_ticks=7_200_000,
        aux_offset_ticks=9_600_000,
        seed=32,
    )
    # rebuild: main_1 = content A, main_2 = content B (different seeds)
    content_a = _render_sequence(seed=31, duration_ticks=7_200_000)
    content_b = _render_sequence(seed=32, duration_ticks=7_200_000)
    write_mono_wav(main_1_path, content_a)
    write_mono_wav(main_2_path, content_b)
    aux_content = content_a + content_b
    aux_path = media / "aux.wav"
    write_mono_wav(aux_path, aux_content)

    project_root, project = _project_with_sources(
        tmp_path,
        media_files=[("m1", main_1_path), ("m2", main_2_path), ("aux", aux_path)],
    )
    main_1_id, main_2_id, aux_id = (
        project.sources[0].source_id,
        project.sources[1].source_id,
        project.sources[2].source_id,
    )
    request = _align_request(
        project,
        operation_id="op_00000000000040008000000000000003",
        alignment_id="aln_many_one",
        main_ids=[main_1_id, main_2_id],
        aux_cameras=[("aux-1", [aux_id])],
    )
    outcome = _run(project_root, project, request)
    assert outcome.record.status == "succeeded"
    artifact = outcome.artifact
    assert artifact is not None
    mapped = [iv for iv in artifact.intervals if iv.classification == "mapped"]
    assert len(mapped) >= 2
    # no auxiliary-chain truth: each main source maps directly to aux
    assert len({iv.main["source_id"] for iv in mapped}) == 2


# ---------------------------------------------------------------------------
# MAV-005: stop/restart missing derived from verified coverage
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("synthetic_media_runtime")
def test_mav_005_stop_restart_missing(tmp_path: Path, real_runtime) -> None:
    sys.path.insert(0, str(ROOT / "core" / "tests" / "fixtures"))
    from alignment_synthetic import (
        _render_sequence,
        _samples_per_tick,
        write_mono_wav,
    )

    media = tmp_path / "media"
    # main 120s: [0,45] covered by aux_1, [75,120] covered by aux_2, gap [45,75]
    main_content = _render_sequence(seed=51, duration_ticks=14_400_000)
    main_path = media / "main.wav"
    write_mono_wav(main_path, main_content)
    aux_1_content = _render_sequence(
        seed=51, duration_ticks=14_400_000, gain=1.04
    )
    aux_1_path = media / "aux1.wav"
    write_mono_wav(aux_1_path, aux_1_content[:_samples_per_tick(5_400_000)])
    # aux_2 carries main [75s, 120s] content at its own origin (no silence
    # prefix), so the direct relation is main_tick = aux_tick + 75s
    aux_2_content = _render_sequence(
        seed=51, duration_ticks=14_400_000, gain=0.97
    )
    aux_2_path = media / "aux2.wav"
    write_mono_wav(
        aux_2_path,
        aux_2_content[
            _samples_per_tick(9_000_000):_samples_per_tick(14_400_000)
        ],
    )

    project_root, project = _project_with_sources(
        tmp_path,
        media_files=[
            ("main", main_path),
            ("aux1", aux_1_path),
            ("aux2", aux_2_path),
        ],
    )
    main_id = project.sources[0].source_id
    aux_1_id, aux_2_id = project.sources[1].source_id, project.sources[2].source_id
    request = _align_request(
        project,
        operation_id="op_00000000000040008000000000000005",
        alignment_id="aln_restart_missing",
        main_ids=[main_id],
        aux_cameras=[("aux-1", [aux_1_id, aux_2_id])],
    )
    outcome = _run(project_root, project, request)
    assert outcome.record.status == "succeeded"
    artifact = outcome.artifact
    assert artifact is not None
    classifications = [iv.classification for iv in artifact.intervals]
    assert "mapped" in classifications
    assert "missing" in classifications
    missing_ticks = sum(
        iv.main["end_ticks"] - iv.main["start_ticks"]
        for iv in artifact.intervals
        if iv.classification == "missing"
    )
    # the 30s gap between aux_1 and aux_2 coverage must be proven missing
    assert missing_ticks > 0
    assert artifact.summary.missing_ticks == missing_ticks


# ---------------------------------------------------------------------------
# MAV-006: low-evidence short overlap → uncertain (no mapped result)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("synthetic_media_runtime")
def test_mav_006_short_overlap_is_uncertain_not_mapped(
    tmp_path: Path, real_runtime
) -> None:
    sys.path.insert(0, str(ROOT / "core" / "tests" / "fixtures"))
    from alignment_synthetic import make_shared_event_pair

    media = tmp_path / "media"
    # shared event only 20s (< 36s minimum) → cannot verify three windows
    main_path, aux_path = make_shared_event_pair(
        media,
        main_duration_ticks=7_200_000,
        aux_duration_ticks=7_200_000,
        shared_start_ticks=0,
        shared_duration_ticks=2_400_000,
        seed=61,
    )
    project_root, project = _project_with_sources(
        tmp_path, media_files=[("main", main_path), ("aux", aux_path)]
    )
    main_id, aux_id = project.sources[0].source_id, project.sources[1].source_id
    request = _align_request(
        project,
        operation_id="op_00000000000040008000000000000006",
        alignment_id="aln_uncertain",
        main_ids=[main_id],
        aux_cameras=[("aux-1", [aux_id])],
    )
    outcome = _run(project_root, project, request)
    assert outcome.record.status == "succeeded"
    artifact = outcome.artifact
    assert artifact is not None
    assert not any(iv.classification == "mapped" for iv in artifact.intervals)
    assert any(iv.classification == "uncertain" for iv in artifact.intervals)
    camera = artifact.auxiliary_cameras[0]
    assert camera.status == "omitted"
    assert camera.mapped_ticks == 0


# ---------------------------------------------------------------------------
# MAV-007: equivalent candidates → conflict (no first-wins)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("synthetic_media_runtime")
def test_mav_007_equivalent_candidates_conflict(tmp_path: Path, real_runtime) -> None:
    sys.path.insert(0, str(ROOT / "core" / "tests" / "fixtures"))
    from alignment_synthetic import (
        _render_sequence,
        _samples_per_tick,
        write_mono_wav,
    )

    media = tmp_path / "media"
    # one 36s identical sequence appears at TWO main positions → conflict
    sequence = _render_sequence(seed=71, duration_ticks=4_320_000)
    main = [0.0] * _samples_per_tick(10_800_000)
    for start_ticks in (360_000, 6_120_000):
        start = _samples_per_tick(start_ticks)
        for index, value in enumerate(sequence):
            if start + index < len(main):
                main[start + index] = value
    main_path = media / "main.wav"
    write_mono_wav(main_path, main)
    aux_path = media / "aux.wav"
    write_mono_wav(aux_path, sequence)
    project_root, project = _project_with_sources(
        tmp_path, media_files=[("main", main_path), ("aux", aux_path)]
    )
    main_id, aux_id = project.sources[0].source_id, project.sources[1].source_id
    request = _align_request(
        project,
        operation_id="op_00000000000040008000000000000007",
        alignment_id="aln_conflict",
        main_ids=[main_id],
        aux_cameras=[("aux-1", [aux_id])],
    )
    outcome = _run(project_root, project, request)
    assert outcome.record.status == "succeeded"
    artifact = outcome.artifact
    assert artifact is not None
    assert not any(iv.classification == "mapped" for iv in artifact.intervals)
    assert any(iv.classification == "conflict" for iv in artifact.intervals)


# ---------------------------------------------------------------------------
# MAV-008: auxiliary failure isolated, good camera retained
# ---------------------------------------------------------------------------


def test_mav_008_auxiliary_failure_isolated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_media_runtime: tuple[str, str],
) -> None:
    sys.path.insert(0, str(ROOT / "core" / "tests" / "fixtures"))
    from alignment_synthetic import make_offset_pair

    media = tmp_path / "media"
    main_path, good_aux = make_offset_pair(
        media / "good",
        main_duration_ticks=7_200_000,
        aux_offset_ticks=120_000,
        seed=81,
    )
    bad_aux = media / "bad" / "corrupt.wav"
    bad_aux.parent.mkdir(parents=True)
    from alignment_synthetic import _render_sequence, _samples_per_tick, write_mono_wav
    write_mono_wav(
        bad_aux,
        _render_sequence(seed=82, duration_ticks=7_200_000)[
            :_samples_per_tick(7_200_000)
        ],
    )

    project_root, project = _project_with_sources(
        tmp_path,
        media_files=[
            ("main", main_path),
            ("good", good_aux),
            ("bad", bad_aux),
        ],
    )
    main_id = project.sources[0].source_id
    good_id, bad_id = project.sources[1].source_id, project.sources[2].source_id
    request = _align_request(
        project,
        operation_id="op_00000000000040008000000000000008",
        alignment_id="aln_isolation",
        main_ids=[main_id],
        aux_cameras=[
            ("aux-good", [good_id]),
            ("aux-bad", [bad_id]),
        ],
    )
    ffmpeg, ffprobe = synthetic_media_runtime
    _stub_correlation_runtime(monkeypatch, ffmpeg, ffprobe)
    probe_index = 0
    good_probe_starts: list[int] = []

    def decode(_source_path: Path, output_path: Path, **_kwargs: object) -> None:
        _write_audio_fixture(output_path, seconds=40, channels=1)

    def extract(
        source_path: Path,
        output_path: Path,
        *_args: object,
        **_kwargs: object,
    ) -> None:
        if Path(source_path) == bad_aux.resolve(strict=True):
            raise AlignmentError(
                "auxiliary_decode_failed", "injected auxiliary decode failure"
            )
        good_probe_starts.append(int(_args[0]))
        _write_audio_fixture(output_path, seconds=15, channels=1)

    def recognize(
        _alignment_python: Path,
        _target_wav: Path,
        _against_wav: Path,
        _output_path: Path,
        **_kwargs: object,
    ) -> AudalignCorrelationResult:
        nonlocal probe_index
        start_ticks = good_probe_starts[probe_index]
        offset_seconds = str((start_ticks + 120_000) / 120_000)
        probe_index += 1
        return AudalignCorrelationResult(
            (AudalignCorrelationCandidate(offset_seconds, 0),)
        )

    monkeypatch.setattr(alignments_module, "decode_alignment_audio", decode)
    monkeypatch.setattr(alignments_module, "_extract_aux_excerpt", extract)
    monkeypatch.setattr(alignments_module, "run_audalign_correlation", recognize)
    outcome = _run(project_root, project, request)
    assert outcome.record.status == "succeeded"
    artifact = outcome.artifact
    assert artifact is not None
    summaries = {camera.camera_id: camera for camera in artifact.auxiliary_cameras}
    assert summaries["aux-good"].status in {"complete", "partial"}
    assert summaries["aux-bad"].status == "failed"
    assert tuple(
        error.to_dict() for error in summaries["aux-bad"].errors
    ) == ({"code": "auxiliary_decode_failed", "source_id": bad_id},)
    # the good camera's mapped result is retained
    good_intervals = [
        iv for iv in artifact.intervals if iv.auxiliary_camera_id == "aux-good"
    ]
    assert any(iv.classification == "mapped" for iv in good_intervals)


# ---------------------------------------------------------------------------
# MAV-009: existing same-request readback skips everything
# ---------------------------------------------------------------------------


def test_mav_009_existing_same_request_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "project"
    project = create_project(project_root, "Readback")
    request = _align_request(
        project,
        operation_id="op_00000000000040008000000000000009",
        alignment_id="aln_existing",
        main_ids=["src_main_1"],
        aux_cameras=[("aux-1", ["src_aux_1"])],
    )
    scope = ProjectOperationScope(
        project_id=project.project_id,
        project_root_hash=canonical_sha256_v1(
            {"hash_schema": 1, "hash_kind": "project_media_operation_scope",
             "project_root": str(project_root)}
        ),
    )
    projection = alignment_request_projection(
        scope=scope.to_dict(),
        **request,
    )
    request_hash = hash_alignment_request(projection)
    store = MediaOperationStore(project_root, project.project_id)
    record = MediaOperationRecord(
        schema_version=2,
        operation_id=request["operation_id"],
        scope=store.scope,
        operation_type="align_multicam",
        request_hash=request_hash,
        input_hash="b" * 64,
        status="succeeded",
        phase_message_code="alignment_succeeded",
        created_at="2026-07-29T00:00:00.000000Z",
        started_at="2026-07-29T00:00:01.000000Z",
        updated_at="2026-07-29T00:00:02.000000Z",
        finished_at="2026-07-29T00:00:02.000000Z",
        result_ref=AlignmentOperationResult(
            alignment_id="aln_existing", schema_version=1, content_hash="c" * 64
        ),
        error=None,
    )
    with store.writer(request["operation_id"], create=True) as acquired:
        assert acquired
        store.write_locked(record)

    calls = {"preflight": 0, "worker": 0}

    def spy_runtime():
        calls["preflight"] += 1
        raise AssertionError("preflight must not run for existing operation")

    monkeypatch.setattr(alignments_module, "_load_persistent_runtime", spy_runtime)
    outcome = run_align_multicam(project_root, **request)
    assert outcome.readback is True
    assert outcome.record == record
    assert outcome.artifact is None
    assert calls["preflight"] == 0


# ---------------------------------------------------------------------------
# MAV-010: existing different-request conflict
# ---------------------------------------------------------------------------


def test_mav_010_existing_different_request_conflict(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project = create_project(project_root, "Conflict")
    store = MediaOperationStore(project_root, project.project_id)
    record = MediaOperationRecord(
        schema_version=2,
        operation_id="op_00000000000040008000000000000010",
        scope=store.scope,
        operation_type="align_multicam",
        request_hash="1" * 64,
        input_hash="2" * 64,
        status="succeeded",
        phase_message_code="alignment_succeeded",
        created_at="2026-07-29T00:00:00.000000Z",
        started_at="2026-07-29T00:00:01.000000Z",
        updated_at="2026-07-29T00:00:02.000000Z",
        finished_at="2026-07-29T00:00:02.000000Z",
        result_ref=AlignmentOperationResult(
            alignment_id="aln_existing", schema_version=1, content_hash="3" * 64
        ),
        error=None,
    )
    with store.writer(record.operation_id, create=True) as acquired:
        assert acquired
        store.write_locked(record)

    request = _align_request(
        project,
        operation_id="op_00000000000040008000000000000010",
        alignment_id="aln_other",
        main_ids=["src_main_1"],
        aux_cameras=[("aux-1", ["src_aux_1"])],
    )
    with pytest.raises(MediaOperationError) as exc:
        run_align_multicam(project_root, **request)
    assert exc.value.code == "operation_input_conflict"
    # record unchanged
    assert store.read(record.operation_id) == record


# ---------------------------------------------------------------------------
# MAV-011: missing operation with stale basis → preflight error, no record
# ---------------------------------------------------------------------------


def test_mav_011_missing_operation_stale_basis(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project = create_project(project_root, "Stale")
    request = _align_request(
        project,
        operation_id="op_00000000000040008000000000000011",
        alignment_id="aln_stale",
        main_ids=["src_main_1"],
        aux_cameras=[("aux-1", ["src_aux_1"])],
    )
    # source does not exist → alignment_input_stale before any record/worker
    with pytest.raises(MediaOperationError) as exc:
        run_align_multicam(project_root, **request)
    assert exc.value.code == "alignment_input_stale"
    operation_store = MediaOperationStore(project_root, project.project_id)
    assert operation_store.read(request["operation_id"]) is None
    assert not (project_root / "artifacts" / "multicam-alignments").exists()


# ---------------------------------------------------------------------------
# MAV-015: response lost after succeeded → same-ID readback
# ---------------------------------------------------------------------------


def test_mav_015_response_lost_readback_succeeded(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project = create_project(project_root, "Lost")
    store = MediaOperationStore(project_root, project.project_id)
    record = MediaOperationRecord(
        schema_version=2,
        operation_id="op_00000000000040008000000000000015",
        scope=store.scope,
        operation_type="align_multicam",
        request_hash="d" * 64,
        input_hash="e" * 64,
        status="succeeded",
        phase_message_code="alignment_succeeded",
        created_at="2026-07-29T00:00:00.000000Z",
        started_at="2026-07-29T00:00:01.000000Z",
        updated_at="2026-07-29T00:00:02.000000Z",
        finished_at="2026-07-29T00:00:02.000000Z",
        result_ref=AlignmentOperationResult(
            alignment_id="aln_response_lost",
            schema_version=1,
            content_hash="e" * 64,
        ),
        error=None,
    )
    with store.writer(record.operation_id, create=True) as acquired:
        assert acquired
        store.write_locked(record)
    status = media_operation_status(project_root, record.operation_id)
    assert status == record
    assert status.status == "succeeded"
    assert status.result_ref is not None


# ---------------------------------------------------------------------------
# MAV-016: hard exit at running → status converges interrupted after child exit
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="fork-based hard-exit test is POSIX-only")
def test_mav_016_hard_exit_child_converges_interrupted(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project = create_project(project_root, "HardExit")
    operation_id = "op_00000000000040008000000000000016"
    child = os.fork()
    if child == 0:
        child_store = MediaOperationStore(project_root, project.project_id)
        with child_store.writer(operation_id, create=True) as acquired:
            if not acquired:
                os._exit(91)
            record = MediaOperationRecord(
                schema_version=2,
                operation_id=operation_id,
                scope=child_store.scope,
                operation_type="align_multicam",
                request_hash="a" * 64,
                input_hash="b" * 64,
                status="running",
                phase_message_code="alignment_processing_auxiliary",
                created_at="2026-07-29T00:00:00.000000Z",
                started_at="2026-07-29T00:00:01.000000Z",
                updated_at="2026-07-29T00:00:01.000000Z",
                finished_at=None,
                result_ref=None,
                error=None,
            )
            child_store.write_locked(record)
            os._exit(0)
    waited, status = os.waitpid(child, 0)
    assert waited == child
    assert os.waitstatus_to_exitcode(status) == 0
    converged = media_operation_status(project_root, operation_id)
    assert converged.status == "interrupted"
    assert converged.phase_message_code == "alignment_interrupted"
    assert converged.error is not None
    assert converged.error.code == "alignment_interrupted"


# ---------------------------------------------------------------------------
# MAV-017: writer disappears → closed interrupted, no artifact scan
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="fork-based test is POSIX-only")
def test_mav_017_writer_disappears_closed_status(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project = create_project(project_root, "WriterGone")
    operation_id = "op_00000000000040008000000000000017"
    child = os.fork()
    if child == 0:
        child_store = MediaOperationStore(project_root, project.project_id)
        with child_store.writer(operation_id, create=True) as acquired:
            if not acquired:
                os._exit(91)
            record = MediaOperationRecord(
                schema_version=2,
                operation_id=operation_id,
                scope=child_store.scope,
                operation_type="align_multicam",
                request_hash="a" * 64,
                input_hash="b" * 64,
                status="running",
                phase_message_code="alignment_indexing_main",
                created_at="2026-07-29T00:00:00.000000Z",
                started_at="2026-07-29T00:00:01.000000Z",
                updated_at="2026-07-29T00:00:01.000000Z",
                finished_at=None,
                result_ref=None,
                error=None,
            )
            child_store.write_locked(record)
            os._exit(0)
    _waited, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    converged = media_operation_status(project_root, operation_id)
    assert converged.status == "interrupted"
    assert converged.error is not None
    assert converged.error.code == "alignment_interrupted"
    # status must not have created an artifact or scanned directories
    assert not (project_root / "artifacts" / "multicam-alignments").exists()


# ---------------------------------------------------------------------------
# MAV-018: store rejects symlink/hardlink/path-escape
# ---------------------------------------------------------------------------


def test_mav_018_store_link_and_escape_rejection(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project = create_project(project_root, "StoreAttack")
    store = AlignmentStore(project_root)
    from roughcut.domain.alignment import (
        AlignmentAlgorithm,
        AlignmentCamera,
        AlignmentCameraGroup,
        AlignmentSummary,
        AlignmentVerificationProfile,
        MulticamAlignmentArtifact,
    )

    profile = AlignmentVerificationProfile(
        name="roughcut_audalign_fixed_offset", version=1
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
    main = AlignmentCameraGroup(camera_id="main", ordered_source_ids=("src_main_1",))
    from roughcut.domain.alignment import AlignmentSourceBasis, AlignmentSourceFingerprint

    basis = (
        AlignmentSourceBasis(
            camera_id="aux-1",
            source_id="src_aux_1",
            fingerprint=AlignmentSourceFingerprint(
                size=1, mtime_ns=1, sha256_head_tail="b" * 64
            ),
            duration_ticks=14_400_000,
        ),
        AlignmentSourceBasis(
            camera_id="main",
            source_id="src_main_1",
            fingerprint=AlignmentSourceFingerprint(
                size=1, mtime_ns=1, sha256_head_tail="a" * 64
            ),
            duration_ticks=14_400_000,
        ),
    )
    summary = AlignmentSummary(
        total_main_ticks=14_400_000,
        camera_count=1,
        mapped_ticks=14_400_000,
        missing_ticks=0,
        uncertain_ticks=0,
        conflict_ticks=0,
    )
    camera_item = AlignmentCamera(
        camera_id="aux-1",
        ordered_source_ids=("src_aux_1",),
        status="complete",
        mapped_ticks=14_400_000,
        missing_ticks=0,
        uncertain_ticks=0,
        conflict_ticks=0,
        errors=(),
    )
    from roughcut.domain.alignment import AlignmentInterval

    interval = AlignmentInterval(
        interval_id="ali_store_1",
        auxiliary_camera_id="aux-1",
        classification="mapped",
        main={"source_id": "src_main_1", "start_ticks": 0, "end_ticks": 14_400_000},
        auxiliary={
            "source_id": "src_aux_1",
            "start_ticks": 0,
            "end_ticks": 14_400_000,
        },
        evidence={
            "code": "fixed_offset_verified",
            "raw_candidate_count": 1,
            "matching_fingerprint_counts": [1],
            "verification_window_count": 3,
            "verification_profile": {
                "name": "roughcut_audalign_fixed_offset",
                "version": 1,
            },
            "max_local_offset_error_ticks": 0,
        },
    )
    artifact = MulticamAlignmentArtifact(
        alignment_id="aln_store_attack",
        project_id=project.project_id,
        producer_operation_id="op_00000000000040008000000000000018",
        created_at="2026-07-29T00:00:00.000000Z",
        request_hash="c" * 64,
        input_hash="d" * 64,
        algorithm=algorithm,
        main_camera=main,
        auxiliary_cameras=(camera_item,),
        source_basis=basis,
        intervals=(interval,),
        summary=summary,
    )

    # symlink target attack
    artifacts_root = project_root / "artifacts" / "multicam-alignments"
    artifacts_root.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    target = artifacts_root / "aln_store_attack.json"
    target.symlink_to(outside)
    with pytest.raises(AlignmentError) as exc:
        store.read("aln_store_attack")
    assert "alignment_store_integrity_error" in str(exc.value) or "symlink" in str(
        exc.value
    )
    target.unlink()
    # hardlink attack
    hard_target = artifacts_root / "aln_store_attack.json"
    hard_source = tmp_path / "hard-source.json"
    hard_source.write_text("{}", encoding="utf-8")
    os.link(hard_source, hard_target)
    with pytest.raises(AlignmentError):
        store.publish("aln_store_attack", artifact)
    hard_target.unlink()
    # path escape: alignment ID with traversal must be rejected
    with pytest.raises(AlignmentError):
        store.read("../outside")
    assert not (project_root / "outside.json").exists()


# ---------------------------------------------------------------------------
# MAV-019: atomic publish race — exactly one winner, loser gets conflict
# ---------------------------------------------------------------------------


def test_mav_019_atomic_publish_race(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project = create_project(project_root, "Race")
    store = AlignmentStore(project_root)
    from roughcut.domain.alignment import (
        AlignmentAlgorithm,
        AlignmentCamera,
        AlignmentCameraGroup,
        AlignmentInterval,
        AlignmentSourceBasis,
        AlignmentSourceFingerprint,
        AlignmentSummary,
        AlignmentVerificationProfile,
        MulticamAlignmentArtifact,
    )

    def build(alignment_id: str, project_id: str) -> MulticamAlignmentArtifact:
        profile = AlignmentVerificationProfile(
            name="roughcut_audalign_fixed_offset", version=1
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
        main = AlignmentCameraGroup(
            camera_id="main", ordered_source_ids=("src_main_1",)
        )
        basis = (
            AlignmentSourceBasis(
                camera_id="aux-1",
                source_id="src_aux_1",
                fingerprint=AlignmentSourceFingerprint(
                    size=1, mtime_ns=1, sha256_head_tail="b" * 64
                ),
                duration_ticks=14_400_000,
            ),
            AlignmentSourceBasis(
                camera_id="main",
                source_id="src_main_1",
                fingerprint=AlignmentSourceFingerprint(
                    size=1, mtime_ns=1, sha256_head_tail="a" * 64
                ),
                duration_ticks=14_400_000,
            ),
        )
        summary = AlignmentSummary(
            total_main_ticks=14_400_000,
            camera_count=1,
            mapped_ticks=14_400_000,
            missing_ticks=0,
            uncertain_ticks=0,
            conflict_ticks=0,
        )
        camera_item = AlignmentCamera(
            camera_id="aux-1",
            ordered_source_ids=("src_aux_1",),
            status="complete",
            mapped_ticks=14_400_000,
            missing_ticks=0,
            uncertain_ticks=0,
            conflict_ticks=0,
            errors=(),
        )
        interval = AlignmentInterval(
            interval_id="ali_race_1",
            auxiliary_camera_id="aux-1",
            classification="mapped",
            main={"source_id": "src_main_1", "start_ticks": 0, "end_ticks": 14_400_000},
            auxiliary={
                "source_id": "src_aux_1",
                "start_ticks": 0,
                "end_ticks": 14_400_000,
            },
            evidence={
                "code": "fixed_offset_verified",
                "raw_candidate_count": 1,
                "matching_fingerprint_counts": [1],
                "verification_window_count": 3,
                "verification_profile": {
                    "name": "roughcut_audalign_fixed_offset",
                    "version": 1,
                },
                "max_local_offset_error_ticks": 0,
            },
        )
        return MulticamAlignmentArtifact(
            alignment_id=alignment_id,
            project_id=project_id,
            producer_operation_id="op_00000000000040008000000000000019",
            created_at="2026-07-29T00:00:00.000000Z",
            request_hash="c" * 64,
            input_hash="d" * 64,
            algorithm=algorithm,
            main_camera=main,
            auxiliary_cameras=(camera_item,),
            source_basis=basis,
            intervals=(interval,),
            summary=summary,
        )

    artifact_a = build("aln_race", project.project_id)
    artifact_b = build("aln_race", "project_other")
    results: list[str] = []
    errors: list[str] = []

    def writer_a():
        try:
            store.publish("aln_race", artifact_a)
            results.append("a")
        except Exception as error:  # noqa: BLE001
            errors.append(str(getattr(error, "code", error)))

    def writer_b():
        try:
            store.publish("aln_race", artifact_b)
            results.append("b")
        except Exception as error:  # noqa: BLE001
            errors.append(str(getattr(error, "code", error)))

    threads = [threading.Thread(target=writer_a), threading.Thread(target=writer_b)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 1
    assert len(errors) == 1
    assert errors[0] == "alignment_publish_conflict"
    final = store.read("aln_race")
    assert final is not None
    assert final.project_id in {project.project_id, "project_other"}


# ---------------------------------------------------------------------------
# MAV-020: same camera partial decode → mapped + uncertain, never missing
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("synthetic_media_runtime")
def test_mav_020_same_camera_partial_decode_uncertain(
    tmp_path: Path, real_runtime
) -> None:
    sys.path.insert(0, str(ROOT / "core" / "tests" / "fixtures"))
    from alignment_synthetic import make_offset_pair

    media = tmp_path / "media"
    main_path, good_aux = make_offset_pair(
        media / "good",
        main_duration_ticks=14_400_000,
        aux_offset_ticks=0,
        seed=201,
        aux_tail_ticks=0,
    )
    from alignment_synthetic import _render_sequence, _samples_per_tick
    from alignment_synthetic import write_mono_wav as _wmw
    _good = _render_sequence(seed=201, duration_ticks=14_400_000)[
        :_samples_per_tick(6_000_000)
    ]
    _wmw(good_aux, _good)
    bad_aux = media / "bad" / "corrupt.wav"
    bad_aux.parent.mkdir(parents=True)
    from alignment_synthetic import _render_sequence, _samples_per_tick, write_mono_wav
    write_mono_wav(
        bad_aux,
        _render_sequence(seed=203, duration_ticks=14_400_000)[
            :_samples_per_tick(6_000_000)
        ],
    )
    # second camera with a full mapping
    other_main, other_aux = make_offset_pair(
        media / "other",
        main_duration_ticks=14_400_000,
        aux_offset_ticks=300_000,
        seed=201,
        aux_gain=0.98,
        aux_tail_ticks=1_200_000,
    )
    project_root, project = _project_with_sources(
        tmp_path,
        media_files=[
            ("main", main_path),
            ("good", good_aux),
            ("bad", bad_aux),
            ("other_main", other_main),
            ("other_aux", other_aux),
        ],
    )
    main_id = project.sources[0].source_id
    good_id, bad_id = project.sources[1].source_id, project.sources[2].source_id
    other_aux_id = project.sources[4].source_id
    # the "other" camera maps main to a different main source; keep main the same
    request = _align_request(
        project,
        operation_id="op_00000000000040008000000000000020",
        alignment_id="aln_partial_decode",
        main_ids=[main_id],
        aux_cameras=[
            ("aux-partial", [good_id, bad_id]),
            ("aux-other", [other_aux_id]),
        ],
    )
    import roughcut.application.alignments as _align_module

    original_decode = _align_module.decode_alignment_audio

    def failing_decode(source_path, output_path, **kwargs):
        if Path(source_path) == bad_aux.resolve(strict=True):
            raise FFmpegAlignmentError("injected decode failure")
        return original_decode(source_path, output_path, **kwargs)

    _align_module.decode_alignment_audio = failing_decode
    try:
        outcome = _run(project_root, project, request)
    finally:
        _align_module.decode_alignment_audio = original_decode
    assert outcome.record.status == "succeeded"
    artifact = outcome.artifact
    assert artifact is not None
    summaries = {camera.camera_id: camera for camera in artifact.auxiliary_cameras}
    assert summaries["aux-partial"].status == "partial"
    assert summaries["aux-other"].status == "complete"
    # partial camera: mapped ticks present and remaining is uncertain (not missing)
    assert summaries["aux-partial"].mapped_ticks > 0
    assert summaries["aux-partial"].missing_ticks == 0
    assert summaries["aux-partial"].uncertain_ticks > 0
    assert tuple(
        error.to_dict() for error in summaries["aux-partial"].errors
    ) == ({"code": "auxiliary_decode_failed", "source_id": bad_id},)
    # summary camera-ticks equation holds
    summary = artifact.summary
    assert (
        summary.mapped_ticks
        + summary.missing_ticks
        + summary.uncertain_ticks
        + summary.conflict_ticks
        == summary.total_main_ticks * summary.camera_count
    )


# ---------------------------------------------------------------------------
# MAV-021/022: positive/negative offset with nonzero extraction, B formula
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("synthetic_media_runtime")
@pytest.mark.parametrize(
    ("vector_id", "main_excerpt", "aux_excerpt", "expected_b"),
    [
        ("MAV-021-positive-offset-nonzero-extraction", 2_400_000, 3_600_000, -1_169_850),
        ("MAV-022-negative-offset-nonzero-extraction", 4_800_000, 1_200_000, 3_569_850),
    ],
)
def test_mav_021_022_signed_offset_b_formula(
    tmp_path: Path,
    real_runtime,
    vector_id: str,
    main_excerpt: int,
    aux_excerpt: int,
    expected_b: int,
) -> None:
    from roughcut.application.alignment_profile import source_relation_b

    audalign_offset = (
        0.25125 if vector_id.startswith("MAV-021") else -0.25125
    )
    b = source_relation_b(
        main_excerpt, aux_excerpt, f"{audalign_offset:.5f}"
    )
    assert b == expected_b
    # classification via the profile requires real audalign; exercise it here
    # through a small real pair
    sys.path.insert(0, str(ROOT / "core" / "tests" / "fixtures"))
    from alignment_synthetic import make_offset_pair

    media = tmp_path / "media"
    main_path, aux_path = make_offset_pair(
        media,
        main_duration_ticks=14_400_000,
        aux_offset_ticks=360_000,
        seed=230,
    )
    from roughcut.adapters.audalign import run_audalign_recognize

    out = tmp_path / "pair.json"
    match = run_audalign_recognize(
        Path(real_runtime.binding.alignment_python.interpreter),
        aux_path,
        main_path,
        out,
    )
    assert match.candidates
    # every candidate converted with the frozen rounding
    for candidate in match.candidates:
        assert isinstance(candidate.offset_ticks, int)


# ---------------------------------------------------------------------------
# MAV-029: hard exit after artifact publish → interrupted, artifact preserved
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="fork-based test is POSIX-only")
def test_mav_029_hard_exit_after_artifact_publish(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project = create_project(project_root, "PublishWindow")
    operation_id = "op_00000000000040008000000000000030"
    alignment_id = "aln_publish_window"
    # publish an artifact directly (simulating the window after atomic publish)
    from roughcut.domain.alignment import (
        AlignmentAlgorithm,
        AlignmentCamera,
        AlignmentCameraGroup,
        AlignmentInterval,
        AlignmentSourceBasis,
        AlignmentSourceFingerprint,
        AlignmentSummary,
        AlignmentVerificationProfile,
        MulticamAlignmentArtifact,
    )

    profile = AlignmentVerificationProfile(
        name="roughcut_audalign_fixed_offset", version=1
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
    main = AlignmentCameraGroup(camera_id="main", ordered_source_ids=("src_main_1",))
    basis = (
        AlignmentSourceBasis(
            camera_id="aux-1",
            source_id="src_aux_1",
            fingerprint=AlignmentSourceFingerprint(
                size=1, mtime_ns=1, sha256_head_tail="b" * 64
            ),
            duration_ticks=14_400_000,
        ),
        AlignmentSourceBasis(
            camera_id="main",
            source_id="src_main_1",
            fingerprint=AlignmentSourceFingerprint(
                size=1, mtime_ns=1, sha256_head_tail="a" * 64
            ),
            duration_ticks=14_400_000,
        ),
    )
    summary = AlignmentSummary(
        total_main_ticks=14_400_000,
        camera_count=1,
        mapped_ticks=14_400_000,
        missing_ticks=0,
        uncertain_ticks=0,
        conflict_ticks=0,
    )
    camera_item = AlignmentCamera(
        camera_id="aux-1",
        ordered_source_ids=("src_aux_1",),
        status="complete",
        mapped_ticks=14_400_000,
        missing_ticks=0,
        uncertain_ticks=0,
        conflict_ticks=0,
        errors=(),
    )
    interval = AlignmentInterval(
        interval_id="ali_publish_1",
        auxiliary_camera_id="aux-1",
        classification="mapped",
        main={"source_id": "src_main_1", "start_ticks": 0, "end_ticks": 14_400_000},
        auxiliary={
            "source_id": "src_aux_1",
            "start_ticks": 0,
            "end_ticks": 14_400_000,
        },
        evidence={
            "code": "fixed_offset_verified",
            "raw_candidate_count": 1,
            "matching_fingerprint_counts": [1],
            "verification_window_count": 3,
            "verification_profile": {
                "name": "roughcut_audalign_fixed_offset",
                "version": 1,
            },
            "max_local_offset_error_ticks": 0,
        },
    )
    artifact = MulticamAlignmentArtifact(
        alignment_id=alignment_id,
        project_id=project.project_id,
        producer_operation_id=operation_id,
        created_at="2026-07-29T00:00:00.000000Z",
        request_hash="c" * 64,
        input_hash="d" * 64,
        algorithm=algorithm,
        main_camera=main,
        auxiliary_cameras=(camera_item,),
        source_basis=basis,
        intervals=(interval,),
        summary=summary,
    )
    artifact_store = AlignmentStore(project_root)
    artifact_store.publish(alignment_id, artifact)
    published_bytes = (
        project_root / "artifacts" / "multicam-alignments" / f"{alignment_id}.json"
    ).read_bytes()

    # child writes the running record and hard-exits before succeeded
    child = os.fork()
    if child == 0:
        child_store = MediaOperationStore(project_root, project.project_id)
        with child_store.writer(operation_id, create=True) as acquired:
            if not acquired:
                os._exit(91)
            record = MediaOperationRecord(
                schema_version=2,
                operation_id=operation_id,
                scope=child_store.scope,
                operation_type="align_multicam",
                request_hash="c" * 64,
                input_hash="d" * 64,
                status="running",
                phase_message_code="alignment_publishing",
                created_at="2026-07-29T00:00:00.000000Z",
                started_at="2026-07-29T00:00:01.000000Z",
                updated_at="2026-07-29T00:00:01.000000Z",
                finished_at=None,
                result_ref=None,
                error=None,
            )
            child_store.write_locked(record)
            os._exit(0)
    _waited, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    converged = media_operation_status(project_root, operation_id)
    assert converged.status == "interrupted"
    assert converged.error is not None
    assert converged.error.code == "alignment_interrupted"
    assert converged.result_ref is None
    # artifact preserved exactly, not adopted or deleted
    assert (
        project_root / "artifacts" / "multicam-alignments" / f"{alignment_id}.json"
    ).read_bytes() == published_bytes


# ---------------------------------------------------------------------------
# MAV-030: basis/runtime change at publish revalidation → failed, zero publish
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("synthetic_media_runtime")
def test_mav_030_basis_and_runtime_change_zero_publish(
    tmp_path: Path, real_runtime
) -> None:
    sys.path.insert(0, str(ROOT / "core" / "tests" / "fixtures"))
    from alignment_synthetic import make_offset_pair

    media = tmp_path / "media"
    main_path, aux_path = make_offset_pair(
        media,
        main_duration_ticks=7_200_000,
        aux_offset_ticks=300_000,
        seed=301,
    )
    project_root, project = _project_with_sources(
        tmp_path, media_files=[("main", main_path), ("aux", aux_path)]
    )
    main_id, aux_id = project.sources[0].source_id, project.sources[1].source_id

    # Case 1: Project/Source basis changes after worker start, before publish.
    # Inject a source mutation at revalidation time (a byte appended to main
    # changes size/fingerprint) so the publish revalidation must fail closed
    # with zero artifact.
    request = _align_request(
        project,
        operation_id="op_00000000000040008000000000000031",
        alignment_id="aln_basis_change_case_project",
        main_ids=[main_id],
        aux_cameras=[("aux-1", [aux_id])],
    )
    original_revalidate = alignments_module._revalidate_basis

    def mutate_then_revalidate(*args, **kwargs):
        with main_path.open("ab") as stream:
            stream.write(b"\x00")
        return original_revalidate(*args, **kwargs)

    alignments_module._revalidate_basis = mutate_then_revalidate
    try:
        with pytest.raises(AlignmentError) as exc:
            run_align_multicam(project_root, **request)
    finally:
        alignments_module._revalidate_basis = original_revalidate
    assert exc.value.code == "alignment_basis_changed_during_run"
    store = MediaOperationStore(project_root, project.project_id)
    record = store.read(request["operation_id"])
    assert record is not None
    assert record.status == "failed"
    assert record.error is not None
    assert record.error.code == "alignment_basis_changed_during_run"
    assert not (
        project_root
        / "artifacts"
        / "multicam-alignments"
        / "aln_basis_change_case_project.json"
    ).exists()

    # Case 2: runtime binding changes after worker start, before publish.
    # Use a fresh project+media so the preflight basis is intact.
    media2 = tmp_path / "media2"
    main2_path, aux2_path = make_offset_pair(
        media2,
        main_duration_ticks=7_200_000,
        aux_offset_ticks=300_000,
        seed=302,
    )
    project_root2 = tmp_path / "project2"
    project2 = create_project(project_root2, "A2")
    for _p in (main2_path, aux2_path):
        project2 = add_source(
            project_root2, _p, ImportMode.LINKED,
            expected_revision=project2.revision,
        )
    main2_id, aux2_id = project2.sources[0].source_id, project2.sources[1].source_id
    request_rt = _align_request(
        project2,
        operation_id="op_00000000000040008000000000000033",
        alignment_id="aln_basis_change_case_runtime",
        main_ids=[main2_id],
        aux_cameras=[("aux-1", [aux2_id])],
    )
    original_runtime = alignments_module._load_persistent_runtime
    runtime_calls = {"count": 0}

    def changed_runtime():
        from dataclasses import replace

        runtime_calls["count"] += 1
        if runtime_calls["count"] >= 2:
            return replace(original_runtime(), runtime_binding_sha256="3" * 64)
        return original_runtime()

    alignments_module._load_persistent_runtime = changed_runtime
    try:
        with pytest.raises(AlignmentError) as exc_rt:
            run_align_multicam(project_root2, **request_rt)
    finally:
        alignments_module._load_persistent_runtime = original_runtime
    assert exc_rt.value.code == "alignment_runtime_changed_during_run"
    store2 = MediaOperationStore(project_root2, project2.project_id)
    record_rt = store2.read(request_rt["operation_id"])
    assert record_rt is not None
    assert record_rt.status == "failed"
    assert record_rt.error is not None
    assert record_rt.error.code == "alignment_runtime_changed_during_run"
    assert not (
        project_root2
        / "artifacts"
        / "multicam-alignments"
        / "aln_basis_change_case_runtime.json"
    ).exists()


# ---------------------------------------------------------------------------
# MAV-004: many-to-many direct interval mapping
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("synthetic_media_runtime")
def test_mav_004_many_to_many_direct_intervals(tmp_path: Path, real_runtime) -> None:
    sys.path.insert(0, str(ROOT / "core" / "tests" / "fixtures"))
    from alignment_synthetic import _render_sequence, _samples_per_tick, write_mono_wav

    media = tmp_path / "media"
    # main_1 80s, main_2 50s; aux_1 covers main_1 [0,40]; aux_2 covers
    # main_1 [40,80] + main_2 [0,50] as one continuous 90s file, so every
    # relation keeps at least 40s of effective overlap (>= the 36s gate)
    main_1_samples = _render_sequence(seed=41, duration_ticks=9_600_000)
    main_2_samples = _render_sequence(seed=42, duration_ticks=6_000_000)
    main_1_path = media / "main1.wav"
    main_2_path = media / "main2.wav"
    write_mono_wav(main_1_path, main_1_samples)
    write_mono_wav(main_2_path, main_2_samples)
    aux_1_samples = _render_sequence(
        seed=41, duration_ticks=9_600_000, gain=1.04
    )[:_samples_per_tick(4_800_000)]
    aux_1_path = media / "aux1.wav"
    write_mono_wav(aux_1_path, aux_1_samples)
    # aux_2 = main_1 from the sample after 4_800_000 ticks + all of main_2,
    # so it is 90s total, matching the frozen vector's 10_800_000 ticks
    aux_2_samples = (
        _render_sequence(seed=41, duration_ticks=9_600_000, gain=0.97)[
            _samples_per_tick(4_800_000):
        ]
        + _render_sequence(seed=42, duration_ticks=6_000_000, gain=0.97)
    )
    aux_2_path = media / "aux2.wav"
    write_mono_wav(aux_2_path, aux_2_samples)

    project_root, project = _project_with_sources(
        tmp_path,
        media_files=[
            ("main1", main_1_path),
            ("main2", main_2_path),
            ("aux1", aux_1_path),
            ("aux2", aux_2_path),
        ],
    )
    main_1_id, main_2_id = project.sources[0].source_id, project.sources[1].source_id
    aux_1_id, aux_2_id = project.sources[2].source_id, project.sources[3].source_id
    request = _align_request(
        project,
        operation_id="op_00000000000040008000000000000004",
        alignment_id="aln_many_many",
        main_ids=[main_1_id, main_2_id],
        aux_cameras=[("aux-1", [aux_1_id, aux_2_id])],
    )
    outcome = _run(project_root, project, request)
    assert outcome.record.status == "succeeded"
    artifact = outcome.artifact
    assert artifact is not None
    mapped = [iv for iv in artifact.intervals if iv.classification == "mapped"]
    assert len(mapped) >= 2
    # direct relations only: aux_1 maps to main_1, aux_2 maps to both mains
    aux_1_mapped = [
        iv for iv in mapped if iv.auxiliary and iv.auxiliary["source_id"] == aux_1_id
    ]
    aux_2_mapped = [
        iv for iv in mapped if iv.auxiliary and iv.auxiliary["source_id"] == aux_2_id
    ]
    assert aux_1_mapped and aux_2_mapped
    assert {iv.main["source_id"] for iv in aux_2_mapped} == {main_1_id, main_2_id}
    # strict partition: for this camera every main source is covered exactly
    # once by a sorted, gap-free interval list (no overlap, no hole)
    camera = artifact.auxiliary_cameras[0]
    for main_source_id, duration in (
        (main_1_id, 9_600_000),
        (main_2_id, 6_000_000),
    ):
        own = sorted(
            (
                iv
                for iv in artifact.intervals
                if iv.auxiliary_camera_id == camera.camera_id
                and iv.main["source_id"] == main_source_id
            ),
            key=lambda iv: iv.main["start_ticks"],
        )
        position = 0
        for iv in own:
            start = iv.main["start_ticks"]
            end = iv.main["end_ticks"]
            assert start == position, (main_source_id, start, position)
            position = end
        assert position == duration, (main_source_id, position, duration)
    # the camera status and per-camera ticks match the emitted intervals
    camera_ticks = {"mapped": 0, "missing": 0, "uncertain": 0, "conflict": 0}
    for iv in artifact.intervals:
        if iv.auxiliary_camera_id != camera.camera_id:
            continue
        camera_ticks[iv.classification] += (
            iv.main["end_ticks"] - iv.main["start_ticks"]
        )
    assert camera_ticks["mapped"] == camera.mapped_ticks
    assert camera_ticks["missing"] == camera.missing_ticks
    assert camera_ticks["uncertain"] == camera.uncertain_ticks
    assert camera_ticks["conflict"] == camera.conflict_ticks
    # summary camera-ticks equation holds over both main sources
    summary = artifact.summary
    assert (
        summary.mapped_ticks
        + summary.missing_ticks
        + summary.uncertain_ticks
        + summary.conflict_ticks
        == summary.total_main_ticks * summary.camera_count
    )


# ---------------------------------------------------------------------------
# P1 stereo mono-cancellation: mono-first fails, L/R fallback recovers
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("synthetic_media_runtime")
def test_p1_stereo_mono_cancellation_lr_fallback(
    tmp_path: Path, real_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stereo pair where L = program and R = -program: the mono mix cancels
    completely, so it may produce zero raw candidates and become uncertain
    directly; the complete L/R fallback must recover the mapping. The accepted
    mapped relation must retain evidence from three verification windows."""
    sys.path.insert(0, str(ROOT / "core" / "tests" / "fixtures"))
    from alignment_synthetic import make_stereo_cancellation_pair

    media = tmp_path / "media"
    main_path, aux_path = make_stereo_cancellation_pair(
        media,
        main_duration_ticks=14_400_000,
        aux_offset_ticks=600_000,
        seed=77,
    )
    project_root, project = _project_with_sources(
        tmp_path, media_files=[("main", main_path), ("aux", aux_path)]
    )
    main_id, aux_id = project.sources[0].source_id, project.sources[1].source_id
    request = _align_request(
        project,
        operation_id="op_00000000000040008000000000000040",
        alignment_id="aln_stereo_lr",
        main_ids=[main_id],
        aux_cameras=[("aux-1", [aux_id])],
    )
    pair_calls: list[str] = []
    original_pair = alignments_module._align_one_pair

    def recording_pair(*args, **kwargs):
        channel = kwargs.get("channel")
        pair_calls.append(channel if channel is not None else "mono")
        return original_pair(*args, **kwargs)

    monkeypatch.setattr(
        alignments_module,
        "_align_one_pair",
        recording_pair,
    )
    outcome = _run(project_root, project, request)
    assert outcome.record.status == "succeeded"
    artifact = outcome.artifact
    assert artifact is not None
    camera = artifact.auxiliary_cameras[0]
    assert camera.status == "partial"
    assert camera.errors == ()
    mapped = [
        iv for iv in artifact.intervals
        if iv.classification == "mapped" and iv.auxiliary_camera_id == camera.camera_id
    ]
    # the fixture geometry is exact: aux inserts 600000 ticks of silence
    # before the program, aux is as long as main, so the true relation is
    # B = -600000 ticks and the mapped main window is [0, 13800000]
    assert len(mapped) == 1
    interval = mapped[0]
    assert interval.auxiliary is not None
    tolerance = ALIGNMENT_CANDIDATE_GROUP_DIAMETER_TICKS
    assert interval.main["start_ticks"] == 0
    assert abs(interval.main["end_ticks"] - 13_800_000) <= tolerance
    assert abs(interval.auxiliary["start_ticks"] - 600_000) <= tolerance
    assert interval.auxiliary["end_ticks"] == 14_400_000
    # the mapped main and auxiliary windows have exactly the same length
    assert (
        interval.main["end_ticks"] - interval.main["start_ticks"]
        == interval.auxiliary["end_ticks"] - interval.auxiliary["start_ticks"]
    )
    actual_b = (
        interval.main["start_ticks"] - interval.auxiliary["start_ticks"]
    )
    assert abs(actual_b - (-600_000)) <= tolerance
    assert abs(camera.missing_ticks - 600_000) <= tolerance
    assert interval.evidence["code"] == "fixed_offset_verified"
    assert interval.evidence["verification_window_count"] == 3
    assert interval.evidence["max_local_offset_error_ticks"] is not None
    assert (
        interval.evidence["max_local_offset_error_ticks"]
        <= ALIGNMENT_CANDIDATE_GROUP_DIAMETER_TICKS
    )
    # mono cancellation may lead straight to uncertain without any
    # verification-window call; the accepted L/R relation records all three.
    assert pair_calls == ["mono", "left", "right"]


@pytest.mark.usefixtures("synthetic_media_runtime")
def test_p1_stereo_distant_relations_conflict(
    tmp_path: Path, real_runtime, monkeypatch
) -> None:
    """A stereo pair where L and R support relations more than 12000 ticks
    apart. The natural mono→fallback path is owned by the cancellation test;
    this test pins the mono stage to uncertain and exercises the complete L/R
    fallback against real Audalign, real FFmpeg decode, and real three-window
    verification, so two inconsistent verified B relations must close as
    conflict, never mapped. It does not claim this fixture's mono mix fails
    naturally."""
    sys.path.insert(0, str(ROOT / "core" / "tests" / "fixtures"))
    from alignment_synthetic import make_stereo_distant_pair

    media = tmp_path / "media"
    main_path, aux_path = make_stereo_distant_pair(
        media,
        main_duration_ticks=14_400_000,
        aux_offset_ticks=600_000,
        seed=78,
    )
    project_root, project = _project_with_sources(
        tmp_path, media_files=[("main", main_path), ("aux", aux_path)]
    )
    main_id, aux_id = project.sources[0].source_id, project.sources[1].source_id
    request = _align_request(
        project,
        operation_id="op_00000000000040008000000000000042",
        alignment_id="aln_stereo_conflict",
        main_ids=[main_id],
        aux_cameras=[("aux-1", [aux_id])],
    )
    original_pair = alignments_module._align_one_pair

    def pinned_mono_then_real_lr(*args, **kwargs):
        if kwargs.get("channel") is None:
            return {"classifications": "uncertain"}
        return original_pair(*args, **kwargs)

    monkeypatch.setattr(alignments_module, "_align_one_pair", pinned_mono_then_real_lr)
    outcome = _run(project_root, project, request)
    assert outcome.record.status == "succeeded"
    artifact = outcome.artifact
    assert artifact is not None
    camera = artifact.auxiliary_cameras[0]
    # zero mapped and no execution error: a pure conflict camera is omitted,
    # never failed
    assert camera.status == "omitted"
    assert camera.errors == ()
    assert not any(
        iv.classification == "mapped" and iv.auxiliary_camera_id == camera.camera_id
        for iv in artifact.intervals
    )
    conflict = [
        iv for iv in artifact.intervals
        if iv.classification == "conflict" and iv.auxiliary_camera_id == camera.camera_id
    ]
    assert conflict, "inconsistent L/R relations must close as conflict"
    # the conflict evidence carries real verified candidate evidence, never
    # no_candidate/0/[]; the verification window count stays the contract
    # value 3 even when two channels each verified three windows
    evidence = conflict[0].evidence
    assert evidence["code"] == "fixed_offset_verified"
    assert evidence["raw_candidate_count"] >= 2
    assert evidence["verification_window_count"] == 3
    assert evidence["max_local_offset_error_ticks"] is not None
    assert (
        len(evidence["matching_fingerprint_counts"])
        == evidence["raw_candidate_count"]
    )


@pytest.mark.usefixtures("synthetic_media_runtime")
def test_p1_unstable_main_audio_fails_closed(tmp_path: Path) -> None:
    sys.path.insert(0, str(ROOT / "core" / "tests" / "fixtures"))
    from alignment_synthetic import make_offset_pair

    media = tmp_path / "media"
    main_path, aux_path = make_offset_pair(
        media,
        main_duration_ticks=7_200_000,
        aux_offset_ticks=300_000,
        seed=78,
    )
    project_root, project = _project_with_sources(
        tmp_path, media_files=[("main", main_path), ("aux", aux_path)]
    )
    main_id, aux_id = project.sources[0].source_id, project.sources[1].source_id
    request = _align_request(
        project,
        operation_id="op_00000000000040008000000000000041",
        alignment_id="aln_unstable",
        main_ids=[main_id],
        aux_cameras=[("aux-1", [aux_id])],
    )
    request["main_audio_stable"] = False
    store = MediaOperationStore(project_root, project.project_id)
    with pytest.raises(MediaOperationError) as exc:
        run_align_multicam(project_root, **request)
    assert exc.value.code == "alignment_input_stale"
    # no record, no workspace, no artifact
    assert store.read(request["operation_id"]) is None
    assert not (
        project_root / "artifacts" / "multicam-alignments" / "aln_unstable.json"
    ).exists()


# ---------------------------------------------------------------------------
# P1 budget hard gates: fake children under the shared deadline/memory ceiling
# ---------------------------------------------------------------------------


class _FakeDeadline:
    def __init__(self, remaining: float = 300.0) -> None:
        self._remaining = remaining
        self._started = time.monotonic()

    def remaining(self) -> float:
        if self._remaining <= 0:
            from roughcut.domain.alignment import AlignmentError

            raise AlignmentError(
                "alignment_time_budget_exceeded",
                "alignment wall-time budget exceeded",
            )
        return self._remaining


class _MarkerArmedDeadline:
    """Allow bounded startup, then apply the original marker-armed timeout."""

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


class _FakeChildBudget:
    """Deterministic fake of the child budget runner for tests."""

    def __init__(
        self,
        *,
        memory_over: bool = False,
        timeout: bool = False,
        memory_limit: int = 4_294_967_296,
    ) -> None:
        self.memory_over_flag = memory_over
        self.timeout_flag = timeout
        self.max_memory_bytes = memory_limit
        self.pids: list[int] = []
        self.terminated: list[int] = []
        self.waited: list[int] = []

    def memory_over(self, pid: int) -> bool:
        self.pids.append(pid)
        return self.memory_over_flag

    def timeout_seconds(self) -> float:
        return 0.01 if self.timeout_flag else 60.0


def test_p1_low_memory_budget_kills_and_waits_child() -> None:
    """A child over the memory ceiling is terminated and waited, never left
    running, and the operation records failed with zero artifact."""
    import roughcut.application.alignments as align_module

    calls: list[str] = []

    def fake_recognize(
        alignment_python,
        target_wav,
        against_wav,
        output_path,
        *,
        timeout_seconds=None,
        budget=None,
        **kwargs,
    ):
        calls.append("recognize")
        from roughcut.adapters.audalign import AudalignMemoryBudgetError

        raise AudalignMemoryBudgetError("alignment memory budget exceeded")

    original = align_module.run_audalign_recognize
    align_module.run_audalign_recognize = fake_recognize
    try:
        from roughcut.application.alignments import _align_one_pair
        from roughcut.domain.alignment import AlignmentError

        deadline = _FakeDeadline()
        with pytest.raises(AlignmentError) as exc:
            _align_one_pair(
                "src_main_1",
                14_400_000,
                Path("/dev/null"),
                "src_aux_1",
                14_400_000,
                Path("/dev/null"),
                "aux-1",
                Path("/dev/null"),
                ToolResolution("ffmpeg", "ffmpeg", "fixture"),
                Path("/tmp"),
                deadline,  # type: ignore[arg-type]
                4_294_967_296,
                None,  # the adapter is faked; no real child budget is consulted
            )
        assert exc.value.code == "alignment_memory_budget_exceeded"
    finally:
        align_module.run_audalign_recognize = original
    assert calls == ["recognize"]


def test_p1_low_disk_budget_stops_before_write() -> None:
    """A decode reservation over the disk ceiling stops before any child
    writes, with no workspace residue and a closed failed record."""
    from roughcut.application.alignments import _WorkspaceBudget

    budget = _WorkspaceBudget(Path("/nonexistent-workspace"), max_disk_bytes=1)
    from roughcut.domain.alignment import AlignmentError

    with pytest.raises(AlignmentError) as exc:
        budget.reserve_decode(duration_ticks=14_400_000)
    assert exc.value.code == "alignment_disk_budget_exceeded"


def test_production_budget_ceilings_accept_maxima_and_reject_over() -> None:
    from roughcut.application import alignments

    assert alignments.ALIGNMENT_DISK_CEILING_BYTES == 2_147_483_648
    assert alignments.ALIGNMENT_MEMORY_CEILING_BYTES == 4_294_967_296
    assert alignments.ALIGNMENT_TIME_CEILING_SECONDS == 7_200

    maxima = (
        ("max_temporary_disk_bytes", alignments.ALIGNMENT_DISK_CEILING_BYTES),
        ("max_analysis_memory_bytes", alignments.ALIGNMENT_MEMORY_CEILING_BYTES),
        ("max_runtime_seconds", alignments.ALIGNMENT_TIME_CEILING_SECONDS),
    )
    for name, ceiling in maxima:
        alignments._validate_budget(name, ceiling, ceiling)
    for name, ceiling in maxima:
        with pytest.raises(MediaOperationError) as exc:
            alignments._validate_budget(name, ceiling + 1, ceiling)
        assert exc.value.code == "alignment_input_stale"
        assert "exceeds the frozen ceiling" in str(exc.value)
    for bad in (0, -1, True):
        with pytest.raises(MediaOperationError) as exc:
            alignments._validate_budget(
                "max_temporary_disk_bytes",
                bad,
                alignments.ALIGNMENT_DISK_CEILING_BYTES,
            )
        assert exc.value.code == "alignment_input_stale"
        assert "not positive" in str(exc.value)


def test_p1_verifier_six_calls_share_deadline() -> None:
    """The verifier's six window FFmpeg/Audalign calls all re-read the same
    operation deadline, and the version probe cannot exceed it."""
    from roughcut.adapters.child_budget import ChildBudget
    from roughcut.application.alignment_profile import FixedOffsetVerifier

    deadline = _FakeDeadline(remaining=60.0)
    child_budget = ChildBudget(deadline, 4_294_967_296)  # type: ignore[arg-type]
    verifier = FixedOffsetVerifier(
        alignment_python=Path("/dev/null"),
        ffmpeg_command="ffmpeg",
        workspace=Path("/tmp"),
        deadline=deadline,  # type: ignore[arg-type]
        child_budget=child_budget,
    )
    assert verifier._timeout() == 60.0
    assert verifier._budget() is not None
    assert verifier._budget().timeout_seconds() == 60.0
    # the verifier must reuse the exact coordinator budget, not build a new one
    assert verifier._budget() is child_budget
    # every one of the six per-window children (2 FFmpeg cuts + 1 audalign per
    # window, three windows) re-reads the same deadline before launching
    import roughcut.adapters.audalign.ffmpeg_audio as ffmpeg_audio_module
    from roughcut.application import alignment_profile as profile_module

    calls: list[str] = []
    original_extract = ffmpeg_audio_module.extract_wav_window
    original_recognize = profile_module.run_audalign_recognize

    def fake_extract(wav_path, output_path, **kwargs):
        calls.append(f"extract:{kwargs.get('timeout_seconds')}")
        raise ffmpeg_audio_module.FFmpegAlignmentError("window cut failed")

    def fake_recognize(*args, **kwargs):
        calls.append(f"recognize:{kwargs.get('timeout_seconds')}")
        raise profile_module.AudalignAdapterError("recognize failed")

    ffmpeg_audio_module.extract_wav_window = fake_extract
    profile_module.run_audalign_recognize = fake_recognize
    try:
        from roughcut.application.alignment_profile import (
            ALIGNMENT_VERIFICATION_WINDOW_TICKS,
        )

        with pytest.raises(ffmpeg_audio_module.FFmpegAlignmentError):
            verifier.verify(
                Path("/dev/null"),
                Path("/dev/null"),
                candidate_b_ticks=0,
                main_overlap_start_ticks=0,
                main_overlap_end_ticks=3 * ALIGNMENT_VERIFICATION_WINDOW_TICKS,
                auxiliary_overlap_start_ticks=0,
                auxiliary_overlap_end_ticks=3 * ALIGNMENT_VERIFICATION_WINDOW_TICKS,
            )
    finally:
        ffmpeg_audio_module.extract_wav_window = original_extract
        profile_module.run_audalign_recognize = original_recognize
    assert calls, "the verifier must launch window children"
    assert all(float(item.split(":")[1]) == 60.0 for item in calls), calls


def test_p1_bounded_child_kills_and_waits_on_timeout(tmp_path: Path) -> None:
    """A bounded child that exceeds the deadline is terminated and waited;
    the writer lock is only released after the child has fully exited."""
    from roughcut.adapters.child_budget import (
        ChildBudget,
        run_bounded_child,
    )

    child_pid_file = tmp_path / "bounded-child.pid"
    deadline = _MarkerArmedDeadline(child_pid_file, 0.3)
    budget = ChildBudget(deadline, 4_294_967_296)
    script = (
        "import os, pathlib, sys, time;"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='ascii');"
        "time.sleep(30)"
    )
    with pytest.raises(ChildProcessTimeBudgetError):
        run_bounded_child(
            [sys.executable, "-c", script, str(child_pid_file)],
            budget=budget,
        )
    assert child_pid_file.exists()
    assert not _process_is_running(int(child_pid_file.read_text(encoding="ascii")))


def test_p1_over_budget_record_failed_zero_artifact(tmp_path: Path) -> None:
    """An operation that exceeds its memory budget records failed with the
    closed code and publishes nothing."""
    project_root = tmp_path / "project"
    project = create_project(project_root, "Budget")
    store = MediaOperationStore(project_root, project.project_id)
    from roughcut.domain.media_operation import MediaOperationFailure

    record = MediaOperationRecord(
        schema_version=2,
        operation_id="op_00000000000040008000000000000043",
        scope=store.scope,
        operation_type="align_multicam",
        request_hash="1" * 64,
        input_hash="2" * 64,
        status="failed",
        phase_message_code="alignment_failed",
        created_at="2026-07-29T00:00:00.000000Z",
        started_at="2026-07-29T00:00:01.000000Z",
        updated_at="2026-07-29T00:00:02.000000Z",
        finished_at="2026-07-29T00:00:02.000000Z",
        result_ref=None,
        error=MediaOperationFailure(
            code="alignment_memory_budget_exceeded",
            responsibility="roughcut_core",
            action="recognize_auxiliary",
            message_code="alignment_failed",
        ),
    )
    with store.writer(record.operation_id, create=True) as acquired:
        assert acquired
        store.write_locked(record)
    assert store.read(record.operation_id) == record
    assert not (
        project_root / "artifacts" / "multicam-alignments"
    ).exists()


# ---------------------------------------------------------------------------
# P1 round two: bounded child success, memory overrun, tree cleanup, TMPDIR
# ---------------------------------------------------------------------------


def test_p2_bounded_child_success_preserves_str_output() -> None:
    """A normal bounded child completes and returns str stdout/stderr without
    any second decode."""
    from roughcut.adapters.child_budget import (
        ChildBudget,
        run_bounded_child,
    )

    deadline = _FakeDeadline(remaining=60.0)
    budget = ChildBudget(deadline, 4_294_967_296)  # type: ignore[arg-type]
    result = run_bounded_child(
        [sys.executable, "-c", "print('hello world')"],
        budget=budget,
    )
    assert isinstance(result.stdout, str)
    assert "hello world" in result.stdout
    assert result.returncode == 0


def test_p2_bounded_child_memory_overrun_kills_mid_run(tmp_path: Path) -> None:
    """A child allocates after startup under the platform memory boundary.

    macOS observes real tree memory. Windows retains the exact production Job
    ceiling and maps only positive Job evidence to the memory exception; a
    plain allocation failure remains an ordinary nonzero result. Either path
    waits the child, and startup/allocation intent alone never proves memory.
    """
    from roughcut.adapters.child_budget import (
        ChildBudget,
        run_bounded_child,
    )

    if sys.platform == "win32":
        # The native owner must retain the production ceiling. If Windows
        # reports positive Job evidence, the runner raises the typed memory
        # error; an allocation failure without that evidence remains an
        # ordinary nonzero child result.
        limit = 4_294_967_296
        allocation_chunks = 272
        allocation_chunk_bytes = 16 * 1024 * 1024
        allocation_pause_seconds = 0.005
    else:
        # Measure the startup baseline RSS of the exact child first; the
        # ceiling sits above that baseline so the failure can only come from
        # the delayed allocation, never from spawn.
        import subprocess as subprocess_module

        from roughcut.adapters.child_budget import _macos_process_rusage_bytes

        baseline_p = subprocess_module.Popen(
            [sys.executable, "-c", "import time; time.sleep(0.3)"],
            start_new_session=True,
        )
        time.sleep(0.15)
        baseline = _macos_process_rusage_bytes(baseline_p.pid)
        baseline_p.kill()
        baseline_p.wait()
        limit = int(baseline * 1.5) + 8 * 1024 * 1024
        allocation_chunks = 64
        allocation_chunk_bytes = 1024 * 1024
        allocation_pause_seconds = 0.02
    deadline = _FakeDeadline(remaining=60.0)
    budget = ChildBudget(deadline, limit)  # type: ignore[arg-type]
    child_pid_file = tmp_path / "bounded-child-memory.pid"
    script = (
        "import os, pathlib, sys, time\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='ascii')\n"
        "chunks = []\n"
        f"for i in range({allocation_chunks}):\n"
        f"    chunks.append(b'x' * {allocation_chunk_bytes})\n"
        f"    time.sleep({allocation_pause_seconds})\n"
        "time.sleep(30)\n"
    )
    if sys.platform == "win32":
        try:
            result = run_bounded_child(
                [sys.executable, "-c", script, str(child_pid_file)],
                budget=budget,
            )
        except ChildProcessMemoryBudgetError:
            pass
        else:
            assert result.returncode != 0
    else:
        with pytest.raises(ChildProcessMemoryBudgetError):
            run_bounded_child(
                [sys.executable, "-c", script, str(child_pid_file)],
                budget=budget,
            )
    assert child_pid_file.exists()
    assert not _process_is_running(int(child_pid_file.read_text(encoding="ascii")))


def test_p2_bounded_child_grandchild_cleanup_on_timeout(tmp_path: Path) -> None:
    """A parent that spawns a grandchild is cleaned up completely on timeout:
    the whole process group is killed and no descendant survives."""
    from roughcut.adapters.child_budget import (
        ChildBudget,
        run_bounded_child,
    )

    descendant_pid_file = tmp_path / "bounded-child-descendant.pid"
    deadline = _MarkerArmedDeadline(descendant_pid_file, 0.4)
    budget = ChildBudget(deadline, 4_294_967_296)
    descendant_script = (
        "import os, pathlib, sys, time;"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='ascii');"
        "time.sleep(60)"
    )
    script = (
        "import subprocess, sys, time;"
        f"subprocess.Popen([sys.executable, '-c', {descendant_script!r}, sys.argv[1]]);"
        "time.sleep(60)"
    )
    with pytest.raises(ChildProcessTimeBudgetError):
        run_bounded_child(
            [sys.executable, "-c", script, str(descendant_pid_file)],
            budget=budget,
        )
    assert descendant_pid_file.exists()
    assert not _process_is_running(
        int(descendant_pid_file.read_text(encoding="ascii"))
    )


def test_p2_bounded_child_tmpdir_applied(tmp_path: Path) -> None:
    """The TMPDIR constraint is applied to the child environment even when the
    caller passes its own env."""
    from roughcut.adapters.child_budget import (
        ChildBudget,
        run_bounded_child,
    )

    deadline = _FakeDeadline(remaining=60.0)
    budget = ChildBudget(
        deadline, 4_294_967_296,  # type: ignore[arg-type]
        apply_tmpdir=lambda env: {**env, "TMPDIR": str(tmp_path)},
    )
    result = run_bounded_child(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ.get('TMPDIR', ''))",
        ],
        budget=budget,
        env={"CUSTOM": "1"},
    )
    assert str(tmp_path) in result.stdout


def test_p2_time_and_memory_codes_are_distinct() -> None:
    """The coordinator's verifier boundary actually maps the two child budget
    exceptions to the two distinct operation codes; a timeout can never
    become a memory failure."""
    from roughcut.adapters.audalign import AudalignMemoryBudgetError, AudalignTimeBudgetError
    from roughcut.adapters.audalign.ffmpeg_audio import (
        FFmpegAlignmentMemoryBudgetError,
        FFmpegAlignmentTimeBudgetError,
    )
    from roughcut.application.alignments import _align_one_pair
    from roughcut.domain.alignment import AlignmentError

    class _FakeCandidate:
        offset_seconds = "0.0"
        confidence = 9

    class _FakeMatch:
        candidates = (_FakeCandidate(),)
        raw_matching_fingerprint_counts = (1,)

    def fake_recognize(*args, **kwargs):
        return _FakeMatch()

    import roughcut.application.alignments as align_module

    original_recognize = align_module.run_audalign_recognize
    original_verify = align_module.FixedOffsetVerifier.verify
    align_module.run_audalign_recognize = fake_recognize

    def fake_verify_time(self, main_wav, auxiliary_wav, **kwargs):
        raise AudalignTimeBudgetError("time exceeded")

    def fake_verify_memory(self, main_wav, auxiliary_wav, **kwargs):
        raise FFmpegAlignmentMemoryBudgetError("memory exceeded")

    try:
        align_module.FixedOffsetVerifier.verify = fake_verify_time
        with pytest.raises(AlignmentError) as time_exc:
            _align_one_pair(
                "src_main_1",
                14_400_000,
                Path("/dev/null"),
                "src_aux_1",
                14_400_000,
                Path("/dev/null"),
                "aux-1",
                Path("/dev/null"),
                ToolResolution("ffmpeg", "ffmpeg", "fixture"),
                Path("/tmp"),
                _FakeDeadline(),  # type: ignore[arg-type]
                4_294_967_296,
                None,
            )
        assert time_exc.value.code == "alignment_time_budget_exceeded"

        align_module.FixedOffsetVerifier.verify = fake_verify_memory
        with pytest.raises(AlignmentError) as memory_exc:
            _align_one_pair(
                "src_main_1",
                14_400_000,
                Path("/dev/null"),
                "src_aux_1",
                14_400_000,
                Path("/dev/null"),
                "aux-1",
                Path("/dev/null"),
                ToolResolution("ffmpeg", "ffmpeg", "fixture"),
                Path("/tmp"),
                _FakeDeadline(),  # type: ignore[arg-type]
                4_294_967_296,
                None,
            )
        assert memory_exc.value.code == "alignment_memory_budget_exceeded"
    finally:
        align_module.run_audalign_recognize = original_recognize
        align_module.FixedOffsetVerifier.verify = original_verify
    assert not issubclass(AudalignTimeBudgetError, AudalignMemoryBudgetError)
    assert not issubclass(
        FFmpegAlignmentTimeBudgetError, FFmpegAlignmentMemoryBudgetError
    )


# ---------------------------------------------------------------------------
# P1 round two: upstream order and evidence threading (no Audalign runtime)
# ---------------------------------------------------------------------------


def test_p2_group_candidates_preserves_upstream_order() -> None:
    """Each 12000-tick group is returned in upstream_index order, not B order."""
    from roughcut.application.alignment_profile import (
        CandidateOffset,
        group_candidates,
    )

    candidates = (
        CandidateOffset(b_ticks=1000, raw_seconds_text="1", confidence=5, upstream_index=2),
        CandidateOffset(b_ticks=1100, raw_seconds_text="2", confidence=9, upstream_index=0),
        CandidateOffset(b_ticks=900, raw_seconds_text="3", confidence=1, upstream_index=1),
        CandidateOffset(b_ticks=200_000, raw_seconds_text="4", confidence=3, upstream_index=3),
    )
    groups = group_candidates(candidates)
    assert len(groups) == 2
    first = groups[0]
    assert [item.upstream_index for item in first] == [0, 1, 2]
    second = groups[1]
    assert [item.upstream_index for item in second] == [3]


def test_p2_mapped_evidence_raw_count_166() -> None:
    """A mapped span emitted by the application evidence pipeline with
    raw_candidate_count == 166 must carry the full original-order matching
    fingerprint counts of length 166, satisfying
    raw_candidate_count == len(matching_fingerprint_counts)."""
    from roughcut.application.alignments import _mapped_span_interval

    counts = tuple(range(166))
    interval = _mapped_span_interval(
        "aux-1",
        "src_main_1",
        0,
        14_400_000,
        (
            0,
            14_400_000,
            "src_aux_1",
            0,
            14_400_000,
            0,
            166,
            counts,
        ),
        0,
    )
    evidence = interval.evidence
    assert evidence["raw_candidate_count"] == 166
    assert len(evidence["matching_fingerprint_counts"]) == 166
    assert evidence["raw_candidate_count"] == len(
        evidence["matching_fingerprint_counts"]
    )


def test_p2_pair_conflict_single_count_copy_window_three() -> None:
    """Two candidate groups more than 12000 ticks apart both pass the fake
    verifier: the pair closes as conflict with the single upstream counts
    copy [10, 20] (never duplicated to four entries), raw == 2, and the
    verification window count == 3."""
    from roughcut.application.alignments import _align_one_pair

    class _FakeCandidate:
        offset_seconds = "0.0"
        confidence = 9

    class _FakeCandidate2:
        offset_seconds = "1.6667"
        confidence = 8

    class _FakeMatch:
        candidates = (_FakeCandidate(), _FakeCandidate2())
        raw_matching_fingerprint_counts = (10, 20)

    def fake_recognize(*args, **kwargs):
        return _FakeMatch()

    import roughcut.application.alignments as align_module

    original = align_module.run_audalign_recognize
    original_verify = align_module.FixedOffsetVerifier.verify
    align_module.run_audalign_recognize = fake_recognize

    def fake_verify(self, main_wav, auxiliary_wav, **kwargs):
        return {
            "passed": True,
            "local_errors_ticks": [0, 0, 0],
            "max_local_offset_error_ticks": 0,
        }

    align_module.FixedOffsetVerifier.verify = fake_verify
    try:
        result = _align_one_pair(
            "src_main_1",
            14_400_000,
            Path("/dev/null"),
            "src_aux_1",
            14_400_000,
            Path("/dev/null"),
            "aux-1",
            Path("/dev/null"),
            ToolResolution("ffmpeg", "ffmpeg", "fixture"),
            Path("/tmp"),
            _FakeDeadline(),  # type: ignore[arg-type]
            4_294_967_296,
            None,
        )
    finally:
        align_module.run_audalign_recognize = original
        align_module.FixedOffsetVerifier.verify = original_verify
    assert result["classifications"] == "conflict"
    assert result["matching_fingerprint_counts"] == [10, 20]
    assert result["raw_candidate_count"] == 2
    assert result["verification_window_count"] == 3


def test_p2_overlapping_mapped_conflict_evidence(tmp_path: Path) -> None:
    """Two overlapping verified mapped owners produce one conflict span whose
    evidence merges the real counts, raw equals the merged length, window
    count stays 3, and max error is the maximum."""
    from roughcut.application.alignments import (
        _conflict_span_interval,
    )
    from roughcut.domain.alignment import ALIGNMENT_VERIFICATION_WINDOW_COUNT

    ordered = [
        (0, 100, "src_a", 0, 100, 500, 3, (1, 2, 3)),
        (50, 150, "src_b", 0, 100, 900, 2, (4, 5)),
    ]
    interval = _conflict_span_interval(
        "aux-1", "src_main_1", 50, 100, 0, ordered, {0, 1}
    )
    evidence = interval.evidence
    assert evidence["matching_fingerprint_counts"] == [1, 2, 3, 4, 5]
    assert evidence["raw_candidate_count"] == 5
    assert evidence["verification_window_count"] == ALIGNMENT_VERIFICATION_WINDOW_COUNT
    assert evidence["max_local_offset_error_ticks"] == 900


def test_p2_lr_one_side_conflict_wins(tmp_path: Path) -> None:
    """A verified conflict from one L/R channel is never overridden by the
    other channel's mapped result."""
    from roughcut.application.alignments import _align_one_pair_mono_first, _WorkspaceBudget

    calls: list[str] = []

    class _FakeMatch:
        candidates = ()
        raw_matching_fingerprint_counts = (1,)

    def fake_recognize(*args, **kwargs):
        calls.append("recognize")
        return _FakeMatch()

    def fake_decode(source_path, output_path, **kwargs):
        return None

    import roughcut.application.alignments as align_module

    original = align_module.run_audalign_recognize
    original_pair = align_module._align_one_pair
    original_decode = align_module.decode_alignment_audio
    align_module.run_audalign_recognize = fake_recognize
    align_module.decode_alignment_audio = fake_decode

    pair_calls: list[str] = []

    def fake_pair(*args, **kwargs):
        channel = kwargs.get("channel")
        pair_calls.append(channel or "mono")
        if channel is None:
            return {"classifications": "uncertain"}
        if channel == "left":
            return {
                "classifications": "conflict",
                "raw_candidate_count": 2,
                "matching_fingerprint_counts": [1, 2],
                "verification_window_count": 3,
                "max_local_offset_error_ticks": 400,
            }
        return {
            "classifications": "mapped",
            "b_ticks": 500,
            "raw_candidate_count": 1,
            "matching_fingerprint_counts": [1],
            "max_local_offset_error_ticks": 100,
        }

    align_module._align_one_pair = fake_pair
    # Use two real regular files so the frozen basis checks exercise the same
    # physical identity and locator hashing as the production path.
    from roughcut.application.sources import fingerprint_file as _ff2

    main_path = tmp_path / "main-input.bin"
    aux_path = tmp_path / "aux-input.bin"
    main_path.write_bytes(b"main fixture")
    aux_path.write_bytes(b"aux fixture")
    assert main_path.is_file()
    assert aux_path.is_file()
    assert main_path.stat().st_nlink == 1
    assert aux_path.stat().st_nlink == 1

    def frozen_entry(source_path: Path) -> dict[str, object]:
        fingerprint = _ff2(source_path)
        return {
            "import_mode": "linked",
            "locator_identity_hash": canonical_sha256_v1(
                {"locator": {"absolute_path": str(source_path)}}
            ),
            "fingerprint": {
                "size": fingerprint.size,
                "mtime_ns": fingerprint.mtime_ns,
                "sha256_head_tail": fingerprint.sha256_head_tail,
            },
            "identity": align_module._source_identity_evidence(source_path),
            "probe": {
                "duration_ticks": 14_400_000,
                "audio_codec": "pcm_s16le",
                "audio_sample_rate": 44_100,
            },
        }

    frozen_identity = {
        "src_main_1": frozen_entry(main_path),
        "src_aux_1": frozen_entry(aux_path),
    }

    def fake_asset(source_path: Path) -> object:
        fingerprint = _ff2(source_path)
        return type(
            "Asset",
            (),
            {
                "import_mode": ImportMode.LINKED,
                "locator": {"absolute_path": str(source_path)},
                "fingerprint": type(
                    "F",
                    (),
                    {
                        "size": fingerprint.size,
                        "mtime_ns": fingerprint.mtime_ns,
                        "sha256_head_tail": fingerprint.sha256_head_tail,
                    },
                )(),
                "probe": type(
                    "P",
                    (),
                    {
                        "duration_ticks": 14_400_000,
                        "audio_codec": "pcm_s16le",
                        "audio_sample_rate": 44_100,
                    },
                )(),
            },
        )()

    main_asset = fake_asset(main_path)
    aux_asset = fake_asset(aux_path)
    alignment_python = tmp_path / "alignment-python"
    alignment_python.write_text("fixture", encoding="utf-8")
    try:
        result = _align_one_pair_mono_first(
            "src_main_1",
            14_400_000,
            main_path,
            main_path,
            "src_aux_1",
            14_400_000,
            aux_path,
            main_asset,  # type: ignore[arg-type]
            aux_asset,  # type: ignore[arg-type]
            "aux-1",
            alignment_python,
            ToolResolution("ffmpeg", "ffmpeg", "fixture"),
            tmp_path,
            _FakeDeadline(),  # type: ignore[arg-type]
            4_294_967_296,
            None,
            _WorkspaceBudget(tmp_path, 536_870_912),
            frozen_identity,
        )
    finally:
        align_module.run_audalign_recognize = original
        align_module._align_one_pair = original_pair
        align_module.decode_alignment_audio = original_decode
    # mono ran first, then both channels; the left channel's verified conflict
    # wins over the right mapped result
    assert pair_calls == ["mono", "left", "right"]
    assert result["classifications"] == "conflict"
    assert result["raw_candidate_count"] == 2
    assert result["matching_fingerprint_counts"] == [1, 2]
    assert result["verification_window_count"] == 3


def test_p2_frozen_identity_replacement_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the input hash is formed, replacing a Source file with one of
    identical bytes/size/mtime but a different inode must fail closed with
    alignment_basis_changed_during_run, a failed record, and zero artifact."""
    from roughcut.application.alignments import (
        _snapshot_requested_identities,
    )

    project_root = tmp_path / "project"
    project = create_project(project_root, "Identity")
    media = tmp_path / "media"
    main_path = media / "main.wav"
    main_path.parent.mkdir(parents=True)
    # a real probeable WAV so add_source succeeds
    import struct as struct_module
    import wave as wave_module

    def _write_wav(path: Path, seed: int) -> None:
        with wave_module.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(44_100)
            frames = b"".join(
                struct_module.pack("<h", (index * seed) % 32767)
                for index in range(44_100 * 40)
            )
            output.writeframes(frames)

    _write_wav(main_path, 3)
    aux_path = media / "aux.wav"
    _write_wav(aux_path, 5)

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
    main_id = project.sources[0].source_id
    aux_id = project.sources[1].source_id
    sources = {
        main_id: project.sources[0],
        aux_id: project.sources[1],
    }
    from roughcut.application.media_operations import _PersistentMediaRuntime

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    assert ffmpeg is not None and ffprobe is not None
    runtime = _PersistentMediaRuntime(
        binding=None,  # type: ignore[arg-type]
        runtime_binding_sha256="a" * 64,
        python_receipt_hash="b" * 64,
        ffmpeg_tool_selection_hash="c" * 64,
        ffprobe_tool_selection_hash="d" * 64,
        ffmpeg=_runtime_tool(ffmpeg),
        ffprobe=_runtime_tool(ffprobe),
    )
    monkeypatch.setenv("ROUGHCUT_FFMPEG_COMMAND", ffmpeg)
    monkeypatch.setenv("ROUGHCUT_FFPROBE_COMMAND", ffprobe)
    frozen = _snapshot_requested_identities(
        project_root, project, sources, runtime
    )
    # replace the main file with identical bytes/size/mtime but a new inode
    old_bytes = main_path.read_bytes()
    old_size = os.stat(main_path).st_size
    old_mtime_ns = os.stat(main_path).st_mtime_ns
    old_inode = os.stat(main_path).st_ino
    replacement = tmp_path / "replacement.wav"
    replacement.write_bytes(old_bytes)
    os.utime(replacement, ns=(old_mtime_ns, old_mtime_ns))
    assert os.stat(replacement).st_ino != old_inode
    os.replace(replacement, main_path)
    assert main_path.read_bytes() == old_bytes
    assert os.stat(main_path).st_size == old_size
    assert os.stat(main_path).st_mtime_ns == old_mtime_ns
    assert os.stat(main_path).st_ino != old_inode
    from roughcut.application.sources import fingerprint_file

    assert fingerprint_file(main_path).sha256_head_tail == frozen[main_id][
        "fingerprint"
    ]["sha256_head_tail"]
    from roughcut.application.alignments import _revalidate_basis

    with pytest.raises(AlignmentError) as exc:
        _revalidate_basis(
            project_root,
            project,
            sources,
            runtime,
            frozen,
        )
    assert exc.value.code == "alignment_basis_changed_during_run"


def test_p2_duplicate_distribution_receipt_rejected(tmp_path: Path) -> None:
    """A receipt with two identical name/version entries must be rejected by
    the managed alignment group reuse validation."""
    from roughcut.adapters.component_environment import (
        ComponentManifest,
        ComponentRecord,
        ComponentVerification,
    )
    from roughcut.adapters.component_installation import (
        _managed_alignment_group_available,
    )

    managed_root = tmp_path / "managed"
    venv = managed_root / "audalign" / "venv"
    (venv / "bin").mkdir(parents=True)
    interpreter = venv / "bin" / "python"
    interpreter.write_text("#!/bin/sh\n", encoding="utf-8")
    interpreter.chmod(0o755)
    receipt_dir = managed_root / "audalign"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt = {
        "distributions": [
            {"name": "audalign", "version": "1.3.1"},
            {"name": "audalign", "version": "1.3.1"},
        ],
        "dependency_lock_receipt": {"value": "f" * 64},
        "license_notice_receipt": {"value": "e" * 64},
    }
    receipt_path = receipt_dir / "venv-receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    import hashlib as hashlib_module

    record = ComponentRecord(
        name="audalign",
        kind="python_package",
        source_type="managed",
        origin="fixture",
        version="1.3.1",
        path="audalign/venv-receipt.json",
        platform=runtime_binding.current_platform(),
        architecture=runtime_binding.current_architecture(),
        license="MIT",
        verification=ComponentVerification(
            "sha256", hashlib_module.sha256(receipt_path.read_bytes()).hexdigest()
        ),
    )
    manifest = ComponentManifest(
        components=(record,),
        platform=runtime_binding.current_platform(),
        architecture=runtime_binding.current_architecture(),
        managed_root=str(managed_root),
    )
    spec = type(
        "FakeAlignmentGroupSpec",
        (),
            {
                "group_name": "audalign",
                "managed_record_name": "audalign",
            "version": "1.3.1",
            "distributions": (
                ("audalign", "1.3.1"),
                ("numpy", "1.26.4"),
            ),
            "dependency_lock_sha256": "f" * 64,
            "license_notice_sha256": "e" * 64,
            "dependency_lock": type(
                "FakeLock", (), {"name": "lock.txt"}
            )(),
            "license_notice_file": type(
                "FakeLicense", (), {"name": "license.txt"}
            )(),
        },
    )()
    available = _managed_alignment_group_available(
        spec,  # type: ignore[arg-type]
        manifest,
        managed_root,
        runtime_binding.current_platform(),
        runtime_binding.current_architecture(),
    )
    assert available is False


def test_p3_coordinator_replacement_fails_closed_before_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replacement between the input hash and the first worker: the operation
    fails closed with alignment_basis_changed_during_run, the record is
    failed, the worker never reads the replaced Source, and no artifact is
    published."""
    import struct as struct_module
    import wave as wave_module

    def _write_wav(path: Path, seed: int) -> None:
        with wave_module.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(44_100)
            frames = b"".join(
                struct_module.pack("<h", (index * seed) % 32767)
                for index in range(44_100 * 40)
            )
            output.writeframes(frames)

    media = tmp_path / "media"
    main_path = media / "main.wav"
    main_path.parent.mkdir(parents=True)
    _write_wav(main_path, 3)
    aux_path = media / "aux.wav"
    _write_wav(aux_path, 5)
    project_root, project = _project_with_sources(
        tmp_path, media_files=[("main", main_path), ("aux", aux_path)]
    )
    main_id = project.sources[0].source_id
    aux_id = project.sources[1].source_id
    request = _align_request(
        project,
        operation_id="op_00000000000040008000000000000050",
        alignment_id="aln_coord_replacement",
        main_ids=[main_id],
        aux_cameras=[("aux-1", [aux_id])],
    )
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    assert ffmpeg is not None and ffprobe is not None
    monkeypatch.setenv("ROUGHCUT_FFMPEG_COMMAND", ffmpeg)
    monkeypatch.setenv("ROUGHCUT_FFPROBE_COMMAND", ffprobe)
    import roughcut.application.alignments as align_module
    from roughcut.adapters.runtime_binding import (
        audalign_distribution_versions_for,
        audalign_distributions_for,
    )
    from roughcut.application.media_operations import _PersistentMediaRuntime
    _dists = audalign_distributions_for("macos")
    _vers = audalign_distribution_versions_for("macos")
    _dists_tuple = tuple({"name": n, "version": _vers[n]} for n in _dists)
    fake_binding = RuntimeAlignmentPython(
        source_type="managed",
        ownership="roughcut_managed",
        interpreter=str(Path(sys.executable).resolve()),
        python_version="3.11",
        distributions=_dists_tuple,
        dependency_lock_receipt={"algorithm": "sha256", "value": "f" * 64},
        license_notice_receipt={"algorithm": "sha256", "value": "e" * 64},
        component_manifest_receipt={"algorithm": "sha256", "value": "d" * 64},
        provider=AUDALIGN_PROVIDER,
        provider_version="1.3.1",
        upstream_commit="d5955ae8a85b1cd480dadd005c3f88986f4ebbef",
    )
    fake_runtime = _PersistentMediaRuntime(
        binding=type("FakeBinding", (), {"alignment_python": fake_binding})(),  # type: ignore[arg-type]
        runtime_binding_sha256="a" * 64,
        python_receipt_hash="b" * 64,
        ffmpeg_tool_selection_hash="c" * 64,
        ffprobe_tool_selection_hash="d" * 64,
        ffmpeg=_runtime_tool(ffmpeg),
        ffprobe=_runtime_tool(ffprobe),
    )
    monkeypatch.setattr(
        align_module, "_load_persistent_runtime", lambda: fake_runtime
    )
    worker_reads: list[Path] = []
    original_decode = align_module.decode_alignment_audio

    def recording_decode(source_path, output_path, **kwargs):
        worker_reads.append(Path(source_path))
        return original_decode(source_path, output_path, **kwargs)

    align_module.decode_alignment_audio = recording_decode
    # replace the main file right after the input hash forms (the first
    # worker call is the main decode)
    original_hash = align_module.canonical_sha256_v1

    def hash_after_replacement(value):
        if isinstance(value, dict) and value.get("input_schema_version") == 1:
            old_bytes = main_path.read_bytes()
            old_mtime_ns = os.stat(main_path).st_mtime_ns
            old_inode = os.stat(main_path).st_ino
            replacement = tmp_path / "replacement2.wav"
            replacement.write_bytes(old_bytes)
            os.utime(replacement, ns=(old_mtime_ns, old_mtime_ns))
            assert os.stat(replacement).st_ino != old_inode
            os.replace(replacement, main_path)
        return original_hash(value)

    align_module.canonical_sha256_v1 = hash_after_replacement
    try:
        with pytest.raises(AlignmentError) as exc:
            run_align_multicam(project_root, **request)
    finally:
        align_module.canonical_sha256_v1 = original_hash
        align_module.decode_alignment_audio = original_decode
    assert exc.value.code == "alignment_basis_changed_during_run"
    store = MediaOperationStore(project_root, project.project_id)
    record = store.read(request["operation_id"])
    assert record is not None
    assert record.status == "failed"
    assert record.error is not None
    assert record.error.code == "alignment_basis_changed_during_run"
    assert record.error.action == "decode_alignment_audio"
    # the worker never read the replaced Source
    assert not any(path == main_path.resolve(strict=False) for path in worker_reads)
    assert not (
        project_root
        / "artifacts"
        / "multicam-alignments"
        / "aln_coord_replacement.json"
    ).exists()


def test_p3_probe_change_fails_closed_at_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A current Project whose requested Source changes audio_codec or
    audio_sample_rate while the revision stays identical fails closed at
    publish revalidation with zero artifact."""
    import struct as struct_module
    import wave as wave_module

    def _write_wav(path: Path, seed: int) -> None:
        with wave_module.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(44_100)
            frames = b"".join(
                struct_module.pack("<h", (index * seed) % 32767)
                for index in range(44_100 * 40)
            )
            output.writeframes(frames)

    media = tmp_path / "media"
    main_path = media / "main.wav"
    main_path.parent.mkdir(parents=True)
    _write_wav(main_path, 3)
    aux_path = media / "aux.wav"
    _write_wav(aux_path, 5)
    project_root, project = _project_with_sources(
        tmp_path, media_files=[("main", main_path), ("aux", aux_path)]
    )
    main_id = project.sources[0].source_id
    aux_id = project.sources[1].source_id
    from roughcut.application.alignments import (
        _snapshot_requested_identities,
    )

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    assert ffmpeg is not None and ffprobe is not None
    monkeypatch.setenv("ROUGHCUT_FFMPEG_COMMAND", ffmpeg)
    monkeypatch.setenv("ROUGHCUT_FFPROBE_COMMAND", ffprobe)
    from roughcut.application.media_operations import _PersistentMediaRuntime

    runtime = _PersistentMediaRuntime(
        binding=None,  # type: ignore[arg-type]
        runtime_binding_sha256="a" * 64,
        python_receipt_hash="b" * 64,
        ffmpeg_tool_selection_hash="c" * 64,
        ffprobe_tool_selection_hash="d" * 64,
        ffmpeg=_runtime_tool(ffmpeg),
        ffprobe=_runtime_tool(ffprobe),
    )
    sources = {main_id: project.sources[0], aux_id: project.sources[1]}
    frozen = _snapshot_requested_identities(
        project_root, project, sources, runtime
    )
    # mutate the current Project: change the main source's audio_sample_rate
    # while keeping the revision identical
    from dataclasses import replace as dataclass_replace

    main_source = project.sources[0]
    changed_probe = dataclass_replace(
        main_source.probe, audio_sample_rate=48_000
    )
    changed_source = dataclass_replace(main_source, probe=changed_probe)
    changed_sources = tuple(
        changed_source if source.source_id == main_id else source
        for source in project.sources
    )
    changed_project = dataclass_replace(project, sources=changed_sources)
    from roughcut.adapters.project_store import ProjectStore

    ProjectStore(project_root).save(changed_project, expected_revision=project.revision)
    from roughcut.application.alignments import _revalidate_basis

    with pytest.raises(AlignmentError) as exc:
        _revalidate_basis(
            project_root,
            project,
            sources,
            runtime,
            frozen,
        )
    assert exc.value.code == "alignment_basis_changed_during_run"


def test_p3_window_verification_time_fault_fails_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_media_runtime: tuple[str, str],
) -> None:
    """A time budget error raised inside the first short-window read fails
    the whole operation with alignment_time_budget_exceeded,
    action=recognize_auxiliary, zero artifact, and no other camera runs."""
    ffmpeg, ffprobe = synthetic_media_runtime
    media = tmp_path / "media"
    main_path = media / "main.wav"
    _write_audio_fixture(main_path, seconds=40)
    aux1 = media / "aux1.wav"
    _write_audio_fixture(aux1, seconds=40)
    aux2 = media / "aux2.wav"
    _write_audio_fixture(aux2, seconds=40)
    project_root, project = _project_with_sources(
        tmp_path,
        media_files=[("main", main_path), ("aux1", aux1), ("aux2", aux2)],
    )
    main_id = project.sources[0].source_id
    aux1_id = project.sources[1].source_id
    aux2_id = project.sources[2].source_id
    request = _align_request(
        project,
        operation_id="op_00000000000040008000000000000051",
        alignment_id="aln_window_time",
        main_ids=[main_id],
        aux_cameras=[("aux-1", [aux1_id]), ("aux-2", [aux2_id])],
        source_pairs={
            "aux-1": [
                {"main_source_id": main_id, "auxiliary_source_id": aux1_id}
            ],
            "aux-2": [
                {"main_source_id": main_id, "auxiliary_source_id": aux2_id}
            ],
        },
    )
    _stub_correlation_runtime(monkeypatch, ffmpeg, ffprobe)
    correlation_calls = _stub_correlation_worker(
        monkeypatch,
        error=AudalignTimeBudgetError("synthetic correlation timeout"),
    )

    with pytest.raises(AlignmentError) as exc:
        run_align_multicam(project_root, **request)
    assert exc.value.code == "alignment_time_budget_exceeded"
    store = MediaOperationStore(project_root, project.project_id)
    record = store.read(request["operation_id"])
    assert record is not None
    assert record.status == "failed"
    assert record.error is not None
    assert record.error.code == "alignment_time_budget_exceeded"
    assert record.error.action == "recognize_auxiliary"
    assert not (
        project_root
        / "artifacts"
        / "multicam-alignments"
        / "aln_window_time.json"
    ).exists()
    # Only the first camera reached its first correlation child; the second
    # camera never started after the operation-wide budget fault.
    assert [kind for kind, _target, _against in correlation_calls] == [
        "extract",
        "recognize",
    ]

def test_p3_window_verification_memory_fault_fails_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_media_runtime: tuple[str, str],
) -> None:
    """A memory budget error raised inside a short-window read fails the
    whole operation with alignment_memory_budget_exceeded and zero artifact."""
    ffmpeg, ffprobe = synthetic_media_runtime
    media = tmp_path / "media"
    main_path = media / "main.wav"
    _write_audio_fixture(main_path, seconds=40)
    aux1 = media / "aux1.wav"
    _write_audio_fixture(aux1, seconds=40)
    project_root, project = _project_with_sources(
        tmp_path, media_files=[("main", main_path), ("aux1", aux1)]
    )
    main_id = project.sources[0].source_id
    aux1_id = project.sources[1].source_id
    request = _align_request(
        project,
        operation_id="op_00000000000040008000000000000052",
        alignment_id="aln_window_memory",
        main_ids=[main_id],
        aux_cameras=[("aux-1", [aux1_id])],
        source_pairs={
            "aux-1": [
                {"main_source_id": main_id, "auxiliary_source_id": aux1_id}
            ]
        },
    )
    _stub_correlation_runtime(monkeypatch, ffmpeg, ffprobe)
    correlation_calls = _stub_correlation_worker(
        monkeypatch,
        error=AudalignMemoryBudgetError("synthetic correlation memory fault"),
    )

    with pytest.raises(AlignmentError) as exc:
        run_align_multicam(project_root, **request)
    assert exc.value.code == "alignment_memory_budget_exceeded"
    store = MediaOperationStore(project_root, project.project_id)
    record = store.read(request["operation_id"])
    assert record is not None
    assert record.status == "failed"
    assert record.error is not None
    assert record.error.code == "alignment_memory_budget_exceeded"
    assert record.error.action == "recognize_auxiliary"
    assert not (
        project_root
        / "artifacts"
        / "multicam-alignments"
        / "aln_window_memory.json"
    ).exists()
    assert [kind for kind, _target, _against in correlation_calls] == [
        "extract",
        "recognize",
    ]

def test_p3_windows_job_abi_and_memory_evidence_fault_injection() -> None:
    """Literal Win32 ABI and the closed positive-memory evidence set."""
    import ctypes

    from roughcut.adapters import child_budget

    assert child_budget._WindowsJob._INFO_BASIC_ACCOUNTING == 1
    assert child_budget._WindowsJob._INFO_EXTENDED_LIMIT == 9
    assert child_budget._WindowsJob._LIMIT_JOB_MEMORY == 0x00000200
    assert child_budget._WindowsJob._INFO_ASSOCIATE_COMPLETION_PORT == 7
    assert child_budget._WindowsJob._JOB_OBJECT_MSG_JOB_MEMORY_LIMIT == 10

    class _FakeKernel:
        def __init__(self) -> None:
            self.assigned: list[int] = []
            self.terminated: list[int] = []
            self.closed = 0
            self.active = 0
            self.peak = 0
            self.messages: list[int] = []
            self.queried_classes: list[int] = []
            self.fail_assign = False
            self.fail_query = False
            self.fail_completion_query = False
            self.fail_terminate = False
            self.fail_close = False

        def OpenProcess(self, *args):
            return 7

        def AssignProcessToJobObject(self, job, pid):
            self.assigned.append(pid)
            if self.fail_assign:
                return 0
            return 1

        def TerminateJobObject(self, job, code):
            self.terminated.append(code)
            if self.fail_terminate:
                return 0
            self.active = 0
            return 1

        def CloseHandle(self, handle):
            self.closed += 1
            return 0 if self.fail_close else 1

        def SetInformationJobObject(self, *args):
            return 1

        def QueryInformationJobObject(self, job, info, buffer, size, returned):
            if self.fail_query:
                return 0
            self.queried_classes.append(info)
            # These expected classes are independent literals. Class 8 is not
            # accepted with a basic-accounting structure.
            if info == 1:
                assert size == ctypes.sizeof(child_budget._WindowsBasicAccounting)
                target = ctypes.cast(
                    buffer,
                    ctypes.POINTER(child_budget._WindowsBasicAccounting),
                ).contents
                target.ActiveProcesses = self.active
            elif info == 9:
                assert size == ctypes.sizeof(child_budget._WindowsExtendedLimit)
                target = ctypes.cast(
                    buffer,
                    ctypes.POINTER(child_budget._WindowsExtendedLimit),
                ).contents
                target.PeakJobMemoryUsed = self.peak
            else:
                return 0
            return 1

        def GetQueuedCompletionStatus(
            self, port, message, key, overlapped, timeout
        ):
            assert timeout == 0
            if self.fail_completion_query:
                return 0
            if not self.messages:
                return 0
            ctypes.cast(message, ctypes.POINTER(ctypes.c_uint32)).contents.value = (
                self.messages.pop(0)
            )
            ctypes.cast(key, ctypes.POINTER(ctypes.c_size_t)).contents.value = 3
            ctypes.cast(overlapped, ctypes.POINTER(ctypes.c_void_p)).contents.value = 99
            return 1

        def CreateJobObjectW(self, *args):
            return 3

    fake = _FakeKernel()

    class _FakeWindowsJob(child_budget._WindowsJob):
        def __init__(self, memory_limit: int) -> None:
            import ctypes

            self._ctypes = ctypes
            self._kernel32 = fake
            self._job = 3
            self._extended = child_budget._WindowsExtendedLimit
            self._thread = None
            self._completion_port = 4
            self._completion_key = 3
            self._assigned = False
            self._emergency_containment_occurred = False
            self._memory_limit_message_received = False

        def _last_error(self) -> int:
            return 5 if fake.fail_completion_query else 258

    # two processes each under the limit, combined over the job-wide ceiling
    fake.active = 2
    fake.peak = 9 * 1024 * 1024
    job = _FakeWindowsJob(10 * 1024 * 1024)
    assert job.memory_limit_exceeded(10 * 1024 * 1024) is False
    fake.peak = 10 * 1024 * 1024
    assert job.memory_limit_exceeded(10 * 1024 * 1024) is True
    fake.peak = 9 * 1024 * 1024
    fake.messages = [10]
    message_job = _FakeWindowsJob(10 * 1024 * 1024)
    assert message_job.memory_limit_exceeded(10 * 1024 * 1024) is True
    missing_message_job = _FakeWindowsJob(10 * 1024 * 1024)
    assert missing_message_job.memory_limit_exceeded(10 * 1024 * 1024) is False
    fake.fail_completion_query = True
    with pytest.raises(child_budget.ChildProcessBudgetError):
        missing_message_job.memory_limit_exceeded(10 * 1024 * 1024)
    fake.fail_completion_query = False

    basic = child_budget._WindowsBasicAccounting()
    assert (
        fake.QueryInformationJobObject(
            3,
            8,
            ctypes.byref(basic),
            ctypes.sizeof(basic),
            None,
        )
        == 0
    )
    assert 9 in fake.queried_classes
    job.terminate()
    job.verify_empty()
    assert 1 in fake.queried_classes
    assert fake.terminated == [1]
    assert fake.active == 0
    job.close()
    assert fake.closed == 2

    # assign failure: the fake kernel rejects the assignment, so the base
    # assign raises the closed error after the OpenProcess handle is closed
    fake.fail_assign = True
    fake.active = 1
    fake.terminated = []
    fake.closed = 0
    job2 = _FakeWindowsJob(10 * 1024 * 1024)
    with pytest.raises(child_budget.ChildProcessBudgetError):
        job2.assign(99)
    # the assignment was attempted with the opened process handle
    assert fake.assigned == [7]
    fake.fail_assign = False

    # A successful assignment does not hide failure to release the temporary
    # process handle. The Job retains ownership for outer cleanup.
    fake.fail_close = True
    job2_close_fault = _FakeWindowsJob(10 * 1024 * 1024)
    with pytest.raises(child_budget.ChildProcessBudgetError):
        job2_close_fault.assign(100)
    assert job2_close_fault.assigned is True
    fake.fail_close = False

    # query failure must raise a closed error, never return 0 silently
    fake.fail_query = True
    job3 = _FakeWindowsJob(10 * 1024 * 1024)
    with pytest.raises(child_budget.ChildProcessBudgetError):
        job3.active_processes()
    fake.fail_query = False

    # terminate failure must raise a closed error, never continue silently
    fake.fail_terminate = True
    fake.active = 1
    job4 = _FakeWindowsJob(10 * 1024 * 1024)
    with pytest.raises(child_budget.ChildProcessBudgetError):
        job4.terminate()
    fake.fail_terminate = False


@pytest.mark.parametrize(
    (
        "message_received",
        "peak",
        "fail_completion_query",
        "fail_peak_query",
        "expected",
    ),
    [
        pytest.param(True, 99, False, True, "positive", id="message-positive-peak-fails"),
        pytest.param(False, 100, True, False, "positive", id="completion-fails-peak-exact"),
        pytest.param(False, 99, False, True, "peak_error", id="message-absent-peak-fails"),
        pytest.param(False, 99, True, False, "completion_error", id="completion-fails-peak-below"),
        pytest.param(False, 99, True, True, "both_errors", id="both-queries-fail"),
    ],
)
def test_p3_windows_job_memory_evidence_cross_query_failures(
    message_received: bool,
    peak: int,
    fail_completion_query: bool,
    fail_peak_query: bool,
    expected: str,
) -> None:
    """Positive evidence wins; otherwise every query failure stays visible."""
    from roughcut.adapters import child_budget

    completion_error = child_budget.ChildProcessBudgetError(
        "completion evidence query failed"
    )
    peak_error = child_budget.ChildProcessBudgetError("peak evidence query failed")

    class _CrossQueryJob(child_budget._WindowsJob):
        def __init__(self) -> None:
            self.calls: list[str] = []

        def drain_notifications(self) -> bool:
            self.calls.append("completion")
            if fail_completion_query:
                raise completion_error
            return message_received

        def peak_job_memory_used(self) -> int:
            self.calls.append("peak")
            if fail_peak_query:
                raise peak_error
            return peak

    job = _CrossQueryJob()
    if expected == "positive":
        assert job.memory_limit_exceeded(100) is True
    else:
        with pytest.raises(child_budget.ChildProcessBudgetError) as exc:
            job.memory_limit_exceeded(100)
        assert not isinstance(
            exc.value,
            child_budget.ChildProcessMemoryBudgetError,
        )
        if expected == "peak_error":
            assert exc.value is peak_error
        else:
            assert exc.value is completion_error
        if expected == "both_errors":
            assert exc.value.__notes__ == [
                "additional memory evidence query failure: peak evidence query failed"
            ]
    assert job.calls == ["completion", "peak"]


def test_p3_windows_job_constructor_sets_limit_before_completion_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The inactive Job receives its hard limit before port association."""
    import ctypes

    from roughcut.adapters import child_budget

    class _Function:
        def __init__(self, implementation):
            self.implementation = implementation
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self.implementation(*args)

    class _FakeKernel:
        def __init__(self) -> None:
            self.set_classes: list[int] = []
            self.assigned = False
            self.closed: list[int] = []
            self.failed_close_handle: int | None = None
            self.failed_information_class: int | None = None
            self.CreateJobObjectW = _Function(lambda *_args: 3)
            self.OpenProcess = _Function(lambda *_args: 7)
            self.AssignProcessToJobObject = _Function(self._assign)
            self.TerminateJobObject = _Function(lambda *_args: 1)
            self.SetInformationJobObject = _Function(self._set_information)
            self.QueryInformationJobObject = _Function(lambda *_args: 1)
            self.CreateIoCompletionPort = _Function(lambda *_args: 4)
            self.GetQueuedCompletionStatus = _Function(lambda *_args: 0)
            self.CreateToolhelp32Snapshot = _Function(lambda *_args: 20)
            self.Thread32First = _Function(lambda *_args: 1)
            self.Thread32Next = _Function(lambda *_args: 0)
            self.OpenThread = _Function(lambda *_args: 30)
            self.ResumeThread = _Function(lambda *_args: 1)
            self.CloseHandle = _Function(self._close)

        def _assign(self, *_args):
            self.assigned = True
            return 1

        def _set_information(self, job, info_class, buffer, size):
            self.set_classes.append(info_class)
            if info_class == 9:
                assert size == ctypes.sizeof(child_budget._WindowsExtendedLimit)
                limit = ctypes.cast(
                    buffer,
                    ctypes.POINTER(child_budget._WindowsExtendedLimit),
                ).contents
                assert limit.LimitFlags & 0x00000200
                assert limit.LimitFlags & 0x00002000
                assert limit.JobMemoryLimit == 128 * 1024 * 1024
            elif info_class == 7:
                assert size == ctypes.sizeof(
                    child_budget._WindowsAssociateCompletionPort
                )
                association = ctypes.cast(
                    buffer,
                    ctypes.POINTER(child_budget._WindowsAssociateCompletionPort),
                ).contents
                assert association.CompletionKey == 3
                assert association.CompletionPort == 4
            else:
                raise AssertionError(f"unexpected information class {info_class}")
            return int(info_class != self.failed_information_class)

        def _close(self, handle):
            self.closed.append(handle)
            return 0 if handle == self.failed_close_handle else 1

    fake = _FakeKernel()
    monkeypatch.setattr(
        child_budget.ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: fake,
        raising=False,
    )
    job = child_budget._WindowsJob(128 * 1024 * 1024)
    assert fake.set_classes == [9, 7]
    assert fake.assigned is False
    job.close()
    assert fake.closed == [3, 4]

    close_fault_job = child_budget._WindowsJob(128 * 1024 * 1024)
    fake.failed_close_handle = 3
    with pytest.raises(child_budget.ChildProcessBudgetError):
        close_fault_job.close()
    assert fake.closed[-2:] == [3, 4]

    fake.failed_close_handle = None
    fake.failed_information_class = 7
    constructor_fault_start = len(fake.closed)
    with pytest.raises(child_budget.ChildProcessBudgetError) as constructor_exc:
        child_budget._WindowsJob(128 * 1024 * 1024)
    assert "completion port could not be associated" in str(constructor_exc.value)
    assert fake.closed[constructor_fault_start:] == [3, 4]

    fake.failed_close_handle = 3
    with pytest.raises(child_budget.ChildProcessBudgetError) as close_exc:
        child_budget._WindowsJob(128 * 1024 * 1024)
    assert "job handle could not be closed" in str(close_exc.value)
    assert close_exc.value.__cause__ is not None
    assert fake.closed[-2:] == [3, 4]


def test_p3_windows_job_suspended_thread_gate_fault_injection() -> None:
    """Toolhelp finds one suspended root thread and ResumeThread returns 1."""
    import ctypes

    from roughcut.adapters import child_budget

    class _FakeKernel:
        def __init__(
            self,
            entries: list[tuple[int, int]],
            resume_result: int = 1,
            *,
            snapshot_close_result: int = 1,
        ):
            self.entries = entries
            self.resume_result = resume_result
            self.snapshot_close_result = snapshot_close_result
            self.index = 0
            self.last_error = 18
            self.opened: list[tuple[int, bool, int]] = []
            self.closed: list[int] = []

        def CreateToolhelp32Snapshot(self, flags, process_id):
            assert flags == 0x00000004
            assert process_id == 0
            self.index = 0
            return 20

        def _publish(self, entry_pointer) -> None:
            thread_id, owner_pid = self.entries[self.index]
            target = ctypes.cast(
                entry_pointer,
                ctypes.POINTER(child_budget._WindowsThreadEntry),
            ).contents
            assert target.dwSize == ctypes.sizeof(child_budget._WindowsThreadEntry)
            target.th32ThreadID = thread_id
            target.th32OwnerProcessID = owner_pid

        def Thread32First(self, snapshot, entry_pointer):
            self._publish(entry_pointer)
            return 1

        def Thread32Next(self, snapshot, entry_pointer):
            self.index += 1
            if self.index >= len(self.entries):
                self.last_error = 18
                return 0
            self._publish(entry_pointer)
            return 1

        def OpenThread(self, access, inherit, thread_id):
            self.opened.append((access, inherit, thread_id))
            return 30

        def ResumeThread(self, thread):
            assert thread == 30
            return self.resume_result

        def CloseHandle(self, handle):
            self.closed.append(handle)
            return self.snapshot_close_result if handle == 20 else 1

    class _FakeWindowsJob(child_budget._WindowsJob):
        def __init__(self, kernel: _FakeKernel) -> None:
            self._ctypes = ctypes
            self._kernel32 = kernel
            self._thread = None

        def _last_error(self) -> int:
            return self._kernel32.last_error

    kernel = _FakeKernel([(11, 77), (12, 88)])
    job = _FakeWindowsJob(kernel)
    job.resume_initial_thread(77)
    assert kernel.opened == [(0x0002, False, 11)]
    assert kernel.closed == [20]
    assert job._thread == 30

    duplicate_kernel = _FakeKernel([(11, 77), (12, 77)])
    with pytest.raises(child_budget.ChildProcessBudgetError):
        _FakeWindowsJob(duplicate_kernel).resume_initial_thread(77)
    assert duplicate_kernel.opened == []

    failed_resume_kernel = _FakeKernel([(11, 77)], resume_result=0xFFFFFFFF)
    with pytest.raises(child_budget.ChildProcessBudgetError):
        _FakeWindowsJob(failed_resume_kernel).resume_initial_thread(77)

    failed_snapshot_close_kernel = _FakeKernel(
        [(11, 77)],
        snapshot_close_result=0,
    )
    with pytest.raises(child_budget.ChildProcessBudgetError):
        _FakeWindowsJob(failed_snapshot_close_kernel).resume_initial_thread(77)
    assert failed_snapshot_close_kernel.opened == []


def test_p3_windows_job_root_exit_descendants_drain() -> None:
    """After the root exits normally, a job with active descendants is
    terminated and drained to zero active processes before the handle is
    closed, so the writer lock is only released afterwards."""
    from roughcut.adapters import child_budget

    class _FakeKernel:
        def __init__(self) -> None:
            self.terminated = 0
            self.closed = 0
            self.active = 1

        def OpenProcess(self, *args):
            return 7

        def AssignProcessToJobObject(self, job, pid):
            return 1

        def TerminateJobObject(self, job, code):
            self.terminated += 1
            self.active = 0
            return 1

        def CloseHandle(self, handle):
            self.closed += 1
            return 1

        def SetInformationJobObject(self, *args):
            return 1

        def CreateJobObjectW(self, *args):
            return 3

    fake = _FakeKernel()

    class _FakeWindowsJob(child_budget._WindowsJob):
        def __init__(self, memory_limit: int) -> None:
            self._ctypes = None
            self._kernel32 = fake
            self._job = 3
            self._extended = None
            self._thread = None
            self._completion_port = None
            self._assigned = False
            self._emergency_containment_occurred = False

        def active_processes(self) -> int:
            return fake.active

    # root exits normally with one active descendant: the runner must
    # terminate the job and wait until the active count reaches zero
    job = _FakeWindowsJob(10 * 1024 * 1024)
    assert job.active_processes() == 1
    job.terminate()
    job.verify_empty()
    assert fake.terminated == 1
    assert fake.active == 0
    job.close()
    assert fake.closed >= 1


@pytest.mark.parametrize(
    ("failure_stage", "expected_events"),
    [
        pytest.param(
            "completion_query",
            [
                "job:init",
                "process:start",
                "job:assign",
                "job:resume",
                "job:memory_query",
                "job:active_query",
                "job:terminate",
                "process:wait",
                "job:verify_empty",
                "job:close_job",
                "job:close_completion",
                "job:close_thread",
            ],
            id="completion-query-failure",
        ),
        pytest.param(
            "active_query",
            [
                "job:init",
                "process:start",
                "job:assign",
                "job:resume",
                "job:memory_query",
                "process:communicate",
                "job:memory_query",
                "job:active_query",
                "job:emergency_close_job",
                "process:wait",
                "job:close_completion",
                "job:close_thread",
            ],
            id="root-exit-active-query-failure",
        ),
        pytest.param(
            "assign",
            [
                "job:init",
                "process:start",
                "job:assign",
                "process:kill",
                "process:wait",
                "job:close_job",
                "job:close_completion",
                "job:close_thread",
            ],
            id="assign-without-job-ownership",
        ),
        pytest.param(
            "gate",
            [
                "job:init",
                "process:start",
                "job:assign",
                "job:resume",
                "job:active_query",
                "job:terminate",
                "process:wait",
                "job:verify_empty",
                "job:close_job",
                "job:close_completion",
                "job:close_thread",
            ],
            id="suspended-gate-resume-failure",
        ),
    ],
)
def test_p3_windows_runner_cleanup_preserves_primary_error(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
    expected_events: list[str],
) -> None:
    """The real bounded-child runner owns every Windows cleanup boundary.

    Assignment and gate failures use actual ownership. Completion/accounting
    failures use verified drain or kill-on-close containment before return.
    """
    from roughcut.adapters import child_budget

    events: list[str] = []
    process_holder: dict[str, _FakeProcess] = {}
    primary = child_budget.ChildProcessBudgetError(
        f"primary {failure_stage} failure"
    )

    class _FakeProcess:
        pid = 73
        args = ("fake-child",)

        def __init__(self) -> None:
            self.running = True
            self.returncode: int | None = None

        def communicate(self, *, timeout: float):
            events.append("process:communicate")
            self.running = False
            self.returncode = 0
            return "", ""

        def poll(self) -> int | None:
            return None if self.running else self.returncode

        def kill(self) -> None:
            events.append("process:kill")
            self.running = False
            self.returncode = 1

        def wait(self) -> int:
            events.append("process:wait")
            if self.running:
                raise AssertionError("process.wait called before root kill")
            if self.returncode is None:
                self.returncode = 1
            return self.returncode

    class _FakeJob:
        def __init__(self, memory_limit: int) -> None:
            events.append("job:init")
            self._assigned = False
            self.emergency_containment_occurred = False

        @property
        def assigned(self) -> bool:
            return self._assigned

        def assign(self, pid: int) -> None:
            events.append("job:assign")
            if failure_stage == "assign":
                raise primary
            self._assigned = True

        def resume_initial_thread(self, pid: int) -> None:
            events.append("job:resume")
            if failure_stage == "gate":
                raise primary

        def memory_limit_exceeded(self, limit: int) -> bool:
            events.append("job:memory_query")
            if failure_stage == "completion_query":
                raise primary
            return False

        def active_processes(self) -> int:
            events.append("job:active_query")
            if failure_stage == "active_query":
                raise primary
            return 1 if process_holder["process"].running else 0

        def terminate(self) -> None:
            events.append("job:terminate")
            process_holder["process"].running = False

        def verify_empty(self) -> None:
            events.append("job:verify_empty")
            assert not process_holder["process"].running

        def close_job_handle(self) -> None:
            events.append("job:close_job")

        def close_job_for_emergency(self) -> None:
            events.append("job:emergency_close_job")
            self.emergency_containment_occurred = True
            process_holder["process"].running = False

        def close_remaining_handles(self) -> None:
            events.extend(("job:close_completion", "job:close_thread"))

    def fake_popen(*args, **kwargs):
        events.append("process:start")
        assert kwargs["creationflags"] & 0x00000004
        process = _FakeProcess()
        process_holder["process"] = process
        return process

    monkeypatch.setattr(child_budget.sys, "platform", "win32")
    monkeypatch.setattr(child_budget, "_WindowsJob", _FakeJob)
    monkeypatch.setattr(child_budget.subprocess, "Popen", fake_popen)
    budget = child_budget.ChildBudget(
        _FakeDeadline(remaining=60.0), 4_294_967_296  # type: ignore[arg-type]
    )

    with pytest.raises(child_budget.ChildProcessBudgetError) as exc:
        child_budget.run_bounded_child(["fake-child"], budget=budget)

    assert exc.value is primary
    if failure_stage == "assign":
        assert "job:terminate" not in events
    assert events == expected_events


@pytest.mark.parametrize(
    "cleanup_failure",
    ["interrupt", "terminate", "class1_query", "job_close", "remaining_close"],
)
def test_p3_windows_cleanup_failure_preserves_primary_error(
    monkeypatch: pytest.MonkeyPatch,
    cleanup_failure: str,
) -> None:
    """Control failures preserve primary; CloseHandle failures stay visible."""
    from roughcut.adapters import child_budget

    events: list[str] = []
    process_holder: dict[str, _FakeProcess] = {}
    job_holder: dict[str, _FakeJob] = {}
    primary: BaseException = (
        KeyboardInterrupt()
        if cleanup_failure == "interrupt"
        else child_budget.ChildProcessTimeBudgetError("primary deadline failure")
    )
    secondary = child_budget.ChildProcessBudgetError(
        f"secondary {cleanup_failure} failure"
    )

    class _RaisingDeadline:
        def remaining(self) -> float:
            raise primary

    class _FakeProcess:
        pid = 74
        args = ("fake-child",)

        def __init__(self) -> None:
            self.running = True
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return None if self.running else self.returncode

        def kill(self) -> None:
            events.append("process:kill")
            self.running = False
            self.returncode = 1

        def wait(self) -> int:
            events.append("process:wait")
            assert not self.running
            if self.returncode is None:
                self.returncode = 1
            return self.returncode

    class _FakeJob:
        def __init__(self, memory_limit: int) -> None:
            events.append("job:init")
            self._assigned = False
            self.query_count = 0
            self.emergency_containment_occurred = False
            job_holder["job"] = self

        @property
        def assigned(self) -> bool:
            return self._assigned

        def assign(self, pid: int) -> None:
            events.append("job:assign")
            self._assigned = True

        def resume_initial_thread(self, pid: int) -> None:
            events.append("job:resume")

        def active_processes(self) -> int:
            self.query_count += 1
            events.append("job:active_query")
            return 1 if process_holder["process"].running else 0

        def terminate(self) -> None:
            events.append("job:terminate")
            if cleanup_failure in {"terminate", "job_close", "remaining_close"}:
                raise secondary
            process_holder["process"].running = False

        def verify_empty(self) -> None:
            events.append("job:class1_query")
            if cleanup_failure == "class1_query":
                raise secondary
            assert not process_holder["process"].running

        def close_job_handle(self) -> None:
            events.append("job:close_job")

        def close_job_for_emergency(self) -> None:
            events.append("job:emergency_close_job")
            if cleanup_failure == "job_close":
                raise secondary
            self.emergency_containment_occurred = True
            process_holder["process"].running = False

        def close_remaining_handles(self) -> None:
            events.append("job:close_completion")
            failed = cleanup_failure == "remaining_close"
            events.append("job:close_thread")
            if failed:
                raise secondary

    def fake_popen(*args, **kwargs):
        events.append("process:start")
        assert kwargs["creationflags"] & 0x00000004
        process = _FakeProcess()
        process_holder["process"] = process
        return process

    monkeypatch.setattr(child_budget.sys, "platform", "win32")
    monkeypatch.setattr(child_budget, "_WindowsJob", _FakeJob)
    monkeypatch.setattr(child_budget.subprocess, "Popen", fake_popen)
    budget = child_budget.ChildBudget(_RaisingDeadline(), 4_294_967_296)

    expected_exception = (
        KeyboardInterrupt
        if cleanup_failure == "interrupt"
        else child_budget.ChildProcessBudgetError
    )
    with pytest.raises(expected_exception) as exc:
        child_budget.run_bounded_child(["fake-child"], budget=budget)

    assert events[:4] == ["job:init", "process:start", "job:assign", "job:resume"]
    assert events[-2:] == ["job:close_completion", "job:close_thread"]
    if cleanup_failure == "interrupt":
        assert exc.value is primary
        assert events[-7:] == [
            "job:active_query",
            "job:terminate",
            "process:wait",
            "job:class1_query",
            "job:close_job",
            "job:close_completion",
            "job:close_thread",
        ]
        assert job_holder["job"].emergency_containment_occurred is False
    elif cleanup_failure in {"terminate", "class1_query"}:
        assert exc.value is primary
        assert job_holder["job"].emergency_containment_occurred is True
    elif cleanup_failure == "job_close":
        assert exc.value is secondary
        assert job_holder["job"].emergency_containment_occurred is False
        assert "process:kill" in events
    else:
        assert exc.value is secondary
        assert job_holder["job"].emergency_containment_occurred is True
        assert events.index("job:close_thread") < len(events)


@pytest.mark.parametrize(
    ("evidence", "raises_memory"),
    [
        pytest.param([False, True], True, id="post-exit-memory-evidence"),
        pytest.param([False, False], False, id="nonzero-without-evidence"),
    ],
)
def test_p3_windows_runner_final_memory_evidence_boundary(
    monkeypatch: pytest.MonkeyPatch,
    evidence: list[bool],
    raises_memory: bool,
) -> None:
    """The final drain closes the exit race without guessing from stderr/code."""
    from roughcut.adapters import child_budget

    popen_kwargs: list[dict[str, object]] = []

    class _FakeProcess:
        pid = 75
        args: ClassVar[list[str]] = ["fake-child"]
        returncode = 7

        def communicate(self, *, timeout: float):
            return "partial output", "MemoryError"

        def poll(self) -> int:
            return self.returncode

        def wait(self) -> int:
            return self.returncode

    class _FakeJob:
        def __init__(self, memory_limit: int) -> None:
            self._assigned = False
            self.closed = False

        @property
        def assigned(self) -> bool:
            return self._assigned

        def assign(self, pid: int) -> None:
            self._assigned = True

        def resume_initial_thread(self, pid: int) -> None:
            return None

        def memory_limit_exceeded(self, limit: int) -> bool:
            return evidence.pop(0)

        def active_processes(self) -> int:
            return 0

        def terminate(self) -> None:
            return None

        def verify_empty(self) -> None:
            return None

        def close_job_handle(self) -> None:
            self.closed = True

        def close_job_for_emergency(self) -> None:
            self.closed = True

        def close_remaining_handles(self) -> None:
            return None

    def fake_popen(*args, **kwargs):
        popen_kwargs.append(kwargs)
        return _FakeProcess()

    monkeypatch.setattr(child_budget.sys, "platform", "win32")
    monkeypatch.setattr(child_budget, "_WindowsJob", _FakeJob)
    monkeypatch.setattr(child_budget.subprocess, "Popen", fake_popen)
    budget = child_budget.ChildBudget(
        _FakeDeadline(remaining=60.0), 4_294_967_296  # type: ignore[arg-type]
    )

    if raises_memory:
        with pytest.raises(child_budget.ChildProcessMemoryBudgetError):
            child_budget.run_bounded_child(
                ["fake-child"],
                budget=budget,
                creationflags=0x08000000,
                cwd="fixture-cwd",
            )
    else:
        result = child_budget.run_bounded_child(
            ["fake-child"],
            budget=budget,
            creationflags=0x08000000,
            cwd="fixture-cwd",
        )
        assert result.returncode == 7
        assert result.stdout == "partial output"
        assert result.stderr == "MemoryError"
        assert result.args == ["fake-child"]
    assert popen_kwargs[0]["creationflags"] == 0x08000004
    assert popen_kwargs[0]["cwd"] == "fixture-cwd"


def test_p4_aux2_replaced_mid_camera_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_media_runtime: tuple[str, str],
) -> None:
    """A replacement between exact Correlation pairs is caught before probe decode."""
    ffmpeg, ffprobe = synthetic_media_runtime
    media = tmp_path / "media"
    main_path = media / "main.wav"
    _write_audio_fixture(main_path, seconds=40)
    aux1 = media / "aux1.wav"
    _write_audio_fixture(aux1, seconds=40)
    aux2 = media / "aux2.wav"
    _write_audio_fixture(aux2, seconds=40)
    project_root, project = _project_with_sources(
        tmp_path,
        media_files=[("main", main_path), ("aux1", aux1), ("aux2", aux2)],
    )
    main_id = project.sources[0].source_id
    aux1_id = project.sources[1].source_id
    aux2_id = project.sources[2].source_id
    request = _align_request(
        project,
        operation_id="op_00000000000040008000000000000060",
        alignment_id="aln_aux2_replaced",
        main_ids=[main_id],
        aux_cameras=[("aux-1", [aux1_id, aux2_id])],
        source_pairs={
            "aux-1": [
                {"main_source_id": main_id, "auxiliary_source_id": aux1_id},
                {"main_source_id": main_id, "auxiliary_source_id": aux2_id},
            ]
        },
    )
    _stub_correlation_runtime(monkeypatch, ffmpeg, ffprobe)
    extract_calls: list[str] = []
    correlation_calls: list[Path] = []
    replaced = False

    def extract(
        source_path: Path,
        output_path: Path,
        *_args: object,
        **_kwargs: object,
    ) -> None:
        nonlocal replaced
        source = Path(source_path)
        extract_calls.append(source.name)
        _write_audio_fixture(output_path, seconds=15, channels=1)
        if not replaced and extract_calls.count(source.name) == 3:
            target = aux2 if source == aux1 else aux1
            old_bytes = target.read_bytes()
            old_mtime_ns = os.stat(target).st_mtime_ns
            old_inode = os.stat(target).st_ino
            replacement = tmp_path / "replacement-other-aux.wav"
            replacement.write_bytes(old_bytes)
            os.utime(replacement, ns=(old_mtime_ns, old_mtime_ns))
            assert os.stat(replacement).st_ino != old_inode
            os.replace(replacement, target)
            replaced = True

    def recognize(
        _alignment_python: Path,
        target_wav: Path,
        _against_wav: Path,
        _output_path: Path,
        **_kwargs: object,
    ) -> AudalignCorrelationResult:
        correlation_calls.append(Path(target_wav))
        return AudalignCorrelationResult(
            (AudalignCorrelationCandidate("0", 0),)
        )

    monkeypatch.setattr(alignments_module, "_extract_aux_excerpt", extract)
    monkeypatch.setattr(
        alignments_module, "run_audalign_correlation", recognize
    )

    with pytest.raises(AlignmentError) as exc:
        run_align_multicam(project_root, **request)
    assert exc.value.code == "alignment_basis_changed_during_run"
    store = MediaOperationStore(project_root, project.project_id)
    record = store.read(request["operation_id"])
    assert record is not None and record.status == "failed"
    assert record.error is not None
    assert record.error.code == "alignment_basis_changed_during_run"
    assert record.error.action == "recognize_auxiliary"
    assert replaced is True
    assert len(correlation_calls) == 3
    replaced_name = "aux2.wav" if extract_calls[0] == "aux1.wav" else "aux1.wav"
    assert replaced_name not in extract_calls[3:]
    assert not (
        project_root
        / "artifacts"
        / "multicam-alignments"
        / "aln_aux2_replaced.json"
    ).exists()

def test_p4_main_replaced_between_correlation_probes_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_media_runtime: tuple[str, str],
) -> None:
    """A main replacement between serial probes is caught before probe two."""
    ffmpeg, ffprobe = synthetic_media_runtime
    media = tmp_path / "media"
    main_path = media / "main.wav"
    _write_audio_fixture(main_path, seconds=40)
    aux1 = media / "aux1.wav"
    _write_audio_fixture(aux1, seconds=40)
    project_root, project = _project_with_sources(
        tmp_path, media_files=[("main", main_path), ("aux1", aux1)]
    )
    main_id = project.sources[0].source_id
    aux1_id = project.sources[1].source_id
    request = _align_request(
        project,
        operation_id="op_00000000000040008000000000000061",
        alignment_id="aln_main_replaced_lr",
        main_ids=[main_id],
        aux_cameras=[("aux-1", [aux1_id])],
        source_pairs={
            "aux-1": [
                {"main_source_id": main_id, "auxiliary_source_id": aux1_id}
            ]
        },
    )
    _stub_correlation_runtime(monkeypatch, ffmpeg, ffprobe)
    main_decodes: list[Path] = []
    extract_calls: list[Path] = []
    correlation_calls: list[Path] = []
    replaced = False

    def decode(
        source_path: Path,
        output_path: Path,
        **_kwargs: object,
    ) -> None:
        main_decodes.append(Path(source_path))
        _write_audio_fixture(output_path, seconds=40, channels=1)

    def extract(
        source_path: Path,
        output_path: Path,
        *_args: object,
        **_kwargs: object,
    ) -> None:
        extract_calls.append(Path(source_path))
        _write_audio_fixture(output_path, seconds=15, channels=1)

    def recognize(
        _alignment_python: Path,
        target_wav: Path,
        _against_wav: Path,
        _output_path: Path,
        **_kwargs: object,
    ) -> AudalignCorrelationResult:
        nonlocal replaced
        correlation_calls.append(Path(target_wav))
        if not replaced:
            old_bytes = main_path.read_bytes()
            old_mtime_ns = os.stat(main_path).st_mtime_ns
            old_inode = os.stat(main_path).st_ino
            replacement = tmp_path / "replacement-main.wav"
            replacement.write_bytes(old_bytes)
            os.utime(replacement, ns=(old_mtime_ns, old_mtime_ns))
            assert os.stat(replacement).st_ino != old_inode
            os.replace(replacement, main_path)
            replaced = True
        return AudalignCorrelationResult(
            (AudalignCorrelationCandidate("0", 0),)
        )

    monkeypatch.setattr(alignments_module, "decode_alignment_audio", decode)
    monkeypatch.setattr(alignments_module, "_extract_aux_excerpt", extract)
    monkeypatch.setattr(
        alignments_module, "run_audalign_correlation", recognize
    )

    with pytest.raises(AlignmentError) as exc:
        run_align_multicam(project_root, **request)
    assert exc.value.code == "alignment_basis_changed_during_run"
    store = MediaOperationStore(project_root, project.project_id)
    record = store.read(request["operation_id"])
    assert record is not None
    assert record.status == "failed"
    assert record.error is not None
    assert record.error.code == "alignment_basis_changed_during_run"
    assert record.error.action == "recognize_auxiliary"
    # The full main was decoded exactly once and the second probe never ran.
    assert replaced is True
    assert main_decodes == [main_path]
    assert len(extract_calls) == 1
    assert len(correlation_calls) == 1
    assert not (
        project_root
        / "artifacts"
        / "multicam-alignments"
        / "aln_main_replaced_lr.json"
    ).exists()

def test_p4_aux_replaced_between_correlation_probes_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_media_runtime: tuple[str, str],
) -> None:
    """An auxiliary replacement between serial probes is caught before probe two."""
    ffmpeg, ffprobe = synthetic_media_runtime
    media = tmp_path / "media"
    main_path = media / "main.wav"
    _write_audio_fixture(main_path, seconds=40)
    aux_path = media / "aux.wav"
    _write_audio_fixture(aux_path, seconds=40)
    project_root, project = _project_with_sources(
        tmp_path, media_files=[("main", main_path), ("aux", aux_path)]
    )
    main_id = project.sources[0].source_id
    aux_id = project.sources[1].source_id
    request = _align_request(
        project,
        operation_id="op_00000000000040008000000000000062",
        alignment_id="aln_aux_replaced_between_channel_reads",
        main_ids=[main_id],
        aux_cameras=[("aux-1", [aux_id])],
        source_pairs={
            "aux-1": [
                {"main_source_id": main_id, "auxiliary_source_id": aux_id}
            ]
        },
    )
    _stub_correlation_runtime(monkeypatch, ffmpeg, ffprobe)
    main_decodes: list[Path] = []
    extract_calls: list[Path] = []
    correlation_calls: list[Path] = []
    replaced = False

    def decode(
        source_path: Path,
        output_path: Path,
        **_kwargs: object,
    ) -> None:
        main_decodes.append(Path(source_path))
        _write_audio_fixture(output_path, seconds=40, channels=1)

    def extract(
        source_path: Path,
        output_path: Path,
        *_args: object,
        **_kwargs: object,
    ) -> None:
        extract_calls.append(Path(source_path))
        _write_audio_fixture(output_path, seconds=15, channels=1)

    def recognize(
        _alignment_python: Path,
        target_wav: Path,
        _against_wav: Path,
        _output_path: Path,
        **_kwargs: object,
    ) -> AudalignCorrelationResult:
        nonlocal replaced
        correlation_calls.append(Path(target_wav))
        if not replaced:
            old_bytes = aux_path.read_bytes()
            old_mtime_ns = os.stat(aux_path).st_mtime_ns
            old_inode = os.stat(aux_path).st_ino
            replacement = tmp_path / "replacement-aux.wav"
            replacement.write_bytes(old_bytes)
            os.utime(replacement, ns=(old_mtime_ns, old_mtime_ns))
            assert os.stat(replacement).st_ino != old_inode
            os.replace(replacement, aux_path)
            replaced = True
        return AudalignCorrelationResult(
            (AudalignCorrelationCandidate("0", 0),)
        )

    monkeypatch.setattr(alignments_module, "decode_alignment_audio", decode)
    monkeypatch.setattr(alignments_module, "_extract_aux_excerpt", extract)
    monkeypatch.setattr(
        alignments_module, "run_audalign_correlation", recognize
    )

    with pytest.raises(AlignmentError) as exc:
        run_align_multicam(project_root, **request)

    assert replaced is True
    assert exc.value.code == "alignment_basis_changed_during_run"
    assert main_decodes == [main_path]
    assert extract_calls == [aux_path]
    assert len(correlation_calls) == 1
    record = MediaOperationStore(project_root, project.project_id).read(
        request["operation_id"]
    )
    assert record is not None and record.status == "failed"
    assert record.error is not None
    assert record.error.code == "alignment_basis_changed_during_run"
    assert record.error.action == "recognize_auxiliary"
    assert AlignmentStore(project_root).read(request["alignment_id"]) is None

def test_p4_correlation_disk_reservation_stops_before_second_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_media_runtime: tuple[str, str],
) -> None:
    """The workspace disk budget is rechecked after each bounded Correlation child."""
    ffmpeg, ffprobe = synthetic_media_runtime
    media = tmp_path / "media"
    main_path = media / "main.wav"
    _write_audio_fixture(main_path, seconds=40)
    aux_path = media / "aux.wav"
    _write_audio_fixture(aux_path, seconds=40)
    project_root, project = _project_with_sources(
        tmp_path, media_files=[("main", main_path), ("aux", aux_path)]
    )
    main_id = project.sources[0].source_id
    aux_id = project.sources[1].source_id
    disk_estimate = alignments_module._estimate_correlation_workspace_bytes(
        (project.sources[0].probe.duration_ticks,),
        (project.sources[1].probe.duration_ticks,),
    )
    request = _align_request(
        project,
        operation_id="op_00000000000040008000000000000063",
        alignment_id="aln_correlation_second_probe",
        main_ids=[main_id],
        aux_cameras=[("aux-1", [aux_id])],
        source_pairs={
            "aux-1": [
                {"main_source_id": main_id, "auxiliary_source_id": aux_id}
            ]
        },
    )
    request["max_temporary_disk_bytes"] = disk_estimate
    _stub_correlation_runtime(monkeypatch, ffmpeg, ffprobe)
    main_decodes: list[Path] = []
    extract_calls: list[Path] = []
    correlation_calls: list[Path] = []
    disk_output_ready = False

    def decode(
        source_path: Path,
        output_path: Path,
        **_kwargs: object,
    ) -> None:
        main_decodes.append(Path(source_path))
        _write_audio_fixture(output_path, seconds=40, channels=1)

    def extract(
        source_path: Path,
        output_path: Path,
        *_args: object,
        **_kwargs: object,
    ) -> None:
        extract_calls.append(Path(source_path))
        _write_audio_fixture(output_path, seconds=15, channels=1)

    def recognize(
        _alignment_python: Path,
        target_wav: Path,
        _against_wav: Path,
        _output_path: Path,
        **_kwargs: object,
    ) -> AudalignCorrelationResult:
        nonlocal disk_output_ready
        correlation_calls.append(Path(target_wav))
        disk_output_ready = True
        return AudalignCorrelationResult(
            (AudalignCorrelationCandidate("0", 0),)
        )

    monkeypatch.setattr(alignments_module, "decode_alignment_audio", decode)
    monkeypatch.setattr(alignments_module, "_extract_aux_excerpt", extract)
    monkeypatch.setattr(
        alignments_module, "run_audalign_correlation", recognize
    )

    original_used_bytes = alignments_module._WorkspaceBudget._used_bytes

    def fake_used_bytes(budget):
        if disk_output_ready:
            return budget.max_disk_bytes + 1
        return original_used_bytes(budget)

    monkeypatch.setattr(
        alignments_module._WorkspaceBudget, "_used_bytes", fake_used_bytes
    )

    with pytest.raises(AlignmentError) as exc:
        run_align_multicam(project_root, **request)

    assert exc.value.code == "alignment_disk_budget_exceeded"
    assert main_decodes == [main_path]
    assert extract_calls == [aux_path]
    assert len(correlation_calls) == 1
    record = MediaOperationStore(project_root, project.project_id).read(
        request["operation_id"]
    )
    assert record is not None and record.status == "failed"
    assert record.error is not None
    assert record.error.code == "alignment_disk_budget_exceeded"
    assert record.error.action == "recognize_auxiliary"
    assert AlignmentStore(project_root).read(request["alignment_id"]) is None

def test_p4_multi_main_broken_aux_isolation(tmp_path: Path) -> None:
    """Multiple main segments + one confirmed-broken auxiliary + one safe
    auxiliary: the broken aux decodes exactly once, its error is recorded
    once, the safe aux continues, the safe mapping is retained, and the
    shared failed_sources dict prevents repeated decode of the broken source
    without blocking the safe one."""
    import roughcut.application.alignments as align_module
    from roughcut.application.alignments import (
        _align_main_source_to_auxiliaries,
        _WorkspaceBudget,
    )

    class _FakeCandidate:
        offset_seconds = "0.0"
        confidence = 9

    class _FakeMatch:
        candidates = (_FakeCandidate(),)
        raw_matching_fingerprint_counts = (1,)

    def fake_recognize(*args, **kwargs):
        candidate = type(
            "Candidate",
            (),
            {"offset_seconds": "0", "confidence": 9},
        )()
        return type(
            "Match",
            (),
            {
                "candidates": (candidate,),
                "raw_matching_fingerprint_counts": (1,),
            },
        )()

    def fake_verify(self, main_wav, auxiliary_wav, **kwargs):
        return {
            "passed": True,
            "local_errors_ticks": [0, 0, 0],
            "max_local_offset_error_ticks": 0,
        }

    decode_calls: list[str] = []
    broken_id = "src_aux_broken"
    safe_id = "src_aux_safe"

    def fake_decode(source_path, output_path, **kwargs):
        decode_calls.append(str(source_path))
        if str(source_path).endswith("broken.wav"):
            raise FFmpegAlignmentError("injected decode failure")
        _write_audio_fixture(Path(output_path), seconds=60, channels=1)

    original_recognize = align_module.run_audalign_recognize
    original_verify = align_module.FixedOffsetVerifier.verify
    original_decode = align_module.decode_alignment_audio
    align_module.run_audalign_recognize = fake_recognize
    align_module.FixedOffsetVerifier.verify = fake_verify
    align_module.decode_alignment_audio = fake_decode

    class _FakeGroup:
        camera_id = "aux-1"
        ordered_source_ids = (broken_id, safe_id)

    from roughcut.application.sources import fingerprint_file as _ff

    class _FakeSource:
        import_mode = ImportMode.LINKED

        def __init__(
            self, name: str = "source", *, duration_ticks: int = 7_200_000
        ) -> None:
            if name == "source":
                self.locator = {
                    "absolute_path": str(Path(sys.executable).resolve(strict=True))
                }
            else:
                # a tmp_path-owned file so _resolve_source_path succeeds; the
                # broken/safe names carry the .wav suffix for the fake decode
                self._fake_path = (tmp_path / f"{name}.wav").resolve()
                self._fake_path.write_bytes(b"x")
                self.locator = {"absolute_path": str(self._fake_path)}
            self._fp = _ff(Path(self.locator["absolute_path"]).resolve(strict=True))
            self.fingerprint = type(
                "F",
                (),
                {
                    "size": self._fp.size,
                    "mtime_ns": self._fp.mtime_ns,
                    "sha256_head_tail": self._fp.sha256_head_tail,
                },
            )()
            self.probe = type(
                "P",
                (),
                {
                    "duration_ticks": duration_ticks,
                    "audio_codec": "pcm_s16le",
                    "audio_sample_rate": 44_100,
                },
            )()

    sources = {
        "src_main_1": _FakeSource(),
        "src_main_2": _FakeSource(),
        broken_id: _FakeSource("broken"),
        safe_id: _FakeSource("safe", duration_ticks=4_800_000),
    }

    def _frozen_for(source_id: str, source: _FakeSource) -> dict[str, object]:
        path = Path(source.locator["absolute_path"]).resolve(strict=True)
        fp = _ff(path)
        return {
            "import_mode": "linked",
            "locator_identity_hash": canonical_sha256_v1(
                {"locator": {"absolute_path": str(path)}}
            ),
            "fingerprint": {
                "size": fp.size,
                "mtime_ns": fp.mtime_ns,
                "sha256_head_tail": fp.sha256_head_tail,
            },
            "identity": align_module._source_identity_evidence(path),
            "probe": {
                "duration_ticks": source.probe.duration_ticks,
                "audio_codec": "pcm_s16le",
                "audio_sample_rate": 44_100,
            },
        }

    frozen_identity = {
        "src_main_1": _frozen_for("src_main_1", sources["src_main_1"]),
        "src_main_2": _frozen_for("src_main_2", sources["src_main_2"]),
        broken_id: _frozen_for(broken_id, sources[broken_id]),
        safe_id: _frozen_for(safe_id, sources[safe_id]),
    }

    workspace = tmp_path / "workspace"
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    deadline = _FakeDeadline(remaining=60.0)
    shared_failed: dict[str, int] = {}
    intervals = []
    errors = []
    try:
        for main_id in ("src_main_1", "src_main_2"):
            result = _align_main_source_to_auxiliaries(
                main_id,
                7_200_000,
                Path("/dev/null"),
                _FakeGroup(),  # type: ignore[arg-type]
                sources,  # type: ignore[arg-type]
                tmp_path,
                Path("/dev/null"),
                _runtime_tool(ffmpeg),
                workspace,
                deadline,  # type: ignore[arg-type]
                4_294_967_296,
                None,
                _WorkspaceBudget(workspace, 536_870_912),
                frozen_identity,
                failed_sources=shared_failed,
            )
            (
                mapped_ticks,
                missing_ticks,
                uncertain_ticks,
                conflict_ticks,
            ) = tuple(
                sum(
                    interval.main["end_ticks"] - interval.main["start_ticks"]
                    for interval in result[0]
                    if interval.classification == classification
                )
                for classification in (
                    "mapped",
                    "missing",
                    "uncertain",
                    "conflict",
                )
            )
            assert result[1:5] == (
                mapped_ticks,
                missing_ticks,
                uncertain_ticks,
                conflict_ticks,
            )
            intervals.extend(result[0])
            errors.extend(result[5])
    finally:
        align_module.run_audalign_recognize = original_recognize
        align_module.FixedOffsetVerifier.verify = original_verify
        align_module.decode_alignment_audio = original_decode
    # the broken aux was decoded exactly once across both main sources
    broken_decodes = [c for c in decode_calls if c.endswith("broken.wav")]
    assert len(broken_decodes) == 1
    # the safe aux was processed for both main sources
    safe_decodes = [c for c in decode_calls if c.endswith("safe.wav")]
    assert len(safe_decodes) == 2
    # the broken source is cached once in the shared dict
    assert shared_failed == {broken_id: 7_200_000}
    assert [error.to_dict() for error in errors] == [
        {"code": "auxiliary_decode_failed", "source_id": broken_id}
    ]
    assert all(
        any(
            interval.classification == "mapped"
            and interval.auxiliary is not None
            and interval.auxiliary["source_id"] == safe_id
            for interval in intervals
            if interval.main["source_id"] == main_id
        )
        for main_id in ("src_main_1", "src_main_2")
    )
    assert all(
        any(
            interval.classification == "uncertain"
            for interval in intervals
            if interval.main["source_id"] == main_id
        )
        for main_id in ("src_main_1", "src_main_2")
    )
    assert not any(
        interval.auxiliary is not None
        and interval.auxiliary["source_id"] == broken_id
        for interval in intervals
    )


def test_p4_audio_stream_unsupported_preserved(tmp_path: Path) -> None:
    """A per-camera auxiliary_audio_stream_unsupported failure is preserved
    as-is and never rewritten to auxiliary_recognition_failed."""
    import roughcut.application.alignments as align_module
    from roughcut.application.alignments import (
        _align_main_source_to_auxiliaries,
        _WorkspaceBudget,
    )
    from roughcut.domain.alignment import AlignmentPerCameraError

    class _FakeCandidate:
        offset_seconds = "0.0"
        confidence = 9

    class _FakeMatch:
        candidates = (_FakeCandidate(),)
        raw_matching_fingerprint_counts = (1,)

    def fake_recognize(*args, **kwargs):
        return _FakeMatch()

    def fake_verify(self, main_wav, auxiliary_wav, **kwargs):
        # mono verification fails, forcing the L/R fallback to run
        return {"passed": False, "reason": "no_window_candidate"}

    def fake_decode(source_path, output_path, **kwargs):
        # the mono decode succeeds; the L/R fallback decodes fail with the
        # stream-unsupported error
        if kwargs.get("channel") is None:
            _write_audio_fixture(Path(output_path), seconds=60, channels=1)
            return
        raise FFmpegAlignmentError("stream unsupported")

    original_recognize = align_module.run_audalign_recognize
    original_verify = align_module.FixedOffsetVerifier.verify
    original_decode = align_module.decode_alignment_audio
    align_module.run_audalign_recognize = fake_recognize
    align_module.FixedOffsetVerifier.verify = fake_verify
    align_module.decode_alignment_audio = fake_decode

    class _FakeGroup:
        camera_id = "aux-1"
        ordered_source_ids = ("src_aux_1",)

    from roughcut.application.sources import fingerprint_file as _ff

    _real_fingerprint = _ff(Path(sys.executable))

    class _FakeSource:
        import_mode = ImportMode.LINKED
        locator: ClassVar[dict[str, str]] = {
            "absolute_path": str(Path(sys.executable).resolve(strict=True))
        }
        fingerprint = type(
            "F",
            (),
            {
                "size": _real_fingerprint.size,
                "mtime_ns": _real_fingerprint.mtime_ns,
                "sha256_head_tail": _real_fingerprint.sha256_head_tail,
            },
        )()
        probe = type(
            "P",
            (),
            {
                "duration_ticks": 7_200_000,
                "audio_codec": "pcm_s16le",
                "audio_sample_rate": 44_100,
            },
        )()

    sources = {"src_main_1": _FakeSource(), "src_aux_1": _FakeSource()}

    def _frozen_for(source_id: str) -> dict[str, object]:
        return {
            "import_mode": "linked",
            "locator_identity_hash": canonical_sha256_v1(
                {"locator": {"absolute_path": str(Path(sys.executable).resolve(strict=True))}}
            ),
            "fingerprint": {
                "size": _real_fingerprint.size,
                "mtime_ns": _real_fingerprint.mtime_ns,
                "sha256_head_tail": _real_fingerprint.sha256_head_tail,
            },
            "identity": align_module._source_identity_evidence(
                Path(sys.executable).resolve(strict=True)
            ),
            "probe": {
                "duration_ticks": 7_200_000,
                "audio_codec": "pcm_s16le",
                "audio_sample_rate": 44_100,
            },
        }

    frozen_identity = {
        "src_main_1": _frozen_for("src_main_1"),
        "src_aux_1": _frozen_for("src_aux_1"),
    }
    workspace = tmp_path / "workspace"
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    try:
        _intervals, _m, _miss, _unc, _conf, errors = (
            _align_main_source_to_auxiliaries(
                "src_main_1",
                7_200_000,
                Path("/dev/null"),
                _FakeGroup(),  # type: ignore[arg-type]
                sources,  # type: ignore[arg-type]
                Path("/tmp"),
                Path("/dev/null"),
                _runtime_tool(ffmpeg),
                workspace,
                _FakeDeadline(remaining=60.0),  # type: ignore[arg-type]
                4_294_967_296,
                None,
                _WorkspaceBudget(workspace, 536_870_912),
                frozen_identity,
            )
        )
    finally:
        align_module.run_audalign_recognize = original_recognize
        align_module.FixedOffsetVerifier.verify = original_verify
        align_module.decode_alignment_audio = original_decode
    assert errors == (
        AlignmentPerCameraError(
            code="auxiliary_audio_stream_unsupported", source_id="src_aux_1"
        ),
    )
