from __future__ import annotations

import shutil
import subprocess
import tempfile
import wave
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from roughcut.adapters.alignment_store import AlignmentStore
from roughcut.adapters.audio_offset_finder import (
    BbcAdapterError,
    BbcMemoryError,
    BbcNoOffsetError,
    BbcOffsetAnalysis,
    BbcOffsetResult,
    BbcTimeoutError,
)
from roughcut.adapters.child_budget import ChildBudget
from roughcut.application import alignments
from roughcut.application.alignments import run_align_multicam
from roughcut.application.bbc_alignment import empty_bbc_evidence
from roughcut.application.media_operations import _PersistentMediaRuntime
from roughcut.application.projects import create_project
from roughcut.application.sources import add_source
from roughcut.domain.alignment import (
    BBC_WRITER_PROFILE,
    WAVEFORM_WRITER_PROFILE,
    AlignmentError,
    alignment_request_projection,
    hash_alignment_request,
)
from roughcut.domain.media_operation import AlignmentOperationResult, MediaOperationError
from roughcut.domain.project import ImportMode
from roughcut.domain.render import ToolResolution


def _write_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(b"\0\0" * 8_000 * 40)


def _selection(provider: str = "bbc_audio_offset_finder") -> SimpleNamespace:
    payload = {
        "provider": provider,
        "provider_version": "0.5.5",
        "interpreter": "/managed/bbc/bin/python",
    }
    return SimpleNamespace(
        provider=provider,
        provider_version="0.5.5",
        to_dict=lambda: dict(payload),
    )


def _runtime(ffmpeg: str, ffprobe: str, selection: object) -> _PersistentMediaRuntime:
    ffmpeg_version = subprocess.run(
        [ffmpeg, "-version"], check=True, capture_output=True, text=True
    ).stdout.splitlines()[0]
    ffprobe_version = subprocess.run(
        [ffprobe, "-version"], check=True, capture_output=True, text=True
    ).stdout.splitlines()[0]
    return _PersistentMediaRuntime(
        binding=SimpleNamespace(alignment_python=selection),
        runtime_binding_sha256="a" * 64,
        python_receipt_hash="b" * 64,
        ffmpeg_tool_selection_hash="c" * 64,
        ffprobe_tool_selection_hash="d" * 64,
        ffmpeg=ToolResolution(ffmpeg, ffmpeg, ffmpeg_version),
        ffprobe=ToolResolution(ffprobe, ffprobe, ffprobe_version),
    )


def _project(tmp_path: Path, names: tuple[str, ...]):
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("BBC application test needs fixture FFmpeg")
    root = tmp_path / "project"
    project = create_project(root, "BBC alignment")
    for name in names:
        media = tmp_path / f"{name}.wav"
        _write_wav(media)
        project = add_source(
            root, media, ImportMode.LINKED, expected_revision=project.revision
        )
    return root, project, ffmpeg, ffprobe


def _request(project, *, pairs: list[dict[str, str]] | None = None):
    auxiliary: dict[str, object] = {
        "camera_id": "aux-1",
        "ordered_source_ids": [source.source_id for source in project.sources[1:]],
    }
    if pairs is not None:
        auxiliary["source_pairs"] = pairs
    return {
        "operation_id": "op_00000000000040008000000000000901",
        "alignment_id": "aln_bbc_production",
        "expected_revision": project.revision,
        "main_camera": {
            "camera_id": "main",
            "ordered_source_ids": [project.sources[0].source_id],
        },
        "auxiliary_cameras": [auxiliary],
        "main_audio_stable": True,
        "max_temporary_disk_bytes": 536_870_912,
        "max_analysis_memory_bytes": 4_294_967_296,
        "max_runtime_seconds": 30,
    }


