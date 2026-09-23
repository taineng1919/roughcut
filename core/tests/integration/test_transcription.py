from __future__ import annotations

import json
import shutil
import subprocess
import sys
import wave
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from roughcut.adapters.funasr.normalize import TranscriptNormalizationError
from roughcut.adapters.funasr.runner import FunASRConfig, FunASRRun
from roughcut.adapters.workflow_store import WorkflowStore
from roughcut.application.media_operations import MediaOperationOutcome
from roughcut.application.projects import create_project, open_project
from roughcut.application.sources import add_source
from roughcut.application.transcription import read_transcript_page, transcribe_source
from roughcut.application.workflows import workflow_action, workflow_start, workflow_status
from roughcut.cli import main as cli_main
from roughcut.domain.errors import WorkflowError
from roughcut.domain.media_operation import (
    MediaOperationRecord,
    ProjectOperationScope,
    TranscriptOperationResult,
)
from roughcut.domain.project import ImportMode, MediaProbe, ProjectError
from roughcut.domain.time import TICKS_PER_SECOND
from roughcut.domain.transcript import TimedTranscript
from roughcut.mcp import TOOLS, handle_request

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "fixtures" / "asr"


def _authorize_public_transcription(
    project_path: Path,
    source_id: str,
    *,
    speaker_diarization: bool,
) -> None:
    started = workflow_start(project_path, "wfr_transcription_contract", [source_id])
    basis = started.status["confirmation_bases"]["scope"]["basis"]
    workflow_action(
        project_path,
        "wfr_transcription_contract",
        "act_transcription_scope",
        "approve_scope",
        {
            "schema_version": 1,
            "confirmation_basis": basis,
            "source_authorizations": [
                {
                    "source_id": source_id,
                    "transcribe": True,
                    "speaker_diarization": speaker_diarization,
                }
            ],
        },
    )


def make_audio(path: Path) -> None:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes(b"\0\0" * 16_000 * 3)


def fixture_runner(name: str):
    def run(_source_path: Path, raw_output_path: Path) -> FunASRRun:
        raw_output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURES / name, raw_output_path)
        return FunASRRun(
            package_version="1.3.8",
            models={"asr": "fixture-asr", "vad": "fixture-vad", "punc": "fixture-punc"},
            parameters={
                "batch_size_s": 300,
                "sentence_timestamp": True,
                "return_raw_text": True,
                "device": "cpu",
                "input_type": "pcm_samples",
                "decode": {
                    "ffmpeg_path": "/fixture/ffmpeg",
                    "ffmpeg_version": "ffmpeg version fixture",
                    "audio_stream": "0:a:0",
                    "sample_rate_hz": 16_000,
                    "channels": 1,
                    "sample_format": "s16le",
                    "container_format": "wav",
                },
                "runtime_versions": {
                    "funasr": "1.3.8",
                    "torch": "2.12.0",
                    "torchaudio": "2.11.0",
                },
            },
            started_at="2026-07-19T00:00:00+00:00",
            completed_at="2026-07-19T00:00:01+00:00",
            exit_status=0,
        )

    return run


def _public_outcome(
    transcript: TimedTranscript,
    *,
    operation_id: str = "op_transcription_contract",
    project_revision: int,
) -> MediaOperationOutcome[object]:
    return MediaOperationOutcome(
        MediaOperationRecord(
            operation_id=operation_id,
            scope=ProjectOperationScope("project", "d" * 64),
            operation_type="transcribe_source",
            request_hash="a" * 64,
            input_hash="b" * 64,
            status="succeeded",
            phase_message_code="transcription_succeeded",
            created_at="2026-07-29T00:00:00.000000Z",
            started_at="2026-07-29T00:00:00.000000Z",
            updated_at="2026-07-29T00:00:01.000000Z",
            finished_at="2026-07-29T00:00:01.000000Z",
            result_ref=TranscriptOperationResult(
                source_id=transcript.source_id,
                transcript_version_id=transcript.transcript_version_id,
                schema_version=1,
                content_hash="c" * 64,
                project_revision=project_revision,
            ),
            error=None,
        ),
        transcript,
        False,
    )


