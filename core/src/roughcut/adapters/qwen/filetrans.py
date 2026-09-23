"""Bounded Qwen Filetrans (DashScope) cloud ASR transport adapter.

This adapter owns exactly one frozen path: prepare a sibling 16 kHz mono FLAC
profile, upload it through the official DashScope temporary-upload policy,
submit one Filetrans task, poll it, inspect subtasks, download the recognition
JSON immediately, and persist only a sanitized recognition copy as
normalization evidence.

Boundaries that are deliberate and load-bearing:

* Region, model, endpoints, upload mode and the provider request shape are fixed
  constants.  There is no region probing, endpoint fallback, provider registry,
  scheduler, automatic retry or remote resume.
* Credentials are injected by the caller through :class:`QwenFiletransConfig`.
  This module never reads Roughcut state, credential files, Host configuration or
  environment variables.  WP3A owns credential ownership and readiness.
* Errors are narrow and typed for later MediaOperation mapping.  They carry a
  phase, a category and an HTTP status only; they never carry the API key, an
  authorization header, a signed URL, an ``oss://`` locator, a task locator, a
  raw response body, transcript text, or any provider-supplied string, because
  a value copied out of an untrusted response body can collide with a real
  credential or locator.
* Retained recognition fields are type- and shape-checked before they are
  written, so a locator or credential cannot be smuggled into the evidence by
  nesting it under an allowed field name or by passing it as a scalar metadata
  value.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import formatdate
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import SplitResult, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from roughcut import __version__ as ROUGHCUT_CORE_VERSION
from roughcut.adapters.ffmpeg.audio import (
    FFmpegAudioError,
    FLACEncode,
    encode_audio_to_flac,
)
from roughcut.adapters.qwen import (
    CHANNEL_ID,
    LANGUAGE_HINTS,
    MODEL_NAME,
    REGION,
    credential_fields_are_valid,
)

UPLOAD_POLICY_ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/uploads"
WORKSPACE_HOST_SUFFIX = f"{REGION}.maas.aliyuncs.com"
SUBMIT_PATH = "/api/v1/services/audio/asr/transcription"
TASK_PATH = "/api/v1/tasks/{task_id}"
UPLOAD_FILE_NAME = "audio.flac"
UPLOAD_CONTENT_TYPE = "audio/flac"

TERMINAL_TASK_STATUSES = frozenset({"SUCCEEDED", "FAILED", "CANCELED", "CANCELLED"})
QWEN_FAILURE_PHASES = (
    "credential",
    "audio_preparation",
    "upload_policy",
    "upload",
    "submit",
    "poll",
    "download",
    "recognition_evidence",
)
QWEN_FAILURE_CATEGORIES = (
    "missing_credential",
    "credential_rejected",
    "network_failure",
    "http_failure",
    "malformed_response",
    "task_failed",
    "subtask_failed",
    "poll_timeout",
    "audio_preparation_failed",
    "unsafe_provider_shape",
    "evidence_conflict",
)

# The upload credential response names the origin to POST to.  Alibaba documents
# that origin as ``dashscope-file-<label>.oss-cn-beijing.aliyuncs.com``, so only
# that documented Beijing temporary-upload shape is accepted.  This keeps one
# fixed region with no probing, fallback or user-supplied endpoint without
# freezing the exact label one observed run happened to return.
# One DNS label (RFC 1035 caps a label at 63 octets), and the shape of a real
# public hostname: an alphabetic top-level label and no empty or edge-hyphen
# labels, so malformed authorities, single-label names and IP literals are all
# refused.
_HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
# `dashscope-file-` is 15 octets, so the provider-issued suffix is capped at 48
# to keep the assembled first label inside that 63-octet limit.
_UPLOAD_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?"
# The upload credential response names the origin to POST to.  Alibaba documents
# that origin as ``dashscope-file-<label>.oss-cn-beijing.aliyuncs.com``, so only
# that documented Beijing temporary-upload shape is accepted: one fixed region,
# no probing, no fallback and no user-supplied endpoint, without freezing the
# exact label one observed run happened to return.
UPLOAD_ORIGIN_PATTERN = re.compile(
    rf"dashscope-file-{_UPLOAD_LABEL}\.oss-cn-beijing\.aliyuncs\.com"
)
# A result object is a provider-returned signed locator whose host is not
# documented, and that GET carries no API key, no upload credential and no user
# audio, so only its authority shape is validated rather than pinning the host
# one observed run returned.
RESULT_HOST_PATTERN = re.compile(rf"(?:{_HOST_LABEL}\.)+[a-z]{{2,63}}")
# `urlsplit` and `urllib.request.Request` both strip whitespace and control
# characters, and `parts.query`/`parts.fragment` hide an empty delimiter, so the
# raw text is checked as well: validation and the request must agree on exactly
# the same authority.

_TASK_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")

_RECOGNITION_TOP_LEVEL_FIELDS = frozenset({"transcripts"})
_RECOGNITION_TRANSCRIPT_FIELDS = frozenset({"text", "sentences"})
_RECOGNITION_SENTENCE_FIELDS = frozenset(
    {"begin_time", "end_time", "text", "speaker_id", "words"}
)
_RECOGNITION_WORD_FIELDS = frozenset({"begin_time", "end_time", "text", "punctuation"})
# S0-evidenced provider locator fields; removed from persisted evidence, never
# persisted, and never treated as an unexpected shape.
_RECOGNITION_DROPPED_LOCATOR_FIELDS = frozenset({"file_url", "properties"})
_RECOGNITION_REQUIRED_SENTENCE_FIELDS = (
    "begin_time",
    "end_time",
    "text",
    "words",
)
_RECOGNITION_REQUIRED_WORD_FIELDS = ("begin_time", "end_time", "text")
_SENSITIVE_FIELD_NAME = re.compile(
    r"(?i)(url|uri|locator|token|signature|credential|secret|policy|authorization"
    r"|cookie|accesskey|task|oss)"
)


class QwenFiletransError(RuntimeError):
    """A bounded, redaction-safe Qwen transport failure.

    ``phase`` and ``category`` are the internal implementation contract that
    WP3B maps into the existing ASR MediaOperation taxonomy.  They are not a
    public error schema.
    """

    def __init__(
        self,
        phase: str,
        category: str,
        *,
        http_status: int | None = None,
    ) -> None:
        if phase not in QWEN_FAILURE_PHASES:
            raise ValueError("unknown Qwen failure phase")
        if category not in QWEN_FAILURE_CATEGORIES:
            raise ValueError("unknown Qwen failure category")
        super().__init__(f"Qwen Filetrans {phase} failed: {category}")
        self.phase = phase
        self.category = category
        self.http_status = http_status


@dataclass(frozen=True)
class QwenFiletransConfig:
    """Caller-injected runtime configuration; never read from ambient state."""

    api_key: str = field(repr=False)
    workspace_id: str = field(repr=False)
    request_timeout_seconds: float = 120.0
    upload_timeout_seconds: float = 300.0
    poll_timeout_seconds: float = 300.0
    poll_interval_seconds: float = 2.0
    ffmpeg_command: str | None = None
    ffmpeg_version: str | None = None
    # The one public speaker flag the frozen request shape carries.  It is sent
    # as the documented ``diarization_enabled`` parameter; the provider's
    # ``speaker_count`` is never requested or treated as a success condition.
    speaker_diarization: bool = False


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | None
    timeout_seconds: float


@dataclass(frozen=True)
class HttpResponse:
    """Exactly what the adapter reads: a status and a body."""

    status: int | None
    body: bytes


Transport = Callable[[HttpRequest], HttpResponse]
AudioEncoder = Callable[[Path, Path], FLACEncode]
PhaseCallback = Callable[[str], None]


@dataclass(frozen=True)
class QwenFiletransRun:
    """The runner facts the normalizer publishes as provenance.

    The fixed Cloud identity and its parameters are derived inside the
    normalizer from one shared source of truth, so a run reports only the facts
    it actually owns.
    """

    started_at: str
    completed_at: str
    exit_status: int


class _NoRedirectHandler(HTTPRedirectHandler):
    """Refuse every redirect instead of following it.

    urllib's default handler re-issues the redirected request with the original
    headers, so a 3xx from an untrusted response would forward the injected
    ``Authorization`` header to whatever origin the response names.  This
    adapter has exactly one fixed endpoint per phase, so a redirect is a typed
    transport failure rather than a hop.
    """

    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


_OPENER = build_opener(_NoRedirectHandler())


def urllib_transport(request: HttpRequest) -> HttpResponse:
    """Perform exactly one request with the Python standard library.

    Redirects are never followed, so the API key can only ever reach the single
    endpoint the adapter itself constructed for that phase.
    """

    prepared = Request(
        request.url,
        data=request.body,
        headers=dict(request.headers),
        method=request.method,
    )
    try:
        with _OPENER.open(prepared, timeout=request.timeout_seconds) as response:
            try:
                payload = response.read()
            except (OSError, HTTPException):
                return HttpResponse(None, b"")
            return HttpResponse(int(response.status), payload)
    except HTTPError as error:
        try:
            payload = error.read()
        except (OSError, HTTPException):
            payload = b""
        return HttpResponse(int(error.code), payload)
    except (URLError, TimeoutError, OSError, HTTPException):
        return HttpResponse(None, b"")


def run_qwen_filetrans(
    source_path: Path,
    raw_output_path: Path,
    *,
    config: QwenFiletransConfig,
    transport: Transport = urllib_transport,
    audio_encoder: AudioEncoder | None = None,
    sleep: Callable[[float], None] = time.sleep,
    phase_callback: PhaseCallback | None = None,
) -> QwenFiletransRun:
    """Run the frozen Cloud path and persist sanitized recognition evidence."""

    require_credentials(config)
    # The destination is immutable normalization evidence, not a scratch file.
    # This run owns it only when it does not already exist, so a failed run can
    # never destroy evidence written by an earlier run.
    if raw_output_path.exists():
        raise QwenFiletransError("recognition_evidence", "evidence_conflict")
    started_at = _now()
    encoder = audio_encoder or (
        lambda media, flac: encode_audio_to_flac(
            media,
            flac,
            ffmpeg_command=config.ffmpeg_command,
            ffmpeg_version=config.ffmpeg_version,
        )
    )
    with tempfile.TemporaryDirectory(prefix="roughcut-cloud-audio-") as directory:
        flac_path = Path(directory) / UPLOAD_FILE_NAME
        if phase_callback is not None:
            phase_callback("transcription_decoding_audio")
        try:
            encoder(source_path, flac_path)
        except FFmpegAudioError as error:
            raise QwenFiletransError(
                "audio_preparation", "audio_preparation_failed"
            ) from error
        if phase_callback is not None:
            phase_callback("transcription_running_asr")
        policy = _request_upload_policy(config, transport)
        oss_url = _upload_audio(flac_path, policy, config, transport)
        task_id = _submit_task(oss_url, config, transport)
        task_output = _poll_task(task_id, config, transport, sleep)
        recognition = _download_recognition(task_output, config, transport)
        sanitized = sanitize_recognition_payload(recognition)
        _write_json_atomically(raw_output_path, sanitized)
    return _run_result(started_at)


def require_credentials(config: QwenFiletransConfig) -> None:
    """Validate the injected credential shape without reading any ambient state."""

    if not credential_fields_are_valid(
        api_key=config.api_key, workspace_id=config.workspace_id
    ):
        raise QwenFiletransError("credential", "missing_credential")


def sanitize_recognition_payload(payload: object) -> dict[str, object]:
    """Project provider recognition content; never persist transport envelopes.

    Only recognition content the normalizer needs is kept.  Locator-bearing or
    secret-bearing provider fields (``file_url``, ``properties`` and anything
    unexpected whose name looks like a URL, token, policy or credential) are
    dropped or fail closed instead of being written as evidence.
    """

    if not isinstance(payload, Mapping):
        raise QwenFiletransError("recognition_evidence", "malformed_response")
    transcripts = payload.get("transcripts")
    if not isinstance(transcripts, list) or not transcripts:
        raise QwenFiletransError("recognition_evidence", "malformed_response")
    _reject_sensitive_fields(payload, _RECOGNITION_TOP_LEVEL_FIELDS)
    sanitized_transcripts: list[dict[str, object]] = []
    for transcript in transcripts:
        if not isinstance(transcript, Mapping):
            raise QwenFiletransError("recognition_evidence", "malformed_response")
        _reject_sensitive_fields(transcript, _RECOGNITION_TRANSCRIPT_FIELDS)
        sentences = transcript.get("sentences")
        if not isinstance(sentences, list) or not sentences:
            raise QwenFiletransError("recognition_evidence", "malformed_response")
        kept_transcript: dict[str, object] = {}
        if "text" in transcript:
            kept_transcript["text"] = _required_text(transcript["text"])
        kept_sentences: list[dict[str, object]] = []
        for sentence in sentences:
            kept_sentences.append(_sanitize_sentence(sentence))
        kept_transcript["sentences"] = kept_sentences
        sanitized_transcripts.append(kept_transcript)
    return {"transcripts": sanitized_transcripts}


def _sanitize_sentence(sentence: object) -> dict[str, object]:
    if not isinstance(sentence, Mapping):
        raise QwenFiletransError("recognition_evidence", "malformed_response")
    _reject_sensitive_fields(sentence, _RECOGNITION_SENTENCE_FIELDS)
    if any(name not in sentence for name in _RECOGNITION_REQUIRED_SENTENCE_FIELDS):
        raise QwenFiletransError("recognition_evidence", "malformed_response")
    words = sentence["words"]
    if not isinstance(words, list) or not words:
        raise QwenFiletransError("recognition_evidence", "malformed_response")
    kept: dict[str, object] = {
        "begin_time": _required_timing(sentence["begin_time"]),
        "end_time": _required_timing(sentence["end_time"]),
        "text": _required_text(sentence["text"]),
    }
    if "speaker_id" in sentence:
        kept["speaker_id"] = _optional_speaker_id(sentence["speaker_id"])
    kept_words: list[dict[str, object]] = []
    for word in words:
        if not isinstance(word, Mapping):
            raise QwenFiletransError("recognition_evidence", "malformed_response")
        _reject_sensitive_fields(word, _RECOGNITION_WORD_FIELDS)
        if any(name not in word for name in _RECOGNITION_REQUIRED_WORD_FIELDS):
            raise QwenFiletransError("recognition_evidence", "malformed_response")
        kept_word: dict[str, object] = {
            "begin_time": _required_timing(word["begin_time"]),
            "end_time": _required_timing(word["end_time"]),
            "text": _required_text(word["text"]),
        }
        if "punctuation" in word:
            kept_word["punctuation"] = _optional_punctuation(word["punctuation"])
        kept_words.append(kept_word)
    kept["words"] = kept_words
    return kept


def _reject_container(value: object) -> None:
    """Refuse a container where the frozen provider shape expects a scalar.

    A mapping or sequence smuggled under an allowed field name would otherwise
    carry a locator or secret straight into the persisted evidence.
    """

    if isinstance(value, (Mapping, list)):
        raise QwenFiletransError("recognition_evidence", "unsafe_provider_shape")


def _required_timing(value: object) -> int:
    """Require the S0-frozen integer-millisecond timestamp shape."""

    _reject_container(value)
    if isinstance(value, bool) or not isinstance(value, int):
        raise QwenFiletransError("recognition_evidence", "malformed_response")
    return value


def _required_text(value: object) -> str:
    _reject_container(value)
    if not isinstance(value, str):
        raise QwenFiletransError("recognition_evidence", "malformed_response")
    return value


def _optional_speaker_id(value: object) -> int | str:
    """Keep a scalar speaker label.

    Only the persistence boundary is enforced here; whether a label is a legal
    provider speaker is recognition semantics, validated once in the normalizer.
    """

    _reject_container(value)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise QwenFiletransError("recognition_evidence", "malformed_response")
    return value


def _optional_punctuation(value: object) -> str | None:
    """Keep a scalar punctuation run.

    The bounded symbol-only rule is recognition semantics and lives in the
    normalizer, which is the layer that appends punctuation to a ``FineUnit``.
    """

    _reject_container(value)
    if value is None or isinstance(value, str):
        return value
    raise QwenFiletransError("recognition_evidence", "malformed_response")


def _reject_sensitive_fields(mapping: Mapping[str, object], keep: frozenset[str]) -> None:
    for name in mapping:
        if name in keep or name in _RECOGNITION_DROPPED_LOCATOR_FIELDS:
            continue
        if not _SENSITIVE_FIELD_NAME.search(str(name)):
            continue
        raise QwenFiletransError("recognition_evidence", "unsafe_provider_shape")


@dataclass(frozen=True)
class _UploadPolicy:
    upload_host: str
    upload_dir: str = field(repr=False)
    access_key_id: str = field(repr=False)
    signature: str = field(repr=False)
    policy: str = field(repr=False)
    object_acl: str
    forbid_overwrite: str


def _request_upload_policy(
    config: QwenFiletransConfig, transport: Transport
) -> _UploadPolicy:
    query = urlencode({"action": "getPolicy", "model": MODEL_NAME})
    response = transport(
        HttpRequest(
            method="GET",
            url=f"{UPLOAD_POLICY_ENDPOINT}?{query}",
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            body=None,
            timeout_seconds=config.request_timeout_seconds,
        )
    )
    payload = _require_json_success(response, "upload_policy")
    data = _response_data(payload)
    if data is None:
        raise QwenFiletransError(
            "upload_policy", "malformed_response", http_status=response.status
        )
    upload_host = _string_field(data, "upload_host")
    upload_dir = _string_field(data, "upload_dir")
    access_key_id = _string_field(data, "oss_access_key_id")
    signature = _string_field(data, "signature")
    policy = _string_field(data, "policy")
    required = (upload_host, upload_dir, access_key_id, signature, policy)
    if any(value is None for value in required):
        raise QwenFiletransError(
            "upload_policy", "malformed_response", http_status=response.status
        )
    assert upload_host is not None
    assert upload_dir is not None
    assert access_key_id is not None
    assert signature is not None
    assert policy is not None
    return _UploadPolicy(
        upload_host=_require_upload_origin(
            upload_host, "upload_policy", http_status=response.status
        ),
        upload_dir=upload_dir,
        access_key_id=access_key_id,
        signature=signature,
        policy=policy,
        object_acl=_string_field(data, "x_oss_object_acl")
        or "private",
        forbid_overwrite=_string_field(data, "x_oss_forbid_overwrite") or "true",
    )


def _upload_audio(
    audio_path: Path,
    policy: _UploadPolicy,
    config: QwenFiletransConfig,
    transport: Transport,
) -> str:
    object_key = f"{policy.upload_dir.rstrip('/')}/{UPLOAD_FILE_NAME}"
    fields = {
        "OSSAccessKeyId": policy.access_key_id,
        "Signature": policy.signature,
        "policy": policy.policy,
        "key": object_key,
        "x-oss-object-acl": policy.object_acl,
        "x-oss-forbid-overwrite": policy.forbid_overwrite,
        "success_action_status": "200",
        "x-oss-content-type": UPLOAD_CONTENT_TYPE,
    }
    body, boundary = _multipart_body(
        fields, audio_path.read_bytes(), filename=UPLOAD_FILE_NAME
    )
    response = transport(
        HttpRequest(
            method="POST",
            url=policy.upload_host,
            headers={
                "Accept": "application/json",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Date": formatdate(usegmt=True),
                "User-Agent": f"roughcut-core/{ROUGHCUT_CORE_VERSION}",
            },
            body=body,
            timeout_seconds=config.upload_timeout_seconds,
        )
    )
    if response.status is None:
        raise QwenFiletransError("upload", "network_failure")
    if not 200 <= response.status < 300:
        raise QwenFiletransError(
            "upload", "http_failure", http_status=response.status
        )
    return f"oss://{object_key}"


def _submit_task(
    oss_url: str, config: QwenFiletransConfig, transport: Transport
) -> str:
    body = json.dumps(
        {
            "model": MODEL_NAME,
            "input": {"file_urls": [oss_url]},
            "parameters": {
                "channel_id": [CHANNEL_ID],
                "language_hints": list(LANGUAGE_HINTS),
                "diarization_enabled": config.speaker_diarization,
            },
        },
        ensure_ascii=False,
    ).encode("utf-8")
    response = transport(
        HttpRequest(
            method="POST",
            url=f"https://{config.workspace_id}.{WORKSPACE_HOST_SUFFIX}{SUBMIT_PATH}",
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-DashScope-Async": "enable",
                "X-DashScope-OssResourceResolve": "enable",
            },
            body=body,
            timeout_seconds=config.request_timeout_seconds,
        )
    )
    payload = _require_json_success(response, "submit")
    output = _task_output(payload)
    task_id = output.get("task_id")
    if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
        raise QwenFiletransError(
            "submit", "malformed_response", http_status=response.status
        )
    return task_id


def _poll_task(
    task_id: str,
    config: QwenFiletransConfig,
    transport: Transport,
    sleep: Callable[[float], None],
) -> Mapping[str, object]:
    deadline = time.monotonic() + config.poll_timeout_seconds
    task_url = (
        f"https://{config.workspace_id}.{WORKSPACE_HOST_SUFFIX}"
        f"{TASK_PATH.format(task_id=task_id)}"
    )
    while True:
        response = transport(
            HttpRequest(
                method="GET",
                url=task_url,
                headers={
                    "Authorization": f"Bearer {config.api_key}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                body=None,
                timeout_seconds=config.request_timeout_seconds,
            )
        )
        payload = _require_json_success(response, "poll")
        output = _task_output(payload)
        status = output.get("task_status")
        if not isinstance(status, str) or not status:
            raise QwenFiletransError(
                "poll", "malformed_response", http_status=response.status
            )
        status = status.upper()
        if status in TERMINAL_TASK_STATUSES:
            if status != "SUCCEEDED":
                raise QwenFiletransError(
                    "poll", "task_failed", http_status=response.status
                )
            _require_subtasks_succeeded(output, response.status)
            return output
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise QwenFiletransError(
                "poll", "poll_timeout", http_status=response.status
            )
        sleep(min(max(config.poll_interval_seconds, 0.05), remaining))


def _download_recognition(
    task_output: Mapping[str, object],
    config: QwenFiletransConfig,
    transport: Transport,
) -> object:
    urls = _result_urls(task_output)
    if len(urls) != 1:
        raise QwenFiletransError("download", "malformed_response")
    result_url = _require_result_url(urls[0], "download")
    response = transport(
        HttpRequest(
            method="GET",
            url=result_url,
            headers={"Accept": "application/json"},
            body=None,
            timeout_seconds=config.request_timeout_seconds,
        )
    )
    return _require_json_success(response, "download")


def _require_json_success(response: HttpResponse, phase: str) -> object:
    if response.status is None:
        raise QwenFiletransError(phase, "network_failure")
    if response.status in {401, 403}:
        raise QwenFiletransError(
            phase, "credential_rejected", http_status=response.status
        )
    if not 200 <= response.status < 300:
        raise QwenFiletransError(phase, "http_failure", http_status=response.status)
    payload = _json_body(response.body)
    if payload is None:
        raise QwenFiletransError(
            phase, "malformed_response", http_status=response.status
        )
    return payload


def _json_body(body: bytes) -> object | None:
    if not body:
        return None
    try:
        parsed: object = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return parsed


def _response_data(payload: object) -> Mapping[str, object] | None:
    """The documented getPolicy shape is ``{"data": {...}}`` and nothing else."""

    if not isinstance(payload, Mapping):
        return None
    data = payload.get("data")
    return data if isinstance(data, Mapping) else None


def _task_output(payload: object) -> Mapping[str, object]:
    """The documented task shape is ``{"output": {...}}`` and nothing else."""

    if isinstance(payload, Mapping):
        output = payload.get("output")
        if isinstance(output, Mapping):
            return output
    return {}


def _require_subtasks_succeeded(output: Mapping[str, object], status: int | None) -> None:
    """Require the S0-evidenced per-file subtask inspection.

    A missing or malformed ``subtask_status`` is an uninterpretable shape, not a
    success, so it fails closed instead of being skipped.
    """

    results = output.get("results")
    if not isinstance(results, list) or not results:
        raise QwenFiletransError("poll", "malformed_response", http_status=status)
    for item in results:
        if not isinstance(item, Mapping):
            raise QwenFiletransError("poll", "malformed_response", http_status=status)
        subtask_status = item.get("subtask_status")
        if not isinstance(subtask_status, str) or not subtask_status:
            raise QwenFiletransError("poll", "malformed_response", http_status=status)
        if subtask_status.upper() != "SUCCEEDED":
            raise QwenFiletransError("poll", "subtask_failed", http_status=status)


def _result_urls(output: Mapping[str, object]) -> list[str]:
    """Collect the S0-evidenced result locator from ``output.results[]``.

    S0 recorded one task-output result entry carrying both ``subtask_status`` and
    ``transcription_url``, mirrored by a top-level ``output.transcription_url``.
    Only the per-result locator is read; the mirror is ignored on purpose, so an
    unevidenced shape fails closed instead of being resolved heuristically.
    """
    urls: list[str] = []
    results = output.get("results")
    if isinstance(results, list):
        for item in results:
            if isinstance(item, Mapping) and isinstance(
                item.get("transcription_url"), str
            ):
                urls.append(item["transcription_url"])
    return urls


def _string_field(data: Mapping[str, object], name: str) -> str | None:
    """Read the documented credential field; the S0 run returned these keys."""

    value = data.get(name)
    return value if isinstance(value, str) and value else None


def _require_authority(
    value: str,
    phase: str,
    pattern: re.Pattern[str],
    *,
    http_status: int | None = None,
) -> SplitResult:
    """Require HTTPS on a documented, well-formed authority.

    The whole authority is compared against its hostname (plus the explicit
    default port), so userinfo (``https://@host``), an explicit empty port
    (``https://host:``), any non-default port and any other host are refused
    instead of merely looking close.
    """

    if any(
        character.isspace() or not character.isprintable() for character in value
    ):
        raise QwenFiletransError(phase, "malformed_response", http_status=http_status)
    try:
        parts = urlsplit(value)
    except ValueError:
        raise QwenFiletransError(
            phase, "malformed_response", http_status=http_status
        ) from None
    host = (parts.hostname or "").lower()
    valid = (
        parts.scheme == "https"
        and parts.netloc.lower() in {host, f"{host}:443"}
        and pattern.fullmatch(host) is not None
    )
    if not valid:
        raise QwenFiletransError(phase, "malformed_response", http_status=http_status)
    return parts


def _require_upload_origin(
    value: str, phase: str, *, http_status: int | None = None
) -> str:
    """The credential response supplies a bare origin; S0 evidences no path."""

    parts = _require_authority(
        value, phase, UPLOAD_ORIGIN_PATTERN, http_status=http_status
    )
    # The delimiter characters are checked in the raw text, because an empty
    # query or fragment (`...?` / `...#`) parses to a falsy component.
    if parts.path not in {"", "/"} or "?" in value or "#" in value:
        raise QwenFiletransError(phase, "malformed_response", http_status=http_status)
    return value


def _require_result_url(
    value: str, phase: str, *, http_status: int | None = None
) -> str:
    """The signed result locator names an object path; its query is the signature."""

    parts = _require_authority(
        value, phase, RESULT_HOST_PATTERN, http_status=http_status
    )
    # A fragment is never sent to the server, so the request target would differ
    # from the locator that was validated.
    if parts.path in {"", "/"} or "#" in value:
        raise QwenFiletransError(phase, "malformed_response", http_status=http_status)
    return value


def _multipart_body(
    fields: Mapping[str, str], payload: bytes, *, filename: str
) -> tuple[bytes, str]:
    boundary = "----roughcut-cloud-" + os.urandom(24).hex()
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            (
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                str(value).encode("utf-8"),
                b"\r\n",
            )
        )
    chunks.extend(
        (
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="file"; '
                f'filename="{filename}"\r\n'
            ).encode(),
            f"Content-Type: {UPLOAD_CONTENT_TYPE}\r\n\r\n".encode(),
            payload,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        )
    )
    return b"".join(chunks), boundary


def _run_result(started_at: str) -> QwenFiletransRun:
    return QwenFiletransRun(
        started_at=started_at, completed_at=_now(), exit_status=0
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _write_json_atomically(path: Path, payload: dict[str, object]) -> None:
    """Publish sanitized evidence without replacing an existing run artifact.

    The destination is the immutable normalization evidence of exactly one run,
    so the caller supplies a per-run destination that does not already exist
    (the entry check above rejects a present one) and this helper refuses a
    destination that appears before publication.  Only this helper ever writes
    ``path``, so a failed run can never delete evidence it does not own.  A
    concurrent second writer to the same destination is not a supported
    precondition; run identity owns destination uniqueness.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(payload, temporary_file, ensure_ascii=False, indent=2, sort_keys=True)
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        if path.exists():
            raise QwenFiletransError("recognition_evidence", "evidence_conflict")
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
