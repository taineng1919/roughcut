from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from roughcut.adapters.ffmpeg.proxy import ProxyVerificationReport
from roughcut.adapters.project_store import ProjectStore
from roughcut.application.projects import create_project
from roughcut.application.proxies import create_proxy, read_proxy
from roughcut.application.sources import fingerprint_file
from roughcut.domain.project import ImportMode, MediaProbe, ProjectError, SourceAsset
from roughcut.domain.proxy import derive_proxy_profile, proxy_cache_key
from roughcut.domain.render import ToolResolution


def _setup_project(tmp_path: Path) -> tuple[Path, Path, SourceAsset]:
    project_path = tmp_path / "代理 项目"
    source_path = tmp_path / "中文 source.mp4"
    source_path.write_bytes(b"source-fixture")
    project = create_project(project_path, "Proxy")
    source = SourceAsset(
        source_id="src_proxy",
        kind="video",
        display_name=source_path.name,
        import_mode=ImportMode.LINKED,
        locator={"absolute_path": str(source_path.resolve())},
        fingerprint=fingerprint_file(source_path),
        probe=MediaProbe(
            duration_ticks=240_000,
            container_start_ticks=0,
            first_content_ticks=0,
            video_codec="h264",
            width=640,
            height=360,
            nominal_frame_rate={"numerator": 25, "denominator": 1},
            is_vfr=False,
            audio_codec="aac",
            audio_sample_rate=48_000,
            rotation_degrees=0,
        ),
    )
    ProjectStore(project_path).save(replace(project, sources=(source,)), expected_revision=0)
    return project_path, source_path, source


def _report() -> ProxyVerificationReport:
    return ProxyVerificationReport(
        duration_ticks=240_000,
        checks={"verified": True},
        probe={"format": "mp4", "video_codec": "h264"},
        padded_video_frames=0,
        padded_audio_samples=0,
        leading_video_frames=0,
        leading_audio_samples=0,
    )


