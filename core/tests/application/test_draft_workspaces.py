from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from test_workflows import (
    _advance_to_draft_review,
    _current_draft_context,
    _draft_ref_from_mutation,
    _prepare_rebase_input,
    _submit_basic_draft,
    _workflow_project,
)

import roughcut.adapters.draft_workspace_store as checkpoint_store_module
import roughcut.application.draft_workspaces as draft_workspaces_module
from roughcut.adapters.artifact_store import write_new_json
from roughcut.adapters.project_store import ProjectStore
from roughcut.adapters.workflow_store import WorkflowStore
from roughcut.application.content_drafts import prepare_content_draft_editor_child
from roughcut.application.draft_editor import (
    edit_draft_candidate,
    load_draft_editor_snapshot,
    prepare_draft_candidate,
    resolve_draft_editor_caret,
    resolve_draft_editor_selection,
)
from roughcut.application.draft_workspaces import (
    DraftWorkspaceState,
    edit_draft_workspace,
    edit_draft_workspace_narration,
    edit_draft_workspace_punctuation,
    edit_draft_workspace_section,
    initialize_draft_workspace,
    open_draft_workspace,
    read_draft_workspace,
    redo_draft_workspace,
    reset_draft_workspace_after_return,
    select_draft_workspace_candidate,
    undo_draft_workspace,
)
from roughcut.application.workflows import workflow_action
from roughcut.domain.content_draft import ContentDraft, NarrationBlock, SourceExcerptBlock
from roughcut.domain.draft_workspace import DraftWorkspaceCheckpoint, DraftWorkspaceCheckpointRef
from roughcut.domain.errors import WorkflowError
from roughcut.domain.project import ProjectError
from roughcut.domain.transcript import (
    FineUnit,
    TimedTranscript,
    TranscriptProvenance,
    TranscriptSegment,
)
from roughcut.domain.workflow import (
    ArtifactRef,
    ReceiptRef,
    canonical_sha256_v1,
    subject_content_hash,
)


def _schema2_blocks(count: int) -> list[dict[str, object]]:
    blocks: list[dict[str, object]] = []
    texts = ("开场。", "图书馆。", "实验室。")
    for index, text in enumerate(texts[:count], start=1):
        blocks.extend(
            [
                {
                    "block_id": f"section_{index}",
                    "kind": "section_title",
                    "title": f"章节 {index}",
                },
                {
                    "block_id": f"block_{index}",
                    "kind": "source_excerpt",
                    "refs": [
                        {
                            "source_id": "src_a",
                            "transcript_version_id": "tr_a",
                            "segment_id": f"seg_{index}",
                            "start_ticks": (index - 1) * 120_000,
                            "end_ticks": index * 120_000,
                        }
                    ],
                    "canonical_text": text,
                },
            ]
        )
    return blocks


def _workspace_fixture(tmp_path: Path) -> tuple[Path, object, ArtifactRef]:
    root = _workflow_project(tmp_path)
    draft_review, _brief = _advance_to_draft_review(root)
    context_hash = _current_draft_context(root)[2]
    brief_ref = draft_review.workflow_run.artifact_refs["brief"]
    assert brief_ref is not None
    first = workflow_action(
        root,
        "wfr_test",
        "act_workspace_parent",
        "submit_draft",
        {
            "schema_version": 1,
            "parent_draft_ref": None,
            "display_title": "校园探访",
            "source_bindings": [
                {"source_id": "src_a", "transcript_version_id": "tr_a"}
            ],
            "brief_ref": brief_ref.to_dict(),
            "context_hash": context_hash,
            "blocks": _schema2_blocks(1),
            "scoped_mutable_block_ids": [],
        },
    )
    parent_ref = _draft_ref_from_mutation(first)
    _run, child_brief_ref, child_context_hash = _current_draft_context(root)
    child = workflow_action(
        root,
        "wfr_test",
        "act_workspace_child",
        "submit_draft",
        {
            "schema_version": 1,
            "parent_draft_ref": parent_ref,
            "display_title": "校园探访",
            "source_bindings": [
                {"source_id": "src_a", "transcript_version_id": "tr_a"}
            ],
            "brief_ref": child_brief_ref.to_dict(),
            "context_hash": child_context_hash,
            "blocks": _schema2_blocks(3),
            "scoped_mutable_block_ids": [],
        },
    )
    anchor = first.workflow_run.artifact_refs["content_draft"]
    assert anchor is not None
    child_ref = ArtifactRef.from_dict(_draft_ref_from_mutation(child))
    return root, first, child_ref


def _workspace_for_transcript(
    tmp_path: Path,
    transcript: TimedTranscript,
    blocks: list[dict[str, object]],
    action_id: str,
) -> tuple[Path, ArtifactRef]:
    root = _workflow_project(tmp_path)
    project = ProjectStore(root).load()
    write_new_json(
        root / "transcripts" / "src_a" / f"{transcript.transcript_version_id}.json",
        transcript.to_dict(),
    )
    ProjectStore(root).save(
        replace(
            project,
            revision=project.revision + 1,
            active_transcript_versions={"src_a": transcript.transcript_version_id},
        ),
        expected_revision=project.revision,
    )
    _advance_to_draft_review(root)
    run, brief_ref, context_hash = _current_draft_context(root)
    assert brief_ref is not None
    result = workflow_action(
        root,
        "wfr_test",
        action_id,
        "submit_draft",
        {
            "schema_version": 1,
            "parent_draft_ref": None,
            "display_title": "边界测试",
            "source_bindings": [
                {
                    "source_id": binding.source_id,
                    "transcript_version_id": binding.transcript_version_id,
                }
                for binding in run.ordered_bindings
            ],
            "brief_ref": brief_ref.to_dict(),
            "context_hash": context_hash,
            "blocks": blocks,
            "scoped_mutable_block_ids": [],
        },
    )
    return root, ArtifactRef.from_dict(_draft_ref_from_mutation(result))


def _gap_workspace_fixture(tmp_path: Path) -> tuple[Path, ArtifactRef]:
    transcript = TimedTranscript(
        schema_version=1,
        transcript_version_id="tr_gap",
        source_id="src_a",
        parent_version_id=None,
        provenance=TranscriptProvenance(
            "fixture",
            "1",
            {},
            {},
            "raw-asr/src_a/gap.json",
            "fixture",
            "fixture",
            0,
        ),
        language="zh-CN",
        segments=(
            TranscriptSegment(
                segment_id="seg_gap",
                start_ticks=100,
                end_ticks=500,
                original_text="甲，乙 丙。",
                corrected_text=None,
                local_speaker_id=None,
                person_id=None,
                confidence=None,
                fine_units=(
                    FineUnit("character", "甲", 100, 200, None),
                    FineUnit("character", " ", 200, 250, None),
                    FineUnit("character", "乙", 250, 350, None),
                    FineUnit("character", " ", 350, 380, None),
                    FineUnit("character", "丙", 380, 480, None),
                ),
                editorial_mark="unmarked",
            ),
        ),
    )
    return _workspace_for_transcript(
        tmp_path,
        transcript,
        [
                {
                    "block_id": "gap_section",
                    "kind": "section_title",
                    "title": "原章节",
                },
                {
                    "block_id": "gap_block",
                    "kind": "source_excerpt",
                    "refs": [
                        {
                            "source_id": "src_a",
                            "transcript_version_id": "tr_gap",
                            "segment_id": "seg_gap",
                            "start_ticks": 100,
                            "end_ticks": 500,
                        }
                    ],
                    "canonical_text": "甲，乙 丙。",
                },
        ],
        "act_gap_workspace_parent",
    )


def _ownership_workspace_fixture(
    tmp_path: Path,
    case: str,
    *,
    include_owner: bool = True,
) -> tuple[Path, ArtifactRef]:
    segment_specs = (
        [
            ("a01", 0, 300_000, "前段区。", (
                FineUnit("word", "前段", 0, 180_000, None),
                FineUnit("character", "区", 270_000, 285_000, None),
                FineUnit("character", "。", 285_000, 300_000, None),
            )),
            ("a02", 300_000, 422_727, "主持人说：“请大家", (
                FineUnit("word", "主持人说：“请大家", 300_000, 422_727, None),
            )),
            ("a03", 422_727, 500_000, "落点", (
                FineUnit("word", "落点", 422_727, 500_000, None),
            )),
        ]
        if case == "a"
        else [
            ("b01", 0, 280_000, "前文", (FineUnit("word", "前文", 0, 280_000, None),)),
            ("b02", 300_000, 600_000, "前情时间学习。", (
                FineUnit("word", "前情", 300_000, 500_000, None),
                FineUnit("character", "时", 528_571, 542_857, None),
                FineUnit("character", "间", 542_857, 557_143, None),
                FineUnit("character", "学", 557_143, 571_429, None),
                FineUnit("character", "习", 571_429, 585_714, None),
                FineUnit("character", "。", 585_714, 600_000, None),
            )),
        ]
    )
    segments = tuple(
        TranscriptSegment(item, start, end, text, None, None, None, None, units, "unmarked")
        for item, start, end, text, units in segment_specs
    )
    transcript = TimedTranscript(
        1,
        "tr_ownership",
        "src_a",
        None,
        TranscriptProvenance(
            "fixture", "1", {}, {}, "raw-asr/src_a/ownership.json", "fixture", "fixture", 0
        ),
        "zh-CN",
        segments,
    )

    specs = (
        [
            ("block_edit_p_aaaaaaaaaaaaaaaaaaaaaaaa_r_0000000000000001", "a01", 270_000, 300_000, "区。"),
            ("block_edit_p_aaaaaaaaaaaaaaaaaaaaaaaa_r_0000000000000002", "a02", 300_000, 422_727, "主持人说：“请大家"),
            ("block_edit_p_cccccccccccccccccccccccc_r_0000000000000003", "a03", 422_727, 500_000, "落点"),
        ]
        if case == "a"
        else [
            ("block_edit_p_bbbbbbbbbbbbbbbbbbbbbbbb_r_0000000000000001", "b01", 0, 280_000, "前文"),
            ("block_edit_p_bbbbbbbbbbbbbbbbbbbbbbbb_r_0000000000000002", "b02", 528_571, 600_000, "时间学习。"),
        ][0 if include_owner else 1 :]
    )
    blocks = [
        {
            "block_id": block_id,
            "kind": "source_excerpt",
            "refs": [{"source_id": "src_a", "transcript_version_id": "tr_ownership", "segment_id": segment_id, "start_ticks": start, "end_ticks": end}],
            "canonical_text": text,
        }
        for block_id, segment_id, start, end, text in specs
    ]
    return _workspace_for_transcript(
        tmp_path,
        transcript,
        blocks,
        "act_ownership_workspace_parent",
    )


def _select_child(root: Path, child_ref: ArtifactRef):
    initialized = initialize_draft_workspace(
        root,
        run_id="wfr_test",
        operation_id="dwop_0_00000000000000000000000000000001",
    )
    selected = select_draft_workspace_candidate(
        root,
        run_id="wfr_test",
        operation_id="dwop_1_00000000000000000000000000000002",
        expected_checkpoint_ref=initialized.checkpoint_ref,
        expected_current_candidate_ref=initialized.checkpoint.current_candidate_ref,
        child_candidate_ref=child_ref,
    )
    return initialized, selected


def test_open_existing_workspace_is_read_only_exact_readback(tmp_path: Path) -> None:
    root = _workflow_project(tmp_path)
    first = _submit_basic_draft(root, action_id="act_workspace_readback")
    initialized = initialize_draft_workspace(
        root,
        run_id="wfr_test",
        operation_id="dwop_0_00000000000000000000000000000051",
    )
    path = root / "workflow" / "draft-workspaces" / "wfr_test.json"
    before = path.read_bytes()

    opened = open_draft_workspace(
        root,
        run_id="wfr_test",
        audit_review_session_id="review_session_restarted",
    )

    anchor = first.workflow_run.artifact_refs["content_draft"]
    assert anchor is not None
    assert opened.checkpoint == initialized.checkpoint
    assert opened.current_candidate.content_draft_id == anchor.artifact_id
    assert path.read_bytes() == before


