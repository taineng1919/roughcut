"""Closed Project-media OperationRecord schema 1."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal, cast

from roughcut.domain.errors import WorkflowError
from roughcut.domain.media_quota import AAC_SAMPLE_QUOTA_TOLERANCE
from roughcut.domain.workflow import (
    canonical_sha256_v1,
    validate_safe_id,
    validate_sha256,
    validate_timestamp,
)

MEDIA_OPERATION_SCHEMA_VERSION = 1
MEDIA_OPERATION_SCHEMA_V2 = 2
MEDIA_OPERATION_TYPES = frozenset(
    {
        "transcribe_source",
        "proxy_create",
        "approve_export",
        "align_multicam",
        "render_multicam_parallel",
    }
)
MEDIA_OPERATION_STATUSES = frozenset(
    {"pending", "running", "succeeded", "failed", "interrupted"}
)
RUNNING_PHASES = {
    "transcribe_source": (
        "transcription_preparing",
        "transcription_decoding_audio",
        "transcription_running_asr",
        "transcription_normalizing",
        "transcription_publishing_transcript",
        "transcription_synchronizing_binding",
    ),
    "proxy_create": (
        "proxy_preparing",
        "proxy_encoding",
        "proxy_verifying",
        "proxy_publishing",
    ),
    "approve_export": (
        "render_preparing",
        "render_encoding",
        "render_verifying",
        "render_revalidating_basis",
        "render_publishing_workflow",
    ),
    "align_multicam": (
        "alignment_preparing",
        "alignment_decoding_main",
        "alignment_indexing_main",
        "alignment_processing_auxiliary",
        "alignment_revalidating_basis",
        "alignment_publishing",
    ),
    "render_multicam_parallel": (
        "parallel_render_preparing",
        "parallel_render_encoding",
        "parallel_render_verifying",
        "parallel_render_revalidating_basis",
        "parallel_render_publishing",
    ),
}
TERMINAL_PHASES = {
    "transcribe_source": {
        "succeeded": "transcription_succeeded",
        "failed": "transcription_failed",
        "interrupted": "transcription_interrupted",
    },
    "proxy_create": {
        "succeeded": "proxy_succeeded",
        "failed": "proxy_failed",
        "interrupted": "proxy_interrupted",
    },
    "approve_export": {
        "succeeded": "render_succeeded",
        "failed": "render_failed",
        "interrupted": "render_interrupted",
    },
    "align_multicam": {
        "succeeded": "alignment_succeeded",
        "failed": "alignment_failed",
        "interrupted": "alignment_interrupted",
    },
    "render_multicam_parallel": {
        "succeeded": "parallel_render_succeeded",
        "failed": "parallel_render_failed",
        "interrupted": "parallel_render_interrupted",
    },
}
RESPONSIBILITIES = frozenset(
    {
        "roughcut_core",
        "asr_worker",
        "ffmpeg_proxy",
        "ffmpeg_render",
        "host",
        "user_input",
    }
)
FAILURE_ACTIONS = {
    "transcribe_source": frozenset(
        {
            "validate_transcription_basis",
            "decode_transcription_audio",
            "run_asr_worker",
            "normalize_transcript",
            "publish_transcript",
            "synchronize_transcript_binding",
        }
    ),
    "proxy_create": frozenset(
        {
            "validate_proxy_basis",
            "encode_proxy",
            "verify_proxy",
            "publish_proxy",
        }
    ),
    "approve_export": frozenset(
        {
            "validate_export_basis",
            "encode_render",
            "verify_render",
            "publish_export_transaction",
        }
    ),
    "align_multicam": frozenset(
        {
            "validate_alignment_basis",
            "decode_alignment_audio",
            "index_main_fingerprint",
            "recognize_auxiliary",
            "revalidate_alignment_basis",
            "publish_alignment_artifact",
        }
    ),
}
INTERRUPTION_ACTIONS = frozenset(
    {"interrupt_media_operation", "recover_abandoned_media_operation"}
)

MediaOperationType = Literal[
    "transcribe_source",
    "proxy_create",
    "approve_export",
    "align_multicam",
    "render_multicam_parallel",
]
MediaOperationStatus = Literal[
    "pending", "running", "succeeded", "failed", "interrupted"
]

# schema 2 adds the alignment and parallel-render operation types; schema 1
# records keep exactly the three frozen types and remain byte-compatible with
# the schema-1 parser.
SCHEMA2_TYPES = frozenset({"align_multicam", "render_multicam_parallel"})

ALIGNMENT_TERMINAL_CODES = frozenset(
    {
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

PARALLEL_RENDER_TERMINAL_CODES = frozenset(
    {
        "parallel_render_disk_budget_exceeded",
        "parallel_render_all_cameras_failed",
        "parallel_render_basis_changed_during_run",
        "parallel_render_runtime_changed_during_run",
        "parallel_render_staging_failed",
        "parallel_render_final_conflict",
        "parallel_render_publish_failed",
        "parallel_render_interrupted",
    }
)

PARALLEL_FAILURE_EVIDENCE_CODES = frozenset(
    {
        "parallel_camera_encode_failed",
        "parallel_camera_verify_failed",
        "parallel_manifest_publish_failed",
        "parallel_failure_evidence_unavailable",
    }
)
PARALLEL_FAILURE_EVIDENCE_CHECKS = frozenset(
    {
        "ffmpeg_encode",
        "output_presence",
        "verify_probe",
        "video_stream",
        "video_codec",
        "video_dimensions",
        "video_pixel_format",
        "video_frame_rate",
        "video_frame_quota",
        "audio_codec",
        "audio_sample_rate",
        "audio_channels",
        "aac_timeline_quota",
        "aac_decoded_quota",
        "manifest_publish",
        "manifest_publish_recovery",
        "failure_evidence_persistence",
    }
)
PARALLEL_QUOTA_CHECKS = frozenset(
    {"video_frame_quota", "aac_timeline_quota", "aac_decoded_quota"}
)
PARALLEL_STDERR_TAIL_MAX_BYTES = 2048
PARALLEL_STDERR_TAIL_MAX_LINES = 8
PARALLEL_EVIDENCE_TEXT_MAX_CHARS = 256
PARALLEL_EVIDENCE_INTEGER_MAX = 2**53 - 1
PARALLEL_RETURN_CODE_MIN = -(2**31)
PARALLEL_RETURN_CODE_MAX = 2**31 - 1


class MediaOperationError(RuntimeError):
    """Stable Project-media operation failure with a closed error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _error(code: str, evidence: str) -> MediaOperationError:
    return MediaOperationError(
        code,
        f"Roughcut Project-media operation {evidence}",
    )


def validate_media_operation_id(value: object) -> str:
    try:
        return validate_safe_id(value, field="operation_id")
    except WorkflowError as error:
        raise _error(
            "operation_integrity_error",
            "rejected an unsafe operation ID",
        ) from error


def _safe_id(value: object, *, field: str) -> str:
    try:
        return validate_safe_id(value, field=field)
    except WorkflowError as error:
        raise _error(
            "operation_integrity_error",
            f"rejected invalid {field}",
        ) from error


def _sha256(value: object, *, field: str) -> str:
    try:
        return validate_sha256(value, field=field)
    except WorkflowError as error:
        raise _error(
            "operation_integrity_error",
            f"rejected invalid {field}",
        ) from error


def _timestamp(value: object, *, field: str) -> str:
    try:
        return validate_timestamp(value, field=field)
    except WorkflowError as error:
        raise _error(
            "operation_integrity_error",
            f"rejected invalid {field}",
        ) from error


