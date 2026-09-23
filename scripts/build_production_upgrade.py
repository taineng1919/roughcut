"""Assemble an offline Roughcut Core plus verified Audalign production upgrade."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import tempfile
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core" / "src"))

from roughcut.adapters.component_installation import (
    AUDALIGN_GROUP_NAME,
    AlignmentGroupSpec,
    cache_artifact_path,
    cache_receipt_path,
    load_release_catalog,
)

from scripts.build_core_update import (
    CORE_DIRECTORY_NAME,
    CORE_VERSION,
    SOURCE_BUNDLE_NAME,
    WHEEL_NAME,
)
from scripts.build_core_update import (
    assemble as assemble_core_update,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORE_SOURCE = ROOT / "core"
PRODUCTION_FOLDER_PREFIX = f"roughcut-macos-arm64-{CORE_VERSION}-production-upgrade"
MANIFEST_NAME = "production-upgrade-manifest.json"
ROOT_CHECKSUMS_NAME = "SHA256SUMS"
GUIDE_NAME = "PRODUCTION-UPGRADE-AGENT.md"
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class ProductionUpgradeError(RuntimeError):
    """Raised when a production-upgrade bundle is not safe to publish."""


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
        raise ProductionUpgradeError(f"{label} is missing") from error
    if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise ProductionUpgradeError(f"{label} is not a regular file")


def _file_record(root: Path, path: Path) -> dict[str, object]:
    _regular_file(path, label=f"bundle file {path.name}")
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": _sha256(path),
        "size": path.stat().st_size,
    }


def _copy_catalog(destination: Path, catalog_root: Path) -> list[dict[str, object]]:
    if not catalog_root.is_dir() or catalog_root.is_symlink():
        raise ProductionUpgradeError("component catalog source is unavailable")
    records: list[dict[str, object]] = []
    for source in sorted(catalog_root.rglob("*")):
        if not source.is_file():
            if source.is_symlink():
                raise ProductionUpgradeError("component catalog contains a link")
            continue
        relative = source.relative_to(catalog_root)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        _regular_file(source, label="component catalog file")
        shutil.copy2(source, target)
        records.append(_file_record(destination.parent, target))
    if not records:
        raise ProductionUpgradeError("component catalog is empty")
    return records


def _validate_cache(
    source_cache: Path,
    target_root: Path,
    bundle_root: Path,
    *,
    group: AlignmentGroupSpec,
) -> list[dict[str, object]]:
    if source_cache.is_symlink() or not source_cache.is_dir():
        raise ProductionUpgradeError("Audalign component-cache-source must be a directory")
    if not (source_cache / "artifacts").is_dir():
        raise ProductionUpgradeError(
            "Audalign component-cache-source must be the exact cache root "
            "containing artifacts"
        )
    artifacts = group.artifacts
    if not artifacts:
        raise ProductionUpgradeError("Audalign catalog artifact closure is empty")

    records: list[dict[str, object]] = []
    for artifact in artifacts:
        source = cache_artifact_path(source_cache, artifact)
        receipt = cache_receipt_path(source)
        _regular_file(source, label=f"Audalign cache artifact {artifact.filename}")
        _regular_file(receipt, label=f"Audalign cache receipt {artifact.filename}")
        if source.stat().st_size != artifact.size or _sha256(source) != artifact.sha256:
            raise ProductionUpgradeError(
                f"Audalign cache artifact does not match catalog: {artifact.filename}"
            )
        try:
            receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ProductionUpgradeError(
                f"Audalign cache receipt is unreadable: {artifact.filename}"
            ) from error
        if receipt_payload != {
            "schema_version": 1,
            "sha256": artifact.sha256,
            "size": artifact.size,
        }:
            raise ProductionUpgradeError(
                f"Audalign cache receipt does not match catalog: {artifact.filename}"
            )

        target = cache_artifact_path(target_root, artifact)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        target_receipt = cache_receipt_path(target)
        shutil.copy2(receipt, target_receipt)
        records.append(
            {
                "name": artifact.name,
                "version": artifact.version,
                "filename": artifact.filename,
                "artifact": _file_record(bundle_root, target),
                "receipt": _file_record(bundle_root, target_receipt),
            }
        )
    return records


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _payload_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ProductionUpgradeError("bundle contains a symbolic link")
        if path.is_file():
            files.append(path)
    return files


def _write_checksums(root: Path) -> None:
    entries = [
        f"{_sha256(path)}  {path.relative_to(root).as_posix()}\n"
        for path in _payload_files(root)
        if path.name != ROOT_CHECKSUMS_NAME
    ]
    (root / ROOT_CHECKSUMS_NAME).write_text("".join(entries), encoding="utf-8")


def _archive(root: Path, archive_path: Path) -> None:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    # Deterministic archive: sorted members, normalized mtime/uid/gid/uname/gname/mode, pax_headers empty
    # Mirrors build_core_release's deterministic tar logic for reproducible SHA256
    def _reset_tarinfo(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo:
        tarinfo.mtime = 0
        tarinfo.uid = 0
        tarinfo.gid = 0
        tarinfo.uname = ""
        tarinfo.gname = ""
        tarinfo.pax_headers = {}
        # Normalize mode to 0o755 for dirs, 0o644 for files (keep executable bits for scripts if needed, but be deterministic)
        if tarinfo.isdir():
            tarinfo.mode = 0o755
        else:
            # Preserve executable bit deterministically: if original mode had any exec bit, set 0o755 else 0o644, but normalize
            # For production upgrade, all files are regular files with 0o644 except maybe scripts, but we normalize to 0o644 for files
            tarinfo.mode = 0o644 if tarinfo.mode & 0o111 == 0 else 0o755
        return tarinfo

    with archive_path.open("wb") as raw, gzip.GzipFile(
        fileobj=raw, mode="wb", mtime=0
    ) as compressed, tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as stream:
        # Add root directory itself first
        root_info = tarfile.TarInfo(root.name)
        root_info.type = tarfile.DIRTYPE
        root_info = _reset_tarinfo(root_info)
        stream.addfile(root_info)
        # Walk in sorted order for determinism
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ProductionUpgradeError("bundle contains a symbolic link")
            arcname = f"{root.name}/{path.relative_to(root).as_posix()}"
            tarinfo = stream.gettarinfo(str(path), arcname=arcname)
            tarinfo = _reset_tarinfo(tarinfo)
            if tarinfo.isreg():
                with path.open("rb") as f:
                    stream.addfile(tarinfo, f)
            else:
                stream.addfile(tarinfo)


def _guide() -> str:
    return f"""# Roughcut production upgrade (offline, macOS arm64 / Python 3.11)

