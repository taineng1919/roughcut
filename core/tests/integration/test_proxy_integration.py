from __future__ import annotations

import json
import math
import subprocess
import time
from array import array
from pathlib import Path

import pytest

from roughcut.adapters.ffmpeg.proxy import ProxyUnsupported
from roughcut.application.projects import create_project
from roughcut.application.proxies import create_proxy, read_proxy
from roughcut.application.sources import add_source
from roughcut.domain.project import ImportMode

pytestmark = pytest.mark.usefixtures("synthetic_media_runtime")


def _ffmpeg(*arguments: str) -> None:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if result.returncode:
        raise AssertionError(result.stderr)


def _dominant_channel(path: Path, seconds: str, *, proxy: bool) -> int:
    video_filter = (
        "crop=160:90:(iw-160)/2:(ih-90)/2,scale=1:1,format=rgb24"
        if proxy
        else "scale=1:1,format=rgb24"
    )
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            seconds,
            "-i",
            str(path),
            "-vf",
            video_filter,
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0 and len(result.stdout) == 3
    return max(range(3), key=result.stdout.__getitem__)


def _pixel(path: Path, seconds: str) -> tuple[int, int, int]:
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            seconds,
            "-i",
            str(path),
            "-vf",
            "crop=160:90:(iw-160)/2:(ih-90)/2,scale=1:1,format=rgb24",
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0 and len(result.stdout) == 3
    return tuple(result.stdout)  # type: ignore[return-value]


def _audio_rms(path: Path, seconds: str) -> float:
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            seconds,
            "-i",
            str(path),
            "-t",
            "0.08",
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-f",
            "f32le",
            "-",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0
    samples = array("f", result.stdout)
    assert samples
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples))


def _project_with_source(tmp_path: Path, source_path: Path):  # type: ignore[no-untyped-def]
    project_path = tmp_path / "代理 项目"
    project = create_project(project_path, "Proxy integration")
    project = add_source(
        project_path,
        source_path,
        ImportMode.LINKED,
        expected_revision=project.revision,
    )
    return project_path, project, project.sources[0]


def _make_matrix_source(tmp_path: Path, case: str) -> Path:
    source = tmp_path / f"中文 {case} source.mp4"
    if case == "audio-only":
        source = source.with_suffix(".wav")
        _ffmpeg(
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=330:duration=0.52",
            "-c:a",
            "pcm_s16le",
            str(source),
        )
    elif case == "video-only":
        _ffmpeg(
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=25:duration=0.52",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        )
    elif case == "duration-mismatch":
        _ffmpeg(
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=25:duration=0.72",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=550:duration=0.36",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source),
        )
    elif case == "4k":
        _ffmpeg(
            "-f",
            "lavfi",
            "-i",
            "color=blue:size=3840x2160:rate=25:duration=0.12",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        )
    elif case == "vfr":
        _ffmpeg(
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=30:duration=0.8",
            "-vf",
            "select='if(lt(t,0.4),not(mod(n,2)),not(mod(n,3)))'",
            "-fps_mode",
            "vfr",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        )
    elif case == "nonzero-pts":
        _ffmpeg(
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=25:duration=0.52",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=660:duration=0.52",
            "-filter_complex",
            "[0:v]setpts=PTS+1/TB[v];[1:a]asetpts=PTS+1/TB[a]",
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
            str(source),
        )
    elif case in {"rotate-90", "rotate-270"}:
        base = tmp_path / "rotation-base.mp4"
        _ffmpeg(
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=25:duration=0.52",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(base),
        )
        degrees = case.removeprefix("rotate-")
        _ffmpeg("-i", str(base), "-c", "copy", "-metadata:s:v:0", f"rotate={degrees}", str(source))
    else:
        raise AssertionError(case)
    return source


def test_real_ffmpeg_proxy_is_verified_and_second_call_is_zero_transcode(
    tmp_path: Path,
) -> None:
    source = tmp_path / "中文 source with spaces.mp4"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x180:rate=25:duration=1.04",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000:duration=1.04",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(source),
    )
    project_path, project, asset = _project_with_source(tmp_path, source)

    created = create_proxy(
        project_path, source_id=asset.source_id, expected_revision=project.revision
    )
    output = project_path / created.state.proxy_relative_path
    before = output.stat().st_mtime_ns
    reused = create_proxy(
        project_path, source_id=asset.source_id, expected_revision=project.revision
    )

    assert created.state.status == reused.state.status == "ready"
    assert reused.reused is True
    assert output.stat().st_mtime_ns == before
    assert (
        read_proxy(
            project_path,
            source_id=asset.source_id,
            expected_revision=project.revision,
        ).status
        == "ready"
    )


