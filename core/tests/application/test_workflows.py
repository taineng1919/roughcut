from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import tracemalloc
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path, PureWindowsPath
from typing import Any

import pytest

import roughcut.adapters.workflow_candidates as workflow_candidates_module
import roughcut.application.media_operations as media_operations_module
import roughcut.application.workflows as workflows_module
from roughcut.adapters.artifact_store import write_new_json
from roughcut.adapters.ffmpeg.render import FFmpegRenderError
from roughcut.adapters.media_operation_store import MediaOperationStore
from roughcut.adapters.project_lock import project_export_claim
from roughcut.adapters.project_store import ProjectStore
from roughcut.adapters.workflow_candidates import (
    stream_file_sha256,
    workflow_export_staging_id,
    workflow_renderer_workspace_name,
)
from roughcut.adapters.workflow_store import WorkflowStore
from roughcut.application.agent_context import calculate_agent_context_hash
from roughcut.application.media_operations import (
    media_operation_status,
    run_approve_export_operation,
)
from roughcut.application.projects import create_project
from roughcut.application.protected_writes import protected_write
from roughcut.application.renders import RenderResult
from roughcut.application.workflows import (
    synchronize_workflow_transcript_binding,
    workflow_action,
    workflow_cancel,
    workflow_start,
    workflow_status,
)
from roughcut.cli import main as cli_main
from roughcut.domain.bindings import SourceTranscriptBinding
from roughcut.domain.brief import EditBrief
from roughcut.domain.edit import EditProposal, MultiSourceEditProposal
from roughcut.domain.errors import WorkflowError
from roughcut.domain.media_operation import MediaOperationError
from roughcut.domain.project import (
    ImportMode,
    MediaProbe,
    SourceAsset,
    SourceFingerprint,
)
from roughcut.domain.render import (
    MultiSourceRenderPlan,
    OutputSettings,
    RenderClip,
    RenderPlan,
    ToolResolution,
)
from roughcut.domain.transcript import (
    TimedTranscript,
    TranscriptProvenance,
    TranscriptSegment,
)
from roughcut.domain.workflow import (
    ArtifactRef,
    TransactionMarker,
    WorkflowRun,
    canonical_sha256_v1,
    workflow_action_input_hash,
)
from roughcut.domain.workflow_actions import parse_workflow_action_input
from roughcut.mcp import handle_request


def _workflow_project(tmp_path: Path) -> Path:
    root = tmp_path / "workflow-project"
    project = create_project(root, "Workflow")
    source = SourceAsset(
        source_id="src_a",
        kind="audio",
        display_name="A.wav",
        import_mode=ImportMode.LINKED,
        locator={"absolute_path": "/fixture/A.wav"},
        fingerprint=SourceFingerprint(100, 1, "fixture"),
        probe=MediaProbe(
            duration_ticks=600_000,
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
        ),
    )
    transcript = TimedTranscript(
        schema_version=1,
        transcript_version_id="tr_a",
        source_id=source.source_id,
        parent_version_id=None,
        provenance=TranscriptProvenance(
            backend="fixture",
            package_version="fixture",
            models={},
            parameters={},
            raw_result_path="raw-asr/src_a/fixture.json",
            started_at="fixture",
            completed_at="fixture",
            exit_status=0,
        ),
        language="zh-CN",
        segments=tuple(
            TranscriptSegment(
                segment_id=f"seg_{index}",
                start_ticks=(index - 1) * 120_000,
                end_ticks=index * 120_000,
                original_text=text,
                corrected_text=None,
                local_speaker_id=None,
                person_id=None,
                confidence=None,
                fine_units=(),
                editorial_mark="unmarked",
            )
            for index, text in enumerate(
                ("开场。", "图书馆。", "实验室。", "操场。"), start=1
            )
        ),
    )
    write_new_json(
        root / "transcripts" / source.source_id / "tr_a.json",
        transcript.to_dict(),
    )
    ProjectStore(root).save(
        replace(
            project,
            revision=1,
            sources=(source,),
            active_transcript_versions={source.source_id: "tr_a"},
        ),
        expected_revision=0,
    )
    return root


def _add_fixture_source(root: Path, source_id: str) -> None:
    project = ProjectStore(root).load()
    source = replace(
        project.sources[0],
        source_id=source_id,
        display_name=f"{source_id}.wav",
        locator={"absolute_path": f"/fixture/{source_id}.wav"},
        fingerprint=SourceFingerprint(101, 2, f"fixture-{source_id}"),
    )
    transcript_id = f"tr_{source_id}"
    original = TimedTranscript.from_dict(
        json.loads(
            (root / "transcripts" / "src_a" / "tr_a.json").read_text(
                encoding="utf-8"
            )
        )
    )
    transcript = replace(
        original,
        transcript_version_id=transcript_id,
        source_id=source_id,
        parent_version_id=None,
    )
    write_new_json(
        root / "transcripts" / source_id / f"{transcript_id}.json",
        transcript.to_dict(),
    )
    ProjectStore(root).save(
        replace(
            project,
            revision=project.revision + 1,
            sources=(*project.sources, source),
            active_transcript_versions={
                **project.active_transcript_versions,
                source_id: transcript_id,
            },
        ),
        expected_revision=project.revision,
    )


def _advance_to_draft_review(
    root: Path,
    multicam_setup: dict[str, object] | None = None,
    *,
    outline_section_count: int = 4,
) -> tuple[object, EditBrief]:
    result = workflow_start(root, "wfr_test", ["src_a"])
    scope_basis = result.status["confirmation_bases"]["scope"]["basis"]
    scope_input: dict[str, object] = {
        "schema_version": 1,
        "confirmation_basis": scope_basis,
        "source_authorizations": [
            {
                "source_id": "src_a",
                "transcribe": False,
                "speaker_diarization": False,
            }
        ],
    }
    if multicam_setup is not None:
        scope_input["multicam_setup"] = multicam_setup
    result = workflow_action(
        root,
        "wfr_test",
        "act_scope",
        "approve_scope",
        scope_input,
    )
    brief_basis = result.status["confirmation_bases"]["brief"]["basis"]
    result = workflow_action(
        root,
        "wfr_test",
        "act_brief",
        "confirm_brief",
        {
            "schema_version": 1,
            "confirmation_basis": brief_basis,
            "theme": "校园探访",
            "target_duration_ticks": 480_000,
            "focus": ["空间"],
            "allow_reorder": False,
            "speaker_resolution_waivers": [],
        },
    )
    section_titles = (
        ["开场", "图书馆", "实验室", "结尾"]
        if outline_section_count == 4
        else [f"章节 {index}" for index in range(1, outline_section_count + 1)]
    )
    outline = {
        "schema_version": 1,
        "title": "校园探访",
        "opening": "开场",
        "sections": [
            {
                "section_id": f"section_{index}",
                "title": title,
                "summary": title,
                "target_duration_ticks": 120_000,
            }
            for index, title in enumerate(section_titles, start=1)
        ],
        "ending": "结尾",
        "required_content_coverage": [],
        "narration_status": "none",
    }
    result = workflow_action(
        root, "wfr_test", "act_outline", "submit_outline", outline
    )
    outline_ref = result.status["presented_subjects"]["outline_ref"]
    result = workflow_action(
        root,
        "wfr_test",
        "act_approve_outline",
        "approve_outline",
        {
            "schema_version": 1,
            "outline_ref": {
                key: outline_ref[key]
                for key in ("artifact_id", "schema_version", "content_hash")
            },
        },
    )
    brief_ref = result.workflow_run.artifact_refs["brief"]
    assert brief_ref is not None
    brief = EditBrief.from_dict(
        json.loads(
            (root / "briefs" / f"{brief_ref.artifact_id}.json").read_text(
                encoding="utf-8"
            )
        )
    )
    return result, brief


def _submit_basic_draft(
    root: Path,
    action_id: str = "act_draft",
    multicam_setup: dict[str, object] | None = None,
    omit_canonical: bool = False,
    *,
    outline_section_count: int = 4,
):
    draft_review, brief = _advance_to_draft_review(
        root,
        multicam_setup=multicam_setup,
        outline_section_count=outline_section_count,
    )
    project = ProjectStore(root).load()
    context_hash = calculate_agent_context_hash(
        root,
        project=project,
        bindings=(SourceTranscriptBinding("src_a", "tr_a"),),
        brief=brief,
    )
    brief_ref = draft_review.workflow_run.artifact_refs["brief"]
    assert brief_ref is not None
    source_block: dict[str, object] = {
        "block_id": "block_1",
        "kind": "source_excerpt",
        "refs": [
            {
                "source_id": "src_a",
                "transcript_version_id": "tr_a",
                "segment_id": "seg_1",
                "start_ticks": 0,
                "end_ticks": 120_000,
            }
        ],
        "canonical_text": "开场。",
    }
    if omit_canonical:
        source_block.pop("canonical_text")
    return workflow_action(
        root,
        "wfr_test",
        action_id,
        "submit_draft",
        {
            "schema_version": 1,
            "parent_draft_ref": None,
            "display_title": "校园探访",
            "source_bindings": [
                {"source_id": "src_a", "transcript_version_id": "tr_a"}
            ],
            "brief_ref": brief_ref.to_dict(),
            "context_hash": context_hash,
            "blocks": [source_block],
            "scoped_mutable_block_ids": [],
        },
    )


