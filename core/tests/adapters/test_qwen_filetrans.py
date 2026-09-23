from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Any, ClassVar, Self
from urllib.error import HTTPError, URLError

import pytest

from roughcut.adapters.ffmpeg.audio import FFmpegAudioError, FLACEncode
from roughcut.adapters.qwen import credential_fields_are_valid, filetrans
from roughcut.adapters.qwen.filetrans import (
    MODEL_NAME,
    QWEN_FAILURE_CATEGORIES,
    QWEN_FAILURE_PHASES,
    SUBMIT_PATH,
    UPLOAD_POLICY_ENDPOINT,
    HttpRequest,
    HttpResponse,
    QwenFiletransConfig,
    QwenFiletransError,
    run_qwen_filetrans,
    sanitize_recognition_payload,
    urllib_transport,
)

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "fixtures" / "asr"

API_KEY = "fake-api-key-canary-0001"
WORKSPACE_ID = "fakeworkspace01"
UPLOAD_ACCESS_KEY_ID = "fake-upload-access-key-id"
UPLOAD_SIGNATURE = "fake-upload-signature-canary"
UPLOAD_POLICY = "fake-upload-policy-token-canary"
# Both hosts are the S0-observed cn-beijing Aliyun OSS shape.
UPLOAD_HOST = "https://dashscope-file-mgr.oss-cn-beijing.aliyuncs.com"
RESULT_HOST = "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com"
SIGNED_RESULT_URL = (
    f"{RESULT_HOST}/prod/fun-asr/20260910/fake-result-object-canary.json"
    "?Expires=1789140747&OSSAccessKeyId=fake-result-access-key-canary"
    "&Signature=fake-signature-canary"
)
TASK_ID = "fake-task-id-0001"
OSS_LOCATOR = "oss://dashscope-tmp/fakeworkspace01/audio.flac"
TRANSCRIPT_TEXT = "你好，世界。"
REDACTION_CANARIES = (
    API_KEY,
    UPLOAD_ACCESS_KEY_ID,
    UPLOAD_SIGNATURE,
    UPLOAD_POLICY,
    "fake-signature-canary",
    "fake-result-object-canary",
    "fake-result-access-key-canary",
    "oss://",
    TASK_ID,
    TRANSCRIPT_TEXT,
)

POLICY_DATA = {
    "upload_host": UPLOAD_HOST,
    "upload_dir": "dashscope-tmp/fakeworkspace01",
    "oss_access_key_id": UPLOAD_ACCESS_KEY_ID,
    "signature": UPLOAD_SIGNATURE,
    "policy": UPLOAD_POLICY,
    "x_oss_object_acl": "private",
    "x_oss_forbid_overwrite": "true",
    "expire_in_seconds": 300,
}


def load_recognition() -> dict[str, Any]:
    payload = json.loads(
        (FIXTURES / "qwen_filetrans_recognition.json").read_text(encoding="utf-8")
    )
    assert isinstance(payload, dict)
    return payload


def recognition_sentences(raw: dict[str, Any]) -> list[dict[str, Any]]:
    sentences = raw["transcripts"][0]["sentences"]
    assert isinstance(sentences, list)
    return sentences


def recognition_words(raw: dict[str, Any], sentence_index: int) -> list[dict[str, Any]]:
    words = recognition_sentences(raw)[sentence_index]["words"]
    assert isinstance(words, list)
    return words


