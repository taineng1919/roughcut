from __future__ import annotations

import hashlib
import inspect
import json
import multiprocessing
import os
import shutil
import stat
import sys
import tempfile
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from queue import Empty
from typing import Any

import pytest

from roughcut.adapters import project_lock, workflow_store
from roughcut.adapters.project_lock import LOCK_FILENAME, project_write_lock
from roughcut.adapters.project_store import ProjectStore
from roughcut.adapters.workflow_store import WorkflowStore
from roughcut.application.projects import create_project
from roughcut.domain.brief import EditBrief
from roughcut.domain.errors import ProjectError, WorkflowError
from roughcut.domain.workflow import (
    ActionReceipt,
    ApprovalRecord,
    ApprovalRef,
    ArtifactRef,
    MutationRef,
    OutputRef,
    ReceiptRef,
    ReceiptState,
    ScopeAuthorization,
    SubjectRef,
    TransactionMarker,
    WorkflowBinding,
    WorkflowRun,
    canonical_json_v1,
    canonical_sha256_v1,
    subject_content_hash,
    workflow_action_input_hash,
)

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
NOW = "2026-07-27T12:34:56.000001Z"


def _brief_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "brief_id": "brief_recovery",
        "theme": "Recovery",
        "target_duration_ticks": 120000,
        "focus": ["recovery"],
        "allow_reorder": False,
    }


def _brief_content_hash(payload: dict[str, object] | None = None) -> str:
    parsed = EditBrief.from_dict(payload or _brief_payload())
    return subject_content_hash("brief", parsed.schema_version, parsed.to_dict())


def _manifest_payload(project_id: str) -> dict[str, object]:
    return {
        "schema_version": 2,
        "render_id": "render_test",
        "project_id": project_id,
        "project_revision": 0,
        "edit_version_id": "edit_test",
        "tools": {
            "ffmpeg_version": "fixture",
            "ffprobe_version": "fixture",
        },
        "output_settings": {
            "width": 1920,
            "height": 1080,
            "frame_rate": {"numerator": 25, "denominator": 1},
            "audio_sample_rate": 48000,
        },
        "clips": [
            {
                "clip_id": "clip_test",
                "source_id": "src_export",
                "source_in_ticks": 0,
                "source_out_ticks": 120000,
            }
        ],
        "total_duration_ticks": 120000,
        "render_schedule": {"strategy": "fixture", "clips": []},
        "command_summary": {"video_encoder": "libx264"},
        "performance": {"wall_seconds": 1.0},
        "output": {
            "mp4_path": "renders/render_test.mp4",
            "probe": {"duration_ticks": 120000},
        },
        "acceptance": {"accepted": True, "checks": {"duration": True}},
        "input_source": {
            "source_id": "src_export",
            "fingerprint": {
                "size": 1,
                "mtime_ns": 1,
                "sha256_head_tail": HASH_A,
            },
            "probe": {"duration_ticks": 120000},
        },
    }


def _manifest_content_hash(payload: dict[str, object]) -> str:
    output = payload["output"]
    assert isinstance(output, dict)
    projection = {
        "render_id": payload["render_id"],
        "edit_version_id": payload["edit_version_id"],
        "input_source": payload["input_source"],
        "clips": payload["clips"],
        "output_settings": payload["output_settings"],
        "output": {"mp4_path": output["mp4_path"]},
        "acceptance": payload["acceptance"],
    }
    return subject_content_hash(
        "render_manifest",
        int(payload["schema_version"]),
        projection,
    )


def _run(
    project_id: str,
    *,
    run_id: str = "wfr_test",
    stage: str = "scope_review",
    lifecycle: str = "active",
) -> WorkflowRun:
    return WorkflowRun.from_dict(
        {
            "schema_version": 1,
            "run_id": run_id,
            "project_id": project_id,
            "stage": stage,
            "lifecycle": lifecycle,
            "created_at": NOW,
            "updated_at": NOW,
            "ordered_bindings": [],
            "scope_authorizations": [],
            "artifact_refs": {
                "brief": None,
                "outline": None,
                "content_draft": None,
                "proposal": None,
                "decision": None,
                "render": None,
            },
            "readiness_basis": {
                "scope_subject_hash": None,
                "brief_subject_hash": None,
                "required_transcripts": [],
                "speaker_resolution": {
                    "mode": "not_ready",
                    "refs": [],
                    "waiver_subject_hash": None,
                },
                "blocking_operation_ids": [],
            },
            "approval_refs": {
                "scope": None,
                "brief": None,
                "outline": None,
                "draft": None,
                "roughcut": None,
                "export": None,
            },
            "last_receipt_ref": None,
        }
    )


def _approval(project_id: str, *, run_id: str = "wfr_test") -> ApprovalRecord:
    return ApprovalRecord(
        schema_version=1,
        approval_id="appr_test",
        run_id=run_id,
        project_id=project_id,
        gate="scope",
        subject=SubjectRef("scope_snapshot", "scope_test", 1, HASH_A),
        dependency_hash=HASH_B,
        issued_project_revision=0,
        issued_by_action_id="act_test",
        source_channel="agent_conversation",
        source_action="approve_scope",
        actor_assurance="unverified_host_user_action",
        issued_at=NOW,
    )


def _receipt(
    project_id: str,
    *,
    run_id: str = "wfr_test",
    input_hash: str = HASH_A,
) -> ActionReceipt:
    state = ReceiptState("scope_review", "active", 0)
    return ActionReceipt(
        schema_version=1,
        action_id="act_test",
        input_hash=input_hash,
        run_id=run_id,
        project_id=project_id,
        action="approve_scope",
        before=state,
        after=state,
        approval_ids=("appr_test",),
        mutation=None,
        output_refs=(),
        created_at=NOW,
    )


@dataclass(frozen=True)
class _RecoveryScenario:
    project_before: Any
    project_after: Any
    run_before: WorkflowRun
    run_after: WorkflowRun
    approval: ApprovalRecord
    receipt: ActionReceipt
    marker: TransactionMarker


def _recovery_scenario(root: Path) -> _RecoveryScenario:
    project_before = create_project(root, "Recovery")
    store = WorkflowStore(root)
    run_before = store.write_run(_run(project_before.project_id))
    brief_hash = _brief_content_hash()
    approval = ApprovalRecord(
        schema_version=1,
        approval_id="appr_recovery",
        run_id=run_before.run_id,
        project_id=project_before.project_id,
        gate="brief",
        subject=SubjectRef("brief", "brief_recovery", 1, brief_hash),
        dependency_hash=HASH_B,
        issued_project_revision=project_before.revision,
        issued_by_action_id="act_recovery",
        source_channel="agent_conversation",
        source_action="confirm_brief",
        actor_assurance="unverified_host_user_action",
        issued_at=NOW,
    )
    approval_ref = ApprovalRef(
        approval_id=approval.approval_id,
        record_schema_version=1,
        record_hash=canonical_sha256_v1(approval.to_dict()),
    )
    project_after = replace(
        project_before,
        revision=project_before.revision + 1,
        active_brief_id="brief_recovery",
    )
    receipt = ActionReceipt(
        schema_version=1,
        action_id="act_recovery",
        input_hash=HASH_C,
        run_id=run_before.run_id,
        project_id=project_before.project_id,
        action="confirm_brief",
        before=ReceiptState("scope_review", "active", project_before.revision),
        after=ReceiptState("scope_review", "active", project_after.revision),
        approval_ids=(approval.approval_id,),
        mutation=MutationRef("brief", "brief_recovery", 1, brief_hash, True),
        output_refs=(),
        created_at=NOW,
    )
    receipt_ref = ReceiptRef(
        action_id=receipt.action_id,
        receipt_schema_version=1,
        receipt_hash=canonical_sha256_v1(receipt.to_dict()),
    )
    run_after = replace(
        run_before,
        artifact_refs={
            **run_before.artifact_refs,
            "brief": ArtifactRef("brief_recovery", 1, brief_hash),
        },
        approval_refs={**run_before.approval_refs, "brief": approval_ref},
        last_receipt_ref=receipt_ref,
    )
    marker = TransactionMarker(
        schema_version=1,
        action_id=receipt.action_id,
        input_hash=receipt.input_hash,
        run_id=run_before.run_id,
        project_id=project_before.project_id,
        action=receipt.action,
        project_before_hash=canonical_sha256_v1(project_before.to_dict()),
        project_after_hash=canonical_sha256_v1(project_after.to_dict()),
        run_before_hash=canonical_sha256_v1(run_before.to_dict()),
        run_after_hash=canonical_sha256_v1(run_after.to_dict()),
        project_before=project_before,
        run_before=run_before,
        candidate_refs=(
            OutputRef("brief", "brief_recovery", 1, brief_hash, None),
        ),
        approval_ids=(approval.approval_id,),
        commit_step="prepared",
    )
    return _RecoveryScenario(
        project_before=project_before,
        project_after=project_after,
        run_before=run_before,
        run_after=run_after,
        approval=approval,
        receipt=receipt,
        marker=marker,
    )


