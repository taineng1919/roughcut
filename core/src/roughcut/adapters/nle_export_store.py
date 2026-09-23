"""Strict immutable receipt store for NLE handoff exports."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path

from roughcut.adapters.workflow_candidates import StagedWorkflowFile, publish_workflow_candidate
from roughcut.domain.nle_handoff import NleExportReceipt, NleHandoffError
from roughcut.domain.workflow import canonical_json_v1, load_closed_json, validate_safe_id

NLE_HANDOFF_RECEIPTS_DIRECTORY = "exports/handoffs/receipts"


def _error(code: str, evidence: str) -> NleHandoffError:
    return NleHandoffError(code, f"Roughcut NLE handoff receipt store {evidence}")


class NleExportStore:
    """Project-contained no-replace store for NLE export receipts."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(os.path.abspath(project_root))
        self.receipts_root = self.project_root / "exports" / "handoffs" / "receipts"

    def read(self, action_id: str) -> NleExportReceipt | None:
        self._validate_action_id(action_id)
        if not os.path.lexists(self.project_root):
            raise _error("nle_export_integrity_error", "Project root is missing")
        if not os.path.lexists(self.receipts_root):
            self._validate_existing_ancestors()
            return None
        self._validate_tree()
        path = self._receipt_path(action_id)
        if not os.path.lexists(path):
            return None
        return self._read_receipt(path, action_id)

    def write(self, receipt: NleExportReceipt) -> NleExportReceipt:
        self._validate_action_id(receipt.action_id)
        self._ensure_tree()
        path = self._receipt_path(receipt.action_id)
        if os.path.lexists(path):
            existing = self._read_receipt(path, receipt.action_id)
            if existing == receipt:
                return existing
            raise _error(
                "nle_export_action_conflict",
                "refused to overwrite a different receipt for the action ID",
            )
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self.receipts_root,
                prefix=f".{receipt.action_id}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(canonical_json_v1(receipt.to_dict()))
                stream.write(b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            publish_workflow_candidate(
                path,
                receipt.action_id,
                StagedWorkflowFile(temporary),
            )
            temporary = None
        except FileExistsError:
            existing = self._read_receipt(path, receipt.action_id)
            if existing == receipt:
                return existing
            raise _error(
                "nle_export_action_conflict",
                "a concurrent writer published a different receipt",
            )
        except NleHandoffError:
            raise
        except Exception as error:
            raise _error(
                "nle_export_write_failed",
                f"receipt publication failed ({type(error).__name__})",
            ) from error
        finally:
            if temporary is not None:
                self._remove_temp(temporary)
        final = self._read_receipt(path, receipt.action_id)
        if final != receipt:
            raise _error("nle_export_integrity_error", "receipt readback changed")
        return final

    def delete_if_exact(self, receipt: NleExportReceipt) -> None:
        """Remove only the exact receipt owned by one failed publication."""

        path = self._receipt_path(receipt.action_id)
        if not os.path.lexists(path):
            return
        existing = self._read_receipt(path, receipt.action_id)
        if existing != receipt:
            raise _error(
                "nle_export_recovery_conflict",
                "refused to remove a receipt with a different identity",
            )
        self._validate_regular_file(path)
        try:
            path.unlink()
            self._sync_directory(self.receipts_root)
        except OSError as error:
            raise _error(
                "nle_export_recovery_conflict",
                "could not remove the exact failed receipt",
            ) from error
        if os.path.lexists(path):
            raise _error(
                "nle_export_recovery_conflict",
                "exact failed receipt remained after removal",
            )

    def validate_output(self, receipt: NleExportReceipt) -> None:
        path = Path(receipt.destination)
        try:
            details = os.lstat(path)
        except OSError as error:
            raise _error(
                "nle_export_integrity_error",
                "receipt output is missing",
            ) from error
        if (
            stat.S_ISLNK(details.st_mode)
            or not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
        ):
            raise _error(
                "nle_export_integrity_error",
                "receipt output is not a single-link regular file",
            )
        if details.st_size != receipt.output_bytes or self._sha256(path) != receipt.output_sha256:
            raise _error("nle_export_integrity_error", "receipt output hash or size changed")

    def _receipt_path(self, action_id: str) -> Path:
        self._validate_action_id(action_id)
        return self.receipts_root / f"{action_id}.json"

    def _read_receipt(self, path: Path, action_id: str) -> NleExportReceipt:
        self._validate_regular_file(path)
        try:
            payload = path.read_bytes()
            data = load_closed_json(payload)
            receipt = NleExportReceipt.from_dict(data)
        except NleHandoffError:
            raise
        except Exception as error:
            raise _error("nle_export_integrity_error", "receipt is unreadable") from error
        if receipt.action_id != action_id:
            raise _error("nle_export_integrity_error", "receipt ID does not match its path")
        if canonical_json_v1(receipt.to_dict()) + b"\n" != payload:
            raise _error("nle_export_integrity_error", "receipt bytes are not canonical")
        return receipt

    def _ensure_tree(self) -> None:
        self._validate_directory(self.project_root)
        current = self.project_root
        for part in ("exports", "handoffs", "receipts"):
            current = current / part
            if not os.path.lexists(current):
                try:
                    current.mkdir()
                except FileExistsError:
                    self._validate_directory(current)
                except OSError as error:
                    raise _error(
                        "nle_export_write_failed",
                        f"could not create receipt directory ({type(error).__name__})",
                    ) from error
            self._validate_directory(current)

    def _validate_existing_ancestors(self) -> None:
        self._validate_directory(self.project_root)
        current = self.project_root
        for part in ("exports", "handoffs", "receipts"):
            current = current / part
            if not os.path.lexists(current):
                return
            self._validate_directory(current)

    def _validate_tree(self) -> None:
        self._validate_directory(self.project_root)
        current = self.project_root
        for part in ("exports", "handoffs", "receipts"):
            current = current / part
            self._validate_directory(current)

    @staticmethod
    def _validate_action_id(action_id: str) -> None:
        try:
            validate_safe_id(action_id, field="NLE export action_id")
        except Exception as error:
            raise _error("nle_export_invalid_arguments", "action_id is invalid") from error

    @staticmethod
    def _validate_directory(path: Path) -> None:
        try:
            details = os.lstat(path)
        except OSError as error:
            raise _error("nle_export_integrity_error", "receipt directory is unreadable") from error
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            raise _error("nle_export_integrity_error", "receipt directory is unsafe")

    @staticmethod
    def _validate_regular_file(path: Path) -> os.stat_result:
        try:
            details = os.lstat(path)
        except OSError as error:
            raise _error("nle_export_integrity_error", "receipt file is unreadable") from error
        if (
            stat.S_ISLNK(details.st_mode)
            or not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
        ):
            raise _error("nle_export_integrity_error", "receipt file is unsafe")
        return details

    @staticmethod
    def _sync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor: int | None = None
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            os.fsync(descriptor)
        except OSError as error:
            raise _error(
                "nle_export_recovery_conflict",
                "receipt directory sync failed",
            ) from error
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _remove_temp(path: Path) -> None:
        if not os.path.lexists(path):
            return
        details = os.lstat(path)
        if (
            stat.S_ISLNK(details.st_mode)
            or not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
        ):
            raise _error("nle_export_recovery_conflict", "unsafe receipt temporary was left behind")
        try:
            path.unlink()
            NleExportStore._sync_directory(path.parent)
        except OSError as error:
            raise _error(
                "nle_export_recovery_conflict", "receipt temporary cleanup failed"
            ) from error

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
