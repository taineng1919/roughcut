from __future__ import annotations

import http.client
import json
from dataclasses import replace
from pathlib import Path

import pytest

from roughcut.adapters.artifact_store import write_new_json
from roughcut.adapters.ffmpeg.proxy import ProxyVerificationReport
from roughcut.adapters.project_store import ProjectStore
from roughcut.application.agent_context import (
    create_edit_brief,
    read_multi_source_agent_context,
)
from roughcut.application.preview import load_review_snapshot
from roughcut.application.projects import create_project
from roughcut.application.proposals import (
    confirm_multi_source_edit_proposal,
    create_multi_source_edit_proposal,
)
from roughcut.application.sources import fingerprint_file
from roughcut.domain.people import Person, SpeakerMap
from roughcut.domain.project import (
    ImportMode,
    MediaProbe,
    ProjectError,
    SourceAsset,
)
from roughcut.domain.proxy import ProxyManifest, ProxyOutput, derive_proxy_profile, proxy_cache_key
from roughcut.domain.render import ToolResolution
from roughcut.domain.transcript import (
    TimedTranscript,
    TranscriptProvenance,
    TranscriptSegment,
)
from roughcut.review.server import start_review_server


def _source(source_id: str, media: Path, *, tags: tuple[str, ...], note: str) -> SourceAsset:
    return SourceAsset(
        source_id=source_id,
        kind="video",
        display_name=media.name,
        import_mode=ImportMode.LINKED,
        locator={"absolute_path": str(media.resolve())},
        fingerprint=fingerprint_file(media),
        probe=MediaProbe(
            duration_ticks=360_000,
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
        tags=tags,
        note=note,
    )


def _transcript(source_id: str, transcript_id: str, prefix: str) -> TimedTranscript:
    return TimedTranscript(
        schema_version=1,
        transcript_version_id=transcript_id,
        source_id=source_id,
        parent_version_id=None,
        provenance=TranscriptProvenance(
            "fixture",
            "1",
            {},
            {},
            f"raw-asr/{source_id}/private.json",
            "fixture",
            "fixture",
            0,
        ),
        language="zh-CN",
        segments=(
            TranscriptSegment(
                "seg_shared",
                0,
                120_000,
                f"{prefix} 第一段。",
                None,
                "spk_0",
                None,
                None,
                (),
                "unmarked",
            ),
            TranscriptSegment(
                f"seg_{source_id}_return",
                120_000,
                240_000,
                f"{prefix} 第二段。",
                None,
                "spk_0",
                None,
                None,
                (),
                "unmarked",
            ),
        ),
    )


def _multisource_project(tmp_path: Path, *, confirm: bool = True) -> dict[str, object]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    project_path = tmp_path / "多素材 review project"
    media_a = tmp_path / "素材 A.mp4"
    media_b = tmp_path / "素材 B.mp4"
    media_c = tmp_path / "未授权素材 C.mp4"
    media_a.write_bytes(bytes(range(256)) * 8)
    media_b.write_bytes(bytes(reversed(range(256))) * 8)
    media_c.write_bytes(b"not authorized")
    project = create_project(project_path, "多素材 Review")
    transcript_a = _transcript("src_a", "tr_a", "A")
    transcript_b = _transcript("src_b", "tr_b", "B")
    for transcript in (transcript_a, transcript_b):
        write_new_json(
            project_path
            / "transcripts"
            / transcript.source_id
            / f"{transcript.transcript_version_id}.json",
            transcript.to_dict(),
        )
    imported = replace(
        project,
        revision=1,
        sources=(
            _source("src_a", media_a, tags=("校园", "开场"), note="A 素材备注"),
            _source("src_b", media_b, tags=("访谈",), note="B 素材备注"),
            _source("src_c", media_c, tags=(), note="不在 bindings"),
        ),
        active_transcript_versions={"src_a": "tr_a", "src_b": "tr_b"},
        persons=(
            Person("person_a", "人物 A", "guest", ""),
            Person("person_b", "人物 B", "guest", ""),
        ),
        speaker_maps=(
            SpeakerMap("src_a", "tr_a", "spk_0", "person_a", True),
            SpeakerMap("src_b", "tr_b", "spk_0", "person_b", True),
        ),
    )
    ProjectStore(project_path).save(imported, expected_revision=0)
    brief = create_edit_brief(
        project_path,
        theme="A 到 B 再回 A",
        target_duration_ticks=360_000,
        focus=["人物切换"],
        allow_reorder=True,
        expected_revision=1,
    )
    bindings = [
        {"source_id": "src_a", "transcript_version_id": "tr_a"},
        {"source_id": "src_b", "transcript_version_id": "tr_b"},
    ]
    context = read_multi_source_agent_context(
        project_path,
        source_bindings=bindings,
        brief_id=brief.brief.brief_id,
        expected_revision=brief.project_revision,
        offset=0,
        limit=10,
    )
    clips = [
        {
            "clip_id": "clip_a_open",
            "source_id": "src_a",
            "transcript_version_id": "tr_a",
            "segment_id": "seg_shared",
            "source_in_ticks": 0,
            "source_out_ticks": 120_000,
            "reason": "A 开场",
            "display_text": "A 第一段。",
        },
        {
            "clip_id": "clip_b_middle",
            "source_id": "src_b",
            "transcript_version_id": "tr_b",
            "segment_id": "seg_shared",
            "source_in_ticks": 0,
            "source_out_ticks": 120_000,
            "reason": "B 中段",
            "display_text": "B 第一段。",
        },
        {
            "clip_id": "clip_a_return",
            "source_id": "src_a",
            "transcript_version_id": "tr_a",
            "segment_id": "seg_src_a_return",
            "source_in_ticks": 120_000,
            "source_out_ticks": 240_000,
            "reason": "返回 A",
            "display_text": "A 第二段。",
        },
    ]
    proposal = create_multi_source_edit_proposal(
        project_path,
        source_bindings=bindings,
        brief_id=brief.brief.brief_id,
        context_hash=context.context_hash,
        clips=clips,
        total_duration_ticks=360_000,
        expected_revision=brief.project_revision,
    )
    decision = None
    if confirm:
        decision = confirm_multi_source_edit_proposal(
            project_path,
            proposal.proposal.proposal_id,
            expected_revision=brief.project_revision,
        )
    return {
        "project_path": project_path,
        "proposal_id": proposal.proposal.proposal_id,
        "decision_id": decision.decision.edit_version_id if decision is not None else None,
        "media_a": media_a,
        "media_b": media_b,
        "media_c": media_c,
    }


def _request(
    port: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    body: dict[str, object] | None = None,
) -> tuple[int, dict[str, str], dict[str, object] | bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    encoded = json.dumps(body).encode() if body is not None else None
    connection.request(method, path, body=encoded, headers=headers)
    response = connection.getresponse()
    response_body = response.read()
    response_headers = {key.lower(): value for key, value in response.getheaders()}
    connection.close()
    if response_headers.get("content-type", "").startswith("application/json"):
        return response.status, response_headers, json.loads(response_body)
    return response.status, response_headers, response_body


def _publish_proxy(project_path: Path, source_id: str, body: bytes) -> None:
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
    write_new_json(directory / "manifest.json", manifest.to_dict())


def _lightweight_proxy_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "roughcut.application.proxies.resolve_proxy_ffprobe",
        lambda: ToolResolution("ffprobe", "/fixture/ffprobe", "ffprobe fixture"),
    )

    def verify(
        _path: Path,
        probe: MediaProbe,
        *_args: object,
        **_kwargs: object,
    ) -> ProxyVerificationReport:
        return ProxyVerificationReport(
            duration_ticks=probe.duration_ticks,
            checks={"fixture": True},
            probe={"fixture": True},
            padded_video_frames=0,
            padded_audio_samples=0,
        )

    monkeypatch.setattr("roughcut.application.proxies.verify_proxy_output", verify)


def test_schema_two_decision_snapshot_is_ordered_person_resolved_and_path_free(
    tmp_path: Path,
) -> None:
    state = _multisource_project(tmp_path)
    snapshot = load_review_snapshot(
        Path(state["project_path"]),
        edit_version_id=str(state["decision_id"]),
    )
    payload = snapshot.to_dict()

    assert payload["schema_version"] == 2
    assert payload["project"]["revision"] == 3  # type: ignore[index]
    assert [source["source_id"] for source in payload["sources"]] == [  # type: ignore[index]
        "src_a",
        "src_b",
    ]
    assert [source["media_url"] for source in payload["sources"]] == [  # type: ignore[index]
        "/media/src_a",
        "/media/src_b",
    ]
    transcript = payload["transcript"]
    assert isinstance(transcript, list)
    shared = [segment for segment in transcript if segment["segment_id"] == "seg_shared"]
    assert [(segment["source_id"], segment["local_speaker_id"]) for segment in shared] == [
        ("src_a", "spk_0"),
        ("src_b", "spk_0"),
    ]
    assert [segment["person_name"] for segment in shared] == ["人物 A", "人物 B"]
    assert [segment["original_text"] for segment in shared] == ["A 第一段。", "B 第一段。"]
    assert [segment["corrected_text"] for segment in shared] == [None, None]
    assert [span["source_id"] for span in payload["timeline"]["spans"]] == [  # type: ignore[index]
        "src_a",
        "src_b",
        "src_a",
    ]
    serialized = json.dumps(payload, ensure_ascii=False)
    for private in (
        str(state["project_path"]),
        str(state["media_a"]),
        str(state["media_b"]),
        "locator",
        "fingerprint",
        "raw-asr",
    ):
        assert private not in serialized


def test_schema_two_snapshot_and_server_mix_original_proxy_without_changing_timeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _multisource_project(tmp_path)
    project_path = Path(state["project_path"])
    proxy_body = b"schema-two-proxy" * 100
    _publish_proxy(project_path, "src_b", proxy_body)
    _lightweight_proxy_validation(monkeypatch)
    static = tmp_path / "proxy-static"
    static.mkdir()
    (static / "index.html").write_text("review", encoding="utf-8")

    with start_review_server(
        project_path,
        edit_version_id=str(state["decision_id"]),
        static_root=static,
    ) as review:
        headers = {"X-Roughcut-Token": review.token}
        status, _response_headers, payload = _request(
            review.port, "GET", "/api/review", headers=headers
        )
        assert status == 200
        assert isinstance(payload, dict)
        assert [
            (source["source_id"], source["playback_kind"])
            for source in payload["sources"]  # type: ignore[index]
        ] == [("src_a", "original"), ("src_b", "proxy")]
        assert [
            (span["source_id"], span["source_in_ticks"], span["source_out_ticks"])
            for span in payload["timeline"]["spans"]  # type: ignore[index]
        ] == [
            ("src_a", 0, 120_000),
            ("src_b", 0, 120_000),
            ("src_a", 120_000, 240_000),
        ]
        status, response_headers, body = _request(
            review.port,
            "GET",
            "/media/src_b",
            headers={**headers, "Range": "bytes=5-24"},
        )
        assert status == 206
        assert response_headers["content-type"] == "video/mp4"
        assert body == proxy_body[5:25]
        assert _request(review.port, "GET", "/media/src_c", headers=headers)[0] == 404
        assert _request(review.port, "GET", "/media/src_b", headers={})[0] == 403
        assert (
            _request(
                review.port,
                "GET",
                "/media/src_b",
                headers={**headers, "Host": "evil.test"},
            )[0]
            == 403
        )
        assert (
            _request(
                review.port,
                "GET",
                "/media/src_b",
                headers={**headers, "Origin": "https://evil.test"},
            )[0]
            == 403
        )


def test_schema_two_server_authorizes_only_bound_media_and_uses_range(tmp_path: Path) -> None:
    state = _multisource_project(tmp_path)
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("review", encoding="utf-8")
    with start_review_server(
        Path(state["project_path"]),
        edit_version_id=str(state["decision_id"]),
        static_root=static,
    ) as review:
        headers = {"X-Roughcut-Token": review.token}
        for source_id, media_key in (("src_a", "media_a"), ("src_b", "media_b")):
            status, response_headers, body = _request(
                review.port,
                "GET",
                f"/media/{source_id}",
                headers={**headers, "Range": "bytes=10-29"},
            )
            assert status == 206
            assert response_headers["content-range"].startswith("bytes 10-29/")
            assert body == Path(state[media_key]).read_bytes()[10:30]
        assert _request(review.port, "GET", "/media/src_c", headers=headers)[0] == 404


def test_schema_two_legacy_review_business_writes_require_workflow(tmp_path: Path) -> None:
    state = _multisource_project(tmp_path, confirm=False)
    project_path = Path(state["project_path"])
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("review", encoding="utf-8")
    with start_review_server(
        project_path,
        proposal_id=str(state["proposal_id"]),
        static_root=static,
    ) as review:
        headers = {
            "X-Roughcut-Token": review.token,
            "Origin": f"http://127.0.0.1:{review.port}",
            "Content-Type": "application/json",
        }
        status, _headers, payload = _request(review.port, "GET", "/api/review", headers=headers)
        assert status == 200
        assert isinstance(payload, dict)
        clips = payload["proposal"]["clips"]  # type: ignore[index]
        assert isinstance(clips, list)
        edited = [clips[1], clips[0], clips[2]]
        before = ProjectStore(project_path).load()
        status, _headers, created = _request(
            review.port,
            "POST",
            "/api/proposals",
            headers=headers,
            body={"clips": edited},
        )
        assert status == 409
        assert isinstance(created, dict)
        assert created["error"]["code"] == "workflow_transition_not_allowed"  # type: ignore[index]
        assert ProjectStore(project_path).load() == before

        status, _headers, confirmed = _request(
            review.port,
            "POST",
            "/api/confirm",
            headers=headers,
            body={"proposal_id": str(state["proposal_id"])},
        )
        assert status == 400
        assert isinstance(confirmed, dict)
        assert confirmed["error"]["code"] == "workflow_required"  # type: ignore[index]
        assert ProjectStore(project_path).load() == before

def test_schema_two_roughcut_candidate_preserves_a_b_a_and_frozen_playback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _multisource_project(tmp_path)
    project_path = Path(state["project_path"])
    _publish_proxy(project_path, "src_b", b"roughcut-proxy" * 100)
    _lightweight_proxy_validation(monkeypatch)
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("review", encoding="utf-8")
    before = ProjectStore(project_path).load()
    with start_review_server(
        project_path,
        edit_version_id=str(state["decision_id"]),
        static_root=static,
    ) as review:
        headers = {
            "X-Roughcut-Token": review.token,
            "Origin": f"http://127.0.0.1:{review.port}",
            "Content-Type": "application/json",
        }
        status, _headers, payload = _request(
            review.port,
            "GET",
            "/api/roughcut-state",
            headers=headers,
        )
        assert status == 200
        assert isinstance(payload, dict)
        assert [source["playback_kind"] for source in payload["review"]["sources"]] == [  # type: ignore[index]
            "original",
            "proxy",
        ]
        basis_id = payload["review"]["basis"]["id"]  # type: ignore[index]

        status, _headers, payload = _request(
            review.port,
            "POST",
            "/api/roughcut-change",
            headers=headers,
            body={
                "basis_id": basis_id,
                "expected_revision": before.revision,
                "operation": {
                    "type": "reorder",
                    "ordered_clip_ids": [
                        "clip_a_return",
                        "clip_b_middle",
                        "clip_a_open",
                    ],
                },
            },
        )
        assert status == 201
        assert isinstance(payload, dict)
        assert payload["review"]["schema_version"] == 2  # type: ignore[index]
        assert payload["review"]["project"]["revision"] == before.revision  # type: ignore[index]
        assert [
            span["source_id"]
            for span in payload["review"]["timeline"]["spans"]  # type: ignore[index]
        ] == ["src_a", "src_b", "src_a"]
        assert [source["playback_kind"] for source in payload["review"]["sources"]] == [  # type: ignore[index]
            "original",
            "proxy",
        ]
        assert ProjectStore(project_path).load() == before


def test_schema_two_decision_direct_edit_preserves_a_b_a_and_branches_after_undo(
    tmp_path: Path,
) -> None:
    state = _multisource_project(tmp_path)
    project_path = Path(state["project_path"])
    root_id = str(state["decision_id"])
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("review", encoding="utf-8")
    with start_review_server(project_path, edit_version_id=root_id, static_root=static) as review:
        headers = {
            "X-Roughcut-Token": review.token,
            "Origin": f"http://127.0.0.1:{review.port}",
            "Content-Type": "application/json",
        }
        status, _headers, payload = _request(
            review.port,
            "POST",
            "/api/edit-change",
            headers=headers,
            body={
                "active_edit_version_id": root_id,
                "expected_revision": 3,
                "operation": {
                    "type": "reorder",
                    "ordered_clip_ids": [
                        "clip_a_return",
                        "clip_b_middle",
                        "clip_a_open",
                    ],
                },
            },
        )
        assert status == 200
        assert isinstance(payload, dict)
        first_branch = payload["review"]["basis"]["id"]  # type: ignore[index]
        assert [
            span["source_id"]
            for span in payload["review"]["timeline"]["spans"]  # type: ignore[index]
        ] == ["src_a", "src_b", "src_a"]

        status, _headers, payload = _request(
            review.port,
            "POST",
            "/api/edit-undo",
            headers=headers,
            body={
                "active_edit_version_id": first_branch,
                "expected_revision": 4,
            },
        )
        assert status == 200
        assert isinstance(payload, dict)
        assert payload["edit_history"]["can_redo"] is True  # type: ignore[index]

        status, _headers, payload = _request(
            review.port,
            "POST",
            "/api/edit-change",
            headers=headers,
            body={
                "active_edit_version_id": root_id,
                "expected_revision": 5,
                "operation": {
                    "type": "trim",
                    "clip_id": "clip_b_middle",
                    "source_in_ticks": 10_000,
                    "source_out_ticks": 110_000,
                },
            },
        )
        assert status == 200
        assert isinstance(payload, dict)
        assert payload["edit_history"]["can_redo"] is False  # type: ignore[index]
        assert payload["edit_history"]["redo_stack"] == []  # type: ignore[index]
        assert [
            span["source_id"]
            for span in payload["review"]["timeline"]["spans"]  # type: ignore[index]
        ] == ["src_a", "src_b", "src_a"]

    assert (project_path / "edits" / f"{first_branch}.json").is_file()


def test_schema_two_direct_reject_is_blocked_and_active_decision_survives_revision_change(
    tmp_path: Path,
) -> None:
    proposal_state = _multisource_project(tmp_path / "reject", confirm=False)
    project_path = Path(proposal_state["project_path"])
    original_proposal_path = project_path / "proposals" / f"{proposal_state['proposal_id']}.json"
    other_proposal = json.loads(original_proposal_path.read_text(encoding="utf-8"))
    other_proposal["proposal_id"] = "proposal_other"
    write_new_json(project_path / "proposals" / "proposal_other.json", other_proposal)
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("review", encoding="utf-8")
    before = ProjectStore(project_path).load()
    with start_review_server(
        project_path,
        proposal_id=str(proposal_state["proposal_id"]),
        static_root=static,
    ) as review:
        status, _headers, payload = _request(
            review.port,
            "POST",
            "/api/reject",
            headers={
                "X-Roughcut-Token": review.token,
                "Origin": f"http://127.0.0.1:{review.port}",
                "Content-Type": "application/json",
            },
            body={"proposal_id": proposal_state["proposal_id"]},
        )
        assert status == 409
        assert isinstance(payload, dict)
        assert payload["error"]["code"] == "workflow_transition_not_allowed"  # type: ignore[index]
        status, _headers, error = _request(
            review.port,
            "POST",
            "/api/reject",
            headers={
                "X-Roughcut-Token": review.token,
                "Origin": f"http://127.0.0.1:{review.port}",
                "Content-Type": "application/json",
            },
            body={"proposal_id": "proposal_other"},
        )
        assert status == 409
        assert isinstance(error, dict)
        assert error["error"]["code"] == "workflow_transition_not_allowed"  # type: ignore[index]
    assert ProjectStore(project_path).load() == before

    decision_state = _multisource_project(tmp_path / "stale")
    decision_path = Path(decision_state["project_path"])
    store = ProjectStore(decision_path)
    current = store.load()
    store.save(replace(current, revision=current.revision + 1), expected_revision=current.revision)
    snapshot = load_review_snapshot(
        decision_path,
        edit_version_id=str(decision_state["decision_id"]),
    )
    assert snapshot.project_revision == current.revision + 1
    assert snapshot.basis_id == decision_state["decision_id"]


def test_schema_two_decision_rejects_a_tampered_frozen_context_hash(tmp_path: Path) -> None:
    state = _multisource_project(tmp_path)
    project_path = Path(state["project_path"])
    decision_path = project_path / "edits" / f"{state['decision_id']}.json"
    payload = json.loads(decision_path.read_text(encoding="utf-8"))
    payload["proposal_snapshot"]["context_hash"] = "0" * 64
    decision_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ProjectError, match="context hash"):
        load_review_snapshot(
            project_path,
            edit_version_id=str(state["decision_id"]),
        )


