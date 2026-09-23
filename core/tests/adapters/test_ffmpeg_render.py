from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from roughcut.adapters.ffmpeg.render import (
    RenderCancelled,
    build_ffmpeg_command,
    build_filter_script,
    run_ffmpeg,
    ticks_to_seconds_text,
)
from roughcut.domain.bindings import SourceTranscriptBinding
from roughcut.domain.project import ImportMode, MediaProbe, SourceAsset, SourceFingerprint
from roughcut.domain.render import (
    MultiSourceRenderPlan,
    OutputSettings,
    RenderClip,
    RenderPlan,
    ToolResolution,
    derive_render_schedule,
)
from roughcut.domain.time import RationalRate


def _plan(*, audio_only: bool = False) -> RenderPlan:
    source = SourceAsset(
        source_id="src_render",
        kind="audio" if audio_only else "video",
        display_name="fixture.mp3" if audio_only else "fixture.mp4",
        import_mode=ImportMode.LINKED,
        locator={"absolute_path": "/fixture/media"},
        fingerprint=SourceFingerprint(1, 2, "a" * 64),
        probe=MediaProbe(
            duration_ticks=1_200_000,
            container_start_ticks=228_000,
            first_content_ticks=240_000,
            video_codec=None if audio_only else "h264",
            width=None if audio_only else 640,
            height=None if audio_only else 360,
            nominal_frame_rate=None if audio_only else {"numerator": 25, "denominator": 1},
            is_vfr=False,
            audio_codec="mp3" if audio_only else "aac",
            audio_sample_rate=48_000,
            rotation_degrees=0,
        ),
    )
    return RenderPlan(
        render_id="render_fixture",
        project_id="proj_fixture",
        project_revision=4,
        edit_version_id="edit_fixture",
        source=source,
        clips=(
            RenderClip("clip_b", "src_render", 120_000, 240_000),
            RenderClip("clip_a", "src_render", 360_000, 480_000),
        ),
        output_settings=OutputSettings(320, 180, RationalRate(25, 1), 48_000),
        ffmpeg=ToolResolution("ffmpeg", "/tools/ffmpeg", "ffmpeg version fixture"),
        ffprobe=ToolResolution("ffprobe", "/tools/ffprobe", "ffprobe version fixture"),
        plan_relative_path="renders/render_fixture.plan.json",
        output_relative_path="renders/render_fixture.mp4",
        manifest_relative_path="renders/render_fixture.manifest.json",
    )


def _multi_plan(*, all_silent: bool = False) -> MultiSourceRenderPlan:
    base = _plan().source
    audio_only = replace(
        base,
        source_id="src_audio",
        kind="audio",
        display_name="audio.mp3",
        locator={"absolute_path": "/fixture/audio"},
        probe=replace(
            base.probe,
            video_codec=None,
            width=None,
            height=None,
            nominal_frame_rate=None,
            audio_codec="mp3",
            rotation_degrees=0,
        ),
    )
    silent = replace(
        base,
        source_id="src_silent",
        display_name="silent.mp4",
        locator={"absolute_path": "/fixture/silent"},
        probe=replace(
            base.probe,
            audio_codec=None,
            audio_sample_rate=None,
            rotation_degrees=90,
        ),
    )
    first = replace(
        base,
        probe=replace(
            base.probe,
            audio_codec=None if all_silent else base.probe.audio_codec,
            audio_sample_rate=None if all_silent else base.probe.audio_sample_rate,
        ),
    )
    sources = (first, silent) if all_silent else (first, audio_only, silent)
    bindings = tuple(
        SourceTranscriptBinding(source.source_id, f"tr_{index}")
        for index, source in enumerate(sources)
    )
    clips = (
        (
            RenderClip("clip_a", "src_render", 120_000, 240_000),
            RenderClip("clip_silent", "src_silent", 360_000, 480_000),
        )
        if all_silent
        else (
            RenderClip("clip_a1", "src_render", 120_000, 240_000),
            RenderClip("clip_audio", "src_audio", 240_000, 360_000),
            RenderClip("clip_silent", "src_silent", 360_000, 480_000),
            RenderClip("clip_a2", "src_render", 480_000, 600_000),
        )
    )
    return MultiSourceRenderPlan(
        render_id="render_multi",
        project_id="proj_fixture",
        project_revision=4,
        edit_version_id="edit_multi",
        source_bindings=bindings,
        sources=sources,
        clips=clips,
        output_settings=OutputSettings(320, 180, RationalRate(25, 1), 48_000),
        ffmpeg=ToolResolution("ffmpeg", "/resolved/ffmpeg", "ffmpeg version fixture"),
        ffprobe=ToolResolution("ffprobe", "/tools/ffprobe", "ffprobe version fixture"),
        plan_relative_path="renders/render_multi.plan.json",
        output_relative_path="renders/render_multi.mp4",
        manifest_relative_path="renders/render_multi.manifest.json",
    )
