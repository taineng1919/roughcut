"""Source fingerprinting and import services."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from roughcut.adapters.ffprobe import probe_media
from roughcut.adapters.project_store import ProjectStore
from roughcut.domain.asr import ASR_CLOUD_TAG, source_filename_declares_cloud
from roughcut.domain.project import (
    ImportMode,
    Project,
    ProjectError,
    SourceAsset,
    SourceFingerprint,
)

FINGERPRINT_CHUNK_SIZE = 1024 * 1024
COPY_CHUNK_SIZE = 1024 * 1024


def fingerprint_file(source_path: Path) -> SourceFingerprint:
    stat = source_path.stat()
    digest = hashlib.sha256()
    digest.update(stat.st_size.to_bytes(8, "big", signed=False))
    with source_path.open("rb") as source_file:
        digest.update(source_file.read(FINGERPRINT_CHUNK_SIZE))
        if stat.st_size > FINGERPRINT_CHUNK_SIZE:
            source_file.seek(max(FINGERPRINT_CHUNK_SIZE, stat.st_size - FINGERPRINT_CHUNK_SIZE))
            digest.update(source_file.read(FINGERPRINT_CHUNK_SIZE))
    return SourceFingerprint(
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        sha256_head_tail=digest.hexdigest(),
    )


def add_source(
    project_path: Path,
    source_path: Path,
    import_mode: ImportMode,
    *,
    expected_revision: int,
) -> Project:
    original_filename = source_path.name
    resolved_source = source_path.resolve(strict=True)
    if not resolved_source.is_file():
        raise ProjectError("source path is not a regular file")
    store = ProjectStore(project_path)
    project = store.load()
    if project.revision != expected_revision:
        raise ProjectError("project revision conflict")

    fingerprint = fingerprint_file(resolved_source)
    probe = probe_media(resolved_source)
    source_id = f"src_{uuid4().hex}"
    copied_path: Path | None = None
    if import_mode is ImportMode.COPIED:
        suffix = resolved_source.suffix
        relative_path = Path("sources") / f"{source_id}{suffix}"
        copied_path = store.project_path / relative_path
        _copy_file_atomically(resolved_source, copied_path)
        locator = {"project_relative_path": relative_path.as_posix()}
    else:
        locator = {"absolute_path": str(resolved_source)}

    asset = SourceAsset(
        source_id=source_id,
        kind="video" if probe.video_codec is not None else "audio",
        display_name=resolved_source.name,
        import_mode=import_mode,
        locator=locator,
        fingerprint=fingerprint,
        probe=probe,
        tags=(ASR_CLOUD_TAG,) if source_filename_declares_cloud(original_filename) else (),
    )
    updated = replace(
        project,
        revision=project.revision + 1,
        updated_at=datetime.now(UTC).isoformat(),
        sources=(*project.sources, asset),
    )
    try:
        store.save(updated, expected_revision=expected_revision)
    except Exception:
        if copied_path is not None:
            copied_path.unlink(missing_ok=True)
        raise
    return updated


def _copy_file_atomically(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with source.open("rb") as source_file, tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as destination_file:
            temporary_path = Path(destination_file.name)
            shutil.copyfileobj(source_file, destination_file, length=COPY_CHUNK_SIZE)
            destination_file.flush()
            os.fsync(destination_file.fileno())
        shutil.copystat(source, temporary_path)
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
