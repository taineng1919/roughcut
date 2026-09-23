from __future__ import annotations

import hashlib
import http.client
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.error
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, Self

import pytest

from roughcut.adapters import component_download, component_environment, component_installation
from roughcut.adapters.component_download import download_artifact
from roughcut.adapters.component_environment import (
    COMPONENT_MANIFEST_FILENAME,
    ComponentError,
    ComponentManifest,
    ComponentRecord,
    ComponentVerification,
    PythonRuntimeRecord,
    build_external_component,
    component_digest,
    component_record_digest,
    current_architecture,
    current_platform,
    load_component_manifest,
    uninstall_managed_components,
    write_component_manifest,
)
from roughcut.adapters.component_installation import (
    AUDALIGN_GROUP_NAME,
    BBC_AUDIO_OFFSET_FINDER_GROUP_NAME,
    MODEL_COMPONENT_NAMES,
    WINDOWS_PATH_EVIDENCE_SHA256,
    AlignmentGroupSpec,
    ArtifactSpec,
    ComponentInstallError,
    ExternalRuntimeProfile,
    FFmpegAction,
    ModelSpec,
    ReleaseCatalog,
    ReleaseProfile,
    RuntimeSpec,
    StaleApprovedPlanError,
    _build_path_budget,
    _resolve_prerequisites,
    apply_install_plan,
    build_install_plan,
    cache_artifact_path,
    cache_receipt_path,
    load_release_catalog,
    probe_external_python,
    validate_install_preflight,
)
from roughcut.adapters.runtime_binding import (
    load_runtime_binding,
    publish_runtime_binding,
)
from roughcut.application.diagnostics import diagnostics
from roughcut.application.installation_operations import (
    installation_operation_status,
    run_component_installation,
)
from roughcut.domain.installation_operation import InstallationResultRef

MODEL_REVISIONS = {
    "model_asr": "0141367fdc9b6ba58b0442ef34bceb56a6c1789c",
    "model_vad": "f9a8b8274674755d925277e27063869038d41515",
    "model_punc": "45ab6961ad58a973ce7785401b4e93a0aab907a3",
    "model_spk": "v2.0.2",
}
LEGACY_EXTERNAL_MODEL_VERSION = (
    "roughcut-legacy-external-macos-arm64-funasr-1.3.8-ab-v1"
)
LEGACY_EXTERNAL_MODEL_DIGESTS = {
    "model_asr": "53f0dd825f91109d19bfcf901ae668039b64d49fea3af52e2cc1c4acebc22f69",
    "model_vad": "3602ad8d7b9728e7a60dda39deba552451285359d8c07e532f415271d246e402",
    "model_punc": "f39ff9c4607103bd65a4046d2c7b525c57e2653eb7bbecf1dda7276de69df113",
}
LEGACY_EXTERNAL_MODEL_ORIGINS = {
    "model_asr": (
        "iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
    ),
    "model_vad": "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
    "model_punc": "iic/punc_ct-transformer_cn-en-common-vocab471067-large",
}
AUDALIGN_CATALOG_PATHS = (
    "core/src/roughcut/component_catalog/audalign/audalign-licenses-macos-arm64-py311.json",
    "core/src/roughcut/component_catalog/audalign/audalign-licenses-windows-x64-py311.json",
    "core/src/roughcut/component_catalog/audalign/audalign-macos-arm64-py311-artifacts.json",
    "core/src/roughcut/component_catalog/audalign/audalign-macos-arm64-py311.lock",
    "core/src/roughcut/component_catalog/audalign/audalign-windows-x64-py311-artifacts.json",
    "core/src/roughcut/component_catalog/audalign/audalign-windows-x64-py311.lock",
)
AUDALIGN_CATALOG_SHA256 = {
    "core/src/roughcut/component_catalog/audalign/audalign-licenses-macos-arm64-py311.json": "fc7254373ff8e5fcd24af22418d87f81f633c4e75eabc3db0596e57381e8cfc3",
    "core/src/roughcut/component_catalog/audalign/audalign-licenses-windows-x64-py311.json": "3c062539e1ec540cb990c6877b12eef3d5e1aed856053e53a6bc626736754fe2",
    "core/src/roughcut/component_catalog/audalign/audalign-macos-arm64-py311-artifacts.json": "e9d334853bd7be0bbc6cd3f4dcea9409598179fb854ad459f96b10fd44fc4a41",
    "core/src/roughcut/component_catalog/audalign/audalign-macos-arm64-py311.lock": "3f1e90c9f4acdd12c62fc29641f7db2e6bb34b0b5a6d4dc1d0aed216f7eaaa81",
    "core/src/roughcut/component_catalog/audalign/audalign-windows-x64-py311-artifacts.json": "b136f36734c29b0dbe4fca0c7fc67287a6ab6567c80c04d99cf1673841f0c560",
    "core/src/roughcut/component_catalog/audalign/audalign-windows-x64-py311.lock": "c240c2138fd031e2f2099351f8ba27dea9e4dfabb168a6cfcf81b53979126984",
}
BBC_CATALOG_PATHS = (
    "core/src/roughcut/component_catalog/audio-offset-finder/audio-offset-finder-licenses-macos-arm64-py311.json",
    "core/src/roughcut/component_catalog/audio-offset-finder/audio-offset-finder-licenses-windows-x64-py311.json",
    "core/src/roughcut/component_catalog/audio-offset-finder/audio-offset-finder-macos-arm64-py311-artifacts.json",
    "core/src/roughcut/component_catalog/audio-offset-finder/audio-offset-finder-macos-arm64-py311.lock",
    "core/src/roughcut/component_catalog/audio-offset-finder/audio-offset-finder-windows-x64-py311-artifacts.json",
    "core/src/roughcut/component_catalog/audio-offset-finder/audio-offset-finder-windows-x64-py311.lock",
)
BBC_CATALOG_SHA256 = {
    "core/src/roughcut/component_catalog/audio-offset-finder/audio-offset-finder-licenses-macos-arm64-py311.json": "b36d1d6fd18401e5beb96bb5eb3f3c9b4f11c8d5c3d454be5b66763f3a5eae02",
    "core/src/roughcut/component_catalog/audio-offset-finder/audio-offset-finder-licenses-windows-x64-py311.json": "badc6d174fad32703a90885dc6f7fbbc6eaced29840c30c47f68b7a930830934",
    "core/src/roughcut/component_catalog/audio-offset-finder/audio-offset-finder-macos-arm64-py311-artifacts.json": "aaf62c55e066bde18ed2cd7efe33e9505de6959b04eff9efe0eac56229425920",
    "core/src/roughcut/component_catalog/audio-offset-finder/audio-offset-finder-macos-arm64-py311.lock": "8b4c9b0ba6e40468569f70c0875541bc7a465b021b84d60ae82f169599d61a0d",
    "core/src/roughcut/component_catalog/audio-offset-finder/audio-offset-finder-windows-x64-py311-artifacts.json": "fa475c9c5025061a18c0afd7de79d472bf8429f95ade2d9131b283c70d111bb0",
    "core/src/roughcut/component_catalog/audio-offset-finder/audio-offset-finder-windows-x64-py311.lock": "400ac2d3cc7fcf3b469ed3797d5e4ae148fae318d53209f55f69030b02580db0",
}


def _model_primary_payload(name: str) -> str:
    return "campplus_cn_common.bin" if name == "model_spk" else "model.pt"


def _model_runtime_files(name: str) -> tuple[str, ...]:
    return component_environment.MODEL_RUNTIME_FILES[name]


def _write_model_runtime_files(model: Path, name: str, *, marker: str = "fixture") -> None:
    model.mkdir(parents=True, exist_ok=True)
    for required in _model_runtime_files(name):
        (model / required).write_bytes(f"{name} {marker} {required}".encode())


class _ArtifactHandler(BaseHTTPRequestHandler):
    payloads: ClassVar[dict[str, bytes]] = {}
    support_range = True
    short_read_bytes: int | None = None
    short_read_served = False
    requests: ClassVar[list[str | None]] = []

    def do_GET(self) -> None:
        payload = self.payloads[self.path]
        range_header = self.headers.get("Range")
        self.requests.append(range_header)
        short_read = (
            range_header is None
            and self.short_read_bytes is not None
            and not type(self).short_read_served
        )
        if short_read:
            type(self).short_read_served = True
            body = payload[: self.short_read_bytes]
            self.send_response(200)
        elif range_header is not None and self.support_range:
            start = int(range_header.removeprefix("bytes=").removesuffix("-"))
            body = payload[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(payload) - 1}/{len(payload)}")
        else:
            body = payload
            self.send_response(200)
        self.send_header("Content-Length", str(len(payload) if short_read else len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@contextmanager
def _artifact_server(
    payloads: dict[str, bytes], *, support_range: bool = True, short_read_bytes: int | None = None
) -> Iterator[tuple[str, list[str | None]]]:
    handler = type("FixtureArtifactHandler", (_ArtifactHandler,), {})
    handler.payloads = payloads
    handler.support_range = support_range
    handler.short_read_bytes = short_read_bytes
    handler.short_read_served = False
    handler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}", handler.requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _artifact(url: str, payload: bytes, *, component: str = "python_runtime") -> ArtifactSpec:
    return ArtifactSpec(
        component=component,
        name="fixture",
        version="1.0",
        filename="fixture.bin",
        url=url,
        license="fixture-only",
        sha256=hashlib.sha256(payload).hexdigest(),
        size=len(payload),
        destination=(
            _model_primary_payload(component) if component != "python_runtime" else None
        ),
    )


def _fake_wheel(distribution: str, version: str) -> bytes:
    module = distribution.replace("-", "_")
    if distribution == "torch":
        source = (
            "class _Version:\n    cuda = None\n"
            "version = _Version()\n"
            "class _Cuda:\n    @staticmethod\n    def is_available():\n        return False\n"
            "cuda = _Cuda()\n"
        )
    elif distribution == "soundfile":
        source = (
            "import pathlib,sys\n"
            "import _soundfile_data\n"
            "_native = 'libsndfile_arm64.dylib' if sys.platform == 'darwin' else 'libsndfile_x64.dll'\n"
            "_full_path = str(pathlib.Path(_soundfile_data.__file__).parent / _native)\n"
            "__libsndfile_version__ = 'fixture'\n"
        )
    else:
        source = "__all__ = []\n"
    dist_info = f"{module}-{version}.dist-info"
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as wheel:
        wheel.writestr(f"{module}/__init__.py", source)
        if distribution == "soundfile":
            wheel.writestr("_soundfile_data/__init__.py", "")
            wheel.writestr("_soundfile_data/libsndfile_arm64.dylib", b"fixture")
            wheel.writestr("_soundfile_data/libsndfile_x64.dll", b"fixture")
        wheel.writestr(
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.1\nName: {distribution}\nVersion: {version}\n",
        )
        wheel.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: roughcut-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        wheel.writestr(f"{dist_info}/RECORD", "")
    return output.getvalue()


def _fixture_catalog(
    tmp_path: Path,
    base_url: str,
    *,
    with_audalign_group: bool = False,
    with_bbc_group: bool = False,
) -> tuple[ReleaseCatalog, dict[str, bytes]]:
    versions = {"funasr": "1.3.14", "torch": "2.6.0", "torchaudio": "2.6.0"}
    payloads: dict[str, bytes] = {}
    runtime_artifacts: list[ArtifactSpec] = []
    for name, version in versions.items():
        filename = f"{name}-{version}-py3-none-any.whl"
        payload = _fake_wheel(name, version)
        path = f"/{filename}"
        payloads[path] = payload
        runtime_artifacts.append(
            ArtifactSpec(
                component="python_runtime",
                name=name,
                version=version,
                filename=filename,
                url=f"{base_url}{path}",
                license="fixture-only",
                sha256=hashlib.sha256(payload).hexdigest(),
                size=len(payload),
            )
        )
    lock = tmp_path / "fixture-catalog/locks/macos-arm64-py311.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text(
        "\n".join(f"{name}=={version}" for name, version in versions.items()) + "\n",
        encoding="utf-8",
    )
    models: dict[str, ModelSpec] = {}
    for index, name in enumerate(MODEL_COMPONENT_NAMES):
        source = tmp_path / "fixture-model-digests" / name
        source.mkdir(parents=True)
        model_artifacts: list[ArtifactSpec] = []
        total_size = 0
        for required in component_environment.MODEL_RUNTIME_FILES[name]:
            payload = f"fixture model payload {index} {required}".encode()
            path = f"/{name}-{required.replace('/', '_')}"
            payloads[path] = payload
            (source / required).write_bytes(payload)
            artifact = ArtifactSpec(
                component=name,
                name=f"fixture/{name}:{required}",
                version=f"fixture-revision-{index}",
                filename=f"{name}-{required.replace('/', '_')}",
                url=f"{base_url}{path}",
                license="Apache-2.0",
                sha256=hashlib.sha256(payload).hexdigest(),
                size=len(payload),
                destination=required,
            )
            model_artifacts.append(artifact)
            total_size += len(payload)
        models[name] = ModelSpec(
            name=name,
            repository=f"fixture/{name}",
            revision=f"fixture-revision-{index}",
            license="Apache-2.0",
            directory_sha256=component_digest(source),
            size=total_size,
            artifacts=tuple(model_artifacts),
        )
    alignment_groups: tuple[AlignmentGroupSpec, ...] = ()
    if with_audalign_group:
        from roughcut.adapters.runtime_binding import (
            audalign_distribution_versions_for as fixture_audalign_distributions_for,
        )

        fixture_audalign_versions = fixture_audalign_distributions_for(
            current_platform()
        )
        audalign_artifacts: list[ArtifactSpec] = []
        for name, version in sorted(fixture_audalign_versions.items()):
            filename = f"{name.replace('-', '_')}-{version}-py3-none-any.whl"
            payload = _fake_wheel(name, version)
            path = f"/audalign-{filename}"
            payloads[path] = payload
            audalign_artifacts.append(
                ArtifactSpec(
                    component=AUDALIGN_GROUP_NAME,
                    name=name,
                    version=version,
                    filename=filename,
                    url=f"{base_url}{path}",
                    license="fixture-only",
                    sha256=hashlib.sha256(payload).hexdigest(),
                    size=len(payload),
                )
            )
        audalign_lock = tmp_path / "fixture-catalog/locks/audalign.lock"
        audalign_lock.parent.mkdir(parents=True, exist_ok=True)
        audalign_lock.write_text(
            "".join(
                f"{name}=={version} \\\n"
                f"    --hash=sha256:{hashlib.sha256(_fake_wheel(name, version)).hexdigest()}\n"
                for name, version in sorted(fixture_audalign_versions.items())
            ),
            encoding="utf-8",
        )
        audalign_notice = tmp_path / "fixture-catalog/locks/audalign-notices.json"
        audalign_notice.write_text(
            json.dumps(
                {
                    name: {
                        "dist_info": f"{name}-{version}.dist-info",
                        "license_files": [],
                        "payloads": {},
                    }
                    for name, version in sorted(fixture_audalign_versions.items())
                }
            ),
            encoding="utf-8",
        )
        alignment_groups = (
            AlignmentGroupSpec(
                provider="audalign",
                plan_group_name=AUDALIGN_GROUP_NAME,
                managed_record_name="audalign",
                managed_dir="audalign",
                direct_distribution="audalign",
                version="1.3.1",
                upstream_commit="d5955ae8a85b1cd480dadd005c3f88986f4ebbef",
                origin="https://pypi.org/project/audalign/1.3.1/",
                record_license="MIT",
                artifacts=tuple(audalign_artifacts),
                dependency_lock=audalign_lock,
                dependency_lock_sha256=hashlib.sha256(
                    audalign_lock.read_bytes()
                ).hexdigest(),
                dependency_lock_origin="fixture://lock",
                license_notice_file=audalign_notice,
                license_notice_sha256=hashlib.sha256(
                    audalign_notice.read_bytes()
                ).hexdigest(),
                estimated_installed_bytes=1,
                distributions=tuple(sorted(fixture_audalign_versions.items())),
            ),
        )
    if with_bbc_group:
        from roughcut.adapters.runtime_binding import (
            bbc_audio_offset_finder_distribution_versions_for as fixture_bbc_versions_for,
        )

        fixture_bbc_versions = fixture_bbc_versions_for(current_platform())
        bbc_artifacts: list[ArtifactSpec] = []
        for name, version in sorted(fixture_bbc_versions.items()):
            filename = f"{name.replace('-', '_')}-{version}-py3-none-any.whl"
            payload = _fake_wheel(name, version)
            path = f"/bbc-{filename}"
            payloads[path] = payload
            bbc_artifacts.append(
                ArtifactSpec(
                    component=BBC_AUDIO_OFFSET_FINDER_GROUP_NAME,
                    name=name,
                    version=version,
                    filename=filename,
                    url=f"{base_url}{path}",
                    license="fixture-only",
                    sha256=hashlib.sha256(payload).hexdigest(),
                    size=len(payload),
                )
            )
        bbc_lock = tmp_path / "fixture-catalog/locks/bbc.lock"
        bbc_lock.parent.mkdir(parents=True, exist_ok=True)
        bbc_lock.write_text(
            "".join(
                f"{name}=={version} \\\n"
                f"    --hash=sha256:{hashlib.sha256(_fake_wheel(name, version)).hexdigest()}\n"
                for name, version in sorted(fixture_bbc_versions.items())
            ),
            encoding="utf-8",
        )
        bbc_notice = tmp_path / "fixture-catalog/locks/bbc-notices.json"
        bbc_notice.write_text(
            json.dumps(
                {
                    name: {
                        "dist_info": f"{name}-{version}.dist-info",
                        "license_files": [],
                        "payloads": {},
                    }
                    for name, version in sorted(fixture_bbc_versions.items())
                }
            ),
            encoding="utf-8",
        )
        alignment_groups = (
            *alignment_groups,
            AlignmentGroupSpec(
                provider="bbc_audio_offset_finder",
                plan_group_name=BBC_AUDIO_OFFSET_FINDER_GROUP_NAME,
                managed_record_name=BBC_AUDIO_OFFSET_FINDER_GROUP_NAME,
                managed_dir=BBC_AUDIO_OFFSET_FINDER_GROUP_NAME,
                direct_distribution="audio-offset-finder",
                version="0.5.5",
                upstream_commit=None,
                origin="https://pypi.org/project/audio-offset-finder/0.5.5/",
                record_license="Apache-2.0",
                artifacts=tuple(bbc_artifacts),
                dependency_lock=bbc_lock,
                dependency_lock_sha256=hashlib.sha256(
                    bbc_lock.read_bytes()
                ).hexdigest(),
                dependency_lock_origin="fixture://lock",
                license_notice_file=bbc_notice,
                license_notice_sha256=hashlib.sha256(
                    bbc_notice.read_bytes()
                ).hexdigest(),
                estimated_installed_bytes=1,
                distributions=tuple(sorted(fixture_bbc_versions.items())),
            ),
        )

    profile = ReleaseProfile(
        id="fixture-macos-arm64-py311",
        platform=current_platform(),
        architecture=current_architecture(),
        python_version="3.11",
        runtime=RuntimeSpec(
            versions=versions,
            artifacts=tuple(runtime_artifacts),
            dependency_lock=lock,
            dependency_lock_sha256=hashlib.sha256(lock.read_bytes()).hexdigest(),
            dependency_lock_origin="fixture://lock",
            estimated_installed_bytes=1,
        ),
        models=models,
        alignment_groups=alignment_groups,
        ffmpeg=FFmpegAction(
            manager="fixture",
            package_id="fixture.ffmpeg",
            command=("fixture", "install"),
            source="https://example.invalid/ffmpeg",
            license="fixture-only",
            note="fixture only",
        ),
    )
    catalog = ReleaseCatalog(
        version="fixture-1",
        digest="f" * 64,
        profiles=(profile,),
        external_profiles=(
            ExternalRuntimeProfile(
                id="fixture-external-138",
                python_version="3.11",
                versions={
                    "funasr": "1.3.8",
                    "torch": "2.12.0",
                    "torchaudio": "2.11.0",
                },
                device="cpu",
                platforms=((current_platform(), current_architecture()),),
            ),
        ),
    )
    return catalog, payloads


def _fixture_catalog_with_platform_bbc_soundfile(
    catalog: ReleaseCatalog,
    tmp_path: Path,
    base_url: str,
    payloads: dict[str, bytes],
) -> ReleaseCatalog:
    profile = catalog.profiles[0]
    spec = profile.alignment_group(BBC_AUDIO_OFFSET_FINDER_GROUP_NAME)
    assert spec is not None
    old_soundfile = next(
        artifact for artifact in spec.artifacts if artifact.name == "soundfile"
    )
    platform_filename = (
        "soundfile-0.14.0-py2.py3-none-macosx_11_0_arm64.whl"
        if profile.platform == "macos"
        else "soundfile-0.14.0-py2.py3-none-win_amd64.whl"
    )
    platform_payload = _fake_wheel("soundfile", old_soundfile.version)
    platform_path = f"/bbc-{platform_filename}"
    payloads[platform_path] = platform_payload
    platform_artifact = replace(
        old_soundfile,
        filename=platform_filename,
        url=f"{base_url}{platform_path}",
        sha256=hashlib.sha256(platform_payload).hexdigest(),
        size=len(platform_payload),
    )
    old_hash = old_soundfile.sha256
    new_hash = platform_artifact.sha256
    lock = tmp_path / "fixture-catalog/locks/bbc-platform.lock"
    lock.write_text(
        spec.dependency_lock.read_text(encoding="utf-8").replace(old_hash, new_hash),
        encoding="utf-8",
    )
    new_spec = replace(
        spec,
        artifacts=tuple(
            platform_artifact if artifact.name == "soundfile" else artifact
            for artifact in spec.artifacts
        ),
        dependency_lock=lock,
        dependency_lock_sha256=hashlib.sha256(lock.read_bytes()).hexdigest(),
    )
    new_profile = replace(
        profile,
        alignment_groups=tuple(
            new_spec if group.plan_group_name == BBC_AUDIO_OFFSET_FINDER_GROUP_NAME else group
            for group in profile.alignment_groups
        ),
    )
    return replace(catalog, digest="e" * 64, profiles=(new_profile,))


def _write_executable(path: Path, body: str, *, windows_body: str | None = None) -> Path:
    if sys.platform == "win32":
        path = path.with_suffix(".cmd")
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        path.write_text(
            f"@echo off\n{windows_body if windows_body is not None else body}\n",
            encoding="utf-8",
        )
    else:
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _write_external_python(path: Path, *, funasr: str = "1.3.8") -> Path:
    payload = json.dumps(
        {
            "python_version": "3.11",
            "funasr": funasr,
            "torch": "2.12.0",
            "torchaudio": "2.11.0",
            "cuda_version": None,
            "cuda_available": False,
        },
        separators=(",", ":"),
    )
    return _write_executable(
        path,
        "test \"$1\" = -I || exit 21\n"
        "test -z \"${PYTHONPATH:-}\" || exit 22\n"
        "test -z \"${PYTHONHOME:-}\" || exit 23\n"
        f"printf 'ROUGHCUT-PROBE/1 %s\\n' '{payload}'",
        windows_body=(
            'if not "%~1"=="-I" exit /b 21\n'
            "if defined PYTHONPATH exit /b 22\n"
            "if defined PYTHONHOME exit /b 23\n"
            f"echo ROUGHCUT-PROBE/1 {payload}"
        ),
    )


def _write_external_models(
    root: Path,
    *,
    names: tuple[str, ...] = MODEL_COMPONENT_NAMES,
    models: dict[str, ModelSpec] | None = None,
) -> Path:
    records = []
    expected_models = models or load_release_catalog().profile_for("macos", "arm64").models
    for name in names:
        model = root / "models" / name
        _write_model_runtime_files(model, name)
        record = build_external_component(
                name,
                model,
                version=expected_models[name].revision,
                origin=f"https://modelscope.cn/models/iic/{name}",
                license="Apache-2.0",
            )
        records.append(
            replace(
                record,
                verification=ComponentVerification(
                    "sha256", expected_models[name].directory_sha256
                ),
            )
        )
    manifest = ComponentManifest(
        components=tuple(records),
        platform=current_platform(),
        architecture=current_architecture(),
    )
    path = root / "external-models.json"
    write_component_manifest(path, manifest)
    return path


