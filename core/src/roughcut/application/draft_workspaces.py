"""Review-only application coordination for Draft Workspace Checkpoints."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from roughcut.adapters.artifact_store import read_json_object
from roughcut.adapters.draft_workspace_store import DraftWorkspaceStore
from roughcut.adapters.project_store import ProjectStore
from roughcut.adapters.workflow_store import WorkflowStore
from roughcut.application.agent_context import (
    calculate_agent_context_hash,
    read_edit_brief,
)
from roughcut.application.draft_editor import (
    DraftEditorCaret,
    DraftEditorPlacementResult,
    DraftEditorPreparedChild,
    DraftEditorSelection,
    DraftEditorSnapshot,
    _persisted_source_paragraph_key,
    _range_offsets,
    _trusted_boundary_pair,
    load_draft_editor_snapshot,
    materialize_draft_editor_placement,
    prepare_draft_candidate_with_placement,
    prepare_draft_editor_result_selection,
    prepare_draft_narration,
    prepare_draft_punctuation,
    resolve_draft_editor_caret,
    validate_draft_editor_snapshot,
)
from roughcut.application.workflow_review import reopen_workflow_review_content_draft
from roughcut.domain.bindings import SourceTranscriptBinding
from roughcut.domain.brief import EditBrief
from roughcut.domain.content_draft import (
    ContentDraft,
    ContentDraftBlock,
    ContentDraftRef,
    NarrationBlock,
    SectionTitleBlock,
    SourceExcerptBlock,
    split_display_text_for_canonical_parts,
)
from roughcut.domain.draft_workspace import (
    DraftWorkspaceBinding,
    DraftWorkspaceCheckpoint,
    DraftWorkspaceCheckpointRef,
    DraftWorkspaceLastCommit,
    draft_workspace_input_hash,
)
from roughcut.domain.errors import WorkflowError
from roughcut.domain.project import Project, ProjectError
from roughcut.domain.readable_transcript import (
    canonical_text_for_range,
    exact_fine_unit_spans,
    trusted_fine_unit_spans,
)
from roughcut.domain.transcript import TimedTranscript, TranscriptSegment
from roughcut.domain.workflow import (
    ArtifactRef,
    ReceiptRef,
    WorkflowRun,
    canonical_sha256_v1,
    subject_content_hash,
)


def _error(code: str, evidence: str) -> WorkflowError:
    return WorkflowError(code, f"Roughcut Draft workspace {evidence}")


@dataclass(frozen=True)
class DraftWorkspaceState:
    checkpoint: DraftWorkspaceCheckpoint
    checkpoint_ref: DraftWorkspaceCheckpointRef
    current_candidate: ContentDraft
    readback: bool
    placement: DraftEditorPlacementResult | None = None
    result_selection: dict[str, object] | None = None


@dataclass(frozen=True)
class _Basis:
    project: Project
    run: WorkflowRun
    bindings: tuple[DraftWorkspaceBinding, ...]
    source_bindings: tuple[SourceTranscriptBinding, ...]
    brief: EditBrief
    context_hash: str
    anchor_ref: ArtifactRef


def read_draft_workspace(
    project_path: Path, *, run_id: str
) -> DraftWorkspaceState | None:
    store = DraftWorkspaceStore(project_path)
    with store.write_lock():
        checkpoint = store.read_locked(run_id)
        if checkpoint is None:
            return None
        basis = _current_basis(store, run_id)
        current = _validate_checkpoint(store, checkpoint, basis)
        return _state(checkpoint, current, readback=False)


def open_draft_workspace(
    project_path: Path,
    *,
    run_id: str,
    audit_review_session_id: str | None = None,
) -> DraftWorkspaceState:
    """Open the exact Review workspace, applying only receipt-backed resets."""

    store = DraftWorkspaceStore(project_path)
    with store.write_lock():
        checkpoint = store.read_locked(run_id)
        basis = _current_basis(store, run_id)
        if checkpoint is not None:
            submit_receipt_ref = _exact_submit_rebase_receipt_for_open(
                store,
                checkpoint=checkpoint,
                basis=basis,
            )
            if submit_receipt_ref is not None:
                return reset_draft_workspace_after_anchor_rebase(
                    store.project_path,
                    run_id=run_id,
                    operation_id=_new_operation_id(checkpoint.generation),
                    expected_checkpoint_ref=(
                        DraftWorkspaceCheckpointRef.for_checkpoint(checkpoint)
                    ),
                    expected_current_candidate_ref=(checkpoint.current_candidate_ref),
                    submit_draft_receipt_ref=submit_receipt_ref,
                    new_anchor_ref=basis.anchor_ref,
                    audit_review_session_id=audit_review_session_id,
                )
            external_submit = _exact_submit_child_receipt_for_open(
                store,
                checkpoint=checkpoint,
                basis=basis,
            )
            if isinstance(external_submit, DraftWorkspaceState):
                return external_submit
            if external_submit is not None:
                external_receipt_ref, child_ref = external_submit
                return _advance_workspace_after_external_submit(
                    store,
                    checkpoint=checkpoint,
                    basis=basis,
                    receipt_ref=external_receipt_ref,
                    child_ref=child_ref,
                    audit_review_session_id=audit_review_session_id,
                )
            try:
                current = _validate_checkpoint(store, checkpoint, basis)
            except WorkflowError as error:
                if error.code != "draft_workspace_stale":
                    raise
                return _reset_stale_workspace_for_open(
                    store,
                    checkpoint=checkpoint,
                    basis=basis,
                    audit_review_session_id=audit_review_session_id,
                )
            return _state(checkpoint, current, readback=False)

        receipt_ref = basis.run.last_receipt_ref
        if receipt_ref is not None:
            receipt = WorkflowStore(store.project_path).read_receipt(
                receipt_ref.action_id,
                run_id=run_id,
            )
            if (
                canonical_sha256_v1(receipt.to_dict()) == receipt_ref.receipt_hash
                and receipt.action == "return_to_draft"
            ):
                return reset_draft_workspace_after_return(
                    store.project_path,
                    run_id=run_id,
                    operation_id=_new_operation_id(0),
                    expected_checkpoint_ref=None,
                    expected_current_candidate_ref=None,
                    return_receipt_ref=receipt_ref,
                    confirmed_anchor_ref=basis.anchor_ref,
                    audit_review_session_id=audit_review_session_id,
                )
        return initialize_draft_workspace(
            store.project_path,
            run_id=run_id,
            operation_id=_new_operation_id(0),
            audit_review_session_id=audit_review_session_id,
        )


def initialize_draft_workspace(
    project_path: Path,
    *,
    run_id: str,
    operation_id: str,
    audit_review_session_id: str | None = None,
) -> DraftWorkspaceState:
    store = DraftWorkspaceStore(project_path)
    with store.write_lock():
        basis = _current_basis(store, run_id)
        existing = store.read_locked(run_id)
        anchor = _read_exact_draft(store, basis.anchor_ref)
        input_hash = draft_workspace_input_hash(
            project_id=basis.project.project_id,
            workflow_run_id=run_id,
            operation_id=operation_id,
            operation="initialize",
            expected_checkpoint_ref=None,
            expected_current_candidate_ref=basis.anchor_ref,
            input_payload={},
        )
        if existing is not None:
            return _readback_or_conflict(
                store,
                existing,
                basis=basis,
                operation_id=operation_id,
                input_hash=input_hash,
            )
        checkpoint = _checkpoint_after(
            basis,
            generation=1,
            current_ref=basis.anchor_ref,
            redo_refs=(),
            operation_id=operation_id,
            expected_generation=0,
            operation="initialize",
            input_hash=input_hash,
            audit_review_session_id=audit_review_session_id,
        )
        store.write_locked(checkpoint, expected_ref=None)
        return _state(checkpoint, anchor, readback=False)


def edit_draft_workspace(
    project_path: Path,
    *,
    run_id: str,
    operation_id: str,
    expected_checkpoint_ref: DraftWorkspaceCheckpointRef,
    expected_current_candidate_ref: ArtifactRef,
    operation: str,
    selection: DraftEditorSelection,
    caret: DraftEditorCaret | None,
    accept_degraded: bool,
    audit_review_session_id: str | None = None,
    prepared_editor_snapshot: DraftEditorSnapshot | None = None,
) -> DraftWorkspaceState:
    operation_name = {
        "delete": "edit_delete",
        "move": "edit_move",
        "insert": "edit_insert",
    }.get(operation)
    if operation_name is None:
        raise _error(
            "draft_workspace_integrity_error",
            "rejected an unsupported mechanical edit operation",
        )
    input_payload: dict[str, object] = {
        "selection": selection.to_dict(),
        "caret": None if caret is None else caret.to_dict(),
        "accept_degraded": accept_degraded,
    }
    return _commit_child_edit(
        project_path,
        run_id=run_id,
        operation_id=operation_id,
        expected_checkpoint_ref=expected_checkpoint_ref,
        expected_current_candidate_ref=expected_current_candidate_ref,
        operation=operation_name,
        input_payload=input_payload,
        audit_review_session_id=audit_review_session_id,
        prepared_editor_snapshot=prepared_editor_snapshot,
        prepare=lambda snapshot, child_id: prepare_draft_candidate_with_placement(
            snapshot,
            operation=operation,
            selection=selection,
            caret=caret,
            accept_degraded=accept_degraded,
            child_id=child_id,
        ),
    )


def edit_draft_workspace_punctuation(
    project_path: Path,
    *,
    run_id: str,
    operation_id: str,
    expected_checkpoint_ref: DraftWorkspaceCheckpointRef,
    expected_current_candidate_ref: ArtifactRef,
    paragraph_id: str,
    block_id: str,
    start_utf16_offset: int,
    end_utf16_offset: int,
    replacement: str,
    audit_review_session_id: str | None = None,
    prepared_editor_snapshot: DraftEditorSnapshot | None = None,
) -> DraftWorkspaceState:
    payload: dict[str, object] = {
        "paragraph_id": paragraph_id,
        "block_id": block_id,
        "start_utf16_offset": start_utf16_offset,
        "end_utf16_offset": end_utf16_offset,
        "replacement": replacement,
    }
    return _commit_child_edit(
        project_path,
        run_id=run_id,
        operation_id=operation_id,
        expected_checkpoint_ref=expected_checkpoint_ref,
        expected_current_candidate_ref=expected_current_candidate_ref,
        operation="punctuation_edit",
        input_payload=payload,
        audit_review_session_id=audit_review_session_id,
        prepared_editor_snapshot=prepared_editor_snapshot,
        prepare=lambda snapshot, child_id: prepare_draft_punctuation(
            snapshot,
            paragraph_id=paragraph_id,
            block_id=block_id,
            start_utf16_offset=start_utf16_offset,
            end_utf16_offset=end_utf16_offset,
            replacement=replacement,
            child_id=child_id,
        ),
    )


def edit_draft_workspace_narration(
    project_path: Path,
    *,
    run_id: str,
    operation_id: str,
    expected_checkpoint_ref: DraftWorkspaceCheckpointRef,
    expected_current_candidate_ref: ArtifactRef,
    block_id: str,
    text: str,
    audit_review_session_id: str | None = None,
    prepared_editor_snapshot: DraftEditorSnapshot | None = None,
) -> DraftWorkspaceState:
    return _commit_child_edit(
        project_path,
        run_id=run_id,
        operation_id=operation_id,
        expected_checkpoint_ref=expected_checkpoint_ref,
        expected_current_candidate_ref=expected_current_candidate_ref,
        operation="narration_edit",
        input_payload={"block_id": block_id, "text": text},
        audit_review_session_id=audit_review_session_id,
        prepared_editor_snapshot=prepared_editor_snapshot,
        prepare=lambda snapshot, child_id: prepare_draft_narration(
            snapshot,
            block_id=block_id,
            text=text,
            child_id=child_id,
        ),
    )


def edit_draft_workspace_section(
    project_path: Path,
    *,
    run_id: str,
    operation_id: str,
    expected_checkpoint_ref: DraftWorkspaceCheckpointRef,
    expected_current_candidate_ref: ArtifactRef,
    operation: str,
    payload: dict[str, object],
    audit_review_session_id: str | None = None,
    prepared_editor_snapshot: DraftEditorSnapshot | None = None,
) -> DraftWorkspaceState:
    """Apply one closed private section operation through the editor checkpoint."""
    if operation not in {
        "section_reorder",
        "section_rename",
        "section_split",
        "section_merge",
        "section_delete",
    }:
        raise _error("draft_workspace_integrity_error", "rejected an unsupported section operation")
    return _commit_child_edit(
        project_path,
        run_id=run_id,
        operation_id=operation_id,
        expected_checkpoint_ref=expected_checkpoint_ref,
        expected_current_candidate_ref=expected_current_candidate_ref,
        operation=operation,
        input_payload=payload,
        audit_review_session_id=audit_review_session_id,
        prepared_editor_snapshot=prepared_editor_snapshot,
        prepare=lambda snapshot, child_id: prepare_section_operation(
            snapshot,
            operation=operation,
            payload=payload,
            child_id=child_id,
        ),
    )


def prepare_section_operation(
    snapshot: DraftEditorSnapshot,
    *,
    operation: str,
    payload: dict[str, object],
    child_id: str,
) -> ContentDraft:
    """Pure section transform; publication remains owned by _commit_child_edit."""
    _require_section_payload(operation, payload)
    from roughcut.domain.content_draft import project_schema1_to_schema2

    basis = project_schema1_to_schema2(snapshot.candidate)
    blocks = list(basis.blocks)
    headings = [
        (index, block)
        for index, block in enumerate(blocks)
        if isinstance(block, SectionTitleBlock)
    ]
    heading_ids = {block.block_id for _, block in headings}
    heading_id = payload.get("heading_block_id")
    if operation != "section_split" and not isinstance(heading_id, str):
        raise ProjectError("section operation heading_block_id is invalid")
    if operation == "section_split":
        return _prepare_section_split(snapshot, blocks, payload, child_id)
    assert isinstance(heading_id, str)
    if heading_id not in heading_ids:
        raise ProjectError("section operation heading does not exist")
    heading_index = next(index for index, item in headings if item.block_id == heading_id)
    if operation == "section_rename":
        title = payload["title"]
        assert isinstance(title, str)
        blocks[heading_index] = SectionTitleBlock(heading_id, title)
    elif operation == "section_reorder":
        before = payload["before_heading_block_id"]
        if before is not None and not isinstance(before, str):
            raise ProjectError("section reorder target is invalid")
        if before == heading_id:
            raise ProjectError("section reorder cannot target itself")
        if before is not None and before not in heading_ids:
            raise ProjectError("section reorder target does not exist")
        next_index = next(
            (index for index, item in headings if item.block_id == before),
            len(blocks),
        )
        end_index = next(
            (index for index, item in headings if index > heading_index),
            len(blocks),
        )
        if before is not None and heading_index < next_index < end_index:
            raise ProjectError("section reorder target is inside the moved section")
        chunk = blocks[heading_index:end_index]
        del blocks[heading_index:end_index]
        if before is None:
            blocks.extend(chunk)
        else:
            target = next(index for index, item in enumerate(blocks) if isinstance(item, SectionTitleBlock) and item.block_id == before)
            blocks[target:target] = chunk
    elif operation == "section_merge":
        direction = payload["direction"]
        adjacent = payload["adjacent_heading_block_id"]
        if direction not in {"previous", "next"} or not isinstance(adjacent, str):
            raise ProjectError("section merge payload is invalid")
        heading_positions = [index for index, _ in headings]
        position = heading_positions.index(heading_index)
        adjacent_index = (
            position - 1 if direction == "previous" else position + 1
        )
        if adjacent_index < 0 or adjacent_index >= len(headings):
            raise ProjectError("section merge has no adjacent section")
        expected_adjacent = headings[adjacent_index][1].block_id
        if adjacent != expected_adjacent:
            raise ProjectError("section merge adjacency is invalid")
        remove_id = heading_id if direction == "previous" else adjacent
        blocks = [
            item
            for item in blocks
            if not (isinstance(item, SectionTitleBlock) and item.block_id == remove_id)
        ]
    elif operation == "section_delete":
        end_index = next(
            (index for index, item in headings if index > heading_index),
            len(blocks),
        )
        del blocks[heading_index:end_index]
    else:
        raise ProjectError("section operation is invalid")
    if tuple(blocks) == basis.blocks:
        raise ProjectError("section operation does not change the candidate")
    return ContentDraft(
        content_draft_id=child_id,
        parent_draft_id=snapshot.candidate.content_draft_id,
        base_project_revision=basis.base_project_revision,
        confirmed_by_user=False,
        brief_snapshot=basis.brief_snapshot,
        source_bindings=basis.source_bindings,
        context_hash=basis.context_hash,
        blocks=tuple(blocks),
        display_title=basis.display_title,
        schema_version=2,
    )


def _prepare_section_split(
    snapshot: DraftEditorSnapshot,
    blocks: list[ContentDraftBlock],
    payload: dict[str, object],
    child_id: str,
) -> ContentDraft:
    heading_id = payload["heading_block_id"]
    target = payload["target"]
    title = payload["title"]
    if not isinstance(heading_id, str) or not isinstance(target, dict) or not isinstance(title, str):
        raise ProjectError("section split payload is invalid")
    heading_index = next(
        (index for index, item in enumerate(blocks)
         if isinstance(item, SectionTitleBlock) and item.block_id == heading_id),
        None,
    )
    if heading_index is None:
        raise ProjectError("section split heading does not exist")
    next_heading = next(
        (index for index in range(heading_index + 1, len(blocks))
         if isinstance(blocks[index], SectionTitleBlock)),
        len(blocks),
    )
    paragraph_id = target.get("paragraph_id")
    block_id = target.get("block_id")
    offset = target.get("utf16_offset")
    if (
        not isinstance(paragraph_id, str)
        or not isinstance(block_id, str)
        or isinstance(offset, bool)
        or not isinstance(offset, int)
    ):
        raise ProjectError("section split target is invalid")
    target_is_heading = block_id == heading_id and paragraph_id.startswith(
        "draft_section_paragraph_"
    )
    if not target_is_heading and not any(
        getattr(item, "block_id", None) == block_id
        for item in blocks[heading_index + 1 : next_heading]
    ):
        raise ProjectError("section split target is outside the section")
    caret = resolve_draft_editor_caret(
        snapshot,
        paragraph_id=paragraph_id,
        offset=offset,
        offset_encoding="utf16",
    )
    target_index = heading_index if target_is_heading else next(
        index for index, item in enumerate(blocks)
        if getattr(item, "block_id", None) == block_id
    )
    target_block = blocks[target_index]
    if target_is_heading:
        if caret.character_offset != 0:
            raise ProjectError("section split heading target must be empty")
        split_index = heading_index + 1
    elif isinstance(target_block, NarrationBlock):
        if caret.character_offset not in {0, len(target_block.text)}:
            raise ProjectError("section split cannot cut inside narration")
        split_index = target_index if caret.character_offset == 0 else target_index + 1
    elif isinstance(target_block, SourceExcerptBlock):
        split_index = _split_source_block_at_caret(
            snapshot,
            blocks,
            target_index=target_index,
            target_block=target_block,
            caret=caret,
        )
    else:
        raise ProjectError("section split target block is invalid")
    new_heading_id = f"section_{uuid4().hex}"
    if any(getattr(item, "block_id", None) == new_heading_id for item in blocks):
        raise ProjectError("section split generated a colliding heading ID")
    _break_generated_paragraph_membership(blocks, split_index=split_index)
    blocks[split_index:split_index] = [SectionTitleBlock(new_heading_id, title)]
    _validate_generated_paragraph_membership(blocks)
    return ContentDraft(
        content_draft_id=child_id,
        parent_draft_id=snapshot.candidate.content_draft_id,
        base_project_revision=snapshot.candidate.base_project_revision,
        confirmed_by_user=False,
        brief_snapshot=snapshot.candidate.brief_snapshot,
        source_bindings=snapshot.candidate.source_bindings,
        context_hash=snapshot.candidate.context_hash,
        blocks=tuple(blocks),
        display_title=snapshot.candidate.display_title,
        schema_version=2,
    )


def _break_generated_paragraph_membership(
    blocks: list[ContentDraftBlock],
    *,
    split_index: int,
) -> None:
    """Give the right side of a generated paragraph boundary a fresh token."""

    if split_index <= 0 or split_index >= len(blocks):
        return
    left = blocks[split_index - 1]
    right = blocks[split_index]
    if not isinstance(left, SourceExcerptBlock) or not isinstance(
        right, SourceExcerptBlock
    ):
        return
    shared = _persisted_source_paragraph_key(left.block_id)
    if shared is None or _persisted_source_paragraph_key(right.block_id) != shared:
        return

    shared_indices = [
        index
        for index, block in enumerate(blocks)
        if isinstance(block, SourceExcerptBlock)
        and _persisted_source_paragraph_key(block.block_id) == shared
    ]
    if shared_indices != list(range(shared_indices[0], shared_indices[-1] + 1)):
        raise ProjectError("section split paragraph membership is discontiguous")
    if split_index not in shared_indices or split_index == shared_indices[0]:
        raise ProjectError("section split paragraph membership boundary is invalid")

    existing_tokens = {
        key[1]
        for block in blocks
        if isinstance(block, SourceExcerptBlock)
        if (key := _persisted_source_paragraph_key(block.block_id)) is not None
    }
    new_token = uuid4().hex[:24]
    if new_token in existing_tokens:
        raise ProjectError("section split generated a colliding paragraph token")
    used_block_ids = {getattr(block, "block_id", None) for block in blocks}
    for index in range(split_index, shared_indices[-1] + 1):
        block = blocks[index]
        assert isinstance(block, SourceExcerptBlock)
        new_block_id = f"block_edit_p_{new_token}_r_{uuid4().hex[:16]}"
        if new_block_id in used_block_ids:
            raise ProjectError("section split generated a colliding block ID")
        used_block_ids.add(new_block_id)
        blocks[index] = SourceExcerptBlock(
            block_id=new_block_id,
            refs=block.refs,
            canonical_text=block.canonical_text,
            section_title=block.section_title,
            display_text=block.display_text,
        )


def _validate_generated_paragraph_membership(blocks: list[ContentDraftBlock]) -> None:
    positions: dict[tuple[str, ...], list[int]] = {}
    for index, block in enumerate(blocks):
        if not isinstance(block, SourceExcerptBlock):
            continue
        key = _persisted_source_paragraph_key(block.block_id)
        if key is not None:
            positions.setdefault(key, []).append(index)
    if any(
        indices != list(range(indices[0], indices[-1] + 1))
        for indices in positions.values()
    ):
        raise ProjectError("section split paragraph membership is discontiguous")


def _split_source_block_at_caret(
    snapshot: DraftEditorSnapshot,
    blocks: list[ContentDraftBlock],
    *,
    target_index: int,
    target_block: SourceExcerptBlock,
    caret: DraftEditorCaret,
) -> int:
    """Split a source block only at a core-resolved exact ref boundary."""

    paragraph = next(
        (
            item
            for item in snapshot.paragraphs
            if item.get("paragraph_id") == caret.paragraph_id
        ),
        None,
    )
    if not isinstance(paragraph, dict):
        raise ProjectError("section split target paragraph is stale")
    runs = paragraph.get("source_runs")
    if not isinstance(runs, list):
        raise ProjectError("section split target source runs are invalid")
    character_offset = caret.character_offset
    target_run: dict[str, object] | None = None
    for index, value in enumerate(runs):
        if not isinstance(value, dict) or value.get("block_id") != target_block.block_id:
            continue
        start = value.get("start_offset")
        end = value.get("end_offset")
        if (
            isinstance(start, int)
            and not isinstance(start, bool)
            and isinstance(end, int)
            and not isinstance(end, bool)
            and start <= character_offset <= end
        ):
            next_run = runs[index + 1] if index + 1 < len(runs) else None
            if (
                character_offset == end
                and isinstance(next_run, dict)
                and next_run.get("block_id") == target_block.block_id
                and next_run.get("start_offset") == character_offset
            ):
                continue
            target_run = value
            break
    if target_run is None:
        raise ProjectError("section split target is not an exact source boundary")
    start = target_run.get("start_offset")
    end = target_run.get("end_offset")
    refs = target_run.get("refs")
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or not isinstance(refs, list)
        or len(refs) != 1
        or not isinstance(refs[0], dict)
    ):
        raise ProjectError("section split target source run is invalid")
    target_ref = ContentDraftRef.from_dict(refs[0])
    try:
        ref_index = next(
            index
            for index, ref in enumerate(target_block.refs)
            if ref == target_ref
        )
    except StopIteration as error:
        raise ProjectError("section split target ref is outside the block") from error
    local_offset = character_offset - start
    if local_offset <= 0:
        if ref_index == 0:
            return target_index
        left_refs = target_block.refs[:ref_index]
        right_refs = target_block.refs[ref_index:]
    elif local_offset >= end - start:
        if ref_index == len(target_block.refs) - 1:
            return target_index + 1
        left_refs = target_block.refs[: ref_index + 1]
        right_refs = target_block.refs[ref_index + 1 :]
    else:
        source_caret = caret.source_caret
        if source_caret is None or source_caret.degraded:
            raise ProjectError("section split target is not a trusted fine-unit boundary")
        boundary = _trusted_boundary_pair(
            snapshot,
            source_caret,
            basis=target_ref,
        )
        left_end_ticks = boundary.left_end_ticks
        right_start_ticks = boundary.right_start_ticks
        if (
            left_end_ticks is None
            or right_start_ticks is None
            or left_end_ticks < target_ref.start_ticks
            or right_start_ticks > target_ref.end_ticks
            or left_end_ticks > right_start_ticks
            or left_end_ticks == target_ref.start_ticks
            or right_start_ticks == target_ref.end_ticks
        ):
            raise ProjectError("section split target is not an exact fine-unit boundary")
        left_ref = _content_ref_range(
            snapshot,
            target_ref,
            target_ref.start_ticks,
            left_end_ticks,
        )
        right_ref = _content_ref_range(
            snapshot,
            target_ref,
            right_start_ticks,
            target_ref.end_ticks,
        )
        left_refs = (*target_block.refs[:ref_index], left_ref)
        right_refs = (right_ref, *target_block.refs[ref_index + 1 :])
    left_block = _source_block_with_refs(snapshot, target_block, left_refs)
    paragraph_key = _persisted_source_paragraph_key(target_block.block_id)
    new_block_id = (
        f"block_split_{uuid4().hex}"
        if paragraph_key is None
        else f"block_edit_p_{paragraph_key[1]}_r_{uuid4().hex[:16]}"
    )
    if any(getattr(item, "block_id", None) == new_block_id for item in blocks):
        raise ProjectError("section split generated a colliding block ID")
    right_block = _source_block_with_refs(snapshot, target_block, right_refs, block_id=new_block_id)
    blocks[target_index : target_index + 1] = [left_block, right_block]
    return target_index + 1


def _content_ref_range(
    snapshot: DraftEditorSnapshot,
    basis: ContentDraftRef,
    start_ticks: int,
    end_ticks: int,
) -> ContentDraftRef:
    transcript = snapshot.transcript_base.transcripts[
        (basis.source_id, basis.transcript_version_id)
    ]
    segment = next(
        (item for item in transcript.segments if item.segment_id == basis.segment_id),
        None,
    )
    if segment is None:
        raise ProjectError("section split source segment does not exist")
    spans = exact_fine_unit_spans(segment) or trusted_fine_unit_spans(segment)
    if (
        (start_ticks != segment.start_ticks
         and not any(span.unit.start_ticks == start_ticks for span in spans or ()))
        or (end_ticks != segment.end_ticks
            and not any(span.unit.end_ticks == end_ticks for span in spans or ()))
    ):
        raise ProjectError("section split target is not an exact fine-unit boundary")
    return ContentDraftRef(
        basis.source_id,
        basis.transcript_version_id,
        basis.segment_id,
        start_ticks,
        end_ticks,
    )


def _source_block_with_refs(
    snapshot: DraftEditorSnapshot,
    parent_block: SourceExcerptBlock,
    refs: tuple[ContentDraftRef, ...],
    *,
    block_id: str | None = None,
) -> SourceExcerptBlock:
    if not refs:
        raise ProjectError("section split cannot create an empty source block")
    texts: list[str] = []
    for ref in refs:
        transcript = snapshot.transcript_base.transcripts[
            (ref.source_id, ref.transcript_version_id)
        ]
        segment = next(
            (item for item in transcript.segments if item.segment_id == ref.segment_id),
            None,
        )
        if segment is None:
            raise ProjectError("section split source segment does not exist")
        texts.append(canonical_text_for_range(segment, ref.start_ticks, ref.end_ticks))
    canonical_text = "\n".join(texts)
    selected_display = "\n".join(
        _display_for_ref_in_block(snapshot, parent_block, ref) for ref in refs
    )
    return SourceExcerptBlock(
        block_id or parent_block.block_id,
        refs,
        canonical_text,
        display_text=selected_display,
    )


def _display_for_ref_in_block(
    snapshot: DraftEditorSnapshot,
    parent_block: SourceExcerptBlock,
    ref: ContentDraftRef,
) -> str:
    parent_parts = [
        canonical_text_for_range(
            _workspace_segment(
                snapshot.transcript_base.transcripts[
                    (item.source_id, item.transcript_version_id)
                ],
                item.segment_id,
            ),
            item.start_ticks,
            item.end_ticks,
        )
        for item in parent_block.refs
    ]
    display_parts = split_display_text_for_canonical_parts(
        parent_block.display_text or parent_block.canonical_text,
        parent_parts,
    )
    for index, parent_ref in enumerate(parent_block.refs):
        if (
            parent_ref.source_id != ref.source_id
            or parent_ref.transcript_version_id != ref.transcript_version_id
            or parent_ref.segment_id != ref.segment_id
            or ref.start_ticks < parent_ref.start_ticks
            or ref.end_ticks > parent_ref.end_ticks
        ):
            continue
        if ref == parent_ref:
            return display_parts[index]
        parent_segment = _workspace_segment(
            snapshot.transcript_base.transcripts[
                (parent_ref.source_id, parent_ref.transcript_version_id)
            ],
            parent_ref.segment_id,
        )
        parent_start, parent_end, _, _ = _range_offsets(parent_segment, parent_ref)
        selected_start, selected_end, _, _ = _range_offsets(parent_segment, ref)
        if not parent_start <= selected_start < selected_end <= parent_end:
            raise ProjectError("section split ref cannot map to source display")
        parent_canonical = parent_parts[index]
        relative_start = selected_start - parent_start
        relative_end = selected_end - parent_start
        prefix = parent_canonical[:relative_start]
        selected = parent_canonical[relative_start:relative_end]
        suffix = parent_canonical[relative_end:]
        if not selected:
            raise ProjectError("section split ref cannot map to source display")
        pieces: list[str] = []
        if prefix:
            pieces.append(prefix)
        selected_piece_index = len(pieces)
        pieces.append(selected)
        if suffix:
            pieces.append(suffix)
        display_pieces = split_display_text_for_canonical_parts(
            display_parts[index],
            pieces,
            connection="",
        )
        return display_pieces[selected_piece_index]
    raise ProjectError("section split ref is outside its source block")


def _workspace_segment(transcript: TimedTranscript, segment_id: str) -> TranscriptSegment:
    segment = next(
        (item for item in transcript.segments if item.segment_id == segment_id),
        None,
    )
    if segment is None:
        raise ProjectError("section split source segment does not exist")
    return segment


def _require_section_payload(operation: str, payload: dict[str, object]) -> None:
    fields = {
        "section_reorder": {"heading_block_id", "before_heading_block_id"},
        "section_rename": {"heading_block_id", "title"},
        "section_split": {"heading_block_id", "target", "title"},
        "section_merge": {"heading_block_id", "direction", "adjacent_heading_block_id"},
        "section_delete": {"heading_block_id"},
    }.get(operation)
    if fields is None or set(payload) != fields:
        raise ProjectError("section operation payload fields are invalid")


def select_draft_workspace_candidate(
    project_path: Path,
    *,
    run_id: str,
    operation_id: str,
    expected_checkpoint_ref: DraftWorkspaceCheckpointRef,
    expected_current_candidate_ref: ArtifactRef,
    child_candidate_ref: ArtifactRef,
    audit_review_session_id: str | None = None,
) -> DraftWorkspaceState:
    return _commit_pointer(
        project_path,
        run_id=run_id,
        operation_id=operation_id,
        expected_checkpoint_ref=expected_checkpoint_ref,
        expected_current_candidate_ref=expected_current_candidate_ref,
        operation="candidate_select",
        input_payload={"child_candidate_ref": child_candidate_ref.to_dict()},
        audit_review_session_id=audit_review_session_id,
        transition=lambda store, checkpoint, basis: (
            child_candidate_ref,
            (),
            _require_direct_child(
                store,
                child_candidate_ref,
                parent_ref=checkpoint.current_candidate_ref,
                basis=basis,
            ),
        ),
    )


def undo_draft_workspace(
    project_path: Path,
    *,
    run_id: str,
    operation_id: str,
    expected_checkpoint_ref: DraftWorkspaceCheckpointRef,
    expected_current_candidate_ref: ArtifactRef,
    audit_review_session_id: str | None = None,
) -> DraftWorkspaceState:
    def transition(
        store: DraftWorkspaceStore,
        checkpoint: DraftWorkspaceCheckpoint,
        basis: _Basis,
    ) -> tuple[ArtifactRef, tuple[ArtifactRef, ...], ContentDraft]:
        current = _read_exact_draft(store, checkpoint.current_candidate_ref)
        if current.content_draft_id == basis.anchor_ref.artifact_id:
            raise _error(
                "draft_workspace_transition_not_allowed",
                "refused undo at the workflow Draft anchor",
            )
        if current.parent_draft_id is None:
            raise _error(
                "draft_workspace_integrity_error",
                "found a current candidate without its expected parent",
            )
        parent = store.read_draft_locked(current.parent_draft_id)
        parent_ref = _draft_ref(parent)
        _validate_candidate_basis(parent, basis, allow_confirmed_anchor=True)
        return (
            parent_ref,
            (*checkpoint.redo_candidate_refs, checkpoint.current_candidate_ref),
            parent,
        )

    return _commit_pointer(
        project_path,
        run_id=run_id,
        operation_id=operation_id,
        expected_checkpoint_ref=expected_checkpoint_ref,
        expected_current_candidate_ref=expected_current_candidate_ref,
        operation="undo",
        input_payload={},
        audit_review_session_id=audit_review_session_id,
        transition=transition,
    )


def redo_draft_workspace(
    project_path: Path,
    *,
    run_id: str,
    operation_id: str,
    expected_checkpoint_ref: DraftWorkspaceCheckpointRef,
    expected_current_candidate_ref: ArtifactRef,
    audit_review_session_id: str | None = None,
) -> DraftWorkspaceState:
    def transition(
        store: DraftWorkspaceStore,
        checkpoint: DraftWorkspaceCheckpoint,
        basis: _Basis,
    ) -> tuple[ArtifactRef, tuple[ArtifactRef, ...], ContentDraft]:
        if not checkpoint.redo_candidate_refs:
            raise _error(
                "draft_workspace_transition_not_allowed",
                "refused redo without a validated redo candidate",
            )
        child_ref = checkpoint.redo_candidate_refs[-1]
        child = _require_direct_child(
            store,
            child_ref,
            parent_ref=checkpoint.current_candidate_ref,
            basis=basis,
        )
        return child_ref, checkpoint.redo_candidate_refs[:-1], child

    return _commit_pointer(
        project_path,
        run_id=run_id,
        operation_id=operation_id,
        expected_checkpoint_ref=expected_checkpoint_ref,
        expected_current_candidate_ref=expected_current_candidate_ref,
        operation="redo",
        input_payload={},
        audit_review_session_id=audit_review_session_id,
        transition=transition,
    )


def reset_draft_workspace_after_return(
    project_path: Path,
    *,
    run_id: str,
    operation_id: str,
    expected_checkpoint_ref: DraftWorkspaceCheckpointRef | None,
    expected_current_candidate_ref: ArtifactRef | None,
    return_receipt_ref: ReceiptRef,
    confirmed_anchor_ref: ArtifactRef,
    audit_review_session_id: str | None = None,
) -> DraftWorkspaceState:
    return _reset_workspace(
        project_path,
        run_id=run_id,
        operation_id=operation_id,
        expected_checkpoint_ref=expected_checkpoint_ref,
        expected_current_candidate_ref=expected_current_candidate_ref,
        operation="return_to_draft_reset",
        receipt_ref=return_receipt_ref,
        new_anchor_ref=confirmed_anchor_ref,
        audit_review_session_id=audit_review_session_id,
    )


def reset_draft_workspace_after_anchor_rebase(
    project_path: Path,
    *,
    run_id: str,
    operation_id: str,
    expected_checkpoint_ref: DraftWorkspaceCheckpointRef | None,
    expected_current_candidate_ref: ArtifactRef | None,
    submit_draft_receipt_ref: ReceiptRef,
    new_anchor_ref: ArtifactRef,
    audit_review_session_id: str | None = None,
) -> DraftWorkspaceState:
    return _reset_workspace(
        project_path,
        run_id=run_id,
        operation_id=operation_id,
        expected_checkpoint_ref=expected_checkpoint_ref,
        expected_current_candidate_ref=expected_current_candidate_ref,
        operation="workflow_anchor_reset",
        receipt_ref=submit_draft_receipt_ref,
        new_anchor_ref=new_anchor_ref,
        audit_review_session_id=audit_review_session_id,
    )


def _reset_stale_workspace_for_open(
    store: DraftWorkspaceStore,
    *,
    checkpoint: DraftWorkspaceCheckpoint,
    basis: _Basis,
    audit_review_session_id: str | None,
) -> DraftWorkspaceState:
    receipt_ref = basis.run.last_receipt_ref
    if receipt_ref is None:
        raise _error(
            "draft_workspace_stale",
            "refused to recover a changed workspace without an exact workflow receipt",
        )
    receipt = WorkflowStore(store.project_path).read_receipt(
        receipt_ref.action_id,
        run_id=basis.run.run_id,
    )
    if canonical_sha256_v1(receipt.to_dict()) != receipt_ref.receipt_hash:
        raise _error(
            "draft_workspace_integrity_error",
            "refused to recover a workspace from a changed ActionReceipt",
        )
    operation_id = _new_operation_id(checkpoint.generation)
    expected_ref = DraftWorkspaceCheckpointRef.for_checkpoint(checkpoint)
    if receipt.action == "return_to_draft":
        return reset_draft_workspace_after_return(
            store.project_path,
            run_id=basis.run.run_id,
            operation_id=operation_id,
            expected_checkpoint_ref=expected_ref,
            expected_current_candidate_ref=checkpoint.current_candidate_ref,
            return_receipt_ref=receipt_ref,
            confirmed_anchor_ref=basis.anchor_ref,
            audit_review_session_id=audit_review_session_id,
        )
    if receipt.action == "submit_draft":
        raise _error(
            "draft_workspace_stale",
            "refused to recover a submit_draft rebase that did not exactly "
            "match its checkpoint, receipt, and prepared parent",
        )
    raise _error(
        "draft_workspace_stale",
        "refused to recover a changed workspace from an unrelated workflow receipt",
    )


def _exact_submit_rebase_receipt_for_open(
    store: DraftWorkspaceStore,
    *,
    checkpoint: DraftWorkspaceCheckpoint,
    basis: _Basis,
) -> ReceiptRef | None:
    if (
        checkpoint.project_id != basis.project.project_id
        or checkpoint.workflow_run_id != basis.run.run_id
    ):
        return None
    receipt_ref = basis.run.last_receipt_ref
    if receipt_ref is None:
        return None
    receipt = WorkflowStore(store.project_path).read_receipt(
        receipt_ref.action_id,
        run_id=basis.run.run_id,
    )
    if canonical_sha256_v1(receipt.to_dict()) != receipt_ref.receipt_hash:
        raise _error(
            "draft_workspace_integrity_error",
            "refused to recover a workspace from a changed ActionReceipt",
        )
    if receipt.action != "submit_draft":
        return None
    expected_state = (
        basis.run.stage,
        basis.run.lifecycle,
        basis.project.revision,
    )
    if (
        receipt.before.stage,
        receipt.before.lifecycle,
        receipt.before.project_revision,
    ) != expected_state or (
        receipt.after.stage,
        receipt.after.lifecycle,
        receipt.after.project_revision,
    ) != expected_state:
        return None
    mutation = receipt.mutation
    if mutation is None or not mutation.changed:
        return None
    mutation_ref = ArtifactRef(
        mutation.artifact_id,
        mutation.schema_version,
        mutation.content_hash,
    )
    if mutation.kind != "content_draft" or mutation_ref != basis.anchor_ref:
        return None

    new_anchor = _read_exact_draft(store, basis.anchor_ref)
    if new_anchor.confirmed_by_user or new_anchor.parent_draft_id is None:
        return None
    _validate_candidate_basis(new_anchor, basis)
    prepared_parent = store.read_draft_locked(new_anchor.parent_draft_id)
    if _is_exact_confirmed_return_rebase(
        checkpoint,
        basis=basis,
        prepared_parent=prepared_parent,
    ):
        return receipt_ref
    if _is_exact_stale_rebase_checkpoint(
        store,
        checkpoint=checkpoint,
        basis=basis,
        prepared_parent=prepared_parent,
    ):
        return receipt_ref
    return None


def _exact_submit_child_receipt_for_open(
    store: DraftWorkspaceStore,
    *,
    checkpoint: DraftWorkspaceCheckpoint,
    basis: _Basis,
) -> tuple[ReceiptRef, ArtifactRef] | DraftWorkspaceState | None:
    """Return a same-basis direct child receipt that has not reached the checkpoint."""

    if (
        checkpoint.project_id != basis.project.project_id
        or checkpoint.workflow_run_id != basis.run.run_id
    ):
        return None
    receipt_ref = basis.run.last_receipt_ref
    if receipt_ref is None:
        return None
    receipt = WorkflowStore(store.project_path).read_receipt(
        receipt_ref.action_id,
        run_id=basis.run.run_id,
    )
    if canonical_sha256_v1(receipt.to_dict()) != receipt_ref.receipt_hash:
        raise _error(
            "draft_workspace_integrity_error",
            "refused to reconcile from a changed ActionReceipt",
        )
    if receipt.project_id != basis.project.project_id or receipt.run_id != basis.run.run_id:
        raise _error(
            "draft_workspace_integrity_error",
            "refused to reconcile a receipt from another Project or run",
        )
    if receipt.action != "submit_draft":
        return None
    expected_state = (
        basis.run.stage,
        basis.run.lifecycle,
        basis.project.revision,
    )
    actual_before = (
        receipt.before.stage,
        receipt.before.lifecycle,
        receipt.before.project_revision,
    )
    actual_after = (
        receipt.after.stage,
        receipt.after.lifecycle,
        receipt.after.project_revision,
    )
    if actual_before != expected_state or actual_after != expected_state:
        raise _error(
            "draft_workspace_stale",
            "refused to reconcile a submit receipt from another workflow basis",
        )
    mutation = receipt.mutation
    if mutation is None or mutation.kind != "content_draft" or not mutation.changed:
        raise _error(
            "draft_workspace_integrity_error",
            "refused to reconcile a submit receipt without an exact changed Content Draft",
        )
    child_ref = ArtifactRef(
        mutation.artifact_id,
        mutation.schema_version,
        mutation.content_hash,
    )
    if child_ref == basis.anchor_ref:
        return None
    child = _read_exact_draft(store, child_ref)
    _validate_candidate_basis(child, basis)
    if child_ref == checkpoint.current_candidate_ref:
        current = _validate_checkpoint(store, checkpoint, basis)
        return _state(checkpoint, current, readback=True)
    if child_ref in checkpoint.redo_candidate_refs:
        return None
    if _draft_ancestry_contains(
        store,
        checkpoint.current_candidate_ref,
        ancestor_id=child_ref.artifact_id,
    ):
        return None
    if child.parent_draft_id != checkpoint.current_candidate_ref.artifact_id:
        raise _error(
            "draft_workspace_stale",
            "refused to replace the current candidate with a stale sibling",
        )
    return receipt_ref, child_ref


def _external_submit_operation_id(
    expected_generation: int,
    *,
    receipt_ref: ReceiptRef,
    child_ref: ArtifactRef,
) -> str:
    identity_hash = canonical_sha256_v1(
        {
            "receipt_ref": receipt_ref.to_dict(),
            "child_candidate_ref": child_ref.to_dict(),
        }
    )
    return f"dwop_{expected_generation}_{identity_hash[:32]}"


def _advance_workspace_after_external_submit(
    store: DraftWorkspaceStore,
    *,
    checkpoint: DraftWorkspaceCheckpoint,
    basis: _Basis,
    receipt_ref: ReceiptRef,
    child_ref: ArtifactRef,
    audit_review_session_id: str | None,
) -> DraftWorkspaceState:
    expected_ref = DraftWorkspaceCheckpointRef.for_checkpoint(checkpoint)
    operation_id = _external_submit_operation_id(
        checkpoint.generation,
        receipt_ref=receipt_ref,
        child_ref=child_ref,
    )
    input_hash = draft_workspace_input_hash(
        project_id=basis.project.project_id,
        workflow_run_id=basis.run.run_id,
        operation_id=operation_id,
        operation="external_submit_advance",
        expected_checkpoint_ref=expected_ref,
        expected_current_candidate_ref=checkpoint.current_candidate_ref,
        input_payload={
            "submit_draft_receipt_ref": receipt_ref.to_dict(),
            "child_candidate_ref": child_ref.to_dict(),
        },
    )
    _validate_expected(checkpoint, expected_ref, checkpoint.current_candidate_ref)
    _validate_checkpoint(store, checkpoint, basis)
    child = _require_direct_child(
        store,
        child_ref,
        parent_ref=checkpoint.current_candidate_ref,
        basis=basis,
    )
    after = _checkpoint_after(
        basis,
        generation=checkpoint.generation + 1,
        current_ref=child_ref,
        redo_refs=(),
        operation_id=operation_id,
        expected_generation=checkpoint.generation,
        operation="external_submit_advance",
        input_hash=input_hash,
        audit_review_session_id=audit_review_session_id,
    )
    store.write_locked(after, expected_ref=expected_ref)
    return _state(after, child, readback=False)


def _is_exact_confirmed_return_rebase(
    checkpoint: DraftWorkspaceCheckpoint,
    *,
    basis: _Basis,
    prepared_parent: ContentDraft,
) -> bool:
    return (
        prepared_parent.confirmed_by_user
        and checkpoint.current_candidate_ref == _draft_ref(prepared_parent)
        and not checkpoint.redo_candidate_refs
        and checkpoint.last_commit.operation == "return_to_draft_reset"
        and _confirmed_parent_checkpoint_basis_matches(
            checkpoint,
            basis=basis,
            prepared_parent=prepared_parent,
        )
    )


def _is_exact_stale_rebase_checkpoint(
    store: DraftWorkspaceStore,
    *,
    checkpoint: DraftWorkspaceCheckpoint,
    basis: _Basis,
    prepared_parent: ContentDraft,
) -> bool:
    if (
        prepared_parent.confirmed_by_user
        and checkpoint.current_candidate_ref == _draft_ref(prepared_parent)
        and not checkpoint.redo_candidate_refs
    ):
        return False
    if prepared_parent.confirmed_by_user:
        if not _confirmed_parent_checkpoint_basis_matches(
            checkpoint,
            basis=basis,
            prepared_parent=prepared_parent,
        ):
            return False
    elif not _unconfirmed_parent_checkpoint_basis_matches(
        store,
        checkpoint=checkpoint,
        prepared_parent=prepared_parent,
    ):
        return False
    if not _checkpoint_current_reaches_prepared_parent(
        store,
        checkpoint=checkpoint,
        prepared_parent=prepared_parent,
    ):
        return False
    parent_ref = checkpoint.current_candidate_ref
    for redo_ref in reversed(checkpoint.redo_candidate_refs):
        redo = _read_exact_draft(store, redo_ref)
        if (
            redo.parent_draft_id != parent_ref.artifact_id
            or not _candidate_matches_pre_rebase_parent(redo, prepared_parent)
        ):
            return False
        parent_ref = redo_ref
    return True


def _confirmed_parent_checkpoint_basis_matches(
    checkpoint: DraftWorkspaceCheckpoint,
    *,
    basis: _Basis,
    prepared_parent: ContentDraft,
) -> bool:
    return (
        checkpoint.project_revision == basis.project.revision
        and checkpoint.ordered_bindings == basis.bindings
        and checkpoint.context_hash == basis.context_hash
        and basis.project.active_content_draft_id
        == prepared_parent.content_draft_id
        and prepared_parent.source_bindings == basis.source_bindings
        and prepared_parent.brief_snapshot == basis.brief
    )


def _unconfirmed_parent_checkpoint_basis_matches(
    store: DraftWorkspaceStore,
    *,
    checkpoint: DraftWorkspaceCheckpoint,
    prepared_parent: ContentDraft,
) -> bool:
    checkpoint_source_bindings = tuple(
        SourceTranscriptBinding(
            binding.source_id,
            binding.transcript_version_id,
        )
        for binding in checkpoint.ordered_bindings
    )
    if (
        prepared_parent.base_project_revision != checkpoint.project_revision
        or prepared_parent.source_bindings != checkpoint_source_bindings
        or prepared_parent.context_hash != checkpoint.context_hash
    ):
        return False
    for binding in checkpoint.ordered_bindings:
        try:
            payload = read_json_object(
                store.project_path
                / "transcripts"
                / binding.source_id
                / f"{binding.transcript_version_id}.json",
                description="pre-rebase Draft workspace Timed Transcript",
            )
            transcript = TimedTranscript.from_dict(payload)
        except BaseException as error:
            raise _error(
                "draft_workspace_integrity_error",
                f"could not verify pre-rebase Transcript content "
                f"({type(error).__name__})",
            ) from error
        if (
            transcript.source_id != binding.source_id
            or transcript.transcript_version_id != binding.transcript_version_id
            or subject_content_hash(
                "timed_transcript",
                transcript.schema_version,
                transcript.to_dict(),
            )
            != binding.transcript_content_hash
        ):
            return False
    return True


def _checkpoint_current_reaches_prepared_parent(
    store: DraftWorkspaceStore,
    *,
    checkpoint: DraftWorkspaceCheckpoint,
    prepared_parent: ContentDraft,
) -> bool:
    candidate = _read_exact_draft(store, checkpoint.current_candidate_ref)
    seen: set[str] = set()
    while candidate.content_draft_id != prepared_parent.content_draft_id:
        if (
            candidate.content_draft_id in seen
            or not _candidate_matches_pre_rebase_parent(
                candidate,
                prepared_parent,
            )
            or candidate.parent_draft_id is None
        ):
            return False
        seen.add(candidate.content_draft_id)
        candidate = store.read_draft_locked(candidate.parent_draft_id)
    return candidate == prepared_parent


def _candidate_matches_pre_rebase_parent(
    candidate: ContentDraft,
    prepared_parent: ContentDraft,
) -> bool:
    return (
        not candidate.confirmed_by_user
        and candidate.base_project_revision
        == prepared_parent.base_project_revision
        and candidate.source_bindings == prepared_parent.source_bindings
        and candidate.brief_snapshot == prepared_parent.brief_snapshot
        and candidate.context_hash == prepared_parent.context_hash
        and candidate.display_title == prepared_parent.display_title
    )


def _new_operation_id(expected_generation: int) -> str:
    return f"dwop_{expected_generation}_{uuid4().hex}"


def _commit_child_edit(
    project_path: Path,
    *,
    run_id: str,
    operation_id: str,
    expected_checkpoint_ref: DraftWorkspaceCheckpointRef,
    expected_current_candidate_ref: ArtifactRef,
    operation: str,
    input_payload: dict[str, object],
    audit_review_session_id: str | None,
    prepared_editor_snapshot: DraftEditorSnapshot | None,
    prepare: Callable[
        [DraftEditorSnapshot, str], ContentDraft | DraftEditorPreparedChild
    ],
) -> DraftWorkspaceState:
    store = DraftWorkspaceStore(project_path)
    with store.write_lock():
        basis = _current_basis(store, run_id)
        checkpoint = _require_checkpoint(store, run_id)
        input_hash = draft_workspace_input_hash(
            project_id=basis.project.project_id,
            workflow_run_id=run_id,
            operation_id=operation_id,
            operation=operation,
            expected_checkpoint_ref=expected_checkpoint_ref,
            expected_current_candidate_ref=expected_current_candidate_ref,
            input_payload=input_payload,
        )
        readback = _maybe_readback(
            store,
            checkpoint,
            basis=basis,
            operation_id=operation_id,
            input_hash=input_hash,
        )
        if readback is not None:
            return readback
        _validate_expected(
            checkpoint,
            expected_checkpoint_ref,
            expected_current_candidate_ref,
        )
        current = _validate_checkpoint(store, checkpoint, basis)
        snapshot = (
            _validated_prepared_editor_snapshot(
                prepared_editor_snapshot,
                basis=basis,
                current=current,
            )
            if prepared_editor_snapshot is not None
            else _editor_snapshot(store.project_path, basis, current)
        )
        child_id = f"draft_{uuid4().hex}"
        prepared = prepare(snapshot, child_id)
        child, placement = (
            (prepared.child, prepared.placement)
            if isinstance(prepared, DraftEditorPreparedChild)
            else (prepared, None)
        )
        if placement is not None:
            placement = materialize_draft_editor_placement(snapshot, child, placement)
        child_ref = _draft_ref(child)
        result_selection = (
            prepare_draft_editor_result_selection(
                snapshot, child=child, placement=placement, child_ref=child_ref
            )
            if placement is not None
            else None
        )
        after = _checkpoint_after(
            basis,
            generation=checkpoint.generation + 1,
            current_ref=child_ref,
            redo_refs=(),
            operation_id=operation_id,
            expected_generation=checkpoint.generation,
            operation=operation,
            input_hash=input_hash,
            audit_review_session_id=audit_review_session_id,
        )
        store.publish_child_locked(child, operation_id=operation_id)
        try:
            store.write_locked(after, expected_ref=expected_checkpoint_ref)
        except BaseException:
            store.discard_owned_child_locked(
                child, expected_parent_id=current.content_draft_id
            )
            raise
        return _state(
            after,
            child,
            readback=False,
            placement=placement,
            result_selection=result_selection,
        )


def _commit_pointer(
    project_path: Path,
    *,
    run_id: str,
    operation_id: str,
    expected_checkpoint_ref: DraftWorkspaceCheckpointRef,
    expected_current_candidate_ref: ArtifactRef,
    operation: str,
    input_payload: dict[str, object],
    audit_review_session_id: str | None,
    transition: Callable[
        [DraftWorkspaceStore, DraftWorkspaceCheckpoint, _Basis],
        tuple[ArtifactRef, tuple[ArtifactRef, ...], ContentDraft],
    ],
) -> DraftWorkspaceState:
    store = DraftWorkspaceStore(project_path)
    with store.write_lock():
        basis = _current_basis(store, run_id)
        checkpoint = _require_checkpoint(store, run_id)
        input_hash = draft_workspace_input_hash(
            project_id=basis.project.project_id,
            workflow_run_id=run_id,
            operation_id=operation_id,
            operation=operation,
            expected_checkpoint_ref=expected_checkpoint_ref,
            expected_current_candidate_ref=expected_current_candidate_ref,
            input_payload=input_payload,
        )
        readback = _maybe_readback(
            store,
            checkpoint,
            basis=basis,
            operation_id=operation_id,
            input_hash=input_hash,
        )
        if readback is not None:
            return readback
        _validate_expected(
            checkpoint,
            expected_checkpoint_ref,
            expected_current_candidate_ref,
        )
        _validate_checkpoint(store, checkpoint, basis)
        current_ref, redo_refs, current = transition(store, checkpoint, basis)
        after = _checkpoint_after(
            basis,
            generation=checkpoint.generation + 1,
            current_ref=current_ref,
            redo_refs=redo_refs,
            operation_id=operation_id,
            expected_generation=checkpoint.generation,
            operation=operation,
            input_hash=input_hash,
            audit_review_session_id=audit_review_session_id,
        )
        store.write_locked(after, expected_ref=expected_checkpoint_ref)
        return _state(after, current, readback=False)


def _reset_workspace(
    project_path: Path,
    *,
    run_id: str,
    operation_id: str,
    expected_checkpoint_ref: DraftWorkspaceCheckpointRef | None,
    expected_current_candidate_ref: ArtifactRef | None,
    operation: str,
    receipt_ref: ReceiptRef,
    new_anchor_ref: ArtifactRef,
    audit_review_session_id: str | None,
) -> DraftWorkspaceState:
    store = DraftWorkspaceStore(project_path)
    with store.write_lock():
        basis = _current_basis(store, run_id)
        checkpoint = store.read_locked(run_id)
        input_key = (
            "return_receipt_ref"
            if operation == "return_to_draft_reset"
            else "submit_draft_receipt_ref"
        )
        anchor_key = (
            "confirmed_anchor_ref"
            if operation == "return_to_draft_reset"
            else "new_anchor_ref"
        )
        input_payload: dict[str, object] = {
            input_key: receipt_ref.to_dict(),
            anchor_key: new_anchor_ref.to_dict(),
        }
        input_hash = draft_workspace_input_hash(
            project_id=basis.project.project_id,
            workflow_run_id=run_id,
            operation_id=operation_id,
            operation=operation,
            expected_checkpoint_ref=expected_checkpoint_ref,
            expected_current_candidate_ref=expected_current_candidate_ref,
            input_payload=input_payload,
        )
        if checkpoint is not None:
            readback = _maybe_readback(
                store,
                checkpoint,
                basis=basis,
                operation_id=operation_id,
                input_hash=input_hash,
            )
            if readback is not None:
                return readback
            if (
                expected_checkpoint_ref is None
                or expected_current_candidate_ref is None
            ):
                raise _error(
                    "draft_workspace_stale",
                    "refused reset without the existing checkpoint CAS identity",
                )
            _validate_expected(
                checkpoint,
                expected_checkpoint_ref,
                expected_current_candidate_ref,
            )
        elif expected_checkpoint_ref is not None:
            raise _error(
                "draft_workspace_stale",
                "refused reset because the expected checkpoint is missing",
            )
        _validate_reset_receipt(
            project_path,
            basis,
            receipt_ref=receipt_ref,
            operation=operation,
            new_anchor_ref=new_anchor_ref,
            old_checkpoint=checkpoint,
        )
        current = _read_exact_draft(store, new_anchor_ref)
        generation = 1 if checkpoint is None else checkpoint.generation + 1
        after = _checkpoint_after(
            basis,
            generation=generation,
            current_ref=new_anchor_ref,
            redo_refs=(),
            operation_id=operation_id,
            expected_generation=generation - 1,
            operation=operation,
            input_hash=input_hash,
            audit_review_session_id=audit_review_session_id,
        )
        store.write_locked(after, expected_ref=expected_checkpoint_ref)
        return _state(after, current, readback=False)


def _current_basis(store: DraftWorkspaceStore, run_id: str) -> _Basis:
    workflow_store = WorkflowStore(store.project_path)
    try:
        run = workflow_store.active_run()
        project = ProjectStore(store.project_path).load()
    except WorkflowError:
        raise
    except Exception as error:
        raise _error(
            "draft_workspace_integrity_error",
            f"could not read current Project/run basis ({type(error).__name__})",
        ) from error
    if run is None:
        raise _error(
            "draft_workspace_transition_not_allowed",
            "requires an explicitly created active WorkflowRun",
        )
    if run.run_id != run_id:
        raise _error(
            "draft_workspace_integrity_error",
            "refused a checkpoint request for another WorkflowRun",
        )
    if run.lifecycle != "active" or run.stage != "draft_review":
        raise _error(
            "draft_workspace_transition_not_allowed",
            "requires one active WorkflowRun at draft_review",
        )
    anchor = run.artifact_refs["content_draft"]
    brief_ref = run.artifact_refs["brief"]
    if anchor is None or brief_ref is None:
        raise _error(
            "draft_workspace_integrity_error",
            "requires exact Brief and Content Draft refs on the active run",
        )
    bindings: list[DraftWorkspaceBinding] = []
    source_bindings: list[SourceTranscriptBinding] = []
    for binding in run.ordered_bindings:
        if (
            binding.transcript_version_id is None
            or binding.transcript_content_hash is None
            or project.active_transcript_versions.get(binding.source_id)
            != binding.transcript_version_id
        ):
            raise _error(
                "draft_workspace_stale",
                "found stale or incomplete active Transcript bindings",
            )
        bindings.append(
            DraftWorkspaceBinding(
                binding.source_id,
                binding.transcript_version_id,
                binding.transcript_content_hash,
            )
        )
        source_bindings.append(
            SourceTranscriptBinding(
                binding.source_id, binding.transcript_version_id
            )
        )
        try:
            transcript_payload = read_json_object(
                store.project_path
                / "transcripts"
                / binding.source_id
                / f"{binding.transcript_version_id}.json",
                description="Draft workspace Timed Transcript",
            )
            transcript = TimedTranscript.from_dict(transcript_payload)
        except BaseException as error:
            raise _error(
                "draft_workspace_integrity_error",
                f"could not verify active Transcript content ({type(error).__name__})",
            ) from error
        if (
            transcript.source_id != binding.source_id
            or transcript.transcript_version_id != binding.transcript_version_id
            or subject_content_hash(
                "timed_transcript",
                transcript.schema_version,
                transcript.to_dict(),
            )
            != binding.transcript_content_hash
        ):
            raise _error(
                "draft_workspace_stale",
                "found an active Transcript whose exact content hash changed",
            )
    if not bindings:
        raise _error(
            "draft_workspace_integrity_error",
            "requires non-empty exact Transcript bindings",
        )
    if project.active_brief_id != brief_ref.artifact_id:
        raise _error(
            "draft_workspace_stale",
            "found a different active Brief than the workflow run",
        )
    try:
        brief = read_edit_brief(store.project_path, brief_ref.artifact_id).brief
        if subject_content_hash("brief", brief.schema_version, brief.to_dict()) != (
            brief_ref.content_hash
        ):
            raise ProjectError("Brief hash mismatch")
        context_hash = calculate_agent_context_hash(
            store.project_path,
            project=project,
            bindings=tuple(source_bindings),
            brief=brief,
        )
    except BaseException as error:
        raise _error(
            "draft_workspace_integrity_error",
            f"could not verify Draft workspace context ({type(error).__name__})",
        ) from error
    return _Basis(
        project,
        run,
        tuple(bindings),
        tuple(source_bindings),
        brief,
        context_hash,
        anchor,
    )


def _validate_checkpoint(
    store: DraftWorkspaceStore,
    checkpoint: DraftWorkspaceCheckpoint,
    basis: _Basis,
) -> ContentDraft:
    if (
        checkpoint.project_id != basis.project.project_id
        or checkpoint.workflow_run_id != basis.run.run_id
    ):
        raise _error(
            "draft_workspace_integrity_error",
            "refused a checkpoint from another Project or run",
        )
    if (
        checkpoint.project_revision != basis.project.revision
        or checkpoint.ordered_bindings != basis.bindings
        or checkpoint.context_hash != basis.context_hash
    ):
        raise _error(
            "draft_workspace_stale",
            "refused a checkpoint whose Project revision or basis changed",
        )
    current = _read_exact_draft(store, checkpoint.current_candidate_ref)
    _validate_ancestry(store, current, basis)
    parent_ref = checkpoint.current_candidate_ref
    for redo_ref in reversed(checkpoint.redo_candidate_refs):
        child = _require_direct_child(
            store, redo_ref, parent_ref=parent_ref, basis=basis
        )
        parent_ref = _draft_ref(child)
    return current


def _validate_ancestry(
    store: DraftWorkspaceStore, current: ContentDraft, basis: _Basis
) -> None:
    seen: set[str] = set()
    candidate = current
    while True:
        if candidate.content_draft_id in seen:
            raise _error(
                "draft_workspace_integrity_error",
                "found a cycle in Content Draft ancestry",
            )
        seen.add(candidate.content_draft_id)
        if candidate.content_draft_id == basis.anchor_ref.artifact_id:
            if _draft_ref(candidate) != basis.anchor_ref:
                raise _error(
                    "draft_workspace_integrity_error",
                    "found a workflow anchor whose content hash changed",
                )
            return
        _validate_candidate_basis(candidate, basis)
        if candidate.parent_draft_id is None:
            raise _error(
                "draft_workspace_integrity_error",
                "could not reach the workflow Draft anchor",
            )
        candidate = store.read_draft_locked(candidate.parent_draft_id)


def _validate_candidate_basis(
    candidate: ContentDraft, basis: _Basis, *, allow_confirmed_anchor: bool = False
) -> None:
    if (
        allow_confirmed_anchor
        and candidate.confirmed_by_user
        and candidate.content_draft_id == basis.anchor_ref.artifact_id
    ):
        if _draft_ref(candidate) != basis.anchor_ref:
            raise _error(
                "draft_workspace_integrity_error",
                "found a confirmed anchor whose exact ref changed",
            )
        return
    if candidate.confirmed_by_user:
        raise _error(
            "draft_workspace_integrity_error",
            "refused a confirmed Content Draft as an editable candidate",
        )
    if (
        candidate.base_project_revision != basis.project.revision
        or candidate.brief_snapshot != basis.brief
        or candidate.source_bindings != basis.source_bindings
        or candidate.context_hash != basis.context_hash
    ):
        raise _error(
            "draft_workspace_stale",
            "refused a Content Draft from another revision or basis",
        )


def _require_direct_child(
    store: DraftWorkspaceStore,
    child_ref: ArtifactRef,
    *,
    parent_ref: ArtifactRef,
    basis: _Basis,
) -> ContentDraft:
    child = _read_exact_draft(store, child_ref)
    if child.parent_draft_id != parent_ref.artifact_id:
        raise _error(
            "draft_workspace_integrity_error",
            "refused a redo or selected candidate that is not the direct child",
        )
    _validate_candidate_basis(child, basis)
    return child


def _read_exact_draft(
    store: DraftWorkspaceStore, ref: ArtifactRef
) -> ContentDraft:
    draft = store.read_draft_locked(ref.artifact_id)
    if _draft_ref(draft) != ref:
        raise _error(
            "draft_workspace_integrity_error",
            "refused a Content Draft whose exact ArtifactRef changed",
        )
    return draft


def _draft_ref(draft: ContentDraft) -> ArtifactRef:
    return ArtifactRef(
        draft.content_draft_id,
        draft.schema_version,
        subject_content_hash(
            "content_draft", draft.schema_version, draft.to_dict()
        ),
    )


def _editor_snapshot(
    project_path: Path, basis: _Basis, current: ContentDraft
) -> DraftEditorSnapshot:
    source_bindings = [binding.to_dict() for binding in basis.source_bindings]
    try:
        if current.confirmed_by_user:
            workflow = reopen_workflow_review_content_draft(
                project_path,
                source_bindings=source_bindings,
                content_draft_id=current.content_draft_id,
                playback_selections=None,  # type: ignore[arg-type]
            )
            from roughcut.application.draft_editor import (
                load_draft_editor_snapshot_from_workflow,
            )

            return load_draft_editor_snapshot_from_workflow(
                workflow, content_draft_id=current.content_draft_id
            )
        return load_draft_editor_snapshot(
            project_path,
            source_bindings=source_bindings,
            content_draft_id=current.content_draft_id,
        )
    except BaseException as error:
        raise _error(
            "draft_workspace_stale",
            f"could not prepare the exact Draft Editor snapshot ({type(error).__name__})",
        ) from error


def _validated_prepared_editor_snapshot(
    snapshot: DraftEditorSnapshot,
    *,
    basis: _Basis,
    current: ContentDraft,
) -> DraftEditorSnapshot:
    expected_key = (
        basis.project.revision,
        tuple(
            (binding.source_id, binding.transcript_version_id)
            for binding in basis.source_bindings
        ),
        current.content_draft_id,
    )
    actual_key = (
        snapshot.cache_key.project_revision,
        snapshot.cache_key.source_bindings,
        snapshot.cache_key.candidate_id,
    )
    try:
        validate_draft_editor_snapshot(snapshot)
    except BaseException as error:
        raise _error(
            "draft_workspace_stale",
            f"refused an invalid prepared Draft Editor snapshot ({type(error).__name__})",
        ) from error
    candidate_matches = snapshot.candidate == current or (
        current.confirmed_by_user
        and snapshot.candidate.confirmed_by_user
        and snapshot.candidate.content_draft_id == current.content_draft_id
        and snapshot.candidate.parent_draft_id == current.parent_draft_id
        and snapshot.candidate.blocks == current.blocks
        and snapshot.candidate.source_bindings == current.source_bindings
        and snapshot.candidate.brief_snapshot == current.brief_snapshot
        and snapshot.candidate.display_title == current.display_title
        and snapshot.candidate.schema_version == current.schema_version
    )
    if actual_key != expected_key or not candidate_matches:
        raise _error(
            "draft_workspace_stale",
            "refused a prepared Draft Editor snapshot from another checkpoint basis",
        )
    return snapshot


def _checkpoint_after(
    basis: _Basis,
    *,
    generation: int,
    current_ref: ArtifactRef,
    redo_refs: tuple[ArtifactRef, ...],
    operation_id: str,
    expected_generation: int,
    operation: str,
    input_hash: str,
    audit_review_session_id: str | None,
) -> DraftWorkspaceCheckpoint:
    return DraftWorkspaceCheckpoint(
        schema_version=1,
        project_id=basis.project.project_id,
        workflow_run_id=basis.run.run_id,
        generation=generation,
        current_candidate_ref=current_ref,
        project_revision=basis.project.revision,
        ordered_bindings=basis.bindings,
        context_hash=basis.context_hash,
        redo_candidate_refs=redo_refs,
        last_commit=DraftWorkspaceLastCommit(
            operation_id,
            expected_generation,
            operation,
            input_hash,
            current_ref,
        ),
        audit_review_session_id=audit_review_session_id,
        updated_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    )


def _validate_expected(
    checkpoint: DraftWorkspaceCheckpoint,
    expected_ref: DraftWorkspaceCheckpointRef,
    expected_current_ref: ArtifactRef,
) -> None:
    if (
        DraftWorkspaceCheckpointRef.for_checkpoint(checkpoint) != expected_ref
        or checkpoint.current_candidate_ref != expected_current_ref
    ):
        raise _error(
            "draft_workspace_stale",
            "refused to overwrite a changed checkpoint or current candidate",
        )


def _require_checkpoint(
    store: DraftWorkspaceStore, run_id: str
) -> DraftWorkspaceCheckpoint:
    checkpoint = store.read_locked(run_id)
    if checkpoint is None:
        raise _error(
            "draft_workspace_stale",
            "requires an explicitly initialized checkpoint",
        )
    return checkpoint


def _maybe_readback(
    store: DraftWorkspaceStore,
    checkpoint: DraftWorkspaceCheckpoint,
    *,
    basis: _Basis,
    operation_id: str,
    input_hash: str,
) -> DraftWorkspaceState | None:
    if checkpoint.last_commit.operation_id != operation_id:
        return None
    if checkpoint.last_commit.input_hash != input_hash:
        raise _error(
            "draft_workspace_action_conflict",
            "found the same operation ID with different canonical input",
        )
    current = _validate_checkpoint(store, checkpoint, basis)
    return _state(checkpoint, current, readback=True)


def _readback_or_conflict(
    store: DraftWorkspaceStore,
    checkpoint: DraftWorkspaceCheckpoint,
    *,
    basis: _Basis,
    operation_id: str,
    input_hash: str,
) -> DraftWorkspaceState:
    readback = _maybe_readback(
        store,
        checkpoint,
        basis=basis,
        operation_id=operation_id,
        input_hash=input_hash,
    )
    if readback is not None:
        return readback
    raise _error(
        "draft_workspace_stale",
        "refused initialization because a checkpoint already exists",
    )


def _state(
    checkpoint: DraftWorkspaceCheckpoint,
    current: ContentDraft,
    *,
    readback: bool,
    placement: DraftEditorPlacementResult | None = None,
    result_selection: dict[str, object] | None = None,
) -> DraftWorkspaceState:
    return DraftWorkspaceState(
        checkpoint,
        DraftWorkspaceCheckpointRef.for_checkpoint(checkpoint),
        current,
        readback,
        placement,
        result_selection,
    )


def _validate_reset_receipt(
    project_path: Path,
    basis: _Basis,
    *,
    receipt_ref: ReceiptRef,
    operation: str,
    new_anchor_ref: ArtifactRef,
    old_checkpoint: DraftWorkspaceCheckpoint | None,
) -> None:
    if basis.run.last_receipt_ref != receipt_ref or basis.anchor_ref != new_anchor_ref:
        raise _error(
            "draft_workspace_stale",
            "refused reset because the run receipt or Draft anchor changed",
        )
    receipt = WorkflowStore(project_path).read_receipt(
        receipt_ref.action_id, run_id=basis.run.run_id
    )
    if canonical_sha256_v1(receipt.to_dict()) != receipt_ref.receipt_hash:
        raise _error(
            "draft_workspace_integrity_error",
            "refused reset because the ActionReceipt hash changed",
        )
    expected_action = (
        "return_to_draft"
        if operation == "return_to_draft_reset"
        else "submit_draft"
    )
    if receipt.action != expected_action:
        raise _error(
            "draft_workspace_transition_not_allowed",
            "refused a checkpoint reset for the wrong workflow action",
        )
    if operation == "workflow_anchor_reset":
        mutation = receipt.mutation
        if (
            mutation is None
            or mutation.kind != "content_draft"
            or mutation.artifact_id != new_anchor_ref.artifact_id
            or mutation.schema_version != new_anchor_ref.schema_version
            or mutation.content_hash != new_anchor_ref.content_hash
        ):
            raise _error(
                "draft_workspace_integrity_error",
                "refused a rebase reset whose receipt mutation does not match",
            )
    elif old_checkpoint is not None:
        confirmed = DraftWorkspaceStore(project_path).read_draft_locked(
            new_anchor_ref.artifact_id
        )
        if (
            not confirmed.confirmed_by_user
            or (
                confirmed.parent_draft_id
                != old_checkpoint.current_candidate_ref.artifact_id
                and old_checkpoint.current_candidate_ref != new_anchor_ref
            )
        ):
            raise _error(
                "draft_workspace_integrity_error",
                "refused a return reset whose confirmed ancestry does not match",
            )
    if operation == "workflow_anchor_reset" and old_checkpoint is not None:
        new_anchor = DraftWorkspaceStore(project_path).read_draft_locked(
            new_anchor_ref.artifact_id
        )
        if new_anchor.parent_draft_id is None or not _draft_ancestry_contains(
            DraftWorkspaceStore(project_path),
            old_checkpoint.current_candidate_ref,
            ancestor_id=new_anchor.parent_draft_id,
        ):
            raise _error(
                "draft_workspace_integrity_error",
                "refused a rebase reset whose old checkpoint ancestry does not "
                "contain the prepared parent",
            )


def _draft_ancestry_contains(
    store: DraftWorkspaceStore,
    current_ref: ArtifactRef,
    *,
    ancestor_id: str,
) -> bool:
    candidate = _read_exact_draft(store, current_ref)
    seen: set[str] = set()
    while candidate.content_draft_id not in seen:
        seen.add(candidate.content_draft_id)
        if candidate.content_draft_id == ancestor_id:
            return True
        if candidate.parent_draft_id is None:
            return False
        candidate = store.read_draft_locked(candidate.parent_draft_id)
    raise _error(
        "draft_workspace_integrity_error",
        "found a cycle while validating the pre-rebase Draft ancestry",
    )
