from __future__ import annotations

import json
from itertools import pairwise
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SKILLS = {
    name: (ROOT / "agent-skill" / "skills" / name / "SKILL.md").read_text(
        encoding="utf-8"
    )
    for name in ("create-roughcut", "revise-roughcut")
}


def compact(text: str) -> str:
    return "".join(text.split())


Ref = tuple[str, str, str, int, int]


def _ref(name: str, start: int) -> Ref:
    return ("src_a", "tr_a_1", f"seg_{name}", start, start + 10)


def _ref_dict(ref: Ref) -> dict[str, object]:
    return {
        "source_id": ref[0],
        "transcript_version_id": ref[1],
        "segment_id": ref[2],
        "start_ticks": ref[3],
        "end_ticks": ref[4],
    }


def _ref_tuple(value: dict[str, object]) -> Ref:
    return (
        value["source_id"],
        value["transcript_version_id"],
        value["segment_id"],
        value["start_ticks"],
        value["end_ticks"],
    )  # type: ignore[return-value]


def _block(
    block_id: str,
    kind: str,
    refs: tuple[Ref, ...] = (),
    *,
    title: str | None = None,
    status: str = "recorded",
    text: str = "fixture text is not a matching key",
) -> dict[str, object]:
    block: dict[str, object] = {"block_id": block_id, "kind": kind, "text": text}
    if kind == "section_title":
        block["title"] = title
    elif kind == "source_excerpt":
        block["refs"] = [_ref_dict(ref) for ref in refs]
    elif kind == "narration":
        block["status"] = status
        block["recorded_refs"] = [_ref_dict(ref) for ref in refs] if status == "recorded" else []
    else:
        raise AssertionError(f"unsupported fixture block kind: {kind}")
    return block


def _approved_outline() -> dict[str, object]:
    coverage = [
        ("manual_work", (_ref("a1", 100), _ref("a2", 200))),
        ("machine_work", (_ref("b", 300),)),
        ("low_price", (_ref("c", 400),)),
    ]
    return {
        "outline_ref": "outline_approved_1",
        "sections": [
            {"section_id": "opening", "title": "开场"},
            {"section_id": "dilemma", "title": "困境章节"},
            {"section_id": "result", "title": "结果"},
            {"section_id": "ending", "title": "结尾"},
        ],
        "required_content_coverage": [
            {
                "requirement": node_id,
                "covered": True,
                "evidence_refs": [_ref_dict(ref) for ref in refs],
            }
            for node_id, refs in coverage
        ],
    }


def _required_items(outline: dict[str, object]) -> list[dict[str, object]]:
    sections = outline["sections"]
    coverage = outline["required_content_coverage"]
    assert isinstance(sections, list)
    assert isinstance(coverage, list)
    dilemma = next(section for section in sections if section["section_id"] == "dilemma")
    items: list[dict[str, object]] = [
        {
            "node_id": "section_dilemma",
            "kind": "section",
            "section_id": dilemma["section_id"],
            "title": dilemma["title"],
            "outline_position": 1,
        }
    ]
    for precedence_position, entry in enumerate(coverage):
        items.append(
            {
                "node_id": entry["requirement"],
                "kind": "content",
                "section_id": "dilemma",
                "outline_coverage_index": precedence_position,
                "evidence_refs": entry["evidence_refs"],
                "precedence_position": precedence_position,
            }
        )
    return items


def _draft_blocks(
    order: tuple[str, ...] = ("manual_work", "machine_work", "low_price"),
    *,
    unrelated: bool = False,
    duplicate_manual: bool = False,
    unrecorded_between_manual: bool = False,
    missing: str | None = None,
) -> list[dict[str, object]]:
    refs: dict[str, tuple[Ref, ...]] = {
        "manual_work": (_ref("a1", 100), _ref("a2", 200)),
        "machine_work": (_ref("b", 300),),
        "low_price": (_ref("c", 400),),
    }
    blocks = [
        _block("heading_opening", "section_title", title="开场"),
        _block("heading_dilemma", "section_title", title="困境章节"),
    ]
    for node_id in order:
        if node_id == missing:
            continue
        node_refs = refs[node_id]
        if node_id == "manual_work":
            blocks.append(_block("manual_part_1", "source_excerpt", (node_refs[0],)))
            if unrecorded_between_manual:
                blocks.append(
                    _block(
                        "unrecorded_gap",
                        "narration",
                        status="approved",
                        text="unrecorded narration is a boundary",
                    )
                )
            blocks.append(_block("manual_part_2", "narration", (node_refs[1],)))
        else:
            blocks.append(_block(node_id, "source_excerpt", node_refs))
        if unrelated and node_id == "manual_work":
            blocks.append(_block("unrelated", "source_excerpt", (_ref("u", 250),)))
    if duplicate_manual:
        blocks.extend(
            [
                _block("manual_duplicate_1", "source_excerpt", (refs["manual_work"][0],)),
                _block("manual_duplicate_2", "source_excerpt", (refs["manual_work"][1],)),
            ]
        )
    blocks.extend(
        [
            _block("heading_result", "section_title", title="结果"),
            _block("heading_ending", "section_title", title="结尾"),
        ]
    )
    return blocks


