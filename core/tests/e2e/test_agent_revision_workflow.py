from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from roughcut.adapters.artifact_store import write_new_json
from roughcut.adapters.project_store import ProjectStore
from roughcut.application.projects import create_project
from roughcut.domain.bindings import SourceTranscriptBinding
from roughcut.domain.brief import EditBrief
from roughcut.domain.edit import (
    EditClip,
    EditDecision,
    EditProposal,
    MultiSourceEditDecision,
    MultiSourceEditProposal,
)
from roughcut.domain.people import Person, SpeakerMap
from roughcut.domain.project import ImportMode, MediaProbe, SourceAsset, SourceFingerprint
from roughcut.domain.transcript import (
    TimedTranscript,
    TranscriptProvenance,
    TranscriptSegment,
)

TICKS_PER_SEGMENT = 120_000


def _run_cli(*arguments: str, expected_exit: int = 0) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-m", "roughcut.cli", *arguments, "--json"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == expected_exit, completed.stderr
    assert completed.stderr == ""
    payload = json.loads(completed.stdout)
    assert isinstance(payload, dict)
    return payload


def _run_mcp(
    tool_name: str,
    arguments: dict[str, object],
    *,
    expect_error: bool = False,
) -> dict[str, Any]:
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    completed = subprocess.run(
        [sys.executable, "-m", "roughcut.mcp"],
        input=json.dumps(request, ensure_ascii=False) + "\n",
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    response = json.loads(completed.stdout)
    result = response["result"]
    assert result.get("isError", False) is expect_error, json.dumps(
        result["structuredContent"], ensure_ascii=False
    )
    payload = result["structuredContent"]
    assert isinstance(payload, dict)
    return payload


def _source(source_id: str, index: int) -> SourceAsset:
    return SourceAsset(
        source_id=source_id,
        kind="audio",
        display_name=f"Fixture {source_id}",
        import_mode=ImportMode.LINKED,
        locator={"absolute_path": f"/fixture/not-read/{source_id}.wav"},
        fingerprint=SourceFingerprint(
            size=100 + index,
            mtime_ns=index,
            sha256_head_tail=f"fixture-{index}",
        ),
        probe=MediaProbe(
            duration_ticks=1_200_000,
            container_start_ticks=0,
            first_content_ticks=0,
            video_codec=None,
            width=None,
            height=None,
            nominal_frame_rate=None,
            is_vfr=False,
            audio_codec="fixture",
            audio_sample_rate=16_000,
            rotation_degrees=0,
        ),
        tags=("fixture", source_id),
        note=f"Safe note for {source_id}",
    )


def _transcript(source_id: str, transcript_id: str) -> TimedTranscript:
    return TimedTranscript(
        schema_version=1,
        transcript_version_id=transcript_id,
        source_id=source_id,
        parent_version_id=None,
        provenance=TranscriptProvenance(
            backend="fixture",
            package_version="fixture",
            models={},
            parameters={},
            raw_result_path=f"raw-asr/{source_id}/fixture.json",
            started_at="fixture",
            completed_at="fixture",
            exit_status=0,
        ),
        language="zh-CN",
        segments=tuple(
            TranscriptSegment(
                segment_id=f"seg_{index}",
                start_ticks=(index - 1) * TICKS_PER_SEGMENT,
                end_ticks=index * TICKS_PER_SEGMENT,
                original_text=f"{source_id} topic {index}",
                corrected_text=None,
                local_speaker_id="spk_0",
                person_id=None,
                confidence=None,
                fine_units=(),
                editorial_mark="unmarked",
            )
            for index in range(1, 7)
        ),
    )


def _clip(source_id: str, transcript_id: str, index: int, clip_id: str) -> EditClip:
    return EditClip(
        clip_id=clip_id,
        source_id=source_id,
        transcript_version_id=transcript_id,
        segment_id=f"seg_{index}",
        source_in_ticks=(index - 1) * TICKS_PER_SEGMENT,
        source_out_ticks=index * TICKS_PER_SEGMENT,
        reason=f"Keep {source_id} topic {index}",
        display_text=f"{source_id} topic {index}",
    )


def _workflow_clip_id(source_id: str, transcript_id: str, index: int) -> str:
    identity = "\0".join(
        (source_id, transcript_id, f"seg_{index}")
    ).encode("utf-8")
    return f"clip_ref_{hashlib.sha256(identity).hexdigest()[:20]}_1"


def _seed_project(tmp_path: Path, *, schema_version: int) -> Path:
    root = tmp_path / f"schema-{schema_version}"
    project = create_project(root, f"Revision acceptance schema {schema_version}")
    sources = (_source("src_a", 1), _source("src_b", 2))
    transcripts = (_transcript("src_a", "tr_a"), _transcript("src_b", "tr_b"))
    for transcript in transcripts:
        write_new_json(
            root
            / "transcripts"
            / transcript.source_id
            / f"{transcript.transcript_version_id}.json",
            transcript.to_dict(),
        )
    brief = EditBrief(
        brief_id="brief_revision_acceptance",
        theme="Fixture revision",
        target_duration_ticks=1_200_000,
        focus=("deterministic",),
        allow_reorder=True,
    )
    write_new_json(root / "briefs" / f"{brief.brief_id}.json", brief.to_dict())

    base_id = f"edit_schema_{schema_version}_base"
    redo_id = f"edit_schema_{schema_version}_redo"
    if schema_version == 1:
        base_clips = tuple(
            _clip(
                "src_a",
                "tr_a",
                index,
                _workflow_clip_id("src_a", "tr_a", index),
            )
            for index in range(1, 5)
        )
        proposal: EditProposal | MultiSourceEditProposal = EditProposal(
            proposal_id="proposal_schema_1_seed",
            base_project_revision=1,
            base_edit_version_id=None,
            source_id="src_a",
            transcript_version_id="tr_a",
            brief_snapshot=brief,
            context_hash="0" * 64,
            clips=base_clips,
            total_duration_ticks=sum(clip.duration_ticks for clip in base_clips),
            created_at="fixture",
        )
        base: EditDecision | MultiSourceEditDecision = EditDecision(
            edit_version_id=base_id,
            proposal_snapshot=proposal,
            project_revision=2,
            created_at="fixture",
        )
        redo_proposal = replace(
            proposal,
            proposal_id="proposal_schema_1_redo",
            base_project_revision=2,
            base_edit_version_id=base_id,
        )
        redo: EditDecision | MultiSourceEditDecision = EditDecision(
            edit_version_id=redo_id,
            proposal_snapshot=redo_proposal,
            project_revision=3,
            created_at="fixture",
        )
    else:
        bindings = (
            SourceTranscriptBinding("src_a", "tr_a"),
            SourceTranscriptBinding("src_b", "tr_b"),
        )
        base_clips = (
            _clip("src_a", "tr_a", 1, _workflow_clip_id("src_a", "tr_a", 1)),
            _clip("src_a", "tr_a", 2, _workflow_clip_id("src_a", "tr_a", 2)),
            _clip("src_b", "tr_b", 1, _workflow_clip_id("src_b", "tr_b", 1)),
            _clip("src_b", "tr_b", 2, _workflow_clip_id("src_b", "tr_b", 2)),
            _clip("src_a", "tr_a", 3, _workflow_clip_id("src_a", "tr_a", 3)),
            _clip("src_b", "tr_b", 3, _workflow_clip_id("src_b", "tr_b", 3)),
        )
        proposal = MultiSourceEditProposal(
            proposal_id="proposal_schema_2_seed",
            base_project_revision=1,
            base_edit_version_id=None,
            source_bindings=bindings,
            brief_snapshot=brief,
            context_hash="0" * 64,
            clips=base_clips,
            total_duration_ticks=sum(clip.duration_ticks for clip in base_clips),
            created_at="fixture",
        )
        base = MultiSourceEditDecision(
            edit_version_id=base_id,
            proposal_snapshot=proposal,
            project_revision=2,
            created_at="fixture",
        )
        redo_proposal = replace(
            proposal,
            proposal_id="proposal_schema_2_redo",
            base_project_revision=2,
            base_edit_version_id=base_id,
        )
        redo = MultiSourceEditDecision(
            edit_version_id=redo_id,
            proposal_snapshot=redo_proposal,
            project_revision=3,
            created_at="fixture",
        )

    write_new_json(root / "edits" / f"{base_id}.json", base.to_dict())
    write_new_json(root / "edits" / f"{redo_id}.json", redo.to_dict())
    seeded = replace(
        project,
        revision=4,
        sources=sources,
        active_transcript_versions={"src_a": "tr_a", "src_b": "tr_b"},
        active_brief_id=brief.brief_id,
        active_edit_version_id=base_id,
        persons=(
            Person("person_a", "Person A", "guest", ""),
            Person("person_b", "Person B", "guest", ""),
        ),
        speaker_maps=(
            SpeakerMap("src_a", "tr_a", "spk_0", "person_a", True),
            SpeakerMap("src_b", "tr_b", "spk_0", "person_b", True),
        ),
        edit_redo_stack=(redo_id,),
    )
    ProjectStore(root).save(seeded, expected_revision=0)
    return root


def _duration(clips: list[dict[str, Any]]) -> int:
    return sum(
        int(clip["source_out_ticks"]) - int(clip["source_in_ticks"])
        for clip in clips
    )


def _cli_revision_pages(
    root: Path, revision: int, *, limit: int
) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    offset = 0
    while True:
        payload = _run_cli(
            "revision-context",
            "--project",
            str(root),
            "--expected-revision",
            str(revision),
            "--offset",
            str(offset),
            "--limit",
            str(limit),
        )
        page = payload["revision_context"]
        pages.append(page)
        next_offset = page["next_offset"]
        if next_offset is None:
            return pages
        offset = int(next_offset)


def _mcp_revision_pages(
    root: Path, revision: int, *, limit: int
) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    offset = 0
    while True:
        payload = _run_mcp(
            "revision_context",
            {
                "project_path": str(root),
                "expected_revision": revision,
                "offset": offset,
                "limit": limit,
            },
        )
        page = payload["revision_context"]
        pages.append(page)
        next_offset = page["next_offset"]
        if next_offset is None:
            return pages
        offset = int(next_offset)


def _assert_pages_share_frozen_context(pages: list[dict[str, Any]]) -> None:
    frozen_keys = (
        "project_revision",
        "base_edit_version_id",
        "edit_schema_version",
        "source_bindings",
        "brief",
        "context_hash",
        "base_clips",
        "base_total_duration_ticks",
    )
    first = pages[0]
    assert len(pages) > 1
    for page in pages[1:]:
        assert {key: page[key] for key in frozen_keys} == {
            key: first[key] for key in frozen_keys
        }


def _assert_candidate_was_presented(
    proposal: dict[str, Any], proposal_diff: dict[str, Any]
) -> None:
    clips = proposal["clips"]
    assert proposal_diff["after_order"] == [clip["clip_id"] for clip in clips]
    assert proposal_diff["after_clip_count"] == len(clips)
    assert proposal_diff["after_total_duration_ticks"] == proposal["total_duration_ticks"]
    assert proposal["total_duration_ticks"] == _duration(clips)
    for clip in clips:
        assert {
            "clip_id",
            "source_id",
            "source_in_ticks",
            "source_out_ticks",
            "reason",
        } <= clip.keys()


def _cli_workflow_action(
    root: Path,
    action_id: str,
    action: str,
    action_input: dict[str, object],
) -> dict[str, Any]:
    return _run_cli(
        "workflow-action",
        "--project",
        str(root),
        "--run-id",
        "wfr_agent_e2e",
        "--action-id",
        action_id,
        "--action",
        action,
        "--input-json",
        json.dumps(action_input, ensure_ascii=False),
    )


def _mcp_workflow_action(
    root: Path,
    action_id: str,
    action: str,
    action_input: dict[str, object],
    *,
    expect_error: bool = False,
) -> dict[str, Any]:
    return _run_mcp(
        "workflow_action",
        {
            "project_path": str(root),
            "run_id": "wfr_agent_e2e",
            "action_id": action_id,
            "action": action,
            "input": action_input,
        },
        expect_error=expect_error,
    )


def _outline_input() -> dict[str, object]:
    return {
        "schema_version": 1,
        "title": "Fixture revision",
        "opening": "Opening",
        "sections": [
            {
                "section_id": f"section_{index}",
                "title": title,
                "summary": title,
                "target_duration_ticks": 300_000,
            }
            for index, title in enumerate(
                ("Opening", "Context", "Details", "Ending"), start=1
            )
        ],
        "ending": "Ending",
        "required_content_coverage": [],
        "narration_status": "none",
    }


def _bootstrap_workflow(
    root: Path,
    *,
    source_ids: list[str],
    surface: str,
) -> dict[str, Any]:
    if surface == "cli":
        started = _run_cli(
            "workflow-start",
            "--project",
            str(root),
            "--run-id",
            "wfr_agent_e2e",
            "--ordered-source-ids-json",
            json.dumps(source_ids),
        )
        def action(
            action_id: str, name: str, value: dict[str, object]
        ) -> dict[str, Any]:
            return _cli_workflow_action(root, action_id, name, value)
    else:
        started = _run_mcp(
            "workflow_start",
            {
                "project_path": str(root),
                "run_id": "wfr_agent_e2e",
                "ordered_source_ids": source_ids,
            },
        )
        def action(
            action_id: str, name: str, value: dict[str, object]
        ) -> dict[str, Any]:
            return _mcp_workflow_action(root, action_id, name, value)
    scope_basis = started["status"]["confirmation_bases"]["scope"]["basis"]
    scoped = action(
        "act_e2e_scope",
        "approve_scope",
        {
            "schema_version": 1,
            "confirmation_basis": scope_basis,
            "source_authorizations": [
                {
                    "source_id": source_id,
                    "transcribe": False,
                    "speaker_diarization": False,
                }
                for source_id in source_ids
            ],
        },
    )
    brief_basis = scoped["status"]["confirmation_bases"]["brief"]["basis"]
    briefed = action(
        "act_e2e_brief",
        "confirm_brief",
        {
            "schema_version": 1,
            "confirmation_basis": brief_basis,
            "theme": "Fixture revision",
            "target_duration_ticks": 1_200_000,
            "focus": ["deterministic"],
            "allow_reorder": True,
            "speaker_resolution_waivers": [],
        },
    )
    outlined = action("act_e2e_outline", "submit_outline", _outline_input())
    outline_ref = outlined["status"]["presented_subjects"]["outline_ref"]
    approved = action(
        "act_e2e_outline_approve",
        "approve_outline",
        {"schema_version": 1, "outline_ref": outline_ref},
    )
    assert briefed["receipt"]["action"] == "confirm_brief"
    return approved


def _draft_blocks(clips: list[dict[str, Any]]) -> list[dict[str, object]]:
    return [
        {
            "block_id": f"block_{clip['clip_id']}",
            "kind": "source_excerpt",
            "refs": [
                {
                    "source_id": clip["source_id"],
                    "transcript_version_id": clip["transcript_version_id"],
                    "segment_id": clip["segment_id"],
                    "start_ticks": clip["source_in_ticks"],
                    "end_ticks": clip["source_out_ticks"],
                }
            ],
            "canonical_text": clip["display_text"],
        }
        for clip in clips
    ]


def _workflow_round(
    root: Path,
    *,
    surface: str,
    round_name: str,
    clips: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    action = _cli_workflow_action if surface == "cli" else _mcp_workflow_action
    status_payload = (
        _run_cli(
            "workflow-status",
            "--project",
            str(root),
            "--run-id",
            "wfr_agent_e2e",
        )
        if surface == "cli"
        else _run_mcp(
            "workflow_status",
            {"project_path": str(root), "run_id": "wfr_agent_e2e"},
        )
    )
    status = status_payload["status"]
    if status["workflow_run"]["stage"] == "export_review":
        status_payload = action(
            root,
            f"act_{round_name}_return",
            "return_to_draft",
            {
                "schema_version": 1,
                "current_subject_ref": status["presented_subjects"][
                    "return_subject_ref"
                ],
                "confirmed_content_draft_ref": status["presented_subjects"][
                    "confirmed_content_draft_ref"
                ],
            },
        )
        status = status_payload["status"]
    revision = ProjectStore(root).load().revision
    context = (
        _cli_revision_pages(root, revision, limit=3)[0]
        if surface == "cli"
        else _mcp_revision_pages(root, revision, limit=3)[0]
    )
    anchor = status["presented_subjects"]["draft_anchor_ref"]
    blocks = _draft_blocks(clips)
    mutable_ids: list[str] = []
    if anchor is not None:
        parent = json.loads(
            (
                root
                / "content-drafts"
                / f"{anchor['artifact_id']}.json"
            ).read_text(encoding="utf-8")
        )
        mutable_ids = [block["block_id"] for block in parent["blocks"]]
    submitted = action(
        root,
        f"act_{round_name}_submit",
        "submit_draft",
        {
            "schema_version": 1,
            "parent_draft_ref": anchor,
            "display_title": f"Round {round_name}",
            "source_bindings": context["source_bindings"],
            "brief_ref": status["workflow_run"]["artifact_refs"]["brief"],
            "context_hash": context["context_hash"],
            "blocks": blocks,
            "scoped_mutable_block_ids": mutable_ids,
        },
    )
    mutation = submitted["receipt"]["mutation"]
    approved = action(
        root,
        f"act_{round_name}_approve",
        "approve_draft",
        {
            "schema_version": 1,
            "content_draft_ref": {
                "artifact_id": mutation["artifact_id"],
                "schema_version": mutation["schema_version"],
                "content_hash": mutation["content_hash"],
            },
        },
    )
    proposal_ref = approved["status"]["presented_subjects"]["proposal_ref"]
    proposal = json.loads(
        (
            root / "proposals" / f"{proposal_ref['artifact_id']}.json"
        ).read_text(encoding="utf-8")
    )
    diff = (
        _run_cli(
            "proposal-diff-read",
            "--project",
            str(root),
            "--proposal-id",
            proposal["proposal_id"],
            "--expected-revision",
            str(ProjectStore(root).load().revision),
        )
        if surface == "cli"
        else _run_mcp(
            "proposal_diff_read",
            {
                "project_path": str(root),
                "proposal_id": proposal["proposal_id"],
                "expected_revision": ProjectStore(root).load().revision,
            },
        )
    )["proposal_diff"]
    _assert_candidate_was_presented(proposal, diff)
    adopted = action(
        root,
        f"act_{round_name}_adopt",
        "adopt_roughcut",
        {"schema_version": 1, "proposal_ref": proposal_ref},
    )
    return adopted, diff


def test_stage_five_public_mcp_reordered_multisource_binding_is_zero_write(
    tmp_path: Path,
) -> None:
    root = _seed_project(tmp_path, schema_version=2)
    approved = _bootstrap_workflow(
        root,
        source_ids=["src_a", "src_b"],
        surface="mcp",
    )
    revision = ProjectStore(root).load().revision
    context = _mcp_revision_pages(root, revision, limit=3)[0]
    before = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and path.name not in {".project.lock", ".claim.lock"}
    }
    rejected = _mcp_workflow_action(
        root,
        "act_reordered_binding",
        "submit_draft",
        {
            "schema_version": 1,
            "parent_draft_ref": None,
            "display_title": "Wrong binding order",
            "source_bindings": list(reversed(context["source_bindings"])),
            "brief_ref": approved["workflow_run"]["artifact_refs"]["brief"],
            "context_hash": context["context_hash"],
            "blocks": _draft_blocks(context["base_clips"]),
            "scoped_mutable_block_ids": [],
        },
        expect_error=True,
    )
    assert rejected["error"] == {"code": "workflow_subject_mismatch"}
    after = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and path.name not in {".project.lock", ".claim.lock"}
    }
    assert after == before


