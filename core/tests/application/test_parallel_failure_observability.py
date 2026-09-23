from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import roughcut.adapters.ffmpeg.multicam_parallel as camera_adapter
from roughcut.adapters.ffmpeg.multicam_parallel import (
    ParallelCameraVerifyError,
    bounded_stderr_tail,
    verify_parallel_camera,
)
from roughcut.application.media_operations import _terminal_record
from roughcut.application.multicam_parallel import (
    _parallel_evidence_fallback,
    _parallel_failure,
    _parallel_verify_evidence,
    _write_parallel_failure_record,
)
from roughcut.domain.media_operation import (
    PARALLEL_EVIDENCE_INTEGER_MAX,
    MediaOperationError,
    MediaOperationFailure,
    MediaOperationRecord,
    ParallelFailureEvidence,
    ParallelRenderOperationResult,
    ProjectOperationScope,
)
from roughcut.domain.render import ToolResolution


def _camera(*, frames: int = 50, samples: int = 96_000) -> dict[str, object]:
    return {
        "slots": [
            {
                "classification": "mapped",
                "video_frame_start": 0,
                "video_frame_end": frames,
                "video_frame_quota": frames,
                "audio_sample_start": 0,
                "audio_sample_end": samples,
                "audio_sample_quota": samples,
                "auxiliary_ref": {
                    "source_id": "src_aux",
                    "source_start_ticks": 0,
                    "source_end_ticks": 192_000,
                },
            }
        ]
    }


def _probe_run(
    *, frame_count: int = 50, timeline_delta: int = 0, decoded_delta: int = 0
):
    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "-show_streams" in command:
            payload = {
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 320,
                        "height": 240,
                        "avg_frame_rate": "25/1",
                        "pix_fmt": "yuv420p",
                        "nb_read_frames": str(frame_count),
                    },
                    {
                        "codec_type": "audio",
                        "codec_name": "aac",
                        "sample_rate": "48000",
                        "channels": 2,
                        "time_base": "1/48000",
                        "duration_ts": str(96_000 + timeline_delta),
                    },
                ]
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        if "-af" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                "",
                f"[Parsed_astats_0] Number of samples: {96_000 + decoded_delta}\n",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    return run


@pytest.mark.parametrize(
    ("kwargs", "check", "expected", "actual", "delta", "tolerance"),
    [
        (
            {"frame_count": 49},
            "video_frame_quota",
            50,
            49,
            -1,
            0,
        ),
        (
            {"timeline_delta": 1025},
            "aac_timeline_quota",
            96_000,
            97_025,
            1025,
            1024,
        ),
        (
            {"decoded_delta": -1025},
            "aac_decoded_quota",
            96_000,
            94_975,
            -1025,
            1024,
        ),
    ],
)
def test_parallel_verify_failure_evidence_distinguishes_exact_checks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    kwargs: dict[str, int],
    check: str,
    expected: int,
    actual: int,
    delta: int,
    tolerance: int,
) -> None:
    output = tmp_path / "camera.mp4"
    output.write_bytes(b"candidate")
    monkeypatch.setattr(camera_adapter.subprocess, "run", _probe_run(**kwargs))
    with pytest.raises(ParallelCameraVerifyError) as raised:
        verify_parallel_camera(
            _camera(),
            output_path=output,
            settings={
                "width": 320,
                "height": 240,
                "frame_rate": {"numerator": 25, "denominator": 1},
                "audio_sample_rate": 48_000,
            },
            ffmpeg=ToolResolution("ffmpeg", "/tools/ffmpeg", "fixture"),
            ffprobe=ToolResolution("ffprobe", "/tools/ffprobe", "fixture"),
        )
    error = raised.value
    assert error.check == check
    evidence = _parallel_verify_evidence("aux_1", error)
    assert evidence.to_dict() == {
        "code": "parallel_camera_verify_failed",
        "camera_id": "aux_1",
        "check": check,
        "expected": expected,
        "actual": actual,
        "delta": delta,
        "tolerance": tolerance,
        "return_code": None,
        "stderr_tail": None,
    }


