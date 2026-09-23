from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path, PureWindowsPath

import pytest

from roughcut.adapters import ffprobe as ffprobe_adapter
from roughcut.adapters import runtime_binding
from roughcut.adapters.ffmpeg.audio import decode_audio_to_pcm
from roughcut.adapters.ffmpeg.render import resolve_render_tools
from roughcut.adapters.ffprobe import probe_media
from roughcut.adapters.funasr.runner import configured_funasr_config
from roughcut.adapters.runtime_binding import (
    AUDALIGN_PROVIDER,
    AUDALIGN_UPSTREAM_COMMIT,
    BBC_AUDIO_OFFSET_FINDER_PROVIDER,
    RuntimeAlignmentPython,
    RuntimeBinding,
    RuntimeBindingError,
    RuntimeComponent,
    RuntimePlanEvidence,
    RuntimePython,
    RuntimeTool,
    alignment_binding_supports_provider,
    default_install_root,
    load_runtime_binding,
    publish_runtime_binding,
    resolve_funasr_selection,
    resolve_runtime_tool,
    runtime_binding_path,
    runtime_binding_status,
)
from roughcut.application import media_operations
from roughcut.application.diagnostics import diagnostics
from roughcut.domain.time import TICKS_PER_SECOND

CORE_ROOT = Path(__file__).resolve().parents[2]


def _executable(path: Path, version: str) -> Path:
    if sys.platform == "win32":
        path = path.with_suffix(".cmd")
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        path.write_text(
            f"@echo off\necho {version}\nexit /b 0\n",
            encoding="utf-8",
        )
    else:
        path.write_text(f"#!/bin/sh\necho '{version}'\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _binding(tmp_path: Path, *, source_type: str = "managed") -> RuntimeBinding:
    install_root = tmp_path / "用户 配置" / "Roughcut"
    managed_root = tmp_path / "组件 数据" / "Managed"
    python = _executable(managed_root / "venv/bin/python", "Python 3.11.9")
    ffmpeg = _executable(tmp_path / "外部 工具/ffmpeg", "ffmpeg version fixture")
    ffprobe = _executable(tmp_path / "外部 工具/ffprobe", "ffprobe version fixture")
    external_manifest = tmp_path / "外部 模型/external manifest.json"
    external_manifest.parent.mkdir(parents=True, exist_ok=True)
    external_manifest.write_text("{}\n", encoding="utf-8")
    component_paths: dict[str, Path] = {}
    components: dict[str, RuntimeComponent] = {}
    for key, name, payload, digest_character in (
        ("asr", "model_asr", "model.pt", "a"),
        ("vad", "model_vad", "model.pt", "b"),
        ("punc", "model_punc", "model.pt", "c"),
        ("campp", "model_spk", "campplus_cn_common.bin", "d"),
    ):
        path = managed_root / "models" / name
        path.mkdir(parents=True, exist_ok=True)
        (path / payload).write_bytes(name.encode())
        component_paths[key] = path
        components[key] = RuntimeComponent(
            component=name,
            source_type=source_type,
            ownership=(
                "roughcut_managed"
                if source_type == "managed"
                else "external_read_only"
            ),
            path=str(path),
            version=f"{name}-fixture",
            origin=f"fixture://{name}",
            license="fixture-only",
            receipt={"algorithm": "sha256", "value": digest_character * 64},
        )
    return RuntimeBinding(
        install_root=str(install_root),
        platform=runtime_binding.current_platform(),
        architecture=runtime_binding.current_architecture(),
        profile="fixture-profile",
        verification_mode="full",
        python=RuntimePython(
            source_type=source_type,
            ownership=(
                "roughcut_managed"
                if source_type == "managed"
                else "external_read_only"
            ),
            interpreter=str(python),
            versions={"funasr": "1.3.8", "torch": "2.6.0", "torchaudio": "2.6.0"},
            receipt=(
                {
                    "interpreter": str(python),
                    "python_version": "3.11",
                    "funasr": "1.3.8",
                    "torch": "2.6.0",
                    "torchaudio": "2.6.0",
                    "cuda_version": None,
                    "cuda_available": False,
                }
                if source_type == "external"
                else {
                    "root": "venv",
                    "interpreter": "venv/bin/python",
                    "dependency_lock": "locks/fixture.lock",
                    "lock_origin": "fixture://lock",
                    "lock_verification": {
                        "algorithm": "sha256",
                        "value": "f" * 64,
                    },
                    "device": "cpu",
                    "python_version": "3.11",
                }
            ),
        ),
        components=components,
        ffmpeg=RuntimeTool(
            command=str(ffmpeg),
            version="ffmpeg version fixture",
        ),
        ffprobe=RuntimeTool(
            command=str(ffprobe),
            version="ffprobe version fixture",
        ),
        evidence=RuntimePlanEvidence(
            plan_hash="a" * 64,
            catalog_version="fixture-1",
            catalog_hash="b" * 64,
            managed_root=str(managed_root),
            managed_manifest_sha256=(
                "c" * 64 if source_type == "managed" else None
            ),
            external_manifest_path=(
                str(external_manifest) if source_type == "external" else None
            ),
            external_manifest_sha256=(
                "e" * 64 if source_type == "external" else None
            ),
        ),
        schema_version=1,
    )


def test_schema_one_roundtrip_supports_unicode_and_spaces(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    path = Path(binding.install_root) / "runtime.json"

    result = publish_runtime_binding(path, binding)

    assert result.published is True
    assert result.reused is False
    assert load_runtime_binding(path) == binding
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 1


@pytest.mark.skipif(sys.platform != "win32", reason="Windows runtime behavior only")
def test_windows_runtime_binding_publish_load_reuse_and_temp_cleanup(
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "中文 install with spaces"
    external_manifest = tmp_path / "中文 model data" / "external manifest.json"
    external_manifest.parent.mkdir(parents=True)
    external_manifest.write_text("{}\n", encoding="utf-8")
    components: dict[str, RuntimeComponent] = {}
    for key, name, payload, digest_character in (
        ("asr", "model_asr", "model.pt", "a"),
        ("vad", "model_vad", "model.pt", "b"),
        ("punc", "model_punc", "model.pt", "c"),
        ("campp", "model_spk", "campplus_cn_common.bin", "d"),
    ):
        component_path = tmp_path / "中文 model data" / name
        component_path.mkdir(parents=True)
        (component_path / payload).write_bytes(name.encode())
        components[key] = RuntimeComponent(
            component=name,
            source_type="external",
            ownership="external_read_only",
            path=str(component_path),
            version=f"{name}-fixture",
            origin=f"fixture://{name}",
            license="fixture-only",
            receipt={"algorithm": "sha256", "value": digest_character * 64},
        )
    binding = RuntimeBinding(
        install_root=str(install_root),
        platform="windows",
        architecture=runtime_binding.current_architecture(),
        profile="windows-fixture",
        verification_mode="full",
        python=RuntimePython(
            source_type="external",
            ownership="external_read_only",
            interpreter=sys.executable,
            versions={
                "funasr": "1.3.8",
                "torch": "2.6.0",
                "torchaudio": "2.6.0",
            },
            receipt={
                "interpreter": sys.executable,
                "python_version": "3.11",
                "funasr": "1.3.8",
                "torch": "2.6.0",
                "torchaudio": "2.6.0",
                "cuda_version": None,
                "cuda_available": False,
            },
        ),
        components=components,
        ffmpeg=RuntimeTool(sys.executable, "python fixture"),
        ffprobe=RuntimeTool(sys.executable, "python fixture"),
        evidence=RuntimePlanEvidence(
            plan_hash="a" * 64,
            catalog_version="windows-fixture",
            catalog_hash="b" * 64,
            managed_root=str(install_root / "managed"),
            managed_manifest_sha256=None,
            external_manifest_path=str(external_manifest),
            external_manifest_sha256="e" * 64,
        ),
        schema_version=1,
    )
    path = install_root / "runtime.json"

    first = publish_runtime_binding(path, binding)
    original = path.read_bytes()
    assert first.published is True
    assert load_runtime_binding(path) == binding

    second = publish_runtime_binding(path, binding)
    assert second.published is False
    assert second.reused is True
    assert path.read_bytes() == original

    changed = replace(binding, profile="windows-fixture-updated")
    updated = publish_runtime_binding(path, changed)
    assert updated.published is True
    assert updated.reused is False
    assert load_runtime_binding(path) == changed
    assert not list(install_root.glob(".runtime.json.*.tmp"))


def test_runtime_status_hashes_valid_and_invalid_regular_files(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    path = Path(binding.install_root) / "runtime.json"
    publish_runtime_binding(path, binding)
    valid_payload = path.read_bytes()

    assert runtime_binding_status(path) == {
        "path": str(path),
        "configured": True,
        "status": "configured",
        "sha256": hashlib.sha256(valid_payload).hexdigest(),
    }

    invalid_payload = b'{"schema_version": 99}\n'
    path.write_bytes(invalid_payload)
    invalid = runtime_binding_status(path)
    assert invalid["configured"] is False
    assert invalid["status"] == "invalid"
    assert invalid["sha256"] == hashlib.sha256(invalid_payload).hexdigest()


def test_stale_plan_cas_exposes_a_bounded_reason_without_changing_binding(
    tmp_path: Path,
) -> None:
    binding = _binding(tmp_path)
    path = Path(binding.install_root) / "runtime.json"
    publish_runtime_binding(path, binding)
    expected = runtime_binding_status(path)
    concurrent = replace(binding, profile="concurrent")
    publish_runtime_binding(path, concurrent)

    with pytest.raises(RuntimeBindingError) as raised:
        publish_runtime_binding(
            path,
            replace(binding, profile="stale-plan"),
            expected_previous_state=expected,
        )

    assert getattr(raised.value, "reason_code", None) == "runtime_publish_stale_plan"
    assert load_runtime_binding(path) == concurrent


def test_existing_invalid_binding_exposes_a_bounded_reason(
    tmp_path: Path,
) -> None:
    binding = _binding(tmp_path)
    path = Path(binding.install_root) / "runtime.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"schema_version":99}\n')

    with pytest.raises(RuntimeBindingError) as raised:
        publish_runtime_binding(path, binding)

    assert getattr(raised.value, "reason_code", None) == (
        "runtime_publish_existing_binding_invalid"
    )


def test_candidate_binding_validation_exposes_a_bounded_reason(
    tmp_path: Path,
) -> None:
    binding = replace(_binding(tmp_path), verification_mode="quick")
    path = Path(binding.install_root) / "runtime.json"

    with pytest.raises(RuntimeBindingError) as raised:
        publish_runtime_binding(path, binding)

    assert raised.value.reason_code == "runtime_publish_binding_validation_failed"


def test_default_and_launcher_derived_runtime_paths_cover_posix_and_windows(
    tmp_path: Path,
) -> None:
    assert default_install_root(home=Path("/Users/示例 用户"), platform="macos") == Path(
        "/Users/示例 用户/.roughcut"
    )
    assert runtime_binding_path(
        executable=Path("/opt/Roughcut 自定义/venv/bin/roughcut"),
        platform="macos",
    ) == Path("/opt/Roughcut 自定义/runtime.json")
    assert runtime_binding_path(
        executable=PureWindowsPath(
            r"D:\用户 数据\Roughcut 自定义\venv\Scripts\roughcut.exe"
        ),
        home=PureWindowsPath(r"C:\Users\示例 用户"),
        platform="windows",
    ) == PureWindowsPath(r"D:\用户 数据\Roughcut 自定义\runtime.json")


def test_schema_roundtrip_preserves_windows_drive_unicode_and_spaces(
    tmp_path: Path,
) -> None:
    original = _binding(tmp_path, source_type="external")
    windows_components = {
        key: replace(
            component,
            path=rf"D:\模型 数据\{component.component}",
        )
        for key, component in original.components.items()
    }
    binding = replace(
        original,
        install_root=r"C:\Users\示例 用户\.roughcut",
        platform="windows",
        architecture="x86_64",
        python=replace(
            original.python,
            interpreter=r"D:\共享 Runtime\venv\Scripts\python.exe",
            receipt={
                **original.python.receipt,
                "interpreter": r"D:\共享 Runtime\venv\Scripts\python.exe",
            },
        ),
        components=windows_components,
        ffmpeg=replace(original.ffmpeg, command=r"C:\Program Files\FFmpeg\ffmpeg.exe"),
        ffprobe=replace(
            original.ffprobe, command=r"C:\Program Files\FFmpeg\ffprobe.exe"
        ),
        evidence=replace(
            original.evidence,
            managed_root=r"C:\Users\示例 用户\.roughcut\components",
            external_manifest_path=r"D:\模型 数据\external manifest.json",
            external_manifest_sha256="e" * 64,
        ),
    )

    roundtrip = RuntimeBinding.from_dict(binding.to_dict())
    roundtrip.validate(validate_target=False, validate_filesystem=False)

    assert roundtrip == binding


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"schema_version": 99}, "schema"),
        ([], "JSON object"),
    ],
)
def test_load_rejects_unknown_or_non_object_schema(
    tmp_path: Path, payload: object, message: str
) -> None:
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeBindingError, match=message):
        load_runtime_binding(path, validate_target=False, validate_filesystem=False)


