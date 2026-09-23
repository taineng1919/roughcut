from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from roughcut import __version__
from roughcut.adapters.artifact_store import write_new_json
from roughcut.adapters.project_store import ProjectStore
from roughcut.application.projects import create_project
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

ROOT = Path(__file__).resolve().parents[3]
BOOTSTRAP = ROOT / "scripts" / "bootstrap.py"
BUILD_HOST_PACKAGE = ROOT / "scripts" / "build_host_package.py"
CANONICAL_SKILLS = {
    name: ROOT / "agent-skill" / "skills" / name / "SKILL.md"
    for name in (
        "roughcut",
        "roughcut-basics",
        "create-roughcut",
        "revise-roughcut",
        "render-roughcut",
    )
}


def run(*command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=ROOT, check=False, capture_output=True, text=True)


def _workflow_project(tmp_path: Path) -> Path:
    root = tmp_path / "host-boundary-workflow"
    project = create_project(root, "Host boundary workflow")
    source = SourceAsset(
        source_id="src_host",
        kind="audio",
        display_name="host.wav",
        import_mode=ImportMode.LINKED,
        locator={"absolute_path": "/fixture/host.wav"},
        fingerprint=SourceFingerprint(100, 1, "fixture-host"),
        probe=MediaProbe(
            duration_ticks=120_000,
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
        transcript_version_id="tr_host",
        source_id=source.source_id,
        parent_version_id=None,
        provenance=TranscriptProvenance(
            backend="fixture",
            package_version="fixture",
            models={},
            parameters={},
            raw_result_path="raw-asr/src_host/fixture.json",
            started_at="fixture",
            completed_at="fixture",
            exit_status=0,
        ),
        language="zh-CN",
        segments=(
            TranscriptSegment(
                segment_id="seg_host",
                start_ticks=0,
                end_ticks=120_000,
                original_text="真实 Host 边界测试。",
                corrected_text=None,
                local_speaker_id=None,
                person_id=None,
                confidence=None,
                fine_units=(),
                editorial_mark="unmarked",
            ),
        ),
    )
    write_new_json(
        root / "transcripts" / source.source_id / "tr_host.json",
        transcript.to_dict(),
    )
    ProjectStore(root).save(
        replace(
            project,
            revision=1,
            sources=(source,),
            active_transcript_versions={source.source_id: "tr_host"},
        ),
        expected_revision=0,
    )
    return root


def test_codex_host_boundary_runs_schema32_three_section_workflow(tmp_path: Path) -> None:
    install_dir = tmp_path / "roughcut-runtime"
    first = run(sys.executable, str(BOOTSTRAP), "--install-dir", str(install_dir), "--json")
    second = run(sys.executable, str(BOOTSTRAP), "--install-dir", str(install_dir), "--json")

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    first_payload = json.loads(first.stdout)
    second_payload = json.loads(second.stdout)
    assert first_payload["installed"] is True
    assert second_payload["installed"] is False
    assert first_payload["core_action"] == "installed"
    assert second_payload["core_action"] == "reused"
    mcp_command = first_payload["mcp_command"]
    assert isinstance(mcp_command, str)
    assert first_payload["runtime_binding"] == {
        "path": str(install_dir.resolve() / "runtime.json"),
        "configured": False,
        "status": "unconfigured",
    }

    project_root = _workflow_project(tmp_path)
    mcp = subprocess.Popen(
        [mcp_command],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert mcp.stdin is not None
    assert mcp.stdout is not None
    assert mcp.stderr is not None
    request_id = 0

    def rpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        nonlocal request_id
        request_id += 1
        request: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            request["params"] = params
        mcp.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        mcp.stdin.flush()
        response = json.loads(mcp.stdout.readline())
        assert response["id"] == request_id
        return response

    def tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = rpc("tools/call", {"name": name, "arguments": arguments})
        result = response["result"]
        assert result["isError"] is False, response
        payload = result["structuredContent"]
        assert payload["ok"] is True, payload
        return payload

    initialize = rpc("initialize", {})
    assert initialize["result"]["protocolVersion"] == "2025-03-26"
    mcp.stdin.write(
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
    )
    mcp.stdin.flush()
    listed = rpc("tools/list", {})["result"]
    assert listed["toolSchemaVersion"] == 32
    workflow_tool = next(item for item in listed["tools"] if item["name"] == "workflow_action")
    submit_branch = next(
        branch
        for branch in workflow_tool["inputSchema"]["oneOf"]
        if branch["properties"]["action"].get("const") == "submit_outline"
    )
    sections_schema = submit_branch["properties"]["input"]["properties"]["sections"]
    assert sections_schema["type"] == "array"
    assert sections_schema["minItems"] == 1
    assert "maxItems" not in sections_schema

    health_payload = tool("health", {})
    assert health_payload["tool_schema_version"] == 32
    started = tool(
        "workflow_start",
        {
            "project_path": str(project_root),
            "run_id": "wfr_host_smoke",
            "ordered_source_ids": ["src_host"],
        },
    )
    scope_basis = started["status"]["confirmation_bases"]["scope"]["basis"]
    scoped = tool(
        "workflow_action",
        {
            "project_path": str(project_root),
            "run_id": "wfr_host_smoke",
            "action_id": "act_host_scope",
            "action": "approve_scope",
            "input": {
                "schema_version": 1,
                "confirmation_basis": scope_basis,
                "source_authorizations": [
                    {
                        "source_id": "src_host",
                        "transcribe": False,
                        "speaker_diarization": False,
                    }
                ],
            },
        },
    )
    brief_basis = scoped["status"]["confirmation_bases"]["brief"]["basis"]
    briefed = tool(
        "workflow_action",
        {
            "project_path": str(project_root),
            "run_id": "wfr_host_smoke",
            "action_id": "act_host_brief",
            "action": "confirm_brief",
            "input": {
                "schema_version": 1,
                "confirmation_basis": brief_basis,
                "theme": "Host boundary",
                "target_duration_ticks": 120_000,
                "focus": ["public protocol"],
                "allow_reorder": False,
                "speaker_resolution_waivers": [],
            },
        },
    )
    assert briefed["status"]["next_action"] == "submit_outline"
    outlined = tool(
        "workflow_action",
        {
            "project_path": str(project_root),
            "run_id": "wfr_host_smoke",
            "action_id": "act_host_outline",
            "action": "submit_outline",
            "input": {
                "schema_version": 1,
                "title": "Host boundary",
                "opening": "开场",
                "sections": [
                    {
                        "section_id": f"section_{index}",
                        "title": f"章节 {index}",
                        "summary": f"章节 {index}",
                        "target_duration_ticks": 40_000,
                    }
                    for index in range(1, 4)
                ],
                "ending": "结尾",
                "required_content_coverage": [],
                "narration_status": "none",
            },
        },
    )
    outline_ref = outlined["status"]["presented_subjects"]["outline_ref"]
    approved = tool(
        "workflow_action",
        {
            "project_path": str(project_root),
            "run_id": "wfr_host_smoke",
            "action_id": "act_host_approve_outline",
            "action": "approve_outline",
            "input": {
                "schema_version": 1,
                "outline_ref": {
                    key: outline_ref[key]
                    for key in ("artifact_id", "schema_version", "content_hash")
                },
            },
        },
    )
    brief_ref = approved["workflow_run"]["artifact_refs"]["brief"]
    opened = tool("project_open", {"project_path": str(project_root)})
    context = tool(
        "agent_context",
        {
            "project_path": str(project_root),
            "source_id": "src_host",
            "transcript_version_id": "tr_host",
            "brief_id": brief_ref["artifact_id"],
            "expected_revision": opened["project"]["revision"],
            "offset": 0,
            "limit": 200,
        },
    )["agent_context"]
    drafted = tool(
        "workflow_action",
        {
            "project_path": str(project_root),
            "run_id": "wfr_host_smoke",
            "action_id": "act_host_draft",
            "action": "submit_draft",
            "input": {
                "schema_version": 1,
                "parent_draft_ref": None,
                "display_title": "Host boundary",
                "source_bindings": [
                    {"source_id": "src_host", "transcript_version_id": "tr_host"}
                ],
                "brief_ref": brief_ref,
                "context_hash": context["context_hash"],
                "blocks": [
                    {
                        "block_id": "block_host",
                        "kind": "source_excerpt",
                        "refs": [
                            {
                                "source_id": "src_host",
                                "transcript_version_id": "tr_host",
                                "segment_id": "seg_host",
                                "start_ticks": 0,
                                "end_ticks": 120_000,
                            }
                        ],
                        "canonical_text": "真实 Host 边界测试。",
                    }
                ],
                "scoped_mutable_block_ids": [],
            },
        },
    )
    assert drafted["workflow_run"]["stage"] == "draft_review"

    mcp.stdin.close()
    assert mcp.wait(timeout=10) == 0
    assert mcp.stderr.read() == ""

    core_name = "roughcut.exe" if Path(mcp_command).suffix == ".exe" else "roughcut"
    core_command = Path(mcp_command).with_name(core_name)
    health = subprocess.run(
        [str(core_command), "health", "--json"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert health.returncode == 0
    health_payload = json.loads(health.stdout)
    assert health_payload["schema_version"] == 1
    assert health_payload["core_version"] == __version__
    assert health_payload["tool_schema_version"] == 32
    assert health_payload["ok"] is True


def assert_skill_references_exist(output: Path) -> None:
    for skill in (output / "skills").glob("*/SKILL.md"):
        for reference in re.findall(r"`([^`]+\.md)`", skill.read_text(encoding="utf-8")):
            assert (skill.parent / reference).is_file()


def test_build_host_package_generates_the_canonical_skill(tmp_path: Path) -> None:
    output = tmp_path / "roughcut-codex"
    result = run(
        sys.executable,
        str(BUILD_HOST_PACKAGE),
        "--output",
        str(output),
        "--mcp-command",
        "/tmp/roughcut-mcp",
    )

    assert result.returncode == 0, result.stderr
    manifest = json.loads((output / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    mcp = json.loads((output / ".mcp.json").read_text(encoding="utf-8"))
    assert set(manifest) == {"name", "version", "description", "skills", "mcpServers"}
    assert mcp["mcpServers"]["roughcut"]["command"] == "/tmp/roughcut-mcp"
    for name, canonical_skill in CANONICAL_SKILLS.items():
        assert (output / "skills" / name / "SKILL.md").read_text(
            encoding="utf-8"
        ) == canonical_skill.read_text(encoding="utf-8")
    assert_skill_references_exist(output)


def test_codex_host_package_expects_mcp_and_five_skills() -> None:
    manifest = json.loads(
        (ROOT / "host-integrations" / "codex" / ".codex-plugin" / "plugin.json").read_text(
            encoding="utf-8"
        )
    )
    # Codex declares both MCP and Skills: it is a full MCP + 5 Skills host, not MCP-only.
    assert manifest["skills"] == "./skills/"
    assert manifest["mcpServers"] == "./.mcp.json"
    mcp = json.loads(
        (ROOT / "host-integrations" / "codex" / ".mcp.json").read_text(encoding="utf-8")
    )
    assert "roughcut" in mcp["mcpServers"]
    for name in (
        "roughcut",
        "roughcut-basics",
        "create-roughcut",
        "revise-roughcut",
        "render-roughcut",
    ):
        assert (ROOT / "agent-skill" / "skills" / name / "SKILL.md").is_file()


def test_build_host_package_requires_absolute_mcp_command(tmp_path: Path) -> None:
    missing = run(
        sys.executable,
        str(BUILD_HOST_PACKAGE),
        "--output",
        str(tmp_path / "missing-command"),
    )
    relative = run(
        sys.executable,
        str(BUILD_HOST_PACKAGE),
        "--output",
        str(tmp_path / "relative-command"),
        "--mcp-command",
        "roughcut-mcp",
    )

    assert missing.returncode == relative.returncode == 1
    assert "absolute --mcp-command" in missing.stderr
    assert "absolute --mcp-command" in relative.stderr
    assert not (tmp_path / "missing-command").exists()
    assert not (tmp_path / "relative-command").exists()


def test_build_host_package_allows_an_ignored_repository_output() -> None:
    with tempfile.TemporaryDirectory(dir=ROOT, prefix=".roughcut-host-package-test-") as temporary_dir:
        temporary_root = Path(temporary_dir)
        output = temporary_root / "package"
        result = run(
            sys.executable,
            str(BUILD_HOST_PACKAGE),
            "--output",
            str(output),
            "--mcp-command",
            "/tmp/roughcut-mcp",
        )
        skill_check = run(sys.executable, str(ROOT / "scripts" / "validate_agent_skills.py"), "--check")
        host_check = run(sys.executable, str(BUILD_HOST_PACKAGE), "--check")

        assert result.returncode == 0, result.stderr
        assert skill_check.returncode == 0, skill_check.stderr
        assert host_check.returncode == 0, host_check.stderr
        assert_skill_references_exist(output)
    assert not temporary_root.exists()


def test_failed_host_package_build_leaves_no_output_or_skill_copy() -> None:
    integration = ROOT / "host-integrations" / "codex"
    with tempfile.TemporaryDirectory(
        dir=integration, prefix=".skill-copy-test-"
    ) as temporary_dir, tempfile.TemporaryDirectory(
        dir=ROOT, prefix=".roughcut-host-package-test-"
    ) as output_parent:
        handwritten = Path(temporary_dir) / "SKILL.md"
        handwritten.write_text(
            "---\nname: copied\ndescription: copied\n---\n", encoding="utf-8"
        )
        output = Path(output_parent) / "package"

        result = run(
            sys.executable,
            str(BUILD_HOST_PACKAGE),
            "--output",
            str(output),
            "--mcp-command",
            "/tmp/roughcut-mcp",
        )

        assert result.returncode == 1
        assert "canonical Agent Skill validation failed" in result.stderr
        assert not output.exists()
    assert not Path(temporary_dir).exists()
    assert not Path(output_parent).exists()
