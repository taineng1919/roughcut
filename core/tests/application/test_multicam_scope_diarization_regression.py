from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from roughcut.adapters.artifact_store import write_new_json
from roughcut.adapters.project_store import ProjectStore
from roughcut.application.projects import create_project
from roughcut.application.workflows import workflow_action, workflow_start, workflow_status
from roughcut.domain.errors import WorkflowError
from roughcut.domain.project import ImportMode, MediaProbe, SourceAsset, SourceFingerprint
from roughcut.domain.transcript import TimedTranscript, TranscriptProvenance, TranscriptSegment


def _probe() -> MediaProbe:
    return MediaProbe(
        duration_ticks=360_000,
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


def _make_source(source_id: str, display_name: str) -> SourceAsset:
    return SourceAsset(
        source_id=source_id,
        kind="video",
        display_name=display_name,
        import_mode=ImportMode.LINKED,
        locator={"absolute_path": f"/fixture/{source_id}.mp4"},
        fingerprint=SourceFingerprint(1000, 1, f"sha-{source_id}"),
        probe=_probe(),
        tags=[],
        note="",
    )


def _project_with_eight_sources(tmp_path: Path) -> tuple[Path, list[str], list[str]]:
    root = tmp_path / "multicam-8-project"
    project = create_project(root, "Multicam 8")
    main_ids = [f"src_main_{i}" for i in range(1, 5)]
    aux_ids = [f"src_aux_{i}" for i in range(1, 5)]
    main_sources = [_make_source(sid, f"主收声{i}.mp4") for i, sid in enumerate(main_ids, start=1)]
    aux_sources = [_make_source(sid, f"副机位{i}.mp4") for i, sid in enumerate(aux_ids, start=1)]
    all_sources = tuple(main_sources + aux_sources)
    # Create transcripts for main sources so they can be active if needed (optional)
    # But for scope approval we don't need active transcripts; we will authorize transcribe.
    ProjectStore(root).save(
        replace(project, sources=all_sources, revision=1),
        expected_revision=0,
    )
    return root, main_ids, aux_ids


def _write_main_transcripts(root: Path, main_ids: list[str]) -> None:
    project = ProjectStore(root).load()
    active_versions = dict(project.active_transcript_versions)
    for source_id in main_ids:
        transcript_id = f"tr_{source_id}"
        provenance = TranscriptProvenance(
            "fixture", "1", {}, {}, f"raw-asr/{source_id}/fixture.json", "a", "b", 0
        )
        segment = TranscriptSegment(
            "seg_000001", 0, 120_000, "测试", None, None, None, None, (), "unmarked"
        )
        transcript = TimedTranscript(
            1, transcript_id, source_id, None, provenance, "zh-CN", (segment,)
        )
        write_new_json(root / "transcripts" / source_id / f"{transcript_id}.json", transcript.to_dict())
        active_versions[source_id] = transcript_id
    ProjectStore(root).save(
        replace(project, active_transcript_versions=active_versions, revision=project.revision + 1),
        expected_revision=project.revision,
    )


def test_approve_scope_with_diarization_does_not_expand_to_auxiliary_sources(
    tmp_path: Path,
) -> None:
    """4 main + 4 aux, speaker_diarization=true, source_authorizations严格只有4主收声。"""

    root, main_ids, aux_ids = _project_with_eight_sources(tmp_path)

    # Start workflow with only main sources ordered (the content main line)
    start = workflow_start(root, "wfr_4plus4", ordered_source_ids=main_ids)
    assert [b.source_id for b in start.workflow_run.ordered_bindings] == main_ids

    status = workflow_status(root, "wfr_4plus4")
    scope_basis = status["confirmation_bases"]["scope"]["basis"]
    assert scope_basis is not None
    basis_id = scope_basis["basis_id"]

    # Attempt to approve scope with only 4 main, each transcribe + diarization true
    authorizations = [
        {"source_id": sid, "transcribe": True, "speaker_diarization": True} for sid in main_ids
    ]

    result = workflow_action(
        root,
        "wfr_4plus4",
        "act_scope_4plus4",
        "approve_scope",
        {
            "schema_version": 1,
            "confirmation_basis": {"basis_id": basis_id},
            "source_authorizations": authorizations,
        },
    )

    # Core must keep exactly 4 authorizations, all main, no aux
    run = result.workflow_run
    assert len(run.scope_authorizations) == 4
    assert [a.source_id for a in run.scope_authorizations] == main_ids
    assert all(a.transcribe and a.speaker_diarization for a in run.scope_authorizations)
    assert len(run.ordered_bindings) == 4
    assert [b.source_id for b in run.ordered_bindings] == main_ids
    # Ensure aux not in any binding or authorization
    for aux in aux_ids:
        assert aux not in [a.source_id for a in run.scope_authorizations]
        assert aux not in [b.source_id for b in run.ordered_bindings]

    # Verify status reports scope current and aux not required
    latest_status = workflow_status(root, "wfr_4plus4")
    assert latest_status["readiness"]["scope_approved"] is True
    # Project still contains all 8 sources, but workflow scope is only 4
    project = ProjectStore(root).load()
    assert len(project.sources) == 8
    assert {s.source_id for s in project.sources} == set(main_ids + aux_ids)


def test_secondary_camera_cannot_be_transcribed_even_with_diarization_enabled(
    tmp_path: Path,
) -> None:
    """副机位不得进入 source_authorizations，
    也不得调用 transcribe_source，即使 diarization=true。
    """

    from roughcut.application.protected_writes import protected_write

    root, main_ids, aux_ids = _project_with_eight_sources(tmp_path)

    workflow_start(root, "wfr_aux_block", ordered_source_ids=main_ids)
    status = workflow_status(root, "wfr_aux_block")
    basis_id = status["confirmation_bases"]["scope"]["basis"]["basis_id"]
    workflow_action(
        root,
        "wfr_aux_block",
        "act_scope_aux",
        "approve_scope",
        {
            "schema_version": 1,
            "confirmation_basis": {"basis_id": basis_id},
            "source_authorizations": [
                {"source_id": sid, "transcribe": True, "speaker_diarization": True}
                for sid in main_ids
            ],
        },
    )

    # Auxiliary source is not in ordered scope, so protected_write must reject
    for aux in aux_ids:
        with (
            pytest.raises(WorkflowError) as exc,
            protected_write(
                root, "transcribe_source", source_id=aux, speaker_diarization=True
            ),
        ):
            pass
        message = str(exc.value).lower()
        assert (
            "scope" in message
            or "outside" in message
            or "source" in message
            or "workflow" in message
        )
        # Also with diarization False must still reject
        with (
            pytest.raises(WorkflowError),
            protected_write(
                root, "transcribe_source", source_id=aux, speaker_diarization=False
            ),
        ):
            pass

    # Main source *can* be transcribed when correctly authorized (with diarization)
    # protected_write should succeed (not raise)
    for main in main_ids:
        with protected_write(root, "transcribe_source", source_id=main, speaker_diarization=True):
            pass
        # Without diarization also allowed because transcribe true,
        # but diarization param false should also pass
        with protected_write(root, "transcribe_source", source_id=main, speaker_diarization=False):
            pass

    # If we try to use diarization on a source where authorization
    # is transcribe true but diarization false, it should fail
    # Create a new workflow where main authorizations have diarization false
    root2, main_ids2, _ = _project_with_eight_sources(tmp_path / "second")
    workflow_start(root2, "wfr_no_diar", ordered_source_ids=main_ids2)
    status2 = workflow_status(root2, "wfr_no_diar")
    basis2 = status2["confirmation_bases"]["scope"]["basis"]["basis_id"]
    workflow_action(
        root2,
        "wfr_no_diar",
        "act_scope_no_diar",
        "approve_scope",
        {
            "schema_version": 1,
            "confirmation_basis": {"basis_id": basis2},
            "source_authorizations": [
                {"source_id": sid, "transcribe": True, "speaker_diarization": False}
                for sid in main_ids2
            ],
        },
    )
    with (
        pytest.raises(WorkflowError) as exc2,
        protected_write(
            root2, "transcribe_source", source_id=main_ids2[0], speaker_diarization=True
        ),
    ):
        pass
    assert (
        "diarization" in str(exc2.value).lower()
        or "workflow" in str(exc2.value).lower()
    )


def test_scope_authorizations_remain_subset_even_when_project_has_many_auxiliary_files(
    tmp_path: Path,
) -> None:
    """Core现有 approve_scope/source_authorizations 子集语义保持不变，diarization不推断扩大。"""

    root, main_ids, aux_ids = _project_with_eight_sources(tmp_path)

    # Workflow start with only main (subset of Project sources)
    workflow_start(root, "wfr_subset", ordered_source_ids=main_ids)
    status = workflow_status(root, "wfr_subset")
    # Project has 8, but workflow has 4, proving subset
    project = ProjectStore(root).load()
    assert len(project.sources) == 8
    assert len(status["workflow_run"]["ordered_bindings"]) == 4

    # Approve with diarization true for each main
    basis_id = status["confirmation_bases"]["scope"]["basis"]["basis_id"]
    result = workflow_action(
        root,
        "wfr_subset",
        "act_subset",
        "approve_scope",
        {
            "schema_version": 1,
            "confirmation_basis": {"basis_id": basis_id},
            "source_authorizations": [
                {"source_id": sid, "transcribe": True, "speaker_diarization": True}
                for sid in main_ids
            ],
        },
    )
    # Even though project has 8, authorizations are exactly the 4 main, proving no auto-inference
    assert len(result.workflow_run.scope_authorizations) == 4
    # Ensure aux tags are not automatically added
    auth_ids = {a.source_id for a in result.workflow_run.scope_authorizations}
    assert auth_ids.isdisjoint(set(aux_ids))


def test_content_workflow_four_main_ready(tmp_path: Path) -> None:
    root, main_ids, aux_ids = _project_with_eight_sources(tmp_path)
    _write_main_transcripts(root, main_ids)
    start = workflow_start(root, "wfr_content_4", ordered_source_ids=main_ids)
    assert [b.source_id for b in start.workflow_run.ordered_bindings] == main_ids
    project = ProjectStore(root).load()
    assert len(project.sources) == 8
    assert set(project.active_transcript_versions.keys()) == set(main_ids)
    for aux_id in aux_ids:
        assert aux_id not in project.active_transcript_versions
    basis_id = workflow_status(root, "wfr_content_4")["confirmation_bases"]["scope"]["basis"]["basis_id"]  # type: ignore[index]
    result = workflow_action(
        root,
        "wfr_content_4",
        "act_content_4",
        "approve_scope",
        {
            "schema_version": 1,
            "confirmation_basis": {"basis_id": basis_id},
            "source_authorizations": [
                {"source_id": source_id, "transcribe": True, "speaker_diarization": True}
                for source_id in main_ids
            ],
        },
    )
    assert [a.source_id for a in result.workflow_run.scope_authorizations] == main_ids
    assert {a.source_id for a in result.workflow_run.scope_authorizations} == set(main_ids)
    assert workflow_status(root, "wfr_content_4")["readiness"]["required_transcripts_ready"] is True
