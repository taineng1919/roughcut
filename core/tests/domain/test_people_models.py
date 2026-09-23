from __future__ import annotations

import pytest

from roughcut.domain.people import Person, SpeakerMap
from roughcut.domain.project import Project, ProjectError, SourceAsset


def _source_data() -> dict[str, object]:
    return {
        "source_id": "src_a",
        "kind": "audio",
        "display_name": "采访.wav",
        "import_mode": "linked",
        "locator": {"absolute_path": "/fixture/采访.wav"},
        "fingerprint": {"size": 10, "mtime_ns": 1, "sha256_head_tail": "fixture"},
        "probe": {
            "duration_ticks": 120_000,
            "container_start_ticks": 0,
            "first_content_ticks": 0,
            "video_codec": None,
            "width": None,
            "height": None,
            "nominal_frame_rate": None,
            "is_vfr": False,
            "audio_codec": "pcm_s16le",
            "audio_sample_rate": 16_000,
            "rotation_degrees": 0,
        },
    }


def _project_data() -> dict[str, object]:
    return {
        "schema_version": 1,
        "project_id": "proj_people",
        "revision": 0,
        "name": "People",
        "created_at": "fixture",
        "updated_at": "fixture",
        "settings": {
            "timebase": 120_000,
            "frame_rate": {"numerator": 25, "denominator": 1},
            "width": 1920,
            "height": 1080,
            "audio_sample_rate": 48_000,
        },
        "sources": [_source_data()],
        "active_transcript_versions": {"src_a": "tr_a"},
        "active_brief_id": None,
        "active_edit_version_id": None,
    }


def test_schema_v1_people_and_source_metadata_are_backward_compatible() -> None:
    project = Project.from_dict(_project_data())

    assert project.persons == ()
    assert project.speaker_maps == ()
    assert project.active_content_draft_id is None
    assert project.sources[0].tags == ()
    assert project.sources[0].note == ""
    stored = project.to_dict()
    assert stored["persons"] == []
    assert stored["speaker_maps"] == []
    assert stored["active_content_draft_id"] is None
    source = stored["sources"][0]  # type: ignore[index]
    assert source["tags"] == []
    assert source["note"] == ""
    assert Project.from_dict(stored) == project


def test_source_tags_are_trimmed_deduplicated_and_stable() -> None:
    data = _source_data()
    data["tags"] = ["  嘉宾 ", "", "校园", "嘉宾", "   ", "校园 "]
    data["note"] = "保留原样的备注"

    source = SourceAsset.from_dict(data)

    assert source.tags == ("嘉宾", "校园")
    assert source.note == "保留原样的备注"
    assert source.to_dict()["tags"] == ["嘉宾", "校园"]


@pytest.mark.parametrize(
    ("person_id", "name", "role", "note"),
    [
        ("../person", "人物", "guest", ""),
        ("person_a", " ", "guest", ""),
        ("person_a", "人物", " ", ""),
        ("person_a", "人物", "guest", None),
    ],
)
def test_person_rejects_unsafe_or_empty_fields(
    person_id: str, name: str, role: str, note: object
) -> None:
    with pytest.raises(ProjectError):
        Person(person_id=person_id, name=name, role=role, note=note)  # type: ignore[arg-type]


def test_speaker_map_identity_is_source_transcript_and_local_speaker() -> None:
    first = SpeakerMap(
        source_id="src_a",
        transcript_version_id="tr_a",
        local_speaker_id="spk_0",
        person_id="person_guest",
        confirmed_by_user=True,
    )
    second_source = SpeakerMap(
        source_id="src_b",
        transcript_version_id="tr_b",
        local_speaker_id="spk_0",
        person_id="person_guest",
        confirmed_by_user=True,
    )

    assert first.identity_key == ("src_a", "tr_a", "spk_0")
    assert second_source.identity_key == ("src_b", "tr_b", "spk_0")
    assert first.identity_key != second_source.identity_key
    with pytest.raises(ProjectError, match="confirmed"):
        SpeakerMap(
            source_id="src_a",
            transcript_version_id="tr_a",
            local_speaker_id="spk_0",
            person_id="person_guest",
            confirmed_by_user=False,
        )


def test_project_rejects_duplicate_person_and_speaker_map_identities() -> None:
    data = _project_data()
    person = {"person_id": "person_guest", "name": "嘉宾", "role": "guest", "note": ""}
    mapping = {
        "source_id": "src_a",
        "transcript_version_id": "tr_a",
        "local_speaker_id": "spk_0",
        "person_id": "person_guest",
        "confirmed_by_user": True,
    }
    data["persons"] = [person, person]
    data["speaker_maps"] = [mapping]
    with pytest.raises(ProjectError, match="duplicate person"):
        Project.from_dict(data)

    data["persons"] = [person]
    data["speaker_maps"] = [mapping, mapping]
    with pytest.raises(ProjectError, match="duplicate speaker map"):
        Project.from_dict(data)
