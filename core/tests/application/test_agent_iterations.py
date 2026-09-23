from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import roughcut.application.proposals as proposal_module
from roughcut.adapters.artifact_store import write_new_json
from roughcut.adapters.project_store import ProjectStore
from roughcut.application.agent_context import (
    create_edit_brief,
    read_agent_context,
    read_multi_source_agent_context,
    read_revision_context,
)
from roughcut.application.edits import change_edit, undo_edit
from roughcut.application.health import TOOL_SCHEMA_VERSION
from roughcut.application.people import (
    confirm_speaker_map,
    create_person,
    update_source_metadata,
)
from roughcut.application.projects import create_project
from roughcut.application.proposals import (
    confirm_edit_proposal,
    confirm_multi_source_edit_proposal,
    create_edit_proposal,
    create_multi_source_edit_proposal,
    read_proposal_diff,
)
from roughcut.domain.edit import EditClip, EditProposal
from roughcut.domain.people import Person, SpeakerMap
from roughcut.domain.project import (
    ImportMode,
    MediaProbe,
    ProjectError,
    SourceAsset,
    SourceFingerprint,
)
from roughcut.domain.transcript import (
    TimedTranscript,
    TranscriptProvenance,
    TranscriptSegment,
)


def _stdio_mcp(tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    completed = subprocess.run(
        [sys.executable, "-m", "roughcut.mcp"],
        input=json.dumps(request, ensure_ascii=False) + "\n",
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    response = json.loads(completed.stdout)
    return response["result"]["structuredContent"]


def _source(source_id: str, index: int) -> SourceAsset:
    return SourceAsset(
        source_id=source_id,
        kind="audio",
        display_name=f"素材 {source_id}.wav",
        import_mode=ImportMode.LINKED,
        locator={"absolute_path": f"/private/绝对路径/{source_id}.wav"},
        fingerprint=SourceFingerprint(100 + index, index, f"fingerprint-{index}"),
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
            audio_sample_rate=16_000,
            rotation_degrees=0,
        ),
        tags=("访谈", source_id),
        note=f"{source_id} 备注",
    )


def _transcript(source_id: str, transcript_id: str) -> TimedTranscript:
    return TimedTranscript(
        schema_version=1,
        transcript_version_id=transcript_id,
        source_id=source_id,
        parent_version_id=None,
        provenance=TranscriptProvenance(
            backend="fixture",
            package_version="fixture",
            models={"private": "must-not-leak"},
            parameters={"provider": "must-not-leak"},
            raw_result_path=f"raw-asr/{source_id}/private.json",
            started_at="fixture",
            completed_at="fixture",
            exit_status=0,
        ),
        language="zh-CN",
        segments=tuple(
            TranscriptSegment(
                segment_id=f"seg_{index}",
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
            for index in range(1, 5)
        ),
    )


def _clip(source_id: str, index: int, clip_id: str) -> dict[str, object]:
    return {
        "clip_id": clip_id,
        "source_id": source_id,
        "transcript_version_id": f"tr_{source_id.removeprefix('src_')}",
        "segment_id": f"seg_{index}",
        "source_in_ticks": (index - 1) * 120_000,
        "source_out_ticks": index * 120_000,
        "reason": f"保留 {source_id} {index}",
        "display_text": f"{source_id} 第 {index} 段。",
    }


def _setup_project(
    tmp_path: Path,
    *,
    schema: int,
    allow_reorder: bool = True,
    confirm: bool = True,
) -> dict[str, object]:
    root = tmp_path / f"revision schema {schema}"
    project = create_project(root, f"Revision schema {schema}")
    bindings = [("src_a", "tr_a"), ("src_b", "tr_b"), ("src_c", "tr_c")]
    for source_id, transcript_id in bindings:
        write_new_json(
            root / "transcripts" / source_id / f"{transcript_id}.json",
            _transcript(source_id, transcript_id).to_dict(),
        )
    imported = replace(
        project,
        revision=1,
        sources=tuple(_source(source_id, index) for index, (source_id, _) in enumerate(bindings)),
        persons=(
            Person("person_a", "人物 A", "guest", ""),
            Person("person_b", "人物 B", "guest", ""),
        ),
        speaker_maps=(
            SpeakerMap("src_a", "tr_a", "spk_0", "person_a", True),
            SpeakerMap("src_b", "tr_b", "spk_0", "person_b", True),
        ),
        active_transcript_versions=dict(bindings),
    )
    ProjectStore(root).save(imported, expected_revision=0)
    brief = create_edit_brief(
        root,
        theme="当前主题",
        target_duration_ticks=720_000,
        focus=["重点"],
        allow_reorder=allow_reorder,
        expected_revision=1,
    )
    if schema == 1:
        context = read_agent_context(
            root,
            source_id="src_a",
            transcript_version_id="tr_a",
            brief_id=brief.brief.brief_id,
            expected_revision=2,
            offset=0,
            limit=4,
        )
        clips = [_clip("src_a", 1, "clip_a1"), _clip("src_a", 2, "clip_a2")]
        proposal = create_edit_proposal(
            root,
            source_id="src_a",
            transcript_version_id="tr_a",
            brief_id=brief.brief.brief_id,
            context_hash=context.context_hash,
            clips=clips,
            total_duration_ticks=240_000,
            expected_revision=2,
        )
        decision = (
            confirm_edit_proposal(root, proposal.proposal.proposal_id, expected_revision=2)
            if confirm
            else None
        )
        scoped_bindings = [
            {"source_id": "src_a", "transcript_version_id": "tr_a"}
        ]
    else:
        scoped_bindings = [
            {"source_id": "src_a", "transcript_version_id": "tr_a"},
            {"source_id": "src_b", "transcript_version_id": "tr_b"},
        ]
        context = read_multi_source_agent_context(
            root,
            source_bindings=scoped_bindings,
            brief_id=brief.brief.brief_id,
            expected_revision=2,
            offset=0,
            limit=8,
        )
        clips = (
            [
                _clip("src_a", 1, "clip_a1"),
                _clip("src_b", 1, "clip_b1"),
                _clip("src_a", 2, "clip_a2"),
            ]
            if allow_reorder
            else [
                _clip("src_a", 1, "clip_a1"),
                _clip("src_a", 2, "clip_a2"),
                _clip("src_b", 1, "clip_b1"),
            ]
        )
        proposal = create_multi_source_edit_proposal(
            root,
            source_bindings=scoped_bindings,
            brief_id=brief.brief.brief_id,
            context_hash=context.context_hash,
            clips=clips,
            total_duration_ticks=360_000,
            expected_revision=2,
        )
        decision = (
            confirm_multi_source_edit_proposal(
                root, proposal.proposal.proposal_id, expected_revision=2
            )
            if confirm
            else None
        )
    return {
        "project_path": root,
        "brief_id": brief.brief.brief_id,
        "proposal": proposal.proposal,
        "decision": decision.decision if decision is not None else None,
        "bindings": scoped_bindings,
        "revision": 3 if confirm else 2,
    }


def _revision(state: dict[str, object], *, offset: int = 0, limit: int = 2):
    return read_revision_context(
        Path(state["project_path"]),
        expected_revision=int(state["revision"]),
        offset=offset,
        limit=limit,
    )


def _create_revision_proposal(
    state: dict[str, object], clips: list[dict[str, object]]
):
    context = _revision(state, limit=20)
    if context.edit_schema_version == 1:
        binding = context.source_bindings[0]
        return create_edit_proposal(
            Path(state["project_path"]),
            source_id=binding.source_id,
            transcript_version_id=binding.transcript_version_id,
            brief_id=context.brief.brief_id,
            context_hash=context.context_hash,
            clips=clips,
            total_duration_ticks=sum(
                int(clip["source_out_ticks"]) - int(clip["source_in_ticks"])
                for clip in clips
            ),
            expected_revision=context.project_revision,
        )
    return create_multi_source_edit_proposal(
        Path(state["project_path"]),
        source_bindings=[binding.to_dict() for binding in context.source_bindings],
        brief_id=context.brief.brief_id,
        context_hash=context.context_hash,
        clips=clips,
        total_duration_ticks=sum(
            int(clip["source_out_ticks"]) - int(clip["source_in_ticks"])
            for clip in clips
        ),
        expected_revision=context.project_revision,
    )


def test_schema_one_revision_context_reuses_agent_hash_and_freezes_every_page(
    tmp_path: Path,
) -> None:
    state = _setup_project(tmp_path, schema=1)
    newer_brief = create_edit_brief(
        Path(state["project_path"]),
        theme="用户更新后的主题",
        target_duration_ticks=600_000,
        focus=["新重点"],
        allow_reorder=False,
        expected_revision=3,
    )
    state["revision"] = newer_brief.project_revision
    state["brief_id"] = newer_brief.brief.brief_id

    first = _revision(state, offset=0, limit=2)
    second = _revision(state, offset=2, limit=2)
    binding = first.source_bindings[0]
    ordinary = read_agent_context(
        Path(state["project_path"]),
        source_id=binding.source_id,
        transcript_version_id=binding.transcript_version_id,
        brief_id=newer_brief.brief.brief_id,
        expected_revision=4,
        offset=0,
        limit=1,
    )

    assert first.schema_version == 1
    assert first.edit_schema_version == 1
    assert first.context_hash == second.context_hash == ordinary.context_hash
    assert first.project_revision == second.project_revision == 4
    assert first.base_edit_version_id == second.base_edit_version_id
    assert first.source_bindings == second.source_bindings
    assert first.brief == second.brief == newer_brief.brief
    assert first.base_clips == second.base_clips
    assert first.base_total_duration_ticks == 240_000
    assert [segment["segment_id"] for segment in first.segments] == ["seg_1", "seg_2"]
    assert all(segment["source_id"] == "src_a" for segment in first.segments)
    payload = json.dumps(first.to_dict(), ensure_ascii=False)
    for forbidden in (
        str(state["project_path"]),
        "/private/",
        "locator",
        "fingerprint",
        "raw-asr",
        "provider",
        "models",
    ):
        assert forbidden not in payload


def test_schema_two_revision_context_keeps_bindings_people_and_a_b_a(
    tmp_path: Path,
) -> None:
    state = _setup_project(tmp_path, schema=2)
    first = _revision(state, offset=0, limit=3)
    second = _revision(state, offset=3, limit=3)

    assert first.context_hash == second.context_hash
    assert [binding.to_dict() for binding in first.source_bindings] == state["bindings"]
    assert [clip.source_id for clip in first.base_clips] == ["src_a", "src_b", "src_a"]
    assert first.segments[0]["segment_id"] == second.segments[1]["segment_id"] == "seg_1"
    assert first.segments[0]["person_id"] == "person_a"
    assert second.segments[1]["person_id"] == "person_b"
    assert first.segments[0]["local_speaker_id"] == "spk_0"
    assert second.segments[1]["local_speaker_id"] == "spk_0"


def test_revision_context_rejects_missing_stale_or_corrupt_state(tmp_path: Path) -> None:
    no_decision = _setup_project(tmp_path / "none", schema=1, confirm=False)
    with pytest.raises(ProjectError, match="active Edit"):
        _revision(no_decision)

    state = _setup_project(tmp_path / "state", schema=1)
    with pytest.raises(ProjectError, match="revision conflict"):
        read_revision_context(
            Path(state["project_path"]), expected_revision=2, offset=0, limit=2
        )
    with pytest.raises(ProjectError, match="offset"):
        read_revision_context(
            Path(state["project_path"]), expected_revision=3, offset=-1, limit=2
        )
    with pytest.raises(ProjectError, match="limit"):
        read_revision_context(
            Path(state["project_path"]), expected_revision=3, offset=0, limit=0
        )

    root = Path(state["project_path"])
    project = ProjectStore(root).load()
    missing_brief = replace(project, active_brief_id=None)
    ProjectStore(root).save(missing_brief, expected_revision=3)
    state["revision"] = 3
    with pytest.raises(ProjectError, match="active Brief"):
        _revision(state)

    corrupt = _setup_project(tmp_path / "corrupt", schema=2)
    decision = corrupt["decision"]
    assert decision is not None
    (Path(corrupt["project_path"]) / "edits" / f"{decision.edit_version_id}.json").write_text(
        '{"schema_version": 99}\n', encoding="utf-8"
    )
    with pytest.raises(ProjectError, match="schema"):
        _revision(corrupt)


def test_revision_context_rejects_stale_decision_transcript_binding(tmp_path: Path) -> None:
    state = _setup_project(tmp_path, schema=2)
    root = Path(state["project_path"])
    alternate = replace(
        _transcript("src_a", "tr_a"),
        transcript_version_id="tr_a_new",
        parent_version_id="tr_a",
    )
    write_new_json(root / "transcripts" / "src_a" / "tr_a_new.json", alternate.to_dict())
    project = ProjectStore(root).load()
    changed = replace(
        project,
        revision=project.revision + 1,
        active_transcript_versions={**project.active_transcript_versions, "src_a": "tr_a_new"},
    )
    ProjectStore(root).save(changed, expected_revision=project.revision)
    state["revision"] = changed.revision

    with pytest.raises(ProjectError, match="active"):
        _revision(state)


def test_initial_proposals_remain_compatible_without_active_edit(tmp_path: Path) -> None:
    for schema in (1, 2):
        state = _setup_project(tmp_path / str(schema), schema=schema, confirm=False)
        project = ProjectStore(Path(state["project_path"])).load()
        proposal = state["proposal"]
        assert proposal.base_edit_version_id is None
        assert project.active_edit_version_id is None
        assert project.revision == 2


def test_revision_scope_rejects_cross_schema_binding_changes_and_clip_identity_reuse(
    tmp_path: Path,
) -> None:
    single = _setup_project(tmp_path / "single", schema=1)
    root = Path(single["project_path"])
    multi_context = read_multi_source_agent_context(
        root,
        source_bindings=[
            {"source_id": "src_a", "transcript_version_id": "tr_a"},
            {"source_id": "src_b", "transcript_version_id": "tr_b"},
        ],
        brief_id=str(single["brief_id"]),
        expected_revision=3,
        offset=0,
        limit=1,
    )
    with pytest.raises(ProjectError, match="schema"):
        create_multi_source_edit_proposal(
            root,
            source_bindings=[
                {"source_id": "src_a", "transcript_version_id": "tr_a"},
                {"source_id": "src_b", "transcript_version_id": "tr_b"},
            ],
            brief_id=str(single["brief_id"]),
            context_hash=multi_context.context_hash,
            clips=[_clip("src_a", 1, "clip_a1"), _clip("src_b", 1, "clip_b1")],
            total_duration_ticks=240_000,
            expected_revision=3,
        )

    multi = _setup_project(tmp_path / "multi", schema=2)
    root = Path(multi["project_path"])
    single_context = read_agent_context(
        root,
        source_id="src_a",
        transcript_version_id="tr_a",
        brief_id=str(multi["brief_id"]),
        expected_revision=3,
        offset=0,
        limit=1,
    )
    with pytest.raises(ProjectError, match="schema"):
        create_edit_proposal(
            root,
            source_id="src_a",
            transcript_version_id="tr_a",
            brief_id=str(multi["brief_id"]),
            context_hash=single_context.context_hash,
            clips=[_clip("src_a", 1, "clip_a1")],
            total_duration_ticks=120_000,
            expected_revision=3,
        )

    for bindings in (
        [
            {"source_id": "src_b", "transcript_version_id": "tr_b"},
            {"source_id": "src_a", "transcript_version_id": "tr_a"},
        ],
        [
            {"source_id": "src_a", "transcript_version_id": "tr_a"},
            {"source_id": "src_c", "transcript_version_id": "tr_c"},
        ],
        [
            {"source_id": "src_a", "transcript_version_id": "tr_a"},
            {"source_id": "src_b", "transcript_version_id": "tr_b"},
            {"source_id": "src_c", "transcript_version_id": "tr_c"},
        ],
    ):
        context = read_multi_source_agent_context(
            root,
            source_bindings=bindings,
            brief_id=str(multi["brief_id"]),
            expected_revision=3,
            offset=0,
            limit=1,
        )
        with pytest.raises(ProjectError, match="bindings"):
            create_multi_source_edit_proposal(
                root,
                source_bindings=bindings,
                brief_id=str(multi["brief_id"]),
                context_hash=context.context_hash,
                clips=[_clip("src_a", 1, "clip_a1"), _clip(bindings[1]["source_id"], 1, "new")],
                total_duration_ticks=240_000,
                expected_revision=3,
            )

    context = _revision(multi, limit=20)
    reused = [clip.to_dict() for clip in context.base_clips]
    reused[0] = {
        **reused[0],
        "source_id": "src_b",
        "transcript_version_id": "tr_b",
        "segment_id": "seg_2",
        "display_text": "src_b 第 2 段。",
    }
    with pytest.raises(ProjectError, match="identity"):
        _create_revision_proposal(multi, reused)

    with pytest.raises(ProjectError, match="at least two"):
        create_multi_source_edit_proposal(
            root,
            source_bindings=[
                {"source_id": "src_a", "transcript_version_id": "tr_a"}
            ],
            brief_id=context.brief.brief_id,
            context_hash=context.context_hash,
            clips=[_clip("src_a", 1, "new_clip")],
            total_duration_ticks=120_000,
            expected_revision=3,
        )


def test_confirm_rechecks_schema_bindings_and_reused_clip_identity(tmp_path: Path) -> None:
    single = _setup_project(tmp_path / "identity", schema=1)
    single_context = _revision(single, limit=20)
    proposal = _create_revision_proposal(
        single, [clip.to_dict() for clip in single_context.base_clips]
    )
    root = Path(single["project_path"])
    proposal_path = root / "proposals" / f"{proposal.proposal.proposal_id}.json"
    data = json.loads(proposal_path.read_text(encoding="utf-8"))
    data["clips"][0].update(
        {
            "segment_id": "seg_3",
            "source_in_ticks": 240_000,
            "source_out_ticks": 360_000,
            "display_text": "src_a 第 3 段。",
        }
    )
    proposal_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ProjectError, match="identity"):
        confirm_edit_proposal(root, proposal.proposal.proposal_id, expected_revision=3)
    assert ProjectStore(root).load().revision == 3

    multi = _setup_project(tmp_path / "bindings", schema=2)
    multi_context = _revision(multi, limit=20)
    proposal = _create_revision_proposal(
        multi, [clip.to_dict() for clip in multi_context.base_clips]
    )
    root = Path(multi["project_path"])
    proposal_path = root / "proposals" / f"{proposal.proposal.proposal_id}.json"
    data = json.loads(proposal_path.read_text(encoding="utf-8"))
    data["source_bindings"] = list(reversed(data["source_bindings"]))
    proposal_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ProjectError, match="bindings"):
        confirm_multi_source_edit_proposal(
            root, proposal.proposal.proposal_id, expected_revision=3
        )
    assert ProjectStore(root).load().revision == 3

    cross = _setup_project(tmp_path / "schema", schema=2)
    root = Path(cross["project_path"])
    current = ProjectStore(root).load()
    ordinary = read_agent_context(
        root,
        source_id="src_a",
        transcript_version_id="tr_a",
        brief_id=str(cross["brief_id"]),
        expected_revision=3,
        offset=0,
        limit=1,
    )
    forged = EditProposal(
        proposal_id="proposal_forged_schema_one",
        base_project_revision=3,
        base_edit_version_id=current.active_edit_version_id,
        source_id="src_a",
        transcript_version_id="tr_a",
        brief_snapshot=ordinary.brief,
        context_hash=ordinary.context_hash,
        clips=(EditClip.from_dict(_clip("src_a", 1, "clip_forged")),),
        total_duration_ticks=120_000,
        created_at="fixture",
    )
    write_new_json(
        root / "proposals" / f"{forged.proposal_id}.json", forged.to_dict()
    )
    with pytest.raises(ProjectError, match="schema"):
        confirm_edit_proposal(root, forged.proposal_id, expected_revision=3)