def _closed(
    value: object, fields: set[str], *, description: str
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise _error(
            "operation_integrity_error",
            f"rejected non-closed {description}",
        )
    return cast(dict[str, Any], value)


def _string(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise _error(
            "operation_integrity_error",
            f"rejected invalid {field}",
        )
    return value


def _integer(value: object, *, field: str, minimum: int = 0) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > 9_007_199_254_740_991
    ):
        raise _error(
            "operation_integrity_error",
            f"rejected invalid {field}",
        )
    return value


def _signed_integer(value: object, *, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < PARALLEL_RETURN_CODE_MIN
        or value > PARALLEL_RETURN_CODE_MAX
    ):
        raise _error(
            "operation_integrity_error",
            f"rejected invalid {field}",
        )
    return value


def _bounded_evidence_delta(value: object, *, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < -PARALLEL_EVIDENCE_INTEGER_MAX
        or value > PARALLEL_EVIDENCE_INTEGER_MAX
    ):
        raise _error(
            "operation_integrity_error",
            f"rejected invalid {field}",
        )
    return value


def _bounded_non_negative_integer(value: object, *, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > PARALLEL_EVIDENCE_INTEGER_MAX
    ):
        raise _error(
            "operation_integrity_error",
            f"rejected invalid {field}",
        )
    return value


def _boolean(value: object, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise _error(
            "operation_integrity_error",
            f"rejected invalid {field}",
        )
    return value


def _relative_path(value: object, *, field: str) -> str:
    path_text = _string(value, field=field)
    path = PurePosixPath(path_text)
    if (
        path.is_absolute()
        or not path.parts
        or "." in path.parts
        or ".." in path.parts
        or "\\" in path_text
        or ":" in path_text
    ):
        raise _error(
            "operation_integrity_error",
            f"rejected unsafe {field}",
        )
    return path_text


@dataclass(frozen=True)
class ProjectOperationScope:
    project_id: str
    project_root_hash: str
    kind: str = "project"

    def __post_init__(self) -> None:
        if self.kind != "project":
            raise _error(
                "operation_integrity_error",
                "rejected an invalid Project scope kind",
            )
        _safe_id(self.project_id, field="scope.project_id")
        _sha256(self.project_root_hash, field="scope.project_root_hash")

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "project_id": self.project_id,
            "project_root_hash": self.project_root_hash,
        }

    @classmethod
    def from_dict(cls, value: object) -> ProjectOperationScope:
        data = _closed(
            value,
            {"kind", "project_id", "project_root_hash"},
            description="Project operation scope",
        )
        return cls(
            kind=_string(data["kind"], field="scope.kind"),
            project_id=_safe_id(data["project_id"], field="scope.project_id"),
            project_root_hash=_sha256(
                data["project_root_hash"],
                field="scope.project_root_hash",
            ),
        )


@dataclass(frozen=True)
class ArtifactIdentity:
    artifact_id: str
    schema_version: int
    content_hash: str

    def __post_init__(self) -> None:
        _safe_id(self.artifact_id, field="artifact_id")
        _integer(self.schema_version, field="schema_version", minimum=1)
        _sha256(self.content_hash, field="content_hash")

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "schema_version": self.schema_version,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, value: object) -> ArtifactIdentity:
        data = _closed(
            value,
            {"artifact_id", "schema_version", "content_hash"},
            description="artifact identity",
        )
        return cls(
            artifact_id=_safe_id(data["artifact_id"], field="artifact_id"),
            schema_version=_integer(
                data["schema_version"], field="schema_version", minimum=1
            ),
            content_hash=_sha256(data["content_hash"], field="content_hash"),
        )


@dataclass(frozen=True)
class ReceiptIdentity:
    action_id: str
    receipt_schema_version: int
    receipt_hash: str

    def __post_init__(self) -> None:
        _safe_id(self.action_id, field="action_id")
        if self.receipt_schema_version != 1:
            raise _error(
                "operation_integrity_error",
                "rejected an unsupported receipt schema",
            )
        _sha256(self.receipt_hash, field="receipt_hash")

    def to_dict(self) -> dict[str, object]:
        return {
            "action_id": self.action_id,
            "receipt_schema_version": self.receipt_schema_version,
            "receipt_hash": self.receipt_hash,
        }

    @classmethod
    def from_dict(cls, value: object) -> ReceiptIdentity:
        data = _closed(
            value,
            {"action_id", "receipt_schema_version", "receipt_hash"},
            description="receipt identity",
        )
        return cls(
            action_id=_safe_id(data["action_id"], field="action_id"),
            receipt_schema_version=_integer(
                data["receipt_schema_version"],
                field="receipt_schema_version",
                minimum=1,
            ),
            receipt_hash=_sha256(data["receipt_hash"], field="receipt_hash"),
        )


@dataclass(frozen=True)
class TranscriptOperationResult:
    source_id: str
    transcript_version_id: str
    schema_version: int
    content_hash: str
    project_revision: int
    kind: str = "transcript"

    def __post_init__(self) -> None:
        if self.kind != "transcript" or self.schema_version != 1:
            raise _error(
                "operation_integrity_error",
                "rejected an invalid Transcript result identity",
            )
        _safe_id(self.source_id, field="result_ref.source_id")
        _safe_id(
            self.transcript_version_id,
            field="result_ref.transcript_version_id",
        )
        _sha256(self.content_hash, field="result_ref.content_hash")
        _integer(self.project_revision, field="result_ref.project_revision")

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "source_id": self.source_id,
            "transcript_version_id": self.transcript_version_id,
            "schema_version": self.schema_version,
            "content_hash": self.content_hash,
            "project_revision": self.project_revision,
        }

    @classmethod
    def from_dict(cls, value: object) -> TranscriptOperationResult:
        data = _closed(
            value,
            {
                "kind",
                "source_id",
                "transcript_version_id",
                "schema_version",
                "content_hash",
                "project_revision",
            },
            description="Transcript result ref",
        )
        return cls(
            kind=_string(data["kind"], field="result_ref.kind"),
            source_id=_safe_id(data["source_id"], field="result_ref.source_id"),
            transcript_version_id=_safe_id(
                data["transcript_version_id"],
                field="result_ref.transcript_version_id",
            ),
            schema_version=_integer(
                data["schema_version"],
                field="result_ref.schema_version",
                minimum=1,
            ),
            content_hash=_sha256(
                data["content_hash"], field="result_ref.content_hash"
            ),
            project_revision=_integer(
                data["project_revision"],
                field="result_ref.project_revision",
            ),
        )


@dataclass(frozen=True)
class ProxyOperationResult:
    source_id: str
    cache_key: str
    manifest_schema_version: int
    manifest_content_hash: str
    output_relative_path: str
    output_size: int
    output_sha256_head_tail: str
    project_revision: int
    kind: str = "proxy"

    def __post_init__(self) -> None:
        if self.kind != "proxy" or self.manifest_schema_version != 1:
            raise _error(
                "operation_integrity_error",
                "rejected an invalid Proxy result identity",
            )
        _safe_id(self.source_id, field="result_ref.source_id")
        _sha256(self.cache_key, field="result_ref.cache_key")
        _sha256(
            self.manifest_content_hash,
            field="result_ref.manifest_content_hash",
        )
        output_path = _relative_path(
            self.output_relative_path,
            field="result_ref.output_relative_path",
        )
        expected_path = f"proxies/{self.source_id}/{self.cache_key}/proxy.mp4"
        if output_path != expected_path:
            raise _error(
                "operation_integrity_error",
                "rejected a Proxy output path that differs from its identity",
            )
        _integer(self.output_size, field="result_ref.output_size", minimum=1)
        _sha256(
            self.output_sha256_head_tail,
            field="result_ref.output_sha256_head_tail",
        )
        _integer(self.project_revision, field="result_ref.project_revision")

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "source_id": self.source_id,
            "cache_key": self.cache_key,
            "manifest_schema_version": self.manifest_schema_version,
            "manifest_content_hash": self.manifest_content_hash,
            "output_relative_path": self.output_relative_path,
            "output_size": self.output_size,
            "output_sha256_head_tail": self.output_sha256_head_tail,
            "project_revision": self.project_revision,
        }

    @classmethod
    def from_dict(cls, value: object) -> ProxyOperationResult:
        data = _closed(
            value,
            {
                "kind",
                "source_id",
                "cache_key",
                "manifest_schema_version",
                "manifest_content_hash",
                "output_relative_path",
                "output_size",
                "output_sha256_head_tail",
                "project_revision",
            },
            description="Proxy result ref",
        )
        return cls(
            kind=_string(data["kind"], field="result_ref.kind"),
            source_id=_safe_id(data["source_id"], field="result_ref.source_id"),
            cache_key=_sha256(data["cache_key"], field="result_ref.cache_key"),
            manifest_schema_version=_integer(
                data["manifest_schema_version"],
                field="result_ref.manifest_schema_version",
                minimum=1,
            ),
            manifest_content_hash=_sha256(
                data["manifest_content_hash"],
                field="result_ref.manifest_content_hash",
            ),
            output_relative_path=_relative_path(
                data["output_relative_path"],
                field="result_ref.output_relative_path",
            ),
            output_size=_integer(
                data["output_size"],
                field="result_ref.output_size",
                minimum=1,
            ),
            output_sha256_head_tail=_sha256(
                data["output_sha256_head_tail"],
                field="result_ref.output_sha256_head_tail",
            ),
            project_revision=_integer(
                data["project_revision"],
                field="result_ref.project_revision",
            ),
        )


