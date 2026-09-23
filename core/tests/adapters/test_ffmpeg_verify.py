from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from roughcut.adapters.ffmpeg.audio_quota import (
    AAC_SAMPLE_QUOTA_TOLERANCE,
    audio_sample_quota_matches,
)
from roughcut.adapters.ffmpeg.verify import (
    _decoded_audio_sample_count,
    evaluate_output_probe,
    verify_render_output,
)
from roughcut.domain.bindings import SourceTranscriptBinding
from roughcut.domain.project import ImportMode, MediaProbe, SourceAsset, SourceFingerprint
from roughcut.domain.render import (
    MultiSourceRenderPlan,
    OutputSettings,
    RenderClip,
    RenderPlan,
    ToolResolution,
)
from roughcut.domain.time import RationalRate


def _plan(*, with_audio: bool = True) -> RenderPlan:
    return RenderPlan(
        render_id="render_fixture",
        project_id="proj_fixture",
        project_revision=4,
        edit_version_id="edit_fixture",
        source=SourceAsset(
            source_id="src_render",
            kind="video",
            display_name="fixture.mp4",
            import_mode=ImportMode.COPIED,
            locator={"project_relative_path": "sources/src_render.mp4"},
            fingerprint=SourceFingerprint(1, 2, "a" * 64),
            probe=MediaProbe(
                duration_ticks=600_000,
                container_start_ticks=0,
                first_content_ticks=0,
                video_codec="h264",
                width=320,
                height=180,
                nominal_frame_rate={"numerator": 25, "denominator": 1},
                is_vfr=False,
                audio_codec="aac" if with_audio else None,
                audio_sample_rate=48_000 if with_audio else None,
                rotation_degrees=0,
            ),
        ),
        clips=(RenderClip("clip_a", "src_render", 0, 240_000),),
        output_settings=OutputSettings(320, 180, RationalRate(25, 1), 48_000),
        ffmpeg=ToolResolution("ffmpeg", "/tools/ffmpeg", "ffmpeg version fixture"),
        ffprobe=ToolResolution("ffprobe", "/tools/ffprobe", "ffprobe version fixture"),
        plan_relative_path="renders/render_fixture.plan.json",
        output_relative_path="renders/render_fixture.mp4",
        manifest_relative_path="renders/render_fixture.manifest.json",
    )


def _multi_plan() -> MultiSourceRenderPlan:
    single = _plan()
    second = replace(
        single.source,
        source_id="src_second",
        display_name="second.mp4",
        locator={"project_relative_path": "sources/src_second.mp4"},
        fingerprint=SourceFingerprint(3, 4, "b" * 64),
    )
    return MultiSourceRenderPlan(
        render_id="render_multi",
        project_id=single.project_id,
        project_revision=single.project_revision,
        edit_version_id="edit_multi",
        source_bindings=(
            SourceTranscriptBinding("src_render", "tr_a"),
            SourceTranscriptBinding("src_second", "tr_b"),
        ),
        sources=(single.source, second),
        clips=(
            RenderClip("clip_a", "src_render", 0, 120_000),
            RenderClip("clip_b", "src_second", 120_000, 240_000),
        ),
        output_settings=single.output_settings,
        ffmpeg=single.ffmpeg,
        ffprobe=single.ffprobe,
        plan_relative_path="renders/render_multi.plan.json",
        output_relative_path="renders/render_multi.mp4",
        manifest_relative_path="renders/render_multi.manifest.json",
    )


def _probe(*, duration: str = "2.000000", video_codec: str = "h264") -> dict[str, object]:
    return {
        "format": {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "start_time": "0.000000",
            "duration": duration,
        },
        "streams": [
            {
                "codec_type": "video",
                "codec_name": video_codec,
                "width": 320,
                "height": 180,
                "avg_frame_rate": "25/1",
                "pix_fmt": "yuv420p",
                "start_time": "0.000000",
                "nb_read_frames": "50",
            },
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "48000",
                "channels": 2,
                "start_time": "0.000000",
                "time_base": "1/48000",
                "duration_ts": "96000",
            },
        ],
    }