def test_load_rejects_corrupt_json(tmp_path: Path) -> None:
    path = tmp_path / "runtime.json"
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(RuntimeBindingError, match="损坏"):
        load_runtime_binding(path, validate_target=False, validate_filesystem=False)


def test_binding_rejects_managed_escape_and_target_mismatch(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    escaped = replace(
        binding,
        components={
            **binding.components,
            "asr": replace(
                binding.components["asr"],
                path=str(tmp_path / "outside/model_asr"),
            ),
        },
    )
    with pytest.raises(RuntimeBindingError, match="escapes"):
        escaped.validate(validate_target=False, validate_filesystem=False)

    wrong_target = replace(binding, architecture=f"not-{binding.architecture}")
    with pytest.raises(RuntimeBindingError, match="architecture"):
        wrong_target.validate()


def test_binding_rejects_incomplete_component_selection(tmp_path: Path) -> None:
    binding = _binding(tmp_path)

    with pytest.raises(RuntimeBindingError, match="incomplete"):
        replace(
            binding,
            components={
                key: value
                for key, value in binding.components.items()
                if key != "campp"
            },
        ).validate()


def test_binding_rejects_runtime_and_component_symlinks(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    path = Path(binding.install_root) / "runtime.json"
    publish_runtime_binding(path, binding)
    linked = tmp_path / "runtime-link.json"
    try:
        linked.symlink_to(path)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable")

    with pytest.raises(RuntimeBindingError, match="symbolic link"):
        load_runtime_binding(linked)

    component = Path(binding.components["asr"].path)
    target = component.with_name("real-asr")
    component.rename(target)
    component.symlink_to(target, target_is_directory=True)
    with pytest.raises(RuntimeBindingError, match="symbolic link"):
        load_runtime_binding(path)


def test_publish_rejects_symlink_install_root(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    real_root = Path(binding.install_root)
    real_root.mkdir(parents=True)
    linked_root = tmp_path / "linked install"
    try:
        linked_root.symlink_to(real_root, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable")
    linked_binding = replace(binding, install_root=str(linked_root))

    with pytest.raises(RuntimeBindingError, match="symbolic link"):
        publish_runtime_binding(linked_root / "runtime.json", linked_binding)

    assert not (real_root / "runtime.json").exists()


def test_atomic_publish_failure_preserves_previous_binding_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = _binding(tmp_path)
    path = Path(binding.install_root) / "runtime.json"
    publish_runtime_binding(path, binding)
    original = path.read_bytes()
    changed = replace(binding, profile="changed-profile")
    real_replace = os.replace

    def fail_runtime_replace(source: object, destination: object) -> None:
        if Path(destination) == path:
            raise OSError("injected publish failure")
        real_replace(source, destination)

    monkeypatch.setattr(runtime_binding.os, "replace", fail_runtime_replace)
    with pytest.raises(RuntimeBindingError, match="原子发布失败") as raised:
        publish_runtime_binding(path, changed)

    assert raised.value.reason_code == "runtime_publish_atomic_replace_failed"
    assert path.read_bytes() == original
    assert not list(path.parent.glob(".runtime.json.*.tmp"))


@pytest.mark.parametrize(
    "invalid_payload",
    [
        b"{broken json\n",
        b'{"schema_version": 99}\n',
    ],
)
def test_publisher_refuses_to_overwrite_invalid_existing_binding(
    tmp_path: Path, invalid_payload: bytes
) -> None:
    binding = _binding(tmp_path)
    path = Path(binding.install_root) / "runtime.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(invalid_payload)

    with pytest.raises(
        RuntimeBindingError,
        match="Roughcut runtime binding publisher 拒绝覆盖无效既有绑定",
    ):
        publish_runtime_binding(path, binding)

    assert path.read_bytes() == invalid_payload
    assert not list(path.parent.glob(".runtime.json.*.tmp"))


def test_identical_binding_publish_remains_idempotent(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    path = Path(binding.install_root) / "runtime.json"
    first = publish_runtime_binding(path, binding)
    original = path.read_bytes()

    second = publish_runtime_binding(path, binding)

    assert first.published is True
    assert second.published is False
    assert second.reused is True
    assert path.read_bytes() == original
    assert not list(path.parent.glob(".runtime.json.*.tmp"))


@pytest.mark.parametrize("external_payload", [b"", b"external lock bytes"])
def test_publish_rejects_lock_symlink_without_touching_external_target(
    tmp_path: Path,
    external_payload: bytes,
) -> None:
    binding = _binding(tmp_path)
    install_root = Path(binding.install_root)
    install_root.mkdir(parents=True)
    external = tmp_path / "outside lock"
    external.write_bytes(external_payload)
    lock_path = install_root / ".runtime.json.lock"
    try:
        lock_path.symlink_to(external)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable")

    with pytest.raises(RuntimeBindingError, match="拒绝无效发布锁"):
        publish_runtime_binding(install_root / "runtime.json", binding)

    assert external.read_bytes() == external_payload
    assert not (install_root / "runtime.json").exists()
    assert not list(install_root.glob(".runtime.json.*.tmp"))


def test_publish_rejects_lock_hardlink_without_touching_external_target(
    tmp_path: Path,
) -> None:
    binding = _binding(tmp_path)
    install_root = Path(binding.install_root)
    install_root.mkdir(parents=True)
    external = tmp_path / "outside hardlink"
    external_payload = b"external hardlink bytes"
    external.write_bytes(external_payload)
    lock_path = install_root / ".runtime.json.lock"
    try:
        os.link(external, lock_path)
    except (OSError, NotImplementedError):
        pytest.skip("hardlinks are unavailable")

    with pytest.raises(RuntimeBindingError, match="拒绝无效发布锁"):
        publish_runtime_binding(install_root / "runtime.json", binding)

    assert external.read_bytes() == external_payload
    assert not (install_root / "runtime.json").exists()
    assert not list(install_root.glob(".runtime.json.*.tmp"))


def test_publish_rejects_lock_directory(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    install_root = Path(binding.install_root)
    lock_path = install_root / ".runtime.json.lock"
    lock_path.mkdir(parents=True)

    with pytest.raises(RuntimeBindingError, match="拒绝无效发布锁"):
        publish_runtime_binding(install_root / "runtime.json", binding)

    assert lock_path.is_dir()
    assert not (install_root / "runtime.json").exists()
    assert not list(install_root.glob(".runtime.json.*.tmp"))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is unavailable")
def test_publish_rejects_non_regular_lock_target(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    install_root = Path(binding.install_root)
    install_root.mkdir(parents=True)
    lock_path = install_root / ".runtime.json.lock"
    os.mkfifo(lock_path)

    with pytest.raises(RuntimeBindingError, match="拒绝无效发布锁"):
        publish_runtime_binding(install_root / "runtime.json", binding)

    assert stat.S_ISFIFO(lock_path.lstat().st_mode)
    assert not (install_root / "runtime.json").exists()
    assert not list(install_root.glob(".runtime.json.*.tmp"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX lock branch only")
def test_posix_lock_file_is_regular_and_reusable(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    path = Path(binding.install_root) / "runtime.json"

    first = publish_runtime_binding(path, binding)
    second = publish_runtime_binding(path, binding)
    lock_stat = (path.parent / ".runtime.json.lock").lstat()

    assert first.published is True
    assert second.reused is True
    assert stat.S_ISREG(lock_stat.st_mode)
    assert lock_stat.st_nlink == 1


def test_windows_branch_validates_hardlink_before_lock_byte_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_root = tmp_path / "install"
    install_root.mkdir()
    external = tmp_path / "outside empty lock"
    external.write_bytes(b"")
    lock_path = install_root / ".runtime.json.lock"
    try:
        os.link(external, lock_path)
    except (OSError, NotImplementedError):
        pytest.skip("hardlinks are unavailable")
    acquire_calls: list[object] = []

    monkeypatch.setattr(
        runtime_binding,
        "_runtime_lock_uses_windows",
        lambda: True,
    )
    monkeypatch.setattr(
        runtime_binding,
        "_acquire_runtime_file_lock",
        lambda lock_file: acquire_calls.append(lock_file),
    )
    monkeypatch.setattr(
        runtime_binding,
        "_release_runtime_file_lock",
        lambda _lock_file: None,
    )

    with pytest.raises(RuntimeBindingError, match="拒绝无效发布锁"), runtime_binding._runtime_publish_lock(install_root):
        raise AssertionError("invalid Windows lock reached publisher")

    assert acquire_calls == []
    assert external.read_bytes() == b""


def test_concurrent_publisher_wins_before_old_unconfigured_plan(
    tmp_path: Path,
) -> None:
    binding = _binding(tmp_path)
    path = Path(binding.install_root) / "runtime.json"
    expected = runtime_binding_status(path)
    old_plan_binding = replace(binding, profile="old-plan")
    concurrent_binding = replace(binding, profile="concurrent")
    barrier = threading.Barrier(2)
    concurrent_done = threading.Event()

    def publish_concurrent() -> bytes:
        barrier.wait()
        publish_runtime_binding(path, concurrent_binding)
        payload = path.read_bytes()
        concurrent_done.set()
        return payload

    def publish_old_plan() -> None:
        barrier.wait()
        assert concurrent_done.wait(timeout=5)
        with pytest.raises(
            RuntimeBindingError,
            match="Roughcut bootstrap 拒绝发布 stale component plan",
        ):
            publish_runtime_binding(
                path,
                old_plan_binding,
                expected_previous_state=expected,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        concurrent = executor.submit(publish_concurrent)
        old_plan = executor.submit(publish_old_plan)
        concurrent_payload = concurrent.result(timeout=10)
        old_plan.result(timeout=10)

    assert path.read_bytes() == concurrent_payload
    assert not list(path.parent.glob(".runtime.json.*.tmp"))


def test_concurrent_publisher_replacement_makes_configured_plan_stale(
    tmp_path: Path,
) -> None:
    binding = _binding(tmp_path)
    path = Path(binding.install_root) / "runtime.json"
    publish_runtime_binding(path, binding)
    expected = runtime_binding_status(path)
    old_plan_binding = replace(binding, profile="old-plan")
    concurrent_binding = replace(binding, profile="concurrent")
    barrier = threading.Barrier(2)
    concurrent_done = threading.Event()

    def publish_concurrent() -> bytes:
        barrier.wait()
        publish_runtime_binding(path, concurrent_binding)
        payload = path.read_bytes()
        concurrent_done.set()
        return payload

    def publish_old_plan() -> None:
        barrier.wait()
        assert concurrent_done.wait(timeout=5)
        with pytest.raises(
            RuntimeBindingError,
            match="Roughcut bootstrap 拒绝发布 stale component plan",
        ):
            publish_runtime_binding(
                path,
                old_plan_binding,
                expected_previous_state=expected,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        concurrent = executor.submit(publish_concurrent)
        old_plan = executor.submit(publish_old_plan)
        concurrent_payload = concurrent.result(timeout=10)
        old_plan.result(timeout=10)

    assert path.read_bytes() == concurrent_payload
    assert not list(path.parent.glob(".runtime.json.*.tmp"))


def test_concurrent_delete_makes_configured_plan_stale(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    path = Path(binding.install_root) / "runtime.json"
    publish_runtime_binding(path, binding)
    expected = runtime_binding_status(path)
    barrier = threading.Barrier(2)
    delete_done = threading.Event()

    def delete_concurrent() -> None:
        barrier.wait()
        path.unlink()
        delete_done.set()

    def publish_old_plan() -> None:
        barrier.wait()
        assert delete_done.wait(timeout=5)
        with pytest.raises(
            RuntimeBindingError,
            match="Roughcut bootstrap 拒绝发布 stale component plan",
        ):
            publish_runtime_binding(
                path,
                replace(binding, profile="old-plan"),
                expected_previous_state=expected,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        deleted = executor.submit(delete_concurrent)
        old_plan = executor.submit(publish_old_plan)
        deleted.result(timeout=10)
        old_plan.result(timeout=10)

    assert not path.exists()
    assert not list(path.parent.glob(".runtime.json.*.tmp"))


@pytest.mark.parametrize(
    "failure",
    ["write", "replace", "keyboard_interrupt", "system_exit"],
)
def test_publish_lock_releases_and_cleans_temp_after_locked_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    binding = _binding(tmp_path)
    path = Path(binding.install_root) / "runtime.json"
    publish_runtime_binding(path, binding)
    expected = runtime_binding_status(path)
    changed = replace(binding, profile="changed")

    def fail_replace(_source: object, _destination: object) -> None:
        if failure == "keyboard_interrupt":
            raise KeyboardInterrupt
        if failure == "system_exit":
            raise SystemExit
        raise OSError("injected locked replace failure")

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("injected locked write failure")

    with monkeypatch.context() as context:
        if failure == "write":
            context.setattr(runtime_binding.os, "fsync", fail_fsync)
        else:
            context.setattr(runtime_binding.os, "replace", fail_replace)
        if failure in {"keyboard_interrupt", "system_exit"}:
            expected_exception = (
                KeyboardInterrupt if failure == "keyboard_interrupt" else SystemExit
            )
            with pytest.raises(expected_exception):
                publish_runtime_binding(
                    path,
                    changed,
                    expected_previous_state=expected,
                )
        else:
            with pytest.raises(RuntimeBindingError, match="原子发布失败") as raised:
                publish_runtime_binding(
                    path,
                    changed,
                    expected_previous_state=expected,
                )
            assert raised.value.reason_code == "runtime_publish_atomic_replace_failed"

    assert not list(path.parent.glob(".runtime.json.*.tmp"))
    retried = publish_runtime_binding(
        path,
        changed,
        expected_previous_state=expected,
    )
    assert retried.published is True
    assert load_runtime_binding(path) == changed


def test_unlock_error_releases_descriptor_and_leaves_no_temporary_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding(tmp_path)
    path = Path(binding.install_root) / "runtime.json"
    publish_runtime_binding(path, binding)
    expected = runtime_binding_status(path)
    changed = replace(binding, profile="published-before-unlock-error")
    real_release = runtime_binding._release_runtime_file_lock

    def release_then_fail(lock_file: object) -> None:
        real_release(lock_file)
        raise OSError("injected unlock failure")

    with monkeypatch.context() as context:
        context.setattr(
            runtime_binding,
            "_release_runtime_file_lock",
            release_then_fail,
        )
        with pytest.raises(RuntimeBindingError, match="无法取得发布锁") as raised:
            publish_runtime_binding(
                path,
                changed,
                expected_previous_state=expected,
            )
        assert raised.value.reason_code == "runtime_publish_lock_failed"

    assert load_runtime_binding(path) == changed
    assert not list(path.parent.glob(".runtime.json.*.tmp"))
    assert publish_runtime_binding(path, changed).reused is True


def test_directory_sync_failure_after_replace_reports_published_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding(tmp_path)
    path = Path(binding.install_root) / "runtime.json"
    publish_runtime_binding(path, binding)
    expected = runtime_binding_status(path)
    changed = replace(binding, profile="changed-after-replace")

    def fail_directory_sync(_path: Path) -> None:
        raise OSError("injected directory sync failure")

    monkeypatch.setattr(
        runtime_binding,
        "_sync_directory",
        fail_directory_sync,
    )
    result = publish_runtime_binding(
        path,
        changed,
        expected_previous_state=expected,
    )

    assert result.published is True
    assert result.reused is False
    assert load_runtime_binding(path) == changed
    assert not list(path.parent.glob(".runtime.json.*.tmp"))


def test_persistent_binding_resolves_exact_funasr_models_and_media_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = _binding(tmp_path)
    path = Path(binding.install_root) / "runtime.json"
    publish_runtime_binding(path, binding)
    monkeypatch.setenv("ROUGHCUT_RUNTIME_BINDING", str(path))
    monkeypatch.delenv("ROUGHCUT_FUNASR_PYTHON", raising=False)
    monkeypatch.delenv("ROUGHCUT_FUNASR_MODEL_ROOT", raising=False)
    monkeypatch.delenv("ROUGHCUT_FFMPEG_COMMAND", raising=False)
    monkeypatch.delenv("ROUGHCUT_FFPROBE_COMMAND", raising=False)

    selected = resolve_funasr_selection()
    ffmpeg = resolve_runtime_tool("ffmpeg")
    ffprobe = resolve_runtime_tool("ffprobe")

    assert selected.source == "persistent_managed"
    assert selected.python_path == Path(binding.python.interpreter)
    assert selected.model_paths == {
        key: Path(component.path) for key, component in binding.components.items()
    }
    assert selected.ffmpeg_command == binding.ffmpeg.command
    assert ffmpeg.command == binding.ffmpeg.command
    assert ffprobe.command == binding.ffprobe.command
    assert ffmpeg.source == ffprobe.source == "persistent_external"


def test_media_operation_loader_uses_configured_binding_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _default_interpreter, default_selection = _alignment_python(tmp_path / "default")
    default_binding = _schema_two_binding(tmp_path / "default", default_selection)
    configured_selection = _bbc_selection(tmp_path / "configured")
    configured_binding = _schema_two_binding(
        tmp_path / "configured", configured_selection
    )
    default_path = Path(default_binding.install_root) / "runtime.json"
    configured_path = Path(configured_binding.install_root) / "runtime.json"
    publish_runtime_binding(default_path, default_binding)
    publish_runtime_binding(configured_path, configured_binding)
    monkeypatch.setattr(runtime_binding, "runtime_binding_path", lambda: default_path)
    monkeypatch.setenv("ROUGHCUT_RUNTIME_BINDING", str(configured_path))

    loaded = media_operations._load_persistent_runtime()
    health = diagnostics(runtime_path=configured_path)

    assert loaded.binding == configured_binding
    assert default_binding.alignment_python is not None
    assert configured_binding.alignment_python is not None
    assert default_binding.alignment_python.provider == AUDALIGN_PROVIDER
    assert configured_binding.alignment_python.provider == BBC_AUDIO_OFFSET_FINDER_PROVIDER
    assert loaded.runtime_binding_sha256 == hashlib.sha256(
        configured_path.read_bytes()
    ).hexdigest()
    assert loaded.ffmpeg.command == configured_binding.ffmpeg.command
    assert loaded.ffprobe.command == configured_binding.ffprobe.command
    assert health["runtime_binding"]["plan_hash"] == (  # type: ignore[index]
        configured_binding.evidence.plan_hash
    )


def test_media_operation_loader_keeps_launcher_default_when_override_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default_binding = _binding(tmp_path / "default")
    default_path = Path(default_binding.install_root) / "runtime.json"
    publish_runtime_binding(default_path, default_binding)
    monkeypatch.delenv("ROUGHCUT_RUNTIME_BINDING", raising=False)
    monkeypatch.setattr(runtime_binding, "runtime_binding_path", lambda: default_path)

    loaded = media_operations._load_persistent_runtime()

    assert loaded.binding == default_binding
    assert loaded.runtime_binding_sha256 == hashlib.sha256(
        default_path.read_bytes()
    ).hexdigest()


def test_media_operation_loader_rejects_relative_binding_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default_binding = _binding(tmp_path / "default")
    default_path = Path(default_binding.install_root) / "runtime.json"
    publish_runtime_binding(default_path, default_binding)
    monkeypatch.setenv("ROUGHCUT_RUNTIME_BINDING", "relative/runtime.json")

    with pytest.raises(RuntimeBindingError, match="absolute"):
        media_operations._load_persistent_runtime()


def test_explicit_and_process_overrides_precede_persistent_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = _binding(tmp_path)
    path = Path(binding.install_root) / "runtime.json"
    publish_runtime_binding(path, binding)
    process_ffmpeg = _executable(
        tmp_path / "process/ffmpeg", "ffmpeg version process"
    )
    explicit_ffmpeg = _executable(
        tmp_path / "explicit/ffmpeg", "ffmpeg version explicit"
    )
    monkeypatch.setenv("ROUGHCUT_RUNTIME_BINDING", str(path))
    monkeypatch.setenv("ROUGHCUT_FFMPEG_COMMAND", str(process_ffmpeg))

    assert resolve_runtime_tool("ffmpeg").command == str(process_ffmpeg)
    selected = resolve_runtime_tool(
        "ffmpeg", explicit_command=str(explicit_ffmpeg)
    )
    assert selected.command == str(explicit_ffmpeg)
    assert selected.source == "explicit_override"


def test_unconfigured_diagnostics_names_binding_and_next_component_plan(
    tmp_path: Path,
) -> None:
    payload = diagnostics(runtime_path=tmp_path / "missing/runtime.json")

    runtime = payload["runtime_binding"]
    assert isinstance(runtime, dict)
    assert runtime["status"] == "unconfigured"
    assert runtime["source"] == "unconfigured"
    assert runtime["message"] == "Roughcut runtime binding 未配置"
    assert runtime["next_action"] == "component_plan"
    assert payload["funasr"]["source"] == "unconfigured"  # type: ignore[index]
    assert payload["ffmpeg"]["source"] == "unconfigured"  # type: ignore[index]
    assert ".roughcut/toolchains" not in json.dumps(payload, ensure_ascii=False)


def test_diagnostics_reports_audalign_as_production_ready(
    tmp_path: Path,
) -> None:
    _interpreter, selection = _alignment_python(tmp_path)
    binding = _schema_two_binding(tmp_path, selection)
    path = Path(binding.install_root) / "runtime.json"
    publish_runtime_binding(path, binding)

    alignment = diagnostics(runtime_path=path)["alignment"]

    assert alignment == {
        "required_provider": AUDALIGN_PROVIDER,
        "required_provider_version": "1.3.1",
        "configured_provider": AUDALIGN_PROVIDER,
        "configured_provider_version": "1.3.1",
        "interpreter": selection.interpreter,
        "status": "available",
        "production_ready": True,
    }


def test_diagnostics_reports_bbc_as_provider_mismatch(
    tmp_path: Path,
) -> None:
    selection = _bbc_selection(tmp_path)
    binding = _schema_two_binding(tmp_path, selection)
    path = Path(binding.install_root) / "runtime.json"
    publish_runtime_binding(path, binding)

    alignment = diagnostics(runtime_path=path)["alignment"]

    assert alignment == {
        "required_provider": AUDALIGN_PROVIDER,
        "required_provider_version": "1.3.1",
        "configured_provider": BBC_AUDIO_OFFSET_FINDER_PROVIDER,
        "configured_provider_version": "0.5.5",
        "interpreter": selection.interpreter,
        "status": "provider_mismatch",
        "production_ready": False,
    }


def test_diagnostics_distinguishes_missing_and_invalid_alignment(
    tmp_path: Path,
) -> None:
    missing = diagnostics(runtime_path=tmp_path / "missing/runtime.json")["alignment"]
    assert missing["status"] == "missing"  # type: ignore[index]
    assert missing["production_ready"] is False  # type: ignore[index]

    _interpreter, selection = _alignment_python(tmp_path / "invalid")
    binding = _schema_two_binding(tmp_path / "invalid", selection)
    payload = binding.to_dict()
    alignment_payload = payload["alignment_python"]
    assert isinstance(alignment_payload, dict)
    alignment_payload["audalign_version"] = "9.9.9"
    path = Path(binding.install_root) / "runtime.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

    invalid = diagnostics(runtime_path=path)["alignment"]
    assert invalid["status"] == "invalid"  # type: ignore[index]
    assert invalid["production_ready"] is False  # type: ignore[index]


def test_diagnostics_reports_missing_bbc_interpreter(
    tmp_path: Path,
) -> None:
    selection = _bbc_selection(tmp_path)
    interpreter = Path(selection.interpreter)
    binding = _schema_two_binding(tmp_path, selection)
    path = Path(binding.install_root) / "runtime.json"
    publish_runtime_binding(path, binding)
    interpreter.unlink()

    alignment = diagnostics(runtime_path=path)["alignment"]

    assert alignment["status"] == "provider_mismatch"  # type: ignore[index]
    assert alignment["production_ready"] is False  # type: ignore[index]


def test_diagnostics_reports_invalid_bbc_closure_with_executable_interpreter(
    tmp_path: Path,
) -> None:
    selection = _bbc_selection(tmp_path)
    binding = _schema_two_binding(tmp_path, selection)
    payload = binding.to_dict()
    alignment_payload = payload["alignment_python"]
    assert isinstance(alignment_payload, dict)
    alignment_payload["distributions"] = alignment_payload["distributions"][:-1]
    path = Path(binding.install_root) / "runtime.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

    alignment = diagnostics(runtime_path=path)["alignment"]

    assert alignment["status"] == "invalid"  # type: ignore[index]
    assert alignment["production_ready"] is False  # type: ignore[index]


def test_configured_diagnostics_reports_component_sources(
    tmp_path: Path,
) -> None:
    binding = _binding(tmp_path, source_type="external")
    path = Path(binding.install_root) / "runtime.json"
    publish_runtime_binding(path, binding)

    payload = diagnostics(runtime_path=path)

    assert payload["runtime_binding"]["source"] == "persistent_external"  # type: ignore[index]
    assert payload["funasr"]["source"] == "persistent_external"  # type: ignore[index]
    assert payload["ffmpeg"]["source"] == "persistent_external"  # type: ignore[index]
    assert payload["ffmpeg"]["ffmpeg"]["command"] == binding.ffmpeg.command  # type: ignore[index]


def test_diagnostics_labels_process_tool_override_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = _binding(tmp_path)
    path = Path(binding.install_root) / "runtime.json"
    publish_runtime_binding(path, binding)
    override = _executable(
        tmp_path / "single process/ffmpeg", "ffmpeg version process override"
    )
    monkeypatch.setenv("ROUGHCUT_FFMPEG_COMMAND", str(override))

    payload = diagnostics(runtime_path=path)

    assert payload["ffmpeg"]["source"] == "explicit_override"  # type: ignore[index]
    assert payload["ffmpeg"]["ffmpeg"]["source"] == "explicit_override"  # type: ignore[index]
    assert payload["ffmpeg"]["ffmpeg"]["command"] == str(override)  # type: ignore[index]


def test_diagnostics_labels_process_funasr_override_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = _binding(tmp_path)
    path = Path(binding.install_root) / "runtime.json"
    publish_runtime_binding(path, binding)
    python = _executable(tmp_path / "single process/python", "Python fixture")
    models = tmp_path / "single process/models"
    models.mkdir(parents=True)
    monkeypatch.setenv("ROUGHCUT_FUNASR_PYTHON", str(python))
    monkeypatch.setenv("ROUGHCUT_FUNASR_MODEL_ROOT", str(models))

    payload = diagnostics(runtime_path=path)

    assert payload["funasr"]["source"] == "explicit_override"  # type: ignore[index]
    assert payload["funasr"]["python"]["source"] == "explicit_override"  # type: ignore[index]
    assert payload["funasr"]["python"]["command"] == str(python)  # type: ignore[index]


def test_absolute_cli_and_stdio_mcp_read_the_same_launcher_root_binding_without_env(
    tmp_path: Path,
) -> None:
    binding = _binding(tmp_path)
    install_root = Path(binding.install_root)
    launcher_python = install_root / "venv/bin/python"
    launcher_python.parent.mkdir(parents=True)
    try:
        launcher_python.symlink_to(Path(sys.executable))
    except (OSError, NotImplementedError):
        pytest.skip("launcher symlinks are unavailable")
    publish_runtime_binding(install_root / "runtime.json", binding)
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("ROUGHCUT_")
    }
    environment["PYTHONPATH"] = str(CORE_ROOT / "src")

    cli = subprocess.run(
        [str(launcher_python), "-m", "roughcut.cli", "diagnostics", "--json"],
        cwd=CORE_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    mcp = subprocess.run(
        [str(launcher_python), "-m", "roughcut.mcp"],
        cwd=CORE_ROOT,
        env=environment,
        input=(
            '{"jsonrpc":"2.0","id":1,"method":"tools/call",'
            '"params":{"name":"diagnostics"}}\n'
        ),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert cli.returncode == mcp.returncode == 0
    assert cli.stderr == mcp.stderr == ""
    cli_payload = json.loads(cli.stdout)
    response = json.loads(mcp.stdout)
    mcp_payload = response["result"]["structuredContent"]
    assert cli_payload == mcp_payload
    assert cli_payload["funasr"]["python"]["command"] == binding.python.interpreter
    assert cli_payload["funasr"]["models"]["asr"]["path"] == binding.components["asr"].path
    assert cli_payload["ffmpeg"]["ffmpeg"]["command"] == binding.ffmpeg.command


def test_render_and_pcm_decode_use_persistent_ffmpeg_without_path_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = _binding(tmp_path)
    path = Path(binding.install_root) / "runtime.json"
    publish_runtime_binding(path, binding)
    monkeypatch.setenv("ROUGHCUT_RUNTIME_BINDING", str(path))
    calls: list[list[str]] = []

    def fake_process(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1:] == ["-version"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="ffmpeg version fixture\n",
                stderr="",
            )
        output = Path(command[-1])
        with wave.open(str(output), "wb") as pcm:
            pcm.setnchannels(1)
            pcm.setsampwidth(2)
            pcm.setframerate(16_000)
            pcm.writeframes(b"\0\0")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    ffmpeg, ffprobe = resolve_render_tools()
    source = tmp_path / "source.fake"
    source.write_bytes(b"fixture")
    decoded = decode_audio_to_pcm(
        source,
        tmp_path / "decoded.wav",
        process_runner=fake_process,
    )

    assert ffmpeg.resolved_path == binding.ffmpeg.command
    assert ffprobe.resolved_path == binding.ffprobe.command
    assert decoded.ffmpeg_path == binding.ffmpeg.command
    assert calls[0][0] == calls[1][0] == binding.ffmpeg.command


def test_funasr_runner_config_uses_exact_binding_models_and_lazy_campp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = _binding(tmp_path)
    path = Path(binding.install_root) / "runtime.json"
    publish_runtime_binding(path, binding)
    monkeypatch.setenv("ROUGHCUT_RUNTIME_BINDING", str(path))

    without_speaker = configured_funasr_config()
    with_speaker = configured_funasr_config(speaker_diarization=True)

    assert without_speaker.python_path == Path(binding.python.interpreter)
    assert without_speaker.asr_model_path == Path(binding.components["asr"].path)
    assert without_speaker.vad_model_path == Path(binding.components["vad"].path)
    assert without_speaker.punc_model_path == Path(binding.components["punc"].path)
    assert without_speaker.speaker_model_path is None
    assert with_speaker.speaker_model_path == Path(binding.components["campp"].path)
    assert with_speaker.ffmpeg_command == binding.ffmpeg.command


def test_source_probe_uses_persistent_ffprobe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = _binding(tmp_path)
    path = Path(binding.install_root) / "runtime.json"
    publish_runtime_binding(path, binding)
    monkeypatch.setenv("ROUGHCUT_RUNTIME_BINDING", str(path))
    captured: dict[str, object] = {}

    def fake_probe(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        payload = {
            "format": {"duration": "1.0", "start_time": "0"},
            "streams": [
                {
                    "codec_type": "audio",
                    "codec_name": "pcm_s16le",
                    "sample_rate": "16000",
                    "start_time": "0",
                    "duration": "1.0",
                }
            ],
        }
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr(ffprobe_adapter.subprocess, "run", fake_probe)
    source = tmp_path / "source.fake"
    source.write_bytes(b"fixture")

    probe = probe_media(source)

    assert probe.duration_ticks == TICKS_PER_SECOND
    command = captured["command"]
    assert isinstance(command, list)
    assert command[0] == binding.ffprobe.command


# ---------------------------------------------------------------------------
# Schema-2 alignment selection: interpreter symlink, venv tree, closure
# ---------------------------------------------------------------------------


def _alignment_python(
    tmp_path: Path,
) -> tuple[Path, RuntimeAlignmentPython]:
    """One schema-2 managed alignment selection inside a fake venv tree."""
    managed_root = tmp_path / "组件 数据" / "Managed"
    venv = managed_root / "audalign" / "venv"
    bin_dir = venv / "bin"
    bin_dir.mkdir(parents=True)
    interpreter = bin_dir / "python"
    interpreter.write_text("#!/bin/sh\necho 'Python 3.11.9'\n", encoding="utf-8")
    interpreter.chmod(0o755)
    (venv / "pyvenv.cfg").write_text("[venv]\n", encoding="utf-8")
    (venv / "lib").mkdir()
    distributions = (
        {"name": "audalign", "version": "1.3.1"},
        {"name": "contourpy", "version": "1.3.3"},
        {"name": "cycler", "version": "0.12.1"},
        {"name": "fonttools", "version": "4.63.0"},
        {"name": "kiwisolver", "version": "1.5.0"},
        {"name": "matplotlib", "version": "3.8.2"},
        {"name": "numpy", "version": "1.26.4"},
        {"name": "packaging", "version": "26.2"},
        {"name": "pillow", "version": "12.3.0"},
        {"name": "pydub", "version": "0.25.1"},
        {"name": "pyparsing", "version": "3.3.2"},
        {"name": "python-dateutil", "version": "2.9.0.post0"},
        {"name": "scipy", "version": "1.12.0"},
        {"name": "setuptools", "version": "59.6.0"},
        {"name": "six", "version": "1.17.0"},
        {"name": "tqdm", "version": "4.66.2"},
    )
    selection = RuntimeAlignmentPython(
        source_type="managed",
        ownership="roughcut_managed",
        interpreter=str(interpreter),
        python_version="3.11",
        distributions=distributions,
        dependency_lock_receipt={"algorithm": "sha256", "value": "f" * 64},
        license_notice_receipt={"algorithm": "sha256", "value": "e" * 64},
        component_manifest_receipt={
            "algorithm": "sha256",
            "value": "d" * 64,
        },
        provider=AUDALIGN_PROVIDER,
        provider_version="1.3.1",
        upstream_commit=AUDALIGN_UPSTREAM_COMMIT,
    )
    return interpreter, selection


def _schema_two_binding(
    tmp_path: Path, selection: RuntimeAlignmentPython
) -> RuntimeBinding:
    binding = _binding(tmp_path, source_type="managed")
    return replace(
        binding,
        schema_version=2,
        alignment_python=selection,
        evidence=replace(
            binding.evidence,
            managed_root=str(Path(selection.interpreter).parent.parent.parent.parent),
        ),
    )


def test_schema_two_alignment_roundtrip_and_venv_root(tmp_path: Path) -> None:
    _interpreter, selection = _alignment_python(tmp_path)
    binding = _schema_two_binding(tmp_path, selection)
    binding.validate()
    # the venv root is interpreter.parent.parent (bin/python), not a fixed
    # parents[2] index
    assert Path(selection.interpreter).parent.parent.is_dir()
    path = Path(binding.install_root) / "runtime.json"
    result = publish_runtime_binding(path, binding)
    assert result.published is True
    loaded = load_runtime_binding(path)
    assert loaded.schema_version == 2
    assert loaded.alignment_python is not None
    assert loaded.alignment_python.interpreter == selection.interpreter


def test_schema_two_windows_roundtrip_requires_colorama(tmp_path: Path) -> None:
    _interpreter, selection = _alignment_python(tmp_path)
    base_binding = _binding(tmp_path)
    schema_two = _schema_two_binding(tmp_path, selection)
    managed_root = r"C:\Users\fixture\Managed"
    windows_interpreter = rf"{managed_root}\venv\Scripts\python.exe"
    windows_components = {
        key: replace(
            component,
            path=rf"{managed_root}\models\{component.component}",
        )
        for key, component in base_binding.components.items()
    }
    windows_selection = replace(
        selection,
        interpreter=windows_interpreter,
        distributions=(
            *selection.distributions,
            {"name": "colorama", "version": "0.4.6"},
        ),
    )
    binding = replace(
        schema_two,
        install_root=r"C:\Users\fixture\.roughcut",
        platform="windows",
        architecture="x86_64",
        python=replace(
            base_binding.python,
            interpreter=windows_interpreter,
        ),
        components=windows_components,
        ffmpeg=replace(
            base_binding.ffmpeg,
            command=r"C:\Program Files\FFmpeg\ffmpeg.exe",
        ),
        ffprobe=replace(
            base_binding.ffprobe,
            command=r"C:\Program Files\FFmpeg\ffprobe.exe",
        ),
        evidence=replace(
            schema_two.evidence,
            managed_root=managed_root,
        ),
        alignment_python=windows_selection,
    )
    loaded = RuntimeBinding.from_dict(binding.to_dict())
    loaded.validate(validate_target=False, validate_filesystem=False)
    assert loaded.schema_version == 2
    assert loaded.alignment_python is not None
    assert loaded.alignment_python.distributions[-1] == {
        "name": "colorama",
        "version": "0.4.6",
    }

    missing = replace(binding, alignment_python=selection)
    with pytest.raises(RuntimeBindingError):
        missing.validate(validate_target=False, validate_filesystem=False)


def test_alignment_selection_reads_the_historical_audalign_encoding(
    tmp_path: Path,
) -> None:
    _interpreter, selection = _alignment_python(tmp_path)
    historical = selection.to_dict()
    assert set(historical) == {
        "source_type",
        "ownership",
        "interpreter",
        "python_version",
        "audalign_version",
        "audalign_upstream_commit",
        "distributions",
        "dependency_lock_receipt",
        "license_notice_receipt",
        "component_manifest_receipt",
    }
    normalized = RuntimeAlignmentPython.from_dict(historical)
    assert normalized.provider == AUDALIGN_PROVIDER
    assert normalized.provider_version == "1.3.1"
    assert normalized.upstream_commit == AUDALIGN_UPSTREAM_COMMIT
    normalized.validate()


def test_audalign_alignment_serialization_stays_byte_compatible() -> None:
    historical = {
        "source_type": "managed",
        "ownership": "roughcut_managed",
        "interpreter": "/canonical/managed/audalign/venv/bin/python",
        "python_version": "3.11",
        "audalign_version": "1.3.1",
        "audalign_upstream_commit": AUDALIGN_UPSTREAM_COMMIT,
        "distributions": [{"name": "audalign", "version": "1.3.1"}],
        "dependency_lock_receipt": {"algorithm": "sha256", "value": "f" * 64},
        "license_notice_receipt": {"algorithm": "sha256", "value": "e" * 64},
        "component_manifest_receipt": {"algorithm": "sha256", "value": "d" * 64},
    }
    selection = RuntimeAlignmentPython.from_dict(historical)
    assert selection.provider == AUDALIGN_PROVIDER
    assert selection.to_dict() == historical


def test_alignment_selection_rejects_mixed_legacy_and_provider_encodings(
    tmp_path: Path,
) -> None:
    _interpreter, selection = _alignment_python(tmp_path)
    mixed = {**selection.to_dict(), "provider": AUDALIGN_PROVIDER}
    with pytest.raises(RuntimeBindingError):
        RuntimeAlignmentPython.from_dict(mixed)


@pytest.mark.parametrize(
    "extra",
    [
        {"provider_version": "9.9.9"},
        {"upstream_commit": AUDALIGN_UPSTREAM_COMMIT},
        {"provider": None},
    ],
    ids=["legacy-plus-provider-version", "legacy-plus-upstream-commit", "legacy-plus-null-provider"],
)
def test_alignment_selection_rejects_legacy_payloads_with_any_provider_key(
    tmp_path: Path, extra: dict[str, object]
) -> None:
    _interpreter, selection = _alignment_python(tmp_path)
    mixed = {**selection.to_dict(), **extra}
    with pytest.raises(RuntimeBindingError):
        RuntimeAlignmentPython.from_dict(mixed)


def test_alignment_selection_requires_one_complete_identity_family(
    tmp_path: Path,
) -> None:
    _interpreter, selection = _alignment_python(tmp_path)
    stripped = {
        key: value
        for key, value in selection.to_dict().items()
        if key not in {"audalign_version", "audalign_upstream_commit"}
    }
    with pytest.raises(RuntimeBindingError):
        RuntimeAlignmentPython.from_dict(stripped)
    with pytest.raises(RuntimeBindingError):
        RuntimeAlignmentPython.from_dict({**stripped, "provider_version": "0.5.5"})


def test_alignment_selection_unknown_provider_fails_closed(tmp_path: Path) -> None:
    _interpreter, selection = _alignment_python(tmp_path)
    payload = {
        key: value
        for key, value in selection.to_dict().items()
        if not key.startswith("audalign_")
    }
    payload["provider"] = BBC_AUDIO_OFFSET_FINDER_PROVIDER
    payload["provider_version"] = "0.5.5"
    parsed = RuntimeAlignmentPython.from_dict(payload)
    with pytest.raises(RuntimeBindingError):
        parsed.validate()


def test_bbc_request_is_explicitly_missing_on_an_audalign_only_binding(
    tmp_path: Path,
) -> None:
    _interpreter, selection = _alignment_python(tmp_path)
    assert (
        alignment_binding_supports_provider(selection, BBC_AUDIO_OFFSET_FINDER_PROVIDER)
        is False
    )
    assert alignment_binding_supports_provider(None, BBC_AUDIO_OFFSET_FINDER_PROVIDER) is False
    assert alignment_binding_supports_provider(selection, AUDALIGN_PROVIDER) is True


def _bbc_selection(tmp_path: Path) -> RuntimeAlignmentPython:
    managed_root = tmp_path / "组件 数据" / "Managed"
    venv = managed_root / "bbc_audio_offset_finder" / "venv"
    bin_dir = venv / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    interpreter = bin_dir / "python"
    interpreter.write_text("#!/bin/sh\necho 'Python 3.11.9'\n", encoding="utf-8")
    interpreter.chmod(0o755)
    (venv / "pyvenv.cfg").write_text("[venv]\n", encoding="utf-8")
    versions = runtime_binding.bbc_audio_offset_finder_distribution_versions_for(
        "macos"
    )
    distributions = tuple(
        {"name": name, "version": version} for name, version in versions.items()
    )
    return RuntimeAlignmentPython(
        source_type="managed",
        ownership="roughcut_managed",
        interpreter=str(interpreter),
        python_version="3.11",
        distributions=distributions,
        dependency_lock_receipt={"algorithm": "sha256", "value": "f" * 64},
        license_notice_receipt={"algorithm": "sha256", "value": "e" * 64},
        component_manifest_receipt={
            "algorithm": "sha256",
            "value": "d" * 64,
        },
        provider=BBC_AUDIO_OFFSET_FINDER_PROVIDER,
        provider_version="0.5.5",
        upstream_commit=None,
    )


def test_bbc_alignment_selection_roundtrip_and_serialization(tmp_path: Path) -> None:
    selection = _bbc_selection(tmp_path)
    selection.validate()
    selection.validate(platform="windows")
    payload = selection.to_dict()
    assert set(payload) == {
        "source_type",
        "ownership",
        "interpreter",
        "python_version",
        "distributions",
        "dependency_lock_receipt",
        "license_notice_receipt",
        "component_manifest_receipt",
        "provider",
        "provider_version",
    }
    parsed = RuntimeAlignmentPython.from_dict(payload)
    assert parsed == selection
    binding = replace(
        _schema_two_binding(tmp_path, selection),
        alignment_python=selection,
    )
    path = Path(binding.install_root) / "runtime.json"
    publish_runtime_binding(path, binding)
    loaded = load_runtime_binding(path)
    assert loaded.alignment_python is not None
    assert loaded.alignment_python.provider == BBC_AUDIO_OFFSET_FINDER_PROVIDER
    assert loaded.alignment_python.provider_version == "0.5.5"
    assert (
        Path(loaded.alignment_python.interpreter).parents[2].name
        == "bbc_audio_offset_finder"
    )


def test_bbc_alignment_selection_rejects_identity_drift(tmp_path: Path) -> None:
    selection = _bbc_selection(tmp_path)
    with pytest.raises(RuntimeBindingError):
        replace(selection, provider_version="0.5.4").validate()
    with pytest.raises(RuntimeBindingError):
        replace(
            selection, upstream_commit=AUDALIGN_UPSTREAM_COMMIT
        ).validate()
    drifted = replace(
        selection,
        distributions=(*selection.distributions[:-1], {"name": "colorama"}),
    )
    with pytest.raises(RuntimeBindingError):
        drifted.validate()


def test_schema_two_rejects_alignment_interpreter_symlink(
    tmp_path: Path,
) -> None:
    interpreter, selection = _alignment_python(tmp_path)
    real = tmp_path / "real-python"
    real.write_text("#!/bin/sh\n", encoding="utf-8")
    real.chmod(0o755)
    interpreter.unlink()
    interpreter.symlink_to(real)
    binding = _schema_two_binding(tmp_path, selection)
    with pytest.raises(RuntimeBindingError):
        binding.validate()


def test_schema_two_rejects_alignment_venv_symlink(tmp_path: Path) -> None:
    interpreter, selection = _alignment_python(tmp_path)
    venv = interpreter.parent.parent
    real_venv = tmp_path / "real-venv"
    real_venv.mkdir()
    venv.rename(real_venv)
    venv.symlink_to(real_venv)
    binding = _schema_two_binding(tmp_path, selection)
    with pytest.raises(RuntimeBindingError):
        binding.validate()


def test_schema_two_rejects_nested_venv_symlink(tmp_path: Path) -> None:
    interpreter, selection = _alignment_python(tmp_path)
    lib = interpreter.parent.parent / "lib"
    outside = tmp_path / "outside"
    outside.mkdir()
    lib.rmdir()
    lib.symlink_to(outside)
    binding = _schema_two_binding(tmp_path, selection)
    with pytest.raises(RuntimeBindingError):
        binding.validate()


def test_schema_two_rejects_extra_distribution(tmp_path: Path) -> None:
    _interpreter, selection = _alignment_python(tmp_path)
    extra = replace(
        selection,
        distributions=(
            *selection.distributions,
            {"name": "matplotlib", "version": "3.8.2"},
        ),
    )
    binding = _schema_two_binding(tmp_path, extra)
    with pytest.raises(RuntimeBindingError):
        binding.validate()


def test_schema_two_rejects_missing_distribution(tmp_path: Path) -> None:
    _interpreter, selection = _alignment_python(tmp_path)
    missing = replace(selection, distributions=selection.distributions[:1])
    binding = _schema_two_binding(tmp_path, missing)
    with pytest.raises(RuntimeBindingError):
        binding.validate()


def test_schema_two_rejects_wrong_distribution_version(tmp_path: Path) -> None:
    _interpreter, selection = _alignment_python(tmp_path)
    wrong = replace(
        selection,
        distributions=(
            *selection.distributions[1:],
            {"name": "audalign", "version": "9.9.9"},
        ),
    )
    binding = _schema_two_binding(tmp_path, wrong)
    with pytest.raises(RuntimeBindingError):
        binding.validate()


def test_schema_two_rejects_duplicate_receipt_entry(tmp_path: Path) -> None:
    _interpreter, selection = _alignment_python(tmp_path)
    duplicated = replace(
        selection,
        distributions=(
            *selection.distributions[1:],
            {"name": "audalign", "version": "1.3.1"},
        ),
    )
    binding = _schema_two_binding(tmp_path, duplicated)
    with pytest.raises(RuntimeBindingError):
        binding.validate()


def test_schema_two_rejects_lock_and_license_drift(tmp_path: Path) -> None:
    _interpreter, selection = _alignment_python(tmp_path)
    # a receipt value that is not an exact sha256 digest is rejected
    drifted = replace(
        selection,
        dependency_lock_receipt={"algorithm": "sha256", "value": "0" * 63},
    )
    binding = _schema_two_binding(tmp_path, drifted)
    with pytest.raises(RuntimeBindingError):
        binding.validate()
    drifted_license = replace(
        selection,
        license_notice_receipt={"algorithm": "sha256", "value": "1" * 63},
    )
    binding_license = _schema_two_binding(tmp_path, drifted_license)
    with pytest.raises(RuntimeBindingError):
        binding_license.validate()
    # a receipt that changes the algorithm is also rejected (fresh project
    # root, since the fake venv tree was already created by the first cases)
    tmp2 = tmp_path / "other"
    tmp2.mkdir()
    _interpreter2, selection2 = _alignment_python(tmp2)
    bad_algorithm = replace(
        selection2,
        dependency_lock_receipt={"algorithm": "md5", "value": "f" * 64},
    )
    binding_algorithm = _schema_two_binding(tmp2, bad_algorithm)
    with pytest.raises(RuntimeBindingError):
        binding_algorithm.validate()
