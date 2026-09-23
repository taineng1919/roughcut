from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path

from roughcut.domain.nle_handoff import (
    NleHandoffClip,
    NleHandoffGap,
    NleHandoffTimeline,
    NleHandoffTrack,
)
from roughcut.domain.time import TICKS_PER_SECOND, RationalRate

DEFAULT_PROJECT_RATE = RationalRate(25, 1)
DEFAULT_SOURCE_RATE = RationalRate(50, 1)


@dataclass(frozen=True)
class FcpxmlForensicCase:
    case_id: str
    project_rate: RationalRate
    source_rate: RationalRate
    main_source: tuple[int, int]
    main_timeline: tuple[int, int]
    aux_source: tuple[int, int]
    aux_timeline: tuple[int, int]
    relationship: str


FCPXML_FORENSIC_CASES = (
    FcpxmlForensicCase(
        case_id="case_1_illegal_connected_offset",
        project_rate=RationalRate(25, 1),
        source_rate=RationalRate(50, 1),
        main_source=(19_936_800, 20_143_200),
        main_timeline=(0, 206_400),
        aux_source=(19_824_285, 20_030_685),
        aux_timeline=(0, 206_400),
        relationship="aux1 mapped child of same main Decision clip",
    ),
    FcpxmlForensicCase(
        case_id="case_2_illegal_duration",
        project_rate=RationalRate(25, 1),
        source_rate=RationalRate(50, 1),
        main_source=(20_143_200, 20_217_000),
        main_timeline=(206_400, 280_200),
        aux_source=(20_030_685, 20_104_485),
        aux_timeline=(206_400, 280_200),
        relationship="aux1 mapped child of same main Decision clip",
    ),
    FcpxmlForensicCase(
        case_id="case_3_sequence_tail_candidate",
        project_rate=RationalRate(25, 1),
        source_rate=RationalRate(50, 1),
        main_source=(88_310_400, 88_473_000),
        main_timeline=(33_860_520, 34_023_120),
        aux_source=(88_357_380, 88_519_980),
        aux_timeline=(33_860_520, 34_023_120),
        relationship="aux1 mapped child of final main Decision clip",
    ),
)


