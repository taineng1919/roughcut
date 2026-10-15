from __future__ import annotations

import hashlib

import pytest

from roughcut.application.content_drafts import prepare_content_draft_editor_child
from roughcut.domain.bindings import SourceTranscriptBinding
from roughcut.domain.brief import EditBrief
from roughcut.domain.content_draft import (
    ContentDraft,
    ContentDraftRef,
    NarrationBlock,
    SectionTitleBlock,
    SourceExcerptBlock,
    legacy_section_block_id,
    project_schema1_to_schema2,
    validate_punctuation_replacement,
)
from roughcut.domain.project import ProjectError


def _brief() -> EditBrief:
    return EditBrief("brief_fixture", "主题", 1_200_000, ("重点",), True)


def _ref() -> ContentDraftRef:
    return ContentDraftRef("src_a", "tr_a", "seg_a", 0, 120_000)


def _schema1() -> ContentDraft:
    return ContentDraft(
        content_draft_id="draft_legacy",
        parent_draft_id=None,
        base_project_revision=3,
        confirmed_by_user=False,
        brief_snapshot=_brief(),
        source_bindings=(SourceTranscriptBinding("src_a", "tr_a"),),
        context_hash="a" * 64,
        blocks=(
            SourceExcerptBlock("block_a", (_ref(),), "原话", section_title="第一章"),
            NarrationBlock("narration_a", "旁白", "draft", (), section_title="第二章"),
        ),
        schema_version=1,
    )


def test_schema2_headings_are_independent_and_empty_chapters_roundtrip() -> None:
    draft = ContentDraft(
        content_draft_id="draft_schema2",
        parent_draft_id=None,
        base_project_revision=3,
        confirmed_by_user=False,
        brief_snapshot=_brief(),
        source_bindings=(SourceTranscriptBinding("src_a", "tr_a"),),
        context_hash="a" * 64,
        blocks=(
            SectionTitleBlock("section_a", "同名"),
            SectionTitleBlock("section_b", "同名"),
            SourceExcerptBlock("block_a", (_ref(),), "原话"),
            SectionTitleBlock("section_c", "末尾空章"),
        ),
        schema_version=2,
    )

    payload = draft.to_dict()
    assert payload["schema_version"] == 2
    assert payload["blocks"][-1] == {
        "block_id": "section_c",
        "kind": "section_title",
        "title": "末尾空章",
    }
    assert all("section_title" not in block for block in payload["blocks"])
    assert ContentDraft.from_dict(payload) == draft


def test_schema1_projection_uses_the_frozen_safe_id_and_keeps_parent_unchanged() -> None:
    parent = _schema1()
    projected = project_schema1_to_schema2(parent)
    assert parent.schema_version == 1
    assert parent.blocks[0].section_title == "第一章"  # type: ignore[union-attr]
    heading = projected.blocks[0]
    assert isinstance(heading, SectionTitleBlock)
    expected_material = b"draft_legacy\x00block_a\x000"
    expected_id = "legacy_section_" + hashlib.sha256(expected_material).hexdigest()[:32]
    assert heading.block_id == expected_id == legacy_section_block_id("draft_legacy", "block_a", 0)
    assert heading.block_id.isascii()
    assert projected.schema_version == 2
    assert [block.kind for block in projected.blocks] == [
        "section_title",
        "source_excerpt",
        "section_title",
        "narration",
    ]


def test_schema1_projection_collision_fails_closed() -> None:
    parent = _schema1()
    collision = legacy_section_block_id("draft_legacy", "block_a", 0)
    # The collision is checked while projecting, not by silently aliasing it.
    with pytest.raises(ProjectError, match="collision"):
        project_schema1_to_schema2(
            ContentDraft(
                content_draft_id=parent.content_draft_id,
                parent_draft_id=None,
                base_project_revision=parent.base_project_revision,
                confirmed_by_user=False,
                brief_snapshot=parent.brief_snapshot,
                source_bindings=parent.source_bindings,
                context_hash=parent.context_hash,
                blocks=(
                    SourceExcerptBlock("block_a", (_ref(),), "原话", section_title="第一章"),
                    SourceExcerptBlock(collision, (_ref(),), "原话"),
                ),
                schema_version=1,
            )
        )