def test_bounded_stderr_tail_is_stable_utf8_safe_and_redacted() -> None:
    raw = (
        "/Users/alice/My Project/private.mp4\n"
        "access_token=xyz\n"
        "AWS_SECRET_ACCESS_KEY=topsecret\n"
        "Authorization: Bearer topsecret\n"
        "C:\\Users\\alice\\My Project\\private.mp4\n"
        "bad\udcfftail\n"
    )
    first = bounded_stderr_tail(raw)
    second = bounded_stderr_tail(raw)
    assert first == second
    assert len(first.encode("utf-8")) <= 2048
    assert len(first.splitlines()) <= 8
    for forbidden in (
        "alice",
        "My Project",
        "private.mp4",
        "xyz",
        "topsecret",
        "Users",
    ):
        assert forbidden not in first
    assert "<redacted-path>" in first
    assert "<redacted>" in first
    assert "bad?tail" in first
    first.encode("utf-8")
    binary_tail = bounded_stderr_tail(b"bad\xff\xfe")
    assert "\ufffd" in binary_tail
    long_tail = bounded_stderr_tail("line\n" + "x" * 5000)
    assert len(long_tail.encode("utf-8")) <= 2048
    million_tail = bounded_stderr_tail("x" * 1_000_000)
    assert million_tail == "x" * 2048


def test_parallel_evidence_numeric_fields_have_a_non_negative_bound() -> None:
    with pytest.raises(MediaOperationError):
        ParallelFailureEvidence(
            code="parallel_camera_verify_failed",
            camera_id="aux_1",
            check="aac_timeline_quota",
            expected=PARALLEL_EVIDENCE_INTEGER_MAX + 1,
            actual=96_000,
            delta=-(PARALLEL_EVIDENCE_INTEGER_MAX + 1 - 96_000),
            tolerance=1024,
            return_code=None,
            stderr_tail=None,
        )
    with pytest.raises(MediaOperationError):
        ParallelFailureEvidence(
            code="parallel_camera_encode_failed",
            camera_id="aux_1",
            check="ffmpeg_encode",
            expected=None,
            actual=None,
            delta=None,
            tolerance=None,
            return_code=2**31,
            stderr_tail="bounded",
        )


def test_parallel_evidence_json_safe_numeric_fields_roundtrip_exact_delta() -> None:
    expected = 2**31 + 1
    actual = expected + 1024
    evidence = ParallelFailureEvidence(
        code="parallel_camera_verify_failed",
        camera_id="aux_1",
        check="aac_timeline_quota",
        expected=expected,
        actual=actual,
        delta=actual - expected,
        tolerance=1024,
        return_code=None,
        stderr_tail=None,
    )
    assert ParallelFailureEvidence.from_dict(evidence.to_dict()) == evidence
    assert evidence.delta == 1024
    with pytest.raises(MediaOperationError):
        ParallelFailureEvidence(
            code="parallel_camera_verify_failed",
            camera_id="aux_1",
            check="aac_timeline_quota",
            expected=PARALLEL_EVIDENCE_INTEGER_MAX + 1,
            actual=PARALLEL_EVIDENCE_INTEGER_MAX,
            delta=-1,
            tolerance=1024,
            return_code=None,
            stderr_tail=None,
        )


def test_parallel_evidence_unsafe_numeric_value_uses_fixed_fallback() -> None:
    error = ParallelCameraVerifyError(
        "fixture unsafe quota",
        check="aac_timeline_quota",
        expected=PARALLEL_EVIDENCE_INTEGER_MAX + 1,
        actual=PARALLEL_EVIDENCE_INTEGER_MAX,
        tolerance=1024,
    )
    with pytest.raises(MediaOperationError):
        _parallel_verify_evidence("aux_1", error)
    fallback = _parallel_evidence_fallback()
    assert fallback.code == "parallel_failure_evidence_unavailable"
    assert fallback.check == "failure_evidence_persistence"


def test_parallel_result_ref_keeps_complete_and_legacy_shapes_closed() -> None:
    legacy = {
        "kind": "multicam_parallel_render",
        "parallel_render_id": "mpr_" + "d" * 32,
        "schema_version": 1,
        "manifest_content_hash": "e" * 64,
    }
    result = ParallelRenderOperationResult.from_dict(legacy)
    assert result.to_dict() == legacy
    evidence = ParallelFailureEvidence(
        code="parallel_camera_encode_failed",
        camera_id="aux_1",
        check="ffmpeg_encode",
        expected=None,
        actual=None,
        delta=None,
        tolerance=None,
        return_code=7,
        stderr_tail="bounded",
    )
    partial = ParallelRenderOperationResult(
        "mpr_" + "d" * 32,
        1,
        "e" * 64,
        partial_failure_evidence=evidence,
    )
    assert partial.to_dict()["partial_failure_evidence"] == evidence.to_dict()
    assert ParallelRenderOperationResult.from_dict(partial.to_dict()) == partial
    with pytest.raises(MediaOperationError):
        ParallelRenderOperationResult(
            "mpr_" + "d" * 32,
            1,
            "e" * 64,
            partial_failure_evidence=ParallelFailureEvidence(
                code="parallel_manifest_publish_failed",
                camera_id=None,
                check="manifest_publish",
                expected="published_verified_manifest",
                actual="publish_failed",
                delta=None,
                tolerance=None,
                return_code=None,
                stderr_tail=None,
            ),
        )
    with pytest.raises(MediaOperationError):
        ParallelFailureEvidence(
            code="parallel_camera_verify_failed",
            camera_id="aux_1",
            check="video_frame_quota",
            expected=50,
            actual=-1,
            delta=-51,
            tolerance=0,
            return_code=None,
            stderr_tail=None,
        )


