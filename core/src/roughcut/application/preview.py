"""Read-only review snapshots derived from current project artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from roughcut.adapters.artifact_store import read_json_object
from roughcut.adapters.project_store import ProjectStore
from roughcut.application.agent_context import (
    _find_source,
    _multi_source_context_hash,
    _read_transcript,
    read_agent_context,
    read_multi_source_agent_context,
)
from roughcut.application.proposals import (
    read_edit_decision,
    read_edit_proposal,
    read_multi_source_edit_decision,
    read_multi_source_edit_proposal,
)
from roughcut.application.proxies import FrozenProxyPlayback, freeze_ready_proxy
from roughcut.domain.edit import EditProposal, MultiSourceEditProposal
from roughcut.domain.people import Person, SpeakerMap
from roughcut.domain.project import Project, ProjectError, SourceAsset
from roughcut.domain.timeline import VirtualTimeline
from roughcut.domain.transcript import TimedTranscript, TranscriptSegment

ReviewProposal = EditProposal | MultiSourceEditProposal


@dataclass(frozen=True)
class ReviewPlaybackSelection:
    source_id: str
    playback_kind: str
    proxy: FrozenProxyPlayback | None = None

    def __post_init__(self) -> None:
        if self.playback_kind not in {"original", "proxy"}:
            raise ProjectError("review playback kind is invalid")
        if (self.playback_kind == "proxy") != (self.proxy is not None):
            raise ProjectError("review proxy selection is invalid")


@dataclass(frozen=True)
class ReviewSnapshot:
    project_path: Path
    proposal: ReviewProposal
    basis_type: str
    basis_id: str
    project_id: str
    project_name: str
    project_revision: int
    context_hash: str
    allowed_operations: tuple[str, ...]
    transcript: tuple[dict[str, object], ...]
    source: dict[str, object] | None
    sources: tuple[dict[str, object], ...]
    timeline: VirtualTimeline
    requires_new_proposal: bool
    schema_version: int
    playback_selections: tuple[ReviewPlaybackSelection, ...]

    @property
    def authorized_source_ids(self) -> frozenset[str]:
        return frozenset(str(source["source_id"]) for source in self.sources)

    def playback_for(self, source_id: str) -> ReviewPlaybackSelection:
        matches = [item for item in self.playback_selections if item.source_id == source_id]
        if len(matches) != 1:
            raise ProjectError("review playback selection is missing")
        return matches[0]

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "project": {
                "project_id": self.project_id,
                "name": self.project_name,
                "revision": self.project_revision,
            },
            "basis": {"type": self.basis_type, "id": self.basis_id},
            "proposal": self.proposal.to_dict(),
            "context_hash": self.context_hash,
            "allowed_operations": list(self.allowed_operations),
            "transcript": list(self.transcript),
            "sources": list(self.sources),
            "timeline": self.timeline.to_dict(),
            "requires_new_proposal": self.requires_new_proposal,
        }
        if self.source is not None:
            payload["source"] = self.source
        return payload


def load_review_snapshot(
    project_path: Path,
    *,
    proposal_id: str | None = None,
    edit_version_id: str | None = None,
    playback_selections: tuple[ReviewPlaybackSelection, ...] | None = None,
) -> ReviewSnapshot:
    if proposal_id is not None and edit_version_id is not None:
        raise ProjectError("review accepts either a proposal or a decision")
    store = ProjectStore(project_path)
    project = store.load()
    proposal, basis_type, basis_id, requires_new_proposal, _decision_revision = _read_review_basis(
        store,
        project,
        proposal_id=proposal_id,
        edit_version_id=edit_version_id,
    )
    if isinstance(proposal, MultiSourceEditProposal):
        return _load_multi_source_snapshot(
            store,
            project,
            proposal,
            basis_type=basis_type,
            basis_id=basis_id,
            requires_new_proposal=requires_new_proposal,
            playback_selections=playback_selections,
        )
    return _load_single_source_snapshot(
        store,
        project,
        proposal,
        basis_type=basis_type,
        basis_id=basis_id,
        requires_new_proposal=requires_new_proposal,
        playback_selections=playback_selections,
    )


def _read_review_basis(
    store: ProjectStore,
    project: Project,
    *,
    proposal_id: str | None,
    edit_version_id: str | None,
) -> tuple[ReviewProposal, str, str, bool, int | None]:
    if proposal_id is not None:
        schema = _artifact_schema(store.project_path / "proposals" / f"{proposal_id}.json")
        if schema == 1:
            proposal: ReviewProposal = read_edit_proposal(store.project_path, proposal_id).proposal
        elif schema == 2:
            proposal = read_multi_source_edit_proposal(store.project_path, proposal_id).proposal
        else:
            raise ProjectError("unsupported review proposal schema version")
        if proposal.base_project_revision != project.revision:
            raise ProjectError("proposal project revision is stale")
        if proposal.base_edit_version_id != project.active_edit_version_id:
            raise ProjectError("proposal edit base is stale")
        return proposal, "proposal", proposal.proposal_id, False, None

    selected_edit_id = edit_version_id or project.active_edit_version_id
    if selected_edit_id is None:
        raise ProjectError("review requires a proposal or active decision")
    if selected_edit_id != project.active_edit_version_id:
        raise ProjectError("review decision is not active")
    schema = _artifact_schema(store.project_path / "edits" / f"{selected_edit_id}.json")
    if schema == 1:
        legacy_decision = read_edit_decision(store.project_path, selected_edit_id).decision
        decision_proposal: ReviewProposal = legacy_decision.proposal_snapshot
        decision_id = legacy_decision.edit_version_id
        decision_revision = legacy_decision.project_revision
    elif schema == 2:
        multi_decision = read_multi_source_edit_decision(
            store.project_path, selected_edit_id
        ).decision
        decision_proposal = multi_decision.proposal_snapshot
        decision_id = multi_decision.edit_version_id
        decision_revision = multi_decision.project_revision
    else:
        raise ProjectError("unsupported review decision schema version")
    return (
        decision_proposal,
        "decision",
        decision_id,
        True,
        decision_revision,
    )


def _load_single_source_snapshot(
    store: ProjectStore,
    project: Project,
    proposal: EditProposal,
    *,
    basis_type: str,
    basis_id: str,
    requires_new_proposal: bool,
    playback_selections: tuple[ReviewPlaybackSelection, ...] | None,
) -> ReviewSnapshot:
    context = read_agent_context(
        store.project_path,
        source_id=proposal.source_id,
        transcript_version_id=proposal.transcript_version_id,
        brief_id=proposal.brief_snapshot.brief_id,
        expected_revision=project.revision,
        offset=0,
        limit=1,
    )
    if basis_type == "proposal" and context.context_hash != proposal.context_hash:
        raise ProjectError("proposal context hash is stale")
    if context.brief != proposal.brief_snapshot:
        raise ProjectError("review brief snapshot is stale")
    source = _find_source(project, proposal.source_id)
    transcript_model = _read_transcript(
        store.project_path, proposal.source_id, proposal.transcript_version_id
    )
    selections = _playback_selections(
        store.project_path,
        project,
        (source,),
        playback_selections,
    )
    source_summary = _source_summary(
        source,
        proposal.transcript_version_id,
        selections[0],
    )
    transcript = _transcript_entries(project, source, transcript_model)
    return ReviewSnapshot(
        project_path=store.project_path,
        proposal=proposal,
        basis_type=basis_type,
        basis_id=basis_id,
        project_id=project.project_id,
        project_name=project.name,
        project_revision=project.revision,
        context_hash=context.context_hash,
        allowed_operations=context.allowed_operations,
        transcript=transcript,
        source=source_summary,
        sources=(source_summary,),
        timeline=VirtualTimeline.from_proposal(proposal),
        requires_new_proposal=requires_new_proposal,
        schema_version=1,
        playback_selections=selections,
    )


def _load_multi_source_snapshot(
    store: ProjectStore,
    project: Project,
    proposal: MultiSourceEditProposal,
    *,
    basis_type: str,
    basis_id: str,
    requires_new_proposal: bool,
    playback_selections: tuple[ReviewPlaybackSelection, ...] | None,
) -> ReviewSnapshot:
    binding_payloads = [binding.to_dict() for binding in proposal.source_bindings]
    context = read_multi_source_agent_context(
        store.project_path,
        source_bindings=binding_payloads,
        brief_id=proposal.brief_snapshot.brief_id,
        expected_revision=project.revision,
        offset=0,
        limit=1,
    )
    if basis_type == "proposal" and context.context_hash != proposal.context_hash:
        raise ProjectError("proposal context hash is stale")
    if context.brief != proposal.brief_snapshot:
        raise ProjectError("review brief snapshot is stale")
    sources: list[dict[str, object]] = []
    transcript: list[dict[str, object]] = []
    transcript_models: list[TimedTranscript] = []
    source_models: list[SourceAsset] = []
    for binding in proposal.source_bindings:
        source = _find_source(project, binding.source_id)
        source_models.append(source)
        transcript_model = _read_transcript(
            store.project_path,
            binding.source_id,
            binding.transcript_version_id,
        )
        transcript_models.append(transcript_model)
        transcript.extend(_transcript_entries(project, source, transcript_model))
    selections = _playback_selections(
        store.project_path,
        project,
        tuple(source_models),
        playback_selections,
    )
    for binding, source, selection in zip(
        proposal.source_bindings,
        source_models,
        selections,
        strict=True,
    ):
        sources.append(_source_summary(source, binding.transcript_version_id, selection))
    if basis_type == "decision":
        frozen_context_hash = _multi_source_context_hash(
            project=project,
            bindings=proposal.source_bindings,
            sources=context.sources,
            persons=context.persons,
            speaker_maps=context.speaker_maps,
            transcripts=transcript_models,
            brief=context.brief,
            allowed_operations=context.allowed_operations,
            project_revision=proposal.base_project_revision,
        )
        if frozen_context_hash != proposal.context_hash:
            raise ProjectError("review decision context hash is stale")
    return ReviewSnapshot(
        project_path=store.project_path,
        proposal=proposal,
        basis_type=basis_type,
        basis_id=basis_id,
        project_id=project.project_id,
        project_name=project.name,
        project_revision=project.revision,
        context_hash=context.context_hash,
        allowed_operations=context.allowed_operations,
        transcript=tuple(transcript),
        source=None,
        sources=tuple(sources),
        timeline=VirtualTimeline.from_proposal(proposal),
        requires_new_proposal=requires_new_proposal,
        schema_version=2,
        playback_selections=selections,
    )


def _source_summary(
    source: SourceAsset,
    transcript_version_id: str,
    playback: ReviewPlaybackSelection,
) -> dict[str, object]:
    return {
        "source_id": source.source_id,
        "transcript_version_id": transcript_version_id,
        "display_name": source.display_name,
        "kind": source.kind,
        "duration_ticks": source.probe.duration_ticks,
        "tags": list(source.tags),
        "note": source.note,
        "media_url": f"/media/{source.source_id}",
        "playback_kind": playback.playback_kind,
        "proxy_profile": (
            dict(playback.proxy.profile_summary) if playback.proxy is not None else None
        ),
    }


def safe_review_source_summary(
    source: SourceAsset,
    transcript_version_id: str,
    playback: ReviewPlaybackSelection,
) -> dict[str, object]:
    """Return the existing path-free Review source summary."""

    return _source_summary(source, transcript_version_id, playback)


def _playback_selections(
    project_path: Path,
    project: Project,
    sources: tuple[SourceAsset, ...],
    frozen: tuple[ReviewPlaybackSelection, ...] | None,
) -> tuple[ReviewPlaybackSelection, ...]:
    if frozen is not None:
        by_source = {selection.source_id: selection for selection in frozen}
        if len(by_source) != len(frozen):
            raise ProjectError("review playback selections contain duplicate sources")
        try:
            return tuple(by_source[source.source_id] for source in sources)
        except KeyError as error:
            raise ProjectError("review playback selection is missing") from error
    selected: list[ReviewPlaybackSelection] = []
    for source in sources:
        proxy = freeze_ready_proxy(
            project_path,
            source_id=source.source_id,
            expected_revision=project.revision,
        )
        selected.append(
            ReviewPlaybackSelection(
                source_id=source.source_id,
                playback_kind="proxy" if proxy is not None else "original",
                proxy=proxy,
            )
        )
    return tuple(selected)


def freeze_review_playback_selections(
    project_path: Path,
    project: Project,
    sources: tuple[SourceAsset, ...],
    frozen: tuple[ReviewPlaybackSelection, ...] | None = None,
) -> tuple[ReviewPlaybackSelection, ...]:
    """Freeze original/proxy choices for a Review session."""

    return _playback_selections(project_path, project, sources, frozen)


def _transcript_entries(
    project: Project,
    source: SourceAsset,
    transcript: TimedTranscript,
) -> tuple[dict[str, object], ...]:
    people = {person.person_id: person for person in project.persons}
    mappings = {
        mapping.local_speaker_id: mapping
        for mapping in project.speaker_maps
        if mapping.source_id == source.source_id
        and mapping.transcript_version_id == transcript.transcript_version_id
    }
    return tuple(
        _transcript_entry(source, transcript, segment, mappings, people)
        for segment in transcript.segments
    )


def _transcript_entry(
    source: SourceAsset,
    transcript: TimedTranscript,
    segment: TranscriptSegment,
    mappings: dict[str, SpeakerMap],
    people: dict[str, Person],
) -> dict[str, object]:
    mapping = (
        mappings.get(segment.local_speaker_id) if segment.local_speaker_id is not None else None
    )
    person = people.get(mapping.person_id) if mapping is not None else None
    if mapping is not None and person is None:
        raise ProjectError("speaker map person is not part of the project")
    person_id = person.person_id if person is not None else None
    person_name = person.name if person is not None else None
    return {
        "source_id": source.source_id,
        "transcript_version_id": transcript.transcript_version_id,
        "segment_id": segment.segment_id,
        "text": segment.corrected_text or segment.original_text,
        "original_text": segment.original_text,
        "corrected_text": segment.corrected_text,
        "start_ticks": segment.start_ticks,
        "end_ticks": segment.end_ticks,
        "speaker": person_name or segment.person_id or segment.local_speaker_id,
        "local_speaker_id": segment.local_speaker_id,
        "person_id": person_id,
        "person_name": person_name,
    }


def _artifact_schema(path: Path) -> int:
    data = read_json_object(path, description="review artifact")
    schema = data.get("schema_version")
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise ProjectError("review artifact schema version is invalid")
    return schema
