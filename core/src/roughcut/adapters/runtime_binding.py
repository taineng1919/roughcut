"""Persistent, versioned selection of one verified Roughcut component runtime."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any, BinaryIO

from roughcut.adapters.component_environment import (
    ComponentManifest,
    ComponentRecord,
    current_architecture,
    current_platform,
    load_component_manifest,
    path_is_within,
)

RUNTIME_BINDING_FILENAME = "runtime.json"
RUNTIME_BINDING_SCHEMA_VERSION = 2
RUNTIME_COMPONENT_KEYS = ("asr", "vad", "punc", "campp")
RUNTIME_COMPONENT_NAMES = {
    "asr": "model_asr",
    "vad": "model_vad",
    "punc": "model_punc",
    "campp": "model_spk",
}
RUNTIME_SOURCES = {"external", "managed"}
RUNTIME_OWNERSHIP = {"external_read_only", "roughcut_managed"}
AUDALIGN_GROUP_NAME = "audalign_fingerprint"
AUDALIGN_UPSTREAM_COMMIT = "d5955ae8a85b1cd480dadd005c3f88986f4ebbef"
AUDALIGN_PROVIDER = "audalign"
BBC_AUDIO_OFFSET_FINDER_PROVIDER = "bbc_audio_offset_finder"
AUDALIGN_DISTRIBUTIONS = (
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
)
AUDALIGN_DISTRIBUTION_VERSIONS = {
    "audalign": "1.3.1",
    "contourpy": "1.3.3",
    "cycler": "0.12.1",
    "fonttools": "4.63.0",
    "kiwisolver": "1.5.0",
    "matplotlib": "3.8.2",
    "numpy": "1.26.4",
    "packaging": "26.2",
    "pillow": "12.3.0",
    "pydub": "0.25.1",
    "pyparsing": "3.3.2",
    "python-dateutil": "2.9.0.post0",
    "scipy": "1.12.0",
    "setuptools": "59.6.0",
    "six": "1.17.0",
    "tqdm": "4.66.2",
}
AUDALIGN_DISTRIBUTIONS_BY_PLATFORM = {
    "macos": AUDALIGN_DISTRIBUTIONS,
    "windows": (*AUDALIGN_DISTRIBUTIONS, "colorama"),
}
AUDALIGN_DISTRIBUTION_VERSIONS_BY_PLATFORM = {
    "macos": AUDALIGN_DISTRIBUTION_VERSIONS,
    "windows": {**AUDALIGN_DISTRIBUTION_VERSIONS, "colorama": "0.4.6"},
}
BBC_AUDIO_OFFSET_FINDER_VERSION = "0.5.5"
BBC_AUDIO_OFFSET_FINDER_GROUP_NAME = "bbc_audio_offset_finder"
BBC_DISTRIBUTIONS = (
    "audio-offset-finder",
    "audioread",
    "certifi",
    "cffi",
    "charset-normalizer",
    "contourpy",
    "cycler",
    "decorator",
    "fonttools",
    "idna",
    "joblib",
    "kiwisolver",
    "lazy-loader",
    "librosa",
    "llvmlite",
    "matplotlib",
    "msgpack",
    "narwhals",
    "numba",
    "numpy",
    "packaging",
    "pillow",
    "platformdirs",
    "pooch",
    "pycparser",
    "pyparsing",
    "python-dateutil",
    "requests",
    "scikit-learn",
    "scipy",
    "six",
    "soundfile",
    "soxr",
    "threadpoolctl",
    "typing-extensions",
    "urllib3",
)
BBC_DISTRIBUTION_VERSIONS = {
    "audio-offset-finder": "0.5.5",
    "audioread": "3.1.0",
    "certifi": "2026.7.22",
    "cffi": "2.1.1",
    "charset-normalizer": "3.5.1",
    "contourpy": "1.3.3",
    "cycler": "0.12.1",
    "decorator": "5.3.1",
    "fonttools": "4.63.0",
    "idna": "3.19",
    "joblib": "1.5.3",
    "kiwisolver": "1.5.0",
    "lazy-loader": "0.5",
    "librosa": "0.11.0",
    "llvmlite": "0.49.0",
    "matplotlib": "3.11.1",
    "msgpack": "1.2.1",
    "narwhals": "2.25.0",
    "numba": "0.67.0",
    "numpy": "1.26.4",
    "packaging": "26.3",
    "pillow": "12.3.0",
    "platformdirs": "4.11.3",
    "pooch": "1.9.0",
    "pycparser": "3.0",
    "pyparsing": "3.3.2",
    "python-dateutil": "2.9.0.post0",
    "requests": "2.34.2",
    "scikit-learn": "1.9.0",
    "scipy": "1.17.1",
    "six": "1.17.0",
    "soundfile": "0.14.0",
    "soxr": "1.1.0",
    "threadpoolctl": "3.6.0",
    "typing-extensions": "4.16.0",
    "urllib3": "2.7.0",
}
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_RUNTIME_PUBLISH_LOCK_FILENAME = f".{RUNTIME_BINDING_FILENAME}.lock"
_RUNTIME_THREAD_LOCKS_GUARD = threading.Lock()
_RUNTIME_THREAD_LOCKS: dict[str, threading.Lock] = {}
RUNTIME_PUBLISH_REASON_CODES = frozenset(
    {
        "runtime_publish_stale_plan",
        "runtime_publish_existing_binding_invalid",
        "runtime_publish_lock_failed",
        "runtime_publish_atomic_replace_failed",
        "runtime_publish_binding_validation_failed",
        "runtime_publish_failed",
    }
)


class RuntimeBindingError(RuntimeError):
    """Raised when persistent runtime state cannot be trusted."""

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


def audalign_distributions_for(platform: str) -> tuple[str, ...]:
    try:
        return AUDALIGN_DISTRIBUTIONS_BY_PLATFORM[platform]
    except KeyError as error:
        raise RuntimeBindingError(
            "Roughcut runtime binding alignment platform is unsupported"
        ) from error


def audalign_distribution_versions_for(platform: str) -> dict[str, str]:
    try:
        return AUDALIGN_DISTRIBUTION_VERSIONS_BY_PLATFORM[platform]
    except KeyError as error:
        raise RuntimeBindingError(
            "Roughcut runtime binding alignment platform is unsupported"
        ) from error


def bbc_audio_offset_finder_distributions_for(platform: str) -> tuple[str, ...]:
    if platform not in {"macos", "windows"}:
        raise RuntimeBindingError(
            "Roughcut runtime binding alignment platform is unsupported"
        )
    return BBC_DISTRIBUTIONS


def bbc_audio_offset_finder_distribution_versions_for(platform: str) -> dict[str, str]:
    if platform not in {"macos", "windows"}:
        raise RuntimeBindingError(
            "Roughcut runtime binding alignment platform is unsupported"
        )
    return BBC_DISTRIBUTION_VERSIONS


@dataclass(frozen=True)
class RuntimePython:
    source_type: str
    ownership: str
    interpreter: str
    versions: dict[str, str]
    receipt: dict[str, object]

    def validate(self) -> None:
        _validate_source_ownership(self.source_type, self.ownership)
        if set(self.versions) != {"funasr", "torch", "torchaudio"} or not all(
            isinstance(value, str) and value for value in self.versions.values()
        ):
            raise RuntimeBindingError("Roughcut runtime binding Python selection is incomplete")
        if not self.interpreter:
            raise RuntimeBindingError("Roughcut runtime binding Python interpreter is missing")
        if self.source_type == "external":
            required = {
                "interpreter",
                "python_version",
                "funasr",
                "torch",
                "torchaudio",
                "cuda_version",
                "cuda_available",
            }
            if (
                set(self.receipt) != required
                or self.receipt.get("interpreter") != self.interpreter
                or self.receipt.get("python_version") != "3.11"
                or self.receipt.get("cuda_version") is not None
                or self.receipt.get("cuda_available") is not False
                or any(
                    self.receipt.get(name) != version
                    for name, version in self.versions.items()
                )
            ):
                raise RuntimeBindingError(
                    "Roughcut runtime binding external Python receipt is invalid"
                )
        else:
            lock = self.receipt.get("lock_verification")
            lock_value = lock.get("value") if isinstance(lock, dict) else None
            if (
                self.receipt.get("root") != "venv"
                or self.receipt.get("device") != "cpu"
                or self.receipt.get("python_version") != "3.11"
                or not isinstance(lock, dict)
                or lock.get("algorithm") != "sha256"
                or not isinstance(lock_value, str)
                or SHA256_PATTERN.fullmatch(lock_value) is None
            ):
                raise RuntimeBindingError(
                    "Roughcut runtime binding managed Python receipt is invalid"
                )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> RuntimePython:
        return cls(
            source_type=_required_string(data, "source_type"),
            ownership=_required_string(data, "ownership"),
            interpreter=_required_string(data, "interpreter"),
            versions=_string_map(data.get("versions"), "Python versions"),
            receipt=_object(data.get("receipt"), "Python receipt"),
        )


@dataclass(frozen=True)
class RuntimeComponent:
    component: str
    source_type: str
    ownership: str
    path: str
    version: str
    origin: str
    license: str
    receipt: dict[str, object]

    def validate(self, key: str) -> None:
        if self.component != RUNTIME_COMPONENT_NAMES[key]:
            raise RuntimeBindingError(
                f"Roughcut runtime binding {key} component selection is invalid"
            )
        _validate_source_ownership(self.source_type, self.ownership)
        if not all((self.path, self.version, self.origin, self.license)):
            raise RuntimeBindingError(
                f"Roughcut runtime binding {key} component selection is incomplete"
            )
        algorithm = self.receipt.get("algorithm")
        value = self.receipt.get("value")
        if algorithm != "sha256" or not isinstance(value, str) or not SHA256_PATTERN.fullmatch(
            value
        ):
            raise RuntimeBindingError(
                f"Roughcut runtime binding {key} component receipt is invalid"
            )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> RuntimeComponent:
        return cls(
            component=_required_string(data, "component"),
            source_type=_required_string(data, "source_type"),
            ownership=_required_string(data, "ownership"),
            path=_required_string(data, "path"),
            version=_required_string(data, "version"),
            origin=_required_string(data, "origin"),
            license=_required_string(data, "license"),
            receipt=_object(data.get("receipt"), "component receipt"),
        )


@dataclass(frozen=True)
class RuntimeTool:
    command: str
    version: str
    source_type: str = "external"
    ownership: str = "external_read_only"

    def validate(self, name: str, *, platform: str) -> None:
        _validate_source_ownership(self.source_type, self.ownership)
        if self.source_type != "external":
            raise RuntimeBindingError(
                f"Roughcut runtime binding {name} source must remain external"
            )
        if not _is_absolute_path(self.command, platform=platform) or not self.version:
            raise RuntimeBindingError(
                f"Roughcut runtime binding {name} selection is incomplete"
            )

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> RuntimeTool:
        return cls(
            command=_required_string(data, "command"),
            version=_required_string(data, "version"),
            source_type=_required_string(data, "source_type"),
            ownership=_required_string(data, "ownership"),
        )


@dataclass(frozen=True)
class RuntimePlanEvidence:
    plan_hash: str
    catalog_version: str
    catalog_hash: str
    managed_root: str
    managed_manifest_sha256: str | None
    external_manifest_path: str | None
    external_manifest_sha256: str | None

    def validate(self, *, platform: str) -> None:
        if (
            SHA256_PATTERN.fullmatch(self.plan_hash) is None
            or SHA256_PATTERN.fullmatch(self.catalog_hash) is None
            or not self.catalog_version
        ):
            raise RuntimeBindingError("Roughcut runtime binding plan evidence is invalid")
        if not _is_absolute_path(self.managed_root, platform=platform):
            raise RuntimeBindingError(
                "Roughcut runtime binding managed root must be absolute"
            )
        _validate_optional_digest(
            self.managed_manifest_sha256, "managed manifest receipt"
        )
        _validate_optional_digest(
            self.external_manifest_sha256, "external manifest receipt"
        )
        if (self.external_manifest_path is None) != (
            self.external_manifest_sha256 is None
        ):
            raise RuntimeBindingError(
                "Roughcut runtime binding external manifest evidence is incomplete"
            )
        if self.external_manifest_path is not None and not _is_absolute_path(
            self.external_manifest_path, platform=platform
        ):
            raise RuntimeBindingError(
                "Roughcut runtime binding external manifest path must be absolute"
            )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> RuntimePlanEvidence:
        return cls(
            plan_hash=_required_string(data, "plan_hash"),
            catalog_version=_required_string(data, "catalog_version"),
            catalog_hash=_required_string(data, "catalog_hash"),
            managed_root=_required_string(data, "managed_root"),
            managed_manifest_sha256=_optional_string(
                data, "managed_manifest_sha256"
            ),
            external_manifest_path=_optional_string(data, "external_manifest_path"),
            external_manifest_sha256=_optional_string(
                data, "external_manifest_sha256"
            ),
        )


@dataclass(frozen=True)
class RuntimeAlignmentPython:
    source_type: str
    ownership: str
    interpreter: str
    python_version: str
    distributions: tuple[dict[str, str], ...]
    dependency_lock_receipt: dict[str, object]
    license_notice_receipt: dict[str, object]
    component_manifest_receipt: dict[str, object]
    provider: str
    provider_version: str
    upstream_commit: str | None = None

    def validate(self, *, platform: str = "macos") -> None:
        _validate_source_ownership(self.source_type, self.ownership)
        if (
            self.source_type != "managed"
            or self.ownership != "roughcut_managed"
            or self.python_version != "3.11"
            or not self.interpreter
        ):
            raise RuntimeBindingError(
                "Roughcut runtime binding alignment Python selection is invalid"
            )
        if self.provider == AUDALIGN_PROVIDER:
            self._validate_audalign(platform=platform)
        elif self.provider == BBC_AUDIO_OFFSET_FINDER_PROVIDER:
            self._validate_bbc(platform=platform)
        else:
            # a provider without a frozen managed contract can never be bound;
            # an Audalign installation is never an equivalent substitute
            raise RuntimeBindingError(
                "Roughcut runtime binding alignment provider is unsupported"
            )

    def _validate_distribution_closure(
        self,
        *,
        expected_distributions: tuple[str, ...],
        expected_versions: dict[str, str],
    ) -> None:
        names = tuple(
            item.get("name") if isinstance(item, dict) else None
            for item in self.distributions
        )
        if (
            names != expected_distributions
            or len(self.distributions) != len({name for name in names if name})
        ):
            raise RuntimeBindingError(
                "Roughcut runtime binding alignment distributions are invalid"
            )
        # every distribution must carry its exact frozen version; a loose or
        # missing version is rejected, not accepted
        for item in self.distributions:
            name = item.get("name")
            version = item.get("version")
            if (
                not isinstance(name, str)
                or not isinstance(version, str)
                or expected_versions.get(name) != version
            ):
                raise RuntimeBindingError(
                    "Roughcut runtime binding alignment distributions are invalid"
                )
        self._validate_receipt_digests()

    def _validate_receipt_digests(self) -> None:
        for receipt in (
            self.dependency_lock_receipt,
            self.license_notice_receipt,
            self.component_manifest_receipt,
        ):
            algorithm = receipt.get("algorithm")
            value = receipt.get("value")
            if (
                algorithm != "sha256"
                or not isinstance(value, str)
                or SHA256_PATTERN.fullmatch(value) is None
            ):
                raise RuntimeBindingError(
                    "Roughcut runtime binding alignment receipt is invalid"
                )

    def _validate_audalign(self, *, platform: str) -> None:
        if (
            self.provider_version != "1.3.1"
            or self.upstream_commit != AUDALIGN_UPSTREAM_COMMIT
        ):
            raise RuntimeBindingError(
                "Roughcut runtime binding alignment Python selection is invalid"
            )
        self._validate_distribution_closure(
            expected_distributions=audalign_distributions_for(platform),
            expected_versions=audalign_distribution_versions_for(platform),
        )

    def _validate_bbc(self, *, platform: str) -> None:
        if (
            self.provider_version != BBC_AUDIO_OFFSET_FINDER_VERSION
            or self.upstream_commit is not None
        ):
            raise RuntimeBindingError(
                "Roughcut runtime binding alignment Python selection is invalid"
            )
        self._validate_distribution_closure(
            expected_distributions=bbc_audio_offset_finder_distributions_for(platform),
            expected_versions=bbc_audio_offset_finder_distribution_versions_for(
                platform
            ),
        )

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "source_type": self.source_type,
            "ownership": self.ownership,
            "interpreter": self.interpreter,
            "python_version": self.python_version,
        }
        if self.provider == AUDALIGN_PROVIDER:
            # The historical schema-2 encoding stays canonical for Audalign
            # bindings so published runtime.json bytes and legacy profile-2
            # input-hash preimages remain stable; from_dict normalizes it.
            result["audalign_version"] = self.provider_version
            if self.upstream_commit is None:
                raise RuntimeBindingError(
                    "Roughcut runtime binding alignment upstream commit is missing"
                )
            result["audalign_upstream_commit"] = self.upstream_commit
        else:
            result["provider"] = self.provider
            result["provider_version"] = self.provider_version
            if self.upstream_commit is not None:
                result["upstream_commit"] = self.upstream_commit
        result["distributions"] = [dict(item) for item in self.distributions]
        result["dependency_lock_receipt"] = dict(self.dependency_lock_receipt)
        result["license_notice_receipt"] = dict(self.license_notice_receipt)
        result["component_manifest_receipt"] = dict(self.component_manifest_receipt)
        return result

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> RuntimeAlignmentPython:
        distributions = data.get("distributions")
        if not isinstance(distributions, list):
            raise RuntimeBindingError(
                "Roughcut runtime binding alignment distributions are missing"
            )
        parsed: list[dict[str, str]] = []
        for item in distributions:
            if not isinstance(item, dict) or set(item) != {"name", "version"}:
                raise RuntimeBindingError(
                    "Roughcut runtime binding alignment distributions are invalid"
                )
            name = item.get("name")
            version = item.get("version")
            if (
                not isinstance(name, str)
                or not name
                or not isinstance(version, str)
                or not version
            ):
                raise RuntimeBindingError(
                    "Roughcut runtime binding alignment distributions are invalid"
                )
            parsed.append({"name": name, "version": version})
        # encoding identity is decided by key presence, never by values: any
        # key of one family mixed into the other encoding is ambiguous and
        # rejected before any field is interpreted
        has_legacy_identity = bool(
            {"audalign_version", "audalign_upstream_commit"} & data.keys()
        )
        has_explicit_provider_identity = bool(
            {"provider", "provider_version", "upstream_commit"} & data.keys()
        )
        if has_legacy_identity and has_explicit_provider_identity:
            raise RuntimeBindingError(
                "Roughcut runtime binding alignment provider encoding is ambiguous"
            )
        if not has_legacy_identity and not has_explicit_provider_identity:
            raise RuntimeBindingError(
                "Roughcut runtime binding alignment provider is missing"
            )
        if has_legacy_identity:
            # historical schema-2 bindings carry only the Audalign identity
            return cls(
                source_type=_required_string(data, "source_type"),
                ownership=_required_string(data, "ownership"),
                interpreter=_required_string(data, "interpreter"),
                python_version=_required_string(data, "python_version"),
                distributions=tuple(parsed),
                dependency_lock_receipt=_object(
                    data.get("dependency_lock_receipt"),
                    "alignment dependency lock receipt",
                ),
                license_notice_receipt=_object(
                    data.get("license_notice_receipt"),
                    "alignment license notice receipt",
                ),
                component_manifest_receipt=_object(
                    data.get("component_manifest_receipt"),
                    "alignment component manifest receipt",
                ),
                provider=AUDALIGN_PROVIDER,
                provider_version=_required_string(data, "audalign_version"),
                upstream_commit=_required_string(
                    data, "audalign_upstream_commit"
                ),
            )
        return cls(
            source_type=_required_string(data, "source_type"),
            ownership=_required_string(data, "ownership"),
            interpreter=_required_string(data, "interpreter"),
            python_version=_required_string(data, "python_version"),
            distributions=tuple(parsed),
            dependency_lock_receipt=_object(
                data.get("dependency_lock_receipt"),
                "alignment dependency lock receipt",
            ),
            license_notice_receipt=_object(
                data.get("license_notice_receipt"),
                "alignment license notice receipt",
            ),
            component_manifest_receipt=_object(
                data.get("component_manifest_receipt"),
                "alignment component manifest receipt",
            ),
            provider=_required_string(data, "provider"),
            provider_version=_required_string(data, "provider_version"),
            upstream_commit=_optional_string(data, "upstream_commit"),
        )


def alignment_binding_supports_provider(
    selection: RuntimeAlignmentPython | None,
    requested_provider: str,
) -> bool:
    """One provider-identity seam: a bound selection satisfies exactly its own
    provider; no binding and any cross-provider request are explicit misses."""
    return selection is not None and selection.provider == requested_provider


@dataclass(frozen=True)
class RuntimeBinding:
    install_root: str
    platform: str
    architecture: str
    profile: str
    verification_mode: str
    python: RuntimePython
    components: dict[str, RuntimeComponent]
    ffmpeg: RuntimeTool
    ffprobe: RuntimeTool
    evidence: RuntimePlanEvidence
    schema_version: int = RUNTIME_BINDING_SCHEMA_VERSION
    alignment_python: RuntimeAlignmentPython | None = None

    def validate(
        self,
        *,
        validate_target: bool = True,
        validate_filesystem: bool = True,
    ) -> None:
        if self.schema_version == 1:
            if self.alignment_python is not None:
                raise RuntimeBindingError(
                    "Roughcut runtime binding schema 1 cannot hold an alignment selection"
                )
        elif self.schema_version != RUNTIME_BINDING_SCHEMA_VERSION:
            raise RuntimeBindingError(
                "Roughcut runtime binding schema version is unsupported"
            )
        else:
            # schema 2 must carry a complete verified alignment selection
            if self.alignment_python is None:
                raise RuntimeBindingError(
                    "Roughcut runtime binding schema 2 requires an alignment selection"
                )
        if self.platform not in {"macos", "linux", "windows"} or not self.architecture:
            raise RuntimeBindingError("Roughcut runtime binding target is invalid")
        if not _is_absolute_path(self.install_root, platform=self.platform):
            raise RuntimeBindingError("Roughcut runtime binding install root must be absolute")
        if not self.profile or self.verification_mode != "full":
            raise RuntimeBindingError(
                "Roughcut runtime binding requires a fully verified profile"
            )
        if validate_target:
            if self.platform != current_platform():
                raise RuntimeBindingError(
                    "Roughcut runtime binding platform does not match this host"
                )
            if self.architecture != current_architecture():
                raise RuntimeBindingError(
                    "Roughcut runtime binding architecture does not match this host"
                )
        self.python.validate()
        if set(self.components) != set(RUNTIME_COMPONENT_KEYS):
            raise RuntimeBindingError(
                "Roughcut runtime binding component selection is incomplete"
            )
        for key in RUNTIME_COMPONENT_KEYS:
            self.components[key].validate(key)
        self.ffmpeg.validate("FFmpeg", platform=self.platform)
        self.ffprobe.validate("ffprobe", platform=self.platform)
        self.evidence.validate(platform=self.platform)
        if self.alignment_python is not None:
            self.alignment_python.validate(platform=self.platform)
        if (
            self.python.source_type == "managed"
            or any(
                component.source_type == "managed"
                for component in self.components.values()
            )
        ) and self.evidence.managed_manifest_sha256 is None:
            raise RuntimeBindingError(
                "Roughcut runtime binding managed manifest evidence is incomplete"
            )
        if any(
            component.source_type == "external"
            for component in self.components.values()
        ) and self.evidence.external_manifest_path is None:
            raise RuntimeBindingError(
                "Roughcut runtime binding external manifest evidence is incomplete"
            )
        self._validate_containment()
        if validate_filesystem:
            self._validate_filesystem()

    def _validate_containment(self) -> None:
        managed_root = self.evidence.managed_root
        path_platform = "windows" if self.platform == "windows" else "posix"
        managed_paths = [
            self.python.interpreter
            if self.python.source_type == "managed"
            else None,
            *(
                component.path
                for component in self.components.values()
                if component.source_type == "managed"
            ),
            self.alignment_python.interpreter
            if self.alignment_python is not None
            else None,
        ]
        if any(
            path is not None
            and not path_is_within(path, managed_root, platform=path_platform)
            for path in managed_paths
        ):
            raise RuntimeBindingError(
                "Roughcut runtime binding managed path escapes managed root"
            )

    def _validate_filesystem(self) -> None:
        selected_paths = [
            Path(self.python.interpreter),
            *(Path(component.path) for component in self.components.values()),
            Path(self.ffmpeg.command),
            Path(self.ffprobe.command),
        ]
        if self.alignment_python is not None:
            selected_paths.append(Path(self.alignment_python.interpreter))
        for path in selected_paths:
            if not path.exists():
                raise RuntimeBindingError(
                    f"Roughcut runtime binding selected path is missing: {path}"
                )
        if any(
            Path(component.path).is_symlink()
            for component in self.components.values()
        ):
            raise RuntimeBindingError(
                "Roughcut runtime binding selected path is a symbolic link"
            )
        if not Path(self.python.interpreter).is_file():
            raise RuntimeBindingError(
                "Roughcut runtime binding Python interpreter is not a file"
            )
        if not os.access(self.python.interpreter, os.X_OK):
            raise RuntimeBindingError(
                "Roughcut runtime binding Python interpreter is not executable"
            )
        if self.alignment_python is not None:
            alignment_path = Path(self.alignment_python.interpreter)
            if alignment_path.is_symlink() or not alignment_path.is_file():
                raise RuntimeBindingError(
                    "Roughcut runtime binding alignment Python interpreter "
                    "is missing or a symbolic link"
                )
            if not os.access(alignment_path, os.X_OK):
                raise RuntimeBindingError(
                    "Roughcut runtime binding alignment Python interpreter "
                    "is not executable"
                )
            # the venv root is the interpreter's parent.parent (bin/python or
            # Scripts/python.exe), never a fixed parents[2] index
            venv_root = alignment_path.parent.parent
            if venv_root.is_symlink() or not venv_root.is_dir():
                raise RuntimeBindingError(
                    "Roughcut runtime binding alignment venv is unsafe"
                )
            self._validate_alignment_venv_tree(venv_root)
        if any(
            not Path(component.path).is_dir()
            for component in self.components.values()
        ):
            raise RuntimeBindingError(
                "Roughcut runtime binding model selection is not a directory"
            )
        for tool in (self.ffmpeg, self.ffprobe):
            path = Path(tool.command)
            if not path.is_file() or not os.access(path, os.X_OK):
                raise RuntimeBindingError(
                    "Roughcut runtime binding tool is not executable"
                )

    def _validate_alignment_venv_tree(self, venv_root: Path) -> None:
        """Recursively verify the alignment venv tree: no symlink, no
        non-regular file, and nothing escaping the venv root."""
        try:
            entries = list(venv_root.rglob("*"))
        except OSError as error:
            raise RuntimeBindingError(
                "Roughcut runtime binding alignment venv could not be scanned"
            ) from error
        for entry in entries:
            try:
                details = os.lstat(entry)
            except OSError as error:
                raise RuntimeBindingError(
                    "Roughcut runtime binding alignment venv is unreadable"
                ) from error
            if stat.S_ISLNK(details.st_mode):
                raise RuntimeBindingError(
                    "Roughcut runtime binding alignment venv contains a "
                    "symbolic link"
                )
            if not (stat.S_ISREG(details.st_mode) or stat.S_ISDIR(details.st_mode)):
                raise RuntimeBindingError(
                    "Roughcut runtime binding alignment venv contains a "
                    "non-regular entry"
                )

    @property
    def source(self) -> str:
        source_types = {
            self.python.source_type,
            *(component.source_type for component in self.components.values()),
        }
        return "persistent_external" if source_types == {"external"} else "persistent_managed"

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": self.schema_version,
            "install_root": self.install_root,
            "target": {
                "platform": self.platform,
                "architecture": self.architecture,
            },
            "profile": self.profile,
            "verification_mode": self.verification_mode,
            "python": self.python.to_dict(),
            "components": {
                key: self.components[key].to_dict() for key in RUNTIME_COMPONENT_KEYS
            },
            "ffmpeg": self.ffmpeg.to_dict(),
            "ffprobe": self.ffprobe.to_dict(),
            "evidence": self.evidence.to_dict(),
        }
        if self.alignment_python is not None:
            result["alignment_python"] = self.alignment_python.to_dict()
        return result

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> RuntimeBinding:
        schema_version = data.get("schema_version")
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise RuntimeBindingError(
                "Roughcut runtime binding schema version is invalid"
            )
        if schema_version not in {1, RUNTIME_BINDING_SCHEMA_VERSION}:
            raise RuntimeBindingError(
                "Roughcut runtime binding schema version is unsupported"
            )
        target = _object(data.get("target"), "target")
        python = _object(data.get("python"), "Python selection")
        components = _object(data.get("components"), "component selections")
        ffmpeg = _object(data.get("ffmpeg"), "FFmpeg selection")
        ffprobe = _object(data.get("ffprobe"), "ffprobe selection")
        evidence = _object(data.get("evidence"), "plan evidence")
        parsed_components: dict[str, RuntimeComponent] = {}
        for key, value in components.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                raise RuntimeBindingError(
                    "Roughcut runtime binding component selections are invalid"
                )
            parsed_components[key] = RuntimeComponent.from_dict(value)
        alignment_raw = data.get("alignment_python")
        if alignment_raw is not None and not isinstance(alignment_raw, dict):
            raise RuntimeBindingError(
                "Roughcut runtime binding alignment Python selection is invalid"
            )
        return cls(
            schema_version=schema_version,
            install_root=_required_string(data, "install_root"),
            platform=_required_string(target, "platform"),
            architecture=_required_string(target, "architecture"),
            profile=_required_string(data, "profile"),
            verification_mode=_required_string(data, "verification_mode"),
            python=RuntimePython.from_dict(python),
            components=parsed_components,
            ffmpeg=RuntimeTool.from_dict(ffmpeg),
            ffprobe=RuntimeTool.from_dict(ffprobe),
            evidence=RuntimePlanEvidence.from_dict(evidence),
            alignment_python=(
                RuntimeAlignmentPython.from_dict(alignment_raw)
                if isinstance(alignment_raw, dict)
                else None
            ),
        )


@dataclass(frozen=True)
class RuntimePublishResult:
    path: str
    published: bool
    reused: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ResolvedRuntimeTool:
    command: str
    source: str


@dataclass(frozen=True)
class ResolvedFunASRSelection:
    python_path: Path
    model_root: Path
    model_paths: dict[str, Path] | None
    speaker_model_path: Path | None
    ffmpeg_command: str
    source: str


def default_install_root(
    *,
    home: PurePath | None = None,
    platform: str | None = None,
) -> PurePath:
    selected_platform = platform or current_platform()
    if selected_platform == "windows":
        windows_home = (
            home
            if isinstance(home, PureWindowsPath)
            else PureWindowsPath(str(home or Path.home()))
        )
        return windows_home / ".roughcut"
    posix_home = Path(home) if home is not None else Path.home()
    return posix_home / ".roughcut"


def runtime_binding_path(
    *,
    install_root: PurePath | None = None,
    executable: PurePath | None = None,
    home: PurePath | None = None,
    platform: str | None = None,
) -> PurePath:
    selected_platform = platform or current_platform()
    if install_root is not None:
        return install_root / RUNTIME_BINDING_FILENAME
    selected_executable = executable
    if selected_executable is None:
        selected_executable = (
            PureWindowsPath(sys.executable)
            if selected_platform == "windows"
            else Path(sys.executable)
        )
    parents = selected_executable.parents
    if (
        len(parents) >= 3
        and parents[0].name.lower() in {"bin", "scripts"}
        and parents[1].name.lower() == "venv"
    ):
        return parents[2] / RUNTIME_BINDING_FILENAME
    return default_install_root(home=home, platform=selected_platform) / (
        RUNTIME_BINDING_FILENAME
    )


def configured_runtime_path(runtime_path: Path | None = None) -> Path:
    if runtime_path is not None:
        return runtime_path
    override = os.environ.get("ROUGHCUT_RUNTIME_BINDING")
    if override:
        selected = Path(override)
        if not selected.is_absolute():
            raise RuntimeBindingError(
                "Roughcut runtime binding process override must be absolute"
            )
        return selected
    return Path(runtime_binding_path())


def resolve_runtime_tool(
    name: str,
    *,
    explicit_command: str | None = None,
    runtime_path: Path | None = None,
) -> ResolvedRuntimeTool:
    if name not in {"ffmpeg", "ffprobe"}:
        raise RuntimeBindingError("Roughcut runtime tool name is unsupported")
    if explicit_command is not None:
        if not explicit_command:
            raise RuntimeBindingError(
                f"Roughcut {name} explicit override is empty"
            )
        return ResolvedRuntimeTool(explicit_command, "explicit_override")
    environment_name = (
        "ROUGHCUT_FFMPEG_COMMAND"
        if name == "ffmpeg"
        else "ROUGHCUT_FFPROBE_COMMAND"
    )
    process_override = os.environ.get(environment_name)
    if process_override:
        return ResolvedRuntimeTool(process_override, "explicit_override")
    binding = load_runtime_binding(configured_runtime_path(runtime_path))
    tool = binding.ffmpeg if name == "ffmpeg" else binding.ffprobe
    return ResolvedRuntimeTool(tool.command, "persistent_external")


def resolve_funasr_selection(
    *,
    explicit_python: Path | None = None,
    explicit_model_root: Path | None = None,
    explicit_speaker_model: Path | None = None,
    runtime_path: Path | None = None,
) -> ResolvedFunASRSelection:
    process_python = os.environ.get("ROUGHCUT_FUNASR_PYTHON")
    process_models = os.environ.get("ROUGHCUT_FUNASR_MODEL_ROOT")
    selected_python = explicit_python or (
        Path(process_python) if process_python else None
    )
    selected_model_root = explicit_model_root or (
        Path(process_models) if process_models else None
    )
    override_used = any(
        value is not None
        for value in (
            explicit_python,
            explicit_model_root,
            explicit_speaker_model,
            process_python,
            process_models,
        )
    )
    binding: RuntimeBinding | None
    try:
        binding = load_runtime_binding(configured_runtime_path(runtime_path))
    except RuntimeBindingError as error:
        if str(error) != "Roughcut runtime binding 未配置":
            raise
        binding = None
    if selected_python is None:
        if binding is None:
            raise RuntimeBindingError("Roughcut runtime binding 未配置")
        selected_python = Path(binding.python.interpreter)
    if selected_model_root is None and binding is None:
        raise RuntimeBindingError("Roughcut runtime binding 未配置")
    if selected_model_root is not None:
        model_paths = None
        model_root = selected_model_root
        speaker_model = explicit_speaker_model
    else:
        assert binding is not None
        model_paths = {
            key: Path(component.path)
            for key, component in binding.components.items()
        }
        model_root = Path(binding.install_root) / "cache" / "modelscope"
        speaker_model = explicit_speaker_model or model_paths["campp"]
    ffmpeg = resolve_runtime_tool("ffmpeg", runtime_path=runtime_path)
    source = "explicit_override" if override_used else binding.source  # type: ignore[union-attr]
    return ResolvedFunASRSelection(
        python_path=selected_python,
        model_root=model_root,
        model_paths=model_paths,
        speaker_model_path=speaker_model,
        ffmpeg_command=ffmpeg.command,
        source=source,
    )


def load_runtime_binding(
    path: Path,
    *,
    validate_target: bool = True,
    validate_filesystem: bool = True,
) -> RuntimeBinding:
    if path.is_symlink():
        raise RuntimeBindingError(
            "Roughcut runtime binding path must not be a symbolic link"
        )
    if path.parent.is_symlink():
        raise RuntimeBindingError(
            "Roughcut runtime binding install root must not be a symbolic link"
        )
    try:
        payload = path.read_bytes()
    except FileNotFoundError as error:
        raise RuntimeBindingError("Roughcut runtime binding 未配置") from error
    except OSError as error:
        raise RuntimeBindingError("Roughcut runtime binding JSON 已损坏") from error
    return _binding_from_payload(
        path,
        payload,
        validate_target=validate_target,
        validate_filesystem=validate_filesystem,
    )


def _binding_from_payload(
    path: Path,
    payload: bytes,
    *,
    validate_target: bool = True,
    validate_filesystem: bool = True,
) -> RuntimeBinding:
    if path.is_symlink():
        raise RuntimeBindingError(
            "Roughcut runtime binding path must not be a symbolic link"
        )
    if path.parent.is_symlink():
        raise RuntimeBindingError(
            "Roughcut runtime binding install root must not be a symbolic link"
        )
    try:
        raw: Any = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeBindingError("Roughcut runtime binding JSON 已损坏") from error
    if not isinstance(raw, dict):
        raise RuntimeBindingError(
            "Roughcut runtime binding must contain a JSON object"
        )
    binding = RuntimeBinding.from_dict(raw)
    if Path(binding.install_root).resolve(strict=False) != path.parent.resolve(
        strict=False
    ):
        raise RuntimeBindingError(
            "Roughcut runtime binding install root does not match its location"
        )
    binding.validate(
        validate_target=validate_target,
        validate_filesystem=validate_filesystem,
    )
    return binding


def publish_runtime_binding(
    path: Path,
    binding: RuntimeBinding,
    *,
    expected_previous_state: Mapping[str, object] | None = None,
) -> RuntimePublishResult:
    expected = Path(binding.install_root) / RUNTIME_BINDING_FILENAME
    if path.resolve(strict=False) != expected.resolve(strict=False):
        raise RuntimeBindingError(
            "Roughcut runtime binding target escapes its install root"
        )
    install_root = path.parent
    if install_root.is_symlink() or path.is_symlink():
        raise RuntimeBindingError(
            "Roughcut runtime binding target must not be a symbolic link",
            reason_code="runtime_publish_existing_binding_invalid",
        )
    install_root.mkdir(parents=True, exist_ok=True)
    with _runtime_publish_lock(install_root):
        if (
            expected_previous_state is not None
            and runtime_binding_status(path) != dict(expected_previous_state)
        ):
            raise RuntimeBindingError(
                "Roughcut bootstrap 拒绝发布 stale component plan",
                reason_code="runtime_publish_stale_plan",
            )
        return _publish_runtime_binding_locked(path, binding)


def _publish_runtime_binding_locked(
    path: Path,
    binding: RuntimeBinding,
) -> RuntimePublishResult:
    install_root = path.parent
    existing: RuntimeBinding | None = None
    existing_payload: bytes | None = None
    if os.path.lexists(path):
        if not path.is_file():
            raise RuntimeBindingError(
                "Roughcut runtime binding publisher 拒绝覆盖无效既有绑定",
                reason_code="runtime_publish_existing_binding_invalid",
            )
        try:
            existing_payload = path.read_bytes()
            existing = _binding_from_payload(path, existing_payload)
        except (OSError, RuntimeBindingError) as error:
            raise RuntimeBindingError(
                "Roughcut runtime binding publisher 拒绝覆盖无效既有绑定",
                reason_code="runtime_publish_existing_binding_invalid",
            ) from error
    try:
        binding.validate()
    except RuntimeBindingError as error:
        raise RuntimeBindingError(
            "Roughcut runtime binding candidate validation failed",
            reason_code="runtime_publish_binding_validation_failed",
        ) from error
    payload = (
        json.dumps(
            binding.to_dict(), ensure_ascii=False, indent=2, sort_keys=True
        )
        + "\n"
    ).encode("utf-8")
    if existing is not None:
        if existing_payload == payload:
            return RuntimePublishResult(str(path), published=False, reused=True)
        if replace(
            binding,
            evidence=replace(
                binding.evidence,
                plan_hash=existing.evidence.plan_hash,
            ),
        ) == existing:
            return RuntimePublishResult(str(path), published=False, reused=True)
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=install_root, prefix=f".{path.name}.", suffix=".tmp"
        )
    except OSError as error:
        raise RuntimeBindingError(
            "Roughcut runtime binding temporary file could not be created",
            reason_code="runtime_publish_atomic_replace_failed",
        ) from error
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException as error:
        temporary.unlink(missing_ok=True)
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise RuntimeBindingError(
            "Roughcut runtime binding 原子发布失败",
            reason_code="runtime_publish_atomic_replace_failed",
        ) from error
    try:
        _sync_directory(install_root)
    except OSError:
        pass
    return RuntimePublishResult(str(path), published=True, reused=False)


@contextmanager
def _runtime_publish_lock(install_root: Path) -> Iterator[None]:
    key = os.path.normcase(str(install_root.resolve(strict=False)))
    with _RUNTIME_THREAD_LOCKS_GUARD:
        thread_lock = _RUNTIME_THREAD_LOCKS.setdefault(key, threading.Lock())
    with thread_lock:
        lock_path = install_root / _RUNTIME_PUBLISH_LOCK_FILENAME
        try:
            with _open_runtime_lock_file(lock_path) as lock_file:
                _acquire_runtime_file_lock(lock_file)
                try:
                    yield
                finally:
                    _release_runtime_file_lock(lock_file)
        except (KeyboardInterrupt, SystemExit):
            raise
        except OSError as error:
            raise RuntimeBindingError(
                "Roughcut runtime binding publisher 无法取得发布锁",
                reason_code="runtime_publish_lock_failed",
            ) from error


@contextmanager
def _open_runtime_lock_file(lock_path: Path) -> Iterator[BinaryIO]:
    descriptor: int | None = None
    try:
        if _runtime_lock_uses_windows() and os.path.lexists(lock_path):
            _validate_runtime_lock_stat(os.lstat(lock_path))
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        if not _runtime_lock_uses_windows():
            flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        opened_stat = os.fstat(descriptor)
        _validate_runtime_lock_stat(opened_stat)
        path_stat = os.stat(lock_path, follow_symlinks=False)
        _validate_runtime_lock_stat(path_stat)
        if (
            opened_stat.st_dev != path_stat.st_dev
            or opened_stat.st_ino != path_stat.st_ino
        ):
            raise RuntimeBindingError(
                "Roughcut runtime binding publisher 拒绝无效发布锁",
                reason_code="runtime_publish_lock_failed",
            )
        lock_file = os.fdopen(descriptor, "r+b")
        descriptor = None
    except RuntimeBindingError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise RuntimeBindingError(
            "Roughcut runtime binding publisher 拒绝无效发布锁",
            reason_code="runtime_publish_lock_failed",
        ) from error
    with lock_file:
        yield lock_file


def _validate_runtime_lock_stat(lock_stat: os.stat_result) -> None:
    if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1:
        raise RuntimeBindingError(
            "Roughcut runtime binding publisher 拒绝无效发布锁",
            reason_code="runtime_publish_lock_failed",
        )


def _runtime_lock_uses_windows() -> bool:
    return os.name == "nt"


def _acquire_runtime_file_lock(lock_file: Any) -> None:
    if _runtime_lock_uses_windows():
        msvcrt: Any = importlib.import_module("msvcrt")

        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        while True:
            lock_file.seek(0)
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                time.sleep(0.01)
    else:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)


def _release_runtime_file_lock(lock_file: Any) -> None:
    if _runtime_lock_uses_windows():
        msvcrt: Any = importlib.import_module("msvcrt")

        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def runtime_binding_status(path: Path) -> dict[str, object]:
    if not os.path.lexists(path):
        return {
            "path": str(path),
            "configured": False,
            "status": "unconfigured",
        }
    payload: bytes | None = None
    if path.is_file() and not path.is_symlink():
        try:
            payload = path.read_bytes()
        except OSError:
            pass
    sha256 = hashlib.sha256(payload).hexdigest() if payload is not None else None
    try:
        if payload is None:
            load_runtime_binding(path)
        else:
            _binding_from_payload(path, payload)
    except RuntimeBindingError as error:
        result: dict[str, object] = {
            "path": str(path),
            "configured": False,
            "status": "invalid",
            "detail": str(error),
        }
        if sha256 is not None:
            result["sha256"] = sha256
        return result
    result = {
        "path": str(path),
        "configured": True,
        "status": "configured",
    }
    if sha256 is not None:
        result["sha256"] = sha256
    return result


def binding_from_install_plan(
    payload: Mapping[str, object],
    *,
    approved_plan_hash: str,
    install_root: Path,
    external_manifest_path: Path | None,
) -> RuntimeBinding:
    """Convert one fully verified, complete plan into the persistent selection."""

    if payload.get("verification_mode") != "full":
        raise RuntimeBindingError(
            "Roughcut bootstrap 仅在 full verification 后发布 runtime binding"
        )
    missing = payload.get("missing_managed_groups")
    actions = payload.get("user_actions")
    if missing != [] or actions != []:
        raise RuntimeBindingError(
            "Roughcut bootstrap 拒绝发布不完整 component plan"
        )
    target = _object(payload.get("target"), "plan target")
    components = _object(payload.get("components"), "plan components")
    state = _object(payload.get("state"), "plan state")
    managed_root = Path(_required_string(payload, "managed_root"))
    managed_manifest_path = managed_root / "component-manifest.json"
    managed_manifest = (
        load_component_manifest(managed_manifest_path)
        if managed_manifest_path.is_file()
        else None
    )
    external_manifest = (
        load_component_manifest(external_manifest_path)
        if external_manifest_path is not None
        else None
    )
    if external_manifest_path is not None and external_manifest_path.is_symlink():
        raise RuntimeBindingError(
            "Roughcut runtime binding external manifest must not be a symbolic link"
        )
    python_component = _object(components.get("funasr"), "FunASR component")
    python_source = _required_string(python_component, "selected_source")
    python_path = _required_string(python_component, "path")
    if python_source == "external":
        external_probe = _object(
            state.get("external_runtime_probe"), "external runtime probe"
        )
        python_receipt = {"interpreter": python_path, **external_probe}
    elif python_source == "managed":
        if managed_manifest is None or managed_manifest.python_runtime is None:
            raise RuntimeBindingError(
                "Roughcut runtime binding managed Python receipt is missing"
            )
        python_receipt = managed_manifest.python_runtime.to_dict()
    else:
        raise RuntimeBindingError(
            "Roughcut runtime binding Python source is invalid"
        )
    versions = (
        {
            name: _required_string(python_receipt, name)
            for name in ("funasr", "torch", "torchaudio")
        }
        if python_source == "external"
        else {
            name: _required_string(
                _object(components.get(name), f"{name} component"), "version"
            )
            for name in ("funasr", "torch", "torchaudio")
        }
    )
    selected_models: dict[str, RuntimeComponent] = {}
    for key, component_name in RUNTIME_COMPONENT_NAMES.items():
        diagnostic = _object(
            components.get(component_name), f"{component_name} component"
        )
        source_type = _required_string(diagnostic, "selected_source")
        manifest = external_manifest if source_type == "external" else managed_manifest
        record = _record_for_component(
            manifest, component_name, source_type=source_type
        )
        receipt: dict[str, object] = {}
        receipt.update(record.verification.to_dict())
        selected_models[key] = RuntimeComponent(
            component=component_name,
            source_type=source_type,
            ownership=_ownership(source_type),
            path=_required_string(diagnostic, "path"),
            version=record.version,
            origin=record.origin,
            license=record.license,
            receipt=receipt,
        )
    ffmpeg = _tool_from_plan(components, "ffmpeg")
    ffprobe = _tool_from_plan(components, "ffprobe")
    alignment_python = _alignment_python_from_plan(
        payload, managed_root, managed_manifest
    )
    binding = RuntimeBinding(
        install_root=str(install_root.resolve(strict=False)),
        platform=_required_string(target, "platform"),
        architecture=_required_string(target, "architecture"),
        profile=_required_string(payload, "profile"),
        verification_mode="full",
        python=RuntimePython(
            source_type=python_source,
            ownership=_ownership(python_source),
            interpreter=python_path,
            versions=versions,
            receipt=python_receipt,
        ),
        components=selected_models,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        evidence=RuntimePlanEvidence(
            plan_hash=approved_plan_hash,
            catalog_version=_required_string(payload, "catalog_version"),
            catalog_hash=_required_string(payload, "catalog_hash"),
            managed_root=str(managed_root),
            managed_manifest_sha256=_optional_digest_from_state(
                state, "managed_manifest_sha256"
            ),
            external_manifest_path=(
                str(external_manifest_path.resolve(strict=False))
                if external_manifest_path is not None
                else None
            ),
            external_manifest_sha256=_optional_digest_from_state(
                state, "external_manifest_sha256"
            ),
        ),
        # a plan without an audalign group produces a schema-1 binding; only a
        # fully verified alignment selection upgrades it to schema 2
        schema_version=(
            RUNTIME_BINDING_SCHEMA_VERSION
            if alignment_python is not None
            else 1
        ),
        alignment_python=alignment_python,
    )
    binding.validate()
    return binding


def _alignment_python_from_plan(
    payload: Mapping[str, object],
    managed_root: Path,
    managed_manifest: ComponentManifest | None,
) -> RuntimeAlignmentPython | None:
    """Build the schema-2 closed alignment selection from a fully verified plan."""
    group_providers = {
        AUDALIGN_GROUP_NAME: AUDALIGN_PROVIDER,
        BBC_AUDIO_OFFSET_FINDER_GROUP_NAME: BBC_AUDIO_OFFSET_FINDER_PROVIDER,
    }
    selected = {
        key: payload[key]
        for key in group_providers
        if payload.get(key) is not None
    }
    if not selected:
        return None
    if len(selected) > 1:
        raise RuntimeBindingError(
            "Roughcut runtime binding alignment plan is ambiguous"
        )
    group_name = next(iter(selected))
    provider = group_providers[group_name]
    group_raw = selected[group_name]
    if not isinstance(group_raw, dict):
        raise RuntimeBindingError(
            "Roughcut runtime binding alignment plan metadata is invalid"
        )
    if group_raw.get("available") is not True:
        raise RuntimeBindingError(
            "Roughcut runtime binding alignment selection is unavailable"
        )
    if managed_manifest is None or managed_manifest.managed_root is None:
        raise RuntimeBindingError(
            "Roughcut runtime binding alignment manifest receipt is missing"
        )
    if Path(managed_manifest.managed_root).resolve(strict=False) != managed_root.resolve(
        strict=False
    ):
        raise RuntimeBindingError(
            "Roughcut runtime binding alignment manifest belongs to another root"
        )
    manifest_records = [
        record
        for record in managed_manifest.components
        if record.name == _ALIGNMENT_MANAGED_RECORDS[provider]
    ]
    if len(manifest_records) != 1 or manifest_records[0].source_type != "managed":
        raise RuntimeBindingError(
            "Roughcut runtime binding alignment manifest receipt is missing"
        )
    manifest_record = manifest_records[0]
    manifest_receipt = manifest_record.verification
    distributions_raw = group_raw.get("distributions")
    if not isinstance(distributions_raw, list):
        raise RuntimeBindingError(
            "Roughcut runtime binding alignment distributions are invalid"
        )
    distributions: list[dict[str, str]] = []
    for item in distributions_raw:
        if (
            not isinstance(item, dict)
            or set(item) != {"name", "version"}
            or not isinstance(item.get("name"), str)
            or not isinstance(item.get("version"), str)
        ):
            raise RuntimeBindingError(
                "Roughcut runtime binding alignment distributions are invalid"
            )
        distributions.append(
            {"name": item["name"], "version": item["version"]}
        )
    lock_raw = group_raw.get("dependency_lock")
    if not isinstance(lock_raw, dict):
        raise RuntimeBindingError(
            "Roughcut runtime binding alignment dependency lock receipt is missing"
        )
    lock_sha256 = lock_raw.get("sha256")
    if not isinstance(lock_sha256, str) or SHA256_PATTERN.fullmatch(
        lock_sha256
    ) is None:
        raise RuntimeBindingError(
            "Roughcut runtime binding alignment dependency lock receipt is invalid"
        )
    license_sha256 = group_raw.get("license_notice_sha256")
    if not isinstance(license_sha256, str) or SHA256_PATTERN.fullmatch(
        license_sha256
    ) is None:
        raise RuntimeBindingError(
            "Roughcut runtime binding alignment license receipt is invalid"
        )
    interpreter = _managed_alignment_interpreter(managed_manifest, provider)
    return RuntimeAlignmentPython(
        source_type="managed",
        ownership="roughcut_managed",
        interpreter=str(interpreter),
        python_version="3.11",
        distributions=tuple(distributions),
        dependency_lock_receipt={"algorithm": "sha256", "value": lock_sha256},
        license_notice_receipt={"algorithm": "sha256", "value": license_sha256},
        component_manifest_receipt={
            "algorithm": manifest_receipt.algorithm,
            "value": manifest_receipt.value,
        },
        provider=provider,
        provider_version=_required_string(group_raw, "version"),
        upstream_commit=(
            _required_string(group_raw, "upstream_commit")
            if provider == AUDALIGN_PROVIDER
            else None
        ),
    )


_ALIGNMENT_MANAGED_DIRECTORIES = {
    AUDALIGN_PROVIDER: "audalign",
    BBC_AUDIO_OFFSET_FINDER_PROVIDER: BBC_AUDIO_OFFSET_FINDER_GROUP_NAME,
}
_ALIGNMENT_MANAGED_RECORDS = {
    AUDALIGN_PROVIDER: "audalign",
    BBC_AUDIO_OFFSET_FINDER_PROVIDER: BBC_AUDIO_OFFSET_FINDER_GROUP_NAME,
}


def _managed_alignment_interpreter(
    manifest: ComponentManifest,
    provider: str,
) -> Path:
    managed_dir = _ALIGNMENT_MANAGED_DIRECTORIES[provider]
    root = Path(manifest.managed_root or "")
    interpreter = root / managed_dir / (
        "venv/Scripts/python.exe"
        if manifest.platform == "windows"
        else "venv/bin/python"
    )
    if not path_is_within(
        str(interpreter),
        str(root),
        platform="windows" if manifest.platform == "windows" else "posix",
    ):
        raise RuntimeBindingError(
            "Roughcut runtime binding alignment interpreter escapes managed root"
        )
    return interpreter


def _validate_source_ownership(source_type: str, ownership: str) -> None:
    if source_type not in RUNTIME_SOURCES or ownership not in RUNTIME_OWNERSHIP:
        raise RuntimeBindingError("Roughcut runtime binding ownership is invalid")
    expected = (
        "external_read_only" if source_type == "external" else "roughcut_managed"
    )
    if ownership != expected:
        raise RuntimeBindingError(
            "Roughcut runtime binding source and ownership do not match"
        )


def _ownership(source_type: str) -> str:
    if source_type == "external":
        return "external_read_only"
    if source_type == "managed":
        return "roughcut_managed"
    raise RuntimeBindingError("Roughcut runtime binding source is invalid")


def _record_for_component(
    manifest: ComponentManifest | None,
    name: str,
    *,
    source_type: str,
) -> ComponentRecord:
    if manifest is None:
        raise RuntimeBindingError(
            f"Roughcut runtime binding {name} manifest receipt is missing"
        )
    matches = [record for record in manifest.components if record.name == name]
    if len(matches) != 1:
        raise RuntimeBindingError(
            f"Roughcut runtime binding {name} manifest receipt is missing"
        )
    if matches[0].source_type != source_type:
        raise RuntimeBindingError(
            f"Roughcut runtime binding {name} source receipt is inconsistent"
        )
    return matches[0]


def _tool_from_plan(
    components: Mapping[str, object], name: str
) -> RuntimeTool:
    diagnostic = _object(components.get(name), f"{name} component")
    if (
        diagnostic.get("status") != "available"
        or diagnostic.get("selected_source") != "external"
    ):
        raise RuntimeBindingError(
            f"Roughcut runtime binding {name} selection is unavailable"
        )
    return RuntimeTool(
        command=_required_string(diagnostic, "resolved_path"),
        version=_required_string(diagnostic, "version"),
    )


def _optional_digest_from_state(
    state: Mapping[str, object], key: str
) -> str | None:
    value = state.get(key)
    if value is not None and not isinstance(value, str):
        raise RuntimeBindingError(
            f"Roughcut runtime binding plan {key} is invalid"
        )
    return value


def _is_absolute_path(value: str, *, platform: str) -> bool:
    if platform == "windows":
        return PureWindowsPath(value).is_absolute()
    return PurePosixPath(value).is_absolute()


def _required_string(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeBindingError(f"Roughcut runtime binding {key} is missing")
    return value


def _optional_string(data: Mapping[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is not None and (not isinstance(value, str) or not value):
        raise RuntimeBindingError(f"Roughcut runtime binding {key} is invalid")
    return value


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RuntimeBindingError(f"Roughcut runtime binding {label} is missing")
    return dict(value)


def _string_map(value: object, label: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise RuntimeBindingError(f"Roughcut runtime binding {label} are missing")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str) or not item:
            raise RuntimeBindingError(f"Roughcut runtime binding {label} are invalid")
        result[key] = item
    return result


def _validate_optional_digest(value: str | None, label: str) -> None:
    if value is not None and SHA256_PATTERN.fullmatch(value) is None:
        raise RuntimeBindingError(f"Roughcut runtime binding {label} is invalid")


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
