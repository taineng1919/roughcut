from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import roughcut.adapters.ffmpeg.multicam_parallel as camera_adapter
import roughcut.adapters.multicam_parallel_store as parallel_store_module
import roughcut.application.media_operations as media_operations_module
import roughcut.application.multicam_parallel as parallel
import roughcut.cli as cli_module
import roughcut.mcp as mcp_module
from roughcut.adapters.alignment_store import AlignmentStore
from roughcut.adapters.ffmpeg.multicam_parallel import (
    ParallelCameraVerifyError,
    _build_command,
    _build_filter_script,
    render_parallel_camera,
    verify_parallel_camera,
)
from roughcut.adapters.ffmpeg_environment import FFmpegRuntimeDriftError
from roughcut.adapters.ffprobe import probe_media
from roughcut.adapters.media_operation_store import MediaOperationStore
from roughcut.adapters.multicam_parallel_store import MulticamParallelStore
from roughcut.adapters.project_store import ProjectStore
from roughcut.application.media_operations import media_operation_status
from roughcut.application.sources import add_source, fingerprint_file
from roughcut.cli import main as cli_main
from roughcut.domain.alignment import (
    AlignmentAlgorithm,
    AlignmentCamera,
    AlignmentCameraGroup,
    AlignmentError,
    AlignmentInterval,
    AlignmentPerCameraError,
    AlignmentSourceBasis,
    AlignmentSourceFingerprint,
    AlignmentSummary,
    AlignmentVerificationProfile,
    MulticamAlignmentArtifact,
)
from roughcut.domain.media_operation import (
    AlignmentOperationResult,
    MediaOperationError,
    MediaOperationFailure,
    ParallelRenderOperationResult,
)
from roughcut.domain.multicam_parallel import (
    ParallelRenderError,
    build_prepare_ref,
    frame_boundary,
    parallel_render_id,
    sample_boundary,
)
from roughcut.domain.project import ImportMode, Project
from roughcut.domain.render import ToolResolution
from roughcut.domain.workflow import canonical_json_v1, canonical_sha256_v1
from roughcut.mcp import handle_request


def _workflow_helpers():
    path = Path(__file__).parents[1] / "application" / "test_workflows.py"
    spec = importlib.util.spec_from_file_location("phase3_workflow_helpers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _mav_037_cases() -> tuple[tuple[str, str], ...]:
    vector_path = (
        Path(__file__).resolve().parents[3]
        / "core"
        / "tests"
        / "fixtures"
        / "multicam-alignment-vectors.json"
    )
    payload = json.loads(vector_path.read_text(encoding="utf-8"))
    vector = next(
        item
        for item in payload["vectors"]
        if item["id"].startswith("MAV-037-")
    )
    return tuple(
        ("mutation", path)
        for path in vector["input"]["isolated_mutation_paths"]
    ) + tuple(
        ("fabricated", case)
        for case in vector["input"]["fabricated_cases"]
    )


MAV_037_CASES = _mav_037_cases()


def _runtime(ffmpeg: str, ffprobe: str, suffix: str = "a") -> object:
    ffmpeg_version = subprocess.run(
        [ffmpeg, "-version"], check=True, capture_output=True, text=True
    ).stdout.splitlines()[0]
    ffprobe_version = subprocess.run(
        [ffprobe, "-version"], check=True, capture_output=True, text=True
    ).stdout.splitlines()[0]
    return SimpleNamespace(
        runtime_binding_sha256=suffix * 64,
        python_receipt_hash=("b" if suffix == "a" else "c") * 64,
        ffmpeg_tool_selection_hash=("d" if suffix == "a" else "e") * 64,
        ffprobe_tool_selection_hash=("f" if suffix == "a" else "a") * 64,
        ffmpeg=ToolResolution(
            ffmpeg, ffmpeg, ffmpeg_version if suffix == "a" else f"fixture-{suffix}"
        ),
        ffprobe=ToolResolution(
            ffprobe, ffprobe, ffprobe_version if suffix == "a" else f"fixture-{suffix}"
        ),
    )


def _complete_main_render(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> object:
    helpers = _workflow_helpers()
    status = helpers.workflow_status(root, "wfr_test")
    export_ref = status["presented_subjects"]["export_ref"]
    monkeypatch.setattr(
        helpers.media_operations_module,
        "_load_persistent_runtime",
        lambda: SimpleNamespace(
            ffmpeg=ToolResolution("ffmpeg", "/fixture/ffmpeg", "fixture"),
            ffprobe=ToolResolution("ffprobe", "/fixture/ffprobe", "fixture"),
        ),
    )
    monkeypatch.setattr(
        helpers.media_operations_module,
        "_validate_media_runtime",
        lambda _runtime: ("ffmpeg version fixture", "ffprobe version fixture"),
    )

    def prepare(
        project_path: Path,
        *,
        edit_version_id: str,
        expected_revision: int,
        render_id: str | None = None,
        tools: object = None,
    ) -> object:
        assert tools is not None
        return helpers._fixture_render_plan(
            project_path,
            edit_version_id=edit_version_id,
            expected_revision=expected_revision,
            render_id=render_id,
        )

    def execute(
        _project_path: Path,
        plan: object,
        *,
        output_path: Path,
        manifest_path: Path,
        after_output_published: object = None,
        phase_callback: object = None,
        **_kwargs: object,
    ) -> tuple[object, dict[str, object]]:
        output_path.write_bytes(b"fixture-main-render")
        if callable(after_output_published):
            after_output_published()
        manifest = helpers._fixture_render_manifest(plan)
        helpers.write_new_json(manifest_path, manifest)
        return (
            helpers.RenderResult(
                plan.render_id,
                plan.output_relative_path,
                plan.manifest_relative_path,
                {"duration": True},
            ),
            manifest,
        )

    monkeypatch.setattr(helpers.workflows_module, "prepare_render_plan", prepare)
    monkeypatch.setattr(
        helpers.workflows_module,
        "execute_prepared_render_to_paths",
        execute,
    )
    outcome = helpers.run_approve_export_operation(
        root,
        run_id="wfr_test",
        action_id="act_phase3_completed_export",
        action_input={"schema_version": 1, "export_ref": export_ref},
    )
    assert outcome.record.status == "succeeded"
    assert outcome.result is not None
    assert outcome.result.workflow_run.lifecycle == "completed"
    assert outcome.result.workflow_run.stage == "exporting"
    return outcome


def _make_mp4(path: Path, *, seconds: int = 2, rotation: int = 0) -> None:
    encoded_path = (
        path
        if rotation == 0
        else path.with_name(f".{path.name}.physical.mp4")
    )
    command = [
        shutil.which("ffmpeg") or "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        (
            "color=c=black:s=320x240:r=25,"
            "drawbox=x=0:y=0:w=160:h=240:color=red:t=fill,"
            "drawbox=x=160:y=0:w=160:h=120:color=green:t=fill,"
            "drawbox=x=160:y=120:w=160:h=120:color=blue:t=fill"
        ),
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000",
        "-t",
        str(seconds),
    ]
    command.extend([
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
    ])
    command.extend(["-y", str(encoded_path)])
    subprocess.run(
        command,
        check=True,
    )
    if rotation:
        subprocess.run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-display_rotation:v:0",
                str(rotation),
                "-i",
                str(encoded_path),
                "-map",
                "0",
                "-c",
                "copy",
                "-y",
                str(path),
            ],
            check=True,
        )
        encoded_path.unlink()


def _make_full_range_auxiliary(path: Path, *, seconds: int = 2) -> None:
    command = [
        shutil.which("ffmpeg") or "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x240:rate=25",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=660:sample_rate=48000",
        "-t",
        str(seconds),
        "-vf",
        "format=yuv422p10le,setparams=range=full",
        "-c:v",
        "ffv1",
        "-level",
        "3",
        "-pix_fmt",
        "yuv422p10le",
        "-color_range",
        "pc",
        "-c:a",
        "pcm_s16le",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-shortest",
        "-y",
        str(path),
    ]
    subprocess.run(command, check=True)


def _tree_bytes(root: Path) -> dict[str, tuple[str, bytes | str | None]]:
    result: dict[str, tuple[str, bytes | str | None]] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            result[relative] = ("symlink", os.readlink(path))
        elif path.is_dir():
            result[relative] = ("directory", None)
        else:
            result[relative] = ("file", path.read_bytes())
    return result


def _prepare_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    camera_ids: tuple[str, ...] = ("aux_1",),
    partitions: tuple[tuple[int, int, str], ...] | None = None,
    run_prepare: bool = True,
    camera_status: str | None = None,
    camera_error_code: str | None = None,
    auxiliary_rotation: int = 0,
    auxiliary_builder: Callable[[Path], None] | None = None,
) -> tuple[Path, object, dict[str, object], dict[str, Path], object]:
    helpers = _workflow_helpers()
    root = helpers._workflow_project(tmp_path)
    media = tmp_path / "phase3-media"
    media.mkdir()
    main_path = media / "main.mp4"
    _make_mp4(main_path)
    project = ProjectStore(root).load()
    main = replace(
        project.sources[0],
        kind="video",
        display_name="main.mp4",
        locator={"absolute_path": str(main_path)},
        fingerprint=fingerprint_file(main_path),
        probe=probe_media(main_path),
    )
    ProjectStore(root).save(replace(project, sources=(main,)), expected_revision=project.revision)
    auxiliary_sources: dict[str, object] = {}
    source_paths: dict[str, Path] = {main.source_id: main_path}
    for camera_id in camera_ids:
        suffix = ".mkv" if auxiliary_builder is not None else ".mp4"
        path = media / f"{camera_id}{suffix}"
        if auxiliary_builder is None:
            _make_mp4(path, rotation=auxiliary_rotation)
        else:
            auxiliary_builder(path)
        project = add_source(
            root,
            path,
            ImportMode.LINKED,
            expected_revision=ProjectStore(root).load().revision,
        )
        source = project.sources[-1]
        if auxiliary_rotation:
            assert source.probe.rotation_degrees == auxiliary_rotation
        auxiliary_sources[camera_id] = source
        source_paths[source.source_id] = path
    helpers._advance_to_export_review(root)
    project = ProjectStore(root).load()
    main = next(source for source in project.sources if source.source_id == "src_a")
    if partitions is None:
        duration = main.probe.duration_ticks
        partitions = ((0, duration, "mapped"),)
    algorithm_profile = AlignmentVerificationProfile("roughcut_audalign_fixed_offset", 1)
    algorithm = AlignmentAlgorithm(
        "audalign_fingerprint",
        "1.3.1",
        "d5955ae8a85b1cd480dadd005c3f88986f4ebbef",
        2,
        1,
        "fixed_offset_equal_speed",
        120_000,
        algorithm_profile,
    )

    def alignment_fingerprint(source: object) -> AlignmentSourceFingerprint:
        fingerprint = source.fingerprint
        return AlignmentSourceFingerprint(
            fingerprint.size,
            fingerprint.mtime_ns,
            fingerprint.sha256_head_tail,
        )

    intervals: list[AlignmentInterval] = []
    cameras: list[AlignmentCamera] = []
    basis: list[AlignmentSourceBasis] = [
        AlignmentSourceBasis(
            "main",
            main.source_id,
            alignment_fingerprint(main),
            main.probe.duration_ticks,
        )
    ]
    for camera_index, camera_id in enumerate(camera_ids):
        source = auxiliary_sources[camera_id]
        basis.append(
            AlignmentSourceBasis(
                camera_id,
                source.source_id,
                alignment_fingerprint(source),
                source.probe.duration_ticks,
            )
        )
        mapped = missing = uncertain = conflict = 0
        for ordinal, (start, end, classification) in enumerate(partitions):
            evidence = {
                "code": "fixed_offset_verified" if classification in {"mapped", "conflict"} else "no_candidate",
                "raw_candidate_count": 2 if classification == "conflict" else (1 if classification == "mapped" else 0),
                "matching_fingerprint_counts": [1, 1] if classification == "conflict" else ([1] if classification == "mapped" else []),
                "verification_window_count": 3 if classification in {"mapped", "conflict"} else 0,
                "verification_profile": algorithm_profile.to_dict(),
                "max_local_offset_error_ticks": 0 if classification in {"mapped", "conflict"} else None,
            }
            auxiliary = (
                {"source_id": source.source_id, "start_ticks": start, "end_ticks": end}
                if classification == "mapped"
                else None
            )
            intervals.append(
                AlignmentInterval(
                    f"{camera_id}_interval_{ordinal}",
                    camera_id,
                    classification,  # type: ignore[arg-type]
                    {"source_id": main.source_id, "start_ticks": start, "end_ticks": end},
                    auxiliary,
                    evidence,
                )
            )
            if classification == "mapped":
                mapped += end - start
            elif classification == "missing":
                missing += end - start
            elif classification == "uncertain":
                uncertain += end - start
            else:
                conflict += end - start
        errors = (
            ()
            if camera_error_code is None
            else (AlignmentPerCameraError(camera_error_code, source.source_id),)
        )
        derived_status = "complete" if mapped == main.probe.duration_ticks else ("omitted" if mapped == 0 else "partial")
        cameras.append(
            AlignmentCamera(
                camera_id,
                (source.source_id,),
                camera_status or derived_status,
                mapped,
                missing,
                uncertain,
                conflict,
                errors,
            )
        )
    basis.sort(key=lambda item: (item.camera_id, item.source_id))
    producer_operation_id = "op_00000000000040008000000000000090"
    artifact = __import__("roughcut.domain.alignment", fromlist=["MulticamAlignmentArtifact"]).MulticamAlignmentArtifact(
        "aln_phase3",
        project.project_id,
        producer_operation_id,
        "2026-08-04T00:00:00.000000Z",
        "1" * 64,
        "2" * 64,
        algorithm,
        AlignmentCameraGroup("main", (main.source_id,)),
        tuple(cameras),
        tuple(basis),
        tuple(intervals),
        AlignmentSummary(
            main.probe.duration_ticks,
            len(cameras),
            sum(camera.mapped_ticks for camera in cameras),
            sum(camera.missing_ticks for camera in cameras),
            sum(camera.uncertain_ticks for camera in cameras),
            sum(camera.conflict_ticks for camera in cameras),
        ),
    )
    AlignmentStore(root).publish(artifact.alignment_id, artifact)
    media_store = MediaOperationStore(root, project.project_id)
    result_ref = AlignmentOperationResult(artifact.alignment_id, 1, artifact.content_hash)
    from roughcut.domain.media_operation import MediaOperationRecord

    record = MediaOperationRecord(
        producer_operation_id,
        media_store.scope,
        "align_multicam",
        "3" * 64,
        "4" * 64,
        "succeeded",
        "alignment_succeeded",
        "2026-08-04T00:00:00.000000Z",
        "2026-08-04T00:00:00.000000Z",
        "2026-08-04T00:00:00.000000Z",
        "2026-08-04T00:00:00.000000Z",
        result_ref,
        None,
        schema_version=2,
    )
    with media_store.writer(producer_operation_id, create=True):
        media_store.write_locked(record)
    runtime = _runtime(shutil.which("ffmpeg") or "ffmpeg", shutil.which("ffprobe") or "ffprobe")
    monkeypatch.setattr(parallel, "_load_persistent_runtime", lambda: runtime)
    prepared = (
        parallel.prepare_multicam_parallel_render(
            root,
            edit_version_id=project.active_edit_version_id,
            alignment_ref=result_ref.to_dict(),
            auxiliary_camera_ids=list(camera_ids),
            expected_revision=project.revision,
        )
        if run_prepare
        else None
    )
    return root, project, None if prepared is None else prepared.prepare_ref, source_paths, runtime