@pytest.fixture
def isolated_funasr_config(tmp_path: Path) -> Iterator[None]:
    def configure(
        *,
        python_path: Path | None = None,
        model_root: Path | None = None,
        speaker_diarization: bool = False,
        speaker_model_path: Path | None = None,
    ) -> FunASRConfig:
        return FunASRConfig(
            python_path=python_path or tmp_path / "fixture runtime/python",
            model_root=model_root or tmp_path / "fixture models",
            speaker_diarization=speaker_diarization,
            speaker_model_path=speaker_model_path,
            ffmpeg_command=str(tmp_path / "fixture tools/ffmpeg"),
        )

    with (
        patch(
            "roughcut.cli.configured_funasr_config",
            side_effect=configure,
        ),
        patch(
            "roughcut.mcp.configured_funasr_config",
            side_effect=configure,
        ),
    ):
        yield


def create_source_project(tmp_path: Path) -> tuple[Path, str, int]:
    source_path = tmp_path / "中文 transcript source.wav"
    make_audio(source_path)
    project_path = tmp_path / "transcript project"
    project = create_project(project_path, "Transcript project")
    fixture_probe = MediaProbe(
        duration_ticks=3 * TICKS_PER_SECOND,
        container_start_ticks=0,
        first_content_ticks=0,
        video_codec=None,
        width=None,
        height=None,
        nominal_frame_rate=None,
        is_vfr=False,
        audio_codec="pcm_s16le",
        audio_sample_rate=16_000,
        rotation_degrees=0,
    )
    with patch(
        "roughcut.application.sources.probe_media",
        return_value=fixture_probe,
    ):
        imported = add_source(
            project_path,
            source_path,
            ImportMode.LINKED,
            expected_revision=project.revision,
        )
    return project_path, imported.sources[0].source_id, imported.revision


def test_raw_is_persisted_before_transcript_and_segments_are_pageable(tmp_path: Path) -> None:
    project_path, source_id, revision = create_source_project(tmp_path)

    transcript = transcribe_source(
        project_path,
        source_id,
        expected_revision=revision,
        runner=fixture_runner("funasr_sentence_info.json"),
    )
    raw_path = project_path / transcript.provenance.raw_result_path
    transcript_path = project_path / "transcripts" / source_id / f"{transcript.transcript_version_id}.json"
    assert raw_path.is_file()
    assert json.loads(raw_path.read_text(encoding="utf-8")) == json.loads(
        (FIXTURES / "funasr_sentence_info.json").read_text(encoding="utf-8")
    )
    assert transcript_path.is_file()
    stored_transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    assert stored_transcript["provenance"]["package_version"] == "1.3.8"
    assert stored_transcript["provenance"]["models"] == {
        "asr": "fixture-asr",
        "vad": "fixture-vad",
        "punc": "fixture-punc",
    }
    assert stored_transcript["provenance"]["parameters"]["device"] == "cpu"
    assert stored_transcript["provenance"]["parameters"]["input_type"] == "pcm_samples"
    assert stored_transcript["provenance"]["parameters"]["decode"] == {
        "ffmpeg_path": "/fixture/ffmpeg",
        "ffmpeg_version": "ffmpeg version fixture",
        "audio_stream": "0:a:0",
        "sample_rate_hz": 16_000,
        "channels": 1,
        "sample_format": "s16le",
        "container_format": "wav",
    }
    assert stored_transcript["provenance"]["parameters"]["runtime_versions"] == {
        "funasr": "1.3.8",
        "torch": "2.12.0",
        "torchaudio": "2.11.0",
    }
    assert open_project(project_path).active_transcript_versions == {
        source_id: transcript.transcript_version_id
    }
    assert open_project(project_path).revision == revision + 1

    first_page = read_transcript_page(
        project_path,
        source_id,
        transcript.transcript_version_id,
        offset=0,
        limit=1,
    )
    second_page = read_transcript_page(
        project_path,
        source_id,
        transcript.transcript_version_id,
        offset=first_page.next_offset or 0,
        limit=1,
    )
    assert [segment["segment_id"] for segment in first_page.segments] == ["seg_000001"]
    assert [segment["segment_id"] for segment in second_page.segments] == ["seg_000002"]
    assert first_page.next_offset == 1
    assert second_page.next_offset is None

    cli = subprocess.run(
        [
            sys.executable,
            "-m",
            "roughcut.cli",
            "transcript-page",
            "--project",
            str(project_path),
            "--source-id",
            source_id,
            "--transcript-id",
            transcript.transcript_version_id,
            "--offset",
            "0",
            "--limit",
            "1",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert cli.returncode == 0, cli.stderr
    cli_page = json.loads(cli.stdout)["transcript_page"]
    mcp = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "transcript_page",
                "arguments": {
                    "project_path": str(project_path),
                    "source_id": source_id,
                    "transcript_version_id": transcript.transcript_version_id,
                    "offset": 0,
                    "limit": 1,
                },
            },
        }
    )
    assert mcp is not None
    result = mcp["result"]
    assert isinstance(result, dict)
    assert result["structuredContent"]["transcript_page"] == cli_page


