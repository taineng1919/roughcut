from __future__ import annotations

import json
import shutil
import subprocess
from array import array
from dataclasses import replace
from itertools import pairwise
from pathlib import Path

import pytest

from roughcut.adapters.artifact_store import write_new_json
from roughcut.adapters.project_store import ProjectStore
from roughcut.application.projects import create_project
from roughcut.application.renders import render_roughcut
from roughcut.application.sources import add_source, fingerprint_file
from roughcut.domain.bindings import SourceTranscriptBinding
from roughcut.domain.brief import EditBrief
from roughcut.domain.edit import (
    EditClip,
    EditDecision,
    EditProposal,
    MultiSourceEditDecision,
    MultiSourceEditProposal,
)
from roughcut.domain.project import ImportMode

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
TICKS = 120_000
SEGMENT_COLORS = ("red", "green", "blue", "yellow", "magenta")
SEGMENT_FREQUENCIES = (440, 550, 660, 770, 880)
pytestmark = pytest.mark.usefixtures("synthetic_media_runtime")


def _require_tools() -> tuple[str, str]:
    if FFMPEG is None or FFPROBE is None:
        pytest.skip("ffmpeg and ffprobe are required for render integration tests")
    return FFMPEG, FFPROBE


def _run(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(command, check=False, capture_output=True)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result


def _make_marked_av_source(path: Path) -> None:
    ffmpeg, _ffprobe = _require_tools()
    command = [ffmpeg, "-hide_banner", "-loglevel", "error"]
    for color, frequency in zip(SEGMENT_COLORS, SEGMENT_FREQUENCIES, strict=True):
        command.extend(["-f", "lavfi", "-i", f"color=c={color}:s=160x90:r=25:d=1"])
        command.extend(
            ["-f", "lavfi", "-i", f"sine=f={frequency}:sample_rate=48000:d=1"]
        )
    inputs = "".join(f"[{index * 2}:v][{index * 2 + 1}:a]" for index in range(5))
    command.extend(
        [
            "-filter_complex",
            f"{inputs}concat=n=5:v=1:a=1[v][a]",
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-movflags",
            "+faststart",
            str(path),
        ]
    )
    _run(command)


def _make_delayed_transport_source(path: Path, base_path: Path) -> None:
    ffmpeg, _ffprobe = _require_tools()
    _make_marked_av_source(base_path)
    _run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(base_path),
            "-map",
            "0",
            "-c",
            "copy",
            "-output_ts_offset",
            "2",
            "-f",
            "mpegts",
            str(path),
        ]
    )


def _make_marked_mp3(path: Path) -> None:
    ffmpeg, _ffprobe = _require_tools()
    command = [ffmpeg, "-hide_banner", "-loglevel", "error"]
    for frequency in SEGMENT_FREQUENCIES:
        command.extend(
            ["-f", "lavfi", "-i", f"sine=f={frequency}:sample_rate=48000:d=1"]
        )
    inputs = "".join(f"[{index}:a]" for index in range(5))
    command.extend(
        [
            "-filter_complex",
            f"{inputs}concat=n=5:v=0:a=1[a]",
            "-map",
            "[a]",
            "-c:a",
            "libmp3lame",
            "-ar",
            "48000",
            str(path),
        ]
    )
    _run(command)


def _make_constant_av(
    path: Path,
    *,
    color: str,
    frequency: int,
    size: str = "160x90",
    rate: int = 25,
    duration: float = 4,
) -> None:
    ffmpeg, _ffprobe = _require_tools()
    _run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s={size}:r={rate}:d={duration}",
            "-f",
            "lavfi",
            "-i",
            f"sine=f={frequency}:sample_rate=48000:d={duration}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-shortest",
            str(path),
        ]
    )


def _make_full_range_av(path: Path, *, seconds: int = 2) -> None:
    ffmpeg, _ffprobe = _require_tools()
    _run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=25",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            str(seconds),
            "-vf",
            "format=yuv422p10le,setparams=range=full",
            "-c:v",
            "ffv1",
            "-level",
            "3",
            "-pix_fmt",
            "yuv422p10le",
            "-color_range",
            "pc",
            "-c:a",
            "pcm_s16le",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-shortest",
            "-y",
            str(path),
        ]
    )