def test_submit_draft_schema2_blocks_create_schema2_child(tmp_path: Path) -> None:
    root = _workflow_project(tmp_path)
    draft_review, brief = _advance_to_draft_review(root)
    project = ProjectStore(root).load()
    context_hash = calculate_agent_context_hash(
        root,
        project=project,
        bindings=(SourceTranscriptBinding("src_a", "tr_a"),),
        brief=brief,
    )
    brief_ref = draft_review.workflow_run.artifact_refs["brief"]
    assert brief_ref is not None

    result = workflow_action(
        root,
        "wfr_test",
        "act_schema2_submit",
        "submit_draft",
        {
            "schema_version": 1,
            "parent_draft_ref": None,
            "display_title": "校园\r\n探访",
            "source_bindings": [
                {"source_id": "src_a", "transcript_version_id": "tr_a"}
            ],
            "brief_ref": brief_ref.to_dict(),
            "context_hash": context_hash,
            "blocks": [
                {
                    "block_id": "section_opening",
                    "kind": "section_title",
                    "title": "开场",
                },
                {
                    "block_id": "block_opening",
                    "kind": "source_excerpt",
                    "refs": [
                        {
                            "source_id": "src_a",
                            "transcript_version_id": "tr_a",
                            "segment_id": "seg_1",
                            "start_ticks": 0,
                            "end_ticks": 120_000,
                        }
                    ],
                    "canonical_text": "开场。",
                },
            ],
            "scoped_mutable_block_ids": [],
        },
    )

    mutation = result.receipt.mutation
    assert mutation is not None
    assert mutation.schema_version == 2
    payload = json.loads(
        (root / "content-drafts" / f"{mutation.artifact_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["schema_version"] == 2
    assert [block["kind"] for block in payload["blocks"]] == [
        "section_title",
        "source_excerpt",
    ]
    assert payload["display_title"] == "校园\n探访"
    stored_run = WorkflowStore(root).read_run("wfr_test")
    stored_ref = stored_run.artifact_refs["content_draft"]
    assert stored_ref is not None
    assert stored_ref.schema_version == 2
    assert stored_ref.artifact_id == mutation.artifact_id
    assert len(list((root / "content-drafts").glob("*.json"))) == 1
    assert len(
        list((root / "workflow" / "receipts").glob("act_schema2_submit.json"))
    ) == 1
    assert not list(
        (root / "workflow" / "transactions").glob("act_schema2_submit*")
    )


def test_submit_draft_omitted_canonical_text_is_derived_by_core(tmp_path: Path) -> None:
    root = _workflow_project(tmp_path)

    result = _submit_basic_draft(root, omit_canonical=True)

    assert result.receipt is not None
    mutation = result.receipt.mutation
    assert mutation is not None
    draft = json.loads(
        (root / "content-drafts" / f"{mutation.artifact_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert draft["blocks"][0]["canonical_text"] == "开场。"


@pytest.mark.parametrize("section_count", (3, 8))
def test_non_default_outline_cardinality_reaches_submit_draft_with_exact_dependency(
    tmp_path: Path,
    section_count: int,
) -> None:
    root = _workflow_project(tmp_path)

    submitted = _submit_basic_draft(
        root,
        action_id=f"act_draft_{section_count}",
        outline_section_count=section_count,
    )

    assert submitted.receipt is not None
    assert submitted.receipt.action == "submit_draft"
    assert submitted.workflow_run.stage == "draft_review"
    status = workflow_status(root, "wfr_test")
    assert status["approval_statuses"]["outline"] == "current"
    outline_ref = submitted.workflow_run.artifact_refs["outline"]
    assert outline_ref is not None
    assert outline_ref.snapshot is not None
    assert len(outline_ref.snapshot["sections"]) == section_count
    assert status["presented_subjects"]["outline_ref"] == {
        "artifact_id": outline_ref.artifact_id,
        "schema_version": outline_ref.schema_version,
        "content_hash": outline_ref.content_hash,
    }

    mutation = submitted.receipt.mutation
    assert mutation is not None
    draft_ref = ArtifactRef(
        mutation.artifact_id, mutation.schema_version, mutation.content_hash
    )
    draft = workflows_module._read_draft_ref(root, draft_ref)
    ancestry = workflows_module._draft_ancestry(root, draft_ref, draft_ref)
    exact_dependency = workflows_module._draft_dependency(
        submitted.workflow_run,
        draft_ref,
        ancestry,
        context_hash=draft.context_hash,
    )
    assert exact_dependency is not None

    changed_artifacts = dict(submitted.workflow_run.artifact_refs)
    changed_artifacts["outline"] = ArtifactRef(
        outline_ref.artifact_id, outline_ref.schema_version, "f" * 64
    )
    changed_run = replace(
        submitted.workflow_run, artifact_refs=changed_artifacts
    )
    changed_dependency = workflows_module._draft_dependency(
        changed_run,
        draft_ref,
        ancestry,
        context_hash=draft.context_hash,
    )
    assert changed_dependency is not None
    assert changed_dependency != exact_dependency


def _advance_to_export_review(
    root: Path,
    multicam_setup: dict[str, object] | None = None,
):
    submitted = _submit_basic_draft(root, multicam_setup=multicam_setup)
    mutation = submitted.receipt.mutation
    assert mutation is not None
    approved = workflow_action(
        root,
        "wfr_test",
        "act_approve_draft",
        "approve_draft",
        {
            "schema_version": 1,
            "content_draft_ref": {
                "artifact_id": mutation.artifact_id,
                "schema_version": mutation.schema_version,
                "content_hash": mutation.content_hash,
            },
        },
    )
    proposal = approved.workflow_run.artifact_refs["proposal"]
    assert proposal is not None
    return workflow_action(
        root,
        "wfr_test",
        "act_adopt",
        "adopt_roughcut",
        {"schema_version": 1, "proposal_ref": proposal.to_dict()},
    )


def _advance_multi_to_export_review(root: Path):
    submitted = _submit_two_binding_draft(root)
    mutation = submitted.receipt.mutation
    assert mutation is not None
    approved = workflow_action(
        root,
        "wfr_test",
        "act_media_multi_approve",
        "approve_draft",
        {
            "schema_version": 1,
            "content_draft_ref": {
                "artifact_id": mutation.artifact_id,
                "schema_version": mutation.schema_version,
                "content_hash": mutation.content_hash,
            },
        },
    )
    proposal = approved.workflow_run.artifact_refs["proposal"]
    assert proposal is not None and proposal.schema_version == 2
    return workflow_action(
        root,
        "wfr_test",
        "act_media_multi_adopt",
        "adopt_roughcut",
        {"schema_version": 1, "proposal_ref": proposal.to_dict()},
    )


@pytest.mark.parametrize("schema_version", (1, 2))
@pytest.mark.parametrize("tamper_kind", ("content", "schema"))
def test_public_proposal_reject_validates_exact_stored_proposal(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    schema_version: int,
    tamper_kind: str,
) -> None:
    root = _workflow_project(tmp_path)
    submitted = _submit_basic_draft(root)
    mutation = submitted.receipt.mutation
    assert mutation is not None
    approved = workflow_action(
        root,
        "wfr_test",
        "act_public_reject_approve_draft",
        "approve_draft",
        {
            "schema_version": 1,
            "content_draft_ref": {
                "artifact_id": mutation.artifact_id,
                "schema_version": mutation.schema_version,
                "content_hash": mutation.content_hash,
            },
        },
    )
    proposal = approved.workflow_run.artifact_refs["proposal"]
    assert proposal is not None
    proposal_path = root / "proposals" / f"{proposal.artifact_id}.json"
    if schema_version == 2:
        _add_fixture_source(root, "src_b")
        single = EditProposal.from_dict(
            json.loads(proposal_path.read_text(encoding="utf-8"))
        )
        multi = MultiSourceEditProposal(
            proposal_id=single.proposal_id,
            base_project_revision=single.base_project_revision,
            base_edit_version_id=single.base_edit_version_id,
            source_bindings=(
                SourceTranscriptBinding("src_a", "tr_a"),
                SourceTranscriptBinding("src_b", "tr_src_b"),
            ),
            brief_snapshot=single.brief_snapshot,
            context_hash=single.context_hash,
            clips=single.clips,
            total_duration_ticks=single.total_duration_ticks,
            created_at=single.created_at,
        )
        proposal_path.write_text(
            json.dumps(multi.to_dict(), ensure_ascii=False, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
        store = WorkflowStore(root)
        current_run = store.read_run("wfr_test")
        artifacts = dict(current_run.artifact_refs)
        artifacts["proposal"] = workflows_module._artifact_ref("proposal", multi)
        current_run = replace(current_run, artifact_refs=artifacts)
        store.write_run(
            current_run,
            expected_run_hash=workflows_module.canonical_sha256_v1(
                store.read_run("wfr_test").to_dict()
            ),
        )
        proposal = artifacts["proposal"]
        assert proposal is not None

    project_revision = ProjectStore(root).load().revision
    valid_run_before = WorkflowStore(root).read_run("wfr_test").to_dict()
    valid_project_before = (root / "project.json").read_bytes()
    mcp_response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": "proposal-reject",
            "method": "tools/call",
            "params": {
                "name": "proposal_reject",
                "arguments": {
                    "project_path": str(root),
                    "proposal_id": proposal.artifact_id,
                    "expected_revision": project_revision,
                },
            },
        }
    )
    assert mcp_response is not None
    assert mcp_response["result"]["structuredContent"]["proposal_rejection"][
        "status"
    ] == "rejected"
    cli_main(
        [
            "proposal-reject",
            "--project",
            str(root),
            "--proposal-id",
            proposal.artifact_id,
            "--expected-revision",
            str(project_revision),
            "--json",
        ]
    )
    cli_success = json.loads(capsys.readouterr().out)
    assert cli_success["proposal_rejection"]["status"] == "rejected"
    assert WorkflowStore(root).read_run("wfr_test").to_dict() == valid_run_before
    assert valid_run_before["stage"] == "roughcut_review"
    assert (root / "project.json").read_bytes() == valid_project_before

    run_before = WorkflowStore(root).read_run("wfr_test").to_dict()
    project_before = (root / "project.json").read_bytes()
    replaced = json.loads(proposal_path.read_text(encoding="utf-8"))
    if tamper_kind == "content":
        replaced["total_duration_ticks"] += 1
    else:
        replaced["schema_version"] = 2 if schema_version == 1 else 1
    proposal_path.write_text(
        json.dumps(replaced, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    mcp_response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": "proposal-reject-tampered",
            "method": "tools/call",
            "params": {
                "name": "proposal_reject",
                "arguments": {
                    "project_path": str(root),
                    "proposal_id": proposal.artifact_id,
                    "expected_revision": project_revision,
                },
            },
        }
    )
    assert mcp_response is not None
    assert mcp_response["result"]["structuredContent"]["error"]["code"] == (
        "workflow_subject_mismatch"
    )
    with pytest.raises(SystemExit):
        cli_main(
            [
                "proposal-reject",
                "--project",
                str(root),
                "--proposal-id",
                proposal.artifact_id,
                "--expected-revision",
                str(project_revision),
                "--json",
            ]
        )
    cli_error = json.loads(capsys.readouterr().out)
    assert cli_error["error"]["code"] == "workflow_subject_mismatch"
    assert WorkflowStore(root).read_run("wfr_test").to_dict() == run_before
    assert (root / "project.json").read_bytes() == project_before


def test_public_render_guard_rejects_active_export_review_bypass(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    _advance_to_export_review(root)
    before = _workflow_business_snapshot(root)
    with pytest.raises(WorkflowError) as rejected, protected_write(root, "render_roughcut"):
        raise AssertionError("public Render must not bypass approve_export")
    assert rejected.value.code == "workflow_transition_not_allowed"
    assert _workflow_business_snapshot(root) == before


def _workflow_business_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
        and "export-staging" not in path.parts
        and path.name not in {".project.lock", ".claim.lock"}
    }


def _public_mcp(tool: str, arguments: dict[str, object]) -> dict[str, Any]:
    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": "stage-five-adversarial",
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
    )
    assert response is not None
    result = response["result"]
    assert isinstance(result, dict)
    payload = result["structuredContent"]
    assert isinstance(payload, dict)
    return payload


def _fixture_render_plan(
    project_path: Path,
    *,
    edit_version_id: str,
    expected_revision: int,
    render_id: str | None = None,
) -> RenderPlan:
    current = ProjectStore(project_path).load()
    assert current.revision == expected_revision
    assert render_id is not None
    tool = ToolResolution("fixture", "/fixture/tool", "fixture")
    return RenderPlan(
        render_id=render_id,
        project_id=current.project_id,
        project_revision=current.revision,
        edit_version_id=edit_version_id,
        source=current.sources[0],
        clips=(RenderClip("clip_test", "src_a", 0, 120_000),),
        output_settings=OutputSettings.from_dict(current.settings),
        ffmpeg=tool,
        ffprobe=tool,
        plan_relative_path=f"renders/{render_id}.plan.json",
        output_relative_path=f"renders/{render_id}.mp4",
        manifest_relative_path=f"renders/{render_id}.manifest.json",
    )


def _fixture_render_manifest(plan: RenderPlan) -> dict[str, object]:
    return {
        "schema_version": 2,
        "render_id": plan.render_id,
        "project_id": plan.project_id,
        "project_revision": plan.project_revision,
        "edit_version_id": plan.edit_version_id,
        "tools": {
            "ffmpeg_version": "fixture",
            "ffprobe_version": "fixture",
        },
        "output_settings": plan.output_settings.to_dict(),
        "clips": [clip.to_dict() for clip in plan.clips],
        "total_duration_ticks": plan.total_duration_ticks,
        "render_schedule": {"strategy": "fixture", "clips": []},
        "command_summary": {"video_encoder": "fixture"},
        "performance": {"wall_seconds": 0},
        "output": {
            "mp4_path": plan.output_relative_path,
            "probe": {"duration_ticks": plan.total_duration_ticks},
        },
        "acceptance": {"accepted": True, "checks": {"duration": True}},
        "input_source": {
            "source_id": plan.source.source_id,
            "fingerprint": plan.source.fingerprint.to_dict(),
            "probe": plan.source.probe.to_dict(),
        },
    }


def _fixture_multi_render_plan(
    project_path: Path,
    *,
    edit_version_id: str,
    expected_revision: int,
    render_id: str | None = None,
) -> MultiSourceRenderPlan:
    current = ProjectStore(project_path).load()
    assert current.revision == expected_revision
    assert render_id is not None
    tool = ToolResolution("fixture", "/fixture/tool", "fixture")
    bindings = tuple(
        SourceTranscriptBinding(
            source.source_id,
            current.active_transcript_versions[source.source_id],
        )
        for source in current.sources
    )
    return MultiSourceRenderPlan(
        render_id=render_id,
        project_id=current.project_id,
        project_revision=current.revision,
        edit_version_id=edit_version_id,
        source_bindings=bindings,
        sources=current.sources,
        clips=(
            RenderClip("clip_a", "src_a", 0, 120_000),
            RenderClip("clip_b", "src_b", 0, 120_000),
        ),
        output_settings=OutputSettings.from_dict(current.settings),
        ffmpeg=tool,
        ffprobe=tool,
        plan_relative_path=f"renders/{render_id}.plan.json",
        output_relative_path=f"renders/{render_id}.mp4",
        manifest_relative_path=f"renders/{render_id}.manifest.json",
    )


def _fixture_multi_render_manifest(
    plan: MultiSourceRenderPlan,
) -> dict[str, object]:
    return {
        "schema_version": 3,
        "render_id": plan.render_id,
        "project_id": plan.project_id,
        "project_revision": plan.project_revision,
        "edit_version_id": plan.edit_version_id,
        "tools": {
            "ffmpeg_version": "fixture",
            "ffprobe_version": "fixture",
        },
        "output_settings": plan.output_settings.to_dict(),
        "clips": [clip.to_dict() for clip in plan.clips],
        "total_duration_ticks": plan.total_duration_ticks,
        "render_schedule": {"strategy": "fixture", "clips": []},
        "command_summary": {"video_encoder": "fixture"},
        "performance": {"wall_seconds": 0},
        "output": {
            "mp4_path": plan.output_relative_path,
            "probe": {"duration_ticks": plan.total_duration_ticks},
        },
        "acceptance": {
            "accepted": True,
            "checks": {
                "duration": True,
                "decision_clips_match_plan": True,
                "input_source_snapshots_verified": True,
            },
        },
        "source_bindings": [
            binding.to_dict() for binding in plan.source_bindings
        ],
        "input_sources": [
            {
                "source_id": source.source_id,
                "fingerprint": source.fingerprint.to_dict(),
                "probe": source.probe.to_dict(),
            }
            for source in plan.sources
        ],
    }


@pytest.mark.skipif(os.name == "nt", reason="real POSIX hard-exit regression")
def test_stage_five_public_mcp_hard_exit_and_recovery_interruption_are_repeatable(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    started = _public_mcp(
        "workflow_start",
        {
            "project_path": str(root),
            "run_id": "wfr_public_recovery",
            "ordered_source_ids": ["src_a"],
        },
    )
    scope_basis = started["status"]["confirmation_bases"]["scope"]["basis"]
    approved = _public_mcp(
        "workflow_action",
        {
            "project_path": str(root),
            "run_id": "wfr_public_recovery",
            "action_id": "act_public_scope",
            "action": "approve_scope",
            "input": {
                "schema_version": 1,
                "confirmation_basis": scope_basis,
                "source_authorizations": [
                    {
                        "source_id": "src_a",
                        "transcribe": False,
                        "speaker_diarization": False,
                    }
                ],
            },
        },
    )
    brief_basis = approved["status"]["confirmation_bases"]["brief"]["basis"]
    project_before = (root / "project.json").read_bytes()
    run_before = (
        root / "workflow" / "runs" / "wfr_public_recovery.json"
    ).read_bytes()
    action_input = {
        "schema_version": 1,
        "confirmation_basis": brief_basis,
        "theme": "公开恢复测试",
        "target_duration_ticks": 240_000,
        "focus": ["恢复"],
        "allow_reorder": False,
        "speaker_resolution_waivers": [],
    }

    child = os.fork()
    if child == 0:
        original_publish = workflows_module._publish_candidate

        def exit_after_candidate(
            project_path: Path, action_id: str, candidate: object
        ) -> None:
            original_publish(project_path, action_id, candidate)
            os._exit(91)

        workflows_module._publish_candidate = exit_after_candidate
        _public_mcp(
            "workflow_action",
            {
                "project_path": str(root),
                "run_id": "wfr_public_recovery",
                "action_id": "act_public_interrupted",
                "action": "confirm_brief",
                "input": action_input,
            },
        )
        os._exit(99)
    _pid, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 91
    marker = (
        root / "workflow" / "transactions" / "act_public_interrupted.json"
    )
    assert marker.is_file()

    recovery_child = os.fork()
    if recovery_child == 0:
        def exit_before_marker_cleanup(
            self: WorkflowStore, path: Path
        ) -> None:
            del self, path
            os._exit(92)

        WorkflowStore._delete_marker_locked = exit_before_marker_cleanup
        _public_mcp(
            "workflow_status",
            {
                "project_path": str(root),
                "run_id": "wfr_public_recovery",
            },
        )
        os._exit(98)
    _pid, status = os.waitpid(recovery_child, 0)
    assert os.waitstatus_to_exitcode(status) == 92
    assert marker.is_file()
    assert (root / "project.json").read_bytes() == project_before
    assert (
        root / "workflow" / "runs" / "wfr_public_recovery.json"
    ).read_bytes() == run_before

    recovered = _public_mcp(
        "workflow_status",
        {
            "project_path": str(root),
            "run_id": "wfr_public_recovery",
        },
    )
    assert recovered["status"]["recovery"]["state"] == "rolled_back"
    assert (root / "project.json").read_bytes() == project_before
    assert (
        root / "workflow" / "runs" / "wfr_public_recovery.json"
    ).read_bytes() == run_before
    assert not marker.exists()
    assert not list((root / "briefs").glob("*.json"))
    assert not list((root / "workflow" / "receipts").glob("act_public_interrupted*"))
    assert not list(root.rglob("*.tmp"))


def test_stage_five_public_mcp_concurrent_export_renders_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _workflow_project(tmp_path)
    _advance_to_export_review(root)
    public_status = _public_mcp(
        "workflow_status",
        {"project_path": str(root), "run_id": "wfr_test"},
    )
    assert not (root / "workflow" / "export-staging").exists()
    export_ref = public_status["status"]["presented_subjects"]["export_ref"]
    render_calls = 0
    render_started = threading.Event()
    competitor_finished = threading.Event()
    start = threading.Barrier(2)
    monkeypatch.setattr(
        media_operations_module,
        "_load_persistent_runtime",
        lambda: type(
            "Runtime",
            (),
            {
                "ffmpeg": ToolResolution(
                    "ffmpeg", "/fixture/ffmpeg", "fixture"
                ),
                "ffprobe": ToolResolution(
                    "ffprobe", "/fixture/ffprobe", "fixture"
                ),
            },
        )(),
    )

    def fake_execute(
        project_path: Path,
        plan: RenderPlan,
        *,
        expected_revision: int,
        output_path: Path,
        manifest_path: Path,
        cancel_requested: object = None,
        after_output_published: object = None,
        phase_callback: object = None,
    ) -> tuple[RenderResult, dict[str, object]]:
        nonlocal render_calls
        del (
            project_path,
            expected_revision,
            cancel_requested,
            phase_callback,
        )
        render_calls += 1
        render_started.set()
        assert competitor_finished.wait(timeout=5)
        output_path.write_bytes(b"fixture-public-render")
        assert callable(after_output_published)
        after_output_published()
        manifest = _fixture_render_manifest(plan)
        write_new_json(manifest_path, manifest)
        return (
            RenderResult(
                plan.render_id,
                plan.output_relative_path,
                plan.manifest_relative_path,
                {"duration": True},
            ),
            manifest,
        )

    def prepare(
        project_path: Path,
        *,
        edit_version_id: str,
        expected_revision: int,
        render_id: str | None = None,
        tools: tuple[ToolResolution, ToolResolution] | None = None,
    ) -> RenderPlan:
        assert tools is not None
        return _fixture_render_plan(
            project_path,
            edit_version_id=edit_version_id,
            expected_revision=expected_revision,
            render_id=render_id,
        )

    monkeypatch.setattr(workflows_module, "prepare_render_plan", prepare)
    monkeypatch.setattr(
        workflows_module, "execute_prepared_render_to_paths", fake_execute
    )

    def export(action_id: str) -> tuple[str, dict[str, Any]]:
        start.wait(timeout=5)
        payload = _public_mcp(
            "workflow_action",
            {
                "project_path": str(root),
                "run_id": "wfr_test",
                "action_id": action_id,
                "action": "approve_export",
                "input": {"schema_version": 1, "export_ref": export_ref},
            },
        )
        if payload["ok"] is False:
            competitor_finished.set()
        return action_id, payload

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(export, action_id)
            for action_id in ("act_export_one", "act_export_two")
        ]
        assert render_started.wait(timeout=5)
        results = [future.result(timeout=10) for future in futures]

    successes = [(action_id, result) for action_id, result in results if result["ok"]]
    failures = [result for _action_id, result in results if not result["ok"]]
    assert len(successes) == 1, [
        (action_id, result.get("error"), result.get("message"))
        for action_id, result in results
    ]
    assert len(failures) == 1, results
    assert failures[0]["error"]["code"] == "workflow_export_in_progress", failures[0]["error"]
    assert str(root) not in json.dumps(failures, ensure_ascii=False)
    assert render_calls == 1
    assert len(list((root / "renders").glob("*.mp4"))) == 1
    assert len(list((root / "renders").glob("*.manifest.json"))) == 1
    assert len(list((root / "renders").glob("*.plan.json"))) == 1
    assert len(
        list((root / "workflow" / "receipts").glob("act_export_*.json"))
    ) == 1

    winning_action, committed = successes[0]
    assert committed["operation_readback"] is False
    assert committed["media_operation"]["operation_id"] == winning_action
    assert set(committed) == {
        "schema_version",
        "tool_schema_version",
        "core_version",
        "source_commit",
        "platform",
        "ok",
        "media_operation",
        "operation_readback",
        "workflow_run",
        "receipt",
        "status",
    }
    readback = _public_mcp(
        "workflow_action",
        {
            "project_path": str(root),
            "run_id": "wfr_test",
            "action_id": winning_action,
            "action": "approve_export",
            "input": {"schema_version": 1, "export_ref": export_ref},
        },
    )
    assert readback["operation_readback"] is True
    assert readback["media_operation"] == committed["media_operation"]
    assert readback["receipt"] == committed["receipt"]
    assert readback["workflow_run"]["lifecycle"] == "completed"
    assert render_calls == 1


def _draft_ref_from_mutation(result: object) -> dict[str, object]:
    mutation = result.receipt.mutation
    assert mutation is not None
    return {
        "artifact_id": mutation.artifact_id,
        "schema_version": mutation.schema_version,
        "content_hash": mutation.content_hash,
    }


def _current_draft_context(root: Path) -> tuple[object, object, str]:
    run = WorkflowStore(root).read_run("wfr_test")
    project = ProjectStore(root).load()
    brief_ref = run.artifact_refs["brief"]
    assert brief_ref is not None
    brief = EditBrief.from_dict(
        json.loads(
            (root / "briefs" / f"{brief_ref.artifact_id}.json").read_text(
                encoding="utf-8"
            )
        )
    )
    bindings = tuple(
        SourceTranscriptBinding(
            binding.source_id, binding.transcript_version_id
        )
        for binding in run.ordered_bindings
        if binding.transcript_version_id is not None
    )
    return run, brief_ref, calculate_agent_context_hash(
        root, project=project, bindings=bindings, brief=brief
    )


def _three_block_draft(root: Path):
    first = _submit_basic_draft(root)
    parent_ref = _draft_ref_from_mutation(first)
    run, brief_ref, context_hash = _current_draft_context(root)
    bindings = [
        {
            "source_id": binding.source_id,
            "transcript_version_id": binding.transcript_version_id,
        }
        for binding in run.ordered_bindings
    ]
    logical_blocks = [
        {
            "block_id": f"block_{index}",
            "kind": "source_excerpt",
            "refs": [
                {
                    "source_id": "src_a",
                    "transcript_version_id": "tr_a",
                    "segment_id": f"seg_{index}",
                    "start_ticks": (index - 1) * 120_000,
                    "end_ticks": index * 120_000,
                }
            ],
            "canonical_text": text,
            "section_title": f"章节 {index}",
        }
        for index, text in enumerate(("开场。", "图书馆。", "实验室。"), start=1)
    ]
    blocks = _schema2_blocks(logical_blocks)
    child = workflow_action(
        root,
        "wfr_test",
        "act_three_blocks",
        "submit_draft",
        {
            "schema_version": 1,
            "parent_draft_ref": parent_ref,
            "display_title": "校园探访",
            "source_bindings": bindings,
            "brief_ref": brief_ref.to_dict(),
            "context_hash": context_hash,
            "blocks": blocks,
            "scoped_mutable_block_ids": [],
        },
    )
    return first, child, logical_blocks


def _schema2_blocks(blocks: list[dict[str, object]]) -> list[dict[str, object]]:
    """Project test-friendly title annotations into independent schema-2 headings."""

    projected: list[dict[str, object]] = []
    for block in blocks:
        copied = copy.deepcopy(block)
        title = copied.pop("section_title", None)
        if title is not None:
            block_id = copied.get("block_id")
            assert isinstance(block_id, str)
            assert isinstance(title, str)
            projected.append(
                {
                    "block_id": f"heading_{block_id}",
                    "kind": "section_title",
                    "title": title,
                }
            )
        projected.append(copied)
    return projected


def _set_logical_section_title(
    blocks: list[dict[str, object]], block_id: str, title: str
) -> None:
    block = next(block for block in blocks if block.get("block_id") == block_id)
    block["section_title"] = title


def _prepare_rebase_input(
    root: Path,
    *,
    before_reapproval: Callable[[object], None] | None = None,
) -> tuple[object, dict[str, object]]:
    first = _submit_basic_draft(root)
    old_anchor = first.workflow_run.artifact_refs["content_draft"]
    assert old_anchor is not None
    if before_reapproval is not None:
        before_reapproval(first)
    _add_fixture_source(root, "src_b")
    status = workflow_status(root, "wfr_test")
    scoped = workflow_action(
        root,
        "wfr_test",
        "act_rebase_scope",
        "approve_scope",
        {
            "schema_version": 1,
            "confirmation_basis": status["confirmation_bases"]["scope"]["basis"],
            "source_authorizations": [
                {
                    "source_id": "src_a",
                    "transcribe": False,
                    "speaker_diarization": False,
                },
                {
                    "source_id": "src_b",
                    "transcribe": False,
                    "speaker_diarization": False,
                },
            ],
        },
    )
    workflow_action(
        root,
        "wfr_test",
        "act_rebase_brief",
        "confirm_brief",
        {
            "schema_version": 1,
            "confirmation_basis": scoped.status["confirmation_bases"]["brief"][
                "basis"
            ],
            "theme": "校园探访",
            "target_duration_ticks": 480_000,
            "focus": ["空间"],
            "allow_reorder": False,
            "speaker_resolution_waivers": [],
        },
    )
    outline = {
        "schema_version": 1,
        "title": "校园探访",
        "opening": "开场",
        "sections": [
            {
                "section_id": f"rebase_section_{index}",
                "title": title,
                "summary": title,
                "target_duration_ticks": 120_000,
            }
            for index, title in enumerate(
                ("开场", "图书馆", "实验室", "结尾"), start=1
            )
        ],
        "ending": "结尾",
        "required_content_coverage": [],
        "narration_status": "none",
    }
    outlined = workflow_action(
        root, "wfr_test", "act_rebase_outline", "submit_outline", outline
    )
    outline_ref = outlined.status["presented_subjects"]["outline_ref"]
    workflow_action(
        root,
        "wfr_test",
        "act_rebase_outline_approval",
        "approve_outline",
        {"schema_version": 1, "outline_ref": outline_ref},
    )
    run, brief_ref, context_hash = _current_draft_context(root)
    old_payload = json.loads(
        (
            root / "content-drafts" / f"{old_anchor.artifact_id}.json"
        ).read_text(encoding="utf-8")
    )
    return first, {
        "schema_version": 1,
        "parent_draft_ref": old_anchor.to_dict(),
        "display_title": "校园探访",
        "source_bindings": [
            {
                "source_id": binding.source_id,
                "transcript_version_id": binding.transcript_version_id,
            }
            for binding in run.ordered_bindings
        ],
        "brief_ref": brief_ref.to_dict(),
        "context_hash": context_hash,
        "blocks": old_payload["blocks"],
        "scoped_mutable_block_ids": [],
    }


def _submit_two_binding_draft(
    root: Path,
    multicam_setup: dict[str, object] | None = None,
    *,
    content_source_ids: tuple[str, ...] | None = None,
):
    _add_fixture_source(root, "src_b")
    if content_source_ids is None:
        content_source_ids = (
            ("src_a",) if multicam_setup is not None else ("src_a", "src_b")
        )
    started = workflow_start(root, "wfr_test", list(content_source_ids))
    scope_input: dict[str, object] = {
        "schema_version": 1,
        "confirmation_basis": started.status["confirmation_bases"]["scope"][
            "basis"
        ],
        "source_authorizations": [
            {
                "source_id": source_id,
                "transcribe": False,
                "speaker_diarization": False,
            }
            for source_id in content_source_ids
        ],
    }
    if multicam_setup is not None:
        scope_input["multicam_setup"] = multicam_setup
    scoped = workflow_action(
        root,
        "wfr_test",
        "act_scope_two",
        "approve_scope",
        scope_input,
    )
    workflow_action(
        root,
        "wfr_test",
        "act_brief_two",
        "confirm_brief",
        {
            "schema_version": 1,
            "confirmation_basis": scoped.status["confirmation_bases"]["brief"][
                "basis"
            ],
            "theme": "双素材",
            "target_duration_ticks": 240_000,
            "focus": ["绑定"],
            "allow_reorder": False,
            "speaker_resolution_waivers": [],
        },
    )
    outlined = workflow_action(
        root,
        "wfr_test",
        "act_outline_two",
        "submit_outline",
        {
            "schema_version": 1,
            "title": "双素材",
            "opening": "A",
            "sections": [
                {
                    "section_id": "section_a",
                    "title": "A",
                    "summary": "A",
                    "target_duration_ticks": 120_000,
                },
                {
                    "section_id": "section_b",
                    "title": "B",
                    "summary": "B",
                    "target_duration_ticks": 120_000,
                },
                {
                    "section_id": "section_c",
                    "title": "C",
                    "summary": "C",
                    "target_duration_ticks": 120_000,
                },
                {
                    "section_id": "section_d",
                    "title": "D",
                    "summary": "D",
                    "target_duration_ticks": 120_000,
                },
            ],
            "ending": "B",
            "required_content_coverage": [],
            "narration_status": "none",
        },
    )
    workflow_action(
        root,
        "wfr_test",
        "act_outline_two_approve",
        "approve_outline",
        {
            "schema_version": 1,
            "outline_ref": outlined.status["presented_subjects"]["outline_ref"],
        },
    )
    run, brief_ref, context_hash = _current_draft_context(root)
    assert [binding.source_id for binding in run.ordered_bindings] == list(
        content_source_ids
    )
    return workflow_action(
        root,
        "wfr_test",
        "act_draft_two",
        "submit_draft",
        {
            "schema_version": 1,
            "parent_draft_ref": None,
            "display_title": "双素材",
            "source_bindings": [
                {
                    "source_id": binding.source_id,
                    "transcript_version_id": binding.transcript_version_id,
                }
                for binding in run.ordered_bindings
            ],
            "brief_ref": brief_ref.to_dict(),
            "context_hash": context_hash,
            "blocks": [
                {
                    "block_id": f"block_{source_id}",
                    "kind": "source_excerpt",
                    "refs": [
                        {
                            "source_id": source_id,
                            "transcript_version_id": (
                                "tr_a" if source_id == "src_a" else f"tr_{source_id}"
                            ),
                            "segment_id": "seg_1",
                            "start_ticks": 0,
                            "end_ticks": 120_000,
                        }
                    ],
                    "canonical_text": "开场。",
                }
                for source_id in content_source_ids
            ],
            "scoped_mutable_block_ids": [],
        },
    )


def test_start_status_cancel_and_lost_response_readback(tmp_path: Path) -> None:
    root = _workflow_project(tmp_path)
    started = workflow_start(root, "wfr_test", ["src_a"])

    assert started.workflow_run.stage == "scope_review"
    assert workflow_status(root, "wfr_test")["next_action"] == "approve_scope"
    canceled = workflow_cancel(root, "wfr_test", "act_cancel")
    retried = workflow_cancel(root, "wfr_test", "act_cancel")

    assert canceled.workflow_run.lifecycle == "canceled"
    assert retried.receipt == canceled.receipt
    assert not (root / "workflow" / "transactions" / "act_cancel.json").exists()


def test_action_and_cancel_without_active_run_fail_closed(tmp_path: Path) -> None:
    root = _workflow_project(tmp_path)
    with pytest.raises(WorkflowError) as action_error:
        workflow_action(
            root,
            "wfr_missing",
            "act_missing",
            "approve_scope",
            {
                "schema_version": 1,
                "confirmation_basis": {
                    "basis_id": f"wfb_scope_{'a' * 64}"
                },
                "source_authorizations": [
                    {
                        "source_id": "src_a",
                        "transcribe": False,
                        "speaker_diarization": False,
                    }
                ],
            },
        )
    assert action_error.value.code == "workflow_required"
    with pytest.raises(WorkflowError) as cancel_error:
        workflow_cancel(root, "wfr_missing", "act_cancel")
    assert cancel_error.value.code == "workflow_required"


def test_status_is_the_exact_closed_schema_and_canceled_run_is_historical(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    workflow_start(root, "wfr_test", ["src_a"])
    status = workflow_status(root, "wfr_test")

    assert set(status) == {
        "schema_version",
        "workflow_run",
        "multicam_alignment",
        "readiness",
        "approval_statuses",
        "confirmation_bases",
        "presented_subjects",
        "binding_sync",
        "recovery",
        "transient_export_claim",
        "allowed_actions",
        "next_action",
    }
    assert set(status["presented_subjects"]) == {
        "outline_ref",
        "draft_anchor_ref",
        "return_subject_ref",
        "confirmed_content_draft_ref",
        "proposal_ref",
        "export_ref",
    }
    canceled = workflow_cancel(root, "wfr_test", "act_cancel")
    historical = canceled.workflow_run.to_dict()
    project = ProjectStore(root).load()
    ProjectStore(root).save(
        replace(
            project,
            revision=project.revision + 1,
            active_transcript_versions={},
        ),
        expected_revision=project.revision,
    )

    assert workflow_status(root, "wfr_test")["workflow_run"] == historical


@pytest.mark.parametrize(
    ("vector_id", "mutation"),
    [
        ("FWV-050", "missing_top"),
        ("FWV-051", "unknown_top"),
        ("FWV-052", "missing_readiness"),
        ("FWV-053", "unknown_scope_basis"),
        ("FWV-054", "missing_presented"),
    ],
)
def test_closed_status_validator_rejects_golden_schema_mutations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    vector_id: str,
    mutation: str,
) -> None:
    del vector_id
    root = _workflow_project(tmp_path)
    workflow_start(root, "wfr_test", ["src_a"])
    public_status = workflows_module._public_status

    def mutate_status(status: dict[str, Any]) -> dict[str, object]:
        payload = copy.deepcopy(public_status(status))
        if mutation == "missing_top":
            payload.pop("confirmation_bases")
        elif mutation == "unknown_top":
            payload["debug_hash"] = "a" * 64
        elif mutation == "missing_readiness":
            payload["readiness"].pop("speaker_resolution_ready_or_waived")
        elif mutation == "unknown_scope_basis":
            payload["confirmation_bases"]["scope"]["basis"]["content_hash"] = (
                "a" * 64
            )
        else:
            payload["presented_subjects"].pop("draft_anchor_ref")
        return payload

    monkeypatch.setattr(workflows_module, "_public_status", mutate_status)
    with pytest.raises(WorkflowError) as captured:
        workflow_status(root, "wfr_test")
    assert captured.value.code == "workflow_integrity_error"


@pytest.mark.parametrize(
    "mutation",
    [
        "integer_scope_basis",
        "wrong_scope_prefix",
        "duplicate_current_source",
        "unsafe_selectable_source",
        "waiver_field_type",
        "recovery_none_with_action",
        "recovery_committed_without_receipt",
        "allowed_actions_out_of_order",
    ],
)
def test_closed_status_validator_rejects_relationship_and_type_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    root = _workflow_project(tmp_path)
    started = workflow_start(root, "wfr_test", ["src_a"])
    workflow_action(
        root,
        "wfr_test",
        "act_scope",
        "approve_scope",
        {
            "schema_version": 1,
            "confirmation_basis": started.status["confirmation_bases"]["scope"][
                "basis"
            ],
            "source_authorizations": [
                {
                    "source_id": "src_a",
                    "transcribe": False,
                    "speaker_diarization": False,
                }
            ],
        },
    )
    public_status = workflows_module._public_status

    def mutate_status(status: dict[str, Any]) -> dict[str, object]:
        payload = copy.deepcopy(public_status(status))
        if mutation == "integer_scope_basis":
            payload["confirmation_bases"]["scope"]["basis"]["basis_id"] = 1
        elif mutation == "wrong_scope_prefix":
            basis_id = payload["confirmation_bases"]["scope"]["basis"]["basis_id"]
            payload["confirmation_bases"]["scope"]["basis"]["basis_id"] = (
                "wfb_brief_" + basis_id.removeprefix("wfb_scope_")
            )
        elif mutation == "duplicate_current_source":
            payload["confirmation_bases"]["scope"][
                "current_ordered_source_ids"
            ] = ["src_a", "src_a"]
        elif mutation == "unsafe_selectable_source":
            payload["confirmation_bases"]["scope"]["selectable_source_ids"] = [
                "../src_a"
            ]
        elif mutation == "waiver_field_type":
            payload["confirmation_bases"]["brief"][
                "eligible_speaker_waivers"
            ] = [
                {
                    "source_id": "src_a",
                    "transcript_version_id": "tr_a",
                    "local_speaker_id": 7,
                }
            ]
        elif mutation == "recovery_none_with_action":
            payload["recovery"]["action_id"] = "act_old"
        elif mutation == "recovery_committed_without_receipt":
            payload["recovery"] = {
                "state": "receipt_committed",
                "action_id": "act_old",
                "receipt_ref": None,
                "message_code": "workflow_previous_action_committed",
            }
        else:
            payload["allowed_actions"] = list(
                reversed(payload["allowed_actions"])
            )
        return payload

    monkeypatch.setattr(workflows_module, "_public_status", mutate_status)
    with pytest.raises(WorkflowError) as captured:
        workflow_status(root, "wfr_test")
    assert captured.value.code == "workflow_integrity_error"


@pytest.mark.parametrize(
    "entrypoint",
    [
        "start",
        "action",
        "action_receipt_readback",
        "cancel",
        "cancel_receipt_readback",
    ],
)
def test_every_facade_success_exit_validates_the_closed_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:
    root = _workflow_project(tmp_path)
    started = None
    if entrypoint != "start":
        started = workflow_start(root, "wfr_test", ["src_a"])
    if entrypoint in {"action_receipt_readback"}:
        assert started is not None
        workflow_action(
            root,
            "wfr_test",
            "act_scope",
            "approve_scope",
            {
                "schema_version": 1,
                "confirmation_basis": started.status["confirmation_bases"]["scope"][
                    "basis"
                ],
                "source_authorizations": [
                    {
                        "source_id": "src_a",
                        "transcribe": False,
                        "speaker_diarization": False,
                    }
                ],
            },
        )
    elif entrypoint == "cancel_receipt_readback":
        workflow_cancel(root, "wfr_test", "act_cancel")
    public_status = workflows_module._public_status

    def corrupt(status: dict[str, Any]) -> dict[str, object]:
        payload = public_status(status)
        payload["internal_debug"] = True
        return payload

    monkeypatch.setattr(workflows_module, "_public_status", corrupt)
    with pytest.raises(WorkflowError) as captured:
        if entrypoint == "start":
            workflow_start(root, "wfr_test", ["src_a"])
        elif entrypoint in {"action", "action_receipt_readback"}:
            assert started is not None
            workflow_action(
                root,
                "wfr_test",
                "act_scope",
                "approve_scope",
                {
                    "schema_version": 1,
                    "confirmation_basis": started.status["confirmation_bases"][
                        "scope"
                    ]["basis"],
                    "source_authorizations": [
                        {
                            "source_id": "src_a",
                            "transcribe": False,
                            "speaker_diarization": False,
                        }
                    ],
                },
            )
        else:
            workflow_cancel(root, "wfr_test", "act_cancel")
    assert captured.value.code == "workflow_integrity_error"


def test_status_repairs_only_the_exact_active_transcript_binding(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    started = workflow_start(root, "wfr_test", ["src_a"])
    authorization_before = started.workflow_run.scope_authorizations
    approval_files_before = list((root / "workflow" / "approvals").iterdir())
    original = TimedTranscript.from_dict(
        json.loads(
            (root / "transcripts" / "src_a" / "tr_a.json").read_text(
                encoding="utf-8"
            )
        )
    )
    replacement = replace(
        original,
        transcript_version_id="tr_a_2",
        parent_version_id="tr_a",
    )
    write_new_json(
        root / "transcripts" / "src_a" / "tr_a_2.json",
        replacement.to_dict(),
    )
    project = ProjectStore(root).load()
    ProjectStore(root).save(
        replace(
            project,
            revision=project.revision + 1,
            active_transcript_versions={"src_a": "tr_a_2"},
        ),
        expected_revision=project.revision,
    )

    repaired = workflow_status(root, "wfr_test")
    assert repaired["binding_sync"] == {
        "state": "repaired",
        "source_ids": ["src_a"],
    }
    assert repaired["workflow_run"]["ordered_bindings"][0][
        "transcript_version_id"
    ] == "tr_a_2"
    assert repaired["workflow_run"]["scope_authorizations"] == [
        item.to_dict() for item in authorization_before
    ]
    assert list((root / "workflow" / "approvals").iterdir()) == approval_files_before
    assert workflow_status(root, "wfr_test")["binding_sync"] == {
        "state": "unchanged",
        "source_ids": [],
    }


def test_transcript_binding_sync_failure_names_workflow_facade(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    workflow_start(root, "wfr_test", ["src_a"])
    with pytest.raises(WorkflowError) as captured:
        synchronize_workflow_transcript_binding(
            root,
            "wfr_test",
            "src_a",
            "tr_not_active",
        )
    assert captured.value.code == "workflow_binding_sync_failed"
    assert "Roughcut workflow façade" in str(captured.value)


def test_status_recovers_candidate_publish_interrupt_before_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workflow_project(tmp_path)
    approved = workflow_start(root, "wfr_test", ["src_a"])
    scope_basis = approved.status["confirmation_bases"]["scope"]["basis"]
    approved = workflow_action(
        root,
        "wfr_test",
        "act_scope",
        "approve_scope",
        {
            "schema_version": 1,
            "confirmation_basis": scope_basis,
            "source_authorizations": [
                {
                    "source_id": "src_a",
                    "transcribe": False,
                    "speaker_diarization": False,
                }
            ],
        },
    )
    project_before = (root / "project.json").read_bytes()
    run_before = approved.workflow_run.to_dict()
    brief_basis = approved.status["confirmation_bases"]["brief"]["basis"]
    publish_candidate = workflows_module._publish_candidate

    def interrupt_after_candidate(
        project_path: Path, action_id: str, candidate: object
    ) -> None:
        publish_candidate(project_path, action_id, candidate)
        raise KeyboardInterrupt

    monkeypatch.setattr(
        workflows_module, "_publish_candidate", interrupt_after_candidate
    )
    with pytest.raises(KeyboardInterrupt):
        workflow_action(
            root,
            "wfr_test",
            "act_interrupted_brief",
            "confirm_brief",
            {
                "schema_version": 1,
                "confirmation_basis": brief_basis,
                "theme": "校园探访",
                "target_duration_ticks": 480_000,
                "focus": ["空间"],
                "allow_reorder": False,
                "speaker_resolution_waivers": [],
            },
        )
    monkeypatch.setattr(
        workflows_module, "_publish_candidate", publish_candidate
    )

    recovered = workflow_status(root, "wfr_test")
    assert recovered["recovery"]["state"] == "rolled_back"
    assert (root / "project.json").read_bytes() == project_before
    assert recovered["workflow_run"] == run_before
    assert not (
        root / "workflow" / "transactions" / "act_interrupted_brief.json"
    ).exists()
    assert list((root / "briefs").glob("*.json")) == []


def test_directory_fsync_failure_after_candidate_rename_recovers_exact_before(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _workflow_project(tmp_path)
    started = workflow_start(root, "wfr_test", ["src_a"])
    scope_basis = started.status["confirmation_bases"]["scope"]["basis"]
    approved = workflow_action(
        root,
        "wfr_test",
        "act_scope",
        "approve_scope",
        {
            "schema_version": 1,
            "confirmation_basis": scope_basis,
            "source_authorizations": [
                {
                    "source_id": "src_a",
                    "transcribe": False,
                    "speaker_diarization": False,
                }
            ],
        },
    )
    project_before = (root / "project.json").read_bytes()
    run_before = approved.workflow_run.to_dict()
    brief_basis = approved.status["confirmation_bases"]["brief"]["basis"]
    real_sync = workflow_candidates_module._sync_directory

    def fail_after_final_rename(path: Path) -> None:
        real_sync(path)
        if path.name == "briefs" and list(path.glob("*.json")):
            raise OSError("fixture post-rename directory fsync failure")

    monkeypatch.setattr(
        workflow_candidates_module,
        "_sync_directory",
        fail_after_final_rename,
    )
    with pytest.raises(OSError, match="post-rename directory fsync failure"):
        workflow_action(
            root,
            "wfr_test",
            "act_brief_dir_sync",
            "confirm_brief",
            {
                "schema_version": 1,
                "confirmation_basis": brief_basis,
                "theme": "校园探访",
                "target_duration_ticks": 480_000,
                "focus": ["空间"],
                "allow_reorder": False,
                "speaker_resolution_waivers": [],
            },
        )

    published = list((root / "briefs").glob("*.json"))
    assert len(published) == 1
    assert os.lstat(published[0]).st_nlink == 1
    assert list((root / "briefs").glob(".*.candidate.tmp")) == []
    monkeypatch.setattr(
        workflow_candidates_module,
        "_sync_directory",
        real_sync,
    )

    recovered = workflow_status(root, "wfr_test")
    assert recovered["recovery"]["state"] == "rolled_back"
    assert (root / "project.json").read_bytes() == project_before
    assert recovered["workflow_run"] == run_before
    assert list((root / "briefs").glob("*.json")) == []
    assert list((root / "briefs").glob(".*.candidate.tmp")) == []


def test_windows_atomic_json_skips_only_post_replace_directory_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "owner.json"
    real_open = os.open
    open_calls: list[object] = []

    def tracked_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        open_calls.append(path)
        if Path(path) == target.parent:
            raise PermissionError("Windows rejects directory handles")
        return real_open(path, flags, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(workflows_module.os, "name", "nt")
        patch.setattr(workflows_module.os, "open", tracked_open)
        workflows_module._write_json_atomically(target, {"schema_version": 1})

    assert json.loads(target.read_text(encoding="utf-8")) == {"schema_version": 1}
    assert target.parent not in open_calls
    assert not target.with_name(f".{target.name}.tmp").exists()


@pytest.mark.skipif(os.name == "nt", reason="real POSIX hard-exit regression")
@pytest.mark.parametrize("exit_point", ["partial_temp", "final_visible"])
def test_real_hard_exit_during_atomic_json_candidate_recovers_exact_before(
    tmp_path: Path, exit_point: str
) -> None:
    root = _workflow_project(tmp_path)
    started = workflow_start(root, "wfr_test", ["src_a"])
    scope_basis = started.status["confirmation_bases"]["scope"]["basis"]
    approved = workflow_action(
        root,
        "wfr_test",
        "act_scope",
        "approve_scope",
        {
            "schema_version": 1,
            "confirmation_basis": scope_basis,
            "source_authorizations": [
                {
                    "source_id": "src_a",
                    "transcribe": False,
                    "speaker_diarization": False,
                }
            ],
        },
    )
    brief_basis = approved.status["confirmation_bases"]["brief"]["basis"]
    project_before = (root / "project.json").read_bytes()
    run_before = approved.workflow_run.to_dict()
    child = os.fork()
    if child == 0:
        if exit_point == "partial_temp":
            import roughcut.adapters.workflow_candidates as candidates_module

            def partial_dump(
                payload: object, stream: object, **kwargs: object
            ) -> None:
                del payload, kwargs
                stream.write('{"partial":')  # type: ignore[attr-defined]
                stream.flush()  # type: ignore[attr-defined]
                os.fsync(stream.fileno())  # type: ignore[attr-defined]
                os._exit(91)

            candidates_module.json.dump = partial_dump
        else:
            original = workflows_module._publish_candidate

            def exit_after_visible(
                project_path: Path, action_id: str, candidate: object
            ) -> None:
                original(project_path, action_id, candidate)
                os._exit(92)

            workflows_module._publish_candidate = exit_after_visible
        workflow_action(
            root,
            "wfr_test",
            f"act_exit_{exit_point}",
            "confirm_brief",
            {
                "schema_version": 1,
                "confirmation_basis": brief_basis,
                "theme": "校园探访",
                "target_duration_ticks": 480_000,
                "focus": ["空间"],
                "allow_reorder": False,
                "speaker_resolution_waivers": [],
            },
        )
        os._exit(93)
    _pid, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) in {91, 92}

    recovered = workflow_status(root, "wfr_test")
    assert recovered["recovery"]["state"] == "rolled_back"
    assert recovered["workflow_run"] == run_before
    assert (root / "project.json").read_bytes() == project_before
    assert list((root / "briefs").glob("*.json")) == []
    assert list((root / "briefs").glob(".*.candidate.tmp")) == []


def test_streaming_sparse_mp4_hash_has_bounded_python_memory(tmp_path: Path) -> None:
    small = tmp_path / "small.mp4"
    large = tmp_path / "large.mp4"
    small.write_bytes(b"\0" * (1024 * 1024))
    with large.open("wb") as stream:
        stream.truncate(128 * 1024 * 1024)

    tracemalloc.start()
    small_hash = stream_file_sha256(small)
    _current, small_peak = tracemalloc.get_traced_memory()
    tracemalloc.reset_peak()
    large_hash = stream_file_sha256(large)
    _current, large_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert small_hash == hashlib.sha256(small.read_bytes()).hexdigest()
    assert len(large_hash) == 64
    assert large_peak < 8 * 1024 * 1024
    assert large_peak < small_peak + 4 * 1024 * 1024


def test_status_rejects_corrupt_export_staging_owner(tmp_path: Path) -> None:
    root = _workflow_project(tmp_path)
    workflow_start(root, "wfr_test", ["src_a"])
    orphan = root / "workflow" / "export-staging" / "stg_corrupt"
    orphan.mkdir(parents=True)
    (orphan / "owner.json").write_text("{", encoding="utf-8")

    with pytest.raises(WorkflowError) as captured:
        workflow_status(root, "wfr_test")
    assert captured.value.code == "workflow_recovery_conflict"
    assert "Roughcut workflow façade" in str(captured.value)
    assert orphan.exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "four_completed",
        "relative_name_not_string",
        "content_hash_not_string",
        "created_at_not_string",
    ],
)
def test_export_owner_closed_schema_type_failures_are_recovery_conflicts(
    tmp_path: Path, mutation: str
) -> None:
    staging_id = "stg_owner_validation"
    owner = workflows_module._owner_payload(
        project_id="proj_test",
        run_id="wfr_test",
        action_id="act_export",
        input_hash="a" * 64,
        export_basis_id=f"wfb_export_{'b' * 64}",
        staging_id=staging_id,
        writing_kind=None,
        completed_files=[
            {
                "kind": "plan",
                "relative_name": f"{staging_id}.plan.json",
                "content_hash": "c" * 64,
            }
        ],
        created_at="2026-07-28T00:00:00.000000Z",
    )
    completed = owner["completed_files"]
    assert isinstance(completed, list)
    if mutation == "four_completed":
        completed.extend(
            [
                {
                    "kind": "mp4",
                    "relative_name": f"{staging_id}.mp4",
                    "content_hash": "d" * 64,
                },
                {
                    "kind": "manifest",
                    "relative_name": f"{staging_id}.manifest.json",
                    "content_hash": "e" * 64,
                },
                {
                    "kind": "manifest",
                    "relative_name": f"{staging_id}.manifest.json",
                    "content_hash": "f" * 64,
                },
            ]
        )
    elif mutation == "relative_name_not_string":
        completed[0]["relative_name"] = 7
    elif mutation == "content_hash_not_string":
        completed[0]["content_hash"] = None
    else:
        owner["created_at"] = 7
    owner_path = tmp_path / "owner.json"
    owner_path.write_text(json.dumps(owner), encoding="utf-8")

    with pytest.raises(WorkflowError) as captured:
        workflows_module._read_owner(owner_path)
    assert captured.value.code == "workflow_recovery_conflict"


def _mid_render_workspace_fixture(
    tmp_path: Path,
) -> tuple[Path, Path, dict[str, object], Path]:
    project_id = "proj_test"
    run_id = "wfr_test"
    action_id = "act_export"
    input_hash = "a" * 64
    basis_id = f"wfb_export_{'b' * 64}"
    staging_id = workflow_export_staging_id(
        project_id,
        run_id,
        action_id,
        input_hash,
    )
    staging_root = tmp_path / "workflow" / "export-staging"
    staging = staging_root / staging_id
    staging.mkdir(parents=True)
    plan_payload = {"schema_version": 1, "fixture": "redacted-mid-render"}
    plan_path = staging / f"{staging_id}.plan.json"
    write_new_json(plan_path, plan_payload)
    owner = workflows_module._owner_payload(
        project_id=project_id,
        run_id=run_id,
        action_id=action_id,
        input_hash=input_hash,
        export_basis_id=basis_id,
        staging_id=staging_id,
        writing_kind="mp4",
        completed_files=[
            {
                "kind": "plan",
                "relative_name": plan_path.name,
                "content_hash": workflows_module.canonical_sha256_v1(
                    plan_payload
                ),
            }
        ],
        created_at="2026-07-30T00:00:00.000000Z",
    )
    workflows_module._write_json_atomically(staging / "owner.json", owner)
    workspace = staging / workflow_renderer_workspace_name(
        project_id,
        run_id,
        action_id,
        input_hash,
        staging_id,
        basis_id,
    )
    workspace.mkdir()
    (workspace / "filter-complex.txt").write_text(
        "[fixture]\n",
        encoding="utf-8",
    )
    (workspace / "candidate.mp4").write_bytes(b"partial")
    return staging_root, staging, owner, workspace


def test_exact_mid_render_workspace_is_valid_closed_staging(
    tmp_path: Path,
) -> None:
    staging_root, staging, owner, _workspace = (
        _mid_render_workspace_fixture(tmp_path)
    )
    workflows_module._validate_staging_directory(
        staging_root,
        staging,
        owner,
        project_id="proj_test",
        run_id="wfr_test",
        action_id="act_export",
        input_hash="a" * 64,
        basis_id=f"wfb_export_{'b' * 64}",
    )


def test_export_staging_and_renderer_paths_fit_windows_max_path() -> None:
    project_root = PureWindowsPath(
        "C:/Users/phase4/Roughcut/Profile2/Windows/"
        "Representative project-012345678901234567890123"
    )
    project_id = "proj_profile2_windows"
    run_id = "wfr_profile2_windows"
    action_id = "act_export_profile2"
    input_hash = "a" * 64
    basis_id = f"wfb_export_{'b' * 64}"
    staging_id = workflow_export_staging_id(
        project_id, run_id, action_id, input_hash
    )
    staging = project_root / "workflow" / "export-staging" / staging_id
    plan_path = staging / f"{staging_id}.plan.json"
    workspace_path = (
        staging
        / workflow_renderer_workspace_name(
            project_id,
            run_id,
            action_id,
            input_hash,
            staging_id,
            basis_id,
        )
        / "candidate.mp4"
    )
    legacy_staging_id = "stg_" + "a" * 64
    legacy_plan_path = (
        project_root
        / "workflow"
        / "export-staging"
        / legacy_staging_id
        / f"{legacy_staging_id}.plan.json"
    )

    expected_digest = canonical_sha256_v1(
        {
            "project_id": project_id,
            "run_id": run_id,
            "action_id": action_id,
            "input_hash": input_hash,
        }
    )
    assert staging_id == f"stg_{expected_digest[:32]}"
    assert len(staging_id) == 36
    assert len(str(plan_path)) < 260
    assert len(str(workspace_path)) < 260
    assert len(str(legacy_plan_path)) >= 260


@pytest.mark.parametrize(
    "invalid",
    (
        "foreign_node",
        "multiple_workspace",
        "workspace_symlink",
        "workspace_hardlink",
        "workspace_identity",
    ),
)
def test_mid_render_workspace_invalid_nodes_fail_closed(
    tmp_path: Path,
    invalid: str,
) -> None:
    if invalid == "workspace_symlink" and not hasattr(os, "symlink"):
        pytest.skip("symlink unavailable")
    if invalid == "workspace_hardlink" and not hasattr(os, "link"):
        pytest.skip("hardlink unavailable")
    staging_root, staging, owner, workspace = (
        _mid_render_workspace_fixture(tmp_path)
    )
    if invalid == "foreign_node":
        (workspace / "foreign.tmp").write_bytes(b"foreign")
    elif invalid == "multiple_workspace":
        (staging / ".renderer-foreign").mkdir()
    elif invalid == "workspace_symlink":
        (workspace / "candidate.mp4").unlink()
        (workspace / "candidate.mp4").symlink_to(tmp_path / "outside")
    elif invalid == "workspace_hardlink":
        (workspace / "candidate.mp4").unlink()
        outside = tmp_path / "outside"
        outside.write_bytes(b"outside")
        os.link(outside, workspace / "candidate.mp4")
    else:
        workspace.rename(staging / f"{workspace.name}0")

    with pytest.raises(WorkflowError) as captured:
        workflows_module._validate_staging_directory(
            staging_root,
            staging,
            owner,
            project_id="proj_test",
            run_id="wfr_test",
            action_id="act_export",
            input_hash="a" * 64,
            basis_id=f"wfb_export_{'b' * 64}",
        )
    assert captured.value.code == "workflow_recovery_conflict"
    assert staging.exists()


@pytest.mark.parametrize("temp_state", ["partial_owner", "empty_directory"])
def test_explicit_export_reentry_cleans_only_deterministic_owner_temp(
    tmp_path: Path, temp_state: str
) -> None:
    staging_root = tmp_path / "workflow" / "export-staging"
    staging_id = workflow_export_staging_id(
        "proj_test", "wfr_test", "act_export", "a" * 64
    )
    directory = staging_root / staging_id
    directory.mkdir(parents=True)
    if temp_state == "partial_owner":
        (directory / ".owner.json.tmp").write_text("{", encoding="utf-8")

    workflows_module._clean_matching_orphan(
        staging_root,
        project_id="proj_test",
        run_id="wfr_test",
        action_id="act_export",
        input_hash="a" * 64,
        basis_id=f"wfb_export_{'b' * 64}",
    )

    assert not directory.exists()


@pytest.mark.skipif(os.name == "nt", reason="real POSIX hard-exit regression")
@pytest.mark.parametrize("exit_point", ["directory_created", "owner_temp_partial"])
def test_real_hard_exit_before_export_owner_visibility_is_safely_reenterable(
    tmp_path: Path, exit_point: str
) -> None:
    staging_root = tmp_path / "workflow" / "export-staging"
    staging_id = workflow_export_staging_id(
        "proj_test", "wfr_test", "act_export", "a" * 64
    )
    directory = staging_root / staging_id
    child = os.fork()
    if child == 0:
        directory.mkdir(parents=True)
        if exit_point == "directory_created":
            os._exit(96)

        def partial_owner_dump(
            payload: object, stream: object, **kwargs: object
        ) -> None:
            del payload, kwargs
            stream.write('{"schema_version":')  # type: ignore[attr-defined]
            stream.flush()  # type: ignore[attr-defined]
            os.fsync(stream.fileno())  # type: ignore[attr-defined]
            os._exit(97)

        workflows_module.json.dump = partial_owner_dump
        workflows_module._write_json_atomically(
            directory / "owner.json",
            {"schema_version": 1},
        )
        os._exit(98)
    _pid, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) in {96, 97}

    workflows_module._clean_matching_orphan(
        staging_root,
        project_id="proj_test",
        run_id="wfr_test",
        action_id="act_export",
        input_hash="a" * 64,
        basis_id=f"wfb_export_{'b' * 64}",
    )
    assert not directory.exists()


@pytest.mark.parametrize("orphan_state", ["partial", "completed_prefix"])
def test_other_action_export_orphan_blocks_direct_action_before_renderer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    orphan_state: str,
) -> None:
    root = _workflow_project(tmp_path)
    adopted = _advance_to_export_review(root)
    export_ref = adopted.status["presented_subjects"]["export_ref"]
    current_input = {"schema_version": 1, "export_ref": export_ref}
    other_input_hash = workflow_action_input_hash(
        "wfr_test", "act_other_export", "approve_export", current_input
    )
    other_staging_id = workflow_export_staging_id(
        adopted.workflow_run.project_id,
        "wfr_test",
        "act_other_export",
        other_input_hash,
    )
    staging_root = root / "workflow" / "export-staging"
    other_staging = staging_root / other_staging_id
    other_staging.mkdir(parents=True, exist_ok=True)
    plan_path = other_staging / f"{other_staging_id}.plan.json"
    completed: list[dict[str, str]] = []
    writing_kind: str | None = "plan"
    if orphan_state == "partial":
        plan_path.write_text('{"partial":', encoding="utf-8")
    else:
        plan_payload = {"schema_version": 1, "fixture": "other-action"}
        write_new_json(plan_path, plan_payload)
        completed = [
            {
                "kind": "plan",
                "relative_name": plan_path.name,
                "content_hash": workflows_module.canonical_sha256_v1(plan_payload),
            }
        ]
        writing_kind = None
    owner = workflows_module._owner_payload(
        project_id=adopted.workflow_run.project_id,
        run_id="wfr_test",
        action_id="act_other_export",
        input_hash=other_input_hash,
        export_basis_id=f"wfb_export_{'b' * 64}",
        staging_id=other_staging_id,
        writing_kind=writing_kind,
        completed_files=completed,
        created_at="2026-07-28T00:00:00.000000Z",
    )
    workflows_module._write_json_atomically(other_staging / "owner.json", owner)
    workflows_module._validate_staging_directory(
        staging_root,
        other_staging,
        owner,
        project_id=adopted.workflow_run.project_id,
        run_id="wfr_test",
        action_id="act_other_export",
        input_hash=other_input_hash,
        basis_id=f"wfb_export_{'b' * 64}",
    )
    evidence_before = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and path.name != ".claim.lock"
    }
    render_calls = 0

    def forbidden_renderer(*args: object, **kwargs: object) -> object:
        nonlocal render_calls
        del args, kwargs
        render_calls += 1
        raise AssertionError("renderer must not start")

    monkeypatch.setattr(
        workflows_module, "execute_prepared_render_to_paths", forbidden_renderer
    )
    with pytest.raises(WorkflowError) as captured:
        workflow_action(
            root,
            "wfr_test",
            "act_current_export",
            "approve_export",
            current_input,
        )

    assert captured.value.code == "workflow_recovery_conflict"
    assert render_calls == 0
    evidence_after = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and path.name != ".claim.lock"
    }
    assert evidence_after == evidence_before
    assert list((root / "workflow" / "transactions").iterdir()) == []