def test_open_reconciles_exact_same_basis_external_submit_child(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    first = _submit_basic_draft(root, action_id="act_external_submit_parent")
    anchor_ref = ArtifactRef.from_dict(_draft_ref_from_mutation(first))
    initialized = initialize_draft_workspace(
        root,
        run_id="wfr_test",
        operation_id="dwop_0_00000000000000000000000000000071",
    )
    run, brief_ref, context_hash = _current_draft_context(root)
    assert brief_ref is not None
    child_result = workflow_action(
        root,
        "wfr_test",
        "act_external_submit_child",
        "submit_draft",
        {
            "schema_version": 1,
            "parent_draft_ref": anchor_ref.to_dict(),
            "display_title": "校园探访",
            "source_bindings": [
                {
                    "source_id": binding.source_id,
                    "transcript_version_id": binding.transcript_version_id,
                }
                for binding in run.ordered_bindings
            ],
            "brief_ref": brief_ref.to_dict(),
            "context_hash": context_hash,
            "blocks": _schema2_blocks(2),
            "scoped_mutable_block_ids": [],
        },
    )
    child_ref = ArtifactRef.from_dict(_draft_ref_from_mutation(child_result))
    run_after_submit = WorkflowStore(root).read_run("wfr_test")
    assert run_after_submit.artifact_refs["content_draft"] == anchor_ref
    assert run_after_submit.last_receipt_ref == child_result.workflow_run.last_receipt_ref

    opened = open_draft_workspace(root, run_id="wfr_test")

    assert opened.current_candidate.content_draft_id == child_ref.artifact_id
    assert opened.checkpoint.current_candidate_ref == child_ref
    assert opened.checkpoint.generation == initialized.checkpoint.generation + 1
    assert opened.checkpoint.redo_candidate_refs == ()
    assert opened.checkpoint.last_commit.operation == "external_submit_advance"
    assert WorkflowStore(root).read_run("wfr_test").artifact_refs["content_draft"] == (
        anchor_ref
    )

    checkpoint_path = root / "workflow" / "draft-workspaces" / "wfr_test.json"
    checkpoint_bytes = checkpoint_path.read_bytes()
    reopened = open_draft_workspace(root, run_id="wfr_test")
    assert reopened.readback is True
    assert reopened.checkpoint == opened.checkpoint
    assert checkpoint_path.read_bytes() == checkpoint_bytes


def _first_paragraph_selection(root: Path, candidate_id: str):
    snapshot = load_draft_editor_snapshot(
        root,
        source_bindings=[
            {"source_id": "src_a", "transcript_version_id": "tr_a"}
        ],
        content_draft_id=candidate_id,
    )
    paragraph = snapshot.paragraphs[0]
    selection = resolve_draft_editor_selection(
        snapshot,
        surface="draft",
        anchor={
            "paragraph_id": paragraph["paragraph_id"],
            "offset": 0,
            "offset_encoding": "codepoint",
        },
        focus={
            "paragraph_id": paragraph["paragraph_id"],
            "offset": len(str(paragraph["text"])),
            "offset_encoding": "codepoint",
        },
    )
    return selection


def test_initialize_select_edit_undo_redo_and_restart_are_checkpoint_backed(
    tmp_path: Path,
) -> None:
    root, _first, child_ref = _workspace_fixture(tmp_path)
    project_before = ProjectStore(root).load()
    run_before = WorkflowStore(root).read_run("wfr_test")
    approval_files = set((root / "workflow" / "approvals").iterdir())
    receipt_files = set((root / "workflow" / "receipts").iterdir())
    transaction_files = set((root / "workflow" / "transactions").iterdir())
    initialized, selected = _select_child(root, child_ref)
    selection = _first_paragraph_selection(root, child_ref.artifact_id)

    edited = edit_draft_workspace(
        root,
        run_id="wfr_test",
        operation_id="dwop_2_00000000000000000000000000000003",
        expected_checkpoint_ref=selected.checkpoint_ref,
        expected_current_candidate_ref=child_ref,
        operation="delete",
        selection=selection,
        caret=None,
        accept_degraded=True,
    )
    assert edited.checkpoint.generation == 3
    assert edited.current_candidate.parent_draft_id == child_ref.artifact_id
    assert edited.checkpoint.redo_candidate_refs == ()

    undone = undo_draft_workspace(
        root,
        run_id="wfr_test",
        operation_id="dwop_3_00000000000000000000000000000004",
        expected_checkpoint_ref=edited.checkpoint_ref,
        expected_current_candidate_ref=edited.checkpoint.current_candidate_ref,
    )
    assert undone.checkpoint.current_candidate_ref == child_ref
    assert undone.checkpoint.redo_candidate_refs == (
        edited.checkpoint.current_candidate_ref,
    )

    restarted = read_draft_workspace(root, run_id="wfr_test")
    assert restarted is not None
    assert restarted.checkpoint == undone.checkpoint

    redone = redo_draft_workspace(
        root,
        run_id="wfr_test",
        operation_id="dwop_4_00000000000000000000000000000005",
        expected_checkpoint_ref=undone.checkpoint_ref,
        expected_current_candidate_ref=child_ref,
    )
    assert redone.checkpoint.current_candidate_ref == (
        edited.checkpoint.current_candidate_ref
    )
    assert redone.checkpoint.redo_candidate_refs == ()
    assert ProjectStore(root).load() == project_before
    assert WorkflowStore(root).read_run("wfr_test") == run_before
    assert set((root / "workflow" / "approvals").iterdir()) == approval_files
    assert set((root / "workflow" / "receipts").iterdir()) == receipt_files
    assert set((root / "workflow" / "transactions").iterdir()) == transaction_files
    assert initialized.current_candidate.content_draft_id != child_ref.artifact_id


def test_punctuation_workspace_commit_is_one_child_one_checkpoint_and_reversible(
    tmp_path: Path,
) -> None:
    root, _first, child_ref = _workspace_fixture(tmp_path)
    _initialized, selected = _select_child(root, child_ref)
    snapshot = load_draft_editor_snapshot(
        root,
        source_bindings=[{"source_id": "src_a", "transcript_version_id": "tr_a"}],
        content_draft_id=child_ref.artifact_id,
    )
    paragraph = next(
        item for item in snapshot.paragraphs if item["kind"] == "source_excerpt"
    )
    run = paragraph["source_runs"][0]
    assert isinstance(run, dict)
    block_id = run["block_id"]
    assert isinstance(block_id, str)
    text = str(paragraph["text"])
    end_utf16 = len(text.encode("utf-16-le")) // 2
    checkpoint_path = root / "workflow" / "draft-workspaces" / "wfr_test.json"
    checkpoint_before = checkpoint_path.read_bytes()
    draft_files_before = set((root / "content-drafts").glob("*.json"))

    with pytest.raises(ProjectError, match="only punctuation"):
        edit_draft_workspace_punctuation(
            root,
            run_id="wfr_test",
            operation_id="dwop_2_00000000000000000000000000000010",
            expected_checkpoint_ref=selected.checkpoint_ref,
            expected_current_candidate_ref=child_ref,
            paragraph_id=str(paragraph["paragraph_id"]),
            block_id=block_id,
            start_utf16_offset=end_utf16,
            end_utf16_offset=end_utf16,
            replacement="字",
        )
    assert checkpoint_path.read_bytes() == checkpoint_before
    assert set((root / "content-drafts").glob("*.json")) == draft_files_before

    committed = edit_draft_workspace_punctuation(
        root,
        run_id="wfr_test",
        operation_id="dwop_2_00000000000000000000000000000011",
        expected_checkpoint_ref=selected.checkpoint_ref,
        expected_current_candidate_ref=child_ref,
        paragraph_id=str(paragraph["paragraph_id"]),
        block_id=block_id,
        start_utf16_offset=end_utf16,
        end_utf16_offset=end_utf16,
        replacement="！",
    )
    assert committed.checkpoint.generation == selected.checkpoint.generation + 1
    assert committed.checkpoint.last_commit is not None
    assert committed.checkpoint.last_commit.operation == "punctuation_edit"
    assert committed.checkpoint.last_commit.operation_id == (
        "dwop_2_00000000000000000000000000000011"
    )
    assert len(set((root / "content-drafts").glob("*.json")) - draft_files_before) == 1
    changed = next(
        block for block in committed.current_candidate.blocks if block.block_id == block_id
    )
    assert isinstance(changed, SourceExcerptBlock)
    assert changed.display_text is not None
    assert changed.display_text.endswith("！")

    undone = undo_draft_workspace(
        root,
        run_id="wfr_test",
        operation_id="dwop_3_00000000000000000000000000000012",
        expected_checkpoint_ref=committed.checkpoint_ref,
        expected_current_candidate_ref=committed.checkpoint.current_candidate_ref,
    )
    undo_candidate = undone.current_candidate
    undone_block = next(
        block for block in undo_candidate.blocks if block.block_id == block_id
    )
    assert isinstance(undone_block, SourceExcerptBlock)
    assert undone_block.display_text is None
    redone = redo_draft_workspace(
        root,
        run_id="wfr_test",
        operation_id="dwop_4_00000000000000000000000000000013",
        expected_checkpoint_ref=undone.checkpoint_ref,
        expected_current_candidate_ref=undone.checkpoint.current_candidate_ref,
    )
    redone_block = next(
        block
        for block in redone.current_candidate.blocks
        if block.block_id == block_id
    )
    assert isinstance(redone_block, SourceExcerptBlock)
    assert redone_block.display_text == changed.display_text


def test_punctuation_workspace_boundary_ownership_is_atomic_and_cross_block_safe(
    tmp_path: Path,
) -> None:
    left_id = "block_edit_p_aaaaaaaaaaaaaaaaaaaaaaaa_r_0000000000000001"
    right_id = "block_edit_p_aaaaaaaaaaaaaaaaaaaaaaaa_r_0000000000000002"
    root, _first, child_ref = _workspace_fixture(tmp_path)
    _initialized, selected = _select_child(root, child_ref)
    source_blocks = [
        block
        for block in selected.current_candidate.blocks
        if isinstance(block, SourceExcerptBlock)
    ]
    assert len(source_blocks) >= 2
    left_text = source_blocks[0].canonical_text
    left = replace(
        source_blocks[0],
        block_id=left_id,
        display_text=(left_text.removesuffix("。")) + "……",
    )
    right = replace(
        source_blocks[1],
        block_id=right_id,
        display_text="！" + source_blocks[1].canonical_text,
    )
    seeded = prepare_content_draft_editor_child(
        parent=selected.current_candidate,
        blocks=(left, right),
        child_id="draft_workspace_boundary_seed",
    )
    write_new_json(
        root / "content-drafts" / "draft_workspace_boundary_seed.json",
        seeded.to_dict(),
    )
    seeded_ref = ArtifactRef(
        seeded.content_draft_id,
        seeded.schema_version,
        subject_content_hash("content_draft", seeded.schema_version, seeded.to_dict()),
    )
    selected_seeded = select_draft_workspace_candidate(
        root,
        run_id="wfr_test",
        operation_id="dwop_2_00000000000000000000000000000020",
        expected_checkpoint_ref=selected.checkpoint_ref,
        expected_current_candidate_ref=child_ref,
        child_candidate_ref=seeded_ref,
    )

    def paragraph_and_boundary(candidate_id: str) -> tuple[dict[str, object], int]:
        snapshot = load_draft_editor_snapshot(
            root,
            source_bindings=[{"source_id": "src_a", "transcript_version_id": "tr_a"}],
            content_draft_id=candidate_id,
        )
        paragraph = next(
            item
            for item in snapshot.paragraphs
            if item["kind"] == "source_excerpt"
            and {
                run.get("block_id")
                for run in item["source_runs"]
                if isinstance(run, dict)
            }
            >= {left_id, right_id}
        )
        left_run = next(
            run
            for run in paragraph["source_runs"]
            if run["block_id"] == left_id
        )
        boundary = int(left_run["end_offset"])
        return paragraph, len(str(paragraph["text"])[:boundary].encode("utf-16-le")) // 2

    paragraph, boundary_utf16 = paragraph_and_boundary(seeded_ref.artifact_id)
    inserted = edit_draft_workspace_punctuation(
        root,
        run_id="wfr_test",
        operation_id="dwop_3_00000000000000000000000000000021",
        expected_checkpoint_ref=selected_seeded.checkpoint_ref,
        expected_current_candidate_ref=seeded_ref,
        paragraph_id=str(paragraph["paragraph_id"]),
        block_id=right_id,
        start_utf16_offset=boundary_utf16,
        end_utf16_offset=boundary_utf16,
        replacement="？",
    )
    assert inserted.checkpoint.generation == selected_seeded.checkpoint.generation + 1
    inserted_right = next(
        block
        for block in inserted.current_candidate.blocks
        if block.block_id == right_id
    )
    assert isinstance(inserted_right, SourceExcerptBlock)
    assert inserted_right.display_text == "？！" + source_blocks[1].canonical_text

    paragraph, boundary_utf16 = paragraph_and_boundary(
        inserted.current_candidate.content_draft_id
    )
    deleted_right = edit_draft_workspace_punctuation(
        root,
        run_id="wfr_test",
        operation_id="dwop_4_00000000000000000000000000000022",
        expected_checkpoint_ref=inserted.checkpoint_ref,
        expected_current_candidate_ref=inserted.checkpoint.current_candidate_ref,
        paragraph_id=str(paragraph["paragraph_id"]),
        block_id=right_id,
        start_utf16_offset=boundary_utf16,
        end_utf16_offset=boundary_utf16 + 1,
        replacement="",
    )
    assert deleted_right.checkpoint.generation == inserted.checkpoint.generation + 1

    paragraph, boundary_utf16 = paragraph_and_boundary(
        deleted_right.current_candidate.content_draft_id
    )
    deleted_left = edit_draft_workspace_punctuation(
        root,
        run_id="wfr_test",
        operation_id="dwop_5_00000000000000000000000000000023",
        expected_checkpoint_ref=deleted_right.checkpoint_ref,
        expected_current_candidate_ref=deleted_right.checkpoint.current_candidate_ref,
        paragraph_id=str(paragraph["paragraph_id"]),
        block_id=left_id,
        start_utf16_offset=boundary_utf16 - 2,
        end_utf16_offset=boundary_utf16,
        replacement="",
    )
    assert deleted_left.checkpoint.generation == deleted_right.checkpoint.generation + 1

    paragraph, boundary_utf16 = paragraph_and_boundary(
        deleted_left.current_candidate.content_draft_id
    )
    before_files = set((root / "content-drafts").glob("*.json"))
    before_generation = deleted_left.checkpoint.generation
    with pytest.raises(ProjectError, match="crosses source blocks"):
        edit_draft_workspace_punctuation(
            root,
            run_id="wfr_test",
            operation_id="dwop_6_00000000000000000000000000000024",
            expected_checkpoint_ref=deleted_left.checkpoint_ref,
            expected_current_candidate_ref=deleted_left.checkpoint.current_candidate_ref,
            paragraph_id=str(paragraph["paragraph_id"]),
            block_id=left_id,
            start_utf16_offset=boundary_utf16 - 1,
            end_utf16_offset=boundary_utf16 + 1,
            replacement="？",
        )
    current = read_draft_workspace(root, run_id="wfr_test")
    assert current is not None
    assert current.checkpoint.generation == before_generation
    assert set((root / "content-drafts").glob("*.json")) == before_files


@pytest.mark.parametrize(
    ("case", "operation"),
    [("a", "move"), ("b", "delete"), ("b", "move")],
)
def test_real_punctuation_prepare_publish_and_workspace_match(
    tmp_path: Path,
    case: str,
    operation: str,
) -> None:
    root, _candidate_ref = _ownership_workspace_fixture(tmp_path, case)
    initialized = initialize_draft_workspace(
        root,
        run_id="wfr_test",
        operation_id="dwop_0_00000000000000000000000000000041",
    )
    snapshot = load_draft_editor_snapshot(
        root,
        source_bindings=[{"source_id": "src_a", "transcript_version_id": "tr_ownership"}],
        content_draft_id=initialized.current_candidate.content_draft_id,
    )
    paragraph = next(
        item
        for item in snapshot.paragraphs
        if item["kind"] == "source_excerpt"
        and ("区。" if case == "a" else "时间学习。") in str(item["text"])
    )
    start = 0 if case == "a" else len("前文")
    end = len(str(paragraph["text"])) if case == "a" else start + len("时间学习")
    selection = resolve_draft_editor_selection(
        snapshot,
        surface="draft",
        anchor={"paragraph_id": paragraph["paragraph_id"], "offset": start},
        focus={"paragraph_id": paragraph["paragraph_id"], "offset": end},
    )
    caret = (
        resolve_draft_editor_caret(
            snapshot,
            paragraph_id=str(
                next(
                    item
                    for item in snapshot.paragraphs
                    if item["kind"] == "source_excerpt"
                    and ("落点" if case == "a" else "前文") in str(item["text"])
                )["paragraph_id"]
            ),
            offset=(2 if case == "a" else 0),
        )
        if operation == "move"
        else None
    )
    prepared = prepare_draft_candidate(
        snapshot,
        operation=operation,
        selection=selection,
        caret=caret,
        accept_degraded=False,
        child_id=f"draft_workspace_{case}_{operation}",
    )
    direct = edit_draft_candidate(
        snapshot,
        operation=operation,
        selection=selection,
        caret=caret,
        accept_degraded=False,
    ).content_draft
    before_files = set((root / "content-drafts").glob("*.json"))
    edited = edit_draft_workspace(
        root,
        run_id="wfr_test",
        operation_id=f"dwop_1_{'4' * 32}",
        expected_checkpoint_ref=initialized.checkpoint_ref,
        expected_current_candidate_ref=initialized.checkpoint.current_candidate_ref,
        operation=operation,
        selection=selection,
        caret=caret,
        accept_degraded=False,
        prepared_editor_snapshot=snapshot,
    )
    assert direct.blocks == prepared.blocks == edited.current_candidate.blocks
    assert edited.checkpoint.generation == initialized.checkpoint.generation + 1
    assert edited.checkpoint.last_commit.operation == f"edit_{operation}"
    assert len(set((root / "content-drafts").glob("*.json")) - before_files) == 1
    sources = [
        block for block in edited.current_candidate.blocks if isinstance(block, SourceExcerptBlock)
    ]
    refs = [ref for block in sources for ref in block.refs]
    if case == "a":
        moved = next(block for block in sources if block.refs[0].segment_id == "a01")
        assert (moved.display_text or moved.canonical_text) == "区。"
        assert not any(ref.segment_id == "a01" and ref.end_ticks > 285_000 for ref in refs)
    else:
        owner = next(block for block in sources if block.refs[0].segment_id == "b01")
        assert owner.display_text == "前文。"
        assert not any(ref.segment_id == "b02" and ref.end_ticks > 585_714 for ref in refs)


def test_selection_punctuation_orphan_rejects_without_workspace_write(
    tmp_path: Path,
) -> None:
    root, _candidate_ref = _ownership_workspace_fixture(
        tmp_path, "b", include_owner=False
    )
    before = initialize_draft_workspace(
        root,
        run_id="wfr_test",
        operation_id="dwop_0_00000000000000000000000000000042",
    )
    snapshot = load_draft_editor_snapshot(
        root,
        source_bindings=[{"source_id": "src_a", "transcript_version_id": "tr_ownership"}],
        content_draft_id=before.current_candidate.content_draft_id,
    )
    paragraph = next(item for item in snapshot.paragraphs if "时间学习。" in str(item["text"]))
    selection = resolve_draft_editor_selection(
        snapshot,
        surface="draft",
        anchor={"paragraph_id": paragraph["paragraph_id"], "offset": 0},
        focus={"paragraph_id": paragraph["paragraph_id"], "offset": len("时间学习")},
    )
    before_json = {path: path.read_bytes() for path in root.rglob("*.json")}
    with pytest.raises(ProjectError, match="orphan"):
        edit_draft_workspace(
            root,
            run_id="wfr_test",
            operation_id=f"dwop_1_{'5' * 32}",
            expected_checkpoint_ref=before.checkpoint_ref,
            expected_current_candidate_ref=before.checkpoint.current_candidate_ref,
            operation="delete",
            selection=selection,
            caret=None,
            accept_degraded=False,
            prepared_editor_snapshot=snapshot,
        )
    after = read_draft_workspace(root, run_id="wfr_test")
    assert after is not None and after.checkpoint == before.checkpoint
    assert {path: path.read_bytes() for path in root.rglob("*.json")} == before_json


def test_response_loss_readback_and_same_operation_different_input_conflict(
    tmp_path: Path,
) -> None:
    root, _first, child_ref = _workspace_fixture(tmp_path)
    _initialized, selected = _select_child(root, child_ref)
    selection = _first_paragraph_selection(root, child_ref.artifact_id)
    committed = edit_draft_workspace(
        root,
        run_id="wfr_test",
        operation_id="dwop_2_00000000000000000000000000000003",
        expected_checkpoint_ref=selected.checkpoint_ref,
        expected_current_candidate_ref=child_ref,
        operation="delete",
        selection=selection,
        caret=None,
        accept_degraded=True,
    )

    readback = edit_draft_workspace(
        root,
        run_id="wfr_test",
        operation_id="dwop_2_00000000000000000000000000000003",
        expected_checkpoint_ref=selected.checkpoint_ref,
        expected_current_candidate_ref=child_ref,
        operation="delete",
        selection=selection,
        caret=None,
        accept_degraded=True,
    )
    assert readback.readback
    assert readback.checkpoint == committed.checkpoint
    assert readback.current_candidate == committed.current_candidate

    with pytest.raises(WorkflowError) as raised:
        edit_draft_workspace(
            root,
            run_id="wfr_test",
            operation_id="dwop_2_00000000000000000000000000000003",
            expected_checkpoint_ref=selected.checkpoint_ref,
            expected_current_candidate_ref=child_ref,
            operation="delete",
            selection=selection,
            caret=None,
            accept_degraded=False,
        )
    assert raised.value.code == "draft_workspace_action_conflict"
    assert read_draft_workspace(root, run_id="wfr_test").checkpoint == committed.checkpoint  # type: ignore[union-attr]


def test_move_and_insert_use_existing_exact_selection_and_caret_rules(
    tmp_path: Path,
) -> None:
    root, _first, child_ref = _workspace_fixture(tmp_path)
    _initialized, selected = _select_child(root, child_ref)
    snapshot = load_draft_editor_snapshot(
        root,
        source_bindings=[
            {"source_id": "src_a", "transcript_version_id": "tr_a"}
        ],
        content_draft_id=child_ref.artifact_id,
    )
    first = snapshot.paragraphs[0]
    last = snapshot.paragraphs[-1]
    selection = resolve_draft_editor_selection(
        snapshot,
        surface="draft",
        anchor={"paragraph_id": first["paragraph_id"], "offset": 0},
        focus={
            "paragraph_id": first["paragraph_id"],
            "offset": len(str(first["text"])),
        },
    )
    caret = resolve_draft_editor_caret(
        snapshot,
        paragraph_id=str(last["paragraph_id"]),
        offset=len(str(last["text"])),
    )
    moved = edit_draft_workspace(
        root,
        run_id="wfr_test",
        operation_id="dwop_2_00000000000000000000000000000006",
        expected_checkpoint_ref=selected.checkpoint_ref,
        expected_current_candidate_ref=child_ref,
        operation="move",
        selection=selection,
        caret=caret,
        accept_degraded=True,
    )
    assert getattr(moved.current_candidate.blocks[-1], "canonical_text", None) == (
        first["text"]
    )

    moved_snapshot = load_draft_editor_snapshot(
        root,
        source_bindings=[
            {"source_id": "src_a", "transcript_version_id": "tr_a"}
        ],
        content_draft_id=moved.current_candidate.content_draft_id,
    )
    source_paragraph = moved_snapshot.transcript_paragraphs[-1]
    source_text = str(source_paragraph["text"])
    source_start = source_text.rfind("操场。")
    assert source_start >= 0
    source_selection = resolve_draft_editor_selection(
        moved_snapshot,
        surface="source",
        anchor={
            "paragraph_id": source_paragraph["paragraph_id"],
            "offset": source_start,
        },
        focus={
            "paragraph_id": source_paragraph["paragraph_id"],
            "offset": source_start + len("操场。"),
        },
    )
    target = moved_snapshot.paragraphs[0]
    insert_caret = resolve_draft_editor_caret(
        moved_snapshot,
        paragraph_id=str(target["paragraph_id"]),
        offset=0,
    )
    inserted = edit_draft_workspace(
        root,
        run_id="wfr_test",
        operation_id="dwop_3_00000000000000000000000000000007",
        expected_checkpoint_ref=moved.checkpoint_ref,
        expected_current_candidate_ref=moved.checkpoint.current_candidate_ref,
        operation="insert",
        selection=source_selection,
        caret=insert_caret,
        accept_degraded=True,
    )
    assert inserted.current_candidate.parent_draft_id == (
        moved.current_candidate.content_draft_id
    )
    assert inserted.checkpoint.generation == 4


@pytest.mark.parametrize(
    ("operation", "expected_commit_operation"),
    [
        ("delete", "edit_delete"),
        ("move", "edit_move"),
        ("insert", "edit_insert"),
        ("section_split", "section_split"),
    ],
)
def test_gap_boundary_workspace_operations_commit_one_child_and_generation(
    tmp_path: Path,
    operation: str,
    expected_commit_operation: str,
) -> None:
    root, parent_ref = _gap_workspace_fixture(tmp_path)
    initialized = initialize_draft_workspace(
        root,
        run_id="wfr_test",
        operation_id="dwop_0_00000000000000000000000000000001",
    )
    snapshot = load_draft_editor_snapshot(
        root,
        source_bindings=[{"source_id": "src_a", "transcript_version_id": "tr_gap"}],
        content_draft_id=parent_ref.artifact_id,
    )
    paragraph = snapshot.paragraphs[0]
    if operation == "insert":
        source = snapshot.transcript_paragraphs[0]
        selection = resolve_draft_editor_selection(
            snapshot,
            surface="source",
            anchor={"paragraph_id": source["paragraph_id"], "offset": 0},
            focus={"paragraph_id": source["paragraph_id"], "offset": 2},
        )
    else:
        selection = resolve_draft_editor_selection(
            snapshot,
            surface="draft",
            anchor={"paragraph_id": paragraph["paragraph_id"], "offset": 2},
            focus={"paragraph_id": paragraph["paragraph_id"], "offset": 3},
        )
    operation_id = f"dwop_1_{'0' * 31}{'1' if operation == 'delete' else '2' if operation == 'move' else '3' if operation == 'insert' else '4'}"
    before_files = set((root / "content-drafts").glob("*.json"))
    if operation == "section_split":
        edited = edit_draft_workspace_section(
            root,
            run_id="wfr_test",
            operation_id=operation_id,
            expected_checkpoint_ref=initialized.checkpoint_ref,
            expected_current_candidate_ref=parent_ref,
            operation=operation,
            payload={
                "heading_block_id": "gap_section",
                "target": {
                    "paragraph_id": paragraph["paragraph_id"],
                    "block_id": "gap_block",
                    "utf16_offset": 2,
                },
                "title": "新章节",
            },
        )
    else:
        caret = None
        if operation == "move":
            caret = resolve_draft_editor_caret(
                snapshot,
                paragraph_id=str(paragraph["paragraph_id"]),
                offset=0,
            )
        elif operation == "insert":
            caret = resolve_draft_editor_caret(
                snapshot,
                paragraph_id=str(paragraph["paragraph_id"]),
                offset=2,
            )
        edited = edit_draft_workspace(
            root,
            run_id="wfr_test",
            operation_id=operation_id,
            expected_checkpoint_ref=initialized.checkpoint_ref,
            expected_current_candidate_ref=parent_ref,
            operation=operation,
            selection=selection,
            caret=caret,
            accept_degraded=False,
        )
    assert edited.checkpoint.generation == initialized.checkpoint.generation + 1
    assert edited.current_candidate.parent_draft_id == parent_ref.artifact_id
    assert edited.checkpoint.last_commit is not None
    assert edited.checkpoint.last_commit.operation == expected_commit_operation
    assert edited.checkpoint.last_commit.operation_id == operation_id
    after_files = set((root / "content-drafts").glob("*.json"))
    assert after_files - before_files == {
        root / "content-drafts" / f"{edited.current_candidate.content_draft_id}.json"
    }

    source_blocks = [
        block for block in edited.current_candidate.blocks if hasattr(block, "refs")
    ]
    evidence = [
        (block.refs[0].start_ticks, block.refs[0].end_ticks, block.canonical_text)
        for block in source_blocks
    ]
    assert all(end <= 200 or start >= 250 for start, end, _text in evidence)
    if operation == "delete":
        assert evidence == [(100, 200, "甲，"), (380, 480, "丙。")]
        assert "".join(block.canonical_text for block in source_blocks) == "甲，丙。"
    elif operation == "move":
        assert evidence == [
            (250, 350, "乙 "),
            (100, 200, "甲，"),
            (380, 480, "丙。"),
        ]
        assert "".join(block.canonical_text for block in source_blocks) == "乙 甲，丙。"
    elif operation == "insert":
        assert evidence == [
            (100, 200, "甲，"),
            (100, 200, "甲，"),
            (250, 500, "乙 丙。"),
        ]
        assert "".join(block.canonical_text for block in source_blocks) == "甲，甲，乙 丙。"
    else:
        assert evidence == [(100, 200, "甲，"), (250, 500, "乙 丙。")]
        assert "".join(block.canonical_text for block in source_blocks) == "甲，乙 丙。"


def test_result_placement_validation_fails_before_child_and_checkpoint_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, parent_ref = _gap_workspace_fixture(tmp_path)
    initialized = initialize_draft_workspace(
        root,
        run_id="wfr_test",
        operation_id="dwop_0_00000000000000000000000000000051",
    )
    snapshot = load_draft_editor_snapshot(
        root,
        source_bindings=[{"source_id": "src_a", "transcript_version_id": "tr_gap"}],
        content_draft_id=parent_ref.artifact_id,
    )
    paragraph = snapshot.paragraphs[0]
    selection = resolve_draft_editor_selection(
        snapshot,
        surface="draft",
        anchor={"paragraph_id": paragraph["paragraph_id"], "offset": 2},
        focus={"paragraph_id": paragraph["paragraph_id"], "offset": 3},
    )
    caret = resolve_draft_editor_caret(
        snapshot,
        paragraph_id=str(paragraph["paragraph_id"]),
        offset=0,
    )
    checkpoint_path = root / "workflow" / "draft-workspaces" / "wfr_test.json"
    before_checkpoint = checkpoint_path.read_bytes()
    before_files = sorted((root / "content-drafts").glob("*.json"))

    def fail_validation(*_args: object, **_kwargs: object) -> object:
        raise ProjectError("placement projection is ambiguous")

    monkeypatch.setattr(
        draft_workspaces_module,
        "materialize_draft_editor_placement",
        fail_validation,
    )
    with pytest.raises(ProjectError, match="ambiguous"):
        edit_draft_workspace(
            root,
            run_id="wfr_test",
            operation_id="dwop_1_00000000000000000000000000000052",
            expected_checkpoint_ref=initialized.checkpoint_ref,
            expected_current_candidate_ref=parent_ref,
            operation="move",
            selection=selection,
            caret=caret,
            accept_degraded=False,
        )

    assert checkpoint_path.read_bytes() == before_checkpoint
    assert sorted((root / "content-drafts").glob("*.json")) == before_files
    current = read_draft_workspace(root, run_id="wfr_test")
    assert current is not None
    assert current.checkpoint == initialized.checkpoint


def test_invalid_transform_is_not_stale_and_can_retry_from_same_checkpoint(
    tmp_path: Path,
) -> None:
    root, _first, child_ref = _workspace_fixture(tmp_path)
    _initialized, selected = _select_child(root, child_ref)
    snapshot = load_draft_editor_snapshot(
        root,
        source_bindings=[{"source_id": "src_a", "transcript_version_id": "tr_a"}],
        content_draft_id=child_ref.artifact_id,
    )
    source_paragraph = next(
        paragraph
        for paragraph in snapshot.paragraphs
        if paragraph["kind"] == "source_excerpt"
    )
    selection = resolve_draft_editor_selection(
        snapshot,
        surface="draft",
        anchor={"paragraph_id": source_paragraph["paragraph_id"], "offset": 0},
        focus={
            "paragraph_id": source_paragraph["paragraph_id"],
            "offset": len(str(source_paragraph["text"])),
        },
    )
    inside_caret = resolve_draft_editor_caret(
        snapshot,
        paragraph_id=str(source_paragraph["paragraph_id"]),
        offset=1,
    )
    checkpoint_path = root / "workflow" / "draft-workspaces" / "wfr_test.json"
    checkpoint_before = checkpoint_path.read_bytes()
    draft_files_before = sorted((root / "content-drafts").glob("*.json"))

    with pytest.raises(ProjectError, match="operation does not change"):
        edit_draft_workspace(
            root,
            run_id="wfr_test",
            operation_id="dwop_2_00000000000000000000000000000011",
            expected_checkpoint_ref=selected.checkpoint_ref,
            expected_current_candidate_ref=child_ref,
            operation="move",
            selection=selection,
            caret=inside_caret,
            accept_degraded=True,
        )

    assert checkpoint_path.read_bytes() == checkpoint_before
    assert sorted((root / "content-drafts").glob("*.json")) == draft_files_before
    current = read_draft_workspace(root, run_id="wfr_test")
    assert current is not None
    assert current.checkpoint == selected.checkpoint
    assert current.current_candidate.content_draft_id == child_ref.artifact_id

    target_paragraph = next(
        paragraph
        for paragraph in reversed(snapshot.paragraphs)
        if paragraph["kind"] == "source_excerpt"
    )
    retry_caret = resolve_draft_editor_caret(
        snapshot,
        paragraph_id=str(target_paragraph["paragraph_id"]),
        offset=len(str(target_paragraph["text"])),
    )
    retried = edit_draft_workspace(
        root,
        run_id="wfr_test",
        operation_id="dwop_2_00000000000000000000000000000012",
        expected_checkpoint_ref=selected.checkpoint_ref,
        expected_current_candidate_ref=child_ref,
        operation="move",
        selection=selection,
        caret=retry_caret,
        accept_degraded=True,
    )
    assert retried.checkpoint.generation == selected.checkpoint.generation + 1
    assert retried.current_candidate.parent_draft_id == child_ref.artifact_id


def test_two_application_instances_cas_loser_is_stale(tmp_path: Path) -> None:
    root, _first, child_ref = _workspace_fixture(tmp_path)
    initialized, _selected = _select_child(root, child_ref)

    with pytest.raises(WorkflowError) as raised:
        select_draft_workspace_candidate(
            root,
            run_id="wfr_test",
            operation_id="dwop_1_00000000000000000000000000000009",
            expected_checkpoint_ref=initialized.checkpoint_ref,
            expected_current_candidate_ref=initialized.checkpoint.current_candidate_ref,
            child_candidate_ref=child_ref,
        )
    assert raised.value.code == "draft_workspace_stale"


def test_checkpoint_publish_failure_cleans_only_owned_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _first, child_ref = _workspace_fixture(tmp_path)
    _initialized, selected = _select_child(root, child_ref)
    selection = _first_paragraph_selection(root, child_ref.artifact_id)
    before_files = set((root / "content-drafts").iterdir())
    checkpoint_before = read_draft_workspace(root, run_id="wfr_test")
    assert checkpoint_before is not None

    def fail_replace(source: Path, target: Path) -> None:
        raise OSError("fixture checkpoint replace failed")

    monkeypatch.setattr(checkpoint_store_module.os, "replace", fail_replace)
    with pytest.raises(WorkflowError) as raised:
        edit_draft_workspace(
            root,
            run_id="wfr_test",
            operation_id="dwop_2_00000000000000000000000000000010",
            expected_checkpoint_ref=selected.checkpoint_ref,
            expected_current_candidate_ref=child_ref,
            operation="delete",
            selection=selection,
            caret=None,
            accept_degraded=True,
        )
    assert raised.value.code == "draft_workspace_write_failed"
    assert set((root / "content-drafts").iterdir()) == before_files
    assert read_draft_workspace(root, run_id="wfr_test").checkpoint == checkpoint_before.checkpoint  # type: ignore[union-attr]


def test_checkpoint_temp_write_failure_cleans_only_owned_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _first, child_ref = _workspace_fixture(tmp_path)
    _initialized, selected = _select_child(root, child_ref)
    selection = _first_paragraph_selection(root, child_ref.artifact_id)
    before_files = set((root / "content-drafts").iterdir())
    checkpoint_before = read_draft_workspace(root, run_id="wfr_test")
    assert checkpoint_before is not None
    original_open = Path.open

    def fail_checkpoint_temp(
        path: Path, mode: str = "r", *args: object, **kwargs: object
    ):
        if path.name == ".wfr_test.json.tmp" and mode == "xb":
            raise PermissionError("fixture checkpoint temp failure")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_checkpoint_temp)
    with pytest.raises(WorkflowError) as raised:
        edit_draft_workspace(
            root,
            run_id="wfr_test",
            operation_id="dwop_2_00000000000000000000000000000012",
            expected_checkpoint_ref=selected.checkpoint_ref,
            expected_current_candidate_ref=child_ref,
            operation="delete",
            selection=selection,
            caret=None,
            accept_degraded=True,
        )
    assert raised.value.code == "draft_workspace_write_failed"
    assert set((root / "content-drafts").iterdir()) == before_files
    assert read_draft_workspace(root, run_id="wfr_test").checkpoint == checkpoint_before.checkpoint  # type: ignore[union-attr]