def build_timeline(
    tmp_path: Path,
    *,
    multicam: bool = True,
    rate: RationalRate = DEFAULT_PROJECT_RATE,
    source_rate: RationalRate | None = None,
    source_width: int = 1920,
    source_height: int = 1080,
) -> NleHandoffTimeline:
    source_rate = source_rate or rate
    media_root = tmp_path / "原始素材"
    source_a_path = (media_root / "A 主机位 & source.mp4").absolute()
    source_b_path = (media_root / "B 副机位 source.mp4").absolute()
    project_frame_ticks = rate.ticks_per_frame
    source_frame_ticks = source_rate.ticks_per_frame

    def timeline_ticks(project_frames: int) -> int:
        return project_frames * project_frame_ticks

    def source_ticks(project_frames: int) -> int:
        source_frames = Fraction(
            project_frames * source_rate.numerator * rate.denominator,
            rate.numerator * source_rate.denominator,
        )
        if source_frames.denominator != 1:
            raise ValueError("fixture source range is not source-frame aligned")
        return source_frames.numerator * source_frame_ticks

    full_duration = source_ticks(2_000)

    def clip(
        logical_id: str,
        source_id: str,
        source_path: Path,
        source_in: int,
        source_out: int,
        timeline_in: int,
        timeline_out: int,
        camera_id: str,
        track_id: str,
    ) -> NleHandoffClip:
        return NleHandoffClip(
            logical_clip_id=logical_id,
            source_id=source_id,
            source_locator=source_path,
            source_duration_ticks=full_duration,
            source_display_name=source_path.stem,
            source_width=source_width,
            source_height=source_height,
            source_nominal_frame_rate=source_rate,
            source_is_vfr=False,
            source_audio_sample_rate=48_000,
            source_in_ticks=source_in,
            source_out_ticks=source_out,
            timeline_in_ticks=timeline_in,
            timeline_out_ticks=timeline_out,
            camera_id=camera_id,
            track_id=track_id,
            media_types=("video", "audio"),
            av_link_id=f"link_{logical_id}",
        )

    segment = timeline_ticks(250)
    main = (
        clip(
            "clip_1",
            "source_a",
            source_a_path,
            source_ticks(500),
            source_ticks(750),
            0,
            segment,
            "main",
            "main",
        ),
        clip(
            "clip_2",
            "source_a",
            source_a_path,
            source_ticks(1_250),
            source_ticks(1_500),
            segment,
            2 * segment,
            "main",
            "main",
        ),
        clip(
            "clip_3",
            "source_a",
            source_a_path,
            source_ticks(875),
            source_ticks(1_125),
            2 * segment,
            3 * segment,
            "main",
            "main",
        ),
    )
    tracks = [
        NleHandoffTrack(
            track_id="main",
            camera_id="main",
            role="main",
            media_types=("video", "audio"),
            items=main,
        )
    ]
    if multicam:
        aux_track_id = "aux_aux_1"
        auxiliary = (
            clip(
                "aux_1",
                "source_b",
                source_b_path,
                source_ticks(575),
                source_ticks(825),
                0,
                segment,
                "aux_1",
                aux_track_id,
            ),
            clip(
                "aux_2",
                "source_b",
                source_b_path,
                source_ticks(1_325),
                source_ticks(1_450),
                segment,
                segment + timeline_ticks(125),
                "aux_1",
                aux_track_id,
            ),
            NleHandoffGap(
                "gap_1",
                segment + timeline_ticks(125),
                2 * segment,
                "aux_1",
                aux_track_id,
            ),
            clip(
                "aux_3",
                "source_b",
                source_b_path,
                source_ticks(950),
                source_ticks(1_200),
                2 * segment,
                3 * segment,
                "aux_1",
                aux_track_id,
            ),
        )
        tracks.append(
            NleHandoffTrack(
                track_id=aux_track_id,
                camera_id="aux_1",
                role="auxiliary",
                media_types=("video", "audio"),
                items=auxiliary,
            )
        )
    return NleHandoffTimeline(
        project_id="project_fixture",
        timebase=TICKS_PER_SECOND,
        frame_rate=rate,
        video_width=1920,
        video_height=1080,
        audio_sample_rate=48_000,
        duration_ticks=3 * segment,
        tracks=tuple(tracks),
    )


def build_mixed_rate_timeline(
    tmp_path: Path,
    *,
    project_rate: RationalRate,
    source_rate: RationalRate,
    source_width: int = 3840,
    source_height: int = 2160,
) -> NleHandoffTimeline:
    source_path = (tmp_path / "mixed-rate" / "Source 50fps.mp4").absolute()
    source_duration_ticks = 4_000 * source_rate.ticks_per_frame
    clip_duration_ticks = 50 * project_rate.ticks_per_frame
    clip = NleHandoffClip(
        logical_clip_id="mixed_clip",
        source_id="mixed_source",
        source_locator=source_path,
        source_duration_ticks=source_duration_ticks,
        source_display_name=source_path.stem,
        source_width=source_width,
        source_height=source_height,
        source_nominal_frame_rate=source_rate,
        source_is_vfr=False,
        source_audio_sample_rate=48_000,
        source_in_ticks=11 * source_rate.ticks_per_frame,
        source_out_ticks=111 * source_rate.ticks_per_frame,
        timeline_in_ticks=0,
        timeline_out_ticks=clip_duration_ticks,
        camera_id="main",
        track_id="main",
        media_types=("video", "audio"),
        av_link_id="link_mixed_clip",
    )
    track = NleHandoffTrack(
        track_id="main",
        camera_id="main",
        role="main",
        media_types=("video", "audio"),
        items=(clip,),
    )
    return NleHandoffTimeline(
        project_id="mixed_rate_project",
        timebase=TICKS_PER_SECOND,
        frame_rate=project_rate,
        video_width=1920,
        video_height=1080,
        audio_sample_rate=48_000,
        duration_ticks=clip_duration_ticks,
        tracks=(track,),
    )