def _media_entries(
    blocks: list[dict[str, object]], start: int, end: int
) -> list[list[tuple[Ref, int, str, int]]]:
    runs: list[list[tuple[Ref, int, str, int]]] = []
    entries: list[tuple[Ref, int, str, int]] = []
    for block_index in range(start, end):
        block = blocks[block_index]
        if block["kind"] == "source_excerpt":
            raw_refs = block.get("refs")
        elif block["kind"] == "narration" and block.get("status") == "recorded":
            raw_refs = block.get("recorded_refs")
        else:
            raw_refs = None
        if not isinstance(raw_refs, list) or not raw_refs:
            if entries:
                runs.append(entries)
                entries = []
            continue
        block_id = block["block_id"]
        assert isinstance(block_id, str)
        for ref_index, raw_ref in enumerate(raw_refs):
            assert isinstance(raw_ref, dict)
            entries.append((_ref_tuple(raw_ref), block_index, block_id, ref_index))
    if entries:
        runs.append(entries)
    return runs


def _fixture_order_acceptance(
    outline: dict[str, object],
    candidate_id: str,
    required_items: list[dict[str, object]],
    blocks: list[dict[str, object]],
    order_requirement: str,
) -> dict[str, object]:
    """Executable test fixture contract, not a Core/runtime acceptance consumer."""

    result: dict[str, object] = {
        "outline_ref": outline["outline_ref"],
        "candidate_id": candidate_id,
        "order_requirement": order_requirement,
        "required_items": required_items,
        "mapping": [],
        "precedence_checks": [],
        "missing_required_nodes": [],
        "result": "NOT_APPLICABLE" if order_requirement == "none" else "PASS",
    }
    if order_requirement == "none":
        return result

    sections = outline["sections"]
    coverage = outline["required_content_coverage"]
    assert isinstance(sections, list)
    assert isinstance(coverage, list)
    ranges: dict[str, tuple[int, int]] = {}
    mapping: list[dict[str, object]] = []
    mapping_by_node: dict[str, dict[str, object]] = {}
    errors: list[dict[str, str]] = []

    for item in required_items:
        node_id = item["node_id"]
        kind = item["kind"]
        section_id = item["section_id"]
        assert isinstance(node_id, str)
        assert isinstance(kind, str)
        assert isinstance(section_id, str)
        if kind == "section":
            title = item["title"]
            headings = [
                index
                for index, block in enumerate(blocks)
                if block["kind"] == "section_title" and block.get("title") == title
            ]
            if len(headings) != 1:
                errors.append(
                    {
                        "node_id": node_id,
                        "reason": (
                            "missing_required_node" if not headings else "mapping_ambiguous"
                        ),
                    }
                )
                continue
            heading_index = headings[0]
            next_heading = next(
                (
                    index
                    for index in range(heading_index + 1, len(blocks))
                    if blocks[index]["kind"] == "section_title"
                ),
                len(blocks),
            )
            ranges[section_id] = (heading_index + 1, next_heading)
            mapped = {
                "node_id": node_id,
                "kind": kind,
                "section_id": section_id,
                "heading_block_id": blocks[heading_index]["block_id"],
                "draft_block_ids": [
                    block["block_id"] for block in blocks[heading_index:next_heading]
                ],
                "draft_position": [heading_index, -1],
            }
            mapping.append(mapped)
            mapping_by_node[node_id] = mapped

    for item in required_items:
        if item["kind"] != "content":
            continue
        node_id = item["node_id"]
        section_id = item["section_id"]
        assert isinstance(node_id, str)
        assert isinstance(section_id, str)
        coverage_index = item["outline_coverage_index"]
        assert isinstance(coverage_index, int)
        if (
            coverage_index < 0
            or coverage_index >= len(coverage)
            or coverage[coverage_index]["evidence_refs"] != item["evidence_refs"]
        ):
            errors.append({"node_id": node_id, "reason": "missing_required_node"})
            continue
        if section_id not in ranges:
            errors.append({"node_id": node_id, "reason": "missing_required_node"})
            continue
        refs = item["evidence_refs"]
        assert isinstance(refs, list)
        target = tuple(_ref_tuple(ref) for ref in refs)
        start, end = ranges[section_id]
        candidates: list[list[tuple[Ref, int, str, int]]] = []
        for entries in _media_entries(blocks, start, end):
            for entry_index in range(len(entries) - len(target) + 1):
                if tuple(
                    entry[0] for entry in entries[entry_index : entry_index + len(target)]
                ) == target:
                    candidates.append(entries[entry_index : entry_index + len(target)])
        if not candidates:
            errors.append({"node_id": node_id, "reason": "missing_required_node"})
            continue
        if len(candidates) > 1:
            errors.append({"node_id": node_id, "reason": "mapping_ambiguous"})
            continue
        touched = candidates[0]
        mapped = {
            "node_id": node_id,
            "kind": "content",
            "section_id": section_id,
            "draft_block_ids": list(dict.fromkeys(entry[2] for entry in touched)),
            "draft_position": [touched[0][1], touched[0][3]],
        }
        mapping.append(mapped)
        mapping_by_node[node_id] = mapped

    precedence_items = sorted(
        (item for item in required_items if item["kind"] == "content"),
        key=lambda item: item["precedence_position"],
    )
    precedence_checks: list[dict[str, object]] = []
    for before, after in pairwise(precedence_items):
        before_id = before["node_id"]
        after_id = after["node_id"]
        if before_id not in mapping_by_node or after_id not in mapping_by_node:
            continue
        before_position = mapping_by_node[before_id]["draft_position"]
        after_position = mapping_by_node[after_id]["draft_position"]
        precedence_checks.append(
            {
                "before_node_id": before_id,
                "after_node_id": after_id,
                "before_position": before_position,
                "after_position": after_position,
                "result": "PASS" if before_position < after_position else "FAIL",
            }
        )

    result["mapping"] = mapping
    result["precedence_checks"] = precedence_checks
    result["missing_required_nodes"] = errors
    result["result"] = (
        "FAIL"
        if errors or any(check["result"] == "FAIL" for check in precedence_checks)
        else "PASS"
    )
    return result


