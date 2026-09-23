"""Project store and transient writer lock for media OperationRecord schema 1."""

from __future__ import annotations

import ctypes
import importlib
import os
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, BinaryIO

from roughcut.domain.errors import WorkflowError
from roughcut.domain.media_operation import (
    MediaOperationError,
    MediaOperationRecord,
    ProjectOperationScope,
    project_operation_scope_hash,
    validate_media_operation_id,
    validate_media_operation_transition,
)
from roughcut.domain.workflow import canonical_json_v1, load_closed_json

_LOCKS_GUARD = threading.Lock()
_WRITER_LOCKS: dict[str, threading.Lock] = {}
_MEDIA_WRITER_FD: ContextVar[int | None] = ContextVar(
    "roughcut_media_writer_fd",
    default=None,
)


def _error(code: str, evidence: str) -> MediaOperationError:
    return MediaOperationError(
        code,
        f"Roughcut Project-media operation store {evidence}",
    )


_WINDOWS_SHARING_ERRORS = {32, 33}


def _open_windows_exclusive_lock(lock_path: Path) -> int | None:
    """Open the existing lock path with an inheritable share-deny handle."""

    from ctypes import wintypes

    win_dll: Any = ctypes.WinDLL  # type: ignore[attr-defined]
    kernel32 = win_dll("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = create_file(
        str(lock_path),
        0x80000000 | 0x40000000,
        0,
        None,
        4,
        0x00000080,
        None,
    )
    handle_value = handle.value if hasattr(handle, "value") else handle
    if handle_value == ctypes.c_void_p(-1).value:
        error_code = ctypes.get_last_error()  # type: ignore[attr-defined]
        if error_code in _WINDOWS_SHARING_ERRORS:
            return None
        raise OSError(error_code, "CreateFileW could not open media operation writer lock")

    try:
        msvcrt: Any = importlib.import_module("msvcrt")

        descriptor = msvcrt.open_osfhandle(
            handle_value,
            os.O_RDWR | getattr(os, "O_BINARY", 0),
        )
    except (OSError, OverflowError):
        close_handle(handle)
        raise
    return descriptor


def media_child_process_kwargs() -> dict[str, object]:
    """Keep the active media writer lock alive in a synchronous child process."""

    if _MEDIA_WRITER_FD.get() is None:
        return {}
    return {"close_fds": False}