def _make_silent_video(
    path: Path, *, color: str, size: str = "160x90", rate: int = 25
) -> None:
    ffmpeg, _ffprobe = _require_tools()
    _run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s={size}:r={rate}:d=4",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ]
    )


def _confirmed_multi_project(
    project_path: Path,
    source_paths: tuple[Path, ...],
    *,
    clip_sources: tuple[int, ...],
) -> tuple[str, int]:
    project = create_project(project_path, "Multi render integration")
    imported = project
    for source_path in source_paths:
        imported = add_source(
            project_path,
            source_path,
            ImportMode.LINKED,
            expected_revision=imported.revision,
        )
    bindings = tuple(
        SourceTranscriptBinding(source.source_id, f"tr_{index}")
        for index, source in enumerate(imported.sources)
    )
    clips = tuple(
        EditClip(
            f"clip_{index}",
            imported.sources[source_index].source_id,
            bindings[source_index].transcript_version_id,
            f"seg_{index}",
            48_000 + index * 120_000,
            120_000 + index * 120_000,
            "synthetic order marker",
            f"marker {index}",
        )
        for index, source_index in enumerate(clip_sources)
    )
    brief = EditBrief(
        "brief_multi_render",
        "multi markers",
        sum(clip.duration_ticks for clip in clips),
        ("order",),
        True,
    )
    proposal = MultiSourceEditProposal(
        "proposal_multi_render",
        imported.revision,
        None,
        bindings,
        brief,
        "c" * 64,
        clips,
        sum(clip.duration_ticks for clip in clips),
        "fixture",
    )
    decision = MultiSourceEditDecision(
        "edit_multi_render", proposal, imported.revision + 1, "fixture"
    )
    write_new_json(
        project_path / "edits" / "edit_multi_render.json", decision.to_dict()
    )
    updated = replace(
        imported,
        revision=decision.project_revision,
        settings={**imported.settings, "width": 160, "height": 90},
        active_transcript_versions={
            binding.source_id: binding.transcript_version_id for binding in bindings
        },
        active_edit_version_id=decision.edit_version_id,
    )
    ProjectStore(project_path).save(updated, expected_revision=imported.revision)
    return decision.edit_version_id, updated.revision


def _confirmed_project(
    project_path: Path,
    source_path: Path,
    *,
    ranges: tuple[tuple[str, str, int, int], ...] | None = None,
) -> tuple[str, int]:
    project = create_project(project_path, "Render integration")
    imported = add_source(
        project_path,
        source_path,
        ImportMode.LINKED,
        expected_revision=project.revision,
    )
    source = imported.sources[0]
    brief = EditBrief(
        brief_id="brief_render",
        theme="signal order",
        target_duration_ticks=216_000,
        focus=("markers",),
        allow_reorder=True,
    )
    if ranges is None:
        ranges = (
            ("clip_five", "seg_five", 492_000, 564_000),
            ("clip_one", "seg_one", 12_000, 84_000),
            ("clip_three", "seg_three", 252_000, 324_000),
        )
    clips = tuple(
        EditClip(
            clip_id,
            source.source_id,
            "tr_render",
            segment_id,
            source_in,
            source_out,
            "deterministic marker",
            f"marker {clip_id}",
        )
        for clip_id, segment_id, source_in, source_out in ranges
    )
    proposal = EditProposal(
        proposal_id="proposal_render",
        base_project_revision=imported.revision,
        base_edit_version_id=None,
        source_id=source.source_id,
        transcript_version_id="tr_render",
        brief_snapshot=brief,
        context_hash="b" * 64,
        clips=clips,
        total_duration_ticks=sum(clip.duration_ticks for clip in clips),
        created_at="fixture",
    )
    decision = EditDecision(
        edit_version_id="edit_render",
        proposal_snapshot=proposal,
        project_revision=imported.revision + 1,
        created_at="fixture",
    )
    write_new_json(project_path / "edits" / "edit_render.json", decision.to_dict())
    settings = {**imported.settings, "width": 160, "height": 90}
    updated = replace(
        imported,
        revision=decision.project_revision,
        settings=settings,
        active_edit_version_id=decision.edit_version_id,
    )
    ProjectStore(project_path).save(updated, expected_revision=imported.revision)
    return decision.edit_version_id, updated.revision


