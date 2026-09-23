"""Versioned, filesystem-safe media component manifests and local lifecycle."""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import platform as platform_module
import posixpath
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

COMPONENT_MANIFEST_FILENAME = "component-manifest.json"
COMPONENT_KINDS = {
    "funasr": "python_package",
    "torch": "python_package",
    "torchaudio": "python_package",
    "model_asr": "model",
    "model_vad": "model",
    "model_punc": "model",
    "model_spk": "model",
    "audalign": "python_package",
    "bbc_audio_offset_finder": "python_package",
    "ffmpeg": "executable",
    "ffprobe": "executable",
}
COMPONENT_NAMES = tuple(COMPONENT_KINDS)
LEGACY_COMPONENT_NAMES = tuple(
    name
    for name in COMPONENT_NAMES
    if name not in {"audalign", "bbc_audio_offset_finder"}
)
MODEL_PRIMARY_PAYLOADS = {"model_spk": "campplus_cn_common.bin"}
# Runtime-required files per model, derived from the frozen
# configuration.json file_path_metas plus the FunASR loader path
# (download_model_from_hub / CTTransformer jieba.load_userdict).
# Only files actually read at runtime or changing behavior are listed;
# README / example / fig / demo media are intentionally excluded.
MODEL_RUNTIME_FILES: dict[str, tuple[str, ...]] = {
    "model_asr": (
        "configuration.json",
        "config.yaml",
        "model.pt",
        "tokens.json",
        "seg_dict",
        "am.mvn",
    ),
    "model_vad": (
        "configuration.json",
        "config.yaml",
        "model.pt",
        "am.mvn",
    ),
    "model_punc": (
        "configuration.json",
        "config.yaml",
        "model.pt",
        "tokens.json",
        "jieba_usr_dict",
    ),
    "model_spk": (
        "configuration.json",
        "config.yaml",
        "campplus_cn_common.bin",
    ),
}
SUPPORTED_PLATFORMS = {"macos", "windows", "linux"}
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
PYTHON_COMPONENT_NAMES = ("funasr", "torch", "torchaudio")
PYTHON_RUNTIME_PROBE_TIMEOUT_SECONDS = 180
PROBE_FRAME_PREFIX = b"ROUGHCUT-PROBE/1 "
PYTHON_RUNTIME_PROBE_FIELDS = frozenset(
    {
        "python_version",
        "funasr",
        "torch",
        "torchaudio",
        "cuda_version",
        "cuda_available",
    }
)
VIRTUAL_ENVIRONMENT_VARIABLES = {
    "CONDA_DEFAULT_ENV",
    "CONDA_PREFIX",
    "VIRTUAL_ENV",
    "_OLD_VIRTUAL_PATH",
    "__PYVENV_LAUNCHER__",
}


class ComponentError(RuntimeError):
    """Raised when component state cannot be trusted or safely changed."""


@dataclass(frozen=True)
class ComponentVerification:
    algorithm: str
    value: str

    def __post_init__(self) -> None:
        if self.algorithm != "sha256" or SHA256_PATTERN.fullmatch(self.value) is None:
            raise ComponentError("component verification must contain a SHA-256 digest")

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> ComponentVerification:
        return cls(
            algorithm=_required_string(data, "algorithm"),
            value=_required_string(data, "value"),
        )