def test_parallel_failure_evidence_roundtrips_and_legacy_error_stays_closed() -> None:
    evidence = ParallelFailureEvidence(
        code="parallel_camera_verify_failed",
        camera_id="aux_1",
        check="aac_timeline_quota",
        expected=96_000,
        actual=95_392,
        delta=-608,
        tolerance=1024,
        return_code=None,
        stderr_tail=None,
    )
    failure = MediaOperationFailure(
        "parallel_render_all_cameras_failed",
        "roughcut_core",
        "render_parallel_cameras",
        "parallel_render_failed",
        evidence,
    )
    assert MediaOperationFailure.from_dict(failure.to_dict()) == failure
    legacy = {
        "code": "parallel_render_all_cameras_failed",
        "responsibility": "roughcut_core",
        "action": "render_parallel_cameras",
        "message_code": "parallel_render_failed",
    }
    assert MediaOperationFailure.from_dict(legacy).to_dict() == legacy


class _FailOnceStore:
    def __init__(self) -> None:
        self.calls = 0

    def write_locked(self, record: MediaOperationRecord) -> MediaOperationRecord:
        self.calls += 1
        if self.calls == 1:
            raise TypeError("fixture evidence serialization failure")
        return record


def test_evidence_persistence_failure_keeps_primary_failure_and_uses_fallback() -> None:
    active = MediaOperationRecord(
        operation_id="op_00000000000040008000000000000077",
        scope=ProjectOperationScope("project_fixture", "a" * 64),
        operation_type="render_multicam_parallel",
        request_hash="b" * 64,
        input_hash="c" * 64,
        status="running",
        phase_message_code="parallel_render_encoding",
        created_at="2026-08-25T00:00:00.000000Z",
        started_at="2026-08-25T00:00:01.000000Z",
        updated_at="2026-08-25T00:00:01.000000Z",
        finished_at=None,
        result_ref=None,
        error=None,
        schema_version=2,
    )
    primary = _parallel_failure(
        "parallel_render_all_cameras_failed",
        evidence=ParallelFailureEvidence(
            code="parallel_camera_encode_failed",
            camera_id="aux_1",
            check="ffmpeg_encode",
            expected=None,
            actual=None,
            delta=None,
            tolerance=None,
            return_code=7,
            stderr_tail="bounded",
        ),
    )
    store = _FailOnceStore()
    record = _write_parallel_failure_record(store, active, primary)  # type: ignore[arg-type]
    assert store.calls == 2
    assert record.error is not None
    assert record.error.code == "parallel_render_all_cameras_failed"
    assert record.error.evidence is not None
    assert record.error.evidence.code == "parallel_failure_evidence_unavailable"
    assert record.result_ref is None


def test_success_terminal_record_has_no_failure_evidence() -> None:
    active = MediaOperationRecord(
        operation_id="op_00000000000040008000000000000078",
        scope=ProjectOperationScope("project_fixture", "a" * 64),
        operation_type="render_multicam_parallel",
        request_hash="b" * 64,
        input_hash="c" * 64,
        status="running",
        phase_message_code="parallel_render_publishing",
        created_at="2026-08-25T00:00:00.000000Z",
        started_at="2026-08-25T00:00:01.000000Z",
        updated_at="2026-08-25T00:00:01.000000Z",
        finished_at=None,
        result_ref=None,
        error=None,
        schema_version=2,
    )
    result = ParallelRenderOperationResult("mpr_" + "d" * 32, 1, "e" * 64)
    succeeded = _terminal_record(active, status="succeeded", result=result)
    assert succeeded.error is None
    assert succeeded.result_ref == result
