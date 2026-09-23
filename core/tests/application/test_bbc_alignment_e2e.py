"""Offline BBC-writer e2e with a stub finder and real FFmpeg windows."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import wave
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from roughcut.adapters.alignment_store import AlignmentStore
from roughcut.adapters.audio_offset_finder import (
    BbcOffsetAnalysis,
    BbcOffsetResult,
)
from roughcut.adapters.child_budget import ChildBudget
from roughcut.application import alignments
from roughcut.application.media_operations import _PersistentMediaRuntime
from roughcut.application.projects import create_project
from roughcut.application.sources import add_source
from roughcut.domain.alignment import (
    ALIGNMENT_MAXIMUM_LOCAL_ERROR_TICKS,
    BBC_WRITER_PROFILE,
    alignment_request_projection,
    hash_alignment_request,
)
from roughcut.domain.project import ImportMode
from roughcut.domain.render import ToolResolution

SAMPLE_RATE = 8_000
OFFSET_SAMPLES = 2 * SAMPLE_RATE


def _noise(seed: int, seconds: int = 40) -> list[int]:
    state = seed
    samples: list[int] = []
    for index in range(seconds * SAMPLE_RATE):
        state = (state * 1_103_515_245 + 12_345) % (1 << 31)
        level = 0.05 + 0.9 * (
            ((index // SAMPLE_RATE * 37 + seed * 13) % 29) / 28
        )
        samples.append(int((state / (1 << 31) - 0.5) * 50_000 * level))
    return samples


def _write_wav(path: Path, samples: list[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(
            b"".join(value.to_bytes(2, "little", signed=True) for value in samples)
        )


def _flat_noise(seed: int, seconds: int = 40) -> list[int]:
    state = seed
    samples: list[int] = []
    for _index in range(seconds * SAMPLE_RATE):
        state = (state * 1_664_525 + 1_013_904_223) % (1 << 32)
        samples.append(int((state / (1 << 32) - 0.5) * 40_000))
    return samples


def _runtime(ffmpeg: str, ffprobe: str) -> _PersistentMediaRuntime:
    def tool(command: str) -> ToolResolution:
        version = subprocess.run(
            [command, "-version"], check=True, capture_output=True, text=True
        ).stdout.splitlines()[0]
        return ToolResolution(command, command, version)

    selection_payload = {
        "provider": "bbc_audio_offset_finder",
        "provider_version": "0.5.5",
        "interpreter": "/isolated/test/bbc/python",
    }
    selection = SimpleNamespace(
        provider="bbc_audio_offset_finder",
        provider_version="0.5.5",
        to_dict=lambda: dict(selection_payload),
    )
    return _PersistentMediaRuntime(
        binding=SimpleNamespace(alignment_python=selection),
        runtime_binding_sha256="a" * 64,
        python_receipt_hash="b" * 64,
        ffmpeg_tool_selection_hash="c" * 64,
        ffprobe_tool_selection_hash="d" * 64,
        ffmpeg=tool(ffmpeg),
        ffprobe=tool(ffprobe),
    )


def _run_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    main_samples: list[int],
    auxiliary_samples: list[int],
    native_b: str,
    suffix: str,
):
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.fail("BBC e2e requires fixture FFmpeg and FFprobe")
    main_path = tmp_path / suffix / "main.wav"
    auxiliary_path = tmp_path / suffix / "auxiliary.wav"
    _write_wav(main_path, main_samples)
    _write_wav(auxiliary_path, auxiliary_samples)
    root = tmp_path / suffix / "project"
    project = create_project(root, "BBC offline e2e")
    for path in (main_path, auxiliary_path):
        project = add_source(
            root, path, ImportMode.LINKED, expected_revision=project.revision
        )
    main_id, auxiliary_id = (source.source_id for source in project.sources)
    runtime = _runtime(ffmpeg, ffprobe)
    monkeypatch.setattr(alignments, "_load_persistent_runtime", lambda: runtime)
    monkeypatch.setattr(
        alignments, "validate_bbc_selection", lambda selection: selection
    )
    monkeypatch.setattr(
        alignments,
        "run_bbc_offset_finder",
        lambda *_args, **_kwargs: BbcOffsetResult(
            native_b, "7.5", BbcOffsetAnalysis()
        ),
    )
    request = {
        "operation_id": f"op_00000000000040008000000000000{suffix}",
        "alignment_id": f"aln_bbc_e2e_{suffix}",
        "expected_revision": project.revision,
        "main_camera": {"camera_id": "main", "ordered_source_ids": [main_id]},
        "auxiliary_cameras": [
            {
                "camera_id": "aux-1",
                "ordered_source_ids": [auxiliary_id],
                "source_pairs": [
                    {
                        "main_source_id": main_id,
                        "auxiliary_source_id": auxiliary_id,
                    }
                ],
            }
        ],
        "main_audio_stable": True,
        "max_temporary_disk_bytes": 536_870_912,
        "max_analysis_memory_bytes": 4_294_967_296,
        "max_runtime_seconds": 120,
    }
    root, project, store = alignments._project_context(root)
    main_group, request_groups = alignments.parse_alignment_request_groups(
        request["main_camera"], request["auxiliary_cameras"]
    )
    selected_main_group, pair_groups = alignments._build_pair_groups(
        main_group, request_groups
    )
    auxiliary_groups = tuple(group.camera for group in pair_groups)
    sources = alignments._resolve_groups(
        root,
        project,
        main_group,
        auxiliary_groups,
        selected_source_ids=alignments._pair_source_ids(
            selected_main_group, pair_groups
        ),
    )
    frozen_identity = alignments._snapshot_requested_identities(
        root, project, sources, runtime
    )
    workspace = Path(tempfile.mkdtemp(prefix="roughcut-historical-bbc-e2e-"))
    workspace_budget = alignments._WorkspaceBudget(
        workspace, max_disk_bytes=request["max_temporary_disk_bytes"]
    )
    deadline = alignments._Deadline(request["max_runtime_seconds"])
    child_budget = replace(
        ChildBudget(deadline, request["max_analysis_memory_bytes"]),
        apply_tmpdir=workspace_budget.apply_tmpdir,
    )
    projection = alignment_request_projection(
        scope=dict(store.scope.to_dict()),
        operation_id=request["operation_id"],
        alignment_id=request["alignment_id"],
        expected_revision=request["expected_revision"],
        main_camera=request["main_camera"],
        auxiliary_cameras=request["auxiliary_cameras"],
        main_audio_stable=request["main_audio_stable"],
        max_temporary_disk_bytes=request["max_temporary_disk_bytes"],
        max_analysis_memory_bytes=request["max_analysis_memory_bytes"],
        max_runtime_seconds=request["max_runtime_seconds"],
        writer_profile=BBC_WRITER_PROFILE,
    )
    try:
        artifact = alignments._execute_alignment(
            root,
            project,
            store.scope,
            sources,
            selected_main_group,
            auxiliary_groups,
            runtime.binding.alignment_python,
            runtime.ffmpeg,
            workspace,
            hash_alignment_request(projection),
            "e" * 64,
            projection,
            runtime,
            deadline,
            request["max_analysis_memory_bytes"],
            child_budget,
            workspace_budget,
            frozen_identity,
            lambda _phase: None,
            request["operation_id"],
            request["alignment_id"],
            pair_groups=pair_groups,
        )
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    assert AlignmentStore(root).read(request["alignment_id"]) == artifact
    return artifact


@pytest.mark.parametrize(
    ("reverse", "native_b", "expected_b", "suffix"),
    [(False, "2", 240_000, "901"), (True, "-2", -240_000, "902")],
)
def test_real_ffmpeg_short_windows_map_signed_b(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reverse: bool,
    native_b: str,
    expected_b: int,
    suffix: str,
) -> None:
    base = _noise(0x5EED_1234)
    long_samples, short_samples = base, base[OFFSET_SAMPLES:]
    main, auxiliary = (
        (short_samples, long_samples) if reverse else (long_samples, short_samples)
    )
    artifact = _run_pair(
        tmp_path,
        monkeypatch,
        main_samples=main,
        auxiliary_samples=auxiliary,
        native_b=native_b,
        suffix=suffix,
    )
    mapped = [item for item in artifact.intervals if item.classification == "mapped"]
    assert mapped
    assert abs(int(mapped[0].evidence["refined_b_ticks"]) - expected_b) <= (
        ALIGNMENT_MAXIMUM_LOCAL_ERROR_TICKS
    )


def test_real_ffmpeg_uncorrelated_pair_stays_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _run_pair(
        tmp_path,
        monkeypatch,
        main_samples=_noise(101),
        auxiliary_samples=_flat_noise(202),
        native_b="0",
        suffix="903",
    )
    assert artifact.summary.mapped_ticks == 0
    assert all(item.classification != "mapped" for item in artifact.intervals)