@dataclass(frozen=True)
class RenderOutputIdentity:
    kind: str
    artifact_id: str
    schema_version: int
    content_hash: str
    project_relative_path: str

    def __post_init__(self) -> None:
        if self.kind not in {"mp4", "manifest"}:
            raise _error(
                "operation_integrity_error",
                "rejected an invalid Render output kind",
            )
        _safe_id(self.artifact_id, field=f"{self.kind}_ref.artifact_id")
        expected_schema = 1 if self.kind == "mp4" else self.schema_version
        if self.schema_version != expected_schema or (
            self.kind == "manifest" and self.schema_version not in {2, 3}
        ):
            raise _error(
                "operation_integrity_error",
                "rejected an invalid Render output schema",
            )
        _sha256(self.content_hash, field=f"{self.kind}_ref.content_hash")
        path = _relative_path(
            self.project_relative_path,
            field=f"{self.kind}_ref.project_relative_path",
        )
        suffix = ".mp4" if self.kind == "mp4" else ".manifest.json"
        if path != f"renders/{self.artifact_id}{suffix}":
            raise _error(
                "operation_integrity_error",
                "rejected a Render output path that differs from its identity",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "artifact_id": self.artifact_id,
            "schema_version": self.schema_version,
            "content_hash": self.content_hash,
            "project_relative_path": self.project_relative_path,
        }

    @classmethod
    def from_dict(cls, value: object) -> RenderOutputIdentity:
        data = _closed(
            value,
            {
                "kind",
                "artifact_id",
                "schema_version",
                "content_hash",
                "project_relative_path",
            },
            description="Render output identity",
        )
        return cls(
            kind=_string(data["kind"], field="output_ref.kind"),
            artifact_id=_safe_id(
                data["artifact_id"], field="output_ref.artifact_id"
            ),
            schema_version=_integer(
                data["schema_version"],
                field="output_ref.schema_version",
                minimum=1,
            ),
            content_hash=_sha256(
                data["content_hash"], field="output_ref.content_hash"
            ),
            project_relative_path=_relative_path(
                data["project_relative_path"],
                field="output_ref.project_relative_path",
            ),
        )


@dataclass(frozen=True)
class RenderOperationResult:
    run_id: str
    render_plan_ref: ArtifactIdentity
    mp4_ref: RenderOutputIdentity
    manifest_ref: RenderOutputIdentity
    approve_export_receipt_ref: ReceiptIdentity
    project_revision: int
    kind: str = "render"

    def __post_init__(self) -> None:
        if self.kind != "render":
            raise _error(
                "operation_integrity_error",
                "rejected an invalid Render result kind",
            )
        _safe_id(self.run_id, field="result_ref.run_id")
        if self.render_plan_ref.schema_version not in {1, 2}:
            raise _error(
                "operation_integrity_error",
                "rejected an unsupported Render Plan schema",
            )
        if (
            self.mp4_ref.kind != "mp4"
            or self.manifest_ref.kind != "manifest"
            or self.mp4_ref.artifact_id != self.render_plan_ref.artifact_id
            or self.manifest_ref.artifact_id != self.render_plan_ref.artifact_id
            or self.manifest_ref.schema_version
            != self.render_plan_ref.schema_version + 1
        ):
            raise _error(
                "operation_integrity_error",
                "rejected an inconsistent Render schema pairing",
            )
        _integer(self.project_revision, field="result_ref.project_revision")

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "run_id": self.run_id,
            "render_plan_ref": self.render_plan_ref.to_dict(),
            "mp4_ref": self.mp4_ref.to_dict(),
            "manifest_ref": self.manifest_ref.to_dict(),
            "approve_export_receipt_ref": (
                self.approve_export_receipt_ref.to_dict()
            ),
            "project_revision": self.project_revision,
        }

    @classmethod
    def from_dict(cls, value: object) -> RenderOperationResult:
        data = _closed(
            value,
            {
                "kind",
                "run_id",
                "render_plan_ref",
                "mp4_ref",
                "manifest_ref",
                "approve_export_receipt_ref",
                "project_revision",
            },
            description="Render result ref",
        )
        return cls(
            kind=_string(data["kind"], field="result_ref.kind"),
            run_id=_safe_id(data["run_id"], field="result_ref.run_id"),
            render_plan_ref=ArtifactIdentity.from_dict(
                data["render_plan_ref"]
            ),
            mp4_ref=RenderOutputIdentity.from_dict(data["mp4_ref"]),
            manifest_ref=RenderOutputIdentity.from_dict(
                data["manifest_ref"]
            ),
            approve_export_receipt_ref=ReceiptIdentity.from_dict(
                data["approve_export_receipt_ref"]
            ),
            project_revision=_integer(
                data["project_revision"],
                field="result_ref.project_revision",
            ),
        )


@dataclass(frozen=True)
class AlignmentOperationResult:
    alignment_id: str
    schema_version: int
    content_hash: str
    kind: str = "multicam_alignment"

    def __post_init__(self) -> None:
        if self.kind != "multicam_alignment" or self.schema_version != 1:
            raise _error(
                "operation_integrity_error",
                "rejected an invalid Alignment result identity",
            )
        _safe_id(self.alignment_id, field="result_ref.alignment_id")
        _sha256(self.content_hash, field="result_ref.content_hash")

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "alignment_id": self.alignment_id,
            "schema_version": self.schema_version,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, value: object) -> AlignmentOperationResult:
        data = _closed(
            value,
            {
                "kind",
                "alignment_id",
                "schema_version",
                "content_hash",
            },
            description="Alignment result ref",
        )
        return cls(
            kind=_string(data["kind"], field="result_ref.kind"),
            alignment_id=_safe_id(
                data["alignment_id"], field="result_ref.alignment_id"
            ),
            schema_version=_integer(
                data["schema_version"],
                field="result_ref.schema_version",
                minimum=1,
            ),
            content_hash=_sha256(
                data["content_hash"], field="result_ref.content_hash"
            ),
        )


