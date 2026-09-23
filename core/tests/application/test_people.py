from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from roughcut.adapters.project_store import ProjectStore
from roughcut.application.people import (
    confirm_speaker_map,
    create_person,
    read_people,
    update_source_metadata,
)
from roughcut.application.projects import create_project
from roughcut.domain.asr import ASR_CLOUD_TAG, merge_cloud_route_tag
from roughcut.domain.project import (
    ImportMode,
    MediaProbe,
    ProjectError,
    SourceAsset,
    SourceFingerprint,
)
from roughcut.domain.transcript import TimedTranscript, TranscriptProvenance, TranscriptSegment


def _transcript(source_id: str, transcript_id: str, *speakers: str) -> TimedTranscript:
    return TimedTranscript(
        schema_version=1,
        transcript_version_id=transcript_id,
        source_id=source_id,
        parent_version_id=None,
        provenance=TranscriptProvenance(
            backend="fixture",
            package_version="fixture",
            models={},
            parameters={},
            raw_result_path=f"raw-asr/{source_id}/fixture.json",
            started_at="fixture",
            completed_at="fixture",
            exit_status=0,
        ),
        language="zh-CN",
        segments=tuple(
            TranscriptSegment(
                segment_id=f"seg_{source_id}_{index}",
                start_ticks=index * 120_000,
                end_ticks=(index + 1) * 120_000,
                original_text=f"{source_id} speaker {speaker}",
                corrected_text=None,
                local_speaker_id=speaker,
                person_id=None,
                confidence=None,
                fine_units=(),
                editorial_mark="unmarked",
            )
            for index, speaker in enumerate(speakers)
        ),
    )


