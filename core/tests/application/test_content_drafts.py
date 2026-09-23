from __future__ import annotations

import copy
import json
import wave
from dataclasses import replace
from pathlib import Path

import pytest

import roughcut.application.content_drafts as content_drafts_module
from roughcut.adapters.artifact_store import write_new_json
from roughcut.adapters.project_store import ProjectStore
from roughcut.application.agent_context import (
    create_edit_brief,
    read_agent_context,
    read_multi_source_agent_context,
)
from roughcut.application.content_drafts import (
    confirm_content_draft,
    create_content_draft,
    create_content_draft_editor_child,
    duration_acceptance_summary,
    propose_content_draft,
    publish_prepared_content_draft_editor_child,
    read_content_draft,
    revise_content_draft_scoped,
)
from roughcut.application.draft_editor import load_draft_editor_snapshot, prepare_draft_punctuation
from roughcut.application.edits import change_edit, propose_review_edit_change
from roughcut.application.people import update_source_metadata
from roughcut.application.projects import create_project
from roughcut.application.proposals import (
    confirm_edit_proposal,
    confirm_multi_source_edit_proposal,
    create_edit_proposal,
    read_proposal_diff,
)
from roughcut.application.transcripts import correct_transcript
from roughcut.domain.bindings import SourceTranscriptBinding
from roughcut.domain.brief import EditBrief
from roughcut.domain.content_draft import (
    ContentDraft,
    ContentDraftRef,
    SectionTitleBlock,
    SourceExcerptBlock,
    legacy_section_block_id,
    punctuation_stripped,
)
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


def _write_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(b"\0\0" * 1_600)


def _source(source_id: str, path: Path) -> SourceAsset:
    stat = path.stat()
    return SourceAsset(
        source_id=source_id,
        kind="audio",
        display_name=path.name,
        import_mode=ImportMode.LINKED,
        locator={"absolute_path": str(path)},
        fingerprint=SourceFingerprint(stat.st_size, stat.st_mtime_ns, f"fixture-{source_id}"),
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
    )


def _transcript(source_id: str, transcript_id: str, texts: tuple[str, ...]) -> TimedTranscript:
    return TimedTranscript(
        schema_version=1,
        transcript_version_id=transcript_id,
        source_id=source_id,
        parent_version_id=None,
        provenance=TranscriptProvenance(
            backend="fixture",
            package_version="fixture",
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
                original_text=text,
                corrected_text=None,
                local_speaker_id="spk_0",
                person_id=None,
                confidence=None,
                fine_units=(),
                editorial_mark="unmarked",
            )
            for index, text in enumerate(texts, start=1)
        ),
    )


def _fixture(tmp_path: Path) -> dict[str, object]:
    root = tmp_path / "content-draft-project"
    project = create_project(root, "Content Draft")
    media_dir = tmp_path / "fixture-media"
    media_dir.mkdir()
    sources: list[SourceAsset] = []
    transcripts = {
        "src_a": _transcript("src_a", "tr_a", ("甲素材第一句。", "甲素材第二句。")),
        "src_b": _transcript("src_b", "tr_b", ("乙素材第一句。",)),
        "src_n": _transcript("src_n", "tr_n", ("识别错字。",)),
    }
    for source_id, transcript in transcripts.items():
        media_path = media_dir / f"{source_id}.wav"
        _write_wav(media_path)
        sources.append(_source(source_id, media_path))
        write_new_json(
            root / "transcripts" / source_id / f"{transcript.transcript_version_id}.json",
            transcript.to_dict(),
        )
    brief = EditBrief("brief_content", "校园主题", 1_200_000, ("保留重点",), True)
    write_new_json(root / "briefs" / f"{brief.brief_id}.json", brief.to_dict())
    prepared = replace(
        project,
        revision=1,
        sources=tuple(sources),
        active_transcript_versions={
            source_id: transcript.transcript_version_id
            for source_id, transcript in transcripts.items()
        },
        active_brief_id=brief.brief_id,
    )
    ProjectStore(root).save(prepared, expected_revision=0)
    return {"root": root, "project": prepared, "brief": brief}


def _bindings(*source_ids: str, transcript_ids: dict[str, str] | None = None) -> list[dict[str, str]]:
    transcript_ids = transcript_ids or {"src_a": "tr_a", "src_b": "tr_b", "src_n": "tr_n"}
    return [
        {"source_id": source_id, "transcript_version_id": transcript_ids[source_id]}
        for source_id in source_ids
    ]


def _context_hash(root: Path, revision: int, *source_ids: str) -> str:
    project = ProjectStore(root).load()
    if len(source_ids) == 1:
        source_id = source_ids[0]
        return read_agent_context(
            root,
            source_id=source_id,
            transcript_version_id=project.active_transcript_versions[source_id],
            brief_id="brief_content",
            expected_revision=revision,
            offset=0,
            limit=1,
        ).context_hash
    bindings = _bindings(
        *source_ids,
        transcript_ids=project.active_transcript_versions,
    )
    return read_multi_source_agent_context(
        root,
        source_bindings=bindings,
        brief_id="brief_content",
        expected_revision=revision,
        offset=0,
        limit=1,
    ).context_hash


def _ref(source_id: str, transcript_id: str, index: int = 1) -> dict[str, object]:
    return {
        "source_id": source_id,
        "transcript_version_id": transcript_id,
        "segment_id": f"seg_{source_id}_{index}",
        "start_ticks": (index - 1) * 120_000,
        "end_ticks": index * 120_000,
    }


def _source_block(
    block_id: str = "block_a", *, text: str = "甲素材第一句。"
) -> dict[str, object]:
    return {
        "block_id": block_id,
        "kind": "source_excerpt",
        "refs": [_ref("src_a", "tr_a")],
        "canonical_text": text,
    }


def _schema2_blocks(blocks: list[dict[str, object]]) -> list[dict[str, object]]:
    projected: list[dict[str, object]] = []
    for block in blocks:
        copied = copy.deepcopy(block)
        title = copied.pop("section_title", None)
        if title is not None:
            block_id = copied.get("block_id")
            assert isinstance(block_id, str)
            assert isinstance(title, str)
            projected.append(
                {
                    "block_id": f"heading_{block_id}",
                    "kind": "section_title",
                    "title": title,
                }
            )
        projected.append(copied)
    return projected


def _partial_overlap_parent(
    root: Path, *, display_text: str | None = None
) -> ContentDraft:
    parent = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=_bindings("src_a"),
        brief_id="brief_content",
        context_hash=_context_hash(root, 1, "src_a"),
        blocks=[
            {
                "block_id": "parent_middle",
                "kind": "source_excerpt",
                "refs": [_ref("src_a", "tr_a", 2)],
                "canonical_text": "甲素材第二句。",
            }
        ],
        expected_revision=1,
    ).content_draft
    if display_text is None:
        return parent
    return create_content_draft_editor_child(
        root,
        parent=parent,
        blocks=(
            SourceExcerptBlock(
                "parent_middle",
                (ContentDraftRef.from_dict(_ref("src_a", "tr_a", 2)),),
                "甲素材第二句。",
                display_text=display_text,
            ),
        ),
        expected_revision=1,
    ).content_draft


def _partial_overlap_child() -> SourceExcerptBlock:
    return SourceExcerptBlock(
        "child_combined",
        (
            ContentDraftRef.from_dict(_ref("src_a", "tr_a", 1)),
            ContentDraftRef.from_dict(_ref("src_a", "tr_a", 2)),
        ),
        "甲素材第一句。\n甲素材第二句。",
    )


def _set_section_title(
    blocks: list[dict[str, object]], block_id: str, title: str
) -> None:
    block = next(block for block in blocks if block.get("block_id") == block_id)
    if block.get("kind") == "section_title":
        block["title"] = title
    else:
        block["section_title"] = title


def _scoped_parent(root: Path) -> tuple[str, list[dict[str, object]]]:
    blocks: list[dict[str, object]] = [
        {
            **_source_block("block_opening"),
            "section_title": "开头",
        },
        {
            "block_id": "block_chapter_a",
            "kind": "source_excerpt",
            "refs": [_ref("src_a", "tr_a", 2)],
            "canonical_text": "甲素材第二句。",
            "section_title": "第一章",
        },
        {
            "block_id": "block_chapter_b",
            "kind": "source_excerpt",
            "refs": [_ref("src_b", "tr_b")],
            "canonical_text": "乙素材第一句。",
            "section_title": "第二章",
        },
        {
            "block_id": "block_ending",
            "kind": "narration",
            "text": "原结尾解说。",
            "status": "draft",
            "recorded_refs": [],
            "section_title": "结尾",
        },
    ]
    parent = create_content_draft(
        root,
        parent_draft_id=None,
        display_title="局部修订测试",
        source_bindings=_bindings("src_a", "src_b"),
        brief_id="brief_content",
        context_hash=_context_hash(root, 1, "src_a", "src_b"),
        blocks=_schema2_blocks(blocks),
        expected_revision=1,
    )
    return parent.content_draft.content_draft_id, blocks


def test_scoped_revision_chains_children_and_preserves_unspecified_blocks_exactly(
    tmp_path: Path,
) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    parent_id, blocks = _scoped_parent(root)
    parent_path = root / "content-drafts" / f"{parent_id}.json"
    immutable_paths = (
        root / "transcripts" / "src_a" / "tr_a.json",
        root / "transcripts" / "src_b" / "tr_b.json",
        parent_path,
    )
    immutable_bytes = {path: path.read_bytes() for path in immutable_paths}

    first_blocks = [dict(block) for block in blocks]
    first_blocks[1] = {
        **first_blocks[1],
        "block_id": "block_chapter_a",
        "refs": [_ref("src_a", "tr_a")],
        "canonical_text": "甲素材第一句。",
    }
    first = revise_content_draft_scoped(
        root,
        parent_draft_id=parent_id,
        mutable_block_ids=["heading_block_chapter_a", "block_chapter_a"],
        blocks=_schema2_blocks(first_blocks),
        expected_revision=1,
    )
    assert first.content_draft.parent_draft_id == parent_id
    assert first.changed_block_ids == ("block_chapter_a",)
    assert first.unchanged_block_ids == (
        "heading_block_opening",
        "block_opening",
        "heading_block_chapter_b",
        "block_chapter_b",
        "heading_block_ending",
        "block_ending",
    )
    assert first.duration_delta_ticks == 0
    first_by_id = {block.block_id: block for block in first.content_draft.blocks}
    parent_by_id = {
        block.block_id: block
        for block in read_content_draft(root, parent_id).content_draft.blocks
    }
    for block_id in first.unchanged_block_ids:
        assert first_by_id[block_id] == parent_by_id[block_id]
    first_path = (
        root
        / "content-drafts"
        / f"{first.content_draft.content_draft_id}.json"
    )
    first_bytes = first_path.read_bytes()

    second_blocks = [
        block.to_dict(schema_version=2) for block in first.content_draft.blocks
    ]
    second_blocks[-1] = {
        **second_blocks[-1],
        "text": "新的结尾解说。",
    }
    second = revise_content_draft_scoped(
        root,
        parent_draft_id=first.content_draft.content_draft_id,
        mutable_block_ids=["block_ending"],
        blocks=second_blocks,
        expected_revision=1,
    )
    assert second.content_draft.parent_draft_id == first.content_draft.content_draft_id
    assert second.changed_block_ids == ("block_ending",)
    assert second.unchanged_block_ids == (
        "heading_block_opening",
        "block_opening",
        "heading_block_chapter_a",
        "block_chapter_a",
        "heading_block_chapter_b",
        "block_chapter_b",
        "heading_block_ending",
    )
    assert second.content_draft.blocks[:-2] == first.content_draft.blocks[:-2]
    second_path = (
        root
        / "content-drafts"
        / f"{second.content_draft.content_draft_id}.json"
    )
    second_bytes = second_path.read_bytes()

    reverted = revise_content_draft_scoped(
        root,
        parent_draft_id=second.content_draft.content_draft_id,
        mutable_block_ids=["block_ending"],
        blocks=[
            block.to_dict(schema_version=2) for block in first.content_draft.blocks
        ],
        expected_revision=1,
    )
    assert reverted.content_draft.parent_draft_id == second.content_draft.content_draft_id
    assert reverted.content_draft.blocks == first.content_draft.blocks
    assert first_path.read_bytes() == first_bytes
    assert second_path.read_bytes() == second_bytes
    for path, before in immutable_bytes.items():
        assert path.read_bytes() == before