def _mapped_result(**kwargs: object) -> dict[str, object]:
    native = str(kwargs["native_offset_seconds"])
    initial = int(kwargs["initial_b_ticks"])
    score = str(kwargs["standard_score"])
    evidence = empty_bbc_evidence(
        "fixed_offset_verified",
        native_offset_seconds=native,
        initial_b_ticks=initial,
        standard_score=score,
    )
    main_start = max(0, initial)
    main_end = min(4_800_000, 4_800_000 + initial)
    middle_start = (main_start + main_end - 1_200_000) // 2
    evidence.update(
        {
            "channel": "mono",
            "refined_b_ticks": initial,
            "refined_correlation": "0.9",
            "refined_geometry": {
                "main_start_ticks": main_start,
                "main_end_ticks": main_end,
                "auxiliary_start_ticks": main_start - initial,
                "auxiliary_end_ticks": main_end - initial,
            },
            "verification_windows": [
                {
                    "label": label,
                    "main_start_ticks": start,
                    "main_end_ticks": start + 1_200_000,
                    "auxiliary_start_ticks": start - initial,
                    "auxiliary_end_ticks": start - initial + 1_200_000,
                    "b_ticks": initial,
                    "local_error_ticks": 0,
                    "correlation": "0.9",
                }
                for label, start in (
                    ("opening", main_start),
                    ("middle", middle_start),
                    ("ending", main_end - 1_200_000),
                )
            ],
            "verification_window_count": 3,
            "max_local_offset_error_ticks": 0,
        }
    )
    return {"classifications": "mapped", "b_ticks": initial, "evidence": evidence}


def _decode_failed_result(**kwargs: object) -> dict[str, object]:
    evidence = empty_bbc_evidence(
        "decode_failed",
        native_offset_seconds=str(kwargs["native_offset_seconds"]),
        initial_b_ticks=int(kwargs["initial_b_ticks"]),
        standard_score=str(kwargs["standard_score"]),
    )
    evidence["channel"] = "right"
    return {"classifications": "uncertain", "evidence": evidence}


def _install_stubs(
    monkeypatch: pytest.MonkeyPatch,
    runtime: _PersistentMediaRuntime,
) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(alignments, "_load_persistent_runtime", lambda: runtime)
    monkeypatch.setattr(
        alignments,
        "validate_bbc_selection",
        lambda selection: selection,
    )
    monkeypatch.setattr(
        alignments,
        "run_bounded_child",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                runtime.ffmpeg.version + "\n"
                if command[0] == runtime.ffmpeg.command
                else runtime.ffprobe.version + "\n"
            ),
            stderr="",
        ),
    )

    def finder(main: Path, auxiliary: Path, **_kwargs: object) -> BbcOffsetResult:
        assert _kwargs["ffmpeg_command"] == runtime.ffmpeg.command
        calls.append((main.name, auxiliary.name))
        return BbcOffsetResult("0.5", "7.5", BbcOffsetAnalysis())

    monkeypatch.setattr(alignments, "run_bbc_offset_finder", finder)
    monkeypatch.setattr(alignments, "analyze_bbc_pair", _mapped_result)
    monkeypatch.setattr(
        alignments,
        "run_audalign_recognize",
        lambda *_args, **_kwargs: pytest.fail("BBC writer fell back to Audalign"),
    )
    monkeypatch.setattr(
        alignments,
        "analyze_waveform_pair",
        lambda *_args, **_kwargs: pytest.fail("BBC writer used old coarse runner"),
    )
    return calls


def _run_historical_bbc(
    root: Path,
    request: dict[str, object],
    runtime: _PersistentMediaRuntime,
) -> SimpleNamespace:
    """Exercise the frozen BBC helper without routing current production writes."""
    root, project, store = alignments._project_context(root)
    main_group, request_groups = alignments.parse_alignment_request_groups(
        request["main_camera"], request["auxiliary_cameras"]
    )
    selected_main_group, pair_groups = alignments._build_pair_groups(
        main_group, request_groups
    )
    auxiliary_groups = tuple(group.camera for group in pair_groups)
    selected_source_ids = alignments._pair_source_ids(
        selected_main_group, pair_groups
    )
    sources = alignments._resolve_groups(
        root,
        project,
        main_group,
        auxiliary_groups,
        selected_source_ids=selected_source_ids,
    )
    frozen_identity = alignments._snapshot_requested_identities(
        root, project, sources, runtime
    )
    workspace = Path(tempfile.mkdtemp(prefix="roughcut-historical-bbc-"))
    workspace_budget = alignments._WorkspaceBudget(
        workspace,
        max_disk_bytes=int(request["max_temporary_disk_bytes"]),
    )
    deadline = alignments._Deadline(int(request["max_runtime_seconds"]))
    child_budget = ChildBudget(
        deadline,
        int(request["max_analysis_memory_bytes"]),
    )
    child_budget = replace(child_budget, apply_tmpdir=workspace_budget.apply_tmpdir)
    projection = alignment_request_projection(
        scope=dict(store.scope.to_dict()),
        operation_id=str(request["operation_id"]),
        alignment_id=str(request["alignment_id"]),
        expected_revision=int(request["expected_revision"]),
        main_camera=request["main_camera"],
        auxiliary_cameras=request["auxiliary_cameras"],
        main_audio_stable=bool(request["main_audio_stable"]),
        max_temporary_disk_bytes=int(request["max_temporary_disk_bytes"]),
        max_analysis_memory_bytes=int(request["max_analysis_memory_bytes"]),
        max_runtime_seconds=int(request["max_runtime_seconds"]),
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
            int(request["max_analysis_memory_bytes"]),
            child_budget,
            workspace_budget,
            frozen_identity,
            lambda _phase: None,
            str(request["operation_id"]),
            str(request["alignment_id"]),
            pair_groups=pair_groups,
        )
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    return SimpleNamespace(artifact=artifact)