def test_probe_acceptance_records_every_required_check() -> None:
    report = evaluate_output_probe(
        _multi_plan(),
        _probe(),
        output_size=1024,
        decode_succeeded=True,
        faststart=True,
    )

    assert report.accepted is True
    assert all(report.checks.values())
    assert report.duration_ticks == 240_000
    assert report.probe["video"]["codec_name"] == "h264"
    assert report.probe["audio"]["codec_name"] == "aac"


def test_probe_acceptance_rejects_wrong_codec_duration_and_decode_failure() -> None:
    report = evaluate_output_probe(
        _plan(),
        _probe(duration="2.200000", video_codec="mpeg4"),
        output_size=1024,
        decode_succeeded=False,
        faststart=False,
    )

    assert report.accepted is False
    assert report.checks["video_codec_h264"] is False
    assert report.checks["duration_within_tolerance"] is False
    assert report.checks["decodes_from_start_to_end"] is False
    assert report.checks["faststart"] is False


def test_probe_acceptance_rejects_nonzero_output_start() -> None:
    probe = _probe()
    probe["format"]["start_time"] = "0.040000"  # type: ignore[index]
    probe["streams"][0]["start_time"] = "0.040000"  # type: ignore[index]
    probe["streams"][1]["start_time"] = "0.040000"  # type: ignore[index]

    report = evaluate_output_probe(
        _multi_plan(),
        probe,
        output_size=1024,
        decode_succeeded=True,
        faststart=True,
    )

    assert report.accepted is False
    assert report.checks["starts_at_zero"] is False


def test_schema_one_verifier_keeps_its_existing_check_and_probe_shape() -> None:
    report = evaluate_output_probe(
        _plan(),
        _probe(),
        output_size=1024,
        decode_succeeded=True,
        faststart=True,
    )

    assert report.accepted is True
    assert "starts_at_zero" not in report.checks
    assert "start_ticks" not in report.probe["format"]


def test_probe_acceptance_rejects_non_stereo_audio() -> None:
    probe = _probe()
    probe["streams"][1]["channels"] = 1  # type: ignore[index]

    report = evaluate_output_probe(
        _plan(),
        probe,
        output_size=1024,
        decode_succeeded=True,
        faststart=True,
    )

    assert report.accepted is False
    assert report.checks["audio_channels_stereo"] is False


def test_probe_acceptance_rejects_frame_and_sample_quota_drift() -> None:
    probe = _probe()
    probe["streams"][0]["nb_read_frames"] = "49"  # type: ignore[index]
    probe["streams"][1]["duration_ts"] = "94000"  # type: ignore[index]

    report = evaluate_output_probe(
        _plan(),
        probe,
        output_size=1024,
        decode_succeeded=True,
        decoded_audio_samples=94_000,
        faststart=True,
    )

    assert report.accepted is False
    assert report.checks["output_frame_quota"] is False
    assert report.checks["audio_sample_quota"] is False
    assert report.checks["decoded_audio_padding_within_aac_frame"] is False


def test_audio_sample_quota_uses_one_aac_frame_tolerance() -> None:
    plan = _plan()
    within = _probe()
    within["streams"][1]["duration_ts"] = str(96_000 - 1024)  # type: ignore[index]
    accepted = evaluate_output_probe(
        plan,
        within,
        output_size=1024,
        decode_succeeded=True,
        faststart=True,
    )
    assert accepted.checks["audio_sample_quota"] is True
    assert accepted.accepted is True

    over = _probe()
    over["streams"][1]["duration_ts"] = str(96_000 - 1025)  # type: ignore[index]
    rejected = evaluate_output_probe(
        plan,
        over,
        output_size=1024,
        decode_succeeded=True,
        faststart=True,
    )
    assert rejected.checks["audio_sample_quota"] is False
    assert rejected.accepted is False

    unavailable = _probe()
    unavailable["streams"][1]["duration_ts"] = "0"  # type: ignore[index]
    not_obtained = evaluate_output_probe(
        plan,
        unavailable,
        output_size=1024,
        decode_succeeded=True,
        faststart=True,
    )
    assert not_obtained.checks["audio_sample_quota"] is False


@pytest.mark.parametrize("delta", [0, 1, -1, 608, -608, 1024, -1024])
def test_aac_quota_contract_accepts_up_to_one_frame_of_drift(delta: int) -> None:
    expected = 22_439_520
    assert audio_sample_quota_matches(codec="aac", expected=expected, actual=expected + delta)