def test_scoped_revision_can_undo_a_replacement_with_a_new_block_id(
    tmp_path: Path,
) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    parent_id, blocks = _scoped_parent(root)
    replacement_blocks = [dict(block) for block in blocks]
    replacement_blocks[1] = {
        **replacement_blocks[1],
        "block_id": "block_chapter_a_replacement",
    }

    replaced = revise_content_draft_scoped(
        root,
        parent_draft_id=parent_id,
        mutable_block_ids=[
            "heading_block_chapter_a",
            "block_chapter_a",
        ],
        blocks=_schema2_blocks(replacement_blocks),
        expected_revision=1,
    )
    assert replaced.changed_block_ids == (
        "heading_block_chapter_a",
        "block_chapter_a",
        "heading_block_chapter_a_replacement",
        "block_chapter_a_replacement",
    )

    restored = revise_content_draft_scoped(
        root,
        parent_draft_id=replaced.content_draft.content_draft_id,
        mutable_block_ids=["heading_block_chapter_a_replacement", "block_chapter_a_replacement"],
        blocks=_schema2_blocks(blocks),
        expected_revision=1,
    )
    assert restored.content_draft.blocks == read_content_draft(
        root, parent_id
    ).content_draft.blocks


def test_scoped_revision_treats_declared_block_reordering_as_a_change(
    tmp_path: Path,
) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    parent_id, blocks = _scoped_parent(root)
    reordered = [blocks[0], blocks[2], blocks[1], blocks[3]]

    revised = revise_content_draft_scoped(
        root,
        parent_draft_id=parent_id,
        mutable_block_ids=[
            "heading_block_chapter_a",
            "block_chapter_a",
            "heading_block_chapter_b",
            "block_chapter_b",
        ],
        blocks=_schema2_blocks(reordered),
        expected_revision=1,
    )

    assert [block.block_id for block in revised.content_draft.blocks] == [
        "heading_block_opening",
        "block_opening",
        "heading_block_chapter_b",
        "block_chapter_b",
        "heading_block_chapter_a",
        "block_chapter_a",
        "heading_block_ending",
        "block_ending",
    ]
    assert revised.changed_block_ids == (
        "heading_block_chapter_a",
        "block_chapter_a",
        "heading_block_chapter_b",
        "block_chapter_b",
    )
    assert revised.unchanged_block_ids == (
        "heading_block_opening",
        "block_opening",
        "heading_block_ending",
        "block_ending",
    )


def test_scoped_revision_rebases_the_active_confirmed_parent_after_adoption(
    tmp_path: Path,
) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    candidate = create_content_draft(
        root,
        parent_draft_id=None,
        display_title="采用后局部修改",
        source_bindings=_bindings("src_a"),
        brief_id="brief_content",
        context_hash=_context_hash(root, 1, "src_a"),
        blocks=[
            {"block_id": "heading_block_a", "kind": "section_title", "title": "开头"},
            _source_block(),
            {"block_id": "heading_block_a2", "kind": "section_title", "title": "结尾"},
            {
                **_source_block("block_a2", text="甲素材第二句。"),
                "refs": [_ref("src_a", "tr_a", 2)],
            },
        ],
        expected_revision=1,
    )
    confirmed = confirm_content_draft(
        root,
        candidate.content_draft.content_draft_id,
        expected_revision=1,
    )
    proposed = propose_content_draft(
        root,
        confirmed.content_draft.content_draft_id,
        expected_revision=2,
    )
    decision = confirm_edit_proposal(
        root,
        proposed.proposal.proposal_id,
        expected_revision=2,
    )
    project_before = (root / "project.json").read_bytes()
    immutable_bytes = {
        root
        / "proposals"
        / f"{proposed.proposal.proposal_id}.json": (
            root / "proposals" / f"{proposed.proposal.proposal_id}.json"
        ).read_bytes(),
        root / "edits" / f"{decision.decision.edit_version_id}.json": (
            root / "edits" / f"{decision.decision.edit_version_id}.json"
        ).read_bytes(),
    }
    blocks = [
        block.to_dict(schema_version=2) for block in confirmed.content_draft.blocks
    ]
    _set_section_title(blocks, "heading_block_a2", "新结尾")

    revised = revise_content_draft_scoped(
        root,
        parent_draft_id=confirmed.content_draft.content_draft_id,
        mutable_block_ids=["heading_block_a2"],
        blocks=blocks,
        expected_revision=3,
    )

    assert revised.content_draft.parent_draft_id == confirmed.content_draft.content_draft_id
    assert revised.content_draft.base_project_revision == 3
    assert revised.changed_block_ids == ("heading_block_a2",)
    assert "block_a" in revised.unchanged_block_ids
    assert revised.content_draft.blocks[0] == confirmed.content_draft.blocks[0]
    assert revised.content_draft.context_hash == _context_hash(root, 3, "src_a")
    assert (root / "project.json").read_bytes() == project_before
    assert all(path.read_bytes() == data for path, data in immutable_bytes.items())


@pytest.mark.parametrize(
    ("mutable_ids", "mutate", "message"),
    [
        (
            ["block_chapter_a"],
            lambda blocks: [
                {**block, "section_title": "被误改"}
                if block["block_id"] == "block_chapter_b"
                else block
                for block in blocks
            ],
            "unspecified",
        ),
        (
            ["block_chapter_a"],
            lambda blocks: [blocks[2], blocks[1], blocks[0], blocks[3]],
            "order",
        ),
        (
            ["block_unknown"],
            lambda blocks: blocks,
            "mutable block",
        ),
    ],
)
def test_scoped_revision_rejects_changes_outside_declared_scope_without_artifact(
    tmp_path: Path,
    mutable_ids: list[str],
    mutate,  # type: ignore[no-untyped-def]
    message: str,
) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    parent_id, blocks = _scoped_parent(root)
    before = {path.name for path in (root / "content-drafts").glob("*.json")}

    with pytest.raises(ProjectError, match=message):
        revise_content_draft_scoped(
            root,
            parent_draft_id=parent_id,
            mutable_block_ids=mutable_ids,
            blocks=_schema2_blocks(mutate([dict(block) for block in blocks])),
            expected_revision=1,
        )

    assert {path.name for path in (root / "content-drafts").glob("*.json")} == before


def test_public_create_and_scoped_revise_reject_display_input_but_inherit_parent_display(
    tmp_path: Path,
) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    parent = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=_bindings("src_a"),
        brief_id="brief_content",
        context_hash=_context_hash(root, 1, "src_a"),
        blocks=[_source_block()],
        expected_revision=1,
    ).content_draft
    parent_editor = create_content_draft_editor_child(
        root,
        parent=parent,
        blocks=(
            SourceExcerptBlock(
                "block_a",
                (ContentDraftRef.from_dict(_ref("src_a", "tr_a")),),
                "甲素材第一句。",
                display_text="甲素材第一句。！！",
            ),
        ),
        expected_revision=1,
    ).content_draft
    before = {path.name for path in (root / "content-drafts").glob("*.json")}
    with pytest.raises(ProjectError, match="fields are invalid"):
        create_content_draft(
            root,
            parent_draft_id=None,
            source_bindings=_bindings("src_a"),
            brief_id="brief_content",
            context_hash=_context_hash(root, 1, "src_a"),
            blocks=[{**_source_block(), "display_text": "甲素材第一句。！！"}],
            expected_revision=1,
        )
    assert {path.name for path in (root / "content-drafts").glob("*.json")} == before

    inherited = create_content_draft(
        root,
        parent_draft_id=parent_editor.content_draft_id,
        source_bindings=_bindings("src_a"),
        brief_id="brief_content",
        context_hash=_context_hash(root, 1, "src_a"),
        blocks=[_source_block()],
        expected_revision=1,
    )
    assert inherited.content_draft.blocks[0].display_text == "甲素材第一句。！！"

    revise_input = [block.to_dict(schema_version=2) for block in parent_editor.blocks]
    source = next(block for block in revise_input if block["kind"] == "source_excerpt")
    source["display_text"] = "甲素材第一句。？？"
    before_revise = {path.name for path in (root / "content-drafts").glob("*.json")}
    with pytest.raises(ProjectError, match="fields are invalid"):
        revise_content_draft_scoped(
            root,
            parent_draft_id=parent_editor.content_draft_id,
            mutable_block_ids=["block_a"],
            blocks=revise_input,
            expected_revision=1,
        )
    assert {path.name for path in (root / "content-drafts").glob("*.json")} == before_revise


def test_parent_partial_child_deterministically_inherits_display_punctuation(
    tmp_path: Path,
) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    parent = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=_bindings("src_a"),
        brief_id="brief_content",
        context_hash=_context_hash(root, 1, "src_a"),
        blocks=[_source_block()],
        expected_revision=1,
    ).content_draft
    parent_editor = create_content_draft_editor_child(
        root,
        parent=parent,
        blocks=(
            SourceExcerptBlock(
                "block_a",
                (ContentDraftRef.from_dict(_ref("src_a", "tr_a")),),
                "甲素材第一句。",
                display_text="甲素材！第一句。",
            ),
        ),
        expected_revision=1,
    ).content_draft

    partial = create_content_draft_editor_child(
        root,
        parent=parent_editor,
        blocks=(
            SourceExcerptBlock(
                "block_a",
                (ContentDraftRef("src_a", "tr_a", "seg_src_a_1", 0, 60_000),),
                "甲素材",
            ),
        ),
        expected_revision=1,
    ).content_draft
    block = next(block for block in partial.blocks if block.block_id == "block_a")
    assert isinstance(block, SourceExcerptBlock)
    assert block.display_text == "甲素材！"


def test_displayless_parent_partial_overlap_leaves_child_displayless(
    tmp_path: Path,
) -> None:
    root = Path(_fixture(tmp_path)["root"])
    child_block = _partial_overlap_child()
    child = create_content_draft_editor_child(
        root,
        parent=_partial_overlap_parent(root),
        blocks=(child_block,),
        expected_revision=1,
    ).content_draft

    assert len(child.blocks) == 1
    block = child.blocks[0]
    assert isinstance(block, SourceExcerptBlock)
    assert block.refs == child_block.refs
    assert block.canonical_text == child_block.canonical_text
    assert block.display_text is None


