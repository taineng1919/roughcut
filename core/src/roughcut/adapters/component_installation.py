"""Pinned media-component planning and explicit managed installation."""

from __future__ import annotations

import hashlib
import json
import math
import ntpath
import os
import re
import secrets
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlparse

from roughcut.adapters.child_budget import ChildBudget
from roughcut.adapters.component_environment import (
    COMPONENT_MANIFEST_FILENAME,
    PYTHON_COMPONENT_NAMES,
    PYTHON_RUNTIME_PROBE_TIMEOUT_SECONDS,
    VIRTUAL_ENVIRONMENT_VARIABLES,
    ComponentError,
    ComponentManifest,
    ComponentRecord,
    ComponentVerification,
    PythonRuntimeRecord,
    _parse_probe_frame,
    component_digest,
    diagnose_components,
    load_component_manifest,
    probe_python_runtime,
    write_component_manifest,
)
from roughcut.adapters.ffmpeg_environment import CommandDiagnostic, diagnose_ffmpeg
from roughcut.adapters.runtime_binding import (
    RUNTIME_PUBLISH_REASON_CODES,
    RuntimeAlignmentPython,
    RuntimeBindingError,
    RuntimePublishResult,
    audalign_distribution_versions_for,
    audalign_distributions_for,
    bbc_audio_offset_finder_distribution_versions_for,
    bbc_audio_offset_finder_distributions_for,
    binding_from_install_plan,
    publish_runtime_binding,
    runtime_binding_path,
    runtime_binding_status,
)

CATALOG_ROOT = Path(__file__).resolve().parents[1] / "component_catalog"
PLAN_SCHEMA_VERSION = 2
CATALOG_SCHEMA_VERSION = 2
MODEL_COMPONENT_NAMES = ("model_asr", "model_vad", "model_punc", "model_spk")
LEGACY_EXTERNAL_RUNTIME_PROFILE_ID = "funasr-1.3.8-cpu-py311"
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


class ComponentInstallError(RuntimeError):
    """Raised when a pinned install cannot be planned or safely applied."""

    failure_responsibility = "roughcut_component_installer"
    failure_action = "install_components"


class StaleApprovedPlanError(ComponentInstallError):
    """Raised for an approved-plan input that no longer matches the plan."""

    failure_responsibility = "user_input"
    failure_action = "validate_approved_full_plan"


class RuntimePublicationError(ComponentInstallError):
    """Raised at the runtime-binding publication boundary."""

    failure_responsibility = "roughcut_runtime_binding"
    failure_action = "publish_runtime_binding"

    def __init__(
        self,
        message: str,
        *,
        reason_code: str = "runtime_publish_failed",
    ) -> None:
        if reason_code not in RUNTIME_PUBLISH_REASON_CODES:
            raise ValueError("runtime publication reason is not closed")
        super().__init__(message)
        self.reason_code = reason_code
        self.failure_reason = reason_code


class ArtifactVerificationError(ComponentInstallError):
    """Raised at the artifact integrity boundary."""

    failure_responsibility = "component_artifact"
    failure_action = "verify_component_artifact"


WINDOWS_VC_MINIMUM = "14.51.36247.0"
WINDOWS_VC_PREREQUISITE_ID = "windows_vc_runtime_x64"
WINDOWS_VC_STATUSES = frozenset(
    {
        "ready",
        "missing",
        "outdated",
        "registry_dll_mismatch",
        "unverifiable_cross_target",
    }
)
WINDOWS_VC_ACTION_FIELDS = frozenset(
    {"kind", "blocking", "manager", "command", "source"}
)
WINDOWS_VC_INSTALL_COMMAND = [
    "winget",
    "install",
    "--id",
    "Microsoft.VCRedist.2015+.x64",
    "--exact",
    "--source",
    "winget",
]
WINDOWS_VC_UPGRADE_COMMAND = [
    "winget",
    "upgrade",
    "--id",
    "Microsoft.VCRedist.2015+.x64",
    "--exact",
    "--source",
    "winget",
]
WINDOWS_VC_MANAGER = "Windows Package Manager"
WINDOWS_VC_SOURCE = "Microsoft/winget"
WINDOWS_PATH_BUDGET_LIMIT = 259
OWNED_TEMP_TOKEN_BYTES = 8
OWNED_TEMP_TOKEN_HEX_LENGTH = OWNED_TEMP_TOKEN_BYTES * 2
OWNED_TEMP_CREATE_ATTEMPTS = 8
CACHE_RECEIPT_TEMP_PREFIX = ".roughcut-receipt-"
CACHE_RECEIPT_TEMP_SUFFIX = ".tmp"
MANAGED_STAGING_PREFIX = ".roughcut-stage-"
MANAGED_BACKUP_PREFIX = ".roughcut-backup-"
WINDOWS_PATH_EVIDENCE_SHA256 = (
    "19baca9a7083101e7edbf4f3871b7d6e5d6d384326133737334a88a455004e06"
)
WINDOWS_PATH_EVIDENCE = {
    "wheel_member": {
        "distribution": "modelscope",
        "version": "1.38.1",
        "artifact_filename": "modelscope-1.38.1-py3-none-any.whl",
        "artifact_sha256": "7d65be96999144ca045386d27a8ea057b8777504c538d9755d60ec2c8906fe72",
        "artifact_size": 6034052,
        "member": "modelscope\\msdatasets\\dataset_cls\\custom_datasets\\image_quality_assessment_degradation\\image_quality_assessment_degradation_dataset.py",
        "characters": 134,
        "utf16_code_units": 134,
    },
    "sdist_member": {
        "distribution": "aliyun-python-sdk-core",
        "version": "2.16.0",
        "artifact_filename": "aliyun-python-sdk-core-2.16.0.tar.gz",
        "artifact_sha256": "651caad597eb39d4fad6cf85133dffe92837d53bdf62db9d8f37dab6508bb8f9",
        "artifact_size": 449555,
        "member": "aliyun-python-sdk-core-2.16.0\\aliyunsdkcore\\vendored\\requests\\packages\\urllib3\\packages\\ssl_match_hostname\\_implementation.py",
        "characters": 125,
        "utf16_code_units": 125,
    },
    "runtime_staged_relative": {
        "source_artifact_filename": "modelscope-1.38.1-py3-none-any.whl",
        "source_artifact_sha256": "7d65be96999144ca045386d27a8ea057b8777504c538d9755d60ec2c8906fe72",
        "path": "venv\\Lib\\site-packages\\modelscope\\msdatasets\\dataset_cls\\custom_datasets\\image_quality_assessment_degradation\\__pycache__\\image_quality_assessment_degradation_dataset.cpython-311.pyc",
        "characters": 182,
        "utf16_code_units": 182,
    },
    "audalign_staged_relative": {
        "source_artifact_filename": "audalign-1.3.1-py3-none-any.whl",
        "source_artifact_sha256": "4d78f71a026d30c7462521084a56c7b1f50103e955b04e2ce7015fd8a107c477",
        "path": "audalign\\venv\\Lib\\site-packages\\audalign\\recognizers\\correcognizeSpectrogram\\__pycache__\\correcognize_spectrogram.cpython-311.pyc",
        "characters": 129,
        "utf16_code_units": 129,
    },
    "bbc_audio_offset_finder_staged_relative": {
        "source_artifact_filename": "scikit_learn-1.9.0-cp311-cp311-win_amd64.whl",
        "source_artifact_sha256": "5dc1818c77575d149e25fce9ef82dd7b7263ae372f03494158668ad632a69759",
        "path": "bbc_audio_offset_finder\\venv\\Lib\\site-packages\\sklearn\\ensemble\\_hist_gradient_boosting\\tests\\__pycache__\\test_monotonic_constraints.cpython-311.pyc",
        "characters": 148,
        "utf16_code_units": 148,
    },
}


@dataclass(frozen=True)
class ArtifactSpec:
    component: str
    name: str
    version: str
    filename: str
    url: str
    license: str
    sha256: str
    size: int
    destination: str | None = None

    def __post_init__(self) -> None:
        if self.component not in {
            "python_runtime",
            AUDALIGN_GROUP_NAME,
            BBC_AUDIO_OFFSET_FINDER_GROUP_NAME,
            *MODEL_COMPONENT_NAMES,
        }:
            raise ComponentInstallError("catalog artifact component is unsupported")
        if not all((self.name, self.version, self.filename, self.url, self.license)):
            raise ComponentInstallError("catalog artifact metadata is incomplete")
        if (
            Path(self.filename).name != self.filename
            or PureWindowsPath(self.filename).name != self.filename
        ):
            raise ComponentInstallError("catalog artifact filename is unsafe")
        if len(self.sha256) != 64 or any(value not in "0123456789abcdef" for value in self.sha256):
            raise ComponentInstallError("catalog artifact SHA-256 is invalid")
        if self.size <= 0:
            raise ComponentInstallError("catalog artifact size is invalid")
        if not artifact_source_is_allowed(self.url, allow_loopback_http=True):
            raise ComponentInstallError("catalog artifact source must use HTTPS")
        if self.destination is not None:
            destination = PurePosixPath(self.destination)
            if (
                destination.is_absolute()
                or self.destination in {"", "."}
                or ".." in destination.parts
            ):
                raise ComponentInstallError("catalog artifact destination is unsafe")

    def to_plan_dict(
        self,
        *,
        cache_path: Path,
        cache_status: str,
        resumable_bytes: int,
    ) -> dict[str, object]:
        return {
            "component": self.component,
            "name": self.name,
            "version": self.version,
            "filename": self.filename,
            "source": self.url,
            "license": self.license,
            "sha256": self.sha256,
            "size": self.size,
            "destination": self.destination,
            "cache_path": str(cache_path),
            "cache_status": cache_status,
            "resumable_bytes": resumable_bytes,
        }


@dataclass(frozen=True)
class ModelSpec:
    name: str
    repository: str
    revision: str
    license: str
    directory_sha256: str
    size: int
    artifacts: tuple[ArtifactSpec, ...]


@dataclass(frozen=True)
class RuntimeSpec:
    versions: dict[str, str]
    artifacts: tuple[ArtifactSpec, ...]
    dependency_lock: Path
    dependency_lock_sha256: str
    dependency_lock_origin: str
    estimated_installed_bytes: int


AUDALIGN_GROUP_NAME = "audalign_fingerprint"
AUDALIGN_UPSTREAM_COMMIT = "d5955ae8a85b1cd480dadd005c3f88986f4ebbef"
AUDALIGN_CATALOG_PREFIX = "audalign"
BBC_AUDIO_OFFSET_FINDER_GROUP_NAME = "bbc_audio_offset_finder"
BBC_AUDIO_OFFSET_FINDER_VERSION = "0.5.5"
BBC_AUDIO_OFFSET_FINDER_ORIGIN = (
    "https://pypi.org/project/audio-offset-finder/0.5.5/"
)
BBC_AUDIO_OFFSET_FINDER_LICENSE = "Apache-2.0"


@dataclass(frozen=True)
class AlignmentGroupSpec:
    """One frozen managed alignment provider group (Audalign or BBC).

    Identities are kept separate on purpose: the plan group names the install
    plan/artifact grouping, while the managed record/directory name is the
    historical on-disk layout (audalign keeps ``audalign``, never
    ``audalign_fingerprint``).
    """

    provider: str
    plan_group_name: str
    managed_record_name: str
    managed_dir: str
    direct_distribution: str
    version: str
    upstream_commit: str | None
    origin: str
    record_license: str
    artifacts: tuple[ArtifactSpec, ...]
    dependency_lock: Path
    dependency_lock_sha256: str
    dependency_lock_origin: str
    license_notice_file: Path
    license_notice_sha256: str
    estimated_installed_bytes: int
    distributions: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class FFmpegAction:
    manager: str
    package_id: str
    command: tuple[str, ...]
    source: str
    license: str
    note: str

    def to_dict(self) -> dict[str, object]:
        return {
            "manager": self.manager,
            "package_id": self.package_id,
            "command": list(self.command),
            "source": self.source,
            "license": self.license,
            "note": self.note,
        }


@dataclass(frozen=True)
class ReleaseProfile:
    id: str
    platform: str
    architecture: str
    python_version: str
    runtime: RuntimeSpec
    models: dict[str, ModelSpec]
    ffmpeg: FFmpegAction
    alignment_groups: tuple[AlignmentGroupSpec, ...] = ()
    prerequisites: dict[str, object] | None = None
    path_budget_evidence: dict[str, object] | None = None

    def alignment_group(self, plan_group_name: str) -> AlignmentGroupSpec | None:
        matches = [
            group
            for group in self.alignment_groups
            if group.plan_group_name == plan_group_name
        ]
        if len(matches) > 1:
            raise ComponentInstallError(
                "release profile carries duplicate alignment groups"
            )
        return matches[0] if matches else None


@dataclass(frozen=True)
class ExternalRuntimeProfile:
    id: str
    python_version: str
    versions: dict[str, str]
    device: str
    platforms: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ReleaseCatalog:
    version: str
    digest: str
    profiles: tuple[ReleaseProfile, ...]
    external_profiles: tuple[ExternalRuntimeProfile, ...]

    def profile_for(self, platform: str, architecture: str) -> ReleaseProfile:
        matches = [
            profile
            for profile in self.profiles
            if profile.platform == platform and profile.architecture == architecture
        ]
        if len(matches) != 1:
            raise ComponentInstallError(
                f"no pinned component profile for {platform}/{architecture}/Python 3.11"
            )
        return matches[0]


@dataclass(frozen=True)
class InstallPlan:
    payload: dict[str, object]
    plan_hash: str

    def to_dict(self) -> dict[str, object]:
        return {**self.payload, "plan_hash": self.plan_hash}


@dataclass(frozen=True)
class ApplyResult:
    approved_plan_hash: str
    installed_groups: tuple[str, ...]
    reused: bool
    manifest_path: str | None
    diagnostics: dict[str, object]
    runtime_binding: RuntimePublishResult | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": 1,
            "kind": "roughcut_component_apply_result",
            "approved_plan_hash": self.approved_plan_hash,
            "installed_groups": list(self.installed_groups),
            "installed": bool(self.installed_groups),
            "reused": self.reused,
            "manifest_path": self.manifest_path,
            "diagnostics": self.diagnostics,
        }
        if self.runtime_binding is not None:
            payload["runtime_binding"] = self.runtime_binding.to_dict()
        return payload


@dataclass
class _ManagedSwap:
    relative: PurePosixPath
    destination: Path
    backup: Path | None = None
    published: bool = False


