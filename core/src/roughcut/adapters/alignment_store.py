"""Project-contained immutable Multicam Alignment artifact store."""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import sys
from pathlib import Path

from roughcut.domain.alignment import (
    AlignmentError,
    MulticamAlignmentArtifact,
    _safe_id,
)
from roughcut.domain.workflow import canonical_json_v1, load_closed_json

ALIGNMENT_ARTIFACT_DIRECTORY = "artifacts/multicam-alignments"
_MACOS_RENAME_EXCL = 0x00000004


def _error(code: str, evidence: str) -> AlignmentError:
    return AlignmentError(
        code,
        f"Roughcut multicam alignment store {evidence}",
    )


def _atomic_no_replace_move(source: Path, destination: Path) -> None:
    """Same-volume atomic no-replace move (macOS renamex_np RENAME_EXCL,
    Windows os.rename). Extracted from the workflow candidate publisher."""
    if sys.platform == "darwin":
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
        return
    if sys.platform == "win32":
        # CPython maps os.rename to MoveFileExW with flags=0: same-volume
        # atomic move with destination-exists rejection (no replace).
        os.rename(source, destination)
        return
    raise OSError(
        errno.ENOTSUP,
        "alignment artifact publication supports only macOS and Windows",
        destination,
    )


class AlignmentStore:
    """Store immutable alignment artifacts under one Project subtree."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(os.path.abspath(project_root))
        self.artifacts_root = (
            self.project_root / ALIGNMENT_ARTIFACT_DIRECTORY
        )

    def read(self, alignment_id: str) -> MulticamAlignmentArtifact | None:
        """Read exactly one artifact by its exact caller-provided ID."""
        _safe_id(alignment_id, field="alignment_id")
        if not os.path.lexists(self.artifacts_root):
            self._validate_ancestors()
            return None
        self._validate_tree()
        path = self._artifact_path(alignment_id)
        if not os.path.lexists(path):
            return None
        return self._read_artifact(path, alignment_id)

    def publish(
        self, alignment_id: str, artifact: MulticamAlignmentArtifact
    ) -> MulticamAlignmentArtifact:
        """Atomically publish one immutable artifact with exact readback."""
        _safe_id(alignment_id, field="alignment_id")
        if artifact.alignment_id != alignment_id:
            raise _error(
                "alignment_integrity_error",
                "rejected an artifact whose ID differs from its target",
            )
        self._ensure_tree()
        path = self._artifact_path(alignment_id)
        if os.path.lexists(path):
            existing = self._read_artifact(path, alignment_id)
            if existing == artifact:
                return existing
            raise _error(
                "alignment_publish_conflict",
                "refused to overwrite an existing different artifact",
            )
        temporary = self._unique_temp_path(alignment_id)
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
                output.write(canonical_json_v1(artifact.to_dict()))
                output.write(b"\n")
                output.flush()
                os.fsync(output.fileno())
            self._sync_directory(self.artifacts_root)
            readback = self._read_artifact(temporary, alignment_id)
            if readback != artifact:
                raise _error(
                    "alignment_store_integrity_error",
                    "rejected changed temporary artifact readback",
                )
            try:
                _atomic_no_replace_move(temporary, path)
            except FileExistsError:
                # a concurrent publisher won; the identical-bytes case is
                # handled by the readback above, otherwise this is a conflict
                existing = self._read_artifact(path, alignment_id)
                if existing != artifact:
                    raise _error(
                        "alignment_publish_conflict",
                        "refused to overwrite a concurrently published artifact",
                    )
                temporary.unlink(missing_ok=True)
                self._sync_directory(self.artifacts_root)
                return artifact
            self._sync_directory(self.artifacts_root)
            final_details = self._validate_regular_file(path)
            if final_details.st_nlink != 1:
                raise _error(
                    "alignment_store_integrity_error",
                    "rejected a published artifact with multiple links",
                )
        except (KeyboardInterrupt, SystemExit):
            temporary.unlink(missing_ok=True)
            raise
        except AlignmentError:
            self._cleanup_temp(temporary)
            raise
        except OSError as error:
            self._cleanup_temp(temporary)
            raise _error(
                "alignment_publish_failed",
                f"could not atomically publish the artifact ({type(error).__name__})",
            ) from error
        final = self.read(alignment_id)
        if final != artifact:
            raise _error(
                "alignment_store_integrity_error",
                "rejected changed final artifact readback",
            )
        return artifact

    def _artifact_path(self, alignment_id: str) -> Path:
        _safe_id(alignment_id, field="alignment_id")
        return self.artifacts_root / f"{alignment_id}.json"

    def _temp_path(self, alignment_id: str) -> Path:
        _safe_id(alignment_id, field="alignment_id")
        return self.artifacts_root / f".{alignment_id}.json.tmp"

    def _unique_temp_path(self, alignment_id: str) -> Path:
        """Per-writer unique temporary so concurrent publishers never collide."""
        _safe_id(alignment_id, field="alignment_id")
        unique = f"{os.getpid()}-{os.urandom(4).hex()}"
        return self.artifacts_root / f".{alignment_id}.{unique}.json.tmp"

    def _ensure_tree(self) -> None:
        self._validate_directory(self.project_root)
        current = self.project_root
        for part in ("artifacts", "multicam-alignments"):
            current = current / part
            if not os.path.lexists(current):
                try:
                    current.mkdir()
                except FileExistsError:
                    self._validate_directory(current)
                except OSError as error:
                    raise _error(
                        "alignment_publish_failed",
                        "could not create controlled alignment storage "
                        f"({type(error).__name__})",
                    ) from error
                self._sync_directory(current.parent)
            self._validate_directory(current)

    def _validate_ancestors(self) -> None:
        current = self.project_root
        for part in ("artifacts", "multicam-alignments"):
            current = current / part
            if not os.path.lexists(current):
                return
            self._validate_directory(current)

    def _validate_tree(self) -> None:
        current = self.project_root
        for part in ("artifacts", "multicam-alignments"):
            current = current / part
            self._validate_directory(current)

    def _read_artifact(
        self, path: Path, alignment_id: str
    ) -> MulticamAlignmentArtifact:
        self._validate_regular_file(path)
        try:
            payload = path.read_bytes()
        except OSError as error:
            raise _error(
                "alignment_store_integrity_error",
                f"could not read the artifact ({type(error).__name__})",
            ) from error
        try:
            data = load_closed_json(payload)
        except Exception as error:
            raise _error(
                "alignment_store_integrity_error",
                "rejected unreadable or duplicate-key JSON",
            ) from error
        artifact = MulticamAlignmentArtifact.from_dict(data)
        if artifact.alignment_id != alignment_id:
            raise _error(
                "alignment_store_integrity_error",
                "rejected an artifact whose ID differs from its path",
            )
        if canonical_json_v1(artifact.to_dict()) + b"\n" != payload:
            raise _error(
                "alignment_store_integrity_error",
                "rejected non-canonical artifact bytes",
            )
        return artifact

    def _cleanup_temp(self, temporary: Path) -> None:
        if not os.path.lexists(temporary):
            return
        self._validate_regular_file(temporary)
        try:
            temporary.unlink()
            self._sync_directory(self.artifacts_root)
        except OSError as error:
            raise _error(
                "alignment_store_integrity_error",
                "preserved temporary artifact evidence after cleanup "
                f"failed ({type(error).__name__})",
            ) from error

    @staticmethod
    def _validate_directory(path: Path) -> None:
        try:
            details = os.lstat(path)
        except OSError as error:
            raise _error(
                "alignment_store_integrity_error",
                f"could not inspect controlled directory ({type(error).__name__})",
            ) from error
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            raise _error(
                "alignment_store_integrity_error",
                "rejected an unsafe controlled directory",
            )

    @staticmethod
    def _validate_regular_file(path: Path) -> os.stat_result:
        try:
            details = os.lstat(path)
        except OSError as error:
            raise _error(
                "alignment_store_integrity_error",
                f"could not inspect controlled file ({type(error).__name__})",
            ) from error
        if (
            stat.S_ISLNK(details.st_mode)
            or not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
        ):
            raise _error(
                "alignment_store_integrity_error",
                "rejected a symlink, hardlink, or non-regular artifact file",
            )
        return details

    @staticmethod
    def _sync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor: int | None = None
        try:
            descriptor = os.open(
                path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            os.fsync(descriptor)
        except OSError as error:
            raise _error(
                "alignment_publish_failed",
                f"could not fsync controlled alignment storage ({type(error).__name__})",
            ) from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
