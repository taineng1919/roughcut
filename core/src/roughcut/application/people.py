"""Revisioned people, source metadata, and confirmed speaker-map services."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from roughcut.adapters.artifact_store import read_json_object
from roughcut.adapters.project_store import ProjectStore
from roughcut.domain.people import Person, SpeakerMap, validate_safe_id
from roughcut.domain.project import Project, ProjectError, SourceAsset
from roughcut.domain.transcript import TimedTranscript


@dataclass(frozen=True)
class PeopleState:
    project_id: str
    project_revision: int
    persons: tuple[Person, ...]
    sources: tuple[dict[str, object], ...]
    speaker_maps: tuple[SpeakerMap, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "project_revision": self.project_revision,
            "persons": [person.to_dict() for person in self.persons],
            "sources": list(self.sources),
            "speaker_maps": [mapping.to_dict() for mapping in self.speaker_maps],
        }


@dataclass(frozen=True)
class PeopleMutation:
    change: str
    changed: bool
    state: PeopleState

    def to_dict(self) -> dict[str, object]:
        return {
            "change": self.change,
            "changed": self.changed,
            "people": self.state.to_dict(),
        }


def create_person(
    project_path: Path,
    *,
    name: str,
    role: str,
    note: str,
    expected_revision: int,
) -> PeopleMutation:
    store, project = _current_project(project_path, expected_revision)
    if not isinstance(name, str) or not isinstance(role, str):
        raise ProjectError("person name and role must be strings")
    person = Person(
        person_id=f"person_{uuid4().hex}",
        name=name.strip(),
        role=role.strip(),
        note=note,
    )
    updated = replace(
        project,
        revision=project.revision + 1,
        updated_at=datetime.now(UTC).isoformat(),
        persons=(*project.persons, person),
    )
    store.save(updated, expected_revision=expected_revision)
    return PeopleMutation(change="created", changed=True, state=_state(updated))


def update_source_metadata(
    project_path: Path,
    *,
    source_id: str,
    tags: list[str],
    note: str,
    expected_revision: int,
    display_name: str | None = None,
) -> PeopleMutation:
    validate_safe_id(source_id, "source_id")
    if not isinstance(tags, list):
        raise ProjectError("source tags must be a list")
    if display_name is not None:
        if not isinstance(display_name, str):
            raise ProjectError("source display_name must be a string")
        display_name = display_name.strip()
        if not display_name:
            raise ProjectError("source display_name must not be blank")
    store, project = _current_project(project_path, expected_revision)
    source = _source(project, source_id)
    updated_source = replace(
        source,
        display_name=source.display_name if display_name is None else display_name,
        tags=tuple(tags),
        note=note,
    )
    updated_sources = tuple(
        updated_source if item.source_id == source_id else item for item in project.sources
    )
    updated = replace(
        project,
        revision=project.revision + 1,
        updated_at=datetime.now(UTC).isoformat(),
        sources=updated_sources,
    )
    store.save(updated, expected_revision=expected_revision)
    return PeopleMutation(change="updated", changed=True, state=_state(updated))


def confirm_speaker_map(
    project_path: Path,
    *,
    source_id: str,
    transcript_version_id: str,
    local_speaker_id: str,
    person_id: str,
    confirmed_by_user: bool,
    expected_revision: int,
) -> PeopleMutation:
    for name, value in (
        ("source_id", source_id),
        ("transcript_version_id", transcript_version_id),
        ("local_speaker_id", local_speaker_id),
        ("person_id", person_id),
    ):
        validate_safe_id(value, name)
    store, project = _current_project(project_path, expected_revision)
    _source(project, source_id)
    if not any(person.person_id == person_id for person in project.persons):
        raise ProjectError("person is not part of the project")
    transcript = _transcript(store.project_path, source_id, transcript_version_id)
    if project.active_transcript_versions.get(source_id) != transcript_version_id:
        raise ProjectError("transcript is not active for the source")
    if not any(segment.local_speaker_id == local_speaker_id for segment in transcript.segments):
        raise ProjectError("local speaker does not exist in the transcript")
    mapping = SpeakerMap(
        source_id=source_id,
        transcript_version_id=transcript_version_id,
        local_speaker_id=local_speaker_id,
        person_id=person_id,
        confirmed_by_user=confirmed_by_user,
    )
    existing_index = next(
        (
            index
            for index, existing in enumerate(project.speaker_maps)
            if existing.identity_key == mapping.identity_key
        ),
        None,
    )
    if existing_index is not None and project.speaker_maps[existing_index].person_id == person_id:
        return PeopleMutation(change="unchanged", changed=False, state=_state(project))

    mappings = list(project.speaker_maps)
    if existing_index is None:
        mappings.append(mapping)
        change = "created"
    else:
        mappings[existing_index] = mapping
        change = "remapped"
    updated = replace(
        project,
        revision=project.revision + 1,
        updated_at=datetime.now(UTC).isoformat(),
        speaker_maps=tuple(mappings),
    )
    store.save(updated, expected_revision=expected_revision)
    return PeopleMutation(change=change, changed=True, state=_state(updated))


def read_people(project_path: Path) -> PeopleState:
    return _state(ProjectStore(project_path).load())


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


def _source(project: Project, source_id: str) -> SourceAsset:
    for source in project.sources:
        if source.source_id == source_id:
            return source
    raise ProjectError("source is not part of the project")


def _transcript(project_path: Path, source_id: str, transcript_id: str) -> TimedTranscript:
    data = read_json_object(
        project_path / "transcripts" / source_id / f"{transcript_id}.json",
        description="timed transcript",
    )
    transcript = TimedTranscript.from_dict(data)
    if transcript.source_id != source_id or transcript.transcript_version_id != transcript_id:
        raise ProjectError("transcript does not belong to the source")
    return transcript


def _state(project: Project) -> PeopleState:
    return PeopleState(
        project_id=project.project_id,
        project_revision=project.revision,
        persons=project.persons,
        sources=tuple(
            {
                "source_id": source.source_id,
                "display_name": source.display_name,
                "tags": list(source.tags),
                "note": source.note,
            }
            for source in project.sources
        ),
        speaker_maps=project.speaker_maps,
    )