@dataclass(frozen=True)
class PythonRuntimeRecord:
    root: str
    interpreter: str
    dependency_lock: str
    lock_origin: str
    lock_verification: ComponentVerification
    device: str
    python_version: str = "3.11"

    def __post_init__(self) -> None:
        for path in (self.root, self.interpreter, self.dependency_lock):
            if not _is_safe_relative_path(path):
                raise ComponentError("managed Python runtime path escapes managed root")
        if self.root != "venv":
            raise ComponentError("managed Python runtime root must be venv")
        if not self.lock_origin:
            raise ComponentError("managed Python runtime lock origin is missing")
        if self.device != "cpu":
            raise ComponentError("managed Python runtime must be CPU-only")
        if self.python_version != "3.11":
            raise ComponentError("managed Python runtime must use Python 3.11")
        interpreter = PurePosixPath(self.interpreter)
        lock = PurePosixPath(self.dependency_lock)
        if interpreter.parts[0] != self.root:
            raise ComponentError("managed Python interpreter escapes runtime root")
        if len(lock.parts) != 2 or lock.parts[0] != "locks":
            raise ComponentError("managed Python dependency lock path is not canonical")

    def to_dict(self) -> dict[str, object]:
        return {
            "root": self.root,
            "interpreter": self.interpreter,
            "dependency_lock": self.dependency_lock,
            "lock_origin": self.lock_origin,
            "lock_verification": self.lock_verification.to_dict(),
            "device": self.device,
            "python_version": self.python_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> PythonRuntimeRecord:
        verification = data.get("lock_verification")
        if not isinstance(verification, dict):
            raise ComponentError("managed Python runtime lock verification is missing")
        python_version = data.get("python_version", "3.11")
        if not isinstance(python_version, str):
            raise ComponentError("managed Python runtime Python version is invalid")
        return cls(
            root=_required_string(data, "root"),
            interpreter=_required_string(data, "interpreter"),
            dependency_lock=_required_string(data, "dependency_lock"),
            lock_origin=_required_string(data, "lock_origin"),
            lock_verification=ComponentVerification.from_dict(verification),
            device=_required_string(data, "device"),
            python_version=python_version,
        )


@dataclass(frozen=True)
class ComponentRecord:
    name: str
    kind: str
    source_type: str
    origin: str
    version: str
    path: str
    platform: str
    architecture: str
    license: str
    verification: ComponentVerification

    def __post_init__(self) -> None:
        expected_kind = COMPONENT_KINDS.get(self.name)
        if expected_kind is None or self.kind != expected_kind:
            raise ComponentError("component name and kind are invalid")
        if self.source_type not in {"external", "managed"}:
            raise ComponentError("component source_type must be external or managed")
        if self.platform not in SUPPORTED_PLATFORMS:
            raise ComponentError("component platform is unsupported")
        for value in (
            self.origin,
            self.version,
            self.path,
            self.architecture,
            self.license,
        ):
            if not value:
                raise ComponentError("component fields must not be empty")
        if self.source_type == "external" and not _is_absolute_path(
            self.path, platform=self.platform
        ):
            raise ComponentError("external component path must be absolute")
        if self.source_type == "managed":
            if not _is_safe_relative_path(self.path):
                raise ComponentError("managed component path escapes managed root")
            if not _is_canonical_managed_path(self.name, self.kind, self.path):
                raise ComponentError("managed component path is not canonical")

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind,
            "source_type": self.source_type,
            "origin": self.origin,
            "version": self.version,
            "path": self.path,
            "platform": self.platform,
            "architecture": self.architecture,
            "license": self.license,
            "verification": self.verification.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> ComponentRecord:
        verification = data.get("verification")
        if not isinstance(verification, dict):
            raise ComponentError("component verification is missing")
        return cls(
            name=_required_string(data, "name"),
            kind=_required_string(data, "kind"),
            source_type=_required_string(data, "source_type"),
            origin=_required_string(data, "origin"),
            version=_required_string(data, "version"),
            path=_required_string(data, "path"),
            platform=_required_string(data, "platform"),
            architecture=_required_string(data, "architecture"),
            license=_required_string(data, "license"),
            verification=ComponentVerification.from_dict(verification),
        )


@dataclass(frozen=True)
class ComponentManifest:
    components: tuple[ComponentRecord, ...]
    platform: str
    architecture: str
    managed_root: str | None = None
    schema_version: int = 1
    python_runtime: PythonRuntimeRecord | None = None

    def __post_init__(self) -> None:
        if self.schema_version not in {1, 2}:
            raise ComponentError("unsupported component manifest schema version")
        if self.platform not in SUPPORTED_PLATFORMS or not self.architecture:
            raise ComponentError("component manifest target is invalid")
        names = [component.name for component in self.components]
        if len(names) != len(set(names)):
            raise ComponentError("component manifest contains duplicate names")
        if any(
            component.platform != self.platform
            or component.architecture != self.architecture
            for component in self.components
        ):
            raise ComponentError("component target differs from manifest target")
        has_managed = any(component.source_type == "managed" for component in self.components)
        if (has_managed or self.python_runtime is not None) and (
            self.managed_root is None
            or not _is_absolute_path(self.managed_root, platform=self.platform)
        ):
            raise ComponentError("managed manifest requires an absolute managed root")
        if self.schema_version == 1 and self.python_runtime is not None:
            raise ComponentError("component manifest schema 1 cannot describe a Python runtime")
        if self.python_runtime is not None:
            expected_interpreter = (
                "venv/Scripts/python.exe"
                if self.platform == "windows"
                else "venv/bin/python"
            )
            if self.python_runtime.interpreter != expected_interpreter:
                raise ComponentError("managed Python interpreter path does not match platform")

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": self.schema_version,
            "platform": self.platform,
            "architecture": self.architecture,
            "managed_root": self.managed_root,
            "components": [component.to_dict() for component in self.components],
        }
        if self.python_runtime is not None:
            result["python_runtime"] = self.python_runtime.to_dict()
        return result

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> ComponentManifest:
        components = data.get("components")
        if not isinstance(components, list):
            raise ComponentError("component manifest components are missing")
        records: list[ComponentRecord] = []
        for component in components:
            if not isinstance(component, dict):
                raise ComponentError("component manifest record is invalid")
            records.append(ComponentRecord.from_dict(component))
        managed_root = data.get("managed_root")
        if managed_root is not None and not isinstance(managed_root, str):
            raise ComponentError("component manifest managed_root is invalid")
        schema_version = data.get("schema_version")
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise ComponentError("component manifest schema version is invalid")
        python_runtime = data.get("python_runtime")
        if python_runtime is not None and not isinstance(python_runtime, dict):
            raise ComponentError("component manifest Python runtime is invalid")
        return cls(
            components=tuple(records),
            platform=_required_string(data, "platform"),
            architecture=_required_string(data, "architecture"),
            managed_root=managed_root,
            schema_version=schema_version,
            python_runtime=(
                PythonRuntimeRecord.from_dict(python_runtime)
                if isinstance(python_runtime, dict)
                else None
            ),
        )


@dataclass(frozen=True)
class ComponentAttempt:
    source_type: str
    status: str
    detail: str | None

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


@dataclass(frozen=True)
class ComponentDiagnostic:
    name: str
    status: str
    selected_source: str | None
    version: str | None
    path: str | None
    compatible: bool
    action: str
    attempts: tuple[ComponentAttempt, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "selected_source": self.selected_source,
            "version": self.version,
            "path": self.path,
            "compatible": self.compatible,
            "action": self.action,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
        }


@dataclass(frozen=True)
class ComponentResolution:
    components: dict[str, ComponentDiagnostic]
    install_required: tuple[str, ...]
    verification_mode: str = "quick"
    schema_version: int = 2

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "verification_mode": self.verification_mode,
            "components": {
                name: diagnostic.to_dict()
                for name, diagnostic in self.components.items()
            },
            "install_required": list(self.install_required),
        }


@dataclass(frozen=True)
class ComponentInstallResult:
    installed: bool
    reused: bool
    install_required: tuple[str, ...]
    manifest: ComponentManifest

    def to_dict(self) -> dict[str, object]:
        return {
            "installed": self.installed,
            "reused": self.reused,
            "install_required": list(self.install_required),
            "manifest": self.manifest.to_dict(),
        }


@dataclass(frozen=True)
class ComponentUninstallResult:
    uninstalled: bool
    removed_components: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "uninstalled": self.uninstalled,
            "removed_components": list(self.removed_components),
        }