def test_display_bearing_parent_partial_overlap_still_fails_closed(
    tmp_path: Path,
) -> None:
    root = Path(_fixture(tmp_path)["root"])
    with pytest.raises(
        ProjectError,
        match="content draft source display inheritance is ambiguous",
    ):
        create_content_draft_editor_child(
            root,
            parent=_partial_overlap_parent(
                root,
                display_text="甲素材第二句。！！",
            ),
            blocks=(_partial_overlap_child(),),
            expected_revision=1,
        )


def test_public_parent_create_and_scoped_revision_inherit_prefix_middle_suffix_display(
    tmp_path: Path,
) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    transcript_path = root / "transcripts" / "src_a" / "tr_a.json"
    transcript = TimedTranscript.from_dict(
        json.loads(transcript_path.read_text(encoding="utf-8"))
    )
    first = transcript.segments[0]
    emoji_segment = replace(
        first,
        end_ticks=60_000,
        original_text="你😀好",
        fine_units=(
            FineUnit("character", "你", 0, 20_000, None),
            FineUnit("character", "😀", 20_000, 40_000, None),
            FineUnit("character", "好", 40_000, 60_000, None),
        ),
    )
    transcript_path.write_text(
        json.dumps(
            replace(transcript, segments=(emoji_segment, *transcript.segments[1:])).to_dict(),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    context_hash = _context_hash(root, 1, "src_a")
    full_ref = {
        "source_id": "src_a",
        "transcript_version_id": "tr_a",
        "segment_id": first.segment_id,
        "start_ticks": 0,
        "end_ticks": 60_000,
    }
    parent = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=_bindings("src_a"),
        brief_id="brief_content",
        context_hash=context_hash,
        blocks=[
            {
                "block_id": "block_a",
                "kind": "source_excerpt",
                "refs": [full_ref],
                "canonical_text": "你😀好",
            }
        ],
        expected_revision=1,
    ).content_draft
    parent_editor = create_content_draft_editor_child(
        root,
        parent=parent,
        blocks=(
            SourceExcerptBlock(
                "block_a",
                (ContentDraftRef("src_a", "tr_a", first.segment_id, 0, 60_000),),
                "你😀好",
                display_text="你，😀。好！",
            ),
        ),
        expected_revision=1,
    ).content_draft
    split_blocks = [
        {
            "block_id": "block_prefix",
            "kind": "source_excerpt",
            "refs": [{**full_ref, "end_ticks": 20_000}],
            "canonical_text": "你",
        },
        {
            "block_id": "block_a",
            "kind": "source_excerpt",
            "refs": [{**full_ref, "start_ticks": 20_000, "end_ticks": 40_000}],
            "canonical_text": "😀",
        },
        {
            "block_id": "block_suffix",
            "kind": "source_excerpt",
            "refs": [{**full_ref, "start_ticks": 40_000}],
            "canonical_text": "好",
        },
    ]
    created = create_content_draft(
        root,
        parent_draft_id=parent_editor.content_draft_id,
        source_bindings=_bindings("src_a"),
        brief_id="brief_content",
        context_hash=context_hash,
        blocks=split_blocks,
        expected_revision=1,
    ).content_draft
    created_sources = [
        block for block in created.blocks if isinstance(block, SourceExcerptBlock)
    ]
    assert [block.display_text for block in created_sources] == ["你，", "😀。", "好！"]
    assert "，" not in (created_sources[1].display_text or "")

    revised = revise_content_draft_scoped(
        root,
        parent_draft_id=parent_editor.content_draft_id,
        mutable_block_ids=["block_a"],
        blocks=split_blocks,
        expected_revision=1,
    ).content_draft
    revised_sources = [
        block for block in revised.blocks if isinstance(block, SourceExcerptBlock)
    ]
    assert [block.display_text for block in revised_sources] == ["你，", "😀。", "好！"]


def test_parent_display_inheritance_fails_closed_for_canonical_and_display_mapping_collision(
    tmp_path: Path,
) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    parent = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=_bindings("src_a"),
        brief_id="brief_content",
        context_hash=_context_hash(root, 1, "src_a"),
        blocks=[
            _source_block("parent_display"),
            _source_block("parent_display_other"),
            _source_block("parent_canonical"),
        ],
        expected_revision=1,
    ).content_draft
    parent_editor = create_content_draft_editor_child(
        root,
        parent=parent,
        blocks=tuple(
            replace(
                block,
                display_text=(
                    "甲，素材第一句。"
                    if block.block_id == "parent_display"
                    else "甲素材第一句。！！"
                    if block.block_id == "parent_display_other"
                    else None
                ),
            )
            for block in parent.blocks
            if isinstance(block, SourceExcerptBlock)
        ),
        expected_revision=1,
    ).content_draft
    before = set((root / "content-drafts").glob("*.json"))
    with pytest.raises(ProjectError, match="ambiguous"):
        create_content_draft(
            root,
            parent_draft_id=parent_editor.content_draft_id,
            source_bindings=_bindings("src_a"),
            brief_id="brief_content",
            context_hash=_context_hash(root, 1, "src_a"),
            blocks=[
                {
                    "block_id": "new_duplicate_source",
                    "kind": "source_excerpt",
                    "refs": [_ref("src_a", "tr_a", 1)],
                    "canonical_text": "甲素材第一句。",
                }
            ],
            expected_revision=1,
        )
    assert set((root / "content-drafts").glob("*.json")) == before


def test_scoped_revision_rejects_stale_unknown_refs_and_save_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    parent_id, blocks = _scoped_parent(root)
    before = {path.name for path in (root / "content-drafts").glob("*.json")}
    unknown_ref_blocks = [dict(block) for block in blocks]
    unknown_ref_blocks[1] = {
        **unknown_ref_blocks[1],
        "refs": [{**_ref("src_a", "tr_a", 2), "segment_id": "seg_unknown"}],
    }
    with pytest.raises(ProjectError, match="segment"):
        revise_content_draft_scoped(
            root,
            parent_draft_id=parent_id,
            mutable_block_ids=["block_chapter_a"],
            blocks=_schema2_blocks(unknown_ref_blocks),
            expected_revision=1,
        )
    assert {path.name for path in (root / "content-drafts").glob("*.json")} == before

    def fail_write(_path: Path, _payload: dict[str, object]) -> None:
        raise OSError("injected scoped save failure")

    monkeypatch.setattr(content_drafts_module, "write_new_json", fail_write)
    changed_blocks = [dict(block) for block in blocks]
    changed_blocks[-1] = {**changed_blocks[-1], "text": "保存失败的新结尾。"}
    with pytest.raises(OSError, match="scoped save failure"):
        revise_content_draft_scoped(
            root,
            parent_draft_id=parent_id,
            mutable_block_ids=["block_ending"],
            blocks=_schema2_blocks(changed_blocks),
            expected_revision=1,
        )
    assert {path.name for path in (root / "content-drafts").glob("*.json")} == before

    monkeypatch.undo()
    project = ProjectStore(root).load()
    ProjectStore(root).save(
        replace(project, revision=project.revision + 1),
        expected_revision=project.revision,
    )
    with pytest.raises(ProjectError, match="stale"):
        revise_content_draft_scoped(
            root,
            parent_draft_id=parent_id,
            mutable_block_ids=["block_ending"],
            blocks=changed_blocks,
            expected_revision=2,
        )
    assert {path.name for path in (root / "content-drafts").glob("*.json")} == before


def test_candidate_create_is_read_only_and_confirm_creates_immutable_child(
    tmp_path: Path,
) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    before_project = (root / "project.json").read_bytes()
    candidate = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=_bindings("src_a"),
        brief_id="brief_content",
        context_hash=_context_hash(root, 1, "src_a"),
        blocks=[
            _source_block(),
            {
                "block_id": "block_user_narration",
                "kind": "narration",
                "text": "用户撰写的旁白。",
                "status": "draft",
                "recorded_refs": [],
            },
            {
                "block_id": "block_agent_narration",
                "kind": "narration",
                "text": "Agent 建议的旁白。",
                "status": "approved",
                "recorded_refs": [],
            },
        ],
        expected_revision=1,
    )
    candidate_path = root / "content-drafts" / f"{candidate.content_draft.content_draft_id}.json"
    candidate_bytes = candidate_path.read_bytes()
    assert candidate.changed is True
    assert candidate.status == "current"
    assert (root / "project.json").read_bytes() == before_project
    assert ProjectStore(root).load().active_content_draft_id is None

    confirmed = confirm_content_draft(
        root,
        candidate.content_draft.content_draft_id,
        expected_revision=1,
    )
    project = ProjectStore(root).load()
    assert confirmed.changed is True
    assert confirmed.content_draft.parent_draft_id == candidate.content_draft.content_draft_id
    assert confirmed.content_draft.confirmed_by_user is True
    assert confirmed.content_draft.base_project_revision == 2
    assert confirmed.content_draft.context_hash == _context_hash(root, 2, "src_a")
    assert [block.status for block in confirmed.content_draft.blocks if hasattr(block, "status")] == [
        "approved",
        "approved",
    ]
    assert project.revision == 2
    assert project.active_content_draft_id == confirmed.content_draft.content_draft_id
    assert candidate_path.read_bytes() == candidate_bytes
    assert read_content_draft(root, confirmed.content_draft.content_draft_id).status == "current"
    assert read_content_draft(root, candidate.content_draft.content_draft_id).status == "stale"

    files_before = sorted((root / "content-drafts").glob("*.json"))
    repeated = confirm_content_draft(
        root,
        confirmed.content_draft.content_draft_id,
        expected_revision=2,
    )
    assert repeated.changed is False
    assert repeated.content_draft == confirmed.content_draft
    assert ProjectStore(root).load().revision == 2
    assert sorted((root / "content-drafts").glob("*.json")) == files_before


def test_single_source_confirmed_draft_schema2_compiles_without_revision_change(
    tmp_path: Path,
) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    candidate = create_content_draft(
        root,
        parent_draft_id=None,
        display_title="校园精华初稿",
        source_bindings=_bindings("src_a"),
        brief_id="brief_content",
        context_hash=_context_hash(root, 1, "src_a"),
        blocks=[
            {"block_id": "heading_block_a", "kind": "section_title", "title": "开场"},
            _source_block(),
            {"block_id": "heading_block_a2", "kind": "section_title", "title": "主体"},
            {
                **_source_block("block_a2", text="甲素材第二句。"),
                "refs": [_ref("src_a", "tr_a", 2)],
            },
        ],
        expected_revision=1,
    )
    confirmed = confirm_content_draft(
        root, candidate.content_draft.content_draft_id, expected_revision=1
    )
    proposed = propose_content_draft(
        root, confirmed.content_draft.content_draft_id, expected_revision=2
    )

    assert candidate.content_draft.display_title == "校园精华初稿"
    assert confirmed.content_draft.display_title == "校园精华初稿"
    assert [
        block.title
        for block in candidate.content_draft.blocks
        if block.kind == "section_title"
    ] == ["开场", "主体"]
    assert [
        block.title
        for block in confirmed.content_draft.blocks
        if block.kind == "section_title"
    ] == ["开场", "主体"]
    assert proposed.proposal_schema_version == 1
    assert [clip.segment_id for clip in proposed.proposal.clips] == [
        "seg_src_a_1",
        "seg_src_a_2",
    ]
    assert [clip.display_text for clip in proposed.proposal.clips] == [
        "甲素材第一句。",
        "甲素材第二句。",
    ]
    assert ProjectStore(root).load().revision == 2
    assert ProjectStore(root).load().active_edit_version_id is None


