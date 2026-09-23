from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from adapters.windows_process import start_windows_helper

import roughcut.adapters.media_operation_store as store_module
from roughcut.adapters.media_operation_store import MediaOperationStore
from roughcut.domain.media_operation import (
    MediaOperationError,
    MediaOperationRecord,
)
from roughcut.domain.workflow import canonical_json_v1


def _pending(store: MediaOperationStore) -> MediaOperationRecord:
    return MediaOperationRecord(
        operation_id="op_fixture",
        scope=store.scope,
        operation_type="proxy_create",
        request_hash="a" * 64,
        input_hash="b" * 64,
        status="pending",
        phase_message_code="proxy_preparing",
        created_at="2026-07-29T08:00:00.000000Z",
        started_at=None,
        updated_at="2026-07-29T08:00:00.000000Z",
        finished_at=None,
        result_ref=None,
        error=None,
    )


def test_media_store_missing_read_is_side_effect_free_and_roundtrips(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    store = MediaOperationStore(project_root, "project_fixture")
    assert store.read("op_fixture") is None
    assert not (project_root / "workflow").exists()

    record = _pending(store)
    with store.writer(record.operation_id, create=True) as acquired:
        assert acquired is True
        assert store.write_locked(record) == record
    assert store.read(record.operation_id) == record


def test_media_store_rejects_duplicate_keys_malformed_hash_and_cross_project(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = MediaOperationStore(first_root, "project_first")
    record = _pending(first)
    with first.writer(record.operation_id, create=True) as acquired:
        assert acquired
        first.write_locked(record)
    path = first.records_root / "op_fixture.json"
    original = path.read_text(encoding="utf-8")
    path.write_text(
        original.replace(
            '"schema_version":1',
            '"schema_version":1,"schema_version":1',
        ),
        encoding="utf-8",
    )
    with pytest.raises(MediaOperationError) as duplicate:
        first.read(record.operation_id)
    assert duplicate.value.code == "operation_integrity_error"

    path.write_bytes(
        canonical_json_v1(
            {**record.to_dict(), "request_hash": "not-a-hash"}
        )
        + b"\n"
    )
    with pytest.raises(MediaOperationError):
        first.read(record.operation_id)

    second = MediaOperationStore(second_root, "project_second")
    second.records_root.mkdir(parents=True)
    (second.records_root / "op_fixture.json").write_bytes(
        canonical_json_v1(record.to_dict()) + b"\n"
    )
    with pytest.raises(MediaOperationError) as copied:
        second.read(record.operation_id)
    assert "another Project scope" in str(copied.value)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink unavailable")
def test_media_store_rejects_symlink_record_directory_and_project_root(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    records_root = project_root / "workflow" / "operations" / "media"
    records_root.mkdir(parents=True)
    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    (records_root / "op_fixture.json").symlink_to(evidence)
    store = MediaOperationStore(project_root, "project_fixture")

    with pytest.raises(MediaOperationError) as raised:
        store.read("op_fixture")
    assert raised.value.code == "operation_integrity_error"
    assert evidence.read_text(encoding="utf-8") == "{}"

    unsafe_operations = tmp_path / "unsafe"
    unsafe_operations.mkdir()
    (records_root / "op_fixture.json").unlink()
    records_root.rmdir()
    (project_root / "workflow" / "operations").rmdir()
    (project_root / "workflow" / "operations").symlink_to(
        unsafe_operations, target_is_directory=True
    )
    with pytest.raises(MediaOperationError):
        store.read("op_fixture")

    real_project = tmp_path / "real-project"
    real_project.mkdir()
    linked_project = tmp_path / "linked-project"
    linked_project.symlink_to(real_project, target_is_directory=True)
    with pytest.raises(MediaOperationError):
        MediaOperationStore(linked_project, "project_fixture").read("op_fixture")


@pytest.mark.skipif(not hasattr(os, "link"), reason="hardlink unavailable")
def test_media_store_rejects_hardlinked_record_and_writer_lock(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    store = MediaOperationStore(project_root, "project_fixture")
    record = _pending(store)
    with store.writer(record.operation_id, create=True) as acquired:
        assert acquired
        store.write_locked(record)
    record_path = store.records_root / "op_fixture.json"
    os.link(record_path, tmp_path / "record-copy.json")
    with pytest.raises(MediaOperationError):
        store.read(record.operation_id)

    record_path.unlink()
    lock_path = store.records_root / ".op_fixture.writer.lock"
    os.link(lock_path, tmp_path / "lock-copy")
    with pytest.raises(MediaOperationError), store.writer(record.operation_id, create=False):
        pass


def test_media_store_rejects_path_escape_without_source_scanning(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    store = MediaOperationStore(project_root, "project_fixture")
    with pytest.raises(MediaOperationError):
        store.read("../escape")


def test_media_store_replace_failure_preserves_previous_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    store = MediaOperationStore(project_root, "project_fixture")
    pending = _pending(store)
    with store.writer(pending.operation_id, create=True) as acquired:
        assert acquired
        store.write_locked(pending)
        running = replace(
            pending,
            status="running",
            started_at="2026-07-29T08:00:01.000000Z",
            updated_at="2026-07-29T08:00:01.000000Z",
        )
        monkeypatch.setattr(
            store_module.os,
            "replace",
            lambda _source, _destination: (_ for _ in ()).throw(
                OSError("fixture replace failure")
            ),
        )
        with pytest.raises(MediaOperationError) as raised:
            store.write_locked(running)
    assert raised.value.code == "operation_write_failed"
    assert store.read(pending.operation_id) == pending
    assert not (store.records_root / ".op_fixture.json.tmp").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows runtime behavior only")
def test_windows_runtime_media_writer_contends_releases_reacquires_and_roundtrips(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "中文 project with spaces"
    project_root.mkdir()
    store = MediaOperationStore(project_root, "project_fixture")
    holder = start_windows_helper(
        "store-hold",
        "media",
        str(project_root),
        "project_fixture",
        "op_fixture",
        "create",
    )
    try:
        assert holder.require_line() == "ACQUIRED"
        with store.writer("op_fixture", create=False) as acquired:
            assert acquired is False
        holder.release()
        holder.finish()

        record = _pending(store)
        with store.writer(record.operation_id, create=False) as acquired:
            assert acquired is True
            assert store.write_locked(record) == record
        assert store.read(record.operation_id) == record
        assert not list(store.records_root.glob("*.tmp"))
    finally:
        if holder.process.poll() is None:
            holder.terminate_and_wait()