def current_platform() -> str:
    if os.name == "nt":
        return "windows"
    if platform_module.system() == "Darwin":
        return "macos"
    return "linux"


def current_architecture() -> str:
    value = platform_module.machine().lower()
    return {"aarch64": "arm64", "amd64": "x86_64"}.get(value, value)


def probe_python_runtime(interpreter: Path) -> dict[str, object]:
    """Run the shared byte-framed FunASR/Torch/TorchAudio runtime probe."""

    selected = Path(os.path.abspath(interpreter))
    if not selected.is_file() or not os.access(selected, os.X_OK):
        raise ComponentError("Python runtime probe interpreter is not executable")
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("PYTHON") and name not in VIRTUAL_ENVIRONMENT_VARIABLES
    }
    probe = (
        "import importlib.metadata as m,json,sys;"
        "import funasr,torch,torchaudio;"
        "payload={'python_version':f'{sys.version_info.major}.{sys.version_info.minor}',"
        "'funasr':m.version('funasr'),'torch':m.version('torch'),"
        "'torchaudio':m.version('torchaudio'),'cuda_version':torch.version.cuda,"
        "'cuda_available':torch.cuda.is_available()};"
        "sys.stdout.buffer.write(b'ROUGHCUT-PROBE/1 '+"
        "json.dumps(payload,ensure_ascii=False,separators=(',',':'),sort_keys=True).encode('utf-8')+b'\\n')"
    )
    try:
        result = subprocess.run(
            [str(selected), "-I", "-c", probe],
            check=False,
            capture_output=True,
            timeout=PYTHON_RUNTIME_PROBE_TIMEOUT_SECONDS,
            env=environment,
        )
    except subprocess.TimeoutExpired as error:
        raise ComponentError("Python runtime probe timed out") from error
    except OSError as error:
        raise ComponentError("Python runtime probe failed") from error
    if result.returncode != 0:
        raise ComponentError("Python runtime probe failed with a non-zero exit")
    payload = _parse_probe_frame(result.stdout, PYTHON_RUNTIME_PROBE_FIELDS)
    required_strings = ("python_version", "funasr", "torch", "torchaudio")
    if any(not isinstance(payload.get(name), str) or not payload[name] for name in required_strings):
        raise ComponentError("Python runtime probe has invalid version fields")
    if payload["cuda_version"] is not None and not isinstance(payload["cuda_version"], str):
        raise ComponentError("Python runtime probe has an invalid CUDA version")
    if not isinstance(payload["cuda_available"], bool):
        raise ComponentError("Python runtime probe has an invalid CUDA availability")
    return payload


def _parse_probe_frame(stdout: object, fields: frozenset[str]) -> dict[str, object]:
    if not isinstance(stdout, (bytes, bytearray)):
        raise ComponentError("Python runtime probe did not return bytes")
    candidate_lines = []
    for line in bytes(stdout).splitlines(keepends=True):
        if not line.startswith(PROBE_FRAME_PREFIX):
            continue
        candidate_lines.append(line)
    if len(candidate_lines) != 1:
        raise ComponentError("Python runtime probe frame count is invalid")
    line = candidate_lines[0]
    if not line.endswith(b"\n") or line.endswith(b"\r\n"):
        raise ComponentError("Python runtime probe frame is invalid")
    frame = line[len(PROBE_FRAME_PREFIX) : -1]
    try:
        payload: Any = json.loads(
            frame.decode("utf-8"),
            object_pairs_hook=_closed_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ComponentError) as error:
        raise ComponentError("Python runtime probe frame is invalid") from error
    if not isinstance(payload, dict) or set(payload) != fields:
        raise ComponentError("Python runtime probe frame fields are invalid")
    return payload


def _closed_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ComponentError("Python runtime probe frame contains duplicate fields")
        result[key] = value
    return result


WINDOWS_VC_DLL_NAMES = (
    "msvcp140.dll",
    "vcruntime140.dll",
    "vcruntime140_1.dll",
)
WINDOWS_VC_MINIMUM_TUPLE = (14, 51, 36247, 0)


def probe_windows_vc_runtime() -> dict[str, object]:
    """Read the 64-bit VC++ registry value and System32 file versions."""

    observed: dict[str, object] = {
        "registry_version": None,
        "system32": {name: None for name in WINDOWS_VC_DLL_NAMES},
    }
    if current_platform() != "windows" or current_architecture() != "x86_64":
        return {"status": "unverifiable_cross_target", "observed": observed}
    registry_raw = _read_windows_registry_version()
    dll_raw = {
        name: _read_windows_dll_version(name) for name in WINDOWS_VC_DLL_NAMES
    }
    registry_version = _normalize_windows_version(registry_raw)
    dll_versions = {
        name: _normalize_windows_version(value) for name, value in dll_raw.items()
    }
    observed["registry_version"] = registry_version
    observed["system32"] = dll_versions
    if registry_raw is None and all(value is None for value in dll_raw.values()):
        status = "missing"
    elif registry_version is None or any(value is None for value in dll_versions.values()):
        status = "registry_dll_mismatch"
    else:
        registry_tuple = _windows_version_tuple(registry_version)
        dll_tuples = {
            name: _windows_version_tuple(value) for name, value in dll_versions.items()
        }
        all_versions = {registry_tuple, *dll_tuples.values()}
        if len(all_versions) != 1:
            status = "registry_dll_mismatch"
        elif registry_tuple is not None and registry_tuple < WINDOWS_VC_MINIMUM_TUPLE:
            status = "outdated"
        else:
            status = "ready"
    return {"status": status, "observed": observed}


