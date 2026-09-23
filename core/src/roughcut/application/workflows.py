"""Controlled internal application façade for the finite workflow."""

from __future__ import annotations

import json
import os
import shutil
import stat
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypeAlias, cast
from uuid import uuid4

from roughcut.adapters.artifact_store import read_json_object, write_new_json
from roughcut.adapters.media_operation_store import MediaOperationStore
from roughcut.adapters.project_lock import (
    project_export_claim,
    project_export_claim_is_busy,
)
from roughcut.adapters.project_store import ProjectStore
from roughcut.adapters.workflow_candidates import (
    StagedWorkflowFile,
    publish_workflow_candidate,
    stream_file_sha256,
    workflow_export_staging_id,
    workflow_renderer_workspace_name,
)
from roughcut.adapters.workflow_store import WorkflowRecovery, WorkflowStore
from roughcut.application.agent_context import (
    calculate_agent_context_hash,
    prepare_edit_brief,
)
from roughcut.application.content_drafts import (
    _normalize_editor_child_blocks,
    _parse_bindings,
    _parse_blocks,
    _read_active_brief,
    _read_draft,
    prepare_confirmed_content_draft,
    prepare_content_draft_proposal,
)
from roughcut.application.proposals import prepare_proposal_decision
from roughcut.application.renders import (
    execute_prepared_render_to_paths,
    prepare_render_plan,
    renderer_workspace_identity,
)
from roughcut.domain.bindings import SourceTranscriptBinding
from roughcut.domain.brief import EditBrief
from roughcut.domain.content_draft import (
    ContentDraft,
    ContentDraftBlock,
    NarrationBlock,
    project_schema1_to_schema2,
)
from roughcut.domain.edit import (
    EditDecision,
    EditProposal,
    MultiSourceEditDecision,
    MultiSourceEditProposal,
)
from roughcut.domain.errors import WorkflowError
from roughcut.domain.project import Project, SourceAsset
from roughcut.domain.render import RenderPlanLike
from roughcut.domain.transcript import TimedTranscript
from roughcut.domain.workflow import (
    APPROVAL_GATES,
    WORKFLOW_ACTIONS,
    ActionReceipt,
    ApprovalRecord,
    ApprovalRef,
    ArtifactRef,
    MulticamSetup,
    MulticamSetupDeclaration,
    MutationRef,
    OutputRef,
    ReceiptRef,
    ReceiptState,
    ScopeAuthorization,
    SubjectRef,
    TransactionMarker,
    WorkflowBinding,
    WorkflowRun,
    canonical_sha256_v1,
    dependency_bundle_hash,
    subject_content_hash,
    validate_safe_id,
    workflow_action_input_hash,
)
from roughcut.domain.workflow_actions import parse_workflow_action_input

PreparedCandidate: TypeAlias = tuple[
    OutputRef, dict[str, object] | StagedWorkflowFile
]
_ACTION_ORDER = (
    "approve_scope",
    "confirm_brief",
    "submit_outline",
    "approve_outline",
    "submit_draft",
    "approve_draft",
    "return_to_draft",
    "adopt_roughcut",
    "approve_export",
)


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _facade_error(code: str, evidence: str) -> WorkflowError:
    return WorkflowError(
        code,
        f"Roughcut workflow façade rejected the requested operation: {evidence}",
    )


def _translate(error: Exception, action: str) -> WorkflowError:
    if isinstance(error, WorkflowError):
        return error
    return _facade_error(
        "workflow_subject_mismatch",
        f"{action} failed existing Roughcut application/core validation: {error}",
    )


@dataclass(frozen=True)
class WorkflowFacadeResult:
    workflow_run: WorkflowRun
    receipt: ActionReceipt | None
    status: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "workflow_run": self.workflow_run.to_dict(),
            "receipt": None if self.receipt is None else self.receipt.to_dict(),
            "status": self.status,
        }


@dataclass(frozen=True)
class PreparedScopeApproval:
    project_before: Project
    project_after: Project
    run_before: WorkflowRun
    run_after: WorkflowRun
    input_hash: str
    candidates: tuple[PreparedCandidate, ...]
    approvals: tuple[ApprovalRecord, ...]
    receipt: ActionReceipt


@dataclass(frozen=True)
class PreparedBrief:
    project_before: Project
    project_after: Project
    run_before: WorkflowRun
    run_after: WorkflowRun
    input_hash: str
    candidates: tuple[PreparedCandidate, ...]
    approvals: tuple[ApprovalRecord, ...]
    receipt: ActionReceipt


@dataclass(frozen=True)
class PreparedOutline:
    project_before: Project
    project_after: Project
    run_before: WorkflowRun
    run_after: WorkflowRun
    input_hash: str
    candidates: tuple[PreparedCandidate, ...]
    approvals: tuple[ApprovalRecord, ...]
    receipt: ActionReceipt


@dataclass(frozen=True)
class PreparedOutlineApproval:
    project_before: Project
    project_after: Project
    run_before: WorkflowRun
    run_after: WorkflowRun
    input_hash: str
    candidates: tuple[PreparedCandidate, ...]
    approvals: tuple[ApprovalRecord, ...]
    receipt: ActionReceipt


@dataclass(frozen=True)
class PreparedContentDraft:
    project_before: Project
    project_after: Project
    run_before: WorkflowRun
    run_after: WorkflowRun
    input_hash: str
    candidates: tuple[PreparedCandidate, ...]
    approvals: tuple[ApprovalRecord, ...]
    receipt: ActionReceipt


@dataclass(frozen=True)
class PreparedConfirmedDraftAndProposal:
    project_before: Project
    project_after: Project
    run_before: WorkflowRun
    run_after: WorkflowRun
    input_hash: str
    candidates: tuple[PreparedCandidate, ...]
    approvals: tuple[ApprovalRecord, ...]
    receipt: ActionReceipt


@dataclass(frozen=True)
class PreparedReturnToDraft:
    project_before: Project
    project_after: Project
    run_before: WorkflowRun
    run_after: WorkflowRun
    input_hash: str
    candidates: tuple[PreparedCandidate, ...]
    approvals: tuple[ApprovalRecord, ...]
    receipt: ActionReceipt


@dataclass(frozen=True)
class PreparedDecision:
    project_before: Project
    project_after: Project
    run_before: WorkflowRun
    run_after: WorkflowRun
    input_hash: str
    candidates: tuple[PreparedCandidate, ...]
    approvals: tuple[ApprovalRecord, ...]
    receipt: ActionReceipt


@dataclass(frozen=True)
class PreparedExport:
    project_before: Project
    project_after: Project
    run_before: WorkflowRun
    run_after: WorkflowRun
    input_hash: str
    candidates: tuple[PreparedCandidate, ...]
    approvals: tuple[ApprovalRecord, ...]
    receipt: ActionReceipt


@dataclass(frozen=True)
class PreparedWorkflowCancel:
    project_before: Project
    project_after: Project
    run_before: WorkflowRun
    run_after: WorkflowRun
    input_hash: str
    candidates: tuple[PreparedCandidate, ...]
    approvals: tuple[ApprovalRecord, ...]
    receipt: ActionReceipt


PreparedResult: TypeAlias = (
    PreparedScopeApproval
    | PreparedBrief
    | PreparedOutline
    | PreparedOutlineApproval
    | PreparedContentDraft
    | PreparedConfirmedDraftAndProposal
    | PreparedReturnToDraft
    | PreparedDecision
    | PreparedExport
    | PreparedWorkflowCancel
)


def _empty_artifact_refs() -> dict[str, ArtifactRef | None]:
    return {
        "brief": None,
        "outline": None,
        "content_draft": None,
        "proposal": None,
        "decision": None,
        "render": None,
    }


def _empty_approval_refs() -> dict[str, ApprovalRef | None]:
    return {gate: None for gate in APPROVAL_GATES}


def _empty_readiness_basis() -> dict[str, object]:
    return {
        "scope_subject_hash": None,
        "brief_subject_hash": None,
        "required_transcripts": [],
        "speaker_resolution": {
            "mode": "not_ready",
            "refs": [],
            "waiver_subject_hash": None,
        },
        "blocking_operation_ids": [],
    }


def _artifact_hash(kind: str, artifact: object) -> str:
    payload = cast(Any, artifact).to_dict()
    schema_version = cast(int, payload["schema_version"])
    if kind in {"proposal", "decision"}:
        payload = dict(payload)
        payload.pop("created_at")
    return subject_content_hash(kind, schema_version, payload)


def _artifact_ref(kind: str, artifact: object) -> ArtifactRef:
    payload = cast(Any, artifact).to_dict()
    artifact_id = {
        "brief": "brief_id",
        "content_draft": "content_draft_id",
        "proposal": "proposal_id",
        "decision": "edit_version_id",
    }[kind]
    return ArtifactRef(
        artifact_id=cast(str, payload[artifact_id]),
        schema_version=cast(int, payload["schema_version"]),
        content_hash=_artifact_hash(kind, artifact),
    )


def _subject(kind: str, ref: ArtifactRef) -> SubjectRef:
    return SubjectRef(kind, ref.artifact_id, ref.schema_version, ref.content_hash)


def _approval_ref(approval: ApprovalRecord) -> ApprovalRef:
    return ApprovalRef(
        approval.approval_id,
        1,
        canonical_sha256_v1(approval.to_dict()),
    )


def _receipt_ref(receipt: ActionReceipt) -> ReceiptRef:
    return ReceiptRef(
        receipt.action_id,
        1,
        canonical_sha256_v1(receipt.to_dict()),
    )


def _run_with_receipt(run: WorkflowRun, receipt: ActionReceipt) -> WorkflowRun:
    return replace(run, last_receipt_ref=_receipt_ref(receipt))


def _read_transcript(
    project_path: Path, source_id: str, transcript_version_id: str
) -> TimedTranscript:
    payload = read_json_object(
        project_path
        / "transcripts"
        / source_id
        / f"{transcript_version_id}.json",
        description="workflow Timed Transcript",
    )
    transcript = TimedTranscript.from_dict(payload)
    if (
        transcript.source_id != source_id
        or transcript.transcript_version_id != transcript_version_id
    ):
        raise _facade_error(
            "workflow_subject_mismatch",
            "Timed Transcript identity does not match its source binding",
        )
    return transcript


def _transcript_hash(transcript: TimedTranscript) -> str:
    return subject_content_hash(
        "timed_transcript", transcript.schema_version, transcript.to_dict()
    )


def _output_settings(project: Project) -> dict[str, object]:
    return {
        key: project.settings[key]
        for key in (
            "timebase",
            "frame_rate",
            "width",
            "height",
            "audio_sample_rate",
        )
    }


def _output_settings_hash(project: Project) -> str:
    return canonical_sha256_v1(_output_settings(project))


def _source_snapshot(source: SourceAsset) -> dict[str, object]:
    return {
        "source_id": source.source_id,
        "import_mode": source.import_mode.value,
        "fingerprint": source.fingerprint.to_dict(),
        "display_name": source.display_name,
        "tags": list(source.tags),
        "note": source.note,
    }


def _source_snapshot_hash(source: SourceAsset) -> str:
    return canonical_sha256_v1(_source_snapshot(source))


def _source_by_id(project: Project, source_id: str) -> SourceAsset:
    matches = [source for source in project.sources if source.source_id == source_id]
    if len(matches) != 1:
        raise _facade_error(
            "workflow_subject_mismatch", f"Source {source_id!r} is not current"
        )
    return matches[0]


def _derive_multicam_setup(
    project: Project,
    run: WorkflowRun,
    declaration: MulticamSetupDeclaration,
    authorizations: tuple[ScopeAuthorization, ...],
    bindings: tuple[WorkflowBinding, ...],
) -> MulticamSetup:
    authorization_ids = {authorization.source_id for authorization in authorizations}
    binding_ids = {binding.source_id for binding in bindings}
    main_source_ids = set(declaration.main_camera.ordered_source_ids)
    if not main_source_ids <= authorization_ids or not main_source_ids <= binding_ids:
        raise _facade_error(
            "workflow_subject_mismatch",
            "Multicam Setup main camera is outside the requested scope",
        )
    source_ids = [
        *declaration.main_camera.ordered_source_ids,
        *(
            source_id
            for camera in declaration.auxiliary_cameras
            for source_id in camera.ordered_source_ids
        ),
    ]
    snapshots = tuple(_source_snapshot(_source_by_id(project, source_id)) for source_id in source_ids)
    source_snapshot_hash = canonical_sha256_v1(list(snapshots))
    identity = {
        "schema_version": declaration.schema_version,
        "project_id": project.project_id,
        "workflow_run_id": run.run_id,
        "main_camera": declaration.main_camera.to_dict(),
        "auxiliary_cameras": [
            camera.to_dict() for camera in declaration.auxiliary_cameras
        ],
        "source_pairs": [pair.to_dict() for pair in declaration.source_pairs],
        "asr_scope": [authorization.to_dict() for authorization in authorizations],
        "source_snapshots": list(snapshots),
        "source_snapshot_hash": source_snapshot_hash,
    }
    return MulticamSetup(
        schema_version=declaration.schema_version,
        setup_id=f"mcs_{canonical_sha256_v1(identity)[:32]}",
        project_id=project.project_id,
        workflow_run_id=run.run_id,
        main_camera=declaration.main_camera,
        auxiliary_cameras=declaration.auxiliary_cameras,
        source_pairs=declaration.source_pairs,
        asr_scope=authorizations,
        source_snapshots=snapshots,
        source_snapshot_hash=source_snapshot_hash,
    )


def _validate_multicam_setup(
    project: Project,
    run: WorkflowRun,
    setup: MulticamSetup,
    authorizations: tuple[ScopeAuthorization, ...],
    *,
    ordered_bindings: tuple[WorkflowBinding, ...] | None = None,
) -> None:
    if setup.project_id != project.project_id or setup.project_id != run.project_id:
        raise _facade_error(
            "workflow_subject_mismatch",
            "Multicam Setup is outside the current Project",
        )
    if setup.workflow_run_id != run.run_id:
        raise _facade_error(
            "workflow_subject_mismatch",
            "Multicam Setup is outside the current WorkflowRun",
        )
    if setup.asr_scope != authorizations:
        raise _facade_error(
            "workflow_subject_mismatch",
            "Multicam Setup ASR scope is not the confirmed scope projection",
        )
    binding_ids = {
        binding.source_id
        for binding in (run.ordered_bindings if ordered_bindings is None else ordered_bindings)
    }
    authorization_ids = {authorization.source_id for authorization in authorizations}
    main_source_ids = set(setup.main_camera.ordered_source_ids)
    if not main_source_ids <= binding_ids or not main_source_ids <= authorization_ids:
        raise _facade_error(
            "workflow_subject_mismatch",
            "Multicam Setup main camera is outside the current scope",
        )
    for snapshot in setup.source_snapshots:
        source_id = cast(str, snapshot["source_id"])
        try:
            current_snapshot = _source_snapshot(_source_by_id(project, source_id))
        except WorkflowError as error:
            raise _facade_error(
                "workflow_stale",
                "Multicam Setup references a Source that is no longer current",
            ) from error
        if current_snapshot != snapshot:
            raise _facade_error(
                "workflow_stale",
                f"Multicam Setup Source snapshot is stale for {source_id!r}",
            )


def _scope_projection(
    project: Project,
    authorizations: tuple[ScopeAuthorization, ...],
    multicam_setup: MulticamSetup | None = None,
) -> dict[str, object]:
    sources = [_source_by_id(project, item.source_id) for item in authorizations]
    projection: dict[str, object] = {
        "project_id": project.project_id,
        "ordered_source_snapshots": [_source_snapshot(source) for source in sources],
        "output_settings": _output_settings(project),
        "ordered_authorizations": [item.to_dict() for item in authorizations],
    }
    if multicam_setup is not None:
        projection["multicam_setup"] = multicam_setup.to_dict()
    return projection


def _scope_dependency(
    project: Project,
    authorizations: tuple[ScopeAuthorization, ...],
    multicam_setup: MulticamSetup | None = None,
) -> str:
    dependency: dict[str, object] = {
        "project_id": project.project_id,
        "ordered_sources": [
            {
                "source_id": item.source_id,
                "source_snapshot_hash": _source_snapshot_hash(
                    _source_by_id(project, item.source_id)
                ),
            }
            for item in authorizations
        ],
        "output_settings_hash": _output_settings_hash(project),
    }
    if multicam_setup is not None:
        dependency["multicam_setup"] = multicam_setup.to_dict()
    return dependency_bundle_hash("scope", dependency)


def _first_decision_published(store: WorkflowStore, run: WorkflowRun) -> bool:
    if not store.receipts_path.exists():
        return False
    for path in sorted(store.receipts_path.iterdir(), key=lambda entry: entry.name):
        if path.suffix != ".json":
            continue
        receipt = store._read_object(
            path, ActionReceipt.from_dict, description="ActionReceipt"
        )
        if (
            receipt.action_id != path.stem
            or receipt.project_id != run.project_id
        ):
            raise _facade_error(
                "workflow_integrity_error",
                "historical ActionReceipt identity does not match its controlled path",
            )
        if receipt.run_id == run.run_id and receipt.action == "adopt_roughcut":
            return True
    return False


def _scope_basis_projection(
    project_path: Path,
    project: Project,
    run: WorkflowRun,
    *,
    first_decision_published: bool,
) -> dict[str, object]:
    authorizations = {item.source_id: item for item in run.scope_authorizations}

    def active(source_id: str) -> dict[str, object] | None:
        transcript_id = project.active_transcript_versions.get(source_id)
        if transcript_id is None:
            return None
        transcript = _read_transcript(project_path, source_id, transcript_id)
        return {
            "transcript_version_id": transcript_id,
            "schema_version": 1,
            "content_hash": _transcript_hash(transcript),
        }

    return {
        "basis_schema": 1,
        "basis_kind": "scope_confirmation",
        "project_id": project.project_id,
        "run_id": run.run_id,
        "current_run_scope": [
            {
                "source_id": binding.source_id,
                "transcript_version_id": binding.transcript_version_id,
                "transcript_content_hash": binding.transcript_content_hash,
                "authorization": (
                    None
                    if binding.source_id not in authorizations
                    else {
                        "transcribe": authorizations[binding.source_id].transcribe,
                        "speaker_diarization": authorizations[
                            binding.source_id
                        ].speaker_diarization,
                    }
                ),
            }
            for binding in run.ordered_bindings
        ],
        "selectable_project_sources": [
            {
                "source_id": source.source_id,
                "source_snapshot_hash": _source_snapshot_hash(source),
                "active_transcript": active(source.source_id),
            }
            for source in project.sources
        ],
        "output_settings_hash": _output_settings_hash(project),
        "first_decision_published": first_decision_published,
    }


def _basis_id(kind: str, projection: dict[str, object]) -> str:
    return f"wfb_{kind}_{canonical_sha256_v1(projection)}"


