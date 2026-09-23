"""FunASR and Qwen Filetrans transcription application services.

Both routes converge on the same downstream pipeline: one provider-specific
runner writes normalization evidence, one provider-specific normalizer derives
the existing ``TimedTranscript`` schema 1, and the shared publish step below
activates it in one Project revision.  There is deliberately no backend
interface, provider registry or generic transcription pipeline here.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from roughcut.adapters.funasr.normalize import normalize_funasr
from roughcut.adapters.funasr.runner import FunASRConfig, FunASRRun, run_funasr
from roughcut.adapters.project_store import ProjectStore
from roughcut.adapters.qwen.filetrans import (
    QwenFiletransConfig,
    run_qwen_filetrans,
)
from roughcut.adapters.qwen.normalize import normalize_qwen
from roughcut.domain.project import ImportMode, Project, ProjectError, SourceAsset
from roughcut.domain.transcript import TimedTranscript

TranscriptionRunner = Callable[[Path, Path], FunASRRun]
TranscriptionPhaseCallback = Callable[[str], None]


@dataclass(frozen=True)
class TranscriptPage:
    transcript_version_id: str
    source_id: str
    offset: int
    limit: int
    total_segments: int
    next_offset: int | None
    segments: tuple[dict[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "transcript_version_id": self.transcript_version_id,
            "source_id": self.source_id,
            "offset": self.offset,
            "limit": self.limit,
            "total_segments": self.total_segments,
            "next_offset": self.next_offset,
            "segments": list(self.segments),
        }


def transcribe_source(
    project_path: Path,
    source_id: str,
    *,
    expected_revision: int,
    runner: TranscriptionRunner | None = None,
    config: FunASRConfig | None = None,
    phase_callback: TranscriptionPhaseCallback | None = None,
) -> TimedTranscript:
    _validate_id(source_id, "source")
    store = ProjectStore(project_path)
    project = store.load()
    if project.revision != expected_revision:
        raise ProjectError("project revision conflict")
    source = _source(project.sources, source_id)
    source_path = _source_path(store.project_path, source)

    run_id = f"run_{uuid4().hex}"
    transcript_id = f"tr_{uuid4().hex}"
    raw_relative = Path("raw-asr") / source_id / f"{run_id}.json"
    raw_path = store.project_path / raw_relative
    selected_runner: TranscriptionRunner
    if runner is None:
        def default_runner(media: Path, output: Path) -> FunASRRun:
            return run_funasr(
                media,
                output,
                config=config,
                phase_callback=phase_callback,
            )
        selected_runner = default_runner
    else:
        selected_runner = runner
        if phase_callback is not None:
            phase_callback("transcription_decoding_audio")
            phase_callback("transcription_running_asr")
    run = selected_runner(source_path, raw_path)
    try:
        raw: Any = json.loads(raw_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProjectError("FunASR raw result is missing or unreadable") from error

    if phase_callback is not None:
        phase_callback("transcription_normalizing")
    transcript = normalize_funasr(
        raw,
        source_id=source_id,
        transcript_version_id=transcript_id,
        raw_result_path=raw_relative.as_posix(),
        source_duration_ticks=source.probe.duration_ticks,
        package_version=run.package_version,
        models=run.models,
        parameters=run.parameters,
        started_at=run.started_at,
        completed_at=run.completed_at,
        exit_status=run.exit_status,
    )
    if phase_callback is not None:
        phase_callback("transcription_publishing_transcript")
    return _publish_transcript(
        store,
        project,
        expected_revision=expected_revision,
        transcript=transcript,
    )


def transcribe_source_qwen_filetrans(
    project_path: Path,
    source_id: str,
    *,
    expected_revision: int,
    config: QwenFiletransConfig,
    phase_callback: TranscriptionPhaseCallback | None = None,
) -> TimedTranscript:
    """Normalize and publish one authorized Qwen Filetrans Cloud recognition.

    This is the Cloud sibling of :func:`transcribe_source`: it runs the frozen
    Cloud transport instead of the local FunASR worker and normalizes the
    sanitized provider recognition evidence with the Qwen normalizer, then
    publishes through the identical shared transcript pipeline.  It owns no
    authorization, no route decision and no credential read; the caller has
    already resolved the route and read the credential.
    """

    _validate_id(source_id, "source")
    store = ProjectStore(project_path)
    project = store.load()
    if project.revision != expected_revision:
        raise ProjectError("project revision conflict")
    source = _source(project.sources, source_id)
    source_path = _source_path(store.project_path, source)

    run_id = f"run_{uuid4().hex}"
    transcript_id = f"tr_{uuid4().hex}"
    raw_relative = Path("raw-asr") / source_id / f"{run_id}.json"
    raw_path = store.project_path / raw_relative
    run = run_qwen_filetrans(
        source_path,
        raw_path,
        config=config,
        phase_callback=phase_callback,
    )
    try:
        raw: Any = json.loads(raw_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProjectError(
            "Qwen Filetrans raw result is missing or unreadable"
        ) from error

    if phase_callback is not None:
        phase_callback("transcription_normalizing")
    transcript = normalize_qwen(
        raw,
        source_id=source_id,
        transcript_version_id=transcript_id,
        raw_result_path=raw_relative.as_posix(),
        source_duration_ticks=source.probe.duration_ticks,
        started_at=run.started_at,
        completed_at=run.completed_at,
        exit_status=run.exit_status,
    )
    if phase_callback is not None:
        phase_callback("transcription_publishing_transcript")
    return _publish_transcript(
        store,
        project,
        expected_revision=expected_revision,
        transcript=transcript,
    )


def _publish_transcript(
    store: ProjectStore,
    project: Project,
    *,
    expected_revision: int,
    transcript: TimedTranscript,
) -> TimedTranscript:
    """Publish one immutable transcript and activate it in one revision."""

    transcript_path = (
        store.project_path
        / "transcripts"
        / transcript.source_id
        / f"{transcript.transcript_version_id}.json"
    )
    _write_json_atomically(transcript_path, transcript.to_dict())
    updated = replace(
        project,
        revision=project.revision + 1,
        updated_at=datetime.now(UTC).isoformat(),
        active_transcript_versions={
            **project.active_transcript_versions,
            transcript.source_id: transcript.transcript_version_id,
        },
    )
    try:
        store.save(updated, expected_revision=expected_revision)
    except Exception:
        transcript_path.unlink(missing_ok=True)
        raise
    return transcript


def read_transcript_page(
    project_path: Path,
    source_id: str,
    transcript_version_id: str,
    *,
    offset: int,
    limit: int,
) -> TranscriptPage:
    _validate_id(source_id, "source")
    _validate_id(transcript_version_id, "transcript")
    if offset < 0 or not 1 <= limit <= 200:
        raise ProjectError("transcript page offset or limit is invalid")
    root = project_path.resolve()
    transcript_path = root / "transcripts" / source_id / f"{transcript_version_id}.json"
    try:
        data: Any = json.loads(transcript_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProjectError("transcript is missing or unreadable") from error
    if not isinstance(data, dict):
        raise ProjectError("transcript must contain an object")
    transcript = TimedTranscript.from_dict(data)
    if transcript.source_id != source_id or transcript.transcript_version_id != transcript_version_id:
        raise ProjectError("transcript identity does not match its path")
    selected = transcript.segments[offset : offset + limit]
    consumed = offset + len(selected)
    next_offset = consumed if consumed < len(transcript.segments) else None
    return TranscriptPage(
        transcript_version_id=transcript_version_id,
        source_id=source_id,
        offset=offset,
        limit=limit,
        total_segments=len(transcript.segments),
        next_offset=next_offset,
        segments=tuple(segment.to_dict() for segment in selected),
    )


def _source(sources: tuple[SourceAsset, ...], source_id: str) -> SourceAsset:
    for source in sources:
        if source.source_id == source_id:
            return source
    raise ProjectError("project source does not exist")


def _validate_id(value: str, label: str) -> None:
    if re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise ProjectError(f"{label} id is invalid")


def _source_path(project_path: Path, source: SourceAsset) -> Path:
    if source.import_mode is ImportMode.COPIED:
        relative = source.locator.get("project_relative_path")
        if relative is None:
            raise ProjectError("copied source locator is missing")
        resolved = (project_path / relative).resolve(strict=True)
        if not resolved.is_relative_to(project_path):
            raise ProjectError("copied source path escapes the project")
        return resolved
    absolute = source.locator.get("absolute_path")
    if absolute is None:
        raise ProjectError("linked source locator is missing")
    return Path(absolute).resolve(strict=True)


def _write_json_atomically(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(payload, temporary_file, ensure_ascii=False, indent=2, sort_keys=True)
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