def load_release_catalog(root: Path | None = None) -> ReleaseCatalog:
    catalog_root = (root or CATALOG_ROOT).resolve(strict=True)
    catalog_path = catalog_root / "release-catalog.json"
    raw = _read_json_object(catalog_path, "release catalog")
    if raw.get("schema_version") != CATALOG_SCHEMA_VERSION:
        raise ComponentInstallError("release catalog schema version is unsupported")
    version = _required_string(raw, "catalog_version")
    raw_profiles = raw.get("profiles")
    raw_external = raw.get("external_runtime_profiles")
    if not isinstance(raw_profiles, list) or not isinstance(raw_external, list):
        raise ComponentInstallError("release catalog profiles are missing")

    referenced_files: set[Path] = {catalog_path}
    profiles: list[ReleaseProfile] = []
    for item in raw_profiles:
        if not isinstance(item, dict):
            raise ComponentInstallError("release profile is invalid")
        runtime_raw = item.get("runtime")
        ffmpeg_raw = item.get("ffmpeg")
        if not isinstance(runtime_raw, dict) or not isinstance(ffmpeg_raw, dict):
            raise ComponentInstallError("release profile runtime metadata is missing")
        artifacts_path = _catalog_child(
            catalog_root, _required_string(runtime_raw, "artifacts")
        )
        lock_path = _catalog_child(
            catalog_root, _required_string(runtime_raw, "dependency_lock")
        )
        models_path = _catalog_child(
            catalog_root, _required_string(item, "models")
        )
        referenced_files.update((artifacts_path, lock_path, models_path))
        lock_sha256 = _required_string(runtime_raw, "dependency_lock_sha256")
        if _file_sha256(lock_path) != lock_sha256:
            raise ComponentInstallError("dependency lock checksum differs from catalog")
        versions = _string_map(runtime_raw.get("versions"), "runtime versions")
        if set(versions) != set(PYTHON_COMPONENT_NAMES):
            raise ComponentInstallError("runtime profile must pin FunASR, Torch, and TorchAudio")
        artifacts = _load_runtime_artifacts(artifacts_path)
        artifact_versions = {
            artifact.name: artifact.version
            for artifact in artifacts
            if artifact.name in PYTHON_COMPONENT_NAMES
        }
        if artifact_versions != versions:
            raise ComponentInstallError("direct runtime artifacts differ from pinned versions")
        _validate_runtime_dependency_lock(lock_path, artifacts)
        models = _load_models(models_path)
        profile_platform = _required_string(item, "platform")
        profile_architecture = _required_string(item, "architecture")
        ffmpeg_command = ffmpeg_raw.get("command")
        if (
            not isinstance(ffmpeg_command, list)
            or not ffmpeg_command
            or not all(isinstance(value, str) and value for value in ffmpeg_command)
        ):
            raise ComponentInstallError("FFmpeg user action command is invalid")
        audalign_spec = _load_alignment_group_spec(
            catalog_root,
            item,
            referenced_files,
            platform=profile_platform,
            architecture=profile_architecture,
            group_key="audalign",
        )
        bbc_spec = _load_alignment_group_spec(
            catalog_root,
            item,
            referenced_files,
            platform=profile_platform,
            architecture=profile_architecture,
            group_key=BBC_AUDIO_OFFSET_FINDER_GROUP_NAME,
        )
        alignment_groups = tuple(
            spec for spec in (audalign_spec, bbc_spec) if spec is not None
        )
        prerequisites = _load_profile_prerequisites(
            item.get("prerequisites"),
            platform=profile_platform,
            architecture=profile_architecture,
        )
        path_budget_evidence = None
        if profile_platform == "windows" and profile_architecture == "x86_64":
            path_budget_evidence = _validate_path_budget_evidence(
                item.get("path_budget_evidence"),
                artifacts=(
                    *artifacts,
                    *(artifact for model in models.values() for artifact in model.artifacts),
                    *(
                        artifact
                        for group in alignment_groups
                        for artifact in group.artifacts
                    ),
                ),
            )
        profiles.append(
            ReleaseProfile(
                id=_required_string(item, "id"),
                platform=profile_platform,
                architecture=profile_architecture,
                python_version=_required_string(item, "python_version"),
                runtime=RuntimeSpec(
                    versions=versions,
                    artifacts=artifacts,
                    dependency_lock=lock_path,
                    dependency_lock_sha256=lock_sha256,
                    dependency_lock_origin=_required_string(
                        runtime_raw, "dependency_lock_origin"
                    ),
                    estimated_installed_bytes=_required_positive_int(
                        runtime_raw, "estimated_installed_bytes"
                    ),
                ),
                models=models,
                ffmpeg=FFmpegAction(
                    manager=_required_string(ffmpeg_raw, "manager"),
                    package_id=_required_string(ffmpeg_raw, "package_id"),
                    command=tuple(ffmpeg_command),
                    source=_required_https(ffmpeg_raw, "source"),
                    license=_required_string(ffmpeg_raw, "license"),
                    note=_required_string(ffmpeg_raw, "note"),
                ),
                alignment_groups=alignment_groups,
                prerequisites=prerequisites,
                path_budget_evidence=path_budget_evidence,
            )
        )

    external_profiles: list[ExternalRuntimeProfile] = []
    for item in raw_external:
        if not isinstance(item, dict):
            raise ComponentInstallError("external runtime profile is invalid")
        platforms_raw = item.get("platforms", [])
        if not isinstance(platforms_raw, list):
            raise ComponentInstallError("external runtime profile targets are invalid")
        platforms: list[tuple[str, str]] = []
        for target in platforms_raw:
            if not isinstance(target, dict):
                raise ComponentInstallError("external runtime target is invalid")
            platforms.append(
                (_required_string(target, "platform"), _required_string(target, "architecture"))
            )
        versions = _string_map(item.get("versions"), "external runtime versions")
        if set(versions) != set(PYTHON_COMPONENT_NAMES):
            raise ComponentInstallError("external runtime profile is incomplete")
        external_profiles.append(
            ExternalRuntimeProfile(
                id=_required_string(item, "id"),
                python_version=_required_string(item, "python_version"),
                versions=versions,
                device=_required_string(item, "device"),
                platforms=tuple(platforms),
            )
        )

    digest = hashlib.sha256()
    for path in sorted(referenced_files):
        digest.update(path.relative_to(catalog_root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(_file_sha256(path)))
    return ReleaseCatalog(
        version=version,
        digest=digest.hexdigest(),
        profiles=tuple(profiles),
        external_profiles=tuple(external_profiles),
    )


def _load_profile_prerequisites(
    value: object,
    *,
    platform: str,
    architecture: str,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ComponentInstallError("catalog profile prerequisites are missing")
    if platform == "macos" and architecture == "arm64":
        if value != {}:
            raise ComponentInstallError("macOS profile prerequisites must be empty")
        return {}
    if platform != "windows" or architecture != "x86_64":
        if value != {}:
            raise ComponentInstallError("non-Windows profile prerequisites must be empty")
        return {}
    if set(value) != {WINDOWS_VC_PREREQUISITE_ID}:
        raise ComponentInstallError("Windows profile prerequisites are not closed")
    raw = value[WINDOWS_VC_PREREQUISITE_ID]
    if not isinstance(raw, dict) or set(raw) != {
        "minimum",
        "manager",
        "package_id",
        "source",
    }:
        raise ComponentInstallError("Windows VC++ prerequisite catalog is not closed")
    expected = {
        "minimum": WINDOWS_VC_MINIMUM,
        "manager": WINDOWS_VC_MANAGER,
        "package_id": "Microsoft.VCRedist.2015+.x64",
        "source": WINDOWS_VC_SOURCE,
    }
    if raw != expected:
        raise ComponentInstallError("Windows VC++ prerequisite catalog differs")
    return {WINDOWS_VC_PREREQUISITE_ID: dict(expected)}


def _validate_path_budget_evidence(
    value: object,
    *,
    artifacts: tuple[ArtifactSpec, ...],
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "limit",
        "acceptance_evidence_sha256",
        "wheel_member",
        "sdist_member",
        "runtime_staged_relative",
        "audalign_staged_relative",
        "bbc_audio_offset_finder_staged_relative",
    }:
        raise ComponentInstallError("Windows path budget evidence is not closed")
    if value.get("schema_version") != 1 or value.get("limit") != WINDOWS_PATH_BUDGET_LIMIT:
        raise ComponentInstallError("Windows path budget evidence version or limit is invalid")
    if value.get("acceptance_evidence_sha256") != WINDOWS_PATH_EVIDENCE_SHA256:
        raise ComponentInstallError("Windows path budget evidence checksum is invalid")

    artifacts_by_identity = {
        (artifact.name, artifact.version, artifact.filename, artifact.sha256, artifact.size): artifact
        for artifact in artifacts
    }
    artifacts_by_filename_sha = {
        (artifact.filename, artifact.sha256): artifact for artifact in artifacts
    }
    expected = WINDOWS_PATH_EVIDENCE
    for key in ("wheel_member", "sdist_member"):
        entry = value.get(key)
        if not isinstance(entry, dict) or set(entry) != {
            "distribution",
            "version",
            "artifact_filename",
            "artifact_sha256",
            "artifact_size",
            "member",
            "characters",
            "utf16_code_units",
        }:
            raise ComponentInstallError("Windows archive member evidence is not closed")
        if entry != expected[key]:
            raise ComponentInstallError("Windows archive member evidence differs")
        distribution = entry.get("distribution")
        version = entry.get("version")
        filename = entry.get("artifact_filename")
        sha256 = entry.get("artifact_sha256")
        size = entry.get("artifact_size")
        if not all(isinstance(item, str) and item for item in (distribution, version, filename, sha256)):
            raise ComponentInstallError("Windows archive member evidence is invalid")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise ComponentInstallError("Windows archive member evidence size is invalid")
        if (distribution, version, filename, sha256, size) not in artifacts_by_identity:
            raise ComponentInstallError("Windows archive member evidence artifact is not in profile")
        _validate_path_budget_string(
            entry.get("member"),
            characters=entry.get("characters"),
            utf16_code_units=entry.get("utf16_code_units"),
        )

    for key in (
        "runtime_staged_relative",
        "audalign_staged_relative",
        "bbc_audio_offset_finder_staged_relative",
    ):
        entry = value.get(key)
        if not isinstance(entry, dict) or set(entry) != {
            "source_artifact_filename",
            "source_artifact_sha256",
            "path",
            "characters",
            "utf16_code_units",
        }:
            raise ComponentInstallError("Windows staged path evidence is not closed")
        if entry != expected[key]:
            raise ComponentInstallError("Windows staged path evidence differs")
        filename = entry.get("source_artifact_filename")
        sha256 = entry.get("source_artifact_sha256")
        if not isinstance(filename, str) or not isinstance(sha256, str):
            raise ComponentInstallError("Windows staged path evidence artifact is invalid")
        if (filename, sha256) not in artifacts_by_filename_sha:
            raise ComponentInstallError("Windows staged path evidence artifact is not in profile")
        _validate_path_budget_string(
            entry.get("path"),
            characters=entry.get("characters"),
            utf16_code_units=entry.get("utf16_code_units"),
        )
    copied: Any = json.loads(json.dumps(value, ensure_ascii=False))
    if not isinstance(copied, dict):
        raise ComponentInstallError("Windows path budget evidence copy is invalid")
    return {str(key): item for key, item in copied.items()}


def _validate_path_budget_string(
    value: object,
    *,
    characters: object,
    utf16_code_units: object,
) -> None:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ComponentInstallError("Windows path budget evidence contains an invalid path")
    if (
        "${" in value
        or "%USERPROFILE%" in value
        or "..." in value
        or "<" in value
        or ">" in value
    ):
        raise ComponentInstallError("Windows path budget evidence contains a placeholder")
    drive, _tail = ntpath.splitdrive(value)
    if (
        drive
        or PureWindowsPath(value).is_absolute()
        or value.startswith(("\\", "/"))
        or any(part in {"", ".", ".."} for part in value.replace("/", "\\").split("\\"))
    ):
        raise ComponentInstallError("Windows path budget evidence path is unsafe")
    if isinstance(characters, bool) or not isinstance(characters, int):
        raise ComponentInstallError("Windows path budget evidence character count is invalid")
    if isinstance(utf16_code_units, bool) or not isinstance(utf16_code_units, int):
        raise ComponentInstallError("Windows path budget evidence UTF-16 count is invalid")
    if characters != len(value) or utf16_code_units != len(value.encode("utf-16-le")) // 2:
        raise ComponentInstallError("Windows path budget evidence character count differs")


def _load_alignment_group_spec(
    catalog_root: Path,
    item: dict[str, object],
    referenced_files: set[Path],
    *,
    platform: str,
    architecture: str,
    group_key: str,
) -> AlignmentGroupSpec | None:
    """Load one optional frozen managed alignment provider group."""
    group_raw = item.get(group_key)
    if group_raw is None:
        return None
    if not isinstance(group_raw, dict):
        raise ComponentInstallError(f"{group_key} profile metadata is invalid")
    if group_key == "audalign":
        provider = "audalign"
        plan_group_name = AUDALIGN_GROUP_NAME
        managed_record_name = "audalign"
        managed_dir = "audalign"
        direct_distribution = "audalign"
        version = "1.3.1"
        upstream_commit: str | None = AUDALIGN_UPSTREAM_COMMIT
        origin = "https://pypi.org/project/audalign/1.3.1/"
        record_license = "MIT"
    elif group_key == BBC_AUDIO_OFFSET_FINDER_GROUP_NAME:
        provider = BBC_AUDIO_OFFSET_FINDER_GROUP_NAME
        plan_group_name = BBC_AUDIO_OFFSET_FINDER_GROUP_NAME
        managed_record_name = BBC_AUDIO_OFFSET_FINDER_GROUP_NAME
        managed_dir = BBC_AUDIO_OFFSET_FINDER_GROUP_NAME
        direct_distribution = "audio-offset-finder"
        version = BBC_AUDIO_OFFSET_FINDER_VERSION
        upstream_commit = None
        origin = BBC_AUDIO_OFFSET_FINDER_ORIGIN
        record_license = BBC_AUDIO_OFFSET_FINDER_LICENSE
    else:
        raise ComponentInstallError("alignment group key is unsupported")
    artifacts_path = _catalog_child(
        catalog_root, _required_string(group_raw, "artifacts")
    )
    lock_path = _catalog_child(
        catalog_root, _required_string(group_raw, "dependency_lock")
    )
    license_path = _catalog_child(
        catalog_root, _required_string(group_raw, "license_notice")
    )
    referenced_files.update((artifacts_path, lock_path, license_path))
    lock_sha256 = _required_string(group_raw, "dependency_lock_sha256")
    if _file_sha256(lock_path) != lock_sha256:
        raise ComponentInstallError(
            f"{group_key} dependency lock checksum differs from catalog"
        )
    license_sha256 = _required_string(group_raw, "license_notice_sha256")
    if _file_sha256(license_path) != license_sha256:
        raise ComponentInstallError(
            f"{group_key} license notice checksum differs from catalog"
        )
    if (platform, architecture) not in {("macos", "arm64"), ("windows", "x86_64")}:
        raise ComponentInstallError(f"{group_key} profile target is unsupported")
    artifacts = _load_alignment_artifacts(
        artifacts_path,
        architecture=architecture,
        component=plan_group_name,
    )
    if provider == "audalign":
        expected_names = audalign_distributions_for(platform)
        expected_versions = audalign_distribution_versions_for(platform)
    else:
        expected_names = bbc_audio_offset_finder_distributions_for(platform)
        expected_versions = bbc_audio_offset_finder_distribution_versions_for(platform)
    _validate_alignment_dependency_lock(lock_path, expected_names, artifacts)
    _validate_alignment_license_notice(license_path, expected_names, platform=platform)
    artifact_map = {artifact.name: artifact.version for artifact in artifacts}
    names = [artifact.name for artifact in artifacts]
    if (
        artifact_map != expected_versions
        or len(artifacts) != len(expected_versions)
        or len(names) != len(set(names))
    ):
        raise ComponentInstallError(
            f"{group_key} closure distributions differ from the frozen table"
        )
    distributions = tuple((name, expected_versions[name]) for name in expected_names)
    return AlignmentGroupSpec(
        provider=provider,
        plan_group_name=plan_group_name,
        managed_record_name=managed_record_name,
        managed_dir=managed_dir,
        direct_distribution=direct_distribution,
        version=version,
        upstream_commit=upstream_commit,
        origin=origin,
        record_license=record_license,
        artifacts=artifacts,
        dependency_lock=lock_path,
        dependency_lock_sha256=lock_sha256,
        dependency_lock_origin=_required_string(group_raw, "dependency_lock_origin"),
        license_notice_file=license_path,
        license_notice_sha256=license_sha256,
        estimated_installed_bytes=_required_positive_int(
            group_raw, "estimated_installed_bytes"
        ),
        distributions=distributions,
    )


def _load_alignment_artifacts(
    path: Path,
    *,
    architecture: str,
    component: str,
) -> tuple[ArtifactSpec, ...]:
    raw = _read_json_object(path, "alignment artifact catalog")
    artifacts_raw = raw.get("artifacts")
    if (
        raw.get("schema_version") != 1
        or raw.get("architecture") != architecture
        or not isinstance(artifacts_raw, list)
    ):
        raise ComponentInstallError("alignment artifact catalog is invalid")
    artifacts: list[ArtifactSpec] = []
    for item in artifacts_raw:
        if not isinstance(item, dict):
            raise ComponentInstallError("alignment artifact catalog is invalid")
        if item.get("component") != component:
            raise ComponentInstallError("alignment artifact catalog group differs")
        artifacts.append(
            ArtifactSpec(
                component=_required_string(item, "component"),
                name=_required_string(item, "name"),
                version=_required_string(item, "version"),
                filename=_required_string(item, "filename"),
                url=_required_string(item, "url"),
                license=_required_string(item, "license"),
                sha256=_required_string(item, "sha256"),
                size=_required_positive_int(item, "size"),
                destination=item.get("destination"),
            )
        )
    if raw.get("artifact_count") != len(artifacts):
        raise ComponentInstallError("alignment artifact catalog count is invalid")
    return tuple(artifacts)


def _validate_alignment_dependency_lock(
    path: Path,
    expected_names: tuple[str, ...],
    artifacts: tuple[ArtifactSpec, ...],
) -> None:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ComponentInstallError("alignment dependency lock is unreadable") from error
    if b"\r" in raw:
        raise ComponentInstallError("audalign dependency lock must use LF line endings")
    lines = raw.decode("utf-8").splitlines()
    entries: list[tuple[str, str, str]] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line or line.startswith("#"):
            index += 1
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s]+) \\", line)
        if match is None or index + 1 >= len(lines):
            raise ComponentInstallError("alignment dependency lock is invalid")
        hash_match = re.fullmatch(r"--hash=sha256:([0-9a-f]{64})", lines[index + 1].strip())
        if hash_match is None:
            raise ComponentInstallError("alignment dependency lock is invalid")
        entries.append((match.group(1), match.group(2), hash_match.group(1)))
        index += 2
    artifacts_by_name = {artifact.name: artifact for artifact in artifacts}
    if set(artifacts_by_name) != set(expected_names) or len(artifacts_by_name) != len(
        artifacts
    ):
        raise ComponentInstallError("alignment dependency lock is not the exact frozen closure")
    normalized = [
        (re.sub(r"[-_.]+", "-", name).lower(), version, sha256)
        for name, version, sha256 in entries
    ]
    expected = {
        name: (artifacts_by_name[name].version, artifacts_by_name[name].sha256)
        for name in expected_names
    }
    actual = {name: (version, sha256) for name, version, sha256 in normalized}
    if (
        len(normalized) != len(expected)
        or len({name for name, _version, _sha256 in normalized}) != len(normalized)
        or actual != expected
    ):
        raise ComponentInstallError("alignment dependency lock is not the exact frozen closure")