def _speaker_facts(
    project_path: Path,
    project: Project,
    run: WorkflowRun,
    waived: set[tuple[str, str, str]] | None = None,
) -> tuple[dict[str, object], list[dict[str, str]]]:
    waived = waived or set()
    mappings = {mapping.identity_key: mapping for mapping in project.speaker_maps}
    refs: list[dict[str, object]] = []
    eligible: list[dict[str, str]] = []
    found_speaker = False
    all_resolved = True
    for binding in run.ordered_bindings:
        if binding.transcript_version_id is None:
            all_resolved = False
            continue
        transcript = _read_transcript(
            project_path, binding.source_id, binding.transcript_version_id
        )
        seen: set[str] = set()
        for segment in transcript.segments:
            local_id = segment.local_speaker_id
            if local_id is None or local_id in seen:
                continue
            seen.add(local_id)
            found_speaker = True
            key = (binding.source_id, binding.transcript_version_id, local_id)
            mapping = mappings.get(key)
            if mapping is not None:
                refs.append(
                    {
                        "source_id": binding.source_id,
                        "transcript_version_id": binding.transcript_version_id,
                        "local_speaker_id": local_id,
                        "resolution": "mapped",
                        "person_id": mapping.person_id,
                    }
                )
            elif key in waived:
                refs.append(
                    {
                        "source_id": binding.source_id,
                        "transcript_version_id": binding.transcript_version_id,
                        "local_speaker_id": local_id,
                        "resolution": "waived",
                        "person_id": None,
                    }
                )
            else:
                all_resolved = False
                eligible.append(
                    {
                        "source_id": binding.source_id,
                        "transcript_version_id": binding.transcript_version_id,
                        "local_speaker_id": local_id,
                    }
                )
    if not found_speaker and all(
        binding.transcript_version_id is not None for binding in run.ordered_bindings
    ):
        mode = "no_speakers"
    elif all_resolved and any(ref["resolution"] == "waived" for ref in refs):
        mode = "waived"
    elif all_resolved and refs:
        mode = "all_mapped"
    else:
        mode = "not_ready"
    waiver_subject_hash = (
        canonical_sha256_v1(
            [
                {
                    "source_id": ref["source_id"],
                    "transcript_version_id": ref["transcript_version_id"],
                    "local_speaker_id": ref["local_speaker_id"],
                }
                for ref in refs
                if ref["resolution"] == "waived"
            ]
        )
        if mode == "waived"
        else None
    )
    return (
        {
            "mode": mode,
            "refs": refs,
            "waiver_subject_hash": waiver_subject_hash,
        },
        eligible,
    )


def _brief_basis_projection(
    project_path: Path,
    project: Project,
    run: WorkflowRun,
    scope_hash: str,
) -> tuple[dict[str, object], list[dict[str, str]]]:
    speaker, eligible = _speaker_facts(project_path, project, run)
    del speaker
    projection = {
        "basis_schema": 1,
        "basis_kind": "brief_confirmation",
        "project_id": project.project_id,
        "run_id": run.run_id,
        "scope_subject_hash": scope_hash,
        "ordered_sources": [
            {
                "source_id": binding.source_id,
                "active_transcript": (
                    None
                    if binding.transcript_version_id is None
                    else {
                        "transcript_version_id": binding.transcript_version_id,
                        "schema_version": 1,
                        "content_hash": binding.transcript_content_hash,
                    }
                ),
            }
            for binding in run.ordered_bindings
        ],
        "output_settings_hash": _output_settings_hash(project),
        "eligible_speaker_waivers": eligible,
    }
    return projection, eligible


def _brief_dependency(project: Project, run: WorkflowRun, scope_hash: str) -> str:
    return dependency_bundle_hash(
        "brief",
        {
            "scope_subject_hash": scope_hash,
            "ordered_source_ids": [
                binding.source_id for binding in run.ordered_bindings
            ],
            "output_settings_hash": _output_settings_hash(project),
        },
    )


def _required_transcripts(run: WorkflowRun) -> list[dict[str, object]]:
    return [
        {
            "source_id": binding.source_id,
            "transcript_version_id": binding.transcript_version_id,
            "schema_version": 1,
            "content_hash": binding.transcript_content_hash,
        }
        for binding in run.ordered_bindings
        if binding.transcript_version_id is not None
    ]


def _read_brief(project_path: Path, ref: ArtifactRef) -> EditBrief:
    payload = read_json_object(
        project_path / "briefs" / f"{ref.artifact_id}.json",
        description="workflow Brief",
    )
    brief = EditBrief.from_dict(payload)
    if _artifact_ref("brief", brief) != ref:
        raise _facade_error(
            "workflow_subject_mismatch", "Brief ref does not match stored Brief"
        )
    return brief


def _read_proposal(
    project_path: Path, ref: ArtifactRef
) -> EditProposal | MultiSourceEditProposal:
    payload = read_json_object(
        project_path / "proposals" / f"{ref.artifact_id}.json",
        description="workflow Proposal",
    )
    if payload.get("schema_version") == 1:
        proposal: EditProposal | MultiSourceEditProposal = EditProposal.from_dict(payload)
    elif payload.get("schema_version") == 2:
        proposal = MultiSourceEditProposal.from_dict(payload)
    else:
        raise _facade_error(
            "workflow_integrity_error", "Proposal has an unknown schema"
        )
    if _artifact_ref("proposal", proposal) != ref:
        raise _facade_error(
            "workflow_subject_mismatch", "Proposal ref does not match stored Proposal"
        )
    return proposal


def validate_workflow_proposal_ref(project_path: Path, ref: ArtifactRef) -> None:
    """Validate one WorkflowRun Proposal ref against its stored artifact."""

    try:
        _read_proposal(project_path, ref)
    except WorkflowError:
        raise
    except Exception as error:
        raise _facade_error(
            "workflow_subject_mismatch",
            f"stored Proposal does not match the WorkflowRun exact ref: {error}",
        ) from error


def _read_decision(
    project_path: Path, ref: ArtifactRef
) -> EditDecision | MultiSourceEditDecision:
    payload = read_json_object(
        project_path / "edits" / f"{ref.artifact_id}.json",
        description="workflow Decision",
    )
    if payload.get("schema_version") == 1:
        decision: EditDecision | MultiSourceEditDecision = EditDecision.from_dict(payload)
    elif payload.get("schema_version") == 2:
        decision = MultiSourceEditDecision.from_dict(payload)
    else:
        raise _facade_error(
            "workflow_integrity_error", "Decision has an unknown schema"
        )
    if _artifact_ref("decision", decision) != ref:
        raise _facade_error(
            "workflow_subject_mismatch", "Decision ref does not match stored Decision"
        )
    return decision


def _read_draft_ref(project_path: Path, ref: ArtifactRef) -> ContentDraft:
    draft = _read_draft(project_path, ref.artifact_id, validate_parent=False)
    if _artifact_ref("content_draft", draft) != ref:
        raise _facade_error(
            "workflow_subject_mismatch",
            "Content Draft ref does not match stored immutable Draft",
        )
    return draft


def _ref_matches(payload: dict[str, object], ref: ArtifactRef) -> bool:
    return payload == {
        "artifact_id": ref.artifact_id,
        "schema_version": ref.schema_version,
        "content_hash": ref.content_hash,
    }


def _input_ref(ref: ArtifactRef) -> dict[str, object]:
    return {
        "artifact_id": ref.artifact_id,
        "schema_version": ref.schema_version,
        "content_hash": ref.content_hash,
    }


def _approval_status(
    store: WorkflowStore,
    run: WorkflowRun,
    gate: str,
    subject: SubjectRef | None,
    dependency_hash: str | None,
) -> Literal["missing", "current", "stale"]:
    ref = run.approval_refs[gate]
    if ref is None:
        return "missing"
    record = store.read_approval(ref.approval_id, run_id=run.run_id)
    if canonical_sha256_v1(record.to_dict()) != ref.record_hash:
        raise _facade_error(
            "workflow_integrity_error", f"{gate} ApprovalRecord hash is invalid"
        )
    if subject is None or dependency_hash is None:
        return "stale"
    return record.effective_status(subject, dependency_hash)


def _scope_current_facts(
    project: Project, run: WorkflowRun
) -> tuple[SubjectRef | None, str | None]:
    if not run.scope_authorizations:
        return None, None
    if run.multicam_setup is not None:
        _validate_multicam_setup(
            project, run, run.multicam_setup, run.scope_authorizations
        )
    projection = _scope_projection(
        project, run.scope_authorizations, run.multicam_setup
    )
    content_hash = subject_content_hash("scope_snapshot", 1, projection)
    return (
        SubjectRef("scope_snapshot", f"scope_{content_hash[:16]}", 1, content_hash),
        _scope_dependency(project, run.scope_authorizations, run.multicam_setup),
    )


def _brief_current_facts(
    project_path: Path,
    project: Project,
    run: WorkflowRun,
    scope_subject: SubjectRef | None,
) -> tuple[SubjectRef | None, str | None]:
    ref = run.artifact_refs["brief"]
    if ref is None or scope_subject is None:
        return None, None
    brief = _read_brief(project_path, ref)
    return (
        _subject("brief", _artifact_ref("brief", brief)),
        _brief_dependency(project, run, scope_subject.content_hash),
    )


def _outline_dependency(
    run: WorkflowRun, speaker_resolution: dict[str, object] | None = None
) -> str | None:
    outline = run.artifact_refs["outline"]
    brief = run.artifact_refs["brief"]
    scope_hash = run.readiness_basis["scope_subject_hash"]
    if outline is None or brief is None or scope_hash is None:
        return None
    speaker = (
        run.readiness_basis["speaker_resolution"]
        if speaker_resolution is None
        else speaker_resolution
    )
    return dependency_bundle_hash(
        "outline",
        {
            "scope_subject_hash": scope_hash,
            "brief_ref": brief.to_dict(),
            "ordered_bindings": [binding.to_dict() for binding in run.ordered_bindings],
            "speaker_resolution": speaker,
            "required_transcripts_ready_basis_hash": canonical_sha256_v1(
                _required_transcripts(run)
            ),
            "speaker_resolution_ready_basis_hash": canonical_sha256_v1(speaker),
        },
    )


def _draft_ancestry(
    project_path: Path, anchor: ArtifactRef, candidate: ArtifactRef
) -> tuple[ArtifactRef, ...]:
    current = _read_draft_ref(project_path, candidate)
    result = [_artifact_ref("content_draft", current)]
    seen = {current.content_draft_id}
    while current.content_draft_id != anchor.artifact_id:
        parent_id = current.parent_draft_id
        if parent_id is None or parent_id in seen:
            raise _facade_error(
                "workflow_subject_mismatch",
                "Content Draft candidate is not a descendant of the workflow anchor",
            )
        seen.add(parent_id)
        current = _read_draft(project_path, parent_id, validate_parent=False)
        result.append(_artifact_ref("content_draft", current))
    if result[-1] != anchor:
        raise _facade_error(
            "workflow_subject_mismatch",
            "Content Draft ancestry does not end at the exact workflow anchor",
        )
    return tuple(result)


def _draft_dependency(
    run: WorkflowRun,
    candidate: ArtifactRef,
    ancestry: tuple[ArtifactRef, ...],
    *,
    context_hash: str,
    confirmed_child: ArtifactRef | None = None,
) -> str | None:
    outline = run.artifact_refs["outline"]
    brief = run.artifact_refs["brief"]
    anchor = run.artifact_refs["content_draft"]
    if outline is None or brief is None or anchor is None:
        return None
    dependencies: dict[str, object] = {
        "outline_content_hash": outline.content_hash,
        "brief_ref": brief.to_dict(),
        "ordered_bindings": [binding.to_dict() for binding in run.ordered_bindings],
        "speaker_resolution": run.readiness_basis["speaker_resolution"],
        "context_hash": context_hash,
        "workflow_anchor_ref": anchor.to_dict(),
        "candidate_ref": candidate.to_dict(),
        "ancestry_refs": [ref.to_dict() for ref in ancestry],
    }
    if confirmed_child is not None:
        dependencies["prepared_confirmed_pair"] = {
            "unconfirmed_ref": candidate.to_dict(),
            "confirmed_ref": confirmed_child.to_dict(),
            "parent_draft_id": candidate.artifact_id,
        }
    return dependency_bundle_hash("draft", dependencies)


def _roughcut_dependency(run: WorkflowRun, proposal: ArtifactRef) -> str | None:
    draft_approval = run.approval_refs["draft"]
    draft = run.artifact_refs["content_draft"]
    outline = run.artifact_refs["outline"]
    brief = run.artifact_refs["brief"]
    if any(item is None for item in (draft_approval, draft, outline, brief)):
        return None
    return dependency_bundle_hash(
        "roughcut",
        {
            "draft_approval_hash": cast(ApprovalRef, draft_approval).record_hash,
            "confirmed_content_draft_ref": cast(ArtifactRef, draft).to_dict(),
            "outline_content_hash": cast(ArtifactRef, outline).content_hash,
            "brief_content_hash": cast(ArtifactRef, brief).content_hash,
            "ordered_bindings": [binding.to_dict() for binding in run.ordered_bindings],
            "proposal_ref": proposal.to_dict(),
            "review_basis_id": f"review_{proposal.content_hash}",
        },
    )


def _export_snapshot(
    project: Project,
    decision: EditDecision | MultiSourceEditDecision,
) -> dict[str, object]:
    proposal = decision.proposal_snapshot
    used_source_ids: list[str] = []
    for clip in proposal.clips:
        if clip.source_id not in used_source_ids:
            used_source_ids.append(clip.source_id)
    total_duration = sum(clip.duration_ticks for clip in proposal.clips)
    snapshot_basis = {
        "active_decision_ref": _artifact_ref("decision", decision).to_dict(),
        "ordered_clips": [
            {
                "clip_id": clip.clip_id,
                "source_id": clip.source_id,
                "source_in_ticks": clip.source_in_ticks,
                "source_out_ticks": clip.source_out_ticks,
            }
            for clip in proposal.clips
        ],
        "clip_count": len(proposal.clips),
        "total_duration_ticks": total_duration,
        "output_settings": _output_settings(project),
        "ordered_source_fingerprint_hashes": [
            {
                "source_id": source_id,
                "fingerprint_hash": canonical_sha256_v1(
                    _source_by_id(project, source_id).fingerprint.to_dict()
                ),
            }
            for source_id in used_source_ids
        ],
    }
    render_id = f"render_{canonical_sha256_v1(snapshot_basis)[:32]}"
    return {
        **snapshot_basis,
        "output_target": f"renders/{render_id}.mp4",
    }


def _export_ref(
    project: Project, decision: EditDecision | MultiSourceEditDecision
) -> ArtifactRef:
    snapshot = _export_snapshot(project, decision)
    content_hash = subject_content_hash("export_snapshot", 1, snapshot)
    return ArtifactRef(f"export_{content_hash[:16]}", 1, content_hash)


def _export_dependency(
    project: Project, run: WorkflowRun, export_ref: ArtifactRef
) -> str | None:
    roughcut = run.approval_refs["roughcut"]
    decision = run.artifact_refs["decision"]
    if roughcut is None or decision is None:
        return None
    source_ids: list[str] = []
    for binding in run.ordered_bindings:
        source_ids.append(binding.source_id)
    return dependency_bundle_hash(
        "export",
        {
            "roughcut_approval_hash": roughcut.record_hash,
            "decision_ref": decision.to_dict(),
            "ordered_bindings": [binding.to_dict() for binding in run.ordered_bindings],
            "output_settings_hash": _output_settings_hash(project),
            "ordered_source_fingerprint_hashes": [
                {
                    "source_id": source_id,
                    "fingerprint_hash": canonical_sha256_v1(
                        _source_by_id(project, source_id).fingerprint.to_dict()
                    ),
                }
                for source_id in source_ids
            ],
            "export_subject_hash": export_ref.content_hash,
        },
    )


def _sync_bindings_locked(
    project_path: Path,
    project: Project,
    run: WorkflowRun,
    store: WorkflowStore,
    *,
    only_source_id: str | None = None,
) -> tuple[WorkflowRun, tuple[str, ...]]:
    if run.lifecycle != "active":
        return run, ()
    changed: list[str] = []
    bindings: list[WorkflowBinding] = []
    for binding in run.ordered_bindings:
        if only_source_id is not None and binding.source_id != only_source_id:
            bindings.append(binding)
            continue
        active_id = project.active_transcript_versions.get(binding.source_id)
        if active_id is None:
            bindings.append(binding)
            continue
        transcript = _read_transcript(project_path, binding.source_id, active_id)
        active_hash = _transcript_hash(transcript)
        current = WorkflowBinding(binding.source_id, active_id, active_hash)
        if current != binding:
            changed.append(binding.source_id)
        bindings.append(current)
    if not changed:
        return run, ()
    readiness = dict(run.readiness_basis)
    readiness["required_transcripts"] = [
        {
            "source_id": binding.source_id,
            "transcript_version_id": binding.transcript_version_id,
            "schema_version": 1,
            "content_hash": binding.transcript_content_hash,
        }
        for binding in bindings
        if binding.transcript_version_id is not None
    ]
    repaired = replace(
        run,
        ordered_bindings=tuple(bindings),
        readiness_basis=readiness,
        updated_at=_timestamp(),
    )
    try:
        store.write_run(
            repaired, expected_run_hash=canonical_sha256_v1(run.to_dict())
        )
    except Exception as error:
        raise _facade_error(
            "workflow_binding_sync_failed",
            f"Roughcut core could not synchronize active Transcript bindings: {error}",
        ) from error
    return repaired, tuple(changed)


def _synchronize_transcript_binding_locked(
    project_path: Path,
    store: WorkflowStore,
    run: WorkflowRun,
    source_id: str,
    transcript_version_id: str,
) -> WorkflowRun:
    current = ProjectStore(project_path).load()
    if current.active_transcript_versions.get(source_id) != transcript_version_id:
        raise _facade_error(
            "workflow_binding_sync_failed",
            "Roughcut workflow façade could not synchronize the active Transcript: "
            "Project active Transcript does not match the committed Transcript",
        )
    try:
        repaired, _ = _sync_bindings_locked(
            project_path,
            current,
            run,
            store,
            only_source_id=source_id,
        )
    except Exception as error:
        raise _facade_error(
            "workflow_binding_sync_failed",
            "Roughcut workflow façade could not synchronize the active "
            f"Transcript binding: {error}",
        ) from error
    binding = next(
        (item for item in repaired.ordered_bindings if item.source_id == source_id),
        None,
    )
    if (
        binding is None
        or binding.transcript_version_id != transcript_version_id
        or binding.transcript_content_hash is None
    ):
        raise _facade_error(
            "workflow_binding_sync_failed",
            "Roughcut workflow façade could not synchronize the committed "
            "Transcript to the exact WorkflowRun binding",
        )
    return repaired