def _write_legacy_external_models(
    root: Path,
    *,
    names: tuple[str, ...] = MODEL_COMPONENT_NAMES,
    version_overrides: dict[str, str] | None = None,
    digest_overrides: dict[str, str] | None = None,
    origin_overrides: dict[str, str] | None = None,
    license_overrides: dict[str, str] | None = None,
    schema_version: int = 1,
) -> Path:
    profile = load_release_catalog().profile_for("macos", "arm64")
    records = []
    for name in names:
        model = root / "models" / name
        _write_model_runtime_files(model, name)
        catalog_model = profile.models[name]
        legacy = name in LEGACY_EXTERNAL_MODEL_DIGESTS
        version = (
            LEGACY_EXTERNAL_MODEL_VERSION if legacy else catalog_model.revision
        )
        digest = (
            LEGACY_EXTERNAL_MODEL_DIGESTS[name]
            if legacy
            else catalog_model.directory_sha256
        )
        origin = (
            LEGACY_EXTERNAL_MODEL_ORIGINS[name]
            if legacy
            else catalog_model.repository
        )
        record = build_external_component(
            name,
            model,
            version=(version_overrides or {}).get(name, version),
            origin=(origin_overrides or {}).get(name, origin),
            license=(license_overrides or {}).get(name, catalog_model.license),
            platform="macos",
            architecture="arm64",
        )
        records.append(
            replace(
                record,
                verification=ComponentVerification(
                    "sha256", (digest_overrides or {}).get(name, digest)
                ),
            )
        )
    manifest = ComponentManifest(
        components=tuple(records),
        platform="macos",
        architecture="arm64",
        schema_version=schema_version,
    )
    path = root / "legacy-external-models.json"
    write_component_manifest(path, manifest)
    return path


def _write_media_tools(root: Path) -> tuple[Path, Path]:
    script = root / "tools/fake_media_tool.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        """from __future__ import annotations

import json
import sys
from pathlib import Path

