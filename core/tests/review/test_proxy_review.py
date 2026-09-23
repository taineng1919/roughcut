from __future__ import annotations

import http.client
import json
from dataclasses import replace
from pathlib import Path

import pytest

from roughcut.adapters.artifact_store import write_new_json
from roughcut.adapters.ffmpeg.proxy import ProxyVerificationReport
from roughcut.adapters.project_store import ProjectStore
from roughcut.application.agent_context import create_edit_brief, read_agent_context
from roughcut.application.preview import load_review_snapshot
from roughcut.application.projects import create_project
from roughcut.application.proposals import confirm_edit_proposal, create_edit_proposal
from roughcut.application.renders import create_render_plan
from roughcut.application.sources import fingerprint_file
from roughcut.domain.project import ImportMode, MediaProbe, SourceAsset
from roughcut.domain.proxy import ProxyManifest, ProxyOutput, derive_proxy_profile, proxy_cache_key
from roughcut.domain.render import ToolResolution
from roughcut.domain.transcript import TimedTranscript, TranscriptProvenance, TranscriptSegment
from roughcut.review.server import start_review_server


@pytest.fixture
def lightweight_proxy_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "roughcut.application.proxies.resolve_proxy_ffprobe",
        lambda: ToolResolution("ffprobe", "/fixture/ffprobe", "ffprobe fixture"),
    )

    def verify(*_args: object, **_kwargs: object) -> ProxyVerificationReport:
        return ProxyVerificationReport(
            duration_ticks=1_200_000,
            checks={"fixture": True},
            probe={"fixture": True},
            padded_video_frames=0,
            padded_audio_samples=0,
        )

    monkeypatch.setattr("roughcut.application.proxies.verify_proxy_output", verify)


def _publish_ready_proxy(project_path: Path, source_id: str, body: bytes) -> tuple[Path, Path]:
    project = ProjectStore(project_path).load()
    source = next(source for source in project.sources if source.source_id == source_id)
    profile = derive_proxy_profile(source.probe, project.settings)
    cache_key = proxy_cache_key(source.fingerprint, source.probe, profile)
    directory = project_path / "proxies" / source_id / cache_key
    directory.mkdir(parents=True)
    output = directory / "proxy.mp4"
    output.write_bytes(body)
    fingerprint = fingerprint_file(output)
    manifest = ProxyManifest(
        source_id=source_id,
        cache_key=cache_key,
        source_fingerprint=source.fingerprint,
        source_probe=source.probe,
        profile=profile,
        output=ProxyOutput(
            relative_path=f"proxies/{source_id}/{cache_key}/proxy.mp4",
            size=fingerprint.size,
            sha256_head_tail=fingerprint.sha256_head_tail,
            duration_ticks=source.probe.duration_ticks,
        ),
        tools={"ffmpeg_version": "fixture", "ffprobe_version": "fixture"},
        checks={"fixture": True},
    )
    manifest_path = directory / "manifest.json"
    write_new_json(manifest_path, manifest.to_dict())
    return output, manifest_path


def _review_project(tmp_path: Path) -> tuple[Path, str, Path]:
    project_path = tmp_path / "proxy review project"
    media = tmp_path / "original-media.mp4"
    media.write_bytes(bytes(range(256)) * 8)
    project = create_project(project_path, "Proxy Review")
    source = SourceAsset(
        source_id="src_review",
        kind="video",
        display_name=media.name,
        import_mode=ImportMode.LINKED,
        locator={"absolute_path": str(media.resolve())},
        fingerprint=fingerprint_file(media),
        probe=MediaProbe(
            1_200_000,
            0,
            0,
            "h264",
            320,
            180,
            {"numerator": 25, "denominator": 1},
            False,
            "aac",
            48_000,
            0,
        ),
    )
    transcript = TimedTranscript(
        1,
        "tr_review",
        source.source_id,
        None,
        TranscriptProvenance("fixture", "1", {}, {}, "raw-asr/fixture.json", "a", "b", 0),
        "zh-CN",
        (
            TranscriptSegment(
                "seg_review",
                0,
                1_200_000,
                "代理审阅。",
                None,
                None,
                None,
                None,
                (),
                "unmarked",
            ),
        ),
    )
    write_new_json(
        project_path / "transcripts" / source.source_id / "tr_review.json",
        transcript.to_dict(),
    )
    ProjectStore(project_path).save(
        replace(
            project,
            revision=1,
            sources=(source,),
            active_transcript_versions={source.source_id: "tr_review"},
        ),
        expected_revision=0,
    )
    brief = create_edit_brief(
        project_path,
        theme="代理审阅",
        target_duration_ticks=1_200_000,
        focus=["完整片段"],
        allow_reorder=True,
        expected_revision=1,
    )
    context = read_agent_context(
        project_path,
        source_id=source.source_id,
        transcript_version_id="tr_review",
        brief_id=brief.brief.brief_id,
        expected_revision=brief.project_revision,
        offset=0,
        limit=1,
    )
    proposal = create_edit_proposal(
        project_path,
        source_id=source.source_id,
        transcript_version_id="tr_review",
        brief_id=brief.brief.brief_id,
        context_hash=context.context_hash,
        clips=[
            {
                "clip_id": "clip_review",
                "source_id": source.source_id,
                "transcript_version_id": "tr_review",
                "segment_id": "seg_review",
                "source_in_ticks": 0,
                "source_out_ticks": 1_200_000,
                "reason": "fixture",
                "display_text": "代理审阅。",
            }
        ],
        total_duration_ticks=1_200_000,
        expected_revision=brief.project_revision,
    )
    return project_path, proposal.proposal.proposal_id, media