@pytest.fixture
def fake_proxy_tools(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    calls = {"transcode": 0, "verify": 0}
    ffmpeg = ToolResolution("ffmpeg", "/fixture/ffmpeg", "ffmpeg version fixture")
    ffprobe = ToolResolution("ffprobe", "/fixture/ffprobe", "ffprobe version fixture")
    monkeypatch.setattr(
        "roughcut.application.proxies.resolve_proxy_tools", lambda: (ffmpeg, ffprobe)
    )
    monkeypatch.setattr("roughcut.application.proxies.resolve_proxy_ffprobe", lambda: ffprobe)
    monkeypatch.setattr(
        "roughcut.application.proxies.reject_unsupported_color",
        lambda *_args, **_kwargs: None,
    )

    def transcode(*_args: object, output_path: Path, **_kwargs: object) -> None:
        calls["transcode"] += 1
        output_path.write_bytes(b"proxy-fixture")

    def verify(*_args: object, **_kwargs: object) -> ProxyVerificationReport:
        calls["verify"] += 1
        return _report()

    monkeypatch.setattr("roughcut.application.proxies.transcode_proxy", transcode)
    monkeypatch.setattr("roughcut.application.proxies.verify_proxy_output", verify)
    return calls


def test_create_publishes_ready_cache_without_project_revision_and_reuses_it(
    tmp_path: Path,
    fake_proxy_tools: dict[str, int],
) -> None:
    project_path, _source_path, source = _setup_project(tmp_path)

    created = create_proxy(project_path, source_id=source.source_id, expected_revision=0)
    manifest_path = project_path / created.state.manifest_relative_path
    output_path = project_path / created.state.proxy_relative_path
    manifest_mtime = manifest_path.stat().st_mtime_ns
    output_mtime = output_path.stat().st_mtime_ns
    reused = create_proxy(project_path, source_id=source.source_id, expected_revision=0)

    assert created.reused is False
    assert created.state.reused is False
    assert created.to_dict()["reused"] is False
    assert reused.reused is True
    assert reused.state.reused is True
    assert reused.state.status == "ready"
    assert ProjectStore(project_path).load().revision == 0
    assert fake_proxy_tools["transcode"] == 1
    assert manifest_path.stat().st_mtime_ns == manifest_mtime
    assert output_path.stat().st_mtime_ns == output_mtime


def test_source_fingerprint_change_is_stale_and_create_refuses_it(
    tmp_path: Path,
    fake_proxy_tools: dict[str, int],
) -> None:
    project_path, source_path, source = _setup_project(tmp_path)
    create_proxy(project_path, source_id=source.source_id, expected_revision=0)
    source_path.write_bytes(b"changed-source")

    state = read_proxy(project_path, source_id=source.source_id, expected_revision=0)

    assert state.status == "stale"
    assert state.reason == "source_fingerprint_changed"
    with pytest.raises(ProjectError, match="fingerprint"):
        create_proxy(project_path, source_id=source.source_id, expected_revision=0)
    assert fake_proxy_tools["transcode"] == 1


def test_unknown_source_stale_revision_and_missing_source_are_rejected(
    tmp_path: Path,
    fake_proxy_tools: dict[str, int],
) -> None:
    project_path, source_path, source = _setup_project(tmp_path)
    with pytest.raises(ProjectError, match="not part"):
        create_proxy(project_path, source_id="src_unknown", expected_revision=0)
    with pytest.raises(ProjectError, match="revision"):
        create_proxy(project_path, source_id=source.source_id, expected_revision=1)
    with pytest.raises(ProjectError, match="revision"):
        read_proxy(project_path, source_id=source.source_id, expected_revision=1)

    source_path.unlink()
    assert (
        read_proxy(project_path, source_id=source.source_id, expected_revision=0).reason
        == "source_unavailable"
    )
    with pytest.raises(ProjectError, match="missing|unreadable"):
        create_proxy(project_path, source_id=source.source_id, expected_revision=0)


def test_non_media_project_mutation_keeps_the_same_ready_cache(
    tmp_path: Path,
    fake_proxy_tools: dict[str, int],
) -> None:
    project_path, _source_path, source = _setup_project(tmp_path)
    created = create_proxy(project_path, source_id=source.source_id, expected_revision=0)
    store = ProjectStore(project_path)
    project = store.load()
    tagged = replace(source, tags=("采访",), note="仅编辑元数据")
    store.save(replace(project, revision=1, sources=(tagged,)), expected_revision=0)

    reused = create_proxy(project_path, source_id=source.source_id, expected_revision=1)

    assert reused.reused is True
    assert reused.state.cache_key == created.state.cache_key
    assert reused.state.project_revision == 1
    assert fake_proxy_tools["transcode"] == 1


def test_invalid_manifest_and_symlink_output_are_never_reused(
    tmp_path: Path,
    fake_proxy_tools: dict[str, int],
) -> None:
    project_path, _source_path, source = _setup_project(tmp_path)
    created = create_proxy(project_path, source_id=source.source_id, expected_revision=0)
    output = project_path / created.state.proxy_relative_path
    external = tmp_path / "external.mp4"
    external.write_bytes(b"proxy-fixture")
    output.unlink()
    output.symlink_to(external)

    state = read_proxy(project_path, source_id=source.source_id, expected_revision=0)
    assert state.status == "invalid"
    assert state.reason == "output_symlink"

    output.unlink()
    output.write_bytes(b"proxy-fixture")
    manifest_path = project_path / created.state.manifest_relative_path
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["output"]["relative_path"] = "../escape.mp4"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    state = read_proxy(project_path, source_id=source.source_id, expected_revision=0)
    assert state.status == "invalid"
    assert state.reason == "manifest_invalid"


@pytest.mark.parametrize("failure", [RuntimeError("transcode"), KeyboardInterrupt()])
def test_generation_failure_cleans_candidate_and_leaves_no_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_proxy_tools: dict[str, int],
    failure: BaseException,
) -> None:
    project_path, _source_path, source = _setup_project(tmp_path)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr("roughcut.application.proxies.transcode_proxy", fail)
    with pytest.raises(type(failure)):
        create_proxy(project_path, source_id=source.source_id, expected_revision=0)

    proxy_root = project_path / "proxies" / source.source_id
    assert not list(proxy_root.glob(".candidate-*"))
    assert not list(proxy_root.glob("*/manifest.json"))
    assert ProjectStore(project_path).load().revision == 0


