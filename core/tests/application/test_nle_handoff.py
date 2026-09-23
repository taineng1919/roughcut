from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

import pytest
import test_workflows as workflow_fixtures
from test_multicam_alignment_continuation import (
    _adopt_without_alignment_runner,
    _fixture_alignment_artifact,
)
from test_workflows import _advance_to_export_review, _workflow_project

from roughcut import m2_7_public_capability
from roughcut.adapters.alignment_store import AlignmentStore
from roughcut.adapters.media_operation_store import MediaOperationStore
from roughcut.adapters.project_store import ProjectStore
from roughcut.adapters.workflow_store import WorkflowStore
from roughcut.application import multicam_continuation as continuation_module
from roughcut.application import nle_handoff
from roughcut.application.alignments import AlignmentOutcome
from roughcut.application.nle_handoff import (
    approve_nle_export,
    project_handoff_timeline,
)
from roughcut.application.sources import fingerprint_file
from roughcut.application.workflows import workflow_action
from roughcut.domain.alignment import (
    AUDALIGN_CORRELATION_ALGORITHM_NAME,
    AUDALIGN_CORRELATION_ALGORITHM_VERSION,
    AUDALIGN_CORRELATION_PROFILE_NAME,
    AUDALIGN_CORRELATION_PROFILE_VERSION,
    AUDALIGN_CORRELATION_RECOGNIZER,
    AUDALIGN_CORRELATION_UPSTREAM_COMMIT,
    AUDALIGN_VERSION,
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
from roughcut.domain.brief import EditBrief
from roughcut.domain.edit import EditClip, EditDecision, EditProposal
from roughcut.domain.media_operation import (
    AlignmentOperationResult,
    MediaOperationFailure,
    MediaOperationRecord,
)
from roughcut.domain.nle_handoff import NleExportReceipt, NleHandoffError
from roughcut.domain.project import ImportMode, MediaProbe, SourceAsset, SourceFingerprint
from roughcut.domain.time import TICKS_PER_SECOND, RationalRate


def _source(
    source_id: str,
    path: Path,
    *,
    duration_ticks: int = 20 * TICKS_PER_SECOND,
    digest: str = "a" * 64,
) -> SourceAsset:
    return SourceAsset(
        source_id=source_id,
        kind="video",
        display_name=f"{source_id} 片段",
        import_mode=ImportMode.LINKED,
        locator={"absolute_path": str(path)},
        fingerprint=SourceFingerprint(1, 1, digest),
        probe=MediaProbe(
            duration_ticks=duration_ticks,
            container_start_ticks=0,
            first_content_ticks=0,
            video_codec="h264",
            width=1920,
            height=1080,
            nominal_frame_rate={"numerator": 25, "denominator": 1},
            is_vfr=False,
            audio_codec="aac",
            audio_sample_rate=48_000,
            rotation_degrees=0,
        ),
    )


def _decision(project_id: str, *, source_id: str = "src_a") -> tuple[EditDecision, object]:
    del project_id
    brief = EditBrief("brief_1", "主题", 240_000, ("重点",), False)
    clips = (
        EditClip(
            "clip_1",
            source_id,
            "tr_a",
            "seg_1",
            2 * TICKS_PER_SECOND,
            5 * TICKS_PER_SECOND,
            "保留",
            "一",
        ),
        EditClip(
            "clip_2",
            source_id,
            "tr_a",
            "seg_2",
            8 * TICKS_PER_SECOND,
            11 * TICKS_PER_SECOND,
            "保留",
            "二",
        ),
    )
    proposal = EditProposal(
        proposal_id="proposal_1",
        base_project_revision=1,
        base_edit_version_id=None,
        source_id=source_id,
        transcript_version_id="tr_a",
        brief_snapshot=brief,
        context_hash="b" * 64,
        clips=clips,
        total_duration_ticks=6 * TICKS_PER_SECOND,
        created_at="fixture",
    )
    return (
        EditDecision(
            edit_version_id="edit_1",
            proposal_snapshot=proposal,
            project_revision=1,
            created_at="fixture",
        ),
        clips,
    )


def test_single_projection_never_needs_alignment_and_keeps_decision_ranges(tmp_path: Path) -> None:
    media = tmp_path / "单机位 source.mp4"
    source = _source("src_a", media)
    decision, clips = _decision("project_fixture")
    project = replace(
        ProjectStore(_workflow_project(tmp_path)).load(),
        revision=1,
        sources=(source,),
        active_edit_version_id=decision.edit_version_id,
    )

    timeline = project_handoff_timeline(
        project,
        decision,
        {source.source_id: media.absolute()},
        alignment=None,
    )

    assert timeline.duration_ticks == 6 * TICKS_PER_SECOND
    assert len(timeline.tracks) == 1
    assert [item.logical_clip_id for item in timeline.main_track.clips] == [
        clip.clip_id for clip in clips
    ]
    assert [
        (
            item.source_in_ticks,
            item.source_out_ticks,
            item.timeline_in_ticks,
            item.timeline_out_ticks,
        )
        for item in timeline.main_track.clips
    ] == [
        (2 * TICKS_PER_SECOND, 5 * TICKS_PER_SECOND, 0, 3 * TICKS_PER_SECOND),
        (8 * TICKS_PER_SECOND, 11 * TICKS_PER_SECOND, 3 * TICKS_PER_SECOND, 6 * TICKS_PER_SECOND),
    ]
    assert timeline.auxiliary_tracks == ()
    for item in timeline.main_track.clips:
        assert item.source_width == source.probe.width
        assert item.source_height == source.probe.height
        assert item.source_nominal_frame_rate == RationalRate(25, 1)
        assert item.source_is_vfr is source.probe.is_vfr
        assert item.source_audio_sample_rate == source.probe.audio_sample_rate


def test_multicam_projection_uses_exact_alignment_and_emits_gap(tmp_path: Path) -> None:
    root = _workflow_project(tmp_path)
    source_a = _source("src_a", tmp_path / "A.mp4", duration_ticks=14_400_000, digest="a" * 64)
    source_b = _source("src_b", tmp_path / "B.mp4", duration_ticks=14_400_000, digest="b" * 64)
    base = ProjectStore(root).load()
    decision, _ = _decision("project_fixture")
    project = replace(
        base,
        revision=1,
        sources=(source_a, source_b),
        active_edit_version_id=decision.edit_version_id,
    )
    ProjectStore(root).save(project, expected_revision=base.revision)
    alignment = _fixture_alignment_artifact(
        root,
        operation_id="op_alignment",
        alignment_id="alignment_1",
        request_hash="c" * 64,
        input_hash="d" * 64,
        partial=True,
    )
    # The fixture alignment is for src_a/src_b and covers the first half as mapped.
    project = ProjectStore(root).load()
    mapped_decision = replace(
        decision,
        proposal_snapshot=replace(
            decision.proposal_snapshot,
            clips=(
                EditClip("clip_1", "src_a", "tr_a", "seg_1", 0, 7_200_000, "保留", "一"),
                EditClip("clip_2", "src_a", "tr_a", "seg_2", 7_200_000, 14_400_000, "保留", "二"),
            ),
            total_duration_ticks=14_400_000,
        ),
    )
    timeline = project_handoff_timeline(
        project,
        mapped_decision,
        {"src_a": (tmp_path / "A.mp4").absolute(), "src_b": (tmp_path / "B.mp4").absolute()},
        alignment=alignment,
    )

    aux = timeline.auxiliary_tracks[0]
    assert [(item.timeline_in_ticks, item.timeline_out_ticks) for item in aux.clips] == [
        (0, 7_200_000)
    ]
    assert [(gap.timeline_in_ticks, gap.timeline_out_ticks) for gap in aux.gaps] == [
        (7_200_000, 14_400_000)
    ]
    assert aux.clips[0].source_in_ticks == 0
    assert aux.clips[0].source_out_ticks == 7_200_000
    current_sources = {source.source_id: source for source in project.sources}
    for item in (*timeline.main_track.clips, *aux.clips):
        current_source = current_sources[item.source_id]
        assert item.source_width == current_source.probe.width
        assert item.source_height == current_source.probe.height
        assert item.source_nominal_frame_rate == RationalRate(25, 1)
        assert item.source_is_vfr is current_source.probe.is_vfr
        assert item.source_audio_sample_rate == current_source.probe.audio_sample_rate


def test_multicam_projection_treats_unpaired_main_source_as_legal_gap(tmp_path: Path) -> None:
    root = _workflow_project(tmp_path)
    source_a = _source("src_a", tmp_path / "A.mp4", duration_ticks=14_400_000, digest="a" * 64)
    source_n = _source("src_n", tmp_path / "N.mp4", duration_ticks=14_400_000, digest="c" * 64)
    source_b = _source("src_b", tmp_path / "B.mp4", duration_ticks=14_400_000, digest="b" * 64)
    base = ProjectStore(root).load()
    decision, _ = _decision("project_fixture")
    unpaired_decision = replace(
        decision,
        proposal_snapshot=replace(
            decision.proposal_snapshot,
            clips=(
                EditClip("clip_1", "src_a", "tr_a", "seg_1", 0, 7_200_000, "保留", "一"),
                EditClip("clip_2", "src_n", "tr_n", "seg_2", 0, 7_200_000, "保留", "二"),
            ),
            total_duration_ticks=14_400_000,
        ),
    )
    project = replace(
        base,
        revision=1,
        sources=(source_a, source_n, source_b),
        active_edit_version_id=unpaired_decision.edit_version_id,
    )
    ProjectStore(root).save(project, expected_revision=base.revision)
    alignment = _fixture_alignment_artifact(
        root,
        operation_id="op_alignment",
        alignment_id="alignment_1",
        request_hash="c" * 64,
        input_hash="d" * 64,
        partial=True,
    )
    # The exact Alignment only declares src_a as its main camera Source.
    assert alignment.main_camera.ordered_source_ids == ("src_a",)

    timeline = project_handoff_timeline(
        project,
        unpaired_decision,
        {
            "src_a": (tmp_path / "A.mp4").absolute(),
            "src_n": (tmp_path / "N.mp4").absolute(),
            "src_b": (tmp_path / "B.mp4").absolute(),
        },
        alignment=alignment,
    )

    aux = timeline.auxiliary_tracks[0]
    assert [(clip.source_id, clip.timeline_in_ticks, clip.timeline_out_ticks) for clip in aux.clips] == [
        ("src_b", 0, 7_200_000)
    ]
    assert [(gap.timeline_in_ticks, gap.timeline_out_ticks) for gap in aux.gaps] == [
        (7_200_000, 14_400_000)
    ]


def _prepare_single_export(
    tmp_path: Path,
    *,
    is_vfr: bool = False,
) -> tuple[Path, str, int]:
    root = _workflow_project(tmp_path)
    media = tmp_path / "export source.mp4"
    media.write_bytes(b"source bytes for nle export")
    project = ProjectStore(root).load()
    source = replace(
        project.sources[0],
        kind="video",
        locator={"absolute_path": str(media)},
        fingerprint=fingerprint_file(media),
        probe=MediaProbe(
            duration_ticks=600_000,
            container_start_ticks=0,
            first_content_ticks=0,
            video_codec="h264",
            width=1920,
            height=1080,
            nominal_frame_rate={"numerator": 25, "denominator": 1},
            is_vfr=is_vfr,
            audio_codec="aac",
            audio_sample_rate=48_000,
            rotation_degrees=0,
        ),
    )
    ProjectStore(root).save(
        replace(project, revision=project.revision + 1, sources=(source,)),
        expected_revision=project.revision,
    )
    adopted = _advance_to_export_review(root)
    ref = adopted.workflow_run.artifact_refs["decision"]
    assert ref is not None
    revision = ProjectStore(root).load().revision
    return root, ref.artifact_id, revision


def test_approved_nle_export_rejects_vfr_source_at_application_boundary(
    tmp_path: Path,
) -> None:
    root, edit_version_id, revision = _prepare_single_export(tmp_path, is_vfr=True)
    destination = tmp_path / "vfr.fcpxml"
    with pytest.raises(NleHandoffError) as error:
        approve_nle_export(
            root,
            run_id="wfr_test",
            action_id="nle_vfr",
            edit_version_id=edit_version_id,
            expected_revision=revision,
            route="fcpxml",
            destination=destination,
            alignment_artifact_id=None,
        )
    assert error.value.code == "nle_export_rate_unsupported"
    assert not destination.exists()


def test_multicam_alignment_loader_requires_exact_project_and_result_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _workflow_project(tmp_path)
    run = _adopt_without_alignment_runner(root, monkeypatch)
    artifact = _fixture_alignment_artifact(
        root,
        operation_id="op_alignment",
        alignment_id="alignment_exact",
        request_hash="c" * 64,
        input_hash="d" * 64,
        partial=True,
    )
    from roughcut.adapters.alignment_store import AlignmentStore

    AlignmentStore(root).publish(artifact.alignment_id, artifact)
    project = ProjectStore(root).load()
    status = {
        "status": "partial",
        "alignment_ref": {
            "kind": "multicam_alignment",
            "alignment_id": artifact.alignment_id,
            "schema_version": 1,
            "content_hash": artifact.content_hash,
        },
    }
    loaded = nle_handoff._load_alignment(
        root,
        project,
        run,
        status,
        artifact.alignment_id,
    )
    assert loaded == artifact
    with pytest.raises(NleHandoffError) as mismatch:
        nle_handoff._load_alignment(
            root,
            project,
            run,
            status,
            "alignment_other",
        )
    assert mismatch.value.code == "nle_export_alignment_not_deliverable"


def _prepare_multicam_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    producer_succeeds: bool = True,
) -> tuple[Path, str, int, str, str]:
    monkeypatch.setattr(
        m2_7_public_capability.platform,
        "system",
        lambda: "Darwin",
    )
    root = _workflow_project(tmp_path)
    source_a_path = tmp_path / "A 主机位.mp4"
    source_b_path = tmp_path / "B 副机位.mp4"
    source_a_path.write_bytes(b"source A")
    source_b_path.write_bytes(b"source B")
    project = ProjectStore(root).load()
    video_probe = MediaProbe(
        duration_ticks=14_400_000,
        container_start_ticks=0,
        first_content_ticks=0,
        video_codec="h264",
        width=1920,
        height=1080,
        nominal_frame_rate={"numerator": 25, "denominator": 1},
        is_vfr=False,
        audio_codec="aac",
        audio_sample_rate=48_000,
        rotation_degrees=0,
    )
    source_a = replace(
        project.sources[0],
        kind="video",
        display_name="A 主机位",
        locator={"absolute_path": str(source_a_path)},
        fingerprint=fingerprint_file(source_a_path),
        probe=video_probe,
    )
    ProjectStore(root).save(
        replace(project, revision=project.revision + 1, sources=(source_a,)),
        expected_revision=project.revision,
    )

    def add_auxiliary_source(project_root: Path, source_id: str) -> None:
        current = ProjectStore(project_root).load()
        auxiliary = SourceAsset(
            source_id=source_id,
            kind="video",
            display_name="B 副机位",
            import_mode=ImportMode.LINKED,
            locator={"absolute_path": str(source_b_path)},
            fingerprint=fingerprint_file(source_b_path),
            probe=video_probe,
        )
        ProjectStore(project_root).save(
            replace(
                current,
                revision=current.revision + 1,
                sources=(*current.sources, auxiliary),
            ),
            expected_revision=current.revision,
        )

    monkeypatch.setattr(workflow_fixtures, "_add_fixture_source", add_auxiliary_source)
    setup = {
        "schema_version": 1,
        "main_camera": {"camera_id": "main", "ordered_source_ids": ["src_a"]},
        "auxiliary_cameras": [{"camera_id": "aux", "ordered_source_ids": ["src_b"]}],
        "source_pairs": [{"main_source_id": "src_a", "auxiliary_source_id": "src_b"}],
    }

    def succeed(*_args: object, **kwargs: object) -> AlignmentOutcome:
        operation_id = str(kwargs["operation_id"])
        alignment_id = str(kwargs["alignment_id"])
        current_project = ProjectStore(root).load()
        current_run = WorkflowStore(root).read_run("wfr_test")
        continuation = current_run.multicam_alignment_continuation
        assert continuation is not None and continuation.request_hash is not None
        artifact = _fixture_alignment_artifact(
            root,
            operation_id=operation_id,
            alignment_id=alignment_id,
            request_hash=continuation.request_hash,
            input_hash="e" * 64,
            partial=False,
        )
        sources = {source.source_id: source for source in current_project.sources}
        basis = tuple(
            replace(
                item,
                fingerprint=replace(
                    item.fingerprint,
                    size=sources[item.source_id].fingerprint.size,
                    mtime_ns=sources[item.source_id].fingerprint.mtime_ns,
                    sha256_head_tail=sources[item.source_id].fingerprint.sha256_head_tail,
                ),
                duration_ticks=sources[item.source_id].probe.duration_ticks,
            )
            for item in artifact.source_basis
        )
        artifact = replace(artifact, source_basis=basis)
        AlignmentStore(root).publish(alignment_id, artifact)
        store = MediaOperationStore(root, current_project.project_id)
        result = AlignmentOperationResult(
            alignment_id=alignment_id,
            schema_version=1,
            content_hash=artifact.content_hash,
        )
        record = MediaOperationRecord(
            operation_id=operation_id,
            scope=store.scope,
            operation_type="align_multicam",
            request_hash=continuation.request_hash,
            input_hash="e" * 64,
            status="succeeded" if producer_succeeds else "failed",
            phase_message_code=("alignment_succeeded" if producer_succeeds else "alignment_failed"),
            created_at="2026-09-02T00:00:00.000000Z",
            started_at="2026-09-02T00:00:01.000000Z",
            updated_at="2026-09-02T00:00:02.000000Z",
            finished_at="2026-09-02T00:00:02.000000Z",
            result_ref=result if producer_succeeds else None,
            error=(
                None
                if producer_succeeds
                else MediaOperationFailure(
                    code="alignment_input_stale",
                    responsibility="roughcut_core",
                    action="validate_alignment_basis",
                    message_code="alignment_failed",
                )
            ),
            schema_version=2,
        )
        with store.writer(operation_id, create=True) as acquired:
            assert acquired
            store.write_locked(record)
        return AlignmentOutcome(record, artifact, False)

    monkeypatch.setattr(continuation_module, "run_align_multicam", succeed)
    submitted = workflow_fixtures._submit_two_binding_draft(root, multicam_setup=setup)
    mutation = submitted.receipt.mutation
    assert mutation is not None
    approved = workflow_action(
        root,
        "wfr_test",
        "nle_multicam_approve_draft",
        "approve_draft",
        {
            "schema_version": 1,
            "content_draft_ref": {
                "artifact_id": mutation.artifact_id,
                "schema_version": mutation.schema_version,
                "content_hash": mutation.content_hash,
            },
        },
    )
    proposal_ref = approved.workflow_run.artifact_refs["proposal"]
    assert proposal_ref is not None
    adopted = workflow_action(
        root,
        "wfr_test",
        "nle_multicam_adopt",
        "adopt_roughcut",
        {"schema_version": 1, "proposal_ref": proposal_ref.to_dict()},
    )
    decision_ref = adopted.workflow_run.artifact_refs["decision"]
    continuation = adopted.workflow_run.multicam_alignment_continuation
    assert decision_ref is not None and continuation is not None
    assert continuation.operation_id is not None and continuation.alignment_id is not None
    return (
        root,
        decision_ref.artifact_id,
        ProjectStore(root).load().revision,
        continuation.operation_id,
        continuation.alignment_id,
    )


