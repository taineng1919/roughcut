"""Frozen versioned single- and multi-source render plan models."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, TypeAlias

from roughcut.domain.bindings import SourceTranscriptBinding, parse_source_bindings
from roughcut.domain.project import ProjectError, SourceAsset
from roughcut.domain.time import TICKS_PER_SECOND, RationalRate

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class RenderClip:
    clip_id: str
    source_id: str
    source_in_ticks: int
    source_out_ticks: int

    def __post_init__(self) -> None:
        if _SAFE_ID.fullmatch(self.clip_id) is None or _SAFE_ID.fullmatch(self.source_id) is None:
            raise ProjectError("render clip identity is invalid")
        if (
            isinstance(self.source_in_ticks, bool)
            or not isinstance(self.source_in_ticks, int)
            or isinstance(self.source_out_ticks, bool)
            or not isinstance(self.source_out_ticks, int)
            or self.source_in_ticks < 0
            or self.source_out_ticks <= self.source_in_ticks
        ):
            raise ProjectError("render clip range is invalid")

    @property
    def duration_ticks(self) -> int:
        return self.source_out_ticks - self.source_in_ticks

    def to_dict(self) -> dict[str, object]:
        return {
            "clip_id": self.clip_id,
            "source_id": self.source_id,
            "source_in_ticks": self.source_in_ticks,
            "source_out_ticks": self.source_out_ticks,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RenderClip:
        return cls(
            clip_id=_string(data, "clip_id"),
            source_id=_string(data, "source_id"),
            source_in_ticks=_integer(data, "source_in_ticks"),
            source_out_ticks=_integer(data, "source_out_ticks"),
        )


@dataclass(frozen=True)
class OutputSettings:
    width: int
    height: int
    frame_rate: RationalRate
    audio_sample_rate: int

    def __post_init__(self) -> None:
        for name, value in (("width", self.width), ("height", self.height)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value % 2:
                raise ProjectError(f"render output {name} must be a positive even integer")
        if (
            isinstance(self.audio_sample_rate, bool)
            or not isinstance(self.audio_sample_rate, int)
            or self.audio_sample_rate <= 0
        ):
            raise ProjectError("render audio sample rate is invalid")
        try:
            _validated_ticks_per_frame = self.frame_rate.ticks_per_frame
        except ValueError as error:
            raise ProjectError("render frame rate is not representable in project ticks") from error

    def to_dict(self) -> dict[str, object]:
        return {
            "width": self.width,
            "height": self.height,
            "frame_rate": {
                "numerator": self.frame_rate.numerator,
                "denominator": self.frame_rate.denominator,
            },
            "audio_sample_rate": self.audio_sample_rate,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OutputSettings:
        rate = data.get("frame_rate")
        if not isinstance(rate, dict):
            raise ProjectError("render frame rate is required")
        return cls(
            width=_integer(data, "width"),
            height=_integer(data, "height"),
            frame_rate=RationalRate(
                _integer(rate, "numerator"), _integer(rate, "denominator")
            ),
            audio_sample_rate=_integer(data, "audio_sample_rate"),
        )


@dataclass(frozen=True)
class ToolResolution:
    command: str
    resolved_path: str
    version: str

    def __post_init__(self) -> None:
        if not self.command or not self.resolved_path or not self.version:
            raise ProjectError("render tool resolution is incomplete")

    def to_dict(self) -> dict[str, object]:
        return {
            "command": self.command,
            "resolved_path": self.resolved_path,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolResolution:
        return cls(
            command=_string(data, "command"),
            resolved_path=_string(data, "resolved_path"),
            version=_string(data, "version"),
        )


@dataclass(frozen=True)
class RenderPlan:
    render_id: str
    project_id: str
    project_revision: int
    edit_version_id: str
    source: SourceAsset
    clips: tuple[RenderClip, ...]
    output_settings: OutputSettings
    ffmpeg: ToolResolution
    ffprobe: ToolResolution
    plan_relative_path: str
    output_relative_path: str
    manifest_relative_path: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        for name, value in (
            ("render_id", self.render_id),
            ("project_id", self.project_id),
            ("edit_version_id", self.edit_version_id),
        ):
            if _SAFE_ID.fullmatch(value) is None:
                raise ProjectError(f"{name} is invalid")
        if (
            isinstance(self.project_revision, bool)
            or not isinstance(self.project_revision, int)
            or self.project_revision < 0
        ):
            raise ProjectError("render project revision is invalid")
        if self.schema_version != 1:
            raise ProjectError("unsupported render plan schema version")
        if not self.clips:
            raise ProjectError("render plan requires at least one clip")
        seen: set[str] = set()
        for clip in self.clips:
            if clip.clip_id in seen:
                raise ProjectError("render clip IDs must be unique")
            seen.add(clip.clip_id)
            if clip.source_id != self.source.source_id:
                raise ProjectError("render plan may reference only its frozen source")
            if clip.source_out_ticks > self.source.probe.duration_ticks:
                raise ProjectError("render clip is outside source bounds")
        for relative_path in (
            self.plan_relative_path,
            self.output_relative_path,
            self.manifest_relative_path,
        ):
            _validate_relative_path(relative_path)
        expected_paths = (
            f"renders/{self.render_id}.plan.json",
            f"renders/{self.render_id}.mp4",
            f"renders/{self.render_id}.manifest.json",
        )
        if (
            self.plan_relative_path,
            self.output_relative_path,
            self.manifest_relative_path,
        ) != expected_paths:
            raise ProjectError("render artifact paths do not match the render identity")

    @property
    def total_duration_ticks(self) -> int:
        return sum(clip.duration_ticks for clip in self.clips)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "render_id": self.render_id,
            "project_id": self.project_id,
            "project_revision": self.project_revision,
            "edit_version_id": self.edit_version_id,
            "source": self.source.to_dict(),
            "clips": [clip.to_dict() for clip in self.clips],
            "total_duration_ticks": self.total_duration_ticks,
            "output_settings": self.output_settings.to_dict(),
            "ffmpeg": self.ffmpeg.to_dict(),
            "ffprobe": self.ffprobe.to_dict(),
            "plan_relative_path": self.plan_relative_path,
            "output_relative_path": self.output_relative_path,
            "manifest_relative_path": self.manifest_relative_path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RenderPlan:
        if "sources" in data or "source_bindings" in data:
            raise ProjectError("mixed render plan schema is invalid")
        source = data.get("source")
        clips = data.get("clips")
        settings = data.get("output_settings")
        ffmpeg = data.get("ffmpeg")
        ffprobe = data.get("ffprobe")
        if (
            not isinstance(source, dict)
            or not isinstance(clips, list)
            or not isinstance(settings, dict)
            or not isinstance(ffmpeg, dict)
            or not isinstance(ffprobe, dict)
        ):
            raise ProjectError("render plan snapshot is incomplete")
        plan = cls(
            schema_version=_integer(data, "schema_version"),
            render_id=_string(data, "render_id"),
            project_id=_string(data, "project_id"),
            project_revision=_integer(data, "project_revision"),
            edit_version_id=_string(data, "edit_version_id"),
            source=SourceAsset.from_dict(source),
            clips=tuple(
                RenderClip.from_dict(clip) if isinstance(clip, dict) else _invalid_clip()
                for clip in clips
            ),
            output_settings=OutputSettings.from_dict(settings),
            ffmpeg=ToolResolution.from_dict(ffmpeg),
            ffprobe=ToolResolution.from_dict(ffprobe),
            plan_relative_path=_string(data, "plan_relative_path"),
            output_relative_path=_string(data, "output_relative_path"),
            manifest_relative_path=_string(data, "manifest_relative_path"),
        )
        if data.get("total_duration_ticks") != plan.total_duration_ticks:
            raise ProjectError("render plan total duration is inconsistent")
        return plan


@dataclass(frozen=True)
class MultiSourceRenderPlan:
    render_id: str
    project_id: str
    project_revision: int
    edit_version_id: str
    source_bindings: tuple[SourceTranscriptBinding, ...]
    sources: tuple[SourceAsset, ...]
    clips: tuple[RenderClip, ...]
    output_settings: OutputSettings
    ffmpeg: ToolResolution
    ffprobe: ToolResolution
    plan_relative_path: str
    output_relative_path: str
    manifest_relative_path: str
    schema_version: int = 2

    def __post_init__(self) -> None:
        for name, value in (
            ("render_id", self.render_id),
            ("project_id", self.project_id),
            ("edit_version_id", self.edit_version_id),
        ):
            if _SAFE_ID.fullmatch(value) is None:
                raise ProjectError(f"{name} is invalid")
        if (
            isinstance(self.project_revision, bool)
            or not isinstance(self.project_revision, int)
            or self.project_revision < 0
        ):
            raise ProjectError("render project revision is invalid")
        if self.schema_version != 2:
            raise ProjectError("unsupported multi-source render plan schema version")
        if len(self.source_bindings) < 2 or len(self.sources) != len(self.source_bindings):
            raise ProjectError("multi-source render plan bindings and sources are incomplete")
        binding_ids = [binding.source_id for binding in self.source_bindings]
        source_ids = [source.source_id for source in self.sources]
        if len(binding_ids) != len(set(binding_ids)) or len(source_ids) != len(set(source_ids)):
            raise ProjectError("multi-source render plan source IDs must be unique")
        if source_ids != binding_ids:
            raise ProjectError("render source order must match source bindings")
        if not self.clips:
            raise ProjectError("render plan requires at least one clip")
        sources_by_id = {source.source_id: source for source in self.sources}
        seen_clips: set[str] = set()
        for clip in self.clips:
            if clip.clip_id in seen_clips:
                raise ProjectError("render clip IDs must be unique")
            seen_clips.add(clip.clip_id)
            source = sources_by_id.get(clip.source_id)
            if source is None:
                raise ProjectError("render clip is outside its source bindings")
            if source.probe.video_codec is None and source.probe.audio_codec is None:
                raise ProjectError("render source has no usable media stream")
            if clip.source_out_ticks > source.probe.duration_ticks:
                raise ProjectError("render clip is outside source bounds")
        for relative_path in (
            self.plan_relative_path,
            self.output_relative_path,
            self.manifest_relative_path,
        ):
            _validate_relative_path(relative_path)
        expected_paths = (
            f"renders/{self.render_id}.plan.json",
            f"renders/{self.render_id}.mp4",
            f"renders/{self.render_id}.manifest.json",
        )
        if (
            self.plan_relative_path,
            self.output_relative_path,
            self.manifest_relative_path,
        ) != expected_paths:
            raise ProjectError("render artifact paths do not match the render identity")

    @property
    def total_duration_ticks(self) -> int:
        return sum(clip.duration_ticks for clip in self.clips)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "render_id": self.render_id,
            "project_id": self.project_id,
            "project_revision": self.project_revision,
            "edit_version_id": self.edit_version_id,
            "source_bindings": [binding.to_dict() for binding in self.source_bindings],
            "sources": [source.to_dict() for source in self.sources],
            "clips": [clip.to_dict() for clip in self.clips],
            "total_duration_ticks": self.total_duration_ticks,
            "output_settings": self.output_settings.to_dict(),
            "ffmpeg": self.ffmpeg.to_dict(),
            "ffprobe": self.ffprobe.to_dict(),
            "plan_relative_path": self.plan_relative_path,
            "output_relative_path": self.output_relative_path,
            "manifest_relative_path": self.manifest_relative_path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MultiSourceRenderPlan:
        if "source" in data:
            raise ProjectError("mixed render plan schema is invalid")
        sources = data.get("sources")
        clips = data.get("clips")
        settings = data.get("output_settings")
        ffmpeg = data.get("ffmpeg")
        ffprobe = data.get("ffprobe")
        if (
            not isinstance(sources, list)
            or not isinstance(clips, list)
            or not isinstance(settings, dict)
            or not isinstance(ffmpeg, dict)
            or not isinstance(ffprobe, dict)
        ):
            raise ProjectError("multi-source render plan snapshot is incomplete")
        parsed_sources: list[SourceAsset] = []
        for source in sources:
            if not isinstance(source, dict):
                raise ProjectError("render source snapshot must be an object")
            parsed_sources.append(SourceAsset.from_dict(source))
        plan = cls(
            schema_version=_integer(data, "schema_version"),
            render_id=_string(data, "render_id"),
            project_id=_string(data, "project_id"),
            project_revision=_integer(data, "project_revision"),
            edit_version_id=_string(data, "edit_version_id"),
            source_bindings=parse_source_bindings(data.get("source_bindings")),
            sources=tuple(parsed_sources),
            clips=tuple(
                RenderClip.from_dict(clip) if isinstance(clip, dict) else _invalid_clip()
                for clip in clips
            ),
            output_settings=OutputSettings.from_dict(settings),
            ffmpeg=ToolResolution.from_dict(ffmpeg),
            ffprobe=ToolResolution.from_dict(ffprobe),
            plan_relative_path=_string(data, "plan_relative_path"),
            output_relative_path=_string(data, "output_relative_path"),
            manifest_relative_path=_string(data, "manifest_relative_path"),
        )
        if data.get("total_duration_ticks") != plan.total_duration_ticks:
            raise ProjectError("render plan total duration is inconsistent")
        return plan


RenderPlanLike: TypeAlias = RenderPlan | MultiSourceRenderPlan


def parse_render_plan(data: dict[str, Any]) -> RenderPlanLike:
    schema_version = data.get("schema_version")
    if schema_version == 1:
        return RenderPlan.from_dict(data)
    if schema_version == 2:
        return MultiSourceRenderPlan.from_dict(data)
    raise ProjectError("unsupported render plan schema version")


@dataclass(frozen=True)
class ScheduledRenderClip:
    clip_id: str
    source_id: str
    source_in_ticks: int
    source_out_ticks: int
    timeline_in_ticks: int
    timeline_out_ticks: int
    output_frame_in: int
    output_frame_out: int
    output_sample_in: int
    output_sample_out: int
    input_index: int
    access_start_ticks: int
    access_end_ticks: int

    @property
    def frame_count(self) -> int:
        return self.output_frame_out - self.output_frame_in

    @property
    def sample_count(self) -> int:
        return self.output_sample_out - self.output_sample_in

    @property
    def access_duration_ticks(self) -> int:
        return self.access_end_ticks - self.access_start_ticks

    def to_dict(self) -> dict[str, object]:
        return {
            "clip_id": self.clip_id,
            "source_id": self.source_id,
            "source_in_ticks": self.source_in_ticks,
            "source_out_ticks": self.source_out_ticks,
            "timeline_in_ticks": self.timeline_in_ticks,
            "timeline_out_ticks": self.timeline_out_ticks,
            "output_frame_in": self.output_frame_in,
            "output_frame_out": self.output_frame_out,
            "frame_count": self.frame_count,
            "output_sample_in": self.output_sample_in,
            "output_sample_out": self.output_sample_out,
            "sample_count": self.sample_count,
            "input_index": self.input_index,
            "access_start_ticks": self.access_start_ticks,
            "access_end_ticks": self.access_end_ticks,
            "access_duration_ticks": self.access_duration_ticks,
        }


@dataclass(frozen=True)
class RenderSchedule:
    clips: tuple[ScheduledRenderClip, ...]
    total_duration_ticks: int
    total_frames: int
    total_samples: int
    strategy: str = "clip_local_accurate_seek"
    rounding_rule: str = "nearest_half_up"
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ProjectError("unsupported render schedule schema version")
        if self.strategy != "clip_local_accurate_seek":
            raise ProjectError("unsupported render schedule strategy")
        if self.rounding_rule != "nearest_half_up":
            raise ProjectError("unsupported render schedule rounding rule")
        if not self.clips:
            raise ProjectError("render schedule requires at least one clip")
        previous_ticks = 0
        previous_frame = 0
        previous_sample = 0
        for expected_input, clip in enumerate(self.clips):
            if (
                clip.input_index != expected_input
                or clip.timeline_in_ticks != previous_ticks
                or clip.output_frame_in != previous_frame
                or clip.output_sample_in != previous_sample
            ):
                raise ProjectError("render schedule boundaries are not contiguous")
            if clip.frame_count <= 0:
                raise ProjectError("render clip has an empty output frame range")
            if clip.sample_count <= 0:
                raise ProjectError("render clip has an empty output sample range")
            if (
                clip.access_start_ticks != clip.source_in_ticks
                or clip.access_end_ticks < clip.source_out_ticks
                or clip.access_end_ticks <= clip.access_start_ticks
            ):
                raise ProjectError("render access window is invalid")
            previous_ticks = clip.timeline_out_ticks
            previous_frame = clip.output_frame_out
            previous_sample = clip.output_sample_out
        if (
            previous_ticks != self.total_duration_ticks
            or previous_frame != self.total_frames
            or previous_sample != self.total_samples
        ):
            raise ProjectError("render schedule totals are inconsistent")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "strategy": self.strategy,
            "rounding_rule": self.rounding_rule,
            "total_duration_ticks": self.total_duration_ticks,
            "total_frames": self.total_frames,
            "total_samples": self.total_samples,
            "clips": [clip.to_dict() for clip in self.clips],
        }


def derive_render_schedule(plan: RenderPlanLike) -> RenderSchedule:
    timeline_boundaries = [0]
    for clip in plan.clips:
        timeline_boundaries.append(timeline_boundaries[-1] + clip.duration_ticks)
    frame_boundaries = [
        _round_nonnegative_half_up(
            ticks * plan.output_settings.frame_rate.numerator,
            TICKS_PER_SECOND * plan.output_settings.frame_rate.denominator,
        )
        for ticks in timeline_boundaries
    ]
    sample_boundaries = [
        _round_nonnegative_half_up(
            ticks * plan.output_settings.audio_sample_rate,
            TICKS_PER_SECOND,
        )
        for ticks in timeline_boundaries
    ]
    scheduled: list[ScheduledRenderClip] = []
    for index, clip in enumerate(plan.clips):
        source = source_for_render_clip(plan, clip)
        has_video = source.probe.video_codec is not None
        has_audio = source.probe.audio_codec is not None
        frame_tail_ticks = plan.output_settings.frame_rate.ticks_per_frame if has_video else 0
        sample_tail_ticks = (
            _ceil_div(TICKS_PER_SECOND, plan.output_settings.audio_sample_rate)
            if has_audio
            else 0
        )
        access_tail_ticks = max(frame_tail_ticks, sample_tail_ticks)
        scheduled.append(
            ScheduledRenderClip(
                clip_id=clip.clip_id,
                source_id=clip.source_id,
                source_in_ticks=clip.source_in_ticks,
                source_out_ticks=clip.source_out_ticks,
                timeline_in_ticks=timeline_boundaries[index],
                timeline_out_ticks=timeline_boundaries[index + 1],
                output_frame_in=frame_boundaries[index],
                output_frame_out=frame_boundaries[index + 1],
                output_sample_in=sample_boundaries[index],
                output_sample_out=sample_boundaries[index + 1],
                input_index=index,
                access_start_ticks=clip.source_in_ticks,
                access_end_ticks=min(
                    source.probe.duration_ticks,
                    clip.source_out_ticks + access_tail_ticks,
                ),
            )
        )
    return RenderSchedule(
        clips=tuple(scheduled),
        total_duration_ticks=timeline_boundaries[-1],
        total_frames=frame_boundaries[-1],
        total_samples=sample_boundaries[-1],
    )


def render_sources(plan: RenderPlanLike) -> tuple[SourceAsset, ...]:
    if isinstance(plan, RenderPlan):
        return (plan.source,)
    return plan.sources


def source_for_render_clip(plan: RenderPlanLike, clip: RenderClip) -> SourceAsset:
    for source in render_sources(plan):
        if source.source_id == clip.source_id:
            return source
    raise ProjectError("render clip references an unknown frozen source")


def render_has_audio(plan: RenderPlanLike) -> bool:
    source_by_id = {source.source_id: source for source in render_sources(plan)}
    return any(source_by_id[clip.source_id].probe.audio_codec is not None for clip in plan.clips)


def _round_nonnegative_half_up(numerator: int, denominator: int) -> int:
    if numerator < 0 or denominator <= 0:
        raise ProjectError("render quantization requires a nonnegative rational value")
    return (2 * numerator + denominator) // (2 * denominator)


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def _validate_relative_path(value: str) -> None:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise ProjectError("render artifact path must be project-relative")


def _invalid_clip() -> RenderClip:
    raise ProjectError("render clip must be an object")


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
