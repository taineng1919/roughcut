"""Build the frozen Core 0.2.7 and macOS arm64 py311 trial archives."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, cast

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
FROZEN_COMMIT = "ea5eddf8ec5a836b36e11bcad15cbb2dda764c02"
CORE_ARCHIVE = "roughcut-macos-arm64-core-0.2.7.tar.gz"
LARGE_ARCHIVE = "roughcut-macos-arm64-large-components-py311-v1.tar.gz"
CORE_ROOT = "roughcut-macos-arm64-core-0.2.7"
LARGE_ROOT = "Large-Components-macos-arm64-py311-v1"
FFMPEG_BUILD = "9.0-martin-riedl-arm64"
FFMPEG_SHA = "f54ec33409c78f54564c80afa16213b0970065100a87f4129516be0c8660c493"
FFPROBE_SHA = "f7142685d6e692ac22fde47facf8c078ce5333512e3ccfa4b83225d0561ad428"
EXPECTED_CORE_ARCHIVE_SHA = "35089dbbe9e4e65df952be309745775b9b5bd6485bf734574f6462b242662ea4"
OUTPUT_FILES = (
    CORE_ARCHIVE,
    LARGE_ARCHIVE,
    "INSTALL-AGENT.md",
    "TRIAL-MANIFEST.json",
    "KNOWN-ISSUES.md",
    "PACKAGING-REPORT.md",
)


class TrialDistributionError(RuntimeError):
    pass


def select_trial_python(
    *, install_root: Path | None, candidates: list[Path], runner: Any = subprocess.run
) -> tuple[Path | None, str | None]:
    """Select the only interpreter allowed before any mutating install action."""
    if install_root is not None:
        existing_python = install_root / "venv/bin/python"
        if not existing_python.is_file():
            return None, "UNSUPPORTED_EXISTING_TRIAL_PYTHON"
        candidates = [existing_python]
        failure = "UNSUPPORTED_EXISTING_TRIAL_PYTHON"
    else:
        candidates = [candidate for candidate in candidates if candidate.name == "python3.11"]
        failure = "PYTHON_311_REQUIRED"
    probe = "import json,platform,sys;print(json.dumps({'system':platform.system(),'sys_version':sys.version,'version':list(sys.version_info[:2]),'machine':platform.machine()}))"
    for candidate in candidates:
        try:
            result = runner([str(candidate), "-I", "-c", probe], capture_output=True, text=True)
        except OSError:
            continue
        if result.returncode:
            continue
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            continue
        if (
            payload.get("system") == "Darwin"
            and isinstance(payload.get("sys_version"), str)
            and payload.get("version") == [3, 11]
            and payload.get("machine") == "arm64"
        ):
            return candidate, None
    return None, failure


def preflight_then_mutate(
    *, install_root: Path | None, candidates: list[Path], mutation: Any, runner: Any = subprocess.run
) -> tuple[Path | None, str | None]:
    selected, failure = select_trial_python(install_root=install_root, candidates=candidates, runner=runner)
    if selected is not None:
        mutation(selected)
    return selected, failure


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def regular(path: Path, label: str) -> None:
    try:
        mode = os.lstat(path).st_mode
    except OSError as error:
        raise TrialDistributionError(f"{label} is missing") from error
    if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
        raise TrialDistributionError(f"{label} must be a regular file")


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def deterministic_tar(source: Path, archive: Path) -> None:
    with (
        archive.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped,
        tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as out,
    ):
        for path in [source, *sorted(source.rglob("*"))]:
            if path.is_symlink():
                raise TrialDistributionError("distribution payload contains a symlink")
            relative = path.relative_to(source.parent).as_posix()
            info = out.gettarinfo(str(path), arcname=relative)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            info.mode = 0o755 if path.is_dir() or os.access(path, os.X_OK) else 0o644
            if path.is_file():
                with path.open("rb") as stream:
                    out.addfile(info, stream)
            else:
                out.addfile(info)


def checksum_tree(root: Path, destination: Path) -> None:
    lines = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path != destination:
            lines.append(f"{sha256(path)}  {path.relative_to(root).as_posix()}\n")
    destination.write_text("".join(lines))


def export_frozen_source(destination: Path) -> Path:
    if subprocess.run(
        ["git", "cat-file", "-e", f"{FROZEN_COMMIT}^{{commit}}"], cwd=ROOT, check=False
    ).returncode:
        raise TrialDistributionError("FROZEN_CORE_COMMIT_UNAVAILABLE")
    process = subprocess.Popen(
        ["git", "archive", "--format=tar", FROZEN_COMMIT], cwd=ROOT, stdout=subprocess.PIPE
    )
    assert process.stdout is not None
    with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
        for member in archive:
            parts = PurePosixPath(member.name).parts
            if member.islnk() or member.issym() or ".." in parts:
                raise TrialDistributionError("frozen source archive is unsafe")
            archive.extract(member, destination)
    if process.wait() != 0:
        raise TrialDistributionError("git archive failed")
    return destination


def frozen_release_assembler(checkout: Path) -> Any:
    """Load the release assembler that ships inside the frozen checkout.

    These archives describe the frozen Core `0.2.7` identity, so their wheel and
    source-bundle names must come from that checkout's own release builder.  The
    mutable builder in the current worktree assembles a later Core identity and
    rejects the frozen wheel metadata, so importing it here would silently bind
    the frozen archives to whatever Core version the worktree currently carries.
    """

    module_path = checkout / "scripts" / "build_core_release.py"
    if not module_path.is_file():
        raise TrialDistributionError("frozen release builder is missing")
    spec = importlib.util.spec_from_file_location(
        "roughcut_frozen_build_core_release", module_path
    )
    if spec is None or spec.loader is None:
        raise TrialDistributionError("frozen release builder is unreadable")
    module = importlib.util.module_from_spec(spec)
    search_path = list(sys.path)
    try:
        spec.loader.exec_module(module)
    finally:
        # The frozen builder supports direct execution and may prepend its own
        # root to sys.path; keep that side effect out of this process.
        sys.path[:] = search_path
    return module


def build_core(stage: Path) -> Path:
    checkout = export_frozen_source(stage / "frozen-source")
    unit = stage / CORE_ROOT / "Core-0.2.7"
    unit.parent.mkdir(parents=True)
    frozen_release_assembler(checkout).assemble(checkout / "core", unit)
    core_files = []
    for path in sorted(unit.iterdir()):
        if path.is_file():
            core_files.append({"path": f"Core-0.2.7/{path.name}", "size": path.stat().st_size, "sha256": sha256(path)})
    write_json(stage / CORE_ROOT / "release-manifest.json", {
        "schema_version": 1, "kind": "roughcut-core-trial", "source_commit": FROZEN_COMMIT,
        "core": {"version": "0.2.7", "dependencies": [], "python": ">=3.11", "wheel_tag": "py3-none-any", "files": core_files},
        "large_components_included": False,
    })
    (stage / CORE_ROOT / "INSTALL-AGENT.md").write_text(
        "# Roughcut Core 0.2.7\n\nUse the top-level Trial `INSTALL-AGENT.md`. This archive is an independent frozen Core unit.\n"
    )
    checksum_tree(stage / CORE_ROOT, stage / CORE_ROOT / "SHA256SUMS")
    return stage / CORE_ROOT


def audit_core(core_root: Path) -> dict[str, object]:
    unit = core_root / "Core-0.2.7"
    wheel = unit / "roughcut-0.2.7-py3-none-any.whl"
    source = unit / "roughcut-core-0.2.7-source.tar.gz"
    groups = {name: 0 for name in ("core_source", "review_static", "catalog_metadata", "agent_skill", "host_integrations", "scripts", "docs_release_notes", "other")}
    with tarfile.open(source, "r:gz") as archive:
        names = [member.name for member in archive if member.isfile()]
        for member in archive.getmembers():
            if not member.isfile():
                continue
            path = "/".join(member.name.split("/")[1:])
            if path.startswith("core/src/roughcut/review/static/"):
                group = "review_static"
            elif path.startswith("core/src/roughcut/component_catalog/"):
                group = "catalog_metadata"
            elif path.startswith("core/src/") or path == "core/pyproject.toml":
                group = "core_source"
            elif path.startswith("agent-skill/"):
                group = "agent_skill"
            elif path.startswith("host-integrations/"):
                group = "host_integrations"
            elif path.startswith("scripts/"):
                group = "scripts"
            elif path.startswith("docs/") or path == "README.md":
                group = "docs_release_notes"
            else:
                group = "other"
            groups[group] += member.size
    offenders = []
    for name in names:
        lower = name.lower()
        base = PurePosixPath(lower).name
        if "component-cache/" in lower or any(f"/models/{model}/" in lower for model in ("model_asr", "model_vad", "model_punc", "model_spk")):
            offenders.append(name)
        if base in {"ffmpeg", "ffprobe"}:
            offenders.append(name)
        if base.endswith(".whl") and base.startswith(("funasr-", "torch-", "torchaudio-", "audalign-")):
            offenders.append(name)
    if offenders:
        raise TrialDistributionError("FAIL_CORE_COMPONENT_SEPARATION")
    return {"wheel_bytes": wheel.stat().st_size, "source_bundle_bytes": source.stat().st_size, "breakdown": groups, "uncompressed_total": sum(groups.values()), "separation": {"funasr_wheel": False, "torch_wheel": False, "torchaudio_wheel": False, "four_models": False, "audalign_dependency_wheels": False, "ffmpeg_binary": False, "component_cache": False}}


def _catalog_file(catalog: Path, relative: str, expected_sha: str | None = None) -> Path:
    path = catalog / relative
    regular(path, f"catalog reference {relative}")
    if expected_sha is not None and sha256(path) != expected_sha:
        raise TrialDistributionError(f"catalog reference hash mismatch: {relative}")
    return path


def _catalog_reference(catalog: Path, relative: str, expected_sha: str | None = None) -> dict[str, object]:
    return cast(dict[str, object], json.loads(_catalog_file(catalog, relative, expected_sha).read_text()))


def load_closure(catalog: Path) -> tuple[list[dict[str, object]], dict[str, object]]:
    release = _catalog_reference(catalog, "release-catalog.json")
    profiles = [p for p in cast(list[dict[str, object]], release["profiles"]) if p.get("id") == "macos-arm64-py311"]
    if len(profiles) != 1:
        raise TrialDistributionError("catalog must contain exactly one macos-arm64-py311 profile")
    profile = profiles[0]
    if (profile.get("platform"), profile.get("architecture"), profile.get("python_version")) != ("macos", "arm64", "3.11"):
        raise TrialDistributionError("trial catalog profile target mismatch")
    runtime_profile = cast(dict[str, object], profile["runtime"])
    versions = runtime_profile["versions"]
    if versions != {"funasr": "1.3.14", "torch": "2.6.0", "torchaudio": "2.6.0"}:
        raise TrialDistributionError("trial runtime versions drifted")
    audalign_profile = cast(dict[str, object], profile["audalign"])
    runtime_ref = cast(str, runtime_profile["artifacts"])
    models_ref = cast(str, profile["models"])
    audalign_ref = cast(str, audalign_profile["artifacts"])
    runtime = _catalog_reference(catalog, runtime_ref)
    models = _catalog_reference(catalog, models_ref)
    audalign = _catalog_reference(catalog, audalign_ref)
    _catalog_file(catalog, cast(str, runtime_profile["dependency_lock"]), cast(str, runtime_profile["dependency_lock_sha256"]))
    _catalog_file(catalog, cast(str, audalign_profile["dependency_lock"]), cast(str, audalign_profile["dependency_lock_sha256"]))
    _catalog_file(catalog, cast(str, audalign_profile["license_notice"]), cast(str, audalign_profile["license_notice_sha256"]))
    records: list[dict[str, object]] = []
    for component, payload in (("python_runtime", runtime), ("audalign", audalign)):
        for raw in cast(list[dict[str, object]], payload["artifacts"]):
            item = dict(raw)
            item.update(component=component, source=item["url"], cache_destination=f"artifacts/{str(item['sha256'])[:2]}/{item['sha256']}/{item['filename']}")
            records.append(item)
    model_bytes = 0
    for model in cast(list[dict[str, object]], models["models"]):
        for item in cast(list[dict[str, object]], model["files"]):
            model_bytes += cast(int, item["size"])
            model_path = cast(str, item["path"])
            model_sha = cast(str, item["sha256"])
            records.append({
                "component": model["name"], "name": model["name"], "version": model["revision"],
                "revision": model["revision"], "filename": Path(model_path).name,
                "model_path": item["path"], "size": item["size"], "sha256": item["sha256"],
                "source": item["url"], "license": model["license"],
                "cache_destination": f"artifacts/{model_sha[:2]}/{model_sha}/{Path(model_path).name}",
            })
    unique = {str(item["sha256"]): item for item in records}
    return records, {
        "logical": len(records), "unique": len(unique), "unique_bytes": sum(cast(int, x["size"]) for x in unique.values()), "model_bytes": model_bytes,
        "profile": {"id": profile["id"], "platform": profile["platform"], "architecture": profile["architecture"], "python_version": profile["python_version"], "runtime_artifacts": runtime_ref, "runtime_lock": runtime_profile["dependency_lock"], "models": models_ref, "audalign_artifacts": audalign_ref, "audalign_lock": audalign_profile["dependency_lock"], "audalign_license_notice": audalign_profile["license_notice"], "versions": versions},
        "runtime_estimated_installed_bytes": runtime_profile["estimated_installed_bytes"],
        "audalign_estimated_installed_bytes": audalign_profile["estimated_installed_bytes"],
    }


def build_large(stage: Path, source: Path) -> tuple[Path, dict[str, object]]:
    catalog = stage / "frozen-source/core/src/roughcut/component_catalog"
    root = stage / LARGE_ROOT
    cache = root / "component-cache"
    records, counts = load_closure(catalog)
    copied: set[str] = set()
    for record in records:
        digest, filename = str(record["sha256"]), str(record["filename"])
        if digest in copied:
            continue
        origin = source / "component-cache/artifacts" / digest[:2] / digest / filename
        regular(origin, f"component artifact {filename}")
        if origin.stat().st_size != record["size"] or sha256(origin) != digest:
            raise TrialDistributionError(f"component artifact mismatch: {filename}")
        target = cache / str(record["cache_destination"])
        target.parent.mkdir(parents=True, exist_ok=True)
        # The verified cache is immutable input. A hard link avoids a second 2.5 GB
        # staging copy on APFS; archive bytes are still read and verified normally.
        try:
            os.link(origin, target)
        except OSError:
            shutil.copyfile(origin, target)
        receipt = origin.with_name(origin.name + ".receipt.json")
        if receipt.exists():
            shutil.copyfile(receipt, target.with_name(target.name + ".receipt.json"))
        else:
            write_json(target.with_name(target.name + ".receipt.json"), {"schema_version": 1, "sha256": digest, "size": record["size"]})
        copied.add(digest)
    profile = cast(dict[str, object], counts["profile"])
    for relative in ("release-catalog.json", profile["models"], profile["runtime_artifacts"], profile["audalign_artifacts"]):
        relative = cast(str, relative)
        target = root / "catalog" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(catalog / relative, target)
    for relative in (cast(str, profile["runtime_lock"]), cast(str, profile["audalign_lock"])):
        target = root / "locks" / Path(relative).name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(catalog / relative, target)
    licenses = root / "licenses"
    licenses.mkdir(parents=True)
    shutil.copyfile(catalog / cast(str, profile["audalign_license_notice"]), licenses / "audalign-licenses.json")
    for name in ("FFMPEG-SOURCE.md", "GPL-3.0.txt", "MODEL-NOTICES.md", "Apache-2.0.txt"):
        regular(source / "licenses" / name, f"license {name}")
        shutil.copyfile(source / "licenses" / name, licenses / name)
    external = root / "external-tools/ffmpeg" / FFMPEG_BUILD
    (external / "bin").mkdir(parents=True)
    (external / "licenses").mkdir()
    for name, expected in (("ffmpeg", FFMPEG_SHA), ("ffprobe", FFPROBE_SHA)):
        origin = source / "bin" / name
        regular(origin, name)
        if sha256(origin) != expected:
            raise TrialDistributionError("FFMPEG_TRIAL_ARTIFACT_NOT_READY")
        shutil.copyfile(origin, external / "bin" / name)
        (external / "bin" / name).chmod(0o755)
    for name in ("FFMPEG-SOURCE.md", "GPL-3.0.txt"):
        shutil.copyfile(source / "licenses" / name, external / "licenses" / name)
    write_json(external / "identity.json", {"schema_version": 1, "build": FFMPEG_BUILD, "architecture": "arm64", "version": "9.0", "ffmpeg_sha256": FFMPEG_SHA, "ffprobe_sha256": FFPROBE_SHA})
    ffmpeg_payload_bytes = sum((external / "bin" / name).stat().st_size for name in ("ffmpeg", "ffprobe"))
    managed_estimated = cast(int, counts["runtime_estimated_installed_bytes"]) + cast(int, counts["audalign_estimated_installed_bytes"]) + cast(int, counts["model_bytes"])
    write_json(root / "manifest.json", {
        "schema_version": 1, "identity": "macos-arm64-py311-v1", "python_profile": "3.11",
        "source_commit": FROZEN_COMMIT, "offline_install_source": True, "runtime_binding_preseeded": False,
        "bbc_audio_offset_finder_runtime_included": False, "artifacts": records, "counts": counts,
        "managed_estimated_installed_bytes": managed_estimated,
        "ffmpeg": {"build": FFMPEG_BUILD, "ffmpeg_sha256": FFMPEG_SHA, "ffprobe_sha256": FFPROBE_SHA, "payload_and_stable_copy_bytes": ffmpeg_payload_bytes},
        "total_estimated_persistent_bytes": managed_estimated + ffmpeg_payload_bytes,
    })
    checksum_tree(root, root / "SHA256SUMS")
    return root, counts


INSTALL_AGENT = f"""# Roughcut macOS arm64 Trial Distribution v1