def test_multicam_approved_nle_export_uses_exact_succeeded_alignment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, edit_version_id, revision, _operation_id, alignment_id = _prepare_multicam_export(
        tmp_path, monkeypatch
    )
    alignment_before = AlignmentStore(root).read(alignment_id)
    assert alignment_before is not None
    outcome = approve_nle_export(
        root,
        run_id="wfr_test",
        action_id="nle_multicam",
        edit_version_id=edit_version_id,
        expected_revision=revision,
        route="fcpxml",
        destination=tmp_path / "multicam.fcpxml",
        alignment_artifact_id=alignment_id,
    )
    alignment_after = AlignmentStore(root).read(alignment_id)
    assert alignment_after is not None
    assert alignment_after.to_dict() == alignment_before.to_dict()
    assert outcome.receipt.alignment_artifact_id == alignment_id
    assert outcome.receipt.output_bytes > 0
    assert outcome.timeline_summary is not None
    assert outcome.timeline_summary["track_count"] == 2
    assert outcome.timeline_summary["tracks"][1]["gap_count"] == 0


def _build_multicam_alignment_artifact(
    root: Path,
    *,
    main_source_ids: tuple[str, ...],
    mapped_source_ids: tuple[str, ...],
    operation_id: str,
    alignment_id: str,
    request_hash: str,
    input_hash: str,
) -> MulticamAlignmentArtifact:
    """Build one internally valid artifact whose main camera group is exact."""

    duration = 14_400_000
    profile = AlignmentVerificationProfile(
        AUDALIGN_CORRELATION_PROFILE_NAME,
        AUDALIGN_CORRELATION_PROFILE_VERSION,
    )
    algorithm = AlignmentAlgorithm(
        name=AUDALIGN_CORRELATION_ALGORITHM_NAME,
        version=AUDALIGN_CORRELATION_ALGORITHM_VERSION,
        upstream_commit=AUDALIGN_CORRELATION_UPSTREAM_COMMIT,
        accuracy=None,
        num_processors=None,
        mapping_model="fixed_offset_equal_speed",
        ticks_per_second=120_000,
        verification_profile=profile,
    )
    mapped_evidence: dict[str, object] = {
        "code": "fixed_offset_verified",
        "provider": "audalign",
        "provider_version": AUDALIGN_VERSION,
        "recognizer": AUDALIGN_CORRELATION_RECOGNIZER,
        "probe_records": [
            {
                "percentage": percentage,
                "auxiliary_start_ticks": start,
                "auxiliary_end_ticks": start + 1_800_000,
                "native_offset_seconds": native,
                "derived_b_ticks": 0,
            }
            for percentage, start, native in (
                (20, 2_880_000, "24.0"),
                (50, 7_200_000, "60.0"),
                (80, 11_520_000, "96.0"),
            )
        ],
        "support_probe_count": 3,
        "cluster_spread_ticks": 0,
        "representative_b_ticks": 0,
        "conflicting_b_ticks": [],
        "verification_profile": profile.to_dict(),
    }
    empty_evidence: dict[str, object] = {
        "code": "no_candidate",
        "provider": "audalign",
        "provider_version": AUDALIGN_VERSION,
        "recognizer": AUDALIGN_CORRELATION_RECOGNIZER,
        "probe_records": [],
        "support_probe_count": 0,
        "cluster_spread_ticks": None,
        "representative_b_ticks": None,
        "conflicting_b_ticks": [],
        "verification_profile": profile.to_dict(),
    }
    intervals: list[AlignmentInterval] = []
    mapped_ticks = 0
    uncertain_ticks = 0
    for index, source_id in enumerate(main_source_ids):
        if source_id in mapped_source_ids:
            intervals.append(
                AlignmentInterval(
                    interval_id=f"aln_main_{index}",
                    auxiliary_camera_id="aux",
                    classification="mapped",
                    main={"source_id": source_id, "start_ticks": 0, "end_ticks": duration},
                    auxiliary={"source_id": "src_b", "start_ticks": 0, "end_ticks": duration},
                    evidence=dict(mapped_evidence),
                )
            )
            mapped_ticks += duration
        else:
            intervals.append(
                AlignmentInterval(
                    interval_id=f"aln_main_{index}",
                    auxiliary_camera_id="aux",
                    classification="uncertain",
                    main={"source_id": source_id, "start_ticks": 0, "end_ticks": duration},
                    auxiliary=None,
                    evidence=dict(empty_evidence),
                )
            )
            uncertain_ticks += duration
    basis = sorted(
        [
            AlignmentSourceBasis(
                camera_id="main",
                source_id=source_id,
                fingerprint=AlignmentSourceFingerprint(1, 1, "a" * 64),
                duration_ticks=duration,
            )
            for source_id in main_source_ids
        ]
        + [
            AlignmentSourceBasis(
                camera_id="aux",
                source_id="src_b",
                fingerprint=AlignmentSourceFingerprint(1, 1, "b" * 64),
                duration_ticks=duration,
            )
        ],
        key=lambda item: (item.camera_id, item.source_id),
    )
    if uncertain_ticks == 0:
        status = "complete"
    elif mapped_ticks > 0:
        status = "partial"
    else:
        status = "omitted"
    camera = AlignmentCamera(
        camera_id="aux",
        ordered_source_ids=("src_b",),
        status=status,  # type: ignore[arg-type]
        mapped_ticks=mapped_ticks,
        missing_ticks=0,
        uncertain_ticks=uncertain_ticks,
        conflict_ticks=0,
        errors=(),
    )
    return MulticamAlignmentArtifact(
        alignment_id=alignment_id,
        project_id=ProjectStore(root).load().project_id,
        producer_operation_id=operation_id,
        created_at="2026-09-02T00:00:00.000000Z",
        request_hash=request_hash,
        input_hash=input_hash,
        algorithm=algorithm,
        main_camera=AlignmentCameraGroup(
            camera_id="main",
            ordered_source_ids=main_source_ids,
        ),
        auxiliary_cameras=(camera,),
        source_basis=tuple(basis),
        intervals=tuple(intervals),
        summary=AlignmentSummary(
            total_main_ticks=duration * len(main_source_ids),
            camera_count=1,
            mapped_ticks=mapped_ticks,
            missing_ticks=0,
            uncertain_ticks=uncertain_ticks,
            conflict_ticks=0,
        ),
    )