def _validate_alignment_license_notice(
    path: Path,
    expected_names: tuple[str, ...],
    *,
    platform: str,
) -> None:
    raw = _read_json_object(path, "alignment license notice")
    normalized_keys = [re.sub(r"[-_.]+", "-", key).lower() for key in raw]
    if set(normalized_keys) != set(expected_names) or len(normalized_keys) != len(
        set(normalized_keys)
    ):
        raise ComponentInstallError("alignment license notice closure is incomplete")
    for item in raw.values():
        if not isinstance(item, dict):
            raise ComponentInstallError("alignment license notice entry is invalid")
        license_files = item.get("license_files")
        payloads = item.get("payloads")
        if (
            not isinstance(license_files, list)
            or not all(isinstance(value, str) and value for value in license_files)
            or not isinstance(payloads, dict)
            or any(not isinstance(key, str) or not key for key in payloads)
            or any(value is not None and not isinstance(value, str) for value in payloads.values())
            or any(value not in payloads for value in license_files)
        ):
            raise ComponentInstallError("alignment license notice entry is invalid")
        if platform == "windows" and any(value is None for value in payloads.values()):
            raise ComponentInstallError("Windows alignment license notice is incomplete")


def build_install_plan(
    managed_root: Path,
    cache_root: Path,
    *,
    install_root: Path | None = None,
    catalog: ReleaseCatalog | None = None,
    external_manifest_path: Path | None = None,
    external_python: Path | None = None,
    platform: str | None = None,
    architecture: str | None = None,
    ffmpeg_command: str = "ffmpeg",
    ffprobe_command: str = "ffprobe",
    verify_components: bool = False,
    include_audalign: bool = False,
    include_bbc_audio_offset_finder: bool = False,
) -> InstallPlan:
    """Build a deterministic, read-only plan for the current component state."""

    from roughcut.adapters.component_environment import (
        current_architecture,
        current_platform,
    )

    selected_catalog = catalog or load_release_catalog()
    host_platform = current_platform()
    host_architecture = current_architecture()
    target_platform = platform or host_platform
    target_architecture = architecture or host_architecture
    profile = selected_catalog.profile_for(target_platform, target_architecture)
    requested_alignment_groups: dict[str, str] = {}
    if include_audalign and include_bbc_audio_offset_finder:
        raise ComponentInstallError("alignment provider selection is mutually exclusive")
    if include_audalign:
        requested_alignment_groups[AUDALIGN_GROUP_NAME] = "audalign"
    if include_bbc_audio_offset_finder:
        requested_alignment_groups[BBC_AUDIO_OFFSET_FINDER_GROUP_NAME] = (
            BBC_AUDIO_OFFSET_FINDER_GROUP_NAME
        )
    for group_name, provider_label in requested_alignment_groups.items():
        if profile.alignment_group(group_name) is None:
            raise ComponentInstallError(
                f"{provider_label} managed group is unavailable for "
                f"{target_platform}/{target_architecture}/Python 3.11"
            )
    if target_platform == "windows" and host_platform == "windows" and os.name != "nt":
        root = Path(str(managed_root))
        cache = Path(str(cache_root))
    else:
        root = managed_root.resolve(strict=False)
        cache = cache_root.resolve(strict=False)
    _validate_dedicated_roots(root, cache)
    apply_supported_on_this_host = (
        target_platform == host_platform
        and target_architecture == host_architecture
        and sys.version_info[:2] == (3, 11)
    )

    external = (
        load_component_manifest(external_manifest_path)
        if external_manifest_path is not None
        else None
    )
    manifest_path = root / COMPONENT_MANIFEST_FILENAME
    managed = load_component_manifest(manifest_path) if manifest_path.is_file() else None
    external_probe: dict[str, object] | None = None
    external_probe_error: str | None = None
    if external_python is not None:
        try:
            external_probe = probe_external_python(external_python)
        except (ComponentInstallError, OSError) as error:
            external_probe_error = str(error)

    runtime_components, runtime_missing, external_runtime_profile = _resolve_runtime(
        selected_catalog,
        profile,
        external_python=external_python,
        external_probe=external_probe,
        external_probe_error=external_probe_error,
        managed=managed,
        target_platform=target_platform,
        target_architecture=target_architecture,
    )
    model_components, missing_models = _resolve_models(
        profile,
        external=external,
        managed=managed,
        external_runtime_profile=external_runtime_profile,
        target_platform=target_platform,
        target_architecture=target_architecture,
        verify_components=verify_components,
    )

    ffmpeg_environment = diagnose_ffmpeg(
        ffmpeg_command=ffmpeg_command,
        ffprobe_command=ffprobe_command,
        full=verify_components,
    )
    ffmpeg = _command_plan(ffmpeg_environment.ffmpeg)
    ffprobe = _command_plan(ffmpeg_environment.ffprobe)
    components = {**runtime_components, **model_components, "ffmpeg": ffmpeg, "ffprobe": ffprobe}
    missing_alignment_groups: list[AlignmentGroupSpec] = []
    for group_name in requested_alignment_groups:
        group_spec = profile.alignment_group(group_name)
        assert group_spec is not None
        if not _managed_alignment_group_available(
            group_spec, managed, root, target_platform, target_architecture
        ):
            missing_alignment_groups.append(group_spec)
    missing_groups = (["python_runtime"] if runtime_missing else []) + list(missing_models)
    for group_spec in missing_alignment_groups:
        missing_groups.append(group_spec.plan_group_name)
    required_artifacts: list[ArtifactSpec] = []
    if runtime_missing:
        required_artifacts.extend(profile.runtime.artifacts)
    for name in missing_models:
        required_artifacts.extend(profile.models[name].artifacts)
    for group_spec in missing_alignment_groups:
        required_artifacts.extend(group_spec.artifacts)

    artifact_payloads: list[dict[str, object]] = []
    logical_artifact_bytes = sum(artifact.size for artifact in required_artifacts)
    unique_artifacts = _unique_artifacts(required_artifacts, cache)
    download_bytes = 0
    cached_bytes = 0
    resumable_bytes = 0
    for artifact in required_artifacts:
        artifact_path = cache_artifact_path(cache, artifact)
        cache_status, artifact_resumable_bytes = _cache_status(artifact_path, artifact)
        artifact_payloads.append(
            artifact.to_plan_dict(
                cache_path=artifact_path,
                cache_status=cache_status,
                resumable_bytes=artifact_resumable_bytes,
            )
        )
    for artifact in unique_artifacts:
        artifact_path = cache_artifact_path(cache, artifact)
        cache_status, artifact_resumable_bytes = _cache_status(artifact_path, artifact)
        if cache_status == "verified":
            cached_bytes += artifact.size
        else:
            download_bytes += max(0, artifact.size - artifact_resumable_bytes)
        resumable_bytes += artifact_resumable_bytes

    installed_bytes = (
        profile.runtime.estimated_installed_bytes if runtime_missing else 0
    ) + sum(profile.models[name].size for name in missing_models)
    for group_spec in missing_alignment_groups:
        installed_bytes += group_spec.estimated_installed_bytes
    prerequisites = _resolve_prerequisites(
        profile,
        target_platform=target_platform,
        target_architecture=target_architecture,
        host_platform=host_platform,
        host_architecture=host_architecture,
    )
    path_budget = _build_path_budget(
        profile,
        install_root=install_root,
        managed_root=root,
        cache_root=cache,
        target_platform=target_platform,
        target_architecture=target_architecture,
        host_platform=host_platform,
        host_architecture=host_architecture,
    )
    user_actions: list[dict[str, object]] = []
    vc_prerequisite = prerequisites.get(WINDOWS_VC_PREREQUISITE_ID)
    if isinstance(vc_prerequisite, dict):
        action = vc_prerequisite.get("action")
        if action is not None:
            if not isinstance(action, dict):
                raise ComponentInstallError("Windows VC++ prerequisite action is invalid")
            user_actions.append(dict(action))
    if ffmpeg["status"] != "available" or ffprobe["status"] != "available":
        user_actions.append(
            {
                "component": "ffmpeg_ffprobe",
                "status": "user_action_required",
                "blocking": (
                    BBC_AUDIO_OFFSET_FINDER_GROUP_NAME in missing_groups
                    and ffmpeg["status"] != "available"
                ),
                **profile.ffmpeg.to_dict(),
            }
        )
    for root_name, budget in path_budget.items():
        if isinstance(budget, dict) and budget.get("status") == "blocking":
            user_actions.append(
                {
                    "kind": "path_budget",
                    "blocking": True,
                    "root": root_name,
                    "limit": WINDOWS_PATH_BUDGET_LIMIT,
                    "max_derived_length": budget.get("max_derived_length"),
                }
            )

    payload: dict[str, object] = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "kind": "roughcut_component_install_plan",
        "catalog_version": selected_catalog.version,
        "catalog_hash": selected_catalog.digest,
        "profile": profile.id,
        "target": {
            "platform": target_platform,
            "architecture": target_architecture,
            "python_version": profile.python_version,
            "apply_supported_on_this_host": apply_supported_on_this_host,
        },
        "prerequisites": prerequisites,
        "path_budget": path_budget,
        "managed_runtime": {
            "device": "cpu",
            "versions": profile.runtime.versions,
            "dependency_lock": {
                "filename": profile.runtime.dependency_lock.name,
                "source": profile.runtime.dependency_lock_origin,
                "sha256": profile.runtime.dependency_lock_sha256,
                "size": profile.runtime.dependency_lock.stat().st_size,
            },
        },
        "managed_root": str(root),
        "cache_root": str(cache),
        "verification_mode": "full" if verify_components else "quick",
        "components": components,
        "missing_managed_groups": missing_groups,
        "artifacts": artifact_payloads,
        "logical_artifact_count": len(required_artifacts),
        "logical_artifact_bytes": logical_artifact_bytes,
        "unique_cache_identity_count": len(unique_artifacts),
        "unique_cache_identity_bytes": sum(artifact.size for artifact in unique_artifacts),
        "required_artifact_bytes": logical_artifact_bytes,
        "download_bytes": download_bytes,
        "verified_cache_bytes": cached_bytes,
        "resumable_bytes": resumable_bytes,
        "estimated_installed_bytes": installed_bytes,
        "estimated_additional_disk_bytes": download_bytes + installed_bytes,
        "user_actions": user_actions,
        "state": {
            "managed_manifest_sha256": (
                _file_sha256(manifest_path) if manifest_path.is_file() else None
            ),
            "external_manifest_sha256": (
                _file_sha256(external_manifest_path)
                if external_manifest_path is not None
                else None
            ),
            "external_runtime_probe": external_probe,
            "external_runtime_probe_error": external_probe_error,
        },
    }
    for group_spec in profile.alignment_groups:
        requested = group_spec.plan_group_name in requested_alignment_groups
        if not requested:
            continue
        available = not any(
            missing.plan_group_name == group_spec.plan_group_name
            for missing in missing_alignment_groups
        )
        group_payload: dict[str, object] = {
            "available": available,
            "version": group_spec.version,
            "dependency_lock": {
                "filename": group_spec.dependency_lock.name,
                "source": group_spec.dependency_lock_origin,
                "sha256": group_spec.dependency_lock_sha256,
            },
            "license_notice_sha256": group_spec.license_notice_sha256,
            "distributions": [
                {"name": name, "version": version}
                for name, version in group_spec.distributions
            ],
            "estimated_installed_bytes": group_spec.estimated_installed_bytes,
        }
        if group_spec.upstream_commit is not None:
            group_payload["upstream_commit"] = group_spec.upstream_commit
        payload[group_spec.plan_group_name] = group_payload
    if install_root is not None:
        binding_path = Path(
            runtime_binding_path(install_root=install_root.resolve(strict=False))
        )
        payload["runtime_binding"] = runtime_binding_status(binding_path)
    return InstallPlan(payload=payload, plan_hash=_canonical_hash(payload))