def test_schema_one_public_cli_semantic_compression_and_confirmation(
    tmp_path: Path,
) -> None:
    root = _seed_project(tmp_path, schema_version=1)
    initial_pages = _cli_revision_pages(root, 4, limit=2)
    _assert_pages_share_frozen_context(initial_pages)
    assert [
        segment["segment_id"]
        for page in initial_pages
        for segment in page["segments"]
    ] == [f"seg_{index}" for index in range(1, 7)]
    _bootstrap_workflow(root, source_ids=["src_a"], surface="cli")
    pages = _cli_revision_pages(root, 5, limit=2)
    _assert_pages_share_frozen_context(pages)
    assert [segment["segment_id"] for page in pages for segment in page["segments"]] == [
        f"seg_{index}" for index in range(1, 7)
    ]
    context = pages[0]
    removed_id = _workflow_clip_id("src_a", "tr_a", 2)
    clips = [
        dict(clip)
        for clip in context["base_clips"]
        if clip["clip_id"] != removed_id
    ]
    clips[0]["reason"] = "Compress the opening"

    project_before = (root / "project.json").read_bytes()
    old_edit = root / "edits" / f"{context['base_edit_version_id']}.json"
    old_edit_before = old_edit.read_bytes()
    redo_before = ProjectStore(root).load().edit_redo_stack
    adopted, proposal_diff = _workflow_round(
        root,
        surface="cli",
        round_name="schema1",
        clips=clips,
    )
    assert proposal_diff["before_order"] == [
        clip["clip_id"] for clip in context["base_clips"]
    ]
    assert proposal_diff["before_clip_count"] == len(context["base_clips"])
    assert proposal_diff["before_total_duration_ticks"] == context[
        "base_total_duration_ticks"
    ]
    assert [item["clip_id"] for item in proposal_diff["removed"]] == [removed_id]
    assert proposal_diff["changed"][0]["changed_fields"] == ["reason"]
    assert project_before != (root / "project.json").read_bytes()
    assert redo_before
    decision_ref = adopted["workflow_run"]["artifact_refs"]["decision"]
    readback = _run_cli(
        "edit-decision-read",
        "--project",
        str(root),
        "--edit-version-id",
        decision_ref["artifact_id"],
    )["decision"]
    assert adopted["receipt"]["after"]["project_revision"] == 7
    assert readback["proposal_snapshot"]["base_edit_version_id"] == context[
        "base_edit_version_id"
    ]
    assert [
        {
            key: clip[key]
            for key in (
                "clip_id",
                "source_id",
                "transcript_version_id",
                "segment_id",
                "source_in_ticks",
                "source_out_ticks",
                "display_text",
            )
        }
        for clip in readback["proposal_snapshot"]["clips"]
    ] == [
        {
            key: clip[key]
            for key in (
                "clip_id",
                "source_id",
                "transcript_version_id",
                "segment_id",
                "source_in_ticks",
                "source_out_ticks",
                "display_text",
            )
        }
        for clip in clips
    ]
    assert old_edit.read_bytes() == old_edit_before
    assert ProjectStore(root).load().edit_redo_stack == ()
    next_context = _cli_revision_pages(root, 7, limit=2)[0]
    assert next_context["base_edit_version_id"] == decision_ref["artifact_id"]


