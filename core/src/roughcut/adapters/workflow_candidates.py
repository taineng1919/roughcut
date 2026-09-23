"""Internal atomic publication helpers for fixed workflow participants."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from roughcut.domain.workflow import canonical_sha256_v1

_MACOS_RENAME_EXCL = 0x00000004


@dataclass(frozen=True)
class StagedWorkflowFile:
    """A complete, fsynced staging file to publish without loading it into memory."""

    path: Path


def stream_file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def workflow_candidate_temp_path(final_path: Path, action_id: str) -> Path:
    return final_path.with_name(f".{final_path.name}.{action_id}.candidate.tmp")


def workflow_export_staging_id(
    project_id: str, run_id: str, action_id: str, input_hash: str
) -> str:
    digest = canonical_sha256_v1(
        {
            "project_id": project_id,
            "run_id": run_id,
            "action_id": action_id,
            "input_hash": input_hash,
        }
    )
    return f"stg_{digest[:32]}"


def workflow_renderer_workspace_name(
    project_id: str,
    run_id: str,
    action_id: str,
    input_hash: str,
    staging_id: str,
    export_basis_id: str,
) -> str:
    digest = canonical_sha256_v1(
        {
            "project_id": project_id,
            "run_id": run_id,
            "action_id": action_id,
            "input_hash": input_hash,
            "staging_id": staging_id,
            "export_basis_id": export_basis_id,
        }
    )
    return f".renderer-{digest}"


def publish_workflow_candidate(
    final_path: Path,
    action_id: str,
    payload: dict[str, object] | StagedWorkflowFile,
) -> None:
    """Publish one marker-owned candidate without exposing partial final content."""

    final_path.parent.mkdir(parents=True, exist_ok=True)
    _sync_directory(final_path.parent)
    temp_path = workflow_candidate_temp_path(final_path, action_id)
    if os.path.lexists(final_path) or os.path.lexists(temp_path):
        raise FileExistsError(final_path)

    if isinstance(payload, StagedWorkflowFile):
        staged_details = _validate_single_link_regular(payload.path)
        destination_details = os.lstat(final_path.parent)
        if (
            stat.S_ISLNK(destination_details.st_mode)
            or not stat.S_ISDIR(destination_details.st_mode)
        ):
            raise ValueError("workflow candidate destination parent is unsafe")
        if staged_details.st_dev != destination_details.st_dev:
            raise OSError(
                errno.EXDEV,
                "workflow staged candidate is not on the destination filesystem",
                payload.path,
            )
        _sync_file(payload.path)
        source_path = payload.path
    else:
        with temp_path.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        _sync_directory(final_path.parent)
        source_path = temp_path

    source_parent = source_path.parent
    _atomic_no_replace_move(source_path, final_path)
    _validate_single_link_regular(final_path)
    if source_parent != final_path.parent:
        _sync_directory(source_parent)
    _sync_directory(final_path.parent)


def _atomic_no_replace_move(source: Path, destination: Path) -> None:
    if sys.platform == "darwin":
        _macos_rename_exclusive(source, destination)
        return
    if sys.platform == "win32":
        _windows_rename_no_replace(source, destination)
        return
    raise OSError(
        errno.ENOTSUP,
        "workflow candidate publication supports only macOS and Windows",
        destination,
    )


def _macos_rename_exclusive(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renamex_np = libc.renamex_np
    renamex_np.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
    renamex_np.restype = ctypes.c_int
    if (
        renamex_np(
            os.fsencode(source),
            os.fsencode(destination),
            _MACOS_RENAME_EXCL,
        )
        != 0
    ):
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), destination)


def _windows_rename_no_replace(source: Path, destination: Path) -> None:
    # On Windows CPython maps os.rename (not os.replace) to MoveFileExW with
    # flags=0: same-volume atomic move with destination-exists rejection.
    os.rename(source, destination)


def _validate_single_link_regular(path: Path) -> os.stat_result:
    details = os.lstat(path)
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISREG(details.st_mode)
        or details.st_nlink != 1
    ):
        raise ValueError("workflow staged candidate is not a single-link regular file")
    return details


def _sync_file(path: Path) -> None:
    mode = "r+b" if os.name == "nt" else "rb"
    with path.open(mode) as stream:
        os.fsync(stream.fileno())


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
