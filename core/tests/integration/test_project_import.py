from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from roughcut.adapters.ffprobe import parse_ffprobe_json
from roughcut.adapters.project_store import ProjectStore
from roughcut.application.projects import create_project, open_project
from roughcut.application.sources import add_source, fingerprint_file
from roughcut.domain.asr import ASR_CLOUD_TAG
from roughcut.domain.project import ImportMode, ProjectError
from roughcut.mcp import handle_request

FFMPEG = shutil.which("ffmpeg")
pytestmark = pytest.mark.usefixtures("synthetic_media_runtime")


def make_media(path: Path) -> None:
    if FFMPEG is None:
        pytest.skip("ffmpeg is required for the synthetic media integration test")
    result = subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x90:rate=25:duration=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:sample_rate=48000:duration=1",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_create_copy_link_open_and_atomically_save_project(tmp_path: Path) -> None:
    source = tmp_path / "中文 source with spaces.mp4"
    make_media(source)
    original_bytes = source.read_bytes()
    project_path = tmp_path / "中文 project with spaces"

    project = create_project(project_path, "中文粗剪")
    copied = add_source(
        project_path,
        source,
        ImportMode.COPIED,
        expected_revision=project.revision,
    )
    linked = add_source(
        project_path,
        source,
        ImportMode.LINKED,
        expected_revision=copied.revision,
    )

    assert source.exists()
    assert source.read_bytes() == original_bytes
    assert copied.sources[0].locator == {
        "project_relative_path": f"sources/{copied.sources[0].source_id}.mp4"
    }
    assert (project_path / copied.sources[0].locator["project_relative_path"]).read_bytes() == original_bytes
    assert linked.sources[1].locator == {"absolute_path": str(source.resolve())}
    assert linked.sources[0].probe.video_codec == "mpeg4"
    assert linked.sources[0].probe.audio_codec == "aac"
    assert linked.sources[0].probe.duration_ticks > 0
    assert linked.sources[0].fingerprint.size == len(original_bytes)

    stored = json.loads((project_path / "project.json").read_text(encoding="utf-8"))
    assert stored["schema_version"] == 1
    assert stored["revision"] == 2
    assert stored["sources"][0]["fingerprint"]
    assert stored["sources"][0]["probe"]
    assert open_project(project_path) == linked

    store = ProjectStore(project_path)
    renamed = replace(linked, name="新名称", revision=linked.revision + 1)
    with patch("roughcut.adapters.project_store.os.replace", wraps=os.replace) as atomic_replace:
        store.save(renamed, expected_revision=linked.revision)
    atomic_replace.assert_called_once()
    assert not list(project_path.glob(".project.json.*.tmp"))

    with pytest.raises(FileExistsError):
        create_project(project_path, "不得覆盖")
    with pytest.raises(ProjectError, match="revision conflict"):
        add_source(project_path, source, ImportMode.LINKED, expected_revision=0)


@pytest.mark.parametrize(
    ("filename", "marked"),
    (
        ("采访02__方言.mov", True),
        ("方言采访.mov", False),
        ("采访_方言版.mov", False),
        ("abc__方言_01.mov", False),
        ("abc__方言.mov.bak", False),
    ),
)
def test_source_import_uses_original_filename_stem_for_cloud_marker(
    tmp_path: Path, filename: str, marked: bool
) -> None:
    source = tmp_path / filename
    if source.suffix == ".bak":
        media_fixture = tmp_path / "media-fixture.mov"
        make_media(media_fixture)
        media_fixture.rename(source)
    else:
        make_media(source)
    project_path = tmp_path / "project"
    project = create_project(project_path, "Project")

    imported = add_source(
        project_path,
        source,
        ImportMode.LINKED,
        expected_revision=project.revision,
    )

    assert imported.revision == 1
    assert (ASR_CLOUD_TAG in imported.sources[0].tags) is marked
    assert imported.sources[0].display_name == filename
    assert source.exists()