def test_production_writer_uses_exact_pair_and_direct_native_b(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, project, ffmpeg, ffprobe = _project(tmp_path, ("main", "aux"))
    runtime = _runtime(ffmpeg, ffprobe, _selection())
    calls = _install_stubs(monkeypatch, runtime)
    outcome = _run_historical_bbc(root, _request(project), runtime)
    assert outcome.artifact is not None
    artifact = outcome.artifact
    assert calls == [("main.wav", "aux.wav")]
    assert artifact.algorithm.name == "bbc_audio_offset_finder_roughcut_correlation"
    interval = next(item for item in artifact.intervals if item.classification == "mapped")
    assert interval.main["start_ticks"] == 60_000
    assert interval.auxiliary["start_ticks"] == 0
    assert interval.evidence["native_offset_seconds"] == "0.5"
    assert interval.evidence["initial_b_ticks"] == 60_000
    assert interval.evidence["standard_score"] == "7.5"


@pytest.mark.parametrize(
    ("native", "expected"),
    [("0.000004166666666666666666666666667", 1),
     ("-0.000004166666666666666666666666667", -1)],
)
def test_application_owns_decimal_to_ticks_conversion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native: str,
    expected: int,
) -> None:
    root, project, ffmpeg, ffprobe = _project(tmp_path, ("main", "aux"))
    runtime = _runtime(ffmpeg, ffprobe, _selection())
    _install_stubs(monkeypatch, runtime)
    monkeypatch.setattr(
        alignments,
        "run_bbc_offset_finder",
        lambda *_args, **_kwargs: SimpleNamespace(
            native_offset_seconds=native,
            offset_ticks=999_999,
            standard_score="1",
        ),
    )
    captured: list[int] = []

    def analyze(**kwargs: object) -> dict[str, object]:
        captured.append(int(kwargs["initial_b_ticks"]))
        return {
            "classifications": "uncertain",
            "evidence": empty_bbc_evidence(
                "initial_overlap_insufficient",
                native_offset_seconds=str(kwargs["native_offset_seconds"]),
                initial_b_ticks=int(kwargs["initial_b_ticks"]),
                standard_score=str(kwargs["standard_score"]),
            ),
        }

    monkeypatch.setattr(alignments, "analyze_bbc_pair", analyze)
    outcome = _run_historical_bbc(root, _request(project), runtime)
    assert outcome.artifact is not None
    assert captured == [expected]