def _normalize_windows_version(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if normalized[:1].lower() == "v":
        normalized = normalized[1:]
    parts = normalized.split(".")
    if len(parts) != 4 or any(not part.isdecimal() for part in parts):
        return None
    return ".".join(str(int(part)) for part in parts)


def _windows_version_tuple(value: str | None) -> tuple[int, int, int, int] | None:
    if value is None:
        return None
    parts = value.split(".")
    if len(parts) != 4 or any(not part.isdecimal() for part in parts):
        return None
    return tuple(int(part) for part in parts)  # type: ignore[return-value]


def _read_windows_registry_version() -> str | None:
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(  # type: ignore[attr-defined]
            winreg.HKEY_LOCAL_MACHINE,  # type: ignore[attr-defined]
            r"SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64",
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,  # type: ignore[attr-defined]
        ) as key:
            value, _value_type = winreg.QueryValueEx(key, "Version")  # type: ignore[attr-defined]
    except (OSError, ImportError):
        return None
    return value if isinstance(value, str) else None


def _read_windows_dll_version(name: str) -> str | None:
    if os.name != "nt":
        return None
    try:
        import ctypes

        version = ctypes.windll.version  # type: ignore[attr-defined]
        path = str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / name)
        size = version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        buffer = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(path, 0, size, buffer):
            return None
        pointer = ctypes.c_void_p()
        length = ctypes.c_uint()
        if not version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
            return None
        class _FixedFileInfo(ctypes.Structure):
            _fields_ = [
                ("signature", ctypes.c_uint32),
                ("struct_version", ctypes.c_uint32),
                ("file_version_ms", ctypes.c_uint32),
                ("file_version_ls", ctypes.c_uint32),
            ]

        info = ctypes.cast(pointer, ctypes.POINTER(_FixedFileInfo)).contents
        return ".".join(
            str(value)
            for value in (
                info.file_version_ms >> 16,
                info.file_version_ms & 0xFFFF,
                info.file_version_ls >> 16,
                info.file_version_ls & 0xFFFF,
            )
        )
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def path_is_within(child: str, root: str, *, platform: str) -> bool:
    if platform == "windows":
        normalized_child = ntpath.normcase(ntpath.abspath(ntpath.normpath(child)))
        normalized_root = ntpath.normcase(ntpath.abspath(ntpath.normpath(root)))
        try:
            return ntpath.commonpath((normalized_child, normalized_root)) == normalized_root
        except ValueError:
            return False
    normalized_child = posixpath.abspath(posixpath.normpath(child))
    normalized_root = posixpath.abspath(posixpath.normpath(root))
    return posixpath.commonpath((normalized_child, normalized_root)) == normalized_root


def component_digest(path: Path) -> str:
    if path.is_symlink():
        raise ComponentError("component artifacts must not be symbolic links")
    if path.is_file():
        return _file_sha256(path)
    if not path.is_dir():
        raise ComponentError("component artifact is missing")
    digest = hashlib.sha256(b"roughcut-component-directory-v1\0")
    for item in sorted(path.rglob("*"), key=lambda value: value.relative_to(path).as_posix()):
        if item.is_symlink():
            raise ComponentError("component artifacts must not contain symbolic links")
        relative = item.relative_to(path).as_posix().encode("utf-8")
        if item.is_dir():
            digest.update(b"D\0" + relative + b"\0")
        elif item.is_file():
            digest.update(b"F\0" + relative + b"\0")
            digest.update(bytes.fromhex(_file_sha256(item)))
        else:
            raise ComponentError("component artifact contains an unsupported entry")
    return digest.hexdigest()


def component_record_digest(name: str, path: Path) -> str:
    """Hash an install payload while excluding CAM++ client-only cache metadata."""

    if name != "model_spk":
        return component_digest(path)
    if path.is_symlink() or not path.is_dir():
        raise ComponentError("component artifact is missing or unsafe")
    digest = hashlib.sha256(b"roughcut-component-directory-v1\0")
    ignored_roots = {".mv", ".msc", "._____temp"}
    items = [
        item
        for item in path.rglob("*")
        if item.relative_to(path).parts[0] not in ignored_roots
    ]
    for item in sorted(items, key=lambda value: value.relative_to(path).as_posix()):
        if item.is_symlink():
            raise ComponentError("component artifacts must not contain symbolic links")
        relative = item.relative_to(path).as_posix().encode("utf-8")
        if item.is_dir():
            digest.update(b"D\0" + relative + b"\0")
        elif item.is_file():
            digest.update(b"F\0" + relative + b"\0")
            digest.update(bytes.fromhex(_file_sha256(item)))
        else:
            raise ComponentError("component artifact contains an unsupported entry")
    return digest.hexdigest()


def build_external_component(
    name: str,
    path: Path,
    *,
    version: str,
    origin: str,
    license: str,
    platform: str | None = None,
    architecture: str | None = None,
) -> ComponentRecord:
    resolved = path.resolve(strict=True)
    kind = COMPONENT_KINDS.get(name)
    if kind is None:
        raise ComponentError("unknown media component")
    if kind == "model" and not resolved.is_dir():
        raise ComponentError("model component must be a directory")
    if kind != "model" and not resolved.is_file():
        raise ComponentError("package and executable components must be files")
    if kind == "executable" and not os.access(resolved, os.X_OK):
        raise ComponentError("executable component is not executable")
    return ComponentRecord(
        name=name,
        kind=kind,
        source_type="external",
        origin=origin,
        version=version,
        path=str(resolved),
        platform=platform or current_platform(),
        architecture=architecture or current_architecture(),
        license=license,
        verification=ComponentVerification("sha256", component_record_digest(name, resolved)),
    )


def load_component_manifest(path: Path) -> ComponentManifest:
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ComponentError("component manifest is missing or unreadable") from error
    if not isinstance(raw, dict):
        raise ComponentError("component manifest must contain a JSON object")
    return ComponentManifest.from_dict(raw)


