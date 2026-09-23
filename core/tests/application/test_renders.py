from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

import roughcut.cli
import roughcut.mcp
from roughcut.adapters.artifact_store import write_new_json
from roughcut.adapters.ffmpeg.render import FFmpegRenderError, RenderCancelled
from roughcut.adapters.ffmpeg.verify import RenderVerificationError, VerificationReport
from roughcut.adapters.project_store import ProjectStore
from roughcut.application.projects import create_project
from roughcut.application.renders import create_render_plan, execute_render_plan
from roughcut.application.sources import fingerprint_file
from roughcut.domain.bindings import SourceTranscriptBinding
from roughcut.domain.brief import EditBrief
from roughcut.domain.edit import (
    EditClip,
    EditDecision,
    EditProposal,
    MultiSourceEditDecision,
    MultiSourceEditProposal,
)
from roughcut.domain.project import ImportMode, MediaProbe, ProjectError, SourceAsset
from roughcut.domain.render import MultiSourceRenderPlan, ToolResolution, parse_render_plan


def _setup_project(tmp_path: Path) -> tuple[Path, Path, EditDecision]:
    project_path = tmp_path / "render project"
    source_path = tmp_path / "linked source.mp4"
    source_path.write_bytes(b"render-source-fixture")
    project = create_project(project_path, "Render")
    source = SourceAsset(
        source_id="src_render",
        kind="video",
        display_name=source_path.name,
        import_mode=ImportMode.LINKED,
        locator={"absolute_path": str(source_path.resolve())},
        fingerprint=fingerprint_file(source_path),
        probe=MediaProbe(
            duration_ticks=600_000,
            container_start_ticks=108_000,
            first_content_ticks=120_000,
            video_codec="h264",
            width=640,
            height=360,
            nominal_frame_rate={"numerator": 25, "denominator": 1},
            is_vfr=False,
            audio_codec="aac",
            audio_sample_rate=48_000,
            rotation_degrees=0,
        ),
    )
    brief = EditBrief(
        brief_id="brief_render",
        theme="render",
        target_duration_ticks=240_000,
        focus=("cuts",),
        allow_reorder=True,
    )
    clips = (
        EditClip(
            "clip_c",
            source.source_id,
            "tr_render",
            "seg_c",
            360_000,
            480_000,
            "third first",
            "secret transcript C",
        ),
        EditClip(
            "clip_a",
            source.source_id,
            "tr_render",
            "seg_a",
            0,
            120_000,
            "first second",
            "secret transcript A",
        ),
    )
    proposal = EditProposal(
        proposal_id="proposal_render",
        base_project_revision=0,
        base_edit_version_id=None,
        source_id=source.source_id,
        transcript_version_id="tr_render",
        brief_snapshot=brief,
        context_hash="a" * 64,
        clips=clips,
        total_duration_ticks=240_000,
        created_at="fixture",
    )
    decision = EditDecision(
        edit_version_id="edit_render",
        proposal_snapshot=proposal,
        project_revision=1,
        created_at="fixture",
    )
    write_new_json(project_path / "edits" / "edit_render.json", decision.to_dict())
    settings = {
        **project.settings,
        "width": 320,
        "height": 180,
    }
    ProjectStore(project_path).save(
        replace(
            project,
            revision=1,
            settings=settings,
            sources=(source,),
            active_edit_version_id=decision.edit_version_id,
        ),
        expected_revision=0,
    )
    return project_path, source_path, decision


