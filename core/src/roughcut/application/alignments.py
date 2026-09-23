"""Internal align_multicam coordinator: schema-2 Project-media operation."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import wave
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

from roughcut.adapters.alignment_store import AlignmentStore
from roughcut.adapters.audalign import (
    AudalignAdapterError,
    AudalignBudgetError,
    AudalignCandidateLimitError,
    AudalignMemoryBudgetError,
    AudalignOutputOverflowError,
    AudalignProviderMismatchError,
    AudalignProviderMissingError,
    AudalignTimeBudgetError,
    run_audalign_correlation,
    run_audalign_recognize,
    validate_audalign_selection,
)
from roughcut.adapters.audalign.ffmpeg_audio import (
    ANALYSIS_SAMPLE_RATE_HZ,
    ANALYSIS_SAMPLE_WIDTH_BYTES,
    FFmpegAlignmentBudgetError,
    FFmpegAlignmentError,
    FFmpegAlignmentMemoryBudgetError,
    FFmpegAlignmentTimeBudgetError,
    decode_alignment_audio,
    extract_wav_window,
)
from roughcut.adapters.audalign.worker import AUDALIGN_WORKER_MAX_OUTPUT_BYTES
from roughcut.adapters.audio_offset_finder import (
    BbcAdapterError,
    BbcMemoryError,
    BbcNoOffsetError,
    BbcTimeoutError,
    bbc_failure_code,
    run_bbc_offset_finder,
    validate_bbc_selection,
)
from roughcut.adapters.child_budget import (
    ChildBudget,
    ChildProcessBudgetError,
    ChildProcessMemoryBudgetError,
    ChildProcessTimeBudgetError,
    run_bounded_child,
)
from roughcut.adapters.ffmpeg_environment import (
    FFmpegRuntimeDriftError,
    verify_runtime_pair,
)
from roughcut.adapters.media_operation_store import (
    MediaOperationStore,
)
from roughcut.adapters.project_store import ProjectStore
from roughcut.adapters.runtime_binding import (
    RuntimeAlignmentPython,
)
from roughcut.application.alignment_profile import (
    CORRELATION_PROBE_PERCENTS,
    CORRELATION_RECALL_EXCERPT_LENGTH_TICKS,
    PROFILE2_MAX_CANDIDATE_GROUPS_PER_CHANNEL,
    PROFILE2_MAX_RAW_CANDIDATES_PER_PROBE,
    PROFILE2_MAX_RECALL_PROBES_PER_CHANNEL,
    PROFILE2_MAX_SELECTED_CANDIDATES_PER_PROBE,
    PROFILE2_RECALL_EXCERPT_LENGTH_TICKS,
    AlignmentProfileError,
    CandidateOffset,
    FixedOffsetVerifier,
    _recheck_workspace_after_child,
    correlation_admission,
    correlation_probe_starts,
    frozen_profile,
    group_candidates,
    group_profile2_hypotheses,
    profile2_call_plan,
    profile2_probe_starts,
    source_relation_b,
)
from roughcut.application.bbc_alignment import analyze_bbc_pair, empty_bbc_evidence
from roughcut.application.media_operations import _load_persistent_runtime
from roughcut.application.sources import fingerprint_file
from roughcut.application.waveform_alignment import (
    WAVEFORM_PROFILE_CANONICAL,
    WaveformDecodeError,
    WaveformEnvelope,
    analyze_waveform_pair,
    read_source_window,
)
from roughcut.domain.alignment import (
    ALIGNMENT_CANDIDATE_GROUP_DIAMETER_TICKS,
    ALIGNMENT_MAXIMUM_LOCAL_ERROR_TICKS,
    ALIGNMENT_MINIMUM_VERIFIED_OVERLAP_TICKS,
    ALIGNMENT_TICKS_PER_SECOND,
    ALIGNMENT_VERIFICATION_WINDOW_COUNT,
    AUDALIGN_CORRELATION_ALGORITHM_NAME,
    AUDALIGN_CORRELATION_ALGORITHM_VERSION,
    AUDALIGN_CORRELATION_MAX_RAW_CANDIDATES_PER_PROBE,
    AUDALIGN_CORRELATION_PROBE_TICKS,
    AUDALIGN_CORRELATION_PROFILE_NAME,
    AUDALIGN_CORRELATION_PROFILE_VERSION,
    AUDALIGN_CORRELATION_RECOGNIZER,
    AUDALIGN_CORRELATION_UPSTREAM_COMMIT,
    AUDALIGN_CORRELATION_WRITER_PROFILE,
    AUDALIGN_VERSION,
    BBC_ALGORITHM_NAME,
    BBC_ALGORITHM_UPSTREAM,
    BBC_ALGORITHM_VERSION,
    BBC_PROFILE_NAME,
    BBC_PROFILE_VERSION,
    BBC_WRITER_PROFILE,
    WAVEFORM_PROFILE_NAME,
    WAVEFORM_PROFILE_VERSION,
    AlignmentCamera,
    AlignmentCameraGroup,
    AlignmentError,
    AlignmentInterval,
    AlignmentPerCameraError,
    AlignmentRequestAuxiliaryGroup,
    AlignmentSourceBasis,
    AlignmentSourceFingerprint,
    AlignmentSourcePair,
    AlignmentSummary,
    MulticamAlignmentArtifact,
    alignment_request_projection,
    hash_alignment_request,
    parse_alignment_request_groups,
    seconds_to_ticks,
)
from roughcut.domain.media_operation import (
    RUNNING_PHASES,
    AlignmentOperationResult,
    MediaOperationError,
    MediaOperationFailure,
    MediaOperationRecord,
    ProjectOperationScope,
    validate_media_operation_id,
)
from roughcut.domain.project import Project, SourceAsset
from roughcut.domain.render import ToolResolution
from roughcut.domain.time import TICKS_PER_SECOND
from roughcut.domain.workflow import canonical_sha256_v1

ALIGNMENT_WRITE_PROFILE_VERSION = 2

ALIGNMENT_TERMINAL_CODES = frozenset(
    {
        "alignment_integrity_error",
        "alignment_input_stale",
        "alignment_runtime_unavailable",
        "alignment_main_probe_failed",
        "alignment_disk_budget_exceeded",
        "alignment_memory_budget_exceeded",
        "alignment_time_budget_exceeded",
        "alignment_main_decode_failed",
        "alignment_main_index_failed",
        "alignment_basis_changed_during_run",
        "alignment_runtime_changed_during_run",
        "alignment_store_integrity_error",
        "alignment_publish_conflict",
        "alignment_publish_failed",
        "alignment_interrupted",
    }
)
ALIGNMENT_PREFLIGHT_CODES = frozenset(
    {
        "operation_input_conflict",
        "alignment_input_stale",
        "alignment_runtime_unavailable",
        "alignment_main_probe_failed",
        "alignment_disk_budget_exceeded",
        "alignment_memory_budget_exceeded",
        "alignment_time_budget_exceeded",
    }
)
ALIGNMENT_DISK_CEILING_BYTES = 2_147_483_648
ALIGNMENT_MEMORY_CEILING_BYTES = 4_294_967_296
ALIGNMENT_TIME_CEILING_SECONDS = 7_200
ANALYSIS_WORKSPACE_BYTES_PER_SECOND = 44100 * 2
ANALYSIS_WORKSPACE_OVERHEAD_BYTES = 32 * 1024 * 1024
WAV_HEADER_BYTES = 44


@dataclass(frozen=True)
class AlignmentOutcome:
    record: MediaOperationRecord
    artifact: MulticamAlignmentArtifact | None
    readback: bool


@dataclass(frozen=True)
class _AlignmentPairGroup:
    camera: AlignmentCameraGroup
    pairs: tuple[AlignmentSourcePair, ...]


def _build_pair_groups(
    main_group: AlignmentCameraGroup,
    request_groups: tuple[AlignmentRequestAuxiliaryGroup, ...],
) -> tuple[AlignmentCameraGroup, tuple[_AlignmentPairGroup, ...]]:
    """Resolve optional exact pairs without inspecting any media."""
    all_pairs: list[AlignmentSourcePair] = []
    pair_groups: list[_AlignmentPairGroup] = []
    for request_group in request_groups:
        pairs: tuple[AlignmentSourcePair, ...]
        if request_group.source_pairs is None:
            if len(main_group.ordered_source_ids) != 1 or len(
                request_group.camera.ordered_source_ids
            ) != 1:
                raise _preflight_error(
                    "alignment_input_stale",
                    "explicit source_pairs are required for a multi-source synchronization group",
                )
            pairs = (
                AlignmentSourcePair(
                    main_source_id=main_group.ordered_source_ids[0],
                    auxiliary_source_id=request_group.camera.ordered_source_ids[0],
                ),
            )
        else:
            assert request_group.source_pairs is not None
            pairs = request_group.source_pairs
        selected_auxiliary_ids = tuple(
            source_id
            for source_id in request_group.camera.ordered_source_ids
            if any(pair.auxiliary_source_id == source_id for pair in pairs)
        )
        if not selected_auxiliary_ids:
            raise _preflight_error(
                "alignment_input_stale",
                "source_pairs did not select an auxiliary Source",
            )
        pair_groups.append(
            _AlignmentPairGroup(
                camera=AlignmentCameraGroup(
                    camera_id=request_group.camera.camera_id,
                    ordered_source_ids=selected_auxiliary_ids,
                ),
                pairs=tuple(pairs),
            )
        )
        all_pairs.extend(pairs)
    selected_main_ids = tuple(
        source_id
        for source_id in main_group.ordered_source_ids
        if any(pair.main_source_id == source_id for pair in all_pairs)
    )
    if not selected_main_ids:
        raise _preflight_error(
            "alignment_input_stale",
            "source_pairs did not select a main Source",
        )
    return (
        AlignmentCameraGroup(camera_id="main", ordered_source_ids=selected_main_ids),
        tuple(pair_groups),
    )


def _pair_source_ids(
    selected_main_group: AlignmentCameraGroup,
    pair_groups: tuple[_AlignmentPairGroup, ...],
) -> set[str]:
    """Return only Source IDs used by exact pairs; group declarations remain
    validated separately but their unpaired Sources are never fingerprinted.
    """
    selected = set(selected_main_group.ordered_source_ids)
    for group in pair_groups:
        selected.update(group.camera.ordered_source_ids)
    return selected


def run_align_multicam(
    project_path: Path,
    *,
    operation_id: str,
    alignment_id: str,
    expected_revision: int,
    main_camera: dict[str, object],
    auxiliary_cameras: list[dict[str, object]],
    main_audio_stable: bool,
    max_temporary_disk_bytes: int,
    max_analysis_memory_bytes: int,
    max_runtime_seconds: int,
) -> AlignmentOutcome:
    """Run or read back one exact authorized multicam alignment."""
    validate_media_operation_id(operation_id)
    root, project, store = _project_context(project_path)
    scope_dict: dict[str, object] = {
        key: value for key, value in store.scope.to_dict().items()
    }

    def projection(
        *, writer_profile: dict[str, object] | None
    ) -> dict[str, object]:
        return alignment_request_projection(
            scope=scope_dict,
            operation_id=operation_id,
            alignment_id=alignment_id,
            expected_revision=expected_revision,
            main_camera=main_camera,
            auxiliary_cameras=auxiliary_cameras,
            main_audio_stable=main_audio_stable,
            max_temporary_disk_bytes=max_temporary_disk_bytes,
            max_analysis_memory_bytes=max_analysis_memory_bytes,
            max_runtime_seconds=max_runtime_seconds,
            writer_profile=writer_profile,
        )

    # New writes bind the Audalign correlation/fixed-offset identity.
    # All superseded writer hashes remain accepted only for exact-ID readback.
    request_projection = projection(writer_profile=AUDALIGN_CORRELATION_WRITER_PROFILE)
    request_hash = hash_alignment_request(request_projection)
    legacy_request_hash = hash_alignment_request(projection(writer_profile=None))
    waveform_request_hash = hash_alignment_request(
        projection(writer_profile=WAVEFORM_PROFILE_CANONICAL)
    )
    bbc_request_hash = hash_alignment_request(
        projection(writer_profile=BBC_WRITER_PROFILE)
    )
    acceptable_request_hashes = frozenset(
        {request_hash, bbc_request_hash, waveform_request_hash, legacy_request_hash}
    )
    existing = store.read(operation_id, allow_writer_temp=True)
    if existing is not None:
        _require_same_request(existing, acceptable_request_hashes)
        return AlignmentOutcome(existing, None, True)

    if project.revision != expected_revision:
        raise _preflight_error(
            "alignment_input_stale", "expected revision does not match the Project"
        )
    if not main_audio_stable:
        # fail closed before any worker/record/workspace: an unstable main
        # audio source cannot support a fixed-offset relation
        raise _preflight_error(
            "alignment_input_stale",
            "main audio stability is required for fixed-offset alignment",
        )
    # budget hard gates: positive integers within the frozen ceilings
    _validate_budget(
        "max_temporary_disk_bytes",
        max_temporary_disk_bytes,
        ALIGNMENT_DISK_CEILING_BYTES,
    )
    _validate_budget(
        "max_analysis_memory_bytes",
        max_analysis_memory_bytes,
        ALIGNMENT_MEMORY_CEILING_BYTES,
    )
    _validate_budget(
        "max_runtime_seconds",
        max_runtime_seconds,
        ALIGNMENT_TIME_CEILING_SECONDS,
    )
    # Parse groups before deciding writer profile (writer does not affect pair semantics)
    main_group, request_groups = parse_alignment_request_groups(
        main_camera,
        auxiliary_cameras,
    )
    selected_main_group, pair_groups = _build_pair_groups(
        main_group,
        request_groups,
    )
    declared_auxiliary_groups = tuple(group.camera for group in request_groups)
    selected_source_ids = _pair_source_ids(selected_main_group, pair_groups)
    all_sources = _resolve_groups(
        root,
        project,
        main_group,
        declared_auxiliary_groups,
        selected_source_ids=selected_source_ids,
    )
    runtime = _load_persistent_runtime()
    # Production writer selection is determined by the normalized managed runtime.
    # Only Audalign correlation is allowed for new writes; BBC is only for
    # historical exact-ID readback which already returned before this check.
    try:
        selection = runtime.binding.alignment_python
        validated = validate_audalign_selection(selection)
        alignment_python = Path(validated.interpreter)
    except (AudalignProviderMismatchError, AudalignProviderMissingError) as error:
        raise _preflight_error(
            "alignment_runtime_unavailable",
            "persistent Audalign alignment runtime is unavailable",
        ) from error
    ffmpeg = ToolResolution(
        runtime.ffmpeg.command, runtime.ffmpeg.command, runtime.ffmpeg.version
    )
    # one frozen identity snapshot per requested source, created exactly once
    # in preflight; the same object feeds the input hash projection, the
    # worker startup check, and the publish-time revalidation
    frozen_identity = _snapshot_requested_identities(
        root, project, all_sources, runtime
    )
    workspace_estimate = _estimate_correlation_workspace_bytes(
        tuple(
            all_sources[source_id].probe.duration_ticks
            for source_id in selected_main_group.ordered_source_ids
        ),
        tuple(
            all_sources[pair.auxiliary_source_id].probe.duration_ticks
            for group in pair_groups
            for pair in group.pairs
        ),
    )
    if workspace_estimate > max_temporary_disk_bytes:
        raise _preflight_error(
            "alignment_disk_budget_exceeded",
            "estimated analysis workspace exceeds the disk budget",
        )
    input_projection = _alignment_input_projection(
        root,
        project,
        request_projection,
        all_sources,
        runtime,
        workspace_estimate,
        frozen_identity,
        profile=AUDALIGN_CORRELATION_WRITER_PROFILE,
    )
    input_hash = canonical_sha256_v1(input_projection)
    deadline = _Deadline(max_runtime_seconds)

    with store.writer(operation_id, create=True) as acquired:
        if not acquired:
            concurrent = _read_live_record(store, operation_id, acceptable_request_hashes)
            return AlignmentOutcome(concurrent, None, True)
        raced = store.read(operation_id)
        if raced is not None:
            _require_same_request(raced, acceptable_request_hashes)
            return AlignmentOutcome(raced, None, True)
        active = _new_pending_record(
            operation_id,
            store.scope,
            request_hash,
            input_hash,
        )
        store.write_locked(active)
        active = _running_record(active, "alignment_preparing")
        store.write_locked(active)

        def update_phase(phase: str) -> None:
            nonlocal active
            active = _phase_record(active, phase)
            store.write_locked(active)

        workspace: Path | None = None
        try:
            workspace = Path(
                tempfile.mkdtemp(prefix="roughcut-alignment-")
            )
            workspace_budget = _WorkspaceBudget(
                workspace,
                max_disk_bytes=max_temporary_disk_bytes,
            )
            child_budget = ChildBudget(deadline, max_analysis_memory_bytes)
            child_budget = replace(
                child_budget, apply_tmpdir=workspace_budget.apply_tmpdir
            )
            _validate_alignment_runtime(runtime, child_budget, workspace_budget)
            artifact = _execute_audalign_correlation_alignment(
                root,
                project,
                store.scope,
                all_sources,
                selected_main_group,
                tuple(group.camera for group in pair_groups),
                alignment_python,
                ffmpeg,
                workspace,
                request_hash,
                input_hash,
                request_projection,
                runtime,
                deadline,
                max_analysis_memory_bytes,
                child_budget,
                workspace_budget,
                frozen_identity,
                update_phase,
                operation_id,
                alignment_id,
                pair_groups=pair_groups,
            )
            succeeded = store.write_locked(
                _terminal_record(
                    active,
                    status="succeeded",
                    result=AlignmentOperationResult(
                        alignment_id=alignment_id,
                        schema_version=1,
                        content_hash=artifact.content_hash,
                    ),
                )
            )
            return AlignmentOutcome(succeeded, artifact, False)
        except (KeyboardInterrupt, SystemExit):
            _record_alignment_interruption(store, active)
            raise
        except AlignmentError as error:
            _record_alignment_failure(store, active, error.code)
            raise
        except MediaOperationError:
            raise
        except Exception as error:
            code = _classify_worker_error(error, active.phase_message_code)
            _record_alignment_failure(store, active, code)
            raise MediaOperationError(code, str(error)) from error
        finally:
            if workspace is not None:
                shutil.rmtree(workspace, ignore_errors=True)


def persist_alignment_preflight_failure(
    project_path: Path,
    *,
    operation_id: str,
    request_hash: str,
    input_hash: str,
    code: str,
    status: Literal["failed", "interrupted"] = "failed",
) -> MediaOperationRecord:
    """Durably close a preflight failure that happened before the runner record."""
    validate_media_operation_id(operation_id)
    stable_code = (
        "alignment_interrupted"
        if status == "interrupted"
        else (
            code
            if code in ALIGNMENT_TERMINAL_CODES
            and code != "alignment_interrupted"
            else "alignment_store_integrity_error"
        )
    )
    _root, _project, store = _project_context(project_path)
    with store.writer(operation_id, create=True) as acquired:
        if not acquired:
            current = store.read(operation_id, allow_writer_temp=True)
            if current is None:
                raise MediaOperationError(
                    "operation_integrity_error",
                    "Roughcut alignment preflight could not read its concurrent record",
                )
            _require_same_request(current, frozenset({request_hash}))
            return current
        existing = store.read(operation_id)
        if existing is not None:
            _require_same_request(existing, frozenset({request_hash}))
            return existing
        active = _new_pending_record(
            operation_id,
            store.scope,
            request_hash,
            input_hash,
        )
        store.write_locked(active)
        active = _running_record(active, "alignment_preparing")
        store.write_locked(active)
        return store.write_locked(
            _terminal_record(
                active,
                status=status,
                failure=MediaOperationFailure(
                    code=stable_code,
                    responsibility="roughcut_core",
                    action=(
                        "recover_abandoned_media_operation"
                        if status == "interrupted"
                        else "validate_alignment_basis"
                    ),
                    message_code=(
                        "alignment_interrupted"
                        if status == "interrupted"
                        else "alignment_failed"
                    ),
                ),
            )
        )


def _waveform_empty_evidence(code: str = "no_candidate") -> dict[str, object]:
    return {
        "code": code,
        "coarse_peak": None,
        "coarse_runner_up": None,
        "coarse_peak_runner_up_separation": None,
        "refined_offset_ticks": None,
        "refined_correlation": None,
        "verification_windows": [],
        "verification_window_count": 0,
        "verification_profile": {
            "name": WAVEFORM_PROFILE_NAME,
            "version": WAVEFORM_PROFILE_VERSION,
        },
        "max_local_offset_error_ticks": None,
        "conflicting_b_ticks": [],
    }


def _waveform_interval(
    *,
    interval_id: str,
    camera_id: str,
    classification: str,
    main_source_id: str,
    start: int,
    end: int,
    evidence: dict[str, object],
    auxiliary: dict[str, object] | None = None,
) -> AlignmentInterval:
    return AlignmentInterval(
        interval_id=interval_id,
        auxiliary_camera_id=camera_id,
        classification=cast(Any, classification),
        main={
            "source_id": main_source_id,
            "start_ticks": start,
            "end_ticks": end,
        },
        auxiliary=auxiliary,
        evidence=dict(evidence),
    )


def _waveform_conflict_evidence(
    evidences: list[dict[str, object]],
    b_ticks: list[int],
) -> dict[str, object]:
    base = dict(evidences[0])
    base["code"] = "fixed_offset_conflict"
    base["conflicting_b_ticks"] = list(dict.fromkeys(b_ticks))
    base["max_local_offset_error_ticks"] = max(
        int(cast(int, evidence["max_local_offset_error_ticks"]))
        for evidence in evidences
        if evidence["max_local_offset_error_ticks"] is not None
    )
    return base


def _bbc_conflict_evidence(
    evidences: list[dict[str, object]], b_ticks: list[int]
) -> dict[str, object]:
    base = dict(evidences[0])
    base["code"] = "fixed_offset_conflict"
    base["conflicting_b_ticks"] = list(dict.fromkeys(b_ticks))
    return base


def _refined_evidence_b(evidence: dict[str, object]) -> int | None:
    # Correlation uses representative_b_ticks; BBC uses refined_b_ticks; waveform uses refined_offset_ticks
    for key in ("representative_b_ticks", "refined_b_ticks", "refined_offset_ticks"):
        value = evidence.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _correlation_conflict_evidence(
    evidences: list[dict[str, object]], b_ticks: list[int]
) -> dict[str, object]:
    base = dict(evidences[0])
    base["code"] = "fixed_offset_conflict"
    base["conflicting_b_ticks"] = list(dict.fromkeys(b_ticks))
    # keep original support/spread/representative but conflict carries divergent B
    return base


def _waveform_partition_camera_timeline(
    *,
    main_duration: int,
    camera_id: str,
    main_source_id: str,
    mapped_segments: list[tuple[int, int, str, int, int, dict[str, object]]],
    unverifiable_segments: list[tuple[int, int, str, dict[str, object]]],
    intervals: list[AlignmentInterval],
    empty_evidence: Callable[[], dict[str, object]] = _waveform_empty_evidence,
) -> tuple[int, int, int, int]:
    """Partition one main Source without turning unpaired sources into missing."""
    def evidence_family(evidence: dict[str, object]) -> str:
        profile = evidence.get("verification_profile")
        if not isinstance(profile, dict):
            raise AlignmentError(
                "alignment_integrity_error",
                "alignment evidence has no verification profile",
            )
        name = profile.get("name")
        if name == WAVEFORM_PROFILE_NAME:
            return "waveform"
        if name == BBC_PROFILE_NAME:
            return "bbc"
        if name == AUDALIGN_CORRELATION_PROFILE_NAME:
            return "correlation"
        raise AlignmentError(
            "alignment_integrity_error",
            "alignment evidence belongs to an unknown profile family",
        )

    supplied_evidence = [
        item[5] for item in mapped_segments
    ] + [item[3] for item in unverifiable_segments]
    families = {evidence_family(item) for item in supplied_evidence}
    if len(families) > 1:
        raise AlignmentError(
            "alignment_integrity_error",
            "alignment evidence mixes profile families",
        )
    if families and evidence_family(empty_evidence()) not in families:
        raise AlignmentError(
            "alignment_integrity_error",
            "alignment empty evidence belongs to a different profile family",
        )
    family = next(iter(families), evidence_family(empty_evidence()))
    ordered = sorted(mapped_segments, key=lambda item: (item[0], item[1], item[2]))
    events: list[tuple[int, int, int]] = []
    for index, (start, end, _source, _aux_start, _aux_end, _evidence) in enumerate(
        ordered
    ):
        events.append((start, 1, index))
        events.append((end, -1, index))
    events.sort(key=lambda item: (item[0], -item[1], item[2]))
    position = 0
    active: set[int] = set()
    mapped_ticks = 0
    missing_ticks = 0
    uncertain_ticks = 0
    conflict_ticks = 0
    index = 0

    def emit_gap(start: int, end: int, gap_index: int) -> None:
        nonlocal missing_ticks, uncertain_ticks, conflict_ticks
        covering = [
            item
            for item in unverifiable_segments
            if item[1] > start and item[0] < end
        ]
        if not covering:
            intervals.append(
                _waveform_interval(
                    interval_id=f"ali_{camera_id}_{main_source_id}_miss_{gap_index}",
                    camera_id=camera_id,
                    classification="missing",
                    main_source_id=main_source_id,
                    start=start,
                    end=end,
                    evidence=empty_evidence(),
                )
            )
            missing_ticks += end - start
            return
        all_conflict = all(item[2] == "conflict" for item in covering)
        if all_conflict:
            evidence = next(item[3] for item in covering)
            intervals.append(
                _waveform_interval(
                    interval_id=f"ali_{camera_id}_{main_source_id}_conf_{gap_index}",
                    camera_id=camera_id,
                    classification="conflict",
                    main_source_id=main_source_id,
                    start=start,
                    end=end,
                    evidence=evidence,
                )
            )
            conflict_ticks += end - start
        else:
            evidence = next(
                (
                    item[3]
                    for item in covering
                    if item[3]["code"] not in {"no_candidate", "no_offset"}
                ),
                empty_evidence(),
            )
            intervals.append(
                _waveform_interval(
                    interval_id=f"ali_{camera_id}_{main_source_id}_unc_{gap_index}",
                    camera_id=camera_id,
                    classification="uncertain",
                    main_source_id=main_source_id,
                    start=start,
                    end=end,
                    evidence=evidence,
                )
            )
            uncertain_ticks += end - start

    def emit_active_span(start: int, end: int, span_index: int) -> None:
        """Emit one span covered by one or more verified owners.

        With several simultaneous owners whose refined placements agree
        within the frozen local-error gate, exactly one deterministic owner
        (ascending auxiliary source ID, then segment start) represents this
        overlap span alone; single-owner regions always stay attributed to
        their real source. Only divergence beyond the gate becomes conflict.
        """
        nonlocal mapped_ticks, conflict_ticks
        owners = [ordered[item] for item in sorted(active)]
        offsets = [
            int(cast(int, _refined_evidence_b(owner[5])))
            for owner in owners
            if _refined_evidence_b(owner[5]) is not None
        ]
        agreeing = (
            len(offsets) == len(owners)
            and max(offsets) - min(offsets) <= ALIGNMENT_MAXIMUM_LOCAL_ERROR_TICKS
        )
        if agreeing:
            chosen = min(owners, key=lambda owner: (owner[2], owner[0]))
            seg_start, _seg_end, source_id, aux_start, _aux_end, evidence = chosen
            intervals.append(
                _waveform_interval(
                    interval_id=f"ali_{camera_id}_{main_source_id}_map_{span_index}",
                    camera_id=camera_id,
                    classification="mapped",
                    main_source_id=main_source_id,
                    start=start,
                    end=end,
                    auxiliary={
                        "source_id": source_id,
                        "start_ticks": aux_start + start - seg_start,
                        "end_ticks": aux_start + end - seg_start,
                    },
                    evidence=evidence,
                )
            )
            mapped_ticks += end - start
            return
        evidences = [owner[5] for owner in owners]
        conflict_offsets = [
            int(cast(int, _refined_evidence_b(evidence)))
            for evidence in evidences
            if _refined_evidence_b(evidence) is not None
        ]
        if family == "bbc":
            conflict_evidence = _bbc_conflict_evidence(evidences, conflict_offsets)
        elif family == "correlation":
            conflict_evidence = _correlation_conflict_evidence(evidences, conflict_offsets)
        else:
            conflict_evidence = _waveform_conflict_evidence(evidences, conflict_offsets)
        intervals.append(
            _waveform_interval(
                interval_id=f"ali_{camera_id}_{main_source_id}_conf_{span_index}",
                camera_id=camera_id,
                classification="conflict",
                main_source_id=main_source_id,
                start=start,
                end=end,
                evidence=conflict_evidence,
            )
        )
        conflict_ticks += end - start

    for point, delta, owner_index in events:
        if point > position:
            if not active:
                emit_gap(position, point, index)
            elif len(active) == 1:
                owner = ordered[next(iter(active))]
                start, _end, source_id, aux_start, _aux_end, evidence = owner
                intervals.append(
                    _waveform_interval(
                        interval_id=f"ali_{camera_id}_{main_source_id}_map_{index}",
                        camera_id=camera_id,
                        classification="mapped",
                        main_source_id=main_source_id,
                        start=position,
                        end=point,
                        auxiliary={
                            "source_id": source_id,
                            "start_ticks": aux_start + position - start,
                            "end_ticks": aux_start + point - start,
                        },
                        evidence=evidence,
                    )
                )
                mapped_ticks += point - position
            else:
                emit_active_span(position, point, index)
            position = point
            index += 1
        if delta == 1:
            active.add(owner_index)
        else:
            active.discard(owner_index)
    if position < main_duration:
        if not active:
            emit_gap(position, main_duration, index)
        elif len(active) == 1:
            start, _end, source_id, aux_start, _aux_end, evidence = ordered[
                next(iter(active))
            ]
            intervals.append(
                _waveform_interval(
                    interval_id=f"ali_{camera_id}_{main_source_id}_map_{index}",
                    camera_id=camera_id,
                    classification="mapped",
                    main_source_id=main_source_id,
                    start=position,
                    end=main_duration,
                    auxiliary={
                        "source_id": source_id,
                        "start_ticks": aux_start + position - start,
                        "end_ticks": aux_start + main_duration - start,
                    },
                    evidence=evidence,
                )
            )
            mapped_ticks += main_duration - position
        else:
            emit_active_span(position, main_duration, index)
    return mapped_ticks, missing_ticks, uncertain_ticks, conflict_ticks


def _make_pair_window_reader(
    *,
    main_path: Path,
    main_source: SourceAsset,
    main_source_id: str,
    auxiliary_path: Path,
    auxiliary_source: SourceAsset,
    auxiliary_source_id: str,
    frozen_identity: dict[str, dict[str, object]],
    child_budget: ChildBudget,
    workspace_budget: _WorkspaceBudget,
    ffmpeg_command: str,
) -> Callable[[Path, int, int, str], list[float]]:
    """Bind one pair's frozen-identity recheck into every short-window read."""

    def read_window(
        path: Path,
        start_ticks: int,
        end_ticks: int,
        channel: str,
    ) -> list[float]:
        # Both pair ends are re-verified against the frozen identity
        # immediately before every bounded read, so a Source replaced after
        # its envelope can never contribute window data mixed from two file
        # versions.
        _assert_source_matches_frozen(
            main_path, main_source, frozen_identity, main_source_id
        )
        _assert_source_matches_frozen(
            auxiliary_path, auxiliary_source, frozen_identity, auxiliary_source_id
        )
        try:
            samples = read_source_window(
                path,
                start_ticks=start_ticks,
                end_ticks=end_ticks,
                channel=channel,
                ffmpeg_command=ffmpeg_command,
                budget=child_budget,
            )
        except (
            ChildProcessTimeBudgetError,
            ChildProcessMemoryBudgetError,
        ) as error:
            _recheck_workspace_after_child(workspace_budget, error)
            raise
        except ChildProcessBudgetError as error:
            _recheck_workspace_after_child(workspace_budget, error)
            raise WaveformDecodeError(
                "waveform short-window child failed"
            ) from error
        workspace_budget.recheck()
        return samples

    return read_window