@dataclass(frozen=True)
class ParallelRenderOperationResult:
    parallel_render_id: str
    schema_version: int
    manifest_content_hash: str
    kind: str = "multicam_parallel_render"
    partial_failure_evidence: ParallelFailureEvidence | None = None

    def __post_init__(self) -> None:
        if self.kind != "multicam_parallel_render" or self.schema_version != 1:
            raise _error(
                "operation_integrity_error",
                "rejected an invalid parallel render result identity",
            )
        _safe_id(self.parallel_render_id, field="result_ref.parallel_render_id")
        _sha256(
            self.manifest_content_hash,
            field="result_ref.manifest_content_hash",
        )
        if self.partial_failure_evidence is not None and self.partial_failure_evidence.code not in {
            "parallel_camera_encode_failed",
            "parallel_camera_verify_failed",
            "parallel_failure_evidence_unavailable",
        }:
            raise _error(
                "operation_integrity_error",
                "rejected non-camera partial parallel failure evidence",
            )

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "kind": self.kind,
            "parallel_render_id": self.parallel_render_id,
            "schema_version": self.schema_version,
            "manifest_content_hash": self.manifest_content_hash,
        }
        if self.partial_failure_evidence is not None:
            result["partial_failure_evidence"] = self.partial_failure_evidence.to_dict()
        return result

    @classmethod
    def from_dict(cls, value: object) -> ParallelRenderOperationResult:
        base_fields = {
            "kind",
            "parallel_render_id",
            "schema_version",
            "manifest_content_hash",
        }
        if not isinstance(value, dict):
            raise _error(
                "operation_integrity_error",
                "rejected non-closed parallel render result ref",
            )
        fields = set(value)
        if fields == base_fields:
            data = cast(dict[str, Any], value)
            partial_failure_evidence = None
        elif fields == base_fields | {"partial_failure_evidence"}:
            data = cast(dict[str, Any], value)
            if data["partial_failure_evidence"] is None:
                raise _error(
                    "operation_integrity_error",
                    "rejected null partial parallel failure evidence",
                )
            partial_failure_evidence = ParallelFailureEvidence.from_dict(
                data["partial_failure_evidence"]
            )
        else:
            data = _closed(
                value,
                base_fields,
                description="parallel render result ref",
            )
            partial_failure_evidence = None
        return cls(
            kind=_string(data["kind"], field="result_ref.kind"),
            parallel_render_id=_safe_id(
                data["parallel_render_id"],
                field="result_ref.parallel_render_id",
            ),
            schema_version=_integer(
                data["schema_version"],
                field="result_ref.schema_version",
                minimum=1,
            ),
            manifest_content_hash=_sha256(
                data["manifest_content_hash"],
                field="result_ref.manifest_content_hash",
            ),
            partial_failure_evidence=partial_failure_evidence,
        )


MediaOperationResult = (
    TranscriptOperationResult
    | ProxyOperationResult
    | RenderOperationResult
    | AlignmentOperationResult
    | ParallelRenderOperationResult
)


@dataclass(frozen=True)
class ParallelFailureEvidence:
    """Small, closed evidence attached to a parallel render failure."""

    code: str
    camera_id: str | None
    check: str
    expected: int | str | None
    actual: int | str | None
    delta: int | None
    tolerance: int | None
    return_code: int | None
    stderr_tail: str | None

    def __post_init__(self) -> None:
        if self.code not in PARALLEL_FAILURE_EVIDENCE_CODES:
            raise _error(
                "operation_integrity_error",
                "rejected an unknown parallel failure evidence code",
            )
        if self.camera_id is not None:
            _safe_id(self.camera_id, field="error.evidence.camera_id")
        if self.check not in PARALLEL_FAILURE_EVIDENCE_CHECKS:
            raise _error(
                "operation_integrity_error",
                "rejected an unknown parallel failure evidence check",
            )
        for field, value in (
            ("expected", self.expected),
            ("actual", self.actual),
        ):
            if value is None:
                continue
            if isinstance(value, int) and not isinstance(value, bool):
                _bounded_non_negative_integer(
                    value, field=f"error.evidence.{field}"
                )
                continue
            if (
                not isinstance(value, str)
                or not value
                or len(value) > PARALLEL_EVIDENCE_TEXT_MAX_CHARS
            ):
                raise _error(
                    "operation_integrity_error",
                    f"rejected invalid error.evidence.{field}",
                )
        if self.delta is not None:
            _bounded_evidence_delta(self.delta, field="error.evidence.delta")
        if self.tolerance is not None:
            _bounded_non_negative_integer(
                self.tolerance, field="error.evidence.tolerance"
            )
        if self.return_code is not None:
            _signed_integer(self.return_code, field="error.evidence.return_code")
        if self.stderr_tail is not None:
            if not isinstance(self.stderr_tail, str):
                raise _error(
                    "operation_integrity_error",
                    "rejected invalid error.evidence.stderr_tail",
                )
            if (
                len(self.stderr_tail.encode("utf-8"))
                > PARALLEL_STDERR_TAIL_MAX_BYTES
                or len(self.stderr_tail.splitlines())
                > PARALLEL_STDERR_TAIL_MAX_LINES
            ):
                raise _error(
                    "operation_integrity_error",
                    "rejected an oversized error.evidence.stderr_tail",
                )

        if self.code == "parallel_camera_encode_failed":
            if (
                self.camera_id is None
                or self.check != "ffmpeg_encode"
                or self.return_code is None
                or self.stderr_tail is None
                or any(
                    value is not None
                    for value in (
                        self.expected,
                        self.actual,
                        self.delta,
                        self.tolerance,
                    )
                )
            ):
                raise _error(
                    "operation_integrity_error",
                    "rejected incomplete parallel encode evidence",
                )
        elif self.code == "parallel_camera_verify_failed":
            if (
                self.camera_id is None
                or self.return_code is not None
                or self.stderr_tail is not None
            ):
                raise _error(
                    "operation_integrity_error",
                    "rejected invalid parallel verify evidence",
                )
            if self.check in PARALLEL_QUOTA_CHECKS:
                if not isinstance(self.expected, int) or isinstance(
                    self.expected, bool
                ):
                    raise _error(
                        "operation_integrity_error",
                        "rejected a parallel quota evidence without expected value",
                    )
                if self.actual is not None and (
                    not isinstance(self.actual, int)
                    or isinstance(self.actual, bool)
                ):
                    raise _error(
                        "operation_integrity_error",
                        "rejected a non-integer parallel quota actual value",
                    )
                if self.actual is None:
                    if self.delta is not None:
                        raise _error(
                            "operation_integrity_error",
                            "rejected a quota delta without an actual value",
                        )
                elif self.delta != self.actual - self.expected:
                    raise _error(
                        "operation_integrity_error",
                        "rejected a parallel quota delta that does not match",
                    )
                expected_tolerance = (
                    AAC_SAMPLE_QUOTA_TOLERANCE
                    if self.check
                    in {"aac_timeline_quota", "aac_decoded_quota"}
                    else 0
                )
                if self.tolerance != expected_tolerance:
                    raise _error(
                        "operation_integrity_error",
                        "rejected a parallel quota tolerance",
                    )
            elif self.delta is not None or self.tolerance is not None:
                raise _error(
                    "operation_integrity_error",
                    "rejected delta or tolerance on a non-quota check",
                )
        elif self.code == "parallel_manifest_publish_failed":
            if (
                self.camera_id is not None
                or self.check not in {"manifest_publish", "manifest_publish_recovery"}
                or not isinstance(self.expected, str)
                or not isinstance(self.actual, str)
                or self.delta is not None
                or self.tolerance is not None
                or self.return_code is not None
                or self.stderr_tail is not None
            ):
                raise _error(
                    "operation_integrity_error",
                    "rejected invalid parallel publish evidence",
                )
        else:
            if (
                self.check != "failure_evidence_persistence"
                or self.camera_id is not None
                or not isinstance(self.expected, str)
                or not isinstance(self.actual, str)
                or any(
                    value is not None
                    for value in (
                        self.delta,
                        self.tolerance,
                        self.return_code,
                        self.stderr_tail,
                    )
                )
            ):
                raise _error(
                    "operation_integrity_error",
                    "rejected invalid failure evidence fallback",
                )

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "camera_id": self.camera_id,
            "check": self.check,
            "expected": self.expected,
            "actual": self.actual,
            "delta": self.delta,
            "tolerance": self.tolerance,
            "return_code": self.return_code,
            "stderr_tail": self.stderr_tail,
        }

    @classmethod
    def from_dict(cls, value: object) -> ParallelFailureEvidence:
        data = _closed(
            value,
            {
                "code",
                "camera_id",
                "check",
                "expected",
                "actual",
                "delta",
                "tolerance",
                "return_code",
                "stderr_tail",
            },
            description="parallel failure evidence",
        )
        for field in ("camera_id", "check", "stderr_tail"):
            if data[field] is not None and not isinstance(data[field], str):
                raise _error(
                    "operation_integrity_error",
                    f"rejected invalid error.evidence.{field}",
                )
        for field in ("delta", "tolerance", "return_code"):
            if data[field] is not None and (
                not isinstance(data[field], int) or isinstance(data[field], bool)
            ):
                raise _error(
                    "operation_integrity_error",
                    f"rejected invalid error.evidence.{field}",
                )
        return cls(
            code=_string(data["code"], field="error.evidence.code"),
            camera_id=cast(str | None, data["camera_id"]),
            check=_string(data["check"], field="error.evidence.check"),
            expected=cast(int | str | None, data["expected"]),
            actual=cast(int | str | None, data["actual"]),
            delta=cast(int | None, data["delta"]),
            tolerance=cast(int | None, data["tolerance"]),
            return_code=cast(int | None, data["return_code"]),
            stderr_tail=cast(str | None, data["stderr_tail"]),
        )