def test_external_project_revision_fails_closed(tmp_path: Path) -> None:
    root, _first, child_ref = _workspace_fixture(tmp_path)
    _select_child(root, child_ref)
    project = ProjectStore(root).load()
    ProjectStore(root).save(
        replace(
            project,
            revision=project.revision + 1,
            updated_at="2026-07-29T00:00:00+00:00",
        ),
        expected_revision=project.revision,
    )

    with pytest.raises(WorkflowError) as raised:
        read_draft_workspace(root, run_id="wfr_test")
    assert raised.value.code == "draft_workspace_stale"


def test_ten_narration_edits_and_undo_branch_preserve_immutable_history(
    tmp_path: Path,
) -> None:
    root, _first, child_ref = _workspace_fixture(tmp_path)
    _initialized, selected = _select_child(root, child_ref)
    parent = selected.current_candidate
    seeded = prepare_content_draft_editor_child(
        parent=parent,
        blocks=(
            *parent.blocks,
            NarrationBlock("narration_a", "解说 0", "draft", ()),
        ),
        child_id="draft_narration_seed",
    )
    write_new_json(
        root / "content-drafts" / "draft_narration_seed.json", seeded.to_dict()
    )
    seeded_ref = ArtifactRef(
        seeded.content_draft_id,
        seeded.schema_version,
        subject_content_hash(
            "content_draft", seeded.schema_version, seeded.to_dict()
        ),
    )
    state = select_draft_workspace_candidate(
        root,
        run_id="wfr_test",
        operation_id="dwop_2_00000000000000000000000000000011",
        expected_checkpoint_ref=selected.checkpoint_ref,
        expected_current_candidate_ref=child_ref,
        child_candidate_ref=seeded_ref,
    )
    history = [state.current_candidate.content_draft_id]
    for index in range(1, 11):
        state = edit_draft_workspace_narration(
            root,
            run_id="wfr_test",
            operation_id=f"dwop_{state.checkpoint.generation}_{index:032x}",
            expected_checkpoint_ref=state.checkpoint_ref,
            expected_current_candidate_ref=state.checkpoint.current_candidate_ref,
            block_id="narration_a",
            text=f"解说 {index}",
        )
        history.append(state.current_candidate.content_draft_id)
    assert state.checkpoint.generation == 13
    assert len(set(history)) == 11

    undone = undo_draft_workspace(
        root,
        run_id="wfr_test",
        operation_id="dwop_13_00000000000000000000000000000020",
        expected_checkpoint_ref=state.checkpoint_ref,
        expected_current_candidate_ref=state.checkpoint.current_candidate_ref,
    )
    old_tip = state.checkpoint.current_candidate_ref
    branch = edit_draft_workspace_narration(
        root,
        run_id="wfr_test",
        operation_id="dwop_14_00000000000000000000000000000021",
        expected_checkpoint_ref=undone.checkpoint_ref,
        expected_current_candidate_ref=undone.checkpoint.current_candidate_ref,
        block_id="narration_a",
        text="分支解说",
    )
    assert branch.checkpoint.redo_candidate_refs == ()
    assert (root / "content-drafts" / f"{old_tip.artifact_id}.json").is_file()


