from __future__ import annotations

from dataclasses import replace

import pytest

from roughcut.domain.bindings import SourceTranscriptBinding
from roughcut.domain.project import (
    ImportMode,
    MediaProbe,
    ProjectError,
    SourceAsset,
    SourceFingerprint,
)
from roughcut.domain.render import (
    MultiSourceRenderPlan,
    OutputSettings,
    RenderClip,
    RenderPlan,
    ToolResolution,
    derive_render_schedule,
    parse_render_plan,
)
from roughcut.domain.time import RationalRate


def _source() -> SourceAsset:
    return SourceAsset(
        source_id="src_render",
        kind="video",
        display_name="fixture.mp4",
        import_mode=ImportMode.COPIED,
        locator={"project_relative_path": "sources/src_render.mp4"},
        fingerprint=SourceFingerprint(123, 456, "a" * 64),
        probe=MediaProbe(
            duration_ticks=1_200_000,
            container_start_ticks=228_000,
            first_content_ticks=240_000,
            video_codec="h264",
            width=640,
            height=360,
            nominal_frame_rate={"numerator": 25, "denominator": 1},
            is_vfr=False,
            audio_codec="aac",
            audio_sample_rate=48_000,
            rotation_degrees=0,
        ),
    )


def _plan() -> RenderPlan:
    return RenderPlan(
        render_id="render_fixture",
        project_id="proj_fixture",
        project_revision=4,
        edit_version_id="edit_fixture",
        source=_source(),
        clips=(
            RenderClip("clip_b", "src_render", 360_000, 480_000),
            RenderClip("clip_a", "src_render", 120_000, 240_000),
        ),
        output_settings=OutputSettings(
            width=320,
            height=180,
            frame_rate=RationalRate(25, 1),
            audio_sample_rate=48_000,
        ),
        ffmpeg=ToolResolution("ffmpeg", "/tools/ffmpeg", "ffmpeg version fixture"),
        ffprobe=ToolResolution("ffprobe", "/tools/ffprobe", "ffprobe version fixture"),
        plan_relative_path="renders/render_fixture.plan.json",
        output_relative_path="renders/render_fixture.mp4",
        manifest_relative_path="renders/render_fixture.manifest.json",
    )


def _multi_plan() -> MultiSourceRenderPlan:
    source_a = _source()
    source_b = replace(
        _source(),
        source_id="src_second",
        display_name="second.mp4",
        locator={"project_relative_path": "sources/src_second.mp4"},
        fingerprint=SourceFingerprint(321, 654, "b" * 64),
        probe=replace(_source().probe, duration_ticks=2_400_000),
    )
    return MultiSourceRenderPlan(
        render_id="render_multi",
        project_id="proj_fixture",
        project_revision=4,
        edit_version_id="edit_multi",
        source_bindings=(
            SourceTranscriptBinding("src_render", "tr_a"),
            SourceTranscriptBinding("src_second", "tr_b"),
        ),
        sources=(source_a, source_b),
        clips=(
            RenderClip("clip_a1", "src_render", 120_000, 240_000),
            RenderClip("clip_b", "src_second", 480_000, 600_000),
            RenderClip("clip_a2", "src_render", 360_000, 480_000),
        ),
        output_settings=OutputSettings(320, 180, RationalRate(25, 1), 48_000),
        ffmpeg=ToolResolution("ffmpeg", "/tools/ffmpeg", "ffmpeg version fixture"),
        ffprobe=ToolResolution("ffprobe", "/tools/ffprobe", "ffprobe version fixture"),
        plan_relative_path="renders/render_multi.plan.json",
        output_relative_path="renders/render_multi.mp4",
        manifest_relative_path="renders/render_multi.manifest.json",
    )


def test_render_plan_roundtrip_preserves_frozen_order_ticks_and_source_snapshot() -> None:
    plan = _plan()

    restored = RenderPlan.from_dict(plan.to_dict())

    assert restored == plan
    assert restored.total_duration_ticks == 240_000
    assert [clip.clip_id for clip in restored.clips] == ["clip_b", "clip_a"]
    assert restored.source.probe.first_content_ticks == 240_000
    assert restored.source.fingerprint.sha256_head_tail == "a" * 64


def test_multisource_render_plan_roundtrip_freezes_bindings_sources_and_a_b_a() -> None:
    plan = _multi_plan()

    restored = parse_render_plan(plan.to_dict())

    assert restored == plan
    assert restored.schema_version == 2
    assert [binding.source_id for binding in restored.source_bindings] == [
        "src_render",
        "src_second",
    ]
    assert [source.source_id for source in restored.sources] == [
        "src_render",
        "src_second",
    ]
    assert [clip.source_id for clip in restored.clips] == [
        "src_render",
        "src_second",
        "src_render",
    ]
    assert restored.total_duration_ticks == 360_000


@pytest.mark.parametrize(
    "change",
    [
        {"sources": (_source(), _source())},
        {"sources": (_source(),)},
        {
            "clips": (
                RenderClip("clip_unknown", "src_unknown", 0, 120_000),
            )
        },
        {
            "clips": (
                RenderClip("clip_oob", "src_second", 0, 2_400_001),
            )
        },
    ],
)
def test_multisource_render_plan_rejects_misaligned_sources_and_clips(
    change: dict[str, object],
) -> None:
    with pytest.raises(ProjectError):
        replace(_multi_plan(), **change)