def _confirmed_editorial_single_source_draft(
    root: Path,
    *,
    source_blocks: tuple[SourceExcerptBlock, ...],
) -> ContentDraft:
    parent = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=_bindings("src_a"),
        brief_id="brief_content",
        context_hash=_context_hash(root, 1, "src_a"),
        blocks=[
            {
                key: value
                for key, value in block.to_dict(schema_version=2).items()
                if key != "display_text"
            }
            for block in source_blocks
        ],
        expected_revision=1,
    ).content_draft
    editor = create_content_draft_editor_child(
        root,
        parent=parent,
        blocks=source_blocks,
        expected_revision=1,
    ).content_draft
    return confirm_content_draft(
        root,
        editor.content_draft_id,
        expected_revision=1,
    ).content_draft


def _editorial_single_source_blocks() -> tuple[SourceExcerptBlock, ...]:
    canonical_texts = ("甲素材第一句。", "甲素材第二句。")
    return tuple(
        SourceExcerptBlock(
            block_id=f"block_a{index}",
            refs=(ContentDraftRef.from_dict(_ref("src_a", "tr_a", index)),),
            canonical_text=canonical_texts[index - 1],
            display_text=f"{canonical_texts[index - 1][:-1]}{marks}",
        )
        for index, marks in ((1, "！！"), (2, "？？"))
    )


def _decision_a_with_different_confirmed_draft_b(
    root: Path,
) -> tuple[object, ContentDraft]:
    confirmed_a = _confirmed_editorial_single_source_draft(
        root,
        source_blocks=_editorial_single_source_blocks(),
    )
    proposed_a = propose_content_draft(
        root,
        confirmed_a.content_draft_id,
        expected_revision=2,
    )
    decision_a = confirm_edit_proposal(
        root,
        proposed_a.proposal.proposal_id,
        expected_revision=2,
    )
    project = ProjectStore(root).load()
    assert project.active_edit_version_id == decision_a.decision.edit_version_id
    draft_b = create_content_draft(
        root,
        parent_draft_id=confirmed_a.content_draft_id,
        display_title=confirmed_a.display_title,
        source_bindings=[binding.to_dict() for binding in confirmed_a.source_bindings],
        brief_id=confirmed_a.brief_snapshot.brief_id,
        context_hash=_context_hash(root, project.revision, "src_a"),
        blocks=[
            {
                key: value
                for key, value in block.to_dict(schema_version=2).items()
                if key != "display_text"
            }
            for block in confirmed_a.blocks
        ],
        expected_revision=project.revision,
    ).content_draft
    snapshot = load_draft_editor_snapshot(
        root,
        source_bindings=_bindings("src_a"),
        content_draft_id=draft_b.content_draft_id,
    )
    paragraph = next(
        item
        for item in snapshot.paragraphs
        if item["kind"] == "source_excerpt" and "甲素材第一句" in str(item["text"])
    )
    text = str(paragraph["text"])
    punctuation_start = text.index("！！")
    prepared = prepare_draft_punctuation(
        snapshot,
        paragraph_id=str(paragraph["paragraph_id"]),
        block_id="block_a1",
        start_utf16_offset=punctuation_start,
        end_utf16_offset=punctuation_start + 2,
        replacement="？？",
        child_id="draft_editorial_b_child",
    )
    published = publish_prepared_content_draft_editor_child(
        root,
        parent=draft_b,
        child=prepared,
        expected_revision=project.revision,
    )
    confirmed_b = confirm_content_draft(
        root,
        published.content_draft.content_draft_id,
        expected_revision=project.revision,
    ).content_draft
    assert ProjectStore(root).load().active_edit_version_id == decision_a.decision.edit_version_id
    assert confirmed_b.blocks[0].display_text == "甲素材第一句？？"
    return decision_a, confirmed_b


def test_edit_lineage_preserves_display_after_a_different_active_content_draft(
    tmp_path: Path,
) -> None:
    root = Path(_fixture(tmp_path)["root"])
    decision_a, confirmed_b = _decision_a_with_different_confirmed_draft_b(root)
    assert confirmed_b.confirmed_by_user is True
    ids = [clip.clip_id for clip in decision_a.decision.proposal_snapshot.clips]

    reordered = change_edit(
        root,
        operation={"type": "reorder", "ordered_clip_ids": [ids[1], ids[0]]},
        expected_revision=4,
        base_edit_version_id=decision_a.decision.edit_version_id,
    )
    assert [clip.display_text for clip in reordered.decision.proposal_snapshot.clips] == [
        "甲素材第二句？？",
        "甲素材第一句！！",
    ]
    deleted = change_edit(
        root,
        operation={"type": "delete", "clip_id": ids[1]},
        expected_revision=5,
        base_edit_version_id=reordered.decision.edit_version_id,
    )
    assert [clip.display_text for clip in deleted.decision.proposal_snapshot.clips] == [
        "甲素材第一句！！",
    ]
    restored = change_edit(
        root,
        operation={"type": "restore", "clip_id": ids[1]},
        expected_revision=6,
        base_edit_version_id=deleted.decision.edit_version_id,
    )
    assert [clip.display_text for clip in restored.decision.proposal_snapshot.clips] == [
        "甲素材第一句！！",
        "甲素材第二句？？",
    ]
    trimmed = change_edit(
        root,
        operation={
            "type": "trim",
            "clip_id": ids[0],
            "source_in_ticks": 1,
            "source_out_ticks": 120_000,
        },
        expected_revision=7,
        base_edit_version_id=restored.decision.edit_version_id,
    )
    assert trimmed.decision.proposal_snapshot.clips[0].display_text == "甲素材第一句！！"


def test_review_lineage_diff_and_confirm_ignore_a_later_draft_display(
    tmp_path: Path,
) -> None:
    root = Path(_fixture(tmp_path)["root"])
    decision_a, _confirmed_b = _decision_a_with_different_confirmed_draft_b(root)
    current = decision_a.decision.proposal_snapshot
    ids = [clip.clip_id for clip in current.clips]
    reordered = propose_review_edit_change(
        root,
        proposal=current,
        operation={"type": "reorder", "ordered_clip_ids": [ids[1], ids[0]]},
        expected_revision=4,
        restoration_clips=current.clips,
    )
    deleted = propose_review_edit_change(
        root,
        proposal=reordered.proposal,
        operation={"type": "delete", "clip_id": ids[1]},
        expected_revision=4,
        restoration_clips=current.clips,
    )
    diff = read_proposal_diff(root, deleted.proposal.proposal_id, expected_revision=4)
    assert diff.proposal_diff.removed[0].display_text == "甲素材第二句？？"
    confirmed = confirm_edit_proposal(
        root,
        deleted.proposal.proposal_id,
        expected_revision=4,
    )
    assert [clip.display_text for clip in confirmed.decision.proposal_snapshot.clips] == [
        "甲素材第一句！！",
    ]


def test_new_content_draft_proposal_uses_its_own_display_after_edit_lineage_exists(
    tmp_path: Path,
) -> None:
    root = Path(_fixture(tmp_path)["root"])
    _decision_a, confirmed_b = _decision_a_with_different_confirmed_draft_b(root)
    proposed_b = propose_content_draft(
        root,
        confirmed_b.content_draft_id,
        expected_revision=4,
    )
    assert [clip.display_text for clip in proposed_b.proposal.clips] == [
        "甲素材第一句？？",
        "甲素材第二句？？",
    ]
    confirmed = confirm_edit_proposal(
        root,
        proposed_b.proposal.proposal_id,
        expected_revision=4,
    )
    assert [clip.display_text for clip in confirmed.decision.proposal_snapshot.clips] == [
        "甲素材第一句？？",
        "甲素材第二句？？",
    ]


def test_nearest_edit_lineage_preserves_latest_draft_display_for_direct_and_review(
    tmp_path: Path,
) -> None:
    root = Path(_fixture(tmp_path)["root"])
    _decision_a, confirmed_b = _decision_a_with_different_confirmed_draft_b(root)
    proposed_b = propose_content_draft(root, confirmed_b.content_draft_id, expected_revision=4)
    decision_b = confirm_edit_proposal(
        root, proposed_b.proposal.proposal_id, expected_revision=4
    )
    ids = [clip.clip_id for clip in decision_b.decision.proposal_snapshot.clips]
    reordered = change_edit(
        root,
        operation={"type": "reorder", "ordered_clip_ids": [ids[1], ids[0]]},
        expected_revision=5,
        base_edit_version_id=decision_b.decision.edit_version_id,
    )
    trimmed = change_edit(
        root,
        operation={
            "type": "trim",
            "clip_id": ids[0],
            "source_in_ticks": 1,
            "source_out_ticks": 120_000,
        },
        expected_revision=6,
        base_edit_version_id=reordered.decision.edit_version_id,
    )
    deleted = change_edit(
        root,
        operation={"type": "delete", "clip_id": ids[0]},
        expected_revision=7,
        base_edit_version_id=trimmed.decision.edit_version_id,
    )
    restored = change_edit(
        root,
        operation={"type": "restore", "clip_id": ids[0]},
        expected_revision=8,
        base_edit_version_id=deleted.decision.edit_version_id,
    )
    assert [clip.display_text for clip in restored.decision.proposal_snapshot.clips] == [
        "甲素材第二句？？",
        "甲素材第一句？？",
    ]

    current = restored.decision.proposal_snapshot
    review = propose_review_edit_change(
        root,
        proposal=current,
        operation={"type": "reorder", "ordered_clip_ids": [ids[0], ids[1]]},
        expected_revision=9,
        restoration_clips=current.clips,
    )
    review_delete = propose_review_edit_change(
        root,
        proposal=review.proposal,
        operation={"type": "delete", "clip_id": ids[1]},
        expected_revision=9,
        restoration_clips=current.clips,
    )
    diff = read_proposal_diff(root, review_delete.proposal.proposal_id, expected_revision=9)
    assert diff.proposal_diff.removed[0].display_text == "甲素材第二句？？"
    confirmed = confirm_edit_proposal(
        root, review_delete.proposal.proposal_id, expected_revision=9
    )
    assert [clip.display_text for clip in confirmed.decision.proposal_snapshot.clips] == [
        "甲素材第一句？？",
    ]

def test_new_draft_review_diff_confirm_and_direct_edit_share_display_trust(
    tmp_path: Path,
) -> None:
    root = Path(_fixture(tmp_path)["root"])
    _decision_a, confirmed_b = _decision_a_with_different_confirmed_draft_b(root)
    proposed_b = propose_content_draft(root, confirmed_b.content_draft_id, expected_revision=4)
    ids = [clip.clip_id for clip in proposed_b.proposal.clips]
    review = propose_review_edit_change(
        root,
        proposal=proposed_b.proposal,
        operation={"type": "reorder", "ordered_clip_ids": [ids[1], ids[0]]},
        expected_revision=4,
        restoration_clips=proposed_b.proposal.clips,
    )
    derived = propose_review_edit_change(
        root,
        proposal=review.proposal,
        operation={"type": "delete", "clip_id": ids[1]},
        expected_revision=4,
        restoration_clips=proposed_b.proposal.clips,
    )
    assert [clip.display_text for clip in derived.proposal.clips] == ["甲素材第一句？？"]
    diff = read_proposal_diff(root, derived.proposal.proposal_id, expected_revision=4)
    change = diff.proposal_diff.changed[0]
    assert (change.before.display_text, change.after.display_text) == ("甲素材第一句！！", "甲素材第一句？？")
    confirmed = confirm_edit_proposal(
        root, derived.proposal.proposal_id, expected_revision=4
    )
    edited = change_edit(
        root,
        operation={
            "type": "trim",
            "clip_id": ids[0],
            "source_in_ticks": 1,
            "source_out_ticks": 120_000,
        },
        expected_revision=5,
        base_edit_version_id=confirmed.decision.edit_version_id,
    )
    assert edited.decision.proposal_snapshot.clips[0].display_text == "甲素材第一句？？"


