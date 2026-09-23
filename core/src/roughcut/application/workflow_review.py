"""Proposal-free Review workflow snapshots and stale-session state."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

from roughcut.adapters.artifact_store import read_json_object
from roughcut.adapters.project_store import ProjectStore
from roughcut.application.agent_context import (
    _read_transcript,
    calculate_agent_context_hash,
    read_edit_brief,
)
from roughcut.application.content_drafts import (
    ContentDraftState,
    read_content_draft,
    read_content_draft_editor_child,
)
from roughcut.application.preview import (
    ReviewPlaybackSelection,
    freeze_review_playback_selections,
    safe_review_source_summary,
)
from roughcut.domain.bindings import SourceTranscriptBinding
from roughcut.domain.brief import EditBrief
from roughcut.domain.content_draft import (
    ContentDraft,
    NarrationBlock,
    SourceExcerptBlock,
)
from roughcut.domain.edit import (
    EditDecision,
    EditProposal,
    MultiSourceEditDecision,
    MultiSourceEditProposal,
)
from roughcut.domain.project import Project, ProjectError, SourceAsset
from roughcut.domain.transcript import TimedTranscript

WORKFLOW_REVIEW_SCHEMA_VERSION = 1
_STAGE_TITLES = (
    ("sources", "选择素材"),
    ("prepare", "准备素材"),
    ("transcript", "阅读原始转录稿"),
    ("brief", "确定剪辑要求"),
    ("draft", "调整初稿"),
    ("proposal", "确认剪辑方案"),
    ("decision", "调整已确认剪辑"),
    ("render", "正式导出"),
)


@dataclass(frozen=True)
class WorkflowSessionStatus:
    status: str
    read_only: bool
    snapshot_revision: int
    current_revision: int
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "read_only": self.read_only,
            "snapshot_revision": self.snapshot_revision,
            "current_revision": self.current_revision,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class WorkflowReviewSnapshot:
    project_path: Path
    project_id: str
    project_name: str
    project_revision: int
    source_bindings: tuple[SourceTranscriptBinding, ...]
    sources: tuple[dict[str, object], ...]
    brief: EditBrief | None
    content_draft: ContentDraftState | None
    context_hash: str | None
    allowed_operations: tuple[str, ...]
    playback_selections: tuple[ReviewPlaybackSelection, ...]
    workflow_stages: tuple[dict[str, object], ...]
    preparation: dict[str, object]
    reopened_parent_draft_id: str | None = None
    workflow_schema_version: int = WORKFLOW_REVIEW_SCHEMA_VERSION

    @property
    def authorized_source_ids(self) -> frozenset[str]:
        return frozenset(binding.source_id for binding in self.source_bindings)

    def playback_for(self, source_id: str) -> ReviewPlaybackSelection:
        matches = [item for item in self.playback_selections if item.source_id == source_id]
        if len(matches) != 1:
            raise ProjectError("review playback selection is missing")
        return matches[0]

    def to_dict(self) -> dict[str, object]:
        return {
            "workflow_schema_version": self.workflow_schema_version,
            "review_mode": "workflow",
            "project": {
                "project_id": self.project_id,
                "name": self.project_name,
                "revision": self.project_revision,
            },
            "source_bindings": [binding.to_dict() for binding in self.source_bindings],
            "sources": list(self.sources),
            "brief": self.brief.to_dict() if self.brief is not None else None,
            "content_draft": (
                self.content_draft.to_dict() if self.content_draft is not None else None
            ),
            "context_hash": self.context_hash,
            "readable_transcript": {
                "endpoint": "/api/workflow/readable-transcript",
                "selection_endpoint": "/api/workflow/selection-resolve",
                "pagination": {"offset_unit": "paragraph", "max_limit": 200},
                "filters": ["source_ids", "person_ids", "adoption_statuses", "keyword"],
                "overlay_bases": ["proposal", "decision"],
                "selection_offset_unit": "unicode_code_point",
            },
            "allowed_operations": list(self.allowed_operations),
            "workflow_stages": list(self.workflow_stages),
            "preparation": self.preparation,
        }


def load_workflow_review_snapshot(
    project_path: Path,
    *,
    source_bindings: list[dict[str, object]],
    content_draft_id: str | None = None,
    playback_selections: tuple[ReviewPlaybackSelection, ...] | None = None,
) -> WorkflowReviewSnapshot:
    bindings = _parse_workflow_bindings(source_bindings)
    store = ProjectStore(project_path)
    project = store.load()
    source_models = tuple(_bound_source(project, binding) for binding in bindings)
    transcripts: list[TimedTranscript] = []
    for binding in bindings:
        transcripts.append(_read_transcript(
            store.project_path,
            binding.source_id,
            binding.transcript_version_id,
        ))
    selections = freeze_review_playback_selections(
        store.project_path,
        project,
        source_models,
        playback_selections,
    )
    sources = tuple(
        safe_review_source_summary(
            source,
            binding.transcript_version_id,
            selection,
        )
        for binding, source, selection in zip(
            bindings,
            source_models,
            selections,
            strict=True,
        )
    )
    brief = (
        read_edit_brief(store.project_path, project.active_brief_id).brief
        if project.active_brief_id is not None
        else None
    )
    selected_draft_id = _selected_draft_id(
        store.project_path,
        project,
        bindings,
        explicit_id=content_draft_id,
    )
    draft = (
        read_content_draft(store.project_path, selected_draft_id)
        if selected_draft_id is not None
        else None
    )
    if draft is not None and draft.content_draft.source_bindings != bindings:
        raise ProjectError("content draft bindings do not match workflow bindings")
    context_hash = (
        calculate_agent_context_hash(
            store.project_path,
            project=project,
            bindings=bindings,
            brief=brief,
        )
        if brief is not None
        else None
    )
    operations = _allowed_operations(brief, draft)
    preparation = _preparation_summary(
        project,
        bindings,
        source_models,
        tuple(transcripts),
        selections,
    )
    stages = _workflow_stages(
        store.project_path,
        project,
        bindings,
        brief,
        draft,
        context_hash,
    )
    return WorkflowReviewSnapshot(
        project_path=store.project_path,
        project_id=project.project_id,
        project_name=project.name,
        project_revision=project.revision,
        source_bindings=bindings,
        sources=sources,
        brief=brief,
        content_draft=draft,
        context_hash=context_hash,
        allowed_operations=operations,
        playback_selections=selections,
        workflow_stages=stages,
        preparation=preparation,
    )


def load_workflow_review_content_draft_child(
    snapshot: WorkflowReviewSnapshot,
    content_draft_id: str,
    *,
    parent: ContentDraft,
) -> ContentDraftState:
    return read_content_draft_editor_child(
        snapshot.project_path,
        content_draft_id,
        parent=parent,
        expected_revision=snapshot.project_revision,
    )


def reopen_workflow_review_content_draft(
    project_path: Path,
    *,
    source_bindings: list[dict[str, object]],
    content_draft_id: str,
    playback_selections: tuple[ReviewPlaybackSelection, ...],
) -> WorkflowReviewSnapshot:
    """Reopen an active confirmed draft without persisting a rebase artifact."""
    snapshot = load_workflow_review_snapshot(
        project_path,
        source_bindings=source_bindings,
        content_draft_id=content_draft_id,
        playback_selections=playback_selections,
    )
    if snapshot.content_draft is None or snapshot.brief is None:
        raise ProjectError("roughcut return requires a confirmed Content Draft")
    stored = snapshot.content_draft.content_draft
    project = ProjectStore(snapshot.project_path).load()
    if (
        not stored.confirmed_by_user
        or project.active_content_draft_id != stored.content_draft_id
    ):
        raise ProjectError("roughcut return Content Draft is not active and confirmed")
    if stored.source_bindings != snapshot.source_bindings:
        raise ProjectError("roughcut return Content Draft bindings do not match")
    if stored.brief_snapshot != snapshot.brief or snapshot.context_hash is None:
        raise ProjectError("roughcut return Content Draft Brief does not match")

    rebased = replace(
        stored,
        base_project_revision=project.revision,
        context_hash=snapshot.context_hash,
    )
    reopened = ContentDraftState(rebased, project.revision, "current", ())
    selected = select_workflow_review_content_draft(snapshot, reopened)
    return replace(
        selected,
        reopened_parent_draft_id=stored.content_draft_id,
    )


def select_workflow_review_content_draft(
    snapshot: WorkflowReviewSnapshot,
    draft: ContentDraftState,
) -> WorkflowReviewSnapshot:
    stages = tuple(
        {
            **stage,
            "status": _draft_stage_status(draft),
            "technical_details": {
                "content_draft_id": draft.content_draft.content_draft_id,
                "artifact_status": draft.status,
                "stale_reasons": list(draft.stale_reasons),
            },
        }
        if stage.get("key") == "draft"
        else stage
        for stage in snapshot.workflow_stages
    )
    return replace(
        snapshot,
        content_draft=draft,
        allowed_operations=_allowed_operations(snapshot.brief, draft),
        workflow_stages=stages,
        reopened_parent_draft_id=snapshot.reopened_parent_draft_id,
    )


def workflow_session_status(snapshot: WorkflowReviewSnapshot) -> WorkflowSessionStatus:
    project = ProjectStore(snapshot.project_path).load()
    reasons: list[str] = []
    if project.revision != snapshot.project_revision:
        reasons.append("project_revision")
    if any(
        project.active_transcript_versions.get(binding.source_id)
        != binding.transcript_version_id
        for binding in snapshot.source_bindings
    ):
        reasons.append("active_transcript")
    active_brief_id = snapshot.brief.brief_id if snapshot.brief is not None else None
    if project.active_brief_id != active_brief_id:
        reasons.append("brief")
    elif snapshot.brief is not None:
        assert active_brief_id is not None
        try:
            current_brief = read_edit_brief(snapshot.project_path, active_brief_id).brief
            if current_brief != snapshot.brief:
                reasons.append("brief")
        except (OSError, ProjectError):
            reasons.append("brief")
    if snapshot.brief is not None and "active_transcript" not in reasons:
        try:
            current_hash = calculate_agent_context_hash(
                snapshot.project_path,
                project=project,
                bindings=snapshot.source_bindings,
                brief=snapshot.brief,
            )
            if current_hash != snapshot.context_hash:
                reasons.append("context_hash")
        except (OSError, ProjectError):
            reasons.append("context_hash")
    if snapshot.content_draft is not None:
        try:
            current_draft = read_content_draft(
                snapshot.project_path,
                snapshot.content_draft.content_draft.content_draft_id,
            )
            if (
                snapshot.reopened_parent_draft_id
                == snapshot.content_draft.content_draft.content_draft_id
            ):
                if snapshot.context_hash is None:
                    reasons.append("content_draft")
                else:
                    expected_reopened = replace(
                        current_draft.content_draft,
                        base_project_revision=snapshot.project_revision,
                        context_hash=snapshot.context_hash,
                    )
                    if (
                        expected_reopened != snapshot.content_draft.content_draft
                        or current_draft.content_draft.confirmed_by_user is not True
                        or project.active_content_draft_id
                        != current_draft.content_draft.content_draft_id
                    ):
                        reasons.append("content_draft")
            elif current_draft != snapshot.content_draft:
                reasons.append("content_draft")
        except (OSError, ProjectError):
            reasons.append("content_draft")
    unique_reasons = tuple(dict.fromkeys(reasons))
    return WorkflowSessionStatus(
        status="stale" if unique_reasons else "current",
        read_only=bool(unique_reasons),
        snapshot_revision=snapshot.project_revision,
        current_revision=project.revision,
        reasons=unique_reasons,
    )


def workflow_review_payload(snapshot: WorkflowReviewSnapshot) -> dict[str, object]:
    payload = snapshot.to_dict()
    payload["session"] = workflow_session_status(snapshot).to_dict()
    return payload


def _bound_source(project: Project, binding: SourceTranscriptBinding) -> SourceAsset:
    if (
        project.active_transcript_versions.get(binding.source_id)
        != binding.transcript_version_id
    ):
        raise ProjectError("workflow Transcript binding is not active")
    source = next(
        (item for item in project.sources if item.source_id == binding.source_id),
        None,
    )
    if source is None:
        raise ProjectError("workflow source is not part of the project")
    return source


def _parse_workflow_bindings(
    value: object,
) -> tuple[SourceTranscriptBinding, ...]:
    if not isinstance(value, list) or not value:
        raise ProjectError("workflow requires at least one source binding")
    bindings: list[SourceTranscriptBinding] = []
    for item in value:
        if not isinstance(item, dict):
            raise ProjectError("workflow source binding must be an object")
        if set(item) != {"source_id", "transcript_version_id"}:
            raise ProjectError("workflow source binding fields are invalid")
        bindings.append(SourceTranscriptBinding.from_dict(item))
    source_ids = [binding.source_id for binding in bindings]
    if len(source_ids) != len(set(source_ids)):
        raise ProjectError("workflow source bindings require unique source IDs")
    return tuple(bindings)


def _allowed_operations(
    brief: EditBrief | None,
    draft: ContentDraftState | None,
) -> tuple[str, ...]:
    operations = [
        "brief_read",
        "brief_create",
        "readable_transcript_read",
        "transcript_selection_resolve",
    ]
    if brief is not None:
        operations.append("content_draft_create")
    if draft is not None:
        operations.append("content_draft_read")
        if draft.status == "current":
            if draft.content_draft.confirmed_by_user:
                if all(
                    not isinstance(block, NarrationBlock) or block.status == "recorded"
                    for block in draft.content_draft.blocks
                ):
                    operations.append("content_draft_propose")
            else:
                operations.append("content_draft_confirm")
    return tuple(operations)


def _selected_draft_id(
    project_path: Path,
    project: Project,
    bindings: tuple[SourceTranscriptBinding, ...],
    *,
    explicit_id: str | None,
) -> str | None:
    if explicit_id is not None:
        return explicit_id
    if project.active_content_draft_id is None:
        return None
    active = read_content_draft(project_path, project.active_content_draft_id)
    if active.content_draft.source_bindings != bindings:
        return None
    return project.active_content_draft_id


def _preparation_summary(
    project: Project,
    bindings: tuple[SourceTranscriptBinding, ...],
    sources: tuple[SourceAsset, ...],
    transcripts: tuple[TimedTranscript, ...],
    selections: tuple[ReviewPlaybackSelection, ...],
) -> dict[str, object]:
    items: list[dict[str, object]] = []
    for binding, source, transcript, playback in zip(
        bindings, sources, transcripts, selections, strict=True
    ):
        local_speakers = {
            segment.local_speaker_id
            for segment in transcript.segments
            if segment.local_speaker_id is not None
        }
        mapped = {
            mapping.local_speaker_id: mapping.person_id
            for mapping in project.speaker_maps
            if mapping.source_id == binding.source_id
            and mapping.transcript_version_id == binding.transcript_version_id
            and mapping.confirmed_by_user
            and mapping.local_speaker_id in local_speakers
        }
        person_names = {
            person.person_id: person.name for person in project.persons
        }
        items.append(
            {
                "display_name": source.display_name,
                "tags": list(source.tags),
                "note": source.note,
                "playback_status": (
                    "代理素材已完成，可直接使用"
                    if playback.playback_kind == "proxy"
                    else "使用原素材播放"
                ),
                "transcript_status": "已完成，可直接使用",
                "mapped_person_count": len(set(mapped.values())),
                "mapped_people": [
                    person_names[person_id]
                    for person_id in dict.fromkeys(mapped.values())
                    if person_id in person_names
                ],
                "unmapped_local_speaker_count": len(local_speakers - set(mapped)),
                "technical_details": {
                    "source_id": binding.source_id,
                    "transcript_version_id": binding.transcript_version_id,
                    "playback_kind": playback.playback_kind,
                },
            }
        )
    return {"sources": items}


def _workflow_stages(
    project_path: Path,
    project: Project,
    bindings: tuple[SourceTranscriptBinding, ...],
    brief: EditBrief | None,
    draft: ContentDraftState | None,
    context_hash: str | None,
) -> tuple[dict[str, object], ...]:
    guided_draft = _guided_workflow_draft(
        project_path, project, draft, bindings, brief
    )
    proposal_state = _proposal_stage_state(
        project_path, project, bindings, brief, context_hash, guided_draft
    )
    decision_status, decision_details, guided_edit_id = _decision_stage_state(
        project_path, project, bindings, brief, guided_draft
    )
    statuses = {
        "sources": "已完成，可直接使用",
        "prepare": "已完成，可直接使用",
        "transcript": "待您确认",
        "brief": "已完成，可直接使用" if brief is not None else "尚未开始",
        "draft": (
            "已完成，可直接使用"
            if guided_draft is not None
            else _draft_stage_status(draft)
        ),
        "proposal": (
            "已完成，可直接使用" if guided_edit_id is not None else proposal_state[0]
        ),
        "decision": decision_status,
        "render": _render_stage_status(project_path, guided_edit_id),
    }
    details: dict[str, dict[str, object]] = {
        "sources": {"source_count": len(bindings)},
        "prepare": {"binding_count": len(bindings)},
        "transcript": {"persistent_read_confirmation": False},
        "brief": {
            "brief_id": brief.brief_id if brief is not None else None,
            "schema_version": brief.schema_version if brief is not None else None,
        },
        "draft": {
            "content_draft_id": (
                draft.content_draft.content_draft_id if draft is not None else None
            ),
            "artifact_status": draft.status if draft is not None else None,
            "stale_reasons": list(draft.stale_reasons) if draft is not None else [],
        },
        "proposal": proposal_state[1],
        "decision": decision_details,
        "render": {"active_edit_version_id": project.active_edit_version_id},
    }
    return tuple(
        {
            "key": key,
            "title": title,
            "status": statuses[key],
            "technical_details": details[key],
        }
        for key, title in _STAGE_TITLES
    )


def _draft_stage_status(draft: ContentDraftState | None) -> str:
    if draft is None:
        return "尚未开始"
    if draft.status == "stale":
        return "内容已变更，需要重新确认"
    return (
        "已完成，可直接使用"
        if draft.content_draft.confirmed_by_user
        else "待您确认"
    )


def _proposal_stage_state(
    project_path: Path,
    project: Project,
    bindings: tuple[SourceTranscriptBinding, ...],
    brief: EditBrief | None,
    context_hash: str | None,
    guided_draft: ContentDraft | None,
) -> tuple[str, dict[str, object]]:
    current = 0
    stale = 0
    historical = 0
    proposal_directory = project_path / "proposals"
    if proposal_directory.is_dir():
        for path in proposal_directory.glob("proposal_*.json"):
            try:
                data = read_json_object(path, description="workflow proposal")
                proposal = _proposal_from_data(data)
            except (OSError, ProjectError):
                continue
            if proposal.proposal_id != path.stem:
                continue
            if not _proposal_matches_draft(proposal, guided_draft):
                historical += 1
                continue
            matches = (
                brief is not None
                and proposal.brief_snapshot == brief
                and proposal.context_hash == context_hash
                and proposal.base_project_revision == project.revision
                and proposal.base_edit_version_id == project.active_edit_version_id
            )
            if matches:
                current += 1
            else:
                stale += 1
    status = (
        "待您确认" if current else
        "内容已变更，需要重新确认" if stale else
        "尚未开始"
    )
    return status, {
        "current_count": current,
        "stale_count": stale,
        "historical_count": historical,
    }


def _proposal_from_data(data: dict[str, object]) -> EditProposal | MultiSourceEditProposal:
    schema = data.get("schema_version")
    if schema == 1:
        return EditProposal.from_dict(data)
    if schema == 2:
        return MultiSourceEditProposal.from_dict(data)
    raise ProjectError("unsupported workflow proposal schema")


def _proposal_bindings(
    proposal: EditProposal | MultiSourceEditProposal,
) -> tuple[SourceTranscriptBinding, ...]:
    if isinstance(proposal, EditProposal):
        return (SourceTranscriptBinding(proposal.source_id, proposal.transcript_version_id),)
    return proposal.source_bindings


def _decision_stage_state(
    project_path: Path,
    project: Project,
    bindings: tuple[SourceTranscriptBinding, ...],
    brief: EditBrief | None,
    guided_draft: ContentDraft | None,
) -> tuple[str, dict[str, object], str | None]:
    edit_id = project.active_edit_version_id
    if edit_id is None:
        return "尚未开始", {"active_edit_version_id": None}, None
    try:
        decision = _read_workflow_decision(project_path, edit_id)
        bindings_match = _proposal_bindings(decision.proposal_snapshot) == bindings
        current_context = _proposal_context_is_current(
            project_path, project, decision.proposal_snapshot, brief
        )
        guided_lineage = _decision_descends_from_draft(
            project_path, decision, guided_draft
        )
    except (OSError, ProjectError):
        return "内容已变更，需要重新确认", {
            "active_edit_version_id": edit_id,
            "reason": "unreadable_decision",
        }, None
    details = {
        "active_edit_version_id": edit_id,
        "bindings_match": bindings_match,
        "current_context": current_context,
        "guided_workflow_lineage": guided_lineage,
    }
    if guided_draft is None or not guided_lineage:
        return "尚未开始", details, None
    if not bindings_match or not current_context:
        return "内容已变更，需要重新确认", details, None
    return "已完成，可直接使用", details, edit_id


def _render_stage_status(project_path: Path, edit_version_id: str | None) -> str:
    if edit_version_id is None:
        return "尚未开始"
    render_directory = project_path / "renders"
    if not render_directory.is_dir():
        return "待您确认"
    for path in render_directory.glob("render_*.manifest.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            isinstance(data, dict)
            and data.get("edit_version_id") == edit_version_id
            and isinstance(data.get("acceptance"), dict)
            and data["acceptance"].get("accepted") is True
        ):
            return "已完成，可直接使用"
    # Render Plans are immutable inputs, not persisted job state. Their presence must
    # never imply that a render process is currently running.
    return "待您确认"


def _guided_workflow_draft(
    project_path: Path,
    project: Project,
    draft: ContentDraftState | None,
    bindings: tuple[SourceTranscriptBinding, ...],
    brief: EditBrief | None,
) -> ContentDraft | None:
    if draft is None:
        return None
    artifact = draft.content_draft
    if (
        not artifact.confirmed_by_user
        or artifact.source_bindings != bindings
        or brief is None
        or artifact.brief_snapshot != brief
    ):
        return None
    if any(
        project.active_transcript_versions.get(binding.source_id)
        != binding.transcript_version_id
        for binding in bindings
    ):
        return None
    frozen_revision_project = replace(project, revision=artifact.base_project_revision)
    if calculate_agent_context_hash(
        project_path,
        project=frozen_revision_project,
        bindings=bindings,
        brief=brief,
    ) != artifact.context_hash:
        return None
    return artifact


def _proposal_matches_draft(
    proposal: EditProposal | MultiSourceEditProposal,
    draft: ContentDraft | None,
) -> bool:
    if draft is None:
        return False
    if (
        _proposal_bindings(proposal) != draft.source_bindings
        or proposal.brief_snapshot != draft.brief_snapshot
        or proposal.context_hash != draft.context_hash
    ):
        return False
    refs = tuple(
        ref
        for block in draft.blocks
        for ref in (
            block.refs
            if isinstance(block, SourceExcerptBlock)
            else block.recorded_refs
            if isinstance(block, NarrationBlock)
            else ()
        )
    )
    clip_ranges = tuple(
        (
            clip.source_id,
            clip.transcript_version_id,
            clip.segment_id,
            clip.source_in_ticks,
            clip.source_out_ticks,
        )
        for clip in proposal.clips
    )
    ref_ranges = tuple(
        (
            ref.source_id,
            ref.transcript_version_id,
            ref.segment_id,
            ref.start_ticks,
            ref.end_ticks,
        )
        for ref in refs
    )
    return clip_ranges == ref_ranges


def _read_workflow_decision(
    project_path: Path, edit_version_id: str
) -> EditDecision | MultiSourceEditDecision:
    data = read_json_object(
        project_path / "edits" / f"{edit_version_id}.json",
        description="workflow decision",
    )
    schema = data.get("schema_version")
    if schema == 1:
        decision: EditDecision | MultiSourceEditDecision = EditDecision.from_dict(data)
    elif schema == 2:
        decision = MultiSourceEditDecision.from_dict(data)
    else:
        raise ProjectError("unsupported workflow decision schema")
    if decision.edit_version_id != edit_version_id:
        raise ProjectError("workflow decision identity mismatch")
    return decision


def _decision_descends_from_draft(
    project_path: Path,
    decision: EditDecision | MultiSourceEditDecision,
    draft: ContentDraft | None,
) -> bool:
    if draft is None:
        return False
    visited: set[str] = set()
    current = decision
    while True:
        if current.edit_version_id in visited:
            raise ProjectError("workflow decision history contains a cycle")
        visited.add(current.edit_version_id)
        if _proposal_matches_draft(current.proposal_snapshot, draft):
            return True
        parent_id = current.proposal_snapshot.base_edit_version_id
        if parent_id is None:
            return False
        current = _read_workflow_decision(project_path, parent_id)


def _proposal_context_is_current(
    project_path: Path,
    project: Project,
    proposal: EditProposal | MultiSourceEditProposal,
    brief: EditBrief | None,
) -> bool:
    if brief is None or proposal.brief_snapshot != brief:
        return False
    if any(
        project.active_transcript_versions.get(binding.source_id)
        != binding.transcript_version_id
        for binding in _proposal_bindings(proposal)
    ):
        return False
    frozen_revision_project = replace(project, revision=proposal.base_project_revision)
    return calculate_agent_context_hash(
        project_path,
        project=frozen_revision_project,
        bindings=_proposal_bindings(proposal),
        brief=brief,
    ) == proposal.context_hash