def _advance_reordered_multisource_decision(
    root: Path,
    helpers: object,
    monkeypatch: pytest.MonkeyPatch,
    main_b_path: Path | None = None,
) -> None:
    helpers._add_fixture_source(root, "src_b")
    if main_b_path is not None:
        project = ProjectStore(root).load()
        source_b = next(source for source in project.sources if source.source_id == "src_b")
        source_b = replace(
            source_b,
            kind="video",
            display_name="main_b.mp4",
            locator={"absolute_path": str(main_b_path)},
            fingerprint=fingerprint_file(main_b_path),
            probe=probe_media(main_b_path),
        )
        ProjectStore(root).save(
            replace(project, sources=tuple(source_b if source.source_id == "src_b" else source for source in project.sources)),
            expected_revision=project.revision,
        )
    _install_partial_transcript_units(root, "src_b", "tr_src_b", (0, 1_001, 10_002, 120_000))
    monkeypatch.setattr(helpers, "_add_fixture_source", lambda *_args: None)
    submitted = helpers._submit_two_binding_draft(root)
    run, brief_ref, context_hash = helpers._current_draft_context(root)
    anchor = run.artifact_refs["content_draft"]
    assert anchor is not None
    reordered = helpers.workflow_action(
        root,
        "wfr_test",
        "act_reordered_draft",
        "submit_draft",
        {
            "schema_version": 1,
            "parent_draft_ref": anchor.to_dict(),
            "display_title": "双素材重排",
            "source_bindings": [
                {"source_id": "src_a", "transcript_version_id": "tr_a"},
                {"source_id": "src_b", "transcript_version_id": "tr_src_b"},
            ],
            "brief_ref": brief_ref.to_dict(),
            "context_hash": context_hash,
            "blocks": [
                {
                    "block_id": "block_b_reordered",
                    "kind": "source_excerpt",
                    "refs": [{"source_id": "src_b", "transcript_version_id": "tr_src_b", "segment_id": "seg_1", "start_ticks": 1001, "end_ticks": 10002}],
                    "canonical_text": "场",
                },
                {
                    "block_id": "block_a_reordered",
                    "kind": "source_excerpt",
                    "refs": [{"source_id": "src_a", "transcript_version_id": "tr_a", "segment_id": "seg_1", "start_ticks": 2000, "end_ticks": 11005}],
                    "canonical_text": "场",
                },
            ],
            "scoped_mutable_block_ids": [],
        },
    )
    mutation = reordered.receipt.mutation
    assert mutation is not None
    approved = helpers.workflow_action(
        root,
        "wfr_test",
        "act_reordered_approve",
        "approve_draft",
        {"schema_version": 1, "content_draft_ref": {"artifact_id": mutation.artifact_id, "schema_version": mutation.schema_version, "content_hash": mutation.content_hash}},
    )
    proposal = approved.workflow_run.artifact_refs["proposal"]
    assert proposal is not None
    helpers.workflow_action(
        root,
        "wfr_test",
        "act_reordered_adopt",
        "adopt_roughcut",
        {"schema_version": 1, "proposal_ref": proposal.to_dict()},
    )
    del submitted


def _install_partial_transcript_units(
    root: Path, source_id: str, transcript_id: str, boundaries: tuple[int, int, int, int]
) -> None:
    path = root / "transcripts" / source_id / f"{transcript_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    segment = next(item for item in payload["segments"] if item["segment_id"] == "seg_1")
    segment["fine_units"] = [
        {"kind": "word", "text": text, "start_ticks": start, "end_ticks": end, "confidence": None}
        for text, start, end in zip(("开", "场", "。"), boundaries, boundaries[1:])
    ]
    path.write_bytes(canonical_json_v1(payload) + b"\n")


def test_mav_012_014_035_prepare_application_uses_exact_alignment_and_global_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    partitions = (
        (0, 24_000, "mapped"),
        (24_000, 48_000, "missing"),
        (48_000, 72_000, "mapped"),
        (72_000, 96_000, "uncertain"),
        (96_000, 120_000, "conflict"),
        (120_000, 240_000, "mapped"),
    )
    root, project, prepare_ref, _sources, _runtime = _prepare_project(
        tmp_path, monkeypatch, partitions=partitions
    )
    assert prepare_ref["alignment_ref"]["alignment_id"] == "aln_phase3"
    before = _tree_bytes(root)
    outcome = parallel.prepare_multicam_parallel_render(
        root,
        edit_version_id=project.active_edit_version_id,
        alignment_ref={
            "kind": "multicam_alignment",
            "alignment_id": "aln_phase3",
            "schema_version": 1,
            "content_hash": AlignmentStore(root).read("aln_phase3").content_hash,
        },
        auxiliary_camera_ids=["aux_1"],
        expected_revision=project.revision,
    )
    slots = outcome.summary["cameras"][0]["slots"]
    assert len(slots) == 5
    assert [slot["output_start_ticks"] for slot in slots] == [0, 24_000, 48_000, 72_000, 96_000]
    assert [slot["video_frame_start"] for slot in slots] == [0, 5, 10, 15, 20]
    assert [slot["audio_sample_start"] for slot in slots] == [0, 9_600, 19_200, 28_800, 38_400]
    assert outcome.summary["video_frame_quota"] == 25
    assert outcome.summary["audio_sample_quota"] == 48_000
    assert outcome.summary["estimated_temporary_disk_bytes"] > 0
    assert outcome.prepare_ref == prepare_ref
    assert not (root / "renders").exists()
    assert _tree_bytes(root) == before


def test_prepare_accepts_completed_exporting_exact_adoption_at_later_revision_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _project, prepare_ref, _sources, _runtime = _prepare_project(
        tmp_path, monkeypatch
    )
    _complete_main_render(root, monkeypatch)
    completed_project = ProjectStore(root).load()
    ProjectStore(root).save(
        replace(completed_project, revision=completed_project.revision + 1),
        expected_revision=completed_project.revision,
    )
    current_project = ProjectStore(root).load()
    run_payload = json.loads(
        next((root / "workflow" / "runs").glob("*.json")).read_text(
            encoding="utf-8"
        )
    )
    assert (run_payload["lifecycle"], run_payload["stage"]) == (
        "completed",
        "exporting",
    )
    adoption_receipt = json.loads(
        (
            root
            / "workflow"
            / "receipts"
            / f"{prepare_ref['decision_adoption']['receipt_ref']['action_id']}.json"
        ).read_text(encoding="utf-8")
    )
    assert current_project.revision > adoption_receipt["after"]["project_revision"]
    before = _tree_bytes(root)
    outcome = parallel.prepare_multicam_parallel_render(
        root,
        edit_version_id=current_project.active_edit_version_id,
        alignment_ref=prepare_ref["alignment_ref"],
        auxiliary_camera_ids=["aux_1"],
        expected_revision=current_project.revision,
    )
    assert outcome.prepare_ref["project_revision"] == current_project.revision
    assert outcome.prepare_ref["decision_adoption"] == prepare_ref["decision_adoption"]
    assert _tree_bytes(root) == before


def test_mav_035_reordered_multisource_decision_uses_one_global_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helpers = _workflow_helpers()
    root = helpers._workflow_project(tmp_path)
    media = tmp_path / "phase3-multisource-media"
    media.mkdir()
    main_path = media / "main.mp4"
    _make_mp4(main_path)
    project = ProjectStore(root).load()
    main = replace(
        project.sources[0],
        kind="video",
        display_name="main.mp4",
        locator={"absolute_path": str(main_path)},
        fingerprint=fingerprint_file(main_path),
        probe=probe_media(main_path),
    )
    ProjectStore(root).save(replace(project, sources=(main,)), expected_revision=project.revision)
    _install_partial_transcript_units(root, "src_a", "tr_a", (0, 2_000, 11_005, 120_000))
    auxiliary_path = media / "aux-1.mp4"
    _make_mp4(auxiliary_path)
    main_b_path = media / "main_b.mp4"
    _make_mp4(main_b_path)
    project = add_source(
        root,
        auxiliary_path,
        ImportMode.LINKED,
        expected_revision=ProjectStore(root).load().revision,
    )
    _advance_reordered_multisource_decision(root, helpers, monkeypatch, main_b_path)
    project = ProjectStore(root).load()
    main_sources = {
        source.source_id: source
        for source in project.sources
        if source.source_id in {"src_a", "src_b"}
    }
    auxiliary = next(source for source in project.sources if source.source_id not in {"src_a", "src_b"})
    algorithm_profile = AlignmentVerificationProfile("roughcut_audalign_fixed_offset", 1)
    algorithm = AlignmentAlgorithm(
        "audalign_fingerprint", "1.3.1", "d5955ae8a85b1cd480dadd005c3f88986f4ebbef", 2, 1,
        "fixed_offset_equal_speed", 120_000, algorithm_profile,
    )

    def source_fingerprint(source: object) -> AlignmentSourceFingerprint:
        fingerprint = source.fingerprint
        digest = fingerprint.sha256_head_tail if len(fingerprint.sha256_head_tail) == 64 else "b" * 64
        return AlignmentSourceFingerprint(fingerprint.size, fingerprint.mtime_ns, digest)

    partition_specs = {
        "src_b": ((0, 3_502, "mapped", 4_000), (3_502, 12_000, "missing", 0), (12_000, main_sources["src_b"].probe.duration_ticks, "missing", 0)),
        "src_a": ((0, 6_803, "mapped", 10_000), (6_803, 12_000, "uncertain", 0), (12_000, main_sources["src_a"].probe.duration_ticks, "mapped", 12_000)),
    }
    intervals: list[AlignmentInterval] = []
    mapped = missing = uncertain = conflict = 0
    for source_id in ("src_b", "src_a"):
        for ordinal, (start, end, classification, auxiliary_start) in enumerate(partition_specs[source_id]):
            auxiliary_ref = (
                {"source_id": auxiliary.source_id, "start_ticks": auxiliary_start, "end_ticks": auxiliary_start + end - start}
                if classification == "mapped" else None
            )
            evidence = {
                "code": "fixed_offset_verified" if classification == "mapped" else "no_candidate",
                "raw_candidate_count": 1 if classification == "mapped" else 0,
                "matching_fingerprint_counts": [1] if classification == "mapped" else [],
                "verification_window_count": 3 if classification == "mapped" else 0,
                "verification_profile": algorithm_profile.to_dict(),
                "max_local_offset_error_ticks": 0 if classification == "mapped" else None,
            }
            intervals.append(
                AlignmentInterval(
                    f"{source_id}_interval_{ordinal}", "aux-1", classification,
                    {"source_id": source_id, "start_ticks": start, "end_ticks": end},
                    auxiliary_ref, evidence,
                )
            )
            if classification == "mapped":
                mapped += end - start
            elif classification == "missing":
                missing += end - start
            elif classification == "uncertain":
                uncertain += end - start
            else:
                conflict += end - start
    basis = [
        AlignmentSourceBasis("main", source_id, source_fingerprint(source), source.probe.duration_ticks)
        for source_id, source in main_sources.items()
    ]
    basis.append(AlignmentSourceBasis("aux-1", auxiliary.source_id, source_fingerprint(auxiliary), auxiliary.probe.duration_ticks))
    basis.sort(key=lambda item: (item.camera_id, item.source_id))
    artifact = MulticamAlignmentArtifact(
        "aln_multi_main", project.project_id,
        "op_00000000000040008000000000000035", "2026-08-04T00:00:00.000000Z", "1" * 64, "2" * 64,
        algorithm, AlignmentCameraGroup("main", ("src_a", "src_b")),
        (AlignmentCamera("aux-1", (auxiliary.source_id,), "partial", mapped, missing, uncertain, conflict, ()),),
        tuple(basis), tuple(intervals),
        AlignmentSummary(sum(source.probe.duration_ticks for source in main_sources.values()), 1, mapped, missing, uncertain, conflict),
    )
    AlignmentStore(root).publish(artifact.alignment_id, artifact)
    media_store = MediaOperationStore(root, project.project_id)
    from roughcut.domain.media_operation import MediaOperationRecord

    with media_store.writer(artifact.producer_operation_id, create=True):
        media_store.write_locked(
            MediaOperationRecord(
                artifact.producer_operation_id, media_store.scope, "align_multicam", "3" * 64, "4" * 64,
                "succeeded", "alignment_succeeded", "2026-08-04T00:00:00.000000Z", "2026-08-04T00:00:00.000000Z",
                "2026-08-04T00:00:00.000000Z", "2026-08-04T00:00:00.000000Z",
                AlignmentOperationResult(artifact.alignment_id, 1, artifact.content_hash), None, schema_version=2,
            )
        )
    runtime = _runtime(shutil.which("ffmpeg") or "ffmpeg", shutil.which("ffprobe") or "ffprobe")
    monkeypatch.setattr(parallel, "_load_persistent_runtime", lambda: runtime)
    before = _tree_bytes(root)
    outcome = parallel.prepare_multicam_parallel_render(
        root, edit_version_id=project.active_edit_version_id,
        alignment_ref=AlignmentOperationResult(artifact.alignment_id, 1, artifact.content_hash).to_dict(),
        auxiliary_camera_ids=["aux-1"], expected_revision=project.revision,
    )
    slots = outcome.summary["cameras"][0]["slots"]
    assert [slot["decision_clip_ref"]["source_id"] for slot in slots] == ["src_b", "src_b", "src_a", "src_a"]
    assert [slot["output_start_ticks"] for slot in slots] == [0, 2_501, 9_001, 13_804]
    assert [slots[-1]["output_end_ticks"]] == [18_006]
    assert [slot["video_frame_start"] for slot in slots] == [0, 1, 2, 3]
    assert [slot["audio_sample_start"] for slot in slots] == [0, 1_000, 3_600, 5_522]
    assert outcome.summary["video_frame_quota"] == 4
    assert outcome.summary["audio_sample_quota"] == 7_202
    assert outcome.summary["output_settings_hash"] == "b11ff679ae9b32ad4b5168003c50eda7a2f2550f0e0866223a6853241312116b"
    assert outcome.prepare_ref == build_prepare_ref(outcome.basis)
    assert _tree_bytes(root) == before