def _run_schema_two_round(
    root: Path,
    *,
    round_name: str,
    clips: list[dict[str, Any]],
) -> tuple[int, dict[str, Any], dict[str, Any]]:
    revision = ProjectStore(root).load().revision
    context = _mcp_revision_pages(root, revision, limit=3)[0]
    project_before = (root / "project.json").read_bytes()
    project_state = ProjectStore(root).load()
    old_edit_id = str(context["base_edit_version_id"])
    old_edit = root / "edits" / f"{old_edit_id}.json"
    old_edit_before = old_edit.read_bytes()
    adopted, proposal_diff = _workflow_round(
        root,
        surface="mcp",
        round_name=round_name,
        clips=clips,
    )
    proposal_ref = adopted["workflow_run"]["artifact_refs"]["proposal"]
    proposal = json.loads(
        (
            root / "proposals" / f"{proposal_ref['artifact_id']}.json"
        ).read_text(encoding="utf-8")
    )
    assert proposal["base_edit_version_id"] == old_edit_id
    assert proposal["source_bindings"] == context["source_bindings"]
    assert proposal_diff["before_order"] == [
        clip["clip_id"] for clip in context["base_clips"]
    ]
    assert proposal_diff["before_clip_count"] == len(context["base_clips"])
    assert proposal_diff["before_total_duration_ticks"] == context[
        "base_total_duration_ticks"
    ]
    assert (root / "project.json").read_bytes() != project_before
    decision_ref = adopted["workflow_run"]["artifact_refs"]["decision"]
    readback = _run_mcp(
        "multi_source_edit_decision_read",
        {
            "project_path": str(root),
            "edit_version_id": decision_ref["artifact_id"],
        },
    )["decision"]
    assert adopted["receipt"]["after"]["project_revision"] == revision + 2
    assert readback["proposal_snapshot"]["base_edit_version_id"] == old_edit_id
    assert readback["proposal_snapshot"]["source_bindings"] == context["source_bindings"]
    assert readback["proposal_snapshot"]["clips"] == proposal["clips"]
    assert readback["proposal_snapshot"]["total_duration_ticks"] == _duration(
        proposal["clips"]
    )
    assert old_edit.read_bytes() == old_edit_before
    assert project_state.active_edit_version_id == old_edit_id
    assert ProjectStore(root).load().edit_redo_stack == ()
    next_revision = revision + 2
    next_context = _mcp_revision_pages(root, next_revision, limit=3)[0]
    assert next_context["base_edit_version_id"] == decision_ref["artifact_id"]
    assert next_context["base_clips"] == proposal["clips"]
    return next_revision, next_context, proposal_diff


