"""Installation-root store and transient writer lock for OperationRecord schema 1."""

from __future__ import annotations

import importlib
import os
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

from roughcut.domain.errors import WorkflowError
from roughcut.domain.installation_operation import (
    InstallationOperationError,
    InstallationOperationRecord,
    installation_scope_hash,
    validate_installation_transition,
    validate_operation_id,
)
from roughcut.domain.workflow import canonical_json_v1, load_closed_json

_LOCKS_GUARD = threading.Lock()
_WRITER_LOCKS: dict[str, threading.Lock] = {}


def _error(code: str, evidence: str) -> InstallationOperationError:
    return InstallationOperationError(
        code,
        f"Roughcut installation operation store {evidence}",
    )


class InstallationOperationStore:
    """Store installation records under one fixed install-root subtree."""

    def __init__(self, install_root: Path) -> None:
        self.install_root = Path(os.path.abspath(install_root))
        self.operations_root = self.install_root / "operations"
        self.records_root = self.operations_root / "component-installation"
        self.scope_hash = installation_scope_hash(str(self.install_root))

    def read(
        self,
        operation_id: str,
        *,
        allow_writer_temp: bool = False,
    ) -> InstallationOperationRecord | None:
        validate_operation_id(operation_id)
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
        if record.scope.install_root_hash != self.scope_hash:
            raise _error(
                "operation_integrity_error",
                "rejected a record copied from another installation scope",
            )
        return record

    def write_locked(
        self,
        record: InstallationOperationRecord,
    ) -> InstallationOperationRecord:
        if record.scope.install_root_hash != self.scope_hash:
            raise _error(
                "operation_integrity_error",
                "rejected a record for another installation scope",
            )
        self._ensure_tree()
        self._discard_owned_temp_locked(record.operation_id)
        existing = self.read(record.operation_id)
        if existing is not None:
            if existing == record:
                return existing
            validate_installation_transition(existing, record)

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
        except InstallationOperationError:
            self._cleanup_temp_after_write_failure(temporary)
            raise
        except OSError as error:
            self._cleanup_temp_after_write_failure(temporary)
            raise _error(
                "operation_write_failed",
                "could not atomically publish the installation record "
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
        """Try to own one operation writer without blocking."""

        validate_operation_id(operation_id)
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
                if not self._try_acquire_file_lock(lock_file):
                    yield False
                    return
                try:
                    self._discard_owned_temp_locked(operation_id)
                    yield True
                finally:
                    self._release_file_lock(lock_file)
        finally:
            process_lock.release()

    def _record_path(self, operation_id: str) -> Path:
        validate_operation_id(operation_id)
        return self.records_root / f"{operation_id}.json"

    def _temp_path(self, operation_id: str) -> Path:
        validate_operation_id(operation_id)
        return self.records_root / f".{operation_id}.json.tmp"

    def _lock_path(self, operation_id: str) -> Path:
        validate_operation_id(operation_id)
        return self.records_root / f".{operation_id}.writer.lock"

    def _ensure_tree(self) -> None:
        if not os.path.lexists(self.install_root):
            parent = self.install_root.parent
            self._validate_directory(parent)
            try:
                self.install_root.mkdir()
            except FileExistsError:
                self._validate_directory(self.install_root)
            except OSError as error:
                raise _error(
                    "operation_write_failed",
                    "could not create the controlled install root "
                    f"({type(error).__name__})",
                ) from error
            self._sync_directory(parent)
        self._validate_directory(self.install_root)
        for path in (self.operations_root, self.records_root):
            if not os.path.lexists(path):
                try:
                    path.mkdir()
                except FileExistsError:
                    self._validate_directory(path)
                except OSError as error:
                    raise _error(
                        "operation_write_failed",
                        "could not create controlled operation storage "
                        f"({type(error).__name__})",
                    ) from error
                self._sync_directory(path.parent)
            self._validate_directory(path)

    def _validate_existing_ancestors(self) -> None:
        for path in (self.install_root, self.operations_root):
            if not os.path.lexists(path):
                return
            self._validate_directory(path)

    def _validate_tree(self) -> None:
        self._validate_directory(self.install_root)
        self._validate_directory(self.operations_root)
        self._validate_directory(self.records_root)

    def _validate_temp(self, operation_id: str, *, allow: bool) -> None:
        temporary = self._temp_path(operation_id)
        if not os.path.lexists(temporary):
            return
        self._validate_regular_file(temporary)
        if not allow:
            raise _error(
                "operation_integrity_error",
                "found an un-reconciled operation temporary file",
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
                "could not clear an owned operation temporary file "
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
                "preserved operation temporary evidence after cleanup failed "
                f"({type(error).__name__})",
            ) from error

    def _read_record(self, path: Path) -> InstallationOperationRecord:
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
            record = InstallationOperationRecord.from_dict(data)
            if canonical_json_v1(record.to_dict()) + b"\n" != payload:
                raise _error(
                    "operation_integrity_error",
                    "rejected non-canonical record bytes",
                )
            return record
        except InstallationOperationError:
            raise
        except OSError as error:
            raise _error(
                "operation_integrity_error",
                f"could not read record ({type(error).__name__})",
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
    def _open_lock_file(self, lock_path: Path) -> Iterator[BinaryIO]:
        descriptor: int | None = None
        try:
            if os.name == "nt" and os.path.lexists(lock_path):
                self._validate_regular_file(lock_path)
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
            if os.name != "nt":
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
                    "rejected an unsafe operation writer lock",
                )
            stream = os.fdopen(descriptor, "r+b")
            descriptor = None
        except InstallationOperationError:
            if descriptor is not None:
                os.close(descriptor)
            raise
        except OSError as error:
            if descriptor is not None:
                os.close(descriptor)
            raise _error(
                "operation_integrity_error",
                f"could not safely open operation writer lock ({type(error).__name__})",
            ) from error
        with stream:
            yield stream

    @staticmethod
    def _try_acquire_file_lock(lock_file: BinaryIO) -> bool:
        if os.name == "nt":
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

    @staticmethod
    def _release_file_lock(lock_file: BinaryIO) -> None:
        if os.name == "nt":
            msvcrt: Any = importlib.import_module("msvcrt")
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
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
                "could not fsync controlled operation storage "
                f"({type(error).__name__})",
            ) from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