@dataclass(frozen=True)
class MediaOperationFailure:
    code: str
    responsibility: str
    action: str
    message_code: str
    evidence: ParallelFailureEvidence | None = None

    def __post_init__(self) -> None:
        if self.code not in {
            "media_operation_failed",
            "media_operation_interrupted",
            *ALIGNMENT_TERMINAL_CODES,
            *PARALLEL_RENDER_TERMINAL_CODES,
        }:
            raise _error(
                "operation_integrity_error",
                "rejected an unknown media operation failure code",
            )
        if self.responsibility not in RESPONSIBILITIES:
            raise _error(
                "operation_integrity_error",
                "rejected an unknown media operation responsibility",
            )
        _string(self.action, field="error.action")
        _string(self.message_code, field="error.message_code")
        if self.evidence is not None and self.code not in PARALLEL_RENDER_TERMINAL_CODES:
            raise _error(
                "operation_integrity_error",
                "rejected parallel evidence on a non-parallel operation",
            )

    def _validate_alignment(
        self, operation_type: MediaOperationType, status: MediaOperationStatus
    ) -> None:
        if self.message_code != TERMINAL_PHASES[operation_type][status]:
            raise _error(
                "operation_integrity_error",
                "rejected a failure message code for another operation",
            )
        if status == "interrupted":
            if self.code != "alignment_interrupted":
                raise _error(
                    "operation_integrity_error",
                    "rejected a failure code that differs from record status",
                )
            if self.action not in INTERRUPTION_ACTIONS:
                raise _error(
                    "operation_integrity_error",
                    "rejected a non-interruption action on interrupted record",
                )
            if (
                self.action == "recover_abandoned_media_operation"
                and self.responsibility != "roughcut_core"
            ):
                raise _error(
                    "operation_integrity_error",
                    "rejected an invalid abandoned-operation responsibility",
                )
            if (
                self.action == "interrupt_media_operation"
                and self.responsibility not in {"host", "user_input"}
            ):
                raise _error(
                    "operation_integrity_error",
                    "rejected an invalid interruption responsibility",
                )
            return
        if self.code not in ALIGNMENT_TERMINAL_CODES:
            raise _error(
                "operation_integrity_error",
                "rejected a failure code that differs from record status",
            )
        if self.code == "alignment_interrupted":
            # a failed record must never carry the interrupted code
            raise _error(
                "operation_integrity_error",
                "rejected the interrupted code on a failed record",
            )
        if self.action not in FAILURE_ACTIONS[operation_type]:
            raise _error(
                "operation_integrity_error",
                "rejected a failure action for another operation",
            )
        alignment_actors = {
            "user_input": {"validate_alignment_basis"},
            "roughcut_core": {
                "validate_alignment_basis",
                "decode_alignment_audio",
                "index_main_fingerprint",
                "recognize_auxiliary",
                "revalidate_alignment_basis",
                "publish_alignment_artifact",
            },
        }
        allowed = alignment_actors.get(self.responsibility)
        if allowed is None:
            # only user_input and roughcut_core may own alignment failures
            raise _error(
                "operation_integrity_error",
                "rejected an invalid alignment failure responsibility",
            )
        if self.action not in allowed:
            raise _error(
                "operation_integrity_error",
                "rejected a responsibility/action mismatch",
            )

    def _validate_parallel_render(
        self, operation_type: MediaOperationType, status: MediaOperationStatus
    ) -> None:
        if self.message_code != TERMINAL_PHASES[operation_type][status]:
            raise _error(
                "operation_integrity_error",
                "rejected a parallel render failure message code",
            )
        if status == "interrupted":
            if self.evidence is not None:
                raise _error(
                    "operation_integrity_error",
                    "rejected evidence on an interrupted parallel render",
                )
            if self.code != "parallel_render_interrupted":
                raise _error(
                    "operation_integrity_error",
                    "rejected a failure code that differs from record status",
                )
            if self.action not in INTERRUPTION_ACTIONS:
                raise _error(
                    "operation_integrity_error",
                    "rejected a non-interruption action on interrupted record",
                )
            if self.action == "recover_abandoned_media_operation":
                if self.responsibility != "roughcut_core":
                    raise _error(
                        "operation_integrity_error",
                        "rejected an invalid abandoned-operation responsibility",
                    )
            elif self.responsibility not in {"host", "user_input"}:
                raise _error(
                    "operation_integrity_error",
                    "rejected an invalid interruption responsibility",
                )
            return
        expected = {
            "parallel_render_disk_budget_exceeded": "validate_parallel_render_basis",
            "parallel_render_all_cameras_failed": "render_parallel_cameras",
            "parallel_render_basis_changed_during_run": "revalidate_parallel_render_basis",
            "parallel_render_runtime_changed_during_run": "revalidate_parallel_render_basis",
            "parallel_render_staging_failed": "manage_parallel_render_staging",
            "parallel_render_final_conflict": "publish_parallel_render",
            "parallel_render_publish_failed": "publish_parallel_render",
        }
        if self.code not in expected or self.action != expected[self.code]:
            raise _error(
                "operation_integrity_error",
                "rejected a parallel render error action mapping",
            )
        if self.responsibility != "roughcut_core":
            raise _error(
                "operation_integrity_error",
                "rejected a parallel render failure responsibility",
            )
        if self.evidence is not None:
            allowed_codes = {
                "parallel_render_all_cameras_failed": {
                    "parallel_camera_encode_failed",
                    "parallel_camera_verify_failed",
                    "parallel_failure_evidence_unavailable",
                },
                "parallel_render_final_conflict": {
                    "parallel_manifest_publish_failed",
                    "parallel_failure_evidence_unavailable",
                },
                "parallel_render_publish_failed": {
                    "parallel_manifest_publish_failed",
                    "parallel_failure_evidence_unavailable",
                },
                "parallel_render_staging_failed": {
                    "parallel_failure_evidence_unavailable",
                },
            }
            allowed = allowed_codes.get(self.code, set())
            if self.evidence.code == "parallel_failure_evidence_unavailable":
                allowed = {"parallel_failure_evidence_unavailable"}
            if self.evidence.code not in allowed:
                raise _error(
                    "operation_integrity_error",
                    "rejected parallel evidence for another terminal failure",
                )

    def validate_for(
        self, operation_type: MediaOperationType, status: MediaOperationStatus
    ) -> None:
        if operation_type == "align_multicam":
            self._validate_alignment(operation_type, status)
            return
        if operation_type == "render_multicam_parallel":
            self._validate_parallel_render(operation_type, status)
            return
        expected_code = (
            "media_operation_interrupted"
            if status == "interrupted"
            else "media_operation_failed"
        )
        if self.code != expected_code:
            raise _error(
                "operation_integrity_error",
                "rejected a failure code that differs from record status",
            )
        if self.message_code != TERMINAL_PHASES[operation_type][status]:
            raise _error(
                "operation_integrity_error",
                "rejected a failure message code for another operation",
            )
        if status == "interrupted":
            if self.action not in INTERRUPTION_ACTIONS:
                raise _error(
                    "operation_integrity_error",
                    "rejected a non-interruption action on interrupted record",
                )
            if (
                self.action == "recover_abandoned_media_operation"
                and self.responsibility != "roughcut_core"
            ):
                raise _error(
                    "operation_integrity_error",
                    "rejected an invalid abandoned-operation responsibility",
                )
            if (
                self.action == "interrupt_media_operation"
                and self.responsibility not in {"host", "user_input"}
            ):
                raise _error(
                    "operation_integrity_error",
                    "rejected an invalid interruption responsibility",
                )
            return
        if self.action not in FAILURE_ACTIONS[operation_type]:
            raise _error(
                "operation_integrity_error",
                "rejected a failure action for another operation",
            )
        actor_actions = {
            "asr_worker": {
                "decode_transcription_audio",
                "run_asr_worker",
            },
            "ffmpeg_proxy": {"encode_proxy", "verify_proxy"},
            "ffmpeg_render": {"encode_render", "verify_render"},
            "user_input": {
                "validate_transcription_basis",
                "validate_proxy_basis",
                "validate_export_basis",
            },
        }
        allowed = actor_actions.get(self.responsibility)
        if allowed is not None and self.action not in allowed:
            raise _error(
                "operation_integrity_error",
                "rejected a responsibility/action mismatch",
            )
        if self.responsibility == "host":
            raise _error(
                "operation_integrity_error",
                "rejected Host responsibility on a failed record",
            )

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "code": self.code,
            "responsibility": self.responsibility,
            "action": self.action,
            "message_code": self.message_code,
        }
        if self.evidence is not None:
            result["evidence"] = self.evidence.to_dict()
        return result

    @classmethod
    def from_dict(cls, value: object) -> MediaOperationFailure:
        if not isinstance(value, dict):
            raise _error(
                "operation_integrity_error",
                "rejected non-closed media operation error",
            )
        fields = set(value)
        required = {"code", "responsibility", "action", "message_code"}
        if fields == required:
            data = cast(dict[str, Any], value)
            evidence = None
        elif fields == required | {"evidence"}:
            data = cast(dict[str, Any], value)
            evidence = ParallelFailureEvidence.from_dict(data["evidence"])
        else:
            raise _error(
                "operation_integrity_error",
                "rejected non-closed media operation error",
            )
        return cls(
            code=_string(data["code"], field="error.code"),
            responsibility=_string(
                data["responsibility"], field="error.responsibility"
            ),
            action=_string(data["action"], field="error.action"),
            message_code=_string(
                data["message_code"], field="error.message_code"
            ),
            evidence=evidence,
        )