def _prepare_unpaired_main_multicam_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    setup_includes_unpaired_main: bool,
    pairs_include_unpaired_main: bool = False,
    artifact_main_sources: tuple[str, ...] = ("src_a",),
) -> tuple[Path, str, int, str]:
    monkeypatch.setattr(
        m2_7_public_capability.platform,
        "system",
        lambda: "Darwin",
    )
    root = _workflow_project(tmp_path)
    video_path = tmp_path / "A 主机位.mp4"
    narration_path = tmp_path / "N 解说.wav"
    auxiliary_path = tmp_path / "B 副机位.mp4"
    foreign_path = tmp_path / "X 其它素材.mp4"
    video_path.write_bytes(b"source A")
    narration_path.write_bytes(b"source N")
    auxiliary_path.write_bytes(b"source B")
    foreign_path.write_bytes(b"source X")
    project = ProjectStore(root).load()
    video_probe = MediaProbe(
        duration_ticks=14_400_000,
        container_start_ticks=0,
        first_content_ticks=0,
        video_codec="h264",
        width=1920,
        height=1080,
        nominal_frame_rate={"numerator": 25, "denominator": 1},
        is_vfr=False,
        audio_codec="aac",
        audio_sample_rate=48_000,
        rotation_degrees=0,
    )
    source_a = replace(
        project.sources[0],
        kind="video",
        display_name="A 主机位",
        locator={"absolute_path": str(video_path)},
        fingerprint=fingerprint_file(video_path),
        probe=video_probe,
    )
    ProjectStore(root).save(
        replace(project, revision=project.revision + 1, sources=(source_a,)),
        expected_revision=project.revision,
    )

    workflow_fixtures._add_fixture_source(root, "src_n")
    current = ProjectStore(root).load()
    narration_source = replace(
        current.sources[-1],
        kind="audio",
        display_name="N 解说",
        locator={"absolute_path": str(narration_path)},
        fingerprint=fingerprint_file(narration_path),
        probe=MediaProbe(
            duration_ticks=14_400_000,
            container_start_ticks=0,
            first_content_ticks=0,
            video_codec=None,
            width=None,
            height=None,
            nominal_frame_rate=None,
            is_vfr=False,
            audio_codec="pcm_s16le",
            audio_sample_rate=48_000,
            rotation_degrees=0,
        ),
    )
    ProjectStore(root).save(
        replace(
            current,
            revision=current.revision + 1,
            sources=(*current.sources[:-1], narration_source),
        ),
        expected_revision=current.revision,
    )

    def add_project_video_source(project_root: Path, source_id: str) -> None:
        media_path = auxiliary_path if source_id == "src_b" else foreign_path
        display_name = "B 副机位" if source_id == "src_b" else "X 其它素材"
        latest = ProjectStore(project_root).load()
        auxiliary = SourceAsset(
            source_id=source_id,
            kind="video",
            display_name=display_name,
            import_mode=ImportMode.LINKED,
            locator={"absolute_path": str(media_path)},
            fingerprint=fingerprint_file(media_path),
            probe=video_probe,
        )
        ProjectStore(project_root).save(
            replace(
                latest,
                revision=latest.revision + 1,
                sources=(*latest.sources, auxiliary),
            ),
            expected_revision=latest.revision,
        )

    add_project_video_source(root, "src_x")
    monkeypatch.setattr(workflow_fixtures, "_add_fixture_source", add_project_video_source)
    source_pairs = [{"main_source_id": "src_a", "auxiliary_source_id": "src_b"}]
    if pairs_include_unpaired_main:
        source_pairs.append(
            {"main_source_id": "src_n", "auxiliary_source_id": "src_b"}
        )
    setup = {
        "schema_version": 1,
        "main_camera": {
            "camera_id": "main",
            "ordered_source_ids": (
                ["src_a", "src_n"] if setup_includes_unpaired_main else ["src_a"]
            ),
        },
        "auxiliary_cameras": [{"camera_id": "aux", "ordered_source_ids": ["src_b"]}],
        "source_pairs": source_pairs,
    }

    def succeed(*_args: object, **kwargs: object) -> AlignmentOutcome:
        operation_id = str(kwargs["operation_id"])
        alignment_id = str(kwargs["alignment_id"])
        current_project = ProjectStore(root).load()
        current_run = WorkflowStore(root).read_run("wfr_test")
        continuation = current_run.multicam_alignment_continuation
        assert continuation is not None and continuation.request_hash is not None
        artifact = _build_multicam_alignment_artifact(
            root,
            main_source_ids=artifact_main_sources,
            mapped_source_ids=artifact_main_sources,
            operation_id=operation_id,
            alignment_id=alignment_id,
            request_hash=continuation.request_hash,
            input_hash="e" * 64,
        )
        sources = {source.source_id: source for source in current_project.sources}
        basis = tuple(
            replace(
                item,
                fingerprint=replace(
                    item.fingerprint,
                    size=sources[item.source_id].fingerprint.size,
                    mtime_ns=sources[item.source_id].fingerprint.mtime_ns,
                    sha256_head_tail=sources[item.source_id].fingerprint.sha256_head_tail,
                ),
                duration_ticks=sources[item.source_id].probe.duration_ticks,
            )
            for item in artifact.source_basis
        )
        artifact = replace(artifact, source_basis=basis)
        AlignmentStore(root).publish(alignment_id, artifact)
        store = MediaOperationStore(root, current_project.project_id)
        result = AlignmentOperationResult(
            alignment_id=alignment_id,
            schema_version=1,
            content_hash=artifact.content_hash,
        )
        record = MediaOperationRecord(
            operation_id=operation_id,
            scope=store.scope,
            operation_type="align_multicam",
            request_hash=continuation.request_hash,
            input_hash="e" * 64,
            status="succeeded",
            phase_message_code="alignment_succeeded",
            created_at="2026-09-02T00:00:00.000000Z",
            started_at="2026-09-02T00:00:01.000000Z",
            updated_at="2026-09-02T00:00:02.000000Z",
            finished_at="2026-09-02T00:00:02.000000Z",
            result_ref=result,
            error=None,
            schema_version=2,
        )
        with store.writer(operation_id, create=True) as acquired:
            assert acquired
            store.write_locked(record)
        return AlignmentOutcome(record, artifact, False)

    monkeypatch.setattr(continuation_module, "run_align_multicam", succeed)
    submitted = workflow_fixtures._submit_two_binding_draft(
        root,
        multicam_setup=setup,
        content_source_ids=("src_a", "src_n"),
    )
    mutation = submitted.receipt.mutation
    assert mutation is not None
    approved = workflow_action(
        root,
        "wfr_test",
        "nle_unpaired_approve_draft",
        "approve_draft",
        {
            "schema_version": 1,
            "content_draft_ref": {
                "artifact_id": mutation.artifact_id,
                "schema_version": mutation.schema_version,
                "content_hash": mutation.content_hash,
            },
        },
    )
    proposal_ref = approved.workflow_run.artifact_refs["proposal"]
    assert proposal_ref is not None
    adopted = workflow_action(
        root,
        "wfr_test",
        "nle_unpaired_adopt",
        "adopt_roughcut",
        {"schema_version": 1, "proposal_ref": proposal_ref.to_dict()},
    )
    decision_ref = adopted.workflow_run.artifact_refs["decision"]
    continuation = adopted.workflow_run.multicam_alignment_continuation
    assert decision_ref is not None and continuation is not None
    assert continuation.alignment_id is not None
    return (
        root,
        decision_ref.artifact_id,
        ProjectStore(root).load().revision,
        continuation.alignment_id,
    )