def synchronize_active_workflow_transcript_binding(
    project: str | Path,
    source_id: str,
    transcript_version_id: str,
) -> WorkflowRun | None:
    """Converge one committed active Transcript when an active run owns its source."""

    project_path = Path(project)
    store = WorkflowStore(project_path)
    with store.write_lock():
        store.recover_pending()
        run = store.active_run()
        if run is None or not any(
            binding.source_id == source_id for binding in run.ordered_bindings
        ):
            return run
        return _synchronize_transcript_binding_locked(
            project_path, store, run, source_id, transcript_version_id
        )


def synchronize_workflow_transcript_binding(
    project: str | Path,
    run_id: str,
    source_id: str,
    transcript_version_id: str,
) -> WorkflowRun:
    """Converge one authorized ASR result into its active WorkflowRun binding."""

    project_path = Path(project)
    store = WorkflowStore(project_path)
    with store.write_lock():
        store.recover_pending()
        run = store.read_run(run_id)
        return _synchronize_transcript_binding_locked(
            project_path, store, run, source_id, transcript_version_id
        )


def workflow_start(
    project: str | Path, run_id: str, ordered_source_ids: list[str]
) -> WorkflowFacadeResult:
    try:
        validate_safe_id(run_id, field="run_id")
    except WorkflowError as error:
        raise _facade_error("workflow_action_invalid", "run_id is invalid") from error
    if not isinstance(ordered_source_ids, list):
        raise _facade_error(
            "workflow_action_invalid", "ordered_source_ids must be an array"
        )
    parsed_ids: list[str] = []
    for source_id in ordered_source_ids:
        try:
            parsed_ids.append(validate_safe_id(source_id, field="source_id"))
        except WorkflowError as error:
            raise _facade_error(
                "workflow_action_invalid", "ordered_source_ids contains an invalid ID"
            ) from error
    if len(parsed_ids) != len(set(parsed_ids)):
        raise _facade_error(
            "workflow_action_invalid", "ordered_source_ids contains duplicates"
        )
    project_path = Path(project)
    store = WorkflowStore(project_path)
    with store.write_lock():
        store.recover_pending()
        if store.active_run() is not None:
            raise _facade_error(
                "workflow_run_conflict", "Project already has an active WorkflowRun"
            )
        current = ProjectStore(project_path).load()
        project_order = [source.source_id for source in current.sources]
        if [source_id for source_id in project_order if source_id in parsed_ids] != parsed_ids:
            raise _facade_error(
                "workflow_subject_mismatch",
                "ordered_source_ids are unknown or do not follow Project order",
            )
        now = _timestamp()
        bindings: list[WorkflowBinding] = []
        for source_id in parsed_ids:
            transcript_id = current.active_transcript_versions.get(source_id)
            if transcript_id is None:
                bindings.append(WorkflowBinding(source_id, None, None))
            else:
                transcript = _read_transcript(project_path, source_id, transcript_id)
                bindings.append(
                    WorkflowBinding(source_id, transcript_id, _transcript_hash(transcript))
                )
        required_transcripts = [
            {
                "source_id": binding.source_id,
                "transcript_version_id": binding.transcript_version_id,
                "schema_version": 1,
                "content_hash": binding.transcript_content_hash,
            }
            for binding in bindings
            if binding.transcript_version_id is not None
        ]
        run = WorkflowRun(
            schema_version=1,
            run_id=run_id,
            project_id=current.project_id,
            stage="scope_review",
            lifecycle="active",
            created_at=now,
            updated_at=now,
            ordered_bindings=tuple(bindings),
            scope_authorizations=(),
            artifact_refs=_empty_artifact_refs(),
            readiness_basis={
                **_empty_readiness_basis(),
                "required_transcripts": required_transcripts,
            },
            approval_refs=_empty_approval_refs(),
            last_receipt_ref=None,
        )
        store.write_run(run)
    status = _status_internal(project_path, run_id)
    return WorkflowFacadeResult(
        status["workflow_run_model"], None, _validated_public_status(status)
    )


def _status_internal(
    project: str | Path,
    run_id: str | None,
) -> dict[str, Any]:
    project_path = Path(project)
    store = WorkflowStore(project_path)
    with store.write_lock():
        recoveries = store.recover_pending()
        if run_id is None:
            run = store.active_run()
            if run is None:
                raise _facade_error(
                    "workflow_required", "Project has no active WorkflowRun"
                )
        else:
            try:
                validate_safe_id(run_id, field="run_id")
            except WorkflowError as error:
                raise _facade_error(
                    "workflow_action_invalid", "run_id is invalid"
                ) from error
            run = store.read_run(run_id)
        current = ProjectStore(project_path).load()
        run, repaired_ids = _sync_bindings_locked(
            project_path, current, run, store
        )
        first_decision = _first_decision_published(store, run)
        scope_subject, scope_dependency = _scope_current_facts(current, run)
        scope_status = _approval_status(
            store, run, "scope", scope_subject, scope_dependency
        )
        brief_subject, brief_dependency = _brief_current_facts(
            project_path, current, run, scope_subject
        )
        brief_status = _approval_status(
            store, run, "brief", brief_subject, brief_dependency
        )
        required_ready = bool(run.ordered_bindings) and all(
            binding.transcript_version_id is not None
            and current.active_transcript_versions.get(binding.source_id)
            == binding.transcript_version_id
            for binding in run.ordered_bindings
        )
        persisted_speaker = cast(
            dict[str, object], run.readiness_basis["speaker_resolution"]
        )
        waived = {
            (
                cast(str, ref["source_id"]),
                cast(str, ref["transcript_version_id"]),
                cast(str, ref["local_speaker_id"]),
            )
            for ref in cast(list[dict[str, object]], persisted_speaker["refs"])
            if ref["resolution"] == "waived"
        }
        speaker, eligible = _speaker_facts(
            project_path, current, run, waived=waived
        )
        speaker_ready = speaker["mode"] in {"no_speakers", "all_mapped", "waived"}
        approval_statuses: dict[str, str] = {
            "scope": scope_status,
            "brief": brief_status,
        }
        outline = run.artifact_refs["outline"]
        outline_subject = (
            None
            if outline is None
            else SubjectRef(
                "outline_snapshot",
                outline.artifact_id,
                outline.schema_version,
                outline.content_hash,
            )
        )
        approval_statuses["outline"] = _approval_status(
            store,
            run,
            "outline",
            outline_subject,
            _outline_dependency(run, speaker),
        )
        draft = run.artifact_refs["content_draft"]
        draft_subject = (
            None
            if draft is None
            else SubjectRef(
                "content_draft",
                draft.artifact_id,
                draft.schema_version,
                draft.content_hash,
            )
        )
        draft_dependency = None
        if draft is not None:
            ancestry = _draft_ancestry(project_path, draft, draft)
            draft_dependency = _draft_dependency(
                run,
                draft,
                ancestry,
                context_hash=_read_draft_ref(project_path, draft).context_hash,
            )
            if run.stage in {"roughcut_review", "export_review", "exporting"}:
                record_ref = run.approval_refs["draft"]
                if record_ref is not None:
                    record = store.read_approval(
                        record_ref.approval_id, run_id=run.run_id
                    )
                    confirmed = _read_draft_ref(project_path, draft)
                    exact_bindings = tuple(
                        (binding.source_id, binding.transcript_version_id)
                        for binding in run.ordered_bindings
                    )
                    draft_bindings = tuple(
                        (binding.source_id, binding.transcript_version_id)
                        for binding in confirmed.source_bindings
                    )
                    pair_is_current = (
                        confirmed.confirmed_by_user
                        and confirmed.parent_draft_id == record.subject.artifact_id
                        and current.active_content_draft_id == confirmed.content_draft_id
                        and exact_bindings == draft_bindings
                        and approval_statuses["outline"] == "current"
                        and approval_statuses["brief"] == "current"
                    )
                    if pair_is_current:
                        draft_subject = record.subject
                        draft_dependency = record.dependency_hash
        approval_statuses["draft"] = _approval_status(
            store, run, "draft", draft_subject, draft_dependency
        )
        proposal = run.artifact_refs["proposal"]
        proposal_subject = (
            None
            if proposal is None
            else SubjectRef(
                "proposal",
                proposal.artifact_id,
                proposal.schema_version,
                proposal.content_hash,
            )
        )
        approval_statuses["roughcut"] = _approval_status(
            store,
            run,
            "roughcut",
            proposal_subject,
            None if proposal is None else _roughcut_dependency(run, proposal),
        )
        decision = run.artifact_refs["decision"]
        export_ref = None
        if decision is not None:
            export_ref = _export_ref(
                current, _read_decision(project_path, decision)
            )
        approval_statuses["export"] = _approval_status(
            store,
            run,
            "export",
            (
                None
                if export_ref is None
                else SubjectRef(
                    "export_snapshot",
                    export_ref.artifact_id,
                    1,
                    export_ref.content_hash,
                )
            ),
            (
                None
                if export_ref is None
                else _export_dependency(current, run, export_ref)
            ),
        )
        readiness = {
            "scope_approved": scope_status == "current",
            "brief_approved": brief_status == "current",
            "required_transcripts_ready": required_ready,
            "speaker_resolution_ready_or_waived": speaker_ready,
            "blocking_operation_ids": list(
                cast(list[str], run.readiness_basis["blocking_operation_ids"])
            ),
        }
        scope_basis = _scope_basis_projection(
            project_path,
            current,
            run,
            first_decision_published=first_decision,
        )
        confirmation_scope: dict[str, object] | None = None
        if (
            run.lifecycle == "active"
            and run.stage in {"scope_review", "outline_review", "draft_review"}
            and not first_decision
        ):
            confirmation_scope = {
                "basis": {"basis_id": _basis_id("scope", scope_basis)},
                "current_ordered_source_ids": [
                    binding.source_id for binding in run.ordered_bindings
                ],
                "selectable_source_ids": [
                    source.source_id for source in current.sources
                ],
            }
        confirmation_brief: dict[str, object] | None = None
        brief_basis_projection: dict[str, object] | None = None
        if (
            run.lifecycle == "active"
            and run.stage in {"scope_review", "outline_review", "draft_review"}
            and scope_status == "current"
            and scope_subject is not None
        ):
            brief_basis, eligible = _brief_basis_projection(
                project_path, current, run, scope_subject.content_hash
            )
            brief_basis_projection = brief_basis
            confirmation_brief = {
                "basis": {"basis_id": _basis_id("brief", brief_basis)},
                "eligible_speaker_waivers": eligible,
            }
        claim_state = (
            "busy" if project_export_claim_is_busy(project_path) else "idle"
        )
        if claim_state == "idle":
            _inspect_export_staging(
                store.workflow_path / "export-staging",
                project_id=current.project_id,
                run_id=run.run_id,
            )
        from roughcut.application.multicam_continuation import (
            multicam_alignment_status,
        )

        alignment_continuation = multicam_alignment_status(project_path, run)
        allowed = _allowed_actions(
            run,
            readiness,
            approval_statuses,
            first_decision=first_decision,
            claim_state=claim_state,
        )
        recovery = _recovery_status(recoveries)
        confirmed_ref = (
            draft
            if draft is not None
            and _read_draft_ref(project_path, draft).confirmed_by_user
            else None
        )
        return {
            "schema_version": 1,
            "workflow_run": run.to_dict(),
            "multicam_alignment": alignment_continuation,
            "readiness": readiness,
            "approval_statuses": approval_statuses,
            "confirmation_bases": {
                "scope": confirmation_scope,
                "brief": confirmation_brief,
            },
            "presented_subjects": {
                "outline_ref": None if outline is None else _input_ref(outline),
                "draft_anchor_ref": None if draft is None else _input_ref(draft),
                "return_subject_ref": (
                    (
                        None
                        if decision is None
                        else {
                            "kind": "decision",
                            **_input_ref(decision),
                        }
                    )
                    if run.stage == "export_review"
                    else (
                        None
                        if proposal is None
                        else {
                            "kind": "proposal",
                            **_input_ref(proposal),
                        }
                    )
                ),
                "confirmed_content_draft_ref": (
                    None if confirmed_ref is None else _input_ref(confirmed_ref)
                ),
                "proposal_ref": None if proposal is None else _input_ref(proposal),
                "export_ref": None if export_ref is None else _input_ref(export_ref),
            },
            "binding_sync": {
                "state": "repaired" if repaired_ids else "unchanged",
                "source_ids": list(repaired_ids),
            },
            "recovery": recovery,
            "transient_export_claim": {"state": claim_state},
            "allowed_actions": list(allowed),
            "next_action": _next_action(
                run,
                readiness,
                approval_statuses,
                allowed,
                eligible_speakers=bool(eligible),
                claim_state=claim_state,
            ),
            "workflow_run_model": run,
            "project_model": current,
            "scope_basis_projection": scope_basis,
            "brief_basis_projection": brief_basis_projection,
            "scope_subject": scope_subject,
            "scope_dependency": scope_dependency,
            "effective_speaker_resolution": speaker,
        }


def _public_status(status: dict[str, Any]) -> dict[str, object]:
    return {
        key: cast(object, value)
        for key, value in status.items()
        if key
        not in {
            "workflow_run_model",
            "project_model",
            "scope_basis_projection",
            "brief_basis_projection",
            "scope_subject",
            "scope_dependency",
            "effective_speaker_resolution",
        }
    }


def _validate_workflow_status(
    payload: dict[str, object],
    *,
    expected_run: WorkflowRun,
    expected_project: Project,
    scope_basis_projection: dict[str, object],
    brief_basis_projection: dict[str, object] | None,
) -> None:
    def closed(
        value: object, fields: set[str], description: str
    ) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != fields:
            raise _facade_error(
                "workflow_integrity_error",
                f"workflow_status {description} is not its closed schema",
            )
        return cast(dict[str, Any], value)

    def artifact_ref(value: object, description: str) -> None:
        item = closed(
            value,
            {"artifact_id", "schema_version", "content_hash"},
            description,
        )
        ArtifactRef.from_dict(item)

    top = closed(
        payload,
        {
            "schema_version",
            "workflow_run",
            "multicam_alignment",
            "readiness",
            "approval_statuses",
            "confirmation_bases",
            "presented_subjects",
            "binding_sync",
            "recovery",
            "transient_export_claim",
            "allowed_actions",
            "next_action",
        },
        "response",
    )
    if type(top["schema_version"]) is not int or top["schema_version"] != 1:
        raise _facade_error(
            "workflow_integrity_error",
            "workflow_status schema_version is not integer 1",
        )
    parsed_run = WorkflowRun.from_dict(top["workflow_run"])
    if parsed_run != expected_run or parsed_run.project_id != expected_project.project_id:
        raise _facade_error(
            "workflow_integrity_error",
            "workflow_status workflow_run does not match the validated current objects",
        )
    from roughcut.application.multicam_continuation import (
        validate_multicam_alignment_status,
    )

    validate_multicam_alignment_status(top["multicam_alignment"])
    readiness = closed(
        top["readiness"],
        {
            "scope_approved",
            "brief_approved",
            "required_transcripts_ready",
            "speaker_resolution_ready_or_waived",
            "blocking_operation_ids",
        },
        "readiness",
    )
    if (
        any(
            not isinstance(readiness[field], bool)
            for field in (
                "scope_approved",
                "brief_approved",
                "required_transcripts_ready",
                "speaker_resolution_ready_or_waived",
            )
        )
        or not isinstance(readiness["blocking_operation_ids"], list)
        or any(
            not isinstance(item, str)
            for item in readiness["blocking_operation_ids"]
        )
    ):
        raise _facade_error(
            "workflow_integrity_error",
            "workflow_status readiness member types are invalid",
        )
    approval_statuses = closed(
        top["approval_statuses"],
        set(APPROVAL_GATES),
        "approval_statuses",
    )
    if any(
        status not in {"missing", "current", "stale"}
        for status in approval_statuses.values()
    ):
        raise _facade_error(
            "workflow_integrity_error",
            "workflow_status approval status is invalid",
        )
    confirmation = closed(
        top["confirmation_bases"], {"scope", "brief"}, "confirmation_bases"
    )
    scope = confirmation["scope"]
    if scope is not None:
        scope_item = closed(
            scope,
            {
                "basis",
                "current_ordered_source_ids",
                "selectable_source_ids",
            },
            "confirmation_bases.scope",
        )
        basis = closed(
            scope_item["basis"], {"basis_id"}, "scope confirmation basis"
        )
        if (
            not isinstance(basis["basis_id"], str)
            or basis["basis_id"] != _basis_id("scope", scope_basis_projection)
        ):
            raise _facade_error(
                "workflow_integrity_error",
                "workflow_status scope basis_id has an invalid type, prefix, or basis",
            )
        for field in ("current_ordered_source_ids", "selectable_source_ids"):
            if not isinstance(scope_item[field], list) or any(
                not isinstance(item, str) for item in scope_item[field]
            ):
                raise _facade_error(
                    "workflow_integrity_error",
                    f"workflow_status scope {field} is invalid",
                )
            for source_id in scope_item[field]:
                validate_safe_id(source_id, field=field)
            if len(scope_item[field]) != len(set(scope_item[field])):
                raise _facade_error(
                    "workflow_integrity_error",
                    f"workflow_status scope {field} contains duplicates",
                )
        if scope_item["current_ordered_source_ids"] != [
            binding.source_id for binding in expected_run.ordered_bindings
        ]:
            raise _facade_error(
                "workflow_integrity_error",
                "workflow_status current scope does not match WorkflowRun binding order",
            )
        if scope_item["selectable_source_ids"] != [
            source.source_id for source in expected_project.sources
        ]:
            raise _facade_error(
                "workflow_integrity_error",
                "workflow_status selectable scope does not match Project source order",
            )
    brief = confirmation["brief"]
    if brief is not None:
        brief_item = closed(
            brief,
            {"basis", "eligible_speaker_waivers"},
            "confirmation_bases.brief",
        )
        basis = closed(
            brief_item["basis"], {"basis_id"}, "brief confirmation basis"
        )
        if (
            brief_basis_projection is None
            or not isinstance(basis["basis_id"], str)
            or basis["basis_id"] != _basis_id("brief", brief_basis_projection)
        ):
            raise _facade_error(
                "workflow_integrity_error",
                "workflow_status brief basis_id has an invalid type, prefix, or basis",
            )
        waivers = brief_item["eligible_speaker_waivers"]
        if not isinstance(waivers, list):
            raise _facade_error(
                "workflow_integrity_error",
                "workflow_status eligible speaker waivers is invalid",
            )
        waiver_keys: list[tuple[str, str, str]] = []
        for waiver in waivers:
            waiver_item = closed(
                waiver,
                {"source_id", "transcript_version_id", "local_speaker_id"},
                "eligible speaker waiver",
            )
            key = tuple(
                validate_safe_id(waiver_item[field], field=field)
                for field in (
                    "source_id",
                    "transcript_version_id",
                    "local_speaker_id",
                )
            )
            waiver_keys.append(cast(tuple[str, str, str], key))
        if len(waiver_keys) != len(set(waiver_keys)):
            raise _facade_error(
                "workflow_integrity_error",
                "workflow_status eligible speaker waivers contain duplicates",
            )
    presented = closed(
        top["presented_subjects"],
        {
            "outline_ref",
            "draft_anchor_ref",
            "return_subject_ref",
            "confirmed_content_draft_ref",
            "proposal_ref",
            "export_ref",
        },
        "presented_subjects",
    )
    for field in (
        "outline_ref",
        "draft_anchor_ref",
        "confirmed_content_draft_ref",
        "proposal_ref",
        "export_ref",
    ):
        if presented[field] is not None:
            artifact_ref(presented[field], f"presented_subjects.{field}")
    if presented["return_subject_ref"] is not None:
        subject = closed(
            presented["return_subject_ref"],
            {"kind", "artifact_id", "schema_version", "content_hash"},
            "presented_subjects.return_subject_ref",
        )
        if subject["kind"] not in {"proposal", "decision"}:
            raise _facade_error(
                "workflow_integrity_error",
                "workflow_status return subject kind is invalid",
            )
        artifact_ref(
            {key: subject[key] for key in ("artifact_id", "schema_version", "content_hash")},
            "presented_subjects.return_subject_ref",
        )
    binding_sync = closed(
        top["binding_sync"], {"state", "source_ids"}, "binding_sync"
    )
    if (
        binding_sync["state"] not in {"unchanged", "repaired"}
        or not isinstance(binding_sync["source_ids"], list)
        or any(not isinstance(item, str) for item in binding_sync["source_ids"])
        or (
            binding_sync["state"] == "unchanged"
            and bool(binding_sync["source_ids"])
        )
        or (
            binding_sync["state"] == "repaired"
            and not binding_sync["source_ids"]
        )
    ):
        raise _facade_error(
            "workflow_integrity_error",
            "workflow_status binding_sync is invalid",
        )
    synchronized_ids = cast(list[str], binding_sync["source_ids"])
    for source_id in synchronized_ids:
        validate_safe_id(source_id, field="binding_sync.source_id")
    if len(synchronized_ids) != len(set(synchronized_ids)):
        raise _facade_error(
            "workflow_integrity_error",
            "workflow_status binding_sync source_ids contain duplicates",
        )
    run_source_ids = [binding.source_id for binding in expected_run.ordered_bindings]
    if synchronized_ids != [
        source_id for source_id in run_source_ids if source_id in synchronized_ids
    ]:
        raise _facade_error(
            "workflow_integrity_error",
            "workflow_status binding_sync source_ids do not follow WorkflowRun order",
        )
    recovery = closed(
        top["recovery"],
        {"state", "action_id", "receipt_ref", "message_code"},
        "recovery",
    )
    recovery_state = recovery["state"]
    if recovery_state not in {"none", "rolled_back", "receipt_committed"}:
        raise _facade_error(
            "workflow_integrity_error",
            "workflow_status recovery state is invalid",
        )
    if recovery_state == "none":
        if any(
            recovery[field] is not None
            for field in ("action_id", "receipt_ref", "message_code")
        ):
            raise _facade_error(
                "workflow_integrity_error",
                "workflow_status recovery none has inconsistent members",
            )
    elif recovery_state == "rolled_back":
        validate_safe_id(recovery["action_id"], field="recovery.action_id")
        if (
            recovery["receipt_ref"] is not None
            or recovery["message_code"] != "workflow_previous_action_recovered"
        ):
            raise _facade_error(
                "workflow_integrity_error",
                "workflow_status rolled-back recovery has inconsistent members",
            )
    else:
        action_id = validate_safe_id(
            recovery["action_id"], field="recovery.action_id"
        )
        receipt_ref = ReceiptRef.from_dict(recovery["receipt_ref"])
        if (
            receipt_ref.action_id != action_id
            or recovery["message_code"] != "workflow_previous_action_committed"
        ):
            raise _facade_error(
                "workflow_integrity_error",
                "workflow_status committed recovery has inconsistent members",
            )
    claim = closed(
        top["transient_export_claim"], {"state"}, "transient_export_claim"
    )
    if claim["state"] not in {"idle", "busy"}:
        raise _facade_error(
            "workflow_integrity_error",
            "workflow_status transient export claim is invalid",
        )
    allowed = top["allowed_actions"]
    if (
        not isinstance(allowed, list)
        or any(action not in WORKFLOW_ACTIONS for action in allowed)
        or len(set(allowed)) != len(allowed)
        or allowed
        != [action for action in _ACTION_ORDER if action in set(allowed)]
    ):
        raise _facade_error(
            "workflow_integrity_error",
            "workflow_status allowed_actions is invalid",
        )
    if top["next_action"] is not None and top["next_action"] not in allowed:
        raise _facade_error(
            "workflow_integrity_error",
            "workflow_status next_action is not an allowed action",
        )