def test_child_publish_failure_keeps_checkpoint_and_creates_no_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _first, child_ref = _workspace_fixture(tmp_path)
    _initialized, selected = _select_child(root, child_ref)
    selection = _first_paragraph_selection(root, child_ref.artifact_id)
    before = set((root / "content-drafts").iterdir())

    def fail_publish(*args: object, **kwargs: object) -> None:
        raise OSError("fixture child failure")

    monkeypatch.setattr(
        checkpoint_store_module, "publish_workflow_candidate", fail_publish
    )
    with pytest.raises(WorkflowError) as raised:
        edit_draft_workspace(
            root,
            run_id="wfr_test",
            operation_id="dwop_2_00000000000000000000000000000030",
            expected_checkpoint_ref=selected.checkpoint_ref,
            expected_current_candidate_ref=child_ref,
            operation="delete",
            selection=selection,
            caret=None,
            accept_degraded=True,
        )
    assert raised.value.code == "draft_workspace_write_failed"
    assert set((root / "content-drafts").iterdir()) == before
    assert read_draft_workspace(root, run_id="wfr_test").checkpoint == selected.checkpoint  # type: ignore[union-attr]


def test_hard_exit_after_child_before_checkpoint_preserves_old_current_and_orphan(
    tmp_path: Path,
) -> None:
    root, _first, child_ref = _workspace_fixture(tmp_path)
    _initialized, selected = _select_child(root, child_ref)
    before_ids = {path.name for path in (root / "content-drafts").iterdir()}
    script = r"""
import os
from pathlib import Path
import roughcut.adapters.draft_workspace_store as store_module
from roughcut.application.draft_editor import load_draft_editor_snapshot, resolve_draft_editor_selection
from roughcut.application.draft_workspaces import edit_draft_workspace
from roughcut.domain.draft_workspace import DraftWorkspaceCheckpointRef
from roughcut.domain.workflow import ArtifactRef

root = Path(os.environ["DWC_PROJECT"])
checkpoint_ref = DraftWorkspaceCheckpointRef.from_dict(__import__("json").loads(os.environ["DWC_CHECKPOINT"]))
current_ref = ArtifactRef.from_dict(__import__("json").loads(os.environ["DWC_CURRENT"]))
snapshot = load_draft_editor_snapshot(
    root,
    source_bindings=[{"source_id": "src_a", "transcript_version_id": "tr_a"}],
    content_draft_id=current_ref.artifact_id,
)
paragraph = snapshot.paragraphs[0]
selection = resolve_draft_editor_selection(
    snapshot,
    surface="draft",
    anchor={"paragraph_id": paragraph["paragraph_id"], "offset": 0, "offset_encoding": "codepoint"},
    focus={"paragraph_id": paragraph["paragraph_id"], "offset": len(str(paragraph["text"])), "offset_encoding": "codepoint"},
)
store_module.os.replace = lambda _source, _target: os._exit(91)
edit_draft_workspace(
    root,
    run_id="wfr_test",
    operation_id="dwop_2_00000000000000000000000000000031",
    expected_checkpoint_ref=checkpoint_ref,
    expected_current_candidate_ref=current_ref,
    operation="delete",
    selection=selection,
    caret=None,
    accept_degraded=True,
)
"""
    environment = {
        **os.environ,
        "DWC_PROJECT": str(root),
        "DWC_CHECKPOINT": json.dumps(selected.checkpoint_ref.to_dict()),
        "DWC_CURRENT": json.dumps(child_ref.to_dict()),
    }
    completed = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 91
    recovered = read_draft_workspace(root, run_id="wfr_test")
    assert recovered is not None
    assert recovered.checkpoint == selected.checkpoint
    after_ids = {path.name for path in (root / "content-drafts").iterdir()}
    orphan_ids = after_ids - before_ids
    assert len([name for name in orphan_ids if name.endswith(".json")]) == 1