def _assert_unpaired_main_gap_export(
    root: Path,
    edit_version_id: str,
    revision: int,
    alignment_id: str,
    destination: Path,
) -> None:
    alignment_before = AlignmentStore(root).read(alignment_id)
    assert alignment_before is not None
    prepared = nle_handoff.prepare_nle_export(
        root,
        run_id="wfr_test",
        action_id="nle_unpaired_main",
        edit_version_id=edit_version_id,
        expected_revision=revision,
        route="fcpxml",
        destination=destination,
        alignment_artifact_id=alignment_id,
    )
    assert [clip.source_id for clip in prepared.timeline.main_track.clips] == [
        "src_a",
        "src_n",
    ]
    aux = prepared.timeline.auxiliary_tracks[0]
    assert [
        (clip.source_id, clip.timeline_in_ticks, clip.timeline_out_ticks)
        for clip in aux.clips
    ] == [("src_b", 0, 120_000)]
    assert [(gap.timeline_in_ticks, gap.timeline_out_ticks) for gap in aux.gaps] == [
        (120_000, 240_000)
    ]

    outcome = approve_nle_export(
        root,
        run_id="wfr_test",
        action_id="nle_unpaired_main",
        edit_version_id=edit_version_id,
        expected_revision=revision,
        route="fcpxml",
        destination=destination,
        alignment_artifact_id=alignment_id,
    )
    assert outcome.receipt.alignment_artifact_id == alignment_id
    assert outcome.timeline_summary is not None
    assert outcome.timeline_summary["tracks"][1]["gap_count"] == 1

    alignment_after = AlignmentStore(root).read(alignment_id)
    assert alignment_after is not None
    assert alignment_after.to_dict() == alignment_before.to_dict()

    root_element = ET.fromstring(destination.read_bytes())
    spine = root_element.find("./library/event/project/sequence/spine")
    assert spine is not None
    main_clips = spine.findall("asset-clip")
    assert [item.attrib["ref"] for item in main_clips] == ["asset-src_a", "asset-src_n"]
    connected = [child for item in main_clips for child in item]
    assert len(connected) == 1
    assert connected[0].attrib["lane"] == "1"
    assert connected[0].attrib["ref"] == "asset-src_b"
    assert root_element.findall(".//gap") == []


