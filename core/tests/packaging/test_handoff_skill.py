from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from roughcut.adapters.artifact_store import write_new_json
from roughcut.adapters.project_store import ProjectStore
from roughcut.application.projects import create_project
from roughcut.domain.brief import EditBrief
from roughcut.domain.project import ImportMode, MediaProbe, SourceAsset, SourceFingerprint
from roughcut.domain.transcript import (
    FineUnit,
    TimedTranscript,
    TranscriptProvenance,
    TranscriptSegment,
)

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "agent-skill" / "skills" / "handoff-roughcut" / "scripts" / "roughcut_handoff.py"
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "core" / "src")}
RUN_ID = "wfr_handoff_fixture"
pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="the test CLI shim uses a POSIX executable script"
)


def _source(source_id: str, duration: int) -> SourceAsset:
    return SourceAsset(
        source_id=source_id,
        kind="audio",
        display_name=f"Fixture {source_id}",
        import_mode=ImportMode.LINKED,
        locator={"absolute_path": f"/fixture/not-read/{source_id}.wav"},
        fingerprint=SourceFingerprint(len(source_id), 1, source_id),
        probe=MediaProbe(
            duration_ticks=duration,
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


def _segment(
    segment_id: str, start: int, end: int, text: str, units: tuple[tuple[str, int, int], ...] = ()
) -> TranscriptSegment:
    return TranscriptSegment(
        segment_id=segment_id,
        start_ticks=start,
        end_ticks=end,
        original_text=text,
        corrected_text=None,
        local_speaker_id=None,
        person_id=None,
        confidence=None,
        fine_units=tuple(FineUnit("word", text, start, end, None) for text, start, end in units),
        editorial_mark="unmarked",
    )


def _transcript(source_id: str, segments: tuple[TranscriptSegment, ...]) -> TimedTranscript:
    return TimedTranscript(
        schema_version=1,
        transcript_version_id=f"tr_{source_id}",
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
        segments=segments,
    )


def _transcripts() -> tuple[TimedTranscript, ...]:
    return (
        _transcript(
            "src_a",
            (
                _segment(
                    "seg_a1",
                    0,
                    120_000,
                    "开场第一句。重复原话。重复原话。",
                    (
                        ("开场", 0, 10_000),
                        ("第一句", 10_000, 20_000),
                        ("。", 20_000, 25_000),
                        ("重复原话", 25_000, 45_000),
                        ("。", 45_000, 50_000),
                        ("重复原话", 50_000, 70_000),
                        ("。", 70_000, 80_000),
                    ),
                ),
                _segment(
                    "seg_a2",
                    120_000,
                    240_000,
                    "中间第二句。",
                    (("中间", 120_000, 140_000), ("第二句", 140_000, 180_000), ("。", 180_000, 190_000)),
                ),
                _segment(
                    "seg_a3",
                    240_000,
                    360_000,
                    "结尾第三句。",
                    (("结尾", 240_000, 260_000), ("第三句", 260_000, 300_000), ("。", 300_000, 310_000)),
                ),
            ),
        ),
        _transcript("src_b", (_segment("seg_b1", 0, 60_000, "另一素材唯一段落。"),)),
        _transcript("src_c", (_segment("seg_c1", 0, 80_000, "没有细粒度时间的较长段落，用于扩段测试。"),)),
    )


def _cli(*arguments: str, expect: int = 0) -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, "-m", "roughcut.cli", *arguments, "--json"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=ENV,
    )
    assert completed.returncode == expect, completed.stderr or completed.stdout
    return json.loads(completed.stdout)


def _action(root: Path, action_id: str, action: str, payload: dict[str, object]) -> dict[str, object]:
    return _cli(
        "workflow-action",
        "--project",
        str(root),
        "--run-id",
        RUN_ID,
        "--action-id",
        action_id,
        "--action",
        action,
        "--input-json",
        json.dumps(payload, ensure_ascii=False),
    )


def _outline() -> dict[str, object]:
    return {
        "schema_version": 1,
        "title": "交接测试大纲",
        "opening": "开场",
        "sections": [
            {
                "section_id": "section_opening",
                "title": "开场",
                "summary": "开场内容",
                "target_duration_ticks": 240_000,
            },
            {
                "section_id": "section_body",
                "title": "主体",
                "summary": "主体内容",
                "target_duration_ticks": 360_000,
            },
        ],
        "ending": "结尾",
        "required_content_coverage": [],
        "narration_status": "none",
    }


def _seed(tmp_path: Path, *, transcribed: set[str] | None = None) -> Path:
    root = tmp_path / "project"
    project = create_project(root, "Handoff fixture")
    service_names = transcribed if transcribed is not None else {"src_a", "src_b", "src_c"}
    sources = tuple(_source(name, 480_000) for name in ("src_a", "src_b", "src_c"))
    active: dict[str, str] = {}
    for transcript in _transcripts():
        write_new_json(
            root / "transcripts" / transcript.source_id / f"{transcript.transcript_version_id}.json",
            transcript.to_dict(),
        )
        if transcript.source_id in service_names:
            active[transcript.source_id] = transcript.transcript_version_id
    brief = EditBrief("brief_handoff", "交接测试主题", 720_000, ("重点",), True)
    write_new_json(root / "briefs" / "brief_handoff.json", brief.to_dict())
    ProjectStore(root).save(
        replace(
            project,
            revision=1,
            sources=sources,
            active_transcript_versions=active,
            active_brief_id=brief.brief_id,
        ),
        expected_revision=0,
    )
    return root


def _prepare(
    tmp_path: Path,
    *,
    approve_outline: bool = True,
    with_brief: bool = True,
) -> Path:
    root = _seed(tmp_path)
    started = _cli(
        "workflow-start",
        "--project",
        str(root),
        "--run-id",
        RUN_ID,
        "--ordered-source-ids-json",
        json.dumps(["src_a", "src_b", "src_c"]),
    )
    status = started["status"]
    scope = _action(
        root,
        "act_scope",
        "approve_scope",
        {
            "schema_version": 1,
            "confirmation_basis": status["confirmation_bases"]["scope"]["basis"],
            "source_authorizations": [
                {"source_id": name, "transcribe": False, "speaker_diarization": False}
                for name in ("src_a", "src_b", "src_c")
            ],
        },
    )
    if not with_brief:
        return root
    brief_basis = scope["status"]["confirmation_bases"]["brief"]["basis"]
    _action(
        root,
        "act_brief",
        "confirm_brief",
        {
            "schema_version": 1,
            "confirmation_basis": brief_basis,
            "theme": "交接测试主题",
            "target_duration_ticks": 720_000,
            "focus": ["重点"],
            "allow_reorder": True,
            "speaker_resolution_waivers": [],
        },
    )
    submitted = _action(root, "act_outline", "submit_outline", _outline())
    outline_ref = submitted["status"]["presented_subjects"]["outline_ref"]
    if approve_outline:
        _action(
            root,
            "act_outline_approve",
            "approve_outline",
            {"schema_version": 1, "outline_ref": outline_ref},
        )
    return root


def _shim(tmp_path: Path) -> Path:
    shim = tmp_path / "roughcut-cli"
    shim.write_text(
        f"#!{sys.executable}\nfrom roughcut.cli import main\nraise SystemExit(main())\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return shim


def _export(root: Path, package: Path, cli: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "export",
            "--project",
            str(root),
            "--package",
            str(package),
            "--cli",
            str(cli),
            *extra,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=ENV,
    )


def _resolve(
    root: Path, package: Path, draft: Path, output: Path, cli: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "resolve",
            "--project",
            str(root),
            "--package",
            str(package),
            "--draft",
            str(draft),
            "--output",
            str(output),
            "--cli",
            str(cli),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=ENV,
    )


def _draft(path: Path, package_id: str, blocks: list[dict[str, object]]) -> Path:
    path.write_text(
        json.dumps({"package_id": package_id, "blocks": blocks}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _package_id(package: Path) -> str:
    note = (package / "handoff.md").read_text(encoding="utf-8")
    metadata = json.loads(note[4 : note.index("\n---\n", 4)])
    return metadata["package_id"]


def _exported(tmp_path: Path, **prepare: object) -> tuple[Path, Path, Path, str]:
    root = _prepare(tmp_path, **prepare)
    cli = _shim(tmp_path)
    background = tmp_path / "background.md"
    background.write_text("用户提供的背景：只使用这段。", encoding="utf-8")
    package = tmp_path / "package"
    completed = _export(root, package, cli, "--background", str(background))
    assert completed.returncode == 0, completed.stderr
    return root, package, cli, _package_id(package)


def _mcp(name: str, arguments: dict[str, object]) -> dict[str, object]:
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    completed = subprocess.run(
        [sys.executable, "-m", "roughcut.mcp"],
        input=json.dumps(request, ensure_ascii=False) + "\n",
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=ENV,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)["result"]["structuredContent"]


def test_package_p_mapping_matches_mcp_readable_and_resolver(tmp_path: Path) -> None:
    root, package, cli, package_id = _exported(tmp_path)
    note = (package / "handoff.md").read_text(encoding="utf-8")
    metadata = json.loads(note[4 : note.index("\n---\n", 4)])
    mapping = json.loads((package / "transcript.map.json").read_text(encoding="utf-8"))
    entry = mapping["entries"][0]
    page = _mcp(
        "readable_transcript_read",
        {
            "project_path": str(root),
            "source_bindings": metadata["source_bindings"],
            "expected_revision": metadata["project_revision"],
            "offset": 0,
            "limit": 200,
        },
    )["readable_transcript"]
    assert page["view_hash"] == mapping["view_hash"]
    assert page["paragraphs"][0]["paragraph_id"] == entry["paragraph_id"]
    mcp_resolution = _mcp(
        "transcript_selection_resolve",
        {
            "project_path": str(root),
            "source_bindings": metadata["source_bindings"],
            "expected_revision": metadata["project_revision"],
            "view_hash": mapping["view_hash"],
            "selections": [{"paragraph_id": entry["paragraph_id"]}],
        },
    )["transcript_selection"]
    draft = _draft(
        tmp_path / "mcp.json", package_id, [{"source_excerpt": {"paragraph": "P001"}}]
    )
    assert _resolve(root, package, draft, tmp_path / "mcp.out.json", cli).returncode == 0
    candidate = json.loads((tmp_path / "mcp.out.json").read_text(encoding="utf-8"))
    keys = ("source_id", "transcript_version_id", "segment_id", "start_ticks", "end_ticks")
    assert candidate["blocks"][0]["refs"] == [
        {key: ref[key] for key in keys} for ref in mcp_resolution["refs"]
    ]


def test_export_uses_workflow_outline_and_writes_only_three_files(tmp_path: Path) -> None:
    root, package, _cli_path, package_id = _exported(tmp_path)
    assert sorted(path.name for path in package.iterdir()) == [
        "handoff.md",
        "transcript.map.json",
        "transcript.md",
    ]
    mapping = json.loads((package / "transcript.map.json").read_text(encoding="utf-8"))
    assert [entry["display_number"] for entry in mapping["entries"]] == ["P001", "P002", "P003"]
    assert mapping["entries"][0]["paragraph_id"].startswith("paragraph_")
    note = (package / "handoff.md").read_text(encoding="utf-8")
    for expected in (
        package_id,
        "proj_",
        "交接测试主题",
        "交接测试大纲",
        "用户提供的背景",
        "最终输出模板",
        "已批准",
    ):
        assert expected in note
    assert not any(path.suffix == ".pyc" for path in package.rglob("*"))


def test_export_returns_copyable_forward_message_and_leaves_export_bytes_untouched(
    tmp_path: Path,
) -> None:
    root = _prepare(tmp_path)
    cli = _shim(tmp_path)
    package = tmp_path / "package"
    completed = _export(root, package, cli)
    assert completed.returncode == 0, completed.stderr
    response = json.loads(completed.stdout)
    message = response["forward_message"]
    package_id = response["package_id"]
    assert package_id in message
    for name in ("transcript.md", "transcript.map.json", "handoff.md"):
        assert name in message
    assert "先读 handoff.md" in message
    assert "occurrence 从 0 开始" in message
    assert "待核实" in message
    assert "不必只输出 JSON" in message
    assert "package_id 和有序 blocks" in message
    assert "不要填写或推算 ticks" in message
    note = (package / "handoff.md").read_text(encoding="utf-8")
    assert len(message) < len(note)
    assert "最终交接稿只返回" in note
    assert "讨论过程可以使用自然语言" in note

    metadata = json.loads(note[4 : note.index("\n---\n", 4)])
    direct = tmp_path / "direct"
    direct.mkdir()
    exported = _cli(
        "markdown-export",
        "--project",
        str(root),
        "--basis",
        "transcript",
        "--output",
        str(direct / "transcript.md"),
        "--expected-revision",
        str(metadata["project_revision"]),
        "--source-bindings-json",
        json.dumps(metadata["source_bindings"], ensure_ascii=False),
    )["markdown_export"]
    assert exported["content_hash"] == package_id
    assert (direct / "transcript.md").read_bytes() == (package / "transcript.md").read_bytes()
    assert (direct / "transcript.map.json").read_bytes() == (package / "transcript.map.json").read_bytes()
    assert "导出给其他模型" not in (package / "transcript.md").read_text(encoding="utf-8")


def test_export_allows_submitted_but_unapproved_outline(tmp_path: Path) -> None:
    root, package, _cli_path, _package_id_value = _exported(tmp_path, approve_outline=False)
    note = (package / "handoff.md").read_text(encoding="utf-8")
    assert "交接测试大纲" in note
    assert "已提交，尚未批准" in note
    assert (root / "project.json").is_file()


def test_export_fails_closed_when_an_ordered_binding_has_no_transcript() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("roughcut_handoff", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    with pytest.raises(module.HandoffError) as error:
        module._bindings(
            {
                "ordered_bindings": [
                    {"source_id": "src_a", "transcript_version_id": "tr_src_a"},
                    {"source_id": "src_c", "transcript_version_id": None},
                ]
            }
        )
    assert error.value.code == "transcript_not_ready"
    assert error.value.details["source_id"] == "src_c"


def test_repeated_quote_requires_occurrence_and_zero_based_disambiguation(tmp_path: Path) -> None:
    root, package, cli, package_id = _exported(tmp_path)
    ambiguous = _draft(
        tmp_path / "ambiguous.json",
        package_id,
        [{"source_excerpt": {"paragraph": "P001", "quote": "重复原话"}}],
    )
    output = tmp_path / "ambiguous.out.json"
    completed = _resolve(root, package, ambiguous, output, cli)
    assert completed.returncode == 2
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["ok"] is False
    assert report["errors"][0]["code"] == "quote_occurrence_required"
    assert len(report["errors"][0]["occurrences"]) == 2
    assert report["blocks"] == []

    explicit = _draft(
        tmp_path / "explicit.json",
        package_id,
        [
            {"source_excerpt": {"paragraph": "P001", "quote": "重复原话", "occurrence": 0}},
            {"source_excerpt": {"paragraph": "P001", "quote": "重复原话", "occurrence": 1}},
        ],
    )
    resolved = _resolve(root, package, explicit, tmp_path / "explicit.out.json", cli)
    assert resolved.returncode == 0, resolved.stderr
    candidate = json.loads((tmp_path / "explicit.out.json").read_text(encoding="utf-8"))
    first, second = candidate["blocks"]
    assert first["refs"][0]["segment_id"] == second["refs"][0]["segment_id"] == "seg_a1"
    assert first["refs"][0]["start_ticks"] != second["refs"][0]["start_ticks"]


def test_expanded_selection_blocks_and_reports_requested_vs_resolved(tmp_path: Path) -> None:
    root, package, cli, package_id = _exported(tmp_path)
    draft = _draft(
        tmp_path / "expanded.json",
        package_id,
        [{"source_excerpt": {"paragraph": "P003", "quote": "较长段落"}}],
    )
    output = tmp_path / "expanded.out.json"
    completed = _resolve(root, package, draft, output, cli)
    assert completed.returncode == 2
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["ok"] is False
    assert report["errors"] == []
    expansion = report["expansions"][0]
    assert expansion["code"] == "expanded_to_segments"
    assert expansion["requested_quote"] == "较长段落"
    assert expansion["resolved_canonical_text"] == "没有细粒度时间的较长段落，用于扩段测试。"
    assert expansion["resolved_refs"][0]["segment_id"] == "seg_c1"
    assert report["blocks"] == []

    accepted = _draft(
        tmp_path / "accepted.json",
        package_id,
        [{"source_excerpt": {"paragraph": "P003", "quote": expansion["resolved_canonical_text"]}}],
    )
    assert _resolve(root, package, accepted, tmp_path / "accepted.out.json", cli).returncode == 0


def test_resolve_preserves_external_order_and_splits_cross_paragraph_quotes(tmp_path: Path) -> None:
    root, package, cli, package_id = _exported(tmp_path)
    ordered = _draft(
        tmp_path / "ordered.json",
        package_id,
        [
            {"source_excerpt": {"paragraph": "P003"}},
            {"section_title": "中段"},
            {"source_excerpt": {"paragraph": "P001", "quote": "中间第二句。\n结尾第三句。"}},
            {"source_excerpt": {"paragraph": "P002"}},
        ],
    )
    output = tmp_path / "ordered.out.json"
    completed = _resolve(root, package, ordered, output, cli)
    assert completed.returncode == 0, completed.stderr
    candidate = json.loads(output.read_text(encoding="utf-8"))
    assert [block["kind"] for block in candidate["blocks"]] == [
        "source_excerpt",
        "section_title",
        "source_excerpt",
        "source_excerpt",
    ]
    assert candidate["blocks"][0]["refs"][0]["source_id"] == "src_c"
    assert [ref["segment_id"] for ref in candidate["blocks"][2]["refs"]] == ["seg_a2", "seg_a3"]
    assert candidate["blocks"][3]["refs"][0]["source_id"] == "src_b"

    crossing = _draft(
        tmp_path / "crossing.json",
        package_id,
        [{"source_excerpt": {"paragraph": "P001", "quote": "结尾第三句。\n另一素材唯一段落。"}}],
    )
    cross_output = tmp_path / "crossing.out.json"
    assert _resolve(root, package, crossing, cross_output, cli).returncode == 2
    report = json.loads(cross_output.read_text(encoding="utf-8"))
    assert report["errors"][0]["code"] == "quote_not_found"
    assert "跨" not in report["errors"][0]["message"]


def test_resolve_does_not_modify_project_and_rejects_stale_or_tampered_packages(tmp_path: Path) -> None:
    root, package, cli, package_id = _exported(tmp_path)
    draft = _draft(
        tmp_path / "draft.json",
        package_id,
        [
            {"source_excerpt": {"paragraph": "P001", "quote": "开场第一句。"}},
            {"narration": "补充解说。"},
        ],
    )
    before = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and "lock" not in path.name
    }
    completed = _resolve(root, package, draft, tmp_path / "candidate.json", cli)
    assert completed.returncode == 0, completed.stderr
    after = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and "lock" not in path.name
    }
    assert after == before

    tampered = tmp_path / "tampered"
    tampered.mkdir()
    for name in ("handoff.md", "transcript.map.json", "transcript.md"):
        (tampered / name).write_bytes((package / name).read_bytes())
    (tampered / "transcript.md").write_text(
        (tampered / "transcript.md").read_text(encoding="utf-8") + "\n篡改\n",
        encoding="utf-8",
    )
    tampered_output = tmp_path / "tampered.out.json"
    assert _resolve(root, tampered, draft, tampered_output, cli).returncode == 2
    assert json.loads(tampered_output.read_text(encoding="utf-8"))["errors"][0]["code"] == "package_tampered"

    project = ProjectStore(root).load()
    ProjectStore(root).save(replace(project, revision=project.revision + 1), expected_revision=project.revision)
    stale_output = tmp_path / "stale.out.json"
    assert _resolve(root, package, draft, stale_output, cli).returncode == 2
    assert json.loads(stale_output.read_text(encoding="utf-8"))["errors"][0]["code"] == "package_stale"


def test_resolve_refuses_output_inside_project_or_package(tmp_path: Path) -> None:
    root, package, cli, package_id = _exported(tmp_path)
    draft = _draft(
        tmp_path / "draft.json", package_id, [{"source_excerpt": {"paragraph": "P002"}}]
    )
    project_manifest = root / "project.json"
    original = project_manifest.read_bytes()
    completed = _resolve(root, package, draft, project_manifest, cli)
    assert completed.returncode == 2
    assert "output_unsafe" in completed.stderr
    assert project_manifest.read_bytes() == original
    assert _resolve(root, package, draft, package / "result.json", cli).returncode == 2
    assert _resolve(root, package, draft, draft, cli).returncode == 2
    assert draft.is_file()


def test_resolved_blocks_submit_and_read_back_through_canonical_workflow(tmp_path: Path) -> None:
    root, package, cli, package_id = _exported(tmp_path)
    draft = _draft(
        tmp_path / "submit.json",
        package_id,
        [
            {"section_title": "开场"},
            {"narration": "外部模型写的未录解说。"},
            {"source_excerpt": {"paragraph": "P001", "quote": "开场第一句。"}},
            {"source_excerpt": {"paragraph": "P002"}},
        ],
    )
    output = tmp_path / "submit.out.json"
    resolved = _resolve(root, package, draft, output, cli)
    assert resolved.returncode == 0, resolved.stderr
    candidate = json.loads(output.read_text(encoding="utf-8"))

    status = _cli(
        "workflow-status", "--project", str(root), "--run-id", RUN_ID
    )["status"]
    revision = ProjectStore(root).load().revision
    context = _cli(
        "multi-source-context",
        "--project",
        str(root),
        "--source-bindings-json",
        json.dumps(candidate["source_bindings"], ensure_ascii=False),
        "--brief-id",
        status["workflow_run"]["artifact_refs"]["brief"]["artifact_id"],
        "--expected-revision",
        str(revision),
        "--offset",
        "0",
        "--limit",
        "200",
    )["multi_source_context"]
    submitted = _action(
        root,
        "act_submit_external",
        "submit_draft",
        {
            "schema_version": 1,
            "parent_draft_ref": None,
            "display_title": "外部定稿测试",
            "source_bindings": candidate["source_bindings"],
            "brief_ref": status["workflow_run"]["artifact_refs"]["brief"],
            "context_hash": context["context_hash"],
            "blocks": candidate["blocks"],
            "scoped_mutable_block_ids": [],
        },
    )
    assert submitted["receipt"]["action"] == "submit_draft"
    content_draft_id = submitted["receipt"]["mutation"]["artifact_id"]
    readback_payload = _cli(
        "content-draft-read",
        "--project",
        str(root),
        "--content-draft-id",
        content_draft_id,
    )
    readback = readback_payload["content_draft"]
    assert [block["kind"] for block in readback["blocks"]] == [
        "section_title",
        "narration",
        "source_excerpt",
        "source_excerpt",
    ]
    assert readback["blocks"][2]["canonical_text"] == "开场第一句。"
    assert readback["blocks"][3]["canonical_text"] == "另一素材唯一段落。"
    assert readback["blocks"][1]["status"] == "draft"
    assert readback["blocks"][1]["recorded_refs"] == []
    assert readback_payload["duration_acceptance"]["status"] in {
        "within_target",
        "under_target",
        "over_target",
    }