@pytest.mark.parametrize("failed_count", [1, 2])
def test_partial_verification_evidence_publishes_uncertain_and_reads_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_count: int,
) -> None:
    root, project, ffmpeg, ffprobe = _project(tmp_path, ("main", "aux"))
    runtime = _runtime(ffmpeg, ffprobe, _selection())
    _install_stubs(monkeypatch, runtime)

    def partial(**kwargs: object) -> dict[str, object]:
        initial = int(kwargs["initial_b_ticks"])
        evidence = empty_bbc_evidence(
            "verification_failed",
            native_offset_seconds=str(kwargs["native_offset_seconds"]),
            initial_b_ticks=initial,
            standard_score=str(kwargs["standard_score"]),
        )
        evidence.update(
            {
                "channel": "mono",
                "refined_b_ticks": initial,
                "refined_correlation": "0.9",
                "refined_geometry": {
                    "main_start_ticks": 60_000,
                    "main_end_ticks": 4_800_000,
                    "auxiliary_start_ticks": 0,
                    "auxiliary_end_ticks": 4_740_000,
                },
            }
        )
        windows = _mapped_result(**kwargs)["evidence"]["verification_windows"]
        evidence["verification_windows"] = windows[:failed_count]
        evidence["verification_windows"][-1]["correlation"] = "0.34"
        evidence["verification_window_count"] = failed_count
        evidence["max_local_offset_error_ticks"] = 0
        return {"classifications": "uncertain", "evidence": evidence}

    monkeypatch.setattr(alignments, "analyze_bbc_pair", partial)
    outcome = _run_historical_bbc(root, _request(project), runtime)
    assert outcome.artifact is not None
    assert outcome.artifact.intervals[0].classification == "uncertain"
    stored = AlignmentStore(root).read("aln_bbc_production")
    assert stored is not None
    assert stored.intervals[0].evidence["verification_window_count"] == failed_count


@pytest.mark.parametrize("selection", [None, _selection("audalign")])
def test_non_bbc_selection_fails_closed_before_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: object
) -> None:
    root, project, ffmpeg, ffprobe = _project(tmp_path, ("main", "aux"))
    runtime = _runtime(ffmpeg, ffprobe, selection)
    monkeypatch.setattr(alignments, "_load_persistent_runtime", lambda: runtime)
    monkeypatch.setattr(
        alignments,
        "run_bbc_offset_finder",
        lambda *_args, **_kwargs: pytest.fail("invalid selection reached worker"),
    )
    with pytest.raises(MediaOperationError) as error:
        run_align_multicam(root, **_request(project))
    assert error.value.code == "alignment_runtime_unavailable"


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (BbcTimeoutError("timeout"), "alignment_time_budget_exceeded"),
        (BbcMemoryError("memory"), "alignment_memory_budget_exceeded"),
    ],
)
def test_bbc_budget_failures_never_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    code: str,
) -> None:
    root, project, ffmpeg, ffprobe = _project(tmp_path, ("main", "aux"))
    runtime = _runtime(ffmpeg, ffprobe, _selection())
    _install_stubs(monkeypatch, runtime)
    monkeypatch.setattr(
        alignments,
        "run_bbc_offset_finder",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
    )
    with pytest.raises(AlignmentError) as error:
        _run_historical_bbc(root, _request(project), runtime)
    assert error.value.code == code


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (BbcNoOffsetError("none"), "finder_insufficient_audio"),
        (BbcAdapterError("malformed"), "finder_result_invalid"),
    ],
)
def test_bbc_pair_failure_is_uncertain_without_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected_code: str,
) -> None:
    root, project, ffmpeg, ffprobe = _project(tmp_path, ("main", "aux"))
    runtime = _runtime(ffmpeg, ffprobe, _selection())
    _install_stubs(monkeypatch, runtime)
    monkeypatch.setattr(
        alignments,
        "run_bbc_offset_finder",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
    )
    outcome = _run_historical_bbc(root, _request(project), runtime)
    assert outcome.artifact is not None
    assert outcome.artifact.intervals[0].classification == "uncertain"
    assert outcome.artifact.intervals[0].evidence["code"] == expected_code


@pytest.mark.parametrize("legacy_profile", [None, WAVEFORM_WRITER_PROFILE])
def test_existing_legacy_operation_hash_reads_back_without_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_profile: dict[str, object] | None,
) -> None:
    from roughcut.adapters.media_operation_store import MediaOperationStore
    from roughcut.application.alignments import (
        _new_pending_record,
        _running_record,
        _terminal_record,
    )

    root, project, _ffmpeg, _ffprobe = _project(tmp_path, ("main", "aux"))
    request = _request(project)
    store = MediaOperationStore(root, project.project_id)
    scope = dict(store.scope.to_dict())
    projection = alignment_request_projection(
        scope=scope,
        operation_id=request["operation_id"],
        alignment_id=request["alignment_id"],
        expected_revision=request["expected_revision"],
        main_camera=request["main_camera"],
        auxiliary_cameras=request["auxiliary_cameras"],
        main_audio_stable=request["main_audio_stable"],
        max_temporary_disk_bytes=request["max_temporary_disk_bytes"],
        max_analysis_memory_bytes=request["max_analysis_memory_bytes"],
        max_runtime_seconds=request["max_runtime_seconds"],
        writer_profile=legacy_profile,
    )
    running = _running_record(
        _new_pending_record(
            request["operation_id"], store.scope, hash_alignment_request(projection), "c" * 64
        ),
        "alignment_processing_auxiliary",
    )
    store.write_locked(
        _terminal_record(
            running,
            status="succeeded",
            result=AlignmentOperationResult(
                alignment_id=request["alignment_id"],
                schema_version=1,
                content_hash="d" * 64,
            ),
        )
    )
    monkeypatch.setattr(
        alignments,
        "_load_persistent_runtime",
        lambda: pytest.fail("legacy readback loaded production runtime"),
    )
    outcome = run_align_multicam(root, **request)
    assert outcome.readback is True
    assert outcome.record.status == "succeeded"