def test_multicam_nle_export_accepts_unpaired_main_declared_in_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, edit_version_id, revision, alignment_id = _prepare_unpaired_main_multicam_export(
        tmp_path,
        monkeypatch,
        setup_includes_unpaired_main=True,
    )
    _assert_unpaired_main_gap_export(
        root,
        edit_version_id,
        revision,
        alignment_id,
        tmp_path / "unpaired-main-setup.fcpxml",
    )


def test_multicam_nle_export_rejects_alignment_missing_paired_main_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, edit_version_id, revision, alignment_id = _prepare_unpaired_main_multicam_export(
        tmp_path,
        monkeypatch,
        setup_includes_unpaired_main=True,
        pairs_include_unpaired_main=True,
    )
    destination = tmp_path / "dropped-paired-main.fcpxml"
    with pytest.raises(NleHandoffError) as error:
        approve_nle_export(
            root,
            run_id="wfr_test",
            action_id="nle_dropped_paired_main",
            edit_version_id=edit_version_id,
            expected_revision=revision,
            route="fcpxml",
            destination=destination,
            alignment_artifact_id=alignment_id,
        )
    assert error.value.code == "nle_export_alignment_not_deliverable"
    assert not destination.exists()


def test_multicam_nle_export_rejects_reordered_alignment_main_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, edit_version_id, revision, alignment_id = _prepare_unpaired_main_multicam_export(
        tmp_path,
        monkeypatch,
        setup_includes_unpaired_main=True,
        pairs_include_unpaired_main=True,
        artifact_main_sources=("src_n", "src_a"),
    )
    destination = tmp_path / "reordered-main.fcpxml"
    with pytest.raises(NleHandoffError) as error:
        approve_nle_export(
            root,
            run_id="wfr_test",
            action_id="nle_reordered_main",
            edit_version_id=edit_version_id,
            expected_revision=revision,
            route="fcpxml",
            destination=destination,
            alignment_artifact_id=alignment_id,
        )
    assert error.value.code == "nle_export_alignment_not_deliverable"
    assert not destination.exists()


