"""Assemble and validate the standalone Roughcut Core trial unit."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import zipfile
from collections.abc import Iterable
from contextlib import chdir
from email.parser import Parser
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORE_SOURCE = ROOT / "core"
CORE_VERSION = "0.2.9"
WHEEL_NAME = f"roughcut-{CORE_VERSION}-py3-none-any.whl"
SOURCE_BUNDLE_NAME = f"roughcut-core-{CORE_VERSION}-source.tar.gz"
SOURCE_BUNDLE_ROOT = f"roughcut-core-{CORE_VERSION}"
SOURCE_DATE_EPOCH = "0"

SOURCE_BUNDLE_DIRECTORIES = (
    "core",
    "agent-skill",
    "host-integrations",
)
SOURCE_BUNDLE_FILES = (
    "README.md",
    "scripts/bootstrap.py",
    "scripts/build_core_release.py",
    "scripts/build_core_update.py",
    "scripts/build_host_package.py",
    "scripts/release_asr_smoke.py",
    "scripts/uninstall.py",
    "scripts/validate_agent_skills.py",
    "docs/agent-tool-contract.md",
    "docs/installation.md",
    "docs/release/PRODUCT_PACKAGING.md",
)
SOURCE_BUNDLE_EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".nox",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "htmlcov",
        "tests",
    }
)

CORE_UPGRADE_AGENT = f"""# Roughcut Core {CORE_VERSION} 离线升级交付

本文件与同目录的 Core source bundle、wheel 和 CORE-SHA256SUMS 组成一个独立 Core 交付单元。

## 交付前

1. 只把本目录交给目标测试机；该目录本身包含执行 Core-only upgrade 所需的 source bundle。
2. 在临时环境核对 `CORE-SHA256SUMS`，不要从源码构建 wheel。

## Core-only command

```sh
set -eu
cd /absolute/path/to/Core-{CORE_VERSION}
CORE_PACKAGE_DIR="$PWD"
CORE_WHEEL="$CORE_PACKAGE_DIR/{WHEEL_NAME}"
INSTALL_DIR="/absolute/path/to/existing-roughcut-install"
WORKBUDDY_HOST_PACKAGE="/absolute/path/to/new-workbuddy-host-package"
CODEX_HOST_PACKAGE="/absolute/path/to/new-codex-host-package"

shasum -a 256 -c "$CORE_PACKAGE_DIR/CORE-SHA256SUMS"
CORE_TMP="$(mktemp -d -t roughcut-core-{CORE_VERSION}-source)"
trap 'rm -rf -- "$CORE_TMP"' EXIT
tar -xzf "$CORE_PACKAGE_DIR/{SOURCE_BUNDLE_NAME}" -C "$CORE_TMP"
CORE_ROOT="$CORE_TMP/{SOURCE_BUNDLE_ROOT}"
MCP_COMMAND="$INSTALL_DIR/venv/bin/roughcut-mcp"

python3.11 "$CORE_ROOT/scripts/bootstrap.py" \\
  --install-dir "$INSTALL_DIR" \\
  --core-wheel "$CORE_WHEEL" \\
  --json

"$INSTALL_DIR/venv/bin/roughcut" health --json
"$INSTALL_DIR/venv/bin/roughcut" diagnostics --json

# Current target: refresh the WorkBuddy Host Package.
python3.11 "$CORE_ROOT/scripts/build_host_package.py" \\
  --host workbuddy \\
  --output "$WORKBUDDY_HOST_PACKAGE" \\
  --mcp-command "$MCP_COMMAND"

# Codex equivalent, when Codex is the target host instead:
python3.11 "$CORE_ROOT/scripts/build_host_package.py" \\
  --host codex \\
  --output "$CODEX_HOST_PACKAGE" \\
  --mcp-command "$MCP_COMMAND"
```

该命令只使用本 Core folder 和解压后的 `CORE_ROOT`，不传组件参数。bootstrap 必须先验证 wheel
存在、是普通文件、文件名精确为 `{WHEEL_NAME}`，以及内嵌 METADATA 的 Name/Version 精确为
`roughcut`/{CORE_VERSION}；验证失败就停止。pip 只从该本地 wheel 离线安装，必须使用
`--no-index --disable-pip-version-check --no-deps`，更新继续使用 `--upgrade --force-reinstall`。
不得从 source bundle 构建、安装 setuptools、绕过 hash、使用代理旁路或清缓存解决问题。`MCP_COMMAND`
固定绑定当前 install root 的绝对 `venv/bin/roughcut-mcp`；用户 reload 宿主前按其宿主完成信任流程。
runtime.json、components、模型、ASR、Audalign、FFmpeg 和既有 `.roughcut` 状态必须原样复用。
命令退出时 trap 会删除 `CORE_TMP`。
"""


class ReleaseAssemblyError(RuntimeError):
    """Raised when the standalone Core unit is not safe to publish."""


_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_BUILD_IDENTITY_RELATIVE = "core/src/roughcut/_build_identity.py"


def _render_build_identity(source_commit: str | None) -> str:
    if source_commit is None:
        literal = "None"
    else:
        literal = f'"{source_commit}"'
    return (
        '"""Build-injected Code source identity (never commit a SHA here).\n'
        '\n'
        'This file is overwritten in release staging only. The tracked source\n'
        'always keeps ``SOURCE_COMMIT = None``.\n'
        '"""\n'
        '\n'
        'from __future__ import annotations\n'
        '\n'
        f"SOURCE_COMMIT: str | None = {literal}\n"
    )


