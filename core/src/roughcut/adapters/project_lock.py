"""Cross-platform, reentrant Project write lock."""

from __future__ import annotations

import importlib
import os
import stat
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

from roughcut.domain.errors import WorkflowError

LOCK_FILENAME = ".roughcut-project.lock"
_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_EXPORT_CLAIM_LOCKS: dict[str, threading.Lock] = {}
_LOCAL = threading.local()


def _lock_error(evidence: str) -> WorkflowError:
    return WorkflowError(
        "workflow_lock_failed",
        f"Roughcut project storage failed to acquire Project write lock: {evidence}",
    )


def _integrity_error(evidence: str) -> WorkflowError:
    return WorkflowError(
        "workflow_lock_failed",
        f"Roughcut project storage rejected unsafe Project write lock: {evidence}",
    )


@contextmanager
def project_write_lock(project_root: Path) -> Iterator[None]:
    """Serialize ProjectStore and workflow-store writes for one resolved root."""

    resolved_root = project_root.resolve()
    key = os.path.normcase(str(resolved_root))
    with _LOCKS_GUARD:
        process_lock = _THREAD_LOCKS.setdefault(key, threading.RLock())
    with process_lock:
        depths = getattr(_LOCAL, "depths", None)
        if depths is None:
            depths = {}
            _LOCAL.depths = depths
        depth = depths.get(key, 0)
        if depth:
            depths[key] = depth + 1
            try:
                yield
            finally:
                depths[key] -= 1
            return

        if not resolved_root.is_dir():
            raise _lock_error(f"project root is not a directory: {resolved_root}")
        lock_path = resolved_root / LOCK_FILENAME
        depths[key] = 1
        try:
            with _open_lock_file(lock_path) as lock_file:
                try:
                    _acquire_file_lock(lock_file)
                except OSError as error:
                    raise _lock_error(
                        f"{type(error).__name__} while locking {lock_path.name}: {error}"
                    ) from error
                try:
                    yield
                finally:
                    try:
                        _release_file_lock(lock_file)
                    except OSError as error:
                        raise _lock_error(
                            f"{type(error).__name__} while unlocking {lock_path.name}: {error}"
                        ) from error
        finally:
            depths.pop(key, None)


@contextmanager
def project_export_claim(project_root: Path) -> Iterator[None]:
    """Acquire the non-blocking per-Project transient export claim."""

    resolved_root = project_root.resolve()
    staging_path = resolved_root / "workflow" / "export-staging"
    key = os.path.normcase(str(staging_path))
    with _LOCKS_GUARD:
        process_lock = _EXPORT_CLAIM_LOCKS.setdefault(key, threading.Lock())
    if not process_lock.acquire(blocking=False):
        raise WorkflowError(
            "workflow_export_in_progress",
            "Roughcut workflow façade found a live per-Project export claim",
        )
    try:
        staging_root = _export_staging_root(resolved_root, create=True)
        assert staging_root is not None
        lock_path = staging_root / ".claim.lock"
        with _open_lock_file(lock_path) as lock_file:
            if not _try_acquire_file_lock(lock_file):
                raise WorkflowError(
                    "workflow_export_in_progress",
                    "Roughcut workflow façade found a live per-Project export claim",
                )
            try:
                yield
            finally:
                _release_file_lock(lock_file)
    finally:
        process_lock.release()


def project_export_claim_is_busy(project_root: Path) -> bool:
    """Return whether the already-created transient export claim is held."""

    resolved_root = project_root.resolve()
    staging_root = _export_staging_root(resolved_root, create=False)
    if staging_root is None:
        return False
    lock_path = staging_root / ".claim.lock"
    if not os.path.lexists(lock_path):
        return False
    key = os.path.normcase(str(staging_root))
    with _LOCKS_GUARD:
        process_lock = _EXPORT_CLAIM_LOCKS.setdefault(key, threading.Lock())
    if not process_lock.acquire(blocking=False):
        return True
    try:
        with _open_lock_file(lock_path) as lock_file:
            acquired = _try_acquire_file_lock(lock_file)
            if acquired:
                _release_file_lock(lock_file)
            return not acquired
    finally:
        process_lock.release()