def _validated_public_status(status: dict[str, Any]) -> dict[str, object]:
    payload = _public_status(status)
    expected_payload = {
        key: cast(object, status[key])
        for key in (
            "schema_version",
            "workflow_run",
            "multicam_alignment",
            "readiness",
            "approval_statuses",
            "confirmation_bases",
            "presented_subjects",
            "binding_sync",
            "recovery",
            "transient_export_claim",
            "allowed_actions",
            "next_action",
        )
    }
    try:
        _validate_workflow_status(
            payload,
            expected_run=status["workflow_run_model"],
            expected_project=status["project_model"],
            scope_basis_projection=status["scope_basis_projection"],
            brief_basis_projection=status["brief_basis_projection"],
        )
        if payload != expected_payload:
            raise _facade_error(
                "workflow_integrity_error",
                "workflow_status public response differs from its validated "
                "internal derivation",
            )
    except Exception as error:
        if (
            isinstance(error, WorkflowError)
            and error.code == "workflow_integrity_error"
            and str(error).startswith("Roughcut workflow façade")
        ):
            raise
        raise _facade_error(
            "workflow_integrity_error",
            f"workflow_status generated an invalid closed response: {error}",
        ) from error
    return payload


def workflow_status(
    project: str | Path, run_id: str | None = None
) -> dict[str, object]:
    return _validated_public_status(_status_internal(project, run_id))


def read_confirmed_multicam_setup(
    project: str | Path, run_id: str
) -> MulticamSetup | None:
    """Read the current durable setup without consulting session state."""

    project_path = Path(project)
    store = WorkflowStore(project_path)
    with store.write_lock():
        store.recover_pending()
        _require_active_run_id(store, run_id)
        run = store.read_run(run_id)
        setup = run.multicam_setup
        if setup is None:
            return None
        current = ProjectStore(project_path).load()
        scope_subject, scope_dependency = _scope_current_facts(current, run)
        if (
            scope_subject is None
            or _approval_status(
                store, run, "scope", scope_subject, scope_dependency
            )
            != "current"
        ):
            raise _facade_error(
                "workflow_stale",
                "confirmed Multicam Setup no longer has current scope approval",
            )
        return setup


def workflow_review_content_draft_ref(
    project: str | Path,
    run_id: str,
    content_draft_id: str,
) -> dict[str, object]:
    """Return an exact ref for a Review-visible Draft within the workflow anchor."""

    project_path = Path(project)
    store = WorkflowStore(project_path)
    with store.write_lock():
        store.recover_pending()
        _require_active_run_id(store, run_id)
        run = store.read_run(run_id)
        anchor = run.artifact_refs["content_draft"]
        if anchor is None:
            raise _facade_error(
                "workflow_subject_mismatch",
                "Review Content Draft requires a current workflow anchor",
            )
        candidate = _read_draft(
            project_path, content_draft_id, validate_parent=False
        )
        candidate_ref = _artifact_ref("content_draft", candidate)
        _draft_ancestry(project_path, anchor, candidate_ref)
        return _input_ref(candidate_ref)


def _recovery_status(recoveries: tuple[WorkflowRecovery, ...]) -> dict[str, object]:
    if not recoveries:
        return {
            "state": "none",
            "action_id": None,
            "receipt_ref": None,
            "message_code": None,
        }
    recovery = recoveries[-1]
    if recovery.disposition == "rolled_back":
        return {
            "state": "rolled_back",
            "action_id": recovery.action_id,
            "receipt_ref": None,
            "message_code": "workflow_previous_action_recovered",
        }
    assert recovery.receipt is not None
    return {
        "state": "receipt_committed",
        "action_id": recovery.action_id,
        "receipt_ref": _receipt_ref(recovery.receipt).to_dict(),
        "message_code": "workflow_previous_action_committed",
    }


def _allowed_actions(
    run: WorkflowRun,
    readiness: dict[str, object],
    approvals: dict[str, str],
    *,
    first_decision: bool,
    claim_state: str,
) -> tuple[str, ...]:
    if run.lifecycle != "active" or run.stage == "exporting":
        return ()
    allowed: set[str] = set()
    if run.stage == "scope_review":
        if not first_decision:
            allowed.add("approve_scope")
        if approvals["scope"] == "current":
            allowed.add("confirm_brief")
            if (
                approvals["brief"] == "current"
                and readiness["required_transcripts_ready"] is True
                and readiness["speaker_resolution_ready_or_waived"] is True
                and not readiness["blocking_operation_ids"]
            ):
                allowed.add("submit_outline")
    elif run.stage == "outline_review":
        if not first_decision:
            allowed.add("approve_scope")
        if approvals["scope"] == "current":
            allowed.add("confirm_brief")
        if (
            approvals["scope"] == "current"
            and approvals["brief"] == "current"
            and readiness["required_transcripts_ready"] is True
            and readiness["speaker_resolution_ready_or_waived"] is True
            and not readiness["blocking_operation_ids"]
        ):
            allowed.update({"submit_outline", "approve_outline"})
    elif run.stage == "draft_review":
        if not first_decision:
            allowed.add("approve_scope")
        if approvals["scope"] == "current":
            allowed.add("confirm_brief")
        if approvals["outline"] == "current":
            allowed.add("submit_draft")
        draft = run.artifact_refs["content_draft"]
        if draft is not None and approvals["outline"] == "current":
            allowed.add("approve_draft")
    elif run.stage == "roughcut_review":
        allowed.add("return_to_draft")
        if (
            run.artifact_refs["proposal"] is not None
            and approvals["draft"] == "current"
        ):
            allowed.add("adopt_roughcut")
    elif run.stage == "export_review":
        allowed.add("return_to_draft")
        if claim_state == "idle" and approvals["roughcut"] == "current":
            allowed.add("approve_export")
    return tuple(action for action in _ACTION_ORDER if action in allowed)


def _next_action(
    run: WorkflowRun,
    readiness: dict[str, object],
    approvals: dict[str, str],
    allowed: tuple[str, ...],
    *,
    eligible_speakers: bool,
    claim_state: str,
) -> str | None:
    if not allowed:
        return None
    if run.stage == "scope_review":
        if approvals["scope"] != "current":
            return "approve_scope"
        if approvals["brief"] != "current" or eligible_speakers:
            return "confirm_brief"
        if "submit_outline" in allowed:
            return "submit_outline"
        return None
    if run.stage == "outline_review":
        if "approve_scope" in allowed and approvals["scope"] != "current":
            return "approve_scope"
        if "confirm_brief" in allowed and approvals["brief"] != "current":
            return "confirm_brief"
        return "approve_outline" if "approve_outline" in allowed else None
    if run.stage == "draft_review":
        draft = run.artifact_refs["content_draft"]
        if "approve_scope" in allowed and approvals["scope"] != "current":
            return "approve_scope"
        if "confirm_brief" in allowed and approvals["brief"] != "current":
            return "confirm_brief"
        if draft is not None and "approve_draft" in allowed:
            return "approve_draft"
        return "submit_draft" if "submit_draft" in allowed else None
    if run.stage == "roughcut_review":
        return "adopt_roughcut" if "adopt_roughcut" in allowed else None
    if run.stage == "export_review":
        return "approve_export" if claim_state == "idle" else None
    return None


def _require_current_approval(
    status: dict[str, Any], gate: str, action: str
) -> None:
    approval_status = cast(dict[str, str], status["approval_statuses"])[gate]
    if approval_status == "stale":
        raise _facade_error(
            "workflow_stale",
            f"{action} dependency gate {gate!r} is stale",
        )
    if approval_status != "current":
        raise _facade_error(
            "workflow_approval_required",
            f"{action} requires a current {gate} approval",
        )


def _approval(
    *,
    project: Project,
    run: WorkflowRun,
    action_id: str,
    action: str,
    gate: str,
    subject: SubjectRef,
    dependency_hash: str,
    now: str,
) -> ApprovalRecord:
    return ApprovalRecord(
        schema_version=1,
        approval_id=f"appr_{uuid4().hex}",
        run_id=run.run_id,
        project_id=project.project_id,
        gate=gate,
        subject=subject,
        dependency_hash=dependency_hash,
        issued_project_revision=project.revision,
        issued_by_action_id=action_id,
        source_channel="agent_conversation",
        source_action=action,
        actor_assurance="unverified_host_user_action",
        issued_at=now,
    )


def _receipt(
    *,
    project_before: Project,
    project_after: Project,
    run_before: WorkflowRun,
    run_after: WorkflowRun,
    action_id: str,
    input_hash: str,
    action: str,
    approvals: tuple[ApprovalRecord, ...] = (),
    mutation: MutationRef | None = None,
    output_refs: tuple[OutputRef, ...] = (),
    now: str,
) -> ActionReceipt:
    return ActionReceipt(
        schema_version=1,
        action_id=action_id,
        input_hash=input_hash,
        run_id=run_before.run_id,
        project_id=project_before.project_id,
        action=action,
        before=ReceiptState(
            run_before.stage, run_before.lifecycle, project_before.revision
        ),
        after=ReceiptState(run_after.stage, run_after.lifecycle, project_after.revision),
        approval_ids=tuple(record.approval_id for record in approvals),
        mutation=mutation,
        output_refs=output_refs,
        created_at=now,
    )


def _candidate_ref(kind: str, ref: ArtifactRef) -> OutputRef:
    path = {
        "brief": f"briefs/{ref.artifact_id}.json",
        "content_draft": f"content-drafts/{ref.artifact_id}.json",
        "proposal": f"proposals/{ref.artifact_id}.json",
        "decision": f"edits/{ref.artifact_id}.json",
    }[kind]
    return OutputRef(kind, ref.artifact_id, ref.schema_version, ref.content_hash, path)


def _marker(prepared: PreparedResult, action: str) -> TransactionMarker:
    return TransactionMarker(
        schema_version=1,
        action_id=prepared.receipt.action_id,
        input_hash=prepared.input_hash,
        run_id=prepared.run_before.run_id,
        project_id=prepared.project_before.project_id,
        action=action,
        project_before_hash=canonical_sha256_v1(prepared.project_before.to_dict()),
        project_after_hash=canonical_sha256_v1(prepared.project_after.to_dict()),
        run_before_hash=canonical_sha256_v1(prepared.run_before.to_dict()),
        run_after_hash=canonical_sha256_v1(prepared.run_after.to_dict()),
        project_before=prepared.project_before,
        run_before=prepared.run_before,
        candidate_refs=tuple(candidate[0] for candidate in prepared.candidates),
        approval_ids=tuple(record.approval_id for record in prepared.approvals),
        commit_step="prepared",
    )


def _publish_candidate(
    project_path: Path, action_id: str, candidate: PreparedCandidate
) -> None:
    ref, payload = candidate
    if ref.project_relative_path is None:
        return
    path = project_path.joinpath(*ref.project_relative_path.split("/"))
    try:
        publish_workflow_candidate(path, action_id, payload)
    except FileExistsError as error:
        raise _facade_error(
            "workflow_integrity_error",
            f"prepared {ref.kind} candidate or owned temporary node already exists",
        ) from error


def _publish_prepared(
    project_path: Path,
    store: WorkflowStore,
    action: str,
    prepared: PreparedResult,
) -> ActionReceipt:
    marker = _marker(prepared, action)
    store.write_transaction(marker)
    for candidate in prepared.candidates:
        _publish_candidate(project_path, marker.action_id, candidate)
    marker = replace(marker, commit_step="candidates_published")
    store.write_transaction(marker)
    if prepared.project_after != prepared.project_before:
        ProjectStore(project_path).save(
            prepared.project_after,
            expected_revision=prepared.project_before.revision,
        )
    marker = replace(marker, commit_step="project_published")
    store.write_transaction(marker)
    for record in prepared.approvals:
        store.write_approval(record)
    store.write_run(
        prepared.run_after,
        expected_run_hash=canonical_sha256_v1(prepared.run_before.to_dict()),
    )
    marker = replace(marker, commit_step="run_published")
    store.write_transaction(marker)
    receipt = store.write_receipt(prepared.receipt)
    store.delete_transaction(receipt.action_id, input_hash=receipt.input_hash)
    return receipt


def _run_after(
    run: WorkflowRun,
    *,
    stage: str,
    now: str,
    artifact_refs: dict[str, ArtifactRef | None] | None = None,
    approval_refs: dict[str, ApprovalRef | None] | None = None,
    ordered_bindings: tuple[WorkflowBinding, ...] | None = None,
    scope_authorizations: tuple[ScopeAuthorization, ...] | None = None,
    readiness_basis: dict[str, object] | None = None,
    lifecycle: str | None = None,
) -> WorkflowRun:
    return replace(
        run,
        stage=stage,
        lifecycle=run.lifecycle if lifecycle is None else lifecycle,
        updated_at=now,
        artifact_refs=run.artifact_refs if artifact_refs is None else artifact_refs,
        approval_refs=run.approval_refs if approval_refs is None else approval_refs,
        ordered_bindings=(
            run.ordered_bindings if ordered_bindings is None else ordered_bindings
        ),
        scope_authorizations=(
            run.scope_authorizations
            if scope_authorizations is None
            else scope_authorizations
        ),
        readiness_basis=(
            run.readiness_basis if readiness_basis is None else readiness_basis
        ),
    )


def _complete_prepared_run(
    run_after: WorkflowRun, receipt: ActionReceipt
) -> WorkflowRun:
    return _run_with_receipt(run_after, receipt)


