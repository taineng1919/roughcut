"""Operation-owned staging and atomic no-replace publication for parallel renders."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import stat
import sys
from pathlib import Path
from typing import cast

from roughcut.domain.multicam_parallel import ParallelRenderError, validate_manifest
from roughcut.domain.workflow import (
    canonical_json_v1,
    canonical_sha256_v1,
    load_closed_json,
    validate_safe_id,
    validate_sha256,
)

_MACOS_RENAME_EXCL = 0x00000004
_PUBLISH_INTENT_FILENAME = ".publish-intent.json"
_PUBLISH_INTENT_FIELDS = {
    "operation_id",
    "parallel_render_id",
    "manifest_content_hash",
}


def _error(code: str, evidence: str) -> ParallelRenderError:
    return ParallelRenderError(code, f"Roughcut parallel render store {evidence}")


def _atomic_no_replace_move(source: Path, destination: Path) -> None:
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        renamex_np = libc.renamex_np
        renamex_np.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        renamex_np.restype = ctypes.c_int
        if renamex_np(os.fsencode(source), os.fsencode(destination), _MACOS_RENAME_EXCL) != 0:
            number = ctypes.get_errno()
            raise OSError(number, os.strerror(number), destination)
        return
    if sys.platform == "win32":
        os.rename(source, destination)
        return
    raise OSError(errno.ENOTSUP, "parallel render publication supports macOS and Windows", destination)


class MulticamParallelStore:
    """Only the owning worker may create/remove staging or publish its final tree."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(os.path.abspath(project_root))
        self.renders_root = self.project_root / "renders"
        self.staging_root = self.renders_root / "multicam-staging"
        self.final_root = self.renders_root / "multicam"

    def staging_path(self, operation_id: str) -> Path:
        validate_safe_id(operation_id, field="operation_id")
        return self.staging_root / operation_id

    def final_path(self, parallel_render_id: str) -> Path:
        validate_safe_id(parallel_render_id, field="parallel_render_id")
        return self.final_root / parallel_render_id

    def create_staging(self, operation_id: str) -> Path:
        staging = self.staging_path(operation_id)
        self._ensure_directory(self.project_root)
        self._ensure_directory(self.renders_root)
        self._ensure_directory(self.staging_root)
        self._ensure_directory(self.final_root)
        if os.path.lexists(staging):
            self._validate_directory(staging)
            raise _error("parallel_render_staging_failed", "refused an existing operation staging directory")
        try:
            staging.mkdir()
            self._sync(self.staging_root)
        except OSError as error:
            raise _error("parallel_render_staging_failed", "could not create operation staging") from error
        return staging

    def remove_staging(self, operation_id: str) -> None:
        staging = self.staging_path(operation_id)
        if not os.path.lexists(staging):
            return
        self._validate_directory(staging)
        try:
            for child in staging.iterdir():
                if child.is_symlink() or child.is_dir():
                    raise _error("parallel_render_staging_failed", "staging contains an unsafe child")
                self._validate_file(child)
                child.unlink()
            staging.rmdir()
            self._sync(self.staging_root)
        except ParallelRenderError:
            raise
        except OSError as error:
            raise _error("parallel_render_staging_failed", "could not remove operation staging") from error

    def write_manifest(self, staging: Path, manifest: dict[str, object]) -> Path:
        validate_manifest(manifest)
        producer = manifest["producer"]
        assert isinstance(producer, dict)
        self._validate_staging_owned(staging, operation_id=str(producer["operation_id"]))
        path = staging / "manifest.json"
        self._write_new_file(path, canonical_json_v1(manifest) + b"\n")
        readback = self.read_manifest_file(path)
        if readback != manifest:
            raise _error("parallel_render_staging_failed", "manifest strict readback changed")
        return path

    def read_manifest_file(self, path: Path) -> dict[str, object]:
        self._validate_file(path)
        try:
            payload = path.read_bytes()
            data = load_closed_json(payload)
        except Exception as error:
            raise _error("parallel_render_publish_failed", "manifest JSON is not readable") from error
        if not isinstance(data, dict) or canonical_json_v1(data) + b"\n" != payload:
            raise _error("parallel_render_publish_failed", "manifest bytes are not canonical")
        validate_manifest(data)
        return data

    def publish(self, staging: Path, parallel_render_id: str, manifest: dict[str, object]) -> Path:
        final = self.final_path(parallel_render_id)
        if manifest.get("parallel_render_id") != parallel_render_id:
            raise _error("parallel_render_publish_failed", "manifest ID differs from final identity")
        producer = manifest.get("producer")
        if not isinstance(producer, dict) or not isinstance(producer.get("operation_id"), str):
            raise _error("parallel_render_publish_failed", "manifest producer is not closed")
        try:
            self._validate_staging_owned(staging, operation_id=producer["operation_id"])
        except ParallelRenderError as error:
            if (
                not os.path.lexists(staging)
                and staging == self.staging_path(producer["operation_id"])
                and os.path.lexists(final)
            ):
                raise _error("parallel_render_final_conflict", "a competing worker already published the final directory") from error
            raise
        readback = self.read_manifest_file(staging / "manifest.json")
        if readback != manifest:
            raise _error("parallel_render_publish_failed", "publish manifest argument differs from staging readback")
        self._validate_staging_tree(staging, manifest)
        if os.stat(staging).st_dev != os.stat(self.final_root).st_dev:
            raise _error("parallel_render_publish_failed", "staging and final are on different filesystems")
        operation_id = cast(str, producer["operation_id"])
        manifest_content_hash = canonical_sha256_v1(manifest)
        self._write_publish_intent(
            staging,
            operation_id=operation_id,
            parallel_render_id=parallel_render_id,
            manifest_content_hash=manifest_content_hash,
        )
        self._validate_staging_tree(staging, manifest, allow_publish_intent=True)
        try:
            _atomic_no_replace_move(staging, final)
        except FileExistsError as error:
            raise _error("parallel_render_final_conflict", "refused a competing final directory") from error
        except OSError as error:
            if os.path.lexists(final):
                raise _error("parallel_render_final_conflict", "refused a competing final directory") from error
            raise _error("parallel_render_publish_failed", "atomic directory publication failed") from error
        try:
            self._sync(self.final_root)
            self._validate_final_tree(final, manifest, allow_publish_intent=True)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as error:
            primary = (
                error
                if isinstance(error, ParallelRenderError)
                and error.code
                in {"parallel_render_publish_failed", "parallel_render_final_conflict"}
                else _error("parallel_render_publish_failed", "final tree publication validation failed")
            )
            try:
                self._rollback_post_move(
                    final,
                    staging,
                    operation_id=operation_id,
                    parallel_render_id=parallel_render_id,
                    manifest=manifest,
                    manifest_content_hash=manifest_content_hash,
                )
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as recovery_error:
                raise ParallelRenderError(
                    primary.code,
                    "Roughcut parallel render post-move publish recovery failed",
                    recovery_failed=True,
                ) from recovery_error
            raise primary from error
        return final

    def read_published_manifest(self, parallel_render_id: str) -> dict[str, object] | None:
        final = self.final_path(parallel_render_id)
        if not os.path.lexists(final):
            return None
        self._validate_directory(final)
        intent = final / _PUBLISH_INTENT_FILENAME
        if os.path.lexists(intent):
            publish_intent = self._read_publish_intent(intent)
            if publish_intent["parallel_render_id"] != parallel_render_id:
                raise _error("parallel_render_publish_failed", "publish intent identity changed")
            manifest = self.read_manifest_file(final / "manifest.json")
            if canonical_sha256_v1(manifest) != publish_intent["manifest_content_hash"]:
                raise _error("parallel_render_publish_failed", "published manifest hash changed")
            self._validate_staging_tree(final, manifest, allow_publish_intent=True)
            self._require_succeeded_operation(publish_intent)
            return manifest
        return self.read_manifest_file(final / "manifest.json")

    def _require_succeeded_operation(self, publish_intent: dict[str, str]) -> None:
        """Accept a retained marker only after the exact operation commit."""

        from roughcut.adapters.media_operation_store import MediaOperationStore
        from roughcut.adapters.project_store import ProjectStore
        from roughcut.domain.errors import ProjectError
        from roughcut.domain.media_operation import (
            MediaOperationError,
            ParallelRenderOperationResult,
        )

        try:
            project = ProjectStore(self.project_root).load()
            record = MediaOperationStore(
                self.project_root,
                project.project_id,
            ).read(
                publish_intent["operation_id"],
                allow_writer_temp=True,
            )
        except (MediaOperationError, OSError, ProjectError) as error:
            raise _error(
                "parallel_render_publish_failed",
                "unresolved publish intent operation record is unreadable",
            ) from error
        if (
            record is None
            or record.status != "succeeded"
            or record.operation_type != "render_multicam_parallel"
            or not isinstance(record.result_ref, ParallelRenderOperationResult)
            or record.result_ref.parallel_render_id
            != publish_intent["parallel_render_id"]
            or record.result_ref.manifest_content_hash
            != publish_intent["manifest_content_hash"]
        ):
            raise _error(
                "parallel_render_publish_failed",
                "unresolved publish intent has no exact succeeded operation record",
            )

    def clear_publish_intent(
        self,
        parallel_render_id: str,
        operation_id: str,
        manifest_content_hash: str,
    ) -> None:
        """Finalize one already-recorded success without scanning output directories."""

        final = self.final_path(parallel_render_id)
        validate_safe_id(operation_id, field="operation_id")
        validate_sha256(manifest_content_hash, field="manifest_content_hash")
        if not os.path.lexists(final):
            raise _error("parallel_render_publish_failed", "success final is missing")
        self._validate_directory(final)
        intent_path = final / _PUBLISH_INTENT_FILENAME
        if not os.path.lexists(intent_path):
            return
        intent = self._read_publish_intent(intent_path)
        expected_intent = {
            "operation_id": operation_id,
            "parallel_render_id": parallel_render_id,
            "manifest_content_hash": manifest_content_hash,
        }
        if intent != expected_intent:
            raise _error("parallel_render_publish_failed", "publish intent identity changed")
        manifest = self.read_manifest_file(final / "manifest.json")
        if canonical_sha256_v1(manifest) != manifest_content_hash:
            raise _error("parallel_render_publish_failed", "published manifest hash changed")
        self._validate_staging_tree(final, manifest, allow_publish_intent=True)
        try:
            intent_path.unlink()
            self._sync(final)
        except OSError as error:
            raise _error("parallel_render_publish_failed", "could not clear publish intent") from error

    def _validate_staging_owned(self, staging: Path, *, operation_id: str) -> None:
        validate_safe_id(operation_id, field="operation_id")
        expected_parent = self.staging_root.resolve()
        try:
            if staging.resolve().parent != expected_parent:
                raise ValueError
            if staging.name != operation_id:
                raise ValueError
        except (OSError, ValueError) as error:
            raise _error("parallel_render_staging_failed", "staging escapes the operation staging root") from error
        self._validate_directory(staging)

    def _validate_staging_tree(
        self,
        staging: Path,
        manifest: dict[str, object],
        *,
        allow_publish_intent: bool = False,
    ) -> None:
        expected = {"manifest.json"}
        if allow_publish_intent:
            expected.add(_PUBLISH_INTENT_FILENAME)
        cameras = cast(list[dict[str, object]], manifest["cameras"])
        for camera in cameras:
            if camera["render_status"] == "succeeded":
                output = camera["output"]
                assert isinstance(output, dict)
                expected.add(cast(str, output["filename"]))
        names = {child.name for child in staging.iterdir()}
        if names != expected:
            raise _error("parallel_render_publish_failed", "staging tree has unexpected files")
        for name in expected:
            self._validate_file(staging / name)
        for camera in cameras:
            if camera["render_status"] != "succeeded":
                continue
            output = camera["output"]
            assert isinstance(output, dict)
            path = staging / cast(str, output["filename"])
            if path.stat().st_size != cast(int, output["bytes"]) or _sha256_file(path) != cast(str, output["content_hash"]):
                raise _error("parallel_render_publish_failed", "camera output readback differs from its manifest")

    def _validate_final_tree(
        self,
        final: Path,
        manifest: dict[str, object],
        *,
        allow_publish_intent: bool = False,
    ) -> None:
        self._validate_directory(final)
        self._validate_staging_tree(
            final,
            manifest,
            allow_publish_intent=allow_publish_intent,
        )
        readback = self.read_manifest_file(final / "manifest.json")
        if readback != manifest:
            raise _error("parallel_render_publish_failed", "final manifest strict readback changed")

    def _rollback_post_move(
        self,
        final: Path,
        staging: Path,
        *,
        operation_id: str,
        parallel_render_id: str,
        manifest: dict[str, object],
        manifest_content_hash: str,
    ) -> None:
        if os.path.lexists(staging):
            raise _error("parallel_render_publish_failed", "rollback staging path is occupied")
        self._validate_directory(final)
        self._validate_staging_tree(final, manifest, allow_publish_intent=True)
        readback = self.read_manifest_file(final / "manifest.json")
        if readback != manifest:
            raise _error("parallel_render_publish_failed", "rollback manifest identity changed")
        intent = self._read_publish_intent(final / _PUBLISH_INTENT_FILENAME)
        if intent != {
            "operation_id": operation_id,
            "parallel_render_id": parallel_render_id,
            "manifest_content_hash": manifest_content_hash,
        }:
            raise _error("parallel_render_publish_failed", "rollback publish intent identity changed")
        try:
            _atomic_no_replace_move(final, staging)
            self._sync(self.final_root)
            self._sync(self.staging_root)
        except OSError as error:
            raise _error("parallel_render_publish_failed", "could not rollback published final") from error

    def _write_publish_intent(
        self,
        staging: Path,
        *,
        operation_id: str,
        parallel_render_id: str,
        manifest_content_hash: str,
    ) -> None:
        payload: dict[str, object] = {
            "operation_id": operation_id,
            "parallel_render_id": parallel_render_id,
            "manifest_content_hash": manifest_content_hash,
        }
        self._write_new_file(
            staging / _PUBLISH_INTENT_FILENAME,
            canonical_json_v1(payload) + b"\n",
        )
        if self._read_publish_intent(staging / _PUBLISH_INTENT_FILENAME) != payload:
            raise _error("parallel_render_publish_failed", "publish intent strict readback changed")

    @staticmethod
    def _read_publish_intent(path: Path) -> dict[str, str]:
        MulticamParallelStore._validate_file(path)
        try:
            payload = path.read_bytes()
            data = load_closed_json(payload)
        except Exception as error:
            raise _error("parallel_render_publish_failed", "publish intent is not readable") from error
        if not isinstance(data, dict) or set(data) != _PUBLISH_INTENT_FIELDS or canonical_json_v1(data) + b"\n" != payload:
            raise _error("parallel_render_publish_failed", "publish intent is not closed")
        try:
            operation_id = validate_safe_id(data["operation_id"], field="operation_id")
            parallel_render_id = validate_safe_id(data["parallel_render_id"], field="parallel_render_id")
            manifest_content_hash = validate_sha256(
                data["manifest_content_hash"], field="manifest_content_hash"
            )
        except Exception as error:
            raise _error("parallel_render_publish_failed", "publish intent identity is invalid") from error
        return {
            "operation_id": operation_id,
            "parallel_render_id": parallel_render_id,
            "manifest_content_hash": manifest_content_hash,
        }

    def _write_new_file(self, path: Path, payload: bytes) -> None:
        if os.path.lexists(path):
            raise _error("parallel_render_staging_failed", "refused to overwrite staging file")
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(descriptor, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            self._sync(path.parent)
        except OSError as error:
            raise _error("parallel_render_staging_failed", "could not write staging file") from error

    @staticmethod
    def _ensure_directory(path: Path) -> None:
        if os.path.lexists(path):
            MulticamParallelStore._validate_directory(path)
            return
        try:
            path.mkdir()
        except OSError as error:
            raise _error("parallel_render_staging_failed", "could not create controlled render directory") from error

    @staticmethod
    def _validate_directory(path: Path) -> None:
        try:
            details = os.lstat(path)
        except OSError as error:
            raise _error("parallel_render_publish_failed", "could not inspect render directory") from error
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            raise _error("parallel_render_publish_failed", "rejected unsafe render directory")

    @staticmethod
    def _validate_file(path: Path) -> None:
        try:
            details = os.lstat(path)
        except OSError as error:
            raise _error("parallel_render_publish_failed", "could not inspect render file") from error
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise _error("parallel_render_publish_failed", "rejected unsafe render file")

    @staticmethod
    def _sync(path: Path) -> None:
        if os.name == "nt":
            return
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError as error:
            raise _error("parallel_render_publish_failed", "could not open render directory for fsync") from error
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