def _setup_multi_project(
    tmp_path: Path,
) -> tuple[Path, tuple[Path, Path], MultiSourceEditDecision]:
    project_path = tmp_path / "multi render project"
    source_paths = (tmp_path / "A source.mp4", tmp_path / "中文 B source.mp4")
    source_paths[0].write_bytes(b"render-source-a")
    source_paths[1].write_bytes(b"render-source-b")
    project = create_project(project_path, "Multi Render")
    sources = tuple(
        SourceAsset(
            source_id=source_id,
            kind="video",
            display_name=path.name,
            import_mode=ImportMode.LINKED,
            locator={"absolute_path": str(path.resolve())},
            fingerprint=fingerprint_file(path),
            probe=MediaProbe(
                duration_ticks=600_000,
                container_start_ticks=0,
                first_content_ticks=0,
                video_codec="h264",
                width=640,
                height=360,
                nominal_frame_rate={"numerator": 25, "denominator": 1},
                is_vfr=False,
                audio_codec="aac",
                audio_sample_rate=48_000,
                rotation_degrees=0,
            ),
        )
        for source_id, path in zip(("src_a", "src_b"), source_paths, strict=True)
    )
    brief = EditBrief("brief_multi", "A B A", 360_000, ("cuts",), True)
    bindings = (
        SourceTranscriptBinding("src_a", "tr_a"),
        SourceTranscriptBinding("src_b", "tr_b"),
    )
    clips = (
        EditClip("clip_a1", "src_a", "tr_a", "seg_a1", 120_000, 240_000, "A", "A"),
        EditClip("clip_b", "src_b", "tr_b", "seg_b", 240_000, 360_000, "B", "B"),
        EditClip("clip_a2", "src_a", "tr_a", "seg_a2", 360_000, 480_000, "A2", "A2"),
    )
    proposal = MultiSourceEditProposal(
        "proposal_multi",
        0,
        None,
        bindings,
        brief,
        "b" * 64,
        clips,
        360_000,
        "fixture",
    )
    decision = MultiSourceEditDecision("edit_multi", proposal, 1, "fixture")
    write_new_json(project_path / "edits" / "edit_multi.json", decision.to_dict())
    ProjectStore(project_path).save(
        replace(
            project,
            revision=1,
            settings={**project.settings, "width": 320, "height": 180},
            sources=sources,
            active_transcript_versions={"src_a": "tr_a", "src_b": "tr_b"},
            active_edit_version_id=decision.edit_version_id,
        ),
        expected_revision=0,
    )
    return project_path, source_paths, decision


@pytest.fixture
def resolved_tools(monkeypatch: pytest.MonkeyPatch) -> tuple[ToolResolution, ToolResolution]:
    tools = (
        ToolResolution("ffmpeg", "/tools/ffmpeg", "ffmpeg version fixture"),
        ToolResolution("ffprobe", "/tools/ffprobe", "ffprobe version fixture"),
    )
    monkeypatch.setattr("roughcut.application.renders.resolve_render_tools", lambda: tools)
    return tools


def test_plan_freezes_only_the_active_decision_source_settings_and_tools(
    tmp_path: Path,
    resolved_tools: tuple[ToolResolution, ToolResolution],
) -> None:
    project_path, source_path, decision = _setup_project(tmp_path)

    plan = create_render_plan(
        project_path,
        edit_version_id=decision.edit_version_id,
        expected_revision=1,
    )

    assert plan.project_revision == 1
    assert plan.edit_version_id == decision.edit_version_id
    assert [clip.clip_id for clip in plan.clips] == ["clip_c", "clip_a"]
    assert plan.output_settings.to_dict() == {
        "width": 320,
        "height": 180,
        "frame_rate": {"numerator": 25, "denominator": 1},
        "audio_sample_rate": 48_000,
    }
    assert plan.source.locator == {"absolute_path": str(source_path.resolve())}
    assert (project_path / plan.plan_relative_path).is_file()
    assert json.loads((project_path / plan.plan_relative_path).read_text(encoding="utf-8")) == plan.to_dict()


def test_multisource_plan_dispatch_freezes_ordered_bindings_sources_and_a_b_a(
    tmp_path: Path,
    resolved_tools: tuple[ToolResolution, ToolResolution],
) -> None:
    project_path, source_paths, decision = _setup_multi_project(tmp_path)

    plan = create_render_plan(
        project_path,
        edit_version_id=decision.edit_version_id,
        expected_revision=1,
    )

    assert isinstance(plan, MultiSourceRenderPlan)
    assert plan.schema_version == 2
    assert plan.source_bindings == decision.proposal_snapshot.source_bindings
    assert [source.source_id for source in plan.sources] == ["src_a", "src_b"]
    assert [source.locator["absolute_path"] for source in plan.sources] == [
        str(path.resolve()) for path in source_paths
    ]
    assert [clip.source_id for clip in plan.clips] == ["src_a", "src_b", "src_a"]
    assert parse_render_plan(
        json.loads((project_path / plan.plan_relative_path).read_text(encoding="utf-8"))
    ) == plan