def test_final_ten_millisecond_overrun_publishes_once_and_preserves_raw(
    tmp_path: Path,
) -> None:
    project_path, source_id, revision = create_source_project(tmp_path)
    raw = [
        {
            "text": "甲乙",
            "sentence_info": [
                {"text": "甲", "start": 0, "end": 1000, "spk": 0},
                {"text": "乙", "start": 1000, "end": 3010, "spk": 1},
            ],
        }
    ]
    raw_bytes = json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode()

    def bounded_overrun_runner(_source_path: Path, raw_output_path: Path) -> FunASRRun:
        raw_output_path.parent.mkdir(parents=True, exist_ok=True)
        raw_output_path.write_bytes(raw_bytes)
        return FunASRRun(
            package_version="1.3.14",
            models={"asr": "fixture-asr", "spk": "fixture-spk"},
            parameters={"speaker_diarization": {"enabled": True}},
            started_at="fixture-start",
            completed_at="fixture-end",
            exit_status=0,
        )

    transcript = transcribe_source(
        project_path,
        source_id,
        expected_revision=revision,
        runner=bounded_overrun_runner,
    )

    raw_path = project_path / transcript.provenance.raw_result_path
    project = open_project(project_path)
    assert raw_path.read_bytes() == raw_bytes
    assert project.revision == revision + 1
    assert project.active_transcript_versions == {
        source_id: transcript.transcript_version_id
    }
    assert transcript.segments[-1].end_ticks == 3 * TICKS_PER_SECOND
    assert transcript.segments[-1].local_speaker_id == "spk_1"


def test_unknown_result_keeps_raw_file_and_does_not_publish_transcript(tmp_path: Path) -> None:
    project_path, source_id, revision = create_source_project(tmp_path)

    with pytest.raises(TranscriptNormalizationError) as error:
        transcribe_source(
            project_path,
            source_id,
            expected_revision=revision,
            runner=fixture_runner("funasr_unknown.json"),
        )

    assert (project_path / error.value.raw_result_path).is_file()
    assert not (project_path / "transcripts" / source_id).exists()
    assert open_project(project_path).revision == revision


def test_malformed_sentence_info_keeps_raw_file_and_does_not_publish_transcript(
    tmp_path: Path,
) -> None:
    project_path, source_id, revision = create_source_project(tmp_path)

    with pytest.raises(TranscriptNormalizationError, match="sentence_info") as error:
        transcribe_source(
            project_path,
            source_id,
            expected_revision=revision,
            runner=fixture_runner("funasr_bad_sentence_info.json"),
        )

    assert (project_path / error.value.raw_result_path).is_file()
    assert not (project_path / "transcripts" / source_id).exists()
    assert open_project(project_path).revision == revision


