from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import roughcut.mcp
from roughcut.adapters.project_store import ProjectStore
from roughcut.application.health import TOOL_SCHEMA_VERSION
from roughcut.application.projects import create_project
from roughcut.domain.bindings import SourceTranscriptBinding
from roughcut.domain.project import (
    ImportMode,
    MediaProbe,
    SourceAsset,
    SourceFingerprint,
)
from roughcut.domain.transcript import (
    FineUnit,
    TimedTranscript,
    TranscriptProvenance,
    TranscriptSegment,
)


def _project(tmp_path: Path) -> tuple[Path, list[dict[str, object]]]:
    root = tmp_path / "contract project"
    project = create_project(root, "Contract")
    source = SourceAsset(
        source_id="src_fixture",
        kind="audio",
        display_name="合同 文稿.wav",
        import_mode=ImportMode.LINKED,
        locator={"absolute_path": "/private/contract.wav"},
        fingerprint=SourceFingerprint(1, 2, "secret"),
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
        transcript_version_id="tr_fixture",
        source_id=source.source_id,
        parent_version_id=None,
        provenance=TranscriptProvenance(
            backend="fixture",
            package_version="fixture",
            models={},
            parameters={},
            raw_result_path="raw-asr/src_fixture/private.json",
            started_at="fixture",
            completed_at="fixture",
            exit_status=0,
        ),
        language="zh-CN",
        segments=(
            TranscriptSegment(
                segment_id="seg_fixture",
                start_ticks=0,
                end_ticks=120_000,
                original_text="完整，原话 2026。",
                corrected_text=None,
                local_speaker_id=None,
                person_id=None,
                confidence=None,
                fine_units=(
                    FineUnit("word", "完整", 0, 40_000, None),
                    FineUnit("word", "原话", 40_000, 80_000, None),
                    FineUnit("token", "2026", 80_000, 120_000, None),
                ),
                editorial_mark="unmarked",
            ),
        ),
    )
    transcript_path = root / "transcripts" / source.source_id / "tr_fixture.json"
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text(
        json.dumps(transcript.to_dict(), ensure_ascii=False), encoding="utf-8"
    )
    ProjectStore(root).save(
        replace(
            project,
            revision=1,
            sources=(source,),
            active_transcript_versions={source.source_id: transcript.transcript_version_id},
        ),
        expected_revision=0,
    )
    return root, [SourceTranscriptBinding("src_fixture", "tr_fixture").to_dict()]


def _cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "roughcut.cli", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def _stdio(name: str, arguments: dict[str, object]) -> dict[str, object]:
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
    response = json.loads(process.stdout)
    return response["result"]["structuredContent"]  # type: ignore[no-any-return]


def test_w2_tools_remain_available_in_current_tool_schema() -> None:
    assert TOOL_SCHEMA_VERSION == 32
    assert {tool["name"] for tool in roughcut.mcp.TOOLS} >= {
        "readable_transcript_read",
        "transcript_selection_resolve",
        "markdown_export",
    }