def test_multicam_nle_export_rejects_foreign_alignment_main_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, edit_version_id, revision, alignment_id = _prepare_unpaired_main_multicam_export(
        tmp_path,
        monkeypatch,
        setup_includes_unpaired_main=True,
        pairs_include_unpaired_main=True,
        artifact_main_sources=("src_a", "src_x"),
    )
    destination = tmp_path / "foreign-main.fcpxml"
    with pytest.raises(NleHandoffError) as error:
        approve_nle_export(
            root,
            run_id="wfr_test",
            action_id="nle_foreign_main",
            edit_version_id=edit_version_id,
            expected_revision=revision,
            route="fcpxml",
            destination=destination,
            alignment_artifact_id=alignment_id,
        )
    assert error.value.code == "nle_export_alignment_not_deliverable"
    assert not destination.exists()


def test_multicam_nle_export_rejects_decision_source_outside_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, edit_version_id, revision, alignment_id = _prepare_unpaired_main_multicam_export(
        tmp_path,
        monkeypatch,
        setup_includes_unpaired_main=False,
    )
    destination = tmp_path / "outside-setup.fcpxml"
    with pytest.raises(NleHandoffError) as error:
        approve_nle_export(
            root,
            run_id="wfr_test",
            action_id="nle_outside_setup",
            edit_version_id=edit_version_id,
            expected_revision=revision,
            route="fcpxml",
            destination=destination,
            alignment_artifact_id=alignment_id,
        )
    assert error.value.code == "nle_export_alignment_not_deliverable"
    assert "outside the Alignment main camera" in str(error.value)
    assert not destination.exists()


def test_multicam_nle_export_rejects_wrong_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root, edit_version_id, _revision, _operation_id, alignment_id = _prepare_multicam_export(
        tmp_path, monkeypatch
    )
    other_root = _workflow_project(tmp_path / "other")
    with pytest.raises(NleHandoffError) as error:
        approve_nle_export(
            other_root,
            run_id="wfr_test",
            action_id="nle_wrong_project",
            edit_version_id=edit_version_id,
            expected_revision=1,
            route="fcpxml",
            destination=tmp_path / "wrong-project.fcpxml",
            alignment_artifact_id=alignment_id,
        )
    assert error.value.code == "nle_export_decision_not_adopted"


def test_multicam_nle_export_rejects_wrong_alignment_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, edit_version_id, revision, _operation_id, _alignment_id = _prepare_multicam_export(
        tmp_path, monkeypatch
    )
    with pytest.raises(NleHandoffError) as error:
        approve_nle_export(
            root,
            run_id="wfr_test",
            action_id="nle_wrong_alignment",
            edit_version_id=edit_version_id,
            expected_revision=revision,
            route="fcpxml",
            destination=tmp_path / "wrong-alignment.fcpxml",
            alignment_artifact_id="alignment_other",
        )
    assert error.value.code == "nle_export_alignment_not_deliverable"


def test_multicam_nle_export_rejects_non_succeeded_alignment_producer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, edit_version_id, revision, _operation_id, alignment_id = _prepare_multicam_export(
        tmp_path, monkeypatch, producer_succeeds=False
    )
    with pytest.raises(NleHandoffError) as error:
        approve_nle_export(
            root,
            run_id="wfr_test",
            action_id="nle_producer_failed",
            edit_version_id=edit_version_id,
            expected_revision=revision,
            route="fcpxml",
            destination=tmp_path / "producer-failed.fcpxml",
            alignment_artifact_id=alignment_id,
        )
    assert error.value.code == "nle_export_alignment_not_deliverable"


def test_multicam_nle_export_rejects_wrong_alignment_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, edit_version_id, revision, _operation_id, alignment_id = _prepare_multicam_export(
        tmp_path, monkeypatch
    )
    original_status = nle_handoff.multicam_alignment_status

    def wrong_hash(project_path: Path, run: object) -> dict[str, object]:
        status = original_status(project_path, run)  # type: ignore[arg-type]
        assert status is not None
        ref = dict(status["alignment_ref"])  # type: ignore[arg-type]
        ref["content_hash"] = "f" * 64
        return {**status, "alignment_ref": ref}

    monkeypatch.setattr(nle_handoff, "multicam_alignment_status", wrong_hash)
    with pytest.raises(NleHandoffError) as error:
        approve_nle_export(
            root,
            run_id="wfr_test",
            action_id="nle_wrong_alignment_hash",
            edit_version_id=edit_version_id,
            expected_revision=revision,
            route="fcpxml",
            destination=tmp_path / "wrong-hash.fcpxml",
            alignment_artifact_id=alignment_id,
        )
    assert error.value.code == "nle_export_alignment_not_deliverable"


def test_multicam_nle_export_rejects_source_basis_duration_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, edit_version_id, revision, _operation_id, alignment_id = _prepare_multicam_export(
        tmp_path, monkeypatch
    )
    project = ProjectStore(root).load()
    source = project.sources[0]
    ProjectStore(root).save(
        replace(
            project,
            revision=project.revision,
            sources=(
                replace(source, probe=replace(source.probe, duration_ticks=14_300_000)),
                *project.sources[1:],
            ),
        ),
        expected_revision=project.revision,
    )
    with pytest.raises(NleHandoffError) as error:
        approve_nle_export(
            root,
            run_id="wfr_test",
            action_id="nle_basis_stale",
            edit_version_id=edit_version_id,
            expected_revision=revision,
            route="fcpxml",
            destination=tmp_path / "basis-stale.fcpxml",
            alignment_artifact_id=alignment_id,
        )
    assert error.value.code == "nle_export_alignment_not_deliverable"


