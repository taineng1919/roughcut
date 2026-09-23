from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from roughcut.adapters.project_store import ProjectStore
from roughcut.application.agent_context import (
    create_edit_brief,
    read_agent_context,
    read_edit_brief,
)
from roughcut.application.people import (
    confirm_speaker_map,
    create_person,
    update_source_metadata,
)
from roughcut.application.projects import create_project
from roughcut.domain.brief import EditBrief
from roughcut.domain.people import SpeakerMap
from roughcut.domain.project import (
    ImportMode,
    MediaProbe,
    ProjectError,
    SourceAsset,
    SourceFingerprint,
)
from roughcut.domain.transcript import (
    TimedTranscript,
    TranscriptProvenance,
    TranscriptSegment,
)
from roughcut.mcp import handle_request


def _project_with_transcript(tmp_path: Path) -> tuple[Path, str, str, int]:
    project_path = tmp_path / "agent context project"
    project = create_project(project_path, "Agent context")
    source_id = "src_context"
    transcript_id = "tr_context"
    source = SourceAsset(
        source_id=source_id,
        kind="audio",
        display_name="访谈素材.wav",
        import_mode=ImportMode.LINKED,
        locator={"absolute_path": "/private/user/media/访谈素材.wav"},
        fingerprint=SourceFingerprint(100, 1, "fixture"),
        probe=MediaProbe(
            duration_ticks=720_000,
            container_start_ticks=360_000,
            first_content_ticks=360_000,
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
    segments = tuple(
        TranscriptSegment(
            segment_id=f"seg_{index:06d}",
            start_ticks=(index - 1) * 120_000,
            end_ticks=index * 120_000,
            original_text=text,
            corrected_text=None,
            local_speaker_id="spk_0" if index % 2 else None,
            person_id=None,
            confidence=None,
            fine_units=(),
            editorial_mark="unmarked",
        )
        for index, text in enumerate(
            ["开场介绍校园。", "图书馆适合学习。", "实验室设备完善。", "操场空间很大。", "总结探校体验。"],
            start=1,
        )
    )
    transcript = TimedTranscript(
        schema_version=1,
        transcript_version_id=transcript_id,
        source_id=source_id,
        parent_version_id=None,
        provenance=TranscriptProvenance(
            backend="funasr",
            package_version="1.3.8",
            models={},
            parameters={},
            raw_result_path=f"raw-asr/{source_id}/fixture.json",
            started_at="fixture",
            completed_at="fixture",
            exit_status=0,
        ),
        language="zh-CN",
        segments=segments,
    )
    transcript_path = project_path / "transcripts" / source_id / f"{transcript_id}.json"
    transcript_path.parent.mkdir(parents=True)
    transcript_path.write_text(
        json.dumps(transcript.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    imported = replace(
        project,
        revision=1,
        sources=(source,),
        active_transcript_versions={source_id: transcript_id},
    )
    ProjectStore(project_path).save(imported, expected_revision=0)
    return project_path, source_id, transcript_id, imported.revision


@pytest.mark.parametrize(
    "kwargs",
    [
        {"theme": " ", "target_duration_ticks": 120_000, "focus": ("校园",), "allow_reorder": False},
        {"theme": "探校", "target_duration_ticks": 0, "focus": ("校园",), "allow_reorder": False},
        {"theme": "探校", "target_duration_ticks": 120_000, "focus": (), "allow_reorder": False},
        {"theme": "探校", "target_duration_ticks": 120_000, "focus": (" ",), "allow_reorder": False},
        {"theme": "探校", "target_duration_ticks": 120_000, "focus": ("校园",), "allow_reorder": 1},
    ],
)
def test_edit_brief_rejects_invalid_minimal_fields(kwargs: dict[str, object]) -> None:
    with pytest.raises(ProjectError):
        EditBrief(brief_id="brief_fixture", **kwargs)  # type: ignore[arg-type]


def test_brief_creation_is_atomic_revisioned_and_readable(tmp_path: Path) -> None:
    project_path, _source_id, _transcript_id, revision = _project_with_transcript(tmp_path)

    state = create_edit_brief(
        project_path,
        theme="突出校园学习环境",
        target_duration_ticks=360_000,
        focus=["图书馆", "实验室"],
        allow_reorder=False,
        expected_revision=revision,
    )

    assert state.project_revision == revision + 1
    assert state.brief.to_dict() == {
        "schema_version": 1,
        "brief_id": state.brief.brief_id,
        "theme": "突出校园学习环境",
        "target_duration_ticks": 360_000,
        "focus": ["图书馆", "实验室"],
        "allow_reorder": False,
    }
    assert read_edit_brief(project_path, state.brief.brief_id) == state
    stored = ProjectStore(project_path).load()
    assert stored.active_brief_id == state.brief.brief_id
    assert stored.revision == revision + 1

    with pytest.raises(ProjectError, match="revision conflict"):
        create_edit_brief(
            project_path,
            theme="过期写入",
            target_duration_ticks=120_000,
            focus=["不得落盘"],
            allow_reorder=True,
            expected_revision=revision,
        )
    assert len(list((project_path / "briefs").glob("*.json"))) == 1


def test_context_pages_are_bounded_private_and_share_a_stable_hash(tmp_path: Path) -> None:
    project_path, source_id, transcript_id, revision = _project_with_transcript(tmp_path)
    brief = create_edit_brief(
        project_path,
        theme="突出校园学习环境",
        target_duration_ticks=360_000,
        focus=["图书馆", "实验室"],
        allow_reorder=False,
        expected_revision=revision,
    )

    first = read_agent_context(
        project_path,
        source_id=source_id,
        transcript_version_id=transcript_id,
        brief_id=brief.brief.brief_id,
        expected_revision=brief.project_revision,
        offset=0,
        limit=2,
    )
    second = read_agent_context(
        project_path,
        source_id=source_id,
        transcript_version_id=transcript_id,
        brief_id=brief.brief.brief_id,
        expected_revision=brief.project_revision,
        offset=2,
        limit=2,
    )

    assert first.context_hash == second.context_hash
    assert first.next_offset == 2
    assert second.next_offset == 4
    assert [segment["segment_id"] for segment in first.segments] == [
        "seg_000001",
        "seg_000002",
    ]
    assert first.segments[0] == {
        "segment_id": "seg_000001",
        "text": "开场介绍校园。",
        "start_ticks": 0,
        "end_ticks": 120_000,
        "speaker": "spk_0",
        "local_speaker_id": "spk_0",
        "person_id": None,
        "person_name": None,
    }
    assert first.persons == ()
    assert first.speaker_maps == ()
    assert first.allowed_operations == ("select", "trim")
    payload = json.dumps(first.to_dict(), ensure_ascii=False)
    assert str(project_path) not in payload
    assert "/private/user/media" not in payload
    assert "locator" not in payload
    assert "fingerprint" not in payload
    assert "raw-asr" not in payload
    assert first.source == {
        "source_id": source_id,
        "display_name": "访谈素材.wav",
        "kind": "audio",
        "duration_ticks": 720_000,
        "tags": [],
        "note": "",
    }


def test_context_dynamically_resolves_confirmed_people_and_hashes_people_state(
    tmp_path: Path,
) -> None:
    project_path, source_id, transcript_id, revision = _project_with_transcript(tmp_path)
    brief = create_edit_brief(
        project_path,
        theme="校园",
        target_duration_ticks=360_000,
        focus=["人物"],
        allow_reorder=False,
        expected_revision=revision,
    )
    original = read_agent_context(
        project_path,
        source_id=source_id,
        transcript_version_id=transcript_id,
        brief_id=brief.brief.brief_id,
        expected_revision=brief.project_revision,
        offset=0,
        limit=2,
    )
    transcript_path = project_path / "transcripts" / source_id / f"{transcript_id}.json"
    transcript_before = transcript_path.read_bytes()

    metadata = update_source_metadata(
        project_path,
        source_id=source_id,
        tags=[" 主访谈 ", "校园", "主访谈"],
        note="主持人与嘉宾",
        expected_revision=brief.project_revision,
    )
    tagged = read_agent_context(
        project_path,
        source_id=source_id,
        transcript_version_id=transcript_id,
        brief_id=brief.brief.brief_id,
        expected_revision=metadata.state.project_revision,
        offset=0,
        limit=2,
    )
    assert tagged.context_hash != original.context_hash
    assert tagged.source["tags"] == ["主访谈", "校园"]
    assert tagged.source["note"] == "主持人与嘉宾"

    person = create_person(
        project_path,
        name="嘉宾 A",
        role="guest",
        note="",
        expected_revision=metadata.state.project_revision,
    )
    with_person = read_agent_context(
        project_path,
        source_id=source_id,
        transcript_version_id=transcript_id,
        brief_id=brief.brief.brief_id,
        expected_revision=person.state.project_revision,
        offset=0,
        limit=2,
    )
    assert with_person.context_hash != tagged.context_hash
    assert with_person.persons == (
        {
            "person_id": person.state.persons[0].person_id,
            "name": "嘉宾 A",
            "role": "guest",
            "note": "",
        },
    )
    assert with_person.segments[0]["speaker"] == "spk_0"
    assert with_person.segments[0]["person_id"] is None

    mapping = confirm_speaker_map(
        project_path,
        source_id=source_id,
        transcript_version_id=transcript_id,
        local_speaker_id="spk_0",
        person_id=person.state.persons[0].person_id,
        confirmed_by_user=True,
        expected_revision=person.state.project_revision,
    )
    mapped = read_agent_context(
        project_path,
        source_id=source_id,
        transcript_version_id=transcript_id,
        brief_id=brief.brief.brief_id,
        expected_revision=mapping.state.project_revision,
        offset=0,
        limit=2,
    )
    assert mapped.context_hash != with_person.context_hash
    assert mapped.speaker_maps == (mapping.state.speaker_maps[0].to_dict(),)
    assert mapped.segments[0] == {
        "segment_id": "seg_000001",
        "text": "开场介绍校园。",
        "start_ticks": 0,
        "end_ticks": 120_000,
        "speaker": "嘉宾 A",
        "local_speaker_id": "spk_0",
        "person_id": person.state.persons[0].person_id,
        "person_name": "嘉宾 A",
    }
    assert transcript_path.read_bytes() == transcript_before


def test_context_does_not_apply_another_sources_spk_zero_mapping(tmp_path: Path) -> None:
    project_path, source_id, transcript_id, revision = _project_with_transcript(tmp_path)
    brief = create_edit_brief(
        project_path,
        theme="校园",
        target_duration_ticks=360_000,
        focus=["人物"],
        allow_reorder=False,
        expected_revision=revision,
    )
    person = create_person(
        project_path,
        name="另一素材人物",
        role="guest",
        note="",
        expected_revision=brief.project_revision,
    )
    project = ProjectStore(project_path).load()
    source_b = replace(
        project.sources[0],
        source_id="src_other",
        display_name="另一素材.wav",
        locator={"absolute_path": "/private/user/media/另一素材.wav"},
    )
    transcript_data = json.loads(
        (project_path / "transcripts" / source_id / f"{transcript_id}.json").read_text(
            encoding="utf-8"
        )
    )
    transcript_data["source_id"] = "src_other"
    transcript_data["transcript_version_id"] = "tr_other"
    other_path = project_path / "transcripts" / "src_other" / "tr_other.json"
    other_path.parent.mkdir(parents=True)
    other_path.write_text(json.dumps(transcript_data, ensure_ascii=False), encoding="utf-8")
    mapping = SpeakerMap(
        source_id="src_other",
        transcript_version_id="tr_other",
        local_speaker_id="spk_0",
        person_id=person.state.persons[0].person_id,
        confirmed_by_user=True,
    )
    updated = replace(
        project,
        revision=project.revision + 1,
        sources=(*project.sources, source_b),
        active_transcript_versions={
            **project.active_transcript_versions,
            "src_other": "tr_other",
        },
        speaker_maps=(mapping,),
    )
    ProjectStore(project_path).save(updated, expected_revision=project.revision)

    context = read_agent_context(
        project_path,
        source_id=source_id,
        transcript_version_id=transcript_id,
        brief_id=brief.brief.brief_id,
        expected_revision=updated.revision,
        offset=0,
        limit=1,
    )

    assert context.speaker_maps == ()
    assert context.segments[0]["speaker"] == "spk_0"
    assert context.segments[0]["person_id"] is None


def test_context_hash_changes_with_revision_transcript_brief_and_operations(
    tmp_path: Path,
) -> None:
    project_path, source_id, transcript_id, revision = _project_with_transcript(tmp_path)
    brief = create_edit_brief(
        project_path,
        theme="校园",
        target_duration_ticks=360_000,
        focus=["学习环境"],
        allow_reorder=False,
        expected_revision=revision,
    )

    original = read_agent_context(
        project_path,
        source_id=source_id,
        transcript_version_id=transcript_id,
        brief_id=brief.brief.brief_id,
        expected_revision=brief.project_revision,
        offset=0,
        limit=2,
    )
    project = ProjectStore(project_path).load()
    ProjectStore(project_path).save(
        replace(project, revision=project.revision + 1), expected_revision=project.revision
    )
    revised = read_agent_context(
        project_path,
        source_id=source_id,
        transcript_version_id=transcript_id,
        brief_id=brief.brief.brief_id,
        expected_revision=brief.project_revision + 1,
        offset=0,
        limit=2,
    )
    assert revised.context_hash != original.context_hash

    transcript_path = project_path / "transcripts" / source_id / f"{transcript_id}.json"
    transcript_data = json.loads(transcript_path.read_text(encoding="utf-8"))
    transcript_data["segments"][0]["original_text"] = "转录内容变化。"
    transcript_path.write_text(json.dumps(transcript_data), encoding="utf-8")
    changed_transcript = read_agent_context(
        project_path,
        source_id=source_id,
        transcript_version_id=transcript_id,
        brief_id=brief.brief.brief_id,
        expected_revision=brief.project_revision + 1,
        offset=0,
        limit=2,
    )
    assert changed_transcript.context_hash != revised.context_hash

    reordered = create_edit_brief(
        project_path,
        theme="校园新主题",
        target_duration_ticks=360_000,
        focus=["学习环境"],
        allow_reorder=True,
        expected_revision=brief.project_revision + 1,
    )
    changed_brief = read_agent_context(
        project_path,
        source_id=source_id,
        transcript_version_id=transcript_id,
        brief_id=reordered.brief.brief_id,
        expected_revision=reordered.project_revision,
        offset=0,
        limit=2,
    )
    assert changed_brief.context_hash != changed_transcript.context_hash
    assert changed_brief.allowed_operations == ("select", "trim", "reorder")


def test_context_requires_current_revision_active_brief_and_transcript(tmp_path: Path) -> None:
    project_path, source_id, transcript_id, revision = _project_with_transcript(tmp_path)
    brief = create_edit_brief(
        project_path,
        theme="校园",
        target_duration_ticks=360_000,
        focus=["学习环境"],
        allow_reorder=False,
        expected_revision=revision,
    )

    for kwargs in (
        {"expected_revision": revision},
        {"expected_revision": brief.project_revision, "brief_id": "brief_unknown"},
        {"expected_revision": brief.project_revision, "transcript_version_id": "tr_unknown"},
    ):
        arguments = {
            "source_id": source_id,
            "transcript_version_id": transcript_id,
            "brief_id": brief.brief.brief_id,
            "expected_revision": brief.project_revision,
            "offset": 0,
            "limit": 2,
            **kwargs,
        }
        with pytest.raises(ProjectError):
            read_agent_context(project_path, **arguments)


def test_cli_and_mcp_context_are_semantically_equivalent(tmp_path: Path) -> None:
    project_path, source_id, transcript_id, revision = _project_with_transcript(tmp_path)
    brief = create_edit_brief(
        project_path,
        theme="校园",
        target_duration_ticks=360_000,
        focus=["学习环境"],
        allow_reorder=False,
        expected_revision=revision,
    )
    cli = subprocess.run(
        [
            sys.executable,
            "-m",
            "roughcut.cli",
            "agent-context",
            "--project",
            str(project_path),
            "--source-id",
            source_id,
            "--transcript-id",
            transcript_id,
            "--brief-id",
            brief.brief.brief_id,
            "--expected-revision",
            str(brief.project_revision),
            "--offset",
            "0",
            "--limit",
            "2",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert cli.returncode == 0, cli.stderr
    cli_payload = json.loads(cli.stdout)

    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "agent_context",
                "arguments": {
                    "project_path": str(project_path),
                    "source_id": source_id,
                    "transcript_version_id": transcript_id,
                    "brief_id": brief.brief.brief_id,
                    "expected_revision": brief.project_revision,
                    "offset": 0,
                    "limit": 2,
                },
            },
        }
    )
    assert response is not None
    result = response["result"]
    assert isinstance(result, dict)
    assert result["structuredContent"] == cli_payload


def test_application_seeded_brief_has_mcp_then_cli_readback(tmp_path: Path) -> None:
    project_path, _source_id, _transcript_id, revision = _project_with_transcript(tmp_path)
    seeded = create_edit_brief(
        project_path,
        theme="学习空间",
        target_duration_ticks=240_000,
        focus=["图书馆"],
        allow_reorder=False,
        expected_revision=revision,
    )
    created = seeded.to_dict()
    brief = created["brief"]
    assert isinstance(brief, dict)

    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "brief_read",
                "arguments": {
                    "project_path": str(project_path),
                    "brief_id": brief["brief_id"],
                },
            },
        }
    )
    assert response is not None
    result = response["result"]
    assert isinstance(result, dict)
    mcp_payload = result["structuredContent"]
    assert isinstance(mcp_payload, dict)
    assert mcp_payload["brief"] == created["brief"]
    assert mcp_payload["project_revision"] == created["project_revision"]

    cli = subprocess.run(
        [
            sys.executable,
            "-m",
            "roughcut.cli",
            "brief-read",
            "--project",
            str(project_path),
            "--brief-id",
            str(brief["brief_id"]),
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert cli.returncode == 0, cli.stderr
    assert json.loads(cli.stdout) == mcp_payload


def test_application_seeded_brief_has_cli_then_mcp_readback(tmp_path: Path) -> None:
    project_path, _source_id, _transcript_id, revision = _project_with_transcript(tmp_path)
    seeded = create_edit_brief(
        project_path,
        theme="学习空间",
        target_duration_ticks=240_000,
        focus=["图书馆"],
        allow_reorder=False,
        expected_revision=revision,
    )
    created = seeded.to_dict()
    brief = created["brief"]
    assert isinstance(brief, dict)

    cli = subprocess.run(
        [
            sys.executable,
            "-m",
            "roughcut.cli",
            "brief-read",
            "--project",
            str(project_path),
            "--brief-id",
            str(brief["brief_id"]),
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert cli.returncode == 0, cli.stderr
    cli_payload = json.loads(cli.stdout)
    assert cli_payload["brief"] == created["brief"]
    assert cli_payload["project_revision"] == created["project_revision"]

    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "brief_read",
                "arguments": {
                    "project_path": str(project_path),
                    "brief_id": brief["brief_id"],
                },
            },
        }
    )
    assert response is not None
    result = response["result"]
    assert isinstance(result, dict)
    assert result["structuredContent"] == cli_payload
