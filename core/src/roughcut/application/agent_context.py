"""Edit brief persistence and bounded Agent context services."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeAlias
from uuid import uuid4

from roughcut.adapters.artifact_store import read_json_object, write_new_json
from roughcut.adapters.project_store import ProjectStore
from roughcut.domain.agent import (
    AgentContextPage,
    MultiSourceAgentContextPage,
    RevisionContextPage,
)
from roughcut.domain.bindings import SourceTranscriptBinding, parse_source_bindings
from roughcut.domain.brief import EditBrief
from roughcut.domain.edit import (
    EditDecision,
    MultiSourceEditDecision,
)
from roughcut.domain.people import Person, SpeakerMap
from roughcut.domain.project import Project, ProjectError, SourceAsset
from roughcut.domain.transcript import TimedTranscript

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_CONTEXT_PAGE = 200

EditDecisionLike: TypeAlias = EditDecision | MultiSourceEditDecision


@dataclass(frozen=True)
class BriefState:
    brief: EditBrief
    project_revision: int

    def to_dict(self) -> dict[str, object]:
        return {"brief": self.brief.to_dict(), "project_revision": self.project_revision}


@dataclass(frozen=True)
class PreparedEditBrief:
    """Pure Brief candidate and exact Project image for controlled publication."""

    brief: EditBrief
    project_after: Project


def prepare_edit_brief(
    project: Project,
    *,
    theme: str,
    target_duration_ticks: int,
    focus: list[str],
    allow_reorder: bool,
    brief_id: str | None = None,
    updated_at: str | None = None,
) -> PreparedEditBrief:
    """Construct the existing Brief mutation without touching the filesystem."""

    prepared_id = brief_id or f"brief_{uuid4().hex}"
    prepared_at = updated_at or datetime.now(UTC).isoformat()
    brief = EditBrief(
        brief_id=prepared_id,
        theme=theme,
        target_duration_ticks=target_duration_ticks,
        focus=tuple(focus),
        allow_reorder=allow_reorder,
    )
    return PreparedEditBrief(
        brief=brief,
        project_after=replace(
            project,
            revision=project.revision + 1,
            updated_at=prepared_at,
            active_brief_id=brief.brief_id,
        ),
    )


def publish_edit_brief_candidate(project_path: Path, brief: EditBrief) -> None:
    """Publish one already-prepared immutable Brief object."""

    write_new_json(project_path / "briefs" / f"{brief.brief_id}.json", brief.to_dict())


def create_edit_brief(
    project_path: Path,
    *,
    theme: str,
    target_duration_ticks: int,
    focus: list[str],
    allow_reorder: bool,
    expected_revision: int,
) -> BriefState:
    store = ProjectStore(project_path)
    project = store.load()
    if project.revision != expected_revision:
        raise ProjectError("project revision conflict")
    prepared = prepare_edit_brief(
        project,
        theme=theme,
        target_duration_ticks=target_duration_ticks,
        focus=focus,
        allow_reorder=allow_reorder,
    )
    brief = prepared.brief
    brief_path = store.project_path / "briefs" / f"{brief.brief_id}.json"
    publish_edit_brief_candidate(store.project_path, brief)
    updated = prepared.project_after
    try:
        store.save(updated, expected_revision=expected_revision)
    except Exception:
        brief_path.unlink(missing_ok=True)
        raise
    return BriefState(brief=brief, project_revision=updated.revision)


def read_edit_brief(project_path: Path, brief_id: str) -> BriefState:
    _validate_id(brief_id, "brief_id")
    store = ProjectStore(project_path)
    project = store.load()
    data = read_json_object(
        store.project_path / "briefs" / f"{brief_id}.json", description="edit brief"
    )
    brief = EditBrief.from_dict(data)
    if brief.brief_id != brief_id:
        raise ProjectError("edit brief identity mismatch")
    return BriefState(brief=brief, project_revision=project.revision)


def read_agent_context(
    project_path: Path,
    *,
    source_id: str,
    transcript_version_id: str,
    brief_id: str,
    expected_revision: int,
    offset: int,
    limit: int,
) -> AgentContextPage:
    _validate_id(source_id, "source_id")
    _validate_id(transcript_version_id, "transcript_version_id")
    _validate_id(brief_id, "brief_id")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ProjectError("context offset must be a non-negative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_CONTEXT_PAGE:
        raise ProjectError("context limit must be between 1 and 200")

    store = ProjectStore(project_path)
    project = store.load()
    if project.revision != expected_revision:
        raise ProjectError("project revision conflict")
    if project.active_brief_id != brief_id:
        raise ProjectError("brief is not active for this project revision")
    if project.active_transcript_versions.get(source_id) != transcript_version_id:
        raise ProjectError("transcript is not active for this project revision")
    source = _find_source(project, source_id)
    brief = read_edit_brief(store.project_path, brief_id).brief
    transcript = _read_transcript(store.project_path, source_id, transcript_version_id)
    operations = ("select", "trim", "reorder") if brief.allow_reorder else ("select", "trim")
    source_context: dict[str, object] = {
        "source_id": source.source_id,
        "display_name": source.display_name,
        "kind": source.kind,
        "duration_ticks": source.probe.duration_ticks,
        "tags": list(source.tags),
        "note": source.note,
    }
    person_context = tuple(person.to_dict() for person in project.persons)
    current_maps = tuple(
        mapping
        for mapping in project.speaker_maps
        if mapping.source_id == source_id
        and mapping.transcript_version_id == transcript_version_id
    )
    speaker_map_context = tuple(mapping.to_dict() for mapping in current_maps)
    context_hash = _single_source_context_hash(
        project=project,
        source=source_context,
        persons=person_context,
        speaker_maps=speaker_map_context,
        transcript=transcript,
        brief=brief,
        allowed_operations=operations,
        project_revision=project.revision,
    )
    page = transcript.segments[offset : offset + limit]
    people_by_id = {person.person_id: person for person in project.persons}
    mappings_by_speaker = {mapping.local_speaker_id: mapping for mapping in current_maps}
    segments: tuple[dict[str, object], ...] = tuple(
        {
            "segment_id": segment.segment_id,
            "text": segment.corrected_text or segment.original_text,
            "start_ticks": segment.start_ticks,
            "end_ticks": segment.end_ticks,
            **_speaker_context(
                segment.local_speaker_id,
                mappings_by_speaker,
                people_by_id,
            ),
        }
        for segment in page
    )
    end_offset = offset + len(page)
    next_offset = end_offset if end_offset < len(transcript.segments) else None
    return AgentContextPage(
        context_hash=context_hash,
        project_id=project.project_id,
        project_revision=project.revision,
        source=source_context,
        transcript_version_id=transcript.transcript_version_id,
        brief=brief,
        settings=dict(project.settings),
        allowed_operations=operations,
        offset=offset,
        limit=limit,
        total=len(transcript.segments),
        next_offset=next_offset,
        persons=person_context,
        speaker_maps=speaker_map_context,
        segments=segments,
    )


def read_multi_source_agent_context(
    project_path: Path,
    *,
    source_bindings: list[dict[str, Any]],
    brief_id: str,
    expected_revision: int,
    offset: int,
    limit: int,
) -> MultiSourceAgentContextPage:
    bindings = parse_source_bindings(source_bindings)
    _validate_id(brief_id, "brief_id")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ProjectError("context offset must be a non-negative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_CONTEXT_PAGE:
        raise ProjectError("context limit must be between 1 and 200")

    store = ProjectStore(project_path)
    project = store.load()
    if project.revision != expected_revision:
        raise ProjectError("project revision conflict")
    if project.active_brief_id != brief_id:
        raise ProjectError("brief is not active for this project revision")
    brief = read_edit_brief(store.project_path, brief_id).brief

    transcripts: list[TimedTranscript] = []
    source_contexts: list[dict[str, object]] = []
    for binding in bindings:
        if (
            project.active_transcript_versions.get(binding.source_id)
            != binding.transcript_version_id
        ):
            raise ProjectError("transcript is not active for this project revision")
        source = _find_source(project, binding.source_id)
        transcript = _read_transcript(
            store.project_path, binding.source_id, binding.transcript_version_id
        )
        transcripts.append(transcript)
        source_contexts.append(
            {
                "source_id": source.source_id,
                "transcript_version_id": transcript.transcript_version_id,
                "display_name": source.display_name,
                "kind": source.kind,
                "duration_ticks": source.probe.duration_ticks,
                "tags": list(source.tags),
                "note": source.note,
            }
        )

    person_context = tuple(person.to_dict() for person in project.persons)
    current_maps = tuple(
        mapping
        for binding in bindings
        for mapping in project.speaker_maps
        if mapping.source_id == binding.source_id
        and mapping.transcript_version_id == binding.transcript_version_id
    )
    speaker_map_context = tuple(mapping.to_dict() for mapping in current_maps)
    operations = (
        ("select", "trim", "cross_source", "reorder")
        if brief.allow_reorder
        else ("select", "trim", "cross_source")
    )
    context_hash = _multi_source_context_hash(
        project=project,
        bindings=bindings,
        sources=source_contexts,
        persons=person_context,
        speaker_maps=speaker_map_context,
        transcripts=transcripts,
        brief=brief,
        allowed_operations=operations,
        project_revision=project.revision,
    )
    people_by_id = {person.person_id: person for person in project.persons}
    mappings_by_identity = {
        (mapping.source_id, mapping.transcript_version_id, mapping.local_speaker_id): mapping
        for mapping in current_maps
    }
    all_segments: list[dict[str, object]] = []
    for binding, transcript in zip(bindings, transcripts, strict=True):
        for segment in transcript.segments:
            mapping = (
                mappings_by_identity.get(
                    (
                        binding.source_id,
                        binding.transcript_version_id,
                        segment.local_speaker_id,
                    )
                )
                if segment.local_speaker_id is not None
                else None
            )
            speaker_map = (
                {mapping.local_speaker_id: mapping}
                if mapping is not None and segment.local_speaker_id is not None
                else {}
            )
            all_segments.append(
                {
                    "source_id": binding.source_id,
                    "transcript_version_id": binding.transcript_version_id,
                    "segment_id": segment.segment_id,
                    "text": segment.corrected_text or segment.original_text,
                    "start_ticks": segment.start_ticks,
                    "end_ticks": segment.end_ticks,
                    **_speaker_context(
                        segment.local_speaker_id,
                        speaker_map,
                        people_by_id,
                    ),
                }
            )
    page = all_segments[offset : offset + limit]
    end_offset = offset + len(page)
    next_offset = end_offset if end_offset < len(all_segments) else None
    return MultiSourceAgentContextPage(
        context_hash=context_hash,
        project_id=project.project_id,
        project_revision=project.revision,
        source_bindings=bindings,
        sources=tuple(source_contexts),
        brief=brief,
        settings=dict(project.settings),
        allowed_operations=operations,
        offset=offset,
        limit=limit,
        total=len(all_segments),
        next_offset=next_offset,
        persons=person_context,
        speaker_maps=speaker_map_context,
        segments=tuple(page),
    )


def read_revision_context(
    project_path: Path,
    *,
    expected_revision: int,
    offset: int,
    limit: int,
) -> RevisionContextPage:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ProjectError("context offset must be a non-negative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_CONTEXT_PAGE:
        raise ProjectError("context limit must be between 1 and 200")
    store = ProjectStore(project_path)
    project = store.load()
    if project.revision != expected_revision:
        raise ProjectError("project revision conflict")
    if project.active_edit_version_id is None:
        raise ProjectError("revision context requires an active Edit Decision")
    if project.active_brief_id is None:
        raise ProjectError("revision context requires an active Brief")
    decision = _read_edit_decision(store.project_path, project.active_edit_version_id)
    bindings: tuple[SourceTranscriptBinding, ...]
    sources: tuple[dict[str, object], ...]
    segments: tuple[dict[str, object], ...]
    if isinstance(decision, EditDecision):
        single_proposal = decision.proposal_snapshot
        bindings = (
            SourceTranscriptBinding(
                source_id=single_proposal.source_id,
                transcript_version_id=single_proposal.transcript_version_id,
            ),
        )
        single_context = read_agent_context(
            store.project_path,
            source_id=single_proposal.source_id,
            transcript_version_id=single_proposal.transcript_version_id,
            brief_id=project.active_brief_id,
            expected_revision=expected_revision,
            offset=offset,
            limit=limit,
        )
        sources = (
            {
                **single_context.source,
                "transcript_version_id": single_context.transcript_version_id,
            },
        )
        segments = tuple(
            {
                "source_id": single_proposal.source_id,
                "transcript_version_id": single_proposal.transcript_version_id,
                **segment,
            }
            for segment in single_context.segments
        )
        context_hash = single_context.context_hash
        project_id = single_context.project_id
        context_revision = single_context.project_revision
        brief = single_context.brief
        settings = single_context.settings
        allowed_operations = single_context.allowed_operations
        context_offset = single_context.offset
        context_limit = single_context.limit
        total = single_context.total
        next_offset = single_context.next_offset
        persons = single_context.persons
        speaker_maps = single_context.speaker_maps
        base_clips = single_proposal.clips
        base_total_duration_ticks = single_proposal.total_duration_ticks
    else:
        multi_proposal = decision.proposal_snapshot
        bindings = multi_proposal.source_bindings
        multi_context = read_multi_source_agent_context(
            store.project_path,
            source_bindings=[binding.to_dict() for binding in bindings],
            brief_id=project.active_brief_id,
            expected_revision=expected_revision,
            offset=offset,
            limit=limit,
        )
        sources = multi_context.sources
        segments = multi_context.segments
        context_hash = multi_context.context_hash
        project_id = multi_context.project_id
        context_revision = multi_context.project_revision
        brief = multi_context.brief
        settings = multi_context.settings
        allowed_operations = multi_context.allowed_operations
        context_offset = multi_context.offset
        context_limit = multi_context.limit
        total = multi_context.total
        next_offset = multi_context.next_offset
        persons = multi_context.persons
        speaker_maps = multi_context.speaker_maps
        base_clips = multi_proposal.clips
        base_total_duration_ticks = multi_proposal.total_duration_ticks
    return RevisionContextPage(
        context_hash=context_hash,
        project_id=project_id,
        project_revision=context_revision,
        base_edit_version_id=decision.edit_version_id,
        edit_schema_version=decision.schema_version,
        source_bindings=bindings,
        sources=sources,
        base_clips=base_clips,
        base_total_duration_ticks=base_total_duration_ticks,
        brief=brief,
        settings=settings,
        allowed_operations=allowed_operations,
        offset=context_offset,
        limit=context_limit,
        total=total,
        next_offset=next_offset,
        persons=persons,
        speaker_maps=speaker_maps,
        segments=segments,
    )


def _multi_source_context_hash(
    *,
    project: Project,
    bindings: Sequence[SourceTranscriptBinding],
    sources: Sequence[dict[str, object]],
    persons: Sequence[dict[str, object]],
    speaker_maps: Sequence[dict[str, object]],
    transcripts: Sequence[TimedTranscript],
    brief: EditBrief,
    allowed_operations: Sequence[str],
    project_revision: int,
) -> str:
    hash_payload = {
        "schema_version": 2,
        "kind": "multi_source_agent_context",
        "project_id": project.project_id,
        "project_revision": project_revision,
        "settings": project.settings,
        "source_bindings": [binding.to_dict() for binding in bindings],
        "sources": list(sources),
        "persons": list(persons),
        "speaker_maps": list(speaker_maps),
        "transcripts": [transcript.to_dict() for transcript in transcripts],
        "brief": brief.to_dict(),
        "allowed_operations": list(allowed_operations),
    }
    encoded = json.dumps(
        hash_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def calculate_agent_context_hash(
    project_path: Path,
    *,
    project: Project,
    bindings: Sequence[SourceTranscriptBinding],
    brief: EditBrief,
) -> str:
    """Calculate the existing schema-1/2 Agent Context hash for a Project snapshot."""

    if not bindings:
        raise ProjectError("agent context requires source bindings")
    if project.active_brief_id != brief.brief_id:
        raise ProjectError("brief is not active for this project revision")
    source_ids = [binding.source_id for binding in bindings]
    if len(source_ids) != len(set(source_ids)):
        raise ProjectError("agent context bindings require unique source IDs")

    persons = tuple(person.to_dict() for person in project.persons)
    relevant_maps = tuple(
        mapping
        for binding in bindings
        for mapping in project.speaker_maps
        if mapping.source_id == binding.source_id
        and mapping.transcript_version_id == binding.transcript_version_id
    )
    speaker_maps = tuple(mapping.to_dict() for mapping in relevant_maps)
    transcripts: list[TimedTranscript] = []
    sources: list[dict[str, object]] = []
    for binding in bindings:
        if project.active_transcript_versions.get(binding.source_id) != binding.transcript_version_id:
            raise ProjectError("transcript is not active for this project revision")
        source = _find_source(project, binding.source_id)
        transcript = _read_transcript(
            project_path, binding.source_id, binding.transcript_version_id
        )
        transcripts.append(transcript)
        sources.append(
            {
                "source_id": source.source_id,
                "transcript_version_id": transcript.transcript_version_id,
                "display_name": source.display_name,
                "kind": source.kind,
                "duration_ticks": source.probe.duration_ticks,
                "tags": list(source.tags),
                "note": source.note,
            }
        )

    if len(bindings) == 1:
        allowed_operations: tuple[str, ...] = (
            ("select", "trim", "reorder")
            if brief.allow_reorder
            else ("select", "trim")
        )
        single_source_context = dict(sources[0])
        single_source_context.pop("transcript_version_id")
        return _single_source_context_hash(
            project=project,
            source=single_source_context,
            persons=persons,
            speaker_maps=speaker_maps,
            transcript=transcripts[0],
            brief=brief,
            allowed_operations=allowed_operations,
            project_revision=project.revision,
        )

    allowed_operations = (
        ("select", "trim", "cross_source", "reorder")
        if brief.allow_reorder
        else ("select", "trim", "cross_source")
    )
    return _multi_source_context_hash(
        project=project,
        bindings=bindings,
        sources=sources,
        persons=persons,
        speaker_maps=speaker_maps,
        transcripts=transcripts,
        brief=brief,
        allowed_operations=allowed_operations,
        project_revision=project.revision,
    )


def _single_source_context_hash(
    *,
    project: Project,
    source: dict[str, object],
    persons: Sequence[dict[str, object]],
    speaker_maps: Sequence[dict[str, object]],
    transcript: TimedTranscript,
    brief: EditBrief,
    allowed_operations: Sequence[str],
    project_revision: int,
) -> str:
    hash_payload = {
        "schema_version": 1,
        "project_id": project.project_id,
        "project_revision": project_revision,
        "settings": project.settings,
        "source": source,
        "persons": list(persons),
        "speaker_maps": list(speaker_maps),
        "transcript": transcript.to_dict(),
        "brief": brief.to_dict(),
        "allowed_operations": list(allowed_operations),
    }
    encoded = json.dumps(
        hash_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _speaker_context(
    local_speaker_id: str | None,
    mappings: dict[str, SpeakerMap],
    people: dict[str, Person],
) -> dict[str, object]:
    mapping = mappings.get(local_speaker_id) if local_speaker_id is not None else None
    if mapping is None:
        return {
            "speaker": local_speaker_id,
            "local_speaker_id": local_speaker_id,
            "person_id": None,
            "person_name": None,
        }
    person = people.get(mapping.person_id)
    if person is None:
        raise ProjectError("speaker map person is not part of the project")
    return {
        "speaker": person.name,
        "local_speaker_id": local_speaker_id,
        "person_id": person.person_id,
        "person_name": person.name,
    }


def _read_transcript(project_path: Path, source_id: str, transcript_id: str) -> TimedTranscript:
    data = read_json_object(
        project_path / "transcripts" / source_id / f"{transcript_id}.json",
        description="timed transcript",
    )
    transcript = TimedTranscript.from_dict(data)
    if transcript.source_id != source_id or transcript.transcript_version_id != transcript_id:
        raise ProjectError("transcript identity mismatch")
    return transcript


def _read_edit_decision(project_path: Path, edit_version_id: str) -> EditDecisionLike:
    _validate_id(edit_version_id, "edit_version_id")
    data = read_json_object(
        project_path / "edits" / f"{edit_version_id}.json",
        description="edit decision",
    )
    schema = data.get("schema_version")
    if schema == 1:
        decision: EditDecisionLike = EditDecision.from_dict(data)
    elif schema == 2:
        decision = MultiSourceEditDecision.from_dict(data)
    else:
        raise ProjectError("unsupported edit decision schema")
    if decision.edit_version_id != edit_version_id:
        raise ProjectError("edit decision identity mismatch")
    return decision


def _find_source(project: Project, source_id: str) -> SourceAsset:
    for source in project.sources:
        if source.source_id == source_id:
            return source
    raise ProjectError("source is not part of the project")


def _validate_id(value: str, name: str) -> None:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ProjectError(f"{name} is invalid")
