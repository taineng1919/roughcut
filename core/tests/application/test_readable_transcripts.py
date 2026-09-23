from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from roughcut.adapters.project_store import ProjectStore
from roughcut.application.people import update_source_metadata
from roughcut.application.projects import create_project
from roughcut.application.readable_transcripts import (
    export_markdown,
    insert_transcript_selection_at_caret,
    make_exact_sequence_caret,
    move_draft_selection_to_caret,
    read_readable_transcript,
    resolve_continuous_transcript_selection,
    resolve_transcript_caret,
    resolve_transcript_selection,
)
from roughcut.domain.bindings import SourceTranscriptBinding
from roughcut.domain.brief import EditBrief
from roughcut.domain.edit import EditClip, MultiSourceEditDecision, MultiSourceEditProposal
from roughcut.domain.people import Person, SpeakerMap
from roughcut.domain.project import (
    ImportMode,
    MediaProbe,
    ProjectError,
    SourceAsset,
    SourceFingerprint,
)
from roughcut.domain.readable_transcript import ResolvedSelectionRef
from roughcut.domain.transcript import (
    FineUnit,
    TimedTranscript,
    TranscriptProvenance,
    TranscriptSegment,
)


def _probe() -> MediaProbe:
    return MediaProbe(
        duration_ticks=2_000_000,
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
    )


def _provenance(source_id: str) -> TranscriptProvenance:
    return TranscriptProvenance(
        backend="fixture",
        package_version="fixture",
        models={"secret": "/private/model"},
        parameters={},
        raw_result_path=f"raw-asr/{source_id}/fixture.json",
        started_at="fixture",
        completed_at="fixture",
        exit_status=0,
    )


def _segment(
    segment_id: str,
    start: int,
    end: int,
    text: str,
    speaker: str | None,
    *,
    corrected_text: str | None = None,
    fine_units: tuple[FineUnit, ...] = (),
) -> TranscriptSegment:
    return TranscriptSegment(
        segment_id=segment_id,
        start_ticks=start,
        end_ticks=end,
        original_text=text,
        corrected_text=corrected_text,
        local_speaker_id=speaker,
        person_id=None,
        confidence=None,
        fine_units=fine_units,
        editorial_mark="unmarked",
    )


