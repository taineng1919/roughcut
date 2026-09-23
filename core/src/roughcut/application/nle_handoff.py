"""Application boundary for projecting and publishing editable NLE handoffs."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeAlias, cast

from roughcut.adapters.alignment_store import AlignmentStore
from roughcut.adapters.project_lock import project_write_lock
from roughcut.adapters.project_store import ProjectStore
from roughcut.adapters.workflow_candidates import (
    StagedWorkflowFile,
    publish_workflow_candidate,
)
from roughcut.adapters.workflow_store import WorkflowStore
from roughcut.application.multicam_continuation import multicam_alignment_status
from roughcut.application.renders import _resolve_source_path
from roughcut.application.sources import fingerprint_file
from roughcut.application.multicam_parallel import _read_adopted_decision
from roughcut.domain.alignment import (
    AlignmentSourceFingerprint,
    MulticamAlignmentArtifact,
    project_alignment_intervals,
)
from roughcut.domain.edit import EditClip, EditDecision, MultiSourceEditDecision
from roughcut.domain.nle_handoff import (
    NLE_EXPORT_PROFILES,
    NLE_EXPORT_SCHEMA_VERSION,
    NLE_ROUTES,
    NleExportReceipt,
    NleHandoffClip,
    NleHandoffError,
    NleHandoffGap,
    NleHandoffTimeline,
    NleHandoffTrack,
    NleSourceSnapshot,
    nle_export_request_hash,
    source_snapshot_hash,
)
from roughcut.domain.errors import ProjectError
from roughcut.domain.project import Project, SourceAsset
from roughcut.domain.time import RationalRate
from roughcut.domain.workflow import (
    ArtifactRef,
    MulticamSetup,
    WorkflowRun,
    canonical_sha256_v1,
)

DecisionLike: TypeAlias = EditDecision | MultiSourceEditDecision


@dataclass(frozen=True)
class NlePreparedExport:
    """Validated export basis plus the in-memory timeline; no files are written."""

    project: Project
    run: WorkflowRun
    decision: DecisionLike
    decision_ref: ArtifactRef
    timeline: NleHandoffTimeline
    source_snapshots: tuple[NleSourceSnapshot, ...]
    alignment: MulticamAlignmentArtifact | None
    destination: Path
    route: str
    exporter_profile: str
    request: dict[str, object]
    request_hash: str


@dataclass(frozen=True)
class NleExportOutcome:
    receipt: NleExportReceipt
    readback: bool
    timeline_summary: dict[str, object] | None

    def to_dict(self) -> dict[str, object]:
        return {
            "nle_export": {
                "schema_version": NLE_EXPORT_SCHEMA_VERSION,
                "action_id": self.receipt.action_id,
                "project_id": self.receipt.project_id,
                "run_id": self.receipt.run_id,
                "project_revision": self.receipt.project_revision,
                "edit_version_id": self.receipt.edit_version_id,
                "route": self.receipt.route,
                "exporter_profile": self.receipt.exporter_profile,
                "destination": self.receipt.destination,
                "alignment_artifact_id": self.receipt.alignment_artifact_id,
                "output_sha256": self.receipt.output_sha256,
                "output_bytes": self.receipt.output_bytes,
                "readback": self.readback,
                "timeline": self.timeline_summary,
            },
            "receipt": self.receipt.to_dict(),
        }


def project_handoff_timeline(
    project: Project,
    decision: DecisionLike,
    source_paths: dict[str, Path],
    alignment: MulticamAlignmentArtifact | None = None,
) -> NleHandoffTimeline:
    """Project one adopted Decision and optional exact Alignment into one timeline."""

    if decision.edit_version_id != project.active_edit_version_id:
        raise _error(
            "nle_export_decision_stale",
            "the Decision is not the Project active Decision",
        )
    settings = project.settings
    rate_data = settings.get("frame_rate")
    if not isinstance(rate_data, dict):
        raise _error("nle_export_integrity_error", "the Project frame rate is invalid")
    try:
        from roughcut.domain.time import RationalRate, TICKS_PER_SECOND

        frame_rate = RationalRate(
            _required_int(rate_data.get("numerator"), "frame-rate numerator"),
            _required_int(rate_data.get("denominator"), "frame-rate denominator"),
        )
        timebase = _required_int(settings.get("timebase"), "Project timebase")
        width = _required_int(settings.get("width"), "Project width")
        height = _required_int(settings.get("height"), "Project height")
        audio_sample_rate = _required_int(
            settings.get("audio_sample_rate"), "Project audio sample rate"
        )
    except (ProjectError, ValueError) as error:
        raise _error("nle_export_integrity_error", "the Project settings are invalid") from error
    if timebase != TICKS_PER_SECOND:
        raise _error("nle_export_integrity_error", "the Project timebase is not canonical")

    source_by_id = {source.source_id: source for source in project.sources}
    clips = tuple(decision.proposal_snapshot.clips)
    if not clips:
        raise _error("nle_export_decision_stale", "the adopted Decision has no clips")
    main_items: list[NleHandoffClip] = []
    timeline_cursor = 0
    for clip in clips:
        source = source_by_id.get(clip.source_id)
        if source is None or clip.source_id not in source_paths:
            raise _error(
                "nle_export_source_stale",
                f"Decision source {clip.source_id!r} is not current",
            )
        _validate_decision_clip(clip, source)
        timeline_end = timeline_cursor + clip.duration_ticks
        main_items.append(
            _make_handoff_clip(
                clip_id=clip.clip_id,
                source=source,
                source_path=source_paths[clip.source_id],
                source_in=clip.source_in_ticks,
                source_out=clip.source_out_ticks,
                timeline_in=timeline_cursor,
                timeline_out=timeline_end,
                camera_id="main",
                track_id="main",
                av_link_id=_stable_id("link", clip.clip_id),
            )
        )
        timeline_cursor = timeline_end
    if timeline_cursor <= 0:
        raise _error("nle_export_decision_stale", "the adopted Decision duration is not positive")

    main_track = NleHandoffTrack(
        track_id="main",
        camera_id="main",
        role="main",
        media_types=_track_media_types(main_items),
        items=tuple(main_items),
    )
    tracks: list[NleHandoffTrack] = [main_track]
    if alignment is not None:
        for camera in alignment.auxiliary_cameras:
            track_id = _stable_id("aux", camera.camera_id)
            items, media_types = _project_auxiliary_track(
                camera.camera_id,
                track_id,
                camera.ordered_source_ids,
                alignment,
                clips,
                source_by_id,
                source_paths,
                timeline_duration=timeline_cursor,
            )
            tracks.append(
                NleHandoffTrack(
                    track_id=track_id,
                    camera_id=camera.camera_id,
                    role="auxiliary",
                    media_types=media_types,
                    items=tuple(items),
                )
            )

    return NleHandoffTimeline(
        project_id=project.project_id,
        timebase=timebase,
        frame_rate=frame_rate,
        video_width=width,
        video_height=height,
        audio_sample_rate=audio_sample_rate,
        duration_ticks=timeline_cursor,
        tracks=tuple(tracks),
    )


def prepare_nle_export(
    project_path: Path,
    *,
    run_id: str,
    action_id: str,
    edit_version_id: str,
    expected_revision: int,
    route: str,
    destination: str | Path,
    alignment_artifact_id: str | None,
) -> NlePreparedExport:
    """Load and validate all Core truth, returning an unwritten export basis."""

    _validate_route(route)
    _validate_safe_action_id(action_id)
    _validate_safe_action_id(run_id)
    _validate_safe_action_id(edit_version_id)
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise _error("nle_export_invalid_arguments", "expected_revision is invalid")

    store = ProjectStore(project_path)
    project = store.load()
    if project.revision != expected_revision:
        raise _error("nle_export_decision_stale", "Project revision does not match the request")
    try:
        decision, decision_ref_data, adoption = _read_adopted_decision(
            store.project_path,
            project,
            edit_version_id,
        )
        if decision_ref_data.get("kind") != "decision" or not isinstance(
            decision_ref_data.get("edit_version_id"), str
        ):
            raise ValueError("the adopted Decision ref is not closed")
        decision_ref = ArtifactRef(
            artifact_id=cast(str, decision_ref_data["edit_version_id"]),
            schema_version=cast(int, decision_ref_data["schema_version"]),
            content_hash=cast(str, decision_ref_data["content_hash"]),
        )
        adopted_run_id = adoption.get("run_id")
        if adopted_run_id != run_id:
            raise ValueError("the requested run is not the exact adoption run")
    except Exception as error:
        raise _error(
            "nle_export_decision_not_adopted",
            "the exact adopted Decision chain is unreadable or stale",
        ) from error
    if decision.edit_version_id != edit_version_id:
        raise _error("nle_export_decision_stale", "the Decision artifact identity changed")
    run_store = WorkflowStore(store.project_path)
    try:
        run = run_store.read_run(run_id)
    except Exception as error:
        raise _error(
            "nle_export_decision_not_adopted", "the requested WorkflowRun is unreadable"
        ) from error
    if run.run_id != run_id or run.project_id != project.project_id:
        raise _error(
            "nle_export_decision_not_adopted", "the WorkflowRun belongs to another Project"
        )
    current_ref = run.artifact_refs.get("decision")
    if current_ref != decision_ref:
        raise _error(
            "nle_export_decision_not_adopted", "the requested Decision is not current for this run"
        )

    destination_path = _canonical_destination(store.project_path, destination)
    _validate_destination_suffix(route, destination_path)
    _validate_destination_parent(destination_path)

    alignment_status = _alignment_status(store.project_path, run)
    alignment = _load_alignment(
        store.project_path,
        project,
        run,
        alignment_status,
        alignment_artifact_id,
    )
    source_paths = _validate_current_sources(project, decision, run, alignment, store.project_path)
    timeline = project_handoff_timeline(project, decision, source_paths, alignment)
    snapshots = _source_snapshots(project, decision, run, alignment)
    request = _request_projection(
        project=project,
        run=run,
        action_id=action_id,
        expected_revision=expected_revision,
        decision_ref=decision_ref,
        route=route,
        destination=destination_path,
        snapshots=snapshots,
        alignment=alignment,
    )
    return NlePreparedExport(
        project=project,
        run=run,
        decision=decision,
        decision_ref=decision_ref,
        timeline=timeline,
        source_snapshots=snapshots,
        alignment=alignment,
        destination=destination_path,
        route=route,
        exporter_profile=NLE_EXPORT_PROFILES[route],
        request=request,
        request_hash=nle_export_request_hash(request),
    )


def write_nle_export(prepared: NlePreparedExport) -> bytes:
    """Serialize an already validated timeline with one fixed writer profile."""

    if prepared.route == "fcpxml":
        from roughcut.adapters.fcpxml import write_fcpxml

        return write_fcpxml(prepared.timeline)
    if prepared.route == "fcp7_xml":
        from roughcut.adapters.fcp7_xml import write_fcp7_xml

        return write_fcp7_xml(prepared.timeline)
    raise _error("nle_export_invalid_arguments", "the export route is unsupported")


def approve_nle_export(
    project_path: Path,
    *,
    run_id: str,
    action_id: str,
    edit_version_id: str,
    expected_revision: int,
    route: str,
    destination: str | Path,
    alignment_artifact_id: str | None,
) -> NleExportOutcome:
    """Approve and atomically publish one exact NLE handoff file."""

    root = ProjectStore(project_path).project_path
    with project_write_lock(root):
        return _approve_nle_export_locked(
            root,
            run_id=run_id,
            action_id=action_id,
            edit_version_id=edit_version_id,
            expected_revision=expected_revision,
            route=route,
            destination=destination,
            alignment_artifact_id=alignment_artifact_id,
        )


def _approve_nle_export_locked(
    project_path: Path,
    *,
    run_id: str,
    action_id: str,
    edit_version_id: str,
    expected_revision: int,
    route: str,
    destination: str | Path,
    alignment_artifact_id: str | None,
) -> NleExportOutcome:
    from roughcut.adapters.nle_export_store import NleExportStore

    root = ProjectStore(project_path).project_path
    canonical_destination = _canonical_destination(root, destination)
    receipt_store = NleExportStore(root)
    existing = receipt_store.read(action_id)
    if existing is not None:
        _validate_existing_receipt_request(
            existing,
            root=root,
            run_id=run_id,
            action_id=action_id,
            edit_version_id=edit_version_id,
            expected_revision=expected_revision,
            route=route,
            destination=canonical_destination,
            alignment_artifact_id=alignment_artifact_id,
        )
        prepared = prepare_nle_export(
            root,
            run_id=run_id,
            action_id=action_id,
            edit_version_id=edit_version_id,
            expected_revision=expected_revision,
            route=route,
            destination=canonical_destination,
            alignment_artifact_id=alignment_artifact_id,
        )
        if existing.request_hash != prepared.request_hash:
            raise _error(
                "nle_export_action_conflict",
                "the current truth does not match the existing NLE receipt",
            )
        receipt_store.validate_output(existing)
        return NleExportOutcome(existing, True, _timeline_summary(prepared.timeline))

    prepared = prepare_nle_export(
        root,
        run_id=run_id,
        action_id=action_id,
        edit_version_id=edit_version_id,
        expected_revision=expected_revision,
        route=route,
        destination=canonical_destination,
        alignment_artifact_id=alignment_artifact_id,
    )
    if os.path.lexists(prepared.destination):
        raise _error(
            "nle_export_destination_conflict",
            "the destination already exists and will not be overwritten",
        )
    payload = write_nle_export(prepared)
    if not payload:
        raise _error("nle_export_write_failed", "the writer returned an empty file")
    output_hash = hashlib.sha256(payload).hexdigest()
    temporary: Path | None = None
    staged_identity: tuple[int, int] | None = None
    published_receipt: NleExportReceipt | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=prepared.destination.parent,
            prefix=f".{prepared.destination.name}.{action_id}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        staged_details = _validate_published_file(temporary)
        staged_identity = (staged_details.st_dev, staged_details.st_ino)
        if staged_details.st_size != len(payload) or _file_sha256(temporary) != output_hash:
            raise _error("nle_export_integrity_error", "staged output readback changed")
        publish_workflow_candidate(
            prepared.destination,
            action_id,
            StagedWorkflowFile(temporary),
        )
        temporary = None
        details = _validate_published_file(prepared.destination)
        if details.st_size != len(payload) or _file_sha256(prepared.destination) != output_hash:
            raise _error("nle_export_integrity_error", "published output readback changed")
        if staged_identity != (details.st_dev, details.st_ino):
            raise _error("nle_export_integrity_error", "published output identity changed")
        receipt = NleExportReceipt(
            schema_version=NLE_EXPORT_SCHEMA_VERSION,
            action_id=action_id,
            request_hash=prepared.request_hash,
            project_id=prepared.project.project_id,
            run_id=prepared.run.run_id,
            project_revision=prepared.project.revision,
            edit_version_id=prepared.decision.edit_version_id,
            decision_schema_version=prepared.decision.schema_version,
            decision_content_hash=prepared.decision_ref.content_hash,
            route=prepared.route,
            exporter_profile=prepared.exporter_profile,
            destination=str(prepared.destination),
            source_snapshots=prepared.source_snapshots,
            alignment_artifact_id=(
                None if prepared.alignment is None else prepared.alignment.alignment_id
            ),
            alignment_content_hash=(
                None if prepared.alignment is None else prepared.alignment.content_hash
            ),
            output_sha256=output_hash,
            output_bytes=len(payload),
            created_at=_timestamp(),
        )
        published_receipt = receipt
        stored = receipt_store.write(receipt)
        return NleExportOutcome(stored, stored != receipt, _timeline_summary(prepared.timeline))
    except Exception:
        if published_receipt is not None:
            try:
                receipt_store.delete_if_exact(published_receipt)
            except NleHandoffError:
                raise
            except Exception as error:
                raise _error(
                    "nle_export_recovery_conflict",
                    "failed receipt cleanup left uncertain receipt state",
                ) from error
        if staged_identity is not None:
            _reconcile_failed_output(
                prepared.destination,
                temporary=temporary,
                staged_identity=staged_identity,
                expected_size=len(payload),
                expected_sha256=output_hash,
            )
        elif temporary is not None:
            _remove_temp(temporary)
        raise


def _alignment_status(project_path: Path, run: WorkflowRun) -> dict[str, object] | None:
    try:
        return multicam_alignment_status(project_path, run)
    except Exception as error:
        raise _error(
            "nle_export_alignment_not_deliverable",
            "the exact Alignment status could not be validated",
        ) from error


def _load_alignment(
    project_path: Path,
    project: Project,
    run: WorkflowRun,
    status: dict[str, object] | None,
    alignment_artifact_id: str | None,
) -> MulticamAlignmentArtifact | None:
    setup = run.multicam_setup
    has_auxiliary = setup is not None and bool(setup.auxiliary_cameras)
    if not has_auxiliary:
        if alignment_artifact_id is not None:
            raise _error(
                "nle_export_alignment_not_expected",
                "a single-camera handoff must use alignment=null",
            )
        return None
    if alignment_artifact_id is None:
        raise _error(
            "nle_export_alignment_required",
            "a multicam handoff requires the exact deliverable Alignment",
        )
    if status is None or status.get("status") not in {"succeeded", "partial"}:
        raise _error(
            "nle_export_alignment_not_deliverable",
            "the exact Alignment producer is not in a deliverable state",
        )
    raw_ref = status.get("alignment_ref")
    if not isinstance(raw_ref, dict) or set(raw_ref) != {
        "kind",
        "alignment_id",
        "schema_version",
        "content_hash",
    }:
        raise _error("nle_export_alignment_not_deliverable", "Alignment result ref is not closed")
    if (
        raw_ref.get("kind") != "multicam_alignment"
        or raw_ref.get("alignment_id") != alignment_artifact_id
        or raw_ref.get("schema_version") != 1
        or not isinstance(raw_ref.get("content_hash"), str)
    ):
        raise _error(
            "nle_export_alignment_not_deliverable",
            "Alignment result ref does not match the request",
        )
    try:
        artifact = AlignmentStore(project_path).read(alignment_artifact_id)
    except Exception as error:
        raise _error(
            "nle_export_alignment_not_deliverable", "Alignment artifact is unreadable"
        ) from error
    if (
        artifact is None
        or artifact.project_id != project.project_id
        or artifact.content_hash != raw_ref["content_hash"]
    ):
        raise _error("nle_export_alignment_not_deliverable", "Alignment artifact identity is stale")
    _validate_alignment_groups(project, run, artifact)
    return artifact


def _validate_alignment_groups(
    project: Project,
    run: WorkflowRun,
    artifact: MulticamAlignmentArtifact,
) -> None:
    setup = run.multicam_setup
    if setup is None:
        raise _error(
            "nle_export_alignment_not_deliverable",
            "Alignment has no durable multicam setup",
        )
    if (
        artifact.main_camera.camera_id != setup.main_camera.camera_id
        or artifact.main_camera.ordered_source_ids != _expected_aligned_main_source_ids(setup)
    ):
        raise _error(
            "nle_export_alignment_not_deliverable",
            "Alignment main camera identity changed",
        )
    artifact_auxiliary = [
        (camera.camera_id, tuple(camera.ordered_source_ids))
        for camera in artifact.auxiliary_cameras
    ]
    setup_auxiliary = [
        (camera.camera_id, tuple(camera.ordered_source_ids)) for camera in setup.auxiliary_cameras
    ]
    if artifact_auxiliary != setup_auxiliary:
        raise _error(
            "nle_export_alignment_not_deliverable",
            "Alignment auxiliary camera identity changed",
        )
    source_ids = set(artifact.main_camera.ordered_source_ids)
    for camera in artifact.auxiliary_cameras:
        source_ids.update(camera.ordered_source_ids)
    project_ids = {source.source_id for source in project.sources}
    if not source_ids <= project_ids:
        raise _error(
            "nle_export_alignment_not_deliverable",
            "Alignment references a missing Source",
        )
    expected_basis = {
        (artifact.main_camera.camera_id, source_id)
        for source_id in artifact.main_camera.ordered_source_ids
    }
    for camera in artifact.auxiliary_cameras:
        expected_basis.update(
            (camera.camera_id, source_id) for source_id in camera.ordered_source_ids
        )
    actual_basis = {(basis.camera_id, basis.source_id) for basis in artifact.source_basis}
    if actual_basis != expected_basis:
        raise _error(
            "nle_export_alignment_not_deliverable",
            "Alignment source basis changed",
        )


def _expected_aligned_main_source_ids(setup: MulticamSetup) -> tuple[str, ...]:
    """Return the exact main Sources the Alignment producer must have aligned.

    Only setup main Sources that participate in a declared ``source_pairs``
    entry enter the Alignment. Their order always follows the setup main
    camera order. When the setup declares no pair at all, Alignment uses the
    implicit single main/aux pair, so every setup main Source is expected.
    """

    paired_source_ids = {pair.main_source_id for pair in setup.source_pairs}
    if not paired_source_ids:
        return setup.main_camera.ordered_source_ids
    return tuple(
        source_id
        for source_id in setup.main_camera.ordered_source_ids
        if source_id in paired_source_ids
    )


def _validate_current_sources(
    project: Project,
    decision: DecisionLike,
    run: WorkflowRun,
    alignment: MulticamAlignmentArtifact | None,
    project_path: Path,
) -> dict[str, Path]:
    required_ids = {clip.source_id for clip in decision.proposal_snapshot.clips}
    if alignment is not None:
        required_ids.update(alignment.main_camera.ordered_source_ids)
        for camera in alignment.auxiliary_cameras:
            required_ids.update(camera.ordered_source_ids)
        setup = run.multicam_setup
        if setup is None:
            raise _error("nle_export_alignment_not_deliverable", "multicam setup is missing")
        if any(
            clip.source_id not in set(setup.main_camera.ordered_source_ids)
            for clip in decision.proposal_snapshot.clips
        ):
            raise _error(
                "nle_export_alignment_not_deliverable",
                "Decision references a Source outside the Alignment main camera",
            )
    source_by_id = {source.source_id: source for source in project.sources}
    paths: dict[str, Path] = {}
    for source_id in sorted(required_ids):
        source = source_by_id.get(source_id)
        if source is None:
            raise _error("nle_export_source_stale", f"Source {source_id!r} is missing")
        try:
            path = _resolve_source_path(project_path, source)
            if fingerprint_file(path) != source.fingerprint:
                raise _error("nle_export_source_stale", f"Source {source_id!r} fingerprint changed")
        except NleHandoffError:
            raise
        except (OSError, ProjectError) as error:
            raise _error(
                "nle_export_source_stale", f"Source {source_id!r} is missing or unreadable"
            ) from error
        if source.probe.video_codec is None and source.probe.audio_codec is None:
            raise _error(
                "nle_export_source_stale", f"Source {source_id!r} has no usable media stream"
            )
        paths[source_id] = path
    if alignment is not None:
        _validate_alignment_basis(project, alignment, source_by_id)
        _validate_setup_snapshots(run, source_by_id)
    return paths


def _validate_alignment_basis(
    project: Project,
    alignment: MulticamAlignmentArtifact,
    sources: dict[str, SourceAsset],
) -> None:
    del project
    for basis in alignment.source_basis:
        source = sources.get(basis.source_id)
        if source is None:
            raise _error("nle_export_alignment_not_deliverable", "Alignment source is missing")
        expected = AlignmentSourceFingerprint(
            source.fingerprint.size,
            source.fingerprint.mtime_ns,
            source.fingerprint.sha256_head_tail,
        )
        if basis.fingerprint != expected or basis.duration_ticks != source.probe.duration_ticks:
            raise _error(
                "nle_export_alignment_not_deliverable", "Alignment source snapshot is stale"
            )


def _validate_setup_snapshots(run: WorkflowRun, sources: dict[str, SourceAsset]) -> None:
    setup = run.multicam_setup
    if setup is None:
        return
    for raw in setup.source_snapshots:
        source_id = cast(str, raw["source_id"])
        source = sources.get(source_id)
        if source is None:
            raise _error("nle_export_source_stale", "multicam setup Source is missing")
        expected = {
            "source_id": source.source_id,
            "import_mode": source.import_mode.value,
            "fingerprint": source.fingerprint.to_dict(),
            "display_name": source.display_name,
            "tags": list(source.tags),
            "note": source.note,
        }
        if expected != raw:
            raise _error("nle_export_source_stale", "multicam setup Source snapshot is stale")


def _source_snapshots(
    project: Project,
    decision: DecisionLike,
    run: WorkflowRun,
    alignment: MulticamAlignmentArtifact | None,
) -> tuple[NleSourceSnapshot, ...]:
    ids = {clip.source_id for clip in decision.proposal_snapshot.clips}
    if alignment is not None:
        ids.update(alignment.main_camera.ordered_source_ids)
        for camera in alignment.auxiliary_cameras:
            ids.update(camera.ordered_source_ids)
    sources = {source.source_id: source for source in project.sources}
    setup = run.multicam_setup
    if setup is not None and alignment is not None:
        ids.update(cast(str, snapshot["source_id"]) for snapshot in setup.source_snapshots)
    result: list[NleSourceSnapshot] = []
    for source_id in sorted(ids):
        source = sources.get(source_id)
        if source is None:
            raise _error("nle_export_source_stale", f"Source {source_id!r} is missing")
        result.append(
            NleSourceSnapshot(
                source_id=source.source_id,
                snapshot_hash=source_snapshot_hash(source),
                locator_hash=canonical_sha256_v1({"locator": source.locator}),
                fingerprint=source.fingerprint,
                duration_ticks=source.probe.duration_ticks,
            )
        )
    return tuple(result)


def _request_projection(
    *,
    project: Project,
    run: WorkflowRun,
    action_id: str,
    expected_revision: int,
    decision_ref: ArtifactRef,
    route: str,
    destination: Path,
    snapshots: tuple[NleSourceSnapshot, ...],
    alignment: MulticamAlignmentArtifact | None,
) -> dict[str, object]:
    return {
        "schema_version": NLE_EXPORT_SCHEMA_VERSION,
        "action": "approve_nle_export",
        "action_id": action_id,
        "project_id": project.project_id,
        "run_id": run.run_id,
        "expected_project_revision": expected_revision,
        "decision": {
            "edit_version_id": decision_ref.artifact_id,
            "schema_version": decision_ref.schema_version,
            "content_hash": decision_ref.content_hash,
        },
        "route": route,
        "exporter_profile": NLE_EXPORT_PROFILES[route],
        "destination": str(destination),
        "source_snapshots": [snapshot.to_dict() for snapshot in snapshots],
        "alignment": (
            None
            if alignment is None
            else {
                "alignment_id": alignment.alignment_id,
                "schema_version": alignment.schema_version,
                "content_hash": alignment.content_hash,
            }
        ),
    }


def _validate_existing_receipt_request(
    receipt: NleExportReceipt,
    *,
    root: Path,
    run_id: str,
    action_id: str,
    edit_version_id: str,
    expected_revision: int,
    route: str,
    destination: Path,
    alignment_artifact_id: str | None,
) -> None:
    _validate_route(route)
    if (
        receipt.action_id != action_id
        or receipt.run_id != run_id
        or receipt.edit_version_id != edit_version_id
        or receipt.project_revision != expected_revision
        or receipt.route != route
        or receipt.exporter_profile != NLE_EXPORT_PROFILES[route]
        or receipt.destination != str(destination)
        or receipt.alignment_artifact_id != alignment_artifact_id
    ):
        raise _error("nle_export_action_conflict", "the action ID is bound to a different request")
    del root


def _timeline_summary(timeline: NleHandoffTimeline) -> dict[str, object]:
    return {
        "duration_ticks": timeline.duration_ticks,
        "track_count": len(timeline.tracks),
        "tracks": [
            {
                "track_id": track.track_id,
                "camera_id": track.camera_id,
                "role": track.role,
                "media_types": list(track.media_types),
                "clip_count": len(track.clips),
                "gap_count": len(track.gaps),
            }
            for track in timeline.tracks
        ],
    }


def _make_handoff_clip(
    *,
    clip_id: str,
    source: SourceAsset,
    source_path: Path,
    source_in: int,
    source_out: int,
    timeline_in: int,
    timeline_out: int,
    camera_id: str,
    track_id: str,
    av_link_id: str,
) -> NleHandoffClip:
    return NleHandoffClip(
        logical_clip_id=clip_id,
        source_id=source.source_id,
        source_locator=source_path,
        source_duration_ticks=source.probe.duration_ticks,
        source_display_name=source.display_name,
        source_width=source.probe.width,
        source_height=source.probe.height,
        source_nominal_frame_rate=_probe_source_rate(source),
        source_is_vfr=source.probe.is_vfr,
        source_audio_sample_rate=source.probe.audio_sample_rate,
        source_in_ticks=source_in,
        source_out_ticks=source_out,
        timeline_in_ticks=timeline_in,
        timeline_out_ticks=timeline_out,
        camera_id=camera_id,
        track_id=track_id,
        media_types=_source_media_types(source),
        av_link_id=av_link_id,
    )


def _project_auxiliary_track(
    camera_id: str,
    track_id: str,
    source_ids: tuple[str, ...],
    alignment: MulticamAlignmentArtifact,
    decision_clips: tuple[EditClip, ...],
    sources: dict[str, SourceAsset],
    source_paths: dict[str, Path],
    *,
    timeline_duration: int,
) -> tuple[list[NleHandoffClip | NleHandoffGap], tuple[str, ...]]:
    items: list[NleHandoffClip | NleHandoffGap] = []
    gap_ranges: list[tuple[int, int]] = []
    timeline_cursor = 0
    source_id_set = set(source_ids)
    alignment_main_source_ids = set(alignment.main_camera.ordered_source_ids)
    for clip in decision_clips:
        if clip.source_id not in alignment_main_source_ids:
            # The exact Alignment only covers its own declared main Sources. A
            # Decision clip from an unpaired main Source has no mapping at all,
            # so its whole range is a legal absence on every auxiliary track;
            # no media, offset or auxiliary range is guessed for it.
            gap_ranges.append((timeline_cursor, timeline_cursor + clip.duration_ticks))
            timeline_cursor += clip.duration_ticks
            continue
        try:
            projected = project_alignment_intervals(
                camera_id=camera_id,
                intervals=alignment.intervals,
                main_source_id=clip.source_id,
                main_start_ticks=clip.source_in_ticks,
                main_end_ticks=clip.source_out_ticks,
                timeline_start_ticks=timeline_cursor,
            )
        except Exception as error:
            raise _error(
                "nle_export_alignment_not_deliverable",
                "Alignment partition cannot be projected",
            ) from error
        for segment in projected:
            if segment.classification == "mapped":
                assert segment.auxiliary_source_id is not None
                assert segment.auxiliary_start_ticks is not None
                assert segment.auxiliary_end_ticks is not None
                if segment.auxiliary_source_id not in source_id_set:
                    raise _error(
                        "nle_export_alignment_not_deliverable",
                        "mapped interval uses a foreign Source",
                    )
                aux_source = sources.get(segment.auxiliary_source_id)
                aux_path = source_paths.get(segment.auxiliary_source_id)
                if aux_source is None or aux_path is None:
                    raise _error("nle_export_source_stale", "mapped auxiliary Source is missing")
                items.append(
                    _make_handoff_clip(
                        clip_id=_stable_id("auxclip", camera_id, clip.clip_id, segment.interval_id),
                        source=aux_source,
                        source_path=aux_path,
                        source_in=segment.auxiliary_start_ticks,
                        source_out=segment.auxiliary_end_ticks,
                        timeline_in=segment.timeline_start_ticks,
                        timeline_out=segment.timeline_end_ticks,
                        camera_id=camera_id,
                        track_id=track_id,
                        av_link_id=_stable_id("link", camera_id, clip.clip_id, segment.interval_id),
                    )
                )
            else:
                gap_ranges.append((segment.timeline_start_ticks, segment.timeline_end_ticks))
        timeline_cursor += clip.duration_ticks
    if timeline_cursor != timeline_duration:
        raise _error(
            "nle_export_integrity_error",
            "auxiliary projection duration differs from the main timeline",
        )
    gap_ranges.sort()
    merged: list[tuple[int, int]] = []
    for start, end in gap_ranges:
        if merged and merged[-1][1] == start:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    for index, (start, end) in enumerate(merged):
        items.append(
            NleHandoffGap(
                gap_id=_stable_id("gap", camera_id, str(index)),
                timeline_in_ticks=start,
                timeline_out_ticks=end,
                camera_id=camera_id,
                track_id=track_id,
            )
        )
    items.sort(key=lambda item: item.timeline_in_ticks)
    if not items:
        items.append(
            NleHandoffGap(
                gap_id=_stable_id("gap", camera_id, "all"),
                timeline_in_ticks=0,
                timeline_out_ticks=timeline_duration,
                camera_id=camera_id,
                track_id=track_id,
            )
        )
    if items[0].timeline_in_ticks != 0 or items[-1].timeline_out_ticks != timeline_duration:
        raise _error(
            "nle_export_alignment_not_deliverable",
            "auxiliary projection does not cover the timeline",
        )
    return items, _camera_media_types(source_ids, sources)


def _validate_decision_clip(clip: EditClip, source: SourceAsset) -> None:
    if clip.source_in_ticks < 0 or clip.source_out_ticks <= clip.source_in_ticks:
        raise _error("nle_export_decision_stale", "Decision contains an invalid source range")
    if clip.source_out_ticks > source.probe.duration_ticks:
        raise _error("nle_export_source_stale", "Decision range exceeds the current Source")


def _probe_source_rate(source: SourceAsset) -> RationalRate | None:
    raw_rate = source.probe.nominal_frame_rate
    if raw_rate is None:
        return None
    try:
        numerator = _required_int(raw_rate.get("numerator"), "source frame-rate numerator")
        denominator = _required_int(raw_rate.get("denominator"), "source frame-rate denominator")
        return RationalRate(numerator, denominator)
    except (ProjectError, ValueError) as error:
        raise _error("nle_export_source_stale", "Source nominal frame rate is invalid") from error


def _source_media_types(source: SourceAsset) -> tuple[str, ...]:
    types = tuple(
        item
        for item, present in (
            ("video", source.probe.video_codec is not None),
            ("audio", source.probe.audio_codec is not None),
        )
        if present
    )
    if not types:
        raise _error("nle_export_source_stale", "Source has no usable video or audio")
    return types


def _track_media_types(items: list[NleHandoffClip]) -> tuple[str, ...]:
    values: set[str] = set()
    for item in items:
        values.update(item.media_types)
    return tuple(item for item in ("video", "audio") if item in values)


def _camera_media_types(
    source_ids: tuple[str, ...], sources: dict[str, SourceAsset]
) -> tuple[str, ...]:
    values: set[str] = set()
    for source_id in source_ids:
        source = sources.get(source_id)
        if source is not None:
            values.update(_source_media_types(source))
    if not values:
        raise _error("nle_export_source_stale", "camera has no usable Source media")
    return tuple(item for item in ("video", "audio") if item in values)


def _required_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProjectError(f"{name} must be an integer")
    return value


def _stable_id(prefix: str, *parts: str) -> str:
    value = f"{prefix}_{'_'.join(parts)}"
    if len(value) <= 128:
        return value
    return f"{prefix}_{canonical_sha256_v1(list(parts))[:32]}"


def _validate_route(route: str) -> None:
    if not isinstance(route, str) or route not in NLE_ROUTES:
        raise _error("nle_export_invalid_arguments", "route must be fcpxml or fcp7_xml")


def _validate_safe_action_id(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or any(
            character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
            for character in value
        )
    ):
        raise _error("nle_export_invalid_arguments", "action/run/Decision identity is invalid")


def _canonical_destination(root: Path, value: str | Path) -> Path:
    try:
        raw = Path(value).expanduser()
        if not raw.is_absolute():
            raw = root / raw
        normalized = Path(os.path.normpath(os.path.abspath(os.fspath(raw))))
        if "\x00" in os.fspath(normalized):
            raise ValueError("destination contains NUL")
        if os.path.lexists(normalized):
            details = os.lstat(normalized)
            if stat.S_ISLNK(details.st_mode):
                raise ValueError("destination is a symlink")
        # Resolve existing parent symlinks (for example macOS /var) before
        # validating every actual directory component. The final target itself
        # was checked above and is never silently followed.
        return normalized.resolve(strict=False)
    except (OSError, TypeError, ValueError) as error:
        raise _error("nle_export_invalid_arguments", "destination is invalid") from error


def _validate_destination_suffix(route: str, destination: Path) -> None:
    expected = ".fcpxml" if route == "fcpxml" else ".xml"
    if destination.suffix != expected:
        raise _error(
            "nle_export_invalid_destination",
            f"destination must use the {expected} suffix for route {route!r}",
        )


def _validate_destination_parent(destination: Path) -> None:
    parent = destination.parent
    try:
        parts = parent.parts
        current = Path(parts[0])
        for part in parts[1:]:
            current = current / part
            details = os.lstat(current)
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
                raise _error(
                    "nle_export_invalid_destination",
                    "destination parent contains an unsafe path component",
                )
    except NleHandoffError:
        raise
    except (OSError, IndexError) as error:
        raise _error(
            "nle_export_invalid_destination", "destination parent does not exist"
        ) from error


def _validate_published_file(path: Path) -> os.stat_result:
    details = os.lstat(path)
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
        raise _error("nle_export_integrity_error", "output is not a single-link regular file")
    return details


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reconcile_failed_output(
    destination: Path,
    *,
    temporary: Path | None,
    staged_identity: tuple[int, int],
    expected_size: int,
    expected_sha256: str,
) -> None:
    """Reconcile only the output inode owned by this failed publication."""

    try:
        details = os.lstat(destination)
    except FileNotFoundError:
        if temporary is not None:
            _remove_temp(temporary)
        return
    except OSError as error:
        raise _error(
            "nle_export_recovery_conflict",
            "could not inspect the failed NLE output",
        ) from error

    safe_regular = (
        stat.S_ISREG(details.st_mode)
        and not stat.S_ISLNK(details.st_mode)
        and details.st_nlink == 1
    )
    matches = False
    if safe_regular:
        try:
            matches = (
                (details.st_dev, details.st_ino) == staged_identity
                and details.st_size == expected_size
                and _file_sha256(destination) == expected_sha256
            )
        except OSError as error:
            raise _error(
                "nle_export_recovery_conflict",
                "could not verify the failed NLE output identity",
            ) from error

    if not matches:
        if temporary is not None:
            _remove_temp(temporary)
        raise _error(
            "nle_export_recovery_conflict",
            "failed NLE output is not the exact staged inode; preserved the changed output",
        )
    try:
        destination.unlink()
        _sync_directory(destination.parent)
    except OSError as error:
        raise _error(
            "nle_export_recovery_conflict",
            "could not remove the exact failed NLE output",
        ) from error
    if os.path.lexists(destination):
        raise _error(
            "nle_export_recovery_conflict",
            "exact failed NLE output remained after removal",
        )
    if temporary is not None:
        _remove_temp(temporary)


def _remove_temp(path: Path) -> None:
    try:
        if os.path.lexists(path):
            details = os.lstat(path)
            if (
                stat.S_ISLNK(details.st_mode)
                or not stat.S_ISREG(details.st_mode)
                or details.st_nlink != 1
            ):
                raise OSError("temporary output is unsafe")
            path.unlink()
            _sync_directory(path.parent)
    except OSError as error:
        raise _error(
            "nle_export_recovery_conflict", "temporary output could not be cleaned"
        ) from error


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        os.fsync(descriptor)
    except OSError as error:
        raise _error(
            "nle_export_recovery_conflict",
            "NLE export directory sync failed",
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _error(code: str, evidence: str) -> NleHandoffError:
    return NleHandoffError(code, f"Roughcut NLE handoff {evidence}")


__all__ = [
    "NleExportOutcome",
    "NlePreparedExport",
    "approve_nle_export",
    "prepare_nle_export",
    "project_handoff_timeline",
    "write_nle_export",
]
