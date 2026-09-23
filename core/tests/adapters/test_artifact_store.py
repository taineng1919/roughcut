from __future__ import annotations

import json
from pathlib import Path

import pytest

from roughcut.adapters import artifact_store
from roughcut.domain.project import ProjectError


def test_write_new_json_uses_exclusive_creation_and_never_overwrites(tmp_path: Path) -> None:
    path = tmp_path / "artifacts" / "immutable.json"
    artifact_store.write_new_json(path, {"value": "first"})

    with pytest.raises(ProjectError, match="already exists"):
        artifact_store.write_new_json(path, {"value": "second"})

    assert json.loads(path.read_text(encoding="utf-8")) == {"value": "first"}


def test_write_new_json_removes_a_partial_file_when_serialization_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "artifacts" / "failed.json"

    def fail_dump(payload: object, output: object, **kwargs: object) -> None:
        del payload, kwargs
        output.write('{"partial":')  # type: ignore[union-attr]
        raise OSError("injected serialization failure")

    monkeypatch.setattr(artifact_store.json, "dump", fail_dump)

    with pytest.raises(OSError, match="injected"):
        artifact_store.write_new_json(path, {"value": "never published"})

    assert not path.exists()