def test_verification_and_manifest_failures_leave_no_orphans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_proxy_tools: dict[str, int],
) -> None:
    project_path, _source_path, source = _setup_project(tmp_path)

    monkeypatch.setattr(
        "roughcut.application.proxies.verify_proxy_output",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("verify")),
    )
    with pytest.raises(RuntimeError, match="verify"):
        create_proxy(project_path, source_id=source.source_id, expected_revision=0)

    monkeypatch.setattr(
        "roughcut.application.proxies.verify_proxy_output", lambda *_a, **_k: _report()
    )
    monkeypatch.setattr(
        "roughcut.application.proxies._write_manifest",
        lambda *_args: (_ for _ in ()).throw(OSError("manifest")),
    )
    with pytest.raises(OSError, match="manifest"):
        create_proxy(project_path, source_id=source.source_id, expected_revision=0)

    root = project_path / "proxies" / source.source_id
    assert not list(root.glob(".candidate-*"))
    assert not list(root.glob("*/proxy.mp4"))
    assert not list(root.glob("*/manifest.json"))


def test_manifest_publish_failure_removes_just_published_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_proxy_tools: dict[str, int],
) -> None:
    project_path, _source_path, source = _setup_project(tmp_path)
    real_replace = __import__("os").replace
    calls = 0

    def fail_second(source_path: Path, destination_path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("publish manifest")
        real_replace(source_path, destination_path)

    monkeypatch.setattr("roughcut.application.proxies.os.replace", fail_second)
    with pytest.raises(OSError, match="publish manifest"):
        create_proxy(project_path, source_id=source.source_id, expected_revision=0)

    root = project_path / "proxies" / source.source_id
    assert not list(root.glob(".candidate-*"))
    assert not list(root.glob("*/proxy.mp4"))
    assert not list(root.glob("*/manifest.json"))


def test_concurrent_project_change_cleans_candidate_without_overwriting_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_proxy_tools: dict[str, int],
) -> None:
    project_path, _source_path, source = _setup_project(tmp_path)

    def mutate_project(*_args: object, output_path: Path, **_kwargs: object) -> None:
        output_path.write_bytes(b"proxy-fixture")
        store = ProjectStore(project_path)
        current = store.load()
        store.save(replace(current, revision=1), expected_revision=0)

    monkeypatch.setattr("roughcut.application.proxies.transcode_proxy", mutate_project)
    with pytest.raises(ProjectError, match="changed during"):
        create_proxy(project_path, source_id=source.source_id, expected_revision=0)

    assert ProjectStore(project_path).load().revision == 1
    root = project_path / "proxies" / source.source_id
    assert not list(root.glob(".candidate-*"))
    assert not list(root.glob("*/manifest.json"))


def test_failed_new_cache_generation_does_not_touch_previous_ready_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_proxy_tools: dict[str, int],
) -> None:
    project_path, _source_path, source = _setup_project(tmp_path)
    ready = create_proxy(project_path, source_id=source.source_id, expected_revision=0)
    ready_manifest = project_path / ready.state.manifest_relative_path
    ready_bytes = ready_manifest.read_bytes()
    store = ProjectStore(project_path)
    project = store.load()
    changed_settings = {**project.settings, "frame_rate": {"numerator": 30, "denominator": 1}}
    store.save(replace(project, revision=1, settings=changed_settings), expected_revision=0)
    monkeypatch.setattr(
        "roughcut.application.proxies.transcode_proxy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("new cache")),
    )

    with pytest.raises(RuntimeError, match="new cache"):
        create_proxy(project_path, source_id=source.source_id, expected_revision=1)

    assert ready_manifest.read_bytes() == ready_bytes
    assert not list(ready_manifest.parents[1].glob(".candidate-*"))


