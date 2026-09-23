from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from roughcut import cli


class _FakeReview:
    url = "http://127.0.0.1:43210/?token=fixture"

    def __init__(self) -> None:
        self.waited = False
        self.closed = False

    def wait(self) -> None:
        self.waited = True
        raise KeyboardInterrupt

    def close(self) -> None:
        self.closed = True


def _workflow_run_files(tmp_path: Path) -> Path:
    project = tmp_path / "workflow project"
    (project / "workflow" / "runs").mkdir(parents=True)
    (project / "project.json").write_text("{}", encoding="utf-8")
    (project / "workflow" / "runs" / "run_exact.json").write_text(
        "{}", encoding="utf-8"
    )
    return project


def _workflow_status(
    stage: str,
    *,
    lifecycle: str = "active",
    content_draft: str | None = "draft_anchor",
    proposal: str | None = "proposal_current",
    decision: str | None = "edit_current",
) -> dict[str, object]:
    def ref(artifact_id: str | None) -> dict[str, object] | None:
        return (
            None
            if artifact_id is None
            else {
                "artifact_id": artifact_id,
                "schema_version": 1,
                "content_hash": "a" * 64,
            }
        )

    return {
        "workflow_run": {
            "run_id": "run_exact",
            "stage": stage,
            "lifecycle": lifecycle,
            "ordered_bindings": [
                {
                    "source_id": "src_a",
                    "transcript_version_id": "tr_a",
                    "transcript_content_hash": "b" * 64,
                }
            ],
            "artifact_refs": {
                "brief": ref("brief_current"),
                "outline": ref("outline_current"),
                "content_draft": ref(content_draft),
                "proposal": ref(proposal),
                "decision": ref(decision),
                "render": None,
            },
        }
    }