This directory is a production-upgrade payload, not a first-install bundle and not a
media, project, or runtime-state backup. It contains one current Core update and one
verified Audalign `audalign==1.3.1` component-cache closure. It does not contain
a prebuilt `runtime.json`. The target keeps its existing FunASR, model, FFmpeg, and
other external identities unless the canonical component plan says they are reusable.

## Verify and unpack

Run these commands on the target host with the exact transferred directory. Do not
replace a path with a guessed sibling or another user's home directory.

```sh
set -eu
PACKAGE_ROOT="/absolute/path/to/{PRODUCTION_FOLDER_PREFIX}"
INSTALL_DIR="/absolute/path/to/existing-roughcut-install"
MANAGED_ROOT="/absolute/path/to/existing-managed-root"
EXTERNAL_COMPONENT_MANIFEST="/absolute/path/to/existing-external-component-manifest.json"
EXTERNAL_FUNASR_PYTHON="/absolute/path/to/existing-funasr-python"
FFMPEG_COMMAND="/absolute/path/to/existing-ffmpeg"
FFPROBE_COMMAND="/absolute/path/to/existing-ffprobe"
WORKBUDDY_HOST_PACKAGE="/absolute/path/to/refreshed-workbuddy-host-package"

cd "$PACKAGE_ROOT"
shasum -a 256 -c "$PACKAGE_ROOT/SHA256SUMS"
CORE_PACKAGE_DIR="$PACKAGE_ROOT/core-update/{CORE_DIRECTORY_NAME}"
cd "$CORE_PACKAGE_DIR"
shasum -a 256 -c "$CORE_PACKAGE_DIR/CORE-SHA256SUMS"
cd "$PACKAGE_ROOT"

UPGRADE_TMP="$(mktemp -d -t roughcut-production-upgrade-{CORE_VERSION})"
trap 'rm -rf -- "$UPGRADE_TMP"' EXIT
tar -xzf "$CORE_PACKAGE_DIR/{SOURCE_BUNDLE_NAME}" -C "$UPGRADE_TMP"
CORE_ROOT="$UPGRADE_TMP/roughcut-core-{CORE_VERSION}"
CORE_WHEEL="$CORE_PACKAGE_DIR/{WHEEL_NAME}"

python3.11 "$CORE_ROOT/scripts/bootstrap.py" \\
  --install-dir "$INSTALL_DIR" \\
  --core-wheel "$CORE_WHEEL" \\
  --json
"$INSTALL_DIR/venv/bin/roughcut" health --json
"$INSTALL_DIR/venv/bin/roughcut" diagnostics --json
```