def test_corrupt_missing_cross_project_and_invalid_redo_fail_closed(
    tmp_path: Path,
) -> None:
    root, _first, child_ref = _workspace_fixture(tmp_path)
    _initialized, _selected = _select_child(root, child_ref)
    checkpoint_path = (
        root / "workflow" / "draft-workspaces" / "wfr_test.json"
    )
    valid_bytes = checkpoint_path.read_bytes()
    checkpoint_path.write_text('{"schema_version":', encoding="utf-8")
    with pytest.raises(WorkflowError) as corrupt:
        read_draft_workspace(root, run_id="wfr_test")
    assert corrupt.value.code == "draft_workspace_integrity_error"
    checkpoint_path.write_bytes(valid_bytes)

    payload = json.loads(valid_bytes)
    payload["current_candidate_ref"]["artifact_id"] = "draft_missing"
    checkpoint_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(WorkflowError) as missing:
        read_draft_workspace(root, run_id="wfr_test")
    assert missing.value.code == "draft_workspace_integrity_error"
    checkpoint_path.write_bytes(valid_bytes)

    payload = json.loads(valid_bytes)
    payload["project_id"] = "another_project"
    checkpoint_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(WorkflowError) as copied:
        read_draft_workspace(root, run_id="wfr_test")
    assert copied.value.code == "draft_workspace_integrity_error"
    checkpoint_path.write_bytes(valid_bytes)

    payload = json.loads(valid_bytes)
    payload["redo_candidate_refs"] = [payload["current_candidate_ref"]]
    checkpoint_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(WorkflowError) as redo:
        read_draft_workspace(root, run_id="wfr_test")
    assert redo.value.code == "draft_workspace_integrity_error"