tool = sys.argv[1]
arguments = sys.argv[2:]
if arguments == [\"-version\"]:
    print(f\"{tool} version 8.1.1-fixture\")
elif tool == \"ffmpeg\" and arguments == [\"-hide_banner\", \"-h\", \"full\"]:
    print(\"-filter_complex <graph_description>\")
elif tool == \"ffmpeg\" and arguments == [\"-hide_banner\", \"-encoders\"]:
    print(\" V....D libx264 fixture\")
    print(\" A....D aac fixture\")
elif tool == \"ffmpeg\":
    Path(arguments[-1]).write_bytes(b\"fixture-mp4\")
elif tool == \"ffprobe\":
    print(json.dumps({
        \"streams\": [
            {\"codec_type\": \"video\", \"codec_name\": \"h264\"},
            {\"codec_type\": \"audio\", \"codec_name\": \"aac\"},
        ],
        \"format\": {\"duration\": \"0.200000\"},
        \"programs\": [],
        \"stream_groups\": [],
    }, separators=(\",\", \":\")))
else:
    raise SystemExit(2)
""",
        encoding="utf-8",
    )
    python = str(Path(sys.executable).resolve())
    ffmpeg = _write_executable(
        root / "tools/ffmpeg",
        f'PYTHONHOME= PYTHONPATH= exec "{python}" "{script}" ffmpeg "$@"',
        windows_body=(
            f'set "PYTHONHOME="\nset "PYTHONPATH="\n"{python}" "{script}" ffmpeg %*'
        ),
    )
    ffprobe = _write_executable(
        root / "tools/ffprobe",
        f'PYTHONHOME= PYTHONPATH= exec "{python}" "{script}" ffprobe "$@"',
        windows_body=(
            f'set "PYTHONHOME="\nset "PYTHONPATH="\n"{python}" "{script}" ffprobe %*'
        ),
    )
    return ffmpeg, ffprobe


def test_media_tool_fixture_is_selected_quick_and_full_without_path_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ffmpeg, ffprobe = _write_media_tools(tmp_path)
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    monkeypatch.setenv("PATH", str(empty_path))

    for verify_components, expected_mode in ((False, "quick"), (True, "full")):
        plan = build_install_plan(
            tmp_path / f"managed-{expected_mode}",
            tmp_path / f"cache-{expected_mode}",
            platform=current_platform(),
            architecture=current_architecture(),
            ffmpeg_command=str(ffmpeg),
            ffprobe_command=str(ffprobe),
            verify_components=verify_components,
        )
        components = plan.payload["components"]
        assert isinstance(components, dict)
        assert plan.payload["verification_mode"] == expected_mode
        assert components["ffmpeg"]["status"] == "available"
        assert components["ffprobe"]["status"] == "available"
        assert components["ffmpeg"]["selected_source"] == "external"
        assert components["ffprobe"]["selected_source"] == "external"


@pytest.mark.parametrize("verify_components", [False, True])
def test_partial_media_pair_does_not_publish_an_external_selection(
    tmp_path: Path, verify_components: bool
) -> None:
    _ffmpeg, ffprobe = _write_media_tools(tmp_path)

    plan = build_install_plan(
        tmp_path / "managed",
        tmp_path / "cache",
        platform=current_platform(),
        architecture=current_architecture(),
        ffmpeg_command=str(tmp_path / "missing-ffmpeg"),
        ffprobe_command=str(ffprobe),
        verify_components=verify_components,
    )

    components = plan.payload["components"]
    assert isinstance(components, dict)
    assert components["ffmpeg"]["status"] == "missing"
    assert components["ffprobe"]["status"] == "unavailable"
    assert components["ffmpeg"]["selected_source"] is None
    assert components["ffprobe"]["selected_source"] is None


def _symlink_or_skip(link: Path, target: Path, *, target_is_directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except OSError as error:
        if sys.platform == "win32" and error.winerror == 1314:
            pytest.skip("Windows symlink creation requires Developer Mode or elevation")
        raise


def _managed_interpreter_path(managed_root: Path) -> Path:
    relative = "venv/Scripts/python.exe" if sys.platform == "win32" else "venv/bin/python"
    return managed_root / relative


def _external_probe_result(*, funasr: str = "1.3.8") -> dict[str, object]:
    return {
        "python_version": "3.11",
        "funasr": funasr,
        "torch": "2.12.0",
        "torchaudio": "2.11.0",
        "cuda_version": None,
        "cuda_available": False,
    }


def _stable_file_identity(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _owned_bbc_publish_fixture(
    tmp_path: Path,
) -> tuple[Path, ReleaseProfile, Path, Path, bytes]:
    catalog, _payloads = _fixture_catalog(
        tmp_path,
        "https://example.invalid",
        with_bbc_group=True,
    )
    profile = catalog.profiles[0]
    spec = profile.alignment_group(BBC_AUDIO_OFFSET_FINDER_GROUP_NAME)
    assert spec is not None
    managed = tmp_path / "managed"
    destination = managed / spec.managed_dir
    receipt = destination / "venv-receipt.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text('{"marker":"old"}\n', encoding="utf-8")
    old_record = ComponentRecord(
        name=spec.managed_record_name,
        kind="python_package",
        source_type="managed",
        origin=spec.origin,
        version="0.4.0",
        path=f"{spec.managed_dir}/venv-receipt.json",
        platform=profile.platform,
        architecture=profile.architecture,
        license=spec.record_license,
        verification=ComponentVerification("sha256", component_digest(receipt)),
    )
    manifest_path = managed / "component-manifest.json"
    write_component_manifest(
        manifest_path,
        ComponentManifest(
            components=(old_record,),
            platform=profile.platform,
            architecture=profile.architecture,
            managed_root=str(managed.resolve()),
            schema_version=1,
        ),
    )
    return managed, profile, manifest_path, destination, manifest_path.read_bytes()


def _patch_fake_bbc_stage(
    monkeypatch: pytest.MonkeyPatch,
    *,
    marker: str = "new",
) -> None:
    def stage(
        staging: Path,
        _cache: Path,
        _profile: ReleaseProfile,
        spec: AlignmentGroupSpec,
        *,
        python_executable: Path,
    ) -> tuple[list[ComponentRecord], list[PurePosixPath]]:
        del python_executable
        receipt = staging / spec.managed_dir / "venv-receipt.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(
            json.dumps({"marker": marker}, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        return [
            ComponentRecord(
                name=spec.managed_record_name,
                kind="python_package",
                source_type="managed",
                origin=spec.origin,
                version=spec.version,
                path=f"{spec.managed_dir}/venv-receipt.json",
                platform=_profile.platform,
                architecture=_profile.architecture,
                license=spec.record_license,
                verification=ComponentVerification(
                    "sha256", component_digest(receipt)
                ),
            )
        ], [PurePosixPath(spec.managed_dir)]

    monkeypatch.setattr(component_installation, "_stage_alignment_group", stage)
    monkeypatch.setattr(
        component_installation,
        "_validate_staged_groups",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        component_installation,
        "_verify_published_manifest",
        lambda *_args, **_kwargs: None,
    )


def _managed_backup_paths(root: Path) -> list[Path]:
    return sorted(
        root.rglob(f"{component_installation.MANAGED_BACKUP_PREFIX}*")
    )


def test_owned_stale_group_success_swaps_new_tree_and_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed, profile, manifest_path, destination, _old_manifest = (
        _owned_bbc_publish_fixture(tmp_path)
    )
    _patch_fake_bbc_stage(monkeypatch)

    installed = component_installation._install_staged_components(
        managed,
        tmp_path / "cache",
        profile,
        (BBC_AUDIO_OFFSET_FINDER_GROUP_NAME,),
        python_executable=Path(sys.executable),
        ffmpeg_command="fixture-ffmpeg",
    )

    assert json.loads((destination / "venv-receipt.json").read_text()) == {
        "marker": "new"
    }
    assert installed.components[0].version == "0.5.5"
    assert load_component_manifest(manifest_path).components[0].version == "0.5.5"
    assert not _managed_backup_paths(managed)
    assert not list(tmp_path.glob(f"{component_installation.MANAGED_STAGING_PREFIX}*"))


def test_owned_stale_group_rollback_restores_old_destination_after_new_publish_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed, profile, manifest_path, destination, old_manifest = (
        _owned_bbc_publish_fixture(tmp_path)
    )
    _patch_fake_bbc_stage(monkeypatch)
    real_replace = component_installation.os.replace
    attempted_new_publish = False

    def fail_new_publish(source: object, target: object) -> None:
        nonlocal attempted_new_publish
        source_path = Path(source)
        target_path = Path(target)
        if (
            target_path == destination
            and any(
                part.startswith(component_installation.MANAGED_STAGING_PREFIX)
                for part in source_path.parts
            )
        ):
            attempted_new_publish = True
            raise OSError("injected staged publish failure")
        real_replace(source, target)

    monkeypatch.setattr(component_installation.os, "replace", fail_new_publish)
    with pytest.raises(ComponentInstallError):
        component_installation._install_staged_components(
            managed,
            tmp_path / "cache",
            profile,
            (BBC_AUDIO_OFFSET_FINDER_GROUP_NAME,),
            python_executable=Path(sys.executable),
            ffmpeg_command="fixture-ffmpeg",
        )

    assert attempted_new_publish is True
    assert json.loads((destination / "venv-receipt.json").read_text()) == {
        "marker": "old"
    }
    assert manifest_path.read_bytes() == old_manifest
    assert not _managed_backup_paths(managed)
    assert not list(tmp_path.glob(f"{component_installation.MANAGED_STAGING_PREFIX}*"))


def test_owned_stale_group_rollback_restores_old_tree_after_manifest_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed, profile, manifest_path, destination, old_manifest = (
        _owned_bbc_publish_fixture(tmp_path)
    )
    _patch_fake_bbc_stage(monkeypatch)
    real_replace = component_installation.os.replace
    manifest_write_attempted = False

    def fail_manifest_replace(source: object, target: object) -> None:
        nonlocal manifest_write_attempted
        if Path(target) == manifest_path and not manifest_write_attempted:
            manifest_write_attempted = True
            raise OSError("injected manifest write failure")
        real_replace(source, target)

    monkeypatch.setattr(component_installation.os, "replace", fail_manifest_replace)
    with pytest.raises(ComponentInstallError):
        component_installation._install_staged_components(
            managed,
            tmp_path / "cache",
            profile,
            (BBC_AUDIO_OFFSET_FINDER_GROUP_NAME,),
            python_executable=Path(sys.executable),
            ffmpeg_command="fixture-ffmpeg",
        )

    assert manifest_write_attempted is True
    assert json.loads((destination / "venv-receipt.json").read_text()) == {
        "marker": "old"
    }
    assert manifest_path.read_bytes() == old_manifest
    assert not _managed_backup_paths(managed)
    assert not list(tmp_path.glob(f"{component_installation.MANAGED_STAGING_PREFIX}*"))


def test_multiple_owned_groups_rollback_in_reverse_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog, _payloads = _fixture_catalog(tmp_path, "https://example.invalid")
    profile = catalog.profiles[0]
    managed = tmp_path / "managed"
    records: list[ComponentRecord] = []
    for name in ("model_asr", "model_vad"):
        destination = managed / "models" / name
        destination.mkdir(parents=True)
        for required in component_environment.MODEL_RUNTIME_FILES[name]:
            (destination / required).write_text(f"old-{name} {required}", encoding="utf-8")
        records.append(
            ComponentRecord(
                name=name,
                kind="model",
                source_type="managed",
                origin="fixture://old-model",
                version="old",
                path=f"models/{name}",
                platform=profile.platform,
                architecture=profile.architecture,
                license="Apache-2.0",
                verification=ComponentVerification(
                    "sha256", component_digest(destination)
                ),
            )
        )
    manifest_path = managed / "component-manifest.json"
    write_component_manifest(
        manifest_path,
        ComponentManifest(
            components=tuple(records),
            platform=profile.platform,
            architecture=profile.architecture,
            managed_root=str(managed.resolve()),
            schema_version=1,
        ),
    )
    old_manifest = manifest_path.read_bytes()

    def stage_model(
        staging: Path,
        _cache: Path,
        selected_profile: ReleaseProfile,
        name: str,
    ) -> ComponentRecord:
        destination = staging / "models" / name
        destination.mkdir(parents=True)
        for required in component_environment.MODEL_RUNTIME_FILES[name]:
            (destination / required).write_text(f"new-{name} {required}", encoding="utf-8")
        model = selected_profile.models[name]
        return ComponentRecord(
            name=name,
            kind="model",
            source_type="managed",
            origin="fixture://new-model",
            version=model.revision,
            path=f"models/{name}",
            platform=selected_profile.platform,
            architecture=selected_profile.architecture,
            license="Apache-2.0",
            verification=ComponentVerification(
                "sha256", component_digest(destination)
            ),
        )

    monkeypatch.setattr(component_installation, "_stage_model", stage_model)
    monkeypatch.setattr(
        component_installation,
        "_validate_staged_groups",
        lambda *_args, **_kwargs: None,
    )
    verified = False

    def verify_published_manifest(_manifest: object, _profile: object) -> None:
        nonlocal verified
        verified = True

    monkeypatch.setattr(
        component_installation,
        "_verify_published_manifest",
        verify_published_manifest,
    )
    real_replace = component_installation.os.replace
    attempted_second_publish = False
    second_destination = managed / "models/model_vad"

    def fail_second_publish(source: object, target: object) -> None:
        nonlocal attempted_second_publish
        source_path = Path(source)
        if (
            Path(target) == second_destination
            and any(
                part.startswith(component_installation.MANAGED_STAGING_PREFIX)
                for part in source_path.parts
            )
        ):
            attempted_second_publish = True
            raise OSError("injected second group publish failure")
        real_replace(source, target)

    monkeypatch.setattr(component_installation.os, "replace", fail_second_publish)
    with pytest.raises(ComponentInstallError):
        component_installation._install_staged_components(
            managed,
            tmp_path / "cache",
            profile,
            ("model_asr", "model_vad"),
            python_executable=Path(sys.executable),
            ffmpeg_command=None,
        )

    assert attempted_second_publish is True
    assert (managed / "models/model_asr/model.pt").read_text() == "old-model_asr model.pt"
    assert (managed / "models/model_vad/model.pt").read_text() == "old-model_vad model.pt"
    assert manifest_path.read_bytes() == old_manifest
    assert not _managed_backup_paths(managed)
    assert not list(tmp_path.glob(f"{component_installation.MANAGED_STAGING_PREFIX}*"))


def test_committed_multi_group_backup_cleanup_failure_does_not_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog, _payloads = _fixture_catalog(tmp_path, "https://example.invalid")
    profile = catalog.profiles[0]
    managed = tmp_path / "managed"
    group_names = ("model_asr", "model_vad")
    records: list[ComponentRecord] = []
    for name in group_names:
        destination = managed / "models" / name
        destination.mkdir(parents=True)
        for required in component_environment.MODEL_RUNTIME_FILES[name]:
            (destination / required).write_text(f"old-{name} {required}", encoding="utf-8")
        records.append(
            ComponentRecord(
                name=name,
                kind="model",
                source_type="managed",
                origin="fixture://old-model",
                version="old",
                path=f"models/{name}",
                platform=profile.platform,
                architecture=profile.architecture,
                license="Apache-2.0",
                verification=ComponentVerification(
                    "sha256", component_digest(destination)
                ),
            )
        )
    manifest_path = managed / COMPONENT_MANIFEST_FILENAME
    write_component_manifest(
        manifest_path,
        ComponentManifest(
            components=tuple(records),
            platform=profile.platform,
            architecture=profile.architecture,
            managed_root=str(managed.resolve()),
            schema_version=1,
        ),
    )
    old_manifest = manifest_path.read_bytes()

    def stage_model(
        staging: Path,
        _cache: Path,
        selected_profile: ReleaseProfile,
        name: str,
    ) -> ComponentRecord:
        destination = staging / "models" / name
        destination.mkdir(parents=True)
        for required in component_environment.MODEL_RUNTIME_FILES[name]:
            (destination / required).write_text(f"new-{name} {required}", encoding="utf-8")
        model = selected_profile.models[name]
        return ComponentRecord(
            name=name,
            kind="model",
            source_type="managed",
            origin="fixture://new-model",
            version=model.revision,
            path=f"models/{name}",
            platform=selected_profile.platform,
            architecture=selected_profile.architecture,
            license="Apache-2.0",
            verification=ComponentVerification(
                "sha256", component_digest(destination)
            ),
        )

    monkeypatch.setattr(component_installation, "_stage_model", stage_model)
    monkeypatch.setattr(
        component_installation,
        "_validate_staged_groups",
        lambda *_args, **_kwargs: None,
    )
    verified = False

    def verify_published_manifest(_manifest: object, _profile: object) -> None:
        nonlocal verified
        verified = True

    monkeypatch.setattr(
        component_installation,
        "_verify_published_manifest",
        verify_published_manifest,
    )
    real_remove = component_installation._remove_owned_path
    removed_backups: list[Path] = []

    def fail_second_backup_cleanup(path: Path) -> None:
        if path.name.startswith(component_installation.MANAGED_BACKUP_PREFIX):
            removed_backups.append(path)
            if len(removed_backups) == 2:
                raise OSError("injected committed backup cleanup failure")
        real_remove(path)

    monkeypatch.setattr(
        component_installation, "_remove_owned_path", fail_second_backup_cleanup
    )

    installed = component_installation._install_staged_components(
        managed,
        tmp_path / "cache",
        profile,
        group_names,
        python_executable=Path(sys.executable),
        ffmpeg_command=None,
    )

    assert verified is True
    assert len(removed_backups) == 2
    assert all(path.parent == managed / "models" for path in removed_backups)
    assert not removed_backups[0].exists()
    assert removed_backups[1].exists()
    assert _managed_backup_paths(managed) == [removed_backups[1]]
    assert (
        (managed / "models/model_asr/model.pt").read_text() == "new-model_asr model.pt"
    )
    assert (
        (managed / "models/model_vad/model.pt").read_text() == "new-model_vad model.pt"
    )
    assert manifest_path.read_bytes() != old_manifest
    assert load_component_manifest(manifest_path) == installed


def test_existing_unowned_destination_remains_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed, profile, _manifest_path, destination, _old_manifest = (
        _owned_bbc_publish_fixture(tmp_path)
    )
    manifest_path = managed / "component-manifest.json"
    write_component_manifest(
        manifest_path,
        ComponentManifest(
            components=(),
            platform=profile.platform,
            architecture=profile.architecture,
            managed_root=str(managed.resolve()),
            schema_version=1,
        ),
    )
    (destination / "venv-receipt.json").write_text(
        '{"marker":"unowned"}\n', encoding="utf-8"
    )
    _patch_fake_bbc_stage(monkeypatch)

    with pytest.raises(ComponentInstallError, match="managed destination appeared before publish"):
        component_installation._install_staged_components(
            managed,
            tmp_path / "cache",
            profile,
            (BBC_AUDIO_OFFSET_FINDER_GROUP_NAME,),
            python_executable=Path(sys.executable),
            ffmpeg_command="fixture-ffmpeg",
        )

    assert json.loads((destination / "venv-receipt.json").read_text()) == {
        "marker": "unowned"
    }


def test_noncanonical_existing_runtime_lock_is_not_owned(tmp_path: Path) -> None:
    catalog, _payloads = _fixture_catalog(tmp_path, "https://fixture.invalid")
    profile = catalog.profiles[0]
    managed = tmp_path / "managed"
    records = tuple(
        ComponentRecord(
            name=name,
            kind="python_package",
            source_type="managed",
            origin="fixture://old-runtime",
            version=profile.runtime.versions[name],
            path=f"packages/{name}/receipt.json",
            platform=profile.platform,
            architecture=profile.architecture,
            license="fixture-only",
            verification=ComponentVerification("sha256", "a" * 64),
        )
        for name in ("funasr", "torch", "torchaudio")
    )
    runtime = PythonRuntimeRecord(
        root="venv",
        interpreter=(
            "venv/Scripts/python.exe"
            if profile.platform == "windows"
            else "venv/bin/python"
        ),
        dependency_lock="locks/not-approved.lock",
        lock_origin="fixture://old-runtime-lock",
        lock_verification=ComponentVerification("sha256", "b" * 64),
        device="cpu",
    )
    existing = ComponentManifest(
        components=records,
        platform=profile.platform,
        architecture=profile.architecture,
        managed_root=str(managed.resolve()),
        schema_version=2,
        python_runtime=runtime,
    )

    with pytest.raises(
        ComponentInstallError, match="managed destination appeared before publish"
    ):
        component_installation._managed_group_owned_paths(
            existing,
            profile,
            managed,
            ("python_runtime",),
        )


def test_existing_symlink_destination_remains_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed, profile, _manifest_path, destination, _old_manifest = (
        _owned_bbc_publish_fixture(tmp_path)
    )
    outside = tmp_path / "outside"
    outside.write_text("must remain", encoding="utf-8")
    shutil.rmtree(destination)
    _symlink_or_skip(destination, outside)
    _patch_fake_bbc_stage(monkeypatch)

    with pytest.raises(ComponentInstallError, match="managed destination appeared before publish"):
        component_installation._install_staged_components(
            managed,
            tmp_path / "cache",
            profile,
            (BBC_AUDIO_OFFSET_FINDER_GROUP_NAME,),
            python_executable=Path(sys.executable),
            ffmpeg_command="fixture-ffmpeg",
        )

    assert destination.is_symlink()
    assert outside.read_text(encoding="utf-8") == "must remain"


def test_existing_external_group_is_not_overwritten_by_managed_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed, profile, manifest_path, destination, _old_manifest = (
        _owned_bbc_publish_fixture(tmp_path)
    )
    manifest = load_component_manifest(manifest_path)
    record = manifest.components[0]
    external_record = replace(
        record,
        source_type="external",
        path=str(destination.resolve()),
    )
    write_component_manifest(
        manifest_path,
        replace(manifest, components=(external_record,)),
    )
    _patch_fake_bbc_stage(monkeypatch)

    with pytest.raises(ComponentInstallError, match="managed destination appeared before publish"):
        component_installation._install_staged_components(
            managed,
            tmp_path / "cache",
            profile,
            (BBC_AUDIO_OFFSET_FINDER_GROUP_NAME,),
            python_executable=Path(sys.executable),
            ffmpeg_command="fixture-ffmpeg",
        )

    assert json.loads((destination / "venv-receipt.json").read_text()) == {
        "marker": "old"
    }


def test_runtime_publish_wrapper_preserves_bounded_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = component_installation.ApplyResult(
        approved_plan_hash="a" * 64,
        installed_groups=(),
        reused=True,
        manifest_path=None,
        diagnostics={},
    )
    plan = component_installation.InstallPlan(payload={}, plan_hash="b" * 64)
    monkeypatch.setattr(
        component_installation,
        "binding_from_install_plan",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        component_installation,
        "runtime_binding_path",
        lambda **_kwargs: tmp_path / "install/runtime.json",
    )

    def fail_publish(*_args: object, **_kwargs: object) -> object:
        raise component_installation.RuntimeBindingError(
            "stale runtime plan",
            reason_code="runtime_publish_stale_plan",
        )

    monkeypatch.setattr(component_installation, "publish_runtime_binding", fail_publish)
    with pytest.raises(component_installation.RuntimePublicationError) as raised:
        component_installation._publish_apply_runtime(
            result,
            plan=plan,
            install_root=tmp_path / "install",
            external_manifest_path=None,
            approved_runtime_status={"status": "configured"},
        )

    assert raised.value.reason_code == "runtime_publish_stale_plan"
    assert raised.value.failure_reason == "runtime_publish_stale_plan"


@pytest.fixture(autouse=True)
def _windows_fixture_host_vc_runtime_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep synthetic fixture applies independent of the real host VC++ state.

    The fixture catalog is host-platform-bound; on a Windows host its profile is
    a windows-x64 target and engages the frozen Windows VC++ prerequisite with a
    live host probe. The plan/apply/rollback contract tests intentionally
    exercise the synthetic apply machinery, not the host prerequisite; the host
    prerequisite contract is covered by the dedicated probe tests in this file
    with explicit registry/DLL observation mocks. This autouse only patches the
    two OS read boundaries and only when the real host is Windows, so macOS
    behavior is unchanged and the dedicated probe tests keep overriding their
    own observations.
    """
    if component_environment.current_platform() != "windows":
        return
    monkeypatch.setattr(
        component_environment,
        "_read_windows_registry_version",
        lambda: "14.51.36247.0",
    )
    monkeypatch.setattr(
        component_environment,
        "_read_windows_dll_version",
        lambda _name: "14.51.36247.0",
    )


@pytest.fixture
def tmp_path(tmp_path: Path) -> Iterator[Path]:
    """Use a synthetic short scratch root on Windows.

    This is a synthetic short scratch root used to keep non-path-budget
    contract tests independent of pytest's deep temp layout. It is not a
    production root or a production-depth default-root witness. The formal
    Windows default root is `%USERPROFILE%\\.roughcut`
    (`Path.home() / ".roughcut"`). The independent
    `test_windows_path_budget_default_roots_pass_and_staging_is_limiting_witness`
    and `test_windows_path_budget_exact_259_passes_and_260_blocks` tests verify
    the default-root and 259/260 path-budget contract. This fixture uses a
    unique drive-level `C:\\rc\\<hash>` root with the same cleanup semantics;
    macOS/CI behavior is unchanged.
    """
    if sys.platform != "win32":
        yield tmp_path
        return
    base = Path((os.environ.get("SystemDrive") or "C:") + os.sep) / "rc"
    root = base / hashlib.sha256(str(tmp_path).encode("utf-8")).hexdigest()[:6]
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


def test_builtin_catalog_pins_exact_profiles_locks_models_and_licenses() -> None:
    catalog = load_release_catalog()
    macos = catalog.profile_for("macos", "arm64")
    windows = catalog.profile_for("windows", "x86_64")

    assert macos.runtime.versions == {
        "funasr": "1.3.14",
        "torch": "2.6.0",
        "torchaudio": "2.6.0",
    }
    assert windows.runtime.versions == {
        "funasr": "1.3.14",
        "torch": "2.6.0+cpu",
        "torchaudio": "2.6.0+cpu",
    }
    assert len(macos.runtime.artifacts) == 82
    assert len(windows.runtime.artifacts) == 83
    assert all(item.license and item.url.startswith("https://") for item in macos.runtime.artifacts)
    assert sum(model.size for model in macos.models.values()) == 2_218_711_106
    assert {model.revision for model in macos.models.values()} == set(MODEL_REVISIONS.values())
    assert macos.runtime.dependency_lock_sha256 == (
        "8b31a036a7c1ea997c21d7c61523710ccbb479a6b3007c878d594c93574fb5f4"
    )
    assert windows.runtime.dependency_lock_sha256 == (
        "0e02da5501304528b0b2eb1afe390bb4bac1dbb0efa27ad4d8ad7b3fd4ee22ea"
    )


def test_builtin_catalog_pins_exact_campplus_payloads_without_client_metadata() -> None:
    model = load_release_catalog().profile_for("macos", "arm64").models["model_spk"]

    assert model.repository == "iic/speech_campplus_sv_zh-cn_16k-common"
    assert model.revision == "v2.0.2"
    assert model.license == "Apache-2.0"
    assert model.size == 28_961_033
    assert model.directory_sha256 == (
        "b3c108deb1f66464fd53d31aa1fe0997f51c5cf2a51c269561ac01de17a88870"
    )
    assert len(model.artifacts) == 10
    destinations = {artifact.destination for artifact in model.artifacts}
    assert "campplus_cn_common.bin" in destinations
    assert ".mv" not in destinations
    assert ".msc" not in destinations
    assert sum(artifact.size for artifact in model.artifacts) == 28_961_033
    assert all("Revision=v2.0.2" in artifact.url for artifact in model.artifacts)


def test_campplus_external_digest_ignores_only_client_metadata(tmp_path: Path) -> None:
    external = tmp_path / "external CAM++"
    managed_shape = tmp_path / "managed shape"
    for root in (external, managed_shape):
        root.mkdir()
        (root / "campplus_cn_common.bin").write_bytes(b"speaker payload")
    (external / ".mv").write_text("Revision:v2.0.2", encoding="utf-8")
    (external / ".msc").write_bytes(b"client metadata")
    (external / "._____temp/examples").mkdir(parents=True)

    record = build_external_component(
        "model_spk",
        external,
        version="v2.0.2",
        origin="https://modelscope.cn/models/iic/speech_campplus_sv_zh-cn_16k-common",
        license="Apache-2.0",
    )

    assert record.verification.value == component_digest(managed_shape)
    assert component_record_digest("model_spk", external) == component_digest(managed_shape)


def test_repository_keeps_catalogs_byte_identical_and_hash_pinned() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    attributes = (repository_root / ".gitattributes").read_text(encoding="utf-8").splitlines()
    assert "core/src/roughcut/component_catalog/python/*.lock text eol=lf" in attributes
    assert "core/src/roughcut/component_catalog/audalign/* -text diff" in attributes
    attr_result = subprocess.run(
        [
            "git",
            "check-attr",
            "text",
            "diff",
            "--",
            *AUDALIGN_CATALOG_PATHS,
        ],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )
    assert attr_result.stdout.splitlines() == [
        line
        for path in AUDALIGN_CATALOG_PATHS
        for line in (f"{path}: text: unset", f"{path}: diff: set")
    ]

    lock_root = repository_root / "core/src/roughcut/component_catalog/python"
    for lock_path in lock_root.glob("*.lock"):
        assert b"\r" not in lock_path.read_bytes()
    for relative_path in AUDALIGN_CATALOG_PATHS:
        catalog_path = repository_root / relative_path
        working_tree_bytes = catalog_path.read_bytes()
        index_bytes = subprocess.run(
            ["git", "show", f":{relative_path}"],
            cwd=repository_root,
            check=True,
            capture_output=True,
        ).stdout
        assert working_tree_bytes == index_bytes
        assert hashlib.sha256(working_tree_bytes).hexdigest() == AUDALIGN_CATALOG_SHA256[
            relative_path
        ]


def test_catalog_digest_tracks_raw_bytes_without_crlf_normalization(
    tmp_path: Path,
) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    catalog_root = tmp_path / "catalog"
    shutil.copytree(repository_root / "core/src/roughcut/component_catalog", catalog_root)
    original = load_release_catalog(catalog_root)
    artifacts_path = catalog_root / (
        "audalign/audalign-macos-arm64-py311-artifacts.json"
    )
    original_bytes = artifacts_path.read_bytes()
    artifacts_path.write_bytes(original_bytes.replace(b"\n", b"\r\n"))

    crlf_catalog = load_release_catalog(catalog_root)

    assert crlf_catalog.digest != original.digest


def test_default_plan_is_read_only_stable_and_reports_all_exact_artifacts(
    tmp_path: Path,
) -> None:
    managed = tmp_path / "never-created-managed"
    cache = tmp_path / "never-created-cache"

    first = build_install_plan(
        managed,
        cache,
        ffmpeg_command="roughcut-missing-ffmpeg",
        ffprobe_command="roughcut-missing-ffprobe",
        platform="macos",
        architecture="arm64",
    )
    second = build_install_plan(
        managed,
        cache,
        ffmpeg_command="roughcut-missing-ffmpeg",
        ffprobe_command="roughcut-missing-ffprobe",
        platform="macos",
        architecture="arm64",
    )

    assert first == second
    assert first.plan_hash == second.plan_hash
    assert not managed.exists()
    assert not cache.exists()
    payload = first.to_dict()
    assert payload["missing_managed_groups"] == [
        "python_runtime",
        "model_asr",
        "model_vad",
        "model_punc",
        "model_spk",
    ]
    artifacts = payload["artifacts"]
    assert isinstance(artifacts, list)
    assert len(artifacts) == 82 + 13 + 8 + 10 + 10
    assert all(
        set(item)
        >= {
            "version",
            "source",
            "license",
            "sha256",
            "size",
            "cache_status",
        }
        for item in artifacts
    )
    assert payload["download_bytes"] == 219_883_936 + 2_218_711_106
    assert payload["prerequisites"] == {}
    assert payload["path_budget"] == {}
    assert payload["user_actions"] == [
        {
            "component": "ffmpeg_ffprobe",
            "status": "user_action_required",
            "blocking": False,
            **load_release_catalog().profile_for("macos", "arm64").ffmpeg.to_dict(),
        }
    ]


def test_plan_reports_runtime_target_without_writing_binding(tmp_path: Path) -> None:
    install_root = tmp_path / "安装 根"
    plan = build_install_plan(
        tmp_path / "managed",
        tmp_path / "cache",
        install_root=install_root,
        ffmpeg_command="roughcut-missing-ffmpeg",
        ffprobe_command="roughcut-missing-ffprobe",
        platform="macos",
        architecture="arm64",
        verify_components=True,
    )

    runtime = plan.payload["runtime_binding"]
    assert runtime == {
        "path": str(install_root.resolve() / "runtime.json"),
        "configured": False,
        "status": "unconfigured",
    }
    assert not install_root.exists()


@pytest.mark.skipif(
    current_platform() != "macos" or current_architecture() != "arm64",
    reason="the legacy external compatibility profile is macOS arm64 only",
)
def test_exact_legacy_external_triplet_quick_and_full_reuse_without_model_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    external_python = _write_external_python(tmp_path / "external/bin/python")
    external_models = _write_legacy_external_models(tmp_path / "external models")
    ffmpeg, ffprobe = _write_media_tools(tmp_path)
    monkeypatch.setattr(
        component_installation,
        "probe_external_python",
        lambda _path: _external_probe_result(),
    )
    arguments = {
        "external_manifest_path": external_models,
        "external_python": external_python,
        "platform": "macos",
        "architecture": "arm64",
        "ffmpeg_command": str(ffmpeg),
        "ffprobe_command": str(ffprobe),
    }

    quick = build_install_plan(
        tmp_path / "managed",
        tmp_path / "cache",
        **arguments,
    )

    quick_components = quick.payload["components"]
    assert isinstance(quick_components, dict)
    assert all(
        quick_components[name]["selected_source"] == "external"
        for name in ("model_asr", "model_vad", "model_punc")
    )
    assert quick.payload["missing_managed_groups"] == []
    assert quick.payload["artifacts"] == []
    assert quick.payload["download_bytes"] == 0
    assert quick.payload["estimated_installed_bytes"] == 0

    expected_digests = {
        **LEGACY_EXTERNAL_MODEL_DIGESTS,
        "model_spk": load_release_catalog()
        .profile_for("macos", "arm64")
        .models["model_spk"]
        .directory_sha256,
    }
    digest_calls: list[str] = []

    def fixture_component_digest(name: str, _path: Path) -> str:
        digest_calls.append(name)
        return expected_digests[name]

    monkeypatch.setattr(
        component_environment,
        "component_record_digest",
        fixture_component_digest,
    )
    full = build_install_plan(
        tmp_path / "managed",
        tmp_path / "cache",
        verify_components=True,
        **arguments,
    )

    full_components = full.payload["components"]
    assert isinstance(full_components, dict)
    assert all(
        full_components[name]["selected_source"] == "external"
        for name in MODEL_COMPONENT_NAMES
    )
    assert set(digest_calls) == set(MODEL_COMPONENT_NAMES)
    assert full.payload["missing_managed_groups"] == []
    assert full.payload["artifacts"] == []
    assert full.payload["download_bytes"] == 0
    assert full.payload["estimated_installed_bytes"] == 0


@pytest.mark.skipif(
    current_platform() != "macos" or current_architecture() != "arm64",
    reason="the frozen audalign catalog is macOS arm64 only",
)
def test_repository_catalog_full_plan_selects_exact_closed_audalign_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    external_python = _write_external_python(tmp_path / "external/bin/python")
    external_models = _write_legacy_external_models(tmp_path / "external models")
    ffmpeg, ffprobe = _write_media_tools(tmp_path)
    monkeypatch.setattr(
        component_installation,
        "probe_external_python",
        lambda _path: _external_probe_result(),
    )
    expected_digests = {
        **LEGACY_EXTERNAL_MODEL_DIGESTS,
        "model_spk": load_release_catalog()
        .profile_for("macos", "arm64")
        .models["model_spk"]
        .directory_sha256,
    }
    monkeypatch.setattr(
        component_environment,
        "component_record_digest",
        lambda name, _path: expected_digests[name],
    )

    plan = build_install_plan(
        tmp_path / "managed",
        tmp_path / "cache",
        external_manifest_path=external_models,
        external_python=external_python,
        platform="macos",
        architecture="arm64",
        ffmpeg_command=str(ffmpeg),
        ffprobe_command=str(ffprobe),
        verify_components=True,
        include_audalign=True,
    )

    artifacts = plan.payload["artifacts"]
    assert isinstance(artifacts, list)
    assert plan.payload["missing_managed_groups"] == [AUDALIGN_GROUP_NAME]
    assert len(artifacts) == 16
    assert {artifact["component"] for artifact in artifacts} == {
        AUDALIGN_GROUP_NAME
    }
    assert sum(artifact["size"] for artifact in artifacts) == 62_470_174
    assert all(artifact["name"] != "colorama" for artifact in artifacts)
    assert plan.payload["required_artifact_bytes"] == 62_470_174
    assert plan.payload["download_bytes"] == 62_470_174
    assert plan.payload["estimated_installed_bytes"] == 260_000_000
    assert plan.payload["estimated_additional_disk_bytes"] == 322_470_174
    assert plan.payload["audalign_fingerprint"] == {
        "available": False,
        "version": "1.3.1",
        "upstream_commit": "d5955ae8a85b1cd480dadd005c3f88986f4ebbef",
        "dependency_lock": {
            "filename": "audalign-macos-arm64-py311.lock",
            "source": "https://pypi.org/simple",
            "sha256": "3f1e90c9f4acdd12c62fc29641f7db2e6bb34b0b5a6d4dc1d0aed216f7eaaa81",
        },
        "license_notice_sha256": (
            "fc7254373ff8e5fcd24af22418d87f81f633c4e75eabc3db0596e57381e8cfc3"
        ),
        "distributions": [
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
        ],
        "estimated_installed_bytes": 260_000_000,
    }
    assert not (tmp_path / "managed").exists()
    assert not (tmp_path / "cache").exists()


def test_windows_catalog_full_plan_selects_exact_closed_audalign_group(
    tmp_path: Path,
) -> None:
    plan = build_install_plan(
        tmp_path / "managed",
        tmp_path / "cache",
        platform="windows",
        architecture="x86_64",
        ffmpeg_command="roughcut-missing-ffmpeg",
        ffprobe_command="roughcut-missing-ffprobe",
        verify_components=True,
        include_audalign=True,
    )

    artifacts = [
        artifact
        for artifact in plan.payload["artifacts"]
        if artifact["component"] == AUDALIGN_GROUP_NAME
    ]
    assert len(artifacts) == 17
    assert sum(artifact["size"] for artifact in artifacts) == 81_126_882
    assert plan.payload["required_artifact_bytes"] >= 81_126_882
    assert plan.payload["logical_artifact_count"] == 141
    assert plan.payload["unique_cache_identity_count"] == 138
    assert (
        plan.payload["logical_artifact_bytes"]
        - plan.payload["unique_cache_identity_bytes"]
        == 136_580
    )
    assert plan.payload["download_bytes"] == plan.payload["unique_cache_identity_bytes"]
    assert plan.payload["verified_cache_bytes"] == 0
    assert plan.payload["resumable_bytes"] == 0
    assert plan.payload["audalign_fingerprint"]["estimated_installed_bytes"] == 320_000_000
    assert plan.payload["catalog_hash"] == load_release_catalog().digest
    assert len(plan.plan_hash) == 64
    assert plan.to_dict()["plan_hash"] == plan.plan_hash
    distributions = plan.payload["audalign_fingerprint"]["distributions"]
    assert {item["name"] for item in distributions} == {
        "audalign",
        "contourpy",
        "cycler",
        "fonttools",
        "kiwisolver",
        "matplotlib",
        "numpy",
        "packaging",
        "pillow",
        "pydub",
        "pyparsing",
        "python-dateutil",
        "scipy",
        "setuptools",
        "six",
        "tqdm",
        "colorama",
    }
    assert any(item == {"name": "colorama", "version": "0.4.6"} for item in distributions)
    assert not (tmp_path / "managed").exists()
    assert not (tmp_path / "cache").exists()


def test_schema_one_approved_plan_hash_is_stale_under_schema_two(
    tmp_path: Path,
) -> None:
    plan = build_install_plan(
        tmp_path / "managed",
        tmp_path / "cache",
        ffmpeg_command="roughcut-missing-ffmpeg",
        ffprobe_command="roughcut-missing-ffprobe",
        verify_components=True,
    )
    legacy_payload = {**plan.payload, "schema_version": 1}
    legacy_plan = replace(
        plan,
        payload=legacy_payload,
        plan_hash=component_installation._canonical_hash(legacy_payload),
    )

    assert legacy_plan.plan_hash != plan.plan_hash
    with pytest.raises(StaleApprovedPlanError):
        validate_install_preflight(legacy_plan, legacy_plan.plan_hash)


@pytest.mark.parametrize("case", ["missing", "extra", "duplicate", "wrong_colorama"])
def test_windows_catalog_rejects_non_closed_audalign_distribution_sets(
    tmp_path: Path, case: str
) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    catalog_root = tmp_path / "catalog"
    shutil.copytree(repository_root / "core/src/roughcut/component_catalog", catalog_root)
    release_path = catalog_root / "release-catalog.json"
    release = json.loads(release_path.read_text(encoding="utf-8"))
    windows_profile = next(item for item in release["profiles"] if item["platform"] == "windows")
    artifacts_path = catalog_root / windows_profile["audalign"]["artifacts"]
    artifacts_catalog = json.loads(artifacts_path.read_text(encoding="utf-8"))
    artifacts = artifacts_catalog["artifacts"]
    if case == "missing":
        artifacts_catalog["artifacts"] = [item for item in artifacts if item["name"] != "colorama"]
    elif case == "extra":
        artifacts_catalog["artifacts"] = [
            *artifacts,
            {**artifacts[0], "name": "extra-wheel"},
        ]
    elif case == "duplicate":
        artifacts_catalog["artifacts"] = [
            dict(artifacts[0]),
            *artifacts[2:],
            dict(artifacts[0]),
        ]
    else:
        artifacts_catalog["artifacts"] = [
            {**item, "version": "0.4.5"} if item["name"] == "colorama" else item
            for item in artifacts
        ]
    artifacts_path.write_bytes(
        (json.dumps(artifacts_catalog, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
    )

    with pytest.raises(ComponentInstallError):
        load_release_catalog(catalog_root)


@pytest.mark.parametrize("field", ["dependency_lock", "license_notice"])
def test_windows_catalog_rejects_closed_notice_or_lock_digest_drift(
    tmp_path: Path, field: str
) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    catalog_root = tmp_path / "catalog"
    shutil.copytree(repository_root / "core/src/roughcut/component_catalog", catalog_root)
    release = json.loads(
        (catalog_root / "release-catalog.json").read_text(encoding="utf-8")
    )
    windows_profile = next(item for item in release["profiles"] if item["platform"] == "windows")
    relative = windows_profile["audalign"][field]
    (catalog_root / relative).write_bytes((catalog_root / relative).read_bytes() + b"\n")

    with pytest.raises(ComponentInstallError):
        load_release_catalog(catalog_root)


def test_windows_catalog_hash_closes_referenced_artifacts(tmp_path: Path) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    catalog_root = tmp_path / "catalog"
    shutil.copytree(repository_root / "core/src/roughcut/component_catalog", catalog_root)
    original = load_release_catalog()
    release = json.loads(
        (catalog_root / "release-catalog.json").read_text(encoding="utf-8")
    )
    windows_profile = next(item for item in release["profiles"] if item["platform"] == "windows")
    artifacts_path = catalog_root / windows_profile["audalign"]["artifacts"]
    artifacts_catalog = json.loads(artifacts_path.read_text(encoding="utf-8"))
    artifacts_catalog["artifacts"][0]["url"] += "?catalog-digest-test"
    artifacts_path.write_bytes(
        (json.dumps(artifacts_catalog, ensure_ascii=False, indent=1) + "\n").encode("utf-8")
    )

    mutated = load_release_catalog(catalog_root)

    assert mutated.digest != original.digest


def test_windows_path_budget_default_roots_pass_and_staging_is_limiting_witness() -> None:
    profile = load_release_catalog().profile_for("windows", "x86_64")
    budget = _build_path_budget(
        profile,
        install_root=Path("C:\\Users\\tester\\.roughcut\\install"),
        managed_root=Path("C:\\Users\\tester\\.roughcut\\managed"),
        cache_root=Path("C:\\Users\\tester\\.roughcut\\cache"),
        target_platform="windows",
        target_architecture="x86_64",
        host_platform="windows",
        host_architecture="x86_64",
    )

    assert all(item["status"] == "pass" for item in budget.values())
    managed = budget["managed"]
    assert managed["limiting_kind"] == "staging_runtime"
    assert managed["max_derived_length"] >= 230
    assert budget["install"]["max_derived_length"] < 260
    assert budget["cache"]["max_derived_length"] < 260


def test_windows_path_budget_exact_259_passes_and_260_blocks() -> None:
    profile = load_release_catalog().profile_for("windows", "x86_64")

    def budget_for_parent_length(length: int) -> dict[str, object]:
        managed = Path("C:\\" + "p" * length + "\\managed")
        return _build_path_budget(
            profile,
            install_root=Path("C:\\Users\\tester\\.roughcut\\install"),
            managed_root=managed,
            cache_root=Path("C:\\Users\\tester\\.roughcut\\cache"),
            target_platform="windows",
            target_architecture="x86_64",
            host_platform="windows",
            host_architecture="x86_64",
        )

    exact = budget_for_parent_length(40)
    over = budget_for_parent_length(41)
    assert exact["managed"] == {
        "max_derived_length": 259,
        "limiting_kind": "staging_runtime",
        "status": "pass",
    }
    assert over["managed"] == {
        "max_derived_length": 260,
        "limiting_kind": "staging_runtime",
        "status": "blocking",
    }


@pytest.mark.parametrize("case", ["unknown", "missing", "length", "sha", "filename"])
def test_windows_path_budget_evidence_is_closed_and_catalog_bound(
    tmp_path: Path, case: str
) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    catalog_root = tmp_path / "catalog"
    shutil.copytree(repository_root / "core/src/roughcut/component_catalog", catalog_root)
    release_path = catalog_root / "release-catalog.json"
    release = json.loads(release_path.read_text(encoding="utf-8"))
    windows_profile = next(item for item in release["profiles"] if item["platform"] == "windows")
    evidence = windows_profile["path_budget_evidence"]
    if case == "unknown":
        evidence["unknown"] = True
    elif case == "missing":
        del evidence["limit"]
    elif case == "length":
        evidence["runtime_staged_relative"]["characters"] += 1
    elif case == "sha":
        evidence["runtime_staged_relative"]["source_artifact_sha256"] = "0" * 64
    else:
        evidence["runtime_staged_relative"]["source_artifact_filename"] = "missing.whl"
    release_path.write_bytes(
        (json.dumps(release, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )

    with pytest.raises(ComponentInstallError):
        load_release_catalog(catalog_root)


def test_windows_plan_budget_never_downloads_artifact_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"download": 0, "urlopen": 0}

    def fail_download(*_args: object, **_kwargs: object) -> None:
        calls["download"] += 1
        raise AssertionError("plan must not download artifacts")

    def fail_urlopen(*_args: object, **_kwargs: object) -> None:
        calls["urlopen"] += 1
        raise AssertionError("plan must not open artifact URLs")

    monkeypatch.setattr(component_download, "download_artifact", fail_download)
    monkeypatch.setattr(component_download.urllib.request, "urlopen", fail_urlopen)
    monkeypatch.setattr(component_environment, "current_platform", lambda: "windows")
    monkeypatch.setattr(component_environment, "current_architecture", lambda: "x86_64")
    monkeypatch.setattr(
        component_environment,
        "probe_windows_vc_runtime",
        lambda: {
            "status": "ready",
            "observed": {
                "registry_version": "14.51.36247.0",
                "system32": {
                    "msvcp140.dll": "14.51.36247.0",
                    "vcruntime140.dll": "14.51.36247.0",
                    "vcruntime140_1.dll": "14.51.36247.0",
                },
            },
        },
    )

    plan = build_install_plan(
        Path("C:\\Users\\tester\\.roughcut\\managed"),
        Path("C:\\Users\\tester\\.roughcut\\cache"),
        install_root=Path("C:\\Users\\tester\\.roughcut\\install"),
        platform="windows",
        architecture="x86_64",
        ffmpeg_command="roughcut-missing-ffmpeg",
        ffprobe_command="roughcut-missing-ffprobe",
        verify_components=True,
        include_audalign=True,
    )

    assert calls == {"download": 0, "urlopen": 0}
    assert plan.payload["schema_version"] == 2
    assert plan.payload["path_budget"]["managed"]["status"] == "pass"
    assert plan.payload["prerequisites"]["windows_vc_runtime_x64"]["status"] == "ready"
    assert plan.payload["catalog_version"] == "2026-08-24.1"
    assert WINDOWS_PATH_EVIDENCE_SHA256 == (
        "19baca9a7083101e7edbf4f3871b7d6e5d6d384326133737334a88a455004e06"
    )


@pytest.mark.parametrize(
    ("status", "registry", "dll", "action"),
    [
        ("missing", None, None, "install"),
        ("outdated", "v14.50.0.0", "14.50.0.0", "upgrade"),
        ("registry_dll_mismatch", "14.51.36247.0", "14.51.36246.0", "repair_or_reinstall"),
        ("ready", "v14.51.36247.0", "14.51.36247.0", None),
    ],
)
def test_windows_vc_runtime_probe_and_action_union(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    registry: str | None,
    dll: str | None,
    action: str | None,
) -> None:
    monkeypatch.setattr(component_environment, "current_platform", lambda: "windows")
    monkeypatch.setattr(component_environment, "current_architecture", lambda: "x86_64")
    monkeypatch.setattr(
        component_environment,
        "_read_windows_registry_version",
        lambda: registry,
    )
    monkeypatch.setattr(
        component_environment,
        "_read_windows_dll_version",
        lambda _name: dll,
    )

    observation = component_environment.probe_windows_vc_runtime()
    assert observation["status"] == status
    observed = observation["observed"]
    assert observed["registry_version"] == (
        None if registry is None else registry.removeprefix("v")
    )
    assert set(observed["system32"]) == {
        "msvcp140.dll",
        "vcruntime140.dll",
        "vcruntime140_1.dll",
    }

    profile = load_release_catalog().profile_for("windows", "x86_64")
    plan_prerequisites = _resolve_prerequisites(
        profile,
        target_platform="windows",
        target_architecture="x86_64",
        host_platform="windows",
        host_architecture="x86_64",
    )
    prerequisite = plan_prerequisites["windows_vc_runtime_x64"]
    assert prerequisite["status"] == status
    if action is None:
        assert prerequisite["action"] is None
    else:
        assert prerequisite["action"]["kind"] == action
        assert prerequisite["action"]["blocking"] is True


def test_windows_vc_cross_target_is_unverifiable_without_windows_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(component_environment, "current_platform", lambda: "macos")
    monkeypatch.setattr(component_environment, "current_architecture", lambda: "arm64")
    monkeypatch.setattr(
        component_environment,
        "_read_windows_registry_version",
        lambda: pytest.fail("cross-target probe must not read registry"),
    )
    monkeypatch.setattr(
        component_environment,
        "_read_windows_dll_version",
        lambda _name: pytest.fail("cross-target probe must not read DLLs"),
    )

    observation = component_environment.probe_windows_vc_runtime()
    assert observation["status"] == "unverifiable_cross_target"
    assert observation["observed"] == {
        "registry_version": None,
        "system32": {
            "msvcp140.dll": None,
            "vcruntime140.dll": None,
            "vcruntime140_1.dll": None,
        },
    }


def test_include_audalign_selects_the_windows_profile_group(
    tmp_path: Path,
) -> None:
    plan = build_install_plan(
        tmp_path / "managed",
        tmp_path / "cache",
        platform="windows",
        architecture="x86_64",
        ffmpeg_command="roughcut-missing-ffmpeg",
        ffprobe_command="roughcut-missing-ffprobe",
        verify_components=True,
        include_audalign=True,
    )

    assert AUDALIGN_GROUP_NAME in plan.payload["missing_managed_groups"]
    assert sum(
        artifact["size"]
        for artifact in plan.payload["artifacts"]
        if artifact["component"] == AUDALIGN_GROUP_NAME
    ) == 81_126_882
    assert not (tmp_path / "managed").exists()
    assert not (tmp_path / "cache").exists()


def test_include_bbc_selects_exact_closed_group_cross_platform(
    tmp_path: Path,
) -> None:
    plan = build_install_plan(
        tmp_path / "managed",
        tmp_path / "cache",
        platform="windows",
        architecture="x86_64",
        ffmpeg_command="roughcut-missing-ffmpeg",
        ffprobe_command="roughcut-missing-ffprobe",
        verify_components=True,
        include_bbc_audio_offset_finder=True,
    )

    payload_group = plan.payload[BBC_AUDIO_OFFSET_FINDER_GROUP_NAME]
    assert isinstance(payload_group, dict)
    assert BBC_AUDIO_OFFSET_FINDER_GROUP_NAME in plan.payload["missing_managed_groups"]
    assert payload_group["available"] is False
    assert payload_group["version"] == "0.5.5"
    assert "upstream_commit" not in payload_group
    assert len(payload_group["distributions"]) == 36
    assert payload_group["distributions"][0] == {
        "name": "audio-offset-finder",
        "version": "0.5.5",
    }
    assert payload_group["estimated_installed_bytes"] == 550_000_000
    artifacts = [
        artifact
        for artifact in plan.payload["artifacts"]
        if artifact["component"] == BBC_AUDIO_OFFSET_FINDER_GROUP_NAME
    ]
    assert len(artifacts) == 36
    assert sum(artifact["size"] for artifact in artifacts) == 128_450_007
    assert not (tmp_path / "managed").exists()
    assert not (tmp_path / "cache").exists()


@pytest.mark.skipif(
    current_platform() != "macos" or current_architecture() != "arm64",
    reason="the bbc apply smoke stages a real venv on the local host",
)
def test_full_apply_installs_bbc_group_and_publishes_provider_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payloads: dict[str, bytes] = {}
    smoke_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        component_installation,
        "_run_staged_bbc_smoke",
        lambda _staging, _profile, spec, _record, *, ffmpeg_command: smoke_calls.append(
            (spec.provider, ffmpeg_command)
        ),
    )
    with _artifact_server(payloads) as (base_url, requests):
        catalog, fixture_payloads = _fixture_catalog(
            tmp_path, base_url, with_bbc_group=True
        )
        payloads.update(fixture_payloads)
        external_python = _write_external_python(tmp_path / "external/bin/python")
        external_models = _write_external_models(
            tmp_path / "external models",
            names=MODEL_COMPONENT_NAMES,
            models=catalog.profiles[0].models,
        )
        for name in MODEL_COMPONENT_NAMES:
            for required in component_environment.MODEL_RUNTIME_FILES[name]:
                (tmp_path / "external models" / "models" / name / required).write_bytes(
                    fixture_payloads[f"/{name}-{required.replace('/', '_')}"]
                )
        ffmpeg, ffprobe = _write_media_tools(tmp_path)
        monkeypatch.setattr(
            component_installation,
            "probe_external_python",
            lambda _path: _external_probe_result(),
        )
        managed = tmp_path / "managed"
        cache = tmp_path / "cache"
        install_root = tmp_path / "install"
        plan = build_install_plan(
            managed,
            cache,
            catalog=catalog,
            install_root=install_root,
            external_manifest_path=external_models,
            external_python=external_python,
            ffmpeg_command=str(ffmpeg),
            ffprobe_command=str(ffprobe),
            verify_components=True,
            include_bbc_audio_offset_finder=True,
        )

        assert plan.payload["missing_managed_groups"] == [
            BBC_AUDIO_OFFSET_FINDER_GROUP_NAME
        ]
        phases: list[str] = []
        applied = apply_install_plan(
            managed,
            cache,
            approved_plan_hash=plan.plan_hash,
            catalog=catalog,
            install_root=install_root,
            external_manifest_path=external_models,
            external_python=external_python,
            ffmpeg_command=str(ffmpeg),
            ffprobe_command=str(ffprobe),
            python_executable=Path(sys.executable),
            verify_components=True,
            include_bbc_audio_offset_finder=True,
            phase_callback=phases.append,
        )
        reused_plan = build_install_plan(
            managed,
            cache,
            catalog=catalog,
            install_root=install_root,
            external_manifest_path=external_models,
            external_python=external_python,
            ffmpeg_command=str(ffmpeg),
            ffprobe_command=str(ffprobe),
            verify_components=True,
            include_bbc_audio_offset_finder=True,
        )
        reused = apply_install_plan(
            managed,
            cache,
            approved_plan_hash=reused_plan.plan_hash,
            catalog=catalog,
            install_root=install_root,
            external_manifest_path=external_models,
            external_python=external_python,
            ffmpeg_command=str(ffmpeg),
            ffprobe_command=str(ffprobe),
            python_executable=Path(sys.executable),
            verify_components=True,
            include_bbc_audio_offset_finder=True,
        )

    assert applied.installed_groups == (BBC_AUDIO_OFFSET_FINDER_GROUP_NAME,)
    assert phases == [
        "component_installation_preparing",
        "component_installation_downloading",
        "component_installation_installing",
        "component_installation_verifying",
        "component_installation_publishing_runtime",
    ]
    assert reused.reused is True
    assert (managed / "bbc_audio_offset_finder/venv-receipt.json").is_file()
    receipt = json.loads(
        (managed / "bbc_audio_offset_finder/venv-receipt.json")
        .read_text(encoding="utf-8")
    )
    assert receipt["provider"] == "bbc_audio_offset_finder"
    assert receipt["provider_version"] == "0.5.5"
    binding = load_runtime_binding(install_root / "runtime.json")
    selection = binding.alignment_python
    assert selection is not None
    assert selection.provider == "bbc_audio_offset_finder"
    assert selection.provider_version == "0.5.5"
    assert selection.interpreter.endswith(
        "bbc_audio_offset_finder/venv/bin/python"
    )
    serialized = binding.to_dict()["alignment_python"]
    assert "provider" in serialized
    assert "audalign_version" not in serialized
    assert len(requests) == 36
    assert smoke_calls == [("bbc_audio_offset_finder", str(ffmpeg.resolve()))]


@pytest.mark.skipif(
    current_platform() != "macos" or current_architecture() != "arm64",
    reason="the BBC upgrade fixture stages real venvs on the local host",
)
def test_synthetic_audalign_only_offline_cached_bbc_catalog_does_not_replace_production_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cached BBC catalog closure stays out of the current production provider."""
    payloads: dict[str, bytes] = {}
    monkeypatch.setattr(
        component_installation,
        "_run_staged_bbc_smoke",
        lambda *_args, **_kwargs: None,
    )
    with _artifact_server(payloads) as (base_url, requests):
        # The loopback server is only allowed to establish the historical
        # Audalign install. The production-like BBC upgrade must use its
        # prefilled artifact/receipt closure and make no HTTP request.
        catalog, fixture_payloads = _fixture_catalog(
            tmp_path,
            base_url,
            with_audalign_group=True,
            with_bbc_group=True,
        )
        payloads.update(fixture_payloads)
        external_python = _write_external_python(tmp_path / "external/bin/python")
        external_models = _write_external_models(
            tmp_path / "external models",
            names=MODEL_COMPONENT_NAMES,
            models=catalog.profiles[0].models,
        )
        for name in MODEL_COMPONENT_NAMES:
            for required in component_environment.MODEL_RUNTIME_FILES[name]:
                (tmp_path / "external models" / "models" / name / required).write_bytes(
                    fixture_payloads[f"/{name}-{required.replace('/', '_')}"]
                )
        ffmpeg, ffprobe = _write_media_tools(tmp_path)
        monkeypatch.setattr(
            component_installation,
            "probe_external_python",
            lambda _path: _external_probe_result(),
        )
        managed = tmp_path / "managed"
        cache = tmp_path / "cache"
        install_root = tmp_path / "install"
        common = {
            "catalog": catalog,
            "install_root": install_root,
            "external_manifest_path": external_models,
            "external_python": external_python,
            "ffmpeg_command": str(ffmpeg),
            "ffprobe_command": str(ffprobe),
            "verify_components": True,
        }
        audalign_plan = build_install_plan(
            managed,
            cache,
            include_audalign=True,
            **common,
        )
        apply_install_plan(
            managed,
            cache,
            approved_plan_hash=audalign_plan.plan_hash,
            python_executable=Path(sys.executable),
            include_audalign=True,
            **common,
        )
        before = diagnostics(runtime_path=install_root / "runtime.json")["alignment"]
        assert before["required_provider"] == "audalign"
        assert before["required_provider_version"] == "1.3.1"
        assert before["configured_provider"] == "audalign"
        assert before["configured_provider_version"] == "1.3.1"
        assert before["status"] == "available"
        assert before["production_ready"] is True
        before_binding = load_runtime_binding(install_root / "runtime.json")
        audalign_request_count = len(requests)

        bbc_spec = catalog.profiles[0].alignment_group(
            BBC_AUDIO_OFFSET_FINDER_GROUP_NAME
        )
        assert bbc_spec is not None
        for artifact in bbc_spec.artifacts:
            artifact_path = cache_artifact_path(cache, artifact)
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.write_bytes(
                fixture_payloads[artifact.url.removeprefix(base_url)]
            )
            component_download._write_receipt(
                cache_receipt_path(artifact_path), artifact
            )
            assert artifact_path.is_file()
            assert cache_receipt_path(artifact_path).is_file()

        bbc_plan = build_install_plan(
            managed,
            cache,
            include_bbc_audio_offset_finder=True,
            **common,
        )
        assert bbc_plan.plan_hash != audalign_plan.plan_hash
        assert bbc_plan.payload["download_bytes"] == 0

        def fail_network(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("offline cached BBC upgrade attempted HTTP")

        monkeypatch.setattr(component_download.urllib.request, "urlopen", fail_network)
        operation_id = "op_offline_cached_bbc_catalog_install"

        def apply_bbc(phase_callback: object) -> object:
            assert callable(phase_callback)
            return apply_install_plan(
                managed,
                cache,
                approved_plan_hash=bbc_plan.plan_hash,
                python_executable=Path(sys.executable),
                phase_callback=phase_callback,
                include_bbc_audio_offset_finder=True,
                **common,
            )

        def result_ref(applied: object) -> InstallationResultRef:
            assert hasattr(applied, "approved_plan_hash")
            return InstallationResultRef(
                approved_plan_hash=applied.approved_plan_hash,
                runtime_binding_sha256=component_digest(
                    install_root / "runtime.json"
                ),
                component_manifest_sha256=component_digest(
                    managed / "component-manifest.json"
                ),
            )

        outcome = run_component_installation(
            install_root,
            operation_id=operation_id,
            approved_plan_hash=bbc_plan.plan_hash,
            apply=apply_bbc,
            result_ref=result_ref,
        )
        assert outcome.record.status == "succeeded"
        assert outcome.record.operation_id == operation_id
        assert outcome.record.result_ref is not None
        assert outcome.record.result_ref.approved_plan_hash == bbc_plan.plan_hash
        assert outcome.readback is False
        assert installation_operation_status(install_root, operation_id) == outcome.record
        assert len(requests) == audalign_request_count

        after_binding = load_runtime_binding(install_root / "runtime.json")
        assert after_binding.python == before_binding.python
        assert after_binding.components == before_binding.components
        assert after_binding.ffmpeg == before_binding.ffmpeg
        assert after_binding.ffprobe == before_binding.ffprobe
        after = diagnostics(runtime_path=install_root / "runtime.json")["alignment"]

    assert after == {
        "required_provider": "audalign",
        "required_provider_version": "1.3.1",
        "configured_provider": "bbc_audio_offset_finder",
        "configured_provider_version": "0.5.5",
        "interpreter": after["interpreter"],
        "status": "provider_mismatch",
        "production_ready": False,
    }


@pytest.mark.skipif(
    current_platform() != "macos" or current_architecture() != "arm64",
    reason="the bbc upgrade fixture stages a real venv on the local host",
)
def test_old_bbc_generic_soundfile_group_is_replaced_by_new_platform_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads: dict[str, bytes] = {}
    monkeypatch.setattr(
        component_installation,
        "_run_staged_bbc_smoke",
        lambda *_args, **_kwargs: None,
    )
    with _artifact_server(payloads) as (base_url, requests):
        old_catalog, fixture_payloads = _fixture_catalog(
            tmp_path,
            base_url,
            with_bbc_group=True,
        )
        payloads.update(fixture_payloads)
        new_catalog = _fixture_catalog_with_platform_bbc_soundfile(
            old_catalog,
            tmp_path,
            base_url,
            payloads,
        )
        old_spec = old_catalog.profiles[0].alignment_group(
            BBC_AUDIO_OFFSET_FINDER_GROUP_NAME
        )
        new_spec = new_catalog.profiles[0].alignment_group(
            BBC_AUDIO_OFFSET_FINDER_GROUP_NAME
        )
        assert old_spec is not None and new_spec is not None
        old_soundfile = next(
            artifact for artifact in old_spec.artifacts if artifact.name == "soundfile"
        )
        new_soundfile = next(
            artifact for artifact in new_spec.artifacts if artifact.name == "soundfile"
        )
        assert "none-any" in old_soundfile.filename
        assert "none-any" not in new_soundfile.filename

        external_python = _write_external_python(tmp_path / "external/bin/python")
        external_models = _write_external_models(
            tmp_path / "external models",
            names=MODEL_COMPONENT_NAMES,
            models=old_catalog.profiles[0].models,
        )
        for name in MODEL_COMPONENT_NAMES:
            for required in component_environment.MODEL_RUNTIME_FILES[name]:
                (tmp_path / "external models" / "models" / name / required).write_bytes(
                    fixture_payloads[f"/{name}-{required.replace('/', '_')}"]
                )
        ffmpeg, ffprobe = _write_media_tools(tmp_path)
        monkeypatch.setattr(
            component_installation,
            "probe_external_python",
            lambda _path: _external_probe_result(),
        )
        managed = tmp_path / "managed"
        cache = tmp_path / "cache"
        install_root = tmp_path / "install"
        common = {
            "install_root": install_root,
            "external_manifest_path": external_models,
            "external_python": external_python,
            "ffmpeg_command": str(ffmpeg),
            "ffprobe_command": str(ffprobe),
            "verify_components": True,
            "include_bbc_audio_offset_finder": True,
            "python_executable": Path(sys.executable),
        }
        old_plan = build_install_plan(
            managed,
            cache,
            catalog=old_catalog,
            **{key: value for key, value in common.items() if key != "python_executable"},
        )
        apply_install_plan(
            managed,
            cache,
            approved_plan_hash=old_plan.plan_hash,
            catalog=old_catalog,
            **common,
        )
        old_manifest = load_component_manifest(managed / COMPONENT_MANIFEST_FILENAME)
        old_record = next(
            record
            for record in old_manifest.components
            if record.name == BBC_AUDIO_OFFSET_FINDER_GROUP_NAME
        )

        new_plan = build_install_plan(
            managed,
            cache,
            catalog=new_catalog,
            **{key: value for key, value in common.items() if key != "python_executable"},
        )
        assert new_plan.payload["missing_managed_groups"] == [
            BBC_AUDIO_OFFSET_FINDER_GROUP_NAME
        ]
        applied = apply_install_plan(
            managed,
            cache,
            approved_plan_hash=new_plan.plan_hash,
            catalog=new_catalog,
            **common,
        )

    assert applied.installed_groups == (BBC_AUDIO_OFFSET_FINDER_GROUP_NAME,)
    new_manifest = load_component_manifest(managed / COMPONENT_MANIFEST_FILENAME)
    new_record = next(
        record
        for record in new_manifest.components
        if record.name == BBC_AUDIO_OFFSET_FINDER_GROUP_NAME
    )
    assert new_record.verification.value != old_record.verification.value
    receipt = json.loads(
        (managed / "bbc_audio_offset_finder/venv-receipt.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt["packages"]["soundfile"]["artifact"] == new_soundfile.filename
    assert new_record.path == "bbc_audio_offset_finder/venv-receipt.json"
    assert not _managed_backup_paths(managed)
    assert not list(tmp_path.glob(f"{component_installation.MANAGED_STAGING_PREFIX}*"))
    binding = load_runtime_binding(install_root / "runtime.json")
    assert binding.alignment_python is not None
    assert binding.alignment_python.provider == "bbc_audio_offset_finder"
    assert requests[-1] is None


def test_missing_bbc_with_unavailable_ffmpeg_is_blocked_in_the_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _artifact_server({}) as (base_url, _requests):
        catalog, _payloads = _fixture_catalog(
            tmp_path, base_url, with_bbc_group=True
        )
        external_python = _write_external_python(tmp_path / "external/bin/python")
        external_models = _write_external_models(
            tmp_path / "external models",
            names=MODEL_COMPONENT_NAMES,
            models=catalog.profiles[0].models,
        )
        for name in MODEL_COMPONENT_NAMES:
            for required in component_environment.MODEL_RUNTIME_FILES[name]:
                (tmp_path / "external models" / "models" / name / required).write_bytes(
                    _payloads[f"/{name}-{required.replace('/', '_')}"]
                )
        monkeypatch.setattr(
            component_installation,
            "probe_external_python",
            lambda _path: _external_probe_result(),
        )
        plan = build_install_plan(
            tmp_path / "managed",
            tmp_path / "cache",
            catalog=catalog,
            external_manifest_path=external_models,
            external_python=external_python,
            ffmpeg_command="roughcut-missing-ffmpeg",
            ffprobe_command="roughcut-missing-ffprobe",
            verify_components=True,
            include_bbc_audio_offset_finder=True,
        )

    assert plan.payload["missing_managed_groups"] == [
        BBC_AUDIO_OFFSET_FINDER_GROUP_NAME
    ]
    assert plan.payload["user_actions"] == [
        {
            "component": "ffmpeg_ffprobe",
            "status": "user_action_required",
            "blocking": True,
            **catalog.profiles[0].ffmpeg.to_dict(),
        }
    ]
    with pytest.raises(
        ComponentInstallError,
        match="blocking component prerequisite action is unresolved",
    ):
        apply_install_plan(
            tmp_path / "managed",
            tmp_path / "cache",
            approved_plan_hash=plan.plan_hash,
            catalog=catalog,
            preflight_plan=plan,
        )


def test_model_only_apply_does_not_require_unavailable_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, _payloads = _fixture_catalog(tmp_path, "https://fixture.invalid")
    installed: list[tuple[tuple[str, ...], str | None]] = []
    monkeypatch.setattr(component_download, "download_artifact", lambda *_args: None)
    monkeypatch.setattr(
        component_installation,
        "_install_staged_components",
        lambda _root, _cache, _profile, groups, *, python_executable, ffmpeg_command: (
            installed.append((groups, ffmpeg_command))
        ),
    )
    plan = build_install_plan(
        tmp_path / "managed",
        tmp_path / "cache",
        catalog=catalog,
        ffmpeg_command="roughcut-missing-ffmpeg",
        ffprobe_command="roughcut-missing-ffprobe",
        verify_components=True,
    )

    result = apply_install_plan(
        tmp_path / "managed",
        tmp_path / "cache",
        approved_plan_hash=plan.plan_hash,
        catalog=catalog,
        ffmpeg_command="roughcut-missing-ffmpeg",
        ffprobe_command="roughcut-missing-ffprobe",
        python_executable=Path(sys.executable),
        verify_components=True,
    )

    assert plan.payload["user_actions"][0]["blocking"] is False
    assert result.installed_groups == tuple(plan.payload["missing_managed_groups"])
    assert installed == [(result.installed_groups, None)]


def test_audalign_only_apply_does_not_require_unavailable_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, payloads = _fixture_catalog(
        tmp_path, "https://fixture.invalid", with_audalign_group=True
    )
    external_python = _write_external_python(tmp_path / "external/bin/python")
    external_models = _write_external_models(
        tmp_path / "external models",
        names=MODEL_COMPONENT_NAMES,
        models=catalog.profiles[0].models,
    )
    for name in MODEL_COMPONENT_NAMES:
        for required in component_environment.MODEL_RUNTIME_FILES[name]:
            (tmp_path / "external models" / "models" / name / required).write_bytes(
                payloads[f"/{name}-{required.replace('/', '_')}"]
            )
    installed: list[tuple[tuple[str, ...], str | None]] = []
    monkeypatch.setattr(
        component_installation,
        "probe_external_python",
        lambda _path: _external_probe_result(),
    )
    monkeypatch.setattr(component_download, "download_artifact", lambda *_args: None)
    monkeypatch.setattr(
        component_installation,
        "_install_staged_components",
        lambda _root, _cache, _profile, groups, *, python_executable, ffmpeg_command: (
            installed.append((groups, ffmpeg_command))
        ),
    )
    plan = build_install_plan(
        tmp_path / "managed",
        tmp_path / "cache",
        catalog=catalog,
        external_manifest_path=external_models,
        external_python=external_python,
        ffmpeg_command="roughcut-missing-ffmpeg",
        ffprobe_command="roughcut-missing-ffprobe",
        verify_components=True,
        include_audalign=True,
    )

    result = apply_install_plan(
        tmp_path / "managed",
        tmp_path / "cache",
        approved_plan_hash=plan.plan_hash,
        catalog=catalog,
        external_manifest_path=external_models,
        external_python=external_python,
        ffmpeg_command="roughcut-missing-ffmpeg",
        ffprobe_command="roughcut-missing-ffprobe",
        python_executable=Path(sys.executable),
        verify_components=True,
        include_audalign=True,
    )

    assert plan.payload["missing_managed_groups"] == [AUDALIGN_GROUP_NAME]
    assert plan.payload["user_actions"][0]["blocking"] is False
    assert result.installed_groups == (AUDALIGN_GROUP_NAME,)
    assert installed == [((AUDALIGN_GROUP_NAME,), None)]


def test_plan_hash_binds_the_alignment_provider_selection(
    tmp_path: Path,
) -> None:
    common: dict[str, object] = {
        "managed_root": tmp_path / "managed",
        "cache_root": tmp_path / "cache",
        "platform": "windows",
        "architecture": "x86_64",
        "ffmpeg_command": "roughcut-missing-ffmpeg",
        "ffprobe_command": "roughcut-missing-ffprobe",
        "verify_components": True,
    }
    audalign_only = build_install_plan(
        include_audalign=True,
        **common,  # type: ignore[arg-type]
    )
    bbc_only = build_install_plan(
        include_bbc_audio_offset_finder=True,
        **common,  # type: ignore[arg-type]
    )
    assert audalign_only.plan_hash != bbc_only.plan_hash
    assert (
        audalign_only.payload["audalign_fingerprint"]["available"]
        is False
    )
    assert (
        bbc_only.payload[BBC_AUDIO_OFFSET_FINDER_GROUP_NAME]["available"]
        is False
    )
    with pytest.raises(ComponentInstallError):
        build_install_plan(
            include_audalign=True,  # type: ignore[arg-type]
            include_bbc_audio_offset_finder=True,  # type: ignore[arg-type]
            **common,  # type: ignore[arg-type]
        )


def test_repository_catalog_loads_bbc_audio_offset_finder_group() -> None:
    catalog = load_release_catalog()
    for platform, architecture, installed_bytes in (
        ("macos", "arm64", 540_000_000),
        ("windows", "x86_64", 550_000_000),
    ):
        group = catalog.profile_for(platform, architecture).alignment_group(
            BBC_AUDIO_OFFSET_FINDER_GROUP_NAME
        )
        assert group is not None
        assert group.provider == "bbc_audio_offset_finder"
        assert group.version == "0.5.5"
        assert group.upstream_commit is None
        assert group.record_license == "Apache-2.0"
        assert group.estimated_installed_bytes == installed_bytes
        assert len(group.artifacts) == 36
        direct = next(
            artifact
            for artifact in group.artifacts
            if artifact.name == "audio-offset-finder"
        )
        assert (direct.version, direct.license) == ("0.5.5", "Apache-2.0")
        from roughcut.adapters.runtime_binding import (
            bbc_audio_offset_finder_distribution_versions_for,
        )

        assert dict(group.distributions) == (
            bbc_audio_offset_finder_distribution_versions_for(platform)
        )


def test_bbc_catalogs_pin_platform_soundfile_wheels_with_bundled_native_libraries() -> None:
    catalog = load_release_catalog()
    expected = {
        ("macos", "arm64"): (
            "soundfile-0.14.0-py2.py3-none-macosx_11_0_arm64.whl",
            "d828d35a059626da52f1415b5faee610aeab393319cb3fc4a9aef47b619fc14c",
            1_103_726,
        ),
        ("windows", "x86_64"): (
            "soundfile-0.14.0-py2.py3-none-win_amd64.whl",
            "299491d3499460fb1b74bb4bd78b57ffc2d243a5fafa7b6ec1b264875c78453e",
            1_021_480,
        ),
    }
    for target, identity in expected.items():
        group = catalog.profile_for(*target).alignment_group(
            BBC_AUDIO_OFFSET_FINDER_GROUP_NAME
        )
        assert group is not None
        soundfile = next(item for item in group.artifacts if item.name == "soundfile")
        assert (soundfile.filename, soundfile.sha256, soundfile.size) == identity
        assert "none-any" not in soundfile.filename
        assert soundfile.license == "BSD 3-Clause License"


def test_windows_plan_budget_rejects_both_alignment_groups_in_one_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(component_environment, "current_platform", lambda: "windows")
    monkeypatch.setattr(
        component_environment, "current_architecture", lambda: "x86_64"
    )
    monkeypatch.setattr(
        component_environment,
        "probe_windows_vc_runtime",
        lambda: {
            "status": "ready",
            "observed": {
                "registry_version": "14.51.36247.0",
                "system32": {
                    "msvcp140.dll": "14.51.36247.0",
                    "vcruntime140.dll": "14.51.36247.0",
                    "vcruntime140_1.dll": "14.51.36247.0",
                },
            },
        },
    )
    with pytest.raises(ComponentInstallError):
        build_install_plan(
            Path("C:\\Users\\tester\\.roughcut\\managed"),
            Path("C:\\Users\\tester\\.roughcut\\cache"),
            platform="windows",
            architecture="x86_64",
            ffmpeg_command="roughcut-missing-ffmpeg",
            ffprobe_command="roughcut-missing-ffprobe",
            verify_components=True,
            include_audalign=True,
            include_bbc_audio_offset_finder=True,
        )


def test_repository_keeps_bbc_catalogs_byte_identical_and_hash_pinned() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    attributes = (
        repository_root / ".gitattributes"
    ).read_text(encoding="utf-8").splitlines()
    assert (
        "core/src/roughcut/component_catalog/audio-offset-finder/* -text diff"
        in attributes
    )
    attr_result = subprocess.run(
        ["git", "check-attr", "text", "diff", "--", *BBC_CATALOG_PATHS],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )
    assert attr_result.stdout.splitlines() == [
        line
        for path in BBC_CATALOG_PATHS
        for line in (f"{path}: text: unset", f"{path}: diff: set")
    ]
    for relative_path in BBC_CATALOG_PATHS:
        catalog_path = repository_root / relative_path
        working_tree_bytes = catalog_path.read_bytes()
        index_bytes = subprocess.run(
            ["git", "show", f":{relative_path}"],
            cwd=repository_root,
            check=True,
            capture_output=True,
        ).stdout
        assert working_tree_bytes == index_bytes
        assert hashlib.sha256(working_tree_bytes).hexdigest() == (
            BBC_CATALOG_SHA256[relative_path]
        )


def test_historical_audalign_managed_layout_is_reused_without_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Historical audalign managed identity/layout must stay reusable."""
    from roughcut.adapters import component_environment as ce

    managed = tmp_path / "managed"
    venv = managed / "audalign" / "venv" / "bin"
    venv.mkdir(parents=True)
    interpreter = venv / "python"
    catalog = load_release_catalog()
    spec = catalog.profile_for("macos", "arm64").alignment_group(
        "audalign_fingerprint"
    )
    assert spec is not None
    contents = (
        b"#!/bin/sh\n"
        b"printf 'ROUGHCUT-PROBE/1 "
        b'{"python_version":"3.11",'
        b'"distributions":[[\"audalign\",\"1.3.1\"],[\"contourpy\",\"1.3.3\"],'
        b'[\"cycler\",\"0.12.1\"],[\"fonttools\",\"4.63.0\"],'
        b'[\"kiwisolver\",\"1.5.0\"],[\"matplotlib\",\"3.8.2\"],'
        b'[\"numpy\",\"1.26.4\"],[\"packaging\",\"26.2\"],'
        b'[\"pillow\",\"12.3.0\"],[\"pydub\",\"0.25.1\"],'
        b'[\"pyparsing\",\"3.3.2\"],[\"python-dateutil\",\"2.9.0.post0\"],'
        b'[\"scipy\",\"1.12.0\"],[\"setuptools\",\"59.6.0\"],'
        b'[\"six\",\"1.17.0\"],[\"tqdm\",\"4.66.2\"]]}'
        b"\\n'\n"
    )
    interpreter.write_bytes(contents)
    interpreter.chmod(0o755)
    (managed / "audalign" / "locks").mkdir(parents=True, exist_ok=True)
    (managed / "audalign" / "licenses").mkdir(parents=True, exist_ok=True)
    (managed / "audalign" / "locks" / spec.dependency_lock.name).write_bytes(
        (
            Path(__file__).resolve().parents[2]
            / "core/src/roughcut/component_catalog"
            / "audalign"
            / spec.dependency_lock.name
        ).read_bytes()
    )
    (managed / "audalign" / "licenses" / spec.license_notice_file.name).write_bytes(
        (
            Path(__file__).resolve().parents[2]
            / "core/src/roughcut/component_catalog"
            / "audalign"
            / spec.license_notice_file.name
        ).read_bytes()
    )
    venv_receipt: dict[str, object] = {
        "schema_version": 1,
        "interpreter": "audalign/venv/bin/python",
        "python_version": "3.11",
        "audalign_version": spec.version,
        "audalign_upstream_commit": spec.upstream_commit,
        "dependency_lock_receipt": {
            "algorithm": "sha256",
            "value": spec.dependency_lock_sha256,
        },
        "license_notice_receipt": {
            "algorithm": "sha256",
            "value": spec.license_notice_sha256,
        },
        "distributions": [
            {"name": name, "version": version} for name, version in spec.distributions
        ],
        "packages": {},
    }
    receipt_path = managed / "audalign" / "venv-receipt.json"
    receipt_path.write_text(json.dumps(venv_receipt))
    import hashlib as hashlib_module

    record = ComponentRecord(
        name=spec.managed_record_name,
        kind="python_package",
        source_type="managed",
        origin=spec.origin,
        version=spec.version,
        path=f"{spec.managed_dir}/venv-receipt.json",
        platform="macos",
        architecture="arm64",
        license=spec.record_license,
        verification=ComponentVerification(
            "sha256", hashlib_module.sha256(receipt_path.read_bytes()).hexdigest()
        ),
    )
    manifest = ComponentManifest(
        components=(record,),
        platform="macos",
        architecture="arm64",
        managed_root=str(managed),
        schema_version=2,
        python_runtime=None,
    )
    ce.write_component_manifest(managed / "component-manifest.json", manifest)

    external_python = _write_external_python(tmp_path / "external/bin/python")
    external_models = _write_legacy_external_models(tmp_path / "external models")
    ffmpeg, ffprobe = _write_media_tools(tmp_path)
    monkeypatch.setattr(
        component_installation, "probe_external_python", lambda _path: _external_probe_result()
    )
    expected_digests = {
        **LEGACY_EXTERNAL_MODEL_DIGESTS,
        "model_spk": catalog.profile_for("macos", "arm64").models["model_spk"].directory_sha256,
    }
    monkeypatch.setattr(
        ce, "component_record_digest", lambda name, _path: expected_digests.get(name, "d" * 64)
    )

    plan = build_install_plan(
        managed,
        tmp_path / "cache",
        external_manifest_path=external_models,
        external_python=external_python,
        platform="macos",
        architecture="arm64",
        ffmpeg_command=str(ffmpeg),
        ffprobe_command=str(ffprobe),
        verify_components=True,
        include_audalign=True,
    )
    assert plan.payload[AUDALIGN_GROUP_NAME]["available"] is True
    assert plan.payload["missing_managed_groups"] == []
    assert plan.payload["artifacts"] == []
    assert not (managed / "audalign_fingerprint").exists()


def test_new_audalign_staging_keeps_historical_record_and_runtime_encoding(
    tmp_path: Path,
) -> None:
    payloads: dict[str, bytes] = {}
    with _artifact_server(payloads) as (base_url, _requests):
        catalog, fixture_payloads = _fixture_catalog(
            tmp_path, base_url, with_audalign_group=True
        )
        payloads.update(fixture_payloads)
        external_python = _write_external_python(tmp_path / "external/bin/python")
        external_models = _write_external_models(
            tmp_path / "external models",
            names=("model_asr", "model_vad", "model_punc", "model_spk"),
            models=catalog.profiles[0].models,
        )
        for name in ("model_asr", "model_vad", "model_punc", "model_spk"):
            for required in component_environment.MODEL_RUNTIME_FILES[name]:
                (tmp_path / "external models" / "models" / name / required).write_bytes(
                    fixture_payloads[f"/{name}-{required.replace('/', '_')}"]
                )
        ffmpeg, ffprobe = _write_media_tools(tmp_path)
        managed = tmp_path / "managed"
        cache = tmp_path / "cache"
        install_root = tmp_path / "install"
        plan = build_install_plan(
            managed,
            cache,
            catalog=catalog,
            install_root=install_root,
            external_manifest_path=external_models,
            external_python=external_python,
            ffmpeg_command=str(ffmpeg),
            ffprobe_command=str(ffprobe),
            verify_components=True,
            include_audalign=True,
        )
        applied = apply_install_plan(
            managed,
            cache,
            approved_plan_hash=plan.plan_hash,
            catalog=catalog,
            install_root=install_root,
            external_manifest_path=external_models,
            external_python=external_python,
            ffmpeg_command=str(ffmpeg),
            ffprobe_command=str(ffprobe),
            python_executable=Path(sys.executable),
            verify_components=True,
            include_audalign=True,
        )
        assert applied.installed_groups == (AUDALIGN_GROUP_NAME,)
        assert (managed / "audalign/venv-receipt.json").is_file()
        assert not (managed / "audalign_fingerprint").exists()
        receipt = json.loads((managed / "audalign/venv-receipt.json").read_text())
        assert receipt["audalign_version"] == "1.3.1"
        assert "provider" not in receipt
        binding = load_runtime_binding(install_root / "runtime.json")
        serialized = binding.to_dict()["alignment_python"]
        assert serialized["audalign_version"] == "1.3.1"
        assert "provider" not in serialized


def test_audalign_fingerprint_managed_layout_is_never_created_or_accepted(
    tmp_path: Path,
) -> None:
    from roughcut.adapters.component_environment import _is_canonical_managed_path

    assert (
        _is_canonical_managed_path(
            "audalign_fingerprint", "python_package", "audalign_fingerprint/venv-receipt.json"
        )
        is False
    )
    assert (
        _is_canonical_managed_path(
            AUDALIGN_GROUP_NAME, "python_package", "audalign_fingerprint/venv-receipt.json"
        )
        is False
    )


@pytest.mark.skipif(
    current_platform() != "macos" or current_architecture() != "arm64",
    reason="the legacy external compatibility profile is macOS arm64 only",
)
def test_legacy_external_full_verification_recomputes_and_rejects_digest_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    external_python = _write_external_python(tmp_path / "external/bin/python")
    external_models = _write_legacy_external_models(tmp_path / "external models")
    ffmpeg, ffprobe = _write_media_tools(tmp_path)
    monkeypatch.setattr(
        component_installation,
        "probe_external_python",
        lambda _path: _external_probe_result(),
    )

    plan = build_install_plan(
        tmp_path / "managed",
        tmp_path / "cache",
        external_manifest_path=external_models,
        external_python=external_python,
        platform="macos",
        architecture="arm64",
        ffmpeg_command=str(ffmpeg),
        ffprobe_command=str(ffprobe),
        verify_components=True,
    )

    components = plan.payload["components"]
    assert isinstance(components, dict)
    for name in ("model_asr", "model_vad", "model_punc"):
        assert components[name]["selected_source"] is None
        assert name in plan.payload["missing_managed_groups"]


@pytest.mark.skipif(
    current_platform() != "macos" or current_architecture() != "arm64",
    reason="the legacy external compatibility profile is macOS arm64 only",
)
@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "mixed_catalog_revision",
        "swapped_digests",
        "wrong_identity",
        "wrong_origin",
        "wrong_license",
        "schema_2",
    ],
)
def test_legacy_external_triplet_metadata_is_closed_and_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    profile = load_release_catalog().profile_for("macos", "arm64")
    names = MODEL_COMPONENT_NAMES
    version_overrides: dict[str, str] = {}
    digest_overrides: dict[str, str] = {}
    origin_overrides: dict[str, str] = {}
    license_overrides: dict[str, str] = {}
    schema_version = 1
    if case == "missing":
        names = ("model_asr", "model_vad", "model_spk")
    elif case == "mixed_catalog_revision":
        version_overrides["model_punc"] = profile.models["model_punc"].revision
        digest_overrides["model_punc"] = profile.models["model_punc"].directory_sha256
    elif case == "swapped_digests":
        digest_overrides["model_asr"] = LEGACY_EXTERNAL_MODEL_DIGESTS["model_vad"]
        digest_overrides["model_vad"] = LEGACY_EXTERNAL_MODEL_DIGESTS["model_asr"]
    elif case == "wrong_identity":
        version_overrides["model_punc"] = f"{LEGACY_EXTERNAL_MODEL_VERSION}-other"
    elif case == "wrong_origin":
        origin_overrides["model_asr"] = "iic/unreviewed-model"
    elif case == "wrong_license":
        license_overrides["model_vad"] = "unknown"
    else:
        schema_version = 2
    external_models = _write_legacy_external_models(
        tmp_path / f"external-{case}",
        names=names,
        version_overrides=version_overrides,
        digest_overrides=digest_overrides,
        origin_overrides=origin_overrides,
        license_overrides=license_overrides,
        schema_version=schema_version,
    )
    external_python = _write_external_python(tmp_path / f"python-{case}/bin/python")
    ffmpeg, ffprobe = _write_media_tools(tmp_path / f"tools-{case}")
    monkeypatch.setattr(
        component_installation,
        "probe_external_python",
        lambda _path: _external_probe_result(),
    )

    plan = build_install_plan(
        tmp_path / f"managed-{case}",
        tmp_path / f"cache-{case}",
        external_manifest_path=external_models,
        external_python=external_python,
        platform="macos",
        architecture="arm64",
        ffmpeg_command=str(ffmpeg),
        ffprobe_command=str(ffprobe),
    )

    components = plan.payload["components"]
    assert isinstance(components, dict)
    selected_legacy = [
        name
        for name in ("model_asr", "model_vad", "model_punc")
        if components[name]["selected_source"] == "external"
        and components[name]["version"] == LEGACY_EXTERNAL_MODEL_VERSION
    ]
    assert selected_legacy == []
    assert set(plan.payload["missing_managed_groups"]).intersection(
        {"model_asr", "model_vad", "model_punc"}
    )


@pytest.mark.skipif(
    current_platform() != "macos" or current_architecture() != "arm64",
    reason="the legacy external compatibility profile is macOS arm64 only",
)
@pytest.mark.parametrize("case", ["wrong_runtime", "wrong_target"])
def test_legacy_external_triplet_requires_exact_runtime_and_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    external_models = _write_legacy_external_models(tmp_path / f"external-{case}")
    external_python = _write_external_python(tmp_path / f"python-{case}/bin/python")
    ffmpeg, ffprobe = _write_media_tools(tmp_path / f"tools-{case}")
    monkeypatch.setattr(
        component_installation,
        "probe_external_python",
        lambda _path: _external_probe_result(
            funasr="1.3.9" if case == "wrong_runtime" else "1.3.8"
        ),
    )
    target = (
        {"platform": "windows", "architecture": "x86_64"}
        if case == "wrong_target"
        else {"platform": "macos", "architecture": "arm64"}
    )

    plan = build_install_plan(
        tmp_path / f"managed-{case}",
        tmp_path / f"cache-{case}",
        external_manifest_path=external_models,
        external_python=external_python,
        ffmpeg_command=str(ffmpeg),
        ffprobe_command=str(ffprobe),
        **target,
    )

    components = plan.payload["components"]
    assert isinstance(components, dict)
    for name in ("model_asr", "model_vad", "model_punc"):
        assert components[name]["selected_source"] is None
        assert name in plan.payload["missing_managed_groups"]


@pytest.mark.skipif(
    current_platform() != "macos" or current_architecture() != "arm64",
    reason="the legacy external compatibility profile is macOS arm64 only",
)
def test_legacy_external_live_symlink_rejects_the_whole_triplet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    external_root = tmp_path / "external"
    external_models = _write_legacy_external_models(external_root)
    original = external_root / "models/model_asr"
    relocated = external_root / "model-asr-relocated"
    original.rename(relocated)
    _symlink_or_skip(original, relocated, target_is_directory=True)
    external_python = _write_external_python(tmp_path / "python/bin/python")
    ffmpeg, ffprobe = _write_media_tools(tmp_path)
    monkeypatch.setattr(
        component_installation,
        "probe_external_python",
        lambda _path: _external_probe_result(),
    )

    plan = build_install_plan(
        tmp_path / "managed",
        tmp_path / "cache",
        external_manifest_path=external_models,
        external_python=external_python,
        platform="macos",
        architecture="arm64",
        ffmpeg_command=str(ffmpeg),
        ffprobe_command=str(ffprobe),
    )

    components = plan.payload["components"]
    assert isinstance(components, dict)
    assert all(
        components[name]["selected_source"] is None
        for name in ("model_asr", "model_vad", "model_punc")
    )
    record = load_component_manifest(external_models).components[0]
    with pytest.raises(ComponentError, match="absolute"):
        replace(record, path="../path-escape")


@pytest.mark.skipif(
    current_platform() != "macos" or current_architecture() != "arm64",
    reason="the legacy external compatibility profile is macOS arm64 only",
)
def test_legacy_external_full_apply_publishes_receipts_and_stale_reapply_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    external_root = tmp_path / "external models"
    external_models = _write_legacy_external_models(external_root)
    external_python = _write_external_python(tmp_path / "external/bin/python")
    ffmpeg, ffprobe = _write_media_tools(tmp_path)
    install_root = tmp_path / "install"
    managed = tmp_path / "managed"
    cache = tmp_path / "cache"
    monkeypatch.setattr(
        component_installation,
        "probe_external_python",
        lambda _path: _external_probe_result(),
    )
    expected_digests = {
        **LEGACY_EXTERNAL_MODEL_DIGESTS,
        "model_spk": load_release_catalog()
        .profile_for("macos", "arm64")
        .models["model_spk"]
        .directory_sha256,
    }
    monkeypatch.setattr(
        component_environment,
        "component_record_digest",
        lambda name, _path: expected_digests[name],
    )
    arguments = {
        "install_root": install_root,
        "external_manifest_path": external_models,
        "external_python": external_python,
        "platform": "macos",
        "architecture": "arm64",
        "ffmpeg_command": str(ffmpeg),
        "ffprobe_command": str(ffprobe),
        "verify_components": True,
    }
    model_files = {
        name: external_root / "models" / name / _model_primary_payload(name)
        for name in MODEL_COMPONENT_NAMES
    }
    model_state = {
        name: (_stable_file_identity(path), path.read_bytes())
        for name, path in model_files.items()
    }
    plan = build_install_plan(managed, cache, **arguments)

    result = apply_install_plan(
        managed,
        cache,
        approved_plan_hash=plan.plan_hash,
        **arguments,
    )

    assert result.installed_groups == ()
    assert result.runtime_binding is not None
    assert result.runtime_binding.published is True
    binding = load_runtime_binding(install_root / "runtime.json")
    for key, name in (("asr", "model_asr"), ("vad", "model_vad"), ("punc", "model_punc")):
        component = binding.components[key]
        assert component.version == LEGACY_EXTERNAL_MODEL_VERSION
        assert component.receipt == {
            "algorithm": "sha256",
            "value": LEGACY_EXTERNAL_MODEL_DIGESTS[name],
        }
        assert component.source_type == "external"
        assert component.ownership == "external_read_only"
    runtime_bytes = (install_root / "runtime.json").read_bytes()
    with pytest.raises(ComponentInstallError, match="stale"):
        apply_install_plan(
            managed,
            cache,
            approved_plan_hash=plan.plan_hash,
            **arguments,
        )

    assert (install_root / "runtime.json").read_bytes() == runtime_bytes
    assert not managed.exists()
    assert not cache.exists()
    assert {
        name: (_stable_file_identity(path), path.read_bytes())
        for name, path in model_files.items()
    } == model_state


@pytest.mark.skipif(
    current_platform() != "macos" or current_architecture() != "arm64",
    reason="the reviewed external 1.3.8 profile is macOS arm64 only",
)
def test_exact_external_138_runtime_and_models_precede_managed_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    external_python = _write_external_python(tmp_path / "共享 环境/bin/python")
    external_models = _write_external_models(tmp_path / "外部 模型")
    ffmpeg, ffprobe = _write_media_tools(tmp_path)
    monkeypatch.setattr(
        component_installation,
        "probe_external_python",
        lambda _path: _external_probe_result(),
    )
    monkeypatch.setenv("PYTHONPATH", "/must/not/leak")
    monkeypatch.setenv("PYTHONHOME", "/must/not/leak")

    plan = build_install_plan(
        tmp_path / "managed",
        tmp_path / "cache",
        external_manifest_path=external_models,
        external_python=external_python,
        ffmpeg_command=str(ffmpeg),
        ffprobe_command=str(ffprobe),
    ).to_dict()

    components = plan["components"]
    assert isinstance(components, dict)
    for name in ("funasr", "torch", "torchaudio", *MODEL_COMPONENT_NAMES):
        assert components[name]["selected_source"] == "external"
    assert plan["missing_managed_groups"] == []
    assert plan["artifacts"] == []
    assert plan["download_bytes"] == 0
    assert plan["user_actions"] == []


@pytest.mark.skipif(
    current_platform() != "macos" or current_architecture() != "arm64",
    reason="the reviewed external 1.3.8 profile is macOS arm64 only",
)
def test_full_external_apply_publishes_zero_download_read_only_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, payloads = _fixture_catalog(tmp_path, "http://127.0.0.1:9")
    external_python = _write_external_python(tmp_path / "共享 环境/bin/python")
    external_models = _write_external_models(
        tmp_path / "外部 模型",
        models=catalog.profiles[0].models,
    )
    for name in MODEL_COMPONENT_NAMES:
        for required in component_environment.MODEL_RUNTIME_FILES[name]:
            (external_models.parent / "models" / name / required).write_bytes(
                payloads[f"/{name}-{required.replace('/', '_')}"]
            )
    ffmpeg, ffprobe = _write_media_tools(tmp_path)
    install_root = tmp_path / "Roughcut 安装"
    managed = tmp_path / "managed"
    cache = tmp_path / "cache"
    monkeypatch.setattr(
        component_installation,
        "probe_external_python",
        lambda _path: _external_probe_result(),
    )
    plan = build_install_plan(
        managed,
        cache,
        catalog=catalog,
        install_root=install_root,
        external_manifest_path=external_models,
        external_python=external_python,
        ffmpeg_command=str(ffmpeg),
        ffprobe_command=str(ffprobe),
        verify_components=True,
    )

    assert not install_root.exists()
    phases: list[str] = []
    result = apply_install_plan(
        managed,
        cache,
        install_root=install_root,
        approved_plan_hash=plan.plan_hash,
        catalog=catalog,
        external_manifest_path=external_models,
        external_python=external_python,
        ffmpeg_command=str(ffmpeg),
        ffprobe_command=str(ffprobe),
        verify_components=True,
        phase_callback=phases.append,
    )

    assert result.installed_groups == ()
    assert result.runtime_binding is not None
    assert result.runtime_binding.published is True
    binding = load_runtime_binding(install_root / "runtime.json")
    assert binding.source == "persistent_external"
    assert binding.python.interpreter == str(external_python)
    assert binding.ffmpeg.source_type == "external"
    assert binding.ffmpeg.ownership == "external_read_only"
    assert binding.ffmpeg.command == str(ffmpeg.resolve())
    assert binding.ffprobe.source_type == "external"
    assert binding.ffprobe.ownership == "external_read_only"
    assert binding.ffprobe.command == str(ffprobe.resolve())
    assert all(
        component.source_type == "external"
        and component.ownership == "external_read_only"
        for component in binding.components.values()
    )
    assert not managed.exists()
    assert not cache.exists()
    assert phases == [
        "component_installation_preparing",
        "component_installation_publishing_runtime",
    ]


def test_uninstall_preserves_external_tool_tree(
    tmp_path: Path,
) -> None:
    managed = tmp_path / "managed components"
    model = managed / "models/model_asr"
    model.mkdir(parents=True)
    payload = model / "model.pt"
    payload.write_bytes(b"managed fixture")
    write_component_manifest(
        managed / "component-manifest.json",
        ComponentManifest(
            components=(
                ComponentRecord(
                    name="model_asr",
                    kind="model",
                    source_type="managed",
                    origin="fixture",
                    version="fixture",
                    path="models/model_asr",
                    platform=current_platform(),
                    architecture=current_architecture(),
                    license="Apache-2.0",
                    verification=ComponentVerification("sha256", component_digest(model)),
                ),
            ),
            platform=current_platform(),
            architecture=current_architecture(),
            managed_root=str(managed.resolve()),
        ),
    )
    stable_tools = tmp_path / "external-tools/ffmpeg/9.0-martin-riedl-arm64/bin"
    stable_tools.mkdir(parents=True)
    ffmpeg = stable_tools / "ffmpeg"
    ffprobe = stable_tools / "ffprobe"
    ffmpeg.write_bytes(b"stable ffmpeg")
    ffprobe.write_bytes(b"stable ffprobe")

    removed = uninstall_managed_components(managed)

    assert removed.uninstalled is True
    assert removed.removed_components == ("model_asr",)
    assert ffmpeg.is_file()
    assert ffprobe.is_file()


@pytest.mark.skipif(
    current_platform() != "macos" or current_architecture() != "arm64",
    reason="the reviewed external 1.3.8 profile is macOS arm64 only",
)
@pytest.mark.parametrize("transition", ["create", "replace", "delete"])
def test_runtime_state_transition_makes_fixture_plan_stale_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transition: str,
) -> None:
    payloads: dict[str, bytes] = {}
    with _artifact_server(payloads) as (base_url, requests):
        catalog, fixture_payloads = _fixture_catalog(tmp_path, base_url)
        payloads.update(fixture_payloads)
        external_python = _write_external_python(tmp_path / "external/bin/python")
        external_models = _write_external_models(
            tmp_path / "external models",
            models=catalog.profiles[0].models,
        )
        for name in MODEL_COMPONENT_NAMES:
            for required in component_environment.MODEL_RUNTIME_FILES[name]:
                (external_models.parent / "models" / name / required).write_bytes(
                    fixture_payloads[f"/{name}-{required.replace('/', '_')}"]
                )
        ffmpeg, ffprobe = _write_media_tools(tmp_path)
        managed = tmp_path / "managed"
        cache = tmp_path / "cache"
        install_root = tmp_path / "install"
        runtime_path = install_root / "runtime.json"
        monkeypatch.setattr(
            component_installation,
            "probe_external_python",
            lambda _path: _external_probe_result(),
        )
        apply_arguments = {
            "catalog": catalog,
            "install_root": install_root,
            "external_manifest_path": external_models,
            "external_python": external_python,
            "ffmpeg_command": str(ffmpeg),
            "ffprobe_command": str(ffprobe),
            "verify_components": True,
        }
        first_plan = build_install_plan(
            managed,
            cache,
            **apply_arguments,
        )
        first = apply_install_plan(
            managed,
            cache,
            approved_plan_hash=first_plan.plan_hash,
            **apply_arguments,
        )
        assert first.runtime_binding is not None
        if transition == "create":
            stale_plan = first_plan
        else:
            stale_plan = build_install_plan(
                managed,
                cache,
                **apply_arguments,
            )
            if transition == "replace":
                binding = load_runtime_binding(runtime_path)
                publish_runtime_binding(
                    runtime_path,
                    replace(binding, profile="concurrent-replacement"),
                )
            else:
                runtime_path.unlink()
        concurrent_bytes = (
            runtime_path.read_bytes() if runtime_path.is_file() else None
        )

        with pytest.raises(
            ComponentInstallError,
            match="Roughcut bootstrap 拒绝发布 stale component plan",
        ):
            apply_install_plan(
                managed,
                cache,
                approved_plan_hash=stale_plan.plan_hash,
                **apply_arguments,
            )

    assert requests == []
    if concurrent_bytes is None:
        assert not runtime_path.exists()
    else:
        assert runtime_path.read_bytes() == concurrent_bytes
    assert not list(install_root.glob(".runtime.json.*.tmp"))


def test_apply_refuses_runtime_publish_without_full_verification(
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "install"
    plan = build_install_plan(
        tmp_path / "managed",
        tmp_path / "cache",
        install_root=install_root,
        ffmpeg_command="roughcut-missing-ffmpeg",
        ffprobe_command="roughcut-missing-ffprobe",
    )

    with pytest.raises(ComponentInstallError, match="full verification"):
        apply_install_plan(
            tmp_path / "managed",
            tmp_path / "cache",
            install_root=install_root,
            approved_plan_hash=plan.plan_hash,
            ffmpeg_command="roughcut-missing-ffmpeg",
            ffprobe_command="roughcut-missing-ffprobe",
        )

    assert not install_root.exists()


@pytest.mark.skipif(
    current_platform() != "macos" or current_architecture() != "arm64",
    reason="the reviewed external 1.3.8 profile is macOS arm64 only",
)
def test_only_missing_speaker_is_installed_and_reused_without_copying_base_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payloads: dict[str, bytes] = {}
    with _artifact_server(payloads) as (base_url, requests):
        catalog, fixture_payloads = _fixture_catalog(tmp_path, base_url)
        payloads.update(fixture_payloads)
        external_python = _write_external_python(tmp_path / "external/bin/python")
        external_root = tmp_path / "external models"
        external_models = _write_external_models(
            external_root,
            names=("model_asr", "model_vad", "model_punc"),
            models=catalog.profiles[0].models,
        )
        for name in ("model_asr", "model_vad", "model_punc"):
            for required in component_environment.MODEL_RUNTIME_FILES[name]:
                (external_root / "models" / name / required).write_bytes(
                    fixture_payloads[f"/{name}-{required.replace('/', '_')}"]
                )
        ffmpeg, ffprobe = _write_media_tools(tmp_path)
        monkeypatch.setattr(
            component_installation,
            "probe_external_python",
            lambda _path: _external_probe_result(),
        )
        managed = tmp_path / "managed"
        cache = tmp_path / "cache"
        install_root = tmp_path / "install"
        plan = build_install_plan(
            managed,
            cache,
            catalog=catalog,
            install_root=install_root,
            external_manifest_path=external_models,
            external_python=external_python,
            ffmpeg_command=str(ffmpeg),
            ffprobe_command=str(ffprobe),
            verify_components=True,
        )

        assert plan.payload["missing_managed_groups"] == ["model_spk"]
        artifacts = plan.payload["artifacts"]
        assert isinstance(artifacts, list)
        assert [artifact["component"] for artifact in artifacts] == ["model_spk"] * len(
            component_environment.MODEL_RUNTIME_FILES["model_spk"]
        )

        phases: list[str] = []
        applied = apply_install_plan(
            managed,
            cache,
            approved_plan_hash=plan.plan_hash,
            catalog=catalog,
            install_root=install_root,
            external_manifest_path=external_models,
            external_python=external_python,
            ffmpeg_command=str(ffmpeg),
            ffprobe_command=str(ffprobe),
            python_executable=Path(sys.executable),
            verify_components=True,
            phase_callback=phases.append,
        )
        reused_plan = build_install_plan(
            managed,
            cache,
            catalog=catalog,
            install_root=install_root,
            external_manifest_path=external_models,
            external_python=external_python,
            ffmpeg_command=str(ffmpeg),
            ffprobe_command=str(ffprobe),
            verify_components=True,
        )
        reused = apply_install_plan(
            managed,
            cache,
            approved_plan_hash=reused_plan.plan_hash,
            catalog=catalog,
            install_root=install_root,
            external_manifest_path=external_models,
            external_python=external_python,
            ffmpeg_command=str(ffmpeg),
            ffprobe_command=str(ffprobe),
            python_executable=Path(sys.executable),
            verify_components=True,
        )

    assert applied.installed_groups == ("model_spk",)
    assert phases == [
        "component_installation_preparing",
        "component_installation_downloading",
        "component_installation_installing",
        "component_installation_verifying",
        "component_installation_publishing_runtime",
    ]
    assert reused.reused is True
    binding = load_runtime_binding(install_root / "runtime.json")
    assert binding.source == "persistent_managed"
    assert binding.components["asr"].source_type == "external"
    assert binding.components["campp"].source_type == "managed"
    assert requests == [None] * len(component_environment.MODEL_RUNTIME_FILES["model_spk"])
    assert (managed / "models/model_spk/campplus_cn_common.bin").is_file()
    for name in ("model_asr", "model_vad", "model_punc"):
        assert not (managed / f"models/{name}").exists()
        assert (external_root / f"models/{name}").is_dir()

    removed = uninstall_managed_components(managed)
    assert removed.removed_components == ("model_spk",)
    for name in ("model_asr", "model_vad", "model_punc"):
        assert (external_root / f"models/{name}").is_dir()


@pytest.mark.skipif(
    current_platform() != "macos" or current_architecture() != "arm64",
    reason="the reviewed external 1.3.8 profile is macOS arm64 only",
)
@pytest.mark.parametrize("failure", ["copy", "manifest"])
def test_incremental_speaker_install_failure_restores_existing_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    payloads: dict[str, bytes] = {}
    with _artifact_server(payloads) as (base_url, _requests):
        catalog, fixture_payloads = _fixture_catalog(tmp_path, base_url)
        payloads.update(fixture_payloads)
        external_python = _write_external_python(tmp_path / "external/bin/python")
        external_models = _write_external_models(
            tmp_path / "external models",
            names=("model_asr", "model_vad", "model_punc"),
            models=catalog.profiles[0].models,
        )
        ffmpeg, ffprobe = _write_media_tools(tmp_path)
        monkeypatch.setattr(
            component_installation,
            "probe_external_python",
            lambda _path: _external_probe_result(),
        )
        managed = tmp_path / "managed"
        managed.mkdir()
        original = ComponentManifest(
            components=(),
            platform=current_platform(),
            architecture=current_architecture(),
            managed_root=str(managed.resolve()),
        )
        manifest_path = managed / "component-manifest.json"
        write_component_manifest(manifest_path, original)
        original_bytes = manifest_path.read_bytes()
        cache = tmp_path / "cache"
        plan = build_install_plan(
            managed,
            cache,
            catalog=catalog,
            external_manifest_path=external_models,
            external_python=external_python,
            ffmpeg_command=str(ffmpeg),
            ffprobe_command=str(ffprobe),
            verify_components=True,
        )
        if failure == "copy":
            real_copy = component_installation.shutil.copy2

            def fail_speaker_copy(source: object, destination: object) -> None:
                if Path(destination).name == "campplus_cn_common.bin":
                    raise OSError("injected speaker copy failure")
                real_copy(source, destination)

            monkeypatch.setattr(component_installation.shutil, "copy2", fail_speaker_copy)
        else:
            real_write = component_installation.write_component_manifest
            failed = False

            def fail_manifest_once(path: Path, manifest: ComponentManifest) -> None:
                nonlocal failed
                if path == manifest_path and not failed:
                    failed = True
                    raise OSError("injected manifest save failure")
                real_write(path, manifest)

            monkeypatch.setattr(
                component_installation, "write_component_manifest", fail_manifest_once
            )

        with pytest.raises(ComponentInstallError, match="could not be installed"):
            apply_install_plan(
                managed,
                cache,
                approved_plan_hash=plan.plan_hash,
                catalog=catalog,
                external_manifest_path=external_models,
                external_python=external_python,
                ffmpeg_command=str(ffmpeg),
                ffprobe_command=str(ffprobe),
                python_executable=Path(sys.executable),
                verify_components=True,
            )

    assert manifest_path.read_bytes() == original_bytes
    assert not (managed / "models/model_spk").exists()
    assert not list(
        tmp_path.glob(f"{component_installation.MANAGED_STAGING_PREFIX}*")
    )


def test_external_probe_executes_explicit_venv_symlink_without_resolving_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _write_executable(
        tmp_path / "base/python3.11", "exit 99", windows_body="exit /b 99"
    )
    selected = tmp_path / "external venv/bin/python"
    selected.parent.mkdir(parents=True)
    _symlink_or_skip(selected, base)
    seen: list[str] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        seen.append(command[0])
        payload = json.dumps(
            {
                "python_version": "3.11",
                "funasr": "1.3.8",
                "torch": "2.12.0",
                "torchaudio": "2.11.0",
                "cuda_version": None,
                "cuda_available": False,
            }
        ).encode()
        return subprocess.CompletedProcess(
            command,
            0,
            b"ROUGHCUT-PROBE/1 " + payload + b"\n",
            b"",
        )

    monkeypatch.setattr(component_environment.subprocess, "run", run)

    result = probe_external_python(selected)

    assert seen == [str(selected.absolute())]
    assert result["interpreter"] == str(selected.absolute())


def test_unverified_external_runtime_is_rejected_with_managed_advice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    external_python = _write_external_python(
        tmp_path / "external/bin/python", funasr="1.3.9"
    )
    monkeypatch.setattr(
        component_installation,
        "probe_external_python",
        lambda _path: _external_probe_result(funasr="1.3.9"),
    )

    plan = build_install_plan(
        tmp_path / "managed",
        tmp_path / "cache",
        external_python=external_python,
        ffmpeg_command="roughcut-missing-ffmpeg",
        ffprobe_command="roughcut-missing-ffprobe",
    ).to_dict()

    components = plan["components"]
    assert isinstance(components, dict)
    funasr = components["funasr"]
    assert funasr["status"] == "install_required"
    assert funasr["attempts"][0]["status"] == "unsupported"
    assert "unverified" in funasr["attempts"][0]["detail"]


def test_unavailable_explicit_external_runtime_falls_back_to_managed_plan(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing/bin/python"

    plan = build_install_plan(
        tmp_path / "managed",
        tmp_path / "cache",
        external_python=missing,
        ffmpeg_command="roughcut-missing-ffmpeg",
        ffprobe_command="roughcut-missing-ffprobe",
    ).to_dict()

    components = plan["components"]
    assert isinstance(components, dict)
    attempt = components["funasr"]["attempts"][0]
    assert attempt["source_type"] == "external"
    assert attempt["status"] == "unavailable"
    assert plan["missing_managed_groups"][0] == "python_runtime"


def test_external_model_receipt_must_match_pinned_catalog_digest(tmp_path: Path) -> None:
    manifest_path = _write_external_models(tmp_path / "external")
    manifest = load_component_manifest(manifest_path)
    tampered = replace(
        manifest,
        components=(
            replace(
                manifest.components[0],
                verification=ComponentVerification("sha256", "0" * 64),
            ),
            *manifest.components[1:],
        ),
    )
    write_component_manifest(manifest_path, tampered)

    plan = build_install_plan(
        tmp_path / "managed",
        tmp_path / "cache",
        external_manifest_path=manifest_path,
        ffmpeg_command="roughcut-missing-ffmpeg",
        ffprobe_command="roughcut-missing-ffprobe",
    ).to_dict()

    components = plan["components"]
    assert isinstance(components, dict)
    assert components["model_asr"]["selected_source"] is None
    assert components["model_asr"]["attempts"][0]["detail"] == (
        "catalog verification does not match"
    )


def test_plan_rejects_broad_or_nested_component_roots(tmp_path: Path) -> None:
    with pytest.raises(ComponentInstallError, match="dedicated"):
        build_install_plan(Path(Path.cwd().anchor), tmp_path / "cache")
    with pytest.raises(ComponentInstallError, match="separate"):
        build_install_plan(tmp_path / "managed", tmp_path / "managed/cache")


def test_verified_cache_changes_plan_hash_and_avoids_that_download(tmp_path: Path) -> None:
    with _artifact_server({}) as (base_url, _requests):
        catalog, fixture_payloads = _fixture_catalog(tmp_path, base_url)
        profile = catalog.profiles[0]
        managed = tmp_path / "managed"
        cache = tmp_path / "cache"
        before = build_install_plan(
            managed,
            cache,
            catalog=catalog,
            ffmpeg_command="roughcut-missing-ffmpeg",
            ffprobe_command="roughcut-missing-ffprobe",
        )
        artifact = profile.runtime.artifacts[0]
        artifact_path = cache_artifact_path(cache, artifact)
        artifact_path.parent.mkdir(parents=True)
        artifact_path.write_bytes(fixture_payloads[f"/{artifact.filename}"])
        cache_receipt_path(artifact_path).write_text(
            json.dumps(
                {"schema_version": 1, "sha256": artifact.sha256, "size": artifact.size}
            ),
            encoding="utf-8",
        )

        after = build_install_plan(
            managed,
            cache,
            catalog=catalog,
            ffmpeg_command="roughcut-missing-ffmpeg",
            ffprobe_command="roughcut-missing-ffprobe",
        )

    assert after.plan_hash != before.plan_hash
    assert after.payload["download_bytes"] == before.payload["download_bytes"] - artifact.size


def test_fixture_plan_dedups_verified_and_resumable_cache_identities(
    tmp_path: Path,
) -> None:
    """Cache accounting dedup is per unique identity, not per logical artifact.

    Verified cache reduces download_bytes and increases verified_cache_bytes;
    a partial file reduces download_bytes by the resumable part and raises
    resumable_bytes; installed bytes come from the frozen profile and are never
    conflated with cache dedup.
    """
    payloads: dict[str, bytes] = {}
    with _artifact_server(payloads) as (base_url, _requests):
        catalog, fixture_payloads = _fixture_catalog(tmp_path, base_url)
        payloads.update(fixture_payloads)
        managed = tmp_path / "managed"
        cache = tmp_path / "cache"
        before = build_install_plan(
            managed,
            cache,
            catalog=catalog,
            ffmpeg_command="roughcut-missing-ffmpeg",
            ffprobe_command="roughcut-missing-ffprobe",
        )
        profile = catalog.profiles[0]
        verified = profile.runtime.artifacts[0]
        partial = profile.runtime.artifacts[1]
        verified_path = cache_artifact_path(cache, verified)
        verified_path.parent.mkdir(parents=True)
        verified_path.write_bytes(fixture_payloads[f"/{verified.filename}"])
        cache_receipt_path(verified_path).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "sha256": verified.sha256,
                    "size": verified.size,
                }
            ),
            encoding="utf-8",
        )
        partial_path = cache_artifact_path(cache, partial)
        partial_path.parent.mkdir(parents=True)
        part = partial_path.with_name(f"{partial_path.name}.part")
        part.write_bytes(b"x" * max(partial.size // 2, 1))

        after = build_install_plan(
            managed,
            cache,
            catalog=catalog,
            ffmpeg_command="roughcut-missing-ffmpeg",
            ffprobe_command="roughcut-missing-ffprobe",
        )

    assert after.payload["verified_cache_bytes"] == verified.size
    assert after.payload["resumable_bytes"] == part.stat().st_size
    assert (
        after.payload["download_bytes"]
        == before.payload["download_bytes"] - verified.size - part.stat().st_size
    )
    assert after.payload["required_artifact_bytes"] == before.payload["required_artifact_bytes"]
    assert after.payload["estimated_installed_bytes"] == before.payload["estimated_installed_bytes"]
    logical = after.payload["logical_artifact_count"]
    unique = after.payload["unique_cache_identity_count"]
    assert logical == len(before.payload["artifacts"])
    assert unique > 0 and unique <= logical
    assert after.payload["logical_artifact_bytes"] >= after.payload["unique_cache_identity_bytes"]


def test_windows_plan_returns_official_external_action(
    tmp_path: Path,
) -> None:
    plan = build_install_plan(
        tmp_path / "windows target only",
        tmp_path / "cache",
        platform="windows",
        architecture="x86_64",
        ffmpeg_command="roughcut-missing-ffmpeg",
        ffprobe_command="roughcut-missing-ffprobe",
    ).to_dict()

    assert plan["profile"] == "windows-x64-py311"
    assert plan["schema_version"] == 2
    assert plan["target"]["apply_supported_on_this_host"] is (
        current_platform() == "windows"
        and current_architecture() == "x86_64"
        and sys.version_info[:2] == (3, 11)
    )
    assert plan["download_bytes"] == 383_812_449 + 2_218_711_106
    assert plan["logical_artifact_count"] == 124
    assert plan["unique_cache_identity_count"] == 124
    assert plan["logical_artifact_bytes"] == plan["unique_cache_identity_bytes"]
    assert plan["download_bytes"] == plan["unique_cache_identity_bytes"]
    assert plan["verified_cache_bytes"] == 0
    assert plan["resumable_bytes"] == 0
    action = plan["user_actions"][0]
    assert action["manager"] == "Windows Package Manager"
    assert action["command"] == [
        "winget",
        "install",
        "--id",
        "Gyan.FFmpeg",
        "--exact",
        "--source",
        "winget",
    ]
    assert not (tmp_path / "windows target only").exists()
    assert not (tmp_path / "cache").exists()


def test_download_resumes_with_range_and_atomically_publishes(tmp_path: Path) -> None:
    payload = b"0123456789" * 100
    with _artifact_server({"/fixture.bin": payload}) as (base_url, requests):
        artifact = _artifact(f"{base_url}/fixture.bin", payload)
        destination = cache_artifact_path(tmp_path / "cache", artifact)
        partial = destination.with_name(f"{destination.name}.part")
        partial.parent.mkdir(parents=True)
        partial.write_bytes(payload[:137])

        result = download_artifact(artifact, tmp_path / "cache")

    assert result.resumed is True
    assert result.reused is False
    assert result.path.read_bytes() == payload
    assert requests == ["bytes=137-"]
    assert not partial.exists()
    assert cache_receipt_path(destination).is_file()


def test_download_safely_restarts_when_server_ignores_range(tmp_path: Path) -> None:
    payload = b"range fallback fixture"
    with _artifact_server({"/fixture.bin": payload}, support_range=False) as (
        base_url,
        requests,
    ):
        artifact = _artifact(f"{base_url}/fixture.bin", payload)
        destination = cache_artifact_path(tmp_path / "cache", artifact)
        partial = destination.with_name(f"{destination.name}.part")
        partial.parent.mkdir(parents=True)
        partial.write_bytes(payload[:5])

        result = download_artifact(artifact, tmp_path / "cache")

    assert result.resumed is False
    assert result.path.read_bytes() == payload
    assert requests == ["bytes=5-"]


def test_complete_verified_part_is_published_without_network(tmp_path: Path) -> None:
    payload = b"complete partial fixture"
    artifact = _artifact("http://127.0.0.1:9/fixture.bin", payload)
    destination = cache_artifact_path(tmp_path / "cache", artifact)
    partial = destination.with_name(f"{destination.name}.part")
    partial.parent.mkdir(parents=True)
    partial.write_bytes(payload)

    result = download_artifact(artifact, tmp_path / "cache")

    assert result.resumed is True
    assert destination.read_bytes() == payload
    assert not partial.exists()
    assert cache_receipt_path(destination).is_file()


def test_checksum_failure_is_never_published_as_verified(tmp_path: Path) -> None:
    payload = b"wrong checksum fixture"
    with _artifact_server({"/fixture.bin": payload}) as (base_url, _requests):
        artifact = _artifact(f"{base_url}/fixture.bin", payload)
        artifact = ArtifactSpec(
            component=artifact.component,
            name=artifact.name,
            version=artifact.version,
            filename=artifact.filename,
            url=artifact.url,
            license=artifact.license,
            sha256="0" * 64,
            size=artifact.size,
        )
        destination = cache_artifact_path(tmp_path / "cache", artifact)

        with pytest.raises(ComponentInstallError, match="checksum"):
            download_artifact(artifact, tmp_path / "cache")

    assert not destination.exists()
    assert not destination.with_name(f"{destination.name}.part").exists()
    assert not cache_receipt_path(destination).exists()


def test_https_download_rejects_redirect_to_plain_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"redirect fixture"
    artifact = _artifact("https://example.invalid/fixture.bin", payload)

    class RedirectedResponse:
        headers: ClassVar[dict[str, str]] = {}

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def geturl(self) -> str:
            return "http://downloads.example.invalid/fixture.bin"

        def getcode(self) -> int:
            return 200

        def read(self, _size: int) -> bytes:
            raise AssertionError("unsafe redirected response must not be read")

    monkeypatch.setattr(
        component_download.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: RedirectedResponse(),
    )

    with pytest.raises(ComponentInstallError, match="unsafe source"):
        download_artifact(artifact, tmp_path / "cache")


def test_non_loopback_component_artifact_network_fails_immediately(
    tmp_path: Path,
) -> None:
    artifact = _artifact(
        "https://artifacts.example.invalid/fixture.bin",
        b"fixture",
    )

    with pytest.raises(
        AssertionError,
        match="test component artifact network must use a loopback server",
    ):
        download_artifact(artifact, tmp_path / "cache")


def test_plan_does_not_trust_receipt_for_same_size_corrupt_cache(tmp_path: Path) -> None:
    payloads: dict[str, bytes] = {}
    with _artifact_server(payloads) as (base_url, _requests):
        catalog, fixture_payloads = _fixture_catalog(tmp_path, base_url)
        payloads.update(fixture_payloads)
        artifact = catalog.profiles[0].runtime.artifacts[0]
        cache = tmp_path / "cache"
        destination = cache_artifact_path(cache, artifact)
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"x" * artifact.size)
        cache_receipt_path(destination).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "sha256": artifact.sha256,
                    "size": artifact.size,
                }
            ),
            encoding="utf-8",
        )

        plan = build_install_plan(
            tmp_path / "managed",
            cache,
            catalog=catalog,
            ffmpeg_command="roughcut-missing-ffmpeg",
            ffprobe_command="roughcut-missing-ffprobe",
        )

    artifact_plan = next(
        item for item in plan.payload["artifacts"] if item["sha256"] == artifact.sha256
    )
    assert artifact_plan["cache_status"] == "missing"
    assert artifact_plan["resumable_bytes"] == 0


def test_cancelled_download_keeps_only_resumable_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"cancelled download fixture"
    with _artifact_server({"/fixture.bin": payload}) as (base_url, _requests):
        artifact = _artifact(f"{base_url}/fixture.bin", payload)
        destination = cache_artifact_path(tmp_path / "cache", artifact)

        def cancel(_response: object, output: object) -> None:
            output.write(payload[:4])  # type: ignore[attr-defined]
            raise KeyboardInterrupt

        monkeypatch.setattr(component_download, "_copy_response", cancel)
        with pytest.raises(KeyboardInterrupt):
            download_artifact(artifact, tmp_path / "cache")

    assert not destination.exists()
    assert destination.with_name(f"{destination.name}.part").read_bytes() == payload[:4]
    assert not cache_receipt_path(destination).exists()


def test_network_interruption_leaves_part_that_next_attempt_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"network interruption fixture"
    with _artifact_server({"/fixture.bin": payload}) as (base_url, requests):
        artifact = _artifact(f"{base_url}/fixture.bin", payload)
        destination = cache_artifact_path(tmp_path / "cache", artifact)

        def interrupt(_response: object, output: object) -> None:
            output.write(payload[:7])  # type: ignore[attr-defined]
            raise urllib.error.URLError("injected disconnect")

        with monkeypatch.context() as context:
            context.setattr(component_download, "_copy_response", interrupt)
            with pytest.raises(ComponentInstallError, match="interrupted"):
                component_download._download_artifact_once(artifact, tmp_path / "cache")
        resumed = download_artifact(artifact, tmp_path / "cache")

    assert resumed.resumed is True
    assert destination.read_bytes() == payload
    assert requests == [None, "bytes=7-"]


def test_transient_connection_interruption_retries_within_one_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"transient connection fixture"
    with _artifact_server({"/fixture.bin": payload}) as (base_url, requests):
        artifact = _artifact(f"{base_url}/fixture.bin", payload)
        real_urlopen = component_download.urllib.request.urlopen
        attempts = 0

        def transient_urlopen(*args: object, **kwargs: object) -> object:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise urllib.error.URLError("injected transient disconnect")
            return real_urlopen(*args, **kwargs)

        monkeypatch.setattr(
            component_download.urllib.request, "urlopen", transient_urlopen
        )
        monkeypatch.setattr(component_download.time, "sleep", lambda _delay: None)

        result = download_artifact(artifact, tmp_path / "cache")

    assert attempts == 2
    assert result.path.read_bytes() == payload
    assert requests == [None]


def test_clean_short_read_retries_with_exact_range_and_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"clean short read fixture"
    short_read_size = 7
    with _artifact_server(
        {"/fixture.bin": payload}, short_read_bytes=short_read_size
    ) as (base_url, requests):
        artifact = _artifact(f"{base_url}/fixture.bin", payload)
        real_urlopen = component_download.urllib.request.urlopen
        urlopen_attempts = 0
        delays: list[int] = []

        def counted_urlopen(*args: object, **kwargs: object) -> object:
            nonlocal urlopen_attempts
            urlopen_attempts += 1
            return real_urlopen(*args, **kwargs)

        monkeypatch.setattr(
            component_download.urllib.request, "urlopen", counted_urlopen
        )
        monkeypatch.setattr(
            component_download.time, "sleep", lambda delay: delays.append(delay)
        )

        result = download_artifact(artifact, tmp_path / "cache")

    destination = cache_artifact_path(tmp_path / "cache", artifact)
    partial = destination.with_name(f"{destination.name}.part")
    receipt = cache_receipt_path(destination)
    assert urlopen_attempts == 2
    assert delays == [1]
    assert requests == [None, f"bytes={short_read_size}-"]
    assert result.resumed is True
    assert result.path == destination
    assert destination.stat().st_size == len(payload)
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == artifact.sha256
    assert json.loads(receipt.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "sha256": artifact.sha256,
        "size": artifact.size,
    }
    assert not partial.exists()


def test_body_read_oserror_retries_with_exact_range_and_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"body read interruption fixture"
    partial_size = 7

    class BodyReadInterruptionResponse:
        def __init__(self, response: Any) -> None:
            self._response = response
            self._first_read = True

        def __enter__(self) -> Self:
            self._response.__enter__()
            return self

        def __exit__(self, *args: object) -> Any:
            return self._response.__exit__(*args)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._response, name)

        def read(self, _size: int) -> bytes:
            if self._first_read:
                self._first_read = False
                return self._response.read(partial_size)
            raise ConnectionResetError("injected body read disconnect")

    with _artifact_server({"/fixture.bin": payload}) as (base_url, requests):
        artifact = _artifact(f"{base_url}/fixture.bin", payload)
        real_urlopen = component_download.urllib.request.urlopen
        attempts = 0
        delays: list[int] = []

        def interrupted_urlopen(*args: object, **kwargs: object) -> object:
            nonlocal attempts
            attempts += 1
            response = real_urlopen(*args, **kwargs)
            if attempts == 1:
                return BodyReadInterruptionResponse(response)
            return response

        monkeypatch.setattr(
            component_download.urllib.request, "urlopen", interrupted_urlopen
        )
        monkeypatch.setattr(
            component_download.time, "sleep", lambda delay: delays.append(delay)
        )

        result = download_artifact(artifact, tmp_path / "cache")

    destination = cache_artifact_path(tmp_path / "cache", artifact)
    partial = destination.with_name(f"{destination.name}.part")
    receipt = cache_receipt_path(destination)
    assert attempts == 2
    assert delays == [1]
    assert requests == [None, f"bytes={partial_size}-"]
    assert result.resumed is True
    assert destination.read_bytes() == payload
    assert destination.stat().st_size == artifact.size
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == artifact.sha256
    assert json.loads(receipt.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "sha256": artifact.sha256,
        "size": artifact.size,
    }
    assert not partial.exists()


def test_body_incomplete_read_retries_with_exact_range_and_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"incomplete body read fixture"
    first_chunk_size = 5
    exception_partial_size = 4
    resume_offset = first_chunk_size + exception_partial_size

    class IncompleteBodyResponse:
        def __init__(self, response: Any) -> None:
            self._response = response
            self._reads = 0

        def __enter__(self) -> Self:
            self._response.__enter__()
            return self

        def __exit__(self, *args: object) -> Any:
            return self._response.__exit__(*args)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._response, name)

        def read(self, _size: int) -> bytes:
            self._reads += 1
            if self._reads == 1:
                return self._response.read(first_chunk_size)
            partial = self._response.read(exception_partial_size)
            raise http.client.IncompleteRead(
                partial,
                len(payload) - resume_offset,
            )

    with _artifact_server({"/fixture.bin": payload}) as (base_url, requests):
        artifact = _artifact(f"{base_url}/fixture.bin", payload)
        real_urlopen = component_download.urllib.request.urlopen
        attempts = 0
        delays: list[int] = []

        def interrupted_urlopen(*args: object, **kwargs: object) -> object:
            nonlocal attempts
            attempts += 1
            response = real_urlopen(*args, **kwargs)
            if attempts == 1:
                return IncompleteBodyResponse(response)
            return response

        monkeypatch.setattr(
            component_download.urllib.request, "urlopen", interrupted_urlopen
        )
        monkeypatch.setattr(
            component_download.time, "sleep", lambda delay: delays.append(delay)
        )

        result = download_artifact(artifact, tmp_path / "cache")

    destination = cache_artifact_path(tmp_path / "cache", artifact)
    partial = destination.with_name(f"{destination.name}.part")
    assert attempts == 2
    assert delays == [1]
    assert requests == [None, f"bytes={resume_offset}-"]
    assert result.resumed is True
    assert destination.read_bytes() == payload
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == artifact.sha256
    assert cache_receipt_path(destination).is_file()
    assert not partial.exists()


def test_receipt_orphan_does_not_block_new_unique_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"verified artifact with orphan receipt temporary"
    artifact = _artifact("http://127.0.0.1:9/fixture.bin", payload)
    cache = tmp_path / "cache"
    destination = cache_artifact_path(cache, artifact)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(payload)
    first_token = "0" * component_installation.OWNED_TEMP_TOKEN_HEX_LENGTH
    second_token = "1" * component_installation.OWNED_TEMP_TOKEN_HEX_LENGTH
    orphan = destination.parent / (
        component_installation.CACHE_RECEIPT_TEMP_PREFIX
        + first_token
        + component_installation.CACHE_RECEIPT_TEMP_SUFFIX
    )
    orphan.write_bytes(b"foreign regular orphan")
    tokens = iter((first_token, second_token))

    monkeypatch.setattr(
        component_download.secrets,
        "token_hex",
        lambda size: next(tokens)
        if size == component_installation.OWNED_TEMP_TOKEN_BYTES
        else "",
    )

    result = download_artifact(artifact, cache)

    assert result.reused is True
    assert orphan.read_bytes() == b"foreign regular orphan"
    assert cache_receipt_path(destination).is_file()
    assert not (
        destination.parent
        / (
            component_installation.CACHE_RECEIPT_TEMP_PREFIX
            + second_token
            + component_installation.CACHE_RECEIPT_TEMP_SUFFIX
        )
    ).exists()


def test_receipt_writers_use_distinct_bounded_temporary_identities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = _artifact("http://127.0.0.1:9/fixture.bin", b"fixture")
    receipt = tmp_path / "fixture.verified.json"
    tokens = iter(
        (
            "2" * component_installation.OWNED_TEMP_TOKEN_HEX_LENGTH,
            "3" * component_installation.OWNED_TEMP_TOKEN_HEX_LENGTH,
        )
    )
    sources: list[Path] = []
    real_replace = component_download.os.replace

    monkeypatch.setattr(
        component_download.secrets,
        "token_hex",
        lambda size: next(tokens)
        if size == component_installation.OWNED_TEMP_TOKEN_BYTES
        else "",
    )

    def capture_replace(source: object, destination: object) -> None:
        sources.append(Path(source))
        real_replace(source, destination)

    monkeypatch.setattr(component_download.os, "replace", capture_replace)

    component_download._write_receipt(receipt, artifact)
    component_download._write_receipt(receipt, artifact)

    assert len(sources) == 2
    assert sources[0] != sources[1]
    assert all(
        path.name.startswith(component_installation.CACHE_RECEIPT_TEMP_PREFIX)
        and path.name.endswith(component_installation.CACHE_RECEIPT_TEMP_SUFFIX)
        and len(path.name)
        == len(component_installation.CACHE_RECEIPT_TEMP_PREFIX)
        + component_installation.OWNED_TEMP_TOKEN_HEX_LENGTH
        + len(component_installation.CACHE_RECEIPT_TEMP_SUFFIX)
        for path in sources
    )


def test_managed_staging_uses_distinct_bounded_core_owned_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tokens = iter(
        (
            "4" * component_installation.OWNED_TEMP_TOKEN_HEX_LENGTH,
            "5" * component_installation.OWNED_TEMP_TOKEN_HEX_LENGTH,
        )
    )
    monkeypatch.setattr(
        component_installation.secrets,
        "token_hex",
        lambda size: next(tokens)
        if size == component_installation.OWNED_TEMP_TOKEN_BYTES
        else "",
    )

    first = component_installation._create_managed_staging(tmp_path)
    second = component_installation._create_managed_staging(tmp_path)

    assert first != second
    assert all(
        path.name.startswith(component_installation.MANAGED_STAGING_PREFIX)
        and len(path.name)
        == len(component_installation.MANAGED_STAGING_PREFIX)
        + component_installation.OWNED_TEMP_TOKEN_HEX_LENGTH
        for path in (first, second)
    )


@pytest.mark.parametrize(
    "message",
    [
        pytest.param("component download checksum differs", id="checksum"),
        pytest.param(
            "component download redirected to an unsafe source", id="http_policy"
        ),
        pytest.param("download server returned an invalid byte range", id="range"),
        pytest.param("component download could not be written", id="disk_write"),
        pytest.param(
            "verified cache artifact could not be published", id="atomic_publish"
        ),
    ],
)
def test_download_does_not_retry_non_interruption_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    artifact = _artifact("http://127.0.0.1:9/fixture.bin", b"fixture")
    calls = 0

    def fail_once(_artifact: ArtifactSpec, _cache: Path) -> object:
        nonlocal calls
        calls += 1
        raise ComponentInstallError(message)

    monkeypatch.setattr(component_download, "_download_artifact_once", fail_once)

    with pytest.raises(ComponentInstallError) as error:
        download_artifact(artifact, tmp_path / "cache")

    assert str(error.value) == message
    assert calls == 1


def test_windows_runtime_installs_pinned_setuptools_before_source_archives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = load_release_catalog().profile_for("windows", "x86_64")
    calls: list[list[str]] = []

    def record(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(component_installation.subprocess, "run", record)
    component_installation._install_runtime_artifacts(
        Path("python.exe"),
        tmp_path / "venv",
        tmp_path / "cache",
        profile,
        environment={},
    )

    setuptools = next(
        artifact for artifact in profile.runtime.artifacts if artifact.name == "setuptools"
    )
    cache = tmp_path / "cache"
    expected = cache_artifact_path(cache, setuptools)
    expected_inputs = [
        str(cache_artifact_path(cache, artifact))
        for artifact in profile.runtime.artifacts
    ]
    assert len(calls) == 2
    assert setuptools.filename.endswith(".whl")
    assert len(setuptools.sha256) == 64
    assert all(value in "0123456789abcdef" for value in setuptools.sha256)
    assert calls[0][-1] == str(expected)
    assert "--no-index" in calls[0]
    assert "--no-deps" in calls[0]
    assert "--no-build-isolation" not in calls[0]
    assert "--progress-bar" in calls[0] and "off" in calls[0]
    assert "--no-index" in calls[1]
    assert "--no-deps" in calls[1]
    assert "--no-build-isolation" in calls[1]
    assert "--progress-bar" in calls[1] and "off" in calls[1]
    assert calls[1][-len(expected_inputs) :] == expected_inputs
    assert all(
        Path(value).parent.name == artifact.sha256
        for value, artifact in zip(expected_inputs, profile.runtime.artifacts, strict=True)
    )


def test_managed_runtime_probe_allows_windows_cold_imports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = tmp_path / "venv"
    interpreter = runtime_root / "Scripts" / "python.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"fixture interpreter")
    interpreter.chmod(0o755)
    lock = tmp_path / "locks" / "windows.lock"
    lock.parent.mkdir()
    lock.write_bytes(b"fixture lock")
    runtime = PythonRuntimeRecord(
        root="venv",
        interpreter="venv/Scripts/python.exe",
        dependency_lock="locks/windows.lock",
        lock_origin="fixture",
        lock_verification=ComponentVerification("sha256", component_digest(lock)),
        device="cpu",
        python_version="3.11",
    )
    manifest = ComponentManifest(
        components=(),
        platform="windows",
        architecture="x86_64",
        managed_root=r"C:\Roughcut Managed",
        schema_version=2,
        python_runtime=runtime,
    )
    observed_timeout = 0

    def probe(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal observed_timeout
        observed_timeout = int(kwargs["timeout"])
        payload = {
            "python_version": "3.11",
            "funasr": "1.3.14",
            "torch": "2.6.0+cpu",
            "torchaudio": "2.6.0+cpu",
            "cuda_version": None,
            "cuda_available": False,
        }
        return subprocess.CompletedProcess(
            command,
            0,
            b"ROUGHCUT-PROBE/1 " + json.dumps(payload).encode() + b"\n",
            b"",
        )

    monkeypatch.setattr(component_environment.subprocess, "run", probe)

    _versions, error = component_environment._probe_managed_python_runtime(
        manifest, managed_root_override=tmp_path
    )

    assert error is None
    assert observed_timeout == 180


def _managed_probe_manifest(root: Path) -> ComponentManifest:
    interpreter = root / "venv/Scripts/python.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"fixture interpreter")
    interpreter.chmod(0o755)
    lock = root / "locks/windows.lock"
    lock.parent.mkdir()
    lock.write_bytes(b"fixture lock")
    return ComponentManifest(
        components=(),
        platform="windows",
        architecture="x86_64",
        managed_root=r"C:\Roughcut Managed",
        schema_version=2,
        python_runtime=PythonRuntimeRecord(
            root="venv",
            interpreter="venv/Scripts/python.exe",
            dependency_lock="locks/windows.lock",
            lock_origin="fixture",
            lock_verification=ComponentVerification("sha256", component_digest(lock)),
            device="cpu",
            python_version="3.11",
        ),
    )


def test_managed_runtime_probe_reports_180_second_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _managed_probe_manifest(tmp_path)
    observed_timeout = 0

    def timeout(command: list[str], **kwargs: object) -> object:
        nonlocal observed_timeout
        observed_timeout = int(kwargs["timeout"])
        raise subprocess.TimeoutExpired(command, observed_timeout)

    monkeypatch.setattr(component_environment.subprocess, "run", timeout)

    versions, error = component_environment._probe_managed_python_runtime(
        manifest, managed_root_override=tmp_path
    )

    assert versions == {}
    assert observed_timeout == 180
    assert error == "Python runtime probe timed out"


def test_managed_runtime_probe_reports_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _managed_probe_manifest(tmp_path)
    observed_timeout = 0

    def fail(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal observed_timeout
        observed_timeout = int(kwargs["timeout"])
        return subprocess.CompletedProcess(command, 23, b"", b"native failure")

    monkeypatch.setattr(component_environment.subprocess, "run", fail)

    versions, error = component_environment._probe_managed_python_runtime(
        manifest, managed_root_override=tmp_path
    )

    assert versions == {}
    assert observed_timeout == 180
    assert error == "Python runtime probe failed with a non-zero exit"


def test_external_runtime_probe_reports_180_second_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    interpreter = _write_executable(
        tmp_path / "external/python", "exit 0", windows_body="exit /b 0"
    )
    observed_timeout = 0

    def timeout(command: list[str], **kwargs: object) -> object:
        nonlocal observed_timeout
        observed_timeout = int(kwargs["timeout"])
        raise subprocess.TimeoutExpired(command, observed_timeout)

    monkeypatch.setattr(component_environment.subprocess, "run", timeout)

    with pytest.raises(
        ComponentInstallError,
        match="external Python runtime probe failed",
    ):
        probe_external_python(interpreter)

    assert observed_timeout == 180


def test_external_runtime_probe_reports_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    interpreter = _write_executable(
        tmp_path / "external/python", "exit 0", windows_body="exit /b 0"
    )
    observed_timeout = 0

    def fail(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal observed_timeout
        observed_timeout = int(kwargs["timeout"])
        return subprocess.CompletedProcess(command, 29, b"", b"native failure")

    monkeypatch.setattr(component_environment.subprocess, "run", fail)

    with pytest.raises(
        ComponentInstallError,
        match="external Python runtime probe failed",
    ):
        probe_external_python(interpreter)

    assert observed_timeout == 180


def test_cache_atomic_publish_failure_never_creates_available_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"atomic cache publish fixture"
    with _artifact_server({"/fixture.bin": payload}) as (base_url, _requests):
        artifact = _artifact(f"{base_url}/fixture.bin", payload)
        destination = cache_artifact_path(tmp_path / "cache", artifact)
        real_replace = component_download.os.replace

        def fail_publish(source: object, target: object) -> None:
            if Path(target) == destination:
                raise OSError("injected publish failure")
            real_replace(source, target)

        monkeypatch.setattr(component_download.os, "replace", fail_publish)
        with pytest.raises(ComponentInstallError, match="could not be published"):
            download_artifact(artifact, tmp_path / "cache")

    assert not destination.exists()
    assert not cache_receipt_path(destination).exists()
    assert destination.with_name(f"{destination.name}.part").is_file()


def test_cache_directory_symlink_cannot_redirect_download_outside_root(
    tmp_path: Path,
) -> None:
    payload = b"cache path escape fixture"
    outside = tmp_path / "outside"
    outside.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    _symlink_or_skip(cache / "artifacts", outside, target_is_directory=True)
    with _artifact_server({"/fixture.bin": payload}) as (base_url, requests):
        artifact = _artifact(f"{base_url}/fixture.bin", payload)

        with pytest.raises(ComponentInstallError, match="cache path is unsafe"):
            download_artifact(artifact, cache)

    assert requests == []
    assert list(outside.iterdir()) == []


def test_cache_part_symlink_is_rejected_without_modifying_target(tmp_path: Path) -> None:
    payload = b"cache partial escape fixture"
    artifact = _artifact("http://127.0.0.1:9/fixture.bin", payload)
    cache = tmp_path / "cache"
    destination = cache_artifact_path(cache, artifact)
    destination.parent.mkdir(parents=True)
    outside = tmp_path / "outside.part"
    outside.write_bytes(b"do not modify")
    _symlink_or_skip(destination.with_name(f"{destination.name}.part"), outside)

    with pytest.raises(ComponentInstallError, match="artifact path is unsafe"):
        download_artifact(artifact, cache)

    assert outside.read_bytes() == b"do not modify"


def test_apply_rejects_stale_hash_before_network_or_managed_write(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    cache = tmp_path / "cache"
    install_root = tmp_path / "install"
    plan = build_install_plan(
        managed,
        cache,
        install_root=install_root,
        ffmpeg_command="roughcut-missing-ffmpeg",
        ffprobe_command="roughcut-missing-ffprobe",
    )
    artifact = load_release_catalog().profile_for("macos", "arm64").runtime.artifacts[0]
    destination = cache_artifact_path(cache, artifact)
    destination.parent.mkdir(parents=True)
    destination.with_name(f"{destination.name}.part").write_bytes(b"x")

    with pytest.raises(ComponentInstallError, match="stale"):
        apply_install_plan(
            managed,
            cache,
            install_root=install_root,
            approved_plan_hash=plan.plan_hash,
            ffmpeg_command="roughcut-missing-ffmpeg",
            ffprobe_command="roughcut-missing-ffprobe",
            verify_components=True,
        )

    assert not managed.exists()
    assert not install_root.exists()


def test_apply_preflight_plan_requires_exact_catalog_without_reloading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed = tmp_path / "managed"
    cache = tmp_path / "cache"
    catalog = load_release_catalog()
    plan = build_install_plan(
        managed,
        cache,
        catalog=catalog,
        ffmpeg_command="roughcut-missing-ffmpeg",
        ffprobe_command="roughcut-missing-ffprobe",
        verify_components=True,
    )
    catalog_loads = 0

    def must_not_reload() -> ReleaseCatalog:
        nonlocal catalog_loads
        catalog_loads += 1
        raise AssertionError("preflight apply must not reload the catalog")

    monkeypatch.setattr(
        component_installation,
        "load_release_catalog",
        must_not_reload,
    )

    with pytest.raises(StaleApprovedPlanError, match="without its release catalog"):
        apply_install_plan(
            managed,
            cache,
            approved_plan_hash=plan.plan_hash,
            preflight_plan=plan,
            ffmpeg_command="roughcut-missing-ffmpeg",
            ffprobe_command="roughcut-missing-ffprobe",
            verify_components=True,
        )

    assert catalog_loads == 0
    assert not managed.exists()
    assert not cache.exists()


def test_apply_rejects_insufficient_disk_before_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed = tmp_path / "managed"
    cache = tmp_path / "cache"
    plan = build_install_plan(
        managed,
        cache,
        ffmpeg_command="roughcut-missing-ffmpeg",
        ffprobe_command="roughcut-missing-ffprobe",
        verify_components=True,
    )
    monkeypatch.setattr(component_installation, "_available_bytes", lambda _path: 0)

    with pytest.raises(ComponentInstallError, match="disk space"):
        apply_install_plan(
            managed,
            cache,
            approved_plan_hash=plan.plan_hash,
            ffmpeg_command="roughcut-missing-ffmpeg",
            ffprobe_command="roughcut-missing-ffprobe",
            verify_components=True,
        )

    assert not managed.exists()
    assert not cache.exists()


def test_apply_checks_cache_volume_when_roots_are_on_different_volumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed_parent = tmp_path / "managed-volume"
    cache_parent = tmp_path / "cache-volume"
    managed_parent.mkdir()
    cache_parent.mkdir()
    managed = managed_parent / "managed"
    cache = cache_parent / "cache"
    plan = build_install_plan(
        managed,
        cache,
        ffmpeg_command="roughcut-missing-ffmpeg",
        ffprobe_command="roughcut-missing-ffprobe",
        verify_components=True,
    )
    monkeypatch.setattr(component_installation, "_same_storage_volume", lambda *_args: False)
    monkeypatch.setattr(
        component_installation,
        "_available_bytes",
        lambda path: 0 if path == cache_parent else 10**15,
    )

    with pytest.raises(ComponentInstallError, match="disk space"):
        apply_install_plan(
            managed,
            cache,
            approved_plan_hash=plan.plan_hash,
            ffmpeg_command="roughcut-missing-ffmpeg",
            ffprobe_command="roughcut-missing-ffprobe",
            verify_components=True,
        )

    assert not managed.exists()
    assert not cache.exists()


def test_apply_preserves_full_verification_mode_in_approved_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed = tmp_path / "managed"
    cache = tmp_path / "cache"
    plan = build_install_plan(
        managed,
        cache,
        ffmpeg_command="roughcut-missing-ffmpeg",
        ffprobe_command="roughcut-missing-ffprobe",
        verify_components=True,
    )

    with pytest.raises(ComponentInstallError, match="stale"):
        apply_install_plan(
            managed,
            cache,
            approved_plan_hash=plan.plan_hash,
            ffmpeg_command="roughcut-missing-ffmpeg",
            ffprobe_command="roughcut-missing-ffprobe",
        )

    monkeypatch.setattr(component_installation, "_available_bytes", lambda _path: 10**15)

    def reached_download(_artifact: object, _cache: object) -> None:
        raise ComponentInstallError("approved full plan reached download")

    monkeypatch.setattr(component_download, "download_artifact", reached_download)
    with pytest.raises(ComponentInstallError, match="reached download"):
        apply_install_plan(
            managed,
            cache,
            approved_plan_hash=plan.plan_hash,
            ffmpeg_command="roughcut-missing-ffmpeg",
            ffprobe_command="roughcut-missing-ffprobe",
            verify_components=True,
        )


def test_plan_apply_full_health_reuse_and_uninstall_with_fake_artifacts(
    tmp_path: Path,
) -> None:
    payloads: dict[str, bytes] = {}
    with _artifact_server(payloads) as (base_url, requests):
        catalog, fixture_payloads = _fixture_catalog(tmp_path, base_url)
        payloads.update(fixture_payloads)
        managed = tmp_path / "用户 数据/managed components"
        cache = tmp_path / "用户 数据/download cache"
        install_root = tmp_path / "用户 数据/Roughcut install"
        ffmpeg, ffprobe = _write_media_tools(tmp_path)
        plan = build_install_plan(
            managed,
            cache,
            catalog=catalog,
            install_root=install_root,
            ffmpeg_command=str(ffmpeg),
            ffprobe_command=str(ffprobe),
            verify_components=True,
        )

        applied = apply_install_plan(
            managed,
            cache,
            approved_plan_hash=plan.plan_hash,
            catalog=catalog,
            install_root=install_root,
            ffmpeg_command=str(ffmpeg),
            ffprobe_command=str(ffprobe),
            python_executable=Path(sys.executable),
            verify_components=True,
        )
        health = build_install_plan(
            managed,
            cache,
            catalog=catalog,
            install_root=install_root,
            ffmpeg_command=str(ffmpeg),
            ffprobe_command=str(ffprobe),
            verify_components=True,
        )
        reused = apply_install_plan(
            managed,
            cache,
            approved_plan_hash=health.plan_hash,
            catalog=catalog,
            install_root=install_root,
            ffmpeg_command=str(ffmpeg),
            ffprobe_command=str(ffprobe),
            python_executable=Path(sys.executable),
            verify_components=True,
        )

    assert applied.installed_groups == (
        "python_runtime",
        "model_asr",
        "model_vad",
        "model_punc",
        "model_spk",
    )
    assert reused.reused is True
    assert applied.runtime_binding is not None
    assert applied.runtime_binding.published is True
    assert reused.runtime_binding is not None
    assert reused.runtime_binding.reused is True
    assert health.payload["missing_managed_groups"] == []
    assert health.payload["artifacts"] == []
    assert len(requests) == 21
    manifest = json.loads(
        (managed / "component-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["schema_version"] == 2
    assert manifest["python_runtime"]["python_version"] == "3.11"
    expected_interpreter = (
        "venv/Scripts/python.exe" if sys.platform == "win32" else "venv/bin/python"
    )
    assert manifest["python_runtime"]["interpreter"] == expected_interpreter
    binding = load_runtime_binding(install_root / "runtime.json")
    assert binding.source == "persistent_managed"
    assert set(binding.components) == {"asr", "vad", "punc", "campp"}

    removed = uninstall_managed_components(managed)

    assert removed.uninstalled is True
    assert not (managed / "component-manifest.json").exists()
    assert cache.is_dir()


def test_install_action_rechecks_approved_runtime_state_before_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payloads: dict[str, bytes] = {}
    with _artifact_server(payloads) as (base_url, _requests):
        catalog, fixture_payloads = _fixture_catalog(tmp_path, base_url)
        payloads.update(fixture_payloads)
        managed = tmp_path / "managed"
        cache = tmp_path / "cache"
        install_root = tmp_path / "install"
        runtime_path = install_root / "runtime.json"
        ffmpeg, ffprobe = _write_media_tools(tmp_path)
        plan = build_install_plan(
            managed,
            cache,
            catalog=catalog,
            install_root=install_root,
            ffmpeg_command=str(ffmpeg),
            ffprobe_command=str(ffprobe),
            verify_components=True,
        )
        real_install = component_installation._install_staged_components
        concurrent_payload = b'{"schema_version": 99, "owner": "concurrent"}\n'

        def install_then_change_runtime(
            root: Path,
            download_cache: Path,
            selected_profile: ReleaseProfile,
                groups: tuple[str, ...],
                *,
                python_executable: Path,
                ffmpeg_command: str,
            ) -> ComponentManifest:
            result = real_install(
                root,
                download_cache,
                selected_profile,
                    groups,
                    python_executable=python_executable,
                    ffmpeg_command=ffmpeg_command,
                )
            install_root.mkdir(parents=True, exist_ok=True)
            runtime_path.write_bytes(concurrent_payload)
            return result

        monkeypatch.setattr(
            component_installation,
            "_install_staged_components",
            install_then_change_runtime,
        )

        with pytest.raises(
            ComponentInstallError,
            match="runtime binding publication failed",
        ):
            apply_install_plan(
                managed,
                cache,
                approved_plan_hash=plan.plan_hash,
                catalog=catalog,
                install_root=install_root,
                ffmpeg_command=str(ffmpeg),
                ffprobe_command=str(ffprobe),
                python_executable=Path(sys.executable),
                verify_components=True,
            )

    assert runtime_path.read_bytes() == concurrent_payload
    assert (managed / "component-manifest.json").is_file()
    assert (managed / "models/model_spk/campplus_cn_common.bin").is_file()
    assert not list(install_root.glob(".runtime.json.*.tmp"))


def test_apply_installs_only_newly_missing_model_into_existing_managed_root(
    tmp_path: Path,
) -> None:
    payloads: dict[str, bytes] = {}
    with _artifact_server(payloads) as (base_url, requests):
        catalog, fixture_payloads = _fixture_catalog(tmp_path, base_url)
        payloads.update(fixture_payloads)
        profile = catalog.profiles[0]
        external_model = tmp_path / "external/model_asr"
        external_model.mkdir(parents=True)
        for required in component_environment.MODEL_RUNTIME_FILES["model_asr"]:
            (external_model / required).write_bytes(
                f"fixture model payload 0 {required}".encode()
            )
        external_manifest = ComponentManifest(
            components=(
                replace(
                    build_external_component(
                        "model_asr",
                        external_model,
                        version=profile.models["model_asr"].revision,
                        origin="https://modelscope.cn/models/fixture/model_asr",
                        license="Apache-2.0",
                    ),
                    verification=ComponentVerification(
                        "sha256", profile.models["model_asr"].directory_sha256
                    ),
                ),
            ),
            platform=current_platform(),
            architecture=current_architecture(),
        )
        external_manifest_path = tmp_path / "external-model.json"
        write_component_manifest(external_manifest_path, external_manifest)
        managed = tmp_path / "managed"
        cache = tmp_path / "cache"
        ffmpeg, ffprobe = _write_media_tools(tmp_path)
        first_plan = build_install_plan(
            managed,
            cache,
            catalog=catalog,
            external_manifest_path=external_manifest_path,
            ffmpeg_command=str(ffmpeg),
            ffprobe_command=str(ffprobe),
            verify_components=True,
        )
        first = apply_install_plan(
            managed,
            cache,
            approved_plan_hash=first_plan.plan_hash,
            catalog=catalog,
            external_manifest_path=external_manifest_path,
            ffmpeg_command=str(ffmpeg),
            ffprobe_command=str(ffprobe),
            python_executable=Path(sys.executable),
            verify_components=True,
        )
        runtime_before = _stable_file_identity(_managed_interpreter_path(managed))
        vad_before = (managed / "models/model_vad/model.pt").read_bytes()

        second_plan = build_install_plan(
            managed,
            cache,
            catalog=catalog,
            ffmpeg_command=str(ffmpeg),
            ffprobe_command=str(ffprobe),
            verify_components=True,
        )
        second = apply_install_plan(
            managed,
            cache,
            approved_plan_hash=second_plan.plan_hash,
            catalog=catalog,
            ffmpeg_command=str(ffmpeg),
            ffprobe_command=str(ffprobe),
            python_executable=Path(sys.executable),
            verify_components=True,
        )

    assert first.installed_groups == (
        "python_runtime",
        "model_vad",
        "model_punc",
        "model_spk",
    )
    assert second.installed_groups == ("model_asr",)
    assert _stable_file_identity(_managed_interpreter_path(managed)) == runtime_before
    assert (managed / "models/model_vad/model.pt").read_bytes() == vad_before
    assert len(requests) == 21


def test_managed_atomic_publish_failure_leaves_no_manifest_or_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payloads: dict[str, bytes] = {}
    with _artifact_server(payloads) as (base_url, _requests):
        catalog, fixture_payloads = _fixture_catalog(tmp_path, base_url)
        payloads.update(fixture_payloads)
        managed = tmp_path / "managed publish failure"
        cache = tmp_path / "cache"
        ffmpeg, ffprobe = _write_media_tools(tmp_path)
        plan = build_install_plan(
            managed,
            cache,
            catalog=catalog,
            ffmpeg_command=str(ffmpeg),
            ffprobe_command=str(ffprobe),
            verify_components=True,
        )
        real_replace = component_installation.os.replace

        def fail_final_publish(source: object, destination: object) -> None:
            if Path(destination) == managed.resolve():
                raise OSError("injected final publish failure")
            real_replace(source, destination)

        monkeypatch.setattr(component_installation.os, "replace", fail_final_publish)
        with pytest.raises(ComponentInstallError, match="could not be installed"):
            apply_install_plan(
                managed,
                cache,
                approved_plan_hash=plan.plan_hash,
                catalog=catalog,
                ffmpeg_command=str(ffmpeg),
                ffprobe_command=str(ffprobe),
                python_executable=Path(sys.executable),
                verify_components=True,
            )

    assert not managed.exists()
    assert not list(
        tmp_path.glob(f"{component_installation.MANAGED_STAGING_PREFIX}*")
    )
    assert list(cache.rglob("*.verified.json"))


def test_post_publish_verification_failure_rolls_back_new_managed_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payloads: dict[str, bytes] = {}
    with _artifact_server(payloads) as (base_url, _requests):
        catalog, fixture_payloads = _fixture_catalog(tmp_path, base_url)
        payloads.update(fixture_payloads)
        managed = tmp_path / "managed verify failure"
        cache = tmp_path / "cache"
        ffmpeg, ffprobe = _write_media_tools(tmp_path)
        plan = build_install_plan(
            managed,
            cache,
            catalog=catalog,
            ffmpeg_command=str(ffmpeg),
            ffprobe_command=str(ffprobe),
            verify_components=True,
        )

        def fail_verification(_manifest: object, _profile: object) -> None:
            raise ComponentInstallError("injected full verification failure")

        monkeypatch.setattr(
            component_installation, "_verify_published_manifest", fail_verification
        )
        with pytest.raises(ComponentInstallError, match="full verification"):
            apply_install_plan(
                managed,
                cache,
                approved_plan_hash=plan.plan_hash,
                catalog=catalog,
                ffmpeg_command=str(ffmpeg),
                ffprobe_command=str(ffprobe),
                python_executable=Path(sys.executable),
                verify_components=True,
            )

    assert not managed.exists()
    assert not list(
        tmp_path.glob(f"{component_installation.MANAGED_STAGING_PREFIX}*")
    )


def test_cancelled_apply_leaves_no_managed_root_or_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payloads: dict[str, bytes] = {}
    with _artifact_server(payloads) as (base_url, _requests):
        catalog, fixture_payloads = _fixture_catalog(tmp_path, base_url)
        payloads.update(fixture_payloads)
        managed = tmp_path / "managed cancelled"
        cache = tmp_path / "cache"
        ffmpeg, ffprobe = _write_media_tools(tmp_path)
        plan = build_install_plan(
            managed,
            cache,
            catalog=catalog,
            ffmpeg_command=str(ffmpeg),
            ffprobe_command=str(ffprobe),
            verify_components=True,
        )

        def cancel_model(
            _staging: Path,
            _cache: Path,
            _profile: ReleaseProfile,
            _name: str,
        ) -> object:
            raise KeyboardInterrupt

        monkeypatch.setattr(component_installation, "_stage_model", cancel_model)
        with pytest.raises(KeyboardInterrupt):
            apply_install_plan(
                managed,
                cache,
                approved_plan_hash=plan.plan_hash,
                catalog=catalog,
                ffmpeg_command=str(ffmpeg),
                ffprobe_command=str(ffprobe),
                python_executable=Path(sys.executable),
                verify_components=True,
            )

    assert not managed.exists()
    assert not list(
        tmp_path.glob(f"{component_installation.MANAGED_STAGING_PREFIX}*")
    )
