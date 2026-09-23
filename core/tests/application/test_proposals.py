from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import roughcut.application.workflows as workflows_module
from roughcut.adapters.project_store import ProjectStore
from roughcut.adapters.workflow_store import WorkflowStore
from roughcut.application.agent_context import (
    create_edit_brief,
    read_agent_context,
    read_multi_source_agent_context,
)
from roughcut.application.projects import create_project
from roughcut.application.proposals import (
    confirm_edit_proposal,
    confirm_multi_source_edit_proposal,
    create_edit_proposal,
    create_multi_source_edit_proposal,
    read_decision,
    read_edit_decision,
    read_multi_source_edit_decision,
    reject_edit_proposal,
)
from roughcut.application.workflows import workflow_start
from roughcut.domain.project import (
    ImportMode,
    MediaProbe,
    ProjectError,
    SourceAsset,
    SourceFingerprint,
)
from roughcut.domain.transcript import (
    FineUnit,
    TimedTranscript,
    TranscriptProvenance,
    TranscriptSegment,
)
from roughcut.mcp import handle_request


def _setup_project(
    tmp_path: Path, *, allow_reorder: bool = False, target_duration_ticks: int = 360_000
) -> dict[str, object]:
    project_path = tmp_path / "proposal project"
    project = create_project(project_path, "Proposal")
    source_id = "src_proposal"
    transcript_id = "tr_proposal"
    source = SourceAsset(
        source_id=source_id,
        kind="audio",
        display_name="校园访谈.wav",
        import_mode=ImportMode.LINKED,
        locator={"absolute_path": "/private/user/media/校园访谈.wav"},
        fingerprint=SourceFingerprint(100, 1, "fixture"),
        probe=MediaProbe(
            duration_ticks=1_200_000,
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
    )
    texts = (
        "开场介绍校园。",
        "图书馆适合安静学习。",
        "实验室设备完善。",
        "操场空间很大。",
        "食堂提供丰富选择。",
        "宿舍环境整洁。",
        "社团活动很丰富。",
        "老师介绍课程安排。",
        "学生分享学习体验。",
        "最后总结探校体验。",
    )
    transcript = TimedTranscript(
        schema_version=1,
        transcript_version_id=transcript_id,
        source_id=source_id,
        parent_version_id=None,
        provenance=TranscriptProvenance(
            backend="funasr",
            package_version="1.3.8",
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
                segment_id=f"seg_{index:06d}",
                start_ticks=(index - 1) * 120_000,
                end_ticks=index * 120_000,
                original_text=text,
                corrected_text=None,
                local_speaker_id=None,
                person_id=None,
                confidence=None,
                fine_units=(),
                editorial_mark="unmarked",
            )
            for index, text in enumerate(texts, start=1)
        ),
    )
    transcript_path = project_path / "transcripts" / source_id / f"{transcript_id}.json"
    transcript_path.parent.mkdir(parents=True)
    transcript_path.write_text(
        json.dumps(transcript.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    imported = replace(
        project,
        revision=1,
        sources=(source,),
        active_transcript_versions={source_id: transcript_id},
    )
    ProjectStore(project_path).save(imported, expected_revision=0)
    brief_state = create_edit_brief(
        project_path,
        theme="突出学习环境",
        target_duration_ticks=target_duration_ticks,
        focus=["图书馆", "实验室"],
        allow_reorder=allow_reorder,
        expected_revision=1,
    )
    context = read_agent_context(
        project_path,
        source_id=source_id,
        transcript_version_id=transcript_id,
        brief_id=brief_state.brief.brief_id,
        expected_revision=brief_state.project_revision,
        offset=0,
        limit=3,
    )
    return {
        "project_path": project_path,
        "source_id": source_id,
        "transcript_id": transcript_id,
        "brief_id": brief_state.brief.brief_id,
        "revision": brief_state.project_revision,
        "context_hash": context.context_hash,
    }


def _clips(state: dict[str, object]) -> list[dict[str, object]]:
    source_id = str(state["source_id"])
    transcript_id = str(state["transcript_id"])
    return [
        {
            "clip_id": "clip_library",
            "source_id": source_id,
            "transcript_version_id": transcript_id,
            "segment_id": "seg_000002",
            "source_in_ticks": 120_000,
            "source_out_ticks": 240_000,
            "reason": "突出图书馆学习环境",
            "display_text": "图书馆适合安静学习。",
        },
        {
            "clip_id": "clip_lab",
            "source_id": source_id,
            "transcript_version_id": transcript_id,
            "segment_id": "seg_000003",
            "source_in_ticks": 240_000,
            "source_out_ticks": 360_000,
            "reason": "补充实验室条件",
            "display_text": "实验室设备完善。",
        },
    ]


def _create(state: dict[str, object], **overrides: object):
    arguments: dict[str, object] = {
        "source_id": state["source_id"],
        "transcript_version_id": state["transcript_id"],
        "brief_id": state["brief_id"],
        "context_hash": state["context_hash"],
        "clips": _clips(state),
        "total_duration_ticks": 240_000,
        "expected_revision": state["revision"],
        **overrides,
    }
    return create_edit_proposal(Path(state["project_path"]), **arguments)  # type: ignore[arg-type]


def test_proposal_is_a_complete_immutable_snapshot_without_project_mutation(
    tmp_path: Path,
) -> None:
    state = _setup_project(tmp_path)
    before = ProjectStore(Path(state["project_path"])).load()

    proposal_state = _create(state)

    after = ProjectStore(Path(state["project_path"])).load()
    assert after == before
    proposal = proposal_state.proposal
    assert proposal_state.project_revision == before.revision
    assert proposal.base_project_revision == before.revision
    assert proposal.base_edit_version_id is None
    assert proposal.brief_snapshot.brief_id == state["brief_id"]
    assert proposal.context_hash == state["context_hash"]
    assert proposal.total_duration_ticks == 240_000
    assert [clip.clip_id for clip in proposal.clips] == ["clip_library", "clip_lab"]
    artifact = Path(state["project_path"]) / "proposals" / f"{proposal.proposal_id}.json"
    assert json.loads(artifact.read_text(encoding="utf-8")) == proposal.to_dict()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_id", "src_unknown"),
        ("transcript_version_id", "tr_unknown"),
        ("segment_id", "seg_unknown"),
        ("source_in_ticks", 250_000),
        ("source_out_ticks", 120_000),
        ("source_in_ticks", 119_999),
        ("source_out_ticks", 240_001),
        ("display_text", "不存在的台词"),
        ("reason", " "),
        ("clip_id", "../unsafe"),
    ],
)
def test_proposal_rejects_invalid_clip_references_and_content(
    tmp_path: Path, field: str, value: object
) -> None:
    state = _setup_project(tmp_path)
    clips = _clips(state)
    clips[0] = {**clips[0], field: value}

    with pytest.raises(ProjectError):
        _create(state, clips=clips)

    assert not (Path(state["project_path"]) / "proposals").exists()
    assert ProjectStore(Path(state["project_path"])).load().revision == state["revision"]


