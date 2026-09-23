from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from windows.w6_support import (
    CORE_ROOT,
    build_persistent_runtime,
    make_av_source,
    runtime_environment,
    seed_source_project,
)

from roughcut.adapters.funasr.normalize import TranscriptNormalizationError
from roughcut.adapters.funasr.runner import FunASRRunnerError
from roughcut.adapters.project_store import ProjectStore
from roughcut.adapters.workflow_store import WorkflowStore
from roughcut.application.media_operations import (
    media_operation_status,
    run_transcription_operation,
)
from roughcut.application.transcription import read_transcript_page
from roughcut.application.workflows import workflow_action, workflow_start


@pytest.fixture
def asr_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    runtime = build_persistent_runtime(
        tmp_path / "asr runtime fixture",
        asr=True,
    )
    monkeypatch.setenv("ROUGHCUT_RUNTIME_BINDING", str(runtime))
    monkeypatch.delenv("ROUGHCUT_FFMPEG_COMMAND", raising=False)
    monkeypatch.delenv("ROUGHCUT_FFPROBE_COMMAND", raising=False)
    monkeypatch.delenv("ROUGHCUT_FUNASR_PYTHON", raising=False)
    monkeypatch.delenv("ROUGHCUT_FUNASR_MODEL_ROOT", raising=False)
    return runtime


