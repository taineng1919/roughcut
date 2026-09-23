"""Bounded real-process helpers and media-free fixtures for Windows W4 tests."""

from __future__ import annotations

import codecs
import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, Self

from roughcut.adapters.artifact_store import write_new_json
from roughcut.adapters.project_store import ProjectStore
from roughcut.application.projects import create_project
from roughcut.domain.project import (
    ImportMode,
    MediaProbe,
    SourceAsset,
    SourceFingerprint,
)
from roughcut.domain.transcript import (
    TimedTranscript,
    TranscriptProvenance,
    TranscriptSegment,
)

CORE_ROOT = Path(__file__).resolve().parents[2]

_BOMS = (
    codecs.BOM_UTF8,
    codecs.BOM_UTF16_LE,
    codecs.BOM_UTF16_BE,
    codecs.BOM_UTF32_LE,
    codecs.BOM_UTF32_BE,
)


def child_environment(overrides: Mapping[str, str | None] | None = None) -> dict[str, str]:
    """Return a child environment with UTF-8 mode disabled."""
    environment = dict(os.environ)
    environment["PYTHONUTF8"] = "0"
    if overrides is not None:
        for key, value in overrides.items():
            if value is None:
                environment.pop(key, None)
            else:
                environment[key] = value
    return environment


def parse_json_bytes(raw: bytes) -> dict[str, Any]:
    """Parse exactly one UTF-8 JSON value and reject BOM-prefixed output."""
    assert not any(raw.startswith(bom) for bom in _BOMS), raw[:8]
    document = raw.decode("utf-8", errors="strict")
    decoder = json.JSONDecoder()
    value, end = decoder.raw_decode(document)
    assert not document[end:].strip(), document[end:]
    assert isinstance(value, dict)
    return value


def parse_cli_result(
    completed: subprocess.CompletedProcess[bytes],
    *,
    expected_returncode: int,
) -> dict[str, Any]:
    assert completed.returncode == expected_returncode, _diagnostics(completed)
    assert completed.stderr == b"", _diagnostics(completed)
    lines = completed.stdout.splitlines()
    assert len(lines) == 1, _diagnostics(completed)
    return parse_json_bytes(lines[0])


def run_cli_raw(
    *arguments: str,
    expected_returncode: int = 0,
    environment: Mapping[str, str | None] | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-m", "roughcut.cli", *arguments],
        cwd=CORE_ROOT,
        check=False,
        capture_output=True,
        text=False,
        env=child_environment(environment),
        timeout=timeout,
    )
    return parse_cli_result(completed, expected_returncode=expected_returncode)


def _diagnostics(completed: subprocess.CompletedProcess[bytes]) -> str:
    return (
        "subprocess diagnostics:\n"
        f"argv={completed.args!r}\n"
        f"returncode={completed.returncode!r}\n"
        f"stdout={completed.stdout!r}\n"
        f"stderr={completed.stderr!r}"
    )


class McpChild:
    """A real UTF-8 byte-pipe child for the production stdio MCP server."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process
        self._lines: queue.Queue[bytes | None] = queue.Queue()
        self._reader = threading.Thread(
            target=self._read_stdout,
            name="w4-mcp-stdout-reader",
            daemon=True,
        )
        self._reader.start()

    @classmethod
    def start(
        cls,
        *,
        script: str | None = None,
        environment: Mapping[str, str | None] | None = None,
    ) -> McpChild:
        command = (
            [sys.executable, "-m", "roughcut.mcp"]
            if script is None
            else [sys.executable, "-c", script]
        )
        process = subprocess.Popen(
            command,
            cwd=CORE_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_environment(environment),
            text=False,
            bufsize=0,
        )
        return cls(process)

    def _read_stdout(self) -> None:
        stdout = self.process.stdout
        if stdout is None:
            self._lines.put(None)
            return
        while True:
            line = stdout.readline()
            if not line:
                break
            self._lines.put(line)
        self._lines.put(None)

    def send(self, request: Mapping[str, object]) -> None:
        stdin = self.process.stdin
        if stdin is None:
            raise AssertionError("MCP child stdin is unavailable")
        stdin.write(json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n")
        stdin.flush()

    def read_json(self, timeout: float = 5.0) -> dict[str, Any]:
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty as error:
            raise AssertionError(self._failure_message()) from error
        if line is None:
            raise AssertionError(self._failure_message())
        return parse_json_bytes(line.rstrip(b"\r\n"))

    def close_stdin(self) -> None:
        stdin = self.process.stdin
        if stdin is not None and not stdin.closed:
            stdin.close()

    def wait(self, timeout: float = 5.0) -> tuple[int | None, bytes]:
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            self.terminate()
            raise AssertionError(self._failure_message()) from error
        self._reader.join(timeout=timeout)
        stderr = self.process.stderr.read() if self.process.stderr is not None else b""
        return self.process.returncode, stderr

    def terminate(self, timeout: float = 5.0) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=timeout)
        self._reader.join(timeout=1)

    def _failure_message(self) -> str:
        stderr = b""
        if self.process.poll() is not None and self.process.stderr is not None:
            stderr = self.process.stderr.read()
        return (
            "MCP child did not produce a JSON line:\n"
            f"returncode={self.process.poll()!r}\n"
            f"stderr={stderr!r}"
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close_stdin()
        self.terminate()


def wait_for_file(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.is_file(), f"child did not create marker {path!s}"


def tool_call(request_id: str, name: str, arguments: Mapping[str, object]) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": dict(arguments)},
    }


def structured_payload(response: Mapping[str, object]) -> dict[str, Any]:
    result = response.get("result")
    assert isinstance(result, dict), response
    payload = result.get("structuredContent")
    assert isinstance(payload, dict), response
    return payload


def seed_workflow_project(root: Path) -> Path:
    """Create one media-free Project that is valid for the finite workflow façade."""
    project = create_project(root, "W4 中文 workflow project")
    source = SourceAsset(
        source_id="src_w4",
        kind="audio",
        display_name="W4 中文 source.wav",
        import_mode=ImportMode.LINKED,
        locator={"absolute_path": str(root / "原始 source with spaces.wav")},
        fingerprint=SourceFingerprint(100, 1, "fixture-w4-source"),
        probe=MediaProbe(
            duration_ticks=120_000,
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
        ),
    )
    transcript = TimedTranscript(
        schema_version=1,
        transcript_version_id="tr_w4",
        source_id=source.source_id,
        parent_version_id=None,
        provenance=TranscriptProvenance(
            backend="fixture",
            package_version="fixture",
            models={},
            parameters={},
            raw_result_path="raw-asr/src_w4/fixture.json",
            started_at="fixture",
            completed_at="fixture",
            exit_status=0,
        ),
        language="zh-CN",
        segments=(
            TranscriptSegment(
                segment_id="seg_w4",
                start_ticks=0,
                end_ticks=120_000,
                original_text="Windows 中文工作流。",
                corrected_text=None,
                local_speaker_id=None,
                person_id=None,
                confidence=None,
                fine_units=(),
                editorial_mark="unmarked",
            ),
        ),
    )
    write_new_json(
        root / "transcripts" / source.source_id / f"{transcript.transcript_version_id}.json",
        transcript.to_dict(),
    )
    ProjectStore(root).save(
        replace(
            project,
            revision=1,
            sources=(source,),
            active_transcript_versions={source.source_id: transcript.transcript_version_id},
        ),
        expected_revision=0,
    )
    return root