def _process_waveform_camera(
    *,
    root: Path,
    sources: dict[str, SourceAsset],
    main_group: AlignmentCameraGroup,
    pair_group: _AlignmentPairGroup,
    main_envelopes: dict[str, WaveformEnvelope],
    auxiliary_envelopes: dict[str, WaveformEnvelope],
    failed_auxiliary_sources: dict[str, str],
    deadline: _Deadline,
    child_budget: ChildBudget,
    workspace_budget: _WorkspaceBudget,
    frozen_identity: dict[str, dict[str, object]],
    ffmpeg: ToolResolution,
) -> tuple[AlignmentCamera, list[AlignmentInterval]]:
    intervals: list[AlignmentInterval] = []
    errors: list[AlignmentPerCameraError] = []
    mapped_total = 0
    missing_total = 0
    uncertain_total = 0
    conflict_total = 0
    reported_errors: set[str] = set()
    pairs_by_main: dict[str, list[AlignmentSourcePair]] = {}
    for pair in pair_group.pairs:
        pairs_by_main.setdefault(pair.main_source_id, []).append(pair)
    for main_source_id in main_group.ordered_source_ids:
        main_duration = sources[main_source_id].probe.duration_ticks
        mapped_segments: list[tuple[int, int, str, int, int, dict[str, object]]] = []
        unverifiable_segments: list[tuple[int, int, str, dict[str, object]]] = []
        for pair in pairs_by_main.get(main_source_id, []):
            aux_source_id = pair.auxiliary_source_id
            aux_duration = sources[aux_source_id].probe.duration_ticks
            if aux_source_id in failed_auxiliary_sources:
                if aux_source_id not in reported_errors:
                    errors.append(
                        AlignmentPerCameraError(
                            code=failed_auxiliary_sources[aux_source_id],
                            source_id=aux_source_id,
                        )
                    )
                    reported_errors.add(aux_source_id)
                unverifiable_segments.append(
                    (0, main_duration, "uncertain", _waveform_empty_evidence("failed"))
                )
                continue
            main_source = sources[main_source_id]
            aux_source = sources[aux_source_id]
            main_path = _resolve_source_path(root, main_source)
            aux_path = _resolve_source_path(root, aux_source)
            read_window = _make_pair_window_reader(
                main_path=main_path,
                main_source=main_source,
                main_source_id=main_source_id,
                auxiliary_path=aux_path,
                auxiliary_source=aux_source,
                auxiliary_source_id=aux_source_id,
                frozen_identity=frozen_identity,
                child_budget=child_budget,
                workspace_budget=workspace_budget,
                ffmpeg_command=ffmpeg.command,
            )

            result = analyze_waveform_pair(
                main_path=main_path,
                auxiliary_path=aux_path,
                main_envelope=main_envelopes[main_source_id],
                auxiliary_envelope=auxiliary_envelopes[aux_source_id],
                main_duration_ticks=main_duration,
                auxiliary_duration_ticks=aux_duration,
                window_reader=read_window,
            )
            classification = str(result["classifications"])
            evidence = cast(dict[str, object], result["evidence"])
            if classification == "mapped":
                b_ticks = int(cast(int, result["b_ticks"]))
                mapped_start = max(0, b_ticks)
                mapped_end = min(main_duration, aux_duration + b_ticks)
                if mapped_end > mapped_start:
                    mapped_segments.append(
                        (
                            mapped_start,
                            mapped_end,
                            aux_source_id,
                            mapped_start - b_ticks,
                            mapped_end - b_ticks,
                            evidence,
                        )
                    )
            elif classification == "conflict":
                unverifiable_segments.append(
                    (0, main_duration, "conflict", evidence)
                )
            else:
                unverifiable_segments.append(
                    (0, main_duration, "uncertain", evidence)
                )
        if not pairs_by_main.get(main_source_id):
            # This is a closed placeholder for a main Source selected by a
            # different auxiliary camera, never a probe/decode or a guessed
            # missing interval.
            unverifiable_segments.append(
                (0, main_duration, "uncertain", _waveform_empty_evidence())
            )
        mapped, missing, uncertain, conflict = _waveform_partition_camera_timeline(
            main_duration=main_duration,
            camera_id=pair_group.camera.camera_id,
            main_source_id=main_source_id,
            mapped_segments=mapped_segments,
            unverifiable_segments=unverifiable_segments,
            intervals=intervals,
        )
        mapped_total += mapped
        missing_total += missing
        uncertain_total += uncertain
        conflict_total += conflict
    total_main_ticks = sum(
        sources[source_id].probe.duration_ticks
        for source_id in main_group.ordered_source_ids
    )
    if mapped_total == total_main_ticks and not errors:
        status = "complete"
    elif mapped_total > 0:
        status = "partial"
    elif errors:
        status = "failed"
    else:
        status = "omitted"
    return (
        AlignmentCamera(
            camera_id=pair_group.camera.camera_id,
            ordered_source_ids=pair_group.camera.ordered_source_ids,
            status=cast(Any, status),
            mapped_ticks=mapped_total,
            missing_ticks=missing_total,
            uncertain_ticks=uncertain_total,
            conflict_ticks=conflict_total,
            errors=tuple(errors),
        ),
        intervals,
    )


