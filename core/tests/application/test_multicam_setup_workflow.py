from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from test_workflows import _add_fixture_source, _workflow_project

from roughcut import mcp
from roughcut.adapters.project_store import ProjectStore
from roughcut.adapters.workflow_store import WorkflowStore
from roughcut.application.workflows import (
    read_confirmed_multicam_setup,
    workflow_action,
    workflow_start,
    workflow_status,
)
from roughcut.domain.errors import WorkflowError
from roughcut.domain.workflow import workflow_action_input_hash
from roughcut.domain.workflow_actions import parse_workflow_action_input


def _setup_declaration(
    *,
    auxiliary_camera_id: str = "aux_a",
    auxiliary_source_id: str = "src_b",
    no_aux: bool = False,
    main_source_id: str = "src_a",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "main_camera": {
            "camera_id": "main",
            "ordered_source_ids": [main_source_id],
        },
        "auxiliary_cameras": (
            []
            if no_aux
            else [
                {
                    "camera_id": auxiliary_camera_id,
                    "ordered_source_ids": [auxiliary_source_id],
                }
            ]
        ),
        "source_pairs": (
            []
            if no_aux
            else [
                {
                    "main_source_id": main_source_id,
                    "auxiliary_source_id": auxiliary_source_id,
                }
            ]
        ),
    }


def _approve_scope_input(
    root: Path,
    *,
    setup: dict[str, object] | None,
    authorizations: list[dict[str, object]] | None = None,
    basis: dict[str, object] | None = None,
    run_id: str = "wfr_test",
) -> dict[str, object]:
    status = workflow_status(root, run_id)
    selected_authorizations: object = authorizations
    if selected_authorizations is None:
        main_camera = cast(dict[str, object], setup["main_camera"]) if setup else None
        source_ids = (
            cast(list[str], main_camera["ordered_source_ids"])
            if main_camera is not None
            else [source.source_id for source in ProjectStore(root).load().sources]
        )
        selected_authorizations = [
            {
                "source_id": source_id,
                "transcribe": False,
                "speaker_diarization": False,
            }
            for source_id in source_ids
        ]
    value: dict[str, object] = {
        "schema_version": 1,
        "confirmation_basis": (
            status["confirmation_bases"]["scope"]["basis"]
            if basis is None
            else basis
        ),
        "source_authorizations": selected_authorizations,
    }
    if setup is not None:
        value["multicam_setup"] = setup
    return value