def test_schema_two_public_mcp_three_rounds_and_stale_reread(tmp_path: Path) -> None:
    root = _seed_project(tmp_path, schema_version=2)
    initial_pages = _mcp_revision_pages(root, 4, limit=3)
    _assert_pages_share_frozen_context(initial_pages)
    initial = initial_pages[0]
    assert initial["brief"]["allow_reorder"] is True
    _bootstrap_workflow(root, source_ids=["src_a", "src_b"], surface="mcp")
    initial = _mcp_revision_pages(root, 5, limit=3)[0]

    removed_ids = {
        _workflow_clip_id("src_a", "tr_a", 2),
        _workflow_clip_id("src_b", "tr_b", 2),
    }
    compressed = [
        dict(clip)
        for clip in initial["base_clips"]
        if clip["clip_id"] not in removed_ids
    ]
    revision, compressed_context, compression_diff = _run_schema_two_round(
        root,
        round_name="compress",
        clips=compressed,
    )
    assert revision == 7
    assert {item["clip_id"] for item in compression_diff["removed"]} == removed_ids

    recovered_id = _workflow_clip_id("src_a", "tr_a", 4)
    recovered_clip = _clip("src_a", "tr_a", 4, recovered_id).to_dict()
    recovered = [
        dict(compressed_context["base_clips"][0]),
        dict(compressed_context["base_clips"][1]),
        recovered_clip,
        *[dict(clip) for clip in compressed_context["base_clips"][2:]],
    ]
    revision, recovered_context, recovery_diff = _run_schema_two_round(
        root,
        round_name="recover",
        clips=recovered,
    )
    assert revision == 9
    assert [item["clip_id"] for item in recovery_diff["added"]] == [recovered_id]
    assert recovered_clip["segment_id"] == "seg_4"

    by_id = {clip["clip_id"]: dict(clip) for clip in recovered_context["base_clips"]}
    reorganized = [
        by_id[_workflow_clip_id("src_a", "tr_a", 1)],
        by_id[_workflow_clip_id("src_b", "tr_b", 1)],
        by_id[_workflow_clip_id("src_b", "tr_b", 3)],
        by_id[recovered_id],
        by_id[_workflow_clip_id("src_a", "tr_a", 3)],
    ]
    revision, current_context, reorganization_diff = _run_schema_two_round(
        root,
        round_name="reorganize",
        clips=reorganized,
    )
    assert revision == 11
    assert reorganization_diff["order_changed"] is True
    assert [clip["source_id"] for clip in reorganized] == [
        "src_a",
        "src_b",
        "src_b",
        "src_a",
        "src_a",
    ]

    pending = [dict(clip) for clip in current_context["base_clips"]]
    pending[0]["reason"] = "Pending stale proposal"
    status = _run_mcp(
        "workflow_status",
        {"project_path": str(root), "run_id": "wfr_agent_e2e"},
    )["status"]
    returned = _mcp_workflow_action(
        root,
        "act_pending_return",
        "return_to_draft",
        {
            "schema_version": 1,
            "current_subject_ref": status["presented_subjects"]["return_subject_ref"],
            "confirmed_content_draft_ref": status["presented_subjects"][
                "confirmed_content_draft_ref"
            ],
        },
    )
    blocks = _draft_blocks(pending)
    anchor = returned["status"]["presented_subjects"]["draft_anchor_ref"]
    parent = json.loads(
        (
            root / "content-drafts" / f"{anchor['artifact_id']}.json"
        ).read_text(encoding="utf-8")
    )
    submitted = _mcp_workflow_action(
        root,
        "act_pending_submit",
        "submit_draft",
        {
            "schema_version": 1,
            "parent_draft_ref": anchor,
            "display_title": "Pending stale proposal",
            "source_bindings": current_context["source_bindings"],
            "brief_ref": returned["workflow_run"]["artifact_refs"]["brief"],
            "context_hash": current_context["context_hash"],
            "blocks": blocks,
            "scoped_mutable_block_ids": [
                block["block_id"] for block in parent["blocks"]
            ],
        },
    )
    draft_mutation = submitted["receipt"]["mutation"]
    approved = _mcp_workflow_action(
        root,
        "act_pending_approve",
        "approve_draft",
        {
            "schema_version": 1,
            "content_draft_ref": {
                "artifact_id": draft_mutation["artifact_id"],
                "schema_version": draft_mutation["schema_version"],
                "content_hash": draft_mutation["content_hash"],
            },
        },
    )
    pending_proposal_ref = approved["status"]["presented_subjects"]["proposal_ref"]
    pending_proposal_id = pending_proposal_ref["artifact_id"]
    _run_mcp(
        "proposal_diff_read",
        {
            "project_path": str(root),
            "proposal_id": pending_proposal_id,
            "expected_revision": 12,
        },
    )
    active_before_conflict = ProjectStore(root).load().active_edit_version_id
    edits_before_conflict = {path.name for path in (root / "edits").glob("*.json")}
    mutation = _run_mcp(
        "source_metadata_update",
        {
            "project_path": str(root),
            "source_id": "src_a",
            "tags": ["fixture", "conflict"],
            "note": "Public-tool conflict mutation",
            "expected_revision": 12,
        },
    )
    assert mutation["people"]["project_revision"] == 13

    stale_diff = _run_mcp(
        "proposal_diff_read",
        {
            "project_path": str(root),
            "proposal_id": pending_proposal_id,
            "expected_revision": 13,
        },
        expect_error=True,
    )
    stale_confirm = _mcp_workflow_action(
        root,
        "act_pending_adopt",
        "adopt_roughcut",
        {"schema_version": 1, "proposal_ref": pending_proposal_ref},
        expect_error=True,
    )
    assert stale_diff["error"] == {"code": "proposal_diff_failed"}
    assert stale_confirm["error"] == {"code": "workflow_stale"}
    after_conflict = ProjectStore(root).load()
    assert after_conflict.active_edit_version_id == active_before_conflict
    assert {path.name for path in (root / "edits").glob("*.json")} == edits_before_conflict

    refreshed = _mcp_revision_pages(root, 13, limit=3)[0]
    assert refreshed["project_revision"] == 13
    assert refreshed["base_edit_version_id"] == active_before_conflict
    assert refreshed["context_hash"] != current_context["context_hash"]
    assert refreshed["base_clips"] == current_context["base_clips"]