def test_new_proposal_requires_core_derived_canonical_text(tmp_path: Path) -> None:
    state = _setup_project(tmp_path)
    clips = _clips(state)

    shortened_display = [{**clips[0], "display_text": "图书馆"}, clips[1]]
    with pytest.raises(ProjectError, match="canonical text"):
        _create(state, clips=shortened_display)

    partial_without_units = [
        {
            **clips[0],
            "source_in_ticks": 140_000,
            "source_out_ticks": 220_000,
            "display_text": "适合安静",
        },
        clips[1],
    ]
    with pytest.raises(ProjectError, match="canonical text"):
        _create(state, clips=partial_without_units, total_duration_ticks=200_000)

    fallback_full_text = [
        {
            **partial_without_units[0],
            "display_text": "图书馆适合安静学习。",
        },
        clips[1],
    ]
    created = _create(state, clips=fallback_full_text, total_duration_ticks=200_000)
    assert created.proposal.clips[0].display_text == "图书馆适合安静学习。"


def test_public_single_source_proposal_rejects_agent_editorial_punctuation(
    tmp_path: Path,
) -> None:
    state = _setup_project(tmp_path)
    clips = _clips(state)
    clips[0] = {**clips[0], "display_text": "图书馆适合安静学习！"}

    with pytest.raises(ProjectError, match="canonical text"):
        _create(state, clips=clips)

    assert not (Path(state["project_path"]) / "proposals").exists()