def test_bbc_input_projection_binds_alignment_selection(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    ffprobe = shutil.which("ffprobe") or "ffprobe"
    first = _runtime(ffmpeg, ffprobe, _selection())
    changed_selection = _selection()
    changed_selection.to_dict = lambda: {  # type: ignore[method-assign]
        "provider": "bbc_audio_offset_finder",
        "provider_version": "0.5.5",
        "interpreter": "/managed/bbc-v2/bin/python",
    }
    changed = _runtime(ffmpeg, ffprobe, changed_selection)
    common = {
        "root": tmp_path,
        "project": SimpleNamespace(project_id="project_fixture", revision=1),
        "request_projection": {"request_schema_version": 2},
        "sources": {},
        "workspace_estimate": 1,
        "frozen_identity": {},
        "profile": BBC_WRITER_PROFILE,
    }
    first_projection = alignments._alignment_input_projection(
        **common, runtime=first
    )
    changed_projection = alignments._alignment_input_projection(
        **common, runtime=changed
    )
    assert first_projection["alignment_python"] != changed_projection["alignment_python"]


def test_publish_revalidates_managed_bbc_selection_not_only_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, project, ffmpeg, ffprobe = _project(tmp_path, ("main", "aux"))
    runtime = _runtime(ffmpeg, ffprobe, _selection())
    _install_stubs(monkeypatch, runtime)
    validations = 0

    def validate(selection: object) -> object:
        nonlocal validations
        validations += 1
        if validations == 2:
            raise BbcAdapterError("managed receipt became stale")
        return selection

    monkeypatch.setattr(alignments, "validate_bbc_selection", validate)
    with pytest.raises(AlignmentError) as error:
        _run_historical_bbc(root, _request(project), runtime)
    assert error.value.code == "alignment_runtime_changed_during_run"
    assert AlignmentStore(root).read("aln_bbc_production") is None


def test_one_auxiliary_finder_failure_is_attributed_and_safe_pair_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, project, ffmpeg, ffprobe = _project(
        tmp_path, ("main", "aux-failing", "aux-safe")
    )
    runtime = _runtime(ffmpeg, ffprobe, _selection())
    _install_stubs(monkeypatch, runtime)
    main_id, failing_id, safe_id = (source.source_id for source in project.sources)
    request = _request(
        project,
        pairs=[
            {"main_source_id": main_id, "auxiliary_source_id": failing_id},
            {"main_source_id": main_id, "auxiliary_source_id": safe_id},
        ],
    )

    def finder(_main: Path, auxiliary: Path, **_kwargs: object) -> BbcOffsetResult:
        if auxiliary.name == "aux-failing.wav":
            raise BbcAdapterError("closed provider failure")
        return BbcOffsetResult("0.5", "1", BbcOffsetAnalysis())

    monkeypatch.setattr(alignments, "run_bbc_offset_finder", finder)
    outcome = _run_historical_bbc(root, request, runtime)
    assert outcome.artifact is not None
    camera = outcome.artifact.auxiliary_cameras[0]
    assert tuple(error.to_dict() for error in camera.errors) == (
        {"code": "auxiliary_recognition_failed", "source_id": failing_id},
    )
    assert any(
        interval.classification == "mapped"
        and interval.auxiliary is not None
        and interval.auxiliary["source_id"] == safe_id
        for interval in outcome.artifact.intervals
    )
    assert AlignmentStore(root).read("aln_bbc_production") == outcome.artifact


def test_final_decode_failure_is_attributed_and_closes_uncertain_partition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, project, ffmpeg, ffprobe = _project(tmp_path, ("main", "aux"))
    runtime = _runtime(ffmpeg, ffprobe, _selection())
    _install_stubs(monkeypatch, runtime)
    monkeypatch.setattr(alignments, "analyze_bbc_pair", _decode_failed_result)

    outcome = _run_historical_bbc(root, _request(project), runtime)

    assert outcome.artifact is not None
    camera = outcome.artifact.auxiliary_cameras[0]
    assert camera.status == "failed"
    assert tuple(error.to_dict() for error in camera.errors) == (
        {
            "code": "auxiliary_verification_failed",
            "source_id": project.sources[1].source_id,
        },
    )
    assert camera.uncertain_ticks == 4_800_000
    assert [item.classification for item in outcome.artifact.intervals] == [
        "uncertain"
    ]
    assert outcome.artifact.intervals[0].evidence["code"] == "decode_failed"
    assert AlignmentStore(root).read("aln_bbc_production") == outcome.artifact


def test_final_decode_failure_does_not_block_safe_exact_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, project, ffmpeg, ffprobe = _project(
        tmp_path, ("main", "aux-failing", "aux-safe")
    )
    runtime = _runtime(ffmpeg, ffprobe, _selection())
    _install_stubs(monkeypatch, runtime)
    main_id, failing_id, safe_id = (source.source_id for source in project.sources)

    def analyze(**kwargs: object) -> dict[str, object]:
        if Path(str(kwargs["auxiliary_path"])).name == "aux-failing.wav":
            return _decode_failed_result(**kwargs)
        return _mapped_result(**kwargs)

    monkeypatch.setattr(alignments, "analyze_bbc_pair", analyze)
    request = _request(
        project,
        pairs=[
            {"main_source_id": main_id, "auxiliary_source_id": failing_id},
            {"main_source_id": main_id, "auxiliary_source_id": safe_id},
        ],
    )

    outcome = _run_historical_bbc(root, request, runtime)

    assert outcome.artifact is not None
    camera = outcome.artifact.auxiliary_cameras[0]
    assert camera.status == "partial"
    assert tuple(error.to_dict() for error in camera.errors) == (
        {"code": "auxiliary_verification_failed", "source_id": failing_id},
    )
    assert any(
        interval.classification == "mapped"
        and interval.auxiliary is not None
        and interval.auxiliary["source_id"] == safe_id
        for interval in outcome.artifact.intervals
    )
    assert AlignmentStore(root).read("aln_bbc_production") == outcome.artifact


@pytest.mark.parametrize(
    ("offsets", "expected_conflict"),
    [((60_000, 60_000), False), ((60_000, 72_000), False), ((60_000, 72_001), True)],
)
def test_multiple_exact_pairs_partition_same_near_and_far_refined_b(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    offsets: tuple[int, int],
    expected_conflict: bool,
) -> None:
    root, project, ffmpeg, ffprobe = _project(tmp_path, ("main", "aux-a", "aux-b"))
    runtime = _runtime(ffmpeg, ffprobe, _selection())
    _install_stubs(monkeypatch, runtime)
    main_id, aux_a_id, aux_b_id = (source.source_id for source in project.sources)
    by_name = {"aux-a.wav": offsets[0], "aux-b.wav": offsets[1]}

    def finder(_main: Path, auxiliary: Path, **_kwargs: object) -> BbcOffsetResult:
        ticks = by_name[auxiliary.name]
        native = str(Decimal(ticks) / Decimal(120_000))
        return BbcOffsetResult(native, "1", BbcOffsetAnalysis())

    monkeypatch.setattr(alignments, "run_bbc_offset_finder", finder)
    request = _request(
        project,
        pairs=[
            {"main_source_id": main_id, "auxiliary_source_id": aux_a_id},
            {"main_source_id": main_id, "auxiliary_source_id": aux_b_id},
        ],
    )
    outcome = _run_historical_bbc(root, request, runtime)
    assert outcome.artifact is not None
    conflicts = [
        interval
        for interval in outcome.artifact.intervals
        if interval.classification == "conflict"
    ]
    assert bool(conflicts) is expected_conflict
    if conflicts:
        assert conflicts[0].evidence["conflicting_b_ticks"] == list(offsets)