def test_compression_diff_is_deterministic_precise_and_read_only(tmp_path: Path) -> None:
    state = _setup_project(tmp_path, schema=2)
    context = _revision(state, limit=20)
    clips = [context.base_clips[0].to_dict(), context.base_clips[2].to_dict()]
    clips[0] = {
        **clips[0],
        "reason": "压缩开场",
    }
    root = Path(state["project_path"])
    before_project = ProjectStore(root).load()
    before_files = sorted(path.name for path in (root / "proposals").glob("*.json"))
    proposal = _create_revision_proposal(state, clips)
    after_create = ProjectStore(root).load()

    first = read_proposal_diff(
        root, proposal.proposal.proposal_id, expected_revision=3
    )
    second = read_proposal_diff(
        root, proposal.proposal.proposal_id, expected_revision=3
    )
    diff = first.proposal_diff
    assert after_create == before_project
    assert after_create.edit_redo_stack == before_project.edit_redo_stack
    assert sorted(path.name for path in (root / "proposals").glob("*.json")) == sorted(
        [*before_files, f"{proposal.proposal.proposal_id}.json"]
    )
    assert first == second
    assert json.dumps(first.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")) == json.dumps(
        second.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    assert diff.before_clip_count == 3
    assert diff.after_clip_count == 2
    assert diff.before_total_duration_ticks == 360_000
    assert diff.after_total_duration_ticks == 240_000
    assert diff.duration_delta_ticks == -120_000
    assert diff.added == ()
    assert [clip.clip_id for clip in diff.removed] == ["clip_b1"]
    assert [change.clip_id for change in diff.changed] == ["clip_a1"]
    assert diff.changed[0].changed_fields == ("reason",)
    assert diff.order_changed is False
    assert diff.before_order == ("clip_a1", "clip_b1", "clip_a2")
    assert diff.after_order == ("clip_a1", "clip_a2")
    serialized = json.dumps(diff.to_dict(), ensure_ascii=False)
    assert "/private/" not in serialized
    assert "locator" not in serialized
    assert "fingerprint" not in serialized
    assert ProjectStore(root).load() == before_project


def test_recovery_and_reorganization_diff_semantics(tmp_path: Path) -> None:
    recovery = _setup_project(tmp_path / "recover", schema=1)
    base = _revision(recovery, limit=20)
    recovered = [clip.to_dict() for clip in base.base_clips]
    recovered.insert(1, _clip("src_a", 3, "clip_new_segment"))
    proposal = _create_revision_proposal(recovery, recovered)
    diff = read_proposal_diff(
        Path(recovery["project_path"]), proposal.proposal.proposal_id, expected_revision=3
    ).proposal_diff
    assert [clip.clip_id for clip in diff.added] == ["clip_new_segment"]
    assert diff.removed == ()
    assert diff.order_changed is False

    reorganize = _setup_project(tmp_path / "reorder", schema=2)
    base = _revision(reorganize, limit=20)
    reordered = [
        base.base_clips[2].to_dict(),
        base.base_clips[1].to_dict(),
        base.base_clips[0].to_dict(),
    ]
    proposal = _create_revision_proposal(reorganize, reordered)
    diff = read_proposal_diff(
        Path(reorganize["project_path"]), proposal.proposal.proposal_id, expected_revision=3
    ).proposal_diff
    assert diff.order_changed is True
    assert [clip.source_id for clip in proposal.proposal.clips] == ["src_a", "src_b", "src_a"]

    locked = _setup_project(tmp_path / "locked", schema=2, allow_reorder=False)
    base = _revision(locked, limit=20)
    forbidden = [base.base_clips[0].to_dict(), base.base_clips[2].to_dict(), base.base_clips[1].to_dict()]
    with pytest.raises(ProjectError, match="reorder"):
        _create_revision_proposal(locked, forbidden)


@pytest.mark.parametrize(
    "mutation",
    ["brief", "person", "speaker_map", "metadata", "transcript", "active_edit"],
)
def test_project_mutations_make_revision_proposal_diff_and_confirm_stale(
    tmp_path: Path, mutation: str
) -> None:
    state = _setup_project(tmp_path, schema=1)
    context = _revision(state, limit=20)
    proposal = _create_revision_proposal(
        state, [clip.to_dict() for clip in context.base_clips]
    )
    root = Path(state["project_path"])
    if mutation == "brief":
        create_edit_brief(
            root,
            theme="changed",
            target_duration_ticks=600_000,
            focus=["changed"],
            allow_reorder=True,
            expected_revision=3,
        )
    elif mutation == "person":
        create_person(
            root,
            name="新增人物",
            role="guest",
            note="",
            expected_revision=3,
        )
    elif mutation == "metadata":
        update_source_metadata(
            root,
            source_id="src_a",
            tags=["changed"],
            note="changed",
            expected_revision=3,
        )
    elif mutation == "speaker_map":
        person = create_person(
            root,
            name="重映射人物",
            role="guest",
            note="",
            expected_revision=3,
        )
        confirm_speaker_map(
            root,
            source_id="src_a",
            transcript_version_id="tr_a",
            local_speaker_id="spk_0",
            person_id=person.state.persons[-1].person_id,
            confirmed_by_user=True,
            expected_revision=person.state.project_revision,
        )
    elif mutation == "transcript":
        alternate = replace(
            _transcript("src_a", "tr_a"),
            transcript_version_id="tr_a_new",
            parent_version_id="tr_a",
        )
        write_new_json(
            root / "transcripts" / "src_a" / "tr_a_new.json",
            alternate.to_dict(),
        )
        project = ProjectStore(root).load()
        ProjectStore(root).save(
            replace(
                project,
                revision=project.revision + 1,
                active_transcript_versions={
                    **project.active_transcript_versions,
                    "src_a": "tr_a_new",
                },
            ),
            expected_revision=project.revision,
        )
    else:
        decision = state["decision"]
        assert decision is not None
        change_edit(
            root,
            operation={"type": "trim", "clip_id": "clip_a1", "source_in_ticks": 1, "source_out_ticks": 120_000},
            expected_revision=3,
            base_edit_version_id=decision.edit_version_id,
        )
    current = ProjectStore(root).load()

    with pytest.raises(ProjectError, match="stale"):
        read_proposal_diff(root, proposal.proposal.proposal_id, expected_revision=current.revision)
    with pytest.raises(ProjectError, match="stale"):
        confirm_edit_proposal(
            root, proposal.proposal.proposal_id, expected_revision=current.revision
        )


def test_revision_proposal_preserves_redo_until_confirmation(tmp_path: Path) -> None:
    state = _setup_project(tmp_path, schema=1)
    root = Path(state["project_path"])
    decision = state["decision"]
    assert decision is not None
    changed = change_edit(
        root,
        operation={
            "type": "trim",
            "clip_id": "clip_a1",
            "source_in_ticks": 10_000,
            "source_out_ticks": 120_000,
        },
        expected_revision=3,
        base_edit_version_id=decision.edit_version_id,
    )
    undo_edit(
        root,
        expected_revision=4,
        base_edit_version_id=changed.decision.edit_version_id,
    )
    state["revision"] = 5
    before = ProjectStore(root).load()
    assert before.edit_redo_stack == (changed.decision.edit_version_id,)
    context = _revision(state, limit=20)
    proposal = _create_revision_proposal(
        state, [clip.to_dict() for clip in context.base_clips]
    )
    after_create = ProjectStore(root).load()
    assert after_create == before

    confirm_edit_proposal(root, proposal.proposal.proposal_id, expected_revision=5)
    confirmed = ProjectStore(root).load()
    assert confirmed.revision == 6
    assert confirmed.edit_redo_stack == ()
    assert (root / "edits" / f"{changed.decision.edit_version_id}.json").is_file()


def test_proposal_write_and_diff_read_failures_do_not_change_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _setup_project(tmp_path, schema=1)
    root = Path(state["project_path"])
    before = ProjectStore(root).load()
    context = _revision(state, limit=20)

    def fail_write(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected proposal write failure")

    monkeypatch.setattr(proposal_module, "write_new_json", fail_write)
    with pytest.raises(OSError, match="injected"):
        _create_revision_proposal(state, [clip.to_dict() for clip in context.base_clips])
    assert ProjectStore(root).load() == before
    monkeypatch.undo()

    proposal = _create_revision_proposal(
        state, [clip.to_dict() for clip in context.base_clips]
    )
    proposal_path = root / "proposals" / f"{proposal.proposal.proposal_id}.json"
    proposal_path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ProjectError):
        read_proposal_diff(root, proposal.proposal.proposal_id, expected_revision=3)
    assert ProjectStore(root).load() == before


def test_confirmed_revision_proposal_becomes_next_revision_context_base(
    tmp_path: Path,
) -> None:
    state = _setup_project(tmp_path, schema=2)
    context = _revision(state, limit=20)
    clips = [clip.to_dict() for clip in context.base_clips]
    clips.append(_clip("src_b", 2, "clip_b2_new"))
    proposal = _create_revision_proposal(state, clips)
    confirmed = confirm_multi_source_edit_proposal(
        Path(state["project_path"]), proposal.proposal.proposal_id, expected_revision=3
    )

    next_context = read_revision_context(
        Path(state["project_path"]), expected_revision=4, offset=0, limit=20
    )
    assert next_context.base_edit_version_id == confirmed.decision.edit_version_id
    assert [clip.clip_id for clip in next_context.base_clips] == [
        "clip_a1",
        "clip_b1",
        "clip_a2",
        "clip_b2_new",
    ]
    assert ProjectStore(Path(state["project_path"])).load().edit_redo_stack == ()


def test_actual_cli_and_stdio_mcp_revision_context_and_diff_are_equivalent(
    tmp_path: Path,
) -> None:
    assert TOOL_SCHEMA_VERSION == 32
    state = _setup_project(tmp_path, schema=2)
    root = Path(state["project_path"])
    cli_context = subprocess.run(
        [
            sys.executable,
            "-m",
            "roughcut.cli",
            "revision-context",
            "--project",
            str(root),
            "--expected-revision",
            "3",
            "--offset",
            "0",
            "--limit",
            "3",
            "--json",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert cli_context.returncode == 0, cli_context.stderr
    assert cli_context.stderr == ""
    context_payload = json.loads(cli_context.stdout)
    mcp_context = _stdio_mcp(
        "revision_context",
        {
            "project_path": str(root),
            "expected_revision": 3,
            "offset": 0,
            "limit": 3,
        },
    )
    assert context_payload == mcp_context

    context = _revision(state, limit=20)
    clips = [clip.to_dict() for clip in context.base_clips[:2]]
    proposal = _create_revision_proposal(state, clips)
    cli_diff = subprocess.run(
        [
            sys.executable,
            "-m",
            "roughcut.cli",
            "proposal-diff-read",
            "--project",
            str(root),
            "--proposal-id",
            proposal.proposal.proposal_id,
            "--expected-revision",
            "3",
            "--json",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert cli_diff.returncode == 0, cli_diff.stderr
    assert cli_diff.stderr == ""
    diff_payload = json.loads(cli_diff.stdout)
    repeated_cli_diff = subprocess.run(
        cli_diff.args,
        text=True,
        capture_output=True,
        check=False,
    )
    assert repeated_cli_diff.returncode == 0
    assert repeated_cli_diff.stdout == cli_diff.stdout
    mcp_diff = _stdio_mcp(
        "proposal_diff_read",
        {
            "project_path": str(root),
            "proposal_id": proposal.proposal.proposal_id,
            "expected_revision": 3,
        },
    )
    assert diff_payload == mcp_diff


@pytest.mark.parametrize(
    ("command", "tool_name"),
    [
        ("revision-context", "revision_context"),
        ("proposal-diff-read", "proposal_diff_read"),
    ],
)
def test_new_cli_and_mcp_missing_arguments_share_versioned_error(
    command: str, tool_name: str
) -> None:
    cli = subprocess.run(
        [sys.executable, "-m", "roughcut.cli", command, "--json"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert cli.returncode == 2
    assert cli.stderr == ""
    cli_payload = json.loads(cli.stdout)
    mcp_payload = _stdio_mcp(tool_name, {})
    assert cli_payload["error"] == mcp_payload["error"] == {"code": "invalid_arguments"}
    assert cli_payload["tool_schema_version"] == mcp_payload["tool_schema_version"] == 32


def test_actual_cli_and_mcp_domain_rejection_codes_match(tmp_path: Path) -> None:
    state = _setup_project(tmp_path, schema=1, confirm=False)
    root = Path(state["project_path"])
    cli = subprocess.run(
        [
            sys.executable,
            "-m",
            "roughcut.cli",
            "revision-context",
            "--project",
            str(root),
            "--expected-revision",
            "2",
            "--offset",
            "0",
            "--limit",
            "2",
            "--json",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert cli.returncode == 2
    assert cli.stderr == ""
    mcp = _stdio_mcp(
        "revision_context",
        {
            "project_path": str(root),
            "expected_revision": 2,
            "offset": 0,
            "limit": 2,
        },
    )
    assert json.loads(cli.stdout)["error"] == mcp["error"] == {
        "code": "revision_context_failed"
    }

    bad_type = subprocess.run(
        [
            sys.executable,
            "-m",
            "roughcut.cli",
            "revision-context",
            "--project",
            str(root),
            "--expected-revision",
            "not-an-integer",
            "--json",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    typed_mcp = _stdio_mcp(
        "revision_context",
        {
            "project_path": str(root),
            "expected_revision": True,
            "offset": 0,
            "limit": 2,
        },
    )
    assert bad_type.returncode == 2
    assert bad_type.stderr == ""
    assert json.loads(bad_type.stdout)["error"] == typed_mcp["error"] == {
        "code": "invalid_arguments"
    }

    missing_page = subprocess.run(
        [
            sys.executable,
            "-m",
            "roughcut.cli",
            "revision-context",
            "--project",
            str(root),
            "--expected-revision",
            "2",
            "--json",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    missing_page_mcp = _stdio_mcp(
        "revision_context",
        {"project_path": str(root), "expected_revision": 2},
    )
    assert missing_page.returncode == 2
    assert json.loads(missing_page.stdout)["error"] == missing_page_mcp["error"] == {
        "code": "invalid_arguments"
    }