class MediaOperationStore:
    """Store media operation records under one fixed Project subtree."""

    def __init__(self, project_root: Path, project_id: str) -> None:
        validate_media_operation_id(project_id)
        self.project_root = Path(os.path.abspath(project_root))
        self.workflow_root = self.project_root / "workflow"
        self.operations_root = self.workflow_root / "operations"
        self.records_root = self.operations_root / "media"
        self.scope = ProjectOperationScope(
            project_id=project_id,
            project_root_hash=project_operation_scope_hash(
                str(self.project_root)
            ),
        )

    def read(
        self,
        operation_id: str,
        *,
        allow_writer_temp: bool = False,
    ) -> MediaOperationRecord | None:
        validate_media_operation_id(operation_id)
        if not os.path.lexists(self.records_root):
            self._validate_existing_ancestors()
            return None
        self._validate_tree()
        self._validate_temp(operation_id, allow=allow_writer_temp)
        path = self._record_path(operation_id)
        if not os.path.lexists(path):
            return None
        record = self._read_record(path)
        if record.operation_id != operation_id:
            raise _error(
                "operation_integrity_error",
                "rejected a record whose operation ID differs from its path",
            )
        if record.scope != self.scope:
            raise _error(
                "operation_integrity_error",
                "rejected a record copied from another Project scope",
            )
        return record

    def write_locked(
        self,
        record: MediaOperationRecord,
    ) -> MediaOperationRecord:
        if record.scope != self.scope:
            raise _error(
                "operation_integrity_error",
                "rejected a record for another Project scope",
            )
        self._ensure_tree()
        self._discard_owned_temp_locked(record.operation_id)
        existing = self.read(record.operation_id)
        if existing is not None:
            if existing == record:
                return existing
            validate_media_operation_transition(existing, record)

        path = self._record_path(record.operation_id)
        temporary = self._temp_path(record.operation_id)
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(descriptor, "wb") as output:
                output.write(canonical_json_v1(record.to_dict()))
                output.write(b"\n")
                output.flush()
                os.fsync(output.fileno())
            self._sync_directory(self.records_root)
            readback = self._read_record(temporary)
            if readback != record:
                raise _error(
                    "operation_integrity_error",
                    "rejected changed temporary record readback",
                )
            os.replace(temporary, path)
            self._sync_directory(self.records_root)
        except (KeyboardInterrupt, SystemExit):
            raise
        except MediaOperationError:
            self._cleanup_temp_after_write_failure(temporary)
            raise
        except OSError as error:
            self._cleanup_temp_after_write_failure(temporary)
            raise _error(
                "operation_write_failed",
                "could not atomically publish the media operation record "
                f"({type(error).__name__})",
            ) from error
        stored = self.read(record.operation_id)
        if stored != record:
            raise _error(
                "operation_integrity_error",
                "rejected changed final record readback",
            )
        return record

    @contextmanager
    def writer(
        self,
        operation_id: str,
        *,
        create: bool,
    ) -> Iterator[bool]:
        """Try to own one media operation writer without blocking."""

        validate_media_operation_id(operation_id)
        lock_path = self._lock_path(operation_id)
        key = os.path.normcase(str(lock_path))
        with _LOCKS_GUARD:
            process_lock = _WRITER_LOCKS.setdefault(key, threading.Lock())
        if not process_lock.acquire(blocking=False):
            yield False
            return
        try:
            if create:
                self._ensure_tree()
            elif not os.path.lexists(self.records_root):
                self._validate_existing_ancestors()
                yield False
                return
            else:
                self._validate_tree()
            with self._open_lock_file(lock_path) as lock_file:
                if lock_file is None or not self._try_acquire_file_lock(lock_file):
                    yield False
                    return
                descriptor = lock_file.fileno()
                previous_inheritable = os.get_inheritable(descriptor)
                os.set_inheritable(descriptor, True)
                token = _MEDIA_WRITER_FD.set(descriptor)
                try:
                    self._discard_owned_temp_locked(operation_id)
                    yield True
                finally:
                    _MEDIA_WRITER_FD.reset(token)
                    os.set_inheritable(descriptor, previous_inheritable)
                    self._release_file_lock(lock_file)
        finally:
            process_lock.release()

    def _record_path(self, operation_id: str) -> Path:
        validate_media_operation_id(operation_id)
        return self.records_root / f"{operation_id}.json"

    def _temp_path(self, operation_id: str) -> Path:
        validate_media_operation_id(operation_id)
        return self.records_root / f".{operation_id}.json.tmp"

    def _lock_path(self, operation_id: str) -> Path:
        validate_media_operation_id(operation_id)
        return self.records_root / f".{operation_id}.writer.lock"

    def _ensure_tree(self) -> None:
        self._validate_directory(self.project_root)
        for path in (
            self.workflow_root,
            self.operations_root,
            self.records_root,
        ):
            if not os.path.lexists(path):
                try:
                    path.mkdir()
                except FileExistsError:
                    self._validate_directory(path)
                except OSError as error:
                    raise _error(
                        "operation_write_failed",
                        "could not create controlled media operation storage "
                        f"({type(error).__name__})",
                    ) from error
                self._sync_directory(path.parent)
            self._validate_directory(path)

    def _validate_existing_ancestors(self) -> None:
        for path in (
            self.project_root,
            self.workflow_root,
            self.operations_root,
        ):
            if not os.path.lexists(path):
                return
            self._validate_directory(path)

    def _validate_tree(self) -> None:
        for path in (
            self.project_root,
            self.workflow_root,
            self.operations_root,
            self.records_root,
        ):
            self._validate_directory(path)

    def _validate_temp(self, operation_id: str, *, allow: bool) -> None:
        temporary = self._temp_path(operation_id)
        if not os.path.lexists(temporary):
            return
        self._validate_regular_file(temporary)
        if not allow:
            raise _error(
                "operation_integrity_error",
                "found an un-reconciled media operation temporary file",
            )

    def _discard_owned_temp_locked(self, operation_id: str) -> None:
        temporary = self._temp_path(operation_id)
        if not os.path.lexists(temporary):
            return
        self._validate_regular_file(temporary)
        try:
            temporary.unlink()
            self._sync_directory(self.records_root)
        except OSError as error:
            raise _error(
                "operation_write_failed",
                "could not clear an owned media operation temporary file "
                f"({type(error).__name__})",
            ) from error

    def _cleanup_temp_after_write_failure(self, temporary: Path) -> None:
        if not os.path.lexists(temporary):
            return
        self._validate_regular_file(temporary)
        try:
            temporary.unlink()
            self._sync_directory(self.records_root)
        except OSError as error:
            raise _error(
                "operation_integrity_error",
                "preserved media operation temporary evidence after cleanup "
                f"failed ({type(error).__name__})",
            ) from error

    def _read_record(self, path: Path) -> MediaOperationRecord:
        self._validate_regular_file(path)
        try:
            payload = path.read_bytes()
            try:
                data = load_closed_json(payload)
            except WorkflowError as error:
                raise _error(
                    "operation_integrity_error",
                    "rejected unreadable or duplicate-key JSON",
                ) from error
            record = MediaOperationRecord.from_dict(data)
            if canonical_json_v1(record.to_dict()) + b"\n" != payload:
                raise _error(
                    "operation_integrity_error",
                    "rejected non-canonical media operation record bytes",
                )
            return record
        except MediaOperationError:
            raise
        except OSError as error:
            raise _error(
                "operation_integrity_error",
                f"could not read media operation record ({type(error).__name__})",
            ) from error

    @staticmethod
    def _validate_directory(path: Path) -> None:
        try:
            details = os.lstat(path)
        except OSError as error:
            raise _error(
                "operation_integrity_error",
                f"could not inspect controlled directory ({type(error).__name__})",
            ) from error
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            raise _error(
                "operation_integrity_error",
                "rejected an unsafe controlled directory",
            )

    @staticmethod
    def _validate_regular_file(path: Path) -> None:
        try:
            details = os.lstat(path)
        except OSError as error:
            raise _error(
                "operation_integrity_error",
                f"could not inspect controlled file ({type(error).__name__})",
            ) from error
        if (
            stat.S_ISLNK(details.st_mode)
            or not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
        ):
            raise _error(
                "operation_integrity_error",
                "rejected a symlink, hardlink, or non-regular operation file",
            )

    @contextmanager
    def _open_lock_file(self, lock_path: Path) -> Iterator[BinaryIO | None]:
        descriptor: int | None = None
        stream: BinaryIO | None = None
        try:
            if os.name == "nt":
                if os.path.lexists(lock_path):
                    self._validate_regular_file(lock_path)
                descriptor = _open_windows_exclusive_lock(lock_path)
                if descriptor is None:
                    yield None
                    return
            else:
                flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(lock_path, flags, 0o600)
            opened = os.fstat(descriptor)
            path_details = os.stat(lock_path, follow_symlinks=False)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or not stat.S_ISREG(path_details.st_mode)
                or path_details.st_nlink != 1
                or opened.st_dev != path_details.st_dev
                or opened.st_ino != path_details.st_ino
            ):
                raise _error(
                    "operation_integrity_error",
                    "rejected an unsafe media operation writer lock",
                )
            stream = os.fdopen(descriptor, "r+b")
            descriptor = None
        except MediaOperationError:
            if descriptor is not None:
                os.close(descriptor)
            raise
        except OSError as error:
            if descriptor is not None:
                os.close(descriptor)
            raise _error(
                "operation_integrity_error",
                "could not safely open media operation writer lock "
                f"({type(error).__name__})",
            ) from error
        assert stream is not None
        with stream:
            yield stream

    @staticmethod
    def _try_acquire_file_lock(lock_file: BinaryIO) -> bool:
        if os.name == "nt":
            # Windows acquisition is the share-deny CreateFileW open in
            # _open_windows_exclusive_lock. Closing the inherited handle is
            # the corresponding release, so no CRT byte lock is needed.
            return True
        import fcntl

        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    @staticmethod
    def _release_file_lock(lock_file: BinaryIO) -> None:
        if os.name == "nt":
            return
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _sync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor: int | None = None
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            os.fsync(descriptor)
        except OSError as error:
            raise _error(
                "operation_write_failed",
                "could not fsync controlled media operation storage "
                f"({type(error).__name__})",
            ) from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