def _frame_rgb(path: Path, output_seconds: str) -> tuple[int, int, int]:
    ffmpeg, _ffprobe = _require_tools()
    raw = _run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            output_seconds,
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            "scale=1:1",
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "-",
        ]
    ).stdout
    assert len(raw) == 3
    return raw[0], raw[1], raw[2]


def _audio_frequency(
    path: Path, output_seconds: str, *, duration_seconds: str = "0.2"
) -> float:
    ffmpeg, _ffprobe = _require_tools()
    raw = _run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            output_seconds,
            "-t",
            duration_seconds,
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-c:a",
            "pcm_s16le",
            "-f",
            "s16le",
            "-",
        ]
    ).stdout
    samples = array("h")
    samples.frombytes(raw)
    crossings = sum(
        1
        for left, right in pairwise(samples)
        if (left < 0 <= right) or (left >= 0 > right)
    )
    duration = len(samples) / 48_000
    return crossings / (2 * duration)


def _audio_peak(path: Path, output_seconds: str) -> int:
    ffmpeg, _ffprobe = _require_tools()
    raw = _run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            output_seconds,
            "-t",
            "0.2",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-c:a",
            "pcm_s16le",
            "-f",
            "s16le",
            "-",
        ]
    ).stdout
    samples = array("h")
    samples.frombytes(raw)
    return max((abs(sample) for sample in samples), default=0)


def _audio_stream_count(path: Path) -> int:
    _ffmpeg, ffprobe = _require_tools()
    result = _run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            str(path),
        ]
    )
    return len(result.stdout.splitlines())


def _assert_reordered_markers(output: Path) -> None:
    magenta = _frame_rgb(output, "0.30")
    red = _frame_rgb(output, "0.90")
    blue = _frame_rgb(output, "1.50")
    assert magenta[0] > 140 and magenta[2] > 140 and magenta[1] < 100
    assert red[0] > 140 and red[1] < 100 and red[2] < 100
    assert blue[2] > 140 and blue[0] < 100 and blue[1] < 100
    for time, expected in (("0.20", 880), ("0.80", 440), ("1.40", 660)):
        assert _audio_frequency(output, time) == pytest.approx(expected, abs=35)


def test_renders_three_reordered_av_clips_from_nonzero_content_pts(tmp_path: Path) -> None:
    source = tmp_path / "delayed.ts"
    _make_delayed_transport_source(source, tmp_path / "base.mp4")
    project_path = tmp_path / "project"
    edit_id, revision = _confirmed_project(project_path, source)
    project = ProjectStore(project_path).load()
    assert project.sources[0].probe.first_content_ticks > 0

    result = render_roughcut(
        project_path,
        edit_version_id=edit_id,
        expected_revision=revision,
    )

    output = project_path / result.mp4_path
    _assert_reordered_markers(output)
    manifest = json.loads((project_path / result.manifest_path).read_text(encoding="utf-8"))
    assert manifest["acceptance"]["accepted"] is True
    assert "starts_at_zero" not in manifest["acceptance"]["checks"]
    assert manifest["input_source"]["probe"]["first_content_ticks"] > 0
    assert [clip["clip_id"] for clip in manifest["clips"]] == [
        "clip_five",
        "clip_one",
        "clip_three",
    ]


