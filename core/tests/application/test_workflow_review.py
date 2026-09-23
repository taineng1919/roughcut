from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from roughcut.adapters.artifact_store import write_new_json
from roughcut.adapters.project_store import ProjectStore
from roughcut.application.agent_context import create_edit_brief, read_multi_source_agent_context
from roughcut.application.content_drafts import (
    confirm_content_draft,
    create_content_draft,
    propose_content_draft,
)
from roughcut.application.preview import ReviewPlaybackSelection
from roughcut.application.projects import create_project
from roughcut.application.proposals import (
    confirm_multi_source_edit_proposal,
    create_multi_source_edit_proposal,
)
from roughcut.application.proxies import FrozenProxyPlayback
from roughcut.application.sources import fingerprint_file
from roughcut.application.workflow_review import (
    _proposal_matches_draft,
    load_workflow_review_snapshot,
    workflow_session_status,
)
from roughcut.domain import content_draft
from roughcut.domain.bindings import SourceTranscriptBinding
from roughcut.domain.brief import EditBrief
from roughcut.domain.edit import EditClip, MultiSourceEditProposal
from roughcut.domain.people import Person, SpeakerMap
from roughcut.domain.project import ImportMode, MediaProbe, ProjectError, SourceAsset
from roughcut.domain.transcript import TimedTranscript, TranscriptProvenance, TranscriptSegment


def _source(source_id: str, media: Path) -> SourceAsset:
    return SourceAsset(
        source_id=source_id,
        kind="video",
        display_name=f"素材 {source_id}",
        import_mode=ImportMode.LINKED,
        locator={"absolute_path": str(media.resolve())},
        fingerprint=fingerprint_file(media),
        probe=MediaProbe(
            duration_ticks=240_000,
            container_start_ticks=0,
            first_content_ticks=0,
            video_codec="h264",
            width=320,
            height=180,
            nominal_frame_rate={"numerator": 25, "denominator": 1},
            is_vfr=False,
            audio_codec="aac",
            audio_sample_rate=48_000,
            rotation_degrees=0,
        ),
        tags=(f"tag:{source_id}",),
        note=f"备注 {source_id}",
    )


def _transcript(source_id: str, transcript_id: str) -> TimedTranscript:
    return TimedTranscript(
        schema_version=1,
        transcript_version_id=transcript_id,
        source_id=source_id,
        parent_version_id=None,
        provenance=TranscriptProvenance(
            "fixture",
            "1",
            {},
            {},
            f"raw-asr/{source_id}/private.json",
            "fixture",
            "fixture",
            0,
        ),
        language="zh-CN",
        segments=(
            TranscriptSegment(
                segment_id=f"seg_{source_id}",
                start_ticks=0,
                end_ticks=120_000,
                original_text=f"{source_id} 的内容。",
                corrected_text=None,
                local_speaker_id="spk_0",
                person_id=None,
                confidence=None,
                fine_units=(),
                editorial_mark="unmarked",
            ),
        ),
    )


def _workflow_project(tmp_path: Path) -> tuple[Path, list[dict[str, str]], str]:
    root = tmp_path / "workflow project"
    project = create_project(root, "Workflow Review")
    sources: list[SourceAsset] = []
    active: dict[str, str] = {}
    for source_id in ("src_a", "src_b", "src_c"):
        media = tmp_path / f"{source_id}.mp4"
        media.write_bytes(source_id.encode() * 64)
        transcript_id = f"tr_{source_id[-1]}"
        transcript = _transcript(source_id, transcript_id)
        write_new_json(
            root / "transcripts" / source_id / f"{transcript_id}.json",
            transcript.to_dict(),
        )
        sources.append(_source(source_id, media))
        active[source_id] = transcript_id
    imported = replace(
        project,
        revision=1,
        sources=tuple(sources),
        active_transcript_versions=active,
    )
    ProjectStore(root).save(imported, expected_revision=0)
    brief = create_edit_brief(
        root,
        theme="显式双素材",
        target_duration_ticks=240_000,
        focus=["只使用 A/B"],
        allow_reorder=True,
        expected_revision=1,
    )
    bindings = [
        {"source_id": "src_b", "transcript_version_id": "tr_b"},
        {"source_id": "src_a", "transcript_version_id": "tr_a"},
    ]
    context = read_multi_source_agent_context(
        root,
        source_bindings=bindings,
        brief_id=brief.brief.brief_id,
        expected_revision=2,
        offset=0,
        limit=10,
    )
    return root, bindings, context.context_hash


