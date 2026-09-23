"""Atomic JSON project storage."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from roughcut.adapters.project_lock import project_write_lock
from roughcut.domain.project import Project, ProjectError


class ProjectStore:
    def __init__(self, project_path: Path) -> None:
        self.project_path = project_path.resolve()
        self.manifest_path = self.project_path / "project.json"

    def load(self) -> Project:
        try:
            data: Any = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ProjectError("project.json is missing or unreadable") from error
        if not isinstance(data, dict):
            raise ProjectError("project.json must contain an object")
        return Project.from_dict(data)

    def save(self, project: Project, *, expected_revision: int | None) -> None:
        self.project_path.mkdir(parents=True, exist_ok=True)
        with project_write_lock(self.project_path):
            if self.manifest_path.exists():
                current = self.load()
                if expected_revision is None or current.revision != expected_revision:
                    raise ProjectError("project revision conflict")
            elif expected_revision is not None:
                raise ProjectError("project revision conflict")

            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self.project_path,
                    prefix=".project.json.",
                    suffix=".tmp",
                    delete=False,
                ) as temporary_file:
                    temporary_path = Path(temporary_file.name)
                    json.dump(
                        project.to_dict(),
                        temporary_file,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    temporary_file.write("\n")
                    temporary_file.flush()
                    os.fsync(temporary_file.fileno())
                os.replace(temporary_path, self.manifest_path)
                temporary_path = None
                self._sync_directory()
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)

    def _sync_directory(self) -> None:
        try:
            descriptor = os.open(self.project_path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)