@pytest.mark.parametrize(
    "fault",
    (
        "pending_marker",
        "receipt_action_mismatch",
        "symlink_evidence",
        "hardlink_evidence",
        "path_object_id_mismatch",
    ),
)
def test_prepare_exact_chain_rejects_pending_mismatch_and_unsafe_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    root, project, prepare_ref, _sources, _runtime = _prepare_project(tmp_path, monkeypatch)
    del prepare_ref
    run_path = next((root / "workflow" / "runs").glob("*.json"))
    run = json.loads(run_path.read_text(encoding="utf-8"))
    approval_id = run["approval_refs"]["roughcut"]["approval_id"]
    approval_path = root / "workflow" / "approvals" / f"{approval_id}.json"
    approval = json.loads(approval_path.read_text(encoding="utf-8"))
    receipt_id = approval["issued_by_action_id"]
    receipt_path = root / "workflow" / "receipts" / f"{receipt_id}.json"
    if fault == "pending_marker":
        marker_path = root / "workflow" / "transactions" / f"{receipt_id}.json"
        marker_path.write_text("{}\n", encoding="utf-8")
    elif fault == "receipt_action_mismatch":
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["action_id"] = "action_mismatch"
        receipt_path.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
    elif fault == "symlink_evidence":
        target = root / "workflow" / "unsafe-run-target.json"
        target.write_bytes(run_path.read_bytes())
        run_path.unlink()
        run_path.symlink_to(target)
    elif fault == "hardlink_evidence":
        os.link(run_path, run_path.with_name("wfr_hardlink_copy.json"))
    else:
        run_path.rename(run_path.with_name("wfr_path_mismatch.json"))
    before = _tree_bytes(root)
    artifact = AlignmentStore(root).read("aln_phase3")
    assert artifact is not None
    with pytest.raises(ParallelRenderError) as error:
        parallel.prepare_multicam_parallel_render(
            root,
            edit_version_id=project.active_edit_version_id,
            alignment_ref={
                "kind": "multicam_alignment",
                "alignment_id": "aln_phase3",
                "schema_version": 1,
                "content_hash": artifact.content_hash,
            },
            auxiliary_camera_ids=["aux_1"],
            expected_revision=project.revision,
        )
    assert error.value.code == "parallel_render_decision_not_adopted"
    assert _tree_bytes(root) == before


def test_start_disk_budget_writes_terminal_record_before_staging_or_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, project, prepare_ref, _sources, _runtime = _prepare_project(tmp_path, monkeypatch)
    monkeypatch.setattr(parallel.shutil, "disk_usage", lambda _path: SimpleNamespace(free=0))
    monkeypatch.setattr(parallel, "render_parallel_camera", lambda *args, **kwargs: pytest.fail("child worker started"))
    operation_id = "op_00000000000040008000000000000103"
    outcome = parallel.start_multicam_parallel_render(root, operation_id=operation_id, prepare_ref=prepare_ref)
    assert outcome.record.status == "failed"
    assert outcome.record.result_ref is None
    assert outcome.record.error is not None
    assert outcome.record.error.to_dict() == {
        "code": "parallel_render_disk_budget_exceeded",
        "responsibility": "roughcut_core",
        "action": "validate_parallel_render_basis",
        "message_code": "parallel_render_failed",
    }
    assert not (root / "renders" / "multicam-staging").exists()
    assert not (root / "renders" / "multicam").exists()
    assert MediaOperationStore(root, project.project_id).read(operation_id) == outcome.record


def test_parallel_new_multicamera_drift_pair_runs_once_after_running_before_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, project, prepare_ref, _sources, _runtime_value = _prepare_project(
        tmp_path, monkeypatch, camera_ids=("aux_1", "aux_2")
    )
    events: list[str] = []
    version_calls: list[list[str]] = []
    original_write = MediaOperationStore.write_locked
    original_create = MulticamParallelStore.create_staging

    def write(store: MediaOperationStore, record: object) -> object:
        if record.status == "running":
            events.append("running")
        return original_write(store, record)

    def verify_pair(**kwargs: object) -> tuple[str, str]:
        version_calls.extend(
            [
                [str(kwargs["ffmpeg_command"]), "-version"],
                [str(kwargs["ffprobe_command"]), "-version"],
            ]
        )
        events.append("drift_pair")
        return str(kwargs["ffmpeg_version"]), str(kwargs["ffprobe_version"])

    def create(store: MulticamParallelStore, operation_id: str) -> Path:
        events.append("create_staging")
        return original_create(store, operation_id)

    camera_calls = 0

    def render(*_args: object, output_path: Path, **_kwargs: object) -> None:
        nonlocal camera_calls
        camera_calls += 1
        output_path.write_bytes(b"fixture camera")

    def reject(*_args: object, **_kwargs: object) -> object:
        raise ParallelCameraVerifyError("fixture verify failure")

    monkeypatch.setattr(MediaOperationStore, "write_locked", write)
    monkeypatch.setattr(MulticamParallelStore, "create_staging", create)
    monkeypatch.setattr(media_operations_module, "verify_runtime_pair", verify_pair)
    monkeypatch.setattr(parallel, "render_parallel_camera", render)
    monkeypatch.setattr(parallel, "verify_parallel_camera", reject)
    operation_id = "op_00000000000040008000000000000131"
    outcome = parallel.start_multicam_parallel_render(
        root, operation_id=operation_id, prepare_ref=prepare_ref
    )
    calls_after_success = list(version_calls)
    existing = parallel.start_multicam_parallel_render(
        root, operation_id=operation_id, prepare_ref=prepare_ref
    )

    assert outcome.record.status == "failed"
    assert outcome.record.error is not None
    assert camera_calls == 2, events
    assert outcome.record.error.code == "parallel_render_all_cameras_failed"
    assert existing.readback is True
    assert events.index("running") < events.index("drift_pair") < events.index("create_staging")
    assert len(calls_after_success) == 2
    assert [call[-1] for call in calls_after_success] == ["-version", "-version"]
    assert version_calls == calls_after_success
    assert outcome.result is None
    assert MediaOperationStore(root, project.project_id).read(operation_id) == outcome.record


def test_parallel_drift_failure_is_closed_before_staging_camera_or_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, project, prepare_ref, _sources, _runtime_value = _prepare_project(
        tmp_path, monkeypatch, camera_ids=("aux_1", "aux_2")
    )

    def drift(**_kwargs: object) -> tuple[str, str]:
        raise FFmpegRuntimeDriftError("fixture drift")

    monkeypatch.setattr(media_operations_module, "verify_runtime_pair", drift)
    monkeypatch.setattr(
        MulticamParallelStore,
        "create_staging",
        lambda *_args, **_kwargs: pytest.fail("drift failure created staging"),
    )
    monkeypatch.setattr(
        parallel,
        "render_parallel_camera",
        lambda *_args, **_kwargs: pytest.fail("drift failure started camera child"),
    )
    operation_id = "op_00000000000040008000000000000132"
    outcome = parallel.start_multicam_parallel_render(
        root, operation_id=operation_id, prepare_ref=prepare_ref
    )

    assert outcome.record.status == "failed"
    assert outcome.record.error is not None
    assert outcome.record.error.code == "parallel_render_runtime_changed_during_run"
    assert outcome.record.result_ref is None
    assert outcome.result is None
    assert not (root / "renders" / "multicam-staging").exists()
    assert not (root / "renders" / "multicam").exists()
    assert MediaOperationStore(root, project.project_id).read(operation_id) == outcome.record


@pytest.mark.parametrize("failure", ("filter_write", "temp_rename", "temp_cleanup"))
def test_staging_first_failures_are_global_staging_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    root, _project, prepare_ref, _sources, _runtime = _prepare_project(tmp_path, monkeypatch)
    operation_id = {
        "filter_write": "op_00000000000040008000000000000104",
        "temp_rename": "op_00000000000040008000000000000105",
        "temp_cleanup": "op_00000000000040008000000000000106",
    }[failure]
    if failure == "filter_write":
        import roughcut.adapters.ffmpeg.multicam_parallel as adapter

        original_write_text = adapter.Path.write_text

        def fail_filter_write(path: Path, data: str, **kwargs: object) -> int:
            if path.name.endswith(".filter.txt"):
                raise OSError("fixture filter write failure")
            return original_write_text(path, data, **kwargs)

        monkeypatch.setattr(adapter.Path, "write_text", fail_filter_write)
    elif failure == "temp_rename":
        original_rename = parallel.os.rename

        def fail_temp_rename(source: str | bytes | os.PathLike[str] | os.PathLike[bytes], destination: str | bytes | os.PathLike[str] | os.PathLike[bytes]) -> None:
            if str(source).endswith(".mp4.tmp"):
                raise OSError("fixture temp rename failure")
            original_rename(source, destination)

        monkeypatch.setattr(parallel.os, "rename", fail_temp_rename)
    else:
        original_remove = parallel._remove_camera_temps

        def fail_temp_cleanup(temp_output: Path, output_path: Path) -> None:
            original_remove(temp_output, output_path)
            raise parallel.ParallelCameraStagingError("fixture temp cleanup failure")

        monkeypatch.setattr(parallel, "_remove_camera_temps", fail_temp_cleanup)
        monkeypatch.setattr(parallel, "render_parallel_camera", lambda camera, **kwargs: (_ for _ in ()).throw(parallel.ParallelCameraEncodeError("fixture encode failure")))
    outcome = parallel.start_multicam_parallel_render(root, operation_id=operation_id, prepare_ref=prepare_ref)
    assert outcome.record.status == "failed"
    assert outcome.record.error is not None and outcome.record.error.code == "parallel_render_staging_failed"
    assert outcome.result is None
    assert not MulticamParallelStore(root).final_root.exists() or not any(MulticamParallelStore(root).final_root.iterdir())


def test_unlisted_start_failure_cleans_staging_and_writes_closed_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _project, prepare_ref, _sources, _runtime = _prepare_project(
        tmp_path, monkeypatch
    )

    def unexpected_failure(*_args: object, **_kwargs: object) -> None:
        raise LookupError("unlisted camera failure")

    monkeypatch.setattr(parallel, "render_parallel_camera", unexpected_failure)
    operation_id = "op_00000000000040008000000000000109"
    outcome = parallel.start_multicam_parallel_render(
        root, operation_id=operation_id, prepare_ref=prepare_ref
    )

    assert outcome.record.status == "failed"
    assert outcome.record.error is not None
    assert outcome.record.error.code == "parallel_render_staging_failed"
    assert outcome.result is None
    store = MulticamParallelStore(root)
    assert not store.staging_path(operation_id).exists()
    assert MediaOperationStore(root, _project.project_id).read(operation_id) == outcome.record


def test_adopted_decision_scan_skips_unlisted_malformed_run_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = tmp_path / "workflow" / "runs"
    for name in ("receipts", "approvals"):
        (tmp_path / "workflow" / name).mkdir(parents=True)
    runs.mkdir(parents=True)
    (runs / "malformed.json").write_text("{}", encoding="utf-8")
    project = cast(
        Project,
        SimpleNamespace(active_edit_version_id="edit_v1", project_id="project", revision=1),
    )

    def unexpected_run(_payload: object) -> object:
        raise LookupError("unlisted malformed run")

    monkeypatch.setattr(parallel.WorkflowRun, "from_dict", unexpected_run)
    with pytest.raises(ParallelRenderError) as error:
        parallel._read_adopted_decision(tmp_path, project, "edit_v1")
    assert error.value.code == "parallel_render_decision_not_adopted"


