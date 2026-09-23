from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from roughcut.adapters.artifact_store import write_new_json
from roughcut.adapters.project_store import ProjectStore
from roughcut.application.agent_context import (
    create_edit_brief,
    read_agent_context,
    read_multi_source_agent_context,
)
from roughcut.application.edits import (
    change_edit,
    propose_review_edit_change,
    read_edit_history,
    redo_edit,
    undo_edit,
)
from roughcut.application.health import TOOL_SCHEMA_VERSION
from roughcut.application.preview import load_review_snapshot
from roughcut.application.projects import create_project
from roughcut.application.proposals import (
    confirm_edit_proposal,
    confirm_multi_source_edit_proposal,
    create_edit_proposal,
    create_multi_source_edit_proposal,
)
from roughcut.domain.project import (
    ImportMode,
    MediaProbe,
    Project,
    ProjectError,
    SourceAsset,
    SourceFingerprint,
)
from roughcut.domain.transcript import (
    TimedTranscript,
    TranscriptProvenance,
    TranscriptSegment,
)


def _source(source_id: str) -> SourceAsset:
    return SourceAsset(
        source_id=source_id,
        kind="audio",
        display_name=f"{source_id}.wav",
        import_mode=ImportMode.LINKED,
        locator={"absolute_path": f"/fixture/{source_id}.wav"},
        fingerprint=SourceFingerprint(100, 1, f"fixture-{source_id}"),
        probe=MediaProbe(
            duration_ticks=600_000,
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


def _transcript(source_id: str, transcript_id: str) -> TimedTranscript:
    return TimedTranscript(
        schema_version=1,
        transcript_version_id=transcript_id,
        source_id=source_id,
        parent_version_id=None,
        provenance=TranscriptProvenance(
            backend="fixture",
            package_version="1",
            models={},
            parameters={},
            raw_result_path=f"raw-asr/{source_id}/fixture.json",
            started_at="fixture",
            completed_at="fixture",
            exit_status=0,
        ),
        language="zh-CN",
        segments=tuple(
            TranscriptSegment(
                segment_id=f"seg_{source_id}_{index}",
                start_ticks=(index - 1) * 120_000,
                end_ticks=index * 120_000,
                original_text=f"{source_id} 第 {index} 段。",
                corrected_text=None,
                local_speaker_id="spk_0",
                person_id=None,
                confidence=None,
                fine_units=(),
                editorial_mark="unmarked",
            )
            for index in range(1, 4)
        ),
    )


def _clip(source_id: str, transcript_id: str, index: int, clip_id: str) -> dict[str, object]:
    return {
        "clip_id": clip_id,
        "source_id": source_id,
        "transcript_version_id": transcript_id,
        "segment_id": f"seg_{source_id}_{index}",
        "source_in_ticks": (index - 1) * 120_000,
        "source_out_ticks": index * 120_000,
        "reason": f"保留 {source_id} {index}",
        "display_text": f"{source_id} 第 {index} 段。",
    }


def _single_project(tmp_path: Path, *, allow_reorder: bool = True) -> dict[str, object]:
    root = tmp_path / "single edit history"
    project = create_project(root, "Single history")
    source_id = "src_a"
    transcript_id = "tr_a"
    transcript = _transcript(source_id, transcript_id)
    write_new_json(root / "transcripts" / source_id / f"{transcript_id}.json", transcript.to_dict())
    imported = replace(
        project,
        revision=1,
        sources=(_source(source_id),),
        active_transcript_versions={source_id: transcript_id},
    )
    ProjectStore(root).save(imported, expected_revision=0)
    brief = create_edit_brief(
        root,
        theme="history",
        target_duration_ticks=360_000,
        focus=["all"],
        allow_reorder=allow_reorder,
        expected_revision=1,
    )
    context = read_agent_context(
        root,
        source_id=source_id,
        transcript_version_id=transcript_id,
        brief_id=brief.brief.brief_id,
        expected_revision=2,
        offset=0,
        limit=3,
    )
    clips = [
        _clip(source_id, transcript_id, 1, "clip_a"),
        _clip(source_id, transcript_id, 2, "clip_b"),
        _clip(source_id, transcript_id, 3, "clip_c"),
    ]
    proposal = create_edit_proposal(
        root,
        source_id=source_id,
        transcript_version_id=transcript_id,
        brief_id=brief.brief.brief_id,
        context_hash=context.context_hash,
        clips=clips,
        total_duration_ticks=360_000,
        expected_revision=2,
    )
    decision = confirm_edit_proposal(root, proposal.proposal.proposal_id, expected_revision=2)
    return {
        "project_path": root,
        "root_edit_id": decision.decision.edit_version_id,
        "brief_id": brief.brief.brief_id,
        "source_id": source_id,
        "transcript_id": transcript_id,
    }


def _multi_project(tmp_path: Path) -> dict[str, object]:
    root = tmp_path / "multi edit history"
    project = create_project(root, "Multi history")
    bindings = [("src_a", "tr_a"), ("src_b", "tr_b")]
    for source_id, transcript_id in bindings:
        transcript = _transcript(source_id, transcript_id)
        write_new_json(
            root / "transcripts" / source_id / f"{transcript_id}.json",
            transcript.to_dict(),
        )
    imported = replace(
        project,
        revision=1,
        sources=tuple(_source(source_id) for source_id, _ in bindings),
        active_transcript_versions=dict(bindings),
    )
    ProjectStore(root).save(imported, expected_revision=0)
    brief = create_edit_brief(
        root,
        theme="A B A",
        target_duration_ticks=360_000,
        focus=["all"],
        allow_reorder=True,
        expected_revision=1,
    )
    source_bindings = [
        {"source_id": source_id, "transcript_version_id": transcript_id}
        for source_id, transcript_id in bindings
    ]
    context = read_multi_source_agent_context(
        root,
        source_bindings=source_bindings,
        brief_id=brief.brief.brief_id,
        expected_revision=2,
        offset=0,
        limit=6,
    )
    clips = [
        _clip("src_a", "tr_a", 1, "clip_a_open"),
        _clip("src_b", "tr_b", 1, "clip_b"),
        _clip("src_a", "tr_a", 2, "clip_a_return"),
    ]
    proposal = create_multi_source_edit_proposal(
        root,
        source_bindings=source_bindings,
        brief_id=brief.brief.brief_id,
        context_hash=context.context_hash,
        clips=clips,
        total_duration_ticks=360_000,
        expected_revision=2,
    )
    decision = confirm_multi_source_edit_proposal(
        root, proposal.proposal.proposal_id, expected_revision=2
    )
    return {
        "project_path": root,
        "root_edit_id": decision.decision.edit_version_id,
        "brief_id": brief.brief.brief_id,
        "source_bindings": source_bindings,
    }


def _change(
    state: dict[str, object], operation: dict[str, object], *, revision: int, base: str
):
    return change_edit(
        Path(state["project_path"]),
        operation=operation,
        expected_revision=revision,
        base_edit_version_id=base,
    )


def test_old_project_defaults_and_serializes_empty_redo_stack(tmp_path: Path) -> None:
    project = create_project(tmp_path / "legacy", "Legacy")
    data = project.to_dict()
    data.pop("edit_redo_stack", None)

    loaded = Project.from_dict(data)

    assert loaded.edit_redo_stack == ()
    assert loaded.to_dict()["edit_redo_stack"] == []


def test_review_changes_create_unconfirmed_proposals_without_project_revision(
    tmp_path: Path,
) -> None:
    state = _single_project(tmp_path)
    project_path = Path(state["project_path"])
    root_id = str(state["root_edit_id"])
    root_snapshot = load_review_snapshot(project_path, edit_version_id=root_id)
    before = ProjectStore(project_path).load()
    before_edits = set((project_path / "edits").glob("*.json"))

    deleted = propose_review_edit_change(
        project_path,
        proposal=root_snapshot.proposal,
        operation={"type": "delete", "clip_id": "clip_b"},
        expected_revision=before.revision,
        restoration_clips=root_snapshot.proposal.clips,
    )
    assert deleted.changed is True
    assert deleted.project_revision == before.revision
    assert deleted.proposal.base_edit_version_id == root_id
    assert [clip.clip_id for clip in deleted.proposal.clips] == ["clip_a", "clip_c"]
    assert ProjectStore(project_path).load() == before
    assert set((project_path / "edits").glob("*.json")) == before_edits

    restored = propose_review_edit_change(
        project_path,
        proposal=deleted.proposal,
        operation={
            "type": "restore",
            "clip_id": "clip_b",
            "insert_before_clip_id": "clip_c",
        },
        expected_revision=before.revision,
        restoration_clips=root_snapshot.proposal.clips,
    )
    assert [clip.clip_id for clip in restored.proposal.clips] == [
        "clip_a",
        "clip_b",
        "clip_c",
    ]

    trimmed = propose_review_edit_change(
        project_path,
        proposal=restored.proposal,
        operation={
            "type": "trim",
            "clip_id": "clip_a",
            "source_in_ticks": 12_000,
            "source_out_ticks": 108_000,
        },
        expected_revision=before.revision,
        restoration_clips=root_snapshot.proposal.clips,
    )
    assert trimmed.proposal.clips[0].display_text == "src_a 第 1 段。"
    assert trimmed.proposal.clips[0].source_in_ticks == 12_000
    assert trimmed.proposal.clips[0].source_out_ticks == 108_000
    assert ProjectStore(project_path).load() == before
    assert len(list((project_path / "proposals").glob("*.json"))) == 4


def test_review_change_rejects_stale_illegal_and_failed_writes_without_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _single_project(tmp_path)
    project_path = Path(state["project_path"])
    snapshot = load_review_snapshot(
        project_path,
        edit_version_id=str(state["root_edit_id"]),
    )
    before = ProjectStore(project_path).load()
    before_proposals = set((project_path / "proposals").glob("*.json"))

    with pytest.raises(ProjectError, match="revision"):
        propose_review_edit_change(
            project_path,
            proposal=snapshot.proposal,
            operation={"type": "delete", "clip_id": "clip_b"},
            expected_revision=before.revision - 1,
            restoration_clips=snapshot.proposal.clips,
        )
    with pytest.raises(ProjectError, match="outside|ordered|empty"):
        propose_review_edit_change(
            project_path,
            proposal=snapshot.proposal,
            operation={
                "type": "trim",
                "clip_id": "clip_a",
                "source_in_ticks": 120_000,
                "source_out_ticks": 120_000,
            },
            expected_revision=before.revision,
            restoration_clips=snapshot.proposal.clips,
        )

    def fail_write(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected proposal write failure")

    monkeypatch.setattr("roughcut.application.edits.write_new_json", fail_write)
    with pytest.raises(OSError, match="injected"):
        propose_review_edit_change(
            project_path,
            proposal=snapshot.proposal,
            operation={"type": "delete", "clip_id": "clip_b"},
            expected_revision=before.revision,
            restoration_clips=snapshot.proposal.clips,
        )
    assert ProjectStore(project_path).load() == before
    assert set((project_path / "proposals").glob("*.json")) == before_proposals


def test_schema_one_delete_restore_trim_reorder_and_noops(tmp_path: Path) -> None:
    state = _single_project(tmp_path)
    root_id = str(state["root_edit_id"])
    deleted = _change(
        state,
        {"type": "delete", "clip_id": "clip_b"},
        revision=3,
        base=root_id,
    )
    assert deleted.changed is True
    assert [clip.clip_id for clip in deleted.decision.proposal_snapshot.clips] == [
        "clip_a",
        "clip_c",
    ]
    assert deleted.decision.proposal_snapshot.base_edit_version_id == root_id
    assert [item["clip_id"] for item in read_edit_history(Path(state["project_path"])).restorable_clips] == ["clip_b"]

    restored = _change(
        state,
        {
            "type": "restore",
            "clip_id": "clip_b",
            "insert_before_clip_id": "clip_c",
        },
        revision=4,
        base=deleted.decision.edit_version_id,
    )
    assert [clip.clip_id for clip in restored.decision.proposal_snapshot.clips] == [
        "clip_a",
        "clip_b",
        "clip_c",
    ]

    trimmed = _change(
        state,
        {
            "type": "trim",
            "clip_id": "clip_a",
            "source_in_ticks": 10_000,
            "source_out_ticks": 110_000,
        },
        revision=5,
        base=restored.decision.edit_version_id,
    )
    reordered = _change(
        state,
        {"type": "reorder", "ordered_clip_ids": ["clip_c", "clip_a", "clip_b"]},
        revision=6,
        base=trimmed.decision.edit_version_id,
    )
    assert [clip.clip_id for clip in reordered.decision.proposal_snapshot.clips] == [
        "clip_c",
        "clip_a",
        "clip_b",
    ]
    edit_files = set((Path(state["project_path"]) / "edits").glob("*.json"))

    reorder_noop = _change(
        state,
        {"type": "reorder", "ordered_clip_ids": ["clip_c", "clip_a", "clip_b"]},
        revision=7,
        base=reordered.decision.edit_version_id,
    )
    trim_noop = _change(
        state,
        {
            "type": "trim",
            "clip_id": "clip_a",
            "source_in_ticks": 10_000,
            "source_out_ticks": 110_000,
        },
        revision=7,
        base=reordered.decision.edit_version_id,
    )
    assert reorder_noop.changed is False
    assert trim_noop.changed is False
    assert ProjectStore(Path(state["project_path"])).load().revision == 7
    assert set((Path(state["project_path"]) / "edits").glob("*.json")) == edit_files


def test_schema_two_keeps_a_b_a_and_uses_the_same_history_behavior(tmp_path: Path) -> None:
    state = _multi_project(tmp_path)
    root_id = str(state["root_edit_id"])
    project_path = Path(state["project_path"])
    before = ProjectStore(project_path).load()
    before_files = set((project_path / "edits").glob("*.json"))
    for operation in (
        {"type": "delete", "clip_id": "missing"},
        {
            "type": "reorder",
            "ordered_clip_ids": ["clip_a_open", "clip_b", "clip_b"],
        },
        {
            "type": "trim",
            "clip_id": "clip_b",
            "source_in_ticks": 120_000,
            "source_out_ticks": 120_000,
        },
    ):
        with pytest.raises(ProjectError):
            _change(state, operation, revision=3, base=root_id)
    assert ProjectStore(project_path).load() == before
    assert set((project_path / "edits").glob("*.json")) == before_files

    trimmed = _change(
        state,
        {
            "type": "trim",
            "clip_id": "clip_b",
            "source_in_ticks": 10_000,
            "source_out_ticks": 100_000,
        },
        revision=3,
        base=root_id,
    )
    reordered = _change(
        state,
        {
            "type": "reorder",
            "ordered_clip_ids": ["clip_a_return", "clip_b", "clip_a_open"],
        },
        revision=4,
        base=trimmed.decision.edit_version_id,
    )
    assert reordered.decision.schema_version == 2
    assert [clip.source_id for clip in reordered.decision.proposal_snapshot.clips] == [
        "src_a",
        "src_b",
        "src_a",
    ]
    deleted = _change(
        state,
        {"type": "delete", "clip_id": "clip_b"},
        revision=5,
        base=reordered.decision.edit_version_id,
    )
    restored = _change(
        state,
        {"type": "restore", "clip_id": "clip_b", "insert_before_clip_id": "clip_a_open"},
        revision=6,
        base=deleted.decision.edit_version_id,
    )
    assert [clip.clip_id for clip in restored.decision.proposal_snapshot.clips] == [
        "clip_a_return",
        "clip_b",
        "clip_a_open",
    ]
    edit_files = set((project_path / "edits").glob("*.json"))
    assert (
        _change(
            state,
            {
                "type": "reorder",
                "ordered_clip_ids": ["clip_a_return", "clip_b", "clip_a_open"],
            },
            revision=7,
            base=restored.decision.edit_version_id,
        ).changed
        is False
    )
    assert (
        _change(
            state,
            {
                "type": "trim",
                "clip_id": "clip_b",
                "source_in_ticks": 10_000,
                "source_out_ticks": 100_000,
            },
            revision=7,
            base=restored.decision.edit_version_id,
        ).changed
        is False
    )
    assert set((project_path / "edits").glob("*.json")) == edit_files
    undone = undo_edit(
        project_path,
        expected_revision=7,
        base_edit_version_id=restored.decision.edit_version_id,
    )
    redone = redo_edit(
        project_path,
        expected_revision=8,
        base_edit_version_id=undone.active_edit_version_id,
    )
    assert redone.active_edit_version_id == restored.decision.edit_version_id
    snapshot = load_review_snapshot(project_path)
    assert [clip.clip_id for clip in snapshot.proposal.clips] == [
        "clip_a_return",
        "clip_b",
        "clip_a_open",
    ]


def test_reorder_permission_and_invalid_operations_leave_history_unchanged(tmp_path: Path) -> None:
    state = _single_project(tmp_path, allow_reorder=False)
    root = str(state["root_edit_id"])
    invalid = [
        {"type": "delete", "clip_id": "missing"},
        {"type": "restore", "clip_id": "missing", "insert_before_clip_id": None},
        {"type": "restore", "clip_id": "clip_a", "insert_before_clip_id": None},
        {"type": "reorder", "ordered_clip_ids": ["clip_b", "clip_a", "clip_c"]},
        {"type": "reorder", "ordered_clip_ids": ["clip_a", "clip_a", "clip_c"]},
        {
            "type": "trim",
            "clip_id": "clip_a",
            "source_in_ticks": True,
            "source_out_ticks": 100_000,
        },
        {
            "type": "trim",
            "clip_id": "clip_a",
            "source_in_ticks": 0.5,
            "source_out_ticks": 100_000,
        },
        {
            "type": "trim",
            "clip_id": "clip_a",
            "source_in_ticks": 0,
            "source_out_ticks": 120_001,
        },
    ]
    before = ProjectStore(Path(state["project_path"])).load()
    before_files = set((Path(state["project_path"]) / "edits").glob("*.json"))
    for operation in invalid:
        with pytest.raises(ProjectError):
            _change(state, operation, revision=3, base=root)
    assert ProjectStore(Path(state["project_path"])).load() == before
    assert set((Path(state["project_path"]) / "edits").glob("*.json")) == before_files

    only_one = _change(
        state, {"type": "delete", "clip_id": "clip_a"}, revision=3, base=root
    )
    second = _change(
        state,
        {"type": "delete", "clip_id": "clip_b"},
        revision=4,
        base=only_one.decision.edit_version_id,
    )
    with pytest.raises(ProjectError, match="empty"):
        _change(
            state,
            {"type": "delete", "clip_id": "clip_c"},
            revision=5,
            base=second.decision.edit_version_id,
        )


def test_stale_revision_base_and_transcript_bindings_are_rejected(tmp_path: Path) -> None:
    state = _single_project(tmp_path)
    root = str(state["root_edit_id"])
    with pytest.raises(ProjectError, match="revision"):
        _change(state, {"type": "delete", "clip_id": "clip_a"}, revision=2, base=root)
    with pytest.raises(ProjectError, match="base"):
        _change(state, {"type": "delete", "clip_id": "clip_a"}, revision=3, base="edit_wrong")

    store = ProjectStore(Path(state["project_path"]))
    project = store.load()
    stale = replace(
        project,
        revision=4,
        active_transcript_versions={str(state["source_id"]): "tr_other"},
    )
    store.save(stale, expected_revision=3)
    with pytest.raises(ProjectError, match="Transcript"):
        _change(state, {"type": "delete", "clip_id": "clip_a"}, revision=4, base=root)


def test_undo_and_redo_reject_stale_transcript_bindings(tmp_path: Path) -> None:
    undo_state = _single_project(tmp_path / "undo")
    undo_root = str(undo_state["root_edit_id"])
    undo_child = _change(
        undo_state,
        {"type": "delete", "clip_id": "clip_b"},
        revision=3,
        base=undo_root,
    )
    undo_path = Path(undo_state["project_path"])
    undo_store = ProjectStore(undo_path)
    undo_project = undo_store.load()
    undo_store.save(
        replace(
            undo_project,
            revision=5,
            active_transcript_versions={str(undo_state["source_id"]): "tr_other"},
        ),
        expected_revision=4,
    )
    with pytest.raises(ProjectError, match="Transcript"):
        undo_edit(
            undo_path,
            expected_revision=5,
            base_edit_version_id=undo_child.decision.edit_version_id,
        )
    assert ProjectStore(undo_path).load().revision == 5

    redo_state = _single_project(tmp_path / "redo")
    redo_root = str(redo_state["root_edit_id"])
    redo_child = _change(
        redo_state,
        {"type": "delete", "clip_id": "clip_b"},
        revision=3,
        base=redo_root,
    )
    redo_path = Path(redo_state["project_path"])
    undo_edit(
        redo_path,
        expected_revision=4,
        base_edit_version_id=redo_child.decision.edit_version_id,
    )
    redo_store = ProjectStore(redo_path)
    redo_project = redo_store.load()
    redo_store.save(
        replace(
            redo_project,
            revision=6,
            active_transcript_versions={str(redo_state["source_id"]): "tr_other"},
        ),
        expected_revision=5,
    )
    with pytest.raises(ProjectError, match="Transcript"):
        redo_edit(
            redo_path,
            expected_revision=6,
            base_edit_version_id=redo_root,
        )
    assert ProjectStore(redo_path).load().revision == 6


def test_undo_redo_branching_and_history_survive_restart(tmp_path: Path) -> None:
    state = _single_project(tmp_path)
    root = str(state["root_edit_id"])
    first = _change(
        state, {"type": "delete", "clip_id": "clip_b"}, revision=3, base=root
    )
    second = _change(
        state,
        {"type": "trim", "clip_id": "clip_a", "source_in_ticks": 10_000, "source_out_ticks": 110_000},
        revision=4,
        base=first.decision.edit_version_id,
    )
    undone = undo_edit(
        Path(state["project_path"]),
        expected_revision=5,
        base_edit_version_id=second.decision.edit_version_id,
    )
    assert undone.active_edit_version_id == first.decision.edit_version_id
    assert undone.edit_redo_stack == (second.decision.edit_version_id,)
    redone = redo_edit(
        Path(state["project_path"]),
        expected_revision=6,
        base_edit_version_id=first.decision.edit_version_id,
    )
    assert redone.active_edit_version_id == second.decision.edit_version_id
    assert redone.edit_redo_stack == ()

    undo_edit(
        Path(state["project_path"]),
        expected_revision=7,
        base_edit_version_id=second.decision.edit_version_id,
    )
    old_branch = Path(state["project_path"]) / "edits" / f"{second.decision.edit_version_id}.json"
    branch = _change(
        state,
        {"type": "restore", "clip_id": "clip_b", "insert_before_clip_id": None},
        revision=8,
        base=first.decision.edit_version_id,
    )
    reloaded = ProjectStore(Path(state["project_path"])).load()
    assert reloaded.active_edit_version_id == branch.decision.edit_version_id
    assert reloaded.edit_redo_stack == ()
    assert old_branch.is_file()
    history = read_edit_history(Path(state["project_path"]))
    assert history.active_edit_version_id == branch.decision.edit_version_id
    assert history.can_undo is True
    assert history.can_redo is False


def test_ten_changes_five_navigation_steps_then_new_branch(tmp_path: Path) -> None:
    state = _single_project(tmp_path)
    current = str(state["root_edit_id"])
    revision = 3
    operations = [
        {"type": "trim", "clip_id": "clip_a", "source_in_ticks": 10_000, "source_out_ticks": 120_000},
        {"type": "trim", "clip_id": "clip_b", "source_in_ticks": 130_000, "source_out_ticks": 240_000},
        {"type": "reorder", "ordered_clip_ids": ["clip_b", "clip_a", "clip_c"]},
        {"type": "delete", "clip_id": "clip_c"},
        {"type": "restore", "clip_id": "clip_c", "insert_before_clip_id": None},
        {"type": "trim", "clip_id": "clip_c", "source_in_ticks": 250_000, "source_out_ticks": 350_000},
        {"type": "reorder", "ordered_clip_ids": ["clip_a", "clip_c", "clip_b"]},
        {"type": "delete", "clip_id": "clip_b"},
        {"type": "restore", "clip_id": "clip_b", "insert_before_clip_id": "clip_c"},
        {"type": "trim", "clip_id": "clip_a", "source_in_ticks": 20_000, "source_out_ticks": 110_000},
    ]
    decisions: list[str] = []
    for operation in operations:
        result = _change(state, operation, revision=revision, base=current)
        revision += 1
        current = result.decision.edit_version_id
        decisions.append(current)

    for _ in range(3):
        navigation = undo_edit(
            Path(state["project_path"]),
            expected_revision=revision,
            base_edit_version_id=current,
        )
        revision += 1
        current = navigation.active_edit_version_id
    for _ in range(2):
        navigation = redo_edit(
            Path(state["project_path"]),
            expected_revision=revision,
            base_edit_version_id=current,
        )
        revision += 1
        current = navigation.active_edit_version_id

    abandoned = decisions[-1]
    branch = _change(
        state,
        {"type": "trim", "clip_id": "clip_a", "source_in_ticks": 30_000, "source_out_ticks": 100_000},
        revision=revision,
        base=current,
    )
    history = read_edit_history(Path(state["project_path"]))
    assert history.project_revision == revision + 1
    assert history.active_edit_version_id == branch.decision.edit_version_id
    assert history.redo_stack == ()
    assert (Path(state["project_path"]) / "edits" / f"{abandoned}.json").is_file()
    assert [clip.clip_id for clip in history.current_clips] == ["clip_a", "clip_b", "clip_c"]
    snapshot = load_review_snapshot(Path(state["project_path"]))
    assert [clip.clip_id for clip in snapshot.proposal.clips] == ["clip_a", "clip_b", "clip_c"]


@pytest.mark.parametrize("fixture_name", ["single", "multi"])
def test_project_save_failure_leaves_no_decision_or_project_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture_name: str
) -> None:
    state = (
        _single_project(tmp_path)
        if fixture_name == "single"
        else _multi_project(tmp_path)
    )
    root = str(state["root_edit_id"])
    project_path = Path(state["project_path"])
    before = ProjectStore(project_path).load()
    before_files = set((project_path / "edits").glob("*.json"))

    def fail_save(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected save failure")

    monkeypatch.setattr(ProjectStore, "save", fail_save)
    with pytest.raises(OSError, match="injected"):
        _change(state, {"type": "delete", "clip_id": "clip_b"}, revision=3, base=root)
    assert ProjectStore(project_path).load() == before
    assert set((project_path / "edits").glob("*.json")) == before_files


@pytest.mark.parametrize("fixture_name", ["single", "multi"])
def test_candidate_write_failure_leaves_no_decision_or_project_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture_name: str
) -> None:
    state = (
        _single_project(tmp_path)
        if fixture_name == "single"
        else _multi_project(tmp_path)
    )
    root = str(state["root_edit_id"])
    project_path = Path(state["project_path"])
    before = ProjectStore(project_path).load()
    before_files = set((project_path / "edits").glob("*.json"))

    def fail_write(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected candidate write failure")

    monkeypatch.setattr("roughcut.application.edits.write_new_json", fail_write)
    with pytest.raises(OSError, match="candidate"):
        _change(state, {"type": "delete", "clip_id": "clip_b"}, revision=3, base=root)
    assert ProjectStore(project_path).load() == before
    assert set((project_path / "edits").glob("*.json")) == before_files


def test_corrupt_missing_self_cycle_and_redo_history_are_rejected(tmp_path: Path) -> None:
    for case in ("missing", "self", "cycle", "redo"):
        state = _single_project(tmp_path / case)
        root = str(state["root_edit_id"])
        child = _change(
            state, {"type": "delete", "clip_id": "clip_b"}, revision=3, base=root
        )
        project_path = Path(state["project_path"])
        child_path = project_path / "edits" / f"{child.decision.edit_version_id}.json"
        if case in {"missing", "self"}:
            data = json.loads(child_path.read_text(encoding="utf-8"))
            data["proposal_snapshot"]["base_edit_version_id"] = (
                "edit_missing" if case == "missing" else child.decision.edit_version_id
            )
            child_path.write_text(json.dumps(data), encoding="utf-8")
        elif case == "cycle":
            root_path = project_path / "edits" / f"{root}.json"
            data = json.loads(root_path.read_text(encoding="utf-8"))
            data["proposal_snapshot"]["base_edit_version_id"] = child.decision.edit_version_id
            root_path.write_text(json.dumps(data), encoding="utf-8")
        else:
            store = ProjectStore(project_path)
            project = store.load()
            store.save(
                replace(project, revision=5, edit_redo_stack=(root,)),
                expected_revision=4,
            )
        with pytest.raises(ProjectError):
            read_edit_history(project_path)


def test_schema_versions_cannot_be_cross_linked(tmp_path: Path) -> None:
    single = _single_project(tmp_path / "single")
    multi = _multi_project(tmp_path / "multi")
    single_path = Path(single["project_path"])
    multi_id = str(multi["root_edit_id"])
    multi_data = json.loads(
        (Path(multi["project_path"]) / "edits" / f"{multi_id}.json").read_text(
            encoding="utf-8"
        )
    )
    multi_data["proposal_snapshot"]["base_edit_version_id"] = single["root_edit_id"]
    write_new_json(single_path / "edits" / f"{multi_id}.json", multi_data)
    store = ProjectStore(single_path)
    project = store.load()
    store.save(
        replace(project, revision=4, active_edit_version_id=multi_id),
        expected_revision=3,
    )

    with pytest.raises(ProjectError, match="mixes schema"):
        read_edit_history(single_path)


def test_root_and_empty_redo_rejections_and_navigation_save_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = _single_project(tmp_path)
    root = str(state["root_edit_id"])
    project_path = Path(state["project_path"])
    with pytest.raises(ProjectError, match="undo"):
        undo_edit(project_path, expected_revision=3, base_edit_version_id=root)
    with pytest.raises(ProjectError, match="redo"):
        redo_edit(project_path, expected_revision=3, base_edit_version_id=root)

    child = _change(
        state, {"type": "delete", "clip_id": "clip_b"}, revision=3, base=root
    )
    before = ProjectStore(project_path).load()
    redo_state = _single_project(tmp_path / "redo")
    redo_root = str(redo_state["root_edit_id"])
    redo_child = _change(
        redo_state,
        {"type": "delete", "clip_id": "clip_b"},
        revision=3,
        base=redo_root,
    )
    undo_edit(
        Path(redo_state["project_path"]),
        expected_revision=4,
        base_edit_version_id=redo_child.decision.edit_version_id,
    )
    redo_before = ProjectStore(Path(redo_state["project_path"])).load()

    def fail_save(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected navigation save failure")

    monkeypatch.setattr(ProjectStore, "save", fail_save)
    with pytest.raises(OSError, match="navigation"):
        undo_edit(
            project_path,
            expected_revision=4,
            base_edit_version_id=child.decision.edit_version_id,
        )
    assert ProjectStore(project_path).load() == before
    with pytest.raises(OSError, match="navigation"):
        redo_edit(
            Path(redo_state["project_path"]),
            expected_revision=5,
            base_edit_version_id=redo_root,
        )
    assert ProjectStore(Path(redo_state["project_path"])).load() == redo_before


@pytest.mark.parametrize("fixture_name", ["single", "multi"])
def test_confirming_a_new_proposal_after_undo_clears_redo(
    tmp_path: Path, fixture_name: str
) -> None:
    state = (
        _single_project(tmp_path)
        if fixture_name == "single"
        else _multi_project(tmp_path)
    )
    project_path = Path(state["project_path"])
    root = str(state["root_edit_id"])
    child = _change(
        state, {"type": "delete", "clip_id": "clip_b"}, revision=3, base=root
    )
    undo_edit(
        project_path,
        expected_revision=4,
        base_edit_version_id=child.decision.edit_version_id,
    )
    history = read_edit_history(project_path)
    clips = [clip.to_dict() for clip in history.current_clips]
    if fixture_name == "single":
        context = read_agent_context(
            project_path,
            source_id=str(state["source_id"]),
            transcript_version_id=str(state["transcript_id"]),
            brief_id=str(state["brief_id"]),
            expected_revision=5,
            offset=0,
            limit=3,
        )
        proposal = create_edit_proposal(
            project_path,
            source_id=str(state["source_id"]),
            transcript_version_id=str(state["transcript_id"]),
            brief_id=str(state["brief_id"]),
            context_hash=context.context_hash,
            clips=clips,
            total_duration_ticks=history.total_duration_ticks,
            expected_revision=5,
        )
        confirmed = confirm_edit_proposal(
            project_path, proposal.proposal.proposal_id, expected_revision=5
        )
    else:
        bindings = state["source_bindings"]
        assert isinstance(bindings, list)
        context = read_multi_source_agent_context(
            project_path,
            source_bindings=bindings,
            brief_id=str(state["brief_id"]),
            expected_revision=5,
            offset=0,
            limit=6,
        )
        proposal = create_multi_source_edit_proposal(
            project_path,
            source_bindings=bindings,
            brief_id=str(state["brief_id"]),
            context_hash=context.context_hash,
            clips=clips,
            total_duration_ticks=history.total_duration_ticks,
            expected_revision=5,
        )
        confirmed = confirm_multi_source_edit_proposal(
            project_path, proposal.proposal.proposal_id, expected_revision=5
        )
    project = ProjectStore(project_path).load()
    assert project.revision == 6
    assert project.active_edit_version_id == confirmed.decision.edit_version_id
    assert project.edit_redo_stack == ()
    assert (project_path / "edits" / f"{child.decision.edit_version_id}.json").is_file()


def test_actual_cli_and_stdio_mcp_edit_change_payloads_are_equivalent(tmp_path: Path) -> None:
    state = _single_project(tmp_path / "fixture")
    cli_project = Path(state["project_path"])
    mcp_project = tmp_path / "mcp clone"
    shutil.copytree(cli_project, mcp_project)
    root = str(state["root_edit_id"])
    operation = {"type": "delete", "clip_id": "clip_b"}
    cli = subprocess.run(
        [
            sys.executable,
            "-m",
            "roughcut.cli",
            "edit-change",
            "--project",
            str(cli_project),
            "--base-edit-version-id",
            root,
            "--operation-json",
            json.dumps(operation),
            "--expected-revision",
            "3",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert cli.returncode == 0, cli.stderr
    assert cli.stderr == ""
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "edit_change",
            "arguments": {
                "project_path": str(mcp_project),
                "base_edit_version_id": root,
                "operation": operation,
                "expected_revision": 3,
            },
        },
    }
    mcp = subprocess.run(
        [sys.executable, "-m", "roughcut.mcp"],
        input=json.dumps(request) + "\n",
        check=False,
        capture_output=True,
        text=True,
    )
    assert mcp.returncode == 0, mcp.stderr
    assert mcp.stderr == ""
    mcp_response = json.loads(mcp.stdout)
    mcp_payload = mcp_response["result"]["structuredContent"]

    assert _normalized_change_payload(json.loads(cli.stdout)) == _normalized_change_payload(
        mcp_payload
    )
    assert TOOL_SCHEMA_VERSION == 32


def test_cli_and_mcp_edit_failures_use_the_same_versioned_error(tmp_path: Path) -> None:
    cli = subprocess.run(
        [
            sys.executable,
            "-m",
            "roughcut.cli",
            "edit-change",
            "--project",
            str(tmp_path),
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert cli.returncode == 2
    assert cli.stderr == ""
    cli_payload = json.loads(cli.stdout)
    request = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "edit_change",
            "arguments": {"project_path": str(tmp_path)},
        },
    }
    mcp = subprocess.run(
        [sys.executable, "-m", "roughcut.mcp"],
        input=json.dumps(request) + "\n",
        check=False,
        capture_output=True,
        text=True,
    )
    mcp_payload = json.loads(mcp.stdout)["result"]["structuredContent"]
    assert cli_payload["error"] == mcp_payload["error"] == {"code": "invalid_arguments"}
    assert cli_payload["tool_schema_version"] == mcp_payload["tool_schema_version"] == 32

    state = _single_project(tmp_path / "domain")
    domain_cli = subprocess.run(
        [
            sys.executable,
            "-m",
            "roughcut.cli",
            "edit-change",
            "--project",
            str(state["project_path"]),
            "--base-edit-version-id",
            str(state["root_edit_id"]),
            "--operation-json",
            json.dumps({"type": "delete", "clip_id": "missing"}),
            "--expected-revision",
            "3",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    domain_request = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "edit_change",
            "arguments": {
                "project_path": str(state["project_path"]),
                "base_edit_version_id": state["root_edit_id"],
                "operation": {"type": "delete", "clip_id": "missing"},
                "expected_revision": 3,
            },
        },
    }
    domain_mcp = subprocess.run(
        [sys.executable, "-m", "roughcut.mcp"],
        input=json.dumps(domain_request) + "\n",
        check=False,
        capture_output=True,
        text=True,
    )
    assert json.loads(domain_cli.stdout)["error"] == json.loads(domain_mcp.stdout)[
        "result"
    ]["structuredContent"]["error"] == {"code": "edit_operation_failed"}

    typed_cli = subprocess.run(
        [
            sys.executable,
            "-m",
            "roughcut.cli",
            "edit-undo",
            "--project",
            str(state["project_path"]),
            "--base-edit-version-id",
            str(state["root_edit_id"]),
            "--expected-revision",
            "not-an-integer",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    typed_request = {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {
            "name": "edit_undo",
            "arguments": {
                "project_path": str(state["project_path"]),
                "base_edit_version_id": state["root_edit_id"],
                "expected_revision": "not-an-integer",
            },
        },
    }
    typed_mcp = subprocess.run(
        [sys.executable, "-m", "roughcut.mcp"],
        input=json.dumps(typed_request) + "\n",
        check=False,
        capture_output=True,
        text=True,
    )
    assert json.loads(typed_cli.stdout)["error"] == json.loads(typed_mcp.stdout)[
        "result"
    ]["structuredContent"]["error"] == {"code": "invalid_arguments"}


def _normalized_change_payload(payload: dict[str, object]) -> dict[str, object]:
    normalized = json.loads(json.dumps(payload))
    change = normalized["edit_change"]
    decision = change["decision"]
    decision["edit_version_id"] = "<new-edit>"
    decision["created_at"] = "<created-at>"
    decision["proposal_snapshot"]["proposal_id"] = "<direct-proposal>"
    decision["proposal_snapshot"]["created_at"] = "<created-at>"
    return normalized
