"""Public surface proofs: CLI and MCP align_multicam calls carry closed
source_pairs to the producer, reject unknown fields, and never touch media
for unpaired Sources."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from roughcut import cli, mcp
from roughcut.application.alignments import AlignmentOutcome


def _mcp_request(name: str, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def _fake_outcome() -> AlignmentOutcome:
    record = SimpleNamespace(
        to_dict=lambda: {
            "operation_id": "op_00000000000040008000000000000150",
            "status": "succeeded",
        }
    )
    return AlignmentOutcome(record, None, True)


def _arguments(project_path: str, auxiliary_cameras: list[dict[str, object]]):
    return {
        "project_path": project_path,
        "operation_id": "op_00000000000040008000000000000150",
        "alignment_id": "aln_public_pairs",
        "expected_revision": 1,
        "main_camera": {
            "camera_id": "main",
            "ordered_source_ids": ["main_a"],
        },
        "auxiliary_cameras": auxiliary_cameras,
        "main_audio_stable": True,
        "max_temporary_disk_bytes": 536_870_912,
        "max_analysis_memory_bytes": 4_294_967_296,
        "max_runtime_seconds": 1800,
    }


def test_mcp_align_multicam_carries_source_pairs_to_the_producer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_producer(*args: object, **kwargs: object):
        captured.update(kwargs)
        captured["project_path"] = args[0] if args else kwargs.get("project_path")
        return _fake_outcome()

    monkeypatch.setattr(mcp, "run_align_multicam", fake_producer)
    pairs = [
        {"main_source_id": "main_a", "auxiliary_source_id": "aux_a"},
        {"main_source_id": "main_a", "auxiliary_source_id": "aux_b"},
    ]
    response = mcp.handle_request(
        _mcp_request(
            "align_multicam",
            _arguments(
                str(tmp_path),
                [
                    {
                        "camera_id": "aux-1",
                        "ordered_source_ids": ["aux_a", "aux_b"],
                        "source_pairs": pairs,
                    }
                ],
            ),
        )
    )
    assert response is not None
    result = response["result"]
    assert result["isError"] is False
    payload = result["structuredContent"]
    assert payload["ok"] is True
    assert captured["auxiliary_cameras"][0]["source_pairs"] == pairs
    assert str(captured["project_path"]) == str(tmp_path)


def _project_with_pairable_sources(tmp_path: Path):
    import wave

    from roughcut.application.projects import create_project
    from roughcut.application.sources import add_source
    from roughcut.domain.project import ImportMode

    ffmpeg = __import__("shutil").which("ffmpeg")
    ffprobe = __import__("shutil").which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.fail("Roughcut 测试 fixture 未解析到 ffmpeg/ffprobe")
    media = tmp_path / "media"
    media.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in ("main_a", "aux_a", "aux_b"):
        path = media / f"{name}.wav"
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(8_000)
            output.writeframes(b"\0\0" * 8_000)
        paths.append(path)
    project_root = tmp_path / "project"
    project = create_project(project_root, "Public surface")
    for path in paths:
        project = add_source(
            project_root,
            path,
            ImportMode.LINKED,
            expected_revision=project.revision,
        )
    ids = [source.source_id for source in project.sources]
    return project_root, {"main": ids[0], "aux_a": ids[1], "aux_b": ids[2]}


def test_mcp_rejects_unknown_pair_fields_before_any_media_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root, ids = _project_with_pairable_sources(tmp_path)

    def forbidden_resolve(*_args: object, **_kwargs: object):
        raise AssertionError("media resolution must not run for a non-closed request")

    import roughcut.application.alignments as alignments_module

    monkeypatch.setattr(
        alignments_module, "_resolve_groups", forbidden_resolve
    )
    response = mcp.handle_request(
        _mcp_request(
            "align_multicam",
            _arguments(
                str(project_root),
                [
                    {
                        "camera_id": "aux-1",
                        "ordered_source_ids": [ids["aux_a"]],
                        "source_pairs": [
                            {
                                "main_source_id": ids["main"],
                                "auxiliary_source_id": ids["aux_a"],
                                "confidence": 9,
                            }
                        ],
                    }
                ],
            ),
        )
    )
    assert response is not None
    result = response["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"] == {
        "code": "alignment_integrity_error"
    }


def test_mcp_multi_file_group_without_pairs_fails_before_media_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root, ids = _project_with_pairable_sources(tmp_path)

    def forbidden_resolve(*_args: object, **_kwargs: object):
        raise AssertionError("unpaired Sources must not be resolved")

    import roughcut.application.alignments as alignments_module

    monkeypatch.setattr(
        alignments_module, "_resolve_groups", forbidden_resolve
    )
    response = mcp.handle_request(
        _mcp_request(
            "align_multicam",
            _arguments(
                str(project_root),
                [
                    {
                        "camera_id": "aux-1",
                        "ordered_source_ids": [ids["aux_a"], ids["aux_b"]],
                    }
                ],
            ),
        )
    )
    assert response is not None
    result = response["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"] == {"code": "alignment_input_stale"}


def test_cli_align_multicam_carries_source_pairs_to_the_producer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    def fake_producer(*args: object, **kwargs: object):
        captured.update(kwargs)
        return _fake_outcome()

    monkeypatch.setattr(cli, "run_align_multicam", fake_producer)
    pairs = [{"main_source_id": "main_a", "auxiliary_source_id": "aux_a"}]
    cameras = json.dumps(
        [
            {
                "camera_id": "aux-1",
                "ordered_source_ids": ["aux_a"],
                "source_pairs": pairs,
            }
        ]
    )
    cli.main(
        [
            "align-multicam",
            "--project",
            str(tmp_path),
            "--operation-id",
            "op_00000000000040008000000000000151",
            "--alignment-id",
            "aln_cli_pairs",
            "--expected-revision",
            "1",
            "--main-camera-json",
            json.dumps({"camera_id": "main", "ordered_source_ids": ["main_a"]}),
            "--auxiliary-cameras-json",
            cameras,
            "--main-audio-stable",
            "true",
            "--max-temporary-disk-bytes",
            "536870912",
            "--max-analysis-memory-bytes",
            "4294967296",
            "--max-runtime-seconds",
            "1800",
            "--json",
        ]
    )
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["ok"] is True
    assert captured["auxiliary_cameras"][0]["source_pairs"] == pairs


def test_cli_rejects_unknown_pair_fields_before_any_media_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_root, ids = _project_with_pairable_sources(tmp_path)

    def forbidden_resolve(*_args: object, **_kwargs: object):
        raise AssertionError("media resolution must not run for a non-closed request")

    import roughcut.application.alignments as alignments_module

    monkeypatch.setattr(
        alignments_module, "_resolve_groups", forbidden_resolve
    )
    cameras = json.dumps(
        [
            {
                "camera_id": "aux-1",
                "ordered_source_ids": [ids["aux_a"]],
                "source_pairs": [
                    {
                        "main_source_id": ids["main"],
                        "auxiliary_source_id": ids["aux_a"],
                        "note": "extra",
                    }
                ],
            }
        ]
    )
    with pytest.raises(SystemExit) as exit_info:
        cli.main(
            [
                "align-multicam",
                "--project",
                str(project_root),
                "--operation-id",
                "op_00000000000040008000000000000152",
                "--alignment-id",
                "aln_cli_bad_pairs",
                "--expected-revision",
                "1",
                "--main-camera-json",
                json.dumps(
                    {
                        "camera_id": "main",
                        "ordered_source_ids": [ids["main"]],
                    }
                ),
                "--auxiliary-cameras-json",
                cameras,
                "--main-audio-stable",
                "true",
                "--max-temporary-disk-bytes",
                "536870912",
                "--max-analysis-memory-bytes",
                "4294967296",
                "--max-runtime-seconds",
                "1800",
                "--json",
            ]
        )
    assert exit_info.value.code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"] == {"code": "alignment_integrity_error"}