@pytest.mark.parametrize("entrypoint", ("diff", "confirm"))
def test_damaged_edit_lineage_rejects_draft_proposal_without_writes(
    tmp_path: Path,
    entrypoint: str,
) -> None:
    root = Path(_fixture(tmp_path)["root"])
    _decision_a, confirmed_b = _decision_a_with_different_confirmed_draft_b(root)
    proposed_b = propose_content_draft(root, confirmed_b.content_draft_id, expected_revision=4)
    store = ProjectStore(root)
    before_project = store.load()
    active_id = before_project.active_edit_version_id
    assert active_id is not None
    edit_path = root / "edits" / f"{active_id}.json"
    edit_data = json.loads(edit_path.read_text(encoding="utf-8"))
    edit_data["proposal_snapshot"]["base_edit_version_id"] = active_id
    edit_path.write_text(json.dumps(edit_data, ensure_ascii=False), encoding="utf-8")
    before_artifacts = {path.relative_to(root) for path in root.rglob("*.json")}

    with pytest.raises(ProjectError, match="cycle"):
        if entrypoint == "diff":
            read_proposal_diff(root, proposed_b.proposal.proposal_id, expected_revision=4)
        else:
            confirm_edit_proposal(root, proposed_b.proposal.proposal_id, expected_revision=4)
    assert store.load() == before_project
    assert {path.relative_to(root) for path in root.rglob("*.json")} == before_artifacts


def test_multi_source_edit_lineage_preserves_display_after_new_content_draft(
    tmp_path: Path,
) -> None:
    root = Path(_fixture(tmp_path)["root"])
    source_blocks = (
        SourceExcerptBlock(
            "multi_block_a",
            (ContentDraftRef.from_dict(_ref("src_a", "tr_a")),),
            "甲素材第一句。",
            display_text="甲素材第一句！！",
        ),
        SourceExcerptBlock(
            "multi_block_b",
            (ContentDraftRef.from_dict(_ref("src_b", "tr_b")),),
            "乙素材第一句。",
            display_text="乙素材第一句？？",
        ),
    )
    parent = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=_bindings("src_a", "src_b"),
        brief_id="brief_content",
        context_hash=_context_hash(root, 1, "src_a", "src_b"),
        blocks=[
            {
                key: value
                for key, value in block.to_dict(schema_version=2).items()
                if key != "display_text"
            }
            for block in source_blocks
        ],
        expected_revision=1,
    ).content_draft
    editor = create_content_draft_editor_child(
        root,
        parent=parent,
        blocks=source_blocks,
        expected_revision=1,
    ).content_draft
    confirmed_a = confirm_content_draft(
        root, editor.content_draft_id, expected_revision=1
    ).content_draft
    proposed_a = propose_content_draft(root, confirmed_a.content_draft_id, expected_revision=2)
    confirm_multi_source_edit_proposal(
        root, proposed_a.proposal.proposal_id, expected_revision=2
    )
    project = ProjectStore(root).load()
    draft_b = create_content_draft(
        root,
        parent_draft_id=confirmed_a.content_draft_id,
        display_title=confirmed_a.display_title,
        source_bindings=[binding.to_dict() for binding in confirmed_a.source_bindings],
        brief_id=confirmed_a.brief_snapshot.brief_id,
        context_hash=_context_hash(root, project.revision, "src_a", "src_b"),
        blocks=[
            {
                key: value
                for key, value in block.to_dict(schema_version=2).items()
                if key != "display_text"
            }
            for block in confirmed_a.blocks
        ],
        expected_revision=project.revision,
    ).content_draft
    snapshot = load_draft_editor_snapshot(
        root,
        source_bindings=_bindings("src_a", "src_b"),
        content_draft_id=draft_b.content_draft_id,
    )
    paragraph = next(
        item
        for item in snapshot.paragraphs
        if item["kind"] == "source_excerpt" and "甲素材第一句" in str(item["text"])
    )
    text = str(paragraph["text"])
    punctuation_start = text.index("！！")
    prepared = prepare_draft_punctuation(
        snapshot,
        paragraph_id=str(paragraph["paragraph_id"]),
        block_id="multi_block_a",
        start_utf16_offset=punctuation_start,
        end_utf16_offset=punctuation_start + 2,
        replacement="？？",
        child_id="draft_multi_editorial_b_child",
    )
    published = publish_prepared_content_draft_editor_child(
        root,
        parent=draft_b,
        child=prepared,
        expected_revision=project.revision,
    )
    confirmed_b = confirm_content_draft(root, published.content_draft.content_draft_id, expected_revision=project.revision).content_draft
    proposed_b = propose_content_draft(root, confirmed_b.content_draft_id, expected_revision=4)
    decision_b = confirm_multi_source_edit_proposal(root, proposed_b.proposal.proposal_id, expected_revision=4)
    ids = [clip.clip_id for clip in decision_b.decision.proposal_snapshot.clips]
    reordered = change_edit(
        root,
        operation={"type": "reorder", "ordered_clip_ids": [ids[1], ids[0]]},
        expected_revision=5,
        base_edit_version_id=decision_b.decision.edit_version_id,
    )
    assert [clip.display_text for clip in reordered.decision.proposal_snapshot.clips] == [
        "乙素材第一句？？",
        "甲素材第一句？？",
    ]


def test_edit_lineage_rejects_same_clip_id_with_tampered_display(
    tmp_path: Path,
) -> None:
    root = Path(_fixture(tmp_path)["root"])
    decision_a, _confirmed_b = _decision_a_with_different_confirmed_draft_b(root)
    tampered = replace(
        decision_a.decision.proposal_snapshot,
        proposal_id="proposal_tampered_lineage",
        base_project_revision=4,
        base_edit_version_id=ProjectStore(root).load().active_edit_version_id,
        clips=(
            replace(
                decision_a.decision.proposal_snapshot.clips[0],
                display_text="甲素材第一句！！？",
            ),
            *decision_a.decision.proposal_snapshot.clips[1:],
        ),
    )
    with pytest.raises(ProjectError, match="canonical text"):
        propose_review_edit_change(
            root,
            proposal=tampered,
            operation={
                "type": "reorder",
                "ordered_clip_ids": [tampered.clips[1].clip_id, tampered.clips[0].clip_id],
            },
            expected_revision=4,
            restoration_clips=decision_a.decision.proposal_snapshot.clips,
        )
    write_new_json(
        root / "proposals" / f"{tampered.proposal_id}.json", tampered.to_dict()
    )
    with pytest.raises(ProjectError):
        read_proposal_diff(root, tampered.proposal_id, expected_revision=4)
    with pytest.raises(ProjectError):
        confirm_edit_proposal(root, tampered.proposal_id, expected_revision=4)


def test_confirmed_content_draft_review_edit_chain_preserves_display_punctuation(
    tmp_path: Path,
) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    confirmed = _confirmed_editorial_single_source_draft(
        root,
        source_blocks=_editorial_single_source_blocks(),
    )
    proposed = propose_content_draft(
        root,
        confirmed.content_draft_id,
        expected_revision=2,
    )
    initial_ids = [clip.clip_id for clip in proposed.proposal.clips]

    reordered = propose_review_edit_change(
        root,
        proposal=proposed.proposal,
        operation={
            "type": "reorder",
            "ordered_clip_ids": [initial_ids[1], initial_ids[0]],
        },
        expected_revision=2,
        restoration_clips=proposed.proposal.clips,
    )
    assert [clip.display_text for clip in reordered.proposal.clips] == [
        "甲素材第二句？？",
        "甲素材第一句！！",
    ]

    deleted = propose_review_edit_change(
        root,
        proposal=reordered.proposal,
        operation={"type": "delete", "clip_id": initial_ids[1]},
        expected_revision=2,
        restoration_clips=proposed.proposal.clips,
    )
    assert [clip.clip_id for clip in deleted.proposal.clips] == [initial_ids[0]]
    assert [clip.display_text for clip in deleted.proposal.clips] == [
        "甲素材第一句！！",
    ]
    confirmed_proposal = confirm_edit_proposal(
        root,
        deleted.proposal.proposal_id,
        expected_revision=2,
    )
    assert [
        clip.display_text for clip in confirmed_proposal.decision.proposal_snapshot.clips
    ] == [
        "甲素材第一句！！",
    ]


def test_confirmed_content_draft_decision_edit_chain_preserves_display_punctuation(
    tmp_path: Path,
) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    confirmed = _confirmed_editorial_single_source_draft(
        root,
        source_blocks=_editorial_single_source_blocks(),
    )
    proposed = propose_content_draft(
        root,
        confirmed.content_draft_id,
        expected_revision=2,
    )
    decision = confirm_edit_proposal(
        root,
        proposed.proposal.proposal_id,
        expected_revision=2,
    )
    ids = [clip.clip_id for clip in decision.decision.proposal_snapshot.clips]

    reordered = change_edit(
        root,
        operation={"type": "reorder", "ordered_clip_ids": [ids[1], ids[0]]},
        expected_revision=3,
        base_edit_version_id=decision.decision.edit_version_id,
    )
    assert [clip.display_text for clip in reordered.decision.proposal_snapshot.clips] == [
        "甲素材第二句？？",
        "甲素材第一句！！",
    ]

    deleted = change_edit(
        root,
        operation={"type": "delete", "clip_id": ids[1]},
        expected_revision=4,
        base_edit_version_id=reordered.decision.edit_version_id,
    )
    assert [clip.display_text for clip in deleted.decision.proposal_snapshot.clips] == [
        "甲素材第一句！！",
    ]


def test_confirmed_content_draft_review_diff_and_confirm_preserve_display_punctuation(
    tmp_path: Path,
) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    confirmed = _confirmed_editorial_single_source_draft(
        root,
        source_blocks=_editorial_single_source_blocks(),
    )
    proposed = propose_content_draft(root, confirmed.content_draft_id, expected_revision=2)
    adopted = confirm_edit_proposal(
        root,
        proposed.proposal.proposal_id,
        expected_revision=2,
    )
    current = adopted.decision.proposal_snapshot
    ids = [clip.clip_id for clip in current.clips]
    review = propose_review_edit_change(
        root,
        proposal=current,
        operation={"type": "reorder", "ordered_clip_ids": [ids[1], ids[0]]},
        expected_revision=3,
        restoration_clips=current.clips,
    )
    diff = read_proposal_diff(root, review.proposal.proposal_id, expected_revision=3)
    assert diff.proposal_diff.order_changed is True
    assert [clip.display_text for clip in review.proposal.clips] == [
        "甲素材第二句？？",
        "甲素材第一句！！",
    ]
    confirmed_review = confirm_edit_proposal(
        root,
        review.proposal.proposal_id,
        expected_revision=3,
    )
    assert [clip.display_text for clip in confirmed_review.decision.proposal_snapshot.clips] == [
        "甲素材第二句？？",
        "甲素材第一句！！",
    ]