def test_workflow_snapshot_freezes_exact_ordered_scope_without_path_leaks(
    tmp_path: Path,
) -> None:
    root, bindings, context_hash = _workflow_project(tmp_path)

    snapshot = load_workflow_review_snapshot(root, source_bindings=bindings)
    payload = snapshot.to_dict()

    assert payload["workflow_schema_version"] == 1
    assert payload["review_mode"] == "workflow"
    assert payload["project"]["revision"] == 2
    assert payload["source_bindings"] == bindings
    assert [source["source_id"] for source in payload["sources"]] == ["src_b", "src_a"]
    assert "src_c" not in str(payload)
    assert payload["brief"]["theme"] == "显式双素材"
    assert payload["context_hash"] == context_hash
    assert payload["content_draft"] is None
    serialized = str(payload)
    assert str(root) not in serialized
    assert str(tmp_path) not in serialized
    assert "locator" not in serialized
    assert "raw-asr" not in serialized


def test_workflow_stage_overview_is_derived_and_preparation_allows_unmapped_speakers(
    tmp_path: Path,
) -> None:
    root, bindings, _context_hash = _workflow_project(tmp_path)

    payload = load_workflow_review_snapshot(root, source_bindings=bindings).to_dict()
    stages = payload["workflow_stages"]

    assert [stage["title"] for stage in stages] == [
        "选择素材",
        "准备素材",
        "阅读原始转录稿",
        "确定剪辑要求",
        "调整初稿",
        "确认剪辑方案",
        "调整已确认剪辑",
        "正式导出",
    ]
    assert [stage["status"] for stage in stages[:4]] == [
        "已完成，可直接使用",
        "已完成，可直接使用",
        "待您确认",
        "已完成，可直接使用",
    ]
    preparation = payload["preparation"]
    assert [item["display_name"] for item in preparation["sources"]] == [
        "素材 src_b",
        "素材 src_a",
    ]
    assert all(item["transcript_status"] == "已完成，可直接使用" for item in preparation["sources"])
    assert all(item["playback_status"] == "使用原素材播放" for item in preparation["sources"])
    assert all(item["unmapped_local_speaker_count"] == 1 for item in preparation["sources"])
    assert stages[1]["status"] != "内容已变更，需要重新确认"
    serialized = str(payload)
    assert str(root) not in serialized
    assert str(tmp_path) not in serialized


def test_workflow_stage_overview_derives_proposal_decision_and_render_artifacts(
    tmp_path: Path,
) -> None:
    root, bindings, context_hash = _workflow_project(tmp_path)
    project = ProjectStore(root).load()
    assert project.active_brief_id is not None
    candidate = create_content_draft(
        root,
        source_bindings=bindings,
        parent_draft_id=None,
        brief_id=project.active_brief_id,
        context_hash=context_hash,
        blocks=[
            {
                "block_id": "block_b",
                "kind": "source_excerpt",
                "refs": [
                    {
                        "source_id": "src_b",
                        "transcript_version_id": "tr_b",
                        "segment_id": "seg_src_b",
                        "start_ticks": 0,
                        "end_ticks": 120_000,
                    }
                ],
                "canonical_text": "src_b 的内容。",
            }
        ],
        expected_revision=project.revision,
    )
    confirmed = confirm_content_draft(
        root,
        candidate.content_draft.content_draft_id,
        expected_revision=project.revision,
    )
    proposal = propose_content_draft(
        root,
        confirmed.content_draft.content_draft_id,
        expected_revision=confirmed.project_revision,
    )

    proposal_payload = load_workflow_review_snapshot(root, source_bindings=bindings).to_dict()
    assert proposal_payload["workflow_stages"][4]["status"] == "已完成，可直接使用"
    assert proposal_payload["workflow_stages"][5]["status"] == "待您确认"
    assert proposal_payload["workflow_stages"][6]["status"] == "尚未开始"

    decision = confirm_multi_source_edit_proposal(
        root,
        proposal.proposal.proposal_id,
        expected_revision=confirmed.project_revision,
    )
    decision_payload = load_workflow_review_snapshot(root, source_bindings=bindings).to_dict()
    assert decision_payload["workflow_stages"][4]["status"] == "已完成，可直接使用"
    assert decision_payload["workflow_stages"][5]["status"] == "已完成，可直接使用"
    assert decision_payload["workflow_stages"][6]["status"] == "已完成，可直接使用"
    assert decision_payload["workflow_stages"][7]["status"] == "待您确认"

    write_new_json(
        root / "renders" / "render_fixture.plan.json",
        {"edit_version_id": decision.decision.edit_version_id},
    )
    planned = load_workflow_review_snapshot(root, source_bindings=bindings).to_dict()
    assert planned["workflow_stages"][7]["status"] == "待您确认"

    write_new_json(
        root / "renders" / "render_fixture.manifest.json",
        {
            "edit_version_id": decision.decision.edit_version_id,
            "acceptance": {"accepted": True, "checks": {}},
        },
    )
    rendered = load_workflow_review_snapshot(root, source_bindings=bindings).to_dict()
    assert rendered["workflow_stages"][7]["status"] == "已完成，可直接使用"