def test_full_range_source_renders_limited_yuv420p_through_production_path(
    tmp_path: Path,
) -> None:
    source = tmp_path / "full-range.mkv"
    _make_full_range_av(source)
    source_before = fingerprint_file(source)
    project_path = tmp_path / "full-range-project"
    edit_id, revision = _confirmed_project(
        project_path,
        source,
        ranges=(
            ("clip_first", "seg_first", 12_000, 84_000),
            ("clip_middle", "seg_middle", 96_000, 168_000),
            ("clip_last", "seg_last", 180_000, 228_000),
        ),
    )
    _ffmpeg, ffprobe = _require_tools()

    def probe(path: Path) -> dict[str, object]:
        return json.loads(
            subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-count_frames",
                    "-show_streams",
                    "-show_format",
                    "-of",
                    "json",
                    str(path),
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )

    input_probe = probe(source)
    input_video = next(
        stream for stream in input_probe["streams"] if stream["codec_type"] == "video"
    )
    input_audio = next(
        stream for stream in input_probe["streams"] if stream["codec_type"] == "audio"
    )
    assert input_video["pix_fmt"] == "yuv422p10le"
    assert input_video["color_range"] == "pc"
    assert input_audio["sample_rate"] == "48000"
    assert input_audio["channels"] == 2

    result = render_roughcut(
        project_path,
        edit_version_id=edit_id,
        expected_revision=revision,
    )
    output = project_path / result.mp4_path
    manifest = json.loads(
        (project_path / result.manifest_path).read_text(encoding="utf-8")
    )
    output_probe = probe(output)
    video = next(
        stream for stream in output_probe["streams"] if stream["codec_type"] == "video"
    )
    audio = next(
        stream for stream in output_probe["streams"] if stream["codec_type"] == "audio"
    )
    schedule = manifest["render_schedule"]
    output_probe_summary = manifest["output"]["probe"]
    output_audio_summary = output_probe_summary["audio"]

    assert result.acceptance and all(result.acceptance.values())
    assert manifest["schema_version"] == 2
    assert manifest["acceptance"]["accepted"] is True
    assert all(manifest["acceptance"]["checks"].values())
    assert manifest["acceptance"]["checks"]["pixel_format_yuv420p"] is True
    assert manifest["acceptance"]["checks"]["faststart"] is True
    assert video["codec_name"] == "h264"
    assert video["pix_fmt"] == "yuv420p"
    assert video["pix_fmt"] != "yuvj420p"
    if "color_range" in video:
        assert video["color_range"] in {"tv", "limited"}
    assert video["avg_frame_rate"] == "25/1"
    assert int(video["nb_read_frames"]) == schedule["total_frames"]
    assert audio["codec_name"] == "aac"
    assert audio["sample_rate"] == "48000"
    assert audio["channels"] == 2
    assert output_audio_summary["timeline_sample_count"] == schedule["total_samples"]
    assert (
        abs(output_audio_summary["decoded_sample_count"] - schedule["total_samples"])
        <= 1024
    )
    assert not any(path.is_dir() for path in (project_path / "renders").iterdir())
    assert fingerprint_file(source) == source_before


def test_audio_only_source_renders_black_h264_canvas_and_reordered_aac(tmp_path: Path) -> None:
    source = tmp_path / "marked.mp3"
    _make_marked_mp3(source)
    project_path = tmp_path / "audio project"
    edit_id, revision = _confirmed_project(project_path, source)

    result = render_roughcut(
        project_path,
        edit_version_id=edit_id,
        expected_revision=revision,
    )

    output = project_path / result.mp4_path
    red, green, blue = _frame_rgb(output, "0.30")
    assert max(red, green, blue) < 20
    for time, expected in (("0.20", 880), ("0.80", 440), ("1.40", 660)):
        assert _audio_frequency(output, time) == pytest.approx(expected, abs=35)
    manifest = json.loads((project_path / result.manifest_path).read_text(encoding="utf-8"))
    assert manifest["output"]["probe"]["video"]["codec_name"] == "h264"
    assert manifest["output"]["probe"]["audio"]["codec_name"] == "aac"


