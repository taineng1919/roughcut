"""Create and read immutable verified source proxies without mutating Project."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from roughcut.adapters.ffmpeg.proxy import (
    FFmpegProxyError,
    ProxyVerificationReport,
    reject_unsupported_color,
    resolve_proxy_ffprobe,
    resolve_proxy_tools,
    transcode_proxy,
    verify_proxy_output,
)
from roughcut.adapters.project_store import ProjectStore
from roughcut.application.renders import _resolve_source_path
from roughcut.application.sources import fingerprint_file
from roughcut.domain.project import Project, ProjectError, SourceAsset
from roughcut.domain.proxy import (
    ProxyManifest,
    ProxyOutput,
    derive_proxy_profile,
    proxy_cache_key,
)
from roughcut.domain.render import ToolResolution

ProxyPhaseCallback = Callable[[str], None]


@dataclass(frozen=True)
class ProxyState:
    source_id: str
    cache_key: str
    status: str
    reason: str | None
    project_revision: int
    reused: bool = False
    proxy_relative_path: str | None = None
    manifest_relative_path: str | None = None
    summary: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "cache_key": self.cache_key,
            "status": self.status,
            "reason": self.reason,
            "project_revision": self.project_revision,
            "reused": self.reused,
            "proxy_relative_path": self.proxy_relative_path,
            "manifest_relative_path": self.manifest_relative_path,
            "summary": self.summary,
        }


@dataclass(frozen=True)
class ProxyResult:
    state: ProxyState

    @property
    def reused(self) -> bool:
        return self.state.reused

    def to_dict(self) -> dict[str, object]:
        return self.state.to_dict()


@dataclass(frozen=True)
class FrozenProxyPlayback:
    source_id: str
    cache_key: str
    output_size: int
    output_sha256_head_tail: str
    manifest_size: int
    manifest_sha256_head_tail: str
    profile_summary: dict[str, object]


def freeze_ready_proxy(
    project_path: Path,
    *,
    source_id: str,
    expected_revision: int,
) -> FrozenProxyPlayback | None:
    state = read_proxy(
        project_path,
        source_id=source_id,
        expected_revision=expected_revision,
    )
    if state.status != "ready":
        return None
    project_root = _validated_project_root(project_path)
    project = ProjectStore(project_root).load()
    source = _project_source(project, source_id)
    try:
        manifest = _load_ready_manifest(project_root, source, state.cache_key)
    except ProjectError:
        return None
    manifest_fingerprint = fingerprint_file(project_root / manifest.manifest_relative_path)
    return FrozenProxyPlayback(
        source_id=source_id,
        cache_key=manifest.cache_key,
        output_size=manifest.output.size,
        output_sha256_head_tail=manifest.output.sha256_head_tail,
        manifest_size=manifest_fingerprint.size,
        manifest_sha256_head_tail=manifest_fingerprint.sha256_head_tail,
        profile_summary=_proxy_profile_summary(manifest),
    )


def resolve_frozen_proxy_path(
    project_path: Path,
    frozen: FrozenProxyPlayback,
) -> Path:
    project_root = _validated_project_root(project_path)
    store = ProjectStore(project_root)
    project = store.load()
    source = _project_source(project, frozen.source_id)
    current_key = proxy_cache_key(
        source.fingerprint,
        source.probe,
        derive_proxy_profile(source.probe, project.settings),
    )
    if current_key != frozen.cache_key:
        raise ProjectError("frozen review proxy no longer matches its source")
    state = read_proxy(
        project_root,
        source_id=frozen.source_id,
        expected_revision=project.revision,
    )
    if state.status != "ready" or state.cache_key != frozen.cache_key:
        raise ProjectError("frozen review proxy is no longer ready")
    manifest = _load_ready_manifest(project_root, source, frozen.cache_key)
    manifest_fingerprint = fingerprint_file(project_root / manifest.manifest_relative_path)
    if (
        manifest_fingerprint.size != frozen.manifest_size
        or manifest_fingerprint.sha256_head_tail != frozen.manifest_sha256_head_tail
        or manifest.output.size != frozen.output_size
        or manifest.output.sha256_head_tail != frozen.output_sha256_head_tail
    ):
        raise ProjectError("frozen review proxy was replaced")
    output_path = project_root / manifest.output.relative_path
    if not _validate_cache_path(project_root, output_path, expected_type="file"):
        raise ProjectError("frozen review proxy output is missing")
    fingerprint = fingerprint_file(output_path)
    if (
        fingerprint.size != frozen.output_size
        or fingerprint.sha256_head_tail != frozen.output_sha256_head_tail
    ):
        raise ProjectError("frozen review proxy output was replaced")
    return output_path.resolve(strict=True)


def read_proxy(
    project_path: Path,
    *,
    source_id: str,
    expected_revision: int,
    ffprobe: ToolResolution | None = None,
) -> ProxyState:
    project_root = _validated_project_root(project_path)
    store = ProjectStore(project_root)
    project = store.load()
    if project.revision != expected_revision:
        raise ProjectError("project revision conflict")
    source = _project_source(project, source_id)
    profile = derive_proxy_profile(source.probe, project.settings)
    cache_key = proxy_cache_key(source.fingerprint, source.probe, profile)
    base = _state(project, source, cache_key, "missing", "no_ready_proxy")
    try:
        source_path = _resolve_source_path(store.project_path, source)
        if fingerprint_file(source_path) != source.fingerprint:
            return _state(project, source, cache_key, "stale", "source_fingerprint_changed")
    except (OSError, ProjectError):
        return _state(project, source, cache_key, "stale", "source_unavailable")

    proxy_root = project_root / "proxies"
    source_cache = proxy_root / source.source_id
    cache_directory = source_cache / cache_key
    manifest_path = cache_directory / "manifest.json"
    try:
        manifest_exists = _validate_cache_path(project_root, manifest_path, expected_type="file")
        stale_cache_exists = _has_other_manifest(project_root, source_cache, cache_key)
    except ProjectError:
        return _state(project, source, cache_key, "invalid", "cache_path_invalid")
    if not manifest_exists:
        if stale_cache_exists:
            return _state(project, source, cache_key, "stale", "cache_key_changed")
        return base
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ProjectError("proxy manifest must contain an object")
        manifest = ProxyManifest.from_dict(raw)
        if manifest.source_id != source.source_id or manifest.cache_key != cache_key:
            raise ProjectError("proxy manifest identity is stale")
        if (
            manifest.source_fingerprint != source.fingerprint
            or manifest.source_probe != source.probe
        ):
            return _state(project, source, cache_key, "stale", "source_snapshot_changed")
    except (OSError, json.JSONDecodeError, ProjectError, ValueError, TypeError):
        return _state(project, source, cache_key, "invalid", "manifest_invalid")

    output_path = project_root / manifest.output.relative_path
    try:
        if not _validate_cache_path(project_root, output_path, expected_type="file"):
            raise OSError("proxy output is outside the project")
        resolved_output = output_path.resolve(strict=True)
    except ProjectError as error:
        reason = "output_symlink" if "symlink" in str(error) else "output_missing"
        return _state(project, source, cache_key, "invalid", reason)
    except OSError:
        return _state(project, source, cache_key, "invalid", "output_missing")
    fingerprint = fingerprint_file(resolved_output)
    if (
        fingerprint.size != manifest.output.size
        or fingerprint.sha256_head_tail != manifest.output.sha256_head_tail
    ):
        return _state(project, source, cache_key, "invalid", "output_fingerprint_changed")
    try:
        selected_ffprobe = ffprobe or resolve_proxy_ffprobe()
        report = verify_proxy_output(
            resolved_output,
            source.probe,
            profile,
            selected_ffprobe,
            decode=False,
        )
    except (OSError, FFmpegProxyError):
        return _state(project, source, cache_key, "invalid", "output_invalid")
    return _ready_state(project, manifest, report, reused=True)


def create_proxy(
    project_path: Path,
    *,
    source_id: str,
    expected_revision: int,
    tools: tuple[ToolResolution, ToolResolution] | None = None,
    phase_callback: ProxyPhaseCallback | None = None,
) -> ProxyResult:
    project_root = _validated_project_root(project_path)
    store = ProjectStore(project_root)
    project = store.load()
    if project.revision != expected_revision:
        raise ProjectError("project revision conflict")
    source = _project_source(project, source_id)
    source_path = _resolve_source_path(store.project_path, source)
    if fingerprint_file(source_path) != source.fingerprint:
        raise ProjectError("proxy source fingerprint has changed")
    profile = derive_proxy_profile(source.probe, project.settings)
    cache_key = proxy_cache_key(source.fingerprint, source.probe, profile)
    existing = read_proxy(
        store.project_path,
        source_id=source.source_id,
        expected_revision=expected_revision,
        ffprobe=None if tools is None else tools[1],
    )
    if existing.status == "ready":
        return ProxyResult(existing)
    if existing.status == "invalid":
        if existing.reason == "cache_path_invalid":
            raise ProjectError("proxy cache path is invalid")
        raise ProjectError("existing proxy cache is invalid")

    ffmpeg, ffprobe = tools or resolve_proxy_tools()
    source_inspection = reject_unsupported_color(
        source_path,
        ffprobe,
        fallback_origin_ticks=source.probe.first_content_ticks,
    )
    proxy_root = project_root / "proxies"
    source_cache = proxy_root / source.source_id
    _ensure_cache_directory(project_root, proxy_root)
    _ensure_cache_directory(project_root, source_cache)
    target_directory = source_cache / cache_key
    if _validate_cache_path(project_root, target_directory, expected_type="directory"):
        raise ProjectError("proxy cache target already exists")
    with tempfile.TemporaryDirectory(dir=source_cache, prefix=".candidate-") as candidate_text:
        candidate_directory = Path(candidate_text)
        candidate_output = candidate_directory / "proxy.mp4"
        candidate_manifest = candidate_directory / "manifest.json"
        if not _validate_cache_path(project_root, candidate_directory, expected_type="directory"):
            raise ProjectError("proxy candidate directory is invalid")
        _validate_cache_path(project_root, candidate_output, expected_type="file")
        _validate_cache_path(project_root, candidate_manifest, expected_type="file")
        if phase_callback is not None:
            phase_callback("proxy_encoding")
        transcode_proxy(
            source_path,
            output_path=candidate_output,
            probe=source.probe,
            profile=profile,
            ffmpeg=ffmpeg,
            source_inspection=source_inspection,
        )
        if not _validate_cache_path(project_root, candidate_output, expected_type="file"):
            raise ProjectError("proxy candidate output is invalid")
        _validate_unchanged(store, project, source, source_path)
        if phase_callback is not None:
            phase_callback("proxy_verifying")
        report = verify_proxy_output(
            candidate_output,
            source.probe,
            profile,
            ffprobe,
            ffmpeg=ffmpeg,
            source_inspection=source_inspection,
            decode=True,
        )
        _validate_unchanged(store, project, source, source_path)
        output_fingerprint = fingerprint_file(candidate_output)
        manifest = ProxyManifest(
            source_id=source.source_id,
            cache_key=cache_key,
            source_fingerprint=source.fingerprint,
            source_probe=source.probe,
            profile=profile,
            output=ProxyOutput(
                relative_path=f"proxies/{source.source_id}/{cache_key}/proxy.mp4",
                size=output_fingerprint.size,
                sha256_head_tail=output_fingerprint.sha256_head_tail,
                duration_ticks=report.duration_ticks,
                probe=report.probe,
            ),
            tools={
                "ffmpeg_version": ffmpeg.version,
                "ffprobe_version": ffprobe.version,
            },
            checks=report.checks,
            padded_video_frames=report.padded_video_frames,
            padded_audio_samples=report.padded_audio_samples,
            leading_video_frames=report.leading_video_frames,
            leading_audio_samples=report.leading_audio_samples,
        )
        _write_manifest(candidate_manifest, manifest)
        if not _validate_cache_path(project_root, candidate_manifest, expected_type="file"):
            raise ProjectError("proxy candidate manifest is invalid")
        if phase_callback is not None:
            phase_callback("proxy_publishing")
        _publish_proxy(
            project_root,
            candidate_output,
            candidate_manifest,
            target_directory,
        )
    return ProxyResult(_ready_state(project, manifest, report, reused=False))


def _publish_proxy(
    project_root: Path,
    output: Path,
    manifest: Path,
    target_directory: Path,
) -> None:
    if not _validate_cache_path(project_root, output, expected_type="file"):
        raise ProjectError("proxy candidate output is invalid")
    if not _validate_cache_path(project_root, manifest, expected_type="file"):
        raise ProjectError("proxy candidate manifest is invalid")
    if _validate_cache_path(project_root, target_directory, expected_type="directory"):
        raise ProjectError("proxy cache target already exists")
    target_directory.mkdir(parents=False, exist_ok=False)
    if not _validate_cache_path(project_root, target_directory, expected_type="directory"):
        raise ProjectError("proxy cache publish directory is invalid")
    published_output = target_directory / "proxy.mp4"
    published_manifest = target_directory / "manifest.json"
    try:
        os.replace(output, published_output)
        try:
            os.replace(manifest, published_manifest)
        except BaseException:
            published_output.unlink(missing_ok=True)
            raise
    except BaseException:
        published_manifest.unlink(missing_ok=True)
        published_output.unlink(missing_ok=True)
        try:
            target_directory.rmdir()
        except OSError:
            pass
        raise


def _write_manifest(path: Path, manifest: ProxyManifest) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(manifest.to_dict(), stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _validate_unchanged(
    store: ProjectStore,
    expected_project: Project,
    expected_source: SourceAsset,
    source_path: Path,
) -> None:
    current = store.load()
    if current != expected_project:
        raise ProjectError("project changed during proxy generation")
    if _project_source(current, expected_source.source_id) != expected_source:
        raise ProjectError("proxy source snapshot changed")
    if fingerprint_file(source_path) != expected_source.fingerprint:
        raise ProjectError("proxy source fingerprint changed during generation")


def _project_source(project: Project, source_id: str) -> SourceAsset:
    if not isinstance(source_id, str) or not source_id:
        raise ProjectError("source_id is required")
    matches = [source for source in project.sources if source.source_id == source_id]
    if len(matches) != 1:
        raise ProjectError("proxy source is not part of the project")
    source = matches[0]
    if source.probe.video_codec is None and source.probe.audio_codec is None:
        raise ProjectError("proxy source has no usable media stream")
    return source


def _state(
    project: Project,
    source: SourceAsset,
    cache_key: str,
    status: str,
    reason: str | None,
) -> ProxyState:
    return ProxyState(
        source_id=source.source_id,
        cache_key=cache_key,
        status=status,
        reason=reason,
        project_revision=project.revision,
    )


def _ready_state(
    project: Project,
    manifest: ProxyManifest,
    report: ProxyVerificationReport,
    *,
    reused: bool,
) -> ProxyState:
    return ProxyState(
        source_id=manifest.source_id,
        cache_key=manifest.cache_key,
        status="ready",
        reason=None,
        project_revision=project.revision,
        reused=reused,
        proxy_relative_path=manifest.output.relative_path,
        manifest_relative_path=manifest.manifest_relative_path,
        summary={
            "duration_ticks": report.duration_ticks,
            "canvas": manifest.profile.to_dict()["canvas"],
            "frame_rate": manifest.profile.to_dict()["frame_rate"],
            "has_audio": manifest.profile.has_audio,
            "checks": dict(report.checks),
        },
    )


def _load_ready_manifest(
    project_root: Path,
    source: SourceAsset,
    cache_key: str,
) -> ProxyManifest:
    manifest_path = project_root / "proxies" / source.source_id / cache_key / "manifest.json"
    if not _validate_cache_path(project_root, manifest_path, expected_type="file"):
        raise ProjectError("proxy manifest is missing")
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProjectError("proxy manifest is invalid") from error
    if not isinstance(raw, dict):
        raise ProjectError("proxy manifest must contain an object")
    manifest = ProxyManifest.from_dict(raw)
    if manifest.source_id != source.source_id or manifest.cache_key != cache_key:
        raise ProjectError("proxy manifest identity is stale")
    if manifest.source_fingerprint != source.fingerprint or manifest.source_probe != source.probe:
        raise ProjectError("proxy manifest source snapshot is stale")
    return manifest


def _proxy_profile_summary(manifest: ProxyManifest) -> dict[str, object]:
    profile = manifest.profile.to_dict()
    return {
        "canvas": profile["canvas"],
        "frame_rate": profile["frame_rate"],
        "has_audio": profile["has_audio"],
    }


def _validated_project_root(project_path: Path) -> Path:
    root = Path(os.path.abspath(project_path))
    try:
        root.lstat()
    except OSError as error:
        raise ProjectError("proxy project root is missing or unreadable") from error
    if root.is_symlink() or not root.is_dir():
        raise ProjectError("proxy project root is invalid")
    return root.resolve(strict=True)


def _validate_cache_path(
    project_root: Path,
    path: Path,
    *,
    expected_type: str,
) -> bool:
    absolute = Path(os.path.abspath(path))
    try:
        relative = absolute.relative_to(project_root)
    except ValueError as error:
        raise ProjectError("proxy cache path escapes the project") from error
    current = project_root
    parts = relative.parts
    for index, part in enumerate(parts):
        current = current / part
        try:
            current.lstat()
        except FileNotFoundError:
            return False
        except OSError as error:
            raise ProjectError("proxy cache path is unreadable") from error
        if current.is_symlink():
            raise ProjectError("proxy cache path contains a symlink")
        is_leaf = index == len(parts) - 1
        if not is_leaf and not current.is_dir():
            raise ProjectError("proxy cache path parent is not a directory")
        if is_leaf:
            if expected_type == "directory" and not current.is_dir():
                raise ProjectError("proxy cache directory has the wrong type")
            if expected_type == "file" and not current.is_file():
                raise ProjectError("proxy cache file has the wrong type")
    return True


def _ensure_cache_directory(project_root: Path, path: Path) -> None:
    if _validate_cache_path(project_root, path, expected_type="directory"):
        return
    parent = path.parent
    if parent != project_root and not _validate_cache_path(
        project_root, parent, expected_type="directory"
    ):
        raise ProjectError("proxy cache parent is missing")
    path.mkdir(parents=False, exist_ok=False)
    if not _validate_cache_path(project_root, path, expected_type="directory"):
        raise ProjectError("proxy cache directory is invalid")


def _has_other_manifest(project_root: Path, source_cache: Path, cache_key: str) -> bool:
    if not _validate_cache_path(project_root, source_cache, expected_type="directory"):
        return False
    try:
        entries = list(os.scandir(source_cache))
    except OSError as error:
        raise ProjectError("proxy cache path is unreadable") from error
    found = False
    for entry in entries:
        if entry.name == cache_key or entry.name.startswith(".candidate-"):
            continue
        directory = source_cache / entry.name
        if not _validate_cache_path(project_root, directory, expected_type="directory"):
            continue
        manifest = directory / "manifest.json"
        if _validate_cache_path(project_root, manifest, expected_type="file"):
            found = True
    return found
