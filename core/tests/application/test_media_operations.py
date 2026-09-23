from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import roughcut.application.media_operations as operations_module
import roughcut.cli
import roughcut.mcp
from roughcut.adapters.ffmpeg.proxy import (
    FFmpegProxyError,
    ProxyVerificationReport,
)
from roughcut.adapters.funasr.normalize import TranscriptNormalizationError
from roughcut.adapters.funasr.runner import FunASRRun, FunASRRunnerError
from roughcut.adapters.media_operation_store import MediaOperationStore
from roughcut.adapters.project_store import ProjectStore
from roughcut.adapters.runtime_binding import RuntimeBindingError
from roughcut.adapters.workflow_store import WorkflowStore
from roughcut.application.media_operations import (
    media_operation_status,
    run_proxy_operation,
    run_transcription_operation,
)
from roughcut.application.projects import create_project
from roughcut.application.sources import fingerprint_file
from roughcut.application.workflows import (
    workflow_action,
    workflow_start,
)
from roughcut.domain.media_operation import (
    MediaOperationError,
    MediaOperationFailure,
    MediaOperationRecord,
)
from roughcut.domain.project import ImportMode, MediaProbe, ProjectError, SourceAsset
from roughcut.domain.render import ToolResolution

ROOT = Path(__file__).resolve().parents[3]
ASR_FIXTURE = ROOT / "fixtures" / "asr" / "funasr_sentence_info.json"


def _runtime() -> object:
    def component(value: str) -> SimpleNamespace:
        return SimpleNamespace(
            path=f"/fixture/{value}",
            receipt={"algorithm": "sha256", "value": value * 64},
        )
    binding = SimpleNamespace(
        install_root="/fixture/runtime",
        python=SimpleNamespace(
            interpreter="/fixture/python",
            receipt={"fixture": True},
        ),
        components={
            "asr": component("a"),
            "vad": component("b"),
            "punc": component("c"),
            "campp": component("d"),
        },
        ffmpeg=SimpleNamespace(command="/fixture/ffmpeg"),
    )
    return SimpleNamespace(
        binding=binding,
        runtime_binding_sha256="1" * 64,
        python_receipt_hash="2" * 64,
        ffmpeg_tool_selection_hash="3" * 64,
        ffprobe_tool_selection_hash="4" * 64,
        ffmpeg=ToolResolution(
            "ffmpeg", "/fixture/ffmpeg", "ffmpeg version fixture"
        ),
        ffprobe=ToolResolution(
            "ffprobe", "/fixture/ffprobe", "ffprobe version fixture"
        ),
    )


@pytest.fixture
def fake_runtime(monkeypatch: pytest.MonkeyPatch) -> object:
    runtime = _runtime()
    monkeypatch.setattr(
        operations_module,
        "_load_persistent_runtime",
        lambda: runtime,
    )
    monkeypatch.setattr(
        operations_module,
        "_validate_media_runtime",
        lambda _runtime: ("ffmpeg version fixture", "ffprobe version fixture"),
    )
    return runtime


def _source_project(
    tmp_path: Path,
    *,
    source_id: str = "src_media",
    video: bool,
) -> tuple[Path, Path, SourceAsset]:
    root = tmp_path / "media-project"
    source_path = tmp_path / ("source.mp4" if video else "source.wav")
    source_path.write_bytes(b"fixture-source")
    project = create_project(root, "Media")
    source = SourceAsset(
        source_id=source_id,
        kind="video" if video else "audio",
        display_name=source_path.name,
        import_mode=ImportMode.LINKED,
        locator={"absolute_path": str(source_path.resolve())},
        fingerprint=fingerprint_file(source_path),
        probe=MediaProbe(
            duration_ticks=240_000 if video else 360_000,
            container_start_ticks=0,
            first_content_ticks=0,
            video_codec="h264" if video else None,
            width=640 if video else None,
            height=360 if video else None,
            nominal_frame_rate=(
                {"numerator": 25, "denominator": 1} if video else None
            ),
            is_vfr=False,
            audio_codec="aac" if video else "pcm_s16le",
            audio_sample_rate=48_000 if video else 16_000,
            rotation_degrees=0,
        ),
    )
    ProjectStore(root).save(
        replace(project, sources=(source,)),
        expected_revision=0,
    )
    return root, source_path, source


def _authorize_transcription(root: Path, source_id: str) -> None:
    started = workflow_start(root, "wfr_media", [source_id])
    basis = started.status["confirmation_bases"]["scope"]["basis"]
    workflow_action(
        root,
        "wfr_media",
        "act_scope_media",
        "approve_scope",
        {
            "schema_version": 1,
            "confirmation_basis": basis,
            "source_authorizations": [
                {
                    "source_id": source_id,
                    "transcribe": True,
                    "speaker_diarization": False,
                }
            ],
        },
    )


def _fixture_runner(calls: dict[str, int]):
    def run(_source_path: Path, raw_output_path: Path) -> FunASRRun:
        calls["worker"] += 1
        raw_output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ASR_FIXTURE, raw_output_path)
        return FunASRRun(
            package_version="fixture",
            models={"asr": "a", "vad": "b", "punc": "c"},
            parameters={"fixture": True},
            started_at="fixture-start",
            completed_at="fixture-end",
            exit_status=0,
        )

    return run


def _mcp(tool: str, arguments: dict[str, object]) -> dict[str, object]:
    response = roughcut.mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": "media-start",
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
    )
    assert response is not None
    result = response["result"]
    assert isinstance(result, dict)
    payload = result["structuredContent"]
    assert isinstance(payload, dict)
    return payload