This top-level guide is trial orchestration metadata outside the frozen Core archive. The Core archive remains independently copyable. Classify the action before mutation. Never execute the nested frozen `CORE-UPGRADE-AGENT.md` PATH-based Python example; this guide controls interpreter selection.

## UPGRADE — independent Core-only entry

This entry requires only `{CORE_ARCHIVE}`. It does not require, read, verify, or unpack Large Components or other top-level files. Verify the Core archive directly:

```sh
test "$(shasum -a 256 "$CORE_ARCHIVE_PATH" | awk '{{print $1}}')" = "{EXPECTED_CORE_ARCHIVE_SHA}"
```

Resolve actual `INSTALL_ROOT` from current Host MCP command, existing executable, and runtime binding. Set `TRIAL_PYTHON="$INSTALL_ROOT/venv/bin/python"`; if that exact file is absent, stop before mutation with `UNSUPPORTED_EXISTING_TRIAL_PYTHON`. Probe Darwin, full `sys.version`, exact 3.11, and native arm64:

```sh
"$TRIAL_PYTHON" -I -c 'import json,platform,sys; print(json.dumps({{"system":platform.system(),"sys_version":sys.version,"version":list(sys.version_info[:2]),"machine":platform.machine()}})); assert platform.system()=="Darwin" and sys.version_info[:2]==(3,11) and platform.machine()=="arm64"'
```

