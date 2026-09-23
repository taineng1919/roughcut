"""Closed domain objects and deterministic identity math for parallel multicam output."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, cast

from roughcut.domain.errors import WorkflowError
from roughcut.domain.time import TICKS_PER_SECOND
from roughcut.domain.workflow import canonical_sha256_v1, validate_safe_id, validate_sha256

PARALLEL_SCHEMA_VERSION = 1
PARALLEL_OPERATION_TYPE = "render_multicam_parallel"
PARALLEL_OUTPUT_PROFILE: dict[str, object] = {
    "name": "roughcut_main_h264_aac",
    "version": 1,
    "video_encoder": "libx264",
    "video_pixel_format": "yuv420p",
    "video_preset": "medium",
    "video_crf": 18,
    "audio_encoder": "aac",
    "audio_channels": 2,
    "faststart": True,
}
PARALLEL_ERROR_CODES = frozenset(
    {
        "parallel_render_prepare_revision_conflict",
        "parallel_render_decision_not_adopted",
        "parallel_render_alignment_not_deliverable",
        "parallel_render_camera_not_deliverable",
        "parallel_render_source_stale",
        "parallel_render_prepare_stale",
        "parallel_render_runtime_unavailable",
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
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9_-]+\.mp4$")


class ParallelRenderError(RuntimeError):
    """Stable parallel prepare/render domain error."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        recovery_failed: bool = False,
    ) -> None:
        if code not in PARALLEL_ERROR_CODES:
            raise ValueError(f"unknown parallel render error code: {code}")
        super().__init__(message)
        self.code = code
        self.recovery_failed = recovery_failed


def _error(code: str, evidence: str) -> ParallelRenderError:
    return ParallelRenderError(
        code,
        f"Roughcut parallel multicam render {evidence}",
    )