def test_duration_acceptance_is_read_back_from_core_without_skill_recomputation() -> None:
    required = (
        "duration_acceptance",
        "target_duration_ticks",
        "actual_duration_ticks",
        "delta_ticks",
        "tolerance_ticks",
        "accepted_upper_bound_ticks",
        "actual - target",
        "floor(target * 10 / 100)",
        "within_target",
        "under_target",
        "over_target",
        "exact refs",
        "Core",
        "非阻塞 warning",
        "素材不足",
        "gap",
        "stale",
        "重新读取当前 candidate",
    )
    for content in SKILLS.values():
        flattened = compact(content)
        for phrase in required:
            assert compact(phrase) in flattened
        assert "字数" in content
        assert "Agent 估算" in content
        assert "UI 重算" in content


def test_create_reads_duration_summary_after_draft_submission() -> None:
    execution = SKILLS["create-roughcut"].split(
        "## 仅供 Agent 执行的顺序", maxsplit=1
    )[1]
    content = compact(execution)
    submit = content.index("`workflow_action(submit_draft)`")
    read = content.index("`content_draft_read`", submit)
    summary = content.index("`duration_acceptance`", read)
    assert submit < read < summary


def test_explicit_order_acceptance_has_only_declared_precedence_cases() -> None:
    required = (
        "chronological",
        "causal",
        "progressive",
        "explicit order acceptance",
        "outline_ref",
        "candidate_id",
        "required_items",
        "section node",
        "content node",
        "required_content_coverage",
        "evidence_refs",
        "outline_coverage_index",
        "mapping",
        "precedence_checks",
        "missing_required_nodes",
        "result",
        "五元组",
        "subsequence",
        "source_excerpt.refs",
        "recorded_refs",
        "draft_position",
        "`mapping_ambiguous`",
        "`missing_required_node`",
        "无关 block",
        "`NOT_APPLICABLE`",
        "A→B→C",
        "A→C→B",
        "causal A→B",
        "progressive",
        "同一 content node 跨两个相邻 blocks",
        "重复 readback",
        "不自动重排",
    )
    for content in SKILLS.values():
        flattened = compact(content)
        for phrase in required:
            assert compact(phrase) in flattened

    create = compact(SKILLS["create-roughcut"])
    for scenario in (
        "同章 chronological A→B→C",
        "同章 chronological A→C→B",
        "causal A→B",
        "progressive 保持顺序",
        "同章 A→无关 block→B→C",
        "exact ref 缺失",
        "exact ref/sequence 重复",
        "no explicit order",
        "same Outline/candidate/block readback",
    ):
        assert compact(scenario) in create