Safely unpack only the Core archive to a new directory, rejecting absolute paths, `..`, symlinks, hardlinks, and non-file/non-directory members; then verify both internal checksum layers:

```sh
"$TRIAL_PYTHON" - "$CORE_ARCHIVE_PATH" "$CORE_UNPACK_ROOT" <<'PY'
import pathlib, sys, tarfile
source, destination = map(pathlib.Path, sys.argv[1:])
destination.mkdir(parents=True, exist_ok=False)
with tarfile.open(source, "r:gz") as archive:
    members = archive.getmembers()
    if any(pathlib.PurePosixPath(m.name).is_absolute() or ".." in pathlib.PurePosixPath(m.name).parts or m.issym() or m.islnk() or not (m.isfile() or m.isdir()) for m in members):
        raise SystemExit("UNSAFE_TRIAL_ARCHIVE")
    archive.extractall(destination, members=members)
PY
cd "$CORE_PACKAGE_ROOT" && shasum -a 256 -c SHA256SUMS
cd "$CORE_PACKAGE_ROOT/Core-0.2.7" && shasum -a 256 -c CORE-SHA256SUMS
```

Snapshot runtime/component files and component trees. Run Core bootstrap with no component arguments, then installed CLI health/diagnostics:

```sh
"$TRIAL_PYTHON" "$CORE_SOURCE_ROOT/scripts/bootstrap.py" --install-dir "$INSTALL_ROOT" --core-wheel "$CORE_WHEEL" --json
"$INSTALL_ROOT/venv/bin/roughcut" health --json
"$INSTALL_ROOT/venv/bin/roughcut" diagnostics --json
"$TRIAL_PYTHON" "$CORE_SOURCE_ROOT/scripts/build_host_package.py" --host "$HOST" --output "$HOST_PACKAGE" --mcp-command "$INSTALL_ROOT/venv/bin/roughcut-mcp"
```