def test_all_failed_cleanup_failure_does_not_replace_terminal_code_and_verify_failure_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _project, prepare_ref, _sources, _runtime = _prepare_project(tmp_path, monkeypatch)
    operation_id = "op_00000000000040008000000000000107"
    monkeypatch.setattr(parallel, "render_parallel_camera", lambda camera, **kwargs: (_ for _ in ()).throw(parallel.ParallelCameraEncodeError("fixture encode failure")))
    original_cleanup = parallel._cleanup_worker_staging
    monkeypatch.setattr(parallel, "_cleanup_worker_staging", lambda *args: (_ for _ in ()).throw(ParallelRenderError("parallel_render_staging_failed", "fixture cleanup failure")))
    encoded = parallel.start_multicam_parallel_render(root, operation_id=operation_id, prepare_ref=prepare_ref)
    assert encoded.record.error is not None and encoded.record.error.code == "parallel_render_all_cameras_failed"
    monkeypatch.setattr(parallel, "_cleanup_worker_staging", original_cleanup)
    from roughcut.adapters.ffmpeg.multicam_parallel import render_parallel_camera as real_render

    monkeypatch.setattr(parallel, "render_parallel_camera", real_render)
    monkeypatch.setattr(parallel, "verify_parallel_camera", lambda *args, **kwargs: (_ for _ in ()).throw(parallel.ParallelCameraVerifyError("fixture verify failure")))
    verified = parallel.start_multicam_parallel_render(root, operation_id="op_00000000000040008000000000000108", prepare_ref=prepare_ref)
    assert verified.record.error is not None and verified.record.error.code == "parallel_render_all_cameras_failed"
    assert verified.result is None


def test_mav_024_kill_error_blocks_for_reap_before_public_staging_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import roughcut.adapters.ffmpeg.multicam_parallel as adapter

    root, project, prepare_ref, _sources, _runtime = _prepare_project(
        tmp_path, monkeypatch
    )
    events: list[str] = []

    class KillErrorProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.terminate_count = 0
            self.kill_count = 0
            self.wait_count = 0

        def communicate(self) -> tuple[str, str]:
            raise subprocess.SubprocessError("fixture communicate failure")

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.terminate_count += 1
            events.append("terminate")

        def kill(self) -> None:
            self.kill_count += 1
            events.append("kill_error")
            assert self.poll() is None
            raise OSError("fixture kill failure")

        def wait(self, timeout: float | None = None) -> int:
            self.wait_count += 1
            if self.wait_count == 1:
                assert timeout == 5
                events.append("bounded_wait_timeout")
                raise subprocess.TimeoutExpired("ffmpeg", timeout or 0)
            assert timeout is None
            self.returncode = -9
            events.append("completed_wait")
            return self.returncode

    process = KillErrorProcess()
    monkeypatch.setattr(
        parallel,
        "_validate_media_runtime",
        lambda _runtime: ("ffmpeg version fixture", "ffprobe version fixture"),
    )
    monkeypatch.setattr(adapter.subprocess, "Popen", lambda *args, **kwargs: process)
    original_unlink = Path.unlink

    def tracked_unlink(path: Path, missing_ok: bool = False) -> None:
        if path.name.endswith(".filter.txt"):
            events.append("filter_cleanup")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", tracked_unlink)
    original_write = MediaOperationStore.write_locked

    def tracked_write(
        store: MediaOperationStore, record: object
    ) -> object:
        written = original_write(store, record)
        if written.status == "failed":
            events.append("terminal_transition")
        return written

    monkeypatch.setattr(MediaOperationStore, "write_locked", tracked_write)
    original_release = MediaOperationStore._release_file_lock

    def tracked_release(lock_file: object) -> None:
        events.append("lock_release")
        original_release(lock_file)

    monkeypatch.setattr(
        MediaOperationStore,
        "_release_file_lock",
        staticmethod(tracked_release),
    )
    operation_id = "op_00000000000040008000000000000116"
    outcome = parallel.start_multicam_parallel_render(
        root,
        operation_id=operation_id,
        prepare_ref=prepare_ref,
    )
    assert process.terminate_count == 1
    assert process.kill_count == 1
    assert process.wait_count == 2
    assert process.poll() == -9
    assert events.index("completed_wait") < events.index("filter_cleanup")
    assert events.index("completed_wait") < events.index("terminal_transition")
    assert events.index("completed_wait") < events.index("lock_release")
    record = MediaOperationStore(root, project.project_id).read(operation_id)
    assert record is not None and record == outcome.record
    assert record.status == "failed"
    assert record.error is not None
    assert record.error.code == "parallel_render_staging_failed"
    assert outcome.result is None
    public_record = json.dumps(record.to_dict())
    assert "fixture communicate failure" not in public_record
    assert "fixture kill failure" not in public_record
    assert not hasattr(adapter, "ParallelCameraChildReapError")
    assert not MulticamParallelStore(root).staging_path(operation_id).exists()


@pytest.mark.parametrize("interrupt_point", ("bounded_wait", "blocking_wait"))
def test_mav_024_defers_child_control_keyboard_interrupt_until_confirmed_reap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt_point: str,
) -> None:
    import roughcut.adapters.ffmpeg.multicam_parallel as adapter

    root, project, prepare_ref, _sources, _runtime = _prepare_project(
        tmp_path, monkeypatch
    )
    events: list[str] = []
    deferred_interrupt = KeyboardInterrupt(f"fixture {interrupt_point} interrupt")

    class DeferredInterruptProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.kill_count = 0
            self.wait_count = 0
            self.blocking_wait_count = 0

        def communicate(self) -> tuple[str, str]:
            raise subprocess.SubprocessError("fixture communicate failure")

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            events.append("terminate")

        def kill(self) -> None:
            self.kill_count += 1
            events.append("kill")

        def wait(self, timeout: float | None = None) -> int:
            self.wait_count += 1
            if timeout is not None:
                assert timeout == 5
                assert self.returncode is None
                assert self.poll() is None
                if interrupt_point == "bounded_wait":
                    events.append("interrupt_observed")
                    raise deferred_interrupt
                events.append("bounded_wait_timeout")
                raise subprocess.TimeoutExpired("ffmpeg", timeout)
            self.blocking_wait_count += 1
            assert self.returncode is None
            assert self.poll() is None
            if (
                interrupt_point == "blocking_wait"
                and self.blocking_wait_count == 1
            ):
                events.append("interrupt_observed")
                raise deferred_interrupt
            self.returncode = -9
            events.append("completed_wait")
            return self.returncode

    process = DeferredInterruptProcess()
    monkeypatch.setattr(
        parallel,
        "_validate_media_runtime",
        lambda _runtime: ("ffmpeg version fixture", "ffprobe version fixture"),
    )
    monkeypatch.setattr(adapter.subprocess, "Popen", lambda *args, **kwargs: process)
    original_unlink = Path.unlink

    def tracked_unlink(path: Path, missing_ok: bool = False) -> None:
        if path.name.endswith(".filter.txt"):
            events.append("filter_cleanup")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", tracked_unlink)
    original_write = MediaOperationStore.write_locked

    def tracked_write(
        store: MediaOperationStore, record: object
    ) -> object:
        written = original_write(store, record)
        if written.status == "interrupted":
            events.append("terminal_transition")
        return written

    monkeypatch.setattr(MediaOperationStore, "write_locked", tracked_write)
    original_release = MediaOperationStore._release_file_lock

    def tracked_release(lock_file: object) -> None:
        events.append("lock_release")
        original_release(lock_file)

    monkeypatch.setattr(
        MediaOperationStore,
        "_release_file_lock",
        staticmethod(tracked_release),
    )
    operation_id = {
        "bounded_wait": "op_00000000000040008000000000000117",
        "blocking_wait": "op_00000000000040008000000000000118",
    }[interrupt_point]
    with pytest.raises(KeyboardInterrupt) as raised:
        parallel.start_multicam_parallel_render(
            root,
            operation_id=operation_id,
            prepare_ref=prepare_ref,
        )
    events.append("keyboard_restored")

    assert raised.value is deferred_interrupt
    assert events.count("interrupt_observed") == 1
    assert process.kill_count == 1
    assert process.poll() == -9
    assert events.index("interrupt_observed") < events.index("completed_wait")
    assert events.index("completed_wait") < events.index("filter_cleanup")
    assert events.index("completed_wait") < events.index("terminal_transition")
    assert events.index("completed_wait") < events.index("lock_release")
    assert events.index("completed_wait") < events.index("keyboard_restored")
    record = MediaOperationStore(root, project.project_id).read(operation_id)
    assert record is not None and record.status == "interrupted"
    assert record.error is not None
    assert record.error.code == "parallel_render_interrupted"
    staging = MulticamParallelStore(root).staging_path(operation_id)
    assert staging.is_dir()
    assert not MulticamParallelStore(root).final_path(
        parallel_render_id(prepare_ref, operation_id)
    ).exists()


def test_mav_032_partial_and_mav_033_all_failed_use_real_ffmpeg_and_worker_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, project, prepare_ref, _sources, _runtime = _prepare_project(
        tmp_path, monkeypatch, camera_ids=("aux_1", "aux_2")
    )
    source_before = {
        source_id: (path.stat().st_size, path.stat().st_mtime_ns, fingerprint_file(path))
        for source_id, path in _sources.items()
    }
    original_render = parallel.render_parallel_camera

    def one_camera_fails(camera: dict[str, object], **kwargs: object) -> None:
        if camera["camera_id"] == "aux_1":
            output = kwargs["output_path"]
            assert isinstance(output, Path)
            output.write_bytes(b"partial-unverified")
            raise parallel.ParallelCameraEncodeError("fixture encode failure")
        original_render(camera, **kwargs)

    monkeypatch.setattr(parallel, "render_parallel_camera", one_camera_fails)
    partial = parallel.start_multicam_parallel_render(
        root,
        operation_id="op_00000000000040008000000000000091",
        prepare_ref=prepare_ref,
    )
    assert partial.record.status == "succeeded"
    assert partial.result is not None
    assert isinstance(partial.record.result_ref, ParallelRenderOperationResult)
    partial_evidence = partial.record.result_ref.partial_failure_evidence
    assert partial_evidence is not None
    assert partial_evidence.camera_id == "aux_1"
    assert partial_evidence.code == "parallel_camera_encode_failed"
    assert partial_evidence.check == "ffmpeg_encode"
    assert partial.result == partial.record.result_ref.to_dict()
    assert partial.result["partial_failure_evidence"] == partial_evidence.to_dict()
    manifest = MulticamParallelStore(root).read_published_manifest(
        partial.result["parallel_render_id"]
    )
    assert manifest is not None and manifest["delivery_status"] == "partial"
    assert not (
        MulticamParallelStore(root).final_path(partial.result["parallel_render_id"])
        / ".publish-intent.json"
    ).exists()
    failed = next(camera for camera in manifest["cameras"] if camera["camera_id"] == "aux_1")
    assert failed["output"] is None
    assert isinstance(failed["error"], dict)
    assert failed["error"] == {"code": "parallel_camera_encode_failed"}
    assert partial.record.error is None
    reloaded_partial = MediaOperationStore(root, project.project_id).read(
        "op_00000000000040008000000000000091"
    )
    assert reloaded_partial == partial.record
    first_status = media_operation_status(
        root, "op_00000000000040008000000000000091"
    )
    second_status = media_operation_status(
        root, "op_00000000000040008000000000000091"
    )
    assert first_status == partial.record
    assert second_status.to_dict() == first_status.to_dict()
    cli_main(
        [
            "media-operation-status",
            "--project",
            str(root),
            "--operation-id",
            "op_00000000000040008000000000000091",
            "--json",
        ]
    )
    cli_payload = json.loads(capsys.readouterr().out)
    assert cli_payload["media_operation"] == partial.record.to_dict()
    mcp_payload = handle_request(
        {
            "jsonrpc": "2.0",
            "id": "partial-status",
            "method": "tools/call",
            "params": {
                "name": "media_operation_status",
                "arguments": {
                    "project_path": str(root),
                    "operation_id": "op_00000000000040008000000000000091",
                },
            },
        }
    )
    assert mcp_payload is not None
    assert (
        mcp_payload["result"]["structuredContent"]["media_operation"]
        == partial.record.to_dict()
    )
    assert not (root / "renders" / "multicam-staging" / "op_00000000000040008000000000000091" / ".aux_1.mp4.tmp").exists()
    assert source_before == {
        source_id: (path.stat().st_size, path.stat().st_mtime_ns, fingerprint_file(path))
        for source_id, path in _sources.items()
    }

    def all_fail(camera: dict[str, object], **kwargs: object) -> None:
        output = kwargs["output_path"]
        assert isinstance(output, Path)
        output.write_bytes(b"partial-unverified")
        raise parallel.ParallelCameraEncodeError("fixture encode failure")

    monkeypatch.setattr(parallel, "render_parallel_camera", all_fail)
    failed_outcome = parallel.start_multicam_parallel_render(
        root,
        operation_id="op_00000000000040008000000000000092",
        prepare_ref=prepare_ref,
    )
    assert failed_outcome.record.status == "failed"
    assert failed_outcome.record.error is not None
    assert failed_outcome.record.error.code == "parallel_render_all_cameras_failed"
    assert failed_outcome.record.error.evidence is not None
    assert failed_outcome.record.error.evidence.camera_id == "aux_1"
    assert failed_outcome.record.error.evidence.code == "parallel_camera_encode_failed"
    assert failed_outcome.record.error.evidence.check == "ffmpeg_encode"
    assert failed_outcome.result is None
    assert not (root / "renders" / "multicam-staging" / "op_00000000000040008000000000000092").exists()
    assert not MulticamParallelStore(root).final_path(
        parallel_render_id(prepare_ref, "op_00000000000040008000000000000092")
    ).exists()
    reloaded_failed = MediaOperationStore(root, project.project_id).read(
        "op_00000000000040008000000000000092"
    )
    assert reloaded_failed == failed_outcome.record
    assert media_operation_status(
        root, "op_00000000000040008000000000000092"
    ).to_dict() == failed_outcome.record.to_dict()
    reader = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, sys; "
                "from pathlib import Path; "
                "from roughcut.application.media_operations import media_operation_status; "
                "record = media_operation_status(Path(sys.argv[1]), sys.argv[2]); "
                "print(json.dumps(record.to_dict(), sort_keys=True))"
            ),
            str(root),
            "op_00000000000040008000000000000091",
        ],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        },
    )
    reloaded_payload = json.loads(reader.stdout)
    assert (
        reloaded_payload["result_ref"]["partial_failure_evidence"]["camera_id"]
        == "aux_1"
    )
    assert reloaded_payload == first_status.to_dict()