def test_confirmed_multi_source_content_draft_review_reorder_preserves_display_punctuation(
    tmp_path: Path,
) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    blocks = (
        SourceExcerptBlock(
            "block_a",
            (ContentDraftRef.from_dict(_ref("src_a", "tr_a")),),
            "甲素材第一句。",
            display_text="甲素材第一句！！",
        ),
        SourceExcerptBlock(
            "block_b",
            (ContentDraftRef.from_dict(_ref("src_b", "tr_b")),),
            "乙素材第一句。",
            display_text="乙素材第一句？？",
        ),
    )
    parent = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=_bindings("src_a", "src_b"),
        brief_id="brief_content",
        context_hash=_context_hash(root, 1, "src_a", "src_b"),
        blocks=[
            {
                key: value
                for key, value in block.to_dict(schema_version=2).items()
                if key != "display_text"
            }
            for block in blocks
        ],
        expected_revision=1,
    ).content_draft
    editor = create_content_draft_editor_child(
        root,
        parent=parent,
        blocks=blocks,
        expected_revision=1,
    ).content_draft
    confirmed = confirm_content_draft(root, editor.content_draft_id, expected_revision=1).content_draft
    proposed = propose_content_draft(root, confirmed.content_draft_id, expected_revision=2)
    ids = [clip.clip_id for clip in proposed.proposal.clips]

    reordered = propose_review_edit_change(
        root,
        proposal=proposed.proposal,
        operation={"type": "reorder", "ordered_clip_ids": [ids[1], ids[0]]},
        expected_revision=2,
        restoration_clips=proposed.proposal.clips,
    )
    assert [clip.display_text for clip in reordered.proposal.clips] == [
        "乙素材第一句？？",
        "甲素材第一句！！",
    ]
    confirmed_proposal = confirm_multi_source_edit_proposal(
        root,
        reordered.proposal.proposal_id,
        expected_revision=2,
    )
    assert [clip.display_text for clip in confirmed_proposal.decision.proposal_snapshot.clips] == [
        "乙素材第一句？？",
        "甲素材第一句！！",
    ]


def test_proposal_splits_one_multiref_display_once_at_the_fixed_connection(
    tmp_path: Path,
) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    parent = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=_bindings("src_a"),
        brief_id="brief_content",
        context_hash=_context_hash(root, 1, "src_a"),
        blocks=[_source_block()],
        expected_revision=1,
    ).content_draft
    first_ref = ContentDraftRef.from_dict(_ref("src_a", "tr_a", 1))
    second_ref = ContentDraftRef.from_dict(_ref("src_a", "tr_a", 2))
    multi_ref = SourceExcerptBlock(
        "block_multi",
        (first_ref, second_ref),
        "甲素材第一句。\n甲素材第二句。",
        display_text="甲素材第一句。！！\n？甲素材第二句。",
    )
    child = create_content_draft_editor_child(
        root,
        parent=parent,
        blocks=(multi_ref,),
        expected_revision=1,
    ).content_draft
    confirmed = confirm_content_draft(
        root,
        child.content_draft_id,
        expected_revision=1,
    )
    proposed = propose_content_draft(
        root,
        confirmed.content_draft.content_draft_id,
        expected_revision=2,
    )
    assert [clip.segment_id for clip in proposed.proposal.clips] == [
        "seg_src_a_1",
        "seg_src_a_2",
    ]
    assert [clip.display_text for clip in proposed.proposal.clips] == [
        "甲素材第一句。！！",
        "？甲素材第二句。",
    ]
    assert [
        (clip.source_in_ticks, clip.source_out_ticks)
        for clip in proposed.proposal.clips
    ] == [(0, 120_000), (120_000, 240_000)]
    assert proposed.proposal.total_duration_ticks == 240_000
    decision = confirm_edit_proposal(
        root,
        proposed.proposal.proposal_id,
        expected_revision=2,
    )
    assert [clip.display_text for clip in decision.decision.proposal_snapshot.clips] == [
        "甲素材第一句。！！",
        "？甲素材第二句。",
    ]


def test_long_draft_with_hundreds_of_punctuation_marks_roundtrips_and_compiles(
    tmp_path: Path,
) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    long_text = " ".join("长词" for _ in range(300))
    long_transcript = _transcript("src_a", "tr_long", (long_text,))
    write_new_json(
        root / "transcripts" / "src_a" / "tr_long.json",
        long_transcript.to_dict(),
    )
    project = ProjectStore(root).load()
    ProjectStore(root).save(
        replace(
            project,
            active_transcript_versions={
                **project.active_transcript_versions,
                "src_a": "tr_long",
            },
        ),
        expected_revision=project.revision,
    )
    canonical = long_text
    display = canonical.replace(" ", "！ ") + "！"
    parent = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=_bindings("src_a", transcript_ids={"src_a": "tr_long"}),
        brief_id="brief_content",
        context_hash=_context_hash(root, 1, "src_a"),
        blocks=[
            {
                "block_id": "long_block",
                "kind": "source_excerpt",
                "refs": [_ref("src_a", "tr_long")],
                "canonical_text": canonical,
            }
        ],
        expected_revision=1,
    ).content_draft
    child = create_content_draft_editor_child(
        root,
        parent=parent,
        blocks=(
            SourceExcerptBlock(
                "long_block",
                parent.blocks[0].refs,  # type: ignore[union-attr]
                canonical,
                display_text=display,
            ),
        ),
        expected_revision=1,
    ).content_draft
    loaded = read_content_draft(root, child.content_draft_id).content_draft
    loaded_block = loaded.blocks[0]
    assert isinstance(loaded_block, SourceExcerptBlock)
    assert loaded_block.display_text == display
    assert len([character for character in display if character == "！"]) == 300
    assert punctuation_stripped(display) == canonical

    confirmed = confirm_content_draft(
        root,
        child.content_draft_id,
        expected_revision=1,
    ).content_draft
    proposed = propose_content_draft(
        root,
        confirmed.content_draft_id,
        expected_revision=2,
    ).proposal
    assert len(proposed.clips) == 1
    assert proposed.clips[0].display_text == display
    assert proposed.clips[0].source_in_ticks == 0
    assert proposed.clips[0].source_out_ticks == 120_000
    assert proposed.total_duration_ticks == 120_000


@pytest.mark.parametrize(
    "block",
    [
        _source_block(text="改写的文本。"),
        _source_block(text="甲素材"),
        _source_block(text="甲素材一句。"),
        {
            **_source_block(),
            "refs": [{**_ref("src_a", "tr_a"), "end_ticks": 240_000}],
        },
        {
            **_source_block(),
            "refs": [_ref("src_b", "tr_b")],
            "canonical_text": "乙素材第一句。",
        },
    ],
)
def test_source_excerpt_rejects_noncanonical_or_out_of_scope_refs(
    tmp_path: Path, block: dict[str, object]
) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    with pytest.raises(ProjectError):
        create_content_draft(
            root,
            parent_draft_id=None,
            source_bindings=_bindings("src_a"),
            brief_id="brief_content",
            context_hash=_context_hash(root, 1, "src_a"),
            blocks=[block],
            expected_revision=1,
        )
    assert not (root / "content-drafts").exists()
    assert ProjectStore(root).load().revision == 1


def test_source_excerpt_omitted_canonical_text_is_derived_and_readback_is_stable(
    tmp_path: Path,
) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    block = _source_block()
    block.pop("canonical_text")

    created = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=_bindings("src_a"),
        brief_id="brief_content",
        context_hash=_context_hash(root, 1, "src_a"),
        blocks=[block],
        expected_revision=1,
    ).content_draft
    first = read_content_draft(root, created.content_draft_id).to_dict()
    second = read_content_draft(root, created.content_draft_id).to_dict()

    assert first == second
    stored = first["content_draft"]["blocks"][0]
    assert stored["canonical_text"] == "甲素材第一句。"


def test_unrecorded_narration_blocks_proposal(tmp_path: Path) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    candidate = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=_bindings("src_a"),
        brief_id="brief_content",
        context_hash=_context_hash(root, 1, "src_a"),
        blocks=[
            _source_block(),
            {
                "block_id": "block_narration",
                "kind": "narration",
                "text": "尚未录制。",
                "status": "draft",
                "recorded_refs": [],
            },
        ],
        expected_revision=1,
    )
    confirmed = confirm_content_draft(
        root, candidate.content_draft.content_draft_id, expected_revision=1
    )
    with pytest.raises(ProjectError, match="not recorded"):
        propose_content_draft(
            root, confirmed.content_draft.content_draft_id, expected_revision=2
        )
    assert ProjectStore(root).load().revision == 2
    assert not (root / "proposals").exists()


def test_multisource_source_blocks_preserve_block_and_ref_order(tmp_path: Path) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    candidate = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=_bindings("src_a", "src_b"),
        brief_id="brief_content",
        context_hash=_context_hash(root, 1, "src_a", "src_b"),
        blocks=[
            {
                "block_id": "block_b",
                "kind": "source_excerpt",
                "refs": [_ref("src_b", "tr_b")],
                "canonical_text": "乙素材第一句。",
            },
            _source_block(),
        ],
        expected_revision=1,
    )
    confirmed = confirm_content_draft(
        root, candidate.content_draft.content_draft_id, expected_revision=1
    )
    proposed = propose_content_draft(
        root, confirmed.content_draft.content_draft_id, expected_revision=2
    )
    assert proposed.proposal_schema_version == 2
    assert [clip.source_id for clip in proposed.proposal.clips] == ["src_b", "src_a"]
    assert [clip.display_text for clip in proposed.proposal.clips] == [
        "乙素材第一句。",
        "甲素材第一句。",
    ]


def test_content_draft_revision_reuses_clip_identity_across_reorder(
    tmp_path: Path,
) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    first_candidate = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=_bindings("src_a"),
        brief_id="brief_content",
        context_hash=_context_hash(root, 1, "src_a"),
        blocks=[
            _source_block(),
            {
                **_source_block("block_a2", text="甲素材第二句。"),
                "refs": [_ref("src_a", "tr_a", 2)],
            },
        ],
        expected_revision=1,
    )
    first_confirmed = confirm_content_draft(
        root, first_candidate.content_draft.content_draft_id, expected_revision=1
    )
    first_proposal = propose_content_draft(
        root, first_confirmed.content_draft.content_draft_id, expected_revision=2
    )
    confirm_edit_proposal(
        root, first_proposal.proposal.proposal_id, expected_revision=2
    )

    revised_candidate = create_content_draft(
        root,
        parent_draft_id=first_confirmed.content_draft.content_draft_id,
        source_bindings=_bindings("src_a"),
        brief_id="brief_content",
        context_hash=_context_hash(root, 3, "src_a"),
        blocks=[
            {
                **_source_block("block_a2", text="甲素材第二句。"),
                "refs": [_ref("src_a", "tr_a", 2)],
            },
            _source_block(),
        ],
        expected_revision=3,
    )
    revised_confirmed = confirm_content_draft(
        root, revised_candidate.content_draft.content_draft_id, expected_revision=3
    )
    revised_proposal = propose_content_draft(
        root, revised_confirmed.content_draft.content_draft_id, expected_revision=4
    )

    first_ids = [clip.clip_id for clip in first_proposal.proposal.clips]
    revised_ids = [clip.clip_id for clip in revised_proposal.proposal.clips]
    assert revised_ids == list(reversed(first_ids))
    assert [clip.segment_id for clip in revised_proposal.proposal.clips] == [
        "seg_src_a_2",
        "seg_src_a_1",
    ]