def _project_with_two_transcripts(tmp_path: Path) -> tuple[Path, dict[str, str], int]:
    project_path = tmp_path / "人物 fixture"
    project = create_project(project_path, "人物 fixture")
    source_ids = ("src_a", "src_b")
    transcript_ids = ("tr_a_current", "tr_b_current")
    sources = tuple(
        SourceAsset(
            source_id=source_id,
            kind="audio",
            display_name=f"{source_id}.wav",
            import_mode=ImportMode.LINKED,
            locator={"absolute_path": f"/private/fixture/{source_id}.wav"},
            fingerprint=SourceFingerprint(100 + index, index, f"fingerprint-{index}"),
            probe=MediaProbe(
                duration_ticks=600_000,
                container_start_ticks=0,
                first_content_ticks=0,
                video_codec=None,
                width=None,
                height=None,
                nominal_frame_rate=None,
                is_vfr=False,
                audio_codec="pcm_s16le",
                audio_sample_rate=16_000,
                rotation_degrees=0,
            ),
        )
        for index, source_id in enumerate(source_ids)
    )
    transcripts = (
        _transcript("src_a", "tr_a_current", "spk_0", "spk_1"),
        _transcript("src_b", "tr_b_current", "spk_0"),
        _transcript("src_a", "tr_a_old", "spk_0"),
    )
    for transcript in transcripts:
        path = (
            project_path
            / "transcripts"
            / transcript.source_id
            / f"{transcript.transcript_version_id}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(transcript.to_dict(), ensure_ascii=False), encoding="utf-8")
    imported = replace(
        project,
        revision=1,
        sources=sources,
        active_transcript_versions=dict(zip(source_ids, transcript_ids, strict=True)),
    )
    ProjectStore(project_path).save(imported, expected_revision=0)
    return (
        project_path,
        {
            "source_a": "src_a",
            "source_b": "src_b",
            "transcript_a": "tr_a_current",
            "transcript_b": "tr_b_current",
            "inactive_a": "tr_a_old",
        },
        imported.revision,
    )


def test_create_read_and_source_metadata_are_revisioned(tmp_path: Path) -> None:
    project_path, ids, revision = _project_with_two_transcripts(tmp_path)

    created = create_person(
        project_path,
        name="  嘉宾 A  ",
        role=" guest ",
        note="第一位嘉宾",
        expected_revision=revision,
    )
    assert created.changed is True
    assert created.change == "created"
    assert created.state.project_revision == revision + 1
    person = created.state.persons[0]
    assert person.name == "嘉宾 A"
    assert person.role == "guest"

    metadata = update_source_metadata(
        project_path,
        source_id=ids["source_a"],
        tags=[" 嘉宾 ", "", "校园", "嘉宾", "  "],
        note="主访谈",
        expected_revision=created.state.project_revision,
    )
    assert metadata.change == "updated"
    assert metadata.state.project_revision == revision + 2
    summary = read_people(project_path)
    assert summary == metadata.state
    source = next(item for item in summary.sources if item["source_id"] == ids["source_a"])
    assert source == {
        "source_id": "src_a",
        "display_name": "src_a.wav",
        "tags": ["嘉宾", "校园"],
        "note": "主访谈",
    }
    stored_source = ProjectStore(project_path).load().sources[0]
    assert stored_source.locator == {"absolute_path": "/private/fixture/src_a.wav"}
    assert stored_source.fingerprint.sha256_head_tail == "fingerprint-0"
    assert stored_source.probe.duration_ticks == 600_000


def test_source_metadata_updates_display_name_with_tags_and_note_once(tmp_path: Path) -> None:
    project_path, ids, revision = _project_with_two_transcripts(tmp_path)

    metadata = update_source_metadata(
        project_path,
        source_id=ids["source_a"],
        display_name="  校长 开场  ",
        tags=[" role:main ", "content:talk"],
        note="主线素材",
        expected_revision=revision,
    )

    assert metadata.state.project_revision == revision + 1
    source = ProjectStore(project_path).load().sources[0]
    assert source.display_name == "校长 开场"
    assert source.tags == ("role:main", "content:talk")
    assert source.note == "主线素材"


def test_source_metadata_explicit_cloud_merge_preserves_tags_and_is_idempotent(
    tmp_path: Path,
) -> None:
    project_path, ids, revision = _project_with_two_transcripts(tmp_path)
    project = ProjectStore(project_path).load()
    seeded_source = replace(
        project.sources[0],
        display_name="主线原始名称",
        tags=("role:main", "audio:good"),
        note="主线原始备注",
    )
    seeded = replace(
        project,
        revision=revision + 1,
        sources=(seeded_source, project.sources[1]),
    )
    ProjectStore(project_path).save(seeded, expected_revision=revision)

    current = ProjectStore(project_path).load()
    first = update_source_metadata(
        project_path,
        source_id=ids["source_a"],
        tags=list(merge_cloud_route_tag(current.sources[0].tags, enabled=True)),
        note=current.sources[0].note,
        expected_revision=current.revision,
    )
    current = ProjectStore(project_path).load()
    second = update_source_metadata(
        project_path,
        source_id=ids["source_a"],
        tags=list(merge_cloud_route_tag(current.sources[0].tags, enabled=True)),
        note=current.sources[0].note,
        expected_revision=first.state.project_revision,
    )

    marked = ProjectStore(project_path).load().sources[0]
    assert marked.tags == ("role:main", "audio:good", ASR_CLOUD_TAG)
    assert marked.display_name == "主线原始名称"
    assert marked.note == "主线原始备注"
    assert second.state.project_revision == seeded.revision + 2

    current = ProjectStore(project_path).load()
    removed = update_source_metadata(
        project_path,
        source_id=ids["source_a"],
        tags=list(merge_cloud_route_tag(current.sources[0].tags, enabled=False)),
        note=current.sources[0].note,
        expected_revision=second.state.project_revision,
    )
    unmarked = ProjectStore(project_path).load().sources[0]
    assert unmarked.tags == ("role:main", "audio:good")
    assert unmarked.display_name == "主线原始名称"
    assert unmarked.note == "主线原始备注"
    assert removed.state.project_revision == seeded.revision + 3


def test_source_metadata_display_name_change_does_not_rederive_or_remove_marker(
    tmp_path: Path,
) -> None:
    project_path, ids, revision = _project_with_two_transcripts(tmp_path)

    updated = update_source_metadata(
        project_path,
        source_id=ids["source_a"],
        display_name="友好名称",
        tags=[ASR_CLOUD_TAG],
        note="",
        expected_revision=revision,
    )

    source = ProjectStore(project_path).load().sources[0]
    assert source.display_name == "友好名称"
    assert source.tags == (ASR_CLOUD_TAG,)
    assert updated.state.project_revision == revision + 1


def test_source_metadata_does_not_derive_cloud_marker_from_friendly_display_name(
    tmp_path: Path,
) -> None:
    project_path, ids, revision = _project_with_two_transcripts(tmp_path)

    updated = update_source_metadata(
        project_path,
        source_id=ids["source_a"],
        display_name="采访02__方言",
        tags=["role:main"],
        note="",
        expected_revision=revision,
    )

    source = ProjectStore(project_path).load().sources[0]
    assert source.display_name == "采访02__方言"
    assert ASR_CLOUD_TAG not in source.tags
    assert updated.state.project_revision == revision + 1


def test_explicit_cloud_batch_uses_each_previous_revision_and_preserves_each_source_tags(
    tmp_path: Path,
) -> None:
    project_path, ids, revision = _project_with_two_transcripts(tmp_path)
    project = ProjectStore(project_path).load()
    seeded = replace(
        project,
        revision=revision + 1,
        sources=(
            replace(
                project.sources[0],
                display_name="主线原始名称",
                tags=("role:main", "audio:good"),
                note="主线备注",
            ),
            replace(
                project.sources[1],
                display_name="补充原始名称",
                tags=("role:aux", "audio:room"),
                note="补充备注",
            ),
        ),
    )
    ProjectStore(project_path).save(seeded, expected_revision=revision)

    current = ProjectStore(project_path).load()
    first_source = current.sources[0]
    first = update_source_metadata(
        project_path,
        source_id=ids["source_a"],
        tags=list(merge_cloud_route_tag(first_source.tags, enabled=True)),
        note=first_source.note,
        expected_revision=current.revision,
    )
    current = ProjectStore(project_path).load()
    second_source = current.sources[1]
    second = update_source_metadata(
        project_path,
        source_id=ids["source_b"],
        tags=list(merge_cloud_route_tag(second_source.tags, enabled=True)),
        note=second_source.note,
        expected_revision=first.state.project_revision,
    )

    assert second.state.project_revision == seeded.revision + 2
    sources = ProjectStore(project_path).load().sources
    assert sources[0].tags == ("role:main", "audio:good", ASR_CLOUD_TAG)
    assert sources[0].display_name == "主线原始名称"
    assert sources[0].note == "主线备注"
    assert sources[1].tags == ("role:aux", "audio:room", ASR_CLOUD_TAG)
    assert sources[1].display_name == "补充原始名称"
    assert sources[1].note == "补充备注"
    with pytest.raises(ProjectError, match="revision conflict"):
        update_source_metadata(
            project_path,
            source_id=ids["source_a"],
            tags=list(sources[0].tags),
            note="stale",
            expected_revision=revision,
        )


def test_source_metadata_allows_duplicate_friendly_names(tmp_path: Path) -> None:
    project_path, ids, revision = _project_with_two_transcripts(tmp_path)

    first = update_source_metadata(
        project_path,
        source_id=ids["source_a"],
        display_name="采访 现场",
        tags=[],
        note="",
        expected_revision=revision,
    )
    second = update_source_metadata(
        project_path,
        source_id=ids["source_b"],
        display_name="采访 现场",
        tags=[],
        note="",
        expected_revision=first.state.project_revision,
    )

    assert [source.display_name for source in ProjectStore(project_path).load().sources] == [
        "采访 现场",
        "采访 现场",
    ]
    assert second.state.project_revision == revision + 2


def test_source_metadata_preserves_linked_and_copied_locations(tmp_path: Path) -> None:
    project_path, ids, revision = _project_with_two_transcripts(tmp_path)
    project = ProjectStore(project_path).load()
    linked = replace(
        project.sources[0],
        locator={"absolute_path": "/Volumes/拍摄 A/采访一.wav"},
    )
    copied = replace(
        project.sources[1],
        import_mode=ImportMode.COPIED,
        locator={"project_relative_path": "sources/采访二.wav"},
    )
    prepared = replace(project, revision=revision + 1, sources=(linked, copied))
    ProjectStore(project_path).save(prepared, expected_revision=revision)

    first = update_source_metadata(
        project_path,
        source_id=ids["source_a"],
        display_name="外盘 主访谈",
        tags=["role:main"],
        note="linked",
        expected_revision=prepared.revision,
    )
    update_source_metadata(
        project_path,
        source_id=ids["source_b"],
        display_name="项目内 补充",
        tags=["role:supplement"],
        note="copied",
        expected_revision=first.state.project_revision,
    )

    stored = ProjectStore(project_path).load().sources
    assert stored[0].import_mode is ImportMode.LINKED
    assert stored[0].locator == {"absolute_path": "/Volumes/拍摄 A/采访一.wav"}
    assert stored[1].import_mode is ImportMode.COPIED
    assert stored[1].locator == {"project_relative_path": "sources/采访二.wav"}


@pytest.mark.parametrize("display_name", ["", "   ", 42])
def test_source_metadata_rejects_invalid_display_name(
    tmp_path: Path, display_name: object
) -> None:
    project_path, ids, revision = _project_with_two_transcripts(tmp_path)
    before = (project_path / "project.json").read_bytes()

    with pytest.raises(ProjectError, match="display_name"):
        update_source_metadata(
            project_path,
            source_id=ids["source_a"],
            display_name=display_name,  # type: ignore[arg-type]
            tags=[],
            note="",
            expected_revision=revision,
        )

    assert (project_path / "project.json").read_bytes() == before


def test_source_metadata_failure_preserves_source_and_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_path, ids, revision = _project_with_two_transcripts(tmp_path)
    artifact_paths = [
        project_path / "transcripts" / ids["source_a"] / f"{ids['transcript_a']}.json",
        project_path / "proxies" / ids["source_a"] / "fixture" / "manifest.json",
        project_path / "proposals" / "proposal_fixture.json",
        project_path / "edits" / "edit_fixture.json",
    ]
    for index, path in enumerate(artifact_paths):
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(f"fixture-{index}".encode())
    project_before = (project_path / "project.json").read_bytes()
    artifacts_before = {path: path.read_bytes() for path in artifact_paths}
    source_before = ProjectStore(project_path).load().sources[0]

    def fail_save(*_args: object, **_kwargs: object) -> None:
        raise OSError("fixture save failure")

    monkeypatch.setattr(ProjectStore, "save", fail_save)
    with pytest.raises(OSError, match="fixture save failure"):
        update_source_metadata(
            project_path,
            source_id=ids["source_a"],
            display_name="不得保存",
            tags=["role:main"],
            note="不得保存",
            expected_revision=revision,
        )

    assert (project_path / "project.json").read_bytes() == project_before
    assert {path: path.read_bytes() for path in artifact_paths} == artifacts_before
    source_after = ProjectStore(project_path).load().sources[0]
    assert source_after == source_before


def test_two_sources_spk_zero_remain_separate_until_each_confirmation(tmp_path: Path) -> None:
    project_path, ids, revision = _project_with_two_transcripts(tmp_path)
    guest_a = create_person(
        project_path, name="嘉宾 A", role="guest", note="", expected_revision=revision
    )
    guest_b = create_person(
        project_path,
        name="嘉宾 B",
        role="guest",
        note="",
        expected_revision=guest_a.state.project_revision,
    )
    assert read_people(project_path).speaker_maps == ()

    mapped_a = confirm_speaker_map(
        project_path,
        source_id=ids["source_a"],
        transcript_version_id=ids["transcript_a"],
        local_speaker_id="spk_0",
        person_id=guest_a.state.persons[0].person_id,
        confirmed_by_user=True,
        expected_revision=guest_b.state.project_revision,
    )
    mapped_b = confirm_speaker_map(
        project_path,
        source_id=ids["source_b"],
        transcript_version_id=ids["transcript_b"],
        local_speaker_id="spk_0",
        person_id=guest_b.state.persons[1].person_id,
        confirmed_by_user=True,
        expected_revision=mapped_a.state.project_revision,
    )

    assert [mapping.identity_key for mapping in mapped_b.state.speaker_maps] == [
        ("src_a", "tr_a_current", "spk_0"),
        ("src_b", "tr_b_current", "spk_0"),
    ]
    assert [mapping.person_id for mapping in mapped_b.state.speaker_maps] == [
        guest_a.state.persons[0].person_id,
        guest_b.state.persons[1].person_id,
    ]
    remapped_a = confirm_speaker_map(
        project_path,
        source_id=ids["source_a"],
        transcript_version_id=ids["transcript_a"],
        local_speaker_id="spk_0",
        person_id=guest_b.state.persons[1].person_id,
        confirmed_by_user=True,
        expected_revision=mapped_b.state.project_revision,
    )
    assert remapped_a.state.persons == mapped_b.state.persons
    assert remapped_a.state.speaker_maps[1] == mapped_b.state.speaker_maps[1]


def test_same_mapping_is_idempotent_and_remap_is_explicit(tmp_path: Path) -> None:
    project_path, ids, revision = _project_with_two_transcripts(tmp_path)
    first_person = create_person(
        project_path, name="甲", role="guest", note="", expected_revision=revision
    )
    second_person = create_person(
        project_path,
        name="乙",
        role="guest",
        note="",
        expected_revision=first_person.state.project_revision,
    )
    transcript_path = project_path / "transcripts" / "src_a" / "tr_a_current.json"
    transcript_before = transcript_path.read_bytes()
    mapped = confirm_speaker_map(
        project_path,
        source_id=ids["source_a"],
        transcript_version_id=ids["transcript_a"],
        local_speaker_id="spk_0",
        person_id=first_person.state.persons[0].person_id,
        confirmed_by_user=True,
        expected_revision=second_person.state.project_revision,
    )
    unchanged = confirm_speaker_map(
        project_path,
        source_id=ids["source_a"],
        transcript_version_id=ids["transcript_a"],
        local_speaker_id="spk_0",
        person_id=first_person.state.persons[0].person_id,
        confirmed_by_user=True,
        expected_revision=mapped.state.project_revision,
    )
    assert unchanged.changed is False
    assert unchanged.change == "unchanged"
    assert unchanged.state.project_revision == mapped.state.project_revision

    remapped = confirm_speaker_map(
        project_path,
        source_id=ids["source_a"],
        transcript_version_id=ids["transcript_a"],
        local_speaker_id="spk_0",
        person_id=second_person.state.persons[1].person_id,
        confirmed_by_user=True,
        expected_revision=unchanged.state.project_revision,
    )
    assert remapped.changed is True
    assert remapped.change == "remapped"
    assert remapped.state.project_revision == unchanged.state.project_revision + 1
    assert remapped.state.speaker_maps[0].person_id == second_person.state.persons[1].person_id
    assert transcript_path.read_bytes() == transcript_before


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_id": "src_unknown"},
        {"transcript_version_id": "tr_unknown"},
        {"transcript_version_id": "tr_b_current"},
        {"transcript_version_id": "tr_a_old"},
        {"local_speaker_id": "spk_unknown"},
        {"person_id": "person_unknown"},
        {"confirmed_by_user": False},
        {"source_id": "../src_a"},
        {"transcript_version_id": "../tr_a_current"},
        {"local_speaker_id": "../spk_0"},
        {"person_id": "../person"},
    ],
)
def test_speaker_confirmation_rejects_unknown_inactive_unconfirmed_and_unsafe_inputs(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    project_path, ids, revision = _project_with_two_transcripts(tmp_path)
    person = create_person(
        project_path, name="嘉宾", role="guest", note="", expected_revision=revision
    )
    arguments: dict[str, object] = {
        "source_id": ids["source_a"],
        "transcript_version_id": ids["transcript_a"],
        "local_speaker_id": "spk_0",
        "person_id": person.state.persons[0].person_id,
        "confirmed_by_user": True,
        "expected_revision": person.state.project_revision,
        **overrides,
    }
    with pytest.raises(ProjectError):
        confirm_speaker_map(project_path, **arguments)  # type: ignore[arg-type]
    assert ProjectStore(project_path).load().revision == person.state.project_revision
    assert ProjectStore(project_path).load().speaker_maps == ()


def test_people_writes_reject_stale_revision(tmp_path: Path) -> None:
    project_path, ids, revision = _project_with_two_transcripts(tmp_path)
    person = create_person(
        project_path, name="嘉宾", role="guest", note="", expected_revision=revision
    )

    with pytest.raises(ProjectError, match="revision conflict"):
        update_source_metadata(
            project_path,
            source_id=ids["source_a"],
            tags=["stale"],
            note="stale",
            expected_revision=revision,
        )
    with pytest.raises(ProjectError, match="revision conflict"):
        confirm_speaker_map(
            project_path,
            source_id=ids["source_a"],
            transcript_version_id=ids["transcript_a"],
            local_speaker_id="spk_0",
            person_id=person.state.persons[0].person_id,
            confirmed_by_user=True,
            expected_revision=revision,
        )


def test_source_metadata_rejects_a_string_instead_of_a_tag_list(tmp_path: Path) -> None:
    project_path, ids, revision = _project_with_two_transcripts(tmp_path)

    with pytest.raises(ProjectError, match="tags must be a list"):
        update_source_metadata(
            project_path,
            source_id=ids["source_a"],
            tags="访谈",  # type: ignore[arg-type]
            note="",
            expected_revision=revision,
        )

    assert ProjectStore(project_path).load().revision == revision


def test_save_failure_leaves_project_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_path, _ids, revision = _project_with_two_transcripts(tmp_path)
    before = (project_path / "project.json").read_bytes()

    def fail_save(*_args: object, **_kwargs: object) -> None:
        raise OSError("fixture save failure")

    monkeypatch.setattr(ProjectStore, "save", fail_save)
    with pytest.raises(OSError, match="fixture save failure"):
        create_person(
            project_path,
            name="不得保存",
            role="guest",
            note="",
            expected_revision=revision,
        )

    assert (project_path / "project.json").read_bytes() == before