def test_mav_013_024_025_026_028_031_037_start_identity_revalidation_and_hard_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _project, prepare_ref, source_paths, runtime_a = _prepare_project(tmp_path, monkeypatch)
    mutated = dict(prepare_ref)
    mutated["plan_basis_hash"] = "f" * 64
    with pytest.raises(ParallelRenderError, match="stale"):
        parallel.start_multicam_parallel_render(
            root,
            operation_id="op_00000000000040008000000000000093",
            prepare_ref=mutated,
        )
    assert not (root / "workflow" / "operations" / "media" / "op_00000000000040008000000000000093.json").exists()

    monkeypatch.setattr(
        parallel,
        "render_parallel_camera",
        lambda camera, **kwargs: (_ for _ in ()).throw(SystemExit(91)),
    )
    with pytest.raises(SystemExit):
        parallel.start_multicam_parallel_render(
            root,
            operation_id="op_00000000000040008000000000000094",
            prepare_ref=prepare_ref,
        )
    store = MediaOperationStore(root, ProjectStore(root).load().project_id)
    interrupted = store.read("op_00000000000040008000000000000094")
    assert interrupted is not None and interrupted.status == "interrupted"
    assert interrupted.error is not None and interrupted.error.code == "parallel_render_interrupted"
    assert (root / "renders" / "multicam-staging" / "op_00000000000040008000000000000094").exists()

    monkeypatch.setattr(parallel, "render_parallel_camera", parallel.render_parallel_camera)
    # Restore the real imported function after the intentional hard-exit case.
    from roughcut.adapters.ffmpeg.multicam_parallel import render_parallel_camera as real_render
    monkeypatch.setattr(parallel, "render_parallel_camera", real_render)
    first = parallel.start_multicam_parallel_render(
        root,
        operation_id="op_00000000000040008000000000000095",
        prepare_ref=prepare_ref,
    )
    real_render = parallel.render_parallel_camera
    monkeypatch.setattr(
        parallel,
        "render_parallel_camera",
        lambda *args, **kwargs: pytest.fail("existing-first invoked the render worker"),
    )
    same = parallel.start_multicam_parallel_render(
        root,
        operation_id="op_00000000000040008000000000000095",
        prepare_ref=prepare_ref,
    )
    assert same.readback is True and same.record == first.record
    monkeypatch.setattr(parallel, "render_parallel_camera", real_render)
    modified_request = dict(prepare_ref)
    modified_request["plan_basis_hash"] = "f" * 64
    with pytest.raises(MediaOperationError) as conflict:
        parallel.start_multicam_parallel_render(
            root,
            operation_id="op_00000000000040008000000000000095",
            prepare_ref=modified_request,
        )
    assert getattr(conflict.value, "code", None) == "operation_input_conflict"
    rerun = parallel.start_multicam_parallel_render(
        root,
        operation_id="op_00000000000040008000000000000096",
        prepare_ref=prepare_ref,
    )
    assert rerun.result is not None and rerun.result["parallel_render_id"] != first.result["parallel_render_id"]
    assert len(list((root / "renders" / "multicam").iterdir())) == 2
    del source_paths, runtime_a


def _mav_037_mutated_value(path: str, current: object) -> object:
    if isinstance(current, int) and not isinstance(current, bool):
        return current + 1
    if isinstance(current, list):
        return ["aux_fabricated"]
    assert isinstance(current, str)
    if path.endswith("hash"):
        return ("e" if current == "f" * 64 else "f") * 64
    if path.endswith("operation_type"):
        return "fabricated_operation"
    if path.endswith("kind"):
        return "fabricated_kind"
    return f"{current}_fabricated"


def _mav_037_mutate(payload: dict[str, object], path: str) -> None:
    parts = path.lstrip("/").split("/")
    current: object = payload
    for part in parts[:-1]:
        if isinstance(current, list):
            current = current[int(part)]
        else:
            assert isinstance(current, dict)
            current = current[part]
    leaf = parts[-1]
    if isinstance(current, list):
        index = int(leaf)
        current[index] = _mav_037_mutated_value(path, current[index])
    else:
        assert isinstance(current, dict)
        current[leaf] = _mav_037_mutated_value(path, current[leaf])


@pytest.mark.parametrize(
    ("case_kind", "case_value"),
    MAV_037_CASES,
    ids=[f"{kind}-{value}" for kind, value in MAV_037_CASES],
)
def test_mav_037_every_frozen_prepare_ref_case_is_isolated_and_zero_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case_kind: str,
    case_value: str,
) -> None:
    root, _project, prepare_ref, _sources, _runtime = _prepare_project(tmp_path, monkeypatch)
    fabricated = json.loads(json.dumps(prepare_ref))
    if case_kind == "mutation":
        _mav_037_mutate(fabricated, case_value)
    elif case_value == "unknown_top_level_field_added":
        fabricated["extra"] = "rejected"
    elif case_value == "unknown_nested_field_added":
        fabricated["decision_ref"]["extra"] = "rejected"
    elif case_value == "required_field_removed":
        del fabricated["plan_basis_hash"]
    else:
        fabricated["plan_basis_hash"] = (
            "e" if fabricated["plan_basis_hash"] == "f" * 64 else "f"
        ) * 64
    operation_id = "op_00000000000040008000000000000113"
    monkeypatch.setattr(
        parallel,
        "render_parallel_camera",
        lambda *args, **kwargs: pytest.fail("fabricated prepare started a child"),
    )
    monkeypatch.setattr(
        MulticamParallelStore,
        "create_staging",
        lambda *args, **kwargs: pytest.fail("fabricated prepare created staging"),
    )
    before = _tree_bytes(root)
    with pytest.raises(ParallelRenderError) as error:
        parallel.start_multicam_parallel_render(
            root,
            operation_id=operation_id,
            prepare_ref=fabricated,
        )
    assert error.value.code == "parallel_render_prepare_stale"
    assert _tree_bytes(root) == before
    assert not (
        root / "workflow" / "operations" / "media" / f"{operation_id}.json"
    ).exists()
    parallel_store = MulticamParallelStore(root)
    assert not parallel_store.staging_path(operation_id).exists()
    assert not parallel_store.final_root.exists()


@pytest.mark.parametrize("case", ("omitted", "failed", "current_decision_zero_mapped"))
def test_mav_034_prepare_delivery_gates_are_zero_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    partitions = (
        ((0, 240_000, "missing"),)
        if case != "current_decision_zero_mapped"
        else ((0, 120_000, "missing"), (120_000, 240_000, "mapped"))
    )
    root, project, _prepare_ref, _sources, _runtime = _prepare_project(
        tmp_path,
        monkeypatch,
        partitions=partitions,
        run_prepare=False,
        camera_status="failed" if case == "failed" else None,
        camera_error_code="auxiliary_decode_failed" if case == "failed" else None,
    )
    before = _tree_bytes(root)
    artifact = AlignmentStore(root).read("aln_phase3")
    assert artifact is not None
    ref = {
        "kind": "multicam_alignment",
        "alignment_id": artifact.alignment_id,
        "schema_version": 1,
        "content_hash": artifact.content_hash,
    }
    with pytest.raises(ParallelRenderError) as error:
        parallel.prepare_multicam_parallel_render(
            root,
            edit_version_id=project.active_edit_version_id,
            alignment_ref=ref,
            auxiliary_camera_ids=["aux_1"],
            expected_revision=project.revision,
        )
    assert error.value.code == "parallel_render_camera_not_deliverable"
    assert _tree_bytes(root) == before


@pytest.mark.parametrize("fault", ("interrupted", "failed", "result_ref_changed", "producer_mismatch"))
def test_mav_036_alignment_delivery_mutations_are_zero_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    root, project, _prepare_ref, _sources, _runtime = _prepare_project(
        tmp_path, monkeypatch, run_prepare=False
    )
    artifact_store = AlignmentStore(root)
    artifact = artifact_store.read("aln_phase3")
    assert artifact is not None
    media_store = MediaOperationStore(root, project.project_id)
    record = media_store.read(artifact.producer_operation_id)
    assert record is not None
    alignment_ref = AlignmentOperationResult(artifact.alignment_id, 1, artifact.content_hash)
    if fault in {"interrupted", "failed"}:
        status = "interrupted" if fault == "interrupted" else "failed"
        error = MediaOperationFailure(
            code="alignment_interrupted" if fault == "interrupted" else "alignment_disk_budget_exceeded",
            responsibility="roughcut_core",
            action="recover_abandoned_media_operation" if fault == "interrupted" else "recognize_auxiliary",
            message_code="alignment_interrupted" if fault == "interrupted" else "alignment_failed",
        )
        mutated = replace(
            record,
            status=status,
            phase_message_code="alignment_interrupted" if fault == "interrupted" else "alignment_failed",
            finished_at=record.updated_at,
            result_ref=None,
            error=error,
        )
        (media_store.records_root / f"{record.operation_id}.json").write_bytes(
            canonical_json_v1(mutated.to_dict()) + b"\n"
        )
    elif fault == "result_ref_changed":
        mutated = replace(
            record,
            result_ref=AlignmentOperationResult("aln_other", 1, "5" * 64),
        )
        (media_store.records_root / f"{record.operation_id}.json").write_bytes(
            canonical_json_v1(mutated.to_dict()) + b"\n"
        )
    else:
        artifact_path = artifact_store.artifacts_root / "aln_phase3.json"
        raw = json.loads(artifact_path.read_text(encoding="utf-8"))
        raw["producer_operation_id"] = "op_00000000000040008000000000000111"
        artifact_path.write_bytes(canonical_json_v1(raw) + b"\n")
        changed = artifact_store.read("aln_phase3")
        assert changed is not None
        alignment_ref = AlignmentOperationResult(changed.alignment_id, 1, changed.content_hash)
        mutated = replace(record, result_ref=alignment_ref)
        (media_store.records_root / f"{record.operation_id}.json").write_bytes(
            canonical_json_v1(mutated.to_dict()) + b"\n"
        )
    before = _tree_bytes(root)
    with pytest.raises(ParallelRenderError) as error:
        parallel.prepare_multicam_parallel_render(
            root,
            edit_version_id=project.active_edit_version_id,
            alignment_ref=alignment_ref.to_dict(),
            auxiliary_camera_ids=["aux_1"],
            expected_revision=project.revision,
        )
    assert error.value.code == "parallel_render_alignment_not_deliverable"
    assert _tree_bytes(root) == before


def test_mav_014_black_silence_and_mapped_av_are_verified_from_real_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, project, prepare_ref, _sources, _runtime = _prepare_project(
        tmp_path,
        monkeypatch,
        partitions=(
            (0, 60_000, "mapped"),
            (60_000, 120_000, "missing"),
            (120_000, 180_000, "uncertain"),
            (180_000, 240_000, "conflict"),
        ),
        auxiliary_rotation=90,
    )
    rotated_source = next(source for source in project.sources if source.source_id != "src_a")
    assert rotated_source.probe.rotation_degrees == 90
    assert (rotated_source.probe.width, rotated_source.probe.height) == (320, 240)
    outcome = parallel.start_multicam_parallel_render(
        root,
        operation_id="op_00000000000040008000000000000102",
        prepare_ref=prepare_ref,
    )
    assert outcome.result is not None
    manifest = MulticamParallelStore(root).read_published_manifest(
        outcome.result["parallel_render_id"]
    )
    assert manifest is not None
    assert not (
        MulticamParallelStore(root).final_path(outcome.result["parallel_render_id"])
        / ".publish-intent.json"
    ).exists()
    output = manifest["cameras"][0]["output"]
    assert isinstance(output, dict)
    output_path = (
        root
        / "renders"
        / "multicam"
        / outcome.result["parallel_render_id"]
        / output["filename"]
    )
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    oriented_pixel = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            "0.25",
            "-i",
            str(output_path),
            "-vf",
            "crop=1:1:iw/2:ih/4,format=rgb24",
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-",
        ],
        check=True,
        capture_output=True,
    ).stdout
    assert len(oriented_pixel) == 3
    red, green, blue = oriented_pixel
    assert red > 150 and green < 100 and blue < 100
    mapped_video = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", "0.25", "-i", str(output_path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        check=True,
        capture_output=True,
    ).stdout
    black_video = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", "0.75", "-i", str(output_path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        check=True,
        capture_output=True,
    ).stdout
    assert max(mapped_video) > 10
    assert max(black_video) <= 2

    mapped_audio = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", "0.25", "-t", "0.1", "-i", str(output_path), "-map", "0:a:0", "-ac", "1", "-ar", "48000", "-f", "f32le", "-"],
        check=True,
        capture_output=True,
    ).stdout
    silent_audio = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", "0.75", "-t", "0.1", "-i", str(output_path), "-map", "0:a:0", "-ac", "1", "-ar", "48000", "-f", "f32le", "-"],
        check=True,
        capture_output=True,
    ).stdout
    import struct

    mapped_samples = struct.unpack(f"{len(mapped_audio) // 4}f", mapped_audio)
    silent_samples = struct.unpack(f"{len(silent_audio) // 4}f", silent_audio)
    assert max(abs(sample) for sample in mapped_samples) > 0.01
    assert max(abs(sample) for sample in silent_samples) < 0.001


