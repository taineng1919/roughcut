from __future__ import annotations

import http.client
import json
import socket
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from roughcut.adapters.artifact_store import write_new_json
from roughcut.adapters.project_store import ProjectStore
from roughcut.application.agent_context import create_edit_brief, read_agent_context
from roughcut.application.projects import create_project
from roughcut.application.proposals import confirm_edit_proposal, create_edit_proposal
from roughcut.application.sources import fingerprint_file
from roughcut.domain.project import (
    ImportMode,
    MediaProbe,
    ProjectError,
    SourceAsset,
)
from roughcut.domain.transcript import TimedTranscript, TranscriptProvenance, TranscriptSegment
from roughcut.review.server import start_review_server


def _review_project(tmp_path: Path, *, allow_reorder: bool = True) -> tuple[Path, str, Path]:
    root = tmp_path / "review project"
    media = tmp_path / "可播放素材.mp4"
    media.write_bytes(bytes(range(256)) * 8)
    project = create_project(root, "Review")
    source_id = "src_review"
    transcript_id = "tr_review"
    source = SourceAsset(
        source_id=source_id,
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
        transcript_id,
        source_id,
        None,
        TranscriptProvenance("fixture", "1", {}, {}, "raw-asr/fixture.json", "a", "b", 0),
        "zh-CN",
        tuple(
            TranscriptSegment(
                f"seg_{index:06d}",
                (index - 1) * 120_000,
                index * 120_000,
                f"第{index}句。",
                None,
                None,
                None,
                None,
                (),
                "unmarked",
            )
            for index in range(1, 11)
        ),
    )
    write_new_json(root / "transcripts" / source_id / f"{transcript_id}.json", transcript.to_dict())
    imported = replace(
        project,
        revision=1,
        sources=(source,),
        active_transcript_versions={source_id: transcript_id},
    )
    ProjectStore(root).save(imported, expected_revision=0)
    brief = create_edit_brief(
        root,
        theme="十句预览",
        target_duration_ticks=1_200_000,
        focus=["全部句段"],
        allow_reorder=allow_reorder,
        expected_revision=1,
    )
    context = read_agent_context(
        root,
        source_id=source_id,
        transcript_version_id=transcript_id,
        brief_id=brief.brief.brief_id,
        expected_revision=2,
        offset=0,
        limit=10,
    )
    clips = [
        {
            "clip_id": f"clip_{index:06d}",
            "source_id": source_id,
            "transcript_version_id": transcript_id,
            "segment_id": f"seg_{index:06d}",
            "source_in_ticks": (index - 1) * 120_000,
            "source_out_ticks": index * 120_000,
            "reason": "fixture",
            "display_text": f"第{index}句。",
        }
        for index in range(1, 11)
    ]
    proposal = create_edit_proposal(
        root,
        source_id=source_id,
        transcript_version_id=transcript_id,
        brief_id=brief.brief.brief_id,
        context_hash=context.context_hash,
        clips=clips,
        total_duration_ticks=1_200_000,
        expected_revision=2,
    )
    return root, proposal.proposal.proposal_id, media


