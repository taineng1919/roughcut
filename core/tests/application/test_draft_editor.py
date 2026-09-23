from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

import roughcut.application.content_drafts as content_drafts_module
import roughcut.application.draft_editor as draft_editor_module
from roughcut.adapters.artifact_store import write_new_json
from roughcut.adapters.project_store import ProjectStore
from roughcut.application.agent_context import (
    create_edit_brief,
    read_agent_context,
    read_multi_source_agent_context,
)
from roughcut.application.content_drafts import (
    confirm_content_draft,
    create_content_draft,
    create_content_draft_editor_child,
    propose_content_draft,
    publish_prepared_content_draft_editor_child,
)
from roughcut.application.draft_editor import (
    edit_draft_candidate,
    load_draft_editor_snapshot,
    materialize_draft_editor_placement,
    prepare_draft_candidate,
    prepare_draft_candidate_with_placement,
    prepare_draft_punctuation,
    read_draft_transcript_window,
    resolve_draft_editor_caret,
    resolve_draft_editor_selection,
    search_draft_editor,
    update_draft_narration,
)
from roughcut.application.draft_workspaces import prepare_section_operation
from roughcut.application.projects import create_project
from roughcut.application.sources import fingerprint_file
from roughcut.domain.content_draft import ContentDraftRef, SourceExcerptBlock
from roughcut.domain.people import Person, SpeakerMap
from roughcut.domain.project import ImportMode, MediaProbe, ProjectError, SourceAsset
from roughcut.domain.transcript import (
    FineUnit,
    TimedTranscript,
    TranscriptProvenance,
    TranscriptSegment,
)


def _source(
    source_id: str,
    media: Path,
    *,
    duration_ticks: int = 1_200_000,
) -> SourceAsset:
    return SourceAsset(
        source_id=source_id,
        kind="video",
        display_name=f"素材 {source_id}",
        import_mode=ImportMode.LINKED,
        locator={"absolute_path": str(media.resolve())},
        fingerprint=fingerprint_file(media),
        probe=MediaProbe(
            duration_ticks=duration_ticks,
            container_start_ticks=0,
            first_content_ticks=0,
            video_codec="h264",
            width=320,
            height=180,
            nominal_frame_rate={"numerator": 25, "denominator": 1},
            is_vfr=False,
            audio_codec="aac",
            audio_sample_rate=48_000,
            rotation_degrees=0,
        ),
        tags=(f"tag:{source_id}",),
        note=f"备注 {source_id}",
    )


def _segment(
    segment_id: str,
    start: int,
    end: int,
    text: str,
    speaker: str,
    *,
    with_units: bool,
) -> TranscriptSegment:
    units = (
        tuple(
            FineUnit(
                "character",
                character,
                start + index * 20_000,
                start + (index + 1) * 20_000,
                None,
            )
            for index, character in enumerate(text)
        )
        if with_units
        else ()
    )
    return TranscriptSegment(
        segment_id=segment_id,
        start_ticks=start,
        end_ticks=end,
        original_text=text,
        corrected_text=None,
        local_speaker_id=speaker,
        person_id=None,
        confidence=None,
        fine_units=units,
        editorial_mark="unmarked",
    )


def _transcript(
    source_id: str,
    transcript_id: str,
    *,
    extra_segments: int = 0,
) -> TimedTranscript:
    if source_id == "src_a":
        base_segments = (
            _segment("seg_a_fine", 0, 60_000, "你😀好", "spk_0", with_units=True),
            _segment(
                "seg_a_coarse",
                400_000,
                500_000,
                "没有细分",
                "spk_0",
                with_units=False,
            ),
            _segment(
                "seg_a_other",
                800_000,
                880_000,
                "另一人物",
                "spk_1",
                with_units=True,
            ),
        )
        generated: list[TranscriptSegment] = []
        for index in range(extra_segments):
            text = (
                "跨窗口目标中文😀。"
                if index == extra_segments - 5
                else f"长稿段落 {index:03d} 重复窗口。"
            )
            start = 1_200_000 + index * 480_000
            generated.append(
                _segment(
                    f"seg_long_{index:03d}",
                    start,
                    start + len(text) * 20_000,
                    text,
                    "spk_0" if index % 2 == 0 else "spk_1",
                    with_units=True,
                )
            )
        segments = (*base_segments, *generated)
    else:
        segments = (
            _segment("seg_b_fine", 0, 100_000, "重复你好", "spk_0", with_units=True),
        )
    return TimedTranscript(
        schema_version=1,
        transcript_version_id=transcript_id,
        source_id=source_id,
        parent_version_id=None,
        provenance=TranscriptProvenance(
            "fixture",
            "1",
            {"private": "/private/model"},
            {},
            f"raw-asr/{source_id}/private.json",
            "fixture",
            "fixture",
            0,
        ),
        language="zh-CN",
        segments=segments,
    )


def _fixture(tmp_path: Path, *, extra_segments: int = 0) -> dict[str, object]:
    root = tmp_path / "初稿 editor project"
    project = create_project(root, "初稿编辑")
    media_a = tmp_path / "素材 A.mp4"
    media_b = tmp_path / "素材 B.mp4"
    media_a.write_bytes(b"A" * 512)
    media_b.write_bytes(b"B" * 512)
    duration = 1_200_000 + max(extra_segments, 1) * 480_000
    sources = (
        _source("src_a", media_a, duration_ticks=duration),
        _source("src_b", media_b),
    )
    for source_id, transcript_id in (("src_a", "tr_a"), ("src_b", "tr_b")):
        write_new_json(
            root / "transcripts" / source_id / f"{transcript_id}.json",
            _transcript(
                source_id,
                transcript_id,
                extra_segments=extra_segments if source_id == "src_a" else 0,
            ).to_dict(),
        )
    people = (
        Person("person_a", "人物甲", "主持人", ""),
        Person("person_other", "人物乙", "老师", ""),
        Person("person_b", "人物丙", "学生", ""),
    )
    prepared = replace(
        project,
        revision=1,
        sources=sources,
        active_transcript_versions={"src_a": "tr_a", "src_b": "tr_b"},
        persons=people,
        speaker_maps=(
            SpeakerMap("src_a", "tr_a", "spk_0", "person_a", True),
            SpeakerMap("src_a", "tr_a", "spk_1", "person_other", True),
            SpeakerMap("src_b", "tr_b", "spk_0", "person_b", True),
        ),
    )
    ProjectStore(root).save(prepared, expected_revision=0)
    brief = create_edit_brief(
        root,
        theme="测试初稿",
        target_duration_ticks=360_000,
        focus=["保持精确引用"],
        allow_reorder=True,
        expected_revision=1,
    )
    bindings = [
        {"source_id": "src_a", "transcript_version_id": "tr_a"},
        {"source_id": "src_b", "transcript_version_id": "tr_b"},
    ]
    context = read_multi_source_agent_context(
        root,
        source_bindings=bindings,
        brief_id=brief.brief.brief_id,
        expected_revision=2,
        offset=0,
        limit=20,
    )
    draft = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=bindings,
        brief_id=brief.brief.brief_id,
        context_hash=context.context_hash,
        blocks=[
            {
                "block_id": "block_a_fine",
                "kind": "source_excerpt",
                "refs": [
                    {
                        "source_id": "src_a",
                        "transcript_version_id": "tr_a",
                        "segment_id": "seg_a_fine",
                        "start_ticks": 0,
                        "end_ticks": 60_000,
                    }
                ],
                "canonical_text": "你😀好",
            },
            {
                "block_id": "block_a_coarse",
                "kind": "source_excerpt",
                "refs": [
                    {
                        "source_id": "src_a",
                        "transcript_version_id": "tr_a",
                        "segment_id": "seg_a_coarse",
                        "start_ticks": 400_000,
                        "end_ticks": 500_000,
                    }
                ],
                "canonical_text": "没有细分",
            },
            {
                "block_id": "block_narration",
                "kind": "narration",
                "text": "待录音解说",
                "status": "draft",
                "recorded_refs": [],
            },
            {
                "block_id": "block_b",
                "kind": "source_excerpt",
                "refs": [
                    {
                        "source_id": "src_b",
                        "transcript_version_id": "tr_b",
                        "segment_id": "seg_b_fine",
                        "start_ticks": 0,
                        "end_ticks": 100_000,
                    }
                ],
                "canonical_text": "重复你好",
            },
        ],
        expected_revision=2,
    )
    return {
        "root": root,
        "bindings": bindings,
        "draft_id": draft.content_draft.content_draft_id,
    }


def _gap_boundary_fixture(
    tmp_path: Path,
    *,
    fine_units: tuple[FineUnit, ...] | None = None,
) -> dict[str, object]:
    fixture = _fixture(tmp_path)
    root = Path(fixture["root"])
    project = ProjectStore(root).load()
    gap_units = (
        fine_units
        if fine_units is not None
        else (
            FineUnit("character", "甲", 100, 200, None),
            FineUnit("character", " ", 200, 250, None),
            FineUnit("character", "乙", 250, 350, None),
            FineUnit("character", " ", 350, 380, None),
            FineUnit("character", "丙", 380, 480, None),
        )
    )
    transcript = TimedTranscript(
        schema_version=1,
        transcript_version_id="tr_gap",
        source_id="src_a",
        parent_version_id=None,
        provenance=TranscriptProvenance(
            "fixture",
            "1",
            {},
            {},
            "raw-asr/src_a/gap.json",
            "fixture",
            "fixture",
            0,
        ),
        language="zh-CN",
        segments=(
            TranscriptSegment(
                segment_id="seg_gap",
                start_ticks=100,
                end_ticks=500,
                original_text="甲，乙 丙。",
                corrected_text=None,
                local_speaker_id="spk_0",
                person_id=None,
                confidence=None,
                fine_units=gap_units,
                editorial_mark="unmarked",
            ),
        ),
    )
    write_new_json(
        root / "transcripts" / "src_a" / "tr_gap.json",
        transcript.to_dict(),
    )
    ProjectStore(root).save(
        replace(
            project,
            revision=project.revision + 1,
            active_transcript_versions={
                **project.active_transcript_versions,
                "src_a": "tr_gap",
            },
        ),
        expected_revision=project.revision,
    )
    project = ProjectStore(root).load()
    bindings = [{"source_id": "src_a", "transcript_version_id": "tr_gap"}]
    assert project.active_brief_id is not None
    context = read_agent_context(
        root,
        source_id="src_a",
        transcript_version_id="tr_gap",
        brief_id=project.active_brief_id,
        expected_revision=project.revision,
        offset=0,
        limit=20,
    )
    draft = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=bindings,
        brief_id=project.active_brief_id,
        context_hash=context.context_hash,
        blocks=[
            {"block_id": "gap_section", "kind": "section_title", "title": "原章节"},
            {
                "block_id": "gap_block",
                "kind": "source_excerpt",
                "refs": [
                    {
                        "source_id": "src_a",
                        "transcript_version_id": "tr_gap",
                        "segment_id": "seg_gap",
                        "start_ticks": 100,
                        "end_ticks": 500,
                    }
                ],
                "canonical_text": "甲，乙 丙。",
            },
        ],
        expected_revision=project.revision,
    )
    return {
        "root": root,
        "bindings": bindings,
        "draft_id": draft.content_draft.content_draft_id,
    }


def _resolved_display_range_fixture(
    tmp_path: Path,
    *,
    display_text: str | None = None,
) -> dict[str, object]:
    fixture = _fixture(tmp_path)
    root = Path(fixture["root"])
    project = ProjectStore(root).load()
    transcript = TimedTranscript(
        schema_version=1,
        transcript_version_id="tr_resolved",
        source_id="src_a",
        parent_version_id=None,
        provenance=TranscriptProvenance(
            "fixture", "1", {}, {}, "raw-asr/src_a/resolved.json", "fixture", "fixture", 0
        ),
        language="zh-CN",
        segments=(
            TranscriptSegment(
                segment_id="seg_resolved",
                start_ticks=300_000,
                end_ticks=422_727,
                original_text="主持人说：“请大家",
                corrected_text=None,
                local_speaker_id="spk_0",
                person_id=None,
                confidence=None,
                fine_units=(
                    FineUnit("character", "主持人说请大家", 300_000, 422_727, None),
                ),
                editorial_mark="unmarked",
            ),
        ),
    )
    write_new_json(root / "transcripts" / "src_a" / "tr_resolved.json", transcript.to_dict())
    ProjectStore(root).save(
        replace(
            project,
            revision=project.revision + 1,
            active_transcript_versions={
                **project.active_transcript_versions,
                "src_a": "tr_resolved",
            },
        ),
        expected_revision=project.revision,
    )
    project = ProjectStore(root).load()
    bindings = [{"source_id": "src_a", "transcript_version_id": "tr_resolved"}]
    assert project.active_brief_id is not None
    context = read_agent_context(
        root,
        source_id="src_a",
        transcript_version_id="tr_resolved",
        brief_id=project.active_brief_id,
        expected_revision=project.revision,
        offset=0,
        limit=20,
    )
    draft = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=bindings,
        brief_id=project.active_brief_id,
        context_hash=context.context_hash,
        blocks=[
            {
                "block_id": "resolved_block",
                "kind": "source_excerpt",
                "refs": [{
                    "source_id": "src_a",
                    "transcript_version_id": "tr_resolved",
                    "segment_id": "seg_resolved",
                    "start_ticks": 300_000,
                    "end_ticks": 422_727,
                }],
                "canonical_text": "主持人说：“请大家",
            },
        ],
        expected_revision=project.revision,
    )
    content_draft_id = draft.content_draft.content_draft_id
    if display_text is not None:
        base = load_draft_editor_snapshot(
            root,
            source_bindings=bindings,
            content_draft_id=content_draft_id,
        )
        seeded = create_content_draft_editor_child(
            root,
            parent=base.candidate,
            blocks=tuple(
                replace(block, display_text=display_text)
                if isinstance(block, SourceExcerptBlock)
                else block
                for block in base.candidate.blocks
            ),
            expected_revision=base.workflow.project_revision,
        )
        content_draft_id = seeded.content_draft.content_draft_id
    return {"root": root, "bindings": bindings, "draft_id": content_draft_id}