def test_real_full_range_auxiliary_is_converted_to_limited_yuv420p_through_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, project, prepare_ref, source_paths, runtime = _prepare_project(
        tmp_path,
        monkeypatch,
        partitions=(
            (0, 60_000, "mapped"),
            (60_000, 120_000, "missing"),
            (120_000, 180_000, "missing"),
            (180_000, 240_000, "missing"),
        ),
        auxiliary_builder=_make_full_range_auxiliary,
    )
    assert prepare_ref is not None
    source_before = {
        source_id: (path.stat().st_size, path.stat().st_mtime_ns, fingerprint_file(path))
        for source_id, path in source_paths.items()
    }
    auxiliary = next(source for source in project.sources if source.source_id != "src_a")
    input_path = source_paths[auxiliary.source_id]
    input_probe = json.loads(
        subprocess.run(
            [runtime.ffprobe.resolved_path, "-v", "error", "-show_streams", "-of", "json", str(input_path)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    input_video = next(stream for stream in input_probe["streams"] if stream["codec_type"] == "video")
    input_audio = next(stream for stream in input_probe["streams"] if stream["codec_type"] == "audio")
    assert input_video["pix_fmt"] == "yuv422p10le"
    assert input_video["color_range"] == "pc"
    assert input_audio["sample_rate"] == "48000"
    assert input_audio["channels"] == 2

    prepared = parallel.prepare_multicam_parallel_render(
        root,
        edit_version_id=project.active_edit_version_id,
        alignment_ref=cast(dict[str, object], prepare_ref["alignment_ref"]),
        auxiliary_camera_ids=["aux_1"],
        expected_revision=project.revision,
    )
    camera = prepared.summary["cameras"][0]
    assert isinstance(camera, dict)
    slots = camera["slots"]
    assert isinstance(slots, list)
    settings = prepared.summary["output_settings"]
    assert isinstance(settings, dict)
    filter_script = _build_filter_script(
        slots,
        {source.source_id: source for source in project.sources},
        settings,
        total_samples=slots[-1]["audio_sample_end"],
    )
    command = _build_command(
        slots,
        source_paths=source_paths,
        sources={source.source_id: source for source in project.sources},
        output_path=tmp_path / "camera_aux_1.mp4",
        settings=settings,
        ffmpeg=runtime.ffmpeg,
        total_frames=slots[-1]["video_frame_end"],
        total_samples=slots[-1]["audio_sample_end"],
        filter_script=tmp_path / "production.filter.txt",
    )
    assert "in_range=auto:out_range=tv" in filter_script
    assert filter_script.count("setparams=range=limited") >= 2
    color_range_index = command.index("-color_range")
    assert command[color_range_index + 1] == "tv"
    filter_index = command.index("-/filter_complex")
    assert command[filter_index : filter_index + 2] == [
        "-/filter_complex",
        str(tmp_path / "production.filter.txt"),
    ]
    assert "-filter_complex_script" not in command

    operation_id = "op_00000000000040008000000000000121"
    outcome = parallel.start_multicam_parallel_render(
        root,
        operation_id=operation_id,
        prepare_ref=prepare_ref,
    )
    assert outcome.record.schema_version == 2
    assert outcome.record.status == "succeeded"
    assert outcome.result is not None
    assert set(outcome.result) == {
        "kind",
        "parallel_render_id",
        "schema_version",
        "manifest_content_hash",
    }
    assert isinstance(outcome.record.result_ref, ParallelRenderOperationResult)
    assert outcome.record.result_ref.partial_failure_evidence is None
    manifest = MulticamParallelStore(root).read_published_manifest(
        outcome.result["parallel_render_id"]
    )
    assert manifest is not None
    assert manifest["delivery_status"] == "complete"
    assert manifest["video_frame_quota"] == prepared.summary["video_frame_quota"]
    assert manifest["audio_sample_quota"] == prepared.summary["audio_sample_quota"]
    output = manifest["cameras"][0]["output"]
    assert isinstance(output, dict)
    output_path = (
        root
        / "renders"
        / "multicam"
        / outcome.result["parallel_render_id"]
        / output["filename"]
    )
    output_probe = json.loads(
        subprocess.run(
            [
                runtime.ffprobe.resolved_path,
                "-v",
                "error",
                "-count_frames",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(output_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    video = next(stream for stream in output_probe["streams"] if stream["codec_type"] == "video")
    audio = next(stream for stream in output_probe["streams"] if stream["codec_type"] == "audio")
    assert video["codec_name"] == "h264"
    assert (video["width"], video["height"]) == (1920, 1080)
    assert video["pix_fmt"] == "yuv420p"
    assert video["pix_fmt"] != "yuvj420p"
    if "color_range" in video:
        assert video["color_range"] in {"tv", "limited"}
    assert video["avg_frame_rate"] == "25/1"
    assert int(video["nb_read_frames"]) == manifest["video_frame_quota"]
    assert audio["codec_name"] == "aac"
    assert audio["sample_rate"] == "48000"
    assert audio["channels"] == 2
    verified = verify_parallel_camera(
        camera,
        output_path=output_path,
        settings=settings,
        ffmpeg=runtime.ffmpeg,
        ffprobe=runtime.ffprobe,
    )
    assert verified.accepted is True
    assert abs(verified.audio_samples - manifest["audio_sample_quota"]) <= 1024
    assert not MulticamParallelStore(root).staging_path(operation_id).exists()
    assert not any(path.name.endswith(".filter.txt") for path in output_path.parent.iterdir())
    assert source_before == {
        source_id: (path.stat().st_size, path.stat().st_mtime_ns, fingerprint_file(path))
        for source_id, path in source_paths.items()
    }


def test_real_ffmpeg_noninteger_global_boundaries_and_wrong_timeline_are_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _root, project, _prepare_ref, source_paths, runtime = _prepare_project(
        tmp_path, monkeypatch
    )
    auxiliary = next(
        source for source in project.sources if source.source_id != "src_a"
    )
    split_ticks = 30_029
    second_split_ticks = 60_061
    total_ticks = 120_000
    first_frame = frame_boundary(split_ticks, 25, 1)
    second_frame = frame_boundary(second_split_ticks, 25, 1)
    total_frames = frame_boundary(total_ticks, 25, 1)
    first_sample = sample_boundary(split_ticks, 48_000)
    second_sample = sample_boundary(second_split_ticks, 48_000)
    total_samples = sample_boundary(total_ticks, 48_000)
    assert (
        first_frame,
        second_frame,
        total_frames,
        first_sample,
        second_sample,
        total_samples,
    ) == (
        6,
        13,
        25,
        12_012,
        24_024,
        48_000,
    )
    camera = {
        "slots": [
            {
                "classification": "mapped",
                "video_frame_start": 0,
                "video_frame_end": first_frame,
                "video_frame_quota": first_frame,
                "audio_sample_start": 0,
                "audio_sample_end": first_sample,
                "audio_sample_quota": first_sample,
                "auxiliary_ref": {
                    "source_id": auxiliary.source_id,
                    "source_start_ticks": 0,
                    "source_end_ticks": split_ticks,
                },
            },
            {
                "classification": "missing",
                "video_frame_start": first_frame,
                "video_frame_end": second_frame,
                "video_frame_quota": second_frame - first_frame,
                "audio_sample_start": first_sample,
                "audio_sample_end": second_sample,
                "audio_sample_quota": second_sample - first_sample,
                "auxiliary_ref": None,
            },
            {
                "classification": "uncertain",
                "video_frame_start": second_frame,
                "video_frame_end": total_frames,
                "video_frame_quota": total_frames - second_frame,
                "audio_sample_start": second_sample,
                "audio_sample_end": total_samples,
                "audio_sample_quota": total_samples - second_sample,
                "auxiliary_ref": None,
            },
        ]
    }
    settings = {
        "width": 320,
        "height": 240,
        "frame_rate": {"numerator": 25, "denominator": 1},
        "audio_sample_rate": 48_000,
    }
    output_path = tmp_path / "noninteger-boundaries.mp4"
    render_parallel_camera(
        camera,
        source_paths=source_paths,
        sources={source.source_id: source for source in project.sources},
        output_path=output_path,
        settings=settings,
        ffmpeg=runtime.ffmpeg,
    )
    verified = verify_parallel_camera(
        camera,
        output_path=output_path,
        settings=settings,
        ffmpeg=runtime.ffmpeg,
        ffprobe=runtime.ffprobe,
    )
    assert verified.frame_count == total_frames
    assert abs(verified.audio_samples - total_samples) <= 1024
    wrong_timeline = json.loads(json.dumps(camera))
    wrong_timeline["slots"][-1]["audio_sample_end"] = total_samples + 1025
    with pytest.raises(ParallelCameraVerifyError):
        verify_parallel_camera(
            wrong_timeline,
            output_path=output_path,
            settings=settings,
            ffmpeg=runtime.ffmpeg,
            ffprobe=runtime.ffprobe,
        )


def _quota_probe_camera(*, total_frames: int, total_samples: int) -> dict[str, object]:
    return {
        "slots": [
            {
                "classification": "mapped",
                "video_frame_start": 0,
                "video_frame_end": total_frames,
                "video_frame_quota": total_frames,
                "audio_sample_start": 0,
                "audio_sample_end": total_samples,
                "audio_sample_quota": total_samples,
                "auxiliary_ref": {
                    "source_id": "src_aux",
                    "source_start_ticks": 0,
                    "source_end_ticks": 192_000,
                },
            }
        ]
    }


def _fake_quota_probe_run(
    *, timeline_delta: int, decoded_delta: int
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "-show_streams" in command:
            payload = {
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 320,
                        "height": 240,
                        "avg_frame_rate": "25/1",
                        "pix_fmt": "yuv420p",
                        "nb_read_frames": "50",
                    },
                    {
                        "codec_type": "audio",
                        "codec_name": "aac",
                        "sample_rate": "48000",
                        "channels": 2,
                        "time_base": "1/48000",
                        "duration_ts": str(96_000 + timeline_delta),
                    },
                ]
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        if "-af" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                "",
                "[Parsed_astats_0] Overall\n"
                f"[Parsed_astats_0] Number of samples: {96_000 + decoded_delta}\n",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    return run


@pytest.mark.parametrize("delta", [0, 1, -1, 608, -608, 1024, -1024])
def test_parallel_timeline_and_decoded_samples_share_one_aac_tolerance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delta: int
) -> None:
    output_path = tmp_path / "camera.mp4"
    output_path.write_bytes(b"candidate")
    monkeypatch.setattr(
        camera_adapter.subprocess,
        "run",
        _fake_quota_probe_run(timeline_delta=delta, decoded_delta=delta),
    )
    verified = verify_parallel_camera(
        _quota_probe_camera(total_frames=50, total_samples=96_000),
        output_path=output_path,
        settings={
            "width": 320,
            "height": 240,
            "frame_rate": {"numerator": 25, "denominator": 1},
            "audio_sample_rate": 48_000,
        },
        ffmpeg=ToolResolution("ffmpeg", "/tools/ffmpeg", "fixture"),
        ffprobe=ToolResolution("ffprobe", "/tools/ffprobe", "fixture"),
    )
    assert verified.accepted is True
    assert verified.frame_count == 50
    assert verified.audio_samples == 96_000 + delta


@pytest.mark.parametrize(
    "timeline_delta,decoded_delta",
    [(1025, 0), (-1025, 0), (0, 1025), (0, -1025)],
)
def test_parallel_sample_quota_beyond_one_aac_frame_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    timeline_delta: int,
    decoded_delta: int,
) -> None:
    output_path = tmp_path / "camera.mp4"
    output_path.write_bytes(b"candidate")
    monkeypatch.setattr(
        camera_adapter.subprocess,
        "run",
        _fake_quota_probe_run(timeline_delta=timeline_delta, decoded_delta=decoded_delta),
    )
    with pytest.raises(ParallelCameraVerifyError):
        verify_parallel_camera(
            _quota_probe_camera(total_frames=50, total_samples=96_000),
            output_path=output_path,
            settings={
                "width": 320,
                "height": 240,
                "frame_rate": {"numerator": 25, "denominator": 1},
                "audio_sample_rate": 48_000,
            },
            ffmpeg=ToolResolution("ffmpeg", "/tools/ffmpeg", "fixture"),
            ffprobe=ToolResolution("ffprobe", "/tools/ffprobe", "fixture"),
        )


def test_mav_025_031_publish_window_and_runtime_change_leave_closed_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, project, prepare_ref, _sources, runtime_a = _prepare_project(tmp_path, monkeypatch)
    assert prepare_ref is not None
    real_publish = MulticamParallelStore.publish

    def publish_then_exit(store: MulticamParallelStore, staging: Path, render_id: str, manifest: dict[str, object]) -> Path:
        real_publish(store, staging, render_id, manifest)
        raise SystemExit(92)

    monkeypatch.setattr(MulticamParallelStore, "publish", publish_then_exit)
    operation_id = "op_00000000000040008000000000000097"
    with pytest.raises(SystemExit):
        parallel.start_multicam_parallel_render(root, operation_id=operation_id, prepare_ref=prepare_ref)
    store = MediaOperationStore(root, project.project_id)
    interrupted = store.read(operation_id)
    assert interrupted is not None and interrupted.status == "interrupted"
    assert interrupted.result_ref is None
    render_id = parallel_render_id(prepare_ref, operation_id)
    parallel_store = MulticamParallelStore(root)
    with pytest.raises(ParallelRenderError, match="unresolved publish intent"):
        parallel_store.read_published_manifest(render_id)
    final = parallel_store.final_path(render_id)
    assert final.is_dir()
    assert (final / ".publish-intent.json").is_file()
    assert parallel_store.read_manifest_file(final / "manifest.json")
    assert media_operation_status(root, operation_id).status == "interrupted"
    reader = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, sys; "
                "from pathlib import Path; "
                "from roughcut.application.media_operations import media_operation_status; "
                "print(json.dumps(media_operation_status(Path(sys.argv[1]), sys.argv[2]).to_dict(), sort_keys=True))"
            ),
            str(root),
            operation_id,
        ],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
    )
    assert json.loads(reader.stdout) == interrupted.to_dict()

    monkeypatch.setattr(MulticamParallelStore, "publish", real_publish)
    retry = parallel.start_multicam_parallel_render(
        root, operation_id=operation_id, prepare_ref=prepare_ref
    )
    assert retry.readback is True and retry.record == interrupted
    runtime_b = _runtime(shutil.which("ffmpeg") or "ffmpeg", shutil.which("ffprobe") or "ffprobe", "z")
    values = iter((runtime_a, runtime_b))
    monkeypatch.setattr(parallel, "_load_persistent_runtime", lambda: next(values))
    failed = parallel.start_multicam_parallel_render(
        root,
        operation_id="op_00000000000040008000000000000098",
        prepare_ref=prepare_ref,
    )
    assert failed.record.status == "failed"
    assert failed.record.error is not None
    assert failed.record.error.code == "parallel_render_runtime_changed_during_run"
    assert not (root / "renders" / "multicam" / parallel_render_id(prepare_ref, "op_00000000000040008000000000000098")).exists()


def test_mav_publish_failure_is_a_durable_terminal_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, project, prepare_ref, _sources, _runtime = _prepare_project(
        tmp_path, monkeypatch
    )

    def publish_failure(
        _store: MulticamParallelStore,
        _staging: Path,
        _render_id: str,
        _manifest: dict[str, object],
    ) -> Path:
        raise ParallelRenderError(
            "parallel_render_publish_failed",
            "fixture final publish conflict",
        )

    monkeypatch.setattr(MulticamParallelStore, "publish", publish_failure)
    operation_id = "op_00000000000040008000000000000119"
    outcome = parallel.start_multicam_parallel_render(
        root, operation_id=operation_id, prepare_ref=prepare_ref
    )
    assert outcome.record.status == "failed"
    assert outcome.result is None
    assert outcome.record.error is not None
    assert outcome.record.error.code == "parallel_render_publish_failed"
    assert outcome.record.error.evidence is not None
    assert outcome.record.error.evidence.code == "parallel_manifest_publish_failed"
    assert outcome.record.error.evidence.check == "manifest_publish"
    store = MediaOperationStore(root, project.project_id)
    reloaded = store.read(operation_id)
    assert reloaded == outcome.record
    assert media_operation_status(root, operation_id).to_dict() == outcome.record.to_dict()
    parallel_store = MulticamParallelStore(root)
    render_id = parallel_render_id(prepare_ref, operation_id)
    assert not parallel_store.staging_path(operation_id).exists()
    assert parallel_store.read_published_manifest(render_id) is None
    assert not parallel_store.final_path(render_id).exists()


def test_mav_publish_failure_before_move_cleans_staging_and_writes_no_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _project, prepare_ref, _sources, _runtime = _prepare_project(tmp_path, monkeypatch)
    monkeypatch.setattr(
        parallel_store_module,
        "_atomic_no_replace_move",
        lambda _source, _destination: (_ for _ in ()).throw(OSError("fixture move failure")),
    )
    operation_id = "op_00000000000040008000000000000122"
    outcome = parallel.start_multicam_parallel_render(
        root, operation_id=operation_id, prepare_ref=prepare_ref
    )
    assert outcome.record.status == "failed"
    assert outcome.result is None
    assert outcome.record.error is not None
    assert outcome.record.error.code == "parallel_render_publish_failed"
    store = MulticamParallelStore(root)
    assert not store.staging_path(operation_id).exists()
    assert not store.final_path(parallel_render_id(prepare_ref, operation_id)).exists()
    assert media_operation_status(root, operation_id).to_dict() == outcome.record.to_dict()


@pytest.mark.parametrize("post_move_failure", ("sync", "validate"))
def test_mav_post_move_failure_rolls_final_back_before_terminal_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    post_move_failure: str,
) -> None:
    root, _project, prepare_ref, _sources, _runtime = _prepare_project(tmp_path, monkeypatch)
    operation_id = (
        "op_00000000000040008000000000000123"
        if post_move_failure == "sync"
        else "op_00000000000040008000000000000124"
    )
    store = MulticamParallelStore(root)
    render_id = parallel_render_id(prepare_ref, operation_id)
    final_root = store.final_root
    if post_move_failure == "sync":
        real_sync = MulticamParallelStore._sync
        calls = 0

        def fail_final_sync(path: Path) -> None:
            nonlocal calls
            if path == final_root and calls == 0:
                calls += 1
                raise ParallelRenderError("parallel_render_publish_failed", "fixture post-move sync failure")
            real_sync(path)

        monkeypatch.setattr(MulticamParallelStore, "_sync", staticmethod(fail_final_sync))
    else:
        real_validate = MulticamParallelStore._validate_final_tree
        calls = 0

        def fail_final_validate(
            current_store: MulticamParallelStore,
            final: Path,
            manifest: dict[str, object],
            *,
            allow_publish_intent: bool = False,
        ) -> None:
            nonlocal calls
            if calls == 0:
                calls += 1
                raise ParallelRenderError("parallel_render_publish_failed", "fixture post-move validation failure")
            real_validate(
                current_store,
                final,
                manifest,
                allow_publish_intent=allow_publish_intent,
            )

        monkeypatch.setattr(MulticamParallelStore, "_validate_final_tree", fail_final_validate)
    outcome = parallel.start_multicam_parallel_render(
        root, operation_id=operation_id, prepare_ref=prepare_ref
    )
    assert outcome.record.status == "failed"
    assert outcome.result is None
    assert outcome.record.error is not None
    assert outcome.record.error.code == "parallel_render_publish_failed"
    assert outcome.record.error.evidence is not None
    assert outcome.record.error.evidence.check == "manifest_publish"
    assert not store.final_path(render_id).exists()
    assert not store.staging_path(operation_id).exists()
    assert media_operation_status(root, operation_id).to_dict() == outcome.record.to_dict()


def test_mav_post_move_rollback_failure_preserves_primary_and_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _project, prepare_ref, _sources, _runtime = _prepare_project(tmp_path, monkeypatch)
    operation_id = "op_00000000000040008000000000000125"
    store = MulticamParallelStore(root)
    render_id = parallel_render_id(prepare_ref, operation_id)
    final = store.final_path(render_id)
    real_validate = MulticamParallelStore._validate_final_tree
    calls = 0

    def fail_final_validate(
        current_store: MulticamParallelStore,
        current_final: Path,
        manifest: dict[str, object],
        *,
        allow_publish_intent: bool = False,
    ) -> None:
        nonlocal calls
        if calls == 0:
            calls += 1
            raise ParallelRenderError("parallel_render_publish_failed", "fixture post-move validation failure")
        real_validate(
            current_store,
            current_final,
            manifest,
            allow_publish_intent=allow_publish_intent,
        )

    monkeypatch.setattr(MulticamParallelStore, "_validate_final_tree", fail_final_validate)
    real_move = parallel_store_module._atomic_no_replace_move

    def fail_rollback(source: Path, destination: Path) -> None:
        if source == final:
            raise OSError("fixture rollback failure")
        real_move(source, destination)

    monkeypatch.setattr(parallel_store_module, "_atomic_no_replace_move", fail_rollback)
    outcome = parallel.start_multicam_parallel_render(
        root, operation_id=operation_id, prepare_ref=prepare_ref
    )
    assert outcome.record.status == "failed"
    assert outcome.result is None
    assert outcome.record.error is not None
    assert outcome.record.error.code == "parallel_render_publish_failed"
    evidence = outcome.record.error.evidence
    assert evidence is not None
    assert evidence.code == "parallel_manifest_publish_failed"
    assert evidence.check == "manifest_publish_recovery"
    assert evidence.expected == "ambiguous_final_removed"
    assert evidence.actual == "recovery_failed"
    assert final.is_dir()
    assert (final / ".publish-intent.json").is_file()
    assert not store.staging_path(operation_id).exists()
    with pytest.raises(ParallelRenderError, match="unresolved publish intent"):
        store.read_published_manifest(render_id)


@pytest.mark.parametrize("unsafe_final", ("unknown", "symlink", "hardlink"))
def test_mav_post_move_rollback_refuses_unknown_or_unsafe_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_final: str,
) -> None:
    root, _project, prepare_ref, _sources, _runtime = _prepare_project(tmp_path, monkeypatch)
    operation_id = {
        "unknown": "op_00000000000040008000000000000129",
        "symlink": "op_00000000000040008000000000000130",
        "hardlink": "op_00000000000040008000000000000131",
    }[unsafe_final]
    store = MulticamParallelStore(root)
    render_id = parallel_render_id(prepare_ref, operation_id)
    final = store.final_path(render_id)
    real_validate = MulticamParallelStore._validate_final_tree
    calls = 0

    def fail_final_validate(
        current_store: MulticamParallelStore,
        current_final: Path,
        manifest: dict[str, object],
        *,
        allow_publish_intent: bool = False,
    ) -> None:
        nonlocal calls
        if calls == 0:
            calls += 1
            output = next(path for path in current_final.iterdir() if path.name.startswith("camera_"))
            if unsafe_final == "unknown":
                (current_final / "unexpected-entry").write_bytes(b"fixture")
            else:
                output.unlink()
                if unsafe_final == "symlink":
                    output.symlink_to(current_final / "manifest.json")
                else:
                    os.link(current_final / "manifest.json", output)
            raise ParallelRenderError("parallel_render_publish_failed", "fixture unsafe final")
        real_validate(
            current_store,
            current_final,
            manifest,
            allow_publish_intent=allow_publish_intent,
        )

    monkeypatch.setattr(MulticamParallelStore, "_validate_final_tree", fail_final_validate)
    outcome = parallel.start_multicam_parallel_render(
        root, operation_id=operation_id, prepare_ref=prepare_ref
    )
    assert outcome.record.status == "failed"
    assert outcome.record.error is not None
    evidence = outcome.record.error.evidence
    assert evidence is not None and evidence.check == "manifest_publish_recovery"
    assert final.is_dir()
    assert (final / ".publish-intent.json").is_file()
    assert not store.staging_path(operation_id).exists()
    if unsafe_final == "unknown":
        assert (final / "unexpected-entry").is_file()
    elif unsafe_final == "symlink":
        assert next(path for path in final.iterdir() if path.name.startswith("camera_")).is_symlink()
    else:
        output = next(path for path in final.iterdir() if path.name.startswith("camera_"))
        assert output.stat().st_nlink > 1