def _request(
    port: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    response_body = response.read()
    response_headers = {key.lower(): value for key, value in response.getheaders()}
    connection.close()
    return response.status, response_headers, response_body


def _cookie(headers: dict[str, str]) -> str:
    return headers["set-cookie"].split(";", 1)[0]


def _confirmed_review_project(tmp_path: Path) -> tuple[Path, str, str, Path]:
    project, proposal_id, media = _review_project(tmp_path)
    decision = confirm_edit_proposal(project, proposal_id, expected_revision=2)
    return project, proposal_id, decision.decision.edit_version_id, media


def _review_headers(review, *, write: bool = False) -> dict[str, str]:  # type: ignore[no-untyped-def]
    headers = {"X-Roughcut-Token": review.token}
    if write:
        headers.update(
            {
                "Origin": f"http://127.0.0.1:{review.port}",
                "Content-Type": "application/json",
            }
        )
    return headers


def test_review_server_binds_loopback_uses_token_and_serves_safe_snapshot(
    tmp_path: Path,
) -> None:
    project, proposal_id, _media = _review_project(tmp_path)
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<h1>review</h1>", encoding="utf-8")
    with start_review_server(project, proposal_id=proposal_id, static_root=static) as review:
        parsed = urlsplit(review.url)
        assert parsed.hostname == "127.0.0.1"
        assert parsed.port == review.port
        assert review.token not in repr(review)

        assert _request(review.port, "GET", "/")[0] == 403
        status, headers, body = _request(review.port, "GET", f"/?token={review.token}")
        assert status == 200
        assert body == b"<h1>review</h1>"
        cookie = _cookie(headers)
        status, _headers, body = _request(
            review.port, "GET", "/api/review", headers={"Cookie": cookie}
        )
        payload = json.loads(body)
        assert status == 200
        assert payload["schema_version"] == 1
        assert payload["sources"] == [payload["source"]]
        assert payload["project"]["revision"] == 2
        assert len(payload["timeline"]["spans"]) == 10
        assert len(payload["transcript"]) == 10
        assert payload["transcript"][0]["original_text"] == "第1句。"
        assert payload["transcript"][0]["corrected_text"] is None
        assert (
            _request(
                review.port,
                "GET",
                "/favicon.ico",
                headers={"Cookie": cookie},
            )[0]
            == 204
        )
        serialized = json.dumps(payload)
        assert str(project) not in serialized
        assert str(_media) not in serialized
        assert "locator" not in serialized


def test_review_server_rejects_bad_host_origin_token_and_path_traversal(tmp_path: Path) -> None:
    project, proposal_id, _media = _review_project(tmp_path)
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("ok", encoding="utf-8")
    with start_review_server(project, proposal_id=proposal_id, static_root=static) as review:
        valid = {"X-Roughcut-Token": review.token}
        assert _request(review.port, "GET", "/api/review", headers={"Host": "evil.test"})[0] == 403
        assert (
            _request(review.port, "GET", "/api/review", headers={"X-Roughcut-Token": "bad"})[0]
            == 403
        )
        assert (
            _request(
                review.port,
                "POST",
                "/api/reject",
                headers={
                    **valid,
                    "Origin": "https://evil.test",
                    "Content-Type": "application/json",
                },
                body=b"{}",
            )[0]
            == 403
        )
        assert _request(review.port, "GET", "/media/../project.json", headers=valid)[0] == 404
        assert _request(review.port, "GET", "/media/src_unknown", headers=valid)[0] == 404


def test_new_index_token_replaces_a_stale_cookie_from_an_older_review_session(
    tmp_path: Path,
) -> None:
    project, proposal_id, _media = _review_project(tmp_path)
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("ok", encoding="utf-8")
    with (
        start_review_server(project, proposal_id=proposal_id, static_root=static) as older,
        start_review_server(project, proposal_id=proposal_id, static_root=static) as current,
    ):
        status, headers, body = _request(
            current.port,
            "GET",
            f"/?token={current.token}",
            headers={"Cookie": f"roughcut_session={older.token}"},
        )
        assert status == 200
        assert body == b"ok"
        assert headers["set-cookie"].startswith(f"roughcut_session={current.token};")


def test_media_supports_full_head_and_single_range_requests(tmp_path: Path) -> None:
    project, proposal_id, media = _review_project(tmp_path)
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("ok", encoding="utf-8")
    with start_review_server(project, proposal_id=proposal_id, static_root=static) as review:
        headers = {"X-Roughcut-Token": review.token}
        status, response_headers, body = _request(
            review.port, "GET", "/media/src_review", headers=headers
        )
        assert status == 200
        assert body == media.read_bytes()
        assert response_headers["accept-ranges"] == "bytes"
        status, response_headers, body = _request(
            review.port,
            "GET",
            "/media/src_review",
            headers={**headers, "Range": "bytes=100-199"},
        )
        assert status == 206
        assert body == media.read_bytes()[100:200]
        assert response_headers["content-range"] == f"bytes 100-199/{media.stat().st_size}"
        status, response_headers, body = _request(
            review.port, "HEAD", "/media/src_review", headers=headers
        )
        assert status == 200
        assert body == b""
        assert response_headers["content-length"] == str(media.stat().st_size)
        assert (
            _request(
                review.port,
                "GET",
                "/media/src_review",
                headers={**headers, "Range": "bytes=999999-"},
            )[0]
            == 416
        )
        assert (
            _request(
                review.port,
                "GET",
                "/media/src_review",
                headers={**headers, "Range": "bytes=0-1,4-5"},
            )[0]
            == 416
        )


def test_registered_copied_source_cannot_escape_project(tmp_path: Path) -> None:
    project, proposal_id, media = _review_project(tmp_path)
    store = ProjectStore(project)
    current = store.load()
    escaped = replace(
        current.sources[0],
        import_mode=ImportMode.COPIED,
        locator={"project_relative_path": "../可播放素材.mp4"},
    )
    store.save(replace(current, sources=(escaped,)), expected_revision=current.revision)
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("ok", encoding="utf-8")
    with start_review_server(project, proposal_id=proposal_id, static_root=static) as review:
        status = _request(
            review.port,
            "GET",
            "/media/src_review",
            headers={"X-Roughcut-Token": review.token},
        )[0]
        assert status == 403
    assert media.exists()


def test_legacy_review_proposal_and_confirm_endpoints_cannot_bypass_workflow(
    tmp_path: Path,
) -> None:
    project, proposal_id, _media = _review_project(tmp_path)
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("ok", encoding="utf-8")
    stale = start_review_server(project, proposal_id=proposal_id, static_root=static)
    with stale, start_review_server(project, proposal_id=proposal_id, static_root=static) as review:
        headers = {
            "X-Roughcut-Token": review.token,
            "Origin": f"http://127.0.0.1:{review.port}",
            "Content-Type": "application/json",
        }
        initial = json.loads(_request(review.port, "GET", "/api/review", headers=headers)[2])
        clips = list(reversed(initial["proposal"]["clips"]))[:-1]
        before = ProjectStore(project).load()
        status, _headers, body = _request(
            review.port,
            "POST",
            "/api/proposals",
            headers=headers,
            body=json.dumps({"clips": clips}).encode(),
        )
        rejected = json.loads(body)
        assert status == 409
        assert rejected["error"]["code"] == "workflow_transition_not_allowed"
        assert ProjectStore(project).load() == before
        status, _headers, body = _request(
            review.port,
            "POST",
            "/api/confirm",
            headers=headers,
            body=json.dumps({"proposal_id": proposal_id}).encode(),
        )
        rejected_confirm = json.loads(body)
        assert status == 400
        assert rejected_confirm["error"]["code"] == "workflow_required"
        assert ProjectStore(project).load() == before

        stale_headers = {
            "X-Roughcut-Token": stale.token,
            "Origin": f"http://127.0.0.1:{stale.port}",
            "Content-Type": "application/json",
        }
        status, _headers, body = _request(
            stale.port,
            "POST",
            "/api/proposals",
            headers=stale_headers,
            body=json.dumps({"clips": clips}).encode(),
        )
        assert status == 409
        assert json.loads(body)["error"]["code"] == "workflow_transition_not_allowed"
        assert ProjectStore(project).load() == before


def test_decision_review_exposes_history_and_refreshes_after_change_undo_redo_and_restart(
    tmp_path: Path,
) -> None:
    project, _proposal_id, decision_id, _media = _confirmed_review_project(tmp_path)
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("review", encoding="utf-8")
    original_files = set((project / "edits").glob("*.json"))

    with start_review_server(project, edit_version_id=decision_id, static_root=static) as review:
        read_headers = _review_headers(review)
        write_headers = _review_headers(review, write=True)
        status, _headers, body = _request(
            review.port, "GET", "/api/edit-history", headers=read_headers
        )
        initial = json.loads(body)
        assert status == 200
        assert initial["review"]["basis"] == {"type": "decision", "id": decision_id}
        assert initial["edit_history"]["project_revision"] == 3
        assert initial["edit_history"]["can_undo"] is False
        assert initial["edit_history"]["can_redo"] is False
        assert str(project) not in json.dumps(initial)

        status, _headers, body = _request(
            review.port,
            "POST",
            "/api/edit-change",
            headers=write_headers,
            body=json.dumps(
                {
                    "active_edit_version_id": decision_id,
                    "expected_revision": 3,
                    "operation": {"type": "delete", "clip_id": "clip_000005"},
                }
            ).encode(),
        )
        changed = json.loads(body)
        assert status == 200
        changed_id = changed["review"]["basis"]["id"]
        assert changed_id != decision_id
        assert changed["edit_change"]["changed"] is True
        assert changed["review"]["project"]["revision"] == 4
        assert len(changed["review"]["proposal"]["clips"]) == 9
        assert len(changed["review"]["timeline"]["spans"]) == 9
        assert changed["edit_history"]["can_undo"] is True
        assert [item["clip_id"] for item in changed["edit_history"]["restorable_clips"]] == [
            "clip_000005"
        ]

        edit_files = set((project / "edits").glob("*.json"))
        current_ids = [clip["clip_id"] for clip in changed["review"]["proposal"]["clips"]]
        status, _headers, body = _request(
            review.port,
            "POST",
            "/api/edit-change",
            headers=write_headers,
            body=json.dumps(
                {
                    "active_edit_version_id": changed_id,
                    "expected_revision": 4,
                    "operation": {"type": "reorder", "ordered_clip_ids": current_ids},
                }
            ).encode(),
        )
        no_op = json.loads(body)
        assert status == 200
        assert no_op["edit_change"]["changed"] is False
        assert no_op["review"]["project"]["revision"] == 4
        assert set((project / "edits").glob("*.json")) == edit_files

        status, _headers, body = _request(
            review.port,
            "POST",
            "/api/edit-change",
            headers=write_headers,
            body=json.dumps(
                {
                    "active_edit_version_id": changed_id,
                    "expected_revision": 4,
                    "operation": {
                        "type": "restore",
                        "clip_id": "clip_000005",
                        "insert_before_clip_id": "clip_000006",
                    },
                }
            ).encode(),
        )
        restored = json.loads(body)
        assert status == 200
        restored_id = restored["review"]["basis"]["id"]
        assert restored["review"]["project"]["revision"] == 5
        assert len(restored["review"]["timeline"]["spans"]) == 10
        assert restored["edit_history"]["restorable_clips"] == []

        status, _headers, body = _request(
            review.port,
            "POST",
            "/api/edit-undo",
            headers=write_headers,
            body=json.dumps(
                {
                    "active_edit_version_id": restored_id,
                    "expected_revision": 5,
                }
            ).encode(),
        )
        undone = json.loads(body)
        assert status == 200
        assert undone["review"]["basis"]["id"] == changed_id
        assert undone["review"]["project"]["revision"] == 6
        assert undone["edit_history"]["can_redo"] is True
        assert len(undone["review"]["timeline"]["spans"]) == 9

        status, _headers, body = _request(
            review.port,
            "POST",
            "/api/edit-redo",
            headers=write_headers,
            body=json.dumps(
                {
                    "active_edit_version_id": changed_id,
                    "expected_revision": 6,
                }
            ).encode(),
        )
        redone = json.loads(body)
        assert status == 200
        assert redone["review"]["basis"]["id"] == restored_id
        assert redone["review"]["project"]["revision"] == 7
        assert redone["edit_history"]["can_redo"] is False

    assert original_files < set((project / "edits").glob("*.json"))
    with start_review_server(project, static_root=static) as restarted:
        status, _headers, body = _request(
            restarted.port,
            "GET",
            "/api/edit-history",
            headers=_review_headers(restarted),
        )
        recovered = json.loads(body)
        assert status == 200
        assert recovered["review"]["basis"]["id"] == restored_id
        assert recovered["review"]["project"]["revision"] == 7
        assert recovered["edit_history"]["can_undo"] is True
        assert recovered["edit_history"]["can_redo"] is False


def test_roughcut_candidate_changes_stay_unconfirmed_without_active_workflow(
    tmp_path: Path,
) -> None:
    project, _proposal_id, decision_id, _media = _confirmed_review_project(tmp_path)
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("review", encoding="utf-8")
    before = ProjectStore(project).load()
    before_edits = set((project / "edits").glob("*.json"))

    with start_review_server(project, edit_version_id=decision_id, static_root=static) as review:
        read_headers = _review_headers(review)
        write_headers = _review_headers(review, write=True)
        status, _headers, body = _request(
            review.port,
            "GET",
            "/api/roughcut-state",
            headers=read_headers,
        )
        initial = json.loads(body)
        assert status == 200
        assert initial["review"]["basis"] == {"type": "decision", "id": decision_id}
        assert initial["candidate_history"] == {
            "can_undo": False,
            "can_redo": False,
            "restorable_clips": [],
        }

        status, _headers, body = _request(
            review.port,
            "POST",
            "/api/roughcut-change",
            headers=write_headers,
            body=json.dumps(
                {
                    "basis_id": decision_id,
                    "expected_revision": before.revision,
                    "operation": {"type": "delete", "clip_id": "clip_000005"},
                }
            ).encode(),
        )
        deleted = json.loads(body)
        assert status == 201, deleted
        proposal_id = deleted["review"]["basis"]["id"]
        assert deleted["review"]["basis"]["type"] == "proposal"
        assert deleted["review"]["project"]["revision"] == before.revision
        assert deleted["candidate_change"]["changed"] is True
        assert deleted["candidate_history"]["can_undo"] is True
        assert ProjectStore(project).load() == before
        assert set((project / "edits").glob("*.json")) == before_edits

        status, _headers, body = _request(
            review.port,
            "POST",
            "/api/roughcut-undo",
            headers=write_headers,
            body=json.dumps(
                {"basis_id": proposal_id, "expected_revision": before.revision}
            ).encode(),
        )
        undone = json.loads(body)
        assert status == 200
        assert undone["review"]["basis"] == {"type": "decision", "id": decision_id}
        assert undone["candidate_history"]["can_redo"] is True

        status, _headers, body = _request(
            review.port,
            "POST",
            "/api/roughcut-redo",
            headers=write_headers,
            body=json.dumps(
                {"basis_id": decision_id, "expected_revision": before.revision}
            ).encode(),
        )
        redone = json.loads(body)
        assert status == 200
        assert redone["review"]["basis"]["id"] == proposal_id

        status, _headers, body = _request(
            review.port,
            "POST",
            "/api/roughcut-adopt",
            headers=write_headers,
            body=json.dumps(
                {"basis_id": proposal_id, "expected_revision": before.revision}
            ).encode(),
        )
        adopted = json.loads(body)
        assert status == 400
        assert adopted["error"]["code"] == "workflow_required"
        assert ProjectStore(project).load() == before
        assert set((project / "edits").glob("*.json")) == before_edits


def test_initial_proposal_roughcut_delete_restore_and_adopt_are_separate(
    tmp_path: Path,
) -> None:
    project, proposal_id, _media = _review_project(tmp_path)
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("review", encoding="utf-8")
    before = ProjectStore(project).load()

    with start_review_server(project, proposal_id=proposal_id, static_root=static) as review:
        headers = _review_headers(review, write=True)
        status, _headers, body = _request(
            review.port,
            "POST",
            "/api/roughcut-change",
            headers=headers,
            body=json.dumps(
                {
                    "basis_id": proposal_id,
                    "expected_revision": before.revision,
                    "operation": {"type": "delete", "clip_id": "clip_000005"},
                }
            ).encode(),
        )
        deleted = json.loads(body)
        assert status == 201
        deleted_id = deleted["review"]["basis"]["id"]
        assert [clip["clip_id"] for clip in deleted["candidate_history"]["restorable_clips"]] == [
            "clip_000005"
        ]
        assert ProjectStore(project).load() == before

        status, _headers, body = _request(
            review.port,
            "POST",
            "/api/roughcut-change",
            headers=headers,
            body=json.dumps(
                {
                    "basis_id": deleted_id,
                    "expected_revision": before.revision,
                    "operation": {
                        "type": "restore",
                        "clip_id": "clip_000005",
                        "insert_before_clip_id": "clip_000006",
                    },
                }
            ).encode(),
        )
        restored = json.loads(body)
        assert status == 201
        restored_id = restored["review"]["basis"]["id"]
        assert len(restored["review"]["proposal"]["clips"]) == 10
        assert restored["candidate_history"]["restorable_clips"] == []
        assert ProjectStore(project).load() == before

        status, _headers, body = _request(
            review.port,
            "POST",
            "/api/roughcut-adopt",
            headers=headers,
            body=json.dumps(
                {
                    "basis_id": restored_id,
                    "expected_revision": before.revision,
                }
            ).encode(),
        )
        adopted = json.loads(body)
        assert status == 400
        assert adopted["error"]["code"] == "workflow_required"
        assert ProjectStore(project).load() == before


def test_roughcut_restore_preserves_the_latest_trimmed_clip(tmp_path: Path) -> None:
    project, _proposal_id, decision_id, _media = _confirmed_review_project(tmp_path)
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("review", encoding="utf-8")
    before = ProjectStore(project).load()

    with start_review_server(project, edit_version_id=decision_id, static_root=static) as review:
        headers = _review_headers(review, write=True)
        status, _headers, body = _request(
            review.port,
            "POST",
            "/api/roughcut-change",
            headers=headers,
            body=json.dumps(
                {
                    "basis_id": decision_id,
                    "expected_revision": before.revision,
                    "operation": {
                        "type": "trim",
                        "clip_id": "clip_000001",
                        "source_in_ticks": 12_000,
                        "source_out_ticks": 108_000,
                    },
                }
            ).encode(),
        )
        trimmed = json.loads(body)
        assert status == 201, trimmed
        trimmed_id = trimmed["review"]["basis"]["id"]

        status, _headers, body = _request(
            review.port,
            "POST",
            "/api/roughcut-change",
            headers=headers,
            body=json.dumps(
                {
                    "basis_id": trimmed_id,
                    "expected_revision": before.revision,
                    "operation": {"type": "delete", "clip_id": "clip_000001"},
                }
            ).encode(),
        )
        deleted = json.loads(body)
        assert status == 201, deleted
        deleted_id = deleted["review"]["basis"]["id"]
        restorable = next(
            clip
            for clip in deleted["candidate_history"]["restorable_clips"]
            if clip["clip_id"] == "clip_000001"
        )
        assert restorable["source_in_ticks"] == 12_000
        assert restorable["source_out_ticks"] == 108_000

        status, _headers, body = _request(
            review.port,
            "POST",
            "/api/roughcut-change",
            headers=headers,
            body=json.dumps(
                {
                    "basis_id": deleted_id,
                    "expected_revision": before.revision,
                    "operation": {
                        "type": "restore",
                        "clip_id": "clip_000001",
                        "insert_before_clip_id": None,
                    },
                }
            ).encode(),
        )
        restored = json.loads(body)
        assert status == 201, restored
        restored_clip = next(
            clip
            for clip in restored["review"]["proposal"]["clips"]
            if clip["clip_id"] == "clip_000001"
        )
        assert restored_clip["source_in_ticks"] == 12_000
        assert restored_clip["source_out_ticks"] == 108_000
        assert ProjectStore(project).load() == before


def test_roughcut_undo_branch_clears_redo_without_deleting_old_candidate(
    tmp_path: Path,
) -> None:
    project, _proposal_id, decision_id, _media = _confirmed_review_project(tmp_path)
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("review", encoding="utf-8")
    before = ProjectStore(project).load()
    with start_review_server(project, edit_version_id=decision_id, static_root=static) as review:
        headers = _review_headers(review, write=True)
        status, _headers, body = _request(
            review.port,
            "POST",
            "/api/roughcut-change",
            headers=headers,
            body=json.dumps(
                {
                    "basis_id": decision_id,
                    "expected_revision": before.revision,
                    "operation": {"type": "delete", "clip_id": "clip_000005"},
                }
            ).encode(),
        )
        first = json.loads(body)
        assert status == 201
        first_id = first["review"]["basis"]["id"]
        first_path = project / "proposals" / f"{first_id}.json"
        assert first_path.is_file()

        status, _headers, body = _request(
            review.port,
            "POST",
            "/api/roughcut-undo",
            headers=headers,
            body=json.dumps(
                {"basis_id": first_id, "expected_revision": before.revision}
            ).encode(),
        )
        undone = json.loads(body)
        assert status == 200
        assert undone["candidate_history"]["can_redo"] is True

        status, _headers, body = _request(
            review.port,
            "POST",
            "/api/roughcut-change",
            headers=headers,
            body=json.dumps(
                {
                    "basis_id": decision_id,
                    "expected_revision": before.revision,
                    "operation": {
                        "type": "trim",
                        "clip_id": "clip_000001",
                        "source_in_ticks": 12_000,
                        "source_out_ticks": 108_000,
                    },
                }
            ).encode(),
        )
        branch = json.loads(body)
        assert status == 201
        assert branch["candidate_history"]["can_redo"] is False
        assert first_path.is_file()
        assert ProjectStore(project).load() == before


def test_roughcut_candidate_snapshot_failure_removes_new_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _proposal_id, decision_id, _media = _confirmed_review_project(tmp_path)
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("review", encoding="utf-8")
    before = ProjectStore(project).load()
    before_proposals = set((project / "proposals").glob("*.json"))

    with start_review_server(project, edit_version_id=decision_id, static_root=static) as review:
        monkeypatch.setattr(
            "roughcut.review.server.load_review_snapshot",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("injected snapshot readback failure")
            ),
        )
        status, _headers, body = _request(
            review.port,
            "POST",
            "/api/roughcut-change",
            headers=_review_headers(review, write=True),
            body=json.dumps(
                {
                    "basis_id": decision_id,
                    "expected_revision": before.revision,
                    "operation": {"type": "delete", "clip_id": "clip_000005"},
                }
            ).encode(),
        )
        assert status == 500
        assert json.loads(body)["error"]["code"] == "review_service_failed"

        status, _headers, body = _request(
            review.port,
            "GET",
            "/api/roughcut-state",
            headers=_review_headers(review),
        )
        current = json.loads(body)
        assert status == 200
        assert current["review"]["basis"] == {"type": "decision", "id": decision_id}
        assert current["candidate_history"]["can_undo"] is False

    assert ProjectStore(project).load() == before
    assert set((project / "proposals").glob("*.json")) == before_proposals