def test_order_enum_is_closed_and_uniform_across_contract_docs() -> None:
    for content in SKILLS.values():
        assert "NOT_APPLICABLE" in content
        assert "not_applicable" not in content
    for name in ("spec.md", "user-workflow.md"):
        assert "architecture/content-order-contract.md" in (
            ROOT / "docs" / name
        ).read_text(encoding="utf-8")
    for path in (ROOT / "docs/architecture/content-order-contract.md",):
        lines = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if "没有用户明确" in line and "顺序" in line
        ]
        assert lines
        assert all("NOT_APPLICABLE" in line for line in lines)
        assert all("not_applicable" not in line for line in lines)


def test_forbidden_order_implementations_are_only_negated_contract_text() -> None:
    forbidden_concepts = (
        "generic causal graph",
        "DAG",
        "Core NLP",
        "LLM judge",
        "workflow gate",
        "自动重排",
    )
    negative_markers = (
        "不建",
        "不做",
        "不调用",
        "不增加",
        "不得",
        "不自动",
        "禁止",
        "没有",
    )
    for content in SKILLS.values():
        lines = content.splitlines()
        for concept in forbidden_concepts:
            matches = [line for line in lines if concept in line]
            assert matches
            assert all(
                any(marker in line for marker in negative_markers) for line in matches
            )


def test_fixture_contract_distinguishes_same_section_content_order() -> None:
    outline = _approved_outline()
    items = _required_items(outline)
    preserved = _fixture_order_acceptance(
        outline, "draft_candidate_1", items, _draft_blocks(), "chronological"
    )
    reordered = _fixture_order_acceptance(
        outline,
        "draft_candidate_1",
        items,
        _draft_blocks(("manual_work", "low_price", "machine_work")),
        "chronological",
    )
    assert preserved["result"] == "PASS"
    assert reordered["result"] == "FAIL"
    assert any(
        check["result"] == "FAIL" for check in reordered["precedence_checks"]
    )
    assert preserved["mapping"] != reordered["mapping"]


def test_fixture_contract_supports_linear_variants_and_unrelated_insertion() -> None:
    outline = _approved_outline()
    items = _required_items(outline)
    causal_items = items[:3]
    causal = _fixture_order_acceptance(
        outline,
        "draft_candidate_1",
        causal_items,
        _draft_blocks(("manual_work", "machine_work"), missing="low_price"),
        "causal",
    )
    progressive = _fixture_order_acceptance(
        outline, "draft_candidate_1", items, _draft_blocks(), "progressive"
    )
    unrelated = _fixture_order_acceptance(
        outline, "draft_candidate_1", items, _draft_blocks(unrelated=True), "chronological"
    )
    assert causal["result"] == "PASS"
    assert progressive["result"] == "PASS"
    assert unrelated["result"] == "PASS"


def test_fixture_contract_reports_exact_ref_missing_and_repeated_ambiguity() -> None:
    outline = _approved_outline()
    items = _required_items(outline)
    missing = _fixture_order_acceptance(
        outline,
        "draft_candidate_1",
        items,
        _draft_blocks(missing="machine_work"),
        "chronological",
    )
    ambiguous = _fixture_order_acceptance(
        outline,
        "draft_candidate_1",
        items,
        _draft_blocks(duplicate_manual=True),
        "chronological",
    )
    missing_reasons = {error["reason"] for error in missing["missing_required_nodes"]}
    ambiguous_reasons = {error["reason"] for error in ambiguous["missing_required_nodes"]}
    assert missing["result"] == "FAIL"
    assert "missing_required_node" in missing_reasons
    assert ambiguous["result"] == "FAIL"
    assert "mapping_ambiguous" in ambiguous_reasons