def _publish_recovery_participants(
    root: Path,
    scenario: _RecoveryScenario,
    stop_after: str,
) -> None:
    store = WorkflowStore(root)
    store.write_transaction(scenario.marker)
    if stop_after == "marker":
        return
    briefs = root / "briefs"
    briefs.mkdir(exist_ok=True)
    (briefs / "brief_recovery.json").write_text(
        json.dumps(_brief_payload()),
        encoding="utf-8",
    )
    store.write_approval(scenario.approval)
    store.write_transaction(
        replace(scenario.marker, commit_step="candidates_published")
    )
    if stop_after == "candidates":
        return
    ProjectStore(root).save(
        scenario.project_after,
        expected_revision=scenario.project_before.revision,
    )
    store.write_transaction(
        replace(scenario.marker, commit_step="project_published")
    )
    if stop_after == "project":
        return
    store.write_run(
        scenario.run_after,
        expected_run_hash=scenario.marker.run_before_hash,
    )
    store.write_transaction(
        replace(scenario.marker, commit_step="run_published")
    )
    if stop_after == "run":
        return
    store.write_receipt(scenario.receipt)


def _hard_exit_recovery_participants(
    root_value: str,
    scenario: _RecoveryScenario,
    stop_after: str,
) -> None:
    _publish_recovery_participants(Path(root_value), scenario, stop_after)
    os._exit(91)


def _hard_exit_during_recovery(root_value: str) -> None:
    store = WorkflowStore(Path(root_value))
    atomic_write = store._atomic_write

    def write_then_exit(
        path: Path,
        payload: dict[str, object],
        *,
        immutable: bool,
    ) -> None:
        atomic_write(path, payload, immutable=immutable)
        if path.parent.name == "runs":
            os._exit(92)

    store._atomic_write = write_then_exit  # type: ignore[method-assign]
    store.recover_pending()


def _export_candidate_scenario(
    root: Path,
    candidate_kind: str,
) -> tuple[TransactionMarker, Path, bytes]:
    project = create_project(root, "Export recovery")
    store = WorkflowStore(root)
    initial_run = store.write_run(_run(project.project_id))
    run = store.write_run(
        replace(
            initial_run,
            stage="export_review",
            ordered_bindings=(WorkflowBinding("src_export", None, None),),
        ),
        expected_run_hash=canonical_sha256_v1(initial_run.to_dict()),
    )
    if candidate_kind == "mp4":
        payload = b"candidate-mp4"
        content_hash = hashlib.sha256(payload).hexdigest()
        schema_version = 1
        relative_path = "renders/render_test.mp4"
    elif candidate_kind == "manifest":
        manifest = _manifest_payload(project.project_id)
        payload = json.dumps(manifest).encode("utf-8")
        content_hash = _manifest_content_hash(manifest)
        schema_version = 2
        relative_path = "renders/render_test.manifest.json"
    else:
        raise AssertionError(f"unsupported fixture candidate: {candidate_kind}")
    candidate_ref = OutputRef(
        candidate_kind,
        "render_test",
        schema_version,
        content_hash,
        relative_path,
    )
    marker = TransactionMarker(
        schema_version=1,
        action_id=f"act_{candidate_kind}",
        input_hash=HASH_C,
        run_id=run.run_id,
        project_id=project.project_id,
        action="approve_export",
        project_before_hash=canonical_sha256_v1(project.to_dict()),
        project_after_hash=canonical_sha256_v1(project.to_dict()),
        run_before_hash=canonical_sha256_v1(run.to_dict()),
        run_after_hash=canonical_sha256_v1(run.to_dict()),
        project_before=project,
        run_before=run,
        candidate_refs=(candidate_ref,),
        approval_ids=(f"appr_{candidate_kind}",),
        commit_step="prepared",
    )
    store.write_transaction(marker)
    path = root.joinpath(*relative_path.split("/"))
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(payload)
    marker = replace(marker, commit_step="candidates_published")
    store.write_transaction(marker)
    return marker, path, payload


def _attach_scope_approval(
    store: WorkflowStore, run: WorkflowRun, approval: ApprovalRecord
) -> WorkflowRun:
    store.write_approval(approval)
    approval_ref = ApprovalRef(
        approval_id=approval.approval_id,
        record_schema_version=1,
        record_hash=canonical_sha256_v1(approval.to_dict()),
    )
    bindings = (
        run.ordered_bindings
        if run.ordered_bindings
        else (WorkflowBinding("src_scope", None, None),)
    )
    updated = replace(
        run,
        ordered_bindings=bindings,
        scope_authorizations=tuple(
            ScopeAuthorization(binding.source_id, True, True)
            for binding in bindings
        ),
        approval_refs={**run.approval_refs, "scope": approval_ref},
    )
    return store.write_run(
        updated,
        expected_run_hash=canonical_sha256_v1(run.to_dict()),
    )


def _concurrent_project_save(
    project_path: str,
    expected_revision: int,
    ready: Any,
    start: Any,
    results: Any,
) -> None:
    store = ProjectStore(Path(project_path))
    project = store.load()
    ready.put("ready")
    start.wait()
    try:
        store.save(
            replace(project, revision=expected_revision + 1),
            expected_revision=expected_revision,
        )
    except (OSError, ProjectError) as error:
        results.put((type(error).__name__, str(error)))
    else:
        results.put(("ok", "saved"))


def _workflow_locked_project_save(
    project_path: str,
    entered: Any,
    release: Any,
    results: Any,
) -> None:
    root = Path(project_path)
    try:
        with WorkflowStore(root).write_lock():
            project = ProjectStore(root).load()
            ProjectStore(root).save(
                replace(project, revision=project.revision + 1),
                expected_revision=project.revision,
            )
            entered.set()
            release.wait(timeout=15)
    except (OSError, ProjectError, WorkflowError) as error:
        results.put((type(error).__name__, str(error)))
    else:
        results.put(("ok", "workflow writer saved"))