def prepare_scope_approval(
    project_path: Path,
    project: Project,
    run: WorkflowRun,
    store: WorkflowStore,
    action_id: str,
    input_hash: str,
    input_payload: dict[str, object],
) -> PreparedScopeApproval:
    now = _timestamp()
    first_decision = _first_decision_published(store, run)
    if run.stage not in {"scope_review", "outline_review", "draft_review"}:
        raise _facade_error(
            "workflow_transition_not_allowed", "approve_scope is illegal at this stage"
        )
    if first_decision:
        raise _facade_error(
            "workflow_transition_not_allowed",
            "scope reapproval is forbidden after the first Decision receipt",
        )
    basis = _scope_basis_projection(
        project_path, project, run, first_decision_published=first_decision
    )
    returned_basis = cast(
        dict[str, str], input_payload["confirmation_basis"]
    )["basis_id"]
    if returned_basis != _basis_id("scope", basis):
        raise _facade_error(
            "workflow_subject_mismatch", "scope confirmation basis is stale"
        )
    raw_authorizations = cast(
        list[dict[str, object]], input_payload["source_authorizations"]
    )
    selectable = {source.source_id for source in project.sources}
    if any(cast(str, item["source_id"]) not in selectable for item in raw_authorizations):
        raise _facade_error(
            "workflow_subject_mismatch",
            "target scope contains a Source outside the selectable basis",
        )
    authorizations = tuple(
        ScopeAuthorization(
            cast(str, item["source_id"]),
            cast(bool, item["transcribe"]),
            cast(bool, item["speaker_diarization"]),
        )
        for item in raw_authorizations
    )
    requested_setup = run.multicam_setup
    declaration: MulticamSetupDeclaration | None = None
    if "multicam_setup" in input_payload:
        try:
            declaration = MulticamSetupDeclaration.from_dict(
                input_payload["multicam_setup"]
            )
        except WorkflowError as error:
            raise _facade_error(
                "workflow_action_invalid", "multicam_setup is invalid"
            ) from error
    bindings: list[WorkflowBinding] = []
    for authorization in authorizations:
        active_id = project.active_transcript_versions.get(authorization.source_id)
        if active_id is None:
            if not authorization.transcribe:
                raise _facade_error(
                    "workflow_not_ready",
                    f"Source {authorization.source_id!r} has no active Transcript "
                    "and transcription was not authorized",
                )
            bindings.append(WorkflowBinding(authorization.source_id, None, None))
        else:
            transcript = _read_transcript(
                project_path, authorization.source_id, active_id
            )
            bindings.append(
                WorkflowBinding(
                    authorization.source_id, active_id, _transcript_hash(transcript)
                )
            )
    proposed_bindings = tuple(bindings)
    if declaration is not None:
        requested_setup = _derive_multicam_setup(
            project, run, declaration, authorizations, proposed_bindings
        )
    if requested_setup is not None:
        _validate_multicam_setup(
            project,
            run,
            requested_setup,
            authorizations,
            ordered_bindings=proposed_bindings,
        )
    if run.approval_refs["scope"] is not None and (
        tuple(bindings) == run.ordered_bindings
        and authorizations == run.scope_authorizations
        and requested_setup == run.multicam_setup
    ):
        raise _facade_error(
            "workflow_action_invalid",
            "scope reapproval must change source, order, or authorization",
        )
    projection = _scope_projection(project, authorizations, requested_setup)
    subject_hash = subject_content_hash("scope_snapshot", 1, projection)
    subject = SubjectRef(
        "scope_snapshot", f"scope_{subject_hash[:16]}", 1, subject_hash
    )
    dependency = _scope_dependency(project, authorizations, requested_setup)
    approval = _approval(
        project=project,
        run=run,
        action_id=action_id,
        action="approve_scope",
        gate="scope",
        subject=subject,
        dependency_hash=dependency,
        now=now,
    )
    artifacts = dict(run.artifact_refs)
    for key in ("brief", "outline", "proposal", "decision", "render"):
        artifacts[key] = None
    approvals = _empty_approval_refs()
    approvals["scope"] = _approval_ref(approval)
    readiness = _empty_readiness_basis()
    readiness["scope_subject_hash"] = subject_hash
    readiness["required_transcripts"] = [
        {
            "source_id": binding.source_id,
            "transcript_version_id": binding.transcript_version_id,
            "schema_version": 1,
            "content_hash": binding.transcript_content_hash,
        }
        for binding in bindings
        if binding.transcript_version_id is not None
    ]
    run_after = _run_after(
        run,
        stage="scope_review",
        now=now,
        artifact_refs=artifacts,
        approval_refs=approvals,
        ordered_bindings=tuple(bindings),
        scope_authorizations=authorizations,
        readiness_basis=readiness,
    )
    run_after = replace(run_after, multicam_setup=requested_setup)
    receipt = _receipt(
        project_before=project,
        project_after=project,
        run_before=run,
        run_after=run_after,
        action_id=action_id,
        input_hash=input_hash,
        action="approve_scope",
        approvals=(approval,),
        now=now,
    )
    run_after = _complete_prepared_run(run_after, receipt)
    return PreparedScopeApproval(
        project, project, run, run_after, input_hash, (), (approval,), receipt
    )


def prepare_brief(
    project_path: Path,
    project: Project,
    run: WorkflowRun,
    store: WorkflowStore,
    action_id: str,
    input_hash: str,
    input_payload: dict[str, object],
) -> PreparedBrief:
    if run.stage not in {"scope_review", "outline_review", "draft_review"}:
        raise _facade_error(
            "workflow_transition_not_allowed", "confirm_brief is illegal at this stage"
        )
    scope_subject, scope_dependency = _scope_current_facts(project, run)
    if (
        scope_subject is None
        or _approval_status(
            store, run, "scope", scope_subject, scope_dependency
        )
        != "current"
    ):
        raise _facade_error(
            "workflow_approval_required", "confirm_brief requires current scope approval"
        )
    basis, eligible = _brief_basis_projection(
        project_path, project, run, scope_subject.content_hash
    )
    returned_basis = cast(
        dict[str, str], input_payload["confirmation_basis"]
    )["basis_id"]
    if returned_basis != _basis_id("brief", basis):
        raise _facade_error(
            "workflow_subject_mismatch", "Brief confirmation basis is stale"
        )
    waivers = cast(
        list[dict[str, str]], input_payload["speaker_resolution_waivers"]
    )
    if waivers != [item for item in eligible if item in waivers]:
        raise _facade_error(
            "workflow_subject_mismatch",
            "speaker waiver is not an exact eligible ordered waiver",
        )
    if run.stage == "draft_review" and run.artifact_refs["brief"] is not None:
        current_brief = _read_brief(
            project_path, run.artifact_refs["brief"]
        )
        business = (
            input_payload["theme"],
            input_payload["target_duration_ticks"],
            tuple(cast(list[str], input_payload["focus"])),
            input_payload["allow_reorder"],
        )
        if business == (
            current_brief.theme,
            current_brief.target_duration_ticks,
            current_brief.focus,
            current_brief.allow_reorder,
        ):
            raise _facade_error(
                "workflow_action_invalid",
                "draft_review confirm_brief must change a Brief business field",
            )
    now = _timestamp()
    prepared_brief = prepare_edit_brief(
        project,
        theme=cast(str, input_payload["theme"]),
        target_duration_ticks=cast(int, input_payload["target_duration_ticks"]),
        focus=cast(list[str], input_payload["focus"]),
        allow_reorder=cast(bool, input_payload["allow_reorder"]),
        updated_at=now,
    )
    brief = prepared_brief.brief
    brief_ref = _artifact_ref("brief", brief)
    dependency = _brief_dependency(project, run, scope_subject.content_hash)
    approval = _approval(
        project=project,
        run=run,
        action_id=action_id,
        action="confirm_brief",
        gate="brief",
        subject=_subject("brief", brief_ref),
        dependency_hash=dependency,
        now=now,
    )
    artifacts = dict(run.artifact_refs)
    artifacts["brief"] = brief_ref
    for key in ("outline", "proposal", "decision", "render"):
        artifacts[key] = None
    approvals = dict(run.approval_refs)
    approvals["brief"] = _approval_ref(approval)
    for gate in ("outline", "draft", "roughcut", "export"):
        approvals[gate] = None
    waived_keys = {
        (item["source_id"], item["transcript_version_id"], item["local_speaker_id"])
        for item in waivers
    }
    speaker, ignored = _speaker_facts(
        project_path, project, run, waived=waived_keys
    )
    del ignored
    readiness = dict(run.readiness_basis)
    readiness.update(
        {
            "brief_subject_hash": brief_ref.content_hash,
            "required_transcripts": _required_transcripts(run),
            "speaker_resolution": speaker,
        }
    )
    run_after = _run_after(
        run,
        stage="scope_review",
        now=now,
        artifact_refs=artifacts,
        approval_refs=approvals,
        readiness_basis=readiness,
    )
    mutation = MutationRef(
        "brief", brief.brief_id, 1, brief_ref.content_hash, True
    )
    receipt = _receipt(
        project_before=project,
        project_after=prepared_brief.project_after,
        run_before=run,
        run_after=run_after,
        action_id=action_id,
        input_hash=input_hash,
        action="confirm_brief",
        approvals=(approval,),
        mutation=mutation,
        now=now,
    )
    run_after = _complete_prepared_run(run_after, receipt)
    candidate = (_candidate_ref("brief", brief_ref), brief.to_dict())
    return PreparedBrief(
        project,
        prepared_brief.project_after,
        run,
        run_after,
        input_hash,
        (candidate,),
        (approval,),
        receipt,
    )


def _validate_outline_evidence(
    project_path: Path, run: WorkflowRun, snapshot: dict[str, object]
) -> None:
    binding_keys = {
        (binding.source_id, binding.transcript_version_id)
        for binding in run.ordered_bindings
        if binding.transcript_version_id is not None
    }
    coverage = cast(
        list[dict[str, object]], snapshot["required_content_coverage"]
    )
    for item in coverage:
        for ref in cast(list[dict[str, object]], item["evidence_refs"]):
            key = (ref["source_id"], ref["transcript_version_id"])
            if key not in binding_keys:
                raise _facade_error(
                    "workflow_subject_mismatch",
                    "Outline evidence ref is outside exact ordered bindings",
                )
            transcript = _read_transcript(
                project_path, cast(str, ref["source_id"]), cast(str, ref["transcript_version_id"])
            )
            segment = next(
                (
                    segment
                    for segment in transcript.segments
                    if segment.segment_id == ref["segment_id"]
                ),
                None,
            )
            if (
                segment is None
                or cast(int, ref["start_ticks"]) < segment.start_ticks
                or cast(int, ref["end_ticks"]) > segment.end_ticks
            ):
                raise _facade_error(
                    "workflow_subject_mismatch",
                    "Outline evidence range is not contained in its exact segment",
                )


def prepare_outline(
    project_path: Path,
    project: Project,
    run: WorkflowRun,
    store: WorkflowStore,
    action_id: str,
    input_hash: str,
    input_payload: dict[str, object],
) -> PreparedOutline:
    if run.stage not in {"scope_review", "outline_review"}:
        raise _facade_error(
            "workflow_transition_not_allowed", "submit_outline is illegal at this stage"
        )
    status = _status_internal(project_path, run.run_id)
    readiness = cast(dict[str, object], status["readiness"])
    approvals = cast(dict[str, str], status["approval_statuses"])
    if approvals["scope"] != "current" or approvals["brief"] != "current":
        raise _facade_error(
            "workflow_approval_required",
            "submit_outline requires current scope and Brief approvals",
        )
    if (
        readiness["required_transcripts_ready"] is not True
        or readiness["speaker_resolution_ready_or_waived"] is not True
        or readiness["blocking_operation_ids"]
    ):
        raise _facade_error(
            "workflow_not_ready", "submit_outline readiness is incomplete"
        )
    _validate_outline_evidence(project_path, run, input_payload)
    content_hash = subject_content_hash("outline_snapshot", 1, input_payload)
    outline_ref = ArtifactRef(
        f"outline_{content_hash[:16]}", 1, content_hash, input_payload
    )
    artifacts = dict(run.artifact_refs)
    artifacts["outline"] = outline_ref
    approval_refs = dict(run.approval_refs)
    approval_refs["outline"] = None
    readiness_basis = dict(run.readiness_basis)
    readiness_basis["required_transcripts"] = _required_transcripts(run)
    readiness_basis["speaker_resolution"] = status[
        "effective_speaker_resolution"
    ]
    now = _timestamp()
    run_after = _run_after(
        run,
        stage="outline_review",
        now=now,
        artifact_refs=artifacts,
        approval_refs=approval_refs,
        readiness_basis=readiness_basis,
    )
    output = OutputRef("outline", outline_ref.artifact_id, 1, content_hash, None)
    receipt = _receipt(
        project_before=project,
        project_after=project,
        run_before=run,
        run_after=run_after,
        action_id=action_id,
        input_hash=input_hash,
        action="submit_outline",
        output_refs=(output,),
        now=now,
    )
    run_after = _complete_prepared_run(run_after, receipt)
    return PreparedOutline(
        project, project, run, run_after, input_hash, (), (), receipt
    )


def prepare_outline_approval(
    project_path: Path,
    project: Project,
    run: WorkflowRun,
    store: WorkflowStore,
    action_id: str,
    input_hash: str,
    input_payload: dict[str, object],
) -> PreparedOutlineApproval:
    if run.stage != "outline_review":
        raise _facade_error(
            "workflow_transition_not_allowed", "approve_outline is illegal at this stage"
        )
    outline = run.artifact_refs["outline"]
    if outline is None or not _ref_matches(
        cast(dict[str, object], input_payload["outline_ref"]), outline
    ):
        raise _facade_error(
            "workflow_subject_mismatch", "approve_outline ref is not current"
        )
    current_status = _status_internal(project_path, run.run_id)
    _require_current_approval(current_status, "scope", "approve_outline")
    _require_current_approval(current_status, "brief", "approve_outline")
    readiness = cast(dict[str, object], current_status["readiness"])
    if (
        readiness["required_transcripts_ready"] is not True
        or readiness["speaker_resolution_ready_or_waived"] is not True
        or readiness["blocking_operation_ids"]
    ):
        raise _facade_error(
            "workflow_not_ready",
            "approve_outline readiness is incomplete",
        )
    dependency = _outline_dependency(
        run,
        current_status["effective_speaker_resolution"],
    )
    if dependency is None:
        raise _facade_error(
            "workflow_not_ready", "Outline dependency is incomplete"
        )
    now = _timestamp()
    approval = _approval(
        project=project,
        run=run,
        action_id=action_id,
        action="approve_outline",
        gate="outline",
        subject=SubjectRef(
            "outline_snapshot",
            outline.artifact_id,
            outline.schema_version,
            outline.content_hash,
        ),
        dependency_hash=dependency,
        now=now,
    )
    approvals = dict(run.approval_refs)
    approvals["outline"] = _approval_ref(approval)
    run_after = _run_after(
        run, stage="draft_review", now=now, approval_refs=approvals
    )
    receipt = _receipt(
        project_before=project,
        project_after=project,
        run_before=run,
        run_after=run_after,
        action_id=action_id,
        input_hash=input_hash,
        action="approve_outline",
        approvals=(approval,),
        now=now,
    )
    run_after = _complete_prepared_run(run_after, receipt)
    return PreparedOutlineApproval(
        project, project, run, run_after, input_hash, (), (approval,), receipt
    )


def _validate_scoped_draft_change(
    parent: ContentDraft,
    candidate_blocks: tuple[ContentDraftBlock, ...],
    mutable_ids: tuple[str, ...],
) -> None:
    parent_by_id = {block.block_id: block for block in parent.blocks}
    candidate_by_id = {block.block_id: block for block in candidate_blocks}
    unknown = set(mutable_ids) - set(parent_by_id)
    if unknown:
        raise _facade_error(
            "workflow_subject_mismatch",
            "scoped mutable block ID is not present in the exact parent",
        )
    if mutable_ids:
        immutable = [
            block.block_id for block in parent.blocks if block.block_id not in mutable_ids
        ]
        for block_id in immutable:
            original = parent_by_id[block_id]
            candidate = candidate_by_id.get(block_id)
            if candidate != original:
                raise _facade_error(
                    "workflow_subject_mismatch",
                    "unnamed Content Draft block changed during a scoped revision",
                )
        candidate_order = [
            block.block_id for block in candidate_blocks if block.block_id in immutable
        ]
        if candidate_order != immutable:
            raise _facade_error(
                "workflow_subject_mismatch",
                "unnamed Content Draft block order changed",
            )


def prepare_content_draft(
    project_path: Path,
    project: Project,
    run: WorkflowRun,
    store: WorkflowStore,
    action_id: str,
    input_hash: str,
    input_payload: dict[str, object],
) -> PreparedContentDraft:
    if run.stage != "draft_review":
        raise _facade_error(
            "workflow_transition_not_allowed", "submit_draft is illegal at this stage"
        )
    status = _status_internal(project_path, run.run_id)
    approvals = cast(dict[str, str], status["approval_statuses"])
    if approvals["outline"] != "current":
        raise _facade_error(
            "workflow_approval_required",
            "submit_draft requires a current Outline approval",
        )
    bindings = _parse_bindings(input_payload["source_bindings"])
    exact_bindings = tuple(
        SourceTranscriptBinding(
            binding.source_id, binding.transcript_version_id
        )
        for binding in run.ordered_bindings
        if binding.transcript_version_id is not None
    )
    if bindings != exact_bindings or len(bindings) != len(run.ordered_bindings):
        raise _facade_error(
            "workflow_subject_mismatch",
            "Content Draft bindings do not equal the exact WorkflowRun bindings",
        )
    brief_ref = run.artifact_refs["brief"]
    if brief_ref is None or not _ref_matches(
        cast(dict[str, object], input_payload["brief_ref"]), brief_ref
    ):
        raise _facade_error(
            "workflow_subject_mismatch", "Content Draft Brief ref is not current"
        )
    brief = _read_active_brief(
        project_path, project, brief_ref.artifact_id
    )
    calculated_context = calculate_agent_context_hash(
        project_path, project=project, bindings=bindings, brief=brief
    )
    if input_payload["context_hash"] != calculated_context:
        raise _facade_error(
            "workflow_stale", "Content Draft context hash is stale"
        )
    parent_ref_payload = input_payload["parent_draft_ref"]
    anchor = run.artifact_refs["content_draft"]
    parent: ContentDraft | None = None
    parent_ref: ArtifactRef | None = None
    if parent_ref_payload is None:
        if anchor is not None:
            raise _facade_error(
                "workflow_subject_mismatch",
                "parent_draft_ref may be null only for the first workflow Draft",
            )
    else:
        parent_ref = ArtifactRef.from_dict(parent_ref_payload)
        if anchor is None:
            raise _facade_error(
                "workflow_subject_mismatch",
                "parent_draft_ref requires an existing workflow anchor",
            )
        _draft_ancestry(project_path, anchor, parent_ref)
        parent = _read_draft_ref(project_path, parent_ref)
    parsed_blocks = _parse_blocks(
        project=project,
        project_path=project_path,
        bindings=bindings,
        blocks=cast(list[dict[str, Any]], input_payload["blocks"]),
        require_active=True,
        schema_version=2,
        allow_derived_canonical=True,
    )
    parent_basis = project_schema1_to_schema2(parent) if parent is not None else None
    if parent_basis is not None:
        parsed_blocks = _normalize_editor_child_blocks(
            parent_basis,
            parsed_blocks,
            ensure_parent_headings=parent is not None and parent.schema_version == 1,
        )
    mutable_ids = tuple(
        cast(list[str], input_payload["scoped_mutable_block_ids"])
    )
    rebase = bool(
        parent is not None
        and (
            parent.source_bindings != bindings
            or parent.brief_snapshot.brief_id != brief.brief_id
            or parent.context_hash != calculated_context
        )
    )
    if parent_basis is not None:
        _validate_scoped_draft_change(parent_basis, parsed_blocks, mutable_ids)
    now = _timestamp()
    draft = ContentDraft(
        content_draft_id=f"draft_{uuid4().hex}",
        parent_draft_id=None if parent is None else parent.content_draft_id,
        base_project_revision=project.revision,
        confirmed_by_user=False,
        brief_snapshot=brief,
        source_bindings=bindings,
        context_hash=calculated_context,
        blocks=parsed_blocks,
        display_title=cast(str | None, input_payload["display_title"]),
        schema_version=2,
    )
    draft_ref = _artifact_ref("content_draft", draft)
    artifacts = dict(run.artifact_refs)
    if anchor is None or rebase:
        if rebase and parent_ref != anchor:
            raise _facade_error(
                "workflow_subject_mismatch",
                "the first rebase child must reference the exact old workflow anchor",
            )
        artifacts["content_draft"] = draft_ref
    now_run = _run_after(
        run, stage="draft_review", now=now, artifact_refs=artifacts
    )
    mutation = MutationRef(
        "content_draft",
        draft.content_draft_id,
        2,
        draft_ref.content_hash,
        True,
    )
    receipt = _receipt(
        project_before=project,
        project_after=project,
        run_before=run,
        run_after=now_run,
        action_id=action_id,
        input_hash=input_hash,
        action="submit_draft",
        mutation=mutation,
        now=now,
    )
    now_run = _complete_prepared_run(now_run, receipt)
    candidate = (
        _candidate_ref("content_draft", draft_ref),
        draft.to_dict(),
    )
    return PreparedContentDraft(
        project, project, run, now_run, input_hash, (candidate,), (), receipt
    )