def test_source_excerpt_requires_one_contiguous_resolved_selection(tmp_path: Path) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    context_hash = _context_hash(root, 1, "src_a", "src_b")
    with pytest.raises(ProjectError, match="one Transcript"):
        create_content_draft(
            root,
            parent_draft_id=None,
            source_bindings=_bindings("src_a", "src_b"),
            brief_id="brief_content",
            context_hash=context_hash,
            blocks=[
                {
                    "block_id": "block_cross_source",
                    "kind": "source_excerpt",
                    "refs": [_ref("src_a", "tr_a"), _ref("src_b", "tr_b")],
                    "canonical_text": "甲素材第一句。\n乙素材第一句。",
                }
            ],
            expected_revision=1,
        )

    with pytest.raises(ProjectError, match="trusted fine-unit"):
        create_content_draft(
            root,
            parent_draft_id=None,
            source_bindings=_bindings("src_a"),
            brief_id="brief_content",
            context_hash=_context_hash(root, 1, "src_a"),
            blocks=[
                {
                    **_source_block(),
                    "refs": [{**_ref("src_a", "tr_a"), "start_ticks": 60_000}],
                }
            ],
            expected_revision=1,
        )


def test_source_excerpt_accepts_trusted_fine_unit_boundaries(tmp_path: Path) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    transcript_path = root / "transcripts" / "src_a" / "tr_a.json"
    transcript = _transcript("src_a", "tr_a", ("甲素材第一句。", "甲素材第二句。"))
    first = replace(
        transcript.segments[0],
        fine_units=(
            FineUnit("word", "甲素材", 0, 60_000, None),
            FineUnit("word", "第一句。", 60_000, 120_000, None),
        ),
    )
    transcript_path.unlink()
    write_new_json(
        transcript_path,
        replace(transcript, segments=(first, transcript.segments[1])).to_dict(),
    )

    candidate = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=_bindings("src_a"),
        brief_id="brief_content",
        context_hash=_context_hash(root, 1, "src_a"),
        blocks=[
            {
                **_source_block(text="第一句。"),
                "refs": [
                    {
                        **_ref("src_a", "tr_a"),
                        "start_ticks": 60_000,
                    }
                ],
            }
        ],
        expected_revision=1,
    )
    assert candidate.content_draft.blocks[0].canonical_text == "第一句。"

    with pytest.raises(ProjectError, match="must not skip"):
        create_content_draft(
            root,
            parent_draft_id=None,
            source_bindings=_bindings("src_a"),
            brief_id="brief_content",
            context_hash=_context_hash(root, 1, "src_a"),
            blocks=[
                {
                    "block_id": "block_skips_tail",
                    "kind": "source_excerpt",
                    "refs": [
                        {**_ref("src_a", "tr_a"), "end_ticks": 60_000},
                        _ref("src_a", "tr_a", 2),
                    ],
                    "canonical_text": "甲素材\n甲素材第二句。",
                }
            ],
            expected_revision=1,
        )


def test_transcript_correction_allows_exact_recorded_narration_and_multisource_proposal(
    tmp_path: Path,
) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    mismatch_block = {
        "block_id": "block_recorded",
        "kind": "narration",
        "text": "批准旁白。",
        "status": "recorded",
        "recorded_refs": [_ref("src_n", "tr_n")],
    }
    with pytest.raises(ProjectError, match="narration text"):
        create_content_draft(
            root,
            parent_draft_id=None,
            source_bindings=_bindings("src_a", "src_n"),
            brief_id="brief_content",
            context_hash=_context_hash(root, 1, "src_a", "src_n"),
            blocks=[_source_block(), mismatch_block],
            expected_revision=1,
        )

    corrected = correct_transcript(
        root,
        source_id="src_n",
        parent_transcript_version_id="tr_n",
        corrections=[{"segment_id": "seg_src_n_1", "corrected_text": "批准旁白。"}],
        expected_revision=1,
    )
    child_id = corrected.transcript.transcript_version_id
    bindings = _bindings(
        "src_a",
        "src_n",
        transcript_ids={"src_a": "tr_a", "src_n": child_id},
    )
    candidate = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=bindings,
        brief_id="brief_content",
        context_hash=_context_hash(root, 2, "src_a", "src_n"),
        blocks=[
            _source_block(),
            {
                **mismatch_block,
                "recorded_refs": [_ref("src_n", child_id)],
            },
        ],
        expected_revision=2,
    )
    confirmed = confirm_content_draft(
        root, candidate.content_draft.content_draft_id, expected_revision=2
    )
    proposed = propose_content_draft(
        root, confirmed.content_draft.content_draft_id, expected_revision=3
    )
    assert proposed.proposal_schema_version == 2
    assert [clip.source_id for clip in proposed.proposal.clips] == ["src_a", "src_n"]
    assert [clip.display_text for clip in proposed.proposal.clips] == [
        "甲素材第一句。",
        "批准旁白。",
    ]
    assert ProjectStore(root).load().revision == 3


def test_recorded_narration_rejects_refs_spanning_transcripts(tmp_path: Path) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    with pytest.raises(ProjectError, match="one Transcript"):
        create_content_draft(
            root,
            parent_draft_id=None,
            source_bindings=_bindings("src_a", "src_b"),
            brief_id="brief_content",
            context_hash=_context_hash(root, 1, "src_a", "src_b"),
            blocks=[
                {
                    "block_id": "block_recorded",
                    "kind": "narration",
                    "text": "甲素材第一句。\n乙素材第一句。",
                    "status": "recorded",
                    "recorded_refs": [
                        _ref("src_a", "tr_a"),
                        _ref("src_b", "tr_b"),
                    ],
                }
            ],
            expected_revision=1,
        )
    assert not (root / "content-drafts").exists()


def test_transcript_activation_change_makes_existing_draft_stale(tmp_path: Path) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    candidate = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=_bindings("src_a"),
        brief_id="brief_content",
        context_hash=_context_hash(root, 1, "src_a"),
        blocks=[_source_block()],
        expected_revision=1,
    )

    correct_transcript(
        root,
        source_id="src_a",
        parent_transcript_version_id="tr_a",
        corrections=[
            {"segment_id": "seg_src_a_1", "corrected_text": "甲素材第一句校正。"}
        ],
        expected_revision=1,
    )

    stale = read_content_draft(root, candidate.content_draft.content_draft_id)
    assert stale.status == "stale"
    assert {"project_revision", "active_transcript"} <= set(stale.stale_reasons)


def test_confirm_project_save_failure_removes_child_and_preserves_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    candidate = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=_bindings("src_a"),
        brief_id="brief_content",
        context_hash=_context_hash(root, 1, "src_a"),
        blocks=[_source_block()],
        expected_revision=1,
    )
    candidate_path = root / "content-drafts" / f"{candidate.content_draft.content_draft_id}.json"
    before_project = (root / "project.json").read_bytes()
    before_candidate = candidate_path.read_bytes()

    def fail_save(*args: object, **kwargs: object) -> None:
        raise OSError("project save failed")

    monkeypatch.setattr(ProjectStore, "save", fail_save)
    with pytest.raises(OSError, match="project save failed"):
        confirm_content_draft(
            root, candidate.content_draft.content_draft_id, expected_revision=1
        )
    assert (root / "project.json").read_bytes() == before_project
    assert candidate_path.read_bytes() == before_candidate
    assert list((root / "content-drafts").glob("*.json")) == [candidate_path]


def test_candidate_and_confirmed_drafts_report_revision_brief_binding_and_context_staleness(
    tmp_path: Path,
) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    candidate = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=_bindings("src_a"),
        brief_id="brief_content",
        context_hash=_context_hash(root, 1, "src_a"),
        blocks=[_source_block()],
        expected_revision=1,
    )
    update_source_metadata(
        root,
        source_id="src_a",
        tags=["changed"],
        note="changed",
        expected_revision=1,
    )
    stale_metadata = read_content_draft(root, candidate.content_draft.content_draft_id)
    assert stale_metadata.status == "stale"
    assert {"project_revision", "context_hash"} <= set(stale_metadata.stale_reasons)

    fresh = create_content_draft(
        root,
        parent_draft_id=candidate.content_draft.content_draft_id,
        source_bindings=_bindings("src_a"),
        brief_id="brief_content",
        context_hash=_context_hash(root, 2, "src_a"),
        blocks=[_source_block()],
        expected_revision=2,
    )
    create_edit_brief(
        root,
        theme="新主题",
        target_duration_ticks=1_200_000,
        focus=["新重点"],
        allow_reorder=True,
        expected_revision=2,
    )
    stale_brief = read_content_draft(root, fresh.content_draft.content_draft_id)
    assert stale_brief.status == "stale"
    assert {"project_revision", "brief"} <= set(stale_brief.stale_reasons)


def test_create_rejects_stale_revision_hash_duplicate_blocks_and_unknown_parent(
    tmp_path: Path,
) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    good_hash = _context_hash(root, 1, "src_a")
    cases = [
        {"expected_revision": 0, "context_hash": good_hash, "parent": None, "blocks": [_source_block()]},
        {"expected_revision": 1, "context_hash": "0" * 64, "parent": None, "blocks": [_source_block()]},
        {
            "expected_revision": 1,
            "context_hash": good_hash,
            "parent": None,
            "blocks": [_source_block(), _source_block()],
        },
        {
            "expected_revision": 1,
            "context_hash": good_hash,
            "parent": "draft_missing",
            "blocks": [_source_block()],
        },
    ]
    for case in cases:
        with pytest.raises(ProjectError):
            create_content_draft(
                root,
                parent_draft_id=case["parent"],  # type: ignore[arg-type]
                source_bindings=_bindings("src_a"),
                brief_id="brief_content",
                context_hash=case["context_hash"],  # type: ignore[arg-type]
                blocks=case["blocks"],  # type: ignore[arg-type]
                expected_revision=case["expected_revision"],  # type: ignore[arg-type]
            )
    assert not (root / "content-drafts").exists()


def test_existing_decision_rejects_binding_expansion_with_rebaseline_error(
    tmp_path: Path,
) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    context_hash = _context_hash(root, 1, "src_a")
    proposal = create_edit_proposal(
        root,
        source_id="src_a",
        transcript_version_id="tr_a",
        brief_id="brief_content",
        context_hash=context_hash,
        clips=[
            {
                "clip_id": "clip_a",
                "source_id": "src_a",
                "transcript_version_id": "tr_a",
                "segment_id": "seg_src_a_1",
                "source_in_ticks": 0,
                "source_out_ticks": 120_000,
                "reason": "fixture",
                "display_text": "甲素材第一句。",
            }
        ],
        total_duration_ticks=120_000,
        expected_revision=1,
    )
    confirm_edit_proposal(root, proposal.proposal.proposal_id, expected_revision=1)
    with pytest.raises(ProjectError, match="rebaseline"):
        create_content_draft(
            root,
            parent_draft_id=None,
            source_bindings=_bindings("src_a", "src_b"),
            brief_id="brief_content",
            context_hash=_context_hash(root, 2, "src_a", "src_b"),
            blocks=[_source_block()],
            expected_revision=2,
        )
    assert ProjectStore(root).load().revision == 2