@dataclass(frozen=True)
class MediaOperationRecord:
    operation_id: str
    scope: ProjectOperationScope
    operation_type: MediaOperationType
    request_hash: str
    input_hash: str
    status: MediaOperationStatus
    phase_message_code: str
    created_at: str
    started_at: str | None
    updated_at: str
    finished_at: str | None
    result_ref: MediaOperationResult | None
    error: MediaOperationFailure | None
    schema_version: int = MEDIA_OPERATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        validate_media_operation_id(self.operation_id)
        if self.operation_type not in MEDIA_OPERATION_TYPES:
            raise _error(
                "operation_integrity_error",
                "rejected an unknown media operation schema or type",
            )
        if self.operation_type in SCHEMA2_TYPES:
            if self.schema_version != MEDIA_OPERATION_SCHEMA_V2:
                raise _error(
                    "operation_integrity_error",
                    "rejected an alignment operation without schema version 2",
                )
        elif self.schema_version != MEDIA_OPERATION_SCHEMA_VERSION:
            raise _error(
                "operation_integrity_error",
                "rejected an unknown media operation schema or type",
            )
        _sha256(self.request_hash, field="request_hash")
        _sha256(self.input_hash, field="input_hash")
        if self.status not in MEDIA_OPERATION_STATUSES:
            raise _error(
                "operation_integrity_error",
                "rejected an unknown media operation status",
            )
        _timestamp(self.created_at, field="created_at")
        _timestamp(self.updated_at, field="updated_at")
        if self.updated_at < self.created_at:
            raise _error(
                "operation_integrity_error",
                "rejected an operation whose updated time moved backward",
            )
        self._validate_state_shape()

    def _validate_state_shape(self) -> None:
        phases = RUNNING_PHASES[self.operation_type]
        terminal = TERMINAL_PHASES[self.operation_type]
        if self.status == "pending":
            valid = (
                self.phase_message_code == phases[0]
                and self.started_at is None
                and self.finished_at is None
                and self.result_ref is None
                and self.error is None
            )
        elif self.status == "running":
            valid = (
                self.phase_message_code in phases
                and self.started_at is not None
                and self.finished_at is None
                and self.result_ref is None
                and self.error is None
            )
        elif self.status == "succeeded":
            result_types = {
                "transcribe_source": TranscriptOperationResult,
                "proxy_create": ProxyOperationResult,
                "approve_export": RenderOperationResult,
                "align_multicam": AlignmentOperationResult,
                "render_multicam_parallel": ParallelRenderOperationResult,
            }
            valid = (
                self.phase_message_code == terminal["succeeded"]
                and self.started_at is not None
                and self.finished_at is not None
                and isinstance(
                    self.result_ref, result_types[self.operation_type]
                )
                and self.error is None
            )
        else:
            valid = (
                self.phase_message_code == terminal[self.status]
                and self.started_at is not None
                and self.finished_at is not None
                and self.result_ref is None
                and self.error is not None
            )
            if valid:
                assert self.error is not None
                self.error.validate_for(self.operation_type, self.status)
        if not valid:
            raise _error(
                "operation_integrity_error",
                "rejected an inconsistent media operation status payload",
            )
        for field, value in (
            ("started_at", self.started_at),
            ("finished_at", self.finished_at),
        ):
            if value is not None:
                _timestamp(value, field=field)
                if value < self.created_at:
                    raise _error(
                        "operation_integrity_error",
                        f"rejected an operation whose {field} moved backward",
                    )
        if self.started_at is not None and self.started_at > self.updated_at:
            raise _error(
                "operation_integrity_error",
                "rejected an operation updated before it started",
            )
        if self.finished_at is not None and self.finished_at != self.updated_at:
            raise _error(
                "operation_integrity_error",
                "rejected a terminal operation with inconsistent finish time",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "operation_id": self.operation_id,
            "scope": self.scope.to_dict(),
            "operation_type": self.operation_type,
            "request_hash": self.request_hash,
            "input_hash": self.input_hash,
            "status": self.status,
            "phase_message_code": self.phase_message_code,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "result_ref": (
                None if self.result_ref is None else self.result_ref.to_dict()
            ),
            "error": None if self.error is None else self.error.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: object) -> MediaOperationRecord:
        data = _closed(
            value,
            {
                "schema_version",
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
            },
            description="Project-media OperationRecord",
        )
        schema_version = _integer(
            data["schema_version"], field="schema_version", minimum=1
        )
        operation_type = data["operation_type"]
        if (
            not isinstance(operation_type, str)
            or operation_type not in MEDIA_OPERATION_TYPES
        ):
            raise _error(
                "operation_integrity_error",
                "rejected an unknown media operation type",
            )
        status = data["status"]
        if not isinstance(status, str) or status not in MEDIA_OPERATION_STATUSES:
            raise _error(
                "operation_integrity_error",
                "rejected an unknown media operation status",
            )
        for field in ("started_at", "finished_at"):
            if data[field] is not None and not isinstance(data[field], str):
                raise _error(
                    "operation_integrity_error",
                    f"rejected invalid {field}",
                )
        result: MediaOperationResult | None = None
        if data["result_ref"] is not None:
            result_data = data["result_ref"]
            if not isinstance(result_data, dict):
                raise _error(
                    "operation_integrity_error",
                    "rejected a non-object result ref",
                )
            kind = result_data.get("kind")
            if kind == "transcript":
                result = TranscriptOperationResult.from_dict(result_data)
            elif kind == "proxy":
                result = ProxyOperationResult.from_dict(result_data)
            elif kind == "render":
                result = RenderOperationResult.from_dict(result_data)
            elif kind == "multicam_alignment":
                result = AlignmentOperationResult.from_dict(result_data)
            elif kind == "multicam_parallel_render":
                result = ParallelRenderOperationResult.from_dict(result_data)
            else:
                raise _error(
                    "operation_integrity_error",
                    "rejected an unknown result ref kind",
                )
        return cls(
            schema_version=schema_version,
            operation_id=validate_media_operation_id(data["operation_id"]),
            scope=ProjectOperationScope.from_dict(data["scope"]),
            operation_type=cast(MediaOperationType, operation_type),
            request_hash=_sha256(
                data["request_hash"], field="request_hash"
            ),
            input_hash=_sha256(data["input_hash"], field="input_hash"),
            status=cast(MediaOperationStatus, status),
            phase_message_code=_string(
                data["phase_message_code"],
                field="phase_message_code",
            ),
            created_at=_timestamp(data["created_at"], field="created_at"),
            started_at=cast(str | None, data["started_at"]),
            updated_at=_timestamp(data["updated_at"], field="updated_at"),
            finished_at=cast(str | None, data["finished_at"]),
            result_ref=result,
            error=(
                None
                if data["error"] is None
                else MediaOperationFailure.from_dict(data["error"])
            ),
        )

    @property
    def record_hash(self) -> str:
        return canonical_sha256_v1(self.to_dict())