def test_approve_draft_dormancy_and_return_reset(tmp_path: Path) -> None:
    root = _workflow_project(tmp_path)
    submitted = _submit_basic_draft(root)
    initialized = initialize_draft_workspace(
        root,
        run_id="wfr_test",
        operation_id="dwop_0_00000000000000000000000000000040",
    )
    mutation = submitted.receipt.mutation
    assert mutation is not None
    parent = initialized.current_candidate
    seeded = prepare_content_draft_editor_child(
        parent=parent,
        blocks=(
            *parent.blocks,
            NarrationBlock(
                "return_narration",
                "开场。",
                "recorded",
                next(
                    block.refs
                    for block in parent.blocks
                    if isinstance(block, SourceExcerptBlock)
                ),
            ),
        ),
        child_id="draft_return_seed",
    )
    write_new_json(
        root / "content-drafts" / "draft_return_seed.json", seeded.to_dict()
    )
    seeded_ref = ArtifactRef(
        seeded.content_draft_id,
        seeded.schema_version,
        subject_content_hash(
            "content_draft", seeded.schema_version, seeded.to_dict()
        ),
    )
    selected = select_draft_workspace_candidate(
        root,
        run_id="wfr_test",
        operation_id="dwop_1_00000000000000000000000000000043",
        expected_checkpoint_ref=initialized.checkpoint_ref,
        expected_current_candidate_ref=initialized.checkpoint.current_candidate_ref,
        child_candidate_ref=seeded_ref,
    )
    approved = workflow_action(
        root,
        "wfr_test",
        "act_checkpoint_approve",
        "approve_draft",
        {
            "schema_version": 1,
            "content_draft_ref": {
                "artifact_id": seeded_ref.artifact_id,
                "schema_version": seeded_ref.schema_version,
                "content_hash": seeded_ref.content_hash,
            },
        },
    )
    with pytest.raises(WorkflowError) as dormant:
        read_draft_workspace(root, run_id="wfr_test")
    assert dormant.value.code == "draft_workspace_transition_not_allowed"
    proposal = approved.workflow_run.artifact_refs["proposal"]
    assert proposal is not None
    adopted = workflow_action(
        root,
        "wfr_test",
        "act_checkpoint_adopt",
        "adopt_roughcut",
        {"schema_version": 1, "proposal_ref": proposal.to_dict()},
    )
    decision = adopted.workflow_run.artifact_refs["decision"]
    confirmed = adopted.workflow_run.artifact_refs["content_draft"]
    assert decision is not None and confirmed is not None
    returned = workflow_action(
        root,
        "wfr_test",
        "act_checkpoint_return",
        "return_to_draft",
        {
            "schema_version": 1,
            "current_subject_ref": {"kind": "decision", **decision.to_dict()},
            "confirmed_content_draft_ref": confirmed.to_dict(),
        },
    )
    receipt_ref = returned.workflow_run.last_receipt_ref
    assert receipt_ref is not None
    project_before_reset = ProjectStore(root).load()
    run_before_reset = WorkflowStore(root).read_run("wfr_test")
    reset = reset_draft_workspace_after_return(
        root,
        run_id="wfr_test",
        operation_id="dwop_2_00000000000000000000000000000041",
        expected_checkpoint_ref=selected.checkpoint_ref,
        expected_current_candidate_ref=selected.checkpoint.current_candidate_ref,
        return_receipt_ref=receipt_ref,
        confirmed_anchor_ref=confirmed,
    )
    assert reset.checkpoint.current_candidate_ref == confirmed
    assert reset.checkpoint.redo_candidate_refs == ()
    assert ProjectStore(root).load() == project_before_reset
    assert WorkflowStore(root).read_run("wfr_test") == run_before_reset
    child = edit_draft_workspace_narration(
        root,
        run_id="wfr_test",
        operation_id="dwop_3_00000000000000000000000000000044",
        expected_checkpoint_ref=reset.checkpoint_ref,
        expected_current_candidate_ref=confirmed,
        block_id="return_narration",
        text="返回后解说",
    )
    restarted = read_draft_workspace(root, run_id="wfr_test")
    assert restarted is not None
    assert restarted.checkpoint == child.checkpoint


def _workspace_child(
    root: Path,
    parent: ContentDraft,
    *,
    child_id: str,
) -> ArtifactRef:
    child = prepare_content_draft_editor_child(
        parent=parent,
        blocks=parent.blocks,
        child_id=child_id,
    )
    write_new_json(root / "content-drafts" / f"{child_id}.json", child.to_dict())
    return ArtifactRef(
        child.content_draft_id,
        child.schema_version,
        subject_content_hash("content_draft", child.schema_version, child.to_dict()),
    )