The Core upgrade is the existing offline bootstrap path. Its wheel install must use
the local wheel only (`--no-index --no-deps`); no PyPI, proxy, online pip fallback,
source build, or runtime-file edit is allowed. Stop if the exact current install root,
Core identity, or existing component identities cannot be read.

## Audalign plan, exact approval, and apply

The values below are placeholders. Resolve them from the current install's persistent
component manifest/runtime binding and keep the same exact values for both plan and
apply. Do not use the package source cache as a managed root and do not copy a
prebuilt runtime binding.

```sh
set -eu
PLAN_JSON="$UPGRADE_TMP/component-plan.json"
python3.11 "$CORE_ROOT/scripts/bootstrap.py" \\
  --install-dir "$INSTALL_DIR" \\
  --managed-root "$MANAGED_ROOT" \\
  --external-components "$EXTERNAL_COMPONENT_MANIFEST" \\
  --external-funasr-python "$EXTERNAL_FUNASR_PYTHON" \\
  --ffmpeg-command "$FFMPEG_COMMAND" \\
  --ffprobe-command "$FFPROBE_COMMAND" \\
  --component-cache "$PACKAGE_ROOT/component-cache/audalign" \\
  --verify-components \\
  --include-audalign \\
  --json > "$PLAN_JSON"

PLAN_HASH="$(
python3.11 - "$PLAN_JSON" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    plan = json.load(handle)
print(plan["media_components"]["plan_hash"])
PY
)"
OPERATION_ID="$(
python3.11 - "$PLAN_JSON" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    plan = json.load(handle)
print(plan["installation_operation"]["operation_id"])
PY
)"

python3.11 "$CORE_ROOT/scripts/bootstrap.py" \\
  --install-dir "$INSTALL_DIR" \\
  --managed-root "$MANAGED_ROOT" \\
  --external-components "$EXTERNAL_COMPONENT_MANIFEST" \\
  --external-funasr-python "$EXTERNAL_FUNASR_PYTHON" \\
  --ffmpeg-command "$FFMPEG_COMMAND" \\
  --ffprobe-command "$FFPROBE_COMMAND" \\
  --component-cache "$PACKAGE_ROOT/component-cache/audalign" \\
  --verify-components \\
  --include-audalign \\
  --apply-components \\
  --approved-plan-hash "$PLAN_HASH" \\
  --operation-id "$OPERATION_ID" \\
  --json

python3.11 "$CORE_ROOT/scripts/bootstrap.py" \\
  --install-dir "$INSTALL_DIR" \\
  --managed-root "$MANAGED_ROOT" \\
  --external-components "$EXTERNAL_COMPONENT_MANIFEST" \\
  --external-funasr-python "$EXTERNAL_FUNASR_PYTHON" \\
  --ffmpeg-command "$FFMPEG_COMMAND" \\
  --ffprobe-command "$FFPROBE_COMMAND" \\
  --component-cache "$PACKAGE_ROOT/component-cache/audalign" \\
  --verify-components \\
  --include-audalign \\
  --component-health \\
  --json
"$INSTALL_DIR/venv/bin/roughcut" diagnostics --json
```