Byte-compare the snapshots, refresh canonical Skills, perform the actual Host reload/restart, and require CLI/Host MCP health and diagnostics to match. If components are incomplete, report `LARGE_COMPONENTS_REQUIRED` and stop. Existing `production_ready=true` needs no Large archive.

## FRESH / COMPONENT-INSTALL — Large entry

This separate entry requires `{LARGE_ARCHIVE}`; FRESH also requires the Core archive. First perform the read-only archive hash verification from `TRIAL-MANIFEST.json` or the exact frozen values: Core `{EXPECTED_CORE_ARCHIVE_SHA}`, Large `1221397b167f959cb02580efe8bc84c176d24a2c0f766532885e93623a5f2f20`. Do not create an unpack/staging directory yet.

Next complete preflight before any mutation. For FRESH, probe an explicit or known `python3.11` candidate; bare `python3`, 3.12, and 3.13 are not substitutes. The isolated probe outputs `system`, `sys_version`, `version`, and `machine`; only after it reports Darwin, full `sys.version`, exact 3.11, and arm64 may it become `TRIAL_PYTHON`. Otherwise return `PYTHON_311_REQUIRED` with zero mutation:

```sh
CANDIDATE="/absolute/path/to/python3.11"
"$CANDIDATE" -I -c 'import json,platform,sys; print(json.dumps({{"system":platform.system(),"sys_version":sys.version,"version":list(sys.version_info[:2]),"machine":platform.machine()}})); assert platform.system()=="Darwin" and sys.version_info[:2]==(3,11) and platform.machine()=="arm64"'
TRIAL_PYTHON="$CANDIDATE"
INSTALL_ROOT="$HOME/.roughcut"
MANAGED_ROOT="$HOME/.roughcut/managed"
```

