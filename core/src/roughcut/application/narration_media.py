"""Idempotent lifecycle for project-managed unrecorded narration placeholder media."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from roughcut.adapters.artifact_store import read_json_object, write_new_json
from roughcut.adapters.ffmpeg.render import (
    FFmpegRenderError,
    resolve_render_tools,
    ticks_to_seconds_text,
)
from roughcut.adapters.ffprobe import ProbeError, probe_media
from roughcut.adapters.workflow_candidates import (
    StagedWorkflowFile,
    publish_workflow_candidate,
)
from roughcut.application.sources import fingerprint_file
from roughcut.domain.edit import PLACEHOLDER_MEDIA_VERSION
from roughcut.domain.narration_placeholder import (
    NARRATION_PLACEHOLDER_FONT_CANDIDATES,
    NARRATION_PLACEHOLDER_GENERATOR,
    NARRATION_PLACEHOLDER_LABEL,
    NARRATION_PLACEHOLDER_RELATIVE_DIRECTORY,
    NarrationPlaceholderMedia,
    placeholder_media_id,
)
from roughcut.domain.project import MediaProbe, ProjectError, SourceFingerprint
from roughcut.domain.render import OutputSettings
from roughcut.domain.time import RationalRate


class NarrationPlaceholderMediaError(ProjectError):
    """Raised when managed placeholder media cannot be generated or verified."""


def placeholder_directory(project_path: Path) -> Path:
    return project_path / NARRATION_PLACEHOLDER_RELATIVE_DIRECTORY


def placeholder_media_path(project_path: Path, media_id: str) -> Path:
    return placeholder_directory(project_path) / f"{media_id}.mp4"


def placeholder_record_path(project_path: Path, media_id: str) -> Path:
    return placeholder_directory(project_path) / f"{media_id}.json"


def read_placeholder_media(
    project_path: Path, media_id: str
) -> NarrationPlaceholderMedia | None:
    if not isinstance(media_id, str) or not media_id.startswith("phm_"):
        raise NarrationPlaceholderMediaError("narration placeholder media ID is invalid")
    record_path = placeholder_record_path(project_path, media_id)
    if not record_path.is_file():
        return None
    try:
        media = NarrationPlaceholderMedia.from_dict(
            read_json_object(record_path, description="narration placeholder media record")
        )
    except ProjectError as error:
        raise NarrationPlaceholderMediaError(
            "narration placeholder media record is invalid"
        ) from error
    if media.media_id != media_id:
        raise NarrationPlaceholderMediaError(
            "narration placeholder media record identity mismatch"
        )
    _validate_record_identity(media)
    return media


def ensure_narration_placeholder_media(
    project_path: Path,
    *,
    output_settings: OutputSettings,
    video_frames: int,
    required_duration_ticks: int,
) -> NarrationPlaceholderMedia:
    """Return verified managed media, generating it exactly once when absent.

    The frame count is the explicit immutable generation parameter carried by
    every placeholder clip, so regeneration is deterministic and can never
    extend an existing media in place. A media file left behind by a crash
    between media publish and record publish is repaired by regenerating the
    exact parameters and comparing content hashes before adopting it.
    """
    if (
        isinstance(video_frames, bool)
        or not isinstance(video_frames, int)
        or video_frames <= 0
    ):
        raise NarrationPlaceholderMediaError(
            "narration placeholder media frames are invalid"
        )
    frame_rate = output_settings.frame_rate
    media_id = placeholder_media_id(
        media_version=PLACEHOLDER_MEDIA_VERSION,
        width=output_settings.width,
        height=output_settings.height,
        frame_rate=frame_rate,
        audio_sample_rate=output_settings.audio_sample_rate,
        video_frames=video_frames,
    )
    media_path = placeholder_media_path(project_path, media_id)
    existing = read_placeholder_media(project_path, media_id)
    if existing is not None:
        _validate_existing_media(
            project_path,
            existing,
            output_settings,
            required_duration_ticks,
            video_frames=video_frames,
        )
        return existing
    if media_path.exists():
        orphan = _adopt_orphan_media(
            project_path,
            media_id,
            media_path,
            output_settings=output_settings,
            video_frames=video_frames,
            required_duration_ticks=required_duration_ticks,
        )
        if orphan is not None:
            return orphan
    return _generate_narration_placeholder_media(
        project_path,
        media_id=media_id,
        output_settings=output_settings,
        video_frames=video_frames,
        required_duration_ticks=required_duration_ticks,
    )


def _adopt_orphan_media(
    project_path: Path,
    media_id: str,
    media_path: Path,
    *,
    output_settings: OutputSettings,
    video_frames: int,
    required_duration_ticks: int,
) -> NarrationPlaceholderMedia | None:
    """Repair a crash-orphaned media file without rewriting it.

    The deterministic generator is re-run and compared by content hash; only a
    byte-identical regeneration proves the orphan is the exact media this media
    ID describes. Anything else fails closed and preserves the file.
    """

    _validate_regular_owned_file(media_path, "narration placeholder media")
    temporary, font_path, probe, content_sha256 = _render_placeholder_temp(
        project_path,
        output_settings=output_settings,
        video_frames=video_frames,
    )
    try:
        if content_sha256 != _file_sha256(media_path):
            raise NarrationPlaceholderMediaError(
                "narration placeholder media exists without its immutable record"
            )
    finally:
        _remove_temporary(temporary)
    _validate_generated_probe(probe, output_settings, video_frames)
    if not _covers(probe, required_duration_ticks):
        raise NarrationPlaceholderMediaError(
            "narration placeholder media does not cover the required duration"
        )
    media = NarrationPlaceholderMedia(
        media_id=media_id,
        media_version=PLACEHOLDER_MEDIA_VERSION,
        label=NARRATION_PLACEHOLDER_LABEL,
        generator=NARRATION_PLACEHOLDER_GENERATOR,
        video_frames=video_frames,
        font_path=font_path,
        content_sha256=content_sha256,
        fingerprint=fingerprint_file(media_path),
        probe=probe,
        created_at=datetime.now(UTC).isoformat(),
    )
    # Return the canonical record that actually landed on disk; a concurrent
    # identical writer may win the record path with a byte-exact record.
    return _write_new_record(
        placeholder_record_path(project_path, media_id),
        media,
        content_sha256=content_sha256,
        final_path=media_path,
    )


def _covers(probe: MediaProbe, required_duration_ticks: int) -> bool:
    return probe.duration_ticks >= required_duration_ticks


def _require_directory(path: Path, name: str) -> None:
    try:
        details = os.lstat(path)
    except OSError as error:
        raise NarrationPlaceholderMediaError(f"{name} is unavailable") from error
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        raise NarrationPlaceholderMediaError(f"{name} is unsafe")


def _validate_regular_owned_file(path: Path, name: str) -> None:
    try:
        details = os.lstat(path)
    except OSError as error:
        raise NarrationPlaceholderMediaError(f"{name} is unavailable") from error
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISREG(details.st_mode)
        or details.st_nlink != 1
    ):
        raise NarrationPlaceholderMediaError(f"{name} is unsafe")


def _validate_record_identity(media: NarrationPlaceholderMedia) -> None:
    probe = media.probe
    if probe.nominal_frame_rate is None:
        raise NarrationPlaceholderMediaError("narration placeholder media frame rate is missing")
    width = _required_int(probe.width, "placeholder media width")
    height = _required_int(probe.height, "placeholder media height")
    sample_rate = _required_int(probe.audio_sample_rate, "placeholder media sample rate")
    try:
        expected_id = placeholder_media_id(
            media_version=media.media_version,
            width=width,
            height=height,
            frame_rate=RationalRate(
                probe.nominal_frame_rate["numerator"], probe.nominal_frame_rate["denominator"]
            ),
            audio_sample_rate=sample_rate,
            video_frames=media.video_frames,
        )
    except (KeyError, ValueError) as error:
        raise NarrationPlaceholderMediaError(
            "narration placeholder media frame rate is invalid"
        ) from error
    if expected_id != media.media_id:
        raise NarrationPlaceholderMediaError(
            "narration placeholder media record does not match its identity"
        )
    if probe.video_codec != "h264" or probe.audio_codec is None:
        raise NarrationPlaceholderMediaError(
            "narration placeholder media streams are invalid"
        )


def _validate_existing_media(
    project_path: Path,
    media: NarrationPlaceholderMedia,
    output_settings: OutputSettings,
    required_duration_ticks: int,
    *,
    video_frames: int,
) -> None:
    _validate_record_identity(media)
    probe = media.probe
    if (
        probe.width != output_settings.width
        or probe.height != output_settings.height
        or probe.audio_sample_rate != output_settings.audio_sample_rate
    ):
        raise NarrationPlaceholderMediaError(
            "narration placeholder media does not match the project output settings"
        )
    if media.video_frames != video_frames:
        raise NarrationPlaceholderMediaError(
            "narration placeholder media frame count does not match its identity"
        )
    media_path = placeholder_media_path(project_path, media.media_id)
    if not media_path.is_file():
        raise NarrationPlaceholderMediaError("narration placeholder media file is missing")
    try:
        if fingerprint_file(media_path) != media.fingerprint:
            raise NarrationPlaceholderMediaError(
                "narration placeholder media fingerprint has changed"
            )
    except OSError as error:
        raise NarrationPlaceholderMediaError(
            "narration placeholder media is unreadable"
        ) from error
    expected_coverage = video_frames * output_settings.frame_rate.ticks_per_frame
    if probe.duration_ticks < expected_coverage:
        raise NarrationPlaceholderMediaError(
            "narration placeholder media duration does not match its frame count"
        )
    if not media.covers_duration(required_duration_ticks):
        raise NarrationPlaceholderMediaError(
            "narration placeholder media does not cover the required duration"
        )


def _render_placeholder_temp(
    project_path: Path,
    *,
    output_settings: OutputSettings,
    video_frames: int,
) -> tuple[Path, str, MediaProbe, str]:
    """Render the placeholder card into an operation-owned unique staging file."""

    try:
        ffmpeg, ffprobe = resolve_render_tools()
    except FFmpegRenderError as error:
        raise NarrationPlaceholderMediaError(str(error)) from error
    font_path = _resolve_font_path()
    _require_drawtext(ffmpeg.resolved_path)
    directory = placeholder_directory(project_path)
    directory.mkdir(parents=True, exist_ok=True)
    _require_directory(directory, "narration placeholder directory")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=directory,
        prefix=".narration-placeholder.",
        suffix=".staging.mp4",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    frame_rate = output_settings.frame_rate
    duration_ticks = video_frames * frame_rate.ticks_per_frame
    command = [
        ffmpeg.resolved_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-f",
        "lavfi",
        "-i",
        (
            f"color=c=black:s={output_settings.width}x{output_settings.height}:"
            f"r={frame_rate.numerator}/{frame_rate.denominator}"
        ),
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=r={output_settings.audio_sample_rate}:cl=stereo",
        "-vf",
        (
            f"drawtext=fontfile={_escape_filter_value(font_path)}:"
            f"text={_escape_filter_value(NARRATION_PLACEHOLDER_LABEL)}:"
            f"fontcolor=white:fontsize={max(16, output_settings.height // 12)}:"
            "x=(w-text_w)/2:y=(h-text_h)/2"
        ),
        "-frames:v",
        str(video_frames),
        "-t",
        ticks_to_seconds_text(duration_ticks),
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-color_range",
        "tv",
        "-r",
        f"{frame_rate.numerator}/{frame_rate.denominator}",
        "-fps_mode",
        "cfr",
        "-c:a",
        "aac",
        "-ar",
        str(output_settings.audio_sample_rate),
        "-ac",
        "2",
        "-map_metadata",
        "-1",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        "-y",
        str(temporary),
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as error:
        _remove_temporary(temporary)
        raise NarrationPlaceholderMediaError(
            "narration placeholder FFmpeg process could not start"
        ) from error
    if result.returncode != 0 or not temporary.is_file():
        _remove_temporary(temporary)
        detail = (result.stderr or "").strip().splitlines()
        message = detail[-1] if detail else "FFmpeg returned a non-zero exit status"
        raise NarrationPlaceholderMediaError(
            f"narration placeholder generation failed: {message}"
        )
    try:
        probe = probe_media(temporary, ffprobe_command=ffprobe.resolved_path)
        _validate_generated_probe(probe, output_settings, video_frames)
        content_sha256 = _file_sha256(temporary)
    except (ProbeError, ProjectError, OSError) as error:
        _remove_temporary(temporary)
        raise NarrationPlaceholderMediaError(
            "narration placeholder media verification failed"
        ) from error
    return temporary, font_path, probe, content_sha256


def _generate_narration_placeholder_media(
    project_path: Path,
    *,
    media_id: str,
    output_settings: OutputSettings,
    video_frames: int,
    required_duration_ticks: int,
) -> NarrationPlaceholderMedia:
    if video_frames * output_settings.frame_rate.ticks_per_frame < required_duration_ticks:
        raise NarrationPlaceholderMediaError(
            "narration placeholder media frames do not cover the required duration"
        )
    temporary, font_path, probe, content_sha256 = _render_placeholder_temp(
        project_path,
        output_settings=output_settings,
        video_frames=video_frames,
    )
    final_path = placeholder_media_path(project_path, media_id)
    record_path = placeholder_record_path(project_path, media_id)
    published_identity: tuple[int, int] | None = None
    try:
        media = NarrationPlaceholderMedia(
            media_id=media_id,
            media_version=PLACEHOLDER_MEDIA_VERSION,
            label=NARRATION_PLACEHOLDER_LABEL,
            generator=NARRATION_PLACEHOLDER_GENERATOR,
            video_frames=video_frames,
            font_path=font_path,
            content_sha256=content_sha256,
            fingerprint=fingerprint_file(temporary),
            probe=probe,
            created_at=datetime.now(UTC).isoformat(),
        )
        try:
            publish_workflow_candidate(
                final_path,
                media_id,
                StagedWorkflowFile(temporary),
            )
            published_identity = _published_identity(final_path)
        except FileExistsError:
            # A concurrent writer published this exact generation first; keep
            # its file only when the bytes match because the media ID binds the
            # full generation parameter set.
            _validate_regular_owned_file(final_path, "narration placeholder media")
            if _file_sha256(final_path) != content_sha256:
                raise NarrationPlaceholderMediaError(
                    "narration placeholder media conflicts with another publication"
                ) from None
            # The published winner is canonical: describe it with its own
            # probe and fingerprint instead of the losing staging file's mtime.
            media = _canonical_media_for_published_file(
                media_id,
                final_path,
                output_settings=output_settings,
                video_frames=video_frames,
                font_path=font_path,
                content_sha256=content_sha256,
                created_at=media.created_at,
            )
        # Whatever won the publication race, the record must describe the exact
        # published file; a mismatch fails closed before any record is written.
        if media.fingerprint != fingerprint_file(final_path):
            raise NarrationPlaceholderMediaError(
                "narration placeholder media fingerprint changed before its record was published"
            )
        return _write_new_record(
            record_path,
            media,
            content_sha256=content_sha256,
            final_path=final_path,
        )
    except BaseException:
        _reconcile_failed_media_publication(
            final_path,
            record_path,
            content_sha256=content_sha256,
            published_identity=published_identity,
        )
        raise
    finally:
        _remove_temporary(temporary)


def _canonical_media_for_published_file(
    media_id: str,
    final_path: Path,
    *,
    output_settings: OutputSettings,
    video_frames: int,
    font_path: str,
    content_sha256: str,
    created_at: str,
) -> NarrationPlaceholderMedia:
    """Build the canonical record identity from the exact published file."""

    try:
        _ffmpeg, ffprobe = resolve_render_tools()
        probe = probe_media(final_path, ffprobe_command=ffprobe.resolved_path)
    except (FFmpegRenderError, ProbeError, OSError) as error:
        raise NarrationPlaceholderMediaError(
            "narration placeholder media verification failed"
        ) from error
    _validate_generated_probe(probe, output_settings, video_frames)
    return NarrationPlaceholderMedia(
        media_id=media_id,
        media_version=PLACEHOLDER_MEDIA_VERSION,
        label=NARRATION_PLACEHOLDER_LABEL,
        generator=NARRATION_PLACEHOLDER_GENERATOR,
        video_frames=video_frames,
        font_path=font_path,
        content_sha256=content_sha256,
        fingerprint=fingerprint_file(final_path),
        probe=probe,
        created_at=created_at,
    )


def _published_identity(path: Path) -> tuple[int, int]:
    details = os.lstat(path)
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISREG(details.st_mode)
        or details.st_nlink != 1
    ):
        raise NarrationPlaceholderMediaError("narration placeholder media is unsafe")
    return (details.st_dev, details.st_ino)


def _write_new_record(
    record_path: Path,
    media: NarrationPlaceholderMedia,
    *,
    content_sha256: str,
    final_path: Path,
) -> NarrationPlaceholderMedia:
    """Publish the immutable record, adopting an identical concurrent record."""

    try:
        write_new_json(record_path, media.to_dict())
        return media
    except Exception as error:
        # A concurrent writer owns the record path. Its file may exist while
        # still being flushed, so wait briefly for a readable record that both
        # matches this generation and describes the exact published file.
        existing: NarrationPlaceholderMedia | None = None
        for _attempt in range(25):
            existing = _read_record_or_none(record_path)
            if existing is not None:
                break
            time.sleep(0.02)
        try:
            published_fingerprint: SourceFingerprint | None = fingerprint_file(final_path)
        except OSError:
            published_fingerprint = None
        if (
            existing is not None
            and published_fingerprint is not None
            and _same_generation(existing, media, content_sha256)
            and existing.fingerprint == published_fingerprint
        ):
            return existing
        raise NarrationPlaceholderMediaError(
            "narration placeholder media record could not be published"
        ) from error


def _same_generation(
    existing: NarrationPlaceholderMedia,
    candidate: NarrationPlaceholderMedia,
    content_sha256: str,
) -> bool:
    return (
        existing.media_id == candidate.media_id
        and existing.media_version == candidate.media_version
        and existing.video_frames == candidate.video_frames
        and existing.content_sha256 == content_sha256
        and existing.probe.width == candidate.probe.width
        and existing.probe.height == candidate.probe.height
        and existing.probe.audio_sample_rate == candidate.probe.audio_sample_rate
        and existing.probe.nominal_frame_rate == candidate.probe.nominal_frame_rate
    )


def _read_record_or_none(record_path: Path) -> NarrationPlaceholderMedia | None:
    if not record_path.is_file():
        return None
    try:
        return NarrationPlaceholderMedia.from_dict(
            read_json_object(record_path, description="narration placeholder media record")
        )
    except ProjectError:
        return None


def _reconcile_failed_media_publication(
    final_path: Path,
    record_path: Path,
    *,
    content_sha256: str,
    published_identity: tuple[int, int] | None,
) -> None:
    """Clean only the exact media inode this action published, if any.

    A record that exists unchanged describes a valid publication (ours or a
    concurrent identical writer), so nothing is removed. Otherwise the file is
    removed only when it is still the staged inode with the expected hash; a
    replaced or foreign file is preserved and reported.
    """

    if record_path.is_file():
        return
    if published_identity is None:
        return
    try:
        details = os.lstat(final_path)
    except OSError:
        return
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISREG(details.st_mode)
        or details.st_nlink != 1
        or (details.st_dev, details.st_ino) != published_identity
    ):
        raise NarrationPlaceholderMediaError(
            "narration placeholder media cleanup refused a changed publication"
        )
    try:
        if _file_sha256(final_path) != content_sha256:
            raise NarrationPlaceholderMediaError(
                "narration placeholder media cleanup refused a changed publication"
            )
        final_path.unlink()
    except NarrationPlaceholderMediaError:
        raise
    except OSError as error:
        raise NarrationPlaceholderMediaError(
            "narration placeholder media cleanup failed"
        ) from error


def _validate_generated_probe(
    probe: MediaProbe,
    output_settings: OutputSettings,
    video_frames: int,
) -> None:
    if probe.video_codec != "h264" or probe.audio_codec != "aac":
        raise NarrationPlaceholderMediaError(
            "narration placeholder media codecs are invalid"
        )
    if (
        probe.width != output_settings.width
        or probe.height != output_settings.height
        or probe.audio_sample_rate != output_settings.audio_sample_rate
    ):
        raise NarrationPlaceholderMediaError(
            "narration placeholder media output settings are invalid"
        )
    expected_rate = {
        "numerator": output_settings.frame_rate.numerator,
        "denominator": output_settings.frame_rate.denominator,
    }
    if probe.nominal_frame_rate != expected_rate:
        raise NarrationPlaceholderMediaError(
            "narration placeholder media frame rate is invalid"
        )
    if probe.duration_ticks < video_frames * output_settings.frame_rate.ticks_per_frame:
        raise NarrationPlaceholderMediaError(
            "narration placeholder media duration is too short"
        )


def _resolve_font_path() -> str:
    for candidate in NARRATION_PLACEHOLDER_FONT_CANDIDATES:
        if candidate.startswith("C:\\") and not sys.platform.startswith("win"):
            continue
        path = Path(candidate)
        if path.is_file():
            return str(path)
    raise NarrationPlaceholderMediaError(
        "narration placeholder needs a known Chinese-capable font and none was found"
    )


def _require_drawtext(ffmpeg_path: str) -> None:
    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-filters"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as error:
        raise NarrationPlaceholderMediaError(
            "narration placeholder could not inspect FFmpeg filters"
        ) from error
    if result.returncode != 0 or " drawtext " not in (result.stdout or ""):
        raise NarrationPlaceholderMediaError(
            "narration placeholder requires FFmpeg drawtext (libfreetype)"
        )


def _escape_filter_value(value: str) -> str:
    # FFmpeg parses the filtergraph before drawtext parses its option values.
    # Each layer consumes its own escapes, including the Windows drive colon.
    option_value = "".join(
        f"\\{character}" if character in "\\':" else character
        for character in value
    )
    return "".join(
        f"\\{character}" if character in "\\'[],;" else character
        for character in option_value
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_temporary(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _required_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise NarrationPlaceholderMediaError(f"{name} is invalid")
    return value


__all__ = [
    "NarrationPlaceholderMediaError",
    "ensure_narration_placeholder_media",
    "placeholder_directory",
    "placeholder_media_path",
    "placeholder_record_path",
    "read_placeholder_media",
]