def test_multisource_plan_rejects_stale_active_transcript_and_cleans_plan(
    tmp_path: Path,
    resolved_tools: tuple[ToolResolution, ToolResolution],
) -> None:
    project_path, _source_paths, decision = _setup_multi_project(tmp_path)
    store = ProjectStore(project_path)
    project = store.load()
    store.save(
        replace(
            project,
            revision=2,
            active_transcript_versions={"src_a": "tr_other", "src_b": "tr_b"},
        ),
        expected_revision=1,
    )

    with pytest.raises(ProjectError, match="transcript"):
        create_render_plan(
            project_path,
            edit_version_id=decision.edit_version_id,
            expected_revision=2,
        )

    assert not list((project_path / "renders").glob("*.plan.json"))


def test_plan_rejects_stale_revision_nonactive_decision_and_missing_source(
    tmp_path: Path,
    resolved_tools: tuple[ToolResolution, ToolResolution],
) -> None:
    project_path, source_path, decision = _setup_project(tmp_path)
    with pytest.raises(ProjectError, match="revision"):
        create_render_plan(project_path, edit_version_id=decision.edit_version_id, expected_revision=0)

    store = ProjectStore(project_path)
    project = store.load()
    store.save(
        replace(project, revision=2, active_edit_version_id=None),
        expected_revision=1,
    )
    with pytest.raises(ProjectError, match="active"):
        create_render_plan(project_path, edit_version_id=decision.edit_version_id, expected_revision=2)

    source_path.unlink()
    store.save(
        replace(store.load(), revision=3, active_edit_version_id=decision.edit_version_id),
        expected_revision=2,
    )
    with pytest.raises(ProjectError, match="source"):
        create_render_plan(project_path, edit_version_id=decision.edit_version_id, expected_revision=3)


def test_plan_rejects_source_fingerprint_change(
    tmp_path: Path,
    resolved_tools: tuple[ToolResolution, ToolResolution],
) -> None:
    project_path, source_path, decision = _setup_project(tmp_path)
    source_path.write_bytes(b"changed-after-import")

    with pytest.raises(ProjectError, match="fingerprint"):
        create_render_plan(project_path, edit_version_id=decision.edit_version_id, expected_revision=1)