For existing component repair, first resolve the actual install/custom managed roots, set exact `TRIAL_PYTHON="$INSTALL_ROOT/venv/bin/python"`, require that exact file, and run the same `-I` Darwin/full `sys.version`/3.11/arm64 probe. Any failure returns `UNSUPPORTED_EXISTING_TRIAL_PYTHON` with zero mutation; never fall back to PATH, Conda, or another Python:

```sh
TRIAL_PYTHON="$INSTALL_ROOT/venv/bin/python"
test -f "$TRIAL_PYTHON" || exit 1  # report UNSUPPORTED_EXISTING_TRIAL_PYTHON
"$TRIAL_PYTHON" -I -c 'import json,platform,sys; print(json.dumps({{"system":platform.system(),"sys_version":sys.version,"version":list(sys.version_info[:2]),"machine":platform.machine()}})); assert platform.system()=="Darwin" and sys.version_info[:2]==(3,11) and platform.machine()=="arm64"' || exit 1
```

Only after the applicable preflight succeeds may the Agent safely unpack each required archive using the same absolute/`..`/symlink/hardlink/non-regular-member rejection above, create staging, prepare FFmpeg, bootstrap Core, or run PLAN/APPLY.

Existing installs first read bound ffmpeg/ffprobe from runtime/manifest/diagnostics. Healthy bindings are `REUSE`: do not copy, migrate, replace, or stage FFmpeg. Only when existing FFmpeg is missing and the user separately supplies and approves Large v1 may component repair prepare its frozen pair at an explicit Roughcut-owned stable versioned path. FRESH prepares the pair at `$HOME/.roughcut/external-tools/ffmpeg/9.0-martin-riedl-arm64`. PLAN/APPLY always receive the final exact stable/bound paths.

