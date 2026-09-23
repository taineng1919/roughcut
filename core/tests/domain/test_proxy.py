from __future__ import annotations

from dataclasses import replace

import pytest

from roughcut.domain.project import MediaProbe, ProjectError, SourceFingerprint
from roughcut.domain.proxy import (
    ProxyManifest,
    ProxyOutput,
    derive_proxy_profile,
    proxy_cache_key,
)


def _probe(**changes: object) -> MediaProbe:
    values: dict[str, object] = {
        "duration_ticks": 1_200_000,
        "container_start_ticks": 12_000,
        "first_content_ticks": 24_000,
        "video_codec": "h264",
        "width": 1920,
        "height": 1080,
        "nominal_frame_rate": {"numerator": 30, "denominator": 1},
        "is_vfr": True,
        "audio_codec": "aac",
        "audio_sample_rate": 44_100,
        "rotation_degrees": 0,
    }
    values.update(changes)
    return MediaProbe(**values)  # type: ignore[arg-type]


def _settings() -> dict[str, object]:
    return {
        "timebase": 120_000,
        "frame_rate": {"numerator": 25, "denominator": 1},
        "width": 1920,
        "height": 1080,
        "audio_sample_rate": 48_000,
    }


def test_cache_key_is_canonical_and_excludes_mutable_project_and_tool_state() -> None:
    fingerprint = SourceFingerprint(123, 456, "a" * 64)
    profile = derive_proxy_profile(_probe(), _settings())

    first = proxy_cache_key(fingerprint, _probe(), profile)
    second = proxy_cache_key(fingerprint, _probe(), profile)

    assert first == second
    assert len(first) == 64
    assert "locator" not in profile.to_dict()
    assert "project_revision" not in profile.to_dict()
    assert "ffmpeg" not in profile.to_dict()


def test_source_fingerprint_probe_or_profile_change_invalidates_cache_key() -> None:
    fingerprint = SourceFingerprint(123, 456, "a" * 64)
    probe = _probe()
    profile = derive_proxy_profile(probe, _settings())
    baseline = proxy_cache_key(fingerprint, probe, profile)

    assert proxy_cache_key(replace(fingerprint, size=124), probe, profile) != baseline
    changed_probe = _probe(duration_ticks=1_200_001)
    assert proxy_cache_key(fingerprint, changed_probe, profile) != baseline
    changed_settings = {**_settings(), "width": 1440, "height": 1080}
    changed_profile = derive_proxy_profile(probe, changed_settings)
    assert proxy_cache_key(fingerprint, probe, changed_profile) != baseline


@pytest.mark.parametrize(
    "relative_path",
    ["/private/proxy.mp4", "../proxy.mp4", "proxies/src/../../proxy.mp4", "C:/proxy.mp4"],
)
def test_manifest_rejects_absolute_or_escaping_output_paths(relative_path: str) -> None:
    probe = _probe()
    fingerprint = SourceFingerprint(123, 456, "a" * 64)
    profile = derive_proxy_profile(probe, _settings())
    cache_key = proxy_cache_key(fingerprint, probe, profile)

    with pytest.raises(ProjectError):
        ProxyManifest(
            source_id="src_fixture",
            cache_key=cache_key,
            source_fingerprint=fingerprint,
            source_probe=probe,
            profile=profile,
            output=ProxyOutput(
                relative_path=relative_path,
                size=123,
                sha256_head_tail="b" * 64,
                duration_ticks=probe.duration_ticks,
            ),
            tools={"ffmpeg_version": "fixture", "ffprobe_version": "fixture"},
            checks={"verified": True},
        )


def test_ready_manifest_roundtrip_rejects_old_schema_or_wrong_cache_identity() -> None:
    probe = _probe()
    fingerprint = SourceFingerprint(123, 456, "a" * 64)
    profile = derive_proxy_profile(probe, _settings())
    cache_key = proxy_cache_key(fingerprint, probe, profile)
    manifest = ProxyManifest(
        source_id="src_fixture",
        cache_key=cache_key,
        source_fingerprint=fingerprint,
        source_probe=probe,
        profile=profile,
        output=ProxyOutput(
            relative_path=f"proxies/src_fixture/{cache_key}/proxy.mp4",
            size=123,
            sha256_head_tail="b" * 64,
            duration_ticks=probe.duration_ticks,
        ),
        tools={"ffmpeg_version": "fixture", "ffprobe_version": "fixture"},
        checks={"verified": True},
    )
    assert ProxyManifest.from_dict(manifest.to_dict()) == manifest
    assert manifest.to_dict()["leading_padding"] == {
        "video_frames": 0,
        "audio_samples": 0,
    }

    old_schema = manifest.to_dict()
    old_schema["schema_version"] = 0
    with pytest.raises(ProjectError, match="supported|schema"):
        ProxyManifest.from_dict(old_schema)

    wrong_cache = manifest.to_dict()
    wrong_cache["cache_key"] = "c" * 64
    with pytest.raises(ProjectError, match="path|cache"):
        ProxyManifest.from_dict(wrong_cache)
