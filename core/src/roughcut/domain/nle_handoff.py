"""Immutable export-time semantics for editable NLE handoff."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias

from roughcut.domain.project import SourceAsset, SourceFingerprint
from roughcut.domain.time import TICKS_PER_SECOND, RationalRate
from roughcut.domain.workflow import canonical_sha256_v1

NLE_EXPORT_SCHEMA_VERSION = 1
FCPXML_EXPORT_PROFILE = "roughcut_fcpxml_1_14"
FCP7_XML_EXPORT_PROFILE = "roughcut_fcp7_xml_xmeml_v5"
NLE_EXPORT_PROFILES = {
    "fcpxml": FCPXML_EXPORT_PROFILE,
    "fcp7_xml": FCP7_XML_EXPORT_PROFILE,
}
NLE_ROUTES = frozenset(NLE_EXPORT_PROFILES)
MEDIA_TYPES = frozenset({"video", "audio"})
_TRACK_ROLES = frozenset({"main", "auxiliary"})
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class NleHandoffError(ValueError):
    """A closed NLE handoff or export boundary error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _error(code: str, message: str) -> NleHandoffError:
    return NleHandoffError(code, f"Roughcut NLE handoff {message}")


def _validate_repeated_source_metadata(tracks: tuple[NleHandoffTrack, ...]) -> None:
    catalog: dict[str, tuple[object, ...]] = {}
    for track in tracks:
        for clip in track.clips:
            metadata = (
                clip.source_locator,
                clip.source_duration_ticks,
                clip.source_display_name,
                clip.source_width,
                clip.source_height,
                clip.source_nominal_frame_rate,
                clip.source_is_vfr,
                clip.source_audio_sample_rate,
                clip.media_types,
            )
            previous = catalog.get(clip.source_id)
            if previous is None:
                catalog[clip.source_id] = metadata
            elif previous != metadata:
                raise _error(
                    "nle_export_integrity_error",
                    f"rejected inconsistent metadata for Source {clip.source_id!r}",
                )


