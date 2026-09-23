from __future__ import annotations

import sys
from pathlib import Path

import pytest
from adapters.windows_process import start_windows_helper

from roughcut.adapters.project_lock import (
    project_export_claim,
    project_export_claim_is_busy,
    project_write_lock,
)
from roughcut.domain.errors import WorkflowError


def _project_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "workflow").mkdir(parents=True)
    return root


def test_export_claim_revalidates_directory_after_cross_process_mkdir_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project_root(tmp_path)
    staging = root / "workflow" / "export-staging"
    original_mkdir = Path.mkdir

    def racing_mkdir(path: Path, *args: object, **kwargs: object) -> None:
        if path == staging:
            original_mkdir(path)
            raise FileExistsError("fixture competing process created staging")
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)

    with project_export_claim(root):
        assert staging.is_dir()
        assert (staging / ".claim.lock").is_file()

    assert staging.is_dir()


@pytest.mark.parametrize("unsafe_kind", ["symlink", "file"])
def test_export_claim_rejects_unsafe_node_after_mkdir_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_kind: str,
) -> None:
    root = _project_root(tmp_path)
    staging = root / "workflow" / "export-staging"
    symlink_target = tmp_path / "unexpected-staging"
    symlink_target.mkdir()
    original_mkdir = Path.mkdir

    def racing_mkdir(path: Path, *args: object, **kwargs: object) -> None:
        if path == staging:
            if unsafe_kind == "symlink":
                path.symlink_to(symlink_target, target_is_directory=True)
            else:
                path.write_bytes(b"unexpected staging node")
            raise FileExistsError("fixture competing process created unsafe node")
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)

    with pytest.raises(WorkflowError) as captured, project_export_claim(root):
        raise AssertionError("unsafe staging must not acquire an export claim")

    assert captured.value.code == "workflow_recovery_conflict"
    if unsafe_kind == "symlink":
        assert staging.is_symlink()
        assert staging.resolve() == symlink_target
    else:
        assert staging.is_file()
        assert staging.read_bytes() == b"unexpected staging node"


@pytest.mark.parametrize(
    "failure",
    [
        PermissionError("fixture export staging permission denied"),
        OSError("fixture export staging creation failed"),
    ],
)
def test_export_claim_maps_mkdir_failure_to_workflow_lock_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: OSError,
) -> None:
    root = _project_root(tmp_path)
    staging = root / "workflow" / "export-staging"
    original_mkdir = Path.mkdir

    def failing_mkdir(path: Path, *args: object, **kwargs: object) -> None:
        if path == staging:
            raise failure
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", failing_mkdir)

    with pytest.raises(WorkflowError) as captured, project_export_claim(root):
        raise AssertionError("failed staging creation must not acquire a claim")

    assert captured.value.code == "workflow_lock_failed"
    assert "Roughcut project storage" in str(captured.value)
    assert "create export staging root failed" in str(captured.value)
    assert not staging.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows runtime behavior only")
def test_windows_runtime_project_write_lock_excludes_and_reacquires(
    tmp_path: Path,
) -> None:
    root = tmp_path / "中文 project with spaces"
    root.mkdir()
    assert root.resolve().drive
    holder = start_windows_helper("project-hold", str(root))
    contender = None
    try:
        assert holder.require_line() == "READY"
        contender = start_windows_helper("project-enter", str(root))
        assert contender.poll_line(timeout=0.5) is None
        holder.release()
        assert contender.require_line() == "ENTERED"
        contender.finish()
        holder.finish()
        with project_write_lock(root), project_write_lock(root):
            pass
    finally:
        if contender is not None and contender.process.poll() is None:
            contender.terminate_and_wait()
        if holder.process.poll() is None:
            holder.terminate_and_wait()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows runtime behavior only")
def test_windows_runtime_project_write_lock_is_released_after_process_termination(
    tmp_path: Path,
) -> None:
    root = tmp_path / "中文 project with spaces"
    root.mkdir()
    holder = start_windows_helper("project-hold", str(root))
    entrant = None
    try:
        assert holder.require_line() == "READY"
        holder.terminate_and_wait()
        entrant = start_windows_helper("project-enter", str(root))
        assert entrant.require_line() == "ENTERED"
        entrant.finish()
    finally:
        if entrant is not None and entrant.process.poll() is None:
            entrant.terminate_and_wait()
        if holder.process.poll() is None:
            holder.terminate_and_wait()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows runtime behavior only")
def test_windows_runtime_export_claim_conflicts_immediately_and_reacquires(
    tmp_path: Path,
) -> None:
    root = tmp_path / "中文 project with spaces"
    (root / "workflow").mkdir(parents=True)
    holder = start_windows_helper("export-hold", str(root))
    contender = None
    reacquirer = None
    try:
        assert holder.require_line() == "READY"
        assert project_export_claim_is_busy(root) is True
        contender = start_windows_helper("export-try", str(root))
        assert contender.require_line() == "ERROR:workflow_export_in_progress"
        contender.finish()
        holder.release()
        holder.finish()
        assert project_export_claim_is_busy(root) is False
        reacquirer = start_windows_helper("export-try", str(root))
        assert reacquirer.require_line() == "ACQUIRED"
        reacquirer.finish()
    finally:
        for process in (contender, reacquirer, holder):
            if process is not None and process.process.poll() is None:
                process.terminate_and_wait()