FRESH bootstraps Core first. PLAN/APPLY/health use exact actual roots and `TRIAL_PYTHON`:

```sh
"$TRIAL_PYTHON" "$CORE_SOURCE_ROOT/scripts/bootstrap.py" --install-dir "$INSTALL_ROOT" --core-wheel "$CORE_WHEEL" --json
"$TRIAL_PYTHON" "$CORE_SOURCE_ROOT/scripts/bootstrap.py" --install-dir "$INSTALL_ROOT" --managed-root "$MANAGED_ROOT" --component-cache "$LARGE_ROOT/component-cache" --ffmpeg-command "$FFMPEG" --ffprobe-command "$FFPROBE" --target-platform macos --target-architecture arm64 --verify-components --include-audalign --json
"$TRIAL_PYTHON" "$CORE_SOURCE_ROOT/scripts/bootstrap.py" --install-dir "$INSTALL_ROOT" --managed-root "$MANAGED_ROOT" --component-cache "$LARGE_ROOT/component-cache" --ffmpeg-command "$FFMPEG" --ffprobe-command "$FFPROBE" --target-platform macos --target-architecture arm64 --verify-components --include-audalign --apply-components --approved-plan-hash "$PLAN_HASH" --operation-id "$OPERATION_ID" --json
"$TRIAL_PYTHON" "$CORE_SOURCE_ROOT/scripts/bootstrap.py" --install-dir "$INSTALL_ROOT" --managed-root "$MANAGED_ROOT" --component-cache "$EMPTY_CACHE" --ffmpeg-command "$FFMPEG" --ffprobe-command "$FFPROBE" --target-platform macos --target-architecture arm64 --verify-components --include-audalign --component-health --json
"$TRIAL_PYTHON" "$CORE_SOURCE_ROOT/scripts/release_asr_smoke.py" --runtime-binding "$INSTALL_ROOT/runtime.json" --json
"$TRIAL_PYTHON" "$CORE_SOURCE_ROOT/scripts/build_host_package.py" --host "$HOST" --output "$HOST_PACKAGE" --mcp-command "$INSTALL_ROOT/venv/bin/roughcut-mcp"
```