def test_rebase_receipt_resets_checkpoint_with_nonempty_redo_to_exact_new_anchor(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    old_workspace = {}

    def initialize_before_reapproval(_first: object) -> None:
        initialized = initialize_draft_workspace(
            root,
            run_id="wfr_test",
            operation_id="dwop_0_00000000000000000000000000000042",
        )
        child_ref = _workspace_child(
            root,
            initialized.current_candidate,
            child_id="draft_old_redo",
        )
        selected = select_draft_workspace_candidate(
            root,
            run_id="wfr_test",
            operation_id="dwop_1_00000000000000000000000000000043",
            expected_checkpoint_ref=initialized.checkpoint_ref,
            expected_current_candidate_ref=initialized.checkpoint.current_candidate_ref,
            child_candidate_ref=child_ref,
        )
        old_workspace["state"] = undo_draft_workspace(
            root,
            run_id="wfr_test",
            operation_id="dwop_2_00000000000000000000000000000045",
            expected_checkpoint_ref=selected.checkpoint_ref,
            expected_current_candidate_ref=child_ref,
        )

    first, rebase_input = _prepare_rebase_input(
        root,
        before_reapproval=initialize_before_reapproval,
    )
    old_anchor = first.workflow_run.artifact_refs["content_draft"]
    assert old_anchor is not None
    old_state = old_workspace["state"]
    assert old_state.checkpoint.current_candidate_ref == old_anchor
    assert old_state.checkpoint.redo_candidate_refs
    # submit_draft publishes the rebase anchor and receipt atomically.
    rebased = workflow_action(
        root,
        "wfr_test",
        "act_checkpoint_rebase_submit",
        "submit_draft",
        rebase_input,
    )
    new_anchor = rebased.workflow_run.artifact_refs["content_draft"]
    receipt_ref = rebased.workflow_run.last_receipt_ref
    assert new_anchor is not None and receipt_ref is not None
    project_before_reset = ProjectStore(root).load()
    run_before_reset = WorkflowStore(root).read_run("wfr_test")
    artifact_bytes = {
        path: path.read_bytes()
        for path in (root / "content-drafts").glob("*.json")
    }
    reset = open_draft_workspace(
        root,
        run_id="wfr_test",
    )
    assert reset.checkpoint.generation == old_state.checkpoint.generation + 1
    assert reset.checkpoint.current_candidate_ref == new_anchor
    assert reset.checkpoint.redo_candidate_refs == ()
    assert ProjectStore(root).load() == project_before_reset
    assert WorkflowStore(root).read_run("wfr_test") == run_before_reset
    assert all(path.read_bytes() == data for path, data in artifact_bytes.items())


def test_rebase_receipt_resets_edited_descendant_to_exact_new_anchor(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    old_workspace = {}

    def edit_before_reapproval(_first: object) -> None:
        initialized = initialize_draft_workspace(
            root,
            run_id="wfr_test",
            operation_id="dwop_0_00000000000000000000000000000046",
        )
        child_ref = _workspace_child(
            root,
            initialized.current_candidate,
            child_id="draft_old_descendant",
        )
        old_workspace["state"] = select_draft_workspace_candidate(
            root,
            run_id="wfr_test",
            operation_id="dwop_1_00000000000000000000000000000047",
            expected_checkpoint_ref=initialized.checkpoint_ref,
            expected_current_candidate_ref=initialized.checkpoint.current_candidate_ref,
            child_candidate_ref=child_ref,
        )

    first, rebase_input = _prepare_rebase_input(
        root,
        before_reapproval=edit_before_reapproval,
    )
    old_anchor = first.workflow_run.artifact_refs["content_draft"]
    old_state = old_workspace["state"]
    assert old_anchor is not None
    assert old_state.current_candidate.parent_draft_id == old_anchor.artifact_id
    assert old_state.checkpoint.redo_candidate_refs == ()
    rebased = workflow_action(
        root,
        "wfr_test",
        "act_checkpoint_descendant_rebase_submit",
        "submit_draft",
        rebase_input,
    )
    new_anchor = rebased.workflow_run.artifact_refs["content_draft"]
    assert new_anchor is not None
    project_before_reset = ProjectStore(root).load()
    run_before_reset = WorkflowStore(root).read_run("wfr_test")
    artifact_bytes = {
        path: path.read_bytes()
        for path in (root / "content-drafts").glob("*.json")
    }

    reset = open_draft_workspace(root, run_id="wfr_test")

    assert reset.checkpoint.generation == old_state.checkpoint.generation + 1
    assert reset.checkpoint.current_candidate_ref == new_anchor
    assert reset.checkpoint.redo_candidate_refs == ()
    assert ProjectStore(root).load() == project_before_reset
    assert WorkflowStore(root).read_run("wfr_test") == run_before_reset
    assert all(path.read_bytes() == data for path, data in artifact_bytes.items())


def _returned_confirmed_workspace(
    root: Path,
) -> tuple[ArtifactRef, DraftWorkspaceState, ReceiptRef]:
    submitted = _submit_basic_draft(root)
    submitted_ref = ArtifactRef.from_dict(_draft_ref_from_mutation(submitted))
    initialized = initialize_draft_workspace(
        root,
        run_id="wfr_test",
        operation_id="dwop_0_00000000000000000000000000000061",
    )
    approved = workflow_action(
        root,
        "wfr_test",
        "act_rebase_confirmed_approve",
        "approve_draft",
        {
            "schema_version": 1,
            "content_draft_ref": submitted_ref.to_dict(),
        },
    )
    proposal = approved.workflow_run.artifact_refs["proposal"]
    assert proposal is not None
    adopted = workflow_action(
        root,
        "wfr_test",
        "act_rebase_confirmed_adopt",
        "adopt_roughcut",
        {"schema_version": 1, "proposal_ref": proposal.to_dict()},
    )
    decision = adopted.workflow_run.artifact_refs["decision"]
    confirmed = adopted.workflow_run.artifact_refs["content_draft"]
    assert decision is not None and confirmed is not None
    returned = workflow_action(
        root,
        "wfr_test",
        "act_rebase_confirmed_return",
        "return_to_draft",
        {
            "schema_version": 1,
            "current_subject_ref": {"kind": "decision", **decision.to_dict()},
            "confirmed_content_draft_ref": confirmed.to_dict(),
        },
    )
    return_receipt_ref = returned.workflow_run.last_receipt_ref
    assert return_receipt_ref is not None
    reset = reset_draft_workspace_after_return(
        root,
        run_id="wfr_test",
        operation_id="dwop_1_00000000000000000000000000000062",
        expected_checkpoint_ref=initialized.checkpoint_ref,
        expected_current_candidate_ref=initialized.checkpoint.current_candidate_ref,
        return_receipt_ref=return_receipt_ref,
        confirmed_anchor_ref=confirmed,
    )
    assert reset.current_candidate.confirmed_by_user is True
    assert reset.checkpoint.last_commit.operation == "return_to_draft_reset"
    assert reset.checkpoint.redo_candidate_refs == ()
    return confirmed, reset, return_receipt_ref


def _submit_same_basis_rebase(root: Path, parent_ref: ArtifactRef):
    run, brief_ref, context_hash = _current_draft_context(root)
    parent_payload = json.loads(
        (root / "content-drafts" / f"{parent_ref.artifact_id}.json").read_text(
            encoding="utf-8"
        )
    )
    return workflow_action(
        root,
        "wfr_test",
        "act_rebase_confirmed_submit",
        "submit_draft",
        {
            "schema_version": 1,
            "parent_draft_ref": parent_ref.to_dict(),
            "display_title": parent_payload["display_title"],
            "source_bindings": [
                {
                    "source_id": binding.source_id,
                    "transcript_version_id": binding.transcript_version_id,
                }
                for binding in run.ordered_bindings
            ],
            "brief_ref": brief_ref.to_dict(),
            "context_hash": context_hash,
            "blocks": parent_payload["blocks"],
            "scoped_mutable_block_ids": [],
        },
    )


def test_open_recovers_confirmed_return_checkpoint_from_exact_rebase_receipt(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    confirmed, returned_workspace, _return_receipt_ref = _returned_confirmed_workspace(
        root
    )
    rebased = _submit_same_basis_rebase(root, confirmed)
    new_anchor = rebased.workflow_run.artifact_refs["content_draft"]
    submit_receipt_ref = rebased.workflow_run.last_receipt_ref
    assert new_anchor is not None and submit_receipt_ref is not None
    assert new_anchor != confirmed
    checkpoint_path = root / "workflow" / "draft-workspaces" / "wfr_test.json"
    checkpoint_before = checkpoint_path.read_bytes()

    reopened = open_draft_workspace(
        root,
        run_id="wfr_test",
        audit_review_session_id="review_session_rebase_reopen",
    )

    assert checkpoint_before != checkpoint_path.read_bytes()
    assert reopened.current_candidate.confirmed_by_user is False
    assert reopened.checkpoint.generation == (
        returned_workspace.checkpoint.generation + 1
    )
    assert reopened.checkpoint.current_candidate_ref == new_anchor
    assert reopened.checkpoint.redo_candidate_refs == ()
    assert reopened.checkpoint.last_commit.operation == "workflow_anchor_reset"
    restarted = open_draft_workspace(
        root,
        run_id="wfr_test",
        audit_review_session_id="review_session_rebase_restart",
    )
    assert restarted.checkpoint == reopened.checkpoint
    assert restarted.current_candidate.content_draft_id == new_anchor.artifact_id


def _ordinary_sibling(
    root: Path,
    parent: ContentDraft,
    *,
    child_id: str,
) -> ArtifactRef:
    child = prepare_content_draft_editor_child(
        parent=parent,
        blocks=parent.blocks,
        child_id=child_id,
    )
    write_new_json(root / "content-drafts" / f"{child_id}.json", child.to_dict())
    return ArtifactRef(
        child.content_draft_id,
        child.schema_version,
        subject_content_hash("content_draft", child.schema_version, child.to_dict()),
    )


def _submit_external_same_basis_child(
    root: Path,
    parent_ref: ArtifactRef,
    *,
    action_id: str,
    block_count: int,
):
    run, brief_ref, context_hash = _current_draft_context(root)
    assert brief_ref is not None
    return workflow_action(
        root,
        "wfr_test",
        action_id,
        "submit_draft",
        {
            "schema_version": 1,
            "parent_draft_ref": parent_ref.to_dict(),
            "display_title": "校园探访",
            "source_bindings": [
                {
                    "source_id": binding.source_id,
                    "transcript_version_id": binding.transcript_version_id,
                }
                for binding in run.ordered_bindings
            ],
            "brief_ref": brief_ref.to_dict(),
            "context_hash": context_hash,
            "blocks": _schema2_blocks(block_count),
            "scoped_mutable_block_ids": [],
        },
    )


def test_external_submit_chain_page_edit_undo_redo_and_approve_exact_current(
    tmp_path: Path,
) -> None:
    root, _first, child_ref = _workspace_fixture(tmp_path)
    initialized = initialize_draft_workspace(
        root,
        run_id="wfr_test",
        operation_id="dwop_0_00000000000000000000000000000072",
    )
    opened = open_draft_workspace(root, run_id="wfr_test")
    assert opened.current_candidate.content_draft_id == child_ref.artifact_id
    assert opened.checkpoint.generation == initialized.checkpoint.generation + 1

    next_result = _submit_external_same_basis_child(
        root,
        child_ref,
        action_id="act_external_submit_grandchild",
        block_count=3,
    )
    next_ref = ArtifactRef.from_dict(_draft_ref_from_mutation(next_result))
    reconciled = open_draft_workspace(root, run_id="wfr_test")
    assert reconciled.current_candidate.content_draft_id == next_ref.artifact_id

    edited = edit_draft_workspace(
        root,
        run_id="wfr_test",
        operation_id="dwop_3_00000000000000000000000000000073",
        expected_checkpoint_ref=reconciled.checkpoint_ref,
        expected_current_candidate_ref=next_ref,
        operation="delete",
        selection=_first_paragraph_selection(root, next_ref.artifact_id),
        caret=None,
        accept_degraded=True,
    )
    undone = undo_draft_workspace(
        root,
        run_id="wfr_test",
        operation_id="dwop_4_00000000000000000000000000000074",
        expected_checkpoint_ref=edited.checkpoint_ref,
        expected_current_candidate_ref=edited.checkpoint.current_candidate_ref,
    )
    assert undone.checkpoint.current_candidate_ref == next_ref
    assert undone.checkpoint.redo_candidate_refs == (
        edited.checkpoint.current_candidate_ref,
    )
    redone = redo_draft_workspace(
        root,
        run_id="wfr_test",
        operation_id="dwop_5_00000000000000000000000000000075",
        expected_checkpoint_ref=undone.checkpoint_ref,
        expected_current_candidate_ref=next_ref,
    )
    assert redone.checkpoint.current_candidate_ref == edited.checkpoint.current_candidate_ref
    assert redone.checkpoint.redo_candidate_refs == ()

    approved = workflow_action(
        root,
        "wfr_test",
        "act_external_submit_approve_current",
        "approve_draft",
        {
            "schema_version": 1,
            "content_draft_ref": redone.checkpoint.current_candidate_ref.to_dict(),
        },
    )
    assert approved.receipt is not None
    assert approved.receipt.action == "approve_draft"
    assert WorkflowStore(root).read_run("wfr_test").stage == "roughcut_review"


def test_external_submit_current_candidate_reopens_after_undo_redo(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    first = _submit_basic_draft(root, action_id="act_restart_parent")
    anchor_ref = ArtifactRef.from_dict(_draft_ref_from_mutation(first))
    initialized = initialize_draft_workspace(
        root,
        run_id="wfr_test",
        operation_id="dwop_0_00000000000000000000000000000077",
    )
    submitted = _submit_external_same_basis_child(
        root,
        anchor_ref,
        action_id="act_restart_child",
        block_count=2,
    )
    child_ref = ArtifactRef.from_dict(_draft_ref_from_mutation(submitted))
    reconciled = open_draft_workspace(root, run_id="wfr_test")
    assert reconciled.current_candidate.content_draft_id == child_ref.artifact_id
    assert reconciled.checkpoint.generation == initialized.checkpoint.generation + 1

    undone = undo_draft_workspace(
        root,
        run_id="wfr_test",
        operation_id="dwop_2_00000000000000000000000000000078",
        expected_checkpoint_ref=reconciled.checkpoint_ref,
        expected_current_candidate_ref=child_ref,
    )
    assert undone.checkpoint.current_candidate_ref == anchor_ref
    redone = redo_draft_workspace(
        root,
        run_id="wfr_test",
        operation_id="dwop_3_00000000000000000000000000000079",
        expected_checkpoint_ref=undone.checkpoint_ref,
        expected_current_candidate_ref=anchor_ref,
    )
    assert redone.checkpoint.current_candidate_ref == child_ref
    checkpoint_path = root / "workflow" / "draft-workspaces" / "wfr_test.json"
    checkpoint_bytes = checkpoint_path.read_bytes()

    restarted = open_draft_workspace(
        root,
        run_id="wfr_test",
        audit_review_session_id="review_session_after_redo",
    )

    assert restarted.readback is True
    assert restarted.current_candidate.content_draft_id == child_ref.artifact_id
    assert restarted.checkpoint == redone.checkpoint
    assert checkpoint_path.read_bytes() == checkpoint_bytes


def test_external_submit_page_edit_reopens_after_undo_and_redo_restart(
    tmp_path: Path,
) -> None:
    root, _first, child_ref = _workspace_fixture(tmp_path)
    _initialized, _selected = _select_child(root, child_ref)
    submitted = _submit_external_same_basis_child(
        root,
        child_ref,
        action_id="act_restart_grandchild",
        block_count=3,
    )
    grandchild_ref = ArtifactRef.from_dict(_draft_ref_from_mutation(submitted))
    reconciled = open_draft_workspace(root, run_id="wfr_test")
    assert reconciled.current_candidate.content_draft_id == grandchild_ref.artifact_id

    edited = edit_draft_workspace(
        root,
        run_id="wfr_test",
        operation_id="dwop_3_00000000000000000000000000000080",
        expected_checkpoint_ref=reconciled.checkpoint_ref,
        expected_current_candidate_ref=grandchild_ref,
        operation="delete",
        selection=_first_paragraph_selection(root, grandchild_ref.artifact_id),
        caret=None,
        accept_degraded=True,
    )
    undone = undo_draft_workspace(
        root,
        run_id="wfr_test",
        operation_id="dwop_4_00000000000000000000000000000081",
        expected_checkpoint_ref=edited.checkpoint_ref,
        expected_current_candidate_ref=edited.checkpoint.current_candidate_ref,
    )
    assert undone.checkpoint.current_candidate_ref == grandchild_ref
    assert undone.checkpoint.redo_candidate_refs == (
        edited.checkpoint.current_candidate_ref,
    )
    checkpoint_path = root / "workflow" / "draft-workspaces" / "wfr_test.json"
    checkpoint_after_undo = checkpoint_path.read_bytes()

    reopened_at_parent = open_draft_workspace(
        root,
        run_id="wfr_test",
        audit_review_session_id="review_session_after_undo",
    )

    assert reopened_at_parent.current_candidate.content_draft_id == grandchild_ref.artifact_id
    assert reopened_at_parent.checkpoint == undone.checkpoint
    assert checkpoint_path.read_bytes() == checkpoint_after_undo

    redone = redo_draft_workspace(
        root,
        run_id="wfr_test",
        operation_id="dwop_5_00000000000000000000000000000082",
        expected_checkpoint_ref=reopened_at_parent.checkpoint_ref,
        expected_current_candidate_ref=grandchild_ref,
    )
    assert redone.checkpoint.current_candidate_ref == edited.checkpoint.current_candidate_ref
    checkpoint_after_redo = checkpoint_path.read_bytes()

    restarted_at_child = open_draft_workspace(
        root,
        run_id="wfr_test",
        audit_review_session_id="review_session_after_redo",
    )

    assert restarted_at_child.current_candidate.content_draft_id == edited.checkpoint.current_candidate_ref.artifact_id
    assert restarted_at_child.checkpoint == redone.checkpoint
    assert checkpoint_path.read_bytes() == checkpoint_after_redo


def test_external_submit_stale_sibling_preserves_current_checkpoint(
    tmp_path: Path,
) -> None:
    root, first, _child_ref = _workspace_fixture(tmp_path)
    anchor_ref = first.workflow_run.artifact_refs["content_draft"]
    assert anchor_ref is not None
    initialize_draft_workspace(
        root,
        run_id="wfr_test",
        operation_id="dwop_0_00000000000000000000000000000076",
    )
    current = open_draft_workspace(root, run_id="wfr_test")
    current_ref = current.checkpoint.current_candidate_ref
    checkpoint_path = root / "workflow" / "draft-workspaces" / "wfr_test.json"
    before = checkpoint_path.read_bytes()

    stale_result = _submit_external_same_basis_child(
        root,
        anchor_ref,
        action_id="act_external_submit_stale_sibling",
        block_count=2,
    )
    stale_ref = ArtifactRef.from_dict(_draft_ref_from_mutation(stale_result))
    assert stale_ref != current_ref

    with pytest.raises(WorkflowError) as raised:
        open_draft_workspace(root, run_id="wfr_test")

    assert raised.value.code == "draft_workspace_stale"
    assert checkpoint_path.read_bytes() == before
    persisted = read_draft_workspace(root, run_id="wfr_test")
    assert persisted is not None
    assert persisted.checkpoint.current_candidate_ref == current_ref


def test_external_submit_cas_race_loser_cannot_overwrite_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, first, _child_ref = _workspace_fixture(tmp_path)
    anchor_ref = first.workflow_run.artifact_refs["content_draft"]
    assert anchor_ref is not None
    initialized = initialize_draft_workspace(
        root,
        run_id="wfr_test",
        operation_id="dwop_0_00000000000000000000000000000078",
    )
    competitor_ref = _ordinary_sibling(
        root,
        initialized.current_candidate,
        child_id="draft_external_submit_cas_competitor",
    )
    _submit_external_same_basis_child(
        root,
        anchor_ref,
        action_id="act_external_submit_cas_loser",
        block_count=2,
    )
    original_write_locked = checkpoint_store_module.DraftWorkspaceStore.write_locked
    raced = False

    def race_write(
        store: checkpoint_store_module.DraftWorkspaceStore,
        checkpoint: object,
        *,
        expected_ref: object,
    ) -> None:
        nonlocal raced
        if (
            not raced
            and isinstance(checkpoint, DraftWorkspaceCheckpoint)
            and checkpoint.last_commit.operation == "external_submit_advance"
        ):
            raced = True
            current = store.read_locked("wfr_test")
            assert current is not None
            select_draft_workspace_candidate(
                root,
                run_id="wfr_test",
                operation_id="dwop_1_00000000000000000000000000000079",
                expected_checkpoint_ref=DraftWorkspaceCheckpointRef.for_checkpoint(
                    current
                ),
                expected_current_candidate_ref=current.current_candidate_ref,
                child_candidate_ref=competitor_ref,
            )
        original_write_locked(
            store,
            checkpoint,
            expected_ref=expected_ref,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(
        checkpoint_store_module.DraftWorkspaceStore,
        "write_locked",
        race_write,
    )
    with pytest.raises(WorkflowError) as raised:
        open_draft_workspace(root, run_id="wfr_test")

    assert raced is True
    assert raised.value.code == "draft_workspace_stale"
    persisted = read_draft_workspace(root, run_id="wfr_test")
    assert persisted is not None
    assert persisted.checkpoint.generation == initialized.checkpoint.generation + 1
    assert persisted.checkpoint.current_candidate_ref == competitor_ref


@pytest.mark.parametrize("tamper", ["missing", "malformed"])
def test_external_submit_malformed_or_missing_receipt_fails_closed(
    tmp_path: Path,
    tamper: str,
) -> None:
    root, first, _child_ref = _workspace_fixture(tmp_path)
    anchor_ref = first.workflow_run.artifact_refs["content_draft"]
    assert anchor_ref is not None
    initialize_draft_workspace(
        root,
        run_id="wfr_test",
        operation_id="dwop_0_00000000000000000000000000000077",
    )
    child_result = _submit_external_same_basis_child(
        root,
        anchor_ref,
        action_id=f"act_external_submit_tamper_{tamper}",
        block_count=2,
    )
    receipt_ref = child_result.workflow_run.last_receipt_ref
    assert receipt_ref is not None
    receipt_path = root / "workflow" / "receipts" / f"{receipt_ref.action_id}.json"
    if tamper == "missing":
        receipt_path.unlink()
    else:
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
        payload["mutation"] = None
        receipt_path.write_text(json.dumps(payload), encoding="utf-8")
    checkpoint_path = root / "workflow" / "draft-workspaces" / "wfr_test.json"
    before = checkpoint_path.read_bytes()

    with pytest.raises(WorkflowError):
        open_draft_workspace(root, run_id="wfr_test")

    assert checkpoint_path.read_bytes() == before


def _replace_run_draft_anchor(
    root: Path,
    *,
    anchor_ref: ArtifactRef,
    receipt_ref: ReceiptRef,
) -> None:
    store = WorkflowStore(root)
    current = store.read_run("wfr_test")
    artifacts = dict(current.artifact_refs)
    artifacts["content_draft"] = anchor_ref
    store.write_run(
        replace(
            current,
            artifact_refs=artifacts,
            last_receipt_ref=receipt_ref,
        ),
        expected_run_hash=canonical_sha256_v1(current.to_dict()),
    )


def test_open_does_not_recover_confirmed_checkpoint_from_unrelated_receipt(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    confirmed, returned_workspace, return_receipt_ref = _returned_confirmed_workspace(
        root
    )
    rebased = _submit_same_basis_rebase(root, confirmed)
    new_anchor = rebased.workflow_run.artifact_refs["content_draft"]
    assert new_anchor is not None
    _replace_run_draft_anchor(
        root,
        anchor_ref=new_anchor,
        receipt_ref=return_receipt_ref,
    )
    checkpoint_path = root / "workflow" / "draft-workspaces" / "wfr_test.json"
    checkpoint_before = checkpoint_path.read_bytes()

    with pytest.raises(WorkflowError) as raised:
        open_draft_workspace(root, run_id="wfr_test")

    assert raised.value.code == "draft_workspace_integrity_error"
    assert checkpoint_path.read_bytes() == checkpoint_before
    assert returned_workspace.checkpoint.current_candidate_ref == confirmed


def test_open_does_not_recover_when_receipt_mutation_and_run_anchor_differ(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    confirmed, returned_workspace, _return_receipt_ref = _returned_confirmed_workspace(
        root
    )
    rebased = _submit_same_basis_rebase(root, confirmed)
    submit_receipt_ref = rebased.workflow_run.last_receipt_ref
    assert submit_receipt_ref is not None
    sibling_ref = _ordinary_sibling(
        root,
        returned_workspace.current_candidate,
        child_id="draft_receipt_anchor_mismatch",
    )
    _replace_run_draft_anchor(
        root,
        anchor_ref=sibling_ref,
        receipt_ref=submit_receipt_ref,
    )
    checkpoint_path = root / "workflow" / "draft-workspaces" / "wfr_test.json"
    checkpoint_before = checkpoint_path.read_bytes()

    with pytest.raises(WorkflowError) as raised:
        open_draft_workspace(root, run_id="wfr_test")

    assert raised.value.code == "draft_workspace_integrity_error"
    assert checkpoint_path.read_bytes() == checkpoint_before


def test_open_does_not_recover_an_ordinary_sibling_checkpoint(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    confirmed, returned_workspace, _return_receipt_ref = _returned_confirmed_workspace(
        root
    )
    rebased = _submit_same_basis_rebase(root, confirmed)
    assert rebased.workflow_run.artifact_refs["content_draft"] is not None
    confirmed_parent_id = returned_workspace.current_candidate.parent_draft_id
    assert confirmed_parent_id is not None
    confirmed_parent = ContentDraft.from_dict(
        json.loads(
            (
                root
                / "content-drafts"
                / f"{confirmed_parent_id}.json"
            ).read_text(encoding="utf-8")
        )
    )
    sibling_ref = _ordinary_sibling(
        root,
        confirmed_parent,
        child_id="draft_ordinary_sibling",
    )
    checkpoint_path = root / "workflow" / "draft-workspaces" / "wfr_test.json"
    payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    payload["current_candidate_ref"] = sibling_ref.to_dict()
    payload["last_commit"]["result_candidate_ref"] = sibling_ref.to_dict()
    checkpoint_path.write_text(json.dumps(payload), encoding="utf-8")
    checkpoint_before = checkpoint_path.read_bytes()

    with pytest.raises(WorkflowError) as raised:
        open_draft_workspace(root, run_id="wfr_test")

    assert raised.value.code in {
        "draft_workspace_integrity_error",
        "draft_workspace_stale",
    }
    assert checkpoint_path.read_bytes() == checkpoint_before


@pytest.mark.parametrize("failure", ["corrupt", "cross_run"])
def test_open_does_not_recover_corrupt_or_cross_run_checkpoint(
    tmp_path: Path,
    failure: str,
) -> None:
    root = _workflow_project(tmp_path)
    confirmed, _returned_workspace, _return_receipt_ref = _returned_confirmed_workspace(
        root
    )
    _submit_same_basis_rebase(root, confirmed)
    checkpoint_path = root / "workflow" / "draft-workspaces" / "wfr_test.json"
    if failure == "corrupt":
        checkpoint_path.write_text("{", encoding="utf-8")
    else:
        payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        payload["workflow_run_id"] = "wfr_other"
        checkpoint_path.write_text(json.dumps(payload), encoding="utf-8")
    checkpoint_before = checkpoint_path.read_bytes()

    with pytest.raises(WorkflowError) as raised:
        open_draft_workspace(root, run_id="wfr_test")

    assert raised.value.code == "draft_workspace_integrity_error"
    assert checkpoint_path.read_bytes() == checkpoint_before


def test_open_recovers_a_mechanically_edited_descendant_checkpoint(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    confirmed, returned_workspace, _return_receipt_ref = _returned_confirmed_workspace(
        root
    )
    parent = returned_workspace.current_candidate
    inserted = replace(parent.blocks[0], block_id="block_mechanical_insert")
    child = prepare_content_draft_editor_child(
        parent=parent,
        blocks=(*parent.blocks, inserted),
        child_id="draft_mechanical_edit",
    )
    write_new_json(
        root / "content-drafts" / "draft_mechanical_edit.json",
        child.to_dict(),
    )
    child_ref = ArtifactRef(
        child.content_draft_id,
        child.schema_version,
        subject_content_hash("content_draft", child.schema_version, child.to_dict()),
    )
    checkpoint_path = root / "workflow" / "draft-workspaces" / "wfr_test.json"
    payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    payload["current_candidate_ref"] = child_ref.to_dict()
    payload["last_commit"]["operation"] = "edit_insert"
    payload["last_commit"]["result_candidate_ref"] = child_ref.to_dict()
    checkpoint_path.write_text(json.dumps(payload), encoding="utf-8")
    rebased = _submit_same_basis_rebase(root, confirmed)
    new_anchor = rebased.workflow_run.artifact_refs["content_draft"]
    assert new_anchor is not None
    project_before_reset = ProjectStore(root).load()
    run_before_reset = WorkflowStore(root).read_run("wfr_test")
    artifact_bytes = {
        path: path.read_bytes()
        for path in (root / "content-drafts").glob("*.json")
    }

    reopened = open_draft_workspace(root, run_id="wfr_test")

    assert reopened.checkpoint.generation == returned_workspace.checkpoint.generation + 1
    assert reopened.checkpoint.current_candidate_ref == new_anchor
    assert reopened.checkpoint.redo_candidate_refs == ()
    assert ProjectStore(root).load() == project_before_reset
    assert WorkflowStore(root).read_run("wfr_test") == run_before_reset
    assert all(path.read_bytes() == data for path, data in artifact_bytes.items())


_DWC_VECTOR_OWNERS = {
    "DWC-001": "test_initialize_select_edit_undo_redo_and_restart_are_checkpoint_backed",
    "DWC-002": "test_initialize_select_edit_undo_redo_and_restart_are_checkpoint_backed",
    "DWC-003": "test_response_loss_readback_and_same_operation_different_input_conflict",
    "DWC-004": "test_response_loss_readback_and_same_operation_different_input_conflict",
    "DWC-005": "test_two_application_instances_cas_loser_is_stale",
    "DWC-006": "test_child_publish_failure_keeps_checkpoint_and_creates_no_child",
    "DWC-007": "test_checkpoint_temp_write_failure_cleans_only_owned_child",
    "DWC-008": "test_checkpoint_publish_failure_cleans_only_owned_child",
    "DWC-009": "test_hard_exit_after_child_before_checkpoint_preserves_old_current_and_orphan",
    "DWC-010": "test_ten_narration_edits_and_undo_branch_preserve_immutable_history",
    "DWC-011": "test_initialize_select_edit_undo_redo_and_restart_are_checkpoint_backed",
    "DWC-012": "test_initialize_select_edit_undo_redo_and_restart_are_checkpoint_backed",
    "DWC-013": "test_ten_narration_edits_and_undo_branch_preserve_immutable_history",
    "DWC-014": "test_initialize_select_edit_undo_redo_and_restart_are_checkpoint_backed",
    "DWC-015": "test_external_project_revision_fails_closed",
    "DWC-016": "test_corrupt_missing_cross_project_and_invalid_redo_fail_closed",
    "DWC-017": "adapters/test_draft_workspace_store.py::test_checkpoint_unsafe_file_fails_closed[symlink]",
    "DWC-018": "adapters/test_draft_workspace_store.py::test_checkpoint_unsafe_file_fails_closed[hardlink]",
    "DWC-019": "test_corrupt_missing_cross_project_and_invalid_redo_fail_closed",
    "DWC-020": "adapters/test_draft_workspace_store.py::test_missing_checkpoint_read_does_not_create_workflow_tree",
    "DWC-021": "test_approve_draft_dormancy_and_return_reset",
    "DWC-022": "test_approve_draft_dormancy_and_return_reset",
    "DWC-023": "test_approve_draft_dormancy_and_return_reset",
    "DWC-024": "test_corrupt_missing_cross_project_and_invalid_redo_fail_closed",
    "DWC-025": "test_corrupt_missing_cross_project_and_invalid_redo_fail_closed",
    "DWC-026": "test_approve_draft_dormancy_and_return_reset",
    "DWC-027": "test_rebase_receipt_resets_checkpoint_with_nonempty_redo_to_exact_new_anchor",
}


def test_all_dwc_vectors_have_explicit_executable_semantic_owner() -> None:
    vector_path = (
        Path(__file__).parents[3]
        / "core"
        / "tests"
        / "fixtures"
        / "draft-workspace-checkpoint-vectors.json"
    )
    vector_ids = {
        item["id"] for item in json.loads(vector_path.read_text(encoding="utf-8"))["vectors"]
    }

    assert vector_ids == set(_DWC_VECTOR_OWNERS)
    assert all("test_" in owner for owner in _DWC_VECTOR_OWNERS.values())
