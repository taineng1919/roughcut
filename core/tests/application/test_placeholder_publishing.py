"""Publishing-safety regressions for generated placeholder media and companions.

These tests cover the failure windows that matter for delivery integrity:
no-replace publication, crash-orphan self-healing, cleanup ownership checks,
symlink/hardlink rejection, concurrent ensure/export, and injected record or
copy failures.
"""

from __future__ import annotations

import os
import shutil
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from roughcut.application import narration_media, nle_handoff
from roughcut.application.narration_media import (
    NarrationPlaceholderMediaError,
    ensure_narration_placeholder_media,
    placeholder_directory,
    placeholder_media_path,
    placeholder_record_path,
    read_placeholder_media,
)
from roughcut.application.nle_handoff import (
    NlePlaceholderDelivery,
    _OwnedCompanionFile,
    _publish_placeholder_companion,
    _remove_owned_companion,
    placeholder_companion_directory,
)
from roughcut.application.projects import create_project
from roughcut.application.sources import fingerprint_file
from roughcut.domain.narration_placeholder import (
    NARRATION_PLACEHOLDER_LABEL,
    NarrationPlaceholderMedia,
    placeholder_media_id,
    placeholder_source_asset,
)
from roughcut.domain.project import MediaProbe, ProjectError
from roughcut.domain.render import OutputSettings
from roughcut.domain.time import RationalRate

pytestmark = pytest.mark.usefixtures("synthetic_media_runtime")

SETTINGS = OutputSettings(
    width=320,
    height=180,
    frame_rate=RationalRate(25, 1),
    audio_sample_rate=48_000,
)


def directory_is_clean(directory: Path) -> bool:
    return all(not path.name.startswith(".") for path in directory.iterdir())


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "placeholder project"
    create_project(root, "占位发布")
    return root


def _ensure(project_path: Path, frames: int = 12):
    return ensure_narration_placeholder_media(
        project_path,
        output_settings=SETTINGS,
        video_frames=frames,
        required_duration_ticks=frames * SETTINGS.frame_rate.ticks_per_frame,
    )


@pytest.mark.parametrize("special_name", [False, True])
def test_placeholder_font_path_survives_both_ffmpeg_escape_layers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, special_name: bool
) -> None:
    original_font = Path(narration_media._resolve_font_path())
    baseline = _ensure(_project(tmp_path / "baseline"))
    name = (
        f"font's [标题],;={original_font.suffix}"
        if special_name
        else ("msyh.ttc" if os.name == "nt" else r"C:\Windows\Fonts\msyh.ttc")
    )
    alias = tmp_path / name
    shutil.copyfile(original_font, alias)
    monkeypatch.setattr(narration_media, "_resolve_font_path", lambda: str(alias))

    generated = _ensure(_project(tmp_path / "escaped"))

    assert generated.font_path == str(alias)
    # The same font must produce the same actual card, rather than silently
    # falling back to another installed font after parsing a broken path.
    assert generated.content_sha256 == baseline.content_sha256


def test_ensure_is_no_replace_idempotent_and_heals_crash_orphans(tmp_path: Path) -> None:
    project_path = _project(tmp_path)
    media = _ensure(project_path)
    media_path = placeholder_media_path(project_path, media.media_id)
    record_path = placeholder_record_path(project_path, media.media_id)
    first_identity = media_path.stat()
    assert record_path.is_file()

    # A second ensure performs no work and keeps the exact published inode.
    again = _ensure(project_path)
    assert again == media
    assert media_path.stat().st_ino == first_identity.st_ino

    # Simulate the crash window between media publish and record publish: the
    # orphaned media must be adopted by deterministic regeneration instead of
    # being rejected, and the orphan inode must be preserved.
    record_path.unlink()
    healed = _ensure(project_path)
    assert healed.media_id == media.media_id
    assert healed.content_sha256 == media.content_sha256
    assert healed.fingerprint == media.fingerprint
    assert media_path.stat().st_ino == first_identity.st_ino
    assert record_path.is_file()
    assert read_placeholder_media(project_path, media.media_id) == healed

    # A tampered orphan is not adopted and is preserved for inspection.
    record_path.unlink()
    original = media_path.read_bytes()
    media_path.write_bytes(original + b"tamper")
    try:
        with pytest.raises(
            NarrationPlaceholderMediaError,
            match="exists without its immutable record",
        ):
            _ensure(project_path)
        assert media_path.read_bytes() == original + b"tamper"
    finally:
        media_path.write_bytes(original)