def build_fcpxml_forensic_timeline(
    tmp_path: Path,
    case: FcpxmlForensicCase,
) -> NleHandoffTimeline:
    """Build a time-only regression timeline for one production forensic case."""

    main_path = (tmp_path / f"{case.case_id}-main.mp4").absolute()
    aux_path = (tmp_path / f"{case.case_id}-aux.mp4").absolute()
    source_duration_ticks = 240_000_000

    def clip(
        logical_id: str,
        source_id: str,
        source_path: Path,
        source_range: tuple[int, int],
        timeline_range: tuple[int, int],
        camera_id: str,
        track_id: str,
    ) -> NleHandoffClip:
        return NleHandoffClip(
            logical_clip_id=logical_id,
            source_id=source_id,
            source_locator=source_path,
            source_duration_ticks=source_duration_ticks,
            source_display_name=source_path.stem,
            source_width=1920,
            source_height=1080,
            source_nominal_frame_rate=case.source_rate,
            source_is_vfr=False,
            source_audio_sample_rate=48_000,
            source_in_ticks=source_range[0],
            source_out_ticks=source_range[1],
            timeline_in_ticks=timeline_range[0],
            timeline_out_ticks=timeline_range[1],
            camera_id=camera_id,
            track_id=track_id,
            media_types=("video", "audio"),
            av_link_id=f"link_{logical_id}",
        )

    prefix_main: tuple[NleHandoffClip, ...] = ()
    prefix_aux: tuple[NleHandoffClip, ...] = ()
    if case.main_timeline[0] > 0:
        prefix_main = (
            clip(
                f"{case.case_id}_prefix_main",
                f"{case.case_id}_main_source",
                main_path,
                (0, case.main_timeline[0]),
                (0, case.main_timeline[0]),
                "main",
                "main",
            ),
        )
        prefix_aux = (
            clip(
                f"{case.case_id}_prefix_aux",
                f"{case.case_id}_aux_source",
                aux_path,
                (0, case.aux_timeline[0]),
                (0, case.aux_timeline[0]),
                "aux1",
                "aux_aux1",
            ),
        )
    main = prefix_main + (
        clip(
            f"{case.case_id}_main",
            f"{case.case_id}_main_source",
            main_path,
            case.main_source,
            case.main_timeline,
            "main",
            "main",
        ),
    )
    auxiliary = prefix_aux + (
        clip(
            f"{case.case_id}_aux",
            f"{case.case_id}_aux_source",
            aux_path,
            case.aux_source,
            case.aux_timeline,
            "aux1",
            "aux_aux1",
        ),
    )
    return NleHandoffTimeline(
        project_id=case.case_id,
        timebase=TICKS_PER_SECOND,
        frame_rate=case.project_rate,
        video_width=1920,
        video_height=1080,
        audio_sample_rate=48_000,
        duration_ticks=case.main_timeline[1],
        tracks=(
            NleHandoffTrack(
                track_id="main",
                camera_id="main",
                role="main",
                media_types=("video", "audio"),
                items=main,
            ),
            NleHandoffTrack(
                track_id="aux_aux1",
                camera_id="aux1",
                role="auxiliary",
                media_types=("video", "audio"),
                items=auxiliary,
            ),
        ),
    )


def build_fcpxml_8789_offset_derivative_timeline(tmp_path: Path) -> NleHandoffTimeline:
    """Build a labeled synthetic derivative for the 8789/50s warning value."""

    base = build_fcpxml_forensic_timeline(tmp_path, FCPXML_FORENSIC_CASES[0])
    source_shift_ticks = 1_156_800
    shifted_tracks = tuple(
        replace(
            track,
            items=tuple(
                replace(
                    item,
                    source_in_ticks=item.source_in_ticks + source_shift_ticks,
                    source_out_ticks=item.source_out_ticks + source_shift_ticks,
                )
                if isinstance(item, NleHandoffClip)
                else item
                for item in track.items
            ),
        )
        for track in base.tracks
    )
    return replace(
        base,
        project_id="synthetic_8789_offset_derivative",
        tracks=shifted_tracks,
    )


