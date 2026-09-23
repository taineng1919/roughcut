from __future__ import annotations

from dataclasses import replace

import pytest

from roughcut.domain.bindings import SourceTranscriptBinding
from roughcut.domain.brief import EditBrief
from roughcut.domain.edit import (
    EditClip,
    EditDecision,
    EditProposal,
    MultiSourceEditDecision,
    MultiSourceEditProposal,
)
from roughcut.domain.project import ProjectError
from roughcut.domain.timeline import TimelineSpan, VirtualTimeline


def _proposal() -> EditProposal:
    clips = (
        EditClip("clip_a", "src_a", "tr_a", "seg_a", 120, 220, "a", "甲"),
        EditClip("clip_b", "src_a", "tr_a", "seg_b", 500, 650, "b", "乙"),
        EditClip("clip_c", "src_a", "tr_a", "seg_c", 900, 950, "c", "丙"),
    )
    return EditProposal(
        proposal_id="proposal_timeline",
        base_project_revision=4,
        base_edit_version_id=None,
        source_id="src_a",
        transcript_version_id="tr_a",
        brief_snapshot=EditBrief("brief_a", "主题", 300, ("重点",), True),
        context_hash="a" * 64,
        clips=clips,
        total_duration_ticks=300,
        created_at="fixture",
    )


def test_timeline_is_contiguous_and_maps_output_clip_and_source_ticks() -> None:
    timeline = VirtualTimeline.from_proposal(_proposal())

    assert timeline.total_duration_ticks == 300
    assert [span.to_dict() for span in timeline.spans] == [
        {
            "clip_id": "clip_a",
            "source_id": "src_a",
            "source_in_ticks": 120,
            "source_out_ticks": 220,
            "output_in_ticks": 0,
            "output_out_ticks": 100,
        },
        {
            "clip_id": "clip_b",
            "source_id": "src_a",
            "source_in_ticks": 500,
            "source_out_ticks": 650,
            "output_in_ticks": 100,
            "output_out_ticks": 250,
        },
        {
            "clip_id": "clip_c",
            "source_id": "src_a",
            "source_in_ticks": 900,
            "source_out_ticks": 950,
            "output_in_ticks": 250,
            "output_out_ticks": 300,
        },
    ]
    assert timeline.map_output_time(0).to_dict() == {
        "clip_id": "clip_a",
        "source_id": "src_a",
        "output_ticks": 0,
        "source_ticks": 120,
    }
    assert timeline.map_output_time(99).source_ticks == 219
    assert timeline.map_output_time(100).clip_id == "clip_b"
    assert timeline.map_output_time(299).source_ticks == 949
    assert timeline.map_source_time("clip_b", 500).output_ticks == 100
    assert timeline.map_source_time("clip_b", 649).output_ticks == 249


def test_timeline_from_decision_uses_its_frozen_proposal_snapshot() -> None:
    proposal = _proposal()
    decision = EditDecision("edit_a", proposal, 5, "fixture")

    assert VirtualTimeline.from_decision(decision) == VirtualTimeline.from_proposal(proposal)


@pytest.mark.parametrize("tick", [-1, 300, 301])
def test_output_mapping_rejects_ticks_outside_half_open_timeline(tick: int) -> None:
    with pytest.raises(ProjectError, match="output time"):
        VirtualTimeline.from_proposal(_proposal()).map_output_time(tick)


@pytest.mark.parametrize("tick", [499, 650])
def test_source_mapping_rejects_ticks_outside_clip_half_open_range(tick: int) -> None:
    with pytest.raises(ProjectError, match="source time"):
        VirtualTimeline.from_proposal(_proposal()).map_source_time("clip_b", tick)


def test_timeline_rejects_unknown_clip_empty_sequences_and_invalid_ranges() -> None:
    timeline = VirtualTimeline.from_proposal(_proposal())
    with pytest.raises(ProjectError, match="unknown clip"):
        timeline.map_source_time("clip_unknown", 0)
    with pytest.raises(ProjectError, match="at least one"):
        VirtualTimeline(())
    with pytest.raises(ProjectError, match="non-empty"):
        TimelineSpan("clip", "src", 10, 10, 0, 1)