def prepare_confirmed_draft_and_proposal(
    project_path: Path,
    project: Project,
    run: WorkflowRun,
    store: WorkflowStore,
    action_id: str,
    input_hash: str,
    input_payload: dict[str, object],
) -> PreparedConfirmedDraftAndProposal:
    if run.stage != "draft_review":
        raise _facade_error(
            "workflow_transition_not_allowed", "approve_draft is illegal at this stage"
        )
    current_status = _status_internal(project_path, run.run_id)
    _require_current_approval(current_status, "outline", "approve_draft")
    anchor = run.artifact_refs["content_draft"]
    if anchor is None:
        raise _facade_error("workflow_not_ready", "WorkflowRun has no Draft anchor")
    candidate_ref = ArtifactRef.from_dict(input_payload["content_draft_ref"])
    if not _ref_matches(
        cast(dict[str, object], input_payload["content_draft_ref"]), candidate_ref
    ):
        raise _facade_error(
            "workflow_subject_mismatch", "Content Draft ref is invalid"
        )
    ancestry = _draft_ancestry(project_path, anchor, candidate_ref)
    candidate = _read_draft_ref(project_path, candidate_ref)
    if candidate.confirmed_by_user:
        raise _facade_error(
            "workflow_subject_mismatch", "approve_draft subject must be unconfirmed"
        )
    if any(
        isinstance(block, NarrationBlock) and block.status != "recorded"
        for block in candidate.blocks
    ):
        raise _facade_error(
            "workflow_not_ready",
            "approve_draft requires recorded refs for every narration block",
        )
    now = _timestamp()
    project_after, confirmed = prepare_confirmed_content_draft(
        project_path,
        project,
        candidate,
        child_id=f"draft_{uuid4().hex}",
        updated_at=now,
    )
    proposal = prepare_content_draft_proposal(
        project_path,
        project_after,
        confirmed,
        proposal_id=f"proposal_{uuid4().hex}",
        created_at=now,
    )
    confirmed_ref = _artifact_ref("content_draft", confirmed)
    proposal_ref = _artifact_ref("proposal", proposal)
    artifacts = dict(run.artifact_refs)
    artifacts["content_draft"] = confirmed_ref
    artifacts["proposal"] = proposal_ref
    artifacts["decision"] = None
    artifacts["render"] = None
    draft_dependency = _draft_dependency(
        run,
        candidate_ref,
        ancestry,
        context_hash=candidate.context_hash,
        confirmed_child=confirmed_ref,
    )
    if draft_dependency is None:
        raise _facade_error(
            "workflow_not_ready", "Draft dependency is incomplete"
        )
    approval = _approval(
        project=project,
        run=run,
        action_id=action_id,
        action="approve_draft",
        gate="draft",
        subject=_subject("content_draft", candidate_ref),
        dependency_hash=draft_dependency,
        now=now,
    )
    approval_refs = dict(run.approval_refs)
    approval_refs["draft"] = _approval_ref(approval)
    approval_refs["roughcut"] = None
    approval_refs["export"] = None
    run_after = _run_after(
        run,
        stage="roughcut_review",
        now=now,
        artifact_refs=artifacts,
        approval_refs=approval_refs,
    )
    mutation = MutationRef(
        "content_draft",
        confirmed.content_draft_id,
        2,
        confirmed_ref.content_hash,
        True,
    )
    proposal_output = OutputRef(
        "proposal",
        proposal_ref.artifact_id,
        proposal_ref.schema_version,
        proposal_ref.content_hash,
        None,
    )
    receipt = _receipt(
        project_before=project,
        project_after=project_after,
        run_before=run,
        run_after=run_after,
        action_id=action_id,
        input_hash=input_hash,
        action="approve_draft",
        approvals=(approval,),
        mutation=mutation,
        output_refs=(proposal_output,),
        now=now,
    )
    run_after = _complete_prepared_run(run_after, receipt)
    candidates: tuple[PreparedCandidate, ...] = (
        (_candidate_ref("content_draft", confirmed_ref), confirmed.to_dict()),
        (_candidate_ref("proposal", proposal_ref), proposal.to_dict()),
    )
    return PreparedConfirmedDraftAndProposal(
        project,
        project_after,
        run,
        run_after,
        input_hash,
        candidates,
        (approval,),
        receipt,
    )


def prepare_return_to_draft(
    project_path: Path,
    project: Project,
    run: WorkflowRun,
    store: WorkflowStore,
    action_id: str,
    input_hash: str,
    input_payload: dict[str, object],
) -> PreparedReturnToDraft:
    del store
    if run.stage not in {"roughcut_review", "export_review"}:
        raise _facade_error(
            "workflow_transition_not_allowed",
            "return_to_draft is illegal at this stage",
        )
    subject_payload = cast(
        dict[str, object], input_payload["current_subject_ref"]
    )
    expected_kind = "proposal" if run.stage == "roughcut_review" else "decision"
    current = run.artifact_refs[expected_kind]
    if (
        subject_payload["kind"] != expected_kind
        or current is None
        or {
            key: subject_payload[key]
            for key in ("artifact_id", "schema_version", "content_hash")
        }
        != current.to_dict()
    ):
        raise _facade_error(
            "workflow_subject_mismatch",
            "return_to_draft subject is not the current Proposal/Decision",
        )
    draft_ref = run.artifact_refs["content_draft"]
    if draft_ref is None or not _ref_matches(
        cast(dict[str, object], input_payload["confirmed_content_draft_ref"]),
        draft_ref,
    ):
        raise _facade_error(
            "workflow_subject_mismatch",
            "return_to_draft confirmed Content Draft ref is not current",
        )
    draft = _read_draft_ref(project_path, draft_ref)
    if not draft.confirmed_by_user:
        raise _facade_error(
            "workflow_subject_mismatch",
            "return_to_draft requires the current confirmed Content Draft",
        )
    if expected_kind == "proposal":
        proposal = _read_proposal(project_path, current)
    else:
        decision = _read_decision(project_path, current)
        proposal = decision.proposal_snapshot
    if proposal.brief_snapshot.brief_id != draft.brief_snapshot.brief_id:
        raise _facade_error(
            "workflow_subject_mismatch",
            "Proposal/Decision ancestry does not match the confirmed Content Draft",
        )
    artifacts = dict(run.artifact_refs)
    for key in ("proposal", "decision", "render"):
        artifacts[key] = None
    now = _timestamp()
    run_without_alignment = replace(run, multicam_alignment_continuation=None)
    run_after = _run_after(
        run_without_alignment,
        stage="draft_review",
        now=now,
        artifact_refs=artifacts,
    )
    receipt = _receipt(
        project_before=project,
        project_after=project,
        run_before=run,
        run_after=run_after,
        action_id=action_id,
        input_hash=input_hash,
        action="return_to_draft",
        now=now,
    )
    run_after = _complete_prepared_run(run_after, receipt)
    return PreparedReturnToDraft(
        project, project, run, run_after, input_hash, (), (), receipt
    )


def prepare_decision(
    project_path: Path,
    project: Project,
    run: WorkflowRun,
    store: WorkflowStore,
    action_id: str,
    input_hash: str,
    input_payload: dict[str, object],
) -> PreparedDecision:
    if run.stage != "roughcut_review":
        raise _facade_error(
            "workflow_transition_not_allowed", "adopt_roughcut is illegal at this stage"
        )
    proposal_ref = run.artifact_refs["proposal"]
    if proposal_ref is None or not _ref_matches(
        cast(dict[str, object], input_payload["proposal_ref"]), proposal_ref
    ):
        raise _facade_error(
            "workflow_subject_mismatch", "adopt_roughcut Proposal ref is not current"
        )
    draft_ref = run.artifact_refs["content_draft"]
    draft_approval = run.approval_refs["draft"]
    if draft_ref is None or draft_approval is None:
        raise _facade_error(
            "workflow_approval_required",
            "adopt_roughcut requires a confirmed Draft and approval",
        )
    draft_record = store.read_approval(
        draft_approval.approval_id, run_id=run.run_id
    )
    if canonical_sha256_v1(draft_record.to_dict()) != draft_approval.record_hash:
        raise _facade_error(
            "workflow_integrity_error", "Draft approval ref hash is invalid"
        )
    current_status = _status_internal(project_path, run.run_id)
    _require_current_approval(current_status, "draft", "adopt_roughcut")
    proposal = _read_proposal(project_path, proposal_ref)
    if proposal.base_edit_version_id != project.active_edit_version_id:
        raise _facade_error(
            "workflow_stale",
            "adopt_roughcut Proposal base Edit is no longer active",
        )
    dependency = _roughcut_dependency(run, proposal_ref)
    if dependency is None:
        raise _facade_error(
            "workflow_not_ready", "roughcut dependency is incomplete"
        )
    now = _timestamp()
    project_after, decision = prepare_proposal_decision(
        project_path, project, proposal, created_at=now
    )
    decision_ref = _artifact_ref("decision", decision)
    approval = _approval(
        project=project,
        run=run,
        action_id=action_id,
        action="adopt_roughcut",
        gate="roughcut",
        subject=_subject("proposal", proposal_ref),
        dependency_hash=dependency,
        now=now,
    )
    artifacts = dict(run.artifact_refs)
    artifacts["decision"] = decision_ref
    artifacts["render"] = None
    approvals = dict(run.approval_refs)
    approvals["roughcut"] = _approval_ref(approval)
    approvals["export"] = None
    run_after = _run_after(
        run,
        stage="export_review",
        now=now,
        artifact_refs=artifacts,
        approval_refs=approvals,
    )
    from roughcut.application.multicam_continuation import (
        build_multicam_alignment_continuation,
    )

    run_after = replace(
        run_after,
        multicam_alignment_continuation=build_multicam_alignment_continuation(
            project_path,
            project_after.project_id,
            run_after,
            decision_ref,
            project_after.revision,
        ),
    )
    mutation = MutationRef(
        "decision",
        decision.edit_version_id,
        decision.schema_version,
        decision_ref.content_hash,
        True,
    )
    receipt = _receipt(
        project_before=project,
        project_after=project_after,
        run_before=run,
        run_after=run_after,
        action_id=action_id,
        input_hash=input_hash,
        action="adopt_roughcut",
        approvals=(approval,),
        mutation=mutation,
        now=now,
    )
    run_after = _complete_prepared_run(run_after, receipt)
    candidate = (
        _candidate_ref("decision", decision_ref),
        decision.to_dict(),
    )
    return PreparedDecision(
        project,
        project_after,
        run,
        run_after,
        input_hash,
        (candidate,),
        (approval,),
        receipt,
    )


def prepare_workflow_cancel(
    project: Project,
    run: WorkflowRun,
    action_id: str,
    input_hash: str,
) -> PreparedWorkflowCancel:
    if run.lifecycle != "active":
        raise _facade_error(
            "workflow_transition_not_allowed", "only an active WorkflowRun can be canceled"
        )
    now = _timestamp()
    run_after = _run_after(
        run,
        stage=run.stage,
        lifecycle="canceled",
        now=now,
    )
    receipt = _receipt(
        project_before=project,
        project_after=project,
        run_before=run,
        run_after=run_after,
        action_id=action_id,
        input_hash=input_hash,
        action="workflow_cancel",
        now=now,
    )
    run_after = _complete_prepared_run(run_after, receipt)
    return PreparedWorkflowCancel(
        project, project, run, run_after, input_hash, (), (), receipt
    )


def _existing_receipt(
    store: WorkflowStore,
    run_id: str,
    action_id: str,
    input_hash: str,
) -> ActionReceipt | None:
    path = store.receipts_path / f"{action_id}.json"
    if not os.path.lexists(path):
        return None
    return store.read_receipt(action_id, run_id=run_id, input_hash=input_hash)


def _require_active_run_id(store: WorkflowStore, run_id: str) -> None:
    active = store.active_run()
    if active is None:
        raise _facade_error(
            "workflow_required",
            "workflow action requires an active WorkflowRun",
        )
    if active.run_id != run_id:
        raise _facade_error(
            "workflow_subject_mismatch",
            "requested run_id is not the Project active WorkflowRun",
        )


def workflow_action(
    project: str | Path,
    run_id: str,
    action_id: str,
    action: str,
    input: object,
) -> WorkflowFacadeResult:
    parsed = parse_workflow_action_input(action, input)
    try:
        validate_safe_id(run_id, field="run_id")
        validate_safe_id(action_id, field="action_id")
    except WorkflowError as error:
        raise _facade_error(
            "workflow_action_invalid", "run_id or action_id is invalid"
        ) from error
    input_hash = workflow_action_input_hash(run_id, action_id, action, parsed)
    project_path = Path(project)
    store = WorkflowStore(project_path)
    if action == "approve_export":
        return _workflow_export(
            project_path, run_id, action_id, input_hash, parsed
        )
    receipt: ActionReceipt
    with store.write_lock():
        recoveries = store.recover_pending()
        if recoveries and recoveries[-1].disposition == "rolled_back":
            raise _facade_error(
                "workflow_action_conflict",
                "the previous action was recovered by Roughcut core; "
                "the user must reconfirm before retrying",
            )
        existing = _existing_receipt(store, run_id, action_id, input_hash)
        if existing is not None:
            receipt = existing
        else:
            _require_active_run_id(store, run_id)
            current_status = _status_internal(project_path, run_id)
            run = current_status["workflow_run_model"]
            if run.lifecycle != "active":
                raise _facade_error(
                    "workflow_transition_not_allowed",
                    "workflow_action requires an active WorkflowRun",
                )
            project_before = current_status["project_model"]
            prepared: PreparedResult
            try:
                if action == "approve_scope":
                    prepared = prepare_scope_approval(
                        project_path, project_before, run, store, action_id, input_hash, parsed
                    )
                elif action == "confirm_brief":
                    prepared = prepare_brief(
                        project_path, project_before, run, store, action_id, input_hash, parsed
                    )
                elif action == "submit_outline":
                    prepared = prepare_outline(
                        project_path, project_before, run, store, action_id, input_hash, parsed
                    )
                elif action == "approve_outline":
                    prepared = prepare_outline_approval(
                        project_path, project_before, run, store, action_id, input_hash, parsed
                    )
                elif action == "submit_draft":
                    prepared = prepare_content_draft(
                        project_path, project_before, run, store, action_id, input_hash, parsed
                    )
                elif action == "approve_draft":
                    prepared = prepare_confirmed_draft_and_proposal(
                        project_path, project_before, run, store, action_id, input_hash, parsed
                    )
                elif action == "return_to_draft":
                    prepared = prepare_return_to_draft(
                        project_path, project_before, run, store, action_id, input_hash, parsed
                    )
                else:
                    assert action == "adopt_roughcut"
                    prepared = prepare_decision(
                        project_path, project_before, run, store, action_id, input_hash, parsed
                    )
            except Exception as error:
                raise _translate(error, action) from error
            receipt = _publish_prepared(project_path, store, action, prepared)
    if action == "adopt_roughcut":
        from roughcut.application.multicam_continuation import (
            run_multicam_alignment_continuation,
        )

        run_multicam_alignment_continuation(project_path, run_id)
    status_internal = _status_internal(project_path, run_id)
    return WorkflowFacadeResult(
        status_internal["workflow_run_model"],
        receipt,
        _validated_public_status(status_internal),
    )


def workflow_cancel(
    project: str | Path, run_id: str, action_id: str
) -> WorkflowFacadeResult:
    try:
        validate_safe_id(run_id, field="run_id")
        validate_safe_id(action_id, field="action_id")
    except WorkflowError as error:
        raise _facade_error(
            "workflow_action_invalid", "run_id or action_id is invalid"
        ) from error
    input_hash = workflow_action_input_hash(
        run_id, action_id, "workflow_cancel", {}
    )
    project_path = Path(project)
    store = WorkflowStore(project_path)
    with store.write_lock():
        recoveries = store.recover_pending()
        if recoveries and recoveries[-1].disposition == "rolled_back":
            raise _facade_error(
                "workflow_action_conflict",
                "the previous action was recovered by Roughcut core; "
                "cancel must be explicitly reconfirmed",
            )
        existing = _existing_receipt(store, run_id, action_id, input_hash)
        if existing is not None:
            run = store.read_run(run_id)
            status = _validated_public_status(
                _status_internal(project_path, run_id)
            )
            return WorkflowFacadeResult(run, existing, status)
        _require_active_run_id(store, run_id)
        current_status = _status_internal(project_path, run_id)
        run = current_status["workflow_run_model"]
        project_before = current_status["project_model"]
        prepared = prepare_workflow_cancel(
            project_before, run, action_id, input_hash
        )
        receipt = _publish_prepared(
            project_path, store, "workflow_cancel", prepared
        )
    status_internal = _status_internal(project_path, run_id)
    return WorkflowFacadeResult(
        status_internal["workflow_run_model"],
        receipt,
        _validated_public_status(status_internal),
    )