The plan must show `download_bytes=0`; APPLY uses exact returned `media_components.plan_hash` and top-level `installation_operation.operation_id`. Existing repair installs missing groups only. Detach Large, use a new empty cache, run installed CLI health/diagnostics and full health above, and require Core/runtime/models/Audalign/FFmpeg identities, `production_ready=true`, no persistent distribution dependency, canonical Skills, actual Host reload/restart, and CLI/Host MCP MATCH. Never `cp -R` or access the network.
"""

KNOWN_ISSUES = """# Known Issues

1. 初稿生成后 Agent 可能不主动打开 Review。Workaround：打开当前初稿审阅页面。
2. duration contract 仍可能让流程采用 `under_target`。Workaround：读取当前初稿实际时长；如果低于目标继续调整，不要确认。
"""


def assemble(source: Path, output: Path, *, deterministic_reference: Path | None = None) -> dict[str, object]:
    if output.exists() and any(output.iterdir()):
        raise TrialDistributionError("output must not exist or must be empty")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="roughcut-trial-") as temporary:
        stage = Path(temporary)
        core_root = build_core(stage)
        core_audit = audit_core(core_root)
        large_root, counts = build_large(stage, source)
        deterministic_tar(core_root, output / CORE_ARCHIVE)
        deterministic_tar(large_root, output / LARGE_ARCHIVE)
    if sha256(output / CORE_ARCHIVE) != EXPECTED_CORE_ARCHIVE_SHA:
        raise TrialDistributionError("FROZEN_CORE_ARCHIVE_IDENTITY_MISMATCH")
    deterministic_status = "NOT_VERIFIED"
    if deterministic_reference is not None:
        for name in (CORE_ARCHIVE, LARGE_ARCHIVE):
            reference = deterministic_reference / name
            regular(reference, f"deterministic reference {name}")
            if reference.stat().st_size != (output / name).stat().st_size or sha256(reference) != sha256(output / name):
                raise TrialDistributionError(f"deterministic mismatch: {name}")
        deterministic_status = "PASS"
    (output / "INSTALL-AGENT.md").write_text(INSTALL_AGENT)
    (output / "KNOWN-ISSUES.md").write_text(KNOWN_ISSUES)
    manifest = {
        "schema_version": 1, "platform": "macos", "architecture": "arm64",
        "core": {"version": "0.2.7", "source_commit": FROZEN_COMMIT, "archive": CORE_ARCHIVE, "sha256": sha256(output / CORE_ARCHIVE), "size": (output / CORE_ARCHIVE).stat().st_size},
        "large_components": {"version": "macos-arm64-py311-v1", "python_profile": "3.11", "archive": LARGE_ARCHIVE, "sha256": sha256(output / LARGE_ARCHIVE), "size": (output / LARGE_ARCHIVE).stat().st_size},
        "fresh_defaults": {"install_root": "~/.roughcut", "managed_root": "~/.roughcut/managed"},
    }
    write_json(output / "TRIAL-MANIFEST.json", manifest)
    managed_bytes = cast(int, counts["runtime_estimated_installed_bytes"]) + cast(int, counts["audalign_estimated_installed_bytes"]) + cast(int, counts["model_bytes"])
    ffmpeg_bytes = sum((source / "bin" / name).stat().st_size for name in ("ffmpeg", "ffprobe"))
    breakdown = cast(dict[str, int], core_audit["breakdown"])
    report = f"""# Packaging Report

