from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from roughcut.adapters.component_download import ComponentDownloadError
from roughcut.adapters.component_environment import ComponentError, _parse_probe_frame
from roughcut.adapters.component_installation import (
    ArtifactVerificationError,
    ComponentInstallError,
    RuntimePublicationError,
    StaleApprovedPlanError,
)
from roughcut.application.installation_operations import (
    installation_operation_status,
    run_component_installation,
)
from roughcut.domain.installation_operation import (
    InstallationOperationError,
    InstallationResultRef,
)


def _result_ref(_result: object) -> InstallationResultRef:
    return InstallationResultRef(
        approved_plan_hash="a" * 64,
        runtime_binding_sha256="b" * 64,
        component_manifest_sha256="c" * 64,
    )


def test_installation_operation_pending_running_succeeded_and_readback(
    tmp_path: Path,
) -> None:
    phases: list[str] = []
    calls = 0

    def apply(update) -> object:  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        for phase in (
            "component_installation_downloading",
            "component_installation_installing",
            "component_installation_verifying",
            "component_installation_publishing_runtime",
        ):
            update(phase)
            phases.append(phase)
        return object()

    first = run_component_installation(
        tmp_path / "install",
        operation_id="op_success",
        approved_plan_hash="a" * 64,
        apply=apply,
        result_ref=_result_ref,
    )
    repeated = run_component_installation(
        tmp_path / "install",
        operation_id="op_success",
        approved_plan_hash="a" * 64,
        apply=apply,
        result_ref=_result_ref,
    )

    assert first.record.status == "succeeded"
    assert first.record.result_ref == _result_ref(object())
    assert phases[-1] == "component_installation_publishing_runtime"
    assert repeated.record == first.record
    assert repeated.readback is True
    assert calls == 1


