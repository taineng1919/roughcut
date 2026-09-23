"""M3.4 A shared test fixture, copied from the preserved acceptance runner.

Source: commit `967bd322` (P1 false-stale acceptance runner) at
`/private/tmp/m3-4-a-false-stale-acceptance-QF3Wan/server_runner.py` —
`source()` / `segment()` / `transcript()` / `build_fixture()` are copied
verbatim so the synthetic project matches the preserved acceptance site.
The fixture covers: 12 Chinese paragraphs, 3 non-empty sections, 1 empty
section, a repeated phrase, emoji, editorial punctuation in canonical text,
and one narration block.  Later M3.4 A levels reuse this same helper.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from roughcut.adapters.artifact_store import write_new_json
from roughcut.adapters.project_store import ProjectStore
from roughcut.application.agent_context import calculate_agent_context_hash
from roughcut.application.projects import create_project
from roughcut.application.sources import fingerprint_file
from roughcut.application.workflows import workflow_action, workflow_start
from roughcut.domain.bindings import SourceTranscriptBinding
from roughcut.domain.brief import EditBrief
from roughcut.domain.people import Person, SpeakerMap
from roughcut.domain.project import ImportMode, MediaProbe, SourceAsset
from roughcut.domain.transcript import (
    FineUnit,
    TimedTranscript,
    TranscriptProvenance,
    TranscriptSegment,
)

TICKS_PER_SECOND = 120_000


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def source(source_id: str, media: Path) -> SourceAsset:
    return SourceAsset(
        source_id=source_id,
        kind="video",
        display_name="左侧初稿素材" if source_id == "src_a" else "右侧原稿素材",
        import_mode=ImportMode.LINKED,
        locator={"absolute_path": str(media.resolve())},
        fingerprint=fingerprint_file(media),
        probe=MediaProbe(
            duration_ticks=2_400_000,
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
        tags=(f"fixture:{source_id}",),
        note="P1 synthetic bytes-only media",
    )


def segment(segment_id: str, start: int, text: str, speaker: str | None) -> TranscriptSegment:
    width = 80_000
    units = tuple(
        FineUnit(
            "character",
            character,
            start + (width * index) // len(text),
            start + (width * (index + 1)) // len(text),
            None,
        )
        for index, character in enumerate(text)
    )
    return TranscriptSegment(
        segment_id=segment_id,
        start_ticks=start,
        end_ticks=start + width,
        original_text=text,
        corrected_text=None,
        local_speaker_id=speaker,
        person_id=None,
        confidence=None,
        fine_units=units,
        editorial_mark="unmarked",
    )


def transcript(source_id: str, transcript_id: str, texts: tuple[str, ...]) -> TimedTranscript:
    return TimedTranscript(
        schema_version=1,
        transcript_version_id=transcript_id,
        source_id=source_id,
        parent_version_id=None,
        provenance=TranscriptProvenance(
            "fixture",
            "1",
            {},
            {},
            f"raw-asr/{source_id}/synthetic.json",
            "fixture",
            "fixture",
            0,
        ),
        language="zh-CN",
        segments=tuple(
            segment(f"seg_{source_id}_{index:02d}", index * 100_000, text, None)
            for index, text in enumerate(texts)
        ),
    )


def build_fixture(root: Path) -> tuple[Path, list[dict[str, str]], str]:
    media_root = root / "synthetic-media"
    media_root.mkdir(parents=True)
    media_a = media_root / "synthetic-a.bytes"
    media_b = media_root / "synthetic-b.bytes"
    media_a.write_bytes(b"synthetic media A; no decoder is needed\n")
    media_b.write_bytes(b"synthetic media B; no decoder is needed\n")

    project_path = root / "project"
    project = create_project(project_path, "P1 假 stale 合成诊断")
    source_a = source("src_a", media_a)
    source_b = source("src_b", media_b)
    source_texts = (
        "主持人说：‘请大家重点内容然后再讨论下一步安排。’",
        "重复原话用于验证既有 occurrence，保留人工标点。",
        "这一段完整中文正文用于提供稳定的起始上下文。",
        "第四段正文包含主持人的补充说明和一个😀表情。",
        "第五段正文用于跨章节目标前的合法落点。",
        "第六段正文继续保留原始说法与人工标点。",
        "第七段正文是另一个章节中的普通同期声。",
        "第八段正文保持右侧原稿对应关系不变。",
        "主持人说：‘请大家重点内容然后再讨论下一步安排。’",
        "第十段正文用于第二次自动移动的来源。",
        "第十一段正文说明后续安排并保持段落边界。",
        "第十二段正文作为最后一个完整中文段落。",
    )
    source_b_texts = (
        "右侧原稿保持不变，这里也保留重点内容然后再讨论下一步安排。",
        "主持人说：‘请大家检查右侧原稿中的重复短语。’",
    )
    transcript_a = transcript("src_a", "tr_a", source_texts)
    transcript_b = transcript("src_b", "tr_b", source_b_texts)
    write_new_json(
        project_path / "transcripts" / "src_a" / "tr_a.json",
        transcript_a.to_dict(),
    )
    write_new_json(
        project_path / "transcripts" / "src_b" / "tr_b.json",
        transcript_b.to_dict(),
    )
    prepared = replace(
        project,
        revision=1,
        sources=(source_a, source_b),
        active_transcript_versions={"src_a": "tr_a", "src_b": "tr_b"},
        persons=(Person("person_a", "主持人", "主持", ""),),
        speaker_maps=(
            SpeakerMap("src_a", "tr_a", "spk_0", "person_a", True),
            SpeakerMap("src_b", "tr_b", "spk_0", "person_a", True),
        ),
    )
    ProjectStore(project_path).save(prepared, expected_revision=0)
    bindings = [
        {"source_id": "src_a", "transcript_version_id": "tr_a"},
        {"source_id": "src_b", "transcript_version_id": "tr_b"},
    ]
    started = workflow_start(project_path, "wfr_p1", ["src_a", "src_b"])
    scoped = workflow_action(
        project_path,
        "wfr_p1",
        "act_scope",
        "approve_scope",
        {
            "schema_version": 1,
            "confirmation_basis": started.status["confirmation_bases"]["scope"]["basis"],
            "source_authorizations": [
                {
                    "source_id": source_id,
                    "transcribe": False,
                    "speaker_diarization": False,
                }
                for source_id in ("src_a", "src_b")
            ],
        },
    )
    workflow_action(
        project_path,
        "wfr_p1",
        "act_brief",
        "confirm_brief",
        {
            "schema_version": 1,
            "confirmation_basis": scoped.status["confirmation_bases"]["brief"]["basis"],
            "theme": "P1 结果高亮自动诊断",
            "target_duration_ticks": 1_200_000,
            "focus": ["重复原话", "章节边界", "精确 refs"],
            "allow_reorder": True,
            "speaker_resolution_waivers": [],
        },
    )
    outline = {
        "schema_version": 1,
        "title": "P1 结果高亮自动诊断",
        "opening": "记录",
        "sections": [
            {"section_id": "section_1", "title": "记录", "summary": "记录", "target_duration_ticks": 400_000},
            {"section_id": "section_2", "title": "讨论", "summary": "讨论", "target_duration_ticks": 400_000},
            {"section_id": "section_3", "title": "安排", "summary": "安排", "target_duration_ticks": 400_000},
            {"section_id": "section_4", "title": "空章节", "summary": "空章节", "target_duration_ticks": 1},
        ],
        "ending": "安排",
        "required_content_coverage": [],
        "narration_status": "none",
    }
    submitted_outline = workflow_action(
        project_path,
        "wfr_p1",
        "act_outline",
        "submit_outline",
        outline,
    )
    outline_ref = submitted_outline.status["presented_subjects"]["outline_ref"]
    draft_review = workflow_action(
        project_path,
        "wfr_p1",
        "act_approve_outline",
        "approve_outline",
        {
            "schema_version": 1,
            "outline_ref": {
                key: outline_ref[key]
                for key in ("artifact_id", "schema_version", "content_hash")
            },
        },
    )
    brief_ref = draft_review.workflow_run.artifact_refs["brief"]
    assert brief_ref is not None
    brief = EditBrief.from_dict(
        json.loads(
            (project_path / "briefs" / f"{brief_ref.artifact_id}.json").read_text(
                encoding="utf-8"
            )
        )
    )
    current_project = ProjectStore(project_path).load()
    context_hash = calculate_agent_context_hash(
        project_path,
        project=current_project,
        bindings=tuple(SourceTranscriptBinding(**binding) for binding in bindings),
        brief=brief,
    )
    blocks: list[dict[str, object]] = [
        {"block_id": "section_one", "kind": "section_title", "title": "第一章 记录"},
    ]
    for index, text in enumerate(source_texts[:4]):
        blocks.append({
            "block_id": f"block_{index:02d}",
            "kind": "source_excerpt",
            "refs": [{
                "source_id": "src_a",
                "transcript_version_id": "tr_a",
                "segment_id": f"seg_src_a_{index:02d}",
                "start_ticks": index * 100_000,
                "end_ticks": index * 100_000 + 80_000,
            }],
            "canonical_text": text,
        })
    blocks.extend([
        {"block_id": "section_two", "kind": "section_title", "title": "第二章 讨论"},
    ])
    for index, text in enumerate(source_texts[4:8], start=4):
        blocks.append({
            "block_id": f"block_{index:02d}",
            "kind": "source_excerpt",
            "refs": [{
                "source_id": "src_a",
                "transcript_version_id": "tr_a",
                "segment_id": f"seg_src_a_{index:02d}",
                "start_ticks": index * 100_000,
                "end_ticks": index * 100_000 + 80_000,
            }],
            "canonical_text": text,
        })
    blocks.append({
        "block_id": "block_narration",
        "kind": "narration",
        "text": "这里是一段待录音解说。",
        "status": "draft",
        "recorded_refs": [],
    })
    blocks.append({"block_id": "section_three", "kind": "section_title", "title": "第三章 安排"})
    for index, text in enumerate(source_texts[8:], start=8):
        blocks.append({
            "block_id": f"block_{index:02d}",
            "kind": "source_excerpt",
            "refs": [{
                "source_id": "src_a",
                "transcript_version_id": "tr_a",
                "segment_id": f"seg_src_a_{index:02d}",
                "start_ticks": index * 100_000,
                "end_ticks": index * 100_000 + 80_000,
            }],
            "canonical_text": text,
        })
    blocks.append({"block_id": "section_empty", "kind": "section_title", "title": "空章节"})
    submitted_draft = workflow_action(
        project_path,
        "wfr_p1",
        "act_draft",
        "submit_draft",
        {
            "schema_version": 1,
            "parent_draft_ref": None,
            "display_title": "P1 自动诊断初稿",
            "source_bindings": bindings,
            "brief_ref": brief_ref.to_dict(),
            "context_hash": context_hash,
            "blocks": blocks,
            "scoped_mutable_block_ids": [],
        },
    )
    draft_ref = submitted_draft.workflow_run.artifact_refs["content_draft"]
    assert draft_ref is not None
    return project_path, bindings, draft_ref.artifact_id