def test_legacy_project_read_does_not_create_workflow_or_change_project(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    create_project(root, "Legacy")
    manifest = root / "project.json"
    before = manifest.read_bytes()

    assert WorkflowStore(root).list_runs() == ()
    assert WorkflowStore(root).active_run() is None
    assert not (root / "workflow").exists()
    assert manifest.read_bytes() == before


def test_single_active_run_and_terminal_history_is_read_only(tmp_path: Path) -> None:
    root = tmp_path / "project"
    project = create_project(root, "Project")
    store = WorkflowStore(root)
    historical = store.write_run(_run(project.project_id, run_id="wfr_completed"))
    historical_approval = _approval(project.project_id, run_id="wfr_completed")
    historical = _attach_scope_approval(
        store,
        historical,
        historical_approval,
    )
    completed = replace(
        historical,
        stage="exporting",
        lifecycle="completed",
        ordered_bindings=(WorkflowBinding("src_history", None, None),),
        scope_authorizations=(ScopeAuthorization("src_history", True, False),),
    )
    store.write_run(
        completed,
        expected_run_hash=canonical_sha256_v1(historical.to_dict()),
    )
    canceling = store.write_run(_run(project.project_id, run_id="wfr_canceled"))
    canceled = replace(canceling, lifecycle="canceled")
    store.write_run(
        canceled,
        expected_run_hash=canonical_sha256_v1(canceling.to_dict()),
    )
    active = store.write_run(_run(project.project_id))

    assert store.active_run() == active
    with pytest.raises(WorkflowError, match="only one active"):
        store.write_run(_run(project.project_id, run_id="wfr_other"))
    with pytest.raises(WorkflowError, match="immutable"):
        store.write_run(
            replace(completed, lifecycle="canceled")
        )
    with pytest.raises(WorkflowError, match="immutable"):
        store.write_run(_run(project.project_id, run_id="wfr_canceled"))


def test_new_run_cannot_be_created_directly_in_a_terminal_state(tmp_path: Path) -> None:
    root = tmp_path / "project"
    project = create_project(root, "Project")
    store = WorkflowStore(root)

    with pytest.raises(WorkflowError, match="must start active"):
        store.write_run(
            _run(project.project_id, run_id="wfr_terminal", lifecycle="canceled")
        )


def test_run_atomic_replace_uses_expected_hash_and_preserves_previous_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    project = create_project(root, "Project")
    store = WorkflowStore(root)
    initial = store.write_run(_run(project.project_id))
    initial_hash = canonical_sha256_v1(initial.to_dict())
    updated = replace(initial, updated_at="2026-07-27T12:34:57.000001Z")

    with pytest.raises(WorkflowError, match="current WorkflowRun hash"):
        store.write_run(updated)
    assert store.write_run(updated, expected_run_hash=initial_hash) == updated
    with pytest.raises(WorkflowError) as captured:
        store.write_run(initial, expected_run_hash=HASH_A)
    assert captured.value.code == "workflow_action_conflict"

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(workflow_store.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        store.write_run(initial, expected_run_hash=canonical_sha256_v1(updated.to_dict()))
    assert store.read_run("wfr_test") == updated
    assert not list((root / "workflow" / "runs").glob(".*.tmp"))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows runtime behavior only")
def test_windows_runtime_workflow_store_and_closed_named_temp_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "中文 workflow project with spaces"
    project = create_project(root, "Project")
    store = WorkflowStore(root)
    run = store.write_run(_run(project.project_id))
    assert store.read_run(run.run_id) == run
    assert root.resolve().drive
    assert not list((root / "workflow" / "runs").glob(".*.tmp"))

    contained = store.project_path / "workflow" / "runs" / "contained.json"
    store._validate_contained(contained)
    escaped = store.project_path.parent / f"{store.project_path.name}-escape"
    with pytest.raises(WorkflowError, match="escapes"):
        store._validate_contained(escaped)

    destination = root / "closed destination.json"
    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=root,
        prefix=".closed-source.",
        suffix=".tmp",
        delete=False,
    ) as temporary_file:
        temporary = Path(temporary_file.name)
        temporary_file.write(b"closed handle bytes")
        temporary_file.flush()
        os.fsync(temporary_file.fileno())
    os.replace(temporary, destination)

    assert destination.read_bytes() == b"closed handle bytes"
    assert not temporary.exists()
    assert not list(root.glob(".closed-source.*.tmp"))

    updated = replace(run, updated_at="2026-07-27T12:34:57.000001Z")
    store.write_run(
        updated,
        expected_run_hash=canonical_sha256_v1(run.to_dict()),
    )
    assert store.read_run(run.run_id) == updated
    with monkeypatch.context() as context:
        context.setattr(
            workflow_store.os,
            "replace",
            lambda _source, _target: (_ for _ in ()).throw(
                OSError("fixture replace failure")
            ),
        )
        failed = replace(updated, updated_at="2026-07-27T12:34:58.000001Z")
        with pytest.raises(OSError, match="fixture replace failure"):
            store.write_run(
                failed,
                expected_run_hash=canonical_sha256_v1(updated.to_dict()),
            )
    assert store.read_run(run.run_id) == updated
    assert not list((root / "workflow" / "runs").glob(".*.tmp"))


def test_approval_is_immutable_and_status_does_not_create_state_file(tmp_path: Path) -> None:
    root = tmp_path / "project"
    project = create_project(root, "Project")
    store = WorkflowStore(root)
    store.write_run(_run(project.project_id))
    approval = store.write_approval(_approval(project.project_id))

    assert store.read_approval("appr_test", run_id="wfr_test") == approval
    assert (
        store.approval_status(
            "appr_test",
            run_id="wfr_test",
            subject=approval.subject,
            dependency_hash=approval.dependency_hash,
        )
        == "current"
    )
    with pytest.raises(WorkflowError, match="immutable"):
        store.write_approval(approval)
    assert not (root / "workflow" / "approval-states").exists()


def test_receipt_same_id_and_input_reads_back_but_different_input_conflicts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    project = create_project(root, "Project")
    store = WorkflowStore(root)
    run = store.write_run(_run(project.project_id))
    _attach_scope_approval(store, run, _approval(project.project_id))
    receipt = _receipt(project.project_id)

    assert store.write_receipt(receipt) == receipt
    response_lost_retry = replace(receipt, created_at="2026-07-27T12:34:57.000001Z")
    assert store.write_receipt(response_lost_retry) == receipt
    assert store.read_receipt("act_test", run_id="wfr_test", input_hash=HASH_A) == receipt
    with pytest.raises(WorkflowError) as captured:
        store.read_receipt("act_test", run_id="wfr_test", input_hash=HASH_B)
    assert captured.value.code == "workflow_action_conflict"
    with pytest.raises(WorkflowError) as captured:
        store.write_receipt(_receipt(project.project_id, input_hash=HASH_B))
    assert captured.value.code == "workflow_action_conflict"


def test_run_can_reference_the_receipt_pending_in_its_transaction(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    project = create_project(root, "Project")
    store = WorkflowStore(root)
    initial = store.write_run(_run(project.project_id))
    approval = _approval(project.project_id)
    approval_ref = ApprovalRef(
        approval_id=approval.approval_id,
        record_schema_version=1,
        record_hash=canonical_sha256_v1(approval.to_dict()),
    )
    approved = replace(
        initial,
        ordered_bindings=(WorkflowBinding("src_scope", None, None),),
        scope_authorizations=(ScopeAuthorization("src_scope", True, True),),
        approval_refs={**initial.approval_refs, "scope": approval_ref},
    )
    receipt = _receipt(project.project_id)
    receipt_ref = ReceiptRef(
        action_id=receipt.action_id,
        receipt_schema_version=1,
        receipt_hash=canonical_sha256_v1(receipt.to_dict()),
    )
    after_run = replace(approved, last_receipt_ref=receipt_ref)
    marker = TransactionMarker(
        schema_version=1,
        action_id=receipt.action_id,
        input_hash=receipt.input_hash,
        run_id=approved.run_id,
        project_id=project.project_id,
        action=receipt.action,
        project_before_hash=canonical_sha256_v1(project.to_dict()),
        project_after_hash=canonical_sha256_v1(project.to_dict()),
        run_before_hash=canonical_sha256_v1(initial.to_dict()),
        run_after_hash=canonical_sha256_v1(after_run.to_dict()),
        project_before=project,
        run_before=initial,
        candidate_refs=(),
        approval_ids=(approval.approval_id,),
        commit_step="prepared",
    )

    store.write_transaction(marker)
    store.write_approval(approval)
    store.write_run(
        after_run,
        expected_run_hash=canonical_sha256_v1(initial.to_dict()),
    )
    store.write_receipt(receipt)
    store.delete_transaction(receipt.action_id, input_hash=receipt.input_hash)
    assert store.read_run(after_run.run_id) == after_run


def test_transaction_marker_roundtrip_update_refuses_delete_without_receipt(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    project = create_project(root, "Project")
    store = WorkflowStore(root)
    run = store.write_run(_run(project.project_id))
    marker = TransactionMarker(
        schema_version=1,
        action_id="act_marker",
        input_hash=HASH_A,
        run_id="wfr_test",
        project_id=project.project_id,
        action="approve_scope",
        project_before_hash=canonical_sha256_v1(project.to_dict()),
        project_after_hash=HASH_B,
        run_before_hash=canonical_sha256_v1(run.to_dict()),
        run_after_hash=HASH_B,
        project_before=project,
        run_before=run,
        candidate_refs=(),
        approval_ids=("appr_marker",),
        commit_step="prepared",
    )

    store.write_transaction(marker)
    assert store.write_transaction(marker) == marker
    with pytest.raises(WorkflowError) as captured:
        store.write_transaction(replace(marker, input_hash=HASH_B))
    assert captured.value.code == "workflow_action_conflict"
    assert store.read_transaction("act_marker", run_id="wfr_test") == marker
    advanced = replace(marker, commit_step="run_published")
    assert store.write_transaction(advanced) == advanced
    with pytest.raises(WorkflowError, match="cannot move backward"):
        store.write_transaction(marker)
    with pytest.raises(WorkflowError, match="immutable fields changed"):
        store.write_transaction(replace(advanced, project_after_hash=HASH_A))
    with pytest.raises(WorkflowError, match="ActionReceipt"):
        store.delete_transaction("act_marker", input_hash=HASH_A)
    assert (root / "workflow" / "transactions" / "act_marker.json").exists()


def test_completed_export_run_can_publish_its_final_receipt(tmp_path: Path) -> None:
    root = tmp_path / "project"
    project = create_project(root, "Project")
    store = WorkflowStore(root)
    initial = store.write_run(_run(project.project_id))
    export_run = replace(
        initial,
        stage="export_review",
        ordered_bindings=(WorkflowBinding("src_export", None, None),),
    )
    export_run = store.write_run(
        export_run,
        expected_run_hash=canonical_sha256_v1(initial.to_dict()),
    )
    approval = ApprovalRecord(
        schema_version=1,
        approval_id="appr_export",
        run_id=export_run.run_id,
        project_id=project.project_id,
        gate="export",
        subject=SubjectRef("export_snapshot", "export_test", 1, HASH_A),
        dependency_hash=HASH_B,
        issued_project_revision=0,
        issued_by_action_id="act_export",
        source_channel="agent_conversation",
        source_action="approve_export",
        actor_assurance="unverified_host_user_action",
        issued_at=NOW,
    )
    export_ref = ApprovalRef(
        approval_id=approval.approval_id,
        record_schema_version=1,
        record_hash=canonical_sha256_v1(approval.to_dict()),
    )
    approved_run = replace(
        export_run,
        approval_refs={**export_run.approval_refs, "export": export_ref},
    )
    mp4_payload = b"mp4"
    mp4_hash = hashlib.sha256(mp4_payload).hexdigest()
    manifest_payload = _manifest_payload(project.project_id)
    manifest_hash = _manifest_content_hash(manifest_payload)
    receipt = ActionReceipt(
        schema_version=1,
        action_id="act_export",
        input_hash=HASH_A,
        run_id=approved_run.run_id,
        project_id=project.project_id,
        action="approve_export",
        before=ReceiptState("export_review", "active", 0),
        after=ReceiptState("exporting", "completed", 0),
        approval_ids=("appr_export",),
        mutation=MutationRef("render", "render_test", 2, manifest_hash, True),
        output_refs=(
            OutputRef("mp4", "render_test", 1, mp4_hash, "renders/render_test.mp4"),
            OutputRef(
                "manifest",
                "render_test",
                2,
                manifest_hash,
                "renders/render_test.manifest.json",
            ),
        ),
        created_at=NOW,
    )
    receipt_ref = ReceiptRef(
        action_id=receipt.action_id,
        receipt_schema_version=1,
        receipt_hash=canonical_sha256_v1(receipt.to_dict()),
    )
    completed = replace(
        approved_run,
        stage="exporting",
        lifecycle="completed",
        last_receipt_ref=receipt_ref,
    )
    marker = TransactionMarker(
        schema_version=1,
        action_id=receipt.action_id,
        input_hash=receipt.input_hash,
        run_id=approved_run.run_id,
        project_id=project.project_id,
        action=receipt.action,
        project_before_hash=canonical_sha256_v1(project.to_dict()),
        project_after_hash=canonical_sha256_v1(project.to_dict()),
        run_before_hash=canonical_sha256_v1(export_run.to_dict()),
        run_after_hash=canonical_sha256_v1(completed.to_dict()),
        project_before=project,
        run_before=export_run,
        candidate_refs=receipt.output_refs,
        approval_ids=(approval.approval_id,),
        commit_step="prepared",
    )

    store.write_transaction(marker)
    store.write_approval(approval)
    renders = root / "renders"
    renders.mkdir()
    (renders / "render_test.mp4").write_bytes(mp4_payload)
    (renders / "render_test.manifest.json").write_text(
        json.dumps(manifest_payload),
        encoding="utf-8",
    )
    store.write_transaction(replace(marker, commit_step="candidates_published"))
    store.write_run(
        completed,
        expected_run_hash=canonical_sha256_v1(export_run.to_dict()),
    )
    store.write_transaction(replace(marker, commit_step="run_published"))
    assert store.write_receipt(receipt) == receipt
    store.delete_transaction(receipt.action_id, input_hash=receipt.input_hash)
    (renders / "render_test.mp4").unlink()
    assert store.write_receipt(receipt) == receipt


def test_scope_authorizations_survive_store_reopen(tmp_path: Path) -> None:
    root = tmp_path / "project"
    project = create_project(root, "Project")
    store = WorkflowStore(root)
    initial = store.write_run(_run(project.project_id))
    approved = _attach_scope_approval(store, initial, _approval(project.project_id))

    reopened = WorkflowStore(root).read_run(approved.run_id)

    assert reopened.scope_authorizations == (
        ScopeAuthorization("src_scope", True, True),
    )


def test_canceled_run_can_publish_and_replay_cancel_receipt(tmp_path: Path) -> None:
    root = tmp_path / "project"
    project = create_project(root, "Project")
    store = WorkflowStore(root)
    initial = store.write_run(_run(project.project_id))
    input_hash = workflow_action_input_hash(
        initial.run_id, "act_cancel", "workflow_cancel", {}
    )
    receipt = ActionReceipt(
        schema_version=1,
        action_id="act_cancel",
        input_hash=input_hash,
        run_id=initial.run_id,
        project_id=project.project_id,
        action="workflow_cancel",
        before=ReceiptState("scope_review", "active", 0),
        after=ReceiptState("scope_review", "canceled", 0),
        approval_ids=(),
        mutation=None,
        output_refs=(),
        created_at=NOW,
    )
    receipt_ref = ReceiptRef(
        action_id=receipt.action_id,
        receipt_schema_version=1,
        receipt_hash=canonical_sha256_v1(receipt.to_dict()),
    )
    canceled = replace(
        initial,
        lifecycle="canceled",
        last_receipt_ref=receipt_ref,
    )
    marker = TransactionMarker(
        schema_version=1,
        action_id=receipt.action_id,
        input_hash=receipt.input_hash,
        run_id=initial.run_id,
        project_id=project.project_id,
        action=receipt.action,
        project_before_hash=canonical_sha256_v1(project.to_dict()),
        project_after_hash=canonical_sha256_v1(project.to_dict()),
        run_before_hash=canonical_sha256_v1(initial.to_dict()),
        run_after_hash=canonical_sha256_v1(canceled.to_dict()),
        project_before=project,
        run_before=initial,
        candidate_refs=(),
        approval_ids=(),
        commit_step="prepared",
    )

    store.write_transaction(marker)
    store.write_run(
        canceled,
        expected_run_hash=canonical_sha256_v1(initial.to_dict()),
    )
    store.write_transaction(replace(marker, commit_step="run_published"))
    assert store.write_receipt(receipt) == receipt
    store.delete_transaction(receipt.action_id, input_hash=receipt.input_hash)
    assert store.write_receipt(receipt) == receipt
    assert store.read_run(initial.run_id).lifecycle == "canceled"


@pytest.mark.parametrize("kind", ["run", "approval", "receipt"])
def test_objects_copied_to_another_project_are_rejected(tmp_path: Path, kind: str) -> None:
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_project = create_project(source_root, "Source")
    target_project = create_project(target_root, "Target")
    source = WorkflowStore(source_root)
    source_run = source.write_run(_run(source_project.project_id))
    if kind == "approval":
        source.write_approval(_approval(source_project.project_id))
    if kind == "receipt":
        _attach_scope_approval(
            source,
            source_run,
            _approval(source_project.project_id),
        )
        source.write_receipt(_receipt(source_project.project_id))

    relative = {
        "run": Path("runs/wfr_test.json"),
        "approval": Path("approvals/appr_test.json"),
        "receipt": Path("receipts/act_test.json"),
    }[kind]
    target_file = target_root / "workflow" / relative
    target_file.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_root / "workflow" / relative, target_file)
    target = WorkflowStore(target_root)

    with pytest.raises(WorkflowError, match="another Project|project_id"):
        if kind == "run":
            target.read_run("wfr_test")
        else:
            target.write_run(_run(target_project.project_id))
            if kind == "approval":
                target.read_approval("appr_test", run_id="wfr_test")
            else:
                target.read_receipt("act_test", run_id="wfr_test")


def test_id_path_mismatch_duplicate_key_and_unknown_schema_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "project"
    project = create_project(root, "Project")
    store = WorkflowStore(root)
    store.write_run(_run(project.project_id))
    path = root / "workflow" / "runs" / "wfr_test.json"

    payload = path.read_text(encoding="utf-8").replace('"run_id":"wfr_test"', '"run_id":"other"')
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(WorkflowError, match="does not match its path"):
        store.read_run("wfr_test")

    path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    with pytest.raises(WorkflowError, match="duplicate key"):
        store.read_run("wfr_test")

    payload = _run(project.project_id).to_dict()
    payload["schema_version"] = 99
    path.write_bytes(workflow_store.canonical_json_v1(payload))
    with pytest.raises(WorkflowError, match="unsupported"):
        store.read_run("wfr_test")


def test_unsafe_id_path_and_symlinked_workflow_tree_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "project"
    create_project(root, "Project")
    with pytest.raises(WorkflowError, match="safe workflow ID"):
        WorkflowStore(root).read_run("../escape")

    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "workflow").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable")
    with pytest.raises(WorkflowError, match="real directory"):
        WorkflowStore(root).list_runs()


def test_hardlinked_json_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "project"
    project = create_project(root, "Project")
    store = WorkflowStore(root)
    store.write_run(_run(project.project_id))
    path = root / "workflow" / "runs" / "wfr_test.json"
    try:
        os.link(path, tmp_path / "second-link")
    except (OSError, NotImplementedError):
        pytest.skip("hardlinks are unavailable")

    with pytest.raises(WorkflowError, match="single-link"):
        store.read_run("wfr_test")


@pytest.mark.parametrize("failure", [OSError("replace failed"), KeyboardInterrupt()])
def test_atomic_write_failure_leaves_no_temporary_or_half_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    root = tmp_path / "project"
    project = create_project(root, "Project")
    store = WorkflowStore(root)

    def fail_replace(_source: object, _target: object) -> None:
        raise failure

    monkeypatch.setattr(workflow_store.os, "replace", fail_replace)
    with pytest.raises(type(failure)):
        store.write_run(_run(project.project_id))

    runs = root / "workflow" / "runs"
    assert not (runs / "wfr_test.json").exists()
    assert not list(runs.glob(".*.tmp"))


def test_project_lock_is_reentrant_and_released_after_keyboard_interrupt(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    create_project(root, "Project")
    with pytest.raises(KeyboardInterrupt), project_write_lock(root), project_write_lock(root):
        raise KeyboardInterrupt
    with project_write_lock(root):
        assert (root / LOCK_FILENAME).is_file()


@pytest.mark.parametrize("target_kind", ["symlink", "hardlink", "directory"])
def test_project_lock_targets_fail_closed(
    tmp_path: Path, target_kind: str
) -> None:
    root = tmp_path / "project"
    create_project(root, "Project")
    lock = root / LOCK_FILENAME
    lock.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    try:
        if target_kind == "symlink":
            lock.symlink_to(outside)
        elif target_kind == "hardlink":
            os.link(outside, lock)
        else:
            lock.mkdir()
    except (OSError, NotImplementedError):
        pytest.skip(f"{target_kind} is unavailable")

    with pytest.raises(WorkflowError) as captured, project_write_lock(root):
        raise AssertionError("invalid lock reached writer")
    assert captured.value.code == "workflow_lock_failed"
    assert "Roughcut project storage" in str(captured.value)
    assert outside.read_bytes() == b"outside"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is unavailable")
def test_non_regular_project_lock_target_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "project"
    create_project(root, "Project")
    lock = root / LOCK_FILENAME
    lock.unlink()
    os.mkfifo(lock)

    with pytest.raises(WorkflowError) as captured, project_write_lock(root):
        raise AssertionError("FIFO lock reached writer")
    assert captured.value.code == "workflow_lock_failed"
    assert stat.S_ISFIFO(lock.lstat().st_mode)


def test_two_processes_with_same_project_revision_allow_only_one_writer(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    create_project(root, "Project")
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    results = context.Queue()
    start = context.Event()
    processes = [
        context.Process(
            target=_concurrent_project_save,
            args=(str(root), 0, ready, start, results),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    try:
        for _ in processes:
            assert ready.get(timeout=15) == "ready"
        start.set()
        outcomes = [results.get(timeout=15) for _ in processes]
    except Empty as error:
        raise AssertionError("cross-process Project writer did not report") from error
    finally:
        for process in processes:
            process.join(timeout=15)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    assert sorted(outcome[0] for outcome in outcomes) == ["ProjectError", "ok"]
    assert ProjectStore(root).load().revision == 1
    assert all(process.exitcode == 0 for process in processes)


def test_project_store_expected_revision_is_checked_inside_shared_lock(tmp_path: Path) -> None:
    root = tmp_path / "project"
    project = create_project(root, "Project")
    store = ProjectStore(root)
    with WorkflowStore(root).write_lock():
        store.save(replace(project, revision=1), expected_revision=0)
    with pytest.raises(ProjectError, match="revision conflict"):
        store.save(replace(project, revision=2), expected_revision=0)
    assert store.load().revision == 1


def test_project_store_cannot_overwrite_concurrent_workflow_writer(tmp_path: Path) -> None:
    root = tmp_path / "project"
    project = create_project(root, "Project")
    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    release = context.Event()
    results = context.Queue()
    process = context.Process(
        target=_workflow_locked_project_save,
        args=(str(root), entered, release, results),
    )
    process.start()
    writer_errors: list[BaseException] = []

    def stale_project_writer() -> None:
        try:
            ProjectStore(root).save(replace(project, revision=1), expected_revision=0)
        except ProjectError as error:
            writer_errors.append(error)

    try:
        assert entered.wait(timeout=15)
        writer = threading.Thread(target=stale_project_writer)
        writer.start()
        writer.join(timeout=0.2)
        assert writer.is_alive()
        release.set()
        writer.join(timeout=15)
        assert not writer.is_alive()
    finally:
        release.set()
        process.join(timeout=15)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)

    assert results.get(timeout=5) == ("ok", "workflow writer saved")
    assert len(writer_errors) == 1
    assert isinstance(writer_errors[0], ProjectError)
    assert str(writer_errors[0]) == "project revision conflict"
    assert process.exitcode == 0
    assert ProjectStore(root).load().revision == 1


def test_windows_lock_branch_has_static_fixture_coverage_only() -> None:
    source = inspect.getsource(project_lock._acquire_file_lock)
    assert "msvcrt.locking" in source
    assert "LK_NBLCK" in source
    assert "_uses_windows_lock()" in source


@pytest.mark.parametrize(
    "stop_after",
    ["marker", "candidates", "project", "run", "receipt"],
)
def test_fwv_018_hard_exit_recovers_before_or_reads_committed_receipt(
    tmp_path: Path,
    stop_after: str,
) -> None:
    root = tmp_path / f"project-{stop_after}"
    scenario = _recovery_scenario(root)
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_hard_exit_recovery_participants,
        args=(str(root), scenario, stop_after),
    )
    process.start()
    process.join(timeout=20)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        raise AssertionError("hard-exit workflow fixture did not terminate")
    assert process.exitcode == 91

    recovery = WorkflowStore(root).recover_pending()

    assert len(recovery) == 1
    assert not (
        root / "workflow" / "transactions" / f"{scenario.marker.action_id}.json"
    ).exists()
    if stop_after == "receipt":
        assert recovery[0].disposition == "receipt_committed"
        assert recovery[0].receipt == scenario.receipt
        assert ProjectStore(root).load() == scenario.project_after
        assert WorkflowStore(root).read_run(scenario.run_after.run_id) == scenario.run_after
    else:
        assert recovery[0].disposition == "rolled_back"
        assert "reconfirm" in recovery[0].message
        assert recovery[0].receipt is None
        assert ProjectStore(root).load() == scenario.project_before
        assert WorkflowStore(root).read_run(scenario.run_before.run_id) == scenario.run_before
        assert not (root / "briefs" / "brief_recovery.json").exists()
        assert not (
            root / "workflow" / "approvals" / f"{scenario.approval.approval_id}.json"
        ).exists()
        assert not (
            root / "workflow" / "receipts" / f"{scenario.receipt.action_id}.json"
        ).exists()


def test_run_published_recovery_reads_schema_two_render_ref_and_rolls_back(
    tmp_path: Path,
) -> None:
    root = tmp_path / "schema-two-render"
    scenario = _recovery_scenario(root)
    render_after = replace(
        scenario.run_after,
        artifact_refs={
            **scenario.run_after.artifact_refs,
            "render": ArtifactRef("render_recovery", 2, HASH_A),
        },
    )
    marker = replace(
        scenario.marker,
        run_after_hash=canonical_sha256_v1(render_after.to_dict()),
    )
    scenario = replace(
        scenario,
        run_after=render_after,
        marker=marker,
    )
    _publish_recovery_participants(root, scenario, "run")
    store = WorkflowStore(root)

    run_path = root / "workflow" / "runs" / f"{render_after.run_id}.json"
    assert WorkflowRun.from_dict(
        json.loads(run_path.read_text(encoding="utf-8"))
    ) == render_after
    marker_path = (
        root
        / "workflow"
        / "transactions"
        / f"{scenario.marker.action_id}.json"
    )
    assert json.loads(marker_path.read_text(encoding="utf-8"))[
        "commit_step"
    ] == "run_published"

    recovery = store.recover_pending()

    assert len(recovery) == 1
    assert recovery[0].disposition == "rolled_back"
    assert store.read_run(scenario.run_before.run_id) == scenario.run_before
    assert not marker_path.exists()


@pytest.mark.parametrize("third_state", ["project", "run"])
def test_fwv_019_third_state_fails_closed_without_overwrite(
    tmp_path: Path,
    third_state: str,
) -> None:
    root = tmp_path / f"project-{third_state}"
    scenario = _recovery_scenario(root)
    _publish_recovery_participants(root, scenario, "run")
    store = WorkflowStore(root)
    if third_state == "project":
        ProjectStore(root).save(
            replace(scenario.project_after, revision=2),
            expected_revision=1,
        )
    else:
        third_run = replace(
            scenario.run_after,
            updated_at="2026-07-27T12:34:57.000001Z",
        )
        with store.write_lock():
            store._atomic_write(
                root / "workflow" / "runs" / "wfr_test.json",
                third_run.to_dict(),
                immutable=False,
            )
    project_bytes = (root / "project.json").read_bytes()
    run_bytes = (root / "workflow" / "runs" / "wfr_test.json").read_bytes()

    with pytest.raises(WorkflowError) as captured:
        store.recover_pending()

    assert captured.value.code == "workflow_recovery_conflict"
    assert "Roughcut core/store" in str(captured.value)
    assert (root / "project.json").read_bytes() == project_bytes
    assert (root / "workflow" / "runs" / "wfr_test.json").read_bytes() == run_bytes
    assert (
        root / "workflow" / "transactions" / "act_recovery.json"
    ).exists()


@pytest.mark.parametrize("third_state", ["project", "run"])
def test_write_receipt_reconciles_residual_marker_before_idempotent_readback(
    tmp_path: Path,
    third_state: str,
) -> None:
    root = tmp_path / third_state
    scenario = _recovery_scenario(root)
    _publish_recovery_participants(root, scenario, "receipt")
    store = WorkflowStore(root)
    if third_state == "project":
        ProjectStore(root).save(
            replace(scenario.project_after, revision=2),
            expected_revision=1,
        )
    else:
        with store.write_lock():
            store._atomic_write(
                root / "workflow" / "runs" / "wfr_test.json",
                replace(
                    scenario.run_after,
                    updated_at="2026-07-27T12:34:57.000001Z",
                ).to_dict(),
                immutable=False,
            )
    project_bytes = (root / "project.json").read_bytes()
    run_bytes = (root / "workflow" / "runs" / "wfr_test.json").read_bytes()

    with pytest.raises(WorkflowError) as captured:
        store.write_receipt(scenario.receipt)

    assert captured.value.code == "workflow_recovery_conflict"
    assert (root / "project.json").read_bytes() == project_bytes
    assert (root / "workflow" / "runs" / "wfr_test.json").read_bytes() == run_bytes
    assert (
        root / "workflow" / "transactions" / "act_recovery.json"
    ).exists()


def test_write_receipt_reconciles_exact_after_and_keeps_historical_readback(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    scenario = _recovery_scenario(root)
    _publish_recovery_participants(root, scenario, "receipt")
    store = WorkflowStore(root)

    assert store.write_receipt(scenario.receipt) == scenario.receipt
    assert not (
        root / "workflow" / "transactions" / "act_recovery.json"
    ).exists()

    ProjectStore(root).save(
        replace(scenario.project_after, revision=2),
        expected_revision=1,
    )
    assert store.write_receipt(scenario.receipt) == scenario.receipt


@pytest.mark.parametrize(
    ("stop_after", "commit_step"),
    [
        ("marker", "prepared"),
        ("candidates", "candidates_published"),
        ("project", "project_published"),
        ("run", "run_published"),
    ],
)
def test_delete_transaction_refuses_every_pre_receipt_step(
    tmp_path: Path,
    stop_after: str,
    commit_step: str,
) -> None:
    root = tmp_path / stop_after
    scenario = _recovery_scenario(root)
    _publish_recovery_participants(root, scenario, stop_after)
    marker_path = root / "workflow" / "transactions" / "act_recovery.json"

    with pytest.raises(WorkflowError, match="ActionReceipt") as captured:
        WorkflowStore(root).delete_transaction(
            scenario.marker.action_id,
            input_hash=scenario.marker.input_hash,
        )

    assert captured.value.code == "workflow_integrity_error"
    assert marker_path.exists()
    assert json.loads(marker_path.read_text(encoding="utf-8"))["commit_step"] == commit_step


def test_delete_transaction_reconciles_committed_receipt(tmp_path: Path) -> None:
    root = tmp_path / "project"
    scenario = _recovery_scenario(root)
    _publish_recovery_participants(root, scenario, "receipt")

    WorkflowStore(root).delete_transaction(
        scenario.marker.action_id,
        input_hash=scenario.marker.input_hash,
    )

    assert not (
        root / "workflow" / "transactions" / "act_recovery.json"
    ).exists()


@pytest.mark.parametrize(
    ("field", "replacement"),
    [("input_hash", HASH_A), ("action", "approve_scope")],
)
def test_delete_transaction_rejects_mismatched_receipt_and_retains_marker(
    tmp_path: Path,
    field: str,
    replacement: str,
) -> None:
    root = tmp_path / "project"
    scenario = _recovery_scenario(root)
    _publish_recovery_participants(root, scenario, "receipt")
    receipt_path = root / "workflow" / "receipts" / "act_recovery.json"
    payload = scenario.receipt.to_dict()
    payload[field] = replacement
    receipt_path.write_bytes(canonical_json_v1(payload) + b"\n")

    with pytest.raises(WorkflowError) as captured:
        WorkflowStore(root).delete_transaction(
            scenario.marker.action_id,
            input_hash=scenario.marker.input_hash,
        )

    assert captured.value.code == "workflow_integrity_error"
    assert (
        root / "workflow" / "transactions" / "act_recovery.json"
    ).exists()


def test_delete_transaction_rejects_committed_receipt_in_third_state(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    scenario = _recovery_scenario(root)
    _publish_recovery_participants(root, scenario, "receipt")
    ProjectStore(root).save(
        replace(scenario.project_after, revision=2),
        expected_revision=1,
    )
    project_bytes = (root / "project.json").read_bytes()
    run_bytes = (root / "workflow" / "runs" / "wfr_test.json").read_bytes()

    with pytest.raises(WorkflowError) as captured:
        WorkflowStore(root).delete_transaction(
            scenario.marker.action_id,
            input_hash=scenario.marker.input_hash,
        )

    assert captured.value.code == "workflow_recovery_conflict"
    assert (root / "project.json").read_bytes() == project_bytes
    assert (root / "workflow" / "runs" / "wfr_test.json").read_bytes() == run_bytes
    assert (
        root / "workflow" / "transactions" / "act_recovery.json"
    ).exists()


def test_recovery_deletes_candidate_only_when_json_content_hash_matches(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    scenario = _recovery_scenario(root)
    _publish_recovery_participants(root, scenario, "candidates")
    candidate = root / "briefs" / "brief_recovery.json"

    assert WorkflowStore(root).recover_pending()[0].disposition == "rolled_back"
    assert not candidate.exists()


@pytest.mark.parametrize("candidate_kind", ["mp4", "manifest"])
def test_recovery_uses_real_export_candidate_hash_rules(
    tmp_path: Path,
    candidate_kind: str,
) -> None:
    root = tmp_path / candidate_kind
    _marker, candidate, _payload = _export_candidate_scenario(root, candidate_kind)

    assert WorkflowStore(root).recover_pending()[0].disposition == "rolled_back"
    assert not candidate.exists()


@pytest.mark.parametrize("candidate_kind", ["brief", "mp4", "manifest"])
def test_recovery_retains_replaced_candidate_and_marker(
    tmp_path: Path,
    candidate_kind: str,
) -> None:
    root = tmp_path / candidate_kind
    if candidate_kind == "brief":
        scenario = _recovery_scenario(root)
        _publish_recovery_participants(root, scenario, "run")
        candidate = root / "briefs" / "brief_recovery.json"
        candidate.write_text(
            json.dumps({**_brief_payload(), "theme": "Replacement"}),
            encoding="utf-8",
        )
        marker_path = (
            root / "workflow" / "transactions" / f"{scenario.marker.action_id}.json"
        )
    else:
        marker, candidate, _payload = _export_candidate_scenario(root, candidate_kind)
        candidate.write_bytes(b"replacement")
        marker_path = root / "workflow" / "transactions" / f"{marker.action_id}.json"

    with pytest.raises(WorkflowError) as captured:
        WorkflowStore(root).recover_pending()

    assert captured.value.code == "workflow_recovery_conflict"
    assert "Roughcut core/store 拒绝删除内容身份不匹配的候选" in str(captured.value)
    assert candidate.exists()
    assert marker_path.exists()
    if candidate_kind == "brief":
        assert ProjectStore(root).load() == scenario.project_after
        run_payload = json.loads(
            (root / "workflow" / "runs" / "wfr_test.json").read_text(
                encoding="utf-8"
            )
        )
        assert WorkflowRun.from_dict(run_payload) == scenario.run_after


@pytest.mark.parametrize("mixed_state", ["project_after", "run_after"])
def test_recovery_accepts_each_before_after_mixed_state(
    tmp_path: Path,
    mixed_state: str,
) -> None:
    root = tmp_path / mixed_state
    scenario = _recovery_scenario(root)
    if mixed_state == "project_after":
        _publish_recovery_participants(root, scenario, "project")
    else:
        store = WorkflowStore(root)
        store.write_transaction(scenario.marker)
        store.write_approval(scenario.approval)
        store.write_run(
            scenario.run_after,
            expected_run_hash=scenario.marker.run_before_hash,
        )

    recovery = WorkflowStore(root).recover_pending()

    assert recovery[0].disposition == "rolled_back"
    assert ProjectStore(root).load() == scenario.project_before
    assert WorkflowStore(root).read_run("wfr_test") == scenario.run_before


@pytest.mark.parametrize("already_deleted", ["candidate", "approval"])
def test_recovery_repeats_when_owned_files_are_partly_deleted(
    tmp_path: Path,
    already_deleted: str,
) -> None:
    root = tmp_path / already_deleted
    scenario = _recovery_scenario(root)
    _publish_recovery_participants(root, scenario, "run")
    path = (
        root / "briefs" / "brief_recovery.json"
        if already_deleted == "candidate"
        else root / "workflow" / "approvals" / "appr_recovery.json"
    )
    path.unlink()

    recovery = WorkflowStore(root).recover_pending()

    assert recovery[0].disposition == "rolled_back"
    assert ProjectStore(root).load() == scenario.project_before
    assert WorkflowStore(root).read_run("wfr_test") == scenario.run_before


def test_recovery_interruption_keeps_marker_and_second_attempt_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    scenario = _recovery_scenario(root)
    _publish_recovery_participants(root, scenario, "run")
    store = WorkflowStore(root)

    def interrupt_approval_cleanup(
        marker: TransactionMarker,
        approval_id: str,
    ) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        store,
        "_delete_owned_approval_locked",
        interrupt_approval_cleanup,
    )
    with pytest.raises(KeyboardInterrupt):
        store.recover_pending()

    assert (
        root / "workflow" / "transactions" / "act_recovery.json"
    ).exists()
    assert ProjectStore(root).load() == scenario.project_before
    assert WorkflowStore(root).recover_pending()[0].disposition == "rolled_back"
    assert WorkflowStore(root).read_run("wfr_test") == scenario.run_before


def test_hard_exit_during_recovery_is_repeatable(tmp_path: Path) -> None:
    root = tmp_path / "project"
    scenario = _recovery_scenario(root)
    _publish_recovery_participants(root, scenario, "run")
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_hard_exit_during_recovery,
        args=(str(root),),
    )
    process.start()
    process.join(timeout=20)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        raise AssertionError("hard-exit recovery fixture did not terminate")

    assert process.exitcode == 92
    assert (
        root / "workflow" / "transactions" / "act_recovery.json"
    ).exists()
    assert ProjectStore(root).load() == scenario.project_before
    assert WorkflowStore(root).recover_pending()[0].disposition == "rolled_back"
    assert WorkflowStore(root).read_run("wfr_test") == scenario.run_before


@pytest.mark.parametrize("failure_point", ["project", "run"])
def test_recovery_write_failure_keeps_repeatable_marker_and_no_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    root = tmp_path / failure_point
    scenario = _recovery_scenario(root)
    _publish_recovery_participants(root, scenario, "run")
    store = WorkflowStore(root)
    if failure_point == "project":
        def fail_project_save(
            _store: ProjectStore,
            _project: Any,
            *,
            expected_revision: int | None,
        ) -> None:
            raise OSError("injected Project recovery failure")

        monkeypatch.setattr(ProjectStore, "save", fail_project_save)
    else:
        atomic_write = store._atomic_write

        def fail_run_write(
            path: Path,
            payload: dict[str, object],
            *,
            immutable: bool,
        ) -> None:
            if path.parent.name == "runs":
                raise OSError("injected WorkflowRun recovery failure")
            atomic_write(path, payload, immutable=immutable)

        monkeypatch.setattr(store, "_atomic_write", fail_run_write)

    with pytest.raises(WorkflowError) as captured:
        store.recover_pending()

    assert captured.value.code == "workflow_integrity_error"
    assert "Roughcut workflow store" in str(captured.value)
    assert (root / "workflow" / "transactions" / "act_recovery.json").exists()
    assert not list((root / "workflow" / "runs").glob(".*.tmp"))
    if failure_point == "project":
        assert ProjectStore(root).load() == scenario.project_after
    else:
        assert ProjectStore(root).load() == scenario.project_before
        run_payload = json.loads(
            (root / "workflow" / "runs" / "wfr_test.json").read_text(
                encoding="utf-8"
            )
        )
        assert WorkflowRun.from_dict(run_payload) == scenario.run_after


@pytest.mark.parametrize(
    "mismatch",
    ["action_id", "input_hash", "run_id", "project_id", "action"],
)
def test_receipt_identity_mismatch_fails_closed(
    tmp_path: Path,
    mismatch: str,
) -> None:
    root = tmp_path / mismatch
    scenario = _recovery_scenario(root)
    _publish_recovery_participants(root, scenario, "run")
    receipt_payload = scenario.receipt.to_dict()
    receipt_payload[mismatch] = {
        "action_id": "act_other",
        "input_hash": HASH_A,
        "run_id": "wfr_other",
        "project_id": "proj_other",
        "action": "approve_scope",
    }[mismatch]
    receipt_path = root / "workflow" / "receipts" / "act_recovery.json"
    receipt_path.write_bytes(canonical_json_v1(receipt_payload) + b"\n")

    with pytest.raises(WorkflowError) as captured:
        WorkflowStore(root).recover_pending()

    assert captured.value.code == "workflow_integrity_error"
    assert "Roughcut workflow store" in str(captured.value)
    assert (
        root / "workflow" / "transactions" / "act_recovery.json"
    ).exists()


def test_receipt_cannot_expose_inconsistent_project_run_success(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    scenario = _recovery_scenario(root)
    _publish_recovery_participants(root, scenario, "receipt")
    ProjectStore(root).save(
        replace(scenario.project_after, revision=2),
        expected_revision=1,
    )

    with pytest.raises(WorkflowError) as captured:
        WorkflowStore(root).recover_pending()

    assert captured.value.code == "workflow_recovery_conflict"
    assert (root / "workflow" / "transactions" / "act_recovery.json").exists()
    assert ProjectStore(root).load().revision == 2


@pytest.mark.parametrize(
    "corruption",
    ["payload_hash", "project_schema", "run_schema", "declared_hash"],
)
def test_invalid_before_image_fails_closed(
    tmp_path: Path,
    corruption: str,
) -> None:
    root = tmp_path / corruption
    scenario = _recovery_scenario(root)
    WorkflowStore(root).write_transaction(scenario.marker)
    marker_path = root / "workflow" / "transactions" / "act_recovery.json"
    marker_payload = json.loads(marker_path.read_text(encoding="utf-8"))
    if corruption == "payload_hash":
        marker_payload["project_before"]["name"] = "Changed"
    elif corruption == "project_schema":
        marker_payload["project_before"]["schema_version"] = 2
    elif corruption == "run_schema":
        marker_payload["run_before"]["schema_version"] = 2
    else:
        marker_payload["project_before_hash"] = HASH_A
    marker_path.write_text(json.dumps(marker_payload), encoding="utf-8")

    with pytest.raises(WorkflowError) as captured:
        WorkflowStore(root).recover_pending()

    assert captured.value.code == "workflow_integrity_error"
    assert "Roughcut workflow store" in str(captured.value)
    assert marker_path.exists()


def test_marker_candidate_path_is_derived_from_controlled_id(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    scenario = _recovery_scenario(root)
    arbitrary_path_marker = replace(
        scenario.marker,
        candidate_refs=(
            OutputRef(
                "brief",
                "brief_recovery",
                1,
                HASH_A,
                "briefs/not_the_owned_id.json",
            ),
        ),
    )

    with pytest.raises(WorkflowError, match="controlled ID"):
        WorkflowStore(root).write_transaction(arbitrary_path_marker)

    assert not (
        root / "workflow" / "transactions" / "act_recovery.json"
    ).exists()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink unavailable")
@pytest.mark.parametrize("target_kind", ["marker", "candidate", "approval"])
def test_recovery_rejects_symlink_marker_candidate_and_approval(
    tmp_path: Path,
    target_kind: str,
) -> None:
    root = tmp_path / target_kind
    scenario = _recovery_scenario(root)
    store = WorkflowStore(root)
    store.write_transaction(scenario.marker)
    external = tmp_path / f"{target_kind}-external"
    external.write_text("user-owned", encoding="utf-8")
    if target_kind == "marker":
        marker_path = root / "workflow" / "transactions" / "act_recovery.json"
        marker_path.unlink()
        marker_path.symlink_to(external)
    elif target_kind == "candidate":
        briefs = root / "briefs"
        briefs.mkdir()
        (briefs / "brief_recovery.json").symlink_to(external)
    else:
        approval_path = root / "workflow" / "approvals" / "appr_recovery.json"
        approval_path.symlink_to(external)

    with pytest.raises(WorkflowError) as captured:
        store.recover_pending()

    assert captured.value.code == "workflow_integrity_error"
    assert external.read_text(encoding="utf-8") == "user-owned"


@pytest.mark.skipif(not hasattr(os, "link"), reason="hardlink unavailable")
@pytest.mark.parametrize("target_kind", ["marker", "candidate", "approval"])
def test_recovery_rejects_hardlinked_marker_candidate_and_approval(
    tmp_path: Path,
    target_kind: str,
) -> None:
    root = tmp_path / target_kind
    scenario = _recovery_scenario(root)
    store = WorkflowStore(root)
    store.write_transaction(scenario.marker)
    external = tmp_path / f"{target_kind}-external"
    try:
        if target_kind == "marker":
            os.link(
                root / "workflow" / "transactions" / "act_recovery.json",
                external,
            )
        elif target_kind == "candidate":
            external.write_text("user-owned", encoding="utf-8")
            briefs = root / "briefs"
            briefs.mkdir()
            os.link(external, briefs / "brief_recovery.json")
        else:
            store.write_approval(scenario.approval)
            os.link(
                root / "workflow" / "approvals" / "appr_recovery.json",
                external,
            )
    except OSError:
        pytest.skip("hardlinks are unavailable")

    with pytest.raises(WorkflowError) as captured:
        store.recover_pending()

    assert captured.value.code == "workflow_integrity_error"
    assert external.exists()


def test_transaction_marker_copied_to_another_project_is_rejected(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source = _recovery_scenario(source_root)
    _recovery_scenario(target_root)
    WorkflowStore(source_root).write_transaction(source.marker)
    target_marker = target_root / "workflow" / "transactions" / "act_recovery.json"
    target_marker.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(
        source_root / "workflow" / "transactions" / "act_recovery.json",
        target_marker,
    )

    with pytest.raises(WorkflowError, match="another Project"):
        WorkflowStore(target_root).recover_pending()


def test_owned_approval_copied_from_another_project_is_rejected(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    scenario = _recovery_scenario(root)
    store = WorkflowStore(root)
    store.write_transaction(scenario.marker)
    foreign = replace(scenario.approval, project_id="proj_foreign")
    approval_path = root / "workflow" / "approvals" / "appr_recovery.json"
    approval_path.write_bytes(canonical_json_v1(foreign.to_dict()) + b"\n")

    with pytest.raises(WorkflowError, match="does not match TransactionMarker"):
        store.recover_pending()

    assert approval_path.exists()
    assert (root / "workflow" / "transactions" / "act_recovery.json").exists()


def test_recovery_deletes_only_marker_owned_files(tmp_path: Path) -> None:
    root = tmp_path / "project"
    scenario = _recovery_scenario(root)
    _publish_recovery_participants(root, scenario, "run")
    unknown = root / "briefs" / "user-history.json"
    unknown.write_text("history", encoding="utf-8")

    WorkflowStore(root).recover_pending()

    assert unknown.read_text(encoding="utf-8") == "history"


def test_unrelated_work_recovers_then_requires_reconfirmation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    scenario = _recovery_scenario(root)
    _publish_recovery_participants(root, scenario, "run")

    with pytest.raises(WorkflowError) as captured:
        WorkflowStore(root).write_run(
            scenario.run_before,
            expected_run_hash=scenario.marker.run_after_hash,
        )

    assert captured.value.code == "workflow_action_conflict"
    assert "reconfirm" in str(captured.value)
    assert ProjectStore(root).load() == scenario.project_before
    assert WorkflowStore(root).read_run("wfr_test") == scenario.run_before


def test_workflow_read_recovers_before_return_and_exposes_notice(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    scenario = _recovery_scenario(root)
    _publish_recovery_participants(root, scenario, "run")
    store = WorkflowStore(root)

    assert store.read_run("wfr_test") == scenario.run_before

    assert store.last_recovery[0].disposition == "rolled_back"
    assert "reconfirm" in store.last_recovery[0].message


def test_response_lost_receipt_readback_cleans_marker_without_rollback(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    scenario = _recovery_scenario(root)
    _publish_recovery_participants(root, scenario, "receipt")
    store = WorkflowStore(root)

    receipt = store.read_receipt(
        scenario.receipt.action_id,
        run_id=scenario.receipt.run_id,
        input_hash=scenario.receipt.input_hash,
    )

    assert receipt == scenario.receipt
    assert store.last_recovery[0].disposition == "receipt_committed"
    assert ProjectStore(root).load() == scenario.project_after
    assert store.read_run("wfr_test") == scenario.run_after
