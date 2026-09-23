from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from windows.w4_support import run_cli_raw
from windows.w5_review_process import ReviewCliChild, assert_port_closed, review_request
from windows.w5_review_support import RUN_ID, seed_review_project

from roughcut.adapters.project_store import ProjectStore
from roughcut.application.workflows import workflow_cancel, workflow_status


def _write_headers(child: ReviewCliChild, *, origin: str | None = None) -> dict[str, str]:
    headers = {"X-Roughcut-Token": child.token}
    if origin is not None:
        headers.update({"Content-Type": "application/json", "Origin": origin})
    return headers


def _artifact_snapshot(project: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(project)): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file() and path.name != "project.json"
    }


@pytest.mark.parametrize(
    ("surface", "basis_type"),
    (
        ("draft_review", None),
        ("roughcut_review", "proposal"),
        ("export_review", "decision"),
    ),
)
def test_w5_cli_run_id_reaches_only_the_current_review_surface(
    tmp_path: Path,
    surface: str,
    basis_type: str | None,
) -> None:
    project = seed_review_project(tmp_path / f"中文 {surface} project with spaces", surface)  # type: ignore[arg-type]
    child = ReviewCliChild.start(project)
    try:
        status, _headers, body = review_request(
            child.port,
            "GET",
            "/api/review",
            headers=_write_headers(child),
        )
        assert status == 200
        payload = json.loads(body)
        if basis_type is None:
            assert payload["review_mode"] == "workflow"
            assert payload["content_draft"]["status"] == "current"
            status, _headers, body = review_request(
                child.port,
                "GET",
                "/api/workflow/draft-editor",
                headers=_write_headers(child),
            )
            assert status == 200
            editor = json.loads(body)
            assert editor["review_mode"] == "draft_editor"
            assert editor["candidate"]["confirmed_by_user"] is False
            candidate_id = editor["candidate"]["candidate_id"]
            status, _headers, body = review_request(
                child.port,
                "POST",
                "/api/workflow/draft-agent-handoff",
                headers=_write_headers(
                    child,
                    origin=f"http://127.0.0.1:{child.port}",
                ),
                body=json.dumps({"candidate_id": candidate_id}).encode("utf-8"),
            )
            assert status == 200
            assert json.loads(body)["handoff"]["candidate_id"] == candidate_id
        else:
            assert payload["basis"] == {"type": basis_type, "id": payload["basis"]["id"]}
            assert payload["timeline"]["spans"]
            assert payload["project"]["name"] == "Windows 中文 Review"
    finally:
        child.stop()
    assert_port_closed(child.port)


def test_w5_review_mutation_uses_shared_core_and_stale_session_is_write_locked(
    tmp_path: Path,
) -> None:
    project = seed_review_project(
        tmp_path / "中文 mutation project with spaces",
        "draft_review",
        include_narration=True,
    )
    child = ReviewCliChild.start(project)
    try:
        status, _headers, body = review_request(
            child.port,
            "GET",
            "/api/workflow/draft-editor",
            headers=_write_headers(child),
        )
        assert status == 200
        editor = json.loads(body)
        candidate_id = editor["candidate"]["candidate_id"]
        before_candidates = {
            path.name for path in (project / "content-drafts").glob("*.json")
        }
        before_success_artifacts = _artifact_snapshot(project)
        workspace = editor["workspace"]
        checkpoint = workspace["expected_checkpoint_ref"]
        current_ref = workspace["expected_current_candidate_ref"]
        mutation = {
            "operation_id": f"dwop_{checkpoint['generation']}_{'a' * 32}",
            "expected_checkpoint_ref": checkpoint,
            "expected_current_candidate_ref": current_ref,
            "candidate_id": candidate_id,
            "block_id": "narration_w5",
            "text": "更新后的待录音解说。",
        }
        status, _headers, body = review_request(
            child.port,
            "POST",
            "/api/workflow/draft-narration",
            headers=_write_headers(child, origin=f"http://localhost:{child.port}"),
            body=json.dumps(mutation, ensure_ascii=False).encode("utf-8"),
        )
        assert status == 201
        created = json.loads(body)
        assert created["draft_editor"]["candidate"]["candidate_id"] != candidate_id
        assert created["draft_editor"]["candidate"]["has_unrecorded_narration"] is True
        assert workflow_status(project, RUN_ID)["workflow_run"]["stage"] == "draft_review"
        assert len({path.name for path in (project / "content-drafts").glob("*.json")}) == len(
            before_candidates
        ) + 1
        after_success_artifacts = _artifact_snapshot(project)
        assert len(after_success_artifacts) == len(before_success_artifacts) + 1

        current = ProjectStore(project).load()
        ProjectStore(project).save(
            replace(current, revision=current.revision + 1),
            expected_revision=current.revision,
        )
        status, _headers, body = review_request(
            child.port,
            "POST",
            "/api/workflow/brief",
            headers=_write_headers(child, origin=f"http://127.0.0.1:{child.port}"),
            body=json.dumps(
                {
                    "theme": "不应写入",
                    "target_duration_ticks": 240_000,
                    "focus": ["stale"],
                    "allow_reorder": True,
                },
                ensure_ascii=False,
            ).encode("utf-8"),
        )
        assert status == 409
        assert json.loads(body)["error"]["code"] == "stale_review"
        assert len({path.name for path in (project / "content-drafts").glob("*.json")}) == len(
            before_candidates
        ) + 1
        assert _artifact_snapshot(project) == after_success_artifacts
    finally:
        child.stop()
    assert_port_closed(child.port)


def test_w5_legacy_review_write_and_exact_run_fail_closed(tmp_path: Path) -> None:
    project = seed_review_project(tmp_path / "中文 legacy review project", "draft_review")
    child = ReviewCliChild.start(project)
    try:
        status, _headers, body = review_request(
            child.port,
            "POST",
            "/api/proposals",
            headers=_write_headers(child, origin=f"http://127.0.0.1:{child.port}"),
            body=b"{}",
        )
        assert status == 409
        assert json.loads(body)["error"]["code"] == "workflow_transition_not_allowed"
    finally:
        child.stop()
    assert_port_closed(child.port)

    unsupported = seed_review_project(tmp_path / "中文 unsupported review project", "scope_review")
    error = run_cli_raw(
        "review",
        str(unsupported),
        "--run-id",
        RUN_ID,
        "--json",
        expected_returncode=2,
    )
    assert error["error"] == {"code": "review_stage_unsupported"}

    closed = seed_review_project(tmp_path / "中文 closed review project", "scope_review")
    workflow_cancel(closed, RUN_ID, "act_w5_cancel")
    error = run_cli_raw(
        "review",
        str(closed),
        "--run-id",
        RUN_ID,
        "--json",
        expected_returncode=2,
    )
    assert error["error"] == {"code": "review_run_closed"}

    corrupt = seed_review_project(tmp_path / "中文 corrupt review project", "roughcut_review")
    proposal_ref = workflow_status(corrupt, RUN_ID)["workflow_run"]["artifact_refs"]["proposal"]
    assert isinstance(proposal_ref, dict)
    (corrupt / "proposals" / f"{proposal_ref['artifact_id']}.json").unlink()
    error = run_cli_raw(
        "review",
        str(corrupt),
        "--run-id",
        RUN_ID,
        "--json",
        expected_returncode=2,
    )
    assert error["error"] == {"code": "workflow_subject_mismatch"}