def _write_json_atomically(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    if os.path.lexists(temporary):
        details = os.lstat(temporary)
        if (
            stat.S_ISLNK(details.st_mode)
            or not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
        ):
            raise _facade_error(
                "workflow_recovery_conflict",
                "export owner temporary node is not a safe owned file",
            )
        temporary.unlink()
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _owner_payload(
    *,
    project_id: str,
    run_id: str,
    action_id: str,
    input_hash: str,
    export_basis_id: str,
    staging_id: str,
    writing_kind: str | None,
    completed_files: list[dict[str, str]],
    created_at: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "project_id": project_id,
        "run_id": run_id,
        "action_id": action_id,
        "input_hash": input_hash,
        "export_basis_id": export_basis_id,
        "staging_id": staging_id,
        "writing_kind": writing_kind,
        "completed_files": completed_files,
        "created_at": created_at,
    }


def _read_owner(path: Path) -> dict[str, object]:
    try:
        details = os.lstat(path)
        if (
            stat.S_ISLNK(details.st_mode)
            or not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
        ):
            raise ValueError("owner is not a single-link regular file")
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=lambda pairs: _owner_pairs(pairs),
        )
        fields = {
            "schema_version",
            "project_id",
            "run_id",
            "action_id",
            "input_hash",
            "export_basis_id",
            "staging_id",
            "writing_kind",
            "completed_files",
            "created_at",
        }
        if not isinstance(payload, dict) or set(payload) != fields:
            raise ValueError("owner is not closed schema 1")
        if payload["schema_version"] != 1:
            raise ValueError("owner schema is unsupported")
        if payload["writing_kind"] not in {None, "plan", "mp4", "manifest"}:
            raise ValueError("writing_kind is invalid")
        if not isinstance(payload["created_at"], str):
            raise TypeError("created_at is not a string")
        completed = payload["completed_files"]
        if not isinstance(completed, list) or len(completed) > 3:
            raise ValueError("completed_files length is invalid")
        validate_safe_id(payload["project_id"], field="project_id")
        validate_safe_id(payload["run_id"], field="run_id")
        validate_safe_id(payload["action_id"], field="action_id")
        validate_safe_id(payload["staging_id"], field="staging_id")
        staging_id = payload["staging_id"]
        if not isinstance(staging_id, str):
            raise TypeError("staging_id is not a string")
        input_hash = payload["input_hash"]
        if (
            not isinstance(input_hash, str)
            or len(input_hash) != 64
            or any(character not in "0123456789abcdef" for character in input_hash)
        ):
            raise ValueError("input hash")
        basis_id = payload["export_basis_id"]
        if (
            not isinstance(basis_id, str)
            or not basis_id.startswith("wfb_export_")
            or len(basis_id) != len("wfb_export_") + 64
            or any(
                character not in "0123456789abcdef"
                for character in basis_id.removeprefix("wfb_export_")
            )
        ):
            raise ValueError("basis ID")
        expected = (
            ("plan", f"{staging_id}.plan.json"),
            ("mp4", f"{staging_id}.mp4"),
            ("manifest", f"{staging_id}.manifest.json"),
        )
        for index, entry in enumerate(completed):
            if not isinstance(entry, dict) or set(entry) != {
                "kind",
                "relative_name",
                "content_hash",
            }:
                raise ValueError("completed entry is not closed")
            kind, relative_name = expected[index]
            content_hash = entry["content_hash"]
            if (
                not isinstance(entry["kind"], str)
                or entry["kind"] != kind
                or not isinstance(entry["relative_name"], str)
                or entry["relative_name"] != relative_name
                or not isinstance(content_hash, str)
                or len(content_hash) != 64
                or any(character not in "0123456789abcdef" for character in content_hash)
            ):
                raise ValueError("completed entry identity is invalid")
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        WorkflowError,
        ValueError,
        TypeError,
        IndexError,
        KeyError,
    ) as error:
        raise _facade_error(
            "workflow_recovery_conflict", f"export owner.json is invalid: {error}"
        ) from error
    return cast(dict[str, object], payload)


def _inspect_export_staging(
    staging_root: Path, *, project_id: str, run_id: str
) -> None:
    if not staging_root.exists():
        return
    for directory in sorted(staging_root.iterdir(), key=lambda path: path.name):
        if directory.name == ".claim.lock":
            continue
        if directory.is_symlink() or not directory.is_dir():
            raise _facade_error(
                "workflow_recovery_conflict",
                "export staging root contains an unsafe orphan node",
            )
        owner = _read_owner(directory / "owner.json")
        if owner["project_id"] != project_id or owner["run_id"] != run_id:
            raise _facade_error(
                "workflow_recovery_conflict",
                "export staging owner belongs to another Project or WorkflowRun",
            )
        _validate_staging_directory(
            staging_root,
            directory,
            owner,
            project_id=project_id,
            run_id=run_id,
            action_id=cast(str, owner["action_id"]),
            input_hash=cast(str, owner["input_hash"]),
            basis_id=cast(str, owner["export_basis_id"]),
        )


def _owner_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate owner key {key}")
        result[key] = value
    return result


def _validate_staging_directory(
    staging_root: Path,
    directory: Path,
    owner: dict[str, object],
    *,
    project_id: str,
    run_id: str,
    action_id: str,
    input_hash: str,
    basis_id: str,
) -> None:
    if directory.parent != staging_root or directory.name != owner["staging_id"]:
        raise _facade_error(
            "workflow_recovery_conflict", "export staging identity/path mismatch"
        )
    directory_stat = os.lstat(directory)
    if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode):
        raise _facade_error(
            "workflow_recovery_conflict", "export staging is not a safe directory"
        )
    expected_identity = (project_id, run_id, action_id, input_hash, basis_id)
    actual_identity = (
        owner["project_id"],
        owner["run_id"],
        owner["action_id"],
        owner["input_hash"],
        owner["export_basis_id"],
    )
    if actual_identity != expected_identity:
        raise _facade_error(
            "workflow_recovery_conflict", "export staging owner identity mismatch"
        )
    staging_id = owner["staging_id"]
    assert isinstance(staging_id, str)
    renderer_workspace_name = workflow_renderer_workspace_name(
        cast(str, owner["project_id"]),
        cast(str, owner["run_id"]),
        cast(str, owner["action_id"]),
        cast(str, owner["input_hash"]),
        staging_id,
        cast(str, owner["export_basis_id"]),
    )
    fixed = {
        "owner.json",
        ".owner.json.tmp",
        f"{staging_id}.plan.json",
        f"{staging_id}.mp4",
        f"{staging_id}.manifest.json",
    }
    entries = set()
    for path in directory.iterdir():
        if path.name == renderer_workspace_name:
            _validate_renderer_workspace(path)
            entries.add(path.name)
            continue
        if path.name not in fixed:
            raise _facade_error(
                "workflow_recovery_conflict", "export staging contains an extra node"
            )
        path_stat = os.lstat(path)
        if (
            stat.S_ISLNK(path_stat.st_mode)
            or not stat.S_ISREG(path_stat.st_mode)
            or path_stat.st_nlink != 1
        ):
            raise _facade_error(
                "workflow_recovery_conflict",
                "export staging contains a symlink/hardlink/non-file node",
            )
        entries.add(path.name)
    if ".owner.json.tmp" in entries:
        (directory / ".owner.json.tmp").unlink()
        entries.remove(".owner.json.tmp")
    completed = cast(list[dict[str, str]], owner["completed_files"])
    expected_entries = {"owner.json"} | {
        entry["relative_name"] for entry in completed
    }
    writing_kind = cast(str | None, owner["writing_kind"])
    allowed_entries = {frozenset(expected_entries)}
    allowed_entries.add(
        frozenset({*expected_entries, renderer_workspace_name})
    )
    if writing_kind is not None:
        partial_name = {
            "plan": f"{staging_id}.plan.json",
            "mp4": f"{staging_id}.mp4",
            "manifest": f"{staging_id}.manifest.json",
        }[writing_kind]
        allowed_entries.add(frozenset({*expected_entries, partial_name}))
        allowed_entries.add(
            frozenset(
                {
                    *expected_entries,
                    partial_name,
                    renderer_workspace_name,
                }
            )
        )
    if frozenset(entries) not in allowed_entries:
        raise _facade_error(
            "workflow_recovery_conflict",
            "export staging nodes do not match owner.json",
        )
    for entry in completed:
        path = directory / entry["relative_name"]
        if entry["kind"] == "mp4":
            actual_hash = stream_file_sha256(path)
        elif entry["kind"] == "manifest":
            actual_hash = _manifest_hash(
                read_json_object(path, description="export staging manifest")
            )
        else:
            actual_hash = canonical_sha256_v1(
                read_json_object(path, description="export staging JSON")
            )
        if actual_hash != entry["content_hash"]:
            raise _facade_error(
                "workflow_recovery_conflict",
                "export staging completed file content identity mismatch",
            )


def _validate_renderer_workspace(directory: Path) -> None:
    details = os.lstat(directory)
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        raise _facade_error(
            "workflow_recovery_conflict",
            "export renderer workspace is not a safe directory",
        )
    allowed = {"filter-complex.txt", "candidate.mp4", "manifest.json"}
    for child in directory.iterdir():
        child_details = os.lstat(child)
        if (
            child.name not in allowed
            or stat.S_ISLNK(child_details.st_mode)
            or not stat.S_ISREG(child_details.st_mode)
            or child_details.st_nlink != 1
        ):
            raise _facade_error(
                "workflow_recovery_conflict",
                "export renderer workspace contains an unsafe node",
            )


def _clean_matching_orphan(
    staging_root: Path,
    *,
    project_id: str,
    run_id: str,
    action_id: str,
    input_hash: str,
    basis_id: str,
) -> None:
    if not staging_root.exists():
        return
    staging_id = workflow_export_staging_id(
        project_id, run_id, action_id, input_hash
    )
    directory = staging_root / staging_id
    for node in staging_root.iterdir():
        if node.name == ".claim.lock" or node == directory:
            continue
        raise _facade_error(
            "workflow_recovery_conflict",
            "export staging contains an orphan owned by another action/input; "
            f"Roughcut core preserved {node.name!r} and did not start Render",
        )
    if not os.path.lexists(directory):
        return
    if not directory.is_dir() or directory.is_symlink():
        raise _facade_error(
            "workflow_recovery_conflict", "export staging root contains an unknown node"
        )
    owner_path = directory / "owner.json"
    if not os.path.lexists(owner_path):
        entries = list(directory.iterdir())
        for entry in entries:
            if entry.name != ".owner.json.tmp":
                raise _facade_error(
                    "workflow_recovery_conflict",
                    "ownerless export staging contains an unknown node",
                )
            details = os.lstat(entry)
            if (
                stat.S_ISLNK(details.st_mode)
                or not stat.S_ISREG(details.st_mode)
                or details.st_nlink != 1
            ):
                raise _facade_error(
                    "workflow_recovery_conflict",
                    "ownerless export staging temp is unsafe",
                )
        shutil.rmtree(directory)
        return
    owner = _read_owner(owner_path)
    _validate_staging_directory(
        staging_root,
        directory,
        owner,
        project_id=project_id,
        run_id=run_id,
        action_id=action_id,
        input_hash=input_hash,
        basis_id=basis_id,
    )
    try:
        shutil.rmtree(directory)
    except OSError as error:
        raise _facade_error(
            "workflow_recovery_conflict",
            f"matching export staging could not be removed: {error}",
        ) from error


def _clean_terminal_tracked_export_orphan(
    project_path: Path,
    store: WorkflowStore,
    staging_root: Path,
    *,
    run_id: str,
    action_id: str,
    input_hash: str,
    input_payload: dict[str, object],
    operation_store: MediaOperationStore,
) -> None:
    """Clean one exact failed/interrupted Render orphan for a new action."""

    if not os.path.lexists(staging_root):
        return
    root_stat = os.lstat(staging_root)
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise _facade_error(
            "workflow_recovery_conflict",
            "export staging root is not a safe directory",
        )
    nodes = [
        node
        for node in sorted(staging_root.iterdir(), key=lambda path: path.name)
        if node.name != ".claim.lock"
    ]
    if not nodes:
        return
    current_staging = staging_root / workflow_export_staging_id(
        operation_store.scope.project_id,
        run_id,
        action_id,
        input_hash,
    )
    if current_staging in nodes:
        return
    if len(nodes) != 1:
        raise _facade_error(
            "workflow_recovery_conflict",
            "export staging contains more than one foreign orphan",
        )
    directory = nodes[0]
    directory_stat = os.lstat(directory)
    if (
        stat.S_ISLNK(directory_stat.st_mode)
        or not stat.S_ISDIR(directory_stat.st_mode)
    ):
        raise _facade_error(
            "workflow_recovery_conflict",
            "export staging root contains an unsafe foreign orphan",
        )
    owner = _read_owner(directory / "owner.json")
    old_action_id = cast(str, owner["action_id"])
    old_input_hash = cast(str, owner["input_hash"])
    old_basis_id = cast(str, owner["export_basis_id"])
    if (
        owner["project_id"] != operation_store.scope.project_id
        or owner["run_id"] != run_id
        or old_action_id == action_id
    ):
        raise _facade_error(
            "workflow_recovery_conflict",
            "export staging owner is not an eligible tracked predecessor",
        )

    from roughcut.domain.media_operation import (
        approve_export_request_projection,
        hash_approve_export_request,
    )

    old_request_hash = hash_approve_export_request(
        approve_export_request_projection(
            operation_store.scope,
            run_id=run_id,
            action_id=old_action_id,
            workflow_action_input_hash=old_input_hash,
        )
    )
    with operation_store.writer(old_action_id, create=False) as acquired:
        if not acquired:
            raise _facade_error(
                "workflow_recovery_conflict",
                "tracked export orphan still has a live media writer",
            )
        old_record = operation_store.read(old_action_id)
        if (
            old_record is None
            or old_record.operation_type != "approve_export"
            or old_record.request_hash != old_request_hash
            or old_record.status not in {"failed", "interrupted"}
        ):
            raise _facade_error(
                "workflow_recovery_conflict",
                "tracked export orphan has no exact failed/interrupted record",
            )
        _validate_staging_directory(
            staging_root,
            directory,
            owner,
            project_id=operation_store.scope.project_id,
            run_id=run_id,
            action_id=old_action_id,
            input_hash=old_input_hash,
            basis_id=old_basis_id,
        )
        with store.write_lock():
            marker = store.transactions_path / f"{old_action_id}.json"
            receipt = store.receipts_path / f"{old_action_id}.json"
            if os.path.lexists(marker) or os.path.lexists(receipt):
                raise _facade_error(
                    "workflow_recovery_conflict",
                    "tracked export orphan still has a marker or receipt",
                )
            recoveries = store.recover_pending()
            if (
                recoveries
                and recoveries[-1].disposition == "rolled_back"
            ):
                raise _facade_error(
                    "workflow_action_conflict",
                    "the previous export was recovered; the user must reconfirm",
                )
            _require_active_run_id(store, run_id)
            current_status = _status_internal(project_path, run_id)
            current_run = current_status["workflow_run_model"]
            current_project = current_status["project_model"]
            if (
                current_run.lifecycle != "active"
                or current_run.stage != "export_review"
                or current_status["approval_statuses"]["roughcut"]
                != "current"
            ):
                raise _facade_error(
                    "workflow_recovery_conflict",
                    "tracked export orphan no longer matches the current run/approval",
                )
            current_decision_ref = current_run.artifact_refs["decision"]
            if current_decision_ref is None:
                raise _facade_error(
                    "workflow_recovery_conflict",
                    "tracked export orphan has no current Decision",
                )
            current_decision = _read_decision(
                project_path, current_decision_ref
            )
            current_export_ref = _export_ref(
                current_project, current_decision
            )
            if not _ref_matches(
                cast(dict[str, object], input_payload["export_ref"]),
                current_export_ref,
            ):
                raise _facade_error(
                    "workflow_recovery_conflict",
                    "new approve_export input does not match the current export",
                )
            current_dependency = _export_dependency(
                current_project,
                current_run,
                current_export_ref,
            )
            current_basis = {
                "project_id": current_project.project_id,
                "run_id": current_run.run_id,
                "export_ref": current_export_ref.to_dict(),
                "dependency_hash": current_dependency,
            }
            if (
                current_dependency is None
                or (
                    "wfb_export_"
                    f"{canonical_sha256_v1(current_basis)}"
                    != old_basis_id
                )
            ):
                raise _facade_error(
                    "workflow_recovery_conflict",
                    "tracked export orphan no longer matches the current basis",
                )
            _clean_matching_orphan(
                staging_root,
                project_id=current_project.project_id,
                run_id=current_run.run_id,
                action_id=old_action_id,
                input_hash=old_input_hash,
                basis_id=old_basis_id,
            )


def _manifest_hash(payload: dict[str, object]) -> str:
    output = cast(dict[str, object], payload["output"])
    if payload["schema_version"] == 2:
        inputs = {"input_source": payload["input_source"]}
    elif payload["schema_version"] == 3:
        inputs = {
            "source_bindings": payload["source_bindings"],
            "input_sources": payload["input_sources"],
        }
    else:
        raise _facade_error(
            "workflow_integrity_error", "Render manifest schema is unsupported"
        )
    return subject_content_hash(
        "render_manifest",
        payload["schema_version"],
        {
            "render_id": payload["render_id"],
            "edit_version_id": payload["edit_version_id"],
            **inputs,
            "clips": payload["clips"],
            "output_settings": payload["output_settings"],
            "output": {"mp4_path": output["mp4_path"]},
            "acceptance": payload["acceptance"],
        },
    )