def test_video_filter_enforces_each_global_frame_and_sample_quota() -> None:
    script = build_filter_script(_plan())

    assert "[0:v:0]setpts=PTS-STARTPTS" in script
    assert (
        "scale=320:180:force_original_aspect_ratio=decrease:"
        "in_range=auto:out_range=tv"
    ) in script
    assert "fps=fps=25/1:start_time=0:round=near:eof_action=pass" in script
    assert script.count("trim=end_frame=25") == 2
    assert script.count("atrim=end_sample=48000") == 2
    assert "format=yuv420p,setparams=range=limited" in script
    assert "concat=n=2:v=1:a=0[vout]" in script
    assert "concat=n=2:v=0:a=1[aout]" in script


def test_audio_only_filter_creates_one_plain_canvas_for_the_full_timeline() -> None:
    script = build_filter_script(_plan(audio_only=True))

    assert "color=c=black:s=320x180:r=25/1,trim=end_frame=50" in script
    assert "setpts=PTS-STARTPTS,setsar=1,format=yuv420p,setparams=range=limited[vout]" in script
    assert "atrim=end_sample=48000" in script
    assert "concat=n=2:v=0:a=1[aout]" in script


def test_audio_only_canvas_does_not_round_each_clip_to_a_video_frame() -> None:
    clips = tuple(
        RenderClip(f"clip_{index}", "src_render", index * 12_120, (index + 1) * 12_120)
        for index in range(54)
    )
    script = build_filter_script(replace(_plan(audio_only=True), clips=clips))

    assert "color=c=black:s=320x180:r=25/1,trim=end_frame=136" in script
    assert script.count("color=c=black") == 1
    assert "concat=n=54:v=0:a=1[aout]" in script


def test_multisource_filter_builds_clip_local_video_audio_black_and_silence_segments() -> None:
    script = build_filter_script(_multi_plan())

    assert "[0:v:0]setpts=PTS-STARTPTS" in script
    assert "in_range=auto:out_range=tv" in script
    assert "[0:a:0]asetpts=PTS-STARTPTS" in script
    assert "color=c=black:s=320x180:r=25/1,trim=end_frame=25" in script
    assert "format=yuv420p,setparams=range=limited[v1]" in script
    assert "[1:a:0]asetpts=PTS-STARTPTS" in script
    assert "[2:v:0]setpts=PTS-STARTPTS,transpose=clock" in script
    assert "anullsrc=r=48000:cl=stereo,atrim=end_sample=48000" in script
    assert "concat=n=4:v=1:a=0[vout]" in script
    assert "concat=n=4:v=0:a=1[aout]" in script


def test_multisource_all_silent_filter_and_command_do_not_create_audio(tmp_path: Path) -> None:
    plan = _multi_plan(all_silent=True)
    script = build_filter_script(plan)
    command = build_ffmpeg_command(
        plan,
        source_paths={
            "src_render": tmp_path / "A.mp4",
            "src_silent": tmp_path / "B.mp4",
        },
        filter_script_path=tmp_path / "filter.txt",
        output_path=tmp_path / "output.mp4",
    )

    assert "[aout]" not in script
    assert "anullsrc" not in script
    assert "-c:a" not in command
    assert command.count("-map") == 1


def test_multisource_command_binds_each_clip_to_its_own_original_path(tmp_path: Path) -> None:
    plan = _multi_plan()
    paths = {
        "src_render": tmp_path / "A original.mp4",
        "src_audio": tmp_path / "中文 B.mp3",
        "src_silent": tmp_path / "C silent.mp4",
    }

    command = build_ffmpeg_command(
        plan,
        source_paths=paths,
        filter_script_path=tmp_path / "filter.txt",
        output_path=tmp_path / "output.mp4",
    )

    inputs = [command[index + 1] for index, value in enumerate(command) if value == "-i"]
    assert inputs == [
        str(paths["src_render"]),
        str(paths["src_audio"]),
        str(paths["src_silent"]),
        str(paths["src_render"]),
    ]
    assert command.count("-ss") == 4
    assert command.count("-t") == 4
    assert command.count("-noautorotate") == 3


