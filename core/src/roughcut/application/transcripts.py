"""Immutable transcript correction versions and active-version state."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from roughcut.adapters.artifact_store import read_json_object, write_new_json
from roughcut.adapters.project_store import ProjectStore
from roughcut.application.workflows import synchronize_active_workflow_transcript_binding
from roughcut.domain.edit import EditDecision, MultiSourceEditDecision
from roughcut.domain.people import SpeakerMap, validate_safe_id
from roughcut.domain.project import Project, ProjectError
from roughcut.domain.transcript import TimedTranscript


@dataclass(frozen=True)
class TranscriptMutation:
    changed: bool
    project_revision: int
    transcript: TimedTranscript

    def to_dict(self) -> dict[str, object]:
        return {
            "changed": self.changed,
            "project_revision": self.project_revision,
            "transcript": _transcript_summary(self.transcript),
        }


@dataclass(frozen=True)
class TranscriptVersionActivation:
    source_id: str
    transcript_version_id: str
    project_revision: int
    changed: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "transcript_version_id": self.transcript_version_id,
            "project_revision": self.project_revision,
            "changed": self.changed,
        }


@dataclass(frozen=True)
class TranscriptVersionSummary:
    transcript_version_id: str
    parent_version_id: str | None
    segment_count: int
    active: bool
    kind: str

    def to_dict(self) -> dict[str, object]:
        return {
            "transcript_version_id": self.transcript_version_id,
            "parent_version_id": self.parent_version_id,
            "segment_count": self.segment_count,
            "active": self.active,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class EditReferenceMismatch:
    source_id: str
    referenced_transcript_version_id: str
    active_transcript_version_id: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "referenced_transcript_version_id": self.referenced_transcript_version_id,
            "active_transcript_version_id": self.active_transcript_version_id,
        }


@dataclass(frozen=True)
class EditReferenceStatus:
    status: str
    mismatches: tuple[EditReferenceMismatch, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "mismatches": [mismatch.to_dict() for mismatch in self.mismatches],
        }


@dataclass(frozen=True)
class TranscriptVersionsState:
    source_id: str
    active_transcript_version_id: str | None
    project_revision: int
    versions: tuple[TranscriptVersionSummary, ...]
    edit_reference_status: EditReferenceStatus

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "active_transcript_version_id": self.active_transcript_version_id,
            "project_revision": self.project_revision,
            "versions": [version.to_dict() for version in self.versions],
            "edit_reference_status": self.edit_reference_status.to_dict(),
        }


def correct_transcript(
    project_path: Path,
    *,
    source_id: str,
    parent_transcript_version_id: str,
    corrections: list[dict[str, Any]],
    expected_revision: int,
) -> TranscriptMutation:
    validate_safe_id(source_id, "source_id")
    validate_safe_id(parent_transcript_version_id, "parent_transcript_version_id")
    store, project = _current_project(project_path, expected_revision)
    _project_source(project, source_id)
    if project.active_transcript_versions.get(source_id) != parent_transcript_version_id:
        raise ProjectError("parent transcript is not active for the source")
    parent = _read_transcript(store.project_path, source_id, parent_transcript_version_id)
    source_transcripts = _read_source_transcripts(store.project_path, source_id)
    _validate_version_chain(store.project_path, project, source_id, source_transcripts)
    updates = _parse_corrections(corrections, parent)
    segments = tuple(
        replace(segment, corrected_text=updates.get(segment.segment_id, segment.corrected_text))
        for segment in parent.segments
    )
    if segments == parent.segments:
        return TranscriptMutation(False, project.revision, parent)

    transcript_id = f"tr_{uuid4().hex}"
    child = replace(
        parent,
        transcript_version_id=transcript_id,
        parent_version_id=parent.transcript_version_id,
        segments=segments,
    )
    child_path = _transcript_path(store.project_path, source_id, transcript_id)
    write_new_json(child_path, child.to_dict())
    inherited_maps = tuple(
        SpeakerMap(
            source_id=mapping.source_id,
            transcript_version_id=transcript_id,
            local_speaker_id=mapping.local_speaker_id,
            person_id=mapping.person_id,
            confirmed_by_user=mapping.confirmed_by_user,
        )
        for mapping in project.speaker_maps
        if mapping.source_id == source_id
        and mapping.transcript_version_id == parent_transcript_version_id
    )
    updated = replace(
        project,
        revision=project.revision + 1,
        updated_at=datetime.now(UTC).isoformat(),
        active_transcript_versions={
            **project.active_transcript_versions,
            source_id: transcript_id,
        },
        speaker_maps=(*project.speaker_maps, *inherited_maps),
    )
    try:
        store.save(updated, expected_revision=expected_revision)
    except Exception:
        child_path.unlink(missing_ok=True)
        raise
    synchronize_active_workflow_transcript_binding(
        store.project_path, source_id, transcript_id
    )
    return TranscriptMutation(True, updated.revision, child)


def activate_transcript_version(
    project_path: Path,
    *,
    source_id: str,
    transcript_version_id: str,
    expected_revision: int,
) -> TranscriptVersionActivation:
    validate_safe_id(source_id, "source_id")
    validate_safe_id(transcript_version_id, "transcript_version_id")
    store, project = _current_project(project_path, expected_revision)
    _project_source(project, source_id)
    _read_transcript(store.project_path, source_id, transcript_version_id)
    source_transcripts = _read_source_transcripts(store.project_path, source_id)
    _validate_version_chain(store.project_path, project, source_id, source_transcripts)
    if project.active_transcript_versions.get(source_id) == transcript_version_id:
        return TranscriptVersionActivation(
            source_id, transcript_version_id, project.revision, False
        )
    updated = replace(
        project,
        revision=project.revision + 1,
        updated_at=datetime.now(UTC).isoformat(),
        active_transcript_versions={
            **project.active_transcript_versions,
            source_id: transcript_version_id,
        },
    )
    store.save(updated, expected_revision=expected_revision)
    synchronize_active_workflow_transcript_binding(
        store.project_path, source_id, transcript_version_id
    )
    return TranscriptVersionActivation(source_id, transcript_version_id, updated.revision, True)


def read_transcript_versions(project_path: Path, *, source_id: str) -> TranscriptVersionsState:
    validate_safe_id(source_id, "source_id")
    store = ProjectStore(project_path)
    project = store.load()
    _project_source(project, source_id)
    transcripts = _read_source_transcripts(store.project_path, source_id)
    _validate_version_chain(store.project_path, project, source_id, transcripts)
    active_id = project.active_transcript_versions.get(source_id)
    if active_id is not None and active_id not in transcripts:
        raise ProjectError("active transcript is missing or unreadable")
    versions = tuple(
        TranscriptVersionSummary(
            transcript_version_id=transcript.transcript_version_id,
            parent_version_id=transcript.parent_version_id,
            segment_count=len(transcript.segments),
            active=transcript.transcript_version_id == active_id,
            kind="original_asr" if transcript.parent_version_id is None else "user_correction",
        )
        for transcript in transcripts.values()
    )
    return TranscriptVersionsState(
        source_id=source_id,
        active_transcript_version_id=active_id,
        project_revision=project.revision,
        versions=versions,
        edit_reference_status=_edit_reference_status(store.project_path, project),
    )


def _parse_corrections(
    corrections: list[dict[str, Any]], parent: TimedTranscript
) -> dict[str, str | None]:
    if not isinstance(corrections, list) or not corrections:
        raise ProjectError("corrections must be a non-empty list")
    known_segments = {segment.segment_id for segment in parent.segments}
    parsed: dict[str, str | None] = {}
    for correction in corrections:
        if not isinstance(correction, dict):
            raise ProjectError("each correction must be an object")
        segment_id = correction.get("segment_id")
        if not isinstance(segment_id, str):
            raise ProjectError("correction segment_id is required")
        validate_safe_id(segment_id, "segment_id")
        if segment_id in parsed:
            raise ProjectError("corrections contain a duplicate segment_id")
        if segment_id not in known_segments:
            raise ProjectError("correction segment does not exist")
        if "corrected_text" not in correction:
            raise ProjectError("correction corrected_text is required")
        corrected_text = correction["corrected_text"]
        if corrected_text is None:
            parsed[segment_id] = None
        elif isinstance(corrected_text, str):
            normalized = corrected_text.strip()
            if not normalized:
                raise ProjectError("corrected_text must not be blank")
            parsed[segment_id] = normalized
        else:
            raise ProjectError("corrected_text must be a string or null")
    return parsed


def _current_project(project_path: Path, expected_revision: int) -> tuple[ProjectStore, Project]:
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise ProjectError("expected_revision must be a non-negative integer")
    store = ProjectStore(project_path)
    project = store.load()
    if project.revision != expected_revision:
        raise ProjectError("project revision conflict")
    return store, project


def _project_source(project: Project, source_id: str) -> None:
    if not any(source.source_id == source_id for source in project.sources):
        raise ProjectError("source is not part of the project")


def _transcript_path(project_path: Path, source_id: str, transcript_id: str) -> Path:
    return project_path / "transcripts" / source_id / f"{transcript_id}.json"


def _read_transcript(project_path: Path, source_id: str, transcript_id: str) -> TimedTranscript:
    data = read_json_object(
        _transcript_path(project_path, source_id, transcript_id),
        description="timed transcript",
    )
    transcript = TimedTranscript.from_dict(data)
    if transcript.source_id != source_id or transcript.transcript_version_id != transcript_id:
        raise ProjectError("transcript identity does not match its path")
    return transcript


def _read_source_transcripts(
    project_path: Path, source_id: str
) -> dict[str, TimedTranscript]:
    directory = project_path / "transcripts" / source_id
    if not directory.exists():
        return {}
    transcripts: dict[str, TimedTranscript] = {}
    for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
        transcript_id = path.stem
        validate_safe_id(transcript_id, "transcript_version_id")
        transcripts[transcript_id] = _read_transcript(project_path, source_id, transcript_id)
    return transcripts


def _validate_version_chain(
    project_path: Path,
    project: Project,
    source_id: str,
    transcripts: dict[str, TimedTranscript],
) -> None:
    for transcript in transcripts.values():
        parent_id = transcript.parent_version_id
        if parent_id is None:
            continue
        validate_safe_id(parent_id, "parent_version_id")
        if parent_id == transcript.transcript_version_id:
            raise ProjectError("transcript version cannot reference itself")
        if parent_id not in transcripts:
            for source in project.sources:
                if source.source_id == source_id:
                    continue
                if _transcript_path(project_path, source.source_id, parent_id).exists():
                    raise ProjectError("transcript parent belongs to another source")
            raise ProjectError("transcript parent is missing")

    visiting: set[str] = set()
    complete: set[str] = set()

    def visit(transcript_id: str) -> None:
        if transcript_id in complete:
            return
        if transcript_id in visiting:
            raise ProjectError("transcript version chain contains a cycle")
        visiting.add(transcript_id)
        parent_id = transcripts[transcript_id].parent_version_id
        if parent_id is not None:
            visit(parent_id)
        visiting.remove(transcript_id)
        complete.add(transcript_id)

    for transcript_id in transcripts:
        visit(transcript_id)


def _edit_reference_status(project_path: Path, project: Project) -> EditReferenceStatus:
    edit_id = project.active_edit_version_id
    if edit_id is None:
        return EditReferenceStatus("none", ())
    validate_safe_id(edit_id, "edit_version_id")
    data = read_json_object(
        project_path / "edits" / f"{edit_id}.json",
        description="active edit decision",
    )
    schema_version = data.get("schema_version")
    bindings: tuple[tuple[str, str], ...]
    if schema_version == 1:
        single_decision = EditDecision.from_dict(data)
        if single_decision.edit_version_id != edit_id:
            raise ProjectError("active edit decision identity mismatch")
        bindings = (
            (
                single_decision.proposal_snapshot.source_id,
                single_decision.proposal_snapshot.transcript_version_id,
            ),
        )
    elif schema_version == 2:
        multi_decision = MultiSourceEditDecision.from_dict(data)
        if multi_decision.edit_version_id != edit_id:
            raise ProjectError("active edit decision identity mismatch")
        bindings = tuple(
            (binding.source_id, binding.transcript_version_id)
            for binding in multi_decision.proposal_snapshot.source_bindings
        )
    else:
        raise ProjectError("active edit decision schema is unsupported")

    mismatches: list[EditReferenceMismatch] = []
    for source_id, referenced_id in bindings:
        _project_source(project, source_id)
        _read_transcript(project_path, source_id, referenced_id)
        active_id = project.active_transcript_versions.get(source_id)
        if active_id != referenced_id:
            mismatches.append(EditReferenceMismatch(source_id, referenced_id, active_id))
    return EditReferenceStatus("stale" if mismatches else "current", tuple(mismatches))


def _transcript_summary(transcript: TimedTranscript) -> dict[str, object]:
    return {
        "transcript_version_id": transcript.transcript_version_id,
        "source_id": transcript.source_id,
        "parent_version_id": transcript.parent_version_id,
        "segment_count": len(transcript.segments),
    }
