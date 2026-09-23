"""Durable post-adopt continuation for the confirmed multicam setup."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from roughcut.adapters.alignment_store import AlignmentStore
from roughcut.adapters.media_operation_store import MediaOperationStore
from roughcut.adapters.project_store import ProjectStore
from roughcut.adapters.workflow_store import WorkflowStore
from roughcut.application.alignments import (
    ALIGNMENT_DISK_CEILING_BYTES,
    ALIGNMENT_MEMORY_CEILING_BYTES,
    ALIGNMENT_TIME_CEILING_SECONDS,
    persist_alignment_preflight_failure,
    run_align_multicam,
)
from roughcut.application.media_operations import media_operation_status
from roughcut.domain.alignment import (
    AUDALIGN_CORRELATION_ALGORITHM_NAME,
    AUDALIGN_CORRELATION_ALGORITHM_VERSION,
    AUDALIGN_CORRELATION_PROFILE_NAME,
    AUDALIGN_CORRELATION_PROFILE_VERSION,
    AUDALIGN_CORRELATION_UPSTREAM_COMMIT,
    AUDALIGN_CORRELATION_WRITER_PROFILE,
    AUDALIGN_CORRELATION_WRITER_PROFILE_SHA256,
    BBC_PROFILE_NAME,
    BBC_PROFILE_VERSION,
    BBC_WRITER_PROFILE,
    AlignmentError,
    alignment_request_projection,
    hash_alignment_request,
)
from roughcut.domain.errors import WorkflowError
from roughcut.domain.media_operation import (
    AlignmentOperationResult,
    MediaOperationFailure,
    MediaOperationRecord,
)
from roughcut.domain.workflow import (
    ArtifactRef,
    MulticamAlignmentContinuation,
    MulticamSetup,
    WorkflowRun,
    canonical_sha256_v1,
    validate_safe_id,
)
from roughcut.m2_7_public_capability import require_m2_7_public_capability

# Automatic adoption uses one Core-owned budget tuple. These equal the existing
# align_multicam ceilings and are included in the exact request hash.
AUTOMATIC_ALIGNMENT_MAX_TEMPORARY_DISK_BYTES = ALIGNMENT_DISK_CEILING_BYTES
AUTOMATIC_ALIGNMENT_MAX_ANALYSIS_MEMORY_BYTES = ALIGNMENT_MEMORY_CEILING_BYTES
AUTOMATIC_ALIGNMENT_MAX_RUNTIME_SECONDS = ALIGNMENT_TIME_CEILING_SECONDS
BBC_WRITER_PROFILE_SHA256 = canonical_sha256_v1(BBC_WRITER_PROFILE)


def _workflow_error(evidence: str) -> WorkflowError:
    return WorkflowError(
        "workflow_integrity_error",
        f"Roughcut multicam continuation rejected durable state: {evidence}",
    )


def _alignment_runtime_is_released() -> bool:
    try:
        require_m2_7_public_capability("align_multicam")
    except AlignmentError as error:
        if error.code == "alignment_runtime_unavailable":
            return False
        raise
    return True


def _setup_alignment_groups(
    setup: MulticamSetup,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    main_camera = setup.main_camera.to_dict()
    auxiliary_cameras: list[dict[str, object]] = []
    for camera in setup.auxiliary_cameras:
        group = camera.to_dict()
        pairs = [
            pair.to_dict()
            for pair in setup.source_pairs
            if pair.auxiliary_source_id in camera.ordered_source_ids
        ]
        if pairs:
            group["source_pairs"] = pairs
        auxiliary_cameras.append(group)
    return main_camera, auxiliary_cameras


def _continuation_ids(
    run: WorkflowRun,
    decision_ref: ArtifactRef,
    setup: MulticamSetup,
    setup_hash: str,
    *,
    writer_profile_name: str,
    writer_profile_version: int,
    writer_profile_hash: str,
) -> tuple[str, str]:
    identity = canonical_sha256_v1(
        {
            "schema_version": 1,
            "run_id": run.run_id,
            "adopted_decision_ref": decision_ref.to_dict(),
            "multicam_setup_id": setup.setup_id,
            "multicam_setup_hash": setup_hash,
            "writer_profile_name": writer_profile_name,
            "writer_profile_version": writer_profile_version,
            "writer_profile_hash": writer_profile_hash,
        }
    )
    return f"op_mc_{identity[:32]}", f"aln_mc_{identity[:32]}"


def _validate_request_context(
    project_path: Path,
    run: WorkflowRun,
    continuation: MulticamAlignmentContinuation,
    setup: MulticamSetup,
    *,
    validate_project_revision: bool = True,
) -> None:
    if (
        continuation.requirement != "required"
        or continuation.operation_id is None
        or continuation.alignment_id is None
        or continuation.request_hash is None
        or continuation.setup_id is None
        or continuation.setup_hash is None
    ):
        raise _workflow_error("required continuation is missing alignment identity")
    if setup.project_id != run.project_id or setup.workflow_run_id != run.run_id:
        raise _workflow_error("continuation setup does not belong to its WorkflowRun")
    decision_ref = run.artifact_refs.get("decision")
    if decision_ref is None or continuation.adopted_decision_ref != decision_ref:
        raise _workflow_error("continuation Decision ref does not match the adopted Decision")
    if continuation.setup_id != setup.setup_id:
        raise _workflow_error("continuation setup ID does not match the durable setup")
    if continuation.setup_hash != canonical_sha256_v1(setup.to_dict()):
        raise _workflow_error("continuation setup hash does not match the durable setup")
    project = ProjectStore(project_path).load()
    if project.project_id != run.project_id:
        raise _workflow_error("continuation Project scope does not match the WorkflowRun")
    if validate_project_revision and project.revision != continuation.expected_revision:
        raise _workflow_error("continuation expected revision does not match the Project")


def _validate_continuation_identity(
    project_path: Path,
    run: WorkflowRun,
    continuation: MulticamAlignmentContinuation,
    setup: MulticamSetup,
    *,
    writer_profile_name: str,
    writer_profile_version: int,
    writer_profile_hash: str,
) -> None:
    _validate_request_context(project_path, run, continuation, setup)
    assert continuation.setup_hash is not None
    assert continuation.operation_id is not None
    assert continuation.alignment_id is not None
    decision_ref = run.artifact_refs["decision"]
    assert decision_ref is not None
    expected_operation_id, expected_alignment_id = _continuation_ids(
        run,
        decision_ref,
        setup,
        continuation.setup_hash,
        writer_profile_name=writer_profile_name,
        writer_profile_version=writer_profile_version,
        writer_profile_hash=writer_profile_hash,
    )
    if (
        continuation.operation_id != expected_operation_id
        or continuation.alignment_id != expected_alignment_id
    ):
        raise _workflow_error("continuation operation identity does not match its frozen setup")


def _require_continuation_profile(
    continuation: MulticamAlignmentContinuation,
    *,
    writer_profile_name: str,
    writer_profile_version: int,
    writer_profile_hash: str,
    description: str,
) -> None:
    if (
        continuation.writer_profile_name != writer_profile_name
        or continuation.writer_profile_version != writer_profile_version
        or continuation.writer_profile_hash != writer_profile_hash
    ):
        raise _workflow_error(description)


def _request_facts(
    project_path: Path,
    run: WorkflowRun,
    continuation: MulticamAlignmentContinuation,
    setup: MulticamSetup,
    writer_profile: dict[str, object],
    *,
    expected_request_hash: str | None = None,
    validate_project_revision: bool = True,
) -> tuple[dict[str, object], str]:
    _validate_request_context(
        project_path,
        run,
        continuation,
        setup,
        validate_project_revision=validate_project_revision,
    )
    assert continuation.operation_id is not None
    assert continuation.alignment_id is not None
    main_camera, auxiliary_cameras = _setup_alignment_groups(setup)
    scope: dict[str, object] = {
        key: value
        for key, value in MediaOperationStore(project_path, run.project_id).scope.to_dict().items()
    }
    projection = alignment_request_projection(
        scope=scope,
        operation_id=continuation.operation_id,
        alignment_id=continuation.alignment_id,
        expected_revision=continuation.expected_revision,
        main_camera=main_camera,
        auxiliary_cameras=auxiliary_cameras,
        main_audio_stable=continuation.main_audio_stable,
        max_temporary_disk_bytes=AUTOMATIC_ALIGNMENT_MAX_TEMPORARY_DISK_BYTES,
        max_analysis_memory_bytes=AUTOMATIC_ALIGNMENT_MAX_ANALYSIS_MEMORY_BYTES,
        max_runtime_seconds=AUTOMATIC_ALIGNMENT_MAX_RUNTIME_SECONDS,
        writer_profile=writer_profile,
    )
    request_hash = hash_alignment_request(projection)
    if expected_request_hash is not None and request_hash != expected_request_hash:
        raise _workflow_error("automatic alignment request identity changed")
    return projection, request_hash


def _request_facts_without_writer_profile(
    projection: dict[str, object],
) -> dict[str, object]:
    return {
        key: value
        for key, value in projection.items()
        if key not in {"request_schema_version", "writer_profile"}
    }


def build_multicam_alignment_continuation(
    project_path: Path,
    project_id: str,
    run: WorkflowRun,
    decision_ref: ArtifactRef,
    expected_revision: int,
) -> MulticamAlignmentContinuation:
    """Bind one adopt Decision to the exact confirmed setup and Correlation request."""
    setup = run.multicam_setup
    if setup is None or not setup.auxiliary_cameras:
        return MulticamAlignmentContinuation(
            schema_version=1,
            adopted_decision_ref=decision_ref,
            requirement="not_required",
            expected_revision=expected_revision,
            main_audio_stable=True,
            setup_id=None,
            setup_hash=None,
            operation_id=None,
            alignment_id=None,
            request_hash=None,
            writer_profile_name=None,
            writer_profile_version=None,
            writer_profile_hash=None,
        )

    setup_hash = canonical_sha256_v1(setup.to_dict())
    writer_profile_hash = AUDALIGN_CORRELATION_WRITER_PROFILE_SHA256
    operation_id, alignment_id = _continuation_ids(
        run,
        decision_ref,
        setup,
        setup_hash,
        writer_profile_name=AUDALIGN_CORRELATION_PROFILE_NAME,
        writer_profile_version=AUDALIGN_CORRELATION_PROFILE_VERSION,
        writer_profile_hash=writer_profile_hash,
    )
    provisional = MulticamAlignmentContinuation(
        schema_version=1,
        adopted_decision_ref=decision_ref,
        requirement="required",
        expected_revision=expected_revision,
        main_audio_stable=True,
        setup_id=setup.setup_id,
        setup_hash=setup_hash,
        operation_id=operation_id,
        alignment_id=alignment_id,
        request_hash="0" * 64,
        writer_profile_name=AUDALIGN_CORRELATION_PROFILE_NAME,
        writer_profile_version=AUDALIGN_CORRELATION_PROFILE_VERSION,
        writer_profile_hash=writer_profile_hash,
    )
    _request, request_hash = _request_facts(
        project_path,
        run,
        provisional,
        setup,
        AUDALIGN_CORRELATION_WRITER_PROFILE,
        validate_project_revision=False,
    )
    return MulticamAlignmentContinuation(
        schema_version=1,
        adopted_decision_ref=decision_ref,
        requirement="required",
        expected_revision=expected_revision,
        main_audio_stable=True,
        setup_id=setup.setup_id,
        setup_hash=setup_hash,
        operation_id=operation_id,
        alignment_id=alignment_id,
        request_hash=request_hash,
        writer_profile_name=AUDALIGN_CORRELATION_PROFILE_NAME,
        writer_profile_version=AUDALIGN_CORRELATION_PROFILE_VERSION,
        writer_profile_hash=writer_profile_hash,
    )


def _failure_input_hash(request_hash: str, code: str) -> str:
    return canonical_sha256_v1(
        {
            "schema_version": 1,
            "kind": "automatic_alignment_preflight_failure",
            "request_hash": request_hash,
            "code": code,
        }
    )


def _deferred_alignment_status(
    continuation: MulticamAlignmentContinuation,
) -> dict[str, object]:
    assert continuation.operation_id is not None
    return {
        "schema_version": 1,
        "status": "failed",
        "operation_id": continuation.operation_id,
        "alignment_ref": None,
        "failure": MediaOperationFailure(
            code="alignment_runtime_unavailable",
            responsibility="roughcut_core",
            action="validate_alignment_basis",
            message_code="alignment_failed",
        ).to_dict(),
    }


def _existing_record(
    project_path: Path,
    continuation: MulticamAlignmentContinuation,
) -> MediaOperationRecord | None:
    assert continuation.operation_id is not None
    store = MediaOperationStore(project_path, ProjectStore(project_path).load().project_id)
    return store.read(continuation.operation_id, allow_writer_temp=True)


def run_multicam_alignment_continuation(project_path: Path, run_id: str) -> None:
    """Execute once after adopt; all failure paths remain adopt-successful."""
    validate_safe_id(run_id, field="run_id")
    run = WorkflowStore(project_path).read_run(run_id)
    continuation = run.multicam_alignment_continuation
    if continuation is None or continuation.requirement == "not_required":
        return
    setup = run.multicam_setup
    if setup is None:
        return
    if not _alignment_runtime_is_released():
        return
    _require_continuation_profile(
        continuation,
        writer_profile_name=AUDALIGN_CORRELATION_PROFILE_NAME,
        writer_profile_version=AUDALIGN_CORRELATION_PROFILE_VERSION,
        writer_profile_hash=AUDALIGN_CORRELATION_WRITER_PROFILE_SHA256,
        description="continuation writer profile is not the current Correlation profile",
    )
    _validate_continuation_identity(
        project_path,
        run,
        continuation,
        setup,
        writer_profile_name=AUDALIGN_CORRELATION_PROFILE_NAME,
        writer_profile_version=AUDALIGN_CORRELATION_PROFILE_VERSION,
        writer_profile_hash=AUDALIGN_CORRELATION_WRITER_PROFILE_SHA256,
    )
    try:
        request, _request_hash = _request_facts(
            project_path,
            run,
            continuation,
            setup,
            AUDALIGN_CORRELATION_WRITER_PROFILE,
            expected_request_hash=continuation.request_hash,
        )
        existing = _existing_record(project_path, continuation)
        if existing is not None and existing.status in {
            "succeeded",
            "failed",
            "interrupted",
        }:
            return
        assert continuation.operation_id is not None
        assert continuation.alignment_id is not None
        run_align_multicam(
            project_path,
            operation_id=continuation.operation_id,
            alignment_id=continuation.alignment_id,
            expected_revision=continuation.expected_revision,
            main_camera=cast(dict[str, object], request["main_camera"]),
            auxiliary_cameras=cast(list[dict[str, object]], request["auxiliary_cameras"]),
            main_audio_stable=continuation.main_audio_stable,
            max_temporary_disk_bytes=AUTOMATIC_ALIGNMENT_MAX_TEMPORARY_DISK_BYTES,
            max_analysis_memory_bytes=AUTOMATIC_ALIGNMENT_MAX_ANALYSIS_MEMORY_BYTES,
            max_runtime_seconds=AUTOMATIC_ALIGNMENT_MAX_RUNTIME_SECONDS,
        )
    except (KeyboardInterrupt, SystemExit):
        existing = _existing_record(project_path, continuation)
        if existing is None:
            assert continuation.operation_id is not None
            assert continuation.request_hash is not None
            persist_alignment_preflight_failure(
                project_path,
                operation_id=continuation.operation_id,
                request_hash=continuation.request_hash,
                input_hash=_failure_input_hash(continuation.request_hash, "alignment_interrupted"),
                code="alignment_interrupted",
                status="interrupted",
            )
    except Exception as error:  # noqa: BLE001 - continuation closes bounded provider failures
        existing = _existing_record(project_path, continuation)
        if existing is not None:
            return
        assert continuation.operation_id is not None
        assert continuation.request_hash is not None
        code = getattr(error, "code", "alignment_store_integrity_error")
        if not isinstance(code, str):
            code = "alignment_store_integrity_error"
        persist_alignment_preflight_failure(
            project_path,
            operation_id=continuation.operation_id,
            request_hash=continuation.request_hash,
            input_hash=_failure_input_hash(continuation.request_hash, code),
            code=code,
        )


def _alignment_ref(result: AlignmentOperationResult) -> dict[str, object]:
    return result.to_dict()


def _validate_operation_identity(
    record: MediaOperationRecord,
    store: MediaOperationStore,
    continuation: MulticamAlignmentContinuation,
    expected_request_hash: str,
) -> None:
    if continuation.operation_id is None:
        raise _workflow_error("required continuation is missing operation identity")
    if record.operation_id != continuation.operation_id:
        raise _workflow_error("media operation ID does not match continuation")
    if record.scope != store.scope:
        raise _workflow_error("media operation scope does not match the Project")
    if (
        record.operation_type != "align_multicam"
        or record.request_hash != expected_request_hash
    ):
        raise _workflow_error("media operation request identity does not match continuation")


def _validate_terminal_alignment(
    project_path: Path,
    run: WorkflowRun,
    continuation: MulticamAlignmentContinuation,
    record: MediaOperationRecord,
) -> AlignmentOperationResult:
    if not isinstance(record.result_ref, AlignmentOperationResult):
        raise _workflow_error("succeeded alignment operation has no alignment result")
    result = record.result_ref
    if continuation.alignment_id is None or result.alignment_id != continuation.alignment_id:
        raise _workflow_error("alignment result ID does not match continuation")
    try:
        artifact = AlignmentStore(project_path).read(continuation.alignment_id)
    except Exception as error:
        raise _workflow_error("published alignment artifact is unreadable") from error
    if artifact is None:
        raise _workflow_error("succeeded alignment operation has no published artifact")
    algorithm = artifact.algorithm
    if (
        algorithm.name != AUDALIGN_CORRELATION_ALGORITHM_NAME
        or algorithm.version != AUDALIGN_CORRELATION_ALGORITHM_VERSION
        or algorithm.upstream_commit != AUDALIGN_CORRELATION_UPSTREAM_COMMIT
        or algorithm.accuracy is not None
        or algorithm.num_processors is not None
        or algorithm.verification_profile.name != AUDALIGN_CORRELATION_PROFILE_NAME
        or algorithm.verification_profile.version != AUDALIGN_CORRELATION_PROFILE_VERSION
    ):
        raise _workflow_error("published alignment artifact is not the Correlation result")
    if (
        artifact.alignment_id != continuation.alignment_id
        or artifact.project_id != run.project_id
        or artifact.content_hash != result.content_hash
        or artifact.producer_operation_id != record.operation_id
        or artifact.request_hash != record.request_hash
        or artifact.input_hash != record.input_hash
    ):
        raise _workflow_error("published alignment artifact does not match operation")
    return result


def _legacy_bbc_continuation_compatibility(
    project_path: Path,
    run: WorkflowRun,
    continuation: MulticamAlignmentContinuation,
    setup: MulticamSetup,
    record: MediaOperationRecord | None,
    store: MediaOperationStore,
) -> str:
    """Read-only compatibility for the exact 0.2.6 BBC-to-Correlation pair."""
    _require_continuation_profile(
        continuation,
        writer_profile_name=BBC_PROFILE_NAME,
        writer_profile_version=BBC_PROFILE_VERSION,
        writer_profile_hash=BBC_WRITER_PROFILE_SHA256,
        description="legacy continuation is not the frozen BBC profile",
    )
    _validate_continuation_identity(
        project_path,
        run,
        continuation,
        setup,
        writer_profile_name=BBC_PROFILE_NAME,
        writer_profile_version=BBC_PROFILE_VERSION,
        writer_profile_hash=BBC_WRITER_PROFILE_SHA256,
    )
    legacy_request, _legacy_hash = _request_facts(
        project_path,
        run,
        continuation,
        setup,
        BBC_WRITER_PROFILE,
        expected_request_hash=continuation.request_hash,
    )
    current_request, current_hash = _request_facts(
        project_path,
        run,
        continuation,
        setup,
        AUDALIGN_CORRELATION_WRITER_PROFILE,
    )
    if _request_facts_without_writer_profile(
        legacy_request
    ) != _request_facts_without_writer_profile(current_request):
        raise _workflow_error("legacy continuation request facts are not stable")
    if record is None:
        raise _workflow_error("legacy continuation has no paired Correlation operation")
    _validate_operation_identity(record, store, continuation, current_hash)
    return current_hash


def multicam_alignment_status(
    project_path: Path,
    run: WorkflowRun,
) -> dict[str, object] | None:
    """Derive the public continuation view from durable run/media/artifact state."""
    decision = run.artifact_refs["decision"]
    if decision is None:
        return None
    continuation = run.multicam_alignment_continuation
    if continuation is None:
        if run.multicam_setup is None or not run.multicam_setup.auxiliary_cameras:
            return {
                "schema_version": 1,
                "status": "not_required",
                "operation_id": None,
                "alignment_ref": None,
                "failure": None,
            }
        raise _workflow_error("adopted auxiliary setup has no continuation identity")
    if continuation.requirement == "not_required":
        return {
            "schema_version": 1,
            "status": "not_required",
            "operation_id": None,
            "alignment_ref": None,
            "failure": None,
        }
    if run.multicam_setup is None:
        raise _workflow_error("required continuation has no durable setup")
    if not _alignment_runtime_is_released():
        return _deferred_alignment_status(continuation)
    profile_is_current = (
        continuation.writer_profile_name == AUDALIGN_CORRELATION_PROFILE_NAME
        and continuation.writer_profile_version == AUDALIGN_CORRELATION_PROFILE_VERSION
        and continuation.writer_profile_hash == AUDALIGN_CORRELATION_WRITER_PROFILE_SHA256
    )
    profile_is_legacy = (
        continuation.writer_profile_name == BBC_PROFILE_NAME
        and continuation.writer_profile_version == BBC_PROFILE_VERSION
        and continuation.writer_profile_hash == BBC_WRITER_PROFILE_SHA256
    )
    if not profile_is_current and not profile_is_legacy:
        raise _workflow_error("continuation writer profile is not a supported frozen identity")
    assert continuation.operation_id is not None
    assert continuation.alignment_id is not None
    assert continuation.request_hash is not None
    store = MediaOperationStore(project_path, run.project_id)
    try:
        record = store.read(continuation.operation_id, allow_writer_temp=True)
    except Exception as error:
        raise _workflow_error("media operation record is unreadable") from error
    if profile_is_legacy:
        request_hash = _legacy_bbc_continuation_compatibility(
            project_path,
            run,
            continuation,
            run.multicam_setup,
            record,
            store,
        )
        read_only_compatibility = True
    else:
        _require_continuation_profile(
            continuation,
            writer_profile_name=AUDALIGN_CORRELATION_PROFILE_NAME,
            writer_profile_version=AUDALIGN_CORRELATION_PROFILE_VERSION,
            writer_profile_hash=AUDALIGN_CORRELATION_WRITER_PROFILE_SHA256,
            description="continuation writer profile is not the current Correlation profile",
        )
        _validate_continuation_identity(
            project_path,
            run,
            continuation,
            run.multicam_setup,
            writer_profile_name=AUDALIGN_CORRELATION_PROFILE_NAME,
            writer_profile_version=AUDALIGN_CORRELATION_PROFILE_VERSION,
            writer_profile_hash=AUDALIGN_CORRELATION_WRITER_PROFILE_SHA256,
        )
        try:
            _request, request_hash = _request_facts(
                project_path,
                run,
                continuation,
                run.multicam_setup,
                AUDALIGN_CORRELATION_WRITER_PROFILE,
                expected_request_hash=continuation.request_hash,
            )
        except WorkflowError:
            raise
        except Exception as error:
            raise _workflow_error("automatic alignment request could not be revalidated") from error
        read_only_compatibility = False
    if record is None:
        assert not read_only_compatibility
        return {
            "schema_version": 1,
            "status": "pending",
            "operation_id": continuation.operation_id,
            "alignment_ref": None,
            "failure": None,
        }
    _validate_operation_identity(record, store, continuation, request_hash)
    if not read_only_compatibility:
        try:
            record = media_operation_status(project_path, continuation.operation_id)
        except Exception as error:
            raise _workflow_error("media operation status is unreadable") from error
        _validate_operation_identity(record, store, continuation, request_hash)
    if record.status in {"pending", "running"}:
        return {
            "schema_version": 1,
            "status": record.status,
            "operation_id": continuation.operation_id,
            "alignment_ref": None,
            "failure": None,
        }
    if record.status == "failed":
        assert record.error is not None
        return {
            "schema_version": 1,
            "status": "failed",
            "operation_id": continuation.operation_id,
            "alignment_ref": None,
            "failure": record.error.to_dict(),
        }
    if record.status == "interrupted":
        assert record.error is not None
        return {
            "schema_version": 1,
            "status": "interrupted",
            "operation_id": continuation.operation_id,
            "alignment_ref": None,
            "failure": record.error.to_dict(),
        }
    result = _validate_terminal_alignment(
        project_path,
        run,
        continuation,
        record,
    )
    artifact = AlignmentStore(project_path).read(continuation.alignment_id)
    assert artifact is not None
    complete = all(camera.status == "complete" for camera in artifact.auxiliary_cameras)
    status = "succeeded" if complete else "partial"
    return {
        "schema_version": 1,
        "status": status,
        "operation_id": continuation.operation_id,
        "alignment_ref": _alignment_ref(result),
        "failure": None,
    }


def validate_multicam_alignment_status(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "status",
        "operation_id",
        "alignment_ref",
        "failure",
    }:
        raise _workflow_error("public multicam alignment status is not closed")
    if isinstance(value["schema_version"], bool) or value["schema_version"] != 1:
        raise _workflow_error("public multicam alignment status schema is unsupported")
    status = value["status"]
    if not isinstance(status, str) or status not in {
        "not_required",
        "pending",
        "running",
        "succeeded",
        "partial",
        "failed",
        "interrupted",
    }:
        raise _workflow_error("public multicam alignment status is unsupported")
    operation_id = value["operation_id"]
    alignment_ref = value["alignment_ref"]
    failure = value["failure"]
    if status == "not_required":
        if operation_id is not None or alignment_ref is not None or failure is not None:
            raise _workflow_error("not_required alignment status carries a result")
        return
    if not isinstance(operation_id, str):
        raise _workflow_error("alignment status operation ID is missing")
    validate_safe_id(operation_id, field="multicam alignment status operation_id")
    if status in {"pending", "running"}:
        if alignment_ref is not None or failure is not None:
            raise _workflow_error("nonterminal alignment status carries terminal facts")
    elif status in {"succeeded", "partial"}:
        if not isinstance(alignment_ref, dict) or set(alignment_ref) != {
            "kind",
            "alignment_id",
            "schema_version",
            "content_hash",
        } or alignment_ref["kind"] != "multicam_alignment":
            raise _workflow_error("terminal alignment status has an invalid result ref")
        try:
            AlignmentOperationResult.from_dict(alignment_ref)
        except Exception as error:
            raise _workflow_error("terminal alignment status has an invalid result ref") from error
        if failure is not None:
            raise _workflow_error("successful alignment status carries failure")
    elif alignment_ref is not None or not isinstance(failure, dict):
        raise _workflow_error("failed alignment status has inconsistent failure facts")
    else:
        try:
            from roughcut.domain.media_operation import MediaOperationFailure

            parsed_failure = MediaOperationFailure.from_dict(failure)
            parsed_failure.validate_for(
                "align_multicam",
                "failed" if status == "failed" else "interrupted",
            )
        except Exception as error:
            raise _workflow_error("failed alignment status has an invalid failure") from error