def write_component_manifest(path: Path, manifest: ComponentManifest) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(manifest.to_dict(), output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _validate_component_names(component_names: tuple[str, ...]) -> tuple[str, ...]:
    requested = tuple(component_names)
    if (
        not requested
        or len(requested) != len(set(requested))
        or not set(requested).issubset(COMPONENT_NAMES)
    ):
        raise ComponentError("component scope must be a non-empty subset of known components")
    return requested


def diagnose_components(
    *,
    external_manifest: ComponentManifest | None = None,
    managed_manifest: ComponentManifest | None = None,
    expected_versions: Mapping[str, str] | None = None,
    expected_verifications: Mapping[str, str] | None = None,
    platform: str | None = None,
    architecture: str | None = None,
    managed_root_override: Path | None = None,
    verify_checksums: bool = False,
    component_names: tuple[str, ...] | None = None,
) -> ComponentResolution:
    target_platform = platform or current_platform()
    target_architecture = architecture or current_architecture()
    external = _component_map(external_manifest)
    managed = _component_map(managed_manifest)
    diagnostics: dict[str, ComponentDiagnostic] = {}
    install_required: list[str] = []
    runtime_probes: dict[int, tuple[dict[str, str], str | None]] = {}
    requested = (
        COMPONENT_NAMES
        if component_names is None
        else _validate_component_names(component_names)
    )
    for name in requested:
        attempts: list[ComponentAttempt] = []
        selected: tuple[ComponentRecord, str] | None = None
        for source_type, record, manifest in (
            ("external", external.get(name), external_manifest),
            ("managed", managed.get(name), managed_manifest),
        ):
            if record is None or manifest is None:
                continue
            detail = _incompatibility_reason(
                record,
                manifest,
                expected_version=(expected_versions or {}).get(name),
                expected_verification=(expected_verifications or {}).get(name),
                target_platform=target_platform,
                target_architecture=target_architecture,
                managed_root_override=managed_root_override,
                verify_checksum=verify_checksums,
                runtime_probes=runtime_probes,
            )
            attempts.append(
                ComponentAttempt(
                    source_type=source_type,
                    status="available" if detail is None else "incompatible",
                    detail=detail,
                )
            )
            if detail is None:
                selected = (record, source_type)
                break
        if selected is None:
            install_required.append(name)
            diagnostics[name] = ComponentDiagnostic(
                name=name,
                status="install_required",
                selected_source=None,
                version=None,
                path=None,
                compatible=False,
                action="install_managed",
                attempts=tuple(attempts),
            )
            continue
        record, source_type = selected
        selected_manifest = (
            managed_manifest if source_type == "managed" else external_manifest
        )
        selected_path = _selected_component_path(
            record,
            selected_manifest,
            managed_root_override=managed_root_override,
        )
        diagnostics[name] = ComponentDiagnostic(
            name=name,
            status="available",
            selected_source=source_type,
            version=record.version,
            path=str(selected_path),
            compatible=True,
            action=f"reuse_{source_type}",
            attempts=tuple(attempts),
        )
    return ComponentResolution(
        diagnostics,
        tuple(install_required),
        verification_mode="full" if verify_checksums else "quick",
    )


def verify_components(
    *,
    external_manifest: ComponentManifest | None = None,
    managed_manifest: ComponentManifest | None = None,
    expected_versions: Mapping[str, str] | None = None,
    expected_verifications: Mapping[str, str] | None = None,
    platform: str | None = None,
    architecture: str | None = None,
    managed_root_override: Path | None = None,
) -> ComponentResolution:
    """Run explicit full artifact verification in addition to runtime checks."""

    return diagnose_components(
        external_manifest=external_manifest,
        managed_manifest=managed_manifest,
        expected_versions=expected_versions,
        expected_verifications=expected_verifications,
        platform=platform,
        architecture=architecture,
        managed_root_override=managed_root_override,
        verify_checksums=True,
    )


def install_local_bundle(
    managed_root: Path,
    bundle_manifest_path: Path,
    *,
    component_names: tuple[str, ...] | None = None,
) -> ComponentInstallResult:
    root = managed_root.resolve(strict=False)
    _validate_managed_root(root)
    bundle = load_component_manifest(bundle_manifest_path)
    records = _component_map(bundle)
    requested = (
        LEGACY_COMPONENT_NAMES
        if component_names is None
        else _validate_component_names(component_names)
    )
    if (
        not requested
        or len(requested) != len(set(requested))
        or not set(requested).issubset(COMPONENT_NAMES)
        or not set(requested).issubset(records)
        or any(records[name].source_type != "external" for name in requested)
    ):
        raise ComponentError("local component bundle does not contain the requested artifacts")
    expected_versions = {name: record.version for name, record in records.items()}
    bundle_diagnosis = diagnose_components(
        external_manifest=bundle,
        expected_versions=expected_versions,
        verify_checksums=True,
        component_names=requested,
    )
    if any(name in bundle_diagnosis.install_required for name in requested):
        raise ComponentError("local component bundle is incompatible or incomplete")

    manifest_path = root / COMPONENT_MANIFEST_FILENAME
    if root.exists():
        if not root.is_dir():
            raise ComponentError("managed root exists and is not a directory")
        installed = load_component_manifest(manifest_path)
        _validate_manifest_ownership(installed, root)
        diagnosis = diagnose_components(
            managed_manifest=installed,
            expected_versions=expected_versions,
            verify_checksums=True,
            component_names=requested,
        )
        requested_missing = tuple(
            name for name in diagnosis.install_required if name in requested
        )
        return ComponentInstallResult(
            installed=False,
            reused=not requested_missing,
            install_required=requested_missing,
            manifest=installed,
        )

    root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=root.parent, prefix=f".{root.name}.install-"))
    try:
        installed_records: list[ComponentRecord] = []
        for name in requested:
            source_record = records[name]
            source = Path(source_record.path)
            relative = _managed_destination(source_record, source)
            destination = staging / relative
            if source_record.kind == "model":
                _copy_directory(source, destination)
            else:
                _copy_file(source, destination)
            if component_record_digest(name, destination) != source_record.verification.value:
                raise ComponentError("copied component failed verification")
            installed_records.append(
                ComponentRecord(
                    name=source_record.name,
                    kind=source_record.kind,
                    source_type="managed",
                    origin=source_record.origin,
                    version=source_record.version,
                    path=relative.as_posix(),
                    platform=source_record.platform,
                    architecture=source_record.architecture,
                    license=source_record.license,
                    verification=source_record.verification,
                )
            )
        manifest = ComponentManifest(
            components=tuple(installed_records),
            platform=bundle.platform,
            architecture=bundle.architecture,
            managed_root=str(root),
        )
        write_component_manifest(staging / COMPONENT_MANIFEST_FILENAME, manifest)
        staged_diagnosis = diagnose_components(
            managed_manifest=manifest,
            expected_versions=expected_versions,
            managed_root_override=staging,
            verify_checksums=True,
            component_names=requested,
        )
        failed_artifacts = set(staged_diagnosis.install_required).intersection(requested)
        failed_artifacts.difference_update(PYTHON_COMPONENT_NAMES)
        if failed_artifacts:
            raise ComponentError("staged managed components failed validation")
        if root.exists():
            raise ComponentError("managed root appeared during installation")
        os.replace(staging, root)
        _sync_directory(root.parent)
    except BaseException as error:
        if staging.exists():
            shutil.rmtree(staging)
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(error, ComponentError):
            raise ComponentError(f"managed components could not be installed: {error}") from error
        raise ComponentError("managed components could not be installed") from error
    install_required = tuple(
        name
        for name in requested
        if name in PYTHON_COMPONENT_NAMES and manifest.python_runtime is None
    )
    return ComponentInstallResult(True, False, install_required, manifest)