def json_response(status: int, payload: object) -> HttpResponse:
    return HttpResponse(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def task_output(**overrides: object) -> HttpResponse:
    output: dict[str, object] = {
        "task_id": TASK_ID,
        "task_status": "SUCCEEDED",
        "results": [
            {"subtask_status": "SUCCEEDED", "transcription_url": SIGNED_RESULT_URL}
        ],
    }
    output.update(overrides)
    return json_response(200, {"output": output})


class RecordingTransport:
    """Scripted transport; never touches the network."""

    def __init__(self, handler: Callable[[HttpRequest], HttpResponse]) -> None:
        self.requests: list[HttpRequest] = []
        self._handler = handler

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        return self._handler(request)


def cloud_transport(
    recognition: object,
    *,
    policy: HttpResponse | None = None,
    upload: HttpResponse | None = None,
    submit: HttpResponse | None = None,
    polls: list[HttpResponse] | None = None,
    download: HttpResponse | None = None,
) -> RecordingTransport:
    queued = list(
        polls
        or [
            json_response(200, {"output": {"task_status": "RUNNING"}}),
            task_output(),
        ]
    )

    def handler(request: HttpRequest) -> HttpResponse:
        url = request.url
        if url.startswith(UPLOAD_POLICY_ENDPOINT):
            return policy if policy is not None else json_response(200, {"data": POLICY_DATA})
        if url.startswith(UPLOAD_HOST):
            return upload if upload is not None else HttpResponse(200, b"",)
        if url.endswith(SUBMIT_PATH):
            return (
                submit
                if submit is not None
                else json_response(200, {"output": {"task_id": TASK_ID}})
            )
        if "/api/v1/tasks/" in url:
            return queued.pop(0) if len(queued) > 1 else queued[0]
        if url.startswith(RESULT_HOST):
            return download if download is not None else json_response(200, recognition)
        raise AssertionError("unexpected request")

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


class RecordingSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def default_config(**overrides: object) -> QwenFiletransConfig:
    settings: dict[str, object] = {
        "api_key": API_KEY,
        "workspace_id": WORKSPACE_ID,
        "poll_interval_seconds": 2.0,
        "poll_timeout_seconds": 300.0,
    }
    settings.update(overrides)
    return QwenFiletransConfig(**settings)  # type: ignore[arg-type]


def raw_path(tmp_path: Path) -> Path:
    return tmp_path / "raw-asr" / "src_fixture" / "run.json"


def run_cloud(
    tmp_path: Path,
    *,
    transport: RecordingTransport,
    config: QwenFiletransConfig | None = None,
    encoder: RecordingEncoder | None = None,
    sleep: RecordingSleep | None = None,
    phase_callback: Callable[[str], None] | None = None,
) -> tuple[filetrans.QwenFiletransRun, Path]:
    source = tmp_path / "source.mov"
    source.write_bytes(b"synthetic-fixture")
    output = raw_path(tmp_path)
    run = run_qwen_filetrans(
        source,
        output,
        config=config or default_config(),
        transport=transport,
        audio_encoder=encoder or RecordingEncoder(),
        sleep=sleep or RecordingSleep(),
        phase_callback=phase_callback,
    )
    return run, output


def request_counts(transport: RecordingTransport) -> dict[str, int]:
    counts = {"policy": 0, "upload": 0, "submit": 0, "poll": 0, "download": 0}
    for request in transport.requests:
        if request.url.startswith(UPLOAD_POLICY_ENDPOINT):
            counts["policy"] += 1
        elif request.url.startswith(UPLOAD_HOST):
            counts["upload"] += 1
        elif request.url.endswith(SUBMIT_PATH):
            counts["submit"] += 1
        elif "/api/v1/tasks/" in request.url:
            counts["poll"] += 1
        elif request.url.startswith(RESULT_HOST):
            counts["download"] += 1
    return counts


def assert_error_is_redacted(error: QwenFiletransError) -> None:
    surfaces = (
        str(error),
        repr(error),
        f"{error.phase} {error.category} {error.http_status}",
    )
    for canary in REDACTION_CANARIES:
        for surface in surfaces:
            assert canary not in surface
    # No provider-supplied string may reach an error attribute at all.
    assert not hasattr(error, "provider_code")


def test_happy_path_uses_the_s0_verified_transport_shape(tmp_path: Path) -> None:
    transport = cloud_transport(load_recognition())
    encoder = RecordingEncoder()
    sleep = RecordingSleep()
    phases: list[str] = []

    run, output = run_cloud(
        tmp_path,
        transport=transport,
        encoder=encoder,
        sleep=sleep,
        phase_callback=phases.append,
    )

    assert phases == ["transcription_decoding_audio", "transcription_running_asr"]
    assert [request.method for request in transport.requests] == [
        "GET",
        "POST",
        "POST",
        "GET",
        "GET",
        "GET",
    ]
    assert request_counts(transport) == {
        "policy": 1,
        "upload": 1,
        "submit": 1,
        "poll": 2,
        "download": 1,
    }
    # Temporary upload and Filetrans submit share one exact model identity.
    assert f"model={MODEL_NAME}" in transport.requests[0].url
    assert json.loads(transport.requests[2].body.decode("utf-8"))["model"] == MODEL_NAME

    submit_headers = transport.requests[2].headers
    assert submit_headers["X-DashScope-Async"] == "enable"
    assert submit_headers["X-DashScope-OssResourceResolve"] == "enable"
    assert submit_headers["Authorization"] == f"Bearer {API_KEY}"
    submit_body = json.loads(transport.requests[2].body.decode("utf-8"))
    assert submit_body["input"]["file_urls"] == [OSS_LOCATOR]
    assert submit_body["parameters"] == {
        "channel_id": [0],
        "language_hints": ["zh", "en"],
        "diarization_enabled": False,
    }

    upload_request = transport.requests[1]
    assert upload_request.headers["Content-Type"].startswith(
        "multipart/form-data; boundary="
    )
    assert b'name="file"; filename="audio.flac"' in upload_request.body
    assert b"fLaC" in upload_request.body

    assert sleep.calls == [2.0]
    # The runner contract is exactly the run facts the normalizer needs.
    assert set(vars(run)) == {"started_at", "completed_at", "exit_status"}
    assert run.exit_status == 0
    assert run.started_at
    assert run.completed_at

    # The disposable FLAC and its temporary directory are removed.
    assert encoder.paths and not encoder.paths[0].parent.exists()

    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert set(evidence) == {"transcripts"}
    assert evidence["transcripts"][0]["sentences"][0]["text"] == TRANSCRIPT_TEXT
    serialized = json.dumps(evidence, ensure_ascii=False)
    assert "file_url" not in serialized
    assert "properties" not in serialized
    assert "fake-signature-canary" not in serialized
    assert "fake-result-object-canary" not in serialized
    assert "oss://" not in serialized
    assert TASK_ID not in serialized


@pytest.mark.parametrize("speaker_diarization", (False, True))
def test_speaker_diarization_is_sent_as_the_documented_provider_parameter(
    tmp_path: Path, speaker_diarization: bool
) -> None:
    """The one public speaker flag must reach the provider, not be a no-op."""

    transport = cloud_transport(load_recognition())
    run_cloud(
        tmp_path,
        transport=transport,
        config=default_config(speaker_diarization=speaker_diarization),
    )

    submit_body = json.loads(transport.requests[2].body.decode("utf-8"))
    parameters = submit_body["parameters"]
    assert parameters["diarization_enabled"] is speaker_diarization
    # The frozen request shape never asks for or asserts a speaker count.
    assert "speaker_count" not in parameters
    assert parameters["channel_id"] == [0]
    assert parameters["language_hints"] == ["zh", "en"]


def test_run_result_carries_only_its_own_redaction_safe_facts(tmp_path: Path) -> None:
    run, output = run_cloud(tmp_path, transport=cloud_transport(load_recognition()))

    serialized = f"{run.started_at} {run.completed_at} {run.exit_status}"
    for canary in REDACTION_CANARIES:
        assert canary not in serialized
    # The sanitized recognition JSON is the evidence boundary and keeps the
    # recognition content the normalizer needs.
    assert TRANSCRIPT_TEXT in output.read_text(encoding="utf-8")


FAILURE_CASES: tuple[tuple[str, dict[str, Any], str, str, int | None], ...] = (
    (
        "policy credential rejected",
        {"policy": json_response(401, {"code": "InvalidApiKey"})},
        "upload_policy",
        "credential_rejected",
        401,
    ),
    (
        "policy forbidden",
        {"policy": json_response(403, {"code": "AccessDenied"})},
        "upload_policy",
        "credential_rejected",
        403,
    ),
    (
        "policy http 503",
        {"policy": json_response(503, {"code": "ServiceUnavailable"})},
        "upload_policy",
        "http_failure",
        503,
    ),
    (
        "policy non json",
        {"policy": HttpResponse(200, b"<html>",)},
        "upload_policy",
        "malformed_response",
        200,
    ),
    (
        "policy missing fields",
        {
            "policy": json_response(
                200, {"data": {"upload_host": UPLOAD_HOST}}
            )
        },
        "upload_policy",
        "malformed_response",
        200,
    ),
    (
        "policy transport failure",
        {"policy": HttpResponse(None, b"",)},
        "upload_policy",
        "network_failure",
        None,
    ),
    (
        "upload rejected",
        {"upload": json_response(403, {"code": "SignatureDoesNotMatch"})},
        "upload",
        "http_failure",
        403,
    ),
    (
        "upload server error",
        {"upload": json_response(503, {})},
        "upload",
        "http_failure",
        503,
    ),
    (
        "upload transport failure",
        {"upload": HttpResponse(None, b"",)},
        "upload",
        "network_failure",
        None,
    ),
    (
        "submit credential rejected",
        {"submit": json_response(401, {"code": "InvalidApiKey"})},
        "submit",
        "credential_rejected",
        401,
    ),
    (
        "submit server error",
        {"submit": json_response(503, {})},
        "submit",
        "http_failure",
        503,
    ),
    (
        "submit non json",
        {"submit": HttpResponse(200, b"not-json",)},
        "submit",
        "malformed_response",
        200,
    ),
    (
        "submit missing task id",
        {"submit": json_response(200, {"output": {"task_status": "PENDING"}})},
        "submit",
        "malformed_response",
        200,
    ),
    (
        "submit unsafe task id",
        {"submit": json_response(200, {"output": {"task_id": "../../evil"}})},
        "submit",
        "malformed_response",
        200,
    ),
    (
        "task failed",
        {"polls": [task_output(task_status="FAILED")]},
        "poll",
        "task_failed",
        200,
    ),
    (
        "task canceled",
        {"polls": [task_output(task_status="CANCELED")]},
        "poll",
        "task_failed",
        200,
    ),
    (
        "subtask failed",
        {
            "polls": [
                task_output(
                    results=[
                        {
                            "subtask_status": "FAILED",
                            "transcription_url": SIGNED_RESULT_URL,
                        }
                    ]
                )
            ]
        },
        "poll",
        "subtask_failed",
        200,
    ),
    (
        "poll rate limited",
        {"polls": [json_response(429, {})]},
        "poll",
        "http_failure",
        429,
    ),
    (
        "poll non json",
        {"polls": [HttpResponse(200, b"nope",)]},
        "poll",
        "malformed_response",
        200,
    ),
    (
        "poll missing status",
        {"polls": [json_response(200, {"output": {}})]},
        "poll",
        "malformed_response",
        200,
    ),
    (
        "download refused",
        {"download": json_response(404, {})},
        "download",
        "http_failure",
        404,
    ),
    (
        "download non json",
        {"download": HttpResponse(200, b"nope",)},
        "download",
        "malformed_response",
        200,
    ),
    (
        "download missing url",
        {"polls": [task_output(results=[{"subtask_status": "SUCCEEDED"}])]},
        "download",
        "malformed_response",
        None,
    ),
    (
        "download duplicate urls",
        {
            "polls": [
                task_output(
                    results=[
                        {
                            "subtask_status": "SUCCEEDED",
                            "transcription_url": SIGNED_RESULT_URL,
                        },
                        {
                            "subtask_status": "SUCCEEDED",
                            "transcription_url": SIGNED_RESULT_URL,
                        },
                    ]
                )
            ]
        },
        "download",
        "malformed_response",
        None,
    ),
    (
        "download non http url",
        {
            "polls": [
                task_output(
                    results=[
                        {
                            "subtask_status": "SUCCEEDED",
                            "transcription_url": "oss://bucket/result.json",
                        }
                    ]
                )
            ]
        },
        "download",
        "malformed_response",
        None,
    ),
    (
        "recognition unsafe locator",
        {
            "download": json_response(
                200,
                {
                    "transcripts": [
                        {
                            "sentences": [
                                {
                                    "begin_time": 0,
                                    "end_time": 100,
                                    "text": "x",
                                    "words": [
                                        {
                                            "begin_time": 0,
                                            "end_time": 100,
                                            "text": "x",
                                            "transcription_url": SIGNED_RESULT_URL,
                                        }
                                    ],
                                }
                            ]
                        }
                    ]
                },
            )
        },
        "recognition_evidence",
        "unsafe_provider_shape",
        None,
    ),
    (
        "recognition missing sentences",
        {"download": json_response(200, {"transcripts": [{"text": "x"}]})},
        "recognition_evidence",
        "malformed_response",
        None,
    ),
    (
        "recognition not an object",
        {"download": json_response(200, [1, 2])},
        "recognition_evidence",
        "malformed_response",
        None,
    ),
)


@pytest.mark.parametrize(
    ("label", "overrides", "phase", "category", "status"),
    FAILURE_CASES,
    ids=[case[0] for case in FAILURE_CASES],
)
def test_transport_failures_are_typed_cleaned_up_and_redacted(
    tmp_path: Path,
    label: str,
    overrides: dict[str, Any],
    phase: str,
    category: str,
    status: int | None,
) -> None:
    transport = cloud_transport(load_recognition(), **overrides)

    with pytest.raises(QwenFiletransError) as error:
        run_cloud(tmp_path, transport=transport)

    assert (error.value.phase, error.value.category) == (phase, category)
    assert error.value.http_status == status
    counts = request_counts(transport)
    assert counts["policy"] <= 1
    assert counts["upload"] <= 1
    assert counts["submit"] <= 1
    assert counts["download"] <= 1
    assert not raw_path(tmp_path).exists()
    assert_error_is_redacted(error.value)


def test_submit_is_not_replayed_after_a_failure(tmp_path: Path) -> None:
    transport = cloud_transport(
        load_recognition(), submit=json_response(503, {"code": "ServiceUnavailable"})
    )

    with pytest.raises(QwenFiletransError):
        run_cloud(tmp_path, transport=transport)

    assert request_counts(transport)["submit"] == 1


def test_poll_timeout_is_bounded_and_makes_no_extra_request(tmp_path: Path) -> None:
    transport = cloud_transport(
        load_recognition(),
        polls=[json_response(200, {"output": {"task_status": "RUNNING"}})],
    )
    sleep = RecordingSleep()

    with pytest.raises(QwenFiletransError) as error:
        run_cloud(
            tmp_path,
            transport=transport,
            config=default_config(poll_timeout_seconds=0.0),
            sleep=sleep,
        )

    assert (error.value.phase, error.value.category) == ("poll", "poll_timeout")
    assert request_counts(transport) == {
        "policy": 1,
        "upload": 1,
        "submit": 1,
        "poll": 1,
        "download": 0,
    }
    assert sleep.calls == []
    assert not raw_path(tmp_path).exists()
    assert_error_is_redacted(error.value)


def test_audio_preparation_failure_is_typed_and_makes_no_request(
    tmp_path: Path,
) -> None:
    transport = cloud_transport(load_recognition())
    encoder = RecordingEncoder(
        error=FFmpegAudioError("FFmpeg could not decode the source audio")
    )

    with pytest.raises(QwenFiletransError) as error:
        run_cloud(tmp_path, transport=transport, encoder=encoder)

    assert (error.value.phase, error.value.category) == (
        "audio_preparation",
        "audio_preparation_failed",
    )
    assert transport.requests == []
    assert not raw_path(tmp_path).exists()
    assert encoder.paths and not encoder.paths[0].parent.exists()


@pytest.mark.parametrize("api_key", ("", "   ", "line\nbreak", "sk\x00nul", "sk\x1bescape", "sk\x0bvertical", "sk\x0cform", "sk\x7fdelete", "sk\x85next", "sk\u200bzero-width", "sk\u2028line-separator", "sk\u2029paragraph-separator", "sk\ud800surrogate"))
def test_missing_credential_fails_before_any_request(
    tmp_path: Path, api_key: str
) -> None:
    transport = cloud_transport(load_recognition())

    with pytest.raises(QwenFiletransError) as error:
        run_cloud(tmp_path, transport=transport, config=default_config(api_key=api_key))

    assert (error.value.phase, error.value.category) == (
        "credential",
        "missing_credential",
    )
    assert transport.requests == []
    assert not raw_path(tmp_path).exists()
    assert (
        credential_fields_are_valid(api_key=api_key, workspace_id=WORKSPACE_ID) is False
    )


@pytest.mark.parametrize(
    "workspace_id", ("", "evil.example.com", "ws id", "ws\nid", "../ws", "ws/../ws")
)
def test_invalid_workspace_id_is_rejected(tmp_path: Path, workspace_id: str) -> None:
    transport = cloud_transport(load_recognition())

    with pytest.raises(QwenFiletransError) as error:
        run_cloud(
            tmp_path,
            transport=transport,
            config=default_config(workspace_id=workspace_id),
        )

    assert (error.value.phase, error.value.category) == (
        "credential",
        "missing_credential",
    )
    assert transport.requests == []


def test_config_repr_never_contains_credentials() -> None:
    config = default_config()

    assert API_KEY not in repr(config)
    assert WORKSPACE_ID not in repr(config)
    assert "api_key" not in repr(config)


def test_failure_phases_and_categories_are_a_closed_internal_contract() -> None:
    assert "credential_rejected" in QWEN_FAILURE_CATEGORIES
    assert "network_failure" in QWEN_FAILURE_CATEGORIES
    assert "http_failure" in QWEN_FAILURE_CATEGORIES
    assert "task_failed" in QWEN_FAILURE_CATEGORIES
    assert "subtask_failed" in QWEN_FAILURE_CATEGORIES
    assert "poll_timeout" in QWEN_FAILURE_CATEGORIES
    assert "malformed_response" in QWEN_FAILURE_CATEGORIES
    assert "evidence_conflict" in QWEN_FAILURE_CATEGORIES
    assert "upload" in QWEN_FAILURE_PHASES
    assert "submit" in QWEN_FAILURE_PHASES
    assert "download" in QWEN_FAILURE_PHASES
    with pytest.raises(ValueError):
        QwenFiletransError("not-a-phase", "network_failure")
    with pytest.raises(ValueError):
        QwenFiletransError("upload", "not-a-category")


def test_sanitizer_removes_locators_and_transport_envelope() -> None:
    sanitized = sanitize_recognition_payload(load_recognition())

    assert set(sanitized) == {"transcripts"}
    serialized = json.dumps(sanitized, ensure_ascii=False)
    for forbidden in ("file_url", "properties", "example.invalid", "oss://"):
        assert forbidden not in serialized
    sentences = sanitized["transcripts"][0]["sentences"]  # type: ignore[index]
    assert sentences[0]["begin_time"] == 100
    assert sentences[0]["words"][0]["punctuation"] == "，"


def test_sanitizer_drops_unknown_fields_and_fails_closed_on_locators() -> None:
    payload: dict[str, object] = {
        "transcripts": [
            {
                "text": "x",
                "benign_transcript_field": 1,
                "sentences": [
                    {
                        "begin_time": 0,
                        "end_time": 1,
                        "text": "x",
                        "benign_sentence_field": {"a": 1},
                        "words": [
                            {
                                "begin_time": 0,
                                "end_time": 1,
                                "text": "x",
                                "word_id": 7,
                            }
                        ],
                    }
                ],
            }
        ]
    }

    serialized = json.dumps(sanitize_recognition_payload(payload), ensure_ascii=False)
    assert "benign" not in serialized
    assert "word_id" not in serialized

    for sensitive_name in (
        "transcription_url",
        "task_id",
        "oss_access_key_id",
        "Signature",
        "authorization",
    ):
        leaking = dict(payload)
        leaking[sensitive_name] = "https://leak.example.invalid"
        with pytest.raises(QwenFiletransError) as error:
            sanitize_recognition_payload(leaking)
        assert error.value.category == "unsafe_provider_shape"


def test_sanitizer_requires_recognition_structure() -> None:
    for payload in (
        [],
        "text",
        5,
        {},
        {"transcripts": []},
        {"transcripts": [{"sentences": []}]},
        {"transcripts": [{"sentences": [{"begin_time": 0, "end_time": 1, "text": "x"}]}]},
        {
            "transcripts": [
                {
                    "sentences": [
                        {
                            "begin_time": 0,
                            "end_time": 1,
                            "text": "x",
                            "words": [{"begin_time": 0, "end_time": 1}],
                        }
                    ]
                }
            ]
        },
    ):
        with pytest.raises(QwenFiletransError) as error:
            sanitize_recognition_payload(payload)
        assert error.value.category == "malformed_response"


def test_urllib_transport_maps_transport_failure_to_no_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(request: object, timeout: float) -> object:
        raise URLError("connection reset")

    monkeypatch.setattr(filetrans._OPENER, "open", explode)

    assert urllib_transport(probe_request()).status is None


def test_urllib_transport_maps_body_read_failure_to_no_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenResponse:
        status = 200
        headers: ClassVar[dict[str, str]] = {}

        def read(self) -> bytes:
            raise OSError("connection reset while reading the body")

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *arguments: object) -> bool:
            return False

    monkeypatch.setattr(
        filetrans._OPENER, "open", lambda request, timeout: BrokenResponse()
    )

    assert urllib_transport(probe_request()).status is None