def test_historical_technical_edit_and_render_do_not_advance_guided_workflow(
    tmp_path: Path,
) -> None:
    root, bindings, context_hash = _workflow_project(tmp_path)
    project = ProjectStore(root).load()
    assert project.active_brief_id is not None
    proposal = create_multi_source_edit_proposal(
        root,
        source_bindings=bindings,
        brief_id=project.active_brief_id,
        context_hash=context_hash,
        clips=[
            {
                "clip_id": "clip_b",
                "source_id": "src_b",
                "transcript_version_id": "tr_b",
                "segment_id": "seg_src_b",
                "source_in_ticks": 0,
                "source_out_ticks": 120_000,
                "reason": "技术验收",
                "display_text": "src_b 的内容。",
            }
        ],
        total_duration_ticks=120_000,
        expected_revision=project.revision,
    )
    decision = confirm_multi_source_edit_proposal(
        root,
        proposal.proposal.proposal_id,
        expected_revision=project.revision,
    )
    write_new_json(
        root / "renders" / "render_technical.manifest.json",
        {
            "edit_version_id": decision.decision.edit_version_id,
            "acceptance": {"accepted": True, "checks": {}},
        },
    )

    payload = load_workflow_review_snapshot(root, source_bindings=bindings).to_dict()

    assert [stage["status"] for stage in payload["workflow_stages"][4:]] == [
        "尚未开始",
        "尚未开始",
        "尚未开始",
        "尚未开始",
    ]
    assert payload["workflow_stages"][6]["technical_details"] == {
        "active_edit_version_id": decision.decision.edit_version_id,
        "bindings_match": True,
        "current_context": True,
        "guided_workflow_lineage": False,
    }


def test_workflow_stage_overview_marks_stale_confirmed_draft_for_reconfirmation(
    tmp_path: Path,
) -> None:
    root, bindings, context_hash = _workflow_project(tmp_path)
    project = ProjectStore(root).load()
    assert project.active_brief_id is not None
    candidate = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=bindings,
        brief_id=project.active_brief_id,
        context_hash=context_hash,
        blocks=[
            {
                "block_id": "block_b",
                "kind": "source_excerpt",
                "refs": [
                    {
                        "source_id": "src_b",
                        "transcript_version_id": "tr_b",
                        "segment_id": "seg_src_b",
                        "start_ticks": 0,
                        "end_ticks": 120_000,
                    }
                ],
                "canonical_text": "src_b 的内容。",
            }
        ],
        expected_revision=project.revision,
    )
    confirm_content_draft(
        root,
        candidate.content_draft.content_draft_id,
        expected_revision=project.revision,
    )
    store = ProjectStore(root)
    current = store.load()
    changed = replace(
        current,
        revision=current.revision + 1,
        sources=(replace(current.sources[0], note="changed"), *current.sources[1:]),
    )
    store.save(changed, expected_revision=current.revision)

    payload = load_workflow_review_snapshot(root, source_bindings=bindings).to_dict()

    assert payload["workflow_stages"][4]["status"] == "内容已变更，需要重新确认"


def test_workflow_snapshot_exposes_only_safe_frozen_playback_identifiers(
    tmp_path: Path,
) -> None:
    root, bindings, _context_hash = _workflow_project(tmp_path)
    proxy = FrozenProxyPlayback(
        source_id="src_b",
        cache_key="private_cache_key",
        output_size=100,
        output_sha256_head_tail="private_output_hash",
        manifest_size=50,
        manifest_sha256_head_tail="private_manifest_hash",
        profile_summary={"width": 960, "height": 540, "video_codec": "h264"},
    )

    snapshot = load_workflow_review_snapshot(
        root,
        source_bindings=bindings,
        playback_selections=(
            ReviewPlaybackSelection("src_b", "proxy", proxy),
            ReviewPlaybackSelection("src_a", "original"),
        ),
    )
    payload = snapshot.to_dict()

    assert [source["playback_kind"] for source in payload["sources"]] == [
        "proxy",
        "original",
    ]
    assert payload["sources"][0]["proxy_profile"] == {
        "width": 960,
        "height": 540,
        "video_codec": "h264",
    }
    assert payload["preparation"]["sources"][0]["playback_status"] == (
        "代理素材已完成，可直接使用"
    )
    serialized = str(payload)
    assert "private_cache_key" not in serialized
    assert "private_output_hash" not in serialized
    assert "private_manifest_hash" not in serialized