@pytest.mark.parametrize("delta", [1025, -1025])
def test_aac_quota_contract_rejects_more_than_one_frame_of_drift(delta: int) -> None:
    expected = 22_439_520
    assert not audio_sample_quota_matches(codec="aac", expected=expected, actual=expected + delta)


def test_non_aac_codecs_keep_the_exact_schedule_quota() -> None:
    assert audio_sample_quota_matches(codec="pcm_s16le", expected=96_000, actual=96_000)
    assert audio_sample_quota_matches(codec=None, expected=96_000, actual=96_000)
    assert not audio_sample_quota_matches(codec="pcm_s16le", expected=96_000, actual=96_001)
    assert not audio_sample_quota_matches(codec=None, expected=96_000, actual=96_608)


def test_shared_tolerance_constant_stays_at_one_aac_frame() -> None:
    assert AAC_SAMPLE_QUOTA_TOLERANCE == 1024


@pytest.mark.parametrize("delta", [0, 1, -1, 608, -608, 1024, -1024])
def test_ordinary_verifier_timeline_and_decoded_quotas_share_the_contract(delta: int) -> None:
    plan = _plan()
    timeline_probe = _probe()
    timeline_probe["streams"][1]["duration_ts"] = str(96_000 + delta)  # type: ignore[index]
    timeline_report = evaluate_output_probe(
        plan,
        timeline_probe,
        output_size=1024,
        decode_succeeded=True,
        faststart=True,
    )
    decoded_report = evaluate_output_probe(
        plan,
        _probe(),
        output_size=1024,
        decode_succeeded=True,
        decoded_audio_samples=96_000 + delta,
        faststart=True,
    )
    assert timeline_report.checks["audio_sample_quota"] is True
    assert decoded_report.checks["decoded_audio_padding_within_aac_frame"] is True


@pytest.mark.parametrize("delta", [1025, -1025])
def test_ordinary_verifier_rejects_more_than_one_frame_of_drift(delta: int) -> None:
    plan = _plan()
    timeline_probe = _probe()
    timeline_probe["streams"][1]["duration_ts"] = str(96_000 + delta)  # type: ignore[index]
    timeline_report = evaluate_output_probe(
        plan,
        timeline_probe,
        output_size=1024,
        decode_succeeded=True,
        faststart=True,
    )
    decoded_report = evaluate_output_probe(
        plan,
        _probe(),
        output_size=1024,
        decode_succeeded=True,
        decoded_audio_samples=96_000 + delta,
        faststart=True,
    )
    assert timeline_report.checks["audio_sample_quota"] is False
    assert decoded_report.checks["decoded_audio_padding_within_aac_frame"] is False


def test_silent_plan_requires_no_audio_stream() -> None:
    probe = _probe()
    probe["streams"] = [probe["streams"][0]]  # type: ignore[index]

    report = evaluate_output_probe(
        _plan(with_audio=False),
        probe,
        output_size=1024,
        decode_succeeded=True,
        faststart=True,
    )

    assert report.accepted is True
    assert report.checks["audio_stream_contract"] is True


def test_audio_sample_count_uses_the_final_astats_summary_without_pcm_buffering() -> None:
    stderr = """
[Parsed_astats_0] Number of samples: 685056
[Parsed_astats_0] Number of samples: 685056
[Parsed_astats_0] Overall
[Parsed_astats_0] Number of samples: 685056
"""

    assert _decoded_audio_sample_count(stderr) == 685_056
    assert _decoded_audio_sample_count("no sample summary") is None


def test_verifier_counts_decoded_samples_with_astats_and_null_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "candidate.mp4"
    output.write_bytes(b"candidate")
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[0] == "/tools/ffprobe":
            return subprocess.CompletedProcess(command, 0, json.dumps(_probe()), "")
        if "-af" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                "",
                "[Parsed_astats_0] Overall\n[Parsed_astats_0] Number of samples: 96000\n",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("roughcut.adapters.ffmpeg.verify._has_faststart", lambda _path: True)

    report = verify_render_output(output, _plan(), process_runner=run)

    assert report.accepted is True
    audio_command = commands[2]
    assert "astats=metadata=0:reset=0" in audio_command
    assert audio_command[audio_command.index("-f") + 1] == "null"
    assert "pcm_s16le" not in audio_command
