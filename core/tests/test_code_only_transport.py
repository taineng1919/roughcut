"""Scheme B transport + FFmpeg installer oracle (thin, reuses existing oracles)."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from roughcut.adapters.component_download import (
    POPULATE_SOURCE_TIERS,
    download_artifact,
    populate_cache_from_local_file,
)
from roughcut.adapters.component_installation import ArtifactSpec
from roughcut.adapters.ffmpeg_environment import _MAX_VERSION, _MIN_VERSION, diagnose_ffmpeg


def _artifact(filename: str, payload: bytes) -> ArtifactSpec:
    return ArtifactSpec(
        component="python_runtime",
        name="fixture-runtime",
        version="1.0",
        filename=filename,
        url="https://example.com/packages/" + filename,
        license="MIT",
        sha256=hashlib.sha256(payload).hexdigest(),
        size=len(payload),
    )


def test_healthy_cache_reuses_without_download(tmp_path: Path) -> None:
    from roughcut.adapters.component_installation import (
        cache_artifact_path,
        cache_receipt_path,
    )

    payload = b"code-only-trial-verified-payload"
    artifact = _artifact("fixture-1.0-py3-none-any.whl", payload)
    cache = tmp_path / "cache"
    destination = cache_artifact_path(cache, artifact)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    result = download_artifact(artifact, cache)
    assert result.reused is True
    assert result.path == destination
    assert cache_receipt_path(destination).is_file()


def test_identity_mismatch_is_not_reused(tmp_path: Path) -> None:
    from roughcut.adapters.component_installation import cache_artifact_path

    good = b"exact-bytes-1234"
    bad = b"exact-bytes-5678"
    assert len(good) == len(bad)
    artifact = _artifact("fixture-1.0-py3-none-any.whl", good)
    cache = tmp_path / "cache"
    destination = cache_artifact_path(cache, artifact)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(bad)
    # Same size but wrong SHA must not verify; the downloader will try the
    # network (unreachable example.com) and must raise instead of reusing.
    with pytest.raises(Exception):
        download_artifact(artifact, cache)


def test_canonical_sha_authority_rejects_wrong_size(tmp_path: Path) -> None:
    payload = b"0123456789"
    artifact = _artifact("fixture-1.0-py3-none-any.whl", payload)
    wrong_size = ArtifactSpec(
        component=artifact.component,
        name=artifact.name,
        version=artifact.version,
        filename=artifact.filename,
        url=artifact.url,
        license=artifact.license,
        sha256=artifact.sha256,
        size=len(payload) + 1,
    )
    with pytest.raises(Exception):
        download_artifact(wrong_size, tmp_path / "cache")


def _ffmpeg_pair(tmp_path: Path) -> tuple[Path, Path]:
    ffmpeg = tmp_path / "ffmpeg"
    ffprobe = tmp_path / "ffprobe"
    for command in (ffmpeg, ffprobe):
        command.write_text("fixture", encoding="utf-8")
        command.chmod(0o755)
    return ffmpeg, ffprobe


def _oracle_runner(ffmpeg_version: str, ffprobe_version: str):
    import json as _json

    def run(command: list[str]):
        if command[1:] == ["-version"]:
            version = (
                ffmpeg_version
                if Path(command[0]).name == "ffmpeg"
                else ffprobe_version
            )
            return subprocess.CompletedProcess(command, 0, stdout=version + "\n", stderr="")
        if command[1:] == ["-hide_banner", "-h", "full"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="-filter_complex <graph_description>\n",
                stderr="",
            )
        if command[1:] == ["-hide_banner", "-encoders"]:
            return subprocess.CompletedProcess(
                command, 0, stdout=" V....D libx264 fixture\n A....D aac fixture\n", stderr=""
            )
        if Path(command[0]).name == "ffmpeg":
            Path(command[-1]).write_bytes(b"mp4")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        payload = {
            "streams": [
                {"codec_type": "video", "codec_name": "h264"},
                {"codec_type": "audio", "codec_name": "aac"},
            ],
            "format": {"duration": "0.200000"},
            "programs": [],
            "stream_groups": [],
        }
        return subprocess.CompletedProcess(command, 0, stdout=_json.dumps(payload), stderr="")

    return run


def _runner(version_line: str, *, help_ok: bool = True):
    ffmpeg_version = version_line
    ffprobe_version = version_line.replace("ffmpeg", "ffprobe")
    return _oracle_runner(ffmpeg_version, ffprobe_version)


def test_ffmpeg_contract_window_matches_product() -> None:
    assert _MIN_VERSION == (8, 1, 0)
    assert _MAX_VERSION == (10, 0, 0)


@pytest.mark.parametrize("version", ["8.0.9", "8.0.0", "10.0.0", "10.1.0"])
def test_installer_oracle_rejects_out_of_window(tmp_path: Path, version: str) -> None:
    ffmpeg, ffprobe = _ffmpeg_pair(tmp_path)
    result = diagnose_ffmpeg(
        ffmpeg_command=str(ffmpeg),
        ffprobe_command=str(ffprobe),
        command_runner=_runner(f"ffmpeg version {version}-vendor"),
    )
    assert result.ffmpeg.status == "unavailable" or result.ffprobe.status == "unavailable"


@pytest.mark.parametrize("version", ["8.1.0", "8.1.1", "9.0"])
def test_installer_oracle_accepts_in_window_with_capabilities(
    tmp_path: Path, version: str
) -> None:
    ffmpeg, ffprobe = _ffmpeg_pair(tmp_path)
    result = diagnose_ffmpeg(
        ffmpeg_command=str(ffmpeg),
        ffprobe_command=str(ffprobe),
        command_runner=_runner(f"ffmpeg version {version}-vendor"),
    )
    assert result.ffmpeg.status == result.ffprobe.status == "available"


def test_installer_oracle_rejects_mismatched_pair(tmp_path: Path) -> None:
    ffmpeg, ffprobe = _ffmpeg_pair(tmp_path)
    result = diagnose_ffmpeg(
        ffmpeg_command=str(ffmpeg),
        ffprobe_command=str(ffprobe),
        command_runner=_oracle_runner(
            "ffmpeg version 8.1.1-vendor", "ffprobe version 9.0-vendor"
        ),
    )
    assert result.ffmpeg.status == "unavailable" or result.ffprobe.status == "unavailable"


@pytest.mark.parametrize(
    "tier", ["local", "aliyun", "modelscope", "canonical", "agent-fallback"]
)
def test_populate_reports_tier_and_keeps_receipt_closed(
    tmp_path: Path, tier: str
) -> None:
    from roughcut.adapters.component_installation import (
        cache_artifact_path,
        cache_receipt_path,
    )

    assert tier in POPULATE_SOURCE_TIERS
    payload = f"scheme-b-populate-{tier}".encode()
    artifact = _artifact(f"populate-{tier}-1.0-py3-none-any.whl", payload)
    channel_file = (tmp_path / f"channel-{tier}.whl").absolute()
    channel_file.write_bytes(payload)
    cache = tmp_path / "cache"
    outcome = populate_cache_from_local_file(artifact, channel_file, tier, cache)
    assert outcome.reused is False
    assert outcome.provenance["tier"] == tier
    assert outcome.provenance["filename"] == artifact.filename
    assert outcome.provenance["sha256"] == artifact.sha256
    assert "credentials" not in json.dumps(outcome.provenance).lower()
    assert "://" not in json.dumps(outcome.provenance)
    receipt = json.loads(
        cache_receipt_path(cache_artifact_path(cache, artifact)).read_text(
            encoding="utf-8"
        )
    )
    assert receipt == {
        "schema_version": 1,
        "sha256": artifact.sha256,
        "size": artifact.size,
    }
    second = populate_cache_from_local_file(artifact, channel_file, tier, cache)
    assert second.reused is True


@pytest.mark.parametrize(
    "tier", ["local", "aliyun", "modelscope", "canonical", "agent-fallback"]
)
def test_populate_enforces_frozen_identity_for_every_tier(
    tmp_path: Path, tier: str
) -> None:
    good = b"frozen-bytes-1234"
    bad = b"frozen-bytes-5678"
    assert len(good) == len(bad)
    artifact = _artifact("frozen-1.0-py3-none-any.whl", good)
    channel_file = (tmp_path / "channel.whl").absolute()
    channel_file.write_bytes(bad)
    with pytest.raises(Exception):
        populate_cache_from_local_file(artifact, channel_file, tier, tmp_path / "cache")


def test_populate_rejects_unknown_tier_and_relative_path(tmp_path: Path) -> None:
    payload = b"populate-tier-check"
    artifact = _artifact("tier-1.0-py3-none-any.whl", payload)
    channel_file = (tmp_path / "channel.whl").absolute()
    channel_file.write_bytes(payload)
    with pytest.raises(Exception):
        populate_cache_from_local_file(
            artifact, channel_file, "homebrew", tmp_path / "cache"
        )
    with pytest.raises(Exception):
        populate_cache_from_local_file(
            artifact, Path("relative/channel.whl"), "local", tmp_path / "cache"
        )


def test_populate_fails_closed_before_mutation_without_disk_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import roughcut.adapters.component_download as populate_module
    from roughcut.adapters.component_installation import cache_artifact_path

    payload = b"populate-preflight-check-payload"
    artifact = _artifact("preflight-1.0-py3-none-any.whl", payload)
    channel_file = (tmp_path / "channel.whl").absolute()
    channel_file.write_bytes(payload)
    cache = tmp_path / "cache"
    monkeypatch.setattr(populate_module, "_available_bytes", lambda _path: 0)
    with pytest.raises(Exception, match="(?i)disk space"):
        populate_cache_from_local_file(artifact, channel_file, "aliyun", cache)
    assert not cache_artifact_path(
        cache.resolve(strict=False), artifact
    ).exists()


def test_populate_filename_resolves_from_frozen_catalog(tmp_path: Path) -> None:
    from scripts import bootstrap as bootstrap_script

    from roughcut.adapters.component_installation import load_release_catalog

    catalog = load_release_catalog()
    profile = catalog.profile_for("macos", "arm64")
    known = profile.runtime.artifacts[0].filename
    cache = tmp_path / "cache"
    cache.mkdir()
    channel_file = (tmp_path / "wrong-bytes.whl").absolute()
    channel_file.write_bytes(b"too short")
    # Known filename resolves, then fails closed on frozen size/SHA (not on lookup).
    with pytest.raises(RuntimeError, match="(?i)(size|checksum|differ)"):
        bootstrap_script.populate_cache_from_channel(
            cache, filename=known, source_path=channel_file, source_tier="aliyun"
        )
    with pytest.raises(RuntimeError, match="(?i)catalog"):
        bootstrap_script.populate_cache_from_channel(
            cache,
            filename="no-such-package-9.9.9-py3-none-any.whl",
            source_path=channel_file,
            source_tier="local",
        )


def test_cache_fill_changes_the_hashed_plan() -> None:
    from roughcut.adapters.component_installation import _canonical_hash

    base = {
        "artifacts": [
            {"filename": "a.whl", "cache_status": "missing"},
            {"filename": "b.whl", "cache_status": "missing"},
        ]
    }
    filled = {
        "artifacts": [
            {"filename": "a.whl", "cache_status": "verified"},
            {"filename": "b.whl", "cache_status": "missing"},
        ]
    }
    assert _canonical_hash(base) != _canonical_hash(filled)


def _unique_catalog_filename() -> tuple[str, str]:
    from collections import defaultdict

    from roughcut.adapters.component_installation import load_release_catalog

    catalog = load_release_catalog()
    profile = catalog.profile_for("macos", "arm64")
    candidates = list(profile.runtime.artifacts)
    for model in profile.models.values():
        candidates.extend(model.artifacts)
    for group in profile.alignment_groups:
        candidates.extend(group.artifacts)
    by_filename: dict[str, list] = defaultdict(list)
    for artifact in candidates:
        by_filename[artifact.filename].append(artifact)
    for filename, items in by_filename.items():
        if len(items) == 1:
            return filename, items[0].component
    raise AssertionError("catalog has no unique filename")


def test_populate_unique_filename_without_component_passes_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import bootstrap as bootstrap_script
    import roughcut.adapters.component_download as download_module

    filename, component = _unique_catalog_filename()
    cache = tmp_path / "cache"
    cache.mkdir()
    channel_file = (tmp_path / "channel.whl").absolute()
    channel_file.write_bytes(b"too short")

    # Resolution must not raise ambiguity; it must reach frozen size/SHA verification.
    with pytest.raises(RuntimeError, match="(?i)(size|checksum|differ)"):
        bootstrap_script.populate_cache_from_channel(
            cache, filename=filename, source_path=channel_file, source_tier="local"
        )

    # With a mocked verified publish, the same unique filename returns component identity.
    from pathlib import Path as _Path

    from roughcut.adapters.component_download import PopulateResult

    def fake_publish(artifact, source_path, source_tier, cache_root):
        return PopulateResult(
            _Path("/tmp/fake"),
            False,
            {
                "tier": source_tier,
                "filename": artifact.filename,
                "sha256": artifact.sha256,
                "size": artifact.size,
                "reused": False,
            },
        )

    monkeypatch.setattr(download_module, "populate_cache_from_local_file", fake_publish)
    # Patch the already-imported reference inside bootstrap module namespace.
    import scripts.bootstrap as _bootstrap_module

    monkeypatch.setattr(
        "roughcut.adapters.component_download.populate_cache_from_local_file",
        fake_publish,
    )
    # Re-import path inside function uses fresh import, so patch target above applies.
    result = _bootstrap_module.populate_cache_from_channel(
        cache, filename=filename, source_path=channel_file, source_tier="local"
    )
    assert result["filename"] == filename
    assert result["component"] == component


def test_populate_ambiguous_filename_without_component_fails_actionable(
    tmp_path: Path,
) -> None:
    from scripts import bootstrap as bootstrap_script

    cache = tmp_path / "cache"
    cache.mkdir()
    channel_file = (tmp_path / "channel.whl").absolute()
    channel_file.write_bytes(b"too short")
    with pytest.raises(
        RuntimeError, match="artifact filename is ambiguous; specify --populate-component"
    ):
        bootstrap_script.populate_cache_from_channel(
            cache, filename="model.pt", source_path=channel_file, source_tier="local"
        )


def test_populate_ambiguous_filename_with_correct_component_passes_resolution(
    tmp_path: Path,
) -> None:
    from scripts import bootstrap as bootstrap_script

    cache = tmp_path / "cache"
    cache.mkdir()
    channel_file = (tmp_path / "channel.whl").absolute()
    channel_file.write_bytes(b"too short")
    # Correct component resolves, then fails closed on frozen size/SHA (not on lookup).
    with pytest.raises(RuntimeError, match="(?i)(size|checksum|differ)"):
        bootstrap_script.populate_cache_from_channel(
            cache,
            filename="model.pt",
            source_path=channel_file,
            source_tier="local",
            component="model_punc",
        )


def test_populate_wrong_component_fails(tmp_path: Path) -> None:
    from scripts import bootstrap as bootstrap_script

    cache = tmp_path / "cache"
    cache.mkdir()
    channel_file = (tmp_path / "channel.whl").absolute()
    channel_file.write_bytes(b"too short")
    with pytest.raises(RuntimeError, match="(?i)catalog|component"):
        bootstrap_script.populate_cache_from_channel(
            cache,
            filename="model.pt",
            source_path=channel_file,
            source_tier="local",
            component="python_runtime",
        )


def test_populate_unknown_component_fails(tmp_path: Path) -> None:
    from scripts import bootstrap as bootstrap_script

    cache = tmp_path / "cache"
    cache.mkdir()
    channel_file = (tmp_path / "channel.whl").absolute()
    channel_file.write_bytes(b"too short")
    with pytest.raises(RuntimeError, match="(?i)unknown|component"):
        bootstrap_script.populate_cache_from_channel(
            cache,
            filename="model.pt",
            source_path=channel_file,
            source_tier="local",
            component="no_such_component",
        )


def test_populate_wrong_bytes_fail_closed(tmp_path: Path) -> None:
    from scripts import bootstrap as bootstrap_script
    from roughcut.adapters.component_installation import load_release_catalog

    catalog = load_release_catalog()
    profile = catalog.profile_for("macos", "arm64")
    known = profile.runtime.artifacts[0]
    cache = tmp_path / "cache"
    cache.mkdir()
    channel_file = (tmp_path / "wrong.whl").absolute()
    channel_file.write_bytes(b"wrong-bytes-payload-1234")
    with pytest.raises(RuntimeError, match="(?i)(size|checksum|differ)"):
        bootstrap_script.populate_cache_from_channel(
            cache,
            filename=known.filename,
            source_path=channel_file,
            source_tier="canonical",
            component=known.component,
        )


def test_populate_correct_artifact_produces_verified_receipt(tmp_path: Path) -> None:
    import json

    from roughcut.adapters.component_installation import (
        cache_artifact_path,
        cache_receipt_path,
    )

    payload = b"populate-component-verified-payload"
    artifact = _artifact("component-check-1.0-py3-none-any.whl", payload)
    channel_file = (tmp_path / "channel.whl").absolute()
    channel_file.write_bytes(payload)
    cache = tmp_path / "cache"
    outcome = populate_cache_from_local_file(artifact, channel_file, "local", cache)
    assert outcome.reused is False
    receipt = json.loads(
        cache_receipt_path(cache_artifact_path(cache, artifact)).read_text(encoding="utf-8")
    )
    assert receipt == {"schema_version": 1, "sha256": artifact.sha256, "size": artifact.size}


def test_populate_component_cli_guards(tmp_path: Path) -> None:
    import argparse

    from scripts import bootstrap as bootstrap_script

    cache = tmp_path / "cache"
    cache.mkdir()
    source = (tmp_path / "artifact.whl").absolute()
    source.write_bytes(b"0123456789")
    # Dangling --populate-component without --populate-cache must fail.
    dangling = argparse.Namespace(
        operation_status=None,
        managed_root=None,
        core_wheel=None,
        external_components=None,
        external_funasr_python=None,
        local_component_bundle=None,
        component_cache=cache,
        apply_components=False,
        approved_plan_hash=None,
        operation_id=None,
        component_health=False,
        target_platform=None,
        target_architecture=None,
        verify_components=False,
        include_audalign=False,
        include_bbc_audio_offset_finder=False,
        populate_cache=False,
        populate_component="model_punc",
        populate_filename=None,
        populate_source=None,
        populate_tier=None,
        install_dir=tmp_path,
    )
    with pytest.raises(RuntimeError, match="require --populate-cache"):
        bootstrap_script._execute_bootstrap_args(dangling)
    # --populate-component with --operation-status must fail (pure read).
    conflict = argparse.Namespace(
        operation_status="op_dummy",
        managed_root=None,
        core_wheel=None,
        external_components=None,
        external_funasr_python=None,
        local_component_bundle=None,
        component_cache=None,
        apply_components=False,
        approved_plan_hash=None,
        operation_id=None,
        component_health=False,
        target_platform=None,
        target_architecture=None,
        verify_components=False,
        include_audalign=False,
        include_bbc_audio_offset_finder=False,
        populate_cache=False,
        populate_component="model_punc",
        populate_filename=None,
        populate_source=None,
        populate_tier=None,
        install_dir=tmp_path,
    )
    with pytest.raises(RuntimeError, match="pure read"):
        bootstrap_script._execute_bootstrap_args(conflict)