def test_roughcut_candidate_write_rejects_stale_review_without_artifact(
    tmp_path: Path,
) -> None:
    project, _proposal_id, decision_id, _media = _confirmed_review_project(tmp_path)
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("review", encoding="utf-8")
    stale = start_review_server(project, edit_version_id=decision_id, static_root=static)
    before_proposals = set((project / "proposals").glob("*.json"))

    with stale:
        current = ProjectStore(project).load()
        ProjectStore(project).save(
            replace(current, revision=current.revision + 1),
            expected_revision=current.revision,
        )
        status, _headers, body = _request(
            stale.port,
            "POST",
            "/api/roughcut-change",
            headers=_review_headers(stale, write=True),
            body=json.dumps(
                {
                    "basis_id": decision_id,
                    "expected_revision": 3,
                    "operation": {"type": "delete", "clip_id": "clip_000005"},
                }
            ).encode(),
        )
        assert status == 409
        assert json.loads(body)["error"]["code"] == "stale_review"

    assert set((project / "proposals").glob("*.json")) == before_proposals


def test_decision_review_restart_recovers_nonempty_redo_stack(tmp_path: Path) -> None:
    project, _proposal_id, decision_id, _media = _confirmed_review_project(tmp_path)
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("review", encoding="utf-8")
    with start_review_server(project, edit_version_id=decision_id, static_root=static) as review:
        headers = _review_headers(review, write=True)
        status, _headers, body = _request(
            review.port,
            "POST",
            "/api/edit-change",
            headers=headers,
            body=json.dumps(
                {
                    "active_edit_version_id": decision_id,
                    "expected_revision": 3,
                    "operation": {"type": "delete", "clip_id": "clip_000005"},
                }
            ).encode(),
        )
        changed = json.loads(body)
        assert status == 200
        child_id = changed["review"]["basis"]["id"]
        status, _headers, body = _request(
            review.port,
            "POST",
            "/api/edit-undo",
            headers=headers,
            body=json.dumps(
                {
                    "active_edit_version_id": child_id,
                    "expected_revision": 4,
                }
            ).encode(),
        )
        assert status == 200
        assert json.loads(body)["edit_history"]["can_redo"] is True

    with start_review_server(project, static_root=static) as restarted:
        read_headers = _review_headers(restarted)
        status, _headers, body = _request(
            restarted.port, "GET", "/api/edit-history", headers=read_headers
        )
        recovered = json.loads(body)
        assert status == 200
        assert recovered["review"]["basis"]["id"] == decision_id
        assert recovered["edit_history"]["redo_stack"] == [child_id]
        status, _headers, body = _request(
            restarted.port,
            "POST",
            "/api/edit-redo",
            headers=_review_headers(restarted, write=True),
            body=json.dumps(
                {
                    "active_edit_version_id": decision_id,
                    "expected_revision": 5,
                }
            ).encode(),
        )
        assert status == 200
        assert json.loads(body)["review"]["basis"]["id"] == child_id


