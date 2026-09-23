from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

import roughcut.adapters.draft_workspace_store as store_module
from roughcut.adapters.draft_workspace_store import DraftWorkspaceStore
from roughcut.application.projects import create_project
from roughcut.domain.draft_workspace import (
    DraftWorkspaceBinding,
    DraftWorkspaceCheckpoint,
    DraftWorkspaceCheckpointRef,
    DraftWorkspaceLastCommit,
)
from roughcut.domain.errors import WorkflowError
from roughcut.domain.workflow import ArtifactRef


def _checkpoint(
    project_id: str,
    *,
    generation: int = 1,
    operation_id: str = "dwop_0_00000000000000000000000000000001",
    operation: str = "initialize",
    current_id: str = "draft_a",
) -> DraftWorkspaceCheckpoint:
    current = ArtifactRef(current_id, 1, "a" * 64)
    return DraftWorkspaceCheckpoint(
        schema_version=1,
        project_id=project_id,
        workflow_run_id="wfr_test",
        generation=generation,
        current_candidate_ref=current,
        project_revision=0,
        ordered_bindings=(DraftWorkspaceBinding("src_a", "tr_a", "b" * 64),),
        context_hash="c" * 64,
        redo_candidate_refs=(),
        last_commit=DraftWorkspaceLastCommit(
            operation_id,
            generation - 1,
            operation,
            "d" * 64,
            current,
        ),
        audit_review_session_id=None,
        updated_at="2026-07-29T00:00:00.000000Z",
    )


def test_missing_checkpoint_read_does_not_create_workflow_tree(tmp_path: Path) -> None:
    root = tmp_path / "project"
    create_project(root, "Project")

    assert DraftWorkspaceStore(root).read("wfr_test") is None
    assert not (root / "workflow").exists()


def test_missing_checkpoint_does_not_hide_unsafe_workflow_parent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    create_project(root, "Project")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "workflow").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorkflowError) as raised:
        DraftWorkspaceStore(root).read("wfr_test")
    assert raised.value.code == "draft_workspace_integrity_error"
    assert (root / "workflow").is_symlink()


def test_checkpoint_atomic_roundtrip_and_two_store_cas(tmp_path: Path) -> None:
    root = tmp_path / "project"
    project = create_project(root, "Project")
    first_store = DraftWorkspaceStore(root)
    second_store = DraftWorkspaceStore(root)
    first = _checkpoint(project.project_id)
    first_store.write(first, expected_ref=None)
    expected = DraftWorkspaceCheckpointRef.for_checkpoint(first)
    winner = replace(
        first,
        generation=2,
        last_commit=DraftWorkspaceLastCommit(
            "dwop_1_00000000000000000000000000000002",
            1,
            "undo",
            "e" * 64,
            first.current_candidate_ref,
        ),
        updated_at="2026-07-29T00:00:01.000000Z",
    )
    loser = replace(
        winner,
        last_commit=DraftWorkspaceLastCommit(
            "dwop_1_00000000000000000000000000000003",
            1,
            "redo",
            "f" * 64,
            first.current_candidate_ref,
        ),
    )

    assert first_store.write(winner, expected_ref=expected) == winner
    with pytest.raises(WorkflowError) as raised:
        second_store.write(loser, expected_ref=expected)
    assert raised.value.code == "draft_workspace_stale"
    assert second_store.read("wfr_test") == winner


def test_checkpoint_replace_failure_preserves_previous_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    project = create_project(root, "Project")
    store = DraftWorkspaceStore(root)
    first = _checkpoint(project.project_id)
    store.write(first, expected_ref=None)
    updated = replace(
        first,
        generation=2,
        last_commit=DraftWorkspaceLastCommit(
            "dwop_1_00000000000000000000000000000002",
            1,
            "undo",
            "e" * 64,
            first.current_candidate_ref,
        ),
        updated_at="2026-07-29T00:00:01.000000Z",
    )

    def fail_replace(source: Path, target: Path) -> None:
        raise OSError("fixture replace failure")

    monkeypatch.setattr(store_module.os, "replace", fail_replace)
    with pytest.raises(WorkflowError) as raised:
        store.write(
            updated,
            expected_ref=DraftWorkspaceCheckpointRef.for_checkpoint(first),
        )

    assert raised.value.code == "draft_workspace_write_failed"
    assert store.read("wfr_test") == first
    assert not (root / "workflow" / "draft-workspaces" / ".wfr_test.json.tmp").exists()


@pytest.mark.parametrize("unsafe_kind", ["symlink", "hardlink"])
def test_checkpoint_unsafe_file_fails_closed(
    tmp_path: Path, unsafe_kind: str
) -> None:
    root = tmp_path / "project"
    project = create_project(root, "Project")
    checkpoint = _checkpoint(project.project_id)
    workspace = root / "workflow" / "draft-workspaces"
    workspace.mkdir(parents=True)
    target = workspace / "wfr_test.json"
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps(checkpoint.to_dict()), encoding="utf-8")
    if unsafe_kind == "symlink":
        target.symlink_to(evidence)
    else:
        os.link(evidence, target)

    with pytest.raises(WorkflowError) as raised:
        DraftWorkspaceStore(root).read("wfr_test")
    assert raised.value.code == "draft_workspace_integrity_error"
    assert target.exists()


def test_owned_checkpoint_temp_is_cleaned_but_unknown_node_is_preserved(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    create_project(root, "Project")
    workspace = root / "workflow" / "draft-workspaces"
    workspace.mkdir(parents=True)
    temp = workspace / ".wfr_test.json.tmp"
    temp.write_text("partial", encoding="utf-8")

    assert DraftWorkspaceStore(root).read("wfr_test") is None
    assert not temp.exists()

    unknown = workspace / "user.txt"
    unknown.write_text("evidence", encoding="utf-8")
    with pytest.raises(WorkflowError):
        DraftWorkspaceStore(root).read("wfr_test")
    assert unknown.read_text(encoding="utf-8") == "evidence"