def _safe_id(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise _error("nle_export_integrity_error", f"rejected invalid {field}")
    return value


def _sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise _error("nle_export_integrity_error", f"rejected invalid {field}")
    return value


def _integer(value: object, *, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _error(
            "nle_export_integrity_error",
            f"rejected {field}; expected integer >= {minimum}",
        )
    return value


def _string(value: object, *, field: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise _error("nle_export_integrity_error", f"rejected invalid {field}")
    if "\x00" in value:
        raise _error("nle_export_integrity_error", f"rejected NUL in {field}")
    return value


def _media_types(value: tuple[str, ...], *, field: str) -> tuple[str, ...]:
    if not value or any(item not in MEDIA_TYPES for item in value):
        raise _error("nle_export_integrity_error", f"rejected invalid {field}")
    if len(value) != len(set(value)):
        raise _error("nle_export_integrity_error", f"rejected duplicate {field}")
    normalized = tuple(item for item in ("video", "audio") if item in value)
    if normalized != value:
        raise _error("nle_export_integrity_error", f"rejected unordered {field}")
    return value


def _range(
    start: int,
    end: int,
    *,
    start_field: str,
    end_field: str,
) -> None:
    _integer(start, field=start_field)
    _integer(end, field=end_field, minimum=1)
    if end <= start:
        raise _error(
            "nle_export_integrity_error",
            f"rejected non-positive range {start_field}/{end_field}",
        )


@dataclass(frozen=True)
class NleHandoffClip:
    """One original-media clip in a logical handoff track."""

    logical_clip_id: str
    source_id: str
    source_locator: Path
    source_duration_ticks: int
    source_display_name: str
    source_width: int | None
    source_height: int | None
    source_nominal_frame_rate: RationalRate | None
    source_is_vfr: bool
    source_audio_sample_rate: int | None
    source_in_ticks: int
    source_out_ticks: int
    timeline_in_ticks: int
    timeline_out_ticks: int
    camera_id: str
    track_id: str
    media_types: tuple[str, ...]
    av_link_id: str

    def __post_init__(self) -> None:
        _safe_id(self.logical_clip_id, field="clip logical identity")
        _safe_id(self.source_id, field="clip source identity")
        _safe_id(self.camera_id, field="clip camera identity")
        _safe_id(self.track_id, field="clip track identity")
        _safe_id(self.av_link_id, field="clip A/V link identity")
        if not isinstance(self.source_locator, Path) or not self.source_locator.is_absolute():
            raise _error("nle_export_integrity_error", "rejected a non-absolute original locator")
        _integer(
            self.source_duration_ticks,
            field="clip source_duration_ticks",
            minimum=1,
        )
        _string(self.source_display_name, field="clip source_display_name")
        for name, value in (
            ("clip source_width", self.source_width),
            ("clip source_height", self.source_height),
        ):
            if value is not None:
                _integer(value, field=name, minimum=1)
        if self.source_nominal_frame_rate is not None:
            if not isinstance(self.source_nominal_frame_rate, RationalRate):
                raise _error(
                    "nle_export_integrity_error",
                    "rejected an invalid clip source_nominal_frame_rate",
                )
            try:
                self.source_nominal_frame_rate.ticks_per_frame
            except ValueError as error:
                raise _error(
                    "nle_export_integrity_error",
                    "rejected a source frame rate that is not representable in project ticks",
                ) from error
        if not isinstance(self.source_is_vfr, bool):
            raise _error("nle_export_integrity_error", "rejected an invalid clip source_is_vfr")
        if self.source_audio_sample_rate is not None:
            _integer(
                self.source_audio_sample_rate,
                field="clip source_audio_sample_rate",
                minimum=1,
            )
        if "video" in self.media_types:
            if self.source_width is None or self.source_height is None:
                raise _error(
                    "nle_export_integrity_error",
                    "rejected a video clip without source dimensions",
                )
            if self.source_nominal_frame_rate is None:
                raise _error(
                    "nle_export_integrity_error",
                    "rejected a video clip without source nominal frame rate",
                )
        _range(
            self.source_in_ticks,
            self.source_out_ticks,
            start_field="clip source_in_ticks",
            end_field="clip source_out_ticks",
        )
        _range(
            self.timeline_in_ticks,
            self.timeline_out_ticks,
            start_field="clip timeline_in_ticks",
            end_field="clip timeline_out_ticks",
        )
        if self.source_out_ticks > self.source_duration_ticks:
            raise _error(
                "nle_export_integrity_error",
                "rejected a clip outside the original source duration",
            )
        if self.source_out_ticks - self.source_in_ticks != self.timeline_duration_ticks:
            raise _error(
                "nle_export_integrity_error",
                "rejected a clip whose source and timeline durations differ",
            )
        _media_types(self.media_types, field="clip media_types")

    @property
    def timeline_duration_ticks(self) -> int:
        return self.timeline_out_ticks - self.timeline_in_ticks

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": "clip",
            "logical_clip_id": self.logical_clip_id,
            "source_id": self.source_id,
            "source_locator": str(self.source_locator),
            "source_duration_ticks": self.source_duration_ticks,
            "source_display_name": self.source_display_name,
            "source_width": self.source_width,
            "source_height": self.source_height,
            "source_nominal_frame_rate": (
                None
                if self.source_nominal_frame_rate is None
                else {
                    "numerator": self.source_nominal_frame_rate.numerator,
                    "denominator": self.source_nominal_frame_rate.denominator,
                }
            ),
            "source_is_vfr": self.source_is_vfr,
            "source_audio_sample_rate": self.source_audio_sample_rate,
            "source_in_ticks": self.source_in_ticks,
            "source_out_ticks": self.source_out_ticks,
            "timeline_in_ticks": self.timeline_in_ticks,
            "timeline_out_ticks": self.timeline_out_ticks,
            "camera_id": self.camera_id,
            "track_id": self.track_id,
            "media_types": list(self.media_types),
            "av_link_id": self.av_link_id,
        }


@dataclass(frozen=True)
class NleHandoffGap:
    """An explicit absence on a logical track; it has no media locator."""

    gap_id: str
    timeline_in_ticks: int
    timeline_out_ticks: int
    camera_id: str
    track_id: str

    def __post_init__(self) -> None:
        _safe_id(self.gap_id, field="gap identity")
        _safe_id(self.camera_id, field="gap camera identity")
        _safe_id(self.track_id, field="gap track identity")
        _range(
            self.timeline_in_ticks,
            self.timeline_out_ticks,
            start_field="gap timeline_in_ticks",
            end_field="gap timeline_out_ticks",
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": "gap",
            "gap_id": self.gap_id,
            "timeline_in_ticks": self.timeline_in_ticks,
            "timeline_out_ticks": self.timeline_out_ticks,
            "camera_id": self.camera_id,
            "track_id": self.track_id,
        }


TimelineItem: TypeAlias = NleHandoffClip | NleHandoffGap


@dataclass(frozen=True)
class NleHandoffTrack:
    """One stable logical camera track with ordered clips and explicit gaps."""

    track_id: str
    camera_id: str
    role: Literal["main", "auxiliary"]
    media_types: tuple[str, ...]
    items: tuple[TimelineItem, ...]

    def __post_init__(self) -> None:
        _safe_id(self.track_id, field="track identity")
        _safe_id(self.camera_id, field="track camera identity")
        if self.role not in _TRACK_ROLES:
            raise _error("nle_export_integrity_error", "rejected an unknown track role")
        _media_types(self.media_types, field="track media_types")
        if not self.items:
            raise _error("nle_export_integrity_error", "rejected an empty handoff track")
        previous_end = 0
        clip_types: set[str] = set()
        for item in self.items:
            if isinstance(item, NleHandoffClip):
                start = item.timeline_in_ticks
                end = item.timeline_out_ticks
                if item.track_id != self.track_id or item.camera_id != self.camera_id:
                    raise _error(
                        "nle_export_integrity_error",
                        "rejected a clip on the wrong logical track",
                    )
                clip_types.update(item.media_types)
            elif isinstance(item, NleHandoffGap):
                start = item.timeline_in_ticks
                end = item.timeline_out_ticks
                if item.track_id != self.track_id or item.camera_id != self.camera_id:
                    raise _error(
                        "nle_export_integrity_error",
                        "rejected a gap on the wrong logical track",
                    )
            else:
                raise _error("nle_export_integrity_error", "rejected an unknown timeline item")
            if start != previous_end:
                raise _error(
                    "nle_export_integrity_error",
                    "rejected a track with a gap or overlap in its item partition",
                )
            previous_end = end
        if not clip_types <= set(self.media_types):
            raise _error(
                "nle_export_integrity_error",
                "rejected track capabilities that omit a clip capability",
            )

    @property
    def timeline_duration_ticks(self) -> int:
        last = self.items[-1]
        return (
            last.timeline_out_ticks if isinstance(last, NleHandoffClip) else last.timeline_out_ticks
        )

    @property
    def clips(self) -> tuple[NleHandoffClip, ...]:
        return tuple(item for item in self.items if isinstance(item, NleHandoffClip))

    @property
    def gaps(self) -> tuple[NleHandoffGap, ...]:
        return tuple(item for item in self.items if isinstance(item, NleHandoffGap))

    def to_dict(self) -> dict[str, object]:
        return {
            "track_id": self.track_id,
            "camera_id": self.camera_id,
            "role": self.role,
            "media_types": list(self.media_types),
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(frozen=True)
class NleHandoffTimeline:
    """The single vendor-neutral, export-time handoff representation."""

    project_id: str
    timebase: int
    frame_rate: RationalRate
    video_width: int
    video_height: int
    audio_sample_rate: int
    duration_ticks: int
    tracks: tuple[NleHandoffTrack, ...]

    def __post_init__(self) -> None:
        _safe_id(self.project_id, field="timeline project identity")
        if self.timebase != TICKS_PER_SECOND:
            raise _error(
                "nle_export_integrity_error",
                "rejected a timeline with a non-canonical timebase",
            )
        try:
            self.frame_rate.ticks_per_frame
        except ValueError as error:
            raise _error(
                "nle_export_integrity_error",
                "rejected a frame rate that is not representable in project ticks",
            ) from error
        for name, value in (
            ("timeline video_width", self.video_width),
            ("timeline video_height", self.video_height),
        ):
            _integer(value, field=name, minimum=2)
            if value % 2:
                raise _error("nle_export_integrity_error", f"rejected odd {name}")
        _integer(self.audio_sample_rate, field="timeline audio_sample_rate", minimum=1)
        _integer(self.duration_ticks, field="timeline duration_ticks", minimum=1)
        if not self.tracks:
            raise _error("nle_export_integrity_error", "rejected a timeline without tracks")
        track_ids = [track.track_id for track in self.tracks]
        camera_ids = [track.camera_id for track in self.tracks]
        if len(track_ids) != len(set(track_ids)) or len(camera_ids) != len(set(camera_ids)):
            raise _error("nle_export_integrity_error", "rejected duplicate logical tracks")
        item_ids: set[str] = set()
        for track in self.tracks:
            for item in track.items:
                item_id = item.logical_clip_id if isinstance(item, NleHandoffClip) else item.gap_id
                if item_id in item_ids:
                    raise _error(
                        "nle_export_integrity_error", "rejected duplicate timeline item identities"
                    )
                item_ids.add(item_id)
        main_tracks = [track for track in self.tracks if track.role == "main"]
        if len(main_tracks) != 1 or self.tracks[0] is not main_tracks[0]:
            raise _error("nle_export_integrity_error", "rejected an invalid main track order")
        for track in self.tracks:
            if track.timeline_duration_ticks != self.duration_ticks:
                raise _error(
                    "nle_export_integrity_error",
                    "rejected a track that does not cover the full timeline",
                )
        _validate_repeated_source_metadata(self.tracks)
        if main_tracks[0].gaps:
            raise _error("nle_export_integrity_error", "rejected a main track with gaps")

    @property
    def main_track(self) -> NleHandoffTrack:
        return self.tracks[0]

    @property
    def auxiliary_tracks(self) -> tuple[NleHandoffTrack, ...]:
        return tuple(track for track in self.tracks[1:] if track.role == "auxiliary")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": NLE_EXPORT_SCHEMA_VERSION,
            "project_id": self.project_id,
            "timebase": self.timebase,
            "frame_rate": {
                "numerator": self.frame_rate.numerator,
                "denominator": self.frame_rate.denominator,
            },
            "video_width": self.video_width,
            "video_height": self.video_height,
            "audio_sample_rate": self.audio_sample_rate,
            "duration_ticks": self.duration_ticks,
            "tracks": [track.to_dict() for track in self.tracks],
        }


@dataclass(frozen=True)
class NleSourceSnapshot:
    """The non-path source identity bound into an NLE export receipt."""

    source_id: str
    snapshot_hash: str
    locator_hash: str
    fingerprint: SourceFingerprint
    duration_ticks: int

    def __post_init__(self) -> None:
        _safe_id(self.source_id, field="source snapshot source_id")
        _sha256(self.snapshot_hash, field="source snapshot hash")
        _sha256(self.locator_hash, field="source locator hash")
        _integer(self.duration_ticks, field="source snapshot duration_ticks", minimum=1)

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "snapshot_hash": self.snapshot_hash,
            "locator_hash": self.locator_hash,
            "fingerprint": self.fingerprint.to_dict(),
            "duration_ticks": self.duration_ticks,
        }

    @classmethod
    def from_dict(cls, data: object) -> NleSourceSnapshot:
        if not isinstance(data, dict) or set(data) != {
            "source_id",
            "snapshot_hash",
            "locator_hash",
            "fingerprint",
            "duration_ticks",
        }:
            raise _error("nle_export_integrity_error", "rejected a closed source snapshot")
        fingerprint = data["fingerprint"]
        if not isinstance(fingerprint, dict):
            raise _error("nle_export_integrity_error", "rejected a source snapshot fingerprint")
        try:
            parsed_fingerprint = SourceFingerprint.from_dict(fingerprint)
        except (TypeError, ValueError) as error:
            raise _error(
                "nle_export_integrity_error", "rejected a source snapshot fingerprint"
            ) from error
        return cls(
            source_id=_safe_id(data["source_id"], field="source snapshot source_id"),
            snapshot_hash=_sha256(data["snapshot_hash"], field="source snapshot hash"),
            locator_hash=_sha256(data["locator_hash"], field="source locator hash"),
            fingerprint=parsed_fingerprint,
            duration_ticks=_integer(
                data["duration_ticks"],
                field="source snapshot duration_ticks",
                minimum=1,
            ),
        )


@dataclass(frozen=True)
class NleExportReceipt:
    """Immutable proof of one published NLE export."""

    schema_version: int
    action_id: str
    request_hash: str
    project_id: str
    run_id: str
    project_revision: int
    edit_version_id: str
    decision_schema_version: int
    decision_content_hash: str
    route: str
    exporter_profile: str
    destination: str
    source_snapshots: tuple[NleSourceSnapshot, ...]
    alignment_artifact_id: str | None
    alignment_content_hash: str | None
    output_sha256: str
    output_bytes: int
    created_at: str

    def __post_init__(self) -> None:
        if self.schema_version != NLE_EXPORT_SCHEMA_VERSION:
            raise _error("nle_export_integrity_error", "rejected an unsupported receipt schema")
        _safe_id(self.action_id, field="receipt action_id")
        _sha256(self.request_hash, field="receipt request_hash")
        _safe_id(self.project_id, field="receipt project_id")
        _safe_id(self.run_id, field="receipt run_id")
        _integer(self.project_revision, field="receipt project_revision", minimum=0)
        _safe_id(self.edit_version_id, field="receipt edit_version_id")
        _integer(
            self.decision_schema_version,
            field="receipt decision_schema_version",
            minimum=1,
        )
        _sha256(self.decision_content_hash, field="receipt decision_content_hash")
        if self.route not in NLE_ROUTES:
            raise _error("nle_export_integrity_error", "rejected an unsupported export route")
        expected_profile = NLE_EXPORT_PROFILES[self.route]
        if self.exporter_profile != expected_profile:
            raise _error("nle_export_integrity_error", "rejected a route/profile mismatch")
        if not Path(self.destination).is_absolute():
            raise _error("nle_export_integrity_error", "rejected a non-absolute destination")
        if not self.source_snapshots:
            raise _error(
                "nle_export_integrity_error", "rejected a receipt without source snapshots"
            )
        source_ids = [snapshot.source_id for snapshot in self.source_snapshots]
        if len(source_ids) != len(set(source_ids)):
            raise _error("nle_export_integrity_error", "rejected duplicate source snapshots")
        if (self.alignment_artifact_id is None) != (self.alignment_content_hash is None):
            raise _error("nle_export_integrity_error", "rejected an incomplete alignment identity")
        if self.alignment_artifact_id is not None:
            _safe_id(self.alignment_artifact_id, field="receipt alignment_artifact_id")
            _sha256(self.alignment_content_hash, field="receipt alignment_content_hash")
        _sha256(self.output_sha256, field="receipt output_sha256")
        _integer(self.output_bytes, field="receipt output_bytes", minimum=1)
        _string(self.created_at, field="receipt created_at")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "action_id": self.action_id,
            "request_hash": self.request_hash,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "project_revision": self.project_revision,
            "edit_version_id": self.edit_version_id,
            "decision_schema_version": self.decision_schema_version,
            "decision_content_hash": self.decision_content_hash,
            "route": self.route,
            "exporter_profile": self.exporter_profile,
            "destination": self.destination,
            "source_snapshots": [snapshot.to_dict() for snapshot in self.source_snapshots],
            "alignment_artifact_id": self.alignment_artifact_id,
            "alignment_content_hash": self.alignment_content_hash,
            "output_sha256": self.output_sha256,
            "output_bytes": self.output_bytes,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: object) -> NleExportReceipt:
        if not isinstance(data, dict):
            raise _error("nle_export_integrity_error", "rejected a non-object receipt")
        fields = {
            "schema_version",
            "action_id",
            "request_hash",
            "project_id",
            "run_id",
            "project_revision",
            "edit_version_id",
            "decision_schema_version",
            "decision_content_hash",
            "route",
            "exporter_profile",
            "destination",
            "source_snapshots",
            "alignment_artifact_id",
            "alignment_content_hash",
            "output_sha256",
            "output_bytes",
            "created_at",
        }
        if set(data) != fields:
            raise _error("nle_export_integrity_error", "rejected receipt fields")
        snapshots = data["source_snapshots"]
        if not isinstance(snapshots, list):
            raise _error("nle_export_integrity_error", "rejected receipt source snapshots")
        return cls(
            schema_version=_integer(data["schema_version"], field="receipt schema_version"),
            action_id=_safe_id(data["action_id"], field="receipt action_id"),
            request_hash=_sha256(data["request_hash"], field="receipt request_hash"),
            project_id=_safe_id(data["project_id"], field="receipt project_id"),
            run_id=_safe_id(data["run_id"], field="receipt run_id"),
            project_revision=_integer(data["project_revision"], field="receipt project_revision"),
            edit_version_id=_safe_id(data["edit_version_id"], field="receipt edit_version_id"),
            decision_schema_version=_integer(
                data["decision_schema_version"],
                field="receipt decision_schema_version",
                minimum=1,
            ),
            decision_content_hash=_sha256(
                data["decision_content_hash"],
                field="receipt decision_content_hash",
            ),
            route=_string(data["route"], field="receipt route"),
            exporter_profile=_string(data["exporter_profile"], field="receipt exporter_profile"),
            destination=_string(data["destination"], field="receipt destination"),
            source_snapshots=tuple(NleSourceSnapshot.from_dict(item) for item in snapshots),
            alignment_artifact_id=(
                None
                if data["alignment_artifact_id"] is None
                else _safe_id(data["alignment_artifact_id"], field="receipt alignment_artifact_id")
            ),
            alignment_content_hash=(
                None
                if data["alignment_content_hash"] is None
                else _sha256(data["alignment_content_hash"], field="receipt alignment_content_hash")
            ),
            output_sha256=_sha256(data["output_sha256"], field="receipt output_sha256"),
            output_bytes=_integer(data["output_bytes"], field="receipt output_bytes", minimum=1),
            created_at=_string(data["created_at"], field="receipt created_at"),
        )


def nle_export_request_hash(request: dict[str, object]) -> str:
    """Hash the exact immutable request identity used by the approval boundary."""

    return canonical_sha256_v1(
        {
            "hash_schema": 1,
            "hash_kind": "nle_export_request",
            "request": request,
        }
    )


def source_snapshot_hash(source: SourceAsset) -> str:
    """Hash a SourceAsset snapshot without making it a new persisted artifact."""

    return canonical_sha256_v1(source.to_dict())


__all__ = [
    "FCP7_XML_EXPORT_PROFILE",
    "FCPXML_EXPORT_PROFILE",
    "MEDIA_TYPES",
    "NLE_EXPORT_PROFILES",
    "NLE_EXPORT_SCHEMA_VERSION",
    "NLE_ROUTES",
    "NleExportReceipt",
    "NleHandoffClip",
    "NleHandoffError",
    "NleHandoffGap",
    "NleHandoffTimeline",
    "NleHandoffTrack",
    "NleSourceSnapshot",
    "TimelineItem",
    "nle_export_request_hash",
    "source_snapshot_hash",
]