@pytest.mark.parametrize("link_level", ["proxies", "source", "cache", "broken-cache"])
def test_proxy_cache_tree_symlinks_never_escape_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_proxy_tools: dict[str, int],
    link_level: str,
) -> None:
    project_path, _source_path, source = _setup_project(tmp_path)
    project = ProjectStore(project_path).load()
    profile = derive_proxy_profile(source.probe, project.settings)
    cache_key = proxy_cache_key(source.fingerprint, source.probe, profile)
    proxies = project_path / "proxies"
    source_cache = proxies / source.source_id
    cache = source_cache / cache_key
    external = tmp_path / "external"
    external.mkdir()
    if link_level == "proxies":
        proxies.symlink_to(external, target_is_directory=True)
    elif link_level == "source":
        proxies.mkdir()
        source_cache.symlink_to(external, target_is_directory=True)
    else:
        source_cache.mkdir(parents=True)
        target = external if link_level == "cache" else tmp_path / "missing-external"
        cache.symlink_to(target, target_is_directory=True)

    state = read_proxy(project_path, source_id=source.source_id, expected_revision=0)
    assert state.status == "invalid"
    with pytest.raises(ProjectError, match="proxy cache path"):
        create_proxy(project_path, source_id=source.source_id, expected_revision=0)
    assert list(external.iterdir()) == []
    assert fake_proxy_tools["transcode"] == 0
    assert ProjectStore(project_path).load().revision == 0


@pytest.mark.parametrize("leaf", ["manifest", "output"])
def test_proxy_manifest_and_output_symlinks_are_rejected_before_external_access(
    tmp_path: Path,
    fake_proxy_tools: dict[str, int],
    leaf: str,
) -> None:
    project_path, _source_path, source = _setup_project(tmp_path)
    created = create_proxy(project_path, source_id=source.source_id, expected_revision=0)
    manifest = project_path / created.state.manifest_relative_path
    output = project_path / created.state.proxy_relative_path
    external = tmp_path / f"external-{leaf}"
    external.write_bytes(b"must-not-change")
    target = manifest if leaf == "manifest" else output
    target.unlink()
    target.symlink_to(external)

    state = read_proxy(project_path, source_id=source.source_id, expected_revision=0)
    assert state.status == "invalid"
    assert external.read_bytes() == b"must-not-change"
    assert fake_proxy_tools["transcode"] == 1
    assert ProjectStore(project_path).load().revision == 0
    assert not list((project_path / "proxies" / source.source_id).glob(".candidate-*"))


def test_project_root_symlink_is_rejected_before_cache_access(
    tmp_path: Path,
    fake_proxy_tools: dict[str, int],
) -> None:
    project_path, _source_path, source = _setup_project(tmp_path)
    alias = tmp_path / "project-alias"
    alias.symlink_to(project_path, target_is_directory=True)

    with pytest.raises(ProjectError, match="project root"):
        read_proxy(alias, source_id=source.source_id, expected_revision=0)
    with pytest.raises(ProjectError, match="project root"):
        create_proxy(alias, source_id=source.source_id, expected_revision=0)
    assert fake_proxy_tools["transcode"] == 0


def test_publish_rechecks_cache_target_after_transcode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_proxy_tools: dict[str, int],
) -> None:
    project_path, _source_path, source = _setup_project(tmp_path)
    project = ProjectStore(project_path).load()
    cache_key = proxy_cache_key(
        source.fingerprint,
        source.probe,
        derive_proxy_profile(source.probe, project.settings),
    )
    target = project_path / "proxies" / source.source_id / cache_key
    external = tmp_path / "publish-external"
    external.mkdir()

    def transcode(*_args: object, output_path: Path, **_kwargs: object) -> None:
        output_path.write_bytes(b"proxy-fixture")
        target.symlink_to(external, target_is_directory=True)

    monkeypatch.setattr("roughcut.application.proxies.transcode_proxy", transcode)
    with pytest.raises(ProjectError, match="proxy cache"):
        create_proxy(project_path, source_id=source.source_id, expected_revision=0)

    assert list(external.iterdir()) == []
    assert not list(target.parent.glob(".candidate-*"))
    assert ProjectStore(project_path).load().revision == 0