def test_ffmpeg_command_is_an_argument_array_with_software_codecs_and_faststart(
    tmp_path: Path,
) -> None:
    plan = replace(
        _plan(),
        ffmpeg=ToolResolution("ffmpeg", "/resolved/ffmpeg", "ffmpeg version fixture"),
    )
    command = build_ffmpeg_command(
        plan,
        source_path=tmp_path / "源 media.mp4",
        filter_script_path=tmp_path / "filter script.txt",
        output_path=tmp_path / "output file.mp4",
    )

    assert command[0] == "/resolved/ffmpeg"
    assert "-copyts" not in command
    assert command.count("-ss") == 2
    assert command.count("-t") == 2
    assert command.count("-noautorotate") == 2
    assert command.count("-i") == 2
    option_index = command.index("-/filter_complex")
    assert command[option_index : option_index + 2] == [
        "-/filter_complex",
        str(tmp_path / "filter script.txt"),
    ]
    assert "-filter_complex_script" not in command
    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-color_range") + 1] == "tv"
    assert command[command.index("-c:a") + 1] == "aac"
    assert command[command.index("-movflags") + 1] == "+faststart"
    assert command[command.index("-frames:v") + 1] == "50"
    assert command[-1].endswith("output file.mp4")


def test_distant_video_clips_use_bounded_clip_local_accurate_seek_inputs(
    tmp_path: Path,
) -> None:
    plan = replace(
        _plan(),
        clips=(
            RenderClip("clip_end", "src_render", 1_080_000, 1_140_000),
            RenderClip("clip_start", "src_render", 120_000, 180_000),
            RenderClip("clip_middle", "src_render", 600_000, 660_000),
        ),
        ffmpeg=ToolResolution("ffmpeg", "/resolved/ffmpeg", "ffmpeg version fixture"),
    )
    schedule = derive_render_schedule(plan)

    command = build_ffmpeg_command(
        plan,
        schedule=schedule,
        source_path=tmp_path / "long source.mp4",
        filter_script_path=tmp_path / "filter.txt",
        output_path=tmp_path / "output.mp4",
    )
    script = build_filter_script(plan, schedule=schedule)

    assert command.count("-i") == 3
    input_indexes = [index for index, value in enumerate(command) if value == "-i"]
    for input_index, scheduled_clip in zip(input_indexes, schedule.clips, strict=True):
        input_options = command[max(0, input_index - 7) : input_index]
        assert input_options[input_options.index("-ss") + 1] == ticks_to_seconds_text(
            scheduled_clip.access_start_ticks
        )
        assert input_options[input_options.index("-t") + 1] == ticks_to_seconds_text(
            scheduled_clip.access_duration_ticks
        )
    assert "split=" not in script
    assert "trim=end_frame=" in script
    assert "atrim=end_sample=" in script
    assert "concat=n=3:v=1:a=0[vout]" in script
    assert "concat=n=3:v=0:a=1[aout]" in script


class _WaitingProcess:
    returncode: int | None = None
    terminated = False

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        if self.returncode is None:
            raise __import__("subprocess").TimeoutExpired("ffmpeg", timeout)
        return "", ""

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -1

    def wait(self, timeout: float | None = None) -> int:
        assert timeout is not None
        self.returncode = -1
        return -1

    def kill(self) -> None:
        self.returncode = -9


def test_cancel_terminates_the_ffmpeg_process() -> None:
    process = _WaitingProcess()

    with pytest.raises(RenderCancelled):
        run_ffmpeg(
            ["ffmpeg", "fixture"],
            cancel_requested=lambda: True,
            process_factory=lambda *_args, **_kwargs: process,
        )

    assert process.terminated is True


def test_keyboard_interrupt_terminates_the_ffmpeg_process() -> None:
    class InterruptedProcess(_WaitingProcess):
        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            raise KeyboardInterrupt

    process = InterruptedProcess()

    with pytest.raises(RenderCancelled, match="interrupted"):
        run_ffmpeg(
            ["ffmpeg", "fixture"],
            process_factory=lambda *_args, **_kwargs: process,
        )

    assert process.terminated is True