def prepare_export_publish(
    project: Project,
    run: WorkflowRun,
    action_id: str,
    input_hash: str,
    export_ref: ArtifactRef,
    dependency: str,
    plan: RenderPlanLike,
    plan_payload: dict[str, object],
    staged_mp4: Path,
    manifest_payload: dict[str, object],
) -> PreparedExport:
    now = _timestamp()
    plan_hash = canonical_sha256_v1(plan_payload)
    mp4_hash = stream_file_sha256(staged_mp4)
    manifest_hash = _manifest_hash(manifest_payload)
    approval = _approval(
        project=project,
        run=run,
        action_id=action_id,
        action="approve_export",
        gate="export",
        subject=SubjectRef(
            "export_snapshot",
            export_ref.artifact_id,
            export_ref.schema_version,
            export_ref.content_hash,
        ),
        dependency_hash=dependency,
        now=now,
    )
    render_ref = ArtifactRef(plan.render_id, plan.schema_version, plan_hash)
    artifacts = dict(run.artifact_refs)
    artifacts["render"] = render_ref
    approvals = dict(run.approval_refs)
    approvals["export"] = _approval_ref(approval)
    run_after = _run_after(
        run,
        stage="exporting",
        lifecycle="completed",
        now=now,
        artifact_refs=artifacts,
        approval_refs=approvals,
    )
    mutation = MutationRef(
        "render", plan.render_id, plan.schema_version, plan_hash, True
    )
    mp4_output = OutputRef(
        "mp4", plan.render_id, 1, mp4_hash, plan.output_relative_path
    )
    manifest_output = OutputRef(
        "manifest",
        plan.render_id,
        cast(int, manifest_payload["schema_version"]),
        manifest_hash,
        plan.manifest_relative_path,
    )
    receipt = _receipt(
        project_before=project,
        project_after=project,
        run_before=run,
        run_after=run_after,
        action_id=action_id,
        input_hash=input_hash,
        action="approve_export",
        approvals=(approval,),
        mutation=mutation,
        output_refs=(mp4_output, manifest_output),
        now=now,
    )
    run_after = _complete_prepared_run(run_after, receipt)
    candidates: tuple[PreparedCandidate, ...] = (
        (
            OutputRef(
                "render",
                plan.render_id,
                plan.schema_version,
                plan_hash,
                plan.plan_relative_path,
            ),
            plan_payload,
        ),
        (mp4_output, StagedWorkflowFile(staged_mp4)),
        (manifest_output, manifest_payload),
    )
    return PreparedExport(
        project,
        project,
        run,
        run_after,
        input_hash,
        candidates,
        (approval,),
        receipt,
    )


def _workflow_export(
    project_path: Path,
    run_id: str,
    action_id: str,
    input_hash: str,
    input_payload: dict[str, object],
) -> WorkflowFacadeResult:
    return cast(
        WorkflowFacadeResult,
        _workflow_export_core(
            project_path,
            run_id,
            action_id,
            input_hash,
            input_payload,
            request_hash=None,
            operation_store=None,
        ),
    )


def _workflow_export_operation(
    project_path: Path,
    run_id: str,
    action_id: str,
    input_hash: str,
    input_payload: dict[str, object],
    *,
    request_hash: str,
    operation_store: MediaOperationStore,
) -> Any:
    """Execute the approve_export façade with its fixed media record."""

    return _workflow_export_core(
        project_path,
        run_id,
        action_id,
        input_hash,
        input_payload,
        request_hash=request_hash,
        operation_store=operation_store,
    )


def _workflow_export_core(
    project_path: Path,
    run_id: str,
    action_id: str,
    input_hash: str,
    input_payload: dict[str, object],
    *,
    request_hash: str | None,
    operation_store: MediaOperationStore | None,
) -> Any:
    store = WorkflowStore(project_path)
    staging_root = store.workflow_path / "export-staging"
    with project_export_claim(project_path):
        if operation_store is not None:
            _clean_terminal_tracked_export_orphan(
                project_path,
                store,
                staging_root,
                run_id=run_id,
                action_id=action_id,
                input_hash=input_hash,
                input_payload=input_payload,
                operation_store=operation_store,
            )
        with store.write_lock():
            recoveries = store.recover_pending()
            if recoveries and recoveries[-1].disposition == "rolled_back":
                raise _facade_error(
                    "workflow_action_conflict",
                    "the previous export was recovered; the user must reconfirm",
                )
            existing = _existing_receipt(store, run_id, action_id, input_hash)
            if existing is not None:
                if operation_store is not None:
                    raise _facade_error(
                        "workflow_integrity_error",
                        "approve_export has an authoritative legacy receipt "
                        "without its requested media operation record",
                    )
                run = store.read_run(run_id)
                return WorkflowFacadeResult(
                    run, existing, workflow_status(project_path, run_id)
                )
            _require_active_run_id(store, run_id)
            current_status = _status_internal(project_path, run_id)
            run = current_status["workflow_run_model"]
            if run.lifecycle != "active" or run.stage != "export_review":
                raise _facade_error(
                    "workflow_transition_not_allowed",
                    "approve_export requires active export_review",
                )
            if current_status["approval_statuses"]["roughcut"] != "current":
                raise _facade_error(
                    "workflow_approval_required",
                    "approve_export requires a current roughcut approval",
                )
            project_before = current_status["project_model"]
            decision_ref = run.artifact_refs["decision"]
            if decision_ref is None:
                raise _facade_error(
                    "workflow_not_ready", "approve_export has no current Decision"
                )
            decision = _read_decision(project_path, decision_ref)
            export_ref = _export_ref(project_before, decision)
            if not _ref_matches(
                cast(dict[str, object], input_payload["export_ref"]), export_ref
            ):
                raise _facade_error(
                    "workflow_subject_mismatch", "approve_export ref is stale"
                )
            dependency = _export_dependency(project_before, run, export_ref)
            if dependency is None:
                raise _facade_error(
                    "workflow_approval_required",
                    "approve_export requires current roughcut approval",
                )
            basis_projection = {
                "project_id": project_before.project_id,
                "run_id": run.run_id,
                "export_ref": export_ref.to_dict(),
                "dependency_hash": dependency,
            }
            basis_id = f"wfb_export_{canonical_sha256_v1(basis_projection)}"
            _clean_matching_orphan(
                staging_root,
                project_id=project_before.project_id,
                run_id=run.run_id,
                action_id=action_id,
                input_hash=input_hash,
                basis_id=basis_id,
            )
            staging_id = workflow_export_staging_id(
                project_before.project_id,
                run.run_id,
                action_id,
                input_hash,
            )
        output_target = _export_snapshot(project_before, decision)["output_target"]
        assert isinstance(output_target, str)
        render_id = Path(output_target).stem
        render_tools = None
        if operation_store is not None:
            from roughcut.application.media_operations import (
                _load_persistent_runtime,
            )

            runtime = _load_persistent_runtime()
            render_tools = (runtime.ffmpeg, runtime.ffprobe)
        with store.write_lock():
            store.recover_pending()
            prepared_project = ProjectStore(project_path).load()
            prepared_run = store.read_run(run_id)
            prepared_decision_ref = prepared_run.artifact_refs["decision"]
            if prepared_decision_ref is None:
                raise _facade_error(
                    "workflow_stale",
                    "export Decision changed before Render Plan prepare",
                )
            prepared_decision = _read_decision(
                project_path, prepared_decision_ref
            )
            prepared_export_ref = _export_ref(
                prepared_project, prepared_decision
            )
            prepared_dependency = _export_dependency(
                prepared_project, prepared_run, prepared_export_ref
            )
            prepared_basis = {
                "project_id": prepared_project.project_id,
                "run_id": prepared_run.run_id,
                "export_ref": prepared_export_ref.to_dict(),
                "dependency_hash": prepared_dependency,
            }
            if (
                prepared_export_ref != export_ref
                or prepared_dependency != dependency
                or f"wfb_export_{canonical_sha256_v1(prepared_basis)}"
                != basis_id
            ):
                raise _facade_error(
                    "workflow_stale",
                    "export basis changed before Render Plan prepare",
                )
            project_before = prepared_project
            run = prepared_run
            decision = prepared_decision
            decision_ref = prepared_decision_ref
            if render_tools is None:
                plan = prepare_render_plan(
                    project_path,
                    edit_version_id=decision.edit_version_id,
                    expected_revision=project_before.revision,
                    render_id=render_id,
                )
            else:
                plan = prepare_render_plan(
                    project_path,
                    edit_version_id=decision.edit_version_id,
                    expected_revision=project_before.revision,
                    render_id=render_id,
                    tools=render_tools,
                )
            plan_payload = plan.to_dict()
            roughcut_receipt = None
            operation_input_hash = None
            if operation_store is not None:
                from roughcut.domain.media_operation import (
                    ArtifactIdentity,
                    ReceiptIdentity,
                    hash_approve_export_input,
                )

                roughcut_ref = run.approval_refs["roughcut"]
                if roughcut_ref is None:
                    raise _facade_error(
                        "workflow_approval_required",
                        "approve_export requires a roughcut approval receipt",
                    )
                roughcut_approval = store.read_approval(
                    roughcut_ref.approval_id, run_id=run.run_id
                )
                roughcut_receipt = store.read_receipt(
                    roughcut_approval.issued_by_action_id,
                    run_id=run.run_id,
                )
                if roughcut_receipt.action != "adopt_roughcut":
                    raise _facade_error(
                        "workflow_integrity_error",
                        "roughcut approval is not owned by adopt_roughcut",
                    )
                operation_input = {
                    "input_schema_version": 1,
                    "operation_type": "approve_export",
                    "project_id": project_before.project_id,
                    "run_id": run.run_id,
                    "approve_export_action": {
                        "action_id": action_id,
                        "input_hash": input_hash,
                    },
                    "roughcut_approval_receipt_ref": ReceiptIdentity(
                        roughcut_receipt.action_id,
                        roughcut_receipt.schema_version,
                        canonical_sha256_v1(roughcut_receipt.to_dict()),
                    ).to_dict(),
                    "decision_ref": ArtifactIdentity(
                        decision_ref.artifact_id,
                        decision_ref.schema_version,
                        decision_ref.content_hash,
                    ).to_dict(),
                    "expected_project_revision": project_before.revision,
                    "export_dependency_hash": dependency,
                    "export_basis_hash": basis_id.removeprefix(
                        "wfb_export_"
                    ),
                    "render_plan_ref": ArtifactIdentity(
                        plan.render_id,
                        plan.schema_version,
                        canonical_sha256_v1(plan_payload),
                    ).to_dict(),
                }
                operation_input_hash = hash_approve_export_input(
                    operation_input
                )

        writer = (
            operation_store.writer(action_id, create=True)
            if operation_store is not None
            else nullcontext(True)
        )
        with writer as acquired:
            if not acquired:
                assert operation_store is not None
                assert request_hash is not None
                from roughcut.application.media_operations import (
                    MediaOperationOutcome,
                    _read_live_record,
                )

                concurrent = _read_live_record(
                    operation_store,
                    action_id,
                    "approve_export",
                    request_hash,
                )
                return MediaOperationOutcome(concurrent, None, True)

            active_operation = None
            update_operation_phase = None
            if operation_store is not None:
                assert request_hash is not None
                assert operation_input_hash is not None
                from roughcut.application.media_operations import (
                    _new_pending_record,
                    _phase_record,
                    _running_record,
                )

                raced = operation_store.read(action_id)
                if raced is not None:
                    from roughcut.application.media_operations import (
                        MediaOperationOutcome,
                        _require_same_request,
                    )

                    _require_same_request(
                        raced, "approve_export", request_hash
                    )
                    return MediaOperationOutcome(raced, None, True)
                active_operation = _new_pending_record(
                    action_id,
                    operation_store.scope,
                    "approve_export",
                    request_hash,
                    operation_input_hash,
                )
                operation_store.write_locked(active_operation)
                active_operation = _running_record(
                    active_operation, "render_preparing"
                )
                operation_store.write_locked(active_operation)

                def update_operation_phase(phase: str) -> None:
                    nonlocal active_operation
                    assert active_operation is not None
                    active_operation = _phase_record(
                        active_operation, phase
                    )
                    operation_store.write_locked(active_operation)

            staging = staging_root / staging_id
            owner = _owner_payload(
                project_id=project_before.project_id,
                run_id=run.run_id,
                action_id=action_id,
                input_hash=input_hash,
                export_basis_id=basis_id,
                staging_id=staging_id,
                writing_kind=None,
                completed_files=[],
                created_at=_timestamp(),
            )
            try:
                if operation_store is not None:
                    from roughcut.application.media_operations import (
                        _validate_media_runtime,
                    )

                    assert runtime is not None
                    _validate_media_runtime(runtime)
                staging.mkdir()
                _write_json_atomically(staging / "owner.json", owner)
                owner["writing_kind"] = "plan"
                _write_json_atomically(staging / "owner.json", owner)
                staged_plan = staging / f"{staging_id}.plan.json"
                write_new_json(staged_plan, plan_payload)
                plan_hash = canonical_sha256_v1(plan_payload)
                owner["completed_files"] = [
                    {
                        "kind": "plan",
                        "relative_name": staged_plan.name,
                        "content_hash": plan_hash,
                    }
                ]
                owner["writing_kind"] = "mp4"
                _write_json_atomically(staging / "owner.json", owner)
                staged_mp4 = staging / f"{staging_id}.mp4"
                staged_manifest = staging / f"{staging_id}.manifest.json"
                renderer_workspace = staging / workflow_renderer_workspace_name(
                    project_before.project_id,
                    run.run_id,
                    action_id,
                    input_hash,
                    staging_id,
                    basis_id,
                )

                def record_staged_mp4() -> None:
                    mp4_hash = stream_file_sha256(staged_mp4)
                    owner["completed_files"] = [
                        *cast(
                            list[dict[str, str]],
                            owner["completed_files"],
                        ),
                        {
                            "kind": "mp4",
                            "relative_name": staged_mp4.name,
                            "content_hash": mp4_hash,
                        },
                    ]
                    owner["writing_kind"] = "manifest"
                    _write_json_atomically(staging / "owner.json", owner)

                with renderer_workspace_identity(renderer_workspace):
                    if update_operation_phase is None:
                        _result, manifest_payload = (
                            execute_prepared_render_to_paths(
                                project_path,
                                plan,
                                expected_revision=project_before.revision,
                                output_path=staged_mp4,
                                manifest_path=staged_manifest,
                                after_output_published=record_staged_mp4,
                            )
                        )
                    else:
                        _result, manifest_payload = (
                            execute_prepared_render_to_paths(
                                project_path,
                                plan,
                                expected_revision=project_before.revision,
                                output_path=staged_mp4,
                                manifest_path=staged_manifest,
                                after_output_published=record_staged_mp4,
                                phase_callback=update_operation_phase,
                            )
                        )
                owner["completed_files"] = [
                    *cast(
                        list[dict[str, str]],
                        owner["completed_files"],
                    ),
                    {
                        "kind": "manifest",
                        "relative_name": staged_manifest.name,
                        "content_hash": _manifest_hash(manifest_payload),
                    },
                ]
                owner["writing_kind"] = None
                _write_json_atomically(staging / "owner.json", owner)
                if update_operation_phase is not None:
                    update_operation_phase("render_revalidating_basis")
                with store.write_lock():
                    store.recover_pending()
                    current_project = ProjectStore(project_path).load()
                    current_run = store.read_run(run_id)
                    current_decision_ref = current_run.artifact_refs[
                        "decision"
                    ]
                    if current_decision_ref is None:
                        raise _facade_error(
                            "workflow_stale",
                            "export Decision changed during staging",
                        )
                    current_decision = _read_decision(
                        project_path, current_decision_ref
                    )
                    current_export_ref = _export_ref(
                        current_project, current_decision
                    )
                    current_dependency = _export_dependency(
                        current_project, current_run, current_export_ref
                    )
                    current_basis = {
                        "project_id": current_project.project_id,
                        "run_id": current_run.run_id,
                        "export_ref": current_export_ref.to_dict(),
                        "dependency_hash": current_dependency,
                    }
                    if (
                        current_export_ref != export_ref
                        or current_dependency != dependency
                        or (
                            "wfb_export_"
                            f"{canonical_sha256_v1(current_basis)}"
                            != basis_id
                        )
                    ):
                        shutil.rmtree(staging)
                        raise _facade_error(
                            "workflow_stale",
                            "export basis changed during staging",
                        )
                    if update_operation_phase is not None:
                        update_operation_phase(
                            "render_publishing_workflow"
                        )
                    manifest_payload = read_json_object(
                        staged_manifest,
                        description="staged Render manifest",
                    )
                    prepared = prepare_export_publish(
                        current_project,
                        current_run,
                        action_id,
                        input_hash,
                        current_export_ref,
                        current_dependency,
                        plan,
                        plan_payload,
                        staged_mp4,
                        manifest_payload,
                    )
                    receipt = _publish_prepared(
                        project_path, store, "approve_export", prepared
                    )
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=False)
                stored_operation = None
                if operation_store is not None:
                    assert active_operation is not None
                    from roughcut.application.media_operations import (
                        _terminal_record,
                    )
                    from roughcut.domain.media_operation import (
                        ArtifactIdentity,
                        ReceiptIdentity,
                        RenderOperationResult,
                        RenderOutputIdentity,
                    )

                    outputs = {
                        output.kind: output
                        for output in receipt.output_refs
                    }
                    mp4 = outputs["mp4"]
                    manifest = outputs["manifest"]
                    assert mp4.project_relative_path is not None
                    assert manifest.project_relative_path is not None
                    result_ref = RenderOperationResult(
                        run_id=run_id,
                        render_plan_ref=ArtifactIdentity(
                            plan.render_id,
                            plan.schema_version,
                            plan_hash,
                        ),
                        mp4_ref=RenderOutputIdentity(
                            "mp4",
                            mp4.artifact_id,
                            mp4.schema_version,
                            mp4.content_hash,
                            mp4.project_relative_path,
                        ),
                        manifest_ref=RenderOutputIdentity(
                            "manifest",
                            manifest.artifact_id,
                            manifest.schema_version,
                            manifest.content_hash,
                            manifest.project_relative_path,
                        ),
                        approve_export_receipt_ref=ReceiptIdentity(
                            receipt.action_id,
                            receipt.schema_version,
                            canonical_sha256_v1(receipt.to_dict()),
                        ),
                        project_revision=project_before.revision,
                    )
                    stored_operation = operation_store.write_locked(
                        _terminal_record(
                            active_operation,
                            status="succeeded",
                            result=result_ref,
                        )
                    )
            except (
                KeyboardInterrupt,
                SystemExit,
            ):
                if operation_store is not None:
                    assert active_operation is not None
                    from roughcut.application.media_operations import (
                        _record_interruption,
                    )

                    _record_interruption(
                        operation_store, active_operation
                    )
                raise
            except Exception as error:
                if operation_store is not None:
                    from roughcut.domain.media_operation import (
                        MediaOperationError,
                    )

                    if isinstance(error, MediaOperationError):
                        raise
                    assert active_operation is not None
                    from roughcut.application.media_operations import (
                        _render_failure,
                        _terminal_record,
                    )

                    operation_store.write_locked(
                        _terminal_record(
                            active_operation,
                            status="failed",
                            failure=_render_failure(
                                active_operation, error
                            ),
                        )
                    )
                raise
    status_internal = _status_internal(project_path, run_id)
    facade = WorkflowFacadeResult(
        status_internal["workflow_run_model"],
        receipt,
        _validated_public_status(status_internal),
    )
    if operation_store is None:
        return facade
    assert stored_operation is not None
    from roughcut.application.media_operations import MediaOperationOutcome

    return MediaOperationOutcome(stored_operation, facade, False)