def test_mav_post_move_cleanup_failure_preserves_primary_and_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _project, prepare_ref, _sources, _runtime = _prepare_project(tmp_path, monkeypatch)
    operation_id = "op_00000000000040008000000000000126"
    store = MulticamParallelStore(root)
    render_id = parallel_render_id(prepare_ref, operation_id)
    real_validate = MulticamParallelStore._validate_final_tree
    calls = 0

    def fail_final_validate(
        current_store: MulticamParallelStore,
        final: Path,
        manifest: dict[str, object],
        *,
        allow_publish_intent: bool = False,
    ) -> None:
        nonlocal calls
        if calls == 0:
            calls += 1
            raise ParallelRenderError("parallel_render_publish_failed", "fixture post-move validation failure")
        real_validate(
            current_store,
            final,
            manifest,
            allow_publish_intent=allow_publish_intent,
        )

    monkeypatch.setattr(MulticamParallelStore, "_validate_final_tree", fail_final_validate)
    monkeypatch.setattr(
        parallel,
        "_cleanup_worker_staging",
        lambda *_args: (_ for _ in ()).throw(
            ParallelRenderError("parallel_render_staging_failed", "fixture cleanup failure")
        ),
    )
    outcome = parallel.start_multicam_parallel_render(
        root, operation_id=operation_id, prepare_ref=prepare_ref
    )
    assert outcome.record.status == "failed"
    assert outcome.record.error is not None
    assert outcome.record.error.code == "parallel_render_publish_failed"
    evidence = outcome.record.error.evidence
    assert evidence is not None and evidence.check == "manifest_publish_recovery"
    assert not store.final_path(render_id).exists()
    assert store.staging_path(operation_id).is_dir()


def test_mav_coordinator_final_conflict_preserves_existing_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _project, prepare_ref, _sources, _runtime = _prepare_project(tmp_path, monkeypatch)
    first = parallel.start_multicam_parallel_render(
        root,
        operation_id="op_00000000000040008000000000000127",
        prepare_ref=prepare_ref,
    )
    assert first.result is not None
    winner_id = cast(str, first.result["parallel_render_id"])
    store = MulticamParallelStore(root)
    winner_before = _tree_bytes(store.final_path(winner_id))
    conflict_operation_id = "op_00000000000040008000000000000128"
    conflict_id = parallel_render_id(prepare_ref, conflict_operation_id)
    shutil.copytree(store.final_path(winner_id), store.final_path(conflict_id))
    conflict_before = _tree_bytes(store.final_path(conflict_id))
    conflict = parallel.start_multicam_parallel_render(
        root,
        operation_id=conflict_operation_id,
        prepare_ref=prepare_ref,
    )
    assert conflict.record.status == "failed"
    assert conflict.result is None
    assert conflict.record.error is not None
    assert conflict.record.error.code == "parallel_render_final_conflict"
    assert conflict.record.error.evidence is not None
    assert conflict.record.error.evidence.check == "manifest_publish"
    assert not store.staging_path(conflict_operation_id).exists()
    assert _tree_bytes(store.final_path(winner_id)) == winner_before
    assert _tree_bytes(store.final_path(conflict_id)) == conflict_before


def test_mav_success_record_gates_marker_when_cleanup_repeats_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, project, prepare_ref, _sources, _runtime = _prepare_project(tmp_path, monkeypatch)
    operation_id = "op_00000000000040008000000000000132"
    calls = 0

    def fail_clear(
        store: MulticamParallelStore,
        parallel_id: str,
        exact_operation_id: str,
        manifest_hash: str,
    ) -> None:
        nonlocal calls
        del store, parallel_id, exact_operation_id, manifest_hash
        calls += 1
        raise ParallelRenderError("parallel_render_publish_failed", "fixture marker cleanup interruption")

    monkeypatch.setattr(MulticamParallelStore, "clear_publish_intent", fail_clear)
    outcome = parallel.start_multicam_parallel_render(
        root, operation_id=operation_id, prepare_ref=prepare_ref
    )
    assert outcome.record.status == "succeeded"
    assert outcome.result is not None
    store = MulticamParallelStore(root)
    final = store.final_path(cast(str, outcome.result["parallel_render_id"]))
    assert (final / ".publish-intent.json").is_file()
    manifest = store.read_published_manifest(cast(str, outcome.result["parallel_render_id"]))
    assert manifest is not None
    assert MediaOperationStore(root, project.project_id).read(operation_id) == outcome.record
    assert media_operation_status(root, operation_id) == outcome.record
    assert media_operation_status(root, operation_id) == outcome.record
    assert store.read_published_manifest(cast(str, outcome.result["parallel_render_id"])) == manifest
    retry = parallel.start_multicam_parallel_render(
        root, operation_id=operation_id, prepare_ref=prepare_ref
    )
    assert retry.readback is True
    assert retry.record == outcome.record
    assert retry.result == outcome.result
    assert calls == 3
    intent_path = final / ".publish-intent.json"
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    intent["parallel_render_id"] = "mpr_wrong_identity"
    intent_path.write_bytes(canonical_json_v1(intent) + b"\n")
    with pytest.raises(ParallelRenderError, match="publish intent identity changed"):
        store.read_published_manifest(cast(str, outcome.result["parallel_render_id"]))