def test_schema_two_transcript_status_reports_only_the_changed_source(tmp_path: Path) -> None:
    state = _multisource_project(tmp_path)
    project_path = Path(state["project_path"])
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("review", encoding="utf-8")
    with start_review_server(
        project_path,
        edit_version_id=str(state["decision_id"]),
        static_root=static,
    ) as review:
        headers = {
            "X-Roughcut-Token": review.token,
            "Origin": f"http://127.0.0.1:{review.port}",
            "Content-Type": "application/json",
        }
        status, _headers, payload = _request(
            review.port,
            "POST",
            "/api/transcript-corrections",
            headers=headers,
            body={
                "source_id": "src_a",
                "parent_transcript_version_id": "tr_a",
                "corrections": [{"segment_id": "seg_shared", "corrected_text": "A 校正第一段。"}],
                "expected_revision": 3,
            },
        )
        assert status == 200
        assert isinstance(payload, dict)
        assert [
            mismatch["source_id"]
            for mismatch in payload["edit_reference_status"]["mismatches"]  # type: ignore[index]
        ] == ["src_a"]
        assert [
            mismatch["source_id"]
            for mismatch in payload["review_session"]["mismatches"]  # type: ignore[index]
        ] == ["src_a"]
        sources = payload["sources"]
        assert isinstance(sources, list)
        assert (
            next(item for item in sources if item["source_id"] == "src_b")[
                "active_transcript_version_id"
            ]
            == "tr_b"
        )