def test_multisource_a_b_a_renders_real_color_audio_order_and_schema_three(
    tmp_path: Path,
) -> None:
    source_a = tmp_path / "A source.mp4"
    source_b = tmp_path / "中文 B source.mp4"
    _make_constant_av(source_a, color="red", frequency=440)
    _make_constant_av(
        source_b,
        color="blue",
        frequency=660,
        size="240x136",
        rate=30,
    )
    project_path = tmp_path / "multi A B A project"
    edit_id, revision = _confirmed_multi_project(
        project_path, (source_a, source_b), clip_sources=(0, 1, 0)
    )
    imported = ProjectStore(project_path).load()
    assert imported.sources[0].probe.nominal_frame_rate != imported.sources[1].probe.nominal_frame_rate

    result = render_roughcut(project_path, edit_version_id=edit_id, expected_revision=revision)

    output = project_path / result.mp4_path
    for time, expected_color, expected_frequency in (
        ("0.30", "red", 440),
        ("0.90", "blue", 660),
        ("1.50", "red", 440),
    ):
        red, green, blue = _frame_rgb(output, time)
        if expected_color == "red":
            assert red > 140 and green < 100 and blue < 100
        else:
            assert blue > 140 and red < 100 and green < 100
        assert _audio_frequency(output, time) == pytest.approx(expected_frequency, abs=35)
    for time, expected_channel, expected_frequency in (
        ("0.56", 0, 440),
        ("0.64", 2, 660),
        ("1.16", 2, 660),
        ("1.24", 0, 440),
    ):
        rgb = _frame_rgb(output, time)
        assert rgb[expected_channel] > 140
        assert _audio_frequency(
            output, time, duration_seconds="0.04"
        ) == pytest.approx(expected_frequency, abs=45)
    manifest_text = (project_path / result.manifest_path).read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert manifest["schema_version"] == 3
    assert [clip["source_id"] for clip in manifest["clips"]] == [
        manifest["input_sources"][0]["source_id"],
        manifest["input_sources"][1]["source_id"],
        manifest["input_sources"][0]["source_id"],
    ]
    assert manifest["render_schedule"]["total_frames"] == 45
    assert manifest["render_schedule"]["total_samples"] == 86_400
    assert manifest["acceptance"]["checks"]["starts_at_zero"] is True
    assert all(
        clip["access_duration_ticks"] <= 76_800
        for clip in manifest["render_schedule"]["clips"]
    )
    assert str(source_a.resolve()) not in manifest_text
    assert str(source_b.resolve()) not in manifest_text
    assert "locator" not in manifest_text


def test_multisource_video_plus_audio_only_uses_black_canvas_and_real_audio(
    tmp_path: Path,
) -> None:
    video = tmp_path / "video.mp4"
    audio = tmp_path / "中文 audio.mp3"
    _make_constant_av(video, color="red", frequency=440)
    _make_marked_mp3(audio)
    project_path = tmp_path / "video audio project"
    edit_id, revision = _confirmed_multi_project(
        project_path, (video, audio), clip_sources=(0, 1)
    )

    result = render_roughcut(project_path, edit_version_id=edit_id, expected_revision=revision)

    output = project_path / result.mp4_path
    red, green, blue = _frame_rgb(output, "0.30")
    assert red > 140 and green < 100 and blue < 100
    assert max(_frame_rgb(output, "0.90")) < 20
    assert _audio_frequency(output, "0.30") == pytest.approx(440, abs=35)
    assert _audio_frequency(output, "0.90") == pytest.approx(550, abs=35)


def test_multisource_av_plus_silent_video_inserts_only_clip_local_silence(
    tmp_path: Path,
) -> None:
    av = tmp_path / "av.mp4"
    silent = tmp_path / "silent.mp4"
    _make_constant_av(av, color="red", frequency=440)
    _make_silent_video(silent, color="green", size="200x100", rate=30)
    project_path = tmp_path / "av silent project"
    edit_id, revision = _confirmed_multi_project(
        project_path, (av, silent), clip_sources=(0, 1)
    )

    result = render_roughcut(project_path, edit_version_id=edit_id, expected_revision=revision)

    output = project_path / result.mp4_path
    assert _audio_peak(output, "0.30") > 500
    assert _audio_peak(output, "0.90") < 50
    green = _frame_rgb(output, "0.90")
    assert green[1] > 60 and green[0] < 100 and green[2] < 100