def _compound_fixture(tmp_path: Path) -> dict[str, object]:
    root = tmp_path / "跨素材 compound editor"
    project = create_project(root, "跨素材连续选区")
    media_a = tmp_path / "compound A.mp4"
    media_b = tmp_path / "compound B.mp4"
    media_a.write_bytes(b"A" * 512)
    media_b.write_bytes(b"B" * 512)
    sources = (
        _source("src_a", media_a),
        _source("src_b", media_b),
    )
    segment_groups: dict[str, tuple[TranscriptSegment, ...]] = {}
    for source_id, transcript_id, label in (
        ("src_a", "tr_a", "甲"),
        ("src_b", "tr_b", "乙"),
    ):
        segments = tuple(
            _segment(
                f"seg_{source_id[-1]}_{index}",
                index * 60_000,
                index * 60_000 + 40_000,
                f"{label}{index}",
                "spk_0",
                with_units=True,
            )
            for index in range(6)
        )
        if source_id == "src_b":
            segments = (
                *segments,
                _segment(
                    "seg_b_tail",
                    480_000,
                    540_000,
                    "收尾段",
                    "spk_1",
                    with_units=True,
                ),
            )
        segment_groups[source_id] = segments
        transcript = TimedTranscript(
            schema_version=1,
            transcript_version_id=transcript_id,
            source_id=source_id,
            parent_version_id=None,
            provenance=TranscriptProvenance(
                "fixture",
                "1",
                {},
                {},
                f"raw-asr/{source_id}/fixture.json",
                "fixture",
                "fixture",
                0,
            ),
            language="zh-CN",
            segments=segments,
        )
        write_new_json(
            root / "transcripts" / source_id / f"{transcript_id}.json",
            transcript.to_dict(),
        )
    prepared = replace(
        project,
        revision=1,
        sources=sources,
        active_transcript_versions={"src_a": "tr_a", "src_b": "tr_b"},
        persons=(
            Person("person_shared", "同一人物", "主持人", ""),
            Person("person_tail", "收尾人物", "嘉宾", ""),
        ),
        speaker_maps=(
            SpeakerMap("src_a", "tr_a", "spk_0", "person_shared", True),
            SpeakerMap("src_b", "tr_b", "spk_0", "person_shared", True),
            SpeakerMap("src_b", "tr_b", "spk_1", "person_tail", True),
        ),
    )
    ProjectStore(root).save(prepared, expected_revision=0)
    brief = create_edit_brief(
        root,
        theme="跨素材测试",
        target_duration_ticks=600_000,
        focus=["保持十二个引用顺序"],
        allow_reorder=True,
        expected_revision=1,
    )
    bindings = [
        {"source_id": "src_a", "transcript_version_id": "tr_a"},
        {"source_id": "src_b", "transcript_version_id": "tr_b"},
    ]
    context = read_multi_source_agent_context(
        root,
        source_bindings=bindings,
        brief_id=brief.brief.brief_id,
        expected_revision=2,
        offset=0,
        limit=20,
    )

    def source_block(source_id: str, transcript_id: str) -> dict[str, object]:
        refs = [
            {
                "source_id": source_id,
                "transcript_version_id": transcript_id,
                "segment_id": segment.segment_id,
                "start_ticks": segment.start_ticks,
                "end_ticks": segment.end_ticks,
            }
            for segment in segment_groups[source_id][:6]
        ]
        return {
            "block_id": f"block_{source_id}",
            "kind": "source_excerpt",
            "refs": refs,
            "canonical_text": "\n".join(segment.original_text for segment in segment_groups[source_id][:6]),
        }

    tail = segment_groups["src_b"][-1]
    draft = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=bindings,
        brief_id=brief.brief.brief_id,
        context_hash=context.context_hash,
        blocks=[
            source_block("src_a", "tr_a"),
            source_block("src_b", "tr_b"),
            {
                "block_id": "block_tail",
                "kind": "source_excerpt",
                "refs": [
                    {
                        "source_id": "src_b",
                        "transcript_version_id": "tr_b",
                        "segment_id": tail.segment_id,
                        "start_ticks": tail.start_ticks,
                        "end_ticks": tail.end_ticks,
                    }
                ],
                "canonical_text": tail.original_text,
            },
        ],
        expected_revision=2,
    )
    return {
        "root": root,
        "bindings": bindings,
        "draft_id": draft.content_draft.content_draft_id,
    }


def _paragraph(snapshot, *, kind: str, contains: str):  # type: ignore[no-untyped-def]
    return next(
        item
        for item in snapshot.paragraphs
        if item["kind"] == kind and contains in str(item["text"])
    )


def _sectioned_snapshot(tmp_path: Path):  # type: ignore[no-untyped-def]
    fixture = _fixture(tmp_path)
    root = Path(fixture["root"])
    project = ProjectStore(root).load()
    assert project.active_brief_id is not None
    context = read_multi_source_agent_context(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        brief_id=project.active_brief_id,
        expected_revision=project.revision,
        offset=0,
        limit=20,
    )
    draft = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        brief_id=project.active_brief_id,
        context_hash=context.context_hash,
        blocks=[
            {"block_id": "section_a", "kind": "section_title", "title": "第一章"},
            {
                "block_id": "block_section_a",
                "kind": "source_excerpt",
                "refs": [{
                    "source_id": "src_a",
                    "transcript_version_id": "tr_a",
                    "segment_id": "seg_a_fine",
                    "start_ticks": 0,
                    "end_ticks": 60_000,
                }],
                "canonical_text": "你😀好",
            },
            {"block_id": "section_b", "kind": "section_title", "title": "第二章"},
            {
                "block_id": "block_section_b",
                "kind": "source_excerpt",
                "refs": [{
                    "source_id": "src_a",
                    "transcript_version_id": "tr_a",
                    "segment_id": "seg_a_coarse",
                    "start_ticks": 400_000,
                    "end_ticks": 500_000,
                }],
                "canonical_text": "没有细分",
            },
            {"block_id": "section_empty", "kind": "section_title", "title": "空章节"},
        ],
        expected_revision=project.revision,
    )
    return load_draft_editor_snapshot(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=draft.content_draft.content_draft_id,
    )


def _different_person_sectioned_snapshot(tmp_path: Path):  # type: ignore[no-untyped-def]
    fixture = _fixture(tmp_path)
    root = Path(fixture["root"])
    project = ProjectStore(root).load()
    assert project.active_brief_id is not None
    context = read_multi_source_agent_context(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        brief_id=project.active_brief_id,
        expected_revision=project.revision,
        offset=0,
        limit=20,
    )
    draft = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        brief_id=project.active_brief_id,
        context_hash=context.context_hash,
        blocks=[
            {"block_id": "people_section", "kind": "section_title", "title": "人物章节"},
            {
                "block_id": "person_a_paragraph",
                "kind": "source_excerpt",
                "refs": [{
                    "source_id": "src_a",
                    "transcript_version_id": "tr_a",
                    "segment_id": "seg_a_fine",
                    "start_ticks": 0,
                    "end_ticks": 60_000,
                }],
                "canonical_text": "你😀好",
            },
            {
                "block_id": "person_other_paragraph_left",
                "kind": "source_excerpt",
                "refs": [{
                    "source_id": "src_a",
                    "transcript_version_id": "tr_a",
                    "segment_id": "seg_a_other",
                    "start_ticks": 800_000,
                    "end_ticks": 840_000,
                }],
                "canonical_text": "另一",
            },
            {
                "block_id": "person_other_paragraph_right",
                "kind": "source_excerpt",
                "refs": [{
                    "source_id": "src_a",
                    "transcript_version_id": "tr_a",
                    "segment_id": "seg_a_other",
                    "start_ticks": 840_000,
                    "end_ticks": 880_000,
                }],
                "canonical_text": "人物",
            },
        ],
        expected_revision=project.revision,
    )
    return load_draft_editor_snapshot(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=draft.content_draft.content_draft_id,
    )


def _three_person_sectioned_snapshot(tmp_path: Path):  # type: ignore[no-untyped-def]
    fixture = _fixture(tmp_path)
    root = Path(fixture["root"])
    project = ProjectStore(root).load()
    assert project.active_brief_id is not None
    context = read_multi_source_agent_context(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        brief_id=project.active_brief_id,
        expected_revision=project.revision,
        offset=0,
        limit=20,
    )
    draft = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        brief_id=project.active_brief_id,
        context_hash=context.context_hash,
        blocks=[
            {"block_id": "three_people", "kind": "section_title", "title": "三段"},
            {
                "block_id": "three_people_a",
                "kind": "source_excerpt",
                "refs": [{
                    "source_id": "src_a",
                    "transcript_version_id": "tr_a",
                    "segment_id": "seg_a_fine",
                    "start_ticks": 0,
                    "end_ticks": 60_000,
                }],
                "canonical_text": "你😀好",
            },
            {
                "block_id": "three_people_b",
                "kind": "source_excerpt",
                "refs": [{
                    "source_id": "src_a",
                    "transcript_version_id": "tr_a",
                    "segment_id": "seg_a_other",
                    "start_ticks": 800_000,
                    "end_ticks": 880_000,
                }],
                "canonical_text": "另一人物",
            },
            {
                "block_id": "three_people_c",
                "kind": "source_excerpt",
                "refs": [{
                    "source_id": "src_b",
                    "transcript_version_id": "tr_b",
                    "segment_id": "seg_b_fine",
                    "start_ticks": 0,
                    "end_ticks": 100_000,
                }],
                "canonical_text": "重复你好",
            },
        ],
        expected_revision=project.revision,
    )
    return load_draft_editor_snapshot(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=draft.content_draft.content_draft_id,
    )


def test_body_move_across_sections_reassigns_content_without_moving_headings(
    tmp_path: Path,
) -> None:
    snapshot = _sectioned_snapshot(tmp_path)
    first = _paragraph(snapshot, kind="source_excerpt", contains="你😀好")
    second = _paragraph(snapshot, kind="source_excerpt", contains="没有细分")
    selection = resolve_draft_editor_selection(
        snapshot,
        surface="draft",
        anchor={"paragraph_id": first["paragraph_id"], "offset": 0},
        focus={"paragraph_id": first["paragraph_id"], "offset": len(first["text"])},
    )
    caret = resolve_draft_editor_caret(
        snapshot,
        paragraph_id=str(second["paragraph_id"]),
        offset=len(str(second["text"])),
    )
    child = prepare_draft_candidate(
        snapshot,
        operation="move",
        selection=selection,
        caret=caret,
        accept_degraded=False,
        child_id="draft_cross_section_move",
    )
    assert [block.block_id for block in child.blocks if block.kind == "section_title"] == [
        "section_a",
        "section_b",
        "section_empty",
    ]
    assert child.blocks[0].block_id == "section_a"
    assert child.blocks[1].kind == "section_title"
    assert child.blocks[1].block_id == "section_b"
    assert [block.canonical_text for block in child.blocks if block.kind == "source_excerpt"] == [
        "没有细分",
        "你😀好",
    ]


def _repeated_substring_fixture(tmp_path: Path) -> dict[str, object]:
    """Draft whose block canonical contains the same substring twice.

    "等等等等" — the right half after a mid-atom caret ("等等") also occurs
    at the atom's very start, so a text-search cut would split at offset 0
    and the insertion would be rejected.  The ticks-based cut must place it
    after the second 等.
    """
    fixture = _fixture(tmp_path)
    root = Path(fixture["root"])
    project = ProjectStore(root).load()
    transcript = TimedTranscript(
        schema_version=1,
        transcript_version_id="tr_repeat",
        source_id="src_a",
        parent_version_id=None,
        provenance=TranscriptProvenance(
            "fixture", "1", {}, {}, "raw-asr/src_a/repeat.json", "fixture", "fixture", 0
        ),
        language="zh-CN",
        segments=(
            TranscriptSegment(
                segment_id="seg_repeat",
                start_ticks=1_200_000,
                end_ticks=1_280_000,
                original_text="等等等等",
                corrected_text=None,
                local_speaker_id="spk_0",
                person_id=None,
                confidence=None,
                fine_units=(
                    FineUnit("character", "等", 1_200_000, 1_220_000, None),
                    FineUnit("character", "等", 1_220_000, 1_240_000, None),
                    FineUnit("character", "等", 1_240_000, 1_260_000, None),
                    FineUnit("character", "等", 1_260_000, 1_280_000, None),
                ),
                editorial_mark="unmarked",
            ),
        ),
    )
    write_new_json(root / "transcripts" / "src_a" / "tr_repeat.json", transcript.to_dict())
    ProjectStore(root).save(
        replace(
            project,
            revision=project.revision + 1,
            active_transcript_versions={
                **project.active_transcript_versions,
                "src_a": "tr_repeat",
            },
        ),
        expected_revision=project.revision,
    )
    project = ProjectStore(root).load()
    bindings = [{"source_id": "src_a", "transcript_version_id": "tr_repeat"}]
    assert project.active_brief_id is not None
    context = read_agent_context(
        root,
        source_id="src_a",
        transcript_version_id="tr_repeat",
        brief_id=project.active_brief_id,
        expected_revision=project.revision,
        offset=0,
        limit=20,
    )
    draft = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=bindings,
        brief_id=project.active_brief_id,
        context_hash=context.context_hash,
        blocks=[
            {
                "block_id": "repeat_block",
                "kind": "source_excerpt",
                "refs": [{
                    "source_id": "src_a",
                    "transcript_version_id": "tr_repeat",
                    "segment_id": "seg_repeat",
                    "start_ticks": 1_200_000,
                    "end_ticks": 1_280_000,
                }],
                "canonical_text": "等等等等",
            },
        ],
        expected_revision=project.revision,
    )
    return {
        "root": root,
        "bindings": bindings,
        "draft_id": draft.content_draft.content_draft_id,
    }


def test_repeated_substring_caret_split_uses_ticks_not_first_text_match(
    tmp_path: Path,
) -> None:
    """A mid-atom caret whose right half also opens the atom cuts by ticks.

    The old `canonical.find(right_ref.canonical_text)` located the first
    text occurrence: cutting "等等等等" after the second 等 produced a right
    half "等等" that also matches at offset 0, so the cut landed at 0 and
    the insertion was rejected.  The ticks-based boundary must split after
    the second 等 and the resulting halves must match their refs.
    """
    fixture = _repeated_substring_fixture(tmp_path)
    snapshot = load_draft_editor_snapshot(
        Path(fixture["root"]),
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=str(fixture["draft_id"]),
    )
    target = next(
        paragraph
        for paragraph in snapshot.paragraphs
        if paragraph["text"] == "等等等等"
    )
    source = next(
        paragraph
        for paragraph in snapshot.transcript_paragraphs
        if paragraph["text"] == "等等等等"
    )
    selection = resolve_draft_editor_selection(
        snapshot,
        surface="source",
        anchor={"paragraph_id": source["paragraph_id"], "offset": 0},
        focus={"paragraph_id": source["paragraph_id"], "offset": 2},
    )
    caret = resolve_draft_editor_caret(
        snapshot,
        paragraph_id=str(target["paragraph_id"]),
        offset=2,
    )
    child = prepare_draft_candidate(
        snapshot,
        operation="insert",
        selection=selection,
        caret=caret,
        accept_degraded=False,
        child_id="repeat_caret_child",
    )
    source_blocks = [block for block in child.blocks if block.kind == "source_excerpt"]
    # The atom is split at the caret into two "等等" halves, plus the
    # inserted selection ("等等" from the source).
    assert {block.canonical_text for block in source_blocks} == {"等等"}
    assert len(source_blocks) == 3


def test_source_insert_into_section_inherits_target_heading(
    tmp_path: Path,
) -> None:
    snapshot = _sectioned_snapshot(tmp_path)
    source_paragraph = next(
        paragraph
        for paragraph in snapshot.transcript_paragraphs
        if paragraph["text"] == "另一人物"
    )
    target = _paragraph(snapshot, kind="source_excerpt", contains="没有细分")
    selection = resolve_draft_editor_selection(
        snapshot,
        surface="source",
        anchor={"paragraph_id": source_paragraph["paragraph_id"], "offset": 0},
        focus={
            "paragraph_id": source_paragraph["paragraph_id"],
            "offset": len(source_paragraph["text"]),
        },
    )
    caret = resolve_draft_editor_caret(
        snapshot,
        paragraph_id=str(target["paragraph_id"]),
        offset=len(str(target["text"])),
    )
    child = prepare_draft_candidate(
        snapshot,
        operation="insert",
        selection=selection,
        caret=caret,
        accept_degraded=False,
        child_id="draft_cross_section_insert",
    )
    heading_index = next(
        index
        for index, block in enumerate(child.blocks)
        if block.kind == "section_title" and block.block_id == "section_b"
    )
    assert all(
        block.kind != "section_title" or block.block_id != "section_a"
        for block in child.blocks[heading_index:]
    )
    assert [block.canonical_text for block in child.blocks if block.kind == "source_excerpt"][-1] == "另一人物"