def test_execute_rejects_fingerprint_change_after_plan_without_running_ffmpeg(
    tmp_path: Path,
    resolved_tools: tuple[ToolResolution, ToolResolution],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_path, source_path, decision = _setup_project(tmp_path)
    plan = create_render_plan(project_path, edit_version_id=decision.edit_version_id, expected_revision=1)
    source_path.write_bytes(b"changed-after-plan")
    called = False

    def should_not_run(_command: list[str], **_kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr("roughcut.application.renders.run_ffmpeg", should_not_run)
    with pytest.raises(ProjectError, match="fingerprint"):
        execute_render_plan(project_path, plan.render_id, expected_revision=1)

    assert called is False
    assert not (project_path / plan.output_relative_path).exists()
    assert not (project_path / plan.manifest_relative_path).exists()


@pytest.mark.parametrize("failure", ["ffmpeg", "verify", "cancel"])
def test_render_failure_cancel_or_verification_failure_cleans_only_job_files(
    tmp_path: Path,
    resolved_tools: tuple[ToolResolution, ToolResolution],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    project_path, source_path, decision = _setup_project(tmp_path)
    plan = create_render_plan(project_path, edit_version_id=decision.edit_version_id, expected_revision=1)

    def fail_render(command: list[str], **_kwargs: object) -> None:
        Path(command[-1]).write_bytes(b"partial")
        if failure == "cancel":
            raise RenderCancelled("cancelled")
        if failure == "ffmpeg":
            raise FFmpegRenderError("failed")

    def fail_verify(
        _path: Path,
        _plan: object,
        **_kwargs: object,
    ) -> VerificationReport:
        if failure == "verify":
            raise RenderVerificationError()
        return _accepted_report()

    monkeypatch.setattr("roughcut.application.renders.run_ffmpeg", fail_render)
    monkeypatch.setattr("roughcut.application.renders.verify_render_output", fail_verify)
    error = RenderCancelled if failure == "cancel" else (RenderVerificationError if failure == "verify" else FFmpegRenderError)
    with pytest.raises(error):
        execute_render_plan(project_path, plan.render_id, expected_revision=1)

    assert source_path.read_bytes() == b"render-source-fixture"
    assert (project_path / plan.plan_relative_path).is_file()
    assert not (project_path / plan.output_relative_path).exists()
    assert not (project_path / plan.manifest_relative_path).exists()
    assert not list((project_path / "renders").glob(f".{plan.render_id}.*"))


def test_success_publishes_verified_output_and_path_free_manifest(
    tmp_path: Path,
    resolved_tools: tuple[ToolResolution, ToolResolution],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_path, source_path, decision = _setup_project(tmp_path)
    plan = create_render_plan(project_path, edit_version_id=decision.edit_version_id, expected_revision=1)

    def succeed(command: list[str], **_kwargs: object) -> None:
        Path(command[-1]).write_bytes(b"verified-mp4")

    monkeypatch.setattr("roughcut.application.renders.run_ffmpeg", succeed)
    monkeypatch.setattr(
        "roughcut.application.renders.verify_render_output",
        lambda _path, _plan, **_kwargs: _accepted_report(),
    )

    result = execute_render_plan(project_path, plan.render_id, expected_revision=1)

    assert result.render_id == plan.render_id
    assert result.mp4_path == plan.output_relative_path
    assert result.manifest_path == plan.manifest_relative_path
    assert (project_path / result.mp4_path).read_bytes() == b"verified-mp4"
    manifest_text = (project_path / result.manifest_path).read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert manifest["acceptance"]["accepted"] is True
    assert manifest["schema_version"] == 2
    assert manifest["input_source"]["fingerprint"] == plan.source.fingerprint.to_dict()
    assert manifest["clips"] == [clip.to_dict() for clip in plan.clips]
    assert manifest["render_schedule"]["strategy"] == "clip_local_accurate_seek"
    assert manifest["render_schedule"]["total_frames"] == 50
    assert manifest["render_schedule"]["total_samples"] == 96_000
    assert manifest["command_summary"]["input_count"] == 2
    assert manifest["command_summary"]["bounded_input_duration"] is True
    assert manifest["performance"]["total_wall_seconds"] >= 0
    assert str(source_path.resolve()) not in manifest_text
    assert "secret transcript" not in manifest_text
    assert not list((project_path / "renders").glob(f".{plan.render_id}.*"))


def test_multisource_success_publishes_schema_three_path_free_manifest(
    tmp_path: Path,
    resolved_tools: tuple[ToolResolution, ToolResolution],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_path, source_paths, decision = _setup_multi_project(tmp_path)
    plan = create_render_plan(
        project_path,
        edit_version_id=decision.edit_version_id,
        expected_revision=1,
    )
    assert isinstance(plan, MultiSourceRenderPlan)

    def succeed(command: list[str], **_kwargs: object) -> None:
        assert [command[index + 1] for index, value in enumerate(command) if value == "-i"] == [
            str(source_paths[0].resolve()),
            str(source_paths[1].resolve()),
            str(source_paths[0].resolve()),
        ]
        Path(command[-1]).write_bytes(b"verified-multi-mp4")

    monkeypatch.setattr("roughcut.application.renders.run_ffmpeg", succeed)
    monkeypatch.setattr(
        "roughcut.application.renders.verify_render_output",
        lambda _path, _plan, **_kwargs: _accepted_report(),
    )

    result = execute_render_plan(project_path, plan.render_id, expected_revision=1)

    manifest_text = (project_path / result.manifest_path).read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert manifest["schema_version"] == 3
    assert manifest["source_bindings"] == [
        binding.to_dict() for binding in plan.source_bindings
    ]
    assert [source["source_id"] for source in manifest["input_sources"]] == [
        "src_a",
        "src_b",
    ]
    assert [clip["source_id"] for clip in manifest["clips"]] == [
        "src_a",
        "src_b",
        "src_a",
    ]
    assert manifest["render_schedule"]["total_frames"] == 75
    assert manifest["render_schedule"]["total_samples"] == 144_000
    assert manifest["acceptance"]["checks"]["decision_clips_match_plan"] is True
    assert "locator" not in manifest_text
    assert all(str(path.resolve()) not in manifest_text for path in source_paths)
    assert "proxies/" not in manifest_text


def test_multisource_concurrent_project_change_before_publish_cleans_candidate(
    tmp_path: Path,
    resolved_tools: tuple[ToolResolution, ToolResolution],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_path, _source_paths, decision = _setup_multi_project(tmp_path)
    plan = create_render_plan(
        project_path,
        edit_version_id=decision.edit_version_id,
        expected_revision=1,
    )
    assert isinstance(plan, MultiSourceRenderPlan)

    def change_project(command: list[str], **_kwargs: object) -> None:
        Path(command[-1]).write_bytes(b"candidate")
        store = ProjectStore(project_path)
        current = store.load()
        store.save(replace(current, revision=2), expected_revision=1)

    monkeypatch.setattr("roughcut.application.renders.run_ffmpeg", change_project)

    with pytest.raises(ProjectError, match="revision"):
        execute_render_plan(project_path, plan.render_id, expected_revision=1)

    assert not (project_path / plan.output_relative_path).exists()
    assert not (project_path / plan.manifest_relative_path).exists()
    assert not list((project_path / "renders").glob(f".{plan.render_id}.*"))


def test_multisource_change_during_verification_is_rejected_before_publication(
    tmp_path: Path,
    resolved_tools: tuple[ToolResolution, ToolResolution],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_path, _source_paths, decision = _setup_multi_project(tmp_path)
    plan = create_render_plan(
        project_path,
        edit_version_id=decision.edit_version_id,
        expected_revision=1,
    )
    assert isinstance(plan, MultiSourceRenderPlan)

    monkeypatch.setattr(
        "roughcut.application.renders.run_ffmpeg",
        lambda command, **_kwargs: Path(command[-1]).write_bytes(b"candidate"),
    )

    def change_during_verify(
        _path: Path, _plan: object, **_kwargs: object
    ) -> VerificationReport:
        store = ProjectStore(project_path)
        current = store.load()
        store.save(replace(current, revision=2), expected_revision=1)
        return _accepted_report()

    monkeypatch.setattr(
        "roughcut.application.renders.verify_render_output", change_during_verify
    )

    with pytest.raises(ProjectError, match="revision"):
        execute_render_plan(project_path, plan.render_id, expected_revision=1)

    assert not (project_path / plan.output_relative_path).exists()
    assert not (project_path / plan.manifest_relative_path).exists()
    assert not list((project_path / "renders").glob(f".{plan.render_id}.*"))


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        ("ffmpeg", FFmpegRenderError),
        ("verify", RenderVerificationError),
        ("manifest", OSError),
        ("publish", OSError),
        ("interrupt", RenderCancelled),
    ],
)
def test_multisource_failures_leave_no_candidate_output_or_manifest(
    tmp_path: Path,
    resolved_tools: tuple[ToolResolution, ToolResolution],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    error: type[BaseException],
) -> None:
    project_path, source_paths, decision = _setup_multi_project(tmp_path)
    plan = create_render_plan(
        project_path,
        edit_version_id=decision.edit_version_id,
        expected_revision=1,
    )
    assert isinstance(plan, MultiSourceRenderPlan)
    originals = tuple(path.read_bytes() for path in source_paths)

    def render(command: list[str], **_kwargs: object) -> None:
        Path(command[-1]).write_bytes(b"partial candidate")
        if failure == "ffmpeg":
            raise FFmpegRenderError("fixture")
        if failure == "interrupt":
            raise RenderCancelled("fixture")

    def verify(_path: Path, _plan: object, **_kwargs: object) -> VerificationReport:
        if failure == "verify":
            raise RenderVerificationError()
        return _accepted_report()

    real_replace = os.replace

    def replace_with_failure(source: Path, destination: Path) -> None:
        if failure == "publish" and str(destination).endswith(".manifest.json"):
            raise OSError("fixture publish failure")
        real_replace(source, destination)

    def fail_manifest(*_args: object) -> None:
        raise OSError("fixture manifest failure")

    monkeypatch.setattr("roughcut.application.renders.run_ffmpeg", render)
    monkeypatch.setattr("roughcut.application.renders.verify_render_output", verify)
    if failure == "manifest":
        monkeypatch.setattr(
            "roughcut.application.renders._write_candidate_manifest",
            fail_manifest,
        )
    if failure == "publish":
        monkeypatch.setattr("roughcut.application.renders.os.replace", replace_with_failure)

    with pytest.raises(error):
        execute_render_plan(project_path, plan.render_id, expected_revision=1)

    assert ProjectStore(project_path).load().revision == 1
    assert tuple(path.read_bytes() for path in source_paths) == originals
    assert not (project_path / plan.output_relative_path).exists()
    assert not (project_path / plan.manifest_relative_path).exists()
    assert not list((project_path / "renders").glob(f".{plan.render_id}.*"))


def test_fwv_015_cli_and_mcp_direct_render_without_skill_or_run_is_zero_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_path, _source_paths, decision = _setup_multi_project(tmp_path)
    historical_mp4 = project_path / "renders" / "render_historical.mp4"
    historical_manifest = project_path / "renders" / "render_historical.manifest.json"
    historical_mp4.parent.mkdir()
    historical_mp4.write_bytes(b"historical-render")
    historical_manifest.write_text(
        '{"render_id":"render_historical"}\n',
        encoding="utf-8",
    )
    before = {
        path.relative_to(project_path).as_posix(): path.read_bytes()
        for path in project_path.rglob("*")
        if path.is_file()
    }
    render_calls = 0

    def forbidden_render(*_args: object, **_kwargs: object) -> None:
        nonlocal render_calls
        render_calls += 1
        raise AssertionError("public render must not reach FFmpeg")

    monkeypatch.setattr("roughcut.application.renders.run_ffmpeg", forbidden_render)
    arguments = {
        "project_path": str(project_path),
        "edit_version_id": decision.edit_version_id,
        "expected_revision": 1,
    }

    with pytest.raises(SystemExit) as exit_info:
        roughcut.cli.main(
            [
                "render-roughcut",
                "--project",
                str(project_path),
                "--edit-version-id",
                decision.edit_version_id,
                "--expected-revision",
                "1",
                "--json",
            ]
        )
    assert exit_info.value.code == 2
    cli_payload = json.loads(capsys.readouterr().out)
    response = roughcut.mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 14,
            "method": "tools/call",
            "params": {"name": "render_roughcut", "arguments": arguments},
        }
    )

    assert response is not None
    mcp_result = response["result"]
    assert isinstance(mcp_result, dict)
    mcp_payload = mcp_result["structuredContent"]
    assert cli_payload == mcp_payload
    assert cli_payload["tool_schema_version"] == 32
    assert cli_payload["error"] == {"code": "workflow_required"}
    assert render_calls == 0
    assert {
        path.relative_to(project_path).as_posix(): path.read_bytes()
        for path in project_path.rglob("*")
        if path.is_file()
    } == before
    assert not (project_path / "workflow").exists()


def test_existing_output_or_manifest_is_never_overwritten(
    tmp_path: Path,
    resolved_tools: tuple[ToolResolution, ToolResolution],
) -> None:
    project_path, _source_path, decision = _setup_project(tmp_path)
    plan = create_render_plan(project_path, edit_version_id=decision.edit_version_id, expected_revision=1)
    output = project_path / plan.output_relative_path
    output.write_bytes(b"existing-success")

    with pytest.raises(ProjectError, match="already exists"):
        execute_render_plan(project_path, plan.render_id, expected_revision=1)

    assert output.read_bytes() == b"existing-success"


def _accepted_report() -> VerificationReport:
    return VerificationReport(
        accepted=True,
        duration_ticks=240_000,
        checks={"fixture": True},
        probe={"format": {"format_name": "mp4"}},
    )