def _authorize(root: Path, source_id: str) -> None:
    started = workflow_start(root, "wfr_w6_asr", [source_id])
    basis = started.status["confirmation_bases"]["scope"]["basis"]
    workflow_action(
        root,
        "wfr_w6_asr",
        "act_w6_asr_scope",
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


def test_w6_real_asr_pipeline_uses_ffmpeg_wav_worker_raw_normalize_and_readback(
    tmp_path: Path,
    asr_runtime: Path,
) -> None:
    source_path = tmp_path / "素材 中文 with spaces.mp4"
    make_av_source(source_path, color="purple", frequency=440, duration=1.4)
    project_path, imported, source = seed_source_project(
        tmp_path / "ASR project root", source_path
    )
    _authorize(project_path, source.source_id)
    before_project = (project_path / "project.json").read_bytes()
    before_pcm = set(Path(tempfile.gettempdir()).glob("roughcut-pcm-*"))

    outcome = run_transcription_operation(
        project_path,
        operation_id="op_w6_asr_success",
        source_id=source.source_id,
        expected_project_revision=imported.revision,
        speaker_diarization=False,
    )

    assert outcome.readback is False
    assert outcome.record.status == "succeeded"
    assert outcome.record.result_ref is not None
    transcript = outcome.result
    assert transcript is not None
    assert transcript.source_id == source.source_id
    assert transcript.provenance.parameters["input_type"] == "pcm_samples"
    assert transcript.provenance.parameters["decode"]["sample_rate_hz"] == 16_000
    assert transcript.provenance.parameters["decode"]["channels"] == 1
    assert transcript.provenance.parameters["decode"]["container_format"] == "wav"
    assert transcript.provenance.parameters["speaker_diarization"] == {"enabled": False}
    assert transcript.segments[0].original_text == "中文 Windows 路径。"
    assert transcript.segments[0].fine_units
    assert all(
        0 <= segment.start_ticks < segment.end_ticks <= source.probe.duration_ticks
        for segment in transcript.segments
    )

    raw_path = project_path / transcript.provenance.raw_result_path
    transcript_path = (
        project_path
        / "transcripts"
        / source.source_id
        / f"{transcript.transcript_version_id}.json"
    )
    assert raw_path.is_file()
    assert transcript_path.is_file()
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    assert raw[0]["sentence_info"][0]["text"] == "中文 Windows 路径。"
    assert json.loads(transcript_path.read_text(encoding="utf-8")) == transcript.to_dict()
    assert ProjectStore(project_path).load().revision == imported.revision + 1
    assert ProjectStore(project_path).load().active_transcript_versions == {
        source.source_id: transcript.transcript_version_id
    }
    workflow = WorkflowStore(project_path).read_run("wfr_w6_asr")
    assert workflow.ordered_bindings[0].transcript_version_id == (
        transcript.transcript_version_id
    )

    page = read_transcript_page(
        project_path,
        source.source_id,
        transcript.transcript_version_id,
        offset=0,
        limit=10,
    )
    assert page.total_segments == 1
    assert page.segments[0]["original_text"] == "中文 Windows 路径。"

    same_id = run_transcription_operation(
        project_path,
        operation_id="op_w6_asr_success",
        source_id=source.source_id,
        expected_project_revision=imported.revision,
        speaker_diarization=False,
    )
    assert same_id.readback is True
    assert same_id.result is None
    assert same_id.record == outcome.record
    assert media_operation_status(project_path, "op_w6_asr_success") == outcome.record

    environment = runtime_environment(asr_runtime)
    environment["PYTHONPATH"] = str(CORE_ROOT / "src")
    status = subprocess.run(
        [
            sys.executable,
            "-m",
            "roughcut.cli",
            "media-operation-status",
            "--project",
            str(project_path),
            "--operation-id",
            "op_w6_asr_success",
            "--json",
        ],
        cwd=CORE_ROOT,
        check=False,
        capture_output=True,
        text=False,
        env=environment,
    )
    assert status.returncode == 0, status.stderr.decode(errors="replace")
    assert status.stderr == b""
    status_payload = json.loads(status.stdout.decode("utf-8"))
    assert status_payload["media_operation"] == outcome.record.to_dict()
    assert status_payload["media_operation"]["result_ref"]["source_id"] == source.source_id

    assert (project_path / "project.json").read_bytes() != before_project
    after_pcm = set(Path(tempfile.gettempdir()).glob("roughcut-pcm-*"))
    assert after_pcm <= before_pcm


def test_w6_native_worker_noise_does_not_pollute_machine_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = build_persistent_runtime(
        tmp_path / "native noise ASR runtime fixture",
        asr=True,
        worker_mode="native_noise",
    )
    monkeypatch.setenv("ROUGHCUT_RUNTIME_BINDING", str(runtime))
    monkeypatch.delenv("ROUGHCUT_FFMPEG_COMMAND", raising=False)
    monkeypatch.delenv("ROUGHCUT_FFPROBE_COMMAND", raising=False)
    source_path = tmp_path / "native noise 中文 with spaces.mp4"
    make_av_source(source_path, color="yellow", frequency=660, duration=1.4)
    project_path, imported, source = seed_source_project(
        tmp_path / "Native noise ASR project root", source_path
    )
    _authorize(project_path, source.source_id)

    outcome = run_transcription_operation(
        project_path,
        operation_id="op_w6_asr_native_noise",
        source_id=source.source_id,
        expected_project_revision=imported.revision,
        speaker_diarization=False,
    )

    assert outcome.record.status == "succeeded"
    assert outcome.result is not None
    assert outcome.result.segments[0].original_text == "中文 Windows 路径。"


def test_w6_worker_nonzero_exit_is_tracked_and_does_not_publish_transcript(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = build_persistent_runtime(
        tmp_path / "failed ASR runtime fixture",
        asr=True,
        worker_mode="worker_failure",
    )
    monkeypatch.setenv("ROUGHCUT_RUNTIME_BINDING", str(runtime))
    monkeypatch.delenv("ROUGHCUT_FFMPEG_COMMAND", raising=False)
    monkeypatch.delenv("ROUGHCUT_FFPROBE_COMMAND", raising=False)
    source_path = tmp_path / "failed worker 中文 with spaces.mp4"
    make_av_source(source_path, color="red", frequency=330, duration=0.4)
    project_path, imported, source = seed_source_project(
        tmp_path / "Failed ASR project root", source_path
    )
    _authorize(project_path, source.source_id)
    before_project = (project_path / "project.json").read_bytes()

    with pytest.raises(FunASRRunnerError, match="status 1"):
        run_transcription_operation(
            project_path,
            operation_id="op_w6_asr_worker_failure",
            source_id=source.source_id,
            expected_project_revision=imported.revision,
            speaker_diarization=False,
        )

    record = media_operation_status(project_path, "op_w6_asr_worker_failure")
    assert record.status == "failed"
    assert record.result_ref is None
    assert record.error is not None
    assert record.error.responsibility == "asr_worker"
    assert record.error.action == "run_asr_worker"
    assert (project_path / "project.json").read_bytes() == before_project
    assert ProjectStore(project_path).load().active_transcript_versions == {}
    assert not list((project_path / "transcripts").rglob("*.json"))
    assert not list((project_path / "raw-asr" / source.source_id).glob("*.json"))
    assert not list(project_path.rglob("*.wav"))


def test_w6_worker_timeout_terminates_child_and_cleans_pcm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = build_persistent_runtime(
        tmp_path / "timeout ASR runtime fixture",
        asr=True,
        worker_mode="timeout",
    )
    monkeypatch.setenv("ROUGHCUT_RUNTIME_BINDING", str(runtime))
    monkeypatch.setenv("W6_WORKER_MARKER", str(tmp_path / "worker-marker.txt"))
    monkeypatch.delenv("ROUGHCUT_FFMPEG_COMMAND", raising=False)
    monkeypatch.delenv("ROUGHCUT_FFPROBE_COMMAND", raising=False)
    source_path = tmp_path / "timeout worker 中文 with spaces.mp4"
    make_av_source(source_path, color="blue", frequency=330, duration=0.4)
    project_path, imported, source = seed_source_project(
        tmp_path / "Timeout ASR project root", source_path
    )
    _authorize(project_path, source.source_id)

    with pytest.raises(FunASRRunnerError, match="could not complete"):
        run_transcription_operation(
            project_path,
            operation_id="op_w6_asr_timeout",
            source_id=source.source_id,
            expected_project_revision=imported.revision,
            speaker_diarization=False,
            timeout_milliseconds=1_500,
        )

    marker = tmp_path / "worker-marker.txt"
    time.sleep(0.5)
    assert marker.read_text(encoding="utf-8") == "started"
    record = media_operation_status(project_path, "op_w6_asr_timeout")
    assert record.status == "failed"
    assert record.error is not None
    assert record.error.responsibility == "asr_worker"
    assert record.error.action == "run_asr_worker"
    assert not list(project_path.rglob("*.wav"))


def test_w6_malformed_worker_result_keeps_raw_and_does_not_publish_transcript(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = build_persistent_runtime(
        tmp_path / "malformed ASR runtime fixture",
        asr=True,
        worker_mode="malformed",
    )
    monkeypatch.setenv("ROUGHCUT_RUNTIME_BINDING", str(runtime))
    monkeypatch.delenv("ROUGHCUT_FFMPEG_COMMAND", raising=False)
    monkeypatch.delenv("ROUGHCUT_FFPROBE_COMMAND", raising=False)
    source_path = tmp_path / "坏素材 中文 with spaces.mp4"
    make_av_source(source_path, color="orange", frequency=330, duration=1.4)
    project_path, imported, source = seed_source_project(
        tmp_path / "Malformed ASR project root", source_path
    )
    _authorize(project_path, source.source_id)
    before_project = (project_path / "project.json").read_bytes()

    with pytest.raises(TranscriptNormalizationError, match="no timed segments"):
        run_transcription_operation(
            project_path,
            operation_id="op_w6_asr_malformed",
            source_id=source.source_id,
            expected_project_revision=imported.revision,
            speaker_diarization=False,
        )

    record = media_operation_status(project_path, "op_w6_asr_malformed")
    assert record.status == "failed"
    assert record.result_ref is None
    assert record.error is not None
    assert record.error.responsibility == "roughcut_core"
    assert record.error.action == "normalize_transcript"
    assert (project_path / "project.json").read_bytes() == before_project
    assert ProjectStore(project_path).load().active_transcript_versions == {}
    raw_files = list((project_path / "raw-asr" / source.source_id).glob("*.json"))
    assert len(raw_files) == 1
    assert json.loads(raw_files[0].read_text(encoding="utf-8")) == {"malformed": True}
    assert not list((project_path / "transcripts" / source.source_id).glob("*.json"))
    assert not list(project_path.rglob("*.wav"))
