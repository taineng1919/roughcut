"""Coordinators for the frozen Project-media operations."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Generic, TypeVar, cast

from roughcut.adapters.ffmpeg.proxy import (
    FFmpegProxyError,
    ProxyCancelled,
    ProxyUnsupported,
)
from roughcut.adapters.ffmpeg.render import FFmpegRenderError
from roughcut.adapters.ffmpeg.verify import RenderVerificationError
from roughcut.adapters.ffmpeg_environment import (
    FFmpegRuntimeDriftError,
    verify_runtime_ffmpeg,
    verify_runtime_pair,
)
from roughcut.adapters.funasr.normalize import TranscriptNormalizationError
from roughcut.adapters.funasr.runner import (
    FunASRConfig,
    FunASRRunnerError,
)
from roughcut.adapters.media_operation_store import (
    MediaOperationStore,
    media_child_process_kwargs,
)
from roughcut.adapters.multicam_parallel_store import MulticamParallelStore
from roughcut.adapters.project_store import ProjectStore
from roughcut.adapters.qwen import (
    BACKEND,
    CLOUD_AUDIO_PROFILE,
    MODEL_NAME,
    REGION,
    TRANSPORT_MODE,
)
from roughcut.adapters.qwen.filetrans import (
    QwenFiletransConfig,
    QwenFiletransError,
)
from roughcut.adapters.qwen.normalize import QwenNormalizationError
from roughcut.adapters.qwen_credential_store import (
    QwenCredential,
    QwenCredentialError,
    read_credential,
)
from roughcut.adapters.runtime_binding import (
    RuntimeBinding,
    RuntimeBindingError,
    configured_runtime_path,
    load_runtime_binding,
)
from roughcut.adapters.workflow_store import WorkflowStore
from roughcut.application.protected_writes import protected_write
from roughcut.application.proxies import (
    ProxyResult,
    _load_ready_manifest,
    _project_source,
    create_proxy,
    read_proxy,
)
from roughcut.application.qwen_credentials import credential_readiness_status
from roughcut.application.renders import _resolve_source_path
from roughcut.application.sources import fingerprint_file
from roughcut.application.transcription import (
    TranscriptionRunner,
    transcribe_source,
    transcribe_source_qwen_filetrans,
)
from roughcut.application.workflows import (
    WorkflowFacadeResult,
    synchronize_workflow_transcript_binding,
    workflow_status,
)
from roughcut.domain.asr import QWEN_FILETRANS_ROUTE, resolve_asr_route
from roughcut.domain.errors import WorkflowError
from roughcut.domain.media_operation import (
    MEDIA_OPERATION_SCHEMA_V2,
    RUNNING_PHASES,
    SCHEMA2_TYPES,
    MediaOperationError,
    MediaOperationFailure,
    MediaOperationRecord,
    MediaOperationResult,
    MediaOperationStatus,
    ParallelRenderOperationResult,
    ProjectOperationScope,
    ProxyOperationResult,
    RenderOperationResult,
    TranscriptOperationResult,
    approve_export_request_projection,
    hash_approve_export_request,
    hash_proxy_input,
    hash_proxy_request,
    hash_transcription_input,
    hash_transcription_request,
    proxy_request_projection,
    transcription_request_projection,
    validate_media_operation_id,
)
from roughcut.domain.multicam_parallel import ParallelRenderError
from roughcut.domain.project import Project, ProjectError, SourceAsset
from roughcut.domain.proxy import derive_proxy_profile, proxy_cache_key
from roughcut.domain.render import ToolResolution
from roughcut.domain.workflow import (
    WorkflowRun,
    canonical_sha256_v1,
    subject_content_hash,
    workflow_action_input_hash,
)

T = TypeVar("T")
DEFAULT_TRANSCRIPTION_TIMEOUT_MILLISECONDS = 3_600_000

# The closed Cloud execution-input identity.  These two field sets are the whole
# Cloud projection; the hash below refuses any other shape, so a credential, a
# temporary or signed locator and any provider task identity can never enter the
# execution identity of an operation.
_QWEN_FILETRANS_INPUT_FIELDS = frozenset(
    {
        "input_schema_version",
        "input_kind",
        "operation_type",
        "backend",
        "project_id",
        "source_ref",
        "expected_project_revision",
        "cloud_config",
    }
)
_QWEN_FILETRANS_CLOUD_CONFIG_FIELDS = frozenset(
    {
        "cloud_config_schema_version",
        "ffmpeg_tool_selection_hash",
        "audio_profile",
        "region",
        "model",
        "workspace_id",
        "transport",
        "speaker_diarization",
        "timeout_milliseconds",
    }
)
_QWEN_FILETRANS_SOURCE_REF_FIELDS = frozenset({"source_id", "source_snapshot_hash"})


@dataclass(frozen=True)
class MediaOperationOutcome(Generic[T]):
    record: MediaOperationRecord
    result: T | None
    readback: bool


@dataclass(frozen=True)
class _PersistentMediaRuntime:
    binding: RuntimeBinding
    runtime_binding_sha256: str
    python_receipt_hash: str
    ffmpeg_tool_selection_hash: str
    ffprobe_tool_selection_hash: str
    ffmpeg: ToolResolution
    ffprobe: ToolResolution


def media_operation_status(
    project_path: Path,
    operation_id: str,
) -> MediaOperationRecord:
    """Read status and converge an abandoned nonterminal writer."""

    validate_media_operation_id(operation_id)
    root, _project, store = _project_context(project_path)
    initial = store.read(operation_id, allow_writer_temp=True)
    if initial is None:
        raise MediaOperationError(
            "operation_not_found",
            "Roughcut Project-media operation status did not find the requested record",
        )
    if initial.status in {"succeeded", "failed", "interrupted"}:
        _reconcile_parallel_publish_intent(root, operation_id, initial)
        return initial
    with store.writer(operation_id, create=False) as acquired:
        if not acquired:
            current = store.read(operation_id, allow_writer_temp=True)
            if current is None:
                raise MediaOperationError(
                    "operation_integrity_error",
                    "Roughcut Project-media operation status lost the active record",
                )
            return current
        current = store.read(operation_id)
        if current is None:
            raise MediaOperationError(
                "operation_integrity_error",
                "Roughcut Project-media operation status lost the abandoned record",
            )
        if current.status in {"succeeded", "failed", "interrupted"}:
            _reconcile_parallel_publish_intent(root, operation_id, current)
            return current
        if current.status == "pending":
            current = _running_record(
                current,
                RUNNING_PHASES[current.operation_type][0],
            )
            store.write_locked(current)
        if current.operation_type == "align_multicam":
            interrupted = _terminal_record(
                current,
                status="interrupted",
                failure=MediaOperationFailure(
                    code="alignment_interrupted",
                    responsibility="roughcut_core",
                    action="recover_abandoned_media_operation",
                    message_code="alignment_interrupted",
                ),
            )
        elif current.operation_type == "render_multicam_parallel":
            interrupted = _terminal_record(
                current,
                status="interrupted",
                failure=MediaOperationFailure(
                    code="parallel_render_interrupted",
                    responsibility="roughcut_core",
                    action="recover_abandoned_media_operation",
                    message_code="parallel_render_interrupted",
                ),
            )
        else:
            interrupted = _terminal_record(
                current,
                status="interrupted",
                failure=MediaOperationFailure(
                    code="media_operation_interrupted",
                    responsibility="roughcut_core",
                    action="recover_abandoned_media_operation",
                    message_code=_terminal_message(
                        current.operation_type, "interrupted"
                    ),
                ),
            )
        return store.write_locked(interrupted)


def _reconcile_parallel_publish_intent(
    root: Path,
    operation_id: str,
    record: MediaOperationRecord,
) -> None:
    if record.status != "succeeded" or not isinstance(
        record.result_ref, ParallelRenderOperationResult
    ):
        return
    try:
        MulticamParallelStore(root).clear_publish_intent(
            record.result_ref.parallel_render_id,
            operation_id,
            record.result_ref.manifest_content_hash,
        )
    except ParallelRenderError as error:
        try:
            published = MulticamParallelStore(root).read_published_manifest(
                record.result_ref.parallel_render_id
            )
        except ParallelRenderError as read_error:
            raise MediaOperationError(
                "operation_integrity_error",
                "Roughcut succeeded parallel render final is not canonically readable",
            ) from read_error
        if published is None:
            raise MediaOperationError(
                "operation_integrity_error",
                "Roughcut succeeded parallel render final is missing",
            ) from error


def run_transcription_operation(
    project_path: Path,
    *,
    operation_id: str,
    source_id: str,
    expected_project_revision: int,
    speaker_diarization: bool,
    timeout_milliseconds: int = DEFAULT_TRANSCRIPTION_TIMEOUT_MILLISECONDS,
    runner: TranscriptionRunner | None = None,
) -> MediaOperationOutcome[object]:
    """Run or read back one exact authorized transcription.

    The Authorization / Source / revision basis is validated first, then the
    canonical route decides which single backend prepares the execution:
    ``funasr`` uses the unchanged local path and ``qwen_filetrans`` reads the
    Cloud credential before any operation record exists, so an unusable
    credential stays a setup/readiness state instead of a runtime failure.
    """

    validate_media_operation_id(operation_id)
    root, project_identity, store = _project_context(project_path)
    request_projection = transcription_request_projection(
        store.scope,
        source_id=source_id,
        expected_project_revision=expected_project_revision,
        speaker_diarization=speaker_diarization,
        timeout_milliseconds=timeout_milliseconds,
    )
    request_hash = hash_transcription_request(request_projection)
    existing = store.read(operation_id, allow_writer_temp=True)
    if existing is not None:
        _require_same_request(existing, "transcribe_source", request_hash)
        return MediaOperationOutcome(existing, None, True)

    workflow_store = WorkflowStore(root)
    with workflow_store.write_lock():
        raced = store.read(operation_id, allow_writer_temp=True)
        if raced is not None:
            _require_same_request(raced, "transcribe_source", request_hash)
            return MediaOperationOutcome(raced, None, True)
        with protected_write(
            root,
            "transcribe_source",
            source_id=source_id,
            speaker_diarization=speaker_diarization,
        ) as run:
            project = ProjectStore(root).load()
            if project.project_id != project_identity.project_id:
                raise MediaOperationError(
                    "operation_integrity_error",
                    "Roughcut Project identity changed during transcription preflight",
                )
            if project.revision != expected_project_revision:
                raise ProjectError("project revision conflict")
            source = _source(project, source_id)
            source_path = _resolve_source_path(root, source)
            if fingerprint_file(source_path) != source.fingerprint:
                raise ProjectError("transcription source fingerprint has changed")
            runtime = _load_persistent_runtime()
            route = resolve_asr_route(source.tags)
            funasr_config: FunASRConfig | None = None
            qwen_config: QwenFiletransConfig | None = None
            if route == QWEN_FILETRANS_ROUTE:
                # Cloud credential preflight happens here, before the operation
                # record below, so no pending/failed record and no raw evidence
                # can exist for a credential that is not usable.
                credential = _read_qwen_filetrans_credential()
                qwen_config = _qwen_filetrans_config(
                    runtime,
                    credential,
                    speaker_diarization=speaker_diarization,
                    timeout_milliseconds=timeout_milliseconds,
                )
                input_hash = _qwen_filetrans_input_hash(
                    _qwen_filetrans_input_projection(
                        project,
                        source,
                        runtime,
                        workspace_id=credential.workspace_id,
                        speaker_diarization=speaker_diarization,
                        timeout_milliseconds=timeout_milliseconds,
                    )
                )
            else:
                funasr_config = _transcription_config(
                    runtime,
                    speaker_diarization=speaker_diarization,
                    timeout_milliseconds=timeout_milliseconds,
                )
                input_projection = _transcription_input_projection(
                    project,
                    source,
                    runtime,
                    speaker_diarization=speaker_diarization,
                    timeout_milliseconds=timeout_milliseconds,
                )
                input_hash = hash_transcription_input(input_projection)
            with store.writer(operation_id, create=True) as acquired:
                if not acquired:
                    concurrent = _read_live_record(
                        store,
                        operation_id,
                        "transcribe_source",
                        request_hash,
                    )
                    return MediaOperationOutcome(concurrent, None, True)
                raced = store.read(operation_id)
                if raced is not None:
                    _require_same_request(
                        raced, "transcribe_source", request_hash
                    )
                    return MediaOperationOutcome(raced, None, True)
                active = _new_pending_record(
                    operation_id,
                    store.scope,
                    "transcribe_source",
                    request_hash,
                    input_hash,
                )
                store.write_locked(active)
                active = _running_record(active, "transcription_preparing")
                store.write_locked(active)

                def update_phase(phase: str) -> None:
                    nonlocal active
                    active = _phase_record(active, phase)
                    store.write_locked(active)

                try:
                    if route == QWEN_FILETRANS_ROUTE:
                        _validate_qwen_cloud_runtime(runtime)
                    else:
                        _validate_media_runtime(runtime)
                    if route == QWEN_FILETRANS_ROUTE:
                        assert qwen_config is not None
                        transcript = transcribe_source_qwen_filetrans(
                            root,
                            source_id,
                            expected_revision=expected_project_revision,
                            config=qwen_config,
                            phase_callback=update_phase,
                        )
                    else:
                        assert funasr_config is not None
                        transcript = transcribe_source(
                            root,
                            source_id,
                            expected_revision=expected_project_revision,
                            runner=runner,
                            config=funasr_config,
                            phase_callback=update_phase,
                        )
                    update_phase("transcription_synchronizing_binding")
                    synchronize_workflow_transcript_binding(
                        root,
                        run.run_id,
                        source_id,
                        transcript.transcript_version_id,
                    )
                    result_ref = TranscriptOperationResult(
                        source_id=source_id,
                        transcript_version_id=(
                            transcript.transcript_version_id
                        ),
                        schema_version=transcript.schema_version,
                        content_hash=subject_content_hash(
                            "timed_transcript",
                            transcript.schema_version,
                            transcript.to_dict(),
                        ),
                        project_revision=expected_project_revision + 1,
                    )
                    succeeded = _terminal_record(
                        active,
                        status="succeeded",
                        result=result_ref,
                    )
                    return MediaOperationOutcome(
                        store.write_locked(succeeded),
                        transcript,
                        False,
                    )
                except (KeyboardInterrupt, SystemExit):
                    _record_interruption(store, active)
                    raise
                except MediaOperationError:
                    raise
                except Exception as error:
                    failure = _transcription_failure(active, error)
                    store.write_locked(
                        _terminal_record(
                            active,
                            status="failed",
                            failure=failure,
                        )
                    )
                    raise


def run_proxy_operation(
    project_path: Path,
    *,
    operation_id: str,
    source_id: str,
    expected_project_revision: int,
) -> MediaOperationOutcome[ProxyResult]:
    """Run or read back one exact Proxy creation."""

    validate_media_operation_id(operation_id)
    root, project, store = _project_context(project_path)
    request_projection = proxy_request_projection(
        store.scope,
        source_id=source_id,
        expected_project_revision=expected_project_revision,
    )
    request_hash = hash_proxy_request(request_projection)
    existing = store.read(operation_id, allow_writer_temp=True)
    if existing is not None:
        _require_same_request(existing, "proxy_create", request_hash)
        return MediaOperationOutcome(existing, None, True)

    if project.revision != expected_project_revision:
        raise ProjectError("project revision conflict")
    source = _project_source(project, source_id)
    source_path = _resolve_source_path(root, source)
    if fingerprint_file(source_path) != source.fingerprint:
        raise ProjectError("proxy source fingerprint has changed")
    profile = derive_proxy_profile(source.probe, project.settings)
    cache_key = proxy_cache_key(source.fingerprint, source.probe, profile)
    runtime = _load_persistent_runtime()
    input_projection = {
        "input_schema_version": 1,
        "operation_type": "proxy_create",
        "project_id": project.project_id,
        "source_ref": {
            "source_id": source.source_id,
            "source_snapshot_hash": canonical_sha256_v1(source.to_dict()),
        },
        "expected_project_revision": expected_project_revision,
        "cache_key": cache_key,
        "profile": profile.to_dict(),
        "tool_refs": {
            "runtime_binding_sha256": runtime.runtime_binding_sha256,
            "ffmpeg_tool_selection_hash": (
                runtime.ffmpeg_tool_selection_hash
            ),
            "ffprobe_tool_selection_hash": (
                runtime.ffprobe_tool_selection_hash
            ),
        },
    }
    input_hash = hash_proxy_input(input_projection)
    with store.writer(operation_id, create=True) as acquired:
        if not acquired:
            concurrent = _read_live_record(
                store, operation_id, "proxy_create", request_hash
            )
            return MediaOperationOutcome(concurrent, None, True)
        raced = store.read(operation_id)
        if raced is not None:
            _require_same_request(raced, "proxy_create", request_hash)
            return MediaOperationOutcome(raced, None, True)
        active = _new_pending_record(
            operation_id,
            store.scope,
            "proxy_create",
            request_hash,
            input_hash,
        )
        store.write_locked(active)
        active = _running_record(active, "proxy_preparing")
        store.write_locked(active)

        def update_phase(phase: str) -> None:
            nonlocal active
            active = _phase_record(active, phase)
            store.write_locked(active)

        try:
            _validate_media_runtime(runtime)
            result = create_proxy(
                root,
                source_id=source_id,
                expected_revision=expected_project_revision,
                tools=(runtime.ffmpeg, runtime.ffprobe),
                phase_callback=update_phase,
            )
            verified = read_proxy(
                root,
                source_id=source_id,
                expected_revision=expected_project_revision,
                ffprobe=runtime.ffprobe,
            )
            if verified.status != "ready" or verified.cache_key != cache_key:
                raise ProjectError(
                    "published Proxy did not pass ready readback"
                )
            manifest = _load_ready_manifest(root, source, cache_key)
            output_path = root / manifest.output.relative_path
            output = fingerprint_file(output_path)
            if (
                output.size != manifest.output.size
                or output.sha256_head_tail
                != manifest.output.sha256_head_tail
            ):
                raise ProjectError(
                    "published Proxy output identity changed during readback"
                )
            result_ref = ProxyOperationResult(
                source_id=source_id,
                cache_key=cache_key,
                manifest_schema_version=manifest.schema_version,
                manifest_content_hash=canonical_sha256_v1(
                    manifest.to_dict()
                ),
                output_relative_path=manifest.output.relative_path,
                output_size=manifest.output.size,
                output_sha256_head_tail=(
                    manifest.output.sha256_head_tail
                ),
                project_revision=expected_project_revision,
            )
            succeeded = _terminal_record(
                active,
                status="succeeded",
                result=result_ref,
            )
            return MediaOperationOutcome(
                store.write_locked(succeeded),
                result,
                False,
            )
        except (KeyboardInterrupt, SystemExit, ProxyCancelled):
            _record_interruption(store, active)
            raise
        except MediaOperationError:
            raise
        except Exception as error:
            failure = _proxy_failure(active, error)
            store.write_locked(
                _terminal_record(
                    active,
                    status="failed",
                    failure=failure,
                )
            )
            raise


def run_approve_export_operation(
    project_path: Path,
    *,
    run_id: str,
    action_id: str,
    action_input: object,
) -> MediaOperationOutcome[object]:
    """Run approve_export through its fixed workflow façade with media tracking."""

    from roughcut.application.workflows import _workflow_export_operation
    from roughcut.domain.workflow_actions import parse_workflow_action_input

    parsed = parse_workflow_action_input("approve_export", action_input)
    input_hash = workflow_action_input_hash(
        run_id,
        action_id,
        "approve_export",
        parsed,
    )
    root, _project, store = _project_context(project_path)
    request_projection = approve_export_request_projection(
        store.scope,
        run_id=run_id,
        action_id=action_id,
        workflow_action_input_hash=input_hash,
    )
    request_hash = hash_approve_export_request(request_projection)
    existing = store.read(action_id, allow_writer_temp=True)
    if existing is not None:
        _require_same_request(existing, "approve_export", request_hash)
        status = workflow_status(root, run_id)
        run = WorkflowRun.from_dict(status["workflow_run"])
        workflow_store = WorkflowStore(root)
        receipt = None
        receipt_path = workflow_store.receipts_path / f"{action_id}.json"
        if os.path.lexists(receipt_path):
            receipt = workflow_store.read_receipt(
                action_id,
                run_id=run_id,
                input_hash=input_hash,
            )
        if existing.status == "succeeded":
            result = existing.result_ref
            if (
                not isinstance(result, RenderOperationResult)
                or receipt is None
                or canonical_sha256_v1(receipt.to_dict())
                != result.approve_export_receipt_ref.receipt_hash
            ):
                raise MediaOperationError(
                    "operation_integrity_error",
                    "Roughcut approve_export readback rejected mismatched receipt identity",
                )
        return MediaOperationOutcome(
            existing,
            WorkflowFacadeResult(run, receipt, status),
            True,
        )
    return cast(
        MediaOperationOutcome[object],
        _workflow_export_operation(
            root,
            run_id,
            action_id,
            input_hash,
            parsed,
            request_hash=request_hash,
            operation_store=store,
        ),
    )


def _project_context(
    project_path: Path,
) -> tuple[Path, Project, MediaOperationStore]:
    root = Path(os.path.abspath(project_path))
    try:
        root_stat = os.lstat(root)
    except OSError as error:
        raise MediaOperationError(
            "operation_integrity_error",
            "Roughcut Project-media operation could not inspect Project scope",
        ) from error
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise MediaOperationError(
            "operation_integrity_error",
            "Roughcut Project-media operation rejected unsafe Project scope",
        )
    project_json = root / "project.json"
    try:
        project_stat = os.lstat(project_json)
    except OSError as error:
        raise MediaOperationError(
            "operation_integrity_error",
            "Roughcut Project-media operation could not read Project identity",
        ) from error
    if (
        stat.S_ISLNK(project_stat.st_mode)
        or not stat.S_ISREG(project_stat.st_mode)
        or project_stat.st_nlink != 1
    ):
        raise MediaOperationError(
            "operation_integrity_error",
            "Roughcut Project-media operation rejected unsafe Project identity",
        )
    project = ProjectStore(root).load()
    return root, project, MediaOperationStore(root, project.project_id)


def _load_persistent_runtime() -> _PersistentMediaRuntime:
    path = Path(configured_runtime_path())
    try:
        details = os.lstat(path)
        if (
            stat.S_ISLNK(details.st_mode)
            or not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
        ):
            raise RuntimeBindingError(
                "Roughcut runtime binding is not a single-link regular file"
            )
        before = path.read_bytes()
        binding = load_runtime_binding(path)
        after = path.read_bytes()
    except OSError as error:
        raise RuntimeBindingError(
            "Roughcut runtime binding JSON 已损坏"
        ) from error
    if before != after:
        raise RuntimeBindingError(
            "Roughcut runtime binding changed during media preflight"
        )
    return _PersistentMediaRuntime(
        binding=binding,
        runtime_binding_sha256=hashlib.sha256(before).hexdigest(),
        python_receipt_hash=canonical_sha256_v1(binding.python.receipt),
        ffmpeg_tool_selection_hash=canonical_sha256_v1(
            binding.ffmpeg.to_dict()
        ),
        ffprobe_tool_selection_hash=canonical_sha256_v1(
            binding.ffprobe.to_dict()
        ),
        ffmpeg=ToolResolution(
            binding.ffmpeg.command,
            binding.ffmpeg.command,
            binding.ffmpeg.version,
        ),
        ffprobe=ToolResolution(
            binding.ffprobe.command,
            binding.ffprobe.command,
            binding.ffprobe.version,
        ),
    )


def _transcription_config(
    runtime: _PersistentMediaRuntime,
    *,
    speaker_diarization: bool,
    timeout_milliseconds: int,
) -> FunASRConfig:
    binding = runtime.binding
    return FunASRConfig(
        python_path=Path(binding.python.interpreter),
        model_root=Path(binding.install_root) / "cache" / "modelscope",
        asr_model_path=Path(binding.components["asr"].path),
        vad_model_path=Path(binding.components["vad"].path),
        punc_model_path=Path(binding.components["punc"].path),
        speaker_diarization=speaker_diarization,
        speaker_model_path=(
            Path(binding.components["campp"].path)
            if speaker_diarization
            else None
        ),
        ffmpeg_command=binding.ffmpeg.command,
        ffmpeg_version=runtime.ffmpeg.version,
        timeout_seconds=timeout_milliseconds / 1000,
    )


def _runtime_version_runner() -> Callable[[list[str]], subprocess.CompletedProcess[str]]:
    """One bounded, captured child-process runner for runtime version checks."""

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            **cast(dict[str, Any], media_child_process_kwargs()),
        )

    return run


def _validate_media_runtime(runtime: _PersistentMediaRuntime) -> tuple[str, str]:
    try:
        return verify_runtime_pair(
            ffmpeg_command=runtime.ffmpeg.command,
            ffmpeg_version=runtime.ffmpeg.version,
            ffprobe_command=runtime.ffprobe.command,
            ffprobe_version=runtime.ffprobe.version,
            command_runner=_runtime_version_runner(),
        )
    except FFmpegRuntimeDriftError as error:
        raise RuntimeBindingError(
            "Roughcut persistent FFmpeg runtime is unavailable"
        ) from error


def _validate_qwen_cloud_runtime(runtime: _PersistentMediaRuntime) -> str:
    """Verify only the FFmpeg identity the Cloud route actually binds.

    The closed Cloud execution input contains ``ffmpeg_tool_selection_hash`` and
    no ffprobe fact, and the Cloud path never invokes ffprobe, so the pair check
    would let a tool outside that closed basis decide whether an identical Cloud
    execution input runs at all.
    """

    try:
        return verify_runtime_ffmpeg(
            ffmpeg_command=runtime.ffmpeg.command,
            ffmpeg_version=runtime.ffmpeg.version,
            command_runner=_runtime_version_runner(),
        )
    except FFmpegRuntimeDriftError as error:
        raise RuntimeBindingError(
            "Roughcut persistent FFmpeg runtime is unavailable"
        ) from error


def _transcription_input_projection(
    project: Project,
    source: SourceAsset,
    runtime: _PersistentMediaRuntime,
    *,
    speaker_diarization: bool,
    timeout_milliseconds: int,
) -> dict[str, object]:
    components = runtime.binding.components
    return {
        "input_schema_version": 1,
        "operation_type": "transcribe_source",
        "project_id": project.project_id,
        "source_ref": {
            "source_id": source.source_id,
            "source_snapshot_hash": canonical_sha256_v1(source.to_dict()),
        },
        "expected_project_revision": project.revision,
        "transcription_config": {
            "config_schema_version": 1,
            "runtime_binding_sha256": runtime.runtime_binding_sha256,
            "python_receipt_hash": runtime.python_receipt_hash,
            "model_refs": {
                "asr": components["asr"].receipt["value"],
                "vad": components["vad"].receipt["value"],
                "punc": components["punc"].receipt["value"],
                "speaker": (
                    components["campp"].receipt["value"]
                    if speaker_diarization
                    else None
                ),
            },
            "ffmpeg_tool_selection_hash": (
                runtime.ffmpeg_tool_selection_hash
            ),
            "speaker_diarization": speaker_diarization,
            "speaker_mode": (
                "punc_segment" if speaker_diarization else None
            ),
            "timeout_milliseconds": timeout_milliseconds,
        },
    }


def _read_qwen_filetrans_credential() -> QwenCredential:
    """Read the one user-level Cloud credential or stop before execution.

    A never-configured, unusable or insecure credential is a setup/readiness
    state, so it raises the existing closed readiness status instead of creating
    an operation record.  It must never be reinterpreted as a runtime
    ``transcription_failed`` worker result.  Only the shape-validated credential
    is returned here; the API Key never leaves this process, and it never enters
    the execution input projection below.
    """

    try:
        return read_credential()
    except QwenCredentialError as error:
        raise MediaOperationError(
            credential_readiness_status(error),
            "Roughcut transcription did not start because the "
            "Qwen Filetrans credential is not usable",
        ) from error


def _qwen_filetrans_config(
    runtime: _PersistentMediaRuntime,
    credential: QwenCredential,
    *,
    speaker_diarization: bool,
    timeout_milliseconds: int,
) -> QwenFiletransConfig:
    """Map the one public timeout onto the bounded Cloud transport timeouts.

    The public ``timeout_milliseconds`` is the whole Cloud execution budget, so
    every bounded Cloud phase -- each policy/status/result request, the
    temporary upload and the task poll wait -- is bounded by that one value.
    There is no second public timeout and no timeout policy beyond this single
    closed mapping.  The persistent FFmpeg selection is reused; no FunASR model
    path or ``FunASRConfig`` is built on the Cloud route.
    """

    budget_seconds = timeout_milliseconds / 1000
    return QwenFiletransConfig(
        api_key=credential.api_key,
        workspace_id=credential.workspace_id,
        request_timeout_seconds=budget_seconds,
        upload_timeout_seconds=budget_seconds,
        poll_timeout_seconds=budget_seconds,
        ffmpeg_command=runtime.binding.ffmpeg.command,
        ffmpeg_version=runtime.ffmpeg.version,
        speaker_diarization=speaker_diarization,
    )


def _qwen_filetrans_input_projection(
    project: Project,
    source: SourceAsset,
    runtime: _PersistentMediaRuntime,
    *,
    workspace_id: str,
    speaker_diarization: bool,
    timeout_milliseconds: int,
) -> dict[str, object]:
    """Build the closed Cloud execution identity of one new operation.

    This is the whole Cloud execution identity: the exact service, the exact
    prepared audio profile and the exact request facts that decide the result.
    The API Key, any temporary/signed locator, any ``oss://`` object URL and any
    provider task identity are deliberately absent, so rotating the API Key
    cannot change the identity of an already-decided execution.
    """

    return {
        "input_schema_version": 1,
        "input_kind": "qwen_filetrans_transcription_input",
        "operation_type": "transcribe_source",
        "backend": BACKEND,
        "project_id": project.project_id,
        "source_ref": {
            "source_id": source.source_id,
            "source_snapshot_hash": canonical_sha256_v1(source.to_dict()),
        },
        "expected_project_revision": project.revision,
        "cloud_config": {
            "cloud_config_schema_version": 1,
            "ffmpeg_tool_selection_hash": runtime.ffmpeg_tool_selection_hash,
            "audio_profile": CLOUD_AUDIO_PROFILE,
            "region": REGION,
            "model": MODEL_NAME,
            "workspace_id": workspace_id,
            "transport": TRANSPORT_MODE,
            "speaker_diarization": speaker_diarization,
            "timeout_milliseconds": timeout_milliseconds,
        },
    }


def _qwen_filetrans_input_hash(projection: dict[str, object]) -> str:
    """Hash the Cloud execution identity after proving it is closed."""

    _require_closed_projection(
        projection,
        _QWEN_FILETRANS_INPUT_FIELDS,
        "Qwen Filetrans execution input",
    )
    cloud_config = projection["cloud_config"]
    _require_closed_projection(
        cloud_config,
        _QWEN_FILETRANS_CLOUD_CONFIG_FIELDS,
        "Qwen Filetrans cloud config",
    )
    _require_closed_projection(
        projection["source_ref"],
        _QWEN_FILETRANS_SOURCE_REF_FIELDS,
        "Qwen Filetrans source ref",
    )
    return canonical_sha256_v1(projection)


def _require_closed_projection(
    value: object,
    fields: frozenset[str],
    description: str,
) -> None:
    if not isinstance(value, dict) or set(value) != fields:
        raise MediaOperationError(
            "operation_integrity_error",
            f"Roughcut rejected a non-closed {description}",
        )


def _source(project: Project, source_id: str) -> SourceAsset:
    matches = [source for source in project.sources if source.source_id == source_id]
    if len(matches) != 1:
        raise ProjectError("project source does not exist")
    return matches[0]


def _require_same_request(
    record: MediaOperationRecord,
    operation_type: str,
    request_hash: str,
) -> None:
    if (
        record.operation_type != operation_type
        or record.request_hash != request_hash
    ):
        raise MediaOperationError(
            "operation_input_conflict",
            "Roughcut Project-media operation refused the same ID "
            "with a different stable request",
        )


def _read_live_record(
    store: MediaOperationStore,
    operation_id: str,
    operation_type: str,
    request_hash: str,
) -> MediaOperationRecord:
    current = store.read(operation_id, allow_writer_temp=True)
    if current is None:
        raise MediaOperationError(
            "operation_integrity_error",
            "Roughcut Project-media operation found a writer without a record",
        )
    _require_same_request(current, operation_type, request_hash)
    return current


def _new_pending_record(
    operation_id: str,
    scope: ProjectOperationScope,
    operation_type: str,
    request_hash: str,
    input_hash: str,
) -> MediaOperationRecord:
    now = _now()
    return MediaOperationRecord(
        operation_id=operation_id,
        scope=scope,
        operation_type=operation_type,  # type: ignore[arg-type]
        request_hash=request_hash,
        input_hash=input_hash,
        status="pending",
        phase_message_code=RUNNING_PHASES[operation_type][0],
        created_at=now,
        started_at=None,
        updated_at=now,
        finished_at=None,
        result_ref=None,
        error=None,
        schema_version=(
            MEDIA_OPERATION_SCHEMA_V2
            if operation_type in SCHEMA2_TYPES
            else 1
        ),
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
            "Roughcut Project-media operation rejected an unknown phase",
        )
    return replace(
        record,
        phase_message_code=phase,
        updated_at=_now(),
    )


def _terminal_record(
    record: MediaOperationRecord,
    *,
    status: MediaOperationStatus,
    result: MediaOperationResult | None = None,
    failure: MediaOperationFailure | None = None,
) -> MediaOperationRecord:
    if status not in {"succeeded", "failed", "interrupted"}:
        raise MediaOperationError(
            "operation_transition_not_allowed",
            "Roughcut Project-media operation rejected a nonterminal status",
        )
    now = _now()
    return replace(
        record,
        status=status,
        phase_message_code=_terminal_message(record.operation_type, status),
        updated_at=now,
        finished_at=now,
        result_ref=result,
        error=failure,
    )


def _terminal_message(operation_type: str, status: str) -> str:
    prefix = {
        "transcribe_source": "transcription",
        "proxy_create": "proxy",
        "approve_export": "render",
        "align_multicam": "alignment",
        "render_multicam_parallel": "parallel_render",
    }[operation_type]
    return f"{prefix}_{status}"


def _record_interruption(
    store: MediaOperationStore,
    active: MediaOperationRecord,
    *,
    responsibility: str = "host",
) -> None:
    store.write_locked(
        _terminal_record(
            active,
            status="interrupted",
            failure=MediaOperationFailure(
                code="media_operation_interrupted",
                responsibility=responsibility,
                action="interrupt_media_operation",
                message_code=_terminal_message(
                    active.operation_type, "interrupted"
                ),
            ),
        )
    )


def _transcription_failure(
    active: MediaOperationRecord,
    error: Exception,
) -> MediaOperationFailure:
    phase = active.phase_message_code
    if phase == "transcription_synchronizing_binding":
        responsibility = "roughcut_core"
        action = "synchronize_transcript_binding"
    elif phase == "transcription_publishing_transcript":
        responsibility = "roughcut_core"
        action = "publish_transcript"
    elif isinstance(error, FunASRRunnerError):
        if phase == "transcription_decoding_audio":
            responsibility = "asr_worker"
            action = "decode_transcription_audio"
        else:
            responsibility = "asr_worker"
            action = "run_asr_worker"
    elif isinstance(error, QwenFiletransError):
        # Every Cloud transport failure -- configured credential rejected by the
        # provider, network/HTTP, temporary upload, submit, task/subtask
        # failure, poll timeout and result download -- stays inside the existing
        # ASR worker boundary.  Only the sibling FLAC preparation phase is the
        # decode action, because that is the step that produced the audio.
        responsibility = "asr_worker"
        action = (
            "decode_transcription_audio"
            if error.phase == "audio_preparation"
            else "run_asr_worker"
        )
    elif isinstance(error, QwenNormalizationError):
        responsibility = "roughcut_core"
        action = "normalize_transcript"
    elif (
        isinstance(error, ProjectError)
        and phase
        in {
            "transcription_decoding_audio",
            "transcription_running_asr",
        }
    ):
        responsibility = "asr_worker"
        action = "run_asr_worker"
    elif isinstance(error, TranscriptNormalizationError):
        responsibility = "roughcut_core"
        action = "normalize_transcript"
    elif isinstance(error, ProjectError):
        responsibility = "user_input"
        action = "validate_transcription_basis"
    else:
        responsibility = "roughcut_core"
        action = "validate_transcription_basis"
    return MediaOperationFailure(
        code="media_operation_failed",
        responsibility=responsibility,
        action=action,
        message_code="transcription_failed",
    )


def _proxy_failure(
    active: MediaOperationRecord,
    error: Exception,
) -> MediaOperationFailure:
    phase = active.phase_message_code
    if isinstance(error, ProxyUnsupported):
        responsibility = "user_input"
        action = "validate_proxy_basis"
    elif isinstance(error, FFmpegProxyError):
        responsibility = "ffmpeg_proxy"
        action = (
            "verify_proxy"
            if phase == "proxy_verifying"
            else "encode_proxy"
        )
    elif isinstance(error, ProjectError):
        responsibility = "user_input"
        action = "validate_proxy_basis"
    else:
        responsibility = "roughcut_core"
        action = (
            "publish_proxy"
            if phase == "proxy_publishing"
            else "validate_proxy_basis"
        )
    return MediaOperationFailure(
        code="media_operation_failed",
        responsibility=responsibility,
        action=action,
        message_code="proxy_failed",
    )


def _render_failure(
    active: MediaOperationRecord,
    error: Exception,
) -> MediaOperationFailure:
    phase = active.phase_message_code
    if isinstance(error, RenderVerificationError):
        responsibility = "ffmpeg_render"
        action = "verify_render"
    elif isinstance(error, FFmpegRenderError):
        responsibility = "ffmpeg_render"
        action = (
            "verify_render"
            if phase == "render_verifying"
            else "encode_render"
        )
    elif isinstance(error, ProjectError) or (
        isinstance(error, WorkflowError)
        and error.code
        in {
            "workflow_stale",
            "workflow_subject_mismatch",
            "workflow_approval_required",
        }
    ):
        responsibility = "user_input"
        action = "validate_export_basis"
    else:
        responsibility = "roughcut_core"
        action = (
            "publish_export_transaction"
            if phase == "render_publishing_workflow"
            else "validate_export_basis"
        )
    return MediaOperationFailure(
        code="media_operation_failed",
        responsibility=responsibility,
        action=action,
        message_code="render_failed",
    )


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
