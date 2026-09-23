"""Run a disposable W4-D2 Chrome acceptance project with synthetic media only."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path

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
)
from roughcut.application.people import create_person
from roughcut.application.projects import create_project
from roughcut.application.proxies import create_proxy
from roughcut.application.sources import fingerprint_file
from roughcut.domain.people import Person, SpeakerMap
from roughcut.domain.project import ImportMode, MediaProbe, SourceAsset
from roughcut.domain.transcript import (
    FineUnit,
    TimedTranscript,
    TranscriptProvenance,
    TranscriptSegment,
)
from roughcut.review.server import start_review_server

TICKS_PER_SECOND = 120_000
MEDIA_DURATION_TICKS = 12 * TICKS_PER_SECOND


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--single", action="store_true")
    parser.add_argument("--compound", action="store_true")
    arguments = parser.parse_args()
    temporary_root = Path(tempfile.mkdtemp(prefix="roughcut-w4c-chrome-"))
    review = None
    try:
        project_path, bindings, confirmed_draft_id = _prepare_fixture(
            temporary_root,
            single=arguments.single,
            compound=arguments.compound,
        )
        review = start_review_server(
            project_path,
            source_bindings=bindings,
            content_draft_id=confirmed_draft_id,
        )
        snapshot = ProjectStore(project_path).load()
        print(
            json.dumps(
                {
                    "status": "ready",
                    "url": review.url,
                    "project_path": str(project_path),
                    "project_revision": snapshot.revision,
                    "content_draft_id": confirmed_draft_id,
                    "source_bindings": bindings,
                    "expected_playback": {"src_a": "original"}
                    if arguments.single else {"src_a": "original", "src_b": "proxy"},
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        for command in iter(input, "stop"):
            if command == "status":
                current = ProjectStore(project_path).load()
                print(
                    json.dumps(
                        {
                            "status": "running",
                            "project_revision": current.revision,
                            "active_edit_version_id": current.active_edit_version_id,
                            "active_content_draft_id": current.active_content_draft_id,
                        }
                    ),
                    flush=True,
                )
            elif command == "mutate":
                current = ProjectStore(project_path).load()
                mutation = create_person(
                    project_path,
                    name="外部变更人物",
                    role="验收",
                    note="用于 stale 页面锁定测试",
                    expected_revision=current.revision,
                )
                print(json.dumps(mutation.to_dict(), ensure_ascii=False), flush=True)
            elif command:
                print(json.dumps({"status": "unknown_command"}), flush=True)
    finally:
        if review is not None:
            review.close()
        shutil.rmtree(temporary_root, ignore_errors=True)
        print(json.dumps({"status": "cleaned"}), flush=True)


def _prepare_fixture(
    root: Path,
    *,
    single: bool = False,
    compound: bool = False,
    confirm: bool = True,
    display_title: str | None = None,
    with_sections: bool = False,
    tail_count: int = 110,
    project_name: str = "W4-D2 合成浏览器验收",
) -> tuple[Path, list[dict[str, object]], str]:
    media_root = root / "synthetic-media"
    media_root.mkdir()
    media_a = media_root / "fixture-a.mp4"
    media_b = media_root / "fixture-b.mp4"
    _create_media(media_a, color="0x295f8a", frequency=440)
    _create_media(media_b, color="0x8a4f29", frequency=660)

    project_path = root / "project"
    project = create_project(project_path, project_name)
    source_a = _source("src_a", "素材 A · 原片", media_a, ("role:main", "content:talk"))
    source_b = _source("src_b", "素材 B · 按需代理", media_b, ("role:supplement", "content:interaction"))
    transcript_a = _transcript(
        "src_a",
        "tr_a",
        "A",
        tail_count=tail_count,
        compound=compound,
    )
    transcript_b = _transcript(
        "src_b",
        "tr_b",
        "B",
        tail_count=tail_count,
        compound=compound,
    )
    for transcript in ((transcript_a,) if single else (transcript_a, transcript_b)):
        write_new_json(
            project_path
            / "transcripts"
            / transcript.source_id
            / f"{transcript.transcript_version_id}.json",
            transcript.to_dict(),
        )
    person_a = Person("person_a", "人物甲", "主持人", "")
    person_b = Person("person_b", "人物乙", "老师", "")
    prepared = replace(
        project,
        revision=1,
        sources=(source_a,) if single else (source_a, source_b),
        active_transcript_versions={"src_a": "tr_a"}
        if single else {"src_a": "tr_a", "src_b": "tr_b"},
        persons=(person_a,) if single else (person_a, person_b),
        speaker_maps=(SpeakerMap("src_a", "tr_a", "spk_0", "person_a", True),)
        if single else (
            SpeakerMap("src_a", "tr_a", "spk_0", "person_a", True),
            SpeakerMap(
                "src_b",
                "tr_b",
                "spk_0",
                "person_a" if compound else "person_b",
                True,
            ),
            *(
                (SpeakerMap("src_a", "tr_a", "spk_1", "person_b", True),)
                if compound
                else ()
            ),
        ),
    )
    ProjectStore(project_path).save(prepared, expected_revision=0)
    brief = create_edit_brief(
        project_path,
        theme="合成单素材浏览器验收" if single else "合成 A→B→A 浏览器验收",
        target_duration_ticks=(2 if single else 6) * TICKS_PER_SECOND,
        focus=["重点：重复文本与人物交接", "旁白：不需要"],
        allow_reorder=True,
        expected_revision=1,
    )
    bindings: list[dict[str, object]] = [
        {"source_id": "src_a", "transcript_version_id": "tr_a"}
    ]
    if not single:
        bindings.append({"source_id": "src_b", "transcript_version_id": "tr_b"})
    context = read_agent_context(
        project_path,
        source_id="src_a",
        transcript_version_id="tr_a",
        brief_id=brief.brief.brief_id,
        expected_revision=brief.project_revision,
        offset=0,
        limit=1,
    ) if single else read_multi_source_agent_context(
        project_path,
        source_bindings=bindings,
        brief_id=brief.brief.brief_id,
        expected_revision=brief.project_revision,
        offset=0,
        limit=1,
    )
    if compound:
        blocks = _compound_blocks(with_sections=with_sections)
    else:
        blocks = [_block(
            "block_a_open",
            "src_a",
            "tr_a",
            "seg_a_open",
            0,
            240_000,
            "重复中文😀片段。重复中文。",
            section_title="开场" if with_sections else None,
        )]
    if not single and not compound:
        blocks.extend([
            _block(
                "block_b_middle",
                "src_b",
                "tr_b",
                "seg_b_middle",
                0,
                240_000,
                "人物乙介绍课程活动。",
                section_title="主体" if with_sections else None,
            ),
            _block(
                "block_a_return",
                "src_a",
                "tr_a",
                "seg_a_return",
                240_000,
                480_000,
                "重复中文😀片段。重复中文。",
                section_title="结尾" if with_sections else None,
            ),
        ])
    candidate = create_content_draft(
        project_path,
        parent_draft_id=None,
        display_title=display_title,
        source_bindings=bindings,
        brief_id=brief.brief.brief_id,
        context_hash=context.context_hash,
        blocks=blocks,
        expected_revision=brief.project_revision,
    )
    if not confirm:
        return project_path, bindings, candidate.content_draft.content_draft_id
    confirmed = confirm_content_draft(
        project_path,
        candidate.content_draft.content_draft_id,
        expected_revision=brief.project_revision,
    )
    if not single:
        create_proxy(
            project_path,
            source_id="src_b",
            expected_revision=confirmed.project_revision,
        )
    return project_path, bindings, confirmed.content_draft.content_draft_id


def _create_media(path: Path, *, color: str, frequency: int) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=320x180:r=25:d=12",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:sample_rate=48000:duration=12",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "25",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            "-shortest",
            str(path),
        ],
        check=True,
    )


def _source(
    source_id: str,
    display_name: str,
    path: Path,
    tags: tuple[str, ...],
) -> SourceAsset:
    return SourceAsset(
        source_id=source_id,
        kind="video",
        display_name=display_name,
        import_mode=ImportMode.LINKED,
        locator={"absolute_path": str(path.resolve())},
        fingerprint=fingerprint_file(path),
        probe=MediaProbe(
            duration_ticks=MEDIA_DURATION_TICKS,
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
        tags=tags,
        note="W4-C 短合成 fixture",
    )


def _transcript(
    source_id: str,
    transcript_id: str,
    label: str,
    *,
    tail_count: int = 110,
    compound: bool = False,
) -> TimedTranscript:
    if compound:
        return _compound_transcript(
            source_id,
            transcript_id,
            label,
            tail_count=tail_count,
        )
    repeated = "重复中文😀片段。重复中文。"
    main_segments = (
        _segment(
            "seg_a_open" if source_id == "src_a" else "seg_b_middle",
            0,
            240_000,
            repeated if source_id == "src_a" else "人物乙介绍课程活动。",
            "spk_0",
            _fine_units(repeated, 0, 240_000) if source_id == "src_a" else (),
        ),
        _segment(
            f"seg_{source_id[-1]}_{'return' if source_id == 'src_a' else 'follow'}",
            240_000,
            480_000,
            repeated if source_id == "src_a" else "学生参与互动体验。",
            "spk_1",
            (),
        ),
    )
    tail_segments: list[TranscriptSegment] = []
    start = 480_000
    step = (MEDIA_DURATION_TICKS - start) // tail_count
    for index in range(tail_count):
        tail_segments.append(
            _segment(
                f"seg_{source_id[-1]}_tail_{index:03d}",
                start + index * step,
                start + (index + 1) * step,
                f"{label} 长文稿第 {index + 1:03d} 段。",
                f"spk_{index % 2}",
                (),
            )
        )
    return TimedTranscript(
        schema_version=1,
        transcript_version_id=transcript_id,
        source_id=source_id,
        parent_version_id=None,
        provenance=TranscriptProvenance(
            backend="fixture",
            package_version="1",
            models={},
            parameters={},
            raw_result_path=f"raw-asr/{source_id}/fixture.json",
            started_at="fixture",
            completed_at="fixture",
            exit_status=0,
        ),
        language="zh-CN",
        segments=(*main_segments, *tail_segments),
    )


def _compound_transcript(
    source_id: str,
    transcript_id: str,
    label: str,
    *,
    tail_count: int,
) -> TimedTranscript:
    main_segments = tuple(
        _segment(
            f"seg_{source_id[-1]}_{index}",
            index * 60_000,
            index * 60_000 + 40_000,
            f"{source_id[-1].upper()}{index}",
            "spk_0",
            _fine_units(
                f"{source_id[-1].upper()}{index}",
                index * 60_000,
                index * 60_000 + 40_000,
            ),
        )
        for index in range(6)
    )
    if source_id == "src_a":
        main_segments = (
            *main_segments,
            _segment(
                "seg_a_tail",
                480_000,
                540_000,
                "收尾段",
                "spk_1",
                _fine_units("收尾段", 480_000, 540_000),
            ),
        )
    tail_segments: list[TranscriptSegment] = []
    start = 540_000
    step = (MEDIA_DURATION_TICKS - start) // tail_count
    for index in range(tail_count):
        tail_segments.append(
            _segment(
                f"seg_{source_id[-1]}_long_{index:03d}",
                start + index * step,
                start + (index + 1) * step,
                f"{label} 长文稿第 {index + 1:03d} 段。",
                f"spk_{2 + index % 2}",
                (),
            )
        )
    return TimedTranscript(
        schema_version=1,
        transcript_version_id=transcript_id,
        source_id=source_id,
        parent_version_id=None,
        provenance=TranscriptProvenance(
            backend="fixture",
            package_version="1",
            models={},
            parameters={},
            raw_result_path=f"raw-asr/{source_id}/fixture.json",
            started_at="fixture",
            completed_at="fixture",
            exit_status=0,
        ),
        language="zh-CN",
        segments=(*main_segments, *tail_segments),
    )


def _compound_blocks(*, with_sections: bool) -> list[dict[str, object]]:
    blocks: list[dict[str, object]] = [
        {
            "block_id": f"block_{source_id}",
            "kind": "source_excerpt",
            "refs": [
                {
                    "source_id": source_id,
                    "transcript_version_id": transcript_id,
                    "segment_id": f"seg_{source_id[-1]}_{index}",
                    "start_ticks": index * 60_000,
                    "end_ticks": index * 60_000 + 40_000,
                }
                for index in range(6)
            ],
            "canonical_text": "\n".join(
                f"{source_id[-1].upper()}{index}" for index in range(6)
            ),
            "section_title": (
                "主体"
                if with_sections and source_id == "src_b"
                else None
            ),
        }
        for source_id, transcript_id in (("src_b", "tr_b"), ("src_a", "tr_a"))
    ]
    blocks.append(
        _block(
            "block_tail",
            "src_a",
            "tr_a",
            "seg_a_tail",
            480_000,
            540_000,
            "收尾段",
            section_title="结尾" if with_sections else None,
        )
    )
    return blocks


def _segment(
    segment_id: str,
    start_ticks: int,
    end_ticks: int,
    text: str,
    speaker: str,
    fine_units: tuple[FineUnit, ...],
) -> TranscriptSegment:
    return TranscriptSegment(
        segment_id=segment_id,
        start_ticks=start_ticks,
        end_ticks=end_ticks,
        original_text=text,
        corrected_text=None,
        local_speaker_id=speaker,
        person_id=None,
        confidence=None,
        fine_units=fine_units,
        editorial_mark="unmarked",
    )


def _fine_units(text: str, start_ticks: int, end_ticks: int) -> tuple[FineUnit, ...]:
    width = end_ticks - start_ticks
    return tuple(
        FineUnit(
            "character",
            character,
            start_ticks + (width * index) // len(text),
            start_ticks + (width * (index + 1)) // len(text),
            None,
        )
        for index, character in enumerate(text)
    )


def _block(
    block_id: str,
    source_id: str,
    transcript_version_id: str,
    segment_id: str,
    start_ticks: int,
    end_ticks: int,
    canonical_text: str,
    *,
    section_title: str | None = None,
) -> dict[str, object]:
    return {
        "block_id": block_id,
        "kind": "source_excerpt",
        "refs": [
            {
                "source_id": source_id,
                "transcript_version_id": transcript_version_id,
                "segment_id": segment_id,
                "start_ticks": start_ticks,
                "end_ticks": end_ticks,
            }
        ],
        "canonical_text": canonical_text,
        "section_title": section_title,
    }


if __name__ == "__main__":
    main()