@pytest.mark.parametrize(
    "case",
    [
        "audio-only",
        "video-only",
        "duration-mismatch",
        "4k",
        "vfr",
        "nonzero-pts",
        "rotate-90",
        "rotate-270",
    ],
)
def test_real_ffmpeg_proxy_synthetic_media_matrix(tmp_path: Path, case: str) -> None:
    source = _make_matrix_source(tmp_path, case)
    project_path, project, asset = _project_with_source(tmp_path, source)

    result = create_proxy(
        project_path, source_id=asset.source_id, expected_revision=project.revision
    )

    assert result.state.status == "ready"
    assert result.state.summary is not None
    assert result.state.summary["has_audio"] is (
        case not in {"video-only", "4k", "vfr", "rotate-90", "rotate-270"}
    )
    if case == "4k":
        assert result.state.summary["canvas"] == {"width": 1280, "height": 720}
    if case == "duration-mismatch":
        manifest = json.loads(
            (project_path / result.state.manifest_relative_path).read_text(encoding="utf-8")
        )
        assert manifest["padding"]["audio_samples"] > 0


def test_proxy_start_middle_end_and_color_boundaries_map_within_one_frame(
    tmp_path: Path,
) -> None:
    source = tmp_path / "time map colors.mp4"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=red:size=160x90:rate=25:duration=0.4",
        "-f",
        "lavfi",
        "-i",
        "color=lime:size=160x90:rate=25:duration=0.4",
        "-f",
        "lavfi",
        "-i",
        "color=blue:size=160x90:rate=25:duration=0.4",
        "-filter_complex",
        "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
        "-map",
        "[v]",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(source),
    )
    project_path, project, asset = _project_with_source(tmp_path, source)
    result = create_proxy(
        project_path, source_id=asset.source_id, expected_revision=project.revision
    )
    proxy = project_path / result.state.proxy_relative_path

    for seconds, expected in (
        ("0.08", 0),
        ("0.36", 0),
        ("0.44", 1),
        ("0.60", 1),
        ("0.76", 1),
        ("0.84", 2),
        ("1.08", 2),
    ):
        assert _dominant_channel(source, seconds, proxy=False) == expected
        assert _dominant_channel(proxy, seconds, proxy=True) == expected


@pytest.mark.parametrize(
    ("video_offset", "audio_offset", "expect_black", "expect_silence"),
    [
        ("0", "0.4", False, True),
        ("0.4", "0", True, False),
        ("1", "1", False, False),
    ],
)
def test_proxy_preserves_relative_stream_start_offsets(
    tmp_path: Path,
    video_offset: str,
    audio_offset: str,
    expect_black: bool,
    expect_silence: bool,
) -> None:
    source = tmp_path / f"offset-{video_offset}-{audio_offset}.mov"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=red:size=160x90:rate=25:duration=1",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=660:sample_rate=48000:duration=1",
        "-filter_complex",
        f"[0:v]setpts=PTS+{video_offset}/TB[v];[1:a]asetpts=PTS+{audio_offset}/TB[a]",
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "pcm_s16le",
        str(source),
    )
    project_path, project, asset = _project_with_source(tmp_path, source)
    result = create_proxy(
        project_path, source_id=asset.source_id, expected_revision=project.revision
    )
    proxy = project_path / result.state.proxy_relative_path
    manifest = json.loads(
        (project_path / result.state.manifest_relative_path).read_text(encoding="utf-8")
    )

    early_pixel = _pixel(proxy, "0.12")
    early_rms = _audio_rms(proxy, "0.12")
    later_pixel = _pixel(proxy, "0.55")
    later_rms = _audio_rms(proxy, "0.55")
    assert (max(early_pixel) < 20) is expect_black
    assert (early_rms < 0.005) is expect_silence
    assert later_pixel[0] > 80 and later_pixel[0] > later_pixel[1] * 2
    assert later_rms > 0.02
    assert (manifest["leading_padding"]["video_frames"] > 0) is expect_black
    assert (manifest["leading_padding"]["audio_samples"] > 0) is expect_silence


@pytest.mark.parametrize("transfer", ["smpte2084", "arib-std-b67"])
def test_real_hdr_metadata_is_rejected_before_proxy_generation(
    tmp_path: Path, transfer: str
) -> None:
    source = tmp_path / f"hdr-{transfer}.mp4"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=red:size=64x64:rate=25:duration=0.2",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-x264-params",
        f"colorprim=bt2020:transfer={transfer}:colormatrix=bt2020nc",
        str(source),
    )
    project_path, project, asset = _project_with_source(tmp_path, source)

    started = time.monotonic()
    with pytest.raises(ProxyUnsupported):
        create_proxy(project_path, source_id=asset.source_id, expected_revision=project.revision)
    assert time.monotonic() - started < 10
    assert not list((project_path / "proxies" / asset.source_id).glob("*/manifest.json"))