The plan hash and operation id are read from the one approved plan; apply must use
that exact pair. A plan that is missing a verified artifact, changes an existing
identity, requests network access, or cannot publish a normalized Audalign binding is a
failed upgrade and must stop. The resulting diagnostics must show
`alignment.status=available`, `configured_provider=audalign`,
`configured_provider_version=1.3.1`, and `production_ready=true`. The overall health
boolean is not a substitute for this production gate.

## Host package refresh and stale-host gate

Parse the actual configured Host MCP command and its absolute `INSTALL_ROOT` from the
current host configuration; do not infer it from the current shell home. Refresh the
Host Package with the existing builder and exact command:

```sh
python3.11 "$CORE_ROOT/scripts/build_host_package.py" \\
  --host workbuddy \\
  --output "$WORKBUDDY_HOST_PACKAGE" \\
  --mcp-command "$INSTALL_DIR/venv/bin/roughcut-mcp"
```

The host must reload/retrust this package. Then read health and diagnostics from the
actual Host MCP process, not only from the CLI. Confirm the same Core version/schema,
the same absolute install root, the same runtime/component identities and
`alignment.production_ready=true`. CLI health passing while Host MCP still serves
an older Core is a stale-host failure; stop before any other operation. Do not reuse
evidence from a different user's home or a different install root.

