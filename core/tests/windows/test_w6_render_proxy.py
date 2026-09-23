from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path

import pytest
from windows.w6_support import (
    build_persistent_runtime,
    deny_shared_read,
    ffprobe_json,
    frame_rgb,
    make_av_source,
    runtime_environment,
    seed_export_review_project,
    seed_source_project,
)

from roughcut.adapters.project_store import ProjectStore
from roughcut.application.media_operations import (
    media_operation_status,
    run_approve_export_operation,
    run_proxy_operation,
)
from roughcut.application.proxies import create_proxy, read_proxy
from roughcut.application.renders import execute_render_plan
from roughcut.application.sources import fingerprint_file
from roughcut.application.workflows import workflow_status
from roughcut.domain.media_operation import MediaOperationError
from roughcut.domain.project import ProjectError


@pytest.fixture
def persistent_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    runtime = build_persistent_runtime(tmp_path / "render proxy runtime")
    monkeypatch.setenv("ROUGHCUT_RUNTIME_BINDING", str(runtime))
    monkeypatch.delenv("ROUGHCUT_FFMPEG_COMMAND", raising=False)
    monkeypatch.delenv("ROUGHCUT_FFPROBE_COMMAND", raising=False)
    return runtime


def test_w6_real_render_uses_original_sources_with_ready_proxies_and_restarts(
    tmp_path: Path,
    persistent_runtime: Path,
) -> None:
    del persistent_runtime
    media_root = tmp_path / "原素材 中文 with spaces"
    media_root.mkdir()
    source_a = media_root / "A 中文 source with spaces.mp4"
    source_b = media_root / "B 中文 source with spaces.mp4"
    make_av_source(source_a, color="red", frequency=440)
    make_av_source(source_b, color="blue", frequency=660)

    fixture = seed_export_review_project(
        tmp_path / "Render fixture root",
        (source_a, source_b),
    )
    status_before = workflow_status(fixture.project_path, fixture.run_id)
    decision_ref = status_before["workflow_run"]["artifact_refs"]["decision"]
    assert isinstance(decision_ref, dict)
    decision_path = fixture.project_path / "edits" / f"{decision_ref['artifact_id']}.json"
    project_before = (fixture.project_path / "project.json").read_bytes()
    decision_before = decision_path.read_bytes()
    source_fingerprints = tuple(
        fingerprint_file(Path(source.locator["absolute_path"]))
        for source in fixture.sources
    )

    proxy_paths: list[Path] = []
    for source in fixture.sources:
        proxy = create_proxy(
            fixture.project_path,
            source_id=source.source_id,
            expected_revision=fixture.revision,
        )
        assert proxy.state.status == "ready"
        assert proxy.state.proxy_relative_path is not None
        proxy_paths.append(fixture.project_path / proxy.state.proxy_relative_path)

    export_ref = status_before["presented_subjects"]["export_ref"]
    action_input = {"schema_version": 1, "export_ref": export_ref}
    with ExitStack() as stack:
        for proxy_path in proxy_paths:
            stack.enter_context(deny_shared_read(proxy_path))
        result = run_approve_export_operation(
            fixture.project_path,
            run_id=fixture.run_id,
            action_id="act_w6_export",
            action_input=action_input,
        )

    assert result.readback is False
    assert result.record.status == "succeeded"
    assert result.record.result_ref is not None
    assert result.result is not None
    assert result.result.workflow_run.lifecycle == "completed"
    assert result.result.receipt is not None
    render_ref = result.record.result_ref
    assert render_ref.render_plan_ref.schema_version == 2
    assert render_ref.manifest_ref.schema_version == 3
    output_path = fixture.project_path / render_ref.mp4_ref.project_relative_path
    manifest_path = fixture.project_path / render_ref.manifest_ref.project_relative_path
    plan_path = fixture.project_path / "renders" / f"{render_ref.mp4_ref.artifact_id}.plan.json"
    assert output_path.is_file()
    assert manifest_path.is_file()
    assert plan_path.is_file()

    manifest_text = manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert manifest["schema_version"] == 3
    assert manifest["acceptance"]["accepted"] is True
    assert all(manifest["acceptance"]["checks"].values())
    assert [clip["source_id"] for clip in manifest["clips"]] == [
        fixture.sources[0].source_id,
        fixture.sources[1].source_id,
        fixture.sources[0].source_id,
    ]
    assert [
        (clip["source_in_ticks"], clip["source_out_ticks"])
        for clip in manifest["clips"]
    ] == [(0, 72_000), (0, 72_000), (72_000, 144_000)]
    assert manifest["render_schedule"]["total_frames"] == 45
    assert manifest["render_schedule"]["total_samples"] == 86_400
    assert manifest["command_summary"]["faststart"] is True
    assert "locator" not in manifest_text
    assert "proxies/" not in manifest_text
    assert str(source_a.resolve()) not in manifest_text
    assert str(source_b.resolve()) not in manifest_text

    output_probe = ffprobe_json(output_path)
    streams = output_probe["streams"]
    assert isinstance(streams, list)
    video = next(stream for stream in streams if stream["codec_type"] == "video")
    audio = next(stream for stream in streams if stream["codec_type"] == "audio")
    assert video["codec_name"] == "h264"
    assert video["pix_fmt"] == "yuv420p"
    assert video["avg_frame_rate"] == "25/1"
    assert int(video["nb_read_frames"]) == 45
    assert audio["codec_name"] == "aac"
    assert audio["sample_rate"] == "48000"
    assert audio["channels"] == 2
    assert frame_rgb(output_path, "0.20")[0] > 140
    assert frame_rgb(output_path, "0.80")[2] > 140
    assert frame_rgb(output_path, "1.40")[0] > 140

    assert (fixture.project_path / "project.json").read_bytes() == project_before
    assert decision_path.read_bytes() == decision_before
    assert tuple(
        fingerprint_file(Path(source.locator["absolute_path"]))
        for source in fixture.sources
    ) == source_fingerprints
    assert not [
        path for path in (fixture.project_path / "renders").iterdir() if path.is_dir()
    ]
    assert not list(fixture.project_path.rglob("filter-complex.txt"))
    staging_root = fixture.project_path / "workflow" / "export-staging"
    assert not staging_root.exists() or {
        path.name for path in staging_root.iterdir()
    } <= {".claim.lock"}
    assert not list(fixture.project_path.rglob("owner.json"))

    operation_readback = run_approve_export_operation(
        fixture.project_path,
        run_id=fixture.run_id,
        action_id="act_w6_export",
        action_input=action_input,
    )
    assert operation_readback.readback is True
    assert operation_readback.record == result.record
    assert operation_readback.result is not None

    readback_script = (
        "import json,sys; "
        "payload=json.load(open(sys.argv[1],encoding='utf-8')); "
        "print(json.dumps({'schema':payload['schema_version'],"
        "'clips':payload['clips'],'accepted':payload['acceptance']['accepted']},"
        "ensure_ascii=False))"
    )
    readback = subprocess.run(
        [sys.executable, "-c", readback_script, str(manifest_path)],
        check=False,
        capture_output=True,
        text=False,
        env=runtime_environment(Path(os.environ["ROUGHCUT_RUNTIME_BINDING"])),
    )
    assert readback.returncode == 0, readback.stderr.decode(errors="replace")
    assert json.loads(readback.stdout.decode("utf-8")) == {
        "schema": 3,
        "clips": manifest["clips"],
        "accepted": True,
    }

    fresh_status = subprocess.run(
        [
            sys.executable,
            "-m",
            "roughcut.cli",
            "media-operation-status",
            "--project",
            str(fixture.project_path),
            "--operation-id",
            "act_w6_export",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=False,
        env=runtime_environment(Path(os.environ["ROUGHCUT_RUNTIME_BINDING"])),
    )
    assert fresh_status.returncode == 0, fresh_status.stderr.decode(errors="replace")
    assert json.loads(fresh_status.stdout.decode("utf-8"))["media_operation"] == (
        result.record.to_dict()
    )

    output_before_failed_retry = output_path.read_bytes()
    manifest_before_failed_retry = manifest_path.read_bytes()
    with pytest.raises(ProjectError, match="already exists"):
        execute_render_plan(
            fixture.project_path,
            render_ref.mp4_ref.artifact_id,
            expected_revision=fixture.revision,
        )
    assert output_path.read_bytes() == output_before_failed_retry
    assert manifest_path.read_bytes() == manifest_before_failed_retry


def test_w6_real_proxy_operation_publishes_reuses_reads_stales_and_fails_closed(
    tmp_path: Path,
    persistent_runtime: Path,
) -> None:
    del persistent_runtime
    source_path = tmp_path / "原素材 中文 with spaces.mp4"
    make_av_source(source_path, color="green", frequency=550)
    project_path, imported, source = seed_source_project(
        tmp_path / "Proxy fixture root", source_path
    )
    project_before = (project_path / "project.json").read_bytes()
    source_before = source_path.read_bytes()

    first = run_proxy_operation(
        project_path,
        operation_id="op_w6_proxy_first",
        source_id=source.source_id,
        expected_project_revision=imported.revision,
    )
    assert first.record.status == "succeeded"
    assert first.record.result_ref is not None
    result_ref = first.record.result_ref
    output_path = project_path / result_ref.output_relative_path
    manifest_path = output_path.parent / "manifest.json"
    assert output_path.is_file()
    assert manifest_path.is_file()
    output_mtime = output_path.stat().st_mtime_ns

    state = read_proxy(
        project_path,
        source_id=source.source_id,
        expected_revision=imported.revision,
    )
    assert state.status == "ready"
    assert state.reused is True
    manifest_text = manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert manifest["source_id"] == source.source_id
    assert manifest["cache_key"] == result_ref.cache_key
    assert manifest["profile"] == {
        "schema_version": 1,
        "profile_version": 1,
        "canvas": {"width": 1280, "height": 720},
        "frame_rate": {"numerator": 25, "denominator": 1},
        "gop_frames": 50,
        "has_video": True,
        "has_audio": True,
        "container": "mp4",
        "video_codec": "libx264",
        "pixel_format": "yuv420p",
        "crf": 23,
        "preset": "veryfast",
        "faststart": True,
        "audio_codec": "aac",
        "audio_sample_rate": 48_000,
    }
    assert str(source_path.resolve()) not in manifest_text
    assert "locator" not in manifest_text
    assert result_ref.output_relative_path.startswith(
        f"proxies/{source.source_id}/{result_ref.cache_key}/"
    )

    output_probe = ffprobe_json(output_path)
    streams = output_probe["streams"]
    assert isinstance(streams, list)
    video = next(stream for stream in streams if stream["codec_type"] == "video")
    audio = next(stream for stream in streams if stream["codec_type"] == "audio")
    assert video["codec_name"] == "h264"
    assert video["pix_fmt"] == "yuv420p"
    assert video["avg_frame_rate"] == "25/1"
    assert video["width"] == 1280
    assert video["height"] == 720
    assert audio["codec_name"] == "aac"
    assert audio["sample_rate"] == "48000"
    assert audio["channels"] == 2

    cache_reuse = run_proxy_operation(
        project_path,
        operation_id="op_w6_proxy_cache_reuse",
        source_id=source.source_id,
        expected_project_revision=imported.revision,
    )
    assert cache_reuse.record.status == "succeeded"
    assert cache_reuse.result is not None
    assert cache_reuse.result.reused is True
    assert output_path.stat().st_mtime_ns == output_mtime
    assert ProjectStore(project_path).load().revision == imported.revision

    operation_readback = run_proxy_operation(
        project_path,
        operation_id="op_w6_proxy_first",
        source_id=source.source_id,
        expected_project_revision=imported.revision,
    )
    assert operation_readback.readback is True
    assert operation_readback.result is None
    assert media_operation_status(project_path, "op_w6_proxy_first") == first.record

    output_before_stale = output_path.read_bytes()
    source_path.write_bytes(source_before + b"changed")
    stale = read_proxy(
        project_path,
        source_id=source.source_id,
        expected_revision=imported.revision,
    )
    assert stale.status == "stale"
    assert stale.reason == "source_fingerprint_changed"
    with pytest.raises(ProjectError, match="fingerprint"):
        create_proxy(
            project_path,
            source_id=source.source_id,
            expected_revision=imported.revision,
        )
    assert output_path.read_bytes() == output_before_stale
    assert manifest_path.is_file()
    assert (project_path / "project.json").read_bytes() == project_before

    bad_source = tmp_path / "坏素材 中文 with spaces.mp4"
    make_av_source(bad_source, color="red", frequency=330)
    bad_project, bad_imported, bad_asset = seed_source_project(
        tmp_path / "Proxy failure fixture root", bad_source
    )
    bad_source.write_bytes(b"not a media file")
    with pytest.raises(ProjectError, match="fingerprint"):
        run_proxy_operation(
            bad_project,
            operation_id="op_w6_proxy_failure",
            source_id=bad_asset.source_id,
            expected_project_revision=bad_imported.revision,
        )
    with pytest.raises(MediaOperationError) as status_error:
        media_operation_status(bad_project, "op_w6_proxy_failure")
    assert status_error.value.code == "operation_not_found"
    assert not (bad_project / "proxies").exists()
    assert not list(bad_project.rglob(".candidate-*"))
