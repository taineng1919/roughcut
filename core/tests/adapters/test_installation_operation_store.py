from __future__ import annotations

import inspect
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from adapters.windows_process import start_windows_helper

import roughcut.adapters.installation_operation_store as store_module
from roughcut.adapters.installation_operation_store import (
    InstallationOperationStore,
)
from roughcut.domain.installation_operation import (
    InstallationOperationError,
    InstallationOperationRecord,
    InstallationScope,
    installation_input_hash,
)
from roughcut.domain.workflow import canonical_json_v1


def _pending(store: InstallationOperationStore) -> InstallationOperationRecord:
    return InstallationOperationRecord(
        operation_id="op_fixture",
        scope=InstallationScope(store.scope_hash),
        input_hash=installation_input_hash("a" * 64),
        status="pending",
        phase_message_code="component_installation_preparing",
        created_at="2026-07-29T08:00:00.000000Z",
        started_at=None,
        updated_at="2026-07-29T08:00:00.000000Z",
        finished_at=None,
        result_ref=None,
        error=None,
    )


def test_installation_store_missing_read_is_side_effect_free_and_roundtrips(
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "install"
    store = InstallationOperationStore(install_root)
    assert store.read("op_fixture") is None
    assert not install_root.exists()

    record = _pending(store)
    with store.writer(record.operation_id, create=True) as acquired:
        assert acquired is True
        assert store.write_locked(record) == record
    assert store.read(record.operation_id) == record


def test_installation_store_rejects_duplicate_keys_unknown_schema_and_changed_scope(
    tmp_path: Path,
) -> None:
    store = InstallationOperationStore(tmp_path / "install")
    record = _pending(store)
    with store.writer(record.operation_id, create=True) as acquired:
        assert acquired
        store.write_locked(record)
    path = store.records_root / "op_fixture.json"

    path.write_text(
        canonical_json_v1(record.to_dict()).decode().replace(
            '"schema_version":1',
            '"schema_version":1,"schema_version":1',
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(InstallationOperationError) as duplicate:
        store.read(record.operation_id)
    assert duplicate.value.code == "operation_integrity_error"

    payload = record.to_dict()
    payload["schema_version"] = 2
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(InstallationOperationError):
        store.read(record.operation_id)

    copied = replace(
        record,
        scope=InstallationScope("f" * 64),
    )
    path.write_bytes(canonical_json_v1(copied.to_dict()) + b"\n")
    with pytest.raises(InstallationOperationError) as changed:
        store.read(record.operation_id)
    assert "another installation scope" in str(changed.value)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink unavailable")
def test_installation_store_rejects_symlink_record_and_directory(
    tmp_path: Path,
) -> None:
    store = InstallationOperationStore(tmp_path / "install")
    store.records_root.mkdir(parents=True)
    evidence = tmp_path / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    (store.records_root / "op_fixture.json").symlink_to(evidence)

    with pytest.raises(InstallationOperationError) as raised:
        store.read("op_fixture")
    assert raised.value.code == "operation_integrity_error"
    assert evidence.read_text(encoding="utf-8") == "{}"

    other_root = tmp_path / "other"
    other_root.mkdir()
    symlink_install = tmp_path / "symlink-install"
    symlink_install.symlink_to(other_root, target_is_directory=True)
    with pytest.raises(InstallationOperationError):
        InstallationOperationStore(symlink_install).read("op_fixture")


@pytest.mark.skipif(not hasattr(os, "link"), reason="hardlink unavailable")
def test_installation_store_rejects_hardlinked_record_and_writer_lock(
    tmp_path: Path,
) -> None:
    store = InstallationOperationStore(tmp_path / "install")
    record = _pending(store)
    with store.writer(record.operation_id, create=True) as acquired:
        assert acquired
        store.write_locked(record)
    record_path = store.records_root / "op_fixture.json"
    os.link(record_path, tmp_path / "record-copy.json")
    with pytest.raises(InstallationOperationError):
        store.read(record.operation_id)

    record_path.unlink()
    lock_path = store.records_root / ".op_fixture.writer.lock"
    os.link(lock_path, tmp_path / "lock-copy")
    with pytest.raises(InstallationOperationError), store.writer(
        record.operation_id, create=False
    ):
        pass


def test_installation_store_rejects_path_escape(tmp_path: Path) -> None:
    store = InstallationOperationStore(tmp_path / "install")
    with pytest.raises(InstallationOperationError):
        store.read("../escape")


def test_installation_store_windows_lock_branch_has_static_fixture_coverage_only() -> None:
    acquire_source = inspect.getsource(
        InstallationOperationStore._try_acquire_file_lock
    )
    release_source = inspect.getsource(
        InstallationOperationStore._release_file_lock
    )
    assert 'importlib.import_module("msvcrt")' in acquire_source
    assert "LK_NBLCK" in acquire_source
    assert "LK_UNLCK" in release_source


def test_installation_store_replace_failure_preserves_previous_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InstallationOperationStore(tmp_path / "install")
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
        with pytest.raises(InstallationOperationError) as raised:
            store.write_locked(running)
    assert raised.value.code == "operation_write_failed"
    assert store.read(pending.operation_id) == pending
    assert not (store.records_root / ".op_fixture.json.tmp").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows runtime behavior only")
def test_windows_runtime_installation_writer_contends_releases_reacquires_and_roundtrips(
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "中文 install with spaces"
    store = InstallationOperationStore(install_root)
    holder = start_windows_helper(
        "store-hold",
        "installation",
        str(install_root),
        "unused-project-id",
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