@pytest.mark.parametrize(
    "replacement",
    [
        TimelineSpan("clip_b", "src_a", 500, 650, 99, 249),
        TimelineSpan("clip_b", "src_a", 500, 650, 101, 251),
    ],
)
def test_timeline_rejects_output_overlap_or_gap(replacement: TimelineSpan) -> None:
    timeline = VirtualTimeline.from_proposal(_proposal())
    with pytest.raises(ProjectError, match="contiguous"):
        VirtualTimeline((timeline.spans[0], replacement, timeline.spans[2]))


def test_timeline_rejects_duplicate_clip_ids_and_total_mismatch() -> None:
    proposal = _proposal()
    duplicate = replace(proposal.clips[1], clip_id="clip_a")
    with pytest.raises(ProjectError, match="unique"):
        VirtualTimeline.from_proposal(replace(proposal, clips=(proposal.clips[0], duplicate)))
    with pytest.raises(ProjectError, match="total duration"):
        VirtualTimeline.from_proposal(replace(proposal, total_duration_ticks=301))


def _multi_source_proposal() -> MultiSourceEditProposal:
    clips = (
        EditClip("clip_a_open", "src_a", "tr_a", "seg_shared", 10, 110, "a", "甲"),
        EditClip("clip_b", "src_b", "tr_b", "seg_shared", 200, 350, "b", "乙"),
        EditClip("clip_a_return", "src_a", "tr_a", "seg_a_2", 500, 550, "a2", "丙"),
    )
    return MultiSourceEditProposal(
        proposal_id="proposal_multi_timeline",
        base_project_revision=8,
        base_edit_version_id=None,
        source_bindings=(
            SourceTranscriptBinding("src_a", "tr_a"),
            SourceTranscriptBinding("src_b", "tr_b"),
        ),
        brief_snapshot=EditBrief("brief_multi", "主题", 300, ("重点",), True),
        context_hash="b" * 64,
        clips=clips,
        total_duration_ticks=300,
        created_at="fixture",
    )


def test_multi_source_timeline_maps_a_b_a_by_clip_identity_and_half_open_bounds() -> None:
    proposal = _multi_source_proposal()
    timeline = VirtualTimeline.from_proposal(proposal)

    assert timeline.total_duration_ticks == 300
    assert [span.source_id for span in timeline.spans] == ["src_a", "src_b", "src_a"]
    assert timeline.map_output_time(0).to_dict() == {
        "clip_id": "clip_a_open",
        "source_id": "src_a",
        "output_ticks": 0,
        "source_ticks": 10,
    }
    assert timeline.map_output_time(99).source_ticks == 109
    assert timeline.map_output_time(100).clip_id == "clip_b"
    assert timeline.map_output_time(249).source_ticks == 349
    assert timeline.map_output_time(250).clip_id == "clip_a_return"
    assert timeline.map_output_time(299).source_ticks == 549
    assert timeline.map_source_time("clip_a_open", 10).output_ticks == 0
    assert timeline.map_source_time("clip_a_return", 500).output_ticks == 250
    with pytest.raises(ProjectError):
        timeline.map_source_time("clip_a_open", 500)
    with pytest.raises(ProjectError):
        timeline.map_output_time(300)


def test_multi_source_schema_two_roundtrips_and_decision_builds_the_same_timeline() -> None:
    proposal = _multi_source_proposal()
    restored = MultiSourceEditProposal.from_dict(proposal.to_dict())
    decision = MultiSourceEditDecision(
        edit_version_id="edit_multi",
        proposal_snapshot=restored,
        project_revision=9,
        created_at="fixture",
    )

    assert restored == proposal
    assert restored.schema_version == 2
    assert MultiSourceEditDecision.from_dict(decision.to_dict()) == decision
    assert VirtualTimeline.from_decision(decision) == VirtualTimeline.from_proposal(restored)


def test_legacy_schema_one_roundtrip_still_builds_single_source_timeline() -> None:
    proposal = _proposal()
    restored = EditProposal.from_dict(proposal.to_dict())
    decision = EditDecision("edit_legacy", restored, 5, "fixture")

    assert restored.schema_version == 1
    assert EditDecision.from_dict(decision.to_dict()) == decision
    assert [span.source_id for span in VirtualTimeline.from_decision(decision).spans] == [
        "src_a",
        "src_a",
        "src_a",
    ]