def test_fixture_contract_cuts_unrecorded_narration_boundary() -> None:
    outline = _approved_outline()
    result = _fixture_order_acceptance(
        outline,
        "draft_candidate_1",
        _required_items(outline),
        _draft_blocks(unrecorded_between_manual=True),
        "chronological",
    )
    assert result["result"] == "FAIL"
    assert {"node_id": "manual_work", "reason": "missing_required_node"} in result[
        "missing_required_nodes"
    ]


def test_fixture_contract_cuts_heading_boundary() -> None:
    outline = _approved_outline()
    blocks = _draft_blocks()
    blocks.insert(3, _block("heading_boundary", "section_title", title="未批准断点"))
    result = _fixture_order_acceptance(
        outline, "draft_candidate_1", _required_items(outline), blocks, "chronological"
    )
    assert result["result"] == "FAIL"
    assert {"node_id": "manual_work", "reason": "missing_required_node"} in result[
        "missing_required_nodes"
    ]


def test_fixture_contract_reports_nonunique_section_heading() -> None:
    outline = _approved_outline()
    items = _required_items(outline)
    blocks = _draft_blocks()
    blocks.insert(
        len(blocks) - 2,
        _block("heading_dilemma_duplicate", "section_title", title="困境章节"),
    )
    result = _fixture_order_acceptance(
        outline, "draft_candidate_1", items, blocks, "chronological"
    )
    assert result["result"] == "FAIL"
    assert {
        error["reason"] for error in result["missing_required_nodes"]
    } >= {"mapping_ambiguous"}


def test_fixture_contract_maps_one_content_node_across_adjacent_blocks_by_exact_refs() -> None:
    outline = _approved_outline()
    result = _fixture_order_acceptance(
        outline,
        "draft_candidate_1",
        _required_items(outline),
        _draft_blocks(),
        "chronological",
    )
    manual_mapping = next(
        mapping for mapping in result["mapping"] if mapping["node_id"] == "manual_work"
    )
    assert manual_mapping["draft_block_ids"] == ["manual_part_1", "manual_part_2"]
    assert manual_mapping["draft_position"] == [2, 0]


def test_fixture_contract_requires_outline_approved_exact_refs_not_text_labels() -> None:
    outline = _approved_outline()
    items = _required_items(outline)
    items[1]["evidence_refs"] = [_ref_dict(_ref("not_approved", 900))]
    result = _fixture_order_acceptance(
        outline, "draft_candidate_1", items, _draft_blocks(), "chronological"
    )
    assert result["result"] == "FAIL"
    assert result["missing_required_nodes"] == [
        {"node_id": "manual_work", "reason": "missing_required_node"}
    ]


def test_fixture_contract_rebuilds_for_stale_candidate_and_is_deterministic() -> None:
    outline = _approved_outline()
    items = _required_items(outline)
    blocks = _draft_blocks()
    first = _fixture_order_acceptance(outline, "draft_candidate_1", items, blocks, "chronological")
    repeated = _fixture_order_acceptance(
        outline, "draft_candidate_1", items, _draft_blocks(), "chronological"
    )
    current = _fixture_order_acceptance(
        outline,
        "draft_candidate_2",
        items,
        _draft_blocks(("manual_work", "low_price", "machine_work")),
        "chronological",
    )
    no_order = _fixture_order_acceptance(
        outline, "draft_candidate_1", items, _draft_blocks(("low_price", "machine_work", "manual_work")), "none"
    )
    assert first == repeated
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(
        repeated, ensure_ascii=False, sort_keys=True
    )
    assert current["candidate_id"] == "draft_candidate_2"
    assert current["result"] == "FAIL"
    assert current["mapping"] != first["mapping"]
    assert no_order["result"] == "NOT_APPLICABLE"


def test_create_order_acceptance_runs_after_outline_approval_and_before_draft_acceptance() -> None:
    execution = compact(
        SKILLS["create-roughcut"].split("## 仅供 Agent 执行的顺序", maxsplit=1)[1]
    )
    assert execution.index("`workflow_action(approve_outline)`") < execution.index(
        "`workflow_action(submit_draft)`"
    )
    assert execution.index("`workflow_action(submit_draft)`") < execution.index(
        "`content_draft_read`"
    )
    read = execution.index("`content_draft_read`")
    assert read < execution.index("临时checklist", read)