def test_record_publish_failure_removes_only_the_publication_it_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_path = _project(tmp_path)
    frames = 12
    media_id = placeholder_media_id(
        media_version=1,
        width=SETTINGS.width,
        height=SETTINGS.height,
        frame_rate=SETTINGS.frame_rate,
        audio_sample_rate=SETTINGS.audio_sample_rate,
        video_frames=frames,
    )
    media_path = placeholder_media_path(project_path, media_id)
    record_path = placeholder_record_path(project_path, media_id)

    def failing_write(path: Path, payload: dict[str, object]) -> None:
        raise ProjectError("injected record failure")

    monkeypatch.setattr(narration_media, "write_new_json", failing_write)
    with pytest.raises(
        NarrationPlaceholderMediaError,
        match="record could not be published",
    ):
        _ensure(project_path, frames=frames)
    # The action-owned media is removed together with the failed record, so a
    # later ensure can regenerate the identical bytes.
    assert not media_path.exists()
    assert not record_path.exists()

    monkeypatch.undo()
    recovered = _ensure(project_path, frames=frames)
    assert recovered.media_id == media_id
    assert media_path.is_file()
    assert record_path.is_file()
    assert directory_is_clean(placeholder_directory(project_path))


def test_record_publish_failure_preserves_a_replaced_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_path = _project(tmp_path)
    media = _ensure(project_path)
    media_path = placeholder_media_path(project_path, media.media_id)
    record_path = placeholder_record_path(project_path, media.media_id)
    record_path.unlink()
    media_path.unlink()

    real_write = narration_media.write_new_json

    def failing_write(path: Path, payload: dict[str, object]) -> None:
        raise ProjectError("injected record failure")

    def run_with_replaced_file() -> None:
        # Replace the published file with different bytes before the failing
        # record write: cleanup must refuse to delete the changed file.
        original_publish = narration_media.publish_workflow_candidate

        def publish_then_replace(final_path: Path, action_id: str, payload: object) -> None:
            original_publish(final_path, action_id, payload)  # type: ignore[arg-type]
            final_path.write_bytes(final_path.read_bytes() + b"changed")

        monkeypatch.setattr(
            narration_media, "publish_workflow_candidate", publish_then_replace
        )
        try:
            with pytest.raises(
                NarrationPlaceholderMediaError,
                match="cleanup refused a changed publication",
            ):
                _ensure(project_path)
        finally:
            monkeypatch.setattr(
                narration_media, "publish_workflow_candidate", original_publish
            )

    monkeypatch.setattr(narration_media, "write_new_json", failing_write)
    run_with_replaced_file()
    assert media_path.is_file()
    assert media_path.read_bytes().endswith(b"changed")
    monkeypatch.setattr(narration_media, "write_new_json", real_write)