- Frozen source commit: `{FROZEN_COMMIT}`.
- Core wheel/source compressed bytes: {core_audit['wheel_bytes']} / {core_audit['source_bundle_bytes']}.
- Core source breakdown bytes: core source excluding Review static/catalog={breakdown['core_source']}; Review static={breakdown['review_static']}; catalog metadata={breakdown['catalog_metadata']}; agent-skill={breakdown['agent_skill']}; host-integrations={breakdown['host_integrations']}; scripts={breakdown['scripts']}; docs/release notes={breakdown['docs_release_notes']}; other={breakdown['other']}; total={core_audit['uncompressed_total']}.
- Core archive bytes/SHA: {(output / CORE_ARCHIVE).stat().st_size} / `{sha256(output / CORE_ARCHIVE)}`.
- Core separation: PASS; no FunASR/Torch/TorchAudio wheel, four model payloads, Audalign dependency wheels, FFmpeg binary, or component cache. Details: `{json.dumps(core_audit['separation'], sort_keys=True)}`.
- Large logical/unique/unique payload bytes: {counts['logical']} / {counts['unique']} / {counts['unique_bytes']}.
- Large archive bytes/SHA: {(output / LARGE_ARCHIVE).stat().st_size} / `{sha256(output / LARGE_ARCHIVE)}`.
- Runtime/model/Audalign estimated bytes: {counts['runtime_estimated_installed_bytes']} / {counts['model_bytes']} / {counts['audalign_estimated_installed_bytes']}; managed estimated installed bytes={managed_bytes}.
- FFmpeg READY: payload/stable-copy bytes={ffmpeg_bytes}; ffmpeg SHA `{FFMPEG_SHA}`; ffprobe SHA `{FFPROBE_SHA}`. Total estimated persistent bytes={managed_bytes + ffmpeg_bytes}.
- Python policy: Core remains `>=3.11`, `dependencies=[]`, `py3-none-any` because no evidence proves Core 3.12/3.13 incompatible. Trial components require native arm64 3.11 because the catalog is py311, contains cp311 artifacts, APPLY enforces `(3,11)`, and managed Python/Audalign receipts bind 3.11.
- Fresh paths: install `~/.roughcut`; managed `~/.roughcut/managed`; distribution cache is detachable and never persistent.
- Fresh/existing tests: synthetic packaging preflight plus canonical missing-only/offline and byte-preservation fixtures; not Machine A/B.
- Deterministic double build: {deterministic_status}; compared both archive size and SHA against an independent builder output.
- Cleanup: builder staging is removed; no real media, process, or port used.
- External unverified gates: Machine A existing upgrade/Host MCP and Machine B clean fresh/Host MCP.
- Final verdict: `MACOS_ARM64_TRIAL_DISTRIBUTION_FAIL_EXTERNAL_MACHINE_ACCEPTANCE_NOT_RUN`.
"""
    (output / "PACKAGING-REPORT.md").write_text(report)
    checksum_tree(output, output / "SHA256SUMS")
    return manifest


def refresh_metadata(reference: Path, output: Path) -> dict[str, object]:
    """Regenerate top-level metadata without rebuilding verified frozen archives."""
    if output.exists() and any(output.iterdir()):
        raise TrialDistributionError("output must not exist or must be empty")
    output.mkdir(parents=True, exist_ok=True)
    manifest = cast(dict[str, object], json.loads((reference / "TRIAL-MANIFEST.json").read_text()))
    for section, name in (("core", CORE_ARCHIVE), ("large_components", LARGE_ARCHIVE)):
        record = cast(dict[str, object], manifest[section])
        source = reference / name
        regular(source, f"metadata reference {name}")
        if source.stat().st_size != record["size"] or sha256(source) != record["sha256"]:
            raise TrialDistributionError(f"metadata reference mismatch: {name}")
        os.link(source, output / name)
    if cast(dict[str, object], manifest["core"])["sha256"] != EXPECTED_CORE_ARCHIVE_SHA:
        raise TrialDistributionError("FROZEN_CORE_ARCHIVE_IDENTITY_MISMATCH")
    report = (reference / "PACKAGING-REPORT.md").read_text()
    if "Deterministic double build: PASS" not in report or "must be filled" in report:
        raise TrialDistributionError("metadata reference report is not final")
    (output / "INSTALL-AGENT.md").write_text(INSTALL_AGENT)
    shutil.copyfile(reference / "KNOWN-ISSUES.md", output / "KNOWN-ISSUES.md")
    write_json(output / "TRIAL-MANIFEST.json", manifest)
    (output / "PACKAGING-REPORT.md").write_text(report)
    checksum_tree(output, output / "SHA256SUMS")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--component-source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--deterministic-reference", type=Path)
    parser.add_argument("--metadata-reference", type=Path)
    args = parser.parse_args()
    if args.metadata_reference:
        if args.component_source or args.deterministic_reference:
            parser.error("--metadata-reference cannot be combined with build inputs")
        manifest = refresh_metadata(args.metadata_reference.resolve(), args.output.resolve())
    else:
        if not args.component_source:
            parser.error("--component-source is required for a full build")
        manifest = assemble(args.component_source.resolve(), args.output.resolve(), deterministic_reference=args.deterministic_reference.resolve() if args.deterministic_reference else None)
    print(json.dumps({"ok": True, "manifest": manifest}, sort_keys=True))


if __name__ == "__main__":
    main()
