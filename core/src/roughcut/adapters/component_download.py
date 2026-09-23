"""Checksum-addressed downloads for the pinned Roughcut component catalog."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import secrets
import shutil
import stat
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from roughcut.adapters.component_installation import (
    CACHE_RECEIPT_TEMP_PREFIX,
    CACHE_RECEIPT_TEMP_SUFFIX,
    OWNED_TEMP_CREATE_ATTEMPTS,
    OWNED_TEMP_TOKEN_BYTES,
    ArtifactSpec,
    ComponentInstallError,
    _available_bytes,
    _nearest_existing_parent,
    artifact_source_is_allowed,
    cache_artifact_path,
    cache_receipt_path,
)

CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+|\*)")
DOWNLOAD_ATTEMPTS = 3


class ComponentDownloadError(ComponentInstallError):
    """Typed download boundary; only transport/short-read instances retry."""

    failure_responsibility = "python_https_runtime"
    failure_action = "download_component_artifact"

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class ComponentArtifactDownloadError(ComponentDownloadError):
    """Typed artifact size, checksum, publish, and receipt boundary."""

    failure_responsibility = "component_artifact"
    failure_action = "verify_component_artifact"


class ComponentDownloadWriteError(ComponentInstallError):
    """Typed local cache write boundary; never retry a disk failure."""

    failure_responsibility = "roughcut_component_installer"
    failure_action = "install_components"


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    reused: bool
    resumed: bool


def download_artifact(artifact: ArtifactSpec, cache_root: Path) -> DownloadResult:
    """Download one exact artifact, preserving a safe partial file for retry."""

    for attempt in range(DOWNLOAD_ATTEMPTS):
        try:
            return _download_artifact_once(artifact, cache_root)
        except ComponentDownloadError as error:
            if not error.retryable or attempt == DOWNLOAD_ATTEMPTS - 1:
                raise
            time.sleep(2**attempt)
    raise AssertionError("component download retry loop did not return")


def _download_artifact_once(
    artifact: ArtifactSpec, cache_root: Path
) -> DownloadResult:
    """Perform one resumable network attempt for an exact artifact."""

    destination = cache_artifact_path(cache_root.resolve(strict=False), artifact)
    receipt = cache_receipt_path(destination)
    _prepare_cache_destination(cache_root.resolve(strict=False), destination, receipt)
    if destination.is_file():
        if _verified_file(destination, artifact):
            _publish_receipt(receipt, artifact)
            return DownloadResult(destination, reused=True, resumed=False)
        destination.unlink()
        receipt.unlink(missing_ok=True)

    partial = destination.with_name(f"{destination.name}.part")
    offset = partial.stat().st_size if partial.is_file() else 0
    if offset > artifact.size:
        partial.unlink()
        offset = 0
    if offset == artifact.size:
        if _file_sha256(partial) == artifact.sha256:
            try:
                os.replace(partial, destination)
                _sync_directory(destination.parent)
                _publish_receipt(receipt, artifact)
            except OSError as error:
                raise ComponentArtifactDownloadError(
                    "verified cache artifact could not be published"
                ) from error
            return DownloadResult(destination, reused=False, resumed=True)
        partial.unlink()
        offset = 0
    resumed = offset > 0
    headers = {"User-Agent": "roughcut-component-bootstrap/1"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = urllib.request.Request(artifact.url, headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=60)
    except urllib.error.HTTPError as error:
        if error.code == 416 and partial.exists():
            partial.unlink()
        raise ComponentDownloadError(f"component download failed with HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise ComponentDownloadError(
            "component download was interrupted", retryable=True
        ) from error
    except OSError as error:
        raise ComponentDownloadError(
            "component download was interrupted", retryable=True
        ) from error
    try:
        with response:
            initial_is_loopback_http = artifact_source_is_allowed(
                artifact.url, allow_loopback_http=True
            ) and not artifact_source_is_allowed(
                artifact.url, allow_loopback_http=False
            )
            if not artifact_source_is_allowed(
                response.geturl(), allow_loopback_http=initial_is_loopback_http
            ):
                raise ComponentDownloadError(
                    "component download redirected to an unsafe source"
                )
            status = response.getcode()
            mode = "wb"
            if offset and status == 206:
                content_range = response.headers.get("Content-Range", "")
                match = CONTENT_RANGE.fullmatch(content_range)
                if (
                    match is None
                    or int(match.group(1)) != offset
                    or int(match.group(2)) != artifact.size - 1
                    or match.group(3) != str(artifact.size)
                ):
                    raise ComponentDownloadError("download server returned an invalid byte range")
                mode = "ab"
            elif offset and status == 200:
                offset = 0
                resumed = False
            elif status != 200:
                raise ComponentDownloadError(f"download server returned HTTP {status}")
            with partial.open(mode) as output:
                _copy_response(response, output)
                output.flush()
                os.fsync(output.fileno())
    except urllib.error.HTTPError as error:
        if error.code == 416 and partial.exists():
            partial.unlink()
        raise ComponentDownloadError(f"component download failed with HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise ComponentDownloadError(
            "component download was interrupted", retryable=True
        ) from error
    except OSError as error:
        raise ComponentDownloadWriteError("component download could not be written") from error

    actual_size = partial.stat().st_size
    if actual_size < artifact.size:
        raise ComponentDownloadError(
            "component download ended before the expected size", retryable=True
        )
    if actual_size > artifact.size:
        partial.unlink()
        raise ComponentArtifactDownloadError("component download exceeded the expected size")
    if _file_sha256(partial) != artifact.sha256:
        partial.unlink()
        raise ComponentArtifactDownloadError("component download checksum differs")
    try:
        os.replace(partial, destination)
        _sync_directory(destination.parent)
    except OSError as error:
        raise ComponentArtifactDownloadError(
            "verified cache artifact could not be published"
        ) from error
    _publish_receipt(receipt, artifact)
    return DownloadResult(destination, reused=False, resumed=resumed)


def _copy_response(response: Any, output: Any) -> None:
    while True:
        try:
            chunk = response.read(1024 * 1024)
        except http.client.IncompleteRead as error:
            if error.partial:
                output.write(error.partial)
            raise ComponentDownloadError(
                "component download was interrupted", retryable=True
            ) from error
        except OSError as error:
            raise ComponentDownloadError(
                "component download was interrupted", retryable=True
            ) from error
        if not chunk:
            return
        output.write(chunk)


def _verified_file(path: Path, artifact: ArtifactSpec) -> bool:
    return path.stat().st_size == artifact.size and _file_sha256(path) == artifact.sha256


def _prepare_cache_destination(
    cache_root: Path, destination: Path, receipt: Path
) -> None:
    cache_root.mkdir(parents=True, exist_ok=True)
    if cache_root.is_symlink() or not cache_root.is_dir():
        raise ComponentInstallError("component cache root is unsafe")
    current = cache_root
    for part in destination.parent.relative_to(cache_root).parts:
        current = current / part
        if os.path.lexists(current):
            if current.is_symlink() or not current.is_dir():
                raise ComponentInstallError("component cache path is unsafe")
        else:
            current.mkdir()
    partial = destination.with_name(f"{destination.name}.part")
    if any(path.is_symlink() for path in (destination, receipt, partial)):
        raise ComponentInstallError("component cache artifact path is unsafe")


def _write_receipt(path: Path, artifact: ArtifactSpec) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary, descriptor = _open_receipt_temporary(path.parent)
    payload = {
        "schema_version": 1,
        "sha256": artifact.sha256,
        "size": artifact.size,
    }
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _open_receipt_temporary(parent: Path) -> tuple[Path, int]:
    for _attempt in range(OWNED_TEMP_CREATE_ATTEMPTS):
        temporary = parent / (
            CACHE_RECEIPT_TEMP_PREFIX
            + secrets.token_hex(OWNED_TEMP_TOKEN_BYTES)
            + CACHE_RECEIPT_TEMP_SUFFIX
        )
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            continue
        return temporary, descriptor
    raise FileExistsError("component cache receipt temporary identity was unavailable")


def _publish_receipt(path: Path, artifact: ArtifactSpec) -> None:
    try:
        _write_receipt(path, artifact)
    except OSError as error:
        raise ComponentArtifactDownloadError(
            "verified cache receipt could not be published"
        ) from error


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


POPULATE_SOURCE_TIERS = ("local", "aliyun", "modelscope", "canonical", "agent-fallback")


@dataclass(frozen=True)
class PopulateResult:
    path: Path
    reused: bool
    provenance: dict[str, object]


def populate_cache_from_local_file(
    artifact: ArtifactSpec,
    source_path: Path,
    source_tier: str,
    cache_root: Path,
) -> PopulateResult:
    """Publish one exact catalog artifact from an already-downloaded local file.

    Scheme B cache-population boundary: the caller (Agent/installer) fetches
    the exact frozen bytes through an allowed channel (verified local, Aliyun
    PyPI simple index, ModelScope.cn, canonical, or trusted Agent fallback)
    and hands the local file to the core for verification. The core enforces
    the frozen identity (exact size + SHA-256 against the catalog
    ``ArtifactSpec``), runs the cache disk-space preflight before any cache
    mutation, publishes through the existing cache layout/receipt
    path, and returns a sanitized provenance record. Credentials, URLs, and
    source paths are never persisted: the receipt stays
    ``{schema_version, sha256, size}``.
    """

    if source_tier not in POPULATE_SOURCE_TIERS:
        raise ComponentInstallError("populate source tier is not closed")
    if not source_path.is_absolute():
        raise ComponentInstallError("populate source path must be absolute")
    try:
        details = os.lstat(source_path)
    except OSError as error:
        raise ComponentInstallError(
            f"populate source file is missing: {source_path}"
        ) from error
    if (
        not stat.S_ISREG(details.st_mode)
        or stat.S_ISLNK(details.st_mode)
        or details.st_nlink != 1
    ):
        raise ComponentInstallError("populate source file is unsafe")
    if details.st_size != artifact.size:
        raise ComponentArtifactDownloadError(
            "populate source size differs from the catalog artifact"
        )
    if _file_sha256(source_path) != artifact.sha256:
        raise ComponentArtifactDownloadError(
            "populate source checksum differs from the catalog artifact"
        )
    resolved_cache = cache_root.resolve(strict=False)
    if _available_bytes(_nearest_existing_parent(resolved_cache)) < artifact.size:
        raise ComponentInstallError("insufficient disk space for cache population")
    destination = cache_artifact_path(resolved_cache, artifact)
    receipt = cache_receipt_path(destination)
    _prepare_cache_destination(resolved_cache, destination, receipt)
    if destination.is_file() and _verified_file(destination, artifact):
        _publish_receipt(receipt, artifact)
        reused = True
    else:
        if destination.is_file():
            destination.unlink()
            receipt.unlink(missing_ok=True)
        partial = destination.with_name(f"{destination.name}.part")
        if partial.is_symlink():
            raise ComponentInstallError("component cache artifact path is unsafe")
        if partial.is_file():
            partial.unlink()
        try:
            with source_path.open("rb") as intake, partial.open("wb") as output:
                shutil.copyfileobj(intake, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            if not _verified_file(partial, artifact):
                partial.unlink(missing_ok=True)
                raise ComponentArtifactDownloadError(
                    "populated cache artifact failed verification"
                )
            os.replace(partial, destination)
            _sync_directory(destination.parent)
        except OSError as error:
            raise ComponentDownloadWriteError(
                "populated cache artifact could not be published"
            ) from error
        _publish_receipt(receipt, artifact)
        reused = False
    provenance: dict[str, object] = {
        "tier": source_tier,
        "filename": artifact.filename,
        "sha256": artifact.sha256,
        "size": artifact.size,
        "reused": reused,
    }
    return PopulateResult(destination, reused, provenance)


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