@pytest.mark.parametrize(
    "invalid_node", ["corrupt_owner", "extra_node", "symlink_owner", "hardlink_owner"]
)
def test_current_action_invalid_export_orphan_blocks_renderer_and_preserves_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_node: str,
) -> None:
    if invalid_node == "symlink_owner" and not hasattr(os, "symlink"):
        pytest.skip("symlink unavailable")
    if invalid_node == "hardlink_owner" and not hasattr(os, "link"):
        pytest.skip("hardlink unavailable")
    root = _workflow_project(tmp_path)
    adopted = _advance_to_export_review(root)
    export_ref = adopted.status["presented_subjects"]["export_ref"]
    input_payload = {"schema_version": 1, "export_ref": export_ref}
    action_id = "act_current_export"
    input_hash = workflow_action_input_hash(
        "wfr_test", action_id, "approve_export", input_payload
    )
    project = ProjectStore(root).load()
    run = WorkflowStore(root).read_run("wfr_test")
    decision_ref = run.artifact_refs["decision"]
    assert decision_ref is not None
    decision = workflows_module._read_decision(root, decision_ref)
    exact_export_ref = workflows_module._export_ref(project, decision)
    dependency = workflows_module._export_dependency(project, run, exact_export_ref)
    assert dependency is not None
    basis_projection = {
        "project_id": project.project_id,
        "run_id": run.run_id,
        "export_ref": exact_export_ref.to_dict(),
        "dependency_hash": dependency,
    }
    basis_id = (
        f"wfb_export_{workflows_module.canonical_sha256_v1(basis_projection)}"
    )
    staging_id = workflow_export_staging_id(
        project.project_id, run.run_id, action_id, input_hash
    )
    staging = root / "workflow" / "export-staging" / staging_id
    staging.mkdir(parents=True, exist_ok=True)
    owner = workflows_module._owner_payload(
        project_id=project.project_id,
        run_id=run.run_id,
        action_id=action_id,
        input_hash=input_hash,
        export_basis_id=basis_id,
        staging_id=staging_id,
        writing_kind=None,
        completed_files=[],
        created_at="2026-07-28T00:00:00.000000Z",
    )
    owner_path = staging / "owner.json"
    if invalid_node == "corrupt_owner":
        owner_path.write_text("{", encoding="utf-8")
    elif invalid_node == "symlink_owner":
        external = tmp_path / "external-owner.json"
        external.write_text(json.dumps(owner), encoding="utf-8")
        owner_path.symlink_to(external)
    elif invalid_node == "hardlink_owner":
        external = tmp_path / "external-owner.json"
        external.write_text(json.dumps(owner), encoding="utf-8")
        os.link(external, owner_path)
    else:
        workflows_module._write_json_atomically(owner_path, owner)
        (staging / "unexpected.user").write_bytes(b"preserve-me")
    evidence_before = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and path.name != ".claim.lock"
    }
    render_calls = 0

    def forbidden_renderer(*args: object, **kwargs: object) -> object:
        nonlocal render_calls
        del args, kwargs
        render_calls += 1
        raise AssertionError("renderer must not start")

    monkeypatch.setattr(
        workflows_module, "execute_prepared_render_to_paths", forbidden_renderer
    )
    with pytest.raises(WorkflowError) as captured:
        workflow_action(
            root,
            "wfr_test",
            action_id,
            "approve_export",
            input_payload,
        )
    assert captured.value.code == "workflow_recovery_conflict"
    assert render_calls == 0
    evidence_after = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and path.name != ".claim.lock"
    }
    assert evidence_after == evidence_before