def uninstall_managed_components(managed_root: Path) -> ComponentUninstallResult:
    root = managed_root.resolve(strict=False)
    _validate_managed_root(root)
    manifest_path = root / COMPONENT_MANIFEST_FILENAME
    if not manifest_path.is_file():
        return ComponentUninstallResult(False, ())
    manifest = load_component_manifest(manifest_path)
    _validate_manifest_ownership(manifest, root)
    owned: list[tuple[str | None, Path]] = []
    for component in manifest.components:
        if component.source_type != "managed":
            continue
        target = _component_path(component, manifest)
        resolved_target = target.resolve(strict=False)
        if not resolved_target.is_relative_to(root):
            raise ComponentError("managed component path escapes managed root")
        owned.append((component.name, target))
    if manifest.python_runtime is not None:
        runtime_root, dependency_lock = _runtime_paths(manifest)
        owned.extend(((None, runtime_root), (None, dependency_lock)))

    removed: list[str] = []
    for name, target in sorted(owned, key=lambda item: len(item[1].parts), reverse=True):
        if target.is_symlink():
            raise ComponentError("managed component path escapes managed root")
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)
        if name is not None:
            removed.append(name)
    manifest_path.unlink()
    for directory in sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        try:
            directory.rmdir()
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass
    return ComponentUninstallResult(True, tuple(removed))


def _incompatibility_reason(
    record: ComponentRecord,
    manifest: ComponentManifest,
    *,
    expected_version: str | None,
    expected_verification: str | None,
    target_platform: str,
    target_architecture: str,
    managed_root_override: Path | None,
    verify_checksum: bool,
    runtime_probes: dict[int, tuple[dict[str, str], str | None]],
) -> str | None:
    if record.platform != target_platform:
        return "platform does not match"
    if record.architecture != target_architecture:
        return "architecture does not match"
    if expected_version is not None and record.version != expected_version:
        return "version does not match"
    if (
        expected_verification is not None
        and record.verification.value != expected_verification
    ):
        return "catalog verification does not match"
    try:
        path = _component_path(
            record, manifest, managed_root_override=managed_root_override
        )
        if record.kind == "model":
            if not path.is_dir():
                return "model directory is missing"
            for required in MODEL_RUNTIME_FILES.get(
                record.name, (MODEL_PRIMARY_PAYLOADS.get(record.name, "model.pt"),)
            ):
                required_path = path / required
                if (
                    path.is_symlink()
                    or not required_path.is_file()
                    or required_path.is_symlink()
                ):
                    return f"model runtime file is missing or unsafe: {required}"
        elif not path.is_file():
            return "component file is missing"
        if record.kind == "executable" and not os.access(path, os.X_OK):
            return "component is not executable"
        if (
            (verify_checksum or record.kind != "model")
            and component_record_digest(record.name, path) != record.verification.value
        ):
            return "component checksum differs"
        if record.kind == "python_package" and record.source_type == "managed":
            if record.name in _ALIGNMENT_GROUP_PROBES:
                probe_config = _ALIGNMENT_GROUP_PROBES[record.name]
                return _probe_alignment_group(
                    record,
                    manifest,
                    managed_root_override=managed_root_override,
                    **probe_config,
                )
            if manifest.python_runtime is None:
                return "managed Python runtime manifest is missing"
            probe_key = id(manifest)
            if probe_key not in runtime_probes:
                runtime_probes[probe_key] = _probe_managed_python_runtime(
                    manifest,
                    managed_root_override=managed_root_override,
                )
            versions, runtime_error = runtime_probes[probe_key]
            if versions and versions.get(record.name) != record.version:
                return "managed Python runtime versions differ"
            if runtime_error is not None:
                return runtime_error
            if versions.get(record.name) != record.version:
                return "managed Python runtime versions differ"
    except (ComponentError, OSError) as error:
        return str(error)
    return None


_ALIGNMENT_GROUP_PROBES = {
    "audalign": {"managed_dir": "audalign", "provider": "audalign"},
    "bbc_audio_offset_finder": {
        "managed_dir": "bbc_audio_offset_finder",
        "provider": "bbc_audio_offset_finder",
    },
}