def test_source_import_marker_uses_symlink_input_for_copied_and_linked(
    tmp_path: Path,
) -> None:
    target = tmp_path / "resolved-target.mov"
    make_media(target)
    original_target_bytes = target.read_bytes()
    symlink = tmp_path / "采访02__方言.mov"
    symlink.symlink_to(target)
    project_path = tmp_path / "project"
    project = create_project(project_path, "Project")

    linked = add_source(
        project_path,
        symlink,
        ImportMode.LINKED,
        expected_revision=project.revision,
    )
    copied = add_source(
        project_path,
        symlink,
        ImportMode.COPIED,
        expected_revision=linked.revision,
    )

    assert linked.revision == 1
    assert copied.revision == 2
    assert all(ASR_CLOUD_TAG in source.tags for source in copied.sources)
    assert linked.sources[0].display_name == target.name
    assert copied.sources[1].display_name == target.name
    assert symlink.exists()
    assert target.exists()
    assert target.read_bytes() == original_target_bytes
    copied_path = project_path / copied.sources[1].locator["project_relative_path"]
    assert copied_path.read_bytes() == original_target_bytes


def test_copystat_failure_leaves_no_imported_file_or_revision_change(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    make_media(source)
    project_path = tmp_path / "project"
    create_project(project_path, "Project")

    with (
        patch("roughcut.application.sources.shutil.copystat", side_effect=OSError("metadata failed")),
        pytest.raises(OSError, match="metadata failed"),
    ):
        add_source(project_path, source, ImportMode.COPIED, expected_revision=0)

    project = open_project(project_path)
    assert project.revision == 0
    assert project.sources == ()
    assert list((project_path / "sources").iterdir()) == []


def test_fingerprint_reads_only_head_and_tail_for_large_files(tmp_path: Path) -> None:
    head = b"h" * (1024 * 1024)
    tail = b"t" * (1024 * 1024)
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(head + b"a" * (2 * 1024 * 1024) + tail)
    second.write_bytes(head + b"b" * (2 * 1024 * 1024) + tail)

    assert fingerprint_file(first).sha256_head_tail == fingerprint_file(second).sha256_head_tail


def test_ffprobe_parser_preserves_nonzero_start_and_exact_rate() -> None:
    probe = parse_ffprobe_json(
        {
            "format": {"duration": "2.500000", "start_time": "0.090000"},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "start_time": "0.100000",
                    "width": 1920,
                    "height": 1080,
                    "avg_frame_rate": "30000/1001",
                    "r_frame_rate": "30000/1001",
                    "tags": {},
                    "side_data_list": [],
                },
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "sample_rate": "48000",
                    "start_time": "0.090000",
                },
            ],
        }
    )

    assert probe.duration_ticks == 300_000
    assert probe.container_start_ticks == 10_800
    assert probe.first_content_ticks == 12_000
    assert probe.nominal_frame_rate == {"numerator": 30_000, "denominator": 1_001}


def test_project_cli_and_mcp_share_the_same_application_service(tmp_path: Path) -> None:
    source_path = tmp_path / "Agent 中文 source.mp4"
    make_media(source_path)
    project_path = tmp_path / "agent project"
    cli = subprocess.run(
        [
            sys.executable,
            "-m",
            "roughcut.cli",
            "project-create",
            "--project",
            str(project_path),
            "--name",
            "Agent project",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert cli.returncode == 0, cli.stderr
    cli_project = json.loads(cli.stdout)["project"]

    response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "project_open", "arguments": {"project_path": str(project_path)}},
        }
    )
    assert response is not None
    mcp_result = response["result"]
    assert isinstance(mcp_result, dict)
    assert mcp_result["structuredContent"]["project"] == cli_project

    source_response = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "source_add",
                "arguments": {
                    "project_path": str(project_path),
                    "source_path": str(source_path),
                    "import_mode": "linked",
                    "expected_revision": cli_project["revision"],
                },
            },
        }
    )
    assert source_response is not None
    source_result = source_response["result"]
    assert isinstance(source_result, dict)
    imported = source_result["structuredContent"]["project"]
    assert imported == open_project(project_path).to_dict()