def test_multicam_nle_export_rejects_setup_snapshot_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, edit_version_id, revision, _operation_id, alignment_id = _prepare_multicam_export(
        tmp_path, monkeypatch
    )
    project = ProjectStore(root).load()
    source = project.sources[0]
    ProjectStore(root).save(
        replace(
            project,
            revision=project.revision,
            sources=(replace(source, display_name="A 改名"), *project.sources[1:]),
        ),
        expected_revision=project.revision,
    )
    with pytest.raises(NleHandoffError) as error:
        approve_nle_export(
            root,
            run_id="wfr_test",
            action_id="nle_setup_stale",
            edit_version_id=edit_version_id,
            expected_revision=revision,
            route="fcpxml",
            destination=tmp_path / "setup-stale.fcpxml",
            alignment_artifact_id=alignment_id,
        )
    assert error.value.code == "nle_export_source_stale"


def test_single_camera_approval_never_reads_alignment_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, edit_version_id, revision = _prepare_single_export(tmp_path)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("single-camera handoff must not read AlignmentStore")

    monkeypatch.setattr(AlignmentStore, "read", forbidden)
    outcome = approve_nle_export(
        root,
        run_id="wfr_test",
        action_id="nle_no_alignment",
        edit_version_id=edit_version_id,
        expected_revision=revision,
        route="fcpxml",
        destination=tmp_path / "single.fcpxml",
        alignment_artifact_id=None,
    )
    assert outcome.receipt.alignment_artifact_id is None


def test_approved_nle_export_is_atomic_and_idempotent(tmp_path: Path) -> None:
    root, edit_version_id, revision = _prepare_single_export(tmp_path)
    destination = tmp_path / "handoff output.fcpxml"

    first = approve_nle_export(
        root,
        run_id="wfr_test",
        action_id="nle_action_1",
        edit_version_id=edit_version_id,
        expected_revision=revision,
        route="fcpxml",
        destination=destination,
        alignment_artifact_id=None,
    )
    second = approve_nle_export(
        root,
        run_id="wfr_test",
        action_id="nle_action_1",
        edit_version_id=edit_version_id,
        expected_revision=revision,
        route="fcpxml",
        destination=destination,
        alignment_artifact_id=None,
    )

    assert first.readback is False
    assert second.readback is True
    assert destination.is_file()
    assert first.receipt.output_sha256 == second.receipt.output_sha256
    assert first.receipt.alignment_artifact_id is None
    assert (root / "exports/handoffs/receipts/nle_action_1.json").is_file()


def test_nle_action_id_cannot_change_route_or_destination(tmp_path: Path) -> None:
    root, edit_version_id, revision = _prepare_single_export(tmp_path)
    destination = tmp_path / "handoff.fcpxml"
    approve_nle_export(
        root,
        run_id="wfr_test",
        action_id="nle_action_1",
        edit_version_id=edit_version_id,
        expected_revision=revision,
        route="fcpxml",
        destination=destination,
        alignment_artifact_id=None,
    )
    with pytest.raises(NleHandoffError) as route_error:
        approve_nle_export(
            root,
            run_id="wfr_test",
            action_id="nle_action_1",
            edit_version_id=edit_version_id,
            expected_revision=revision,
            route="fcp7_xml",
            destination=destination,
            alignment_artifact_id=None,
        )
    assert route_error.value.code == "nle_export_action_conflict"
    with pytest.raises(NleHandoffError) as alignment_error:
        approve_nle_export(
            root,
            run_id="wfr_test",
            action_id="nle_action_1",
            edit_version_id=edit_version_id,
            expected_revision=revision,
            route="fcpxml",
            destination=destination,
            alignment_artifact_id="alignment_changed",
        )
    assert alignment_error.value.code == "nle_export_action_conflict"
    with pytest.raises(NleHandoffError) as destination_error:
        approve_nle_export(
            root,
            run_id="wfr_test",
            action_id="nle_action_1",
            edit_version_id=edit_version_id,
            expected_revision=revision,
            route="fcpxml",
            destination=tmp_path / "other.fcpxml",
            alignment_artifact_id=None,
        )
    assert destination_error.value.code == "nle_export_action_conflict"


def test_stale_source_and_existing_destination_fail_closed(tmp_path: Path) -> None:
    root, edit_version_id, revision = _prepare_single_export(tmp_path)
    destination = tmp_path / "handoff.fcpxml"
    destination.write_bytes(b"pre-existing")
    with pytest.raises(NleHandoffError) as conflict:
        approve_nle_export(
            root,
            run_id="wfr_test",
            action_id="nle_action_2",
            edit_version_id=edit_version_id,
            expected_revision=revision,
            route="fcpxml",
            destination=destination,
            alignment_artifact_id=None,
        )
    assert conflict.value.code == "nle_export_destination_conflict"

    destination.unlink()
    source = next(path for path in tmp_path.iterdir() if path.name == "export source.mp4")
    source.write_bytes(b"changed source bytes")
    with pytest.raises(NleHandoffError) as stale:
        approve_nle_export(
            root,
            run_id="wfr_test",
            action_id="nle_action_3",
            edit_version_id=edit_version_id,
            expected_revision=revision,
            route="fcpxml",
            destination=destination,
            alignment_artifact_id=None,
        )
    assert stale.value.code == "nle_export_source_stale"
    assert not (root / "exports/handoffs/receipts/nle_action_3.json").exists()


def test_nle_export_rejects_stale_revision_and_active_decision(tmp_path: Path) -> None:
    root, edit_version_id, revision = _prepare_single_export(tmp_path)
    with pytest.raises(NleHandoffError) as revision_error:
        approve_nle_export(
            root,
            run_id="wfr_test",
            action_id="nle_old_revision",
            edit_version_id=edit_version_id,
            expected_revision=revision - 1,
            route="fcpxml",
            destination=tmp_path / "old-revision.fcpxml",
            alignment_artifact_id=None,
        )
    assert revision_error.value.code == "nle_export_decision_stale"
    with pytest.raises(NleHandoffError) as decision_error:
        approve_nle_export(
            root,
            run_id="wfr_test",
            action_id="nle_old_decision",
            edit_version_id="edit_not_current",
            expected_revision=revision,
            route="fcpxml",
            destination=tmp_path / "old-decision.fcpxml",
            alignment_artifact_id=None,
        )
    assert decision_error.value.code == "nle_export_decision_not_adopted"


def test_nle_export_rejects_missing_source(tmp_path: Path) -> None:
    root, edit_version_id, revision = _prepare_single_export(tmp_path)
    project = ProjectStore(root).load()
    ProjectStore(root).save(
        replace(project, sources=(), revision=project.revision),
        expected_revision=project.revision,
    )
    with pytest.raises(NleHandoffError) as error:
        approve_nle_export(
            root,
            run_id="wfr_test",
            action_id="nle_missing_source",
            edit_version_id=edit_version_id,
            expected_revision=revision,
            route="fcpxml",
            destination=tmp_path / "missing-source.fcpxml",
            alignment_artifact_id=None,
        )
    assert error.value.code == "nle_export_source_stale"