def test_scope_reapproval_uses_fresh_selectable_basis_and_can_replace_source(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    started = workflow_start(root, "wfr_test", ["src_a"])
    initial_basis = started.status["confirmation_bases"]["scope"]["basis"]
    first = workflow_action(
        root,
        "wfr_test",
        "act_scope_a",
        "approve_scope",
        {
            "schema_version": 1,
            "confirmation_basis": initial_basis,
            "source_authorizations": [
                {
                    "source_id": "src_a",
                    "transcribe": False,
                    "speaker_diarization": False,
                }
            ],
        },
    )
    _add_fixture_source(root, "src_b")
    fresh = workflow_status(root, "wfr_test")
    assert fresh["approval_statuses"]["scope"] == "current"
    fresh_basis = fresh["confirmation_bases"]["scope"]["basis"]

    with pytest.raises(WorkflowError) as captured:
        workflow_action(
            root,
            "wfr_test",
            "act_scope_stale",
            "approve_scope",
            {
                "schema_version": 1,
                "confirmation_basis": initial_basis,
                "source_authorizations": [
                    {
                        "source_id": "src_b",
                        "transcribe": False,
                        "speaker_diarization": False,
                    }
                ],
            },
        )
    assert captured.value.code == "workflow_subject_mismatch"
    replaced = workflow_action(
        root,
        "wfr_test",
        "act_scope_b",
        "approve_scope",
        {
            "schema_version": 1,
            "confirmation_basis": fresh_basis,
            "source_authorizations": [
                {
                    "source_id": "src_b",
                    "transcribe": False,
                    "speaker_diarization": False,
                }
            ],
        },
    )
    assert [
        binding.source_id for binding in replaced.workflow_run.ordered_bindings
    ] == ["src_b"]
    assert replaced.workflow_run.stage == "scope_review"
    assert first.workflow_run.artifact_refs["content_draft"] is None


def test_complete_non_render_facade_flow_and_action_idempotency(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    draft_review, brief = _advance_to_draft_review(root)
    project = ProjectStore(root).load()
    context_hash = calculate_agent_context_hash(
        root,
        project=project,
        bindings=(SourceTranscriptBinding("src_a", "tr_a"),),
        brief=brief,
    )
    brief_ref = draft_review.workflow_run.artifact_refs["brief"]
    assert brief_ref is not None
    draft_input = {
        "schema_version": 1,
        "parent_draft_ref": None,
        "display_title": "校园探访",
        "source_bindings": [
            {"source_id": "src_a", "transcript_version_id": "tr_a"}
        ],
        "brief_ref": brief_ref.to_dict(),
        "context_hash": context_hash,
        "blocks": [
            {
                "block_id": "heading_block_1",
                "kind": "section_title",
                "title": "开场",
            },
            {
                "block_id": "block_1",
                "kind": "source_excerpt",
                "refs": [
                    {
                        "source_id": "src_a",
                        "transcript_version_id": "tr_a",
                        "segment_id": "seg_1",
                        "start_ticks": 0,
                        "end_ticks": 120_000,
                    }
                ],
                "canonical_text": "开场。",
            }
        ],
        "scoped_mutable_block_ids": [],
    }
    submitted = workflow_action(
        root, "wfr_test", "act_draft", "submit_draft", draft_input
    )
    retried = workflow_action(
        root, "wfr_test", "act_draft", "submit_draft", copy.deepcopy(draft_input)
    )
    assert retried.receipt == submitted.receipt
    unrelated = ProjectStore(root).load()
    ProjectStore(root).save(
        replace(unrelated, revision=unrelated.revision + 1),
        expected_revision=unrelated.revision,
    )
    draft_ref = submitted.receipt.mutation
    assert draft_ref is not None
    approved = workflow_action(
        root,
        "wfr_test",
        "act_approve_draft",
        "approve_draft",
        {
            "schema_version": 1,
            "content_draft_ref": {
                "artifact_id": draft_ref.artifact_id,
                "schema_version": draft_ref.schema_version,
                "content_hash": draft_ref.content_hash,
            },
        },
    )
    proposal_ref = approved.workflow_run.artifact_refs["proposal"]
    assert proposal_ref is not None
    unrelated = ProjectStore(root).load()
    ProjectStore(root).save(
        replace(unrelated, revision=unrelated.revision + 1),
        expected_revision=unrelated.revision,
    )
    original = TimedTranscript.from_dict(
        json.loads(
            (root / "transcripts" / "src_a" / "tr_a.json").read_text(
                encoding="utf-8"
            )
        )
    )
    replacement = replace(
        original,
        transcript_version_id="tr_a_changed",
        parent_version_id="tr_a",
    )
    write_new_json(
        root / "transcripts" / "src_a" / "tr_a_changed.json",
        replacement.to_dict(),
    )
    changed = ProjectStore(root).load()
    ProjectStore(root).save(
        replace(
            changed,
            revision=changed.revision + 1,
            active_transcript_versions={"src_a": "tr_a_changed"},
        ),
        expected_revision=changed.revision,
    )
    stale = workflow_status(root, "wfr_test")
    assert stale["approval_statuses"]["draft"] == "stale"
    with pytest.raises(WorkflowError) as stale_error:
        workflow_action(
            root,
            "wfr_test",
            "act_adopt_stale",
            "adopt_roughcut",
            {"schema_version": 1, "proposal_ref": proposal_ref.to_dict()},
        )
    assert stale_error.value.code == "workflow_stale"
    restored = ProjectStore(root).load()
    ProjectStore(root).save(
        replace(
            restored,
            revision=restored.revision + 1,
            active_transcript_versions={"src_a": "tr_a"},
        ),
        expected_revision=restored.revision,
    )
    assert workflow_status(root, "wfr_test")["approval_statuses"]["draft"] == "current"
    adopted = workflow_action(
        root,
        "wfr_test",
        "act_adopt",
        "adopt_roughcut",
        {"schema_version": 1, "proposal_ref": proposal_ref.to_dict()},
    )
    assert adopted.workflow_run.stage == "export_review"
    decision_ref = adopted.workflow_run.artifact_refs["decision"]
    confirmed_ref = adopted.workflow_run.artifact_refs["content_draft"]
    assert decision_ref is not None and confirmed_ref is not None
    returned = workflow_action(
        root,
        "wfr_test",
        "act_return",
        "return_to_draft",
        {
            "schema_version": 1,
            "current_subject_ref": {
                "kind": "decision",
                **decision_ref.to_dict(),
            },
            "confirmed_content_draft_ref": confirmed_ref.to_dict(),
        },
    )
    assert returned.workflow_run.stage == "draft_review"
    assert returned.workflow_run.artifact_refs["content_draft"] == confirmed_ref
    assert returned.workflow_run.artifact_refs["proposal"] is None
    assert returned.workflow_run.artifact_refs["decision"] is None
    brief_basis = returned.status["confirmation_bases"]["brief"]["basis"]
    reset = workflow_action(
        root,
        "wfr_test",
        "act_brief_after_decision",
        "confirm_brief",
        {
            "schema_version": 1,
            "confirmation_basis": brief_basis,
            "theme": "校园探访（修订）",
            "target_duration_ticks": 480_000,
            "focus": ["空间"],
            "allow_reorder": False,
            "speaker_resolution_waivers": [],
        },
    )
    assert reset.workflow_run.stage == "scope_review"
    assert reset.status["confirmation_bases"]["scope"] is None
    assert "approve_scope" not in reset.status["allowed_actions"]
    before_run = reset.workflow_run.to_dict()
    before_project = (root / "project.json").read_bytes()
    before_approvals = sorted((root / "workflow" / "approvals").iterdir())
    with pytest.raises(WorkflowError) as scope_error:
        workflow_action(
            root,
            "wfr_test",
            "act_scope_after_decision",
            "approve_scope",
            {
                "schema_version": 1,
                "confirmation_basis": {
                    "basis_id": f"wfb_scope_{'a' * 64}"
                },
                "source_authorizations": [
                    {
                        "source_id": "src_a",
                        "transcribe": False,
                        "speaker_diarization": False,
                    }
                ],
            },
        )
    assert scope_error.value.code == "workflow_transition_not_allowed"
    assert workflow_status(root, "wfr_test")["workflow_run"] == before_run
    assert (root / "project.json").read_bytes() == before_project
    assert sorted((root / "workflow" / "approvals").iterdir()) == before_approvals
    assert list((root / "workflow" / "transactions").iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="real POSIX concurrency regression")
def test_concurrent_adopt_roughcut_has_one_decision_and_one_receipt(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    submitted = _submit_basic_draft(root)
    mutation = submitted.receipt.mutation
    assert mutation is not None
    approved = workflow_action(
        root,
        "wfr_test",
        "act_concurrent_approve",
        "approve_draft",
        {
            "schema_version": 1,
            "content_draft_ref": {
                "artifact_id": mutation.artifact_id,
                "schema_version": mutation.schema_version,
                "content_hash": mutation.content_hash,
            },
        },
    )
    proposal = approved.workflow_run.artifact_refs["proposal"]
    assert proposal is not None
    children: list[int] = []
    for suffix in ("a", "b"):
        child = os.fork()
        if child == 0:
            try:
                workflow_action(
                    root,
                    "wfr_test",
                    f"act_adopt_{suffix}",
                    "adopt_roughcut",
                    {"schema_version": 1, "proposal_ref": proposal.to_dict()},
                )
            except WorkflowError as error:
                os._exit(
                    10 if error.code == "workflow_transition_not_allowed" else 11
                )
            os._exit(0)
        children.append(child)
    exit_codes = [
        os.waitstatus_to_exitcode(os.waitpid(child, 0)[1]) for child in children
    ]
    assert sorted(exit_codes) == [0, 10]
    assert len(list((root / "edits").glob("*.json"))) == 1
    adoption_receipts = list(
        (root / "workflow" / "receipts").glob("act_adopt_*.json")
    )
    assert len(adoption_receipts) == 1
    assert workflow_status(root, "wfr_test")["workflow_run"]["stage"] == "export_review"


def _apply_mutation(payload: object, mutation: dict[str, object]) -> None:
    if mutation["operation"] == "add_top_level_field":
        assert isinstance(payload, dict)
        payload[mutation["field"]] = mutation["value"]
        return
    parts = [part for part in str(mutation["path"]).split("/") if part]
    target = payload
    for part in parts[:-1]:
        target = target[int(part)] if isinstance(target, list) else target[part]
    operation = mutation["operation"]
    final = parts[-1]
    if operation == "append":
        collection = target[int(final)] if isinstance(target, list) else target[final]
        assert isinstance(collection, list)
        collection.append(mutation["value"])
    elif operation == "add" or operation == "replace":
        if isinstance(target, list):
            target[int(final)] = mutation["value"]
        else:
            target[final] = mutation["value"]
    elif operation == "remove":
        if isinstance(target, list):
            target.pop(int(final))
        else:
            target.pop(final)
    else:
        raise AssertionError(f"unsupported vector mutation {operation}")


@pytest.mark.parametrize("vector_index", range(20))
def test_all_action_input_golden_vectors(vector_index: int) -> None:
    vectors_path = (
        Path(__file__).parents[3]
        / "core"
        / "tests"
        / "fixtures"
        / "finite-workflow-vectors.json"
    )
    contract = json.loads(vectors_path.read_text(encoding="utf-8"))
    vector = contract["action_input_vectors"][vector_index]
    payload: Any = copy.deepcopy(
        contract["action_input_fixtures"][vector["input_fixture"]]
    )
    mutations = []
    if "input_mutation" in vector:
        mutations.append(vector["input_mutation"])
    mutations.extend(vector.get("input_mutations", []))
    for mutation in mutations:
        _apply_mutation(payload, mutation)
    if vector["expect"]["ok"]:
        parsed = parse_workflow_action_input(vector["action"], payload)
        reordered = dict(reversed(list(parsed.items())))
        assert workflow_action_input_hash(
            "wfr_vector", "act_vector", vector["action"], parsed
        ) == workflow_action_input_hash(
            "wfr_vector", "act_vector", vector["action"], reordered
        )
    else:
        with pytest.raises(WorkflowError) as captured:
            parse_workflow_action_input(vector["action"], payload)
        assert captured.value.code == "workflow_action_invalid"


def test_fwv_020_reordered_input_has_exact_canonical_hash() -> None:
    vectors_path = (
        Path(__file__).parents[3]
        / "core"
        / "tests"
        / "fixtures"
        / "finite-workflow-vectors.json"
    )
    vectors = json.loads(vectors_path.read_text(encoding="utf-8"))
    vector = vectors["vectors"][19]
    hashes = [
        workflow_action_input_hash(
            request["run_id"],
            request["action_id"],
            request["action"],
            parse_workflow_action_input(request["action"], request["input"]),
        )
        for request in vector["requests"]
    ]
    assert hashes == [vector["expect"]["expected_input_hash"]] * 2


def test_unrelated_project_revision_preserves_outline_dependency(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    result, _brief = _advance_to_draft_review(root)
    before = workflow_status(root, "wfr_test")
    assert before["approval_statuses"]["outline"] == "current"
    project = ProjectStore(root).load()
    ProjectStore(root).save(
        replace(project, revision=project.revision + 1),
        expected_revision=project.revision,
    )
    after = workflow_status(root, "wfr_test")
    assert after["approval_statuses"]["outline"] == "current"
    assert after["workflow_run"]["stage"] == result.workflow_run.stage
    assert "submit_draft" in after["allowed_actions"]


def test_scope_reapproval_rejects_source_outside_selectable_basis(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    started = workflow_start(root, "wfr_test", ["src_a"])
    project_before = (root / "project.json").read_bytes()
    run_before = started.workflow_run.to_dict()
    with pytest.raises(WorkflowError) as captured:
        workflow_action(
            root,
            "wfr_test",
            "act_outside_scope",
            "approve_scope",
            {
                "schema_version": 1,
                "confirmation_basis": started.status["confirmation_bases"][
                    "scope"
                ]["basis"],
                "source_authorizations": [
                    {
                        "source_id": "src_not_selectable",
                        "transcribe": True,
                        "speaker_diarization": False,
                    }
                ],
            },
        )
    assert captured.value.code == "workflow_subject_mismatch"
    assert (root / "project.json").read_bytes() == project_before
    assert workflow_status(root, "wfr_test")["workflow_run"] == run_before
    assert list((root / "workflow" / "transactions").iterdir()) == []


def test_prepare_before_marker_has_zero_business_writes(tmp_path: Path) -> None:
    root = _workflow_project(tmp_path)
    started = workflow_start(root, "wfr_test", ["src_a"])
    approved = workflow_action(
        root,
        "wfr_test",
        "act_scope",
        "approve_scope",
        {
            "schema_version": 1,
            "confirmation_basis": started.status["confirmation_bases"]["scope"][
                "basis"
            ],
            "source_authorizations": [
                {
                    "source_id": "src_a",
                    "transcribe": False,
                    "speaker_diarization": False,
                }
            ],
        },
    )
    payload = {
        "schema_version": 1,
        "confirmation_basis": approved.status["confirmation_bases"]["brief"][
            "basis"
        ],
        "theme": "校园探访",
        "target_duration_ticks": 480_000,
        "focus": ["空间"],
        "allow_reorder": False,
        "speaker_resolution_waivers": [],
    }
    parsed = parse_workflow_action_input("confirm_brief", payload)
    before = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    prepared = workflows_module.prepare_brief(
        root,
        ProjectStore(root).load(),
        WorkflowStore(root).read_run("wfr_test"),
        WorkflowStore(root),
        "act_prepare_only",
        workflow_action_input_hash(
            "wfr_test", "act_prepare_only", "confirm_brief", parsed
        ),
        parsed,
    )
    after = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    assert prepared.candidates and prepared.approvals
    assert before == after
    assert not (
        root / "workflow" / "transactions" / "act_prepare_only.json"
    ).exists()


def test_draft_descendants_preserve_anchor_ancestry_for_delete_move_insert(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    first, parent, blocks = _three_block_draft(root)
    anchor_before = first.workflow_run.artifact_refs["content_draft"]
    parent_ref = _draft_ref_from_mutation(parent)
    _run, brief_ref, context_hash = _current_draft_context(root)
    candidate_logical_blocks = [
        blocks[1],
        blocks[2],
        {
            "block_id": "block_4",
            "kind": "source_excerpt",
            "refs": [
                {
                    "source_id": "src_a",
                    "transcript_version_id": "tr_a",
                    "segment_id": "seg_4",
                    "start_ticks": 360_000,
                    "end_ticks": 480_000,
                }
            ],
            "canonical_text": "操场。",
            "section_title": "新增章节",
        },
    ]
    candidate_blocks = _schema2_blocks(candidate_logical_blocks)
    revised = workflow_action(
        root,
        "wfr_test",
        "act_delete_move_insert",
        "submit_draft",
        {
            "schema_version": 1,
            "parent_draft_ref": parent_ref,
            "display_title": "校园探访",
            "source_bindings": [
                {"source_id": "src_a", "transcript_version_id": "tr_a"}
            ],
            "brief_ref": brief_ref.to_dict(),
            "context_hash": context_hash,
            "blocks": candidate_blocks,
            "scoped_mutable_block_ids": [
                "heading_block_1",
                "block_1",
            ],
        },
    )
    revised_ref = _draft_ref_from_mutation(revised)
    revised_payload = json.loads(
        (
            root
            / "content-drafts"
            / f"{revised_ref['artifact_id']}.json"
        ).read_text(encoding="utf-8")
    )
    assert revised_payload["parent_draft_id"] == parent_ref["artifact_id"]
    assert [block["block_id"] for block in revised_payload["blocks"]] == [
        "heading_block_2",
        "block_2",
        "heading_block_3",
        "block_3",
        "heading_block_4",
        "block_4",
    ]
    assert revised.workflow_run.artifact_refs["content_draft"] == anchor_before
    assert (
        workflow_status(root, "wfr_test")["presented_subjects"]["draft_anchor_ref"]
        == anchor_before.to_dict()
    )


def test_scoped_draft_metadata_changes_only_named_blocks(tmp_path: Path) -> None:
    root = _workflow_project(tmp_path)
    first, parent, blocks = _three_block_draft(root)
    parent_ref = _draft_ref_from_mutation(parent)
    _run, brief_ref, context_hash = _current_draft_context(root)
    changed = copy.deepcopy(blocks)
    _set_logical_section_title(changed, "block_2", "图书馆（修订）")
    result = workflow_action(
        root,
        "wfr_test",
        "act_scoped_metadata",
        "submit_draft",
        {
            "schema_version": 1,
            "parent_draft_ref": parent_ref,
            "display_title": "校园探访",
            "source_bindings": [
                {"source_id": "src_a", "transcript_version_id": "tr_a"}
            ],
            "brief_ref": brief_ref.to_dict(),
            "context_hash": context_hash,
            "blocks": _schema2_blocks(changed),
            "scoped_mutable_block_ids": ["heading_block_2"],
        },
    )
    child_ref = _draft_ref_from_mutation(result)
    child = json.loads(
        (
            root / "content-drafts" / f"{child_ref['artifact_id']}.json"
        ).read_text(encoding="utf-8")
    )
    expected_blocks = _schema2_blocks(blocks)
    expected_heading = next(
        block for block in expected_blocks if block["block_id"] == "heading_block_2"
    )
    assert child["blocks"][0] == expected_blocks[0]
    assert next(
        block for block in child["blocks"] if block["block_id"] == "heading_block_2"
    ) == {**expected_heading, "title": "图书馆（修订）"}
    assert child["blocks"][3] == expected_blocks[3]
    assert result.workflow_run.artifact_refs["content_draft"] == (
        first.workflow_run.artifact_refs["content_draft"]
    )


def test_full_draft_rewrite_requires_explicit_empty_scope(tmp_path: Path) -> None:
    root = _workflow_project(tmp_path)
    _first, parent, blocks = _three_block_draft(root)
    parent_ref = _draft_ref_from_mutation(parent)
    _run, brief_ref, context_hash = _current_draft_context(root)
    rewritten = list(reversed(copy.deepcopy(blocks)))
    for block in rewritten:
        _set_logical_section_title(
            rewritten,
            str(block["block_id"]),
            f"重做：{block['section_title']}",
        )
    result = workflow_action(
        root,
        "wfr_test",
        "act_full_rewrite",
        "submit_draft",
        {
            "schema_version": 1,
            "parent_draft_ref": parent_ref,
            "display_title": "全稿结构重做",
            "source_bindings": [
                {"source_id": "src_a", "transcript_version_id": "tr_a"}
            ],
            "brief_ref": brief_ref.to_dict(),
            "context_hash": context_hash,
            "blocks": _schema2_blocks(rewritten),
            "scoped_mutable_block_ids": [],
        },
    )
    assert result.receipt is not None
    assert result.workflow_run.stage == "draft_review"
    assert "approve_draft" in result.status["allowed_actions"]


def test_scoped_draft_rejects_metadata_change_on_unnamed_block(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    _first, parent, blocks = _three_block_draft(root)
    parent_ref = _draft_ref_from_mutation(parent)
    _run, brief_ref, context_hash = _current_draft_context(root)
    changed = copy.deepcopy(blocks)
    _set_logical_section_title(changed, "block_1", "未授权修改")
    before_files = set((root / "content-drafts").glob("*.json"))
    with pytest.raises(WorkflowError) as captured:
        workflow_action(
            root,
            "wfr_test",
            "act_bad_metadata_scope",
            "submit_draft",
            {
                "schema_version": 1,
                "parent_draft_ref": parent_ref,
                "display_title": "校园探访",
                "source_bindings": [
                    {"source_id": "src_a", "transcript_version_id": "tr_a"}
                ],
                "brief_ref": brief_ref.to_dict(),
                "context_hash": context_hash,
                "blocks": _schema2_blocks(changed),
                "scoped_mutable_block_ids": ["heading_block_2"],
            },
        )
    assert captured.value.code == "workflow_subject_mismatch"
    assert set((root / "content-drafts").glob("*.json")) == before_files


def test_predecision_scope_reapproval_can_delete_selected_source(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    _add_fixture_source(root, "src_b")
    started = workflow_start(root, "wfr_test", ["src_a", "src_b"])
    approved = workflow_action(
        root,
        "wfr_test",
        "act_scope_both",
        "approve_scope",
        {
            "schema_version": 1,
            "confirmation_basis": started.status["confirmation_bases"]["scope"][
                "basis"
            ],
            "source_authorizations": [
                {
                    "source_id": source_id,
                    "transcribe": False,
                    "speaker_diarization": False,
                }
                for source_id in ("src_a", "src_b")
            ],
        },
    )
    reapproved = workflow_action(
        root,
        "wfr_test",
        "act_scope_delete",
        "approve_scope",
        {
            "schema_version": 1,
            "confirmation_basis": approved.status["confirmation_bases"]["scope"][
                "basis"
            ],
            "source_authorizations": [
                {
                    "source_id": "src_a",
                    "transcribe": False,
                    "speaker_diarization": False,
                }
            ],
        },
    )
    assert [
        binding.source_id for binding in reapproved.workflow_run.ordered_bindings
    ] == ["src_a"]
    assert (root / "transcripts" / "src_b" / "tr_src_b.json").exists()


def test_scope_reapproval_failure_restores_anchor_and_downstream_refs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _workflow_project(tmp_path)
    submitted = _submit_basic_draft(root)
    run_before = submitted.workflow_run.to_dict()
    _add_fixture_source(root, "src_b")
    basis = workflow_status(root, "wfr_test")["confirmation_bases"]["scope"][
        "basis"
    ]
    write_approval = WorkflowStore.write_approval

    def interrupt_after_approval(
        store: WorkflowStore, record: object
    ) -> object:
        write_approval(store, record)
        raise KeyboardInterrupt

    monkeypatch.setattr(WorkflowStore, "write_approval", interrupt_after_approval)
    with pytest.raises(KeyboardInterrupt):
        workflow_action(
            root,
            "wfr_test",
            "act_scope_recovery",
            "approve_scope",
            {
                "schema_version": 1,
                "confirmation_basis": basis,
                "source_authorizations": [
                    {
                        "source_id": "src_a",
                        "transcribe": False,
                        "speaker_diarization": False,
                    },
                    {
                        "source_id": "src_b",
                        "transcribe": False,
                        "speaker_diarization": False,
                    },
                ],
            },
        )
    monkeypatch.setattr(WorkflowStore, "write_approval", write_approval)
    recovered = workflow_status(root, "wfr_test")
    assert recovered["recovery"]["state"] == "rolled_back"
    assert recovered["workflow_run"] == run_before
    assert recovered["workflow_run"]["stage"] == "draft_review"


def test_confirmed_draft_and_proposal_publish_failure_recovers_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _workflow_project(tmp_path)
    submitted = _submit_basic_draft(root)
    draft_ref = _draft_ref_from_mutation(submitted)
    publish = workflows_module._publish_candidate
    count = 0

    def interrupt_after_pair(
        project_path: Path, action_id: str, candidate: object
    ) -> None:
        nonlocal count
        publish(project_path, action_id, candidate)
        count += 1
        if count == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(workflows_module, "_publish_candidate", interrupt_after_pair)
    with pytest.raises(KeyboardInterrupt):
        workflow_action(
            root,
            "wfr_test",
            "act_confirm_pair",
            "approve_draft",
            {"schema_version": 1, "content_draft_ref": draft_ref},
        )
    monkeypatch.setattr(workflows_module, "_publish_candidate", publish)
    recovered = workflow_status(root, "wfr_test")
    assert recovered["recovery"]["state"] == "rolled_back"
    assert recovered["workflow_run"] == submitted.workflow_run.to_dict()
    assert len(list((root / "content-drafts").glob("*.json"))) == 1
    assert list((root / "proposals").glob("*.json")) == []


def test_rebase_submit_failure_restores_old_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _workflow_project(tmp_path)
    first, rebase_input = _prepare_rebase_input(root)
    old_anchor = first.workflow_run.artifact_refs["content_draft"]
    publish = workflows_module._publish_candidate

    def interrupt_after_child(
        project_path: Path, action_id: str, candidate: object
    ) -> None:
        publish(project_path, action_id, candidate)
        raise KeyboardInterrupt

    monkeypatch.setattr(workflows_module, "_publish_candidate", interrupt_after_child)
    with pytest.raises(KeyboardInterrupt):
        workflow_action(
            root,
            "wfr_test",
            "act_rebase_failure",
            "submit_draft",
            rebase_input,
        )
    monkeypatch.setattr(workflows_module, "_publish_candidate", publish)
    recovered = workflow_status(root, "wfr_test")
    assert recovered["recovery"]["state"] == "rolled_back"
    assert recovered["presented_subjects"]["draft_anchor_ref"] == old_anchor.to_dict()
    assert len(list((root / "content-drafts").glob("*.json"))) == 1


def test_rebase_receipt_readback_keeps_unconfirmed_anchor(tmp_path: Path) -> None:
    root = _workflow_project(tmp_path)
    _first, rebase_input = _prepare_rebase_input(root)
    committed = workflow_action(
        root,
        "wfr_test",
        "act_rebase_commit",
        "submit_draft",
        rebase_input,
    )
    retried = workflow_action(
        root,
        "wfr_test",
        "act_rebase_commit",
        "submit_draft",
        copy.deepcopy(rebase_input),
    )
    new_anchor = committed.workflow_run.artifact_refs["content_draft"]
    assert new_anchor is not None
    assert retried.receipt == committed.receipt
    assert retried.workflow_run.artifact_refs["content_draft"] == new_anchor
    payload = json.loads(
        (
            root / "content-drafts" / f"{new_anchor.artifact_id}.json"
        ).read_text(encoding="utf-8")
    )
    assert payload["confirmed_by_user"] is False
    assert list((root / "workflow" / "transactions").iterdir()) == []


def test_same_basis_child_does_not_replace_workflow_anchor(tmp_path: Path) -> None:
    root = _workflow_project(tmp_path)
    _first, rebase_input = _prepare_rebase_input(root)
    rebased = workflow_action(
        root,
        "wfr_test",
        "act_rebase_anchor",
        "submit_draft",
        rebase_input,
    )
    anchor = rebased.workflow_run.artifact_refs["content_draft"]
    assert anchor is not None
    child_input = copy.deepcopy(rebase_input)
    child_input["parent_draft_ref"] = anchor.to_dict()
    child = workflow_action(
        root,
        "wfr_test",
        "act_same_basis_child",
        "submit_draft",
        child_input,
    )
    assert child.receipt is not None
    assert child.workflow_run.artifact_refs["content_draft"] == anchor
    assert child.receipt.mutation is not None
    assert child.receipt.mutation.artifact_id != anchor.artifact_id


def test_fwv_008_second_adoption_preserves_the_only_decision(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    adopted = _advance_to_export_review(root)
    proposal = adopted.workflow_run.artifact_refs["proposal"]
    assert proposal is not None
    decision = adopted.workflow_run.artifact_refs["decision"]
    before = _workflow_business_snapshot(root)

    with pytest.raises(WorkflowError) as captured:
        workflow_action(
            root,
            "wfr_test",
            "act_adopt_second",
            "adopt_roughcut",
            {
                "schema_version": 1,
                "proposal_ref": {
                    "artifact_id": proposal.artifact_id,
                    "schema_version": proposal.schema_version,
                    "content_hash": proposal.content_hash,
                },
            },
        )

    assert captured.value.code == "workflow_transition_not_allowed"
    assert WorkflowStore(root).read_run("wfr_test").artifact_refs["decision"] == decision
    assert len(list((root / "edits").glob("*.json"))) == 1
    assert _workflow_business_snapshot(root) == before


def test_fwv_002_unknown_continue_action_cannot_cross_any_gate(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    workflow_start(root, "wfr_test", ["src_a"])
    before = _workflow_business_snapshot(root)
    with pytest.raises(WorkflowError) as captured:
        workflow_action(
            root,
            "wfr_test",
            "act_continue",
            "workflow_continue",
            {},
        )
    assert captured.value.code == "workflow_action_invalid"
    assert _workflow_business_snapshot(root) == before
    assert list((root / "workflow" / "transactions").iterdir()) == []


def test_fwv_003_same_segment_id_in_two_bindings_rejects_crossed_proposal(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    submitted = _submit_two_binding_draft(root)
    draft_ref = _draft_ref_from_mutation(submitted)
    approved = workflow_action(
        root,
        "wfr_test",
        "act_approve_two",
        "approve_draft",
        {"schema_version": 1, "content_draft_ref": draft_ref},
    )
    proposal_ref = approved.workflow_run.artifact_refs["proposal"]
    assert proposal_ref is not None
    proposal_path = root / "proposals" / f"{proposal_ref.artifact_id}.json"
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    assert {
        (
                clip["source_id"],
                clip["transcript_version_id"],
                clip["segment_id"],
        )
        for clip in proposal["clips"]
    } == {
        ("src_a", "tr_a", "seg_1"),
        ("src_b", "tr_src_b", "seg_1"),
    }
    proposal["clips"][1]["transcript_version_id"] = "tr_a"
    proposal_path.write_text(
        json.dumps(proposal, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    before = _workflow_business_snapshot(root)
    with pytest.raises(WorkflowError) as captured:
        workflow_action(
            root,
            "wfr_test",
            "act_wrong_binding",
            "adopt_roughcut",
            {"schema_version": 1, "proposal_ref": proposal_ref.to_dict()},
        )
    assert captured.value.code == "workflow_subject_mismatch"
    assert _workflow_business_snapshot(root) == before


def test_fwv_024_wrong_draft_hash_is_rejected_before_any_write(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    submitted = _submit_basic_draft(root)
    draft_ref = _draft_ref_from_mutation(submitted)
    draft_ref["content_hash"] = "6" * 64
    before = _workflow_business_snapshot(root)

    with pytest.raises(WorkflowError) as captured:
        workflow_action(
            root,
            "wfr_test",
            "act_wrong_draft_ref",
            "approve_draft",
            {"schema_version": 1, "content_draft_ref": draft_ref},
        )

    assert captured.value.code == "workflow_subject_mismatch"
    assert _workflow_business_snapshot(root) == before
    assert list((root / "workflow" / "transactions").iterdir()) == []


def test_fwv_026_wrong_export_hash_is_rejected_without_render_or_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workflow_project(tmp_path)
    adopted = _advance_to_export_review(root)
    export_ref = copy.deepcopy(adopted.status["presented_subjects"]["export_ref"])
    export_ref["content_hash"] = "7" * 64
    render_calls = 0

    def should_not_prepare(*args: object, **kwargs: object) -> RenderPlan:
        nonlocal render_calls
        del args, kwargs
        render_calls += 1
        raise AssertionError("renderer preparation must not run")

    monkeypatch.setattr(
        workflows_module, "prepare_render_plan", should_not_prepare
    )
    before = _workflow_business_snapshot(root)
    with pytest.raises(WorkflowError) as captured:
        workflow_action(
            root,
            "wfr_test",
            "act_wrong_export_ref",
            "approve_export",
            {"schema_version": 1, "export_ref": export_ref},
        )

    assert captured.value.code == "workflow_subject_mismatch"
    assert render_calls == 0
    assert _workflow_business_snapshot(root) == before
    assert list((root / "workflow" / "transactions").iterdir()) == []


def test_fwv_025_wrong_proposal_hash_is_rejected_before_decision_prepare(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    submitted = _submit_basic_draft(root)
    draft_ref = _draft_ref_from_mutation(submitted)
    approved = workflow_action(
        root,
        "wfr_test",
        "act_approve_for_wrong_proposal",
        "approve_draft",
        {"schema_version": 1, "content_draft_ref": draft_ref},
    )
    proposal_ref = approved.workflow_run.artifact_refs["proposal"]
    assert proposal_ref is not None
    wrong_ref = proposal_ref.to_dict()
    wrong_ref["content_hash"] = "5" * 64
    before = _workflow_business_snapshot(root)
    with pytest.raises(WorkflowError) as captured:
        workflow_action(
            root,
            "wfr_test",
            "act_wrong_proposal_ref",
            "adopt_roughcut",
            {"schema_version": 1, "proposal_ref": wrong_ref},
        )
    assert captured.value.code == "workflow_subject_mismatch"
    assert _workflow_business_snapshot(root) == before
    assert not list((root / "edits").glob("*.json"))


def test_fwv_037_scope_requires_transcript_or_explicit_transcribe(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    project = ProjectStore(root).load()
    ProjectStore(root).save(
        replace(
            project,
            revision=project.revision + 1,
            active_transcript_versions={},
        ),
        expected_revision=project.revision,
    )
    started = workflow_start(root, "wfr_test", ["src_a"])
    before = _workflow_business_snapshot(root)
    with pytest.raises(WorkflowError) as captured:
        workflow_action(
            root,
            "wfr_test",
            "act_scope_without_transcript",
            "approve_scope",
            {
                "schema_version": 1,
                "confirmation_basis": started.status["confirmation_bases"]["scope"][
                    "basis"
                ],
                "source_authorizations": [
                    {
                        "source_id": "src_a",
                        "transcribe": False,
                        "speaker_diarization": False,
                    }
                ],
            },
        )
    assert captured.value.code == "workflow_not_ready"
    assert _workflow_business_snapshot(root) == before
    assert WorkflowStore(root).read_run("wfr_test").approval_refs["scope"] is None


def test_fwv_031_completed_staging_is_removed_when_basis_changes_before_relock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workflow_project(tmp_path)
    adopted = _advance_to_export_review(root)
    export_ref = adopted.status["presented_subjects"]["export_ref"]
    monkeypatch.setattr(
        workflows_module, "prepare_render_plan", _fixture_render_plan
    )

    def complete_then_change_basis(
        project_path: Path,
        plan: RenderPlan,
        *,
        expected_revision: int,
        output_path: Path,
        manifest_path: Path,
        cancel_requested: object = None,
        after_output_published: object = None,
    ) -> tuple[RenderResult, dict[str, object]]:
        del expected_revision, cancel_requested
        output_path.write_bytes(b"complete-staged-mp4")
        if after_output_published is not None:
            after_output_published()
        manifest = _fixture_render_manifest(plan)
        write_new_json(manifest_path, manifest)
        current = ProjectStore(project_path).load()
        ProjectStore(project_path).save(
            replace(
                current,
                revision=current.revision + 1,
                settings={**current.settings, "width": 1280},
            ),
            expected_revision=current.revision,
        )
        return (
            RenderResult(
                plan.render_id,
                output_path.relative_to(project_path).as_posix(),
                manifest_path.relative_to(project_path).as_posix(),
                {"duration": True},
            ),
            manifest,
        )

    monkeypatch.setattr(
        workflows_module,
        "execute_prepared_render_to_paths",
        complete_then_change_basis,
    )
    run_before = (
        root / "workflow" / "runs" / "wfr_test.json"
    ).read_bytes()
    approvals_before = {
        path.name: path.read_bytes()
        for path in (root / "workflow" / "approvals").iterdir()
    }
    with pytest.raises(WorkflowError) as captured:
        workflow_action(
            root,
            "wfr_test",
            "act_export_stale_after_staging",
            "approve_export",
            {"schema_version": 1, "export_ref": export_ref},
        )

    assert captured.value.code == "workflow_stale"
    assert (root / "workflow" / "runs" / "wfr_test.json").read_bytes() == run_before
    assert {
        path.name: path.read_bytes()
        for path in (root / "workflow" / "approvals").iterdir()
    } == approvals_before
    assert list((root / "workflow" / "transactions").iterdir()) == []
    assert list((root / "workflow" / "receipts").glob("act_export_stale*")) == []
    assert [
        path
        for path in (root / "workflow" / "export-staging").iterdir()
        if path.name != ".claim.lock"
    ] == []
    assert not list((root / "renders").glob("*.mp4"))


@pytest.mark.skipif(os.name == "nt", reason="real POSIX hard-exit regression")
def test_fwv_030_046_mp4_write_hard_exit_leaves_only_owned_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workflow_project(tmp_path)
    adopted = _advance_to_export_review(root)
    export_ref = adopted.status["presented_subjects"]["export_ref"]
    before = _workflow_business_snapshot(root)
    calls = tmp_path / "render-invocations"
    monkeypatch.setattr(
        workflows_module, "prepare_render_plan", _fixture_render_plan
    )

    def hard_exit_during_mp4(
        project_path: Path,
        plan: RenderPlan,
        *,
        expected_revision: int,
        output_path: Path,
        manifest_path: Path,
        cancel_requested: object = None,
        after_output_published: object = None,
    ) -> tuple[RenderResult, dict[str, object]]:
        del (
            project_path,
            plan,
            expected_revision,
            manifest_path,
            cancel_requested,
            after_output_published,
        )
        calls.write_text("1", encoding="utf-8")
        with output_path.open("wb") as stream:
            stream.write(b"owned-partial-mp4")
            stream.flush()
            os.fsync(stream.fileno())
            os._exit(86)
        raise AssertionError("os._exit returned")

    monkeypatch.setattr(
        workflows_module,
        "execute_prepared_render_to_paths",
        hard_exit_during_mp4,
    )
    child = os.fork()
    if child == 0:
        workflow_action(
            root,
            "wfr_test",
            "act_export_retry",
            "approve_export",
            {"schema_version": 1, "export_ref": export_ref},
        )
        os._exit(87)
    _pid, child_status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(child_status) == 86
    assert calls.read_text(encoding="utf-8") == "1"
    assert _workflow_business_snapshot(root) == before
    assert list((root / "workflow" / "transactions").iterdir()) == []
    assert not list((root / "renders").glob("*.mp4"))
    staging_nodes = [
        path
        for path in (root / "workflow" / "export-staging").iterdir()
        if path.name != ".claim.lock"
    ]
    assert len(staging_nodes) == 1
    owner = workflows_module._read_owner(staging_nodes[0] / "owner.json")
    assert owner["action_id"] == "act_export_retry"
    assert owner["writing_kind"] == "mp4"
    assert [item["kind"] for item in owner["completed_files"]] == ["plan"]
    partial = staging_nodes[0] / f"{staging_nodes[0].name}.mp4"
    assert partial.read_bytes() == b"owned-partial-mp4"
    status = workflow_status(root, "wfr_test")
    assert status["workflow_run"]["stage"] == "export_review"
    assert _workflow_business_snapshot(root) == before


@pytest.mark.skipif(os.name == "nt", reason="real POSIX claim competition")
@pytest.mark.parametrize(
    ("second_action_id", "vector_id"),
    [
        ("act_export_a", "FWV-044"),
        ("act_export_b", "FWV-045"),
    ],
)
def test_fwv_044_045_export_actions_compete_for_one_live_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    second_action_id: str,
    vector_id: str,
) -> None:
    del vector_id
    root = _workflow_project(tmp_path)
    adopted = _advance_to_export_review(root)
    export_ref = adopted.status["presented_subjects"]["export_ref"]
    before = _workflow_business_snapshot(root)
    parent_pid = os.getpid()
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    calls = tmp_path / "claim-render-calls"
    monkeypatch.setattr(
        workflows_module, "prepare_render_plan", _fixture_render_plan
    )

    def blocking_renderer(
        project_path: Path,
        plan: RenderPlan,
        *,
        expected_revision: int,
        output_path: Path,
        manifest_path: Path,
        cancel_requested: object = None,
        after_output_published: object = None,
    ) -> tuple[RenderResult, dict[str, object]]:
        del expected_revision, cancel_requested
        if os.getpid() == parent_pid:
            raise AssertionError("the competing action started a second renderer")
        calls.write_text("1", encoding="utf-8")
        os.write(ready_write, b"1")
        assert os.read(release_read, 1) == b"1"
        output_path.write_bytes(b"fixture-mp4")
        if after_output_published is not None:
            after_output_published()
        manifest = _fixture_render_manifest(plan)
        write_new_json(manifest_path, manifest)
        return (
            RenderResult(
                plan.render_id,
                output_path.relative_to(project_path).as_posix(),
                manifest_path.relative_to(project_path).as_posix(),
                {"duration": True},
            ),
            manifest,
        )

    monkeypatch.setattr(
        workflows_module,
        "execute_prepared_render_to_paths",
        blocking_renderer,
    )
    child = os.fork()
    if child == 0:
        os.close(ready_read)
        os.close(release_write)
        workflow_action(
            root,
            "wfr_test",
            "act_export_a",
            "approve_export",
            {"schema_version": 1, "export_ref": export_ref},
        )
        os._exit(0)
    os.close(ready_write)
    os.close(release_read)
    assert os.read(ready_read, 1) == b"1"
    with pytest.raises(WorkflowError) as captured:
        workflow_action(
            root,
            "wfr_test",
            second_action_id,
            "approve_export",
            {"schema_version": 1, "export_ref": export_ref},
        )
    assert captured.value.code == "workflow_export_in_progress"
    assert calls.read_text(encoding="utf-8") == "1"
    assert _workflow_business_snapshot(root) == before
    os.write(release_write, b"1")
    os.close(release_write)
    _pid, child_status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(child_status) == 0
    assert calls.read_text(encoding="utf-8") == "1"
    assert len(list((root / "renders").glob("*.mp4"))) == 1
    if second_action_id != "act_export_a":
        assert not (
            root / "workflow" / "receipts" / second_action_id
        ).with_suffix(".json").exists()


def test_fwv_038_reconfirms_same_brief_with_new_exact_speaker_waiver(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    started = workflow_start(root, "wfr_test", ["src_a"])
    scoped = workflow_action(
        root,
        "wfr_test",
        "act_scope",
        "approve_scope",
        {
            "schema_version": 1,
            "confirmation_basis": started.status["confirmation_bases"]["scope"][
                "basis"
            ],
            "source_authorizations": [
                {
                    "source_id": "src_a",
                    "transcribe": False,
                    "speaker_diarization": False,
                }
            ],
        },
    )
    business_fields = {
        "theme": "校园探访",
        "target_duration_ticks": 480_000,
        "focus": ["空间"],
        "allow_reorder": False,
    }
    first = workflow_action(
        root,
        "wfr_test",
        "act_brief_initial",
        "confirm_brief",
        {
            "schema_version": 1,
            "confirmation_basis": scoped.status["confirmation_bases"]["brief"][
                "basis"
            ],
            **business_fields,
            "speaker_resolution_waivers": [],
        },
    )
    old_brief = first.workflow_run.artifact_refs["brief"]
    transcript = TimedTranscript.from_dict(
        json.loads(
            (root / "transcripts" / "src_a" / "tr_a.json").read_text(
                encoding="utf-8"
            )
        )
    )
    transcript = replace(
        transcript,
        transcript_version_id="tr_a_2",
        parent_version_id="tr_a",
        segments=tuple(
            replace(segment, local_speaker_id=f"spk_{index % 2 + 1}")
            for index, segment in enumerate(transcript.segments)
        ),
    )
    write_new_json(
        root / "transcripts" / "src_a" / "tr_a_2.json",
        transcript.to_dict(),
    )
    project = ProjectStore(root).load()
    ProjectStore(root).save(
        replace(
            project,
            revision=project.revision + 1,
            active_transcript_versions={"src_a": "tr_a_2"},
        ),
        expected_revision=project.revision,
    )
    status = workflow_status(root, "wfr_test")
    waivers = status["confirmation_bases"]["brief"][
        "eligible_speaker_waivers"
    ]
    assert [item["local_speaker_id"] for item in waivers] == ["spk_1", "spk_2"]
    assert status["next_action"] == "confirm_brief"
    payload = {
        "schema_version": 1,
        "confirmation_basis": status["confirmation_bases"]["brief"]["basis"],
        **business_fields,
        "speaker_resolution_waivers": waivers,
    }
    reconfirmed = workflow_action(
        root, "wfr_test", "act_brief_add_waiver", "confirm_brief", payload
    )
    retried = workflow_action(
        root,
        "wfr_test",
        "act_brief_add_waiver",
        "confirm_brief",
        copy.deepcopy(payload),
    )
    assert reconfirmed.workflow_run.stage == "scope_review"
    assert reconfirmed.workflow_run.artifact_refs["brief"] != old_brief
    assert retried.receipt == reconfirmed.receipt
    changed = copy.deepcopy(payload)
    changed["speaker_resolution_waivers"] = []
    with pytest.raises(WorkflowError) as captured:
        workflow_action(
            root,
            "wfr_test",
            "act_brief_add_waiver",
            "confirm_brief",
            changed,
        )
    assert captured.value.code == "workflow_action_conflict"


def test_fwv_040_041_exact_viewed_branch_creates_current_confirmed_child(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    first, parent, blocks = _three_block_draft(root)
    parent_ref = _draft_ref_from_mutation(parent)
    _run, brief_ref, context_hash = _current_draft_context(root)

    def branch(action_id: str, title: str):
        changed = copy.deepcopy(blocks)
        _set_logical_section_title(changed, "block_1", title)
        return workflow_action(
            root,
            "wfr_test",
            action_id,
            "submit_draft",
            {
                "schema_version": 1,
                "parent_draft_ref": parent_ref,
                "display_title": "校园探访",
                "source_bindings": [
                    {"source_id": "src_a", "transcript_version_id": "tr_a"}
                ],
                "brief_ref": brief_ref.to_dict(),
                "context_hash": context_hash,
                "blocks": _schema2_blocks(changed),
                "scoped_mutable_block_ids": ["heading_block_1"],
            },
        )

    branch_a = branch("act_branch_a", "A")
    branch_b = branch("act_branch_b", "B")
    ref_a = _draft_ref_from_mutation(branch_a)
    ref_b = _draft_ref_from_mutation(branch_b)
    approved = workflow_action(
        root,
        "wfr_test",
        "act_approve_branch_a",
        "approve_draft",
        {"schema_version": 1, "content_draft_ref": ref_a},
    )
    confirmed_ref = approved.workflow_run.artifact_refs["content_draft"]
    approval_ref = approved.workflow_run.approval_refs["draft"]
    assert confirmed_ref is not None and approval_ref is not None
    confirmed = json.loads(
        (
            root / "content-drafts" / f"{confirmed_ref.artifact_id}.json"
        ).read_text(encoding="utf-8")
    )
    approval = WorkflowStore(root).read_approval(
        approval_ref.approval_id, run_id="wfr_test"
    )
    assert confirmed["parent_draft_id"] == ref_a["artifact_id"]
    assert approval.subject.artifact_id == ref_a["artifact_id"]
    assert approval.subject.content_hash == ref_a["content_hash"]
    assert confirmed_ref.artifact_id not in {
        ref_a["artifact_id"],
        ref_b["artifact_id"],
    }
    assert approved.status["approval_statuses"]["draft"] == "current"
    assert first.workflow_run.artifact_refs["content_draft"] is not None


def test_fwv_055_scope_reapproval_adds_source_and_preserves_old_anchor(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    submitted = _submit_basic_draft(root)
    old_anchor = submitted.workflow_run.artifact_refs["content_draft"]
    assert old_anchor is not None
    old_anchor_bytes = (
        root / "content-drafts" / f"{old_anchor.artifact_id}.json"
    ).read_bytes()
    _add_fixture_source(root, "src_narration")
    status = workflow_status(root, "wfr_test")
    result = workflow_action(
        root,
        "wfr_test",
        "act_scope_add_narration",
        "approve_scope",
        {
            "schema_version": 1,
            "confirmation_basis": status["confirmation_bases"]["scope"]["basis"],
            "source_authorizations": [
                {
                    "source_id": "src_a",
                    "transcribe": False,
                    "speaker_diarization": False,
                },
                {
                    "source_id": "src_narration",
                    "transcribe": True,
                    "speaker_diarization": False,
                },
            ],
        },
    )
    assert result.workflow_run.stage == "scope_review"
    assert [
        binding.source_id for binding in result.workflow_run.ordered_bindings
    ] == ["src_a", "src_narration"]
    assert [item.to_dict() for item in result.workflow_run.scope_authorizations] == [
        {
            "source_id": "src_a",
            "transcribe": False,
            "speaker_diarization": False,
        },
        {
            "source_id": "src_narration",
            "transcribe": True,
            "speaker_diarization": False,
        },
    ]
    assert result.workflow_run.artifact_refs["content_draft"] == old_anchor
    for kind in ("brief", "outline", "proposal", "decision", "render"):
        assert result.workflow_run.artifact_refs[kind] is None
    for gate in ("brief", "outline", "draft", "roughcut", "export"):
        assert result.workflow_run.approval_refs[gate] is None
    assert (
        root / "content-drafts" / f"{old_anchor.artifact_id}.json"
    ).read_bytes() == old_anchor_bytes


def test_fwv_056_old_core_basis_is_stale_even_for_in_basis_authorization_change(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    started = workflow_start(root, "wfr_test", ["src_a"])
    old_basis = started.status["confirmation_bases"]["scope"]["basis"]
    _add_fixture_source(root, "src_b")
    before = _workflow_business_snapshot(root)
    with pytest.raises(WorkflowError) as captured:
        workflow_action(
            root,
            "wfr_test",
            "act_scope_stale_basis",
            "approve_scope",
            {
                "schema_version": 1,
                "confirmation_basis": old_basis,
                "source_authorizations": [
                        {
                            "source_id": "src_a",
                            "transcribe": True,
                            "speaker_diarization": True,
                        }
                ],
            },
        )
    assert captured.value.code == "workflow_subject_mismatch"
    assert _workflow_business_snapshot(root) == before


def test_fwv_057_rebase_uses_old_anchor_and_preserves_old_blocks(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    first, rebase_input = _prepare_rebase_input(root)
    old_anchor = first.workflow_run.artifact_refs["content_draft"]
    assert old_anchor is not None
    old_payload = json.loads(
        (
            root / "content-drafts" / f"{old_anchor.artifact_id}.json"
        ).read_text(encoding="utf-8")
    )
    new_heading = {
        "block_id": "heading_block_narration",
        "kind": "section_title",
        "title": "旁白",
    }
    new_block = {
        "block_id": "block_narration",
        "kind": "source_excerpt",
        "refs": [
            {
                "source_id": "src_b",
                "transcript_version_id": "tr_src_b",
                "segment_id": "seg_1",
                "start_ticks": 0,
                "end_ticks": 120_000,
            }
        ],
        "canonical_text": "开场。",
    }
    rebase_input["blocks"] = [
        *copy.deepcopy(old_payload["blocks"]),
        new_heading,
        new_block,
    ]
    result = workflow_action(
        root,
        "wfr_test",
        "act_draft_binding_rebase",
        "submit_draft",
        rebase_input,
    )
    new_anchor = result.workflow_run.artifact_refs["content_draft"]
    assert new_anchor is not None and new_anchor != old_anchor
    payload = json.loads(
        (
            root / "content-drafts" / f"{new_anchor.artifact_id}.json"
        ).read_text(encoding="utf-8")
    )
    assert payload["parent_draft_id"] == old_anchor.artifact_id
    assert payload["blocks"][:-2] == old_payload["blocks"]
    assert payload["blocks"][-2:] == [new_heading, new_block]
    assert result.receipt is not None
    assert result.workflow_run.approval_refs["draft"] is None
    assert (
        root / "content-drafts" / f"{old_anchor.artifact_id}.json"
    ).exists()


_WORKFLOW_ERROR_CODES = {
    "workflow_required",
    "workflow_run_conflict",
    "workflow_transition_not_allowed",
    "workflow_action_invalid",
    "workflow_action_conflict",
    "workflow_subject_mismatch",
    "workflow_not_ready",
    "workflow_approval_required",
    "workflow_stale",
    "workflow_integrity_error",
    "workflow_lock_failed",
    "workflow_binding_sync_failed",
    "workflow_export_in_progress",
    "workflow_recovery_conflict",
}


def _vector_requests(vector: dict[str, Any]) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    if "request" in vector:
        requests.append(vector["request"])
    requests.extend(vector.get("requests", []))
    requests.extend(
        step["request"] for step in vector.get("steps", []) if "request" in step
    )
    return requests


_FWV_DEFERRED_TO_STAGE_4_B: dict[str, str] = {}

_FWV_EXECUTABLE_TEST_OWNERS = {
    "FWV-001": (
        "core/tests/application/test_workflow_public_surface.py::"
        "test_fwv_001_direct_render_has_no_mutation_or_renderer_call"
    ),
    "FWV-002": "test_fwv_002_unknown_continue_action_cannot_cross_any_gate",
    "FWV-003": (
        "test_fwv_003_same_segment_id_in_two_bindings_rejects_crossed_proposal"
    ),
    "FWV-004": "test_unrelated_project_revision_preserves_outline_dependency",
    "FWV-005": "test_complete_non_render_facade_flow_and_action_idempotency",
    "FWV-006": "test_complete_non_render_facade_flow_and_action_idempotency",
    "FWV-007": "test_same_action_id_with_different_closed_input_conflicts",
    "FWV-008": "test_fwv_008_second_adoption_preserves_the_only_decision",
    "FWV-009": "test_complete_non_render_facade_flow_and_action_idempotency",
    "FWV-010": "test_concurrent_adopt_roughcut_has_one_decision_and_one_receipt",
    "FWV-011": (
        "core/tests/adapters/test_workflow_store.py::"
        "test_objects_copied_to_another_project_are_rejected[run]"
    ),
    "FWV-012": (
        "core/tests/adapters/test_workflow_store.py::"
        "test_legacy_project_read_does_not_create_workflow_or_change_project"
    ),
    "FWV-013": (
        "core/tests/adapters/test_workflow_store.py::"
        "test_unsafe_id_path_and_symlinked_workflow_tree_are_rejected"
    ),
    "FWV-014": (
        "core/tests/adapters/test_workflow_store.py::"
        "test_id_path_mismatch_duplicate_key_and_unknown_schema_fail_closed"
    ),
    "FWV-015": (
        "core/tests/application/test_renders.py::"
        "test_fwv_015_cli_and_mcp_direct_render_without_skill_or_run_is_zero_write"
    ),
    "FWV-016": (
        "core/tests/adapters/test_workflow_store.py::"
        "test_scope_authorizations_survive_store_reopen"
    ),
    "FWV-017": "test_start_status_cancel_and_lost_response_readback",
    "FWV-018": (
        "core/tests/adapters/test_workflow_store.py::"
        "test_fwv_018_hard_exit_recovers_before_or_reads_committed_receipt"
    ),
    "FWV-019": (
        "core/tests/adapters/test_workflow_store.py::"
        "test_fwv_019_third_state_fails_closed_without_overwrite"
    ),
    "FWV-020": "test_fwv_020_reordered_input_has_exact_canonical_hash",
    "FWV-021": "test_same_action_id_with_different_closed_input_conflicts",
    "FWV-022": "test_all_action_input_golden_vectors",
    "FWV-023": "test_scope_reapproval_rejects_source_outside_selectable_basis",
    "FWV-024": "test_fwv_024_wrong_draft_hash_is_rejected_before_any_write",
    "FWV-025": (
        "test_fwv_025_wrong_proposal_hash_is_rejected_before_decision_prepare"
    ),
    "FWV-026": "test_fwv_026_wrong_export_hash_is_rejected_without_render_or_write",
    "FWV-027": "test_prepare_before_marker_has_zero_business_writes",
    "FWV-028": "test_real_hard_exit_during_atomic_json_candidate_recovers_exact_before",
    "FWV-029": (
        "core/tests/adapters/test_workflow_store.py::"
        "test_fwv_018_hard_exit_recovers_before_or_reads_committed_receipt"
    ),
    "FWV-030": "test_fwv_030_046_mp4_write_hard_exit_leaves_only_owned_staging",
    "FWV-031": (
        "test_fwv_031_completed_staging_is_removed_when_basis_changes_before_relock"
    ),
    "FWV-032": "test_export_uses_transient_staging_and_fake_renderer_once",
    "FWV-033": "test_status_repairs_only_the_exact_active_transcript_binding",
    "FWV-034": "test_status_repairs_only_the_exact_active_transcript_binding",
    "FWV-035": "test_status_repairs_only_the_exact_active_transcript_binding",
    "FWV-036": "test_status_repairs_only_the_exact_active_transcript_binding",
    "FWV-037": "test_fwv_037_scope_requires_transcript_or_explicit_transcribe",
    "FWV-038": "test_fwv_038_reconfirms_same_brief_with_new_exact_speaker_waiver",
    "FWV-039": "test_draft_descendants_preserve_anchor_ancestry_for_delete_move_insert",
    "FWV-040": (
        "test_fwv_040_041_exact_viewed_branch_creates_current_confirmed_child"
    ),
    "FWV-041": (
        "test_fwv_040_041_exact_viewed_branch_creates_current_confirmed_child"
    ),
    "FWV-042": "test_complete_non_render_facade_flow_and_action_idempotency",
    "FWV-043": "test_confirmed_draft_and_proposal_publish_failure_recovers_pair",
    "FWV-044": (
        "test_fwv_044_045_export_actions_compete_for_one_live_claim"
    ),
    "FWV-045": (
        "test_fwv_044_045_export_actions_compete_for_one_live_claim"
    ),
    "FWV-046": "test_fwv_030_046_mp4_write_hard_exit_leaves_only_owned_staging",
    "FWV-047": "test_export_uses_transient_staging_and_fake_renderer_once",
    "FWV-048": "test_other_action_export_orphan_blocks_direct_action_before_renderer",
    "FWV-049": "test_status_is_the_exact_closed_schema_and_canceled_run_is_historical",
    "FWV-050": "test_closed_status_validator_rejects_golden_schema_mutations",
    "FWV-051": "test_closed_status_validator_rejects_golden_schema_mutations",
    "FWV-052": "test_closed_status_validator_rejects_golden_schema_mutations",
    "FWV-053": "test_closed_status_validator_rejects_golden_schema_mutations",
    "FWV-054": "test_closed_status_validator_rejects_golden_schema_mutations",
    "FWV-055": (
        "test_fwv_055_scope_reapproval_adds_source_and_preserves_old_anchor"
    ),
    "FWV-056": (
        "test_fwv_056_old_core_basis_is_stale_even_for_in_basis_authorization_change"
    ),
    "FWV-057": "test_fwv_057_rebase_uses_old_anchor_and_preserves_old_blocks",
    "FWV-058": "test_complete_non_render_facade_flow_and_action_idempotency",
    "FWV-059": "test_scope_reapproval_failure_restores_anchor_and_downstream_refs",
    "FWV-060": "test_complete_non_render_facade_flow_and_action_idempotency",
    "FWV-061": "test_scoped_draft_metadata_changes_only_named_blocks",
    "FWV-062": "test_scoped_draft_rejects_metadata_change_on_unnamed_block",
    "FWV-063": "test_full_draft_rewrite_requires_explicit_empty_scope",
    "FWV-064": "test_predecision_scope_reapproval_can_delete_selected_source",
    "FWV-065": "test_scope_reapproval_uses_fresh_selectable_basis_and_can_replace_source",
    "FWV-066": "test_scope_reapproval_rejects_source_outside_selectable_basis",
    "FWV-067": "test_complete_non_render_facade_flow_and_action_idempotency",
    "FWV-068": "test_rebase_submit_failure_restores_old_anchor",
    "FWV-069": "test_rebase_receipt_readback_keeps_unconfirmed_anchor",
    "FWV-070": "test_same_basis_child_does_not_replace_workflow_anchor",
}


def test_workflow_vector_semantic_owner_manifest_is_complete() -> None:
    vectors_path = (
        Path(__file__).parents[3]
        / "core"
        / "tests"
        / "fixtures"
        / "finite-workflow-vectors.json"
    )
    contract = json.loads(vectors_path.read_text(encoding="utf-8"))
    vector_ids = {vector["id"] for vector in contract["vectors"]}
    assert set(_FWV_EXECUTABLE_TEST_OWNERS).isdisjoint(_FWV_DEFERRED_TO_STAGE_4_B)
    assert (
        set(_FWV_EXECUTABLE_TEST_OWNERS) | set(_FWV_DEFERRED_TO_STAGE_4_B)
        == vector_ids
    )
    for owner in set(_FWV_EXECUTABLE_TEST_OWNERS.values()):
        if "::" in owner:
            relative_path, node_id = owner.split("::", 1)
            test_name = node_id.split("[", 1)[0]
            source = (Path(__file__).parents[3] / relative_path).read_text(
                encoding="utf-8"
            )
            assert f"def {test_name}(" in source
            continue
        assert owner in globals(), f"missing FWV semantic owner test: {owner}"


def test_same_action_id_with_different_closed_input_conflicts(tmp_path: Path) -> None:
    root = _workflow_project(tmp_path)
    result = workflow_start(root, "wfr_test", ["src_a"])
    basis = result.status["confirmation_bases"]["scope"]["basis"]
    approved = {
        "schema_version": 1,
        "confirmation_basis": basis,
        "source_authorizations": [
            {
                "source_id": "src_a",
                "transcribe": False,
                "speaker_diarization": False,
            }
        ],
    }
    workflow_action(
        root, "wfr_test", "act_scope", "approve_scope", approved
    )
    changed = copy.deepcopy(approved)
    changed["source_authorizations"][0]["transcribe"] = True
    with pytest.raises(WorkflowError) as captured:
        workflow_action(
            root, "wfr_test", "act_scope", "approve_scope", changed
        )
    assert captured.value.code == "workflow_action_conflict"


def test_export_uses_transient_staging_and_fake_renderer_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workflow_project(tmp_path)
    draft_review, brief = _advance_to_draft_review(root)
    project = ProjectStore(root).load()
    context_hash = calculate_agent_context_hash(
        root,
        project=project,
        bindings=(SourceTranscriptBinding("src_a", "tr_a"),),
        brief=brief,
    )
    brief_ref = draft_review.workflow_run.artifact_refs["brief"]
    assert brief_ref is not None
    submitted = workflow_action(
        root,
        "wfr_test",
        "act_export_draft",
        "submit_draft",
        {
            "schema_version": 1,
            "parent_draft_ref": None,
            "display_title": "校园探访",
            "source_bindings": [
                {"source_id": "src_a", "transcript_version_id": "tr_a"}
            ],
            "brief_ref": brief_ref.to_dict(),
            "context_hash": context_hash,
            "blocks": [
                {
                    "block_id": "heading_block_1",
                    "kind": "section_title",
                    "title": "开场",
                },
                {
                    "block_id": "block_1",
                    "kind": "source_excerpt",
                    "refs": [
                        {
                            "source_id": "src_a",
                            "transcript_version_id": "tr_a",
                            "segment_id": "seg_1",
                            "start_ticks": 0,
                            "end_ticks": 120_000,
                        }
                    ],
                    "canonical_text": "开场。",
                },
            ],
            "scoped_mutable_block_ids": [],
        },
    )
    mutation = submitted.receipt.mutation
    assert mutation is not None
    approved = workflow_action(
        root,
        "wfr_test",
        "act_export_approve_draft",
        "approve_draft",
        {
            "schema_version": 1,
            "content_draft_ref": {
                "artifact_id": mutation.artifact_id,
                "schema_version": mutation.schema_version,
                "content_hash": mutation.content_hash,
            },
        },
    )
    proposal_ref = approved.workflow_run.artifact_refs["proposal"]
    assert proposal_ref is not None
    adopted = workflow_action(
        root,
        "wfr_test",
        "act_export_adopt",
        "adopt_roughcut",
        {"schema_version": 1, "proposal_ref": proposal_ref.to_dict()},
    )
    render_calls = 0

    def fake_prepare_render_plan(
        project_path: Path,
        *,
        edit_version_id: str,
        expected_revision: int,
        render_id: str | None = None,
    ) -> RenderPlan:
        current = ProjectStore(project_path).load()
        assert current.revision == expected_revision
        assert render_id is not None
        source = current.sources[0]
        tool = ToolResolution("fixture", "/fixture/tool", "fixture")
        return RenderPlan(
            render_id=render_id,
            project_id=current.project_id,
            project_revision=current.revision,
            edit_version_id=edit_version_id,
            source=source,
            clips=(RenderClip("clip_test", "src_a", 0, 120_000),),
            output_settings=OutputSettings.from_dict(current.settings),
            ffmpeg=tool,
            ffprobe=tool,
            plan_relative_path=f"renders/{render_id}.plan.json",
            output_relative_path=f"renders/{render_id}.mp4",
            manifest_relative_path=f"renders/{render_id}.manifest.json",
        )

    def fake_execute(
        project_path: Path,
        plan: RenderPlan,
        *,
        expected_revision: int,
        output_path: Path,
        manifest_path: Path,
        cancel_requested: object = None,
        after_output_published: object = None,
    ) -> tuple[RenderResult, dict[str, object]]:
        nonlocal render_calls
        del project_path, expected_revision, cancel_requested
        render_calls += 1
        if render_calls == 1:
            output_path.write_bytes(b"owned-partial-mp4")
            raise KeyboardInterrupt
        output_path.write_bytes(b"fixture-mp4")
        if after_output_published is not None:
            after_output_published()
        manifest = {
            "schema_version": 2,
            "render_id": plan.render_id,
            "project_id": plan.project_id,
            "project_revision": plan.project_revision,
            "edit_version_id": plan.edit_version_id,
            "tools": {
                "ffmpeg_version": "fixture",
                "ffprobe_version": "fixture",
            },
            "output_settings": plan.output_settings.to_dict(),
            "clips": [clip.to_dict() for clip in plan.clips],
            "total_duration_ticks": plan.total_duration_ticks,
            "render_schedule": {"strategy": "fixture", "clips": []},
            "command_summary": {"video_encoder": "fixture"},
            "performance": {"wall_seconds": 0},
            "output": {
                "mp4_path": plan.output_relative_path,
                "probe": {"duration_ticks": plan.total_duration_ticks},
            },
            "acceptance": {"accepted": True, "checks": {"duration": True}},
            "input_source": {
                "source_id": plan.source.source_id,
                "fingerprint": plan.source.fingerprint.to_dict(),
                "probe": plan.source.probe.to_dict(),
            },
        }
        write_new_json(manifest_path, manifest)
        return (
            RenderResult(
                plan.render_id,
                plan.output_relative_path,
                plan.manifest_relative_path,
                {"duration": True},
            ),
            manifest,
        )

    monkeypatch.setattr(
        workflows_module, "prepare_render_plan", fake_prepare_render_plan
    )
    monkeypatch.setattr(
        workflows_module, "execute_prepared_render_to_paths", fake_execute
    )
    export_ref = adopted.status["presented_subjects"]["export_ref"]
    with project_export_claim(root), pytest.raises(WorkflowError) as claim_error:
        workflow_action(
            root,
            "wfr_test",
            "act_export",
            "approve_export",
            {"schema_version": 1, "export_ref": export_ref},
        )
    assert claim_error.value.code == "workflow_export_in_progress"
    assert render_calls == 0
    with pytest.raises(KeyboardInterrupt):
        workflow_action(
            root,
            "wfr_test",
            "act_export",
            "approve_export",
            {"schema_version": 1, "export_ref": export_ref},
        )
    assert [
        path.name
        for path in (root / "workflow" / "export-staging").iterdir()
        if path.name != ".claim.lock"
    ]
    if os.name != "nt":
        child = os.fork()
        if child == 0:
            original_publish = workflows_module._publish_candidate

            def exit_after_formal_mp4_visible(
                project_path: Path, action_id: str, candidate: object
            ) -> None:
                original_publish(project_path, action_id, candidate)
                if candidate[0].kind == "mp4":  # type: ignore[index]
                    os._exit(94)

            workflows_module._publish_candidate = exit_after_formal_mp4_visible
            workflow_action(
                root,
                "wfr_test",
                "act_export",
                "approve_export",
                {"schema_version": 1, "export_ref": export_ref},
            )
            os._exit(95)
        _pid, child_status = os.waitpid(child, 0)
        assert os.waitstatus_to_exitcode(child_status) == 94
        recovered = workflow_status(root, "wfr_test")
        assert recovered["recovery"]["state"] == "rolled_back"
        assert recovered["workflow_run"]["stage"] == "export_review"
    exported = workflow_action(
        root,
        "wfr_test",
        "act_export",
        "approve_export",
        {"schema_version": 1, "export_ref": export_ref},
    )
    retried = workflow_action(
        root,
        "wfr_test",
        "act_export",
        "approve_export",
        {"schema_version": 1, "export_ref": export_ref},
    )

    assert render_calls == 2
    assert exported.workflow_run.lifecycle == "completed"
    assert retried.receipt == exported.receipt
    staging_root = root / "workflow" / "export-staging"
    assert [path.name for path in staging_root.iterdir()] == [".claim.lock"]


def test_media_operation_approve_export_uses_transaction_and_terminal_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _workflow_project(tmp_path)
    adopted = _advance_to_export_review(root)
    export_ref = adopted.status["presented_subjects"]["export_ref"]
    render_calls = 0
    drift_calls = 0

    monkeypatch.setattr(
        media_operations_module,
        "_load_persistent_runtime",
        lambda: type(
            "Runtime",
            (),
            {
                "ffmpeg": ToolResolution(
                    "ffmpeg", "/fixture/ffmpeg", "fixture"
                ),
                "ffprobe": ToolResolution(
                    "ffprobe", "/fixture/ffprobe", "fixture"
                ),
            },
        )(),
    )

    def validate_runtime(_runtime: object) -> tuple[str, str]:
        nonlocal drift_calls
        drift_calls += 1
        return "ffmpeg version fixture", "ffprobe version fixture"

    monkeypatch.setattr(
        media_operations_module,
        "_validate_media_runtime",
        validate_runtime,
    )

    def prepare(
        project_path: Path,
        *,
        edit_version_id: str,
        expected_revision: int,
        render_id: str | None = None,
        tools: tuple[ToolResolution, ToolResolution] | None = None,
    ) -> RenderPlan:
        assert tools is not None
        return _fixture_render_plan(
            project_path,
            edit_version_id=edit_version_id,
            expected_revision=expected_revision,
            render_id=render_id,
        )

    def execute(
        _project_path: Path,
        plan: RenderPlan,
        *,
        expected_revision: int,
        output_path: Path,
        manifest_path: Path,
        cancel_requested: object = None,
        after_output_published: Callable[[], None] | None = None,
        phase_callback: Callable[[str], None] | None = None,
    ) -> tuple[RenderResult, dict[str, object]]:
        nonlocal render_calls
        del expected_revision, cancel_requested
        render_calls += 1
        if phase_callback is not None:
            phase_callback("render_encoding")
        output_path.write_bytes(b"fixture-render")
        if after_output_published is not None:
            after_output_published()
        if phase_callback is not None:
            phase_callback("render_verifying")
        manifest = _fixture_render_manifest(plan)
        write_new_json(manifest_path, manifest)
        return (
            RenderResult(
                plan.render_id,
                plan.output_relative_path,
                plan.manifest_relative_path,
                {"duration": True},
            ),
            manifest,
        )

    monkeypatch.setattr(workflows_module, "prepare_render_plan", prepare)
    monkeypatch.setattr(
        workflows_module,
        "execute_prepared_render_to_paths",
        execute,
    )
    revision_before = ProjectStore(root).load().revision
    outcome = run_approve_export_operation(
        root,
        run_id="wfr_test",
        action_id="act_media_export",
        action_input={"schema_version": 1, "export_ref": export_ref},
    )
    assert outcome.record.status == "succeeded"
    result = outcome.record.result_ref
    assert result is not None
    assert result.render_plan_ref.schema_version == 1
    assert result.manifest_ref.schema_version == 2
    assert result.approve_export_receipt_ref.action_id == "act_media_export"
    assert outcome.result is not None
    assert outcome.result.workflow_run.stage == "exporting"
    assert outcome.result.workflow_run.lifecycle == "completed"
    assert ProjectStore(root).load().revision == revision_before
    assert not list((root / "workflow" / "transactions").glob("*.json"))
    assert not [
        path
        for path in (root / "workflow" / "export-staging").iterdir()
        if path.name != ".claim.lock"
    ]
    assert render_calls == 1
    assert drift_calls == 1

    monkeypatch.setattr(
        media_operations_module,
        "_load_persistent_runtime",
        lambda: (_ for _ in ()).throw(
            AssertionError("terminal readback re-ran runtime preflight")
        ),
    )
    readback = run_approve_export_operation(
        root,
        run_id="wfr_test",
        action_id="act_media_export",
        action_input={"schema_version": 1, "export_ref": export_ref},
    )
    assert readback.readback is True
    assert readback.record == outcome.record
    assert readback.result is not None
    assert readback.result.workflow_run == outcome.result.workflow_run
    assert readback.result.receipt == outcome.result.receipt
    assert render_calls == 1
    assert drift_calls == 1
    assert (
        media_operation_status(root, "act_media_export")
        == outcome.record
    )


def test_public_approve_export_render_failure_is_closed_and_does_not_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _workflow_project(tmp_path)
    adopted = _advance_to_export_review(root)
    export_ref = adopted.status["presented_subjects"]["export_ref"]
    store = WorkflowStore(root)
    project_before = (root / "project.json").read_bytes()
    run_path = store.runs_path / "wfr_test.json"
    run_before = run_path.read_bytes()
    renders_before = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in (root / "renders").rglob("*")
        if path.is_file()
    }
    render_calls = 0

    monkeypatch.setattr(
        media_operations_module,
        "_load_persistent_runtime",
        lambda: type(
            "Runtime",
            (),
            {
                "ffmpeg": ToolResolution(
                    "ffmpeg", "/fixture/ffmpeg", "fixture"
                ),
                "ffprobe": ToolResolution(
                    "ffprobe", "/fixture/ffprobe", "fixture"
                ),
            },
        )(),
    )
    monkeypatch.setattr(
        workflows_module,
        "prepare_render_plan",
        lambda project_path, **kwargs: _fixture_render_plan(
            project_path,
            edit_version_id=kwargs["edit_version_id"],
            expected_revision=kwargs["expected_revision"],
            render_id=kwargs["render_id"],
        ),
    )

    def fail_render(
        _project_path: Path,
        _plan: RenderPlan,
        *,
        expected_revision: int,
        output_path: Path,
        manifest_path: Path,
        cancel_requested: object = None,
        after_output_published: Callable[[], None] | None = None,
        phase_callback: Callable[[str], None] | None = None,
    ) -> tuple[RenderResult, dict[str, object]]:
        nonlocal render_calls
        del (
            expected_revision,
            manifest_path,
            cancel_requested,
            after_output_published,
        )
        render_calls += 1
        if phase_callback is not None:
            phase_callback("render_encoding")
        output_path.write_bytes(b"fixture-partial")
        raise RuntimeError(
            "/private/render.mp4 stderr token=secret"
        )

    monkeypatch.setattr(
        workflows_module,
        "execute_prepared_render_to_paths",
        fail_render,
    )
    arguments = {
        "project_path": str(root),
        "run_id": "wfr_test",
        "action_id": "act_public_render_failure",
        "action": "approve_export",
        "input": {"schema_version": 1, "export_ref": export_ref},
    }

    failed = _public_mcp("workflow_action", arguments)
    assert failed["error"] == {"code": "render_operation_failed"}
    assert render_calls == 1
    status = _public_mcp(
        "media_operation_status",
        {
            "project_path": str(root),
            "operation_id": "act_public_render_failure",
        },
    )
    assert status["media_operation"]["status"] == "failed"
    encoded = json.dumps(status)
    assert "/private/" not in encoded
    assert "stderr" not in encoded
    assert "token=secret" not in encoded

    readback = _public_mcp("workflow_action", arguments)
    assert readback["operation_readback"] is True
    assert readback["media_operation"] == status["media_operation"]
    assert render_calls == 1
    assert (root / "project.json").read_bytes() == project_before
    assert run_path.read_bytes() == run_before
    assert {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in (root / "renders").rglob("*")
        if path.is_file()
    } == renders_before
    assert not (
        store.receipts_path / "act_public_render_failure.json"
    ).exists()
    assert not (
        store.transactions_path / "act_public_render_failure.json"
    ).exists()


def test_media_operation_multisource_export_preserves_schema_two_three_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _workflow_project(tmp_path)
    adopted = _advance_multi_to_export_review(root)
    export_ref = adopted.status["presented_subjects"]["export_ref"]
    monkeypatch.setattr(
        media_operations_module,
        "_load_persistent_runtime",
        lambda: type(
            "Runtime",
            (),
            {
                "ffmpeg": ToolResolution(
                    "ffmpeg", "/fixture/ffmpeg", "fixture"
                ),
                "ffprobe": ToolResolution(
                    "ffprobe", "/fixture/ffprobe", "fixture"
                ),
            },
        )(),
    )

    def prepare(project_path: Path, **kwargs: object) -> MultiSourceRenderPlan:
        return _fixture_multi_render_plan(
            project_path,
            edit_version_id=str(kwargs["edit_version_id"]),
            expected_revision=int(kwargs["expected_revision"]),
            render_id=str(kwargs["render_id"]),
        )

    def execute(
        _project_path: Path,
        plan: MultiSourceRenderPlan,
        *,
        expected_revision: int,
        output_path: Path,
        manifest_path: Path,
        cancel_requested: object = None,
        after_output_published: Callable[[], None] | None = None,
        phase_callback: Callable[[str], None] | None = None,
    ) -> tuple[RenderResult, dict[str, object]]:
        del expected_revision, cancel_requested
        if phase_callback is not None:
            phase_callback("render_encoding")
        output_path.write_bytes(b"fixture-multi-render")
        if after_output_published is not None:
            after_output_published()
        if phase_callback is not None:
            phase_callback("render_verifying")
        manifest = _fixture_multi_render_manifest(plan)
        write_new_json(manifest_path, manifest)
        return (
            RenderResult(
                plan.render_id,
                plan.output_relative_path,
                plan.manifest_relative_path,
                {"duration": True},
            ),
            manifest,
        )

    monkeypatch.setattr(workflows_module, "prepare_render_plan", prepare)
    monkeypatch.setattr(
        workflows_module,
        "execute_prepared_render_to_paths",
        execute,
    )
    outcome = run_approve_export_operation(
        root,
        run_id="wfr_test",
        action_id="act_media_multi_export",
        action_input={"schema_version": 1, "export_ref": export_ref},
    )
    assert outcome.record.status == "succeeded"
    result = outcome.record.result_ref
    assert result is not None
    assert result.render_plan_ref.schema_version == 2
    assert result.manifest_ref.schema_version == 3
    assert result.approve_export_receipt_ref.action_id == (
        "act_media_multi_export"
    )
    stored_run = WorkflowStore(root).read_run("wfr_test")
    assert stored_run.lifecycle == "completed"
    assert stored_run.artifact_refs["render"] == ArtifactRef(
        result.render_plan_ref.artifact_id,
        2,
        result.render_plan_ref.content_hash,
    )
    assert outcome.result is not None
    assert (
        WorkflowStore(root).read_receipt(
            "act_media_multi_export",
            run_id="wfr_test",
        )
        == outcome.result.receipt
    )
    assert not (
        root
        / "workflow"
        / "transactions"
        / "act_media_multi_export.json"
    ).exists()
    assert {
        output.kind: output.schema_version
        for output in outcome.result.receipt.output_refs
    } == {"mp4": 1, "manifest": 3}


def test_media_operation_multisource_run_published_failure_recovers_exact_before(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _workflow_project(tmp_path)
    adopted = _advance_multi_to_export_review(root)
    export_ref = adopted.status["presented_subjects"]["export_ref"]
    monkeypatch.setattr(
        media_operations_module,
        "_load_persistent_runtime",
        lambda: type(
            "Runtime",
            (),
            {
                "ffmpeg": ToolResolution(
                    "ffmpeg", "/fixture/ffmpeg", "fixture"
                ),
                "ffprobe": ToolResolution(
                    "ffprobe", "/fixture/ffprobe", "fixture"
                ),
            },
        )(),
    )
    monkeypatch.setattr(
        workflows_module,
        "prepare_render_plan",
        lambda project_path, **kwargs: _fixture_multi_render_plan(
            project_path,
            edit_version_id=str(kwargs["edit_version_id"]),
            expected_revision=int(kwargs["expected_revision"]),
            render_id=str(kwargs["render_id"]),
        ),
    )

    def execute(
        _project_path: Path,
        plan: MultiSourceRenderPlan,
        *,
        expected_revision: int,
        output_path: Path,
        manifest_path: Path,
        cancel_requested: object = None,
        after_output_published: Callable[[], None] | None = None,
        phase_callback: Callable[[str], None] | None = None,
    ) -> tuple[RenderResult, dict[str, object]]:
        del expected_revision, cancel_requested
        if phase_callback is not None:
            phase_callback("render_encoding")
        output_path.write_bytes(b"fixture-multi-render")
        if after_output_published is not None:
            after_output_published()
        if phase_callback is not None:
            phase_callback("render_verifying")
        manifest = _fixture_multi_render_manifest(plan)
        write_new_json(manifest_path, manifest)
        return (
            RenderResult(
                plan.render_id,
                plan.output_relative_path,
                plan.manifest_relative_path,
                {"duration": True},
            ),
            manifest,
        )

    monkeypatch.setattr(
        workflows_module,
        "execute_prepared_render_to_paths",
        execute,
    )
    write_transaction = WorkflowStore.write_transaction

    def interrupt_after_run_published(
        store: WorkflowStore,
        marker: TransactionMarker,
    ) -> TransactionMarker:
        published = write_transaction(store, marker)
        if marker.commit_step == "run_published":
            raise KeyboardInterrupt
        return published

    monkeypatch.setattr(
        WorkflowStore,
        "write_transaction",
        interrupt_after_run_published,
    )
    with pytest.raises(KeyboardInterrupt):
        run_approve_export_operation(
            root,
            run_id="wfr_test",
            action_id="act_media_multi_recovery",
            action_input={"schema_version": 1, "export_ref": export_ref},
        )

    operation = media_operation_status(
        root,
        "act_media_multi_recovery",
    )
    assert operation.status == "interrupted"
    run_path = root / "workflow" / "runs" / "wfr_test.json"
    published_run = WorkflowRun.from_dict(
        json.loads(run_path.read_text(encoding="utf-8"))
    )
    assert published_run.lifecycle == "completed"
    assert published_run.artifact_refs["render"] is not None
    assert published_run.artifact_refs["render"].schema_version == 2
    marker_path = (
        root
        / "workflow"
        / "transactions"
        / "act_media_multi_recovery.json"
    )
    assert json.loads(marker_path.read_text(encoding="utf-8"))[
        "commit_step"
    ] == "run_published"
    assert not (
        root
        / "workflow"
        / "receipts"
        / "act_media_multi_recovery.json"
    ).exists()

    monkeypatch.setattr(
        WorkflowStore,
        "write_transaction",
        write_transaction,
    )
    recovered = workflow_status(root, "wfr_test")

    assert recovered["recovery"]["state"] == "rolled_back"
    assert recovered["workflow_run"]["stage"] == "export_review"
    assert recovered["workflow_run"]["lifecycle"] == "active"
    assert recovered["workflow_run"]["artifact_refs"]["render"] is None
    assert not marker_path.exists()
    assert not list((root / "renders").glob("*.json"))
    assert not list((root / "renders").glob("*.mp4"))
    assert (
        media_operation_status(root, "act_media_multi_recovery")
        == operation
    )


def test_media_operation_approve_export_claim_and_interrupt_preserve_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _workflow_project(tmp_path)
    adopted = _advance_to_export_review(root)
    export_ref = adopted.status["presented_subjects"]["export_ref"]
    monkeypatch.setattr(
        media_operations_module,
        "_load_persistent_runtime",
        lambda: type(
            "Runtime",
            (),
            {
                "ffmpeg": ToolResolution(
                    "ffmpeg", "/fixture/ffmpeg", "fixture"
                ),
                "ffprobe": ToolResolution(
                    "ffprobe", "/fixture/ffprobe", "fixture"
                ),
            },
        )(),
    )
    monkeypatch.setattr(
        workflows_module,
        "prepare_render_plan",
        lambda project_path, **kwargs: _fixture_render_plan(
            project_path,
            edit_version_id=kwargs["edit_version_id"],
            expected_revision=kwargs["expected_revision"],
            render_id=kwargs["render_id"],
        ),
    )
    render_calls = 0

    def interrupt(
        _project_path: Path,
        plan: RenderPlan,
        *,
        expected_revision: int,
        output_path: Path,
        manifest_path: Path,
        cancel_requested: object = None,
        after_output_published: Callable[[], None] | None = None,
        phase_callback: Callable[[str], None] | None = None,
    ) -> tuple[RenderResult, dict[str, object]]:
        nonlocal render_calls
        del (
            plan,
            expected_revision,
            manifest_path,
            cancel_requested,
            after_output_published,
        )
        render_calls += 1
        if phase_callback is not None:
            phase_callback("render_encoding")
        output_path.write_bytes(b"owned-partial")
        raise KeyboardInterrupt

    monkeypatch.setattr(
        workflows_module,
        "execute_prepared_render_to_paths",
        interrupt,
    )
    action_input = {"schema_version": 1, "export_ref": export_ref}
    project_id = ProjectStore(root).load().project_id
    operation_store = MediaOperationStore(root, project_id)
    with project_export_claim(root), pytest.raises(WorkflowError) as claim:
        run_approve_export_operation(
            root,
            run_id="wfr_test",
            action_id="act_media_interrupt",
            action_input=action_input,
        )
    assert claim.value.code == "workflow_export_in_progress"
    assert operation_store.read("act_media_interrupt") is None
    assert render_calls == 0

    with pytest.raises(KeyboardInterrupt):
        run_approve_export_operation(
            root,
            run_id="wfr_test",
            action_id="act_media_interrupt",
            action_input=action_input,
        )
    record = media_operation_status(root, "act_media_interrupt")
    assert record.status == "interrupted"
    assert record.result_ref is None
    assert record.error is not None
    assert record.error.responsibility == "host"
    assert record.error.action == "interrupt_media_operation"
    assert WorkflowStore(root).read_run("wfr_test").stage == "export_review"
    staging = [
        path
        for path in (root / "workflow" / "export-staging").iterdir()
        if path.name != ".claim.lock"
    ]
    assert len(staging) == 1
    owner = json.loads((staging[0] / "owner.json").read_text(encoding="utf-8"))
    assert owner["action_id"] == "act_media_interrupt"
    assert owner["writing_kind"] == "mp4"
    assert not list((root / "workflow" / "transactions").glob("*.json"))

    readback = run_approve_export_operation(
        root,
        run_id="wfr_test",
        action_id="act_media_interrupt",
        action_input=action_input,
    )
    assert readback.record == record
    assert readback.readback is True
    assert render_calls == 1

    changed = copy.deepcopy(action_input)
    changed["export_ref"]["content_hash"] = "0" * 64
    with pytest.raises(MediaOperationError) as conflict:
        run_approve_export_operation(
            root,
            run_id="wfr_test",
            action_id="act_media_interrupt",
            action_input=changed,
        )
    assert conflict.value.code == "operation_input_conflict"

    def finish(
        _project_path: Path,
        plan: RenderPlan,
        *,
        expected_revision: int,
        output_path: Path,
        manifest_path: Path,
        cancel_requested: object = None,
        after_output_published: Callable[[], None] | None = None,
        phase_callback: Callable[[str], None] | None = None,
    ) -> tuple[RenderResult, dict[str, object]]:
        nonlocal render_calls
        del expected_revision, cancel_requested
        render_calls += 1
        if phase_callback is not None:
            phase_callback("render_encoding")
        output_path.write_bytes(b"fixture-interrupted-rerun")
        if after_output_published is not None:
            after_output_published()
        if phase_callback is not None:
            phase_callback("render_verifying")
        manifest = _fixture_render_manifest(plan)
        write_new_json(manifest_path, manifest)
        return (
            RenderResult(
                plan.render_id,
                plan.output_relative_path,
                plan.manifest_relative_path,
                {"duration": True},
            ),
            manifest,
        )

    monkeypatch.setattr(
        workflows_module,
        "execute_prepared_render_to_paths",
        finish,
    )
    rerun = run_approve_export_operation(
        root,
        run_id="wfr_test",
        action_id="act_media_interrupt_rerun",
        action_input=action_input,
    )
    assert rerun.record.status == "succeeded"
    assert render_calls == 2
    assert operation_store.read("act_media_interrupt") == record
    assert [
        path.name
        for path in (root / "workflow" / "export-staging").iterdir()
    ] == [".claim.lock"]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_hard_exit_render_orphan_converges_then_explicit_new_id_reruns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _workflow_project(tmp_path)
    adopted = _advance_to_export_review(root)
    export_ref = adopted.status["presented_subjects"]["export_ref"]
    monkeypatch.setattr(
        media_operations_module,
        "_load_persistent_runtime",
        lambda: type(
            "Runtime",
            (),
            {
                "ffmpeg": ToolResolution(
                    "ffmpeg", "/fixture/ffmpeg", "fixture"
                ),
                "ffprobe": ToolResolution(
                    "ffprobe", "/fixture/ffprobe", "fixture"
                ),
            },
        )(),
    )
    monkeypatch.setattr(
        workflows_module,
        "prepare_render_plan",
        lambda project_path, **kwargs: _fixture_render_plan(
            project_path,
            edit_version_id=kwargs["edit_version_id"],
            expected_revision=kwargs["expected_revision"],
            render_id=kwargs["render_id"],
        ),
    )
    worker_marker = tmp_path / "hard-exit-render-worker"

    def hard_exit(
        _project_path: Path,
        _plan: RenderPlan,
        *,
        expected_revision: int,
        output_path: Path,
        manifest_path: Path,
        cancel_requested: object = None,
        after_output_published: Callable[[], None] | None = None,
        phase_callback: Callable[[str], None] | None = None,
    ) -> tuple[RenderResult, dict[str, object]]:
        del (
            expected_revision,
            manifest_path,
            cancel_requested,
            after_output_published,
        )
        if phase_callback is not None:
            phase_callback("render_encoding")
        output_path.write_bytes(b"owned-hard-exit-partial")
        worker_marker.write_bytes(b"hard-exit")
        os._exit(0)

    monkeypatch.setattr(
        workflows_module,
        "execute_prepared_render_to_paths",
        hard_exit,
    )
    action_input = {"schema_version": 1, "export_ref": export_ref}
    child = os.fork()
    if child == 0:
        run_approve_export_operation(
            root,
            run_id="wfr_test",
            action_id="act_hard_exit_render",
            action_input=action_input,
        )
        os._exit(90)
    _pid, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert worker_marker.read_bytes() == b"hard-exit"

    interrupted = media_operation_status(
        root, "act_hard_exit_render"
    )
    assert interrupted.status == "interrupted"
    assert interrupted.error is not None
    assert interrupted.error.responsibility == "roughcut_core"
    staging = [
        path
        for path in (root / "workflow" / "export-staging").iterdir()
        if path.name != ".claim.lock"
    ]
    assert len(staging) == 1

    def finish(
        _project_path: Path,
        plan: RenderPlan,
        *,
        expected_revision: int,
        output_path: Path,
        manifest_path: Path,
        cancel_requested: object = None,
        after_output_published: Callable[[], None] | None = None,
        phase_callback: Callable[[str], None] | None = None,
    ) -> tuple[RenderResult, dict[str, object]]:
        del expected_revision, cancel_requested
        if phase_callback is not None:
            phase_callback("render_encoding")
        output_path.write_bytes(b"fixture-hard-exit-rerun")
        if after_output_published is not None:
            after_output_published()
        if phase_callback is not None:
            phase_callback("render_verifying")
        manifest = _fixture_render_manifest(plan)
        write_new_json(manifest_path, manifest)
        worker_marker.write_bytes(b"rerun")
        return (
            RenderResult(
                plan.render_id,
                plan.output_relative_path,
                plan.manifest_relative_path,
                {"duration": True},
            ),
            manifest,
        )

    monkeypatch.setattr(
        workflows_module,
        "execute_prepared_render_to_paths",
        finish,
    )
    rerun = run_approve_export_operation(
        root,
        run_id="wfr_test",
        action_id="act_hard_exit_render_rerun",
        action_input=action_input,
    )
    assert rerun.record.status == "succeeded"
    assert worker_marker.read_bytes() == b"rerun"
    assert MediaOperationStore(
        root, ProjectStore(root).load().project_id
    ).read("act_hard_exit_render") == interrupted
    assert [
        path.name
        for path in (root / "workflow" / "export-staging").iterdir()
    ] == [".claim.lock"]


@pytest.mark.parametrize(
    ("failure", "responsibility"),
    (
        (
            RuntimeError("/private/render.mp4 stderr token=secret"),
            "roughcut_core",
        ),
        (
            FFmpegRenderError("/private/render.mp4 stderr token=secret"),
            "ffmpeg_render",
        ),
    ),
)
def test_failed_render_explicit_new_id_cleans_only_exact_tracked_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    responsibility: str,
) -> None:
    root = _workflow_project(tmp_path)
    adopted = _advance_to_export_review(root)
    export_ref = adopted.status["presented_subjects"]["export_ref"]
    monkeypatch.setattr(
        media_operations_module,
        "_load_persistent_runtime",
        lambda: type(
            "Runtime",
            (),
            {
                "ffmpeg": ToolResolution(
                    "ffmpeg", "/fixture/ffmpeg", "fixture"
                ),
                "ffprobe": ToolResolution(
                    "ffprobe", "/fixture/ffprobe", "fixture"
                ),
            },
        )(),
    )
    monkeypatch.setattr(
        workflows_module,
        "prepare_render_plan",
        lambda project_path, **kwargs: _fixture_render_plan(
            project_path,
            edit_version_id=kwargs["edit_version_id"],
            expected_revision=kwargs["expected_revision"],
            render_id=kwargs["render_id"],
        ),
    )
    calls = 0

    def fail_render(
        _project_path: Path,
        _plan: RenderPlan,
        *,
        expected_revision: int,
        output_path: Path,
        manifest_path: Path,
        cancel_requested: object = None,
        after_output_published: Callable[[], None] | None = None,
        phase_callback: Callable[[str], None] | None = None,
    ) -> tuple[RenderResult, dict[str, object]]:
        nonlocal calls
        del (
            expected_revision,
            manifest_path,
            cancel_requested,
            after_output_published,
        )
        calls += 1
        if phase_callback is not None:
            phase_callback("render_encoding")
        output_path.write_bytes(b"owned-failed-partial")
        owner = json.loads(
            (output_path.parent / "owner.json").read_text(encoding="utf-8")
        )
        renderer_workspace = (
            output_path.parent
            / workflow_renderer_workspace_name(
                owner["project_id"],
                owner["run_id"],
                owner["action_id"],
                owner["input_hash"],
                owner["staging_id"],
                owner["export_basis_id"],
            )
        )
        renderer_workspace.mkdir()
        (renderer_workspace / "filter-complex.txt").write_text(
            "[fixture]\n",
            encoding="utf-8",
        )
        (renderer_workspace / "candidate.mp4").write_bytes(
            b"owned-renderer-partial"
        )
        raise failure

    monkeypatch.setattr(
        workflows_module,
        "execute_prepared_render_to_paths",
        fail_render,
    )
    action_input = {"schema_version": 1, "export_ref": export_ref}
    suffix = "core" if responsibility == "roughcut_core" else "ffmpeg"
    old_action_id = f"act_failed_render_{suffix}"
    new_action_id = f"act_rerun_render_{suffix}"
    with pytest.raises(type(failure)):
        run_approve_export_operation(
            root,
            run_id="wfr_test",
            action_id=old_action_id,
            action_input=action_input,
        )

    project_id = ProjectStore(root).load().project_id
    operation_store = MediaOperationStore(root, project_id)
    failed = media_operation_status(root, old_action_id)
    assert failed.status == "failed"
    assert failed.error is not None
    assert failed.error.responsibility == responsibility
    encoded = json.dumps(failed.to_dict())
    assert "/private" not in encoded
    assert "stderr" not in encoded
    assert "secret" not in encoded
    staging = [
        path
        for path in (root / "workflow" / "export-staging").iterdir()
        if path.name != ".claim.lock"
    ]
    assert len(staging) == 1

    if responsibility == "roughcut_core":
        owner_path = staging[0] / "owner.json"
        exact_owner = json.loads(owner_path.read_text(encoding="utf-8"))
        changed_owner = copy.deepcopy(exact_owner)
        changed_owner["export_basis_id"] = f"wfb_export_{'0' * 64}"
        workflows_module._write_json_atomically(
            owner_path, changed_owner
        )
        with pytest.raises(WorkflowError) as changed_basis:
            run_approve_export_operation(
                root,
                run_id="wfr_test",
                action_id=new_action_id,
                action_input=action_input,
            )
        assert changed_basis.value.code == "workflow_recovery_conflict"
        assert calls == 1
        assert staging[0].exists()
        assert operation_store.read(new_action_id) is None
        workflows_module._write_json_atomically(owner_path, exact_owner)

        stale_input = copy.deepcopy(action_input)
        stale_input["export_ref"]["content_hash"] = "0" * 64
        with pytest.raises(WorkflowError) as stale_request:
            run_approve_export_operation(
                root,
                run_id="wfr_test",
                action_id=new_action_id,
                action_input=stale_input,
            )
        assert stale_request.value.code == "workflow_recovery_conflict"
        assert calls == 1
        assert staging[0].exists()
        assert operation_store.read(new_action_id) is None

        with operation_store.writer(
            old_action_id, create=False
        ) as acquired:
            assert acquired
            with pytest.raises(WorkflowError) as live:
                run_approve_export_operation(
                    root,
                    run_id="wfr_test",
                    action_id=new_action_id,
                    action_input=action_input,
                )
        assert live.value.code == "workflow_recovery_conflict"
        assert calls == 1
        assert staging[0].exists()
        assert operation_store.read(new_action_id) is None

        for controlled in ("transactions", "receipts"):
            blocker = (
                root
                / "workflow"
                / controlled
                / f"{old_action_id}.json"
            )
            blocker.write_text("{}\n", encoding="utf-8")
            with pytest.raises(WorkflowError) as authoritative:
                run_approve_export_operation(
                    root,
                    run_id="wfr_test",
                    action_id=new_action_id,
                    action_input=action_input,
                )
            assert authoritative.value.code == "workflow_recovery_conflict"
            assert calls == 1
            assert staging[0].exists()
            assert operation_store.read(new_action_id) is None
            blocker.unlink()

    def succeed_render(
        _project_path: Path,
        plan: RenderPlan,
        *,
        expected_revision: int,
        output_path: Path,
        manifest_path: Path,
        cancel_requested: object = None,
        after_output_published: Callable[[], None] | None = None,
        phase_callback: Callable[[str], None] | None = None,
    ) -> tuple[RenderResult, dict[str, object]]:
        nonlocal calls
        del expected_revision, cancel_requested
        calls += 1
        if phase_callback is not None:
            phase_callback("render_encoding")
        output_path.write_bytes(b"fixture-rerun-render")
        if after_output_published is not None:
            after_output_published()
        if phase_callback is not None:
            phase_callback("render_verifying")
        manifest = _fixture_render_manifest(plan)
        write_new_json(manifest_path, manifest)
        return (
            RenderResult(
                plan.render_id,
                plan.output_relative_path,
                plan.manifest_relative_path,
                {"duration": True},
            ),
            manifest,
        )

    monkeypatch.setattr(
        workflows_module,
        "execute_prepared_render_to_paths",
        succeed_render,
    )
    rerun = run_approve_export_operation(
        root,
        run_id="wfr_test",
        action_id=new_action_id,
        action_input=action_input,
    )
    assert rerun.record.status == "succeeded"
    assert calls == 2
    assert operation_store.read(old_action_id) == failed
    assert WorkflowStore(root).read_run("wfr_test").lifecycle == "completed"
    assert rerun.result is not None
    assert WorkflowStore(root).read_receipt(
        new_action_id, run_id="wfr_test"
    ) == rerun.result.receipt
    assert not (
        root / "workflow" / "transactions" / f"{new_action_id}.json"
    ).exists()
    assert [
        path.name
        for path in (root / "workflow" / "export-staging").iterdir()
    ] == [".claim.lock"]


def test_media_operation_approve_export_stale_ref_fails_before_record(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    adopted = _advance_to_export_review(root)
    stale = copy.deepcopy(adopted.status["presented_subjects"]["export_ref"])
    stale["content_hash"] = "0" * 64
    with pytest.raises(WorkflowError) as raised:
        run_approve_export_operation(
            root,
            run_id="wfr_test",
            action_id="act_media_stale",
            action_input={"schema_version": 1, "export_ref": stale},
        )
    assert raised.value.code == "workflow_subject_mismatch"
    store = MediaOperationStore(root, ProjectStore(root).load().project_id)
    assert store.read("act_media_stale") is None
    assert WorkflowStore(root).read_run("wfr_test").stage == "export_review"

@pytest.fixture(autouse=True)
def _validated_media_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        media_operations_module,
        "_validate_media_runtime",
        lambda _runtime: ("ffmpeg version fixture", "ffprobe version fixture"),
    )