def test_first_editor_child_is_schema2_without_mutating_schema1_parent() -> None:
    parent = _schema1()
    child = prepare_content_draft_editor_child(
        parent=parent,
        blocks=parent.blocks,
        child_id="draft_child",
    )
    assert parent.schema_version == 1
    assert child.schema_version == 2
    assert child.parent_draft_id == parent.content_draft_id
    assert child.blocks[0].kind == "section_title"
    assert all(
        getattr(block, "section_title", None) is None
        for block in child.blocks
        if not isinstance(block, SectionTitleBlock)
    )


def test_schema2_source_excerpt_roundtrips_optional_editorial_display_text() -> None:
    block = SourceExcerptBlock(
        "block_a",
        (_ref(),),
        "原话",
        display_text="原话！",
    )
    draft = ContentDraft(
        content_draft_id="draft_display",
        parent_draft_id=None,
        base_project_revision=3,
        confirmed_by_user=False,
        brief_snapshot=_brief(),
        source_bindings=(SourceTranscriptBinding("src_a", "tr_a"),),
        context_hash="a" * 64,
        blocks=(block,),
        schema_version=2,
    )

    payload = draft.to_dict()
    assert payload["blocks"] == [
        {
            "block_id": "block_a",
            "kind": "source_excerpt",
            "refs": [_ref().to_dict()],
            "canonical_text": "原话",
            "display_text": "原话！",
        }
    ]
    assert ContentDraft.from_dict(payload) == draft


def test_schema2_display_text_must_differ_only_by_unicode_punctuation() -> None:
    with pytest.raises(ProjectError, match="display text"):
        SourceExcerptBlock(
            "block_a",
            (_ref(),),
            "原话",
            display_text="原词",
        )

    with pytest.raises(ProjectError, match="display text"):
        SourceExcerptBlock(
            "block_a",
            (_ref(),),
            "原话",
            display_text="原话\n",
        )

    with pytest.raises(ProjectError, match="display text"):
        SourceExcerptBlock(
            "block_a",
            (_ref(),),
            "原话",
            display_text="原 话",
        )


def test_schema2_equal_display_is_omitted_and_non_punctuation_codepoints_are_verbatim() -> None:
    equal = SourceExcerptBlock("block_a", (_ref(),), "原话", display_text="原话")
    assert equal.display_text is None
    assert "display_text" not in equal.to_dict(schema_version=2)

    block = SourceExcerptBlock(
        "block_a",
        (_ref(),),
        "a😀B\n9",
        display_text="a！😀B\n9。",
    )
    assert block.display_text == "a！😀B\n9。"


def test_schema1_payload_cannot_smuggle_display_text_and_schema2_run_capacity_counts_neighbors() -> None:
    payload = _schema1().to_dict()
    source = next(block for block in payload["blocks"] if block["kind"] == "source_excerpt")
    assert isinstance(source, dict)
    source["display_text"] = "原话！"
    with pytest.raises(ProjectError, match="source block fields"):
        ContentDraft.from_dict(payload)

    with pytest.raises(ProjectError, match="run exceeds"):
        validate_punctuation_replacement(
            "原话",
            "原话。。。。。。。",
            len("原话。。。。。。。"),
            len("原话。。。。。。。"),
            "！！",
        )
    accepted = validate_punctuation_replacement(
        "原话",
        "原话。。。。。。。",
        len("原话。。。。。。。"),
        len("原话。。。。。。。"),
        "！",
    )
    assert accepted == "原话。。。。。。。！"

    with pytest.raises(ProjectError, match="range must contain"):
        validate_punctuation_replacement("原话", "原话！", 1, 2, "。")


