"""Managed generated-media identity and record for unrecorded narration placeholders."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from roughcut.domain.project import (
    ImportMode,
    MediaProbe,
    ProjectError,
    SourceAsset,
    SourceFingerprint,
)
from roughcut.domain.time import RationalRate
from roughcut.domain.workflow import canonical_sha256_v1

NARRATION_PLACEHOLDER_MEDIA_SCHEMA_VERSION = 1
NARRATION_PLACEHOLDER_LABEL = "解说待配音"
NARRATION_PLACEHOLDER_GENERATOR = "ffmpeg_drawtext_black_card_v1"
NARRATION_PLACEHOLDER_RELATIVE_DIRECTORY = "generated/narration-placeholder"
NARRATION_PLACEHOLDER_TAIL_FRAMES = 2
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

# Explicit ordered Chinese-capable font sources.  The generator probes FFmpeg
# drawtext (libfreetype) and fails closed when neither exists; it never assumes
# a build or a platform font is present.
NARRATION_PLACEHOLDER_FONT_CANDIDATES: tuple[str, ...] = (
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "C:\\Windows\\Fonts\\msyh.ttc",
    "C:\\Windows\\Fonts\\simhei.ttf",
)


def placeholder_media_id(
    *,
    media_version: int,
    width: int,
    height: int,
    frame_rate: RationalRate,
    audio_sample_rate: int,
    video_frames: int,
) -> str:
    """Derive the immutable generated-media identity from exact generation parameters."""
    material = {
        "kind": "narration_placeholder_media",
        "schema_version": NARRATION_PLACEHOLDER_MEDIA_SCHEMA_VERSION,
        "media_version": media_version,
        "width": width,
        "height": height,
        "frame_rate": {
            "numerator": frame_rate.numerator,
            "denominator": frame_rate.denominator,
        },
        "audio_sample_rate": audio_sample_rate,
        "video_frames": video_frames,
    }
    return "phm_" + canonical_sha256_v1(material)[:24]


def placeholder_media_frames(
    required_duration_ticks: int, frame_rate: RationalRate
) -> int:
    """Frame-cover the longest placeholder segment plus a deterministic tail margin."""
    if (
        isinstance(required_duration_ticks, bool)
        or not isinstance(required_duration_ticks, int)
        or required_duration_ticks <= 0
    ):
        raise ProjectError("narration placeholder required duration is invalid")
    ticks_per_frame = frame_rate.ticks_per_frame
    total_ticks = required_duration_ticks + NARRATION_PLACEHOLDER_TAIL_FRAMES * ticks_per_frame
    return -(-total_ticks // ticks_per_frame)


@dataclass(frozen=True)
class NarrationPlaceholderMedia:
    """One immutable, project-managed generated placeholder video and its evidence."""

    media_id: str
    media_version: int
    label: str
    generator: str
    video_frames: int
    font_path: str
    content_sha256: str
    fingerprint: SourceFingerprint
    probe: MediaProbe
    created_at: str
    schema_version: int = NARRATION_PLACEHOLDER_MEDIA_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != NARRATION_PLACEHOLDER_MEDIA_SCHEMA_VERSION:
            raise ProjectError("narration placeholder media schema is unsupported")
        if not isinstance(self.media_id, str) or not self.media_id.startswith("phm_"):
            raise ProjectError("narration placeholder media ID is invalid")
        if (
            isinstance(self.media_version, bool)
            or not isinstance(self.media_version, int)
            or self.media_version <= 0
        ):
            raise ProjectError("narration placeholder media version is invalid")
        if self.label != NARRATION_PLACEHOLDER_LABEL:
            raise ProjectError("narration placeholder media label is invalid")
        if self.generator != NARRATION_PLACEHOLDER_GENERATOR:
            raise ProjectError("narration placeholder media generator is invalid")
        if (
            isinstance(self.video_frames, bool)
            or not isinstance(self.video_frames, int)
            or self.video_frames <= 0
        ):
            raise ProjectError("narration placeholder media frame count is invalid")
        if not isinstance(self.font_path, str) or not self.font_path:
            raise ProjectError("narration placeholder font source is invalid")
        if not isinstance(self.content_sha256, str) or _SHA256.fullmatch(
            self.content_sha256
        ) is None:
            raise ProjectError("narration placeholder media hash is invalid")
        if not isinstance(self.created_at, str) or not self.created_at:
            raise ProjectError("narration placeholder media timestamp is invalid")

    @property
    def relative_path(self) -> str:
        return f"{NARRATION_PLACEHOLDER_RELATIVE_DIRECTORY}/{self.media_id}.mp4"

    @property
    def record_relative_path(self) -> str:
        return f"{NARRATION_PLACEHOLDER_RELATIVE_DIRECTORY}/{self.media_id}.json"

    @property
    def frame_rate(self) -> RationalRate:
        raw = self.probe.nominal_frame_rate
        if raw is None:
            raise ProjectError("narration placeholder media frame rate is missing")
        return RationalRate(raw["numerator"], raw["denominator"])

    def covers_duration(self, required_ticks: int) -> bool:
        return self.probe.duration_ticks >= required_ticks

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "media_id": self.media_id,
            "media_version": self.media_version,
            "label": self.label,
            "generator": self.generator,
            "video_frames": self.video_frames,
            "font_path": self.font_path,
            "content_sha256": self.content_sha256,
            "fingerprint": self.fingerprint.to_dict(),
            "probe": self.probe.to_dict(),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: object) -> NarrationPlaceholderMedia:
        if not isinstance(data, dict) or set(data) != {
            "schema_version",
            "media_id",
            "media_version",
            "label",
            "generator",
            "video_frames",
            "font_path",
            "content_sha256",
            "fingerprint",
            "probe",
            "created_at",
        }:
            raise ProjectError("narration placeholder media record fields are invalid")
        fingerprint = data["fingerprint"]
        probe = data["probe"]
        if not isinstance(fingerprint, dict) or not isinstance(probe, dict):
            raise ProjectError("narration placeholder media record is incomplete")
        return cls(
            schema_version=_integer(data, "schema_version"),
            media_id=_string(data, "media_id"),
            media_version=_integer(data, "media_version"),
            label=_string(data, "label"),
            generator=_string(data, "generator"),
            video_frames=_integer(data, "video_frames"),
            font_path=_string(data, "font_path"),
            content_sha256=_string(data, "content_sha256"),
            fingerprint=SourceFingerprint.from_dict(fingerprint),
            probe=MediaProbe.from_dict(probe),
            created_at=_string(data, "created_at"),
        )


def placeholder_source_asset(media: NarrationPlaceholderMedia) -> SourceAsset:
    """Return the project-managed SourceAsset snapshot for one generated media."""
    return SourceAsset(
        source_id=media.media_id,
        kind="video",
        display_name=NARRATION_PLACEHOLDER_LABEL,
        import_mode=ImportMode.COPIED,
        locator={"project_relative_path": media.relative_path},
        fingerprint=media.fingerprint,
        probe=media.probe,
        tags=("generated", "narration_placeholder"),
        note="项目生成的未配音解说占位素材",
    )


def _string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ProjectError(f"narration placeholder media {key} is invalid")
    return value


def _integer(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProjectError(f"narration placeholder media {key} is invalid")
    return value


__all__ = [
    "NARRATION_PLACEHOLDER_FONT_CANDIDATES",
    "NARRATION_PLACEHOLDER_GENERATOR",
    "NARRATION_PLACEHOLDER_LABEL",
    "NARRATION_PLACEHOLDER_MEDIA_SCHEMA_VERSION",
    "NARRATION_PLACEHOLDER_RELATIVE_DIRECTORY",
    "NARRATION_PLACEHOLDER_TAIL_FRAMES",
    "NarrationPlaceholderMedia",
    "placeholder_media_frames",
    "placeholder_media_id",
    "placeholder_source_asset",
]