def test_invalid_speaker_keeps_raw_and_project_revision_unchanged(tmp_path: Path) -> None:
    project_path, source_id, revision = create_source_project(tmp_path)

    def invalid_speaker_runner(_source: Path, raw_output: Path) -> FunASRRun:
        raw_output.parent.mkdir(parents=True, exist_ok=True)
        raw_output.write_text(
            json.dumps(
                [
                    {
                        "text": "非法说话人。",
                        "sentence_info": [
                            {
                                "text": "非法说话人。",
                                "start": 0,
                                "end": 800,
                                "spk": True,
                            }
                        ],
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return FunASRRun(
            package_version="1.3.8",
            models={"asr": "fixture", "spk": "model_spk"},
            parameters={"speaker_diarization": {"enabled": True}},
            started_at="2026-07-21T00:00:00Z",
            completed_at="2026-07-21T00:00:01Z",
            exit_status=0,
        )

    with pytest.raises(TranscriptNormalizationError, match="speaker") as error:
        transcribe_source(
            project_path,
            source_id,
            expected_revision=revision,
            runner=invalid_speaker_runner,
        )

    raw_path = project_path / error.value.raw_result_path
    assert raw_path.is_file()
    assert json.loads(raw_path.read_text(encoding="utf-8"))[0]["sentence_info"][0][
        "spk"
    ] is True
    assert not (project_path / "transcripts" / source_id).exists()
    assert open_project(project_path).revision == revision


def test_transcript_page_rejects_path_traversal(tmp_path: Path) -> None:
    project_path, source_id, _revision = create_source_project(tmp_path)

    with pytest.raises(ProjectError, match="id is invalid"):
        read_transcript_page(
            project_path,
            source_id,
            "../../project",
            offset=0,
            limit=10,
        )


def test_transcribe_cli_and_mcp_return_the_same_bounded_metadata(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project_path, source_id, revision = create_source_project(tmp_path)
    transcript = transcribe_source(
        project_path,
        source_id,
        expected_revision=revision,
        runner=fixture_runner("funasr_sentence_info.json"),
    )
    _authorize_public_transcription(
        project_path, source_id, speaker_diarization=False
    )
    arguments = {
        "project_path": str(project_path),
        "operation_id": "op_transcription_contract",
        "source_id": source_id,
        "expected_revision": revision,
    }
    outcome = _public_outcome(transcript, project_revision=revision + 1)

    with patch(
        "roughcut.cli.run_transcription_operation",
        return_value=outcome,
    ):
        cli_main(
            [
                "transcribe-source",
                "--project",
                str(project_path),
                "--operation-id",
                "op_transcription_contract",
                "--operation-id",
                "op_transcription_contract",
                "--source-id",
                source_id,
                "--expected-revision",
                str(revision),
                "--json",
            ]
        )
    cli_payload = json.loads(capsys.readouterr().out)

    with patch(
        "roughcut.mcp.run_transcription_operation",
        return_value=outcome,
    ):
        mcp = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "transcribe_source", "arguments": arguments},
            }
        )
    assert mcp is not None
    mcp_result = mcp["result"]
    assert isinstance(mcp_result, dict)
    assert mcp_result["structuredContent"] == cli_payload
    assert cli_payload["operation_readback"] is False
    assert cli_payload["transcript"] == {
        "transcript_version_id": transcript.transcript_version_id,
        "source_id": source_id,
        "segment_count": 2,
        "raw_result_path": transcript.provenance.raw_result_path,
        "project_revision": revision + 1,
    }


@pytest.mark.parametrize("surface", ("cli", "mcp"))
def test_authorized_public_transcription_returns_with_exact_run_binding(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    surface: str,
) -> None:
    project_path, source_id, revision = create_source_project(tmp_path)
    _authorize_public_transcription(
        project_path, source_id, speaker_diarization=False
    )
    transcript = transcribe_source(
        project_path,
        source_id,
        expected_revision=revision,
        runner=fixture_runner("funasr_sentence_info.json"),
    )
    outcome = _public_outcome(transcript, project_revision=revision + 1)

    if surface == "cli":
        with patch(
            "roughcut.cli.run_transcription_operation",
            return_value=outcome,
        ) as coordinator:
            cli_main(
                [
                    "transcribe-source",
                    "--project",
                    str(project_path),
                    "--operation-id",
                    "op_transcription_contract",
                    "--source-id",
                    source_id,
                    "--expected-revision",
                    str(revision),
                    "--json",
                ]
            )
        payload = json.loads(capsys.readouterr().out)
    else:
        with patch(
            "roughcut.mcp.run_transcription_operation",
            return_value=outcome,
        ) as coordinator:
            response = handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": "binding-sync",
                    "method": "tools/call",
                    "params": {
                        "name": "transcribe_source",
                        "arguments": {
                            "project_path": str(project_path),
                            "operation_id": "op_transcription_contract",
                            "source_id": source_id,
                            "expected_revision": revision,
                        },
                    },
                }
            )
        assert response is not None
        payload = response["result"]["structuredContent"]
    assert payload["transcript"]["transcript_version_id"] == (
        transcript.transcript_version_id
    )
    coordinator.assert_called_once_with(
        project_path,
        operation_id="op_transcription_contract",
        source_id=source_id,
        expected_project_revision=revision,
        speaker_diarization=False,
    )