def _resolve_prerequisites(
    profile: ReleaseProfile,
    *,
    target_platform: str,
    target_architecture: str,
    host_platform: str,
    host_architecture: str,
) -> dict[str, object]:
    if target_platform != "windows" or target_architecture != "x86_64":
        return {}
    definition = profile.prerequisites or {
        WINDOWS_VC_PREREQUISITE_ID: {
            "minimum": WINDOWS_VC_MINIMUM,
            "manager": WINDOWS_VC_MANAGER,
            "package_id": "Microsoft.VCRedist.2015+.x64",
            "source": WINDOWS_VC_SOURCE,
        }
    }
    raw_definition = definition.get(WINDOWS_VC_PREREQUISITE_ID)
    if not isinstance(raw_definition, dict) or set(raw_definition) != {
        "minimum",
        "manager",
        "package_id",
        "source",
    }:
        raise ComponentInstallError("Windows VC++ prerequisite definition is invalid")
    minimum = raw_definition.get("minimum")
    manager = raw_definition.get("manager")
    package_id = raw_definition.get("package_id")
    source = raw_definition.get("source")
    if (
        minimum != WINDOWS_VC_MINIMUM
        or manager != WINDOWS_VC_MANAGER
        or package_id != "Microsoft.VCRedist.2015+.x64"
        or source != WINDOWS_VC_SOURCE
    ):
        raise ComponentInstallError("Windows VC++ prerequisite definition differs")
    if host_platform != "windows" or host_architecture != "x86_64":
        observation: dict[str, object] = {
            "status": "unverifiable_cross_target",
            "observed": {
                "registry_version": None,
                "system32": {
                    "msvcp140.dll": None,
                    "vcruntime140.dll": None,
                    "vcruntime140_1.dll": None,
                },
            },
        }
    else:
        from roughcut.adapters.component_environment import probe_windows_vc_runtime

        observation = probe_windows_vc_runtime()
    if not isinstance(observation, dict) or set(observation) != {"status", "observed"}:
        raise ComponentInstallError("Windows VC++ prerequisite observation is invalid")
    status = observation.get("status")
    observed = observation.get("observed")
    if status not in WINDOWS_VC_STATUSES or not isinstance(observed, dict):
        raise ComponentInstallError("Windows VC++ prerequisite status is invalid")
    if set(observed) != {"registry_version", "system32"}:
        raise ComponentInstallError("Windows VC++ prerequisite observation is not closed")
    system32 = observed.get("system32")
    if not isinstance(system32, dict) or set(system32) != {
        "msvcp140.dll",
        "vcruntime140.dll",
        "vcruntime140_1.dll",
    }:
        raise ComponentInstallError("Windows VC++ DLL observation is not closed")
    normalized_system32: dict[str, str | None] = {
        name: _normalized_version(system32.get(name))
        for name in (
            "msvcp140.dll",
            "vcruntime140.dll",
            "vcruntime140_1.dll",
        )
    }
    normalized_observed: dict[str, object] = {
        "registry_version": _normalized_version(observed.get("registry_version")),
        "system32": normalized_system32,
    }
    if observed.get("registry_version") is not None and normalized_observed["registry_version"] is None:
        raise ComponentInstallError("Windows VC++ observed version is invalid")
    if any(
        system32.get(name) is not None and normalized_system32[name] is None
        for name in system32
    ):
        raise ComponentInstallError("Windows VC++ observed version is invalid")
    observed_versions = [
        normalized_observed["registry_version"],
        *normalized_system32.values(),
    ]
    if host_platform != "windows" or host_architecture != "x86_64":
        expected_status = "unverifiable_cross_target"
    elif all(value is None for value in observed_versions):
        expected_status = "missing"
    elif any(value is None for value in observed_versions) or len(set(observed_versions)) != 1:
        expected_status = "registry_dll_mismatch"
    else:
        version_value = observed_versions[0]
        if not isinstance(version_value, str):
            raise ComponentInstallError("Windows VC++ observed version is invalid")
        version_tuple = _version_tuple(version_value)
        minimum_tuple = _version_tuple(WINDOWS_VC_MINIMUM)
        if version_tuple is None or minimum_tuple is None:
            raise ComponentInstallError("Windows VC++ observed version is invalid")
        expected_status = (
            "outdated" if version_tuple < minimum_tuple else "ready"
        )
    if status != expected_status:
        raise ComponentInstallError("Windows VC++ status does not match its observation")
    action: dict[str, object] | None
    if status == "missing":
        action = {
            "kind": "install",
            "blocking": True,
            "manager": manager,
            "command": list(WINDOWS_VC_INSTALL_COMMAND),
            "source": source,
        }
    elif status == "outdated":
        action = {
            "kind": "upgrade",
            "blocking": True,
            "manager": manager,
            "command": list(WINDOWS_VC_UPGRADE_COMMAND),
            "source": source,
        }
    elif status == "registry_dll_mismatch":
        action = {
            "kind": "repair_or_reinstall",
            "blocking": True,
            "manager": manager,
            "command": None,
            "source": source,
        }
    else:
        action = None
    if action is not None and set(action) != WINDOWS_VC_ACTION_FIELDS:
        raise ComponentInstallError("Windows VC++ prerequisite action is not closed")
    return {
        WINDOWS_VC_PREREQUISITE_ID: {
            "observed": {
                "registry_version": normalized_observed["registry_version"],
                "system32": normalized_observed["system32"],
            },
            "minimum": minimum,
            "status": status,
            "action": action,
        }
    }


def _build_path_budget(
    profile: ReleaseProfile,
    *,
    install_root: Path | None,
    managed_root: Path,
    cache_root: Path,
    target_platform: str,
    target_architecture: str,
    host_platform: str,
    host_architecture: str,
) -> dict[str, object]:
    if target_platform != "windows" or target_architecture != "x86_64":
        return {}
    if host_platform != "windows" or host_architecture != "x86_64":
        return {
            name: {
                "max_derived_length": 0,
                "limiting_kind": "cross_target",
                "status": "not_applicable",
            }
            for name in ("install", "managed", "cache")
        }
    evidence_value = profile.path_budget_evidence
    if evidence_value is None:
        evidence: dict[str, object] = {
            key: value for key, value in WINDOWS_PATH_EVIDENCE.items()
        }
    else:
        evidence = evidence_value
    install_text = _windows_root_text(install_root or (Path.home() / ".roughcut"))
    managed_text = _windows_root_text(managed_root)
    cache_text = _windows_root_text(cache_root)
    artifacts = (
        *profile.runtime.artifacts,
        *(artifact for model in profile.models.values() for artifact in model.artifacts),
        *(
            artifact
            for group in profile.alignment_groups
            for artifact in group.artifacts
        ),
    )
    artifact_candidates = [
        (
            _windows_join(
                cache_text,
                "artifacts",
                artifact.sha256[:2],
                artifact.sha256,
                artifact.filename,
            ),
            "cache_artifact",
        )
        for artifact in artifacts
    ]
    runtime_evidence = evidence.get("runtime_staged_relative")
    audalign_evidence = evidence.get("audalign_staged_relative")
    bbc_evidence = evidence.get("bbc_audio_offset_finder_staged_relative")
    wheel_evidence = evidence.get("wheel_member")
    sdist_evidence = evidence.get("sdist_member")
    if (
        not isinstance(runtime_evidence, dict)
        or not isinstance(audalign_evidence, dict)
        or not isinstance(bbc_evidence, dict)
        or not isinstance(wheel_evidence, dict)
        or not isinstance(sdist_evidence, dict)
    ):
        raise ComponentInstallError("Windows path budget evidence is incomplete")
    runtime_path = runtime_evidence.get("path")
    audalign_path = audalign_evidence.get("path")
    bbc_path = bbc_evidence.get("path")
    wheel_member = wheel_evidence.get("member")
    sdist_member = sdist_evidence.get("member")
    if (
        not isinstance(runtime_path, str)
        or not isinstance(audalign_path, str)
        or not isinstance(bbc_path, str)
        or not isinstance(wheel_member, str)
        or not isinstance(sdist_member, str)
    ):
        raise ComponentInstallError("Windows path budget evidence paths are invalid")
    operation_directory = _windows_join(
        install_text, "operations", "component-installation"
    )
    max_operation_id = "op_" + "x" * 125
    install_candidates = [
        (_windows_join(install_text, "runtime.json"), "runtime_binding"),
        (_windows_join(operation_directory, f"{max_operation_id}.json"), "operation_record"),
        (
            _windows_join(operation_directory, f".{max_operation_id}.json.tmp"),
            "operation_record_temp",
        ),
        (
            _windows_join(operation_directory, f".{max_operation_id}.writer.lock"),
            "operation_writer_lock",
        ),
    ]
    cache_candidates = [
        (path, kind) for path, kind in artifact_candidates
    ]
    cache_candidates.extend(
        (f"{path}.part", "cache_partial") for path, _kind in artifact_candidates
    )
    cache_candidates.extend(
        (f"{path}.verified.json", "cache_receipt") for path, _kind in artifact_candidates
    )
    cache_candidates.extend(
        (
            _windows_join(
                ntpath.dirname(path),
                CACHE_RECEIPT_TEMP_PREFIX
                + "f" * OWNED_TEMP_TOKEN_HEX_LENGTH
                + CACHE_RECEIPT_TEMP_SUFFIX,
            ),
            "cache_receipt_temp",
        )
        for path, _kind in artifact_candidates
    )
    published_candidates = [
        (_windows_join(managed_text, runtime_path), "published_runtime"),
        (_windows_join(managed_text, audalign_path), "published_audalign"),
        (_windows_join(managed_text, bbc_path), "published_bbc_audio_offset_finder"),
    ]
    staging_name = MANAGED_STAGING_PREFIX + "f" * OWNED_TEMP_TOKEN_HEX_LENGTH
    staging_root = _windows_join(ntpath.dirname(managed_text), staging_name)
    managed_relative_candidates = [
        ("component-manifest.json", "manifest"),
        (f"locks\\{profile.runtime.dependency_lock.name}", "runtime_lock"),
        ("models", "model_root"),
    ]
    for model in profile.models.values():
        for artifact in model.artifacts:
            if artifact.destination is not None:
                managed_relative_candidates.append(
                    (
                        f"models\\{model.name}\\{artifact.destination}",
                        "model_payload",
                    )
                )
    for group in profile.alignment_groups:
        short = "audalign" if group.managed_record_name == "audalign" else group.managed_dir
        managed_relative_candidates.extend(
            [
                (
                    f"{group.managed_dir}\\locks\\{group.dependency_lock.name}",
                    f"{short}_lock",
                ),
                (
                    f"{group.managed_dir}\\licenses\\{group.license_notice_file.name}",
                    f"{short}_license",
                ),
            ]
        )
    managed_candidates = [
        *published_candidates,
        (_windows_join(staging_root, runtime_path), "staging_runtime"),
        (_windows_join(staging_root, audalign_path), "staging_audalign"),
        (
            _windows_join(staging_root, bbc_path),
            "staging_bbc_audio_offset_finder",
        ),
        *(
            (
                _windows_join(
                    staging_root,
                    "venv",
                    "Lib",
                    "site-packages",
                    member,
                ),
                kind,
            )
            for member, kind in (
                (wheel_member, "staging_wheel_member"),
                (sdist_member, "staging_sdist_member"),
            )
        ),
        *(
            (_windows_join(managed_text, relative), kind)
            for relative, kind in managed_relative_candidates
        ),
        *(
            (_windows_join(staging_root, relative), f"staging_{kind}")
            for relative, kind in managed_relative_candidates
        ),
    ]
    return {
        "install": _budget_result(install_candidates),
        "managed": _budget_result(managed_candidates),
        "cache": _budget_result(cache_candidates),
    }