def test_narration_duration_estimation_is_integer_frame_aligned_and_manual_override_wins() -> None:
    from roughcut.domain.content_draft import (
        estimated_narration_seconds,
        narration_effective_duration_ticks,
        narration_requested_seconds,
        narration_text_unit_count,
    )
    from roughcut.domain.time import TICKS_PER_SECOND, RationalRate

    # Whitespace and Unicode P* punctuation never count; every other code point
    # counts as one unit, and the estimate is integer math only.
    assert narration_text_unit_count("你 好，世 界。！😀") == 5
    assert estimated_narration_seconds("你 好，世 界。！😀") == 2
    assert estimated_narration_seconds("") == 1
    assert estimated_narration_seconds("字" * 200) == 60
    assert estimated_narration_seconds("字" * 201) == 61

    automatic = NarrationBlock(
        block_id="block_narration",
        text="字" * 201,
        status="approved",
        recorded_refs=(),
    )
    assert narration_requested_seconds(automatic) == 61
    at_integer = narration_effective_duration_ticks(automatic, RationalRate(25, 1))
    assert at_integer == 61 * TICKS_PER_SECOND
    assert at_integer % RationalRate(25, 1).ticks_per_frame == 0

    fractional = RationalRate(30000, 1001)
    for rate in (
        RationalRate(24, 1), RationalRate(25, 1), RationalRate(30, 1),
        RationalRate(50, 1), RationalRate(60, 1), RationalRate(24000, 1001),
        fractional, RationalRate(60000, 1001),
    ):
        ticks_per_frame = rate.ticks_per_frame
        duration = narration_effective_duration_ticks(automatic, rate)
        assert isinstance(duration, int)
        assert duration % ticks_per_frame == 0
        assert duration >= 61 * TICKS_PER_SECOND
        assert duration < 61 * TICKS_PER_SECOND + ticks_per_frame

    manual = NarrationBlock(
        block_id="block_manual",
        text="字" * 201,
        status="approved",
        recorded_refs=(),
        estimated_duration_seconds=5,
    )
    assert narration_requested_seconds(manual) == 5
    assert narration_effective_duration_ticks(manual, RationalRate(25, 1)) == (
        5 * TICKS_PER_SECOND
    )

    recorded = NarrationBlock(
        block_id="block_recorded",
        text="字" * 201,
        status="recorded",
        recorded_refs=(ContentDraftRef("src_a", "tr_a", "seg_a", 10, 500_010),),
        estimated_duration_seconds=5,
    )
    # Recorded narration keeps its exact reference duration; the manual value
    # never overrides a real recording.
    assert narration_effective_duration_ticks(recorded, fractional) == 500_000


def test_pre_feature_schema2_draft_dict_stays_byte_identical_after_roundtrip() -> None:
    """Schema-1/2 artifacts written before the duration field must not drift.

    ``estimated_duration_seconds`` is optional; parsing an old narration block
    and re-serializing it must reproduce the original mapping exactly, so the
    artifact content hash and Workflow refs stay valid.
    """

    legacy = {
        "schema_version": 2,
        "content_draft_id": "draft_legacy_roundtrip",
        "parent_draft_id": None,
        "base_project_revision": 3,
        "confirmed_by_user": True,
        "brief_snapshot": _brief().to_dict(),
        "source_bindings": [
            {"source_id": "src_a", "transcript_version_id": "tr_a"}
        ],
        "context_hash": "a" * 64,
        "display_title": None,
        "blocks": [
            {
                "block_id": "heading_legacy",
                "kind": "section_title",
                "title": "章节",
            },
            {
                "block_id": "block_legacy",
                "kind": "source_excerpt",
                "refs": [
                    {
                        "source_id": "src_a",
                        "transcript_version_id": "tr_a",
                        "segment_id": "seg_a",
                        "start_ticks": 0,
                        "end_ticks": 120_000,
                    }
                ],
                "canonical_text": "节目原话。",
            },
            {
                "block_id": "narration_legacy",
                "kind": "narration",
                "text": "旧解说。",
                "status": "approved",
                "recorded_refs": [],
            },
        ],
    }
    draft = ContentDraft.from_dict(legacy)
    assert draft.to_dict() == legacy
    narration = next(
        block for block in draft.blocks if isinstance(block, NarrationBlock)
    )
    assert narration.estimated_duration_seconds is None

    with_override = {
        **legacy,
        "blocks": [
            *legacy["blocks"][:2],
            {
                **legacy["blocks"][2],
                "estimated_duration_seconds": 4,
            },
        ],
    }
    assert ContentDraft.from_dict(with_override).to_dict() == with_override
