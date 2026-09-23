from __future__ import annotations

import copy

import pytest

from roughcut.domain.errors import WorkflowError
from roughcut.domain.workflow_actions import parse_workflow_action_input


def _outline(section_count: int) -> dict[str, object]:
    return {
        "schema_version": 1,
        "title": "Outline",
        "opening": "Opening",
        "sections": [
            {
                "section_id": f"section_{index}",
                "title": f"Section {index}",
                "summary": f"Summary {index}",
                "target_duration_ticks": 120_000,
            }
            for index in range(section_count)
        ],
        "ending": "Ending",
        "required_content_coverage": [],
        "narration_status": "none",
    }


@pytest.mark.parametrize("section_count", (1, 3, 4, 7, 8, 12))
def test_submit_outline_parser_accepts_any_non_empty_section_count(
    section_count: int,
) -> None:
    payload = _outline(section_count)

    parsed = parse_workflow_action_input("submit_outline", payload)

    assert parsed == payload
    assert len(parsed["sections"]) == section_count  # type: ignore[arg-type]


@pytest.mark.parametrize("sections", ([], {}, "section"))
def test_submit_outline_parser_rejects_empty_or_non_array_sections(
    sections: object,
) -> None:
    payload = _outline(1)
    payload["sections"] = sections

    with pytest.raises(WorkflowError, match="sections must be a non-empty array"):
        parse_workflow_action_input("submit_outline", payload)


def test_submit_outline_parser_preserves_section_integrity_gates() -> None:
    duplicate = _outline(3)
    duplicate_sections = duplicate["sections"]
    assert isinstance(duplicate_sections, list)
    duplicate_sections[1]["section_id"] = duplicate_sections[0]["section_id"]
    with pytest.raises(WorkflowError, match="duplicate section_id"):
        parse_workflow_action_input("submit_outline", duplicate)

    invalid_duration = _outline(1)
    duration_sections = invalid_duration["sections"]
    assert isinstance(duration_sections, list)
    duration_sections[0]["target_duration_ticks"] = 0
    with pytest.raises(WorkflowError, match="target_duration_ticks"):
        parse_workflow_action_input("submit_outline", invalid_duration)

    malformed_evidence = _outline(1)
    malformed_evidence["required_content_coverage"] = [
        {
            "requirement": "Required",
            "covered": True,
            "evidence_refs": [
                {
                    "source_id": "src_a",
                    "transcript_version_id": "tr_a",
                    "segment_id": "seg_a",
                    "start_ticks": 20,
                    "end_ticks": 10,
                }
            ],
        }
    ]
    with pytest.raises(WorkflowError, match="non-empty half-open range"):
        parse_workflow_action_input("submit_outline", malformed_evidence)

    coverage_mismatch = copy.deepcopy(malformed_evidence)
    coverage_mismatch["required_content_coverage"][0]["evidence_refs"] = []  # type: ignore[index]
    with pytest.raises(WorkflowError, match="covered must equal"):
        parse_workflow_action_input("submit_outline", coverage_mismatch)