This delivery is upgrade-only. It contains no user Project, transcript, source media,
ASR run, alignment run, or render procedure; those are outside this package contract.
"""


def assemble(
    core_source: Path,
    component_cache_source: Path,
    output: Path,
    *,
    source_commit: str,
) -> tuple[Path, Path]:
    """Build and validate a complete offline production-upgrade folder/archive."""

    if COMMIT_RE.fullmatch(source_commit) is None:
        raise ProductionUpgradeError(
            "source_commit must be a 40-character lowercase Git SHA"
        )
    output = Path(output).absolute()
    archive_path = output.with_name(f"{output.name}.tar.gz")
    if output.exists() or archive_path.exists():
        raise ProductionUpgradeError("output folder or archive already exists")
    core_source = Path(core_source).resolve()
    if not core_source.is_dir():
        raise ProductionUpgradeError("Core source directory is missing")
    source_cache = Path(component_cache_source)
    if not source_cache.is_absolute():
        raise ProductionUpgradeError("component-cache-source must be absolute")
    catalog_root = core_source / "src" / "roughcut" / "component_catalog"

    catalog = load_release_catalog(catalog_root)
    profile = catalog.profile_for("macos", "arm64")
    group = profile.alignment_group(AUDALIGN_GROUP_NAME)
    if group is None or group.provider != "audalign":
        raise ProductionUpgradeError("Audalign 1.3.1 catalog group is unavailable")
    if group.version != "1.3.1" or group.direct_distribution != "audalign":
        raise ProductionUpgradeError(
            "Audalign catalog identity is not audalign 1.3.1"
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent)
    )
    try:
        core_update = staging / "core-update"
        assemble_core_update(core_source, core_update, source_commit=source_commit)

        cache_root = staging / "component-cache" / "audalign"
        cache_records = _validate_cache(
            source_cache,
            cache_root,
            staging,
            group=group,
        )
        catalog_records = _copy_catalog(staging / "component-catalog", catalog_root)

        core_manifest_path = core_update / "release-manifest.json"
        _regular_file(core_manifest_path, label="Core update manifest")
        core_manifest = json.loads(core_manifest_path.read_text(encoding="utf-8"))
        core_directory = core_update / CORE_DIRECTORY_NAME
        core_wheel = core_directory / WHEEL_NAME
        core_source_bundle = core_directory / SOURCE_BUNDLE_NAME
        core_records = {
            "release_manifest": _file_record(staging, core_manifest_path),
            "wheel": _file_record(staging, core_wheel),
            "source_bundle": _file_record(staging, core_source_bundle),
        }
        core_identity = (
            core_manifest.get("core") if isinstance(core_manifest, dict) else None
        )
        if (
            not isinstance(core_identity, dict)
            or core_identity.get("git_commit") != source_commit
        ):
            raise ProductionUpgradeError("Core update manifest source identity differs")

        guide_path = staging / GUIDE_NAME
        guide_path.write_text(_guide(), encoding="utf-8")
        manifest = {
            "schema_version": 1,
            "release_schema": "roughcut.production-upgrade.v1",
            "kind": "roughcut-production-upgrade-bundle",
            "package_kind": "production-upgrade",
            "offline": True,
            "platform": "macos",
            "architecture": "arm64",
            "python_version": "3.11",
            "source_commit": source_commit,
            "core_dependencies": [],
            "core_update": {
                "path": "core-update",
                "manifest": core_records["release_manifest"],
                "manifest_schema": core_manifest.get("release_schema"),
                "version": CORE_VERSION,
                "source_commit": source_commit,
                "wheel": core_records["wheel"],
                "source_bundle": core_records["source_bundle"],
            },
            "component_catalog": {
                "path": "component-catalog",
                "catalog_version": catalog.version,
                "catalog_digest": catalog.digest,
                "files": catalog_records,
            },
            "audalign_component_cache": {
                "path": "component-cache/audalign",
                "provider": group.provider,
                "version": group.version,
                "direct_distribution": group.direct_distribution,
                "artifact_count": len(group.artifacts),
                "cache_bytes": sum(artifact.size for artifact in group.artifacts),
                "expected_download_bytes_on_target": 0,
                "dependency_lock": {
                    "path": (
                        "component-catalog/"
                        f"{group.dependency_lock.relative_to(catalog_root).as_posix()}"
                    ),
                    "sha256": group.dependency_lock_sha256,
                    "origin": group.dependency_lock_origin,
                },
                "license_notice": {
                    "path": (
                        "component-catalog/"
                        f"{group.license_notice_file.relative_to(catalog_root).as_posix()}"
                    ),
                    "sha256": group.license_notice_sha256,
                },
                "artifacts": cache_records,
            },
            "target_contract": {
                "reuses_existing": [
                    "FunASR",
                    "models",
                    "FFmpeg",
                    "ffprobe",
                    "install_root",
                ],
                "runtime_binding_preseeded": False,
                "apply_path": "existing bootstrap component plan/apply",
                "no_network": True,
            },
            "guide": GUIDE_NAME,
            "root_checksums": ROOT_CHECKSUMS_NAME,
        }
        _write_json(staging / MANIFEST_NAME, manifest)
        _write_checksums(staging)

        payload = _payload_files(staging)
        if not payload or (staging / ROOT_CHECKSUMS_NAME) not in payload:
            raise ProductionUpgradeError("production-upgrade payload is incomplete")
        forbidden = {"runtime.json", "Project", "media", "Gate8"}
        names = {path.name for path in payload}
        if names & forbidden:
            raise ProductionUpgradeError(
                "production-upgrade contains forbidden payload"
            )

        os.rename(staging, output)
        _archive(output, archive_path)
        return output, archive_path
    except ProductionUpgradeError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, tarfile.TarError) as error:
        raise ProductionUpgradeError(
            f"production-upgrade assembly failed: {error}"
        ) from error
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-source", type=Path, default=DEFAULT_CORE_SOURCE)
    parser.add_argument("--component-cache-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    try:
        assemble(
            args.core_source,
            args.component_cache_source,
            args.output,
            source_commit=args.source_commit,
        )
    except ProductionUpgradeError as error:
        print(f"Roughcut production upgrade assembly failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