def _process_bbc_camera(
    *,
    root: Path,
    sources: dict[str, SourceAsset],
    main_group: AlignmentCameraGroup,
    pair_group: _AlignmentPairGroup,
    alignment_selection: RuntimeAlignmentPython,
    workspace: Path,
    child_budget: ChildBudget,
    workspace_budget: _WorkspaceBudget,
    frozen_identity: dict[str, dict[str, object]],
    ffmpeg: ToolResolution,
) -> tuple[AlignmentCamera, list[AlignmentInterval]]:
    intervals: list[AlignmentInterval] = []
    errors: list[AlignmentPerCameraError] = []
    totals = {"mapped": 0, "missing": 0, "uncertain": 0, "conflict": 0}
    pairs_by_main: dict[str, list[AlignmentSourcePair]] = {}
    for pair in pair_group.pairs:
        pairs_by_main.setdefault(pair.main_source_id, []).append(pair)

    for main_source_id in main_group.ordered_source_ids:
        main_source = sources[main_source_id]
        main_duration = main_source.probe.duration_ticks
        mapped_segments: list[tuple[int, int, str, int, int, dict[str, object]]] = []
        unverifiable_segments: list[tuple[int, int, str, dict[str, object]]] = []
        for pair in pairs_by_main.get(main_source_id, []):
            auxiliary_source_id = pair.auxiliary_source_id
            auxiliary_source = sources[auxiliary_source_id]
            main_path = _resolve_source_path(root, main_source)
            auxiliary_path = _resolve_source_path(root, auxiliary_source)
            _assert_source_matches_frozen(
                main_path, main_source, frozen_identity, main_source_id
            )
            _assert_source_matches_frozen(
                auxiliary_path,
                auxiliary_source,
                frozen_identity,
                auxiliary_source_id,
            )
            try:
                finder = run_bbc_offset_finder(
                    main_path,
                    auxiliary_path,
                    selection=alignment_selection,
                    budget=child_budget,
                    workspace_root=workspace,
                    ffmpeg_command=ffmpeg.command,
                )
            except BbcNoOffsetError as error:
                unverifiable_segments.append(
                    (
                        0,
                        main_duration,
                        "uncertain",
                        empty_bbc_evidence(bbc_failure_code(error)),
                    )
                )
                continue
            except (BbcTimeoutError, BbcMemoryError):
                raise
            except BbcAdapterError as error:
                errors.append(
                    AlignmentPerCameraError(
                        code="auxiliary_recognition_failed",
                        source_id=auxiliary_source_id,
                    )
                )
                unverifiable_segments.append(
                    (
                        0,
                        main_duration,
                        "uncertain",
                        empty_bbc_evidence(bbc_failure_code(error)),
                    )
                )
                continue
            workspace_budget.recheck()
            if finder.standard_score is None:
                errors.append(
                    AlignmentPerCameraError(
                        code="auxiliary_recognition_failed",
                        source_id=auxiliary_source_id,
                    )
                )
                unverifiable_segments.append(
                    (
                        0,
                        main_duration,
                        "uncertain",
                        empty_bbc_evidence("finder_result_invalid"),
                    )
                )
                continue
            read_window = _make_pair_window_reader(
                main_path=main_path,
                main_source=main_source,
                main_source_id=main_source_id,
                auxiliary_path=auxiliary_path,
                auxiliary_source=auxiliary_source,
                auxiliary_source_id=auxiliary_source_id,
                frozen_identity=frozen_identity,
                child_budget=child_budget,
                workspace_budget=workspace_budget,
                ffmpeg_command=ffmpeg.command,
            )
            initial_b_ticks = seconds_to_ticks(finder.native_offset_seconds)
            result = analyze_bbc_pair(
                main_path=main_path,
                auxiliary_path=auxiliary_path,
                main_duration_ticks=main_duration,
                auxiliary_duration_ticks=auxiliary_source.probe.duration_ticks,
                native_offset_seconds=finder.native_offset_seconds,
                initial_b_ticks=initial_b_ticks,
                standard_score=finder.standard_score,
                window_reader=read_window,
            )
            evidence = cast(dict[str, object], result["evidence"])
            if result["classifications"] == "mapped":
                b_ticks = cast(int, result["b_ticks"])
                mapped_start = max(0, b_ticks)
                mapped_end = min(
                    main_duration,
                    auxiliary_source.probe.duration_ticks + b_ticks,
                )
                mapped_segments.append(
                    (
                        mapped_start,
                        mapped_end,
                        auxiliary_source_id,
                        mapped_start - b_ticks,
                        mapped_end - b_ticks,
                        evidence,
                    )
                )
            else:
                if evidence["code"] == "decode_failed":
                    errors.append(
                        AlignmentPerCameraError(
                            code="auxiliary_verification_failed",
                            source_id=auxiliary_source_id,
                        )
                    )
                unverifiable_segments.append(
                    (0, main_duration, "uncertain", evidence)
                )
        if not pairs_by_main.get(main_source_id):
            unverifiable_segments.append(
                (0, main_duration, "uncertain", empty_bbc_evidence("no_offset"))
            )
        values = _waveform_partition_camera_timeline(
            main_duration=main_duration,
            camera_id=pair_group.camera.camera_id,
            main_source_id=main_source_id,
            mapped_segments=mapped_segments,
            unverifiable_segments=unverifiable_segments,
            intervals=intervals,
            empty_evidence=lambda: empty_bbc_evidence("no_offset"),
        )
        for key, value in zip(totals, values, strict=True):
            totals[key] += value
    total_main_ticks = sum(
        sources[source_id].probe.duration_ticks
        for source_id in main_group.ordered_source_ids
    )
    status = (
        "complete"
        if totals["mapped"] == total_main_ticks and not errors
        else "partial"
        if totals["mapped"] > 0
        else "failed"
        if errors
        else "omitted"
    )
    return (
        AlignmentCamera(
            camera_id=pair_group.camera.camera_id,
            ordered_source_ids=pair_group.camera.ordered_source_ids,
            status=cast(Any, status),
            mapped_ticks=totals["mapped"],
            missing_ticks=totals["missing"],
            uncertain_ticks=totals["uncertain"],
            conflict_ticks=totals["conflict"],
            errors=tuple(errors),
        ),
        intervals,
    )


def _execute_alignment(
    root: Path,
    project: Project,
    scope: ProjectOperationScope,
    sources: dict[str, SourceAsset],
    main_group: AlignmentCameraGroup,
    auxiliary_groups: tuple[AlignmentCameraGroup, ...],
    alignment_python: RuntimeAlignmentPython,
    ffmpeg: ToolResolution,
    workspace: Path,
    request_hash: str,
    input_hash: str,
    request_projection: dict[str, object],
    runtime: Any,
    deadline: _Deadline,
    memory_limit: int,
    child_budget: ChildBudget,
    workspace_budget: _WorkspaceBudget,
    frozen_identity: dict[str, dict[str, object]],
    update_phase: Callable[[str], None],
    operation_id: str,
    alignment_id: str,
    *,
    pair_groups: tuple[_AlignmentPairGroup, ...],
) -> MulticamAlignmentArtifact:
    """Historical BBC initial-B plus Roughcut refine/verify helper.

    ``run_align_multicam`` never dispatches here: the helper remains available
    only for historical compatibility tests and exact legacy readback support.
    """
    del scope, auxiliary_groups, request_projection, memory_limit, deadline
    try:
        validate_bbc_selection(alignment_python)
    except BbcAdapterError as error:
        raise AlignmentError(
            "alignment_runtime_unavailable",
            "historical BBC alignment selection is unavailable",
        ) from error
    camera_items: list[AlignmentCamera] = []
    intervals: list[AlignmentInterval] = []
    for pair_group in pair_groups:
        update_phase("alignment_processing_auxiliary")
        try:
            camera, camera_intervals = _process_bbc_camera(
                root=root,
                sources=sources,
                main_group=main_group,
                pair_group=pair_group,
                alignment_selection=alignment_python,
                workspace=workspace,
                child_budget=child_budget,
                workspace_budget=workspace_budget,
                frozen_identity=frozen_identity,
                ffmpeg=ffmpeg,
            )
        except (ChildProcessTimeBudgetError, BbcTimeoutError) as error:
            _recheck_workspace_after_child(workspace_budget, error)
            raise AlignmentError(
                "alignment_time_budget_exceeded",
                "BBC alignment exceeded the wall-time budget",
            ) from error
        except (ChildProcessMemoryBudgetError, BbcMemoryError) as error:
            _recheck_workspace_after_child(workspace_budget, error)
            raise AlignmentError(
                "alignment_memory_budget_exceeded",
                "BBC alignment exceeded the memory budget",
            ) from error
        camera_items.append(camera)
        intervals.extend(camera_intervals)

    update_phase("alignment_revalidating_basis")
    # The BBC writer pins its exact managed selection plus FFmpeg/FFprobe;
    # historical Audalign publication retains whole-binding semantics.
    try:
        validate_bbc_selection(runtime.binding.alignment_python)
    except BbcAdapterError as error:
        raise AlignmentError(
            "alignment_runtime_changed_during_run",
            "historical BBC alignment selection changed during alignment",
        ) from error
    _revalidate_basis(
        root,
        project,
        sources,
        runtime,
        frozen_identity,
        require_full_binding=False,
        require_alignment_selection=False,
    )
    source_basis: list[AlignmentSourceBasis] = []
    for source_id in main_group.ordered_source_ids:
        source = sources[source_id]
        source_basis.append(
            AlignmentSourceBasis(
                camera_id="main",
                source_id=source_id,
                fingerprint=_alignment_fingerprint(source.fingerprint),
                duration_ticks=source.probe.duration_ticks,
            )
        )
    for pair_group in pair_groups:
        for source_id in pair_group.camera.ordered_source_ids:
            source = sources[source_id]
            source_basis.append(
                AlignmentSourceBasis(
                    camera_id=pair_group.camera.camera_id,
                    source_id=source_id,
                    fingerprint=_alignment_fingerprint(source.fingerprint),
                    duration_ticks=source.probe.duration_ticks,
                )
            )
    source_basis.sort(key=lambda basis: (basis.camera_id, basis.source_id))
    total_main_ticks = sum(
        sources[source_id].probe.duration_ticks
        for source_id in main_group.ordered_source_ids
    )
    summary = AlignmentSummary(
        total_main_ticks=total_main_ticks,
        camera_count=len(camera_items),
        mapped_ticks=sum(item.mapped_ticks for item in camera_items),
        missing_ticks=sum(item.missing_ticks for item in camera_items),
        uncertain_ticks=sum(item.uncertain_ticks for item in camera_items),
        conflict_ticks=sum(item.conflict_ticks for item in camera_items),
    )
    from roughcut.domain.alignment import AlignmentAlgorithm, AlignmentVerificationProfile

    algorithm = AlignmentAlgorithm(
        name=BBC_ALGORITHM_NAME,
        version=BBC_ALGORITHM_VERSION,
        upstream_commit=BBC_ALGORITHM_UPSTREAM,
        accuracy=None,
        num_processors=None,
        mapping_model="fixed_offset_equal_speed",
        ticks_per_second=ALIGNMENT_TICKS_PER_SECOND,
        verification_profile=AlignmentVerificationProfile(
            name=BBC_PROFILE_NAME,
            version=BBC_PROFILE_VERSION,
        ),
    )
    artifact = MulticamAlignmentArtifact(
        alignment_id=alignment_id,
        project_id=project.project_id,
        producer_operation_id=operation_id,
        created_at=_now(),
        request_hash=request_hash,
        input_hash=input_hash,
        algorithm=algorithm,
        main_camera=main_group,
        auxiliary_cameras=tuple(camera_items),
        source_basis=tuple(source_basis),
        intervals=tuple(intervals),
        summary=summary,
    )
    update_phase("alignment_publishing")
    AlignmentStore(root).publish(alignment_id, artifact)
    return artifact


def _correlation_empty_evidence(code: str = "no_candidate") -> dict[str, object]:
    # Closed uncertain correlation evidence with no probes (e.g., missing interval)
    return {
        "code": code,
        "provider": "audalign",
        "provider_version": AUDALIGN_VERSION,
        "recognizer": AUDALIGN_CORRELATION_RECOGNIZER,
        "probe_records": [],
        "support_probe_count": 0,
        "cluster_spread_ticks": None,
        "representative_b_ticks": None,
        "conflicting_b_ticks": [],
        "verification_profile": {
            "name": AUDALIGN_CORRELATION_PROFILE_NAME,
            "version": AUDALIGN_CORRELATION_PROFILE_VERSION,
        },
    }


def _build_correlation_evidence(
    *,
    code: str,
    probe_records: list[dict[str, object]],
    support: tuple[int, ...] | None = None,
    spread: int | None = None,
    representative: int | None = None,
    conflicts: list[int] | None = None,
) -> dict[str, object]:
    return {
        "code": code,
        "provider": "audalign",
        "provider_version": AUDALIGN_VERSION,
        "recognizer": AUDALIGN_CORRELATION_RECOGNIZER,
        "probe_records": list(probe_records),
        "support_probe_count": len(support) if support is not None else 0,
        "cluster_spread_ticks": spread,
        "representative_b_ticks": representative,
        "conflicting_b_ticks": list(conflicts or []),
        "verification_profile": {
            "name": AUDALIGN_CORRELATION_PROFILE_NAME,
            "version": AUDALIGN_CORRELATION_PROFILE_VERSION,
        },
    }


def _decode_main_wav(
    source_path: Path,
    output_path: Path,
    ffmpeg: ToolResolution,
    deadline: _Deadline,
    child_budget: ChildBudget,
    workspace_budget: _WorkspaceBudget,
) -> None:
    workspace_budget.reserve_decode(duration_ticks=0)  # placeholder, actual duration checked by caller
    try:
        decode_alignment_audio(
            source_path,
            output_path,
            ffmpeg_command=ffmpeg.command,
            ffmpeg_version=ffmpeg.version,
            timeout_seconds=deadline.remaining(),
            budget=child_budget,
        )
    except FFmpegAlignmentTimeBudgetError as error:
        raise AlignmentError("alignment_time_budget_exceeded", "main decode wall-time exceeded") from error
    except FFmpegAlignmentMemoryBudgetError as error:
        raise AlignmentError("alignment_memory_budget_exceeded", "main decode memory exceeded") from error
    except FFmpegAlignmentBudgetError as error:
        raise AlignmentError("alignment_disk_budget_exceeded", "main decode budget exceeded") from error
    except FFmpegAlignmentError as error:
        raise AlignmentError("alignment_main_decode_failed", "main source decode failed") from error
    workspace_budget.recheck()