def test_existing_first_readback_skips_plan_and_prerequisite_checks(
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "install"
    first = run_component_installation(
        install_root,
        operation_id="op_existing_first",
        approved_plan_hash="a" * 64,
        apply=lambda _update: object(),
        result_ref=_result_ref,
    )

    def must_not_plan() -> object:
        raise AssertionError("existing operation must not rebuild the plan")

    def must_not_apply(_update) -> object:  # type: ignore[no-untyped-def]
        raise AssertionError("existing operation must not apply again")

    writer_temp = (
        install_root
        / "operations/component-installation/.op_existing_first.json.tmp"
    )
    writer_temp.write_bytes(b"safe orphan writer temporary\n")

    repeated = run_component_installation(
        install_root,
        operation_id="op_existing_first",
        approved_plan_hash="a" * 64,
        apply=must_not_apply,
        result_ref=_result_ref,
        preflight=must_not_plan,
    )
    assert repeated.readback is True
    assert repeated.record == first.record
    assert writer_temp.read_bytes() == b"safe orphan writer temporary\n"

    with pytest.raises(InstallationOperationError) as conflict:
        run_component_installation(
            install_root,
            operation_id="op_existing_first",
            approved_plan_hash="b" * 64,
            apply=must_not_apply,
            result_ref=_result_ref,
            preflight=must_not_plan,
        )
    assert conflict.value.code == "operation_input_conflict"
    assert writer_temp.read_bytes() == b"safe orphan writer temporary\n"


def test_installation_operation_failure_has_closed_responsibility(
    tmp_path: Path,
) -> None:
    def fail(_update) -> object:  # type: ignore[no-untyped-def]
        raise ArtifactVerificationError("component artifact checksum mismatch")

    with pytest.raises(InstallationOperationError) as raised:
        run_component_installation(
            tmp_path / "install",
            operation_id="op_failed",
            approved_plan_hash="a" * 64,
            apply=fail,
            result_ref=_result_ref,
        )
    assert raised.value.code == "component_installation_failed"
    record = installation_operation_status(tmp_path / "install", "op_failed")
    assert record.status == "failed"
    assert record.error is not None
    assert record.error.responsibility == "component_artifact"
    assert "checksum" not in str(raised.value)
    assert "checksum" not in str(record.to_dict())


def test_runtime_publish_reason_is_persisted_as_bounded_operation_evidence(
    tmp_path: Path,
) -> None:
    def fail(_update) -> object:  # type: ignore[no-untyped-def]
        raise RuntimePublicationError(
            "runtime binding publication failed at /private/runtime.json",
            reason_code="runtime_publish_stale_plan",
        )

    with pytest.raises(InstallationOperationError):
        run_component_installation(
            tmp_path / "install",
            operation_id="op_runtime_reason",
            approved_plan_hash="a" * 64,
            apply=fail,
            result_ref=_result_ref,
        )
    record = installation_operation_status(tmp_path / "install", "op_runtime_reason")
    assert record.error is not None
    assert record.error.reason_code == "runtime_publish_stale_plan"
    serialized = json.dumps(record.to_dict())
    assert "/private" not in serialized
    assert "traceback" not in serialized.lower()


def test_new_operation_stale_preflight_has_no_operation_record(
    tmp_path: Path,
) -> None:
    apply_calls = 0

    def apply(_update) -> object:  # type: ignore[no-untyped-def]
        nonlocal apply_calls
        apply_calls += 1
        return object()

    def reject_preflight() -> object:
        raise StaleApprovedPlanError(
            "Roughcut bootstrap 拒绝发布 stale component plan"
        )

    with pytest.raises(StaleApprovedPlanError):
        run_component_installation(
            tmp_path / "install",
            operation_id="op_stale_preflight",
            approved_plan_hash="a" * 64,
            apply=apply,
            result_ref=_result_ref,
            preflight=reject_preflight,
        )

    with pytest.raises(InstallationOperationError) as missing:
        installation_operation_status(tmp_path / "install", "op_stale_preflight")
    assert missing.value.code == "operation_not_found"
    assert apply_calls == 0


def test_full_verification_text_does_not_change_typed_mapping(
    tmp_path: Path,
) -> None:
    def fail(_update) -> object:  # type: ignore[no-untyped-def]
        raise RuntimeError("staged full verification failed")

    with pytest.raises(InstallationOperationError):
        run_component_installation(
            tmp_path / "install",
            operation_id="op_text_mapping",
            approved_plan_hash="a" * 64,
            apply=fail,
            result_ref=_result_ref,
        )
    record = installation_operation_status(tmp_path / "install", "op_text_mapping")
    assert record.error is not None
    assert record.error.responsibility == "roughcut_component_installer"
    assert record.error.action == "install_components"


def test_network_transport_failure_maps_to_https_runtime(
    tmp_path: Path,
) -> None:
    def fail(_update) -> object:  # type: ignore[no-untyped-def]
        raise ComponentDownloadError("TLS transport failed")

    with pytest.raises(InstallationOperationError):
        run_component_installation(
            tmp_path / "install",
            operation_id="op_transport_failure",
            approved_plan_hash="a" * 64,
            apply=fail,
            result_ref=_result_ref,
        )
    record = installation_operation_status(tmp_path / "install", "op_transport_failure")
    assert record.error is not None
    assert record.error.responsibility == "python_https_runtime"
    assert record.error.action == "download_component_artifact"


def test_staged_full_verification_framing_failure_maps_to_component_installer(
    tmp_path: Path,
) -> None:
    def fail(_update) -> object:  # type: ignore[no-untyped-def]
        try:
            _parse_probe_frame(
                b"ROUGHCUT-PROBE/1 \xff\n",
                frozenset({"python_version"}),
            )
        except ComponentError as error:
            raise ComponentInstallError(
                "staged full verification framing failure"
            ) from error
        raise AssertionError("invalid probe frame unexpectedly passed")

    with pytest.raises(InstallationOperationError):
        run_component_installation(
            tmp_path / "install",
            operation_id="op_framing_failure",
            approved_plan_hash="a" * 64,
            apply=fail,
            result_ref=_result_ref,
        )
    record = installation_operation_status(tmp_path / "install", "op_framing_failure")
    assert record.error is not None
    assert record.error.responsibility == "roughcut_component_installer"
    assert record.error.action == "install_components"


def test_installation_operation_keyboard_interrupt_is_persisted(
    tmp_path: Path,
) -> None:
    def interrupt(_update) -> object:  # type: ignore[no-untyped-def]
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_component_installation(
            tmp_path / "install",
            operation_id="op_interrupted",
            approved_plan_hash="a" * 64,
            apply=interrupt,
            result_ref=_result_ref,
        )
    record = installation_operation_status(
        tmp_path / "install",
        "op_interrupted",
    )
    assert record.status == "interrupted"
    assert record.error is not None
    assert record.error.responsibility == "roughcut_bootstrap"


def test_installation_operation_live_writer_remains_running(
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "install"
    entered = threading.Event()
    release = threading.Event()
    result: list[object] = []

    def apply(_update) -> object:  # type: ignore[no-untyped-def]
        entered.set()
        assert release.wait(timeout=5)
        return object()

    thread = threading.Thread(
        target=lambda: result.append(
            run_component_installation(
                install_root,
                operation_id="op_running",
                approved_plan_hash="a" * 64,
                apply=apply,
                result_ref=_result_ref,
            )
        )
    )
    thread.start()
    assert entered.wait(timeout=5)
    running = installation_operation_status(install_root, "op_running")
    assert running.status == "running"
    second_apply_calls = 0

    def must_not_apply(_update) -> object:  # type: ignore[no-untyped-def]
        nonlocal second_apply_calls
        second_apply_calls += 1
        return object()

    duplicate = run_component_installation(
        install_root,
        operation_id="op_running",
        approved_plan_hash="a" * 64,
        apply=must_not_apply,
        result_ref=_result_ref,
    )
    assert duplicate.record.status == "running"
    assert duplicate.readback is True
    assert second_apply_calls == 0
    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(result) == 1
    assert installation_operation_status(install_root, "op_running").status == (
        "succeeded"
    )


def test_installation_operation_live_subprocess_lock_remains_running(
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "install"
    ready = tmp_path / "writer-ready"
    release = tmp_path / "writer-release"
    source_root = Path(__file__).resolve().parents[2] / "src"
    script = """
import os
import time
from pathlib import Path
from roughcut.application.installation_operations import run_component_installation
from roughcut.domain.installation_operation import InstallationResultRef
root = Path(os.environ["ROUGH_CUT_TEST_INSTALL_ROOT"])
ready = Path(os.environ["ROUGH_CUT_TEST_READY"])
release = Path(os.environ["ROUGH_CUT_TEST_RELEASE"])
def apply(update):
    update("component_installation_downloading")
    ready.write_text("ready", encoding="utf-8")
    while not release.exists():
        time.sleep(0.01)
    return object()
run_component_installation(
    root,
    operation_id="op_live_process",
    approved_plan_hash="a" * 64,
    apply=apply,
    result_ref=lambda _value: InstallationResultRef(
        approved_plan_hash="a" * 64,
        runtime_binding_sha256="b" * 64,
        component_manifest_sha256=None,
    ),
)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        env={
            **os.environ,
            "PYTHONPATH": str(source_root),
            "ROUGH_CUT_TEST_INSTALL_ROOT": str(install_root),
            "ROUGH_CUT_TEST_READY": str(ready),
            "ROUGH_CUT_TEST_RELEASE": str(release),
        },
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        record = installation_operation_status(
            install_root,
            "op_live_process",
        )
        assert record.status == "running"
        assert record.phase_message_code == "component_installation_downloading"
        duplicate = run_component_installation(
            install_root,
            operation_id="op_live_process",
            approved_plan_hash="a" * 64,
            apply=lambda _update: pytest.fail("duplicate apply must not run"),
            result_ref=_result_ref,
        )
        assert duplicate.readback is True
        assert duplicate.record == record
    finally:
        release.write_text("release", encoding="utf-8")
        process.wait(timeout=5)
    assert process.returncode == 0
    assert installation_operation_status(
        install_root,
        "op_live_process",
    ).status == "succeeded"


def test_installation_operation_hard_exit_converges_to_interrupted(
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "install"
    source_root = Path(__file__).resolve().parents[2] / "src"
    script = """
import os
from pathlib import Path
from roughcut.application.installation_operations import run_component_installation
from roughcut.domain.installation_operation import InstallationResultRef
root = Path(os.environ["ROUGH_CUT_TEST_INSTALL_ROOT"])
def apply(update):
    update("component_installation_downloading")
    os._exit(23)
run_component_installation(
    root,
    operation_id="op_hard_exit",
    approved_plan_hash="a" * 64,
    apply=apply,
    result_ref=lambda _value: InstallationResultRef(
        approved_plan_hash="a" * 64,
        runtime_binding_sha256="b" * 64,
        component_manifest_sha256=None,
    ),
)
"""
    environment = {
        **os.environ,
        "PYTHONPATH": str(source_root),
        "ROUGH_CUT_TEST_INSTALL_ROOT": str(install_root),
    }
    exited = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        env=environment,
    )
    assert exited.returncode == 23
    record = installation_operation_status(install_root, "op_hard_exit")
    assert record.status == "interrupted"
    assert record.error is not None
    assert record.error.action == "recover_abandoned_component_installation"


def test_installation_operation_same_id_different_input_conflicts_without_apply(
    tmp_path: Path,
) -> None:
    calls = 0

    def apply(_update) -> object:  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return object()

    run_component_installation(
        tmp_path / "install",
        operation_id="op_conflict",
        approved_plan_hash="a" * 64,
        apply=apply,
        result_ref=_result_ref,
    )
    with pytest.raises(InstallationOperationError) as raised:
        run_component_installation(
            tmp_path / "install",
            operation_id="op_conflict",
            approved_plan_hash="d" * 64,
            apply=apply,
            result_ref=_result_ref,
        )
    assert raised.value.code == "operation_input_conflict"
    assert calls == 1


def test_installation_operation_status_missing_is_pure_read(
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "install"
    with pytest.raises(InstallationOperationError) as raised:
        installation_operation_status(install_root, "op_missing")
    assert raised.value.code == "operation_not_found"
    assert not install_root.exists()