def _probe_alignment_group(
    record: ComponentRecord,
    manifest: ComponentManifest,
    *,
    managed_root_override: Path | None,
    managed_dir: str,
    provider: str,
) -> str | None:
    """Verify one isolated alignment venv and its pinned closure in place.

    The live probe reads the actual installed distribution state through
    importlib.metadata and must match the exact platform-specific frozen
    name+version pairs, not just the direct distribution version.
    """
    if manifest.managed_root is None:
        return f"managed {provider} root is missing"
    root = managed_root_override or Path(manifest.managed_root)
    venv_root = root / managed_dir / "venv"
    interpreter = root / PurePosixPath(
        f"{managed_dir}/venv/Scripts/python.exe"
        if manifest.platform == "windows"
        else f"{managed_dir}/venv/bin/python"
    )
    if venv_root.is_symlink() or not venv_root.is_dir():
        return f"{provider} venv is missing or a symbolic link"
    if interpreter.is_symlink() or not interpreter.is_file():
        return f"{provider} interpreter is missing or a symbolic link"
    if not os.access(interpreter, os.X_OK):
        return f"{provider} interpreter is missing or not executable"
    receipt = root / PurePosixPath(record.path)
    if not receipt.is_file() or component_digest(receipt) != record.verification.value:
        return f"{provider} receipt checksum differs"
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("PYTHON") and name not in VIRTUAL_ENVIRONMENT_VARIABLES
    }
    bbc_probe = ""
    expected_probe_fields = {"python_version", "distributions"}
    if provider == "bbc_audio_offset_finder":
        expected_probe_fields.update(
            {
                "audio_offset_finder_imported",
                "soundfile_imported",
                "soundfile_bundled_library",
            }
        )
        bbc_probe = (
            "import audio_offset_finder,soundfile,_soundfile_data,pathlib;"
            "native_name=('libsndfile_arm64.dylib' if sys.platform=='darwin' "
            "else 'libsndfile_x64.dll' if sys.platform=='win32' else '');"
            "native_path=pathlib.Path(_soundfile_data.__file__).parent/native_name;"
            "bundled=(native_path.is_file() and "
            "pathlib.Path(soundfile._full_path)==native_path and "
            "bool(soundfile.__libsndfile_version__));"
        )
    probe = (
        "import importlib.metadata as m,json,sys;"
        + "d=sorted((x.metadata.get('Name') or x.name, x.version) "
        + "for x in m.distributions());"
        + bbc_probe
        + "payload={'python_version':f'{sys.version_info.major}.{sys.version_info.minor}',"
        + "'distributions':[[n,v] for n,v in d]"
        + (
            ",'audio_offset_finder_imported':True,'soundfile_imported':True,"
            "'soundfile_bundled_library':bundled"
            if provider == "bbc_audio_offset_finder"
            else ""
        )
        + "};"
        + "sys.stdout.buffer.write(b'ROUGHCUT-PROBE/1 '+"
        + "json.dumps(payload,ensure_ascii=False,separators=(',',':'),sort_keys=True).encode('utf-8')+b'\\n')"
    )
    try:
        result = subprocess.run(
            [str(interpreter), "-I", "-c", probe],
            check=False,
            capture_output=True,
            timeout=PYTHON_RUNTIME_PROBE_TIMEOUT_SECONDS,
            env=environment,
        )
    except subprocess.TimeoutExpired:
        return f"{provider} runtime probe timed out"
    except OSError:
        return f"{provider} runtime probe failed"
    if result.returncode != 0:
        return f"{provider} runtime probe failed with exit code {result.returncode}"
    try:
        payload = _parse_probe_frame(
            result.stdout,
            frozenset(expected_probe_fields),
        )
    except ComponentError:
        return f"{provider} runtime probe returned invalid JSON"
    if payload.get("python_version") != "3.11":
        return f"{provider} runtime Python version differs"
    distributions = payload.get("distributions")
    if not isinstance(distributions, list):
        return f"{provider} runtime probe is incomplete"
    # canonicalize every live distribution name per PEP 503 (lowercase,
    # runs of [_.-] collapsed to a single dash) before the closure comparison
    def _canonicalize(name: str) -> str:
        return re.sub(r"[-_.]+", "-", name).lower()

    actual: list[tuple[str, str]] = []
    for item in distributions:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], str)
        ):
            return f"{provider} runtime probe is incomplete"
        actual.append((_canonicalize(item[0]), item[1]))
    actual = sorted(actual)
    # The exact frozen closure is imported lazily to avoid a circular import
    # with runtime_binding (which itself imports this module). The target
    # manifest selects the platform-specific closure; Windows includes the
    # colorama marker dependency and macOS does not.
    from roughcut.adapters.runtime_binding import (
        audalign_distribution_versions_for,
        audalign_distributions_for,
        bbc_audio_offset_finder_distribution_versions_for,
        bbc_audio_offset_finder_distributions_for,
    )

    if provider == "audalign":
        distributions_getter = audalign_distributions_for
        versions_getter = audalign_distribution_versions_for
        direct_distribution = "audalign"
    else:
        distributions_getter = bbc_audio_offset_finder_distributions_for
        versions_getter = bbc_audio_offset_finder_distribution_versions_for
        direct_distribution = "audio-offset-finder"

    try:
        expected_distributions = distributions_getter(manifest.platform)
        expected_versions = versions_getter(manifest.platform)
    except RuntimeError:
        return f"{provider} platform closure is unsupported"
    expected = sorted(expected_distributions)
    if [name for name, _version in actual] != expected:
        return f"{provider} distribution closure differs"
    if len(actual) != len({entry for entry in actual}):
        return f"{provider} distribution closure contains duplicates"
    actual_versions = dict(actual)
    for name in expected_distributions:
        if actual_versions.get(name) != expected_versions[name]:
            return f"{provider} distribution versions differ"
    if actual_versions.get(direct_distribution) != record.version:
        return f"{provider} runtime versions differ"
    if provider == "bbc_audio_offset_finder" and (
        payload.get("audio_offset_finder_imported") is not True
        or payload.get("soundfile_imported") is not True
    ):
        return "bbc_audio_offset_finder runtime imports are unavailable"
    if (
        provider == "bbc_audio_offset_finder"
        and payload.get("soundfile_bundled_library") is not True
    ):
        return "bbc_audio_offset_finder bundled libsndfile is unavailable"
    return None