def test_real_cli_and_stdio_mcp_read_and_selection_payloads_match(tmp_path: Path) -> None:
    root, bindings = _project(tmp_path)
    bindings_json = json.dumps(bindings, ensure_ascii=False)
    cli = _cli(
        "readable-transcript-read",
        "--project",
        str(root),
        "--source-bindings-json",
        bindings_json,
        "--expected-revision",
        "1",
        "--offset",
        "0",
        "--limit",
        "50",
        "--json",
    )
    assert cli.returncode == 0
    assert cli.stderr == ""
    cli_payload = json.loads(cli.stdout)
    mcp_payload = _stdio(
        "readable_transcript_read",
        {
            "project_path": str(root),
            "source_bindings": bindings,
            "expected_revision": 1,
            "offset": 0,
            "limit": 50,
        },
    )
    assert cli_payload == mcp_payload

    paragraph = cli_payload["readable_transcript"]["paragraphs"][0]
    selections = [{"paragraph_id": paragraph["paragraph_id"]}]
    selection_cli = _cli(
        "transcript-selection-resolve",
        "--project",
        str(root),
        "--source-bindings-json",
        bindings_json,
        "--expected-revision",
        "1",
        "--view-hash",
        cli_payload["readable_transcript"]["view_hash"],
        "--selections-json",
        json.dumps(selections),
        "--json",
    )
    assert selection_cli.returncode == 0
    selection_cli_payload = json.loads(selection_cli.stdout)
    selection_mcp_payload = _stdio(
        "transcript_selection_resolve",
        {
            "project_path": str(root),
            "source_bindings": bindings,
            "expected_revision": 1,
            "view_hash": cli_payload["readable_transcript"]["view_hash"],
            "selections": selections,
        },
    )
    assert selection_cli_payload == selection_mcp_payload

    partial = [
        {
            "paragraph_id": paragraph["paragraph_id"],
            "start_offset": 0,
            "end_offset": 3,
            "quote": "完整，",
            "occurrence": 0,
        }
    ]
    partial_cli = _cli(
        "transcript-selection-resolve",
        "--project",
        str(root),
        "--source-bindings-json",
        bindings_json,
        "--expected-revision",
        "1",
        "--view-hash",
        cli_payload["readable_transcript"]["view_hash"],
        "--selections-json",
        json.dumps(partial, ensure_ascii=False),
        "--json",
    )
    assert partial_cli.returncode == 0
    partial_cli_payload = json.loads(partial_cli.stdout)
    partial_mcp_payload = _stdio(
        "transcript_selection_resolve",
        {
            "project_path": str(root),
            "source_bindings": bindings,
            "expected_revision": 1,
            "view_hash": cli_payload["readable_transcript"]["view_hash"],
            "selections": partial,
        },
    )
    assert partial_cli_payload == partial_mcp_payload
    assert partial_cli_payload["transcript_selection"]["mode"] == "exact"
    assert partial_cli_payload["transcript_selection"]["canonical_text"] == "完整，"
    assert partial_cli_payload["transcript_selection"]["refs"][0]["end_ticks"] == 40_000


def test_real_cli_and_stdio_mcp_markdown_exports_have_equal_content(tmp_path: Path) -> None:
    root, bindings = _project(tmp_path)
    cli_output = tmp_path / "cli.md"
    mcp_output = tmp_path / "mcp.md"
    cli = _cli(
        "markdown-export",
        "--project",
        str(root),
        "--basis",
        "transcript",
        "--source-bindings-json",
        json.dumps(bindings),
        "--expected-revision",
        "1",
        "--output",
        str(cli_output),
        "--json",
    )
    assert cli.returncode == 0
    cli_payload = json.loads(cli.stdout)
    mcp_payload = _stdio(
        "markdown_export",
        {
            "project_path": str(root),
            "basis": "transcript",
            "source_bindings": bindings,
            "expected_revision": 1,
            "output_path": str(mcp_output),
        },
    )
    assert (
        cli_payload["markdown_export"]["content_hash"]
        == mcp_payload["markdown_export"]["content_hash"]
    )
    assert cli_output.read_bytes() == mcp_output.read_bytes()
    assert (
        cli_output.with_suffix(".map.json").read_bytes()
        == mcp_output.with_suffix(".map.json").read_bytes()
    )


def test_cli_and_stdio_failures_are_versioned_and_normalized(tmp_path: Path) -> None:
    root, bindings = _project(tmp_path)
    cli = _cli(
        "transcript-selection-resolve",
        "--project",
        str(root),
        "--source-bindings-json",
        json.dumps(bindings),
        "--expected-revision",
        "1",
        "--view-hash",
        "0" * 64,
        "--selections-json",
        '[{"paragraph_id":"paragraph_missing"}]',
        "--json",
    )
    assert cli.returncode == 2
    assert cli.stderr == ""
    cli_payload = json.loads(cli.stdout)
    mcp_payload = _stdio(
        "transcript_selection_resolve",
        {
            "project_path": str(root),
            "source_bindings": bindings,
            "expected_revision": 1,
            "view_hash": "0" * 64,
            "selections": [{"paragraph_id": "paragraph_missing"}],
        },
    )
    assert cli_payload == mcp_payload
    assert cli_payload["error"] == {"code": "transcript_selection_failed"}
