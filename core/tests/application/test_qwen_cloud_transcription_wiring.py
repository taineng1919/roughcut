"""WP3B wiring: canonical route -> Cloud execution -> existing publish pipeline.

Every test here is offline.  Only the Cloud adapter callable is swapped, so the
``QwenFiletransConfig`` under test is still the production one built from the
real credential store and the real persistent runtime; the transport, the audio
encoder and the sleep function are fakes.  The Qwen credential is a synthetic
record in a temporary ``HOME``, and the runtime fixture deliberately names
FunASR model paths that do not exist.  No test uploads media, contacts Alibaba or
reads a real user credential.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import roughcut.application.media_operations as operations_module
import roughcut.application.transcription as transcription_module
import roughcut.mcp
from roughcut.adapters.ffmpeg.audio import FFmpegAudioError, FLACEncode
from roughcut.adapters.funasr.runner import FunASRRun
from roughcut.adapters.media_operation_store import MediaOperationStore
from roughcut.adapters.project_store import ProjectStore
from roughcut.adapters.qwen import BACKEND, CLOUD_AUDIO_PROFILE, MODEL_NAME, REGION
from roughcut.adapters.qwen.filetrans import (
    SUBMIT_PATH,
    UPLOAD_POLICY_ENDPOINT,
    HttpRequest,
    HttpResponse,
    QwenFiletransError,
    QwenFiletransRun,
    run_qwen_filetrans,
)
from roughcut.adapters.qwen.normalize import QwenNormalizationError
from roughcut.adapters.qwen_credential_store import clear_credential, write_credential
from roughcut.adapters.runtime_binding import RuntimeBindingError
from roughcut.adapters.workflow_store import WorkflowStore
from roughcut.application.media_operations import (
    _qwen_filetrans_input_hash,
    _qwen_filetrans_input_projection,
    run_transcription_operation,
)
from roughcut.application.projects import create_project
from roughcut.application.sources import fingerprint_file
from roughcut.application.workflows import (
    workflow_action,
    workflow_start,
    workflow_status,
)
from roughcut.domain.asr import ASR_CLOUD_TAG
from roughcut.domain.errors import WorkflowError
from roughcut.domain.media_operation import (
    MediaOperationError,
    TranscriptOperationResult,
)
from roughcut.domain.project import (
    ImportMode,
    MediaProbe,
    ProjectError,
    SourceAsset,
)
from roughcut.domain.render import ToolResolution

ROOT = Path(__file__).resolve().parents[3]
RECOGNITION_FIXTURE = ROOT / "fixtures" / "asr" / "qwen_filetrans_recognition.json"

API_KEY = "fake-cloud-api-key-canary-0001"
ROTATED_API_KEY = "fake-cloud-api-key-canary-0002"
WORKSPACE_ID = "fakecloudworkspace01"
UPLOAD_HOST = "https://dashscope-file-mgr.oss-cn-beijing.aliyuncs.com"
RESULT_HOST = "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com"
SIGNED_RESULT_URL = (
    f"{RESULT_HOST}/prod/fun-asr/20260910/fake-result-object-canary.json"
    "?Expires=1789140747&OSSAccessKeyId=fake-result-access-key-canary"
    "&Signature=fake-signature-canary"
)
TASK_ID = "fake-task-id-canary-0001"
OSS_LOCATOR = "oss://dashscope-tmp/fakecloudworkspace01/audio.flac"
# The recognition fixture ends at 3600 ms, so the synthetic Source must be at
# least that long for the normalizer to accept it.
SOURCE_DURATION_TICKS = 600_000
TRANSCRIPT_TEXT = "你好，世界。"
# Nothing that only exists inside a Cloud transport envelope may reach a
# persisted identity: not the API key, not an upload credential, not a signed or
# ``oss://`` locator, and not the provider task locator.
TRANSPORT_CANARIES = (
    API_KEY,
    ROTATED_API_KEY,
    "fake-upload-access-key-id",
    "fake-upload-signature-canary",
    "fake-upload-policy-token-canary",
    "fake-signature-canary",
    "fake-result-object-canary",
    "fake-result-access-key-canary",
    OSS_LOCATOR,
    TASK_ID,
)

POLICY_DATA = {
    "upload_host": UPLOAD_HOST,
    "upload_dir": "dashscope-tmp/fakecloudworkspace01",
    "oss_access_key_id": "fake-upload-access-key-id",
    "signature": "fake-upload-signature-canary",
    "policy": "fake-upload-policy-token-canary",
    "x_oss_object_acl": "private",
    "x_oss_forbid_overwrite": "true",
}


# --------------------------------------------------------------------------
# fixtures and fakes
# --------------------------------------------------------------------------


def _runtime(*, ffmpeg_tool_selection_hash: str = "3" * 64) -> SimpleNamespace:
    """A persistent runtime whose FunASR model paths intentionally absent."""

    def component(value: str) -> SimpleNamespace:
        return SimpleNamespace(
            path=f"/absent-funasr-model/{value}",
            receipt={"algorithm": "sha256", "value": value * 64},
        )

    binding = SimpleNamespace(
        install_root="/absent-funasr-runtime",
        python=SimpleNamespace(
            interpreter="/absent-funasr-python",
            receipt={"fixture": True},
        ),
        components={
            "asr": component("a"),
            "vad": component("b"),
            "punc": component("c"),
            "campp": component("d"),
        },
        ffmpeg=SimpleNamespace(command="/fixture/ffmpeg"),
    )
    return SimpleNamespace(
        binding=binding,
        runtime_binding_sha256="1" * 64,
        python_receipt_hash="2" * 64,
        ffmpeg_tool_selection_hash=ffmpeg_tool_selection_hash,
        ffprobe_tool_selection_hash="4" * 64,
        ffmpeg=ToolResolution(
            "ffmpeg", "/fixture/ffmpeg", "ffmpeg version 9.0-fixture"
        ),
        ffprobe=ToolResolution(
            "ffprobe", "/fixture/ffprobe", "ffprobe version 9.0-fixture"
        ),
    )


@pytest.fixture
def cloud_runtime(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    runtime = _runtime()
    monkeypatch.setattr(operations_module, "_load_persistent_runtime", lambda: runtime)
    monkeypatch.setattr(
        operations_module,
        "_validate_media_runtime",
        lambda _runtime: ("ffmpeg version 9.0-fixture", "ffprobe version 9.0-fixture"),
    )
    monkeypatch.setattr(
        operations_module,
        "_validate_qwen_cloud_runtime",
        lambda _runtime: "ffmpeg version 9.0-fixture",
    )
    return runtime


@pytest.fixture
def credential_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private temporary user home that is never the real one."""

    home = tmp_path / "cloud user home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_WORKSPACE_ID", raising=False)
    assert Path.home() == home
    return home