def test_workflow_preparation_counts_only_exact_confirmed_speaker_maps(
    tmp_path: Path,
) -> None:
    root, bindings, _context_hash = _workflow_project(tmp_path)
    store = ProjectStore(root)
    project = store.load()
    person = Person("person_teacher", "赵老师", "老师", "")
    updated = replace(
        project,
        revision=project.revision + 1,
        persons=(person,),
        speaker_maps=(SpeakerMap("src_b", "tr_b", "spk_0", person.person_id, True),),
    )
    store.save(updated, expected_revision=project.revision)

    payload = load_workflow_review_snapshot(root, source_bindings=bindings).to_dict()
    prepared = payload["preparation"]["sources"]

    assert prepared[0]["mapped_person_count"] == 1
    assert prepared[0]["mapped_people"] == ["赵老师"]
    assert prepared[0]["unmapped_local_speaker_count"] == 0
    assert prepared[1]["mapped_person_count"] == 0
    assert prepared[1]["unmapped_local_speaker_count"] == 1


def test_workflow_snapshot_requires_active_bindings_and_matching_explicit_draft(
    tmp_path: Path,
) -> None:
    root, bindings, context_hash = _workflow_project(tmp_path)
    brief_id = ProjectStore(root).load().active_brief_id
    assert brief_id is not None
    draft = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=bindings,
        brief_id=brief_id,
        context_hash=context_hash,
        blocks=[
            {
                "block_id": "block_b",
                "kind": "source_excerpt",
                "refs": [
                    {
                        "source_id": "src_b",
                        "transcript_version_id": "tr_b",
                        "segment_id": "seg_src_b",
                        "start_ticks": 0,
                        "end_ticks": 120_000,
                    }
                ],
                "canonical_text": "src_b 的内容。",
            }
        ],
        expected_revision=2,
    )

    with pytest.raises(ProjectError, match="bindings"):
        load_workflow_review_snapshot(
            root,
            source_bindings=list(reversed(bindings)),
            content_draft_id=draft.content_draft.content_draft_id,
        )
    with pytest.raises(ProjectError, match="active"):
        load_workflow_review_snapshot(
            root,
            source_bindings=[
                {"source_id": "src_b", "transcript_version_id": "tr_wrong"},
                bindings[1],
            ],
        )


def test_workflow_snapshot_without_brief_is_explicitly_readable(tmp_path: Path) -> None:
    root, bindings, _context_hash = _workflow_project(tmp_path)
    store = ProjectStore(root)
    current = store.load()
    store.save(replace(current, revision=3, active_brief_id=None), expected_revision=2)

    snapshot = load_workflow_review_snapshot(root, source_bindings=bindings)

    assert snapshot.project_revision == 3
    assert snapshot.brief is None
    assert snapshot.context_hash is None
    assert "brief_create" in snapshot.allowed_operations
    assert "proposal_diff_read" not in snapshot.allowed_operations


def test_workflow_snapshot_validates_transcript_even_without_brief(tmp_path: Path) -> None:
    root, bindings, _context_hash = _workflow_project(tmp_path)
    store = ProjectStore(root)
    current = store.load()
    store.save(replace(current, revision=3, active_brief_id=None), expected_revision=2)
    (root / "transcripts" / "src_a" / "tr_a.json").unlink()

    with pytest.raises(ProjectError, match="timed transcript is missing or unreadable"):
        load_workflow_review_snapshot(root, source_bindings=bindings)


def test_single_source_workflow_snapshot_does_not_infer_other_sources(tmp_path: Path) -> None:
    root, _bindings, _context_hash = _workflow_project(tmp_path)

    snapshot = load_workflow_review_snapshot(
        root,
        source_bindings=[
            {"source_id": "src_a", "transcript_version_id": "tr_a"}
        ],
    )

    assert [binding.source_id for binding in snapshot.source_bindings] == ["src_a"]
    assert [source["source_id"] for source in snapshot.sources] == ["src_a"]
    assert snapshot.authorized_source_ids == {"src_a"}


