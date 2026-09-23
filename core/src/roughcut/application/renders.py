"""Freeze and execute current versioned Edit Decision renders."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from roughcut.adapters.artifact_store import read_json_object, write_new_json
from roughcut.adapters.ffmpeg.render import (
    CancelCheck,
    build_ffmpeg_command,
    build_filter_script,
    resolve_render_tools,
    run_ffmpeg,
)
from roughcut.adapters.ffmpeg.verify import VerificationReport, verify_render_output
from roughcut.adapters.project_store import ProjectStore
from roughcut.application.sources import fingerprint_file
from roughcut.domain.edit import EditDecision, MultiSourceEditDecision
from roughcut.domain.project import ImportMode, Project, ProjectError, SourceAsset
from roughcut.domain.render import (
    MultiSourceRenderPlan,
    OutputSettings,
    RenderClip,
    RenderPlan,
    RenderPlanLike,
    RenderSchedule,
    ToolResolution,
    derive_render_schedule,
    parse_render_plan,
    render_has_audio,
    render_sources,
)

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_RENDERER_WORKSPACE: ContextVar[Path | None] = ContextVar(
    "roughcut_renderer_workspace",
    default=None,
)


@dataclass(frozen=True)
class RenderResult:
    render_id: str
    mp4_path: str
    manifest_path: str
    acceptance: dict[str, bool]

    def to_dict(self) -> dict[str, object]:
        return {
            "render_id": self.render_id,
            "mp4_path": self.mp4_path,
            "manifest_path": self.manifest_path,
            "acceptance": {"accepted": all(self.acceptance.values()), "checks": self.acceptance},
        }


def render_roughcut(
    project_path: Path,
    *,
    edit_version_id: str,
    expected_revision: int,
    cancel_requested: CancelCheck | None = None,
) -> RenderResult:
    plan = create_render_plan(
        project_path,
        edit_version_id=edit_version_id,
        expected_revision=expected_revision,
    )
    return execute_render_plan(
        project_path,
        plan.render_id,
        expected_revision=expected_revision,
        cancel_requested=cancel_requested,
    )


def create_render_plan(
    project_path: Path,
    *,
    edit_version_id: str,
    expected_revision: int,
) -> RenderPlanLike:
    plan = prepare_render_plan(
        project_path,
        edit_version_id=edit_version_id,
        expected_revision=expected_revision,
    )
    store = ProjectStore(project_path)
    plan_path = store.project_path / plan.plan_relative_path
    write_new_json(plan_path, plan.to_dict())
    try:
        latest = store.load()
        source_paths = {
            source.source_id: _resolve_source_path(store.project_path, source)
            for source in render_sources(plan)
        }
        _validate_plan_snapshot(
            store.project_path,
            plan,
            latest,
            expected_revision,
            source_paths=source_paths,
        )
    except Exception:
        plan_path.unlink(missing_ok=True)
        raise
    return plan


def prepare_render_plan(
    project_path: Path,
    *,
    edit_version_id: str,
    expected_revision: int,
    render_id: str | None = None,
    tools: tuple[ToolResolution, ToolResolution] | None = None,
) -> RenderPlanLike:
    """Construct and validate a Render Plan without publishing any artifact."""

    store = ProjectStore(project_path)
    project = store.load()
    _validate_current_decision(project, edit_version_id, expected_revision)
    decision = _read_decision(store.project_path, edit_version_id)
    sources: tuple[SourceAsset, ...]
    if isinstance(decision, EditDecision):
        sources = (_source_for_decision(project, decision.proposal_snapshot.source_id),)
    else:
        _validate_active_transcript_bindings(project, decision)
        sources = tuple(
            _source_for_decision(project, binding.source_id)
            for binding in decision.proposal_snapshot.source_bindings
        )
    source_paths = {
        source.source_id: _resolve_source_path(store.project_path, source)
        for source in sources
    }
    for source in sources:
        if fingerprint_file(source_paths[source.source_id]) != source.fingerprint:
            raise ProjectError("render source fingerprint has changed")
    clips = tuple(
        RenderClip(
            clip_id=clip.clip_id,
            source_id=clip.source_id,
            source_in_ticks=clip.source_in_ticks,
            source_out_ticks=clip.source_out_ticks,
        )
        for clip in decision.proposal_snapshot.clips
    )
    settings = OutputSettings.from_dict(project.settings)
    ffmpeg, ffprobe = tools or resolve_render_tools()
    prepared_render_id = render_id or f"render_{uuid4().hex}"
    if isinstance(decision, EditDecision):
        plan: RenderPlanLike = RenderPlan(
            render_id=prepared_render_id,
            project_id=project.project_id,
            project_revision=project.revision,
            edit_version_id=decision.edit_version_id,
            source=sources[0],
            clips=clips,
            output_settings=settings,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            plan_relative_path=f"renders/{prepared_render_id}.plan.json",
            output_relative_path=f"renders/{prepared_render_id}.mp4",
            manifest_relative_path=f"renders/{prepared_render_id}.manifest.json",
        )
    else:
        plan = MultiSourceRenderPlan(
            render_id=prepared_render_id,
            project_id=project.project_id,
            project_revision=project.revision,
            edit_version_id=decision.edit_version_id,
            source_bindings=decision.proposal_snapshot.source_bindings,
            sources=sources,
            clips=clips,
            output_settings=settings,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            plan_relative_path=f"renders/{prepared_render_id}.plan.json",
            output_relative_path=f"renders/{prepared_render_id}.mp4",
            manifest_relative_path=f"renders/{prepared_render_id}.manifest.json",
        )
    derive_render_schedule(plan)
    return plan


def execute_render_plan(
    project_path: Path,
    render_id: str,
    *,
    expected_revision: int,
    cancel_requested: CancelCheck | None = None,
) -> RenderResult:
    _validate_id(render_id, "render_id")
    store = ProjectStore(project_path)
    plan_path = store.project_path / "renders" / f"{render_id}.plan.json"
    plan = parse_render_plan(read_json_object(plan_path, description="render plan"))
    result, _manifest = execute_prepared_render_to_paths(
        store.project_path,
        plan,
        expected_revision=expected_revision,
        output_path=store.project_path / plan.output_relative_path,
        manifest_path=store.project_path / plan.manifest_relative_path,
        cancel_requested=cancel_requested,
    )
    return result


def execute_prepared_render_to_paths(
    project_path: Path,
    plan: RenderPlanLike,
    *,
    expected_revision: int,
    output_path: Path,
    manifest_path: Path,
    cancel_requested: CancelCheck | None = None,
    after_output_published: Callable[[], None] | None = None,
    phase_callback: Callable[[str], None] | None = None,
    renderer_workspace: Path | None = None,
) -> tuple[RenderResult, dict[str, object]]:
    """Execute one prepared plan to controlled unpublished or final paths."""

    store = ProjectStore(project_path)
    schedule = derive_render_schedule(plan)
    project = store.load()
    _validate_plan_against_project(plan, project, expected_revision)
    source_paths = {
        source.source_id: _resolve_source_path(store.project_path, source)
        for source in render_sources(plan)
    }
    _validate_plan_snapshot(
        store.project_path,
        plan,
        project,
        expected_revision,
        source_paths=source_paths,
    )

    if output_path.exists() or manifest_path.exists():
        raise ProjectError("render output or manifest already exists")
    renders_directory = output_path.parent
    renders_directory.mkdir(parents=True, exist_ok=True)
    with _render_workspace(
        renders_directory,
        plan.render_id,
        renderer_workspace or _RENDERER_WORKSPACE.get(),
    ) as job_directory:
        filter_path = job_directory / "filter-complex.txt"
        candidate_path = job_directory / "candidate.mp4"
        candidate_manifest_path = job_directory / "manifest.json"
        filter_script = build_filter_script(plan, schedule=schedule)
        filter_path.write_text(filter_script, encoding="utf-8")
        command = build_ffmpeg_command(
            plan,
            schedule=schedule,
            source_path=(source_paths[plan.source.source_id] if isinstance(plan, RenderPlan) else None),
            source_paths=source_paths,
            filter_script_path=filter_path,
            output_path=candidate_path,
        )
        job_started = time.monotonic()
        ffmpeg_started = time.monotonic()
        if phase_callback is not None:
            phase_callback("render_encoding")
        run_ffmpeg(command, cancel_requested=cancel_requested)
        ffmpeg_wall_seconds = time.monotonic() - ffmpeg_started
        _validate_plan_snapshot(
            store.project_path,
            plan,
            store.load(),
            expected_revision,
            source_paths=source_paths,
        )
        verification_started = time.monotonic()
        if phase_callback is not None:
            phase_callback("render_verifying")
        report = verify_render_output(candidate_path, plan, schedule=schedule)
        verification_wall_seconds = time.monotonic() - verification_started
        performance = {
            "ffmpeg_wall_seconds": round(ffmpeg_wall_seconds, 6),
            "verification_wall_seconds": round(verification_wall_seconds, 6),
            "total_wall_seconds": round(time.monotonic() - job_started, 6),
        }
        manifest = _render_manifest(
            plan,
            schedule,
            report,
            filter_script,
            command,
            source_paths,
            filter_path,
            candidate_path,
            performance,
        )
        _write_candidate_manifest(candidate_manifest_path, manifest)
        _sync_file(candidate_path)
        _validate_plan_snapshot(
            store.project_path,
            plan,
            store.load(),
            expected_revision,
            source_paths=source_paths,
        )
        if output_path.exists() or manifest_path.exists():
            raise ProjectError("render output or manifest already exists")
        output_published = False
        try:
            os.replace(candidate_path, output_path)
            output_published = True
            if after_output_published is not None:
                after_output_published()
            os.replace(candidate_manifest_path, manifest_path)
            _sync_directory(renders_directory)
        except Exception:
            if output_published:
                output_path.unlink(missing_ok=True)
            raise
    result = RenderResult(
        render_id=plan.render_id,
        mp4_path=plan.output_relative_path,
        manifest_path=plan.manifest_relative_path,
        acceptance=_acceptance_checks(plan, report),
    )
    return result, manifest


@contextmanager
def renderer_workspace_identity(path: Path) -> Iterator[None]:
    token = _RENDERER_WORKSPACE.set(path)
    try:
        yield
    finally:
        _RENDERER_WORKSPACE.reset(token)


@contextmanager
def _render_workspace(
    renders_directory: Path,
    render_id: str,
    renderer_workspace: Path | None,
) -> Iterator[Path]:
    if renderer_workspace is None:
        with tempfile.TemporaryDirectory(
            dir=renders_directory,
            prefix=f".{render_id}.",
        ) as temporary_directory:
            yield Path(temporary_directory)
        return
    if (
        renderer_workspace.parent != renders_directory
        or os.path.lexists(renderer_workspace)
    ):
        raise ProjectError("renderer workspace identity is invalid")
    renderer_workspace.mkdir()
    _sync_directory(renders_directory)
    try:
        yield renderer_workspace
    finally:
        _remove_renderer_workspace(renderer_workspace)


def _remove_renderer_workspace(renderer_workspace: Path) -> None:
    try:
        workspace_details = os.lstat(renderer_workspace)
    except OSError as error:
        raise ProjectError("renderer workspace disappeared") from error
    if (
        stat.S_ISLNK(workspace_details.st_mode)
        or not stat.S_ISDIR(workspace_details.st_mode)
    ):
        raise ProjectError("renderer workspace identity changed")
    allowed = {"filter-complex.txt", "candidate.mp4", "manifest.json"}
    children = list(renderer_workspace.iterdir())
    for child in children:
        details = os.lstat(child)
        if (
            child.name not in allowed
            or stat.S_ISLNK(details.st_mode)
            or not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
        ):
            raise ProjectError("renderer workspace contains an unsafe node")
    for child in children:
        child.unlink()
    renderer_workspace.rmdir()
    _sync_directory(renderer_workspace.parent)


def _validate_current_decision(
    project: Project, edit_version_id: str, expected_revision: int
) -> None:
    _validate_id(edit_version_id, "edit_version_id")
    if project.revision != expected_revision:
        raise ProjectError("project revision conflict")
    if project.active_edit_version_id != edit_version_id:
        raise ProjectError("render requires the active Edit Decision")


def _validate_plan_against_project(
    plan: RenderPlanLike, project: Project, expected_revision: int
) -> None:
    _validate_current_decision(project, plan.edit_version_id, expected_revision)
    if plan.project_id != project.project_id or plan.project_revision != project.revision:
        raise ProjectError("render plan project revision is stale")
    if plan.output_settings != OutputSettings.from_dict(project.settings):
        raise ProjectError("render plan output settings are stale")


def _source_for_decision(project: Project, source_id: str) -> SourceAsset:
    matches = [source for source in project.sources if source.source_id == source_id]
    if len(matches) != 1:
        raise ProjectError("render decision references an unknown source")
    source = matches[0]
    if source.probe.video_codec is None and source.probe.audio_codec is None:
        raise ProjectError("render source has no usable media stream")
    return source


def _resolve_source_path(project_path: Path, source: SourceAsset) -> Path:
    try:
        if source.import_mode is ImportMode.COPIED:
            relative = source.locator.get("project_relative_path")
            if relative is None:
                raise ProjectError("copied render source locator is missing")
            resolved = (project_path / relative).resolve(strict=True)
            if not resolved.is_relative_to(project_path):
                raise ProjectError("copied render source escapes the project")
        else:
            absolute = source.locator.get("absolute_path")
            if absolute is None:
                raise ProjectError("linked render source locator is missing")
            resolved = Path(absolute).resolve(strict=True)
    except OSError as error:
        raise ProjectError("render source is missing or unreadable") from error
    if not resolved.is_file():
        raise ProjectError("render source is missing or unreadable")
    return resolved


def _validate_plan_snapshot(
    project_path: Path,
    plan: RenderPlanLike,
    project: Project,
    expected_revision: int,
    *,
    source_paths: dict[str, Path],
) -> None:
    _validate_plan_against_project(plan, project, expected_revision)
    decision = _read_decision(project_path, plan.edit_version_id)
    frozen_sources = render_sources(plan)
    if isinstance(plan, RenderPlan):
        if not isinstance(decision, EditDecision):
            raise ProjectError("render plan schema does not match active Decision")
        single_proposal = decision.proposal_snapshot
        proposal_clips = single_proposal.clips
        if single_proposal.source_id != plan.source.source_id:
            raise ProjectError("render plan source does not match active Decision")
    else:
        if not isinstance(decision, MultiSourceEditDecision):
            raise ProjectError("render plan schema does not match active Decision")
        multi_proposal = decision.proposal_snapshot
        proposal_clips = multi_proposal.clips
        _validate_active_transcript_bindings(project, decision)
        if multi_proposal.source_bindings != plan.source_bindings:
            raise ProjectError("render plan source bindings are stale")
    expected_clips = tuple(
        RenderClip(
            clip.clip_id,
            clip.source_id,
            clip.source_in_ticks,
            clip.source_out_ticks,
        )
        for clip in proposal_clips
    )
    if expected_clips != plan.clips:
        raise ProjectError("render plan clips do not match active Decision")
    current_sources = tuple(
        _source_for_decision(project, source.source_id) for source in frozen_sources
    )
    if current_sources != frozen_sources:
        raise ProjectError("render source snapshot is stale")
    for source in frozen_sources:
        source_path = source_paths.get(source.source_id)
        if source_path is None or fingerprint_file(source_path) != source.fingerprint:
            raise ProjectError("render source fingerprint has changed")


def _validate_active_transcript_bindings(
    project: Project, decision: MultiSourceEditDecision
) -> None:
    for binding in decision.proposal_snapshot.source_bindings:
        if project.active_transcript_versions.get(binding.source_id) != binding.transcript_version_id:
            raise ProjectError("render decision transcript binding is stale")


def _read_decision(project_path: Path, edit_version_id: str) -> EditDecision | MultiSourceEditDecision:
    data = read_json_object(
        project_path / "edits" / f"{edit_version_id}.json",
        description="edit decision",
    )
    schema_version = data.get("schema_version")
    if schema_version == 1:
        decision: EditDecision | MultiSourceEditDecision = EditDecision.from_dict(data)
    elif schema_version == 2:
        decision = MultiSourceEditDecision.from_dict(data)
    else:
        raise ProjectError("unsupported edit decision schema")
    if decision.edit_version_id != edit_version_id:
        raise ProjectError("edit decision identity mismatch")
    return decision


def _render_manifest(
    plan: RenderPlanLike,
    schedule: RenderSchedule,
    report: VerificationReport,
    filter_script: str,
    command: list[str],
    source_paths: dict[str, Path],
    filter_path: Path,
    candidate_path: Path,
    performance: dict[str, float],
) -> dict[str, object]:
    source_values = {str(path) for path in source_paths.values()}
    sanitized_command = [
        "<source>" if value in source_values else
        "<filter-script>" if value == str(filter_path) else
        "<output>" if value == str(candidate_path) else value
        for value in command
    ]
    arguments_json = json.dumps(sanitized_command, ensure_ascii=False, separators=(",", ":"))
    common: dict[str, object] = {
        "render_id": plan.render_id,
        "project_id": plan.project_id,
        "project_revision": plan.project_revision,
        "edit_version_id": plan.edit_version_id,
        "tools": {
            "ffmpeg_version": plan.ffmpeg.version,
            "ffprobe_version": plan.ffprobe.version,
        },
        "output_settings": plan.output_settings.to_dict(),
        "clips": [clip.to_dict() for clip in plan.clips],
        "total_duration_ticks": plan.total_duration_ticks,
        "render_schedule": schedule.to_dict(),
        "command_summary": {
            "filter_sha256": hashlib.sha256(filter_script.encode("utf-8")).hexdigest(),
            "arguments_sha256": hashlib.sha256(arguments_json.encode("utf-8")).hexdigest(),
            "video_encoder": "libx264",
            "audio_encoder": "aac" if render_has_audio(plan) else None,
            "faststart": True,
            "input_strategy": schedule.strategy,
            "input_count": len(schedule.clips),
            "input_side_accurate_seek": True,
            "bounded_input_duration": True,
            "process_concurrency": 1,
        },
        "performance": performance,
        "output": {
            "mp4_path": plan.output_relative_path,
            "probe": report.probe,
        },
        "acceptance": {
            "accepted": report.accepted,
            "checks": report.checks,
        },
    }
    if isinstance(plan, RenderPlan):
        return {
            "schema_version": 2,
            **common,
            "input_source": {
                "source_id": plan.source.source_id,
                "fingerprint": plan.source.fingerprint.to_dict(),
                "probe": plan.source.probe.to_dict(),
            },
        }
    checks = _acceptance_checks(plan, report)
    common["acceptance"] = {"accepted": all(checks.values()), "checks": checks}
    return {
        "schema_version": 3,
        **common,
        "source_bindings": [binding.to_dict() for binding in plan.source_bindings],
        "input_sources": [
            {
                "source_id": source.source_id,
                "fingerprint": source.fingerprint.to_dict(),
                "probe": source.probe.to_dict(),
            }
            for source in plan.sources
        ],
    }


def _acceptance_checks(
    plan: RenderPlanLike, report: VerificationReport
) -> dict[str, bool]:
    checks = dict(report.checks)
    if isinstance(plan, MultiSourceRenderPlan):
        checks.update(
            decision_clips_match_plan=True,
            input_source_snapshots_verified=True,
        )
    return checks


def _write_candidate_manifest(path: Path, manifest: dict[str, object]) -> None:
    with path.open("x", encoding="utf-8") as manifest_file:
        json.dump(manifest, manifest_file, ensure_ascii=False, indent=2, sort_keys=True)
        manifest_file.write("\n")
        manifest_file.flush()
        os.fsync(manifest_file.fileno())


def _sync_file(path: Path) -> None:
    with path.open("rb+") as output_file:
        os.fsync(output_file.fileno())


def _sync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _validate_id(value: str, name: str) -> None:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ProjectError(f"{name} is invalid")