def test_proposal_review_keeps_confirmation_gate_and_rejects_direct_edit(
    tmp_path: Path,
) -> None:
    project, proposal_id, _media = _review_project(tmp_path)
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("review", encoding="utf-8")
    before = ProjectStore(project).load()
    with start_review_server(project, proposal_id=proposal_id, static_root=static) as review:
        headers = _review_headers(review, write=True)
        status, _headers, body = _request(
            review.port,
            "POST",
            "/api/edit-change",
            headers=headers,
            body=json.dumps(
                {
                    "active_edit_version_id": "edit_missing",
                    "expected_revision": before.revision,
                    "operation": {"type": "delete", "clip_id": "clip_000005"},
                }
            ).encode(),
        )
        assert status == 400
        assert json.loads(body)["error"]["code"] == "invalid_review_change"
    assert ProjectStore(project).load() == before


def test_decision_review_rejects_stale_invalid_and_failed_changes_without_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _proposal_id, decision_id, _media = _confirmed_review_project(tmp_path)
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("review", encoding="utf-8")
    before = ProjectStore(project).load()
    before_files = set((project / "edits").glob("*.json"))
    with start_review_server(project, edit_version_id=decision_id, static_root=static) as review:
        headers = _review_headers(review, write=True)
        for payload in (
            {
                "active_edit_version_id": decision_id,
                "expected_revision": 3,
                "operation": {"type": "delete", "clip_id": "missing"},
            },
            {
                "active_edit_version_id": decision_id,
                "expected_revision": 3,
                "operation": {
                    "type": "reorder",
                    "ordered_clip_ids": ["clip_000001", "clip_000001"],
                },
            },
            {
                "active_edit_version_id": decision_id,
                "expected_revision": 3,
                "operation": {
                    "type": "trim",
                    "clip_id": "clip_000001",
                    "source_in_ticks": 120_000,
                    "source_out_ticks": 120_000,
                },
            },
        ):
            status, _headers, body = _request(
                review.port,
                "POST",
                "/api/edit-change",
                headers=headers,
                body=json.dumps(payload).encode(),
            )
            assert status == 400
            assert json.loads(body)["error"]["code"] == "invalid_review_change"

        for payload in (
            {
                "active_edit_version_id": decision_id,
                "expected_revision": 2,
                "operation": {"type": "delete", "clip_id": "clip_000005"},
            },
            {
                "active_edit_version_id": "edit_other",
                "expected_revision": 3,
                "operation": {"type": "delete", "clip_id": "clip_000005"},
            },
        ):
            status, _headers, body = _request(
                review.port,
                "POST",
                "/api/edit-change",
                headers=headers,
                body=json.dumps(payload).encode(),
            )
            assert status == 409
            assert json.loads(body)["error"]["code"] == "stale_review"

        def fail_change(*_args: object, **_kwargs: object) -> None:
            raise OSError("injected edit write failure")

        monkeypatch.setattr("roughcut.review.server.change_edit", fail_change)
        status, _headers, body = _request(
            review.port,
            "POST",
            "/api/edit-change",
            headers=headers,
            body=json.dumps(
                {
                    "active_edit_version_id": decision_id,
                    "expected_revision": 3,
                    "operation": {"type": "delete", "clip_id": "clip_000005"},
                }
            ).encode(),
        )
        assert status == 500
        assert json.loads(body)["error"]["code"] == "review_service_failed"
        status, _headers, body = _request(
            review.port, "GET", "/api/review", headers=_review_headers(review)
        )
        assert status == 200
        assert json.loads(body)["basis"]["id"] == decision_id

    assert ProjectStore(project).load() == before
    assert set((project / "edits").glob("*.json")) == before_files