@pytest.fixture
def fake_proxy(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    calls = {"worker": 0, "verify": 0}
    monkeypatch.setattr(
        "roughcut.application.proxies.reject_unsupported_color",
        lambda *_args, **_kwargs: None,
    )

    def transcode(
        *_args: object,
        output_path: Path,
        **_kwargs: object,
    ) -> None:
        calls["worker"] += 1
        output_path.write_bytes(b"fixture-proxy")

    def verify(
        *_args: object,
        **_kwargs: object,
    ) -> ProxyVerificationReport:
        calls["verify"] += 1
        return ProxyVerificationReport(
            duration_ticks=240_000,
            checks={"verified": True},
            probe={"format": "mp4", "video_codec": "h264"},
            padded_video_frames=0,
            padded_audio_samples=0,
            leading_video_frames=0,
            leading_audio_samples=0,
        )

    monkeypatch.setattr(
        "roughcut.application.proxies.transcode_proxy",
        transcode,
    )
    monkeypatch.setattr(
        "roughcut.application.proxies.verify_proxy_output",
        verify,
    )
    return calls


def test_public_proxy_start_response_loss_readback_and_status_share_one_record(
    tmp_path: Path,
    fake_runtime: object,
    fake_proxy: dict[str, int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    del fake_runtime
    root, _source_path, source = _source_project(tmp_path, video=True)
    operation_id = "op_public_proxy"

    roughcut.cli.main(
        [
            "proxy-create",
            "--project",
            str(root),
            "--operation-id",
            operation_id,
            "--source-id",
            source.source_id,
            "--expected-revision",
            "0",
            "--json",
        ]
    )
    lost_response = json.loads(capsys.readouterr().out)
    assert lost_response["operation_readback"] is False
    assert lost_response["media_operation"]["status"] == "succeeded"
    assert fake_proxy["worker"] == 1

    readback = _mcp(
        "proxy_create",
        {
            "project_path": str(root),
            "operation_id": operation_id,
            "source_id": source.source_id,
            "expected_revision": 0,
        },
    )
    assert readback["operation_readback"] is True
    assert readback["proxy"] is None
    assert readback["media_operation"] == lost_response["media_operation"]
    assert fake_proxy["worker"] == 1

    status = _mcp(
        "media_operation_status",
        {"project_path": str(root), "operation_id": operation_id},
    )
    assert status["media_operation"] == lost_response["media_operation"]

    conflict = _mcp(
        "proxy_create",
        {
            "project_path": str(root),
            "operation_id": operation_id,
            "source_id": source.source_id,
            "expected_revision": 1,
        },
    )
    assert conflict["error"] == {"code": "operation_input_conflict"}
    assert fake_proxy["worker"] == 1


def test_mcp_duplicate_operation_delivery_uses_existing_first_once(
    tmp_path: Path,
    fake_runtime: object,
    fake_proxy: dict[str, int],
) -> None:
    del fake_runtime
    root, _source_path, source = _source_project(tmp_path, video=True)
    arguments = {
        "project_path": str(root),
        "operation_id": "op_mcp_duplicate",
        "source_id": source.source_id,
        "expected_revision": 0,
    }
    first = _mcp("proxy_create", arguments)
    files_after_first = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.name.endswith(".writer.lock")
    }
    second = _mcp("proxy_create", arguments)

    assert first["operation_readback"] is False
    assert second["operation_readback"] is True
    assert second["media_operation"] == first["media_operation"]
    assert fake_proxy == {"worker": 1, "verify": 2}
    assert {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.name.endswith(".writer.lock")
    } == files_after_first


def test_public_proxy_runtime_preflight_failure_creates_no_record_or_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, _source_path, source = _source_project(tmp_path, video=True)
    project_before = (root / "project.json").read_bytes()
    monkeypatch.setattr(
        operations_module,
        "_load_persistent_runtime",
        lambda: (_ for _ in ()).throw(
            RuntimeBindingError(
                "/private/runtime.json stderr token=secret"
            )
        ),
    )

    with pytest.raises(SystemExit) as exit_info:
        roughcut.cli.main(
            [
                "proxy-create",
                "--project",
                str(root),
                "--operation-id",
                "op_public_preflight_failure",
                "--source-id",
                source.source_id,
                "--expected-revision",
                "0",
                "--json",
            ]
        )

    output = capsys.readouterr()
    assert exit_info.value.code == 2
    assert output.err == ""
    assert json.loads(output.out)["error"] == {
        "code": "proxy_operation_failed"
    }
    assert "/private/" not in output.out
    status = _mcp(
        "media_operation_status",
        {
            "project_path": str(root),
            "operation_id": "op_public_preflight_failure",
        },
    )
    assert status["error"] == {"code": "operation_not_found"}
    assert (root / "project.json").read_bytes() == project_before
    assert not (
        root
        / "workflow"
        / "operations"
        / "media"
        / "op_public_preflight_failure.json"
    ).exists()
    assert not (root / "proxies").exists()


def test_public_proxy_worker_failure_is_closed_and_same_id_does_not_restart(
    tmp_path: Path,
    fake_runtime: object,
    fake_proxy: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del fake_runtime, fake_proxy
    root, _source_path, source = _source_project(tmp_path, video=True)
    project_before = (root / "project.json").read_bytes()
    worker_calls = 0

    def fail_worker(*_args: object, **_kwargs: object) -> None:
        nonlocal worker_calls
        worker_calls += 1
        raise RuntimeError(
            "/private/source.mp4 stderr token=secret"
        )

    monkeypatch.setattr(
        "roughcut.application.proxies.transcode_proxy",
        fail_worker,
    )
    arguments = {
        "project_path": str(root),
        "operation_id": "op_public_worker_failure",
        "source_id": source.source_id,
        "expected_revision": 0,
    }

    failed = _mcp("proxy_create", arguments)
    assert failed["error"] == {"code": "proxy_operation_failed"}
    assert worker_calls == 1
    status = _mcp(
        "media_operation_status",
        {
            "project_path": str(root),
            "operation_id": "op_public_worker_failure",
        },
    )
    assert status["media_operation"]["status"] == "failed"
    assert "/private/" not in json.dumps(status)
    assert "stderr" not in json.dumps(status)
    assert "token=secret" not in json.dumps(status)

    readback = _mcp("proxy_create", arguments)
    assert readback["operation_readback"] is True
    assert readback["media_operation"] == status["media_operation"]
    assert worker_calls == 1
    assert (root / "project.json").read_bytes() == project_before
    assert not list(root.rglob("proxy.mp4"))
    assert not list(root.rglob("manifest.json"))
    assert not list(root.rglob(".candidate-*"))


def test_public_transcription_start_and_same_id_status_use_tracked_coordinator(
    tmp_path: Path,
    fake_runtime: object,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    del fake_runtime
    root, _source_path, source = _source_project(tmp_path, video=False)
    _authorize_transcription(root, source.source_id)
    calls = {"worker": 0}
    internal_transcribe = operations_module.transcribe_source

    def transcribe_fixture(
        project_path: Path,
        source_id: str,
        *,
        expected_revision: int,
        runner: object = None,
        config: object = None,
        phase_callback: object = None,
    ) -> object:
        del runner
        return internal_transcribe(
            project_path,
            source_id,
            expected_revision=expected_revision,
            runner=_fixture_runner(calls),
            config=config,
            phase_callback=phase_callback,
        )

    monkeypatch.setattr(
        operations_module,
        "transcribe_source",
        transcribe_fixture,
    )
    operation_id = "op_public_transcribe"
    roughcut.cli.main(
        [
            "transcribe-source",
            "--project",
            str(root),
            "--operation-id",
            operation_id,
            "--source-id",
            source.source_id,
            "--expected-revision",
            "0",
            "--json",
        ]
    )
    started = json.loads(capsys.readouterr().out)
    assert started["operation_readback"] is False
    assert started["media_operation"]["status"] == "succeeded"
    assert calls["worker"] == 1

    status = _mcp(
        "media_operation_status",
        {"project_path": str(root), "operation_id": operation_id},
    )
    assert status["media_operation"] == started["media_operation"]

    readback = _mcp(
        "transcribe_source",
        {
            "project_path": str(root),
            "operation_id": operation_id,
            "source_id": source.source_id,
            "expected_revision": 0,
            "speaker_diarization": False,
        },
    )
    assert readback["operation_readback"] is True
    assert readback["transcript"] is None
    assert readback["media_operation"] == started["media_operation"]
    assert calls["worker"] == 1


def test_transcription_operation_binds_exact_result_and_readback_skips_preflight(
    tmp_path: Path,
    fake_runtime: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, source_path, source = _source_project(tmp_path, video=False)
    _authorize_transcription(root, source.source_id)
    calls = {"worker": 0, "runtime": 0, "drift": 0}
    original_runtime = operations_module._load_persistent_runtime
    original_validate = operations_module._validate_media_runtime

    def load_runtime() -> object:
        calls["runtime"] += 1
        return original_runtime()

    monkeypatch.setattr(
        operations_module,
        "_load_persistent_runtime",
        load_runtime,
    )

    def validate_runtime(runtime: object) -> tuple[str, str]:
        calls["drift"] += 1
        return original_validate(runtime)

    monkeypatch.setattr(
        operations_module,
        "_validate_media_runtime",
        validate_runtime,
    )
    stage_before = WorkflowStore(root).read_run("wfr_media").stage
    first = run_transcription_operation(
        root,
        operation_id="op_transcribe",
        source_id=source.source_id,
        expected_project_revision=0,
        speaker_diarization=False,
        runner=_fixture_runner(calls),
    )
    assert first.record.status == "succeeded"
    assert first.record.result_ref is not None
    transcript_id = first.record.result_ref.transcript_version_id
    assert ProjectStore(root).load().active_transcript_versions == {
        source.source_id: transcript_id
    }
    bound = WorkflowStore(root).read_run("wfr_media")
    assert bound.stage == stage_before
    assert bound.ordered_bindings[0].transcript_version_id == transcript_id
    assert ProjectStore(root).load().revision == 1

    source_path.write_bytes(b"changed-after-success")
    ProjectStore(root).save(
        replace(ProjectStore(root).load(), revision=2),
        expected_revision=1,
    )
    monkeypatch.setattr(
        operations_module,
        "_load_persistent_runtime",
        lambda: (_ for _ in ()).throw(
            AssertionError("readback re-ran runtime preflight")
        ),
    )
    second = run_transcription_operation(
        root,
        operation_id="op_transcribe",
        source_id=source.source_id,
        expected_project_revision=0,
        speaker_diarization=False,
        runner=_fixture_runner(calls),
    )
    assert second.readback is True
    assert second.record == first.record
    assert calls == {"worker": 1, "runtime": 1, "drift": 1}


def test_transcription_publishes_adjacent_overlap_normalization_without_rewriting_raw(
    tmp_path: Path,
    fake_runtime: object,
) -> None:
    root, _source_path, source = _source_project(tmp_path, video=False)
    _authorize_transcription(root, source.source_id)
    raw_payload = [
        {
            "text": "合成",
            "sentence_info": [
                {"text": "甲", "start": 0, "end": 1000},
                {"text": "乙", "start": 980, "end": 1500},
            ],
        }
    ]
    raw_bytes = json.dumps(raw_payload, ensure_ascii=False).encode("utf-8")

    def overlap_runner(_source: Path, raw_output: Path) -> FunASRRun:
        raw_output.parent.mkdir(parents=True, exist_ok=True)
        raw_output.write_bytes(raw_bytes)
        return FunASRRun(
            package_version="fixture",
            models={"asr": "fixture"},
            parameters={"fixture": True},
            started_at="fixture-start",
            completed_at="fixture-end",
            exit_status=0,
        )

    outcome = run_transcription_operation(
        root,
        operation_id="op_adjacent_overlap_publish",
        source_id=source.source_id,
        expected_project_revision=0,
        speaker_diarization=False,
        runner=overlap_runner,
    )

    assert outcome.record.status == "succeeded"
    assert outcome.record.result_ref is not None
    raw_files = list((root / "raw-asr" / source.source_id).glob("*.json"))
    assert len(raw_files) == 1
    assert raw_files[0].read_bytes() == raw_bytes
    assert json.loads(raw_files[0].read_text(encoding="utf-8")) == raw_payload
    transcript_id = outcome.record.result_ref.transcript_version_id
    transcript_payload = json.loads(
        (
            root
            / "transcripts"
            / source.source_id
            / f"{transcript_id}.json"
        ).read_text(encoding="utf-8")
    )
    assert [
        (segment["start_ticks"], segment["end_ticks"])
        for segment in transcript_payload["segments"]
    ] == [(0, 117_600), (117_600, 180_000)]
    assert ProjectStore(root).load().active_transcript_versions[source.source_id] == transcript_id


@pytest.mark.parametrize(
    ("operation_id", "end_milliseconds", "expected_status"),
    (
        ("op_bounded_overrun", 3010, "succeeded"),
        ("op_large_overrun", 3020, "failed"),
    ),
)
def test_transcription_operation_handles_only_bounded_final_overrun(
    tmp_path: Path,
    fake_runtime: object,
    operation_id: str,
    end_milliseconds: int,
    expected_status: str,
) -> None:
    root, _source_path, source = _source_project(tmp_path, video=False)
    _authorize_transcription(root, source.source_id)

    def overrun_runner(_source: Path, raw_output: Path) -> FunASRRun:
        raw_output.parent.mkdir(parents=True, exist_ok=True)
        raw_output.write_text(
            json.dumps(
                [
                    {
                        "text": "甲乙",
                        "sentence_info": [
                            {"text": "甲", "start": 0, "end": 1000},
                            {
                                "text": "乙",
                                "start": 1000,
                                "end": end_milliseconds,
                            },
                        ],
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return FunASRRun(
            package_version="1.3.14",
            models={"asr": "fixture"},
            parameters={"fixture": True},
            started_at="fixture-start",
            completed_at="fixture-end",
            exit_status=0,
        )

    if expected_status == "failed":
        with pytest.raises(TranscriptNormalizationError, match="exceeds source duration"):
            run_transcription_operation(
                root,
                operation_id=operation_id,
                source_id=source.source_id,
                expected_project_revision=0,
                speaker_diarization=False,
                runner=overrun_runner,
            )
    else:
        outcome = run_transcription_operation(
            root,
            operation_id=operation_id,
            source_id=source.source_id,
            expected_project_revision=0,
            speaker_diarization=False,
            runner=overrun_runner,
        )
        assert outcome.record.status == "succeeded"
        assert outcome.record.result_ref is not None

    status = media_operation_status(root, operation_id)
    assert status.status == expected_status
    if expected_status == "failed":
        assert status.result_ref is None
        assert status.error is not None
        assert status.error.responsibility == "roughcut_core"
        assert status.error.action == "normalize_transcript"


def test_transcription_stale_revision_and_changed_request_fail_without_worker(
    tmp_path: Path,
    fake_runtime: object,
) -> None:
    root, _source_path, source = _source_project(tmp_path, video=False)
    _authorize_transcription(root, source.source_id)
    calls = {"worker": 0}
    with pytest.raises(ProjectError, match="revision"):
        run_transcription_operation(
            root,
            operation_id="op_stale",
            source_id=source.source_id,
            expected_project_revision=1,
            speaker_diarization=False,
            runner=_fixture_runner(calls),
        )
    store = MediaOperationStore(root, ProjectStore(root).load().project_id)
    assert store.read("op_stale") is None

    completed = run_transcription_operation(
        root,
        operation_id="op_conflict",
        source_id=source.source_id,
        expected_project_revision=0,
        speaker_diarization=False,
        runner=_fixture_runner(calls),
    )
    with pytest.raises(MediaOperationError) as raised:
        run_transcription_operation(
            root,
            operation_id="op_conflict",
            source_id=source.source_id,
            expected_project_revision=0,
            speaker_diarization=True,
            runner=_fixture_runner(calls),
        )
    assert raised.value.code == "operation_input_conflict"
    assert store.read("op_conflict") == completed.record
    assert calls["worker"] == 1


@pytest.mark.parametrize(
    ("field", "changed"),
    (
        ("source_id", "src_changed"),
        ("expected_project_revision", 99),
        ("speaker_diarization", True),
        ("timeout_milliseconds", 1),
    ),
)
def test_mor_025_transcription_rejects_each_changed_public_request_field(
    tmp_path: Path,
    fake_runtime: object,
    field: str,
    changed: object,
) -> None:
    root, _source_path, source = _source_project(tmp_path, video=False)
    _authorize_transcription(root, source.source_id)
    calls = {"worker": 0}
    original = run_transcription_operation(
        root,
        operation_id="op_all_transcription_fields",
        source_id=source.source_id,
        expected_project_revision=0,
        speaker_diarization=False,
        timeout_milliseconds=3_600_000,
        runner=_fixture_runner(calls),
    )
    before = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    request: dict[str, object] = {
        "operation_id": "op_all_transcription_fields",
        "source_id": source.source_id,
        "expected_project_revision": 0,
        "speaker_diarization": False,
        "timeout_milliseconds": 3_600_000,
        "runner": _fixture_runner(calls),
    }
    request[field] = changed
    with pytest.raises(MediaOperationError) as conflict:
        run_transcription_operation(root, **request)
    assert conflict.value.code == "operation_input_conflict"
    assert calls["worker"] == 1
    store = MediaOperationStore(root, ProjectStore(root).load().project_id)
    assert store.read("op_all_transcription_fields") == original.record
    assert {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    } == before


def test_proxy_operation_pair_publish_reuse_and_project_invariants(
    tmp_path: Path,
    fake_runtime: object,
    fake_proxy: dict[str, int],
) -> None:
    root, _source_path, source = _source_project(tmp_path, video=True)
    first = run_proxy_operation(
        root,
        operation_id="op_proxy_first",
        source_id=source.source_id,
        expected_project_revision=0,
    )
    assert first.record.status == "succeeded"
    assert first.record.result_ref is not None
    result = first.record.result_ref
    assert (root / result.output_relative_path).is_file()
    assert (
        root
        / "proxies"
        / source.source_id
        / result.cache_key
        / "manifest.json"
    ).is_file()
    assert ProjectStore(root).load().revision == 0

    second = run_proxy_operation(
        root,
        operation_id="op_proxy_cached",
        source_id=source.source_id,
        expected_project_revision=0,
    )
    assert second.record.status == "succeeded"
    assert second.result is not None and second.result.reused is True
    assert fake_proxy["worker"] == 1
    assert ProjectStore(root).load().revision == 0


def test_proxy_new_execution_validates_runtime_once_and_existing_first_skips_it(
    tmp_path: Path,
    fake_runtime: object,
    fake_proxy: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _source_path, source = _source_project(tmp_path, video=True)
    checks = 0

    def validate(_runtime: object) -> tuple[str, str]:
        nonlocal checks
        checks += 1
        return "ffmpeg version fixture", "ffprobe version fixture"

    monkeypatch.setattr(operations_module, "_validate_media_runtime", validate)
    arguments = {
        "operation_id": "op_proxy_drift_once",
        "source_id": source.source_id,
        "expected_project_revision": 0,
    }
    first = run_proxy_operation(root, **arguments)
    readback = run_proxy_operation(root, **arguments)

    assert first.record.status == "succeeded"
    assert readback.readback is True
    assert checks == 1
    assert fake_proxy["worker"] == 1


def test_proxy_runtime_drift_writes_closed_failure_before_worker(
    tmp_path: Path,
    fake_runtime: object,
    fake_proxy: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del fake_runtime
    root, _source_path, source = _source_project(tmp_path, video=True)

    def fail(_runtime: object) -> tuple[str, str]:
        raise RuntimeBindingError("fixture drift")

    monkeypatch.setattr(operations_module, "_validate_media_runtime", fail)
    with pytest.raises(RuntimeBindingError, match="fixture drift"):
        run_proxy_operation(
            root,
            operation_id="op_proxy_drift_failure",
            source_id=source.source_id,
            expected_project_revision=0,
        )

    record = media_operation_status(root, "op_proxy_drift_failure")
    assert record.status == "failed"
    assert record.result_ref is None
    assert fake_proxy["worker"] == 0
    assert not (root / "proxies").exists()
    assert not (root / "workflow" / "runs").exists()


def test_proxy_same_id_conflict_and_terminal_readback_ignore_current_basis(
    tmp_path: Path,
    fake_runtime: object,
    fake_proxy: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, source_path, source = _source_project(tmp_path, video=True)
    first = run_proxy_operation(
        root,
        operation_id="op_proxy",
        source_id=source.source_id,
        expected_project_revision=0,
    )
    with pytest.raises(MediaOperationError) as raised:
        run_proxy_operation(
            root,
            operation_id="op_proxy",
            source_id=source.source_id,
            expected_project_revision=1,
        )
    assert raised.value.code == "operation_input_conflict"

    source_path.write_bytes(b"changed-after-success")
    ProjectStore(root).save(
        replace(ProjectStore(root).load(), revision=1),
        expected_revision=0,
    )
    monkeypatch.setattr(
        operations_module,
        "_load_persistent_runtime",
        lambda: (_ for _ in ()).throw(
            AssertionError("readback re-ran runtime preflight")
        ),
    )
    readback = run_proxy_operation(
        root,
        operation_id="op_proxy",
        source_id=source.source_id,
        expected_project_revision=0,
    )
    assert readback.record == first.record
    assert readback.readback is True
    assert fake_proxy["worker"] == 1


@pytest.mark.parametrize(
    ("field", "changed"),
    (
        ("source_id", "src_changed"),
        ("expected_project_revision", 99),
    ),
)
def test_mor_025_proxy_rejects_each_changed_public_request_field(
    tmp_path: Path,
    fake_runtime: object,
    fake_proxy: dict[str, int],
    field: str,
    changed: object,
) -> None:
    root, _source_path, source = _source_project(tmp_path, video=True)
    original = run_proxy_operation(
        root,
        operation_id="op_all_proxy_fields",
        source_id=source.source_id,
        expected_project_revision=0,
    )
    before = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    request: dict[str, object] = {
        "operation_id": "op_all_proxy_fields",
        "source_id": source.source_id,
        "expected_project_revision": 0,
    }
    request[field] = changed
    with pytest.raises(MediaOperationError) as conflict:
        run_proxy_operation(root, **request)
    assert conflict.value.code == "operation_input_conflict"
    assert fake_proxy["worker"] == 1
    store = MediaOperationStore(root, ProjectStore(root).load().project_id)
    assert store.read("op_all_proxy_fields") == original.record
    assert {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    } == before


def test_live_writer_second_call_returns_running_without_starting_worker(
    tmp_path: Path,
    fake_runtime: object,
    fake_proxy: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _source_path, source = _source_project(tmp_path, video=True)
    entered = threading.Event()
    release = threading.Event()
    original = operations_module.create_proxy

    def blocked(*args: object, **kwargs: object):
        entered.set()
        assert release.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(operations_module, "create_proxy", blocked)
    outcomes: list[object] = []
    worker = threading.Thread(
        target=lambda: outcomes.append(
            run_proxy_operation(
                root,
                operation_id="op_live",
                source_id=source.source_id,
                expected_project_revision=0,
            )
        )
    )
    worker.start()
    assert entered.wait(timeout=5)
    live = run_proxy_operation(
        root,
        operation_id="op_live",
        source_id=source.source_id,
        expected_project_revision=0,
    )
    assert live.readback is True
    assert live.record.status == "running"
    assert live.record.phase_message_code == "proxy_preparing"
    assert fake_proxy["worker"] == 0
    release.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert fake_proxy["worker"] == 1


def _abandoned_record(
    store: MediaOperationStore,
    operation_id: str,
    *,
    status: str,
) -> MediaOperationRecord:
    pending = MediaOperationRecord(
        operation_id=operation_id,
        scope=store.scope,
        operation_type="proxy_create",
        request_hash="a" * 64,
        input_hash="b" * 64,
        status="pending",
        phase_message_code="proxy_preparing",
        created_at="2026-07-29T08:00:00.000000Z",
        started_at=None,
        updated_at="2026-07-29T08:00:00.000000Z",
        finished_at=None,
        result_ref=None,
        error=None,
    )
    if status == "pending":
        return pending
    return replace(
        pending,
        status="running",
        started_at="2026-07-29T08:00:01.000000Z",
        updated_at="2026-07-29T08:00:01.000000Z",
    )


@pytest.mark.parametrize("initial_status", ("pending", "running"))
def test_status_converges_abandoned_nonterminal_without_artifact_inference(
    tmp_path: Path,
    initial_status: str,
) -> None:
    root, _source_path, _source = _source_project(tmp_path, video=True)
    store = MediaOperationStore(root, ProjectStore(root).load().project_id)
    record = _abandoned_record(
        store,
        f"op_abandoned_{initial_status}",
        status=initial_status,
    )
    with store.writer(record.operation_id, create=True) as acquired:
        assert acquired
        store.write_locked(record)
    (root / "proxies" / "untrusted.mp4").parent.mkdir()
    (root / "proxies" / "untrusted.mp4").write_bytes(b"artifact")

    interrupted = media_operation_status(root, record.operation_id)
    assert interrupted.status == "interrupted"
    assert interrupted.result_ref is None
    assert interrupted.error == MediaOperationFailure(
        code="media_operation_interrupted",
        responsibility="roughcut_core",
        action="recover_abandoned_media_operation",
        message_code="proxy_interrupted",
    )


def test_status_missing_is_side_effect_free_and_terminal_is_pure(
    tmp_path: Path,
) -> None:
    root, _source_path, _source = _source_project(tmp_path, video=True)
    with pytest.raises(MediaOperationError) as missing:
        media_operation_status(root, "op_missing")
    assert missing.value.code == "operation_not_found"
    assert not (root / "workflow").exists()

    store = MediaOperationStore(root, ProjectStore(root).load().project_id)
    running = _abandoned_record(store, "op_terminal", status="running")
    terminal = replace(
        running,
        status="failed",
        phase_message_code="proxy_failed",
        updated_at="2026-07-29T08:00:02.000000Z",
        finished_at="2026-07-29T08:00:02.000000Z",
        error=MediaOperationFailure(
            code="media_operation_failed",
            responsibility="ffmpeg_proxy",
            action="encode_proxy",
            message_code="proxy_failed",
        ),
    )
    with store.writer(terminal.operation_id, create=True) as acquired:
        assert acquired
        store.write_locked(terminal)
    assert media_operation_status(root, terminal.operation_id) == terminal


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
@pytest.mark.parametrize(
    ("exit_status", "expected_status"),
    (("pending", "interrupted"), ("running", "interrupted")),
)
def test_hard_exit_at_nonterminal_publication_converges_interrupted(
    tmp_path: Path,
    exit_status: str,
    expected_status: str,
) -> None:
    root, _source_path, _source = _source_project(tmp_path, video=True)
    store = MediaOperationStore(root, ProjectStore(root).load().project_id)
    operation_id = f"op_exit_{exit_status}"
    child = os.fork()
    if child == 0:
        child_store = MediaOperationStore(
            root, ProjectStore(root).load().project_id
        )
        with child_store.writer(operation_id, create=True) as acquired:
            if not acquired:
                os._exit(91)
            record = _abandoned_record(
                child_store,
                operation_id,
                status=exit_status,
            )
            child_store.write_locked(record)
            os._exit(0)
    waited, status = os.waitpid(child, 0)
    assert waited == child
    assert os.waitstatus_to_exitcode(status) == 0
    assert store.read(operation_id) is not None
    assert media_operation_status(root, operation_id).status == expected_status


def test_inherited_media_writer_stays_live_until_fake_child_exits(
    tmp_path: Path,
) -> None:
    root, _source_path, _source = _source_project(tmp_path, video=True)
    project_id = ProjectStore(root).load().project_id
    store = MediaOperationStore(root, project_id)
    operation_id = "op_inherited_child"
    record = _abandoned_record(store, operation_id, status="running")
    with store.writer(operation_id, create=True) as acquired:
        assert acquired
        store.write_locked(record)

    entered = tmp_path / "child-entered"
    release = tmp_path / "child-release"
    output = tmp_path / "child-output"
    child_script = """
import sys
import time
from pathlib import Path

entered, release, output = map(Path, sys.argv[1:])
entered.write_text("ready", encoding="utf-8")
while not release.exists():
    with output.open("ab") as stream:
        stream.write(b"x")
        stream.flush()
    time.sleep(0.02)
"""
    owner_script = """
import os
import subprocess
import sys
import time
from pathlib import Path

from roughcut.adapters.media_operation_store import (
    MediaOperationStore,
    media_child_process_kwargs,
)

root = Path(sys.argv[1])
project_id = sys.argv[2]
operation_id = sys.argv[3]
child_script = sys.argv[4]
paths = sys.argv[5:]
store = MediaOperationStore(root, project_id)
with store.writer(operation_id, create=False) as acquired:
    if not acquired:
        os._exit(91)
    subprocess.Popen(
        [sys.executable, "-c", child_script, *paths],
        **media_child_process_kwargs(),
    )
    entered = Path(paths[0])
    deadline = time.monotonic() + 2
    while not entered.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    os._exit(0 if entered.exists() else 92)
"""
    owner = subprocess.run(
        [
            sys.executable,
            "-c",
            owner_script,
            str(root),
            project_id,
            operation_id,
            child_script,
            str(entered),
            str(release),
            str(output),
        ],
        check=False,
    )
    assert owner.returncode == 0
    assert entered.read_text(encoding="utf-8") == "ready"
    try:
        first_size = output.stat().st_size
        deadline = time.monotonic() + 2
        while output.stat().st_size <= first_size and time.monotonic() < deadline:
            time.sleep(0.01)
        assert output.stat().st_size > first_size
        live = media_operation_status(root, operation_id)
        assert live.status == "running"
        assert live.finished_at is None
    finally:
        release.write_text("", encoding="utf-8")
    interrupted = None
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        observed = media_operation_status(root, operation_id)
        if observed.status == "interrupted":
            interrupted = observed
            break
        assert observed.status == "running"
        time.sleep(0.01)
    assert interrupted is not None
    assert media_operation_status(root, operation_id) == interrupted


def test_proxy_failure_record_is_closed_and_candidate_cleanup_is_preserved(
    tmp_path: Path,
    fake_runtime: object,
    fake_proxy: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _source_path, source = _source_project(tmp_path, video=True)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("/private/source.mp4 stderr token=secret")

    monkeypatch.setattr(
        "roughcut.application.proxies.transcode_proxy",
        fail,
    )
    with pytest.raises(RuntimeError):
        run_proxy_operation(
            root,
            operation_id="op_proxy_failure",
            source_id=source.source_id,
            expected_project_revision=0,
        )
    record = media_operation_status(root, "op_proxy_failure")
    assert record.status == "failed"
    encoded = json.dumps(record.to_dict())
    assert "/private" not in encoded
    assert "stderr" not in encoded
    assert "secret" not in encoded
    assert record.error is not None
    assert record.error.responsibility == "roughcut_core"
    assert not list((root / "proxies").rglob(".candidate-*"))


def test_mor_017_asr_worker_failure_is_closed_and_does_not_leak_details(
    tmp_path: Path,
    fake_runtime: object,
) -> None:
    root, _source_path, source = _source_project(tmp_path, video=False)
    _authorize_transcription(root, source.source_id)
    calls = 0

    def fail_worker(_source: Path, _raw_output: Path) -> FunASRRun:
        nonlocal calls
        calls += 1
        raise FunASRRunnerError(
            "/private/model stderr traceback token=secret"
        )

    with pytest.raises(FunASRRunnerError):
        run_transcription_operation(
            root,
            operation_id="op_asr_worker_failure",
            source_id=source.source_id,
            expected_project_revision=0,
            speaker_diarization=False,
            runner=fail_worker,
        )
    record = media_operation_status(root, "op_asr_worker_failure")
    assert record.status == "failed"
    assert record.error is not None
    assert record.error.responsibility == "asr_worker"
    assert record.error.action == "run_asr_worker"
    assert calls == 1
    encoded = json.dumps(record.to_dict())
    assert "/private" not in encoded
    assert "stderr" not in encoded
    assert "traceback" not in encoded
    assert "secret" not in encoded
    assert ProjectStore(root).load().active_transcript_versions == {}


def test_mor_017_ffmpeg_proxy_failure_is_closed_and_cleans_candidate(
    tmp_path: Path,
    fake_runtime: object,
    fake_proxy: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _source_path, source = _source_project(tmp_path, video=True)
    calls = 0

    def fail_encode(*_args: object, **_kwargs: object) -> None:
        nonlocal calls
        calls += 1
        raise FFmpegProxyError(
            "/private/source.mp4 stderr traceback token=secret"
        )

    monkeypatch.setattr(
        "roughcut.application.proxies.transcode_proxy",
        fail_encode,
    )
    with pytest.raises(FFmpegProxyError):
        run_proxy_operation(
            root,
            operation_id="op_ffmpeg_proxy_failure",
            source_id=source.source_id,
            expected_project_revision=0,
        )
    record = media_operation_status(root, "op_ffmpeg_proxy_failure")
    assert record.status == "failed"
    assert record.error is not None
    assert record.error.responsibility == "ffmpeg_proxy"
    assert record.error.action == "encode_proxy"
    assert calls == 1
    encoded = json.dumps(record.to_dict())
    assert "/private" not in encoded
    assert "stderr" not in encoded
    assert "traceback" not in encoded
    assert "secret" not in encoded
    assert not list((root / "proxies").rglob(".candidate-*"))


def test_proxy_project_change_after_encode_fails_before_pair_publish(
    tmp_path: Path,
    fake_runtime: object,
    fake_proxy: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _source_path, source = _source_project(tmp_path, video=True)

    def mutate_project(
        *_args: object,
        output_path: Path,
        **_kwargs: object,
    ) -> None:
        output_path.write_bytes(b"candidate-proxy")
        store = ProjectStore(root)
        current = store.load()
        store.save(
            replace(current, revision=current.revision + 1),
            expected_revision=current.revision,
        )

    monkeypatch.setattr(
        "roughcut.application.proxies.transcode_proxy",
        mutate_project,
    )
    with pytest.raises(ProjectError, match="changed"):
        run_proxy_operation(
            root,
            operation_id="op_proxy_stale_publish",
            source_id=source.source_id,
            expected_project_revision=0,
        )
    record = media_operation_status(root, "op_proxy_stale_publish")
    assert record.status == "failed"
    assert record.error is not None
    assert record.error.responsibility == "user_input"
    assert record.error.action == "validate_proxy_basis"
    assert not list((root / "proxies").rglob("manifest.json"))
    assert not list((root / "proxies").rglob(".candidate-*"))


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_transcription_hard_exit_before_publish_is_not_adopted(
    tmp_path: Path,
    fake_runtime: object,
) -> None:
    root, _source_path, source = _source_project(tmp_path, video=False)
    _authorize_transcription(root, source.source_id)

    def exit_runner(_source: Path, raw_output: Path) -> FunASRRun:
        raw_output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ASR_FIXTURE, raw_output)
        os._exit(0)

    child = os.fork()
    if child == 0:
        run_transcription_operation(
            root,
            operation_id="op_asr_exit_before_publish",
            source_id=source.source_id,
            expected_project_revision=0,
            speaker_diarization=False,
            runner=exit_runner,
        )
        os._exit(90)
    _pid, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    record = media_operation_status(root, "op_asr_exit_before_publish")
    assert record.status == "interrupted"
    assert record.result_ref is None
    assert ProjectStore(root).load().active_transcript_versions == {}
    assert ProjectStore(root).load().revision == 0
    assert list((root / "raw-asr" / source.source_id).glob("*.json"))
    assert not list((root / "transcripts" / source.source_id).glob("*.json"))


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_transcription_hard_exit_after_project_commit_keeps_binding_repairable(
    tmp_path: Path,
    fake_runtime: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _source_path, source = _source_project(tmp_path, video=False)
    _authorize_transcription(root, source.source_id)
    calls = {"worker": 0}
    monkeypatch.setattr(
        operations_module,
        "synchronize_workflow_transcript_binding",
        lambda *_args, **_kwargs: os._exit(0),
    )
    child = os.fork()
    if child == 0:
        run_transcription_operation(
            root,
            operation_id="op_asr_exit_after_commit",
            source_id=source.source_id,
            expected_project_revision=0,
            speaker_diarization=False,
            runner=_fixture_runner(calls),
        )
        os._exit(90)
    _pid, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    record = media_operation_status(root, "op_asr_exit_after_commit")
    assert record.status == "interrupted"
    assert record.result_ref is None
    project = ProjectStore(root).load()
    transcript_id = project.active_transcript_versions[source.source_id]
    assert project.revision == 1
    assert (
        WorkflowStore(root)
        .read_run("wfr_media")
        .ordered_bindings[0]
        .transcript_version_id
        is None
    )
    from roughcut.application.workflows import (
        synchronize_workflow_transcript_binding,
    )

    synchronize_workflow_transcript_binding(
        root,
        "wfr_media",
        source.source_id,
        transcript_id,
    )
    assert (
        WorkflowStore(root)
        .read_run("wfr_media")
        .ordered_bindings[0]
        .transcript_version_id
        == transcript_id
    )
    assert media_operation_status(root, record.operation_id) == record


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_hard_exit_after_terminal_publish_reads_back_succeeded(
    tmp_path: Path,
    fake_runtime: object,
    fake_proxy: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _source_path, source = _source_project(tmp_path, video=True)
    marker = tmp_path / "worker-called"

    def transcode(
        *_args: object,
        output_path: Path,
        **_kwargs: object,
    ) -> None:
        marker.write_bytes(b"called")
        output_path.write_bytes(b"fixture-proxy")

    monkeypatch.setattr(
        "roughcut.application.proxies.transcode_proxy",
        transcode,
    )
    original_write = MediaOperationStore.write_locked

    def exit_after_terminal(
        self: MediaOperationStore,
        record: MediaOperationRecord,
    ) -> MediaOperationRecord:
        stored = original_write(self, record)
        if record.status == "succeeded":
            os._exit(0)
        return stored

    monkeypatch.setattr(
        MediaOperationStore,
        "write_locked",
        exit_after_terminal,
    )
    child = os.fork()
    if child == 0:
        run_proxy_operation(
            root,
            operation_id="op_terminal_exit",
            source_id=source.source_id,
            expected_project_revision=0,
        )
        os._exit(90)
    _pid, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    monkeypatch.setattr(MediaOperationStore, "write_locked", original_write)
    succeeded = media_operation_status(root, "op_terminal_exit")
    assert succeeded.status == "succeeded"
    assert succeeded.result_ref is not None
    assert marker.read_bytes() == b"called"
    readback = run_proxy_operation(
        root,
        operation_id="op_terminal_exit",
        source_id=source.source_id,
        expected_project_revision=0,
    )
    assert readback.record == succeeded
    assert marker.read_bytes() == b"called"