def test_status_repairs_project_commit_before_transcription_binding_sync(
    tmp_path: Path,
) -> None:
    project_path, source_id, revision = create_source_project(tmp_path)
    _authorize_public_transcription(
        project_path, source_id, speaker_diarization=False
    )
    transcript = transcribe_source(
        project_path,
        source_id,
        expected_revision=revision,
        runner=fixture_runner("funasr_sentence_info.json"),
    )
    before = WorkflowStore(project_path).read_run("wfr_transcription_contract")
    assert before.ordered_bindings[0].transcript_version_id is None

    repaired = workflow_status(project_path, "wfr_transcription_contract")
    binding = repaired["workflow_run"]["ordered_bindings"][0]
    assert repaired["binding_sync"] == {
        "state": "repaired",
        "source_ids": [source_id],
    }
    assert binding["transcript_version_id"] == transcript.transcript_version_id
    assert binding["transcript_content_hash"] is not None


def test_cli_reports_binding_sync_failure_after_project_commit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project_path, source_id, revision = create_source_project(tmp_path)
    _authorize_public_transcription(
        project_path, source_id, speaker_diarization=False
    )

    sync_error = WorkflowError(
        "workflow_binding_sync_failed",
        "Roughcut workflow façade could not synchronize the active Transcript binding",
    )
    with (
        patch(
            "roughcut.cli.run_transcription_operation",
            side_effect=sync_error,
        ),
        pytest.raises(SystemExit),
    ):
        cli_main(
            [
                "transcribe-source",
                "--project",
                str(project_path),
                "--operation-id",
                "op_transcription_contract",
                "--source-id",
                source_id,
                "--expected-revision",
                str(revision),
                "--json",
            ]
        )
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "workflow_binding_sync_failed"


def test_transcribe_cli_and_mcp_pass_equivalent_explicit_speaker_config(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_path, source_id, revision = create_source_project(tmp_path)
    transcript = transcribe_source(
        project_path,
        source_id,
        expected_revision=revision,
        runner=fixture_runner("funasr_sentence_info.json"),
    )
    _authorize_public_transcription(
        project_path, source_id, speaker_diarization=True
    )
    outcome = _public_outcome(transcript, project_revision=revision + 1)

    with patch(
        "roughcut.cli.run_transcription_operation",
        return_value=outcome,
    ) as cli_call:
        cli_main(
            [
                "transcribe-source",
                "--project",
                str(project_path),
                "--operation-id",
                "op_transcription_contract",
                "--source-id",
                source_id,
                "--expected-revision",
                str(revision),
                "--speaker-diarization",
                "true",
                "--json",
            ]
        )
    cli_payload = json.loads(capsys.readouterr().out)

    with patch(
        "roughcut.mcp.run_transcription_operation",
        return_value=outcome,
    ) as mcp_call:
        mcp = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 20,
                "method": "tools/call",
                "params": {
                    "name": "transcribe_source",
                    "arguments": {
                        "project_path": str(project_path),
                        "operation_id": "op_transcription_contract",
                        "source_id": source_id,
                        "expected_revision": revision,
                        "speaker_diarization": True,
                    },
                },
            }
        )

    assert mcp is not None
    assert mcp["result"]["structuredContent"] == cli_payload
    assert cli_call.call_args.kwargs["speaker_diarization"] is True
    assert mcp_call.call_args.kwargs["speaker_diarization"] is True


def test_transcribe_cli_and_mcp_default_speaker_diarization_off(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project_path, source_id, revision = create_source_project(tmp_path)
    transcript = transcribe_source(
        project_path,
        source_id,
        expected_revision=revision,
        runner=fixture_runner("funasr_sentence_info.json"),
    )
    _authorize_public_transcription(
        project_path, source_id, speaker_diarization=False
    )
    outcome = _public_outcome(transcript, project_revision=revision + 1)
    with patch(
        "roughcut.cli.run_transcription_operation",
        return_value=outcome,
    ) as cli_call:
        cli_main(
            [
                "transcribe-source",
                "--project",
                str(project_path),
                "--operation-id",
                "op_transcription_contract",
                "--source-id",
                source_id,
                "--expected-revision",
                str(revision),
                "--json",
            ]
        )
    capsys.readouterr()
    assert cli_call.call_args.kwargs["speaker_diarization"] is False

    with patch(
        "roughcut.mcp.run_transcription_operation",
        return_value=outcome,
    ) as mcp_call:
        response = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 21,
                "method": "tools/call",
                "params": {
                    "name": "transcribe_source",
                    "arguments": {
                        "project_path": str(project_path),
                        "operation_id": "op_transcription_contract",
                        "source_id": source_id,
                        "expected_revision": revision,
                    },
                },
            }
        )
    assert response is not None
    assert mcp_call.call_args.kwargs["speaker_diarization"] is False

    tool = next(item for item in TOOLS if item["name"] == "transcribe_source")
    properties = tool["inputSchema"]["properties"]
    assert properties["speaker_diarization"] == {"type": "boolean", "default": False}
    assert "speaker_model_path" not in properties
    assert "funasr_python" not in properties
    assert "model_root" not in properties
    assert "operation_id" in tool["inputSchema"]["required"]