def _fixture(tmp_path: Path) -> dict[str, object]:
    root = tmp_path / "可读文稿 project"
    project = create_project(root, "可读文稿")
    source_a = SourceAsset(
        source_id="src_a",
        kind="audio",
        display_name="开场 采访",
        import_mode=ImportMode.LINKED,
        locator={"absolute_path": "/private/真实素材 A.wav"},
        fingerprint=SourceFingerprint(10, 11, "secret-a"),
        probe=_probe(),
        tags=("role:main",),
        note="主线",
    )
    source_b = SourceAsset(
        source_id="src_b",
        kind="audio",
        display_name="校园 参观",
        import_mode=ImportMode.LINKED,
        locator={"absolute_path": "/private/真实素材 B.wav"},
        fingerprint=SourceFingerprint(20, 22, "secret-b"),
        probe=_probe(),
        tags=("role:supplement",),
        note="补充",
    )
    fine = (
        FineUnit("character", "你", 0, 40_000, None),
        FineUnit("character", "好", 40_000, 80_000, None),
        FineUnit("character", "，", 80_000, 120_000, None),
    )
    transcript_a = TimedTranscript(
        schema_version=1,
        transcript_version_id="tr_a",
        source_id="src_a",
        parent_version_id=None,
        provenance=_provenance("src_a"),
        language="zh-CN",
        segments=(
            _segment("seg_a1", 0, 120_000, "你好，", "spk_0", fine_units=fine),
            _segment("seg_a2", 140_000, 240_000, "欢迎。", "spk_2"),
            _segment(
                "seg_a3",
                250_000,
                360_000,
                "我是老师。",
                "spk_1",
                corrected_text="我是赵老师。",
                fine_units=(FineUnit("token", "我是老师。", 250_000, 360_000, None),),
            ),
            _segment("seg_a4", 610_001, 720_000, "间隔之后。", "spk_1"),
            _segment("seg_a5", 730_000, 840_000, "重复重复。", "spk_9"),
            _segment("seg_a6", 850_000, 960_000, "重复重复。", "spk_9"),
        ),
    )
    transcript_b = TimedTranscript(
        schema_version=1,
        transcript_version_id="tr_b",
        source_id="src_b",
        parent_version_id=None,
        provenance=_provenance("src_b"),
        language="zh-CN",
        segments=(
            _segment("seg_b1", 0, 120_000, "另一素材老师。", "spk_0"),
            _segment("seg_b2", 130_000, 240_000, "主持人回来。", "spk_1"),
        ),
    )
    for transcript in (transcript_a, transcript_b):
        path = (
            root / "transcripts" / transcript.source_id / f"{transcript.transcript_version_id}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(transcript.to_dict(), ensure_ascii=False), encoding="utf-8")
    host = Person("person_host", "小严", "主持人", "")
    teacher = Person("person_teacher", "小赵", "老师", "")
    maps = (
        SpeakerMap("src_a", "tr_a", "spk_0", host.person_id, True),
        SpeakerMap("src_a", "tr_a", "spk_2", host.person_id, True),
        SpeakerMap("src_a", "tr_a", "spk_1", teacher.person_id, True),
        SpeakerMap("src_b", "tr_b", "spk_0", teacher.person_id, True),
        SpeakerMap("src_b", "tr_b", "spk_1", host.person_id, True),
    )
    prepared = replace(
        project,
        revision=1,
        sources=(source_a, source_b),
        persons=(host, teacher),
        speaker_maps=maps,
        active_transcript_versions={"src_a": "tr_a", "src_b": "tr_b"},
    )
    ProjectStore(root).save(prepared, expected_revision=0)
    bindings = [
        SourceTranscriptBinding("src_a", "tr_a").to_dict(),
        SourceTranscriptBinding("src_b", "tr_b").to_dict(),
    ]
    return {"root": root, "revision": 1, "bindings": bindings}


def _proposal(root: Path) -> str:
    brief = EditBrief("brief_fixture", "主题", 360_000, ("重点",), True)
    proposal = MultiSourceEditProposal(
        proposal_id="proposal_overlay",
        base_project_revision=1,
        base_edit_version_id=None,
        source_bindings=(
            SourceTranscriptBinding("src_a", "tr_a"),
            SourceTranscriptBinding("src_b", "tr_b"),
        ),
        brief_snapshot=brief,
        context_hash="a" * 64,
        clips=(
            EditClip(
                "clip_a1",
                "src_a",
                "tr_a",
                "seg_a1",
                0,
                120_000,
                "采用开场",
                "你好，",
            ),
            EditClip(
                "clip_a2_partial",
                "src_a",
                "tr_a",
                "seg_a2",
                160_000,
                220_000,
                "部分采用",
                "欢迎",
            ),
            EditClip(
                "clip_b1",
                "src_b",
                "tr_b",
                "seg_b1",
                0,
                120_000,
                "采用老师介绍",
                "另一素材老师。",
            ),
        ),
        total_duration_ticks=300_000,
        created_at="fixture",
    )
    path = root / "proposals" / f"{proposal.proposal_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(proposal.to_dict(), ensure_ascii=False), encoding="utf-8")
    return proposal.proposal_id


def test_readable_transcript_groups_by_binding_speaker_gap_and_identity(tmp_path: Path) -> None:
    state = _fixture(tmp_path)

    page = read_readable_transcript(
        Path(state["root"]),
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        offset=0,
        limit=20,
    )

    assert page.view_schema_version == 1
    assert page.algorithm_version == 1
    assert page.total == 6
    assert [paragraph.text for paragraph in page.paragraphs] == [
        "你好，\n欢迎。",
        "我是赵老师。",
        "间隔之后。",
        "重复重复。\n重复重复。",
        "另一素材老师。",
        "主持人回来。",
    ]
    assert [paragraph.person_name for paragraph in page.paragraphs[-2:]] == ["小赵", "小严"]
    assert page.paragraphs[0].person_name == "小严"
    assert page.paragraphs[0].local_speaker_id is None
    assert page.paragraphs[0].local_speaker_ids == ("spk_0", "spk_2")
    assert page.paragraphs[3].person_id is None
    assert [ref.segment_id for paragraph in page.paragraphs for ref in paragraph.refs] == [
        "seg_a1",
        "seg_a2",
        "seg_a3",
        "seg_a4",
        "seg_a5",
        "seg_a6",
        "seg_b1",
        "seg_b2",
    ]
    serialized = json.dumps(page.to_dict(), ensure_ascii=False)
    assert "/private/" not in serialized
    assert "raw-asr" not in serialized
    assert "secret-a" not in serialized
    assert "model" not in serialized


def test_filtering_and_pagination_preserve_paragraph_identity_and_hash(tmp_path: Path) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    whole = read_readable_transcript(
        root,
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        offset=0,
        limit=20,
    )
    filtered = read_readable_transcript(
        root,
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        offset=0,
        limit=1,
        filters={"source_ids": ["src_a"], "person_ids": ["person_host"], "keyword": "欢迎"},
    )
    second_page = read_readable_transcript(
        root,
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        offset=1,
        limit=2,
    )

    assert filtered.view_hash == whole.view_hash == second_page.view_hash
    assert filtered.paragraphs[0].paragraph_id == whole.paragraphs[0].paragraph_id
    assert second_page.paragraphs[0].paragraph_id == whole.paragraphs[1].paragraph_id
    assert filtered.next_cursor is None

    reversed_page = read_readable_transcript(
        root,
        source_bindings=list(reversed(state["bindings"])),  # type: ignore[arg-type]
        expected_revision=1,
        offset=0,
        limit=20,
    )
    assert reversed_page.view_hash != whole.view_hash
    assert reversed_page.paragraphs[0].source_id == "src_b"


def test_overlay_reports_adopted_partial_and_unadopted(tmp_path: Path) -> None:
    state = _fixture(tmp_path)
    proposal_id = _proposal(Path(state["root"]))

    page = read_readable_transcript(
        Path(state["root"]),
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        offset=0,
        limit=20,
        overlay={"basis": "proposal", "artifact_id": proposal_id},
    )

    assert page.paragraphs[0].adoption_status == "partial"
    assert page.paragraphs[1].adoption_status == "unadopted"
    assert page.paragraphs[4].adoption_status == "adopted"
    adopted_only = read_readable_transcript(
        Path(state["root"]),
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        offset=0,
        limit=20,
        overlay={"basis": "proposal", "artifact_id": proposal_id},
        filters={"adoption_statuses": ["partial"]},
    )
    assert [paragraph.paragraph_id for paragraph in adopted_only.paragraphs] == [
        page.paragraphs[0].paragraph_id
    ]

    proposal_data = json.loads(
        (Path(state["root"]) / "proposals" / f"{proposal_id}.json").read_text(encoding="utf-8")
    )
    decision = MultiSourceEditDecision(
        edit_version_id="edit_overlay",
        proposal_snapshot=MultiSourceEditProposal.from_dict(proposal_data),
        project_revision=2,
        created_at="fixture",
    )
    decision_path = Path(state["root"]) / "edits" / "edit_overlay.json"
    decision_path.parent.mkdir(parents=True, exist_ok=True)
    decision_path.write_text(json.dumps(decision.to_dict(), ensure_ascii=False), encoding="utf-8")
    decision_page = read_readable_transcript(
        Path(state["root"]),
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        offset=0,
        limit=20,
        overlay={"basis": "decision", "artifact_id": "edit_overlay"},
    )
    assert [paragraph.adoption_status for paragraph in decision_page.paragraphs] == [
        paragraph.adoption_status for paragraph in page.paragraphs
    ]
    assert decision_page.view_hash != page.view_hash


def test_soft_limits_split_only_at_the_next_segment_boundary(tmp_path: Path) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    transcript_path = root / "transcripts" / "src_a" / "tr_a.json"
    transcript = TimedTranscript.from_dict(json.loads(transcript_path.read_text(encoding="utf-8")))
    long_text = "甲" * 301
    limited = replace(
        transcript,
        segments=(
            _segment("seg_long", 0, 120_000, long_text, "spk_0"),
            _segment("seg_after_limit", 130_000, 240_000, "下一段。", "spk_0"),
            _segment("seg_45_seconds", 300_000, 5_700_000, "长时间段。", "spk_0"),
            _segment("seg_after_time", 5_710_000, 5_820_000, "时间后段。", "spk_0"),
        ),
    )
    transcript_path.write_text(json.dumps(limited.to_dict(), ensure_ascii=False), encoding="utf-8")

    page = read_readable_transcript(
        root,
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        offset=0,
        limit=20,
    )
    source_a = [paragraph for paragraph in page.paragraphs if paragraph.source_id == "src_a"]
    assert [[ref.segment_id for ref in paragraph.refs] for paragraph in source_a] == [
        ["seg_long"],
        ["seg_after_limit", "seg_45_seconds"],
        ["seg_after_time"],
    ]


def test_long_transcript_pagination_covers_every_stable_paragraph(tmp_path: Path) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    transcript_path = root / "transcripts" / "src_a" / "tr_a.json"
    transcript = TimedTranscript.from_dict(json.loads(transcript_path.read_text(encoding="utf-8")))
    long_transcript = replace(
        transcript,
        segments=tuple(
            _segment(
                f"seg_long_{index:03d}",
                index * 2_000,
                index * 2_000 + 1_000,
                f"第{index}段。",
                f"spk_{index % 2 + 20}",
            )
            for index in range(205)
        ),
    )
    transcript_path.write_text(
        json.dumps(long_transcript.to_dict(), ensure_ascii=False), encoding="utf-8"
    )

    first = read_readable_transcript(
        root,
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        offset=0,
        limit=200,
        filters={"source_ids": ["src_a"]},
    )
    second = read_readable_transcript(
        root,
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        offset=200,
        limit=200,
        filters={"source_ids": ["src_a"]},
    )
    assert first.total == second.total == 205
    assert first.next_cursor == 200
    assert second.next_cursor is None
    ids = [paragraph.paragraph_id for paragraph in (*first.paragraphs, *second.paragraphs)]
    assert len(ids) == len(set(ids)) == 205
    assert first.view_hash == second.view_hash


def test_selection_resolves_trusted_substring_and_expands_untrusted_segment(
    tmp_path: Path,
) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    page = read_readable_transcript(
        root,
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        offset=0,
        limit=20,
    )
    first = page.paragraphs[0]
    exact = resolve_transcript_selection(
        root,
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        view_hash=page.view_hash,
        selections=[
            {
                "paragraph_id": first.paragraph_id,
                "start_offset": 1,
                "end_offset": 2,
                "quote": "好",
                "occurrence": 0,
            }
        ],
    )
    assert exact.mode == "exact"
    assert exact.canonical_text == "好"
    assert exact.refs[0].start_ticks == 40_000
    assert exact.refs[0].end_ticks == 80_000

    teacher = page.paragraphs[1]
    expanded = resolve_transcript_selection(
        root,
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        view_hash=page.view_hash,
        selections=[
            {
                "paragraph_id": teacher.paragraph_id,
                "start_offset": 2,
                "end_offset": 4,
                "quote": "赵老",
                "occurrence": 0,
            }
        ],
    )
    assert expanded.mode == "expanded_to_segments"
    assert expanded.canonical_text == "我是赵老师。"
    assert expanded.refs[0].start_ticks == 250_000
    assert expanded.refs[0].end_ticks == 360_000


def test_selection_rejects_ambiguous_repeat_and_stale_hash(tmp_path: Path) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    page = read_readable_transcript(
        root,
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        offset=0,
        limit=20,
    )
    repeated = page.paragraphs[3]
    with pytest.raises(ProjectError, match="occurrence"):
        resolve_transcript_selection(
            root,
            source_bindings=state["bindings"],  # type: ignore[arg-type]
            expected_revision=1,
            view_hash=page.view_hash,
            selections=[
                {
                    "paragraph_id": repeated.paragraph_id,
                    "start_offset": 0,
                    "end_offset": 2,
                    "quote": "重复",
                }
            ],
        )

    occurrence = resolve_transcript_selection(
        root,
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        view_hash=page.view_hash,
        selections=[
            {
                "paragraph_id": repeated.paragraph_id,
                "start_offset": 2,
                "end_offset": 4,
                "quote": "重复",
                "occurrence": 1,
            }
        ],
    )
    assert occurrence.mode == "expanded_to_segments"
    assert occurrence.canonical_text == "重复重复。"

    changed = update_source_metadata(
        root,
        source_id="src_a",
        display_name="新名称",
        tags=["role:main"],
        note="主线",
        expected_revision=1,
    )
    with pytest.raises(ProjectError, match="view hash is stale"):
        resolve_transcript_selection(
            root,
            source_bindings=state["bindings"],  # type: ignore[arg-type]
            expected_revision=changed.state.project_revision,
            view_hash=page.view_hash,
            selections=[{"paragraph_id": page.paragraphs[0].paragraph_id}],
        )


def test_cross_paragraph_selection_expands_whole_paragraphs_in_view_order(tmp_path: Path) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    page = read_readable_transcript(
        root,
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        offset=0,
        limit=20,
    )
    resolution = resolve_transcript_selection(
        root,
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        view_hash=page.view_hash,
        selections=[
            {"paragraph_id": page.paragraphs[2].paragraph_id},
            {"paragraph_id": page.paragraphs[1].paragraph_id},
        ],
    )
    assert [ref.segment_id for ref in resolution.refs] == ["seg_a3", "seg_a4"]
    assert resolution.canonical_text == "我是赵老师。\n间隔之后。"

    with pytest.raises(ProjectError, match="partial cross-paragraph"):
        resolve_transcript_selection(
            root,
            source_bindings=state["bindings"],  # type: ignore[arg-type]
            expected_revision=1,
            view_hash=page.view_hash,
            selections=[
                {
                    "paragraph_id": page.paragraphs[0].paragraph_id,
                    "start_offset": 0,
                    "end_offset": 1,
                    "quote": "你",
                    "occurrence": 0,
                },
                {"paragraph_id": page.paragraphs[1].paragraph_id},
            ],
        )


def _install_punctuation_gap_transcript(root: Path) -> None:
    """Transcript whose display punctuation carries exact fine-unit ticks."""
    transcript = TimedTranscript(
        schema_version=1,
        transcript_version_id="tr_a",
        source_id="src_a",
        parent_version_id=None,
        provenance=_provenance("src_a"),
        language="zh-CN",
        segments=(
            _segment(
                "seg_gap",
                0,
                80_000,
                "主持人说：‘请大家",
                "spk_0",
                fine_units=(
                    FineUnit("character", "主", 0, 3_200, None),
                    FineUnit("character", "持", 3_200, 6_400, None),
                    FineUnit("character", "人", 6_400, 9_600, None),
                    FineUnit("character", "说", 9_600, 12_800, None),
                    FineUnit("character", "：", 12_800, 16_000, None),
                    FineUnit("character", "‘", 16_000, 19_200, None),
                    FineUnit("character", "请", 19_200, 22_400, None),
                    FineUnit("character", "大", 22_400, 25_600, None),
                    FineUnit("character", "家", 25_600, 28_800, None),
                ),
            ),
        ),
    )
    (root / "transcripts" / "src_a" / "tr_a.json").write_text(
        json.dumps(transcript.to_dict(), ensure_ascii=False),
        encoding="utf-8",
    )


def test_w4_punctuation_gap_selection_keeps_exact_characters(
    tmp_path: Path,
) -> None:
    """Selections ending inside a punctuation gap keep the included chars.

    Display punctuation that carries fine-unit ticks must be selectable at
    every code-point boundary (contract 87-88): dragging [0,4) over
    「主持人说」 must keep 说 instead of snapping back to [0,3), and [0,5)
    must include the explicitly selected colon.
    """
    state = _fixture(tmp_path)
    root = Path(state["root"])
    _install_punctuation_gap_transcript(root)
    page = read_readable_transcript(
        root,
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        offset=0,
        limit=20,
    )
    first = page.paragraphs[0]
    assert first.text.startswith("主持人说：‘请大家")

    cases = [
        ((0, 4), "主持人说", 0, 12_800, False),
        ((3, 4), "说", 9_600, 12_800, False),
        ((0, 5), "主持人说：", 0, 16_000, False),
        # explicitly included punctuation with its own ticks follows the
        # selection, including when the endpoint lands on a span boundary
        ((0, 6), "主持人说：‘", 0, 19_200, False),
        ((3, 6), "说", 9_600, 12_800, False),
        # punctuation-only selection falls back to the containing spoken span
        ((4, 6), "说", 9_600, 12_800, True),
        ((4, 7), "：‘请", 12_800, 22_400, False),
        ((0, 3), "主持人", 0, 9_600, False),
    ]
    for (anchor, focus), canonical, start_ticks, end_ticks, adjusted in cases:
        resolved = resolve_continuous_transcript_selection(
            root,
            source_bindings=state["bindings"],  # type: ignore[arg-type]
            expected_revision=1,
            view_hash=page.view_hash,
            anchor={"paragraph_id": first.paragraph_id, "offset": anchor},
            focus={"paragraph_id": first.paragraph_id, "offset": focus},
        )
        assert resolved.canonical_text == canonical, (
            f"[{anchor},{focus}) expected {canonical!r} got {resolved.canonical_text!r}"
        )
        assert resolved.adjusted is adjusted, (
            f"[{anchor},{focus}) expected adjusted={adjusted} got {resolved.adjusted}"
        )
        assert resolved.refs[0].start_ticks == start_ticks
        assert resolved.refs[-1].end_ticks == end_ticks


def _install_w4_e2_transcript(root: Path) -> None:
    transcript = TimedTranscript(
        schema_version=1,
        transcript_version_id="tr_a",
        source_id="src_a",
        parent_version_id=None,
        provenance=_provenance("src_a"),
        language="zh-CN",
        segments=(
            _segment(
                "seg_a1",
                0,
                250_000,
                "你好A😀1",
                "spk_0",
                corrected_text="你，好 A😀1。",
                fine_units=(
                    FineUnit("character", "你", 0, 50_000, None),
                    FineUnit("character", "好", 50_000, 100_000, None),
                    FineUnit("word", "A", 100_000, 150_000, None),
                    FineUnit("character", "😀", 150_000, 200_000, None),
                    FineUnit("token", "1", 200_000, 250_000, None),
                ),
            ),
            _segment(
                "seg_a2",
                250_000,
                500_000,
                "同段继续。",
                "spk_2",
                fine_units=(
                    FineUnit("character", "同", 250_000, 300_000, None),
                    FineUnit("character", "段", 300_000, 350_000, None),
                    FineUnit("character", "继", 350_000, 400_000, None),
                    FineUnit("character", "续", 400_000, 500_000, None),
                ),
            ),
            _segment(
                "seg_a3",
                500_000,
                700_000,
                "老师开场。",
                "spk_1",
                fine_units=(
                    FineUnit("character", "老", 500_000, 550_000, None),
                    FineUnit("character", "师", 550_000, 600_000, None),
                    FineUnit("character", "开", 600_000, 650_000, None),
                    FineUnit("character", "场", 650_000, 700_000, None),
                ),
            ),
            _segment("seg_a4", 700_000, 850_000, "坏单位文本。", "spk_1"),
            _segment(
                "seg_a5",
                850_000,
                1_050_000,
                "重复重复",
                "spk_1",
                fine_units=(
                    FineUnit("token", "重复", 850_000, 950_000, None),
                    FineUnit("token", "重复", 950_000, 1_050_000, None),
                ),
            ),
        ),
    )
    path = root / "transcripts" / "src_a" / "tr_a.json"
    path.write_text(json.dumps(transcript.to_dict(), ensure_ascii=False), encoding="utf-8")


def test_w4_e2_caret_identity_alignment_and_utf16_offsets(tmp_path: Path) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    _install_w4_e2_transcript(root)
    page = read_readable_transcript(
        root,
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        offset=0,
        limit=20,
    )
    first, second, first_other_source = page.paragraphs[:3]

    emoji_caret = resolve_transcript_caret(
        root,
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        view_hash=page.view_hash,
        paragraph_id=first.paragraph_id,
        offset=7,
        offset_encoding="utf16",
    )
    assert emoji_caret.character_offset == 6
    assert emoji_caret.utf16_offset == 7
    assert emoji_caret.left is not None
    assert emoji_caret.left.fine_unit_index == 3
    assert emoji_caret.right is not None
    assert emoji_caret.right.fine_unit_index == 4
    assert emoji_caret.degraded is False

    first_end = resolve_transcript_caret(
        root,
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        view_hash=page.view_hash,
        paragraph_id=first.paragraph_id,
        offset=len(first.text),
    )
    second_start = resolve_transcript_caret(
        root,
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        view_hash=page.view_hash,
        paragraph_id=second.paragraph_id,
        offset=0,
    )
    assert first_end.left is not None
    assert first_end.left.end_ticks == second_start.right.start_ticks == 500_000
    assert first_end.boundary_id != second_start.boundary_id

    source_boundary = resolve_transcript_caret(
        root,
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        view_hash=page.view_hash,
        paragraph_id=first_other_source.paragraph_id,
        offset=0,
    )
    assert source_boundary.source_id == "src_b"
    assert source_boundary.right is not None
    assert source_boundary.right.source_id == "src_b"

    degraded = resolve_transcript_caret(
        root,
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        view_hash=page.view_hash,
        paragraph_id=second.paragraph_id,
        offset=second.text.index("坏单位文本。") + 2,
    )
    assert degraded.degraded is True
    assert degraded.degradation_reason == "missing_fine_units"
    assert degraded.character_offset in {
        second.text.index("坏单位文本。"),
        len(second.text),
    }
    with pytest.raises(ProjectError, match="paragraph does not exist"):
        resolve_transcript_caret(
            root,
            source_bindings=state["bindings"],  # type: ignore[arg-type]
            expected_revision=1,
            view_hash=page.view_hash,
            paragraph_id="paragraph_missing",
            offset=0,
        )


def test_w4_e2_continuous_selection_forward_reverse_cross_paragraph_and_stale(
    tmp_path: Path,
) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    _install_w4_e2_transcript(root)
    page = read_readable_transcript(
        root,
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        offset=0,
        limit=20,
    )
    first, second = page.paragraphs[:2]
    forward = resolve_continuous_transcript_selection(
        root,
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        view_hash=page.view_hash,
        anchor={"paragraph_id": first.paragraph_id, "offset": 2},
        focus={"paragraph_id": first.paragraph_id, "offset": 6},
    )
    reverse = resolve_continuous_transcript_selection(
        root,
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        view_hash=page.view_hash,
        anchor={"paragraph_id": first.paragraph_id, "offset": 7, "offset_encoding": "utf16"},
        focus={"paragraph_id": first.paragraph_id, "offset": 2},
    )
    assert forward.direction == "forward"
    assert reverse.direction == "backward"
    assert forward.refs == reverse.refs
    assert forward.canonical_text == "好 A😀"
    assert forward.refs[0].start_ticks == 50_000
    assert forward.refs[0].end_ticks == 200_000
    assert forward.refs[0].fine_unit_start_index == 1
    assert forward.refs[0].fine_unit_end_index == 4

    outward_tie = resolve_continuous_transcript_selection(
        root,
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        view_hash=page.view_hash,
        anchor={"paragraph_id": first.paragraph_id, "offset": 1},
        focus={"paragraph_id": first.paragraph_id, "offset": 3},
    )
    # The display-only gap between 好 and A carries no fine-unit ticks, so
    # the legacy snap-over-gap behavior is preserved for that gap.
    assert outward_tie.adjusted is True
    assert outward_tie.start_caret.character_offset == 0
    assert outward_tie.end_caret.character_offset == 4
    assert outward_tie.refs[0].start_ticks == 0
    assert outward_tie.refs[0].end_ticks == 100_000

    cross = resolve_continuous_transcript_selection(
        root,
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        view_hash=page.view_hash,
        anchor={"paragraph_id": first.paragraph_id, "offset": len(first.text) - 2},
        focus={"paragraph_id": second.paragraph_id, "offset": 2},
    )
    assert [ref.segment_id for ref in cross.refs] == ["seg_a2", "seg_a3"]
    assert cross.canonical_text.endswith("\n老师")

    from_paragraph_boundary = resolve_continuous_transcript_selection(
        root,
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        view_hash=page.view_hash,
        anchor={"paragraph_id": first.paragraph_id, "offset": len(first.text)},
        focus={"paragraph_id": second.paragraph_id, "offset": 2},
    )
    assert [ref.segment_id for ref in from_paragraph_boundary.refs] == ["seg_a3"]
    assert from_paragraph_boundary.start_caret.paragraph_id == first.paragraph_id
    assert from_paragraph_boundary.end_caret.paragraph_id == second.paragraph_id

    with pytest.raises(ProjectError, match="view hash is stale"):
        resolve_continuous_transcript_selection(
            root,
            source_bindings=state["bindings"],  # type: ignore[arg-type]
            expected_revision=1,
            view_hash="0" * 64,
            anchor={"paragraph_id": first.paragraph_id, "offset": 0},
            focus={"paragraph_id": first.paragraph_id, "offset": 1},
        )

    repeated_start = second.text.index("重复重复")
    first_repeat = resolve_continuous_transcript_selection(
        root,
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        view_hash=page.view_hash,
        anchor={"paragraph_id": second.paragraph_id, "offset": repeated_start},
        focus={"paragraph_id": second.paragraph_id, "offset": repeated_start + 2},
    )
    second_repeat = resolve_continuous_transcript_selection(
        root,
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        view_hash=page.view_hash,
        anchor={"paragraph_id": second.paragraph_id, "offset": repeated_start + 2},
        focus={"paragraph_id": second.paragraph_id, "offset": repeated_start + 4},
    )
    assert first_repeat.canonical_text == second_repeat.canonical_text == "重复"
    assert first_repeat.refs[0].start_ticks == 850_000
    assert second_repeat.refs[0].start_ticks == 950_000

    bad_start = second.text.index("坏单位文本。")
    degraded = resolve_continuous_transcript_selection(
        root,
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        view_hash=page.view_hash,
        anchor={"paragraph_id": second.paragraph_id, "offset": bad_start + 1},
        focus={"paragraph_id": second.paragraph_id, "offset": bad_start + 3},
    )
    assert degraded.degraded is True
    assert degraded.degradation_reasons == ("missing_fine_units",)
    assert degraded.refs[0].segment_id == "seg_a4"
    assert degraded.refs[0].canonical_text == "坏单位文本。"
    assert degraded.refs[0].start_ticks == 700_000
    assert degraded.refs[0].end_ticks == 850_000


def test_w4_e2_move_and_insert_payloads_preserve_exact_refs(tmp_path: Path) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    _install_w4_e2_transcript(root)
    page = read_readable_transcript(
        root,
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        offset=0,
        limit=20,
    )
    first, second = page.paragraphs[:2]
    source_selection = resolve_continuous_transcript_selection(
        root,
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        view_hash=page.view_hash,
        anchor={"paragraph_id": first.paragraph_id, "offset": 1},
        focus={"paragraph_id": first.paragraph_id, "offset": 3},
    )
    draft_refs = (
        ResolvedSelectionRef("src_a", "tr_a", "seg_a3", 500_000, 550_000, "老", 0, 1),
        *source_selection.refs,
        ResolvedSelectionRef("src_a", "tr_a", "seg_a3", 550_000, 600_000, "师", 1, 2),
    )
    end_caret = make_exact_sequence_caret(
        page.view_hash,
        second.paragraph_id,
        draft_refs,
        len(draft_refs),
    )
    moved = move_draft_selection_to_caret(
        draft_refs,
        selection=source_selection,
        selection_start_index=1,
        caret=end_caret,
    )
    assert moved.changed is True
    assert moved.refs == (draft_refs[0], draft_refs[2], draft_refs[1])
    assert moved.caret.sequence_index == 3

    def sort_key(ref: ResolvedSelectionRef) -> tuple[str, str, str, int, int]:
        return (
            ref.source_id,
            ref.transcript_version_id,
            ref.segment_id,
            ref.start_ticks,
            ref.end_ticks,
        )

    assert sorted(moved.refs, key=sort_key) == sorted(draft_refs, key=sort_key)

    no_op_caret = make_exact_sequence_caret(
        page.view_hash,
        first.paragraph_id,
        draft_refs,
        1,
    )
    no_op = move_draft_selection_to_caret(
        draft_refs,
        selection=source_selection,
        selection_start_index=1,
        caret=no_op_caret,
    )
    assert no_op.changed is False
    assert no_op.reason == "caret_is_adjacent"
    no_op_end = move_draft_selection_to_caret(
        draft_refs,
        selection=source_selection,
        selection_start_index=1,
        caret=make_exact_sequence_caret(
            page.view_hash,
            first.paragraph_id,
            draft_refs,
            2,
        ),
    )
    assert no_op_end.changed is False
    assert no_op_end.reason == "caret_is_adjacent"

    multi_ref_selection = replace(
        source_selection,
        refs=(draft_refs[0], draft_refs[1]),
    )
    inside_caret = make_exact_sequence_caret(
        page.view_hash,
        first.paragraph_id,
        draft_refs,
        1,
    )
    with pytest.raises(ProjectError, match="inside"):
        move_draft_selection_to_caret(
            draft_refs,
            selection=multi_ref_selection,
            selection_start_index=0,
            caret=inside_caret,
        )
    with pytest.raises(ProjectError, match="stale"):
        move_draft_selection_to_caret(
            (draft_refs[2], draft_refs[0], draft_refs[1]),
            selection=source_selection,
            selection_start_index=2,
            caret=end_caret,
        )

    before_transcript = (root / "transcripts" / "src_a" / "tr_a.json").read_bytes()
    inserted = insert_transcript_selection_at_caret(
        root,
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        view_hash=page.view_hash,
        draft_refs=(draft_refs[0], draft_refs[2]),
        selection=source_selection,
        caret=make_exact_sequence_caret(
            page.view_hash,
            second.paragraph_id,
            (draft_refs[0], draft_refs[2]),
            1,
        ),
    )
    assert inserted.refs == (draft_refs[0], draft_refs[1], draft_refs[2])
    assert inserted.caret.sequence_index == 2
    assert (root / "transcripts" / "src_a" / "tr_a.json").read_bytes() == before_transcript

    cross_speaker = resolve_continuous_transcript_selection(
        root,
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
        view_hash=page.view_hash,
        anchor={"paragraph_id": first.paragraph_id, "offset": len(first.text) - 2},
        focus={"paragraph_id": second.paragraph_id, "offset": 2},
    )
    with pytest.raises(ProjectError, match="speaker"):
        insert_transcript_selection_at_caret(
            root,
            source_bindings=state["bindings"],  # type: ignore[arg-type]
            expected_revision=1,
            view_hash=page.view_hash,
            draft_refs=(draft_refs[0], draft_refs[2]),
            selection=cross_speaker,
            caret=make_exact_sequence_caret(
                page.view_hash,
                second.paragraph_id,
                (draft_refs[0], draft_refs[2]),
                1,
            ),
        )

    cross_binding = replace(
        source_selection,
        refs=(
            source_selection.refs[0],
            ResolvedSelectionRef(
                "src_b",
                "tr_b",
                "seg_b1",
                0,
                120_000,
                "另一素材老师。",
            ),
        ),
    )
    with pytest.raises(ProjectError, match="bindings"):
        insert_transcript_selection_at_caret(
            root,
            source_bindings=state["bindings"],  # type: ignore[arg-type]
            expected_revision=1,
            view_hash=page.view_hash,
            draft_refs=(draft_refs[0], draft_refs[2]),
            selection=cross_binding,
            caret=make_exact_sequence_caret(
                page.view_hash,
                second.paragraph_id,
                (draft_refs[0], draft_refs[2]),
                1,
            ),
        )

    unknown = replace(
        source_selection,
        refs=(ResolvedSelectionRef("src_a", "tr_a", "seg_missing", 0, 1, "未知"),),
    )
    with pytest.raises(ProjectError, match="unknown"):
        insert_transcript_selection_at_caret(
            root,
            source_bindings=state["bindings"],  # type: ignore[arg-type]
            expected_revision=1,
            view_hash=page.view_hash,
            draft_refs=(draft_refs[0], draft_refs[2]),
            selection=unknown,
            caret=make_exact_sequence_caret(
                page.view_hash,
                second.paragraph_id,
                (draft_refs[0], draft_refs[2]),
                1,
            ),
        )

    discontinuous = replace(
        source_selection,
        refs=(
            ResolvedSelectionRef("src_a", "tr_a", "seg_a1", 0, 50_000, "你，", 0, 1),
            ResolvedSelectionRef(
                "src_a",
                "tr_a",
                "seg_a1",
                150_000,
                200_000,
                "😀",
                3,
                4,
            ),
        ),
    )
    with pytest.raises(ProjectError, match="continuous"):
        insert_transcript_selection_at_caret(
            root,
            source_bindings=state["bindings"],  # type: ignore[arg-type]
            expected_revision=1,
            view_hash=page.view_hash,
            draft_refs=(draft_refs[0], draft_refs[2]),
            selection=discontinuous,
            caret=make_exact_sequence_caret(
                page.view_hash,
                second.paragraph_id,
                (draft_refs[0], draft_refs[2]),
                1,
            ),
        )


def test_markdown_and_mapping_are_deterministic_and_path_free(tmp_path: Path) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    first = export_markdown(
        root,
        basis="transcript",
        output_path=tmp_path / "导出一.md",
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
    )
    second = export_markdown(
        root,
        basis="transcript",
        output_path=tmp_path / "导出二.md",
        source_bindings=state["bindings"],  # type: ignore[arg-type]
        expected_revision=1,
    )

    first_md = Path(first.markdown_path).read_bytes()
    second_md = Path(second.markdown_path).read_bytes()
    first_map = Path(first.mapping_path).read_bytes()
    second_map = Path(second.mapping_path).read_bytes()
    assert first_md == second_md
    assert first_map == second_map
    assert "开场 采访" in first_md.decode()
    serialized = first_md + first_map
    assert b"/private/" not in serialized
    assert b"raw-asr" not in serialized
    mapping = json.loads(first_map)
    assert mapping["view_hash"]
    assert mapping["entries"][0]["refs"][0]["segment_id"] == "seg_a1"


def test_markdown_exports_existing_proposal_and_decision_scripts(tmp_path: Path) -> None:
    state = _fixture(tmp_path)
    root = Path(state["root"])
    proposal_id = _proposal(root)
    proposal_data = json.loads(
        (root / "proposals" / f"{proposal_id}.json").read_text(encoding="utf-8")
    )
    decision = MultiSourceEditDecision(
        edit_version_id="edit_export",
        proposal_snapshot=MultiSourceEditProposal.from_dict(proposal_data),
        project_revision=2,
        created_at="fixture",
    )
    decision_path = root / "edits" / "edit_export.json"
    decision_path.parent.mkdir(parents=True, exist_ok=True)
    decision_path.write_text(json.dumps(decision.to_dict(), ensure_ascii=False), encoding="utf-8")

    proposal_export = export_markdown(
        root,
        basis="proposal",
        artifact_id=proposal_id,
        output_path=tmp_path / "proposal.md",
        expected_revision=1,
    )
    decision_export = export_markdown(
        root,
        basis="decision",
        artifact_id=decision.edit_version_id,
        output_path=tmp_path / "decision.md",
        expected_revision=1,
    )

    proposal_map = json.loads(Path(proposal_export.mapping_path).read_text(encoding="utf-8"))
    decision_map = json.loads(Path(decision_export.mapping_path).read_text(encoding="utf-8"))
    assert proposal_map["basis"] == "proposal"
    assert proposal_map["artifact_hash"]
    assert proposal_map["entries"][1]["refs"] == [
        {
            "end_ticks": 220_000,
            "segment_id": "seg_a2",
            "source_id": "src_a",
            "start_ticks": 160_000,
            "transcript_version_id": "tr_a",
        }
    ]
    assert decision_map["basis"] == "decision"
    assert decision_map["artifact_id"] == "edit_export"
    exported_bytes = (
        Path(decision_export.markdown_path).read_bytes()
        + Path(decision_export.mapping_path).read_bytes()
    )
    assert b"/private/" not in exported_bytes
    assert b"raw-asr" not in exported_bytes


def test_markdown_publish_failure_leaves_no_half_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _fixture(tmp_path)
    import roughcut.application.readable_transcripts as readable

    output = tmp_path / "failure.md"
    real_replace = readable.os.replace
    calls = 0

    def fail_second(source: str | bytes | Path, destination: str | bytes | Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("mapping publish failed")
        real_replace(source, destination)

    monkeypatch.setattr(readable.os, "replace", fail_second)
    with pytest.raises(OSError, match="mapping publish failed"):
        export_markdown(
            Path(state["root"]),
            basis="transcript",
            output_path=output,
            source_bindings=state["bindings"],  # type: ignore[arg-type]
            expected_revision=1,
        )

    assert not output.exists()
    assert not output.with_suffix(".map.json").exists()
    assert not list(tmp_path.glob(".*.tmp"))


def test_markdown_second_temp_failure_cleans_first_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _fixture(tmp_path)
    import roughcut.application.readable_transcripts as readable

    output = tmp_path / "temp-failure.md"
    real_write_temp = readable._write_temp
    calls = 0

    def fail_second(directory: Path, content: bytes) -> Path:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("mapping temp failed")
        return real_write_temp(directory, content)

    monkeypatch.setattr(readable, "_write_temp", fail_second)
    with pytest.raises(OSError, match="mapping temp failed"):
        export_markdown(
            Path(state["root"]),
            basis="transcript",
            output_path=output,
            source_bindings=state["bindings"],  # type: ignore[arg-type]
            expected_revision=1,
        )

    assert not output.exists()
    assert not output.with_suffix(".map.json").exists()
    assert not list(tmp_path.glob(".*.tmp"))
