from __future__ import annotations

import copy

import pytest

from roughcut.domain.errors import WorkflowError
from roughcut.domain.workflow_actions import parse_workflow_action_input
from roughcut.mcp import CONTENT_DRAFT_BLOCK_SCHEMA


def _ref() -> dict[str, object]:
    return {
        "source_id": "src_a",
        "transcript_version_id": "tr_a",
        "segment_id": "seg_1",
        "start_ticks": 0,
        "end_ticks": 120_000,
    }


def _input(blocks: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "parent_draft_ref": None,
        "display_title": "合同初稿",
        "source_bindings": [
            {"source_id": "src_a", "transcript_version_id": "tr_a"}
        ],
        "brief_ref": {
            "artifact_id": "brief_a",
            "schema_version": 1,
            "content_hash": "a" * 64,
        },
        "context_hash": "b" * 64,
        "blocks": blocks,
        "scoped_mutable_block_ids": [],
    }


def _source(block_id: str = "body_a") -> dict[str, object]:
    return {
        "block_id": block_id,
        "kind": "source_excerpt",
        "refs": [_ref()],
        "canonical_text": "原稿正文。",
    }


def _narration(block_id: str = "narration_a") -> dict[str, object]:
    return {
        "block_id": block_id,
        "kind": "narration",
        "text": "解说正文。",
        "status": "draft",
        "recorded_refs": [],
    }


def _heading(block_id: str, title: str) -> dict[str, object]:
    return {"block_id": block_id, "kind": "section_title", "title": title}


def test_submit_draft_accepts_schema2_blocks_and_trims_heading_title() -> None:
    parsed = parse_workflow_action_input(
        "submit_draft",
        _input(
            [
                _heading("section_a", "  同名章节  "),
                _heading("section_b", "同名章节"),
                _source(),
                _narration(),
                _heading("section_c", "末尾章节"),
            ]
        ),
    )

    assert [block["kind"] for block in parsed["blocks"]] == [
        "section_title",
        "section_title",
        "source_excerpt",
        "narration",
        "section_title",
    ]
    assert [block.get("title") for block in parsed["blocks"] if block["kind"] == "section_title"] == [
        "同名章节",
        "同名章节",
        "末尾章节",
    ]
    assert parsed["display_title"] == "合同初稿"


def test_submit_draft_allows_core_to_derive_omitted_canonical_text() -> None:
    block = _source()
    block.pop("canonical_text")

    parsed = parse_workflow_action_input(
        "submit_draft", _input([_heading("section_a", "章节"), block])
    )

    assert "canonical_text" not in parsed["blocks"][1]


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda payload: payload["blocks"][0].update({"section_title": "旧字段"}), "fields are invalid"),
        (lambda payload: payload["blocks"][0].update({"extra": True}), "fields are invalid"),
        (lambda payload: payload["blocks"][0].update({"title": "   \t"}), "non-empty"),
        (lambda payload: payload["blocks"][0].update({"title": "x" * 81}), "1-80"),
        (lambda payload: payload["blocks"][0].update({"block_id": "bad.id"}), "safe ID"),
        (lambda payload: payload["blocks"].append(copy.deepcopy(payload["blocks"][0])), "duplicate block_id"),
    ],
)
def test_submit_draft_rejects_invalid_schema2_heading_or_ids(
    mutate: object, message: str
) -> None:
    payload = _input([_heading("section_a", "章节"), _source()])
    assert callable(mutate)
    mutate(payload)  # type: ignore[operator]
    with pytest.raises(WorkflowError, match=message):
        parse_workflow_action_input("submit_draft", payload)


def test_submit_draft_rejects_embedded_section_title_on_content_blocks() -> None:
    for block in (_source(), _narration()):
        payload = _input([block])
        block["section_title"] = "旧章节"
        with pytest.raises(WorkflowError, match="fields are invalid"):
            parse_workflow_action_input("submit_draft", payload)


def test_submit_draft_rejects_agent_supplied_display_text() -> None:
    payload = _input([_source()])
    payload["blocks"][0]["display_text"] = "原稿正文。！！"
    with pytest.raises(WorkflowError, match="fields are invalid"):
        parse_workflow_action_input("submit_draft", payload)


def test_mcp_content_draft_block_union_matches_submit_draft_shape() -> None:
    branches = CONTENT_DRAFT_BLOCK_SCHEMA["oneOf"]
    fields_by_kind = {
        branch["properties"]["kind"]["const"]: set(branch["properties"])
        for branch in branches
    }
    assert fields_by_kind == {
        "source_excerpt": {"block_id", "kind", "refs", "canonical_text"},
        "narration": {"block_id", "kind", "text", "status", "recorded_refs"},
        "section_title": {"block_id", "kind", "title"},
    }
    assert parse_workflow_action_input(
        "submit_draft", _input([_heading("section_a", "章节"), _source()])
    )["blocks"]
