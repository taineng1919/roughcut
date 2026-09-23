"""Assemble and validate the standalone Roughcut Core update folder."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from pathlib import Path

if __package__ in {
    None,
    "",
}:  # Support direct execution from an extracted source bundle.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.build_core_release import (
    CORE_VERSION,
    SOURCE_BUNDLE_NAME,
    WHEEL_NAME,
    ReleaseAssemblyError,
    assemble as assemble_core,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORE_SOURCE = ROOT / "core"
UPDATE_FOLDER_NAME = f"roughcut-macos-arm64-{CORE_VERSION}-core-update"
CORE_DIRECTORY_NAME = f"Core-{CORE_VERSION}"
MANIFEST_NAME = "release-manifest.json"
ROOT_CHECKSUMS_NAME = "SHA256SUMS"
CORE_CHECKSUMS_NAME = "CORE-SHA256SUMS"
CORE_GUIDE_NAME = "CORE-UPGRADE-AGENT.md"
INSTALL_GUIDE_NAME = "INSTALL-AGENT.md"

CORE_PAYLOAD_NAMES = (
    CORE_CHECKSUMS_NAME,
    CORE_GUIDE_NAME,
    WHEEL_NAME,
    SOURCE_BUNDLE_NAME,
)
ROOT_PAYLOAD_NAMES = tuple(
    f"{CORE_DIRECTORY_NAME}/{name}" for name in CORE_PAYLOAD_NAMES
) + (INSTALL_GUIDE_NAME, MANIFEST_NAME)

_INSTALL_AGENT = f"""# Roughcut Core {CORE_VERSION} macOS arm64 离线升级目录

本目录是已有完整 Roughcut 安装的 Core-only 更新目录，不是首次安装包、安装程序、DMG、launcher
或自动更新器。根目录只包含 `Core-{CORE_VERSION}/`、`INSTALL-AGENT.md`、`SHA256SUMS` 和
`release-manifest.json`；其中 `Core-{CORE_VERSION}/` 是独立的四文件 Core 交付单元。
本目录不包含 Large Components、模型、ASR/Audalign runtime、FFmpeg、用户项目或媒体。
根目录的 `release-manifest.json` 是本次目录身份和 Core 文件摘要的机器可读记录；根 `SHA256SUMS`
覆盖实际 payload 但不覆盖自身，`Core-{CORE_VERSION}/CORE-SHA256SUMS` 只覆盖 source bundle 和 wheel。

## 这次更新的边界

- 这是离线 Core update：不联网、不从 source 构建、不安装 setuptools，也不下载或更新任何依赖。
- 不改写或重建 `runtime.json`、`components`、模型、ASR、Audalign、FFmpeg 或已有 `.roughcut` 状态。
- 不把本目录、source bundle 临时解压目录或 Host Package 当作 MCP 运行目录；MCP 使用既有安装根内
  的绝对 `venv/bin/roughcut-mcp`。
- 只允许使用下面已经由 `SHA256SUMS` 和 `CORE-SHA256SUMS` 核对过的本地 wheel；不要把 source
  bundle 交给 pip，也不要从 source bundle 构建 wheel。

## 唯一的 Core-only 升级步骤

以下是发行目录的可执行路径。把占位路径替换为实际路径；不要添加组件参数，不要把路径改成任何
未由用户明确指定的 sibling 目录：

```sh
set -eu
PACKAGE_ROOT="/absolute/path/to/{UPDATE_FOLDER_NAME}"
CORE_PACKAGE_DIR="$PACKAGE_ROOT/{CORE_DIRECTORY_NAME}"
INSTALL_DIR="/absolute/path/to/existing-roughcut-install"
WORKBUDDY_HOST_PACKAGE="/absolute/path/to/new-workbuddy-host-package"
CODEX_HOST_PACKAGE="/absolute/path/to/new-codex-host-package"

cd "$PACKAGE_ROOT"
shasum -a 256 -c "$PACKAGE_ROOT/{ROOT_CHECKSUMS_NAME}"
cd "$CORE_PACKAGE_DIR"
shasum -a 256 -c "$CORE_PACKAGE_DIR/{CORE_CHECKSUMS_NAME}"
CORE_WHEEL="$CORE_PACKAGE_DIR/{WHEEL_NAME}"
CORE_TMP="$(mktemp -d -t roughcut-core-{CORE_VERSION}-source)"
trap 'rm -rf -- "$CORE_TMP"' EXIT

tar -xzf "$CORE_PACKAGE_DIR/{SOURCE_BUNDLE_NAME}" -C "$CORE_TMP"
CORE_ROOT="$CORE_TMP/roughcut-core-{CORE_VERSION}"
MCP_COMMAND="$INSTALL_DIR/venv/bin/roughcut-mcp"

python3.11 "$CORE_ROOT/scripts/bootstrap.py" \\
  --install-dir "$INSTALL_DIR" \\
  --core-wheel "$CORE_WHEEL" \\
  --json

"$INSTALL_DIR/venv/bin/roughcut" health --json
"$INSTALL_DIR/venv/bin/roughcut" diagnostics --json

python3.11 "$CORE_ROOT/scripts/build_host_package.py" \\
  --host workbuddy \\
  --output "$WORKBUDDY_HOST_PACKAGE" \\
  --mcp-command "$MCP_COMMAND"

# 需要同时刷新 Codex 时执行；不需要时可跳过这一段。
python3.11 "$CORE_ROOT/scripts/build_host_package.py" \\
  --host codex \\
  --output "$CODEX_HOST_PACKAGE" \\
  --mcp-command "$MCP_COMMAND"
```

