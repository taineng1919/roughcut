"""Project-scoped atomic store for Draft Workspace Checkpoint schema 1."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from roughcut.adapters.artifact_store import read_json_object
from roughcut.adapters.project_lock import project_write_lock
from roughcut.adapters.project_store import ProjectStore
from roughcut.adapters.workflow_candidates import (
    publish_workflow_candidate,
    workflow_candidate_temp_path,
)
from roughcut.domain.content_draft import ContentDraft
from roughcut.domain.draft_workspace import (
    DraftWorkspaceCheckpoint,
    DraftWorkspaceCheckpointRef,
)
from roughcut.domain.errors import WorkflowError
from roughcut.domain.workflow import (
    canonical_json_v1,
    load_closed_json,
    subject_content_hash,
    validate_safe_id,
)


def _integrity_error(evidence: str) -> WorkflowError:
    return WorkflowError(
        "draft_workspace_integrity_error",
        f"Roughcut Draft workspace store rejected storage integrity: {evidence}",
    )


def _stale_error(evidence: str) -> WorkflowError:
    return WorkflowError(
        "draft_workspace_stale",
        f"Roughcut Draft workspace refused stale checkpoint state: {evidence}",
    )


def _write_error(action: str, error: BaseException) -> WorkflowError:
    return WorkflowError(
        "draft_workspace_write_failed",
        "Roughcut core/storage could not "
        f"{action}; the previous Draft workspace checkpoint remains current "
        f"({type(error).__name__})",
    )


class DraftWorkspaceStore:
    """Store one mutable checkpoint per validated WorkflowRun."""

    def __init__(self, project_path: Path) -> None:
        self.project_path = project_path.resolve()
        self.workflow_path = self.project_path / "workflow"
        self.workspace_path = self.workflow_path / "draft-workspaces"

    @contextmanager
    def write_lock(self) -> Iterator[None]:
        with project_write_lock(self.project_path):
            yield

    def read(self, run_id: str) -> DraftWorkspaceCheckpoint | None:
        validate_safe_id(run_id, field="run_id")
        with self.write_lock():
            return self.read_locked(run_id)

    def read_locked(self, run_id: str) -> DraftWorkspaceCheckpoint | None:
        validate_safe_id(run_id, field="run_id")
        path = self._checkpoint_path(run_id)
        if not os.path.lexists(self.workspace_path):
            if os.path.lexists(self.workflow_path):
                self._validate_directory(self.workflow_path)
            return None
        self._validate_workspace_tree()
        self._clean_checkpoint_temp_locked(run_id)
        if not os.path.lexists(path):
            return None
        checkpoint = self._read_checkpoint(path)
        if checkpoint.workflow_run_id != run_id:
            raise _integrity_error("checkpoint run ID does not match its path")
        project_id = ProjectStore(self.project_path).load().project_id
        if checkpoint.project_id != project_id:
            raise _integrity_error("checkpoint was copied from another Project")
        return checkpoint

    def write(
        self,
        checkpoint: DraftWorkspaceCheckpoint,
        *,
        expected_ref: DraftWorkspaceCheckpointRef | None,
    ) -> DraftWorkspaceCheckpoint:
        with self.write_lock():
            return self.write_locked(checkpoint, expected_ref=expected_ref)

    def write_locked(
        self,
        checkpoint: DraftWorkspaceCheckpoint,
        *,
        expected_ref: DraftWorkspaceCheckpointRef | None,
    ) -> DraftWorkspaceCheckpoint:
        project_id = ProjectStore(self.project_path).load().project_id
        if checkpoint.project_id != project_id:
            raise _integrity_error("checkpoint project ID does not match project.json")
        path = self._checkpoint_path(checkpoint.workflow_run_id)
        self._ensure_workspace_tree()
        self._clean_checkpoint_temp_locked(checkpoint.workflow_run_id)
        existing = self.read_locked(checkpoint.workflow_run_id)
        if existing is None:
            if expected_ref is not None or checkpoint.generation != 1:
                raise _stale_error("expected checkpoint does not exist")
        else:
            existing_ref = DraftWorkspaceCheckpointRef.for_checkpoint(existing)
            if expected_ref != existing_ref:
                raise _stale_error("checkpoint generation or hash changed")
            if checkpoint.generation != existing.generation + 1:
                raise _integrity_error("checkpoint generation must increase by exactly one")

        temp_path = self._checkpoint_temp_path(checkpoint.workflow_run_id)
        try:
            with temp_path.open("xb") as stream:
                stream.write(canonical_json_v1(checkpoint.to_dict()))
                stream.write(b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            self._sync_directory(self.workspace_path)
            readback = self._read_checkpoint(temp_path)
            if readback != checkpoint:
                raise _integrity_error("checkpoint temporary readback changed")
            os.replace(temp_path, path)
            self._sync_directory(self.workspace_path)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as error:
            try:
                if os.path.lexists(temp_path):
                    self._validate_regular_file(temp_path)
                    temp_path.unlink()
                    self._sync_directory(self.workspace_path)
            except BaseException as cleanup_error:
                raise _integrity_error(
                    "checkpoint failure cleanup could not prove temporary ownership"
                ) from cleanup_error
            raise _write_error("atomically publish Draft workspace checkpoint", error) from error
        return checkpoint

    def publish_child_locked(
        self, child: ContentDraft, *, operation_id: str
    ) -> None:
        """Atomically publish one already prepared immutable editor child."""

        validate_safe_id(operation_id, field="operation_id")
        path = self._draft_path(child.content_draft_id)
        self._validate_directory(path.parent)
        temp_path = workflow_candidate_temp_path(path, operation_id)
        if os.path.lexists(path) or os.path.lexists(temp_path):
            raise _integrity_error(
                "prepared Content Draft child path already exists before publication"
            )
        try:
            publish_workflow_candidate(path, operation_id, child.to_dict())
            stored = self._read_draft(child.content_draft_id)
            if stored != child or self._draft_hash(stored) != self._draft_hash(child):
                raise _integrity_error("published child failed exact readback")
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as error:
            if isinstance(error, WorkflowError):
                raise
            self._cleanup_failed_child_publication(
                child,
                final_path=path,
                temp_path=temp_path,
            )
            raise _write_error("atomically publish immutable Content Draft child", error) from error

    def discard_owned_child_locked(
        self,
        child: ContentDraft,
        *,
        expected_parent_id: str,
    ) -> None:
        """Delete only the exact inactive child owned by the failed call."""

        path = self._draft_path(child.content_draft_id)
        if not os.path.lexists(path):
            return
        self._validate_directory(path.parent)
        self._validate_regular_file(path)
        stored = self._read_draft(child.content_draft_id)
        project = ProjectStore(self.project_path).load()
        if (
            stored != child
            or self._draft_hash(stored) != self._draft_hash(child)
            or stored.parent_draft_id != expected_parent_id
            or stored.confirmed_by_user
            or project.active_content_draft_id == stored.content_draft_id
        ):
            raise _integrity_error(
                "Roughcut Draft workspace refused to delete a child whose identity changed"
            )
        path.unlink()
        self._sync_directory(path.parent)

    def read_draft_locked(self, artifact_id: str) -> ContentDraft:
        return self._read_draft(artifact_id)

    def _cleanup_failed_child_publication(
        self,
        child: ContentDraft,
        *,
        final_path: Path,
        temp_path: Path,
    ) -> None:
        existing = [
            path for path in (final_path, temp_path) if os.path.lexists(path)
        ]
        if not existing:
            return
        details = [os.lstat(path) for path in existing]
        if any(
            stat.S_ISLNK(item.st_mode) or not stat.S_ISREG(item.st_mode)
            for item in details
        ):
            raise _integrity_error(
                "failed child publication left an unsafe node; evidence was preserved"
            )
        if len(existing) == 2 and (
            details[0].st_dev != details[1].st_dev
            or details[0].st_ino != details[1].st_ino
            or details[0].st_nlink != 2
            or details[1].st_nlink != 2
        ):
            raise _integrity_error(
                "failed child publication nodes do not share exact ownership"
            )
        if len(existing) == 1 and details[0].st_nlink != 1:
            raise _integrity_error(
                "failed child publication node has an unexpected hard link"
            )
        try:
            payload = read_json_object(
                existing[0], description="failed Draft workspace child"
            )
            stored = ContentDraft.from_dict(payload)
        except BaseException as error:
            raise _integrity_error(
                "failed child publication content identity could not be verified"
            ) from error
        if (
            payload != stored.to_dict()
            or stored != child
            or self._draft_hash(stored) != self._draft_hash(child)
        ):
            raise _integrity_error(
                "failed child publication content identity changed; evidence was preserved"
            )
        for candidate_path in existing:
            candidate_path.unlink()
        self._sync_directory(final_path.parent)

    def _ensure_workspace_tree(self) -> None:
        self._validate_contained(self.workspace_path)
        for directory in (self.workflow_path, self.workspace_path):
            if os.path.lexists(directory):
                self._validate_directory(directory)
            else:
                try:
                    directory.mkdir()
                    self._sync_directory(directory.parent)
                except OSError as error:
                    raise _write_error(
                        f"create Draft workspace directory {directory.name}", error
                    ) from error
        self._validate_workspace_entries()

    def _validate_workspace_tree(self) -> None:
        self._validate_directory(self.workflow_path)
        self._validate_directory(self.workspace_path)
        self._validate_workspace_entries()

    def _validate_workspace_entries(self) -> None:
        try:
            entries = tuple(self.workspace_path.iterdir())
        except OSError as error:
            raise _integrity_error(
                f"cannot enumerate Draft workspace directory ({type(error).__name__})"
            ) from error
        for entry in entries:
            name = entry.name
            if name.startswith(".") and name.endswith(".json.tmp"):
                run_id = name[1:-9]
            elif name.endswith(".json") and not name.startswith("."):
                run_id = name[:-5]
            else:
                raise _integrity_error("Draft workspace directory contains an unknown node")
            try:
                validate_safe_id(run_id, field="checkpoint path run ID")
            except WorkflowError as error:
                raise _integrity_error("checkpoint path contains an unsafe run ID") from error
            self._validate_regular_file(entry)

    def _clean_checkpoint_temp_locked(self, run_id: str) -> None:
        temp_path = self._checkpoint_temp_path(run_id)
        if not os.path.lexists(temp_path):
            return
        self._validate_regular_file(temp_path)
        try:
            temp_path.unlink()
            self._sync_directory(self.workspace_path)
        except OSError as error:
            raise _integrity_error(
                f"cannot remove owned checkpoint temporary file ({type(error).__name__})"
            ) from error

    def _read_checkpoint(self, path: Path) -> DraftWorkspaceCheckpoint:
        self._validate_regular_file(path)
        try:
            payload = path.read_bytes()
            return DraftWorkspaceCheckpoint.from_dict(load_closed_json(payload))
        except WorkflowError as error:
            if error.code == "draft_workspace_integrity_error":
                raise
            raise _integrity_error("checkpoint failed strict parsing") from error
        except BaseException as error:
            raise _integrity_error(
                f"checkpoint is unreadable ({type(error).__name__})"
            ) from error

    def _read_draft(self, artifact_id: str) -> ContentDraft:
        validate_safe_id(artifact_id, field="content_draft_id")
        path = self._draft_path(artifact_id)
        self._validate_directory(path.parent)
        self._validate_regular_file(path)
        try:
            payload = read_json_object(path, description="Draft workspace Content Draft")
            draft = ContentDraft.from_dict(payload)
        except BaseException as error:
            raise _integrity_error("Content Draft failed strict parsing") from error
        if draft.content_draft_id != artifact_id or payload != draft.to_dict():
            raise _integrity_error("Content Draft identity or closed payload changed")
        return draft

    @staticmethod
    def _draft_hash(draft: ContentDraft) -> str:
        return subject_content_hash(
            "content_draft", draft.schema_version, draft.to_dict()
        )

    def _checkpoint_path(self, run_id: str) -> Path:
        path = self.workspace_path / f"{run_id}.json"
        self._validate_contained(path)
        return path

    def _checkpoint_temp_path(self, run_id: str) -> Path:
        path = self.workspace_path / f".{run_id}.json.tmp"
        self._validate_contained(path)
        return path

    def _draft_path(self, artifact_id: str) -> Path:
        path = self.project_path / "content-drafts" / f"{artifact_id}.json"
        self._validate_contained(path)
        return path

    def _validate_contained(self, path: Path) -> None:
        try:
            path.relative_to(self.project_path)
        except ValueError as error:
            raise _integrity_error("Draft workspace path escapes the Project root") from error

    def _validate_directory(self, path: Path) -> None:
        self._validate_contained(path)
        try:
            details = os.lstat(path)
        except OSError as error:
            raise _integrity_error(
                f"cannot inspect Draft workspace directory ({type(error).__name__})"
            ) from error
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            raise _integrity_error("Draft workspace path component is not a real directory")

    def _validate_regular_file(self, path: Path) -> None:
        self._validate_contained(path)
        try:
            details = os.lstat(path)
        except OSError as error:
            raise _integrity_error(
                f"cannot inspect Draft workspace file ({type(error).__name__})"
            ) from error
        if (
            stat.S_ISLNK(details.st_mode)
            or not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
        ):
            raise _integrity_error(
                "Draft workspace JSON is not a single-link regular file"
            )

    @staticmethod
    def _sync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