def _probe_managed_python_runtime(
    manifest: ComponentManifest,
    *,
    managed_root_override: Path | None,
) -> tuple[dict[str, str], str | None]:
    runtime = manifest.python_runtime
    if runtime is None:
        return {}, "managed Python runtime manifest is missing"
    root = managed_root_override or Path(manifest.managed_root or "")
    interpreter = root / PurePosixPath(runtime.interpreter)
    dependency_lock = root / PurePosixPath(runtime.dependency_lock)
    if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        return {}, "managed Python interpreter is missing or not executable"
    if not dependency_lock.is_file():
        return {}, "managed Python dependency lock is missing"
    if component_digest(dependency_lock) != runtime.lock_verification.value:
        return {}, "managed Python dependency lock checksum differs"
    try:
        payload = probe_python_runtime(interpreter)
    except ComponentError as error:
        return {}, str(error)
    versions: dict[str, str] = {}
    for name in PYTHON_COMPONENT_NAMES:
        value = payload.get(name)
        if not isinstance(value, str) or not value:
            return {}, "managed Python runtime probe is incomplete"
        versions[name] = value
    if payload.get("python_version") != runtime.python_version:
        return versions, "managed Python runtime version differs"
    if payload.get("cuda_version") is not None or payload.get("cuda_available") is not False:
        return versions, "managed Python runtime is not CPU-only"
    return versions, None


def _selected_component_path(
    record: ComponentRecord,
    manifest: ComponentManifest | None,
    *,
    managed_root_override: Path | None,
) -> Path:
    if (
        record.kind == "python_package"
        and record.source_type == "managed"
        and manifest is not None
        and manifest.python_runtime is not None
    ):
        root = managed_root_override or Path(manifest.managed_root or "")
        return root / PurePosixPath(manifest.python_runtime.interpreter)
    return _component_path(
        record,
        manifest,
        managed_root_override=managed_root_override,
    )


def _component_path(
    record: ComponentRecord,
    manifest: ComponentManifest | None,
    *,
    managed_root_override: Path | None = None,
) -> Path:
    if record.source_type == "external":
        return Path(record.path)
    if manifest is None or manifest.managed_root is None:
        raise ComponentError("managed component has no owning root")
    root = managed_root_override or Path(manifest.managed_root)
    candidate = root / PurePosixPath(record.path)
    if not path_is_within(
        str(candidate), str(root), platform="windows" if record.platform == "windows" else "posix"
    ):
        raise ComponentError("managed component path escapes managed root")
    return candidate


def _runtime_paths(
    manifest: ComponentManifest,
    *,
    managed_root_override: Path | None = None,
) -> tuple[Path, Path]:
    runtime = manifest.python_runtime
    if runtime is None or manifest.managed_root is None:
        raise ComponentError("managed Python runtime has no owning root")
    root = managed_root_override or Path(manifest.managed_root)
    runtime_root = root / PurePosixPath(runtime.root)
    dependency_lock = root / PurePosixPath(runtime.dependency_lock)
    for candidate in (runtime_root, dependency_lock):
        if not path_is_within(
            str(candidate),
            str(root),
            platform="windows" if manifest.platform == "windows" else "posix",
        ):
            raise ComponentError("managed Python runtime path escapes managed root")
    return runtime_root, dependency_lock


def _managed_destination(record: ComponentRecord, source: Path) -> Path:
    if record.kind == "model":
        return Path("models") / record.name
    if record.kind == "executable":
        return Path("bin") / source.name
    return Path("packages") / record.name / source.name


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _copy_directory(source: Path, destination: Path) -> None:
    component_digest(source)
    shutil.copytree(source, destination, copy_function=shutil.copy2)


def _component_map(manifest: ComponentManifest | None) -> dict[str, ComponentRecord]:
    if manifest is None:
        return {}
    return {component.name: component for component in manifest.components}


def _validate_manifest_ownership(manifest: ComponentManifest, root: Path) -> None:
    if manifest.managed_root is None:
        raise ComponentError("managed manifest has no owning root")
    if Path(manifest.managed_root).resolve(strict=False) != root:
        raise ComponentError("component manifest belongs to a different managed root")
    for component in manifest.components:
        if component.source_type == "managed":
            _component_path(component, manifest)
    if manifest.python_runtime is not None:
        _runtime_paths(manifest)


def _is_absolute_path(value: str, *, platform: str) -> bool:
    if platform == "windows":
        return PureWindowsPath(value).is_absolute()
    return PurePosixPath(value).is_absolute()


def _is_safe_relative_path(value: str) -> bool:
    path = PurePosixPath(value)
    return not path.is_absolute() and value not in {"", "."} and ".." not in path.parts


def _is_canonical_managed_path(name: str, kind: str, value: str) -> bool:
    parts = PurePosixPath(value).parts
    if kind == "model":
        return parts == ("models", name)
    if kind == "executable":
        return len(parts) == 2 and parts[0] == "bin"
    if name in _ALIGNMENT_GROUP_PROBES:
        return parts == (name, "venv-receipt.json")
    if len(parts) == 4 and parts[:2] == ("audalign", "packages"):
        return parts[3] == "receipt.json"
    return len(parts) == 3 and parts[:2] == ("packages", name)


def _validate_managed_root(root: Path) -> None:
    if root == root.parent or root == Path.home().resolve(strict=False):
        raise ComponentError("managed root must be a dedicated subdirectory")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _required_string(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ComponentError(f"component manifest {key} is invalid")
    return value


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