def _extract_aux_excerpt(
    source_path: Path,
    output_path: Path,
    start_ticks: int,
    end_ticks: int,
    ffmpeg: ToolResolution,
    child_budget: ChildBudget,
    workspace_budget: _WorkspaceBudget,
) -> None:
    # Direct decode of a 15 s auxiliary excerpt from the original source (no full aux WAV)
    duration_ticks = end_ticks - start_ticks
    if (
        start_ticks < 0
        or duration_ticks != AUDALIGN_CORRELATION_PROBE_TICKS
    ):
        raise AlignmentError(
            "alignment_integrity_error",
            "correlation auxiliary probe window is not the frozen 15 seconds",
        )
    workspace_budget.reserve_decode(duration_ticks=duration_ticks)
    # Use Decimal contract for exact tick->seconds string
    start_seconds_text = format(
        Decimal(start_ticks) / Decimal(ALIGNMENT_TICKS_PER_SECOND), "f"
    )
    duration_seconds_text = format(
        Decimal(duration_ticks) / Decimal(ALIGNMENT_TICKS_PER_SECOND), "f"
    )
    command = [
        ffmpeg.command,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(source_path),
        "-map",
        "0:a:0",
        "-ss",
        start_seconds_text,
        "-t",
        duration_seconds_text,
        "-map_metadata",
        "-1",
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(ANALYSIS_SAMPLE_RATE_HZ),
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        str(output_path),
    ]
    try:
        result = run_bounded_child(
            command,
            budget=child_budget,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            raise AlignmentError(
                "auxiliary_decode_failed",
                "aux probe FFmpeg returned non-zero",
            )
    except ChildProcessTimeBudgetError as error:
        _recheck_workspace_after_child(workspace_budget, error)
        raise AlignmentError("alignment_time_budget_exceeded", "aux excerpt decode exceeded wall-time") from error
    except ChildProcessMemoryBudgetError as error:
        _recheck_workspace_after_child(workspace_budget, error)
        raise AlignmentError("alignment_memory_budget_exceeded", "aux excerpt decode exceeded memory") from error
    except ChildProcessBudgetError as error:
        _recheck_workspace_after_child(workspace_budget, error)
        raise AlignmentError("alignment_disk_budget_exceeded", "aux excerpt decode budget exceeded") from error
    except AlignmentError:
        raise
    except OSError as error:
        _recheck_workspace_after_child(workspace_budget, error)
        raise AlignmentError(
            "auxiliary_decode_failed", "aux excerpt decode failed"
        ) from error
    workspace_budget.recheck()
    # validate wav contract: 44.1k mono PCM16 and exactly 15s
    try:
        with wave.open(str(output_path), "rb") as wf:
            if not (
                wf.getframerate() == ANALYSIS_SAMPLE_RATE_HZ
                and wf.getnchannels() == 1
                and wf.getsampwidth() == ANALYSIS_SAMPLE_WIDTH_BYTES
            ):
                raise AlignmentError("auxiliary_decode_failed", "aux excerpt wav contract violation")
            expected_frames = (
                duration_ticks * ANALYSIS_SAMPLE_RATE_HZ
            ) // ALIGNMENT_TICKS_PER_SECOND
            if (
                duration_ticks * ANALYSIS_SAMPLE_RATE_HZ
            ) % ALIGNMENT_TICKS_PER_SECOND != 0 or wf.getnframes() != expected_frames:
                raise AlignmentError("auxiliary_decode_failed", "aux excerpt duration mismatch")
            if expected_frames != 661500:
                raise AlignmentError("auxiliary_decode_failed", "aux excerpt not 15s")
    except (OSError, wave.Error) as error:
        raise AlignmentError("auxiliary_decode_failed", "aux excerpt wav unreadable") from error


def _run_one_correlation_probe(
    main_wav: Path,
    aux_source_path: Path,
    aux_start: int,
    aux_end: int,
    percentage: int,
    alignment_python: Path,
    ffmpeg: ToolResolution,
    workspace: Path,
    pair_key: str,
    deadline: _Deadline,
    child_budget: ChildBudget,
    workspace_budget: _WorkspaceBudget,
) -> tuple[dict[str, object], int | None, str | None]:
    """Extract aux excerpt and run a single correlation probe.

    Returns (probe_record, derived_b_or_none, failure_code_or_none).
    """
    probe_excerpt = workspace / f"{pair_key}-probe-{percentage}.wav"
    output_json = workspace / f"{pair_key}-probe-{percentage}.json"
    # Ensure probe excerpt is cleaned after use (caller will unlink)
    try:
        try:
            _extract_aux_excerpt(
                aux_source_path,
                probe_excerpt,
                aux_start,
                aux_end,
                ffmpeg,
                child_budget,
                workspace_budget,
            )
        except AlignmentError as error:
            if error.code in {
                "alignment_time_budget_exceeded",
                "alignment_memory_budget_exceeded",
                "alignment_disk_budget_exceeded",
            }:
                raise
            return (
                {
                    "percentage": percentage,
                    "auxiliary_start_ticks": aux_start,
                    "auxiliary_end_ticks": aux_end,
                    "native_offset_seconds": None,
                    "derived_b_ticks": None,
                },
                None,
                "auxiliary_decode_failed",
            )
        workspace_budget.reserve_bytes(size_bytes=AUDALIGN_WORKER_MAX_OUTPUT_BYTES)
        try:
            result = run_audalign_correlation(
                alignment_python,
                probe_excerpt,
                main_wav,
                output_json,
                timeout_seconds=deadline.remaining(),
                budget=child_budget,
                max_raw_candidates=AUDALIGN_CORRELATION_MAX_RAW_CANDIDATES_PER_PROBE,
            )
        except (AudalignTimeBudgetError, AudalignMemoryBudgetError, AudalignBudgetError):
            raise
        except AudalignAdapterError:
            # worker failure => distinct from legitimate no_candidate
            return (
                {
                    "percentage": percentage,
                    "auxiliary_start_ticks": aux_start,
                    "auxiliary_end_ticks": aux_end,
                    "native_offset_seconds": None,
                    "derived_b_ticks": None,
                },
                None,
                "worker_failed",
            )
        _recheck_workspace_after_child(workspace_budget)
        if not result.candidates:
            return (
                {
                    "percentage": percentage,
                    "auxiliary_start_ticks": aux_start,
                    "auxiliary_end_ticks": aux_end,
                    "native_offset_seconds": None,
                    "derived_b_ticks": None,
                },
                None,
                None,
            )
        # Use only first candidate (strongest) - correlation returns single best offset per probe
        candidate = result.candidates[0]
        derived_b = source_relation_b(0, aux_start, candidate.offset_seconds)
        record = {
            "percentage": percentage,
            "auxiliary_start_ticks": aux_start,
            "auxiliary_end_ticks": aux_end,
            "native_offset_seconds": candidate.offset_seconds,
            "derived_b_ticks": derived_b,
        }
        return (record, derived_b, None)
    finally:
        # timely cleanup of probe temp files (strict serial, no three live)
        for path in (probe_excerpt, output_json):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        # recheck workspace after cleanup; do not swallow, let it propagate as disk fail closed
        workspace_budget.recheck()


def _process_correlation_camera(
    *,
    root: Path,
    sources: dict[str, SourceAsset],
    main_group: AlignmentCameraGroup,
    pair_group: _AlignmentPairGroup,
    main_wavs: dict[str, Path],
    alignment_python: Path,
    ffmpeg: ToolResolution,
    workspace: Path,
    deadline: _Deadline,
    child_budget: ChildBudget,
    workspace_budget: _WorkspaceBudget,
    frozen_identity: dict[str, dict[str, object]],
) -> tuple[AlignmentCamera, list[AlignmentInterval]]:
    intervals: list[AlignmentInterval] = []
    errors: list[AlignmentPerCameraError] = []
    mapped_total = 0
    missing_total = 0
    uncertain_total = 0
    conflict_total = 0
    # Build map from main source id to its paired aux ids
    pairs_by_main: dict[str, list[AlignmentSourcePair]] = {}
    for pair in pair_group.pairs:
        pairs_by_main.setdefault(pair.main_source_id, []).append(pair)

    for main_source_id in main_group.ordered_source_ids:
        main_duration = sources[main_source_id].probe.duration_ticks
        main_wav = main_wavs[main_source_id]
        main_source = sources[main_source_id]
        main_path = _resolve_source_path(root, main_source)
        # collect segments for this main source across its paired aux sources
        mapped_segments: list[tuple[int, int, str, int, int, dict[str, object]]] = []
        unverifiable_segments: list[tuple[int, int, str, dict[str, object]]] = []
        pairs = pairs_by_main.get(main_source_id, [])
        if not pairs:
            # No declared pair means no probes were run for this camera/source;
            # keep the uncovered span uncertain without calling it inconsistent.
            unverifiable_segments.append(
                (
                    0,
                    main_duration,
                    "uncertain",
                    _correlation_empty_evidence("insufficient_probes"),
                )
            )
            # continue to partition
        else:
            for pair in pairs:
                aux_source_id = pair.auxiliary_source_id
                aux_source = sources[aux_source_id]
                aux_path = _resolve_source_path(root, aux_source)
                aux_duration = aux_source.probe.duration_ticks
                # frozen identity checks before any probe
                _assert_source_matches_frozen(main_path, main_source, frozen_identity, main_source_id)
                _assert_source_matches_frozen(aux_path, aux_source, frozen_identity, aux_source_id)
                # Probe schedule
                if aux_duration < AUDALIGN_CORRELATION_PROBE_TICKS:
                    evidence = _build_correlation_evidence(
                        code="insufficient_probes",
                        probe_records=[],
                        support=(),
                        spread=None,
                        representative=None,
                    )
                    unverifiable_segments.append((0, main_duration, "uncertain", evidence))
                    continue
                probe_starts = correlation_probe_starts(aux_duration)
                if not probe_starts:
                    evidence = _build_correlation_evidence(
                        code="insufficient_probes",
                        probe_records=[],
                        support=(),
                        spread=None,
                        representative=None,
                    )
                    unverifiable_segments.append((0, main_duration, "uncertain", evidence))
                    continue
                # Run three probes serially
                probe_records: list[dict[str, object]] = []
                derived_bs: list[int | None] = []
                pair_key = f"pair-{main_source_id}-{aux_source_id}"
                probe_failure_codes: list[str | None] = []
                for pct, aux_start in zip(CORRELATION_PROBE_PERCENTS, probe_starts):
                    aux_end = aux_start + AUDALIGN_CORRELATION_PROBE_TICKS
                    # re-check identity before each bounded read
                    _assert_source_matches_frozen(main_path, main_source, frozen_identity, main_source_id)
                    _assert_source_matches_frozen(aux_path, aux_source, frozen_identity, aux_source_id)
                    record, derived, failure_code = _run_one_correlation_probe(
                        main_wav,
                        aux_path,
                        aux_start,
                        aux_end,
                        pct,
                        alignment_python,
                        ffmpeg,
                        workspace,
                        pair_key,
                        deadline,
                        child_budget,
                        workspace_budget,
                    )
                    probe_records.append(record)
                    derived_bs.append(derived)
                    probe_failure_codes.append(failure_code)
                pair_failure_code = (
                    "auxiliary_decode_failed"
                    if "auxiliary_decode_failed" in probe_failure_codes
                    else "worker_failed"
                    if "worker_failed" in probe_failure_codes
                    else None
                )
                if pair_failure_code is not None:
                    errors.append(
                        AlignmentPerCameraError(
                            code=(
                                "auxiliary_decode_failed"
                                if pair_failure_code == "auxiliary_decode_failed"
                                else "auxiliary_recognition_failed"
                            ),
                            source_id=aux_source_id,
                        )
                    )
                # Any non-budget decode/worker failure makes this exact pair
                # uncertain, even when the other probes happen to agree. A
                # safe pair in another declared relationship still proceeds.
                admission_code, representative, spread, support_indices = correlation_admission(
                    tuple(derived_bs), probe_starts
                )
                if pair_failure_code is None and admission_code == "fixed_offset_verified":
                    assert representative is not None and spread is not None and support_indices
                    evidence = _build_correlation_evidence(
                        code="fixed_offset_verified",
                        probe_records=probe_records,
                        support=support_indices,
                        spread=spread,
                        representative=representative,
                        conflicts=[],
                    )
                    # Map the whole overlap as mapped segment with that B
                    mapped_start = max(0, representative)
                    mapped_end = min(main_duration, aux_duration + representative)
                    if mapped_end > mapped_start:
                        mapped_segments.append(
                            (
                                mapped_start,
                                mapped_end,
                                aux_source_id,
                                mapped_start - representative,
                                mapped_end - representative,
                                evidence,
                            )
                        )
                    else:
                        # No overlap: B is valid but yields no main overlap, so it is not a verifiable mapping
                        # Convert to legitimate uncertain evidence (not fixed_offset_verified on uncertain interval)
                        no_overlap_evidence = _build_correlation_evidence(
                            code="no_candidate",
                            probe_records=probe_records,
                            support=(),
                            spread=None,
                            representative=None,
                            conflicts=[],
                        )
                        unverifiable_segments.append((0, main_duration, "uncertain", no_overlap_evidence))
                else:
                    # Handle worker_failed distinct from legitimate no_candidate
                    if pair_failure_code is not None:
                        evidence_code = pair_failure_code
                    else:
                        code_map = {
                            "no_candidate": "no_candidate",
                            "probe_inconsistent": "probe_inconsistent",
                            "insufficient_probes": "insufficient_probes",
                        }
                        evidence_code = code_map.get(admission_code, "probe_inconsistent")
                    evidence = _build_correlation_evidence(
                        code=evidence_code,
                        probe_records=probe_records,
                        support=(),
                        spread=None,
                        representative=None,
                        conflicts=[],
                    )
                    unverifiable_segments.append((0, main_duration, "uncertain", evidence))
        # Partition this main source's timeline
        if not pairs:
            # placeholder already added; partition will emit uncertain
            mapped, missing, uncertain, conflict = _waveform_partition_camera_timeline(
                main_duration=main_duration,
                camera_id=pair_group.camera.camera_id,
                main_source_id=main_source_id,
                mapped_segments=mapped_segments,
                unverifiable_segments=unverifiable_segments,
                intervals=intervals,
                empty_evidence=lambda: _correlation_empty_evidence("no_candidate"),
            )
        else:
            # Check if there are any mapped segments; if none, the gap handling will use unverifiable_segments
            mapped, missing, uncertain, conflict = _waveform_partition_camera_timeline(
                main_duration=main_duration,
                camera_id=pair_group.camera.camera_id,
                main_source_id=main_source_id,
                mapped_segments=mapped_segments,
                unverifiable_segments=unverifiable_segments,
                intervals=intervals,
                empty_evidence=lambda: _correlation_empty_evidence("no_candidate"),
            )
        mapped_total += mapped
        missing_total += missing
        uncertain_total += uncertain
        conflict_total += conflict

    total_main_ticks = sum(sources[s].probe.duration_ticks for s in main_group.ordered_source_ids)
    if mapped_total == total_main_ticks and not errors:
        status = "complete"
    elif mapped_total > 0:
        status = "partial"
    elif errors:
        status = "failed"
    else:
        status = "omitted"
    return (
        AlignmentCamera(
            camera_id=pair_group.camera.camera_id,
            ordered_source_ids=pair_group.camera.ordered_source_ids,
            status=cast(Any, status),
            mapped_ticks=mapped_total,
            missing_ticks=missing_total,
            uncertain_ticks=uncertain_total,
            conflict_ticks=conflict_total,
            errors=tuple(errors),
        ),
        intervals,
    )


def _execute_audalign_correlation_alignment(
    root: Path,
    project: Project,
    scope: ProjectOperationScope,
    sources: dict[str, SourceAsset],
    main_group: AlignmentCameraGroup,
    auxiliary_groups: tuple[AlignmentCameraGroup, ...],
    alignment_python: Path,
    ffmpeg: ToolResolution,
    workspace: Path,
    request_hash: str,
    input_hash: str,
    request_projection: dict[str, object],
    runtime: Any,
    deadline: _Deadline,
    memory_limit: int,
    child_budget: ChildBudget,
    workspace_budget: _WorkspaceBudget,
    frozen_identity: dict[str, dict[str, object]],
    update_phase: Callable[[str], None],
    operation_id: str,
    alignment_id: str,
    *,
    pair_groups: tuple[_AlignmentPairGroup, ...],
) -> MulticamAlignmentArtifact:
    del scope, auxiliary_groups, request_projection, memory_limit
    # Decode each unique full main once at 44.1k mono PCM16 and cache
    main_wavs: dict[str, Path] = {}
    update_phase("alignment_decoding_main")
    for source_id in main_group.ordered_source_ids:
        source = sources[source_id]
        source_path = _resolve_source_path(root, source)
        _assert_source_matches_frozen(source_path, source, frozen_identity, source_id)
        wav = workspace / f"main-{source_id}.wav"
        workspace_budget.reserve_decode(duration_ticks=source.probe.duration_ticks)
        try:
            decode_alignment_audio(
                source_path,
                wav,
                ffmpeg_command=ffmpeg.command,
                ffmpeg_version=ffmpeg.version,
                timeout_seconds=deadline.remaining(),
                budget=child_budget,
            )
        except FFmpegAlignmentTimeBudgetError as error:
            raise AlignmentError("alignment_time_budget_exceeded", "main decode wall-time exceeded") from error
        except FFmpegAlignmentMemoryBudgetError as error:
            raise AlignmentError("alignment_memory_budget_exceeded", "main decode memory exceeded") from error
        except FFmpegAlignmentBudgetError as error:
            raise AlignmentError("alignment_disk_budget_exceeded", "main decode budget exceeded") from error
        except FFmpegAlignmentError as error:
            raise AlignmentError("alignment_main_decode_failed", "main decode failed") from error
        workspace_budget.recheck()
        main_wavs[source_id] = wav

    camera_items: list[AlignmentCamera] = []
    intervals: list[AlignmentInterval] = []
    for pair_group in pair_groups:
        update_phase("alignment_processing_auxiliary")
        try:
            camera, camera_intervals = _process_correlation_camera(
                root=root,
                sources=sources,
                main_group=main_group,
                pair_group=pair_group,
                main_wavs=main_wavs,
                alignment_python=alignment_python,
                ffmpeg=ffmpeg,
                workspace=workspace,
                deadline=deadline,
                child_budget=child_budget,
                workspace_budget=workspace_budget,
                frozen_identity=frozen_identity,
            )
        except (ChildProcessTimeBudgetError, AudalignTimeBudgetError) as error:
            _recheck_workspace_after_child(workspace_budget, error)
            raise AlignmentError("alignment_time_budget_exceeded", "correlation alignment exceeded wall-time") from error
        except (ChildProcessMemoryBudgetError, AudalignMemoryBudgetError) as error:
            _recheck_workspace_after_child(workspace_budget, error)
            raise AlignmentError("alignment_memory_budget_exceeded", "correlation alignment exceeded memory") from error
        except (ChildProcessBudgetError, AudalignBudgetError) as error:
            _recheck_workspace_after_child(workspace_budget, error)
            raise AlignmentError("alignment_disk_budget_exceeded", "correlation alignment exceeded disk budget") from error
        camera_items.append(camera)
        intervals.extend(camera_intervals)
        # cleanup per-camera probe temps already done per probe; ensure no leftover main wav leak
        # (main wavs remain cached for other cameras)

    update_phase("alignment_revalidating_basis")
    _revalidate_basis(
        root,
        project,
        sources,
        runtime,
        frozen_identity,
        require_full_binding=False,
        require_alignment_selection=True,
    )
    # Build source_basis sorted
    source_basis: list[AlignmentSourceBasis] = []
    for source_id in main_group.ordered_source_ids:
        source = sources[source_id]
        source_basis.append(
            AlignmentSourceBasis(
                camera_id="main",
                source_id=source_id,
                fingerprint=_alignment_fingerprint(source.fingerprint),
                duration_ticks=source.probe.duration_ticks,
            )
        )
    for pair_group in pair_groups:
        for source_id in pair_group.camera.ordered_source_ids:
            source = sources[source_id]
            source_basis.append(
                AlignmentSourceBasis(
                    camera_id=pair_group.camera.camera_id,
                    source_id=source_id,
                    fingerprint=_alignment_fingerprint(source.fingerprint),
                    duration_ticks=source.probe.duration_ticks,
                )
            )
    source_basis.sort(key=lambda b: (b.camera_id, b.source_id))
    total_main_ticks = sum(sources[s].probe.duration_ticks for s in main_group.ordered_source_ids)
    summary = AlignmentSummary(
        total_main_ticks=total_main_ticks,
        camera_count=len(camera_items),
        mapped_ticks=sum(c.mapped_ticks for c in camera_items),
        missing_ticks=sum(c.missing_ticks for c in camera_items),
        uncertain_ticks=sum(c.uncertain_ticks for c in camera_items),
        conflict_ticks=sum(c.conflict_ticks for c in camera_items),
    )
    from roughcut.domain.alignment import AlignmentAlgorithm, AlignmentVerificationProfile

    algorithm = AlignmentAlgorithm(
        name=AUDALIGN_CORRELATION_ALGORITHM_NAME,
        version=AUDALIGN_CORRELATION_ALGORITHM_VERSION,
        upstream_commit=AUDALIGN_CORRELATION_UPSTREAM_COMMIT,
        accuracy=None,
        num_processors=None,
        mapping_model="fixed_offset_equal_speed",
        ticks_per_second=ALIGNMENT_TICKS_PER_SECOND,
        verification_profile=AlignmentVerificationProfile(
            name=AUDALIGN_CORRELATION_PROFILE_NAME,
            version=AUDALIGN_CORRELATION_PROFILE_VERSION,
        ),
    )
    artifact = MulticamAlignmentArtifact(
        alignment_id=alignment_id,
        project_id=project.project_id,
        producer_operation_id=operation_id,
        created_at=_now(),
        request_hash=request_hash,
        input_hash=input_hash,
        algorithm=algorithm,
        main_camera=main_group,
        auxiliary_cameras=tuple(camera_items),
        source_basis=tuple(source_basis),
        intervals=tuple(intervals),
        summary=summary,
    )
    update_phase("alignment_publishing")
    AlignmentStore(root).publish(alignment_id, artifact)
    # cleanup main wavs
    for wav in main_wavs.values():
        try:
            wav.unlink(missing_ok=True)
        except OSError:
            pass
    return artifact


def _execute_audalign_alignment_legacy(
    root: Path,
    project: Project,
    scope: ProjectOperationScope,
    sources: dict[str, SourceAsset],
    main_group: AlignmentCameraGroup,
    auxiliary_groups: tuple[AlignmentCameraGroup, ...],
    alignment_python: Path,
    ffmpeg: ToolResolution,
    workspace: Path,
    request_hash: str,
    input_hash: str,
    request_projection: dict[str, object],
    runtime: Any,
    deadline: _Deadline,
    memory_limit: int,
    child_budget: ChildBudget,
    workspace_budget: _WorkspaceBudget,
    frozen_identity: dict[str, dict[str, object]],
    update_phase: Callable[[str], None],
    operation_id: str,
    alignment_id: str,
) -> MulticamAlignmentArtifact:
    update_phase("alignment_decoding_main")
    main_wavs: dict[str, Path] = {}
    main_fingerprints: dict[str, AlignmentSourceFingerprint] = {}
    main_durations: dict[str, int] = {}
    source_basis: list[AlignmentSourceBasis] = []
    for source_id in main_group.ordered_source_ids:
        source = sources[source_id]
        source_path = _resolve_source_path(root, source)
        # every main Source is checked against the frozen snapshot before its
        # decode starts, not only at publish time
        _assert_source_matches_frozen(source_path, source, frozen_identity, source_id)
        fingerprint = _alignment_fingerprint(source.fingerprint)
        main_fingerprints[source_id] = fingerprint
        main_durations[source_id] = source.probe.duration_ticks
        wav = workspace / f"main-{source_id}.wav"
        workspace_budget.reserve_decode(duration_ticks=source.probe.duration_ticks)
        try:
            decode_alignment_audio(
                source_path,
                wav,
                ffmpeg_command=ffmpeg.command,
                ffmpeg_version=ffmpeg.version,
                timeout_seconds=deadline.remaining(),
                budget=child_budget,
            )
        except FFmpegAlignmentTimeBudgetError as error:
            raise AlignmentError(
                "alignment_time_budget_exceeded",
                "main source audio decode exceeded the wall-time budget",
            ) from error
        except FFmpegAlignmentMemoryBudgetError as error:
            raise AlignmentError(
                "alignment_memory_budget_exceeded",
                "main source audio decode exceeded the memory budget",
            ) from error
        except FFmpegAlignmentBudgetError as error:
            raise AlignmentError(
                "alignment_main_decode_failed",
                "main source audio child accounting failed",
            ) from error
        except FFmpegAlignmentError as error:
            raise AlignmentError(
                "alignment_main_decode_failed",
                "main source audio could not be decoded",
            ) from error
        workspace_budget.recheck()
        main_wavs[source_id] = wav
        source_basis.append(
            AlignmentSourceBasis(
                camera_id="main",
                source_id=source_id,
                fingerprint=fingerprint,
                duration_ticks=source.probe.duration_ticks,
            )
        )
    update_phase("alignment_indexing_main")

    camera_items: list[AlignmentCamera] = []
    intervals: list[AlignmentInterval] = []
    main_total_ticks = sum(main_durations.values())
    for group in auxiliary_groups:
        update_phase("alignment_processing_auxiliary")
        camera_item, camera_intervals = _process_auxiliary_camera(
            root,
            sources,
            group,
            main_wavs,
            main_durations,
            main_group,
            alignment_python,
            ffmpeg,
            workspace,
            deadline,
            memory_limit,
            child_budget,
            workspace_budget,
            frozen_identity,
            main_total_ticks,
        )
        camera_items.append(camera_item)
        intervals.extend(camera_intervals)
        for source_id in group.ordered_source_ids:
            source = sources[source_id]
            source_path = _resolve_source_path(root, source)
            source_basis.append(
                AlignmentSourceBasis(
                    camera_id=group.camera_id,
                    source_id=source_id,
                    fingerprint=_alignment_fingerprint(source.fingerprint),
                    duration_ticks=source.probe.duration_ticks,
                )
            )
    source_basis.sort(key=lambda basis: (basis.camera_id, basis.source_id))

    update_phase("alignment_revalidating_basis")
    _revalidate_basis(
        root,
        project,
        sources,
        runtime,
        frozen_identity,
    )

    summary = AlignmentSummary(
        total_main_ticks=main_total_ticks,
        camera_count=len(auxiliary_groups),
        mapped_ticks=sum(item.mapped_ticks for item in camera_items),
        missing_ticks=sum(item.missing_ticks for item in camera_items),
        uncertain_ticks=sum(item.uncertain_ticks for item in camera_items),
        conflict_ticks=sum(item.conflict_ticks for item in camera_items),
    )
    from roughcut.domain.alignment import (
        AlignmentAlgorithm,
        AlignmentVerificationProfile,
    )

    algorithm = AlignmentAlgorithm(
        name="audalign_fingerprint",
        version="1.3.1",
        upstream_commit="d5955ae8a85b1cd480dadd005c3f88986f4ebbef",
        accuracy=2,
        num_processors=1,
        mapping_model="fixed_offset_equal_speed",
        ticks_per_second=ALIGNMENT_TICKS_PER_SECOND,
        verification_profile=AlignmentVerificationProfile(
            name="roughcut_audalign_fixed_offset",
            version=ALIGNMENT_WRITE_PROFILE_VERSION,
        ),
    )
    artifact = MulticamAlignmentArtifact(
        alignment_id=alignment_id,
        project_id=project.project_id,
        producer_operation_id=operation_id,
        created_at=_now(),
        request_hash=request_hash,
        input_hash=input_hash,
        algorithm=algorithm,
        main_camera=main_group,
        auxiliary_cameras=tuple(camera_items),
        source_basis=tuple(source_basis),
        intervals=tuple(intervals),
        summary=summary,
    )
    update_phase("alignment_publishing")
    store = AlignmentStore(root)
    store.publish(alignment_id, artifact)
    return artifact


def _process_auxiliary_camera(
    root: Path,
    sources: dict[str, SourceAsset],
    group: AlignmentCameraGroup,
    main_wavs: dict[str, Path],
    main_durations: dict[str, int],
    main_group: AlignmentCameraGroup,
    alignment_python: Path,
    ffmpeg: ToolResolution,
    workspace: Path,
    deadline: _Deadline,
    memory_limit: int,
    child_budget: ChildBudget,
    workspace_budget: _WorkspaceBudget,
    frozen_identity: dict[str, dict[str, object]],
    main_total_ticks: int,
) -> tuple[AlignmentCamera, list[AlignmentInterval]]:
    errors: list[AlignmentPerCameraError] = []
    mapped_total = 0
    missing_total = 0
    uncertain_total = 0
    conflict_total = 0
    intervals: list[AlignmentInterval] = []
    failed_sources: dict[str, int] = {}
    for main_source_id in main_group.ordered_source_ids:
        main_duration = main_durations[main_source_id]
        (
            camera_intervals,
            mapped,
            missing,
            uncertain,
            conflict,
            source_errors,
        ) = _align_main_source_to_auxiliaries(
            main_source_id,
            main_duration,
            main_wavs[main_source_id],
            group,
            sources,
            root,
            alignment_python,
            ffmpeg,
            workspace,
            deadline,
            memory_limit,
            child_budget,
            workspace_budget,
            frozen_identity,
            failed_sources=failed_sources,
        )
        intervals.extend(camera_intervals)
        mapped_total += mapped
        missing_total += missing
        uncertain_total += uncertain
        conflict_total += conflict
        errors.extend(source_errors)
    if (
        mapped_total == 0
        and missing_total == 0
        and uncertain_total == 0
        and conflict_total == 0
    ):
        status = "failed"
    elif mapped_total == main_total_ticks and not errors:
        status = "complete"
    elif mapped_total > 0:
        status = "partial"
    elif errors:
        status = "failed"
    else:
        status = "omitted"
    return (
        AlignmentCamera(
            camera_id=group.camera_id,
            ordered_source_ids=group.ordered_source_ids,
            status=status,  # type: ignore[arg-type]
            mapped_ticks=mapped_total,
            missing_ticks=missing_total,
            uncertain_ticks=uncertain_total,
            conflict_ticks=conflict_total,
            errors=tuple(errors),
        ),
        intervals,
    )


def _align_main_source_to_auxiliaries(
    main_source_id: str,
    main_duration: int,
    main_wav: Path,
    group: AlignmentCameraGroup,
    sources: dict[str, SourceAsset],
    root: Path,
    alignment_python: Path,
    ffmpeg: ToolResolution,
    workspace: Path,
    deadline: _Deadline,
    memory_limit: int,
    child_budget: ChildBudget,
    workspace_budget: _WorkspaceBudget,
    frozen_identity: dict[str, dict[str, object]],
    failed_sources: dict[str, int] | None = None,
) -> tuple[
    list[AlignmentInterval],
    int,
    int,
    int,
    int,
    tuple[AlignmentPerCameraError, ...],
]:
    """Map one main source onto one auxiliary camera's ordered sources.

    The camera partition over the main timeline is built from the union of
    verified mapped segments and the unverifiable coverage of any source that
    failed/uncertain/conflicted; everything else is proven missing only after
    every source succeeded.
    """
    intervals: list[AlignmentInterval] = []
    mapped_segments: list[
        tuple[int, int, str, int, int, int, int, tuple[int, ...]]
    ] = []
    unverifiable_segments: list[tuple[int, int, str, str, dict[str, object] | None]] = []
    pair_errors: list[AlignmentPerCameraError] = []
    # the shared failed_sources dict is passed through, never copied: a
    # source with a definite auxiliary_decode_failed is recorded once and
    # skipped by every later main source without re-decoding it
    failed: dict[str, int] = failed_sources if failed_sources is not None else {}
    for source_id in group.ordered_source_ids:
        if source_id in failed:
            # decode failure: the source's potential main coverage is bounded
            # by its own duration and stays uncertain, never missing
            potential = min(main_duration, failed[source_id])
            unverifiable_segments.append(
                (0, potential, source_id, "uncertain", None)
            )
            if potential < main_duration:
                unverifiable_segments.append(
                    (potential, main_duration, source_id, "uncertain", None)
                )
            continue
        source = sources[source_id]
        aux_duration = source.probe.duration_ticks
        aux_source_path = _resolve_source_path(root, source)
        result = _align_one_pair_mono_first(
            main_source_id,
            main_duration,
            main_wav,
            _resolve_source_path(root, sources[main_source_id]),
            source_id,
            aux_duration,
            aux_source_path,
            sources[main_source_id],
            source,
            group.camera_id,
            alignment_python,
            ffmpeg,
            workspace,
            deadline,
            memory_limit,
            child_budget,
            workspace_budget,
            frozen_identity,
        )
        if result["classifications"] == "failed":
            error_code = str(result.get("error_code", "auxiliary_recognition_failed"))
            if error_code not in {
                "auxiliary_decode_failed",
                "auxiliary_recognition_failed",
                "auxiliary_verification_failed",
                "auxiliary_audio_stream_unsupported",
            }:
                error_code = "auxiliary_recognition_failed"
            pair_errors.append(
                AlignmentPerCameraError(code=error_code, source_id=source_id)
            )
            if error_code == "auxiliary_decode_failed":
                failed[source_id] = aux_duration
            unverifiable_segments.append(
                (0, main_duration, source_id, "uncertain", None)
            )
            continue
        if result["classifications"] == "mapped":
            b = int(result["b_ticks"])  # type: ignore[call-overload]
            mapped_main_start = max(0, b)
            mapped_main_end = min(main_duration, aux_duration + b)
            mapped_aux_start = mapped_main_start - b
            mapped_aux_end = mapped_main_end - b
            if mapped_main_end > mapped_main_start:
                mapped_segments.append(
                    (
                        mapped_main_start,
                        mapped_main_end,
                        source_id,
                        mapped_aux_start,
                        mapped_aux_end,
                        int(result["max_local_offset_error_ticks"]),  # type: ignore[call-overload]
                        int(result["raw_candidate_count"]),  # type: ignore[call-overload]
                        tuple(
                            int(item)
                            for item in result["matching_fingerprint_counts"]  # type: ignore[attr-defined]
                        ),
                    )
                )
        elif result["classifications"] == "conflict":
            # a verified conflict covers the whole main timeline as conflict;
            # it is not an execution error (the pair ran to completion), so no
            # per-camera error is recorded and a pure conflict camera is
            # omitted, never failed. The real verified evidence travels with
            # the unverifiable span into the partition and is never rewritten
            # as no_candidate/0/[]
            evidence: dict[str, object] = {
                "code": "fixed_offset_verified",
                "raw_candidate_count": int(  # type: ignore[call-overload]
                    result["raw_candidate_count"]
                ),
                "matching_fingerprint_counts": [
                    int(item)
                    for item in result["matching_fingerprint_counts"]  # type: ignore[attr-defined]
                ],
                "verification_window_count": int(  # type: ignore[call-overload]
                    result["verification_window_count"]
                ),
                "verification_profile": {
                    "name": "roughcut_audalign_fixed_offset",
                    "version": ALIGNMENT_WRITE_PROFILE_VERSION,
                },
                "max_local_offset_error_ticks": int(  # type: ignore[call-overload]
                    result["max_local_offset_error_ticks"]
                ),
            }
            unverifiable_segments.append(
                (0, main_duration, source_id, "conflict", evidence)
            )
        else:
            # no verified relation for this source: its whole potential
            # coverage is unverifiable and stays uncertain/conflict, never
            # missing (the source was processed but could not prove absence)
            classification = str(result["classifications"])
            unverifiable_segments.append(
                (0, main_duration, source_id, classification, None)
            )
    # close each pair workspace after its evidence is in memory
    for source_id in group.ordered_source_ids:
        shutil.rmtree(
            workspace / f"pair-{main_source_id}-{source_id}", ignore_errors=True
        )

    mapped_ticks, missing_ticks, uncertain_ticks, conflict_ticks = (
        _partition_camera_timeline(
            main_duration,
            mapped_segments,
            unverifiable_segments,
            intervals,
            group.camera_id,
            main_source_id,
        )
    )
    return (
        intervals,
        mapped_ticks,
        missing_ticks,
        uncertain_ticks,
        conflict_ticks,
        tuple(pair_errors),
    )


def _partition_camera_timeline(
    main_duration: int,
    mapped_segments: list[
        tuple[int, int, str, int, int, int, int, tuple[int, ...]]
    ],
    unverifiable_segments: list[
        tuple[int, int, str, str, dict[str, object] | None]
    ],
    intervals: list[AlignmentInterval],
    camera_id: str,
    main_source_id: str,
) -> tuple[int, int, int, int]:
    """Build one non-overlapping partition of the main timeline.

    Verified mapped segments win over unverifiable coverage; unverifiable
    source coverage becomes uncertain/conflict (never missing); the remaining
    complement is missing only when no source was unverifiable.
    """
    # Build the union partition of all verified mapped segments; where two or
    # more verified segments overlap, the overlapped range is closed conflict
    # (never first-wins), and the remaining complement is missing (or
    # uncertain/conflict when unverifiable sources exist). A sweep over the
    # segment endpoints emits exact mapped/conflict/gap intervals.
    ordered = sorted(mapped_segments, key=lambda item: (item[0], item[1]))
    events: list[tuple[int, int, int]] = []
    for index, (start, end, _src, _a, _b, _err, _raw, _counts) in enumerate(
        ordered
    ):
        events.append((start, 1, index))
        events.append((end, -1, index))
    events.sort(key=lambda item: (item[0], -item[1], item[2]))
    mapped_ticks = 0
    missing_ticks = 0
    uncertain_ticks = 0
    conflict_ticks = 0
    position = 0
    active: set[int] = set()
    gap_index = 0
    for point, delta, index in events:
        if point <= position:
            if delta == 1:
                active.add(index)
            else:
                active.discard(index)
            continue
        if point > position:
            if not active:
                gap_class = _append_gap(
                    intervals,
                    camera_id,
                    main_source_id,
                    position,
                    point,
                    gap_index,
                    unverifiable_segments,
                )
                if gap_class == "missing":
                    missing_ticks += point - position
                elif gap_class == "conflict":
                    conflict_ticks += point - position
                else:
                    uncertain_ticks += point - position
                gap_index += 1
            elif len(active) == 1:
                owner = ordered[next(iter(active))]
                mapped_ticks += point - position
                intervals.append(
                    _mapped_span_interval(
                        camera_id,
                        main_source_id,
                        position,
                        point,
                        owner,
                        gap_index,
                    )
                )
                gap_index += 1
            else:
                conflict_ticks += point - position
                intervals.append(
                    _conflict_span_interval(
                        camera_id,
                        main_source_id,
                        position,
                        point,
                        gap_index,
                        ordered,
                        active,
                    )
                )
                gap_index += 1
            position = point
        if delta == 1:
            active.add(index)
        else:
            active.discard(index)
    if position < main_duration:
        if not active:
            gap_class = _append_gap(
                intervals,
                camera_id,
                main_source_id,
                position,
                main_duration,
                gap_index,
                unverifiable_segments,
            )
            if gap_class == "missing":
                missing_ticks += main_duration - position
            elif gap_class == "conflict":
                conflict_ticks += main_duration - position
            else:
                uncertain_ticks += main_duration - position
        elif len(active) == 1:
            owner = ordered[next(iter(active))]
            mapped_ticks += main_duration - position
            intervals.append(
                _mapped_span_interval(
                    camera_id,
                    main_source_id,
                    position,
                    main_duration,
                    owner,
                    gap_index,
                )
            )
        else:
            conflict_ticks += main_duration - position
            intervals.append(
                _conflict_span_interval(
                    camera_id,
                    main_source_id,
                    position,
                    main_duration,
                    gap_index,
                    ordered,
                    active,
                )
            )
    return mapped_ticks, missing_ticks, uncertain_ticks, conflict_ticks


def _mapped_span_interval(
    camera_id: str,
    main_source_id: str,
    start: int,
    end: int,
    owner: tuple[int, int, str, int, int, int, int, tuple[int, ...]],
    index: int,
) -> AlignmentInterval:
    """Emit one mapped span with the owning segment's auxiliary detail.

    The raw candidate count and the full original-order matching fingerprint
    counts are the segment's actual upstream evidence, never a synthesized
    placeholder, so raw_candidate_count == len(matching_fingerprint_counts).
    """
    _ms, _me, source_id, aux_start, _aux_end, max_error, raw_count, counts = owner
    return AlignmentInterval(
        interval_id=f"ali_{camera_id}_{main_source_id}_{source_id}_m_{index}",
        auxiliary_camera_id=camera_id,
        classification="mapped",
        main={"source_id": main_source_id, "start_ticks": start, "end_ticks": end},
        auxiliary={
            "source_id": source_id,
            "start_ticks": aux_start + (start - _ms),
            "end_ticks": aux_start + (end - _ms),
        },
        evidence={
            "code": "fixed_offset_verified",
            "raw_candidate_count": raw_count,
            "matching_fingerprint_counts": list(counts),
            "verification_window_count": ALIGNMENT_VERIFICATION_WINDOW_COUNT,
            "verification_profile": {
                "name": "roughcut_audalign_fixed_offset",
                "version": ALIGNMENT_WRITE_PROFILE_VERSION,
            },
            "max_local_offset_error_ticks": max_error,
        },
    )


def _conflict_span_interval(
    camera_id: str,
    main_source_id: str,
    start: int,
    end: int,
    index: int,
    ordered: list[
        tuple[int, int, str, int, int, int, int, tuple[int, ...]]
    ],
    active: set[int],
) -> AlignmentInterval:
    """One conflict span merging the actual evidence of every active owner.

    The real matching fingerprint counts of all verified owners are merged in
    request source order, the raw count equals the merged length, the
    verification window count stays the contract value 3 (never summed), and
    the maximum local error is the maximum across owners.
    """
    max_error = 0
    merged: list[int] = []
    for owner_index in sorted(active):
        _s, _e, _src, _a, _b, error, _raw, counts = ordered[owner_index]
        max_error = max(max_error, error)
        merged.extend(counts)
    return AlignmentInterval(
        interval_id=f"ali_{camera_id}_{main_source_id}_conf_{index}",
        auxiliary_camera_id=camera_id,
        classification="conflict",
        main={"source_id": main_source_id, "start_ticks": start, "end_ticks": end},
        auxiliary=None,
        evidence={
            "code": "fixed_offset_verified",
            "raw_candidate_count": len(merged),
            "matching_fingerprint_counts": merged,
            "verification_window_count": ALIGNMENT_VERIFICATION_WINDOW_COUNT,
            "verification_profile": {
                "name": "roughcut_audalign_fixed_offset",
                "version": ALIGNMENT_WRITE_PROFILE_VERSION,
            },
            "max_local_offset_error_ticks": max_error,
        },
    )


def _append_gap(
    intervals: list[AlignmentInterval],
    camera_id: str,
    main_source_id: str,
    start: int,
    end: int,
    index: int,
    unverifiable_segments: list[
        tuple[int, int, str, str, dict[str, object] | None]
    ],
) -> str:
    """Append one gap interval and return its classification: conflict/
    uncertain when unverifiable sources cover it, missing only when every
    source was verified successfully."""
    if not unverifiable_segments:
        intervals.append(
            _missing_interval(
                camera_id,
                f"ali_{camera_id}_{main_source_id}_miss_{index}",
                main_source_id,
                start,
                end,
            )
        )
        return "missing"
    conflict = _gap_is_conflict(unverifiable_segments, start, end)
    intervals.append(
        _unverifiable_gap_interval(
            camera_id,
            main_source_id,
            start,
            end,
            index,
            conflict,
            unverifiable_segments,
        )
    )
    return "conflict" if conflict else "uncertain"


def _gap_is_conflict(
    unverifiable_segments: list[
        tuple[int, int, str, str, dict[str, object] | None]
    ],
    start: int,
    end: int,
) -> bool:
    """A gap is conflict when every covering source conflicted."""
    covering: list[str] = []
    for _s, _e, _sid, classification, _evidence in unverifiable_segments:
        if _e > start and _s < end:
            covering.append(classification)
    return bool(covering) and all(item == "conflict" for item in covering)


def _unverifiable_gap_interval(
    camera_id: str,
    main_source_id: str,
    start: int,
    end: int,
    index: int,
    conflict: bool,
    unverifiable_segments: list[
        tuple[int, int, str, str, dict[str, object] | None]
    ],
) -> AlignmentInterval:
    if conflict:
        # the conflict evidence travels with the unverifiable span; the first
        # verified conflict evidence covering this range is preserved exactly
        # (never rewritten as no_candidate/0/[])
        evidence: dict[str, object] | None = None
        for _s, _e, _sid, _classification, span_evidence in unverifiable_segments:
            if span_evidence is not None and _e > start and _s < end:
                evidence = span_evidence
                break
        if evidence is not None:
            return AlignmentInterval(
                interval_id=f"ali_{camera_id}_{main_source_id}_conf_{index}",
                auxiliary_camera_id=camera_id,
                classification="conflict",
                main={
                    "source_id": main_source_id,
                    "start_ticks": start,
                    "end_ticks": end,
                },
                auxiliary=None,
                evidence=evidence,
            )
        return AlignmentInterval(
            interval_id=f"ali_{camera_id}_{main_source_id}_conf_{index}",
            auxiliary_camera_id=camera_id,
            classification="conflict",
            main={
                "source_id": main_source_id,
                "start_ticks": start,
                "end_ticks": end,
            },
            auxiliary=None,
            evidence={
                "code": "no_candidate",
                "raw_candidate_count": 0,
                "matching_fingerprint_counts": [],
                "verification_window_count": 0,
                "verification_profile": {
                    "name": "roughcut_audalign_fixed_offset",
                    "version": ALIGNMENT_WRITE_PROFILE_VERSION,
                },
                "max_local_offset_error_ticks": None,
            },
        )
    return _uncertain_interval(
        camera_id,
        f"ali_{camera_id}_{main_source_id}_unc_{index}",
        main_source_id,
        start,
        end,
    )


def _align_one_pair(
    main_source_id: str,
    main_duration: int,
    main_wav: Path,
    aux_source_id: str,
    aux_duration: int,
    aux_wav: Path,
    camera_id: str,
    alignment_python: Path,
    ffmpeg: ToolResolution,
    workspace: Path,
    deadline: _Deadline,
    memory_limit: int,
    child_budget: ChildBudget,
    channel: str | None = None,
    *,
    profile2: bool = False,
    workspace_budget: _WorkspaceBudget | None = None,
) -> dict[str, object]:
    """Run the frozen profile: raw candidates, grouping, three-window verify.

    Candidates are verified in audalign's original return order within each
    12000-tick group; the first candidate that passes all three windows is the
    group's saved relation. Two or more verified groups close as conflict.
    Every child shares the operation deadline and the memory limit.
    """
    if profile2:
        if workspace_budget is None:
            raise AlignmentError(
                "alignment_disk_budget_exceeded",
                "profile 2 requires the operation workspace budget",
            )
        return _align_one_pair_profile2(
            main_source_id,
            main_duration,
            main_wav,
            aux_source_id,
            aux_duration,
            aux_wav,
            camera_id,
            alignment_python,
            ffmpeg,
            workspace,
            deadline,
            memory_limit,
            child_budget,
            workspace_budget,
            channel,
        )
    pair_workspace = workspace / f"pair-{main_source_id}-{aux_source_id}"
    pair_workspace.mkdir(parents=True, exist_ok=True)
    output = pair_workspace / f"raw-{channel or 'mono'}.json"
    if workspace_budget is not None:
        workspace_budget.reserve_bytes(
            size_bytes=AUDALIGN_WORKER_MAX_OUTPUT_BYTES
        )
    try:
        match = run_audalign_recognize(
            alignment_python,
            aux_wav,
            main_wav,
            output,
            timeout_seconds=deadline.remaining(),
            budget=child_budget,
        )
    except AudalignTimeBudgetError as error:
        _recheck_workspace_after_child(workspace_budget, error)
        raise AlignmentError(
            "alignment_time_budget_exceeded",
            "audalign recognition exceeded the wall-time budget",
        ) from error
    except AudalignMemoryBudgetError as error:
        _recheck_workspace_after_child(workspace_budget, error)
        raise AlignmentError(
            "alignment_memory_budget_exceeded",
            "audalign recognition exceeded the memory budget",
        ) from error
    except (AudalignCandidateLimitError, AudalignOutputOverflowError) as error:
        _recheck_workspace_after_child(workspace_budget, error)
        return {"classifications": "uncertain"}
    except AudalignAdapterError as error:
        _recheck_workspace_after_child(workspace_budget, error)
        return {
            "classifications": "failed",
            "error_code": "auxiliary_recognition_failed",
        }
    _recheck_workspace_after_child(workspace_budget)
    if not match.candidates:
        return {"classifications": "uncertain"}
    # one single copy of the upstream evidence, shared by mapped and conflict
    upstream_counts = tuple(match.raw_matching_fingerprint_counts)
    candidates = tuple(
        CandidateOffset(
            b_ticks=source_relation_b(0, 0, candidate.offset_seconds),
            raw_seconds_text=candidate.offset_seconds,
            confidence=candidate.confidence,
            upstream_index=index,
        )
        for index, candidate in enumerate(match.candidates)
    )
    groups = group_candidates(candidates)
    verifier = FixedOffsetVerifier(
        alignment_python=alignment_python,
        ffmpeg_command=ffmpeg.command,
        workspace=pair_workspace,
        deadline=deadline,
        child_budget=child_budget,
        workspace_budget=workspace_budget,
    )
    verified_groups: list[tuple[CandidateOffset, int]] = []
    for group in groups:
        if len(group) == 0:
            continue
        # each group is verified in audalign's original return order; the
        # first candidate that passes all three windows is the group relation
        selected: CandidateOffset | None = None
        selected_error: int | None = None
        for candidate in group:
            # effective overlap in main time: aux_tick = main_tick - B
            main_overlap_start = max(0, candidate.b_ticks)
            main_overlap_end = min(
                main_duration, aux_duration + candidate.b_ticks
            )
            aux_overlap_start = main_overlap_start - candidate.b_ticks
            aux_overlap_end = main_overlap_end - candidate.b_ticks
            if (
                main_overlap_end - main_overlap_start
                < ALIGNMENT_MINIMUM_VERIFIED_OVERLAP_TICKS
            ):
                continue
            try:
                verification = verifier.verify(
                    main_wav,
                    aux_wav,
                    candidate_b_ticks=candidate.b_ticks,
                    main_overlap_start_ticks=main_overlap_start,
                    main_overlap_end_ticks=main_overlap_end,
                    auxiliary_overlap_start_ticks=aux_overlap_start,
                    auxiliary_overlap_end_ticks=aux_overlap_end,
                )
            except (AudalignTimeBudgetError, FFmpegAlignmentTimeBudgetError) as error:
                raise AlignmentError(
                    "alignment_time_budget_exceeded",
                    "window verification exceeded the wall-time budget",
                ) from error
            except (
                AudalignMemoryBudgetError,
                FFmpegAlignmentMemoryBudgetError,
            ) as error:
                raise AlignmentError(
                    "alignment_memory_budget_exceeded",
                    "window verification exceeded the memory budget",
                ) from error
            except FFmpegAlignmentBudgetError:
                return {
                    "classifications": "failed",
                    "error_code": "auxiliary_verification_failed",
                }
            except (
                AudalignCandidateLimitError,
                AudalignOutputOverflowError,
            ) as error:
                _recheck_workspace_after_child(workspace_budget, error)
                return {"classifications": "uncertain"}
            except (AudalignAdapterError, FFmpegAlignmentError):
                return {
                    "classifications": "failed",
                    "error_code": "auxiliary_verification_failed",
                }
            if verification.get("passed") is True:
                selected = candidate
                selected_error = int(
                    verification["max_local_offset_error_ticks"]  # type: ignore[call-overload]
                )
                break
        if selected is None:
            continue
        assert selected_error is not None
        verified_groups.append((selected, selected_error))
    if len(verified_groups) == 0:
        return {"classifications": "uncertain"}
    if len(verified_groups) >= 2:
        # pair conflict: the single upstream matching_fingerprint_counts copy
        # is saved once (never duplicated), the raw count is its true length,
        # the verification window count is the contract value 3, and the max
        # error is the maximum over the verified groups
        return {
            "classifications": "conflict",
            "raw_candidate_count": len(upstream_counts),
            "matching_fingerprint_counts": list(upstream_counts),
            "verification_window_count": ALIGNMENT_VERIFICATION_WINDOW_COUNT,
            "max_local_offset_error_ticks": max(
                error for _candidate, error in verified_groups
            ),
        }
    candidate, max_error = verified_groups[0]
    return {
        "classifications": "mapped",
        "b_ticks": candidate.b_ticks,
        "raw_candidate_count": len(upstream_counts),
        "matching_fingerprint_counts": list(upstream_counts),
        "max_local_offset_error_ticks": max_error,
    }


def _align_one_pair_profile2(
    main_source_id: str,
    main_duration: int,
    main_wav: Path,
    aux_source_id: str,
    aux_duration: int,
    aux_wav: Path,
    camera_id: str,
    alignment_python: Path,
    ffmpeg: ToolResolution,
    workspace: Path,
    deadline: _Deadline,
    memory_limit: int,
    child_budget: ChildBudget,
    workspace_budget: _WorkspaceBudget,
    channel: str | None = None,
) -> dict[str, object]:
    """Execute profile 2 recall admission and bounded verification.

    Recall always cuts a 15-second auxiliary excerpt and compares it with the
    same complete main WAV. Only upstream candidate zero is admitted. The
    selected candidates are converted to source-global B before grouping;
    each group gets exactly one representative and exactly one three-window
    verification attempt.
    """
    pair_workspace = workspace / f"pair-{main_source_id}-{aux_source_id}"
    pair_workspace.mkdir(parents=True, exist_ok=True)
    starts = profile2_probe_starts(aux_duration)
    if not starts:
        return {"classifications": "uncertain"}
    if len(starts) > PROFILE2_MAX_RECALL_PROBES_PER_CHANNEL:
        return {"classifications": "uncertain"}

    admitted: list[CandidateOffset] = []
    all_upstream_counts: list[int] = []
    for probe_index, auxiliary_start in enumerate(starts):
        profile2_call_plan(recall_calls=probe_index + 1, verification_calls=0)
        target = pair_workspace / (
            f"target-{channel or 'mono'}-probe-{probe_index}.wav"
        )
        workspace_budget.reserve_decode(
            duration_ticks=PROFILE2_RECALL_EXCERPT_LENGTH_TICKS
        )
        try:
            extract_wav_window(
                aux_wav,
                target,
                start_ticks=auxiliary_start,
                end_ticks=auxiliary_start + PROFILE2_RECALL_EXCERPT_LENGTH_TICKS,
                ffmpeg_command=ffmpeg.command,
                timeout_seconds=deadline.remaining(),
                budget=child_budget,
            )
        except FFmpegAlignmentTimeBudgetError as error:
            _recheck_workspace_after_child(workspace_budget, error)
            raise AlignmentError(
                "alignment_time_budget_exceeded",
                "profile 2 recall exceeded the wall-time budget",
            ) from error
        except FFmpegAlignmentMemoryBudgetError as error:
            _recheck_workspace_after_child(workspace_budget, error)
            raise AlignmentError(
                "alignment_memory_budget_exceeded",
                "profile 2 recall exceeded the memory budget",
            ) from error
        except FFmpegAlignmentBudgetError as error:
            _recheck_workspace_after_child(workspace_budget, error)
            raise AlignmentError(
                "alignment_disk_budget_exceeded",
                "profile 2 recall exceeded the child or disk budget",
            ) from error
        except FFmpegAlignmentError as error:
            _recheck_workspace_after_child(workspace_budget, error)
            return {
                "classifications": "failed",
                "error_code": "auxiliary_recognition_failed",
            }
        _recheck_workspace_after_child(workspace_budget)

        output = pair_workspace / (
            f"recall-{channel or 'mono'}-probe-{probe_index}.json"
        )
        workspace_budget.reserve_bytes(
            size_bytes=AUDALIGN_WORKER_MAX_OUTPUT_BYTES
        )
        try:
            match = run_audalign_recognize(
                alignment_python,
                target,
                main_wav,
                output,
                timeout_seconds=deadline.remaining(),
                budget=child_budget,
                max_raw_candidates=PROFILE2_MAX_RAW_CANDIDATES_PER_PROBE,
            )
        except AudalignTimeBudgetError as error:
            _recheck_workspace_after_child(workspace_budget, error)
            raise AlignmentError(
                "alignment_time_budget_exceeded",
                "profile 2 recall exceeded the wall-time budget",
            ) from error
        except AudalignMemoryBudgetError as error:
            _recheck_workspace_after_child(workspace_budget, error)
            raise AlignmentError(
                "alignment_memory_budget_exceeded",
                "profile 2 recall exceeded the memory budget",
            ) from error
        except AudalignBudgetError as error:
            _recheck_workspace_after_child(workspace_budget, error)
            raise AlignmentError(
                "alignment_disk_budget_exceeded",
                "profile 2 recall exceeded the child budget",
            ) from error
        except (AudalignCandidateLimitError, AudalignOutputOverflowError) as error:
            _recheck_workspace_after_child(workspace_budget, error)
            return {"classifications": "uncertain"}
        except AudalignAdapterError as error:
            _recheck_workspace_after_child(workspace_budget, error)
            return {
                "classifications": "failed",
                "error_code": "auxiliary_recognition_failed",
            }
        _recheck_workspace_after_child(workspace_budget)
        raw_count = len(match.candidates)
        if raw_count > PROFILE2_MAX_RAW_CANDIDATES_PER_PROBE:
            return {"classifications": "uncertain"}
        if raw_count == 0:
            continue
        # Matching counts are preserved in upstream response order. They are
        # evidence only; admission is determined solely by upstream index 0.
        all_upstream_counts.extend(match.raw_matching_fingerprint_counts)
        if PROFILE2_MAX_SELECTED_CANDIDATES_PER_PROBE != 1:
            raise AlignmentProfileError("profile 2 admission is not closed")
        candidate = match.candidates[0]
        admitted.append(
            CandidateOffset(
                b_ticks=source_relation_b(
                    0, auxiliary_start, candidate.offset_seconds
                ),
                raw_seconds_text=candidate.offset_seconds,
                confidence=candidate.confidence,
                upstream_index=0,
                probe_index=probe_index,
            )
        )

    if not admitted:
        return {"classifications": "uncertain"}
    groups = group_profile2_hypotheses(tuple(admitted))
    if len(groups) > PROFILE2_MAX_CANDIDATE_GROUPS_PER_CHANNEL:
        return {"classifications": "uncertain"}

    verifier = FixedOffsetVerifier(
        alignment_python=alignment_python,
        ffmpeg_command=ffmpeg.command,
        workspace=pair_workspace,
        deadline=deadline,
        child_budget=child_budget,
        workspace_budget=workspace_budget,
    )
    verified: list[tuple[CandidateOffset, int]] = []
    verification_calls = 0
    for group in groups:
        representative = group[0]
        verification_calls += ALIGNMENT_VERIFICATION_WINDOW_COUNT
        profile2_call_plan(
            recall_calls=len(starts), verification_calls=verification_calls
        )
        main_overlap_start = max(0, representative.b_ticks)
        main_overlap_end = min(
            main_duration, aux_duration + representative.b_ticks
        )
        auxiliary_overlap_start = main_overlap_start - representative.b_ticks
        auxiliary_overlap_end = main_overlap_end - representative.b_ticks
        if (
            main_overlap_end - main_overlap_start
            < ALIGNMENT_MINIMUM_VERIFIED_OVERLAP_TICKS
        ):
            continue
        try:
            result = verifier.verify(
                main_wav,
                aux_wav,
                candidate_b_ticks=representative.b_ticks,
                main_overlap_start_ticks=main_overlap_start,
                main_overlap_end_ticks=main_overlap_end,
                auxiliary_overlap_start_ticks=auxiliary_overlap_start,
                auxiliary_overlap_end_ticks=auxiliary_overlap_end,
            )
        except (AudalignTimeBudgetError, FFmpegAlignmentTimeBudgetError) as error:
            raise AlignmentError(
                "alignment_time_budget_exceeded",
                "profile 2 verification exceeded the wall-time budget",
            ) from error
        except (
            AudalignMemoryBudgetError,
            FFmpegAlignmentMemoryBudgetError,
        ) as error:
            raise AlignmentError(
                "alignment_memory_budget_exceeded",
                "profile 2 verification exceeded the memory budget",
            ) from error
        except (AudalignBudgetError, FFmpegAlignmentBudgetError) as error:
            raise AlignmentError(
                "alignment_disk_budget_exceeded",
                "profile 2 verification exceeded the child or disk budget",
            ) from error
        except (
            AudalignCandidateLimitError,
            AudalignOutputOverflowError,
        ) as error:
            _recheck_workspace_after_child(workspace_budget, error)
            return {"classifications": "uncertain"}
        except (AudalignAdapterError, FFmpegAlignmentError):
            # The representative is not replaced by another raw candidate;
            # an adapter/decode failure is a pair execution failure.
            return {
                "classifications": "failed",
                "error_code": "auxiliary_verification_failed",
            }
        if result.get("passed") is True:
            verified.append(
                (
                    representative,
                    int(result["max_local_offset_error_ticks"]),  # type: ignore[call-overload]
                )
            )

    if not verified:
        return {"classifications": "uncertain"}
    evidence = {
        "raw_candidate_count": len(all_upstream_counts),
        "matching_fingerprint_counts": list(all_upstream_counts),
        "verification_window_count": ALIGNMENT_VERIFICATION_WINDOW_COUNT,
        "max_local_offset_error_ticks": max(error for _item, error in verified),
    }
    if len(verified) > 1:
        return {"classifications": "conflict", **evidence}
    return {
        "classifications": "mapped",
        "b_ticks": verified[0][0].b_ticks,
        **evidence,
    }


def _align_one_pair_mono_first(
    main_source_id: str,
    main_duration: int,
    main_wav: Path,
    main_source_path: Path,
    aux_source_id: str,
    aux_duration: int,
    aux_source_path: Path,
    main_source_asset: SourceAsset,
    aux_source_asset: SourceAsset,
    camera_id: str,
    alignment_python: Path,
    ffmpeg: ToolResolution,
    workspace: Path,
    deadline: _Deadline,
    memory_limit: int,
    child_budget: ChildBudget,
    workspace_budget: _WorkspaceBudget,
    frozen_identity: dict[str, dict[str, object]],
) -> dict[str, object]:
    pair_workspace = workspace / f"pair-{main_source_id}-{aux_source_id}"
    try:
        return _align_one_pair_mono_first_in_workspace(
            main_source_id,
            main_duration,
            main_wav,
            main_source_path,
            aux_source_id,
            aux_duration,
            aux_source_path,
            main_source_asset,
            aux_source_asset,
            camera_id,
            alignment_python,
            ffmpeg,
            workspace,
            deadline,
            memory_limit,
            child_budget,
            workspace_budget,
            frozen_identity,
        )
    finally:
        shutil.rmtree(pair_workspace, ignore_errors=True)


def _align_one_pair_mono_first_in_workspace(
    main_source_id: str,
    main_duration: int,
    main_wav: Path,
    main_source_path: Path,
    aux_source_id: str,
    aux_duration: int,
    aux_source_path: Path,
    main_source_asset: SourceAsset,
    aux_source_asset: SourceAsset,
    camera_id: str,
    alignment_python: Path,
    ffmpeg: ToolResolution,
    workspace: Path,
    deadline: _Deadline,
    memory_limit: int,
    child_budget: ChildBudget,
    workspace_budget: _WorkspaceBudget,
    frozen_identity: dict[str, dict[str, object]],
) -> dict[str, object]:
    """Mono-first recognition with full L/R fallback and conflict closing.

    The prepared main mono WAV is reused; only the auxiliary mono is decoded
    here. When mono cannot form a unique safe mapping, the complete L/R
    fallback (both channels) runs before any classification: every mapped L/R
    result must land in the same 12000-tick B group to be mapped, and two
    inconsistent verified B relations close as conflict.
    """
    pair_workspace = workspace / f"pair-{main_source_id}-{aux_source_id}"
    pair_workspace.mkdir(parents=True, exist_ok=True)
    # Profile 2 refuses a short auxiliary before decoding or starting any
    # Audalign/FFmpeg recall worker; no partial excerpt is used as a guess.
    if not profile2_probe_starts(aux_duration):
        return {"classifications": "uncertain"}
    aux_mono = pair_workspace / "aux-mono.wav"
    workspace_budget.reserve_decode(duration_ticks=aux_duration)
    # every read of a user source is preceded by a frozen basis check;
    # workspace WAVs already decoded are not re-checked
    _assert_source_matches_frozen(
        aux_source_path, aux_source_asset, frozen_identity, aux_source_id
    )
    try:
        decode_alignment_audio(
            aux_source_path,
            aux_mono,
            ffmpeg_command=ffmpeg.command,
            ffmpeg_version=ffmpeg.version,
            timeout_seconds=deadline.remaining(),
            budget=child_budget,
        )
    except FFmpegAlignmentTimeBudgetError:
        raise AlignmentError(
            "alignment_time_budget_exceeded",
            "auxiliary audio decode exceeded the wall-time budget",
        )
    except FFmpegAlignmentMemoryBudgetError:
        raise AlignmentError(
            "alignment_memory_budget_exceeded",
            "auxiliary audio decode exceeded the memory budget",
        )
    except FFmpegAlignmentBudgetError:
        return {
            "classifications": "failed",
            "error_code": "auxiliary_decode_failed",
        }
    except FFmpegAlignmentError:
        return {
            "classifications": "failed",
            "error_code": "auxiliary_decode_failed",
        }
    workspace_budget.recheck()
    mono_result = _align_one_pair(
        main_source_id,
        main_duration,
        main_wav,
        aux_source_id,
        aux_duration,
        aux_mono,
        camera_id,
        alignment_python,
        ffmpeg,
        workspace,
        deadline,
        memory_limit,
        child_budget,
        channel=None,
        profile2=True,
        workspace_budget=workspace_budget,
    )
    if mono_result["classifications"] == "failed":
        return mono_result
    if mono_result["classifications"] in {"mapped", "conflict"}:
        return mono_result
    # Only a non-failed mono result with zero verified B groups reaches the
    # complete L/R fallback. A mono conflict is final and cannot be covered by
    # a mapped channel result.
    shutil.rmtree(pair_workspace, ignore_errors=True)
    pair_workspace.mkdir(parents=True, exist_ok=True)
    workspace_budget.recheck()
    channel_results: list[dict[str, object]] = []
    for channel in ("left", "right"):
        main_ch = pair_workspace / f"main-{channel}.wav"
        aux_ch = pair_workspace / f"aux-{channel}.wav"
        workspace_budget.reserve_decode(duration_ticks=main_duration)
        _assert_source_matches_frozen(
            main_source_path, main_source_asset, frozen_identity, main_source_id
        )
        try:
            decode_alignment_audio(
                main_source_path,
                main_ch,
                ffmpeg_command=ffmpeg.command,
                ffmpeg_version=ffmpeg.version,
                channel=channel,
                timeout_seconds=deadline.remaining(),
                budget=child_budget,
            )
        except FFmpegAlignmentTimeBudgetError:
            raise AlignmentError(
                "alignment_time_budget_exceeded",
                "channel audio decode exceeded the wall-time budget",
            )
        except FFmpegAlignmentMemoryBudgetError:
            raise AlignmentError(
                "alignment_memory_budget_exceeded",
                "channel audio decode exceeded the memory budget",
            )
        except FFmpegAlignmentBudgetError:
            return {
                "classifications": "failed",
                "error_code": "auxiliary_decode_failed",
            }
        except FFmpegAlignmentError:
            return {
                "classifications": "failed",
                "error_code": "auxiliary_audio_stream_unsupported",
            }
        workspace_budget.recheck()
        workspace_budget.reserve_decode(duration_ticks=aux_duration)
        _assert_source_matches_frozen(
            aux_source_path, aux_source_asset, frozen_identity, aux_source_id
        )
        try:
            decode_alignment_audio(
                aux_source_path,
                aux_ch,
                ffmpeg_command=ffmpeg.command,
                ffmpeg_version=ffmpeg.version,
                channel=channel,
                timeout_seconds=deadline.remaining(),
                budget=child_budget,
            )
        except FFmpegAlignmentTimeBudgetError:
            raise AlignmentError(
                "alignment_time_budget_exceeded",
                "channel audio decode exceeded the wall-time budget",
            )
        except FFmpegAlignmentMemoryBudgetError:
            raise AlignmentError(
                "alignment_memory_budget_exceeded",
                "channel audio decode exceeded the memory budget",
            )
        except FFmpegAlignmentBudgetError:
            return {
                "classifications": "failed",
                "error_code": "auxiliary_decode_failed",
            }
        except FFmpegAlignmentError:
            return {
                "classifications": "failed",
                "error_code": "auxiliary_audio_stream_unsupported",
            }
        workspace_budget.recheck()
        channel_result = _align_one_pair(
            main_source_id,
            main_duration,
            main_ch,
            aux_source_id,
            aux_duration,
            aux_ch,
            camera_id,
            alignment_python,
            ffmpeg,
            workspace,
            deadline,
            memory_limit,
            child_budget,
            channel=channel,
            profile2=True,
            workspace_budget=workspace_budget,
        )
        if channel_result["classifications"] == "failed":
            return channel_result
        channel_results.append(channel_result)
        if channel == "left":
            shutil.rmtree(pair_workspace, ignore_errors=True)
            pair_workspace.mkdir(parents=True, exist_ok=True)
            workspace_budget.recheck()
    mapped_lr = [
        (int(result["b_ticks"]), result)  # type: ignore[call-overload]
        for result in channel_results
        if result["classifications"] == "mapped"
    ]
    if any(
        result["classifications"] == "conflict" for result in channel_results
    ):
        # a verified conflict from either channel is never overridden by the
        # other channel's mapped result
        merged_counts: list[int] = []
        for result in channel_results:
            if result["classifications"] != "conflict":
                continue
            merged_counts.extend(
                int(item)
                for item in result["matching_fingerprint_counts"]  # type: ignore[attr-defined]
            )
        return {
            "classifications": "conflict",
            "raw_candidate_count": len(merged_counts),
            "matching_fingerprint_counts": merged_counts,
            "verification_window_count": ALIGNMENT_VERIFICATION_WINDOW_COUNT,
            "max_local_offset_error_ticks": max(
                int(result["max_local_offset_error_ticks"])  # type: ignore[call-overload]
                for result in channel_results
                if result["classifications"] == "conflict"
            ),
        }
    if not mapped_lr:
        return mono_result
    # all mapped L/R results must support the same B within the group
    # diameter; two inconsistent verified relations close as conflict
    reference = mapped_lr[0][0]
    if any(
        abs(b - reference) > ALIGNMENT_CANDIDATE_GROUP_DIAMETER_TICKS
        for b, _result in mapped_lr
    ):
        counts: list[int] = []
        for _b, result in mapped_lr:
            raw_counts = result["matching_fingerprint_counts"]
            counts.extend(
                int(item)
                for item in raw_counts  # type: ignore[attr-defined]
            )
        return {
            "classifications": "conflict",
            "raw_candidate_count": len(counts),
            "matching_fingerprint_counts": counts,
            "verification_window_count": ALIGNMENT_VERIFICATION_WINDOW_COUNT,
            "max_local_offset_error_ticks": max(
                int(result["max_local_offset_error_ticks"])  # type: ignore[call-overload]
                for _b, result in mapped_lr
            ),
        }
    return mapped_lr[0][1]


def _uncertain_interval(
    camera_id: str,
    interval_id: str,
    main_source_id: str,
    start: int,
    end: int,
) -> AlignmentInterval:
    return AlignmentInterval(
        interval_id=interval_id,
        auxiliary_camera_id=camera_id,
        classification="uncertain",
        main={"source_id": main_source_id, "start_ticks": start, "end_ticks": end},
        auxiliary=None,
        evidence={
            "code": "no_candidate",
            "raw_candidate_count": 0,
            "matching_fingerprint_counts": [],
            "verification_window_count": 0,
            "verification_profile": {
                "name": "roughcut_audalign_fixed_offset",
                "version": ALIGNMENT_WRITE_PROFILE_VERSION,
            },
            "max_local_offset_error_ticks": None,
        },
    )


def _missing_interval(
    camera_id: str,
    interval_id: str,
    main_source_id: str,
    start: int,
    end: int,
) -> AlignmentInterval:
    return AlignmentInterval(
        interval_id=interval_id,
        auxiliary_camera_id=camera_id,
        classification="missing",
        main={"source_id": main_source_id, "start_ticks": start, "end_ticks": end},
        auxiliary=None,
        evidence={
            "code": "no_candidate",
            "raw_candidate_count": 0,
            "matching_fingerprint_counts": [],
            "verification_window_count": 0,
            "verification_profile": {
                "name": "roughcut_audalign_fixed_offset",
                "version": ALIGNMENT_WRITE_PROFILE_VERSION,
            },
            "max_local_offset_error_ticks": None,
        },
    )


def _revalidate_basis(
    root: Path,
    project: Project,
    sources: dict[str, SourceAsset],
    runtime: Any,
    frozen_identity: dict[str, dict[str, object]],
    *,
    require_full_binding: bool = True,
    require_alignment_selection: bool = False,
) -> None:
    """Publish-time revalidation over exactly the requested sources.

    Each requested source is compared field by field against the single
    frozen snapshot created in preflight: current Project revision, the
    Project's import_mode/locator/fingerprint/probe, the frozen physical
    identity (dev/inode/regular/symlink/resolved identity), and the current
    bounded fingerprint. A mismatch is a closed basis change with zero publish.

    The historical Audalign writer additionally pins the whole aggregate
    ``runtime_binding_sha256``. The BBC writer pins FFmpeg/FFprobe plus its
    exact managed alignment selection, whose closure/filesystem validity is
    rechecked here before publish.
    """
    current_project = ProjectStore(root).load()
    if current_project.project_id != project.project_id:
        raise AlignmentError(
            "alignment_basis_changed_during_run",
            "Project identity changed during alignment",
        )
    if current_project.revision != project.revision:
        raise AlignmentError(
            "alignment_basis_changed_during_run",
            "Project revision changed during alignment",
        )
    current_by_id = {source.source_id: source for source in current_project.sources}
    for source_id in sources:
        current = current_by_id.get(source_id)
        if current is None:
            raise AlignmentError(
                "alignment_basis_changed_during_run",
                "requested source is missing from the current Project",
            )
        # the same field-by-field helper used before every decode and before
        # auxiliary processing is reused at publish time
        _assert_source_matches_frozen(
            _resolve_source_path(root, current),
            current,
            frozen_identity,
            source_id,
        )
    binding = _load_persistent_runtime()
    if require_full_binding:
        if binding.runtime_binding_sha256 != runtime.runtime_binding_sha256:
            raise AlignmentError(
                "alignment_runtime_changed_during_run",
                "persistent runtime changed during alignment",
            )
        return
    if (
        binding.ffmpeg_tool_selection_hash != runtime.ffmpeg_tool_selection_hash
        or binding.ffprobe_tool_selection_hash
        != runtime.ffprobe_tool_selection_hash
    ):
        raise AlignmentError(
            "alignment_runtime_changed_during_run",
            "FFmpeg/FFprobe selection changed during alignment",
        )
    if require_alignment_selection:
        current_selection = binding.binding.alignment_python
        frozen = runtime.binding.alignment_python
        try:
            validated = validate_audalign_selection(current_selection)
        except AudalignAdapterError as error:
            raise AlignmentError(
                "alignment_runtime_changed_during_run",
                "Audalign alignment selection became invalid during alignment",
            ) from error
        if frozen is None or validated.to_dict() != frozen.to_dict():
            raise AlignmentError(
                "alignment_runtime_changed_during_run",
                "Audalign alignment selection changed during alignment",
            )


def _resolve_groups(
    root: Path,
    project: Project,
    main_group: AlignmentCameraGroup,
    auxiliary_groups: tuple[AlignmentCameraGroup, ...],
    *,
    selected_source_ids: set[str] | None = None,
) -> dict[str, SourceAsset]:
    """Resolve only the Sources requested by this operation's camera groups.

    Sources that are not part of the request never enter the input hash, the
    resource estimate, or publish-time revalidation.
    """
    requested: set[str] = set()
    for camera in (main_group, *auxiliary_groups):
        for source_id in camera.ordered_source_ids:
            if source_id in requested:
                raise AlignmentError(
                    "alignment_input_stale",
                    "source groups overlap or duplicate a source",
                )
            requested.add(source_id)
    by_id = {source.source_id: source for source in project.sources}
    missing = requested - set(by_id)
    if missing:
        raise AlignmentError(
            "alignment_input_stale",
            "requested source does not exist in the Project",
        )
    selected: dict[str, SourceAsset] = {}
    sources_to_inspect = requested if selected_source_ids is None else selected_source_ids
    if not sources_to_inspect.issubset(requested):
        raise AlignmentError(
            "alignment_input_stale",
            "selected pair Source is outside the declared synchronization group",
        )
    for source_id in sources_to_inspect:
        source = by_id[source_id]
        source_path = _resolve_source_path(root, source)
        _assert_source_unchanged(source_path, source)
        selected[source_id] = source
    return selected


def _assert_source_matches_frozen(
    source_path: Path,
    source: SourceAsset,
    frozen_identity: dict[str, dict[str, object]],
    source_id: str,
) -> None:
    """One local helper comparing the current Source against its frozen
    snapshot field by field: import_mode, locator hash, the three fingerprint
    fields, the probe fields, the physical identity, and the current bounded
    fingerprint. It is called before every main decode, before the first
    processing of every auxiliary Source, and at publish time."""
    snapshot = frozen_identity.get(source_id)
    if snapshot is None:
        raise AlignmentError(
            "alignment_basis_changed_during_run",
            "requested source is missing from the frozen snapshot",
        )
    snapshot_identity = snapshot["identity"]
    assert isinstance(snapshot_identity, dict)
    frozen_fingerprint = snapshot["fingerprint"]
    assert isinstance(frozen_fingerprint, dict)
    frozen_probe = snapshot["probe"]
    assert isinstance(frozen_probe, dict)
    if (
        source.import_mode.value != snapshot["import_mode"]
        or canonical_sha256_v1({"locator": source.locator})
        != snapshot["locator_identity_hash"]
        or source.fingerprint.size != frozen_fingerprint["size"]
        or source.fingerprint.mtime_ns != frozen_fingerprint["mtime_ns"]
        or source.fingerprint.sha256_head_tail
        != frozen_fingerprint["sha256_head_tail"]
        or source.probe.duration_ticks != frozen_probe["duration_ticks"]
        or source.probe.audio_codec != frozen_probe["audio_codec"]
        or source.probe.audio_sample_rate != frozen_probe["audio_sample_rate"]
    ):
        raise AlignmentError(
            "alignment_basis_changed_during_run",
            "requested source metadata differs from the frozen snapshot",
        )
    try:
        current_fingerprint = fingerprint_file(source_path)
    except OSError as error:
        raise AlignmentError(
            "alignment_basis_changed_during_run",
            "source file is missing or unreadable",
        ) from error
    if (
        current_fingerprint.size != frozen_fingerprint["size"]
        or current_fingerprint.mtime_ns != frozen_fingerprint["mtime_ns"]
        or current_fingerprint.sha256_head_tail
        != frozen_fingerprint["sha256_head_tail"]
    ):
        raise AlignmentError(
            "alignment_basis_changed_during_run",
            "source fingerprint differs from the frozen snapshot",
        )
    try:
        current_identity = _source_identity_evidence(source_path)
    except AlignmentError as error:
        if error.code == "alignment_input_stale":
            # a raced identity inspection on an executed-record path is a
            # closed basis change, never an input-stale preflight error
            raise AlignmentError(
                "alignment_basis_changed_during_run",
                "source identity changed during alignment",
            ) from error
        raise
    if snapshot_identity != current_identity:
        raise AlignmentError(
            "alignment_basis_changed_during_run",
            "source identity differs from the frozen snapshot",
        )


def _assert_source_unchanged(source_path: Path, source: SourceAsset) -> None:
    try:
        current = fingerprint_file(source_path)
    except OSError as error:
        raise AlignmentError(
            "alignment_input_stale",
            "source file is missing or unreadable",
        ) from error
    if (
        current.size != source.fingerprint.size
        or current.mtime_ns != source.fingerprint.mtime_ns
        or current.sha256_head_tail != source.fingerprint.sha256_head_tail
    ):
        raise AlignmentError(
            "alignment_input_stale",
            "source fingerprint changed",
        )


def _alignment_fingerprint(
    fingerprint: Any,
) -> AlignmentSourceFingerprint:
    return AlignmentSourceFingerprint(
        size=fingerprint.size,
        mtime_ns=fingerprint.mtime_ns,
        sha256_head_tail=fingerprint.sha256_head_tail,
    )


def _resolve_source_path(root: Path, source: SourceAsset) -> Path:
    from roughcut.application.renders import _resolve_source_path as _resolve

    return _resolve(root, source)


def _validate_budget(name: str, value: int, ceiling: int) -> None:
    """One public budget must be a positive integer within the frozen ceiling."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _preflight_error("alignment_input_stale", f"{name} is not positive")
    if value > ceiling:
        raise _preflight_error(
            "alignment_input_stale",
            f"{name} exceeds the frozen ceiling",
        )


def _validate_alignment_runtime(
    runtime: Any,
    budget: ChildBudget,
    workspace_budget: _WorkspaceBudget | None = None,
) -> None:
    def run(command: list[str]) -> Any:
        try:
            result = run_bounded_child(
                command,
                budget=budget,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as error:
            _recheck_workspace_after_child(workspace_budget, error)
            raise
        _recheck_workspace_after_child(workspace_budget)
        return result

    try:
        verify_runtime_pair(
            ffmpeg_command=runtime.ffmpeg.command,
            ffmpeg_version=runtime.ffmpeg.version,
            ffprobe_command=runtime.ffprobe.command,
            ffprobe_version=runtime.ffprobe.version,
            command_runner=run,
        )
    except ChildProcessTimeBudgetError as error:
        raise AlignmentError(
            "alignment_time_budget_exceeded",
            "FFmpeg runtime drift check exceeded the wall-time budget",
        ) from error
    except ChildProcessMemoryBudgetError as error:
        raise AlignmentError(
            "alignment_memory_budget_exceeded",
            "FFmpeg runtime drift check exceeded the memory budget",
        ) from error
    except (ChildProcessBudgetError, FFmpegRuntimeDriftError) as error:
        raise AlignmentError(
            "alignment_runtime_unavailable",
            "persistent FFmpeg runtime is unavailable",
        ) from error


def _estimate_workspace_bytes(
    main_duration_ticks: tuple[int, ...],
    auxiliary_duration_ticks: tuple[int, ...],
) -> int:
    """Estimate the bounded peak after pair scratch is released eagerly."""
    main_total = sum(main_duration_ticks)
    active_fallback_pair = max(main_duration_ticks) + max(
        auxiliary_duration_ticks
    )
    seconds = (main_total + active_fallback_pair) / TICKS_PER_SECOND
    return int(seconds * ANALYSIS_WORKSPACE_BYTES_PER_SECOND) + (
        ANALYSIS_WORKSPACE_OVERHEAD_BYTES
    )


def _estimate_correlation_workspace_bytes(
    main_duration_ticks: tuple[int, ...],
    auxiliary_duration_ticks: tuple[int, ...],
) -> int:
    """Conservative serial estimate for the correlation bounded probe path.

    Covers: each unique full main decoded once at 44.1 kHz mono PCM16 plus WAV header,
    a single live 15 s auxiliary excerpt (never three at once) plus header, one bounded
    worker output file, and deterministic overhead. Serial execution is mandatory; the
    estimate never assumes three probes live simultaneously. Uses ceiling to avoid
    floating floor underestimation.
    """
    main_bytes = sum(
        _ceil_pcm_wav_bytes(duration_ticks)
        for duration_ticks in main_duration_ticks
    )
    # single live aux probe excerpt (15 s mono PCM16) plus header
    aux_probe_bytes = _ceil_pcm_wav_bytes(
        CORRELATION_RECALL_EXCERPT_LENGTH_TICKS
    )
    # bounded worker JSON output
    worker_bytes = AUDALIGN_WORKER_MAX_OUTPUT_BYTES
    # deterministic overhead for temp directories, small FFmpeg windows, etc.
    overhead = ANALYSIS_WORKSPACE_OVERHEAD_BYTES
    # If there are no auxiliary sources (should fail earlier), just main+overhead
    if not auxiliary_duration_ticks:
        return main_bytes + overhead
    return main_bytes + aux_probe_bytes + worker_bytes + overhead


def _ceil_pcm_wav_bytes(
    duration_ticks: int,
    *,
    sample_rate: int = ANALYSIS_SAMPLE_RATE_HZ,
    sample_width: int = ANALYSIS_SAMPLE_WIDTH_BYTES,
) -> int:
    """Return a deterministic integer ceiling for one PCM WAV output."""
    if duration_ticks < 0 or sample_rate <= 0 or sample_width <= 0:
        raise ValueError("PCM WAV size inputs must be positive")
    numerator = duration_ticks * sample_rate * sample_width
    pcm_bytes = (numerator + ALIGNMENT_TICKS_PER_SECOND - 1) // ALIGNMENT_TICKS_PER_SECOND
    return pcm_bytes + WAV_HEADER_BYTES


def _snapshot_requested_identities(
    root: Path,
    project: Project,
    sources: dict[str, SourceAsset],
    runtime: Any,
) -> dict[str, dict[str, object]]:
    """One frozen identity snapshot per requested Source, created in preflight.

    The same snapshot object feeds the input hash projection, the worker
    startup check, and the publish-time revalidation.
    """
    snapshot: dict[str, dict[str, object]] = {}
    for source_id, source in sources.items():
        source_path = _resolve_source_path(root, source)
        snapshot[source_id] = {
            "source_id": source_id,
            "import_mode": source.import_mode.value,
            "locator_identity_hash": canonical_sha256_v1(
                {"locator": source.locator}
            ),
            "fingerprint": {
                "size": source.fingerprint.size,
                "mtime_ns": source.fingerprint.mtime_ns,
                "sha256_head_tail": source.fingerprint.sha256_head_tail,
            },
            "identity": _source_identity_evidence(source_path),
            "probe": {
                "duration_ticks": source.probe.duration_ticks,
                "audio_codec": source.probe.audio_codec,
                "audio_sample_rate": source.probe.audio_sample_rate,
            },
        }
    return snapshot


def _alignment_input_projection(
    root: Path,
    project: Project,
    request_projection: dict[str, object],
    sources: dict[str, SourceAsset],
    runtime: Any,
    workspace_estimate: int,
    frozen_identity: dict[str, dict[str, object]],
    *,
    profile: dict[str, object] | None = None,
) -> dict[str, object]:
    binding = runtime.binding
    source_basis: dict[str, object] = {}
    for source_id in sources:
        snapshot = frozen_identity[source_id]
        source_basis[source_id] = dict(snapshot)
    if profile is not None:
        # The current Correlation writer binds the persistent FFmpeg/FFprobe
        # selections, Audalign interpreter, component tree, and its canonical
        # profile identity. Historical waveform artifacts retain their own
        # legacy projection and remain available through exact-ID readback.
        projection: dict[str, object] = {
            "input_schema_version": 1,
            "operation_type": "align_multicam",
            "project_id": project.project_id,
            "expected_project_revision": project.revision,
            "request": request_projection,
            "sources": source_basis,
            "ffmpeg_tool_selection_hash": runtime.ffmpeg_tool_selection_hash,
            "ffprobe_tool_selection_hash": runtime.ffprobe_tool_selection_hash,
            "workspace_estimate_bytes": workspace_estimate,
            "profile": profile,
        }
        if profile in (BBC_WRITER_PROFILE, AUDALIGN_CORRELATION_WRITER_PROFILE):
            selection = binding.alignment_python
            projection["alignment_python"] = (
                selection.to_dict() if selection is not None else None
            )
        return projection
    return {
        "input_schema_version": 1,
        "operation_type": "align_multicam",
        "project_id": project.project_id,
        "expected_project_revision": project.revision,
        "request": request_projection,
        "sources": source_basis,
        "runtime_binding_sha256": runtime.runtime_binding_sha256,
        "alignment_python": (
            binding.alignment_python.to_dict()
            if binding.alignment_python
            else None
        ),
        "ffmpeg_tool_selection_hash": runtime.ffmpeg_tool_selection_hash,
        "ffprobe_tool_selection_hash": runtime.ffprobe_tool_selection_hash,
        "workspace_estimate_bytes": workspace_estimate,
        # The exact detached object is part of the producer input-hash
        # preimage. Historical direct callers retain profile 2; the public
        # writer passes the independent waveform identity.
        "profile": (
            frozen_profile(ALIGNMENT_WRITE_PROFILE_VERSION)
            if profile is None
            else profile
        ),
    }


def _classify_worker_error(error: Exception, phase: str) -> str:
    # child budget faults keep their own closed codes; they must never be
    # absorbed by the generic disk classifier
    if isinstance(error, ChildProcessTimeBudgetError):
        return "alignment_time_budget_exceeded"
    if isinstance(error, ChildProcessMemoryBudgetError):
        return "alignment_memory_budget_exceeded"
    if isinstance(error, (ChildProcessBudgetError, AudalignBudgetError)):
        return "alignment_disk_budget_exceeded"
    if isinstance(error, FFmpegAlignmentError):
        if phase == "alignment_decoding_main":
            return "alignment_main_decode_failed"
        return "alignment_disk_budget_exceeded"
    if isinstance(error, AudalignAdapterError):
        return "alignment_main_index_failed"
    return "alignment_disk_budget_exceeded"


_ALIGNMENT_PHASE_ACTIONS = {
    "alignment_decoding_main": "decode_alignment_audio",
    "alignment_indexing_main": "index_main_fingerprint",
    "alignment_processing_auxiliary": "recognize_auxiliary",
    "alignment_revalidating_basis": "revalidate_alignment_basis",
    "alignment_publishing": "publish_alignment_artifact",
}


def _record_alignment_failure(
    store: MediaOperationStore,
    active: MediaOperationRecord,
    code: str,
) -> None:
    if code not in ALIGNMENT_TERMINAL_CODES:
        code = "alignment_disk_budget_exceeded"
    # the failure action follows the active phase exactly; an unknown phase
    # falls back to the publish action
    action = _ALIGNMENT_PHASE_ACTIONS.get(
        active.phase_message_code, "publish_alignment_artifact"
    )
    store.write_locked(
        _terminal_record(
            active,
            status="failed",
            failure=MediaOperationFailure(
                code=code,
                responsibility="roughcut_core",
                action=action,
                message_code="alignment_failed",
            ),
        )
    )


def _record_alignment_interruption(
    store: MediaOperationStore,
    active: MediaOperationRecord,
) -> None:
    store.write_locked(
        _terminal_record(
            active,
            status="interrupted",
            failure=MediaOperationFailure(
                code="alignment_interrupted",
                responsibility="host",
                action="interrupt_media_operation",
                message_code="alignment_interrupted",
            ),
        )
    )


def _preflight_error(code: str, message: str) -> MediaOperationError:
    if code not in ALIGNMENT_PREFLIGHT_CODES:
        code = "alignment_input_stale"
    return MediaOperationError(code, message)


def _project_context(
    project_path: Path,
) -> tuple[Path, Project, MediaOperationStore]:
    from roughcut.application.media_operations import _project_context as _ctx

    return _ctx(project_path)


def _require_same_request(
    record: MediaOperationRecord,
    acceptable_request_hashes: frozenset[str],
) -> None:
    if (
        record.operation_type != "align_multicam"
        or record.request_hash not in acceptable_request_hashes
    ):
        raise MediaOperationError(
            "operation_input_conflict",
            "Roughcut alignment operation refused the same ID "
            "with a different stable request",
        )


def _read_live_record(
    store: MediaOperationStore,
    operation_id: str,
    acceptable_request_hashes: frozenset[str],
) -> MediaOperationRecord:
    current = store.read(operation_id, allow_writer_temp=True)
    if current is None:
        raise MediaOperationError(
            "operation_integrity_error",
            "Roughcut alignment operation found a writer without a record",
        )
    _require_same_request(current, acceptable_request_hashes)
    return current


def _new_pending_record(
    operation_id: str,
    scope: ProjectOperationScope,
    request_hash: str,
    input_hash: str,
) -> MediaOperationRecord:
    now = _now()
    return MediaOperationRecord(
        operation_id=operation_id,
        scope=scope,
        operation_type="align_multicam",
        request_hash=request_hash,
        input_hash=input_hash,
        status="pending",
        phase_message_code="alignment_preparing",
        created_at=now,
        started_at=None,
        updated_at=now,
        finished_at=None,
        result_ref=None,
        error=None,
        schema_version=2,
    )


def _running_record(
    record: MediaOperationRecord,
    phase: str,
) -> MediaOperationRecord:
    now = _now()
    return replace(
        record,
        status="running",
        phase_message_code=phase,
        started_at=record.started_at or now,
        updated_at=now,
    )


def _phase_record(
    record: MediaOperationRecord,
    phase: str,
) -> MediaOperationRecord:
    if phase not in RUNNING_PHASES[record.operation_type]:
        raise MediaOperationError(
            "operation_transition_not_allowed",
            "Roughcut alignment operation rejected an unknown phase",
        )
    return replace(
        record,
        phase_message_code=phase,
        updated_at=_now(),
    )


def _terminal_record(
    record: MediaOperationRecord,
    *,
    status: str,
    result: AlignmentOperationResult | None = None,
    failure: MediaOperationFailure | None = None,
) -> MediaOperationRecord:
    now = _now()
    if status == "succeeded":
        phase = "alignment_succeeded"
    elif status == "failed":
        phase = "alignment_failed"
    elif status == "interrupted":
        phase = "alignment_interrupted"
    else:
        raise MediaOperationError(
            "operation_transition_not_allowed",
            "Roughcut alignment operation rejected a nonterminal status",
        )
    return replace(
        record,
        status=status,  # type: ignore[arg-type]
        phase_message_code=phase,
        updated_at=now,
        finished_at=now,
        result_ref=result,
        error=failure,
    )


class _Deadline:
    def __init__(self, maximum_seconds: int) -> None:
        self.started = datetime.now(UTC).timestamp()
        self.maximum_seconds = maximum_seconds

    def remaining(self) -> float:
        remaining = self.maximum_seconds - (
            datetime.now(UTC).timestamp() - self.started
        )
        if remaining <= 0:
            raise AlignmentError(
                "alignment_time_budget_exceeded",
                "alignment wall-time budget exceeded",
            )
        return remaining


class _WorkspaceBudget:
    """Real disk budget over the analysis workspace.

    The memory budget is enforced per child through the process-tree RSS
    monitor; no pseudo-estimate is used. The workspace root is constrained as
    the child TMPDIR so transient artifacts stay inside the audited tree.
    """

    def __init__(self, root: Path, max_disk_bytes: int) -> None:
        self.root = Path(root)
        self.max_disk_bytes = max_disk_bytes
        self._observed_disk = 0

    def check_disk(self) -> None:
        used = self._used_bytes()
        if used > self.max_disk_bytes:
            raise AlignmentError(
                "alignment_disk_budget_exceeded",
                "alignment workspace disk budget exceeded",
            )
        self._observed_disk = max(self._observed_disk, used)

    def reserve_decode(
        self,
        *,
        duration_ticks: int,
        sample_rate: int = ANALYSIS_SAMPLE_RATE_HZ,
        sample_width: int = ANALYSIS_SAMPLE_WIDTH_BYTES,
    ) -> None:
        """Reserve the predictable WAV decode size before the child runs.

        The reservation fails before the decode starts, so no step writes
        past the requested ceiling and then reports failure.
        """
        predicted = _ceil_pcm_wav_bytes(
            duration_ticks,
            sample_rate=sample_rate,
            sample_width=sample_width,
        )
        self.reserve_bytes(size_bytes=predicted)

    def reserve_bytes(self, *, size_bytes: int) -> None:
        if (
            isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or size_bytes <= 0
        ):
            raise AlignmentError(
                "alignment_disk_budget_exceeded",
                "predicted workspace output size is invalid",
            )
        used = self._used_bytes()
        if used + size_bytes > self.max_disk_bytes:
            raise AlignmentError(
                "alignment_disk_budget_exceeded",
                "predicted workspace output exceeds the disk budget",
            )
        self._observed_disk = max(self._observed_disk, used + size_bytes)

    def apply_tmpdir(self, environment: dict[str, str]) -> dict[str, str]:
        environment = dict(environment)
        environment["TMPDIR"] = str(self.root)
        if os.name == "nt":
            environment["TEMP"] = str(self.root)
            environment["TMP"] = str(self.root)
        return environment

    def recheck(self) -> None:
        """Whole-workspace recheck after every child returns."""
        self.check_disk()

    def _used_bytes(self) -> int:
        total = 0
        if not self.root.exists():
            return 0
        for path in self.root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        return total


def _source_identity_evidence(source_path: Path) -> dict[str, object]:
    """Capture resolved dev/inode/regular/symlink evidence for input_hash."""
    try:
        details = os.lstat(source_path)
        resolved = source_path.resolve(strict=True)
        resolved_details = os.stat(resolved)
    except OSError as error:
        raise AlignmentError(
            "alignment_input_stale",
            "source identity could not be inspected",
        ) from error
    return {
        "device": details.st_dev,
        "inode": details.st_ino,
        "regular": stat.S_ISREG(details.st_mode),
        "symlink": stat.S_ISLNK(details.st_mode),
        "resolved_device": resolved_details.st_dev,
        "resolved_inode": resolved_details.st_ino,
        "resolved_regular": stat.S_ISREG(resolved_details.st_mode),
    }


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