def test_scope_setup_replacement_uses_requested_bindings_not_old_run_scope(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    _add_fixture_source(root, "src_b")
    workflow_start(root, "wfr_test", ["src_a"])
    workflow_action(
        root,
        "wfr_test",
        "act_scope_initial",
        "approve_scope",
        _approve_scope_input(
            root,
            setup=None,
            authorizations=[
                {
                    "source_id": "src_a",
                    "transcribe": False,
                    "speaker_diarization": False,
                }
            ],
        ),
    )
    setup = _setup_declaration(no_aux=True, main_source_id="src_b")

    approved = workflow_action(
        root,
        "wfr_test",
        "act_scope_setup_replace",
        "approve_scope",
        _approve_scope_input(root, setup=setup),
    )

    assert approved.workflow_run.multicam_setup is not None
    assert approved.workflow_run.multicam_setup.main_camera.ordered_source_ids == (
        "src_b",
    )
    reopened = read_confirmed_multicam_setup(root, "wfr_test")
    assert reopened is not None
    assert reopened.main_camera.ordered_source_ids == ("src_b",)
    assert reopened.asr_scope[0].source_id == "src_b"
    assert [snapshot["source_id"] for snapshot in reopened.source_snapshots] == [
        "src_b"
    ]


@pytest.mark.parametrize(
    "extra_field,extra_value",
    [
        ("setup_id", "mcs_injected"),
        ("project_id", "proj_injected"),
        ("workflow_run_id", "wfr_injected"),
        ("asr_scope", []),
        ("source_snapshots", []),
        ("source_snapshot_hash", "a" * 64),
    ],
)
def test_public_setup_declaration_rejects_persisted_identity_injection(
    tmp_path: Path, extra_field: str, extra_value: object
) -> None:
    root = _workflow_project(tmp_path)
    workflow_start(root, "wfr_test", ["src_a"])
    declaration = _setup_declaration(no_aux=True)
    declaration[extra_field] = extra_value
    input_payload = _approve_scope_input(root, setup=declaration)

    with pytest.raises(WorkflowError) as captured:
        workflow_action(
            root,
            "wfr_test",
            f"act_invalid_{extra_field}",
            "approve_scope",
            input_payload,
        )
    assert captured.value.code == "workflow_action_invalid"


def test_public_setup_declaration_rejects_non_project_aux_source(tmp_path: Path) -> None:
    root = _workflow_project(tmp_path)
    workflow_start(root, "wfr_test", ["src_a"])
    declaration = _setup_declaration(auxiliary_source_id="src_missing")

    with pytest.raises(WorkflowError) as captured:
        workflow_action(
            root,
            "wfr_test",
            "act_invalid_missing_source",
            "approve_scope",
            _approve_scope_input(root, setup=declaration),
        )
    assert captured.value.code == "workflow_subject_mismatch"


def test_public_setup_declaration_main_must_be_in_requested_authorizations(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    _add_fixture_source(root, "src_b")
    workflow_start(root, "wfr_test", ["src_a"])
    declaration = _setup_declaration(no_aux=True, main_source_id="src_b")
    authorizations = [
        {"source_id": "src_a", "transcribe": False, "speaker_diarization": False}
    ]

    with pytest.raises(WorkflowError) as captured:
        workflow_action(
            root,
            "wfr_test",
            "act_invalid_main_scope",
            "approve_scope",
            _approve_scope_input(
                root, setup=declaration, authorizations=authorizations
            ),
        )
    assert captured.value.code == "workflow_subject_mismatch"


def test_public_setup_rejects_auxiliary_without_transcript_even_when_not_transcribed(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    _add_fixture_source(root, "src_b")
    project = ProjectStore(root).load()
    ProjectStore(root).save(
        replace(
            project,
            revision=project.revision + 1,
            active_transcript_versions={"src_a": "tr_a"},
        ),
        expected_revision=project.revision,
    )
    workflow_start(root, "wfr_test", ["src_a"])
    declaration = _setup_declaration()
    before = (root / "workflow" / "runs" / "wfr_test.json").read_bytes()

    with pytest.raises(WorkflowError) as captured:
        workflow_action(
            root,
            "wfr_test",
            "act_invalid_aux_authorization",
            "approve_scope",
            _approve_scope_input(
                root,
                setup=declaration,
                authorizations=[
                    {
                        "source_id": "src_a",
                        "transcribe": False,
                        "speaker_diarization": False,
                    },
                    {
                        "source_id": "src_b",
                        "transcribe": False,
                        "speaker_diarization": False,
                    },
                ],
            ),
        )

    assert captured.value.code == "workflow_not_ready"
    assert "no active Transcript" in str(captured.value)
    assert (root / "workflow" / "runs" / "wfr_test.json").read_bytes() == before
    reopened = WorkflowStore(root).read_run("wfr_test")
    assert reopened.approval_refs["scope"] is None
    assert reopened.multicam_setup is None


def test_public_setup_allows_ordered_extra_content_without_aux_asr_readiness(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    _add_fixture_source(root, "src_b")
    _add_fixture_source(root, "src_c")
    workflow_start(root, "wfr_test", ["src_a", "src_b", "src_c"])

    approved = workflow_action(
        root,
        "wfr_test",
        "act_ordered_extra_content",
        "approve_scope",
        _approve_scope_input(
            root,
            setup=_setup_declaration(),
            authorizations=[
                {
                    "source_id": "src_a",
                    "transcribe": False,
                    "speaker_diarization": False,
                },
                {
                    "source_id": "src_c",
                    "transcribe": False,
                    "speaker_diarization": False,
                },
            ],
        ),
    )

    assert [binding.source_id for binding in approved.workflow_run.ordered_bindings] == [
        "src_a",
        "src_c",
    ]
    assert [
        authorization.source_id
        for authorization in approved.workflow_run.scope_authorizations
    ] == ["src_a", "src_c"]
    assert approved.workflow_run.multicam_setup is not None
    assert [
        authorization.source_id
        for authorization in approved.workflow_run.multicam_setup.asr_scope
    ] == ["src_a", "src_c"]
    required_transcripts = approved.workflow_run.readiness_basis[
        "required_transcripts"
    ]
    assert isinstance(required_transcripts, list)
    assert [item["source_id"] for item in required_transcripts] == [
        "src_a",
        "src_c",
    ]
    assert "src_b" not in [item["source_id"] for item in required_transcripts]


def test_public_setup_declaration_pair_must_stay_in_declared_groups(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    _add_fixture_source(root, "src_b")
    workflow_start(root, "wfr_test", ["src_a"])
    declaration = _setup_declaration()
    pairs = cast(list[dict[str, object]], declaration["source_pairs"])
    pairs[0]["auxiliary_source_id"] = "src_missing"

    with pytest.raises(WorkflowError) as captured:
        workflow_action(
            root,
            "wfr_test",
            "act_invalid_pair_group",
            "approve_scope",
            _approve_scope_input(root, setup=declaration),
        )
    assert captured.value.code == "workflow_action_invalid"


def test_scope_setup_persists_and_reopens_from_a_new_process(tmp_path: Path) -> None:
    root = _workflow_project(tmp_path)
    _add_fixture_source(root, "src_b")
    workflow_start(root, "wfr_test", ["src_a"])
    setup = _setup_declaration()
    setup_input = _approve_scope_input(root, setup=setup)
    approved = workflow_action(
        root,
        "wfr_test",
        "act_scope_setup",
        "approve_scope",
        setup_input,
    )
    run_bytes = (root / "workflow" / "runs" / "wfr_test.json").read_bytes()
    repeated = workflow_action(
        root,
        "wfr_test",
        "act_scope_setup",
        "approve_scope",
        setup_input,
    )
    assert repeated.receipt == approved.receipt
    assert (root / "workflow" / "runs" / "wfr_test.json").read_bytes() == run_bytes

    assert approved.workflow_run.multicam_setup is not None
    persisted = approved.workflow_run.multicam_setup
    persisted_payload = persisted.to_dict()
    assert persisted_payload["project_id"] == ProjectStore(root).load().project_id
    assert persisted_payload["workflow_run_id"] == "wfr_test"
    assert persisted.main_camera.ordered_source_ids == ("src_a",)
    assert persisted.auxiliary_cameras[0].ordered_source_ids == ("src_b",)
    assert [binding.source_id for binding in approved.workflow_run.ordered_bindings] == [
        "src_a"
    ]
    assert [
        authorization.source_id
        for authorization in approved.workflow_run.scope_authorizations
    ] == ["src_a"]
    required_transcripts = approved.workflow_run.readiness_basis[
        "required_transcripts"
    ]
    assert isinstance(required_transcripts, list)
    assert [item["source_id"] for item in required_transcripts] == ["src_a"]
    assert [authorization.source_id for authorization in persisted.asr_scope] == ["src_a"]
    assert [snapshot["source_id"] for snapshot in persisted.source_snapshots] == [
        "src_a",
        "src_b",
    ]
    stored = WorkflowStore(root).read_run("wfr_test")
    assert stored.multicam_setup is not None
    assert stored.multicam_setup.to_dict() == persisted_payload
    assert workflow_status(root, "wfr_test")["workflow_run"]["multicam_setup"] == (
        persisted_payload
    )

    environment = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).parents[2] / "src"),
    }
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, sys; "
                "from pathlib import Path; "
                "from roughcut.application.workflows import read_confirmed_multicam_setup; "
                "setup = read_confirmed_multicam_setup(Path(sys.argv[1]), 'wfr_test'); "
                "print(json.dumps(setup.to_dict(), sort_keys=True))"
            ),
            str(root),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout) == persisted_payload


