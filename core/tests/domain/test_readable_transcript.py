from __future__ import annotations

import pytest

from roughcut.domain.project import ProjectError
from roughcut.domain.readable_transcript import (
    canonical_text_for_range,
    codepoint_to_utf16_offset,
    fine_unit_alignment,
    trusted_fine_unit_spans,
    utf16_to_codepoint_offset,
)
from roughcut.domain.transcript import FineUnit, TranscriptSegment


def _segment(
    *,
    text: str = "甲乙丙。",
    corrected_text: str | None = None,
    fine_units: tuple[FineUnit, ...] | None = None,
) -> TranscriptSegment:
    return TranscriptSegment(
        segment_id="seg_fixture",
        start_ticks=100,
        end_ticks=500,
        original_text=text,
        corrected_text=corrected_text,
        local_speaker_id="spk_0",
        person_id=None,
        confidence=None,
        fine_units=(
            fine_units
            if fine_units is not None
            else (
                FineUnit("character", "甲", 100, 200, None),
                FineUnit("character", "乙", 200, 300, None),
                FineUnit("character", "丙", 300, 400, None),
                FineUnit("character", "。", 400, 500, None),
            )
        ),
        editorial_mark="unmarked",
    )


def test_canonical_text_uses_full_effective_segment_text() -> None:
    assert canonical_text_for_range(_segment(), 100, 500) == "甲乙丙。"
    assert (
        canonical_text_for_range(_segment(corrected_text="校正全文", fine_units=()), 100, 500)
        == "校正全文"
    )


def test_canonical_text_allows_only_contiguous_trusted_fine_units() -> None:
    assert canonical_text_for_range(_segment(), 200, 400) == "乙丙"
    assert canonical_text_for_range(_segment(), 200, 350) == "甲乙丙。"


def test_canonical_text_uses_segment_edges_as_natural_partial_boundaries() -> None:
    segment = _segment(
        fine_units=(
            FineUnit("character", "甲", 100, 200, None),
            FineUnit("character", "乙", 200, 300, None),
            FineUnit("character", "丙", 300, 400, None),
        )
    )

    assert canonical_text_for_range(segment, 100, 300) == "甲乙"
    assert canonical_text_for_range(segment, 200, 500) == "乙丙。"


@pytest.mark.parametrize(
    "fine_units",
    [
        (),
        (
            FineUnit("character", "甲", 100, 300, None),
            FineUnit("character", "乙", 250, 400, None),
            FineUnit("character", "丙。", 400, 500, None),
        ),
        (
            FineUnit("character", "甲", 99, 200, None),
            FineUnit("character", "乙丙。", 200, 500, None),
        ),
        (
            FineUnit("character", "甲", 100, 200, None),
            FineUnit("character", "乙。", 200, 500, None),
        ),
    ],
)
def test_canonical_text_falls_back_for_missing_overlapping_out_of_bounds_or_mismatched_units(
    fine_units: tuple[FineUnit, ...],
) -> None:
    assert canonical_text_for_range(_segment(fine_units=fine_units), 200, 400) == "甲乙丙。"


def test_corrected_text_invalidates_original_fine_unit_alignment() -> None:
    segment = _segment(corrected_text="甲乙丁。")
    assert canonical_text_for_range(segment, 200, 400) == "甲乙丁。"
    assert fine_unit_alignment(segment)[1] == "spoken_text_mismatch"


def test_display_alignment_ignores_only_punctuation_and_display_whitespace() -> None:
    segment = _segment(
        text="Hello， 世界 2026！",
        corrected_text="Hello, 世界 2026。",
        fine_units=(
            FineUnit("word", "Hello", 100, 200, None),
            FineUnit("character", "世", 200, 250, None),
            FineUnit("character", "界", 250, 300, None),
            FineUnit("token", "2026", 300, 450, None),
        ),
    )

    spans = trusted_fine_unit_spans(segment)

    assert spans is not None
    assert [span.unit.text for span in spans] == ["Hello", "世", "界", "2026"]
    assert canonical_text_for_range(segment, 100, 300) == "Hello, 世界 "
    assert canonical_text_for_range(segment, 200, 450) == "世界 2026。"


def test_corrected_text_punctuation_only_change_keeps_units_trusted() -> None:
    segment = _segment(
        text="甲乙丙",
        corrected_text="甲，乙丙！",
        fine_units=(
            FineUnit("character", "甲", 100, 200, None),
            FineUnit("character", "乙", 200, 300, None),
            FineUnit("character", "丙", 300, 500, None),
        ),
    )

    assert trusted_fine_unit_spans(segment) is not None
    assert canonical_text_for_range(segment, 100, 300) == "甲，乙"


def test_alignment_reports_missing_and_empty_fine_units() -> None:
    assert fine_unit_alignment(_segment(fine_units=()))[1] == "missing_fine_units"
    empty_text = (
        FineUnit("character", "", 100, 200, None),
        FineUnit("character", "乙丙", 200, 500, None),
    )
    assert fine_unit_alignment(_segment(fine_units=empty_text))[1] == "empty_fine_unit_text"


@pytest.mark.parametrize(
    "fine_units",
    [
        (
            FineUnit("character", "甲", 100, 200, None),
            FineUnit("character", "乙", 300, 400, None),
            FineUnit("character", "丙", 250, 500, None),
        ),
        (
            FineUnit("character", "甲", 100, 200, None),
            FineUnit("character", "乙", 200, 300, None),
            FineUnit("character", "丙", 300, 501, None),
        ),
        (
            FineUnit("character", "甲", 100, 200, None),
            FineUnit("character", "乙", 200, 300, None),
            FineUnit("character", "丁", 300, 500, None),
        ),
    ],
)
def test_display_alignment_rejects_disordered_out_of_bounds_or_spoken_text_changes(
    fine_units: tuple[FineUnit, ...],
) -> None:
    assert trusted_fine_unit_spans(_segment(fine_units=fine_units)) is None


def test_utf16_offsets_round_trip_emoji_and_reject_half_surrogate() -> None:
    text = "甲😀乙"

    assert codepoint_to_utf16_offset(text, 0) == 0
    assert codepoint_to_utf16_offset(text, 2) == 3
    assert utf16_to_codepoint_offset(text, 3) == 2
    with pytest.raises(ProjectError, match="surrogate"):
        utf16_to_codepoint_offset(text, 2)