def test_render_plan_schema_dispatch_rejects_unknown_and_mixed_shapes() -> None:
    unknown = _multi_plan().to_dict()
    unknown["schema_version"] = 99
    with pytest.raises(ProjectError, match="schema"):
        parse_render_plan(unknown)

    mixed = _multi_plan().to_dict()
    mixed["source"] = _source().to_dict()
    with pytest.raises(ProjectError, match="mixed"):
        parse_render_plan(mixed)

    mixed_single = _plan().to_dict()
    mixed_single["sources"] = [_source().to_dict()]
    with pytest.raises(ProjectError, match="mixed"):
        parse_render_plan(mixed_single)


def test_multisource_schedule_quantizes_globally_and_uses_each_source_probe() -> None:
    plan = replace(
        _multi_plan(),
        sources=(
            replace(_multi_plan().sources[0], probe=replace(_source().probe, audio_codec=None, audio_sample_rate=None)),
            replace(
                _multi_plan().sources[1],
                probe=replace(
                    _multi_plan().sources[1].probe,
                    video_codec=None,
                    width=None,
                    height=None,
                    nominal_frame_rate=None,
                    audio_codec="aac",
                    audio_sample_rate=48_000,
                ),
            ),
        ),
    )

    schedule = derive_render_schedule(plan)

    assert schedule.total_frames == 75
    assert schedule.total_samples == 144_000
    assert [clip.input_index for clip in schedule.clips] == [0, 1, 2]
    assert schedule.clips[0].access_end_ticks == 244_800
    assert schedule.clips[1].access_end_ticks > 600_000
    assert schedule.clips[2].access_end_ticks == 484_800


def test_multisource_many_short_clips_share_one_global_frame_and_sample_rounding() -> None:
    clips = tuple(
        RenderClip(
            f"clip_{index:02d}",
            "src_render" if index % 2 == 0 else "src_second",
            index * 12_120,
            (index + 1) * 12_120,
        )
        for index in range(54)
    )
    plan = replace(
        _multi_plan(),
        clips=clips,
        output_settings=OutputSettings(320, 180, RationalRate(30_000, 1_001), 48_000),
    )

    schedule = derive_render_schedule(plan)

    assert schedule.total_duration_ticks == 654_480
    assert schedule.total_frames == 163
    assert schedule.total_samples == 261_792
    assert sum(clip.frame_count for clip in schedule.clips) == 163
    assert sum(clip.sample_count for clip in schedule.clips) == 261_792


@pytest.mark.parametrize(
    "change",
    [
        {"clips": ()},
        {"clips": (RenderClip("clip_a", "src_unknown", 0, 120_000),)},
        {"clips": (RenderClip("clip_a", "src_render", 0, 1_200_001),)},
        {"output_relative_path": "../escape.mp4"},
        {"manifest_relative_path": "/absolute/manifest.json"},
    ],
)
def test_render_plan_rejects_invalid_or_escaping_frozen_state(
    change: dict[str, object],
) -> None:
    with pytest.raises(ProjectError):
        replace(_plan(), **change)


def test_render_clip_rejects_negative_or_empty_ranges() -> None:
    with pytest.raises(ProjectError):
        RenderClip("clip_a", "src_render", -1, 120_000)
    with pytest.raises(ProjectError):
        RenderClip("clip_a", "src_render", 120_000, 120_000)


def test_output_settings_require_even_dimensions_and_integral_tick_frame_duration() -> None:
    with pytest.raises(ProjectError):
        OutputSettings(319, 180, RationalRate(25, 1), 48_000)
    with pytest.raises(ProjectError):
        OutputSettings(320, 180, RationalRate(29, 1), 48_000)


@pytest.mark.parametrize(
    ("frame_rate", "expected_frames"),
    [
        (RationalRate(25, 1), 136),
        (RationalRate(30, 1), 164),
        (RationalRate(30_000, 1_001), 163),
    ],
)
def test_schedule_quantizes_54_clips_from_cumulative_timeline_boundaries(
    frame_rate: RationalRate,
    expected_frames: int,
) -> None:
    clips = tuple(
        RenderClip(f"clip_{index:02d}", "src_render", index * 12_120, (index + 1) * 12_120)
        for index in range(54)
    )
    plan = replace(
        _plan(),
        clips=clips,
        output_settings=OutputSettings(320, 180, frame_rate, 48_000),
    )

    schedule = derive_render_schedule(plan)

    assert schedule.rounding_rule == "nearest_half_up"
    assert schedule.total_duration_ticks == 654_480
    assert schedule.total_frames == expected_frames
    assert schedule.total_samples == 261_792
    assert sum(clip.frame_count for clip in schedule.clips) == expected_frames
    assert sum(clip.sample_count for clip in schedule.clips) == 261_792
    assert [clip.timeline_out_ticks for clip in schedule.clips[:-1]] == [
        clip.timeline_in_ticks for clip in schedule.clips[1:]
    ]
    assert [clip.output_frame_out for clip in schedule.clips[:-1]] == [
        clip.output_frame_in for clip in schedule.clips[1:]
    ]
    assert [clip.output_sample_out for clip in schedule.clips[:-1]] == [
        clip.output_sample_in for clip in schedule.clips[1:]
    ]


def test_schedule_rejects_a_nonempty_clip_that_quantizes_to_no_output_frame() -> None:
    plan = replace(
        _plan(),
        clips=(RenderClip("clip_tiny", "src_render", 0, 1),),
    )

    with pytest.raises(ProjectError, match="empty output frame range"):
        derive_render_schedule(plan)