def test_multisource_all_silent_videos_publish_without_audio_track(tmp_path: Path) -> None:
    first = tmp_path / "silent A.mp4"
    second_base = tmp_path / "静音 B base.mp4"
    second = tmp_path / "静音 B rotated.mp4"
    _make_silent_video(first, color="red")
    _make_silent_video(second_base, color="blue", size="240x136", rate=30)
    ffmpeg, _ffprobe = _require_tools()
    _run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-display_rotation:v:0",
            "90",
            "-i",
            str(second_base),
            "-c",
            "copy",
            str(second),
        ]
    )
    project_path = tmp_path / "all silent project"
    edit_id, revision = _confirmed_multi_project(
        project_path, (first, second), clip_sources=(0, 1, 0)
    )
    assert ProjectStore(project_path).load().sources[1].probe.rotation_degrees == 90

    result = render_roughcut(project_path, edit_version_id=edit_id, expected_revision=revision)

    output = project_path / result.mp4_path
    assert _audio_stream_count(output) == 0
    manifest = json.loads((project_path / result.manifest_path).read_text(encoding="utf-8"))
    assert manifest["command_summary"]["audio_encoder"] is None
    assert manifest["output"]["probe"]["audio"] is None


def test_long_source_distant_clips_use_sparse_windows_and_global_quotas(tmp_path: Path) -> None:
    source = tmp_path / "long-marked.mp4"
    _make_long_marked_source(source)
    project_path = tmp_path / "long-project"
    ranges = (
        ("clip_end", "seg_end", 14_412_000, 14_484_000),
        ("clip_start", "seg_start", 132_000, 204_000),
        ("clip_middle", "seg_middle", 7_212_000, 7_284_000),
    )
    edit_id, revision = _confirmed_project(
        project_path,
        source,
        ranges=ranges,
    )

    result = render_roughcut(
        project_path,
        edit_version_id=edit_id,
        expected_revision=revision,
    )

    output = project_path / result.mp4_path
    green = _frame_rgb(output, "0.30")
    red = _frame_rgb(output, "0.90")
    blue = _frame_rgb(output, "1.50")
    assert green[1] > 60 and green[0] < 100 and green[2] < 100
    assert red[0] > 140 and red[1] < 100 and red[2] < 100
    assert blue[2] > 140 and blue[0] < 100 and blue[1] < 100
    for time, expected in (("0.20", 880), ("0.80", 440), ("1.40", 660)):
        assert _audio_frequency(output, time) == pytest.approx(expected, abs=35)

    manifest = json.loads((project_path / result.manifest_path).read_text(encoding="utf-8"))
    schedule = manifest["render_schedule"]
    assert schedule["strategy"] == "clip_local_accurate_seek"
    assert schedule["total_frames"] == 45
    assert schedule["total_samples"] == 86_400
    assert [clip["clip_id"] for clip in schedule["clips"]] == [
        "clip_end",
        "clip_start",
        "clip_middle",
    ]
    assert all(
        clip["access_end_ticks"] - clip["access_start_ticks"] <= 76_800
        for clip in schedule["clips"]
    )
    assert manifest["output"]["probe"]["video"]["frame_count"] == 45
    assert manifest["output"]["probe"]["audio"]["timeline_sample_count"] == 86_400


def _make_long_marked_source(path: Path) -> None:
    ffmpeg, _ffprobe = _require_tools()
    command = [ffmpeg, "-hide_banner", "-loglevel", "error"]
    for color, frequency in (("red", 440), ("blue", 660), ("green", 880)):
        command.extend(["-f", "lavfi", "-i", f"color=c={color}:s=160x90:r=25:d=60"])
        command.extend(
            ["-f", "lavfi", "-i", f"sine=f={frequency}:sample_rate=48000:d=60"]
        )
    inputs = "".join(f"[{index * 2}:v][{index * 2 + 1}:a]" for index in range(3))
    command.extend(
        [
            "-filter_complex",
            f"{inputs}concat=n=3:v=1:a=1[v][a]",
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-g",
            "250",
            "-keyint_min",
            "250",
            "-sc_threshold",
            "0",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-movflags",
            "+faststart",
            str(path),
        ]
    )
    _run(command)
