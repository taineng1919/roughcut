"""Closed schema-1 inputs for the finite workflow application façade."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, cast

from roughcut.domain.errors import WorkflowError
from roughcut.domain.workflow import (
    WORKFLOW_ACTIONS,
    MulticamSetupDeclaration,
    canonical_json_v1,
    validate_safe_id,
    validate_sha256,
)

_BASIS_ID = re.compile(r"^wfb_(scope|brief)_[a-f0-9]{64}$")


def _invalid(message: str) -> WorkflowError:
    return WorkflowError(
        "workflow_action_invalid",
        f"Roughcut workflow façade rejected action input: {message}",
    )


def _closed(value: object, fields: set[str], description: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _invalid(f"{description} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise _invalid(f"{description} field names must be strings")
    actual = set(value)
    if actual != fields:
        raise _invalid(
            f"{description} fields are invalid; "
            f"missing={sorted(fields - actual)}, extra={sorted(actual - fields)}"
        )
    return cast(dict[str, Any], value)


def _schema(value: dict[str, Any], description: str) -> None:
    if (
        isinstance(value["schema_version"], bool)
        or not isinstance(value["schema_version"], int)
        or value["schema_version"] != 1
    ):
        raise _invalid(f"{description} schema_version must be integer 1")


def _safe_id(value: object, field: str) -> str:
    try:
        return validate_safe_id(value, field=field)
    except WorkflowError as error:
        raise _invalid(f"{field} must be a safe ID") from error


def _hash(value: object, field: str) -> str:
    try:
        return validate_sha256(value, field=field)
    except WorkflowError as error:
        raise _invalid(f"{field} must be a lowercase SHA-256") from error


def _text(value: object, field: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str):
        raise _invalid(f"{field} must be a string")
    normalized = unicodedata.normalize(
        "NFC", value.replace("\r\n", "\n").replace("\r", "\n")
    )
    if not normalized.strip():
        raise _invalid(f"{field} must be non-empty")
    return normalized


def _integer(value: object, field: str, *, positive: bool = False) -> int:
    minimum = 1 if positive else 0
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _invalid(f"{field} must be an integer >= {minimum}")
    return value


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise _invalid(f"{field} must be a boolean")
    return value


def _basis(value: object, *, kind: str) -> dict[str, str]:
    item = _closed(value, {"basis_id"}, "confirmation_basis")
    basis_id = item["basis_id"]
    if not isinstance(basis_id, str) or _BASIS_ID.fullmatch(basis_id) is None:
        raise _invalid("confirmation_basis.basis_id is invalid")
    if not basis_id.startswith(f"wfb_{kind}_"):
        raise _invalid(f"confirmation_basis must be a {kind} basis")
    return {"basis_id": basis_id}


def _artifact_ref(value: object, field: str) -> dict[str, object]:
    item = _closed(
        value, {"artifact_id", "schema_version", "content_hash"}, field
    )
    schema_version = _integer(item["schema_version"], f"{field}.schema_version", positive=True)
    return {
        "artifact_id": _safe_id(item["artifact_id"], f"{field}.artifact_id"),
        "schema_version": schema_version,
        "content_hash": _hash(item["content_hash"], f"{field}.content_hash"),
    }


def _subject_ref(value: object, field: str) -> dict[str, object]:
    item = _closed(
        value, {"kind", "artifact_id", "schema_version", "content_hash"}, field
    )
    if item["kind"] not in {"proposal", "decision"}:
        raise _invalid(f"{field}.kind must be proposal or decision")
    ref = _artifact_ref(
        {
            "artifact_id": item["artifact_id"],
            "schema_version": item["schema_version"],
            "content_hash": item["content_hash"],
        },
        field,
    )
    return {"kind": item["kind"], **ref}


def _binding(value: object, field: str) -> dict[str, str]:
    item = _closed(value, {"source_id", "transcript_version_id"}, field)
    return {
        "source_id": _safe_id(item["source_id"], f"{field}.source_id"),
        "transcript_version_id": _safe_id(
            item["transcript_version_id"], f"{field}.transcript_version_id"
        ),
    }


def _range_ref(value: object, field: str) -> dict[str, object]:
    item = _closed(
        value,
        {
            "source_id",
            "transcript_version_id",
            "segment_id",
            "start_ticks",
            "end_ticks",
        },
        field,
    )
    start = _integer(item["start_ticks"], f"{field}.start_ticks")
    end = _integer(item["end_ticks"], f"{field}.end_ticks", positive=True)
    if end <= start:
        raise _invalid(f"{field} must be a non-empty half-open range")
    return {
        "source_id": _safe_id(item["source_id"], f"{field}.source_id"),
        "transcript_version_id": _safe_id(
            item["transcript_version_id"], f"{field}.transcript_version_id"
        ),
        "segment_id": _safe_id(item["segment_id"], f"{field}.segment_id"),
        "start_ticks": start,
        "end_ticks": end,
    }


def _unique_ids(items: list[dict[str, Any]], key: str, field: str) -> None:
    ids = [cast(str, item[key]) for item in items]
    if len(ids) != len(set(ids)):
        raise _invalid(f"{field} must not contain duplicate {key} values")


def _approve_scope(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _invalid("ApproveScopeInputV1 must be an object")
    if any(not isinstance(key, str) for key in value):
        raise _invalid("ApproveScopeInputV1 field names must be strings")
    fields = set(value)
    legacy_fields = {"schema_version", "confirmation_basis", "source_authorizations"}
    setup_fields = legacy_fields | {"multicam_setup"}
    if fields == legacy_fields:
        item = _closed(value, legacy_fields, "ApproveScopeInputV1")
    elif fields == setup_fields:
        item = _closed(value, setup_fields, "ApproveScopeInputV1")
    else:
        raise _invalid(
            "ApproveScopeInputV1 fields are invalid; "
            f"missing={sorted(legacy_fields - fields)}, "
            f"extra={sorted(fields - legacy_fields)}"
        )
    _schema(item, "ApproveScopeInputV1")
    raw = item["source_authorizations"]
    if not isinstance(raw, list) or not raw:
        raise _invalid("source_authorizations must be a non-empty array")
    authorizations: list[dict[str, object]] = []
    for index, authorization in enumerate(raw):
        current = _closed(
            authorization,
            {"source_id", "transcribe", "speaker_diarization"},
            f"source_authorizations[{index}]",
        )
        transcribe = _boolean(
            current["transcribe"], f"source_authorizations[{index}].transcribe"
        )
        diarization = _boolean(
            current["speaker_diarization"],
            f"source_authorizations[{index}].speaker_diarization",
        )
        if diarization and not transcribe:
            raise _invalid("speaker_diarization=true requires transcribe=true")
        authorizations.append(
            {
                "source_id": _safe_id(
                    current["source_id"],
                    f"source_authorizations[{index}].source_id",
                ),
                "transcribe": transcribe,
                "speaker_diarization": diarization,
            }
        )
    _unique_ids(authorizations, "source_id", "source_authorizations")
    normalized: dict[str, object] = {
        "schema_version": 1,
        "confirmation_basis": _basis(item["confirmation_basis"], kind="scope"),
        "source_authorizations": authorizations,
    }
    if "multicam_setup" in item:
        try:
            normalized["multicam_setup"] = MulticamSetupDeclaration.from_dict(
                item["multicam_setup"]
            ).to_dict()
        except WorkflowError as error:
            raise _invalid(f"multicam_setup is invalid: {error}") from error
    return normalized


def _waiver(value: object, field: str) -> dict[str, str]:
    item = _closed(
        value,
        {"source_id", "transcript_version_id", "local_speaker_id"},
        field,
    )
    return {
        "source_id": _safe_id(item["source_id"], f"{field}.source_id"),
        "transcript_version_id": _safe_id(
            item["transcript_version_id"], f"{field}.transcript_version_id"
        ),
        "local_speaker_id": _safe_id(
            item["local_speaker_id"], f"{field}.local_speaker_id"
        ),
    }


def _confirm_brief(value: object) -> dict[str, object]:
    item = _closed(
        value,
        {
            "schema_version",
            "confirmation_basis",
            "theme",
            "target_duration_ticks",
            "focus",
            "allow_reorder",
            "speaker_resolution_waivers",
        },
        "ConfirmBriefInputV1",
    )
    _schema(item, "ConfirmBriefInputV1")
    focus = item["focus"]
    if not isinstance(focus, list) or not focus:
        raise _invalid("focus must be a non-empty array")
    parsed_focus = [_text(entry, f"focus[{index}]") for index, entry in enumerate(focus)]
    waivers = item["speaker_resolution_waivers"]
    if not isinstance(waivers, list):
        raise _invalid("speaker_resolution_waivers must be an array")
    parsed_waivers = [
        _waiver(entry, f"speaker_resolution_waivers[{index}]")
        for index, entry in enumerate(waivers)
    ]
    waiver_keys = [
        (entry["source_id"], entry["transcript_version_id"], entry["local_speaker_id"])
        for entry in parsed_waivers
    ]
    if len(waiver_keys) != len(set(waiver_keys)):
        raise _invalid("speaker_resolution_waivers must be unique")
    return {
        "schema_version": 1,
        "confirmation_basis": _basis(item["confirmation_basis"], kind="brief"),
        "theme": _text(item["theme"], "theme"),
        "target_duration_ticks": _integer(
            item["target_duration_ticks"], "target_duration_ticks", positive=True
        ),
        "focus": parsed_focus,
        "allow_reorder": _boolean(item["allow_reorder"], "allow_reorder"),
        "speaker_resolution_waivers": parsed_waivers,
    }


def _submit_outline(value: object) -> dict[str, object]:
    item = _closed(
        value,
        {
            "schema_version",
            "title",
            "opening",
            "sections",
            "ending",
            "required_content_coverage",
            "narration_status",
        },
        "SubmitOutlineInputV1",
    )
    _schema(item, "SubmitOutlineInputV1")
    sections = item["sections"]
    if not isinstance(sections, list) or not sections:
        raise _invalid("sections must be a non-empty array")
    parsed_sections: list[dict[str, object]] = []
    for index, section in enumerate(sections):
        current = _closed(
            section,
            {"section_id", "title", "summary", "target_duration_ticks"},
            f"sections[{index}]",
        )
        parsed_sections.append(
            {
                "section_id": _safe_id(
                    current["section_id"], f"sections[{index}].section_id"
                ),
                "title": _text(current["title"], f"sections[{index}].title"),
                "summary": _text(current["summary"], f"sections[{index}].summary"),
                "target_duration_ticks": _integer(
                    current["target_duration_ticks"],
                    f"sections[{index}].target_duration_ticks",
                    positive=True,
                ),
            }
        )
    _unique_ids(parsed_sections, "section_id", "sections")
    coverage = item["required_content_coverage"]
    if not isinstance(coverage, list):
        raise _invalid("required_content_coverage must be an array")
    parsed_coverage: list[dict[str, object]] = []
    for index, entry in enumerate(coverage):
        current = _closed(
            entry,
            {"requirement", "covered", "evidence_refs"},
            f"required_content_coverage[{index}]",
        )
        refs = current["evidence_refs"]
        if not isinstance(refs, list):
            raise _invalid(f"required_content_coverage[{index}].evidence_refs must be an array")
        parsed_refs = [
            _range_ref(ref, f"required_content_coverage[{index}].evidence_refs[{ref_index}]")
            for ref_index, ref in enumerate(refs)
        ]
        covered = _boolean(
            current["covered"], f"required_content_coverage[{index}].covered"
        )
        if covered != bool(parsed_refs):
            raise _invalid("covered must equal whether evidence_refs is non-empty")
        parsed_coverage.append(
            {
                "requirement": _text(
                    current["requirement"],
                    f"required_content_coverage[{index}].requirement",
                ),
                "covered": covered,
                "evidence_refs": parsed_refs,
            }
        )
    if item["narration_status"] not in {"none", "pending", "to_write", "recorded"}:
        raise _invalid("narration_status is unsupported")
    return {
        "schema_version": 1,
        "title": _text(item["title"], "title"),
        "opening": _text(item["opening"], "opening"),
        "sections": parsed_sections,
        "ending": _text(item["ending"], "ending"),
        "required_content_coverage": parsed_coverage,
        "narration_status": item["narration_status"],
    }


def _one_ref(value: object, *, description: str, field: str) -> dict[str, object]:
    item = _closed(value, {"schema_version", field}, description)
    _schema(item, description)
    return {"schema_version": 1, field: _artifact_ref(item[field], field)}


def _submit_draft(value: object) -> dict[str, object]:
    item = _closed(
        value,
        {
            "schema_version",
            "parent_draft_ref",
            "display_title",
            "source_bindings",
            "brief_ref",
            "context_hash",
            "blocks",
            "scoped_mutable_block_ids",
        },
        "SubmitDraftInputV1",
    )
    _schema(item, "SubmitDraftInputV1")
    display_title = _text(item["display_title"], "display_title", nullable=True)
    if display_title is not None and len(display_title) > 80:
        raise _invalid("display_title must contain at most 80 characters")
    bindings = item["source_bindings"]
    if not isinstance(bindings, list) or not bindings:
        raise _invalid("source_bindings must be a non-empty array")
    parsed_bindings = [
        _binding(binding, f"source_bindings[{index}]")
        for index, binding in enumerate(bindings)
    ]
    _unique_ids(parsed_bindings, "source_id", "source_bindings")
    blocks = item["blocks"]
    if not isinstance(blocks, list) or not blocks:
        raise _invalid("blocks must be a non-empty array")
    parsed_blocks: list[dict[str, object]] = []
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            raise _invalid(f"blocks[{index}] must be an object")
        kind = block.get("kind")
        if kind == "source_excerpt":
            allowed = {"block_id", "kind", "refs", "canonical_text"}
            if set(block) - allowed or not {"block_id", "kind", "refs"}.issubset(block):
                raise _invalid(f"blocks[{index}] fields are invalid")
            current = cast(dict[str, Any], block)
            refs = current["refs"]
            if not isinstance(refs, list) or not refs:
                raise _invalid(f"blocks[{index}].refs must be non-empty")
            parsed: dict[str, object] = {
                "block_id": _safe_id(current["block_id"], f"blocks[{index}].block_id"),
                "kind": "source_excerpt",
                "refs": [
                    _range_ref(ref, f"blocks[{index}].refs[{ref_index}]")
                    for ref_index, ref in enumerate(refs)
                ],
            }
            if "canonical_text" in current:
                parsed["canonical_text"] = _text(
                    current["canonical_text"], f"blocks[{index}].canonical_text"
                )
        elif kind == "narration":
            current = _closed(
                block,
                {"block_id", "kind", "text", "status", "recorded_refs"},
                f"blocks[{index}]",
            )
            refs = current["recorded_refs"]
            if not isinstance(refs, list):
                raise _invalid(f"blocks[{index}].recorded_refs must be an array")
            if current["status"] not in {"draft", "approved", "recorded"}:
                raise _invalid(f"blocks[{index}].status is unsupported")
            parsed_refs = [
                _range_ref(ref, f"blocks[{index}].recorded_refs[{ref_index}]")
                for ref_index, ref in enumerate(refs)
            ]
            if (current["status"] == "recorded") != bool(parsed_refs):
                raise _invalid("recorded narration status must match recorded_refs")
            parsed = {
                "block_id": _safe_id(current["block_id"], f"blocks[{index}].block_id"),
                "kind": "narration",
                "text": _text(current["text"], f"blocks[{index}].text"),
                "status": current["status"],
                "recorded_refs": parsed_refs,
            }
        elif kind == "section_title":
            current = _closed(
                block,
                {"block_id", "kind", "title"},
                f"blocks[{index}]",
            )
            title_value = _text(current["title"], f"blocks[{index}].title")
            assert title_value is not None
            section_title = title_value.strip()
            if not section_title or len(section_title) > 80:
                raise _invalid(
                    f"blocks[{index}].title must contain 1-80 Unicode code points"
                )
            parsed = {
                "block_id": _safe_id(current["block_id"], f"blocks[{index}].block_id"),
                "kind": "section_title",
                "title": section_title,
            }
        else:
            raise _invalid(f"blocks[{index}].kind is unsupported")
        parsed_blocks.append(parsed)
    _unique_ids(parsed_blocks, "block_id", "blocks")
    mutable = item["scoped_mutable_block_ids"]
    if not isinstance(mutable, list):
        raise _invalid("scoped_mutable_block_ids must be an array")
    parsed_mutable = [
        _safe_id(block_id, f"scoped_mutable_block_ids[{index}]")
        for index, block_id in enumerate(mutable)
    ]
    if len(parsed_mutable) != len(set(parsed_mutable)):
        raise _invalid("scoped_mutable_block_ids must be unique")
    return {
        "schema_version": 1,
        "parent_draft_ref": (
            None
            if item["parent_draft_ref"] is None
            else _artifact_ref(item["parent_draft_ref"], "parent_draft_ref")
        ),
        "display_title": display_title,
        "source_bindings": parsed_bindings,
        "brief_ref": _artifact_ref(item["brief_ref"], "brief_ref"),
        "context_hash": _hash(item["context_hash"], "context_hash"),
        "blocks": parsed_blocks,
        "scoped_mutable_block_ids": parsed_mutable,
    }


def _return_to_draft(value: object) -> dict[str, object]:
    item = _closed(
        value,
        {"schema_version", "current_subject_ref", "confirmed_content_draft_ref"},
        "ReturnToDraftInputV1",
    )
    _schema(item, "ReturnToDraftInputV1")
    return {
        "schema_version": 1,
        "current_subject_ref": _subject_ref(
            item["current_subject_ref"], "current_subject_ref"
        ),
        "confirmed_content_draft_ref": _artifact_ref(
            item["confirmed_content_draft_ref"], "confirmed_content_draft_ref"
        ),
    }


def parse_workflow_action_input(action: object, value: object) -> dict[str, object]:
    """Validate and normalize exactly one frozen workflow action input."""

    if not isinstance(action, str) or action not in WORKFLOW_ACTIONS:
        raise _invalid("action must be one of the nine frozen business actions")
    if action == "approve_scope":
        parsed = _approve_scope(value)
    elif action == "confirm_brief":
        parsed = _confirm_brief(value)
    elif action == "submit_outline":
        parsed = _submit_outline(value)
    elif action == "approve_outline":
        parsed = _one_ref(
            value, description="ApproveOutlineInputV1", field="outline_ref"
        )
    elif action == "submit_draft":
        parsed = _submit_draft(value)
    elif action == "approve_draft":
        parsed = _one_ref(
            value, description="ApproveDraftInputV1", field="content_draft_ref"
        )
    elif action == "return_to_draft":
        parsed = _return_to_draft(value)
    elif action == "adopt_roughcut":
        parsed = _one_ref(
            value, description="AdoptRoughcutInputV1", field="proposal_ref"
        )
    else:
        assert action == "approve_export"
        parsed = _one_ref(
            value, description="ApproveExportInputV1", field="export_ref"
        )
    canonical_json_v1(parsed)
    return parsed