def _closed(value: object, fields: set[str], description: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise _error("parallel_render_prepare_stale", f"rejected non-closed {description}")
    return cast(dict[str, Any], value)


def _id(value: object, field: str) -> str:
    try:
        return validate_safe_id(value, field=field)
    except WorkflowError as error:
        raise _error("parallel_render_prepare_stale", f"rejected invalid {field}") from error


def _hash(value: object, field: str) -> str:
    try:
        return validate_sha256(value, field=field)
    except WorkflowError as error:
        raise _error("parallel_render_prepare_stale", f"rejected invalid {field}") from error


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise _error("parallel_render_prepare_stale", f"rejected invalid {field}")
    return value


def _integer(value: object, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _error("parallel_render_prepare_stale", f"rejected invalid {field}")
    return value


def _ref(value: object, *, kind: str, field: str) -> dict[str, object]:
    data = _closed(value, {"kind", "alignment_id", "schema_version", "content_hash"} if kind == "multicam_alignment" else {"kind", "edit_version_id", "schema_version", "content_hash"}, field)
    if data["kind"] != kind:
        raise _error("parallel_render_prepare_stale", f"rejected {field} kind")
    key = "alignment_id" if kind == "multicam_alignment" else "edit_version_id"
    _id(data[key], f"{field}.{key}")
    _integer(data["schema_version"], f"{field}.schema_version", 1)
    _hash(data["content_hash"], f"{field}.content_hash")
    return dict(data)


def _adoption(value: object) -> dict[str, object]:
    data = _closed(value, {"run_id", "receipt_ref", "approval_ref"}, "decision adoption")
    _id(data["run_id"], "decision_adoption.run_id")
    receipt = _closed(
        data["receipt_ref"],
        {"action_id", "receipt_schema_version", "receipt_hash"},
        "receipt ref",
    )
    approval = _closed(
        data["approval_ref"],
        {"approval_id", "record_schema_version", "record_hash"},
        "approval ref",
    )
    _id(receipt["action_id"], "receipt_ref.action_id")
    _integer(receipt["receipt_schema_version"], "receipt_ref.receipt_schema_version", 1)
    _hash(receipt["receipt_hash"], "receipt_ref.receipt_hash")
    _id(approval["approval_id"], "approval_ref.approval_id")
    _integer(approval["record_schema_version"], "approval_ref.record_schema_version", 1)
    _hash(approval["record_hash"], "approval_ref.record_hash")
    return {"run_id": data["run_id"], "receipt_ref": dict(receipt), "approval_ref": dict(approval)}


def _producer(value: object, *, result_ref: dict[str, object]) -> dict[str, object]:
    data = _closed(value, {"operation_id", "operation_type", "result_ref"}, "alignment producer")
    operation_id = _id(data["operation_id"], "alignment_producer.operation_id")
    if data["operation_type"] != "align_multicam":
        raise _error("parallel_render_alignment_not_deliverable", "rejected an alignment producer type")
    supplied = _ref(data["result_ref"], kind="multicam_alignment", field="alignment_producer.result_ref")
    if supplied != result_ref:
        raise _error("parallel_render_alignment_not_deliverable", "rejected a non-exact alignment producer ref")
    return {"operation_id": operation_id, "operation_type": "align_multicam", "result_ref": supplied}


def validate_output_settings(value: object) -> dict[str, object]:
    data = _closed(value, {"width", "height", "frame_rate", "audio_sample_rate"}, "output settings")
    width = _integer(data["width"], "output_settings.width", 1)
    height = _integer(data["height"], "output_settings.height", 1)
    rate = _closed(data["frame_rate"], {"numerator", "denominator"}, "output frame rate")
    numerator = _integer(rate["numerator"], "output_settings.frame_rate.numerator", 1)
    denominator = _integer(rate["denominator"], "output_settings.frame_rate.denominator", 1)
    sample_rate = _integer(data["audio_sample_rate"], "output_settings.audio_sample_rate", 1)
    return {
        "width": width,
        "height": height,
        "frame_rate": {"numerator": numerator, "denominator": denominator},
        "audio_sample_rate": sample_rate,
    }


def output_settings_hash(settings: dict[str, object]) -> str:
    return canonical_sha256_v1(
        {
            "kind": "multicam_parallel_output_settings",
            "schema_version": 1,
            "profile": PARALLEL_OUTPUT_PROFILE,
            "settings": validate_output_settings(settings),
        }
    )


def round_nonnegative_half_up(numerator: int, denominator: int) -> int:
    if numerator < 0 or denominator <= 0:
        raise ValueError("rounding input must be nonnegative")
    return int((Decimal(numerator) / Decimal(denominator)).to_integral_value(rounding=ROUND_HALF_UP))


def frame_boundary(output_ticks: int, numerator: int, denominator: int) -> int:
    return round_nonnegative_half_up(
        output_ticks * numerator,
        TICKS_PER_SECOND * denominator,
    )


def sample_boundary(output_ticks: int, sample_rate: int) -> int:
    return round_nonnegative_half_up(output_ticks * sample_rate, TICKS_PER_SECOND)


@dataclass(frozen=True)
class ParallelPrepareRef:
    value: dict[str, object]

    def __post_init__(self) -> None:
        _validate_prepare_ref(self.value)

    def to_dict(self) -> dict[str, object]:
        return dict(self.value)

    @property
    def prepare_id(self) -> str:
        return cast(str, self.value["prepare_id"])

    @property
    def plan_basis_hash(self) -> str:
        return cast(str, self.value["plan_basis_hash"])


def _validate_prepare_ref(value: object) -> dict[str, object]:
    data = _closed(
        value,
        {
            "schema_version", "prepare_id", "project_id", "project_revision",
            "decision_ref", "decision_adoption", "alignment_ref", "alignment_producer",
            "auxiliary_camera_ids", "output_settings_hash", "plan_basis_hash",
        },
        "prepare ref",
    )
    if data["schema_version"] != 1:
        raise _error("parallel_render_prepare_stale", "rejected an unsupported prepare schema")
    _id(data["prepare_id"], "prepare_id")
    _id(data["project_id"], "project_id")
    _integer(data["project_revision"], "project_revision", 0)
    decision = _ref(data["decision_ref"], kind="decision", field="decision_ref")
    _adoption(data["decision_adoption"])
    alignment = _ref(data["alignment_ref"], kind="multicam_alignment", field="alignment_ref")
    _producer(data["alignment_producer"], result_ref=alignment)
    cameras = data["auxiliary_camera_ids"]
    if not isinstance(cameras, list) or not cameras or any(not isinstance(item, str) for item in cameras):
        raise _error("parallel_render_prepare_stale", "rejected auxiliary camera IDs")
    for camera_id in cameras:
        _id(camera_id, "auxiliary_camera_id")
    if len(cameras) != len(set(cameras)):
        raise _error("parallel_render_prepare_stale", "rejected duplicate auxiliary camera IDs")
    _hash(data["output_settings_hash"], "output_settings_hash")
    _hash(data["plan_basis_hash"], "plan_basis_hash")
    del decision
    return cast(dict[str, object], data)


@dataclass(frozen=True)
class ParallelSlot:
    value: dict[str, object]

    def __post_init__(self) -> None:
        _validate_slot(self.value)

    def to_dict(self) -> dict[str, object]:
        return dict(self.value)


def _validate_slot(value: object) -> dict[str, object]:
    data = _closed(
        value,
        {
            "slot_id", "decision_clip_ref", "classification", "auxiliary_ref",
            "output_start_ticks", "output_end_ticks", "video_frame_start", "video_frame_end",
            "video_frame_quota", "audio_sample_start", "audio_sample_end", "audio_sample_quota",
        },
        "parallel slot",
    )
    _id(data["slot_id"], "slot_id")
    clip = _closed(
        data["decision_clip_ref"],
        {"clip_id", "clip_ordinal", "source_id", "source_start_ticks", "source_end_ticks"},
        "decision clip ref",
    )
    _id(clip["clip_id"], "decision_clip_ref.clip_id")
    _integer(clip["clip_ordinal"], "decision_clip_ref.clip_ordinal", 0)
    _id(clip["source_id"], "decision_clip_ref.source_id")
    clip_start = _integer(clip["source_start_ticks"], "decision_clip_ref.source_start_ticks", 0)
    clip_end = _integer(clip["source_end_ticks"], "decision_clip_ref.source_end_ticks", 1)
    if clip_end <= clip_start:
        raise _error("parallel_render_prepare_stale", "rejected an empty decision clip range")
    classification = data["classification"]
    if classification not in {"mapped", "missing", "uncertain", "conflict"}:
        raise _error("parallel_render_prepare_stale", "rejected slot classification")
    auxiliary = data["auxiliary_ref"]
    if classification == "mapped":
        aux = _closed(auxiliary, {"source_id", "source_start_ticks", "source_end_ticks"}, "auxiliary slot ref")
        _id(aux["source_id"], "auxiliary_ref.source_id")
        aux_start = _integer(aux["source_start_ticks"], "auxiliary_ref.source_start_ticks", 0)
        aux_end = _integer(aux["source_end_ticks"], "auxiliary_ref.source_end_ticks", 1)
        if aux_end <= aux_start or aux_end - aux_start != clip_end - clip_start:
            raise _error("parallel_render_prepare_stale", "rejected an unequal mapped slot")
    elif auxiliary is not None:
        raise _error("parallel_render_prepare_stale", "rejected an auxiliary ref on a non-mapped slot")
    output_start = _integer(data["output_start_ticks"], "output_start_ticks", 0)
    output_end = _integer(data["output_end_ticks"], "output_end_ticks", 1)
    if output_end <= output_start:
        raise _error("parallel_render_prepare_stale", "rejected an empty output slot")
    frame_start = _integer(data["video_frame_start"], "video_frame_start", 0)
    frame_end = _integer(data["video_frame_end"], "video_frame_end", 0)
    frame_quota = _integer(data["video_frame_quota"], "video_frame_quota", 0)
    sample_start = _integer(data["audio_sample_start"], "audio_sample_start", 0)
    sample_end = _integer(data["audio_sample_end"], "audio_sample_end", 0)
    sample_quota = _integer(data["audio_sample_quota"], "audio_sample_quota", 0)
    if frame_end - frame_start != frame_quota or sample_end - sample_start != sample_quota:
        raise _error("parallel_render_prepare_stale", "rejected a slot quota mismatch")
    return cast(dict[str, object], data)


def _planned_output(camera_id: str) -> dict[str, str]:
    filename = f"camera_{camera_id}.mp4"
    if _SAFE_FILENAME.fullmatch(filename) is None:
        raise _error("parallel_render_prepare_stale", "camera ID cannot form a safe filename")
    return {"filename": filename, "relative_path": filename}


def _validate_camera_plan(value: object, *, render_status: str) -> dict[str, object]:
    data = _closed(
        value,
        {
            "camera_id", "alignment_status", "coverage_status", "render_status", "mapped_ticks",
            "missing_ticks", "uncertain_ticks", "conflict_ticks", "black_silence_ticks",
            "planned_output", "slots",
        },
        "parallel camera plan",
    )
    camera_id = _id(data["camera_id"], "camera_id")
    if data["alignment_status"] not in {"complete", "partial"}:
        raise _error("parallel_render_prepare_stale", "rejected camera alignment status")
    if data["coverage_status"] not in {"complete", "partial"}:
        raise _error("parallel_render_prepare_stale", "rejected camera coverage status")
    if data["render_status"] != render_status:
        raise _error("parallel_render_prepare_stale", "rejected camera render status")
    ticks = [_integer(data[field], field, 0) for field in ("mapped_ticks", "missing_ticks", "uncertain_ticks", "conflict_ticks", "black_silence_ticks")]
    if ticks[0] <= 0 or ticks[4] != ticks[1] + ticks[2] + ticks[3]:
        raise _error("parallel_render_prepare_stale", "rejected camera tick equation")
    planned = _closed(data["planned_output"], {"filename", "relative_path"}, "planned output")
    expected_output = _planned_output(camera_id)
    if dict(planned) != expected_output:
        raise _error("parallel_render_prepare_stale", "rejected camera output identity")
    slots = data["slots"]
    if not isinstance(slots, list) or not slots:
        raise _error("parallel_render_prepare_stale", "rejected an empty camera slot plan")
    for slot in slots:
        _validate_slot(slot)
    return cast(dict[str, object], data)


@dataclass(frozen=True)
class ParallelPrepareSummary:
    value: dict[str, object]

    def __post_init__(self) -> None:
        _validate_prepare_summary(self.value)

    def to_dict(self) -> dict[str, object]:
        return dict(self.value)


def _validate_prepare_summary(value: object) -> dict[str, object]:
    data = _closed(
        value,
        {
            "schema_version", "prepare_id", "plan_basis_hash", "project_id", "project_revision",
            "decision", "alignment", "output_profile", "output_settings", "output_settings_hash",
            "total_ticks", "video_frame_quota", "audio_sample_quota", "estimated_temporary_disk_bytes",
            "manifest", "cameras",
        },
        "parallel prepare summary",
    )
    if data["schema_version"] != 1:
        raise _error("parallel_render_prepare_stale", "rejected an unsupported summary schema")
    _id(data["prepare_id"], "summary.prepare_id")
    _hash(data["plan_basis_hash"], "summary.plan_basis_hash")
    _id(data["project_id"], "summary.project_id")
    _integer(data["project_revision"], "summary.project_revision", 0)
    decision = _closed(data["decision"], {"ref", "adoption"}, "summary decision")
    _ref(decision["ref"], kind="decision", field="summary.decision.ref")
    _adoption(decision["adoption"])
    alignment = _closed(data["alignment"], {"ref", "producer"}, "summary alignment")
    alignment_ref = _ref(alignment["ref"], kind="multicam_alignment", field="summary.alignment.ref")
    _producer(alignment["producer"], result_ref=alignment_ref)
    if data["output_profile"] != PARALLEL_OUTPUT_PROFILE:
        raise _error("parallel_render_prepare_stale", "rejected output profile")
    settings = validate_output_settings(data["output_settings"])
    if output_settings_hash(settings) != data["output_settings_hash"]:
        raise _error("parallel_render_prepare_stale", "rejected output settings hash")
    _hash(data["output_settings_hash"], "summary.output_settings_hash")
    total = _integer(data["total_ticks"], "summary.total_ticks", 1)
    frames = _integer(data["video_frame_quota"], "summary.video_frame_quota", 0)
    samples = _integer(data["audio_sample_quota"], "summary.audio_sample_quota", 0)
    _integer(data["estimated_temporary_disk_bytes"], "summary.estimated_temporary_disk_bytes", 1)
    manifest = _closed(data["manifest"], {"filename", "relative_path"}, "summary manifest")
    if dict(manifest) != {"filename": "manifest.json", "relative_path": "manifest.json"}:
        raise _error("parallel_render_prepare_stale", "rejected manifest identity")
    cameras = data["cameras"]
    if not isinstance(cameras, list) or not cameras:
        raise _error("parallel_render_prepare_stale", "rejected empty camera summary")
    camera_ids: list[str] = []
    for camera in cameras:
        parsed = _validate_camera_plan(camera, render_status="planned")
        camera_ids.append(cast(str, parsed["camera_id"]))
        slots = cast(list[dict[str, object]], parsed["slots"])
        if cast(int, parsed["mapped_ticks"]) + cast(int, parsed["missing_ticks"]) + cast(int, parsed["uncertain_ticks"]) + cast(int, parsed["conflict_ticks"]) != total:
            raise _error("parallel_render_prepare_stale", "rejected camera total ticks")
        output_end = 0
        frame_sum = 0
        sample_sum = 0
        for slot in slots:
            if cast(int, slot["output_start_ticks"]) != output_end:
                raise _error("parallel_render_prepare_stale", "rejected a gapped camera slot partition")
            if cast(int, slot["video_frame_start"]) != frame_boundary(
                cast(int, slot["output_start_ticks"]),
                cast(dict[str, int], settings["frame_rate"])["numerator"],
                cast(dict[str, int], settings["frame_rate"])["denominator"],
            ) or cast(int, slot["video_frame_end"]) != frame_boundary(
                cast(int, slot["output_end_ticks"]),
                cast(dict[str, int], settings["frame_rate"])["numerator"],
                cast(dict[str, int], settings["frame_rate"])["denominator"],
            ) or cast(int, slot["audio_sample_start"]) != sample_boundary(
                cast(int, slot["output_start_ticks"]),
                cast(int, settings["audio_sample_rate"]),
            ) or cast(int, slot["audio_sample_end"]) != sample_boundary(
                cast(int, slot["output_end_ticks"]),
                cast(int, settings["audio_sample_rate"]),
            ):
                raise _error("parallel_render_prepare_stale", "rejected a non-global slot quota boundary")
            output_end = cast(int, slot["output_end_ticks"])
            frame_sum += cast(int, slot["video_frame_quota"])
            sample_sum += cast(int, slot["audio_sample_quota"])
        if output_end != total or frame_sum != frames or sample_sum != samples:
            raise _error("parallel_render_prepare_stale", "rejected camera global quota")
    if len(camera_ids) != len(set(camera_ids)):
        raise _error("parallel_render_prepare_stale", "rejected duplicate camera summary")
    del frames, samples, slots
    return cast(dict[str, object], data)


def build_prepare_ref(basis: dict[str, object]) -> dict[str, object]:
    basis_hash = canonical_sha256_v1(basis)
    return {
        "schema_version": 1,
        "prepare_id": "mpr_" + canonical_sha256_v1(
            {"kind": "multicam_parallel_prepare", "schema_version": 1, "plan_basis_hash": basis_hash}
        )[:32],
        "project_id": cast(dict[str, object], basis["project"])["project_id"],
        "project_revision": cast(dict[str, object], basis["project"])["revision"],
        "decision_ref": cast(dict[str, object], basis["decision"])["ref"],
        "decision_adoption": cast(dict[str, object], basis["decision"])["adoption"],
        "alignment_ref": cast(dict[str, object], basis["alignment"])["ref"],
        "alignment_producer": cast(dict[str, object], basis["alignment"])["producer"],
        "auxiliary_camera_ids": basis["auxiliary_camera_ids"],
        "output_settings_hash": basis["output_settings_hash"],
        "plan_basis_hash": basis_hash,
    }


def parallel_render_id(prepare_ref: dict[str, object], operation_id: str) -> str:
    return "mpr_" + canonical_sha256_v1(
        {
            "kind": "multicam_parallel_render",
            "prepare_ref": prepare_ref,
            "operation_id": _id(operation_id, "operation_id"),
        }
    )[:32]


def validate_parallel_prepare_pair(prepare_ref: dict[str, object], basis: dict[str, object]) -> None:
    parsed = _validate_prepare_ref(prepare_ref)
    expected = build_prepare_ref(basis)
    if parsed != expected:
        raise _error("parallel_render_prepare_stale", "rejected a stale or field-modified prepare ref")


def validate_manifest(value: object) -> dict[str, object]:
    data = _closed(
        value,
        {
            "schema_version", "parallel_render_id", "project_id", "created_at", "producer", "prepare_ref",
            "decision", "alignment", "delivery_status", "output_profile", "output_settings", "output_settings_hash",
            "total_ticks", "video_frame_quota", "audio_sample_quota", "manifest_file", "cameras",
        },
        "parallel render manifest",
    )
    if data["schema_version"] != 1:
        raise _error("parallel_render_publish_failed", "rejected manifest schema")
    render_id = _id(data["parallel_render_id"], "parallel_render_id")
    _id(data["project_id"], "manifest.project_id")
    _string(data["created_at"], "manifest.created_at")
    producer = _closed(data["producer"], {"operation_id", "operation_type"}, "manifest producer")
    _id(producer["operation_id"], "manifest.producer.operation_id")
    if producer["operation_type"] != PARALLEL_OPERATION_TYPE:
        raise _error("parallel_render_publish_failed", "rejected manifest producer type")
    prepare = cast(dict[str, object], data["prepare_ref"])
    _validate_prepare_ref(prepare)
    if data["project_id"] != prepare["project_id"]:
        raise _error("parallel_render_publish_failed", "rejected a manifest project mismatch")
    producer = cast(dict[str, object], producer)
    if parallel_render_id(prepare, cast(str, producer["operation_id"])) != render_id:
        raise _error("parallel_render_publish_failed", "rejected a manifest render identity mismatch")
    decision = _closed(data["decision"], {"ref", "adoption"}, "manifest decision")
    _ref(decision["ref"], kind="decision", field="manifest.decision.ref")
    _adoption(decision["adoption"])
    if decision != {
        "ref": prepare["decision_ref"],
        "adoption": prepare["decision_adoption"],
    }:
        raise _error("parallel_render_publish_failed", "rejected a manifest Decision mismatch")
    alignment = _closed(data["alignment"], {"ref", "producer"}, "manifest alignment")
    alignment_ref = _ref(alignment["ref"], kind="multicam_alignment", field="manifest.alignment.ref")
    _producer(alignment["producer"], result_ref=alignment_ref)
    if alignment != {
        "ref": prepare["alignment_ref"],
        "producer": prepare["alignment_producer"],
    }:
        raise _error("parallel_render_publish_failed", "rejected a manifest alignment mismatch")
    if data["delivery_status"] not in {"complete", "partial"}:
        raise _error("parallel_render_publish_failed", "rejected delivery status")
    if data["output_profile"] != PARALLEL_OUTPUT_PROFILE:
        raise _error("parallel_render_publish_failed", "rejected manifest output profile")
    settings = validate_output_settings(data["output_settings"])
    if output_settings_hash(settings) != data["output_settings_hash"]:
        raise _error("parallel_render_publish_failed", "rejected manifest output settings hash")
    _integer(data["total_ticks"], "manifest.total_ticks", 1)
    _integer(data["video_frame_quota"], "manifest.video_frame_quota", 0)
    _integer(data["audio_sample_quota"], "manifest.audio_sample_quota", 0)
    manifest_file = _closed(data["manifest_file"], {"filename", "relative_path", "project_relative_path"}, "manifest file")
    if manifest_file["filename"] != "manifest.json" or manifest_file["relative_path"] != "manifest.json" or manifest_file["project_relative_path"] != f"renders/multicam/{render_id}/manifest.json":
        raise _error("parallel_render_publish_failed", "rejected manifest file identity")
    cameras = data["cameras"]
    if not isinstance(cameras, list) or not cameras:
        raise _error("parallel_render_publish_failed", "rejected empty manifest cameras")
    succeeded = 0
    failed = 0
    camera_ids: set[str] = set()
    expected_camera_ids = set(cast(list[str], prepare["auxiliary_camera_ids"]))
    total_ticks = _integer(data["total_ticks"], "manifest.total_ticks", 1)
    frame_quota = _integer(data["video_frame_quota"], "manifest.video_frame_quota", 0)
    sample_quota = _integer(data["audio_sample_quota"], "manifest.audio_sample_quota", 0)
    for raw in cameras:
        camera = _closed(raw, {"camera_id", "alignment_status", "coverage_status", "render_status", "mapped_ticks", "missing_ticks", "uncertain_ticks", "conflict_ticks", "black_silence_ticks", "output", "error", "slots"}, "manifest camera")
        camera_id = _id(camera["camera_id"], "manifest.camera_id")
        if camera_id in camera_ids:
            raise _error("parallel_render_publish_failed", "rejected duplicate manifest camera")
        camera_ids.add(camera_id)
        if camera_id not in expected_camera_ids:
            raise _error("parallel_render_publish_failed", "rejected an unplanned manifest camera")
        plan_fields = {
            "camera_id", "alignment_status", "coverage_status", "render_status",
            "mapped_ticks", "missing_ticks", "uncertain_ticks", "conflict_ticks",
            "black_silence_ticks", "slots",
        }
        plan_camera = {
            **{field: camera[field] for field in plan_fields},
            "planned_output": _planned_output(camera_id),
        }
        _validate_camera_plan(plan_camera, render_status=cast(str, camera["render_status"]))
        camera_ticks = sum(
            cast(int, slot["output_end_ticks"]) - cast(int, slot["output_start_ticks"])
            for slot in cast(list[dict[str, object]], camera["slots"])
        )
        camera_frames = sum(cast(int, slot["video_frame_quota"]) for slot in cast(list[dict[str, object]], camera["slots"]))
        camera_samples = sum(cast(int, slot["audio_sample_quota"]) for slot in cast(list[dict[str, object]], camera["slots"]))
        if (camera_ticks, camera_frames, camera_samples) != (total_ticks, frame_quota, sample_quota):
            raise _error("parallel_render_publish_failed", "rejected manifest global quota linkage")
        output = camera["output"]
        error = camera["error"]
        if camera["render_status"] == "succeeded":
            if not isinstance(output, dict) or error is not None:
                raise _error("parallel_render_publish_failed", "rejected successful camera linkage")
            out = _closed(output, {"filename", "relative_path", "project_relative_path", "bytes", "content_hash"}, "camera output")
            if out["filename"] != f"camera_{camera_id}.mp4" or out["relative_path"] != out["filename"] or out["project_relative_path"] != f"renders/multicam/{render_id}/{out['filename']}":
                raise _error("parallel_render_publish_failed", "rejected camera output path")
            _integer(out["bytes"], "camera output bytes", 1)
            _hash(out["content_hash"], "camera output content_hash")
            succeeded += 1
        elif camera["render_status"] == "failed":
            if output is not None or not isinstance(error, dict):
                raise _error("parallel_render_publish_failed", "rejected failed camera linkage")
            error_data = _closed(error, {"code"}, "camera error")
            if error_data["code"] not in {"parallel_camera_encode_failed", "parallel_camera_verify_failed"}:
                raise _error("parallel_render_publish_failed", "rejected camera error code")
            failed += 1
        else:
            raise _error("parallel_render_publish_failed", "rejected planned camera in manifest")
    if camera_ids != expected_camera_ids:
        raise _error("parallel_render_publish_failed", "rejected an incomplete manifest camera set")
    if succeeded == 0 or (failed and data["delivery_status"] != "partial") or (not failed and data["delivery_status"] != "complete"):
        raise _error("parallel_render_publish_failed", "rejected delivery status linkage")
    return cast(dict[str, object], data)
