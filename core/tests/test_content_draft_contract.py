from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import roughcut.mcp
from roughcut.adapters.artifact_store import write_new_json
from roughcut.adapters.project_store import ProjectStore
from roughcut.application.agent_context import read_agent_context
from roughcut.application.health import TOOL_SCHEMA_VERSION
from roughcut.application.projects import create_project
from roughcut.domain.brief import EditBrief
from roughcut.domain.project import (
    ImportMode,
    MediaProbe,
    SourceAsset,
    SourceFingerprint,
)
from roughcut.domain.transcript import (
    TimedTranscript,
    TranscriptProvenance,
    TranscriptSegment,
)


def _project(tmp_path: Path, name: str) -> tuple[Path, list[dict[str, str]], str]:
    root = tmp_path / name
    project = create_project(root, "Contract")
    source = SourceAsset(
        source_id="src_contract",
        kind="audio",
        display_name="旁白合同.wav",
        import_mode=ImportMode.LINKED,
        locator={"absolute_path": "/fixture/contract.wav"},
        fingerprint=SourceFingerprint(1, 2, "fixture"),
        probe=MediaProbe(
            duration_ticks=240_000,
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
        transcript_version_id="tr_contract",
        source_id=source.source_id,
        parent_version_id=None,
        provenance=TranscriptProvenance(
            backend="fixture",
            package_version="fixture",
            models={},
            parameters={},
            raw_result_path="raw-asr/src_contract/fixture.json",
            started_at="fixture",
            completed_at="fixture",
            exit_status=0,
        ),
        language="zh-CN",
        segments=(
            TranscriptSegment(
                segment_id="seg_contract",
                start_ticks=0,
                end_ticks=120_000,
                original_text="合同原话。",
                corrected_text=None,
                local_speaker_id=None,
                person_id=None,
                confidence=None,
                fine_units=(),
                editorial_mark="unmarked",
            ),
        ),
    )
    brief = EditBrief("brief_contract", "合同", 600_000, ("重点",), True)
    write_new_json(root / "briefs" / "brief_contract.json", brief.to_dict())
    write_new_json(
        root / "transcripts" / "src_contract" / "tr_contract.json",
        transcript.to_dict(),
    )
    prepared = replace(
        project,
        project_id="proj_content_contract",
        revision=1,
        created_at="fixture",
        updated_at="fixture",
        sources=(source,),
        active_transcript_versions={"src_contract": "tr_contract"},
        active_brief_id=brief.brief_id,
    )
    ProjectStore(root).save(prepared, expected_revision=0)
    context_hash = read_agent_context(
        root,
        source_id="src_contract",
        transcript_version_id="tr_contract",
        brief_id=brief.brief_id,
        expected_revision=1,
        offset=0,
        limit=1,
    ).context_hash
    return (
        root,
        [{"source_id": "src_contract", "transcript_version_id": "tr_contract"}],
        context_hash,
    )


def _blocks() -> list[dict[str, object]]:
    return [
        {
            "block_id": "section_contract",
            "kind": "section_title",
            "title": "开场",
        },
        {
            "block_id": "block_contract",
            "kind": "source_excerpt",
            "refs": [
                {
                    "source_id": "src_contract",
                    "transcript_version_id": "tr_contract",
                    "segment_id": "seg_contract",
                    "start_ticks": 0,
                    "end_ticks": 120_000,
                }
            ],
            "canonical_text": "合同原话。",
        }
    ]


def _cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "roughcut.cli", *arguments, "--json"],
        check=False,
        capture_output=True,
        text=True,
    )


def _stdio(name: str, arguments: dict[str, object]) -> tuple[dict[str, Any], bool]:
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    process = subprocess.run(
        [sys.executable, "-m", "roughcut.mcp"],
        input=json.dumps(request, ensure_ascii=False) + "\n",
        check=False,
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0
    assert process.stderr == ""
    result = json.loads(process.stdout)["result"]
    return result["structuredContent"], result.get("isError", False)


def test_content_draft_tools_use_current_tool_schema() -> None:
    assert TOOL_SCHEMA_VERSION == 32
    assert {tool["name"] for tool in roughcut.mcp.TOOLS} >= {
        "content_draft_create",
        "content_draft_revise_scoped",
        "content_draft_read",
        "content_draft_confirm",
        "content_draft_propose",
    }


def test_real_cli_and_stdio_mcp_content_draft_writes_require_workflow(
    tmp_path: Path,
) -> None:
    cli_root, bindings, context_hash = _project(tmp_path, "cli")
    mcp_root, _, _ = _project(tmp_path, "mcp")
    cli_create = _cli(
        "content-draft-create",
        "--project",
        str(cli_root),
        "--display-title",
        "合同初稿",
        "--source-bindings-json",
        json.dumps(bindings),
        "--brief-id",
        "brief_contract",
        "--context-hash",
        context_hash,
        "--blocks-json",
        json.dumps(_blocks(), ensure_ascii=False),
        "--expected-revision",
        "1",
    )
    project_before = (cli_root / "project.json").read_bytes()
    assert cli_create.returncode == 2
    assert cli_create.stderr == ""
    cli_payload = json.loads(cli_create.stdout)
    mcp_payload, is_error = _stdio(
        "content_draft_create",
        {
            "project_path": str(mcp_root),
            "parent_draft_id": None,
            "display_title": "合同初稿",
            "source_bindings": bindings,
            "brief_id": "brief_contract",
            "context_hash": context_hash,
            "blocks": _blocks(),
            "expected_revision": 1,
        },
    )
    assert is_error is True
    assert cli_payload == mcp_payload
    assert cli_payload["error"] == {"code": "workflow_required"}
    assert (cli_root / "project.json").read_bytes() == project_before
    assert not (cli_root / "workflow").exists()
    assert not (mcp_root / "workflow").exists()
    tool = next(
        item for item in roughcut.mcp.TOOLS if item["name"] == "content_draft_create"
    )
    assert tool["inputSchema"]["properties"]["display_title"] == {
        "type": ["string", "null"],
        "minLength": 1,
        "maxLength": 80,
    }


def test_real_cli_and_stdio_mcp_content_draft_failures_match(tmp_path: Path) -> None:
    root, bindings, context_hash = _project(tmp_path, "failure")
    cli = _cli(
        "content-draft-create",
        "--project",
        str(root),
        "--source-bindings-json",
        json.dumps(bindings),
        "--brief-id",
        "brief_contract",
        "--context-hash",
        "0" * 64,
        "--blocks-json",
        json.dumps(_blocks()),
        "--expected-revision",
        "1",
    )
    mcp, is_error = _stdio(
        "content_draft_create",
        {
            "project_path": str(root),
            "source_bindings": bindings,
            "brief_id": "brief_contract",
            "context_hash": "0" * 64,
            "blocks": _blocks(),
            "expected_revision": 1,
        },
    )
    assert context_hash != "0" * 64
    assert cli.returncode == 2
    assert cli.stderr == ""
    assert is_error is True
    assert json.loads(cli.stdout) == mcp
    assert mcp["error"] == {"code": "workflow_required"}

    missing = _cli("content-draft-read", "--project", str(root))
    missing_mcp, is_error = _stdio(
        "content_draft_read", {"project_path": str(root)}
    )
    assert missing.returncode == 2
    assert is_error is True
    assert json.loads(missing.stdout) == missing_mcp
    assert missing_mcp["error"] == {"code": "invalid_arguments"}