def test_public_multi_source_proposal_rejects_agent_editorial_punctuation(
    tmp_path: Path,
) -> None:
    state = _setup_project(tmp_path)
    root = Path(state["project_path"])
    project = ProjectStore(root).load()
    source_a = project.sources[0]
    source_b = replace(
        source_a,
        source_id="src_proposal_b",
        display_name="第二素材.wav",
        locator={"absolute_path": "/private/user/media/第二素材.wav"},
        fingerprint=SourceFingerprint(101, 2, "fixture-b"),
    )
    transcript_path = root / "transcripts" / str(state["source_id"]) / f"{state['transcript_id']}.json"
    transcript = TimedTranscript.from_dict(
        json.loads(transcript_path.read_text(encoding="utf-8"))
    )
    transcript_b = replace(
        transcript,
        source_id=source_b.source_id,
        transcript_version_id="tr_proposal_b",
        segments=tuple(
            replace(segment, segment_id=f"b_{segment.segment_id}")
            for segment in transcript.segments
        ),
    )
    target_transcript_path = root / "transcripts" / source_b.source_id / "tr_proposal_b.json"
    target_transcript_path.parent.mkdir(parents=True)
    target_transcript_path.write_text(
        json.dumps(transcript_b.to_dict(), ensure_ascii=False),
        encoding="utf-8",
    )
    imported = replace(
        project,
        revision=project.revision + 1,
        sources=(*project.sources, source_b),
        active_transcript_versions={
            **project.active_transcript_versions,
            source_b.source_id: transcript_b.transcript_version_id,
        },
    )
    ProjectStore(root).save(imported, expected_revision=project.revision)
    brief = create_edit_brief(
        root,
        theme="跨素材 Proposal 标点边界",
        target_duration_ticks=360_000,
        focus=["验证公共输入"],
        allow_reorder=False,
        expected_revision=imported.revision,
    )
    bindings = [
        {"source_id": str(state["source_id"]), "transcript_version_id": str(state["transcript_id"])},
        {"source_id": source_b.source_id, "transcript_version_id": transcript_b.transcript_version_id},
    ]
    context = read_multi_source_agent_context(
        root,
        source_bindings=bindings,
        brief_id=brief.brief.brief_id,
        expected_revision=brief.project_revision,
        offset=0,
        limit=3,
    )
    clips = [
        {
            "clip_id": "clip_public_a",
            "source_id": str(state["source_id"]),
            "transcript_version_id": str(state["transcript_id"]),
            "segment_id": "seg_000002",
            "source_in_ticks": 120_000,
            "source_out_ticks": 240_000,
            "reason": "公共单源标点不得自填",
            "display_text": "图书馆适合安静学习！",
        },
        {
            "clip_id": "clip_public_b",
            "source_id": source_b.source_id,
            "transcript_version_id": transcript_b.transcript_version_id,
            "segment_id": "b_seg_000003",
            "source_in_ticks": 240_000,
            "source_out_ticks": 360_000,
            "reason": "跨素材公共输入",
            "display_text": "实验室设备完善。",
        },
    ]

    with pytest.raises(ProjectError, match="canonical text"):
        create_multi_source_edit_proposal(
            root,
            source_bindings=bindings,
            brief_id=brief.brief.brief_id,
            context_hash=context.context_hash,
            clips=clips,
            total_duration_ticks=240_000,
            expected_revision=brief.project_revision,
        )

    canonical_clips = [
        {**clips[0], "display_text": "图书馆适合安静学习。"},
        clips[1],
    ]
    created = create_multi_source_edit_proposal(
        root,
        source_bindings=bindings,
        brief_id=brief.brief.brief_id,
        context_hash=context.context_hash,
        clips=canonical_clips,
        total_duration_ticks=240_000,
        expected_revision=brief.project_revision,
    )
    proposal_path = root / "proposals" / f"{created.proposal.proposal_id}.json"
    payload = json.loads(proposal_path.read_text(encoding="utf-8"))
    payload["clips"][0]["display_text"] = "《图书馆》适合安静学习！！！"
    payload["clips"][0]["reason"] = "Content Draft source excerpt"
    proposal_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with pytest.raises(ProjectError, match="canonical text"):
        confirm_multi_source_edit_proposal(
            root,
            created.proposal.proposal_id,
            expected_revision=brief.project_revision,
        )
    assert not (root / "edits").exists() or not any((root / "edits").iterdir())
    assert ProjectStore(root).load().revision == brief.project_revision