def test_narration_selection_snaps_to_full_block_and_rejects_mixed_range(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    snapshot = load_draft_editor_snapshot(
        Path(fixture["root"]),
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=str(fixture["draft_id"]),
    )
    narration = _paragraph(snapshot, kind="narration", contains="待录音解说")
    selection = resolve_draft_editor_selection(
        snapshot,
        surface="draft",
        anchor={"paragraph_id": narration["paragraph_id"], "offset": 1},
        focus={"paragraph_id": narration["paragraph_id"], "offset": 3},
    )
    assert selection.narration_block_id == "block_narration"
    assert selection.display_anchor["character_offset"] == 0
    assert selection.display_focus["character_offset"] == len(narration["text"])
    target = _paragraph(snapshot, kind="source_excerpt", contains="重复你好")
    caret = resolve_draft_editor_caret(
        snapshot,
        paragraph_id=str(target["paragraph_id"]),
        offset=len(str(target["text"])),
    )
    child = prepare_draft_candidate(
        snapshot,
        operation="move",
        selection=selection,
        caret=caret,
        accept_degraded=False,
        child_id="draft_narration_move",
    )
    moved = next(block for block in child.blocks if block.block_id == "block_narration")
    assert moved.kind == "narration"
    assert moved.text == "待录音解说"
    assert moved.status == "draft"
    assert moved.recorded_refs == ()
    source = _paragraph(snapshot, kind="source_excerpt", contains="你😀好")
    with pytest.raises(ProjectError, match="mix narration and source"):
        resolve_draft_editor_selection(
            snapshot,
            surface="draft",
            anchor={"paragraph_id": narration["paragraph_id"], "offset": 1},
            focus={"paragraph_id": source["paragraph_id"], "offset": 1},
        )


def test_section_operations_use_closed_payloads_and_exact_split_boundaries(
    tmp_path: Path,
) -> None:
    snapshot = _sectioned_snapshot(tmp_path)
    first = _paragraph(snapshot, kind="source_excerpt", contains="你😀好")
    split_target = {
        "paragraph_id": first["paragraph_id"],
        "block_id": "block_section_a",
        "utf16_offset": 1,
    }

    reordered = prepare_section_operation(
        snapshot,
        operation="section_reorder",
        payload={
            "heading_block_id": "section_b",
            "before_heading_block_id": "section_a",
        },
        child_id="draft_section_reorder",
    )
    assert [block.block_id for block in reordered.blocks if block.kind == "section_title"] == [
        "section_b",
        "section_a",
        "section_empty",
    ]

    renamed = prepare_section_operation(
        snapshot,
        operation="section_rename",
        payload={"heading_block_id": "section_a", "title": "重命名"},
        child_id="draft_section_rename",
    )
    assert renamed.blocks[0].title == "重命名"

    merged = prepare_section_operation(
        snapshot,
        operation="section_merge",
        payload={
            "heading_block_id": "section_a",
            "direction": "next",
            "adjacent_heading_block_id": "section_b",
        },
        child_id="draft_section_merge",
    )
    assert [block.block_id for block in merged.blocks if block.kind == "section_title"] == [
        "section_a",
        "section_empty",
    ]
    assert [block.canonical_text for block in merged.blocks if block.kind == "source_excerpt"] == [
        "你😀好",
        "没有细分",
    ]

    deleted = prepare_section_operation(
        snapshot,
        operation="section_delete",
        payload={"heading_block_id": "section_b"},
        child_id="draft_section_delete",
    )
    assert [block.block_id for block in deleted.blocks if block.kind == "section_title"] == [
        "section_a",
        "section_empty",
    ]
    assert all(
        getattr(block, "block_id", None) != "block_section_b"
        for block in deleted.blocks
    )

    split = prepare_section_operation(
        snapshot,
        operation="section_split",
        payload={
            "heading_block_id": "section_a",
            "target": split_target,
            "title": "中段",
        },
        child_id="draft_section_split",
    )
    split_headings = [block for block in split.blocks if block.kind == "section_title"]
    assert split_headings[0].block_id == "section_a"
    assert split_headings[1].block_id.startswith("section_")
    assert split_headings[1].block_id not in {"section_a", "section_b", "section_empty"}
    assert split_headings[1].title == "中段"
    split_sources = [block for block in split.blocks if block.kind == "source_excerpt"]
    assert [block.canonical_text for block in split_sources[:2]] == ["你", "😀好"]
    assert split_sources[0].refs[0].end_ticks == 20_000
    assert split_sources[1].refs[0].start_ticks == 20_000

    empty_deleted = prepare_section_operation(
        snapshot,
        operation="section_delete",
        payload={"heading_block_id": "section_empty"},
        child_id="draft_empty_section_delete",
    )
    assert all(
        getattr(block, "block_id", None) != "section_empty"
        for block in empty_deleted.blocks
    )

    source_paragraph = next(
        paragraph
        for paragraph in snapshot.transcript_paragraphs
        if paragraph["text"] == "另一人物"
    )
    source_selection = resolve_draft_editor_selection(
        snapshot,
        surface="source",
        anchor={"paragraph_id": source_paragraph["paragraph_id"], "offset": 0},
        focus={
            "paragraph_id": source_paragraph["paragraph_id"],
            "offset": len(source_paragraph["text"]),
        },
    )
    empty_paragraph = next(
        paragraph
        for paragraph in snapshot.paragraphs
        if paragraph["kind"] == "section_title"
        and paragraph["block_id"] == "section_empty"
    )
    empty_caret = resolve_draft_editor_caret(
        snapshot,
        paragraph_id=str(empty_paragraph["paragraph_id"]),
        offset=0,
    )
    inserted = prepare_draft_candidate(
        snapshot,
        operation="insert",
        selection=source_selection,
        caret=empty_caret,
        accept_degraded=False,
        child_id="draft_empty_section_insert",
    )
    empty_index = next(
        index
        for index, block in enumerate(inserted.blocks)
        if block.block_id == "section_empty"
    )
    assert inserted.blocks[empty_index + 1].canonical_text == "另一人物"

    with pytest.raises(ProjectError, match="payload fields"):
        prepare_section_operation(
            snapshot,
            operation="section_delete",
            payload={"heading_block_id": "section_a", "extra": True},
            child_id="draft_invalid_section",
        )


def test_snapshot_is_path_free_and_exposes_safe_continuous_draft_and_sources(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    snapshot = load_draft_editor_snapshot(
        Path(fixture["root"]),
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=str(fixture["draft_id"]),
    )

    payload = snapshot.to_dict()

    assert payload["editor_schema_version"] == 1
    assert payload["project"] == {"name": "初稿编辑"}
    assert payload["brief"]["theme"] == "测试初稿"
    assert payload["duration_acceptance"] == {
        "target_duration_ticks": 360_000,
        "actual_duration_ticks": 260_000,
        "delta_ticks": -100_000,
        "status": "under_target",
        "tolerance_ticks": 36_000,
        "accepted_upper_bound_ticks": 396_000,
    }
    assert payload["candidate"]["candidate_id"] == fixture["draft_id"]
    assert snapshot.cache_key.project_revision == 2
    assert snapshot.cache_key.source_bindings == (("src_a", "tr_a"), ("src_b", "tr_b"))
    assert snapshot.cache_key.candidate_id == fixture["draft_id"]
    assert [source["source_id"] for source in payload["sources"]] == ["src_a", "src_b"]
    assert [source["media_url"] for source in payload["sources"]] == [
        "/media/src_a",
        "/media/src_b",
    ]
    assert [paragraph["kind"] for paragraph in payload["paragraphs"]] == [
        "source_excerpt",
        "narration",
        "source_excerpt",
    ]
    assert payload["paragraphs"][0]["person"]["name"] == "人物甲"
    assert payload["paragraphs"][1]["narration_status"] == "draft"
    assert payload["paragraphs"][0]["source_runs"][0]["refs"]
    first_run = payload["paragraphs"][0]["source_runs"][0]
    assert first_run["start_offset"] == 0
    assert first_run["end_offset"] == len(first_run["text"])
    assert first_run["source_start_offset"] == 0
    assert first_run["source_end_offset"] == len(first_run["text"])
    second_run = payload["paragraphs"][0]["source_runs"][1]
    assert second_run["start_offset"] == len(first_run["text"])
    assert second_run["source_start_offset"] == 0
    assert second_run["source_end_offset"] == len(second_run["text"])
    serialized = json.dumps(payload, ensure_ascii=False)
    assert str(fixture["root"]) not in serialized
    assert str(tmp_path) not in serialized
    assert "locator" not in serialized
    assert "raw-asr" not in serialized
    assert "context_hash" not in serialized
    assert "token" not in serialized.lower()
    assert "revision" not in serialized.lower()
    assert not (Path(fixture["root"]) / "proposals").exists()
    assert not (Path(fixture["root"]) / "edits").exists()
    assert not (Path(fixture["root"]) / "renders").exists()


def test_section_title_only_marks_the_first_paragraph_of_a_multi_person_block(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    root = Path(fixture["root"])
    project = ProjectStore(root).load()
    assert project.active_brief_id is not None
    context = read_multi_source_agent_context(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        brief_id=project.active_brief_id,
        expected_revision=2,
        offset=0,
        limit=20,
    )
    draft = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        brief_id=project.active_brief_id,
        context_hash=context.context_hash,
        blocks=[
            {
                "block_id": "section_multi_person",
                "kind": "section_title",
                "title": "同一章节",
            },
            {
                "block_id": "block_multi_person_section",
                "kind": "source_excerpt",
                "refs": [
                    {
                        "source_id": "src_a",
                        "transcript_version_id": "tr_a",
                        "segment_id": "seg_a_fine",
                        "start_ticks": 0,
                        "end_ticks": 60_000,
                    },
                    {
                        "source_id": "src_a",
                        "transcript_version_id": "tr_a",
                        "segment_id": "seg_a_coarse",
                        "start_ticks": 400_000,
                        "end_ticks": 500_000,
                    },
                    {
                        "source_id": "src_a",
                        "transcript_version_id": "tr_a",
                        "segment_id": "seg_a_other",
                        "start_ticks": 800_000,
                        "end_ticks": 880_000,
                    },
                ],
                "canonical_text": "你😀好\n没有细分\n另一人物",
            },
        ],
        expected_revision=2,
    )

    snapshot = load_draft_editor_snapshot(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=draft.content_draft.content_draft_id,
    )
    paragraphs = [
        paragraph
        for paragraph in snapshot.paragraphs
        if paragraph["kind"] == "source_excerpt"
    ]

    assert [paragraph["section_title"] for paragraph in paragraphs] == [
        "同一章节",
        None,
    ]
    assert [paragraph["text"] for paragraph in paragraphs] == [
        "你😀好没有细分",
        "另一人物",
    ]
    assert [
        run["text"]
        for paragraph in paragraphs
        for run in paragraph["source_runs"]  # type: ignore[union-attr]
    ] == ["你😀好", "没有细分", "另一人物"]
    assert [
        ref["segment_id"]
        for paragraph in paragraphs
        for ref in paragraph["exact_refs"]  # type: ignore[union-attr]
    ] == ["seg_a_fine", "seg_a_coarse", "seg_a_other"]


def test_section_projection_merges_adjacent_same_person_blocks_and_titles_once(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    root = Path(fixture["root"])
    project = ProjectStore(root).load()
    assert project.active_brief_id is not None
    context = read_multi_source_agent_context(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        brief_id=project.active_brief_id,
        expected_revision=project.revision,
        offset=0,
        limit=20,
    )
    draft = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        brief_id=project.active_brief_id,
        context_hash=context.context_hash,
        blocks=[
            {"block_id": "same_title_a", "kind": "section_title", "title": "同名"},
            {
                "block_id": "same_title_a_first",
                "kind": "source_excerpt",
                "refs": [{
                    "source_id": "src_a",
                    "transcript_version_id": "tr_a",
                    "segment_id": "seg_a_fine",
                    "start_ticks": 0,
                    "end_ticks": 60_000,
                }],
                "canonical_text": "你😀好",
            },
            {
                "block_id": "same_title_a_second",
                "kind": "source_excerpt",
                "refs": [{
                    "source_id": "src_a",
                    "transcript_version_id": "tr_a",
                    "segment_id": "seg_a_coarse",
                    "start_ticks": 400_000,
                    "end_ticks": 500_000,
                }],
                "canonical_text": "没有细分",
            },
            {
                "block_id": "same_title_narration",
                "kind": "narration",
                "text": "章节中部解说",
                "status": "draft",
                "recorded_refs": [],
            },
            {"block_id": "same_title_b", "kind": "section_title", "title": "同名"},
            {"block_id": "same_title_tail", "kind": "section_title", "title": "同名"},
        ],
        expected_revision=project.revision,
    )
    snapshot = load_draft_editor_snapshot(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=draft.content_draft.content_draft_id,
    )

    source_paragraphs = [
        paragraph
        for paragraph in snapshot.paragraphs
        if paragraph["kind"] == "source_excerpt"
    ]
    assert [paragraph["text"] for paragraph in source_paragraphs] == [
        "你😀好没有细分",
    ]
    assert [paragraph["section_title"] for paragraph in source_paragraphs] == ["同名"]
    narration = _paragraph(snapshot, kind="narration", contains="章节中部解说")
    assert narration["section_title"] is None
    assert [
        paragraph["block_id"]
        for paragraph in snapshot.paragraphs
        if paragraph["kind"] == "section_title"
    ] == ["same_title_b", "same_title_tail"]


def test_initial_projection_uses_unique_contiguous_person_runs_for_a_b_a(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    root = Path(fixture["root"])
    project = ProjectStore(root).load()
    assert project.active_brief_id is not None
    context = read_multi_source_agent_context(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        brief_id=project.active_brief_id,
        expected_revision=project.revision,
        offset=0,
        limit=20,
    )
    draft = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        brief_id=project.active_brief_id,
        context_hash=context.context_hash,
        blocks=[
            {"block_id": "runs_section", "kind": "section_title", "title": "连续人物"},
            {
                "block_id": "run_a_first",
                "kind": "source_excerpt",
                "refs": [{
                    "source_id": "src_a",
                    "transcript_version_id": "tr_a",
                    "segment_id": "seg_a_fine",
                    "start_ticks": 0,
                    "end_ticks": 20_000,
                }],
                "canonical_text": "你",
            },
            {
                "block_id": "run_a_second",
                "kind": "source_excerpt",
                "refs": [{
                    "source_id": "src_a",
                    "transcript_version_id": "tr_a",
                    "segment_id": "seg_a_fine",
                    "start_ticks": 20_000,
                    "end_ticks": 60_000,
                }],
                "canonical_text": "😀好",
            },
            {
                "block_id": "run_b",
                "kind": "source_excerpt",
                "refs": [{
                    "source_id": "src_a",
                    "transcript_version_id": "tr_a",
                    "segment_id": "seg_a_other",
                    "start_ticks": 800_000,
                    "end_ticks": 880_000,
                }],
                "canonical_text": "另一人物",
            },
            {
                "block_id": "run_a_third",
                "kind": "source_excerpt",
                "refs": [{
                    "source_id": "src_a",
                    "transcript_version_id": "tr_a",
                    "segment_id": "seg_a_coarse",
                    "start_ticks": 400_000,
                    "end_ticks": 500_000,
                }],
                "canonical_text": "没有细分",
            },
        ],
        expected_revision=project.revision,
    )

    snapshot = load_draft_editor_snapshot(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=draft.content_draft.content_draft_id,
    )
    paragraphs = [item for item in snapshot.paragraphs if item["kind"] == "source_excerpt"]
    assert [item["text"] for item in paragraphs] == [
        "你😀好",
        "另一人物",
        "没有细分",
    ]
    assert [item["person"]["person_id"] for item in paragraphs] == [  # type: ignore[index]
        "person_a",
        "person_other",
        "person_a",
    ]
    assert [item["section_title"] for item in paragraphs] == ["连续人物", None, None]


def test_partial_move_reloads_remaining_and_target_as_two_paragraphs(
    tmp_path: Path,
) -> None:
    snapshot = _sectioned_snapshot(tmp_path)
    source = _paragraph(snapshot, kind="source_excerpt", contains="你😀好")
    target = _paragraph(snapshot, kind="source_excerpt", contains="没有细分")
    selection = resolve_draft_editor_selection(
        snapshot,
        surface="draft",
        anchor={"paragraph_id": source["paragraph_id"], "offset": 0},
        focus={"paragraph_id": source["paragraph_id"], "offset": 1},
    )
    caret = resolve_draft_editor_caret(
        snapshot,
        paragraph_id=str(target["paragraph_id"]),
        offset=len(str(target["text"])),
    )
    mutation = edit_draft_candidate(
        snapshot,
        operation="move",
        selection=selection,
        caret=caret,
        accept_degraded=False,
    )
    reloaded = load_draft_editor_snapshot(
        Path(snapshot.workflow.project_path),
        source_bindings=[binding.to_dict() for binding in snapshot.workflow.source_bindings],
        content_draft_id=mutation.content_draft.content_draft_id,
    )
    source_paragraphs = [
        paragraph
        for paragraph in reloaded.paragraphs
        if paragraph["kind"] == "source_excerpt"
    ]
    assert [paragraph["text"] for paragraph in source_paragraphs] == ["😀好", "没有细分你"]
    assert [paragraph["section_title"] for paragraph in source_paragraphs] == ["第一章", "第二章"]


def test_same_section_partial_move_reloads_one_paragraph_with_one_title(
    tmp_path: Path,
) -> None:
    fixture = _gap_boundary_fixture(tmp_path)
    snapshot = load_draft_editor_snapshot(
        Path(fixture["root"]),
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=str(fixture["draft_id"]),
    )
    paragraph = _paragraph(snapshot, kind="source_excerpt", contains="甲，乙")
    selection = resolve_draft_editor_selection(
        snapshot,
        surface="draft",
        anchor={"paragraph_id": paragraph["paragraph_id"], "offset": 2},
        focus={"paragraph_id": paragraph["paragraph_id"], "offset": 3},
    )
    caret = resolve_draft_editor_caret(
        snapshot,
        paragraph_id=str(paragraph["paragraph_id"]),
        offset=0,
    )
    mutation = edit_draft_candidate(
        snapshot,
        operation="move",
        selection=selection,
        caret=caret,
        accept_degraded=False,
    )
    reloaded = load_draft_editor_snapshot(
        Path(snapshot.workflow.project_path),
        source_bindings=[
            binding.to_dict() for binding in snapshot.workflow.source_bindings
        ],
        content_draft_id=mutation.content_draft.content_draft_id,
    )

    source_paragraphs = [
        item for item in reloaded.paragraphs if item["kind"] == "source_excerpt"
    ]
    assert len(source_paragraphs) == 1
    assert source_paragraphs[0]["text"] == "乙 甲，丙。"
    assert [
        item["section_title"]
        for item in reloaded.paragraphs
        if item["section_title"] == "原章节"
    ] == ["原章节"]
    assert [
        (ref["start_ticks"], ref["end_ticks"])
        for ref in source_paragraphs[0]["exact_refs"]  # type: ignore[index]
    ] == [(250, 350), (100, 200), (380, 480)]
    assert all(
        end <= 200 or start >= 250
        for start, end in [
            (ref["start_ticks"], ref["end_ticks"])
            for ref in source_paragraphs[0]["exact_refs"]  # type: ignore[index]
        ]
    )


def test_different_person_partial_move_roundtrips_two_paragraph_memberships(
    tmp_path: Path,
) -> None:
    snapshot = _different_person_sectioned_snapshot(tmp_path)
    source = _paragraph(snapshot, kind="source_excerpt", contains="你😀好")
    target = _paragraph(snapshot, kind="source_excerpt", contains="另一人物")
    assert len([item for item in snapshot.paragraphs if item["kind"] == "source_excerpt"]) == 2

    selection = resolve_draft_editor_selection(
        snapshot,
        surface="draft",
        anchor={"paragraph_id": source["paragraph_id"], "offset": 1},
        focus={"paragraph_id": source["paragraph_id"], "offset": 2},
    )
    caret = resolve_draft_editor_caret(
        snapshot,
        paragraph_id=str(target["paragraph_id"]),
        offset=2,
    )
    mutation = edit_draft_candidate(
        snapshot,
        operation="move",
        selection=selection,
        caret=caret,
        accept_degraded=False,
    )
    reloaded = load_draft_editor_snapshot(
        Path(snapshot.workflow.project_path),
        source_bindings=[binding.to_dict() for binding in snapshot.workflow.source_bindings],
        content_draft_id=mutation.content_draft.content_draft_id,
    )

    paragraphs = [item for item in reloaded.paragraphs if item["kind"] == "source_excerpt"]
    assert len(paragraphs) == 2
    assert [item["text"] for item in paragraphs] == ["你好", "另一😀人物"]
    assert paragraphs[0]["person"]["person_id"] == "person_a"  # type: ignore[index]
    assert paragraphs[1]["person"] == {  # type: ignore[index]
        "person_id": None,
        "name": None,
        "role": None,
        "local_speaker_id": None,
    }
    assert [
        item["section_title"]
        for item in reloaded.paragraphs
        if item["section_title"] == "人物章节"
    ] == ["人物章节"]
    assert [
        (ref["segment_id"], ref["start_ticks"], ref["end_ticks"])
        for ref in paragraphs[0]["exact_refs"]  # type: ignore[index]
    ] == [
        ("seg_a_fine", 0, 20_000),
        ("seg_a_fine", 40_000, 60_000),
    ]
    assert [
        (ref["segment_id"], ref["start_ticks"], ref["end_ticks"])
        for ref in paragraphs[1]["exact_refs"]  # type: ignore[index]
    ] == [
        ("seg_a_other", 800_000, 840_000),
        ("seg_a_fine", 20_000, 40_000),
        ("seg_a_other", 840_000, 880_000),
    ]
    assert [
        run["refs"][0]["segment_id"]
        for run in paragraphs[1]["source_runs"]  # type: ignore[index]
    ] == ["seg_a_other", "seg_a_fine", "seg_a_other"]


def test_different_person_partial_move_to_target_start_has_no_false_person(
    tmp_path: Path,
) -> None:
    snapshot = _different_person_sectioned_snapshot(tmp_path)
    source = _paragraph(snapshot, kind="source_excerpt", contains="你😀好")
    target = _paragraph(snapshot, kind="source_excerpt", contains="另一人物")
    selection = resolve_draft_editor_selection(
        snapshot,
        surface="draft",
        anchor={"paragraph_id": source["paragraph_id"], "offset": 1},
        focus={"paragraph_id": source["paragraph_id"], "offset": 2},
    )
    caret = resolve_draft_editor_caret(
        snapshot,
        paragraph_id=str(target["paragraph_id"]),
        offset=0,
    )
    mutation = edit_draft_candidate(
        snapshot,
        operation="move",
        selection=selection,
        caret=caret,
        accept_degraded=False,
    )
    reloaded = load_draft_editor_snapshot(
        Path(snapshot.workflow.project_path),
        source_bindings=[binding.to_dict() for binding in snapshot.workflow.source_bindings],
        content_draft_id=mutation.content_draft.content_draft_id,
    )

    paragraphs = [item for item in reloaded.paragraphs if item["kind"] == "source_excerpt"]
    assert [item["text"] for item in paragraphs] == ["你好", "😀另一人物"]
    assert paragraphs[1]["person"] == {  # type: ignore[index]
        "person_id": None,
        "name": None,
        "role": None,
        "local_speaker_id": None,
    }
    assert [
        (ref["segment_id"], ref["start_ticks"], ref["end_ticks"])
        for ref in paragraphs[1]["exact_refs"]  # type: ignore[index]
    ] == [
        ("seg_a_fine", 20_000, 40_000),
        ("seg_a_other", 800_000, 840_000),
        ("seg_a_other", 840_000, 880_000),
    ]


def test_section_split_breaks_generated_paragraph_membership_and_remains_editable(
    tmp_path: Path,
) -> None:
    snapshot = _different_person_sectioned_snapshot(tmp_path)
    source = _paragraph(snapshot, kind="source_excerpt", contains="你😀好")
    target = _paragraph(snapshot, kind="source_excerpt", contains="另一人物")
    selection = resolve_draft_editor_selection(
        snapshot,
        surface="draft",
        anchor={"paragraph_id": source["paragraph_id"], "offset": 1},
        focus={"paragraph_id": source["paragraph_id"], "offset": 2},
    )
    caret = resolve_draft_editor_caret(
        snapshot,
        paragraph_id=str(target["paragraph_id"]),
        offset=0,
    )
    moved = edit_draft_candidate(
        snapshot,
        operation="move",
        selection=selection,
        caret=caret,
        accept_degraded=False,
    )
    moved_snapshot = load_draft_editor_snapshot(
        Path(snapshot.workflow.project_path),
        source_bindings=[binding.to_dict() for binding in snapshot.workflow.source_bindings],
        content_draft_id=moved.content_draft.content_draft_id,
    )
    mixed = _paragraph(moved_snapshot, kind="source_excerpt", contains="😀另一人物")
    runs = mixed["source_runs"]
    assert isinstance(runs, list) and [run["text"] for run in runs] == [
        "😀",
        "另一",
        "人物",
    ]
    generated_tokens = [str(run["block_id"]).split("_")[3] for run in runs]
    assert len(set(generated_tokens)) == 1

    prepared = prepare_section_operation(
        moved_snapshot,
        operation="section_split",
        payload={
            "heading_block_id": "people_section",
            "target": {
                "paragraph_id": mixed["paragraph_id"],
                "block_id": runs[1]["block_id"],
                "utf16_offset": 2,
            },
            "title": "边界新章",
        },
        child_id="draft_generated_boundary_split",
    )
    split = publish_prepared_content_draft_editor_child(
        Path(moved_snapshot.workflow.project_path),
        parent=moved_snapshot.candidate,
        child=prepared,
        expected_revision=moved_snapshot.workflow.project_revision,
    )
    reloaded = load_draft_editor_snapshot(
        Path(moved_snapshot.workflow.project_path),
        source_bindings=[binding.to_dict() for binding in moved_snapshot.workflow.source_bindings],
        content_draft_id=split.content_draft.content_draft_id,
    )

    paragraphs = [item for item in reloaded.paragraphs if item["kind"] == "source_excerpt"]
    assert [item["text"] for item in paragraphs] == ["你好", "😀", "另一人物"]
    assert [item["section_title"] for item in paragraphs] == [
        "人物章节",
        None,
        "边界新章",
    ]
    assert [item["person"]["person_id"] for item in paragraphs] == [  # type: ignore[index]
        "person_a",
        "person_a",
        "person_other",
    ]
    headings = [
        block for block in split.content_draft.blocks if block.kind == "section_title"
    ]
    assert [block.title for block in headings] == ["人物章节", "边界新章"]
    assert len({block.block_id for block in headings}) == 2
    assert sum(item["section_title"] == "人物章节" for item in reloaded.paragraphs) == 1
    assert sum(item["section_title"] == "边界新章" for item in reloaded.paragraphs) == 1

    blocks = list(split.content_draft.blocks)
    new_heading_index = next(
        index
        for index, block in enumerate(blocks)
        if block.kind == "section_title" and block.title == "边界新章"
    )
    left_id = blocks[new_heading_index - 1].block_id
    right_id = blocks[new_heading_index + 1].block_id
    left_token = left_id.split("_")[3]
    right_token = right_id.split("_")[3]
    assert left_token == generated_tokens[0]
    assert right_token != left_token
    assert blocks[new_heading_index + 2].block_id.split("_")[3] == right_token
    token_positions: dict[str, list[int]] = {}
    for index, block in enumerate(blocks):
        if block.kind == "source_excerpt" and block.block_id.startswith("block_edit_p_"):
            token_positions.setdefault(block.block_id.split("_")[3], []).append(index)
    assert all(
        positions == list(range(positions[0], positions[-1] + 1))
        for positions in token_positions.values()
    )
    moved_evidence = [
        (block.refs, block.canonical_text)
        for block in moved_snapshot.candidate.blocks
        if block.kind == "source_excerpt"
    ]
    split_evidence = [
        (block.refs, block.canonical_text)
        for block in split.content_draft.blocks
        if block.kind == "source_excerpt"
    ]
    assert split_evidence == moved_evidence
    assert [
        (run["refs"], run["start_ticks"], run["end_ticks"])
        for paragraph in paragraphs
        for run in paragraph["source_runs"]  # type: ignore[index]
    ] == [
        (run["refs"], run["start_ticks"], run["end_ticks"])
        for paragraph in moved_snapshot.paragraphs
        if paragraph["kind"] == "source_excerpt"
        for run in paragraph["source_runs"]  # type: ignore[index]
    ]

    right = _paragraph(reloaded, kind="source_excerpt", contains="另一人物")
    next_selection = resolve_draft_editor_selection(
        reloaded,
        surface="draft",
        anchor={"paragraph_id": right["paragraph_id"], "offset": 0},
        focus={"paragraph_id": right["paragraph_id"], "offset": 1},
    )
    next_caret = resolve_draft_editor_caret(
        reloaded,
        paragraph_id=str(right["paragraph_id"]),
        offset=len(str(right["text"])),
    )
    next_move = edit_draft_candidate(
        reloaded,
        operation="move",
        selection=next_selection,
        caret=next_caret,
        accept_degraded=False,
    )
    assert next_move.content_draft.parent_draft_id == split.content_draft.content_draft_id


def test_section_split_inside_generated_block_retags_the_right_paragraph_tail(
    tmp_path: Path,
) -> None:
    snapshot = _different_person_sectioned_snapshot(tmp_path)
    source = _paragraph(snapshot, kind="source_excerpt", contains="你😀好")
    target = _paragraph(snapshot, kind="source_excerpt", contains="另一人物")
    selection = resolve_draft_editor_selection(
        snapshot,
        surface="draft",
        anchor={"paragraph_id": source["paragraph_id"], "offset": 1},
        focus={"paragraph_id": source["paragraph_id"], "offset": 2},
    )
    caret = resolve_draft_editor_caret(
        snapshot,
        paragraph_id=str(target["paragraph_id"]),
        offset=0,
    )
    moved = edit_draft_candidate(
        snapshot,
        operation="move",
        selection=selection,
        caret=caret,
        accept_degraded=False,
    )
    moved_snapshot = load_draft_editor_snapshot(
        Path(snapshot.workflow.project_path),
        source_bindings=[binding.to_dict() for binding in snapshot.workflow.source_bindings],
        content_draft_id=moved.content_draft.content_draft_id,
    )
    mixed = _paragraph(moved_snapshot, kind="source_excerpt", contains="😀另一人物")
    runs = mixed["source_runs"]
    assert isinstance(runs, list) and [run["text"] for run in runs] == [
        "😀",
        "另一",
        "人物",
    ]
    old_tokens = [str(run["block_id"]).split("_")[3] for run in runs]
    assert len(set(old_tokens)) == 1

    prepared = prepare_section_operation(
        moved_snapshot,
        operation="section_split",
        payload={
            "heading_block_id": "people_section",
            "target": {
                "paragraph_id": mixed["paragraph_id"],
                "block_id": runs[1]["block_id"],
                "utf16_offset": 3,
            },
            "title": "段内新章",
        },
        child_id="draft_generated_internal_split",
    )
    split = publish_prepared_content_draft_editor_child(
        Path(moved_snapshot.workflow.project_path),
        parent=moved_snapshot.candidate,
        child=prepared,
        expected_revision=moved_snapshot.workflow.project_revision,
    )
    reloaded = load_draft_editor_snapshot(
        Path(moved_snapshot.workflow.project_path),
        source_bindings=[binding.to_dict() for binding in moved_snapshot.workflow.source_bindings],
        content_draft_id=split.content_draft.content_draft_id,
    )

    paragraphs = [item for item in reloaded.paragraphs if item["kind"] == "source_excerpt"]
    assert [item["text"] for item in paragraphs] == ["你好", "😀另", "一人物"]
    assert [item["section_title"] for item in paragraphs] == [
        "人物章节",
        None,
        "段内新章",
    ]
    assert paragraphs[1]["person"] == {  # type: ignore[index]
        "person_id": None,
        "name": None,
        "role": None,
        "local_speaker_id": None,
    }
    assert paragraphs[2]["person"]["person_id"] == "person_other"  # type: ignore[index]
    assert sum(item["section_title"] == "人物章节" for item in reloaded.paragraphs) == 1
    assert sum(item["section_title"] == "段内新章" for item in reloaded.paragraphs) == 1

    blocks = list(split.content_draft.blocks)
    headings = [block for block in blocks if block.kind == "section_title"]
    assert [block.title for block in headings] == ["人物章节", "段内新章"]
    assert len({block.block_id for block in headings}) == 2
    heading_index = next(
        index
        for index, block in enumerate(blocks)
        if block.kind == "section_title" and block.title == "段内新章"
    )
    left_ids = [blocks[heading_index - 2].block_id, blocks[heading_index - 1].block_id]
    right_ids = [blocks[heading_index + 1].block_id, blocks[heading_index + 2].block_id]
    assert [block.canonical_text for block in blocks[heading_index - 2 : heading_index]] == [
        "😀",
        "另",
    ]
    assert [block.canonical_text for block in blocks[heading_index + 1 : heading_index + 3]] == [
        "一",
        "人物",
    ]
    assert {block_id.split("_")[3] for block_id in left_ids} == {old_tokens[0]}
    right_tokens = {block_id.split("_")[3] for block_id in right_ids}
    assert len(right_tokens) == 1
    assert right_tokens != {old_tokens[0]}
    token_positions: dict[str, list[int]] = {}
    for index, block in enumerate(blocks):
        if block.kind == "source_excerpt" and block.block_id.startswith("block_edit_p_"):
            token_positions.setdefault(block.block_id.split("_")[3], []).append(index)
    assert all(
        positions == list(range(positions[0], positions[-1] + 1))
        for positions in token_positions.values()
    )
    assert [
        (
            run["text"],
            run["refs"][0]["source_id"],
            run["refs"][0]["transcript_version_id"],
            run["refs"][0]["segment_id"],
            run["start_ticks"],
            run["end_ticks"],
        )
        for paragraph in paragraphs[1:]
        for run in paragraph["source_runs"]  # type: ignore[index]
    ] == [
        ("😀", "src_a", "tr_a", "seg_a_fine", 20_000, 40_000),
        ("另", "src_a", "tr_a", "seg_a_other", 800_000, 820_000),
        ("一", "src_a", "tr_a", "seg_a_other", 820_000, 840_000),
        ("人物", "src_a", "tr_a", "seg_a_other", 840_000, 880_000),
    ]

    right = _paragraph(reloaded, kind="source_excerpt", contains="一人物")
    next_selection = resolve_draft_editor_selection(
        reloaded,
        surface="draft",
        anchor={"paragraph_id": right["paragraph_id"], "offset": 0},
        focus={"paragraph_id": right["paragraph_id"], "offset": 1},
    )
    next_caret = resolve_draft_editor_caret(
        reloaded,
        paragraph_id=str(right["paragraph_id"]),
        offset=len(str(right["text"])),
    )
    next_move = edit_draft_candidate(
        reloaded,
        operation="move",
        selection=next_selection,
        caret=next_caret,
        accept_degraded=False,
    )
    assert next_move.content_draft.parent_draft_id == split.content_draft.content_draft_id


def test_two_complete_paragraphs_move_to_third_end_and_roundtrip_boundaries(
    tmp_path: Path,
) -> None:
    snapshot = _three_person_sectioned_snapshot(tmp_path)
    source = [item for item in snapshot.paragraphs if item["kind"] == "source_excerpt"]
    selection = resolve_draft_editor_selection(
        snapshot,
        surface="draft",
        anchor={"paragraph_id": source[0]["paragraph_id"], "offset": 0},
        focus={
            "paragraph_id": source[1]["paragraph_id"],
            "offset": len(str(source[1]["text"])),
        },
    )
    caret = resolve_draft_editor_caret(
        snapshot,
        paragraph_id=str(source[2]["paragraph_id"]),
        offset=len(str(source[2]["text"])),
    )
    mutation = edit_draft_candidate(
        snapshot,
        operation="move",
        selection=selection,
        caret=caret,
        accept_degraded=False,
    )
    reloaded = load_draft_editor_snapshot(
        Path(snapshot.workflow.project_path),
        source_bindings=[binding.to_dict() for binding in snapshot.workflow.source_bindings],
        content_draft_id=mutation.content_draft.content_draft_id,
    )

    paragraphs = [item for item in reloaded.paragraphs if item["kind"] == "source_excerpt"]
    assert [item["text"] for item in paragraphs] == ["重复你好", "你😀好", "另一人物"]
    assert [item["section_title"] for item in paragraphs] == ["三段", None, None]
    assert [
        ref["segment_id"]
        for paragraph in paragraphs
        for ref in paragraph["exact_refs"]  # type: ignore[index]
    ] == ["seg_b_fine", "seg_a_fine", "seg_a_other"]


def test_cross_two_paragraph_partial_move_preserves_internal_and_caret_boundaries(
    tmp_path: Path,
) -> None:
    snapshot = _three_person_sectioned_snapshot(tmp_path)
    source = [item for item in snapshot.paragraphs if item["kind"] == "source_excerpt"]
    selection = resolve_draft_editor_selection(
        snapshot,
        surface="draft",
        anchor={"paragraph_id": source[0]["paragraph_id"], "offset": 1},
        focus={"paragraph_id": source[1]["paragraph_id"], "offset": 2},
    )
    caret = resolve_draft_editor_caret(
        snapshot,
        paragraph_id=str(source[2]["paragraph_id"]),
        offset=2,
    )
    mutation = edit_draft_candidate(
        snapshot,
        operation="move",
        selection=selection,
        caret=caret,
        accept_degraded=False,
    )
    reloaded = load_draft_editor_snapshot(
        Path(snapshot.workflow.project_path),
        source_bindings=[binding.to_dict() for binding in snapshot.workflow.source_bindings],
        content_draft_id=mutation.content_draft.content_draft_id,
    )

    paragraphs = [item for item in reloaded.paragraphs if item["kind"] == "source_excerpt"]
    assert [item["text"] for item in paragraphs] == [
        "你",
        "人物",
        "重复",
        "😀好",
        "另一",
        "你好",
    ]
    assert [item["section_title"] for item in paragraphs] == ["三段", None, None, None, None, None]
    assert [
        (ref["segment_id"], ref["start_ticks"], ref["end_ticks"])
        for paragraph in paragraphs
        for ref in paragraph["exact_refs"]  # type: ignore[index]
    ] == [
        ("seg_a_fine", 0, 20_000),
        ("seg_a_other", 840_000, 880_000),
        ("seg_b_fine", 0, 40_000),
        ("seg_a_fine", 20_000, 60_000),
        ("seg_a_other", 800_000, 840_000),
        ("seg_b_fine", 40_000, 100_000),
    ]


def test_snapshot_supports_a_single_source_binding(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    root = Path(fixture["root"])
    project = ProjectStore(root).load()
    assert project.active_brief_id is not None
    bindings = [{"source_id": "src_a", "transcript_version_id": "tr_a"}]
    context = read_agent_context(
        root,
        source_id="src_a",
        transcript_version_id="tr_a",
        brief_id=project.active_brief_id,
        expected_revision=project.revision,
        offset=0,
        limit=20,
    )
    draft = create_content_draft(
        root,
        parent_draft_id=None,
        source_bindings=bindings,
        brief_id=project.active_brief_id,
        context_hash=context.context_hash,
        blocks=[
            {
                "block_id": "single_source",
                "kind": "source_excerpt",
                "refs": [
                    {
                        "source_id": "src_a",
                        "transcript_version_id": "tr_a",
                        "segment_id": "seg_a_fine",
                        "start_ticks": 0,
                        "end_ticks": 60_000,
                    }
                ],
                "canonical_text": "你😀好",
            }
        ],
        expected_revision=project.revision,
    )

    snapshot = load_draft_editor_snapshot(
        root,
        source_bindings=bindings,
        content_draft_id=draft.content_draft.content_draft_id,
    )

    assert [source["source_id"] for source in snapshot.sources] == ["src_a"]
    assert all(
        ref["source_id"] == "src_a"
        for paragraph in snapshot.paragraphs
        for ref in paragraph["exact_refs"]  # type: ignore[union-attr]
    )


def test_500_plus_transcript_window_preserves_identity_search_and_unicode_selection(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path, extra_segments=520)
    snapshot = load_draft_editor_snapshot(
        Path(fixture["root"]),
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=str(fixture["draft_id"]),
    )
    source_paragraphs = tuple(
        paragraph
        for paragraph in snapshot.transcript_paragraphs
        if paragraph["source_id"] == "src_a"
    )
    assert len(source_paragraphs) >= 500

    page = read_draft_transcript_window(
        snapshot,
        source_id="src_a",
        offset=240,
        limit=25,
    )
    assert page.offset == 240
    assert page.total == len(source_paragraphs)
    assert page.paragraphs == source_paragraphs[240:265]

    target = next(
        paragraph
        for paragraph in source_paragraphs
        if "跨窗口目标中文😀。" in str(paragraph["text"])
    )
    located = read_draft_transcript_window(
        snapshot,
        source_id="src_a",
        offset=0,
        limit=11,
        paragraph_id=str(target["paragraph_id"]),
    )
    assert target in located.paragraphs
    assert located.located_paragraph_id == target["paragraph_id"]
    assert next(
        paragraph
        for paragraph in located.paragraphs
        if paragraph["paragraph_id"] == target["paragraph_id"]
    ) == target

    searched = search_draft_editor(
        snapshot,
        surface="source",
        query="跨窗口目标",
        offset=0,
        limit=20,
    )
    assert searched.total == 1
    assert searched.matches[0]["paragraph_id"] == target["paragraph_id"]

    text = str(target["text"])
    emoji_offset = text.index("😀")
    selected = resolve_draft_editor_selection(
        snapshot,
        surface="source",
        anchor={
            "paragraph_id": target["paragraph_id"],
            "offset": emoji_offset,
            "offset_encoding": "codepoint",
        },
        focus={
            "paragraph_id": target["paragraph_id"],
            "offset": emoji_offset + 2,
            "offset_encoding": "utf16",
        },
    )
    assert selected.resolution.canonical_text == "😀"
    assert selected.resolution.refs[0].segment_id == target["refs"][0]["segment_id"]  # type: ignore[index]


def test_selection_exposes_the_full_resolved_display_range_after_fine_unit_snap(
    tmp_path: Path,
) -> None:
    fixture = _resolved_display_range_fixture(tmp_path)
    snapshot = load_draft_editor_snapshot(
        Path(fixture["root"]),
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=str(fixture["draft_id"]),
    )
    paragraph = _paragraph(snapshot, kind="source_excerpt", contains="主持人说")
    selection = resolve_draft_editor_selection(
        snapshot,
        surface="draft",
        anchor={"paragraph_id": paragraph["paragraph_id"], "offset": 3},
        focus={"paragraph_id": paragraph["paragraph_id"], "offset": 5},
    )

    assert paragraph["text"] == "主持人说：“请大家"
    assert selection.resolution is not None
    assert selection.resolution.canonical_text == "主持人说：“请大家"
    assert selection.resolution.adjusted is True
    assert [(ref.start_ticks, ref.end_ticks) for ref in selection.resolution.refs] == [
        (300_000, 422_727),
    ]
    payload = selection.to_dict()
    assert payload["display_range"]["anchor"]["character_offset"] == 3  # type: ignore[index]
    assert payload["display_range"]["focus"]["character_offset"] == 5  # type: ignore[index]
    assert payload["resolved_display_range"] == {  # type: ignore[index]
        "anchor": {
            "paragraph_id": paragraph["paragraph_id"],
            "character_offset": 0,
            "utf16_offset": 0,
        },
        "focus": {
            "paragraph_id": paragraph["paragraph_id"],
            "character_offset": 9,
            "utf16_offset": 9,
        },
    }


def test_resolved_display_range_keeps_unselected_editorial_edge_punctuation_out(
    tmp_path: Path,
) -> None:
    fixture = _resolved_display_range_fixture(
        tmp_path,
        display_text="，主持人说：“请大家！",
    )
    snapshot = load_draft_editor_snapshot(
        Path(fixture["root"]),
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=str(fixture["draft_id"]),
    )
    paragraph = _paragraph(snapshot, kind="source_excerpt", contains="主持人说")
    selection = resolve_draft_editor_selection(
        snapshot,
        surface="draft",
        anchor={"paragraph_id": paragraph["paragraph_id"], "offset": 4},
        focus={"paragraph_id": paragraph["paragraph_id"], "offset": 6},
    )

    resolved = selection.to_dict()["resolved_display_range"]
    assert resolved["anchor"]["character_offset"] == 1  # type: ignore[index]
    assert resolved["focus"]["character_offset"] == 10  # type: ignore[index]

    backward_payload = resolve_draft_editor_selection(
        snapshot,
        surface="draft",
        anchor={"paragraph_id": paragraph["paragraph_id"], "offset": 6},
        focus={"paragraph_id": paragraph["paragraph_id"], "offset": 4},
    ).to_dict()
    assert backward_payload["display_range"]["anchor"]["character_offset"] == 6  # type: ignore[index]
    assert backward_payload["display_range"]["focus"]["character_offset"] == 4  # type: ignore[index]
    backward = backward_payload["resolved_display_range"]
    assert backward["anchor"]["character_offset"] == 10  # type: ignore[index]
    assert backward["focus"]["character_offset"] == 1  # type: ignore[index]

    explicit_edges = resolve_draft_editor_selection(
        snapshot,
        surface="draft",
        anchor={"paragraph_id": paragraph["paragraph_id"], "offset": 0},
        focus={"paragraph_id": paragraph["paragraph_id"], "offset": 11},
    ).to_dict()["resolved_display_range"]
    assert explicit_edges["anchor"]["character_offset"] == 0  # type: ignore[index]
    assert explicit_edges["focus"]["character_offset"] == 11  # type: ignore[index]


def test_draft_display_point_honors_end_bias_for_resolved_caret(
    tmp_path: Path,
) -> None:
    fixture = _resolved_display_range_fixture(
        tmp_path,
        display_text="，主持人说：“请大家！",
    )
    snapshot = load_draft_editor_snapshot(
        Path(fixture["root"]),
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=str(fixture["draft_id"]),
    )
    paragraph = _paragraph(snapshot, kind="source_excerpt", contains="主持人说")
    selection = resolve_draft_editor_selection(
        snapshot,
        surface="draft",
        anchor={"paragraph_id": paragraph["paragraph_id"], "offset": 4},
        focus={"paragraph_id": paragraph["paragraph_id"], "offset": 6},
    )
    source_spans = tuple(span for span in snapshot._spans if span.source is not None)
    assert selection.resolution is not None
    resolved_end = draft_editor_module._draft_display_point(
        snapshot,
        source_spans,
        selection.resolution.end_caret,
        bias="end",
    )
    assert resolved_end["character_offset"] == 10


def test_fine_unit_delete_move_insert_and_stale_caret_create_immutable_children(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    root = Path(fixture["root"])
    original_id = str(fixture["draft_id"])
    snapshot = load_draft_editor_snapshot(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=original_id,
    )
    paragraph = _paragraph(snapshot, kind="source_excerpt", contains="你😀好")
    selection = resolve_draft_editor_selection(
        snapshot,
        surface="draft",
        anchor={
            "paragraph_id": paragraph["paragraph_id"],
            "offset": 1,
            "offset_encoding": "codepoint",
        },
        focus={
            "paragraph_id": paragraph["paragraph_id"],
            "offset": 3,
            "offset_encoding": "utf16",
        },
    )
    assert selection.resolution.direction == "forward"
    assert selection.resolution.canonical_text == "😀"
    assert selection.resolution.refs[0].start_ticks == 20_000
    assert selection.resolution.refs[0].end_ticks == 40_000
    selection_payload = selection.to_dict()
    assert selection_payload["display_range"] == {
        "anchor": {
            "paragraph_id": paragraph["paragraph_id"],
            "character_offset": 1,
            "utf16_offset": 1,
        },
        "focus": {
            "paragraph_id": paragraph["paragraph_id"],
            "character_offset": 2,
            "utf16_offset": 3,
        },
    }

    deleted = edit_draft_candidate(
        snapshot,
        operation="delete",
        selection=selection,
        caret=None,
        accept_degraded=False,
    )
    assert deleted.content_draft.parent_draft_id == original_id
    assert deleted.content_draft.confirmed_by_user is False
    assert deleted.project_revision == 2
    assert "😀" not in "".join(
        block.canonical_text
        for block in deleted.content_draft.blocks
        if hasattr(block, "canonical_text")
    )
    assert (root / "content-drafts" / f"{original_id}.json").is_file()

    moved_snapshot = load_draft_editor_snapshot(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=original_id,
    )
    moved_paragraph = _paragraph(moved_snapshot, kind="source_excerpt", contains="你😀好")
    moved_selection = resolve_draft_editor_selection(
        moved_snapshot,
        surface="draft",
        anchor={"paragraph_id": moved_paragraph["paragraph_id"], "offset": 0},
        focus={"paragraph_id": moved_paragraph["paragraph_id"], "offset": 1},
    )
    caret = resolve_draft_editor_caret(
        moved_snapshot,
        paragraph_id=moved_paragraph["paragraph_id"],
        offset=3,
        offset_encoding="codepoint",
    )
    moved = edit_draft_candidate(
        moved_snapshot,
        operation="move",
        selection=moved_selection,
        caret=caret,
        accept_degraded=False,
    )
    moved_text = "".join(
        block.canonical_text
        for block in moved.content_draft.blocks
        if hasattr(block, "canonical_text")
    )
    assert moved_text.startswith("😀好你")

    source_paragraph = next(
        item
        for item in snapshot.transcript_paragraphs
        if item["source_id"] == "src_b"
    )
    source_selection = resolve_draft_editor_selection(
        moved_snapshot,
        surface="source",
        anchor={"paragraph_id": source_paragraph["paragraph_id"], "offset": 0},
        focus={"paragraph_id": source_paragraph["paragraph_id"], "offset": 1},
    )
    inserted = edit_draft_candidate(
        moved_snapshot,
        operation="insert",
        selection=source_selection,
        caret=caret,
        accept_degraded=False,
    )
    assert inserted.content_draft.parent_draft_id == original_id
    assert any(
        getattr(block, "canonical_text", "") == "重"
        for block in inserted.content_draft.blocks
    )

    child_snapshot = load_draft_editor_snapshot(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=deleted.content_draft.content_draft_id,
    )
    child_paragraph = _paragraph(
        child_snapshot,
        kind="source_excerpt",
        contains="你好",
    )
    child_selection = resolve_draft_editor_selection(
        child_snapshot,
        surface="draft",
        anchor={"paragraph_id": child_paragraph["paragraph_id"], "offset": 0},
        focus={"paragraph_id": child_paragraph["paragraph_id"], "offset": 1},
    )
    with pytest.raises(ProjectError, match="caret is stale"):
        edit_draft_candidate(
            child_snapshot,
            operation="move",
            selection=child_selection,
            caret=caret,
            accept_degraded=False,
        )

    source_b = _paragraph(snapshot, kind="source_excerpt", contains="重复你好")
    inside_selection = resolve_draft_editor_selection(
        snapshot,
        surface="draft",
        anchor={"paragraph_id": source_b["paragraph_id"], "offset": 0},
        focus={"paragraph_id": source_b["paragraph_id"], "offset": 2},
    )
    inside_caret = resolve_draft_editor_caret(
        snapshot,
        paragraph_id=source_b["paragraph_id"],
        offset=1,
    )
    with pytest.raises(ProjectError, match="inside the moved selection"):
        edit_draft_candidate(
            snapshot,
            operation="move",
            selection=inside_selection,
            caret=inside_caret,
            accept_degraded=False,
        )


def test_gap_boundary_delete_move_insert_and_section_split_keep_truthful_ranges(
    tmp_path: Path,
) -> None:
    fixture = _gap_boundary_fixture(tmp_path)
    root = Path(fixture["root"])
    snapshot = load_draft_editor_snapshot(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=str(fixture["draft_id"]),
    )
    paragraph = _paragraph(snapshot, kind="source_excerpt", contains="甲，乙")
    assert paragraph["text"] == "甲，乙 丙。"

    def source_evidence(candidate):  # type: ignore[no-untyped-def]
        return [
            (
                ref.start_ticks,
                ref.end_ticks,
                block.canonical_text,
            )
            for block in candidate.blocks
            if hasattr(block, "refs")
            for ref in block.refs
        ]

    def assert_gap_is_not_referenced(evidence) -> None:  # type: ignore[no-untyped-def]
        assert all(end <= 200 or start >= 250 for start, end, _text in evidence)

    selection = resolve_draft_editor_selection(
        snapshot,
        surface="draft",
        anchor={"paragraph_id": paragraph["paragraph_id"], "offset": 2},
        focus={"paragraph_id": paragraph["paragraph_id"], "offset": 3},
    )
    assert selection.resolution.canonical_text == "乙 "
    assert [
        (ref.start_ticks, ref.end_ticks, ref.canonical_text)
        for ref in selection.resolution.refs
    ] == [(250, 350, "乙 ")]

    deleted = prepare_draft_candidate(
        snapshot,
        operation="delete",
        selection=selection,
        caret=None,
        accept_degraded=False,
        child_id="draft_gap_delete",
    )
    deleted_evidence = source_evidence(deleted)
    assert deleted_evidence == [(100, 200, "甲，"), (380, 480, "丙。")]
    assert_gap_is_not_referenced(deleted_evidence)
    assert "".join(block.canonical_text for block in deleted.blocks if hasattr(block, "canonical_text")) == "甲，丙。"

    start_caret = resolve_draft_editor_caret(
        snapshot,
        paragraph_id=str(paragraph["paragraph_id"]),
        offset=0,
    )
    prepared_move = prepare_draft_candidate_with_placement(
        snapshot,
        operation="move",
        selection=selection,
        caret=start_caret,
        accept_degraded=False,
        child_id="draft_gap_move",
    )
    moved = prepared_move.child
    assert prepared_move.placement is not None
    moved_placement = materialize_draft_editor_placement(
        snapshot, moved, prepared_move.placement,
    )
    assert moved_placement.display_range is not None
    assert moved_placement.display_range[0]["character_offset"] == 0
    assert moved_placement.display_range[1]["character_offset"] == 2
    assert moved_placement.display_range[0]["paragraph_id"] == moved_placement.display_range[1]["paragraph_id"]
    moved_evidence = source_evidence(moved)
    assert moved_evidence == [
        (250, 350, "乙 "),
        (100, 200, "甲，"),
        (380, 480, "丙。"),
    ]
    assert_gap_is_not_referenced(moved_evidence)
    assert "".join(block.canonical_text for block in moved.blocks if hasattr(block, "canonical_text")) == "乙 甲，丙。"

    source_paragraph = snapshot.transcript_paragraphs[0]
    source_selection = resolve_draft_editor_selection(
        snapshot,
        surface="source",
        anchor={"paragraph_id": source_paragraph["paragraph_id"], "offset": 0},
        focus={"paragraph_id": source_paragraph["paragraph_id"], "offset": 2},
    )
    assert source_selection.resolution.canonical_text == "甲，"
    boundary_caret = resolve_draft_editor_caret(
        snapshot,
        paragraph_id=str(paragraph["paragraph_id"]),
        offset=2,
    )
    prepared_insert = prepare_draft_candidate_with_placement(
        snapshot,
        operation="insert",
        selection=source_selection,
        caret=boundary_caret,
        accept_degraded=False,
        child_id="draft_gap_insert",
    )
    inserted = prepared_insert.child
    assert prepared_insert.placement is not None
    inserted_placement = materialize_draft_editor_placement(
        snapshot, inserted, prepared_insert.placement,
    )
    assert inserted_placement.display_range is not None
    assert inserted_placement.display_range[0]["character_offset"] == 2
    assert inserted_placement.display_range[1]["character_offset"] == 4
    inserted_evidence = source_evidence(inserted)
    assert inserted_evidence == [
        (100, 200, "甲，"),
        (100, 200, "甲，"),
        (250, 500, "乙 丙。"),
    ]
    assert_gap_is_not_referenced(inserted_evidence)
    assert "".join(block.canonical_text for block in inserted.blocks if hasattr(block, "canonical_text")) == "甲，甲，乙 丙。"

    split = prepare_section_operation(
        snapshot,
        operation="section_split",
        payload={
            "heading_block_id": "gap_section",
            "target": {
                "paragraph_id": paragraph["paragraph_id"],
                "block_id": "gap_block",
                "utf16_offset": 2,
            },
            "title": "新章节",
        },
        child_id="draft_gap_section_split",
    )
    split_sources = [block for block in split.blocks if hasattr(block, "refs")]
    split_evidence = source_evidence(split)
    assert [(block.canonical_text, block.refs[0].start_ticks, block.refs[0].end_ticks) for block in split_sources] == [
        ("甲，", 100, 200),
        ("乙 丙。", 250, 500),
    ]
    assert_gap_is_not_referenced(split_evidence)
    assert "".join(block.canonical_text for block in split_sources) == "甲，乙 丙。"


@pytest.mark.parametrize(
    "bad_units",
    [
        (
            FineUnit("character", "甲", 100, 200, None),
            FineUnit("character", "，", 190, 250, None),
            FineUnit("character", "乙", 250, 350, None),
        ),
        (
            FineUnit("character", "，", 200, 250, None),
            FineUnit("character", "甲", 100, 200, None),
            FineUnit("character", "乙", 250, 350, None),
        ),
        (),
    ],
)
def test_gap_boundary_bad_fine_units_fail_closed_without_write(
    tmp_path: Path,
    bad_units: tuple[FineUnit, ...],
) -> None:
    fixture = _gap_boundary_fixture(tmp_path, fine_units=bad_units)
    root = Path(fixture["root"])
    snapshot = load_draft_editor_snapshot(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=str(fixture["draft_id"]),
    )
    paragraph = _paragraph(snapshot, kind="source_excerpt", contains="甲，乙")
    selection = resolve_draft_editor_selection(
        snapshot,
        surface="draft",
        anchor={"paragraph_id": paragraph["paragraph_id"], "offset": 0},
        focus={"paragraph_id": paragraph["paragraph_id"], "offset": 1},
    )
    before_files = sorted((root / "content-drafts").glob("*.json"))
    before_revision = ProjectStore(root).load().revision
    with pytest.raises(ProjectError, match="accepted"):
        edit_draft_candidate(
            snapshot,
            operation="delete",
            selection=selection,
            caret=None,
            accept_degraded=False,
        )
    assert sorted((root / "content-drafts").glob("*.json")) == before_files
    assert ProjectStore(root).load().revision == before_revision


def test_gap_boundary_fine_unit_identity_mismatch_fails_closed_without_write(
    tmp_path: Path,
) -> None:
    fixture = _gap_boundary_fixture(tmp_path)
    root = Path(fixture["root"])
    snapshot = load_draft_editor_snapshot(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=str(fixture["draft_id"]),
    )
    paragraph = _paragraph(snapshot, kind="source_excerpt", contains="甲，乙")
    selection = resolve_draft_editor_selection(
        snapshot,
        surface="draft",
        anchor={"paragraph_id": paragraph["paragraph_id"], "offset": 2},
        focus={"paragraph_id": paragraph["paragraph_id"], "offset": 3},
    )
    assert selection.resolution is not None
    end_caret = selection.resolution.end_caret
    assert end_caret.left is not None
    corrupted_end_caret = replace(
        end_caret,
        left=replace(end_caret.left, fine_unit_index=0),
    )
    corrupted_selection = replace(
        selection,
        resolution=replace(selection.resolution, end_caret=corrupted_end_caret),
    )
    before_files = sorted((root / "content-drafts").glob("*.json"))
    with pytest.raises(ProjectError, match="invalid"):
        edit_draft_candidate(
            snapshot,
            operation="delete",
            selection=corrupted_selection,
            caret=None,
            accept_degraded=False,
        )
    assert sorted((root / "content-drafts").glob("*.json")) == before_files


def test_compound_selection_spans_twelve_refs_and_two_sources_for_delete_and_move(
    tmp_path: Path,
) -> None:
    fixture = _compound_fixture(tmp_path)
    root = Path(fixture["root"])
    parent_id = str(fixture["draft_id"])
    parent_path = root / "content-drafts" / f"{parent_id}.json"
    immutable_paths = (
        root / "transcripts" / "src_a" / "tr_a.json",
        root / "transcripts" / "src_b" / "tr_b.json",
        parent_path,
    )
    immutable_bytes = {path: path.read_bytes() for path in immutable_paths}
    snapshot = load_draft_editor_snapshot(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=parent_id,
    )
    compound = [
        paragraph
        for paragraph in snapshot.paragraphs
        if len(paragraph["exact_refs"]) == 12  # type: ignore[arg-type]
    ]
    assert len(compound) == 1
    assert {
        run["source_id"] for run in compound[0]["source_runs"]  # type: ignore[union-attr]
    } == {"src_a", "src_b"}

    forward = resolve_draft_editor_selection(
        snapshot,
        surface="draft",
        anchor={"paragraph_id": compound[0]["paragraph_id"], "offset": 0},
        focus={
            "paragraph_id": compound[0]["paragraph_id"],
            "offset": len(str(compound[0]["text"])),
        },
    )
    backward = resolve_draft_editor_selection(
        snapshot,
        surface="draft",
        anchor={
            "paragraph_id": compound[0]["paragraph_id"],
            "offset": len(str(compound[0]["text"])),
        },
        focus={"paragraph_id": compound[0]["paragraph_id"], "offset": 0},
    )
    expected_ids = [
        *(f"seg_a_{index}" for index in range(6)),
        *(f"seg_b_{index}" for index in range(6)),
    ]
    assert [ref.segment_id for ref in forward.resolution.refs] == expected_ids
    assert [ref.segment_id for ref in backward.resolution.refs] == expected_ids
    assert forward.resolution.direction == "forward"
    assert backward.resolution.direction == "backward"
    expected_correspondence = [
        {
            "source_id": "src_a",
            "source_display_name": "素材 src_a",
            "paragraph_id": compound[0]["source_runs"][0]["paragraph_id"],  # type: ignore[index]
            "start_offset": compound[0]["source_runs"][0]["source_start_offset"],  # type: ignore[index]
            "end_offset": compound[0]["source_runs"][5]["source_end_offset"],  # type: ignore[index]
        },
        {
            "source_id": "src_b",
            "source_display_name": "素材 src_b",
            "paragraph_id": compound[0]["source_runs"][6]["paragraph_id"],  # type: ignore[index]
            "start_offset": compound[0]["source_runs"][6]["source_start_offset"],  # type: ignore[index]
            "end_offset": compound[0]["source_runs"][11]["source_end_offset"],  # type: ignore[index]
        },
    ]
    assert forward.to_dict()["correspondence_groups"] == expected_correspondence
    assert backward.to_dict()["correspondence_groups"] == expected_correspondence

    tail = next(
        paragraph
        for paragraph in snapshot.paragraphs
        if "收尾段" in str(paragraph["text"])
    )
    across_paragraphs = resolve_draft_editor_selection(
        snapshot,
        surface="draft",
        anchor={"paragraph_id": compound[0]["paragraph_id"], "offset": 0},
        focus={
            "paragraph_id": tail["paragraph_id"],
            "offset": len(str(tail["text"])),
        },
    )
    assert [ref.segment_id for ref in across_paragraphs.resolution.refs] == [
        *expected_ids,
        "seg_b_tail",
    ]

    deleted = edit_draft_candidate(
        snapshot,
        operation="delete",
        selection=forward,
        caret=None,
        accept_degraded=False,
    )
    assert deleted.content_draft.parent_draft_id == parent_id
    assert [
        ref.segment_id
        for block in deleted.content_draft.blocks
        for ref in getattr(block, "refs", ())
    ] == ["seg_b_tail"]

    caret = resolve_draft_editor_caret(
        snapshot,
        paragraph_id=str(tail["paragraph_id"]),
        offset=len(str(tail["text"])),
    )
    moved = edit_draft_candidate(
        snapshot,
        operation="move",
        selection=backward,
        caret=caret,
        accept_degraded=False,
    )
    assert moved.content_draft.parent_draft_id == parent_id
    assert [
        ref.segment_id
        for block in moved.content_draft.blocks
        for ref in getattr(block, "refs", ())
    ] == ["seg_b_tail", *expected_ids]
    for path, before in immutable_bytes.items():
        assert path.read_bytes() == before


def test_correspondence_groups_split_skipped_speech_inside_one_display_paragraph(
    tmp_path: Path,
) -> None:
    fixture = _compound_fixture(tmp_path)
    root = Path(fixture["root"])
    project = ProjectStore(root).load()
    assert project.active_brief_id is not None
    context = read_multi_source_agent_context(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        brief_id=project.active_brief_id,
        expected_revision=project.revision,
        offset=0,
        limit=20,
    )
    draft = create_content_draft(
        root,
        parent_draft_id=str(fixture["draft_id"]),
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        brief_id=project.active_brief_id,
        context_hash=context.context_hash,
        blocks=[
            {
                "block_id": f"skipped_speech_{index}",
                "kind": "source_excerpt",
                "refs": [
                    {
                        "source_id": "src_a",
                        "transcript_version_id": "tr_a",
                        "segment_id": segment_id,
                        "start_ticks": index * 60_000,
                        "end_ticks": index * 60_000 + 40_000,
                    }
                ],
                "canonical_text": f"甲{index}",
            }
            for index, segment_id in ((0, "seg_a_0"), (2, "seg_a_2"))
        ],
        expected_revision=project.revision,
    )
    snapshot = load_draft_editor_snapshot(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=draft.content_draft.content_draft_id,
    )
    paragraphs = snapshot.paragraphs
    assert len(paragraphs) == 1
    selection = resolve_draft_editor_selection(
        snapshot,
        surface="draft",
        anchor={"paragraph_id": paragraphs[0]["paragraph_id"], "offset": 0},
        focus={
            "paragraph_id": paragraphs[0]["paragraph_id"],
            "offset": len(str(paragraphs[0]["text"])),
        },
    )

    groups = selection.to_dict()["correspondence_groups"]
    assert len(groups) == 2  # type: ignore[arg-type]
    assert [
        (group["start_offset"], group["end_offset"])  # type: ignore[index]
        for group in groups  # type: ignore[union-attr]
    ] == [
        (
            paragraphs[0]["source_runs"][0]["source_start_offset"],  # type: ignore[index]
            paragraphs[0]["source_runs"][0]["source_end_offset"],  # type: ignore[index]
        ),
        (
            paragraphs[0]["source_runs"][1]["source_start_offset"],  # type: ignore[index]
            paragraphs[0]["source_runs"][1]["source_end_offset"],  # type: ignore[index]
        ),
    ]


def test_reverse_selection_degraded_acceptance_and_cross_person_rejection(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    snapshot = load_draft_editor_snapshot(
        Path(fixture["root"]),
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=str(fixture["draft_id"]),
    )
    fine = _paragraph(snapshot, kind="source_excerpt", contains="你😀好")
    reverse = resolve_draft_editor_selection(
        snapshot,
        surface="draft",
        anchor={"paragraph_id": fine["paragraph_id"], "offset": 3},
        focus={"paragraph_id": fine["paragraph_id"], "offset": 1},
    )
    assert reverse.resolution.direction == "backward"
    assert reverse.resolution.canonical_text == "😀好"

    coarse = _paragraph(snapshot, kind="source_excerpt", contains="没有细分")
    coarse_start = str(coarse["text"]).index("没有细分")
    degraded = resolve_draft_editor_selection(
        snapshot,
        surface="draft",
        anchor={
            "paragraph_id": coarse["paragraph_id"],
            "offset": coarse_start + 1,
        },
        focus={
            "paragraph_id": coarse["paragraph_id"],
            "offset": coarse_start + 2,
        },
    )
    assert degraded.resolution.degraded is True
    assert "missing_fine_units" in degraded.resolution.degradation_reasons
    with pytest.raises(ProjectError, match="accepted"):
        edit_draft_candidate(
            snapshot,
            operation="delete",
            selection=degraded,
            caret=None,
            accept_degraded=False,
        )
    accepted = edit_draft_candidate(
        snapshot,
        operation="delete",
        selection=degraded,
        caret=None,
        accept_degraded=True,
    )
    assert all(
        getattr(block, "canonical_text", "") != "没有细分"
        for block in accepted.content_draft.blocks
    )

    other_source = next(
        item
        for item in snapshot.transcript_paragraphs
        if item["source_id"] == "src_a" and item["person_name"] == "人物乙"
    )
    with pytest.raises(ProjectError, match="person"):
        resolve_draft_editor_selection(
            snapshot,
            surface="source",
            anchor={"paragraph_id": fine["source_runs"][0]["paragraph_id"], "offset": 0},
            focus={"paragraph_id": other_source["paragraph_id"], "offset": 1},
        )

    source_a = [
        item
        for item in snapshot.transcript_paragraphs
        if item["source_id"] == "src_a" and item["person_name"] == "人物甲"
    ]
    assert len(source_a) == 2
    across_paragraphs = resolve_draft_editor_selection(
        snapshot,
        surface="source",
        anchor={"paragraph_id": source_a[0]["paragraph_id"], "offset": 0},
        focus={
            "paragraph_id": source_a[1]["paragraph_id"],
            "offset": len(str(source_a[1]["text"])),
        },
    )
    assert [ref.segment_id for ref in across_paragraphs.resolution.refs] == [
        "seg_a_fine",
        "seg_a_coarse",
    ]
    assert across_paragraphs.resolution.degraded is True

    source_b = next(
        item for item in snapshot.transcript_paragraphs if item["source_id"] == "src_b"
    )
    with pytest.raises(ProjectError, match="source binding"):
        resolve_draft_editor_selection(
            snapshot,
            surface="source",
            anchor={"paragraph_id": source_a[0]["paragraph_id"], "offset": 0},
            focus={"paragraph_id": source_b["paragraph_id"], "offset": 1},
        )
    with pytest.raises(ProjectError, match="does not exist"):
        resolve_draft_editor_selection(
            snapshot,
            surface="draft",
            anchor={"paragraph_id": "draft_paragraph_unknown", "offset": 0},
            focus={"paragraph_id": fine["paragraph_id"], "offset": 1},
        )


def test_candidate_write_failure_and_stale_revision_leave_no_partial_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path)
    root = Path(fixture["root"])
    snapshot = load_draft_editor_snapshot(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=str(fixture["draft_id"]),
    )
    paragraph = _paragraph(snapshot, kind="source_excerpt", contains="你😀好")
    selection = resolve_draft_editor_selection(
        snapshot,
        surface="draft",
        anchor={"paragraph_id": paragraph["paragraph_id"], "offset": 0},
        focus={"paragraph_id": paragraph["paragraph_id"], "offset": 1},
    )
    before_revision = ProjectStore(root).load().revision
    before_artifacts = {
        path.name for path in (root / "content-drafts").glob("*.json")
    }

    def fail_write(_path: Path, _payload: dict[str, object]) -> None:
        raise OSError("injected artifact failure")

    monkeypatch.setattr(content_drafts_module, "write_new_json", fail_write)
    with pytest.raises(OSError, match="injected artifact failure"):
        edit_draft_candidate(
            snapshot,
            operation="delete",
            selection=selection,
            caret=None,
            accept_degraded=False,
        )
    assert ProjectStore(root).load().revision == before_revision
    assert {
        path.name for path in (root / "content-drafts").glob("*.json")
    } == before_artifacts

    monkeypatch.undo()
    current = ProjectStore(root).load()
    ProjectStore(root).save(
        replace(current, revision=current.revision + 1),
        expected_revision=current.revision,
    )
    with pytest.raises(ProjectError, match="stale|revision"):
        resolve_draft_editor_selection(
            snapshot,
            surface="draft",
            anchor={"paragraph_id": paragraph["paragraph_id"], "offset": 0},
            focus={"paragraph_id": paragraph["paragraph_id"], "offset": 1},
        )
    assert {
        path.name for path in (root / "content-drafts").glob("*.json")
    } == before_artifacts


def test_search_is_complete_stable_and_read_only(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    root = Path(fixture["root"])
    snapshot = load_draft_editor_snapshot(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=str(fixture["draft_id"]),
    )
    before = (ProjectStore(root).load(), snapshot.candidate.content_draft_id)

    source_matches = search_draft_editor(
        snapshot,
        surface="source",
        query="你",
        offset=0,
        limit=1,
    )
    second = search_draft_editor(
        snapshot,
        surface="source",
        query="你",
        offset=1,
        limit=10,
    )
    draft_matches = search_draft_editor(
        snapshot,
        surface="draft",
        query="你",
        offset=0,
        limit=10,
    )

    assert source_matches.total >= 2
    assert source_matches.next_cursor == 1
    assert second.matches[0]["match_id"] != source_matches.matches[0]["match_id"]
    assert all("occurrence" in item for item in (*source_matches.matches, *second.matches))
    assert draft_matches.total >= 2
    assert ProjectStore(root).load() == before[0]
    assert snapshot.candidate.content_draft_id == before[1]


def test_narration_update_creates_child_without_rewriting_source_speech(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    root = Path(fixture["root"])
    original_id = str(fixture["draft_id"])
    snapshot = load_draft_editor_snapshot(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=original_id,
    )

    changed = update_draft_narration(
        snapshot,
        block_id="block_narration",
        text="更新后的待录音解说",
    )

    assert changed.content_draft.parent_draft_id == original_id
    narration = next(
        block for block in changed.content_draft.blocks if block.kind == "narration"
    )
    assert narration.text == "更新后的待录音解说"
    assert narration.status == "draft"
    assert narration.recorded_refs == ()
    with pytest.raises(ProjectError, match="narration"):
        update_draft_narration(
            snapshot,
            block_id="block_a_fine",
            text="伪造同期声",
        )
    assert "你😀好" in (root / "content-drafts" / f"{original_id}.json").read_text(
        encoding="utf-8"
    )


def test_punctuation_prepare_changes_display_only_and_rejects_surrogate_midpoint(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    snapshot = load_draft_editor_snapshot(
        Path(fixture["root"]),
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=str(fixture["draft_id"]),
    )
    paragraph = _paragraph(snapshot, kind="source_excerpt", contains="你😀好")
    prepared = prepare_draft_punctuation(
        snapshot,
        paragraph_id=str(paragraph["paragraph_id"]),
        block_id="block_a_fine",
        start_utf16_offset=3,
        end_utf16_offset=3,
        replacement="！",
        child_id="draft_punctuation_child",
    )
    block = next(
        block for block in prepared.blocks if block.block_id == "block_a_fine"
    )
    assert isinstance(block, SourceExcerptBlock)
    assert block.canonical_text == "你😀好"
    assert block.display_text == "你😀！好"
    assert prepared.schema_version == 2

    with pytest.raises(ProjectError, match="surrogate"):
        prepare_draft_punctuation(
            snapshot,
            paragraph_id=str(paragraph["paragraph_id"]),
            block_id="block_a_fine",
            start_utf16_offset=2,
            end_utf16_offset=2,
            replacement="！",
            child_id="draft_punctuation_surrogate",
        )


def test_punctuation_prepare_counts_adjacent_existing_run_and_rejects_cross_block_range(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    root = Path(fixture["root"])
    base = load_draft_editor_snapshot(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=str(fixture["draft_id"]),
    )
    seeded = create_content_draft_editor_child(
        root,
        parent=base.candidate,
        blocks=tuple(
            replace(block, display_text="你😀好。。。。。。。")
            if isinstance(block, SourceExcerptBlock)
            and block.block_id == "block_a_fine"
            else block
            for block in base.candidate.blocks
        ),
        expected_revision=base.workflow.project_revision,
    )
    snapshot = load_draft_editor_snapshot(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=seeded.content_draft.content_draft_id,
    )
    paragraph = _paragraph(snapshot, kind="source_excerpt", contains="你😀好")
    end = len("你😀好".encode("utf-16-le")) // 2
    accepted = prepare_draft_punctuation(
        snapshot,
        paragraph_id=str(paragraph["paragraph_id"]),
        block_id="block_a_fine",
        start_utf16_offset=end,
        end_utf16_offset=end,
        replacement="！",
        child_id="draft_punctuation_eighth",
    )
    accepted_block = next(block for block in accepted.blocks if block.block_id == "block_a_fine")
    assert isinstance(accepted_block, SourceExcerptBlock)
    assert accepted_block.display_text == "你😀好！。。。。。。。"
    with pytest.raises(ProjectError, match="run exceeds"):
        prepare_draft_punctuation(
            snapshot,
            paragraph_id=str(paragraph["paragraph_id"]),
            block_id="block_a_fine",
            start_utf16_offset=end,
            end_utf16_offset=end,
            replacement="！！",
            child_id="draft_punctuation_ninth",
        )

    grouped = _different_person_sectioned_snapshot(tmp_path / "grouped")
    grouped_root = Path(grouped.workflow.project_path)
    grouped_child = create_content_draft_editor_child(
        grouped_root,
        parent=grouped.candidate,
        blocks=tuple(
            replace(block, display_text="另一……")
            if isinstance(block, SourceExcerptBlock)
            and block.block_id == "person_other_paragraph_left"
            else replace(block, display_text="人物！")
            if isinstance(block, SourceExcerptBlock)
            and block.block_id == "person_other_paragraph_right"
            else block
            for block in grouped.candidate.blocks
        ),
        expected_revision=grouped.workflow.project_revision,
    )
    grouped_snapshot = load_draft_editor_snapshot(
        grouped_root,
        source_bindings=[
            {"source_id": "src_a", "transcript_version_id": "tr_a"},
            {"source_id": "src_b", "transcript_version_id": "tr_b"},
        ],
        content_draft_id=grouped_child.content_draft.content_draft_id,
    )
    grouped_paragraph = _paragraph(grouped_snapshot, kind="source_excerpt", contains="另一")
    assert str(grouped_paragraph["text"]) == "另一……人物！"
    boundary = len("另一……")
    at_right = prepare_draft_punctuation(
        grouped_snapshot,
        paragraph_id=str(grouped_paragraph["paragraph_id"]),
        block_id="person_other_paragraph_right",
        start_utf16_offset=boundary,
        end_utf16_offset=boundary,
        replacement="？",
        child_id="draft_punctuation_right_boundary",
    )
    right_block = next(
        block for block in at_right.blocks if block.block_id == "person_other_paragraph_right"
    )
    assert isinstance(right_block, SourceExcerptBlock)
    assert right_block.display_text == "？人物！"
    with pytest.raises(ProjectError, match="crosses source blocks"):
        prepare_draft_punctuation(
            grouped_snapshot,
            paragraph_id=str(grouped_paragraph["paragraph_id"]),
            block_id="person_other_paragraph_left",
            start_utf16_offset=1,
            end_utf16_offset=boundary + 1,
            replacement="？",
            child_id="draft_punctuation_cross_block",
        )

    left_punctuation_deleted = prepare_draft_punctuation(
        grouped_snapshot,
        paragraph_id=str(grouped_paragraph["paragraph_id"]),
        block_id="person_other_paragraph_left",
        start_utf16_offset=boundary - 1,
        end_utf16_offset=boundary,
        replacement="",
        child_id="draft_punctuation_delete_left_block_end",
    )
    left_block = next(
        block
        for block in left_punctuation_deleted.blocks
        if block.block_id == "person_other_paragraph_left"
    )
    right_block = next(
        block
        for block in left_punctuation_deleted.blocks
        if block.block_id == "person_other_paragraph_right"
    )
    assert isinstance(left_block, SourceExcerptBlock)
    assert isinstance(right_block, SourceExcerptBlock)
    assert left_block.display_text == "另一…"
    assert right_block.display_text == "人物！"

    right_seeded = create_content_draft_editor_child(
        grouped_root,
        parent=grouped_snapshot.candidate,
        blocks=tuple(
            replace(block, display_text="另一……")
            if isinstance(block, SourceExcerptBlock)
            and block.block_id == "person_other_paragraph_left"
            else replace(block, display_text="！人物！")
            if isinstance(block, SourceExcerptBlock)
            and block.block_id == "person_other_paragraph_right"
            else block
            for block in grouped_snapshot.candidate.blocks
        ),
        expected_revision=grouped_snapshot.workflow.project_revision,
    )
    right_snapshot = load_draft_editor_snapshot(
        grouped_root,
        source_bindings=[
            {"source_id": "src_a", "transcript_version_id": "tr_a"},
            {"source_id": "src_b", "transcript_version_id": "tr_b"},
        ],
        content_draft_id=right_seeded.content_draft.content_draft_id,
    )
    right_paragraph = _paragraph(right_snapshot, kind="source_excerpt", contains="人物")
    right_boundary = len("另一……")
    right_punctuation_deleted = prepare_draft_punctuation(
        right_snapshot,
        paragraph_id=str(right_paragraph["paragraph_id"]),
        block_id="person_other_paragraph_right",
        start_utf16_offset=right_boundary,
        end_utf16_offset=right_boundary + 1,
        replacement="",
        child_id="draft_punctuation_delete_right_block_start",
    )
    right_block = next(
        block
        for block in right_punctuation_deleted.blocks
        if block.block_id == "person_other_paragraph_right"
    )
    assert isinstance(right_block, SourceExcerptBlock)
    assert right_block.display_text == "人物！"


def test_punctuation_prepare_counts_fixed_newline_for_second_and_third_ref_offsets(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    root = Path(fixture["root"])
    third_text = "另一人物"
    base = load_draft_editor_snapshot(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=str(fixture["draft_id"]),
    )
    refs = (
        ContentDraftRef("src_a", "tr_a", "seg_a_fine", 0, 60_000),
        ContentDraftRef("src_a", "tr_a", "seg_a_coarse", 400_000, 500_000),
        ContentDraftRef("src_a", "tr_a", "seg_a_other", 800_000, 880_000),
    )
    multi_ref = SourceExcerptBlock(
        "block_multi_punctuation",
        refs,
        f"你😀好\n没有细分\n{third_text}",
        display_text=f"你😀好！！\n？没有细分……\n！{third_text}",
    )
    seeded = create_content_draft_editor_child(
        root,
        parent=base.candidate,
        blocks=(multi_ref,),
        expected_revision=base.workflow.project_revision,
    )
    snapshot = load_draft_editor_snapshot(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=seeded.content_draft.content_draft_id,
    )
    paragraph = _paragraph(snapshot, kind="source_excerpt", contains="另一人物")
    third_run = next(
        run
        for run in paragraph["source_runs"]
        if run["refs"][0]["segment_id"] == "seg_a_other"
    )
    third_punctuation_offset = int(third_run["start_offset"]) + 1
    prepared = prepare_draft_punctuation(
        snapshot,
        paragraph_id=str(paragraph["paragraph_id"]),
        block_id="block_multi_punctuation",
        start_utf16_offset=third_punctuation_offset,
        end_utf16_offset=third_punctuation_offset,
        replacement="？",
        child_id="draft_punctuation_third_ref",
    )
    changed = next(
        block for block in prepared.blocks if block.block_id == "block_multi_punctuation"
    )
    assert isinstance(changed, SourceExcerptBlock)
    assert changed.display_text == f"你😀好！！\n？没有细分……\n！？{third_text}"


def test_punctuation_visible_second_ref_offset_counts_emoji_and_fixed_newline(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    root = Path(fixture["root"])
    base = load_draft_editor_snapshot(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=str(fixture["draft_id"]),
    )
    refs = (
        ContentDraftRef("src_a", "tr_a", "seg_a_fine", 0, 60_000),
        ContentDraftRef("src_a", "tr_a", "seg_a_coarse", 400_000, 500_000),
    )
    multi_ref = SourceExcerptBlock(
        "block_multi_visible_offset",
        refs,
        "你😀好\n没有细分",
        display_text="你😀好！\n没有细分",
    )
    seeded = create_content_draft_editor_child(
        root,
        parent=base.candidate,
        blocks=(multi_ref,),
        expected_revision=base.workflow.project_revision,
    )
    snapshot = load_draft_editor_snapshot(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=seeded.content_draft.content_draft_id,
    )
    paragraph = _paragraph(snapshot, kind="source_excerpt", contains="没有细分")
    visible_after_me = len("你😀好！没".encode("utf-16-le")) // 2
    inserted = prepare_draft_punctuation(
        snapshot,
        paragraph_id=str(paragraph["paragraph_id"]),
        block_id="block_multi_visible_offset",
        start_utf16_offset=visible_after_me,
        end_utf16_offset=visible_after_me,
        replacement="？",
        child_id="draft_punctuation_second_ref_insert",
    )
    inserted_block = next(
        block for block in inserted.blocks if block.block_id == "block_multi_visible_offset"
    )
    assert isinstance(inserted_block, SourceExcerptBlock)
    assert inserted_block.display_text == "你😀好！\n没？有细分"
    assert inserted_block.refs == multi_ref.refs

    inserted_child = create_content_draft_editor_child(
        root,
        parent=snapshot.candidate,
        blocks=inserted.blocks,
        expected_revision=snapshot.workflow.project_revision,
    )
    inserted_snapshot = load_draft_editor_snapshot(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=inserted_child.content_draft.content_draft_id,
    )
    inserted_paragraph = _paragraph(inserted_snapshot, kind="source_excerpt", contains="没？有细分")

    replaced = prepare_draft_punctuation(
        inserted_snapshot,
        paragraph_id=str(inserted_paragraph["paragraph_id"]),
        block_id="block_multi_visible_offset",
        start_utf16_offset=visible_after_me,
        end_utf16_offset=visible_after_me + 1,
        replacement="！",
        child_id="draft_punctuation_second_ref_replace",
    )
    replaced_block = next(
        block for block in replaced.blocks if block.block_id == "block_multi_visible_offset"
    )
    assert isinstance(replaced_block, SourceExcerptBlock)
    assert replaced_block.display_text == "你😀好！\n没！有细分"
    assert replaced_block.refs == multi_ref.refs

    replaced_child = create_content_draft_editor_child(
        root,
        parent=inserted_snapshot.candidate,
        blocks=replaced.blocks,
        expected_revision=inserted_snapshot.workflow.project_revision,
    )
    replaced_snapshot = load_draft_editor_snapshot(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=replaced_child.content_draft.content_draft_id,
    )
    replaced_paragraph = _paragraph(replaced_snapshot, kind="source_excerpt", contains="没！有细分")

    deleted = prepare_draft_punctuation(
        replaced_snapshot,
        paragraph_id=str(replaced_paragraph["paragraph_id"]),
        block_id="block_multi_visible_offset",
        start_utf16_offset=visible_after_me,
        end_utf16_offset=visible_after_me + 1,
        replacement="",
        child_id="draft_punctuation_second_ref_delete",
    )
    deleted_block = next(
        block for block in deleted.blocks if block.block_id == "block_multi_visible_offset"
    )
    assert isinstance(deleted_block, SourceExcerptBlock)
    assert deleted_block.display_text == "你😀好！\n没有细分"
    assert deleted_block.refs == multi_ref.refs

    proposal_child = create_content_draft_editor_child(
        root,
        parent=base.candidate,
        blocks=inserted.blocks,
        expected_revision=base.workflow.project_revision,
    )
    confirmed = confirm_content_draft(
        root,
        proposal_child.content_draft.content_draft_id,
        expected_revision=base.workflow.project_revision,
    )
    proposed = propose_content_draft(
        root,
        confirmed.content_draft.content_draft_id,
        expected_revision=confirmed.project_revision,
    )
    assert [clip.display_text for clip in proposed.proposal.clips] == [
        "你😀好！",
        "没？有细分",
    ]
    assert [
        (clip.segment_id, clip.source_in_ticks, clip.source_out_ticks)
        for clip in proposed.proposal.clips
    ] == [
        ("seg_a_fine", 0, 60_000),
        ("seg_a_coarse", 400_000, 500_000),
    ]


def test_move_delete_display_selection_preserves_unselected_punctuation_ownership(
    tmp_path: Path,
) -> None:
    snapshot = _different_person_sectioned_snapshot(tmp_path)
    root = Path(snapshot.workflow.project_path)
    seeded = create_content_draft_editor_child(
        root,
        parent=snapshot.candidate,
        blocks=tuple(
            replace(block, display_text="另一……")
            if isinstance(block, SourceExcerptBlock)
            and block.block_id == "person_other_paragraph_left"
            else replace(block, display_text="人物！")
            if isinstance(block, SourceExcerptBlock)
            and block.block_id == "person_other_paragraph_right"
            else block
            for block in snapshot.candidate.blocks
        ),
        expected_revision=snapshot.workflow.project_revision,
    )
    snapshot = load_draft_editor_snapshot(
        root,
        source_bindings=[
            {"source_id": "src_a", "transcript_version_id": "tr_a"},
            {"source_id": "src_b", "transcript_version_id": "tr_b"},
        ],
        content_draft_id=seeded.content_draft.content_draft_id,
    )
    paragraph = _paragraph(snapshot, kind="source_excerpt", contains="另一")
    full_display_selection = resolve_draft_editor_selection(
        snapshot,
        surface="draft",
        anchor={"paragraph_id": paragraph["paragraph_id"], "offset": 0},
        focus={"paragraph_id": paragraph["paragraph_id"], "offset": 4},
    )
    deleted = prepare_draft_candidate(
        snapshot,
        operation="delete",
        selection=full_display_selection,
        caret=None,
        accept_degraded=False,
        child_id="draft_delete_display_punctuation",
    )
    remaining = next(
        block
        for block in deleted.blocks
        if isinstance(block, SourceExcerptBlock)
        and block.canonical_text == "人物"
    )
    assert remaining.display_text == "人物！"
    canonical_only_selection = resolve_draft_editor_selection(
        snapshot,
        surface="draft",
        anchor={"paragraph_id": paragraph["paragraph_id"], "offset": 0},
        focus={"paragraph_id": paragraph["paragraph_id"], "offset": 2},
    )
    deleted_partial = prepare_draft_candidate(
        snapshot,
        operation="delete",
        selection=canonical_only_selection,
        caret=None,
        accept_degraded=False,
        child_id="draft_delete_display_prefix_owner",
    )
    remaining = next(
        block
        for block in deleted_partial.blocks
        if isinstance(block, SourceExcerptBlock)
        and block.canonical_text == "人物"
    )
    assert remaining.display_text == "……人物！"

    move_caret = resolve_draft_editor_caret(
        snapshot,
        paragraph_id=str(paragraph["paragraph_id"]),
        offset=len(str(paragraph["text"])),
    )
    moved_partial = prepare_draft_candidate(
        snapshot,
        operation="move",
        selection=canonical_only_selection,
        caret=move_caret,
        accept_degraded=False,
        child_id="draft_move_display_prefix_owner",
    )
    moved_sources = [
        block for block in moved_partial.blocks if isinstance(block, SourceExcerptBlock)
    ]
    moved_right = next(block for block in moved_sources if block.canonical_text == "人物")
    moved_left = next(block for block in moved_sources if block.canonical_text == "另一")
    assert moved_right.display_text == "……人物！"
    assert moved_left.display_text is None


def test_schema1_punctuation_prepare_projects_one_immutable_schema2_child(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    root = Path(fixture["root"])
    base = load_draft_editor_snapshot(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=str(fixture["draft_id"]),
    )
    legacy = replace(
        base.candidate,
        content_draft_id="draft_legacy_punctuation",
        parent_draft_id=None,
        schema_version=1,
    )
    legacy_path = root / "content-drafts" / f"{legacy.content_draft_id}.json"
    write_new_json(legacy_path, legacy.to_dict())
    before = legacy_path.read_bytes()
    snapshot = load_draft_editor_snapshot(
        root,
        source_bindings=fixture["bindings"],  # type: ignore[arg-type]
        content_draft_id=legacy.content_draft_id,
    )
    paragraph = _paragraph(snapshot, kind="source_excerpt", contains="你😀好")
    child = prepare_draft_punctuation(
        snapshot,
        paragraph_id=str(paragraph["paragraph_id"]),
        block_id="block_a_fine",
        start_utf16_offset=3,
        end_utf16_offset=3,
        replacement="！",
        child_id="draft_schema2_punctuation_child",
    )
    assert child.schema_version == 2
    assert child.parent_draft_id == legacy.content_draft_id
    assert legacy_path.read_bytes() == before
    changed = next(block for block in child.blocks if block.block_id == "block_a_fine")
    assert isinstance(changed, SourceExcerptBlock)
    assert changed.display_text == "你😀！好"