def project_operation_scope_hash(project_root: str) -> str:
    return canonical_sha256_v1(
        {
            "hash_schema": 1,
            "hash_kind": "project_media_operation_scope",
            "project_root": project_root,
        }
    )


def transcription_request_projection(
    scope: ProjectOperationScope,
    *,
    source_id: str,
    expected_project_revision: int,
    speaker_diarization: bool,
    timeout_milliseconds: int,
) -> dict[str, object]:
    projection: dict[str, object] = {
        "request_schema_version": 1,
        "operation_type": "transcribe_source",
        "scope": scope.to_dict(),
        "source_id": source_id,
        "expected_project_revision": expected_project_revision,
        "transcription_request": {
            "speaker_diarization": speaker_diarization,
            "timeout_milliseconds": timeout_milliseconds,
        },
    }
    _validate_transcription_request(projection)
    return projection


def proxy_request_projection(
    scope: ProjectOperationScope,
    *,
    source_id: str,
    expected_project_revision: int,
) -> dict[str, object]:
    projection: dict[str, object] = {
        "request_schema_version": 1,
        "operation_type": "proxy_create",
        "scope": scope.to_dict(),
        "source_id": source_id,
        "expected_project_revision": expected_project_revision,
    }
    _validate_proxy_request(projection)
    return projection


def approve_export_request_projection(
    scope: ProjectOperationScope,
    *,
    run_id: str,
    action_id: str,
    workflow_action_input_hash: str,
) -> dict[str, object]:
    projection: dict[str, object] = {
        "request_schema_version": 1,
        "operation_type": "approve_export",
        "scope": scope.to_dict(),
        "run_id": run_id,
        "action_id": action_id,
        "workflow_action": {
            "action": "approve_export",
            "input_hash": workflow_action_input_hash,
        },
    }
    _validate_approve_export_request(projection)
    return projection


def hash_transcription_request(value: object) -> str:
    return canonical_sha256_v1(_validate_transcription_request(value))


def hash_proxy_request(value: object) -> str:
    return canonical_sha256_v1(_validate_proxy_request(value))


def hash_approve_export_request(value: object) -> str:
    return canonical_sha256_v1(_validate_approve_export_request(value))


def parallel_render_request_projection(
    scope: ProjectOperationScope,
    *,
    operation_id: str,
    prepare_ref: dict[str, object],
) -> dict[str, object]:
    projection: dict[str, object] = {
        "request_schema_version": 1,
        "operation_type": "render_multicam_parallel",
        "scope": scope.to_dict(),
        "operation_id": validate_media_operation_id(operation_id),
        "prepare_ref": prepare_ref,
    }
    _validate_parallel_render_request(projection)
    return projection


def hash_parallel_render_request(value: object) -> str:
    return canonical_sha256_v1(_validate_parallel_render_request(value))


def _validate_parallel_render_request(value: object) -> dict[str, Any]:
    data = _closed(
        value,
        {
            "request_schema_version",
            "operation_type",
            "scope",
            "operation_id",
            "prepare_ref",
        },
        description="parallel render request",
    )
    _schema_and_type(
        data,
        schema_field="request_schema_version",
        operation_type="render_multicam_parallel",
    )
    ProjectOperationScope.from_dict(data["scope"])
    validate_media_operation_id(data["operation_id"])
    prepare = data["prepare_ref"]
    if not isinstance(prepare, dict):
        raise _error(
            "operation_integrity_error",
            "rejected a non-object parallel render prepare ref",
        )
    # The application owns the complete prepare-ref closed schema. The
    # operation request only binds the exact object without normalizing it.
    return data


def hash_transcription_input(value: object) -> str:
    data = _closed(
        value,
        {
            "input_schema_version",
            "operation_type",
            "project_id",
            "source_ref",
            "expected_project_revision",
            "transcription_config",
        },
        description="transcription execution input",
    )
    _schema_and_type(data, schema_field="input_schema_version", operation_type="transcribe_source")
    _safe_id(data["project_id"], field="project_id")
    _validate_source_ref(data["source_ref"])
    _integer(data["expected_project_revision"], field="expected_project_revision")
    config = _closed(
        data["transcription_config"],
        {
            "config_schema_version",
            "runtime_binding_sha256",
            "python_receipt_hash",
            "model_refs",
            "ffmpeg_tool_selection_hash",
            "speaker_diarization",
            "speaker_mode",
            "timeout_milliseconds",
        },
        description="transcription config",
    )
    if config["config_schema_version"] != 1:
        raise _error("operation_integrity_error", "rejected transcription config schema")
    _sha256(config["runtime_binding_sha256"], field="runtime_binding_sha256")
    _sha256(config["python_receipt_hash"], field="python_receipt_hash")
    model_refs = _closed(
        config["model_refs"],
        {"asr", "vad", "punc", "speaker"},
        description="transcription model refs",
    )
    for name in ("asr", "vad", "punc"):
        _sha256(model_refs[name], field=f"model_refs.{name}")
    speaker = _boolean(config["speaker_diarization"], field="speaker_diarization")
    if speaker:
        _sha256(model_refs["speaker"], field="model_refs.speaker")
        if config["speaker_mode"] != "punc_segment":
            raise _error("operation_integrity_error", "rejected speaker mode")
    elif model_refs["speaker"] is not None or config["speaker_mode"] is not None:
        raise _error("operation_integrity_error", "rejected disabled speaker config")
    _sha256(
        config["ffmpeg_tool_selection_hash"],
        field="ffmpeg_tool_selection_hash",
    )
    _integer(
        config["timeout_milliseconds"],
        field="timeout_milliseconds",
        minimum=1,
    )
    return canonical_sha256_v1(data)