def _stage_shared_source_with_identity(
    source_root: Path, staging_root: Path, source_commit: str | None
) -> None:
    files = _source_bundle_files(source_root)
    for relative, path in files.items():
        destination = staging_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    identity_path = staging_root / _BUILD_IDENTITY_RELATIVE
    identity_path.parent.mkdir(parents=True, exist_ok=True)
    identity_path.write_text(
        _render_build_identity(source_commit), encoding="utf-8"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _metadata_from_bytes(payload: bytes, *, artifact: str) -> dict[str, str]:
    try:
        metadata = Parser().parsestr(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ReleaseAssemblyError(
            f"{artifact} metadata is not valid UTF-8 email metadata"
        ) from error
    values = {
        "Name": metadata.get("Name", ""),
        "Version": metadata.get("Version", ""),
    }
    if values["Name"] != "roughcut" or values["Version"] != CORE_VERSION:
        raise ReleaseAssemblyError(
            f"{artifact} metadata identity must be roughcut/{CORE_VERSION}"
        )
    return values


def _wheel_metadata(artifact: Path) -> tuple[set[str], str]:
    try:
        with zipfile.ZipFile(artifact) as archive:
            names = set(archive.namelist())
            metadata_names = [
                name for name in names if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                raise ReleaseAssemblyError(
                    "wheel must contain exactly one dist-info/METADATA"
                )
            _metadata_from_bytes(
                archive.read(metadata_names[0]),
                artifact=artifact.name,
            )
            entry_points_names = [
                name for name in names if name.endswith(".dist-info/entry_points.txt")
            ]
            if len(entry_points_names) != 1:
                raise ReleaseAssemblyError(
                    "wheel must contain exactly one dist-info/entry_points.txt"
                )
            entry_points = archive.read(entry_points_names[0]).decode("utf-8")
            wheel_names = [name for name in names if name.endswith(".dist-info/WHEEL")]
            if len(wheel_names) != 1:
                raise ReleaseAssemblyError("wheel must contain exactly one dist-info/WHEEL")
            wheel_metadata = archive.read(wheel_names[0]).decode("utf-8")
    except ReleaseAssemblyError:
        raise
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile, KeyError) as error:
        raise ReleaseAssemblyError(f"{artifact.name} is not a readable wheel") from error

    required = {
        "roughcut/review/static/index.html",
        "roughcut/component_catalog/release-catalog.json",
        "roughcut/component_catalog/models.json",
    }
    if not required <= names:
        missing = ", ".join(sorted(required - names))
        raise ReleaseAssemblyError(f"wheel is missing required Core assets: {missing}")
    if not any(
        name.startswith("roughcut/review/static/assets/") and name.endswith(".js")
        for name in names
    ):
        raise ReleaseAssemblyError("wheel is missing the built Review JavaScript asset")
    if not any(
        name.startswith("roughcut/review/static/assets/") and name.endswith(".css")
        for name in names
    ):
        raise ReleaseAssemblyError("wheel is missing the built Review CSS asset")
    for entry_point in (
        "roughcut = roughcut.cli:main",
        "roughcut-mcp = roughcut.mcp:main",
    ):
        if entry_point not in entry_points.splitlines():
            raise ReleaseAssemblyError(f"wheel entry points are missing {entry_point!r}")
    if "Root-Is-Purelib: true" not in wheel_metadata.splitlines():
        raise ReleaseAssemblyError("wheel is not marked Root-Is-Purelib: true")
    return names, entry_points


def _safe_tar_members(members: Iterable[tarfile.TarInfo]) -> list[tarfile.TarInfo]:
    safe_members: list[tarfile.TarInfo] = []
    for member in members:
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise ReleaseAssemblyError(f"source bundle contains an unsafe path: {member.name}")
        if member.issym() or member.islnk():
            raise ReleaseAssemblyError(f"source bundle contains a link: {member.name}")
        safe_members.append(member)
    return safe_members


def _tar_info(
    name: str,
    *,
    mode: int,
    size: int = 0,
    member_type: bytes = tarfile.REGTYPE,
) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = member_type
    info.mode = mode
    info.size = size
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    info.pax_headers = {}
    return info


def _source_bundle_files(source_root: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}

    def add_file(relative: str) -> None:
        path = source_root / relative
        try:
            details = os.lstat(path)
        except OSError as error:
            raise ReleaseAssemblyError(
                f"source bundle input is missing: {relative}"
            ) from error
        if not stat.S_ISREG(details.st_mode):
            raise ReleaseAssemblyError(
                f"source bundle input is not a regular file: {relative}"
            )
        files[relative] = path

    def add_directory(relative_directory: str) -> None:
        directory = source_root / relative_directory
        try:
            details = os.lstat(directory)
        except OSError as error:
            raise ReleaseAssemblyError(
                f"source bundle directory is missing: {relative_directory}"
            ) from error
        if not stat.S_ISDIR(details.st_mode):
            raise ReleaseAssemblyError(
                f"source bundle path is not a directory: {relative_directory}"
            )
        for current, directory_names, file_names in os.walk(
            directory,
            topdown=True,
            followlinks=False,
        ):
            current_path = Path(current)
            kept_directories: list[str] = []
            for name in sorted(directory_names):
                child = current_path / name
                if child.is_symlink():
                    raise ReleaseAssemblyError(
                        f"source bundle contains a symlink: {child.relative_to(source_root)}"
                    )
                if name in SOURCE_BUNDLE_EXCLUDED_DIRECTORY_NAMES or name.endswith(
                    ".egg-info"
                ):
                    continue
                kept_directories.append(name)
            directory_names[:] = kept_directories
            for name in sorted(file_names):
                child = current_path / name
                relative = child.relative_to(source_root).as_posix()
                if name in {".coverage"} or name.endswith((".pyc", ".pyo")):
                    continue
                add_file(relative)

    for relative_directory in SOURCE_BUNDLE_DIRECTORIES:
        add_directory(relative_directory)
    for relative_file in SOURCE_BUNDLE_FILES:
        add_file(relative_file)
    return files


def _write_source_bundle(source_root: Path, artifact: Path) -> None:
    files = _source_bundle_files(source_root)
    directory_names: set[str] = {SOURCE_BUNDLE_ROOT}
    for relative in files:
        parts = PurePosixPath(relative).parts
        directory_names.update(
            str(PurePosixPath(SOURCE_BUNDLE_ROOT, *parts[:index]))
            for index in range(1, len(parts))
        )
    try:
        with (
            artifact.open("wb") as raw,
            gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed,
            tarfile.open(
                fileobj=compressed,
                mode="w",
                format=tarfile.PAX_FORMAT,
            ) as target,
        ):
            for directory_name in sorted(directory_names):
                target.addfile(
                    _tar_info(
                        directory_name,
                        mode=0o755,
                        member_type=tarfile.DIRTYPE,
                    )
                )
            for relative, path in sorted(files.items()):
                archive_name = str(PurePosixPath(SOURCE_BUNDLE_ROOT, relative))
                mode = stat.S_IMODE(os.lstat(path).st_mode)
                info = _tar_info(archive_name, mode=mode, size=path.stat().st_size)
                with path.open("rb") as stream:
                    target.addfile(info, stream)
    except ReleaseAssemblyError:
        raise
    except (OSError, EOFError, tarfile.TarError) as error:
        raise ReleaseAssemblyError(f"{artifact.name} could not be assembled") from error


def _validate_source_bundle(artifact: Path) -> None:
    root = f"{SOURCE_BUNDLE_ROOT}/"
    try:
        with tarfile.open(artifact, mode="r:gz") as archive:
            members = _safe_tar_members(archive.getmembers())
            if len(names := {member.name for member in members}) != len(members):
                raise ReleaseAssemblyError("source bundle contains duplicate archive paths")
            outside_root = sorted(
                name
                for name in names
                if name != SOURCE_BUNDLE_ROOT and not name.startswith(root)
            )
            if outside_root:
                raise ReleaseAssemblyError(
                    f"source bundle contains paths outside {SOURCE_BUNDLE_ROOT}: "
                    f"{', '.join(outside_root[:5])}"
                )
            required = {
                f"{root}scripts/bootstrap.py",
                f"{root}scripts/build_host_package.py",
                f"{root}scripts/build_core_update.py",
                f"{root}scripts/release_asr_smoke.py",
                f"{root}scripts/validate_agent_skills.py",
                f"{root}agent-skill/skills/roughcut/SKILL.md",
                f"{root}host-integrations/codex/.mcp.json",
                f"{root}host-integrations/workbuddy/.mcp.json",
                f"{root}core/pyproject.toml",
                f"{root}core/src/roughcut/__init__.py",
                f"{root}docs/agent-tool-contract.md",
                f"{root}docs/installation.md",
                f"{root}docs/release/PRODUCT_PACKAGING.md",
            }
            if not required <= names:
                missing = ", ".join(sorted(required - names))
                raise ReleaseAssemblyError(
                    f"source bundle is missing required Core files: {missing}"
                )
            forbidden_markers = (
                "/tests/",
                "/build/",
                "/__pycache__/",
                "/.pytest_cache/",
                "/.ruff_cache/",
                "/.mypy_cache/",
                ".pyc",
                ".pyo",
                ".egg-info/",
                "Large-Components",
                "component-cache",
            )
            forbidden = sorted(
                name for name in names if any(marker in name for marker in forbidden_markers)
            )
            if forbidden:
                raise ReleaseAssemblyError(
                    f"source bundle contains forbidden paths: {', '.join(forbidden[:5])}"
                )
    except ReleaseAssemblyError:
        raise
    except (OSError, EOFError, tarfile.TarError) as error:
        raise ReleaseAssemblyError(
            f"{artifact.name} is not a readable Core source bundle"
        ) from error


def _build_wheel(core_source: Path, build_dir: Path) -> Path:
    try:
        from setuptools import build_meta
    except ImportError as error:
        raise ReleaseAssemblyError(
            "local setuptools is required for Core release assembly; no dependency is installed"
        ) from error

    with tempfile.TemporaryDirectory(prefix="roughcut-core-build-") as temporary_dir:
        staging_source = Path(temporary_dir) / "core"
        staging_output = Path(temporary_dir) / "dist"
        shutil.copytree(
            core_source,
            staging_source,
            ignore=shutil.ignore_patterns("build", "*.egg-info", ".pytest_cache"),
        )
        staging_output.mkdir()
        previous_source_date_epoch = os.environ.get("SOURCE_DATE_EPOCH")
        os.environ["SOURCE_DATE_EPOCH"] = SOURCE_DATE_EPOCH
        try:
            with chdir(staging_source):
                wheel_name = build_meta.build_wheel(str(staging_output))
        finally:
            if previous_source_date_epoch is None:
                os.environ.pop("SOURCE_DATE_EPOCH", None)
            else:
                os.environ["SOURCE_DATE_EPOCH"] = previous_source_date_epoch
        if wheel_name != WHEEL_NAME:
            raise ReleaseAssemblyError(f"backend produced an unexpected wheel: {wheel_name}")
        wheel_path = build_dir / WHEEL_NAME
        shutil.copy2(staging_output / WHEEL_NAME, wheel_path)
    return wheel_path


def assemble(
    core_source: Path, output: Path, *, source_commit: str | None = None
) -> tuple[Path, Path]:
    if source_commit is not None and _COMMIT_RE.fullmatch(source_commit) is None:
        raise ReleaseAssemblyError(
            "source_commit must be a 40-character lowercase Git SHA"
        )
    core_source = core_source.resolve()
    output = output.resolve()
    if not core_source.is_dir():
        raise ReleaseAssemblyError(f"Core source directory is missing: {core_source}")
    if output.exists() and any(output.iterdir()):
        raise ReleaseAssemblyError(f"output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="roughcut-shared-source-") as shared_dir:
        shared_root = Path(shared_dir) / "repo"
        shared_root.mkdir()
        _stage_shared_source_with_identity(
            core_source.parent, shared_root, source_commit
        )
        staged_core = shared_root / "core"
        wheel_path = _build_wheel(staged_core, output)
        source_bundle_path = output / SOURCE_BUNDLE_NAME
        _write_source_bundle(shared_root, source_bundle_path)
    if wheel_path.name != WHEEL_NAME:
        raise ReleaseAssemblyError(f"unexpected wheel filename: {wheel_path.name}")
    _wheel_metadata(wheel_path)
    _validate_source_bundle(source_bundle_path)

    guide_path = output / "CORE-UPGRADE-AGENT.md"
    guide_path.write_text(CORE_UPGRADE_AGENT, encoding="utf-8")
    checksums = "".join(
        f"{_sha256(path)}  {path.name}\n"
        for path in (source_bundle_path, wheel_path)
    )
    (output / "CORE-SHA256SUMS").write_text(checksums, encoding="utf-8")
    return source_bundle_path, wheel_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-source", type=Path, default=DEFAULT_CORE_SOURCE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-commit", required=False, default=None)
    args = parser.parse_args()
    try:
        assemble(
            args.core_source, args.output, source_commit=args.source_commit
        )
    except ReleaseAssemblyError as error:
        print(f"Roughcut Core release assembly failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