def _budget_result(candidates: list[tuple[str, str]]) -> dict[str, object]:
    if not candidates:
        return {
            "max_derived_length": 0,
            "limiting_kind": "none",
            "status": "pass",
        }
    selected = max(candidates, key=lambda item: (len(item[0].encode("utf-16-le")) // 2, -candidates.index(item)))
    length = len(selected[0].encode("utf-16-le")) // 2
    return {
        "max_derived_length": length,
        "limiting_kind": selected[1],
        "status": "blocking" if length >= WINDOWS_PATH_BUDGET_LIMIT + 1 else "pass",
    }


def _windows_root_text(value: Path) -> str:
    text = str(value)
    return ntpath.normpath(text)


def _windows_join(*parts: str) -> str:
    return ntpath.normpath(ntpath.join(*parts))


def _version_tuple(value: str) -> tuple[int, int, int, int] | None:
    normalized = value[1:] if value[:1].lower() == "v" else value
    pieces = normalized.split(".")
    if len(pieces) != 4 or any(not piece.isdecimal() for piece in pieces):
        return None
    return tuple(int(piece) for piece in pieces)  # type: ignore[return-value]


def _normalized_version(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    pieces = value[1:] if value[:1].lower() == "v" else value
    parsed = _version_tuple(pieces)
    if parsed is None:
        return None
    return ".".join(str(part) for part in parsed)


def _unique_artifacts(
    artifacts: tuple[ArtifactSpec, ...] | list[ArtifactSpec],
    cache_root: Path,
) -> tuple[ArtifactSpec, ...]:
    result: list[ArtifactSpec] = []
    identities: set[tuple[str, str, str]] = set()
    for artifact in artifacts:
        identity = (artifact.sha256, artifact.filename, str(cache_artifact_path(cache_root, artifact)))
        if identity in identities:
            continue
        identities.add(identity)
        result.append(artifact)
    return tuple(result)


def validate_approved_full_plan(plan: InstallPlan, approved_plan_hash: str) -> None:
    if approved_plan_hash != plan.plan_hash:
        raise StaleApprovedPlanError(
            "Roughcut bootstrap 拒绝发布 stale component plan"
        )


def validate_install_preflight(
    plan: InstallPlan,
    approved_plan_hash: str,
    *,
    catalog: ReleaseCatalog | None = None,
) -> None:
    validate_approved_full_plan(plan, approved_plan_hash)
    if catalog is not None and (
        plan.payload.get("catalog_version") != catalog.version
        or plan.payload.get("catalog_hash") != catalog.digest
    ):
        raise StaleApprovedPlanError(
            "Roughcut bootstrap rejected a plan from a different release catalog"
        )
    _validate_apply_preflight(plan)


def _validate_apply_preflight(plan: InstallPlan) -> None:
    payload = plan.payload
    if payload.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise StaleApprovedPlanError("Roughcut bootstrap rejected a non-schema-2 approved plan")
    if payload.get("verification_mode") != "full":
        raise StaleApprovedPlanError(
            "Roughcut bootstrap only publishes runtime binding after full verification"
        )
    target = payload.get("target")
    if not isinstance(target, dict) or target.get("apply_supported_on_this_host") is not True:
        raise ComponentInstallError("this pinned profile cannot be applied on the current host")
    actions = payload.get("user_actions")
    if not isinstance(actions, list) or any(
        not isinstance(action, dict) for action in actions
    ):
        raise ComponentInstallError("install plan user actions are invalid")
    if any(action.get("blocking") is True for action in actions):
        raise ComponentInstallError("blocking component prerequisite action is unresolved")
    path_budget = payload.get("path_budget")
    if not isinstance(path_budget, dict):
        raise ComponentInstallError("install plan path budget is invalid")
    if any(
        isinstance(value, dict) and value.get("status") == "blocking"
        for value in path_budget.values()
    ):
        raise ComponentInstallError("component path budget exceeds the Windows limit")


def _approved_ffmpeg_path(plan: InstallPlan) -> str:
    components = plan.payload.get("components")
    ffmpeg = components.get("ffmpeg") if isinstance(components, dict) else None
    resolved = ffmpeg.get("resolved_path") if isinstance(ffmpeg, dict) else None
    if not isinstance(resolved, str) or not Path(resolved).is_absolute():
        raise ComponentInstallError("approved persistent FFmpeg selection is invalid")
    return resolved


def apply_install_plan(
    managed_root: Path,
    cache_root: Path,
    *,
    approved_plan_hash: str,
    install_root: Path | None = None,
    catalog: ReleaseCatalog | None = None,
    external_manifest_path: Path | None = None,
    external_python: Path | None = None,
    platform: str | None = None,
    architecture: str | None = None,
    ffmpeg_command: str = "ffmpeg",
    ffprobe_command: str = "ffprobe",
    python_executable: Path | None = None,
    verify_components: bool = False,
    include_audalign: bool = False,
    include_bbc_audio_offset_finder: bool = False,
    phase_callback: Callable[[str], None] | None = None,
    preflight_plan: InstallPlan | None = None,
) -> ApplyResult:
    """Apply exactly one preflighted approved plan and install missing groups."""

    from roughcut.adapters.component_download import download_artifact
    from roughcut.adapters.component_environment import (
        current_architecture,
        current_platform,
    )

    if preflight_plan is not None:
        if catalog is None:
            raise StaleApprovedPlanError(
                "Roughcut bootstrap cannot adopt a preflight plan without its release catalog"
            )
        selected_catalog = catalog
        current = preflight_plan
    else:
        selected_catalog = catalog or load_release_catalog()
        current = build_install_plan(
            managed_root,
            cache_root,
            catalog=selected_catalog,
            install_root=install_root,
            external_manifest_path=external_manifest_path,
            external_python=external_python,
            platform=platform,
            architecture=architecture,
            ffmpeg_command=ffmpeg_command,
            ffprobe_command=ffprobe_command,
            verify_components=verify_components,
            include_audalign=include_audalign,
            include_bbc_audio_offset_finder=include_bbc_audio_offset_finder,
        )
    validate_install_preflight(
        current,
        approved_plan_hash,
        catalog=selected_catalog,
    )
    if install_root is not None and not verify_components:
        raise ComponentInstallError(
            "Roughcut bootstrap only publishes runtime binding after full verification"
        )
    if phase_callback is not None:
        phase_callback("component_installation_preparing")
    approved_runtime_status: dict[str, object] | None = None
    if install_root is not None:
        runtime_status = current.payload.get("runtime_binding")
        if not isinstance(runtime_status, dict):
            raise ComponentInstallError(
                "Roughcut bootstrap component plan runtime binding state is invalid"
            )
        approved_runtime_status = runtime_status
    target = current.payload["target"]
    if not isinstance(target, dict) or target.get("apply_supported_on_this_host") is not True:
        raise ComponentInstallError("this pinned profile cannot be applied on the current host")
    target_platform = platform or current_platform()
    target_architecture = architecture or current_architecture()
    profile = selected_catalog.profile_for(target_platform, target_architecture)
    missing_raw = current.payload["missing_managed_groups"]
    if not isinstance(missing_raw, list) or not all(isinstance(item, str) for item in missing_raw):
        raise ComponentInstallError("install plan missing-component state is invalid")
    missing = tuple(missing_raw)
    root = managed_root.resolve(strict=False)
    cache = cache_root.resolve(strict=False)
    if not missing:
        components = current.payload["components"]
        if not isinstance(components, dict):
            raise ComponentInstallError("install plan diagnostics are invalid")
        result = ApplyResult(
            approved_plan_hash=current.plan_hash,
            installed_groups=(),
            reused=True,
            manifest_path=(
                str(root / COMPONENT_MANIFEST_FILENAME)
                if (root / COMPONENT_MANIFEST_FILENAME).is_file()
                else None
            ),
            diagnostics=components,
        )
        return _publish_apply_runtime(
            result,
            plan=current,
            install_root=install_root,
            external_manifest_path=external_manifest_path,
            approved_runtime_status=approved_runtime_status,
            phase_callback=phase_callback,
        )

    estimated = current.payload["estimated_additional_disk_bytes"]
    download_bytes = current.payload["download_bytes"]
    installed_bytes = current.payload["estimated_installed_bytes"]
    if isinstance(estimated, bool) or not isinstance(estimated, int):
        raise ComponentInstallError("install plan disk estimate is invalid")
    if isinstance(download_bytes, bool) or not isinstance(download_bytes, int):
        raise ComponentInstallError("install plan disk estimate is invalid")
    if isinstance(installed_bytes, bool) or not isinstance(installed_bytes, int):
        raise ComponentInstallError("install plan disk estimate is invalid")
    managed_parent = _nearest_existing_parent(root)
    cache_parent = _nearest_existing_parent(cache)
    if _same_storage_volume(managed_parent, cache_parent):
        if _available_bytes(managed_parent) < estimated:
            raise ComponentInstallError("insufficient disk space for the approved install plan")
    elif (
        _available_bytes(managed_parent) < installed_bytes
        or _available_bytes(cache_parent) < download_bytes
    ):
        raise ComponentInstallError("insufficient disk space for the approved install plan")

    required = _unique_artifacts(_artifacts_for_groups(profile, missing), cache)
    if phase_callback is not None:
        phase_callback("component_installation_downloading")
    for artifact in required:
        download_artifact(artifact, cache)

    if phase_callback is not None:
        phase_callback("component_installation_installing")
    executable = (python_executable or Path(sys.executable)).resolve(strict=True)
    if sys.version_info[:2] != (3, 11) and executable == Path(sys.executable).resolve():
        raise ComponentInstallError("managed runtime installation requires Python 3.11")
    _install_staged_components(
        root,
        cache,
        profile,
        missing,
        python_executable=executable,
        ffmpeg_command=(
            _approved_ffmpeg_path(current)
            if BBC_AUDIO_OFFSET_FINDER_GROUP_NAME in missing
            else None
        ),
    )
    if phase_callback is not None:
        phase_callback("component_installation_verifying")
    post_plan = build_install_plan(
        managed_root,
        cache_root,
        catalog=selected_catalog,
        install_root=install_root,
        external_manifest_path=external_manifest_path,
        external_python=external_python,
        platform=platform,
        architecture=architecture,
        ffmpeg_command=ffmpeg_command,
        ffprobe_command=ffprobe_command,
        verify_components=verify_components,
        include_audalign=include_audalign,
        include_bbc_audio_offset_finder=include_bbc_audio_offset_finder,
    )
    post_components = post_plan.payload["components"]
    if not isinstance(post_components, dict):
        raise ComponentInstallError("post-install component diagnostics are invalid")
    result = ApplyResult(
        approved_plan_hash=current.plan_hash,
        installed_groups=missing,
        reused=False,
        manifest_path=str(root / COMPONENT_MANIFEST_FILENAME),
        diagnostics=post_components,
    )
    return _publish_apply_runtime(
        result,
        plan=post_plan,
        install_root=install_root,
        external_manifest_path=external_manifest_path,
        approved_plan_hash=current.plan_hash,
        approved_runtime_status=approved_runtime_status,
        phase_callback=phase_callback,
    )


def _publish_apply_runtime(
    result: ApplyResult,
    *,
    plan: InstallPlan,
    install_root: Path | None,
    external_manifest_path: Path | None,
    approved_plan_hash: str | None = None,
    approved_runtime_status: dict[str, object] | None = None,
    phase_callback: Callable[[str], None] | None = None,
) -> ApplyResult:
    if install_root is None:
        return result
    try:
        binding = binding_from_install_plan(
            plan.payload,
            approved_plan_hash=approved_plan_hash or plan.plan_hash,
            install_root=install_root,
            external_manifest_path=external_manifest_path,
        )
        binding_path = Path(
            runtime_binding_path(install_root=install_root.resolve(strict=False))
        )
        if approved_runtime_status is None:
            raise ComponentInstallError(
                "Roughcut bootstrap 拒绝发布 stale component plan"
            )
        if phase_callback is not None:
            phase_callback("component_installation_publishing_runtime")
        published = publish_runtime_binding(
            binding_path,
            binding,
            expected_previous_state=approved_runtime_status,
        )
    except (ComponentError, OSError, RuntimeBindingError) as error:
        reason_code = getattr(error, "reason_code", None)
        if reason_code not in RUNTIME_PUBLISH_REASON_CODES:
            reason_code = "runtime_publish_binding_validation_failed" if isinstance(
                error, (ComponentError, RuntimeBindingError)
            ) else "runtime_publish_failed"
        raise RuntimePublicationError(
            "runtime binding publication failed",
            reason_code=reason_code,
        ) from error
    return ApplyResult(
        approved_plan_hash=result.approved_plan_hash,
        installed_groups=result.installed_groups,
        reused=result.reused,
        manifest_path=result.manifest_path,
        diagnostics=result.diagnostics,
        runtime_binding=published,
    )


def probe_external_python(interpreter: Path) -> dict[str, object]:
    """Actually import one explicit external runtime in Python isolated mode."""

    selected = Path(os.path.abspath(interpreter))
    try:
        payload = probe_python_runtime(selected)
    except ComponentError as error:
        raise ComponentInstallError(
            "external Python runtime probe failed"
        ) from error
    return {
        "interpreter": str(selected),
        **payload,
    }


def cache_artifact_path(cache_root: Path, artifact: ArtifactSpec) -> Path:
    return cache_root / "artifacts" / artifact.sha256[:2] / artifact.sha256 / artifact.filename


def cache_receipt_path(artifact_path: Path) -> Path:
    return artifact_path.with_name(f"{artifact_path.name}.verified.json")


def _artifacts_for_groups(
    profile: ReleaseProfile, groups: tuple[str, ...]
) -> tuple[ArtifactSpec, ...]:
    artifacts: list[ArtifactSpec] = []
    for group in groups:
        if group == "python_runtime":
            artifacts.extend(profile.runtime.artifacts)
        elif group in profile.models:
            artifacts.extend(profile.models[group].artifacts)
        else:
            alignment_group = profile.alignment_group(group)
            if alignment_group is None:
                raise ComponentInstallError(
                    "install plan contains an unsupported managed group"
                )
            artifacts.extend(alignment_group.artifacts)
    return tuple(artifacts)


def _managed_alignment_group_available(
    spec: AlignmentGroupSpec,
    managed: ComponentManifest | None,
    root: Path,
    target_platform: str,
    target_architecture: str,
) -> bool:
    """Reuse a managed alignment group only after full in-place verification:
    exact platform closure, distribution list closed/sorted/unique with exact
    name+version pairs, lock/license receipts verified against the actual
    files (exact SHA), a live importlib.metadata probe of the full installed
    distribution closure, and a safe interpreter/venv tree."""
    if managed is None:
        return False
    if managed.platform != target_platform or managed.architecture != target_architecture:
        return False
    if managed.managed_root is None:
        return False
    if Path(managed.managed_root).resolve(strict=False) != root.resolve(strict=False):
        return False
    group_records = [
        record for record in managed.components
        if record.name == spec.managed_record_name
    ]
    if len(group_records) != 1 or group_records[0].source_type != "managed":
        return False
    record = group_records[0]
    if record.version != spec.version:
        return False
    # exact distribution name+version closure must match the frozen table
    receipt_path = root / PurePosixPath(record.path)
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(receipt, dict):
        return False
    distributions = receipt.get("distributions")
    if not isinstance(distributions, list):
        return False
    # exact list comparison: frozen order and length, every entry closed to
    # exactly {"name","version"}; converting to a dict would silently lose
    # duplicate entries, so duplicates are compared and rejected here
    parsed_entries: list[tuple[str, str]] = []
    for item in distributions:
        if not isinstance(item, dict) or set(item) != {"name", "version"}:
            return False
        name = item.get("name")
        version = item.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            return False
        parsed_entries.append((name, version))
    expected_entries = list(spec.distributions)
    if len(parsed_entries) != len(expected_entries):
        return False
    if len({entry for entry in parsed_entries}) != len(parsed_entries):
        return False
    if parsed_entries != expected_entries:
        return False
    # lock/license receipts must match the actual files on disk, not just the
    # JSON self-description
    lock_receipt = receipt.get("dependency_lock_receipt")
    license_receipt = receipt.get("license_notice_receipt")
    if (
        not isinstance(lock_receipt, dict)
        or lock_receipt.get("value") != spec.dependency_lock_sha256
        or not isinstance(license_receipt, dict)
        or license_receipt.get("value") != spec.license_notice_sha256
    ):
        return False
    lock_path = (
        root
        / PurePosixPath(spec.managed_dir)
        / "locks"
        / spec.dependency_lock.name
    )
    license_path = (
        root
        / PurePosixPath(spec.managed_dir)
        / "licenses"
        / spec.license_notice_file.name
    )
    try:
        if (
            not lock_path.is_file()
            or lock_path.is_symlink()
            or _file_sha256(lock_path) != spec.dependency_lock_sha256
            or not license_path.is_file()
            or license_path.is_symlink()
            or _file_sha256(license_path) != spec.license_notice_sha256
        ):
            return False
    except OSError:
        return False
    # full verification probe of the venv interpreter and pinned closure
    from roughcut.adapters.component_environment import _probe_alignment_group

    reason = _probe_alignment_group(
        record,
        managed,
        managed_root_override=root,
        managed_dir=spec.managed_dir,
        provider=spec.provider,
    )
    return reason is None


def _install_staged_components(
    root: Path,
    cache: Path,
    profile: ReleaseProfile,
    groups: tuple[str, ...],
    *,
    python_executable: Path,
    ffmpeg_command: str | None,
) -> ComponentManifest:
    if root.exists() and not root.is_dir():
        raise ComponentInstallError("managed root exists and is not a directory")
    manifest_path = root / COMPONENT_MANIFEST_FILENAME
    existing = load_component_manifest(manifest_path) if manifest_path.is_file() else None
    if existing is not None:
        if Path(existing.managed_root or "").resolve(strict=False) != root:
            raise ComponentInstallError("component manifest belongs to a different managed root")
        if existing.platform != profile.platform or existing.architecture != profile.architecture:
            raise ComponentInstallError("managed manifest target differs from approved profile")
    elif root.exists() and any(root.iterdir()):
        raise ComponentInstallError("managed root contains unowned files and has no manifest")

    old_manifest_bytes = (
        manifest_path.read_bytes() if existing is not None else None
    )
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = _create_managed_staging(root.parent)
    new_records: list[ComponentRecord] = []
    new_runtime: PythonRuntimeRecord | None = None
    publish_paths: list[PurePosixPath] = []
    try:
        if "python_runtime" in groups:
            runtime_records, new_runtime, runtime_paths = _stage_runtime(
                staging,
                cache,
                profile,
                python_executable=python_executable,
            )
            new_records.extend(runtime_records)
            publish_paths.extend(runtime_paths)
        for name in MODEL_COMPONENT_NAMES:
            if name not in groups:
                continue
            record = _stage_model(staging, cache, profile, name)
            new_records.append(record)
            publish_paths.append(PurePosixPath(record.path))
        for group_name in (AUDALIGN_GROUP_NAME, BBC_AUDIO_OFFSET_FINDER_GROUP_NAME):
            if group_name not in groups:
                continue
            alignment_spec = profile.alignment_group(group_name)
            if alignment_spec is None:
                raise ComponentInstallError("install plan contains an unsupported managed group")
            group_records, group_paths = _stage_alignment_group(
                staging,
                cache,
                profile,
                alignment_spec,
                python_executable=python_executable,
            )
            new_records.extend(group_records)
            publish_paths.extend(group_paths)

        records_by_name = {
            component.name: component
            for component in (existing.components if existing is not None else ())
        }
        records_by_name.update({component.name: component for component in new_records})
        python_runtime = new_runtime or (existing.python_runtime if existing is not None else None)
        manifest = ComponentManifest(
            components=tuple(records_by_name.values()),
            platform=profile.platform,
            architecture=profile.architecture,
            managed_root=str(root),
            schema_version=2 if python_runtime is not None else 1,
            python_runtime=python_runtime,
        )
        write_component_manifest(staging / COMPONENT_MANIFEST_FILENAME, manifest)
        _validate_staged_groups(
            staging,
            profile,
            new_records,
            new_runtime,
            ffmpeg_command=ffmpeg_command,
        )

        if not root.exists():
            os.replace(staging, root)
            _sync_directory(root.parent)
            try:
                _verify_published_manifest(manifest, profile)
            except BaseException:
                shutil.rmtree(root)
                _sync_directory(root.parent)
                raise
            return manifest

        owned_paths = _managed_group_owned_paths(
            existing,
            profile,
            root,
            groups,
        )
        publish_path_set = set(publish_paths)
        obsolete_paths = owned_paths - publish_path_set
        transaction_paths = list(dict.fromkeys(publish_paths))
        transaction_paths.extend(
            sorted(
                obsolete_paths,
                key=lambda relative: relative.as_posix(),
            )
        )
        swaps: list[_ManagedSwap] = []
        try:
            for relative in transaction_paths:
                source = staging / relative
                destination = _safe_managed_destination(root, relative)
                source_exists = os.path.lexists(source)
                if source_exists and (
                    source.is_symlink()
                    or not _managed_destination_has_expected_shape(source, relative)
                ):
                    raise ComponentInstallError(
                        "staged managed destination is invalid"
                    )
                if not source_exists and relative not in obsolete_paths:
                    raise ComponentInstallError(
                        "staged managed destination is missing before publish"
                    )
                destination_exists = os.path.lexists(destination)
                if destination_exists and (
                    destination.is_symlink()
                    or relative not in owned_paths
                    or not _managed_destination_has_expected_shape(destination, relative)
                ):
                    raise ComponentInstallError("managed destination appeared before publish")
                swap = _ManagedSwap(relative=relative, destination=destination)
                swaps.append(swap)
                if destination_exists:
                    backup = _create_managed_backup(destination.parent)
                    os.replace(destination, backup)
                    swap.backup = backup
                destination.parent.mkdir(parents=True, exist_ok=True)
                if source_exists:
                    os.replace(source, destination)
                    swap.published = True
            write_component_manifest(manifest_path, manifest)
            _sync_directory(root)
            _verify_published_manifest(manifest, profile)
        except BaseException:
            try:
                _rollback_managed_swaps(
                    swaps,
                    manifest_path,
                    old_manifest_bytes,
                    root,
                )
            except BaseException as rollback_error:
                raise ComponentInstallError(
                    "managed component rollback failed"
                ) from rollback_error
            raise
        # A verified published manifest commits the managed swap.  Backup
        # removal below is cleanup only and must never reopen rollback.
        for swap in swaps:
            if swap.backup is not None:
                try:
                    _remove_owned_path(swap.backup)
                except OSError:
                    continue
        _sync_directory(root)
        return manifest
    except BaseException as error:
        if staging.exists():
            shutil.rmtree(staging)
        if isinstance(error, (KeyboardInterrupt, SystemExit, ComponentInstallError)):
            raise
        raise ComponentInstallError("managed components could not be installed") from error
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _managed_group_owned_paths(
    existing: ComponentManifest | None,
    profile: ReleaseProfile,
    root: Path,
    groups: tuple[str, ...],
) -> set[PurePosixPath]:
    """Return only canonical paths explicitly owned by the requested groups.

    A missing record is allowed for first install and is deliberately not
    treated as ownership.  Any existing record for a requested group must be
    a managed record with the exact canonical path and target, otherwise the
    publish remains fail-closed.
    """

    if existing is None:
        return set()
    if (
        existing.managed_root is None
        or Path(existing.managed_root).resolve(strict=False) != root.resolve(strict=False)
        or existing.platform != profile.platform
        or existing.architecture != profile.architecture
    ):
        raise ComponentInstallError("managed destination appeared before publish")
    records = {record.name: record for record in existing.components}
    owned: set[PurePosixPath] = set()

    def require_managed_record(
        record: ComponentRecord | None,
        expected_path: str,
    ) -> ComponentRecord:
        if (
            record is None
            or record.source_type != "managed"
            or record.platform != profile.platform
            or record.architecture != profile.architecture
            or record.path != expected_path
        ):
            raise ComponentInstallError("managed destination appeared before publish")
        return record

    for group in groups:
        if group == "python_runtime":
            runtime = existing.python_runtime
            package_records = [records.get(name) for name in PYTHON_COMPONENT_NAMES]
            has_existing_runtime_group = runtime is not None or any(
                record is not None for record in package_records
            )
            if not has_existing_runtime_group:
                continue
            if runtime is None:
                raise ComponentInstallError("managed destination appeared before publish")
            for name, record in zip(PYTHON_COMPONENT_NAMES, package_records):
                require_managed_record(record, f"packages/{name}/receipt.json")
                owned.add(PurePosixPath("packages") / name)
            expected_interpreter = (
                "venv/Scripts/python.exe"
                if profile.platform == "windows"
                else "venv/bin/python"
            )
            expected_lock = f"locks/{profile.runtime.dependency_lock.name}"
            if (
                runtime.root != "venv"
                or runtime.interpreter != expected_interpreter
                or runtime.dependency_lock != expected_lock
            ):
                raise ComponentInstallError("managed destination appeared before publish")
            owned.add(PurePosixPath(runtime.root))
            owned.add(PurePosixPath(runtime.dependency_lock))
            continue

        if group in MODEL_COMPONENT_NAMES:
            record = records.get(group)
            if record is None:
                continue
            require_managed_record(record, f"models/{group}")
            owned.add(PurePosixPath("models") / group)
            continue

        alignment_spec = profile.alignment_group(group)
        if alignment_spec is None:
            raise ComponentInstallError("install plan contains an unsupported managed group")
        record = records.get(alignment_spec.managed_record_name)
        if record is None:
            continue
        require_managed_record(
            record,
            f"{alignment_spec.managed_dir}/venv-receipt.json",
        )
        owned.add(PurePosixPath(alignment_spec.managed_dir))
    return owned


def _safe_managed_destination(root: Path, relative: PurePosixPath) -> Path:
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ComponentInstallError("managed destination appeared before publish")
    destination = root.joinpath(*relative.parts)
    try:
        root_resolved = root.resolve(strict=False)
        if not destination.resolve(strict=False).is_relative_to(root_resolved):
            raise ComponentInstallError("managed destination appeared before publish")
        if not destination.parent.resolve(strict=False).is_relative_to(root_resolved):
            raise ComponentInstallError("managed destination appeared before publish")
    except OSError as error:
        raise ComponentInstallError("managed destination appeared before publish") from error
    if destination.is_symlink():
        raise ComponentInstallError("managed destination appeared before publish")
    return destination


def _managed_destination_has_expected_shape(
    path: Path,
    relative: PurePosixPath,
) -> bool:
    if relative.parts[0] == "locks":
        return path.is_file() and not path.is_symlink()
    return path.is_dir() and not path.is_symlink()


def _create_managed_backup(parent: Path) -> Path:
    for _attempt in range(OWNED_TEMP_CREATE_ATTEMPTS):
        backup = parent / (
            MANAGED_BACKUP_PREFIX + secrets.token_hex(OWNED_TEMP_TOKEN_BYTES)
        )
        if not os.path.lexists(backup):
            return backup
    raise ComponentInstallError("managed component backup identity was unavailable")


def _rollback_managed_swaps(
    swaps: list[_ManagedSwap],
    manifest_path: Path,
    old_manifest_bytes: bytes | None,
    root: Path,
) -> None:
    for swap in reversed(swaps):
        if swap.published and os.path.lexists(swap.destination):
            _remove_owned_path(swap.destination)
        if swap.backup is not None and os.path.lexists(swap.backup):
            os.replace(swap.backup, swap.destination)
    if old_manifest_bytes is None:
        manifest_path.unlink(missing_ok=True)
    else:
        _write_bytes_atomic(manifest_path, old_manifest_bytes)
    _sync_directory(root)


def _create_managed_staging(parent: Path) -> Path:
    for _attempt in range(OWNED_TEMP_CREATE_ATTEMPTS):
        staging = parent / (
            MANAGED_STAGING_PREFIX + secrets.token_hex(OWNED_TEMP_TOKEN_BYTES)
        )
        try:
            staging.mkdir(mode=0o700)
        except FileExistsError:
            continue
        except OSError as error:
            raise ComponentInstallError(
                "managed installer staging root could not be created"
            ) from error
        return staging
    raise ComponentInstallError("managed installer staging root identity was unavailable")


def _stage_runtime(
    staging: Path,
    cache: Path,
    profile: ReleaseProfile,
    *,
    python_executable: Path,
) -> tuple[list[ComponentRecord], PythonRuntimeRecord, list[PurePosixPath]]:
    venv = staging / "venv"
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("PYTHON") and name not in VIRTUAL_ENVIRONMENT_VARIABLES
    }
    create = subprocess.run(
        [str(python_executable), "-I", "-m", "venv", str(venv)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    if create.returncode != 0:
        raise ComponentInstallError("managed Python virtual environment creation failed")
    _install_runtime_artifacts(
        python_executable,
        venv,
        cache,
        profile,
        environment=environment,
    )

    lock_relative = PurePosixPath("locks") / profile.runtime.dependency_lock.name
    lock_destination = staging / lock_relative
    lock_destination.parent.mkdir(parents=True)
    shutil.copy2(profile.runtime.dependency_lock, lock_destination)
    if _file_sha256(lock_destination) != profile.runtime.dependency_lock_sha256:
        raise ArtifactVerificationError("copied dependency lock checksum differs")

    records: list[ComponentRecord] = []
    direct = {artifact.name: artifact for artifact in profile.runtime.artifacts}
    for name in PYTHON_COMPONENT_NAMES:
        artifact = direct[name]
        relative = PurePosixPath("packages") / name / "receipt.json"
        receipt = staging / relative
        _write_json_atomic(
            receipt,
            {
                "schema_version": 1,
                "name": name,
                "version": profile.runtime.versions[name],
                "artifact": artifact.filename,
                "source": artifact.url,
                "sha256": artifact.sha256,
                "license": artifact.license,
            },
        )
        records.append(
            ComponentRecord(
                name=name,
                kind="python_package",
                source_type="managed",
                origin=artifact.url,
                version=profile.runtime.versions[name],
                path=relative.as_posix(),
                platform=profile.platform,
                architecture=profile.architecture,
                license=artifact.license,
                verification=ComponentVerification("sha256", component_digest(receipt)),
            )
        )
    interpreter = "venv/Scripts/python.exe" if profile.platform == "windows" else "venv/bin/python"
    runtime = PythonRuntimeRecord(
        root="venv",
        interpreter=interpreter,
        dependency_lock=lock_relative.as_posix(),
        lock_origin=profile.runtime.dependency_lock_origin,
        lock_verification=ComponentVerification(
            "sha256", profile.runtime.dependency_lock_sha256
        ),
        device="cpu",
        python_version=profile.python_version,
    )
    publish = [
        PurePosixPath("venv"),
        lock_relative,
        *[PurePosixPath("packages") / name for name in PYTHON_COMPONENT_NAMES],
    ]
    return records, runtime, publish


def _install_runtime_artifacts(
    python_executable: Path,
    venv: Path,
    cache: Path,
    profile: ReleaseProfile,
    *,
    environment: dict[str, str],
) -> None:
    artifacts = profile.runtime.artifacts
    if any(not artifact.filename.endswith(".whl") for artifact in artifacts):
        setuptools = next(
            (artifact for artifact in artifacts if artifact.name == "setuptools"), None
        )
        if setuptools is None:
            raise ComponentInstallError("pinned managed setuptools artifact is missing")
        bootstrap = subprocess.run(
            [
                str(python_executable),
                "-I",
                "-m",
                "pip",
                "--python",
                str(venv),
                "install",
                "--no-index",
                "--no-deps",
                "--progress-bar",
                "off",
                str(cache_artifact_path(cache, setuptools)),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        if bootstrap.returncode != 0:
            raise ComponentInstallError("pinned managed setuptools could not be installed")

    artifact_paths = [cache_artifact_path(cache, artifact) for artifact in artifacts]
    install = subprocess.run(
        [
            str(python_executable),
            "-I",
            "-m",
            "pip",
            "--python",
            str(venv),
            "install",
            "--no-index",
            "--no-deps",
            "--no-build-isolation",
            "--progress-bar",
            "off",
            *[str(path) for path in artifact_paths],
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    if install.returncode != 0:
        detail = (
            (install.stderr or "")[-4000:]
        )
        raise ComponentInstallError(
            "pinned managed Python artifacts could not be installed: "
            + repr(detail)
        )


def _stage_model(
    staging: Path,
    cache: Path,
    profile: ReleaseProfile,
    name: str,
) -> ComponentRecord:
    model = profile.models[name]
    relative = PurePosixPath("models") / name
    destination = staging / relative
    for artifact in model.artifacts:
        if artifact.destination is None:
            raise ComponentInstallError("model catalog destination is missing")
        source = cache_artifact_path(cache, artifact)
        target = destination / PurePosixPath(artifact.destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    if component_digest(destination) != model.directory_sha256:
        raise ArtifactVerificationError("installed model directory checksum differs")
    return ComponentRecord(
        name=name,
        kind="model",
        source_type="managed",
        origin=f"https://modelscope.cn/models/{model.repository}/files",
        version=model.revision,
        path=relative.as_posix(),
        platform=profile.platform,
        architecture=profile.architecture,
        license=model.license,
        verification=ComponentVerification("sha256", model.directory_sha256),
    )


def _repair_macos_copies_venv(venv: Path, python_executable: Path) -> None:
    """Make a `--copies` venv bootable on macOS framework Pythons.

    `--copies` produces a real interpreter binary whose rpath points at
    <venv>/lib/libpython3.11.dylib; framework builds keep that library in the
    base prefix, so we copy it into the venv lib dir and refresh the stdlib
    from the base prefix (the copied venv otherwise cannot find encodings).
    """
    base = Path(python_executable).resolve(strict=True).parent.parent
    lib_dir = venv / "lib"
    lib_dir.mkdir(parents=True, exist_ok=True)
    base_lib = base / "lib" / "libpython3.11.dylib"
    if base_lib.is_file():
        target = lib_dir / "libpython3.11.dylib"
        if not target.exists():
            shutil.copy2(base_lib, target)
    base_stdlib = base / "lib" / "python3.11"
    if base_stdlib.is_dir() and not (lib_dir / "python3.11").exists():
        shutil.copytree(base_stdlib, lib_dir / "python3.11")


def _stage_alignment_group(
    staging: Path,
    cache: Path,
    profile: ReleaseProfile,
    spec: AlignmentGroupSpec,
    *,
    python_executable: Path,
) -> tuple[list[ComponentRecord], list[PurePosixPath]]:
    """Create one isolated managed alignment venv and receipts inside staging."""
    group_dir = spec.managed_dir
    venv = staging / group_dir / "venv"
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("PYTHON") and name not in VIRTUAL_ENVIRONMENT_VARIABLES
    }
    create_command = [
        str(python_executable),
        "-I",
        "-m",
        "venv",
        "--copies",
        "--without-pip",
        str(venv),
    ]
    create = subprocess.run(
        create_command,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    if create.returncode != 0:
        raise ComponentInstallError(
            f"{group_dir} managed virtual environment creation failed"
        )
    if profile.platform == "macos":
        _repair_macos_copies_venv(venv, python_executable)
    _install_alignment_artifacts(
        python_executable,
        venv,
        cache,
        spec,
        environment=environment,
    )
    lock_relative = PurePosixPath(group_dir) / "locks" / spec.dependency_lock.name
    lock_destination = staging / lock_relative
    lock_destination.parent.mkdir(parents=True)
    shutil.copy2(spec.dependency_lock, lock_destination)
    if _file_sha256(lock_destination) != spec.dependency_lock_sha256:
        raise ArtifactVerificationError(
            "copied alignment dependency lock checksum differs"
        )
    license_relative = (
        PurePosixPath(group_dir) / "licenses" / spec.license_notice_file.name
    )
    license_destination = staging / license_relative
    license_destination.parent.mkdir(parents=True)
    shutil.copy2(spec.license_notice_file, license_destination)
    if _file_sha256(license_destination) != spec.license_notice_sha256:
        raise ArtifactVerificationError(
            "copied alignment license notice checksum differs"
        )

    records: list[ComponentRecord] = []
    direct = {artifact.name: artifact for artifact in spec.artifacts}
    package_receipts: dict[str, object] = {}
    for name, version in spec.distributions:
        artifact = direct[name]
        package_receipts[name] = {
            "version": version,
            "artifact": artifact.filename,
            "source": artifact.url,
            "sha256": artifact.sha256,
            "license": artifact.license,
        }
    interpreter = (
        f"{group_dir}/venv/Scripts/python.exe"
        if profile.platform == "windows"
        else f"{group_dir}/venv/bin/python"
    )
    receipt_identity: dict[str, object]
    if spec.provider == "audalign":
        # historical audalign receipts keep their legacy identity keys so the
        # published bytes of an unchanged group stay identical across reinstalls
        assert spec.upstream_commit is not None
        receipt_identity = {
            "audalign_version": spec.version,
            "audalign_upstream_commit": spec.upstream_commit,
        }
    else:
        receipt_identity = {
            "provider": spec.provider,
            "provider_version": spec.version,
        }
    venv_receipt = staging / group_dir / "venv-receipt.json"
    _write_json_atomic(
        venv_receipt,
        {
            "schema_version": 1,
            "interpreter": interpreter,
            "python_version": "3.11",
            **receipt_identity,
            "dependency_lock_receipt": {
                "algorithm": "sha256",
                "value": spec.dependency_lock_sha256,
            },
            "license_notice_receipt": {
                "algorithm": "sha256",
                "value": spec.license_notice_sha256,
            },
            "distributions": [
                {"name": name, "version": version}
                for name, version in spec.distributions
            ],
            "packages": package_receipts,
        },
    )
    records.append(
        ComponentRecord(
            name=group_dir,
            kind="python_package",
            source_type="managed",
            origin=spec.origin,
            version=spec.version,
            path=f"{group_dir}/venv-receipt.json",
            platform=profile.platform,
            architecture=profile.architecture,
            license=spec.record_license,
            verification=ComponentVerification(
                "sha256", component_digest(venv_receipt)
            ),
        )
    )
    publish = [
        PurePosixPath(group_dir),
    ]
    return records, publish


def _install_alignment_artifacts(
    python_executable: Path,
    venv: Path,
    cache: Path,
    spec: AlignmentGroupSpec,
    *,
    environment: dict[str, str],
) -> None:
    artifact_paths = [cache_artifact_path(cache, artifact) for artifact in spec.artifacts]
    install = subprocess.run(
        [
            str(python_executable),
            "-I",
            "-m",
            "pip",
            "--python",
            str(venv),
            "install",
            "--no-index",
            "--no-deps",
            "--no-build-isolation",
            *[str(path) for path in artifact_paths],
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    if install.returncode != 0:
        raise ComponentInstallError("pinned alignment artifacts could not be installed")


def _validate_staged_groups(
    staging: Path,
    profile: ReleaseProfile,
    records: list[ComponentRecord],
    runtime: PythonRuntimeRecord | None,
    *,
    ffmpeg_command: str | None,
) -> None:
    runtime_records = [record for record in records if record.kind == "python_package"]
    if runtime is not None:
        manifest = ComponentManifest(
            components=tuple(runtime_records),
            platform=profile.platform,
            architecture=profile.architecture,
            managed_root=str(staging),
            schema_version=2,
            python_runtime=runtime,
        )
        expected = {
            **profile.runtime.versions,
            **{name: model.revision for name, model in profile.models.items()},
        }
        diagnosis = diagnose_components(
            managed_manifest=manifest,
            expected_versions=expected,
            managed_root_override=staging,
            platform=profile.platform,
            architecture=profile.architecture,
            verify_checksums=True,
        )
        failures = [
            diagnosis.components[record.name]
            for record in runtime_records
            if diagnosis.components[record.name].selected_source != "managed"
        ]
        if failures:
            details = []
            for failure in failures:
                detail = next(
                    (
                        attempt.detail
                        for attempt in reversed(failure.attempts)
                        if attempt.detail is not None
                    ),
                    failure.status,
                )
                details.append(f"{failure.name}: {detail}")
            raise ComponentInstallError(
                "staged managed components failed full verification: "
                + "; ".join(details)
            )
    _validate_staged_alignment_group(
        staging, records, profile, AUDALIGN_GROUP_NAME, ffmpeg_command=ffmpeg_command
    )
    _validate_staged_alignment_group(
        staging,
        records,
        profile,
        BBC_AUDIO_OFFSET_FINDER_GROUP_NAME,
        ffmpeg_command=ffmpeg_command,
    )


def _validate_staged_alignment_group(
    staging: Path,
    records: list[ComponentRecord],
    profile: ReleaseProfile,
    group_name: str,
    *,
    ffmpeg_command: str | None,
) -> None:
    """Verify one staged alignment venv interpreter and receipts in place."""
    alignment_spec = profile.alignment_group(group_name)
    if alignment_spec is None:
        return
    group_records = [
        record for record in records if record.name == alignment_spec.managed_record_name
    ]
    if not group_records:
        return
    if len(group_records) != 1:
        raise ComponentInstallError(f"staged {group_name} records are invalid")
    venv_root = staging / alignment_spec.managed_dir / "venv"
    interpreter = (
        venv_root / "Scripts/python.exe"
        if profile.platform == "windows"
        else venv_root / "bin/python"
    )
    if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        raise ComponentInstallError(
            f"staged {group_name} interpreter is missing or not executable"
        )
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("PYTHON") and name not in VIRTUAL_ENVIRONMENT_VARIABLES
    }
    direct_distribution = alignment_spec.direct_distribution
    probe = (
        "import importlib.metadata as m,json,sys;"
        "payload={'python_version':f'{sys.version_info.major}.{sys.version_info.minor}',"
        + "'"
        + direct_distribution
        + "':m.version('"
        + direct_distribution
        + "')};"
        "sys.stdout.buffer.write(b'ROUGHCUT-PROBE/1 '+"
        "json.dumps(payload,ensure_ascii=False,separators=(',',':'),sort_keys=True).encode('utf-8')+b'\\n')"
    )
    result = subprocess.run(
        [str(interpreter), "-I", "-c", probe],
        check=False,
        capture_output=True,
        env=environment,
        timeout=PYTHON_RUNTIME_PROBE_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise ComponentInstallError(f"staged {group_name} runtime probe failed")
    try:
        payload = _parse_probe_frame(
            result.stdout,
            frozenset({"python_version", direct_distribution}),
        )
    except ComponentError as error:
        raise ComponentInstallError(
            f"staged {group_name} runtime probe returned invalid JSON"
        ) from error
    expected_version = dict(alignment_spec.distributions)[direct_distribution]
    if (
        not isinstance(payload, dict)
        or payload.get("python_version") != "3.11"
        or payload.get(direct_distribution) != expected_version
    ):
        raise ComponentInstallError(f"staged {group_name} runtime versions differ")
    venv_receipt = staging / alignment_spec.managed_dir / "venv-receipt.json"
    receipt_record = group_records[0]
    if receipt_record.verification.value != component_digest(venv_receipt):
        raise ArtifactVerificationError(f"staged {group_name} receipt verification differs")
    if group_name == BBC_AUDIO_OFFSET_FINDER_GROUP_NAME:
        if ffmpeg_command is None:
            raise ComponentInstallError(
                "staged bbc_audio_offset_finder smoke requires approved FFmpeg"
            )
        _run_staged_bbc_smoke(
            staging,
            profile,
            alignment_spec,
            receipt_record,
            ffmpeg_command=ffmpeg_command,
        )


class _BbcSmokeDeadline:
    def remaining(self) -> float:
        return 120.0


def _write_bbc_smoke_wav(path: Path, *, shift_frames: int = 0) -> None:
    sample_rate = 16_000
    frame_count = 4 * sample_rate
    samples: list[int] = []
    state = 0x1234ABCD
    for index in range(frame_count):
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        noise = (state / 0x7FFFFFFF) * 2.0 - 1.0
        phase = index / sample_rate
        value = int(9000 * math.sin(2 * math.pi * (180 + 55 * phase) * phase) + 3500 * noise)
        samples.append(max(-32768, min(32767, value)))
    if shift_frames > 0:
        samples = ([0] * shift_frames + samples)[:frame_count]
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def _run_staged_bbc_smoke(
    staging: Path,
    profile: ReleaseProfile,
    spec: AlignmentGroupSpec,
    record: ComponentRecord,
    *,
    ffmpeg_command: str,
) -> None:
    """Exercise the packaged provider through the production worker/adapter."""
    from roughcut.adapters.audio_offset_finder import (
        BbcAdapterError,
        run_bbc_offset_finder,
    )

    interpreter = staging / PurePosixPath(
        f"{spec.managed_dir}/venv/Scripts/python.exe"
        if profile.platform == "windows"
        else f"{spec.managed_dir}/venv/bin/python"
    )
    selection = RuntimeAlignmentPython(
        source_type="managed",
        ownership="roughcut_managed",
        interpreter=str(interpreter),
        python_version="3.11",
        distributions=tuple(
            {"name": name, "version": version} for name, version in spec.distributions
        ),
        dependency_lock_receipt={"algorithm": "sha256", "value": spec.dependency_lock_sha256},
        license_notice_receipt={"algorithm": "sha256", "value": spec.license_notice_sha256},
        component_manifest_receipt={
            "algorithm": "sha256",
            "value": record.verification.value,
        },
        provider=spec.provider,
        provider_version=spec.version,
    )
    try:
        with tempfile.TemporaryDirectory(
            dir=staging.parent, prefix=".roughcut-bbc-smoke-"
        ) as temporary:
            workspace = Path(temporary)
            main_path = workspace / "main.wav"
            auxiliary_path = workspace / "auxiliary.wav"
            _write_bbc_smoke_wav(main_path)
            _write_bbc_smoke_wav(auxiliary_path, shift_frames=4_800)
            result = run_bbc_offset_finder(
                main_path,
                auxiliary_path,
                selection=selection,
                budget=ChildBudget(_BbcSmokeDeadline(), 4 * 1024**3),
                workspace_root=workspace,
                ffmpeg_command=ffmpeg_command,
            )
    except BbcAdapterError as error:
        raise ComponentInstallError("staged BBC production smoke failed") from error
    if not result.native_offset_seconds:
        raise ComponentInstallError("staged BBC production smoke returned no offset")


def _verify_published_manifest(
    manifest: ComponentManifest, profile: ReleaseProfile
) -> None:
    expected = {
        **profile.runtime.versions,
        **{name: model.revision for name, model in profile.models.items()},
    }
    diagnosis = diagnose_components(
        managed_manifest=manifest,
        expected_versions=expected,
        platform=profile.platform,
        architecture=profile.architecture,
        verify_checksums=False,
    )
    if any(
        diagnosis.components[record.name].selected_source != "managed"
        for record in manifest.components
    ):
        raise ComponentInstallError("published managed components failed full verification")


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / (
        f".{hashlib.sha256(str(path).encode('utf-8')).hexdigest()[:12]}.tmp"
    )
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _remove_owned_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _nearest_existing_parent(path: Path) -> Path:
    candidate = path
    while not candidate.exists():
        if candidate == candidate.parent:
            return candidate
        candidate = candidate.parent
    return candidate


def _available_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def _same_storage_volume(first: Path, second: Path) -> bool:
    return first.stat().st_dev == second.stat().st_dev


def _validate_dedicated_roots(managed_root: Path, cache_root: Path) -> None:
    home = Path.home().resolve(strict=False)
    for root in (managed_root, cache_root):
        if root == root.parent or root == home:
            raise ComponentInstallError("component roots must be dedicated subdirectories")
    if (
        managed_root == cache_root
        or managed_root.is_relative_to(cache_root)
        or cache_root.is_relative_to(managed_root)
    ):
        raise ComponentInstallError("managed root and component cache must be separate")


def _sync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _resolve_runtime(
    catalog: ReleaseCatalog,
    profile: ReleaseProfile,
    *,
    external_python: Path | None,
    external_probe: dict[str, object] | None,
    external_probe_error: str | None,
    managed: ComponentManifest | None,
    target_platform: str,
    target_architecture: str,
) -> tuple[dict[str, dict[str, object]], bool, str | None]:
    attempts: list[dict[str, object]] = []
    selected_source: str | None = None
    selected_path: str | None = None
    selected_versions: dict[str, str] | None = None
    selected_external_profile: str | None = None
    if external_probe_error is not None and external_python is not None:
        attempts.append(
            {
                "source_type": "external",
                "status": "unavailable",
                "profile": None,
                "detail": external_probe_error,
            }
        )
    if external_probe is not None and external_python is not None:
        matched = next(
            (
                item
                for item in catalog.external_profiles
                if item.python_version == external_probe["python_version"]
                and item.versions
                == {name: external_probe[name] for name in PYTHON_COMPONENT_NAMES}
                and item.device == "cpu"
                and external_probe["cuda_version"] is None
                and external_probe["cuda_available"] is False
                and (not item.platforms or (target_platform, target_architecture) in item.platforms)
            ),
            None,
        )
        if matched is not None:
            selected_source = "external"
            selected_path = str(Path(os.path.abspath(external_python)))
            selected_versions = matched.versions
            selected_external_profile = matched.id
            attempts.append(
                {
                    "source_type": "external",
                    "status": "available",
                    "profile": matched.id,
                    "detail": None,
                }
            )
        else:
            attempts.append(
                {
                    "source_type": "external",
                    "status": "unsupported",
                    "profile": None,
                    "detail": "external runtime combination is unverified; use the pinned managed profile or a reviewed external profile",
                }
            )

    if selected_source is None and managed is not None:
        diagnosis = diagnose_components(
            managed_manifest=managed,
            expected_versions=profile.runtime.versions,
            platform=target_platform,
            architecture=target_architecture,
        )
        managed_available = all(
            diagnosis.components[name].selected_source == "managed"
            for name in PYTHON_COMPONENT_NAMES
        )
        detail = next(
            (
                attempt.detail
                for name in PYTHON_COMPONENT_NAMES
                for attempt in diagnosis.components[name].attempts
                if attempt.source_type == "managed" and attempt.detail is not None
            ),
            None,
        )
        attempts.append(
            {
                "source_type": "managed",
                "status": "available" if managed_available else "incompatible",
                "profile": profile.id if managed_available else None,
                "detail": detail,
            }
        )
        if managed_available:
            selected_source = "managed"
            selected_versions = profile.runtime.versions
            if managed.python_runtime is None or managed.managed_root is None:
                raise ComponentInstallError("managed runtime manifest is incomplete")
            selected_path = str(
                Path(managed.managed_root) / PurePosixPath(managed.python_runtime.interpreter)
            )

    components: dict[str, dict[str, object]] = {}
    for name in PYTHON_COMPONENT_NAMES:
        components[name] = {
            "status": "available" if selected_source is not None else "install_required",
            "selected_source": selected_source,
            "version": selected_versions[name] if selected_versions is not None else None,
            "path": selected_path,
            "compatible": selected_source is not None,
            "action": (
                f"reuse_{selected_source}" if selected_source is not None else "install_managed"
            ),
            "attempts": attempts,
        }
    return components, selected_source is None, selected_external_profile


def _resolve_models(
    profile: ReleaseProfile,
    *,
    external: ComponentManifest | None,
    managed: ComponentManifest | None,
    external_runtime_profile: str | None,
    target_platform: str,
    target_architecture: str,
    verify_components: bool,
) -> tuple[dict[str, dict[str, object]], tuple[str, ...]]:
    expected = {name: model.revision for name, model in profile.models.items()}
    expected_verifications = {
        name: model.directory_sha256 for name, model in profile.models.items()
    }
    diagnosis = None
    if _is_exact_legacy_external_triplet(
        external,
        external_runtime_profile=external_runtime_profile,
        target_platform=target_platform,
        target_architecture=target_architecture,
    ):
        legacy_versions = {**expected}
        legacy_versions.update(
            dict.fromkeys(LEGACY_EXTERNAL_MODEL_DIGESTS, LEGACY_EXTERNAL_MODEL_VERSION)
        )
        legacy_verifications = {**expected_verifications, **LEGACY_EXTERNAL_MODEL_DIGESTS}
        candidate = diagnose_components(
            external_manifest=external,
            managed_manifest=managed,
            expected_versions=legacy_versions,
            expected_verifications=legacy_verifications,
            platform=target_platform,
            architecture=target_architecture,
            verify_checksums=verify_components,
        )
        if all(
            candidate.components[name].selected_source == "external"
            for name in LEGACY_EXTERNAL_MODEL_DIGESTS
        ):
            diagnosis = candidate
    if diagnosis is None:
        diagnosis = diagnose_components(
            external_manifest=external,
            managed_manifest=managed,
            expected_versions=expected,
            expected_verifications=expected_verifications,
            platform=target_platform,
            architecture=target_architecture,
            verify_checksums=verify_components,
        )
    components: dict[str, dict[str, object]] = {}
    missing: list[str] = []
    for name in MODEL_COMPONENT_NAMES:
        diagnostic = diagnosis.components[name]
        components[name] = diagnostic.to_dict()
        if diagnostic.selected_source is None:
            missing.append(name)
    return components, tuple(missing)


def _is_exact_legacy_external_triplet(
    manifest: ComponentManifest | None,
    *,
    external_runtime_profile: str | None,
    target_platform: str,
    target_architecture: str,
) -> bool:
    if (
        manifest is None
        or manifest.schema_version != 1
        or manifest.managed_root is not None
        or target_platform != "macos"
        or target_architecture != "arm64"
        or manifest.platform != target_platform
        or manifest.architecture != target_architecture
        or external_runtime_profile != LEGACY_EXTERNAL_RUNTIME_PROFILE_ID
    ):
        return False
    records = {record.name: record for record in manifest.components}
    return all(
        (record := records.get(name)) is not None
        and record.kind == "model"
        and record.source_type == "external"
        and record.version == LEGACY_EXTERNAL_MODEL_VERSION
        and record.origin == LEGACY_EXTERNAL_MODEL_ORIGINS[name]
        and record.license == "Apache-2.0"
        and record.verification.algorithm == "sha256"
        and record.verification.value == digest
        for name, digest in LEGACY_EXTERNAL_MODEL_DIGESTS.items()
    )


def _command_plan(diagnostic: CommandDiagnostic) -> dict[str, object]:
    command = diagnostic.command
    requested = Path(command)
    resolved: str | None
    if requested.parent != Path("."):
        resolved = str(requested.resolve(strict=False))
    else:
        resolved = shutil.which(command)
    return {
        **diagnostic.to_dict(),
        "selected_source": "external" if diagnostic.status == "available" else None,
        "resolved_path": resolved,
        "action": "reuse_external" if diagnostic.status == "available" else "user_action_required",
    }


def _cache_status(path: Path, artifact: ArtifactSpec) -> tuple[str, int]:
    receipt_path = cache_receipt_path(path)
    if path.is_file() and path.stat().st_size == artifact.size and receipt_path.is_file():
        try:
            receipt = _read_json_object(receipt_path, "cache receipt")
        except ComponentInstallError:
            receipt = {}
        if receipt == {
            "schema_version": 1,
            "sha256": artifact.sha256,
            "size": artifact.size,
        } and _file_sha256(path) == artifact.sha256:
            return "verified", 0
    part = path.with_name(f"{path.name}.part")
    if part.is_file():
        return "partial", min(part.stat().st_size, artifact.size)
    return "missing", 0


def artifact_source_is_allowed(url: str, *, allow_loopback_http: bool) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" or (
        allow_loopback_http
        and parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    )


def _validate_runtime_dependency_lock(
    path: Path,
    artifacts: tuple[ArtifactSpec, ...],
) -> None:
    """Require the uv-compiled runtime lock to be the exact artifact closure.

    The managed 82-entry lock (FunASR 1.3.14 / Torch 2.6.0 / Torchaudio 2.6.0)
    must not be replaced by the unrelated 79-line external 1.3.8 freeze:
    names/versions must match the artifact catalog exactly and each artifact
    SHA-256 must appear among that distribution's lock hashes.
    """

    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ComponentInstallError("runtime dependency lock is unreadable") from error
    if b"\r" in raw:
        raise ComponentInstallError("runtime dependency lock must use LF line endings")
    lines = raw.decode("utf-8").splitlines()
    entries: list[tuple[str, str, set[str]]] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line or line.startswith("#"):
            index += 1
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.\-]+)==([^\s]+) \\", line)
        if match is None:
            raise ComponentInstallError("runtime dependency lock is invalid")
        hashes: set[str] = set()
        index += 1
        while index < len(lines) and lines[index].strip().startswith("--hash=sha256:"):
            hash_match = re.fullmatch(
                r"--hash=sha256:([0-9a-f]{64})(?:\s*\\)?", lines[index].strip()
            )
            if hash_match is None:
                raise ComponentInstallError("runtime dependency lock is invalid")
            hashes.add(hash_match.group(1))
            index += 1
        if not hashes:
            raise ComponentInstallError("runtime dependency lock is invalid")
        entries.append((match.group(1), match.group(2), hashes))
    artifacts_by_name = {artifact.name: artifact for artifact in artifacts}
    normalized_names = [re.sub(r"[-_.]+", "-", name).lower() for name, _, _ in entries]
    if (
        set(artifacts_by_name) != {artifact.name for artifact in artifacts}
        or len(artifacts_by_name) != len(artifacts)
        or set(normalized_names) != {re.sub(r"[-_.]+", "-", name).lower() for name in artifacts_by_name}
        or len(normalized_names) != len(entries)
        or len(set(normalized_names)) != len(normalized_names)
    ):
        raise ComponentInstallError("runtime dependency lock is not the exact frozen closure")
    for name, version, hashes in entries:
        canonical = next(
            key
            for key in artifacts_by_name
            if re.sub(r"[-_.]+", "-", key).lower() == re.sub(r"[-_.]+", "-", name).lower()
        )
        artifact = artifacts_by_name[canonical]
        if artifact.version != version or artifact.sha256 not in hashes:
            raise ComponentInstallError(
                "runtime dependency lock is not the exact frozen closure"
            )


def _load_runtime_artifacts(path: Path) -> tuple[ArtifactSpec, ...]:
    raw = _read_json_object(path, "runtime artifact catalog")
    artifacts_raw = raw.get("artifacts")
    if raw.get("schema_version") != 1 or not isinstance(artifacts_raw, list):
        raise ComponentInstallError("runtime artifact catalog is invalid")
    artifacts: list[ArtifactSpec] = []
    for item in artifacts_raw:
        if not isinstance(item, dict):
            raise ComponentInstallError("runtime artifact metadata is invalid")
        artifacts.append(
            ArtifactSpec(
                component="python_runtime",
                name=_required_string(item, "name"),
                version=_required_string(item, "version"),
                filename=_required_string(item, "filename"),
                url=_required_https(item, "url"),
                license=_required_string(item, "license"),
                sha256=_required_string(item, "sha256"),
                size=_required_positive_int(item, "size"),
            )
        )
    if raw.get("artifact_count") != len(artifacts) or len({item.name for item in artifacts}) != len(
        artifacts
    ):
        raise ComponentInstallError("runtime artifact catalog count is invalid")
    return tuple(artifacts)


def _load_models(path: Path) -> dict[str, ModelSpec]:
    raw = _read_json_object(path, "model catalog")
    models_raw = raw.get("models")
    if raw.get("schema_version") != 1 or not isinstance(models_raw, list):
        raise ComponentInstallError("model catalog is invalid")
    models: dict[str, ModelSpec] = {}
    for item in models_raw:
        if not isinstance(item, dict):
            raise ComponentInstallError("model metadata is invalid")
        name = _required_string(item, "name")
        repository = _required_string(item, "repository")
        revision = _required_string(item, "revision")
        license_value = _required_string(item, "license")
        files_raw = item.get("files")
        if name not in MODEL_COMPONENT_NAMES or not isinstance(files_raw, list):
            raise ComponentInstallError("model component is invalid")
        artifacts: list[ArtifactSpec] = []
        for file_item in files_raw:
            if not isinstance(file_item, dict):
                raise ComponentInstallError("model file metadata is invalid")
            destination = _required_string(file_item, "path")
            artifacts.append(
                ArtifactSpec(
                    component=name,
                    name=f"{repository}:{destination}",
                    version=revision,
                    filename=Path(destination).name,
                    url=_required_https(file_item, "url"),
                    license=license_value,
                    sha256=_required_string(file_item, "sha256"),
                    size=_required_positive_int(file_item, "size"),
                    destination=destination,
                )
            )
        destinations = [artifact.destination for artifact in artifacts]
        if len(destinations) != len(set(destinations)):
            raise ComponentInstallError("model catalog contains duplicate file paths")
        size = _required_positive_int(item, "size")
        if sum(artifact.size for artifact in artifacts) != size:
            raise ComponentInstallError("model file sizes differ from catalog total")
        models[name] = ModelSpec(
            name=name,
            repository=repository,
            revision=revision,
            license=license_value,
            directory_sha256=_required_string(item, "directory_sha256"),
            size=size,
            artifacts=tuple(artifacts),
        )
    if set(models) != set(MODEL_COMPONENT_NAMES):
        raise ComponentInstallError("model catalog must contain the four pinned models")
    return models


def _catalog_child(root: Path, value: str) -> Path:
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ComponentInstallError("catalog reference escapes catalog root")
    child = (root / relative).resolve(strict=True)
    if not child.is_relative_to(root):
        raise ComponentInstallError("catalog reference escapes catalog root")
    return child


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ComponentInstallError(f"{label} is missing or invalid") from error
    if not isinstance(value, dict):
        raise ComponentInstallError(f"{label} must contain a JSON object")
    return value


def _required_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ComponentInstallError(f"catalog {key} is missing")
    return value


def _required_https(data: dict[str, Any], key: str) -> str:
    value = _required_string(data, key)
    if urlparse(value).scheme != "https":
        raise ComponentInstallError(f"catalog {key} must use HTTPS")
    return value


def _required_positive_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ComponentInstallError(f"catalog {key} is invalid")
    return value


def _string_map(value: object, label: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ComponentInstallError(f"{label} are missing")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str) or not item:
            raise ComponentInstallError(f"{label} are invalid")
        result[key] = item
    return result


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