def _request(
    port: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str],
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    connection.request(method, path, headers=headers)
    response = connection.getresponse()
    body = response.read()
    response_headers = {key.lower(): value for key, value in response.getheaders()}
    connection.close()
    return response.status, response_headers, body


def _static(tmp_path: Path) -> Path:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("review", encoding="utf-8")
    return static


def test_schema_one_snapshot_selects_ready_proxy_without_leaking_cache_paths(
    tmp_path: Path,
    lightweight_proxy_validation: None,
) -> None:
    project, proposal_id, _media = _review_project(tmp_path)
    _publish_ready_proxy(project, "src_review", b"proxy-body")

    payload = load_review_snapshot(project, proposal_id=proposal_id).to_dict()

    source = payload["source"]
    assert isinstance(source, dict)
    assert source["playback_kind"] == "proxy"
    assert source["media_url"] == "/media/src_review"
    assert source["proxy_profile"] == {
        "canvas": {"width": 1280, "height": 720},
        "frame_rate": {"numerator": 25, "denominator": 1},
        "has_audio": True,
    }
    serialized = json.dumps(payload)
    assert "manifest.json" not in serialized
    assert "proxy.mp4" not in serialized
    assert "cache_key" not in serialized
    assert str(project) not in serialized


@pytest.mark.parametrize("cache_state", ["missing", "stale", "invalid"])
def test_schema_one_snapshot_falls_back_to_original_for_nonready_proxy(
    tmp_path: Path,
    lightweight_proxy_validation: None,
    cache_state: str,
) -> None:
    project, proposal_id, _media = _review_project(tmp_path)
    if cache_state == "invalid":
        _output, manifest = _publish_ready_proxy(project, "src_review", b"proxy-body")
        manifest.write_text("not-json", encoding="utf-8")
    elif cache_state == "stale":
        output, _manifest = _publish_ready_proxy(project, "src_review", b"proxy-body")
        output.parent.rename(output.parent.parent / ("0" * 64))

    payload = load_review_snapshot(project, proposal_id=proposal_id).to_dict()

    source = payload["source"]
    assert isinstance(source, dict)
    assert source["playback_kind"] == "original"
    assert source["proxy_profile"] is None


def test_proxy_media_uses_same_url_and_supports_get_head_range_and_416(
    tmp_path: Path,
    lightweight_proxy_validation: None,
) -> None:
    project, proposal_id, _media = _review_project(tmp_path)
    proxy_body = bytes(range(256)) * 12
    _publish_ready_proxy(project, "src_review", proxy_body)

    with start_review_server(
        project, proposal_id=proposal_id, static_root=_static(tmp_path)
    ) as review:
        headers = {"X-Roughcut-Token": review.token}
        status, response_headers, body = _request(
            review.port, "GET", "/media/src_review", headers=headers
        )
        assert status == 200
        assert body == proxy_body
        assert response_headers["content-type"] == "video/mp4"
        assert _request(review.port, "HEAD", "/media/src_review", headers=headers)[0] == 200
        status, response_headers, body = _request(
            review.port,
            "GET",
            "/media/src_review",
            headers={**headers, "Range": "bytes=100-199"},
        )
        assert status == 206
        assert body == proxy_body[100:200]
        assert response_headers["content-range"] == f"bytes 100-199/{len(proxy_body)}"
        assert (
            _request(
                review.port,
                "GET",
                "/media/src_review",
                headers={**headers, "Range": "bytes=999999-"},
            )[0]
            == 416
        )