bootstrap 在任何 pip、runtime 或组件工作前会验证 wheel 是普通文件、文件名精确为
`{WHEEL_NAME}`，并验证 zip 内 METADATA 的 `Name: roughcut` 与 `Version: {CORE_VERSION}`。
校验失败归责于 Roughcut bootstrap 的 Core wheel validation，必须停止。pip 只能使用
`--no-index --disable-pip-version-check --no-deps`，更新使用 `--upgrade --force-reinstall`。
升级后必须从同一个安装根运行 health/diagnostics，并用同一个 `CORE_ROOT` 刷新 WorkBuddy Host Package；
安装后由用户按宿主信任流程 reload。命令结束时 trap 会删除临时解压目录。

## 缺少组件时停止，不猜路径

本目录没有 Large Components，因此不能完成 fresh install，也不能补齐缺失组件。若 health、
diagnostics 或用户确认显示现有安装缺少模型、ASR/Audalign runtime、组件或可用 FFmpeg，立即停止
Core-only 流程；不要联网、不要猜 sibling Large Components 路径、不要把本目录冒充完整安装包，也
不要自行调用 component plan/apply。只有在用户另行取得并明确指定一份独立的离线 Large Components
来源或完整的兼容组件绝对路径后，才能按那份来源自己的安装指引另行处理；本目录的升级步骤不能
替代该来源。

独立来源中的 `component-cache` 只是离线安装源，不是运行目录；模型和 Python/FunASR/Torch/
Audalign 的长期文件应由既有组件流程写入明确的 managed root。FFmpeg source pair 也不是运行目录：
用户另行批准时，必须先核对版本、架构、SHA-256 和许可证，在同一父目录 staging、复验后原子落位
到稳定、版本化的 external/read-only 目录；runtime 只绑定该稳定绝对路径，不能绑定 component-cache
或来源的 `bin`。