def configure_credential(api_key: str = API_KEY) -> None:
    write_credential(api_key=api_key, workspace_id=WORKSPACE_ID)


def cloud_project(
    tmp_path: Path,
    *,
    cloud: bool = True,
    source_id: str = "src_cloud",
    root_name: str = "cloud-project",
) -> tuple[Path, Path, SourceAsset]:
    root = tmp_path / root_name
    source_path = tmp_path / "source.wav"
    source_path.write_bytes(b"fixture-source")
    project = create_project(root, "Cloud")
    source = SourceAsset(
        source_id=source_id,
        kind="audio",
        display_name=source_path.name,
        import_mode=ImportMode.LINKED,
        locator={"absolute_path": str(source_path.resolve())},
        fingerprint=fingerprint_file(source_path),
        probe=MediaProbe(
            duration_ticks=SOURCE_DURATION_TICKS,
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
        tags=(ASR_CLOUD_TAG,) if cloud else (),
    )
    ProjectStore(root).save(replace(project, sources=(source,)), expected_revision=0)
    _authorize_transcription(root, source_id)
    return root, source_path, source


def _authorize_transcription(root: Path, source_id: str) -> None:
    started = workflow_start(root, "wfr_cloud", [source_id])
    basis = started.status["confirmation_bases"]["scope"]["basis"]
    workflow_action(
        root,
        "wfr_cloud",
        "act_scope_cloud",
        "approve_scope",
        {
            "schema_version": 1,
            "confirmation_basis": basis,
            "source_authorizations": [
                {
                    "source_id": source_id,
                    "transcribe": True,
                    "speaker_diarization": False,
                }
            ],
        },
    )


def json_response(status: int, payload: object) -> HttpResponse:
    return HttpResponse(
        status, json.dumps(payload, ensure_ascii=False).encode("utf-8")
    )


def task_output(status: str = "SUCCEEDED") -> HttpResponse:
    return json_response(
        200,
        {
            "output": {
                "task_id": TASK_ID,
                "task_status": status,
                "results": [
                    {
                        "subtask_status": "SUCCEEDED",
                        "transcription_url": SIGNED_RESULT_URL,
                    }
                ],
            }
        },
    )


class RecordingTransport:
    """Scripted transport; never touches the network."""

    def __init__(self, handler: Any) -> None:
        self.requests: list[HttpRequest] = []
        self._handler = handler

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        return self._handler(request)


def cloud_transport(**overrides: HttpResponse | None) -> RecordingTransport:
    queued = [
        json_response(200, {"output": {"task_status": "RUNNING"}}),
        task_output(),
    ]

    def handler(request: HttpRequest) -> HttpResponse:
        url = request.url
        if url.startswith(UPLOAD_POLICY_ENDPOINT):
            return overrides.get("policy") or json_response(200, {"data": POLICY_DATA})
        if url.startswith(UPLOAD_HOST):
            return overrides.get("upload") or HttpResponse(200, b"")
        if url.endswith(SUBMIT_PATH):
            return overrides.get("submit") or json_response(
                200, {"output": {"task_id": TASK_ID}}
            )
        if "/api/v1/tasks/" in url:
            return queued.pop(0) if len(queued) > 1 else queued[0]
        if url.startswith(RESULT_HOST):
            recognition = overrides.get("download")
            if recognition is not None:
                return recognition
            return json_response(
                200, json.loads(RECOGNITION_FIXTURE.read_text(encoding="utf-8"))
            )
        raise AssertionError(f"unexpected Cloud request: {url}")

    return RecordingTransport(handler)


class RecordingEncoder:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.paths: list[Path] = []
        self._error = error

    def __call__(self, source: Path, output: Path) -> FLACEncode:
        self.paths.append(output)
        if self._error is not None:
            raise self._error
        output.write_bytes(b"fLaC" + b"\x00" * 40)
        return FLACEncode(
            ffmpeg_path="fake-ffmpeg",
            ffmpeg_version="ffmpeg version fake",
            audio_stream="0:a:0",
            sample_rate_hz=16_000,
            channels=1,
            sample_format="s16",
            bits_per_sample=16,
            container_format="flac",
        )


def use_cloud_adapter(
    monkeypatch: pytest.MonkeyPatch,
    *,
    transport: RecordingTransport | None = None,
    encoder: RecordingEncoder | None = None,
    error: Exception | None = None,
    error_after_decode_start: bool = True,
    raw_payload: object | None = None,
    captured: dict[str, Any] | None = None,
) -> RecordingTransport:
    """Swap only the Cloud adapter callable.

    Production still builds the ``QwenFiletransConfig`` from the real credential
    store and the real persistent runtime, so the transport timeouts, the
    speaker flag and the FFmpeg selection under test are the production ones.
    Only the network, the encoder and the sleep function are fakes.

    ``error_after_decode_start`` mirrors where the failure really happens: a
    transport or worker failure occurs after the audio step began, while a
    stale execution basis is rejected before it, in
    ``transcription_preparing``.
    """

    traffic = transport or cloud_transport()
    recorder = encoder or RecordingEncoder()

    def call(
        source_path: Path,
        raw_output_path: Path,
        *,
        config: Any,
        phase_callback: Any = None,
    ) -> QwenFiletransRun:
        if captured is not None:
            captured["config"] = config
        if error is not None and not error_after_decode_start:
            raise error
        if phase_callback is not None:
            phase_callback("transcription_decoding_audio")
            phase_callback("transcription_running_asr")
        if error is not None:
            raise error
        if raw_payload is not None:
            raw_output_path.parent.mkdir(parents=True, exist_ok=True)
            raw_output_path.write_text(
                json.dumps(raw_payload, ensure_ascii=False), encoding="utf-8"
            )
            return QwenFiletransRun(
                started_at="2026-09-11T00:00:00+00:00",
                completed_at="2026-09-11T00:00:01+00:00",
                exit_status=0,
            )
        return run_qwen_filetrans(
            source_path,
            raw_output_path,
            config=config,
            transport=traffic,
            audio_encoder=recorder,
            sleep=lambda _seconds: None,
            phase_callback=None,
        )

    monkeypatch.setattr(transcription_module, "run_qwen_filetrans", call)
    return traffic


def run_cloud_operation(
    root: Path,
    *,
    operation_id: str,
    source_id: str = "src_cloud",
    expected_revision: int = 0,
    speaker_diarization: bool = False,
    timeout_milliseconds: int = 3_600_000,
) -> Any:
    return run_transcription_operation(
        root,
        operation_id=operation_id,
        source_id=source_id,
        expected_project_revision=expected_revision,
        speaker_diarization=speaker_diarization,
        timeout_milliseconds=timeout_milliseconds,
    )


def record_of(root: Path, operation_id: str) -> Any:
    record = MediaOperationStore(root, ProjectStore(root).load().project_id).read(
        operation_id
    )
    assert record is not None
    return record


# --------------------------------------------------------------------------
# §24 existing-operation-first
# --------------------------------------------------------------------------


def test_existing_operation_readback_ignores_every_changed_current_fact(
    tmp_path: Path,
    cloud_runtime: SimpleNamespace,
    credential_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del cloud_runtime, credential_home
    configure_credential()
    root, _source_path, source = cloud_project(tmp_path)
    transport = use_cloud_adapter(monkeypatch)

    first = run_cloud_operation(root, operation_id="op_cloud_readback")

    assert first.record.status == "succeeded"
    assert len(transport.requests) == 6

    # Everything the current basis could contribute is now hostile: the route
    # marker is gone, the credential is deleted, and the route resolver, the
    # credential read, the runtime preflight, the Cloud flow and the adapter all
    # explode if they are reached at all.
    (tmp_path / "source.wav").write_bytes(b"changed-after-success")
    project = ProjectStore(root).load()
    ProjectStore(root).save(
        replace(
            project,
            revision=project.revision + 1,
            sources=(replace(source, tags=()),),
        ),
        expected_revision=project.revision,
    )
    clear_credential()
    monkeypatch.setattr(
        operations_module,
        "resolve_asr_route",
        lambda _tags: pytest.fail("readback re-resolved the current route"),
    )
    monkeypatch.setattr(
        operations_module,
        "_load_persistent_runtime",
        lambda: pytest.fail("readback re-ran the runtime preflight"),
    )
    monkeypatch.setattr(
        operations_module,
        "_read_qwen_filetrans_credential",
        lambda: pytest.fail("readback read the current credential"),
    )
    monkeypatch.setattr(
        operations_module,
        "transcribe_source_qwen_filetrans",
        lambda *_args, **_kwargs: pytest.fail("readback restarted the Cloud run"),
    )
    monkeypatch.setattr(
        operations_module,
        "_validate_media_runtime",
        lambda _runtime: pytest.fail("readback revalidated the runtime"),
    )
    monkeypatch.setattr(
        transcription_module,
        "run_qwen_filetrans",
        lambda *_args, **_kwargs: pytest.fail("readback called the Qwen transport"),
    )

    second = run_cloud_operation(root, operation_id="op_cloud_readback")

    assert second.readback is True
    assert second.record == first.record
    assert second.result is None
    assert len(transport.requests) == 6


def test_historical_readback_survives_a_rotated_credential(
    tmp_path: Path,
    cloud_runtime: SimpleNamespace,
    credential_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del cloud_runtime, credential_home
    configure_credential()
    root, _source_path, _source = cloud_project(tmp_path)
    use_cloud_adapter(monkeypatch)
    first = run_cloud_operation(root, operation_id="op_cloud_rotated")

    configure_credential(ROTATED_API_KEY)
    monkeypatch.setattr(
        operations_module,
        "_read_qwen_filetrans_credential",
        lambda: pytest.fail("readback read the rotated credential"),
    )
    second = run_cloud_operation(root, operation_id="op_cloud_rotated")

    assert second.readback is True
    assert second.record == first.record


def test_same_operation_id_with_a_changed_stable_request_conflicts(
    tmp_path: Path,
    cloud_runtime: SimpleNamespace,
    credential_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del cloud_runtime, credential_home
    configure_credential()
    root, _source_path, _source = cloud_project(tmp_path)
    use_cloud_adapter(monkeypatch)
    run_cloud_operation(root, operation_id="op_cloud_conflict")

    with pytest.raises(MediaOperationError) as raised:
        run_cloud_operation(
            root, operation_id="op_cloud_conflict", speaker_diarization=True
        )

    assert raised.value.code == "operation_input_conflict"


# --------------------------------------------------------------------------
# §25 routing and local isolation
# --------------------------------------------------------------------------


def test_local_route_never_touches_the_cloud_boundary(
    tmp_path: Path,
    cloud_runtime: SimpleNamespace,
    credential_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del cloud_runtime, credential_home
    root, _source_path, source = cloud_project(tmp_path, cloud=False)
    calls = {"worker": 0}
    raw_bytes = json.dumps(
        [{"text": "合成", "sentence_info": [{"text": "甲", "start": 0, "end": 900}]}],
        ensure_ascii=False,
    ).encode("utf-8")

    def local_runner(_source_path: Path, raw_output: Path) -> FunASRRun:
        calls["worker"] += 1
        raw_output.parent.mkdir(parents=True, exist_ok=True)
        raw_output.write_bytes(raw_bytes)
        return FunASRRun(
            package_version="fixture",
            models={"asr": "fixture"},
            parameters={"fixture": True},
            started_at="2026-09-11T00:00:00+00:00",
            completed_at="2026-09-11T00:00:01+00:00",
            exit_status=0,
        )

    # Nothing on the local path may reach the Cloud boundary or the credential
    # store, and the local execution identity must be the unchanged one.
    monkeypatch.setattr(
        operations_module,
        "read_credential",
        lambda *_args, **_kwargs: pytest.fail(
            "the local route read the Qwen credential"
        ),
    )
    monkeypatch.setattr(
        operations_module,
        "transcribe_source_qwen_filetrans",
        lambda *_args, **_kwargs: pytest.fail(
            "the local route ran the Qwen Filetrans flow"
        ),
    )
    monkeypatch.setattr(
        operations_module,
        "QwenFiletransConfig",
        lambda *_args, **_kwargs: pytest.fail("the local route built a Cloud config"),
    )
    runtime = operations_module._load_persistent_runtime()
    expected_projection = operations_module._transcription_input_projection(
        ProjectStore(root).load(),
        source,
        runtime,
        speaker_diarization=False,
        timeout_milliseconds=3_600_000,
    )
    expected_hash = operations_module.hash_transcription_input(expected_projection)

    outcome = run_transcription_operation(
        root,
        operation_id="op_local_isolated",
        source_id=source.source_id,
        expected_project_revision=0,
        speaker_diarization=False,
        runner=local_runner,
    )

    assert outcome.record.status == "succeeded"
    assert calls["worker"] == 1
    assert outcome.record.input_hash == expected_hash


def test_cloud_route_selects_the_qwen_path_and_needs_no_funasr_models(
    tmp_path: Path,
    cloud_runtime: SimpleNamespace,
    credential_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del cloud_runtime, credential_home
    configure_credential()
    root, _source_path, _source = cloud_project(tmp_path)
    monkeypatch.setattr(
        operations_module,
        "_transcription_config",
        lambda *_args, **_kwargs: pytest.fail("the Cloud route built a FunASR config"),
    )
    monkeypatch.setattr(
        operations_module,
        "transcribe_source",
        lambda *_args, **_kwargs: pytest.fail(
            "the Cloud route ran the local FunASR flow"
        ),
    )
    transport = use_cloud_adapter(monkeypatch)

    outcome = run_cloud_operation(root, operation_id="op_cloud_route")

    assert outcome.record.status == "succeeded"
    assert len(transport.requests) == 6
    # The fixture runtime names model paths that do not exist on disk.
    assert not Path("/absent-funasr-model/asr").exists()


# --------------------------------------------------------------------------
# §26 credential boundary
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", ("not_configured", "invalid"))
def test_unusable_credential_creates_no_operation_and_no_evidence(
    tmp_path: Path,
    cloud_runtime: SimpleNamespace,
    credential_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    del cloud_runtime
    if status == "invalid":
        # A real store location whose record has a broken shape: build the
        # record the production way, then corrupt only its content.
        configure_credential()
        corrupted = credential_home / ".roughcut" / "private" / "qwen-filetrans.json"
        corrupted.write_text('{"format_version": 1}', encoding="utf-8")
    root, _source_path, _source = cloud_project(tmp_path)
    transport = use_cloud_adapter(monkeypatch)
    monkeypatch.setattr(
        operations_module,
        "transcribe_source_qwen_filetrans",
        lambda *_args, **_kwargs: pytest.fail(
            "an unusable credential started Cloud execution"
        ),
    )

    with pytest.raises(MediaOperationError) as raised:
        run_cloud_operation(root, operation_id="op_cloud_unusable")

    assert raised.value.code == status
    assert (
        MediaOperationStore(root, ProjectStore(root).load().project_id).read(
            "op_cloud_unusable"
        )
        is None
    )
    assert not (root / "raw-asr").exists()
    assert not (root / "transcripts").exists()
    assert transport.requests == []
    project = ProjectStore(root).load()
    assert project.revision == 0
    assert ASR_CLOUD_TAG in project.sources[0].tags


@pytest.mark.skipif(os.name == "nt", reason="POSIX file-mode boundary")
def test_insecure_credential_store_creates_no_operation(
    tmp_path: Path,
    cloud_runtime: SimpleNamespace,
    credential_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del cloud_runtime
    configure_credential()
    record = credential_home / ".roughcut" / "private" / "qwen-filetrans.json"
    os.chmod(record, 0o644)
    root, _source_path, _source = cloud_project(tmp_path)
    transport = use_cloud_adapter(monkeypatch)

    with pytest.raises(MediaOperationError) as raised:
        run_cloud_operation(root, operation_id="op_cloud_insecure")

    assert raised.value.code == "insecure"
    assert (
        MediaOperationStore(root, ProjectStore(root).load().project_id).read(
            "op_cloud_insecure"
        )
        is None
    )
    assert not (root / "raw-asr").exists()
    assert transport.requests == []


def test_environment_credential_is_never_a_production_fallback(
    tmp_path: Path,
    cloud_runtime: SimpleNamespace,
    credential_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del cloud_runtime, credential_home
    monkeypatch.setenv("DASHSCOPE_API_KEY", API_KEY)
    monkeypatch.setenv("DASHSCOPE_WORKSPACE_ID", WORKSPACE_ID)
    root, _source_path, _source = cloud_project(tmp_path)
    transport = use_cloud_adapter(monkeypatch)

    with pytest.raises(MediaOperationError) as raised:
        run_cloud_operation(root, operation_id="op_cloud_env")

    assert raised.value.code == "not_configured"
    assert not (root / "raw-asr").exists()
    assert not (root / "transcripts").exists()
    assert transport.requests == []


def test_provider_rejected_configured_credential_is_a_runtime_failure(
    tmp_path: Path,
    cloud_runtime: SimpleNamespace,
    credential_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del cloud_runtime, credential_home
    configure_credential()
    root, _source_path, _source = cloud_project(tmp_path)
    use_cloud_adapter(
        monkeypatch,
        transport=cloud_transport(policy=json_response(401, {"code": "InvalidApiKey"})),
    )

    with pytest.raises(QwenFiletransError):
        run_cloud_operation(root, operation_id="op_cloud_401")

    record = record_of(root, "op_cloud_401")
    assert record.status == "failed"
    assert record.error is not None
    assert (record.error.responsibility, record.error.action) == (
        "asr_worker",
        "run_asr_worker",
    )
    assert record.error.message_code == "transcription_failed"
    # A configured-but-rejected credential is a runtime failure, not a
    # setup/readiness state, and it must not become a generic core validation
    # failure.
    assert record.error.responsibility != "roughcut_core"
    assert record.error.action != "validate_transcription_basis"


# --------------------------------------------------------------------------
# §27 Cloud closed execution input identity
# --------------------------------------------------------------------------


def _projection(
    root: Path,
    source: SourceAsset,
    runtime: SimpleNamespace,
    *,
    workspace_id: str = WORKSPACE_ID,
    speaker_diarization: bool = False,
    timeout_milliseconds: int = 3_600_000,
) -> dict[str, object]:
    return _qwen_filetrans_input_projection(
        ProjectStore(root).load(),
        source,
        runtime,  # type: ignore[arg-type]
        workspace_id=workspace_id,
        speaker_diarization=speaker_diarization,
        timeout_milliseconds=timeout_milliseconds,
    )


def test_cloud_projection_is_closed_and_binds_the_fixed_identity(
    tmp_path: Path,
    cloud_runtime: SimpleNamespace,
) -> None:
    root, _source_path, source = cloud_project(tmp_path)
    projection = _projection(root, source, cloud_runtime)

    assert set(projection) == {
        "input_schema_version",
        "input_kind",
        "operation_type",
        "backend",
        "project_id",
        "source_ref",
        "expected_project_revision",
        "cloud_config",
    }
    assert set(projection["source_ref"]) == {  # type: ignore[arg-type]
        "source_id",
        "source_snapshot_hash",
    }
    cloud_config = projection["cloud_config"]
    assert isinstance(cloud_config, dict)
    assert set(cloud_config) == {
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
    # The Cloud identity is distinct from the local one by construction.
    assert projection["input_kind"] == "qwen_filetrans_transcription_input"
    assert projection["operation_type"] == "transcribe_source"
    assert projection["backend"] == BACKEND == "qwen_filetrans"
    assert cloud_config["audio_profile"] == CLOUD_AUDIO_PROFILE == "16_khz_mono_flac"
    assert cloud_config["region"] == REGION == "cn-beijing"
    assert cloud_config["model"] == MODEL_NAME == "qwen-audio-3.0-asr-flash-filetrans"
    assert cloud_config["transport"] == "temporary_upload"
    assert cloud_config["workspace_id"] == WORKSPACE_ID
    assert cloud_config["speaker_diarization"] is False
    assert cloud_config["timeout_milliseconds"] == 3_600_000

    # No credential, locator or provider task identity can enter the identity.
    serialized = json.dumps(projection, ensure_ascii=False, sort_keys=True)
    for canary in TRANSPORT_CANARIES:
        assert canary not in serialized
    for forbidden in ("api_key", "Authorization", "http", "url", "task_id"):
        assert forbidden not in serialized
    assert _qwen_filetrans_input_hash(projection) == _qwen_filetrans_input_hash(
        _projection(root, source, cloud_runtime)
    )


def test_cloud_projection_rejects_a_non_closed_shape(
    tmp_path: Path,
    cloud_runtime: SimpleNamespace,
) -> None:
    root, _source_path, source = cloud_project(tmp_path)
    projection = _projection(root, source, cloud_runtime)
    projection["api_key"] = API_KEY

    with pytest.raises(MediaOperationError) as raised:
        _qwen_filetrans_input_hash(projection)

    assert raised.value.code == "operation_integrity_error"


@pytest.mark.parametrize(
    "mutation",
    (
        "source_snapshot",
        "revision",
        "ffmpeg_selection",
        "workspace_id",
        "speaker_diarization",
        "timeout",
    ),
)
def test_cloud_input_hash_changes_with_each_execution_fact(
    tmp_path: Path,
    cloud_runtime: SimpleNamespace,
    mutation: str,
) -> None:
    root, _source_path, source = cloud_project(tmp_path)
    known = _projection(root, source, cloud_runtime)
    assert _qwen_filetrans_input_hash(known) == _qwen_filetrans_input_hash(
        _projection(root, source, cloud_runtime)
    )

    runtime = cloud_runtime
    if mutation == "source_snapshot":
        source = replace(source, note="changed note")
    elif mutation == "revision":
        project = ProjectStore(root).load()
        ProjectStore(root).save(
            replace(project, revision=project.revision + 1),
            expected_revision=project.revision,
        )
    elif mutation == "ffmpeg_selection":
        runtime = _runtime(ffmpeg_tool_selection_hash="9" * 64)

    changed = _projection(
        root,
        source,
        runtime,
        workspace_id=(
            "otherworkspace01" if mutation == "workspace_id" else WORKSPACE_ID
        ),
        speaker_diarization=mutation == "speaker_diarization",
        timeout_milliseconds=(60_000 if mutation == "timeout" else 3_600_000),
    )

    assert _qwen_filetrans_input_hash(changed) != _qwen_filetrans_input_hash(known)


def test_cloud_operation_persists_the_closed_projection_hash(
    tmp_path: Path,
    cloud_runtime: SimpleNamespace,
    credential_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del credential_home
    configure_credential()
    root, _source_path, source = cloud_project(tmp_path)
    use_cloud_adapter(monkeypatch)
    expected_hash = _qwen_filetrans_input_hash(
        _projection(root, source, cloud_runtime)
    )

    outcome = run_cloud_operation(root, operation_id="op_cloud_identity")

    assert outcome.record.input_hash == expected_hash
    serialized = json.dumps(
        outcome.record.to_dict(), ensure_ascii=False, sort_keys=True
    )
    for canary in TRANSPORT_CANARIES:
        assert canary not in serialized
    assert "oss://" not in serialized
    assert "https://" not in serialized
    assert outcome.record.schema_version == 1
    assert outcome.record.operation_type == "transcribe_source"
    # No new persisted MediaOperation field was added for Cloud.
    assert set(outcome.record.to_dict()) == {
        "operation_id",
        "scope",
        "operation_type",
        "request_hash",
        "input_hash",
        "status",
        "phase_message_code",
        "created_at",
        "started_at",
        "updated_at",
        "finished_at",
        "result_ref",
        "error",
        "schema_version",
    }


def test_api_key_rotation_does_not_change_the_cloud_input_hash(
    tmp_path: Path,
    cloud_runtime: SimpleNamespace,
    credential_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del credential_home
    configure_credential()
    root, _source_path, source = cloud_project(tmp_path)
    use_cloud_adapter(
        monkeypatch,
        transport=cloud_transport(upload=json_response(500, {"code": "InternalError"})),
    )

    with pytest.raises(QwenFiletransError):
        run_cloud_operation(root, operation_id="op_cloud_key_a")
    first = record_of(root, "op_cloud_key_a")
    assert first.status == "failed"
    # A failed operation is terminal and leaves the Project revision untouched,
    # so the same execution basis can be revisited with the rotated credential.
    assert ProjectStore(root).load().revision == 0

    configure_credential(ROTATED_API_KEY)
    use_cloud_adapter(monkeypatch)
    second = run_cloud_operation(root, operation_id="op_cloud_key_b")

    assert second.record.status == "succeeded"
    assert second.record.input_hash == first.input_hash
    # The API Key is not even an input of the projection builder.
    assert "api_key" not in _projection(root, source, cloud_runtime)["cloud_config"]


# --------------------------------------------------------------------------
# §28 failure mapping
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("phase", "category", "status", "action"),
    (
        ("credential", "credential_rejected", 401, "run_asr_worker"),
        ("credential", "credential_rejected", 403, "run_asr_worker"),
        ("upload_policy", "credential_rejected", 401, "run_asr_worker"),
        ("upload_policy", "http_failure", 503, "run_asr_worker"),
        ("upload_policy", "network_failure", None, "run_asr_worker"),
        ("upload", "http_failure", 500, "run_asr_worker"),
        ("upload", "network_failure", None, "run_asr_worker"),
        ("submit", "credential_rejected", 401, "run_asr_worker"),
        ("submit", "http_failure", 503, "run_asr_worker"),
        ("submit", "malformed_response", 200, "run_asr_worker"),
        ("poll", "task_failed", 200, "run_asr_worker"),
        ("poll", "subtask_failed", 200, "run_asr_worker"),
        ("poll", "poll_timeout", 200, "run_asr_worker"),
        ("poll", "network_failure", None, "run_asr_worker"),
        ("download", "http_failure", 404, "run_asr_worker"),
        ("recognition_evidence", "unsafe_provider_shape", 200, "run_asr_worker"),
        ("recognition_evidence", "malformed_response", 200, "run_asr_worker"),
        ("audio_preparation", "audio_preparation_failed", None, "decode_transcription_audio"),
    ),
)
def test_cloud_transport_failure_mapping_is_exact(
    tmp_path: Path,
    cloud_runtime: SimpleNamespace,
    credential_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    category: str,
    status: int | None,
    action: str,
) -> None:
    del cloud_runtime, credential_home
    configure_credential()
    root, _source_path, _source = cloud_project(tmp_path)
    use_cloud_adapter(
        monkeypatch,
        error=QwenFiletransError(phase, category, http_status=status),
    )

    with pytest.raises(QwenFiletransError):
        run_cloud_operation(root, operation_id="op_cloud_mapping")

    record = record_of(root, "op_cloud_mapping")
    assert record.status == "failed"
    assert record.error is not None
    assert record.error.responsibility == "asr_worker"
    assert record.error.action == action
    assert record.error.message_code == "transcription_failed"
    assert record.phase_message_code == "transcription_failed"


def test_real_adapter_rejections_keep_the_worker_boundary(
    tmp_path: Path,
    cloud_runtime: SimpleNamespace,
    credential_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del cloud_runtime, credential_home
    configure_credential()
    root, _source_path, _source = cloud_project(tmp_path)

    # 403 from the real transport inside the real adapter.
    use_cloud_adapter(
        monkeypatch,
        transport=cloud_transport(upload=json_response(403, {"code": "AccessDenied"})),
    )
    with pytest.raises(QwenFiletransError):
        run_cloud_operation(root, operation_id="op_cloud_real_403")
    rejected = record_of(root, "op_cloud_real_403")
    assert rejected.error is not None
    assert (rejected.error.responsibility, rejected.error.action) == (
        "asr_worker",
        "run_asr_worker",
    )

    # A real FLAC preparation failure inside the real adapter.
    use_cloud_adapter(
        monkeypatch,
        encoder=RecordingEncoder(
            error=FFmpegAudioError("Roughcut FLAC encode failed")
        ),
    )
    with pytest.raises(QwenFiletransError):
        run_cloud_operation(root, operation_id="op_cloud_real_flac")
    flac = record_of(root, "op_cloud_real_flac")
    assert flac.error is not None
    assert (flac.error.responsibility, flac.error.action) == (
        "asr_worker",
        "decode_transcription_audio",
    )


def test_normalization_failure_is_a_core_responsibility(
    tmp_path: Path,
    cloud_runtime: SimpleNamespace,
    credential_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del cloud_runtime, credential_home
    configure_credential()
    root, _source_path, _source = cloud_project(tmp_path)
    # A normalizable-but-invalid recognition payload: the sentence ends far
    # beyond the Source duration.
    use_cloud_adapter(
        monkeypatch,
        raw_payload={
            "transcripts": [
                {
                    "sentences": [
                        {
                            "begin_time": 0,
                            "end_time": 9_000_000,
                            "text": "越界",
                            "words": [
                                {"begin_time": 0, "end_time": 9_000_000, "text": "越界"}
                            ],
                        }
                    ]
                }
            ]
        },
    )

    with pytest.raises(QwenNormalizationError):
        run_cloud_operation(root, operation_id="op_cloud_normalize")

    record = record_of(root, "op_cloud_normalize")
    assert record.status == "failed"
    assert record.error is not None
    assert (record.error.responsibility, record.error.action) == (
        "roughcut_core",
        "normalize_transcript",
    )
    assert record.error.message_code == "transcription_failed"


def test_publish_failure_is_a_core_responsibility(
    tmp_path: Path,
    cloud_runtime: SimpleNamespace,
    credential_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del cloud_runtime, credential_home
    configure_credential()
    root, _source_path, _source = cloud_project(tmp_path)
    use_cloud_adapter(monkeypatch)
    monkeypatch.setattr(
        transcription_module,
        "_write_json_atomically",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("publish failed")),
    )

    with pytest.raises(OSError):
        run_cloud_operation(root, operation_id="op_cloud_publish")

    record = record_of(root, "op_cloud_publish")
    assert record.status == "failed"
    assert record.error is not None
    assert (record.error.responsibility, record.error.action) == (
        "roughcut_core",
        "publish_transcript",
    )
    assert record.error.message_code == "transcription_failed"


def test_binding_synchronization_failure_is_a_core_responsibility(
    tmp_path: Path,
    cloud_runtime: SimpleNamespace,
    credential_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del cloud_runtime, credential_home
    configure_credential()
    root, _source_path, _source = cloud_project(tmp_path)
    use_cloud_adapter(monkeypatch)
    monkeypatch.setattr(
        operations_module,
        "synchronize_workflow_transcript_binding",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            WorkflowError("workflow_stale", "Roughcut workflow binding is stale")
        ),
    )

    with pytest.raises(WorkflowError):
        run_cloud_operation(root, operation_id="op_cloud_binding")

    record = record_of(root, "op_cloud_binding")
    assert record.status == "failed"
    assert record.error is not None
    assert (record.error.responsibility, record.error.action) == (
        "roughcut_core",
        "synchronize_transcript_binding",
    )
    assert record.error.message_code == "transcription_failed"


def test_execution_time_stale_basis_maps_to_user_input(
    tmp_path: Path,
    cloud_runtime: SimpleNamespace,
    credential_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del cloud_runtime, credential_home
    configure_credential()
    root, _source_path, _source = cloud_project(tmp_path)
    # The stale basis is rejected before any decode starts, which is where the
    # Cloud basis guards run.
    use_cloud_adapter(
        monkeypatch,
        error=ProjectError("project revision conflict"),
        error_after_decode_start=False,
    )

    with pytest.raises(ProjectError):
        run_cloud_operation(root, operation_id="op_cloud_stale")

    record = record_of(root, "op_cloud_stale")
    assert record.status == "failed"
    assert record.error is not None
    assert (record.error.responsibility, record.error.action) == (
        "user_input",
        "validate_transcription_basis",
    )
    assert record.error.message_code == "transcription_failed"


# --------------------------------------------------------------------------
# §19 / §29 authorization, disclosure and speaker diarization
# --------------------------------------------------------------------------


def test_route_mutation_after_a_local_approval_requires_reconfirmation(
    tmp_path: Path,
    cloud_runtime: SimpleNamespace,
    credential_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del cloud_runtime, credential_home
    configure_credential()
    root, _source_path, source = cloud_project(tmp_path, cloud=False)
    transport = use_cloud_adapter(monkeypatch)

    # The route marker is added after the local scope was approved, so the
    # Source snapshot and Project revision change and the old approval is stale.
    project = ProjectStore(root).load()
    ProjectStore(root).save(
        replace(
            project,
            revision=project.revision + 1,
            sources=(replace(source, tags=(ASR_CLOUD_TAG,)),),
        ),
        expected_revision=project.revision,
    )

    with pytest.raises(WorkflowError) as raised:
        run_cloud_operation(root, operation_id="op_cloud_stale_scope")

    assert raised.value.code == "workflow_stale"
    assert (
        MediaOperationStore(root, ProjectStore(root).load().project_id).read(
            "op_cloud_stale_scope"
        )
        is None
    )
    assert transport.requests == []
    assert not (root / "raw-asr").exists()


@pytest.mark.parametrize("speaker_diarization", (False, True))
def test_speaker_diarization_reaches_the_provider_request(
    tmp_path: Path,
    cloud_runtime: SimpleNamespace,
    credential_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    speaker_diarization: bool,
) -> None:
    del cloud_runtime, credential_home
    configure_credential()
    root, _source_path, source = cloud_project(tmp_path)
    # Speaker diarization is one parameter of the same already-confirmed scope,
    # so the scope is re-approved on the same run instead of starting a second
    # one or asking for a second confirmation gate.
    status = workflow_status(root, "wfr_cloud")
    basis = status["confirmation_bases"]["scope"]["basis"]
    workflow_action(
        root,
        "wfr_cloud",
        "act_scope_speakers",
        "approve_scope",
        {
            "schema_version": 1,
            "confirmation_basis": basis,
            "source_authorizations": [
                {
                    "source_id": source.source_id,
                    "transcribe": True,
                    "speaker_diarization": True,
                }
            ],
        },
    )
    transport = use_cloud_adapter(monkeypatch)

    outcome = run_cloud_operation(
        root,
        operation_id="op_cloud_speakers",
        speaker_diarization=speaker_diarization,
    )

    assert outcome.record.status == "succeeded"
    submit_body = json.loads(transport.requests[2].body.decode("utf-8"))
    assert submit_body["parameters"]["diarization_enabled"] is speaker_diarization
    assert submit_body["parameters"]["channel_id"] == [0]
    assert submit_body["parameters"]["language_hints"] == ["zh", "en"]
    assert "speaker_count" not in submit_body["parameters"]


def test_diarization_and_timeout_change_the_cloud_input_hash(
    tmp_path: Path,
    cloud_runtime: SimpleNamespace,
) -> None:
    root, _source_path, source = cloud_project(tmp_path)
    plain = _qwen_filetrans_input_hash(_projection(root, source, cloud_runtime))
    diarized = _qwen_filetrans_input_hash(
        _projection(root, source, cloud_runtime, speaker_diarization=True)
    )
    shorter = _qwen_filetrans_input_hash(
        _projection(root, source, cloud_runtime, timeout_milliseconds=1_000)
    )

    assert len({plain, diarized, shorter}) == 3


# --------------------------------------------------------------------------
# §12 timeout mapping and §30 downstream publication
# --------------------------------------------------------------------------


def test_public_timeout_is_the_cloud_execution_budget(
    tmp_path: Path,
    cloud_runtime: SimpleNamespace,
    credential_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del credential_home
    configure_credential()
    root, _source_path, _source = cloud_project(tmp_path)
    captured: dict[str, Any] = {}
    use_cloud_adapter(monkeypatch, captured=captured)

    run_cloud_operation(
        root, operation_id="op_cloud_timeout", timeout_milliseconds=90_000
    )

    config = captured["config"]
    assert config.request_timeout_seconds == 90.0
    assert config.upload_timeout_seconds == 90.0
    assert config.poll_timeout_seconds == 90.0
    assert config.ffmpeg_command == "/fixture/ffmpeg"


def test_cloud_route_ignores_ffprobe_while_the_local_route_still_requires_it(
    tmp_path: Path,
    credential_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Cloud basis binds FFmpeg only, so ffprobe must not decide it.

    Both routes run the real runtime validators here; only the child-process
    runner is faked, and it reports a healthy FFmpeg with an unavailable
    ffprobe.  The Cloud execution identity contains no ffprobe fact and the
    Cloud path never invokes ffprobe, so the same basis must still run, while the
    unchanged local pair preflight keeps failing closed.
    """

    del credential_home
    configure_credential()
    runtime = _runtime()
    monkeypatch.setattr(
        operations_module, "_load_persistent_runtime", lambda: runtime
    )
    seen: list[list[str]] = []

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        seen.append(list(command))
        if command[0].endswith("ffprobe"):
            raise OSError("ffprobe is not available")
        return subprocess.CompletedProcess(
            command, 0, "ffmpeg version 9.0-fixture\n", ""
        )

    monkeypatch.setattr(operations_module, "_runtime_version_runner", lambda: runner)
    cloud_root, _source_path, _source = cloud_project(tmp_path)
    use_cloud_adapter(monkeypatch)

    outcome = run_cloud_operation(cloud_root, operation_id="op_cloud_no_ffprobe")

    assert outcome.record.status == "succeeded"
    assert seen == [["ffmpeg", "-version"]]

    seen.clear()
    local_root, _local_source_path, _local_source = cloud_project(
        tmp_path, cloud=False, root_name="local-project"
    )
    with pytest.raises(RuntimeBindingError):
        run_transcription_operation(
            local_root,
            operation_id="op_local_needs_pair",
            source_id="src_cloud",
            expected_project_revision=0,
            speaker_diarization=False,
        )

    assert seen == [["ffmpeg", "-version"], ["ffprobe", "-version"]]


def test_cloud_route_still_rejects_a_drifted_ffmpeg(
    tmp_path: Path,
    credential_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del credential_home
    configure_credential()
    monkeypatch.setattr(
        operations_module, "_load_persistent_runtime", lambda: _runtime()
    )

    def drifted(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command, 0, "ffmpeg version 9.1-other\n", ""
        )

    monkeypatch.setattr(
        operations_module, "_runtime_version_runner", lambda: drifted
    )
    root, _source_path, _source = cloud_project(tmp_path)
    use_cloud_adapter(monkeypatch)

    with pytest.raises(RuntimeBindingError):
        run_cloud_operation(root, operation_id="op_cloud_ffmpeg_drift")

    record = record_of(root, "op_cloud_ffmpeg_drift")
    assert record.status == "failed"
    assert record.error is not None
    assert (record.error.responsibility, record.error.action) == (
        "roughcut_core",
        "validate_transcription_basis",
    )


def test_cloud_success_enters_the_existing_downstream_pipeline(
    tmp_path: Path,
    cloud_runtime: SimpleNamespace,
    credential_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del cloud_runtime, credential_home
    configure_credential()
    root, _source_path, source = cloud_project(tmp_path)
    stage_before = WorkflowStore(root).read_run("wfr_cloud").stage
    transport = use_cloud_adapter(monkeypatch)

    outcome = run_cloud_operation(root, operation_id="op_cloud_publish_ok")

    assert outcome.record.status == "succeeded"
    assert outcome.record.phase_message_code == "transcription_succeeded"
    assert outcome.record.schema_version == 1
    result = outcome.record.result_ref
    assert isinstance(result, TranscriptOperationResult)
    assert result.source_id == source.source_id
    assert result.project_revision == 1

    project = ProjectStore(root).load()
    assert project.schema_version == 1
    assert project.revision == 1
    assert project.active_transcript_versions == {
        source.source_id: result.transcript_version_id
    }

    transcript_path = (
        root / "transcripts" / source.source_id / f"{result.transcript_version_id}.json"
    )
    payload = json.loads(transcript_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["source_id"] == source.source_id
    assert payload["provenance"]["backend"] == BACKEND
    assert payload["provenance"]["raw_result_path"].startswith("raw-asr/")
    assert payload["provenance"]["parameters"]["transport"] == "temporary_upload"
    assert payload["segments"][0]["original_text"] == TRANSCRIPT_TEXT
    assert payload["segments"][0]["fine_units"][0]["text"] == "你好，"
    assert payload["segments"][0]["local_speaker_id"] == "spk_0"
    assert payload["segments"][0]["confidence"] is None
    serialized_transcript = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    for canary in TRANSPORT_CANARIES:
        assert canary not in serialized_transcript

    bound = WorkflowStore(root).read_run("wfr_cloud")
    assert bound.stage == stage_before
    assert bound.ordered_bindings[0].transcript_version_id == (
        result.transcript_version_id
    )

    evidence = list((root / "raw-asr" / source.source_id).glob("*.json"))
    assert len(evidence) == 1
    serialized = evidence[0].read_text(encoding="utf-8")
    for canary in TRANSPORT_CANARIES:
        assert canary not in serialized
    assert "oss://" not in serialized
    assert "file_url" not in serialized
    assert transport.requests


def test_cloud_operation_survives_spaces_and_non_ascii_paths(
    tmp_path: Path,
    cloud_runtime: SimpleNamespace,
    credential_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del cloud_runtime, credential_home
    configure_credential()
    root, _source_path, source = cloud_project(
        tmp_path, root_name="方言 项目 cloud"
    )
    use_cloud_adapter(monkeypatch)

    outcome = run_cloud_operation(root, operation_id="op_cloud_unicode")

    assert outcome.record.status == "succeeded"
    assert ProjectStore(root).load().active_transcript_versions == {
        source.source_id: outcome.record.result_ref.transcript_version_id
    }


def test_cloud_operation_is_reachable_through_the_unchanged_public_arguments(
    tmp_path: Path,
    cloud_runtime: SimpleNamespace,
    credential_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del cloud_runtime, credential_home
    configure_credential()
    root, _source_path, source = cloud_project(tmp_path)
    use_cloud_adapter(monkeypatch)

    response = roughcut.mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": "wp3b",
            "method": "tools/call",
            "params": {
                "name": "transcribe_source",
                "arguments": {
                    "project_path": str(root),
                    "operation_id": "op_cloud_mcp",
                    "source_id": source.source_id,
                    "expected_revision": 0,
                },
            },
        }
    )

    assert response is not None
    payload = response["result"]["structuredContent"]
    assert payload["media_operation"]["status"] == "succeeded"
    assert payload["operation_readback"] is False

    tool = next(
        item for item in roughcut.mcp.TOOLS if item["name"] == "transcribe_source"
    )
    assert set(tool["inputSchema"]["properties"]) == {
        "project_path",
        "operation_id",
        "source_id",
        "expected_revision",
        "speaker_diarization",
    }
    assert tool["description"] == (
        "Transcribe one authorized Source using its canonical ASR route."
    )
    serialized_tool = json.dumps(tool, ensure_ascii=False)
    for forbidden in ("--backend", "--cloud", "--qwen", "--dialect", "FunASR"):
        assert forbidden not in serialized_tool