def test_proxy_selection_does_not_fallback_after_loss_or_switch_after_creation(
    tmp_path: Path,
    lightweight_proxy_validation: None,
) -> None:
    project, proposal_id, original = _review_project(tmp_path)
    static = _static(tmp_path)

    with start_review_server(
        project, proposal_id=proposal_id, static_root=static
    ) as original_review:
        headers = {"X-Roughcut-Token": original_review.token}
        _publish_ready_proxy(project, "src_review", b"late-proxy")
        assert (
            json.loads(_request(original_review.port, "GET", "/api/review", headers=headers)[2])[
                "source"
            ]["playback_kind"]
            == "original"
        )
        assert (
            _request(original_review.port, "GET", "/media/src_review", headers=headers)[2]
            == original.read_bytes()
        )

    with start_review_server(project, proposal_id=proposal_id, static_root=static) as proxy_review:
        headers = {"X-Roughcut-Token": proxy_review.token}
        payload = json.loads(_request(proxy_review.port, "GET", "/api/review", headers=headers)[2])
        assert payload["source"]["playback_kind"] == "proxy"
        store = ProjectStore(project)
        current = store.load()
        store.save(
            replace(current, revision=current.revision + 1, name="non-media change"),
            expected_revision=current.revision,
        )
        assert _request(
            proxy_review.port, "GET", "/media/src_review", headers=headers
        )[2] == b"late-proxy"
        proxy_path = next((project / "proxies" / "src_review").glob("*/proxy.mp4"))
        proxy_path.unlink()
        status, response_headers, body = _request(
            proxy_review.port, "GET", "/media/src_review", headers=headers
        )
        assert status == 409
        assert response_headers["content-type"].startswith("application/json")
        error = json.loads(body)["error"]
        assert error["code"] == "proxy_unavailable"
        assert b"late-proxy" not in body
        assert original.read_bytes() not in body


@pytest.mark.parametrize("leaf", ["manifest", "output", "replaced-manifest", "replaced-output"])
def test_frozen_proxy_rejects_symlinked_or_replaced_artifacts(
    tmp_path: Path,
    lightweight_proxy_validation: None,
    leaf: str,
) -> None:
    project, proposal_id, _media = _review_project(tmp_path)
    output, manifest = _publish_ready_proxy(project, "src_review", b"proxy-body")
    external = tmp_path / f"external-{leaf}"
    external.write_bytes(b"external")

    with start_review_server(
        project, proposal_id=proposal_id, static_root=_static(tmp_path)
    ) as review:
        target = manifest if "manifest" in leaf else output
        if leaf.startswith("replaced-"):
            target.write_bytes(target.read_bytes() + b" ")
        else:
            target.unlink()
            target.symlink_to(external)
        status, _headers, body = _request(
            review.port,
            "GET",
            "/media/src_review",
            headers={"X-Roughcut-Token": review.token},
        )
        assert status == 409
        assert json.loads(body)["error"]["code"] == "proxy_unavailable"
        assert external.read_bytes() == b"external"


def test_formal_render_plan_still_freezes_registered_original_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lightweight_proxy_validation: None,
) -> None:
    project, proposal_id, original = _review_project(tmp_path)
    _publish_ready_proxy(project, "src_review", b"proxy-body")
    decision = confirm_edit_proposal(project, proposal_id, expected_revision=2)
    ffmpeg = ToolResolution("ffmpeg", "/fixture/ffmpeg", "ffmpeg fixture")
    ffprobe = ToolResolution("ffprobe", "/fixture/ffprobe", "ffprobe fixture")
    monkeypatch.setattr(
        "roughcut.application.renders.resolve_render_tools",
        lambda: (ffmpeg, ffprobe),
    )

    plan = create_render_plan(
        project,
        edit_version_id=decision.decision.edit_version_id,
        expected_revision=decision.project_revision,
    )

    assert plan.source.locator == {"absolute_path": str(original.resolve())}
    assert "proxies" not in json.dumps(plan.to_dict())
