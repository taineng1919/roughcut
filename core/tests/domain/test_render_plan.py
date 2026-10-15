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
    render_manifest_projection,
)
from roughcut.domain.time import RationalRate
from roughcut.domain.workflow import subject_content_hash


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


def test_pre_feature_render_plans_roundtrip_without_generated_sources_key() -> None:
    """Plans written before generated sources existed must not drift.

    ``generated_sources`` is a place-only addition; parsing an old plan dict and
    re-serializing it must reproduce the original mapping exactly so the stored
    plan and its hashes stay valid.
    """

    for plan in (_plan(), _multi_plan()):
        legacy = {
            key: value
            for key, value in plan.to_dict().items()
            if key != "generated_sources"
        }
        assert "generated_sources" not in legacy
        parsed = parse_render_plan(legacy)
        assert parsed.to_dict() == legacy
        assert parsed.generated_sources == ()
        assert plan.to_dict() == legacy
        direct = type(plan).from_dict(legacy)
        assert direct.to_dict() == legacy


def _generated_source(source_id: str) -> dict[str, object]:
    return {
        "source_id": source_id,
        "fingerprint": {
            "size": 1_024,
            "mtime_ns": 2_048,
            "sha256_head_tail": "c" * 64,
        },
        "probe": {"duration_ticks": 240_000},
    }


def _manifest_bundle(schema_version: int) -> tuple[dict[str, object], dict[str, object]]:
    """Return one closed Render manifest and its frozen legacy projection.

    The projection is written by hand instead of calling the production helper
    so this test independently pins the pre-placeholder bytes and subject hash.
    """

    clips = [RenderClip("clip_a", "src_render", 0, 120_000).to_dict()]
    output_settings = {"width": 320, "height": 180}
    acceptance = {"accepted": True, "checks": {"duration": True}}
    output = {
        "mp4_path": "renders/render_fixture.mp4",
        "probe": {"duration_ticks": 120_000},
    }
    payload: dict[str, object] = {
        "schema_version": schema_version,
        "render_id": "render_fixture",
        "project_id": "proj_fixture",
        "project_revision": 4,
        "edit_version_id": "edit_fixture",
        "tools": {"ffmpeg_version": "fixture", "ffprobe_version": "fixture"},
        "output_settings": output_settings,
        "clips": clips,
        "total_duration_ticks": 120_000,
        "render_schedule": {"strategy": "fixture", "clips": []},
        "command_summary": {"video_encoder": "fixture"},
        "performance": {"wall_seconds": 0},
        "output": output,
        "acceptance": acceptance,
    }
    if schema_version == 2:
        inputs: dict[str, object] = {
            "input_source": {
                "source_id": "src_render",
                "fingerprint": {"size": 1, "mtime_ns": 2, "sha256_head_tail": "a" * 64},
                "probe": {"duration_ticks": 120_000},
            }
        }
    else:
        inputs = {
            "source_bindings": [
                {"source_id": "src_render", "transcript_version_id": "tr_a"}
            ],
            "input_sources": [
                {
                    "source_id": "src_render",
                    "fingerprint": {
                        "size": 1,
                        "mtime_ns": 2,
                        "sha256_head_tail": "a" * 64,
                    },
                    "probe": {"duration_ticks": 120_000},
                }
            ],
        }
    payload.update(inputs)
    projection = {
        "render_id": "render_fixture",
        "edit_version_id": "edit_fixture",
        **inputs,
        "clips": clips,
        "output_settings": output_settings,
        "output": {"mp4_path": output["mp4_path"]},
        "acceptance": acceptance,
    }
    return payload, projection


@pytest.mark.parametrize("schema_version", [2, 3])
def test_manifest_projection_keeps_pre_placeholder_bytes_and_hash(
    schema_version: int,
) -> None:
    payload, projection = _manifest_bundle(schema_version)

    assert render_manifest_projection(payload) == projection
    assert subject_content_hash(
        "render_manifest", schema_version, render_manifest_projection(payload)
    ) == subject_content_hash("render_manifest", schema_version, projection)


@pytest.mark.parametrize("schema_version", [2, 3])
def test_manifest_projection_binds_generated_sources_into_the_subject(
    schema_version: int,
) -> None:
    payload, projection = _manifest_bundle(schema_version)
    generated = _generated_source("src_placeholder")
    payload["generated_sources"] = [generated]

    placeholder_projection = render_manifest_projection(payload)

    assert placeholder_projection == {**projection, "generated_sources": [generated]}
    assert subject_content_hash(
        "render_manifest", schema_version, placeholder_projection
    ) != subject_content_hash("render_manifest", schema_version, projection)
    tampered = _generated_source("src_placeholder")
    tampered["fingerprint"] = {
        "size": 2_048,
        "mtime_ns": 2_048,
        "sha256_head_tail": "c" * 64,
    }
    payload["generated_sources"] = [tampered]
    assert render_manifest_projection(payload) != placeholder_projection


@pytest.mark.parametrize("schema_version", [2, 3])
@pytest.mark.parametrize(
    "mutation",
    [
        {"unexpected": True},
        {"generated_sources": []},
        {"generated_sources": None},
        {"generated_sources": "src_placeholder"},
        {"generated_sources": ["src_placeholder"]},
        {"generated_sources": [{"source_id": "src_placeholder"}]},
        {
            "generated_sources": [
                {**_generated_source("src_placeholder"), "locator": {}}
            ]
        },
    ],
)
def test_manifest_projection_rejects_unknown_or_malformed_fields(
    schema_version: int,
    mutation: dict[str, object],
) -> None:
    payload, _projection = _manifest_bundle(schema_version)
    payload.update(mutation)

    with pytest.raises(ProjectError, match="Render manifest"):
        render_manifest_projection(payload)


def test_manifest_projection_rejects_an_unsupported_schema_version() -> None:
    payload, _projection = _manifest_bundle(2)
    payload["schema_version"] = 1

    with pytest.raises(ProjectError, match="unsupported Render manifest schema"):
        render_manifest_projection(payload)


def test_manifest_projection_rejects_an_invalid_output_payload() -> None:
    payload, _projection = _manifest_bundle(3)
    payload["output"] = {"mp4_path": "renders/render_fixture.mp4"}

    with pytest.raises(ProjectError, match="Render manifest output is invalid"):
        render_manifest_projection(payload)