def hash_proxy_input(value: object) -> str:
    data = _closed(
        value,
        {
            "input_schema_version",
            "operation_type",
            "project_id",
            "source_ref",
            "expected_project_revision",
            "cache_key",
            "profile",
            "tool_refs",
        },
        description="Proxy execution input",
    )
    _schema_and_type(data, schema_field="input_schema_version", operation_type="proxy_create")
    _safe_id(data["project_id"], field="project_id")
    _validate_source_ref(data["source_ref"])
    _integer(data["expected_project_revision"], field="expected_project_revision")
    _sha256(data["cache_key"], field="cache_key")
    _validate_proxy_profile(data["profile"])
    tools = _closed(
        data["tool_refs"],
        {
            "runtime_binding_sha256",
            "ffmpeg_tool_selection_hash",
            "ffprobe_tool_selection_hash",
        },
        description="Proxy tool refs",
    )
    for field in tools:
        _sha256(tools[field], field=f"tool_refs.{field}")
    return canonical_sha256_v1(data)


def hash_approve_export_input(value: object) -> str:
    data = _closed(
        value,
        {
            "input_schema_version",
            "operation_type",
            "project_id",
            "run_id",
            "approve_export_action",
            "roughcut_approval_receipt_ref",
            "decision_ref",
            "expected_project_revision",
            "export_dependency_hash",
            "export_basis_hash",
            "render_plan_ref",
        },
        description="approve_export execution input",
    )
    _schema_and_type(data, schema_field="input_schema_version", operation_type="approve_export")
    _safe_id(data["project_id"], field="project_id")
    _safe_id(data["run_id"], field="run_id")
    action = _closed(
        data["approve_export_action"],
        {"action_id", "input_hash"},
        description="approve_export action identity",
    )
    _safe_id(action["action_id"], field="approve_export_action.action_id")
    _sha256(action["input_hash"], field="approve_export_action.input_hash")
    ReceiptIdentity.from_dict(data["roughcut_approval_receipt_ref"])
    ArtifactIdentity.from_dict(data["decision_ref"])
    _integer(data["expected_project_revision"], field="expected_project_revision")
    _sha256(data["export_dependency_hash"], field="export_dependency_hash")
    _sha256(data["export_basis_hash"], field="export_basis_hash")
    plan = ArtifactIdentity.from_dict(data["render_plan_ref"])
    if plan.schema_version not in {1, 2}:
        raise _error("operation_integrity_error", "rejected Render Plan schema")
    return canonical_sha256_v1(data)


def validate_media_operation_transition(
    before: MediaOperationRecord,
    after: MediaOperationRecord,
) -> None:
    if (
        before.operation_id,
        before.scope,
        before.operation_type,
        before.request_hash,
        before.input_hash,
        before.created_at,
    ) != (
        after.operation_id,
        after.scope,
        after.operation_type,
        after.request_hash,
        after.input_hash,
        after.created_at,
    ):
        raise _error(
            "operation_transition_not_allowed",
            "refused to change immutable media operation identity",
        )
    allowed = {
        "pending": {"running"},
        "running": {"running", "succeeded", "failed", "interrupted"},
        "succeeded": set(),
        "failed": set(),
        "interrupted": set(),
    }
    if after.status not in allowed[before.status]:
        raise _error(
            "operation_transition_not_allowed",
            f"refused transition {before.status}->{after.status}",
        )
    if after.updated_at < before.updated_at:
        raise _error(
            "operation_transition_not_allowed",
            "refused an updated time that moved backward",
        )
    if before.status == after.status == "running":
        phases = RUNNING_PHASES[before.operation_type]
        if phases.index(after.phase_message_code) < phases.index(
            before.phase_message_code
        ):
            raise _error(
                "operation_transition_not_allowed",
                "refused a running phase that moved backward",
            )


def _schema_and_type(
    data: dict[str, Any], *, schema_field: str, operation_type: str
) -> None:
    if data[schema_field] != 1 or data["operation_type"] != operation_type:
        raise _error(
            "operation_integrity_error",
            "rejected unknown projection schema or operation type",
        )


def _validate_transcription_request(value: object) -> dict[str, Any]:
    data = _closed(
        value,
        {
            "request_schema_version",
            "operation_type",
            "scope",
            "source_id",
            "expected_project_revision",
            "transcription_request",
        },
        description="transcription request",
    )
    _schema_and_type(
        data,
        schema_field="request_schema_version",
        operation_type="transcribe_source",
    )
    ProjectOperationScope.from_dict(data["scope"])
    _safe_id(data["source_id"], field="source_id")
    _integer(data["expected_project_revision"], field="expected_project_revision")
    request = _closed(
        data["transcription_request"],
        {"speaker_diarization", "timeout_milliseconds"},
        description="transcription request options",
    )
    _boolean(request["speaker_diarization"], field="speaker_diarization")
    _integer(
        request["timeout_milliseconds"],
        field="timeout_milliseconds",
        minimum=1,
    )
    return data


def _validate_proxy_request(value: object) -> dict[str, Any]:
    data = _closed(
        value,
        {
            "request_schema_version",
            "operation_type",
            "scope",
            "source_id",
            "expected_project_revision",
        },
        description="Proxy request",
    )
    _schema_and_type(
        data,
        schema_field="request_schema_version",
        operation_type="proxy_create",
    )
    ProjectOperationScope.from_dict(data["scope"])
    _safe_id(data["source_id"], field="source_id")
    _integer(data["expected_project_revision"], field="expected_project_revision")
    return data


def _validate_approve_export_request(value: object) -> dict[str, Any]:
    data = _closed(
        value,
        {
            "request_schema_version",
            "operation_type",
            "scope",
            "run_id",
            "action_id",
            "workflow_action",
        },
        description="approve_export request",
    )
    _schema_and_type(
        data,
        schema_field="request_schema_version",
        operation_type="approve_export",
    )
    ProjectOperationScope.from_dict(data["scope"])
    _safe_id(data["run_id"], field="run_id")
    _safe_id(data["action_id"], field="action_id")
    action = _closed(
        data["workflow_action"],
        {"action", "input_hash"},
        description="approve_export workflow action",
    )
    if action["action"] != "approve_export":
        raise _error(
            "operation_integrity_error",
            "rejected workflow action for another operation",
        )
    _sha256(action["input_hash"], field="workflow_action.input_hash")
    return data


def _validate_source_ref(value: object) -> None:
    data = _closed(
        value,
        {"source_id", "source_snapshot_hash"},
        description="Source ref",
    )
    _safe_id(data["source_id"], field="source_ref.source_id")
    _sha256(
        data["source_snapshot_hash"],
        field="source_ref.source_snapshot_hash",
    )


def _validate_proxy_profile(value: object) -> None:
    data = _closed(
        value,
        {
            "schema_version",
            "profile_version",
            "canvas",
            "frame_rate",
            "gop_frames",
            "has_video",
            "has_audio",
            "container",
            "video_codec",
            "pixel_format",
            "crf",
            "preset",
            "faststart",
            "audio_codec",
            "audio_sample_rate",
        },
        description="Proxy profile",
    )
    if data["schema_version"] != 1 or data["profile_version"] != 1:
        raise _error("operation_integrity_error", "rejected Proxy profile schema")
    canvas = _closed(
        data["canvas"], {"width", "height"}, description="Proxy canvas"
    )
    _integer(canvas["width"], field="profile.canvas.width", minimum=1)
    _integer(canvas["height"], field="profile.canvas.height", minimum=1)
    rate = _closed(
        data["frame_rate"],
        {"numerator", "denominator"},
        description="Proxy frame rate",
    )
    _integer(rate["numerator"], field="profile.frame_rate.numerator", minimum=1)
    _integer(rate["denominator"], field="profile.frame_rate.denominator", minimum=1)
    for field in ("gop_frames", "crf", "audio_sample_rate"):
        _integer(data[field], field=f"profile.{field}", minimum=1)
    for field in ("has_video", "has_audio", "faststart"):
        _boolean(data[field], field=f"profile.{field}")
    fixed = {
        "container": "mp4",
        "video_codec": "libx264",
        "pixel_format": "yuv420p",
        "preset": "veryfast",
        "audio_codec": "aac",
    }
    if any(data[field] != expected for field, expected in fixed.items()):
        raise _error(
            "operation_integrity_error",
            "rejected unsupported Proxy profile value",
        )
