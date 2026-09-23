from __future__ import annotations

import errno
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import roughcut.adapters.workflow_candidates as candidates_module
from roughcut.adapters.workflow_candidates import (
    StagedWorkflowFile,
    publish_workflow_candidate,
    workflow_candidate_temp_path,
    workflow_export_staging_id,
)
from roughcut.domain.workflow import canonical_sha256_v1

pytestmark = pytest.mark.skipif(
    sys.platform not in {"darwin", "win32"},
    reason="workflow candidate publication supports only macOS and Windows",
)


def _payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "zeta": "中文",
        "alpha": {"enabled": True, "values": [2, 1]},
    }


def _expected_json_bytes(payload: dict[str, object]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def test_workflow_export_staging_id_uses_short_canonical_hash() -> None:
    project_id = "proj_test"
    run_id = "wfr_test"
    action_id = "act_export"
    input_hash = "a" * 64
    expected_digest = canonical_sha256_v1(
        {
            "project_id": project_id,
            "run_id": run_id,
            "action_id": action_id,
            "input_hash": input_hash,
        }
    )

    staging_id = workflow_export_staging_id(
        project_id, run_id, action_id, input_hash
    )

    assert staging_id == f"stg_{expected_digest[:32]}"
    assert len(staging_id) == 36


def test_dict_candidate_preserves_json_bytes_and_moves_temp_to_single_link_final(
    tmp_path: Path,
) -> None:
    final_path = tmp_path / "artifacts" / "candidate.json"
    temp_path = workflow_candidate_temp_path(final_path, "act_dict")
    payload = _payload()

    publish_workflow_candidate(final_path, "act_dict", payload)

    details = os.lstat(final_path)
    assert final_path.read_bytes() == _expected_json_bytes(payload)
    assert stat.S_ISREG(details.st_mode)
    assert details.st_nlink == 1
    assert not temp_path.exists()


def test_staged_candidate_moves_same_inode_without_temp_or_copy(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    source = staging / "candidate.mp4"
    source.write_bytes(b"small staged fixture")
    source_inode = os.lstat(source).st_ino
    final_path = tmp_path / "renders" / "candidate.mp4"
    temp_path = workflow_candidate_temp_path(final_path, "act_staged")

    publish_workflow_candidate(
        final_path,
        "act_staged",
        StagedWorkflowFile(source),
    )

    details = os.lstat(final_path)
    assert final_path.read_bytes() == b"small staged fixture"
    assert details.st_ino == source_inode
    assert details.st_nlink == 1
    assert not source.exists()
    assert not temp_path.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows runtime behavior only")
def test_windows_runtime_staged_candidate_sync_and_publish(
    tmp_path: Path,
) -> None:
    source = tmp_path / "中文 staged file.bin"
    source.write_bytes(b"staged bytes")

    candidates_module._sync_file(source)
    final_path = tmp_path / "输出 with spaces" / "candidate.bin"
    publish_workflow_candidate(
        final_path,
        "act_windows_runtime",
        StagedWorkflowFile(source),
    )

    assert final_path.read_bytes() == b"staged bytes"
    assert not source.exists()
    assert not workflow_candidate_temp_path(final_path, "act_windows_runtime").exists()


@pytest.mark.parametrize("payload_kind", ["dict", "staged"])
def test_existing_destination_is_never_overwritten(
    tmp_path: Path,
    payload_kind: str,
) -> None:
    final_path = tmp_path / "artifacts" / "candidate.bin"
    final_path.parent.mkdir()
    final_path.write_bytes(b"existing destination")
    source = tmp_path / "staging.bin"
    source.write_bytes(b"staged source")
    payload: dict[str, object] | StagedWorkflowFile
    payload = _payload() if payload_kind == "dict" else StagedWorkflowFile(source)

    with pytest.raises(FileExistsError):
        publish_workflow_candidate(final_path, "act_exists", payload)

    assert final_path.read_bytes() == b"existing destination"
    assert source.exists()
    assert not workflow_candidate_temp_path(final_path, "act_exists").exists()


def test_destination_race_is_never_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    final_path = tmp_path / "artifacts" / "candidate.json"
    temp_path = workflow_candidate_temp_path(final_path, "act_race")
    real_move = candidates_module._atomic_no_replace_move

    def race(source: Path, destination: Path) -> None:
        destination.write_bytes(b"racing destination")
        real_move(source, destination)

    monkeypatch.setattr(candidates_module, "_atomic_no_replace_move", race)
    with pytest.raises(FileExistsError):
        publish_workflow_candidate(final_path, "act_race", _payload())

    assert final_path.read_bytes() == b"racing destination"
    assert temp_path.read_bytes() == _expected_json_bytes(_payload())
    assert os.lstat(temp_path).st_nlink == 1


@pytest.mark.parametrize("unsafe_kind", ["symlink", "hardlink"])
def test_staged_unsafe_source_is_rejected(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    evidence = tmp_path / "evidence.bin"
    evidence.write_bytes(b"evidence")
    source = tmp_path / "source.bin"
    if unsafe_kind == "symlink":
        source.symlink_to(evidence)
    else:
        os.link(evidence, source)
    final_path = tmp_path / "renders" / "candidate.mp4"

    with pytest.raises(ValueError, match="single-link regular file"):
        publish_workflow_candidate(
            final_path,
            "act_unsafe",
            StagedWorkflowFile(source),
        )

    assert evidence.read_bytes() == b"evidence"
    assert os.path.lexists(source)
    assert not final_path.exists()


def test_staged_cross_filesystem_source_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"source")
    final_path = tmp_path / "renders" / "candidate.mp4"
    real_validate = candidates_module._validate_single_link_regular

    def different_device(path: Path) -> os.stat_result:
        details = real_validate(path)
        return SimpleNamespace(  # type: ignore[return-value]
            st_mode=details.st_mode,
            st_nlink=details.st_nlink,
            st_dev=details.st_dev + 1,
        )

    monkeypatch.setattr(
        candidates_module,
        "_validate_single_link_regular",
        different_device,
    )
    with pytest.raises(OSError) as captured:
        publish_workflow_candidate(
            final_path,
            "act_cross_device",
            StagedWorkflowFile(source),
        )

    assert captured.value.errno == errno.EXDEV
    assert source.read_bytes() == b"source"
    assert not final_path.exists()


@pytest.mark.parametrize("payload_kind", ["dict", "staged"])
@pytest.mark.parametrize("failure_point", ["before", "after"])
def test_rename_failure_preserves_exact_publication_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload_kind: str,
    failure_point: str,
) -> None:
    final_path = tmp_path / "artifacts" / "candidate.bin"
    source = tmp_path / "staged.bin"
    source.write_bytes(b"staged source")
    temp_path = workflow_candidate_temp_path(final_path, "act_failure")
    payload: dict[str, object] | StagedWorkflowFile
    payload = _payload() if payload_kind == "dict" else StagedWorkflowFile(source)
    real_move = candidates_module._atomic_no_replace_move

    def fail_move(move_source: Path, destination: Path) -> None:
        if failure_point == "after":
            real_move(move_source, destination)
        raise OSError("fixture rename failure")

    monkeypatch.setattr(candidates_module, "_atomic_no_replace_move", fail_move)
    with pytest.raises(OSError, match="fixture rename failure"):
        publish_workflow_candidate(final_path, "act_failure", payload)

    publication_source = temp_path if payload_kind == "dict" else source
    if failure_point == "before":
        assert publication_source.exists()
        assert not final_path.exists()
    else:
        assert not publication_source.exists()
        assert final_path.exists()
        assert os.lstat(final_path).st_nlink == 1
    if payload_kind == "staged":
        assert not temp_path.exists()


@pytest.mark.parametrize("payload_kind", ["dict", "staged"])
def test_directory_fsync_failure_after_rename_leaves_only_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload_kind: str,
) -> None:
    final_path = tmp_path / "artifacts" / "candidate.bin"
    source = tmp_path / "staged.bin"
    source.write_bytes(b"staged source")
    temp_path = workflow_candidate_temp_path(final_path, "act_dir_sync")
    payload: dict[str, object] | StagedWorkflowFile
    payload = _payload() if payload_kind == "dict" else StagedWorkflowFile(source)
    real_sync = candidates_module._sync_directory

    def fail_after_rename(path: Path) -> None:
        real_sync(path)
        if final_path.exists():
            raise OSError("fixture directory fsync failure")

    monkeypatch.setattr(candidates_module, "_sync_directory", fail_after_rename)
    with pytest.raises(OSError, match="fixture directory fsync failure"):
        publish_workflow_candidate(final_path, "act_dir_sync", payload)

    assert final_path.exists()
    assert os.lstat(final_path).st_nlink == 1
    assert not temp_path.exists()
    if payload_kind == "staged":
        assert not source.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS primitive only")
def test_macos_actual_renamex_np_is_exclusive(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    source.write_bytes(b"source")
    destination.write_bytes(b"destination")

    assert candidates_module._MACOS_RENAME_EXCL == 0x00000004
    with pytest.raises(FileExistsError):
        candidates_module._macos_rename_exclusive(source, destination)

    assert source.read_bytes() == b"source"
    assert destination.read_bytes() == b"destination"


def test_windows_branch_uses_non_replace_rename_and_preserves_failure_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    source.write_bytes(b"source")
    calls: list[tuple[Path, Path]] = []

    def fail_rename(move_source: Path, move_destination: Path) -> None:
        calls.append((move_source, move_destination))
        raise FileExistsError(move_destination)

    monkeypatch.setattr(candidates_module.sys, "platform", "win32")
    monkeypatch.setattr(candidates_module.os, "rename", fail_rename)
    with pytest.raises(FileExistsError):
        candidates_module._atomic_no_replace_move(source, destination)

    assert calls == [(source, destination)]
    assert source.read_bytes() == b"source"
    assert not destination.exists()