def test_workflow_snapshot_reads_matching_active_confirmed_draft_only(tmp_path: Path) -> None:
    root, bindings, context_hash = _workflow_project(tmp_path)
    project = ProjectStore(root).load()
    assert project.active_brief_id is not None
    candidate = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=bindings,
        brief_id=project.active_brief_id,
        context_hash=context_hash,
        blocks=[
            {
                "block_id": "block_b",
                "kind": "source_excerpt",
                "refs": [
                    {
                        "source_id": "src_b",
                        "transcript_version_id": "tr_b",
                        "segment_id": "seg_src_b",
                        "start_ticks": 0,
                        "end_ticks": 120_000,
                    }
                ],
                "canonical_text": "src_b 的内容。",
            }
        ],
        expected_revision=2,
    )
    confirmed = confirm_content_draft(
        root,
        candidate.content_draft.content_draft_id,
        expected_revision=2,
    )

    matching = load_workflow_review_snapshot(root, source_bindings=bindings)
    single = load_workflow_review_snapshot(root, source_bindings=[bindings[1]])

    assert matching.content_draft is not None
    assert (
        matching.content_draft.content_draft.content_draft_id
        == confirmed.content_draft.content_draft_id
    )
    assert matching.content_draft.content_draft.confirmed_by_user is True
    assert "content_draft_propose" in matching.allowed_operations
    assert "content_draft_confirm" not in matching.allowed_operations
    assert single.content_draft is None


@pytest.mark.parametrize(
    ("change", "specific_reason"),
    [
        ("metadata", "context_hash"),
        ("person", "context_hash"),
        ("speaker_map", "context_hash"),
        ("brief", "brief"),
        ("transcript", "active_transcript"),
    ],
)
def test_workflow_session_becomes_read_only_for_external_project_changes(
    tmp_path: Path,
    change: str,
    specific_reason: str,
) -> None:
    case_path = tmp_path / change
    case_path.mkdir()
    root, bindings, _context_hash = _workflow_project(case_path)
    snapshot = load_workflow_review_snapshot(root, source_bindings=bindings)
    store = ProjectStore(root)
    project = store.load()
    updated = project
    if change == "metadata":
        updated = replace(
            project,
            sources=(replace(project.sources[0], note="changed"), *project.sources[1:]),
        )
    elif change == "person":
        updated = replace(
            project,
            persons=(*project.persons, Person("person_new", "新人", "嘉宾", "")),
        )
    elif change == "speaker_map":
        person = Person("person_new", "新人", "嘉宾", "")
        updated = replace(
            project,
            persons=(*project.persons, person),
            speaker_maps=(
                *project.speaker_maps,
                SpeakerMap("src_a", "tr_a", "spk_0", person.person_id, True),
            ),
        )
    elif change == "brief":
        updated = replace(project, active_brief_id=None)
    elif change == "transcript":
        updated = replace(
            project,
            active_transcript_versions={
                **project.active_transcript_versions,
                "src_a": "tr_other",
            },
        )
    store.save(replace(updated, revision=project.revision + 1), expected_revision=project.revision)

    status = workflow_session_status(snapshot)

    assert status.status == "stale"
    assert status.read_only is True
    assert status.current_revision == project.revision + 1
    assert "project_revision" in status.reasons
    assert specific_reason in status.reasons


def test_proposal_matches_draft_uses_closed_schema2_block_ref_union() -> None:
    bindings = (
        SourceTranscriptBinding("src_a", "tr_a"),
        SourceTranscriptBinding("src_b", "tr_b"),
    )
    brief = EditBrief("brief_closed_refs", "主题", 240_000, ("重点",), True)
    source_ref = content_draft.ContentDraftRef("src_a", "tr_a", "seg_a", 0, 120_000)
    narration_ref = content_draft.ContentDraftRef("src_b", "tr_b", "seg_b", 120_000, 240_000)
    draft = content_draft.ContentDraft(
        "draft_closed_refs", None, 4, False, brief, bindings, "a" * 64,
        (
            content_draft.SectionTitleBlock("section_one", "第一章"),
            content_draft.SourceExcerptBlock("source_one", (source_ref,), "同期声"),
            content_draft.SectionTitleBlock("section_two", "第二章"),
            content_draft.NarrationBlock("narration_one", "解说", "recorded", (narration_ref,)),
        ),
        schema_version=2,
    )
    proposal = MultiSourceEditProposal(
        "proposal_closed_refs", 4, None, bindings, brief, "a" * 64,
        (
            EditClip("clip_source", "src_a", "tr_a", "seg_a", 0, 120_000, "同期声", "同期声"),
            EditClip("clip_narration", "src_b", "tr_b", "seg_b", 120_000, 240_000, "解说", "解说"),
        ),
        240_000, "fixture",
    )

    assert _proposal_matches_draft(proposal, draft) is True
