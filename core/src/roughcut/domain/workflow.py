"""Finite-workflow schema 1 value objects and canonical hashing."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, Literal, TypeAlias, cast

from roughcut.domain.errors import ProjectError, WorkflowError
from roughcut.domain.project import Project

SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")

WORKFLOW_STAGES = frozenset(
    {
        "scope_review",
        "outline_review",
        "draft_review",
        "roughcut_review",
        "export_review",
        "exporting",
    }
)
WORKFLOW_LIFECYCLES = frozenset({"active", "completed", "canceled"})
WORKFLOW_ACTIONS = frozenset(
    {
        "approve_scope",
        "confirm_brief",
        "submit_outline",
        "approve_outline",
        "submit_draft",
        "approve_draft",
        "return_to_draft",
        "adopt_roughcut",
        "approve_export",
    }
)
WORKFLOW_RECEIPT_ACTIONS = WORKFLOW_ACTIONS | {"workflow_cancel"}
APPROVAL_GATES = frozenset({"scope", "brief", "outline", "draft", "roughcut", "export"})
GATE_ACTION = {
    "scope": "approve_scope",
    "brief": "confirm_brief",
    "outline": "approve_outline",
    "draft": "approve_draft",
    "roughcut": "adopt_roughcut",
    "export": "approve_export",
}
GATE_SUBJECT_KIND = {
    "scope": "scope_snapshot",
    "brief": "brief",
    "outline": "outline_snapshot",
    "draft": "content_draft",
    "roughcut": "proposal",
    "export": "export_snapshot",
}
ACTION_MUTATION_KIND = {
    "approve_scope": None,
    "confirm_brief": "brief",
    "submit_outline": None,
    "approve_outline": None,
    "submit_draft": "content_draft",
    "approve_draft": "content_draft",
    "return_to_draft": None,
    "adopt_roughcut": "decision",
    "approve_export": "render",
    "workflow_cancel": None,
}
ACTION_PROJECT_REVISION_DELTA = {
    "approve_scope": 0,
    "confirm_brief": 1,
    "submit_outline": 0,
    "approve_outline": 0,
    "submit_draft": 0,
    "approve_draft": 1,
    "return_to_draft": 0,
    "adopt_roughcut": 1,
    "approve_export": 0,
    "workflow_cancel": 0,
}
ACTION_TRANSITIONS = {
    "approve_scope": frozenset(
        {
            ("scope_review", "scope_review"),
            ("outline_review", "scope_review"),
            ("draft_review", "scope_review"),
        }
    ),
    "confirm_brief": frozenset(
        {
            ("scope_review", "scope_review"),
            ("outline_review", "scope_review"),
            ("draft_review", "scope_review"),
        }
    ),
    "submit_outline": frozenset(
        {
            ("scope_review", "outline_review"),
            ("outline_review", "outline_review"),
        }
    ),
    "approve_outline": frozenset({("outline_review", "draft_review")}),
    "submit_draft": frozenset({("draft_review", "draft_review")}),
    "approve_draft": frozenset({("draft_review", "roughcut_review")}),
    "return_to_draft": frozenset(
        {
            ("roughcut_review", "draft_review"),
            ("export_review", "draft_review"),
        }
    ),
    "adopt_roughcut": frozenset({("roughcut_review", "export_review")}),
    "approve_export": frozenset({("export_review", "exporting")}),
    "workflow_cancel": frozenset(
        {
            ("scope_review", "scope_review"),
            ("outline_review", "outline_review"),
            ("draft_review", "draft_review"),
            ("roughcut_review", "roughcut_review"),
            ("export_review", "export_review"),
            ("exporting", "exporting"),
        }
    ),
}
JsonValue: TypeAlias = None | bool | int | str | list["JsonValue"] | dict[str, "JsonValue"]


def _error(message: str) -> WorkflowError:
    return WorkflowError("workflow_integrity_error", message)


def validate_safe_id(value: object, *, field: str) -> str:
    if not isinstance(value, str) or SAFE_ID_PATTERN.fullmatch(value) is None:
        raise _error(f"{field} must be a safe workflow ID")
    return value


def validate_sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise _error(f"{field} must be a lowercase SHA-256")
    return value


def validate_timestamp(value: object, *, field: str) -> str:
    if not isinstance(value, str) or TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise _error(f"{field} must be a workflow UTC timestamp")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError as error:
        raise _error(f"{field} must be a valid workflow UTC timestamp") from error
    return value


def load_closed_json(payload: str | bytes) -> dict[str, Any]:
    """Load a JSON object while rejecting duplicate keys and non-integer numbers."""

    if (isinstance(payload, bytes) and payload.startswith(b"\xef\xbb\xbf")) or (
        isinstance(payload, str) and payload.startswith("\ufeff")
    ):
        raise _error("workflow JSON must not contain a UTF-8 BOM")

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise _error(f"JSON contains duplicate key {key!r}")
            result[key] = value
        return result

    def reject_number(value: str) -> Any:
        raise _error(f"JSON number {value!r} is not an integer")

    try:
        value = json.loads(
            payload,
            object_pairs_hook=pairs_hook,
            parse_float=reject_number,
            parse_constant=reject_number,
        )
    except WorkflowError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _error("workflow JSON is unreadable") from error
    if not isinstance(value, dict):
        raise _error("workflow JSON must contain an object")
    return value


def _normalize_string(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    if any(0xD800 <= ord(character) <= 0xDFFF for character in normalized):
        raise _error("canonical JSON rejects unpaired surrogate characters")
    return normalized


def _normalize_json(value: object) -> JsonValue:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise _error("canonical JSON rejects floating-point numbers")
    if isinstance(value, str):
        return _normalize_string(value)
    if isinstance(value, list):
        return [_normalize_json(item) for item in value]
    if isinstance(value, dict):
        normalized: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise _error("canonical JSON object keys must be strings")
            normalized_key = _normalize_string(key)
            if normalized_key in normalized:
                raise _error("canonical JSON contains duplicate normalized keys")
            normalized[normalized_key] = _normalize_json(item)
        return normalized
    raise _error(f"canonical JSON rejects {type(value).__name__}")


def canonical_json_v1(value: object) -> bytes:
    """Return the frozen canonical JSON v1 byte representation."""

    normalized = _normalize_json(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_sha256_v1(value: object) -> str:
    return hashlib.sha256(canonical_json_v1(value)).hexdigest()


def subject_content_hash(subject_kind: str, schema_version: int, content: object) -> str:
    validate_safe_id(subject_kind, field="subject_kind")
    _positive_integer(schema_version, field="schema_version")
    return canonical_sha256_v1(
        {
            "hash_schema": 1,
            "hash_kind": "subject_content",
            "subject_kind": subject_kind,
            "schema_version": schema_version,
            "content": content,
        }
    )


def dependency_bundle_hash(bundle_kind: str, dependencies: object) -> str:
    if bundle_kind not in APPROVAL_GATES:
        raise _error("bundle_kind is unsupported")
    return canonical_sha256_v1(
        {
            "hash_schema": 1,
            "hash_kind": "dependency_bundle",
            "bundle_kind": bundle_kind,
            "schema_version": 1,
            "dependencies": dependencies,
        }
    )


def workflow_action_input_hash(
    run_id: str, action_id: str, action: str, input_payload: object
) -> str:
    validate_safe_id(run_id, field="run_id")
    validate_safe_id(action_id, field="action_id")
    if action not in WORKFLOW_RECEIPT_ACTIONS:
        raise _error("action is unsupported")
    return canonical_sha256_v1(
        {
            "hash_schema": 1,
            "hash_kind": "workflow_action_input",
            "run_id": run_id,
            "action_id": action_id,
            "action": action,
            "input": input_payload,
        }
    )


def _closed(data: object, fields: set[str], *, description: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise _error(f"{description} must be an object")
    if any(not isinstance(key, str) for key in data):
        raise _error(f"{description} field names must be strings")
    actual = set(data)
    if actual != fields:
        missing = sorted(fields - actual)
        extra = sorted(actual - fields)
        raise _error(f"{description} fields are invalid; missing={missing}, extra={extra}")
    return cast(dict[str, Any], data)


def _string(value: object, *, field: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise _error(f"{field} must be a {'string' if allow_empty else 'non-empty string'}")
    _normalize_string(value)
    return value


def _integer(value: object, *, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _error(f"{field} must be an integer >= {minimum}")
    return value


def _positive_integer(value: object, *, field: str) -> int:
    return _integer(value, field=field, minimum=1)


def _optional_hash(value: object, *, field: str) -> str | None:
    return None if value is None else validate_sha256(value, field=field)


@dataclass(frozen=True)
class WorkflowBinding:
    source_id: str
    transcript_version_id: str | None
    transcript_content_hash: str | None

    def __post_init__(self) -> None:
        validate_safe_id(self.source_id, field="source_id")
        if (self.transcript_version_id is None) != (self.transcript_content_hash is None):
            raise _error("binding transcript ID and content hash must both be null or non-null")
        if self.transcript_version_id is not None:
            validate_safe_id(self.transcript_version_id, field="transcript_version_id")
            validate_sha256(self.transcript_content_hash, field="transcript_content_hash")

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "transcript_version_id": self.transcript_version_id,
            "transcript_content_hash": self.transcript_content_hash,
        }

    @classmethod
    def from_dict(cls, data: object) -> WorkflowBinding:
        value = _closed(
            data,
            {"source_id", "transcript_version_id", "transcript_content_hash"},
            description="workflow binding",
        )
        transcript_id = value["transcript_version_id"]
        if transcript_id is not None:
            transcript_id = validate_safe_id(transcript_id, field="transcript_version_id")
        transcript_hash = value["transcript_content_hash"]
        if transcript_hash is not None:
            transcript_hash = validate_sha256(transcript_hash, field="transcript_content_hash")
        return cls(
            source_id=validate_safe_id(value["source_id"], field="source_id"),
            transcript_version_id=transcript_id,
            transcript_content_hash=transcript_hash,
        )


@dataclass(frozen=True)
class ScopeAuthorization:
    source_id: str
    transcribe: bool
    speaker_diarization: bool

    def __post_init__(self) -> None:
        validate_safe_id(self.source_id, field="scope authorization source_id")
        if not isinstance(self.transcribe, bool) or not isinstance(
            self.speaker_diarization, bool
        ):
            raise _error("scope authorization values must be booleans")
        if self.speaker_diarization and not self.transcribe:
            raise _error("speaker diarization authorization requires transcription")

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "transcribe": self.transcribe,
            "speaker_diarization": self.speaker_diarization,
        }

    @classmethod
    def from_dict(cls, data: object) -> ScopeAuthorization:
        value = _closed(
            data,
            {"source_id", "transcribe", "speaker_diarization"},
            description="scope authorization",
        )
        return cls(
            source_id=validate_safe_id(
                value["source_id"], field="scope authorization source_id"
            ),
            transcribe=value["transcribe"],
            speaker_diarization=value["speaker_diarization"],
        )


@dataclass(frozen=True)
class MulticamCamera:
    camera_id: str
    ordered_source_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        validate_safe_id(self.camera_id, field="multicam camera_id")
        if not self.ordered_source_ids:
            raise _error("multicam camera must contain at least one Source")
        for source_id in self.ordered_source_ids:
            validate_safe_id(source_id, field="multicam camera source_id")
        if len(self.ordered_source_ids) != len(set(self.ordered_source_ids)):
            raise _error("multicam camera contains duplicate Sources")

    def to_dict(self) -> dict[str, object]:
        return {
            "camera_id": self.camera_id,
            "ordered_source_ids": list(self.ordered_source_ids),
        }

    @classmethod
    def from_dict(cls, data: object) -> MulticamCamera:
        value = _closed(
            data,
            {"camera_id", "ordered_source_ids"},
            description="multicam camera",
        )
        source_ids = value["ordered_source_ids"]
        if not isinstance(source_ids, list) or not all(
            isinstance(source_id, str) for source_id in source_ids
        ):
            raise _error("multicam camera ordered_source_ids must be an array of IDs")
        return cls(
            camera_id=validate_safe_id(value["camera_id"], field="multicam camera_id"),
            ordered_source_ids=tuple(
                validate_safe_id(source_id, field="multicam camera source_id")
                for source_id in source_ids
            ),
        )


@dataclass(frozen=True)
class MulticamSourcePair:
    main_source_id: str
    auxiliary_source_id: str

    def __post_init__(self) -> None:
        validate_safe_id(self.main_source_id, field="multicam pair main_source_id")
        validate_safe_id(
            self.auxiliary_source_id, field="multicam pair auxiliary_source_id"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "main_source_id": self.main_source_id,
            "auxiliary_source_id": self.auxiliary_source_id,
        }

    @classmethod
    def from_dict(cls, data: object) -> MulticamSourcePair:
        value = _closed(
            data,
            {"main_source_id", "auxiliary_source_id"},
            description="multicam source pair",
        )
        return cls(
            main_source_id=validate_safe_id(
                value["main_source_id"], field="multicam pair main_source_id"
            ),
            auxiliary_source_id=validate_safe_id(
                value["auxiliary_source_id"],
                field="multicam pair auxiliary_source_id",
            ),
        )


def _validate_multicam_camera_groups(
    main_camera: MulticamCamera,
    auxiliary_cameras: tuple[MulticamCamera, ...],
    source_pairs: tuple[MulticamSourcePair, ...],
) -> tuple[str, ...]:
    if main_camera.camera_id != "main":
        raise _error("multicam main_camera.camera_id must be main")
    auxiliary_ids = [camera.camera_id for camera in auxiliary_cameras]
    if len(auxiliary_ids) != len(set(auxiliary_ids)):
        raise _error("multicam auxiliary cameras must be unique")
    if "main" in auxiliary_ids:
        raise _error("multicam auxiliary camera cannot be main")
    camera_source_ids = [
        *main_camera.ordered_source_ids,
        *(
            source_id
            for camera in auxiliary_cameras
            for source_id in camera.ordered_source_ids
        ),
    ]
    if len(camera_source_ids) != len(set(camera_source_ids)):
        raise _error("multicam setup cannot reuse a Source across cameras")
    main_source_ids = set(main_camera.ordered_source_ids)
    auxiliary_source_ids = {
        source_id
        for camera in auxiliary_cameras
        for source_id in camera.ordered_source_ids
    }
    pair_values = [pair.to_dict() for pair in source_pairs]
    if len(pair_values) != len(
        {canonical_json_v1(pair).decode("utf-8") for pair in pair_values}
    ):
        raise _error("multicam source_pairs must be unique")
    if not auxiliary_cameras and source_pairs:
        raise _error("no-aux multicam setup cannot contain source_pairs")
    for pair in source_pairs:
        if pair.main_source_id not in main_source_ids:
            raise _error("multicam pair main Source is outside main_camera")
        if pair.auxiliary_source_id not in auxiliary_source_ids:
            raise _error("multicam pair auxiliary Source is outside auxiliary cameras")
    return tuple(camera_source_ids)


@dataclass(frozen=True)
class MulticamSetupDeclaration:
    """User-confirmed camera facts before Core derives durable identity facts."""

    schema_version: int
    main_camera: MulticamCamera
    auxiliary_cameras: tuple[MulticamCamera, ...]
    source_pairs: tuple[MulticamSourcePair, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise _error("MulticamSetupDeclaration schema_version is unsupported")
        _validate_multicam_camera_groups(
            self.main_camera, self.auxiliary_cameras, self.source_pairs
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "main_camera": self.main_camera.to_dict(),
            "auxiliary_cameras": [
                camera.to_dict() for camera in self.auxiliary_cameras
            ],
            "source_pairs": [pair.to_dict() for pair in self.source_pairs],
        }

    @classmethod
    def from_dict(cls, data: object) -> MulticamSetupDeclaration:
        value = _closed(
            data,
            {"schema_version", "main_camera", "auxiliary_cameras", "source_pairs"},
            description="MulticamSetupDeclaration",
        )
        auxiliary = value["auxiliary_cameras"]
        pairs = value["source_pairs"]
        if not isinstance(auxiliary, list) or not isinstance(pairs, list):
            raise _error("MulticamSetupDeclaration nested values must be arrays")
        return cls(
            schema_version=_integer(
                value["schema_version"], field="multicam declaration schema_version"
            ),
            main_camera=MulticamCamera.from_dict(value["main_camera"]),
            auxiliary_cameras=tuple(
                MulticamCamera.from_dict(item) for item in auxiliary
            ),
            source_pairs=tuple(MulticamSourcePair.from_dict(item) for item in pairs),
        )


def _multicam_source_snapshot(data: object) -> dict[str, Any]:
    value = _closed(
        data,
        {
            "source_id",
            "import_mode",
            "fingerprint",
            "display_name",
            "tags",
            "note",
        },
        description="multicam source snapshot",
    )
    import_mode = value["import_mode"]
    if not isinstance(import_mode, str) or import_mode not in {"copied", "linked"}:
        raise _error("multicam source snapshot import_mode is unsupported")
    fingerprint = _closed(
        value["fingerprint"],
        {"size", "mtime_ns", "sha256_head_tail"},
        description="multicam source fingerprint",
    )
    tags = value["tags"]
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise _error("multicam source snapshot tags must be an array of strings")
    if len(tags) != len(set(tags)):
        raise _error("multicam source snapshot tags must be unique")
    return {
        "source_id": validate_safe_id(value["source_id"], field="multicam snapshot source_id"),
        "import_mode": import_mode,
        "fingerprint": {
            "size": _integer(fingerprint["size"], field="multicam fingerprint size"),
            "mtime_ns": _integer(
                fingerprint["mtime_ns"], field="multicam fingerprint mtime_ns"
            ),
            "sha256_head_tail": _string(
                fingerprint["sha256_head_tail"],
                field="multicam fingerprint sha256_head_tail",
            ),
        },
        "display_name": _string(value["display_name"], field="multicam snapshot display_name"),
        "tags": list(tags),
        "note": _string(value["note"], field="multicam snapshot note", allow_empty=True),
    }


@dataclass(frozen=True)
class MulticamSetup:
    schema_version: int
    setup_id: str
    project_id: str
    workflow_run_id: str
    main_camera: MulticamCamera
    auxiliary_cameras: tuple[MulticamCamera, ...]
    source_pairs: tuple[MulticamSourcePair, ...]
    asr_scope: tuple[ScopeAuthorization, ...]
    source_snapshots: tuple[dict[str, Any], ...]
    source_snapshot_hash: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise _error("MulticamSetup schema_version is unsupported")
        validate_safe_id(self.setup_id, field="multicam setup_id")
        _string(self.project_id, field="multicam setup project_id")
        validate_safe_id(self.workflow_run_id, field="multicam setup workflow_run_id")
        camera_source_ids = _validate_multicam_camera_groups(
            self.main_camera, self.auxiliary_cameras, self.source_pairs
        )
        authorization_ids = [authorization.source_id for authorization in self.asr_scope]
        if not authorization_ids or len(authorization_ids) != len(set(authorization_ids)):
            raise _error("multicam asr_scope must contain unique authorizations")
        if not set(self.main_camera.ordered_source_ids) <= set(authorization_ids):
            raise _error("multicam main camera Sources must be in asr_scope")
        snapshots = tuple(_multicam_source_snapshot(snapshot) for snapshot in self.source_snapshots)
        snapshot_ids = [cast(str, snapshot["source_id"]) for snapshot in snapshots]
        if tuple(snapshot_ids) != camera_source_ids:
            raise _error("multicam source_snapshots must follow camera Source order")
        if validate_sha256(self.source_snapshot_hash, field="multicam source_snapshot_hash") != (
            self.source_snapshot_hash
        ):
            raise _error("multicam source_snapshot_hash is invalid")
        if canonical_sha256_v1(list(snapshots)) != self.source_snapshot_hash:
            raise _error("multicam source_snapshot_hash does not match snapshots")
        object.__setattr__(self, "source_snapshots", snapshots)
        if self.setup_id != f"mcs_{canonical_sha256_v1(self._identity_payload())[:32]}":
            raise _error("multicam setup_id does not match its canonical identity")

    def _identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "workflow_run_id": self.workflow_run_id,
            "main_camera": self.main_camera.to_dict(),
            "auxiliary_cameras": [camera.to_dict() for camera in self.auxiliary_cameras],
            "source_pairs": [pair.to_dict() for pair in self.source_pairs],
            "asr_scope": [authorization.to_dict() for authorization in self.asr_scope],
            "source_snapshots": list(self.source_snapshots),
            "source_snapshot_hash": self.source_snapshot_hash,
        }

    def to_dict(self) -> dict[str, object]:
        return {"setup_id": self.setup_id, **self._identity_payload()}

    @classmethod
    def from_dict(cls, data: object) -> MulticamSetup:
        value = _closed(
            data,
            {
                "schema_version",
                "setup_id",
                "project_id",
                "workflow_run_id",
                "main_camera",
                "auxiliary_cameras",
                "source_pairs",
                "asr_scope",
                "source_snapshots",
                "source_snapshot_hash",
            },
            description="MulticamSetup",
        )
        auxiliary = value["auxiliary_cameras"]
        pairs = value["source_pairs"]
        authorizations = value["asr_scope"]
        snapshots = value["source_snapshots"]
        if not all(isinstance(items, list) for items in (auxiliary, pairs, authorizations, snapshots)):
            raise _error("MulticamSetup nested values must be arrays")
        return cls(
            schema_version=_integer(value["schema_version"], field="multicam setup schema_version"),
            setup_id=validate_safe_id(value["setup_id"], field="multicam setup_id"),
            project_id=_string(value["project_id"], field="multicam setup project_id"),
            workflow_run_id=validate_safe_id(
                value["workflow_run_id"], field="multicam setup workflow_run_id"
            ),
            main_camera=MulticamCamera.from_dict(value["main_camera"]),
            auxiliary_cameras=tuple(MulticamCamera.from_dict(item) for item in auxiliary),
            source_pairs=tuple(MulticamSourcePair.from_dict(item) for item in pairs),
            asr_scope=tuple(ScopeAuthorization.from_dict(item) for item in authorizations),
            source_snapshots=tuple(_multicam_source_snapshot(item) for item in snapshots),
            source_snapshot_hash=validate_sha256(
                value["source_snapshot_hash"], field="multicam source_snapshot_hash"
            ),
        )


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    schema_version: int
    content_hash: str
    snapshot: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        validate_safe_id(self.artifact_id, field="artifact_id")
        _positive_integer(self.schema_version, field="artifact schema_version")
        validate_sha256(self.content_hash, field="artifact content_hash")
        if self.snapshot is not None:
            _validate_outline_snapshot(self.snapshot)
            if self.content_hash != subject_content_hash(
                "outline_snapshot", 1, self.snapshot
            ):
                raise _error("outline content hash does not match its snapshot")
            expected_id = f"outline_{self.content_hash[:16]}"
            if self.artifact_id != expected_id:
                raise _error("outline artifact ID does not match its content hash")

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "artifact_id": self.artifact_id,
            "schema_version": self.schema_version,
            "content_hash": self.content_hash,
        }
        if self.snapshot is not None:
            value["snapshot"] = self.snapshot
        return value

    @classmethod
    def from_dict(cls, data: object, *, outline: bool = False) -> ArtifactRef:
        fields = {"artifact_id", "schema_version", "content_hash"}
        if outline:
            fields.add("snapshot")
        value = _closed(data, fields, description="artifact ref")
        snapshot = value.get("snapshot")
        if outline:
            snapshot = _validate_outline_snapshot(snapshot)
        return cls(
            artifact_id=validate_safe_id(value["artifact_id"], field="artifact_id"),
            schema_version=_positive_integer(value["schema_version"], field="schema_version"),
            content_hash=validate_sha256(value["content_hash"], field="content_hash"),
            snapshot=snapshot,
        )


def _validate_evidence_ref(data: object) -> dict[str, Any]:
    value = _closed(
        data,
        {
            "source_id",
            "transcript_version_id",
            "segment_id",
            "start_ticks",
            "end_ticks",
        },
        description="outline evidence ref",
    )
    start = _integer(value["start_ticks"], field="start_ticks")
    end = _positive_integer(value["end_ticks"], field="end_ticks")
    if end <= start:
        raise _error("outline evidence range must be non-empty")
    return {
        "source_id": validate_safe_id(value["source_id"], field="source_id"),
        "transcript_version_id": validate_safe_id(
            value["transcript_version_id"], field="transcript_version_id"
        ),
        "segment_id": validate_safe_id(value["segment_id"], field="segment_id"),
        "start_ticks": start,
        "end_ticks": end,
    }


def _validate_outline_snapshot(data: object) -> dict[str, Any]:
    value = _closed(
        data,
        {
            "schema_version",
            "title",
            "opening",
            "sections",
            "ending",
            "required_content_coverage",
            "narration_status",
        },
        description="outline snapshot",
    )
    if value["schema_version"] != 1:
        raise _error("outline snapshot schema_version is unsupported")
    sections = value["sections"]
    if not isinstance(sections, list) or not sections:
        raise _error("outline snapshot must contain a non-empty sections array")
    parsed_sections: list[dict[str, Any]] = []
    section_ids: set[str] = set()
    for section in sections:
        item = _closed(
            section,
            {"section_id", "title", "summary", "target_duration_ticks"},
            description="outline section",
        )
        section_id = validate_safe_id(item["section_id"], field="section_id")
        if section_id in section_ids:
            raise _error("outline section IDs must be unique")
        section_ids.add(section_id)
        parsed_sections.append(
            {
                "section_id": section_id,
                "title": _string(item["title"], field="section title"),
                "summary": _string(item["summary"], field="section summary"),
                "target_duration_ticks": _positive_integer(
                    item["target_duration_ticks"], field="target_duration_ticks"
                ),
            }
        )
    coverage = value["required_content_coverage"]
    if not isinstance(coverage, list):
        raise _error("required_content_coverage must be an array")
    parsed_coverage: list[dict[str, Any]] = []
    for requirement in coverage:
        item = _closed(
            requirement,
            {"requirement", "covered", "evidence_refs"},
            description="content coverage",
        )
        covered = item["covered"]
        refs = item["evidence_refs"]
        if not isinstance(covered, bool) or not isinstance(refs, list):
            raise _error("content coverage fields are invalid")
        parsed_refs = [_validate_evidence_ref(ref) for ref in refs]
        if covered != bool(parsed_refs):
            raise _error("covered requires non-empty evidence refs and vice versa")
        parsed_coverage.append(
            {
                "requirement": _string(item["requirement"], field="requirement"),
                "covered": covered,
                "evidence_refs": parsed_refs,
            }
        )
    narration = value["narration_status"]
    if narration not in {"none", "pending", "to_write", "recorded"}:
        raise _error("narration_status is unsupported")
    return {
        "schema_version": 1,
        "title": _string(value["title"], field="outline title"),
        "opening": _string(value["opening"], field="outline opening"),
        "sections": parsed_sections,
        "ending": _string(value["ending"], field="outline ending"),
        "required_content_coverage": parsed_coverage,
        "narration_status": narration,
    }


@dataclass(frozen=True)
class ApprovalRef:
    approval_id: str
    record_schema_version: int
    record_hash: str

    def __post_init__(self) -> None:
        validate_safe_id(self.approval_id, field="approval_id")
        if self.record_schema_version != 1:
            raise _error("approval ref schema_version is unsupported")
        validate_sha256(self.record_hash, field="record_hash")

    def to_dict(self) -> dict[str, object]:
        return {
            "approval_id": self.approval_id,
            "record_schema_version": self.record_schema_version,
            "record_hash": self.record_hash,
        }

    @classmethod
    def from_dict(cls, data: object) -> ApprovalRef:
        value = _closed(
            data,
            {"approval_id", "record_schema_version", "record_hash"},
            description="approval ref",
        )
        return cls(
            approval_id=validate_safe_id(value["approval_id"], field="approval_id"),
            record_schema_version=_integer(
                value["record_schema_version"], field="record_schema_version"
            ),
            record_hash=validate_sha256(value["record_hash"], field="record_hash"),
        )


@dataclass(frozen=True)
class ReceiptRef:
    action_id: str
    receipt_schema_version: int
    receipt_hash: str

    def __post_init__(self) -> None:
        validate_safe_id(self.action_id, field="action_id")
        if self.receipt_schema_version != 1:
            raise _error("receipt ref schema_version is unsupported")
        validate_sha256(self.receipt_hash, field="receipt_hash")

    def to_dict(self) -> dict[str, object]:
        return {
            "action_id": self.action_id,
            "receipt_schema_version": self.receipt_schema_version,
            "receipt_hash": self.receipt_hash,
        }

    @classmethod
    def from_dict(cls, data: object) -> ReceiptRef:
        value = _closed(
            data,
            {"action_id", "receipt_schema_version", "receipt_hash"},
            description="receipt ref",
        )
        return cls(
            action_id=validate_safe_id(value["action_id"], field="action_id"),
            receipt_schema_version=_integer(
                value["receipt_schema_version"], field="receipt_schema_version"
            ),
            receipt_hash=validate_sha256(value["receipt_hash"], field="receipt_hash"),
        )


@dataclass(frozen=True)
class MulticamAlignmentContinuation:
    """Immutable adopt-to-alignment identity; live status stays in media state."""

    schema_version: int
    adopted_decision_ref: ArtifactRef
    requirement: Literal["not_required", "required"]
    expected_revision: int
    main_audio_stable: bool
    setup_id: str | None
    setup_hash: str | None
    operation_id: str | None
    alignment_id: str | None
    request_hash: str | None
    writer_profile_name: str | None
    writer_profile_version: int | None
    writer_profile_hash: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.adopted_decision_ref, ArtifactRef):
            raise _error("Multicam alignment continuation Decision ref is invalid")
        if self.schema_version != 1:
            raise _error("Multicam alignment continuation schema_version is unsupported")
        if self.requirement not in {"not_required", "required"}:
            raise _error("Multicam alignment continuation requirement is unsupported")
        _integer(self.expected_revision, field="multicam continuation expected_revision", minimum=1)
        if self.main_audio_stable is not True:
            raise _error(
                "Multicam alignment continuation requires the confirmed main audio assertion"
            )
        if self.requirement == "not_required":
            if any(
                value is not None
                for value in (
                    self.setup_id,
                    self.setup_hash,
                    self.operation_id,
                    self.alignment_id,
                    self.request_hash,
                    self.writer_profile_name,
                    self.writer_profile_version,
                    self.writer_profile_hash,
                )
            ):
                raise _error(
                    "not_required multicam continuation cannot carry alignment identity"
                )
            return
        if any(
            value is None
            for value in (
                self.setup_id,
                self.setup_hash,
                self.operation_id,
                self.alignment_id,
                self.request_hash,
                self.writer_profile_name,
                self.writer_profile_version,
                self.writer_profile_hash,
            )
        ):
            raise _error("required multicam continuation has incomplete identity")
        validate_safe_id(self.setup_id, field="multicam continuation setup_id")
        validate_sha256(self.setup_hash, field="multicam continuation setup_hash")
        validate_safe_id(self.operation_id, field="multicam continuation operation_id")
        validate_safe_id(self.alignment_id, field="multicam continuation alignment_id")
        validate_sha256(self.request_hash, field="multicam continuation request_hash")
        _string(
            self.writer_profile_name,
            field="multicam continuation writer_profile_name",
        )
        _integer(
            self.writer_profile_version,
            field="multicam continuation writer_profile_version",
            minimum=1,
        )
        validate_sha256(
            self.writer_profile_hash,
            field="multicam continuation writer_profile_hash",
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "adopted_decision_ref": self.adopted_decision_ref.to_dict(),
            "requirement": self.requirement,
            "expected_revision": self.expected_revision,
            "main_audio_stable": self.main_audio_stable,
            "setup_id": self.setup_id,
            "setup_hash": self.setup_hash,
            "operation_id": self.operation_id,
            "alignment_id": self.alignment_id,
            "request_hash": self.request_hash,
            "writer_profile_name": self.writer_profile_name,
            "writer_profile_version": self.writer_profile_version,
            "writer_profile_hash": self.writer_profile_hash,
        }

    @classmethod
    def from_dict(cls, data: object) -> MulticamAlignmentContinuation:
        value = _closed(
            data,
            {
                "schema_version",
                "adopted_decision_ref",
                "requirement",
                "expected_revision",
                "main_audio_stable",
                "setup_id",
                "setup_hash",
                "operation_id",
                "alignment_id",
                "request_hash",
                "writer_profile_name",
                "writer_profile_version",
                "writer_profile_hash",
            },
            description="MulticamAlignmentContinuation",
        )
        requirement = value["requirement"]
        if not isinstance(requirement, str):
            raise _error("multicam continuation requirement must be a string")
        profile_version = value["writer_profile_version"]
        if profile_version is not None and isinstance(profile_version, bool):
            raise _error("multicam continuation writer profile version is invalid")
        return cls(
            schema_version=_integer(
                value["schema_version"],
                field="multicam continuation schema_version",
            ),
            adopted_decision_ref=ArtifactRef.from_dict(value["adopted_decision_ref"]),
            requirement=cast(Literal["not_required", "required"], requirement),
            expected_revision=_integer(
                value["expected_revision"],
                field="multicam continuation expected_revision",
                minimum=1,
            ),
            main_audio_stable=value["main_audio_stable"],
            setup_id=(
                None
                if value["setup_id"] is None
                else validate_safe_id(
                    value["setup_id"], field="multicam continuation setup_id"
                )
            ),
            setup_hash=(
                None
                if value["setup_hash"] is None
                else validate_sha256(
                    value["setup_hash"], field="multicam continuation setup_hash"
                )
            ),
            operation_id=(
                None
                if value["operation_id"] is None
                else validate_safe_id(
                    value["operation_id"], field="multicam continuation operation_id"
                )
            ),
            alignment_id=(
                None
                if value["alignment_id"] is None
                else validate_safe_id(
                    value["alignment_id"], field="multicam continuation alignment_id"
                )
            ),
            request_hash=(
                None
                if value["request_hash"] is None
                else validate_sha256(
                    value["request_hash"], field="multicam continuation request_hash"
                )
            ),
            writer_profile_name=(
                None
                if value["writer_profile_name"] is None
                else _string(
                    value["writer_profile_name"],
                    field="multicam continuation writer_profile_name",
                )
            ),
            writer_profile_version=(
                None
                if profile_version is None
                else _integer(
                    profile_version,
                    field="multicam continuation writer_profile_version",
                    minimum=1,
                )
            ),
            writer_profile_hash=(
                None
                if value["writer_profile_hash"] is None
                else validate_sha256(
                    value["writer_profile_hash"],
                    field="multicam continuation writer_profile_hash",
                )
            ),
        )


def _parse_artifact_refs(data: object) -> dict[str, ArtifactRef | None]:
    fields = {"brief", "outline", "content_draft", "proposal", "decision", "render"}
    value = _closed(data, fields, description="artifact_refs")
    result = {
        key: (
            None
            if value[key] is None
            else ArtifactRef.from_dict(value[key], outline=key == "outline")
        )
        for key in fields
    }
    for key, ref in result.items():
        if ref is None:
            continue
        allowed_versions = (
            {1, 2}
            if key in {"content_draft", "proposal", "decision", "render"}
            else {1}
        )
        if ref.schema_version not in allowed_versions:
            raise _error(f"{key} artifact schema_version is unsupported")
    return result


def _parse_readiness_basis(data: object) -> dict[str, Any]:
    value = _closed(
        data,
        {
            "scope_subject_hash",
            "brief_subject_hash",
            "required_transcripts",
            "speaker_resolution",
            "blocking_operation_ids",
        },
        description="readiness_basis",
    )
    transcripts = value["required_transcripts"]
    if not isinstance(transcripts, list):
        raise _error("required_transcripts must be an array")
    parsed_transcripts: list[dict[str, Any]] = []
    for transcript in transcripts:
        item = _closed(
            transcript,
            {"source_id", "transcript_version_id", "schema_version", "content_hash"},
            description="required transcript",
        )
        if item["schema_version"] != 1:
            raise _error("required transcript schema_version is unsupported")
        parsed_transcripts.append(
            {
                "source_id": validate_safe_id(item["source_id"], field="source_id"),
                "transcript_version_id": validate_safe_id(
                    item["transcript_version_id"], field="transcript_version_id"
                ),
                "schema_version": 1,
                "content_hash": validate_sha256(item["content_hash"], field="content_hash"),
            }
        )
    speaker = _closed(
        value["speaker_resolution"],
        {"mode", "refs", "waiver_subject_hash"},
        description="speaker_resolution",
    )
    mode = speaker["mode"]
    if mode not in {"not_ready", "no_speakers", "all_mapped", "waived"}:
        raise _error("speaker resolution mode is unsupported")
    refs = speaker["refs"]
    if not isinstance(refs, list):
        raise _error("speaker resolution refs must be an array")
    parsed_refs: list[dict[str, Any]] = []
    for ref in refs:
        item = _closed(
            ref,
            {
                "source_id",
                "transcript_version_id",
                "local_speaker_id",
                "resolution",
                "person_id",
            },
            description="speaker resolution ref",
        )
        resolution = item["resolution"]
        person = item["person_id"]
        if resolution not in {"mapped", "waived"}:
            raise _error("speaker ref resolution is unsupported")
        if resolution == "mapped":
            person = validate_safe_id(person, field="person_id")
        elif person is not None:
            raise _error("waived speaker ref person_id must be null")
        parsed_refs.append(
            {
                "source_id": validate_safe_id(item["source_id"], field="source_id"),
                "transcript_version_id": validate_safe_id(
                    item["transcript_version_id"], field="transcript_version_id"
                ),
                "local_speaker_id": validate_safe_id(
                    item["local_speaker_id"], field="local_speaker_id"
                ),
                "resolution": resolution,
                "person_id": person,
            }
        )
    operation_ids = value["blocking_operation_ids"]
    if not isinstance(operation_ids, list):
        raise _error("blocking_operation_ids must be an array")
    parsed_operations = [
        validate_safe_id(operation, field="blocking operation ID") for operation in operation_ids
    ]
    if len(parsed_operations) != len(set(parsed_operations)):
        raise _error("blocking_operation_ids must be unique")
    waiver = _optional_hash(speaker["waiver_subject_hash"], field="waiver_subject_hash")
    if mode == "waived" and waiver is None:
        raise _error("waived speaker resolution requires waiver_subject_hash")
    if mode != "waived" and waiver is not None:
        raise _error("only waived speaker resolution has waiver_subject_hash")
    return {
        "scope_subject_hash": _optional_hash(
            value["scope_subject_hash"], field="scope_subject_hash"
        ),
        "brief_subject_hash": _optional_hash(
            value["brief_subject_hash"], field="brief_subject_hash"
        ),
        "required_transcripts": parsed_transcripts,
        "speaker_resolution": {
            "mode": mode,
            "refs": parsed_refs,
            "waiver_subject_hash": waiver,
        },
        "blocking_operation_ids": parsed_operations,
    }


@dataclass(frozen=True)
class WorkflowRun:
    schema_version: int
    run_id: str
    project_id: str
    stage: str
    lifecycle: str
    created_at: str
    updated_at: str
    ordered_bindings: tuple[WorkflowBinding, ...]
    scope_authorizations: tuple[ScopeAuthorization, ...]
    artifact_refs: dict[str, ArtifactRef | None]
    readiness_basis: dict[str, Any]
    approval_refs: dict[str, ApprovalRef | None]
    last_receipt_ref: ReceiptRef | None
    multicam_setup: MulticamSetup | None = None
    multicam_alignment_continuation: MulticamAlignmentContinuation | None = None

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise _error("WorkflowRun schema_version is unsupported")
        validate_safe_id(self.run_id, field="run_id")
        _string(self.project_id, field="project_id")
        if self.stage not in WORKFLOW_STAGES or self.lifecycle not in WORKFLOW_LIFECYCLES:
            raise _error("WorkflowRun stage or lifecycle is unsupported")
        if self.lifecycle == "completed" and self.stage != "exporting":
            raise _error("completed WorkflowRun must remain at exporting")
        validate_timestamp(self.created_at, field="created_at")
        validate_timestamp(self.updated_at, field="updated_at")
        source_ids = [binding.source_id for binding in self.ordered_bindings]
        if len(source_ids) != len(set(source_ids)):
            raise _error("WorkflowRun ordered bindings contain duplicate sources")
        authorization_source_ids = [
            authorization.source_id for authorization in self.scope_authorizations
        ]
        if authorization_source_ids not in ([], source_ids):
            raise _error(
                "WorkflowRun scope authorizations must be empty or match ordered bindings"
            )
        if (
            self.approval_refs.get("scope") is not None
            and authorization_source_ids != source_ids
        ):
            raise _error(
                "approved scope requires authorizations for every ordered binding"
            )
        if self.multicam_setup is not None:
            if self.multicam_setup.project_id != self.project_id:
                raise _error("WorkflowRun multicam setup project does not match run")
            if self.multicam_setup.workflow_run_id != self.run_id:
                raise _error("WorkflowRun multicam setup run does not match run")
            if self.multicam_setup.asr_scope != self.scope_authorizations:
                raise _error(
                    "WorkflowRun multicam setup ASR scope does not match run scope"
                )
            if not set(self.multicam_setup.main_camera.ordered_source_ids) <= set(
                source_ids
            ):
                raise _error(
                    "WorkflowRun multicam setup contains a source outside bindings"
                )
        if self.multicam_alignment_continuation is not None:
            decision_ref = self.artifact_refs.get("decision")
            if decision_ref is None or (
                self.multicam_alignment_continuation.adopted_decision_ref
                != decision_ref
            ):
                raise _error(
                    "WorkflowRun multicam continuation decision ref does not match current Decision"
                )
            if self.multicam_alignment_continuation.requirement == "required":
                if self.multicam_setup is None:
                    raise _error(
                        "required multicam continuation needs a durable MulticamSetup"
                    )
                if (
                    self.multicam_alignment_continuation.setup_id
                    != self.multicam_setup.setup_id
                    or self.multicam_alignment_continuation.setup_hash
                    != canonical_sha256_v1(self.multicam_setup.to_dict())
                ):
                    raise _error(
                        "WorkflowRun multicam continuation setup identity does not match"
                    )
            elif self.multicam_setup is not None and self.multicam_setup.auxiliary_cameras:
                raise _error(
                    "required multicam setup cannot use a not_required continuation"
                )
        if set(self.artifact_refs) != {
            "brief",
            "outline",
            "content_draft",
            "proposal",
            "decision",
            "render",
        }:
            raise _error("WorkflowRun artifact_refs fields are invalid")
        if set(self.approval_refs) != APPROVAL_GATES:
            raise _error("WorkflowRun approval_refs fields are invalid")
        if any(
            value is not None and not isinstance(value, ArtifactRef)
            for value in self.artifact_refs.values()
        ):
            raise _error("WorkflowRun artifact_refs values are invalid")
        if any(
            value is not None and not isinstance(value, ApprovalRef)
            for value in self.approval_refs.values()
        ):
            raise _error("WorkflowRun approval_refs values are invalid")
        readiness = _parse_readiness_basis(self.readiness_basis)
        required = readiness["required_transcripts"]
        expected_required = [
            {
                "source_id": binding.source_id,
                "transcript_version_id": binding.transcript_version_id,
                "schema_version": 1,
                "content_hash": binding.transcript_content_hash,
            }
            for binding in self.ordered_bindings
            if binding.transcript_version_id is not None
        ]
        if required != expected_required:
            raise _error("required_transcripts do not match non-null ordered bindings")
        binding_positions = {
            (binding.source_id, binding.transcript_version_id): index
            for index, binding in enumerate(self.ordered_bindings)
            if binding.transcript_version_id is not None
        }
        speaker = readiness["speaker_resolution"]
        speaker_refs = speaker["refs"]
        speaker_keys: set[tuple[str, str, str]] = set()
        speaker_positions: list[int] = []
        for ref in speaker_refs:
            binding_key = (ref["source_id"], ref["transcript_version_id"])
            if binding_key not in binding_positions:
                raise _error("speaker resolution ref is outside ordered bindings")
            speaker_key = (*binding_key, ref["local_speaker_id"])
            if speaker_key in speaker_keys:
                raise _error("speaker resolution refs contain a duplicate local speaker")
            speaker_keys.add(speaker_key)
            speaker_positions.append(binding_positions[binding_key])
        if speaker_positions != sorted(speaker_positions):
            raise _error("speaker resolution refs do not follow binding order")
        if speaker["mode"] == "no_speakers" and speaker_refs:
            raise _error("no_speakers readiness cannot contain speaker refs")
        if speaker["mode"] == "all_mapped" and (
            not speaker_refs
            or any(ref["resolution"] != "mapped" for ref in speaker_refs)
        ):
            raise _error("all_mapped readiness requires mapped speaker refs")
        if speaker["mode"] == "waived" and (
            not speaker_refs
            or not any(ref["resolution"] == "waived" for ref in speaker_refs)
        ):
            raise _error("waived readiness requires at least one waived speaker ref")
        if (
            not self.ordered_bindings
            and (
                self.stage != "scope_review"
                or self.approval_refs["scope"] is not None
            )
        ):
            raise _error("empty ordered bindings are only valid before scope approval")

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "project_id": self.project_id,
            "stage": self.stage,
            "lifecycle": self.lifecycle,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "ordered_bindings": [binding.to_dict() for binding in self.ordered_bindings],
            "scope_authorizations": [
                authorization.to_dict() for authorization in self.scope_authorizations
            ],
            "artifact_refs": {
                key: None if value is None else value.to_dict()
                for key, value in self.artifact_refs.items()
            },
            "readiness_basis": self.readiness_basis,
            "approval_refs": {
                key: None if value is None else value.to_dict()
                for key, value in self.approval_refs.items()
            },
            "last_receipt_ref": (
                None if self.last_receipt_ref is None else self.last_receipt_ref.to_dict()
            ),
        }
        if self.multicam_setup is not None:
            value["multicam_setup"] = self.multicam_setup.to_dict()
        if self.multicam_alignment_continuation is not None:
            value["multicam_alignment_continuation"] = (
                self.multicam_alignment_continuation.to_dict()
            )
        return value

    @classmethod
    def from_dict(cls, data: object) -> WorkflowRun:
        if not isinstance(data, dict):
            raise _error("WorkflowRun must be an object")
        if any(not isinstance(key, str) for key in data):
            raise _error("WorkflowRun field names must be strings")
        legacy_fields = {
            "schema_version",
            "run_id",
            "project_id",
            "stage",
            "lifecycle",
            "created_at",
            "updated_at",
            "ordered_bindings",
            "scope_authorizations",
            "artifact_refs",
            "readiness_basis",
            "approval_refs",
            "last_receipt_ref",
        }
        actual_fields = set(data)
        allowed_fields = legacy_fields | {
            "multicam_setup",
            "multicam_alignment_continuation",
        }
        if not legacy_fields <= actual_fields or not actual_fields <= allowed_fields:
            raise _error(
                "WorkflowRun fields are invalid; "
                f"missing={sorted(legacy_fields - actual_fields)}, "
                f"extra={sorted(actual_fields - legacy_fields)}"
            )
        value = cast(dict[str, Any], data)
        if value["schema_version"] != 1:
            raise _error("WorkflowRun schema_version is unsupported")
        bindings = value["ordered_bindings"]
        authorizations = value["scope_authorizations"]
        if not isinstance(bindings, list) or not isinstance(authorizations, list):
            raise _error("ordered_bindings and scope_authorizations must be arrays")
        approval_data = _closed(
            value["approval_refs"], set(APPROVAL_GATES), description="approval_refs"
        )
        approval_refs = {
            gate: (
                None if approval_data[gate] is None else ApprovalRef.from_dict(approval_data[gate])
            )
            for gate in APPROVAL_GATES
        }
        receipt_data = value["last_receipt_ref"]
        setup_data = value.get("multicam_setup")
        continuation_data = value.get("multicam_alignment_continuation")
        return cls(
            schema_version=1,
            run_id=validate_safe_id(value["run_id"], field="run_id"),
            project_id=_string(value["project_id"], field="project_id"),
            stage=_string(value["stage"], field="stage"),
            lifecycle=_string(value["lifecycle"], field="lifecycle"),
            created_at=validate_timestamp(value["created_at"], field="created_at"),
            updated_at=validate_timestamp(value["updated_at"], field="updated_at"),
            ordered_bindings=tuple(WorkflowBinding.from_dict(binding) for binding in bindings),
            scope_authorizations=tuple(
                ScopeAuthorization.from_dict(authorization)
                for authorization in authorizations
            ),
            artifact_refs=_parse_artifact_refs(value["artifact_refs"]),
            readiness_basis=_parse_readiness_basis(value["readiness_basis"]),
            approval_refs=approval_refs,
            last_receipt_ref=(
                None if receipt_data is None else ReceiptRef.from_dict(receipt_data)
            ),
            multicam_setup=(
                None if setup_data is None else MulticamSetup.from_dict(setup_data)
            ),
            multicam_alignment_continuation=(
                None
                if continuation_data is None
                else MulticamAlignmentContinuation.from_dict(continuation_data)
            ),
        )


@dataclass(frozen=True)
class SubjectRef:
    kind: str
    artifact_id: str
    schema_version: int
    content_hash: str

    def __post_init__(self) -> None:
        validate_safe_id(self.kind, field="subject kind")
        validate_safe_id(self.artifact_id, field="subject artifact_id")
        _positive_integer(self.schema_version, field="subject schema_version")
        validate_sha256(self.content_hash, field="subject content_hash")

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "artifact_id": self.artifact_id,
            "schema_version": self.schema_version,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, data: object) -> SubjectRef:
        value = _closed(
            data,
            {"kind", "artifact_id", "schema_version", "content_hash"},
            description="approval subject",
        )
        return cls(
            kind=validate_safe_id(value["kind"], field="subject kind"),
            artifact_id=validate_safe_id(value["artifact_id"], field="artifact_id"),
            schema_version=_positive_integer(value["schema_version"], field="schema_version"),
            content_hash=validate_sha256(value["content_hash"], field="content_hash"),
        )


@dataclass(frozen=True)
class ApprovalRecord:
    schema_version: int
    approval_id: str
    run_id: str
    project_id: str
    gate: str
    subject: SubjectRef
    dependency_hash: str
    issued_project_revision: int
    issued_by_action_id: str
    source_channel: str
    source_action: str
    actor_assurance: str
    issued_at: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise _error("ApprovalRecord schema_version is unsupported")
        validate_safe_id(self.approval_id, field="approval_id")
        validate_safe_id(self.run_id, field="run_id")
        _string(self.project_id, field="project_id")
        if self.gate not in APPROVAL_GATES:
            raise _error("approval gate is unsupported")
        if self.subject.kind != GATE_SUBJECT_KIND[self.gate]:
            raise _error("approval subject kind does not match gate")
        validate_sha256(self.dependency_hash, field="dependency_hash")
        _integer(self.issued_project_revision, field="issued_project_revision")
        validate_safe_id(self.issued_by_action_id, field="issued_by_action_id")
        if self.source_channel not in {"agent_conversation", "review_application"}:
            raise _error("approval source channel is unsupported")
        if self.source_action != GATE_ACTION[self.gate]:
            raise _error("approval source action does not match gate")
        if self.actor_assurance != "unverified_host_user_action":
            raise _error("approval actor assurance is unsupported")
        validate_timestamp(self.issued_at, field="issued_at")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "approval_id": self.approval_id,
            "run_id": self.run_id,
            "project_id": self.project_id,
            "gate": self.gate,
            "subject": self.subject.to_dict(),
            "dependency_hash": self.dependency_hash,
            "issued_project_revision": self.issued_project_revision,
            "issued_by_action_id": self.issued_by_action_id,
            "source": {
                "channel": self.source_channel,
                "action": self.source_action,
                "actor_assurance": self.actor_assurance,
            },
            "issued_at": self.issued_at,
        }

    @classmethod
    def from_dict(cls, data: object) -> ApprovalRecord:
        value = _closed(
            data,
            {
                "schema_version",
                "approval_id",
                "run_id",
                "project_id",
                "gate",
                "subject",
                "dependency_hash",
                "issued_project_revision",
                "issued_by_action_id",
                "source",
                "issued_at",
            },
            description="ApprovalRecord",
        )
        if value["schema_version"] != 1:
            raise _error("ApprovalRecord schema_version is unsupported")
        source = _closed(
            value["source"],
            {"channel", "action", "actor_assurance"},
            description="approval source",
        )
        return cls(
            schema_version=1,
            approval_id=validate_safe_id(value["approval_id"], field="approval_id"),
            run_id=validate_safe_id(value["run_id"], field="run_id"),
            project_id=_string(value["project_id"], field="project_id"),
            gate=_string(value["gate"], field="gate"),
            subject=SubjectRef.from_dict(value["subject"]),
            dependency_hash=validate_sha256(
                value["dependency_hash"], field="dependency_hash"
            ),
            issued_project_revision=_integer(
                value["issued_project_revision"], field="issued_project_revision"
            ),
            issued_by_action_id=validate_safe_id(
                value["issued_by_action_id"], field="issued_by_action_id"
            ),
            source_channel=_string(source["channel"], field="source channel"),
            source_action=_string(source["action"], field="source action"),
            actor_assurance=_string(source["actor_assurance"], field="actor_assurance"),
            issued_at=validate_timestamp(value["issued_at"], field="issued_at"),
        )

    def effective_status(self, subject: SubjectRef, dependency_hash: str) -> Literal["current", "stale"]:
        validate_sha256(dependency_hash, field="dependency_hash")
        return (
            "current"
            if self.subject == subject and self.dependency_hash == dependency_hash
            else "stale"
        )


@dataclass(frozen=True)
class ReceiptState:
    stage: str
    lifecycle: str
    project_revision: int

    def __post_init__(self) -> None:
        if self.stage not in WORKFLOW_STAGES or self.lifecycle not in WORKFLOW_LIFECYCLES:
            raise _error("receipt state stage or lifecycle is unsupported")
        _integer(self.project_revision, field="project_revision")

    def to_dict(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "lifecycle": self.lifecycle,
            "project_revision": self.project_revision,
        }

    @classmethod
    def from_dict(cls, data: object) -> ReceiptState:
        value = _closed(
            data, {"stage", "lifecycle", "project_revision"}, description="receipt state"
        )
        return cls(
            stage=_string(value["stage"], field="stage"),
            lifecycle=_string(value["lifecycle"], field="lifecycle"),
            project_revision=_integer(value["project_revision"], field="project_revision"),
        )


@dataclass(frozen=True)
class MutationRef:
    kind: str
    artifact_id: str
    schema_version: int
    content_hash: str
    changed: bool

    def __post_init__(self) -> None:
        if self.kind not in {"brief", "content_draft", "decision", "render"}:
            raise _error("receipt mutation kind is unsupported")
        validate_safe_id(self.artifact_id, field="artifact_id")
        _positive_integer(self.schema_version, field="schema_version")
        validate_sha256(self.content_hash, field="content_hash")
        if not isinstance(self.changed, bool):
            raise _error("mutation changed must be a boolean")

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "artifact_id": self.artifact_id,
            "schema_version": self.schema_version,
            "content_hash": self.content_hash,
            "changed": self.changed,
        }

    @classmethod
    def from_dict(cls, data: object) -> MutationRef:
        value = _closed(
            data,
            {"kind", "artifact_id", "schema_version", "content_hash", "changed"},
            description="receipt mutation",
        )
        if not isinstance(value["changed"], bool):
            raise _error("mutation changed must be a boolean")
        return cls(
            kind=_string(value["kind"], field="mutation kind"),
            artifact_id=validate_safe_id(value["artifact_id"], field="artifact_id"),
            schema_version=_positive_integer(value["schema_version"], field="schema_version"),
            content_hash=validate_sha256(value["content_hash"], field="content_hash"),
            changed=value["changed"],
        )


@dataclass(frozen=True)
class OutputRef:
    kind: str
    artifact_id: str
    schema_version: int
    content_hash: str
    project_relative_path: str | None

    def __post_init__(self) -> None:
        validate_safe_id(self.kind, field="output kind")
        validate_safe_id(self.artifact_id, field="artifact_id")
        _positive_integer(self.schema_version, field="schema_version")
        validate_sha256(self.content_hash, field="content_hash")
        if self.project_relative_path is not None:
            path = PurePosixPath(self.project_relative_path)
            if (
                not self.project_relative_path
                or path.is_absolute()
                or not path.parts
                or ".." in path.parts
                or "." in path.parts
                or "\\" in self.project_relative_path
                or ":" in self.project_relative_path
            ):
                raise _error("output project_relative_path is unsafe")

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "artifact_id": self.artifact_id,
            "schema_version": self.schema_version,
            "content_hash": self.content_hash,
            "project_relative_path": self.project_relative_path,
        }

    @classmethod
    def from_dict(cls, data: object) -> OutputRef:
        value = _closed(
            data,
            {
                "kind",
                "artifact_id",
                "schema_version",
                "content_hash",
                "project_relative_path",
            },
            description="receipt output ref",
        )
        return cls(
            kind=validate_safe_id(value["kind"], field="output kind"),
            artifact_id=validate_safe_id(value["artifact_id"], field="artifact_id"),
            schema_version=_positive_integer(value["schema_version"], field="schema_version"),
            content_hash=validate_sha256(value["content_hash"], field="content_hash"),
            project_relative_path=(
                None
                if value["project_relative_path"] is None
                else _string(value["project_relative_path"], field="project_relative_path")
            ),
        )


@dataclass(frozen=True)
class ActionReceipt:
    schema_version: int
    action_id: str
    input_hash: str
    run_id: str
    project_id: str
    action: str
    before: ReceiptState
    after: ReceiptState
    approval_ids: tuple[str, ...]
    mutation: MutationRef | None
    output_refs: tuple[OutputRef, ...]
    created_at: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise _error("ActionReceipt schema_version is unsupported")
        validate_safe_id(self.action_id, field="action_id")
        validate_sha256(self.input_hash, field="input_hash")
        validate_safe_id(self.run_id, field="run_id")
        _string(self.project_id, field="project_id")
        if self.action not in WORKFLOW_RECEIPT_ACTIONS:
            raise _error("receipt action is unsupported")
        if self.before.lifecycle != "active":
            raise _error("workflow action receipt must start from an active run")
        if self.action == "approve_export":
            allowed_after_lifecycles = {"active", "completed"}
        elif self.action == "workflow_cancel":
            allowed_after_lifecycles = {"canceled"}
        else:
            allowed_after_lifecycles = {"active"}
        if self.after.lifecycle not in allowed_after_lifecycles:
            raise _error("receipt lifecycle does not match its workflow action")
        if (self.before.stage, self.after.stage) not in ACTION_TRANSITIONS[self.action]:
            raise _error("receipt transition does not match its workflow action")
        if (
            self.after.project_revision - self.before.project_revision
            != ACTION_PROJECT_REVISION_DELTA[self.action]
        ):
            raise _error("receipt Project revision delta does not match its workflow action")
        for approval_id in self.approval_ids:
            validate_safe_id(approval_id, field="approval_id")
        if len(self.approval_ids) != len(set(self.approval_ids)):
            raise _error("receipt approval_ids must be unique")
        approval_actions = set(GATE_ACTION.values())
        expected_approval_count = 1 if self.action in approval_actions else 0
        if len(self.approval_ids) != expected_approval_count:
            raise _error("receipt approval_ids do not match its workflow action")
        expected_mutation_kind = ACTION_MUTATION_KIND[self.action]
        if expected_mutation_kind is None:
            if self.mutation is not None:
                raise _error("receipt mutation does not match its workflow action")
        elif self.mutation is None or self.mutation.kind != expected_mutation_kind:
            raise _error("receipt mutation does not match its workflow action")
        if self.action == "submit_outline":
            if (
                len(self.output_refs) != 1
                or self.output_refs[0].kind != "outline"
                or self.output_refs[0].project_relative_path is not None
            ):
                raise _error("submit_outline receipt requires one pathless outline ref")
        elif self.action == "approve_draft":
            if (
                len(self.output_refs) != 1
                or self.output_refs[0].kind != "proposal"
                or self.output_refs[0].project_relative_path is not None
            ):
                raise _error("approve_draft receipt requires one pathless proposal ref")
        elif self.action == "approve_export":
            if (
                len(self.output_refs) != 2
                or any(ref.project_relative_path is None for ref in self.output_refs)
                or {ref.kind for ref in self.output_refs} != {"mp4", "manifest"}
            ):
                raise _error("approve_export receipt requires published mp4 and manifest refs")
        elif self.output_refs:
            raise _error("receipt output refs do not match its workflow action")
        validate_timestamp(self.created_at, field="created_at")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "action_id": self.action_id,
            "input_hash": self.input_hash,
            "run_id": self.run_id,
            "project_id": self.project_id,
            "action": self.action,
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
            "approval_ids": list(self.approval_ids),
            "mutation": None if self.mutation is None else self.mutation.to_dict(),
            "output_refs": [ref.to_dict() for ref in self.output_refs],
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: object) -> ActionReceipt:
        value = _closed(
            data,
            {
                "schema_version",
                "action_id",
                "input_hash",
                "run_id",
                "project_id",
                "action",
                "before",
                "after",
                "approval_ids",
                "mutation",
                "output_refs",
                "created_at",
            },
            description="ActionReceipt",
        )
        if value["schema_version"] != 1:
            raise _error("ActionReceipt schema_version is unsupported")
        approval_ids = value["approval_ids"]
        output_refs = value["output_refs"]
        if not isinstance(approval_ids, list) or not isinstance(output_refs, list):
            raise _error("receipt approval_ids and output_refs must be arrays")
        mutation = value["mutation"]
        return cls(
            schema_version=1,
            action_id=validate_safe_id(value["action_id"], field="action_id"),
            input_hash=validate_sha256(value["input_hash"], field="input_hash"),
            run_id=validate_safe_id(value["run_id"], field="run_id"),
            project_id=_string(value["project_id"], field="project_id"),
            action=_string(value["action"], field="action"),
            before=ReceiptState.from_dict(value["before"]),
            after=ReceiptState.from_dict(value["after"]),
            approval_ids=tuple(
                validate_safe_id(approval_id, field="approval_id")
                for approval_id in approval_ids
            ),
            mutation=None if mutation is None else MutationRef.from_dict(mutation),
            output_refs=tuple(OutputRef.from_dict(ref) for ref in output_refs),
            created_at=validate_timestamp(value["created_at"], field="created_at"),
        )


@dataclass(frozen=True)
class TransactionMarker:
    """Short-lived, fixed action commit marker; never an event-log entry."""

    schema_version: int
    action_id: str
    input_hash: str
    run_id: str
    project_id: str
    action: str
    project_before_hash: str
    project_after_hash: str
    run_before_hash: str
    run_after_hash: str
    project_before: Project
    run_before: WorkflowRun
    candidate_refs: tuple[OutputRef, ...]
    approval_ids: tuple[str, ...]
    commit_step: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise _error("TransactionMarker schema_version is unsupported")
        validate_safe_id(self.action_id, field="action_id")
        validate_sha256(self.input_hash, field="input_hash")
        validate_safe_id(self.run_id, field="run_id")
        _string(self.project_id, field="project_id")
        if self.action not in WORKFLOW_RECEIPT_ACTIONS:
            raise _error("transaction action is unsupported")
        for name, value in (
            ("project_before_hash", self.project_before_hash),
            ("project_after_hash", self.project_after_hash),
            ("run_before_hash", self.run_before_hash),
            ("run_after_hash", self.run_after_hash),
        ):
            validate_sha256(value, field=name)
        try:
            parsed_project = Project.from_dict(self.project_before.to_dict())
        except ProjectError as error:
            raise _error(
                f"TransactionMarker project_before failed Project schema parsing: {error}"
            ) from error
        if parsed_project != self.project_before:
            raise _error("TransactionMarker project_before is not an exact Project payload")
        if self.project_before.project_id != self.project_id:
            raise _error("TransactionMarker project_before belongs to another Project")
        if (
            self.run_before.project_id != self.project_id
            or self.run_before.run_id != self.run_id
        ):
            raise _error("TransactionMarker run_before identity does not match marker")
        if canonical_sha256_v1(self.project_before.to_dict()) != self.project_before_hash:
            raise _error("TransactionMarker project_before hash does not match payload")
        if canonical_sha256_v1(self.run_before.to_dict()) != self.run_before_hash:
            raise _error("TransactionMarker run_before hash does not match payload")
        approval_ids = [
            validate_safe_id(approval_id, field="approval_id")
            for approval_id in self.approval_ids
        ]
        if len(approval_ids) != len(set(approval_ids)):
            raise _error("TransactionMarker approval_ids must be unique")
        if self.commit_step not in {
            "prepared",
            "candidates_published",
            "project_published",
            "run_published",
        }:
            raise _error("transaction commit_step is unsupported")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "action_id": self.action_id,
            "input_hash": self.input_hash,
            "run_id": self.run_id,
            "project_id": self.project_id,
            "action": self.action,
            "project_before_hash": self.project_before_hash,
            "project_after_hash": self.project_after_hash,
            "run_before_hash": self.run_before_hash,
            "run_after_hash": self.run_after_hash,
            "project_before": self.project_before.to_dict(),
            "run_before": self.run_before.to_dict(),
            "candidate_refs": [ref.to_dict() for ref in self.candidate_refs],
            "approval_ids": list(self.approval_ids),
            "commit_step": self.commit_step,
        }

    @classmethod
    def from_dict(cls, data: object) -> TransactionMarker:
        value = _closed(
            data,
            {
                "schema_version",
                "action_id",
                "input_hash",
                "run_id",
                "project_id",
                "action",
                "project_before_hash",
                "project_after_hash",
                "run_before_hash",
                "run_after_hash",
                "project_before",
                "run_before",
                "candidate_refs",
                "approval_ids",
                "commit_step",
            },
            description="TransactionMarker",
        )
        if value["schema_version"] != 1:
            raise _error("TransactionMarker schema_version is unsupported")
        refs = value["candidate_refs"]
        approval_ids = value["approval_ids"]
        if not isinstance(refs, list) or not isinstance(approval_ids, list):
            raise _error("candidate_refs and approval_ids must be arrays")
        project_before = value["project_before"]
        if not isinstance(project_before, dict):
            raise _error("TransactionMarker project_before must be an object")
        try:
            parsed_project = Project.from_dict(project_before)
        except ProjectError as error:
            raise _error(
                f"TransactionMarker project_before failed Project schema parsing: {error}"
            ) from error
        if canonical_json_v1(project_before) != canonical_json_v1(
            parsed_project.to_dict()
        ):
            raise _error("TransactionMarker project_before is not an exact Project payload")
        return cls(
            schema_version=1,
            action_id=validate_safe_id(value["action_id"], field="action_id"),
            input_hash=validate_sha256(value["input_hash"], field="input_hash"),
            run_id=validate_safe_id(value["run_id"], field="run_id"),
            project_id=_string(value["project_id"], field="project_id"),
            action=_string(value["action"], field="action"),
            project_before_hash=validate_sha256(
                value["project_before_hash"], field="project_before_hash"
            ),
            project_after_hash=validate_sha256(
                value["project_after_hash"], field="project_after_hash"
            ),
            run_before_hash=validate_sha256(
                value["run_before_hash"], field="run_before_hash"
            ),
            run_after_hash=validate_sha256(
                value["run_after_hash"], field="run_after_hash"
            ),
            project_before=parsed_project,
            run_before=WorkflowRun.from_dict(value["run_before"]),
            candidate_refs=tuple(OutputRef.from_dict(ref) for ref in refs),
            approval_ids=tuple(
                validate_safe_id(approval_id, field="approval_id")
                for approval_id in approval_ids
            ),
            commit_step=_string(value["commit_step"], field="commit_step"),
        )