def build_partial_connected_child_timeline(tmp_path: Path) -> NleHandoffTimeline:
    """Build a child whose timeline start is rounded inside its main parent."""

    base = build_fcpxml_forensic_timeline(tmp_path, FCPXML_FORENSIC_CASES[0])
    parent = base.main_track.clips[-1]
    auxiliary_track = base.auxiliary_tracks[0]
    child = auxiliary_track.clips[-1]
    child_timeline_start = 2_600
    child_source_start = parent.source_in_ticks + 2_400
    child_timeline_end = parent.timeline_out_ticks
    shifted_child = replace(
        child,
        source_in_ticks=child_source_start,
        source_out_ticks=child_source_start + child_timeline_end - child_timeline_start,
        timeline_in_ticks=child_timeline_start,
        timeline_out_ticks=child_timeline_end,
    )
    gap = NleHandoffGap(
        "partial_child_gap",
        parent.timeline_in_ticks,
        child_timeline_start,
        auxiliary_track.camera_id,
        auxiliary_track.track_id,
    )
    return replace(
        base,
        tracks=(
            base.main_track,
            replace(auxiliary_track, items=(gap, shifted_child)),
        ),
    )


def build_many_clip_timeline(
    tmp_path: Path,
    *,
    clip_count: int = 159,
    clip_duration_ticks: int = 73_800,
    rate: RationalRate = DEFAULT_PROJECT_RATE,
    source_rate: RationalRate = DEFAULT_SOURCE_RATE,
) -> NleHandoffTimeline:
    """Build a many-clip timeline whose canonical durations are not project frames."""

    main_path = (tmp_path / "many-main.mp4").absolute()
    aux_path = (tmp_path / "many-aux.mp4").absolute()
    source_duration_ticks = 240_000_000
    main_items: list[NleHandoffClip] = []
    auxiliary_items: list[NleHandoffClip] = []
    timeline_cursor = 0
    for index in range(clip_count):
        timeline_end = timeline_cursor + clip_duration_ticks
        source_in = index * 240_000
        main_items.append(
            NleHandoffClip(
                logical_clip_id=f"many_main_{index:03d}",
                source_id="many_main_source",
                source_locator=main_path,
                source_duration_ticks=source_duration_ticks,
                source_display_name="many-main",
                source_width=1920,
                source_height=1080,
                source_nominal_frame_rate=source_rate,
                source_is_vfr=False,
                source_audio_sample_rate=48_000,
                source_in_ticks=source_in,
                source_out_ticks=source_in + clip_duration_ticks,
                timeline_in_ticks=timeline_cursor,
                timeline_out_ticks=timeline_end,
                camera_id="main",
                track_id="main",
                media_types=("video", "audio"),
                av_link_id=f"link_many_main_{index:03d}",
            )
        )
        aux_source_in = source_in + 7_200
        auxiliary_items.append(
            NleHandoffClip(
                logical_clip_id=f"many_aux_{index:03d}",
                source_id="many_aux_source",
                source_locator=aux_path,
                source_duration_ticks=source_duration_ticks,
                source_display_name="many-aux",
                source_width=1920,
                source_height=1080,
                source_nominal_frame_rate=source_rate,
                source_is_vfr=False,
                source_audio_sample_rate=48_000,
                source_in_ticks=aux_source_in,
                source_out_ticks=aux_source_in + clip_duration_ticks,
                timeline_in_ticks=timeline_cursor,
                timeline_out_ticks=timeline_end,
                camera_id="aux1",
                track_id="aux_aux1",
                media_types=("video", "audio"),
                av_link_id=f"link_many_aux_{index:03d}",
            )
        )
        timeline_cursor = timeline_end
    return NleHandoffTimeline(
        project_id="many_clip_project",
        timebase=TICKS_PER_SECOND,
        frame_rate=rate,
        video_width=1920,
        video_height=1080,
        audio_sample_rate=48_000,
        duration_ticks=timeline_cursor,
        tracks=(
            NleHandoffTrack(
                track_id="main",
                camera_id="main",
                role="main",
                media_types=("video", "audio"),
                items=tuple(main_items),
            ),
            NleHandoffTrack(
                track_id="aux_aux1",
                camera_id="aux1",
                role="auxiliary",
                media_types=("video", "audio"),
                items=tuple(auxiliary_items),
            ),
        ),
    )