def test_mav_success_readback_survives_post_unlink_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _project, prepare_ref, _sources, _runtime = _prepare_project(tmp_path, monkeypatch)
    operation_id = "op_00000000000040008000000000000133"
    real_clear = MulticamParallelStore.clear_publish_intent

    def clear_then_fail(
        store: MulticamParallelStore,
        parallel_id: str,
        exact_operation_id: str,
        manifest_hash: str,
    ) -> None:
        real_clear(store, parallel_id, exact_operation_id, manifest_hash)
        raise ParallelRenderError("parallel_render_publish_failed", "fixture post-unlink sync failure")

    monkeypatch.setattr(MulticamParallelStore, "clear_publish_intent", clear_then_fail)
    outcome = parallel.start_multicam_parallel_render(
        root, operation_id=operation_id, prepare_ref=prepare_ref
    )
    assert outcome.record.status == "succeeded"
    assert outcome.result is not None
    store = MulticamParallelStore(root)
    render_id = cast(str, outcome.result["parallel_render_id"])
    assert not (store.final_path(render_id) / ".publish-intent.json").exists()
    assert store.read_published_manifest(render_id) is not None
    assert media_operation_status(root, operation_id) == outcome.record


def test_mav_024_hard_exit_after_verify_preserves_staging_and_child_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, project, prepare_ref, _sources, _runtime = _prepare_project(tmp_path, monkeypatch)
    real_verify = parallel.verify_parallel_camera

    def verify_then_exit(*args: object, **kwargs: object) -> object:
        verified = real_verify(*args, **kwargs)
        assert verified.accepted is True
        raise SystemExit(94)

    monkeypatch.setattr(parallel, "verify_parallel_camera", verify_then_exit)
    operation_id = "op_00000000000040008000000000000115"
    with pytest.raises(SystemExit):
        parallel.start_multicam_parallel_render(
            root, operation_id=operation_id, prepare_ref=prepare_ref
        )
    record = MediaOperationStore(root, project.project_id).read(operation_id)
    assert record is not None and record.status == "interrupted"
    assert record.error is not None and record.error.code == "parallel_render_interrupted"
    staging = MulticamParallelStore(root).staging_path(operation_id)
    assert staging.is_dir()
    assert any(path.name.endswith(".mp4.tmp") for path in staging.iterdir())
    assert not MulticamParallelStore(root).final_path(
        parallel_render_id(prepare_ref, operation_id)
    ).exists()
    staging_before_status = tuple(
        sorted(path.relative_to(staging) for path in staging.rglob("*"))
    )
    assert media_operation_status(root, operation_id).status == "interrupted"
    assert staging.is_dir()
    assert tuple(
        sorted(path.relative_to(staging) for path in staging.rglob("*"))
    ) == staging_before_status
    monkeypatch.setattr(parallel, "verify_parallel_camera", real_verify)


@pytest.mark.parametrize("mutation", ("mapped_source", "plan_basis"))
def test_mav_031_basis_revalidation_rejects_source_and_plan_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    root, _project, prepare_ref, source_paths, _runtime = _prepare_project(tmp_path, monkeypatch)
    assert prepare_ref is not None
    operation_id = "op_00000000000040008000000000000114"
    if mutation == "mapped_source":
        source_path = next(path for source_id, path in source_paths.items() if source_id != "src_a")
        original_bytes = source_path.read_bytes()
        original_mtime = source_path.stat().st_mtime_ns
        real_render = parallel.render_parallel_camera

        def render_then_mutate(camera: dict[str, object], **kwargs: object) -> None:
            real_render(camera, **kwargs)
            source_path.write_bytes(original_bytes + b"basis mutation")

        monkeypatch.setattr(parallel, "render_parallel_camera", render_then_mutate)
        outcome = parallel.start_multicam_parallel_render(
            root, operation_id=operation_id, prepare_ref=prepare_ref
        )
        source_path.write_bytes(original_bytes)
        os.utime(source_path, ns=(original_mtime, original_mtime))
    else:
        real_prepare = parallel.prepare_multicam_parallel_render
        prepare_calls = 0

        def prepare_then_change(*args: object, **kwargs: object) -> parallel.ParallelPrepareOutcome:
            nonlocal prepare_calls
            prepare_calls += 1
            prepared = real_prepare(*args, **kwargs)
            if prepare_calls >= 2:
                changed_basis = dict(prepared.basis)
                changed_basis["fixture_plan_change"] = True
                return replace(prepared, basis=changed_basis)
            return prepared

        monkeypatch.setattr(parallel, "prepare_multicam_parallel_render", prepare_then_change)
        outcome = parallel.start_multicam_parallel_render(
            root, operation_id=operation_id, prepare_ref=prepare_ref
        )
        assert prepare_calls == 2
    assert outcome.record.status == "failed"
    assert outcome.record.error is not None
    assert outcome.record.error.code == "parallel_render_basis_changed_during_run"
    assert outcome.result is None
    assert not MulticamParallelStore(root).final_root.exists() or not any(MulticamParallelStore(root).final_root.iterdir())


def test_mav_027_store_publish_is_no_replace_and_final_readback_is_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _project, prepare_ref, _sources, _runtime = _prepare_project(tmp_path, monkeypatch)
    first = parallel.start_multicam_parallel_render(
        root,
        operation_id="op_00000000000040008000000000000099",
        prepare_ref=prepare_ref,
    )
    assert first.result is not None
    store = MulticamParallelStore(root)
    original_manifest = store.read_published_manifest(first.result["parallel_render_id"])
    assert original_manifest is not None
    original_final = store.final_path(first.result["parallel_render_id"])
    race_operation_id = "op_00000000000040008000000000000100"
    race_target = parallel_render_id(original_manifest["prepare_ref"], race_operation_id)
    store_a = MulticamParallelStore(root)
    store_b = MulticamParallelStore(root)
    store_a.staging_root = root / "renders" / "multicam-staging-race-a"
    store_b.staging_root = root / "renders" / "multicam-staging-race-b"
    store_a.final_root = store.final_root
    store_b.final_root = store.final_root
    staging_a = store_a.create_staging(race_operation_id)
    staging_b = store_b.create_staging(race_operation_id)

    def build_manifest(staging: Path, *, variant: str) -> dict[str, object]:
        manifest = copy.deepcopy(original_manifest)
        producer = manifest["producer"]
        assert isinstance(producer, dict)
        producer["operation_id"] = race_operation_id
        manifest["parallel_render_id"] = race_target
        manifest_file = manifest["manifest_file"]
        assert isinstance(manifest_file, dict)
        manifest_file["project_relative_path"] = (
            f"renders/multicam/{race_target}/manifest.json"
        )
        has_output = False
        changed = False
        cameras = manifest["cameras"]
        assert isinstance(cameras, list)
        for camera in cameras:
            assert isinstance(camera, dict)
            if camera["render_status"] != "succeeded":
                continue
            output = camera["output"]
            assert isinstance(output, dict)
            filename = output["filename"]
            assert isinstance(filename, str)
            shutil.copy2(original_final / filename, staging / filename)
            has_output = True
            output["project_relative_path"] = (
                f"renders/multicam/{race_target}/{filename}"
            )
            if variant == "b" and not changed:
                payload = (staging / filename).read_bytes() + b"\nfixture-tree-b"
                (staging / filename).write_bytes(payload)
                output["bytes"] = len(payload)
                output["content_hash"] = hashlib.sha256(payload).hexdigest()
                changed = True
        assert has_output
        if variant == "b":
            assert changed
        store_for_staging = store_a if variant == "a" else store_b
        store_for_staging.write_manifest(staging, manifest)
        return manifest

    manifest_a = build_manifest(staging_a, variant="a")
    manifest_b = build_manifest(staging_b, variant="b")

    def tree_hash(path: Path) -> str:
        digest = hashlib.sha256()
        for child in sorted(path.iterdir(), key=lambda item: item.name):
            digest.update(child.name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(child.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    tree_hash_a = tree_hash(staging_a)
    tree_hash_b = tree_hash(staging_b)
    assert staging_a != staging_b
    assert tree_hash_a != tree_hash_b
    errors: list[ParallelRenderError] = []
    successes: list[tuple[Path, dict[str, object]]] = []

    def publish(
        publisher: MulticamParallelStore,
        staging: Path,
        manifest: dict[str, object],
    ) -> None:
        try:
            successes.append((publisher.publish(staging, race_target, manifest), manifest))
        except ParallelRenderError as error:
            errors.append(error)

    threads = [
        threading.Thread(target=publish, args=(store_a, staging_a, manifest_a)),
        threading.Thread(target=publish, args=(store_b, staging_b, manifest_b)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(successes) == 1
    assert len(errors) == 1 and errors[0].code == "parallel_render_final_conflict"
    winning_final, winning_manifest = successes[0]
    store.clear_publish_intent(
        race_target,
        race_operation_id,
        canonical_sha256_v1(winning_manifest),
    )
    final_manifest = store.read_published_manifest(race_target)
    assert final_manifest is not None
    assert store.read_manifest_file(store.final_path(race_target) / "manifest.json") == final_manifest
    assert final_manifest == winning_manifest
    assert final_manifest == manifest_a or final_manifest == manifest_b
    final_hash = tree_hash(winning_final)
    assert final_hash == (tree_hash_a if winning_manifest == manifest_a else tree_hash_b)


def test_cli_and_mcp_parallel_dispatch_success_and_alignment_error_envelopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, project, prepare_ref, _sources, _runtime = _prepare_project(tmp_path, monkeypatch)
    assert prepare_ref is not None
    alignment = AlignmentStore(root).read("aln_phase3")
    assert alignment is not None
    alignment_ref = {
        "kind": "multicam_alignment",
        "alignment_id": alignment.alignment_id,
        "schema_version": 1,
        "content_hash": alignment.content_hash,
    }
    cli_main(
        [
            "multicam-parallel-render-prepare",
            "--project", str(root),
            "--edit-version-id", project.active_edit_version_id,
            "--alignment-ref-json", json.dumps(alignment_ref),
            "--auxiliary-camera-ids-json", '["aux_1"]',
            "--expected-revision", str(project.revision),
            "--json",
        ]
    )
    cli_success = json.loads(capsys.readouterr().out)
    assert cli_success["prepare_ref"] == prepare_ref
    mcp_success = handle_request(
        {
            "jsonrpc": "2.0",
            "id": "parallel-prepare-success",
            "method": "tools/call",
            "params": {
                "name": "multicam_parallel_render_prepare",
                "arguments": {
                    "project_path": str(root),
                    "edit_version_id": project.active_edit_version_id,
                    "alignment_ref": alignment_ref,
                    "auxiliary_camera_ids": ["aux_1"],
                    "expected_revision": project.revision,
                },
            },
        }
    )
    assert mcp_success is not None
    assert mcp_success["result"]["structuredContent"]["prepare_ref"] == prepare_ref

    def alignment_error(*args: object, **kwargs: object) -> object:
        raise AlignmentError("alignment_main_probe_failed", "fixture error")

    monkeypatch.setattr(cli_module, "run_align_multicam", alignment_error)
    with pytest.raises(SystemExit):
        cli_main(
            [
                "align-multicam", "--project", str(root),
                "--operation-id", "op_00000000000040008000000000000109",
                "--alignment-id", "aln_dispatch_error", "--expected-revision", str(project.revision),
                "--main-camera-json", '{"camera_id":"main","ordered_source_ids":["src_a"]}',
                "--auxiliary-cameras-json", '[{"camera_id":"aux_1","ordered_source_ids":["src_aux_1"]}]',
                "--main-audio-stable", "true", "--max-temporary-disk-bytes", "1",
                "--max-analysis-memory-bytes", "1", "--max-runtime-seconds", "1", "--json",
            ]
        )
    cli_error = json.loads(capsys.readouterr().out)
    assert cli_error["error"] == {"code": "alignment_main_probe_failed"}
    assert "traceback" not in json.dumps(cli_error).lower()

    monkeypatch.setattr(mcp_module, "run_align_multicam", alignment_error)
    mcp_error = handle_request(
        {
            "jsonrpc": "2.0",
            "id": "alignment-dispatch-error",
            "method": "tools/call",
            "params": {
                "name": "align_multicam",
                "arguments": {
                    "project_path": str(root),
                    "operation_id": "op_00000000000040008000000000000110",
                    "alignment_id": "aln_dispatch_error",
                    "expected_revision": project.revision,
                    "main_camera": {"camera_id": "main", "ordered_source_ids": ["src_a"]},
                    "auxiliary_cameras": [{"camera_id": "aux_1", "ordered_source_ids": ["src_aux_1"]}],
                    "main_audio_stable": True,
                    "max_temporary_disk_bytes": 1,
                    "max_analysis_memory_bytes": 1,
                    "max_runtime_seconds": 1,
                },
            },
        }
    )
    assert mcp_error is not None
    error_payload = mcp_error["result"]["structuredContent"]
    assert error_payload["error"] == {"code": "alignment_main_probe_failed"}
    assert "traceback" not in json.dumps(error_payload).lower()