def _export_staging_root(
    resolved_root: Path, *, create: bool
) -> Path | None:
    workflow_root = resolved_root / "workflow"
    if not os.path.lexists(workflow_root):
        if not create:
            return None
        raise WorkflowError(
            "workflow_recovery_conflict",
            "Roughcut core/store cannot create export staging without workflow storage",
        )
    workflow_stat = os.lstat(workflow_root)
    if stat.S_ISLNK(workflow_stat.st_mode) or not stat.S_ISDIR(workflow_stat.st_mode):
        raise WorkflowError(
            "workflow_recovery_conflict",
            "Roughcut core/store rejected unsafe workflow storage for export claim",
        )
    staging_root = workflow_root / "export-staging"
    if os.path.lexists(staging_root):
        _validate_export_staging_root(staging_root)
    elif create:
        try:
            staging_root.mkdir()
        except FileExistsError:
            pass
        except OSError as error:
            raise _lock_error(
                "create export staging root failed: "
                f"{type(error).__name__}: {error}"
            ) from error
        _validate_export_staging_root(staging_root)
    else:
        return None
    return staging_root


def _validate_export_staging_root(staging_root: Path) -> None:
    try:
        staging_stat = os.lstat(staging_root)
    except OSError as error:
        raise _lock_error(
            "verify export staging root after creation failed: "
            f"{type(error).__name__}: {error}"
        ) from error
    if stat.S_ISLNK(staging_stat.st_mode) or not stat.S_ISDIR(staging_stat.st_mode):
        raise WorkflowError(
            "workflow_recovery_conflict",
            "Roughcut core/store rejected unsafe export staging root",
        )


@contextmanager
def _open_lock_file(lock_path: Path) -> Iterator[BinaryIO]:
    descriptor: int | None = None
    try:
        if _uses_windows_lock() and os.path.lexists(lock_path):
            _validate_lock_stat(os.lstat(lock_path), lock_path)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        if not _uses_windows_lock():
            flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        opened_stat = os.fstat(descriptor)
        _validate_lock_stat(opened_stat, lock_path)
        path_stat = os.stat(lock_path, follow_symlinks=False)
        _validate_lock_stat(path_stat, lock_path)
        if (
            opened_stat.st_dev != path_stat.st_dev
            or opened_stat.st_ino != path_stat.st_ino
        ):
            raise _integrity_error(f"lock target changed while opening: {lock_path.name}")
        lock_file = os.fdopen(descriptor, "r+b")
        descriptor = None
    except WorkflowError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise _integrity_error(
            f"cannot safely open {lock_path.name}: {type(error).__name__}: {error}"
        ) from error
    with lock_file:
        yield lock_file


def _validate_lock_stat(lock_stat: os.stat_result, lock_path: Path) -> None:
    if not stat.S_ISREG(lock_stat.st_mode):
        raise _integrity_error(f"lock target is not a regular file: {lock_path.name}")
    if lock_stat.st_nlink != 1:
        raise _integrity_error(f"lock target has {lock_stat.st_nlink} hard links")


def _uses_windows_lock() -> bool:
    return os.name == "nt"


def _acquire_file_lock(lock_file: BinaryIO) -> None:
    if _uses_windows_lock():
        msvcrt: Any = importlib.import_module("msvcrt")
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
            os.fsync(lock_file.fileno())
        while True:
            lock_file.seek(0)
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                time.sleep(0.01)
    else:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)


def _try_acquire_file_lock(lock_file: BinaryIO) -> bool:
    if _uses_windows_lock():
        msvcrt: Any = importlib.import_module("msvcrt")
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
            os.fsync(lock_file.fileno())
        lock_file.seek(0)
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _release_file_lock(lock_file: BinaryIO) -> None:
    if _uses_windows_lock():
        msvcrt: Any = importlib.import_module("msvcrt")
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