def test_proposal_confirmation_does_not_trust_caller_controlled_editorial_display(
    tmp_path: Path,
) -> None:
    state = _setup_project(tmp_path)
    created = _create(state)
    root = Path(state["project_path"])
    artifact_path = root / "proposals" / f"{created.proposal.proposal_id}.json"
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    payload["clips"][0]["display_text"] = "《图书馆》适合安静学习！！！"
    payload["clips"][0]["reason"] = "Content Draft source excerpt"
    artifact_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with pytest.raises(ProjectError, match="canonical text"):
        confirm_edit_proposal(
            root,
            created.proposal.proposal_id,
            expected_revision=int(state["revision"]),
        )

    assert not (root / "edits").exists() or not any((root / "edits").iterdir())
    assert ProjectStore(root).load().revision == int(state["revision"])


def test_new_proposal_accepts_only_contiguous_trusted_fine_unit_substring(
    tmp_path: Path,
) -> None:
    state = _setup_project(tmp_path)
    transcript_path = (
        Path(state["project_path"])
        / "transcripts"
        / str(state["source_id"])
        / f"{state['transcript_id']}.json"
    )
    transcript = TimedTranscript.from_dict(json.loads(transcript_path.read_text(encoding="utf-8")))
    segment = transcript.segments[1]
    fine_units = (
        FineUnit("token", "图书馆", 120_000, 150_000, None),
        FineUnit("token", "适合", 150_000, 180_000, None),
        FineUnit("token", "安静", 180_000, 210_000, None),
        FineUnit("token", "学习。", 210_000, 240_000, None),
    )
    updated = replace(
        transcript,
        segments=(
            transcript.segments[0],
            replace(segment, fine_units=fine_units),
            *transcript.segments[2:],
        ),
    )
    transcript_path.write_text(
        json.dumps(updated.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    context = read_agent_context(
        Path(state["project_path"]),
        source_id=str(state["source_id"]),
        transcript_version_id=str(state["transcript_id"]),
        brief_id=str(state["brief_id"]),
        expected_revision=int(state["revision"]),
        offset=0,
        limit=3,
    )
    clips = _clips(state)
    clips[0] = {
        **clips[0],
        "source_in_ticks": 150_000,
        "source_out_ticks": 210_000,
        "display_text": "适合安静",
    }
    created = _create(
        {**state, "context_hash": context.context_hash},
        clips=clips,
        total_duration_ticks=180_000,
    )
    assert created.proposal.clips[0].display_text == "适合安静"

    skipped_middle = [{**clips[0], "display_text": "适合学习。"}, clips[1]]
    with pytest.raises(ProjectError, match="canonical text"):
        _create(
            {**state, "context_hash": context.context_hash},
            clips=skipped_middle,
            total_duration_ticks=180_000,
        )


def test_proposal_rejects_duplicate_clip_ids_and_invalid_total(tmp_path: Path) -> None:
    state = _setup_project(tmp_path)
    duplicate = _clips(state)
    duplicate[1] = {**duplicate[1], "clip_id": duplicate[0]["clip_id"]}
    for clips, total in ((duplicate, 240_000), (_clips(state), 0), (_clips(state), 239_999)):
        with pytest.raises(ProjectError):
            _create(state, clips=clips, total_duration_ticks=total)
    assert not (Path(state["project_path"]) / "proposals").exists()


def test_proposal_rejects_empty_and_unreasonably_long_snapshots(tmp_path: Path) -> None:
    state = _setup_project(tmp_path, target_duration_ticks=120_000)
    with pytest.raises(ProjectError):
        _create(state, clips=[], total_duration_ticks=0)

    clips = []
    for index in range(1, 8):
        clips.append(
            {
                "clip_id": f"clip_{index}",
                "source_id": state["source_id"],
                "transcript_version_id": state["transcript_id"],
                "segment_id": f"seg_{index:06d}",
                "source_in_ticks": (index - 1) * 120_000,
                "source_out_ticks": index * 120_000,
                "reason": "候选片段",
                "display_text": (
                    "开场介绍校园。",
                    "图书馆适合安静学习。",
                    "实验室设备完善。",
                    "操场空间很大。",
                    "食堂提供丰富选择。",
                    "宿舍环境整洁。",
                    "社团活动很丰富。",
                )[index - 1],
            }
        )
    with pytest.raises(ProjectError, match="target duration"):
        _create(state, clips=clips, total_duration_ticks=840_000)


def test_proposal_enforces_brief_reorder_permission(tmp_path: Path) -> None:
    state = _setup_project(tmp_path)
    reordered = list(reversed(_clips(state)))
    with pytest.raises(ProjectError, match="reorder"):
        _create(state, clips=reordered)

    allowed = _setup_project(tmp_path / "allowed", allow_reorder=True)
    proposal = _create(allowed, clips=list(reversed(_clips(allowed))))
    assert [clip.segment_id for clip in proposal.proposal.clips] == [
        "seg_000003",
        "seg_000002",
    ]


def test_proposal_rejects_stale_revision_brief_transcript_and_context_hash(
    tmp_path: Path,
) -> None:
    state = _setup_project(tmp_path)
    with pytest.raises(ProjectError, match="revision conflict"):
        _create(state, expected_revision=int(state["revision"]) - 1)
    with pytest.raises(ProjectError):
        _create(state, brief_id="brief_unknown")
    with pytest.raises(ProjectError):
        _create(state, transcript_version_id="tr_unknown")
    with pytest.raises(ProjectError, match="context hash"):
        _create(state, context_hash="0" * 64)
    assert not (Path(state["project_path"]) / "proposals").exists()


def test_explicit_confirmation_creates_one_immutable_decision(tmp_path: Path) -> None:
    state = _setup_project(tmp_path)
    proposal = _create(state)
    rejected = reject_edit_proposal(
        Path(state["project_path"]),
        proposal.proposal.proposal_id,
        expected_revision=int(state["revision"]),
    )
    assert rejected.status == "rejected"
    assert rejected.project_revision == state["revision"]
    assert not (Path(state["project_path"]) / "edits").exists()

    decision_state = confirm_edit_proposal(
        Path(state["project_path"]),
        proposal.proposal.proposal_id,
        expected_revision=int(state["revision"]),
    )
    project = ProjectStore(Path(state["project_path"])).load()
    assert project.revision == int(state["revision"]) + 1
    assert project.active_edit_version_id == decision_state.decision.edit_version_id
    assert decision_state.project_revision == project.revision
    assert decision_state.decision.created_by == "user"
    assert decision_state.decision.proposal_snapshot == proposal.proposal
    assert (
        read_edit_decision(Path(state["project_path"]), decision_state.decision.edit_version_id)
        == decision_state
    )

    with pytest.raises(ProjectError):
        confirm_edit_proposal(
            Path(state["project_path"]),
            proposal.proposal.proposal_id,
            expected_revision=project.revision,
        )
    assert ProjectStore(Path(state["project_path"])).load() == project
    assert len(list((Path(state["project_path"]) / "edits").glob("*.json"))) == 1


def test_stale_or_failed_confirmation_leaves_no_decision(tmp_path: Path, monkeypatch) -> None:
    state = _setup_project(tmp_path)
    proposal = _create(state)
    project_path = Path(state["project_path"])
    brief = create_edit_brief(
        project_path,
        theme="更新主题",
        target_duration_ticks=360_000,
        focus=["操场"],
        allow_reorder=False,
        expected_revision=int(state["revision"]),
    )
    with pytest.raises(ProjectError):
        confirm_edit_proposal(
            project_path,
            proposal.proposal.proposal_id,
            expected_revision=brief.project_revision,
        )
    assert not (project_path / "edits").exists()

    fresh_state = _setup_project(tmp_path / "save-failure")
    fresh = _create(fresh_state)
    fresh_path = Path(fresh_state["project_path"])
    original_save = ProjectStore.save

    def fail_save(self, project, *, expected_revision):  # type: ignore[no-untyped-def]
        if project.active_edit_version_id is not None:
            raise OSError("injected project save failure")
        return original_save(self, project, expected_revision=expected_revision)

    monkeypatch.setattr(ProjectStore, "save", fail_save)
    with pytest.raises(OSError, match="injected"):
        confirm_edit_proposal(
            fresh_path,
            fresh.proposal.proposal_id,
            expected_revision=int(fresh_state["revision"]),
        )
    assert ProjectStore(fresh_path).load().revision == fresh_state["revision"]
    assert list((fresh_path / "edits").glob("*.json")) == []


def test_confirmation_never_overwrites_an_existing_decision_artifact(tmp_path: Path) -> None:
    state = _setup_project(tmp_path)
    proposal = _create(state).proposal
    project_path = Path(state["project_path"])
    edit_id = f"edit_{proposal.proposal_id.removeprefix('proposal_')}"
    decision_path = project_path / "edits" / f"{edit_id}.json"
    decision_path.parent.mkdir()
    decision_path.write_text('{"sentinel": true}\n', encoding="utf-8")

    with pytest.raises(ProjectError, match="already exists"):
        confirm_edit_proposal(
            project_path,
            proposal.proposal_id,
            expected_revision=int(state["revision"]),
        )

    assert decision_path.read_text(encoding="utf-8") == '{"sentinel": true}\n'
    assert ProjectStore(project_path).load().revision == state["revision"]


def test_fixture_transcript_paged_context_to_confirmed_decision(tmp_path: Path) -> None:
    state = _setup_project(tmp_path)
    pages = [
        read_agent_context(
            Path(state["project_path"]),
            source_id=str(state["source_id"]),
            transcript_version_id=str(state["transcript_id"]),
            brief_id=str(state["brief_id"]),
            expected_revision=int(state["revision"]),
            offset=offset,
            limit=4,
        )
        for offset in (0, 4, 8)
    ]
    assert {page.context_hash for page in pages} == {state["context_hash"]}
    assert [segment["segment_id"] for page in pages for segment in page.segments] == [
        f"seg_{index:06d}" for index in range(1, 11)
    ]

    proposal = _create(state)
    decision = confirm_edit_proposal(
        Path(state["project_path"]),
        proposal.proposal.proposal_id,
        expected_revision=int(state["revision"]),
    )
    assert (
        read_edit_decision(Path(state["project_path"]), decision.decision.edit_version_id)
        == decision
    )


def test_public_proposal_create_and_confirm_require_workflow_but_domain_readback_stays_available(
    tmp_path: Path,
) -> None:
    state = _setup_project(tmp_path)
    project_path = Path(state["project_path"])
    before = ProjectStore(project_path).load()
    cli = subprocess.run(
        [
            sys.executable,
            "-m",
            "roughcut.cli",
            "proposal-create",
            "--project",
            str(state["project_path"]),
            "--source-id",
            str(state["source_id"]),
            "--transcript-id",
            str(state["transcript_id"]),
            "--brief-id",
            str(state["brief_id"]),
            "--context-hash",
            str(state["context_hash"]),
            "--clips-json",
            json.dumps(_clips(state), ensure_ascii=False),
            "--total-duration-ticks",
            "240000",
            "--expected-revision",
            str(state["revision"]),
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert cli.returncode == 2
    assert json.loads(cli.stdout)["error"]["code"] == "workflow_required"
    assert ProjectStore(project_path).load() == before

    confirmed = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "proposal_confirm",
                "arguments": {
                    "project_path": str(state["project_path"]),
                    "proposal_id": "proposal_missing",
                    "expected_revision": state["revision"],
                },
            },
        }
    )
    assert confirmed is not None
    confirmed_result = confirmed["result"]
    assert isinstance(confirmed_result, dict)
    decision_payload = confirmed_result["structuredContent"]
    assert isinstance(decision_payload, dict)
    assert decision_payload["error"]["code"] == "workflow_required"
    assert ProjectStore(project_path).load() == before

    proposal = _create(state).proposal
    decision = confirm_edit_proposal(
        project_path,
        proposal.proposal_id,
        expected_revision=int(state["revision"]),
    )
    decision_payload = {
        "ok": True,
        "decision": decision.decision.to_dict(),
        "project_revision": decision.project_revision,
    }
    edit_version_id = decision.decision.edit_version_id

    readback = subprocess.run(
        [
            sys.executable,
            "-m",
            "roughcut.cli",
            "edit-decision-read",
            "--project",
            str(state["project_path"]),
            "--edit-version-id",
            edit_version_id,
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert readback.returncode == 0, readback.stderr
    readback_payload = json.loads(readback.stdout)
    assert readback_payload["decision"] == decision.decision.to_dict()
    assert readback_payload["project_revision"] == decision.project_revision

    unified = read_decision(project_path, edit_version_id)
    unified_payload = unified.to_dict()
    assert unified_payload == read_decision(project_path, edit_version_id).to_dict()
    assert unified_payload["decision"]["type"] == "edit_decision"  # type: ignore[index]
    assert unified_payload["decision"]["schema_version"] == 1  # type: ignore[index]
    assert unified_payload["decision"]["edit_version_id"] == edit_version_id  # type: ignore[index]
    assert unified_payload["decision"]["payload"] == decision.decision.to_dict()  # type: ignore[index]
    with pytest.raises(ProjectError, match="does not match legacy reader"):
        read_multi_source_edit_decision(project_path, edit_version_id)

    cli_unified = subprocess.run(
        [
            sys.executable,
            "-m",
            "roughcut.cli",
            "decision-read",
            "--project",
            str(project_path),
            "--edit-version-id",
            edit_version_id,
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert cli_unified.returncode == 0, cli_unified.stderr
    assert json.loads(cli_unified.stdout)["decision"] == unified_payload["decision"]

    mcp_read = handle_request(
        {
            "jsonrpc": "2.0",
            "id": "decision-read",
            "method": "tools/call",
            "params": {
                "name": "decision_read",
                "arguments": {
                    "project_path": str(project_path),
                    "edit_version_id": edit_version_id,
                },
            },
        }
    )
    assert mcp_read is not None
    mcp_result = mcp_read["result"]
    assert isinstance(mcp_result, dict)
    assert mcp_result["structuredContent"]["decision"] == unified_payload["decision"]  # type: ignore[index]

    bad_mcp = handle_request(
        {
            "jsonrpc": "2.0",
            "id": "bad-decision-read",
            "method": "tools/call",
            "params": {
                "name": "decision_read",
                "arguments": {
                    "project_path": str(project_path),
                    "edit_version_id": "missing_decision",
                },
            },
        }
    )
    assert bad_mcp is not None
    bad_result = bad_mcp["result"]
    assert isinstance(bad_result, dict)
    assert bad_result["structuredContent"]["error"]["code"] == "decision_read_integrity"  # type: ignore[index]

    corrupt_path = project_path / "edits" / f"{edit_version_id}.json"
    original = corrupt_path.read_bytes()
    try:
        corrupt_path.write_bytes(b"{")
        with pytest.raises((ProjectError, json.JSONDecodeError)):
            read_decision(project_path, edit_version_id)
        corrupt_path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
        with pytest.raises(ProjectError, match="unsupported edit decision schema"):
            read_decision(project_path, edit_version_id)
        corrupt_path.write_text(
            json.dumps({**decision.decision.to_dict(), "edit_version_id": "other"}),
            encoding="utf-8",
        )
        with pytest.raises(ProjectError, match="identity mismatch"):
            read_decision(project_path, edit_version_id)
    finally:
        corrupt_path.write_bytes(original)

    with pytest.raises(ProjectError, match="revision conflict"):
        _create(state, expected_revision=int(state["revision"]))


def test_cli_and_mcp_proposal_reject_are_equivalent_and_non_mutating(tmp_path: Path) -> None:
    state = _setup_project(tmp_path)
    proposal = _create(state).proposal
    project_path = Path(state["project_path"])
    started = workflow_start(project_path, "wfr_proposal_reject", [str(state["source_id"])])
    store = WorkflowStore(project_path)
    run = store.read_run(started.workflow_run.run_id)
    artifacts = dict(run.artifact_refs)
    artifacts["proposal"] = workflows_module._artifact_ref("proposal", proposal)
    current = replace(run, stage="roughcut_review", artifact_refs=artifacts)
    store.write_run(
        current,
        expected_run_hash=workflows_module.canonical_sha256_v1(run.to_dict()),
    )
    before = ProjectStore(project_path).load()
    run_before = store.read_run(current.run_id).to_dict()
    cli = subprocess.run(
        [
            sys.executable,
            "-m",
            "roughcut.cli",
            "proposal-reject",
            "--project",
            str(project_path),
            "--proposal-id",
            proposal.proposal_id,
            "--expected-revision",
            str(state["revision"]),
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert cli.returncode == 0, cli.stderr
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "proposal_reject",
                "arguments": {
                    "project_path": str(project_path),
                    "proposal_id": proposal.proposal_id,
                    "expected_revision": state["revision"],
                },
            },
        }
    )
    assert response is not None
    result = response["result"]
    assert isinstance(result, dict)
    assert result["structuredContent"] == json.loads(cli.stdout)
    assert ProjectStore(project_path).load() == before
    assert store.read_run(current.run_id).to_dict() == run_before
    assert not (project_path / "edits").exists()
