"""Small atomic JSON helpers for immutable project artifacts."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from roughcut.domain.project import ProjectError


def write_new_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with path.open("x", encoding="utf-8") as artifact_file:
            created = True
            json.dump(payload, artifact_file, ensure_ascii=False, indent=2, sort_keys=True)
            artifact_file.write("\n")
            artifact_file.flush()
            os.fsync(artifact_file.fileno())
    except FileExistsError as error:
        raise ProjectError("immutable artifact already exists") from error
    except Exception:
        if created:
            path.unlink(missing_ok=True)
        raise


def read_json_object(path: Path, *, description: str) -> dict[str, Any]:
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProjectError(f"{description} is missing or unreadable") from error
    if not isinstance(data, dict):
        raise ProjectError(f"{description} must contain an object")
    return data