def test_urllib_transport_maps_incomplete_read_to_no_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`IncompleteRead` derives from `HTTPException`, not `OSError`."""

    class TruncatedResponse:
        status = 200
        headers: ClassVar[dict[str, str]] = {}

        def read(self) -> bytes:
            raise IncompleteRead(b"partial", 10)

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *arguments: object) -> bool:
            return False

    monkeypatch.setattr(
        filetrans._OPENER, "open", lambda request, timeout: TruncatedResponse()
    )

    assert urllib_transport(probe_request()).status is None


def test_urllib_transport_maps_incomplete_error_body_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TruncatedBody:
        def read(self, *arguments: object) -> bytes:
            raise IncompleteRead(b"partial", 10)

        def close(self) -> None:
            return

    def refuse(request: object, timeout: float) -> object:
        raise HTTPError(
            "https://example.invalid", 503, "unavailable", None, TruncatedBody()
        )

    monkeypatch.setattr(filetrans._OPENER, "open", refuse)

    response = urllib_transport(probe_request())

    assert response.status == 503
    assert response.body == b""


@contextmanager
def loopback_server(handler: type[BaseHTTPRequestHandler]) -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_urllib_transport_never_follows_a_redirect_across_origins() -> None:
    """A 3xx must never forward the API key to another origin.

    urllib's default redirect handler re-issues the request with the original
    headers, so this is the transport-level proof that the injected
    ``Authorization`` header has exactly one destination.
    """

    seen: list[tuple[str, str | None]] = []

    class TargetHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(("redirect-target", self.headers.get("Authorization")))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *arguments: object) -> None:
            return

    with loopback_server(TargetHandler) as target_port:

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                seen.append(("credential-endpoint", self.headers.get("Authorization")))
                self.send_response(302)
                self.send_header(
                    "Location", f"http://127.0.0.1:{target_port}/exfiltrated"
                )
                self.end_headers()

            def log_message(self, *arguments: object) -> None:
                return

        with loopback_server(RedirectHandler) as origin_port:
            response = urllib_transport(
                HttpRequest(
                    method="GET",
                    url=f"http://127.0.0.1:{origin_port}/api/v1/tasks/fake-task-id",
                    headers={"Authorization": f"Bearer {API_KEY}"},
                    body=None,
                    timeout_seconds=5.0,
                )
            )

    assert response.status == 302
    assert [kind for kind, _ in seen] == ["credential-endpoint"]
    assert seen[0][1] == f"Bearer {API_KEY}"


def test_urllib_transport_preserves_http_status_and_error_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(request: object, timeout: float) -> object:
        raise HTTPError(
            "https://example.invalid",
            503,
            "unavailable",
            None,
            BytesIO(b'{"code":"ServiceUnavailable"}'),
        )

    monkeypatch.setattr(filetrans._OPENER, "open", refuse)

    response = urllib_transport(probe_request())

    assert response.status == 503
    assert b"ServiceUnavailable" in response.body


def probe_request() -> HttpRequest:
    return HttpRequest(
        method="GET",
        url="https://example.invalid/probe",
        headers={},
        body=None,
        timeout_seconds=1.0,
    )


def test_extension_point_types_are_narrow() -> None:
    """The adapter exposes no provider registry or generic ASR interface."""

    public_names = {
        name for name in dir(filetrans) if not name.startswith("_")
    }
    for forbidden in ("REGIONS", "ENDPOINTS", "PROVIDERS", "FALLBACKS"):
        assert forbidden not in public_names
    assert not isinstance(filetrans, Mapping)






def test_sanitizer_keeps_scalar_metadata_for_the_normalizer() -> None:
    """The sanitizer keeps scalars; the normalizer owns the semantic rules."""

    payload = load_recognition()
    recognition_sentences(payload)[0]["speaker_id"] = 3
    recognition_words(payload, 0)[0]["punctuation"] = "，"
    recognition_words(payload, 0)[1]["punctuation"] = "……"

    sanitized = sanitize_recognition_payload(payload)

    sentences = sanitized["transcripts"][0]["sentences"]  # type: ignore[index]
    assert sentences[0]["speaker_id"] == 3
    assert sentences[0]["words"][0]["punctuation"] == "，"
    assert sentences[0]["words"][1]["punctuation"] == "……"


@pytest.mark.parametrize("speaker_id", (True, 1.0))
def test_sanitizer_rejects_non_scalar_speaker_ids(speaker_id: object) -> None:
    """Only the scalar boundary is enforced here; the index rule is downstream."""

    payload = load_recognition()
    recognition_sentences(payload)[0]["speaker_id"] = speaker_id

    with pytest.raises(QwenFiletransError) as error:
        sanitize_recognition_payload(payload)

    assert error.value.category == "malformed_response"




def test_sanitizer_fails_closed_on_a_container_under_an_allowed_field() -> None:
    """A locator nested under an allowed field name must not reach evidence."""

    nested = {"transcription_url": SIGNED_RESULT_URL}
    mutations: tuple[tuple[str, Callable[[dict[str, Any]], None]], ...] = (
        (
            "sentence timing mapping",
            lambda raw: recognition_sentences(raw)[0].update({"begin_time": nested}),
        ),
        (
            "sentence speaker list",
            lambda raw: recognition_sentences(raw)[0].update({"speaker_id": [nested]}),
        ),
        (
            "word text mapping",
            lambda raw: recognition_words(raw, 0)[0].update({"text": nested}),
        ),
        (
            "word punctuation list",
            lambda raw: recognition_words(raw, 0)[0].update({"punctuation": [nested]}),
        ),
    )
    for label, mutate in mutations:
        payload = load_recognition()
        mutate(payload)
        with pytest.raises(QwenFiletransError) as error:
            sanitize_recognition_payload(payload)
        assert error.value.category == "unsafe_provider_shape", label
        assert "fake-signature-canary" not in str(error.value), label


@pytest.mark.parametrize(
    "mutate",
    (
        lambda raw: recognition_sentences(raw)[0].update({"begin_time": "100"}),
        lambda raw: recognition_sentences(raw)[0].update({"end_time": True}),
        lambda raw: raw["transcripts"][0].update({"text": 5}),
        lambda raw: recognition_words(raw, 0)[0].update({"punctuation": 7}),
    ),
)
def test_sanitizer_requires_the_frozen_scalar_types(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    payload = load_recognition()
    mutate(payload)

    with pytest.raises(QwenFiletransError) as error:
        sanitize_recognition_payload(payload)

    assert error.value.category == "malformed_response"


def test_sanitizer_keeps_absent_and_null_punctuation() -> None:
    payload = load_recognition()
    sentence_words = recognition_words(payload, 0)
    sentence_words[0]["punctuation"] = None
    sentence_words[1].pop("punctuation")

    sanitized = sanitize_recognition_payload(payload)

    kept = sanitized["transcripts"][0]["sentences"][0]["words"]  # type: ignore[index]
    assert kept[0]["punctuation"] is None
    assert "punctuation" not in kept[1]


@pytest.mark.parametrize(
    ("label", "output", "expected"),
    (
        (
            "no results",
            {"task_status": "SUCCEEDED"},
            ("poll", "malformed_response"),
        ),
        (
            "empty results",
            {"task_status": "SUCCEEDED", "results": []},
            ("poll", "malformed_response"),
        ),
        (
            "result without subtask status",
            {
                "task_status": "SUCCEEDED",
                "results": [{"transcription_url": SIGNED_RESULT_URL}],
            },
            ("poll", "malformed_response"),
        ),
        (
            "blank subtask status",
            {
                "task_status": "SUCCEEDED",
                "results": [
                    {"subtask_status": "", "transcription_url": SIGNED_RESULT_URL}
                ],
            },
            ("poll", "malformed_response"),
        ),
        (
            "non string subtask status",
            {
                "task_status": "SUCCEEDED",
                "results": [
                    {"subtask_status": 5, "transcription_url": SIGNED_RESULT_URL}
                ],
            },
            ("poll", "malformed_response"),
        ),
        (
            "result not an object",
            {"task_status": "SUCCEEDED", "results": ["SUCCEEDED"]},
            ("poll", "malformed_response"),
        ),
        (
            "one subtask failed",
            {
                "task_status": "SUCCEEDED",
                "results": [
                    {"subtask_status": "SUCCEEDED"},
                    {"subtask_status": "FAILED"},
                ],
            },
            ("poll", "subtask_failed"),
        ),
    ),
)
def test_terminal_task_requires_a_well_formed_subtask_inspection(
    tmp_path: Path,
    label: str,
    output: dict[str, object],
    expected: tuple[str, str],
) -> None:
    transport = cloud_transport(
        load_recognition(), polls=[json_response(200, {"output": output})]
    )

    with pytest.raises(QwenFiletransError) as error:
        run_cloud(tmp_path, transport=transport)

    assert (error.value.phase, error.value.category) == expected, label
    assert not raw_path(tmp_path).exists()


@pytest.mark.parametrize(
    "upload_host",
    (
        "http://dashscope-file-mgr.oss-cn-beijing.aliyuncs.com",
        "https://oss.example.invalid",
        "https://dashscope-file-mgr.oss-cn-singapore.aliyuncs.com",
        "https://dashscope-file-mgr.oss-cn-beijing.aliyuncs.com.attacker.example",
        "https://eviloss-cn-beijingfake.aliyuncs.com",
        "https://attacker-bucket.oss-cn-beijing.aliyuncs.com",
        "https://bucket.oss-cn-beijing-other.aliyuncs.com",
        "https://.oss-cn-beijing.aliyuncs.com",
        "https://oss-cn-beijing.aliyuncs.com",
        "https://user:pass@dashscope-file-mgr.oss-cn-beijing.aliyuncs.com",
        "https://dashscope-file-mgr.oss-cn-beijing.aliyuncs.com:444",
        "https://bucket.oss-cn-beijing.aliyuncs.com/upload-dir",
        "https://bucket.oss-cn-beijing.aliyuncs.com/?route=x",
    ),
)
def test_upload_origin_must_be_the_documented_beijing_oss_shape(
    tmp_path: Path, upload_host: str
) -> None:
    transport = cloud_transport(
        load_recognition(),
        policy=json_response(200, {"data": {**POLICY_DATA, "upload_host": upload_host}}),
    )

    with pytest.raises(QwenFiletransError) as error:
        run_cloud(tmp_path, transport=transport)

    assert (error.value.phase, error.value.category) == (
        "upload_policy",
        "malformed_response",
    )
    assert error.value.http_status == 200
    # The temporary upload credential and the audio never leave the process.
    assert request_counts(transport) == {
        "policy": 1,
        "upload": 0,
        "submit": 0,
        "poll": 0,
        "download": 0,
    }


@pytest.mark.parametrize(
    "result_url",
    (
        "http://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/result.json",
        "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com:444/r.json",
        "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com",
        "https://user:pass@dashscope-result-bj.oss-cn-beijing.aliyuncs.com/r.json",
    ),
)
def test_result_url_rejects_unsafe_or_malformed_authorities(
    tmp_path: Path, result_url: str
) -> None:
    transport = cloud_transport(
        load_recognition(),
        polls=[
            task_output(
                results=[
                    {"subtask_status": "SUCCEEDED", "transcription_url": result_url}
                ]
            )
        ],
    )

    with pytest.raises(QwenFiletransError) as error:
        run_cloud(tmp_path, transport=transport)

    assert (error.value.phase, error.value.category) == (
        "download",
        "malformed_response",
    )
    assert request_counts(transport)["download"] == 0


def test_upload_origin_rejects_a_malformed_authority_before_any_request(
    tmp_path: Path,
) -> None:
    """A malformed authority must not receive credentials or audio."""

    transport = cloud_transport(
        load_recognition(),
        policy=json_response(
            200,
            {
                "data": {
                    **POLICY_DATA,
                    "upload_host": (
                        "https://attacker-bucket.oss-cn-beijing.aliyuncs.com:444"
                        "/unobserved?route=x"
                    ),
                }
            },
        ),
    )

    with pytest.raises(QwenFiletransError) as error:
        run_cloud(tmp_path, transport=transport)

    assert (error.value.phase, error.value.category) == (
        "upload_policy",
        "malformed_response",
    )
    assert request_counts(transport) == {
        "policy": 1,
        "upload": 0,
        "submit": 0,
        "poll": 0,
        "download": 0,
    }
    assert_error_is_redacted(error.value)


def test_dynamic_documented_upload_host_completes_the_pipeline(tmp_path: Path) -> None:
    """A provider-issued label must work end to end, not just `file-mgr`."""

    dynamic_origin = "https://dashscope-file-abc123.oss-cn-beijing.aliyuncs.com"
    recognition = load_recognition()
    uploaded: list[str] = []

    def handler(request: HttpRequest) -> HttpResponse:
        if request.url.startswith(UPLOAD_POLICY_ENDPOINT):
            return json_response(200, {"data": {**POLICY_DATA, "upload_host": dynamic_origin}})
        if request.url.startswith(dynamic_origin):
            uploaded.append(request.url)
            return HttpResponse(200, b"",)
        if request.url.endswith(SUBMIT_PATH):
            return json_response(200, {"output": {"task_id": TASK_ID}})
        if "/api/v1/tasks/" in request.url:
            return task_output()
        if request.url.startswith(RESULT_HOST):
            return json_response(200, recognition)
        raise AssertionError(f"unexpected request {request.url}")

    transport = RecordingTransport(handler)
    run, output = run_cloud(tmp_path, transport=transport)

    assert run.exit_status == 0
    assert uploaded == [dynamic_origin]
    assert output.exists()


def test_documented_request_headers_are_sent(tmp_path: Path) -> None:
    """Policy and poll GETs carry the documented bearer and JSON content type."""

    transport = cloud_transport(load_recognition())
    run_cloud(tmp_path, transport=transport)

    by_phase = {
        "policy": transport.requests[0],
        "upload": transport.requests[1],
        "submit": transport.requests[2],
        "poll": transport.requests[3],
        "download": transport.requests[5],
    }
    for phase in ("policy", "submit", "poll"):
        assert by_phase[phase].headers["Authorization"] == f"Bearer {API_KEY}", phase
        assert by_phase[phase].headers["Content-Type"] == "application/json", phase
    assert by_phase["policy"].headers["Accept"] == "application/json"
    assert by_phase["upload"].headers["Content-Type"].startswith(
        "multipart/form-data; boundary="
    )
    # The signed result object is fetched without the API key.
    assert "Authorization" not in by_phase["download"].headers




def test_upload_origin_uses_the_documented_dynamic_host() -> None:
    """The credential response names the origin; only its shape is constrained."""

    for label in ("mgr", "abc123", "x7", "file-2026", "a" * 40):
        origin = f"https://dashscope-file-{label}.oss-cn-beijing.aliyuncs.com"
        assert filetrans._require_upload_origin(origin, "upload_policy") == origin
        assert (
            filetrans._require_upload_origin(f"{origin}:443", "upload_policy")
            == f"{origin}:443"
        )
    assert (
        filetrans.UPLOAD_ORIGIN_PATTERN.fullmatch(
            "dashscope-file-abc.oss-cn-beijing.aliyuncs.com"
        )
        is not None
    )


def test_result_url_is_not_bound_to_one_observed_host() -> None:
    """The task query returns the locator; no single result host is required."""

    for url in (
        (
            "https://dashscope-result.oss-cn-beijing.aliyuncs.com/result.json"
            "?Expires=1761631066&OSSAccessKeyId=x&Signature=y"
        ),
        "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/prod/x.json?Signature=s",
        "https://other-cdn.example.com/result.json?sig=x",
    ):
        assert filetrans._require_result_url(url, "download") == url


def test_result_url_still_requires_a_safe_authority() -> None:
    """Minimal validation: HTTPS, no userinfo, default port, real DNS name."""

    for url in (
        "https://127.0.0.1/result.json",
        "https://localhost/result.json",
        "https://user:pass@dashscope-result.oss-cn-beijing.aliyuncs.com/x.json",
        "https://@dashscope-result.oss-cn-beijing.aliyuncs.com/x.json",
        "https://dashscope-result.oss-cn-beijing.aliyuncs.com:444/x.json",
        "https://dashscope-result.oss-cn-beijing.aliyuncs.com:/x.json",
        "https://.oss-cn-beijing.aliyuncs.com/x.json",
        "https://-host.example.com/x.json",
        "http://dashscope-result.oss-cn-beijing.aliyuncs.com/x.json",
        "oss://dashscope-result.oss-cn-beijing.aliyuncs.com/x.json",
    ):
        with pytest.raises(QwenFiletransError) as error:
            filetrans._require_result_url(url, "download")
        assert error.value.category == "malformed_response", url


@pytest.mark.parametrize("suffix_length", (1, 47, 48))
def test_upload_origin_accepts_a_dns_bounded_provider_label(suffix_length: int) -> None:
    """`dashscope-file-` plus the label must stay inside 63 octets."""

    host = f"dashscope-file-{'a' * suffix_length}.oss-cn-beijing.aliyuncs.com"
    assert len(host.split(".")[0]) <= 63

    assert filetrans._require_upload_origin(f"https://{host}", "upload_policy")


@pytest.mark.parametrize("suffix_length", (49, 63))
def test_upload_origin_rejects_an_overlong_dns_label(suffix_length: int) -> None:
    host = f"dashscope-file-{'a' * suffix_length}.oss-cn-beijing.aliyuncs.com"
    assert len(host.split(".")[0]) > 63

    with pytest.raises(QwenFiletransError) as error:
        filetrans._require_upload_origin(f"https://{host}", "upload_policy")

    assert error.value.category == "malformed_response"


@pytest.mark.parametrize("suffix", ("?", "#", "/?", "/#", "?route=x"))
def test_upload_origin_rejects_raw_query_or_fragment_delimiters(suffix: str) -> None:
    """An empty delimiter parses to a falsy component, so raw text is checked."""

    url = f"https://dashscope-file-abc.oss-cn-beijing.aliyuncs.com{suffix}"

    with pytest.raises(QwenFiletransError) as error:
        filetrans._require_upload_origin(url, "upload_policy")

    assert error.value.category == "malformed_response"


@pytest.mark.parametrize("character", ("\n", "\r\n", "\t", " ", "\x00", "\x7f"))
def test_authority_rejects_control_and_whitespace_characters(character: str) -> None:
    """`urlsplit` strips these, so the raw text must be checked too."""

    for validator, phase, url in (
        (
            filetrans._require_upload_origin,
            "upload_policy",
            f"https://dashscope-file-abc.oss-cn-beijing.aliyuncs.com{character}",
        ),
        (
            filetrans._require_result_url,
            "download",
            f"https://dashscope-result.oss-cn-beijing.aliyuncs.com/x.json{character}",
        ),
    ):
        with pytest.raises(QwenFiletransError) as error:
            validator(url, phase)
        assert error.value.category == "malformed_response", repr(url)


@pytest.mark.parametrize("character", ("\u00a0", "\u0085", "\u200b", "\u3000"))
def test_authority_rejects_non_ascii_whitespace_and_controls(character: str) -> None:
    """`urllib` strips these too, so they must not validate as clean."""

    for validator, phase, url in (
        (
            filetrans._require_upload_origin,
            "upload_policy",
            f"https://dashscope-file-abc.oss-cn-beijing.aliyuncs.com{character}",
        ),
        (
            filetrans._require_result_url,
            "download",
            f"https://dashscope-result.oss-cn-beijing.aliyuncs.com/x.json{character}",
        ),
    ):
        with pytest.raises(QwenFiletransError) as error:
            validator(url, phase)
        assert error.value.category == "malformed_response", repr(url)


@pytest.mark.parametrize("suffix", ("#", "#fragment"))
def test_result_url_rejects_fragments(suffix: str) -> None:
    """A fragment is never sent, so the request target would differ."""

    url = f"https://dashscope-result.oss-cn-beijing.aliyuncs.com/x.json{suffix}"

    with pytest.raises(QwenFiletransError) as error:
        filetrans._require_result_url(url, "download")

    assert error.value.category == "malformed_response"


def test_result_url_query_signature_is_preserved() -> None:
    """The signed query must still reach the request target verbatim."""

    url = (
        "https://dashscope-result.oss-cn-beijing.aliyuncs.com/result.json"
        "?Expires=1761631066&OSSAccessKeyId=x&Signature=y"
    )

    assert filetrans._require_result_url(url, "download") == url
@pytest.mark.parametrize("authority", ("@", ":", ":444", ":abc"))
def test_upload_origin_rejects_userinfo_empty_port_and_any_other_host(
    authority: str,
) -> None:
    """The previously-fixed forms: empty userinfo and an empty/invalid port."""

    url = f"https://{authority}dashscope-file-mgr.oss-cn-beijing.aliyuncs.com"
    with pytest.raises(QwenFiletransError) as error:
        filetrans._require_upload_origin(url, "upload_policy")
    assert error.value.category == "malformed_response", url


def test_redirect_status_from_any_phase_is_a_typed_failure(
    tmp_path: Path,
) -> None:
    """A refused redirect for each phase stays a bounded, redacted failure."""

    redirect = HttpResponse(302, b"")
    cases: tuple[tuple[str, dict[str, Any], str], ...] = (
        ("policy", {"policy": redirect}, "upload_policy"),
        ("upload", {"upload": redirect}, "upload"),
        ("submit", {"submit": redirect}, "submit"),
        ("poll", {"polls": [redirect]}, "poll"),
        ("download", {"download": redirect}, "download"),
    )
    for label, overrides, phase in cases:
        transport = cloud_transport(load_recognition(), **overrides)
        with pytest.raises(QwenFiletransError) as error:
            run_cloud(tmp_path, transport=transport)
        assert (error.value.phase, error.value.category) == (phase, "http_failure"), label
        assert error.value.http_status == 302, label
        assert not raw_path(tmp_path).exists(), label
        assert_error_is_redacted(error.value)


def test_no_provider_supplied_code_reaches_the_error(tmp_path: Path) -> None:
    """A closed allowlist still collides with a real key or task id.

    Round-2 review reproduced `api_key="InvalidApiKey"` and a task id of
    `ServiceUnavailable` landing in the error attribute, so the adapter no
    longer carries a provider-supplied value at all.
    """

    for code, api_key, task_id in (
        ("InvalidApiKey", "InvalidApiKey", TASK_ID),
        ("ServiceUnavailable", API_KEY, "ServiceUnavailable"),
    ):
        auth_transport = cloud_transport(
            load_recognition(), submit=json_response(401, {"code": code})
        )
        with pytest.raises(QwenFiletransError) as auth_error:
            run_cloud(
                tmp_path,
                transport=auth_transport,
                config=default_config(api_key=api_key),
            )
        assert not hasattr(auth_error.value, "provider_code")
        assert api_key not in repr(auth_error.value)

        poll_transport = cloud_transport(
            load_recognition(),
            submit=json_response(200, {"output": {"task_id": task_id}}),
            polls=[json_response(503, {"code": code})],
        )
        with pytest.raises(QwenFiletransError) as poll_error:
            run_cloud(tmp_path, transport=poll_transport)
        assert not hasattr(poll_error.value, "provider_code")
        assert task_id not in repr(poll_error.value)


def test_untrusted_provider_code_never_reaches_the_error(tmp_path: Path) -> None:
    for canary in REDACTION_CANARIES:
        transport = cloud_transport(
            load_recognition(), submit=json_response(401, {"code": canary})
        )

        with pytest.raises(QwenFiletransError) as error:
            run_cloud(tmp_path, transport=transport)

        assert_error_is_redacted(error.value)


def test_existing_evidence_fails_closed_before_any_request(tmp_path: Path) -> None:
    output = raw_path(tmp_path)
    output.parent.mkdir(parents=True)
    output.write_text("{\"transcripts\": \"previous-run-evidence\"}", encoding="utf-8")
    transport = cloud_transport(load_recognition())

    with pytest.raises(QwenFiletransError) as error:
        run_cloud(tmp_path, transport=transport)

    assert (error.value.phase, error.value.category) == (
        "recognition_evidence",
        "evidence_conflict",
    )
    assert request_counts(transport) == {
        "policy": 0,
        "upload": 0,
        "submit": 0,
        "poll": 0,
        "download": 0,
    }
    assert output.read_text(encoding="utf-8") == "{\"transcripts\": \"previous-run-evidence\"}"


def test_failed_run_leaves_no_evidence_behind(tmp_path: Path) -> None:
    transport = cloud_transport(
        load_recognition(),
        download=json_response(200, [1, 2]),
    )

    with pytest.raises(QwenFiletransError):
        run_cloud(tmp_path, transport=transport)

    assert not raw_path(tmp_path).exists()


def test_destination_created_mid_run_is_not_deleted(tmp_path: Path) -> None:
    """A destination this run does not own must survive its failure.

    Round-2 review reproduced the failure path deleting a file that appeared
    after the entry check; only the atomic publish may touch the destination.
    """

    output = raw_path(tmp_path)

    def create_destination(phase: str) -> None:
        if phase == "transcription_decoding_audio":
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("concurrent-run-evidence", encoding="utf-8")

    transport = cloud_transport(load_recognition())
    with pytest.raises(QwenFiletransError) as error:
        run_cloud(
            tmp_path,
            transport=transport,
            encoder=RecordingEncoder(error=FFmpegAudioError("no audio")),
            phase_callback=create_destination,
        )

    assert (error.value.phase, error.value.category) == (
        "audio_preparation",
        "audio_preparation_failed",
    )
    assert output.read_text(encoding="utf-8") == "concurrent-run-evidence"


def test_publish_refuses_to_overwrite_a_destination_created_mid_run(
    tmp_path: Path,
) -> None:
    output = raw_path(tmp_path)

    def create_destination(phase: str) -> None:
        if phase == "transcription_decoding_audio":
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("concurrent-run-evidence", encoding="utf-8")

    transport = cloud_transport(load_recognition())
    with pytest.raises(QwenFiletransError) as error:
        run_cloud(tmp_path, transport=transport, phase_callback=create_destination)

    assert (error.value.phase, error.value.category) == (
        "recognition_evidence",
        "evidence_conflict",
    )
    assert output.read_text(encoding="utf-8") == "concurrent-run-evidence"
    assert not list(output.parent.glob(".*run.json.*"))
