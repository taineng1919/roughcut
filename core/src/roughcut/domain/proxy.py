"""Immutable proxy profile, cache identity, and ready-manifest models."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from roughcut.domain.project import MediaProbe, ProjectError, SourceFingerprint
from roughcut.domain.time import RationalRate

PROXY_SCHEMA_VERSION = 1
PROXY_PROFILE_VERSION = 1
MAX_PROXY_WIDTH = 1280
MAX_PROXY_HEIGHT = 720
PROXY_AUDIO_SAMPLE_RATE = 48_000
PROXY_TIMELINE_POLICY = "shared-earliest-stream-origin-v1"
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ProxyProfile:
    canvas_width: int
    canvas_height: int
    frame_rate: RationalRate
    gop_frames: int
    has_video: bool
    has_audio: bool
    schema_version: int = PROXY_SCHEMA_VERSION
    profile_version: int = PROXY_PROFILE_VERSION
    container: str = "mp4"
    video_codec: str = "libx264"
    pixel_format: str = "yuv420p"
    crf: int = 23
    preset: str = "veryfast"
    faststart: bool = True
    audio_codec: str | None = "aac"
    audio_sample_rate: int | None = PROXY_AUDIO_SAMPLE_RATE

    def __post_init__(self) -> None:
        if self.schema_version != PROXY_SCHEMA_VERSION:
            raise ProjectError("unsupported proxy schema version")
        if self.profile_version != PROXY_PROFILE_VERSION:
            raise ProjectError("unsupported proxy profile version")
        for name, value, maximum in (
            ("width", self.canvas_width, MAX_PROXY_WIDTH),
            ("height", self.canvas_height, MAX_PROXY_HEIGHT),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
                or value % 2
                or value > maximum
            ):
                raise ProjectError(f"proxy canvas {name} is invalid")
        try:
            _validated_ticks_per_frame = self.frame_rate.ticks_per_frame
        except ValueError as error:
            raise ProjectError("proxy frame rate is not representable in project ticks") from error
        if self.gop_frames <= 0:
            raise ProjectError("proxy GOP is invalid")
        if not self.has_video and not self.has_audio:
            raise ProjectError("proxy source has no usable stream")
        if self.has_audio:
            if self.audio_codec != "aac" or self.audio_sample_rate != PROXY_AUDIO_SAMPLE_RATE:
                raise ProjectError("proxy audio profile is invalid")
        elif self.audio_codec is not None or self.audio_sample_rate is not None:
            raise ProjectError("silent proxy profile must not declare audio output")
        if (
            self.container != "mp4"
            or self.video_codec != "libx264"
            or self.pixel_format != "yuv420p"
            or self.crf != 23
            or self.preset != "veryfast"
            or not self.faststart
        ):
            raise ProjectError("proxy encoding profile is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "profile_version": self.profile_version,
            "canvas": {"width": self.canvas_width, "height": self.canvas_height},
            "frame_rate": {
                "numerator": self.frame_rate.numerator,
                "denominator": self.frame_rate.denominator,
            },
            "gop_frames": self.gop_frames,
            "has_video": self.has_video,
            "has_audio": self.has_audio,
            "container": self.container,
            "video_codec": self.video_codec,
            "pixel_format": self.pixel_format,
            "crf": self.crf,
            "preset": self.preset,
            "faststart": self.faststart,
            "audio_codec": self.audio_codec,
            "audio_sample_rate": self.audio_sample_rate,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProxyProfile:
        canvas = _object(data, "canvas")
        rate = _object(data, "frame_rate")
        return cls(
            schema_version=_integer(data, "schema_version"),
            profile_version=_integer(data, "profile_version"),
            canvas_width=_integer(canvas, "width"),
            canvas_height=_integer(canvas, "height"),
            frame_rate=RationalRate(_integer(rate, "numerator"), _integer(rate, "denominator")),
            gop_frames=_integer(data, "gop_frames"),
            has_video=_boolean(data, "has_video"),
            has_audio=_boolean(data, "has_audio"),
            container=_string(data, "container"),
            video_codec=_string(data, "video_codec"),
            pixel_format=_string(data, "pixel_format"),
            crf=_integer(data, "crf"),
            preset=_string(data, "preset"),
            faststart=_boolean(data, "faststart"),
            audio_codec=_optional_string(data.get("audio_codec")),
            audio_sample_rate=_optional_integer(data.get("audio_sample_rate")),
        )


@dataclass(frozen=True)
class ProxyOutput:
    relative_path: str
    size: int
    sha256_head_tail: str
    duration_ticks: int
    probe: dict[str, object] | None = None

    def __post_init__(self) -> None:
        _validate_relative_path(self.relative_path)
        if self.size <= 0 or self.duration_ticks <= 0:
            raise ProjectError("proxy output size and duration must be positive")
        if _SHA256.fullmatch(self.sha256_head_tail) is None:
            raise ProjectError("proxy output fingerprint is invalid")
        if self.probe is not None and not isinstance(self.probe, dict):
            raise ProjectError("proxy output probe is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "size": self.size,
            "sha256_head_tail": self.sha256_head_tail,
            "duration_ticks": self.duration_ticks,
            "probe": self.probe,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProxyOutput:
        probe = data.get("probe")
        if probe is not None and not isinstance(probe, dict):
            raise ProjectError("proxy output probe is invalid")
        return cls(
            relative_path=_string(data, "relative_path"),
            size=_integer(data, "size"),
            sha256_head_tail=_string(data, "sha256_head_tail"),
            duration_ticks=_integer(data, "duration_ticks"),
            probe=probe,
        )


@dataclass(frozen=True)
class ProxyManifest:
    source_id: str
    cache_key: str
    source_fingerprint: SourceFingerprint
    source_probe: MediaProbe
    profile: ProxyProfile
    output: ProxyOutput
    tools: dict[str, str]
    checks: dict[str, bool]
    padded_video_frames: int = 0
    padded_audio_samples: int = 0
    leading_video_frames: int = 0
    leading_audio_samples: int = 0
    schema_version: int = PROXY_SCHEMA_VERSION
    status: str = "ready"

    def __post_init__(self) -> None:
        if self.schema_version != PROXY_SCHEMA_VERSION or self.status != "ready":
            raise ProjectError("proxy manifest is not a supported ready artifact")
        if _SAFE_ID.fullmatch(self.source_id) is None or _SHA256.fullmatch(self.cache_key) is None:
            raise ProjectError("proxy manifest identity is invalid")
        expected = f"proxies/{self.source_id}/{self.cache_key}/proxy.mp4"
        if self.output.relative_path != expected:
            raise ProjectError("proxy output path does not match its cache identity")
        if self.cache_key != proxy_cache_key(
            self.source_fingerprint, self.source_probe, self.profile
        ):
            raise ProjectError("proxy manifest cache key does not match its frozen inputs")
        if not self.tools or not all(
            isinstance(key, str) and key and isinstance(value, str) and value
            for key, value in self.tools.items()
        ):
            raise ProjectError("proxy manifest tool summary is invalid")
        if not self.checks or not all(
            isinstance(key, str) and key and value is True for key, value in self.checks.items()
        ):
            raise ProjectError("proxy manifest checks must all pass")
        for value in (
            self.padded_video_frames,
            self.padded_audio_samples,
            self.leading_video_frames,
            self.leading_audio_samples,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ProjectError("proxy padding summary is invalid")

    @property
    def manifest_relative_path(self) -> str:
        return f"proxies/{self.source_id}/{self.cache_key}/manifest.json"

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "source_id": self.source_id,
            "cache_key": self.cache_key,
            "source_fingerprint": self.source_fingerprint.to_dict(),
            "source_probe": self.source_probe.to_dict(),
            "profile": self.profile.to_dict(),
            "output": self.output.to_dict(),
            "tools": dict(self.tools),
            "checks": dict(self.checks),
            "padding": {
                "video_frames": self.padded_video_frames,
                "audio_samples": self.padded_audio_samples,
            },
            "leading_padding": {
                "video_frames": self.leading_video_frames,
                "audio_samples": self.leading_audio_samples,
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProxyManifest:
        fingerprint = _object(data, "source_fingerprint")
        probe = _object(data, "source_probe")
        profile = _object(data, "profile")
        output = _object(data, "output")
        tools = _string_dict(data, "tools")
        checks = _bool_dict(data, "checks")
        padding = _object(data, "padding")
        leading_padding = data.get("leading_padding", {})
        if not isinstance(leading_padding, dict):
            raise ProjectError("leading_padding must be an object")
        return cls(
            schema_version=_integer(data, "schema_version"),
            status=_string(data, "status"),
            source_id=_string(data, "source_id"),
            cache_key=_string(data, "cache_key"),
            source_fingerprint=SourceFingerprint.from_dict(fingerprint),
            source_probe=MediaProbe.from_dict(probe),
            profile=ProxyProfile.from_dict(profile),
            output=ProxyOutput.from_dict(output),
            tools=tools,
            checks=checks,
            padded_video_frames=_integer(padding, "video_frames"),
            padded_audio_samples=_integer(padding, "audio_samples"),
            leading_video_frames=_optional_padding(leading_padding, "video_frames"),
            leading_audio_samples=_optional_padding(leading_padding, "audio_samples"),
        )


def derive_proxy_profile(probe: MediaProbe, settings: dict[str, object]) -> ProxyProfile:
    rate_data = settings.get("frame_rate")
    if not isinstance(rate_data, dict):
        raise ProjectError("project frame rate is invalid")
    rate = RationalRate(_integer(rate_data, "numerator"), _integer(rate_data, "denominator"))
    project_width = _positive_even(settings.get("width"), "project width")
    project_height = _positive_even(settings.get("height"), "project height")
    scale = min(MAX_PROXY_WIDTH / project_width, MAX_PROXY_HEIGHT / project_height, 1.0)
    width = max(2, int(project_width * scale) // 2 * 2)
    height = max(2, int(project_height * scale) // 2 * 2)
    has_video = probe.video_codec is not None
    has_audio = probe.audio_codec is not None
    if has_video and (probe.width is None or probe.height is None):
        raise ProjectError("video proxy source dimensions are missing")
    if probe.duration_ticks <= 0:
        raise ProjectError("proxy source duration is invalid")
    return ProxyProfile(
        canvas_width=width,
        canvas_height=height,
        frame_rate=rate,
        gop_frames=max(1, _ceil_div(2 * rate.numerator, rate.denominator)),
        has_video=has_video,
        has_audio=has_audio,
        audio_codec="aac" if has_audio else None,
        audio_sample_rate=PROXY_AUDIO_SAMPLE_RATE if has_audio else None,
    )


def proxy_cache_key(
    fingerprint: SourceFingerprint,
    probe: MediaProbe,
    profile: ProxyProfile,
) -> str:
    payload = {
        "schema_version": PROXY_SCHEMA_VERSION,
        "profile_version": PROXY_PROFILE_VERSION,
        "timeline_policy": PROXY_TIMELINE_POLICY,
        "source_fingerprint": fingerprint.to_dict(),
        "source_probe": probe.to_dict(),
        "profile": profile.to_dict(),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _validate_relative_path(value: str) -> None:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ProjectError("proxy path must be project-relative POSIX text")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ProjectError("proxy path must remain inside the project")
    windows = PureWindowsPath(value)
    if windows.is_absolute() or windows.drive:
        raise ProjectError("proxy path must remain inside the project")


def _object(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ProjectError(f"{key} must be an object")
    return value


def _string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ProjectError(f"{key} must be a non-empty string")
    return value


def _integer(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProjectError(f"{key} must be an integer")
    return value


def _boolean(data: dict[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ProjectError(f"{key} must be a boolean")
    return value


def _optional_string(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise ProjectError("optional string is invalid")


def _optional_integer(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProjectError("optional integer is invalid")
    return value


def _optional_padding(data: dict[str, Any], key: str) -> int:
    value = data.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProjectError(f"{key} must be a non-negative integer")
    return value


def _string_dict(data: dict[str, Any], key: str) -> dict[str, str]:
    value = data.get(key)
    if not isinstance(value, dict) or not all(
        isinstance(name, str) and isinstance(item, str) for name, item in value.items()
    ):
        raise ProjectError(f"{key} must contain strings")
    return dict(value)


def _bool_dict(data: dict[str, Any], key: str) -> dict[str, bool]:
    value = data.get(key)
    if not isinstance(value, dict) or not all(
        isinstance(name, str) and isinstance(item, bool) for name, item in value.items()
    ):
        raise ProjectError(f"{key} must contain booleans")
    return dict(value)


def _positive_even(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value % 2:
        raise ProjectError(f"{name} must be a positive even integer")
    return value


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)