def test_decision_review_rejects_reorder_when_brief_disallows_it(tmp_path: Path) -> None:
    project, proposal_id, _media = _review_project(tmp_path, allow_reorder=False)
    decision = confirm_edit_proposal(project, proposal_id, expected_revision=2)
    decision_id = decision.decision.edit_version_id
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("review", encoding="utf-8")
    before = ProjectStore(project).load()
    with start_review_server(project, edit_version_id=decision_id, static_root=static) as review:
        status, _headers, body = _request(
            review.port,
            "POST",
            "/api/edit-change",
            headers=_review_headers(review, write=True),
            body=json.dumps(
                {
                    "active_edit_version_id": decision_id,
                    "expected_revision": 3,
                    "operation": {
                        "type": "reorder",
                        "ordered_clip_ids": [
                            "clip_000002",
                            "clip_000001",
                            *[f"clip_{index:06d}" for index in range(3, 11)],
                        ],
                    },
                }
            ).encode(),
        )
        assert status == 400
        assert json.loads(body)["error"]["code"] == "invalid_review_change"
    assert ProjectStore(project).load() == before


def test_review_server_closes_thread_and_port_and_start_failure_leaves_nothing(
    tmp_path: Path,
) -> None:
    project, proposal_id, _media = _review_project(tmp_path)
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("ok", encoding="utf-8")
    review = start_review_server(project, proposal_id=proposal_id, static_root=static)
    port = review.port
    assert review.thread.is_alive()
    review.close()
    assert not review.thread.is_alive()
    with socket.socket() as probe:
        probe.settimeout(0.2)
        assert probe.connect_ex(("127.0.0.1", port)) != 0
    with pytest.raises(ProjectError):
        start_review_server(project, proposal_id="proposal_unknown", static_root=static)