def test_setup_only_reapproval_changes_scope_identity_and_same_setup_is_noop(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    _add_fixture_source(root, "src_b")
    workflow_start(root, "wfr_test", ["src_a"])
    first_setup = _setup_declaration()
    first = workflow_action(
        root,
        "wfr_test",
        "act_scope_setup_one",
        "approve_scope",
        _approve_scope_input(root, setup=first_setup),
    )
    before = (root / "workflow" / "runs" / "wfr_test.json").read_bytes()
    changed_setup = _setup_declaration(auxiliary_camera_id="aux_changed")
    second = workflow_action(
        root,
        "wfr_test",
        "act_scope_setup_two",
        "approve_scope",
        _approve_scope_input(root, setup=changed_setup),
    )

    assert first.workflow_run.approval_refs["scope"] != second.workflow_run.approval_refs[
        "scope"
    ]
    assert first.receipt.input_hash != second.receipt.input_hash
    assert second.workflow_run.multicam_setup is not None
    assert first.workflow_run.multicam_setup is not None
    assert (
        first.workflow_run.multicam_setup.setup_id
        != second.workflow_run.multicam_setup.setup_id
    )
    assert second.workflow_run.multicam_setup.main_camera.ordered_source_ids == ("src_a",)
    assert second.workflow_run.multicam_setup.auxiliary_cameras[0].camera_id == (
        "aux_changed"
    )
    assert (root / "workflow" / "runs" / "wfr_test.json").read_bytes() != before

    with pytest.raises(WorkflowError, match="must change"):
        workflow_action(
            root,
            "wfr_test",
            "act_scope_setup_noop",
            "approve_scope",
            _approve_scope_input(root, setup=changed_setup),
        )


def test_setup_source_snapshot_change_is_stale_without_mutating_run(tmp_path: Path) -> None:
    root = _workflow_project(tmp_path)
    _add_fixture_source(root, "src_b")
    workflow_start(root, "wfr_test", ["src_a"])
    setup = _setup_declaration()
    workflow_action(
        root,
        "wfr_test",
        "act_scope_setup_stale",
        "approve_scope",
        _approve_scope_input(root, setup=setup),
    )
    run_path = root / "workflow" / "runs" / "wfr_test.json"
    before = run_path.read_bytes()
    project = ProjectStore(root).load()
    changed_source = replace(
        next(source for source in project.sources if source.source_id == "src_b"),
        fingerprint=replace(
            next(source for source in project.sources if source.source_id == "src_b").fingerprint,
            mtime_ns=99,
        ),
    )
    ProjectStore(root).save(
        replace(
            project,
            revision=project.revision + 1,
            sources=tuple(
                changed_source if source.source_id == "src_b" else source
                for source in project.sources
            ),
        ),
        expected_revision=project.revision,
    )

    with pytest.raises(WorkflowError) as captured:
        read_confirmed_multicam_setup(root, "wfr_test")
    assert captured.value.code == "workflow_stale"
    assert run_path.read_bytes() == before


def test_unreferenced_selectable_source_changes_only_scope_basis(tmp_path: Path) -> None:
    root = _workflow_project(tmp_path)
    _add_fixture_source(root, "src_b")
    _add_fixture_source(root, "src_c")
    workflow_start(root, "wfr_test", ["src_a"])
    setup = _setup_declaration()
    workflow_action(
        root,
        "wfr_test",
        "act_scope_setup_unreferenced",
        "approve_scope",
        _approve_scope_input(root, setup=setup),
    )
    before = workflow_status(root, "wfr_test")
    project = ProjectStore(root).load()
    changed_source = replace(
        next(source for source in project.sources if source.source_id == "src_c"),
        display_name="changed.wav",
    )
    ProjectStore(root).save(
        replace(
            project,
            revision=project.revision + 1,
            sources=tuple(
                changed_source if source.source_id == "src_c" else source
                for source in project.sources
            ),
        ),
        expected_revision=project.revision,
    )
    after = workflow_status(root, "wfr_test")

    assert after["approval_statuses"]["scope"] == "current"
    assert (
        after["confirmation_bases"]["scope"]["basis"]
        != before["confirmation_bases"]["scope"]["basis"]
    )


def test_no_aux_setup_is_durable_and_legacy_scope_projection_stays_old_shape(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    workflow_start(root, "wfr_test", ["src_a"])
    setup = _setup_declaration(no_aux=True)
    legacy_input = _approve_scope_input(root, setup=None)
    assert "multicam_setup" not in parse_workflow_action_input(
        "approve_scope", legacy_input
    )

    workflow_action(
        root,
        "wfr_test",
        "act_scope_no_aux",
        "approve_scope",
        _approve_scope_input(root, setup=setup),
    )
    reopened = read_confirmed_multicam_setup(root, "wfr_test")
    assert reopened is not None
    assert reopened.main_camera.ordered_source_ids == ("src_a",)
    assert reopened.auxiliary_cameras == ()
    assert reopened.source_pairs == ()

    legacy_root = _workflow_project(tmp_path / "legacy")
    workflow_start(legacy_root, "wfr_test", ["src_a"])
    legacy_payload = _approve_scope_input(legacy_root, setup=None)
    parsed_legacy = parse_workflow_action_input("approve_scope", legacy_payload)
    approved = workflow_action(
        legacy_root,
        "wfr_test",
        "act_scope_legacy",
        "approve_scope",
        legacy_payload,
    )
    assert approved.workflow_run.multicam_setup is None
    stored_payload = json.loads(
        (legacy_root / "workflow" / "runs" / "wfr_test.json").read_text()
    )
    assert "multicam_setup" not in stored_payload
    assert approved.receipt.input_hash == workflow_action_input_hash(
        "wfr_test", "act_scope_legacy", "approve_scope", parsed_legacy
    )


def test_legacy_approve_scope_input_hash_bytes_remain_frozen() -> None:
    payload = {
        "schema_version": 1,
        "confirmation_basis": {"basis_id": "wfb_scope_" + "a" * 64},
        "source_authorizations": [
            {
                "source_id": "src_a",
                "transcribe": False,
                "speaker_diarization": False,
            }
        ],
    }
    parsed = parse_workflow_action_input("approve_scope", payload)
    assert workflow_action_input_hash(
        "wfr_test", "act_test", "approve_scope", parsed
    ) == "d1551d43837c8112c4e61f40d8dc48befc47c53b0dfbe4ad482dc233cc8e63d8"


def test_public_workflow_schema_advertises_closed_durable_setup_family() -> None:
    tool = next(item for item in mcp.TOOLS if item["name"] == "workflow_action")
    branches = tool["inputSchema"]["oneOf"]
    approve = next(
        branch
        for branch in branches
        if branch["properties"]["action"]["const"] == "approve_scope"
    )
    input_schema = approve["properties"]["input"]
    assert input_schema["additionalProperties"] is False
    assert "multicam_setup" in input_schema["properties"]
    setup_schema = input_schema["properties"]["multicam_setup"]
    assert setup_schema["additionalProperties"] is False
    assert set(setup_schema["properties"]) == {
        "schema_version",
        "main_camera",
        "auxiliary_cameras",
        "source_pairs",
    }
    assert setup_schema["required"] == [
        "schema_version",
        "main_camera",
        "auxiliary_cameras",
        "source_pairs",
    ]
    declaration = _setup_declaration(no_aux=True)
    parsed = parse_workflow_action_input(
        "approve_scope",
        {
            "schema_version": 1,
            "confirmation_basis": {"basis_id": "wfb_scope_" + "a" * 64},
            "source_authorizations": [
                {
                    "source_id": "src_a",
                    "transcribe": False,
                    "speaker_diarization": False,
                }
            ],
            "multicam_setup": declaration,
        },
    )
    assert parsed["multicam_setup"] == declaration


def test_mcp_confirm_and_cli_reopen_return_the_same_durable_setup(
    tmp_path: Path,
) -> None:
    root = _workflow_project(tmp_path)
    _add_fixture_source(root, "src_b")
    start_response = mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": "setup-start",
            "method": "tools/call",
            "params": {
                "name": "workflow_start",
                "arguments": {
                    "project_path": str(root),
                    "run_id": "wfr_public_setup",
                    "ordered_source_ids": ["src_a"],
                },
            },
        }
    )
    assert start_response is not None
    start_payload = start_response["result"]["structuredContent"]
    setup = _setup_declaration()
    scope_input = {
        **_approve_scope_input(root, setup=setup, run_id="wfr_public_setup"),
        "confirmation_basis": start_payload["status"]["confirmation_bases"]["scope"][
            "basis"
        ],
    }
    action_response = mcp.handle_request(
        {
            "jsonrpc": "2.0",
            "id": "setup-action",
            "method": "tools/call",
            "params": {
                "name": "workflow_action",
                "arguments": {
                    "project_path": str(root),
                    "run_id": "wfr_public_setup",
                    "action_id": "act_public_setup",
                    "action": "approve_scope",
                    "input": scope_input,
                },
            },
        }
    )
    assert action_response is not None
    action_payload = action_response["result"]["structuredContent"]
    persisted_payload = action_payload["workflow_run"]["multicam_setup"]
    assert persisted_payload["project_id"] == ProjectStore(root).load().project_id
    assert persisted_payload["workflow_run_id"] == "wfr_public_setup"
    assert persisted_payload["main_camera"] == setup["main_camera"]
    assert persisted_payload["auxiliary_cameras"] == setup["auxiliary_cameras"]

    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "roughcut.cli",
            "workflow-status",
            "--project",
            str(root),
            "--run-id",
            "wfr_public_setup",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")},
    )
    assert process.returncode == 0, process.stderr
    assert process.stderr == ""
    assert (
        json.loads(process.stdout)["status"]["workflow_run"]["multicam_setup"]
        == persisted_payload
    )