def test_concurrent_ensure_publishes_exactly_one_media(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_path = _project(tmp_path)
    frames = 8
    media_id = placeholder_media_id(
        media_version=1,
        width=SETTINGS.width,
        height=SETTINGS.height,
        frame_rate=SETTINGS.frame_rate,
        audio_sample_rate=SETTINGS.audio_sample_rate,
        video_frames=frames,
    )
    results: list[object] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    real_publish = narration_media.publish_workflow_candidate

    def synchronized_publish(final_path: Path, action_id: str, payload: object) -> None:
        barrier.wait(timeout=30)
        real_publish(final_path, action_id, payload)  # type: ignore[arg-type]

    monkeypatch.setattr(
        narration_media, "publish_workflow_candidate", synchronized_publish
    )

    def worker() -> None:
        try:
            results.append(_ensure(project_path, frames=frames))
        except BaseException as error:  # noqa: BLE001 - recorded for the assertion
            errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors, errors
    assert len(results) == 2
    first, second = results
    assert first.media_id == second.media_id  # type: ignore[attr-defined]
    assert first.content_sha256 == second.content_sha256  # type: ignore[attr-defined]
    media_path = placeholder_media_path(project_path, media_id)
    record_path = placeholder_record_path(project_path, media_id)
    assert media_path.is_file()
    assert record_path.is_file()
    directory = placeholder_directory(project_path)
    assert sorted(path.name for path in directory.iterdir()) == [
        f"{media_id}.json",
        f"{media_id}.mp4",
    ]


def test_symlinked_placeholder_directory_fails_closed(tmp_path: Path) -> None:
    project_path = _project(tmp_path)
    external = tmp_path / "outside"
    external.mkdir()
    generated = project_path / "generated"
    generated.mkdir()
    (generated / "narration-placeholder").symlink_to(external, target_is_directory=True)
    with pytest.raises(NarrationPlaceholderMediaError, match="unsafe"):
        _ensure(project_path)
    assert not any(external.iterdir())


def _synthetic_media(
    tmp_path: Path,
    *,
    seed: bytes = b"placeholder",
    video_frames: int = 25,
) -> tuple[NarrationPlaceholderMedia, Path]:
    body_path = tmp_path / f"{seed.decode()}.bin"
    body_path.write_bytes(seed * 100)
    media_id = placeholder_media_id(
        media_version=1,
        width=320,
        height=180,
        frame_rate=RationalRate(25, 1),
        audio_sample_rate=48_000,
        video_frames=video_frames,
    )
    fingerprint = fingerprint_file(body_path)
    media = NarrationPlaceholderMedia(
        media_id=media_id,
        media_version=1,
        label=NARRATION_PLACEHOLDER_LABEL,
        generator="ffmpeg_drawtext_black_card_v1",
        video_frames=video_frames,
        font_path="/fixture/font.ttc",
        content_sha256=_sha256_file(body_path),
        fingerprint=fingerprint,
        probe=MediaProbe(
            duration_ticks=video_frames * 4_800,
            container_start_ticks=0,
            first_content_ticks=0,
            video_codec="h264",
            width=320,
            height=180,
            nominal_frame_rate={"numerator": 25, "denominator": 1},
            is_vfr=False,
            audio_codec="aac",
            audio_sample_rate=48_000,
            rotation_degrees=0,
        ),
        created_at="2026-10-16T00:00:00.000000Z",
    )
    return media, body_path


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _delivery(media: NarrationPlaceholderMedia, body_path: Path, destination: Path):
    return NlePlaceholderDelivery(
        media=media,
        source=placeholder_source_asset(media),
        source_path=body_path,
        delivered_path=destination,
    )


def test_companion_publish_rejects_symlink_and_hardlink_targets(tmp_path: Path) -> None:
    media, body_path = _synthetic_media(tmp_path)
    companion = tmp_path / "handoff_placeholder_media"
    outside = tmp_path / "outside"
    outside.mkdir()

    # A symlinked companion directory must be rejected without writing through it.
    companion.symlink_to(outside, target_is_directory=True)
    with pytest.raises(Exception, match="not a plain directory"):
        _publish_placeholder_companion(
            (_delivery(media, body_path, companion / f"{media.media_id}.mp4"),)
        )
    assert not any(outside.iterdir())
    companion.unlink()

    # A hardlinked existing companion file is not a single-link delivery.
    companion.mkdir()
    target = companion / f"{media.media_id}.mp4"
    target.write_bytes(body_path.read_bytes())
    os.link(target, tmp_path / "hardlink.bin")
    with pytest.raises(Exception, match="single-link regular file"):
        _publish_placeholder_companion(
            (_delivery(media, body_path, target),)
        )
    assert target.is_file()


def test_companion_publish_is_owned_only_for_created_files(tmp_path: Path) -> None:
    media, body_path = _synthetic_media(tmp_path)
    companion = tmp_path / "handoff_placeholder_media"
    target = companion / f"{media.media_id}.mp4"
    delivery = _delivery(media, body_path, target)

    owned, created_directory = _publish_placeholder_companion((delivery,))
    assert len(owned) == 1
    assert created_directory == companion
    assert target.is_file()

    # Re-publishing the identical file reuses it and owns nothing.
    reused, directory_again = _publish_placeholder_companion((delivery,))
    assert reused == []
    assert directory_again is None

    # Ownership cleanup verifies inode and bytes: a replaced file survives.
    target.write_bytes(target.read_bytes() + b"changed")
    _remove_owned_companion(owned, directory=created_directory)
    assert target.is_file()
    assert target.read_bytes().endswith(b"changed")


def test_companion_cleanup_removes_exact_owned_files_and_directory(
    tmp_path: Path,
) -> None:
    media, body_path = _synthetic_media(tmp_path)
    destination_directory = tmp_path / "exports"
    destination_directory.mkdir()
    companion = placeholder_companion_directory(destination_directory / "handoff.fcpxml")
    target = companion / f"{media.media_id}.mp4"
    owned, created_directory = _publish_placeholder_companion(
        (_delivery(media, body_path, target),)
    )
    assert created_directory == companion
    _remove_owned_companion(owned, directory=created_directory)
    assert not target.exists()
    assert not companion.exists()


def test_companion_publish_failure_cleans_own_files_and_keeps_reused_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, first_body = _synthetic_media(tmp_path, seed=b"first", video_frames=25)
    second, second_body = _synthetic_media(tmp_path, seed=b"second", video_frames=26)
    companion = tmp_path / "handoff_placeholder_media"
    companion.mkdir()
    first_target = companion / f"{first.media_id}.mp4"
    first_target.write_bytes(first_body.read_bytes())  # pre-existing, reused
    second_target = companion / f"{second.media_id}.mp4"

    real_publish = nle_handoff.publish_workflow_candidate

    def publish_then_fail(final_path: Path, action_id: str, payload: object) -> None:
        if final_path == second_target:
            raise OSError("injected companion failure")
        real_publish(final_path, action_id, payload)  # type: ignore[arg-type]

    monkeypatch.setattr(
        nle_handoff, "publish_workflow_candidate", publish_then_fail
    )
    with pytest.raises(Exception, match="could not be published"):
        _publish_placeholder_companion(
            (
                _delivery(first, first_body, first_target),
                _delivery(second, second_body, second_target),
            )
        )
    # The reused pre-existing delivery survives; nothing else remains.
    assert first_target.is_file()
    assert first_target.read_bytes() == first_body.read_bytes()
    assert not second_target.exists()
    assert sorted(path.name for path in companion.iterdir()) == [
        f"{first.media_id}.mp4"
    ]


def test_companion_cleanup_requires_all_expected_owners(
    tmp_path: Path,
) -> None:
    """A stale owned entry pointing at a different inode is never deleted."""

    media, body_path = _synthetic_media(tmp_path)
    companion = tmp_path / "handoff_placeholder_media"
    target = companion / f"{media.media_id}.mp4"
    owned, created_directory = _publish_placeholder_companion(
        (_delivery(media, body_path, target),)
    )
    stale = _OwnedCompanionFile(
        path=target,
        device=owned[0].device,
        inode=owned[0].inode + 1,
        content_sha256=owned[0].content_sha256,
    )
    _remove_owned_companion([stale], directory=created_directory)
    assert target.is_file()
    _remove_owned_companion(owned, directory=created_directory)
    assert not target.exists()


def test_concurrent_companion_publish_reuses_one_exact_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media, body_path = _synthetic_media(tmp_path)
    companion = tmp_path / "handoff_placeholder_media"
    companion.mkdir()
    target = companion / f"{media.media_id}.mp4"
    delivery = _delivery(media, body_path, target)
    barrier = threading.Barrier(2)
    outcomes: list[list[_OwnedCompanionFile]] = []
    errors: list[BaseException] = []

    real_publish = nle_handoff.publish_workflow_candidate

    def synchronized_publish(final_path: Path, action_id: str, payload: object) -> None:
        barrier.wait(timeout=30)
        real_publish(final_path, action_id, payload)  # type: ignore[arg-type]

    monkeypatch.setattr(
        nle_handoff, "publish_workflow_candidate", synchronized_publish
    )

    def worker() -> None:
        try:
            owned, _directory = _publish_placeholder_companion((delivery,))
            outcomes.append(owned)
        except BaseException as error:  # noqa: BLE001 - recorded for the assertion
            errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors, errors
    assert sorted(len(owned) for owned in outcomes) == [0, 1]
    assert target.is_file()
    assert target.read_bytes() == body_path.read_bytes()
    assert sorted(path.name for path in companion.iterdir()) == [
        f"{media.media_id}.mp4"
    ]


def test_placeholder_media_helper_module_and_copies_are_available(
    tmp_path: Path,
) -> None:
    """Guard against accidental loss of the public test entry points."""

    assert shutil.which("ffmpeg") is not None
    assert narration_media.__name__ == "roughcut.application.narration_media"
    assert callable(read_placeholder_media)
    assert replace(
        _synthetic_media(tmp_path)[0],
        created_at="2026-10-16T00:00:00.000000Z",
    ).media_id


def test_deterministic_race_losing_staging_never_writes_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fixed order: A publishes media, B loses the media race but writes the
    record first, A's record write then loses. Both callers and the persisted
    record must describe the actual published file (never B's staging mtime),
    and a third ensure must succeed without rewriting anything.
    """

    project_path = _project(tmp_path)
    frames = 8
    media_id = placeholder_media_id(
        media_version=1,
        width=SETTINGS.width,
        height=SETTINGS.height,
        frame_rate=SETTINGS.frame_rate,
        audio_sample_rate=SETTINGS.audio_sample_rate,
        video_frames=frames,
    )
    media_path = placeholder_media_path(project_path, media_id)

    real_publish = narration_media.publish_workflow_candidate
    real_write = narration_media.write_new_json
    barrier = threading.Barrier(2)
    winner_published = threading.Event()
    loser_record_written = threading.Event()

    def publish(path: Path, action: str, payload: object) -> None:
        barrier.wait(timeout=30)
        if threading.current_thread().name == "race-winner":
            real_publish(path, action, payload)  # type: ignore[arg-type]
            winner_published.set()
            if not loser_record_written.wait(30):
                raise RuntimeError("loser record timeout")
        else:
            if not winner_published.wait(30):
                raise RuntimeError("winner publication timeout")
            real_publish(path, action, payload)  # type: ignore[arg-type]

    def write(path: Path, payload: dict[str, object]) -> None:
        real_write(path, payload)
        if threading.current_thread().name == "race-loser":
            loser_record_written.set()

    monkeypatch.setattr(narration_media, "publish_workflow_candidate", publish)
    monkeypatch.setattr(narration_media, "write_new_json", write)

    results: list[object] = []
    errors: list[BaseException] = []
    barrier_reset = threading.Barrier(2)

    def worker(reset: threading.Barrier) -> None:
        reset.wait(timeout=30)
        try:
            results.append(_ensure(project_path, frames=frames))
        except BaseException as error:  # noqa: BLE001 - recorded for the assertion
            errors.append(error)

    threads = [
        threading.Thread(target=worker, args=(barrier_reset,), name=name)
        for name in ("race-winner", "race-loser")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert not errors, errors
    assert len(results) == 2

    actual = fingerprint_file(media_path)
    assert actual == fingerprint_file(media_path)
    for result in results:
        assert result.fingerprint == actual  # type: ignore[attr-defined]
        assert result.content_sha256 == _sha256_file(media_path)  # type: ignore[attr-defined]
    persisted = read_placeholder_media(project_path, media_id)
    assert persisted is not None
    assert persisted.fingerprint == actual
    assert persisted.content_sha256 == _sha256_file(media_path)

    inode_before = media_path.stat().st_ino
    third = _ensure(project_path, frames=frames)
    assert third == persisted
    assert media_path.stat().st_ino == inode_before
    assert third.fingerprint == fingerprint_file(media_path)
    assert sorted(path.name for path in placeholder_directory(project_path).iterdir()) == [
        f"{media_id}.json",
        f"{media_id}.mp4",
    ]