def test_review_transcript_correction_is_immutable_stales_edit_and_can_switch_back(
    tmp_path: Path,
) -> None:
    project, _proposal_id, decision_id, _media = _confirmed_review_project(tmp_path)
    parent_path = project / "transcripts/src_review/tr_review.json"
    parent_bytes = parent_path.read_bytes()
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("review", encoding="utf-8")

    with start_review_server(project, edit_version_id=decision_id, static_root=static) as review:
        read_headers = _review_headers(review)
        write_headers = _review_headers(review, write=True)
        status, _headers, body = _request(
            review.port, "GET", "/api/transcript-versions", headers=read_headers
        )
        versions = json.loads(body)
        assert status == 200
        assert versions["project_revision"] == 3
        assert versions["edit_reference_status"] == {"status": "current", "mismatches": []}
        assert versions["review_session"]["status"] == "current"
        assert versions["sources"][0]["active_segments"][0] == {
            "segment_id": "seg_000001",
            "start_ticks": 0,
            "end_ticks": 120_000,
            "original_text": "第1句。",
            "corrected_text": None,
        }

        status, _headers, body = _request(
            review.port,
            "POST",
            "/api/transcript-corrections",
            headers=write_headers,
            body=json.dumps(
                {
                    "source_id": "src_review",
                    "parent_transcript_version_id": "tr_review",
                    "corrections": [
                        {"segment_id": "seg_000001", "corrected_text": "  校正第一句  "}
                    ],
                    "expected_revision": 3,
                }
            ).encode(),
        )
        corrected = json.loads(body)
        assert status == 200
        assert corrected["transcript_mutation"]["changed"] is True
        child_id = corrected["transcript_mutation"]["transcript"]["transcript_version_id"]
        assert corrected["project_revision"] == 4
        assert corrected["edit_reference_status"]["status"] == "stale"
        assert corrected["edit_reference_status"]["mismatches"] == [
            {
                "source_id": "src_review",
                "referenced_transcript_version_id": "tr_review",
                "active_transcript_version_id": child_id,
            }
        ]
        assert corrected["review_session"]["read_only"] is True
        assert corrected["sources"][0]["active_segments"][0]["corrected_text"] == "校正第一句"
        assert parent_path.read_bytes() == parent_bytes

        status, _headers, body = _request(
            review.port,
            "POST",
            "/api/proposals",
            headers=write_headers,
            body=json.dumps({"clips": []}).encode(),
        )
        assert status == 409
        assert json.loads(body)["error"]["code"] == "workflow_transition_not_allowed"

        status, _headers, body = _request(
            review.port,
            "POST",
            "/api/edit-change",
            headers=write_headers,
            body=json.dumps(
                {
                    "active_edit_version_id": decision_id,
                    "expected_revision": 4,
                    "operation": {"type": "delete", "clip_id": "clip_000005"},
                }
            ).encode(),
        )
        assert status == 409
        assert json.loads(body)["error"]["code"] == "stale_review"

        status, _headers, body = _request(
            review.port,
            "POST",
            "/api/transcript-corrections",
            headers=write_headers,
            body=json.dumps(
                {
                    "source_id": "src_review",
                    "parent_transcript_version_id": child_id,
                    "corrections": [{"segment_id": "seg_000001", "corrected_text": None}],
                    "expected_revision": 4,
                }
            ).encode(),
        )
        restored_text = json.loads(body)
        assert status == 200
        assert restored_text["project_revision"] == 5
        assert restored_text["sources"][0]["active_segments"][0]["corrected_text"] is None

        restored_id = restored_text["sources"][0]["active_transcript_version_id"]
        status, _headers, body = _request(
            review.port,
            "POST",
            "/api/transcript-corrections",
            headers=write_headers,
            body=json.dumps(
                {
                    "source_id": "src_review",
                    "parent_transcript_version_id": restored_id,
                    "corrections": [{"segment_id": "seg_000001", "corrected_text": None}],
                    "expected_revision": 5,
                }
            ).encode(),
        )
        no_op = json.loads(body)
        assert status == 200
        assert no_op["transcript_mutation"]["changed"] is False
        assert no_op["project_revision"] == 5

        status, _headers, body = _request(
            review.port,
            "POST",
            "/api/transcript-activate",
            headers=write_headers,
            body=json.dumps(
                {
                    "source_id": "src_review",
                    "transcript_version_id": "tr_review",
                    "expected_revision": 5,
                }
            ).encode(),
        )
        activated = json.loads(body)
        assert status == 200
        assert activated["transcript_activation"]["changed"] is True
        assert activated["project_revision"] == 6
        assert activated["edit_reference_status"] == {"status": "current", "mismatches": []}
        assert activated["review_session"]["read_only"] is True

        status, _headers, body = _request(
            review.port,
            "POST",
            "/api/transcript-activate",
            headers=write_headers,
            body=json.dumps(
                {
                    "source_id": "src_review",
                    "transcript_version_id": "tr_review",
                    "expected_revision": 6,
                }
            ).encode(),
        )
        repeated = json.loads(body)
        assert status == 200
        assert repeated["transcript_activation"]["changed"] is False
        assert repeated["project_revision"] == 6