def test_review_cli_accepts_positional_project_outputs_json_and_cleans_up(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    review = _FakeReview()
    captured: dict[str, object] = {}

    def fake_start(project_path, *, proposal_id, edit_version_id):  # type: ignore[no-untyped-def]
        captured.update(
            project_path=project_path,
            proposal_id=proposal_id,
            edit_version_id=edit_version_id,
        )
        return review

    monkeypatch.setattr(cli, "start_review_server", fake_start)
    project = tmp_path / "项目"

    cli.main(["review", str(project), "--proposal-id", "proposal_a", "--json"])

    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["review"]["url"] == review.url
    assert captured == {
        "project_path": project,
        "proposal_id": "proposal_a",
        "edit_version_id": None,
    }
    assert review.waited is True
    assert review.closed is True


def test_review_cli_human_entry_does_not_require_json(tmp_path: Path, monkeypatch, capsys) -> None:
    review = _FakeReview()
    monkeypatch.setattr(cli, "start_review_server", lambda *args, **kwargs: review)

    cli.main(["review", str(tmp_path / "project"), "--edit-version-id", "edit_a"])

    assert capsys.readouterr().out.strip() == f"Review: {review.url}"
    assert review.closed is True


def test_review_cli_accepts_explicit_workflow_bindings_and_optional_draft(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    review = _FakeReview()
    captured: dict[str, object] = {}

    def fake_start(project_path, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(project_path=project_path, **kwargs)
        return review

    monkeypatch.setattr(cli, "start_review_server", fake_start)
    project = tmp_path / "workflow project"
    bindings = [
        {"source_id": "src_b", "transcript_version_id": "tr_b"},
        {"source_id": "src_a", "transcript_version_id": "tr_a"},
    ]

    cli.main(
        [
            "review",
            str(project),
            "--source-bindings-json",
            json.dumps(bindings),
            "--content-draft-id",
            "draft_a",
            "--json",
        ]
    )

    assert json.loads(capsys.readouterr().out)["review"]["url"] == review.url
    assert captured == {
        "project_path": project,
        "proposal_id": None,
        "edit_version_id": None,
        "source_bindings": bindings,
        "content_draft_id": "draft_a",
    }


def test_review_cli_rejects_workflow_basis_mixing_as_pure_json(
    tmp_path: Path, capsys
) -> None:
    with pytest.raises(SystemExit) as stopped:
        cli.main(
            [
                "review",
                str(tmp_path / "project"),
                "--source-bindings-json",
                '[{"source_id":"src_a","transcript_version_id":"tr_a"}]',
                "--proposal-id",
                "proposal_a",
                "--json",
            ]
        )

    output = capsys.readouterr()
    assert stopped.value.code == 2
    assert output.err == ""
    assert json.loads(output.out)["error"]["code"] == "invalid_arguments"


def test_review_cli_run_id_routes_three_existing_review_surfaces(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    project = _workflow_run_files(tmp_path)
    review = _FakeReview()
    captured: list[dict[str, object]] = []
    current_status = _workflow_status("draft_review")

    def fake_status(project_path, run_id):  # type: ignore[no-untyped-def]
        assert project_path == project
        assert run_id == "run_exact"
        return current_status

    def fake_start(project_path, **kwargs):  # type: ignore[no-untyped-def]
        captured.append({"project_path": project_path, **kwargs})
        return review

    monkeypatch.setattr(cli, "workflow_status", fake_status)
    monkeypatch.setattr(cli, "start_review_server", fake_start)
    monkeypatch.setattr(
        cli,
        "read_draft_workspace",
        lambda project_path, *, run_id: SimpleNamespace(
            current_candidate=SimpleNamespace(content_draft_id="draft_child")
        ),
    )
    monkeypatch.setattr(cli, "validate_workflow_proposal_ref", lambda *_args: None)
    monkeypatch.setattr(
        cli,
        "read_decision",
        lambda *_args: SimpleNamespace(
            decision=SimpleNamespace(edit_version_id="edit_current")
        ),
    )
    for stage in ("draft_review", "roughcut_review", "export_review"):
        current_status = _workflow_status(stage)
        cli.main(["review", str(project), "--run-id", "run_exact", "--json"])
        capsys.readouterr()

    assert captured == [
        {
            "project_path": project,
            "proposal_id": None,
            "edit_version_id": None,
            "source_bindings": [
                {"source_id": "src_a", "transcript_version_id": "tr_a"}
            ],
            "content_draft_id": "draft_child",
        },
        {
            "project_path": project,
            "proposal_id": "proposal_current",
            "edit_version_id": None,
        },
        {
            "project_path": project,
            "proposal_id": None,
            "edit_version_id": "edit_current",
        },
    ]


@pytest.mark.parametrize(
    ("stage", "lifecycle", "error_code"),
    [
        ("scope_review", "active", "review_stage_unsupported"),
        ("outline_review", "active", "review_stage_unsupported"),
        ("exporting", "active", "review_stage_unsupported"),
        ("draft_review", "canceled", "review_run_closed"),
        ("export_review", "completed", "review_run_closed"),
    ],
)
def test_review_cli_run_id_rejects_unsupported_and_closed_runs(
    tmp_path: Path,
    monkeypatch,
    capsys,
    stage: str,
    lifecycle: str,
    error_code: str,
) -> None:
    project = _workflow_run_files(tmp_path)
    monkeypatch.setattr(
        cli, "workflow_status", lambda *_args: _workflow_status(stage, lifecycle=lifecycle)
    )

    with pytest.raises(SystemExit) as stopped:
        cli.main(["review", str(project), "--run-id", "run_exact", "--json"])

    assert stopped.value.code == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == error_code


def test_review_cli_run_id_rejects_missing_or_corrupt_exact_refs(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    project = _workflow_run_files(tmp_path)
    monkeypatch.setattr(
        cli,
        "workflow_status",
        lambda *_args: _workflow_status("roughcut_review", proposal=None),
    )

    with pytest.raises(SystemExit) as stopped:
        cli.main(["review", str(project), "--run-id", "run_exact", "--json"])

    assert stopped.value.code == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "review_run_integrity"


def test_review_cli_run_id_unknown_exact_run_and_second_launcher_are_rejected(
    tmp_path: Path, capsys
) -> None:
    project = tmp_path / "workflow project"
    project.mkdir()
    (project / "project.json").write_text("{}", encoding="utf-8")

    with pytest.raises(SystemExit) as stopped:
        cli.main(["review", str(project), "--run-id", "run_missing", "--json"])
    assert stopped.value.code == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "review_run_not_found"

    with pytest.raises(SystemExit) as stopped:
        cli.main(
            ["review", str(project), "--workflow-run", "run_missing", "--json"]
        )
    assert stopped.value.code == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_arguments"