@pytest.mark.parametrize(
    ("cli_option", "mcp_field"),
    (
        ("--funasr-python", "funasr_python"),
        ("--model-root", "model_root"),
        ("--speaker-model-path", "speaker_model_path"),
    ),
)
def test_tracked_transcribe_rejects_each_removed_runtime_override(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    cli_option: str,
    mcp_field: str,
) -> None:
    project_path, source_id, revision = create_source_project(tmp_path)
    override_path = tmp_path / "removed-runtime-override"
    with pytest.raises(SystemExit) as cli_exit:
        cli_main(
            [
                "transcribe-source",
                "--project",
                str(project_path),
                "--source-id",
                source_id,
                "--expected-revision",
                str(revision),
                cli_option,
                str(override_path),
                "--json",
            ]
        )
    cli_payload = json.loads(capsys.readouterr().out)

    mcp = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 22,
            "method": "tools/call",
            "params": {
                "name": "transcribe_source",
                "arguments": {
                    "project_path": str(project_path),
                    "operation_id": "op_transcription_contract",
                    "source_id": source_id,
                    "expected_revision": revision,
                    mcp_field: str(override_path),
                },
            },
        }
    )

    assert cli_exit.value.code == 2
    assert cli_payload["error"]["code"] == "invalid_arguments"
    assert mcp is not None
    assert mcp["result"]["structuredContent"]["error"]["code"] == "invalid_arguments"


def test_transcribe_cli_and_mcp_keep_project_errors_as_domain_failures(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project_path, source_id, revision = create_source_project(tmp_path)
    _authorize_public_transcription(
        project_path, source_id, speaker_diarization=False
    )
    error = ProjectError("project revision conflict")

    with patch("roughcut.cli.run_transcription_operation", side_effect=error), pytest.raises(
        SystemExit
    ) as cli_exit:
        cli_main(
                [
                    "transcribe-source",
                    "--project",
                    str(project_path),
                    "--operation-id",
                    "op_transcription_contract",
                    "--source-id",
                    source_id,
                    "--expected-revision",
                    str(revision),
                    "--json",
                ]
            )
    cli_payload = json.loads(capsys.readouterr().out)

    with patch("roughcut.mcp.run_transcription_operation", side_effect=error):
        mcp = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 23,
                "method": "tools/call",
                "params": {
                    "name": "transcribe_source",
                    "arguments": {
                        "project_path": str(project_path),
                        "operation_id": "op_transcription_contract",
                        "source_id": source_id,
                        "expected_revision": revision,
                    },
                },
            }
        )

    assert cli_exit.value.code == 2
    assert cli_payload["error"]["code"] == "transcription_failed"
    assert mcp is not None
    assert mcp["result"]["structuredContent"]["error"]["code"] == (
        "transcription_failed"
    )


def test_transcribe_cli_error_is_json_and_reports_preserved_raw_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project_path, source_id, revision = create_source_project(tmp_path)
    _authorize_public_transcription(
        project_path, source_id, speaker_diarization=False
    )
    normalization_error = TranscriptNormalizationError(
        "unknown result", f"raw-asr/{source_id}/preserved.json"
    )

    with patch(
        "roughcut.cli.run_transcription_operation",
        side_effect=normalization_error,
    ), pytest.raises(SystemExit) as exit_status:
        cli_main(
            [
                "transcribe-source",
                "--project",
                str(project_path),
                "--operation-id",
                "op_transcription_contract",
                "--source-id",
                source_id,
                "--expected-revision",
                str(revision),
                "--json",
            ]
        )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_status.value.code == 2
    assert captured.err == ""
    assert payload["error"] == {"code": "transcription_failed"}