def test_stale_proposal_session_cannot_confirm_or_reject_after_correction(tmp_path: Path) -> None:
    project, proposal_id, _media = _review_project(tmp_path)
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("review", encoding="utf-8")
    with start_review_server(project, proposal_id=proposal_id, static_root=static) as review:
        headers = _review_headers(review, write=True)
        correction = {
            "source_id": "src_review",
            "parent_transcript_version_id": "tr_review",
            "corrections": [{"segment_id": "seg_000001", "corrected_text": "校正"}],
            "expected_revision": 2,
        }
        assert (
            _request(
                review.port,
                "POST",
                "/api/transcript-corrections",
                headers=headers,
                body=json.dumps(correction).encode(),
            )[0]
            == 200
        )
        for endpoint, payload, expected_code in (
            ("/api/confirm", {"proposal_id": proposal_id}, "stale_review"),
            (
                "/api/reject",
                {"proposal_id": proposal_id},
                "workflow_transition_not_allowed",
            ),
        ):
            status, _headers, body = _request(
                review.port,
                "POST",
                endpoint,
                headers=headers,
                body=json.dumps(payload).encode(),
            )
            assert status == 409
            assert json.loads(body)["error"]["code"] == expected_code


def test_transcript_review_rejects_unknown_blank_corrupt_and_save_failure_without_partial_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _proposal_id, decision_id, _media = _confirmed_review_project(tmp_path)
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("review", encoding="utf-8")
    parent_path = project / "transcripts/src_review/tr_review.json"
    before_parent = parent_path.read_bytes()
    before_project = (project / "project.json").read_bytes()
    with start_review_server(project, edit_version_id=decision_id, static_root=static) as review:
        headers = _review_headers(review, write=True)
        common = {
            "parent_transcript_version_id": "tr_review",
            "corrections": [{"segment_id": "seg_000001", "corrected_text": "校正"}],
            "expected_revision": 3,
        }
        for payload in (
            {"source_id": "src_unknown", **common},
            {
                "source_id": "src_review",
                **common,
                "corrections": [{"segment_id": "seg_000001", "corrected_text": "   "}],
            },
        ):
            assert (
                _request(
                    review.port,
                    "POST",
                    "/api/transcript-corrections",
                    headers=headers,
                    body=json.dumps(payload).encode(),
                )[0]
                == 400
            )

        corrupt = TimedTranscript.from_dict(json.loads(parent_path.read_text(encoding="utf-8")))
        write_new_json(
            project / "transcripts/src_review/tr_corrupt.json",
            replace(
                corrupt,
                transcript_version_id="tr_corrupt",
                parent_version_id="tr_missing",
            ).to_dict(),
        )
        assert (
            _request(
                review.port,
                "POST",
                "/api/transcript-corrections",
                headers=headers,
                body=json.dumps({"source_id": "src_review", **common}).encode(),
            )[0]
            == 400
        )
        (project / "transcripts/src_review/tr_corrupt.json").unlink()

        def fail_save(*_args: object, **_kwargs: object) -> None:
            raise OSError("injected save failure")

        monkeypatch.setattr(ProjectStore, "save", fail_save)
        status, _headers, body = _request(
            review.port,
            "POST",
            "/api/transcript-corrections",
            headers=headers,
            body=json.dumps({"source_id": "src_review", **common}).encode(),
        )
        assert status == 500
        assert json.loads(body)["error"]["code"] == "review_service_failed"

    assert parent_path.read_bytes() == before_parent
    assert (project / "project.json").read_bytes() == before_project
    assert list((project / "transcripts/src_review").glob("*.json")) == [parent_path]