如果另行进行完整首次安装，source-detachment 只作为那份独立来源的收口原则：先临时移出来源而
不是直接删除，核对 runtime.json、component manifest 和配置没有来源绝对路径，再用新建且为空的
专用 cache 运行 Core health/diagnostics、full component health 和 release ASR smoke；失败时恢复
来源并清理临时 cache，只有全部通过后才能决定删除安装源。上述步骤不属于这次 Core-only update，
本目录不提供可直接执行的 sibling Large 路径命令。
"""

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file(path: Path, *, label: str) -> None:
    try:
        details = os.lstat(path)
    except OSError as error:
        raise ReleaseAssemblyError(f"{label} is missing: {path}") from error
    if not stat.S_ISREG(details.st_mode):
        raise ReleaseAssemblyError(f"{label} is not a regular file: {path}")


def _artifact(path: Path, relative: str) -> dict[str, object]:
    _regular_file(path, label="Core artifact")
    return {
        "path": relative,
        "sha256": _sha256(path),
        "size": path.stat().st_size,
    }


def _write_manifest(
    core_directory: Path, destination: Path, source_commit: str
) -> None:
    files = [
        _artifact(
            core_directory / name,
            f"{CORE_DIRECTORY_NAME}/{name}",
        )
        for name in CORE_PAYLOAD_NAMES
    ]
    manifest = {
        "architecture": "arm64",
        "core": {
            "checksums": f"{CORE_DIRECTORY_NAME}/{CORE_CHECKSUMS_NAME}",
            "directory": CORE_DIRECTORY_NAME,
            "files": files,
            "git_commit": source_commit,
            "offline_update": True,
            "version": CORE_VERSION,
        },
        "kind": "core-update-folder",
        "large_components": {
            "included": False,
            "independent_source_required": True,
            "rebuilt": False,
        },
        "offline_update": True,
        "platform": "macOS",
        "release_name": UPDATE_FOLDER_NAME,
        "release_schema": "roughcut.core-update-folder.v1",
        "root": {
            "checksums": ROOT_CHECKSUMS_NAME,
            "payload_files": list(ROOT_PAYLOAD_NAMES),
        },
        "schema_version": 1,
    }
    destination.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_root_checksums(root: Path) -> None:
    checksums = "".join(
        f"{_sha256(root / relative)}  {relative}\n" for relative in ROOT_PAYLOAD_NAMES
    )
    (root / ROOT_CHECKSUMS_NAME).write_text(checksums, encoding="utf-8")


def _read_checksums(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise ReleaseAssemblyError(
            f"checksum file is unreadable: {path.name}"
        ) from error
    entries: dict[str, str] = {}
    for line in lines:
        if not line or "  " not in line:
            raise ReleaseAssemblyError(f"invalid checksum line in {path.name}")
        digest, relative = line.split("  ", 1)
        if not _SHA256_RE.fullmatch(digest) or not relative or relative in entries:
            raise ReleaseAssemblyError(f"invalid checksum entry in {path.name}")
        entries[relative] = digest
    return entries


def _validate_core_directory(core_directory: Path) -> None:
    if not core_directory.is_dir():
        raise ReleaseAssemblyError("assembled Core directory is missing")
    names = {path.name for path in core_directory.iterdir()}
    if names != set(CORE_PAYLOAD_NAMES):
        raise ReleaseAssemblyError(
            "assembled Core directory does not contain exactly the four Core files"
        )
    for name in CORE_PAYLOAD_NAMES:
        _regular_file(core_directory / name, label="assembled Core file")
    entries = _read_checksums(core_directory / CORE_CHECKSUMS_NAME)
    expected = {SOURCE_BUNDLE_NAME, WHEEL_NAME}
    if set(entries) != expected:
        raise ReleaseAssemblyError(
            "CORE-SHA256SUMS does not cover exactly the source and wheel"
        )
    for relative, expected_digest in entries.items():
        if _sha256(core_directory / relative) != expected_digest:
            raise ReleaseAssemblyError(f"Core checksum mismatch: {relative}")


def _validate_root(root: Path, source_commit: str) -> None:
    expected_names = {
        CORE_DIRECTORY_NAME,
        INSTALL_GUIDE_NAME,
        ROOT_CHECKSUMS_NAME,
        MANIFEST_NAME,
    }
    if {path.name for path in root.iterdir()} != expected_names:
        raise ReleaseAssemblyError(
            "assembled update root does not contain exactly four entries"
        )
    _validate_core_directory(root / CORE_DIRECTORY_NAME)
    _regular_file(root / INSTALL_GUIDE_NAME, label="root install guide")
    _regular_file(root / MANIFEST_NAME, label="release manifest")
    install_guide = (root / INSTALL_GUIDE_NAME).read_text(encoding="utf-8")
    forbidden_guide_fragments = (
        "Large-Components-macOS-arm64-r1",
        "../",
        "--managed-root",
        "--component-cache",
    )
    if any(fragment in install_guide for fragment in forbidden_guide_fragments):
        raise ReleaseAssemblyError(
            "root install guide contains a guessed Large path or component command"
        )
    required_phrases = (
        "Core-only",
        "不联网",
        "不从 source 构建",
        "runtime.json",
        "component-cache",
        "source-detachment",
        "WorkBuddy Host Package",
        "缺少组件时停止",
    )
    if any(phrase not in install_guide for phrase in required_phrases):
        raise ReleaseAssemblyError(
            "root install guide is missing a Core-only safety instruction"
        )

    try:
        manifest = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReleaseAssemblyError("release manifest is not valid JSON") from error
    if not isinstance(manifest, dict):
        raise ReleaseAssemblyError("release manifest must be an object")
    core = manifest.get("core")
    large_components = manifest.get("large_components")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("release_schema") != "roughcut.core-update-folder.v1"
        or manifest.get("kind") != "core-update-folder"
        or manifest.get("offline_update") is not True
        or manifest.get("release_name") != UPDATE_FOLDER_NAME
        or manifest.get("platform") != "macOS"
        or manifest.get("architecture") != "arm64"
        or not isinstance(core, dict)
        or core.get("version") != CORE_VERSION
        or core.get("directory") != CORE_DIRECTORY_NAME
        or core.get("git_commit") != source_commit
        or core.get("offline_update") is not True
        or large_components
        != {
            "included": False,
            "independent_source_required": True,
            "rebuilt": False,
        }
    ):
        raise ReleaseAssemblyError(
            "release manifest identity or Core-only facts are invalid"
        )
    if core.get("checksums") != f"{CORE_DIRECTORY_NAME}/{CORE_CHECKSUMS_NAME}":
        raise ReleaseAssemblyError(
            "release manifest points to the wrong Core checksum file"
        )
    manifest_files = core.get("files")
    if not isinstance(manifest_files, list):
        raise ReleaseAssemblyError("release manifest Core files must be a list")
    expected_core_paths = {
        f"{CORE_DIRECTORY_NAME}/{name}" for name in CORE_PAYLOAD_NAMES
    }
    actual_core_paths = {
        item.get("path") for item in manifest_files if isinstance(item, dict)
    }
    if actual_core_paths != expected_core_paths or len(manifest_files) != len(
        expected_core_paths
    ):
        raise ReleaseAssemblyError(
            "release manifest Core file list is incomplete or duplicated"
        )
    for item in manifest_files:
        if not isinstance(item, dict):
            raise ReleaseAssemblyError("release manifest Core file entry is invalid")
        relative = item.get("path")
        if not isinstance(relative, str) or relative not in expected_core_paths:
            raise ReleaseAssemblyError(
                "release manifest contains an unexpected Core path"
            )
        artifact = root / relative
        if item.get("size") != artifact.stat().st_size or item.get("sha256") != _sha256(
            artifact
        ):
            raise ReleaseAssemblyError(f"release manifest hash mismatch: {relative}")
    root_info = manifest.get("root")
    if (
        not isinstance(root_info, dict)
        or root_info.get("checksums") != ROOT_CHECKSUMS_NAME
        or root_info.get("payload_files") != list(ROOT_PAYLOAD_NAMES)
    ):
        raise ReleaseAssemblyError("release manifest root payload list is invalid")

    entries = _read_checksums(root / ROOT_CHECKSUMS_NAME)
    if set(entries) != set(ROOT_PAYLOAD_NAMES) or ROOT_CHECKSUMS_NAME in entries:
        raise ReleaseAssemblyError("SHA256SUMS does not cover exactly the root payload")
    for relative, expected_digest in entries.items():
        if _sha256(root / relative) != expected_digest:
            raise ReleaseAssemblyError(f"root checksum mismatch: {relative}")


def _destination_exists(path: Path) -> bool:
    return os.path.lexists(path)


def assemble(core_source: Path, output: Path, *, source_commit: str) -> Path:
    """Build a complete Core update folder and publish it only after validation."""

    if not _COMMIT_RE.fullmatch(source_commit):
        raise ReleaseAssemblyError(
            "source_commit must be a 40-character lowercase Git SHA"
        )
    raw_output = Path(output)
    if _destination_exists(raw_output) and raw_output.is_symlink():
        raise ReleaseAssemblyError(f"output must not be a symlink: {raw_output}")
    output = raw_output.absolute()
    if _destination_exists(output):
        if not output.is_dir():
            raise ReleaseAssemblyError(f"output is not a directory: {output}")
        if any(output.iterdir()):
            raise ReleaseAssemblyError(f"output directory must be empty: {output}")
    core_source = Path(core_source).resolve()
    if not core_source.is_dir():
        raise ReleaseAssemblyError(f"Core source directory is missing: {core_source}")

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent)
    )
    try:
        core_directory = staging / CORE_DIRECTORY_NAME
        assemble_core(core_source, core_directory, source_commit=source_commit)
        (staging / INSTALL_GUIDE_NAME).write_text(_INSTALL_AGENT, encoding="utf-8")
        _write_manifest(
            staging / CORE_DIRECTORY_NAME, staging / MANIFEST_NAME, source_commit
        )
        _write_root_checksums(staging)
        _validate_root(staging, source_commit)

        if _destination_exists(output):
            if not output.is_dir() or any(output.iterdir()):
                raise ReleaseAssemblyError(
                    f"output directory became non-empty: {output}"
                )
            output.rmdir()
        os.rename(staging, output)
        return output
    except ReleaseAssemblyError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReleaseAssemblyError(f"Core update assembly failed: {error}") from error
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-source", type=Path, default=DEFAULT_CORE_SOURCE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    try:
        assemble(args.core_source, args.output, source_commit=args.source_commit)
    except ReleaseAssemblyError as error:
        print(f"Roughcut Core update assembly failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