def test_artifact_write_failures_leave_no_candidate_or_confirmed_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    import roughcut.application.content_drafts as drafts

    def fail_write(*args: object, **kwargs: object) -> None:
        raise OSError("artifact write failed")

    monkeypatch.setattr(drafts, "write_new_json", fail_write)
    with pytest.raises(OSError, match="artifact write failed"):
        create_content_draft(
            root,
            parent_draft_id=None,
            source_bindings=_bindings("src_a"),
            brief_id="brief_content",
            context_hash=_context_hash(root, 1, "src_a"),
            blocks=[_source_block()],
            expected_revision=1,
        )
    assert not (root / "content-drafts").exists()
    monkeypatch.undo()

    candidate = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=_bindings("src_a"),
        brief_id="brief_content",
        context_hash=_context_hash(root, 1, "src_a"),
        blocks=[_source_block()],
        expected_revision=1,
    )
    candidate_path = root / "content-drafts" / f"{candidate.content_draft.content_draft_id}.json"
    monkeypatch.setattr(drafts, "write_new_json", fail_write)
    with pytest.raises(OSError, match="artifact write failed"):
        confirm_content_draft(
            root, candidate.content_draft.content_draft_id, expected_revision=1
        )
    assert ProjectStore(root).load().revision == 1
    assert list((root / "content-drafts").glob("*.json")) == [candidate_path]


def test_schema2_heading_is_not_compiled_into_proposal_media(tmp_path: Path) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    created = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=_bindings("src_a"),
        brief_id="brief_content",
        context_hash=_context_hash(root, 1, "src_a"),
        blocks=[
            {"block_id": "section_a", "kind": "section_title", "title": "开场"},
            _source_block(),
            {"block_id": "section_empty", "kind": "section_title", "title": "空章"},
        ],
        expected_revision=1,
    )
    assert created.content_draft.schema_version == 2
    assert ContentDraft.from_dict(
        read_content_draft(root, created.content_draft.content_draft_id).content_draft.to_dict()
    ).schema_version == 2
    confirmed = confirm_content_draft(
        root, created.content_draft.content_draft_id, expected_revision=1
    )
    proposed = propose_content_draft(
        root, confirmed.content_draft.content_draft_id, expected_revision=2
    )
    assert len(proposed.proposal.clips) == 1
    assert proposed.proposal.total_duration_ticks == 120_000


def test_schema1_editor_child_projects_once_and_leaves_parent_json_unchanged(
    tmp_path: Path,
) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    parent = ContentDraft(
        content_draft_id="draft_schema1_parent",
        parent_draft_id=None,
        base_project_revision=1,
        confirmed_by_user=False,
        brief_snapshot=state["brief"],  # type: ignore[arg-type]
        source_bindings=(SourceTranscriptBinding("src_a", "tr_a"),),
        context_hash=_context_hash(root, 1, "src_a"),
        blocks=(
            SourceExcerptBlock(
                "block_a",
                (ContentDraftRef("src_a", "tr_a", "seg_src_a_1", 0, 120_000),),
                "甲素材第一句。",
                section_title="第一章",
            ),
        ),
        schema_version=1,
    )
    write_new_json(root / "content-drafts" / f"{parent.content_draft_id}.json", parent.to_dict())
    before = (root / "content-drafts" / f"{parent.content_draft_id}.json").read_bytes()
    child = create_content_draft_editor_child(
        root,
        parent=parent,
        blocks=parent.blocks,
        expected_revision=1,
    )
    after = (root / "content-drafts" / f"{parent.content_draft_id}.json").read_bytes()
    assert child.content_draft.schema_version == 2
    assert child.content_draft.parent_draft_id == parent.content_draft_id
    assert child.content_draft.blocks[0].kind == "section_title"
    assert before == after
    assert len(list((root / "content-drafts").glob("*.json"))) == 2


def test_schema1_scoped_revision_compares_against_projected_parent(
    tmp_path: Path,
) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    parent = ContentDraft(
        content_draft_id="draft_schema1_scoped",
        parent_draft_id=None,
        base_project_revision=1,
        confirmed_by_user=False,
        brief_snapshot=state["brief"],  # type: ignore[arg-type]
        source_bindings=(SourceTranscriptBinding("src_a", "tr_a"),),
        context_hash=_context_hash(root, 1, "src_a"),
        blocks=(
            SourceExcerptBlock(
                "block_a",
                (ContentDraftRef("src_a", "tr_a", "seg_src_a_1", 0, 120_000),),
                "甲素材第一句。",
                section_title="第一章",
            ),
        ),
        schema_version=1,
    )
    write_new_json(root / "content-drafts" / f"{parent.content_draft_id}.json", parent.to_dict())

    revised = revise_content_draft_scoped(
        root,
        parent_draft_id=parent.content_draft_id,
        mutable_block_ids=["block_a"],
        blocks=[
            {
                "block_id": legacy_section_block_id(
                    parent.content_draft_id, "block_a", 0
                ),
                "kind": "section_title",
                "title": "第一章",
            },
            {
                "block_id": "block_a",
                "kind": "source_excerpt",
                "refs": [
                    {
                        "source_id": "src_a",
                        "transcript_version_id": "tr_a",
                        "segment_id": "seg_src_a_2",
                        "start_ticks": 120_000,
                        "end_ticks": 240_000,
                    }
                ],
                "canonical_text": "甲素材第二句。",
            }
        ],
        expected_revision=1,
    )
    assert revised.content_draft.schema_version == 2
    assert revised.content_draft.parent_draft_id == parent.content_draft_id
    assert revised.changed


def test_schema1_create_child_projects_heading_without_rewriting_parent(
    tmp_path: Path,
) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    parent = ContentDraft(
        content_draft_id="draft_schema1_create",
        parent_draft_id=None,
        base_project_revision=1,
        confirmed_by_user=False,
        brief_snapshot=state["brief"],  # type: ignore[arg-type]
        source_bindings=(SourceTranscriptBinding("src_a", "tr_a"),),
        context_hash=_context_hash(root, 1, "src_a"),
        blocks=(
            SourceExcerptBlock(
                "block_a",
                (ContentDraftRef("src_a", "tr_a", "seg_src_a_1", 0, 120_000),),
                "甲素材第一句。",
                section_title="第一章",
            ),
        ),
        schema_version=1,
    )
    write_new_json(root / "content-drafts" / f"{parent.content_draft_id}.json", parent.to_dict())
    before = (root / "content-drafts" / f"{parent.content_draft_id}.json").read_bytes()
    project = ProjectStore(root).load()
    created = create_content_draft(
        root,
        parent_draft_id=parent.content_draft_id,
        source_bindings=[{"source_id": "src_a", "transcript_version_id": "tr_a"}],
        brief_id=parent.brief_snapshot.brief_id,
        context_hash=parent.context_hash,
        blocks=[
            {
                "block_id": "block_a",
                "kind": "source_excerpt",
                "refs": [
                    {
                        "source_id": "src_a",
                        "transcript_version_id": "tr_a",
                        "segment_id": "seg_src_a_1",
                        "start_ticks": 0,
                        "end_ticks": 120_000,
                    }
                ],
                "canonical_text": "甲素材第一句。",
            }
        ],
        expected_revision=project.revision,
    )
    assert created.content_draft.schema_version == 2
    assert created.content_draft.blocks[0].kind == "section_title"
    assert (root / "content-drafts" / f"{parent.content_draft_id}.json").read_bytes() == before


def _duration_candidate(target_duration_ticks: int, durations: tuple[int, ...]) -> ContentDraft:
    blocks: tuple[SourceExcerptBlock | SectionTitleBlock, ...] = tuple(
        SourceExcerptBlock(
            block_id=f"block_{index}",
            refs=(
                ContentDraftRef(
                    "src_a",
                    "tr_a",
                    f"seg_{index}",
                    sum(durations[:index]),
                    sum(durations[: index + 1]),
                ),
            ),
            canonical_text="原话",
        )
        for index in range(len(durations))
    )
    if not blocks:
        blocks = (SectionTitleBlock("heading_only", "空章节"),)
    return ContentDraft(
        content_draft_id="draft_duration",
        parent_draft_id=None,
        base_project_revision=1,
        confirmed_by_user=False,
        brief_snapshot=EditBrief(
            "brief_duration",
            "时长测试",
            target_duration_ticks,
            ("精确引用",),
            True,
        ),
        source_bindings=(SourceTranscriptBinding("src_a", "tr_a"),),
        context_hash="0" * 64,
        blocks=blocks,
        schema_version=2,
    )


@pytest.mark.parametrize(
    ("target_duration_ticks", "durations", "status"),
    [
        (100, (100,), "within_target"),
        (99, (108,), "within_target"),
        (99, (109,), "over_target"),
        (100, (99,), "under_target"),
        (100, (), "under_target"),
        (100, (50, 50), "within_target"),
    ],
)
def test_duration_acceptance_summary_uses_exact_ref_sum_and_integer_boundaries(
    target_duration_ticks: int, durations: tuple[int, ...], status: str
) -> None:
    summary = duration_acceptance_summary(
        _duration_candidate(target_duration_ticks, durations)
    )

    assert summary["target_duration_ticks"] == target_duration_ticks
    assert summary["actual_duration_ticks"] == sum(durations)
    assert summary["delta_ticks"] == sum(durations) - target_duration_ticks
    assert summary["status"] == status
    assert summary["tolerance_ticks"] == (target_duration_ticks * 10) // 100
    assert summary["accepted_upper_bound_ticks"] == target_duration_ticks + (
        target_duration_ticks * 10
    ) // 100


def test_duration_acceptance_public_readback_rederives_for_ref_changes_and_is_deterministic(
    tmp_path: Path,
) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])  # type: ignore[arg-type]
    candidate = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=_bindings("src_a"),
        brief_id="brief_content",
        context_hash=_context_hash(root, 1, "src_a"),
        blocks=[_source_block()],
        expected_revision=1,
    )
    initial = read_content_draft(root, candidate.content_draft.content_draft_id)
    initial_payload = initial.to_dict()
    assert initial_payload["duration_acceptance"] == {
        "target_duration_ticks": 1_200_000,
        "actual_duration_ticks": 120_000,
        "delta_ticks": -1_080_000,
        "status": "under_target",
        "tolerance_ticks": 120_000,
        "accepted_upper_bound_ticks": 1_320_000,
    }
    assert initial_payload == initial.to_dict()

    revised = create_content_draft(
        root,
        parent_draft_id=candidate.content_draft.content_draft_id,
        source_bindings=_bindings("src_a"),
        brief_id="brief_content",
        context_hash=_context_hash(root, 1, "src_a"),
        blocks=[
            _source_block(),
            {
                "block_id": "block_b",
                "kind": "source_excerpt",
                "refs": [_ref("src_a", "tr_a", 2)],
                "canonical_text": "甲素材第二句。",
            },
        ],
        expected_revision=1,
    )
    revised_payload = read_content_draft(
        root, revised.content_draft.content_draft_id
    ).to_dict()
    assert revised_payload["duration_acceptance"]["actual_duration_ticks"] == 240_000
    assert revised_payload["duration_acceptance"]["delta_ticks"] == -960_000

    correct_transcript(
        root,
        source_id="src_a",
        parent_transcript_version_id="tr_a",
        corrections=[
            {"segment_id": "seg_src_a_1", "corrected_text": "甲素材第一句校正。"}
        ],
        expected_revision=1,
    )
    stale_payload = read_content_draft(
        root, candidate.content_draft.content_draft_id
    ).to_dict()
    assert stale_payload["status"] == "stale"
    assert stale_payload["duration_acceptance"] == initial_payload["duration_acceptance"]