def test_nle_route_requires_matching_destination_suffix(tmp_path: Path) -> None:
    root, edit_version_id, revision = _prepare_single_export(tmp_path)
    with pytest.raises(NleHandoffError) as error:
        approve_nle_export(
            root,
            run_id="wfr_test",
            action_id="nle_bad_suffix",
            edit_version_id=edit_version_id,
            expected_revision=revision,
            route="fcpxml",
            destination=tmp_path / "handoff.xml",
            alignment_artifact_id=None,
        )
    assert error.value.code == "nle_export_invalid_destination"
    assert not (root / "exports/handoffs/receipts/nle_bad_suffix.json").exists()


def test_nle_export_rejects_wrong_adoption_run(tmp_path: Path) -> None:
    root, edit_version_id, revision = _prepare_single_export(tmp_path)
    with pytest.raises(NleHandoffError) as error:
        approve_nle_export(
            root,
            run_id="another_run",
            action_id="nle_wrong_run",
            edit_version_id=edit_version_id,
            expected_revision=revision,
            route="fcpxml",
            destination=tmp_path / "wrong-run.fcpxml",
            alignment_artifact_id=None,
        )
    assert error.value.code == "nle_export_decision_not_adopted"


def test_output_move_then_validation_failure_rolls_back_owned_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, edit_version_id, revision = _prepare_single_export(tmp_path)
    destination = tmp_path / "post-move-validation.fcpxml"
    original_validate = nle_handoff._validate_published_file
    calls = 0

    def fail_after_publish(path: Path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise NleHandoffError(
                "nle_export_integrity_error", "fixture post-publish validation failure"
            )
        return original_validate(path)

    monkeypatch.setattr(nle_handoff, "_validate_published_file", fail_after_publish)
    with pytest.raises(NleHandoffError, match="fixture post-publish validation failure"):
        approve_nle_export(
            root,
            run_id="wfr_test",
            action_id="nle_post_move_failed",
            edit_version_id=edit_version_id,
            expected_revision=revision,
            route="fcpxml",
            destination=destination,
            alignment_artifact_id=None,
        )
    assert not destination.exists()
    assert not (root / "exports/handoffs/receipts/nle_post_move_failed.json").exists()


def test_output_publish_failure_cleans_staged_temp_without_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, edit_version_id, revision = _prepare_single_export(tmp_path)
    destination = tmp_path / "publish-failed.fcpxml"

    def fail_publish(*_args: object, **_kwargs: object) -> None:
        raise NleHandoffError("nle_export_write_failed", "fixture output publish failure")

    monkeypatch.setattr(nle_handoff, "publish_workflow_candidate", fail_publish)
    with pytest.raises(NleHandoffError, match="fixture output publish failure"):
        approve_nle_export(
            root,
            run_id="wfr_test",
            action_id="nle_publish_failed",
            edit_version_id=edit_version_id,
            expected_revision=revision,
            route="fcpxml",
            destination=destination,
            alignment_artifact_id=None,
        )
    assert not destination.exists()
    assert not list(tmp_path.glob(".*publish-failed.fcpxml.*.tmp"))
    assert not (root / "exports/handoffs/receipts/nle_publish_failed.json").exists()


def test_changed_destination_is_preserved_as_recovery_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from roughcut.adapters.nle_export_store import NleExportStore

    root, edit_version_id, revision = _prepare_single_export(tmp_path)
    destination = tmp_path / "changed-during-cleanup.fcpxml"

    def replace_then_fail(self: NleExportStore, receipt: NleExportReceipt) -> NleExportReceipt:
        del self
        Path(receipt.destination).write_bytes(b"concurrent replacement")
        raise NleHandoffError(
            "nle_export_write_failed", "fixture receipt failure after replacement"
        )

    monkeypatch.setattr(NleExportStore, "write", replace_then_fail)
    with pytest.raises(NleHandoffError) as error:
        approve_nle_export(
            root,
            run_id="wfr_test",
            action_id="nle_replaced",
            edit_version_id=edit_version_id,
            expected_revision=revision,
            route="fcpxml",
            destination=destination,
            alignment_artifact_id=None,
        )
    assert error.value.code == "nle_export_recovery_conflict"
    assert destination.read_bytes() == b"concurrent replacement"
    assert not (root / "exports/handoffs/receipts/nle_replaced.json").exists()


def test_output_cleanup_failure_is_visible_without_success_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from roughcut.adapters.nle_export_store import NleExportStore

    root, edit_version_id, revision = _prepare_single_export(tmp_path)
    destination = tmp_path / "cleanup-failed.fcpxml"
    original_write = NleExportStore.write

    def write_then_fail(self: NleExportStore, receipt: NleExportReceipt) -> NleExportReceipt:
        original_write(self, receipt)
        raise NleHandoffError("nle_export_write_failed", "fixture receipt failure")

    monkeypatch.setattr(NleExportStore, "write", write_then_fail)
    monkeypatch.setattr(
        nle_handoff,
        "_sync_directory",
        lambda _path: (_ for _ in ()).throw(OSError("fixture cleanup sync failure")),
    )
    with pytest.raises(NleHandoffError) as error:
        approve_nle_export(
            root,
            run_id="wfr_test",
            action_id="nle_cleanup_failed",
            edit_version_id=edit_version_id,
            expected_revision=revision,
            route="fcpxml",
            destination=destination,
            alignment_artifact_id=None,
        )
    assert error.value.code == "nle_export_recovery_conflict"
    assert not (root / "exports/handoffs/receipts/nle_cleanup_failed.json").exists()


def test_receipt_failure_after_publish_rolls_back_exact_receipt_and_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from roughcut.adapters.nle_export_store import NleExportStore

    root, edit_version_id, revision = _prepare_single_export(tmp_path)
    destination = tmp_path / "receipt-failed.fcpxml"
    original_write = NleExportStore.write

    def write_then_fail(self: NleExportStore, receipt: NleExportReceipt) -> NleExportReceipt:
        original_write(self, receipt)
        raise NleHandoffError("nle_export_write_failed", "fixture receipt failure")

    monkeypatch.setattr(NleExportStore, "write", write_then_fail)
    with pytest.raises(NleHandoffError, match="fixture receipt failure"):
        approve_nle_export(
            root,
            run_id="wfr_test",
            action_id="nle_receipt_failed",
            edit_version_id=edit_version_id,
            expected_revision=revision,
            route="fcpxml",
            destination=destination,
            alignment_artifact_id=None,
        )
    assert not destination.exists()
    assert not (root / "exports/handoffs/receipts/nle_receipt_failed.json").exists()


def test_writer_failure_does_not_leave_receipt_or_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, edit_version_id, revision = _prepare_single_export(tmp_path)
    destination = tmp_path / "failed.fcpxml"

    def fail(_prepared: object) -> bytes:
        raise NleHandoffError("nle_export_write_failed", "fixture writer failure")

    monkeypatch.setattr(nle_handoff, "write_nle_export", fail)
    with pytest.raises(NleHandoffError, match="fixture writer failure"):
        approve_nle_export(
            root,
            run_id="wfr_test",
            action_id="nle_action_failed",
            edit_version_id=edit_version_id,
            expected_revision=revision,
            route="fcpxml",
            destination=destination,
            alignment_artifact_id=None,
        )
    assert not destination.exists()
    assert not (root / "exports/handoffs/receipts/nle_action_failed.json").exists()
